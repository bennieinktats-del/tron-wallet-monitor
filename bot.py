import os
import time
import sqlite3
import logging
import calendar
import html
import requests

from collections import defaultdict
from datetime import datetime, timedelta, timezone

# ============================================================
# TRON WALLET MONITOR
# ============================================================

print("🚀 Starting Persistent TRON Wallet Monitor...")

# ============================================================
# CONFIGURATION
# ============================================================

TARGET_PAIRS = int(os.getenv("TARGET_PAIRS", "50"))
MIN_TRANSFER_USD = float(os.getenv("MIN_TRANSFER_USD", "50"))
WEEKS_BACK = int(os.getenv("WEEKS_BACK", "2"))
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "120"))

TRONSCAN_API_KEY = os.getenv("TRONSCAN_API_KEY", "")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
DATABASE_FILE = os.getenv("DATABASE_FILE", "tron_monitor.db")

SCAN_STATUS = "Not started"
SCAN_PAGE = 0
SCAN_OFFSET = 0
SCAN_PROCESSED = 0
SCAN_CANDIDATE_PAIRS = 0
SCAN_TWO_PLUS_PAIRS = 0
SCAN_CEX_QUALIFIED = 0
SCAN_LAST_ACTIVITY = "Never"
SCAN_STARTED_AT = "Never"
SCAN_COMPLETED_AT = "Never"
MONITOR_CYCLES = 0
MONITOR_LAST_ACTIVITY = "Never"

USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRONSCAN_URL = "https://apilist.tronscanapi.com/api"
TRONGRID_URL = "https://api.trongrid.io"

CEX_KEYWORDS = [
    "binance", "okx", "huobi", "htx", "gate", "kucoin", "bybit",
    "mexc", "bitfinex", "coinbase", "kraken", "bitget", "poloniex",
]

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("tron-monitor")

# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_a TEXT NOT NULL,
    wallet_b TEXT NOT NULL,
    cex_name TEXT,
    transfer_count INTEGER DEFAULT 0,
    total_amount REAL DEFAULT 0,
    cex_balance REAL DEFAULT 0,
    first_seen TEXT,
    last_seen TEXT,
    qualified_at TEXT,
    active INTEGER DEFAULT 1,
    alerted INTEGER DEFAULT 0,
    UNIQUE(wallet_a, wallet_b)
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS excluded_pairs (
    pair_id INTEGER PRIMARY KEY,
    excluded_at TEXT NOT NULL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS monitored_wallets (
    address TEXT PRIMARY KEY,
    wallet_type TEXT,
    pair_id INTEGER,
    first_seen TEXT,
    last_checked TEXT,
    last_transaction_timestamp INTEGER DEFAULT 0
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS scan_state (
    key TEXT PRIMARY KEY,
    value TEXT
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS telegram_state (
    key TEXT PRIMARY KEY,
    value TEXT
)
""")

# Persist the actual historical transfers seen by the scanner.
# This is deliberately separate from the pairs table: pair qualification
# can survive independently, while /history needs the underlying TXs.
db.execute("""
CREATE TABLE IF NOT EXISTS historical_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_a TEXT NOT NULL,
    wallet_b TEXT NOT NULL,
    txid TEXT,
    timestamp INTEGER DEFAULT 0,
    amount REAL DEFAULT 0,
    cex_name TEXT,
    qualifying INTEGER DEFAULT 0,
    source TEXT DEFAULT 'historical_scan',
    UNIQUE(wallet_a, wallet_b, txid)
)
""")

# Upgrade history records created by older versions.
_columns = {row[1] for row in db.execute("PRAGMA table_info(historical_transactions)").fetchall()}
if "cex_name" not in _columns:
    db.execute("ALTER TABLE historical_transactions ADD COLUMN cex_name TEXT")
if "qualifying" not in _columns:
    db.execute("ALTER TABLE historical_transactions ADD COLUMN qualifying INTEGER DEFAULT 0")
if "source" not in _columns:
    db.execute("ALTER TABLE historical_transactions ADD COLUMN source TEXT DEFAULT 'historical_scan'")

db.commit()

# ============================================================
# HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def utc_string():
    return utc_now().isoformat()


def telegram_escape(value):
    return html.escape(str(value or ""))


def transaction_id(tx):
    return tx.get("hash") or tx.get("transaction_id") or tx.get("txID") or ""


def transaction_timestamp(tx):
    try:
        return int(tx.get("block_ts") or tx.get("timestamp") or 0)
    except (ValueError, TypeError):
        return 0


def transfer_amount(tx):
    try:
        raw = int(tx.get("quant", 0))
    except (ValueError, TypeError):
        return 0.0
    return raw / 1_000_000


def get_tag_name(tx):
    tag = tx.get("from_address_tag")
    if isinstance(tag, dict):
        return (
            tag.get("from_address_tag")
            or tag.get("name")
            or tag.get("tag")
            or ""
        )
    if isinstance(tag, str):
        return tag
    return ""


def detect_cex(tag):
    normalized = str(tag or "").lower()
    for keyword in CEX_KEYWORDS:
        if keyword in normalized:
            return True, str(tag)
    return False, str(tag or "Unknown")


def format_tx_date(tx):
    timestamp = transaction_timestamp(tx)
    if not timestamp:
        return "Unknown"
    try:
        return datetime.fromtimestamp(
            timestamp / 1000, timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OSError, OverflowError):
        return "Unknown"


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_URL = (
    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    if TELEGRAM_BOT_TOKEN
    else ""
)


def send_telegram(message):
    if not TELEGRAM_URL or not CHAT_ID:
        log.warning("Telegram is not configured.")
        return False

    try:
        response = requests.post(
            f"{TELEGRAM_URL}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if response.ok:
            return True

        log.warning(
            "Telegram error %s: %s",
            response.status_code,
            response.text[:300],
        )
    except requests.RequestException as exc:
        log.warning("Telegram request failed: %s", exc)

    return False


def get_telegram_offset():
    row = db.execute("""
        SELECT value
        FROM telegram_state
        WHERE key = 'offset'
    """).fetchone()

    if not row:
        return 0

    try:
        return int(row["value"])
    except (ValueError, TypeError):
        return 0


def set_telegram_offset(offset):
    db.execute("""
        INSERT INTO telegram_state(key, value)
        VALUES('offset', ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
    """, (str(offset),))
    db.commit()


# ============================================================
# API HEADERS
# ============================================================

def tronscan_headers():
    if TRONSCAN_API_KEY:
        return {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
    return {}


def trongrid_headers():
    if TRONGRID_API_KEY:
        return {"TRON-PRO-API-KEY": TRONGRID_API_KEY}
    return {}


# ============================================================
# HTTP
# ============================================================

def http_get(url, params=None, headers=None, timeout=30, attempts=3):
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers=headers or {},
                timeout=timeout,
            )

            if response.status_code == 429:
                wait = min(10 * attempt, 30)
                log.warning("Rate limited. Waiting %ss...", wait)
                time.sleep(wait)
                continue

            response.raise_for_status()
            return response.json()

        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(2 * attempt, 10))

    raise RuntimeError(
        f"API request failed after {attempts} attempts: {last_error}"
    )


# ============================================================
# TRONSCAN GLOBAL TRANSFER PAGE
# ============================================================

def get_usdt_transfer_page(
    start_timestamp=None,
    end_timestamp=None,
    start=0,
    limit=50,
):
    params = {
        "start": start,
        "limit": limit,
        "contract_address": USDT_CONTRACT,
        "sort": "-timestamp",
    }

    if start_timestamp is not None:
        params["start_timestamp"] = start_timestamp

    if end_timestamp is not None:
        params["end_timestamp"] = end_timestamp

    return http_get(
        f"{TRONSCAN_URL}/token_trc20/transfers",
        params=params,
        headers=tronscan_headers(),
        timeout=30,
    )


def get_usdt_transfers(
    start_timestamp=None,
    end_timestamp=None,
    start=0,
    limit=50,
):
    data = get_usdt_transfer_page(
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        start=start,
        limit=limit,
    )
    return data.get("token_transfers", [])


# ============================================================
# WALLET HISTORY
# ============================================================

def get_wallet_history(address, limit=50, start=0):
    try:
        data = http_get(
            f"{TRONSCAN_URL}/token_trc20/transfers",
            params={
                "address": address,
                "limit": limit,
                "start": start,
                "sort": "-timestamp",
                "contract_address": USDT_CONTRACT,
            },
            headers=tronscan_headers(),
            timeout=30,
        )
        return data.get("token_transfers", [])
    except Exception as exc:
        log.warning(
            "History lookup failed for %s: %s",
            address,
            exc,
        )
        return []


# ============================================================
# TRON DATA
# ============================================================

def get_usdt_balance(address):
    try:
        data = http_get(
            f"{TRONGRID_URL}/v1/accounts/{address}/trc20",
            params={
                "only_confirmed": "true",
                "limit": 200,
            },
            headers=trongrid_headers(),
            timeout=20,
        )

        for token in data.get("data", []):
            token_info = token.get("token_info", {})
            token_address = token_info.get("address")

            if token_address != USDT_CONTRACT:
                continue

            raw = int(token.get("balance", 0))
            decimals = int(token_info.get("decimals", 6))
            return raw / (10 ** decimals)

    except Exception as exc:
        log.warning(
            "Could not obtain USDT balance for %s: %s",
            address,
            exc,
        )

    return 0.0


# ============================================================
# HISTORICAL TRANSACTION STORAGE
# ============================================================

def save_historical_transaction(
    wallet_a,
    wallet_b,
    tx,
    cex_name=None,
    qualifying=False,
    source="historical_scan",
):
    """Persist the exact blockchain transfer used/seen by the scanner."""
    txid = transaction_id(tx) or None
    timestamp = transaction_timestamp(tx)
    amount = transfer_amount(tx)

    if txid:
        row = db.execute("""
            SELECT id
            FROM historical_transactions
            WHERE wallet_a = ? AND wallet_b = ? AND txid = ?
            LIMIT 1
        """, (wallet_a, wallet_b, txid)).fetchone()
    else:
        row = db.execute("""
            SELECT id
            FROM historical_transactions
            WHERE wallet_a = ?
              AND wallet_b = ?
              AND txid IS NULL
              AND timestamp = ?
              AND ABS(amount - ?) < 0.000001
            LIMIT 1
        """, (wallet_a, wallet_b, timestamp, amount)).fetchone()

    if row:
        db.execute("""
            UPDATE historical_transactions
            SET cex_name = COALESCE(?, cex_name),
                qualifying = MAX(COALESCE(qualifying, 0), ?),
                source = COALESCE(?, source)
            WHERE id = ?
        """, (
            cex_name,
            1 if qualifying else 0,
            source,
            row["id"],
        ))
    else:
        db.execute("""
            INSERT INTO historical_transactions(
                wallet_a, wallet_b, txid, timestamp, amount,
                cex_name, qualifying, source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            wallet_a,
            wallet_b,
            txid,
            timestamp,
            amount,
            cex_name,
            1 if qualifying else 0,
            source,
        ))

    db.commit()


def get_saved_historical_transactions(
    wallet_a,
    wallet_b,
    start_ts_ms=None,
    end_ts_ms=None,
):
    query = """
        SELECT txid, timestamp, amount, cex_name, qualifying, source
        FROM historical_transactions
        WHERE wallet_a = ? AND wallet_b = ?
    """
    params = [wallet_a, wallet_b]

    if start_ts_ms is not None:
        query += " AND timestamp >= ?"
        params.append(start_ts_ms)

    if end_ts_ms is not None:
        query += " AND timestamp <= ?"
        params.append(end_ts_ms)

    query += " ORDER BY timestamp DESC, id DESC"

    rows = db.execute(query, params).fetchall()

    return [
        {
            "txid": row["txid"] or "",
            "timestamp": row["timestamp"] or 0,
            "amount": row["amount"] or 0.0,
            "cex_name": row["cex_name"],
            "qualifying": bool(row["qualifying"]),
            "source": row["source"],
        }
        for row in rows
    ]


def pair_is_excluded(pair_id):
    row = db.execute("""
        SELECT 1
        FROM excluded_pairs
        WHERE pair_id = ?
    """, (pair_id,)).fetchone()

    return row is not None


def save_pair(
    wallet_a,
    wallet_b,
    cex_name,
    transfer_count,
    total_amount,
    cex_balance,
):
    now = utc_string()

    existing = db.execute("""
        SELECT *
        FROM pairs
        WHERE wallet_a = ?
          AND wallet_b = ?
    """, (wallet_a, wallet_b)).fetchone()

    if existing:
        db.execute("""
            UPDATE pairs
            SET transfer_count = ?,
                total_amount = ?,
                cex_balance = ?,
                cex_name = ?,
                last_seen = ?
            WHERE id = ?
        """, (
            transfer_count,
            total_amount,
            cex_balance,
            cex_name,
            now,
            existing["id"],
        ))
        pair_id = existing["id"]
        is_new = False

    else:
        cursor = db.execute("""
            INSERT INTO pairs (
                wallet_a,
                wallet_b,
                cex_name,
                transfer_count,
                total_amount,
                cex_balance,
                first_seen,
                last_seen,
                qualified_at,
                active,
                alerted
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
        """, (
            wallet_a,
            wallet_b,
            cex_name,
            transfer_count,
            total_amount,
            cex_balance,
            now,
            now,
            now,
        ))
        pair_id = cursor.lastrowid
        is_new = True

    # Explicitly preserve the meaning of each monitored wallet:
    # A and B are TRON wallets monitored for TRC-20 USDT.
    db.execute("""
        INSERT INTO monitored_wallets(
            address,
            wallet_type,
            pair_id,
            first_seen
        )
        VALUES (?, 'A — TRON / TRC-20 USDT', ?, ?)
        ON CONFLICT(address)
        DO UPDATE SET
            wallet_type = excluded.wallet_type,
            pair_id = excluded.pair_id
    """, (wallet_a, pair_id, now))

    db.execute("""
        INSERT INTO monitored_wallets(
            address,
            wallet_type,
            pair_id,
            first_seen
        )
        VALUES (?, 'B — TRON / TRC-20 USDT', ?, ?)
        ON CONFLICT(address)
        DO UPDATE SET
            wallet_type = excluded.wallet_type,
            pair_id = excluded.pair_id
    """, (wallet_b, pair_id, now))

    db.commit()
    return pair_id, is_new


def refresh_active_slots():
    """Keep only the first TARGET_PAIRS non-excluded pairs active."""
    db.execute("UPDATE pairs SET active = 0")

    rows = db.execute("""
        SELECT p.id
        FROM pairs p
        LEFT JOIN excluded_pairs e
          ON e.pair_id = p.id
        WHERE e.pair_id IS NULL
        ORDER BY p.id
        LIMIT ?
    """, (TARGET_PAIRS,)).fetchall()

    for row in rows:
        db.execute(
            "UPDATE pairs SET active = 1 WHERE id = ?",
            (row["id"],),
        )

    db.commit()


def get_active_display_pairs():
    """Return active pairs in user-facing Pair #1, #2... order."""
    return db.execute("""
        SELECT *
        FROM pairs
        WHERE active = 1
        ORDER BY id
    """).fetchall()


# ============================================================
# PAIR ALERT
# ============================================================

def announce_pair(pair_id, pair):
    status = "✅" if pair["cex_balance"] >= 500 else "⚠️"

    message = (
        f"{status} <b>Eligible Pair #{pair_id}</b>\n\n"
        f"🏦 <b>CEX / Wallet A — TRON / TRC-20 USDT:</b>\n"
        f"<code>{telegram_escape(pair['wallet_a'])}</code>\n\n"
        f"🏷 <b>CEX Tag:</b> "
        f"{telegram_escape(pair['cex_name'])}\n"
        f"💰 <b>USDT Balance:</b> "
        f"${pair['cex_balance']:,.2f}\n\n"
        f"👤 <b>Wallet B — TRON / TRC-20 USDT:</b>\n"
        f"<code>{telegram_escape(pair['wallet_b'])}</code>\n\n"
        f"📊 <b>Historical A → B Transfers:</b> "
        f"{pair['transfer_count']}x\n"
        f"💵 <b>Total Volume:</b> "
        f"${pair['total_amount']:,.2f}\n\n"
        f"Historical qualification window: "
        f"{WEEKS_BACK} week(s).\n"
        f"Monitoring will continue after qualification."
    )

    send_telegram(message)


# ============================================================
# HISTORICAL SCANNER
# ============================================================

def historical_scan():
    end_date = utc_now()
    start_date = end_date - timedelta(weeks=WEEKS_BACK)

    start_ms = int(start_date.timestamp() * 1000)
    end_ms = int(end_date.timestamp() * 1000)

    global SCAN_STATUS, SCAN_PAGE, SCAN_OFFSET
    global SCAN_PROCESSED, SCAN_CANDIDATE_PAIRS
    global SCAN_TWO_PLUS_PAIRS, SCAN_CEX_QUALIFIED
    global SCAN_LAST_ACTIVITY, SCAN_STARTED_AT, SCAN_COMPLETED_AT

    SCAN_STATUS = "Running"
    SCAN_PAGE = 0
    SCAN_OFFSET = 0
    SCAN_PROCESSED = 0
    SCAN_CANDIDATE_PAIRS = 0
    SCAN_TWO_PLUS_PAIRS = 0
    SCAN_CEX_QUALIFIED = 0
    SCAN_LAST_ACTIVITY = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
    SCAN_STARTED_AT = SCAN_LAST_ACTIVITY
    SCAN_COMPLETED_AT = "Never"

    log.info(
        "Historical scan: %s → %s",
        start_date.isoformat(),
        end_date.isoformat(),
    )

    pair_stats = defaultdict(
        lambda: {
            "count": 0,
            "total": 0.0,
            "tags": set(),
            "txids": set(),
            # Store the actual historical transfers so /history
            # can display exactly what qualified the pair.
            "transactions": [],
        }
    )

    seen_global_txids = set()
    offset = 0
    page_size = 50
    processed = 0
    duplicate_pages = 0
    max_duplicate_pages = 5
    max_pages = 500
    page_number = 0

    while True:
        page_number += 1

        if page_number > max_pages:
            log.warning(
                "Historical scan reached safety limit of %s pages.",
                max_pages,
            )
            break

        try:
            data = get_usdt_transfer_page(
                start_timestamp=start_ms,
                end_timestamp=end_ms,
                start=offset,
                limit=page_size,
            )
        except Exception as exc:
            log.error("Historical scan request failed: %s", exc)
            time.sleep(10)
            continue

        transfers = data.get("token_transfers", [])

        if not transfers:
            log.info(
                "No more historical transfers returned at offset %s.",
                offset,
            )
            break

        log.info(
            "Historical batch: %s transfers (offset %s)",
            len(transfers),
            offset,
        )

        new_transactions = 0

        for tx in transfers:
            txid = transaction_id(tx)

            if txid and txid in seen_global_txids:
                continue

            if txid:
                seen_global_txids.add(txid)

            new_transactions += 1

            wallet_a = tx.get("from_address")
            wallet_b = tx.get("to_address")

            if not wallet_a or not wallet_b:
                continue

            amount = transfer_amount(tx)

            # Preserve every historical A -> B TRC-20 USDT transfer encountered
            # by the scanner, even when it is below the qualification threshold.
            save_historical_transaction(
                wallet_a,
                wallet_b,
                tx,
                source="historical_scan",
            )

            if amount <= MIN_TRANSFER_USD:
                continue

            timestamp = transaction_timestamp(tx)

            if timestamp:
                if timestamp < start_ms:
                    continue
                if timestamp > end_ms:
                    continue

            # The scanner has now positively seen this historical
            # TRON TRC-20 USDT transfer. Save it so /history can show
            # the exact transactions that established the pair.
            save_historical_transaction(wallet_a, wallet_b, tx)

            pair_key = (wallet_a, wallet_b)
            stats = pair_stats[pair_key]

            if txid:
                if txid in stats["txids"]:
                    continue
                stats["txids"].add(txid)

            stats["count"] += 1
            stats["total"] += amount

            tag = get_tag_name(tx)
            if tag:
                stats["tags"].add(tag)

            # IMPORTANT:
            # Keep the actual historical transfer. This is what fixes
            # /history when the wallet endpoint later fails to return
            # the same historical transactions.
            stats["transactions"].append(tx)

        if new_transactions == 0:
            duplicate_pages += 1
            log.warning(
                "Historical page contained no new transactions "
                "(duplicate page %s/%s).",
                duplicate_pages,
                max_duplicate_pages,
            )

            if duplicate_pages >= max_duplicate_pages:
                log.warning(
                    "Historical pagination stopped after %s "
                    "consecutive duplicate pages.",
                    max_duplicate_pages,
                )
                break
        else:
            duplicate_pages = 0
            processed += new_transactions

        SCAN_PAGE = page_number
        SCAN_OFFSET = offset
        SCAN_PROCESSED = processed
        SCAN_CANDIDATE_PAIRS = len(pair_stats)
        SCAN_TWO_PLUS_PAIRS = sum(
            1 for stats in pair_stats.values()
            if stats["count"] >= 2
        )
        SCAN_CEX_QUALIFIED = sum(
            1 for stats in pair_stats.values()
            if stats["count"] >= 2
            and any(
                detect_cex(tag)[0]
                for tag in stats["tags"]
            )
        )
        db.commit()

        SCAN_LAST_ACTIVITY = utc_now().strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )

        log.info(
            "Historical progress: %s unique transfers | "
            "%s candidate pairs | %s with 2+ transfers | "
            "%s CEX-qualified",
            SCAN_PROCESSED,
            SCAN_CANDIDATE_PAIRS,
            SCAN_TWO_PLUS_PAIRS,
            SCAN_CEX_QUALIFIED,
        )

        # --------------------------------------------------------
        # Qualification check
        # --------------------------------------------------------

        for (wallet_a, wallet_b), stats in list(pair_stats.items()):
            if stats["count"] < 2:
                continue

            existing_pair = db.execute("""
                SELECT id
                FROM pairs
                WHERE wallet_a = ?
                  AND wallet_b = ?
            """, (wallet_a, wallet_b)).fetchone()

            if existing_pair:
                continue

            cex_name = ""

            for tag in stats["tags"]:
                is_cex, detected_name = detect_cex(tag)
                if is_cex:
                    cex_name = detected_name
                    break

            if not cex_name:
                continue

            log.info(
                "Candidate pair qualifies: %s → %s | %sx | $%.2f | CEX=%s",
                wallet_a,
                wallet_b,
                stats["count"],
                stats["total"],
                cex_name,
            )

            balance = get_usdt_balance(wallet_a)

            pair = {
                "wallet_a": wallet_a,
                "wallet_b": wallet_b,
                "cex_name": cex_name,
                "transfer_count": stats["count"],
                "total_amount": stats["total"],
                "cex_balance": balance,
            }

            # Mark the exact blockchain transfers that established qualification.
            for evidence_tx in stats["transactions"]:
                save_historical_transaction(
                    wallet_a,
                    wallet_b,
                    evidence_tx,
                    cex_name=cex_name,
                    qualifying=True,
                    source="qualifying_scan",
                )

            pair_id, is_new = save_pair(
                wallet_a,
                wallet_b,
                cex_name,
                stats["count"],
                stats["total"],
                balance,
            )

            if is_new:
                log.info("✅ Eligible pair found: #%s", pair_id)
                announce_pair(pair_id, pair)

        offset += page_size

        if len(transfers) < page_size:
            log.info(
                "Historical final page reached (%s transfers).",
                len(transfers),
            )
            break

        time.sleep(0.25)

    # ========================================================
    # FINAL QUALIFICATION PASS
    # ========================================================

    log.info(
        "Historical pages finished. "
        "Performing final pair qualification..."
    )

    for (wallet_a, wallet_b), stats in pair_stats.items():
        if stats["count"] < 2:
            continue

        existing_pair = db.execute("""
            SELECT id
            FROM pairs
            WHERE wallet_a = ?
              AND wallet_b = ?
        """, (wallet_a, wallet_b)).fetchone()

        if existing_pair:
            continue

        cex_name = ""

        for tag in stats["tags"]:
            is_cex, detected_name = detect_cex(tag)
            if is_cex:
                cex_name = detected_name
                break

        if not cex_name:
            continue

        balance = get_usdt_balance(wallet_a)

        pair = {
            "wallet_a": wallet_a,
            "wallet_b": wallet_b,
            "cex_name": cex_name,
            "transfer_count": stats["count"],
            "total_amount": stats["total"],
            "cex_balance": balance,
        }

        pair_id, is_new = save_pair(
            wallet_a,
            wallet_b,
            cex_name,
            stats["count"],
            stats["total"],
            balance,
        )

        if is_new:
            log.info(
                "✅ Eligible pair found in final pass: #%s",
                pair_id,
            )
            announce_pair(pair_id, pair)

    refresh_active_slots()

    active_count = db.execute("""
        SELECT COUNT(*) AS count
        FROM pairs
        WHERE active = 1
    """).fetchone()["count"]

    saved_count = db.execute("""
        SELECT COUNT(*) AS count
        FROM pairs
    """).fetchone()["count"]

    SCAN_STATUS = "Complete"
    SCAN_PAGE = page_number
    SCAN_OFFSET = offset
    SCAN_PROCESSED = processed
    SCAN_CANDIDATE_PAIRS = len(pair_stats)
    SCAN_TWO_PLUS_PAIRS = sum(
        1 for stats in pair_stats.values()
        if stats["count"] >= 2
    )
    SCAN_CEX_QUALIFIED = sum(
        1 for stats in pair_stats.values()
        if stats["count"] >= 2
        and any(
            detect_cex(tag)[0]
            for tag in stats["tags"]
        )
    )
    SCAN_LAST_ACTIVITY = utc_now().strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    SCAN_COMPLETED_AT = SCAN_LAST_ACTIVITY

    log.info(
        "Historical scan completed. "
        "Total unique transfers processed: %s | "
        "Candidate pairs: %s | Saved: %s | Active: %s/%s",
        processed,
        len(pair_stats),
        saved_count,
        active_count,
        TARGET_PAIRS,
    )


# ============================================================
# ONGOING MONITORING
# ============================================================

def get_active_pairs():
    return db.execute("""
        SELECT *
        FROM pairs
        WHERE active = 1
        ORDER BY id
    """).fetchall()


def monitor_pair(pair):
    wallet_a = pair["wallet_a"]
    wallet_b = pair["wallet_b"]

    row = db.execute("""
        SELECT last_transaction_timestamp
        FROM monitored_wallets
        WHERE address = ?
    """, (wallet_b,)).fetchone()

    previous_timestamp = (
        int(row["last_transaction_timestamp"])
        if row
        else 0
    )

    try:
        history = get_wallet_history(wallet_b, limit=50)
        newest_timestamp = previous_timestamp

        for tx in history:
            timestamp = transaction_timestamp(tx)

            if timestamp > newest_timestamp:
                newest_timestamp = timestamp

            if timestamp <= previous_timestamp:
                continue

            if tx.get("to_address") != wallet_b:
                continue

            if tx.get("from_address") != wallet_a:
                continue

            amount = transfer_amount(tx)

            if amount <= 0:
                continue

            txid = transaction_id(tx) or "unknown"

            send_telegram(
                f"🔔 <b>New A → B USDT Transaction</b>\n\n"
                f"<b>Pair:</b> #{pair['id']}\n"
                f"<b>Network:</b> TRON\n"
                f"<b>Asset:</b> TRC-20 USDT\n\n"
                f"<b>Wallet A:</b>\n"
                f"<code>{telegram_escape(wallet_a)}</code>\n\n"
                f"<b>Wallet B:</b>\n"
                f"<code>{telegram_escape(wallet_b)}</code>\n\n"
                f"<b>Amount:</b> ${amount:,.2f}\n"
                f"<b>TX:</b> "
                f"<code>{telegram_escape(txid)}</code>"
            )

        db.execute("""
            UPDATE monitored_wallets
            SET last_checked = ?,
                last_transaction_timestamp = ?
            WHERE address = ?
        """, (
            utc_string(),
            newest_timestamp,
            wallet_b,
        ))
        db.commit()

    except Exception as exc:
        log.warning(
            "Monitoring failed for pair #%s: %s",
            pair["id"],
            exc,
        )


def monitor_wallets():
    global MONITOR_CYCLES, MONITOR_LAST_ACTIVITY

    pairs = get_active_pairs()

    for pair in pairs:
        if pair_is_excluded(pair["id"]):
            continue
        monitor_pair(pair)

    MONITOR_CYCLES += 1
    MONITOR_LAST_ACTIVITY = utc_now().strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def format_pair(pair):
    excluded = pair_is_excluded(pair["id"])
    status = "🚫 EXCLUDED" if excluded else "✅ ACTIVE"

    return (
        f"<b>#{pair['id']} — {status}</b>\n"
        f"CEX: {telegram_escape(pair['cex_name'])}\n"
        f"Network: TRON\n"
        f"Asset: TRC-20 USDT\n\n"
        f"Wallet A — TRON / TRC-20 USDT:\n"
        f"<code>{telegram_escape(pair['wallet_a'])}</code>\n"
        f"Wallet A USDT: ${pair['cex_balance']:,.2f}\n\n"
        f"Wallet B — TRON / TRC-20 USDT:\n"
        f"<code>{telegram_escape(pair['wallet_b'])}</code>\n\n"
        f"Historical A → B Transfers: {pair['transfer_count']}x\n"
        f"Volume: ${pair['total_amount']:,.2f}\n"
    )


def command_info():
    pairs = db.execute("""
        SELECT *
        FROM pairs
        ORDER BY id
    """).fetchall()

    if not pairs:
        send_telegram(
            "No eligible pairs have been discovered yet."
        )
        return

    active_pairs = [
        p for p in pairs
        if p["active"] == 1
        and not pair_is_excluded(p["id"])
    ]

    excluded_pairs = [
        p for p in pairs
        if pair_is_excluded(p["id"])
    ]

    message = (
        f"<b>TRON TRC-20 USDT Wallet Pairs</b>\n"
        f"Active: {len(active_pairs)}/{TARGET_PAIRS}\n\n"
    )

    for index, pair in enumerate(active_pairs, 1):
        message += (
            format_pair(pair).replace(
                f"#{pair['id']} —",
                f"Pair #{index} —",
                1,
            )
            + "\n"
        )

    if excluded_pairs:
        message += "<b>Excluded</b>\n\n"
        for pair in excluded_pairs:
            message += format_pair(pair) + "\n"

    send_telegram(message)


def command_exclude(parts):
    if len(parts) < 2:
        send_telegram("Usage: <code>/exclude 1</code>")
        return

    try:
        display_number = int(parts[1])
    except ValueError:
        send_telegram("Pair number must be a number.")
        return

    active_pairs = get_active_display_pairs()

    if display_number < 1 or display_number > len(active_pairs):
        send_telegram(
            f"Active Pair #{display_number} was not found. "
            f"Use <code>/info</code> to see the current pair numbers."
        )
        return

    pair = active_pairs[display_number - 1]
    pair_id = pair["id"]

    db.execute("""
        INSERT OR REPLACE INTO excluded_pairs(pair_id, excluded_at)
        VALUES (?, ?)
    """, (pair_id, utc_string()))
    db.commit()

    refresh_active_slots()

    send_telegram(
        f"🗑️ <b>Pair #{display_number} excluded.</b>\n\n"
        f"The next eligible candidate has been promoted into "
        f"the active slots."
    )


def get_historical_pair_transactions(
    wallet_a,
    wallet_b,
    weeks_back=WEEKS_BACK,
    max_pages=100,
):
    """
    Query the same global TRONSCAN TRC-20 USDT historical source
    used by the scanner, filtered to the selected A → B pair.

    This is intentionally NOT dependent on the receiver-wallet
    endpoint, because that endpoint can return an incomplete/different
    result set even when the global historical scan already identified
    the pair.
    """
    end_date = utc_now()
    start_date = end_date - timedelta(weeks=weeks_back)

    start_ms = int(start_date.timestamp() * 1000)
    end_ms = int(end_date.timestamp() * 1000)

    matches = []
    seen_ids = set()
    offset = 0
    page_size = 50

    for _ in range(max_pages):
        try:
            data = get_usdt_transfer_page(
                start_timestamp=start_ms,
                end_timestamp=end_ms,
                start=offset,
                limit=page_size,
            )
        except Exception as exc:
            log.warning(
                "Historical A → B lookup failed at offset %s: %s",
                offset,
                exc,
            )
            break

        transfers = data.get("token_transfers", [])

        if not transfers:
            break

        for tx in transfers:
            txid = transaction_id(tx)

            if txid and txid in seen_ids:
                continue

            if txid:
                seen_ids.add(txid)

            if tx.get("from_address") != wallet_a:
                continue

            if tx.get("to_address") != wallet_b:
                continue

            amount = transfer_amount(tx)
            if amount <= 0:
                continue

            timestamp = transaction_timestamp(tx)
            if timestamp:
                if timestamp < start_ms or timestamp > end_ms:
                    continue

            matches.append(tx)

        offset += page_size

        if len(transfers) < page_size:
            break

        time.sleep(0.15)

    matches.sort(
        key=transaction_timestamp,
        reverse=True,
    )
    return matches


def _parse_history_period(text_value):
    """Return (start_ts_ms, end_ts_ms, label) for a month or date/range."""
    value = text_value.strip()
    # Month formats: YYYY-MM, YYYY/MM, Month YYYY, Mon YYYY
    for fmt in ("%Y-%m", "%Y/%m", "%B %Y", "%b %Y"):
        try:
            dt = datetime.strptime(value, fmt)
            last_day = calendar.monthrange(dt.year, dt.month)[1]
            start = datetime(dt.year, dt.month, 1)
            end = datetime(dt.year, dt.month, last_day, 23, 59, 59)
            return int(start.timestamp() * 1000), int(end.timestamp() * 1000), start.strftime("%B %Y")
        except ValueError:
            pass

    # Single date: YYYY-MM-DD
    try:
        dt = datetime.strptime(value, "%Y-%m-%d")
        end = dt.replace(hour=23, minute=59, second=59)
        return int(dt.timestamp() * 1000), int(end.timestamp() * 1000), dt.strftime("%Y-%m-%d")
    except ValueError:
        pass

    # Date range: YYYY-MM-DD to YYYY-MM-DD
    parts = re.split(r"\s*(?:to|-)\s*", value, maxsplit=1)
    if len(parts) == 2:
        try:
            start = datetime.strptime(parts[0].strip(), "%Y-%m-%d")
            end = datetime.strptime(parts[1].strip(), "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            if end >= start:
                return int(start.timestamp() * 1000), int(end.timestamp() * 1000), f"{parts[0].strip()} to {parts[1].strip()}"
        except ValueError:
            pass
    return None


def _get_pair_for_history(chat_id, pair_number):
    pairs = get_active_display_pairs()
    try:
        idx = int(pair_number) - 1
    except (TypeError, ValueError):
        return None
    if idx < 0 or idx >= len(pairs):
        return None
    return pairs[idx]


def _fetch_historical_pair_transactions(wallet_a, wallet_b, start_ts_ms, end_ts_ms):
    """Fetch exact Wallet A -> Wallet B USDT transfers for an arbitrary period."""
    results = []
    seen = set()
    start = 0
    limit = 50

    while True:
        params = {
            "contract_address": USDT_CONTRACT,
            "fromAddress": wallet_a,
            "toAddress": wallet_b,
            "start_timestamp": start_ts_ms,
            "end_timestamp": end_ts_ms,
            "start": start,
            "limit": limit,
            "sort": "-timestamp",
        }

        try:
            response = requests.get(
                f"{TRONSCAN_API}/token_trc20/transfers",
                params=params,
                headers=tronscan_headers(),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            log.exception("Historical pair lookup failed: %s", exc)
            break

        transfers = data.get("token_transfers") or []
        if not transfers:
            break

        for tx in transfers:
            txid = transaction_id(tx)
            timestamp = transaction_timestamp(tx)
            amount = transfer_amount(tx)

            key = txid or f"{timestamp}:{amount}"
            if key in seen:
                continue
            seen.add(key)

            results.append({
                "txid": txid,
                "timestamp": timestamp,
                "amount": amount,
            })

        if len(transfers) < limit:
            break

        start += limit

        # TronScan documents a maximum start+limit of 10,000.
        if start + limit > 10000:
            break

    results.sort(key=lambda item: item["timestamp"], reverse=True)
    return results


def command_history(message):
    pairs = get_active_display_pairs()
    if not pairs:
        send_message(message.chat.id, "No active pairs are currently available.")
        return

    lines = [
        "📜 <b>TRON TRC-20 USDT HISTORY</b>",
        "",
        "Select a pair:",
    ]
    for i, pair in enumerate(pairs, 1):
        lines.append(f"<b>Pair {i}</b> — A: <code>{html.escape(pair['wallet_a'])}</code> → B: <code>{html.escape(pair['wallet_b'])}</code>")
    lines += [
        "",
        "Then send the period, for example:",
        "<code>August 2026</code>",
        "<code>July 2026</code>",
        "<code>2026-07-01 to 2026-07-31</code>",
    ]
    send_message(message.chat.id, "\n".join(lines))

    bot.register_next_step_handler(message, _history_pair_step)


def _history_pair_step(message):
    pair = _get_pair_for_history(message.chat.id, message.text.strip())
    if not pair:
        send_message(message.chat.id, "Invalid pair number. Run /history again and choose a valid pair.")
        return

    send_message(
        message.chat.id,
        f"Pair selected.\n\n"
        f"<b>Wallet A</b>: <code>{html.escape(pair['wallet_a'])}</code>\n"
        f"<b>Wallet B</b>: <code>{html.escape(pair['wallet_b'])}</code>\n\n"
        "Enter a month or date range, e.g. <code>August 2026</code>, "
        "<code>July 2026</code>, or <code>2026-07-01 to 2026-07-31</code>."
    )
    bot.register_next_step_handler(message, lambda msg: _history_period_step(msg, pair))


def _history_period_step(message, pair):
    parsed = _parse_history_period(message.text)
    if not parsed:
        send_message(
            message.chat.id,
            "I couldn't understand that period. Use <code>August 2026</code>, "
            "<code>July 2026</code>, <code>2026-07-15</code>, or "
            "<code>2026-07-01 to 2026-07-31</code>."
        )
        return

    start_ts, end_ts, label = parsed
    send_message(
        message.chat.id,
        f"🔎 Looking up <b>{html.escape(label)}</b> for this Wallet A → Wallet B pair..."
    )

    # First use the persistent transaction evidence captured by the scanner.
    saved = get_saved_historical_transactions(
        pair["wallet_a"],
        pair["wallet_b"],
        start_ts,
        end_ts,
    )

    # Then query TRONSCAN directly for the selected date range. This is
    # independent of WEEKS_BACK and uses exact sender/receiver filters.
    fetched = _fetch_historical_pair_transactions(
        pair["wallet_a"],
        pair["wallet_b"],
        start_ts,
        end_ts,
    )

    merged = {}
    for tx in saved + fetched:
        key = tx.get("txid") or f"{tx.get('timestamp', 0)}:{tx.get('amount', 0)}"
        if key not in merged:
            merged[key] = tx
        elif tx.get("qualifying"):
            merged[key].update(tx)

    transactions = sorted(
        merged.values(),
        key=lambda item: item.get("timestamp", 0),
        reverse=True,
    )

    # Preserve anything recovered from the date-specific blockchain lookup.
    for tx in fetched:
        save_historical_transaction(
            pair["wallet_a"],
            pair["wallet_b"],
            {
                "transaction_id": tx.get("txid"),
                "timestamp": tx.get("timestamp"),
                "quant": (tx.get("amount", 0) or 0) * 1_000_000,
            },
            cex_name=pair.get("cex_name"),
            source="history_lookup",
        )

    if not transactions:
        send_message(
            message.chat.id,
            f"📜 <b>History — {html.escape(label)}</b>\n\n"
            "No A → B TRC-20 USDT transfers were found for this period.\n\n"
            f"<b>Wallet A:</b> <code>{html.escape(pair['wallet_a'])}</code>\n"
            f"<b>Wallet B:</b> <code>{html.escape(pair['wallet_b'])}</code>\n"
            "<b>Network:</b> TRON\n"
            "<b>Asset:</b> TRC-20 USDT"
        )
        return

    total = sum(float(tx.get("amount", 0) or 0) for tx in transactions)

    lines = [
        f"📜 <b>TRON TRC-20 USDT HISTORY — {html.escape(label)}</b>",
        "",
        f"<b>Wallet A:</b> <code>{html.escape(pair['wallet_a'])}</code>",
        f"<b>Wallet B:</b> <code>{html.escape(pair['wallet_b'])}</code>",
        "<b>Network:</b> TRON",
        "<b>Asset:</b> TRC-20 USDT",
        f"<b>Transfers:</b> {len(transactions)}",
        f"<b>Total:</b> {total:,.6f} USDT",
        "",
    ]

    for i, tx in enumerate(transactions, 1):
        ts = tx.get("timestamp", 0) or 0
        when = (
            datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
            if ts else "Unknown"
        )
        lines.append(f"<b>#{i}</b> {when}")
        lines.append(
            f"Amount: <b>{float(tx.get('amount', 0) or 0):,.6f} USDT</b>"
        )

        if tx.get("qualifying"):
            lines.append("⭐ <b>Qualifying transaction</b>")

        if tx.get("cex_name"):
            lines.append(f"CEX: {html.escape(str(tx['cex_name']))}")

        if tx.get("txid"):
            lines.append(f"TX: <code>{html.escape(tx['txid'])}</code>")

        lines.append("")

    output = "\n".join(lines)

    # Telegram message-size protection.
    for offset in range(0, len(output), 3800):
        send_message(message.chat.id, output[offset:offset + 3800])


def command_cost():
    active = len([
        p
        for p in get_active_pairs()
        if not pair_is_excluded(p["id"])
    ])

    estimated_trx = active * 2.2

    send_telegram(
        f"💰 <b>Informational Cost Estimate</b>\n\n"
        f"Active pairs: {active}\n"
        f"Estimated network cost: ~{estimated_trx:.2f} TRX\n\n"
        f"No transactions are executed by this bot."
    )


def command_status():
    saved = db.execute("""
        SELECT COUNT(*) AS count
        FROM pairs
    """).fetchone()["count"]

    active = db.execute("""
        SELECT COUNT(*) AS count
        FROM pairs
        WHERE active = 1
    """).fetchone()["count"]

    wallets = db.execute("""
        SELECT COUNT(*) AS count
        FROM monitored_wallets
    """).fetchone()["count"]

    excluded = db.execute("""
        SELECT COUNT(*) AS count
        FROM excluded_pairs
    """).fetchone()["count"]

    send_telegram(
        f"🤖 <b>Monitor Status</b>\n\n"
        f"<b>Network / Asset</b>\n"
        f"TRON / TRC-20 USDT\n\n"
        f"<b>Historical Scanner</b>\n"
        f"Status: {SCAN_STATUS}\n"
        f"Transfers processed: {SCAN_PROCESSED:,}\n"
        f"Candidate A→B pairs: {SCAN_CANDIDATE_PAIRS:,}\n"
        f"Pairs with 2+ transfers: {SCAN_TWO_PLUS_PAIRS:,}\n"
        f"CEX-qualified candidates: {SCAN_CEX_QUALIFIED:,}\n"
        f"Scan page: {SCAN_PAGE:,}\n"
        f"Current offset: {SCAN_OFFSET:,}\n"
        f"Last scan activity: {SCAN_LAST_ACTIVITY}\n\n"
        f"<b>Saved / Active</b>\n"
        f"Saved candidate pairs: {saved}\n"
        f"Active monitoring slots: {active}/{TARGET_PAIRS}\n"
        f"Monitored wallets: {wallets}\n"
        f"Excluded pairs: {excluded}\n\n"
        f"<b>Ongoing Monitor</b>\n"
        f"Monitoring cycles completed: {MONITOR_CYCLES:,}\n"
        f"Last monitoring activity: {MONITOR_LAST_ACTIVITY}\n\n"
        f"Lookback: {WEEKS_BACK} week(s)\n"
        f"Minimum transfer: ${MIN_TRANSFER_USD:,.2f}\n"
        f"Target pairs: {TARGET_PAIRS}"
    )


def command_final(message):
    refresh_active_slots()
    pairs = get_active_display_pairs()

    lines = [
        "📋 <b>FINAL ACTIVE PAIRS — TRON TRC-20 USDT</b>",
        "",
        f"<b>Active pair count:</b> {len(pairs)}",
        "<b>Network:</b> TRON",
        "<b>Asset:</b> TRC-20 USDT",
        "",
    ]

    if not pairs:
        lines.append("No active pairs remain after exclusions.")
    else:
        for i, pair in enumerate(pairs, 1):
            lines.extend([
                f"<b>Pair {i}</b>",
                f"Wallet A: <code>{html.escape(pair['wallet_a'])}</code>",
                f"Wallet B: <code>{html.escape(pair['wallet_b'])}</code>",
                f"CEX: {html.escape(pair.get('cex_name') or 'Unknown')}",
                f"Transfers: {pair.get('transfer_count', 0)}",
                f"Volume: {pair.get('total_amount', 0):,.2f} USDT",
                "",
            ])

    # Also persist the current active wallet identities so the final report
    # always reflects the post-exclusion set.
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM monitored_wallets")
        now = int(time.time())
        for pair in pairs:
            cur.execute(
                """INSERT OR REPLACE INTO monitored_wallets
                   (address, wallet_type, pair_id, first_seen, last_checked, last_transaction_timestamp)
                   VALUES (?, ?, ?, COALESCE((SELECT first_seen FROM monitored_wallets WHERE address=?), ?), ?, 0)""",
                (pair["wallet_a"], "A — TRON / TRC-20 USDT", pair["id"], pair["wallet_a"], now, now)
            )
            cur.execute(
                """INSERT OR REPLACE INTO monitored_wallets
                   (address, wallet_type, pair_id, first_seen, last_checked, last_transaction_timestamp)
                   VALUES (?, ?, ?, COALESCE((SELECT first_seen FROM monitored_wallets WHERE address=?), ?), ?, 0)""",
                (pair["wallet_b"], "B — TRON / TRC-20 USDT", pair["id"], pair["wallet_b"], now, now)
            )
        conn.commit()
        conn.close()
    except Exception:
        logging.exception("Could not refresh monitored wallet identities in /final")

    send_message(message.chat.id, "\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Monitor stopped by user.")
    except Exception as exc:
        log.exception("Fatal error: %s", exc)
    finally:
        db.close()

import os
import time
import sqlite3
import logging
import html
import requests
import threading

from collections import defaultdict
from datetime import datetime, timedelta, timezone

print("🚀 Starting Persistent TRON Wallet Monitor...")

TARGET_PAIRS = int(os.getenv("TARGET_PAIRS", "50"))
MIN_TRANSFER_USD = float(os.getenv("MIN_TRANSFER_USD", "50"))
WEEKS_BACK = int(os.getenv("WEEKS_BACK", "2"))
START_DATE = os.getenv("START_DATE", "").strip()  # YYYY-MM-DD; if blank, WEEKS_BACK is used
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("tron-monitor")

db = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row

db.execute("""CREATE TABLE IF NOT EXISTS pairs (
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
)""")
db.execute("""CREATE TABLE IF NOT EXISTS excluded_pairs (
    pair_id INTEGER PRIMARY KEY,
    excluded_at TEXT NOT NULL
)""")
db.execute("""CREATE TABLE IF NOT EXISTS monitored_wallets (
    address TEXT PRIMARY KEY,
    wallet_type TEXT,
    pair_id INTEGER,
    first_seen TEXT,
    last_checked TEXT,
    last_transaction_timestamp INTEGER DEFAULT 0
)""")
db.execute("CREATE TABLE IF NOT EXISTS scan_state (key TEXT PRIMARY KEY, value TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS telegram_state (key TEXT PRIMARY KEY, value TEXT)")
# Per-pair monitoring state. This prevents historical transactions from being
# re-alerted and keeps each A -> B pair independent, even when Wallet B is
# shared by multiple pairs.
db.execute("""CREATE TABLE IF NOT EXISTS monitor_state (
    pair_id INTEGER PRIMARY KEY,
    baseline_initialized INTEGER DEFAULT 0,
    last_transaction_timestamp INTEGER DEFAULT 0,
    last_checked TEXT
)""")
db.execute("""CREATE TABLE IF NOT EXISTS monitor_seen_transactions (
    pair_id INTEGER NOT NULL,
    txid TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY(pair_id, txid)
)""")
db.commit()


def utc_now():
    return datetime.now(timezone.utc)


def utc_string():
    return utc_now().isoformat()


def telegram_escape(value):
    return html.escape(str(value or ""))


def transaction_id(tx):
    return tx.get("hash") or tx.get("transaction_id") or tx.get("txID") or tx.get("transactionHash") or ""


def send_telegram(message):
    if not TELEGRAM_URL or not CHAT_ID:
        log.warning("Telegram is not configured.")
        return False
    try:
        response = requests.post(
            f"{TELEGRAM_URL}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
        if response.ok:
            return True
        log.warning("Telegram error %s: %s", response.status_code, response.text[:300])
    except requests.RequestException as exc:
        log.warning("Telegram request failed: %s", exc)
    return False


def get_telegram_offset():
    row = db.execute("SELECT value FROM telegram_state WHERE key = 'offset'").fetchone()
    if not row:
        return 0
    try:
        return int(row["value"])
    except (ValueError, TypeError):
        return 0


def set_telegram_offset(offset):
    db.execute("""INSERT INTO telegram_state(key, value) VALUES('offset', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value""", (str(offset),))
    db.commit()


def tronscan_headers():
    return {"TRON-PRO-API-KEY": TRONSCAN_API_KEY} if TRONSCAN_API_KEY else {}


def trongrid_headers():
    return {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}


def http_get(url, params=None, headers=None, timeout=30, attempts=3):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, params=params, headers=headers or {}, timeout=timeout)
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
    raise RuntimeError(f"API request failed after {attempts} attempts: {last_error}")


def get_usdt_transfer_page(start_timestamp=None, end_timestamp=None, start=0, limit=50):
    params = {"start": start, "limit": limit, "contract_address": USDT_CONTRACT, "sort": "-timestamp"}
    if start_timestamp is not None:
        params["start_timestamp"] = start_timestamp
    if end_timestamp is not None:
        params["end_timestamp"] = end_timestamp
    return http_get(f"{TRONSCAN_URL}/token_trc20/transfers", params=params, headers=tronscan_headers(), timeout=30)


def get_usdt_transfers(start_timestamp=None, end_timestamp=None, start=0, limit=50):
    return get_usdt_transfer_page(start_timestamp, end_timestamp, start, limit).get("token_transfers", [])


def get_wallet_history(address, limit=50, start=0, start_timestamp=None, end_timestamp=None):
    """Get TRC-20 USDT transfers involving one wallet.

    TronScan's current token_trc20/transfers endpoint uses
    ``relatedAddress`` for an address-wide lookup.
    """
    try:
        params = {
            "relatedAddress": address,
            "limit": min(int(limit), 50),
            "start": int(start),
            "sort": "-timestamp",
            "contract_address": USDT_CONTRACT,
        }
        if start_timestamp is not None:
            params["start_timestamp"] = start_timestamp
        if end_timestamp is not None:
            params["end_timestamp"] = end_timestamp
        data = http_get(
            f"{TRONSCAN_URL}/token_trc20/transfers",
            params=params,
            headers=tronscan_headers(), timeout=30,
        )
        return data.get("token_transfers", [])
    except Exception as exc:
        log.warning("History lookup failed for %s: %s", address, exc)
        return []


def get_pair_history(wallet_a, wallet_b, max_pages=200, page_size=50):
    """Return actual TRC-20 USDT transfers directly from A to B.

    Use TronScan's exact fromAddress + toAddress filters instead of fetching
    a wallet's recent transactions and hoping the pair appears in them.
    This avoids the previous false 'No matching transfers' result.
    """
    try:
        start_date, end_date = get_scan_window()
        start_ms = int(start_date.timestamp() * 1000)
        end_ms = int(end_date.timestamp() * 1000)
    except Exception:
        start_ms = end_ms = None

    found = {}
    for page in range(max_pages):
        params = {
            "fromAddress": wallet_a,
            "toAddress": wallet_b,
            "contract_address": USDT_CONTRACT,
            "start": page * page_size,
            "limit": min(page_size, 50),
            "sort": "-timestamp",
        }
        if start_ms is not None:
            params["start_timestamp"] = start_ms
            params["end_timestamp"] = end_ms
        try:
            data = http_get(
                f"{TRONSCAN_URL}/token_trc20/transfers",
                params=params,
                headers=tronscan_headers(), timeout=30,
            )
        except Exception as exc:
            log.warning("Pair history lookup failed for %s → %s: %s", wallet_a, wallet_b, exc)
            break

        txs = data.get("token_transfers", [])
        if not txs:
            break
        for tx in txs:
            # Keep the exact direction check as a safety guard.
            if tx.get("from_address") != wallet_a or tx.get("to_address") != wallet_b:
                continue
            txid = transaction_id(tx)
            key = txid or f"{transaction_timestamp(tx)}:{transfer_amount(tx)}"
            found[key] = tx
        if len(txs) < page_size:
            break
        # TronScan documents a maximum pagination range of 10,000 records.
        if (page + 1) * page_size >= 10000:
            break
        time.sleep(0.1)

    return sorted(found.values(), key=transaction_timestamp, reverse=True)


def get_usdt_balance(address):
    """Best-effort current TRC-20 USDT balance.

    Balance lookup is informational only. A TronGrid 404/rate-limit/API
    failure must never disqualify an otherwise valid historical pair.
    Returns None when the balance cannot be obtained.
    """
    try:
        data = http_get(
            f"{TRONGRID_URL}/v1/accounts/{address}/trc20",
            params={"only_confirmed": "true", "limit": 200},
            headers=trongrid_headers(), timeout=20, attempts=2,
        )
        for token in data.get("data", []):
            info = token.get("token_info", {})
            if info.get("address") != USDT_CONTRACT:
                continue
            raw = int(token.get("balance", 0))
            decimals = int(info.get("decimals", 6) or 6)
            return raw / (10 ** decimals)
        return 0.0
    except Exception as exc:
        log.warning("USDT balance unavailable for %s; continuing without balance (%s)", address, exc)
        return None


def transfer_amount(tx):
    try:
        if tx.get("quant") is not None:
            return int(tx.get("quant", 0)) / 1_000_000
        return float(tx.get("amount", 0) or 0)
    except (ValueError, TypeError):
        return 0.0


def transaction_timestamp(tx):
    try:
        return int(tx.get("block_ts") or tx.get("timestamp") or 0)
    except (ValueError, TypeError):
        return 0


def get_tag_name(tx):
    tag = tx.get("from_address_tag") or tx.get("from_address_tag_name") or tx.get("fromAddressTag")
    if isinstance(tag, dict):
        return tag.get("from_address_tag") or tag.get("name") or tag.get("tag") or ""
    return tag if isinstance(tag, str) else ""


def detect_cex(tag):
    normalized = str(tag or "").lower()
    for keyword in CEX_KEYWORDS:
        if keyword in normalized:
            return True, str(tag)
    return False, str(tag or "Unknown")


def pair_is_excluded(pair_id):
    row = db.execute("SELECT 1 FROM excluded_pairs WHERE pair_id = ?", (pair_id,)).fetchone()
    return row is not None


def save_pair(wallet_a, wallet_b, cex_name, transfer_count, total_amount, cex_balance):
    now = utc_string()
    existing = db.execute("SELECT * FROM pairs WHERE wallet_a = ? AND wallet_b = ?", (wallet_a, wallet_b)).fetchone()
    if existing:
        db.execute("""UPDATE pairs SET transfer_count=?, total_amount=?, cex_balance=?, cex_name=?, last_seen=? WHERE id=?""",
                   (transfer_count, total_amount, cex_balance, cex_name, now, existing["id"]))
        pair_id, is_new = existing["id"], False
    else:
        cursor = db.execute("""INSERT INTO pairs
            (wallet_a,wallet_b,cex_name,transfer_count,total_amount,cex_balance,first_seen,last_seen,qualified_at,active,alerted)
            VALUES (?,?,?,?,?,?,?,?,?,0,0)""",
            (wallet_a, wallet_b, cex_name, transfer_count, total_amount, cex_balance, now, now, now))
        pair_id, is_new = cursor.lastrowid, True
    for address, wallet_type in ((wallet_a, "A — TRON TRC-20 USDT"), (wallet_b, "B — TRON TRC-20 USDT")):
        db.execute("""INSERT INTO monitored_wallets(address,wallet_type,pair_id,first_seen)
            VALUES(?,?,?,?) ON CONFLICT(address) DO NOTHING""", (address, wallet_type, pair_id, now))
    db.commit()
    return pair_id, is_new


def refresh_active_slots():
    db.execute("UPDATE pairs SET active = 0")
    rows = db.execute("""SELECT p.id FROM pairs p LEFT JOIN excluded_pairs e ON e.pair_id=p.id
        WHERE e.pair_id IS NULL ORDER BY p.id LIMIT ?""", (TARGET_PAIRS,)).fetchall()
    for row in rows:
        db.execute("UPDATE pairs SET active=1 WHERE id=?", (row["id"],))
    db.commit()


def get_display_pairs():
    """Return every saved pair that has NOT been excluded, in discovery order.

    Display numbers are deliberately separate from the database IDs. This means
    Pair 1 is always the first currently eligible pair, Pair 2 the second, etc.
    A pair does not disappear from this list merely because it is outside the
    current monitoring-slot limit.
    """
    return db.execute(
        """SELECT p.* FROM pairs p
           LEFT JOIN excluded_pairs e ON e.pair_id = p.id
           WHERE e.pair_id IS NULL
           ORDER BY p.id"""
    ).fetchall()


def get_active_display_pairs():
    """Backward-compatible alias: display all non-excluded saved pairs."""
    return get_display_pairs()


def announce_pair(pair_id, pair):
    balance = pair.get("cex_balance")
    if balance is None:
        status = "ℹ️"
        balance_text = "Unavailable (balance API did not respond)"
    else:
        status = "✅" if balance >= 500 else "⚠️"
        balance_text = f"${balance:,.2f}"
    send_telegram(
        f"{status} <b>Eligible Pair #{pair_id}</b>\n\n"
        f"<b>Wallet A — TRON TRC-20 USDT:</b>\n<code>{telegram_escape(pair['wallet_a'])}</code>\n\n"
        f"<b>CEX Tag:</b> {telegram_escape(pair['cex_name'])}\n"
        f"<b>Current USDT Balance:</b> {balance_text}\n\n"
        f"<b>Wallet B — TRON TRC-20 USDT:</b>\n<code>{telegram_escape(pair['wallet_b'])}</code>\n\n"
        f"<b>Historical Transfers:</b> {pair['transfer_count']}x\n"
        f"<b>Total Volume:</b> ${pair['total_amount']:,.2f}\n\n"
        f"Historical qualification window: {scan_window_text()}.\n"
        f"Monitoring will continue after qualification."
    )


def get_scan_window():
    """Return the historical scan start/end datetimes.

    START_DATE takes priority when supplied as YYYY-MM-DD. Otherwise the
    existing WEEKS_BACK setting is used. The end of the window is always now.
    """
    end_date = utc_now()
    if START_DATE:
        try:
            start_date = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise ValueError(f"Invalid START_DATE '{START_DATE}'. Use YYYY-MM-DD, e.g. 2026-09-01")
        if start_date > end_date:
            raise ValueError("START_DATE cannot be in the future.")
    else:
        start_date = end_date - timedelta(weeks=WEEKS_BACK)
    return start_date, end_date


def scan_window_text():
    try:
        start_date, end_date = get_scan_window()
        return f"{start_date.strftime('%Y-%m-%d')} → {end_date.strftime('%Y-%m-%d')} UTC"
    except Exception:
        return f"Invalid START_DATE: {START_DATE}"


def log_scan_progress(force=False):
    """Print a compact live scanner snapshot to GitHub Actions logs."""
    log.info(
        "🔎 HISTORICAL SCAN | page=%s offset=%s processed=%s | "
        "candidate_pairs=%s | 2+_transfers=%s | CEX_qualified=%s | window=%s",
        SCAN_PAGE, SCAN_OFFSET, SCAN_PROCESSED, SCAN_CANDIDATE_PAIRS,
        SCAN_TWO_PLUS_PAIRS, SCAN_CEX_QUALIFIED, scan_window_text(),
    )


def historical_scan():
    start_date, end_date = get_scan_window()
    start_ms, end_ms = int(start_date.timestamp()*1000), int(end_date.timestamp()*1000)
    global SCAN_STATUS, SCAN_PAGE, SCAN_OFFSET, SCAN_PROCESSED, SCAN_CANDIDATE_PAIRS
    global SCAN_TWO_PLUS_PAIRS, SCAN_CEX_QUALIFIED, SCAN_LAST_ACTIVITY, SCAN_STARTED_AT, SCAN_COMPLETED_AT
    SCAN_STATUS, SCAN_PAGE, SCAN_OFFSET, SCAN_PROCESSED = "Running", 0, 0, 0
    SCAN_CANDIDATE_PAIRS = SCAN_TWO_PLUS_PAIRS = SCAN_CEX_QUALIFIED = 0
    SCAN_LAST_ACTIVITY = SCAN_STARTED_AT = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
    SCAN_COMPLETED_AT = "Never"
    log.info("🚀 HISTORICAL SCAN STARTED")
    log.info("📅 Scan window: %s", scan_window_text())
    log.info("💰 Minimum transfer: $%,.2f | 🎯 Target pairs: %s", MIN_TRANSFER_USD, TARGET_PAIRS)
    log.info("📡 Searching TRON TRC-20 USDT transfers...")
    pair_stats = defaultdict(lambda: {"count":0, "total":0.0, "tags":set(), "txids":set()})
    seen_global_txids = set()
    offset, page_size, processed, duplicate_pages, max_duplicate_pages, max_pages = 0, 50, 0, 0, 5, 500
    page_number = 0
    while True:
        page_number += 1
        if page_number > max_pages:
            log.warning("Historical scan reached safety limit of %s pages.", max_pages); break
        try:
            data = get_usdt_transfer_page(start_timestamp=start_ms, end_timestamp=end_ms, start=offset, limit=page_size)
        except Exception as exc:
            log.error("Historical scan request failed: %s", exc); time.sleep(10); continue
        transfers = data.get("token_transfers", [])
        if not transfers: break
        new_transactions = 0
        for tx in transfers:
            txid = transaction_id(tx)
            if txid and txid in seen_global_txids: continue
            if txid: seen_global_txids.add(txid)
            new_transactions += 1
            wallet_a, wallet_b = tx.get("from_address"), tx.get("to_address")
            if not wallet_a or not wallet_b: continue
            amount = transfer_amount(tx)
            if amount < MIN_TRANSFER_USD: continue
            timestamp = transaction_timestamp(tx)
            if timestamp and not (start_ms <= timestamp <= end_ms): continue
            stats = pair_stats[(wallet_a, wallet_b)]
            if txid and txid in stats["txids"]: continue
            if txid: stats["txids"].add(txid)
            stats["count"] += 1; stats["total"] += amount
            tag = get_tag_name(tx)
            if tag: stats["tags"].add(tag)
        if new_transactions == 0:
            duplicate_pages += 1
            if duplicate_pages >= max_duplicate_pages: break
        else:
            duplicate_pages = 0; processed += new_transactions
        SCAN_PAGE, SCAN_OFFSET, SCAN_PROCESSED = page_number, offset, processed
        SCAN_CANDIDATE_PAIRS = len(pair_stats)
        SCAN_TWO_PLUS_PAIRS = sum(1 for s in pair_stats.values() if s["count"] >= 2)
        SCAN_CEX_QUALIFIED = sum(1 for s in pair_stats.values() if s["count"] >= 2 and any(detect_cex(t)[0] for t in s["tags"]))
        SCAN_LAST_ACTIVITY = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
        log_scan_progress()
        for (wallet_a, wallet_b), stats in list(pair_stats.items()):
            if stats["count"] < 2: continue
            if db.execute("SELECT id FROM pairs WHERE wallet_a=? AND wallet_b=?", (wallet_a,wallet_b)).fetchone(): continue
            cex_name = next((detect_cex(t)[1] for t in stats["tags"] if detect_cex(t)[0]), "")
            if not cex_name: continue
            balance = get_usdt_balance(wallet_a)
            pair = {"wallet_a":wallet_a,"wallet_b":wallet_b,"cex_name":cex_name,"transfer_count":stats["count"],"total_amount":stats["total"],"cex_balance":balance}
            pair_id, is_new = save_pair(wallet_a,wallet_b,cex_name,stats["count"],stats["total"],balance)
            if is_new: announce_pair(pair_id,pair)
        offset += page_size
        if len(transfers) < page_size: break
        time.sleep(0.25)
    refresh_active_slots()
    saved = db.execute("SELECT COUNT(*) AS count FROM pairs").fetchone()["count"]
    active = db.execute("SELECT COUNT(*) AS count FROM pairs WHERE active=1").fetchone()["count"]
    SCAN_STATUS = "Complete"; SCAN_PAGE = page_number; SCAN_OFFSET = offset; SCAN_PROCESSED = processed
    SCAN_CANDIDATE_PAIRS = len(pair_stats)
    SCAN_TWO_PLUS_PAIRS = sum(1 for s in pair_stats.values() if s["count"] >= 2)
    SCAN_CEX_QUALIFIED = sum(1 for s in pair_stats.values() if s["count"] >= 2 and any(detect_cex(t)[0] for t in s["tags"]))
    SCAN_LAST_ACTIVITY = SCAN_COMPLETED_AT = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
    log.info("✅ HISTORICAL SCAN COMPLETE")
    log.info("📊 Processed=%s | Candidate pairs=%s | 2+ transfers=%s | CEX-qualified=%s", processed, len(pair_stats), SCAN_TWO_PLUS_PAIRS, SCAN_CEX_QUALIFIED)
    log.info("💾 Saved=%s | Active=%s/%s", saved, active, TARGET_PAIRS)


def get_active_pairs():
    return db.execute("SELECT * FROM pairs WHERE active=1 ORDER BY id").fetchall()


def get_display_number(pair_id):
    rows = get_display_pairs()
    for index, row in enumerate(rows, start=1):
        if row["id"] == pair_id:
            return index
    return pair_id


def initialize_pair_monitor_baseline(pair):
    """Seed the pair's current transaction hashes without sending alerts.

    This is crucial on startup: a newly qualified pair already has historical
    A -> B transactions. Those must not be reported as "new" just because the
    monitoring loop is seeing them for the first time.
    """
    pair_id = pair["id"]
    state = db.execute("SELECT baseline_initialized FROM monitor_state WHERE pair_id=?", (pair_id,)).fetchone()
    if state and int(state["baseline_initialized"] or 0) == 1:
        return

    wallet_a, wallet_b = pair["wallet_a"], pair["wallet_b"]
    try:
        history = get_wallet_history(wallet_b, limit=50)
        newest = 0
        for tx in history:
            if tx.get("from_address") != wallet_a or tx.get("to_address") != wallet_b:
                continue
            timestamp = transaction_timestamp(tx)
            newest = max(newest, timestamp)
            txid = transaction_id(tx)
            if txid:
                db.execute("INSERT OR IGNORE INTO monitor_seen_transactions(pair_id,txid,seen_at) VALUES(?,?,?)",
                            (pair_id, txid, utc_string()))
        db.execute("""INSERT INTO monitor_state(pair_id,baseline_initialized,last_transaction_timestamp,last_checked)
            VALUES(?,?,?,?)
            ON CONFLICT(pair_id) DO UPDATE SET baseline_initialized=1,
                last_transaction_timestamp=excluded.last_transaction_timestamp,
                last_checked=excluded.last_checked""",
                    (pair_id, 1, newest, utc_string()))
        db.commit()
        log.info("📌 Pair #%s monitoring baseline initialized at %s (no historical alerts sent).",
                 get_display_number(pair_id), newest)
    except Exception as exc:
        log.warning("Could not initialize monitoring baseline for pair #%s: %s", pair_id, exc)


def monitor_pair(pair):
    pair_id = pair["id"]
    wallet_a, wallet_b = pair["wallet_a"], pair["wallet_b"]
    initialize_pair_monitor_baseline(pair)

    state = db.execute("SELECT last_transaction_timestamp FROM monitor_state WHERE pair_id=?", (pair_id,)).fetchone()
    previous_timestamp = int(state["last_transaction_timestamp"] or 0) if state else 0
    try:
        history = get_wallet_history(wallet_b, limit=50)
        newest_timestamp = previous_timestamp
        new_alerts = []
        for tx in history:
            timestamp = transaction_timestamp(tx)
            if timestamp <= 0:
                continue
            if tx.get("from_address") != wallet_a or tx.get("to_address") != wallet_b:
                continue
            amount = transfer_amount(tx)
            if amount <= 0:
                continue
            txid = transaction_id(tx)
            newest_timestamp = max(newest_timestamp, timestamp)
            if txid:
                seen = db.execute("SELECT 1 FROM monitor_seen_transactions WHERE pair_id=? AND txid=?", (pair_id, txid)).fetchone()
                if seen:
                    continue
                db.execute("INSERT INTO monitor_seen_transactions(pair_id,txid,seen_at) VALUES(?,?,?)", (pair_id, txid, utc_string()))
                new_alerts.append((timestamp, amount, txid))
            else:
                # Without a transaction hash, timestamp is the safest fallback.
                if timestamp <= previous_timestamp:
                    continue
                new_alerts.append((timestamp, amount, "unknown"))

        # Alert oldest-to-newest so multiple new transfers arrive in order.
        for timestamp, amount, txid in sorted(new_alerts, key=lambda x: x[0]):
            send_telegram(
                f"🔔 <b>New A → B TRON TRC-20 USDT Transaction</b>\n\n"
                f"<b>Pair:</b> #{get_display_number(pair_id)}\n"
                f"<b>Wallet A:</b> <code>{telegram_escape(wallet_a)}</code>\n\n"
                f"<b>Wallet B:</b> <code>{telegram_escape(wallet_b)}</code>\n\n"
                f"<b>Amount:</b> ${amount:,.2f} USDT\n"
                f"<b>Transaction time:</b> {datetime.fromtimestamp(timestamp / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') if timestamp else 'Unknown'}\n"
                f"<b>Detected by bot:</b> {utc_now().strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                f"<b>TX:</b> <code>{telegram_escape(txid)}</code>"
            )

        db.execute("""UPDATE monitor_state SET last_checked=?, last_transaction_timestamp=? WHERE pair_id=?""",
                   (utc_string(), newest_timestamp, pair_id))
        db.execute("UPDATE monitored_wallets SET last_checked=?, last_transaction_timestamp=? WHERE address=?",
                   (utc_string(), newest_timestamp, wallet_b))
        db.commit()
    except Exception as exc:
        log.warning("Monitoring failed for pair #%s: %s", get_display_number(pair_id), exc)


def monitor_wallets():
    global MONITOR_CYCLES, MONITOR_LAST_ACTIVITY
    for pair in get_active_pairs():
        if not pair_is_excluded(pair["id"]): monitor_pair(pair)
    MONITOR_CYCLES += 1
    MONITOR_LAST_ACTIVITY = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")


def format_pair(pair, display_number=None):
    excluded = pair_is_excluded(pair["id"])
    status = "🚫 EXCLUDED" if excluded else "✅ ACTIVE"
    number = display_number if display_number is not None else pair["id"]
    return (
        f"<b>Pair #{number} — {status}</b>\n"
        f"CEX: {telegram_escape(pair['cex_name'])}\n"
        f"Wallet A — TRON TRC-20 USDT:\n<code>{telegram_escape(pair['wallet_a'])}</code>\n"
        f"Wallet A USDT: ${pair['cex_balance']:,.2f}\n\n"
        f"Wallet B — TRON TRC-20 USDT:\n<code>{telegram_escape(pair['wallet_b'])}</code>\n\n"
        f"Transfers: {pair['transfer_count']}x\nVolume: ${pair['total_amount']:,.2f}\n"
    )


def command_info():
    all_pairs = db.execute("SELECT * FROM pairs ORDER BY id").fetchall()
    if not all_pairs:
        send_telegram("No eligible pairs have been discovered yet."); return
    eligible_pairs = get_display_pairs()
    excluded_pairs = [p for p in all_pairs if pair_is_excluded(p["id"])]
    monitored_count = sum(1 for p in eligible_pairs if p["active"] == 1)
    message = (
        f"<b>Wallet Pairs — TRON TRC-20 USDT</b>\n"
        f"Eligible (not excluded): {len(eligible_pairs)}\n"
        f"Currently monitored: {monitored_count}/{TARGET_PAIRS}\n\n"
    )
    for index, pair in enumerate(eligible_pairs, 1):
        message += format_pair(pair, index) + "\n"
    if excluded_pairs:
        message += "<b>Excluded</b>\n\n"
        for pair in excluded_pairs:
            message += format_pair(pair) + "\n"
    send_telegram(message)


def command_exclude(parts):
    if len(parts) < 2: send_telegram("Usage: <code>/exclude 1</code>"); return
    try: display_number = int(parts[1])
    except ValueError: send_telegram("Pair number must be a number."); return
    eligible_pairs = get_display_pairs()
    if not 1 <= display_number <= len(eligible_pairs):
        send_telegram(f"Pair #{display_number} was not found. Use <code>/info</code> or <code>/final</code> to see the current pair numbers."); return
    pair = eligible_pairs[display_number-1]
    db.execute("INSERT OR REPLACE INTO excluded_pairs(pair_id,excluded_at) VALUES(?,?)", (pair["id"],utc_string()))
    db.commit(); refresh_active_slots()
    send_telegram(f"🗑️ <b>Pair #{display_number} excluded.</b>\n\nThe remaining eligible pairs have been renumbered from Pair 1, and the next eligible candidate can be promoted into the monitoring slots.")


HISTORY_PAGE_SIZE = 8  # Keeps each Telegram history message safely below the limit.


def command_history(parts):
    """Show a pair's A → B history in Telegram-sized pages.

    Usage:
      /history 5       -> page 1
      /history 5 2     -> page 2

    Pair numbers always refer to the current non-excluded display list, the
    same list used by /final and /exclude. No pair is deleted by paging.
    """
    if len(parts) < 2:
        send_telegram(
            "Usage: <code>/history 1</code> or <code>/history 1 2</code>\n\n"
            "First number = Pair number. Second number = history page.\n"
            "Example: <code>/history 5 2</code> shows page 2 for Pair #5."
        )
        return

    try:
        display_number = int(parts[1])
        page = int(parts[2]) if len(parts) >= 3 else 1
    except ValueError:
        send_telegram("Pair number and page number must be numbers.")
        return

    if display_number < 1:
        send_telegram("Pair number must be 1 or higher.")
        return
    if page < 1:
        send_telegram("History page must be 1 or higher.")
        return

    eligible_pairs = get_display_pairs()
    if not 1 <= display_number <= len(eligible_pairs):
        send_telegram(
            f"Pair #{display_number} was not found. Use <code>/final</code> to see "
            "the current pair numbers."
        )
        return

    pair = eligible_pairs[display_number - 1]
    wallet_a, wallet_b = pair["wallet_a"], pair["wallet_b"]

    # Retrieve the full available A → B history for the selected scan window.
    # The result is paged only when formatting it for Telegram, so the user
    # never loses access to a wallet's history because of message length.
    matching = get_pair_history(wallet_a, wallet_b, max_pages=200, page_size=50)

    if not matching:
        send_telegram(
            f"⚠️ <b>No matching A → B TRON TRC-20 USDT transfers were returned "
            f"for Pair #{display_number}.</b>\n\n"
            f"Wallet A: <code>{telegram_escape(wallet_a)}</code>\n"
            f"Wallet B: <code>{telegram_escape(wallet_b)}</code>\n\n"
            f"Scan window: {scan_window_text()}\n"
            "The pair itself has NOT been removed. This only means no matching "
            "transfers were returned for this history query."
        )
        return

    total = sum(transfer_amount(tx) for tx in matching)
    total_pages = (len(matching) + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE
    if page > total_pages:
        send_telegram(
            f"Pair #{display_number} has {len(matching)} matching transfer(s), "
            f"so there are only {total_pages} history page(s).\n\n"
            f"Use <code>/history {display_number} 1</code> through "
            f"<code>/history {display_number} {total_pages}</code>."
        )
        return

    start_index = (page - 1) * HISTORY_PAGE_SIZE
    page_items = matching[start_index:start_index + HISTORY_PAGE_SIZE]
    first_number = start_index + 1
    last_number = start_index + len(page_items)

    message = (
        f"🔍 <b>Pair #{display_number} — A → B TRON TRC-20 USDT History</b>\n\n"
        f"Wallet A: <code>{telegram_escape(wallet_a)}</code>\n"
        f"Wallet B: <code>{telegram_escape(wallet_b)}</code>\n\n"
        f"<b>Transfers found:</b> {len(matching)}\n"
        f"<b>Total volume:</b> ${total:,.2f}\n"
        f"<b>Showing:</b> {first_number}–{last_number} of {len(matching)}\n"
        f"<b>Page:</b> {page}/{total_pages}\n\n"
    )

    for number, tx in zip(range(first_number, last_number + 1), page_items):
        amount = transfer_amount(tx)
        timestamp = transaction_timestamp(tx)
        if timestamp:
            date = datetime.fromtimestamp(timestamp / 1000, timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
        else:
            date = "Unknown time"
        txid = transaction_id(tx) or "Unknown"
        message += (
            f"<b>#{number}</b>\n"
            f"🕐 {date}\n"
            f"💰 ${amount:,.2f} USDT\n"
            f"TX: <code>{telegram_escape(txid)}</code>\n\n"
        )

    if page < total_pages:
        message += (
            f"➡️ Next page: <code>/history {display_number} {page + 1}</code>"
        )
    else:
        message += "✅ This is the last history page."

    send_telegram(message)


def command_cost():
    active = len([p for p in get_active_pairs() if not pair_is_excluded(p["id"])])
    send_telegram(f"💰 <b>Informational Cost Estimate</b>\n\nActive pairs: {active}\nEstimated network cost: ~{active*2.2:.2f} TRX\n\nNo transactions are executed by this bot.")


def command_status():
    saved = db.execute("SELECT COUNT(*) AS count FROM pairs").fetchone()["count"]
    active = db.execute("SELECT COUNT(*) AS count FROM pairs WHERE active=1").fetchone()["count"]
    wallets = db.execute("SELECT COUNT(*) AS count FROM monitored_wallets").fetchone()["count"]
    excluded = db.execute("SELECT COUNT(*) AS count FROM excluded_pairs").fetchone()["count"]
    send_telegram(
        f"🤖 <b>Monitor Status</b>\n\n<b>Historical Scanner</b>\nStatus: {SCAN_STATUS}\n"
        f"Transfers processed: {SCAN_PROCESSED:,}\nCandidate A→B pairs: {SCAN_CANDIDATE_PAIRS:,}\n"
        f"Pairs with 2+ transfers: {SCAN_TWO_PLUS_PAIRS:,}\nCEX-qualified candidates: {SCAN_CEX_QUALIFIED:,}\n"
        f"Scan page: {SCAN_PAGE:,}\nCurrent offset: {SCAN_OFFSET:,}\nLast scan activity: {SCAN_LAST_ACTIVITY}\n"
        f"Scan started: {SCAN_STARTED_AT}\nScan completed: {SCAN_COMPLETED_AT}\n\n"
        f"<b>Saved / Active</b>\nSaved candidate pairs: {saved}\nActive monitoring slots: {active}/{TARGET_PAIRS}\n"
        f"Monitored wallets: {wallets}\nExcluded pairs: {excluded}\n\n<b>Ongoing Monitor</b>\n"
        f"Monitoring cycles completed: {MONITOR_CYCLES:,}\nLast monitoring activity: {MONITOR_LAST_ACTIVITY}\n\n"
        f"Asset monitored: TRON TRC-20 USDT\nHistorical scan dates: {scan_window_text()}\nMinimum transfer: ${MIN_TRANSFER_USD:,.2f}\nTarget pairs: {TARGET_PAIRS}"
    )


def command_final():
    # Display ALL currently eligible (non-excluded) saved pairs, not only the
    # first TARGET_PAIRS monitoring slots. This guarantees that if nothing has
    # been excluded, the list starts at Pair 1 and continues to the last saved
    # candidate. Display numbers are freshly assigned 1..N and are used by
    # /history and /exclude as the same stable-in-the-current-list reference.
    eligible_pairs = get_display_pairs()
    if not eligible_pairs:
        send_telegram("No eligible TRON TRC-20 USDT pairs are currently available."); return

    monitored_count = sum(1 for pair in eligible_pairs if pair["active"] == 1)
    header = (
        f"<b>Current Eligible Pairs — TRON TRC-20 USDT</b>\n"
        f"{len(eligible_pairs)} eligible | {monitored_count}/{TARGET_PAIRS} currently monitored\n\n"
    )
    blocks = []
    for display_number, pair in enumerate(eligible_pairs, start=1):
        monitor_status = "MONITORED" if pair["active"] == 1 else "SAVED — NOT IN CURRENT MONITORING SLOTS"
        blocks.append(
            f"<b>Pair {display_number}</b> — {monitor_status}\n"
            f"Wallet A — TRON TRC-20 USDT:\n<code>{telegram_escape(pair['wallet_a'])}</code>\n"
            f"Wallet B — TRON TRC-20 USDT:\n<code>{telegram_escape(pair['wallet_b'])}</code>\n"
            f"CEX: {telegram_escape(pair['cex_name'])}\n\n"
        )

    chunks = []
    current = header
    for block in blocks:
        if len(current) + len(block) > 3800:
            chunks.append(current)
            current = ""
        current += block
    if current:
        chunks.append(current)
    for chunk in chunks:
        send_telegram(chunk)


def handle_command(text):
    parts = text.strip().split()
    if not parts: return
    command = parts[0].lower().lstrip("/")
    if command == "info": command_info()
    elif command == "exclude": command_exclude(parts)
    elif command == "history": command_history(parts)
    elif command == "cost": command_cost()
    elif command == "status": command_status()
    elif command == "final": command_final()  # FIX: /final was missing here.
    elif command == "help":
        send_telegram("<b>Available commands</b>\n\n<code>/info</code> — Show discovered pairs\n<code>/exclude 1</code> — Exclude a pair\n<code>/history 1</code> — Show history page 1 for Pair #1\n<code>/history 1 2</code> — Show page 2 for Pair #1\n<code>/final</code> — Show current active pair addresses\n<code>/cost</code> — Informational cost estimate\n<code>/status</code> — Monitor status\n<code>/help</code> — Show commands")


TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""


def poll_telegram():
    if not TELEGRAM_URL or not CHAT_ID: return
    offset = get_telegram_offset()
    try:
        data = http_get(f"{TELEGRAM_URL}/getUpdates", params={"offset":offset,"timeout":5,"allowed_updates":["message"]}, timeout=10, attempts=2)
    except Exception as exc:
        log.warning("Telegram polling failed: %s", exc); return
    for update in data.get("result", []):
        update_id = update.get("update_id")
        if update_id is not None: set_telegram_offset(int(update_id)+1)
        message = update.get("message")
        if not message: continue
        chat_id = str(message.get("chat",{}).get("id",""))
        if chat_id != str(CHAT_ID): continue
        text = message.get("text","").strip()
        if text:
            try: handle_command(text)
            except Exception as exc:
                log.exception("Command failed: %s", exc)
                send_telegram("❌ An error occurred while processing that command.")


def telegram_polling_loop():
    """Keep Telegram commands responsive while scanning/monitoring runs.

    Telegram polling runs independently from the historical scanner and wallet
    monitoring loop, so commands such as /status, /final and /history do not
    have to wait for a long monitoring cycle to finish.
    """
    log.info("📱 Telegram command polling started in background.")
    while True:
        try:
            poll_telegram()
        except Exception as exc:
            log.exception("Telegram polling loop error: %s", exc)
        time.sleep(0.5)


def startup_message():
    send_telegram(
        "🚀 <b>TRON Wallet Monitor Started</b>\n\n"
        f"Asset: TRON TRC-20 USDT\nHistorical scan dates: {scan_window_text()}\n"
        f"Minimum transfer: ${MIN_TRANSFER_USD:,.2f}\nRequired transfers: 2+\nTarget pairs: {TARGET_PAIRS}\n\n"
        "Historical qualification and ongoing monitoring are active."
    )


def main():
    if not TRONSCAN_API_KEY: log.warning("TRONSCAN_API_KEY is not configured.")
    if not TRONGRID_API_KEY: log.warning("TRONGRID_API_KEY is not configured.")
    if not TELEGRAM_BOT_TOKEN: log.warning("TELEGRAM_BOT_TOKEN is not configured.")
    if not CHAT_ID: log.warning("CHAT_ID is not configured.")
    try:
        scan_window_text()
        get_scan_window()
    except Exception as exc:
        log.error("Historical scan date configuration error: %s", exc)
        send_telegram(f"❌ Historical scan date error: {telegram_escape(str(exc))}")
        return
    # Start Telegram polling BEFORE the historical scan so commands such as
    # /status remain responsive even while the scanner is working through
    # hundreds of API pages. The thread is a daemon so it stops with the bot.
    telegram_thread = threading.Thread(target=telegram_polling_loop, name="telegram-poller", daemon=True)
    telegram_thread.start()

    startup_message()
    try: historical_scan()
    except Exception as exc:
        global SCAN_STATUS, SCAN_LAST_ACTIVITY
        SCAN_STATUS = "Failed"; SCAN_LAST_ACTIVITY = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
        log.exception("Historical scan crashed: %s", exc)
    refresh_active_slots()
    last_monitor = 0
    log.info("Entering continuous monitoring mode. Telegram commands remain responsive in the background.")
    while True:
        current_time = time.time()
        if current_time - last_monitor >= MONITOR_INTERVAL:
            try: monitor_wallets()
            except Exception as exc: log.exception("Monitoring cycle failed: %s", exc)
            last_monitor = current_time
        time.sleep(2)


if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: log.info("Monitor stopped by user.")
    except Exception as exc: log.exception("Fatal error: %s", exc)
    finally: db.close()

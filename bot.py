import os
import time
import sqlite3
import logging
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

MONITOR_INTERVAL = int(
    os.getenv("MONITOR_INTERVAL", "120")
)

TRONSCAN_API_KEY = os.getenv(
    "TRONSCAN_API_KEY",
    ""
)

TRONGRID_API_KEY = os.getenv(
    "TRONGRID_API_KEY",
    ""
)

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    ""
)

CHAT_ID = os.getenv(
    "CHAT_ID",
    ""
)

DATABASE_FILE = os.getenv(
    "DATABASE_FILE",
    "tron_monitor.db"
)

# Live diagnostic state shown by /status.
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

USDT_CONTRACT = (
    "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
)

TRONSCAN_URL = (
    "https://apilist.tronscanapi.com/api"
)

TRONGRID_URL = (
    "https://api.trongrid.io"
)

CEX_KEYWORDS = [
    "binance",
    "okx",
    "huobi",
    "htx",
    "gate",
    "kucoin",
    "bybit",
    "mexc",
    "bitfinex",
    "coinbase",
    "kraken",
    "bitget",
    "poloniex",
]


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger(
    "tron-monitor"
)


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(
    DATABASE_FILE,
    check_same_thread=False
)

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


db.commit()


# ============================================================
# HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def utc_string():
    return utc_now().isoformat()


def telegram_escape(value):
    return html.escape(
        str(value or "")
    )


def transaction_id(tx):
    return (
        tx.get("hash")
        or tx.get("transaction_id")
        or tx.get("txID")
        or ""
    )


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
        log.warning(
            "Telegram is not configured."
        )
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
            response.text[:300]
        )

    except requests.RequestException as exc:

        log.warning(
            "Telegram request failed: %s",
            exc
        )

    return False


def get_telegram_offset():

    row = db.execute(
        """
        SELECT value
        FROM telegram_state
        WHERE key = 'offset'
        """
    ).fetchone()

    if not row:
        return 0

    try:
        return int(
            row["value"]
        )
    except (
        ValueError,
        TypeError
    ):
        return 0


def set_telegram_offset(offset):

    db.execute(
        """
        INSERT INTO telegram_state(key, value)
        VALUES('offset', ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
        """,
        (
            str(offset),
        )
    )

    db.commit()


# ============================================================
# API HEADERS
# ============================================================

def tronscan_headers():

    if TRONSCAN_API_KEY:
        return {
            "TRON-PRO-API-KEY":
                TRONSCAN_API_KEY
        }

    return {}


def trongrid_headers():

    if TRONGRID_API_KEY:
        return {
            "TRON-PRO-API-KEY":
                TRONGRID_API_KEY
        }

    return {}


# ============================================================
# HTTP
# ============================================================

def http_get(
    url,
    params=None,
    headers=None,
    timeout=30,
    attempts=3
):

    last_error = None

    for attempt in range(
        1,
        attempts + 1
    ):

        try:

            response = requests.get(
                url,
                params=params,
                headers=headers or {},
                timeout=timeout,
            )

            if response.status_code == 429:

                wait = min(
                    10 * attempt,
                    30
                )

                log.warning(
                    "Rate limited. Waiting %ss...",
                    wait
                )

                time.sleep(wait)
                continue

            response.raise_for_status()

            return response.json()

        except (
            requests.RequestException,
            ValueError,
        ) as exc:

            last_error = exc

            if attempt < attempts:

                time.sleep(
                    min(
                        2 * attempt,
                        10
                    )
                )

    raise RuntimeError(
        f"API request failed after "
        f"{attempts} attempts: "
        f"{last_error}"
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
        params[
            "start_timestamp"
        ] = start_timestamp

    if end_timestamp is not None:
        params[
            "end_timestamp"
        ] = end_timestamp

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

    return data.get(
        "token_transfers",
        []
    )


# ============================================================
# WALLET HISTORY
# ============================================================

def get_wallet_history(
    address,
    limit=50,
    start=0
):

    try:

        data = http_get(
            f"{TRONSCAN_URL}/token_trc20/transfers",
            params={
                "address": address,
                "limit": limit,
                "start": start,
                "sort": "-timestamp",
                "contract_address":
                    USDT_CONTRACT,
            },
            headers=tronscan_headers(),
            timeout=30,
        )

        return data.get(
            "token_transfers",
            []
        )

    except Exception as exc:

        log.warning(
            "History lookup failed for %s: %s",
            address,
            exc
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

        for token in data.get(
            "data",
            []
        ):

            token_info = token.get(
                "token_info",
                {}
            )

            token_address = token_info.get(
                "address"
            )

            if token_address != USDT_CONTRACT:
                continue

            raw = int(
                token.get(
                    "balance",
                    0
                )
            )

            decimals = int(
                token_info.get(
                    "decimals",
                    6
                )
            )

            return raw / (
                10 ** decimals
            )

    except Exception as exc:

        log.warning(
            "Could not obtain USDT balance "
            "for %s: %s",
            address,
            exc
        )

    return 0.0


# ============================================================
# TRANSACTION PARSING
# ============================================================

def transfer_amount(tx):

    try:

        raw = int(
            tx.get(
                "quant",
                0
            )
        )

    except (
        ValueError,
        TypeError
    ):

        return 0.0

    return raw / 1_000_000


def transaction_timestamp(tx):

    try:

        return int(
            tx.get("block_ts")
            or tx.get("timestamp")
            or 0
        )

    except (
        ValueError,
        TypeError
    ):

        return 0


def get_tag_name(tx):

    tag = tx.get(
        "from_address_tag"
    )

    if isinstance(
        tag,
        dict
    ):

        return (
            tag.get(
                "from_address_tag"
            )
            or tag.get("name")
            or tag.get("tag")
            or ""
        )

    if isinstance(
        tag,
        str
    ):

        return tag

    return ""


def detect_cex(tag):

    normalized = str(
        tag or ""
    ).lower()

    for keyword in CEX_KEYWORDS:

        if keyword in normalized:
            return True, str(tag)

    return False, str(
        tag or "Unknown"
    )


# ============================================================
# DATABASE PAIRS
# ============================================================

def pair_is_excluded(pair_id):

    row = db.execute(
        """
        SELECT 1
        FROM excluded_pairs
        WHERE pair_id = ?
        """,
        (
            pair_id,
        )
    ).fetchone()

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

    existing = db.execute(
        """
        SELECT *
        FROM pairs
        WHERE wallet_a = ?
          AND wallet_b = ?
        """,
        (
            wallet_a,
            wallet_b
        )
    ).fetchone()

    if existing:

        db.execute(
            """
            UPDATE pairs
            SET transfer_count = ?,
                total_amount = ?,
                cex_balance = ?,
                cex_name = ?,
                last_seen = ?
            WHERE id = ?
            """,
            (
                transfer_count,
                total_amount,
                cex_balance,
                cex_name,
                now,
                existing["id"],
            )
        )

        pair_id = existing["id"]
        is_new = False

    else:

        cursor = db.execute(
            """
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
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0
            )
            """,
            (
                wallet_a,
                wallet_b,
                cex_name,
                transfer_count,
                total_amount,
                cex_balance,
                now,
                now,
                now,
            )
        )

        pair_id = cursor.lastrowid
        is_new = True

    db.execute(
        """
        INSERT INTO monitored_wallets(
            address,
            wallet_type,
            pair_id,
            first_seen
        )
        VALUES (?, 'A', ?, ?)
        ON CONFLICT(address)
        DO NOTHING
        """,
        (
            wallet_a,
            pair_id,
            now
        )
    )

    db.execute(
        """
        INSERT INTO monitored_wallets(
            address,
            wallet_type,
            pair_id,
            first_seen
        )
        VALUES (?, 'B', ?, ?)
        ON CONFLICT(address)
        DO NOTHING
        """,
        (
            wallet_b,
            pair_id,
            now
        )
    )

    db.commit()

    return pair_id, is_new


def refresh_active_slots():
    """Keep only the first TARGET_PAIRS non-excluded pairs active."""
    db.execute("UPDATE pairs SET active = 0")

    rows = db.execute(
        """
        SELECT p.id
        FROM pairs p
        LEFT JOIN excluded_pairs e
          ON e.pair_id = p.id
        WHERE e.pair_id IS NULL
        ORDER BY p.id
        LIMIT ?
        """,
        (TARGET_PAIRS,)
    ).fetchall()

    for row in rows:
        db.execute(
            "UPDATE pairs SET active = 1 WHERE id = ?",
            (row["id"],)
        )

    db.commit()


def get_active_display_pairs():
    """Return active pairs in the user-facing Pair #1, #2... order."""
    return db.execute(
        """
        SELECT *
        FROM pairs
        WHERE active = 1
        ORDER BY id
        """
    ).fetchall()


# ============================================================
# PAIR ALERT
# ============================================================

def announce_pair(
    pair_id,
    pair
):

    status = (
        "✅"
        if pair["cex_balance"] >= 500
        else "⚠️"
    )

    message = (
        f"{status} <b>Eligible Pair #{pair_id}</b>\n\n"

        f"🏦 <b>CEX / Wallet A:</b>\n"
        f"<code>"
        f"{telegram_escape(pair['wallet_a'])}"
        f"</code>\n\n"

        f"🏷 <b>CEX Tag:</b> "
        f"{telegram_escape(pair['cex_name'])}\n"

        f"💰 <b>USDT Balance:</b> "
        f"${pair['cex_balance']:,.2f}\n\n"

        f"👤 <b>Wallet B:</b>\n"
        f"<code>"
        f"{telegram_escape(pair['wallet_b'])}"
        f"</code>\n\n"

        f"📊 <b>Transfers:</b> "
        f"{pair['transfer_count']}x\n"

        f"💵 <b>Total Volume:</b> "
        f"${pair['total_amount']:,.2f}\n\n"

        f"Historical qualification window: "
        f"{WEEKS_BACK} week(s).\n"

        f"Monitoring will continue after qualification."
    )

    send_telegram(
        message
    )


# ============================================================
# HISTORICAL SCANNER
#
# IMPORTANT:
# We scan the global historical endpoint directly.
# We do NOT scan every receiver wallet.
#
# Duplicate pages are tolerated temporarily.
# This prevents a single overlapping TronScan page
# from prematurely ending the entire historical scan.
# ============================================================

def historical_scan():

    end_date = utc_now()

    start_date = (
        end_date
        - timedelta(
            weeks=WEEKS_BACK
        )
    )

    start_ms = int(
        start_date.timestamp()
        * 1000
    )

    end_ms = int(
        end_date.timestamp()
        * 1000
    )

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
        end_date.isoformat()
    )

    # --------------------------------------------------------
    # Pair statistics
    # --------------------------------------------------------

    pair_stats = defaultdict(
        lambda: {
            "count": 0,
            "total": 0.0,
            "tags": set(),
            "txids": set(),
        }
    )

    # Every transaction we've actually processed.
    seen_global_txids = set()

    offset = 0
    page_size = 50
    processed = 0

    # Number of consecutive pages that contain no new IDs.
    duplicate_pages = 0

    # Hard safety limit.
    # Prevents a broken API from running forever.
    max_duplicate_pages = 5

    # Hard page safety limit for one historical run.
    max_pages = 500

    page_number = 0

    while True:

        page_number += 1

        if page_number > max_pages:

            log.warning(
                "Historical scan reached safety limit "
                "of %s pages.",
                max_pages
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

            log.error(
                "Historical scan request failed: %s",
                exc
            )

            time.sleep(10)
            continue

        transfers = data.get(
            "token_transfers",
            []
        )

        # ----------------------------------------------------
        # No results = genuine end.
        # ----------------------------------------------------

        if not transfers:

            log.info(
                "No more historical transfers returned "
                "at offset %s.",
                offset
            )

            break

        log.info(
            "Historical batch: %s transfers "
            "(offset %s)",
            len(transfers),
            offset
        )

        new_transactions = 0

        # ----------------------------------------------------
        # Process transactions
        # ----------------------------------------------------

        for tx in transfers:

            txid = transaction_id(
                tx
            )

            # If the API supplied an ID and we already
            # processed it, skip the duplicate.
            if txid and txid in seen_global_txids:
                continue

            if txid:
                seen_global_txids.add(
                    txid
                )

            new_transactions += 1

            wallet_a = tx.get(
                "from_address"
            )

            wallet_b = tx.get(
                "to_address"
            )

            if not wallet_a or not wallet_b:
                continue

            amount = transfer_amount(
                tx
            )

            if amount <= MIN_TRANSFER_USD:
                continue

            timestamp = transaction_timestamp(
                tx
            )

            if timestamp:

                if timestamp < start_ms:
                    continue

                if timestamp > end_ms:
                    continue

            pair_key = (
                wallet_a,
                wallet_b
            )

            stats = pair_stats[
                pair_key
            ]

            # Protect the pair counter against
            # duplicate transaction IDs.
            if txid:

                if txid in stats["txids"]:
                    continue

                stats["txids"].add(
                    txid
                )

            stats["count"] += 1

            stats["total"] += amount

            tag = get_tag_name(
                tx
            )

            if tag:

                stats["tags"].add(
                    tag
                )

        # ----------------------------------------------------
        # Duplicate page handling
        # ----------------------------------------------------

        if new_transactions == 0:

            duplicate_pages += 1

            log.warning(
                "Historical page contained no new "
                "transactions "
                "(duplicate page %s/%s).",
                duplicate_pages,
                max_duplicate_pages
            )

            # IMPORTANT:
            #
            # Do NOT stop immediately.
            #
            # Move forward and see if TronScan gives us
            # another page.
            #
            # Only stop after several consecutive duplicate
            # pages.
            if duplicate_pages >= max_duplicate_pages:

                log.warning(
                    "Historical pagination stopped after "
                    "%s consecutive duplicate pages.",
                    max_duplicate_pages
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
            and any(detect_cex(tag)[0] for tag in stats["tags"])
        )
        SCAN_LAST_ACTIVITY = utc_now().strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )

        log.info(
            "Historical progress: "
            "%s unique transfers | "
            "%s candidate pairs | "
            "%s with 2+ transfers | "
            "%s CEX-qualified",
            SCAN_PROCESSED,
            SCAN_CANDIDATE_PAIRS,
            SCAN_TWO_PLUS_PAIRS,
            SCAN_CEX_QUALIFIED
        )

        # ----------------------------------------------------
        # Qualification check
        # ----------------------------------------------------

        for (
            wallet_a,
            wallet_b
        ), stats in list(
            pair_stats.items()
        ):

            if stats["count"] < 2:
                continue

            # Skip already saved pair.
            existing_pair = db.execute(
                """
                SELECT id
                FROM pairs
                WHERE wallet_a = ?
                  AND wallet_b = ?
                """,
                (
                    wallet_a,
                    wallet_b
                )
            ).fetchone()

            if existing_pair:
                continue

            # Search ALL retained tags for a CEX tag.
            cex_name = ""

            for tag in stats["tags"]:

                is_cex, detected_name = (
                    detect_cex(tag)
                )

                if is_cex:

                    cex_name = (
                        detected_name
                    )

                    break

            if not cex_name:
                continue

            log.info(
                "Candidate pair qualifies: "
                "%s → %s | "
                "%sx | $%.2f | CEX=%s",
                wallet_a,
                wallet_b,
                stats["count"],
                stats["total"],
                cex_name
            )

            # Only call balance API after all
            # qualification requirements are met.
            balance = get_usdt_balance(
                wallet_a
            )

            pair = {
                "wallet_a": wallet_a,
                "wallet_b": wallet_b,
                "cex_name": cex_name,
                "transfer_count":
                    stats["count"],
                "total_amount":
                    stats["total"],
                "cex_balance":
                    balance,
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
                    "✅ Eligible pair found: #%s",
                    pair_id
                )

                announce_pair(
                    pair_id,
                    pair
                )

        # ----------------------------------------------------
        # PAGINATION
        # ----------------------------------------------------
        #
        # Always move exactly one requested page forward.
        #
        # This is important. We do NOT use len(unique IDs)
        # to calculate the next offset.
        # ----------------------------------------------------

        offset += page_size

        # A short page is normally the end.
        #
        # However, if we previously encountered weird
        # pagination behavior, continue only if needed.
        if len(transfers) < page_size:

            log.info(
                "Historical final page reached "
                "(%s transfers).",
                len(transfers)
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

    for (
        wallet_a,
        wallet_b
    ), stats in pair_stats.items():

        if stats["count"] < 2:
            continue

        existing_pair = db.execute(
            """
            SELECT id
            FROM pairs
            WHERE wallet_a = ?
              AND wallet_b = ?
            """,
            (
                wallet_a,
                wallet_b
            )
        ).fetchone()

        if existing_pair:
            continue

        cex_name = ""

        for tag in stats["tags"]:

            is_cex, detected_name = (
                detect_cex(tag)
            )

            if is_cex:

                cex_name = detected_name
                break

        if not cex_name:
            continue

        balance = get_usdt_balance(
            wallet_a
        )

        pair = {
            "wallet_a": wallet_a,
            "wallet_b": wallet_b,
            "cex_name": cex_name,
            "transfer_count":
                stats["count"],
            "total_amount":
                stats["total"],
            "cex_balance":
                balance,
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
                pair_id
            )

            announce_pair(
                pair_id,
                pair
            )

    refresh_active_slots()

    active_count = db.execute(
        "SELECT COUNT(*) AS count FROM pairs WHERE active = 1"
    ).fetchone()["count"]

    saved_count = db.execute(
        "SELECT COUNT(*) AS count FROM pairs"
    ).fetchone()["count"]

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
        and any(detect_cex(tag)[0] for tag in stats["tags"])
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
        TARGET_PAIRS
    )


# ============================================================
# ONGOING MONITORING
# ============================================================

def get_active_pairs():

    return db.execute(
        """
        SELECT *
        FROM pairs
        WHERE active = 1
        ORDER BY id
        """
    ).fetchall()


def monitor_pair(pair):

    wallet_a = pair["wallet_a"]
    wallet_b = pair["wallet_b"]

    row = db.execute(
        """
        SELECT last_transaction_timestamp
        FROM monitored_wallets
        WHERE address = ?
        """,
        (
            wallet_b,
        )
    ).fetchone()

    previous_timestamp = (
        int(
            row[
                "last_transaction_timestamp"
            ]
        )
        if row
        else 0
    )

    try:

        history = get_wallet_history(
            wallet_b,
            limit=50
        )

        newest_timestamp = (
            previous_timestamp
        )

        for tx in history:

            timestamp = transaction_timestamp(
                tx
            )

            if timestamp > newest_timestamp:

                newest_timestamp = timestamp

            if timestamp <= previous_timestamp:
                continue

            if tx.get(
                "to_address"
            ) != wallet_b:
                continue

            if tx.get(
                "from_address"
            ) != wallet_a:
                continue

            amount = transfer_amount(
                tx
            )

            if amount <= 0:
                continue

            txid = (
                transaction_id(tx)
                or "unknown"
            )

            send_telegram(
                f"🔔 <b>New A → B USDT Transaction</b>\n\n"

                f"<b>Pair:</b> #{pair['id']}\n"

                f"<b>Wallet A:</b>\n"
                f"<code>"
                f"{telegram_escape(wallet_a)}"
                f"</code>\n\n"

                f"<b>Wallet B:</b>\n"
                f"<code>"
                f"{telegram_escape(wallet_b)}"
                f"</code>\n\n"

                f"<b>Amount:</b> "
                f"${amount:,.2f}\n"

                f"<b>TX:</b> "
                f"<code>"
                f"{telegram_escape(txid)}"
                f"</code>"
            )

        db.execute(
            """
            UPDATE monitored_wallets
            SET last_checked = ?,
                last_transaction_timestamp = ?
            WHERE address = ?
            """,
            (
                utc_string(),
                newest_timestamp,
                wallet_b,
            )
        )

        db.commit()

    except Exception as exc:

        log.warning(
            "Monitoring failed for pair #%s: %s",
            pair["id"],
            exc
        )


def monitor_wallets():

    global MONITOR_CYCLES, MONITOR_LAST_ACTIVITY

    pairs = get_active_pairs()

    for pair in pairs:

        if pair_is_excluded(
            pair["id"]
        ):
            continue

        monitor_pair(
            pair
        )

    MONITOR_CYCLES += 1
    MONITOR_LAST_ACTIVITY = utc_now().strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def format_pair(pair):

    excluded = pair_is_excluded(
        pair["id"]
    )

    status = (
        "🚫 EXCLUDED"
        if excluded
        else "✅ ACTIVE"
    )

    return (
        f"<b>#{pair['id']} — {status}</b>\n"

        f"CEX: "
        f"{telegram_escape(pair['cex_name'])}\n"

        f"Wallet A:\n"
        f"<code>"
        f"{telegram_escape(pair['wallet_a'])}"
        f"</code>\n"

        f"Wallet A USDT: "
        f"${pair['cex_balance']:,.2f}\n\n"

        f"Wallet B:\n"
        f"<code>"
        f"{telegram_escape(pair['wallet_b'])}"
        f"</code>\n\n"

        f"Transfers: "
        f"{pair['transfer_count']}x\n"

        f"Volume: "
        f"${pair['total_amount']:,.2f}\n"
    )


def command_info():
    pairs = db.execute(
        "SELECT * FROM pairs ORDER BY id"
    ).fetchall()

    if not pairs:
        send_telegram("No eligible pairs have been discovered yet.")
        return

    active_pairs = [p for p in pairs if p["active"] == 1 and not pair_is_excluded(p["id"])]
    excluded_pairs = [p for p in pairs if pair_is_excluded(p["id"])]

    message = (
        f"<b>Wallet Pairs</b>\n"
        f"Active: {len(active_pairs)}/{TARGET_PAIRS}\n\n"
    )

    for index, pair in enumerate(active_pairs, 1):
        message += format_pair(pair).replace(
            f"#{pair['id']} —",
            f"Pair #{index} —",
            1
        ) + "\n"

    if excluded_pairs:
        message += "<b>Excluded</b>\n\n"
        for pair in excluded_pairs:
            message += format_pair(pair) + "\n"

    send_telegram(message)


def command_exclude(parts):
    if len(parts) < 2:
        send_telegram("Usage: <code>exclude 1</code>")
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

    db.execute(
        """
        INSERT OR REPLACE INTO excluded_pairs(pair_id, excluded_at)
        VALUES (?, ?)
        """,
        (pair_id, utc_string())
    )
    db.commit()

    refresh_active_slots()

    send_telegram(
        f"🗑️ <b>Pair #{display_number} excluded.</b>\n\n"
        f"The next eligible candidate has been promoted into the active slots."
    )


def command_history(parts):
    if len(parts) < 2:
        send_telegram(
            "Usage: <code>history 1</code>\n"
            "Shows Wallet A → Wallet B USDT history for Pair #1."
        )
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
    wallet_a = pair["wallet_a"]
    wallet_b = pair["wallet_b"]

    txs = get_wallet_history(wallet_b, limit=100)
    matching = [
        tx for tx in txs
        if tx.get("from_address") == wallet_a
        and tx.get("to_address") == wallet_b
    ]

    if not matching:
        send_telegram(
            f"No A → B USDT transactions were found for Pair #{display_number}."
        )
        return

    total = sum(transfer_amount(tx) for tx in matching)

    message = (
        f"🔍 <b>Pair #{display_number} — A → B USDT History</b>\n\n"
        f"Wallet A: <code>{telegram_escape(wallet_a)}</code>\n"
        f"Wallet B: <code>{telegram_escape(wallet_b)}</code>\n"
        f"Transfers found: {len(matching)}\n"
        f"Volume shown: ${total:,.2f}\n\n"
    )

    for index, tx in enumerate(matching, 1):
        amount = transfer_amount(tx)
        timestamp = transaction_timestamp(tx)
        date = (
            datetime.fromtimestamp(timestamp / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if timestamp else "Unknown"
        )
        txid = transaction_id(tx) or "Unknown"

        message += (
            f"<b>#{index}</b> {date}\n"
            f"💰 ${amount:,.2f}\n"
            f"TX: <code>{telegram_escape(txid)}</code>\n\n"
        )

    send_telegram(message)


def command_cost():

    active = len(
        [
            p
            for p in get_active_pairs()
            if not pair_is_excluded(
                p["id"]
            )
        ]
    )

    estimated_trx = (
        active * 2.2
    )

    send_telegram(
        f"💰 <b>Informational Cost Estimate</b>\n\n"

        f"Active pairs: "
        f"{active}\n"

        f"Estimated network cost: "
        f"~{estimated_trx:.2f} TRX\n\n"

        f"No transactions are executed by this bot."
    )


def command_status():
    saved = db.execute(
        "SELECT COUNT(*) AS count FROM pairs"
    ).fetchone()["count"]

    active = db.execute(
        "SELECT COUNT(*) AS count FROM pairs WHERE active = 1"
    ).fetchone()["count"]

    wallets = db.execute(
        "SELECT COUNT(*) AS count FROM monitored_wallets"
    ).fetchone()["count"]

    excluded = db.execute(
        "SELECT COUNT(*) AS count FROM excluded_pairs"
    ).fetchone()["count"]

    send_telegram(
        f"🤖 <b>Monitor Status</b>\n\n"

        f"<b>Historical Scanner</b>\n"
        f"Status: {SCAN_STATUS}\n"
        f"Transfers processed: {SCAN_PROCESSED:,}\n"
        f"Candidate A→B pairs: {SCAN_CANDIDATE_PAIRS:,}\n"
        f"Pairs with 2+ transfers: {SCAN_TWO_PLUS_PAIRS:,}\n"
        f"CEX-qualified candidates: {SCAN_CEX_QUALIFIED:,}\n"
        f"Scan page: {SCAN_PAGE:,}\n"
        f"Current offset: {SCAN_OFFSET:,}\n"
        f"Last scan activity: {SCAN_LAST_ACTIVITY}\n"

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


def command_final():
    active_pairs = get_active_display_pairs()

    if not active_pairs:
        send_telegram("No active pairs are currently being monitored.")
        return

    message = (
        f"<b>Final Active Wallet Pairs</b>\n"
        f"{len(active_pairs)}/{TARGET_PAIRS} active\n\n"
    )

    for index, pair in enumerate(active_pairs, 1):
        message += (
            f"<b>Pair {index}</b>\n"
            f"CEX: {telegram_escape(pair['cex_name'])}\n"
            f"Wallet A: <code>{telegram_escape(pair['wallet_a'])}</code>\n"
            f"Wallet B: <code>{telegram_escape(pair['wallet_b'])}</code>\n\n"
        )

    send_telegram(message)


def handle_command(text):

    parts = text.strip().split()

    if not parts:
        return

    command = (
        parts[0]
        .lower()
        .lstrip("/")
    )

    if command == "info":

        command_info()

    elif command == "exclude":

        command_exclude(
            parts
        )

    elif command == "history":

        command_history(
            parts
        )

    elif command == "cost":

        command_cost()

    elif command == "status":

        command_status()

    elif command == "help":

        send_telegram(
            "<b>Available commands</b>\n\n"

            "<code>info</code> — "
            "Show discovered pairs\n"

            "<code>exclude 1</code> — "
            "Exclude a pair\n"

            "<code>history 1</code> — "
            "Show Pair #1 A → B USDT history\n\n"
            "<code>final</code> — "
            "Show the current active pair addresses\n"

            "<code>cost</code> — "
            "Informational cost estimate\n"

            "<code>status</code> — "
            "Monitor status\n"

            "<code>help</code> — "
            "Show commands"
        )


# ============================================================
# TELEGRAM POLLING
# ============================================================

def poll_telegram():

    if not TELEGRAM_URL or not CHAT_ID:
        return

    offset = get_telegram_offset()

    try:

        data = http_get(
            f"{TELEGRAM_URL}/getUpdates",
            params={
                "offset": offset,
                "timeout": 5,
                "allowed_updates": [
                    "message"
                ],
            },
            timeout=10,
            attempts=2,
        )

    except Exception as exc:

        log.warning(
            "Telegram polling failed: %s",
            exc
        )

        return

    updates = data.get(
        "result",
        []
    )

    for update in updates:

        update_id = update.get(
            "update_id"
        )

        if update_id is not None:

            set_telegram_offset(
                int(update_id) + 1
            )

        message = update.get(
            "message"
        )

        if not message:
            continue

        chat_id = str(
            message.get(
                "chat",
                {}
            ).get(
                "id",
                ""
            )
        )

        if chat_id != str(
            CHAT_ID
        ):
            continue

        text = message.get(
            "text",
            ""
        ).strip()

        if text:

            try:

                handle_command(
                    text
                )

            except Exception as exc:

                log.exception(
                    "Command failed: %s",
                    exc
                )

                send_telegram(
                    "❌ An error occurred while "
                    "processing that command."
                )


# ============================================================
# STARTUP MESSAGE
# ============================================================

def startup_message():

    send_telegram(
        "🚀 <b>TRON Wallet Monitor Started</b>\n\n"

        f"Historical lookback: "
        f"{WEEKS_BACK} week(s)\n"

        f"Minimum transfer: "
        f"${MIN_TRANSFER_USD:,.2f}\n"

        f"Required transfers: 2+\n"

        f"Target pairs: "
        f"{TARGET_PAIRS}\n\n"

        "Historical qualification and "
        "ongoing monitoring are active."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not TRONSCAN_API_KEY:

        log.warning(
            "TRONSCAN_API_KEY is not configured."
        )

    if not TRONGRID_API_KEY:

        log.warning(
            "TRONGRID_API_KEY is not configured."
        )

    if not TELEGRAM_BOT_TOKEN:

        log.warning(
            "TELEGRAM_BOT_TOKEN is not configured."
        )

    if not CHAT_ID:

        log.warning(
            "CHAT_ID is not configured."
        )

    startup_message()

    # --------------------------------------------------------
    # Historical scan
    # --------------------------------------------------------

    try:

        historical_scan()

    except Exception as exc:

        SCAN_STATUS = "Failed"
        SCAN_LAST_ACTIVITY = utc_now().strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )

        log.exception(
            "Historical scan crashed: %s",
            exc
        )

    refresh_active_slots()

    # --------------------------------------------------------
    # Continuous monitoring
    # --------------------------------------------------------

    last_monitor = 0

    log.info(
        "Entering continuous monitoring mode."
    )

    while True:

        current_time = time.time()

        poll_telegram()

        if (
            current_time - last_monitor
            >= MONITOR_INTERVAL
        ):

            try:

                monitor_wallets()

            except Exception as exc:

                log.exception(
                    "Monitoring cycle failed: %s",
                    exc
                )

            last_monitor = current_time

        time.sleep(2)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        log.info(
            "Monitor stopped by user."
        )

    except Exception as exc:

        log.exception(
            "Fatal error: %s",
            exc
        )

    finally:

        db.close()

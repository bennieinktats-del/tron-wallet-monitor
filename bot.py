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
# Persistent historical qualification + ongoing monitoring
# ============================================================

print("🚀 Starting Persistent TRON Wallet Monitor...")

# ============================================================
# CONFIGURATION
# ============================================================

TARGET_PAIRS = int(os.getenv("TARGET_PAIRS", "5"))
MIN_TRANSFER_USD = float(os.getenv("MIN_TRANSFER_USD", "50"))
WEEKS_BACK = int(os.getenv("WEEKS_BACK", "2"))

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "120"))

TRONSCAN_API_KEY = os.getenv("TRONSCAN_API_KEY", "")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

DATABASE_FILE = os.getenv("DATABASE_FILE", "tron_monitor.db")

USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

TRONSCAN_URL = "https://apilist.tronscanapi.com/api"
TRONGRID_URL = "https://api.trongrid.io"

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

log = logging.getLogger("tron-monitor")

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
    return html.escape(str(value or ""))


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
            response.text[:300]
        )

    except requests.RequestException as exc:
        log.warning("Telegram request failed: %s", exc)

    return False


def get_telegram_offset():
    row = db.execute(
        "SELECT value FROM telegram_state WHERE key = 'offset'"
    ).fetchone()

    if not row:
        return 0

    try:
        return int(row["value"])
    except ValueError:
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
        return {
            "TRON-PRO-API-KEY": TRONSCAN_API_KEY
        }

    return {}


def trongrid_headers():
    if TRONGRID_API_KEY:
        return {
            "TRON-PRO-API-KEY": TRONGRID_API_KEY
        }

    return {}


# ============================================================
# HTTP WITH RETRIES
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

        except (
            requests.RequestException,
            ValueError,
        ) as exc:
            last_error = exc

            if attempt < attempts:
                time.sleep(min(2 * attempt, 10))

    raise RuntimeError(
        f"API request failed after {attempts} attempts: {last_error}"
    )


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
            token_address = (
                token.get("token_info", {}).get("address")
            )

            if token_address == USDT_CONTRACT:
                raw = int(token.get("balance", 0))
                decimals = int(
                    token.get("token_info", {}).get("decimals", 6)
                )

                return raw / (10 ** decimals)

    except Exception as exc:
        log.warning(
            "Could not obtain USDT balance for %s: %s",
            address,
            exc
        )

    return 0.0


def get_wallet_history(address, limit=10):
    try:
        data = http_get(
            f"{TRONSCAN_URL}/token_trc20/transfers",
            params={
                "address": address,
                "limit": limit,
                "start": 0,
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
            exc
        )

        return []


def get_usdt_transfers(
    start_timestamp=None,
    end_timestamp=None,
    start=0,
    limit=100,
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
    ).get("token_transfers", [])


# ============================================================
# TRANSACTION PARSING
# ============================================================

def transfer_amount(tx):
    try:
        raw = int(tx.get("quant", 0))
    except (ValueError, TypeError):
        return 0.0

    return raw / 1_000_000


def transaction_timestamp(tx):
    return int(
        tx.get("block_ts")
        or tx.get("timestamp")
        or 0
    )


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
    normalized = tag.lower()

    for keyword in CEX_KEYWORDS:
        if keyword in normalized:
            return True, tag

    return False, tag or "Unknown"


# ============================================================
# PAIR DATABASE
# ============================================================

def pair_is_excluded(pair_id):
    row = db.execute(
        "SELECT 1 FROM excluded_pairs WHERE pair_id = ?",
        (pair_id,)
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
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

    db.execute("""
        INSERT INTO monitored_wallets(
            address,
            wallet_type,
            pair_id,
            first_seen
        )
        VALUES (?, 'A', ?, ?)
        ON CONFLICT(address) DO NOTHING
    """, (wallet_a, pair_id, now))

    db.execute("""
        INSERT INTO monitored_wallets(
            address,
            wallet_type,
            pair_id,
            first_seen
        )
        VALUES (?, 'B', ?, ?)
        ON CONFLICT(address) DO NOTHING
    """, (wallet_b, pair_id, now))

    db.commit()

    return pair_id, is_new


# ============================================================
# PAIR QUALIFICATION
# ============================================================
def analyze_wallet(wallet_b, start_ms, end_ms):
    all_history = []
    offset = 0

    while True:
        try:
            batch = get_usdt_transfers(
                start_timestamp=start_ms,
                end_timestamp=end_ms,
                start=offset,
                limit=50,
            )

        except Exception as exc:
            log.warning(
                "Could not fetch transfers for wallet %s: %s",
                wallet_b,
                exc
            )
            break

        if not batch:
            break

        all_history.extend(batch)

        log.info(
            "Wallet %s analysis batch: %s transfers",
            wallet_b,
            len(batch)
        )

        offset += len(batch)

        if len(batch) < 50:
            break

        time.sleep(0.2)

    by_sender = defaultdict(list)

    for tx in all_history:
        if tx.get("to_address") != wallet_b:
            continue

        from_address = tx.get("from_address")

        if not from_address:
            continue

        amount = transfer_amount(tx)

        if amount <= MIN_TRANSFER_USD:
            continue

        timestamp = transaction_timestamp(tx)

        if timestamp:
            if timestamp < start_ms or timestamp > end_ms:
                continue

        by_sender[from_address].append({
            "amount": amount,
            "timestamp": timestamp,
            "tag": get_tag_name(tx),
            "txid": (
                tx.get("hash")
                or tx.get("transaction_id")
                or tx.get("txID")
                or ""
            ),
        })

    qualified = []

    for wallet_a, transactions in by_sender.items():

        if len(transactions) < 2:
            continue

        tags = [
            tx["tag"]
            for tx in transactions
            if tx["tag"]
        ]

        tag_name = tags[0] if tags else ""

        is_cex, cex_name = detect_cex(tag_name)

        if not is_cex:
            continue

        total = sum(
            tx["amount"]
            for tx in transactions
        )

        balance = get_usdt_balance(wallet_a)

        qualified.append({
            "wallet_a": wallet_a,
            "wallet_b": wallet_b,
            "cex_name": cex_name,
            "transfer_count": len(transactions),
            "total_amount": total,
            "cex_balance": balance,
        })

    return qualified



# ============================================================
# NEW PAIR ALERT
# ============================================================

def announce_pair(pair_id, pair):
    status = "✅" if pair["cex_balance"] >= 500 else "⚠️"

    message = (
        f"{status} <b>Eligible Pair #{pair_id}</b>\n\n"
        f"🏦 <b>CEX / Wallet A:</b>\n"
        f"<code>{telegram_escape(pair['wallet_a'])}</code>\n\n"
        f"🏷 <b>CEX Tag:</b> "
        f"{telegram_escape(pair['cex_name'])}\n"
        f"💰 <b>USDT Balance:</b> "
        f"${pair['cex_balance']:,.2f}\n\n"
        f"👤 <b>Wallet B:</b>\n"
        f"<code>{telegram_escape(pair['wallet_b'])}</code>\n\n"
        f"📊 <b>Transfers:</b> "
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

    log.info(
        "Historical scan: %s → %s",
        start_date.isoformat(),
        end_date.isoformat()
    )

    offset = 0
    processed = 0

    while True:

        try:
            transfers = get_usdt_transfers(
                start_timestamp=start_ms,
                end_timestamp=end_ms,
                start=offset,
                limit=50,
            )

        except Exception as exc:
            log.error(
                "Historical scan request failed: %s",
                exc
            )
            time.sleep(10)
            continue

        if not transfers:
            log.info(
                "No more historical transfers returned at offset %s.",
                offset
            )
            break

        log.info(
            "Historical batch: %s transfers",
            len(transfers)
        )

        receivers = set()

        for tx in transfers:
            receiver = tx.get("to_address")

            if receiver:
                receivers.add(receiver)

        for wallet_b in receivers:

            current_count = db.execute(
                "SELECT COUNT(*) AS count FROM pairs"
            ).fetchone()["count"]

            if current_count >= TARGET_PAIRS:
                log.info(
                    "Target of %s eligible pairs reached.",
                    TARGET_PAIRS
                )
                return

            try:
                results = analyze_wallet(
                    wallet_b,
                    start_ms,
                    end_ms
                )

                for pair in results:

                    pair_id, is_new = save_pair(
                        pair["wallet_a"],
                        pair["wallet_b"],
                        pair["cex_name"],
                        pair["transfer_count"],
                        pair["total_amount"],
                        pair["cex_balance"],
                    )

                    if is_new:
                        log.info(
                            "Eligible pair found: #%s",
                            pair_id
                        )

                        announce_pair(
                            pair_id,
                            pair
                        )

            except Exception as exc:
                log.warning(
                    "Could not analyze %s: %s",
                    wallet_b,
                    exc
                )

            time.sleep(0.2)

        processed += len(transfers)
        offset += len(transfers)

        log.info(
            "Historical progress: %s transfers",
            processed
        )

        # Only stop when the API actually returns no records.
        # A 50-transfer response is NOT treated as the end.
        time.sleep(0.2)

    log.info(
        "Historical scan completed. Total transfers processed: %s",
        processed
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

    last_checked = db.execute("""
        SELECT last_transaction_timestamp
        FROM monitored_wallets
        WHERE address = ?
    """, (wallet_b,)).fetchone()

    previous_timestamp = (
        int(last_checked["last_transaction_timestamp"])
        if last_checked
        else 0
    )

    try:
        history = get_wallet_history(
            wallet_b,
            limit=50
        )

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

            txid = (
                tx.get("hash")
                or tx.get("transaction_id")
                or tx.get("txID")
                or "unknown"
            )

            send_telegram(
                f"🔔 <b>New A → B USDT Transaction</b>\n\n"
                f"<b>Pair:</b> #{pair['id']}\n"
                f"<b>Wallet A:</b>\n"
                f"<code>{telegram_escape(wallet_a)}</code>\n\n"
                f"<b>Wallet B:</b>\n"
                f"<code>{telegram_escape(wallet_b)}</code>\n\n"
                f"<b>Amount:</b> ${amount:,.2f}\n"
                f"<b>TX:</b> <code>{telegram_escape(txid)}</code>"
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
            exc
        )


def monitor_wallets():
    pairs = get_active_pairs()

    for pair in pairs:
        if pair_is_excluded(pair["id"]):
            continue

        monitor_pair(pair)


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def format_pair(pair):
    excluded = pair_is_excluded(pair["id"])
    status = "🚫 EXCLUDED" if excluded else "✅ ACTIVE"

    return (
        f"<b>#{pair['id']} — {status}</b>\n"
        f"CEX: {telegram_escape(pair['cex_name'])}\n"
        f"Wallet A:\n"
        f"<code>{telegram_escape(pair['wallet_a'])}</code>\n"
        f"Wallet A USDT: ${pair['cex_balance']:,.2f}\n\n"
        f"Wallet B:\n"
        f"<code>{telegram_escape(pair['wallet_b'])}</code>\n\n"
        f"Transfers: {pair['transfer_count']}x\n"
        f"Volume: ${pair['total_amount']:,.2f}\n"
    )


def command_info():
    pairs = db.execute("""
        SELECT *
        FROM pairs
        ORDER BY id
    """).fetchall()

    if not pairs:
        send_telegram("No eligible pairs have been discovered yet.")
        return

    active = [
        p for p in pairs
        if not pair_is_excluded(p["id"])
    ]

    message = (
        f"<b>Wallet Pairs</b>\n"
        f"Active: {len(active)}/{len(pairs)}\n\n"
    )

    for pair in pairs:
        message += format_pair(pair) + "\n"

    send_telegram(message)


def command_exclude(parts):
    if len(parts) < 2:
        send_telegram(
            "Usage: <code>exclude 1</code>"
        )
        return

    try:
        pair_id = int(parts[1])
    except ValueError:
        send_telegram("Pair number must be a number.")
        return

    pair = db.execute(
        "SELECT id FROM pairs WHERE id = ?",
        (pair_id,)
    ).fetchone()

    if not pair:
        send_telegram(
            f"Pair #{pair_id} was not found."
        )
        return

    db.execute("""
        INSERT OR REPLACE INTO excluded_pairs(
            pair_id,
            excluded_at
        )
        VALUES (?, ?)
    """, (
        pair_id,
        utc_string()
    ))

    db.commit()

    send_telegram(
        f"🗑️ <b>Pair #{pair_id} excluded.</b>"
    )


def command_history(parts):
    if len(parts) < 2:
        send_telegram(
            "Usage: <code>history WALLET_ADDRESS</code>"
        )
        return

    address = parts[1].strip()

    txs = get_wallet_history(
        address,
        limit=10
    )

    if not txs:
        send_telegram(
            "No USDT transactions were found."
        )
        return

    message = (
        f"🔍 <b>Last 10 USDT Transactions</b>\n"
        f"<code>{telegram_escape(address)}</code>\n\n"
    )

    for index, tx in enumerate(txs[:10], 1):
        amount = transfer_amount(tx)

        from_address = tx.get(
            "from_address",
            "Unknown"
        )

        to_address = tx.get(
            "to_address",
            "Unknown"
        )

        timestamp = transaction_timestamp(tx)

        if timestamp:
            date = datetime.fromtimestamp(
                timestamp / 1000,
                timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
        else:
            date = "Unknown"

        message += (
            f"<b>#{index}</b> {date}\n"
            f"💰 ${amount:,.2f}\n"
            f"From: <code>{telegram_escape(from_address)}</code>\n"
            f"To: <code>{telegram_escape(to_address)}</code>\n\n"
        )

    send_telegram(message)


def command_cost():
    active = len([
        p for p in get_active_pairs()
        if not pair_is_excluded(p["id"])
    ])

    estimated_trx = active * 2.2

    send_telegram(
        f"💰 <b>Informational Cost Estimate</b>\n\n"
        f"Active pairs: {active}\n"
        f"Estimated network cost: "
        f"~{estimated_trx:.2f} TRX\n\n"
        f"No transactions are executed by this bot."
    )


def command_status():
    pairs = db.execute(
        "SELECT COUNT(*) AS count FROM pairs"
    ).fetchone()["count"]

    wallets = db.execute(
        "SELECT COUNT(*) AS count FROM monitored_wallets"
    ).fetchone()["count"]

    excluded = db.execute(
        "SELECT COUNT(*) AS count FROM excluded_pairs"
    ).fetchone()["count"]

    send_telegram(
        f"🤖 <b>Monitor Status</b>\n\n"
        f"Eligible pairs: {pairs}\n"
        f"Monitored wallets: {wallets}\n"
        f"Excluded pairs: {excluded}\n"
        f"Lookback: {WEEKS_BACK} week(s)\n"
        f"Minimum transfer: ${MIN_TRANSFER_USD:,.2f}\n"
        f"Target pairs: {TARGET_PAIRS}"
    )


def handle_command(text):
    parts = text.strip().split()

    if not parts:
        return

    command = parts[0].lower().lstrip("/")

    if command == "info":
        command_info()

    elif command == "exclude":
        command_exclude(parts)

    elif command == "history":
        command_history(parts)

    elif command == "cost":
        command_cost()

    elif command == "status":
        command_status()

    elif command == "help":
        send_telegram(
            "<b>Available commands</b>\n\n"
            "<code>info</code> — Show discovered pairs\n"
            "<code>exclude 1</code> — Exclude a pair\n"
            "<code>history WALLET</code> — Show recent USDT history\n"
            "<code>cost</code> — Informational cost estimate\n"
            "<code>status</code> — Monitor status\n"
            "<code>help</code> — Show commands"
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
                "allowed_updates": ["message"],
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

    updates = data.get("result", [])

    for update in updates:
        update_id = update.get("update_id")

        if update_id is not None:
            set_telegram_offset(
                int(update_id) + 1
            )

        message = update.get("message")

        if not message:
            continue

        chat_id = str(
            message.get("chat", {}).get("id", "")
        )

        if chat_id != str(CHAT_ID):
            continue

        text = message.get("text", "").strip()

        if text:
            try:
                handle_command(text)
            except Exception as exc:
                log.exception(
                    "Command failed: %s",
                    exc
                )

                send_telegram(
                    "❌ An error occurred while processing that command."
                )


# ============================================================
# STARTUP
# ============================================================

def startup_message():
    send_telegram(
        "🚀 <b>TRON Wallet Monitor Started</b>\n\n"
        f"Historical lookback: {WEEKS_BACK} week(s)\n"
        f"Minimum transfer: ${MIN_TRANSFER_USD:,.2f}\n"
        f"Required transfers: 2+\n"
        f"Target pairs: {TARGET_PAIRS}\n\n"
        "Historical qualification and ongoing monitoring are active."
    )


# ============================================================
# MAIN LOOP
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

    # Historical qualification
    try:
        historical_scan()
    except Exception as exc:
        log.exception(
            "Historical scan crashed: %s",
            exc
        )

    # Continuous monitoring
    last_monitor = 0

    log.info(
        "Entering continuous monitoring mode."
    )

    while True:
        current_time = time.time()

        # Keep Telegram responsive
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

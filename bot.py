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

TARGET_PAIRS = int(os.getenv("TARGET_PAIRS", "2"))
MIN_TRANSFER_USD = float(os.getenv("MIN_TRANSFER_USD", "50"))
WEEKS_BACK = int(os.getenv("WEEKS_BACK", "2"))
HUMAN_LOOKBACK_DAYS = int(os.getenv("HUMAN_LOOKBACK_DAYS", "14"))
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
SCAN_ROUTINE_CANDIDATES = 0
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
db.execute("""CREATE TABLE IF NOT EXISTS human_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_a TEXT NOT NULL,
    wallet_b TEXT NOT NULL,
    transfer_count INTEGER DEFAULT 0,
    distinct_dates INTEGER DEFAULT 0,
    time_spread_hours REAL DEFAULT 0,
    amount_median REAL DEFAULT 0,
    amount_spread_pct REAL DEFAULT 0,
    routine_score REAL DEFAULT 0,
    cex_name TEXT DEFAULT '',
    first_seen TEXT,
    last_seen TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL,
    UNIQUE(wallet_a, wallet_b)
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


def get_pair_history_for_date(wallet_a, wallet_b, date_text):
    """Return A -> B TRC-20 USDT transfers for one UTC calendar day."""
    day = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    next_day = day + timedelta(days=1)
    start_ms = int(day.timestamp() * 1000)
    end_ms = int(next_day.timestamp() * 1000) - 1
    found = {}
    for page in range(200):
        params = {
            "fromAddress": wallet_a,
            "toAddress": wallet_b,
            "contract_address": USDT_CONTRACT,
            "start": page * 50, "limit": 50, "sort": "-timestamp",
            "start_timestamp": start_ms, "end_timestamp": end_ms,
        }
        try:
            data = http_get(f"{TRONSCAN_URL}/token_trc20/transfers", params=params,
                            headers=tronscan_headers(), timeout=30)
        except Exception as exc:
            log.warning("Date history lookup failed for %s → %s on %s: %s", wallet_a, wallet_b, date_text, exc)
            break
        txs = data.get("token_transfers", [])
        if not txs:
            break
        for tx in txs:
            if tx.get("from_address") == wallet_a and tx.get("to_address") == wallet_b:
                txid = transaction_id(tx)
                found[txid or f"{transaction_timestamp(tx)}:{transfer_amount(tx)}"] = tx
        if len(txs) < 50:
            break
        time.sleep(0.1)
    return sorted(found.values(), key=transaction_timestamp, reverse=True)


def get_wallet_history_all(address, max_pages=200, page_size=50, date_text=None):
    """Return all available TRC-20 USDT transfers involving one wallet."""
    start_ms = end_ms = None
    if date_text:
        day = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start_ms = int(day.timestamp() * 1000)
        end_ms = int((day + timedelta(days=1)).timestamp() * 1000) - 1
    found = {}
    for page in range(max_pages):
        params = {
            "relatedAddress": address, "contract_address": USDT_CONTRACT,
            "start": page * page_size, "limit": min(page_size, 50), "sort": "-timestamp",
        }
        if start_ms is not None:
            params["start_timestamp"] = start_ms
            params["end_timestamp"] = end_ms
        try:
            data = http_get(f"{TRONSCAN_URL}/token_trc20/transfers", params=params,
                            headers=tronscan_headers(), timeout=30)
        except Exception as exc:
            log.warning("Wallet history lookup failed for %s: %s", address, exc)
            break
        txs = data.get("token_transfers", [])
        if not txs:
            break
        for tx in txs:
            txid = transaction_id(tx)
            found[txid or f"{transaction_timestamp(tx)}:{transfer_amount(tx)}:{tx.get('from_address')}:{tx.get('to_address')}"] = tx
        if len(txs) < page_size or (page + 1) * page_size >= 10000:
            break
        time.sleep(0.1)
    return sorted(found.values(), key=transaction_timestamp, reverse=True)


def parse_date_text(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return None


def format_history_transactions(title, subtitle, txs, page, page_size=8):
    total = len(txs)
    if total == 0:
        return None
    total_pages = (total + page_size - 1) // page_size
    if page > total_pages:
        return f"{title}\n\nOnly {total_pages} page(s) available. Use page 1–{total_pages}."
    start = (page - 1) * page_size
    items = txs[start:start + page_size]
    total_volume = sum(transfer_amount(tx) for tx in txs)
    message = (f"<b>{title}</b>\n\n{subtitle}\n\n"
               f"<b>Transfers:</b> {total}\n<b>Total volume:</b> ${total_volume:,.2f}\n"
               f"<b>Showing:</b> {start+1}–{start+len(items)} of {total}\n"
               f"<b>Page:</b> {page}/{total_pages}\n\n")
    for n, tx in zip(range(start+1, start+len(items)+1), items):
        ts = transaction_timestamp(tx)
        date = datetime.fromtimestamp(ts/1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if ts else "Unknown time"
        txid = transaction_id(tx) or "Unknown"
        sender = tx.get("from_address", "Unknown")
        recipient = tx.get("to_address", "Unknown")
        message += (f"<b>#{n}</b>\n🕐 {date}\n💰 ${transfer_amount(tx):,.2f} USDT\n"
                    f"From: <code>{telegram_escape(sender)}</code>\n"
                    f"To: <code>{telegram_escape(recipient)}</code>\n"
                    f"TX: <code>{telegram_escape(txid)}</code>\n\n")
    if page < total_pages:
        message += f"➡️ Next page: use the same command with page {page+1}."
    else:
        message += "✅ This is the last page."
    return message


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
    """Return the historical scan window.

    The human-activity scanner uses a strict 14-day window by default. START_DATE
    can still be supplied for a custom starting date; the scanner will never
    use a window longer than HUMAN_LOOKBACK_DAYS.
    """
    end_date = utc_now()
    latest_allowed_start = end_date - timedelta(days=HUMAN_LOOKBACK_DAYS)
    if START_DATE:
        try:
            requested = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise ValueError(f"Invalid START_DATE '{START_DATE}'. Use YYYY-MM-DD, e.g. 2026-06-15")
        if requested > end_date:
            raise ValueError("START_DATE cannot be in the future.")
        start_date = max(requested, latest_allowed_start)
    else:
        start_date = latest_allowed_start
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
        "🔎 HISTORICAL SCAN | chunk=%s page=%s offset=%s processed=%s | "
        "A→B=%s | 2+_transfers=%s | routine_candidates=%s | CEX_tagged=%s | window=%s",
        SCAN_CHUNK, SCAN_PAGE, SCAN_OFFSET, SCAN_PROCESSED, SCAN_CANDIDATE_PAIRS,
        SCAN_TWO_PLUS_PAIRS, SCAN_ROUTINE_CANDIDATES, SCAN_CEX_QUALIFIED, scan_window_text(),
    )


def median(values):
    values = sorted(values)
    if not values:
        return 0.0
    n = len(values)
    mid = n // 2
    return float(values[mid]) if n % 2 else (values[mid - 1] + values[mid]) / 2.0


def human_routine_score(transfers):
    """Strict A→B recurring-routine test.

    A candidate receives 100/100 only when ALL strict conditions pass:
    - at least 3 A→B transfers on at least 2 different dates
    - no more than two A→B transfers on any calendar day
    - activity shows consistent recurring behaviour across dates (daily or weekly)
    - one or two transfers on a day are allowed when the overall pattern remains consistent
    - every consecutive transfer has a meaningful gap (>= 2 minutes)
    - transaction times are not machine-identical; there must be natural timing variation
    - the wallet is not making large same-minute bursts

    This is a behavioural heuristic only; blockchain data cannot prove that a
    human personally controls a wallet.
    """
    if len(transfers) < 3:
        return None

    rows = []
    for tx in transfers:
        ts = transaction_timestamp(tx)
        amount = transfer_amount(tx)
        if not ts or amount <= 0:
            continue
        dt = datetime.fromtimestamp(ts / 1000, timezone.utc)
        rows.append((ts, dt, amount))

    if len(rows) < 3:
        return None
    rows.sort(key=lambda x: x[0])

    dates = [r[1].date() for r in rows]
    amounts = [r[2] for r in rows]
    distinct_dates = len(set(dates))
    if distinct_dates < 2:
        return None

    # Allow one or two A→B transfers per UTC calendar day. Two transfers
    # are acceptable when the relationship still shows a consistent recurring
    # routine across multiple dates; larger same-day bursts are rejected.
    counts_by_date = defaultdict(int)
    for d in dates:
        counts_by_date[d] += 1
    if max(counts_by_date.values()) > 2:
        return None

    timestamps = [r[0] for r in rows]
    gaps_minutes = [(b - a) / 60000.0 for a, b in zip(timestamps, timestamps[1:])]
    if not gaps_minutes or min(gaps_minutes) < 2.0:
        return None

    # Reject machine-like identical timing. Humans can repeat a routine time,
    # but the gaps must still show reasonable variation.
    if len(gaps_minutes) >= 2:
        rounded_gaps = [round(g / 0.5) for g in gaps_minutes]
        unique_ratio = len(set(rounded_gaps)) / len(rounded_gaps)
        gap_range = max(gaps_minutes) - min(gaps_minutes)
        if unique_ratio < 0.5 and gap_range < 2.0:
            return None
    else:
        unique_ratio = 1.0
        gap_range = 0.0

    # Daily or weekly recurrence. Daily means activity occurs on at least
    # 3 consecutive/near-consecutive days; weekly means at least two gaps of
    # roughly 5–9 days. We do not require exact dates or exact times.
    sorted_dates = sorted(set(dates))
    day_gaps = [(b - a).days for a, b in zip(sorted_dates, sorted_dates[1:])]
    daily_like = sum(g <= 2 for g in day_gaps) >= 2
    weekly_like = sum(5 <= g <= 9 for g in day_gaps) >= 1
    if not (daily_like or weekly_like):
        return None

    span_days = max(0, (max(dates) - min(dates)).days)
    weeks = max(1.0, span_days / 7.0)
    weekly_count = len(rows) / weeks
    # Keep the intended human activity band. A single-day sample is handled
    # by the recurrence requirement above, so it cannot qualify accidentally.
    if not (2.0 <= weekly_count <= 15.0):
        return None

    # Amounts can vary naturally; reject only an extreme zero/invalid median.
    med_amount = median(amounts)
    if med_amount <= 0:
        return None
    amount_spread = median([abs(a - med_amount) / med_amount for a in amounts]) * 100.0

    time_minutes = [r[1].hour * 60 + r[1].minute + r[1].second / 60.0 for r in rows]
    med_time = median(time_minutes)
    time_spread = median([min(abs(t - med_time), 1440 - abs(t - med_time)) for t in time_minutes])

    # All strict conditions passed: exact 100/100.
    return {
        "score": 100.0,
        "distinct_dates": distinct_dates,
        "time_spread_hours": round(time_spread / 60.0, 2),
        "amount_median": round(med_amount, 2),
        "amount_spread_pct": round(amount_spread, 1),
        "first_seen": min(dates).isoformat(),
        "last_seen": max(dates).isoformat(),
        "weekly_count": round(weekly_count, 2),
        "median_gap_minutes": round(median(gaps_minutes), 2),
        "min_gap_minutes": round(min(gaps_minutes), 2),
        "timing_variation_ratio": round(unique_ratio, 2),
        "pattern": "daily" if daily_like else "weekly",
    }

def save_human_candidate(wallet_a, wallet_b, transfers, cex_name, analysis):
    now = utc_string()
    db.execute("""INSERT INTO human_candidates
        (wallet_a,wallet_b,transfer_count,distinct_dates,time_spread_hours,amount_median,amount_spread_pct,
         routine_score,cex_name,first_seen,last_seen,status,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',?)
        ON CONFLICT(wallet_a,wallet_b) DO UPDATE SET
          transfer_count=excluded.transfer_count, distinct_dates=excluded.distinct_dates,
          time_spread_hours=excluded.time_spread_hours, amount_median=excluded.amount_median,
          amount_spread_pct=excluded.amount_spread_pct, routine_score=excluded.routine_score,
          cex_name=excluded.cex_name, first_seen=excluded.first_seen, last_seen=excluded.last_seen""",
        (wallet_a, wallet_b, len(transfers), analysis["distinct_dates"], analysis["time_spread_hours"],
         analysis["amount_median"], analysis["amount_spread_pct"], analysis["score"], cex_name,
         analysis["first_seen"], analysis["last_seen"], now))
    db.commit()


def get_human_candidates():
    return db.execute("SELECT * FROM human_candidates WHERE status='pending' AND routine_score >= 100 ORDER BY routine_score DESC, id LIMIT ?", (TARGET_PAIRS,)).fetchall()


def approve_human_candidate(display_number):
    candidates = get_human_candidates()
    if not 1 <= display_number <= len(candidates):
        return None
    candidate = candidates[display_number - 1]
    existing = db.execute("SELECT * FROM pairs WHERE wallet_a=? AND wallet_b=?", (candidate["wallet_a"], candidate["wallet_b"])).fetchone()
    if existing:
        db.execute("UPDATE human_candidates SET status='approved' WHERE id=?", (candidate["id"],))
        db.commit()
        refresh_active_slots()
        return existing["id"]
    balance = get_usdt_balance(candidate["wallet_a"])
    pair_id, _ = save_pair(candidate["wallet_a"], candidate["wallet_b"], candidate["cex_name"] or "Unknown",
                           candidate["transfer_count"], candidate["amount_median"] * candidate["transfer_count"], balance)
    db.execute("UPDATE human_candidates SET status='approved' WHERE id=?", (candidate["id"],))
    db.commit()
    refresh_active_slots()
    return pair_id


def historical_scan():
    """Scan the full historical window without hitting TronScan's 10k offset cap.

    The scanner starts with 7-day windows, but any busy window that approaches
    the 10,000-record pagination ceiling is automatically split in half and
    rescanned. Exact A→B relationships are merged across all windows, so the
    final routine score is based on the complete historical period.
    """
    start_date, end_date = get_scan_window()
    global SCAN_STATUS, SCAN_CHUNK, SCAN_PAGE, SCAN_OFFSET, SCAN_PROCESSED
    global SCAN_CANDIDATE_PAIRS, SCAN_TWO_PLUS_PAIRS, SCAN_CEX_QUALIFIED
    global SCAN_ROUTINE_CANDIDATES, SCAN_LAST_ACTIVITY, SCAN_STARTED_AT, SCAN_COMPLETED_AT

    SCAN_STATUS = "Running"
    SCAN_CHUNK = SCAN_PAGE = SCAN_OFFSET = SCAN_PROCESSED = 0
    SCAN_CANDIDATE_PAIRS = SCAN_TWO_PLUS_PAIRS = SCAN_CEX_QUALIFIED = SCAN_ROUTINE_CANDIDATES = 0
    SCAN_LAST_ACTIVITY = SCAN_STARTED_AT = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
    SCAN_COMPLETED_AT = "Never"

    log.info("🚀 RECURRING-HUMAN-ACTIVITY HISTORICAL SCAN STARTED")
    log.info("📅 Full scan window: %s", scan_window_text())
    log.info("💰 Minimum transfer: $%.2f | 🎯 Manual-verification candidates only", MIN_TRANSFER_USD)
    log.info("📡 Scanning TRON TRC-20 USDT in bounded time windows for recurring A→B patterns...")

    pair_stats = defaultdict(dict)
    seen_global_txids = set()
    qualified_pairs = set()
    stop_scan = False
    page_size = 50
    processed = 0
    total_pages = 0
    chunk_number = 0

    def scan_interval(interval_start, interval_end, label):
        """Scan one interval; recursively split it if it nears the 10k cap."""
        nonlocal processed, total_pages, chunk_number
        chunk_number += 1
        chunk_id = chunk_number
        start_ms = int(interval_start.timestamp() * 1000)
        end_ms = int(interval_end.timestamp() * 1000) - 1
        offset = 0
        page_number = 0
        hit_ceiling = False

        log.info("🧩 SCAN CHUNK %s | %s → %s UTC | %s", chunk_id,
                 interval_start.strftime("%Y-%m-%d %H:%M"),
                 interval_end.strftime("%Y-%m-%d %H:%M"), label)

        while True:
            page_number += 1
            total_pages += 1
            try:
                data = get_usdt_transfer_page(
                    start_timestamp=start_ms, end_timestamp=end_ms,
                    start=offset, limit=page_size,
                )
            except Exception as exc:
                log.error("Historical scan request failed in chunk %s page %s: %s", chunk_id, page_number, exc)
                time.sleep(10)
                continue

            transfers = data.get("token_transfers", [])
            if not transfers:
                break

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
                if not wallet_a or not wallet_b or wallet_a == wallet_b:
                    continue

                amount = transfer_amount(tx)
                if amount < MIN_TRANSFER_USD:
                    continue

                timestamp = transaction_timestamp(tx)
                if timestamp and not (start_ms <= timestamp <= end_ms):
                    continue

                pair_key = (wallet_a, wallet_b)
                tx_key = txid or f"{timestamp}:{amount}:{wallet_a}:{wallet_b}"
                pair_stats[pair_key][tx_key] = tx

            processed += new_transactions
            SCAN_CHUNK = chunk_id
            SCAN_PAGE = total_pages
            SCAN_OFFSET = offset
            SCAN_PROCESSED = processed
            SCAN_CANDIDATE_PAIRS = len(pair_stats)
            SCAN_TWO_PLUS_PAIRS = sum(1 for txs in pair_stats.values() if len(txs) >= 2)
            SCAN_ROUTINE_CANDIDATES = 0
            SCAN_CEX_QUALIFIED = 0
            SCAN_LAST_ACTIVITY = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")

            if page_number % 5 == 0 or len(transfers) < page_size:
                log.info("🔎 CHUNK %s | page=%s offset=%s processed=%s | A→B=%s | 2+ transfers=%s",
                         chunk_id, page_number, offset, processed,
                         len(pair_stats), SCAN_TWO_PLUS_PAIRS)

            # Stop as soon as TWO distinct exact A→B relationships reach the
            # strict 100/100 recurring-consistency test. This deliberately
            # avoids scanning the rest of the two-week window once the target
            # has been reached.
            for (candidate_a, candidate_b), candidate_map in pair_stats.items():
                if len(qualified_pairs) >= TARGET_PAIRS:
                    stop_scan = True
                    break
                candidate_key = (candidate_a, candidate_b)
                if candidate_key in qualified_pairs or len(candidate_map) < 3:
                    continue
                candidate_txs = list(candidate_map.values())
                candidate_analysis = human_routine_score(candidate_txs)
                if not candidate_analysis or candidate_analysis["score"] < 100:
                    continue
                candidate_tags = set()
                for candidate_tx in candidate_txs:
                    candidate_tag = get_tag_name(candidate_tx)
                    if candidate_tag:
                        candidate_tags.add(candidate_tag)
                candidate_cex = ", ".join(sorted(candidate_tags)) if candidate_tags else ""
                save_human_candidate(candidate_a, candidate_b, candidate_txs, candidate_cex, candidate_analysis)
                qualified_pairs.add(candidate_key)
                SCAN_ROUTINE_CANDIDATES = len(qualified_pairs)
                log.info("🎯 STRICT 100/100 TARGET %s/%s FOUND: %s → %s | stopping once target count is reached",
                         len(qualified_pairs), TARGET_PAIRS, candidate_a, candidate_b)
                if len(qualified_pairs) >= TARGET_PAIRS:
                    stop_scan = True
                    break

            if stop_scan:
                log.info("🛑 TARGET REACHED: %s/%s strict 100/100 recurring A→B pairs. Historical scan stopped early.",
                         len(qualified_pairs), TARGET_PAIRS)
                break

            offset += page_size
            if len(transfers) < page_size:
                break
            if offset >= 9500:
                hit_ceiling = True
                break
            time.sleep(0.15)

        if hit_ceiling:
            duration = interval_end - interval_start
            if duration <= timedelta(hours=6):
                log.warning("⚠️ Chunk %s reached the 10k ceiling at minimum split size; this interval may be incomplete.", chunk_id)
                return
            midpoint = interval_start + duration / 2
            log.warning("⚠️ Chunk %s reached the 10k API ceiling. Splitting into two smaller windows.", chunk_id)
            scan_interval(interval_start, midpoint, "automatic split")
            scan_interval(midpoint, interval_end, "automatic split")

    # Start with 7-day windows. Busy windows are recursively split as needed.
    chunk_start = start_date
    while chunk_start < end_date:
        chunk_end = min(chunk_start + timedelta(days=7), end_date)
        scan_interval(chunk_start, chunk_end, "7-day base window")
        if stop_scan:
            break
        chunk_start = chunk_end

    # If the scan reached the end of the two-week window before finding two
    # targets, evaluate everything collected and save any remaining strict matches.
    if not stop_scan:
        strict_candidates = []
        for (wallet_a, wallet_b), tx_map in pair_stats.items():
            if (wallet_a, wallet_b) in qualified_pairs or len(tx_map) < 3:
                continue
            txs = list(tx_map.values())
            analysis = human_routine_score(txs)
            if not analysis or analysis["score"] < 100:
                continue
            tags = set()
            for tx in txs:
                tag = get_tag_name(tx)
                if tag:
                    tags.add(tag)
            cex_name = ", ".join(sorted(tags)) if tags else ""
            strict_candidates.append((analysis, wallet_a, wallet_b, txs, cex_name))

        strict_candidates.sort(key=lambda x: (x[0].get("timing_variation_ratio", 0),
                                              x[0].get("distinct_dates", 0),
                                              len(x[3])), reverse=True)
        for analysis, wallet_a, wallet_b, txs, cex_name in strict_candidates:
            if len(qualified_pairs) >= TARGET_PAIRS:
                break
            save_human_candidate(wallet_a, wallet_b, txs, cex_name, analysis)
            qualified_pairs.add((wallet_a, wallet_b))

    human_candidates_found = len(qualified_pairs)
    refresh_active_slots()
    saved = db.execute("SELECT COUNT(*) AS count FROM pairs").fetchone()["count"]
    active = db.execute("SELECT COUNT(*) AS count FROM pairs WHERE active=1").fetchone()["count"]
    pending = db.execute("SELECT COUNT(*) AS count FROM human_candidates WHERE status='pending'").fetchone()["count"]

    SCAN_STATUS = "Complete"
    SCAN_CHUNK = chunk_number
    SCAN_PAGE = total_pages
    SCAN_OFFSET = 0
    SCAN_PROCESSED = processed
    SCAN_CANDIDATE_PAIRS = len(pair_stats)
    SCAN_TWO_PLUS_PAIRS = sum(1 for txs in pair_stats.values() if len(txs) >= 2)
    SCAN_ROUTINE_CANDIDATES = human_candidates_found
    SCAN_CEX_QUALIFIED = 0
    SCAN_LAST_ACTIVITY = SCAN_COMPLETED_AT = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")

    log.info("✅ RECURRING-HUMAN-ACTIVITY SCAN COMPLETE")
    log.info("📊 Processed=%s | A→B relationships=%s | 2+ transfers=%s | Routine candidates=%s",
             processed, len(pair_stats), SCAN_TWO_PLUS_PAIRS, human_candidates_found)
    log.info("👤 Pending manual verification candidates=%s | Existing approved/saved pairs=%s | Active=%s/%s",
             pending, saved, active, TARGET_PAIRS)


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
    """Pair history with optional date filter.

    /history 5
    /history 5 2
    /history 5 2026-08-01
    /history 5 2026-08-01 2
    """
    if len(parts) < 2:
        send_telegram(
            "<b>History commands</b>\n\n"
            "<code>/history 5</code> — all A → B history in the scan window\n"
            "<code>/history 5 2</code> — page 2\n"
            "<code>/history 5 2026-08-01</code> — Pair 5 on August 1\n"
            "<code>/history 5 2026-08-01 2</code> — that day's page 2\n\n"
            "Dates use YYYY-MM-DD and UTC."
        ); return
    try:
        display_number = int(parts[1])
    except ValueError:
        send_telegram("Pair number must be a number."); return
    page = 1; date_text = None
    for value in parts[2:]:
        if value.isdigit(): page = int(value)
        else: date_text = parse_date_text(value)
    if page < 1:
        send_telegram("History page must be 1 or higher."); return
    eligible_pairs = get_display_pairs()
    if not 1 <= display_number <= len(eligible_pairs):
        send_telegram(f"Pair #{display_number} was not found. Use <code>/final</code> to see the current pair numbers."); return
    pair = eligible_pairs[display_number-1]
    a, b = pair["wallet_a"], pair["wallet_b"]
    if any(not x.isdigit() and parse_date_text(x) is None for x in parts[2:]):
        send_telegram("Invalid date. Use YYYY-MM-DD, e.g. <code>2026-08-01</code>."); return
    matching = get_pair_history_for_date(a, b, date_text) if date_text else get_pair_history(a, b, max_pages=200, page_size=50)
    if not matching:
        period = date_text or scan_window_text()
        send_telegram(f"⚠️ <b>No A → B TRON TRC-20 USDT transfers found for Pair #{display_number}.</b>\n\n"
                      f"Wallet A: <code>{telegram_escape(a)}</code>\nWallet B: <code>{telegram_escape(b)}</code>\n\n"
                      f"Date/window checked: {period}\nThe pair has NOT been removed."); return
    title = f"Pair #{display_number} — A → B TRON TRC-20 USDT History"
    subtitle = f"Wallet A: <code>{telegram_escape(a)}</code>\nWallet B: <code>{telegram_escape(b)}</code>"
    if date_text: subtitle += f"\nDate: <b>{date_text} UTC</b>"
    msg = format_history_transactions(title, subtitle, matching, page)
    send_telegram(msg)


def command_wallet(parts):
    """Wallet-wide USDT log, independent of a pair.

    /wallet ADDRESS
    /wallet ADDRESS 2026-08-01
    /wallet ADDRESS 2
    /wallet ADDRESS 2026-08-01 2
    """
    if len(parts) < 2:
        send_telegram("<b>Wallet log</b>\n\n<code>/wallet TRON_ADDRESS</code> — all TRC-20 USDT transfers\n<code>/wallet TRON_ADDRESS 2026-08-01</code> — that day's transfers\n<code>/wallet TRON_ADDRESS 2</code> — page 2\n<code>/wallet TRON_ADDRESS 2026-08-01 2</code> — that day's page 2\n\nDates use YYYY-MM-DD and UTC."); return
    address = parts[1].strip()
    if not (address.startswith("T") and len(address) == 34):
        send_telegram("That doesn't look like a valid TRON address."); return
    page = 1; date_text = None
    for value in parts[2:]:
        if value.isdigit(): page = int(value)
        else: date_text = parse_date_text(value)
    if any(not x.isdigit() and parse_date_text(x) is None for x in parts[2:]):
        send_telegram("Invalid date. Use YYYY-MM-DD, e.g. <code>2026-08-01</code>."); return
    if page < 1:
        send_telegram("History page must be 1 or higher."); return
    txs = get_wallet_history_all(address, max_pages=200, page_size=50, date_text=date_text)
    if not txs:
        period = date_text or "the available history"
        send_telegram(f"No TRON TRC-20 USDT transfers were found for <code>{telegram_escape(address)}</code> for {period}."); return
    title = "Wallet Log — TRON TRC-20 USDT"
    subtitle = f"Wallet: <code>{telegram_escape(address)}</code>"
    if date_text: subtitle += f"\nDate: <b>{date_text} UTC</b>"
    else: subtitle += "\nShowing incoming and outgoing USDT transfers."
    msg = format_history_transactions(title, subtitle, txs, page)
    send_telegram(msg)


def command_candidates(parts=None):
    candidates = get_human_candidates()
    if not candidates:
        send_telegram("No pending human-looking candidates are waiting for manual verification."); return
    header = (f"<b>Human-Looking Routine Candidates</b>\n"
              f"90-day window: {scan_window_text()}\nStrict human-pattern threshold: 100/100\n"
              f"These are suggestions only — CEX labels do NOT automatically disqualify a candidate. Manually verify the A→B history before approving.\n\n")
    blocks = []
    for n, c in enumerate(candidates, 1):
        blocks.append(
            f"<b>Candidate {n}</b> — Routine score: <b>{c['routine_score']:.1f}/100</b>\n"
            f"Wallet A: <code>{telegram_escape(c['wallet_a'])}</code>\n"
            f"Wallet B: <code>{telegram_escape(c['wallet_b'])}</code>\n"
            f"Transfers: {c['transfer_count']} across {c['distinct_dates']} dates\n"
            f"Typical amount: ${c['amount_median']:,.2f} (median spread {c['amount_spread_pct']:.1f}%)\n"
            f"Typical time spread: {c['time_spread_hours']:.2f} hours\n"
            f"Pattern: {c['first_seen']} → {c['last_seen']}\n"
            f"<code>/candidate {n}</code> to inspect this candidate\n"
            f"<code>/approve {n}</code> after you manually verify\n\n")
    current=header
    for block in blocks:
        if len(current)+len(block)>3800:
            send_telegram(current); current=""
        current += block
    if current: send_telegram(current)


def command_candidate(parts):
    """Inspect a human-looking candidate before approving it.

    /candidate 1
    /candidate 1 2026-08-01
    /candidate 1 2
    /candidate 1 2026-08-01 2
    """
    candidates = get_human_candidates()
    if len(parts) < 2:
        send_telegram("Usage: <code>/candidate 1</code> or <code>/candidate 1 2026-08-01</code>."); return
    try: n=int(parts[1])
    except ValueError:
        send_telegram("Candidate number must be a number."); return
    if not 1 <= n <= len(candidates):
        send_telegram("Candidate not found. Use <code>/candidates</code> first."); return
    page=1; date_text=None
    for value in parts[2:]:
        if value.isdigit(): page=int(value)
        else: date_text=parse_date_text(value)
    if any(not x.isdigit() and parse_date_text(x) is None for x in parts[2:]):
        send_telegram("Invalid date. Use YYYY-MM-DD."); return
    if page < 1:
        send_telegram("History page must be 1 or higher."); return
    c=candidates[n-1]
    a,b=c['wallet_a'],c['wallet_b']
    txs=get_pair_history_for_date(a,b,date_text) if date_text else get_pair_history(a,b,max_pages=200,page_size=50)
    if not txs:
        send_telegram(f"No A → B TRON TRC-20 USDT transfers found for Candidate #{n} in {date_text or 'the 90-day scan window'}.\n\nUse <code>/approve {n}</code> only after reviewing the available pattern."); return
    subtitle=(f"Wallet A: <code>{telegram_escape(a)}</code>\nWallet B: <code>{telegram_escape(b)}</code>\n"
              f"Routine score: <b>{c['routine_score']:.1f}/100</b>\n"
              f"Transfers: {c['transfer_count']} across {c['distinct_dates']} dates")
    if date_text: subtitle += f"\nDate: <b>{date_text} UTC</b>"
    msg=format_history_transactions(f"Candidate #{n} — A → B TRON TRC-20 USDT", subtitle, txs, page)
    send_telegram(msg)


def command_approve(parts):
    if len(parts) < 2:
        send_telegram("Usage: <code>/approve 1</code> — approve a human-looking candidate after manual verification."); return
    try: n=int(parts[1])
    except ValueError:
        send_telegram("Candidate number must be a number."); return
    pair_id=approve_human_candidate(n)
    if pair_id is None:
        send_telegram("Candidate not found. Use <code>/candidates</code> first."); return
    send_telegram(f"✅ Candidate #{n} manually approved as Pair #{get_display_number(pair_id)}. It is now eligible for ongoing monitoring.")


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
        f"Pairs with 2+ transfers: {SCAN_TWO_PLUS_PAIRS:,}\nRoutine candidates: {SCAN_ROUTINE_CANDIDATES:,}\nCEX-tagged routine candidates: {SCAN_CEX_QUALIFIED:,}\n"
        f"Scan chunk: {SCAN_CHUNK:,}\nScan page: {SCAN_PAGE:,}\nCurrent offset: {SCAN_OFFSET:,}\nLast scan activity: {SCAN_LAST_ACTIVITY}\n"
        f"Scan started: {SCAN_STARTED_AT}\nScan completed: {SCAN_COMPLETED_AT}\n\n"
        f"<b>Saved / Active</b>\nSaved candidate pairs: {saved}\nActive monitoring slots: {active}/{TARGET_PAIRS}\n"
        f"Monitored wallets: {wallets}\nExcluded pairs: {excluded}\n"
        f"Pending human-looking candidates: {db.execute("SELECT COUNT(*) AS count FROM human_candidates WHERE status='pending'").fetchone()['count']}\n\n<b>Ongoing Monitor</b>\n"
        f"Monitoring cycles completed: {MONITOR_CYCLES:,}\nLast monitoring activity: {MONITOR_LAST_ACTIVITY}\n\n"
        f"Asset monitored: TRON TRC-20 USDT\nHistorical scan dates: {scan_window_text()}\nHuman routine candidate threshold: 100/100\nMinimum transfer: ${MIN_TRANSFER_USD:,.2f}\nTarget pairs: {TARGET_PAIRS}"
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
    elif command == "wallet": command_wallet(parts)
    elif command == "candidates": command_candidates(parts)
    elif command == "candidate": command_candidate(parts)
    elif command == "approve": command_approve(parts)
    elif command == "help":
        send_telegram("<b>Available commands</b>\n\n<code>/info</code> — Show discovered pairs\n<code>/exclude 1</code> — Exclude a pair\n<code>/history 1</code> — All A → B history for Pair #1\n<code>/history 1 2026-08-01</code> — Pair #1 on a specific day\n<code>/history 1 2026-08-01 2</code> — Page 2 of that day\n<code>/wallet ADDRESS</code> — All USDT transfers involving a wallet\n<code>/wallet ADDRESS 2026-08-01</code> — That wallet on a specific day\n<code>/final</code> — Show current eligible pair addresses\n<code>/candidates</code> — Show human-looking routine candidates\n<code>/candidate 1</code> — Inspect candidate 1 history\n<code>/approve 1</code> — Manually approve candidate 1\n<code>/cost</code> — Informational cost estimate\n<code>/status</code> — Monitor status\n<code>/help</code> — Show commands")


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
        f"Minimum transfer: ${MIN_TRANSFER_USD:,.2f}\nHuman-pattern window: {HUMAN_LOOKBACK_DAYS} days\nStrict human routine threshold: 100/100\nStops after {TARGET_PAIRS} qualifying recurring A→B pairs.\n\n"
        "The bot will suggest human-looking routine candidates for manual verification before monitoring."
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

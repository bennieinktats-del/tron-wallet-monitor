import os
import time
import requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from tronpy import Tron
from tronpy.keys import PrivateKey  # REQUIRED for signing transactions
import telegram

# =========================
# SETTINGS
# =========================

TRONGRID_URL = "https://api.trongrid.io"
TRONSCAN_URL = "https://apilist.tronscanapi.com"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# Thresholds and settings
MIN_BALANCE_USD = 500
MIN_TRANSFER_USD = 150
REQUIRED_TRANSFERS = 2
WINDOW_DAYS = 7

# API Keys and Secrets
TRONGRID_API_KEY = "YOUR_TRONGRID_API_KEY"
TRONSCAN_API_KEY = "YOUR_TRONSCAN_API_KEY"
PRIVATE_KEY = "YOUR_PRIVATE_KEY"              # Hex string, with or without '0x'
TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
CHAT_ID = "YOUR_CHAT_ID"

# Initialize Telegram bot
telegram_bot = telegram.Bot(token=TELEGRAM_TOKEN)

# =========================
# HTTP HELPERS
# =========================

def trongrid_get(path, params=None):
    headers = {}
    if TRONGRID_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRONGRID_API_KEY

    url = TRONGRID_URL + path
    try:
        response = requests.get(url, params=params or {}, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching from TRON Grid: {e}")
        return None

def tronscan_get(path, params=None):
    headers = {}
    if TRONSCAN_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRONSCAN_API_KEY

    url = TRONSCAN_URL + path
    try:
        response = requests.get(url, params=params or {}, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching from TRON Scan: {e}")
        return None

# =========================
# TRON ACCOUNT DATA
# =========================

def get_account(address):
    data = trongrid_get(f"/v1/accounts/{address}", {"only_confirmed": "true"})
    if data is None:
        return None
    accounts = data.get("data", [])
    return accounts[0] if accounts else None

def get_trx_balance(address):
    account = get_account(address)
    if not account:
        return 0
    sun = int(account.get("balance", 0))
    return sun / 1_000_000

def get_usdt_balance(address):
    data = trongrid_get(f"/v1/accounts/{address}/trc20/balance", {
        "contract_address": USDT_CONTRACT
    })
    if data is None or not data.get("success"):
        return 0
    
    data_list = data.get("data", [])
    if not data_list:
        return 0
    
    # The API returns a list of dicts like: [{"TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t": "150000000"}]
    item = data_list[0]
    balance_str = item.get(USDT_CONTRACT, "0")
    return int(balance_str) / 1_000_000

# =========================
# USDT TRANSFERS
# =========================

def get_usdt_transfers(address, start_ms, end_ms):
    transfers = []
    fingerprint = None
    while True:
        params = {
            "only_confirmed": "true",
            "only_to": "true",
            "limit": 200,
            "order_by": "block_timestamp,asc",
            "min_timestamp": start_ms,
            "max_timestamp": end_ms,
            "contract_address": USDT_CONTRACT
        }
        if fingerprint:
            params["fingerprint"] = fingerprint

        data = trongrid_get(f"/v1/accounts/{address}/transactions/trc20", params)
        if data is None:
            break

        rows = data.get("data", [])
        for row in rows:
            try:
                amount = int(row.get("value", 0)) / 1_000_000
                transfers.append({
                    "txid": row.get("transaction_id"),
                    "from": row.get("from"),
                    "to": row.get("to"),
                    "amount_usd": amount,
                    "token": "USDT",
                    "timestamp": row.get("block_timestamp", 0)
                })
            except Exception as e:
                print(f"Error processing transfer row: {e}")
                continue

        meta = data.get("meta", {})
        fingerprint = meta.get("fingerprint")
        if not fingerprint or not rows:
            break
    return transfers

# =========================
# QUALIFICATION CHECK
# =========================

def check_wallet(address):
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=WINDOW_DAYS)
    start_ms = int(seven_days_ago.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    print(f"Checking wallet: {address}")

    trx_balance = get_trx_balance(address)
    usdt_balance = get_usdt_balance(address)
    
    # FIX: TRX is not $1. Using a realistic estimate (~$0.25) to prevent false positives.
    # For production, consider fetching the live TRX/USD price via an API like CoinGecko.
    trx_price_usd = 0.25
    current_usd_balance = usdt_balance + (trx_balance * trx_price_usd)

    if current_usd_balance < MIN_BALANCE_USD:
        print(f"Wallet does not meet the balance requirement. Balance: ${current_usd_balance:.2f}")
        return False

    transfers = get_usdt_transfers(address, start_ms, end_ms)
    by_sender = defaultdict(list)

    for transfer in transfers:
        if transfer["amount_usd"] >= MIN_TRANSFER_USD:
            sender = transfer["from"]
            if sender:
                by_sender[sender].append(transfer)

    for sender, sender_transfers in by_sender.items():
        if len(sender_transfers) >= REQUIRED_TRANSFERS:
            print("Wallet qualified!")
            return True

    print("No qualifying sender found.")
    return False

# =========================
# NETWORK DISCOVERY
# =========================

def discover_recent_trx_transfers(start_ms, end_ms, limit=50):
    params = {
        "start": 0,
        "limit": limit,
        "sort": "-timestamp",
        "start_timestamp": start_ms,
        "end_timestamp": end_ms,
        "token": "_"
    }
    data = tronscan_get("/api/transfer", params)
    return data.get("data", []) if data else []

def discover_recent_usdt_transfers(start_ms, end_ms, limit=50):
    params = {
        "start": 0,
        "limit": limit,
        "sort": "-timestamp",
        "start_timestamp": start_ms,
        "end_timestamp": end_ms,
        "contract_address": USDT_CONTRACT
    }
    data = tronscan_get("/api/token_trc20/transfers", params)
    return data.get("data", []) if data else []

# =========================
# SEND TRANSACTION
# =========================

def send_transaction(to_address, amount, token='TRX'):
    tron = Tron()
    
    # Clean the private key string (remove '0x' if present) and convert to bytes
    clean_pk = PRIVATE_KEY.replace('0x', '').replace('0X', '')
    try:
        priv_key = PrivateKey(bytes.fromhex(clean_pk))
    except Exception as e:
        print(f"Invalid private key format: {e}")
        return None
        
    from_address = priv_key.public_key.to_base58check_address()

    try:
        if token == 'TRX':
            # amount is in TRX, convert to SUN (1 TRX = 1,000,000 SUN)
            amount_sun = int(amount * 1_000_000)
            txn = (
                tron.trx.transfer(from_address, to_address, amount_sun)
                .build()
                .sign(priv_key)
            )
        elif token == 'USDT':
            # amount is in USDT, convert to smallest unit (6 decimals)
            amount_unit = int(amount * 1_000_000)
            contract = tron.get_contract(USDT_CONTRACT)
            txn = (
                contract.functions.transfer(to_address, amount_unit)
                .with_owner(from_address)
                .fee_limit(100_000_000)  # 100 TRX fee limit (adjust as needed)
                .build()
                .sign(priv_key)
            )
        else:
            print(f"Unsupported token: {token}")
            return None

        result = txn.broadcast()
        return result
    except Exception as e:
        print(f"Error sending transaction to {to_address}: {e}")
        return None

# =========================
# SEND TELEGRAM ALERT
# =========================

def send_telegram_alert(message):
    try:
        telegram_bot.send_message(chat_id=CHAT_ID, text=message)
    except Exception as e:
        print(f"Error sending Telegram message: {e}")

# =========================
# MAIN
# =========================

def main():
    print("TRON WALLET MONITOR IS RUNNING")

    start_date = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    start_ms = int(start_date.timestamp() * 1000)

    while True:
        try:
            print("Discovering recent TRX transfers...")
            trx_transfers = discover_recent_trx_transfers(start_ms, int(datetime.now(timezone.utc).timestamp() * 1000))
            print(f"TRX transfers discovered: {len(trx_transfers)}")

            print("Discovering recent USDT transfers...")
            usdt_transfers = discover_recent_usdt_transfers(start_ms, int(datetime.now(timezone.utc).timestamp() * 1000))
            print(f"USDT transfers discovered: {len(usdt_transfers)}")

            candidates = set()
            for tx in trx_transfers:
                to_address = tx.get("to")
                if to_address:
                    candidates.add(to_address)

            for tx in usdt_transfers:
                to_address = tx.get("to") or tx.get("toAddress") or tx.get("to_address")
                if to_address:
                    candidates.add(to_address)

            print(f"Candidate wallets discovered: {len(candidates)}")

            for address in candidates:
                try:
                    if check_wallet(address):
                        # Send a transaction of 0.011 TRX to the matching address
                        transaction_result = send_transaction(address, 0.011, token='TRX')
                        txid = transaction_result.txid if transaction_result else "Failed"
                        send_telegram_alert(f"✅ QUALIFIED WALLET\nWallet: {address}\nTransaction TXID: {txid}")
                except Exception as e:
                    print(f"Error checking {address}: {e}")

            print("Waiting 5 minutes before next check...\n")
            time.sleep(300)

        except Exception as e:
            print(f"An error occurred in the main loop: {e}")
            time.sleep(60) # Prevent rapid infinite loop on persistent errors

if __name__ == "__main__":
    main()
import os
import time
import requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from tronpy import Tron
from tronpy.keys import PrivateKey
import telegram

# =========================
# SAFETY CHECK FOR GITHUB ACTIONS
# =========================
REQUIRED_ENV_VARS = ["TRONGRID_API_KEY", "TRONSCAN_API_KEY", "PRIVATE_KEY", "TELEGRAM_TOKEN", "CHAT_ID"]
missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var) or os.getenv(var).startswith("YOUR_")]
if missing_vars:
    print(f"❌ CRITICAL ERROR: Missing or unreplaced environment variables: {', '.join(missing_vars)}")
    print("➡️ Please add these to your GitHub Repository > Settings > Secrets and variables > Actions")
    exit(1)

# =========================
# SETTINGS (Now pulled from Environment Variables)
# =========================
TRONGRID_URL = "https://api.trongrid.io"
TRONSCAN_URL = "https://apilist.tronscanapi.com"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

MIN_BALANCE_USD = 500
MIN_TRANSFER_USD = 150
REQUIRED_TRANSFERS = 2
WINDOW_DAYS = 7

# Pulled securely from GitHub Secrets
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY")
TRONSCAN_API_KEY = os.getenv("TRONSCAN_API_KEY")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Initialize Telegram bot
telegram_bot = telegram.Bot(token=TELEGRAM_TOKEN)

# ... [KEEP THE REST OF THE FUNCTIONS EXACTLY AS PROVIDED IN THE PREVIOUS CORRECTED CODE] ...

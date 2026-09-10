import os
import time
import requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from tronpy import Tron  # Ensure tronpy is installed
import telegram  # Ensure python-telegram-bot is installed

# =========================
# SETTINGS
# =========================

TRONGRID_URL = "https://api.trongrid.io"
TRONSCAN_URL = "https://apilist.tronscanapi.com"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# Thresholds and settings
MIN_BALANCE_USD = 500  # Minimum wallet balance required
MIN_TRANSFER_USD = 150  # Minimum transfer amount to qualify
REQUIRED_TRANSFERS = 2   # Number of qualifying transfers required
WINDOW_DAYS = 7          # Look back period in days

# API Keys and Secrets
TRONGRID_API_KEY = "YOUR_TRONGRID_API_KEY"  # Replace with your TRON Grid API key
TRONSCAN_API_KEY = "YOUR_TRONSCAN_API_KEY"  # Replace with your TRON Scan API key
PRIVATE_KEY = "YOUR_PRIVATE_KEY"              # Replace with your TRON wallet private key
TELEGRAM_TOKEN = "8874535199:AAFMTgsh3G-U3GHNm2jiukeMzBV8SC7VFUk"  # Your Telegram bot token
CHAT_ID = "YOUR_CHAT_ID"                      # Replace with your Telegram chat ID

# Initialize Telegram bot
telegram_bot = telegram.Bot(token=TELEGRAM_TOKEN)

# =========================
# HTTP HELPERS
# =========================

def trongrid_get(path, params=None):
    headers = {}
    if TRONGGRID_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRONGRID_API_KEY

    url = TRONGRID_URL + path
    response = requests.get(url, params=params or {}, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()

def tronscan_get(path, params=None):
    headers = {}
    if TRONSCAN_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRONSCAN_API_KEY

    url = TRONSCAN_URL + path
    response = requests.get(url, params=params or {}, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()

# =========================
# TRON ACCOUNT DATA
# =========================

def get_account(address):
    data = trongrid_get(f"/v1/accounts/{address}", {"only_confirmed": "true"})
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
        "only_confirmed": "true",
        "contract_address": USDT_CONTRACT
    })
    if isinstance(data, dict):
        if "data" in data and isinstance(data["data"], list) and data["data"]:
            item = data["data"][0]
            value = item.get("balance", item.get("amount", 0))
            return int(value) / 1_000_000 if value else 0
    return 0

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
            except Exception:
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
    current_usd_balance = usdt_balance + (trx_balance * 1)  # Assuming TRX is approximately $1 for simplicity

    if current_usd_balance < MIN_BALANCE_USD:
        print("Wallet does not meet the balance requirement.")
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
    return data.get("data", [])

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
    return data.get("data", [])

# =========================
# SEND TRANSACTION
# =========================

def send_transaction(to_address, amount, token='TRX'):
    tron = Tron()
    tron.private_key = PRIVATE_KEY

    if token == 'TRX':
        txn = tron.trx.transfer(to_address, amount)
    elif token == 'USDT':
        txn = tron.trx.contract(USDT_CONTRACT).transfer(to_address, amount)

    txn.sign()
    result = txn.broadcast()
    return result

# =========================
# SEND TELEGRAM ALERT
# =========================

def send_telegram_alert(message):
    telegram_bot.send_message(chat_id=CHAT_ID, text=message)

# =========================
# MAIN
# =========================

def main():
    print("TRON WALLET MONITOR IS RUNNING")

    start_date = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    start_ms = int(start_date.timestamp() * 1000)

    while True:
        # Discover wallets and check conditions
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
                    send_telegram_alert(f"QUALIFIED WALLET\nWallet: {address}\nTransaction Result: {transaction_result}")
            except Exception as e:
                print(f"Error checking {address}: {e}")

        time.sleep(300)  # Wait for 5 minutes before the next check

if __name__ == "__main__":
    main()
    

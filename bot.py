import os
import time
import requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from tronpy import Tron
from tronpy.keys import PrivateKey

# =========================
# 1. LOAD SECRETS FROM GITHUB
# =========================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TRONGRID_API_KEY = os.environ.get("TRONGRID_API_KEY")
TRONSCAN_API_KEY = os.environ.get("TRONSCAN_API_KEY")
PRIVATE_KEY = os.environ.get("PRIVATE_KEY")

if not all([TELEGRAM_BOT_TOKEN, CHAT_ID, TRONGRID_API_KEY, TRONSCAN_API_KEY, PRIVATE_KEY]):
    raise Exception("Missing secrets!")

print(f"✅ Secrets Loaded. Chat ID: {CHAT_ID}")

# =========================
# 2. SETTINGS
# =========================
TRONGRID_URL = "https://api.trongrid.io"
TRONSCAN_URL = "https://apilist.tronscanapi.com"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

MIN_BALANCE_USD = 500
MIN_TRANSFER_USD = 150
REQUIRED_TRANSFERS = 2
WINDOW_DAYS = 7

# =========================
# 3. TELEGRAM USING REQUESTS (No async issues)
# =========================
def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}
    response = requests.post(url, json=data)
    if response.json().get("ok"):
        print("✅ Telegram alert sent!")
    else:
        print(f" Telegram error: {response.json()}")

# =========================
# 4. API HELPERS
# =========================
def trongrid_get(path, params=None):
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY}
    try:
        response = requests.get(TRONGRID_URL + path, params=params or {}, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Trongrid error: {e}")
        return None

def tronscan_get(path, params=None):
    headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
    try:
        response = requests.get(TRONSCAN_URL + path, params=params or {}, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Tronscan error: {e}")
        return None

def get_trx_balance(address):
    data = trongrid_get(f"/v1/accounts/{address}", {"only_confirmed": "true"})
    if not data or not data.get("data"):
        return 0
    sun = int(data["data"][0].get("balance", 0))
    return sun / 1_000_000

def get_usdt_balance(address):
    data = trongrid_get(f"/v1/accounts/{address}/trc20/balance", {"contract_address": USDT_CONTRACT})
    if not data or not data.get("data"):
        return 0
    balance_str = data["data"][0].get(USDT_CONTRACT, "0")
    return int(balance_str) / 1_000_000

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
        if not data:
            break

        rows = data.get("data", [])
        for row in rows:
            try:
                amount = int(row.get("value", 0)) / 1_000_000
                transfers.append({
                    "from": row.get("from"),
                    "amount_usd": amount,
                    "timestamp": row.get("block_timestamp", 0)
                })
            except:
                continue

        meta = data.get("meta", {})
        fingerprint = meta.get("fingerprint")
        if not fingerprint or not rows:
            break
    return transfers

# =========================
# 5. QUALIFICATION CHECK
# =========================
def check_wallet(address):
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=WINDOW_DAYS)
    start_ms = int(seven_days_ago.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    print(f"Checking wallet: {address}")

    trx_balance = get_trx_balance(address)
    usdt_balance = get_usdt_balance(address)
    current_usd_balance = usdt_balance + (trx_balance * 0.25)  # TRX ≈ $0.25

    if current_usd_balance < MIN_BALANCE_USD:
        print(f" Balance too low: ${current_usd_balance:.2f}")
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
            print(" QUALIFIED!")
            return True

    print(" No qualifying sender found.")
    return False

# =========================
# 6. NETWORK DISCOVERY
# =========================
def discover_recent_transfers(start_ms, end_ms, limit=50):
    candidates = set()
    
    # TRX transfers
    data = tronscan_get("/api/transfer", {
        "start": 0, "limit": limit, "sort": "-timestamp",
        "start_timestamp": start_ms, "end_timestamp": end_ms, "token": "_"
    })
    if data:
        for tx in data.get("data", []):
            if tx.get("to"):
                candidates.add(tx["to"])

    # USDT transfers
    data = tronscan_get("/api/token_trc20/transfers", {
        "start": 0, "limit": limit, "sort": "-timestamp",
        "start_timestamp": start_ms, "end_timestamp": end_ms,
        "contract_address": USDT_CONTRACT
    })
    if data:
        for tx in data.get("data", []):
            to_addr = tx.get("to") or tx.get("toAddress") or tx.get("to_address")
            if to_addr:
                candidates.add(to_addr)

    return candidates

# =========================
# 7. SEND TRANSACTION
# =========================
def send_transaction(to_address, amount, token='TRX'):
    tron = Tron()
    clean_pk = PRIVATE_KEY.replace('0x', '').replace('0X', '')
    try:
        priv_key = PrivateKey(bytes.fromhex(clean_pk))
    except Exception as e:
        print(f"Invalid private key: {e}")
        return None
        
    from_address = priv_key.public_key.to_base58check_address()

    try:
        if token == 'TRX':
            amount_sun = int(amount * 1_000_000)
            txn = (
                tron.trx.transfer(from_address, to_address, amount_sun)
                .build()
                .sign(priv_key)
            )
        else:
            print("Only TRX transfers supported for now")
            return None

        result = txn.broadcast()
        return result
    except Exception as e:
        print(f"Transaction error: {e}")
        return None

# =========================
# 8. MAIN FUNCTION
# =========================
def main():
    print("🚀 TRON WALLET MONITOR STARTING...")
    send_telegram_alert("✅ TRON Wallet Monitor is ONLINE!")
    
    start_date = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    start_ms = int(start_date.timestamp() * 1000)

    while True:
        try:
            print("\n=== Scanning for new transfers... ===")
            current_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            candidates = discover_recent_transfers(start_ms, current_ms)
            
            print(f"Found {len(candidates)} candidate wallets")

            for address in candidates:
                try:
                    if check_wallet(address):
                        print(f"Sending reward to {address}...")
                        result = send_transaction(address, 0.011, token='TRX')
                        txid = result.txid if result else "Failed"
                        
                        msg = f"✅ QUALIFIED WALLET\n\nAddress: {address}\nTXID: {txid}"
                        send_telegram_alert(msg)
                except Exception as e:
                    print(f"Error checking {address}: {e}")

            print(f"\n⏳ Waiting 5 minutes...")
            time.sleep(300)

        except Exception as e:
            print(f"Main loop error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()

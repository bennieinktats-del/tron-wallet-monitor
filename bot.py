import os
import time
import requests
from datetime import datetime, timedelta, timezone

# =========================
# 🟢 USER SETTINGS
# =========================
TARGET_PAIRS = 5
MIN_AMOUNT_USDT = 100  # Look for transfers over 100 USDT

# =========================
# 1. LOAD SECRETS
# =========================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TRONSCAN_API_KEY = os.environ.get("TRONSCAN_API_KEY")

if not all([TELEGRAM_BOT_TOKEN, CHAT_ID, TRONSCAN_API_KEY]):
    raise Exception("Missing Secrets!")

TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def send_telegram_alert(message):
    url = f"{TELEGRAM_URL}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=data, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

# =========================
# 2. MAIN SCANNER (WHALE HUNTER)
# =========================
def main():
    print("🚀 Starting Whale Hunter...")
    send_telegram_alert(f"🚀 <b>Whale Hunter Started</b>\nLooking for: <b>Transfers > {MIN_AMOUNT_USDT} USDT</b>")
    
    found_pairs = []
    
    # Calculate time window (Last 24 hours)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000)
    
    headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
    
    print("📡 Fetching data...")
    try:
        # Fetch a large batch
        response = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={
                "start": 0, 
                "limit": 500, 
                "sort": "-timestamp",
                "start_timestamp": start_ms, 
                "end_timestamp": end_ms,
                "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
            },
            headers=headers, 
            timeout=30
        )
        
        data = response.json()
        transfers = data.get("data", [])
        print(f"✅ Received {len(transfers)} transactions!")
        
        # Scan for large amounts
        for tx in transfers:
            if len(found_pairs) >= TARGET_PAIRS:
                break
                
            # Get amount (TronScan uses 'quant' for USDT usually, divided by 10^6)
            amount_raw = tx.get("quant") or tx.get("amount") or 0
            amount_usdt = int(amount_raw) / 1_000_000
            
            if amount_usdt >= MIN_AMOUNT_USDT:
                from_addr = tx.get("from_address") or tx.get("from")
                to_addr = tx.get("to_address") or tx.get("to")
                
                print(f"🎯 WHALE FOUND! {amount_usdt} USDT from {from_addr} to {to_addr}")
                
                found_pairs.append({
                    "sender": from_addr,
                    "receiver": to_addr,
                    "amount": amount_usdt
                })
                
                send_telegram_alert(f"✅ <b>Whale #{len(found_pairs)}/{TARGET_PAIRS} Caught!</b>\n<b>Amount:</b> {amount_usdt} USDT\n<b>From:</b> <code>{from_addr}</code>\n<b>To:</b> <code>{to_addr}</code>")
                
    except Exception as e:
        print(f" Error: {e}")
        send_telegram_alert(f"❌ Error: {e}")
        return

    if len(found_pairs) > 0:
        send_telegram_alert(f"🏁 <b>Hunt Complete!</b>\nCaught {len(found_pairs)} whales.")
    else:
        send_telegram_alert("⚠️ Hunt finished but found no whales > 100 USDT in this batch.")

if __name__ == "__main__":
    main()

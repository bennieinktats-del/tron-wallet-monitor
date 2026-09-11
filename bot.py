import os
import json
import requests
from datetime import datetime, timedelta, timezone

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
# 2. DATA INSPECTOR
# =========================
def main():
    print("🔍 Inspecting TronScan API data structure...")
    send_telegram_alert(" <b>Data Inspector Started</b>\nFetching sample transactions to see field names...")
    
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp() * 1000)
    
    headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
    
    try:
        response = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={
                "start": 0, 
                "limit": 5, 
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
        
        print(f"✅ Found {len(transfers)} transfers")
        
        if transfers:
            # Print the first transaction as formatted JSON
            first_tx = transfers[0]
            print("\n=== FIRST TRANSACTION (ALL FIELDS) ===")
            print(json.dumps(first_tx, indent=2))
            
            # Send to Telegram (truncated)
            tx_json = json.dumps(first_tx, indent=2)
            if len(tx_json) > 4000:
                tx_json = tx_json[:4000] + "\n...(truncated)"
            
            send_telegram_alert(f"✅ <b>API Working!</b>\n\n<b>First Transaction Fields:</b>\n\n<pre>{tx_json}</pre>")
            
            # Also print all available keys
            print("\n=== ALL FIELD NAMES ===")
            print(list(first_tx.keys()))
            
    except Exception as e:
        print(f"❌ Error: {e}")
        send_telegram_alert(f" Error: {e}")

if __name__ == "__main__":
    main()

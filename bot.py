import os
import requests
from datetime import datetime, timedelta, timezone

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TRONSCAN_API_KEY = os.environ.get("TRONSCAN_API_KEY")

TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def send_telegram_alert(message):
    url = f"{TELEGRAM_URL}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}
    requests.post(url, json=data, timeout=10)

def main():
    print(" Simple Inspector...")
    send_telegram_alert("🔍 Testing API...")
    
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000)
    
    headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
    
    try:
        response = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={
                "start": 0, 
                "limit": 1,
                "sort": "-timestamp",
                "start_timestamp": start_ms, 
                "end_timestamp": end_ms,
                "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
            },
            headers=headers, 
            timeout=30
        )
        
        print(f"Status: {response.status_code}")
        data = response.json()
        transfers = data.get("data", [])
        
        send_telegram_alert(f"Status Code: {response.status_code}\nTotal Transfers: {len(transfers)}")
        
        if transfers:
            tx = transfers[0]
            # Show just the important fields
            msg = "✅ Got data!\n\n"
            msg += f"From: {tx.get('from_address', 'N/A')}\n"
            msg += f"To: {tx.get('to_address', 'N/A')}\n"
            msg += f"Amount: {tx.get('quant', 'N/A')}\n"
            msg += f"Token Name: {tx.get('token_info', {}).get('name', 'N/A')}\n"
            msg += f"\nALL KEYS: {list(tx.keys())}"
            
            send_telegram_alert(msg)
        else:
            send_telegram_alert("❌ API returned empty list!")
            
    except Exception as e:
        send_telegram_alert(f"❌ Error: {str(e)}")

if __name__ == "__main__":
    main()

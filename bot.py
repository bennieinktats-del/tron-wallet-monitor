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
    print("Testing different API methods...")
    send_telegram_alert("🔍 Testing different API query methods...")
    
    headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
    
    # Method 1: Without date filters
    try:
        send_telegram_alert("📌 Method 1: No date filters...")
        response1 = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={
                "start": 0, 
                "limit": 5,
                "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
            },
            headers=headers, 
            timeout=30
        )
        data1 = response1.json()
        transfers1 = data1.get("data", [])
        send_telegram_alert(f"Method 1 (No dates): {len(transfers1)} transfers\nStatus: {response1.status_code}")
        
        if transfers1:
            tx = transfers1[0]
            msg = "✅ SUCCESS! Sample data:\n"
            msg += f"From: {tx.get('from_address', 'N/A')}\n"
            msg += f"To: {tx.get('to_address', 'N/A')}\n"
            msg += f"Amount: {tx.get('quant', 'N/A')}\n"
            send_telegram_alert(msg)
            
    except Exception as e:
        send_telegram_alert(f"Method 1 Error: {e}")
    
    # Method 2: Using Trongrid instead
    try:
        send_telegram_alert("\n📌 Method 2: Using Trongrid API...")
        response2 = requests.get(
            "https://api.trongrid.io/v1/transactions/trc20",
            params={
                "limit": 5,
                "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
            },
            headers=headers, 
            timeout=30
        )
        data2 = response2.json()
        transfers2 = data2.get("data", [])
        send_telegram_alert(f"Method 2 (Trongrid): {len(transfers2)} transfers\nStatus: {response2.status_code}")
        
    except Exception as e:
        send_telegram_alert(f"Method 2 Error: {e}")

if __name__ == "__main__":
    main()

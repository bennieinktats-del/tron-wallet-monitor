import os
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TRONSCAN_API_KEY = os.environ.get("TRONSCAN_API_KEY")

TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def send_telegram_alert(message):
    url = f"{TELEGRAM_URL}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}
    requests.post(url, json=data, timeout=10)

def main():
    print("Testing alternative API endpoints...")
    send_telegram_alert("🔍 Testing alternative endpoints...")
    
    headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
    
    # Try 1: TronScan without contract filter
    try:
        send_telegram_alert("📌 Trying: All token transfers (no contract filter)...")
        r1 = requests.get(
            "https://apilist.tronscanapi.com/api/transfer",
            params={"start": 0, "limit": 5},
            headers=headers, timeout=30
        )
        d1 = r1.json()
        send_telegram_alert(f"All transfers: {len(d1.get('data', []))}\nKeys: {list(d1.keys())}")
    except Exception as e:
        send_telegram_alert(f"Error 1: {e}")
    
    # Try 2: Get USDT token info first
    try:
        send_telegram_alert("\n📌 Trying: Get USDT token info...")
        r2 = requests.get(
            "https://apilist.tronscanapi.com/api/token",
            params={"token": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"},
            headers=headers, timeout=30
        )
        d2 = r2.json()
        send_telegram_alert(f"Token info status: {r2.status_code}\nData: {d2}")
    except Exception as e:
        send_telegram_alert(f"Error 2: {e}")
        
    # Try 3: Direct USDT transfers with different endpoint
    try:
        send_telegram_alert("\n📌 Trying: Direct USDT endpoint...")
        r3 = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={
                "relatedAddress": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                "start": 0,
                "limit": 5
            },
            headers=headers, timeout=30
        )
        d3 = r3.json()
        send_telegram_alert(f"Direct USDT: {len(d3.get('data', []))}\nFull response: {d3}")
    except Exception as e:
        send_telegram_alert(f"Error 3: {e}")

if __name__ == "__main__":
    main()

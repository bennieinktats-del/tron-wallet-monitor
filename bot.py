import os
import time
import requests
from datetime import datetime, timedelta, timezone
from tronpy import Tron
from tronpy.keys import PrivateKey

# =========================
# 🟢 USER SETTINGS
# =========================
TARGET_PAIRS = 5
MIN_BALANCE_USD = 0
MIN_TRANSFER_USD = 1
WINDOW_DAYS = 7
GAS_COST_PER_PAIR_TRX = 2.2

CEX_KEYWORDS = ['binance', 'okx', 'huobi', 'htx', 'gate', 'kucoin', 'bybit', 'mexc', 'bitfinex', 'coinbase', 'kraken', 'bitget', 'poloniex']

# =========================
# 1. LOAD SECRETS
# =========================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TRONGRID_API_KEY = os.environ.get("TRONGRID_API_KEY")
TRONSCAN_API_KEY = os.environ.get("TRONSCAN_API_KEY")
PRIVATE_KEY = os.environ.get("PRIVATE_KEY")

if not all([TELEGRAM_BOT_TOKEN, CHAT_ID, TRONGRID_API_KEY, TRONSCAN_API_KEY, PRIVATE_KEY]):
    raise Exception("Missing GitHub Secrets!")

print("✅ All secrets loaded successfully!")

tron = Tron()
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# =========================
# 2. TELEGRAM
# =========================
def send_telegram_alert(message):
    url = f"{TELEGRAM_URL}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=data, timeout=10)
        print(f"Telegram response: {response.json()}")
    except Exception as e:
        print(f"Telegram error: {e}")

# =========================
# 3. MAIN FUNCTION - SUPER SIMPLE
# =========================
def main():
    print("🚀 TRON MONITOR STARTING...")
    send_telegram_alert("🚀 TRON Monitor Started - Testing...")
    
    # Test 1: Fetch recent USDT transfers
    print("📡 Fetching recent USDT transfers from TronScan...")
    
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).timestamp() * 1000)
    
    headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
    
    try:
        response = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={
                "start": 0, 
                "limit": 10, 
                "sort": "-timestamp",
                "start_timestamp": start_ms, 
                "end_timestamp": end_ms,
                "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
            },
            headers=headers, 
            timeout=30
        )
        
        print(f"API Status Code: {response.status_code}")
        
        data = response.json()
        transfers = data.get("data", [])
        
        print(f"✅ Found {len(transfers)} transfers!")
        
        # Show first transfer as test
        if transfers:
            first_tx = transfers[0]
            print(f"Sample TX - From: {first_tx.get('from')}")
            print(f"Sample TX - To: {first_tx.get('to')}")
            print(f"Sample TX - Amount: {first_tx.get('quant')}")
            
            send_telegram_alert(f"✅ API Working!\nFound {len(transfers)} transfers\n\nSample:\nFrom: {first_tx.get('from')[:20]}...\nTo: {first_tx.get('to')[:20]}...")
        
    except Exception as e:
        print(f"❌ API Error: {e}")
        send_telegram_alert(f" Error: {e}")
        return
    
    print("✅ Test complete!")
    send_telegram_alert("✅ Bot is working correctly!")

if __name__ == "__main__":
    main()


import os
import time
import requests
from tronpy import Tron
from tronpy.keys import PrivateKey

TARGET_PAIRS = 5
CEX_KEYWORDS = ['binance', 'okx', 'huobi', 'htx', 'gate', 'kucoin', 'bybit', 'mexc', 'bitfinex', 'coinbase', 'kraken', 'bitget', 'poloniex']

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TRONSCAN_API_KEY = os.environ.get("TRONSCAN_API_KEY")
PRIVATE_KEY = os.environ.get("PRIVATE_KEY")

print("✅ Starting...")
tron = Tron()
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def send_telegram(message):
    try:
        requests.post(f"{TELEGRAM_URL}/sendMessage", json={
            "chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"
        }, timeout=10)
    except: pass

def main():
    print("🚀 Scanner starting...")
    send_telegram("🚀 <b>Debug Scanner Started</b>")
    
    # Test 1: Can we get transfers?
    print("Test 1: Fetching transfers...")
    headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
    
    try:
        r = requests.get("https://apilist.tronscanapi.com/api/transfer", 
                        params={"start": 0, "limit": 10},
                        headers=headers, timeout=30)
        transfers = r.json().get("data", [])
        print(f"✅ Got {len(transfers)} transfers")
        send_telegram(f"✅ Got {len(transfers)} transfers from API")
        
        if transfers:
            # Show first transfer
            tx = transfers[0]
            print(f"Sample: {tx}")
            msg = "📄 <b>Sample Transfer:</b>\n"
            for key, value in tx.items():
                msg += f"<b>{key}</b>: <code>{str(value)[:50]}</code>\n"
            send_telegram(msg)
            
    except Exception as e:
        print(f"❌ Error: {e}")
        send_telegram(f"❌ Error: {e}")
        return
    
    # Test 2: Check if API returns from_address and to_address
    if transfers:
        tx = transfers[0]
        from_addr = tx.get("from_address")
        to_addr = tx.get("to_address")
        
        print(f"From: {from_addr}")
        print(f"To: {to_addr}")
        
        send_telegram(f" <b>Fields check:</b>\nFrom: <code>{from_addr}</code>\nTo: <code>{to_addr}</code>")
    
    send_telegram("✅ Debug complete! Check logs for details.")

if __name__ == "__main__":
    main()

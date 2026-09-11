import os
import sys

print("=== STEP 1: Imports ===")
try:
    import requests
    print("✅ requests imported")
except Exception as e:
    print(f"❌ requests failed: {e}")
    sys.exit(1)

try:
    from tronpy import Tron
    from tronpy.keys import PrivateKey
    print("✅ tronpy imported")
except Exception as e:
    print(f"❌ tronpy failed: {e}")
    sys.exit(1)

print("\n=== STEP 2: Environment ===")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TRONSCAN_API_KEY = os.getenv("TRONSCAN_API_KEY")

print(f"Telegram Token: {'✅ Set' if TELEGRAM_BOT_TOKEN else '❌ Missing'}")
print(f"Chat ID: {'✅ Set' if CHAT_ID else ' Missing'}")
print(f"TronScan Key: {'✅ Set' if TRONSCAN_API_KEY else '❌ Missing'}")

if not all([TELEGRAM_BOT_TOKEN, CHAT_ID, TRONSCAN_API_KEY]):
    print("❌ Missing required secrets!")
    sys.exit(1)

print("\n=== STEP 3: API Test ===")
try:
    r = requests.get(
        "https://apilist.tronscanapi.com/api/token_trc20/transfers",
        params={"start": 0, "limit": 5, "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"},
        timeout=30
    )
    print(f"API Status: {r.status_code}")
    data = r.json()
    transfers = data.get("token_transfers", [])
    print(f"Transfers found: {len(transfers)}")
    
    if transfers:
        print("✅ API WORKING!")
        # Send success to Telegram
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": f"✅ Bot is working! Found {len(transfers)} transfers"
            },
            timeout=10
        )
        print("✅ Telegram message sent")
    else:
        print("⚠️ API returned empty")
        
except Exception as e:
    print(f"❌ API test failed: {e}")
    sys.exit(1)

print("\n=== ALL TESTS PASSED ===")

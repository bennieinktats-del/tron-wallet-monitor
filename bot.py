import os
import requests

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

print("🔍 Testing PUBLIC TronScan API (no key needed)...")

# Try PUBLIC endpoint - no API key needed
r = requests.get(
    "https://apilist.tronscanapi.com/api/token_trc20/transfers",
    params={
        "start": 0,
        "limit": 5,
        "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    },
    timeout=30
)

print(f"Status: {r.status_code}")
data = r.json()
transfers = data.get("data", [])

print(f"Found {len(transfers)} transfers")

if transfers:
    tx = transfers[0]
    print(f"\n✅ SUCCESS!")
    print(f"From: {tx.get('from')}")
    print(f"To: {tx.get('to')}")
    
    msg = f"✅ API Works!\nFound {len(transfers)} USDT transfers\n\nFrom: {tx.get('from')[:20]}...\nTo: {tx.get('to')[:20]}..."
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", 
                  json={"chat_id": CHAT_ID, "text": msg})
else:
    print(f"\n❌ Still empty!")
    print(f"Response: {data}")
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", 
                  json={"chat_id": CHAT_ID, "text": f"❌ API returned: {data}"})

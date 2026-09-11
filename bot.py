import os
import requests
from datetime import datetime, timezone

# =========================
# SETTINGS - AS SIMPLE AS IT GETS
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TRONSCAN_API_KEY = os.getenv("TRONSCAN_API_KEY")

print("✅ Script started - Testing API...")

# Just fetch recent USDT transfers - NO FILTERS
headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
r = requests.get(
    "https://apilist.tronscanapi.com/api/token_trc20/transfers",
    params={"start": 0, "limit": 20, "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"},
    headers=headers,
    timeout=30
)

print(f"API Status: {r.status_code}")
data = r.json()
transfers = data.get("data", [])

print(f"Found {len(transfers)} transfers")

# Show first transfer
if transfers:
    tx = transfers[0]
    print(f"\nSample Transfer:")
    print(f"  From: {tx.get('from')}")
    print(f"  To: {tx.get('to')}")
    print(f"  Amount: {tx.get('quant')}")
    print(f"  All keys: {list(tx.keys())}")
    
    # Send to Telegram
    msg = f"✅ API Working!\nFound {len(transfers)} transfers\n\nFrom: {tx.get('from')}\nTo: {tx.get('to')}"
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", 
                  json={"chat_id": CHAT_ID, "text": msg})
else:
    print("❌ No transfers found!")
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", 
                  json={"chat_id": CHAT_ID, "text": " API returned empty!"})

print("✅ Done")

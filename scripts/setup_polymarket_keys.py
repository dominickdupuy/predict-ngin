#!/usr/bin/env python3
"""
Generate Polymarket L2 API credentials and write them back to .env.

Usage:
    python scripts/setup_polymarket_keys.py
"""

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from py_clob_client.client import ClobClient

# ── load .env ─────────────────────────────────────────────────────────────────
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

# ── config ────────────────────────────────────────────────────────────────────
HOST      = "https://clob.polymarket.com"   # must be CLOB host, not polymarket.com
CHAIN_ID  = 137                             # Polygon mainnet

private_key       = os.getenv("PORTFOLIO_PRIVATE_KEY") or os.getenv("POLYMARKET_PRIVATE_KEY", "")
portfolio_address = os.getenv("POLYMARKET_ADDRESS", "")  # checksummed funder address

if not private_key or private_key in ("0x...",):
    print("ERROR: set PORTFOLIO_PRIVATE_KEY (or POLYMARKET_PRIVATE_KEY) in .env")
    sys.exit(1)

if not private_key.startswith("0x"):
    private_key = "0x" + private_key

# ── build client and create L2 key ───────────────────────────────────────────
print(f"Connecting to {HOST} ...")

client = ClobClient(
    host=HOST,
    key=private_key,
    chain_id=CHAIN_ID,
    signature_type=1,           # 1 = POLY_PROXY (Magic / Google / email login)
    funder=portfolio_address,   # proxy/deposit wallet address
)

try:
    creds = client.derive_api_key()
except Exception as exc:
    print(f"\nERROR: {exc}")
    print("\nCommon causes:")
    print("  • Wrong private key or wallet not connected to polymarket.com")
    sys.exit(1)

api_key    = creds.api_key
secret     = creds.api_secret
passphrase = creds.api_passphrase

print("\n" + "=" * 60)
print("  L2 credentials created — writing to .env")
print("=" * 60)
print(f"  POLYMARKET_API_KEY={api_key}")
print(f"  POLYMARKET_API_SECRET={secret}")
print(f"  POLYMARKET_API_PASSPHRASE={passphrase}")

# ── write back to .env ────────────────────────────────────────────────────────
env_text = _env_path.read_text()

def _set(text: str, key: str, value: str) -> str:
    pattern = rf"^({re.escape(key)}=).*"
    replacement = rf"\g<1>{value}"
    new_text, n = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if n == 0:
        new_text = text.rstrip() + f"\n{key}={value}\n"
    return new_text

env_text = _set(env_text, "POLYMARKET_API_KEY",        api_key)
env_text = _set(env_text, "POLYMARKET_API_SECRET",      secret)
env_text = _set(env_text, "POLYMARKET_API_PASSPHRASE",  passphrase)

_env_path.write_text(env_text)
print("\n.env updated. You can now run live trading.")
print("=" * 60)

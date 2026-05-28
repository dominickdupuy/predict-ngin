#!/usr/bin/env python3
"""
Mock data server for testing the dashboard.

Simulates live trading: opens/closes positions, logs trades,
and writes a growing equity curve. Run this alongside the dashboard.

Usage:
    python scripts/live/mock_data_server.py
"""

import json
import math
import random
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

_root = Path(__file__).resolve().parents[2]
DATA_DIR = _root / "data" / "live"
DATA_DIR.mkdir(parents=True, exist_ok=True)

POSITIONS_FILE  = DATA_DIR / "positions.json"
TRADES_FILE     = DATA_DIR / "trades.jsonl"
EQUITY_LOG_FILE = DATA_DIR / "equity_log.jsonl"

random.seed(42)

MARKETS = [
    {"id": "0xabc001", "title": "Will the Fed cut rates in June 2026?",        "category": "Finance"},
    {"id": "0xabc002", "title": "Will Ethereum hit $5000 by end of Q2 2026?",  "category": "Crypto"},
    {"id": "0xabc003", "title": "Will Trump sign the tax bill before July?",    "category": "Politics"},
    {"id": "0xabc004", "title": "Will GPT-5 be released before July 2026?",    "category": "Technology"},
    {"id": "0xabc005", "title": "Will there be a US recession in 2026?",       "category": "Economics"},
    {"id": "0xabc006", "title": "Will oil hit $100/barrel by September 2026?", "category": "Commodities"},
    {"id": "0xabc007", "title": "Will China GDP growth exceed 5% in 2026?",    "category": "Geopolitics"},
    {"id": "0xabc008", "title": "Will Bitcoin exceed $120k in 2026?",          "category": "Crypto"},
    {"id": "0xabc009", "title": "Will Apple release AR glasses in 2026?",      "category": "Technology"},
    {"id": "0xabc010", "title": "Will the S&P 500 hit 7000 before July?",      "category": "Finance"},
]

WHALES = [
    "0xf4a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9",
    "0xa9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a1b0",
    "0x1234567890abcdef1234567890abcdef12345678",
    "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    "0xcafe0000cafe1111cafe2222cafe3333cafe4444",
]


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2, default=str))


def _append_jsonl(path: Path, record: dict):
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _clear_file(path: Path):
    path.write_text("")


# ── Seed historical equity curve (past 30 days) ──────────────────────────────

def _seed_equity_history(initial_capital: float = 1000.0):
    _clear_file(EQUITY_LOG_FILE)
    equity = initial_capital
    now = datetime.now(timezone.utc)
    # Generate ~4 data points per hour for past 30 days = ~2880 points
    points = 24 * 4 * 30
    start  = now - timedelta(days=30)
    for i in range(points):
        # Random walk with slight upward drift
        pct = random.gauss(0.0008, 0.008)
        equity = max(equity * (1 + pct), initial_capital * 0.5)
        ts = (start + timedelta(minutes=15 * i)).isoformat()
        _append_jsonl(EQUITY_LOG_FILE, {"ts": ts, "equity": round(equity, 2), "positions": random.randint(0, 4), "deployed": round(equity * random.uniform(0.1, 0.4), 2)})
    return equity


# ── Seed closed trade history ─────────────────────────────────────────────────

def _seed_trade_history():
    _clear_file(TRADES_FILE)
    now = datetime.now(timezone.utc)
    closed_trades = []

    for i in range(25):
        mkt = random.choice(MARKETS)
        whale = random.choice(WHALES)
        side = random.choice(["BUY", "SELL"])
        ep = round(random.uniform(0.05, 0.75), 3)
        size = round(random.uniform(20, 200), 2)
        days_ago = random.uniform(1, 28)
        entry_ts = (now - timedelta(days=days_ago)).isoformat()

        # Open record
        _append_jsonl(TRADES_FILE, {
            "action": "OPEN",
            "market_id": mkt["id"],
            "market_title": mkt["title"],
            "category": mkt["category"],
            "direction": side,
            "price": ep,
            "whale_address": whale,
            "whale_winrate": round(random.uniform(0.60, 0.85), 3),
            "whale_score": round(random.uniform(0.5, 4.0), 2),
            "size_usd": size,
            "ts": entry_ts,
        })

        # Weighted toward wins (simulate ~65% win rate)
        won = random.random() < 0.65
        if side == "BUY":
            exit_price = round(random.uniform(0.85, 0.99), 3) if won else 0.0
            shares = size / max(ep, 1e-6)
            gross = (exit_price - ep) * shares
        else:
            entry_no = 1.0 - ep
            exit_price_no = round(random.uniform(0.85, 0.99), 3) if won else 0.0
            shares = size / max(entry_no, 1e-6)
            gross = (exit_price_no - entry_no) * shares

        net_pnl = round(gross * 0.97, 2)
        exit_ts = (now - timedelta(days=days_ago - random.uniform(0.5, 10))).isoformat()
        reason  = random.choice(["RESOLVED_YES", "RESOLVED_NO", "WHALE_EXIT", "PARTIAL_EXIT"])

        _append_jsonl(TRADES_FILE, {
            "action": "CLOSE",
            "market_id": mkt["id"],
            "market_title": mkt["title"],
            "reason": reason,
            "exit_price": exit_price if side == "BUY" else (1 - exit_price_no),
            "net_pnl": net_pnl,
            "ts": exit_ts,
        })
        closed_trades.append(net_pnl)

    return closed_trades


# ── Seed open positions ───────────────────────────────────────────────────────

def _seed_positions(capital: float):
    open_mkts = random.sample(MARKETS, 3)
    positions = []
    deployed = 0.0
    for mkt in open_mkts:
        side  = random.choice(["BUY", "SELL"])
        ep    = round(random.uniform(0.10, 0.70), 3)
        size  = round(capital * random.uniform(0.04, 0.09), 2)
        days  = random.uniform(0.5, 5.0)
        entry_ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        whale = random.choice(WHALES)
        positions.append({
            "market_id":    mkt["id"],
            "market_title": mkt["title"],
            "category":     mkt["category"],
            "side":         side,
            "entry_price":  ep,
            "size_usd":     size,
            "whale_address": whale,
            "whale_score":  round(random.uniform(0.5, 4.0), 2),
            "entry_date":   entry_ts,
            "whale_winrate": round(random.uniform(0.60, 0.85), 3),
            "partial_exit_done": False,
        })
        deployed += size
        _append_jsonl(TRADES_FILE, {
            "action": "OPEN",
            "market_id": mkt["id"],
            "market_title": mkt["title"],
            "category": mkt["category"],
            "direction": side,
            "price": ep,
            "whale_address": whale,
            "whale_winrate": round(random.uniform(0.60, 0.85), 3),
            "whale_score": round(random.uniform(0.5, 4.0), 2),
            "size_usd": size,
            "ts": entry_ts,
        })

    state = {
        "total_capital":    round(capital, 2),
        "positions":        positions,
        "category_exposure": {p["category"]: p["size_usd"] for p in positions},
        "whale_exposure":    {p["whale_address"]: p["size_usd"] for p in positions},
        "market_exposure":   {p["market_id"]: p["size_usd"] for p in positions},
        "tier_exposure":     {},
        "saved_at":         _now_iso(),
    }
    _write_json(POSITIONS_FILE, state)
    return state


# ── Live simulation loop ──────────────────────────────────────────────────────

def _simulate(state: dict, tick: int):
    """
    Each tick (every few seconds):
    - Update equity with small random walk
    - Occasionally close a position (resolved/exit)
    - Occasionally open a new position
    - Log equity snapshot
    """
    positions = state["positions"]
    capital   = state["total_capital"]
    equity_log = _read_last_equity()
    equity = equity_log if equity_log else capital

    # Random walk equity
    pct    = random.gauss(0.001, 0.012)
    equity = max(equity * (1 + pct), capital * 0.4)

    # ── Maybe close a position ──────────────────────────────────────────────
    if positions and random.random() < 0.15:
        pos = random.choice(positions)
        positions.remove(pos)

        side = pos["side"]
        ep   = float(pos["entry_price"])
        size = float(pos["size_usd"])
        won  = random.random() < 0.65

        if side == "BUY":
            exit_price = round(random.uniform(0.85, 0.99), 3) if won else 0.0
            gross = (exit_price - ep) * (size / max(ep, 1e-6))
        else:
            entry_no    = 1.0 - ep
            exit_p_no   = round(random.uniform(0.85, 0.99), 3) if won else 0.0
            exit_price  = 1.0 - exit_p_no
            gross = (exit_p_no - entry_no) * (size / max(entry_no, 1e-6))

        net_pnl = round(gross * 0.97, 2)
        reason  = random.choice(["RESOLVED_YES", "RESOLVED_NO", "WHALE_EXIT"])
        capital = round(capital + net_pnl, 2)

        _append_jsonl(TRADES_FILE, {
            "action": "CLOSE",
            "market_id": pos["market_id"],
            "market_title": pos.get("market_title", ""),
            "reason": reason,
            "exit_price": exit_price,
            "net_pnl": net_pnl,
            "ts": _now_iso(),
        })
        print(f"  CLOSE  {pos.get('market_title','')[:40]}  pnl=${net_pnl:+.2f}  reason={reason}")

    # ── Maybe open a new position ───────────────────────────────────────────
    open_ids = {p["market_id"] for p in positions}
    available = [m for m in MARKETS if m["id"] not in open_ids]
    if available and len(positions) < 6 and random.random() < 0.12:
        mkt   = random.choice(available)
        side  = random.choice(["BUY", "SELL"])
        ep    = round(random.uniform(0.10, 0.70), 3)
        size  = round(capital * random.uniform(0.04, 0.09), 2)
        whale = random.choice(WHALES)
        score = round(random.uniform(0.5, 4.0), 2)
        wr    = round(random.uniform(0.60, 0.85), 3)

        new_pos = {
            "market_id":    mkt["id"],
            "market_title": mkt["title"],
            "category":     mkt["category"],
            "side":         side,
            "entry_price":  ep,
            "size_usd":     size,
            "whale_address": whale,
            "whale_score":  score,
            "entry_date":   _now_iso(),
            "whale_winrate": wr,
            "partial_exit_done": False,
        }
        positions.append(new_pos)
        _append_jsonl(TRADES_FILE, {
            "action": "OPEN",
            "market_id": mkt["id"],
            "market_title": mkt["title"],
            "category": mkt["category"],
            "direction": side,
            "price": ep,
            "whale_address": whale,
            "whale_winrate": wr,
            "whale_score": score,
            "size_usd": size,
            "ts": _now_iso(),
        })
        print(f"  OPEN   {mkt['title'][:40]}  {side} ${size:.0f} @ {ep*100:.1f}¢  whale={whale[:10]}...")

    # ── Persist state ───────────────────────────────────────────────────────
    deployed = sum(float(p["size_usd"]) for p in positions)
    state["total_capital"] = round(capital, 2)
    state["positions"]     = positions
    state["saved_at"]      = _now_iso()
    _write_json(POSITIONS_FILE, state)

    _append_jsonl(EQUITY_LOG_FILE, {
        "ts":        _now_iso(),
        "equity":    round(equity, 2),
        "positions": len(positions),
        "deployed":  round(deployed, 2),
    })

    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] tick={tick}  equity=${equity:,.2f}  positions={len(positions)}  deployed=${deployed:,.0f}")
    return state


def _read_last_equity() -> float | None:
    if not EQUITY_LOG_FILE.exists():
        return None
    try:
        lines = EQUITY_LOG_FILE.read_text().strip().splitlines()
        if lines:
            return float(json.loads(lines[-1])["equity"])
    except Exception:
        pass
    return None


def _fetch_polymarket_balance() -> float:
    """Read actual pUSD balance from Polymarket (signature_type=2 proxy wallet)."""
    try:
        import os
        from dotenv import load_dotenv
        load_dotenv(_root / ".env")
        from eth_account import Account
        from py_clob_client_v2 import ClobClient
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType, ApiCreds

        pk = os.getenv("POLYMARKET_PRIVATE_KEY", "")
        if not pk:
            return 1000.0
        pk = pk if pk.startswith("0x") else "0x" + pk
        address = Account.from_key(pk).address

        api_key    = os.getenv("POLYMARKET_API_KEY", "")
        api_secret = os.getenv("POLYMARKET_API_SECRET", "")
        passphrase = os.getenv("POLYMARKET_API_PASSPHRASE", "")
        creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=passphrase) if api_key else None

        c = ClobClient("https://clob.polymarket.com", chain_id=137, key=pk,
                       creds=creds, signature_type=2, funder=address)
        r = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2))
        bal = int(r.get("balance", 0)) / 1e6
        return bal if bal > 0 else 1000.0
    except Exception as e:
        print(f"  (could not fetch live balance: {e} — using $1000)")
        return 1000.0


def main():
    print("Fetching Polymarket balance...")
    initial_capital = _fetch_polymarket_balance()
    print(f"  Starting capital: ${initial_capital:,.2f}")
    print("Seeding mock data...")
    equity  = _seed_equity_history(initial_capital=initial_capital)
    _seed_trade_history()
    state   = _seed_positions(capital=round(equity, 2))
    print(f"Seeded: equity=${equity:,.2f}  positions={len(state['positions'])}  trades written")
    print("Simulating live trades — press Ctrl+C to stop\n")

    tick = 0
    try:
        while True:
            tick += 1
            state = _simulate(state, tick)
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

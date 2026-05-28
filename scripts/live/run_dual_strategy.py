#!/usr/bin/env python3
"""
Dual-strategy live trading launcher.

Runs whale following + latency arb in parallel threads, each with $25 capital.
Positions are floor-rounded to whole dollars.

Prerequisites:
  1. Python 3.9+
  2. pip install -r requirements.txt py-clob-client py-order-utils
  3. Data fetched: python scripts/data/setup_live_data.py
  4. Env vars set (see below)

Required environment variables (live orders):
  POLYMARKET_PRIVATE_KEY   — Polygon wallet private key (0x...)
  POLYMARKET_API_KEY       — L2 API key
  POLYMARKET_API_SECRET    — L2 API secret
  POLYMARKET_API_PASSPHRASE— L2 API passphrase
  POLYMARKET_CHAIN_ID      — 137 (mainnet) or 80002 (testnet)

Optional:
  NEWSAPI_KEY              — NewsAPI.org key for latency arb (falls back to RSS)

Usage:
    # Dry-run (print signals, no orders):
    python scripts/live/run_dual_strategy.py --dry-run

    # Paper trading (simulated fills):
    python scripts/live/run_dual_strategy.py

    # Live trading ($25 each, real orders):
    python scripts/live/run_dual_strategy.py --live

    # Custom capital split:
    python scripts/live/run_dual_strategy.py --live --whale-capital 30 --arb-capital 20
"""

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))


def _run_whale(capital: float, live: bool, dry_run: bool, research_dir: Path) -> None:
    """Thread target: run whale following strategy."""
    cmd = [
        sys.executable,
        str(_root / "scripts" / "live" / "run_live_strategy.py"),
        "--capital", str(capital),
        "--research-dir", str(research_dir),
        "--min-position-usd", "1",
        "--interval", "60",
    ]
    if live:
        cmd.append("--live")
    elif dry_run:
        cmd.append("--dry-run")

    print(f"[WHALE] Starting: capital=${capital:.0f}  mode={'LIVE' if live else ('DRY-RUN' if dry_run else 'PAPER')}")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            print(f"[WHALE] {line}", end="")
        proc.wait()
        print(f"[WHALE] Process exited with code {proc.returncode}")
    except Exception as e:
        print(f"[WHALE] Error: {e}")


def _run_latency_arb(capital: float, live: bool, dry_run: bool, newsapi_key: str) -> None:
    """Thread target: run latency arb strategy."""
    cmd = [
        sys.executable,
        str(_root / "scripts" / "live" / "latency_arb.py"),
        "--capital", str(capital),
        "--interval", "30",
    ]
    if live:
        cmd.append("--live")
    else:
        cmd.append("--dry-run")
    if newsapi_key:
        cmd.extend(["--newsapi-key", newsapi_key])

    print(f"[ARB]   Starting: capital=${capital:.0f}  mode={'LIVE' if live else 'DRY-RUN'}")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            print(f"[ARB]   {line}", end="")
        proc.wait()
        print(f"[ARB]   Process exited with code {proc.returncode}")
    except Exception as e:
        print(f"[ARB]   Error: {e}")


def main():
    p = argparse.ArgumentParser(
        description="Dual-strategy live trader: whale following + latency arb",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--whale-capital",  type=float, default=25.0, help="Capital for whale following ($)")
    p.add_argument("--arb-capital",    type=float, default=25.0, help="Capital for latency arb ($)")
    p.add_argument("--live",           action="store_true", help="Place real orders (requires credentials)")
    p.add_argument("--dry-run",        action="store_true", help="Print signals only, no orders")
    p.add_argument("--research-dir",   type=Path, default=_root / "data" / "research",
                   help="Research data directory (for whale set)")
    p.add_argument("--newsapi-key",    default=os.environ.get("NEWSAPI_KEY", ""),
                   help="NewsAPI.org key for latency arb (optional; falls back to RSS)")
    args = p.parse_args()

    total = args.whale_capital + args.arb_capital
    mode  = "LIVE" if args.live else ("DRY-RUN" if args.dry_run else "PAPER")

    print("=" * 65)
    print("  DUAL STRATEGY LAUNCHER")
    print("=" * 65)
    print(f"  Mode:             {mode}")
    print(f"  Whale capital:    ${args.whale_capital:.2f}")
    print(f"  Arb capital:      ${args.arb_capital:.2f}")
    print(f"  Total capital:    ${total:.2f}")
    print(f"  Research dir:     {args.research_dir}")
    print(f"  Position rounding: floor (whole dollars)")
    print("=" * 65)

    if args.live:
        required = [
            "POLYMARKET_PRIVATE_KEY",
            "POLYMARKET_API_KEY",
            "POLYMARKET_API_SECRET",
            "POLYMARKET_API_PASSPHRASE",
        ]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            print(f"\nERROR: Live mode requires these env vars: {', '.join(missing)}")
            print("  Set them with: set POLYMARKET_PRIVATE_KEY=0x...")
            print("  Then re-run this script.")
            sys.exit(1)

    if not args.research_dir.exists():
        print(f"\nWARNING: Research data not found at {args.research_dir}")
        print("  Run first: python scripts/data/setup_live_data.py")
        print("  Continuing anyway — whale set will be empty until data is fetched.\n")

    whale_thread = threading.Thread(
        target=_run_whale,
        args=(args.whale_capital, args.live, args.dry_run, args.research_dir),
        daemon=True,
        name="whale-strategy",
    )
    arb_thread = threading.Thread(
        target=_run_latency_arb,
        args=(args.arb_capital, args.live, args.dry_run, args.newsapi_key),
        daemon=True,
        name="latency-arb",
    )

    whale_thread.start()
    time.sleep(2)  # stagger startup slightly
    arb_thread.start()

    print("\nBoth strategies running. Press Ctrl+C to stop.\n")

    try:
        while whale_thread.is_alive() or arb_thread.is_alive():
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nShutdown signal received. Both strategies will stop.")
        sys.exit(0)


if __name__ == "__main__":
    main()

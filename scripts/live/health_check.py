#!/usr/bin/env python3
"""
Health check for the live whale strategy daemon.

Usage:
    PYTHONPATH=.:src venv/bin/python3 scripts/live/health_check.py
    PYTHONPATH=.:src venv/bin/python3 scripts/live/health_check.py --json
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POSITIONS_PATH = ROOT / "data" / "live" / "positions.json"
TRADES_PATH    = ROOT / "data" / "live" / "trades.jsonl"
STALE_MINUTES  = 10   # Consider stale if positions.json not updated in this many minutes


def check_process_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "run_live_strategy.py"],
            capture_output=True, text=True,
        )
        return result.returncode == 0
    except Exception:
        return False


def load_positions():
    if not POSITIONS_PATH.exists():
        return None
    try:
        with open(POSITIONS_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def last_modified_minutes(path: Path) -> float:
    if not path.exists():
        return float("inf")
    mtime = path.stat().st_mtime
    return (datetime.now().timestamp() - mtime) / 60.0


def last_trades(n: int = 5):
    if not TRADES_PATH.exists():
        return []
    lines = []
    try:
        with open(TRADES_PATH) as f:
            for line in f:
                lines.append(line.strip())
        return [json.loads(l) for l in lines[-n:] if l]
    except Exception:
        return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    running  = check_process_running()
    data     = load_positions()
    stale_m  = last_modified_minutes(POSITIONS_PATH)
    is_stale = stale_m > STALE_MINUTES
    healthy  = running and not is_stale

    result = {
        "healthy":         healthy,
        "process_running": running,
        "positions_stale": is_stale,
        "stale_minutes":   round(stale_m, 1) if stale_m != float("inf") else None,
        "positions":       [],
        "total_capital":   None,
        "deployed":        None,
        "available":       None,
        "recent_events":   last_trades(5),
    }

    if data:
        capital  = data.get("total_capital", 0)
        deployed = sum(p.get("size_usd", 0) for p in data.get("positions", []))
        result["total_capital"] = capital
        result["deployed"]      = round(deployed, 0)
        result["available"]     = round(capital - deployed, 0)
        result["positions"] = [
            {
                "market_id":    p["market_id"][:20],
                "side":         p["side"],
                "size_usd":     round(p.get("size_usd", 0), 0),
                "entry_price":  p.get("entry_price"),
                "entry_date":   p.get("entry_date", "")[:10],
                "whale":        p.get("whale_address", "")[:10],
            }
            for p in data.get("positions", [])
        ]

    if args.json:
        print(json.dumps(result, indent=2))
        sys.exit(0 if healthy else 1)

    # Human-readable output
    status = "OK" if healthy else ("STALE" if running else "DOWN")
    print(f"\n{'='*55}")
    print(f"  Whale Strategy Health Check  [{status}]")
    print(f"{'='*55}")
    print(f"  Process running:  {'YES' if running else 'NO'}")
    print(f"  Positions last updated: {round(stale_m, 1)} min ago"
          if stale_m != float("inf") else "  Positions file: NOT FOUND")

    if data:
        capital  = result["total_capital"]
        deployed = result["deployed"]
        print(f"  Capital:   ${capital:,.0f}")
        print(f"  Deployed:  ${deployed:,.0f}  ({deployed/max(capital,1):.1%})")
        print(f"  Available: ${result['available']:,.0f}")
        print(f"\n  Open positions ({len(result['positions'])}):")
        for p in result["positions"]:
            print(f"    {p['market_id']:<22} {p['side']:<5} ${p['size_usd']:>9,.0f}  "
                  f"@ {p['entry_price']:.3f}  {p['entry_date']}")
    else:
        print("  No position state found.")

    if result["recent_events"]:
        print(f"\n  Recent events:")
        for ev in reversed(result["recent_events"]):
            action = ev.get("action", "?")
            mid    = str(ev.get("market_id", ""))[:20]
            pnl    = ev.get("net_pnl") or ev.get("pnl")
            pnl_s  = f"  pnl=${pnl:+,.0f}" if pnl is not None else ""
            size   = ev.get("size_usd") or ev.get("add_size")
            size_s = f"  ${size:,.0f}" if size else ""
            print(f"    {action:<15} {mid}{size_s}{pnl_s}")

    print()
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()

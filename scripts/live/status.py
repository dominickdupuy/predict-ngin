#!/usr/bin/env python3
"""
Show current live strategy portfolio status.

Usage:
    PYTHONPATH=.:src venv/bin/python3 scripts/live/status.py
    PYTHONPATH=.:src venv/bin/python3 scripts/live/status.py --trades 20
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades", type=int, default=10, help="Recent trade events to show")
    parser.add_argument("--positions-file", type=Path,
                        default=ROOT / "data" / "live" / "positions.json")
    parser.add_argument("--trades-file", type=Path,
                        default=ROOT / "data" / "live" / "trades.jsonl")
    args = parser.parse_args()

    print(f"\n{'='*65}")
    print(f"  Live Strategy Portfolio Status")
    print(f"{'='*65}")

    # Positions
    if args.positions_file.exists():
        with open(args.positions_file) as f:
            data = json.load(f)
        capital  = data.get("total_capital", 0)
        positions = data.get("positions", [])
        deployed = sum(p.get("size_usd", 0) for p in positions)
        saved_at = data.get("saved_at", "unknown")
        print(f"  Capital:   ${capital:,.0f}")
        print(f"  Deployed:  ${deployed:,.0f}  ({deployed/max(capital,1):.1%})")
        print(f"  Available: ${capital-deployed:,.0f}")
        print(f"  Last save: {saved_at}")
        print(f"\n  Open Positions ({len(positions)}):")
        if positions:
            print(f"  {'Market ID':<24} {'Side':<5} {'Size USD':>10}  {'Entry':>6}  {'Entry Date':<12}  Whale")
            print(f"  {'-'*80}")
            for p in sorted(positions, key=lambda x: -x.get("size_usd", 0)):
                partial = " (partial)" if p.get("partial_exit_done") else ""
                print(
                    f"  {p['market_id'][:24]:<24} {p['side']:<5} "
                    f"${p.get('size_usd', 0):>9,.0f}  "
                    f"{p.get('entry_price', 0):>6.3f}  "
                    f"{str(p.get('entry_date', ''))[:10]:<12}  "
                    f"{p.get('whale_address', '')[:10]}{partial}"
                )
        else:
            print("  (none)")
    else:
        print("  No positions file found.")

    # Recent trade events
    print(f"\n  Recent Trade Events (last {args.trades}):")
    if args.trades_file.exists():
        lines = args.trades_file.read_text().strip().splitlines()
        recent = lines[-args.trades:]
        if recent:
            for line in reversed(recent):
                try:
                    ev = json.loads(line)
                    action = ev.get("action", "?")
                    mid    = str(ev.get("market_id", ""))[:24]
                    size   = ev.get("size_usd") or ev.get("add_size") or ev.get("size_closed")
                    pnl    = ev.get("net_pnl")
                    size_s = f"  ${size:,.0f}" if size else ""
                    pnl_s  = f"  pnl=${pnl:+,.0f}" if pnl is not None else ""
                    print(f"  {action:<16} {mid}{size_s}{pnl_s}")
                except Exception:
                    pass
        else:
            print("  (no events)")
    else:
        print("  No trades log found.")

    print()


if __name__ == "__main__":
    main()

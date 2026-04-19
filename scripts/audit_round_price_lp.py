"""
Manual audit of round_price_lp trades.

For a random sample of trades produced at the $300k threshold, this script:
  1. Shows the raw trade record (entry_px, exit_px, cost bps, PnL).
  2. Re-reconstructs the order book at entry_s from the PIT trade tape.
  3. Shows the best bid/ask, mid, and compares entry_px to what was on the book.
  4. Sanity-checks the absolute $ slippage vs the bps figure the engine reported.

Run: python scripts/audit_round_price_lp.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backtest_v3.backtest.engine import BacktestEngine, EngineConfig
from backtest_v3.data.clob_book import CLOBBookReconstructor
from backtest_v3.data.loader import PITDataLoader
from backtest_v3.strategies.round_price_lp import RoundPriceLP


CATEGORIES = ("Politics", "Economy", "Geopolitics", "Finance")
START = "2025-02-14"
END = "2026-02-14"
N_SAMPLE = 3
SEED = 42


def main() -> int:
    RoundPriceLP.default_params = RoundPriceLP.default_params.merge({
        "min_round_cluster_count": 3,
        "scan_window_s": 14400,
        "notional_usd": 250.0,
    })

    print("Loading data...")
    loader = PITDataLoader(ROOT / "data" / "research", categories=CATEGORIES)
    for cat in CATEGORIES:
        loader._load_trades(cat)

    cfg = EngineConfig(
        start_s=int(pd.Timestamp(START, tz="UTC").timestamp()),
        end_s=int(pd.Timestamp(END, tz="UTC").timestamp()),
        step_s=12 * 3600,
        liquidity_threshold_usd=300_000.0,
        liquidity_lookback_s=30 * 24 * 3600,
        label="audit_rp",
    )
    print("Running round_price_lp @ $300k...")
    res = BacktestEngine(loader, [RoundPriceLP(loader)], cfg).run()
    trades = res.trades
    print(f"  {len(trades)} trades produced\n")

    if trades.empty:
        print("No trades to audit.")
        return 0

    random.seed(SEED)
    sample_ix = random.sample(range(len(trades)), k=min(N_SAMPLE, len(trades)))
    recon = CLOBBookReconstructor()

    for i, ix in enumerate(sample_ix, 1):
        t = trades.iloc[ix]
        print(f"--- Sample {i}/{len(sample_ix)} (trade #{ix}) ---")
        print(f"  condition_id     : {t['condition_id']}")
        print(f"  category         : {t['category']}")
        print(f"  side             : {t['side']}")
        print(f"  entry_s          : {int(t['entry_s'])}  "
              f"({pd.Timestamp(int(t['entry_s']), unit='s', tz='UTC')})")
        print(f"  exit_s           : {int(t['exit_s'])}  "
              f"({pd.Timestamp(int(t['exit_s']), unit='s', tz='UTC')})")
        print(f"  requested_usd    : ${t['requested_usd']:.2f}")
        print(f"  entry_filled_usd : ${t['entry_filled_usd']:.2f}")
        print(f"  entry_px         : ${t['entry_px']:.4f}")
        print(f"  exit_px          : ${t['exit_px']:.4f}")
        print(f"  pnl_usd          : ${t['pnl_usd']:.2f}")
        print(f"  entry_cost_bps   : {t['entry_cost_bps']:.1f}")
        print(f"  exit_cost_bps    : {t['exit_cost_bps']:.1f}")

        cat = t["category"]
        tape = loader._load_trades(cat)
        mkt = tape[tape["conditionId"] == t["condition_id"]]
        if mkt.empty:
            print(f"  !! no trades found for this condition_id in tape")
            print()
            continue

        before = mkt[mkt["timestamp"] < int(t["entry_s"])]
        print(f"  tape size (pre-entry): {len(before):,}")
        if not before.empty:
            last_trade_s = int(before["timestamp"].iloc[-1])
            last_price = float(before["price"].iloc[-1])
            print(f"  last trade pre-entry : {pd.Timestamp(last_trade_s, unit='s', tz='UTC')} "
                  f"@ ${last_price:.4f}")

        if before.empty:
            print(f"  !! no pre-entry trades — cannot build book")
            print()
            continue

        book = recon.reconstruct(t["condition_id"], int(t["entry_s"]), before)
        if book is None:
            print(f"  !! reconstructor returned None")
            print()
            continue

        best_bid = book.bids[0] if book.bids else None
        best_ask = book.asks[0] if book.asks else None
        print(f"  BOOK @ entry_s:")
        print(f"    best_bid : {best_bid.price:.4f} (size ${best_bid.size_usd:,.0f})"
              if best_bid else "    best_bid : (none)")
        print(f"    best_ask : {best_ask.price:.4f} (size ${best_ask.size_usd:,.0f})"
              if best_ask else "    best_ask : (none)")
        print(f"    mid      : {book.mid:.4f}")

        # Spread in bps of mid
        if best_bid and best_ask and book.mid > 0:
            spread_bps = (best_ask.price - best_bid.price) / book.mid * 10_000
            print(f"    spread   : {spread_bps:.0f} bps of mid")

        # Compare to recorded entry_px
        ep = float(t["entry_px"])
        if book.mid > 0:
            rel_vs_mid_bps = (ep - book.mid) / book.mid * 10_000
            sign = "+" if rel_vs_mid_bps >= 0 else ""
            print(f"    entry_px vs mid : {sign}{rel_vs_mid_bps:.0f} bps of mid  "
                  f"(engine recorded {t['entry_cost_bps']:.0f})")

        # Absolute dollar cost check
        if book.mid > 0:
            notional = float(t["entry_filled_usd"])
            # Approx $ slippage: |entry_px - mid| * (notional / avg_px)
            shares = notional / ep if ep > 0 else 0
            dollar_slip = abs(ep - book.mid) * shares
            print(f"    absolute $ slip (approx): ${dollar_slip:.2f} on ${notional:.0f} notional")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

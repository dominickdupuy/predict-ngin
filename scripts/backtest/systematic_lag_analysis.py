"""
systematic_lag_analysis.py — Unbiased measurement of price-discovery lag in Polymarket

Methodology
-----------
Uses price-dynamics detection, not manual headline selection, to find latency
arbitrage opportunities.  For each resolved market:

  1. Normalize all trade prices to YES perspective
     (outcomeIndex=0 → price as-is; outcomeIndex=1 → YES = 1 − price)
  2. Build a 10-minute volume-weighted average price (VWAP) series
  3. Find "transition events": windows where YES price moved from ≤ 75%
     to ≥ 85% within 24 hours — these are presumptively news-driven
  4. Record:
       entry_price       — YES price at the 85% crossing
       residual          — 1 − entry_price (the gap to certainty)
       jump_speed_hours  — time from last sub-75% tick to 85% crossing
       convergence_min   — minutes from 85% crossing to first ≥ 97% tick
  5. Markets with no qualifying transition are labelled "no arb event"
     (they either drifted slowly or moved from a high base)

Bias controls
-------------
• No headline selection: the signal is purely price-derived
• Stratified random sample per category (fixed seed) — reproducible
• Minimum 200 trades required to include a market (sparse markets excluded)
• Sports and Tech omitted (each > 1 GB; would dominate sample without adding
  diversity — include separately with --include-sports-tech if needed)
• Separate statistics for "fast jumps" (< 6 h) vs "medium jumps" (6–24 h)

Usage
-----
    PYTHONPATH=.:src venv/bin/python3 scripts/backtest/systematic_lag_analysis.py
    PYTHONPATH=.:src venv/bin/python3 scripts/backtest/systematic_lag_analysis.py \\
        --n-markets 500 --seed 42 --workers 35
"""

import argparse
import json
import logging
import os
import random
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("lag_analysis")

ROOT = Path(__file__).resolve().parent.parent.parent

# Categories included (Sports/Tech omitted due to size; use --include-large to add)
DEFAULT_CATEGORIES = [
    "Art_and_Culture",
    "Climate_and_Science",
    "Economy",
    "Finance",
    "Geopolitics",
    "Other",
    "Politics",
]
LARGE_CATEGORIES = ["Sports", "Tech"]

# ── Price dynamics thresholds ─────────────────────────────────────────────────
ENTRY_LOWER   = 0.75   # price must have been ≤ this before the jump
ENTRY_UPPER   = 0.85   # price must have crossed ≥ this on entry
FULL_PRICE    = 0.97   # price must reach ≥ this to count as "fully priced"
MAX_JUMP_HRS  = 24.0   # max hours for the 75%→85% transition to count as a jump
FAST_JUMP_HRS = 6.0    # below this: "fast jump" (likely news); above: "medium jump"
MIN_TRADES    = 200    # minimum trades required to include a market
VWAP_BUCKET   = "10min"  # VWAP resampling interval


@dataclass
class MarketLagResult:
    condition_id: str
    category: str
    resolution: str          # "YES" or "NO"
    n_trades: int
    has_arb_event: bool      # did a qualifying price jump occur?
    entry_price: float       # YES price at the 85% crossing (0 if no arb event)
    residual: float          # 1 − entry_price (0 if no arb event)
    jump_speed_hours: float  # time from sub-75% to 85% crossing (−1 if no event)
    convergence_min: float   # minutes from 85% to 97% (−1 if never reached 97%)
    is_fast_jump: bool       # jump_speed_hours < FAST_JUMP_HRS
    price_at_resolution: float  # final price (sanity check)


# ── Per-market analysis (runs in worker) ─────────────────────────────────────

def analyze_market(args: tuple) -> Optional[MarketLagResult]:
    """
    Compute lag metrics for a single resolved market.

    Receives (condition_id, resolution, trades_list, category) where
    trades_list is a list of (timestamp, yes_price, size) tuples —
    already normalized to YES perspective by the parent process.
    """
    condition_id, resolution, trades_list, category = args

    if len(trades_list) < MIN_TRADES:
        return None

    # Build DataFrame from pre-normalized trades
    df = pd.DataFrame(trades_list, columns=["ts", "yes_price", "size"])
    df = df.sort_values("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df = df.set_index("dt")

    # 10-minute VWAP
    num   = (df["yes_price"] * df["size"]).resample(VWAP_BUCKET).sum()
    denom = df["size"].resample(VWAP_BUCKET).sum()
    vwap  = (num / denom).dropna()

    if len(vwap) < 5:
        return None

    # Resolve direction: YES resolution → look for upward jump; NO → downward
    is_yes = resolution == "YES"
    # For NO markets, flip so we always look for an upward crossing in the
    # "resolution direction" price series.
    series = vwap if is_yes else 1.0 - vwap

    # Final price (should be near 1.0 for a correctly resolved market)
    price_at_res = float(series.iloc[-1])

    # ── Find the last time the series was ≤ ENTRY_LOWER ──────────────────────
    below_lower = series[series <= ENTRY_LOWER]
    if below_lower.empty:
        # Price never came down far enough to start from — no arb event
        return MarketLagResult(
            condition_id=condition_id, category=category, resolution=resolution,
            n_trades=len(df), has_arb_event=False,
            entry_price=0, residual=0, jump_speed_hours=-1,
            convergence_min=-1, is_fast_jump=False,
            price_at_resolution=price_at_res,
        )

    last_below_dt = below_lower.index[-1]
    last_below_px = float(below_lower.iloc[-1])

    # ── Find first time after last_below_dt that series ≥ ENTRY_UPPER ────────
    after_last_below = series[series.index > last_below_dt]
    above_upper = after_last_below[after_last_below >= ENTRY_UPPER]

    if above_upper.empty:
        # Crossed below 75% but never reached 85% — no qualifying jump
        return MarketLagResult(
            condition_id=condition_id, category=category, resolution=resolution,
            n_trades=len(df), has_arb_event=False,
            entry_price=0, residual=0, jump_speed_hours=-1,
            convergence_min=-1, is_fast_jump=False,
            price_at_resolution=price_at_res,
        )

    entry_dt = above_upper.index[0]
    entry_px  = float(above_upper.iloc[0])
    residual  = round(1.0 - entry_px, 4)

    # ── Jump speed: hours from last sub-75% to 85% crossing ──────────────────
    jump_speed_hours = (entry_dt - last_below_dt).total_seconds() / 3600

    if jump_speed_hours > MAX_JUMP_HRS:
        # Too slow to be a news event
        return MarketLagResult(
            condition_id=condition_id, category=category, resolution=resolution,
            n_trades=len(df), has_arb_event=False,
            entry_price=round(entry_px, 4), residual=residual,
            jump_speed_hours=round(jump_speed_hours, 2),
            convergence_min=-1, is_fast_jump=False,
            price_at_resolution=price_at_res,
        )

    # ── Convergence: minutes from entry to FULL_PRICE ─────────────────────────
    after_entry = series[series.index >= entry_dt]
    at_full = after_entry[after_entry >= FULL_PRICE]

    if not at_full.empty:
        convergence_min = (at_full.index[0] - entry_dt).total_seconds() / 60
    else:
        # Never reached FULL_PRICE within the data — use time to end of series
        convergence_min = (after_entry.index[-1] - entry_dt).total_seconds() / 60

    return MarketLagResult(
        condition_id=condition_id, category=category, resolution=resolution,
        n_trades=len(df), has_arb_event=True,
        entry_price=round(entry_px, 4), residual=residual,
        jump_speed_hours=round(jump_speed_hours, 2),
        convergence_min=round(convergence_min, 1),
        is_fast_jump=jump_speed_hours < FAST_JUMP_HRS,
        price_at_resolution=price_at_res,
    )


# ── Batch loader ──────────────────────────────────────────────────────────────

def load_category_sample(
    category: str,
    resolutions: dict,
    n_per_category: int,
    rng: random.Random,
) -> list[tuple]:
    """
    Load a random sample of resolved markets from one category.

    Returns list of (condition_id, resolution, trades_list, category) tuples
    ready for parallel processing.
    """
    path = ROOT / "data" / "poly_cat" / category / "trades.parquet"
    if not path.exists():
        return []

    log.info(f"  Loading {category}…")
    df = pd.read_parquet(
        path,
        columns=["conditionId", "price", "size", "timestamp", "outcomeIndex"],
    )

    # Filter to resolved markets only
    resolved_in_cat = set(df["conditionId"].unique()) & set(resolutions.keys())
    if not resolved_in_cat:
        return []

    # Stratified by YES/NO so we don't over-represent one resolution type
    yes_ids = [cid for cid in resolved_in_cat if resolutions[cid] == "YES"]
    no_ids  = [cid for cid in resolved_in_cat if resolutions[cid] == "NO"]

    # Sample proportionally (maintain the natural ~40/60 YES/NO split)
    n_yes = min(len(yes_ids), int(n_per_category * 0.40))
    n_no  = min(len(no_ids),  int(n_per_category * 0.60))

    sampled_yes = rng.sample(yes_ids, n_yes) if n_yes > 0 else []
    sampled_no  = rng.sample(no_ids,  n_no)  if n_no  > 0 else []
    sampled     = set(sampled_yes + sampled_no)

    log.info(
        f"  {category}: {len(resolved_in_cat):,} resolved  →  "
        f"sample {len(sampled)} ({n_yes} YES + {n_no} NO)"
    )

    # Filter to sampled markets, normalize prices to YES perspective
    sub = df[df["conditionId"].isin(sampled)].copy()
    sub["yes_price"] = np.where(sub["outcomeIndex"] == 1, 1.0 - sub["price"], sub["price"])
    sub = sub[(sub["yes_price"] >= 0.001) & (sub["yes_price"] <= 0.999)]

    jobs = []
    for cid, grp in sub.groupby("conditionId"):
        if len(grp) < MIN_TRADES:
            continue
        trades_list = list(zip(
            grp["timestamp"].tolist(),
            grp["yes_price"].tolist(),
            grp["size"].tolist(),
        ))
        jobs.append((cid, resolutions[cid], trades_list, category))

    return jobs


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_report(results: list[MarketLagResult]):
    arb_events = [r for r in results if r.has_arb_event]
    fast_jumps = [r for r in arb_events if r.is_fast_jump]
    slow_jumps = [r for r in arb_events if not r.is_fast_jump]
    no_events  = [r for r in results if not r.has_arb_event]

    print(f"\n{'='*80}")
    print("  SYSTEMATIC LAG ANALYSIS  —  Polymarket Price-Discovery Study")
    print(f"{'='*80}")
    print(f"  Total markets analysed:          {len(results):>5,}")
    print(f"  Markets with arb event:          {len(arb_events):>5,}  ({len(arb_events)/len(results):.1%})")
    print(f"    of which — fast jump (< 6 h):  {len(fast_jumps):>5,}  ({len(fast_jumps)/len(results):.1%})")
    print(f"    of which — medium jump (6–24 h):{len(slow_jumps):>4,}  ({len(slow_jumps)/len(results):.1%})")
    print(f"  Markets without arb event:       {len(no_events):>5,}  ({len(no_events)/len(results):.1%})")

    if arb_events:
        res_all = [r.residual for r in arb_events]
        conv_all = [r.convergence_min for r in arb_events if r.convergence_min >= 0]

        print(f"\n{'─'*80}")
        print("  ALL ARB EVENTS (n={})".format(len(arb_events)))
        print(f"  Residual at entry (gap to certainty):")
        print(f"    Mean:   {np.mean(res_all):.1%}")
        print(f"    Median: {np.median(res_all):.1%}")
        print(f"    p25:    {np.percentile(res_all, 25):.1%}")
        print(f"    p75:    {np.percentile(res_all, 75):.1%}")
        print(f"    Max:    {np.max(res_all):.1%}")
        if conv_all:
            print(f"  Convergence time (minutes):")
            print(f"    Mean:   {np.mean(conv_all):.0f} min")
            print(f"    Median: {np.median(conv_all):.0f} min")
            print(f"    p25:    {np.percentile(conv_all, 25):.0f} min")
            print(f"    p75:    {np.percentile(conv_all, 75):.0f} min")

    if fast_jumps:
        res_fast = [r.residual for r in fast_jumps]
        conv_fast = [r.convergence_min for r in fast_jumps if r.convergence_min >= 0]
        spd_fast = [r.jump_speed_hours for r in fast_jumps]

        print(f"\n{'─'*80}")
        print("  FAST JUMPS ONLY  (< {:.0f} h, likely news-driven, n={})".format(
            FAST_JUMP_HRS, len(fast_jumps)))
        print(f"  Jump speed (hours from sub-75% to 85% crossing):")
        print(f"    Mean:   {np.mean(spd_fast):.2f} h  ({np.mean(spd_fast)*60:.0f} min)")
        print(f"    Median: {np.median(spd_fast):.2f} h  ({np.median(spd_fast)*60:.0f} min)")
        print(f"  Residual at entry:")
        print(f"    Mean:   {np.mean(res_fast):.1%}")
        print(f"    Median: {np.median(res_fast):.1%}")
        if conv_fast:
            print(f"  Convergence time after entry (minutes):")
            print(f"    Mean:   {np.mean(conv_fast):.0f} min")
            print(f"    Median: {np.median(conv_fast):.0f} min")
            print(f"    p25:    {np.percentile(conv_fast, 25):.0f} min")
            print(f"    p75:    {np.percentile(conv_fast, 75):.0f} min")
            print(f"    p90:    {np.percentile(conv_fast, 90):.0f} min")

    # Per-category breakdown
    from collections import defaultdict
    cats = defaultdict(list)
    for r in arb_events:
        cats[r.category].append(r)

    if cats:
        print(f"\n{'─'*80}")
        print("  ARENAS WITH MOST ARB EVENTS  (by category)")
        for cat, evs in sorted(cats.items(), key=lambda x: -len(x[1])):
            res_c = [e.residual for e in evs]
            fast_c = sum(1 for e in evs if e.is_fast_jump)
            print(
                f"  {cat:<25}  {len(evs):>4} events  "
                f"({fast_c} fast)  "
                f"mean residual={np.mean(res_c):.1%}"
            )

    print(f"{'='*80}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Systematic lag analysis — price-dynamics based")
    p.add_argument("--n-markets",         type=int, default=1000,
                   help="Total markets to sample (default: 1000)")
    p.add_argument("--seed",              type=int, default=42,
                   help="Random seed for reproducibility (default: 42)")
    p.add_argument("--workers",           type=int, default=35,
                   help="Parallel workers (default: 35)")
    p.add_argument("--include-large",     action="store_true",
                   help="Include Sports and Tech categories (adds ~2 GB memory per category)")
    p.add_argument("--out",               default="backtests/latency_arb/systematic_lag.json",
                   help="Output JSON path")
    args = p.parse_args()

    rng = random.Random(args.seed)

    # ── Load resolutions ───────────────────────────────────────────────────────
    log.info("Loading resolutions…")
    res_df = pd.read_csv(ROOT / "data" / "poly_cat" / "resolutions.csv")
    resolutions = dict(zip(res_df["market_id"], res_df["winner"]))
    log.info(f"  {len(resolutions):,} resolved markets  "
             f"({res_df['winner'].eq('YES').sum():,} YES, {res_df['winner'].eq('NO').sum():,} NO)")

    # ── Build job list from each category ─────────────────────────────────────
    categories = DEFAULT_CATEGORIES + (LARGE_CATEGORIES if args.include_large else [])
    n_per_cat  = max(1, args.n_markets // len(categories))

    all_jobs = []
    for cat in categories:
        jobs = load_category_sample(cat, resolutions, n_per_cat, rng)
        all_jobs.extend(jobs)
        log.info(f"  {cat}: {len(jobs)} jobs queued")

    log.info(f"\nTotal jobs: {len(all_jobs)}  |  Workers: {args.workers}")

    # ── Parallel analysis ──────────────────────────────────────────────────────
    results = []
    n_done  = 0

    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(analyze_market, job): job for job in all_jobs}
        for fut in as_completed(futs):
            n_done += 1
            if n_done % 50 == 0:
                log.info(f"  Progress: {n_done}/{len(all_jobs)}")
            try:
                r = fut.result()
                if r is not None:
                    results.append(r)
            except Exception as e:
                log.warning(f"  Worker error: {e}")

    log.info(f"Completed {len(results)} markets successfully")

    # ── Report ─────────────────────────────────────────────────────────────────
    print_report(results)

    # ── Save ───────────────────────────────────────────────────────────────────
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2, default=str)
    log.info(f"Results saved to {out}")


if __name__ == "__main__":
    main()

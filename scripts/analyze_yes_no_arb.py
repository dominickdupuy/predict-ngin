"""
Does the YES + NO sum-to-$1 arb exist on Polymarket after costs?

Uses data/research/<cat>/prices.parquet — per-(market, outcome, ts) price
snapshots. For each market × timestamp where YES and NO prices are both
available, compute sum = P(YES) + P(NO). An arb opportunity exists when
sum < 1.00 - round_trip_cost_frac.

Reports event count, total post-cost PnL, and the distribution of the sum
to separate "real arb" from "stale-quote noise". If post-cost PnL is
zero or negligible, do not build the strategy.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


CATEGORIES = ("Politics", "Economy", "Geopolitics", "Finance")
START = "2025-02-14"
END = "2026-02-14"
# Polymarket snapshot data is per-minute — we don't need to grid further.
HALF_SPREAD_BPS = 50          # 0.5¢ on each leg, conservative
ROUND_TRIP_FEE_BPS = 0.0
PROFIT_GATE_BPS = 0           # report *any* net-positive event
NOTIONAL_PER_LEG_USD = 250.0


def scan_category(cat: str, start_s: int, end_s: int) -> pd.DataFrame:
    path = ROOT / "data" / "research" / cat / "prices.parquet"
    if not path.exists():
        print(f"[{cat}] no prices.parquet")
        return pd.DataFrame()
    print(f"[{cat}] loading prices...")
    df = pd.read_parquet(path)
    if df.empty:
        return pd.DataFrame()
    df = df[(df["timestamp"] >= start_s) & (df["timestamp"] <= end_s)]
    if df.empty:
        return pd.DataFrame()
    # Pivot to wide: one row per (market_id, timestamp) with YES and NO cols.
    # Very many rows — do it market-by-market to keep memory manageable.
    cost_frac = (2 * HALF_SPREAD_BPS + ROUND_TRIP_FEE_BPS + PROFIT_GATE_BPS) / 10_000.0

    events: List[Dict] = []
    seen = df["market_id"].dropna().unique()
    print(f"[{cat}] {len(seen):,} markets to scan")

    for i, mid in enumerate(seen):
        sub = df[df["market_id"] == mid][["timestamp", "outcome", "price"]]
        if sub["outcome"].nunique() < 2:
            continue
        pv = sub.pivot_table(
            index="timestamp", columns="outcome", values="price", aggfunc="last"
        ).dropna()
        if pv.empty or "Yes" not in pv.columns or "No" not in pv.columns:
            continue
        pv["sum"] = pv["Yes"] + pv["No"]
        pv["edge"] = 1.0 - pv["sum"] - cost_frac
        pos = pv[pv["edge"] > 0]
        if pos.empty:
            continue
        for ts, row in pos.iterrows():
            events.append({
                "category": cat,
                "market_id": mid,
                "timestamp_s": int(ts),
                "yes_price": float(row["Yes"]),
                "no_price": float(row["No"]),
                "sum_price": float(row["sum"]),
                "net_edge_after_cost": float(row["edge"]),
                "pnl_usd": float(row["edge"]) * NOTIONAL_PER_LEG_USD * 2,
            })
    return pd.DataFrame(events)


def main() -> int:
    out_dir = ROOT / "docs"
    out_dir.mkdir(exist_ok=True)

    start_s = int(pd.Timestamp(START, tz="UTC").timestamp())
    end_s = int(pd.Timestamp(END, tz="UTC").timestamp())

    frames = []
    for cat in CATEGORIES:
        f = scan_category(cat, start_s, end_s)
        if not f.empty:
            frames.append(f)
        print(f"  [{cat}] events found: {0 if f.empty else len(f):,}")

    if not frames:
        print("\nNo arb events found under any threshold. Strategy dead.")
        (out_dir / "YES_NO_ARB_ANALYSIS.md").write_text(
            "# YES/NO Arb Analysis\n\n"
            f"**Window:** {START} -> {END}\n"
            f"**Cost gate:** {2*HALF_SPREAD_BPS + ROUND_TRIP_FEE_BPS + PROFIT_GATE_BPS} bps round-trip\n\n"
            "**Result:** no profitable events found.\n",
            encoding="utf-8",
        )
        return 0

    events = pd.concat(frames, ignore_index=True)
    events.to_csv(out_dir / "YES_NO_ARB_EVENTS.csv", index=False)

    # Collapse: one "opportunity" per (market_id, consecutive-ts) run, so a
    # market stuck at sum=0.99 for 3h doesn't count as 180 "events".
    events = events.sort_values(["market_id", "timestamp_s"]).reset_index(drop=True)
    events["gap"] = events.groupby("market_id")["timestamp_s"].diff().gt(300).cumsum()
    runs = events.groupby(["market_id", "gap"]).agg(
        category=("category", "first"),
        start_s=("timestamp_s", "min"),
        end_s=("timestamp_s", "max"),
        peak_edge=("net_edge_after_cost", "max"),
        mean_edge=("net_edge_after_cost", "mean"),
        pnl_peak=("pnl_usd", "max"),
        samples=("timestamp_s", "size"),
    ).reset_index(drop=False)
    runs["duration_s"] = runs["end_s"] - runs["start_s"]

    per_cat = events.groupby("category").agg(
        n_events=("pnl_usd", "size"),
        unique_markets=("market_id", "nunique"),
        total_pnl_if_each_taken=("pnl_usd", "sum"),
        mean_edge_bps=("net_edge_after_cost", lambda x: float(x.mean() * 10_000)),
        max_edge_bps=("net_edge_after_cost", lambda x: float(x.max() * 10_000)),
    ).round(2)

    body = ["# YES/NO Arb Analysis", ""]
    body.append(f"**Window:** {START} -> {END}")
    body.append(f"**Categories:** {', '.join(CATEGORIES)}")
    body.append(f"**Data source:** `prices.parquet` (snapshot YES/NO prices per market)")
    body.append(f"**Cost gate:** {2*HALF_SPREAD_BPS + ROUND_TRIP_FEE_BPS + PROFIT_GATE_BPS} bps round-trip "
                f"(2×{HALF_SPREAD_BPS} half-spread + {ROUND_TRIP_FEE_BPS} fees + {PROFIT_GATE_BPS} gate)")
    body.append(f"**Notional per leg:** ${NOTIONAL_PER_LEG_USD}")
    body.append("")
    body.append("## Summary")
    body.append("")
    body.append(f"- Raw arb snapshots: **{len(events):,}**")
    body.append(f"- Distinct opportunity runs (≥5-min gap separates runs): **{len(runs):,}**")
    body.append(f"- Markets with at least one arb: **{events['market_id'].nunique():,}**")
    body.append(f"- Mean edge: **{events['net_edge_after_cost'].mean()*10_000:.1f} bps**")
    body.append(f"- Max edge: **{events['net_edge_after_cost'].max()*10_000:.1f} bps**")
    body.append(f"- Peak-edge realizable PnL (one fill per run): "
                f"**${runs['pnl_peak'].sum():,.2f}**")
    body.append("")
    body.append("## Per-category")
    body.append("")
    body.append(per_cat.to_markdown())
    body.append("")
    body.append("## Top 20 opportunity runs by peak PnL")
    body.append("")
    top = runs.sort_values("pnl_peak", ascending=False).head(20).copy()
    top["start_utc"] = pd.to_datetime(top["start_s"], unit="s", utc=True)
    top["duration_min"] = (top["duration_s"] / 60).round(1)
    body.append(top[["category", "market_id", "start_utc", "duration_min", "peak_edge",
                     "pnl_peak", "samples"]].round(4).to_markdown(index=False))
    body.append("")
    body.append("## Decision")
    body.append("")
    total_peak = float(runs["pnl_peak"].sum())
    if total_peak > 200 and len(runs) >= 20:
        body.append(f"**Build the strategy.** {len(runs):,} distinct runs × "
                    f"${total_peak:,.0f} peak-edge PnL justifies implementation.")
    else:
        body.append(f"**Do not build (yet).** Only {len(runs):,} runs and "
                    f"${total_peak:,.0f} peak-edge PnL — not enough density "
                    f"to justify an always-on arb bot after accounting for "
                    f"the fact that real arbs must be taken faster than our "
                    f"snapshot grid resolves.")
    body.append("")
    (out_dir / "YES_NO_ARB_ANALYSIS.md").write_text("\n".join(body), encoding="utf-8")
    runs.to_csv(out_dir / "YES_NO_ARB_RUNS.csv", index=False)

    print()
    print(f"Raw snapshots: {len(events):,}")
    print(f"Distinct runs: {len(runs):,}")
    print(f"Markets:       {events['market_id'].nunique():,}")
    print(f"Peak-edge PnL: ${runs['pnl_peak'].sum():,.2f}")
    print()
    print(f"Wrote: docs/YES_NO_ARB_ANALYSIS.md  +  YES_NO_ARB_EVENTS.csv + YES_NO_ARB_RUNS.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Statistical Arbitrage Backtest
================================
Runs and compares three stat-arb strategies on historical Polymarket data:

  1. CALENDAR CASCADE  — Buy lagging markets when a shorter-deadline market jumps.
  2. MONOTONICITY ARB  — Riskless arb when P(earlier deadline) > P(later deadline).
  3. PAIRS TRADING     — Z-score mean reversion on correlated same-event markets.

Usage:
    python scripts/backtest/run_stat_arb_backtest.py
    python scripts/backtest/run_stat_arb_backtest.py --strategy cascade
    python scripts/backtest/run_stat_arb_backtest.py --strategy pairs --min-corr 0.7
    python scripts/backtest/run_stat_arb_backtest.py --strategy all --output results/stat_arb.csv
"""

import argparse
import os
import sys
import warnings
from typing import List

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.trading.strategies.calendar_cascade import (
    CalendarCascadeConfig,
    CalendarCascadeStrategy,
)
from src.trading.strategies.pairs_trading import (
    PairsTradingConfig,
    PairsTradingStrategy,
)

# ── Data loading ──────────────────────────────────────────────────────────────

RESEARCH_CATEGORIES = [
    "Economy", "Finance", "Geopolitics", "Politics",
    "Art_and_Culture", "Climate_and_Science",
]

def load_trades(research_dir: str, categories: List[str] = None) -> pd.DataFrame:
    cats = categories or RESEARCH_CATEGORIES
    all_trades = []
    for cat in cats:
        path = os.path.join(research_dir, cat, "trades.parquet")
        if os.path.exists(path):
            t = pd.read_parquet(
                path,
                columns=["conditionId", "title", "eventSlug", "outcomeIndex",
                         "price", "timestamp", "size"],
            )
            all_trades.append(t)
    if not all_trades:
        raise FileNotFoundError(f"No trades.parquet found in {research_dir}")

    trades = pd.concat(all_trades, ignore_index=True)
    trades["yes_price"] = np.where(
        trades["outcomeIndex"] == 1, 1.0 - trades["price"], trades["price"]
    )
    trades = trades[(trades["yes_price"] > 0.001) & (trades["yes_price"] < 0.999)].copy()
    trades["ts"] = pd.to_datetime(trades["timestamp"], unit="s", utc=True)
    print(f"Loaded {len(trades):,} trades from {len(all_trades)} categories")
    return trades


# ── Cascade backtest ─────────────────────────────────────────────────────────

def run_cascade_backtest(
    trades: pd.DataFrame,
    cfg: CalendarCascadeConfig,
    capital: float = 10_000.0,
    trade_size_pct: float = 0.05,
) -> pd.DataFrame:
    strategy = CalendarCascadeStrategy(config=cfg)
    signals, violations = strategy.scan(trades)

    print(f"  Cascade signals: {len(signals)}, Monotonicity violations: {len(violations)}")
    trade_size = capital * trade_size_pct
    records = []

    # ── Cascade trades ──
    # Group signals by (event, follow_cid, lead_cid) and pair entries with exits
    # Simple simulation: enter at signal timestamp, exit at target or max hold
    event_series = strategy.build_event_series(trades)

    for ev_slug, (sorted_cids, deadlines) in event_series.items():
        pivot = strategy.build_pivot(trades, ev_slug, sorted_cids)
        if pivot.empty:
            continue
        present = [c for c in sorted_cids if c in pivot.columns]
        pivot = pivot[present]

        for sig in [s for s in signals if s.event_slug == ev_slug and s.signal_type == "cascade"]:
            if sig.follow_cid not in pivot.columns:
                continue
            follow_series = pivot[sig.follow_cid]
            entry_ts = sig.timestamp
            entry_price = sig.follow_price

            target_price = entry_price + sig.spread * cfg.target_capture
            max_exit_ts = entry_ts + pd.Timedelta(minutes=cfg.max_hold_periods * 5)
            window = follow_series.loc[entry_ts:max_exit_ts].dropna()
            if window.empty:
                continue

            exit_slice = window[window >= target_price]
            if not exit_slice.empty:
                exit_price = float(exit_slice.iloc[0])
                exit_ts = exit_slice.index[0]
                exit_type = "target"
            else:
                exit_price = float(window.iloc[-1])
                exit_ts = window.index[-1]
                exit_type = "timeout"

            shares = trade_size / max(entry_price, 0.05)
            gross = shares * (exit_price - entry_price)
            fee = trade_size * cfg.fee_rate * 2
            records.append({
                "strategy": "cascade",
                "event": ev_slug,
                "entry_ts": entry_ts,
                "exit_ts": exit_ts,
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "spread_at_entry": round(sig.spread, 4),
                "hold_min": round((exit_ts - entry_ts).total_seconds() / 60, 1),
                "exit_type": exit_type,
                "gross_pnl": round(gross, 2),
                "net_pnl": round(gross - fee, 2),
                "roi": round((gross - fee) / trade_size, 4),
            })

    # ── Monotonicity trades ──
    for v in violations:
        # Buy YES late (p_late) + Buy NO early (1 - p_early)
        # Cost: p_late + (1 - p_early) = 1 - spread
        cost_per_share = 1.0 - v.spread
        shares = trade_size / max(cost_per_share, 0.05)
        gross = shares * v.spread
        fee = trade_size * cfg.mono_fee_rate * 2
        records.append({
            "strategy": "monotone",
            "event": v.event_slug,
            "entry_ts": v.timestamp,
            "exit_ts": v.timestamp,  # point-in-time (hold to resolution)
            "entry_price": round(v.late_price, 4),
            "exit_price": 1.0,
            "spread_at_entry": round(v.spread, 4),
            "hold_min": round(v.spread * 0, 0),  # unknown, resolved at maturity
            "exit_type": "resolution",
            "gross_pnl": round(gross, 2),
            "net_pnl": round(gross - fee, 2),
            "roi": round((gross - fee) / trade_size, 4),
        })

    return pd.DataFrame(records)


# ── Pairs backtest ────────────────────────────────────────────────────────────

def run_pairs_backtest(
    trades: pd.DataFrame,
    cfg: PairsTradingConfig,
) -> pd.DataFrame:
    strategy = PairsTradingStrategy(config=cfg)
    titles_map = trades.groupby("conditionId")["title"].first().to_dict() if "title" in trades.columns else {}

    pairs = strategy.find_pairs(trades, titles_map=titles_map)
    print(f"  Qualifying pairs: {len(pairs)}")
    if not pairs:
        return pd.DataFrame()

    signals = strategy.generate_signals(pairs, trades)
    entries = {s.pair_id: s for s in signals if s.signal_type == "entry"}

    records = []
    for sig in signals:
        if sig.signal_type != "exit":
            continue
        pair_id = sig.pair_id
        if pair_id not in entries:
            continue
        en = entries[pair_id]
        hold_h = (sig.timestamp - en.timestamp).total_seconds() / 3600

        ep1, ep2 = en.entry_price1, en.entry_price2
        xp1, xp2 = sig.entry_price1, sig.entry_price2
        if any(np.isnan(v) for v in [ep1, ep2, xp1, xp2]):
            continue

        d_sign = 1 if en.direction == "long1_short2" else -1
        leg = cfg.leg_size_usd
        shares1 = leg / max(ep1, 0.05)
        shares2 = leg / max(ep2, 0.05)
        ret1 = d_sign * (xp1 - ep1) * shares1
        ret2 = -d_sign * (xp2 - ep2) * shares2
        fee = 2 * cfg.fee_rate * leg
        net = ret1 + ret2 - fee

        records.append({
            "strategy": "pairs",
            "event": en.event_slug,
            "entry_ts": en.timestamp,
            "exit_ts": sig.timestamp,
            "entry_price1": round(ep1, 4),
            "entry_price2": round(ep2, 4),
            "exit_price1": round(xp1, 4),
            "exit_price2": round(xp2, 4),
            "entry_z": round(en.z_score, 2),
            "exit_z": round(sig.z_score, 2),
            "hold_h": round(hold_h, 1),
            "exit_type": sig.metadata.get("exit_reason", ""),
            "net_pnl": round(net, 2),
            "roi": round(net / (2 * leg), 4),
        })
        # Remove entry so it's not matched twice
        del entries[pair_id]

    return pd.DataFrame(records)


# ── Summary printer ───────────────────────────────────────────────────────────

def print_summary(df: pd.DataFrame, label: str, trade_size: float = 500.0):
    print(f"\n{'='*65}")
    print(f"  {label}")
    print(f"{'='*65}")
    if df.empty:
        print("  No trades.")
        return
    win = (df["net_pnl"] > 0).mean()
    print(f"  Trades      : {len(df)}")
    print(f"  Win rate    : {win:.1%}")
    print(f"  Total PnL   : ${df['net_pnl'].sum():,.2f}")
    print(f"  Mean PnL    : ${df['net_pnl'].mean():.2f}")
    print(f"  Mean ROI    : {df['roi'].mean():.2%}")
    print(f"  Median ROI  : {df['roi'].median():.2%}")
    if df['roi'].std() > 0:
        print(f"  Sharpe      : {df['roi'].mean() / df['roi'].std():.2f}")
    if "hold_min" in df.columns:
        print(f"  Avg hold    : {df['hold_min'].mean():.1f} min")
    if "hold_h" in df.columns:
        print(f"  Avg hold    : {df['hold_h'].mean():.1f} h")
    if "exit_type" in df.columns:
        print(f"\n  Exit breakdown:")
        print(df.groupby("exit_type")["net_pnl"].agg(["count","mean","sum"])
              .rename(columns={"count":"n","mean":"avg_pnl","sum":"total_pnl"})
              .to_string())


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Stat arb backtest")
    parser.add_argument("--research-dir", default="data/research")
    parser.add_argument("--strategy", choices=["cascade", "monotone", "pairs", "all"],
                        default="all")
    parser.add_argument("--capital", type=float, default=10_000.0)
    parser.add_argument("--trade-size-pct", type=float, default=0.05)
    parser.add_argument("--output", default=None, help="Save trades CSV to path")

    # Cascade params
    parser.add_argument("--jump-threshold", type=float, default=0.08)
    parser.add_argument("--n-followers", type=int, default=3)
    parser.add_argument("--target-capture", type=float, default=0.50)

    # Pairs params
    parser.add_argument("--min-corr", type=float, default=0.60)
    parser.add_argument("--z-entry", type=float, default=2.0)
    parser.add_argument("--max-half-life", type=float, default=72.0)
    parser.add_argument("--leg-size", type=float, default=250.0)

    args = parser.parse_args()

    trades = load_trades(args.research_dir)
    all_results = []

    cascade_cfg = CalendarCascadeConfig(
        jump_threshold=args.jump_threshold,
        n_followers=args.n_followers,
        target_capture=args.target_capture,
    )
    pairs_cfg = PairsTradingConfig(
        min_corr=args.min_corr,
        z_entry=args.z_entry,
        max_half_life_h=args.max_half_life,
        leg_size_usd=args.leg_size,
    )

    if args.strategy in ("cascade", "monotone", "all"):
        print("\n[1/3] Running Calendar Cascade + Monotonicity Arb...")
        cas_df = run_cascade_backtest(trades, cascade_cfg, args.capital, args.trade_size_pct)
        cascade_df = cas_df[cas_df["strategy"] == "cascade"]
        mono_df    = cas_df[cas_df["strategy"] == "monotone"]
        print_summary(cascade_df, "CALENDAR CASCADE ARB", args.capital * args.trade_size_pct)
        print_summary(mono_df,    "MONOTONICITY ARB (RISKLESS)", args.capital * args.trade_size_pct)
        all_results.append(cas_df)

    if args.strategy in ("pairs", "all"):
        print("\n[2/3] Running Pairs Trading...")
        pairs_df = run_pairs_backtest(trades, pairs_cfg)
        print_summary(pairs_df, "PAIRS TRADING", args.leg_size * 2)
        all_results.append(pairs_df)

    if all_results:
        combined = pd.concat(all_results, ignore_index=True)
        print(f"\n{'='*65}")
        print(f"  COMBINED STAT ARB PORTFOLIO")
        print(f"{'='*65}")
        by_strat = combined.groupby("strategy")["net_pnl"].agg(["count","mean","sum"])
        print(by_strat.to_string())
        print(f"\n  Total trades: {len(combined)}")
        print(f"  Total PnL:    ${combined['net_pnl'].sum():,.2f}")
        print(f"  Win rate:     {(combined['net_pnl']>0).mean():.1%}")

        if args.output:
            combined.to_csv(args.output, index=False)
            print(f"\n  Saved to {args.output}")


if __name__ == "__main__":
    main()

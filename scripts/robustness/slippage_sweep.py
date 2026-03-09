#!/usr/bin/env python3
"""
Slippage break-even analysis for the whale-following strategy.

Runs the backtest once (leveraging whale-set cache), then post-processes the
trade records at multiple slippage levels to find the break-even point.

Slippage model: entry fill is N% worse than the whale's observed trade price.
  BUY  (YES tokens): new_entry = min(entry_price * (1 + s), 0.99)
  SELL (NO  tokens): new_entry_no = min((1 - entry_price) * (1 + s), 0.99)
                     exit_price column already holds the NO token payout.

The existing backtest already applies a 3% cost haircut (gross * 0.97).
Slippage is additive on top of that: it degrades the fill price, which reduces
gross PnL before the 3% haircut is applied.

Usage:
    PYTHONPATH=.:src venv/bin/python3 scripts/robustness/slippage_sweep.py
    PYTHONPATH=.:src venv/bin/python3 scripts/robustness/slippage_sweep.py --slippage 0,0.5,1,2,3,5,7,10
    PYTHONPATH=.:src venv/bin/python3 scripts/robustness/slippage_sweep.py --output-dir data/output/robustness
"""

import argparse
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / "src"))

import numpy as np
import pandas as pd

from scripts.backtest.run_whale_category_backtest import run_whale_category_backtest
from src.whale_strategy.whale_config import load_whale_config


def recompute_metrics(
    trades_df: pd.DataFrame,
    slippage: float,
    capital: float,
) -> dict:
    """
    Recompute backtest metrics after applying slippage to entry prices.

    For each trade row, worsens the entry price by `slippage` fraction and
    recomputes gross and net PnL.  The 3% cost haircut is preserved.

    Args:
        trades_df: DataFrame from run_whale_category_backtest (raw trade records).
        slippage:  Fractional slippage on entry, e.g. 0.02 = 2%.
        capital:   Starting capital (for equity curve and CAGR).

    Returns:
        Dict of metrics at this slippage level.
    """
    df = trades_df.copy()
    direction = df["direction"].str.upper()

    # --- Recompute entry price after slippage ---
    buy_mask = direction == "BUY"
    sell_mask = ~buy_mask

    # BUY: pay more for YES tokens
    new_entry_buy = (df.loc[buy_mask, "entry_price"] * (1.0 + slippage)).clip(upper=0.99)

    # SELL: pay more for NO tokens (entry_no = 1 - entry_price gets worse)
    entry_no_orig = 1.0 - df.loc[sell_mask, "entry_price"]
    new_entry_no = (entry_no_orig * (1.0 + slippage)).clip(upper=0.99)

    # --- Recompute gross PnL ---
    size = df["position_size"]
    exit_price = df["exit_price"]

    new_gross = pd.Series(index=df.index, dtype=float)

    # BUY: gross = (exit - new_entry) * (size / new_entry)
    new_gross[buy_mask] = (
        (exit_price[buy_mask] - new_entry_buy)
        * (size[buy_mask] / new_entry_buy.values)
    )

    # SELL: gross = (exit - new_entry_no) * (size / new_entry_no)
    # exit_price for SELL is already the NO token payout
    new_gross[sell_mask] = (
        (exit_price[sell_mask] - new_entry_no)
        * (size[sell_mask] / new_entry_no.clip(lower=1e-6).values)
    )

    # Net PnL preserves the same 3% cost haircut
    new_net = new_gross * 0.97

    df["gross_pnl"] = new_gross
    df["net_pnl"] = new_net

    # --- Metrics ---
    total_pnl = df["net_pnl"].sum()
    total = len(df)
    wins = (df["net_pnl"] > 0).sum()
    win_rate = wins / total if total else 0.0

    wins_pnl = df.loc[df["net_pnl"] > 0, "net_pnl"]
    losses_pnl = df.loc[df["net_pnl"] <= 0, "net_pnl"]
    avg_win = float(wins_pnl.mean()) if len(wins_pnl) else 0.0
    avg_loss = float(losses_pnl.mean()) if len(losses_pnl) else 0.0
    profit_factor = (
        wins_pnl.sum() / abs(losses_pnl.sum())
        if losses_pnl.sum() != 0
        else float("inf")
    )

    # Daily equity curve for Sharpe and max drawdown
    daily_pnl = df.groupby(pd.to_datetime(df["exit_date"]).dt.normalize())["net_pnl"].sum()
    date_range = pd.date_range(start=daily_pnl.index.min(), end=daily_pnl.index.max(), freq="D")
    daily_pnl = daily_pnl.reindex(date_range, fill_value=0).sort_index()
    cumulative_pnl = daily_pnl.cumsum()
    equity = capital + cumulative_pnl
    daily_returns = equity.pct_change().dropna()

    sharpe = 0.0
    if len(daily_returns) >= 5 and daily_returns.std() > 0:
        sharpe = float((daily_returns.mean() / daily_returns.std()) * (252 ** 0.5))

    max_dd = 0.0
    peak = capital
    for v in equity:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd

    # CAGR
    n_days = (equity.index[-1] - equity.index[0]).days
    cagr = 0.0
    if n_days > 0 and equity.iloc[0] > 0:
        cagr = (float(equity.iloc[-1]) / float(equity.iloc[0])) ** (365.0 / n_days) - 1.0

    return {
        "slippage_pct": round(slippage * 100, 2),
        "total_trades": total,
        "win_rate_pct": round(win_rate * 100, 1),
        "total_net_pnl": round(total_pnl, 0),
        "roi_pct": round((total_pnl / capital) * 100, 1),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "profit_factor": round(min(profit_factor, 99.0), 2),
        "avg_win": round(avg_win, 0),
        "avg_loss": round(avg_loss, 0),
        "cagr_pct": round(cagr * 100, 1),
    }


def find_breakeven(results: list[dict], capital: float) -> float | None:
    """
    Linearly interpolate the break-even slippage (where total_net_pnl = 0).

    Returns the slippage % at which PnL crosses zero, or None if no crossing.
    """
    pnls = [(r["slippage_pct"], r["total_net_pnl"]) for r in results]
    for i in range(len(pnls) - 1):
        s0, p0 = pnls[i]
        s1, p1 = pnls[i + 1]
        if p0 >= 0 >= p1:  # crossing from positive to negative
            # linear interpolation
            frac = p0 / (p0 - p1)
            return round(s0 + frac * (s1 - s0), 2)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Slippage break-even sweep for whale strategy")
    parser.add_argument(
        "--research-dir", type=Path, default=_project_root / "data" / "research",
    )
    parser.add_argument(
        "--resolutions-dir", type=Path, default=_project_root / "data" / "poly_cat",
        help="Extra resolutions directory (default: data/poly_cat)",
    )
    parser.add_argument("--capital", type=float, default=1_000_000)
    parser.add_argument("--min-usd", type=float, default=100)
    parser.add_argument(
        "--slippage",
        default="0,0.5,1,2,3,5,7,10",
        help="Comma-separated slippage levels in percent (default: 0,0.5,1,2,3,5,7,10)",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=_project_root / "data" / "output" / "robustness",
    )
    parser.add_argument(
        "--workers", type=int, default=35,
        help="Parallel workers for whale-set building (default: 35)",
    )
    parser.add_argument(
        "--whale-cache-dir", type=Path,
        default=_project_root / "cache" / "whale_sets",
    )
    args = parser.parse_args()

    slippage_levels = [float(x.strip()) / 100.0 for x in args.slippage.split(",")]

    whale_config = load_whale_config()

    print("=" * 72)
    print("SLIPPAGE BREAK-EVEN ANALYSIS")
    print("=" * 72)
    print(f"  research_dir   : {args.research_dir}")
    print(f"  resolutions_dir: {args.resolutions_dir}")
    print(f"  capital        : ${args.capital:,.0f}")
    print(f"  slippage levels: {[f'{s*100:.1f}%' for s in slippage_levels]}")
    print(f"  whale_config   : {whale_config}")
    print()

    # --- Step 1: Run baseline backtest once ---
    print("Step 1: Running baseline backtest (s=0%)...")
    result = run_whale_category_backtest(
        research_dir=args.research_dir,
        capital=args.capital,
        min_usd=args.min_usd,
        whale_config=whale_config,
        rebalance_freq="1W",
        n_workers=args.workers,
        extra_resolutions_dir=args.resolutions_dir,
        whale_cache_dir=args.whale_cache_dir,
    )

    if "error" in result:
        print(f"Backtest failed: {result['error']}")
        return 1

    trades_df = result["trades_df"]
    n_trades = len(trades_df)
    print(f"  Baseline: {n_trades} trades, WR={result['win_rate']*100:.1f}%, "
          f"PnL=${result['total_net_pnl']:,.0f}, Sharpe={result['sharpe_ratio']:.2f}")
    print()

    # --- Step 2: Sweep slippage levels ---
    print("Step 2: Sweeping slippage levels (post-processing trade records)...")
    print()

    header = (
        f"{'Slippage':>9}  {'Trades':>6}  {'Win%':>5}  {'Sharpe':>6}  "
        f"{'MaxDD%':>6}  {'PF':>5}  {'Net PnL':>12}  {'CAGR%':>7}"
    )
    print(header)
    print("-" * len(header))

    all_results = []
    for s in slippage_levels:
        metrics = recompute_metrics(trades_df, s, args.capital)
        all_results.append(metrics)

        marker = ""
        if metrics["total_net_pnl"] < 0:
            marker = "  <-- LOSS"
        elif metrics["slippage_pct"] == 0.0:
            marker = "  <-- baseline"

        print(
            f"{metrics['slippage_pct']:>8.1f}%  "
            f"{metrics['total_trades']:>6,}  "
            f"{metrics['win_rate_pct']:>5.1f}  "
            f"{metrics['sharpe']:>6.2f}  "
            f"{metrics['max_drawdown_pct']:>6.2f}  "
            f"{metrics['profit_factor']:>5.2f}  "
            f"${metrics['total_net_pnl']:>11,.0f}  "
            f"{metrics['cagr_pct']:>6.1f}%"
            f"{marker}"
        )

    print()

    # --- Step 3: Break-even ---
    be = find_breakeven(all_results, args.capital)
    if be is not None:
        print(f"Break-even slippage: ~{be:.2f}%")
        print(f"  → Strategy stays profitable up to ~{be:.2f}% worse fill vs whale trade price.")
    else:
        profitable_at_all = all(r["total_net_pnl"] > 0 for r in all_results)
        if profitable_at_all:
            max_s = max(r["slippage_pct"] for r in all_results)
            print(f"Break-even not reached: strategy is profitable at all tested levels (up to {max_s:.1f}%).")
        else:
            print("Break-even not reached: strategy is unprofitable at all tested levels.")

    # --- Step 4: Sharpe degradation summary ---
    baseline_sharpe = all_results[0]["sharpe"]
    print()
    print("Sharpe degradation vs baseline:")
    for r in all_results:
        if r["slippage_pct"] == 0.0:
            continue
        delta = r["sharpe"] - baseline_sharpe
        pct_drop = (delta / baseline_sharpe * 100) if baseline_sharpe != 0 else 0
        print(f"  {r['slippage_pct']:.1f}%  Sharpe={r['sharpe']:.2f}  ({delta:+.2f}, {pct_drop:+.1f}%)")

    # --- Step 5: Save ---
    results_df = pd.DataFrame(all_results)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.output_dir / "slippage_sweep.csv"
    results_df.to_csv(out_path, index=False)
    print()
    print(f"Results saved to {out_path}")

    # Also save the baseline trades with entry prices for manual inspection
    trades_out = args.output_dir / "slippage_sweep_baseline_trades.csv"
    trades_df.to_csv(trades_out, index=False)
    print(f"Baseline trades saved to {trades_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

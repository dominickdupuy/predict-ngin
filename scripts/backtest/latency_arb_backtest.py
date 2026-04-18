"""
latency_arb_backtest.py — Portfolio-level backtest of latency arb strategy

Oracle assumption: the pipeline detects every price-jump event at the instant the
YES price first crosses 85% (for YES-resolving) or first crosses below 15% (for
NO-resolving).  This is the upper bound — the NER pipeline's job is to approach
this ideal by catching the signal before or as the crossing happens.

For each signal crossing the backtest:
  1. Enters at entry_price + ENTRY_SPREAD (simulates taker order)
  2. Exits at the first 10-min VWAP that hits EXIT_TARGET (97%), OR at STOP_HOURS
     whichever comes first — using the actual price path from the parquet file
  3. Applies fee on the notional of each leg

Portfolio rules:
  • Start with INITIAL_CAPITAL
  • Size each trade: Kelly fraction × available_capital, capped at KELLY_CAP
  • Max MAX_POSITIONS open simultaneously
  • If at capacity, skip signals (they would have been skipped live too)

Output:
  • Trade log CSV
  • Equity curve CSV
  • Summary statistics (Sharpe, CAGR, max drawdown, win rate)

Usage:
    PYTHONPATH=.:src venv/bin/python3 scripts/backtest/latency_arb_backtest.py
    PYTHONPATH=.:src venv/bin/python3 scripts/backtest/latency_arb_backtest.py \\
        --capital 10000 --kelly-cap 0.25 --stop-hours 4 --max-positions 5
"""

import argparse
import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
import sys

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("lat_arb_bt")

ROOT = Path(__file__).resolve().parent.parent.parent

# ── Cost model ────────────────────────────────────────────────────────────────
ENTRY_SPREAD  = 0.015   # taker spread above mid at entry
EXIT_SPREAD   = 0.005   # market-maker spread below mid at exit
FEE_RATE      = 0.002   # 0.2% of notional per leg (polymarket taker fee)

# ── Signal thresholds ─────────────────────────────────────────────────────────
SIGNAL_LOWER  = 0.75    # price must have been ≤ this before the crossing
SIGNAL_UPPER  = 0.85    # price must cross ≥ this to trigger entry
EXIT_TARGET   = 0.97    # exit when price reaches this
VWAP_BUCKET   = "10min"

# ── Categories (in data/poly_cat/) ────────────────────────────────────────────
CATEGORIES = [
    "Art_and_Culture", "Climate_and_Science", "Economy",
    "Finance", "Geopolitics", "Other", "Politics",
]


@dataclass
class Trade:
    condition_id: str
    category: str
    resolution: str
    entry_dt: str
    entry_price: float        # actual fill price (mid + spread)
    exit_dt: str
    exit_price: float         # actual fill price (mid - spread)
    exit_reason: str          # "target" | "stop_N h" | "end_of_data"
    gross_pnl: float          # per contract (exit - entry, before fee)
    net_pnl: float            # after fee
    position_usd: float       # dollars deployed
    net_pnl_usd: float        # dollars P&L
    holding_hours: float
    entry_capital: float      # portfolio equity at entry


@dataclass
class PortfolioStats:
    n_trades: int
    n_wins: int
    win_rate: float
    total_net_pnl_usd: float
    total_return_pct: float
    cagr_pct: float
    sharpe: float
    max_drawdown_pct: float
    avg_holding_hours: float
    avg_net_roi_per_trade: float
    skipped_at_capacity: int
    date_range: str


# ── Load and build VWAP series per market ─────────────────────────────────────

def load_vwap_series(
    category: str,
    condition_ids: set,
) -> dict[str, pd.Series]:
    """
    Load parquet for one category, filter to condition_ids,
    normalize to YES prices, build 10-min VWAP, return dict[cid → Series].
    """
    path = ROOT / "data" / "poly_cat" / category / "trades.parquet"
    if not path.exists():
        return {}

    df = pd.read_parquet(
        path,
        columns=["conditionId", "price", "size", "timestamp", "outcomeIndex"],
    )
    df = df[df["conditionId"].isin(condition_ids)]
    if df.empty:
        return {}

    df["yes_price"] = np.where(df["outcomeIndex"] == 1, 1.0 - df["price"], df["price"])
    df = df[(df["yes_price"] >= 0.001) & (df["yes_price"] <= 0.999)]
    df["dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)

    result = {}
    for cid, grp in df.groupby("conditionId"):
        grp = grp.set_index("dt").sort_index()
        num   = (grp["yes_price"] * grp["size"]).resample(VWAP_BUCKET).sum()
        denom = grp["size"].resample(VWAP_BUCKET).sum()
        vwap  = (num / denom).dropna()
        if len(vwap) >= 3:
            result[cid] = vwap
    return result


# ── Find all signal crossings in a price series ───────────────────────────────

def find_crossings(
    series: pd.Series,
    resolution: str,
) -> list[tuple]:
    """
    Find every time the price (in resolution direction) crosses from
    ≤ SIGNAL_LOWER to ≥ SIGNAL_UPPER.

    Uses the FIRST such crossing for each "sub-SIGNAL_LOWER episode"
    to avoid look-ahead bias.  Each episode: a contiguous stretch
    below SIGNAL_LOWER followed by a rise above SIGNAL_UPPER.

    Returns list of (crossing_datetime, entry_price).
    """
    # Flip NO markets so we always look for an upward crossing
    s = series if resolution == "YES" else 1.0 - series

    crossings = []
    in_episode = False
    last_low_idx = None

    for i in range(len(s)):
        v = float(s.iloc[i])
        if v <= SIGNAL_LOWER:
            in_episode = True
            last_low_idx = i
        elif in_episode and v >= SIGNAL_UPPER:
            # Crossing detected
            crossings.append((s.index[i], v))
            in_episode = False   # wait for another dip below SIGNAL_LOWER

    return crossings


# ── Simulate a single trade's exit on the actual price path ──────────────────

def simulate_exit(
    series: pd.Series,
    resolution: str,
    entry_dt: pd.Timestamp,
    stop_hours: float,
) -> tuple[pd.Timestamp, float, str]:
    """
    Given entry at entry_dt, walk forward on `series` and exit:
      - at EXIT_TARGET (97% in resolution direction), or
      - at the stop_hours time limit

    Returns (exit_datetime, exit_price_in_resolution_direction, reason).
    """
    s = series if resolution == "YES" else 1.0 - series

    after = s[s.index > entry_dt]
    if after.empty:
        return entry_dt, float(s.iloc[-1]) if not s.empty else EXIT_TARGET, "end_of_data"

    stop_dt = entry_dt + pd.Timedelta(hours=stop_hours)

    for dt, price in after.items():
        if price >= EXIT_TARGET:
            return dt, float(price), "target"
        if dt >= stop_dt:
            return dt, float(price), f"stop_{stop_hours:.0f}h"

    # End of data before either target or stop
    last_dt = after.index[-1]
    return last_dt, float(after.iloc[-1]), "end_of_data"


# ── Portfolio simulation ───────────────────────────────────────────────────────

def simulate_portfolio(
    all_signals: list[dict],
    initial_capital: float,
    kelly_cap: float,
    max_positions: int,
    stop_hours: float,
) -> tuple[list[Trade], list[dict]]:
    """
    Walk through signals in time order, opening and closing positions.
    Returns (trades, equity_curve_rows).
    """
    # Sort all signals by entry datetime
    all_signals.sort(key=lambda x: x["entry_dt"])

    capital       = initial_capital
    open_positions: list[dict] = []   # list of {exit_dt, exit_price, size_usd, ...}
    trades:         list[Trade] = []
    equity_rows:    list[dict]  = []
    skipped        = 0

    for sig in all_signals:
        entry_dt = sig["entry_dt"]

        # Close any positions that have already exited
        still_open = []
        for pos in open_positions:
            if pos["exit_dt"] <= entry_dt:
                # Realise P&L
                pnl_usd = pos["net_pnl_usd"]
                capital += pos["size_usd"] + pnl_usd
                trades.append(pos["trade"])
                equity_rows.append({
                    "dt": pos["exit_dt"].isoformat(),
                    "equity": capital,
                    "event": "close",
                    "condition_id": pos["trade"].condition_id,
                })
            else:
                still_open.append(pos)
        open_positions = still_open

        # Skip if at max concurrent positions
        if len(open_positions) >= max_positions:
            skipped += 1
            continue

        # Size: kelly × available capital, capped
        residual     = 1.0 - sig["entry_price"]
        kelly_frac   = min(residual / max(1.0 - residual, 0.01), kelly_cap)
        position_usd = kelly_frac * capital

        if position_usd > capital:
            position_usd = capital
        if position_usd < 10:
            continue

        # Actual fill prices (mid ± spread)
        fill_entry = sig["entry_price"] + ENTRY_SPREAD
        fill_entry = min(fill_entry, 0.999)

        fill_exit  = sig["exit_price"] - EXIT_SPREAD
        fill_exit  = max(fill_exit, 0.001)

        # P&L per contract
        gross_pnl   = fill_exit - fill_entry
        fee         = FEE_RATE * (fill_entry + fill_exit)   # both legs
        net_pnl     = gross_pnl - fee

        # Contracts purchased = position_usd / fill_entry
        contracts   = position_usd / fill_entry
        net_pnl_usd = net_pnl * contracts
        holding_h   = (sig["exit_dt"] - entry_dt).total_seconds() / 3600

        trade = Trade(
            condition_id     = sig["condition_id"],
            category         = sig["category"],
            resolution       = sig["resolution"],
            entry_dt         = entry_dt.isoformat(),
            entry_price      = round(fill_entry, 4),
            exit_dt          = sig["exit_dt"].isoformat(),
            exit_price       = round(fill_exit, 4),
            exit_reason      = sig["exit_reason"],
            gross_pnl        = round(gross_pnl, 5),
            net_pnl          = round(net_pnl, 5),
            position_usd     = round(position_usd, 2),
            net_pnl_usd      = round(net_pnl_usd, 2),
            holding_hours    = round(holding_h, 2),
            entry_capital    = round(capital, 2),
        )

        # Deduct position from capital immediately (it's deployed)
        capital -= position_usd
        open_positions.append({
            "exit_dt":    sig["exit_dt"],
            "exit_price": fill_exit,
            "size_usd":   position_usd,
            "net_pnl_usd": net_pnl_usd,
            "trade":      trade,
        })

        equity_rows.append({
            "dt":           entry_dt.isoformat(),
            "equity":       capital + sum(p["size_usd"] for p in open_positions),
            "event":        "open",
            "condition_id": sig["condition_id"],
        })

    # Close any remaining positions at their scheduled exit
    for pos in sorted(open_positions, key=lambda x: x["exit_dt"]):
        pnl_usd = pos["net_pnl_usd"]
        capital += pos["size_usd"] + pnl_usd
        trades.append(pos["trade"])
        equity_rows.append({
            "dt": pos["exit_dt"].isoformat(),
            "equity": capital,
            "event": "close",
            "condition_id": pos["trade"].condition_id,
        })

    return trades, equity_rows, skipped


# ── Statistics ────────────────────────────────────────────────────────────────

def compute_stats(
    trades: list[Trade],
    equity_rows: list[dict],
    initial_capital: float,
    skipped: int,
) -> PortfolioStats:
    if not trades:
        return None

    pnls  = [t.net_pnl_usd for t in trades]
    wins  = [p for p in pnls if p > 0]
    rois  = [t.net_pnl / t.entry_price for t in trades]

    eq_df = pd.DataFrame(equity_rows).drop_duplicates("dt").set_index("dt").sort_index()
    equities = eq_df["equity"].values.astype(float)

    # Sharpe (daily returns approximation)
    if len(equities) > 2:
        daily_rets = np.diff(equities) / equities[:-1]
        sharpe = (daily_rets.mean() / (daily_rets.std() + 1e-9)) * np.sqrt(252)
    else:
        sharpe = 0.0

    # Max drawdown
    rolling_max = np.maximum.accumulate(equities)
    drawdowns   = (equities - rolling_max) / rolling_max
    max_dd      = float(drawdowns.min())

    # CAGR
    final_eq = equities[-1]
    # Infer total period from trade dates
    dates = [pd.Timestamp(t.entry_dt) for t in trades]
    period_days = (max(dates) - min(dates)).days if len(dates) > 1 else 365
    years = max(period_days / 365.25, 0.01)
    cagr  = (final_eq / initial_capital) ** (1 / years) - 1

    return PortfolioStats(
        n_trades             = len(trades),
        n_wins               = len(wins),
        win_rate             = len(wins) / len(trades),
        total_net_pnl_usd    = sum(pnls),
        total_return_pct     = (final_eq / initial_capital - 1) * 100,
        cagr_pct             = cagr * 100,
        sharpe               = sharpe,
        max_drawdown_pct     = max_dd * 100,
        avg_holding_hours    = np.mean([t.holding_hours for t in trades]),
        avg_net_roi_per_trade = np.mean(rois) * 100,
        skipped_at_capacity  = skipped,
        date_range           = f"{min(dates).date()} → {max(dates).date()}",
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Latency arb portfolio backtest")
    p.add_argument("--capital",       type=float, default=10_000)
    p.add_argument("--kelly-cap",     type=float, default=0.25,  help="Max Kelly fraction (default 0.25)")
    p.add_argument("--stop-hours",    type=float, default=4.0,   help="Time stop in hours (default 4)")
    p.add_argument("--max-positions", type=int,   default=5,     help="Max concurrent positions (default 5)")
    p.add_argument("--lag-file",      default="backtests/latency_arb/systematic_lag.json")
    p.add_argument("--out-dir",       default="backtests/latency_arb")
    args = p.parse_args()

    # ── Load fast-jump events ────────────────────────────────────────────────
    log.info("Loading fast-jump event list…")
    with open(args.lag_file) as f:
        lag_data = json.load(f)

    fast = [r for r in lag_data if r["is_fast_jump"]]
    log.info(f"  {len(fast)} fast-jump events across "
             f"{len(set(r['category'] for r in fast))} categories")

    # Index by category
    from collections import defaultdict
    by_cat: dict[str, list[str]] = defaultdict(list)
    res_map: dict[str, str] = {}
    for r in fast:
        by_cat[r["category"]].append(r["condition_id"])
        res_map[r["condition_id"]] = r["resolution"]

    # ── Load VWAP series per category ────────────────────────────────────────
    log.info("Loading price paths…")
    vwap_series: dict[str, pd.Series] = {}
    for cat, cids in by_cat.items():
        log.info(f"  {cat}: {len(cids)} markets")
        vwap_series.update(load_vwap_series(cat, set(cids)))
    log.info(f"Loaded price paths for {len(vwap_series)} / {len(fast)} markets")

    # ── Find all signal crossings ─────────────────────────────────────────────
    log.info("Finding signal crossings (unbiased — first crossing per episode)…")
    all_signals = []
    for cid, series in vwap_series.items():
        res = res_map.get(cid, "YES")
        s   = series if res == "YES" else 1.0 - series

        crossings = find_crossings(series, res)
        for entry_dt, entry_price_mid in crossings:
            exit_dt, exit_price_mid, reason = simulate_exit(
                series, res, entry_dt, args.stop_hours
            )
            all_signals.append({
                "condition_id": cid,
                "category":     next(
                    (r["category"] for r in fast if r["condition_id"] == cid), "Unknown"
                ),
                "resolution":   res,
                "entry_dt":     entry_dt,
                "entry_price":  entry_price_mid,
                "exit_dt":      exit_dt,
                "exit_price":   exit_price_mid,
                "exit_reason":  reason,
            })

    log.info(f"Total crossings found: {len(all_signals)} "
             f"(avg {len(all_signals)/max(len(vwap_series),1):.1f} per market)")

    # ── Portfolio simulation ──────────────────────────────────────────────────
    log.info(f"Running portfolio simulation  capital=${args.capital:,.0f}  "
             f"kelly={args.kelly_cap:.0%}  stop={args.stop_hours:.0f}h  "
             f"max_pos={args.max_positions}")

    trades, equity_rows, skipped = simulate_portfolio(
        all_signals,
        initial_capital = args.capital,
        kelly_cap       = args.kelly_cap,
        max_positions   = args.max_positions,
        stop_hours      = args.stop_hours,
    )

    # ── Statistics ────────────────────────────────────────────────────────────
    stats = compute_stats(trades, equity_rows, args.capital, skipped)

    print(f"\n{'='*72}")
    print("  LATENCY ARB BACKTEST RESULTS")
    print(f"{'='*72}")
    print(f"  Period:               {stats.date_range}")
    print(f"  Capital:              ${args.capital:,.0f}")
    print(f"  Stop:                 {args.stop_hours:.0f} h  |  Max positions: {args.max_positions}")
    print(f"  Total trades:         {stats.n_trades}")
    print(f"  Signals skipped:      {stats.skipped_at_capacity} (at max capacity)")
    print(f"")
    print(f"  Win rate:             {stats.win_rate:.1%}")
    print(f"  Avg net ROI/trade:    {stats.avg_net_roi_per_trade:.2f}%")
    print(f"  Avg holding time:     {stats.avg_holding_hours:.1f} h")
    print(f"")
    print(f"  Total P&L:            ${stats.total_net_pnl_usd:,.2f}")
    print(f"  Total return:         {stats.total_return_pct:.1f}%")
    print(f"  CAGR:                 {stats.cagr_pct:.1f}%")
    print(f"  Sharpe ratio:         {stats.sharpe:.2f}")
    print(f"  Max drawdown:         {stats.max_drawdown_pct:.1f}%")
    print(f"{'='*72}")

    # Per-category breakdown
    from collections import defaultdict as dd
    cat_trades = dd(list)
    for t in trades:
        cat_trades[t.category].append(t)

    print("\n  BY CATEGORY:")
    print(f"  {'Category':<25} {'N':>5}  {'WR':>6}  {'Avg ROI':>8}  {'Total $':>9}  {'Reason: target%':>15}")
    for cat in sorted(cat_trades, key=lambda c: -len(cat_trades[c])):
        ts = cat_trades[cat]
        n  = len(ts)
        wr = sum(1 for t in ts if t.net_pnl_usd > 0) / n
        roi = np.mean([t.net_pnl / t.entry_price for t in ts]) * 100
        total = sum(t.net_pnl_usd for t in ts)
        pct_target = sum(1 for t in ts if t.exit_reason == "target") / n
        print(f"  {cat:<25} {n:>5}  {wr:>5.1%}  {roi:>7.2f}%  ${total:>8,.0f}  {pct_target:>14.1%}")

    # Exit reason breakdown
    from collections import Counter
    reasons = Counter(t.exit_reason for t in trades)
    print(f"\n  EXIT REASONS:")
    for reason, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
        pct = cnt / len(trades)
        avg_roi = np.mean([t.net_pnl / t.entry_price for t in trades
                           if t.exit_reason == reason]) * 100
        print(f"    {reason:<20} {cnt:>4} ({pct:.1%})   avg ROI: {avg_roi:.2f}%")

    # Worst trades
    worst = sorted(trades, key=lambda t: t.net_pnl_usd)[:5]
    print(f"\n  5 WORST TRADES:")
    for t in worst:
        print(f"    {t.condition_id[:20]}  {t.category:<20}  "
              f"entry={t.entry_price:.3f}  exit={t.exit_price:.3f}  "
              f"P&L=${t.net_pnl_usd:+,.0f}  ({t.exit_reason})")

    # ── Save outputs ─────────────────────────────────────────────────────────
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    trades_df = pd.DataFrame([asdict(t) for t in trades])
    trades_df.to_csv(out / "backtest_trades.csv", index=False)

    eq_df = pd.DataFrame(equity_rows)
    eq_df.to_csv(out / "backtest_equity.csv", index=False)

    with open(out / "backtest_stats.json", "w") as f:
        json.dump(asdict(stats), f, indent=2)

    log.info(f"\nSaved to {out}/backtest_trades.csv, backtest_equity.csv, backtest_stats.json")


if __name__ == "__main__":
    main()

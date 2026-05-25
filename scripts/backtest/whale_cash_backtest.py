#!/usr/bin/env python3
"""
Whale-following cash-management backtest.

Position sizing:
  - Starting capital: $1,000 (configurable)
  - Each new position: 10% of remaining CASH (not equity)
  - Cash only restored when a position closes (no mark-to-market)
  - Example: $1000 start → $100, $90, $81 ... if nothing closes

Signal:
  - First qualifying whale trade per resolved market = entry
  - Exit price: 1.0 (win) or 0.0 (loss) at resolution

Whale selection:
  - Full 406-whale set built with the custom scoring formula
  - In-sample: whales identified on the same resolved-market history used for simulation
    (this shows ceiling performance; label as in-sample)

Usage:
    python scripts/backtest/whale_cash_backtest.py
    python scripts/backtest/whale_cash_backtest.py --capital 1000 --pct 0.10
"""

import argparse
import sys
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))

from src.whale_strategy.research_data_loader import load_historical_trades
from src.whale_strategy.whale_surprise import build_surprise_positive_whale_set
import pyarrow.parquet as pq


# ── Data loading ──────────────────────────────────────────────────────────────

def load_inputs(hist_dir: Path):
    print("Loading trades...")
    trades = load_historical_trades(hist_dir, min_usd=5.0)
    print(f"  {len(trades):,} trades  {trades['maker'].nunique():,} wallets")

    print("Loading resolutions...")
    res_csv = hist_dir / "resolutions.csv"
    rdf = pd.read_csv(res_csv)
    resolution_winners = dict(zip(rdf["market_id"].astype(str), rdf["winner"].astype(str)))
    print(f"  {len(resolution_winners):,} resolved markets")

    print("Loading market metadata...")
    mdf = pd.read_parquet(
        hist_dir / "markets.parquet",
        columns=["conditionId", "volumeNum", "volume", "closedTime", "question"],
    )
    vol_col = "volumeNum" if "volumeNum" in mdf.columns else "volume"
    market_volumes = dict(
        zip(mdf["conditionId"].astype(str),
            mdf[vol_col].fillna(0).astype(float))
    )
    # Parse closedTime for exit timestamps
    mdf["_closed_ts"] = pd.to_datetime(mdf["closedTime"], errors="coerce", utc=True)
    close_times = dict(
        zip(mdf["conditionId"].astype(str), mdf["_closed_ts"])
    )
    market_titles = dict(
        zip(mdf["conditionId"].astype(str),
            mdf["question"].fillna("").astype(str))
    )
    return trades, resolution_winners, market_volumes, close_times, market_titles


def build_whales(trades: pd.DataFrame, resolution_winners: dict,
                 market_volumes: dict):
    print("Building whale set...")
    whale_set, scores, winrates = build_surprise_positive_whale_set(
        trades,
        resolution_winners,
        min_trades=5,
        require_positive_surprise=True,
        volume_percentile=95.0,
        cutoff=trades["datetime"].max(),
        market_volumes=market_volumes,
        lambda_decay=0.01,
        min_score=0.0,
    )
    print(f"  {len(whale_set):,} qualified whales")
    return whale_set, scores, winrates


# ── Signal extraction ─────────────────────────────────────────────────────────

def extract_signals(trades: pd.DataFrame, whale_set: set,
                    resolution_winners: dict, whale_scores: dict,
                    whale_winrates: dict) -> pd.DataFrame:
    """
    One signal per resolved market: the first trade by a qualified whale.
    Signals are sorted chronologically (entry time).
    """
    wt = trades[
        trades["maker"].isin(whale_set) &
        trades["market_id"].isin(resolution_winners.keys())
    ].copy()

    wt["_mid"] = wt["market_id"].astype(str)
    wt["_winner"] = wt["_mid"].map(resolution_winners)
    wt = wt.sort_values("datetime")

    # Keep only first whale trade per market
    first = wt.drop_duplicates(subset=["_mid"], keep="first").copy()

    first["score"]   = first["maker"].map(whale_scores).fillna(0)
    first["winrate"] = first["maker"].map(whale_winrates).fillna(0.5)

    # Outcome
    dir_lower = first["maker_direction"].str.lower()
    winner    = first["_winner"].str.upper()
    first["won"] = (
        ((dir_lower == "buy")  & (winner == "YES")) |
        ((dir_lower == "sell") & (winner == "NO"))
    )

    print(f"Signals: {len(first):,} resolved-market whale entries  "
          f"(win rate {first['won'].mean():.1%})")
    return first.reset_index(drop=True)


# ── Simulation ────────────────────────────────────────────────────────────────

def simulate(signals: pd.DataFrame, close_times: dict, market_titles: dict,
             starting_capital: float, position_pct: float,
             rolling_window: int = 100, min_samples: int = 30,
             z_threshold: float = 2.0) -> dict:
    """
    Chronological cash-management simulation with causal rolling quality filter.

    Rolling 2σ filter (no look-ahead):
      - Maintain a deque of the last `rolling_window` whale scores seen in the
        signal stream.  For each new signal: compute threshold = mean + z*std
        of the CURRENT deque (before adding the new score), then decide.
      - First `min_samples` signals are used purely for warm-up; no trades placed
        until the deque has at least `min_samples` observations.

    Cash rule: only restored on position close (cost-basis locking).
    """
    cash = starting_capital
    open_positions: dict = {}   # market_id -> record
    closed: list = []
    equity_curve: list = []     # (datetime, equity)

    # Rolling score history — causal (deque updated AFTER the trade decision)
    score_history: deque = deque(maxlen=rolling_window)
    warmup_skipped = 0
    filter_skipped = 0

    # Build event list: entries + exits
    # Entry  → (entry_datetime, "ENTRY", row)
    # Exit   → (close_datetime, "EXIT",  market_id)
    def _to_utc(ts):
        """Coerce any timestamp to UTC-aware."""
        t = pd.Timestamp(ts)
        if t.tzinfo is None:
            return t.tz_localize("UTC")
        return t.tz_convert("UTC")

    events = []
    for _, row in signals.iterrows():
        events.append((_to_utc(row["datetime"]), "ENTRY", row))

    # Map entry timestamps per market for fallback exit timing
    entry_ts_map = {row["_mid"]: _to_utc(row["datetime"])
                    for _, row in signals.iterrows()}

    for mid, row in signals.set_index("_mid").iterrows():
        ct = close_times.get(mid)
        if ct is not None and pd.notna(ct):
            exit_ts = _to_utc(ct)
        else:
            # Fallback: 30 days after entry (reasonable resolution window)
            exit_ts = entry_ts_map.get(mid, pd.Timestamp("2024-01-01", tz="UTC")) + pd.Timedelta(days=30)
        events.append((exit_ts, "EXIT", mid))

    events.sort(key=lambda x: x[0])

    for ts, kind, payload in events:
        if kind == "ENTRY":
            row = payload
            mid = row["_mid"]
            whale_score = float(row.get("score", 0))

            # ── Causal rolling 2σ filter ──────────────────────────────────
            # Step 1: compute threshold from history BEFORE adding this score
            n_seen = len(score_history)
            if n_seen < min_samples:
                # Warm-up: accumulate scores, don't trade yet
                score_history.append(whale_score)
                warmup_skipped += 1
                continue

            hist_arr = np.array(score_history)
            mu    = hist_arr.mean()
            sigma = hist_arr.std(ddof=1) if n_seen > 1 else 0.0
            threshold = mu + z_threshold * sigma

            # Step 2: always add score to history (causal)
            score_history.append(whale_score)

            # Step 3: filter — must have positive score AND be above rolling threshold
            if whale_score <= 0 or whale_score <= threshold:
                filter_skipped += 1
                continue
            # ─────────────────────────────────────────────────────────────

            # Skip if already have a position in this market
            if mid in open_positions:
                continue
            # Skip if already closed (close event processed before this entry)
            if any(p["market_id"] == mid for p in closed):
                continue

            size = cash * position_pct
            if size < 0.01:
                continue

            cash -= size
            open_positions[mid] = {
                "market_id":   mid,
                "entry_price": float(row["price"]),
                "direction":   str(row["maker_direction"]).upper(),
                "size":        size,
                "won":         bool(row["won"]),
                "entry_ts":    ts,
                "whale":       str(row["maker"]),
                "score":       float(row["score"]),
                "title":       market_titles.get(mid, "")[:60],
            }
            total_deployed = sum(p["size"] for p in open_positions.values())
            equity_curve.append((ts, cash + total_deployed))

        elif kind == "EXIT":
            mid = payload
            if mid not in open_positions:
                continue
            pos = open_positions.pop(mid)

            ep  = pos["entry_price"]
            sz  = pos["size"]
            direction = pos["direction"]
            won = pos["won"]

            # Payout calculation
            if direction == "BUY":
                payout = sz / ep if won else 0.0
            else:  # SELL YES = BUY NO
                no_price = 1.0 - ep
                payout = sz / max(no_price, 1e-6) if won else 0.0

            net_pnl = payout - sz
            roi     = net_pnl / sz

            cash += payout
            pos.update({"exit_ts": ts, "payout": payout,
                        "net_pnl": net_pnl, "roi": roi})
            closed.append(pos)

            total_deployed = sum(p["size"] for p in open_positions.values())
            equity_curve.append((ts, cash + total_deployed))

    # Close remaining open positions as unrealised (treat as still open, mark at cost)
    n_open = len(open_positions)
    unrealised_cost = sum(p["size"] for p in open_positions.values())
    final_equity = cash + unrealised_cost  # cost basis, not mark-to-market

    return {
        "closed":          closed,
        "equity_curve":    equity_curve,
        "final_cash":      cash,
        "final_equity":    final_equity,
        "n_open":          n_open,
        "unrealised_cost": unrealised_cost,
        "starting_capital": starting_capital,
        "warmup_skipped":  warmup_skipped,
        "filter_skipped":  filter_skipped,
    }


# ── Metrics ───────────────────────────────────────────────────────────────────

def compute_metrics(result: dict) -> dict:
    closed  = result["closed"]
    eq      = result["equity_curve"]
    start   = result["starting_capital"]
    final   = result["final_equity"]

    if not closed:
        return {}

    pnls    = np.array([p["net_pnl"] for p in closed])
    rois    = np.array([p["roi"]     for p in closed])
    won     = np.array([p["won"]     for p in closed], dtype=bool)
    sizes   = np.array([p["size"]    for p in closed])

    total_return = (final - start) / start
    win_rate     = won.mean()
    avg_roi      = rois.mean()
    avg_win_roi  = rois[won].mean()   if won.any()  else 0.0
    avg_loss_roi = rois[~won].mean()  if (~won).any() else 0.0
    profit_factor = (
        pnls[pnls > 0].sum() / abs(pnls[pnls < 0].sum())
        if (pnls < 0).any() else float("inf")
    )

    # Equity-curve metrics
    if len(eq) >= 2:
        eq_df = pd.DataFrame(eq, columns=["ts", "equity"]).sort_values("ts")
        # Drop any extreme fallback dates (>2030)
        eq_df = eq_df[eq_df["ts"] < pd.Timestamp("2030-01-01", tz="UTC")]
        if eq_df.empty:
            max_dd, cagr, sharpe = 0.0, 0.0, 0.0
        else:
            eq_vals = eq_df["equity"].to_numpy()
            # Max drawdown
            peak   = np.maximum.accumulate(eq_vals)
            dd     = (peak - eq_vals) / np.maximum(peak, 1e-6)
            max_dd = dd.max()
            # CAGR
            t0    = eq_df["ts"].iloc[0]
            t1    = eq_df["ts"].iloc[-1]
            years = max((t1 - t0).days / 365.25, 1/365)
            cagr  = (final / start) ** (1 / years) - 1
            # Sharpe — resample to daily, log returns
            daily = eq_df.set_index("ts")["equity"].resample("D").last().ffill()
            log_r = np.log(daily / daily.shift(1)).dropna()
            sharpe = (log_r.mean() / (log_r.std() + 1e-9)) * np.sqrt(252)
    else:
        max_dd = 0.0
        cagr   = 0.0
        sharpe = 0.0

    # Best / worst trades
    best_idx  = np.argmax(pnls)
    worst_idx = np.argmin(pnls)

    return {
        "warmup_skipped": result.get("warmup_skipped", 0),
        "filter_skipped": result.get("filter_skipped", 0),
        "n_trades":      len(closed),
        "n_wins":        int(won.sum()),
        "n_losses":      int((~won).sum()),
        "win_rate":      win_rate,
        "total_return":  total_return,
        "cagr":          cagr,
        "sharpe":        sharpe,
        "max_drawdown":  max_dd,
        "avg_position":  sizes.mean(),
        "avg_roi":       avg_roi,
        "avg_win_roi":   avg_win_roi,
        "avg_loss_roi":  avg_loss_roi,
        "profit_factor": profit_factor,
        "total_pnl":     pnls.sum(),
        "final_equity":  final,
        "n_open":        result["n_open"],
        "unrealised_cost": result["unrealised_cost"],
        "best_trade":    closed[best_idx],
        "worst_trade":   closed[worst_idx],
    }


def print_report(m: dict, capital: float, signals: pd.DataFrame) -> None:
    sep = "=" * 65
    closed = m.get("_all_closed", [])
    if closed:
        dates = [p["entry_ts"] for p in closed if "entry_ts" in p]
        if dates:
            d0 = min(dates)
            d1 = max(p.get("exit_ts", p["entry_ts"]) for p in closed if "entry_ts" in p)
            date_range = f"  Period: {str(d0)[:10]}  to  {str(d1)[:10]}"
        else:
            date_range = ""
    else:
        date_range = ""

    print(f"\n{sep}")
    print("  WHALE BACKTEST RESULTS  (in-sample, cash-only sizing)")
    if date_range:
        print(date_range)
    print(sep)
    print(f"  Starting capital      :  ${capital:,.2f}")
    print(f"  Final equity          :  ${m['final_equity']:,.2f}  "
          f"({m['total_return']:+.1%})")
    print(f"  CAGR                  :  {m['cagr']:+.1%}")
    print(f"  Sharpe ratio          :  {m['sharpe']:.2f}")
    print(f"  Max drawdown          :  {m['max_drawdown']:.1%}")
    print(f"  Total P&L             :  ${m['total_pnl']:+,.2f}")
    print()
    print(f"  Trades closed         :  {m['n_trades']}")
    print(f"  Win / Loss            :  {m['n_wins']} / {m['n_losses']}")
    print(f"  Win rate              :  {m['win_rate']:.1%}")
    print(f"  Avg position size     :  ${m['avg_position']:.2f}")
    print(f"  Avg trade ROI         :  {m['avg_roi']:+.1%}")
    print(f"  Avg win  ROI          :  {m['avg_win_roi']:+.1%}")
    print(f"  Avg loss ROI          :  {m['avg_loss_roi']:+.1%}")
    print(f"  Profit factor         :  {m['profit_factor']:.2f}x")
    print()
    print(f"  Still open (at cost)  :  {m['n_open']} positions  "
          f"${m['unrealised_cost']:.2f}")
    print(f"  Warmup skipped        :  {m.get('warmup_skipped', 0)}")
    print(f"  Filter rejected       :  {m.get('filter_skipped', 0)}  (score <= mean+2*std)")
    print()
    print("  Best trade:")
    b = m["best_trade"]
    print(f"    {b['title'][:55]}")
    print(f"    {b['direction']} @ {b['entry_price']:.3f}  "
          f"size=${b['size']:.2f}  P&L=${b['net_pnl']:+.2f}  ROI={b['roi']:+.1%}")
    print()
    print("  Worst trade:")
    w = m["worst_trade"]
    print(f"    {w['title'][:55]}")
    print(f"    {w['direction']} @ {w['entry_price']:.3f}  "
          f"size=${w['size']:.2f}  P&L=${w['net_pnl']:+.2f}  ROI={w['roi']:+.1%}")
    print(sep)

    # Top 10 trades by P&L
    top = sorted(m.get("_all_closed", []), key=lambda x: x["net_pnl"], reverse=True)[:10]
    if top:
        print("\n  Top 10 trades by P&L:")
        print(f"  {'Title':50s}  {'Dir':4s}  {'Entry':5s}  {'Size':7s}  {'P&L':9s}  {'ROI':6s}")
        for t in top:
            print(f"  {t['title'][:50]:50s}  {t['direction']:4s}  "
                  f"{t['entry_price']:5.3f}  ${t['size']:6.2f}  "
                  f"${t['net_pnl']:+8.2f}  {t['roi']:+.1%}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Whale cash-management backtest")
    p.add_argument("--capital",  type=float, default=1000.0, help="Starting capital ($)")
    p.add_argument("--pct",      type=float, default=0.10,   help="Position size fraction")
    p.add_argument("--min-score",      type=float, default=0.0,   help="Min whale score filter")
    p.add_argument("--min-trades",     type=int,   default=5,     help="Min trades to qualify whale")
    p.add_argument("--rolling-window", type=int,   default=100,   help="Rolling score history window size")
    p.add_argument("--min-samples",    type=int,   default=30,    help="Warmup observations before filtering")
    p.add_argument("--z-threshold",    type=float, default=1.0,   help="Std deviations above mean to qualify")
    args = p.parse_args()

    hist = _root / "data" / "historical"

    trades, resolution_winners, market_volumes, close_times, market_titles = load_inputs(hist)
    whale_set, scores, winrates = build_whales(trades, resolution_winners, market_volumes)

    # Optional score filter
    if args.min_score > 0:
        whale_set = {w for w in whale_set if scores.get(w, 0) >= args.min_score}
        scores    = {w: s for w, s in scores.items() if w in whale_set}
        winrates  = {w: r for w, r in winrates.items() if w in whale_set}
        print(f"  After min_score>={args.min_score}: {len(whale_set):,} whales")

    signals = extract_signals(trades, whale_set, resolution_winners, scores, winrates)
    if signals.empty:
        print("No signals — nothing to backtest.")
        return

    print(f"\nSimulating {len(signals):,} signals  "
          f"(capital=${args.capital:,.0f}, position={args.pct:.0%}, "
          f"2-sigma filter: window={args.rolling_window} warmup={args.min_samples})...")
    result = simulate(signals, close_times, market_titles, args.capital, args.pct,
                      rolling_window=args.rolling_window,
                      min_samples=args.min_samples,
                      z_threshold=args.z_threshold)

    m = compute_metrics(result)
    m["_all_closed"] = result["closed"]
    print_report(m, args.capital, signals)


if __name__ == "__main__":
    main()

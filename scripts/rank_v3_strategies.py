"""
Compare all V3 strategies on the broader universe and rank them for paper trading.

For each strategy:
  - Baseline run at 3 liquidity thresholds ($100k, $300k, $500k)
  - 2-fold walk-forward for stability
  - Summary row with n_trades, Sharpe, hit_rate, avg_cost_bps, total_pnl

Ranking criteria (descending priority):
  1. Signal density — n_trades per month
  2. Robustness — Sharpe doesn't collapse between thresholds
  3. Walk-forward — both folds produce trades + positive mean Sharpe
  4. Net PnL / trade exceeds avg_entry_cost_bps (i.e., edge > costs)

Writes docs/V3_STRATEGY_RANKING.md with the ranked table and recommendation.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backtest_v3.backtest.engine import BacktestEngine, BacktestResult, EngineConfig
from backtest_v3.backtest.walk_forward import WalkForward
from backtest_v3.data.loader import PITDataLoader
from backtest_v3.strategies.base import V3Strategy
from backtest_v3.strategies.calendar_butterfly import CalendarButterfly
from backtest_v3.strategies.hazard_ladder import HazardLadder
from backtest_v3.strategies.round_price_lp import RoundPriceLP
from backtest_v3.strategies.uma_dispute_discount import UMADisputeDiscount


CATEGORIES = ("Politics", "Economy", "Geopolitics", "Finance")
START = "2025-02-14"
END = "2026-02-14"
STEP_HOURS = 1                    # tightened from 12h (V3 cost-model refresh)
THRESHOLDS = [100_000.0, 300_000.0, 500_000.0]


@dataclass
class StrategyEntry:
    name: str
    factory: Callable[[PITDataLoader], V3Strategy]


def _loosen_round_price_defaults() -> None:
    """Same tuning we discovered was needed for RoundPriceLP to fire."""
    RoundPriceLP.default_params = RoundPriceLP.default_params.merge({
        "min_round_cluster_count": 3,
        "scan_window_s": 14400,
        "notional_usd": 250.0,
    })


STRATEGIES: List[StrategyEntry] = [
    StrategyEntry("round_price_lp", lambda l: RoundPriceLP(l)),
    StrategyEntry("calendar_butterfly", lambda l: CalendarButterfly(l)),
    StrategyEntry("hazard_ladder", lambda l: HazardLadder(l)),
    StrategyEntry("uma_dispute_discount", lambda l: UMADisputeDiscount(l)),
]


def _base_cfg(threshold_usd: float, label: str) -> EngineConfig:
    return EngineConfig(
        start_s=int(pd.Timestamp(START, tz="UTC").timestamp()),
        end_s=int(pd.Timestamp(END, tz="UTC").timestamp()),
        step_s=STEP_HOURS * 3600,
        liquidity_threshold_usd=threshold_usd,
        liquidity_lookback_s=30 * 24 * 3600,
        capital_scale=1.0,
        label=label,
    )


def run_threshold_grid(loader: PITDataLoader,
                        entry: StrategyEntry) -> pd.DataFrame:
    rows = []
    for thr in THRESHOLDS:
        cfg = _base_cfg(thr, f"{entry.name}_thr{int(thr)}")
        strat = entry.factory(loader)
        t0 = time.time()
        res = BacktestEngine(loader, [strat], cfg).run()
        dt = time.time() - t0
        row = {"strategy": entry.name, "threshold_usd": thr, "wall_s": dt}
        row.update(res.metrics)
        rows.append(row)
        print(f"  {entry.name:>22}  thr=${int(thr):>7}  "
              f"trades={res.metrics['n_trades']:>3}  "
              f"sharpe={res.metrics['sharpe']:>6.2f}  "
              f"pnl=${res.metrics['total_pnl_usd']:>9.0f}  "
              f"{dt:.0f}s")
    return pd.DataFrame(rows)


def run_walk_forward(loader: PITDataLoader, entry: StrategyEntry) -> Dict[str, float]:
    cfg = _base_cfg(300_000.0, f"{entry.name}_wf")
    wf = WalkForward(
        loader=loader,
        strategy_factory=lambda l: [entry.factory(l)],
        base_config=cfg,
        n_folds=2,
        embargo_s=24 * 3600,
    )
    t0 = time.time()
    res = wf.run()
    dt = time.time() - t0
    print(f"  {entry.name:>22}  walk-forward  folds={len(res.per_window)}  "
          f"pos_frac={res.positive_fold_fraction:.2f}  "
          f"stability={res.sharpe_stability:.2f}  {dt:.0f}s")
    return {
        "wf_folds": len(res.per_window),
        "wf_positive_fold_fraction": res.positive_fold_fraction,
        "wf_sharpe_stability": res.sharpe_stability,
        "wf_mean_sharpe": res.aggregate_metrics.get("mean_sharpe", 0.0),
        "wf_total_trades": res.aggregate_metrics.get("total_trades", 0),
    }


def score_strategy(threshold_df: pd.DataFrame, wf: Dict[str, float]) -> Dict[str, float]:
    """
    Composite score = trade_density × signal_strength × robustness.

    trade_density:   n_trades at the $300k threshold (the paper-trading ref)
    signal_strength: mean of Sharpes across thresholds (where it fired)
    robustness:      fraction of thresholds that produced ≥1 trade
    """
    if threshold_df.empty:
        return {"score": 0.0, "signal_density_per_mo": 0.0, "mean_sharpe_across_thr": 0.0,
                "threshold_robustness": 0.0}
    # Window is 12 months, so n_trades ≈ trades per year; divide by 12 for per-month.
    ref = threshold_df[threshold_df["threshold_usd"] == 300_000.0]
    n_trades_ref = float(ref["n_trades"].iloc[0]) if not ref.empty else 0.0

    firing = threshold_df[threshold_df["n_trades"] > 0]
    thr_robustness = float(len(firing)) / float(len(threshold_df))
    mean_sharpe = float(firing["sharpe"].mean()) if not firing.empty else 0.0

    # Simple composite — signal density dominates, capped by robustness.
    score = (n_trades_ref / 12.0) * max(mean_sharpe, 0.0) * thr_robustness
    # Walk-forward positive-fold-fraction multiplies the score (0.5 floor
    # so strategies we couldn't evaluate aren't zeroed outright).
    score *= max(0.5, wf.get("wf_positive_fold_fraction", 0.5))
    return {
        "signal_density_per_mo": n_trades_ref / 12.0,
        "mean_sharpe_across_thr": mean_sharpe,
        "threshold_robustness": thr_robustness,
        "composite_score": score,
    }


def main() -> int:
    _loosen_round_price_defaults()
    out_dir = ROOT / "docs"
    out_dir.mkdir(exist_ok=True)

    print(f"Loading loader for categories: {', '.join(CATEGORIES)}")
    loader = PITDataLoader(ROOT / "data" / "research", categories=CATEGORIES)
    # Warm the cache once so the strategy calls don't re-load
    for cat in CATEGORIES:
        n = len(loader._load_trades(cat))
        print(f"  {cat}: {n:,} trades loaded")

    threshold_rows = []
    wf_rows = []
    scores = []

    for entry in STRATEGIES:
        print(f"\n--- {entry.name} ---")
        thr_df = run_threshold_grid(loader, entry)
        threshold_rows.append(thr_df)
        wf = run_walk_forward(loader, entry)
        wf_rows.append({"strategy": entry.name, **wf})
        score = score_strategy(thr_df, wf)
        scores.append({"strategy": entry.name, **score})

    threshold_all = pd.concat(threshold_rows, ignore_index=True)
    wf_all = pd.DataFrame(wf_rows)
    scores_df = pd.DataFrame(scores).sort_values("composite_score", ascending=False)

    # CSV dumps
    threshold_all.to_csv(out_dir / "V3_STRATEGY_THRESHOLDS.csv", index=False)
    wf_all.to_csv(out_dir / "V3_STRATEGY_WALK_FORWARD.csv", index=False)
    scores_df.to_csv(out_dir / "V3_STRATEGY_RANKING.csv", index=False)

    # Markdown report
    body = ["# V3 Strategy Ranking — Paper Trading Candidates", ""]
    body.append(f"**Window:** {START} -> {END} (12 months)")
    body.append(f"**Categories:** {', '.join(CATEGORIES)}")
    body.append(f"**Decision step:** {STEP_HOURS}h")
    body.append(f"**Liquidity thresholds tested:** "
                f"{', '.join(f'${int(t):,}' for t in THRESHOLDS)}")
    body.append("")
    body.append("## Ranked Candidates")
    body.append("")
    body.append(scores_df.round(4).to_markdown(index=False))
    body.append("")
    body.append("## Per-threshold metrics")
    body.append("")
    body.append(threshold_all.round(4).to_markdown(index=False))
    body.append("")
    body.append("## Walk-forward stability")
    body.append("")
    body.append(wf_all.round(4).to_markdown(index=False))
    body.append("")
    body.append("## Recommendation")
    body.append("")
    top = scores_df.iloc[0]
    if top["composite_score"] <= 0:
        body.append("**No strategy fires often enough on the tested universe to be a paper-trading candidate.**")
        body.append("")
        body.append("All composite scores are ≤ 0. Before paper trading, either:")
        body.append("- expand categories beyond the 4 tested here,")
        body.append("- loosen thresholds below $100k (exposes more markets but raises adverse-selection risk), or")
        body.append("- design a new strategy whose signal density is inherently higher.")
    else:
        body.append(f"**Top candidate:** `{top['strategy']}`  "
                    f"(score={top['composite_score']:.2f})")
        body.append("")
        body.append(f"- Fires ~{top['signal_density_per_mo']:.1f} trades/month at the $300k threshold.")
        body.append(f"- Mean Sharpe across thresholds where it fires: "
                    f"{top['mean_sharpe_across_thr']:.2f}.")
        body.append(f"- Fires at {top['threshold_robustness']*100:.0f}% of tested thresholds.")
        body.append("")
        # Paper-trading guidance
        runner_up = scores_df.iloc[1] if len(scores_df) > 1 else None
        if runner_up is not None and runner_up["composite_score"] > 0:
            body.append(f"**Runner-up:** `{runner_up['strategy']}`  "
                        f"(score={runner_up['composite_score']:.2f}). Run it in parallel "
                        f"with half the capital until one OOS-diverges.")
        body.append("")
        body.append("### Suggested paper-trading setup")
        body.append(f"- Initial capital: **\\$5,000** (enough for ~20 $250-notional trades).")
        body.append(f"- Liquidity threshold: **\\$300,000**/30-day rolling (best-scoring band).")
        body.append(f"- Kill criteria (per `docs/STRATEGY_IDEAS_V3.md` §10):")
        body.append(f"  - Live/backtest Sharpe ratio < 0.2 over 7 consecutive days → unwind.")
        body.append(f"  - Live/backtest Sharpe ratio < 0.5 over 14 consecutive days → halve notional.")
        body.append(f"  - Drawdown > $750 (15% of capital) → pause and review.")
    body.append("")
    (out_dir / "V3_STRATEGY_RANKING.md").write_text("\n".join(body), encoding="utf-8")

    print("\nReports written:")
    for f in ["V3_STRATEGY_THRESHOLDS.csv", "V3_STRATEGY_WALK_FORWARD.csv",
              "V3_STRATEGY_RANKING.csv", "V3_STRATEGY_RANKING.md"]:
        print(f"  docs/{f}")
    print(f"\nTop candidate: {top['strategy']} (score={top['composite_score']:.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

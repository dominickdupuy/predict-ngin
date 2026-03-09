"""
Parameter sensitivity sweep on holdout period only.

Tests key whale strategy parameters one-at-a-time on the holdout period
(Sep 2025 onwards) to detect if performance is fragile to parameter choices.

Usage:
    PYTHONPATH=.:src venv/bin/python3 scripts/robustness/param_sensitivity_holdout.py
"""

import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, ".")
sys.path.insert(0, "src")

from scripts.backtest.run_whale_category_backtest import run_whale_category_backtest
from src.whale_strategy.whale_config import WhaleConfig


HOLDOUT_START = "2025-09-01"
RESEARCH_DIR = Path("data/research")
RESOLUTIONS_DIR = Path("data/poly_cat")
CAPITAL = 1_000_000
WORKERS = 35


def run_one(cfg: WhaleConfig, param: str, value: Any) -> Dict:
    result = run_whale_category_backtest(
        research_dir=RESEARCH_DIR,
        capital=CAPITAL,
        min_usd=100,
        start_date=HOLDOUT_START,
        end_date=None,
        whale_config=cfg,
        extra_resolutions_dir=RESOLUTIONS_DIR,
        n_workers=WORKERS,
        rebalance_freq="1W",
    )

    if "error" in result:
        return {"param": param, "value": value, "trades": 0,
                "win_rate": float("nan"), "net_pnl": float("nan"),
                "sharpe": float("nan"), "error": result["error"]}

    return {
        "param": param,
        "value": value,
        "trades": result.get("total_trades", 0),
        "win_rate": round(result.get("win_rate", 0), 4),
        "net_pnl": round(result.get("total_net_pnl", 0), 0),
        "sharpe": round(result.get("sharpe_ratio", 0), 2),
    }


def main():
    base_cfg = WhaleConfig()

    # (label, attr_name, [values_to_sweep])
    grid: List[Tuple[str, str, List]] = [
        ("volume_percentile",    "volume_percentile",           [85, 90, 92, 95, 97, 99]),
        ("bayes_prior_alpha",    "bayes_prior_alpha",           [0.5, 1.0, 2.0, 4.0, 8.0]),
        ("recency_halflife",     "recency_halflife_days",       [0, 30, 60, 90, 180, 365]),
        ("ic_score_weight",      "ic_score_weight",             [0.0, 0.10, 0.20, 0.30, 0.40]),
        ("max_entry_yes_price",  "max_entry_yes_price",         [0.90, 0.93, 0.95, 0.97, 0.98, 0.99]),
        ("partial_exit_thresh",  "partial_exit_gain_threshold", [0.20, 0.30, 0.40, 0.60, 0.80]),
    ]

    all_results = []

    for param_label, attr, values in grid:
        print(f"\n{'='*60}")
        print(f"Sweeping: {param_label}  (baseline={getattr(base_cfg, attr)})")
        print(f"{'='*60}")

        for val in values:
            marker = " <-- baseline" if val == getattr(base_cfg, attr) else ""
            cfg = WhaleConfig(**{attr: val})
            result = run_one(cfg, param_label, val)
            all_results.append(result)

            err = f"  ERROR: {result.get('error')}" if "error" in result else ""
            print(
                f"  {str(val):<8}{marker:<14}  "
                f"trades={result['trades']:3d}  "
                f"WR={result['win_rate']:.1%}  "
                f"PnL=${result['net_pnl']:>14,.0f}  "
                f"Sharpe={result['sharpe']:.2f}"
                f"{err}"
            )

    # Final summary
    print(f"\n\n{'='*60}")
    print("SENSITIVITY SUMMARY  (holdout: Sep 2025 → Mar 2026)")
    print(f"{'='*60}")
    df_all = pd.DataFrame(all_results)
    for param, grp in df_all.groupby("param", sort=False):
        pnl_vals = grp["net_pnl"].dropna()
        if len(pnl_vals) > 1:
            pnl_range = pnl_vals.max() - pnl_vals.min()
            mean_pnl = pnl_vals.mean()
            cv = pnl_vals.std() / (abs(mean_pnl) + 1) * 100
            print(f"  {param:<25}  PnL range=${pnl_range:>12,.0f}   CV={cv:.1f}%")

    df_all.to_csv("scripts/robustness/sensitivity_holdout_results.csv", index=False)
    print("\nResults saved to scripts/robustness/sensitivity_holdout_results.csv")


if __name__ == "__main__":
    main()

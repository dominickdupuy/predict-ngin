"""
Walk-forward OOS validation.

Splits the backtest horizon into N contiguous non-overlapping test windows
with optional embargo (no-look gap) between adjacent windows. Runs the
same strategy config on each window and reports per-window metrics so we
can judge consistency, not just aggregate Sharpe.

This is the core defense against "looked good on training" — any strategy
that passes only on some folds is overfit.

Reference: López de Prado, "Advances in Financial Machine Learning" (2018),
ch. 7 (walk-forward) + ch. 12 (Deflated Sharpe).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ..data.loader import PITDataLoader
from ..strategies.base import V3Strategy
from .engine import BacktestEngine, BacktestResult, EngineConfig


@dataclass
class WalkForwardResult:
    per_window: List[BacktestResult]
    per_window_metrics: pd.DataFrame
    aggregate_metrics: Dict[str, float]
    positive_fold_fraction: float
    sharpe_stability: float           # 1 - cv of fold sharpes

    def summary(self) -> str:
        lines = ["=== Walk-forward result ==="]
        lines.append(self.per_window_metrics.round(4).to_string())
        lines.append("\nAggregate:")
        for k, v in self.aggregate_metrics.items():
            lines.append(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")
        lines.append(f"\npositive_fold_fraction: {self.positive_fold_fraction:.2f}")
        lines.append(f"sharpe_stability: {self.sharpe_stability:.3f}")
        return "\n".join(lines)


class WalkForward:
    def __init__(
        self,
        loader: PITDataLoader,
        strategy_factory: Callable[[PITDataLoader], List[V3Strategy]],
        base_config: EngineConfig,
        n_folds: int = 4,
        embargo_s: int = 0,
    ):
        self.loader = loader
        self.strategy_factory = strategy_factory
        self.base_config = base_config
        self.n_folds = int(n_folds)
        self.embargo_s = int(embargo_s)

    def run(self) -> WalkForwardResult:
        cfg = self.base_config
        span = cfg.end_s - cfg.start_s
        if span <= 0 or self.n_folds <= 0:
            raise ValueError(f"bad span/n_folds: span={span}, n_folds={self.n_folds}")
        fold_len = span // self.n_folds

        per_window: List[BacktestResult] = []
        rows = []
        for i in range(self.n_folds):
            w_start = cfg.start_s + i * fold_len
            w_end = cfg.start_s + (i + 1) * fold_len if i < self.n_folds - 1 else cfg.end_s
            if self.embargo_s and i > 0:
                w_start += self.embargo_s
            if w_end <= w_start:
                continue
            sub_cfg = EngineConfig(
                start_s=w_start, end_s=w_end,
                step_s=cfg.step_s,
                liquidity_threshold_usd=cfg.liquidity_threshold_usd,
                liquidity_lookback_s=cfg.liquidity_lookback_s,
                capital_scale=cfg.capital_scale,
                executor_level_fill_fraction=cfg.executor_level_fill_fraction,
                executor_max_depth_fraction=cfg.executor_max_depth_fraction,
                taker_fee_bps=cfg.taker_fee_bps,
                reconstructor_kwargs=dict(cfg.reconstructor_kwargs),
                label=f"{cfg.label}_fold{i}",
                max_open_per_market=cfg.max_open_per_market,
            )
            engine = BacktestEngine(self.loader, self.strategy_factory(self.loader), sub_cfg)
            res = engine.run()
            per_window.append(res)
            row = {"fold": i, "start_s": w_start, "end_s": w_end}
            row.update(res.metrics)
            rows.append(row)

        if not rows:
            return WalkForwardResult(
                per_window=[],
                per_window_metrics=pd.DataFrame(),
                aggregate_metrics={},
                positive_fold_fraction=0.0,
                sharpe_stability=0.0,
            )

        pwm = pd.DataFrame(rows)
        sharpes = pwm["sharpe"].to_numpy()
        pos_frac = float((sharpes > 0).mean())
        mean_sharpe = float(sharpes.mean())
        std_sharpe = float(sharpes.std(ddof=1)) if len(sharpes) > 1 else 0.0
        stability = float(max(0.0, 1.0 - (std_sharpe / (abs(mean_sharpe) + 1e-9))))

        agg = {
            "mean_sharpe": mean_sharpe,
            "std_sharpe": std_sharpe,
            "total_pnl_usd": float(pwm["total_pnl_usd"].sum()),
            "total_trades": int(pwm["n_trades"].sum()),
            "mean_hit_rate": float(pwm["hit_rate"].mean()),
            "mean_max_drawdown_usd": float(pwm["max_drawdown_usd"].mean()),
        }

        return WalkForwardResult(
            per_window=per_window,
            per_window_metrics=pwm,
            aggregate_metrics=agg,
            positive_fold_fraction=pos_frac,
            sharpe_stability=stability,
        )

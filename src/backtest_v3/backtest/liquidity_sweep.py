"""
Liquidity-threshold sweep.

Sweeps `EngineConfig.liquidity_threshold_usd` across a list of thresholds
(default: the V3 grid 100k, 300k, 500k, 750k, 1M) and reports per-threshold
metrics. Answers the practical question: "at what minimum-volume bar does this
strategy stop working, and how much does net PnL shrink as I raise the bar?"

This is separate from `ParameterSweep` because:
- The threshold is an engine-level setting, not a strategy-level one.
- The interesting question here is monotonicity — does Sharpe degrade
  smoothly as liquidity rises (expected: costs down but opportunity count
  down faster), or does a cliff show up somewhere (sign of a strategy
  that only works on thin markets, i.e. probably not a strategy at all)?
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

import pandas as pd

from ..data.loader import PITDataLoader
from ..data.universe import LiquidUniverse
from ..strategies.base import V3Strategy
from .engine import BacktestEngine, BacktestResult, EngineConfig


StrategyFactory = Callable[[PITDataLoader], List[V3Strategy]]


@dataclass
class LiquiditySweepResult:
    per_threshold: Dict[float, BacktestResult]
    table: pd.DataFrame                     # one row per threshold, columns = metrics

    def summary(self) -> str:
        if self.table.empty:
            return "=== LiquiditySweep: no thresholds produced results ==="
        cols = [c for c in ["threshold_usd", "n_markets_mean", "sharpe", "sortino",
                            "total_pnl_usd", "n_trades", "hit_rate",
                            "avg_entry_cost_bps", "max_drawdown_usd"]
                if c in self.table.columns]
        return "=== LiquiditySweep ===\n" + self.table[cols].round(4).to_string(index=False)


class LiquiditySweep:
    def __init__(
        self,
        loader: PITDataLoader,
        strategy_factory: StrategyFactory,
        base_config: EngineConfig,
        thresholds_usd: Optional[Sequence[float]] = None,
    ):
        self.loader = loader
        self.strategy_factory = strategy_factory
        self.base_config = base_config
        self.thresholds_usd = list(thresholds_usd) if thresholds_usd else list(
            LiquidUniverse.DEFAULT_THRESHOLDS
        )

    def run(self) -> LiquiditySweepResult:
        per: Dict[float, BacktestResult] = {}
        rows: List[Dict[str, Any]] = []
        for thr in self.thresholds_usd:
            cfg = self._clone_cfg(self.base_config, thr)
            strategies = self.strategy_factory(self.loader)
            engine = BacktestEngine(self.loader, strategies, cfg)
            res = engine.run()
            per[thr] = res
            n_markets_mean = self._mean_universe_size(
                self.loader, thr, cfg.liquidity_lookback_s,
                cfg.start_s, cfg.end_s, cfg.step_s,
            )
            row = {"threshold_usd": thr, "n_markets_mean": n_markets_mean}
            row.update(res.metrics)
            rows.append(row)

        table = pd.DataFrame(rows)
        return LiquiditySweepResult(per_threshold=per, table=table)

    @staticmethod
    def _clone_cfg(cfg: EngineConfig, threshold_usd: float) -> EngineConfig:
        return EngineConfig(
            start_s=cfg.start_s, end_s=cfg.end_s,
            step_s=cfg.step_s,
            liquidity_threshold_usd=float(threshold_usd),
            liquidity_lookback_s=cfg.liquidity_lookback_s,
            capital_scale=cfg.capital_scale,
            executor_level_fill_fraction=cfg.executor_level_fill_fraction,
            executor_max_depth_fraction=cfg.executor_max_depth_fraction,
            taker_fee_bps=cfg.taker_fee_bps,
            reconstructor_kwargs=dict(cfg.reconstructor_kwargs),
            label=f"{cfg.label}_thr{int(threshold_usd)}",
            max_open_per_market=cfg.max_open_per_market,
        )

    @staticmethod
    def _mean_universe_size(
        loader: PITDataLoader,
        threshold_usd: float,
        lookback_s: int,
        start_s: int,
        end_s: int,
        step_s: int,
    ) -> float:
        uni = LiquidUniverse(loader, threshold_usd=threshold_usd, lookback_s=lookback_s)
        # Sample at ~10 points across the span to avoid rescanning every decision time
        n_samples = 10
        span = max(end_s - start_s, 1)
        stride = max(span // n_samples, step_s)
        sizes: List[int] = []
        t = start_s
        while t <= end_s:
            sizes.append(len(uni.snapshot(t)))
            t += stride
        return float(sum(sizes) / len(sizes)) if sizes else 0.0

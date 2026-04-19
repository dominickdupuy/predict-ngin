"""
Capacity / Sharpe-scaling curve.

Sweeps `EngineConfig.capital_scale` across a geometric ladder (default:
0.1×, 0.25×, 0.5×, 1×, 2×, 4×, 8×) and records per-scale metrics. The curve
directly answers the V3 design question "how does Sharpe change as capital
scales" — the whole reason we built the CLOB book-walking executor instead
of using a parametric impact model.

Interpretation
--------------
On a well-behaved strategy the curve looks like:
    Sharpe flat at small scale (fills are tiny, slippage ~ half-spread),
    kinks downward once the scale forces you into deep book levels,
    crosses Sharpe=0 at the capacity wall.

The capacity wall is the scale at which expected edge = expected cost. For
Polymarket-size books on $500k+ markets, the wall is typically in the 1–4×
range of the default $500 notional per signal (see
docs/STRATEGY_IDEAS_V3.md §1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

import pandas as pd

from ..data.loader import PITDataLoader
from ..strategies.base import V3Strategy
from ..backtest.engine import BacktestEngine, BacktestResult, EngineConfig


StrategyFactory = Callable[[PITDataLoader], List[V3Strategy]]


DEFAULT_SCALES = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0)


@dataclass
class CapacityCurveResult:
    per_scale: Dict[float, BacktestResult]
    table: pd.DataFrame
    capacity_wall_scale: Optional[float]     # first scale where sharpe <= 0

    def summary(self) -> str:
        if self.table.empty:
            return "=== CapacityCurve: no scales produced results ==="
        cols = [c for c in ["capital_scale", "sharpe", "sortino", "total_pnl_usd",
                            "n_trades", "hit_rate", "avg_entry_cost_bps",
                            "avg_exit_cost_bps", "max_drawdown_usd"]
                if c in self.table.columns]
        wall = self.capacity_wall_scale
        wall_str = f"{wall:.2f}×" if wall is not None else "not reached in ladder"
        return ("=== CapacityCurve ===\n"
                f"capacity_wall: {wall_str}\n"
                + self.table[cols].round(4).to_string(index=False))


class CapacityCurve:
    def __init__(
        self,
        loader: PITDataLoader,
        strategy_factory: StrategyFactory,
        base_config: EngineConfig,
        scales: Optional[Sequence[float]] = None,
    ):
        self.loader = loader
        self.strategy_factory = strategy_factory
        self.base_config = base_config
        self.scales = list(scales) if scales else list(DEFAULT_SCALES)

    def run(self) -> CapacityCurveResult:
        per: Dict[float, BacktestResult] = {}
        rows: List[Dict[str, Any]] = []
        for s in self.scales:
            cfg = self._clone_cfg(self.base_config, s)
            strategies = self.strategy_factory(self.loader)
            engine = BacktestEngine(self.loader, strategies, cfg)
            res = engine.run()
            per[s] = res
            row = {"capital_scale": s}
            row.update(res.metrics)
            rows.append(row)

        table = pd.DataFrame(rows)
        wall = self._find_capacity_wall(table)
        return CapacityCurveResult(per_scale=per, table=table, capacity_wall_scale=wall)

    @staticmethod
    def _clone_cfg(cfg: EngineConfig, capital_scale: float) -> EngineConfig:
        return EngineConfig(
            start_s=cfg.start_s, end_s=cfg.end_s,
            step_s=cfg.step_s,
            liquidity_threshold_usd=cfg.liquidity_threshold_usd,
            liquidity_lookback_s=cfg.liquidity_lookback_s,
            capital_scale=float(capital_scale),
            executor_level_fill_fraction=cfg.executor_level_fill_fraction,
            executor_max_depth_fraction=cfg.executor_max_depth_fraction,
            taker_fee_bps=cfg.taker_fee_bps,
            reconstructor_kwargs=dict(cfg.reconstructor_kwargs),
            label=f"{cfg.label}_scale{capital_scale:g}",
            max_open_per_market=cfg.max_open_per_market,
        )

    @staticmethod
    def _find_capacity_wall(table: pd.DataFrame) -> Optional[float]:
        """First scale (ascending) where Sharpe drops to 0 or below."""
        if table.empty or "sharpe" not in table.columns:
            return None
        srt = table.sort_values("capital_scale")
        # Require a non-degenerate early point (Sharpe > 0) before looking
        # for the wall, else we'd flag a stillborn strategy as walled.
        if (srt["sharpe"] > 0).sum() == 0:
            return None
        for _, r in srt.iterrows():
            if r["sharpe"] <= 0 and r["capital_scale"] > srt["capital_scale"].min():
                return float(r["capital_scale"])
        return None

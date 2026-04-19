"""
Backtest V3 — point-in-time-safe backtest framework for V3 strategies.

Design goals (see docs/STRATEGY_IDEAS_V3.md):
- No look-ahead: every feature is tagged with available_at and the engine
  refuses to use any feature where available_at > decision_time.
- Realistic execution: a CLOB snapshot executor walks actual (or reconstructed)
  book depth instead of applying a parametric impact formula.
- Parameter-robustness first: walk-forward OOS + deflated Sharpe + liquidity
  threshold sweeps are built into the engine, not added as afterthoughts.
- Capacity-aware: every backtest emits a Sharpe-vs-capital curve, not a point.
"""

from .data.loader import PITDataLoader
from .data.universe import LiquidUniverse
from .data.clob_book import CLOBBookReconstructor
from .execution.book_executor import BookExecutor, Fill
from .backtest.engine import BacktestEngine, BacktestResult
from .backtest.walk_forward import WalkForward
from .backtest.sensitivity import ParameterSweep, deflated_sharpe
from .backtest.liquidity_sweep import LiquiditySweep
from .reporting.capacity_curve import CapacityCurve
from .strategies.base import V3Strategy, Signal, SignalSide

__all__ = [
    "PITDataLoader",
    "LiquidUniverse",
    "CLOBBookReconstructor",
    "BookExecutor",
    "Fill",
    "BacktestEngine",
    "BacktestResult",
    "WalkForward",
    "ParameterSweep",
    "deflated_sharpe",
    "LiquiditySweep",
    "CapacityCurve",
    "V3Strategy",
    "Signal",
    "SignalSide",
]

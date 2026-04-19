"""
V3 strategy base class.

Design contract:
- A strategy emits signals given an `as_of_s` and a loader handle.
- A strategy never reads raw data directly — it goes through PITDataLoader.
- A strategy declares its `params` as a frozen dict so the parameter-sweep
  engine can enumerate configurations without reflection hacks.
- A strategy is cheap to construct and pure in its dependency on (params,
  as_of, loader). No hidden state between calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..data.loader import PITDataLoader


class SignalSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Signal:
    """
    A trading intent emitted by a strategy at `as_of_s`.

    `available_at_s` is the earliest time at which *every* feature used by
    this signal would have been observable. The backtest engine checks
    available_at_s <= as_of_s and refuses to act on violations.

    `expected_hold_s` tells the engine when to check for exit. `exit_price`
    is the target mid at which the strategy wants out; the engine closes
    the position when mid crosses the target or when the hold period
    elapses, whichever first.
    """
    strategy_name: str
    condition_id: str
    as_of_s: int
    available_at_s: int
    side: SignalSide
    notional_usd: float
    exit_price: Optional[float] = None
    expected_hold_s: int = 6 * 3600
    conviction: float = 1.0            # [0, 1], scales position
    reason: str = ""
    features: Dict[str, Any] = field(default_factory=dict)

    # Execution intent. If `limit_price` is set, the engine tries to fill as a
    # maker (post the quote, fill when a contra-side trade prints through it)
    # instead of crossing the book as a taker.
    limit_price: Optional[float] = None
    maker_fill_window_s: int = 30 * 60

    # Risk controls enforced by the engine. Stop and trail are expressed in
    # bps of the entry mid so they port across price levels.
    stop_loss_bps: Optional[float] = None     # exit if adverse move exceeds
    trail_trigger_bps: Optional[float] = None # arm the trail after a +bps move
    trail_giveback_bps: Optional[float] = None # exit if favorable move gives back

    def __post_init__(self):
        if self.available_at_s > self.as_of_s:
            raise ValueError(
                f"Signal violates PIT: available_at_s={self.available_at_s} "
                f"> as_of_s={self.as_of_s} in {self.strategy_name} on {self.condition_id}"
            )
        if not (0.0 <= self.conviction <= 1.0):
            raise ValueError(f"conviction out of [0,1]: {self.conviction}")


@dataclass(frozen=True)
class StrategyParams:
    """Declarative parameter bag. Strategies declare their sweep grid here."""
    name: str
    values: Dict[str, Any]

    def merge(self, overrides: Dict[str, Any]) -> "StrategyParams":
        merged = {**self.values, **overrides}
        return StrategyParams(name=self.name, values=merged)

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


class V3Strategy:
    """
    Abstract base. Concrete strategies implement `emit`.
    """

    name: str = "v3_strategy"
    default_params: StrategyParams = StrategyParams(name="base", values={})
    # Parameter sensitivity grid — pure declarative, engine reads this
    # without introspection. Grid points should be small enough for
    # multiple-hypothesis correction (deflated Sharpe) to stay sane.
    param_grid: Dict[str, List[Any]] = {}

    def __init__(self, loader: PITDataLoader, params: Optional[StrategyParams] = None):
        self.loader = loader
        self.params = params or self.default_params

    def emit(self, as_of_s: int, universe_condition_ids: Iterable[str]) -> List[Signal]:
        raise NotImplementedError

    @classmethod
    def sweep_configs(cls) -> List[Dict[str, Any]]:
        """Enumerate all combinations from param_grid. Small grids only."""
        keys = list(cls.param_grid.keys())
        if not keys:
            return [dict(cls.default_params.values)]
        out: List[Dict[str, Any]] = [{}]
        for k in keys:
            new = []
            for v in cls.param_grid[k]:
                for cfg in out:
                    new.append({**cfg, k: v})
            out = new
        # Backfill defaults for unset keys
        for cfg in out:
            for k, v in cls.default_params.values.items():
                cfg.setdefault(k, v)
        return out

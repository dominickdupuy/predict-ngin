"""
Signal-decay overlay (V3 §7.1).

Given a rolling PnL series from a live (or live-like) strategy, compute
live_sharpe / backtest_sharpe over a window. Fire a "scale-down" or
"unwind" event when the ratio crosses a pre-registered threshold.

This is not a signal-emitting strategy. It is a risk overlay that the
backtest engine and paper-trading harness both consume.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

import numpy as np
import pandas as pd


class DecayAction(str, Enum):
    HOLD = "HOLD"
    SCALE_DOWN = "SCALE_DOWN"
    UNWIND = "UNWIND"


@dataclass(frozen=True)
class DecayVerdict:
    action: DecayAction
    ratio: float
    live_sharpe: float
    backtest_sharpe: float
    window_days: int
    reason: str


class SignalDecayOverlay:
    """Compares live PnL Sharpe to backtest Sharpe and emits scale decisions."""

    def __init__(
        self,
        scale_down_ratio: float = 0.5,
        unwind_ratio: float = 0.2,
        scale_down_days: int = 14,
        unwind_days: int = 7,
        window_days: int = 30,
    ):
        self.scale_down_ratio = float(scale_down_ratio)
        self.unwind_ratio = float(unwind_ratio)
        self.scale_down_days = int(scale_down_days)
        self.unwind_days = int(unwind_days)
        self.window_days = int(window_days)

    def evaluate(
        self,
        live_daily_pnl: pd.Series,
        backtest_sharpe: float,
    ) -> DecayVerdict:
        """live_daily_pnl is indexed by date, one value per day."""
        if live_daily_pnl.empty or backtest_sharpe <= 0:
            return DecayVerdict(
                action=DecayAction.HOLD,
                ratio=1.0,
                live_sharpe=0.0,
                backtest_sharpe=backtest_sharpe,
                window_days=self.window_days,
                reason="insufficient data",
            )
        recent = live_daily_pnl.tail(self.window_days)
        if len(recent) < 5 or recent.std(ddof=1) == 0:
            return DecayVerdict(
                action=DecayAction.HOLD,
                ratio=1.0,
                live_sharpe=0.0,
                backtest_sharpe=backtest_sharpe,
                window_days=self.window_days,
                reason="insufficient window",
            )
        daily_mean = float(recent.mean())
        daily_std = float(recent.std(ddof=1))
        live_sharpe = (daily_mean / daily_std) * np.sqrt(252) if daily_std > 0 else 0.0
        ratio = live_sharpe / backtest_sharpe if backtest_sharpe > 0 else 0.0

        # Sustained breach check
        if len(live_daily_pnl) >= self.unwind_days:
            tail = live_daily_pnl.tail(self.unwind_days)
            if tail.std(ddof=1) > 0:
                tail_sharpe = float(tail.mean()) / float(tail.std(ddof=1)) * np.sqrt(252)
                tail_ratio = tail_sharpe / backtest_sharpe
                if tail_ratio < self.unwind_ratio:
                    return DecayVerdict(
                        action=DecayAction.UNWIND,
                        ratio=tail_ratio,
                        live_sharpe=tail_sharpe,
                        backtest_sharpe=backtest_sharpe,
                        window_days=self.unwind_days,
                        reason=(
                            f"{self.unwind_days}d ratio={tail_ratio:.2f} "
                            f"< unwind_ratio={self.unwind_ratio}"
                        ),
                    )

        if len(live_daily_pnl) >= self.scale_down_days:
            tail = live_daily_pnl.tail(self.scale_down_days)
            if tail.std(ddof=1) > 0:
                tail_sharpe = float(tail.mean()) / float(tail.std(ddof=1)) * np.sqrt(252)
                tail_ratio = tail_sharpe / backtest_sharpe
                if tail_ratio < self.scale_down_ratio:
                    return DecayVerdict(
                        action=DecayAction.SCALE_DOWN,
                        ratio=tail_ratio,
                        live_sharpe=tail_sharpe,
                        backtest_sharpe=backtest_sharpe,
                        window_days=self.scale_down_days,
                        reason=(
                            f"{self.scale_down_days}d ratio={tail_ratio:.2f} "
                            f"< scale_down_ratio={self.scale_down_ratio}"
                        ),
                    )

        return DecayVerdict(
            action=DecayAction.HOLD,
            ratio=ratio,
            live_sharpe=live_sharpe,
            backtest_sharpe=backtest_sharpe,
            window_days=self.window_days,
            reason="healthy",
        )

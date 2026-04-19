"""
Liquid universe selector.

The single most-sensitive backtest parameter (per PARAMETER_SENSITIVITY_SUMMARY.md)
is the liquid-market threshold. We expose it as a first-class knob and
force every backtest to declare it.

All universe membership is computed PIT-safely from the trade tape, never
from the end-of-life `volumeClob` field in markets_filtered.csv.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from .loader import PITDataLoader


@dataclass(frozen=True)
class UniverseSnapshot:
    as_of_s: int
    threshold_usd: float
    lookback_s: int
    condition_ids: List[str]
    per_market_volume: Dict[str, float]

    def __len__(self) -> int:
        return len(self.condition_ids)


class LiquidUniverse:
    """
    Selects markets whose trailing `lookback_s` volume >= threshold_usd.

    Thresholds to sweep (from PARAMETER_SENSITIVITY_SUMMARY):
        $100k, $300k, $500k, $750k, $1M
    """

    DEFAULT_THRESHOLDS = (100_000.0, 300_000.0, 500_000.0, 750_000.0, 1_000_000.0)

    def __init__(
        self,
        loader: PITDataLoader,
        threshold_usd: float = 500_000.0,
        lookback_s: int = 30 * 24 * 3600,   # 30d rolling
    ):
        self.loader = loader
        self.threshold_usd = float(threshold_usd)
        self.lookback_s = int(lookback_s)

    def snapshot(self, as_of_s: int, categories: Optional[Iterable[str]] = None) -> UniverseSnapshot:
        cats = list(categories) if categories else self.loader.categories_available()
        per_market: Dict[str, float] = {}
        for cat in cats:
            trades = self.loader.get_trades(cat, as_of_s=as_of_s, lookback_s=self.lookback_s)
            if trades.empty:
                continue
            vols = trades.groupby("conditionId")["usd_amount"].sum()
            for cid, v in vols.items():
                per_market[cid] = per_market.get(cid, 0.0) + float(v)
        kept = [cid for cid, v in per_market.items() if v >= self.threshold_usd]
        kept.sort()
        return UniverseSnapshot(
            as_of_s=as_of_s,
            threshold_usd=self.threshold_usd,
            lookback_s=self.lookback_s,
            condition_ids=kept,
            per_market_volume=per_market,
        )

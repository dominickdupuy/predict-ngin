"""
CLOB snapshot reconstruction.

We do not have historical L2 book snapshots on disk (see V1 §1.2 backlog).
What we do have is the trade tape. This module builds a *best-effort* book
snapshot from trade history that the execution module can walk as if it
were a real book.

Reconstruction model
--------------------

For market `m` at time `t`:

  1. Estimate local liquidity L(m,t) as total USD volume in the 24h
     preceding `t` (adaptively capped between 5k and 5M).

  2. The mid at t is the last trade price at-or-before t (PIT-safe).

  3. The half-spread is estimated as max(min_tick, k / sqrt(trades_per_hour))
     with k calibrated so that liquid markets (>$500k/24h) land at ~0.005
     half-spread and thin markets at ~0.025.

  4. The book is a geometric ladder: at level i (i=0 is best),
        price = mid ± (half_spread + i * tick)
        size  = L(m,t) * alpha * (1-alpha)^i
     with alpha = 0.15 (so level-0 has ~15% of depth, level-5 ~7%, etc.).

This is **not** a real L2 snapshot. What it is: a realistic, PIT-safe
approximation of CLOB depth that (a) walks the book for limit/market fills,
(b) models partial fills, (c) respects that thin books cannot absorb large
orders — instead of pretending a parametric sqrt(impact) formula covers
all regimes.

When true L2 data lands (V1 §1.2), the reconstructor is replaced by a
snapshot reader and the executor interface does not change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd


# Tick granularity on Polymarket. Most markets are 0.01; high-liquidity
# events drop to 0.001. We default to 0.001 for the book grid.
_MIN_TICK = 0.001


@dataclass(frozen=True)
class BookLevel:
    price: float
    size_usd: float


@dataclass(frozen=True)
class BookSnapshot:
    condition_id: str
    as_of_s: int
    mid: float
    bids: List[BookLevel]   # descending in price
    asks: List[BookLevel]   # ascending in price
    liquidity_24h_usd: float
    tick: float

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


class CLOBBookReconstructor:
    """
    Reconstruct a best-effort book from the trade tape.

    Parameters are exposed so sensitivity tests can sweep them.
    """

    def __init__(
        self,
        alpha: float = 0.15,         # level-0 depth share
        decay: float = 0.85,         # size multiplier per level
        n_levels: int = 10,
        liquid_spread_half: float = 0.005,
        thin_spread_half: float = 0.010,   # 1¢ half = 2¢ total; matches live median ~1.3¢
        liquid_volume_threshold: float = 500_000.0,
        min_tick: float = _MIN_TICK,
        local_window_s: int = 24 * 3600,
        min_liquidity_usd: float = 5_000.0,
        max_liquidity_usd: float = 5_000_000.0,
    ):
        self.alpha = float(alpha)
        self.decay = float(decay)
        self.n_levels = int(n_levels)
        self.liquid_spread_half = float(liquid_spread_half)
        self.thin_spread_half = float(thin_spread_half)
        self.liquid_volume_threshold = float(liquid_volume_threshold)
        self.min_tick = float(min_tick)
        self.local_window_s = int(local_window_s)
        self.min_liquidity_usd = float(min_liquidity_usd)
        self.max_liquidity_usd = float(max_liquidity_usd)

    def reconstruct(
        self,
        condition_id: str,
        as_of_s: int,
        trades: pd.DataFrame,
    ) -> Optional[BookSnapshot]:
        """
        Build a snapshot at as_of_s from trades (must be PIT-filtered already).

        `trades` must contain at least: timestamp, conditionId, price, usd_amount.
        Returns None if the market has no trade history up to as_of_s.
        """
        if trades.empty:
            return None
        mask = (trades["conditionId"] == condition_id) & (trades["timestamp"] <= as_of_s)
        hist = trades.loc[mask]
        if hist.empty:
            return None

        # Local liquidity over the rolling window
        left = as_of_s - self.local_window_s
        recent = hist[hist["timestamp"] >= left]
        liquidity = float(recent["usd_amount"].sum())
        liquidity = float(np.clip(liquidity, self.min_liquidity_usd, self.max_liquidity_usd))

        mid = float(hist["price"].iloc[-1])
        mid = float(np.clip(mid, self.min_tick, 1.0 - self.min_tick))

        # Trade frequency → spread estimate
        span_h = max(1.0, (as_of_s - int(hist["timestamp"].iloc[0])) / 3600.0)
        trades_per_hour = len(hist) / span_h
        half_spread = self._interp_half_spread(liquidity, trades_per_hour)
        # Round to tick grid
        half_spread = max(self.min_tick, round(half_spread / self.min_tick) * self.min_tick)

        bids, asks = [], []
        depth0 = liquidity * self.alpha
        for i in range(self.n_levels):
            size_usd = depth0 * (self.decay ** i)
            bid_price = mid - half_spread - i * self.min_tick
            ask_price = mid + half_spread + i * self.min_tick
            if 0 < bid_price < 1:
                bids.append(BookLevel(price=round(bid_price, 4), size_usd=size_usd))
            if 0 < ask_price < 1:
                asks.append(BookLevel(price=round(ask_price, 4), size_usd=size_usd))

        return BookSnapshot(
            condition_id=condition_id,
            as_of_s=as_of_s,
            mid=mid,
            bids=bids,
            asks=asks,
            liquidity_24h_usd=liquidity,
            tick=self.min_tick,
        )

    def _interp_half_spread(self, liquidity: float, trades_per_hour: float) -> float:
        """Interpolate between thin and liquid regimes by rolling volume."""
        if liquidity <= 0:
            return self.thin_spread_half
        log_ratio = np.log10(max(1.0, liquidity)) - np.log10(self.liquid_volume_threshold)
        # log_ratio > 0 → more liquid than threshold; clip to [-2, +1]
        t = float(np.clip(log_ratio, -2.0, 1.0))
        # map t=-2 → thin, t=+1 → liquid, linear in log-space
        w = (t + 2.0) / 3.0  # [0, 1]
        base = self.thin_spread_half + (self.liquid_spread_half - self.thin_spread_half) * w
        # Low trade frequency widens spread
        if trades_per_hour < 5:
            base *= 1.5
        elif trades_per_hour < 1:
            base *= 2.5
        return float(base)

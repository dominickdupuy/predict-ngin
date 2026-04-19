"""
Calendar-butterfly arbitrage on date-laddered markets.

V3 §4.2. For three same-event markets with sequential end dates t1 < t2 < t3
on the same question, the survival probabilities must form a non-increasing
sequence. Concavity (declining hazard rate) implies:

    p(t2) <= 0.5 * (p(t1) + p(t3))

which means the *butterfly* b = p(t2) - 0.5*(p(t1) + p(t3)) should be <= 0.
A strictly positive butterfly is a structural mispricing: we can short the
middle leg and long the wings in a 1:2 ratio, hold to resolution, and the
payoff is non-negative in every state of the world.

This strategy detects positive butterflies above a threshold and emits
three signals per event. Position signs are computed so the trade is
directionally correct regardless of which leg is the "belly".

Grouping
--------
Markets are grouped by `eventSlug` when available, falling back to
`groupItemTitle` + category. Only sibling markets with distinct end-dates
and the same YES/NO outcome semantics are considered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..data.loader import PITDataLoader, MarketMeta
from .base import Signal, SignalSide, StrategyParams, V3Strategy


@dataclass(frozen=True)
class _Leg:
    condition_id: str
    end_s: int
    mid: float
    meta: MarketMeta


class CalendarButterfly(V3Strategy):
    name = "calendar_butterfly"

    default_params = StrategyParams(
        name=name,
        values={
            "min_butterfly_bps": 200,       # 2¢ threshold after fees
            "min_hours_to_resolution": 24,  # avoid final 24h (V3 §5.2 zone)
            "max_hours_to_resolution": 180 * 24,
            "notional_usd_per_leg": 500.0,
            "max_ratio_between_legs_hours": 180 * 24,
            "price_staleness_s": 3600,
        },
    )

    param_grid = {
        "min_butterfly_bps": [100, 200, 400, 800],
        "notional_usd_per_leg": [250.0, 500.0, 1000.0],
        "price_staleness_s": [1800, 3600, 7200],
    }

    def emit(self, as_of_s: int, universe_condition_ids: Iterable[str]) -> List[Signal]:
        p = self.params
        min_bf = p.get("min_butterfly_bps") / 10_000.0
        min_h = p.get("min_hours_to_resolution")
        max_h = p.get("max_hours_to_resolution")
        staleness = p.get("price_staleness_s")
        leg_usd = p.get("notional_usd_per_leg")

        universe = set(universe_condition_ids)
        if not universe:
            return []

        # Build leg objects per (category, event group)
        legs_by_group: Dict[Tuple[str, str], List[_Leg]] = {}
        for cat in self.loader.categories_available():
            markets = self.loader._load_markets(cat)
            if markets.empty or "conditionId" not in markets.columns:
                continue
            cids = set(markets["conditionId"].tolist()) & universe
            if not cids:
                continue
            for cid in cids:
                meta = self.loader.get_market_meta(cid, as_of_s)
                if meta is None or meta.end_date_s == 0:
                    continue
                hrs = meta.hours_to_resolution
                if hrs < min_h or hrs > max_h:
                    continue
                mid = self.loader.get_mid_price(cat, cid, as_of_s, max_staleness_s=staleness)
                if mid is None:
                    continue
                group_key = (cat, meta.event_slug or meta.group_item_title or "")
                if not group_key[1]:
                    continue
                legs_by_group.setdefault(group_key, []).append(
                    _Leg(condition_id=cid, end_s=meta.end_date_s, mid=mid, meta=meta)
                )

        signals: List[Signal] = []
        for group_key, legs in legs_by_group.items():
            if len(legs) < 3:
                continue
            # Sort by end date. Butterfly is defined on consecutive triples.
            legs_sorted = sorted(legs, key=lambda l: l.end_s)
            for i in range(len(legs_sorted) - 2):
                a, b, c = legs_sorted[i], legs_sorted[i + 1], legs_sorted[i + 2]
                # Require roughly even spacing so the butterfly interpretation holds
                dt_ab = b.end_s - a.end_s
                dt_bc = c.end_s - b.end_s
                if dt_ab <= 0 or dt_bc <= 0:
                    continue
                if max(dt_ab, dt_bc) > 3 * min(dt_ab, dt_bc):
                    continue
                butterfly = b.mid - 0.5 * (a.mid + c.mid)
                if butterfly <= min_bf:
                    continue
                # Position: SELL belly, BUY wings. Available_at = max of input
                # feature times (price staleness already bounded).
                # We approximate available_at by as_of (prices have already
                # been filtered <= as_of, and meta is created-at-start).
                available_at = as_of_s
                reason = (
                    f"butterfly={butterfly:.4f} > {min_bf:.4f}, "
                    f"event={group_key[1][:40]}"
                )
                feats = {
                    "butterfly": butterfly,
                    "p_a": a.mid, "p_b": b.mid, "p_c": c.mid,
                    "dt_ab_days": dt_ab / 86400.0,
                    "dt_bc_days": dt_bc / 86400.0,
                }
                signals.append(Signal(
                    strategy_name=self.name, condition_id=a.condition_id,
                    as_of_s=as_of_s, available_at_s=available_at,
                    side=SignalSide.BUY, notional_usd=leg_usd,
                    exit_price=None, expected_hold_s=dt_ab,
                    conviction=min(1.0, float(butterfly / (2 * min_bf))),
                    reason=reason + " [wing-left]", features=feats,
                ))
                signals.append(Signal(
                    strategy_name=self.name, condition_id=b.condition_id,
                    as_of_s=as_of_s, available_at_s=available_at,
                    side=SignalSide.SELL, notional_usd=2 * leg_usd,
                    exit_price=None, expected_hold_s=dt_ab,
                    conviction=min(1.0, float(butterfly / (2 * min_bf))),
                    reason=reason + " [belly]", features=feats,
                ))
                signals.append(Signal(
                    strategy_name=self.name, condition_id=c.condition_id,
                    as_of_s=as_of_s, available_at_s=available_at,
                    side=SignalSide.BUY, notional_usd=leg_usd,
                    exit_price=None, expected_hold_s=dt_ab,
                    conviction=min(1.0, float(butterfly / (2 * min_bf))),
                    reason=reason + " [wing-right]", features=feats,
                ))
        return signals

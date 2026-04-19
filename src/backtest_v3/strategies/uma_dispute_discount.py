"""
UMA dispute-risk discount mining (V3 §6.1).

Markets close to resolution (short TTR) trading at price ~0.95–0.99 imply
a discount reflecting UMA dispute risk. Historical dispute rate is ~2–3%;
when the observed discount (1 - price) exceeds the empirical dispute rate
by a threshold, BUY the YES and hold to resolution.

Symmetric logic applies on the short side (price near 0.01–0.05).

This strategy is deliberately capped-risk: position size scales down
linearly with 1 - price (or price for the short side) so tail loss on a
disputed resolution is bounded.

Data caveat: the historical dispute rate is not in our data set yet. We
use a configured prior (`dispute_prior`) with a conservative default of
0.03. When we have UMA contract-event data (V1 §4.2 backlog), we'll
estimate this per-category and replace the prior.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from ..data.loader import PITDataLoader
from .base import Signal, SignalSide, StrategyParams, V3Strategy


class UMADisputeDiscount(V3Strategy):
    name = "uma_dispute_discount"

    default_params = StrategyParams(
        name=name,
        values={
            "dispute_prior": 0.03,           # P(adverse dispute) prior
            "discount_edge_bps": 100,        # required gap above prior
            "ttr_min_hours": 1,
            "ttr_max_hours": 24,
            "price_upper_yes": 0.99,
            "price_lower_yes": 0.92,
            "price_staleness_s": 900,
            "max_payout_usd": 200.0,         # cap loss magnitude per trade
        },
    )

    param_grid = {
        "dispute_prior": [0.02, 0.03, 0.05],
        "discount_edge_bps": [50, 100, 200],
        "ttr_max_hours": [12, 24, 48],
    }

    def emit(self, as_of_s: int, universe_condition_ids: Iterable[str]) -> List[Signal]:
        p = self.params
        prior = float(p.get("dispute_prior"))
        edge = p.get("discount_edge_bps") / 10_000.0
        ttr_min = p.get("ttr_min_hours") * 3600
        ttr_max = p.get("ttr_max_hours") * 3600
        p_upper = p.get("price_upper_yes")
        p_lower = p.get("price_lower_yes")
        staleness = p.get("price_staleness_s")
        max_payout = p.get("max_payout_usd")

        signals: List[Signal] = []
        universe = set(universe_condition_ids)
        if not universe:
            return signals

        for cat in self.loader.categories_available():
            markets = self.loader._load_markets(cat)
            if markets.empty or "conditionId" not in markets.columns:
                continue
            cids = set(markets["conditionId"].tolist()) & universe
            for cid in cids:
                meta = self.loader.get_market_meta(cid, as_of_s)
                if meta is None or meta.end_date_s == 0:
                    continue
                ttr = meta.end_date_s - as_of_s
                if ttr < ttr_min or ttr > ttr_max:
                    continue
                mid = self.loader.get_mid_price(cat, cid, as_of_s, max_staleness_s=staleness)
                if mid is None:
                    continue

                # YES-side discount: mid in [p_lower, p_upper], implied "real" price ~1
                # so fair adjustment is 1 - mid. If (1 - mid) > prior + edge, BUY YES.
                if p_lower <= mid <= p_upper:
                    implied_discount = 1.0 - mid
                    if implied_discount > prior + edge:
                        # position sized so tail loss (mid -> 0) bounded by max_payout
                        notional = min(max_payout / max(0.01, mid), max_payout)
                        signals.append(Signal(
                            strategy_name=self.name,
                            condition_id=cid,
                            as_of_s=as_of_s,
                            available_at_s=as_of_s,
                            side=SignalSide.BUY,
                            notional_usd=notional,
                            exit_price=None,             # hold to resolution
                            expected_hold_s=int(ttr),
                            conviction=float(min(1.0, (implied_discount - prior) / (3 * edge))),
                            reason=(
                                f"yes-side discount={implied_discount:.4f} > "
                                f"prior+edge={prior + edge:.4f}, ttr={ttr/3600:.1f}h"
                            ),
                            features={
                                "mid": mid, "implied_discount": implied_discount,
                                "prior": prior, "ttr_h": ttr / 3600.0,
                            },
                        ))
                # NO-side symmetric
                if (1 - p_upper) <= mid <= (1 - p_lower):
                    implied_discount = mid
                    if implied_discount > prior + edge:
                        notional = min(max_payout / max(0.01, 1 - mid), max_payout)
                        signals.append(Signal(
                            strategy_name=self.name,
                            condition_id=cid,
                            as_of_s=as_of_s,
                            available_at_s=as_of_s,
                            side=SignalSide.SELL,
                            notional_usd=notional,
                            exit_price=None,
                            expected_hold_s=int(ttr),
                            conviction=float(min(1.0, (implied_discount - prior) / (3 * edge))),
                            reason=(
                                f"no-side discount={implied_discount:.4f} > "
                                f"prior+edge={prior + edge:.4f}, ttr={ttr/3600:.1f}h"
                            ),
                            features={
                                "mid": mid, "implied_discount": implied_discount,
                                "prior": prior, "ttr_h": ttr / 3600.0,
                            },
                        ))
        return signals

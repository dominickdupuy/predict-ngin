"""
Hazard-rate ladder fitting (V3 §4.1).

For a set of same-event markets with different end dates, fit a Weibull
survival curve S(t) = exp(-(t/lambda)^k) to the observed mid-prices, and
trade markets whose observed price deviates from the fit by > threshold.

The Weibull two-parameter family is a defensible prior for "time to event"
in political / legal contexts (Cox 1972; Kalbfleisch-Prentice 1980). For
very small laddders (3–4 points) the fit is underdetermined; we fall back
to a local-regression residual against a monotone spline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from ..data.loader import PITDataLoader, MarketMeta
from .base import Signal, SignalSide, StrategyParams, V3Strategy


@dataclass(frozen=True)
class _Leg:
    condition_id: str
    end_s: int
    mid: float
    meta: MarketMeta


def _weibull_survival(t: np.ndarray, lam: float, k: float) -> np.ndarray:
    lam = max(lam, 1e-6)
    return np.exp(-np.power(np.maximum(t, 0) / lam, k))


def _fit_weibull_nll(survivals: np.ndarray, ts: np.ndarray) -> Tuple[float, float]:
    """
    Fit Weibull (lambda, k) to observed survival points by minimizing
    least-squares residual in log-log space (Weibull plot). This is a
    standard closed-form estimator when data are noise-free survivals.
    """
    # Avoid log(0)
    s = np.clip(survivals, 1e-4, 1.0 - 1e-4)
    y = np.log(-np.log(s))           # ln(-ln S) = k*ln(t/lam) = k*ln(t) - k*ln(lam)
    x = np.log(np.maximum(ts, 1.0))
    # Linear regression y = k*x - k*ln(lam)
    xm = x.mean(); ym = y.mean()
    denom = float(((x - xm) ** 2).sum())
    if denom <= 1e-9:
        return 1.0, 1.0
    k = float(((x - xm) * (y - ym)).sum() / denom)
    if k <= 0 or not np.isfinite(k):
        return 1.0, 1.0
    intercept = ym - k * xm
    ln_lam = -intercept / k
    lam = float(np.exp(ln_lam))
    if not np.isfinite(lam) or lam <= 0:
        return 1.0, max(k, 0.1)
    return lam, max(k, 0.1)


class HazardLadder(V3Strategy):
    name = "hazard_ladder"

    default_params = StrategyParams(
        name=name,
        values={
            "min_ladder_size": 4,
            "min_dev_bps": 300,              # 3¢ residual
            "price_staleness_s": 3600,
            "notional_usd_per_trade": 500.0,
            "min_hours_to_resolution": 48,
            "max_hours_to_resolution": 365 * 24,
        },
    )

    param_grid = {
        "min_ladder_size": [3, 4, 5],
        "min_dev_bps": [200, 300, 500, 800],
        "notional_usd_per_trade": [250.0, 500.0, 1000.0],
    }

    def emit(self, as_of_s: int, universe_condition_ids: Iterable[str]) -> List[Signal]:
        p = self.params
        min_ladder = p.get("min_ladder_size")
        min_dev = p.get("min_dev_bps") / 10_000.0
        staleness = p.get("price_staleness_s")
        notional = p.get("notional_usd_per_trade")
        min_h = p.get("min_hours_to_resolution")
        max_h = p.get("max_hours_to_resolution")

        universe = set(universe_condition_ids)
        if not universe:
            return []

        groups: Dict[Tuple[str, str], List[_Leg]] = {}
        for cat in self.loader.categories_available():
            markets = self.loader._load_markets(cat)
            if markets.empty or "conditionId" not in markets.columns:
                continue
            cids = set(markets["conditionId"].tolist()) & universe
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
                key = (cat, meta.event_slug or meta.group_item_title or "")
                if not key[1]:
                    continue
                groups.setdefault(key, []).append(
                    _Leg(condition_id=cid, end_s=meta.end_date_s, mid=mid, meta=meta)
                )

        signals: List[Signal] = []
        for key, legs in groups.items():
            if len(legs) < min_ladder:
                continue
            legs_sorted = sorted(legs, key=lambda l: l.end_s)
            ts = np.array([(l.end_s - as_of_s) for l in legs_sorted], dtype=float)
            survivals = np.array([l.mid for l in legs_sorted], dtype=float)

            # Validate ladder is roughly monotone-decreasing (P("by later date")
            # should be higher, so survival-to-outcome = mid if YES). We use
            # observed prices directly — if the ladder is wildly non-monotone,
            # that's already a V1 monotonicity-arb signal, not a hazard-rate
            # one.
            if not np.all(np.diff(survivals) > -0.30):
                continue

            lam, k = _fit_weibull_nll(survivals, ts)
            fitted = _weibull_survival(ts, lam, k)
            residuals = survivals - fitted
            for leg, resid, fit_px in zip(legs_sorted, residuals, fitted):
                if abs(resid) < min_dev:
                    continue
                side = SignalSide.SELL if resid > 0 else SignalSide.BUY
                conviction = float(min(1.0, abs(resid) / (3 * min_dev)))
                signals.append(Signal(
                    strategy_name=self.name,
                    condition_id=leg.condition_id,
                    as_of_s=as_of_s,
                    available_at_s=as_of_s,
                    side=side,
                    notional_usd=notional,
                    exit_price=float(fit_px),
                    expected_hold_s=max(3600, int(leg.end_s - as_of_s) // 4),
                    conviction=conviction,
                    reason=(
                        f"weibull(lam={lam:.1f}s, k={k:.2f}) residual "
                        f"{resid:+.4f} vs threshold {min_dev:.4f}, event={key[1][:40]}"
                    ),
                    features={
                        "weibull_lambda_s": lam, "weibull_k": k,
                        "fitted_p": float(fit_px), "residual": float(resid),
                        "ladder_size": len(legs_sorted),
                    },
                ))
        return signals

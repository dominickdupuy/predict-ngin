"""
Crypto ↔ Deribit options arbitrage (V3 §2.5).

The Polymarket price for "BTC > $X by date" has a closed-form fair value
from Deribit ATM implied vol via Black-Scholes:

    p_impl = 1 - Phi( (ln(X/S) - (r - 0.5*sigma^2)*tau) / (sigma*sqrt(tau)) )

where S = spot, sigma = Deribit ATM IV, tau = years to market close, r = 0.

We do not fetch Deribit data in this module — the pattern is a
*dependency-injected feed*. The caller supplies a `feed` object with
methods `spot(as_of_s) -> float` and `atm_iv(as_of_s, tenor_s) -> float`.
That keeps the backtest self-contained when Deribit data is absent, and
makes live deployment a swap of feed implementation.

Strategy scope
--------------
The strategy only emits signals for markets whose `question` text matches
a configurable regex (default: BTC/ETH thresholds). If no matches in the
universe, it emits zero signals and logs nothing — a no-op that composes
safely with the other strategies.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Protocol

from ..data.loader import PITDataLoader
from .base import Signal, SignalSide, StrategyParams, V3Strategy


class CryptoFeed(Protocol):
    def spot(self, symbol: str, as_of_s: int) -> Optional[float]: ...
    def atm_iv(self, symbol: str, as_of_s: int, tenor_s: int) -> Optional[float]: ...


@dataclass
class NullCryptoFeed:
    """Feed that always returns None — strategy emits nothing."""
    def spot(self, symbol: str, as_of_s: int) -> Optional[float]:
        return None
    def atm_iv(self, symbol: str, as_of_s: int, tenor_s: int) -> Optional[float]:
        return None


_DEFAULT_RE = re.compile(
    r"\b(BTC|Bitcoin|ETH|Ethereum)\b.*?\$?([\d,]+(?:\.\d+)?)", re.IGNORECASE,
)


def _normcdf(x: float) -> float:
    # math.erf is enough for our precision needs
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_above_prob(S: float, K: float, sigma: float, tau_years: float, r: float = 0.0) -> float:
    if sigma <= 0 or tau_years <= 0 or S <= 0 or K <= 0:
        return float("nan")
    # Probability under risk-neutral lognormal that S_T > K
    d2 = (math.log(S / K) + (r - 0.5 * sigma * sigma) * tau_years) / (sigma * math.sqrt(tau_years))
    return float(_normcdf(d2))


class CryptoDeribitArb(V3Strategy):
    name = "crypto_deribit_arb"

    default_params = StrategyParams(
        name=name,
        values={
            "min_mispricing_bps": 500,       # 5¢ gap
            "notional_usd": 500.0,
            "price_staleness_s": 900,
            "ttr_min_hours": 24,
            "ttr_max_hours": 180 * 24,
        },
    )

    param_grid = {
        "min_mispricing_bps": [300, 500, 800],
        "notional_usd": [250.0, 500.0, 1000.0],
    }

    def __init__(
        self,
        loader: PITDataLoader,
        params: Optional[StrategyParams] = None,
        feed: Optional[CryptoFeed] = None,
        question_regex: Optional[re.Pattern] = None,
    ):
        super().__init__(loader=loader, params=params)
        self.feed = feed or NullCryptoFeed()
        self.question_regex = question_regex or _DEFAULT_RE

    def emit(self, as_of_s: int, universe_condition_ids: Iterable[str]) -> List[Signal]:
        p = self.params
        min_mis = p.get("min_mispricing_bps") / 10_000.0
        notional = p.get("notional_usd")
        staleness = p.get("price_staleness_s")
        ttr_min = p.get("ttr_min_hours") * 3600
        ttr_max = p.get("ttr_max_hours") * 3600

        signals: List[Signal] = []
        universe = set(universe_condition_ids)
        if not universe:
            return signals

        for cat in self.loader.categories_available():
            markets = self.loader._load_markets(cat)
            if markets.empty or "conditionId" not in markets.columns:
                continue
            sub = markets[markets["conditionId"].isin(universe)]
            if sub.empty:
                continue
            for _, r in sub.iterrows():
                cid = r["conditionId"]
                q = str(r.get("question", ""))
                m = self.question_regex.search(q)
                if not m:
                    continue
                symbol = m.group(1).upper()[:3]   # "BTC" or "ETH"
                if symbol == "BIT":
                    symbol = "BTC"
                if symbol == "ETH":
                    pass
                strike_s = m.group(2).replace(",", "")
                try:
                    strike = float(strike_s)
                except ValueError:
                    continue
                meta = self.loader.get_market_meta(cid, as_of_s)
                if meta is None or meta.end_date_s == 0:
                    continue
                ttr = meta.end_date_s - as_of_s
                if ttr < ttr_min or ttr > ttr_max:
                    continue

                spot = self.feed.spot(symbol, as_of_s)
                iv = self.feed.atm_iv(symbol, as_of_s, ttr)
                if spot is None or iv is None:
                    continue
                tau_years = ttr / (365.25 * 86400.0)
                p_impl = _bs_above_prob(spot, strike, iv, tau_years)
                if not (p_impl == p_impl):  # NaN check
                    continue
                mid = self.loader.get_mid_price(cat, cid, as_of_s, max_staleness_s=staleness)
                if mid is None:
                    continue
                gap = mid - p_impl
                if abs(gap) < min_mis:
                    continue
                side = SignalSide.SELL if gap > 0 else SignalSide.BUY
                signals.append(Signal(
                    strategy_name=self.name,
                    condition_id=cid,
                    as_of_s=as_of_s,
                    available_at_s=as_of_s,
                    side=side,
                    notional_usd=notional,
                    exit_price=float(p_impl),
                    expected_hold_s=min(ttr, 7 * 24 * 3600),
                    conviction=float(min(1.0, abs(gap) / (3 * min_mis))),
                    reason=(
                        f"mid={mid:.4f} vs BS p_impl={p_impl:.4f} "
                        f"(S={spot:.2f}, K={strike:.2f}, IV={iv:.3f})"
                    ),
                    features={
                        "spot": spot, "strike": strike, "iv": iv,
                        "mid": mid, "p_impl": p_impl, "gap": gap,
                        "symbol": symbol,
                    },
                ))
        return signals

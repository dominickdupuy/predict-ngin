"""
Pairs Trading Strategy for Prediction Markets
===============================================
Finds correlated market pairs within the same event group (e.g. election
candidates, margin-of-victory brackets) and trades mean reversion of their
spread using z-score signals.

Key properties of prediction market pairs:
- Same underlying event → structural correlation
- Price bounded [0, 1] → spreads are naturally mean-reverting near resolution
- Fast mean reversion: median half-life ~3h in historical data
- Best pairs: anti-correlated outcomes ("A wins" / "B wins") or adjacent
  brackets ("Trump +5-6%" / "Trump +6-7%")

Signal logic:
  - Fit hedge ratio on training half of data (walk-forward safe)
  - Compute rolling z-score on test half
  - Enter when |z| > ENTRY_Z (spread is anomalously wide)
  - Exit when |z| < EXIT_Z (spread reverted) or sign flip or max hold
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore")


# ── Config ─────────────────────────────────────────────────────────────────────

@dataclass
class PairsTradingConfig:
    # Pair selection
    min_obs: int = 40                 # min hourly observations required
    min_corr: float = 0.6             # min |correlation| to consider pair
    max_half_life_h: float = 72.0     # max mean-reversion half-life (hours)
    min_spread_std: float = 0.02      # min spread std (filters low-vol pairs)
    train_fraction: float = 0.5       # fraction of data used for calibration

    # Signal generation
    z_entry: float = 2.0              # z-score threshold to enter
    z_exit: float = 0.5               # z-score threshold to exit
    lookback_periods: int = 30        # rolling window for z-score (hours)
    max_hold_h: float = 48.0          # max hold before forced exit

    # Position sizing
    leg_size_usd: float = 250.0       # $ per leg (total = 2 * leg_size_usd)
    fee_rate: float = 0.01            # taker fee per leg

    # Filtering
    max_markets_per_event: int = 8    # skip events with too many markets
    min_markets_per_event: int = 2
    ffill_limit_h: int = 24           # max hours to forward-fill prices


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class PairSpec:
    """A calibrated market pair ready for signal generation."""
    event_slug: str
    cid1: str
    cid2: str
    title1: str
    title2: str
    correlation: float
    hedge_ratio: float       # OLS: s1 ~ slope * s2 + intercept
    intercept: float
    half_life_h: float
    spread_std: float
    n_train_obs: int


@dataclass
class PairsSignal:
    """Entry or exit signal for a pair."""
    pair_id: str              # f"{cid1}_{cid2}"
    event_slug: str
    signal_type: str          # "entry" or "exit"
    timestamp: pd.Timestamp
    cid1: str
    cid2: str
    direction: str            # "long1_short2" or "short1_long2"
    z_score: float
    spread: float
    spread_mean: float
    spread_std: float
    entry_price1: float
    entry_price2: float
    expected_profit: float    # rough estimate
    metadata: Dict[str, Any] = field(default_factory=dict)


# ── Pair calibration ───────────────────────────────────────────────────────────

def _half_life(spread: np.ndarray) -> float:
    """Ornstein-Uhlenbeck half-life via OLS on lagged spread."""
    lagged = spread[:-1]
    delta = np.diff(spread)
    if np.std(lagged) < 1e-8 or len(lagged) < 5:
        return np.inf
    slope, *_ = stats.linregress(lagged, delta)
    if slope >= 0:
        return np.inf
    return -np.log(2) / slope


def calibrate_pair(
    s1: np.ndarray,
    s2: np.ndarray,
    config: PairsTradingConfig,
) -> Optional[Tuple[float, float, float, float]]:
    """
    Fit OLS hedge ratio on training data and compute spread statistics.

    Returns:
        (slope, intercept, half_life_h, spread_std) or None if pair doesn't qualify.
    """
    if np.std(s1) < 1e-6 or np.std(s2) < 1e-6:
        return None

    corr, _ = stats.pearsonr(s1, s2)
    if abs(corr) < config.min_corr:
        return None

    slope, intercept, *_ = stats.linregress(s2, s1)
    spread = s1 - (slope * s2 + intercept)

    if np.std(spread) < config.min_spread_std:
        return None

    hl = _half_life(spread)
    if not np.isfinite(hl) or hl <= 0 or hl > config.max_half_life_h:
        return None

    return slope, intercept, hl, float(np.std(spread))


# ── Signal generator ───────────────────────────────────────────────────────────

class PairsTradingStrategy:
    """
    Scans all same-event market pairs and generates z-score mean-reversion signals.

    Usage:
        strategy = PairsTradingStrategy()
        pairs = strategy.find_pairs(trades_df, titles_map)
        signals = strategy.generate_signals(pairs, trades_df, titles_map)
    """

    def __init__(self, config: Optional[PairsTradingConfig] = None):
        self.cfg = config or PairsTradingConfig()

    # ── Pair discovery ────────────────────────────────────────────────────────

    def _build_hourly_pivot(
        self, trades: pd.DataFrame, event_slug: str
    ) -> pd.DataFrame:
        ev = trades[trades["eventSlug"] == event_slug].copy()
        if ev.empty:
            return pd.DataFrame()
        ev["hour"] = ev["ts"].dt.floor("1h")
        vwap = (
            ev.groupby(["hour", "conditionId"])
            .apply(
                lambda g: np.average(g["yes_price"], weights=g["size"]),
                include_groups=False,
            )
            .reset_index(name="vwap")
        )
        pivot = vwap.pivot(index="hour", columns="conditionId", values="vwap").sort_index()
        return pivot.ffill(limit=self.cfg.ffill_limit_h)

    def find_pairs(
        self,
        trades: pd.DataFrame,
        titles_map: Optional[Dict[str, str]] = None,
    ) -> List[PairSpec]:
        """
        Discover and calibrate all qualifying pairs in `trades`.

        Args:
            trades:     DataFrame with columns conditionId, eventSlug, yes_price,
                        ts (pd.Timestamp), size.
            titles_map: optional conditionId → title mapping for metadata.

        Returns:
            List of calibrated PairSpec objects.
        """
        cfg = self.cfg
        if titles_map is None:
            titles_map = trades.groupby("conditionId")["title"].first().to_dict() if "title" in trades.columns else {}

        ev_counts = trades.groupby("eventSlug")["conditionId"].nunique()
        candidate_events = ev_counts[
            (ev_counts >= cfg.min_markets_per_event)
            & (ev_counts <= cfg.max_markets_per_event)
        ].index

        pairs: List[PairSpec] = []

        for ev in candidate_events:
            pivot = self._build_hourly_pivot(trades, ev)
            pivot = pivot.dropna(axis=1, thresh=max(5, len(pivot) // 3))
            if pivot.shape[1] < 2 or len(pivot) < cfg.min_obs:
                continue

            cols = pivot.columns.tolist()
            from itertools import combinations
            for c1, c2 in combinations(cols, 2):
                both = pivot[[c1, c2]].dropna()
                if len(both) < cfg.min_obs:
                    continue

                split = int(len(both) * cfg.train_fraction)
                if split < cfg.min_obs // 2:
                    continue
                train = both.iloc[:split]

                result = calibrate_pair(
                    train[c1].values, train[c2].values, cfg
                )
                if result is None:
                    continue

                slope, intercept, hl, sp_std = result
                corr, _ = stats.pearsonr(train[c1].values, train[c2].values)

                pairs.append(PairSpec(
                    event_slug=ev,
                    cid1=c1,
                    cid2=c2,
                    title1=titles_map.get(c1, c1[:30]),
                    title2=titles_map.get(c2, c2[:30]),
                    correlation=round(float(corr), 3),
                    hedge_ratio=round(float(slope), 4),
                    intercept=round(float(intercept), 4),
                    half_life_h=round(hl, 2),
                    spread_std=round(sp_std, 4),
                    n_train_obs=split,
                ))

        pairs.sort(key=lambda p: p.half_life_h)
        return pairs

    # ── Signal generation ─────────────────────────────────────────────────────

    def generate_signals(
        self,
        pairs: List[PairSpec],
        trades: pd.DataFrame,
    ) -> List[PairsSignal]:
        """
        Generate entry/exit signals for all pairs using test-period data.

        Args:
            pairs:  Calibrated pairs from find_pairs().
            trades: Full trades DataFrame.

        Returns:
            List of PairsSignal objects sorted by timestamp.
        """
        cfg = self.cfg
        all_signals: List[PairsSignal] = []

        for pair in pairs:
            pivot = self._build_hourly_pivot(trades, pair.event_slug)
            if pair.cid1 not in pivot.columns or pair.cid2 not in pivot.columns:
                continue

            both = pivot[[pair.cid1, pair.cid2]].dropna()
            if len(both) < cfg.min_obs:
                continue

            split = int(len(both) * cfg.train_fraction)
            test = both.iloc[split:]
            if len(test) < 10:
                continue

            spread = test[pair.cid1] - (pair.hedge_ratio * test[pair.cid2] + pair.intercept)
            roll_mean = spread.rolling(cfg.lookback_periods, min_periods=10).mean()
            roll_std  = spread.rolling(cfg.lookback_periods, min_periods=10).std()
            z = ((spread - roll_mean) / roll_std.replace(0, np.nan)).dropna()

            pair_id = f"{pair.cid1[:8]}_{pair.cid2[:8]}"
            in_pos = False
            entry_z = 0.0
            entry_ts = None
            entry_direction = ""

            for ts, zval in z.items():
                if not in_pos:
                    if abs(zval) > cfg.z_entry:
                        # z > 0: spread above mean → short c1, long c2
                        direction = "short1_long2" if zval > 0 else "long1_short2"
                        p1 = both[pair.cid1].get(ts, float("nan"))
                        p2 = both[pair.cid2].get(ts, float("nan"))
                        if np.isnan(p1) or np.isnan(p2):
                            continue
                        ep = spread.get(ts, float("nan"))
                        em = roll_mean.get(ts, float("nan"))
                        es = roll_std.get(ts, 1.0)

                        # Expected profit = z-score-based capture estimate
                        exp_profit = abs(ep - em) * cfg.leg_size_usd - 2 * cfg.fee_rate * cfg.leg_size_usd

                        all_signals.append(PairsSignal(
                            pair_id=pair_id,
                            event_slug=pair.event_slug,
                            signal_type="entry",
                            timestamp=ts,
                            cid1=pair.cid1,
                            cid2=pair.cid2,
                            direction=direction,
                            z_score=float(zval),
                            spread=float(ep),
                            spread_mean=float(em) if not np.isnan(em) else 0.0,
                            spread_std=float(es),
                            entry_price1=float(p1),
                            entry_price2=float(p2),
                            expected_profit=float(exp_profit),
                            metadata={"half_life_h": pair.half_life_h},
                        ))
                        in_pos = True
                        entry_z = float(zval)
                        entry_ts = ts
                        entry_direction = direction

                else:
                    hold_h = (ts - entry_ts).total_seconds() / 3600 if entry_ts else 0.0
                    z_crossed = abs(zval) < cfg.z_exit
                    flipped = (zval * entry_z) < 0
                    timed_out = hold_h > cfg.max_hold_h

                    if z_crossed or flipped or timed_out:
                        exit_reason = "z_cross" if z_crossed else ("flip" if flipped else "timeout")
                        p1 = both[pair.cid1].get(ts, float("nan"))
                        p2 = both[pair.cid2].get(ts, float("nan"))
                        all_signals.append(PairsSignal(
                            pair_id=pair_id,
                            event_slug=pair.event_slug,
                            signal_type="exit",
                            timestamp=ts,
                            cid1=pair.cid1,
                            cid2=pair.cid2,
                            direction=entry_direction,
                            z_score=float(zval),
                            spread=float(spread.get(ts, float("nan"))),
                            spread_mean=float(roll_mean.get(ts, float("nan"))),
                            spread_std=float(roll_std.get(ts, 1.0)),
                            entry_price1=float(p1),
                            entry_price2=float(p2),
                            expected_profit=0.0,
                            metadata={"exit_reason": exit_reason, "hold_h": hold_h},
                        ))
                        in_pos = False

        all_signals.sort(key=lambda s: s.timestamp)
        return all_signals

    def get_parameters(self) -> Dict[str, Any]:
        cfg = self.cfg
        return {
            "strategy_name": "pairs_trading",
            "min_corr": cfg.min_corr,
            "max_half_life_h": cfg.max_half_life_h,
            "min_spread_std": cfg.min_spread_std,
            "z_entry": cfg.z_entry,
            "z_exit": cfg.z_exit,
            "lookback_periods": cfg.lookback_periods,
            "max_hold_h": cfg.max_hold_h,
            "leg_size_usd": cfg.leg_size_usd,
            "fee_rate": cfg.fee_rate,
        }

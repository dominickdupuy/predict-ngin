"""
Calendar Cascade Arbitrage Strategy
=====================================
Exploits information diffusion lag between markets in the same deadline series.

When a "by date D_i" market jumps sharply (event confirmed), later-deadline
markets "by date D_{i+1}, D_{i+2}, ..." must also update because:
  P(event by D_j) >= P(event by D_i)  for all D_j > D_i  [monotonicity law]

Any lag in the later markets is a tradeable opportunity.

Two sub-strategies:
  1. CASCADE:   Buy lagging followers after lead market jumps.
  2. MONOTONE:  Pure riskless arb when P(D_early) > P(D_late) — buy both legs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ── Signal types ─────────────────────────────────────────────────────────────

@dataclass
class CalendarCascadeSignal:
    """A cascade entry signal: buy the lagging follower market."""
    event_slug: str
    lead_cid: str          # conditionId of the market that jumped
    follow_cid: str        # conditionId of the lagging market
    signal_type: str       # "cascade" or "monotone"
    timestamp: pd.Timestamp
    lead_price: float      # current price of lead market
    follow_price: float    # current price of follow market (entry price)
    spread: float          # lead_price - follow_price (the lag)
    jump_size: float       # how much the lead market moved (cascade only)
    days_between: int      # deadline separation in days
    expected_profit: float # spread * capture_fraction - fees
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MonotonicityViolation:
    """A pure riskless arb: P(early) > P(late)."""
    event_slug: str
    early_cid: str
    late_cid: str
    timestamp: pd.Timestamp
    early_price: float     # should be <= late_price
    late_price: float
    spread: float          # early_price - late_price > 0
    net_profit_per_share: float  # spread - fees
    days_between: int


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class CalendarCascadeConfig:
    # Cascade parameters
    jump_window_periods: int = 5    # periods to measure jump over (at 5-min buckets)
    jump_threshold: float = 0.08    # min price move in lead to fire signal
    entry_lag_periods: int = 1      # wait N periods after detection before entry
    max_hold_periods: int = 24      # max hold (periods) before forced exit (24 * 5min = 2h)
    target_capture: float = 0.50   # exit at 50% of initial spread
    min_spread_entry: float = 0.01  # min spread between lead and follow to enter
    max_follow_price: float = 0.93  # don't buy if already near resolution
    min_lead_price: float = 0.10    # lead must have moved meaningfully
    max_lead_price: float = 0.90    # avoid near-resolved lead
    n_followers: int = 3            # max followers to buy per signal
    fee_rate: float = 0.01          # taker fee per leg

    # Monotonicity arb parameters
    mono_min_spread: float = 0.01   # min violation spread to trade
    mono_fee_rate: float = 0.01

    # Price bucket frequency
    bucket_freq: str = "5min"
    ffill_limit: int = 12           # max buckets to forward-fill (1h at 5min)


# ── Deadline parsing ──────────────────────────────────────────────────────────

_MONTH_PATTERN = (
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[a-z]*\.?\s+\d{1,2}(?:,?\s*\d{4})?"
)
_BY_PATTERN = re.compile(rf"by\s+({_MONTH_PATTERN})", re.IGNORECASE)


def parse_deadline(title: str) -> Optional[pd.Timestamp]:
    """Extract deadline date from a 'by <date>' market title."""
    m = _BY_PATTERN.search(str(title))
    if not m:
        return None
    ds = m.group(1).strip()
    if not re.search(r"\d{4}", ds):
        return None  # no year → ambiguous
    try:
        return pd.to_datetime(ds)
    except Exception:
        return None


# ── Core detector ─────────────────────────────────────────────────────────────

class CalendarCascadeDetector:
    """
    Identifies calendar cascade and monotonicity arb opportunities from
    a price pivot DataFrame (rows = time buckets, columns = conditionIds).

    Args:
        sorted_cids:  conditionIds ordered by deadline (ascending).
        deadlines:    mapping conditionId → pd.Timestamp deadline.
        config:       strategy hyperparameters.
    """

    def __init__(
        self,
        sorted_cids: List[str],
        deadlines: Dict[str, pd.Timestamp],
        event_slug: str,
        config: Optional[CalendarCascadeConfig] = None,
    ):
        self.sorted_cids = sorted_cids
        self.deadlines = deadlines
        self.event_slug = event_slug
        self.cfg = config or CalendarCascadeConfig()

    def detect_cascade_signals(
        self, pivot: pd.DataFrame
    ) -> List[CalendarCascadeSignal]:
        """Scan pivot for cascade entry signals."""
        cfg = self.cfg
        signals: List[CalendarCascadeSignal] = []

        for i, lead_cid in enumerate(self.sorted_cids[:-1]):
            if lead_cid not in pivot.columns:
                continue
            lead_series = pivot[lead_cid].dropna()
            if len(lead_series) < cfg.jump_window_periods + 1:
                continue

            # Detect upward jumps
            price_change = lead_series - lead_series.shift(cfg.jump_window_periods)
            jump_times = price_change[price_change > cfg.jump_threshold].index

            for jt in jump_times:
                lead_price = lead_series.get(jt, float("nan"))
                if not (cfg.min_lead_price < lead_price < cfg.max_lead_price):
                    continue

                # Entry after lag
                entry_idx_pos = lead_series.index.get_loc(jt)
                entry_pos = entry_idx_pos + cfg.entry_lag_periods
                if entry_pos >= len(pivot):
                    continue
                entry_ts = pivot.index[entry_pos]

                # Followers
                followers = self.sorted_cids[i + 1 : i + 1 + cfg.n_followers]
                for follow_cid in followers:
                    if follow_cid not in pivot.columns:
                        continue
                    follow_series = pivot[follow_cid]
                    if entry_ts not in follow_series.index:
                        continue
                    follow_price = follow_series.get(entry_ts, float("nan"))
                    if np.isnan(follow_price) or follow_price > cfg.max_follow_price:
                        continue

                    # Lead price at entry time
                    lead_at_entry_series = lead_series.reindex(pivot.index).ffill()
                    lead_at_entry = lead_at_entry_series.get(entry_ts, float("nan"))
                    if np.isnan(lead_at_entry):
                        continue

                    spread = lead_at_entry - follow_price
                    if spread < cfg.min_spread_entry:
                        continue

                    fees = cfg.fee_rate * 2
                    expected_profit = spread * cfg.target_capture - fees

                    d_lead = self.deadlines.get(lead_cid)
                    d_follow = self.deadlines.get(follow_cid)
                    days_between = int((d_follow - d_lead).days) if (d_lead and d_follow) else 0

                    signals.append(CalendarCascadeSignal(
                        event_slug=self.event_slug,
                        lead_cid=lead_cid,
                        follow_cid=follow_cid,
                        signal_type="cascade",
                        timestamp=entry_ts,
                        lead_price=float(lead_at_entry),
                        follow_price=float(follow_price),
                        spread=float(spread),
                        jump_size=float(price_change.get(jt, 0)),
                        days_between=days_between,
                        expected_profit=float(expected_profit),
                        metadata={"jump_detected_at": str(jt)},
                    ))

        return signals

    def detect_monotonicity_violations(
        self, pivot: pd.DataFrame
    ) -> List[MonotonicityViolation]:
        """Scan pivot for riskless monotonicity violations."""
        cfg = self.cfg
        violations: List[MonotonicityViolation] = []

        for i in range(len(self.sorted_cids) - 1):
            c_early = self.sorted_cids[i]
            c_late = self.sorted_cids[i + 1]
            if c_early not in pivot.columns or c_late not in pivot.columns:
                continue

            both = pivot[[c_early, c_late]].dropna()
            if len(both) < 3:
                continue

            spread_series = both[c_early] - both[c_late]  # must be <= 0
            viols = spread_series[spread_series > cfg.mono_min_spread]

            d_early = self.deadlines.get(c_early)
            d_late  = self.deadlines.get(c_late)
            days_between = int((d_late - d_early).days) if (d_early and d_late) else 0

            for ts, sprd in viols.items():
                p_early = float(both.loc[ts, c_early])
                p_late  = float(both.loc[ts, c_late])
                net_profit = sprd - cfg.mono_fee_rate * 2

                violations.append(MonotonicityViolation(
                    event_slug=self.event_slug,
                    early_cid=c_early,
                    late_cid=c_late,
                    timestamp=ts,
                    early_price=p_early,
                    late_price=p_late,
                    spread=float(sprd),
                    net_profit_per_share=float(net_profit),
                    days_between=days_between,
                ))

        return violations


# ── High-level scanner ────────────────────────────────────────────────────────

class CalendarCascadeStrategy:
    """
    Scans all calendar event series in a trades DataFrame for stat-arb signals.

    Usage:
        strategy = CalendarCascadeStrategy()
        signals, violations = strategy.scan(trades_df)
    """

    def __init__(self, config: Optional[CalendarCascadeConfig] = None):
        self.cfg = config or CalendarCascadeConfig()

    def build_event_series(
        self, trades: pd.DataFrame
    ) -> Dict[str, Tuple[List[str], Dict[str, pd.Timestamp]]]:
        """
        Find all calendar event series and return sorted conditionId lists.

        Returns:
            dict event_slug → (sorted_cids, deadlines_map)
        """
        if "ts" not in trades.columns:
            trades = trades.copy()
            trades["ts"] = pd.to_datetime(trades["timestamp"], unit="s", utc=True)
        if "yes_price" not in trades.columns:
            trades = trades.copy()
            trades["yes_price"] = np.where(
                trades["outcomeIndex"] == 1, 1.0 - trades["price"], trades["price"]
            )

        ev_counts = trades.groupby("eventSlug")["conditionId"].nunique()
        multi_events = ev_counts[ev_counts >= 3].index

        id_to_title = (
            trades.groupby("conditionId")["title"].first().to_dict()
        )

        result: Dict[str, Tuple[List[str], Dict[str, pd.Timestamp]]] = {}
        for ev in multi_events:
            cids = trades[trades["eventSlug"] == ev]["conditionId"].unique()
            deadlines: Dict[str, pd.Timestamp] = {}
            for cid in cids:
                d = parse_deadline(id_to_title.get(cid, ""))
                if d is not None:
                    deadlines[cid] = d
            if len(deadlines) >= 3:
                sorted_cids = sorted(deadlines.keys(), key=lambda c: deadlines[c])
                result[ev] = (sorted_cids, deadlines)
        return result

    def build_pivot(
        self, trades: pd.DataFrame, event_slug: str, cids: List[str]
    ) -> pd.DataFrame:
        """Build a time-bucketed VWAP pivot for a single event."""
        if "ts" not in trades.columns:
            trades = trades.copy()
            trades["ts"] = pd.to_datetime(trades["timestamp"], unit="s", utc=True)
        if "yes_price" not in trades.columns:
            trades = trades.copy()
            trades["yes_price"] = np.where(
                trades["outcomeIndex"] == 1, 1.0 - trades["price"], trades["price"]
            )

        ev_t = trades[
            (trades["eventSlug"] == event_slug)
            & (trades["conditionId"].isin(cids))
            & (trades["yes_price"] > 0.001)
            & (trades["yes_price"] < 0.999)
        ].copy()
        if ev_t.empty:
            return pd.DataFrame()

        ev_t["bucket"] = ev_t["ts"].dt.floor(self.cfg.bucket_freq)
        vwap = (
            ev_t.groupby(["bucket", "conditionId"])
            .apply(
                lambda g: np.average(g["yes_price"], weights=g["size"]),
                include_groups=False,
            )
            .reset_index(name="vwap")
        )
        pivot = vwap.pivot(index="bucket", columns="conditionId", values="vwap").sort_index()
        pivot = pivot.ffill(limit=self.cfg.ffill_limit)
        return pivot

    def scan(
        self, trades: pd.DataFrame
    ) -> Tuple[List[CalendarCascadeSignal], List[MonotonicityViolation]]:
        """
        Full scan across all calendar event series.

        Returns:
            (cascade_signals, monotonicity_violations)
        """
        event_series = self.build_event_series(trades)
        all_signals: List[CalendarCascadeSignal] = []
        all_violations: List[MonotonicityViolation] = []

        for ev_slug, (sorted_cids, deadlines) in event_series.items():
            pivot = self.build_pivot(trades, ev_slug, sorted_cids)
            if pivot.empty:
                continue

            present = [c for c in sorted_cids if c in pivot.columns]
            if len(present) < 2:
                continue

            detector = CalendarCascadeDetector(
                sorted_cids=present,
                deadlines=deadlines,
                event_slug=ev_slug,
                config=self.cfg,
            )
            all_signals.extend(detector.detect_cascade_signals(pivot))
            all_violations.extend(detector.detect_monotonicity_violations(pivot))

        all_signals.sort(key=lambda s: s.timestamp)
        all_violations.sort(key=lambda v: v.timestamp)
        return all_signals, all_violations

    def get_parameters(self) -> Dict[str, Any]:
        cfg = self.cfg
        return {
            "strategy_name": "calendar_cascade",
            "jump_threshold": cfg.jump_threshold,
            "jump_window_periods": cfg.jump_window_periods,
            "entry_lag_periods": cfg.entry_lag_periods,
            "max_hold_periods": cfg.max_hold_periods,
            "target_capture": cfg.target_capture,
            "min_spread_entry": cfg.min_spread_entry,
            "n_followers": cfg.n_followers,
            "fee_rate": cfg.fee_rate,
            "mono_min_spread": cfg.mono_min_spread,
            "bucket_freq": cfg.bucket_freq,
        }

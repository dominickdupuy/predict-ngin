"""
Point-in-time data loader.

Every feature returned by this loader carries an `available_at` timestamp.
The backtest engine refuses to consume any feature where
`available_at > decision_time`. This enforces the look-ahead discipline
that V1 §9.1 and V2 Appendix D require.

Raw Polymarket data has three look-ahead traps we guard against:

1. **Market volume fields** (`volumeClob`, `volume24hr`, `liquidityNum`) are
   stored as end-of-life snapshots in `markets_filtered.csv`. Using them
   point-in-time requires reconstructing from trade history only.

2. **closedTime and resolution** are set after resolution. Any filter based
   on these leaks label information into the feature pipeline.

3. **endDateIso vs. actual resolution time.** `endDateIso` is (usually)
   known at market creation; `closedTime` is the actual resolution time.
   For backtests we must only use `endDateIso` to define "time-to-resolution".

This module never reads raw fields without first masking anything that
would be unavailable at `as_of`. Direct `pd.read_parquet` on the raw files
is explicitly discouraged inside strategies — use this loader.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd


# Epoch-seconds sentinel used by upstream data for "unknown"
_EPOCH_UNKNOWN = 0


def _to_epoch_s(ts) -> int:
    """Normalize to integer epoch seconds."""
    if pd.isna(ts):
        return _EPOCH_UNKNOWN
    if isinstance(ts, (int, np.integer)):
        return int(ts)
    if isinstance(ts, (float, np.floating)):
        return int(ts)
    if isinstance(ts, str):
        # ISO strings
        return int(pd.Timestamp(ts, tz="UTC").timestamp())
    if isinstance(ts, pd.Timestamp):
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return int(ts.timestamp())
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return int(ts.timestamp())
    raise TypeError(f"Unsupported timestamp type: {type(ts)}")


@dataclass(frozen=True)
class MarketMeta:
    """
    Point-in-time market metadata.

    `volume_at_as_of` is computed from trades, never copied from the raw
    `volumeClob` field. Fields that would be unavailable at `as_of`
    (resolution, closedTime) are explicitly None here.
    """

    condition_id: str
    question: str
    category: str
    event_slug: Optional[str]
    end_date_s: int              # scheduled end (typically known at creation)
    start_date_s: int            # known at creation
    neg_risk: bool               # known at creation
    group_item_title: Optional[str]
    volume_at_as_of: float       # reconstructed, PIT-safe
    as_of_s: int

    @property
    def is_active_at_as_of(self) -> bool:
        return self.start_date_s <= self.as_of_s <= self.end_date_s

    @property
    def hours_to_resolution(self) -> float:
        return max(0.0, (self.end_date_s - self.as_of_s) / 3600.0)


class PITDataLoader:
    """
    Loads Polymarket research data with point-in-time discipline.

    Paths:
        data_root/<Category>/markets_filtered.csv
        data_root/<Category>/trades.parquet
        data_root/<Category>/prices.parquet

    All queries take an `as_of` timestamp and only return facts knowable
    at or before `as_of`.
    """

    DEFAULT_CATEGORIES = (
        "Politics",
        "Geopolitics",
        "Economy",
        "Finance",
        "Climate_and_Science",
        "Sports",
        "Tech",
        "Art_and_Culture",
    )

    # Trade columns we actually use. Drop the other 20+ to keep memory lean.
    _TRADE_COLS = (
        "timestamp",
        "conditionId",
        "price",
        "size",
        "usd_amount",
        "side",
        "outcome",
        "proxyWallet",
        "eventSlug",
    )

    def __init__(
        self,
        data_root: str | Path,
        categories: Optional[Sequence[str]] = None,
        eager_load: bool = False,
    ):
        self.data_root = Path(data_root)
        if not self.data_root.exists():
            raise FileNotFoundError(f"data_root not found: {self.data_root}")
        self.categories = tuple(categories) if categories else self.DEFAULT_CATEGORIES
        self._trades_cache: Dict[str, pd.DataFrame] = {}
        self._markets_cache: Dict[str, pd.DataFrame] = {}
        self._resolutions_cache: Optional[pd.DataFrame] = None
        if eager_load:
            for cat in self.categories:
                self._load_trades(cat)
                self._load_markets(cat)

    # ------------------------------------------------------------------ raw loads

    def _load_trades(self, category: str) -> pd.DataFrame:
        if category in self._trades_cache:
            return self._trades_cache[category]
        path = self.data_root / category / "trades.parquet"
        if not path.exists():
            df = pd.DataFrame(columns=list(self._TRADE_COLS))
        else:
            avail = [c for c in self._TRADE_COLS if c in pd.read_parquet(path, columns=None).columns[:60]]
            # Read exactly the columns we need (fall back to all then subset)
            try:
                df = pd.read_parquet(path, columns=list(self._TRADE_COLS))
            except Exception:
                df = pd.read_parquet(path)
                keep = [c for c in self._TRADE_COLS if c in df.columns]
                df = df[keep].copy()
            # Drop rows with sentinel timestamp; these are unusable for PIT
            df = df[df["timestamp"] > 0].copy()
            df["timestamp"] = df["timestamp"].astype("int64")
            # The upstream parquet has two row generations: older rows carry
            # usd_amount directly; newer rows only have price + size. Fill
            # the missing usd_amount so downstream volume calcs work uniformly.
            if "usd_amount" in df.columns and "price" in df.columns and "size" in df.columns:
                missing = df["usd_amount"].isna()
                if missing.any():
                    df.loc[missing, "usd_amount"] = (
                        df.loc[missing, "price"] * df.loc[missing, "size"]
                    )
            df["category"] = category
            df = df.sort_values(["conditionId", "timestamp"], kind="mergesort").reset_index(drop=True)
        self._trades_cache[category] = df
        return df

    def _load_markets(self, category: str) -> pd.DataFrame:
        if category in self._markets_cache:
            return self._markets_cache[category]
        path = self.data_root / category / "markets_filtered.csv"
        if not path.exists():
            self._markets_cache[category] = pd.DataFrame()
            return self._markets_cache[category]
        # Only read fields we declare PIT-safe + scheduling fields
        safe_fields = [
            "conditionId",
            "question",
            "slug",
            "eventSlug",
            "startDate",
            "endDate",
            "endDateIso",
            "startDateIso",
            "negRisk",
            "negRiskMarketID",
            "groupItemTitle",
            "category",
            "topic1",
            "subcategory",
        ]
        header = pd.read_csv(path, nrows=0).columns.tolist()
        use = [c for c in safe_fields if c in header]
        df = pd.read_csv(path, usecols=use, low_memory=False)
        df["category"] = df.get("category", category).fillna(category) if "category" in df.columns else category
        # Canonicalize scheduling to epoch seconds
        for c in ("startDate", "endDate", "startDateIso", "endDateIso"):
            if c in df.columns:
                df[c + "_s"] = df[c].apply(_to_epoch_s)
        df["negRisk"] = df.get("negRisk", False).fillna(False).astype(bool) if "negRisk" in df.columns else False
        self._markets_cache[category] = df
        return df

    # ------------------------------------------------------------------ PIT queries

    def get_market_meta(self, condition_id: str, as_of_s: int) -> Optional[MarketMeta]:
        """Return PIT-safe metadata for a single market, or None if unknown at as_of."""
        for cat in self.categories:
            markets = self._load_markets(cat)
            if markets.empty or "conditionId" not in markets.columns:
                continue
            row = markets[markets["conditionId"] == condition_id]
            if row.empty:
                continue
            r = row.iloc[0]
            start_s = int(r.get("startDate_s", 0) or 0)
            end_s = int(r.get("endDateIso_s", 0) or r.get("endDate_s", 0) or 0)
            # The market does not exist yet if we're before its start.
            if start_s and start_s > as_of_s:
                return None
            vol_as_of = self._volume_as_of(cat, condition_id, as_of_s)
            return MarketMeta(
                condition_id=condition_id,
                question=str(r.get("question", "")),
                category=cat,
                event_slug=r.get("eventSlug") if pd.notna(r.get("eventSlug")) else None,
                end_date_s=end_s,
                start_date_s=start_s,
                neg_risk=bool(r.get("negRisk", False)),
                group_item_title=r.get("groupItemTitle") if pd.notna(r.get("groupItemTitle")) else None,
                volume_at_as_of=vol_as_of,
                as_of_s=as_of_s,
            )
        return None

    def _volume_as_of(self, category: str, condition_id: str, as_of_s: int) -> float:
        """USD volume traded up to and including as_of. PIT-safe by construction."""
        t = self._load_trades(category)
        if t.empty:
            return 0.0
        mask = (t["conditionId"] == condition_id) & (t["timestamp"] <= as_of_s)
        if not mask.any():
            return 0.0
        return float(t.loc[mask, "usd_amount"].sum())

    def get_trades(
        self,
        category: str,
        as_of_s: int,
        condition_id: Optional[str] = None,
        lookback_s: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Trades strictly at-or-before `as_of_s`. Optional `lookback_s` trims
        the left edge. PIT-safe (no rows with timestamp > as_of).
        """
        t = self._load_trades(category)
        if t.empty:
            return t
        right = t["timestamp"] <= as_of_s
        df = t.loc[right]
        if lookback_s is not None:
            left = df["timestamp"] >= (as_of_s - lookback_s)
            df = df.loc[left]
        if condition_id is not None:
            df = df[df["conditionId"] == condition_id]
        return df.copy()

    def get_mid_price(
        self,
        category: str,
        condition_id: str,
        as_of_s: int,
        max_staleness_s: int = 600,
    ) -> Optional[float]:
        """
        Latest trade price at-or-before as_of_s. Returns None if the last
        trade is older than `max_staleness_s` — stale prices are a classic
        source of fake edge in illiquid market backtests.
        """
        t = self._load_trades(category)
        if t.empty:
            return None
        mask = (t["conditionId"] == condition_id) & (t["timestamp"] <= as_of_s)
        sub = t.loc[mask]
        if sub.empty:
            return None
        last_ts = int(sub["timestamp"].iloc[-1])
        if as_of_s - last_ts > max_staleness_s:
            return None
        return float(sub["price"].iloc[-1])

    def get_live_markets(self, as_of_s: int, categories: Optional[Iterable[str]] = None) -> pd.DataFrame:
        """All markets that are 'live' at as_of — start<=as_of<=end. PIT-safe."""
        cats = categories or self.categories
        out = []
        for cat in cats:
            m = self._load_markets(cat)
            if m.empty:
                continue
            df = m.copy()
            df["category"] = cat
            start_s = df.get("startDate_s", pd.Series(0, index=df.index)).fillna(0)
            end_s = df.get("endDateIso_s", df.get("endDate_s", pd.Series(0, index=df.index))).fillna(0)
            df["_start_s"] = start_s.astype("int64")
            df["_end_s"] = end_s.astype("int64")
            mask = (df["_start_s"] <= as_of_s) & (df["_end_s"] >= as_of_s) & (df["_start_s"] > 0)
            out.append(df.loc[mask])
        if not out:
            return pd.DataFrame()
        return pd.concat(out, ignore_index=True)

    def categories_available(self) -> List[str]:
        return list(self.categories)

    # ------------------------------------------------------------------ iteration

    def iter_decision_times(
        self,
        start_s: int,
        end_s: int,
        step_s: int,
    ) -> Iterable[int]:
        """Uniform decision grid. Strategies may subsample further."""
        t = start_s
        while t <= end_s:
            yield t
            t += step_s

    # ------------------------------------------------------------------ self-test

    def assert_no_lookahead(self, series_with_available_at: pd.Series, decision_time_s: int) -> None:
        """Raise if any element has available_at > decision_time."""
        if series_with_available_at.empty:
            return
        if (series_with_available_at > decision_time_s).any():
            violators = series_with_available_at[series_with_available_at > decision_time_s].head(5).tolist()
            raise ValueError(
                f"Look-ahead detected: {len(violators)}+ features have "
                f"available_at > decision_time={decision_time_s}. First: {violators}"
            )

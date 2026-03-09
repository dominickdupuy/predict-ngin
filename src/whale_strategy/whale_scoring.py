"""
Whale quality scoring per strategy spec.

W = w1*S_performance + w2*S_conviction + w3*S_timing + w4*S_consistency
Weights: 0.40, 0.30, 0.15, 0.15
"""

from dataclasses import dataclass
from typing import Dict, Optional, Set

import numpy as np
import pandas as pd

# Score weights
W_PERFORMANCE = 0.40
W_CONVICTION = 0.30
W_TIMING = 0.15
W_CONSISTENCY = 0.15

# Minimum score for tradeable signal
MIN_WHALE_SCORE = 6.0

# Whale criteria (from spec)
WHALE_CRITERIA = {
    "min_position_size": 50_000,  # USD
    "min_market_liquidity": 100_000,
    "max_time_to_resolution": 180,  # days
    "min_whale_historical_trades": 10,
}


@dataclass
class ResolvedTrade:
    """Single resolved trade for performance calculation."""
    market_id: str
    roi: float
    profitable: bool
    timestamp: pd.Timestamp


def _get_resolved_trades(
    trades_df: pd.DataFrame,
    whale_address: str,
    resolution_winners: Dict[str, str],
    role: str = "maker",
    lookback_days: Optional[int] = None,
    as_of: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    """Get resolved trades for a whale with ROI and correctness."""
    trader_col = role
    direction_col = f"{role}_direction"

    df = trades_df[
        (trades_df[trader_col] == whale_address) &
        (trades_df["market_id"].astype(str).isin(resolution_winners.keys()))
    ].copy()
    if df.empty:
        return pd.DataFrame()

    if lookback_days and as_of:
        cutoff = as_of - pd.Timedelta(days=lookback_days)
        df = df[df["datetime"] <= as_of]
        df = df[df["datetime"] >= cutoff]

    df["market_id_str"] = df["market_id"].astype(str).str.replace(".0", "", regex=False)
    df["winner"] = df["market_id_str"].map(resolution_winners)
    df = df.dropna(subset=["winner"])

    direction = df[direction_col].str.lower()
    winner = df["winner"].str.upper()
    df["correct"] = (
        ((direction == "buy") & (winner == "YES")) |
        ((direction == "sell") & (winner == "NO"))
    )
    # ROI: (resolution - entry) / entry for buy YES; (entry - resolution) / (1-entry) for sell NO
    resolution = df["winner"].map({"YES": 1.0, "NO": 0.0})
    entry = df["price"].astype(float)
    df["roi"] = np.where(
        direction == "buy",
        (resolution - entry) / np.maximum(entry, 0.01),
        (entry - resolution) / np.maximum(1 - entry, 0.01),
    )
    df["profitable"] = df["roi"] > 0
    return df


def calculate_performance_score(
    whale_address: str,
    trades_df: pd.DataFrame,
    resolution_winners: Dict[str, str],
    role: str = "maker",
) -> float:
    """
    S_performance [0, 10]: Historical track record on resolved markets.
    """
    resolved = _get_resolved_trades(trades_df, whale_address, resolution_winners, role)
    if len(resolved) < WHALE_CRITERIA["min_whale_historical_trades"]:
        return 0.0

    win_rate = resolved["profitable"].mean()
    avg_roi = resolved["roi"].mean()
    returns = resolved["roi"].to_numpy()
    sharpe = np.mean(returns) / (np.std(returns) + 1e-6) if len(returns) > 1 else 0

    raw = (
        0.30 * (win_rate * 10) +
        0.40 * min(avg_roi * 2, 10) +
        0.30 * min(max(sharpe, 0) * 2, 10)
    )
    return min(float(raw), 10.0)


def estimate_whale_capital(
    trades_df: pd.DataFrame,
    whale_address: str,
    role: str = "maker",
) -> float:
    """Estimate whale capital: 5x their largest observed position."""
    df = trades_df[trades_df[role] == whale_address]
    if df.empty:
        return 100_000  # Default
    max_pos = df["usd_amount"].max()
    return max(float(max_pos) * 5, 50_000)


def calculate_conviction_score(
    trade_row: pd.Series,
    trades_df: pd.DataFrame,
    whale_address: str,
    role: str = "maker",
) -> float:
    """
    S_conviction [0, 10]: Position sizing intensity.
    Larger positions relative to whale's capital = higher conviction.
    """
    whale_capital = estimate_whale_capital(trades_df, whale_address, role)
    size_usd = float(trade_row.get("usd_amount", 0) or 0)
    if size_usd <= 0 or whale_capital <= 0:
        return 0.0

    concentration = size_usd / whale_capital
    # 20% concentration = max conviction
    concentration_score = min(concentration / 0.20 * 10, 10)
    # Single large entry bonus (simplified: assume single if size > 10k)
    speed_score = 10 if size_usd >= 10_000 else 5
    return 0.70 * concentration_score + 0.30 * speed_score


def _normalize_ts(ts) -> Optional[pd.Timestamp]:
    """Normalize to tz-naive for subtraction compatibility."""
    if ts is None or (isinstance(ts, float) and np.isnan(ts)):
        return None
    t = pd.to_datetime(ts, errors="coerce")
    if pd.isna(t):
        return None
    if hasattr(t, "tz") and t.tz is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t


def calculate_timing_score(
    trade_row: pd.Series,
    market_creation_ts: Optional[pd.Timestamp],
    market_end_ts: Optional[pd.Timestamp],
) -> float:
    """
    S_timing [0, 10]: Entry timing quality.
    Earlier entries (0-30% of market lifecycle) = higher score.
    """
    trade_ts = _normalize_ts(trade_row.get("datetime"))
    creation = _normalize_ts(market_creation_ts)
    end = _normalize_ts(market_end_ts)
    if trade_ts is None or creation is None or end is None:
        return 5.0  # Neutral

    total_days = (end - creation).days
    if total_days <= 0:
        return 5.0
    market_age_days = (trade_ts - creation).days
    maturity = market_age_days / total_days

    if maturity < 0.30:
        return 10.0
    elif maturity < 0.50:
        return 7.0
    elif maturity < 0.70:
        return 5.0
    else:
        return 3.0


def calculate_consistency_score(
    whale_address: str,
    trades_df: pd.DataFrame,
    resolution_winners: Dict[str, str],
    role: str = "maker",
    lookback_days: int = 30,
    as_of: Optional[pd.Timestamp] = None,
) -> float:
    """
    S_consistency [0, 10]: Recent performance vs historical baseline.
    """
    if as_of is None and "datetime" in trades_df.columns:
        as_of = trades_df["datetime"].max()

    recent = _get_resolved_trades(
        trades_df, whale_address, resolution_winners, role,
        lookback_days=lookback_days, as_of=as_of,
    )
    if len(recent) < 3:
        return 5.0

    recent_win_rate = recent["profitable"].mean()
    overall = _get_resolved_trades(trades_df, whale_address, resolution_winners, role)
    if len(overall) < 5:
        return 5.0
    overall_win_rate = overall["profitable"].mean()

    if recent_win_rate >= overall_win_rate * 1.1:
        return 10.0
    elif recent_win_rate >= overall_win_rate * 0.9:
        return 7.0
    elif recent_win_rate >= overall_win_rate * 0.7:
        return 4.0
    else:
        return 0.0


def calculate_whale_score(
    trade_row: pd.Series,
    trades_df: pd.DataFrame,
    resolution_winners: Dict[str, str],
    market_meta: Optional[Dict] = None,
    role: str = "maker",
) -> float:
    """
    Full whale quality score W ∈ [0, 10].

    W = 0.40*S_perf + 0.30*S_conv + 0.15*S_time + 0.15*S_cons
    """
    whale = trade_row.get(role, "")
    if not whale or not isinstance(whale, str) or not whale.startswith("0x"):
        return 0.0

    s_perf = calculate_performance_score(
        whale, trades_df, resolution_winners, role,
    )
    s_conv = calculate_conviction_score(trade_row, trades_df, whale, role)

    creation = None
    end = None
    if market_meta:
        for k in ("startDate", "startDateIso", "createdAt"):
            if k in market_meta and market_meta[k]:
                try:
                    creation = pd.to_datetime(market_meta[k])
                    break
                except Exception:
                    pass
        for k in ("endDate", "endDateIso", "closedTime"):
            if k in market_meta and market_meta[k]:
                try:
                    end = pd.to_datetime(market_meta[k])
                    break
                except Exception:
                    pass
    s_time = calculate_timing_score(trade_row, creation, end)

    s_cons = calculate_consistency_score(
        whale, trades_df, resolution_winners, role,
    )

    W = (
        W_PERFORMANCE * s_perf +
        W_CONVICTION * s_conv +
        W_TIMING * s_time +
        W_CONSISTENCY * s_cons
    )
    return min(max(float(W), 0.0), 10.0)


def qualifies_as_whale_signal(
    trade_row: pd.Series,
    whale_score: float,
    market_liquidity: float,
    time_to_resolution_days: Optional[float],
) -> bool:
    """Check if trade qualifies as tradeable whale signal."""
    if whale_score < MIN_WHALE_SCORE:
        return False
    if float(trade_row.get("usd_amount", 0)) < WHALE_CRITERIA["min_position_size"]:
        return False
    if market_liquidity < WHALE_CRITERIA["min_market_liquidity"]:
        return False
    if time_to_resolution_days is not None and time_to_resolution_days > WHALE_CRITERIA["max_time_to_resolution"]:
        return False
    return True

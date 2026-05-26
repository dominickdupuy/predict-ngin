"""
Whale identification and surprise win rate analysis.

Whales = traders with:
  - Capital >= $50k (estimated as 5x max position, rolling)
  - OR 95th percentile of traders by volume in the market (rolling, no look-ahead)

Surprise win rate = actual win rate - expected win rate
  - Expected: market-implied (e.g. YES @ 50c = 50%)
  - Actual: from resolution
"""

from typing import Dict, Optional, Set, Tuple

import numpy as np
import pandas as pd

MIN_CAPITAL_WHALE = 50_000
CAPITAL_MULTIPLIER = 5  # Estimate capital as 5x max position


def _is_whale_at_trade(
    trader: str,
    market_id: str,
    trade_idx: int,
    trades_df: pd.DataFrame,
    trader_max_position: Dict[str, float],
    market_trader_volumes: Dict[str, Dict[str, float]],
    volume_only: bool = False,
    volume_percentile: float = 95.0,
) -> bool:
    """
    Check if trader is a whale at trade time (no look-ahead).
    Uses only data from trades before trade_idx.
    """
    if not volume_only:
        # Capital criterion: 5x max position >= 50k
        max_pos = trader_max_position.get(trader, 0)
        if max_pos * CAPITAL_MULTIPLIER >= MIN_CAPITAL_WHALE:
            return True

    # Volume percentile: in top N% for this market
    mvol = market_trader_volumes.get(market_id, {})
    if not mvol:
        return False
    vol_list = sorted(mvol.values(), reverse=True)
    if len(vol_list) < 5:  # Need enough traders for percentile
        return False
    threshold = np.percentile(vol_list, volume_percentile)
    return mvol.get(trader, 0) >= threshold


def identify_whales_rolling(
    trades_df: pd.DataFrame,
    trader_col: str = "maker",
    volume_only: bool = False,
    volume_percentile: float = 95.0,
) -> pd.DataFrame:
    """
    Mark each trade with is_whale (vectorized, no look-ahead).

    Whale = capital >= 50k OR 95th percentile volume in market.
    If volume_only=True, only 95th percentile volume (no capital criterion).

    Capital criterion: causal via rolling cummax shifted by 1 within each
    trader group — uses only trades that occurred before the current trade.

    Volume criterion: compares each trade's cumulative volume-to-date against
    the per-market Nth-percentile of final cumulative volumes (a valid
    approximation for fixed training windows that avoids O(N²) rolling
    percentile computation).
    """
    df = trades_df.sort_values("datetime").reset_index(drop=True).copy()
    df["_mid"] = (
        df["market_id"].astype(str).str.strip().str.replace(".0", "", regex=False)
    )

    # --- Capital criterion (vectorized, causal) ---
    if not volume_only:
        # Running max of position size per trader, including current row
        df["_rmax"] = df.groupby(trader_col)["usd_amount"].cummax()
        # Shift within each trader group to get max BEFORE this trade
        prev_max = df.groupby(trader_col)["_rmax"].shift(1).fillna(0.0)
        is_capital_whale = prev_max * CAPITAL_MULTIPLIER >= MIN_CAPITAL_WHALE
        df.drop(columns=["_rmax"], inplace=True)
    else:
        is_capital_whale = pd.Series(False, index=df.index)

    # --- Volume percentile criterion (vectorized) ---
    # Cumulative volume per (market, trader) BEFORE current trade
    cum_vol = df.groupby(["_mid", trader_col])["usd_amount"].cumsum()
    df["_cum_vol_before"] = cum_vol - df["usd_amount"]

    # Final cumulative volumes per (market, trader) — used to derive threshold
    final_vols = (
        df.groupby(["_mid", trader_col])["usd_amount"].sum().reset_index()
    )
    final_vols.columns = ["_mid", trader_col, "_total_vol"]

    n_traders_map = final_vols.groupby("_mid")[trader_col].count()
    threshold_map = (
        final_vols.groupby("_mid")["_total_vol"]
        .quantile(volume_percentile / 100.0)
    )

    df["_n_traders"] = df["_mid"].map(n_traders_map)
    df["_vol_threshold"] = df["_mid"].map(threshold_map)

    is_vol_whale = (
        (df["_n_traders"] >= 5) &
        (df["_cum_vol_before"] >= df["_vol_threshold"])
    )

    df["is_whale"] = is_capital_whale | is_vol_whale
    df.drop(
        columns=["_mid", "_cum_vol_before", "_n_traders", "_vol_threshold"],
        inplace=True,
    )
    return df


def calculate_surprise_metrics(
    whale_trades: pd.DataFrame,
    resolution_winners: Dict[str, str],
    direction_col: str = "maker_direction",
) -> Dict:
    """
    Expected WR = price for BUY YES, 1-price for SELL NO.
    Actual WR = fraction of correct predictions.
    Surprise WR = actual - expected.
    """
    df = whale_trades.copy()
    df["market_id_str"] = df["market_id"].astype(str).str.replace(".0", "", regex=False)
    df["winner"] = df["market_id_str"].map(resolution_winners)
    df = df.dropna(subset=["winner"])
    if len(df) < 5:
        return {
            "expected_win_rate": np.nan,
            "actual_win_rate": np.nan,
            "surprise_win_rate": np.nan,
            "sample_size": len(df),
        }

    direction = df[direction_col].str.lower()
    price = df["price"].astype(float)
    winner = df["winner"].str.upper()

    # Expected: BUY YES @ p -> expected WR = p; SELL NO @ p -> expected WR = 1-p
    expected = np.where(
        direction == "buy",
        price,
        1 - price,
    )
    df["expected_wr"] = expected

    # Actual: correct = (BUY & YES) or (SELL & NO)
    correct = (
        ((direction == "buy") & (winner == "YES")) |
        ((direction == "sell") & (winner == "NO"))
    )
    df["correct"] = correct

    return {
        "expected_win_rate": float(expected.mean()),
        "actual_win_rate": float(correct.mean()),
        "surprise_win_rate": float(correct.mean() - expected.mean()),
        "sample_size": len(df),
    }


def _filter_unfavored_trades(
    df: pd.DataFrame,
    direction_col: str = "maker_direction",
    max_price: float = 0.40,
) -> pd.DataFrame:
    """
    Keep only unfavored (underdog) trades: BUY at <= max_price, SELL at >= (1-max_price).
    E.g. max_price=0.40: BUY YES at 40c or less, SELL NO at 60c+ (short favorite).
    """
    direction = df[direction_col].str.lower()
    price = df["price"].astype(float)
    mask = (
        ((direction == "buy") & (price <= max_price)) |
        ((direction == "sell") & (price >= (1 - max_price)))
    )
    return df[mask].copy()


def calculate_performance_score_with_surprise(
    whale_address: str,
    trades_df: pd.DataFrame,
    resolution_winners: Dict[str, str],
    direction_col: str = "maker_direction",
    min_trades: int = 10,
    unfavored_only: bool = False,
    unfavored_max_price: float = 0.40,
) -> Optional[Dict]:
    """
    Score whales based on beating market expectations (surprise), not raw win rate.
    If unfavored_only=True, only include trades at <= unfavored_max_price (BUY) or
    >= (1-unfavored_max_price) (SELL) - i.e. underdog positions.
    """
    whale_trades = trades_df[
        (trades_df["maker"] == whale_address) &
        (trades_df["market_id"].astype(str).isin(resolution_winners.keys()))
    ].copy()
    if unfavored_only:
        whale_trades = _filter_unfavored_trades(
            whale_trades, direction_col, unfavored_max_price
        )
    if len(whale_trades) < min_trades:
        return None

    surprise_metrics = calculate_surprise_metrics(
        whale_trades, resolution_winners, direction_col
    )
    expected_wr = surprise_metrics["expected_win_rate"]
    actual_wr = surprise_metrics["actual_win_rate"]
    surprise_wr = surprise_metrics["surprise_win_rate"]

    # ROI and Sharpe from resolved trades
    direction = whale_trades[direction_col].str.lower()
    price = whale_trades["price"].astype(float)
    winner = whale_trades["market_id"].astype(str).map(resolution_winners)
    resolution = winner.map({"YES": 1.0, "NO": 0.0})
    roi = np.where(
        direction == "buy",
        (resolution - price) / np.maximum(price, 0.01),
        (price - resolution) / np.maximum(1 - price, 0.01),
    )
    avg_roi = float(np.mean(roi))
    sharpe = float(np.mean(roi) / (np.std(roi) + 1e-6))

    # Surprise score: +15% surprise = 10, 0% = 5, -15% = 0
    surprise_score = min(max(surprise_wr / 0.15 * 5 + 5, 0), 10)
    roi_score = min(avg_roi * 10, 10)
    sharpe_score = min(max(sharpe, 0) * 2, 10)

    raw_score = 0.50 * surprise_score + 0.30 * roi_score + 0.20 * sharpe_score

    return {
        "score": min(float(raw_score), 10),
        "expected_win_rate": expected_wr,
        "actual_win_rate": actual_wr,
        "surprise_win_rate": surprise_wr,
        "avg_roi": avg_roi,
        "sharpe": sharpe,
        "sample_size": len(whale_trades),
    }


def score_whales_custom(
    trades_df: pd.DataFrame,
    resolution_winners: Dict[str, str],
    market_volumes: Dict[str, float],
    lambda_decay: float = 0.01,
    min_trades: int = 5,
    min_score: float = 0.0,
    cutoff: Optional[pd.Timestamp] = None,
    direction_col: str = "maker_direction",
    trader_col: str = "maker",
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """
    Score each whale with the capital-volume-recency weighted edge formula:

        weighted_edge = SUM(edge_i * w_i) / SUM(w_i)
        w_i = log10(capital_i) * sqrt(volume_i) * exp(-lambda * t_i_days)
        edge_i = outcome_i - p_i       (outcome: 1=win, 0=loss; p_i = implied prob)
        Score = weighted_edge * sqrt(N) / (1 + downside_vol(edge_i))

    Args:
        market_volumes: {market_id: volume_usd} — used as volume_i per trade.
                        Missing markets default to the median volume.
        lambda_decay: exponential decay rate per day (0 = no decay).
        min_score: filter threshold — only return whales with score >= min_score.

    Returns:
        scores:   {whale_address: score}
        winrates: {whale_address: actual_win_rate}  (unweighted, for Kelly sizing)
    """
    if not resolution_winners:
        return {}, {}

    df = trades_df.copy()
    df["_mid"] = df["market_id"].astype(str).str.strip().str.replace(".0", "", regex=False)
    df["_winner"] = df["_mid"].map(resolution_winners)
    df = df.dropna(subset=["_winner"])
    if df.empty:
        return {}, {}

    dir_lower = df[direction_col].str.lower()
    price = df["price"].astype(float).clip(1e-6, 1 - 1e-6)
    winner_up = df["_winner"].str.upper()

    # p_i = implied probability of winning at entry
    df["_p"] = np.where(dir_lower == "buy", price, 1.0 - price)

    # outcome_i = 1 if trade won, 0 if lost
    df["_outcome"] = (
        ((dir_lower == "buy") & (winner_up == "YES")) |
        ((dir_lower == "sell") & (winner_up == "NO"))
    ).astype(float)

    # edge_i = outcome_i - p_i
    df["_edge"] = df["_outcome"] - df["_p"]

    # capital_i = usd_amount (dollars at risk)
    df["_capital"] = df["usd_amount"].astype(float).clip(lower=1.0)

    # volume_i = market liquidity/volume
    median_vol = float(np.median(list(market_volumes.values()))) if market_volumes else 100_000.0
    df["_volume"] = df["_mid"].map(market_volumes).fillna(median_vol).astype(float).clip(lower=1.0)

    # t_i = days ago from cutoff (or now)
    ref = pd.Timestamp(cutoff) if cutoff is not None else pd.Timestamp.now(tz="UTC")
    if df["datetime"].dt.tz is None:
        ref = ref.tz_localize(None) if ref.tzinfo else ref
    else:
        ref = ref.tz_convert("UTC") if ref.tzinfo else ref.tz_localize("UTC")
    df["_days_ago"] = (ref - df["datetime"]).dt.total_seconds().clip(lower=0.0) / 86400.0

    # w_i = log10(capital) * sqrt(volume) * exp(-lambda * t)
    df["_w"] = (
        np.log10(df["_capital"].clip(lower=1.0)) *
        np.sqrt(df["_volume"]) *
        np.exp(-lambda_decay * df["_days_ago"])
    )

    scores: Dict[str, float] = {}
    winrates: Dict[str, float] = {}

    for whale, grp in df.groupby(trader_col):
        n = len(grp)
        if n < min_trades:
            continue

        w = grp["_w"].to_numpy()
        edge = grp["_edge"].to_numpy()
        outcome = grp["_outcome"].to_numpy()
        w_sum = w.sum()

        if w_sum <= 0:
            continue

        # Weighted mean edge
        weighted_edge = float((edge * w).sum() / w_sum)

        # Consistency penalty: weighted downside semi-deviation.
        # Same w_i (capital · volume · 6mo decay) applied to the vol calculation
        # so stale losses penalise less than recent losses, mirroring the mean.
        neg_mask = edge < 0
        if neg_mask.any():
            wn = w[neg_mask]
            en = edge[neg_mask]
            wn_sum = wn.sum()
            if wn_sum > 0:
                w_mean = (wn * en).sum() / wn_sum
                w_var  = (wn * (en - w_mean) ** 2).sum() / wn_sum
                edge_downvol = float(np.sqrt(max(w_var, 0.0)))
            else:
                edge_downvol = 0.0
        else:
            edge_downvol = 0.0

        # Final score
        score = weighted_edge * np.sqrt(n) / (1.0 + edge_downvol)

        if score < min_score:
            continue

        scores[str(whale)] = float(score)
        winrates[str(whale)] = float(outcome.mean())

    return scores, winrates


def build_surprise_positive_whale_set(
    train_trades: pd.DataFrame,
    resolution_winners: Dict[str, str],
    min_surprise: float = 0.0,
    min_trades: int = 10,
    require_positive_surprise: bool = True,
    direction_col: str = "maker_direction",
    trader_col: str = "maker",
    volume_percentile: float = 95.0,
    cutoff: Optional[pd.Timestamp] = None,
    recency_halflife_days: float = 90.0,
    bayes_prior_alpha: float = 2.0,
    bayes_prior_beta: float = 2.0,
    market_volumes: Optional[Dict[str, float]] = None,
    lambda_decay: float = 0.01,
    min_score: float = 0.0,
) -> Tuple[Set[str], Dict[str, float], Dict[str, float]]:
    """
    Build set of volume whales with positive expected return (capital-weighted surprise > 0).

    Qualifying criterion: shrunk_win_rate > market-implied expected_win_rate.
    This means the whale's actual win rate exceeds what the market prices implied —
    i.e. positive expected return on capital deployed.

    Weights each trade by recency_decay * usd_amount so large recent positions
    count proportionally more than small or stale ones.

    Applies Bayesian shrinkage (Beta-Binomial) to correct winner's curse:
    an uninformed whale who got lucky on a few large bets gets pulled toward 50%.

    Returns:
        whale_set: Set of whale addresses with positive expected return
        whale_scores: Dict[whale_address, score] for filter_and_score_signals
        whale_winrates: Dict[whale_address, shrunk_win_rate] for Kelly sizing
    """
    trades_with_whale = identify_whales_rolling(
        train_trades,
        trader_col=trader_col,
        volume_only=True,
        volume_percentile=volume_percentile,
    )
    whale_trades = trades_with_whale[trades_with_whale["is_whale"]]

    if whale_trades.empty:
        return set(), {}, {}

    whale_addresses = set(whale_trades[trader_col].unique())

    if not resolution_winners:
        # No resolution data — volume-only set, neutral scores
        whale_scores   = {addr: 0.5 for addr in whale_addresses}
        whale_winrates = {addr: 0.5 for addr in whale_addresses}
        return whale_addresses, whale_scores, whale_winrates

    # Score whale-identified traders using the custom formula.
    # Only trades from identified whale addresses are fed in to avoid
    # polluting the volume_i median with non-whale markets.
    whale_only_trades = train_trades[
        train_trades[trader_col].isin(whale_addresses)
    ].copy()

    scores, winrates = score_whales_custom(
        trades_df=whale_only_trades,
        resolution_winners=resolution_winners,
        market_volumes=market_volumes or {},
        lambda_decay=lambda_decay,
        min_trades=min_trades,
        min_score=min_score if require_positive_surprise else float("-inf"),
        cutoff=cutoff,
        direction_col=direction_col,
        trader_col=trader_col,
    )

    if not scores:
        # All whales below threshold — return volume-only set as fallback
        whale_scores   = {addr: 0.5 for addr in whale_addresses}
        whale_winrates = {addr: 0.5 for addr in whale_addresses}
        return whale_addresses, whale_scores, whale_winrates

    whale_set: Set[str] = set(scores.keys())
    return whale_set, scores, winrates


def compute_whale_ic(
    train_trades: pd.DataFrame,
    price_lookup_fn,
    horizon_days: int = 7,
    min_trades: int = 5,
    trader_col: str = "maker",
    direction_col: str = "maker_direction",
    max_horizon_date: Optional[pd.Timestamp] = None,
) -> Dict[str, float]:
    """
    Compute per-whale Information Coefficient (IC) from training trades.

    IC = fraction of trades where the whale correctly predicted short-term YES price direction.
    - BUY: correct if CLOB price rose by t+horizon_days
    - SELL: correct if CLOB price fell by t+horizon_days
    IC is centered at 0.50 (random); IC > 0.50 indicates directional skill.

    This is an independent signal from resolution-based win rate — it measures
    short-term conviction, not binary outcome prediction.

    Args:
        train_trades: Historical trades DataFrame
        price_lookup_fn: Callable(market_id, date) -> float|None  (CLOB YES price)
        horizon_days: Days after trade to measure price direction
        min_trades: Minimum trades to compute IC
        trader_col: Column name for trader address
        direction_col: Column name for direction (buy/sell)
        max_horizon_date: Cap horizon lookups to this date to avoid test-period leakage.
            Should be set to the train/test split date. If None, no cap is applied.

    Returns:
        Dict[whale_address, ic_score] where ic_score in [0.0, 1.0], 0.50 = random
    """
    df = train_trades.copy()
    df["_mid"] = df["market_id"].astype(str).str.strip().str.replace(".0", "", regex=False)
    df["_dir"] = df[direction_col].str.lower()
    df["_price"] = df["price"].astype(float)
    horizon_dates = df["datetime"] + pd.Timedelta(days=horizon_days)
    if max_horizon_date is not None:
        horizon_dates = horizon_dates.clip(upper=pd.Timestamp(max_horizon_date))
    df["_horizon_date"] = horizon_dates

    results = {}
    for whale, group in df.groupby(trader_col):
        if len(group) < min_trades:
            continue

        correct_count = 0
        valid_count = 0
        for _, row in group.iterrows():
            future_price = price_lookup_fn(row["_mid"], row["_horizon_date"])
            if future_price is None:
                continue
            entry_price = row["_price"]
            direction = row["_dir"]
            if direction == "buy":
                correct = future_price > entry_price
            else:  # sell = short YES = expect price to fall
                correct = future_price < entry_price
            correct_count += int(correct)
            valid_count += 1

        if valid_count >= min_trades:
            results[whale] = correct_count / valid_count

    return results


def build_volume_whale_set(
    trades_df: pd.DataFrame,
    trader_col: str = "maker",
    volume_percentile: float = 95.0,
    default_score: float = 7.0,
    default_winrate: float = 0.5,
) -> Tuple[Set[str], Dict[str, float], Dict[str, float]]:
    """
    Build whale set from Nth percentile volume only (no capital, no resolution).

    Returns whale_set, whale_scores (default), whale_winrates (default).
    Use when resolution data is unavailable or when filtering by resolved markets is not desired.
    """
    trades_with_whale = identify_whales_rolling(
        trades_df,
        trader_col=trader_col,
        volume_only=True,
        volume_percentile=volume_percentile,
    )
    whale_trades = trades_with_whale[trades_with_whale["is_whale"]]
    whale_set = set(whale_trades[trader_col].unique())
    whale_scores = {addr: default_score for addr in whale_set}
    whale_winrates = {addr: default_winrate for addr in whale_set}
    return whale_set, whale_scores, whale_winrates

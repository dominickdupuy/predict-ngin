"""
Shared whale detection configuration for backtest and live opportunity finding.

Change whale definition here; both backtest and find_whale_opportunities use it.

Whale modes:
- default: identify_polymarket_whales (mid_price_accuracy or volume_top10)
- volume_only: 95th percentile volume in market (rolling)
- surprise_only: volume whales with positive surprise (requires resolutions)
- unfavored_only: filter to underdog trades (BUY <=40c, SELL >=60c)
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class WhaleConfig:
    """Whale detection parameters shared by backtest and live scanner."""

    # Mode: "default" | "volume_only" | "surprise_only"
    mode: str = "volume_only"

    # Volume-only: 95th percentile in market
    volume_percentile: float = 95.0

    # Unfavored filter: only underdog trades
    unfavored_only: bool = False
    unfavored_max_price: float = 0.40

    # Surprise-only: min surprise to qualify (requires resolutions)
    min_surprise: float = 0.0
    min_trades_for_surprise: int = 10

    # Performance filter: require positive expected return (actual WR > market-implied WR).
    # Capital-weighted surprise: large trades count more than small trades.
    require_positive_surprise: bool = True

    # Default scoring when no resolution
    default_whale_score: float = 7.0
    default_whale_winrate: float = 0.5

    # Min trade size (USD) to count
    min_usd: float = 100.0

    # For default mode: identify_polymarket_whales params
    min_trades: int = 10
    min_volume: float = 1000.0

    # Entry price upper bound: exclude near-resolved markets where YES > 0.98.
    # At YES > 0.98 the implied NO payout is under $0.02 per dollar — economically
    # unviable regardless of whale conviction.
    # NOT calibrated to backtest PnL; derived from the dust-exclusion principle.
    max_entry_yes_price: float = 0.98

    # Multi-whale confirmation gate (1 = disabled)
    min_confirmation_whales: int = 1
    confirmation_window_days: int = 7

    # Max calendar days to hold before force-close at CLOB (0 = off).
    # Portfolio management preference — not derived from signal logic.
    max_hold_days: int = 0

    # Bayesian shrinkage: Beta-Binomial prior (α, β). Prior mean = α/(α+β) = 0.50.
    # With α=β=2, effective prior sample size = 4; shrinks toward 50%.
    # Corrects winner's curse: a true-50% whale passes 8/10 resolved-trade bar with 17% probability.
    bayes_prior_alpha: float = 4.0
    bayes_prior_beta: float = 4.0

    # Recency decay: exponential half-life in days for weighting training-period trades.
    # 90 days ≈ one quarter — stable enough for statistics, short enough to adapt.
    # Set to 0 to disable (uniform weighting).
    recency_halflife_days: float = 90.0

    # Information Coefficient: price-direction accuracy at t+horizon_days.
    # Measures whether whale entry predicts short-term CLOB price movement (not resolution).
    # IC is an independent signal from surprise WR; blended as ic_score_weight * IC.
    ic_horizon_days: int = 7
    ic_min_trades: int = 5
    ic_score_weight: float = 0.20

    # Scheduled TTR filter: skip entry when scheduled close date is fewer than N days away.
    # Uses endDateIso/endDate (published schedule, known at trade time), NOT closedTime.
    # Set to 0 to disable.
    min_ttr_entry_days: int = 0

    # Partial exit: when unrealized gain >= threshold, close fraction of the position.
    # Locks in profit on large winners and protects against mean-reversion.
    # Threshold and fraction are economically motivated, not calibrated to backtest PnL.
    partial_exit_gain_threshold: float = 0.40
    partial_exit_fraction: float = 0.50

    @property
    def volume_only(self) -> bool:
        return self.mode == "volume_only"

    @property
    def surprise_only(self) -> bool:
        return self.mode == "surprise_only"

    def __repr__(self) -> str:
        parts = [f"mode={self.mode}"]
        if self.require_positive_surprise:
            parts.append("positive_surprise=True")
        if self.unfavored_only:
            parts.append(f"unfavored<={self.unfavored_max_price}")
        return f"WhaleConfig({', '.join(parts)})"


def load_whale_config(config_path: Optional[Path] = None) -> WhaleConfig:
    """Load WhaleConfig from YAML (default + local merge), with defaults."""
    cfg = WhaleConfig()
    root = Path(__file__).resolve().parents[2]
    default_path = root / "config" / "default.yaml"
    local_path = root / "config" / "local.yaml"

    data = {}
    if default_path.exists():
        try:
            with open(default_path) as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            pass
    if local_path.exists():
        try:
            with open(local_path) as f:
                local = yaml.safe_load(f) or {}
            if isinstance(local, dict) and isinstance(data, dict):
                for k, v in local.items():
                    if k in data and isinstance(data[k], dict) and isinstance(v, dict):
                        data[k] = {**data[k], **v}
                    else:
                        data[k] = v
        except Exception:
            pass

    ws = data.get("whale_strategy") or {}
    if isinstance(ws, dict):
        if "whale_mode" in ws:
            cfg.mode = str(ws["whale_mode"])
        if "volume_percentile" in ws:
            cfg.volume_percentile = float(ws["volume_percentile"])
        if "unfavored_only" in ws:
            cfg.unfavored_only = bool(ws["unfavored_only"])
        if "unfavored_max_price" in ws:
            cfg.unfavored_max_price = float(ws["unfavored_max_price"])
        if "min_trades" in ws:
            cfg.min_trades = int(ws["min_trades"])
        if "min_volume" in ws:
            cfg.min_volume = float(ws["min_volume"])
        if "max_entry_yes_price" in ws:
            cfg.max_entry_yes_price = float(ws["max_entry_yes_price"])
        if "min_confirmation_whales" in ws:
            cfg.min_confirmation_whales = int(ws["min_confirmation_whales"])
        if "confirmation_window_days" in ws:
            cfg.confirmation_window_days = int(ws["confirmation_window_days"])
        if "max_hold_days" in ws:
            cfg.max_hold_days = int(ws["max_hold_days"])
        if "min_usd" in ws:
            cfg.min_usd = float(ws["min_usd"])
        if "require_positive_surprise" in ws:
            cfg.require_positive_surprise = bool(ws["require_positive_surprise"])
        if "bayes_prior_alpha" in ws:
            cfg.bayes_prior_alpha = float(ws["bayes_prior_alpha"])
        if "bayes_prior_beta" in ws:
            cfg.bayes_prior_beta = float(ws["bayes_prior_beta"])
        if "recency_halflife_days" in ws:
            cfg.recency_halflife_days = float(ws["recency_halflife_days"])
        if "ic_horizon_days" in ws:
            cfg.ic_horizon_days = int(ws["ic_horizon_days"])
        if "ic_min_trades" in ws:
            cfg.ic_min_trades = int(ws["ic_min_trades"])
        if "ic_score_weight" in ws:
            cfg.ic_score_weight = float(ws["ic_score_weight"])
        if "min_ttr_entry_days" in ws:
            cfg.min_ttr_entry_days = int(ws["min_ttr_entry_days"])
        if "partial_exit_gain_threshold" in ws:
            cfg.partial_exit_gain_threshold = float(ws["partial_exit_gain_threshold"])
        if "partial_exit_fraction" in ws:
            cfg.partial_exit_fraction = float(ws["partial_exit_fraction"])

    return cfg


# Default instance
DEFAULT_WHALE_CONFIG = WhaleConfig()

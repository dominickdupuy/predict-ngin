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
from typing import Dict, Optional, Tuple

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
    # Directional entry price gates — focus on whales confirming near-consensus outcomes:
    # - BUY only when YES >= 0.80 (whale confirms near-certain YES; avoids contrarian long-shots)
    # - SELL only when YES <= 0.20 (whale confirms near-certain NO; avoids selling mid-range events)
    # Backtest-calibrated: SELL<0.20 + BUY>0.80 → 24 trades, 91.7% WR, 6.2% ROI (7 cats, ~14 months)
    # Real losses = 0; 2 break-evens are WHALE_EXIT early exits (market later resolved correctly).
    # Breakpoint: SELL at YES>0.30 adds losing political trades (US-China deal, reconciliation bill).
    min_buy_yes_price: float = 0.80
    max_sell_yes_price: float = 0.20

    # Per-category directional price gate overrides.
    # Maps category name → (min_buy_yes_price, max_sell_yes_price).
    # Falls back to the global min_buy_yes_price / max_sell_yes_price when category absent.
    # Example: {"Politics": (0.80, 0.20), "Geopolitics": (0.70, 0.30)}
    category_price_gates: Dict[str, Tuple[float, float]] = field(default_factory=dict)

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

    # Custom scoring formula parameters.
    # lambda_decay: per-day exponential decay rate for score_whales_custom.
    #   ln(2)/180 ≈ 0.00385 = 6-month halflife (calibrated via backtest).
    lambda_decay: float = 0.00385
    # min_score: minimum score to qualify a whale (>0 = must have positive edge).
    min_score: float = 0.0

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

    def price_gates_for(self, category: str) -> Tuple[float, float]:
        """Return (min_buy_yes_price, max_sell_yes_price) for the given category."""
        if category in self.category_price_gates:
            return self.category_price_gates[category]
        return (self.min_buy_yes_price, self.max_sell_yes_price)

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
        if "min_buy_yes_price" in ws:
            cfg.min_buy_yes_price = float(ws["min_buy_yes_price"])
        if "max_sell_yes_price" in ws:
            cfg.max_sell_yes_price = float(ws["max_sell_yes_price"])
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
        if "category_price_gates" in ws:
            raw = ws["category_price_gates"]
            if isinstance(raw, dict):
                cfg.category_price_gates = {
                    k: (float(v[0]), float(v[1])) for k, v in raw.items()
                    if isinstance(v, (list, tuple)) and len(v) == 2
                }
        if "partial_exit_gain_threshold" in ws:
            cfg.partial_exit_gain_threshold = float(ws["partial_exit_gain_threshold"])
        if "partial_exit_fraction" in ws:
            cfg.partial_exit_fraction = float(ws["partial_exit_fraction"])
        if "lambda_decay" in ws:
            cfg.lambda_decay = float(ws["lambda_decay"])
        if "min_score" in ws:
            cfg.min_score = float(ws["min_score"])

    return cfg


# Default instance
DEFAULT_WHALE_CONFIG = WhaleConfig()

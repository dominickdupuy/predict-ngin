"""
Parameter sensitivity + deflated Sharpe (p-hacking correction).

Two jobs:

1. Enumerate every combination in a strategy's `param_grid`, run the backtest
   once per combo, and report the full metrics table. This is the raw material
   for judging whether a strategy is fragile — Sharpe that only shows up on
   one grid point is overfit.

2. Deflate the best observed Sharpe by the number of trials. When you test N
   configurations and pick the best, the winner's Sharpe is upward-biased.
   Bailey & López de Prado (2014) give a closed-form correction that folds
   in trial-count, Sharpe-variance across trials, and the higher moments
   (skew/kurtosis) of the return distribution.

Reference: Bailey, López de Prado — "The Deflated Sharpe Ratio" (2014);
           López de Prado — "Advances in Financial Machine Learning" (2018), ch. 8.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..data.loader import PITDataLoader
from ..strategies.base import StrategyParams, V3Strategy
from .engine import BacktestEngine, BacktestResult, EngineConfig


# ------------------------------------------------------------------ Deflated SR

_EULER_MASCHERONI = 0.5772156649015329


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _phi_inv(p: float) -> float:
    """
    Inverse standard normal CDF via Beasley-Springer-Moro approximation.
    Accurate to ~1e-9 on the central mass; good enough for SR deflation.
    """
    if not (0.0 < p < 1.0):
        raise ValueError(f"phi_inv requires p in (0,1), got {p}")
    # Constants
    a = [-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00]
    p_low = 0.02425
    p_high = 1.0 - p_low
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / \
               (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1.0)
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
             ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)


def _sr_cutoff(n_trials: int, sr_variance: float) -> float:
    """
    Expected max of N iid Sharpe trials under the null SR=0.
    Bailey-López de Prado (2014), eq. 6.

        E[max SR] ≈ sqrt(Var[SR]) * [ (1-γ)·Φ⁻¹(1 - 1/N)
                                    + γ·Φ⁻¹(1 - 1/(N·e)) ]
    """
    if n_trials <= 1 or sr_variance <= 0:
        return 0.0
    e = math.e
    gamma = _EULER_MASCHERONI
    p1 = 1.0 - 1.0 / n_trials
    p2 = 1.0 - 1.0 / (n_trials * e)
    # Clamp to avoid edge blowups for tiny N
    p1 = min(max(p1, 1e-9), 1.0 - 1e-9)
    p2 = min(max(p2, 1e-9), 1.0 - 1e-9)
    term = (1.0 - gamma) * _phi_inv(p1) + gamma * _phi_inv(p2)
    return math.sqrt(sr_variance) * term


def deflated_sharpe(
    observed_sharpe: float,
    n_trials: int,
    sr_variance: float,
    n_returns: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> Dict[str, float]:
    """
    Deflated Sharpe Ratio (Bailey-López de Prado 2014).

    Parameters
    ----------
    observed_sharpe : annualised Sharpe of the *selected* (best) trial.
    n_trials        : number of configurations enumerated.
    sr_variance     : variance of annualised Sharpes across trials.
    n_returns       : number of return observations used to compute each Sharpe.
    skew, kurtosis  : higher moments of the selected strategy's returns.

    Returns
    -------
    dict with:
      cutoff_sr : expected max SR under null given N trials
      dsr       : deflated Sharpe (probability the true SR > 0 after correction)
      z         : the argument to Φ in the DSR formula
    """
    if n_returns <= 1:
        return {"cutoff_sr": 0.0, "dsr": 0.0, "z": 0.0}
    cutoff = _sr_cutoff(n_trials, sr_variance)
    gamma3 = float(skew)
    gamma4 = float(kurtosis)
    denom_sq = 1.0 - gamma3 * observed_sharpe + (gamma4 - 1.0) / 4.0 * (observed_sharpe ** 2)
    if denom_sq <= 0:
        denom_sq = 1e-9
    z = (observed_sharpe - cutoff) * math.sqrt(max(n_returns - 1, 1)) / math.sqrt(denom_sq)
    return {"cutoff_sr": float(cutoff), "dsr": float(_phi(z)), "z": float(z)}


# ------------------------------------------------------------------ Parameter sweep

StrategyFactory = Callable[[PITDataLoader, StrategyParams], V3Strategy]


@dataclass
class SweepRow:
    param_hash: str
    params: Dict[str, Any]
    metrics: Dict[str, float]
    daily_pnl: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))


@dataclass
class ParameterSweepResult:
    rows: List[SweepRow]
    table: pd.DataFrame                     # one row per config, columns = metrics
    best_row: Optional[SweepRow]
    deflated: Dict[str, float]              # DSR for best config

    def summary(self) -> str:
        if self.table.empty:
            return "=== ParameterSweep: no trials produced results ==="
        out = ["=== ParameterSweep ===",
               f"n_trials: {len(self.table)}",
               f"best sharpe: {self.best_row.metrics.get('sharpe', 0):.3f}"
               f"  (params: {self.best_row.params})",
               "Deflated:"]
        for k, v in self.deflated.items():
            out.append(f"  {k}: {v:.4f}")
        cols = [c for c in ["sharpe", "sortino", "total_pnl_usd", "n_trades",
                            "hit_rate", "max_drawdown_usd"] if c in self.table.columns]
        out.append("")
        out.append(self.table[cols + [c for c in self.table.columns if c.startswith("param_")]]
                   .sort_values("sharpe", ascending=False).round(4).to_string(index=False))
        return "\n".join(out)


class ParameterSweep:
    """
    Run a backtest across every combination in `strategy_cls.param_grid` and
    report per-config metrics plus deflated Sharpe for the best config.

    Design choices
    --------------
    - One strategy class per sweep. If you want to sweep across multiple
      strategies, run them separately — their param grids aren't comparable.
    - The engine is rebuilt per trial so state never leaks. The PITDataLoader
      is shared (it's read-only).
    - We deflate the *best* Sharpe by the count of configurations tried, using
      the empirical variance of Sharpes across trials. This is the standard
      multi-testing correction for the "pick the best grid point" workflow.
    """

    def __init__(
        self,
        loader: PITDataLoader,
        strategy_cls: type,
        base_config: EngineConfig,
        strategy_factory: Optional[StrategyFactory] = None,
        extra_strategies: Optional[Sequence[V3Strategy]] = None,
    ):
        self.loader = loader
        self.strategy_cls = strategy_cls
        self.base_config = base_config
        self.extra_strategies = list(extra_strategies) if extra_strategies else []
        self.strategy_factory = strategy_factory or self._default_factory

    @staticmethod
    def _default_factory(loader: PITDataLoader, params: StrategyParams) -> V3Strategy:
        raise NotImplementedError(
            "ParameterSweep requires a strategy_factory(loader, params) callable "
            "because strategy constructors vary in signature."
        )

    def run(self) -> ParameterSweepResult:
        configs = self.strategy_cls.sweep_configs()
        if not configs:
            return ParameterSweepResult(rows=[], table=pd.DataFrame(),
                                        best_row=None, deflated={})

        rows: List[SweepRow] = []
        for i, cfg_values in enumerate(configs):
            params = StrategyParams(name=self.strategy_cls.name, values=dict(cfg_values))
            strat = self.strategy_factory(self.loader, params)
            engine_cfg = self._clone_cfg_with_label(self.base_config, f"sweep{i}")
            engine = BacktestEngine(self.loader, [strat, *self.extra_strategies], engine_cfg)
            res = engine.run()
            param_hash = _hash_params(cfg_values)
            rows.append(SweepRow(
                param_hash=param_hash,
                params=dict(cfg_values),
                metrics=dict(res.metrics),
                daily_pnl=res.daily_pnl.copy(),
            ))

        table = _build_sweep_table(rows)
        best_row = _pick_best(rows)
        deflated = {}
        if best_row is not None and len(rows) > 1:
            sharpes = np.array([r.metrics.get("sharpe", 0.0) for r in rows], dtype=float)
            sr_var = float(sharpes.var(ddof=1))
            n_returns = max(len(best_row.daily_pnl), 1)
            skew, kurt = _moments(best_row.daily_pnl)
            deflated = deflated_sharpe(
                observed_sharpe=best_row.metrics.get("sharpe", 0.0),
                n_trials=len(rows),
                sr_variance=sr_var,
                n_returns=n_returns,
                skew=skew,
                kurtosis=kurt,
            )
            deflated["n_trials"] = float(len(rows))
            deflated["sr_variance_across_trials"] = sr_var

        return ParameterSweepResult(rows=rows, table=table,
                                    best_row=best_row, deflated=deflated)

    @staticmethod
    def _clone_cfg_with_label(cfg: EngineConfig, label_suffix: str) -> EngineConfig:
        return EngineConfig(
            start_s=cfg.start_s, end_s=cfg.end_s,
            step_s=cfg.step_s,
            liquidity_threshold_usd=cfg.liquidity_threshold_usd,
            liquidity_lookback_s=cfg.liquidity_lookback_s,
            capital_scale=cfg.capital_scale,
            executor_level_fill_fraction=cfg.executor_level_fill_fraction,
            executor_max_depth_fraction=cfg.executor_max_depth_fraction,
            taker_fee_bps=cfg.taker_fee_bps,
            reconstructor_kwargs=dict(cfg.reconstructor_kwargs),
            label=f"{cfg.label}_{label_suffix}",
            max_open_per_market=cfg.max_open_per_market,
        )


# ------------------------------------------------------------------ helpers

def _hash_params(values: Dict[str, Any]) -> str:
    # Deterministic short hash: sorted key=val joined
    parts = [f"{k}={values[k]}" for k in sorted(values)]
    s = ";".join(parts)
    # shortened fingerprint — readable in tables, not collision-free across
    # wildly different keyspaces, but fine within a single sweep
    return f"{abs(hash(s)) % (10**10):010d}"


def _build_sweep_table(rows: List[SweepRow]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    recs = []
    for r in rows:
        rec = {"param_hash": r.param_hash}
        for k, v in r.params.items():
            rec[f"param_{k}"] = v
        rec.update(r.metrics)
        recs.append(rec)
    return pd.DataFrame(recs)


def _pick_best(rows: List[SweepRow]) -> Optional[SweepRow]:
    if not rows:
        return None
    # Filter: require at least 5 trades so Sharpe is not a degenerate artefact
    eligible = [r for r in rows if r.metrics.get("n_trades", 0) >= 5]
    pool = eligible if eligible else rows
    return max(pool, key=lambda r: r.metrics.get("sharpe", -1e9))


def _moments(daily: pd.Series) -> tuple:
    if daily.empty or len(daily) < 3:
        return 0.0, 3.0
    x = daily.to_numpy(dtype=float)
    mu = x.mean()
    sd = x.std(ddof=1)
    if sd <= 0:
        return 0.0, 3.0
    z = (x - mu) / sd
    skew = float((z ** 3).mean())
    kurt = float((z ** 4).mean())
    return skew, kurt

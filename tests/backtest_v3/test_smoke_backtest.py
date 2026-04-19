"""
End-to-end smoke test on real Polymarket data.

Runs a short backtest window with one of the V3 strategies on one category,
asserting only that the pipeline completes end-to-end (data load → universe
selection → signal emission → CLOB reconstruction → book execution →
trade bookkeeping → metrics). Marked as a slow integration test; skipped if
the data directory is absent.

We deliberately do *not* assert on PnL sign or Sharpe — a smoke test is
about wiring, not performance.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from backtest_v3.backtest.engine import BacktestEngine, EngineConfig
from backtest_v3.backtest.walk_forward import WalkForward
from backtest_v3.data.loader import PITDataLoader
from backtest_v3.strategies.calendar_butterfly import CalendarButterfly
from backtest_v3.strategies.round_price_lp import RoundPriceLP


DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "research"


pytestmark = pytest.mark.skipif(
    not (DATA_ROOT / "Politics" / "trades.parquet").exists(),
    reason="Polymarket research data not available on this machine",
)


def _short_window_config() -> EngineConfig:
    # 2026-01-15..2026-02-14, single-category smoke window (30 days, 6h step)
    return EngineConfig(
        start_s=int(pd.Timestamp("2026-01-15", tz="UTC").timestamp()),
        end_s=int(pd.Timestamp("2026-02-14", tz="UTC").timestamp()),
        step_s=6 * 3600,
        liquidity_threshold_usd=300_000.0,
        liquidity_lookback_s=30 * 24 * 3600,
        capital_scale=1.0,
        label="smoke",
    )


def test_smoke_backtest_runs_end_to_end() -> None:
    loader = PITDataLoader(DATA_ROOT, categories=("Politics",))
    cfg = _short_window_config()
    strat = CalendarButterfly(loader)
    engine = BacktestEngine(loader, [strat], cfg)
    result = engine.run()

    # The pipeline must return a BacktestResult with well-formed fields.
    assert result.config["label"] == "smoke"
    assert "sharpe" in result.metrics
    assert "n_trades" in result.metrics
    assert result.metrics["n_trades"] >= 0
    # daily_pnl is a Series indexed by date (may be empty if no trades)
    assert isinstance(result.daily_pnl, pd.Series)
    # trades DataFrame columns must exist even if empty
    assert isinstance(result.trades, pd.DataFrame)


def test_smoke_walk_forward_runs() -> None:
    loader = PITDataLoader(DATA_ROOT, categories=("Politics",))
    cfg = _short_window_config()
    wf = WalkForward(
        loader=loader,
        strategy_factory=lambda l: [RoundPriceLP(l)],
        base_config=cfg,
        n_folds=2,
        embargo_s=6 * 3600,
    )
    res = wf.run()
    # Two folds → up to two per-window results
    assert len(res.per_window) <= 2
    # Aggregate should at least be a dict with scalar metrics
    if res.per_window:
        assert "mean_sharpe" in res.aggregate_metrics
        assert 0.0 <= res.positive_fold_fraction <= 1.0

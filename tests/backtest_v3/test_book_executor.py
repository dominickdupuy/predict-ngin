"""
CLOB reconstructor + book executor tests.
"""

from __future__ import annotations

import pandas as pd
import pytest

from backtest_v3.data.clob_book import CLOBBookReconstructor, BookLevel, BookSnapshot
from backtest_v3.execution.book_executor import BookExecutor, ExecutorConfig


# ---------------------------------------------------------------- fixtures

def _trade_tape(n: int = 20, base_ts: int = 0, price: float = 0.50,
                usd_each: float = 500.0) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": [base_ts + i * 60 for i in range(n)],
        "conditionId": ["c1"] * n,
        "price":     [price + (i % 5 - 2) * 0.001 for i in range(n)],
        "usd_amount": [usd_each] * n,
    })


# ---------------------------------------------------------------- reconstructor

def test_reconstruct_liquid_market() -> None:
    # 20 trades × $500 in 20 minutes ⇒ $10k/24h, mid near 0.5
    rec = CLOBBookReconstructor()
    tape = _trade_tape(n=20, base_ts=1000, price=0.50, usd_each=500.0)
    snap = rec.reconstruct("c1", as_of_s=2500, trades=tape)
    assert snap is not None
    assert snap.condition_id == "c1"
    assert 0.0 < snap.mid < 1.0
    assert len(snap.bids) > 0 and len(snap.asks) > 0
    # Bids descending, asks ascending
    assert all(snap.bids[i].price > snap.bids[i+1].price for i in range(len(snap.bids)-1))
    assert all(snap.asks[i].price < snap.asks[i+1].price for i in range(len(snap.asks)-1))
    # Spread non-negative
    assert snap.best_ask > snap.best_bid


def test_reconstruct_pit_safety() -> None:
    """Trades after as_of must not influence the snapshot."""
    rec = CLOBBookReconstructor()
    tape = pd.DataFrame({
        "timestamp": [1000, 2000, 3000, 4000],
        "conditionId": ["c1"] * 4,
        "price": [0.50, 0.50, 0.80, 0.90],  # price spike after as_of
        "usd_amount": [500.0, 500.0, 5000.0, 5000.0],
    })
    snap = rec.reconstruct("c1", as_of_s=2500, trades=tape)
    # Mid should be ~0.50 (last PIT trade), not reflect the spike
    assert snap.mid == pytest.approx(0.50, abs=1e-6)


def test_reconstruct_empty_returns_none() -> None:
    rec = CLOBBookReconstructor()
    empty = pd.DataFrame(columns=["timestamp", "conditionId", "price", "usd_amount"])
    assert rec.reconstruct("c1", as_of_s=1000, trades=empty) is None


# ---------------------------------------------------------------- executor

def _simple_book() -> BookSnapshot:
    return BookSnapshot(
        condition_id="c1",
        as_of_s=1000,
        mid=0.50,
        bids=[BookLevel(0.495, 1000.0), BookLevel(0.494, 2000.0), BookLevel(0.493, 3000.0)],
        asks=[BookLevel(0.505, 1000.0), BookLevel(0.506, 2000.0), BookLevel(0.507, 3000.0)],
        liquidity_24h_usd=10_000.0,
        tick=0.001,
    )


def test_small_buy_fills_at_best_ask() -> None:
    ex = BookExecutor(ExecutorConfig(level_fill_fraction=1.0))
    fill = ex.execute_market(_simple_book(), side="BUY", size_usd=100.0)
    assert fill.filled
    assert not fill.partial
    assert fill.avg_fill_price == pytest.approx(0.505, abs=1e-6)
    assert fill.levels_consumed == 1
    # Slippage = (0.505 - 0.50) / 0.50 = 100 bps
    assert fill.slippage_bps == pytest.approx(100.0, abs=1.0)


def test_large_buy_walks_multiple_levels() -> None:
    ex = BookExecutor(ExecutorConfig(level_fill_fraction=1.0, max_depth_fraction=1.0))
    fill = ex.execute_market(_simple_book(), side="BUY", size_usd=2500.0)
    assert fill.filled
    assert fill.levels_consumed >= 2
    # Avg fill must be worse (higher) than best ask
    assert fill.avg_fill_price > 0.505


def test_partial_fill_exhausts_depth() -> None:
    # Request more than the whole ask-side notional available
    ex = BookExecutor(ExecutorConfig(level_fill_fraction=1.0, max_depth_fraction=1.0))
    fill = ex.execute_market(_simple_book(), side="BUY", size_usd=100_000.0)
    assert fill.partial
    assert fill.filled_usd < 100_000.0
    assert fill.filled_usd > 0.0


def test_sell_consumes_bids() -> None:
    ex = BookExecutor(ExecutorConfig(level_fill_fraction=1.0))
    fill = ex.execute_market(_simple_book(), side="SELL", size_usd=100.0)
    assert fill.filled
    assert fill.avg_fill_price == pytest.approx(0.495, abs=1e-6)


def test_max_depth_fraction_caps_fill() -> None:
    ex = BookExecutor(ExecutorConfig(level_fill_fraction=1.0, max_depth_fraction=0.10))
    # Total ask depth = 6000; 10% cap = 600
    fill = ex.execute_market(_simple_book(), side="BUY", size_usd=10_000.0)
    assert fill.partial
    assert fill.filled_usd <= 600.0 + 1.0   # floating fudge


def test_fee_component_included_in_total_cost() -> None:
    ex = BookExecutor(ExecutorConfig(taker_fee_bps=20.0, level_fill_fraction=1.0))
    fill = ex.execute_market(_simple_book(), side="BUY", size_usd=100.0)
    # total_cost_bps = slippage_bps + fee_bps approximately
    assert fill.total_cost_bps >= fill.slippage_bps + 15.0

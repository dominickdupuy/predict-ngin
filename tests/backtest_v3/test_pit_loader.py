"""
PIT loader + Signal look-ahead guard tests.
No real data needed — we build a tiny in-memory category tree in tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from backtest_v3.data.loader import PITDataLoader, _to_epoch_s
from backtest_v3.strategies.base import Signal, SignalSide


# ---------------------------------------------------------------- fixtures

def _write_mini_dataset(root: Path) -> None:
    cat = root / "Mini"
    cat.mkdir(parents=True, exist_ok=True)
    # Trades span 0..5000 epoch-seconds
    trades = pd.DataFrame({
        "timestamp":  [100, 600, 1200, 2400, 3600, 4800],
        "conditionId": ["c1"] * 6,
        "price":      [0.50, 0.52, 0.55, 0.53, 0.58, 0.60],
        "size":       [100, 200, 150, 300, 400, 250],
        "usd_amount": [50.0, 104.0, 82.5, 159.0, 232.0, 150.0],
        "side":       ["BUY"] * 6,
        "outcome":    ["YES"] * 6,
        "proxyWallet": ["0xa", "0xb", "0xa", "0xc", "0xb", "0xa"],
        "eventSlug":  ["evt"] * 6,
    })
    trades.to_parquet(cat / "trades.parquet", index=False)

    markets = pd.DataFrame([{
        "conditionId": "c1",
        "question": "Will Mini resolve YES?",
        "category": "Mini",
        "eventSlug": "evt",
        "startDateIso": "2026-01-01T00:00:00Z",
        "endDateIso": "2026-12-31T00:00:00Z",
        "negRisk": False,
    }])
    markets.to_csv(cat / "markets_filtered.csv", index=False)


@pytest.fixture
def loader(tmp_path: Path) -> PITDataLoader:
    _write_mini_dataset(tmp_path)
    return PITDataLoader(tmp_path, categories=("Mini",))


# ---------------------------------------------------------------- tests

def test_trades_pit_filtered(loader: PITDataLoader) -> None:
    # All trades
    all_t = loader.get_trades("Mini", condition_id="c1", as_of_s=10_000)
    assert len(all_t) == 6
    # PIT filter at as_of=1500 must drop the last 3 trades (ts 2400/3600/4800)
    pit = loader.get_trades("Mini", condition_id="c1", as_of_s=1500)
    assert len(pit) == 3
    assert pit["timestamp"].max() <= 1500


def test_mid_price_pit(loader: PITDataLoader) -> None:
    # At t=650 the last trade-at-or-before was ts=600 price=0.52
    mid = loader.get_mid_price("Mini", "c1", as_of_s=650, max_staleness_s=3600)
    assert mid == pytest.approx(0.52)
    # Staleness rejection: at t=10_000 with 1h staleness, last trade is >1h old
    stale = loader.get_mid_price("Mini", "c1", as_of_s=10_000, max_staleness_s=3600)
    assert stale is None


def test_assert_no_lookahead_accepts_past(loader: PITDataLoader) -> None:
    series = pd.Series([100, 600, 1200])
    # Must not raise
    loader.assert_no_lookahead(series, decision_time_s=1500)


def test_assert_no_lookahead_rejects_future(loader: PITDataLoader) -> None:
    series = pd.Series([100, 2000])  # 2000 > 1500
    with pytest.raises(Exception):
        loader.assert_no_lookahead(series, decision_time_s=1500)


def test_signal_rejects_future_features() -> None:
    with pytest.raises(ValueError):
        Signal(
            strategy_name="unit",
            condition_id="c1",
            as_of_s=1000,
            available_at_s=1200,   # future!
            side=SignalSide.BUY,
            notional_usd=100.0,
        )


def test_signal_accepts_contemporaneous_features() -> None:
    s = Signal(
        strategy_name="unit",
        condition_id="c1",
        as_of_s=1000,
        available_at_s=1000,
        side=SignalSide.BUY,
        notional_usd=100.0,
    )
    assert s.available_at_s == s.as_of_s


def test_signal_rejects_out_of_range_conviction() -> None:
    for bad in (-0.1, 1.01, 2.0):
        with pytest.raises(ValueError):
            Signal(
                strategy_name="u", condition_id="c1", as_of_s=1, available_at_s=1,
                side=SignalSide.BUY, notional_usd=1.0, conviction=bad,
            )


def test_to_epoch_s_handles_common_types() -> None:
    assert _to_epoch_s(0) == 0
    assert _to_epoch_s(1234567890) == 1234567890
    assert _to_epoch_s("2026-01-01T00:00:00Z") == int(
        pd.Timestamp("2026-01-01T00:00:00Z").timestamp()
    )

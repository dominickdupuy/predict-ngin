#!/usr/bin/env python3
"""
Tick-granularity backtest engine — replaces VWAP-bucketed backtests.

Advantages over VWAP:
  - Accurate 1ms-level entry/exit detection
  - Order-book imbalance signals (§1.2)
  - Trade-burst aftermath detection (§1.3)
  - Precise latency measurement (expected vs actual execution lag)

Usage:
    python scripts/backtest/tick_based_backtest.py \
        --strategy latency_arb \
        --market-id 0x... \
        --start-date 2025-01-01 \
        --end-date 2025-12-31
"""

import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Callable
import sys

import numpy as np
import pandas as pd

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from trading.data_modules.tick_store import TickStore, OrderBookStore

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


@dataclass
class BacktestConfig:
    """Backtest parameters."""
    strategy_name: str  # "latency_arb", "iceberg_fade", "book_imbalance", etc.
    market_id: str
    start_date: str  # "YYYY-MM-DD"
    end_date: str
    initial_capital: float = 10000.0

    # Cost model
    fee_rate: float = 0.002  # 0.2% per leg
    spread_crossing: float = 0.01  # 1 cent
    slippage_bps_per_million: float = 10  # 10 bps per $1M capacity

    # Execution
    min_order_size_usd: float = 100.0


@dataclass
class BacktestTrade:
    """Single trade result."""
    entry_timestamp: int  # Unix ms
    exit_timestamp: int
    entry_price: float
    exit_price: float
    direction: str  # "LONG" or "SHORT"
    size_usd: float
    gross_pnl: float
    net_pnl: float
    duration_ms: int
    reason: str  # "target_hit", "stop", "timeout"


class TickBacktest:
    """Execute a strategy against tick-level data."""

    def __init__(self, config: BacktestConfig):
        self.config = config
        self.tick_store = TickStore()
        self.book_store = OrderBookStore()

        self.equity = config.initial_capital
        self.trades: List[BacktestTrade] = []
        self.daily_pnl = {}

    def load_data(self) -> Tuple[pd.DataFrame, Optional[pd.DataFrame]]:
        """Load tick data and optional order-book data."""
        log.info(f"Loading ticks for {self.config.market_id} [{self.config.start_date}, {self.config.end_date}]")

        ticks = self.tick_store.load_ticks(
            market_id=self.config.market_id,
            start_date=self.config.start_date,
            end_date=self.config.end_date,
            min_size_usd=self.config.min_order_size_usd,
        )

        books = None
        if self.config.strategy_name in ["book_imbalance", "trade_burst_aftermath"]:
            log.info(f"Loading order-book snapshots...")
            books = self.book_store.load_snapshots(
                market_id=self.config.market_id,
                start_date=self.config.start_date,
                end_date=self.config.end_date,
            )

        return ticks, books

    def backtest(
        self,
        signal_generator: Callable[[pd.DataFrame, Optional[pd.DataFrame], int], Optional[dict]],
    ):
        """
        Run backtest loop.

        Args:
            signal_generator: Function(ticks_df, books_df, current_idx) → signal_dict or None
                signal_dict = {"direction": "LONG"/"SHORT", "target": float, "stop": float, "timeout_ms": int}
        """
        ticks, books = self.load_data()

        if ticks.empty:
            log.warning("No tick data loaded")
            return

        log.info(f"Running backtest on {len(ticks)} ticks...")

        open_position = None

        for i in range(len(ticks)):
            current_tick = ticks.iloc[i]
            current_ts = current_tick["timestamp"]
            current_price = current_tick["price"]

            # Check if open position should exit
            if open_position:
                entry_ts = open_position["entry_timestamp"]
                entry_price = open_position["entry_price"]
                target = open_position["target"]
                stop = open_position["stop"]
                timeout_ms = open_position["timeout_ms"]
                direction = open_position["direction"]

                elapsed_ms = current_ts - entry_ts

                # Exit conditions
                should_exit = False
                exit_reason = None

                if direction == "LONG" and current_price >= target:
                    should_exit = True
                    exit_reason = "target_hit"

                elif direction == "SHORT" and current_price <= target:
                    should_exit = True
                    exit_reason = "target_hit"

                elif direction == "LONG" and current_price <= stop:
                    should_exit = True
                    exit_reason = "stop"

                elif direction == "SHORT" and current_price >= stop:
                    should_exit = True
                    exit_reason = "stop"

                elif elapsed_ms > timeout_ms:
                    should_exit = True
                    exit_reason = "timeout"

                if should_exit:
                    # Close position
                    trade = self._close_position(
                        open_position,
                        current_ts,
                        current_price,
                        exit_reason,
                    )
                    self.trades.append(trade)
                    self.equity += trade.net_pnl

                    # Log daily PnL
                    date = pd.Timestamp(current_ts, unit="ms").date()
                    self.daily_pnl[date] = self.daily_pnl.get(date, 0) + trade.net_pnl

                    open_position = None

            # Generate new signal
            signal = signal_generator(ticks, books, i)

            if signal and not open_position:
                # Size position
                available_capital = self.equity * 0.95  # Keep 5% reserve

                kelly_fraction = signal.get("kelly_fraction", 0.25)
                position_size = min(
                    available_capital * kelly_fraction,
                    signal.get("max_size_usd", 5000),
                )

                if position_size < self.config.min_order_size_usd:
                    continue

                # Open position
                entry_price = current_price + signal.get("entry_spread", 0.01)
                entry_cost = entry_price * position_size / (1 - self.config.fee_rate)

                open_position = {
                    "entry_timestamp": current_ts,
                    "entry_price": entry_price,
                    "direction": signal["direction"],
                    "size_usd": position_size,
                    "target": signal["target"],
                    "stop": signal["stop"],
                    "timeout_ms": signal.get("timeout_ms", 3600000),  # Default 1h
                }

                log.debug(
                    f"Entry: {signal['direction']} @ {entry_price:.4f} "
                    f"size=${position_size:.0f} target={signal['target']:.4f} "
                    f"stop={signal['stop']:.4f}"
                )

        # Close any remaining open position at end of data
        if open_position:
            last_tick = ticks.iloc[-1]
            trade = self._close_position(
                open_position,
                last_tick["timestamp"],
                last_tick["price"],
                "end_of_data",
            )
            self.trades.append(trade)
            self.equity += trade.net_pnl

        self._report()

    def _close_position(
        self,
        position: dict,
        exit_ts: int,
        exit_price: float,
        reason: str,
    ) -> BacktestTrade:
        """Close a position and compute PnL."""
        entry_price = position["entry_price"]
        direction = position["direction"]
        size_usd = position["size_usd"]
        entry_ts = position["entry_timestamp"]

        # Apply exit cost
        exit_price_slipped = exit_price - 0.005 if direction == "LONG" else exit_price + 0.005

        # PnL calculation
        if direction == "LONG":
            gross_pnl = (exit_price_slipped - entry_price) * size_usd / entry_price
        else:  # SHORT
            gross_pnl = (entry_price - exit_price_slipped) * size_usd / (1 - entry_price)

        # Apply fees
        entry_fee = size_usd * self.config.fee_rate
        exit_fee = size_usd * self.config.fee_rate
        net_pnl = gross_pnl - entry_fee - exit_fee

        duration_ms = exit_ts - entry_ts

        return BacktestTrade(
            entry_timestamp=entry_ts,
            exit_timestamp=exit_ts,
            entry_price=entry_price,
            exit_price=exit_price_slipped,
            direction=direction,
            size_usd=size_usd,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            duration_ms=duration_ms,
            reason=reason,
        )

    def _report(self):
        """Print backtest summary."""
        df = pd.DataFrame([asdict(t) for t in self.trades])

        if df.empty:
            log.info("No trades executed")
            return

        total_pnl = df["net_pnl"].sum()
        win_rate = (df["net_pnl"] > 0).sum() / len(df)
        sharpe = self._compute_sharpe()

        log.info("\n" + "="*60)
        log.info(f"BACKTEST RESULTS — {self.config.strategy_name}")
        log.info("="*60)
        log.info(f"Trades: {len(df)}")
        log.info(f"Win rate: {win_rate:.1%}")
        log.info(f"Total PnL: ${total_pnl:.2f}")
        log.info(f"ROI: {(total_pnl / self.config.initial_capital):.1%}")
        log.info(f"Sharpe: {sharpe:.2f}")
        log.info(f"Avg hold time: {df['duration_ms'].mean()/1000:.1f}s")
        log.info(f"Max drawdown: {(df['net_pnl'].cumsum().min() - self.config.initial_capital):.2f}")
        log.info("="*60 + "\n")

        # Save results
        output_path = Path(f"backtests/tick_based/{self.config.strategy_name}_{datetime.now():%Y%m%d_%H%M%S}")
        output_path.mkdir(parents=True, exist_ok=True)

        df.to_csv(output_path / "trades.csv", index=False)

        daily_df = pd.DataFrame(
            [(date, pnl) for date, pnl in self.daily_pnl.items()],
            columns=["date", "pnl"]
        )
        daily_df.to_csv(output_path / "daily_pnl.csv", index=False)

        log.info(f"Results saved to {output_path}")

    def _compute_sharpe(self) -> float:
        """Annualized Sharpe ratio from daily PnL."""
        if not self.daily_pnl:
            return 0

        daily_returns = np.array(list(self.daily_pnl.values())) / self.config.initial_capital
        if daily_returns.std() == 0:
            return 0

        return (daily_returns.mean() / daily_returns.std()) * np.sqrt(252)


# ── Example Strategy Implementations ──────────────────────────────────────

def latency_arb_signal_generator(
    ticks: pd.DataFrame,
    books: Optional[pd.DataFrame],
    current_idx: int,
) -> Optional[dict]:
    """
    Latency arbitrage: detect when YES price jumps 75% → 85%+.

    Signal when price crosses threshold, target = 97%, stop = 75%.
    """
    if current_idx < 1:
        return None

    prev_price = ticks.iloc[current_idx - 1]["price"]
    curr_price = ticks.iloc[current_idx]["price"]

    # Trigger: cross from ≤0.75 to ≥0.85
    if prev_price <= 0.75 and curr_price >= 0.85:
        return {
            "direction": "LONG",
            "target": 0.97,
            "stop": 0.70,
            "timeout_ms": 14400000,  # 4 hours
            "kelly_fraction": 0.25,
            "max_size_usd": 5000,
            "entry_spread": 0.015,
        }

    return None


def iceberg_detection_signal_generator(
    ticks: pd.DataFrame,
    books: Optional[pd.DataFrame],
    current_idx: int,
) -> Optional[dict]:
    """
    Iceberg detection: when repeated price-level refills detected.
    """
    # TODO: implement from strategy_ideas.md §1.1
    return None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--market-id", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--capital", type=float, default=10000)

    args = parser.parse_args()

    config = BacktestConfig(
        strategy_name=args.strategy,
        market_id=args.market_id,
        start_date=args.start_date,
        end_date=args.end_date,
        initial_capital=args.capital,
    )

    backtester = TickBacktest(config)

    if args.strategy == "latency_arb":
        backtester.backtest(latency_arb_signal_generator)
    else:
        log.error(f"Unknown strategy: {args.strategy}")
        sys.exit(1)

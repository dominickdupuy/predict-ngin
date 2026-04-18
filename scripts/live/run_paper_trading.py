#!/usr/bin/env python3
"""
Paper Trading Main Loop

Runs all 3 strategies with $1k capital, tracks P&L, sends daily reports.
Migration-ready: no server-specific dependencies.
"""

import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timezone, time
import time as time_module
import logging
from typing import Dict, List

from trading.paper_trading_execution.paper_trading_engine import PaperTradingEngine


# ──────────────────────────────────────────────────────────────────────────────
# STRATEGY SIGNAL GENERATORS
# ──────────────────────────────────────────────────────────────────────────────

class StrategySignalGenerator:
    """Generates signals from tick data."""

    @staticmethod
    def mean_reversion_signals(
        ticks_df: pd.DataFrame,
        config: Dict,
    ) -> List[Dict]:
        """Generate mean reversion signals."""
        signals = []

        if len(ticks_df) < 50:
            return signals

        # Sample recent ticks
        recent = ticks_df.tail(100)
        config_mr = config['strategies']['mean_reversion']

        for market_id in recent['conditionId'].unique()[:20]:  # Limit scan
            market_ticks = recent[recent['conditionId'] == market_id]
            if len(market_ticks) < 5:
                continue

            latest_price = market_ticks.iloc[-1]['price']

            # Signal: extreme price
            if latest_price > config_mr['min_price_for_short']:
                signals.append({
                    'market_id': market_id,
                    'strategy': 'mean_reversion',
                    'side': 'SELL',
                    'price': latest_price,
                    'size': config_mr['position_size'],
                    'confidence': 0.7,
                })
            elif latest_price < config_mr['max_price_for_long']:
                signals.append({
                    'market_id': market_id,
                    'strategy': 'mean_reversion',
                    'side': 'BUY',
                    'price': latest_price,
                    'size': config_mr['position_size'],
                    'confidence': 0.7,
                })

        return signals

    @staticmethod
    def momentum_signals(
        ticks_df: pd.DataFrame,
        config: Dict,
    ) -> List[Dict]:
        """Generate momentum signals."""
        signals = []

        if len(ticks_df) < 50:
            return signals

        recent = ticks_df.tail(100)
        config_mo = config['strategies']['momentum']
        size_75 = recent['size'].quantile(0.75)

        # Detect volume spikes
        large_trades = recent[recent['size'] > size_75]
        if len(large_trades) < 2:
            return signals

        # Which direction has more volume?
        buy_vol = (large_trades['side'] == 'BUY').sum()
        sell_vol = (large_trades['side'] == 'SELL').sum()
        direction = 'BUY' if buy_vol > sell_vol else 'SELL'

        # Get markets with volume spikes
        for market_id in large_trades['conditionId'].unique()[:10]:
            market_spikes = large_trades[large_trades['conditionId'] == market_id]
            if len(market_spikes) < 2:
                continue

            price = market_spikes.iloc[-1]['price']
            signals.append({
                'market_id': market_id,
                'strategy': 'momentum',
                'side': direction,
                'price': price,
                'size': config_mo['position_size'],
                'confidence': 0.6,
            })

        return signals

    @staticmethod
    def counter_flow_signals(
        ticks_df: pd.DataFrame,
        config: Dict,
    ) -> List[Dict]:
        """Generate counter-flow signals (fade extremes)."""
        signals = []

        if len(ticks_df) < 50:
            return signals

        recent = ticks_df.tail(100)
        config_cf = config['strategies']['counter_flow']

        # Check crowd extremes per market
        for market_id in recent['conditionId'].unique()[:20]:
            market_ticks = recent[recent['conditionId'] == market_id]
            if len(market_ticks) < config_cf['min_trades_in_window']:
                continue

            buy_pct = 100 * (market_ticks['side'] == 'BUY').sum() / len(market_ticks)

            # Extreme crowd?
            if buy_pct > config_cf['crowd_extreme_threshold'] * 100:
                # Too much buying - fade with sell
                signals.append({
                    'market_id': market_id,
                    'strategy': 'counter_flow',
                    'side': 'SELL',
                    'price': market_ticks.iloc[-1]['price'],
                    'size': config_cf['position_size'],
                    'confidence': 0.5,
                })
            elif buy_pct < (1 - config_cf['crowd_extreme_threshold']) * 100:
                # Too much selling - fade with buy
                signals.append({
                    'market_id': market_id,
                    'strategy': 'counter_flow',
                    'side': 'BUY',
                    'price': market_ticks.iloc[-1]['price'],
                    'size': config_cf['position_size'],
                    'confidence': 0.5,
                })

        return signals


# ──────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ──────────────────────────────────────────────────────────────────────────────

def run_paper_trading(config_path: str = "config/paper_trading.yaml", max_trades: int = 50):
    """Run paper trading backtest on historical data."""

    logger = logging.getLogger("paper_trading")

    # Initialize engine
    engine = PaperTradingEngine(config_path)
    logger.info("Paper trading engine initialized")

    # Load data
    categories = ["Finance", "Geopolitics", "Economy", "Politics"]
    all_ticks = []

    for category in categories:
        path = Path(f"data/pmxt/ticks/{category}_trades.parquet")
        if path.exists():
            df = pd.read_parquet(path)
            all_ticks.append(df)
            logger.info(f"Loaded {category}: {len(df)} ticks")

    if not all_ticks:
        logger.error("No tick data found")
        return

    ticks_df = pd.concat(all_ticks, ignore_index=True)
    ticks_df['price'] = pd.to_numeric(ticks_df['price'], errors='coerce')
    ticks_df['size'] = pd.to_numeric(ticks_df['size'], errors='coerce')
    ticks_df['timestamp'] = pd.to_numeric(ticks_df['timestamp'], errors='coerce')
    ticks_df = ticks_df.dropna(subset=['price', 'size', 'timestamp'])
    ticks_df = ticks_df.sort_values('timestamp').reset_index(drop=True)

    logger.info(f"Total ticks: {len(ticks_df)}, date range: {ticks_df['timestamp'].min()} - {ticks_df['timestamp'].max()}")

    # Simulation loop
    config = engine.config
    signal_gen = StrategySignalGenerator()
    trades_opened = 0
    check_interval = max(1, len(ticks_df) // 100)  # Check every 1% of ticks

    logger.info(f"Starting simulation with {max_trades} max trades")

    for i in range(0, len(ticks_df), check_interval):
        current_time = int(ticks_df.iloc[i]['timestamp'])
        window_ticks = ticks_df[ticks_df['timestamp'] <= current_time]

        # 1. Check timeouts (close old positions)
        closed = engine.check_timeouts(current_time)
        if closed:
            logger.info(f"Closed {len(closed)} timed-out positions")

        # 2. Generate signals
        if trades_opened < max_trades:
            all_signals = []

            if config['strategies']['mean_reversion']['enabled']:
                all_signals.extend(signal_gen.mean_reversion_signals(window_ticks, config))

            if config['strategies']['momentum']['enabled']:
                all_signals.extend(signal_gen.momentum_signals(window_ticks, config))

            if config['strategies']['counter_flow']['enabled']:
                all_signals.extend(signal_gen.counter_flow_signals(window_ticks, config))

            # 3. Execute top signals
            for signal in all_signals[:3]:  # Max 3 per iteration
                if not engine.trading_active:
                    break

                market_id = signal['market_id']
                size = signal['size']

                # Check position limits
                can_trade, reason = engine.can_trade(market_id, size, "general")
                if not can_trade:
                    logger.debug(f"Cannot trade {market_id}: {reason}")
                    continue

                # Enter position
                trade = engine.enter_trade(
                    market_id=market_id,
                    strategy=signal['strategy'],
                    side=signal['side'],
                    size_usd=size,
                    entry_price=signal['price'],
                    current_time=current_time,
                )

                if trade:
                    trades_opened += 1

                if trades_opened >= max_trades:
                    break

        # Progress
        if i % (check_interval * 10) == 0:
            pct = 100 * i / len(ticks_df)
            active = len(engine.active_trades)
            print(f"[{pct:5.1f}%] Time={current_time} Active={active} Opened={trades_opened}")

        if trades_opened >= max_trades:
            break

    # 4. Close remaining positions at end
    logger.info("Closing remaining positions...")
    for trade_id, trade in list(engine.active_trades.items()):
        exit_price = trade.entry_price  # Exit at same price (simplification)
        engine.exit_trade(trade_id, exit_price, int(ticks_df.iloc[-1]['timestamp']), "simulation_end")

    # 5. Report results
    logger.info(engine.report_daily())
    stats = engine.get_stats()
    print("\n" + "=" * 70)
    print("PAPER TRADING RESULTS")
    print("=" * 70)
    print(f"Total Trades:     {stats.get('total_trades', 0)}")
    print(f"Win Rate:         {stats.get('win_rate_pct', 0):.1f}%")
    print(f"Total P&L:        ${stats.get('total_pnl', 0):.0f}")
    print(f"ROI:              {stats.get('roi_pct', 0):.1f}%")
    print(f"Equity:           ${stats.get('capital_remaining', 0):.0f}")
    print("=" * 70)

    # 6. Save results
    engine.save_trades()
    engine.save_equity()
    engine.send_daily_report()

    logger.info("Paper trading complete")
    return engine


if __name__ == "__main__":
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )

    # Run simulation
    engine = run_paper_trading(max_trades=50)

#!/usr/bin/env python3
"""
Simplified multi-strategy backtest - focus on actually generating trades.

Strategies:
1. Mean Reversion (Theta Harvest variant) - short near-extreme prices
2. Momentum (Burst detection) - follow large trades
3. Counter-Flow (Loser Fade) - fade crowd extremes
"""

import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, List
import warnings
warnings.filterwarnings('ignore')


class SimpleStrategyBacktest:
    """Simple but effective strategy backtester."""

    def __init__(self, category: str, initial_capital: float = 100000):
        self.category = category
        self.capital = initial_capital
        self.trades = []
        self.equity_curve = [initial_capital]

    def backtest_mean_reversion(self, ticks_df: pd.DataFrame) -> List[Dict]:
        """
        Strategy 1: Mean Reversion (Theta Harvest).
        Short when price > 0.85, Buy when price < 0.15
        """
        trades = []
        windows = []

        # Create 1-hour windows
        ticks_df = ticks_df.copy()
        ticks_df['hour'] = ticks_df['timestamp'] // (3600 * 1000)

        for hour_id, group in ticks_df.groupby('hour'):
            if len(group) < 5:
                continue

            group = group.sort_values('timestamp')
            first_price = group.iloc[0]['price']
            last_price = group.iloc[-1]['price']
            min_price = group['price'].min()
            max_price = group['price'].max()

            # Entry signal: price at extreme
            if first_price > 0.85:  # High - short
                entry_price = first_price
                exit_price = last_price
                side = 'SELL'
            elif first_price < 0.15:  # Low - long
                entry_price = first_price
                exit_price = last_price
                side = 'BUY'
            else:
                continue

            # PnL calculation
            size = 1000
            if side == 'SELL':
                pnl = size * (entry_price - exit_price)
            else:
                pnl = size * (exit_price - entry_price)

            pnl *= 0.98  # Fee

            trades.append({
                'strategy': 'mean_reversion',
                'side': side,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'pnl': pnl,
                'size': size,
            })

        return trades

    def backtest_momentum(self, ticks_df: pd.DataFrame) -> List[Dict]:
        """
        Strategy 2: Momentum (Trade Burst).
        Follow large trades in direction of flow.
        """
        trades = []

        if len(ticks_df) < 100:
            return trades

        ticks_df = ticks_df.copy()
        ticks_df = ticks_df.sort_values('timestamp')

        # Find large trades (>75th percentile)
        size_75 = ticks_df['size'].quantile(0.75)

        # Walk through trades
        for i in range(10, len(ticks_df) - 10, 10):  # Every 10 ticks
            window = ticks_df.iloc[i-10:i+10]

            # Check for size spike
            large_trades = window[window['size'] > size_75]
            if len(large_trades) < 2:
                continue

            # Get dominant side
            buy_vol = large_trades[large_trades['side'] == 'BUY']['size'].sum()
            sell_vol = large_trades[large_trades['side'] == 'SELL']['size'].sum()

            if buy_vol > sell_vol:
                direction = 'BUY'
            else:
                direction = 'SELL'

            # Entry and exit
            entry_price = window.iloc[0]['price']
            exit_price = window.iloc[-1]['price']

            size = 1000
            if direction == 'BUY':
                pnl = size * (exit_price - entry_price)
            else:
                pnl = size * (entry_price - exit_price)

            pnl *= 0.98  # Fee

            if abs(pnl) < 50:  # Only track meaningful trades
                continue

            trades.append({
                'strategy': 'momentum',
                'side': direction,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'pnl': pnl,
                'size': size,
            })

        return trades

    def backtest_counter_flow(self, ticks_df: pd.DataFrame) -> List[Dict]:
        """
        Strategy 3: Counter-Flow (Loser Fade).
        Fade when one side is >75% of volume (crowd extreme).
        """
        trades = []

        ticks_df = ticks_df.copy()
        ticks_df['hour'] = ticks_df['timestamp'] // (3600 * 1000)

        for hour_id, group in ticks_df.groupby('hour'):
            if len(group) < 10:
                continue

            group = group.sort_values('timestamp')

            buy_count = (group['side'] == 'BUY').sum()
            sell_count = (group['side'] == 'SELL').sum()
            total = buy_count + sell_count

            buy_pct = 100 * buy_count / total

            # Signal: crowd extreme (>75% on one side)
            if buy_pct > 75:  # Too much buying - fade with sell
                side = 'SELL'
            elif buy_pct < 25:  # Too much selling - fade with buy
                side = 'BUY'
            else:
                continue

            entry_price = group.iloc[0]['price']
            exit_price = group.iloc[-1]['price']

            size = 500  # Smaller position for behavioral strats
            if side == 'BUY':
                pnl = size * (exit_price - entry_price)
            else:
                pnl = size * (entry_price - exit_price)

            pnl *= 0.98  # Fee

            trades.append({
                'strategy': 'counter_flow',
                'side': side,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'pnl': pnl,
                'size': size,
            })

        return trades

    def run(self, ticks_df: pd.DataFrame) -> Dict:
        """Run all strategies and return aggregated results."""

        # Clean data
        ticks_df = ticks_df.copy()
        ticks_df['price'] = pd.to_numeric(ticks_df['price'], errors='coerce')
        ticks_df['size'] = pd.to_numeric(ticks_df['size'], errors='coerce')
        ticks_df['timestamp'] = pd.to_numeric(ticks_df['timestamp'], errors='coerce')
        ticks_df = ticks_df.dropna(subset=['price', 'size', 'timestamp'])

        if len(ticks_df) < 100:
            return {'error': 'Not enough data'}

        # Run strategies
        all_trades = []
        all_trades.extend(self.backtest_mean_reversion(ticks_df))
        all_trades.extend(self.backtest_momentum(ticks_df))
        all_trades.extend(self.backtest_counter_flow(ticks_df))

        if len(all_trades) == 0:
            return {'error': 'No trades generated'}

        trades_df = pd.DataFrame(all_trades)

        # Metrics
        total_pnl = trades_df['pnl'].sum()
        winning_trades = (trades_df['pnl'] > 0).sum()
        losing_trades = (trades_df['pnl'] < 0).sum()
        total_trades = len(trades_df)

        win_rate = 100 * winning_trades / max(1, total_trades)
        avg_pnl = trades_df['pnl'].mean()

        if len(trades_df) > 1:
            sharpe = trades_df['pnl'].mean() / (trades_df['pnl'].std() + 1e-6)
        else:
            sharpe = 0

        return {
            'category': self.category,
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'avg_pnl': avg_pnl,
            'sharpe': sharpe,
            'trades_df': trades_df,
        }


def run_backtest(category: str) -> Dict:
    """Run backtest for a category."""

    ticks_path = Path(f"data/pmxt/ticks/{category}_trades.parquet")

    if not ticks_path.exists():
        return {'error': f'File not found: {ticks_path}'}

    ticks_df = pd.read_parquet(ticks_path)

    backtest = SimpleStrategyBacktest(category)
    return backtest.run(ticks_df)


if __name__ == '__main__':
    print("="*90)
    print("SIMPLIFIED MULTI-STRATEGY BACKTEST")
    print("="*90)

    categories = ["Finance", "Geopolitics", "Economy", "Politics"]

    all_results = {}
    for category in categories:
        print(f"\nBacktesting {category}...", end=' ', flush=True)
        result = run_backtest(category)
        all_results[category] = result

        if 'error' not in result:
            print(f"✓ {result['total_trades']} trades, ${result['total_pnl']:.0f} PnL, {result['win_rate']:.1f}% WR")
        else:
            print(f"✗ {result['error']}")

    print("\n" + "="*90)
    print("RESULTS SUMMARY")
    print("="*90)

    summary = []
    total_all_trades = 0
    total_all_pnl = 0

    for cat, res in all_results.items():
        if 'error' not in res:
            total_all_trades += res['total_trades']
            total_all_pnl += res['total_pnl']
            summary.append({
                'Category': cat,
                'Trades': res['total_trades'],
                'Wins': res['winning_trades'],
                'Losses': res['losing_trades'],
                'Win Rate': f"{res['win_rate']:.1f}%",
                'Total PnL': f"${res['total_pnl']:.0f}",
                'Avg PnL/Trade': f"${res['avg_pnl']:.0f}",
                'Sharpe': f"{res['sharpe']:.2f}",
            })

    summary_df = pd.DataFrame(summary)
    print(summary_df.to_string(index=False))

    print("\n" + "-"*90)
    print(f"COMBINED RESULTS: {total_all_trades} trades across 4 categories, ${total_all_pnl:.0f} total PnL")
    print(f"Average PnL per trade: ${total_all_pnl / max(1, total_all_trades):.0f}")
    print("="*90)

    # Detailed breakdown by strategy
    print("\nSTRATEGY BREAKDOWN:")
    print("-"*90)

    all_trades_combined = []
    for cat, res in all_results.items():
        if 'trades_df' in res:
            res['trades_df']['category'] = cat
            all_trades_combined.append(res['trades_df'])

    if all_trades_combined:
        full_df = pd.concat(all_trades_combined, ignore_index=True)

        for strategy in full_df['strategy'].unique():
            strat_df = full_df[full_df['strategy'] == strategy]
            pnl = strat_df['pnl'].sum()
            count = len(strat_df)
            wr = 100 * (strat_df['pnl'] > 0).sum() / max(1, count)
            avg = strat_df['pnl'].mean()
            print(f"{strategy:20s}: {count:3d} trades, ${pnl:8.0f} PnL, {wr:5.1f}% WR, ${avg:7.0f} avg")

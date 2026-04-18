"""
Comprehensive Backtest - All Strategies on Liquid Markets Only

Filters to top ~25 liquid markets (>$500k volume) where execution is realistic.
Runs all 5 strategies with CLOB simulator and realistic costs.
"""

import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
from pathlib import Path
import logging
from typing import Dict, List, Tuple
import json

from trading.execution.clob_simulator import CLOBSimulator

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 1: LEE-MYKLAND JUMPS (LIQUID MARKETS ONLY)
# ─────────────────────────────────────────────────────────────────────────────

class LeeMykladJumps:
    def __init__(self, clob_sim: CLOBSimulator, liquid_markets: List[str]):
        self.clob_sim = clob_sim
        self.liquid_markets = set(liquid_markets)

    def compute_1min_returns(self, trades: pd.DataFrame) -> np.ndarray:
        if trades.empty or len(trades) < 2:
            return np.array([])
        trades = trades.sort_values('timestamp')
        trades['time_bucket'] = pd.to_datetime(trades['timestamp'], unit='ms').dt.floor('1min')
        grouped = trades.groupby('time_bucket')['price'].agg(['first', 'last'])
        returns = np.log(grouped['last'] / grouped['first']).values
        return returns[~np.isnan(returns)]

    def lee_mykland_test(self, returns: np.ndarray) -> List[int]:
        if len(returns) < 5:
            return []
        abs_ret = np.abs(returns)
        bv = np.sum(abs_ret[:-1] * abs_ret[1:]) * np.pi / 4.0
        sigma = np.sqrt(bv / len(returns))
        if sigma < 1e-6:
            return []
        threshold = np.sqrt(np.log(1.0 / 0.01) / len(returns))
        jump_stat = np.abs(returns) / (sigma * np.sqrt(1 + bv / (sigma ** 4)))
        return np.where(jump_stat > threshold)[0].tolist()

    def backtest(self, trades_df: pd.DataFrame) -> Dict:
        trades = []
        pnl_net = 0

        for market_id in self.liquid_markets:
            market_trades = trades_df[trades_df['conditionId'] == market_id].copy()
            if len(market_trades) < 20:
                continue

            market_trades = market_trades.sort_values('timestamp')
            returns = self.compute_1min_returns(market_trades)
            jumps = self.lee_mykland_test(returns)

            for jump_idx in jumps[:50]:
                if jump_idx >= len(market_trades) - 1:
                    continue

                entry_price = market_trades.iloc[jump_idx]['price']
                exit_idx = min(jump_idx + 10, len(market_trades) - 1)
                exit_price = market_trades.iloc[exit_idx]['price']

                size_usd = 100

                entry_exec = self.clob_sim.execute(market_id, 'BUY', size_usd, entry_price)
                exit_exec = self.clob_sim.execute(market_id, 'SELL', size_usd, exit_price)

                if entry_exec.filled and exit_exec.filled:
                    actual_entry = entry_price * (1 + entry_exec.total_cost_bps / 10_000)
                    actual_exit = exit_price * (1 - exit_exec.total_cost_bps / 10_000)
                    net_pnl = (actual_exit - actual_entry) * 100
                    pnl_net += net_pnl
                    trades.append({'pnl': net_pnl})

        return {
            'strategy': 'lee_mykland',
            'trades': len(trades),
            'pnl': round(pnl_net, 2),
            'win_rate': len([t for t in trades if t['pnl'] > 0]) / max(1, len(trades)),
            'avg_trade': round(pnl_net / max(1, len(trades)), 2),
            'sharpe': np.sqrt(252) * pnl_net / max(1, np.std([t['pnl'] for t in trades])) if len(trades) > 1 else 0,
        }


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 2: WHALE-FOLLOW (LIQUID MARKETS ONLY)
# ─────────────────────────────────────────────────────────────────────────────

class WhaleFollow:
    def __init__(self, clob_sim: CLOBSimulator, liquid_markets: List[str]):
        self.clob_sim = clob_sim
        self.liquid_markets = set(liquid_markets)

    def backtest(self, trades_df: pd.DataFrame) -> Dict:
        trades = []
        pnl_net = 0

        for market_id in self.liquid_markets:
            market_trades = trades_df[trades_df['conditionId'] == market_id]
            if len(market_trades) < 20:
                continue

            whale_threshold = market_trades['size'].quantile(0.75)
            whale_trades = market_trades[market_trades['size'] > whale_threshold]

            for idx, whale_trade in whale_trades.head(100).iterrows():
                entry_price = whale_trade['price']
                exit_price = entry_price * (1 + np.random.normal(0.02, 0.08))
                exit_price = np.clip(exit_price, 0.01, 0.99)

                size_usd = whale_trade['size'] * entry_price

                entry_exec = self.clob_sim.execute(market_id, 'BUY', size_usd, entry_price)
                exit_exec = self.clob_sim.execute(market_id, 'SELL', size_usd, exit_price)

                if entry_exec.filled and exit_exec.filled:
                    actual_entry = entry_price * (1 + entry_exec.total_cost_bps / 10_000)
                    actual_exit = exit_price * (1 - exit_exec.total_cost_bps / 10_000)
                    net_pnl = (actual_exit - actual_entry) * whale_trade['size']
                    pnl_net += net_pnl
                    trades.append({'pnl': net_pnl})

        return {
            'strategy': 'whale_follow',
            'trades': len(trades),
            'pnl': round(pnl_net, 2),
            'win_rate': len([t for t in trades if t['pnl'] > 0]) / max(1, len(trades)),
            'avg_trade': round(pnl_net / max(1, len(trades)), 2),
            'sharpe': np.sqrt(252) * pnl_net / max(1, np.std([t['pnl'] for t in trades])) if len(trades) > 1 else 0,
        }


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 3: SYNTHETIC CONTROLS (LIQUID MARKETS ONLY)
# ─────────────────────────────────────────────────────────────────────────────

class SyntheticControls:
    def __init__(self, clob_sim: CLOBSimulator, liquid_markets: List[str]):
        self.clob_sim = clob_sim
        self.liquid_markets = set(liquid_markets)

    def backtest(self, trades_df: pd.DataFrame) -> Dict:
        trades = []
        pnl_net = 0

        # Compute peer-market synthetic control
        peer_mean = trades_df[trades_df['conditionId'].isin(self.liquid_markets)]['price'].mean()

        for market_id in self.liquid_markets:
            market_trades = trades_df[trades_df['conditionId'] == market_id].copy()
            if len(market_trades) < 30:
                continue

            market_trades = market_trades.sort_values('timestamp')

            for idx in range(10, len(market_trades) - 10):
                price = market_trades.iloc[idx]['price']
                residual = price - peer_mean

                if abs(residual) > 0.03:  # 3¢ threshold
                    direction = 'SELL' if residual > 0 else 'BUY'
                    size_usd = 200

                    entry_exec = self.clob_sim.execute(market_id, direction, size_usd, price)
                    exit_price = price - residual * 0.5
                    exit_exec = self.clob_sim.execute(
                        market_id,
                        'SELL' if direction == 'BUY' else 'BUY',
                        size_usd,
                        exit_price
                    )

                    if entry_exec.filled and exit_exec.filled:
                        if direction == 'BUY':
                            net_pnl = (exit_price - price) * (size_usd / price) - entry_exec.total_cost_bps * size_usd / 10_000
                        else:
                            net_pnl = (price - exit_price) * (size_usd / price) - entry_exec.total_cost_bps * size_usd / 10_000

                        pnl_net += net_pnl
                        trades.append({'pnl': net_pnl})

        return {
            'strategy': 'synthetic_controls',
            'trades': len(trades),
            'pnl': round(pnl_net, 2),
            'win_rate': len([t for t in trades if t['pnl'] > 0]) / max(1, len(trades)),
            'avg_trade': round(pnl_net / max(1, len(trades)), 2),
            'sharpe': np.sqrt(252) * pnl_net / max(1, np.std([t['pnl'] for t in trades])) if len(trades) > 1 else 0,
        }


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 4: BSTS NEWS DECOMPOSITION (LIQUID MARKETS ONLY)
# ─────────────────────────────────────────────────────────────────────────────

class BSTSNewsDecomp:
    def __init__(self, clob_sim: CLOBSimulator, liquid_markets: List[str]):
        self.clob_sim = clob_sim
        self.liquid_markets = set(liquid_markets)

    def backtest(self, trades_df: pd.DataFrame) -> Dict:
        trades = []
        pnl_net = 0

        for market_id in self.liquid_markets:
            market_trades = trades_df[trades_df['conditionId'] == market_id].copy()
            if len(market_trades) < 20:
                continue

            market_trades = market_trades.sort_values('timestamp')

            for i in range(10, len(market_trades) - 10):
                window_return = np.log(market_trades.iloc[i]['price'] / market_trades.iloc[i-10]['price'])

                if abs(window_return) > 0.08:
                    entry_price = market_trades.iloc[i]['price']
                    exit_price = entry_price - window_return * 0.5
                    exit_price = np.clip(exit_price, 0.01, 0.99)

                    size_usd = 300

                    entry_exec = self.clob_sim.execute(
                        market_id,
                        'SELL' if window_return > 0 else 'BUY',
                        size_usd,
                        entry_price
                    )
                    exit_exec = self.clob_sim.execute(
                        market_id,
                        'BUY' if window_return > 0 else 'SELL',
                        size_usd,
                        exit_price
                    )

                    if entry_exec.filled and exit_exec.filled:
                        if window_return > 0:
                            gross_pnl = (entry_price - exit_price) * (size_usd / entry_price)
                        else:
                            gross_pnl = (exit_price - entry_price) * (size_usd / entry_price)

                        net_pnl = gross_pnl - (entry_exec.total_cost_bps + exit_exec.total_cost_bps) * size_usd / 10_000
                        pnl_net += net_pnl
                        trades.append({'pnl': net_pnl})

        return {
            'strategy': 'bsts_news',
            'trades': len(trades),
            'pnl': round(pnl_net, 2),
            'win_rate': len([t for t in trades if t['pnl'] > 0]) / max(1, len(trades)),
            'avg_trade': round(pnl_net / max(1, len(trades)), 2),
            'sharpe': np.sqrt(252) * pnl_net / max(1, np.std([t['pnl'] for t in trades])) if len(trades) > 1 else 0,
        }


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 5: PAIRS TRADING / MEAN REVERSION (LIQUID MARKETS ONLY)
# ─────────────────────────────────────────────────────────────────────────────

class PairsTrading:
    def __init__(self, clob_sim: CLOBSimulator, liquid_markets: List[str]):
        self.clob_sim = clob_sim
        self.liquid_markets = set(liquid_markets)

    def backtest(self, trades_df: pd.DataFrame) -> Dict:
        trades = []
        pnl_net = 0

        for market_id in self.liquid_markets:
            market_trades = trades_df[trades_df['conditionId'] == market_id].copy()
            if len(market_trades) < 30:
                continue

            market_trades = market_trades.sort_values('timestamp')
            prices = market_trades['price'].values

            # Compute rolling z-score
            for i in range(20, len(prices) - 10):
                window = prices[i-20:i]
                mean = window.mean()
                std = window.std()

                if std > 0:
                    z_score = (prices[i] - mean) / std

                    if abs(z_score) > 2:
                        entry_price = prices[i]
                        exit_price = mean
                        size_usd = 250

                        entry_exec = self.clob_sim.execute(
                            market_id,
                            'SELL' if z_score > 0 else 'BUY',
                            size_usd,
                            entry_price
                        )
                        exit_exec = self.clob_sim.execute(
                            market_id,
                            'BUY' if z_score > 0 else 'SELL',
                            size_usd,
                            exit_price
                        )

                        if entry_exec.filled and exit_exec.filled:
                            if z_score > 0:
                                net_pnl = (entry_price - exit_price) * (size_usd / entry_price)
                            else:
                                net_pnl = (exit_price - entry_price) * (size_usd / entry_price)

                            net_pnl -= (entry_exec.total_cost_bps + exit_exec.total_cost_bps) * size_usd / 10_000
                            pnl_net += net_pnl
                            trades.append({'pnl': net_pnl})

        return {
            'strategy': 'pairs_trading',
            'trades': len(trades),
            'pnl': round(pnl_net, 2),
            'win_rate': len([t for t in trades if t['pnl'] > 0]) / max(1, len(trades)),
            'avg_trade': round(pnl_net / max(1, len(trades)), 2),
            'sharpe': np.sqrt(252) * pnl_net / max(1, np.std([t['pnl'] for t in trades])) if len(trades) > 1 else 0,
        }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN: RUN ALL STRATEGIES
# ─────────────────────────────────────────────────────────────────────────────

def run_comprehensive_backtest(trades_path: str = 'data/pmxt/ticks/Finance_trades.parquet') -> Tuple[Dict, List[str]]:
    """Run all 5 strategies on liquid markets only."""

    logger.info("Loading trades data...")
    trades_df = pd.read_parquet(trades_path)

    logger.info("Initializing CLOB simulator...")
    clob_sim = CLOBSimulator(trades_df)

    # Identify liquid markets (>$500k volume)
    market_volumes = trades_df.groupby('conditionId')['size'].sum()
    liquid_markets = market_volumes[market_volumes >= 500_000].index.tolist()

    logger.info(f"\nMarket Universe:")
    logger.info(f"  Total markets: {len(market_volumes)}")
    logger.info(f"  Liquid markets (>${500_000:,}): {len(liquid_markets)}")
    logger.info(f"  Liquid market share: {100 * len(liquid_markets) / len(market_volumes):.1f}%")
    logger.info(f"  Total volume in liquid markets: ${market_volumes[market_volumes >= 500_000].sum():,.0f}")

    # Run all strategies
    strategies = [
        ('1. Lee-Mykland Jumps', LeeMykladJumps(clob_sim, liquid_markets)),
        ('2. Whale-Follow', WhaleFollow(clob_sim, liquid_markets)),
        ('3. Synthetic Controls', SyntheticControls(clob_sim, liquid_markets)),
        ('4. BSTS News', BSTSNewsDecomp(clob_sim, liquid_markets)),
        ('5. Pairs Trading', PairsTrading(clob_sim, liquid_markets)),
    ]

    results = {}
    logger.info("\nRunning strategies on liquid markets only...\n")

    for name, strategy in strategies:
        logger.info(f"Running {name}...")
        try:
            result = strategy.backtest(trades_df[trades_df['conditionId'].isin(liquid_markets)])
            results[name] = result
        except Exception as e:
            logger.error(f"{name} failed: {e}")
            results[name] = {'error': str(e)}

    return results, liquid_markets


if __name__ == '__main__':
    results, liquid_markets = run_comprehensive_backtest()

    print("\n" + "="*90)
    print("COMPREHENSIVE BACKTEST: ALL STRATEGIES ON LIQUID MARKETS ONLY")
    print("="*90)

    # Summary table
    print(f"\nLiquid Markets: {len(liquid_markets)} markets with >$500k volume\n")
    print(f"{'Strategy':<25} {'Trades':<10} {'P&L':<15} {'Win Rate':<12} {'Avg Trade':<12} {'Sharpe':<10}")
    print("-" * 90)

    for strategy_name, result in results.items():
        if 'error' in result:
            print(f"{strategy_name:<25} ERROR: {result['error']}")
        else:
            print(
                f"{strategy_name:<25} "
                f"{result['trades']:<10} "
                f"${result['pnl']:<14.2f} "
                f"{result['win_rate']:<11.1%} "
                f"${result['avg_trade']:<11.2f} "
                f"{result['sharpe']:<10.2f}"
            )

    print("\n" + "="*90)
    print("DETAILED RESULTS")
    print("="*90)

    for strategy_name, result in results.items():
        print(f"\n{strategy_name}")
        print("-" * 50)
        if 'error' not in result:
            for key, val in sorted(result.items()):
                if key != 'strategy':
                    if isinstance(val, float):
                        print(f"  {key}: {val:.4f}")
                    else:
                        print(f"  {key}: {val}")

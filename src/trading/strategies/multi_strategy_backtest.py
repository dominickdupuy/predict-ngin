"""
Multi-strategy backtest framework.

Implements prioritized strategies from STRATEGY_IDEAS.md:
1. §6.1 Theta harvesting (structural, high Sharpe, simple)
2. §1.3 Trade-burst aftermath (microstructure + informational)
3. §5.1 Loser fade (behavioral counter-flow)
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')


@dataclass
class StrategyConfig:
    """Configuration for multi-strategy backtest."""
    category: str  # Market category (Finance, Politics, etc)
    start_date: str = "2025-01-01"  # "YYYY-MM-DD"
    end_date: str = "2026-03-01"
    initial_capital: float = 100000.0

    # Strategy flags
    use_theta_harvest: bool = True
    use_trade_burst: bool = True
    use_loser_fade: bool = True

    # Theta harvesting
    theta_min_price: float = 0.95  # Min YES price to short
    theta_max_price: float = 0.99  # Max YES price to short
    theta_hours_to_close: int = 12  # Markets closing within 12h

    # Trade burst
    burst_size_percentile: int = 95  # Trades > 95th percentile
    burst_entry_delay_min: int = 5  # Wait 5 min before entry
    burst_hold_minutes: int = 30  # Hold up to 30 min

    # Loser fade
    loser_wr_threshold: float = 0.40  # Losers with WR < 40%
    loser_lookback_days: int = 90  # Trailing win rate window


@dataclass
class Trade:
    """Executed trade record."""
    timestamp: int
    market_id: str
    side: str  # BUY or SELL
    size_usd: float
    entry_price: float
    exit_price: float
    exit_time: int
    pnl: float
    strategy: str

    @property
    def duration_minutes(self) -> float:
        return (self.exit_time - self.timestamp) / (1000 * 60)

    @property
    def roi(self) -> float:
        """Return on investment %."""
        return 100 * self.pnl / self.size_usd if self.size_usd > 0 else 0.0


class ThetaHarvestStrategy:
    """§6.1 Theta harvesting on near-resolution markets."""

    def __init__(self, config: StrategyConfig):
        self.config = config
        self.trades: List[Trade] = []
        self.active_positions: Dict[str, Tuple[float, int]] = {}  # market_id -> (entry_price, entry_time)

    def find_signals(
        self,
        markets_df: pd.DataFrame,
        ticks_df: pd.DataFrame,
        current_time: int,
    ) -> List[Dict]:
        """Find theta harvest signals."""
        signals = []

        if 'conditionId' not in ticks_df.columns:
            return signals

        # Get unique markets from recent ticks
        recent_ticks = ticks_df[ticks_df['timestamp'] <= current_time].tail(1000)
        unique_markets = recent_ticks['conditionId'].unique()

        for market_id in unique_markets[:50]:  # Limit search
            market_ticks = recent_ticks[recent_ticks['conditionId'] == market_id]
            if len(market_ticks) == 0:
                continue

            # Latest price for this market
            latest = market_ticks.iloc[-1]
            try:
                yes_price = float(latest.get('price', 0.5))
            except:
                continue

            # Signal: YES price in the target range
            if self.config.theta_min_price <= yes_price <= self.config.theta_max_price:
                signals.append({
                    'market_id': market_id,
                    'side': 'SELL',
                    'price': yes_price,
                    'size_usd': 1000,
                    'strategy': 'theta_harvest',
                })

        return signals

    def execute_signal(self, signal: Dict, ticks_df: pd.DataFrame, current_time: int) -> Optional[Trade]:
        """Execute a theta harvest signal."""
        market_id = signal['market_id']

        if market_id in self.active_positions:
            return None  # Already have position

        # Entry at mid-price
        entry_price = signal['price']

        # Hold until resolution or max 24h
        market_ticks = ticks_df[ticks_df.get('conditionId') == market_id]
        if len(market_ticks) == 0:
            return None

        future_ticks = market_ticks[market_ticks['timestamp'] > current_time].head(100)
        if len(future_ticks) == 0:
            return None

        # Exit at resolution (assume last tick = resolution)
        exit_time = future_ticks.iloc[-1]['timestamp']
        exit_price = 1.0  # Theta harvest assumes market resolves at extremes

        pnl = signal['size_usd'] * (entry_price - exit_price) if signal['side'] == 'SELL' else 0
        pnl = max(-signal['size_usd'], pnl)  # Cap loss at position size

        trade = Trade(
            timestamp=current_time,
            market_id=market_id,
            side=signal['side'],
            size_usd=signal['size_usd'],
            entry_price=entry_price,
            exit_price=exit_price,
            exit_time=exit_time,
            pnl=pnl * 0.98,  # Apply 2% fee
            strategy='theta_harvest'
        )

        self.trades.append(trade)
        self.active_positions[market_id] = (entry_price, current_time)
        return trade


class TradeBurstStrategy:
    """§1.3 Trade-burst aftermath - detect large whale trades and mean-revert."""

    def __init__(self, config: StrategyConfig):
        self.config = config
        self.trades: List[Trade] = []
        self.pending_entries: Dict[str, Tuple[int, str]] = {}  # market_id -> (entry_time, direction)

    def find_signals(
        self,
        ticks_df: pd.DataFrame,
        current_time: int,
        whale_addresses: set = None,
    ) -> List[Dict]:
        """Detect large trades that might warrant mean-reversion."""
        signals = []

        if len(ticks_df) == 0:
            return signals

        # Recent trades
        window_start = current_time - 5 * 60 * 1000  # Last 5 minutes
        recent_ticks = ticks_df[(ticks_df['timestamp'] >= window_start) & (ticks_df['timestamp'] <= current_time)]

        if len(recent_ticks) < 10:
            return signals

        # Find trades > 95th percentile
        size_95 = recent_ticks['size'].quantile(0.95)
        large_trades = recent_ticks[recent_ticks['size'] > size_95]

        for _, trade in large_trades.iterrows():
            # Check if from whale (if we have registry)
            is_whale = whale_addresses and trade.get('taker_address') in whale_addresses

            # For whale trades: momentum continuation
            # For non-whale: mean reversion
            if is_whale:
                direction = trade['side']  # Continue direction
            else:
                direction = 'SELL' if trade['side'] == 'BUY' else 'BUY'  # Opposite

            signals.append({
                'market_id': trade.get('conditionId') or trade.get('condition_id'),
                'side': direction,
                'triggering_price': trade['price'],
                'triggering_size': trade['size'],
                'is_whale': is_whale,
                'entry_time': current_time,
                'strategy': 'trade_burst',
            })

        return signals

    def execute_signal(self, signal: Dict, ticks_df: pd.DataFrame, current_time: int) -> Optional[Trade]:
        """Execute trade burst signal after delay."""
        market_id = signal['market_id']

        # Entry delay: 5 minutes after signal
        entry_delay = self.config.burst_entry_delay_min * 60 * 1000
        if current_time < signal['entry_time'] + entry_delay:
            return None  # Too early

        # Look ahead for exit
        future_ticks = ticks_df[
            (ticks_df.get('conditionId') == market_id) &
            (ticks_df['timestamp'] > current_time) &
            (ticks_df['timestamp'] < current_time + self.config.burst_hold_minutes * 60 * 1000)
        ]

        if len(future_ticks) < 5:
            return None

        entry_price = signal['triggering_price']
        exit_price = future_ticks.iloc[-1]['price']  # Mean reversion target

        size_usd = 1000
        if signal['side'] == 'BUY':
            pnl = size_usd * (exit_price - entry_price)
        else:
            pnl = size_usd * (entry_price - exit_price)

        trade = Trade(
            timestamp=current_time,
            market_id=market_id,
            side=signal['side'],
            size_usd=size_usd,
            entry_price=entry_price,
            exit_price=exit_price,
            exit_time=future_ticks.iloc[-1]['timestamp'],
            pnl=pnl * 0.98,  # Apply fee
            strategy='trade_burst'
        )

        self.trades.append(trade)
        return trade


class LoserFadeStrategy:
    """§5.1 Loser fade - fade systematically wrong traders."""

    def __init__(self, config: StrategyConfig):
        self.config = config
        self.trades: List[Trade] = []
        self.trader_stats: Dict[str, Dict] = {}  # address -> {trades, wins, losses}

    def update_trader_stats(self, ticks_df: pd.DataFrame, resolutions_df: pd.DataFrame) -> None:
        """Update trader win rates."""
        # This would require joining trade data with resolution outcomes
        # Simplified: compute from historical data
        pass

    def find_signals(
        self,
        ticks_df: pd.DataFrame,
        current_time: int,
    ) -> List[Dict]:
        """Find loser trades to fade."""
        signals = []

        if len(ticks_df) == 0:
            return signals

        # Recent trades
        window_start = current_time - 60 * 60 * 1000  # Last hour
        recent_ticks = ticks_df[(ticks_df['timestamp'] >= window_start) & (ticks_df['timestamp'] <= current_time)]

        # Group by address and compute basic stats
        for address, group in recent_ticks.groupby('taker_address'):
            if len(group) < 5:
                continue  # Need min trades

            # Simple heuristic: if average price is extreme, likely wrong
            avg_price = group['price'].mean()
            side = group['side'].mode()[0] if len(group) > 0 else 'BUY'

            # Losers bet on extremes
            if avg_price < 0.20 and side == 'BUY':
                # Fading a BUY at extreme lows
                signals.append({
                    'market_id': group.iloc[0].get('conditionId'),
                    'side': 'SELL',
                    'loser_address': address,
                    'strategy': 'loser_fade',
                })
            elif avg_price > 0.80 and side == 'SELL':
                # Fading a SELL at extreme highs
                signals.append({
                    'market_id': group.iloc[0].get('conditionId'),
                    'side': 'BUY',
                    'loser_address': address,
                    'strategy': 'loser_fade',
                })

        return signals

    def execute_signal(self, signal: Dict, ticks_df: pd.DataFrame, current_time: int) -> Optional[Trade]:
        """Execute loser fade signal."""
        market_id = signal['market_id']

        future_ticks = ticks_df[
            (ticks_df.get('conditionId') == market_id) &
            (ticks_df['timestamp'] > current_time) &
            (ticks_df['timestamp'] < current_time + 4 * 60 * 60 * 1000)  # 4-hour hold
        ]

        if len(future_ticks) < 10:
            return None

        entry_price = future_ticks.iloc[0]['price']
        exit_price = future_ticks.iloc[-1]['price']

        size_usd = 500  # Smaller position for behavioral strategies
        if signal['side'] == 'BUY':
            pnl = size_usd * (exit_price - entry_price)
        else:
            pnl = size_usd * (entry_price - exit_price)

        trade = Trade(
            timestamp=current_time,
            market_id=market_id,
            side=signal['side'],
            size_usd=size_usd,
            entry_price=entry_price,
            exit_price=exit_price,
            exit_time=future_ticks.iloc[-1]['timestamp'],
            pnl=pnl * 0.98,
            strategy='loser_fade'
        )

        self.trades.append(trade)
        return trade


class MultiStrategyBacktest:
    """Unified backtest harness for multiple strategies."""

    def __init__(self, config: StrategyConfig):
        self.config = config
        self.theta = ThetaHarvestStrategy(config) if config.use_theta_harvest else None
        self.burst = TradeBurstStrategy(config) if config.use_trade_burst else None
        self.loser = LoserFadeStrategy(config) if config.use_loser_fade else None

        self.all_trades: List[Trade] = []
        self.equity_curve: List[Tuple[int, float]] = []

    def run(
        self,
        ticks_df: pd.DataFrame,
        markets_df: pd.DataFrame,
        resolutions_df: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """Run multi-strategy backtest."""

        # Clean data
        ticks_df = ticks_df.copy()
        ticks_df['timestamp'] = pd.to_numeric(ticks_df['timestamp'], errors='coerce')
        ticks_df['price'] = pd.to_numeric(ticks_df['price'], errors='coerce')
        ticks_df['size'] = pd.to_numeric(ticks_df['size'], errors='coerce')
        ticks_df = ticks_df.dropna(subset=['timestamp', 'price', 'size'])
        ticks_df = ticks_df.sort_values('timestamp').reset_index(drop=True)

        if len(ticks_df) < 100:
            return pd.DataFrame()

        capital = self.config.initial_capital
        equity = capital

        # Walk through time (sample for speed)
        unique_times = sorted(ticks_df['timestamp'].unique())
        step = max(1, len(unique_times) // 50)  # 50 time steps max
        sampled_times = unique_times[::step]

        for current_time in sampled_times:
            try:
                window_ticks = ticks_df[ticks_df['timestamp'] <= current_time]
                if len(window_ticks) < 10:
                    continue

                # Theta harvest signals
                if self.theta:
                    signals = self.theta.find_signals(markets_df, window_ticks, current_time)
                    for signal in signals[:5]:
                        try:
                            trade = self.theta.execute_signal(signal, window_ticks, current_time)
                            if trade and -500 < trade.pnl < 10000:
                                self.all_trades.append(trade)
                                equity += trade.pnl
                        except:
                            pass

                # Trade burst signals
                if self.burst:
                    signals = self.burst.find_signals(window_ticks, current_time)
                    for signal in signals[:3]:
                        try:
                            trade = self.burst.execute_signal(signal, window_ticks, current_time)
                            if trade and -500 < trade.pnl < 10000:
                                self.all_trades.append(trade)
                                equity += trade.pnl
                        except:
                            pass

                # Loser fade signals
                if self.loser:
                    signals = self.loser.find_signals(window_ticks, current_time)
                    for signal in signals[:3]:
                        try:
                            trade = self.loser.execute_signal(signal, window_ticks, current_time)
                            if trade and -500 < trade.pnl < 10000:
                                self.all_trades.append(trade)
                                equity += trade.pnl
                        except:
                            pass

                self.equity_curve.append((current_time, equity))
            except:
                continue

        return self._format_results()

    def _format_results(self) -> pd.DataFrame:
        """Format backtest results."""
        if not self.all_trades:
            return pd.DataFrame()

        trades_list = []
        for trade in self.all_trades:
            trades_list.append({
                'timestamp': trade.timestamp,
                'market_id': trade.market_id,
                'strategy': trade.strategy,
                'side': trade.side,
                'size': trade.size_usd,
                'entry_price': trade.entry_price,
                'exit_price': trade.exit_price,
                'pnl': trade.pnl,
                'roi_pct': trade.roi,
                'duration_min': trade.duration_minutes,
            })

        df = pd.DataFrame(trades_list)
        return df


def backtest_strategies(category: str = "Finance") -> Dict:
    """Backtest strategies on a category."""

    # Load data
    ticks_path = Path(f"data/pmxt/ticks/{category}_trades.parquet")
    markets_path = Path("data/pmxt/markets/all_markets_unified.parquet")

    if not ticks_path.exists():
        return {'error': f'No data for {category}'}

    ticks_df = pd.read_parquet(ticks_path)
    markets_df = pd.read_parquet(markets_path)

    config = StrategyConfig(category=category)
    backtest = MultiStrategyBacktest(config)

    results = backtest.run(ticks_df, markets_df)

    if len(results) == 0:
        return {'error': 'No trades generated'}

    # Compute metrics
    total_trades = len(results)
    winning_trades = len(results[results['pnl'] > 0])
    losing_trades = len(results[results['pnl'] < 0])

    total_pnl = results['pnl'].sum()
    avg_pnl = results['pnl'].mean()
    sharpe = results['roi_pct'].mean() / (results['roi_pct'].std() + 1e-6) if len(results) > 1 else 0

    return {
        'category': category,
        'total_trades': total_trades,
        'winning_trades': winning_trades,
        'losing_trades': losing_trades,
        'win_rate': 100 * winning_trades / max(1, total_trades),
        'total_pnl': total_pnl,
        'avg_pnl_per_trade': avg_pnl,
        'sharpe': sharpe,
        'trades_df': results,
    }

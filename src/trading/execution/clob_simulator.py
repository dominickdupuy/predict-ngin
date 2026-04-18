"""
CLOB Simulator — Realistic Execution with Market Impact & Slippage

Estimates book depth from trade data, applies realistic market-impact & spread costs.
Uses square-root impact model (standard in microstructure literature).
"""

import pandas as pd
import numpy as np
from typing import Dict, Tuple, Optional
from dataclasses import dataclass

@dataclass
class ExecutionResult:
    """Result of a simulated trade execution."""
    filled: bool
    fill_price: float
    avg_price: float
    slippage_bps: float
    taker_fee_bps: float = 20
    market_impact_bps: float = 0.0
    spread_bps: float = 0.0
    total_cost_bps: float = 0.0
    execution_note: str = ""


class CLOBSimulator:
    """
    Simulate realistic CLOB execution on Polymarket.

    Key features:
    - Estimates book depth from historical trade data
    - Stratifies by liquidity tier (top-100, medium, long-tail)
    - Applies market-impact formula: impact = coeff * sqrt(size / depth)
    - Models spread as function of depth
    - Tracks fill prices realistically
    """

    # Empirically-calibrated parameters by liquidity tier
    LIQUIDITY_TIERS = {
        'liquid': {
            'volume_threshold': 500_000,  # >$500k
            'spread_bps': 10,  # 1¢ at price 0.50
            'impact_coeff': 0.001,  # Low impact
            'min_depth': 100_000,
        },
        'medium': {
            'volume_threshold': 100_000,  # $100k–$500k
            'spread_bps': 30,  # 3¢ at price 0.50
            'impact_coeff': 0.003,
            'min_depth': 20_000,
        },
        'illiquid': {
            'volume_threshold': 0,  # <$100k
            'spread_bps': 100,  # 10¢ at price 0.50
            'impact_coeff': 0.010,
            'min_depth': 5_000,
        },
    }

    def __init__(self, trades_df: pd.DataFrame):
        """Initialize with historical trades; estimate book depth."""
        self.trades_df = trades_df
        self.market_stats = {}
        self.estimate_market_stats()

    def estimate_market_stats(self) -> None:
        """
        Estimate per-market: depth, spread, liquidity tier.

        Inferred from:
        - Total volume traded (proxy for market depth)
        - Price clustering (proxy for bid-ask spread)
        - Trade frequency (proxy for liquidity)
        """
        for market_id in self.trades_df['conditionId'].unique():
            market_trades = self.trades_df[self.trades_df['conditionId'] == market_id]

            # 1. Total volume = proxy for depth
            total_volume = market_trades['size'].sum()

            # 2. Estimate spread from price variation
            # Bid-ask bounce causes discrete price clustering
            prices = market_trades['price']
            price_changes = prices.diff().abs()
            typical_tick = price_changes[price_changes > 0].median()
            if np.isnan(typical_tick) or typical_tick == 0:
                typical_tick = 0.01

            # 3. Trade frequency = liquidity freshness
            trade_freq = len(market_trades)

            # 4. Determine tier
            tier = self._get_tier(total_volume)

            self.market_stats[market_id] = {
                'total_volume': total_volume,
                'estimated_depth': max(total_volume * 0.2, self.LIQUIDITY_TIERS[tier]['min_depth']),
                'spread_bps': self.LIQUIDITY_TIERS[tier]['spread_bps'],
                'impact_coeff': self.LIQUIDITY_TIERS[tier]['impact_coeff'],
                'tier': tier,
                'typical_tick': typical_tick,
                'trade_freq': trade_freq,
            }

    def _get_tier(self, volume: float) -> str:
        """Classify market by volume."""
        if volume >= self.LIQUIDITY_TIERS['liquid']['volume_threshold']:
            return 'liquid'
        elif volume >= self.LIQUIDITY_TIERS['medium']['volume_threshold']:
            return 'medium'
        else:
            return 'illiquid'

    def execute(
        self,
        market_id: str,
        side: str,
        size_usd: float,
        mid_price: float,
        timestamp: Optional[int] = None,
    ) -> ExecutionResult:
        """
        Simulate order execution against CLOB.

        Args:
            market_id: Market identifier
            side: 'BUY' or 'SELL'
            size_usd: Dollar amount to trade
            mid_price: Current market mid (0.00–1.00)
            timestamp: (unused, for future depth-varying models)

        Returns:
            ExecutionResult with fill price, slippage breakdown, costs
        """

        if market_id not in self.market_stats:
            # Unknown market, use illiquid tier
            stats = {
                'estimated_depth': 5_000,
                'spread_bps': 100,
                'impact_coeff': 0.010,
                'tier': 'illiquid',
            }
        else:
            stats = self.market_stats[market_id]

        # 1. Market impact (in BPS)
        # impact_bps = impact_coeff * sqrt(size / depth) * 10,000
        # This formula comes from Almgren & Chriss (market microstructure)
        impact_bps = (
            stats['impact_coeff']
            * np.sqrt(size_usd / stats['estimated_depth'])
            * 10_000
        )
        impact_bps = min(impact_bps, 500)  # Cap at 500bps (50% of price) to avoid blow-ups

        # 2. Spread (half-spread paid on entry)
        spread_bps = stats['spread_bps']

        # 3. Taker fee
        taker_fee_bps = 20  # 0.2%

        # 4. Total cost in BPS
        total_cost_bps = spread_bps + impact_bps + taker_fee_bps

        # 5. Fill price
        # For a BUY: you cross the ask, so you pay spread + impact
        # For a SELL: you cross the bid, so you receive less
        if side == 'BUY':
            fill_price = mid_price * (1 + total_cost_bps / 10_000)
        else:  # SELL
            fill_price = mid_price * (1 - total_cost_bps / 10_000)

        # 6. Check if we stay within bounds [0, 1]
        if fill_price < 0 or fill_price > 1:
            return ExecutionResult(
                filled=False,
                fill_price=fill_price,
                avg_price=mid_price,
                slippage_bps=total_cost_bps,
                execution_note=f"Fill price {fill_price:.4f} out of bounds",
            )

        return ExecutionResult(
            filled=True,
            fill_price=fill_price,
            avg_price=fill_price,
            slippage_bps=total_cost_bps,
            market_impact_bps=impact_bps,
            spread_bps=spread_bps,
            total_cost_bps=total_cost_bps,
            execution_note=f"Tier: {stats['tier']}, Depth est: ${stats['estimated_depth']:,.0f}",
        )

    def backtest_cost_impact(self, market_id: str, size_usd: float, mid_price: float = 0.50) -> Dict:
        """Show cost breakdown for a given trade."""
        result = self.execute(market_id, 'BUY', size_usd, mid_price)

        return {
            'market_id': market_id,
            'size_usd': size_usd,
            'tier': self.market_stats.get(market_id, {}).get('tier', 'unknown'),
            'spread_bps': result.spread_bps,
            'market_impact_bps': result.market_impact_bps,
            'taker_fee_bps': result.taker_fee_bps,
            'total_cost_bps': result.total_cost_bps,
            'cost_dollars': size_usd * result.total_cost_bps / 10_000,
            'pct_of_capital': 100 * result.total_cost_bps / 10_000,
        }


# ─────────────────────────────────────────────────────────────────────────────
# UTILITIES FOR BACKTEST INTEGRATION
# ─────────────────────────────────────────────────────────────────────────────

def apply_slippage_to_pnl(
    entry_price: float,
    exit_price: float,
    size_usd: float,
    entry_slippage_bps: float,
    exit_slippage_bps: float,
) -> float:
    """
    Calculate PnL after realistic slippage.

    Args:
        entry_price: Intended entry price
        exit_price: Intended exit price
        size_usd: Position size in dollars
        entry_slippage_bps: Entry cost in basis points
        exit_slippage_bps: Exit cost in basis points

    Returns:
        Realistic PnL after slippage
    """
    # Actual entry price (worse than mid)
    actual_entry = entry_price * (1 + entry_slippage_bps / 10_000)

    # Actual exit price (worse than mid)
    actual_exit = exit_price * (1 - exit_slippage_bps / 10_000)

    # PnL in dollars (per unit)
    pnl_per_unit = actual_exit - actual_entry

    # Total PnL
    total_pnl = pnl_per_unit * size_usd

    return total_pnl


def estimate_capacity(
    market_id: str,
    strategy_edge_bps: float,
    clob_simulator: CLOBSimulator,
) -> Dict:
    """
    Estimate max position size before slippage eats all edge.

    Args:
        market_id: Target market
        strategy_edge_bps: Pre-cost edge in basis points
        clob_simulator: CLOB simulator with market stats

    Returns:
        Dict with max size, breakeven analysis
    """
    stats = clob_simulator.market_stats.get(market_id, {})
    depth = stats.get('estimated_depth', 10_000)

    # Maximum size where cost ~= edge
    # Solve: impact_coeff * sqrt(size / depth) * 10k = edge_bps
    # => size = depth * (edge_bps / (impact_coeff * 10k))^2

    impact_coeff = stats.get('impact_coeff', 0.003)
    max_size = depth * (strategy_edge_bps / (impact_coeff * 10_000)) ** 2

    return {
        'market_id': market_id,
        'tier': stats.get('tier', 'unknown'),
        'depth_usd': depth,
        'strategy_edge_bps': strategy_edge_bps,
        'max_position_usd': max(1_000, max_size),
        'note': f"Position larger than this will have slippage > edge",
    }

"""
Liquidity-aware CLOB execution module.

Tracks real order-book depth and prevents over-commitment when liquidity is exhausted.
Simulates realistic fills accounting for slippage, partial fills, and own consumption.
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from datetime import datetime


@dataclass
class OrderBookSnapshot:
    """Point-in-time order-book state."""
    timestamp: int  # Unix milliseconds
    market_id: str
    bid_prices: np.ndarray  # [P1, P2, ..., P50] (highest first)
    bid_sizes: np.ndarray  # [S1, S2, ..., S50]
    ask_prices: np.ndarray  # [P1, P2, ..., P50] (lowest first)
    ask_sizes: np.ndarray  # [S1, S2, ..., S50]

    def mid_price(self) -> float:
        """Mid price."""
        return (self.bid_prices[0] + self.ask_prices[0]) / 2.0

    def spread_bps(self) -> float:
        """Spread in basis points."""
        mid = self.mid_price()
        if mid == 0:
            return 0.0
        return 10000 * (self.ask_prices[0] - self.bid_prices[0]) / mid


@dataclass
class ExecutionResult:
    """Result of attempting to execute a position."""
    success: bool
    filled_size: float  # Amount actually filled
    average_price: float  # Weighted average execution price
    slippage_bps: int  # Slippage in basis points vs mid
    liquidity_consumed_bps: float  # % of market liquidity consumed on best level
    remaining_available: float  # Size that could still be filled at worse prices
    message: str  # Reason for partial/failed fill


class CLOBLiquidityManager:
    """
    Tracks order-book state and provides realistic execution fills.

    Key behaviors:
    - Tracks consumed liquidity from our own trades
    - Rejects size requests that exceed available liquidity
    - Models slippage based on book depth
    - Prevents double-counting of our own capital
    """

    def __init__(self):
        """Initialize liquidity tracker."""
        self.snapshots: Dict[str, OrderBookSnapshot] = {}  # market_id -> latest snapshot
        self.our_positions: Dict[str, float] = defaultdict(float)  # market_id -> net position USD
        self.consumed_liquidity: Dict[str, Dict[str, float]] = defaultdict(lambda: {
            'bid_consumed': 0.0,  # Our BUY orders that consumed asks
            'ask_consumed': 0.0,  # Our SELL orders that consumed bids
        })
        self.trade_history: List[Dict] = []

    def update_book(self, snapshot: OrderBookSnapshot) -> None:
        """Update order-book state for a market."""
        self.snapshots[snapshot.market_id] = snapshot

    def can_execute(
        self,
        market_id: str,
        side: str,  # "BUY" or "SELL"
        size_usd: float,
        max_price_impact_bps: int = 500,  # Max 5% slippage allowed
    ) -> Tuple[bool, str]:
        """Check if position can be executed without excessive slippage."""
        if market_id not in self.snapshots:
            return False, "No book snapshot available"

        book = self.snapshots[market_id]
        result = self._simulate_execution(book, side, size_usd)

        if not result.success:
            return False, result.message

        if result.slippage_bps > max_price_impact_bps:
            return False, f"Slippage {result.slippage_bps} bps exceeds limit {max_price_impact_bps}"

        # Check that we have enough liquidity
        if result.filled_size < size_usd * 0.95:  # Allow 5% shortfall max
            return False, f"Only {result.filled_size:.0f}/{size_usd:.0f} available"

        return True, "OK"

    def execute(
        self,
        market_id: str,
        side: str,
        size_usd: float,
        timestamp: int,
        order_type: str = "market",  # "market" or "limit"
        limit_price: Optional[float] = None,
    ) -> ExecutionResult:
        """
        Execute a trade against the order book.

        Returns: ExecutionResult with fill price, size, and slippage metrics.
        """
        if market_id not in self.snapshots:
            return ExecutionResult(
                success=False, filled_size=0, average_price=0,
                slippage_bps=0, liquidity_consumed_bps=0, remaining_available=0,
                message="No book snapshot"
            )

        book = self.snapshots[market_id]
        result = self._simulate_execution(book, side, size_usd, limit_price)

        if result.success and result.filled_size > 0:
            # Record the trade
            self.trade_history.append({
                'timestamp': timestamp,
                'market_id': market_id,
                'side': side,
                'size': result.filled_size,
                'price': result.average_price,
                'slippage_bps': result.slippage_bps,
            })

            # Update our position
            if side == "BUY":
                self.our_positions[market_id] += result.filled_size
                self.consumed_liquidity[market_id]['bid_consumed'] += result.filled_size
            else:  # SELL
                self.our_positions[market_id] -= result.filled_size
                self.consumed_liquidity[market_id]['ask_consumed'] += result.filled_size

        return result

    def _simulate_execution(
        self,
        book: OrderBookSnapshot,
        side: str,
        size_usd: float,
        limit_price: Optional[float] = None,
    ) -> ExecutionResult:
        """Simulate execution against book depth."""

        if side == "BUY":
            # Buying YES means hitting asks
            prices = book.ask_prices  # Ascending
            sizes = book.ask_sizes
        else:  # SELL
            # Selling YES means hitting bids (highest first, so reverse)
            prices = book.bid_prices[::-1]  # Descending to ascending
            sizes = book.bid_sizes[::-1]

        total_filled = 0.0
        total_cost = 0.0
        levels_needed = 0

        # Walk the book
        for level in range(min(len(prices), 50)):
            if total_filled >= size_usd:
                break

            price = prices[level]
            size = sizes[level]

            # Apply limit price check
            if limit_price is not None:
                if side == "BUY" and price > limit_price:
                    break
                if side == "SELL" and price < limit_price:
                    break

            # Don't consume liquidity we've already used
            if side == "BUY":
                consumed = self.consumed_liquidity[book.market_id]['bid_consumed']
            else:
                consumed = self.consumed_liquidity[book.market_id]['ask_consumed']

            # Conservative: reduce available size by what we've already taken
            size = max(0, size - consumed)

            # How much can we take at this level?
            amount_at_level = min(size, size_usd - total_filled)

            if amount_at_level <= 0:
                continue

            total_filled += amount_at_level
            total_cost += amount_at_level * price
            levels_needed += 1

        if total_filled == 0:
            return ExecutionResult(
                success=False, filled_size=0, average_price=0,
                slippage_bps=0, liquidity_consumed_bps=0, remaining_available=0,
                message=f"No liquidity available on {side} side"
            )

        avg_price = total_cost / total_filled
        mid = book.mid_price()

        # Slippage calculation
        if side == "BUY":
            slippage_bps = int(10000 * (avg_price - mid) / mid) if mid > 0 else 0
        else:  # SELL
            slippage_bps = int(10000 * (mid - avg_price) / mid) if mid > 0 else 0

        # Liquidity consumed %
        first_level_size = book.bid_sizes[0] if side == "SELL" else book.ask_sizes[0]
        liquidity_consumed_bps = 10000 * (total_filled / first_level_size) if first_level_size > 0 else 0

        # Can we get more at worse prices?
        remaining = 0.0
        for level in range(levels_needed, min(len(sizes), 50)):
            remaining += sizes[level]

        return ExecutionResult(
            success=True,
            filled_size=total_filled,
            average_price=avg_price,
            slippage_bps=slippage_bps,
            liquidity_consumed_bps=liquidity_consumed_bps,
            remaining_available=remaining,
            message="Filled"
        )

    def get_position(self, market_id: str) -> float:
        """Get current position in market (USD equivalent)."""
        return self.our_positions.get(market_id, 0.0)

    def get_pnl(self, market_id: str, current_price: float) -> float:
        """Estimate PnL at current price."""
        position = self.get_position(market_id)
        if position == 0:
            return 0.0

        # Rough estimate: position * (current_price - avg_entry)
        # Actual calculation would need trade history
        return position  # Placeholder

    def reset_consumed_liquidity(self, market_id: str) -> None:
        """Reset tracked consumption for a market (e.g., new book snapshot)."""
        self.consumed_liquidity[market_id] = {
            'bid_consumed': 0.0,
            'ask_consumed': 0.0,
        }

    def get_available_liquidity(self, market_id: str, side: str, depth: int = 5) -> float:
        """Get available liquidity (USD) at top N levels."""
        if market_id not in self.snapshots:
            return 0.0

        book = self.snapshots[market_id]

        if side == "BUY":
            sizes = book.ask_sizes[:depth]
            consumed = self.consumed_liquidity[market_id]['bid_consumed']
        else:
            sizes = book.bid_sizes[:depth]
            consumed = self.consumed_liquidity[market_id]['ask_consumed']

        # Sum available, accounting for our consumption
        return max(0.0, float(np.sum(sizes)) - consumed)

    def summary_stats(self, market_id: str) -> Dict:
        """Get summary statistics for a market."""
        if market_id not in self.snapshots:
            return {}

        book = self.snapshots[market_id]
        position = self.get_position(market_id)

        return {
            'market_id': market_id,
            'timestamp': book.timestamp,
            'mid_price': book.mid_price(),
            'spread_bps': book.spread_bps(),
            'bid_size_L1': book.bid_sizes[0],
            'ask_size_L1': book.ask_sizes[0],
            'our_position': position,
            'consumed_bid': self.consumed_liquidity[market_id]['bid_consumed'],
            'consumed_ask': self.consumed_liquidity[market_id]['ask_consumed'],
            'trades_executed': len([t for t in self.trade_history if t['market_id'] == market_id]),
        }

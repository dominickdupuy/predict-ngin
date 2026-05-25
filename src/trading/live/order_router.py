"""
Order Router for Polymarket CLOB

Routes orders to Polymarket's Central Limit Order Book (CLOB).
Handles authentication, order creation, and execution.

Authentication:
    Polymarket CLOB uses EIP-712 typed-data signing, NOT HMAC.
    Orders must be signed with a Polygon wallet private key.
    The recommended approach is the official py-clob-client library.

    Install:  pip install py-clob-client
    Docs:     https://github.com/Polymarket/py-clob-client

    Required environment variables:
        POLYMARKET_PRIVATE_KEY    Polygon wallet private key (0x...)
        POLYMARKET_API_KEY        L2 API key (from CLOB /auth/api-key)
        POLYMARKET_API_SECRET     L2 API secret
        POLYMARKET_API_PASSPHRASE L2 API passphrase
        POLYMARKET_CHAIN_ID       137 (Polygon mainnet) or 80002 (Amoy testnet)

    Obtaining L2 credentials:
        1. Fund a Polygon wallet with USDC
        2. Approve USDC on the CTF Exchange contract
        3. Call POST /auth/api-key signed with your private key to generate L2 keys

    Dry-run mode (default, no credentials needed):
        router = OrderRouter(dry_run=True)
        # Simulates order execution using real order book prices

Usage:
    from src.trading.live.order_router import OrderRouter, OrderSide

    # Paper/dry-run (default)
    router = OrderRouter(dry_run=True)
    result = router.place_market_order(token_id, OrderSide.BUY, size_usd=100)

    # Live (requires py-clob-client + credentials)
    router = OrderRouter(dry_run=False)
    result = router.place_market_order(token_id, OrderSide.BUY, size_usd=100)

Reference:
- Polymarket CLOB docs: https://docs.polymarket.com
- py-clob-client: https://github.com/Polymarket/py-clob-client
"""

import os
import time
import json
import requests
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# API endpoints
CLOB_API  = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# Rate limiting
REQUEST_DELAY = 0.1
MAX_RETRIES   = 3


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    GTC = "gtc"  # Good-til-cancelled
    FOK = "fok"  # Fill-or-kill


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    LIVE = "live"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    MATCHED = "matched"


@dataclass
class OrderResult:
    """Result of an order submission."""
    success: bool
    order_id: Optional[str] = None
    status: Optional[str] = None
    filled_size: float = 0
    avg_price: float = 0
    fees: float = 0
    error: Optional[str] = None
    raw_response: Optional[Dict] = None


@dataclass
class OrderBook:
    """Order book snapshot."""
    token_id: str
    timestamp: datetime
    bids: List[Tuple[float, float]]  # (price, size)
    asks: List[Tuple[float, float]]
    best_bid: float
    best_ask: float
    spread: float
    midpoint: float


def _build_clob_client():
    """
    Build a py-clob-client ClobClient using environment variables.

    Returns the client instance, or None if py-clob-client is not installed
    or credentials are missing.

    The ClobClient handles all EIP-712 order signing internally.
    """
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError:
        return None

    private_key    = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    api_key        = os.environ.get("POLYMARKET_API_KEY", "")
    api_secret     = os.environ.get("POLYMARKET_API_SECRET", "")
    passphrase     = os.environ.get("POLYMARKET_API_PASSPHRASE", "")
    chain_id       = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))
    sig_type       = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
    funder_address = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "") or None

    if not private_key:
        return None

    try:
        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=passphrase,
        ) if api_key else None

        kwargs = dict(
            host=CLOB_API,
            key=private_key,
            chain_id=chain_id,
            creds=creds,
            signature_type=sig_type,
        )
        if funder_address:
            kwargs["funder"] = funder_address

        client = ClobClient(**kwargs)
        return client
    except Exception as e:
        print(f"Warning: could not initialise ClobClient: {e}")
        return None


class OrderRouter:
    """
    Routes orders to Polymarket CLOB.

    Supports market orders, limit orders, and order management.
    """

    def __init__(self, dry_run: bool = True):
        """
        Initialize order router.

        Args:
            dry_run: If True, simulate execution without submitting real orders.
                     Real orders require py-clob-client + env vars:
                     POLYMARKET_PRIVATE_KEY, POLYMARKET_API_KEY,
                     POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE,
                     POLYMARKET_CHAIN_ID (default 137)
        """
        self.dry_run = dry_run
        self._clob_client = None if dry_run else _build_clob_client()

        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent":   "PredictionMarketTrader/1.0",
        })

        # Order tracking
        self.pending_orders: Dict[str, Dict] = {}
        self.filled_orders: List[Dict] = []
        self._dry_order_counter = 0

    def _next_dry_order_id(self, prefix: str) -> str:
        self._dry_order_counter += 1
        return f"{prefix}-{self._dry_order_counter:06d}"

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
        authenticated: bool = False,
    ) -> Tuple[bool, Dict]:
        """Make API request with optional authentication."""
        url = f"{CLOB_API}{endpoint}"
        body = json.dumps(data) if data else ""

        headers = {}
        if authenticated and self.auth:
            headers.update(self.auth.sign_request(method, endpoint, body))

        try:
            time.sleep(REQUEST_DELAY)

            if method == "GET":
                response = self.session.get(url, headers=headers, timeout=10)
            elif method == "POST":
                response = self.session.post(url, data=body, headers=headers, timeout=10)
            elif method == "DELETE":
                response = self.session.delete(url, headers=headers, timeout=10)
            else:
                return False, {"error": f"Unknown method: {method}"}

            if response.status_code in [200, 201]:
                return True, response.json() if response.text else {}
            else:
                return False, {
                    "error": f"HTTP {response.status_code}",
                    "detail": response.text,
                }

        except requests.RequestException as e:
            return False, {"error": str(e)}

    def is_authenticated(self) -> bool:
        """Check if live credentials are available."""
        if self.dry_run:
            return True
        return self._clob_client is not None

    def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """
        Get order book for a token.

        Args:
            token_id: CLOB token ID

        Returns:
            OrderBook or None
        """
        success, data = self._request("GET", f"/book?token_id={token_id}")

        if not success:
            print(f"Failed to get order book: {data.get('error')}")
            return None

        bids = [(float(b["price"]), float(b["size"])) for b in data.get("bids", [])]
        asks = [(float(a["price"]), float(a["size"])) for a in data.get("asks", [])]

        best_bid = bids[0][0] if bids else 0
        best_ask = asks[0][0] if asks else 1

        return OrderBook(
            token_id=token_id,
            timestamp=datetime.now(),
            bids=bids,
            asks=asks,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=round(best_ask - best_bid, 6),
            midpoint=(best_bid + best_ask) / 2,
        )

    def get_best_price(self, token_id: str, side: OrderSide) -> Optional[float]:
        """Get best available price for a side."""
        book = self.get_order_book(token_id)
        if not book:
            return None

        if side == OrderSide.BUY:
            return book.best_ask  # Buy at ask
        else:
            return book.best_bid  # Sell at bid

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price."""
        book = self.get_order_book(token_id)
        return book.midpoint if book else None

    def estimate_fill_price(
        self,
        token_id: str,
        side: OrderSide,
        size_usd: float,
    ) -> Tuple[float, float]:
        """
        Estimate average fill price for a size.

        Returns:
            (estimated_avg_price, estimated_slippage)
        """
        book = self.get_order_book(token_id)
        if not book:
            return 0, 0

        levels = book.asks if side == OrderSide.BUY else book.bids
        if not levels:
            return 0, 0

        # Walk the book
        remaining = size_usd
        total_cost = 0

        for price, size in levels:
            level_value = price * size
            if remaining <= level_value:
                total_cost += remaining
                remaining = 0
                break
            else:
                total_cost += level_value
                remaining -= level_value

        if size_usd - remaining > 0:
            avg_price = total_cost / (size_usd - remaining)
        else:
            avg_price = levels[0][0]

        slippage = abs(avg_price - book.midpoint) / book.midpoint

        return avg_price, slippage

    def place_market_order(
        self,
        token_id: str,
        side: OrderSide,
        size_usd: float,
        notes: str = "",
    ) -> OrderResult:
        """
        Place a market order.

        Args:
            token_id: CLOB token ID
            side: BUY or SELL
            size_usd: Order size in USD
            notes: Optional notes

        Returns:
            OrderResult
        """
        if self.dry_run:
            # Simulate execution
            avg_price, slippage = self.estimate_fill_price(token_id, side, size_usd)
            return OrderResult(
                success=True,
                order_id=self._next_dry_order_id("DRY"),
                status="filled",
                filled_size=size_usd,
                avg_price=avg_price,
                fees=size_usd * 0.001,  # Estimated
                error=None,
                raw_response={"dry_run": True, "slippage": slippage},
            )

        if not self.is_authenticated():
            return OrderResult(
                success=False,
                error=(
                    "Not authenticated. Install py-clob-client and set "
                    "POLYMARKET_PRIVATE_KEY env var (and optionally "
                    "POLYMARKET_API_KEY/SECRET/PASSPHRASE for L2 auth)."
                ),
            )

        # Live order via py-clob-client (handles EIP-712 signing internally)
        try:
            from py_clob_client.clob_types import MarketOrderArgs, BUY, SELL

            best_price = self.get_best_price(token_id, side)
            if not best_price:
                return OrderResult(success=False, error="Could not get market price")

            token_size = round(size_usd / best_price, 4)
            clob_side  = BUY if side == OrderSide.BUY else SELL

            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=token_size,
            )
            signed_order = self._clob_client.create_market_order(order_args)
            resp = self._clob_client.post_order(signed_order, clob_side)

            order_id = resp.get("orderID") or resp.get("id", "")
            return OrderResult(
                success=True,
                order_id=str(order_id),
                status=resp.get("status", "submitted"),
                filled_size=float(resp.get("filledSize", size_usd)),
                avg_price=float(resp.get("avgPrice", best_price)),
                fees=float(resp.get("fee", size_usd * 0.001)),
                raw_response=resp,
            )
        except Exception as exc:
            return OrderResult(success=False, error=str(exc))

    def place_limit_order(
        self,
        token_id: str,
        side: OrderSide,
        price: float,
        size_usd: float,
        time_in_force: str = "GTC",
    ) -> OrderResult:
        """
        Place a limit order.

        Args:
            token_id: CLOB token ID
            side: BUY or SELL
            price: Limit price
            size_usd: Order size in USD
            time_in_force: GTC, FOK, etc.

        Returns:
            OrderResult
        """
        if self.dry_run:
            order_id = self._next_dry_order_id("DRY-LMT")
            self.pending_orders[order_id] = {
                "token_id": token_id,
                "side": side.value,
                "price": price,
                "size": size_usd,
                "created_at": datetime.now().isoformat(),
                "status": "live",
            }
            return OrderResult(
                success=True,
                order_id=order_id,
                status="live",
                raw_response={"dry_run": True, "type": "limit"},
            )

        if not self.is_authenticated():
            return OrderResult(
                success=False,
                error="Not authenticated",
            )

        token_size = size_usd / price

        order_data = {
            "tokenID": token_id,
            "side": side.value,
            "size": str(token_size),
            "price": str(price),
            "type": time_in_force,
        }

        success, response = self._request(
            "POST",
            "/order",
            data=order_data,
            authenticated=True,
        )

        if success:
            order_id = response.get("orderID", response.get("id"))
            self.pending_orders[order_id] = {
                "token_id": token_id,
                "side": side.value,
                "price": price,
                "size": size_usd,
                "created_at": datetime.now().isoformat(),
            }
            return OrderResult(
                success=True,
                order_id=order_id,
                status="live",
                raw_response=response,
            )
        else:
            return OrderResult(
                success=False,
                error=response.get("error"),
                raw_response=response,
            )

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if self.dry_run:
            if order_id in self.pending_orders:
                del self.pending_orders[order_id]
            return True

        if not self.is_authenticated():
            return False

        success, response = self._request(
            "DELETE",
            f"/order/{order_id}",
            authenticated=True,
        )

        if success and order_id in self.pending_orders:
            del self.pending_orders[order_id]

        return success

    def cancel_all_orders(self) -> int:
        """Cancel all open orders."""
        cancelled = 0
        for order_id in list(self.pending_orders.keys()):
            if self.cancel_order(order_id):
                cancelled += 1
        return cancelled

    def get_order_status(self, order_id: str) -> Optional[Dict]:
        """Get status of an order."""
        if self.dry_run:
            return self.pending_orders.get(order_id)

        success, response = self._request(
            "GET",
            f"/order/{order_id}",
            authenticated=True,
        )

        return response if success else None

    def get_open_orders(self) -> List[Dict]:
        """Get all open orders."""
        if self.dry_run:
            return list(self.pending_orders.values())

        success, response = self._request(
            "GET",
            "/orders",
            authenticated=True,
        )

        return response.get("orders", []) if success else []

    def get_fills(self, limit: int = 100) -> List[Dict]:
        """Get recent fills."""
        if self.dry_run:
            return self.filled_orders[-limit:]

        success, response = self._request(
            "GET",
            f"/fills?limit={limit}",
            authenticated=True,
        )

        return response.get("fills", []) if success else []


class SmartOrderRouter:
    """
    Smart order routing with execution algorithms.

    Features:
    - TWAP (Time-Weighted Average Price)
    - Iceberg orders
    - Adaptive limit orders
    """

    def __init__(self, router: OrderRouter):
        self.router = router

    def execute_twap(
        self,
        token_id: str,
        side: OrderSide,
        total_size_usd: float,
        duration_minutes: int = 10,
        num_slices: int = 5,
    ) -> List[OrderResult]:
        """
        Execute order as TWAP.

        Splits order into equal slices over time to minimize impact.
        """
        slice_size = total_size_usd / num_slices
        interval = duration_minutes * 60 / num_slices

        results = []

        for i in range(num_slices):
            result = self.router.place_market_order(token_id, side, slice_size)
            results.append(result)

            if i < num_slices - 1:
                time.sleep(interval)

        return results

    def execute_iceberg(
        self,
        token_id: str,
        side: OrderSide,
        total_size_usd: float,
        visible_size_usd: float = 100,
    ) -> List[OrderResult]:
        """
        Execute iceberg order.

        Shows only visible_size at a time, refills on execution.
        """
        results = []
        remaining = total_size_usd

        while remaining > 0:
            slice_size = min(visible_size_usd, remaining)
            result = self.router.place_market_order(token_id, side, slice_size)
            results.append(result)

            if result.success:
                remaining -= result.filled_size
            else:
                break

            # Small delay between slices
            time.sleep(1)

        return results

    def execute_adaptive_limit(
        self,
        token_id: str,
        side: OrderSide,
        size_usd: float,
        max_slippage: float = 0.02,
        timeout_seconds: int = 60,
    ) -> OrderResult:
        """
        Place adaptive limit order.

        Starts at midpoint, adjusts toward market if unfilled.
        """
        book = self.router.get_order_book(token_id)
        if not book:
            return OrderResult(success=False, error="Cannot get order book")

        # Start at favorable price
        if side == OrderSide.BUY:
            start_price = book.midpoint * (1 - max_slippage / 2)
            final_price = book.best_ask * (1 + max_slippage)
        else:
            start_price = book.midpoint * (1 + max_slippage / 2)
            final_price = book.best_bid * (1 - max_slippage)

        # Place initial order
        result = self.router.place_limit_order(
            token_id, side, start_price, size_usd, "GTC"
        )

        if not result.success:
            return result

        # Monitor and adjust
        start_time = time.time()
        order_id = result.order_id

        while time.time() - start_time < timeout_seconds:
            status = self.router.get_order_status(order_id)

            if status and status.get("status") == "filled":
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    status="filled",
                    filled_size=size_usd,
                    avg_price=start_price,
                )

            # Adjust price toward market
            elapsed_ratio = (time.time() - start_time) / timeout_seconds
            if side == OrderSide.BUY:
                new_price = start_price + (final_price - start_price) * elapsed_ratio
            else:
                new_price = start_price - (start_price - final_price) * elapsed_ratio

            # Cancel and replace
            self.router.cancel_order(order_id)
            result = self.router.place_limit_order(
                token_id, side, new_price, size_usd, "GTC"
            )
            order_id = result.order_id

            time.sleep(5)

        # Timeout - cancel remaining
        self.router.cancel_order(order_id)
        return OrderResult(
            success=False,
            order_id=order_id,
            status="timeout",
            error="Order timed out",
        )


def main():
    """Test order router."""
    import sys

    # Create router in dry run mode
    router = OrderRouter(dry_run=True)

    if len(sys.argv) < 2:
        print("Order Router Test (Dry Run Mode)")
        print("-" * 40)

        # Get a sample market
        response = requests.get(f"{GAMMA_API}/markets?limit=1&closed=false")
        if response.status_code == 200:
            markets = response.json()
            if markets:
                market = markets[0]
                tokens = market.get("clobTokenIds", [])
                if tokens:
                    token_id = tokens[0]
                    print(f"\nMarket: {market['question'][:50]}...")
                    print(f"Token ID: {token_id}")

                    # Get order book
                    book = router.get_order_book(token_id)
                    if book:
                        print(f"\nOrder Book:")
                        print(f"  Best Bid: {book.best_bid:.3f}")
                        print(f"  Best Ask: {book.best_ask:.3f}")
                        print(f"  Spread: {book.spread:.4f}")
                        print(f"  Midpoint: {book.midpoint:.3f}")

                    # Test market order
                    print(f"\nSimulating $100 BUY order...")
                    result = router.place_market_order(
                        token_id,
                        OrderSide.BUY,
                        100,
                    )
                    print(f"  Success: {result.success}")
                    print(f"  Order ID: {result.order_id}")
                    print(f"  Avg Price: {result.avg_price:.3f}")
                    print(f"  Fees: ${result.fees:.2f}")

    elif sys.argv[1] == "--live":
        print("Live mode requires API credentials.")
        print("Set environment variables:")
        print("  POLYMARKET_API_KEY")
        print("  POLYMARKET_API_SECRET")
        print("  POLYMARKET_PASSPHRASE")


if __name__ == "__main__":
    main()

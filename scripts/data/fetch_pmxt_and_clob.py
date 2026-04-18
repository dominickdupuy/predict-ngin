#!/usr/bin/env python3
"""
High-granularity data fetching: PMXT historical + Polymarket CLOB real-time/tick

Replaces: fetch_polymarket_trades.py, fetch_research_trades_and_prices.py

Data pipeline:
  1. PMXT API: 1-min OHLCV + order-book snapshots (historical)
  2. CLOB WebSocket: raw tick trades + order-book depth (live)
  3. Parquet storage: tick-level schema optimized for microsecond timestamps

Usage:
    python scripts/data/fetch_pmxt_and_clob.py \
        --categories Politics,Geopolitics,Economy \
        --mode all  # [ohlcv, ticks, orderbook, all]
"""

import asyncio
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, AsyncIterator
import sys

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("fetch_pmxt_clob")

# ── Config ────────────────────────────────────────────────────────────────────
CATEGORIES = [
    "Art_and_Culture", "Climate_and_Science", "Economy",
    "Finance", "Geopolitics", "Other", "Politics",
    "Sports", "Tech"  # Added: large historical datasets
]

PMXT_BASE = "https://api.pmxt.dev"
CLOB_WS = "wss://ws.clob.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

DATA_ROOT = Path("data/pmxt")
OHLCV_ROOT = DATA_ROOT / "ohlcv"
TICK_ROOT = DATA_ROOT / "ticks"
ORDERBOOK_ROOT = DATA_ROOT / "orderbook"

for root in [OHLCV_ROOT, TICK_ROOT, ORDERBOOK_ROOT]:
    root.mkdir(parents=True, exist_ok=True)


# ── Data Schemas ──────────────────────────────────────────────────────────────

@dataclass
class OHLCVRecord:
    """1-minute candle from PMXT."""
    market_id: str
    outcome_index: int  # 0=NO, 1=YES
    timestamp: int  # Unix ms
    open: float
    high: float
    low: float
    close: float
    volume: float  # in base currency


@dataclass
class TickRecord:
    """Single trade tick — highest granularity."""
    market_id: str
    condition_id: str
    outcome_index: int  # 0=NO, 1=YES
    timestamp: int  # Unix ms
    price: float  # normalized to YES side
    size: float  # in USD or tokens
    side: str  # "BUY" or "SELL"
    taker_address: str
    maker_address: Optional[str]
    trade_hash: str  # unique identifier


@dataclass
class OrderBookSnapshot:
    """Order book state at a point in time."""
    market_id: str
    condition_id: str
    outcome_index: int
    timestamp: int  # Unix ms

    # Bid side (ascending price)
    bid_prices: List[float]
    bid_sizes: List[float]

    # Ask side (descending price)
    ask_prices: List[float]
    ask_sizes: List[float]

    # Derived
    mid_price: float
    spread: float


# ── PMXT Fetcher (Historical) ────────────────────────────────────────────────

class PMXTFetcher:
    """Fetch OHLCV and orderbook from PMXT archive."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self.session = None

    async def fetch_1m_ohlcv(
        self,
        market_id: str,
        start_date: str = None,
        end_date: str = None,
        limit: int = None,
    ) -> pd.DataFrame:
        """
        Fetch 1-minute OHLCV candles.

        Args:
            market_id: Polymarket condition ID
            start_date: "YYYY-MM-DD" or Unix timestamp
            end_date: "YYYY-MM-DD" or Unix timestamp
            limit: max candles to return

        Returns:
            DataFrame with [timestamp, open, high, low, close, volume]
        """
        # PMXT endpoint: /markets/{market_id}/ohlcv
        # Params: timeframe=1m, since=ts_ms, limit=1000

        params = {"timeframe": "1m"}
        if start_date:
            params["since"] = start_date
        if end_date:
            params["until"] = end_date
        if limit:
            params["limit"] = min(limit, 1000)  # PMXT paginate at 1000

        url = f"{PMXT_BASE}/markets/{market_id}/ohlcv"

        # TODO: implement async HTTP fetch (aiohttp or httpx)
        # For now, placeholder:
        log.info(f"Would fetch {url} with {params}")

        return pd.DataFrame()

    async def fetch_orderbook_snapshot(
        self,
        market_id: str,
        timestamp: int,
    ) -> OrderBookSnapshot:
        """Fetch order book at specific timestamp (hourly resolution in archive)."""
        # PMXT archive: /markets/{market_id}/orderbook?timestamp={ts}
        # Returns: {bids: [[price, size], ...], asks: [...]}
        log.info(f"Would fetch orderbook for {market_id} at {timestamp}")
        return None


# ── CLOB WebSocket Streamer (Real-time Ticks) ───────────────────────────────

class CLOBStreamer:
    """Real-time trade ticks and order-book updates via Polymarket CLOB WebSocket."""

    def __init__(self, market_ids: List[str]):
        self.market_ids = market_ids
        self.ws_url = CLOB_WS

    async def stream_trades(self) -> AsyncIterator[TickRecord]:
        """
        Connect to CLOB trade stream.

        WS endpoint: wss://ws.clob.polymarket.com/trades
        Message format: {market_id, price, size, side, timestamp, ...}
        """
        import websockets

        try:
            async with websockets.connect(self.ws_url) as ws:
                # Subscribe to specific markets
                subscribe_msg = {
                    "action": "subscribe",
                    "type": "trades",
                    "markets": self.market_ids,
                }
                await ws.send(json.dumps(subscribe_msg))

                async for message in ws:
                    data = json.loads(message)

                    # Parse CLOB trade format
                    tick = TickRecord(
                        market_id=data["market"],
                        condition_id=data["condition_id"],
                        outcome_index=data["outcome_index"],
                        timestamp=int(data["timestamp"] * 1000),  # Convert to ms
                        price=float(data["price"]),
                        size=float(data["size"]),
                        side=data["side"],  # "BUY" or "SELL"
                        taker_address=data.get("taker", ""),
                        maker_address=data.get("maker", None),
                        trade_hash=data.get("hash", ""),
                    )

                    yield tick

        except Exception as e:
            log.error(f"CLOB trade stream error: {e}")
            raise

    async def stream_orderbook(self) -> AsyncIterator[OrderBookSnapshot]:
        """
        Connect to CLOB order-book stream.

        WS endpoint: wss://ws.clob.polymarket.com/book
        Update frequency: event-driven (on every order-book change)
        Message format: {market_id, bids: [[price, size], ...], asks: [...]}
        """
        import websockets

        try:
            async with websockets.connect(self.ws_url) as ws:
                subscribe_msg = {
                    "action": "subscribe",
                    "type": "book",
                    "markets": self.market_ids,
                    "depth": 50,  # Top 50 levels on each side
                }
                await ws.send(json.dumps(subscribe_msg))

                async for message in ws:
                    data = json.loads(message)

                    bids = data.get("bids", [])
                    asks = data.get("asks", [])

                    # Normalize to price/size tuples
                    bid_prices = [float(p) for p, s in bids]
                    bid_sizes = [float(s) for p, s in bids]
                    ask_prices = [float(p) for p, s in asks]
                    ask_sizes = [float(s) for p, s in asks]

                    mid = (bid_prices[0] + ask_prices[0]) / 2 if bid_prices and ask_prices else 0.5
                    spread = ask_prices[0] - bid_prices[0] if bid_prices and ask_prices else 0

                    snapshot = OrderBookSnapshot(
                        market_id=data["market"],
                        condition_id=data["condition_id"],
                        outcome_index=1,  # YES side
                        timestamp=int(data["timestamp"] * 1000),
                        bid_prices=bid_prices,
                        bid_sizes=bid_sizes,
                        ask_prices=ask_prices,
                        ask_sizes=ask_sizes,
                        mid_price=mid,
                        spread=spread,
                    )

                    yield snapshot

        except Exception as e:
            log.error(f"CLOB book stream error: {e}")
            raise


# ── Parquet Writers (Tick-Optimized Schemas) ──────────────────────────────

class TickParquetWriter:
    """Write high-granularity tick data to Parquet (columnar, compressed)."""

    @staticmethod
    def write_ticks(
        ticks: List[TickRecord],
        market_id: str,
        date: str,  # "YYYY-MM-DD"
    ) -> Path:
        """
        Write tick data to partitioned Parquet file.

        Schema:
          - market_id (string)
          - condition_id (string)
          - outcome_index (uint8)
          - timestamp (int64, Unix ms)
          - price (float32)
          - size (float32)
          - side (string)
          - taker_address (string)
          - maker_address (string, nullable)
          - trade_hash (string)
        """
        df = pd.DataFrame([asdict(t) for t in ticks])

        # Optimize dtypes for compression
        df["outcome_index"] = df["outcome_index"].astype("uint8")
        df["timestamp"] = df["timestamp"].astype("int64")
        df["price"] = df["price"].astype("float32")
        df["size"] = df["size"].astype("float32")

        # Partition by market_id/date
        output_dir = TICK_ROOT / market_id / date[:7]  # YYYY-MM
        output_dir.mkdir(parents=True, exist_ok=True)

        output_path = output_dir / f"ticks_{date}.parquet"

        table = pa.Table.from_pandas(df)
        pq.write_table(
            table,
            output_path,
            compression="snappy",
            use_dictionary=True,  # Compress strings (side, addresses)
        )

        log.info(f"Wrote {len(ticks)} ticks to {output_path}")
        return output_path

    @staticmethod
    def write_orderbook_snapshots(
        snapshots: List[OrderBookSnapshot],
        market_id: str,
        date: str,
    ) -> Path:
        """
        Write order-book snapshots to Parquet.

        Schema (denormalized for speed):
          - market_id, condition_id, outcome_index, timestamp
          - bid_prices[0..49], bid_sizes[0..49]  (fixed-width arrays)
          - ask_prices[0..49], ask_sizes[0..49]
          - mid_price, spread
        """
        # Flatten list columns to fixed-width arrays
        records = []
        max_depth = 50

        for snap in snapshots:
            rec = asdict(snap)

            # Pad/truncate to max_depth
            bid_prices = (snap.bid_prices + [0] * max_depth)[:max_depth]
            bid_sizes = (snap.bid_sizes + [0] * max_depth)[:max_depth]
            ask_prices = (snap.ask_prices + [0] * max_depth)[:max_depth]
            ask_sizes = (snap.ask_sizes + [0] * max_depth)[:max_depth]

            for i in range(max_depth):
                rec[f"bid_price_{i}"] = bid_prices[i]
                rec[f"bid_size_{i}"] = bid_sizes[i]
                rec[f"ask_price_{i}"] = ask_prices[i]
                rec[f"ask_size_{i}"] = ask_sizes[i]

            # Drop list columns
            del rec["bid_prices"], rec["bid_sizes"], rec["ask_prices"], rec["ask_sizes"]
            records.append(rec)

        df = pd.DataFrame(records)
        df["timestamp"] = df["timestamp"].astype("int64")
        df["outcome_index"] = df["outcome_index"].astype("uint8")

        output_dir = ORDERBOOK_ROOT / market_id / date[:7]
        output_dir.mkdir(parents=True, exist_ok=True)

        output_path = output_dir / f"orderbook_{date}.parquet"

        table = pa.Table.from_pandas(df)
        pq.write_table(
            table,
            output_path,
            compression="snappy",
        )

        log.info(f"Wrote {len(snapshots)} orderbook snapshots to {output_path}")
        return output_path


# ── Main Orchestrator ─────────────────────────────────────────────────────

async def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Fetch high-granularity PMXT + CLOB data"
    )
    parser.add_argument(
        "--categories",
        default=",".join(CATEGORIES),
        help="Comma-separated categories",
    )
    parser.add_argument(
        "--mode",
        choices=["ohlcv", "ticks", "orderbook", "all"],
        default="all",
        help="Which data to fetch",
    )
    parser.add_argument(
        "--start-date",
        help="Start date (YYYY-MM-DD) for historical fetch",
    )
    parser.add_argument(
        "--end-date",
        help="End date (YYYY-MM-DD) for historical fetch",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Stream live CLOB data (infinite loop)",
    )

    args = parser.parse_args()

    categories = args.categories.split(",")

    log.info(f"Starting PMXT + CLOB fetch: {args.mode} for {categories}")

    if args.mode in ["ohlcv", "all"] and args.start_date:
        log.info("Fetching OHLCV from PMXT...")
        pmxt = PMXTFetcher()
        # TODO: implement category → market_id lookup, then fetch 1m candles

    if args.mode in ["ticks", "orderbook", "all"] and args.live:
        log.info("Streaming live CLOB data...")
        # TODO: get market_ids for categories, then stream
        # For now, minimal stub:
        market_ids = ["0x..." for cat in categories]  # placeholder

        streamer = CLOBStreamer(market_ids)

        if args.mode in ["ticks", "all"]:
            async for tick in streamer.stream_trades():
                # Buffer and periodically flush to parquet
                log.debug(f"Trade: {tick.market_id} @ {tick.price}")

        if args.mode in ["orderbook", "all"]:
            async for snapshot in streamer.stream_orderbook():
                log.debug(f"Book: {snapshot.market_id} spread={snapshot.spread:.4f}")

    log.info("Done")


if __name__ == "__main__":
    asyncio.run(main())

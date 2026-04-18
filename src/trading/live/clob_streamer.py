"""
Real-time CLOB order-book and trade streamer.

Consumes Polymarket CLOB WebSocket feed, buffers ticks/snapshots,
and persists to Parquet for backtesting.

Usage:
    streamer = CLOBStreamer(market_ids=["0x...", "0x..."])
    await streamer.stream_all(duration_minutes=60)
"""

import asyncio
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Deque, AsyncIterator
from collections import deque

import websockets
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

CLOB_WS_URL = "wss://ws.clob.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"

TICK_DATA_ROOT = Path("data/pmxt/ticks")
ORDERBOOK_DATA_ROOT = Path("data/pmxt/orderbook")


@dataclass
class TickRecord:
    """Trade tick from CLOB WebSocket."""
    market_id: str
    condition_id: str
    outcome_index: int  # 0=NO, 1=YES
    timestamp: int  # Unix ms
    price: float
    size: float
    side: str  # "BUY" or "SELL"
    taker_address: str
    maker_address: Optional[str] = None
    trade_hash: str = ""


@dataclass
class BookSnapshot:
    """Order-book snapshot from CLOB WebSocket."""
    market_id: str
    condition_id: str
    outcome_index: int
    timestamp: int  # Unix ms
    bid_prices: List[float]
    bid_sizes: List[float]
    ask_prices: List[float]
    ask_sizes: List[float]


class CLOBStreamer:
    """Stream real-time CLOB data."""

    def __init__(
        self,
        market_ids: List[str],
        max_buffer_size: int = 10000,
        flush_interval_sec: int = 60,
    ):
        self.market_ids = market_ids
        self.max_buffer_size = max_buffer_size
        self.flush_interval_sec = flush_interval_sec

        # Buffers
        self.tick_buffer: Deque[TickRecord] = deque(maxlen=max_buffer_size)
        self.book_buffer: Deque[BookSnapshot] = deque(maxlen=max_buffer_size)

        # Stats
        self.tick_count = 0
        self.book_count = 0
        self.error_count = 0

    async def stream_trades(self) -> AsyncIterator[TickRecord]:
        """
        Stream trade ticks from CLOB WebSocket.

        Message format (expected):
        {
            "type": "trade",
            "market": "0x...",
            "condition_id": "0x...",
            "outcome_index": 1,
            "price": 0.75,
            "size": 100.0,
            "side": "BUY",
            "timestamp": 1704067200000,
            "taker": "0xabc...",
            "maker": "0xdef...",
            "hash": "0x..."
        }
        """
        try:
            async with websockets.connect(CLOB_WS_URL) as ws:
                # Subscribe to trades for all markets
                subscribe_msg = {
                    "action": "subscribe",
                    "channel": "trades",
                    "markets": self.market_ids,
                }
                await ws.send(json.dumps(subscribe_msg))

                log.info(f"Subscribed to {len(self.market_ids)} markets (trades)")

                async for message in ws:
                    try:
                        data = json.loads(message)

                        if data.get("type") != "trade":
                            continue

                        tick = TickRecord(
                            market_id=data["market"],
                            condition_id=data["condition_id"],
                            outcome_index=int(data["outcome_index"]),
                            timestamp=int(data["timestamp"]),
                            price=float(data["price"]),
                            size=float(data["size"]),
                            side=data["side"],
                            taker_address=data.get("taker", ""),
                            maker_address=data.get("maker"),
                            trade_hash=data.get("hash", ""),
                        )

                        self.tick_buffer.append(tick)
                        self.tick_count += 1

                        yield tick

                    except (KeyError, ValueError, json.JSONDecodeError) as e:
                        log.warning(f"Failed to parse trade message: {e}")
                        self.error_count += 1
                        continue

        except Exception as e:
            log.error(f"Trade stream error: {e}")
            self.error_count += 1
            raise

    async def stream_orderbook(self, depth: int = 50) -> AsyncIterator[BookSnapshot]:
        """
        Stream order-book snapshots from CLOB WebSocket.

        Message format (expected):
        {
            "type": "book",
            "market": "0x...",
            "condition_id": "0x...",
            "outcome_index": 1,
            "timestamp": 1704067200000,
            "bids": [[0.74, 100.0], [0.73, 200.0], ...],
            "asks": [[0.75, 150.0], [0.76, 300.0], ...]
        }
        """
        try:
            async with websockets.connect(CLOB_WS_URL) as ws:
                subscribe_msg = {
                    "action": "subscribe",
                    "channel": "book",
                    "markets": self.market_ids,
                    "depth": depth,
                }
                await ws.send(json.dumps(subscribe_msg))

                log.info(f"Subscribed to {len(self.market_ids)} markets (orderbook)")

                async for message in ws:
                    try:
                        data = json.loads(message)

                        if data.get("type") != "book":
                            continue

                        bids = data.get("bids", [])
                        asks = data.get("asks", [])

                        # Normalize
                        bid_prices = [float(p) for p, s in bids[:depth]]
                        bid_sizes = [float(s) for p, s in bids[:depth]]
                        ask_prices = [float(p) for p, s in asks[:depth]]
                        ask_sizes = [float(s) for p, s in asks[:depth]]

                        snapshot = BookSnapshot(
                            market_id=data["market"],
                            condition_id=data["condition_id"],
                            outcome_index=int(data["outcome_index"]),
                            timestamp=int(data["timestamp"]),
                            bid_prices=bid_prices,
                            bid_sizes=bid_sizes,
                            ask_prices=ask_prices,
                            ask_sizes=ask_sizes,
                        )

                        self.book_buffer.append(snapshot)
                        self.book_count += 1

                        yield snapshot

                    except (KeyError, ValueError, json.JSONDecodeError) as e:
                        log.warning(f"Failed to parse book message: {e}")
                        self.error_count += 1
                        continue

        except Exception as e:
            log.error(f"Orderbook stream error: {e}")
            self.error_count += 1
            raise

    async def stream_all(self, duration_minutes: int = 60):
        """
        Stream both trades and orderbook concurrently.

        Args:
            duration_minutes: How long to stream before stopping
        """
        start_time = datetime.now()
        timeout = asyncio.get_event_loop().time() + (duration_minutes * 60)

        async def flush_periodically():
            """Flush buffers to disk periodically."""
            while True:
                await asyncio.sleep(self.flush_interval_sec)

                if asyncio.get_event_loop().time() > timeout:
                    break

                self.flush_ticks()
                self.flush_orderbook()

        async def stream_task(stream_gen):
            """Consume a stream until timeout."""
            try:
                async for _ in stream_gen:
                    if asyncio.get_event_loop().time() > timeout:
                        break
            except asyncio.CancelledError:
                pass

        try:
            # Run streams + periodic flush concurrently
            await asyncio.wait_for(
                asyncio.gather(
                    stream_task(self.stream_trades()),
                    stream_task(self.stream_orderbook()),
                    flush_periodically(),
                    return_exceptions=True,
                ),
                timeout=duration_minutes * 60 + 5,
            )
        except asyncio.TimeoutError:
            log.info(f"Timeout after {duration_minutes} minutes")

        # Final flush
        self.flush_ticks()
        self.flush_orderbook()

        elapsed = datetime.now() - start_time
        log.info(
            f"Stream complete: {self.tick_count} ticks, "
            f"{self.book_count} snapshots, {self.error_count} errors in {elapsed}"
        )

    def flush_ticks(self) -> Optional[Path]:
        """Flush tick buffer to Parquet."""
        if not self.tick_buffer:
            return None

        ticks_list = list(self.tick_buffer)
        self.tick_buffer.clear()

        df = pd.DataFrame([asdict(t) for t in ticks_list])

        # Infer market_id and date from first record
        first_tick = ticks_list[0]
        market_id = first_tick.market_id
        date = datetime.fromtimestamp(first_tick.timestamp / 1000).strftime("%Y-%m-%d")

        # Optimize dtypes
        df["outcome_index"] = df["outcome_index"].astype("uint8")
        df["timestamp"] = df["timestamp"].astype("int64")
        df["price"] = df["price"].astype("float32")
        df["size"] = df["size"].astype("float32")

        # Write to Parquet
        output_dir = TICK_DATA_ROOT / market_id / date[:7]
        output_dir.mkdir(parents=True, exist_ok=True)

        output_path = output_dir / f"ticks_{date}.parquet"

        # Append if exists
        if output_path.exists():
            existing = pq.read_table(output_path).to_pandas()
            df = pd.concat([existing, df], ignore_index=True)

        table = pa.Table.from_pandas(df)
        pq.write_table(table, output_path, compression="snappy")

        log.info(f"Flushed {len(df)} ticks to {output_path}")
        return output_path

    def flush_orderbook(self) -> Optional[Path]:
        """Flush orderbook buffer to Parquet."""
        if not self.book_buffer:
            return None

        books_list = list(self.book_buffer)
        self.book_buffer.clear()

        # Denormalize to fixed-width columns
        records = []
        max_depth = 50

        for book in books_list:
            rec = {
                "market_id": book.market_id,
                "condition_id": book.condition_id,
                "outcome_index": book.outcome_index,
                "timestamp": book.timestamp,
            }

            for i in range(max_depth):
                rec[f"bid_price_{i}"] = book.bid_prices[i] if i < len(book.bid_prices) else 0
                rec[f"bid_size_{i}"] = book.bid_sizes[i] if i < len(book.bid_sizes) else 0
                rec[f"ask_price_{i}"] = book.ask_prices[i] if i < len(book.ask_prices) else 0
                rec[f"ask_size_{i}"] = book.ask_sizes[i] if i < len(book.ask_sizes) else 0

            records.append(rec)

        df = pd.DataFrame(records)
        df["outcome_index"] = df["outcome_index"].astype("uint8")
        df["timestamp"] = df["timestamp"].astype("int64")

        # Write
        first_book = books_list[0]
        market_id = first_book.market_id
        date = datetime.fromtimestamp(first_book.timestamp / 1000).strftime("%Y-%m-%d")

        output_dir = ORDERBOOK_DATA_ROOT / market_id / date[:7]
        output_dir.mkdir(parents=True, exist_ok=True)

        output_path = output_dir / f"orderbook_{date}.parquet"

        # Append if exists
        if output_path.exists():
            existing = pq.read_table(output_path).to_pandas()
            df = pd.concat([existing, df], ignore_index=True)

        table = pa.Table.from_pandas(df)
        pq.write_table(table, output_path, compression="snappy")

        log.info(f"Flushed {len(df)} orderbook snapshots to {output_path}")
        return output_path


async def main():
    """Example: stream from a specific market."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python clob_streamer.py <market_id> [duration_minutes]")
        sys.exit(1)

    market_id = sys.argv[1]
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 60

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    streamer = CLOBStreamer(
        market_ids=[market_id],
        flush_interval_sec=30,
    )

    await streamer.stream_all(duration_minutes=duration)


if __name__ == "__main__":
    asyncio.run(main())

"""
Tick-granularity data store — replaces VWAP-bucketed parquet_store.

High-resolution trading data with microsecond-to-millisecond precision:
  - Individual trade ticks (TickRecord)
  - Order-book snapshots (OrderBookSnapshot)
  - OHLCV at any timeframe (1s, 1m, 5m, etc.)

Usage:
    from trading.data_modules.tick_store import TickStore, OrderBookStore

    # Load all ticks for a market in a date range
    ticks = TickStore().load_ticks(
        market_id="0x...",
        start_date="2025-01-01",
        end_date="2025-12-31"
    )

    # Resample to 1m OHLCV
    ohlcv_1m = ticks.resample_ohlcv(timeframe="1m")

    # Load order-book snapshots
    books = OrderBookStore().load_snapshots(
        market_id="0x...",
        start_date="2025-01-01"
    )
"""

from pathlib import Path
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timedelta
import logging

import pandas as pd
import numpy as np
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

TICK_DATA_ROOT = Path("data/pmxt/ticks")
ORDERBOOK_DATA_ROOT = Path("data/pmxt/orderbook")


class TickStore:
    """Load and resample tick-level trade data."""

    def __init__(self, base_dir: Path = TICK_DATA_ROOT):
        self.base_dir = base_dir

    def load_ticks(
        self,
        market_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        min_size_usd: float = 0,
    ) -> pd.DataFrame:
        """
        Load tick-level trade data for a market.

        Args:
            market_id: Polymarket condition ID (e.g., "0xabc...")
            start_date: "YYYY-MM-DD" or None for all
            end_date: "YYYY-MM-DD" or None for all
            min_size_usd: Filter trades by minimum USD size

        Returns:
            DataFrame with columns:
              - timestamp (int, Unix ms)
              - price (float)
              - size (float)
              - side ("BUY" or "SELL")
              - taker_address (str)
              - maker_address (str or null)
              - trade_hash (str)
        """
        market_dir = self.base_dir / market_id

        if not market_dir.exists():
            log.warning(f"No tick data found for {market_id}")
            return pd.DataFrame()

        # Collect all parquet files in the date range
        files = []
        for month_dir in sorted(market_dir.iterdir()):
            if not month_dir.is_dir():
                continue

            month_str = month_dir.name  # "YYYY-MM"

            # Check date bounds
            if start_date and month_str < start_date[:7]:
                continue
            if end_date and month_str > end_date[:7]:
                continue

            for pq_file in sorted(month_dir.glob("ticks_*.parquet")):
                date_str = pq_file.stem.replace("ticks_", "")  # "YYYY-MM-DD"

                if start_date and date_str < start_date:
                    continue
                if end_date and date_str > end_date:
                    continue

                files.append(pq_file)

        if not files:
            log.warning(f"No tick files found for {market_id} in range [{start_date}, {end_date}]")
            return pd.DataFrame()

        # Load and concatenate
        dfs = []
        for pq_file in files:
            df = pq.read_table(pq_file).to_pandas()
            dfs.append(df)

        df = pd.concat(dfs, ignore_index=True)

        # Apply filters
        if min_size_usd > 0:
            df = df[df["size"] >= min_size_usd]

        # Ensure sorted by timestamp
        df = df.sort_values("timestamp").reset_index(drop=True)

        log.info(f"Loaded {len(df)} ticks for {market_id}")
        return df

    def resample_ohlcv(
        self,
        ticks: pd.DataFrame,
        timeframe: str = "1m",
    ) -> pd.DataFrame:
        """
        Resample tick data to OHLCV candles.

        Args:
            ticks: DataFrame from load_ticks()
            timeframe: "1s", "5s", "1m", "5m", "15m", "1h", "1d"

        Returns:
            DataFrame with [timestamp, open, high, low, close, volume]
        """
        if ticks.empty:
            return pd.DataFrame()

        # Convert timestamp (ms) to datetime
        ticks = ticks.copy()
        ticks["dt"] = pd.to_datetime(ticks["timestamp"], unit="ms")

        # Group by timeframe
        grouped = ticks.groupby(pd.Grouper(key="dt", freq=timeframe))

        ohlcv = grouped.agg({
            "price": ["first", "max", "min", "last"],
            "size": "sum",
        })

        ohlcv.columns = ["open", "high", "low", "close", "volume"]
        ohlcv["timestamp"] = ohlcv.index.astype("int64") // 10**6  # back to ms
        ohlcv = ohlcv.reset_index(drop=True)

        return ohlcv[["timestamp", "open", "high", "low", "close", "volume"]]

    def compute_vwap(
        self,
        ticks: pd.DataFrame,
        timeframe: str = "1m",
    ) -> pd.DataFrame:
        """
        Compute volume-weighted average price (VWAP) instead of close.

        Returns:
            DataFrame with [timestamp, vwap] for each candle
        """
        if ticks.empty:
            return pd.DataFrame()

        ticks = ticks.copy()
        ticks["dt"] = pd.to_datetime(ticks["timestamp"], unit="ms")

        grouped = ticks.groupby(pd.Grouper(key="dt", freq=timeframe))

        vwap_data = []
        for dt, group in grouped:
            if len(group) == 0:
                continue

            numerator = (group["price"] * group["size"]).sum()
            denominator = group["size"].sum()

            vwap = numerator / denominator if denominator > 0 else group["price"].iloc[-1]

            vwap_data.append({
                "timestamp": int(dt.timestamp() * 1000),
                "vwap": vwap,
            })

        return pd.DataFrame(vwap_data)

    def compute_spread(
        self,
        ticks: pd.DataFrame,
        timeframe: str = "1m",
    ) -> pd.DataFrame:
        """
        Compute bid-ask spread proxy from tick data.

        For each candle, estimate spread from order-flow imbalance:
          - Cluster buy/sell sides
          - Measure price differential between them
        """
        if ticks.empty:
            return pd.DataFrame()

        ticks = ticks.copy()
        ticks["dt"] = pd.to_datetime(ticks["timestamp"], unit="ms")

        grouped = ticks.groupby(pd.Grouper(key="dt", freq=timeframe))

        spread_data = []
        for dt, group in grouped:
            if len(group) == 0:
                continue

            buys = group[group["side"] == "BUY"]["price"]
            sells = group[group["side"] == "SELL"]["price"]

            if len(buys) == 0 or len(sells) == 0:
                continue

            bid_price = buys.mean()  # Avg buy price
            ask_price = sells.mean()  # Avg sell price
            spread = ask_price - bid_price

            spread_data.append({
                "timestamp": int(dt.timestamp() * 1000),
                "spread": spread,
                "bid": bid_price,
                "ask": ask_price,
            })

        return pd.DataFrame(spread_data)


class OrderBookStore:
    """Load order-book snapshots."""

    def __init__(self, base_dir: Path = ORDERBOOK_DATA_ROOT):
        self.base_dir = base_dir

    def load_snapshots(
        self,
        market_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        depth: int = 10,  # Top N levels on each side
    ) -> pd.DataFrame:
        """
        Load order-book snapshots.

        Args:
            market_id: Polymarket condition ID
            start_date: "YYYY-MM-DD"
            end_date: "YYYY-MM-DD"
            depth: number of price levels to return

        Returns:
            DataFrame with columns:
              - timestamp (int, Unix ms)
              - bid_price_0 to bid_price_{depth-1}
              - bid_size_0 to bid_size_{depth-1}
              - ask_price_0 to ask_price_{depth-1}
              - ask_size_0 to ask_size_{depth-1}
              - mid_price (float)
              - spread (float)
        """
        market_dir = self.base_dir / market_id

        if not market_dir.exists():
            log.warning(f"No orderbook data for {market_id}")
            return pd.DataFrame()

        # Load all matching files
        files = []
        for month_dir in sorted(market_dir.iterdir()):
            if not month_dir.is_dir():
                continue

            month_str = month_dir.name

            if start_date and month_str < start_date[:7]:
                continue
            if end_date and month_str > end_date[:7]:
                continue

            for pq_file in sorted(month_dir.glob("orderbook_*.parquet")):
                date_str = pq_file.stem.replace("orderbook_", "")

                if start_date and date_str < start_date:
                    continue
                if end_date and date_str > end_date:
                    continue

                files.append(pq_file)

        if not files:
            log.warning(f"No orderbook files for {market_id}")
            return pd.DataFrame()

        dfs = []
        for pq_file in files:
            df = pq.read_table(pq_file).to_pandas()
            dfs.append(df)

        df = pd.concat(dfs, ignore_index=True)
        df = df.sort_values("timestamp").reset_index(drop=True)

        # Keep only requested depth
        cols_to_keep = ["timestamp", "mid_price", "spread"]
        cols_to_keep += [f"bid_price_{i}" for i in range(min(depth, 50))]
        cols_to_keep += [f"bid_size_{i}" for i in range(min(depth, 50))]
        cols_to_keep += [f"ask_price_{i}" for i in range(min(depth, 50))]
        cols_to_keep += [f"ask_size_{i}" for i in range(min(depth, 50))]

        cols_to_keep = [c for c in cols_to_keep if c in df.columns]

        return df[cols_to_keep]

    def compute_imbalance(
        self,
        books: pd.DataFrame,
        depth: int = 5,
    ) -> pd.DataFrame:
        """
        Compute order-book imbalance: (bid_volume - ask_volume) / (bid_volume + ask_volume).

        High positive imbalance → more buyers (bullish).
        High negative imbalance → more sellers (bearish).

        Returns:
            DataFrame with [timestamp, imbalance, imbalance_5, imbalance_10, ...]
        """
        if books.empty:
            return pd.DataFrame()

        imbalances = []

        for _, row in books.iterrows():
            imb_data = {"timestamp": row["timestamp"]}

            for d in [1, 5, 10, 20]:
                if d > depth:
                    continue

                bid_vol = sum(
                    row.get(f"bid_size_{i}", 0) for i in range(min(d, 50))
                )
                ask_vol = sum(
                    row.get(f"ask_size_{i}", 0) for i in range(min(d, 50))
                )

                total = bid_vol + ask_vol
                imbalance = (bid_vol - ask_vol) / total if total > 0 else 0

                imb_data[f"imbalance_{d}"] = imbalance

            imbalances.append(imb_data)

        return pd.DataFrame(imbalances)

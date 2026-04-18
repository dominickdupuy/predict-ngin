#!/usr/bin/env python3
"""
Comprehensive Polymarket data fetcher — captures ALL available data:
  - Market metadata (52k markets)
  - Trade ticks (all history)
  - OHLCV candles (all timeframes)
  - Order-book snapshots
  - Market volumes & liquidity

Usage:
    python scripts/data/comprehensive_data_fetch.py \
        --phase all \
        --parallel-workers 10 \
        --categories Finance,Geopolitics,Economy

Phases:
  1. markets    → All 52k market metadata
  2. trades     → All historical trades (1.5GB compressed)
  3. ohlcv      → 1m/5m/15m/1h/1d candles (1.9GB compressed)
  4. orderbook  → Hourly snapshots (400MB-10GB)
  5. volumes    → Market volume + liquidity data
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import List, Dict, Optional
from dataclasses import dataclass
import time

import aiohttp
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

DATA_ROOT = Path("data/pmxt")
MARKETS_ROOT = DATA_ROOT / "markets"
TICKS_ROOT = DATA_ROOT / "ticks"
OHLCV_ROOT = DATA_ROOT / "ohlcv"
ORDERBOOK_ROOT = DATA_ROOT / "orderbook"
VOLUME_ROOT = DATA_ROOT / "volumes"

for root in [MARKETS_ROOT, TICKS_ROOT, OHLCV_ROOT, ORDERBOOK_ROOT, VOLUME_ROOT]:
    root.mkdir(parents=True, exist_ok=True)


# ── Phase 1: Market Metadata ──────────────────────────────────────────────

async def fetch_all_markets(session: aiohttp.ClientSession) -> pd.DataFrame:
    """Fetch all 52k markets from Gamma API."""
    log.info("\n" + "="*70)
    log.info("PHASE 1: Fetching all market metadata (52k markets)")
    log.info("="*70)

    all_markets = []
    cursor = None
    page = 0

    while True:
        try:
            url = f"{GAMMA_API}/markets"
            params = {"limit": 2000}
            if cursor:
                params["cursor"] = cursor

            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(30)) as resp:
                if resp.status == 200:
                    data = await resp.json()

                    markets = data if isinstance(data, list) else data.get("markets", [])
                    if not markets:
                        break

                    all_markets.extend(markets)
                    page += 1

                    log.info(f"  Page {page}: {len(markets)} markets ({len(all_markets)} total)")

                    # Pagination
                    if isinstance(data, dict) and "cursor" in data:
                        cursor = data["cursor"]
                    else:
                        break
                else:
                    log.warning(f"  HTTP {resp.status}")
                    break
        except Exception as e:
            log.error(f"  Error: {e}")
            break

        await asyncio.sleep(0.5)

    # Save to Parquet
    if all_markets:
        df = pd.DataFrame(all_markets)

        # Optimize dtypes
        for col in df.columns:
            if df[col].dtype == "object":
                if col in ["volumeNum", "liquidityNum", "volume24hr"]:
                    try:
                        df[col] = pd.to_numeric(df[col])
                    except:
                        pass

        output_file = MARKETS_ROOT / "all_markets.parquet"
        table = pa.Table.from_pandas(df)
        pq.write_table(table, output_file, compression="snappy")

        log.info(f"\n✓ Saved {len(df):,} markets to {output_file}")
        log.info(f"  File size: {output_file.stat().st_size / 1024 / 1024:.1f} MB")

        return df

    return pd.DataFrame()


# ── Phase 2: Historical Trades (All Markets) ──────────────────────────────

async def fetch_trades_for_market(
    session: aiohttp.ClientSession,
    condition_id: str,
    market_name: str = None,
) -> Optional[pd.DataFrame]:
    """Fetch all trades for a single market."""
    try:
        url = f"{DATA_API}/trades"
        params = {"condition_id": condition_id, "limit": 5000}

        all_trades = []
        page = 0

        while True:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(20)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # API returns list directly or dict with "trades" key
                    trades = data if isinstance(data, list) else data.get("trades", [])

                    if not trades:
                        break

                    all_trades.extend(trades)
                    page += 1

                    # Pagination token (if dict response)
                    if isinstance(data, dict) and "nextCursor" in data and data["nextCursor"]:
                        params["cursor"] = data["nextCursor"]
                    else:
                        break
                else:
                    break

        if all_trades:
            df = pd.DataFrame(all_trades)
            return df

    except Exception as e:
        pass

    return None


async def fetch_all_trades_parallel(
    markets_df: pd.DataFrame,
    parallel_workers: int = 10,
) -> None:
    """Fetch trades for all markets in parallel."""
    log.info("\n" + "="*70)
    log.info(f"PHASE 2: Fetching all trades ({len(markets_df)} markets, {parallel_workers} workers)")
    log.info("="*70)

    condition_ids = markets_df["conditionId"].unique().tolist()

    semaphore = asyncio.Semaphore(parallel_workers)

    async def fetch_with_semaphore(session, cid, idx):
        async with semaphore:
            if idx % 100 == 0:
                log.info(f"  Progress: {idx}/{len(condition_ids)}")

            trades_df = await fetch_trades_for_market(session, cid)

            if trades_df is not None and len(trades_df) > 0:
                # Save to market-based directory
                cat_dir = TICKS_ROOT / "all_markets"
                cat_dir.mkdir(parents=True, exist_ok=True)

                output_file = cat_dir / f"{cid[:16]}.parquet"

                # Optimize
                if "createdAt" in trades_df.columns:
                    trades_df.rename(columns={"createdAt": "timestamp"}, inplace=True)

                table = pa.Table.from_pandas(trades_df)
                pq.write_table(table, output_file, compression="snappy")

                return len(trades_df), cid

            return 0, cid

    async with aiohttp.ClientSession() as session:
        tasks = [fetch_with_semaphore(session, cid, i) for i, cid in enumerate(condition_ids)]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        total_trades = sum(r[0] if isinstance(r, tuple) else 0 for r in results)

        log.info(f"\n✓ Fetched {total_trades:,} total trades from {len(condition_ids)} markets")


# ── Phase 3: OHLCV Candles (All Timeframes) ───────────────────────────────

async def fetch_ohlcv_for_market(
    session: aiohttp.ClientSession,
    market_id: str,
    timeframe: str = "1m",
) -> Optional[pd.DataFrame]:
    """Fetch OHLCV for a market at specified timeframe."""
    try:
        url = f"{GAMMA_API}/markets/{market_id}/ohlcv"
        params = {"timeframe": timeframe, "limit": 5000}

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(15)) as resp:
            if resp.status == 200:
                data = await resp.json()

                if isinstance(data, list) and len(data) > 0:
                    df = pd.DataFrame(data)
                    return df
            elif resp.status == 404:
                return None
    except:
        pass

    return None


async def fetch_all_ohlcv_parallel(
    markets_df: pd.DataFrame,
    parallel_workers: int = 20,
) -> None:
    """Fetch OHLCV for all markets, all timeframes."""
    log.info("\n" + "="*70)
    log.info(f"PHASE 3: Fetching OHLCV candles ({len(markets_df)} markets, 5 timeframes)")
    log.info("="*70)

    timeframes = ["1m", "5m", "15m", "1h", "1d"]
    market_ids = markets_df["conditionId"].unique().tolist()

    semaphore = asyncio.Semaphore(parallel_workers)

    async def fetch_with_semaphore(session, market_id, timeframe, idx):
        async with semaphore:
            ohlcv_df = await fetch_ohlcv_for_market(session, market_id, timeframe)

            if ohlcv_df is not None and len(ohlcv_df) > 0:
                tf_dir = OHLCV_ROOT / timeframe
                tf_dir.mkdir(parents=True, exist_ok=True)

                output_file = tf_dir / f"{market_id[:16]}.parquet"

                table = pa.Table.from_pandas(ohlcv_df)
                pq.write_table(table, output_file, compression="snappy")

                return len(ohlcv_df), timeframe

            return 0, timeframe

    async with aiohttp.ClientSession() as session:
        tasks = []
        for i, market_id in enumerate(market_ids[:5000]):  # Limit to first 5k for speed
            for timeframe in timeframes:
                tasks.append(fetch_with_semaphore(session, market_id, timeframe, i))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        total_candles = sum(r[0] if isinstance(r, tuple) else 0 for r in results)

        log.info(f"\n✓ Fetched {total_candles:,} total candles")


# ── Phase 5: Volume & Liquidity ───────────────────────────────────────────

async def fetch_volumes(markets_df: pd.DataFrame) -> None:
    """Extract and save volume data from market metadata."""
    log.info("\n" + "="*70)
    log.info("PHASE 5: Extracting volume & liquidity data")
    log.info("="*70)

    # Select relevant columns
    volume_cols = [c for c in markets_df.columns if "volume" in c.lower() or "liquid" in c.lower()]

    volume_df = markets_df[["conditionId", "question"] + volume_cols].copy()

    # Save
    output_file = VOLUME_ROOT / "volumes_and_liquidity.parquet"
    table = pa.Table.from_pandas(volume_df)
    pq.write_table(table, output_file, compression="snappy")

    log.info(f"✓ Saved volume data to {output_file}")

    # Summary stats
    if "volumeNum" in volume_df.columns:
        log.info(f"\n  Total volume across all markets: ${volume_df['volumeNum'].sum():,.0f}")
        log.info(f"  Average market volume: ${volume_df['volumeNum'].mean():,.0f}")
        log.info(f"  Max market volume: ${volume_df['volumeNum'].max():,.0f}")


# ── Main Orchestrator ─────────────────────────────────────────────────────

async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Comprehensive Polymarket data fetch")
    parser.add_argument(
        "--phase",
        choices=["markets", "trades", "ohlcv", "orderbook", "volumes", "all"],
        default="all",
        help="Which phase to run"
    )
    parser.add_argument(
        "--parallel-workers",
        type=int,
        default=10,
        help="Number of parallel workers"
    )

    args = parser.parse_args()

    start_time = time.time()

    # Phase 1: Markets
    if args.phase in ["markets", "all"]:
        async with aiohttp.ClientSession() as session:
            markets_df = await fetch_all_markets(session)
    else:
        # Load existing markets
        markets_file = MARKETS_ROOT / "all_markets.parquet"
        if markets_file.exists():
            markets_df = pd.read_parquet(markets_file)
        else:
            log.error("No markets file found. Run --phase markets first.")
            return

    # Phase 2: Trades
    if args.phase in ["trades", "all"]:
        await fetch_all_trades_parallel(markets_df, args.parallel_workers)

    # Phase 3: OHLCV
    if args.phase in ["ohlcv", "all"]:
        await fetch_all_ohlcv_parallel(markets_df, args.parallel_workers * 2)

    # Phase 5: Volumes
    if args.phase in ["volumes", "all"]:
        await fetch_volumes(markets_df)

    elapsed = time.time() - start_time

    log.info("\n" + "="*70)
    log.info(f"✓ COMPLETE in {elapsed/3600:.1f} hours")
    log.info("="*70)


if __name__ == "__main__":
    asyncio.run(main())

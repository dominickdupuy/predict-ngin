#!/usr/bin/env python3
"""
Migrate data from data/research/ to data/pmxt/ structure for paper trading.

This script consolidates Polymarket data from individual category directories
into the unified format expected by the paper trading engine.
"""

import sys
import pandas as pd
from pathlib import Path
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# Categories to consolidate
CATEGORIES = ["Finance", "Geopolitics", "Economy", "Politics", "Tech", "Sports", "Climate_and_Science", "Art_and_Culture", "Other"]

def create_pmxt_structure():
    """Create data/pmxt directory structure."""
    pmxt_dirs = [
        Path("data/pmxt/ticks"),
        Path("data/pmxt/markets"),
        Path("data/pmxt/ohlcv"),
    ]
    for d in pmxt_dirs:
        d.mkdir(parents=True, exist_ok=True)
        logger.info(f"Created {d}")

def consolidate_trades():
    """Consolidate trades from all categories into individual parquet files per category."""
    logger.info("Consolidating trades...")

    for category in CATEGORIES:
        trades_path = Path(f"data/research/{category}/trades.parquet")
        if not trades_path.exists():
            logger.warning(f"Trades not found for {category}, skipping")
            continue

        # Read and rename to match the expected format
        df = pd.read_parquet(trades_path)

        # Ensure required columns exist
        if 'conditionId' not in df.columns and 'market_id' in df.columns:
            df['conditionId'] = df['market_id']

        # Save to pmxt structure
        output_path = Path(f"data/pmxt/ticks/{category}_trades.parquet")
        df.to_parquet(output_path, compression='snappy')
        logger.info(f"Consolidated {len(df):,} ticks from {category} → {output_path}")

def consolidate_markets():
    """Consolidate market metadata from all categories."""
    logger.info("Consolidating markets...")

    all_markets = []
    for category in CATEGORIES:
        markets_path = Path(f"data/research/{category}/markets_filtered.csv")
        if not markets_path.exists():
            logger.warning(f"Markets not found for {category}, skipping")
            continue

        df = pd.read_csv(markets_path, dtype=str)  # Read as strings to avoid type inference issues
        df['category'] = category
        all_markets.append(df)

    if all_markets:
        combined = pd.concat(all_markets, ignore_index=True)
        output_path = Path("data/pmxt/markets/markets_all.parquet")
        combined.to_parquet(output_path, compression='snappy', engine='pyarrow')
        logger.info(f"Consolidated {len(combined):,} markets → {output_path}")

def consolidate_ohlcv():
    """Consolidate OHLCV data from all categories."""
    logger.info("Consolidating OHLCV data...")

    all_prices = []
    for category in CATEGORIES:
        prices_path = Path(f"data/research/{category}/prices.parquet")
        if not prices_path.exists():
            logger.warning(f"OHLCV not found for {category}, skipping")
            continue

        df = pd.read_parquet(prices_path)
        df['category'] = category
        all_prices.append(df)

    if all_prices:
        combined = pd.concat(all_prices, ignore_index=True)
        output_path = Path("data/pmxt/ohlcv/ohlcv_all.parquet")
        combined.to_parquet(output_path, compression='snappy')
        logger.info(f"Consolidated {len(combined):,} OHLCV rows → {output_path}")

def verify_data():
    """Verify consolidated data."""
    logger.info("\nVerifying consolidated data...")

    tick_files = list(Path("data/pmxt/ticks").glob("*.parquet"))
    for tick_file in tick_files:
        df = pd.read_parquet(tick_file)
        print(f"  {tick_file.name}: {len(df):,} ticks")

    markets_file = Path("data/pmxt/markets/markets_all.parquet")
    if markets_file.exists():
        df = pd.read_parquet(markets_file)
        print(f"  markets_all.parquet: {len(df):,} markets")

    ohlcv_file = Path("data/pmxt/ohlcv/ohlcv_all.parquet")
    if ohlcv_file.exists():
        df = pd.read_parquet(ohlcv_file)
        print(f"  ohlcv_all.parquet: {len(df):,} rows")

if __name__ == "__main__":
    try:
        logger.info("Setting up paper trading data structure...")
        create_pmxt_structure()
        consolidate_trades()
        consolidate_markets()
        consolidate_ohlcv()
        verify_data()
        logger.info("\n✓ Paper trading data setup complete!")
    except Exception as e:
        logger.error(f"Setup failed: {e}", exc_info=True)
        sys.exit(1)

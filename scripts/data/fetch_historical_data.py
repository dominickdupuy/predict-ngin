#!/usr/bin/env python3
"""
Pull maximum-depth historical data from Polymarket.

Sources:
  1. HuggingFace SII-WANGZJ/Polymarket_data
       markets.parquet  68 MB  — 268k market metadata records
       quant.parquet    21 GB  — 170M clean trades from inception (YES-perspective)
  2. CLOB prices-history API
       1h price bars for every active/recent market, chunked in 14-day windows
       going back to market creation date (or Polymarket inception Oct 2020)
  3. Data API — recent trades (last 10k per market) for any gaps

Output layout:
  data/historical/
    markets.parquet              — merged market metadata
    quant.parquet                — raw HuggingFace trades (symlink / copy)
    price_1h/                    — one parquet per conditionId[:20]
    recent_trades/               — Data API top-N trades per market

Resume-safe: already-written parquet files are skipped.

Usage:
    # Full pull (recommended — runs for ~1h, needs ~25 GB free):
    python scripts/data/fetch_historical_data.py

    # Price bars only (fast, ~30 min, ~2 GB):
    python scripts/data/fetch_historical_data.py --prices-only

    # HuggingFace download only:
    python scripts/data/fetch_historical_data.py --hf-only

    # Recent trades from Data API only:
    python scripts/data/fetch_historical_data.py --trades-only

    # Narrow to liquid markets (>$10k volume) for a quick test:
    python scripts/data/fetch_historical_data.py --min-volume 10000 --prices-only
"""

import argparse
import ast
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

HF_REPO   = "SII-WANGZJ/Polymarket_data"

HIST_DIR    = _root / "data" / "historical"
PRICE_DIR   = HIST_DIR / "price_1h"
TRADES_DIR  = HIST_DIR / "recent_trades"
MARKETS_F   = HIST_DIR / "markets.parquet"

for d in [HIST_DIR, PRICE_DIR, TRADES_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Oct 2020 — Polymarket launch; farthest we can go back
INCEPTION_TS  = int(datetime(2020, 10, 1, tzinfo=timezone.utc).timestamp())
# CLOB max window = 14 days; 14*24 = 336 bars per request
WINDOW_DAYS   = 13          # stay safely under the 14-day limit
WINDOW_SECS   = WINDOW_DAYS * 86400
FIDELITY_MINS = 60          # 1-hour bars


# ─── helpers ──────────────────────────────────────────────────────────────────

async def _get(session: aiohttp.ClientSession, url: str, params: dict,
               retries: int = 4) -> dict | list | None:
    for attempt in range(retries):
        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status == 200:
                    return await r.json()
                if r.status == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return None
        except Exception:
            if attempt < retries - 1:
                await asyncio.sleep(1)
    return None


def _parse_tokens(raw) -> list[str]:
    if not raw or str(raw) in ("nan", "null", "None", "[]"):
        return []
    try:
        return [str(t) for t in ast.literal_eval(str(raw))]
    except Exception:
        return []


# ─── Phase 1: market metadata ─────────────────────────────────────────────────

async def fetch_markets(session: aiohttp.ClientSession) -> pd.DataFrame:
    """Fetch all markets (active + closed) from Gamma API."""
    if MARKETS_F.exists():
        df = pd.read_parquet(MARKETS_F)
        print(f"[markets] loaded {len(df):,} from cache")
        return df

    print("[markets] fetching from Gamma API (active + closed)...")
    all_markets: list = []
    PAGE = 100  # Gamma API hard cap per page

    for closed in ("false", "true"):
        offset = 0
        while True:
            batch = await _get(session, f"{GAMMA_API}/markets",
                               {"limit": PAGE, "offset": offset, "closed": closed})
            if not batch:
                break
            items = batch if isinstance(batch, list) else batch.get("markets", [])
            if not items:
                break
            all_markets.extend(items)
            offset += len(items)
            if offset % 2000 == 0:
                print(f"  {len(all_markets):,} markets so far (closed={closed})...")
            if len(items) < PAGE:  # partial page = last page
                break
            await asyncio.sleep(0.05)

    df = (pd.DataFrame(all_markets)
          .drop_duplicates(subset=["conditionId"], keep="first")
          if all_markets else pd.DataFrame())

    if not df.empty:
        pq.write_table(pa.Table.from_pandas(df), MARKETS_F, compression="snappy")
        print(f"[markets] {len(df):,} saved -> {MARKETS_F}")
    return df


# ─── Phase 2: HuggingFace bulk trade data ─────────────────────────────────────

def download_hf(output_dir: Path) -> None:
    """Download markets.parquet + quant.parquet from HuggingFace."""
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("[hf] huggingface_hub not installed. Run: pip install huggingface_hub")
        return

    files = [
        ("markets.parquet", "68 MB"),
        ("quant.parquet",   "21 GB"),
    ]
    for fname, size in files:
        dest = output_dir / fname
        if dest.exists():
            print(f"[hf] {fname} already exists ({size}), skipping")
            continue
        print(f"[hf] downloading {fname} ({size})...")
        try:
            local = hf_hub_download(
                repo_id=HF_REPO,
                filename=fname,
                repo_type="dataset",
                local_dir=str(output_dir),
            )
            print(f"[hf] saved -> {local}")
        except Exception as exc:
            print(f"[hf] ERROR: {exc}")


# ─── Phase 3: 1h price bars from CLOB ────────────────────────────────────────

async def fetch_price_bars(
    session: aiohttp.ClientSession,
    condition_id: str,
    token_id: str,
    semaphore: asyncio.Semaphore,
    market_start_ts: int | None = None,
) -> int:
    """
    Fetch full 1h price history for one token via CLOB prices-history.
    Chunks into 13-day windows from market creation (or inception) to now.
    Returns number of bars written.
    """
    out = PRICE_DIR / f"{condition_id[:20]}.parquet"
    if out.exists():
        return 0

    start = max(market_start_ts or INCEPTION_TS, INCEPTION_TS)
    end   = int(time.time())

    async with semaphore:
        all_bars: list = []
        ts = start
        while ts < end:
            chunk_end = min(ts + WINDOW_SECS, end)
            data = await _get(
                session, f"{CLOB_API}/prices-history",
                {"market": token_id, "fidelity": FIDELITY_MINS,
                 "startTs": ts, "endTs": chunk_end},
            )
            if data:
                bars = data.get("history", []) if isinstance(data, dict) else data
                all_bars.extend(bars)
            ts = chunk_end
            await asyncio.sleep(0.03)

        if not all_bars:
            return 0

        df = pd.DataFrame(all_bars)
        df["condition_id"] = condition_id
        df["token_id"]     = token_id
        # rename canonical columns
        if "t" in df.columns:
            df.rename(columns={"t": "ts", "p": "price"}, inplace=True)
        df = df.drop_duplicates(subset=["ts"]).sort_values("ts")
        pq.write_table(pa.Table.from_pandas(df), out, compression="snappy")
        return len(df)


async def fetch_all_prices(
    markets_df: pd.DataFrame,
    workers: int,
    min_volume: float,
) -> None:
    done_ids = {f.stem for f in PRICE_DIR.iterdir() if f.suffix == ".parquet"}

    # Filter by volume
    vol_col = next((c for c in ("volumeNum", "volume", "volume24hr") if c in markets_df.columns), None)
    if vol_col:
        markets_df = markets_df[
            pd.to_numeric(markets_df[vol_col], errors="coerce").fillna(0) >= min_volume
        ]

    # Build (conditionId, tokenId, startTs) tuples — use YES token only
    tasks_meta: list[tuple[str, str, int | None]] = []
    for _, row in markets_df.iterrows():
        cid = str(row.get("conditionId", ""))
        if not cid or cid[:20] in done_ids:
            continue
        tokens = _parse_tokens(row.get("clobTokenIds", "[]"))
        if not tokens:
            continue
        token = tokens[0]  # YES token

        # parse market creation date for smarter start
        start_ts = None
        for date_col in ("createdAt", "startDate", "created_at"):
            raw = row.get(date_col)
            if raw and str(raw) not in ("nan", "None", ""):
                try:
                    start_ts = int(pd.Timestamp(str(raw)).timestamp())
                    break
                except Exception:
                    pass

        tasks_meta.append((cid, token, start_ts))

    print(f"[prices] {len(tasks_meta):,} markets to fetch "
          f"({len(done_ids):,} already cached, {len(markets_df):,} qualifying)")

    semaphore   = asyncio.Semaphore(workers)
    total_bars  = 0
    done_count  = 0

    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=workers + 5)
    ) as session:
        coros = [
            fetch_price_bars(session, cid, tok, semaphore, start)
            for cid, tok, start in tasks_meta
        ]
        for coro in asyncio.as_completed(coros):
            n = await coro
            total_bars += n
            done_count += 1
            if done_count % 100 == 0 or done_count == len(tasks_meta):
                pct = 100 * done_count / max(len(tasks_meta), 1)
                print(f"  {done_count}/{len(tasks_meta)} ({pct:.0f}%) — "
                      f"{total_bars:,} bars written")

    print(f"[prices] done: {total_bars:,} 1h bars across {done_count} markets")


# ─── Phase 4: recent trades from Data API ────────────────────────────────────

async def fetch_recent_trade(
    session: aiohttp.ClientSession,
    condition_id: str,
    semaphore: asyncio.Semaphore,
    limit: int = 10_000,
) -> int:
    out = TRADES_DIR / f"{condition_id[:20]}.parquet"
    if out.exists():
        return 0

    async with semaphore:
        data = await _get(session, f"{DATA_API}/trades",
                          {"market": condition_id, "limit": limit})
        if not data:
            return 0
        trades = data if isinstance(data, list) else data.get("trades", [])
        if not trades:
            return 0

        df = pd.DataFrame(trades)
        df["condition_id"] = condition_id
        pq.write_table(pa.Table.from_pandas(df), out, compression="snappy")
        return len(df)


async def fetch_all_recent_trades(
    markets_df: pd.DataFrame,
    workers: int,
    min_volume: float,
) -> None:
    done_ids = {f.stem for f in TRADES_DIR.iterdir() if f.suffix == ".parquet"}

    vol_col = next((c for c in ("volumeNum", "volume") if c in markets_df.columns), None)
    if vol_col:
        markets_df = markets_df[
            pd.to_numeric(markets_df[vol_col], errors="coerce").fillna(0) >= min_volume
        ]

    todo = [
        str(row["conditionId"])
        for _, row in markets_df.iterrows()
        if str(row.get("conditionId", ""))[:20] not in done_ids
    ]

    print(f"[trades] {len(todo):,} markets to fetch "
          f"({len(done_ids):,} already cached)")

    semaphore    = asyncio.Semaphore(workers)
    total_trades = 0
    done_count   = 0

    async with aiohttp.ClientSession() as session:
        coros = [fetch_recent_trade(session, cid, semaphore) for cid in todo]
        for coro in asyncio.as_completed(coros):
            n = await coro
            total_trades += n
            done_count   += 1
            if done_count % 200 == 0 or done_count == len(todo):
                print(f"  {done_count}/{len(todo)} — {total_trades:,} trades")

    print(f"[trades] done: {total_trades:,} trades across {done_count} markets")


# ─── main ─────────────────────────────────────────────────────────────────────

async def _run(args: argparse.Namespace) -> None:
    t0 = time.time()

    async with aiohttp.ClientSession() as session:
        markets_df = await fetch_markets(session)

    if markets_df.empty:
        print("ERROR: could not fetch market list")
        return

    print(f"\nMarkets loaded: {len(markets_df):,}")

    if args.hf_only or not args.prices_only and not args.trades_only:
        print("\n--- HuggingFace bulk trades ---")
        download_hf(HIST_DIR)

    if args.trades_only or (not args.prices_only and not args.hf_only):
        print("\n--- Recent trades (Data API) ---")
        await fetch_all_recent_trades(markets_df, args.workers, args.min_volume)

    if args.prices_only or (not args.trades_only and not args.hf_only):
        print("\n--- 1h price bars (CLOB) ---")
        await fetch_all_prices(markets_df, args.workers, args.min_volume)

    elapsed = time.time() - t0
    price_files  = list(PRICE_DIR.iterdir())
    trade_files  = list(TRADES_DIR.iterdir())
    price_mb     = sum(f.stat().st_size for f in price_files) / 1e6
    trade_mb     = sum(f.stat().st_size for f in trade_files) / 1e6

    print(f"\n{'='*60}")
    print(f"Complete in {elapsed/60:.1f} min")
    print(f"  price_1h/     : {len(price_files):,} files  {price_mb:.0f} MB")
    print(f"  recent_trades/: {len(trade_files):,} files  {trade_mb:.0f} MB")
    print(f"  data root     : {HIST_DIR}")
    print(f"{'='*60}")


def main():
    p = argparse.ArgumentParser(description="Fetch maximum-depth Polymarket historical data")
    p.add_argument("--min-volume",   type=float, default=0,  help="Min market USD volume to include (0 = all)")
    p.add_argument("--workers",      type=int,   default=20, help="Async concurrency")
    p.add_argument("--prices-only",  action="store_true",    help="Only fetch 1h price bars")
    p.add_argument("--trades-only",  action="store_true",    help="Only fetch recent trades")
    p.add_argument("--hf-only",      action="store_true",    help="Only download HuggingFace data")
    args = p.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()

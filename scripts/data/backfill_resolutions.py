#!/usr/bin/env python3
"""
Backfill market resolution data from data/historical/markets.parquet
and the Gamma API.

Outputs: data/historical/resolutions.csv  (columns: market_id, winner)

Two phases:
  1. Derive from markets.parquet — outcomePrices near 1/0 on closed markets.
  2. Sweep Gamma API for any markets closed since the parquet snapshot
     (--refresh flag, slower, needs network).

Usage:
    python scripts/data/backfill_resolutions.py            # parquet only, fast
    python scripts/data/backfill_resolutions.py --refresh  # + Gamma API sweep
"""

import argparse
import ast
import asyncio
import sys
import time
from pathlib import Path

import aiohttp
import pandas as pd

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

HIST_DIR      = _root / "data" / "historical"
MARKETS_F     = HIST_DIR / "markets.parquet"
RESOLUTIONS_F = HIST_DIR / "resolutions.csv"
GAMMA_API     = "https://gamma-api.polymarket.com"

THRESHOLD_YES = 0.95   # outcomePrices[0] >= this → YES won
THRESHOLD_NO  = 0.05   # outcomePrices[0] <= this → NO won


def _parse_outcome_prices(raw) -> list[float] | None:
    if raw is None or str(raw) in ("nan", "None", "null", ""):
        return None
    try:
        parsed = ast.literal_eval(str(raw))
        return [float(p) for p in parsed]
    except Exception:
        return None


def derive_from_parquet() -> dict[str, str]:
    """Return {conditionId: 'YES'|'NO'} from markets.parquet."""
    if not MARKETS_F.exists():
        print("[resolutions] markets.parquet not found — run fetch_historical_data.py first")
        return {}

    df = pd.read_parquet(MARKETS_F, columns=["conditionId", "closed", "outcomePrices"])
    closed = df[df["closed"].fillna(False).astype(bool)].copy()
    print(f"[resolutions] {len(closed):,} closed markets in parquet")

    winners: dict[str, str] = {}
    ambiguous = 0

    for _, row in closed.iterrows():
        cid = str(row["conditionId"]).strip()
        if not cid or cid in ("nan", "None"):
            continue
        prices = _parse_outcome_prices(row.get("outcomePrices"))
        if prices is None or len(prices) < 2:
            ambiguous += 1
            continue
        p_yes = prices[0]
        if p_yes >= THRESHOLD_YES:
            winners[cid] = "YES"
        elif p_yes <= THRESHOLD_NO:
            winners[cid] = "NO"
        else:
            ambiguous += 1

    print(f"[resolutions] {len(winners):,} resolved  {ambiguous:,} ambiguous/unresolved")
    return winners


async def _get(session: aiohttp.ClientSession, url: str, params: dict,
               retries: int = 3) -> dict | list | None:
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


async def sweep_gamma_api(known: set[str], workers: int = 20) -> dict[str, str]:
    """Fetch closed markets from Gamma API not already in known set."""
    print("[resolutions] sweeping Gamma API for additional closed markets...")
    PAGE = 100
    winners: dict[str, str] = {}

    async with aiohttp.ClientSession() as session:
        sem = asyncio.Semaphore(workers)
        offset = 0
        fetched = 0

        while True:
            async with sem:
                batch = await _get(session, f"{GAMMA_API}/markets",
                                   {"limit": PAGE, "offset": offset, "closed": "true"})
            if not batch:
                break
            items = batch if isinstance(batch, list) else batch.get("markets", [])
            if not items:
                break

            for m in items:
                cid = str(m.get("conditionId", "")).strip()
                if not cid or cid in known:
                    continue
                prices = _parse_outcome_prices(m.get("outcomePrices"))
                if prices and len(prices) >= 2:
                    p_yes = prices[0]
                    if p_yes >= THRESHOLD_YES:
                        winners[cid] = "YES"
                    elif p_yes <= THRESHOLD_NO:
                        winners[cid] = "NO"

            fetched += len(items)
            offset  += len(items)
            if fetched % 5000 == 0:
                print(f"  swept {fetched:,} markets ({len(winners):,} new resolutions)...")
            if len(items) < PAGE:
                break
            await asyncio.sleep(0.05)

    print(f"[resolutions] Gamma sweep complete: {len(winners):,} additional resolutions")
    return winners


def save(winners: dict[str, str]) -> None:
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(
        [{"market_id": k, "winner": v} for k, v in winners.items()]
    ).drop_duplicates(subset=["market_id"])
    df.to_csv(RESOLUTIONS_F, index=False)
    yes_n = (df["winner"] == "YES").sum()
    no_n  = (df["winner"] == "NO").sum()
    print(f"[resolutions] saved {len(df):,} resolutions -> {RESOLUTIONS_F}")
    print(f"  YES: {yes_n:,}  NO: {no_n:,}")


async def _main(refresh: bool, workers: int) -> None:
    t0 = time.time()

    winners = derive_from_parquet()

    if refresh:
        extra = await sweep_gamma_api(set(winners.keys()), workers=workers)
        winners.update(extra)

    if not winners:
        print("[resolutions] no resolutions found — check markets.parquet")
        return

    save(winners)
    print(f"[resolutions] done in {time.time()-t0:.1f}s")


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill Polymarket resolution outcomes")
    p.add_argument("--refresh", action="store_true",
                   help="Also sweep Gamma API for markets closed after parquet snapshot")
    p.add_argument("--workers", type=int, default=20)
    args = p.parse_args()
    asyncio.run(_main(args.refresh, args.workers))


if __name__ == "__main__":
    main()

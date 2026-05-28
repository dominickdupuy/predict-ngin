#!/usr/bin/env python3
"""
Fetch resolution outcomes for markets in our trade dataset.

Strategy:
1. Find all conditionIds from markets_filtered.csv that have endDate in the past
2. Paginate through Gamma API closed markets to find those IDs
3. Also check outcomePrices in markets_filtered.csv directly (may already have resolved prices)
4. Merge with existing data/research/resolutions.csv
"""
import ast
import csv
import time
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import requests

_root = Path(__file__).resolve().parents[2]
RESEARCH_DIR = _root / "data" / "research"
OUT_PATH = RESEARCH_DIR / "resolutions.csv"
GAMMA_API = "https://gamma-api.polymarket.com"


def _parse_outcome(raw) -> str | None:
    try:
        prices = ast.literal_eval(str(raw)) if raw else []
        prices = [float(p) for p in prices]
    except Exception:
        return None
    if len(prices) >= 2:
        if prices[0] >= 0.99:
            return "YES"
        if prices[0] <= 0.01:
            return "NO"
    return None


def load_our_market_ids() -> dict:
    """Load conditionId -> endDate for all our markets."""
    mids = {}
    today = pd.Timestamp.now(tz="UTC")
    for p in RESEARCH_DIR.glob("*/markets_filtered.csv"):
        df = pd.read_csv(p, dtype=str, low_memory=False)
        if "conditionId" not in df.columns:
            continue
        for _, row in df.iterrows():
            cid = str(row.get("conditionId", "")).strip()
            if not cid or cid == "nan":
                continue
            mids[cid] = row
    print(f"Total markets in our dataset: {len(mids)}")
    return mids


def extract_from_csv(our_markets: dict) -> dict:
    """Check if outcomePrices in markets_filtered.csv already shows resolution."""
    found = {}
    for cid, row in our_markets.items():
        raw = row.get("outcomePrices", "")
        outcome = _parse_outcome(raw)
        if outcome:
            found[cid] = outcome
    print(f"  Already resolved in CSV outcomePrices: {len(found)}")
    return found


def fetch_closed_markets_for_ids(our_market_ids: set, session: requests.Session) -> dict:
    """
    Paginate through Gamma API closed markets to find any of our IDs.
    Stops when we've checked enough pages or found all ours.
    """
    found = {}
    offset = 0
    remaining = set(our_market_ids)
    print(f"  Checking Gamma API closed markets for {len(remaining)} IDs...")

    while remaining:
        try:
            r = session.get(
                f"{GAMMA_API}/markets",
                params={"limit": 100, "offset": offset, "closed": "true"},
                timeout=30,
            )
            if r.status_code == 422:
                print(f"  API limit reached at offset {offset}")
                break
            r.raise_for_status()
            batch = r.json()
            items = batch if isinstance(batch, list) else batch.get("markets") or batch.get("data") or []
            if not items:
                break

            for m in items:
                cid = str(m.get("conditionId") or "").strip()
                if cid in remaining:
                    outcome = _parse_outcome(m.get("outcomePrices", ""))
                    if outcome:
                        found[cid] = outcome
                        remaining.discard(cid)

            offset += len(items)
            if offset % 2000 == 0:
                print(f"    offset {offset}: found {len(found)} resolutions, {len(remaining)} still looking...")

            if len(items) < 100:
                break
            time.sleep(0.05)

        except Exception as e:
            print(f"  Warning at offset {offset}: {e}")
            break

    print(f"  Found {len(found)} resolutions from closed-market pagination")
    return found


def re_fetch_expired(expired_ids: list, session: requests.Session) -> dict:
    """
    For markets with endDate in the past, re-fetch individually using
    the slug or by scanning recent closed pages more carefully.
    """
    found = {}
    # Try fetching 5 pages of most-recently-closed markets
    for offset in range(0, 500, 100):
        try:
            r = session.get(
                f"{GAMMA_API}/markets",
                params={"limit": 100, "offset": offset, "closed": "true",
                        "order": "endDate", "ascending": "false"},
                timeout=30,
            )
            if r.status_code != 200:
                break
            items = r.json()
            if not isinstance(items, list):
                items = items.get("markets") or items.get("data") or []
            target = set(expired_ids)
            for m in items:
                cid = str(m.get("conditionId") or "").strip()
                if cid in target:
                    outcome = _parse_outcome(m.get("outcomePrices", ""))
                    if outcome:
                        found[cid] = outcome
        except Exception:
            break
        time.sleep(0.05)
    print(f"  Found {len(found)} resolutions from recently-closed re-fetch")
    return found


def main():
    session = requests.Session()
    session.headers["User-Agent"] = "predict-ngin/1.0"

    print("Loading our market IDs...")
    our_markets = load_our_market_ids()

    print("\nStep 1: Check existing outcomePrices in CSV...")
    resolutions = extract_from_csv(our_markets)

    print("\nStep 2: Find expired markets (endDate in past)...")
    today = pd.Timestamp.now(tz="UTC")
    expired_ids = []
    for cid, row in our_markets.items():
        end = pd.to_datetime(row.get("endDate", ""), errors="coerce", utc=True)
        if pd.notna(end) and end < today and cid not in resolutions:
            expired_ids.append(cid)
    print(f"  {len(expired_ids)} expired markets without resolution yet")

    print("\nStep 3: Paginate closed markets to find our IDs...")
    our_ids_set = set(our_markets.keys()) - set(resolutions.keys())
    from_pagination = fetch_closed_markets_for_ids(our_ids_set, session)
    resolutions.update(from_pagination)

    print("\nStep 4: Re-fetch recently closed for expired IDs...")
    still_missing = [cid for cid in expired_ids if cid not in resolutions]
    if still_missing:
        recent = re_fetch_expired(still_missing, session)
        resolutions.update(recent)

    # Load existing resolutions and merge
    existing = {}
    if OUT_PATH.exists():
        df_ex = pd.read_csv(OUT_PATH)
        if "market_id" in df_ex.columns and "winner" in df_ex.columns:
            existing = dict(zip(df_ex["market_id"].astype(str).str.strip(),
                                df_ex["winner"].astype(str).str.strip()))
    print(f"\nExisting resolutions: {len(existing)}")

    merged = {**existing, **resolutions}  # our new ones override
    print(f"Merged total: {len(merged)}")

    # Overlap with our trade data
    trade_ids = set()
    for p in RESEARCH_DIR.glob("*/trades.parquet"):
        df = pd.read_parquet(p, columns=["market_id"])
        trade_ids.update(df["market_id"].dropna().astype(str).str.strip().unique())

    overlap = set(merged.keys()) & trade_ids
    print(f"\nOVERLAP with trade data: {len(overlap)} markets have both trades AND resolutions")

    with open(OUT_PATH, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["market_id", "winner"])
        for cid, winner in merged.items():
            writer.writerow([cid, winner])

    print(f"Saved {len(merged)} resolutions -> {OUT_PATH}")

    if overlap:
        print(f"\nSample resolved markets with trade data:")
        for cid in list(overlap)[:5]:
            print(f"  {cid[:30]}... -> {merged[cid]}")


if __name__ == "__main__":
    main()

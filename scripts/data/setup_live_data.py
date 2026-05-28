#!/usr/bin/env python3
"""
Pull all Polymarket data needed for live trading whale identification.

Fetches:
  1. Markets by category from Gamma API → data/research/{Category}/markets_filtered.csv
  2. All trades for those markets from Data API → data/research/{Category}/trades.parquet

This gives the live strategy fresh data to build the current whale set.

Usage:
    python scripts/data/setup_live_data.py                    # All categories
    python scripts/data/setup_live_data.py --categories Finance,Politics
    python scripts/data/setup_live_data.py --workers 20 --top-n 300
    python scripts/data/setup_live_data.py --trades-only      # Skip market fetch
"""

import argparse
import ast
import sys
import time
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"

CATEGORIES = [
    "Finance", "Politics", "Geopolitics", "Economy",
    "Tech", "Sports", "Climate_and_Science", "Art_and_Culture", "Other",
]

CATEGORY_KEYWORDS = {
    "Finance":            ["stock", "crypto", "bitcoin", "eth", "fed", "rate", "market", "price", "asset", "fund", "investment"],
    "Politics":           ["president", "election", "senate", "congress", "vote", "law", "democrat", "republican", "government", "policy"],
    "Geopolitics":        ["war", "military", "nato", "sanction", "conflict", "treaty", "diplomacy", "missile", "ukraine", "russia", "china", "israel"],
    "Economy":            ["gdp", "inflation", "unemployment", "recession", "jobs", "trade", "tariff", "economic", "growth"],
    "Tech":               ["ai", "tech", "apple", "google", "microsoft", "openai", "robot", "software", "hardware", "internet"],
    "Sports":             ["nfl", "nba", "mlb", "soccer", "football", "basketball", "baseball", "championship", "tournament", "team"],
    "Climate_and_Science": ["climate", "carbon", "temperature", "emissions", "science", "research", "study", "nasa", "space"],
    "Art_and_Culture":    ["oscars", "emmy", "grammy", "award", "movie", "music", "art", "culture", "entertainment"],
}


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "predict-ngin-data/1.0"
    return s


def _classify_category(question: str, description: str = "") -> str:
    text = (question + " " + description).lower()
    best, best_score = "Other", 0
    for cat, keywords in CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > best_score:
            best, best_score = cat, score
    return best


def fetch_all_markets(session: requests.Session, include_closed: bool = False) -> list:
    """Fetch all active markets from Gamma API (paginated by offset)."""
    print("Fetching markets from Gamma API...")
    markets = []
    offset = 0
    page_size = 100

    while True:
        params = {"limit": page_size, "offset": offset}
        if not include_closed:
            params["active"] = "true"

        try:
            r = session.get(f"{GAMMA_API}/markets", params=params, timeout=30)
            r.raise_for_status()
            batch = r.json()

            if isinstance(batch, dict):
                items = batch.get("markets") or batch.get("data") or []
            else:
                items = batch if isinstance(batch, list) else []

            if not items:
                break

            markets.extend(items)
            offset += len(items)

            if offset % 1000 == 0:
                print(f"  {len(markets):,} markets fetched...")

            if len(items) < page_size:
                break
            time.sleep(0.1)
        except Exception as e:
            print(f"  Warning: offset {offset} error: {e}")
            break

    print(f"  Total: {len(markets):,} markets")
    return markets


def partition_by_category(markets: list) -> dict:
    """Group markets into category buckets."""
    buckets = defaultdict(list)
    for m in markets:
        raw_cat = m.get("category", "") or ""
        # Try direct mapping first
        normalized = raw_cat.strip().replace(" ", "_")
        if normalized in CATEGORIES:
            cat = normalized
        else:
            cat = _classify_category(
                m.get("question", ""),
                m.get("description", ""),
            )
        buckets[cat].append(m)
    return dict(buckets)


def save_markets_csv(markets: list, out_path: Path, category: str) -> None:
    if not markets:
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(markets)

    # Ensure conditionId column exists
    if "conditionId" not in df.columns and "id" in df.columns:
        df["conditionId"] = df["id"]
    if "category" not in df.columns:
        df["category"] = category

    df.to_csv(out_path, index=False)
    print(f"  Saved {len(df):,} markets -> {out_path}")


def _parse_token_ids(raw) -> list:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return []
    s = str(raw).strip()
    if not s or s == "[]":
        return []
    if s.startswith("["):
        try:
            return [str(x).strip() for x in ast.literal_eval(s)]
        except Exception:
            pass
    return [s] if s else []


def fetch_trades_for_market(
    condition_id: str,
    clob_token_ids: list,
    session: requests.Session,
    limit: int = 10_000,
) -> list:
    """Fetch all trades for a market from the Data API."""
    trades = []
    try:
        # Prefer conditionId endpoint
        r = session.get(
            f"{DATA_API}/trades",
            params={"market": condition_id, "limit": limit},
            timeout=30,
        )
        if r.status_code == 200:
            batch = r.json()
            if batch:
                for t in batch:
                    t["conditionId"] = condition_id
                trades.extend(batch)
    except Exception:
        pass

    return trades


def fetch_trades_parallel(
    markets_csv: Path,
    out_path: Path,
    workers: int = 10,
    limit_per_market: int = 10_000,
) -> None:
    """Fetch trades for all markets in a category CSV, save to parquet."""
    if not markets_csv.exists():
        print(f"  Skipping (no CSV): {markets_csv}")
        return

    df_markets = pd.read_csv(markets_csv, dtype=str)
    if "conditionId" not in df_markets.columns:
        print(f"  Skipping (no conditionId column): {markets_csv}")
        return

    condition_ids = df_markets["conditionId"].dropna().unique().tolist()
    if not condition_ids:
        return

    # Get clobTokenIds for each market
    token_map = {}
    for _, row in df_markets.iterrows():
        cid = str(row.get("conditionId", ""))
        raw = row.get("clobTokenIds", "")
        token_map[cid] = _parse_token_ids(raw)

    print(f"  Fetching trades for {len(condition_ids):,} markets...")
    all_trades = []
    progress = {"done": 0, "total": len(condition_ids)}
    lock = threading.Lock()

    def _fetch(cid):
        session = _make_session()
        result = fetch_trades_for_market(cid, token_map.get(cid, []), session, limit_per_market)
        with lock:
            progress["done"] += 1
            if progress["done"] % 50 == 0:
                print(f"    {progress['done']}/{progress['total']} markets done "
                      f"({len(all_trades):,} trades so far)...")
        return result

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch, cid): cid for cid in condition_ids}
        for fut in as_completed(futs):
            try:
                batch = fut.result()
                all_trades.extend(batch)
            except Exception as e:
                print(f"    Warning: {futs[fut]}: {e}")

    if not all_trades:
        print(f"  No trades fetched for {markets_csv.parent.name}")
        return

    df = pd.DataFrame(all_trades)

    # Normalise column names to match research_data_loader expectations
    col_renames = {
        "proxyWallet": "maker",
        "side": "maker_direction",
        "usdcSize": "usd_amount",
        "conditionId": "market_id",
    }
    df.rename(columns={k: v for k, v in col_renames.items() if k in df.columns}, inplace=True)

    if "timestamp" in df.columns:
        df["datetime"] = pd.to_datetime(
            pd.to_numeric(df["timestamp"], errors="coerce"), unit="s", utc=True
        )
    if "price" in df.columns:
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
    if "usd_amount" not in df.columns and "size" in df.columns and "price" in df.columns:
        df["usd_amount"] = pd.to_numeric(df["size"], errors="coerce") * df["price"]
    if "category" not in df.columns:
        df["category"] = markets_csv.parent.name

    # Keep only non-empty rows
    df = df.dropna(subset=["market_id", "price"] if "market_id" in df.columns else ["price"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False, compression="snappy")
    print(f"  Saved {len(df):,} trades -> {out_path}")


def main():
    p = argparse.ArgumentParser(description="Fetch Polymarket data for live whale trading")
    p.add_argument("--research-dir", default="data/research", help="Output directory")
    p.add_argument("--categories", default=None, help="Comma-separated categories (default: all)")
    p.add_argument("--workers", type=int, default=15, help="Parallel workers for trade fetch")
    p.add_argument("--top-n", type=int, default=500, help="Max markets per category")
    p.add_argument("--trades-only", action="store_true", help="Skip market fetch, only update trades")
    p.add_argument("--include-closed", action="store_true", help="Include closed markets")
    args = p.parse_args()

    research_dir = _root / args.research_dir
    categories = [c.strip() for c in args.categories.split(",")] if args.categories else CATEGORIES

    session = _make_session()

    # ── Step 1: Fetch & categorise markets ────────────────────────────────────
    if not args.trades_only:
        print("\n[1/2] Fetching markets...")
        all_markets = fetch_all_markets(session, include_closed=args.include_closed)
        bucketed = partition_by_category(all_markets)

        for cat in categories:
            markets = bucketed.get(cat, [])
            # Sort by volume and cap
            markets.sort(key=lambda m: float(m.get("volume", 0) or 0), reverse=True)
            markets = markets[: args.top_n]

            out_csv = research_dir / cat / "markets_filtered.csv"
            save_markets_csv(markets, out_csv, cat)
    else:
        print("\n[1/2] Skipping market fetch (--trades-only)")

    # ── Step 2: Fetch trades ──────────────────────────────────────────────────
    print("\n[2/2] Fetching trades...")
    for cat in categories:
        csv_path = research_dir / cat / "markets_filtered.csv"
        out_parquet = research_dir / cat / "trades.parquet"
        print(f"\n  Category: {cat}")
        fetch_trades_parallel(csv_path, out_parquet, workers=args.workers)

    print("\n✓ Data setup complete.")
    print(f"  Research data: {research_dir}")
    print("  Run the live strategy with:")
    print(f"    python scripts/live/run_live_strategy.py --research-dir {research_dir} --capital 25 --live")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Incremental refresh of data/historical/ before live trading.

Updates three sources without re-downloading everything:
  1. recent_trades/ — new trades from Data API since the latest stored timestamp
  2. resolutions.csv — newly resolved markets from Gamma API
  3. markets.parquet — fresh metadata for active markets

Safe to re-run at any time; already-written data is not overwritten.

Usage:
    python scripts/data/refresh_data.py
    python scripts/data/refresh_data.py --max-age-hours 12   # skip if fresh
    python scripts/data/refresh_data.py --trades-only
    python scripts/data/refresh_data.py --resolutions-only
"""

import argparse
import ast
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root))

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"

HIST_DIR     = _root / "data" / "historical"
TRADES_DIR   = HIST_DIR / "recent_trades"
RES_FILE     = HIST_DIR / "resolutions.csv"
MARKETS_FILE = HIST_DIR / "markets.parquet"
CURSOR_FILE  = HIST_DIR / ".refresh_cursor.json"  # persists fetch progress

for d in [HIST_DIR, TRADES_DIR]:
    d.mkdir(parents=True, exist_ok=True)


def _read_cursor() -> dict:
    if CURSOR_FILE.exists():
        try:
            return json.loads(CURSOR_FILE.read_text())
        except Exception:
            pass
    return {}


def _write_cursor(updates: dict) -> None:
    cursor = _read_cursor()
    cursor.update(updates)
    cursor["updated_at"] = datetime.now(timezone.utc).isoformat()
    CURSOR_FILE.write_text(json.dumps(cursor, indent=2))


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "predict-ngin-refresh/1.0"
    return s


def _file_age_hours(path: Path) -> float:
    if not path.exists():
        return float("inf")
    return (time.time() - path.stat().st_mtime) / 3600


# ── 1. Trades refresh ──────────────────────────────────────────────────────────

def _latest_trade_ts() -> int:
    """
    Return the most recent trade timestamp (unix seconds) to use as fetch cursor.
    Reads from cursor file first (written after each successful refresh),
    falls back to scanning parquet files if cursor missing.
    """
    # Cursor file is the authoritative source — written after every successful fetch
    cursor = _read_cursor()
    if "trades_last_ts" in cursor:
        return int(cursor["trades_last_ts"])

    # No cursor yet — scan parquet files to bootstrap
    import glob, random
    files = glob.glob(str(TRADES_DIR / "*.parquet"))
    if not files:
        return int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())

    sample = random.sample(files, min(500, len(files)))
    ts_max_ms = 0
    for f in sample:
        try:
            col = pd.read_parquet(f, columns=["timestamp"])["timestamp"]
            m = int(col.max())
            if m > ts_max_ms:
                ts_max_ms = m
        except Exception:
            pass

    ts = ts_max_ms // 1000 if ts_max_ms > 1e12 else ts_max_ms
    # Persist so future runs don't need to scan
    if ts > 0:
        _write_cursor({"trades_last_ts": ts})
    return ts


def _flush_trades(buffer: dict[str, list], seen_hashes: dict[str, set]) -> int:
    """Write a buffer of {conditionId: [trade, ...]} to parquet files. Returns rows written."""
    written = 0
    for cid, trades in buffer.items():
        out_path = TRADES_DIR / f"{cid}.parquet"
        rows = []
        for t in trades:
            tx = str(t.get("transactionHash") or "")
            if tx and tx in seen_hashes.get(cid, set()):
                continue
            ts_raw = int(float(t.get("timestamp", 0) or 0))
            rows.append({
                "proxyWallet":     str(t.get("proxyWallet") or t.get("maker") or ""),
                "side":            str(t.get("side", "BUY") or "BUY").upper(),
                "asset":           str(t.get("asset") or ""),
                "conditionId":     cid,
                "size":            float(t.get("size") or 0),
                "price":           float(t.get("price") or 0),
                "timestamp":       ts_raw * 1000 if ts_raw < 1e12 else ts_raw,
                "title":           str(t.get("title") or ""),
                "transactionHash": str(t.get("transactionHash") or ""),
                "usdcSize":        float(t.get("usdcSize") or t.get("amount") or 0),
                "condition_id":    cid,
            })
            if tx:
                seen_hashes.setdefault(cid, set()).add(tx)

        if not rows:
            continue

        new_df = pd.DataFrame(rows)
        if out_path.exists():
            try:
                old_df = pd.read_parquet(out_path)
                new_df = pd.concat([old_df, new_df], ignore_index=True)
            except Exception:
                pass
        pq.write_table(pa.Table.from_pandas(new_df, preserve_index=False), out_path)
        written += len(rows)
    return written


def refresh_trades(session: requests.Session, lookback_buffer_hours: int = 2,
                   flush_every: int = 50_000) -> int:
    """
    Fetch trades since the cursor and write progressively to disk.
    Flushes every `flush_every` trades and updates the cursor so progress
    survives a timeout — next run picks up exactly where this one stopped.
    """
    latest_ts = _latest_trade_ts()
    since_ts  = latest_ts - lookback_buffer_hours * 3600
    since_dt  = datetime.fromtimestamp(since_ts, tz=timezone.utc)
    print(f"  Fetching trades since {since_dt.strftime('%Y-%m-%d %H:%M UTC')}...")

    buffer:      dict[str, list] = defaultdict(list)
    seen_hashes: dict[str, set]  = {}
    after        = since_ts
    page         = 0
    total_raw    = 0
    total_written = 0
    last_cursor  = since_ts

    while True:
        try:
            resp = session.get(
                f"{DATA_API}/trades",
                params={"limit": 500, "after": after},
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json() or []
        except Exception as e:
            print(f"  Warning: trade fetch error (page {page}): {e}")
            break

        if not batch:
            break

        for t in batch:
            cid = str(t.get("conditionId") or "").strip()
            if cid:
                buffer[cid].append(t)

        total_raw += len(batch)
        page += 1

        batch_ts = [int(float(t.get("timestamp", 0) or 0)) for t in batch]
        if batch_ts:
            after = max(batch_ts)
            if after > 1e12:
                after //= 1000
            last_cursor = after

        if total_raw % flush_every < 500:
            n = _flush_trades(buffer, seen_hashes)
            total_written += n
            buffer.clear()
            _write_cursor({"trades_last_ts": last_cursor})
            dt = datetime.fromtimestamp(last_cursor, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            print(f"    ... {total_raw:,} fetched, {total_written:,} written, cursor={dt}")

        if len(batch) < 500:
            break

        time.sleep(0.05)

    # Final flush of remaining buffer
    if buffer:
        total_written += _flush_trades(buffer, seen_hashes)

    if total_raw == 0:
        print("  No new trades found.")
        return 0

    # Save final cursor
    _write_cursor({"trades_last_ts": last_cursor})
    dt = datetime.fromtimestamp(last_cursor, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"  Done: {total_raw:,} fetched, {total_written:,} written, cursor={dt}")
    return total_written


# ── 2. Resolutions refresh ─────────────────────────────────────────────────────

def refresh_resolutions(session: requests.Session) -> int:
    """
    Append newly resolved markets to resolutions.csv.
    Deduplicates by market_id. Returns number of new rows added.
    """
    existing: dict[str, str] = {}
    latest_ts = 0

    if RES_FILE.exists():
        try:
            rdf = pd.read_csv(RES_FILE)
            if "market_id" in rdf.columns and "winner" in rdf.columns:
                existing = dict(zip(rdf["market_id"].astype(str), rdf["winner"].astype(str)))
            # Find latest closedTime to use as cursor
            if "closedTime" in rdf.columns:
                ts_col = pd.to_numeric(rdf["closedTime"], errors="coerce").dropna()
                if not ts_col.empty:
                    latest_ts = int(ts_col.max())
                    if latest_ts > 1e12:
                        latest_ts //= 1000
        except Exception:
            pass

    # Go back at least 7 days to catch anything missed
    since_ts = max(
        latest_ts - 3600,
        int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp()),
    )
    since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc)
    print(f"  Fetching resolved markets since {since_dt.strftime('%Y-%m-%d UTC')}...")

    new_rows: list[dict] = []
    offset = 0

    while True:
        try:
            resp = session.get(
                f"{GAMMA_API}/markets",
                params={"closed": "true", "limit": 500, "offset": offset},
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json() or []
        except Exception as e:
            print(f"  Warning: resolution fetch error at offset {offset}: {e}")
            break

        if not batch:
            break

        stop = False
        for m in batch:
            cid = str(m.get("conditionId") or "").strip()
            if not cid or cid in existing:
                continue

            # Determine winner from outcomePrices
            raw = m.get("outcomePrices", "")
            try:
                prices = ast.literal_eval(str(raw)) if raw else []
                prices = [float(p) for p in prices]
            except Exception:
                continue

            if len(prices) < 2:
                continue

            if prices[0] >= 0.99:
                winner = "YES"
            elif prices[0] <= 0.01:
                winner = "NO"
            else:
                continue  # not yet resolved

            # Check closedTime to know if we've gone past our window
            closed_raw = m.get("closedTime") or m.get("updatedAt") or ""
            try:
                if isinstance(closed_raw, (int, float)):
                    closed_ts = int(float(closed_raw))
                    if closed_ts > 1e12:
                        closed_ts //= 1000
                else:
                    closed_ts = int(
                        datetime.fromisoformat(
                            str(closed_raw).replace("Z", "+00:00")
                        ).timestamp()
                    )
                if closed_ts < since_ts - 86400 * 30:
                    stop = True
            except Exception:
                closed_ts = 0

            new_rows.append({
                "market_id":   cid,
                "winner":      winner,
                "closedTime":  closed_ts,
                "question":    str(m.get("question") or ""),
            })
            existing[cid] = winner

        offset += len(batch)
        if stop or len(batch) < 500:
            break
        time.sleep(0.15)

    if new_rows:
        new_df = pd.DataFrame(new_rows)
        if RES_FILE.exists():
            old_df = pd.read_csv(RES_FILE)
            combined = pd.concat([old_df, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=["market_id"])
        else:
            combined = new_df
        combined.to_csv(RES_FILE, index=False)
        print(f"  Added {len(new_rows)} new resolutions  (total {len(combined)})")
    else:
        print(f"  No new resolutions  (total {len(existing)})")

    _write_cursor({"resolutions_last_run": datetime.now(timezone.utc).isoformat()})
    return len(new_rows)


# ── 3. Markets refresh ─────────────────────────────────────────────────────────

def refresh_markets(session: requests.Session, limit: int = 5000) -> int:
    """
    Fetch the most recently active/closed markets and merge into markets.parquet.
    Only updates records newer than what's stored. Returns number of rows upserted.
    """
    print(f"  Fetching latest {limit} markets from Gamma API...")

    fetched = []
    for closed_flag in ["false", "true"]:
        offset = 0
        while len(fetched) < limit:
            try:
                resp = session.get(
                    f"{GAMMA_API}/markets",
                    params={
                        "closed":  closed_flag,
                        "limit":   500,
                        "offset":  offset,
                        "order":   "updatedAt",
                        "ascending": "false",
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                batch = resp.json() or []
            except Exception as e:
                print(f"  Warning: markets fetch error: {e}")
                break

            if not batch:
                break
            fetched.extend(batch)
            offset += len(batch)
            if len(batch) < 500:
                break
            time.sleep(0.1)

            if len(fetched) >= limit // 2:
                break

    if not fetched:
        print("  No market data fetched.")
        return 0

    new_df = pd.DataFrame(fetched)
    if "conditionId" not in new_df.columns:
        print("  Warning: conditionId not in markets response.")
        return 0

    new_df = new_df.drop_duplicates(subset=["conditionId"])

    if MARKETS_FILE.exists():
        try:
            old_df = pd.read_parquet(MARKETS_FILE)
            merged = pd.concat([old_df, new_df], ignore_index=True)
            merged = merged.drop_duplicates(subset=["conditionId"], keep="last")
        except Exception:
            merged = new_df
    else:
        merged = new_df

    pq.write_table(pa.Table.from_pandas(merged, preserve_index=False), MARKETS_FILE)
    print(f"  Markets: {len(new_df)} fetched → {len(merged)} total in parquet")
    return len(new_df)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Incremental data refresh for live trading")
    parser.add_argument("--max-age-hours", type=float, default=0,
                        help="Skip refresh if all sources are fresher than this (0 = always refresh)")
    parser.add_argument("--trades-only",      action="store_true")
    parser.add_argument("--resolutions-only", action="store_true")
    parser.add_argument("--markets-only",     action="store_true")
    args = parser.parse_args()

    # Staleness check
    if args.max_age_hours > 0:
        ages = {
            "trades":      _file_age_hours(sorted(TRADES_DIR.glob("*.parquet"))[-1]) if list(TRADES_DIR.glob("*.parquet")) else float("inf"),
            "resolutions": _file_age_hours(RES_FILE),
            "markets":     _file_age_hours(MARKETS_FILE),
        }
        if all(a < args.max_age_hours for a in ages.values()):
            print(f"All data sources are fresh (< {args.max_age_hours}h old). Skipping refresh.")
            for k, v in ages.items():
                print(f"  {k}: {v:.1f}h old")
            return

    all_sources = not (args.trades_only or args.resolutions_only or args.markets_only)
    session = _session()

    print(f"\n{'='*55}")
    print(f"  Data Refresh — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*55}")

    if all_sources or args.trades_only:
        print("\n[1/3] Refreshing trades...")
        refresh_trades(session)

    if all_sources or args.resolutions_only:
        print("\n[2/3] Refreshing resolutions...")
        refresh_resolutions(session)

    if all_sources or args.markets_only:
        print("\n[3/3] Refreshing markets...")
        refresh_markets(session)

    print(f"\nRefresh complete at {datetime.now().strftime('%H:%M:%S')}")


if __name__ == "__main__":
    main()

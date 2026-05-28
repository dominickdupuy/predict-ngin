#!/usr/bin/env python3
"""Fetch closed/resolved Polymarket markets and build resolutions.csv."""
import ast
import csv
import time
from pathlib import Path
import requests

GAMMA_API = "https://gamma-api.polymarket.com"
OUT_PATH  = Path(__file__).resolve().parents[2] / "data" / "research" / "resolutions.csv"
TARGET    = 10_000


def main():
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "predict-ngin/1.0"

    print("Fetching closed/resolved markets from Gamma API...")
    resolutions = {}
    offset = 0

    while len(resolutions) < TARGET:
        try:
            r = session.get(
                f"{GAMMA_API}/markets",
                params={"limit": 100, "offset": offset, "closed": "true"},
                timeout=30,
            )
            r.raise_for_status()
            batch = r.json()
            if isinstance(batch, dict):
                items = batch.get("markets") or batch.get("data") or []
            else:
                items = batch if isinstance(batch, list) else []

            if not items:
                break

            for m in items:
                cid = str(m.get("conditionId") or "").strip()
                if not cid:
                    continue
                raw = m.get("outcomePrices", "")
                try:
                    prices = ast.literal_eval(str(raw)) if raw else []
                    prices = [float(p) for p in prices]
                except Exception:
                    continue
                if len(prices) >= 2:
                    if prices[0] >= 0.99:
                        resolutions[cid] = "YES"
                    elif prices[0] <= 0.01:
                        resolutions[cid] = "NO"

            offset += len(items)
            if offset % 1000 == 0:
                print(f"  {len(resolutions)} resolutions gathered (offset {offset})...")

            if len(items) < 100:
                break
            time.sleep(0.1)

        except Exception as e:
            print(f"  Warning at offset {offset}: {e}")
            break

    yes_count = sum(1 for v in resolutions.values() if v == "YES")
    no_count  = sum(1 for v in resolutions.values() if v == "NO")
    print(f"Total resolved: {len(resolutions)}  (YES: {yes_count}, NO: {no_count})")

    with open(OUT_PATH, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["market_id", "winner"])
        for cid, winner in resolutions.items():
            writer.writerow([cid, winner])

    print(f"Saved -> {OUT_PATH}")


if __name__ == "__main__":
    main()

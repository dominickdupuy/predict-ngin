"""
latency_arb.py — Systematic latency arbitrage pipeline for Polymarket

Combines:
  1. news_monitor.py  — headline ingestion + NLP market matching
  2. Minute-level price history — measures actual lag after headline
  3. Position sizing — Kelly-inspired, capped by residual uncertainty
  4. Signal ranking — prioritizes: high residual × high NLP score × high market volume

This is the "systematic" version requested. It measures the actual lag using
Polymarket 1-min CLOB prices, not just the entry-day snapshot from the backtest.

Key metric: "lag_minutes" — how many minutes after a headline does the market
take to reach >95% (or <5%) YES? Historical mean across 5 events: ~45 minutes.

Usage:
    # Dry-run (print signals, no orders):
    python scripts/live/latency_arb.py --dry-run --newsapi-key YOUR_KEY

    # Live (requires py-clob-client and funded wallet):
    python scripts/live/latency_arb.py --newsapi-key YOUR_KEY --capital 10000

    # Backtest mode (measure lag on historical trade data):
    python scripts/live/latency_arb.py --backtest --condition-id 0xabc...
"""

import argparse
import datetime
import json
import logging
import math
import time
import sys
import os
from dataclasses import dataclass, asdict
from typing import Optional
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("latency_arb")

CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


# ── Historical lag measurement ─────────────────────────────────────────────────

@dataclass
class LagMeasurement:
    """How long it took the market to fully price in a confirmed event."""
    condition_id: str
    market_question: str
    event_description: str
    headline_utc: datetime.datetime
    price_at_headline: float      # YES price when news broke
    price_at_full: float          # YES price when fully priced (>95% or <5%)
    minutes_to_full: float        # how many minutes until fully priced
    resolution: float             # 1.0 or 0.0
    residual_at_headline: float   # lag = |resolution - price_at_headline|


def measure_lag_from_trades(
    condition_id: str,
    headline_utc: datetime.datetime,
    resolution: float,
    window_hours: int = 6,
    session: requests.Session = None,
) -> Optional[LagMeasurement]:
    """
    Fetch trade ticks for `condition_id` and measure how long it takes
    for the YES price to reach within 5% of resolution after `headline_utc`.

    Returns LagMeasurement or None if insufficient data.
    """
    s = session or requests.Session()
    s.headers["User-Agent"] = "latency-arb/1.0"

    # Fetch trades in window
    start_ts = int((headline_utc - datetime.timedelta(hours=1)).timestamp())
    end_ts   = int((headline_utc + datetime.timedelta(hours=window_hours)).timestamp())

    trades = []
    offset = 0
    while True:
        r = s.get(
            f"{DATA_API}/trades",
            params={"market": condition_id, "limit": 500, "offset": offset, "after": start_ts},
            timeout=20,
        )
        batch = r.json() or []
        trades.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
        time.sleep(0.1)

    if not trades:
        log.warning(f"No trades found for {condition_id}")
        return None

    # Build per-minute VWAP
    import pandas as pd
    rows = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        ts = int(float(t.get("timestamp") or 0))
        if ts > 1e12:
            ts //= 1000
        if ts < start_ts or ts > end_ts:
            continue
        price = float(t.get("price") or 0)
        size  = float(t.get("size") or 0)
        oi    = t.get("outcomeIndex")
        oi    = int(oi) if oi is not None else 0
        if oi == 1:
            price = 1.0 - price  # normalize to YES
        if 0.001 <= price <= 0.999 and size > 0:
            rows.append({"ts": ts, "price": price, "size": size})

    if not rows:
        return None

    df = pd.DataFrame(rows)
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df = df.set_index("dt").sort_index()
    vwap = (df["price"] * df["size"]).resample("1min").sum() / df["size"].resample("1min").sum()
    vwap = vwap.dropna()

    if vwap.empty:
        return None

    import pytz
    hl_utc = headline_utc.replace(tzinfo=pytz.UTC) if headline_utc.tzinfo is None else headline_utc

    # Price at headline
    before_hl = vwap[vwap.index <= hl_utc]
    price_at_hl = float(before_hl.iloc[-1]) if not before_hl.empty else float(vwap.iloc[0])

    # Find when price reaches "full" (within 5% of resolution)
    after_hl = vwap[vwap.index > hl_utc]
    minutes_to_full = None
    price_at_full = price_at_hl
    threshold = 0.05

    for dt, p in after_hl.items():
        if (resolution == 1.0 and p >= 1.0 - threshold) or \
           (resolution == 0.0 and p <= threshold):
            minutes_to_full = (dt - hl_utc).total_seconds() / 60
            price_at_full = float(p)
            break

    if minutes_to_full is None:
        # Never fully priced in window
        minutes_to_full = (after_hl.index[-1] - hl_utc).total_seconds() / 60 if not after_hl.empty else window_hours * 60
        price_at_full = float(after_hl.iloc[-1]) if not after_hl.empty else price_at_hl

    return LagMeasurement(
        condition_id=condition_id,
        market_question="",
        event_description="",
        headline_utc=headline_utc,
        price_at_headline=price_at_hl,
        price_at_full=price_at_full,
        minutes_to_full=minutes_to_full,
        resolution=resolution,
        residual_at_headline=abs(resolution - price_at_hl),
    )


def _measure_item(item: dict) -> tuple[dict, Optional[LagMeasurement]]:
    """Module-level worker function so ProcessPoolExecutor can pickle it."""
    hl_dt = datetime.datetime.fromisoformat(item["headline_utc"])
    m = measure_lag_from_trades(
        condition_id=item["condition_id"],
        headline_utc=hl_dt,
        resolution=item["resolution"],
    )
    return item, m


def run_lag_analysis(condition_ids_with_metadata: list[dict]) -> list[LagMeasurement]:
    """
    Batch lag measurement across multiple historical events.
    Uses up to 35 parallel workers on HPC.
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed

    results = []
    n_workers = min(len(condition_ids_with_metadata), 35)

    with ProcessPoolExecutor(max_workers=n_workers) as ex:
        futs = {ex.submit(_measure_item, item): item for item in condition_ids_with_metadata}
        for fut in as_completed(futs):
            item = futs[fut]
            try:
                _, m = fut.result()
                if m:
                    m.market_question   = item.get("question", "")
                    m.event_description = item.get("headline", "")
                    results.append(m)
                    log.info(
                        f"Lag: {item['headline'][:50]:<50} "
                        f"residual={m.residual_at_headline:.1%}  "
                        f"full_price_in={m.minutes_to_full:.0f} min"
                    )
            except Exception as e:
                log.warning(f"Error measuring {item['condition_id']}: {e}")

    return results


# ── Live signal ranker ─────────────────────────────────────────────────────────

@dataclass
class RankedSignal:
    """A latency arb signal ranked by expected profit opportunity."""
    condition_id: str
    market_question: str
    market_url: str
    direction: str              # "BUY" or "SELL"
    current_yes_price: float
    residual_pct: float         # the lag
    nlp_score: float
    volume_24h: float
    expected_lag_minutes: float # from historical average
    kelly_fraction: float       # position size fraction
    signal_score: float         # composite ranking score
    headline_title: str
    headline_url: str
    headline_published_utc: str
    minutes_since_headline: float
    clob_token_id: str


HISTORICAL_LAG_MINUTES = 45.0  # mean from our 5-event analysis: ~45 min average


def rank_signal(signal_dict: dict, hist_lag_min: float = HISTORICAL_LAG_MINUTES) -> RankedSignal:
    """
    Composite score = residual_pct × nlp_score × log(volume_24h + 1) × (1 / time_since_hl)
    Higher residual + better NLP match + more liquid market + fresher headline = better signal.
    """
    import math
    residual   = signal_dict["residual_pct"]
    nlp        = signal_dict["nlp_score"]
    vol        = signal_dict.get("volume_24h", 1000)
    age_min    = max(signal_dict["minutes_since_headline"], 0.5)

    # Kelly fraction: f = (edge - (1-edge)) / odds
    # edge ≈ residual (expected move to 0 or 1 from current price)
    # simplified: f ≈ residual / (1 - residual)
    edge = residual
    kelly = edge / max(1.0 - edge, 0.01)
    kelly = min(kelly, 0.25)  # cap at 25% of capital per trade

    score = (
        residual                      # 0-20% (the lag itself)
        * nlp                         # 0-1 (match quality)
        * math.log1p(vol / 1000)      # market liquidity
        * (1.0 / math.sqrt(age_min))  # time decay — fresher = better
    )

    return RankedSignal(
        condition_id=signal_dict.get("market_id", ""),
        market_question=signal_dict.get("market_question", ""),
        market_url=signal_dict.get("market_url", ""),
        direction=signal_dict.get("direction", ""),
        current_yes_price=signal_dict.get("current_yes_price", 0),
        residual_pct=residual,
        nlp_score=nlp,
        volume_24h=vol,
        expected_lag_minutes=hist_lag_min,
        kelly_fraction=kelly,
        signal_score=score,
        headline_title=signal_dict.get("headline_title", ""),
        headline_url=signal_dict.get("headline_url", ""),
        headline_published_utc=signal_dict.get("headline_published_utc", ""),
        minutes_since_headline=age_min,
        clob_token_id=signal_dict.get("clob_token_id", ""),
    )


def print_ranked_signals(signals: list[RankedSignal], capital: float = 10000):
    """Pretty-print ranked signals with position sizes."""
    signals_sorted = sorted(signals, key=lambda s: s.signal_score, reverse=True)
    print(f"\n{'='*90}")
    print(f"  RANKED LATENCY ARB SIGNALS  |  Capital: ${capital:,.0f}  |  {datetime.datetime.utcnow().strftime('%H:%M:%S UTC')}")
    print(f"{'='*90}")
    for rank, s in enumerate(signals_sorted, 1):
        pos_size = math.floor(min(s.kelly_fraction * capital, 0.25 * capital))
        print(
            f"#{rank:2d}  {'BUY' if s.direction=='BUY' else 'SELL':4s}  "
            f"YES={s.current_yes_price:.1%}  lag={s.residual_pct:.1%}  "
            f"NLP={s.nlp_score:.2f}  score={s.signal_score:.4f}  "
            f"size=${pos_size:,.0f}"
        )
        print(f"      Market: {s.market_question[:75]}")
        print(f"      Headline: {s.headline_title[:75]}")
        print(f"      Age: {s.minutes_since_headline:.1f} min  |  {s.market_url}")
    print(f"{'='*90}\n")


# ── CLI entry points ───────────────────────────────────────────────────────────

KNOWN_EVENTS = [
    {
        "condition_id": "0xa37bcb2aec3a653b4d3446406ce605b1e7dc668fc2ee3902ddf7ca6552585f9e",
        "headline": "CNN: Israel bombs Houthis in Yemen after rebels attack commercial ship",
        "headline_utc": "2025-07-06T21:00:00",
        "resolution": 1.0,
        "question": "Israel strikes Yemen by Monday?",
    },
    {
        "condition_id": "0xc5da1ea1f3b67411908af996b5b257120c7e0a1f803633c807e103d1b2a6a311",
        "headline": "IDF announces start of Operation Gideon's Chariots Gaza ground offensive",
        "headline_utc": "2025-05-18T04:00:00",
        "resolution": 1.0,
        "question": "Will Israel launch a major ground offensive in Gaza in May?",
    },
    {
        "condition_id": "0x3315de37bfb5e6565dec1452a914a9160afbe18512aa29a89abf1bfe9c263c7b",
        "headline": "Putin announces Russian forces captured Huliaipole, Ukraine",
        "headline_utc": "2025-12-27T09:00:00",
        "resolution": 1.0,
        "question": "Will Russia capture Huliaipole by December 31?",
    },
    {
        "condition_id": "0x7035aa7555c84710f78a64c3f91c232d03a7153af0fdcf2a01f7838033204afb",
        "headline": "South Korean court approves arrest warrant for Yoon Suk Yeol",
        "headline_utc": "2025-07-09T18:00:00",
        "resolution": 1.0,
        "question": "Yoon arrested by July 15?",
    },
    {
        "condition_id": "0x24dff082e905d9448a23b7c48d26c78bc31cf367d50b3d471c00de0171896f0a",
        "headline": "South Korea election exit polls: Lee Jun-seok wins 7.7%, not in 11-14% band",
        "headline_utc": "2025-06-03T11:30:00",
        "resolution": 0.0,
        "question": "Will Lee Jun-seok win between 11% and 14% of the vote?",
    },
]


def main():
    p = argparse.ArgumentParser(description="Latency arb pipeline for Polymarket")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--backtest",    action="store_true",  help="Measure lag on known historical events")
    mode.add_argument("--live",        action="store_true",  help="Run live news monitor + signal ranker")
    mode.add_argument("--dry-run",     action="store_true",  help="Live mode but print signals only, no orders")

    p.add_argument("--newsapi-key",    default=None,   help="NewsAPI.org key (optional; falls back to RSS/GDELT)")
    p.add_argument("--capital",        type=float, default=10000, help="Total capital for position sizing (default: $10,000)")
    p.add_argument("--interval",       type=int,   default=30,   help="Poll interval in seconds (default: 30)")
    p.add_argument("--log",            default="signals.jsonl",  help="Output JSONL signal log")
    p.add_argument("--condition-id",   default=None,   help="Specific conditionId for single-market backtest")
    p.add_argument("--min-residual",   type=float, default=0.03, help="Min lag to fire signal (default: 3%%)")
    p.add_argument("--workers",        type=int,   default=35,   help="Parallel workers for backtest (default: 35)")
    args = p.parse_args()

    if args.backtest:
        events = KNOWN_EVENTS
        if args.condition_id:
            events = [e for e in events if e["condition_id"] == args.condition_id]
        if not events:
            log.error(f"No events found for {args.condition_id}")
            return

        log.info(f"Running lag backtest on {len(events)} known events using {args.workers} workers...")
        measurements = run_lag_analysis(events)

        if measurements:
            import numpy as np
            print(f"\n{'='*80}")
            print("  LAG MEASUREMENT RESULTS")
            print(f"{'='*80}")
            print(f"{'Event':<45} {'Lag@hl':>7} {'Full in':>8} {'Residual':>9}")
            print("-"*80)
            for m in measurements:
                print(
                    f"{m.event_description[:44]:<45} "
                    f"{m.residual_at_headline:>6.1%}  "
                    f"{m.minutes_to_full:>6.0f} min  "
                    f"{abs(m.resolution - m.price_at_full):>8.1%}"
                )
            print("-"*80)
            residuals = [m.residual_at_headline for m in measurements]
            lags      = [m.minutes_to_full for m in measurements]
            print(f"Mean residual at headline: {np.mean(residuals):.1%}")
            print(f"Mean minutes to full price discovery: {np.mean(lags):.0f} min")
            print(f"Median minutes to full price discovery: {np.median(lags):.0f} min")
            print(f"\nConclusion: average latency arbitrage window = {np.mean(lags):.0f} minutes")
            print(f"{'='*80}\n")

            if args.log:
                with open(args.log.replace(".jsonl", "_backtest.json"), "w") as f:
                    json.dump([asdict(m) for m in measurements], f, indent=2, default=str)
                log.info(f"Results saved to {args.log.replace('.jsonl', '_backtest.json')}")

    elif args.live or args.dry_run:
        if args.dry_run:
            log.info("DRY-RUN mode — signals will be printed but no orders placed")

        # Set up live order router when --live is specified
        order_router = None
        if args.live:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
            from src.trading.live.order_router import OrderRouter, OrderSide
            order_router = OrderRouter(dry_run=False)
            if not order_router.is_authenticated():
                log.error("Live mode requires POLYMARKET_PRIVATE_KEY env var.")
                log.error("Install: pip install py-clob-client")
                return
            log.info("Live order router initialised (latency arb)")

        # Import and run the news monitor
        from scripts.live.news_monitor import (
            NewsAPIIngester, GDELTIngester, RSSIngester,
            load_open_markets, HeadlineMatcher, PriceChecker, SignalEmitter,
            MIN_BUY_YES, MAX_SELL_YES
        )

        # Choose ingester
        if args.newsapi_key:
            ingester = NewsAPIIngester(args.newsapi_key)
        else:
            try:
                ingester = RSSIngester()
            except ImportError:
                ingester = GDELTIngester()

        price_checker = PriceChecker()
        emitter = SignalEmitter(log_path=args.log if not args.dry_run else None)
        markets = load_open_markets()
        matcher = HeadlineMatcher(markets)
        pending_signals = []
        last_refresh = time.time()
        placed_signals = set()  # deduplicate by (market_id, direction)

        while True:
            if time.time() - last_refresh > 1800:
                markets = load_open_markets()
                matcher = HeadlineMatcher(markets)
                last_refresh = time.time()

            headlines = ingester.fetch()
            for headline in headlines:
                matches = matcher.match(headline, top_k=5)
                for market, nlp_score in matches:
                    yes_price, token_id = price_checker.get_yes_price(market)
                    if not yes_price:
                        continue

                    age_min = (datetime.datetime.utcnow() - headline.published_utc).total_seconds() / 60
                    if yes_price > MIN_BUY_YES and yes_price < 0.97:
                        direction, residual = "BUY", 1.0 - yes_price
                    elif yes_price < MAX_SELL_YES and yes_price > 0.03:
                        direction, residual = "SELL", yes_price
                    else:
                        continue

                    if residual < args.min_residual:
                        continue

                    raw = {
                        "market_id": market.get("conditionId", ""),
                        "market_question": market.get("question", ""),
                        "market_url": f"https://polymarket.com/event/{market.get('slug', '')}",
                        "direction": direction,
                        "current_yes_price": yes_price,
                        "residual_pct": residual,
                        "nlp_score": nlp_score,
                        "volume_24h": float(market.get("volume24hr", 0) or 0),
                        "headline_title": headline.title,
                        "headline_url": headline.url,
                        "headline_published_utc": headline.published_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "minutes_since_headline": age_min,
                        "clob_token_id": token_id,
                    }
                    ranked = rank_signal(raw)
                    pending_signals.append(ranked)

            if pending_signals:
                print_ranked_signals(pending_signals, capital=args.capital)
                if not args.dry_run and args.log:
                    with open(args.log, "a") as f:
                        for s in pending_signals:
                            f.write(json.dumps(asdict(s)) + "\n")

                # Place live orders for top signal
                if order_router is not None and pending_signals:
                    best = sorted(pending_signals, key=lambda s: s.signal_score, reverse=True)[0]
                    sig_key = (best.condition_id, best.direction)
                    if sig_key not in placed_signals and best.clob_token_id:
                        pos_size = math.floor(min(best.kelly_fraction * args.capital, 0.25 * args.capital))
                        if pos_size >= 1:
                            placed_signals.add(sig_key)
                            side = OrderSide.BUY if best.direction == "BUY" else OrderSide.SELL
                            try:
                                result = order_router.place_market_order(
                                    best.clob_token_id, side, size_usd=float(pos_size)
                                )
                                log.info(f"ORDER PLACED: {best.direction} ${pos_size} "
                                         f"on {best.market_question[:50]} | {result}")
                            except Exception as e:
                                log.error(f"Order failed: {e}")

                pending_signals.clear()

            time.sleep(args.interval)


if __name__ == "__main__":
    main()

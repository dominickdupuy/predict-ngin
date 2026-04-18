"""
pipeline.py — Automated News-to-Trade Latency Arbitrage Pipeline

Integrates three systems into one continuous loop:
  1. News Ingestion     — EventRegistry API (NewsAPI.ai) polls every `--interval` seconds
  2. NER Matching       — SemanticMatcher (sentence-transformers + ER concept overlap)
  3. Paper Execution    — PaperTrader tracks virtual positions and P&L

Modes
-----
--dry-run       : Print signals, no position tracking (default)
--paper-trade   : Track virtual positions + P&L, auto-close on convergence
--backtest      : Measure actual price-discovery lag on 5 known historical events

Usage
-----
# Dry-run (just print signals):
PYTHONPATH=.:src venv/bin/python3 scripts/live/pipeline.py \\
    --newsapi-key KEY --dry-run

# Paper trading (virtual positions):
PYTHONPATH=.:src venv/bin/python3 scripts/live/pipeline.py \\
    --newsapi-key KEY --paper-trade --capital 10000

# Lag backtest (measures actual market lag on known events):
PYTHONPATH=.:src venv/bin/python3 scripts/live/pipeline.py --backtest

Architecture
------------
EventRegistryIngester
    ↓ Article / ConfirmedEvent
SemanticMatcher.match()
    ↓ [(market, blended_score, matched_concepts), ...]
PriceChecker.get_yes_price()
    ↓ current YES price + residual (gap to 0 or 1)
RankedSignal (composite score = residual × nlp × log(vol) / √age)
    ↓
PaperTrader.submit_order()   [paper-trade mode]
    ↓
Position auto-close when YES > 0.97 (BUY) or YES < 0.03 (SELL)
    ↓
JSONL signal log + equity log
"""

import argparse
import datetime
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import requests

# ── path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pipeline")

# ── API endpoints ─────────────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"
ER_BASE   = "https://newsapi.ai/api/v1"

# ── Signal thresholds ─────────────────────────────────────────────────────────
MIN_BUY_YES   = 0.80    # BUY YES only when price is this low (headline says YES likely)
MAX_SELL_YES  = 0.20    # SELL YES only when price is this high (headline says NO likely)
MIN_RESIDUAL  = 0.05    # minimum gap to resolution worth trading
CONVERGENCE_BUY  = 0.97 # close BUY position when YES >= this
CONVERGENCE_SELL = 0.03 # close SELL position when YES <= this

# ── Historical events for lag backtest ───────────────────────────────────────
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


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class RankedSignal:
    condition_id: str
    market_question: str
    market_url: str
    direction: str              # "BUY" or "SELL"
    current_yes_price: float
    residual_pct: float
    nlp_score: float
    volume_24h: float
    kelly_fraction: float
    signal_score: float
    headline_title: str
    headline_url: str
    headline_published_utc: str
    minutes_since_headline: float
    clob_token_id: str
    matched_concepts: list[str]


@dataclass
class LagMeasurement:
    condition_id: str
    market_question: str
    headline: str
    headline_utc: str
    resolution: float
    price_at_headline: float
    price_at_full: float
    residual_at_headline: float
    minutes_to_full: float


# ── Helper: score a signal ────────────────────────────────────────────────────

def _score_signal(
    residual: float,
    nlp: float,
    volume_24h: float,
    age_minutes: float,
) -> tuple[float, float]:
    """
    Composite score = residual × nlp × log(vol+1) / √age
    Kelly fraction = residual / (1 - residual), capped at 0.25.
    """
    age = max(age_minutes, 0.5)
    score = (
        residual
        * nlp
        * math.log1p(volume_24h / 1000)
        * (1.0 / math.sqrt(age))
    )
    kelly = min(residual / max(1.0 - residual, 0.01), 0.25)
    return round(score, 6), round(kelly, 4)


# ── Price checker ─────────────────────────────────────────────────────────────

class PriceChecker:
    def __init__(self, session: requests.Session = None):
        self.s = session or requests.Session()
        self.s.headers["User-Agent"] = "latency-arb-pipeline/1.0"

    def get_yes_price(self, market: dict) -> tuple[float, str]:
        """Return (YES price, clob_token_id).  Tries Gamma first, CLOB fallback."""
        prices = market.get("outcomePrices", [])
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except Exception:
                prices = []

        token_ids = market.get("clobTokenIds", [])
        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except Exception:
                token_ids = []
        token_id = token_ids[0] if token_ids else ""

        try:
            price = float(prices[0]) if prices else 0.0
            if 0.001 < price < 0.999:
                return price, token_id
        except Exception:
            pass

        if token_id:
            try:
                r = self.s.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=8)
                book = r.json()
                asks = book.get("asks", [])
                bids = book.get("bids", [])
                if asks and bids:
                    return (float(asks[0]["price"]) + float(bids[-1]["price"])) / 2, token_id
                elif asks:
                    return float(asks[0]["price"]), token_id
                elif bids:
                    return float(bids[-1]["price"]), token_id
            except Exception:
                pass

        return 0.0, token_id


# ── Market loader ─────────────────────────────────────────────────────────────

def load_open_markets(session: requests.Session, limit: int = 3000) -> list[dict]:
    markets, offset, batch = [], 0, 500
    while len(markets) < limit:
        try:
            r = session.get(
                f"{GAMMA_API}/markets",
                params={"closed": "false", "active": "true", "limit": batch, "offset": offset},
                timeout=20,
            )
            data = r.json()
            if not data:
                break
            markets.extend(data)
            if len(data) < batch:
                break
            offset += batch
            time.sleep(0.1)
        except Exception as e:
            log.warning(f"Market load error at offset {offset}: {e}")
            break
    log.info(f"Loaded {len(markets):,} open markets")
    return markets


# ── Lag backtest ──────────────────────────────────────────────────────────────

def measure_lag(
    condition_id: str,
    headline_utc: datetime.datetime,
    resolution: float,
    window_hours: int = 6,
    session: requests.Session = None,
) -> Optional[LagMeasurement]:
    import pandas as pd
    import pytz

    s = session or requests.Session()
    s.headers["User-Agent"] = "latency-arb/1.0"

    start_ts = int((headline_utc - datetime.timedelta(hours=1)).timestamp())
    end_ts   = int((headline_utc + datetime.timedelta(hours=window_hours)).timestamp())

    trades, offset = [], 0
    while True:
        try:
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
        except Exception as e:
            log.warning(f"Trade fetch error: {e}")
            break

    if not trades:
        return None

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
            price = 1.0 - price
        if 0.001 <= price <= 0.999 and size > 0:
            rows.append({"ts": ts, "price": price, "size": size})

    if not rows:
        return None

    df = pd.DataFrame(rows)
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df = df.set_index("dt").sort_index()
    vwap = (
        (df["price"] * df["size"]).resample("1min").sum()
        / df["size"].resample("1min").sum()
    ).dropna()

    if vwap.empty:
        return None

    hl_utc = headline_utc.replace(tzinfo=pytz.UTC) if headline_utc.tzinfo is None else headline_utc

    before_hl = vwap[vwap.index <= hl_utc]
    price_at_hl = float(before_hl.iloc[-1]) if not before_hl.empty else float(vwap.iloc[0])

    after_hl = vwap[vwap.index > hl_utc]
    minutes_to_full = None
    price_at_full = price_at_hl

    for dt, p in after_hl.items():
        if (resolution == 1.0 and p >= 0.95) or (resolution == 0.0 and p <= 0.05):
            minutes_to_full = (dt - hl_utc).total_seconds() / 60
            price_at_full = float(p)
            break

    if minutes_to_full is None:
        minutes_to_full = (
            (after_hl.index[-1] - hl_utc).total_seconds() / 60
            if not after_hl.empty else window_hours * 60
        )
        price_at_full = float(after_hl.iloc[-1]) if not after_hl.empty else price_at_hl

    return LagMeasurement(
        condition_id=condition_id,
        market_question="",
        headline="",
        headline_utc=headline_utc.isoformat(),
        resolution=resolution,
        price_at_headline=round(price_at_hl, 4),
        price_at_full=round(price_at_full, 4),
        residual_at_headline=round(abs(resolution - price_at_hl), 4),
        minutes_to_full=round(minutes_to_full, 1),
    )


def _measure_item(item: dict):
    """Module-level worker for ProcessPoolExecutor pickling."""
    hl_dt = datetime.datetime.fromisoformat(item["headline_utc"])
    m = measure_lag(condition_id=item["condition_id"], headline_utc=hl_dt, resolution=item["resolution"])
    return item, m


def run_lag_backtest(events: list[dict], workers: int = 5) -> list[LagMeasurement]:
    from concurrent.futures import ProcessPoolExecutor, as_completed

    results = []
    with ProcessPoolExecutor(max_workers=min(len(events), workers)) as ex:
        futs = {ex.submit(_measure_item, e): e for e in events}
        for fut in as_completed(futs):
            item = futs[fut]
            try:
                _, m = fut.result()
                if m:
                    m.market_question = item.get("question", "")
                    m.headline = item.get("headline", "")
                    results.append(m)
                    log.info(
                        f"  {item['headline'][:55]:<55} "
                        f"residual={m.residual_at_headline:.1%}  "
                        f"full_in={m.minutes_to_full:.0f} min"
                    )
                else:
                    log.warning(f"  No trades found for: {item['condition_id']}")
            except Exception as e:
                log.warning(f"  Error: {item['condition_id']}: {e}")
    return results


# ── Main pipeline ─────────────────────────────────────────────────────────────

class Pipeline:
    """
    Continuous latency-arb pipeline.

    Polls EventRegistry, matches headlines to open markets via semantic NER,
    emits ranked signals, and optionally manages paper positions.
    """

    def __init__(self, args):
        self.args = args
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "latency-arb-pipeline/1.0"

        # Components
        from scripts.live.news_monitor import EventRegistryIngester, RSSFallback
        from src.trading.live.semantic_matcher import SemanticMatcher

        self.ingester = EventRegistryIngester(
            args.newsapi_key,   # list[str] or str
            lookback_minutes=args.lookback,
            session=self.session,
        )
        self.rss_backup = RSSFallback(lookback_minutes=args.lookback * 2)
        self.price_checker = PriceChecker(session=self.session)

        log.info("Loading open markets and encoding questions…")
        self.markets = load_open_markets(self.session, limit=args.max_markets)
        self.matcher = SemanticMatcher(self.markets)
        self._last_market_refresh = time.time()

        # Paper trader (only in paper-trade mode)
        self.paper_trader = None
        if args.paper_trade:
            from src.trading.live.paper_trading import PaperTrader, OrderSide
            self.paper_trader = PaperTrader(
                initial_capital=args.capital,
                state_path="data/latency_arb_paper_state.json",
                log_path="data/latency_arb_paper_log.jsonl",
                max_position_size=args.capital * 0.25,  # 25% per trade
                max_positions=args.max_positions,
            )
            self._OrderSide = OrderSide

        # Output paths
        out_dir = Path("backtests/latency_arb")
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        self.signal_log = out_dir / f"signals_{ts}.jsonl"
        self.equity_log = out_dir / f"equity_{ts}.jsonl"

        self._signal_count = 0
        self._poll_count = 0
        self._seen_signals: set[str] = set()  # deduplicate (market_id + headline_url)

    # ── Market refresh ────────────────────────────────────────────────────────

    def _maybe_refresh_markets(self):
        if time.time() - self._last_market_refresh > 1800:
            log.info("Refreshing market list…")
            self.markets = load_open_markets(self.session, limit=self.args.max_markets)
            self.matcher.set_markets(self.markets)
            self._last_market_refresh = time.time()

    # ── Signal processing ─────────────────────────────────────────────────────

    def _process_articles(self, articles) -> list[RankedSignal]:
        signals = []
        now_utc = datetime.datetime.utcnow()

        for art in articles:
            if not art.is_relevant():
                continue
            age_min = (now_utc - art.published_utc).total_seconds() / 60
            if age_min > self.args.max_age_minutes:
                continue

            matches = self.matcher.match(
                art.title, art.body, art.concepts,
                top_k=5,
                min_score=self.args.min_nlp_score,
            )
            for market, nlp_score, matched_concepts in matches:
                sig = self._build_signal(
                    market, nlp_score, matched_concepts,
                    art.title, art.url,
                    art.published_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    age_min,
                )
                if sig:
                    signals.append(sig)

        return signals

    def _process_events(self, events) -> list[RankedSignal]:
        signals = []
        for ev in events:
            matches = self.matcher.match(
                ev.title, ev.summary, ev.concepts,
                top_k=3,
                min_score=self.args.min_nlp_score,
            )
            for market, nlp_score, matched_concepts in matches:
                sig = self._build_signal(
                    market, nlp_score, matched_concepts,
                    ev.title, f"https://newsapi.ai/event/{ev.uri}",
                    ev.event_date + "T00:00:00Z",
                    0.0,
                )
                if sig:
                    signals.append(sig)
        return signals

    def _build_signal(
        self,
        market: dict,
        nlp_score: float,
        matched_concepts: list[str],
        title: str,
        url: str,
        published_utc: str,
        age_min: float,
    ) -> Optional[RankedSignal]:
        yes_price, token_id = self.price_checker.get_yes_price(market)
        if not (0 < yes_price < 1):
            return None

        if yes_price > MIN_BUY_YES and yes_price < (1 - MIN_RESIDUAL):
            direction, residual = "BUY", 1.0 - yes_price
        elif yes_price < MAX_SELL_YES and yes_price > MIN_RESIDUAL:
            direction, residual = "SELL", yes_price
        else:
            return None

        if residual < self.args.min_residual:
            return None

        # Deduplicate
        sig_key = f"{market.get('conditionId','')}|{url}"
        if sig_key in self._seen_signals:
            return None
        self._seen_signals.add(sig_key)

        vol = float(market.get("volume24hr", 0) or 0)
        score, kelly = _score_signal(residual, nlp_score, vol, age_min)
        slug = market.get("slug", market.get("conditionId", ""))

        return RankedSignal(
            condition_id=market.get("conditionId", ""),
            market_question=market.get("question", ""),
            market_url=f"https://polymarket.com/event/{slug}",
            direction=direction,
            current_yes_price=yes_price,
            residual_pct=residual,
            nlp_score=nlp_score,
            volume_24h=vol,
            kelly_fraction=kelly,
            signal_score=score,
            headline_title=title,
            headline_url=url,
            headline_published_utc=published_utc,
            minutes_since_headline=age_min,
            clob_token_id=token_id,
            matched_concepts=matched_concepts,
        )

    # ── Display ───────────────────────────────────────────────────────────────

    def _print_signal(self, s: RankedSignal, rank: int = 1):
        pos_size = s.kelly_fraction * self.args.capital
        arrow = "↑ BUY " if s.direction == "BUY" else "↓ SELL"
        print(f"""
┌── LATENCY ARB SIGNAL #{self._signal_count} {'─'*50}
│  {arrow}  YES={s.current_yes_price:.1%}  Lag={s.residual_pct:.1%}  NLP={s.nlp_score:.3f}  Score={s.signal_score:.5f}
│  Market:   {s.market_question[:72]}
│  Headline: {s.headline_title[:72]}
│  Source:   {s.headline_url[:72]}
│  Published:{s.headline_published_utc}  ({s.minutes_since_headline:.1f} min ago)
│  Concepts: {', '.join(s.matched_concepts[:5]) if s.matched_concepts else 'n/a'}
│  Kelly: {s.kelly_fraction:.1%}  |  Position: ${pos_size:,.0f}  |  {s.market_url}
└{'─'*74}""")

    def _print_status(self):
        if self.paper_trader:
            status = self.paper_trader.get_status()
            equity = status["equity"]
            pnl    = status["total_pnl"]
            ret    = status["return_pct"]
            n_open = status["open_positions"]
            wr     = status["win_rate"]
            calls  = self.ingester._calls
            key_info = f"keys={len(self.ingester._keys)}"
            print(
                f"\n[STATUS]  Equity=${equity:,.2f}  P&L=${pnl:+,.2f} ({ret:+.2f}%)  "
                f"Open={n_open}  WR={wr:.0%}  Signals={self._signal_count}  "
                f"Polls={self._poll_count}  API calls={calls} ({key_info})"
            )
        else:
            calls = self.ingester._calls
            key_info = f"keys={len(self.ingester._keys)}"
            print(
                f"\n[STATUS]  Signals={self._signal_count}  Polls={self._poll_count}  "
                f"API calls={calls} ({key_info})"
            )

    # ── Position management ───────────────────────────────────────────────────

    def _update_positions(self):
        """Close paper positions that have converged toward resolution."""
        if not self.paper_trader:
            return
        from src.trading.live.paper_trading import PositionStatus

        to_close = []
        for pos_id, pos in self.paper_trader.account.positions.items():
            if pos.status != PositionStatus.OPEN:
                continue
            yes_price, _ = self.price_checker.get_yes_price({"conditionId": pos.market_id, "outcomePrices": []})
            if yes_price <= 0:
                continue
            # Update unrealized P&L
            pos.current_price = yes_price
            if pos.side.value == "buy":
                pos.unrealized_pnl = (yes_price - pos.entry_price) * pos.entry_size_usd
            else:
                pos.unrealized_pnl = (pos.entry_price - yes_price) * pos.entry_size_usd

            # Check convergence
            if pos.side.value == "buy" and yes_price >= CONVERGENCE_BUY:
                to_close.append((pos_id, f"convergence_buy@{yes_price:.3f}"))
            elif pos.side.value == "sell" and yes_price <= CONVERGENCE_SELL:
                to_close.append((pos_id, f"convergence_sell@{yes_price:.3f}"))

        for pos_id, note in to_close:
            result = self.paper_trader.close_position(pos_id, notes=note)
            if result:
                pos = self.paper_trader.account.positions[pos_id]
                log.info(
                    f"Closed {pos_id}: P&L=${pos.realized_pnl:+.2f}  ({note})"
                )
                self._log_equity()

    def _log_equity(self):
        if not self.paper_trader:
            return
        status = self.paper_trader.get_status()
        with open(self.equity_log, "a") as f:
            f.write(json.dumps({
                "ts": datetime.datetime.utcnow().isoformat(),
                **status,
            }) + "\n")

    # ── Paper execution ───────────────────────────────────────────────────────

    def _maybe_execute(self, sig: RankedSignal):
        if not self.paper_trader:
            return
        from src.trading.live.paper_trading import OrderSide

        # Skip if already have a position in this market
        from src.trading.live.paper_trading import PositionStatus
        for pos in self.paper_trader.account.positions.values():
            if pos.market_id == sig.condition_id and pos.status == PositionStatus.OPEN:
                return

        side = OrderSide.BUY if sig.direction == "BUY" else OrderSide.SELL
        size = sig.kelly_fraction * self.args.capital

        # Override token lookup: inject yes_price into a mock market dict
        # so PriceChecker.get_yes_price returns the known price
        order = self.paper_trader.submit_order(
            market_id=sig.condition_id,
            token_id=sig.clob_token_id,
            side=side,
            size_usd=size,
            signal_source="latency_arb",
            notes=f"nlp={sig.nlp_score:.3f} residual={sig.residual_pct:.2%}",
        )
        if order:
            log.info(
                f"Paper {sig.direction} ${size:,.0f} "
                f"YES@{sig.current_yes_price:.3f} "
                f"market={sig.market_question[:50]}"
            )
            self._log_equity()

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        log.info(
            f"Pipeline started  |  interval={self.args.interval}s  "
            f"capital=${self.args.capital:,.0f}  "
            f"mode={'paper-trade' if self.args.paper_trade else 'dry-run'}"
        )
        log.info(f"Signal log: {self.signal_log}")

        while True:
            t0 = time.time()
            self._poll_count += 1
            self._maybe_refresh_markets()

            # ── Fetch articles ────────────────────────────────────────────────
            articles = self.ingester.fetch_articles()
            if not articles:
                articles = self.rss_backup.fetch()

            signals = self._process_articles(articles)

            # ── Confirmed events (every 6th poll ≈ 3 min) ────────────────────
            if self._poll_count % 6 == 0:
                events = self.ingester.fetch_events(
                    min_articles=self.args.min_event_articles
                )
                signals += self._process_events(events)

            # ── Emit & (optionally) execute ───────────────────────────────────
            signals.sort(key=lambda s: s.signal_score, reverse=True)
            for sig in signals:
                self._signal_count += 1
                self._print_signal(sig, rank=self._signal_count)
                with open(self.signal_log, "a") as f:
                    f.write(json.dumps(asdict(sig)) + "\n")
                self._maybe_execute(sig)

            # ── Update positions ──────────────────────────────────────────────
            if self.paper_trader:
                self._update_positions()

            # ── Status ────────────────────────────────────────────────────────
            if self._poll_count % 10 == 0:
                self._print_status()

            elapsed = time.time() - t0
            time.sleep(max(0, self.args.interval - elapsed))


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Latency Arb Pipeline — news ingestion → NER matching → paper execution"
    )

    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--backtest",    action="store_true", help="Measure lag on 5 known historical events")
    mode.add_argument("--dry-run",     action="store_true", help="Print signals only, no paper positions")
    mode.add_argument("--paper-trade", action="store_true", help="Track virtual positions and P&L")

    p.add_argument("--newsapi-key",        default=None, nargs="+",
                   help="NewsAPI.ai / EventRegistry API key(s). Pass one or two keys; they rotate round-robin.")
    p.add_argument("--capital",            type=float, default=10_000, help="Virtual capital (default: $10,000)")
    p.add_argument("--interval",           type=int,   default=30,    help="Poll interval seconds (default: 30)")
    p.add_argument("--lookback",           type=int,   default=5,     help="Article lookback minutes (default: 5)")
    p.add_argument("--max-age-minutes",    type=float, default=60,    help="Ignore headlines older than N min")
    p.add_argument("--max-markets",        type=int,   default=3000,  help="Open markets to load (default: 3000)")
    p.add_argument("--max-positions",      type=int,   default=5,     help="Max concurrent paper positions (default: 5)")
    p.add_argument("--min-residual",       type=float, default=MIN_RESIDUAL, help="Min residual to fire signal")
    p.add_argument("--min-nlp-score",      type=float, default=0.15,  help="Min NLP match score (default: 0.15)")
    p.add_argument("--min-event-articles", type=int,   default=3,     help="Min articles for confirmed event")
    p.add_argument("--workers",            type=int,   default=5,     help="Workers for lag backtest")

    args = p.parse_args()

    if args.backtest:
        log.info("Running lag backtest on 5 known historical events…")
        results = run_lag_backtest(KNOWN_EVENTS, workers=args.workers)

        if not results:
            log.error("No measurements returned — check network / trade data availability.")
            return

        import numpy as np
        residuals = [m.residual_at_headline for m in results]
        lags      = [m.minutes_to_full for m in results]

        print(f"\n{'='*82}")
        print("  LAG MEASUREMENT RESULTS")
        print(f"{'='*82}")
        print(f"{'Event':<50} {'Resid@HL':>8} {'Full in':>8} {'At full':>8}")
        print("-"*82)
        for m in results:
            print(
                f"{m.headline[:49]:<50} "
                f"{m.residual_at_headline:>7.1%}  "
                f"{m.minutes_to_full:>6.0f} min  "
                f"{abs(m.resolution - m.price_at_full):>7.1%}"
            )
        print("-"*82)
        print(f"  Mean residual at headline:       {np.mean(residuals):.1%}")
        print(f"  Mean minutes to full discovery:  {np.mean(lags):.0f} min")
        print(f"  Median minutes to full discovery:{np.median(lags):.0f} min")
        print(f"{'='*82}\n")

        out = Path("backtests/latency_arb")
        out.mkdir(parents=True, exist_ok=True)
        out_file = out / "lag_backtest.json"
        with open(out_file, "w") as f:
            json.dump([asdict(m) for m in results], f, indent=2, default=str)
        log.info(f"Results saved to {out_file}")
        return

    if not args.newsapi_key:
        p.error("--newsapi-key is required for --dry-run and --paper-trade modes")
    # Normalise to a list (nargs="+" always returns a list, but guard either way)
    if isinstance(args.newsapi_key, str):
        args.newsapi_key = [args.newsapi_key]
    n_keys = len(args.newsapi_key)
    if n_keys > 1:
        log.info(f"Using {n_keys} API keys (round-robin rotation)")

    pipeline = Pipeline(args)
    try:
        pipeline.run()
    except KeyboardInterrupt:
        log.info("Pipeline stopped.")
        if pipeline.paper_trader:
            pipeline.paper_trader.print_status()


if __name__ == "__main__":
    main()

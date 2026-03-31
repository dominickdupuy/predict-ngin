"""
news_monitor.py — Real-time news headline monitor for latency arbitrage
Uses NewsAPI.ai (EventRegistry) as the primary source.

Key advantages over GDELT/RSS:
  - dateTimePub: exact publish timestamp to the second
  - Structured events: multiple outlets confirming = higher confidence
  - Concept extraction: entities (people, locations, orgs) for direct market matching
  - Sentiment scores: filter for event-confirming headlines (negative = conflict/arrest)
  - eventUri: link articles to structured events for multi-source confirmation

API: https://newsapi.ai  (EventRegistry)
Key: stored in config/local.yaml under news_api_key, or pass --newsapi-key

Ingest modes (in order of quality):
  1. EventRegistry article stream  — primary, precise timestamps, rich metadata
  2. EventRegistry event stream     — secondary, multi-article confirmation
  3. RSS feeds                      — fallback if API quota exhausted

Usage:
    python scripts/live/news_monitor.py --newsapi-key KEY [--interval 30] [--log signals.jsonl]

Run locally or on HPC (EventRegistry is reachable from the cluster).
"""

import argparse
import datetime
import hashlib
import json
import logging
import time
from dataclasses import dataclass, asdict, field
from typing import Optional
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("news_monitor")

# ── API config ────────────────────────────────────────────────────────────────
ER_BASE    = "https://newsapi.ai/api/v1"
GAMMA_API  = "https://gamma-api.polymarket.com"
CLOB_API   = "https://clob.polymarket.com"
DATA_API   = "https://data-api.polymarket.com"

# Price gates aligned with whale strategy
MIN_BUY_YES  = 0.80
MAX_SELL_YES = 0.20
MIN_RESIDUAL = 0.05   # skip if already >95% or <5% (need meaningful lag)

# Categories we care about for prediction market relevance
RELEVANT_CATEGORIES = {
    "news/Politics",
    "news/Conflicts, War and Peace",
    "news/Law",
    "news/Economy, Business and Finance",
    "news/Science and Technology",
    "news/Arts and Entertainment",
    "news/Weather",
    "news/Disaster, Accident and Emergency Incident",
}

# Keywords that signal a confirmable outcome
CONFIRMING_KEYWORDS = [
    "ceasefire", "arrested", "convicted", "sentenced", "elected", "won",
    "captured", "launched", "strikes", "signed", "declared", "approved",
    "rejected", "killed", "died", "resigned", "appointed", "agreed",
    "confirmed", "announced", "opened", "closed", "reached", "achieved",
]


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Article:
    uri: str
    title: str
    body: str
    published_utc: datetime.datetime   # dateTimePub — exact publish time
    crawled_utc: datetime.datetime     # dateTime    — when ER processed it
    source_name: str
    url: str
    sentiment: float                   # -1 to +1
    concepts: list[dict]               # [{label, score, type, uri}]
    categories: list[dict]             # [{label, score}]
    event_uri: Optional[str]           # links to a structured event
    is_duplicate: bool

    def id(self) -> str:
        return hashlib.md5(self.uri.encode()).hexdigest()[:12]

    def concept_labels(self) -> list[str]:
        return [c.get("label", {}).get("eng", "") for c in self.concepts
                if c.get("label", {}).get("eng")]

    def is_relevant(self) -> bool:
        """Quick filter: has prediction-market-relevant content."""
        if self.is_duplicate:
            return False
        cats = {c.get("label", "") for c in self.categories}
        if cats & RELEVANT_CATEGORIES:
            return True
        text = (self.title + " " + self.body[:300]).lower()
        return any(kw in text for kw in CONFIRMING_KEYWORDS)


@dataclass
class ConfirmedEvent:
    """An EventRegistry structured event — multiple outlets confirming same story."""
    uri: str
    title: str
    summary: str
    event_date: str                  # YYYY-MM-DD
    article_count: int
    sentiment: float
    concepts: list[dict]
    categories: list[dict]
    location: Optional[dict]


@dataclass
class LatArbSignal:
    headline_title: str
    headline_url: str
    published_utc: str               # exact to the second
    crawl_lag_seconds: float         # how long after publish did ER process it
    market_id: str
    market_question: str
    market_url: str
    direction: str                   # "BUY" or "SELL"
    current_yes_price: float
    residual_pct: float
    match_score: float               # concept overlap score
    sentiment: float                 # headline sentiment
    article_count: int               # 1 = single article; >1 = event confirmed
    detected_utc: str
    clob_token_id: str
    minutes_since_headline: float
    confirming_concepts: list[str]   # concepts that matched the market


# ── EventRegistry ingester ────────────────────────────────────────────────────

class EventRegistryIngester:
    """
    Polls NewsAPI.ai for:
      1. New articles (fast path) — sorted by dateTimePub, last N minutes
      2. Structured events (confirmation path) — multi-article events

    Rate: ~1 call per poll per mode = ~120 calls/hour at 30s interval.
    2,000 free searches ≈ ~16 hours of continuous monitoring.
    """

    def __init__(self, api_key: str, lookback_minutes: int = 5, session=None):
        self.key = api_key
        self.lookback = lookback_minutes
        self.seen_articles: set[str] = set()
        self.seen_events: set[str] = set()
        self.s = session or requests.Session()
        self.s.headers["User-Agent"] = "polymarket-latency-arb/1.0"
        self._calls = 0

    def _ts(self, dt: datetime.datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S")

    def _parse_dt(self, s: str) -> datetime.datetime:
        if not s:
            return datetime.datetime.utcnow()
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.datetime.strptime(s, fmt)
            except ValueError:
                continue
        return datetime.datetime.utcnow()

    def _utc_now(self) -> datetime.datetime:
        """
        Robust UTC time: use time.time() which is always epoch-based,
        avoiding issues where the system clock is set to a local timezone
        but datetime.utcnow() reflects wall-clock time instead of UTC.
        """
        return datetime.datetime.utcfromtimestamp(time.time())

    def fetch_articles(self) -> list[Article]:
        """
        Poll article stream filtered by relevant categories.
        Uses dateStart=YYYY-MM-DD (ER date format) and deduplicates by URI.
        Lookback window applied in Python on dateTimePub to handle TZ offsets.
        """
        now   = self._utc_now()
        since = now - datetime.timedelta(minutes=self.lookback)
        today = now.strftime("%Y-%m-%d")

        # Category URIs — must be repeated params, not pipe-delimited
        CATEGORY_URIS = [
            "news/Politics",
            "news/Conflicts, War and Peace",
            "news/Law",
            "news/Disaster, Accident and Emergency Incident",
        ]

        # Build as list of tuples for repeated keys
        params = [
            ("apiKey",                self.key),
            ("lang",                  "eng"),
            ("articlesCount",         100),
            ("articlesSortBy",        "date"),
            ("articlesSortByAsc",     False),
            ("resultType",            "articles"),
            ("includeArticleCategories", True),
            ("includeArticleConcepts",   True),
            ("includeArticleSentiment",  True),
            ("includeArticleBody",        True),
            ("isDuplicateFilter",     "skipDuplicates"),
            ("dateStart",             today),
        ] + [("categoryUri", c) for c in CATEGORY_URIS]
        try:
            r = self.s.get(f"{ER_BASE}/article/getArticles", params=params, timeout=20)
            r.raise_for_status()
            self._calls += 1
            results = r.json().get("articles", {}).get("results", [])
        except Exception as e:
            log.warning(f"Article fetch error: {e}")
            return []

        articles = []
        for raw in results:
            uri = raw.get("uri", "")
            if not uri or uri in self.seen_articles:
                continue

            pub_dt   = self._parse_dt(raw.get("dateTimePub", ""))
            crawl_dt = self._parse_dt(raw.get("dateTime", ""))

            # Apply lookback filter in Python (robust to TZ offset on HPC)
            if pub_dt < since:
                continue

            self.seen_articles.add(uri)
            articles.append(Article(
                uri=uri,
                title=raw.get("title", ""),
                body=raw.get("body", "") or "",
                published_utc=pub_dt,
                crawled_utc=crawl_dt,
                source_name=(raw.get("source") or {}).get("title", ""),
                url=raw.get("url", ""),
                sentiment=float(raw.get("sentiment") or 0),
                concepts=raw.get("concepts") or [],
                categories=raw.get("categories") or [],
                event_uri=raw.get("eventUri"),
                is_duplicate=bool(raw.get("isDuplicate")),
            ))

        log.debug(f"Articles: {len(articles)} new  (API calls used: {self._calls})")
        return articles

    def fetch_events(self, min_articles: int = 3) -> list[ConfirmedEvent]:
        """
        Poll event stream for recently-updated events with ≥ min_articles
        from multiple outlets — these are the highest-confidence signals.
        """
        now   = datetime.datetime.utcnow()
        since = now - datetime.timedelta(minutes=self.lookback * 3)

        params = {
            "apiKey": self.key,
            "lang": "eng",
            "eventsCount": 50,
            "eventsSortBy": "date",
            "eventsSortByAsc": False,
            "includeEventSummary": True,
            "includeEventSentiment": True,
            "includeEventConcepts": True,
            "includeEventCategories": True,
            "includeEventArticleCounts": True,
            "includeEventLocation": True,
            "dateStart": self._ts(since),
        }
        try:
            r = self.s.get(f"{ER_BASE}/event/getEvents", params=params, timeout=20)
            r.raise_for_status()
            self._calls += 1
            results = r.json().get("events", {}).get("results", [])
        except Exception as e:
            log.warning(f"Event fetch error: {e}")
            return []

        events = []
        for raw in results:
            uri = raw.get("uri", "")
            if not uri or uri in self.seen_events:
                continue
            total = raw.get("totalArticleCount", 0)
            if total < min_articles:
                continue
            self.seen_events.add(uri)
            events.append(ConfirmedEvent(
                uri=uri,
                title=(raw.get("title") or {}).get("eng", ""),
                summary=(raw.get("summary") or {}).get("eng", ""),
                event_date=raw.get("eventDate", ""),
                article_count=total,
                sentiment=float(raw.get("sentiment") or 0),
                concepts=raw.get("concepts") or [],
                categories=raw.get("categories") or [],
                location=raw.get("location"),
            ))

        log.debug(f"Events: {len(events)} new confirmed events")
        return events


# ── RSS fallback ──────────────────────────────────────────────────────────────

class RSSFallback:
    FEEDS = [
        "https://feeds.reuters.com/reuters/worldNews",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://feeds.npr.org/1004/rss.xml",
    ]

    def __init__(self, lookback_minutes: int = 10):
        self.lookback = lookback_minutes
        self.seen: set = set()
        try:
            import feedparser
            self._fp = feedparser
        except ImportError:
            self._fp = None

    def fetch(self) -> list[Article]:
        if not self._fp:
            return []
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(minutes=self.lookback)
        articles = []
        for url in self.FEEDS:
            try:
                feed = self._fp.parse(url)
                for e in feed.entries:
                    link = e.get("link", "")
                    if link in self.seen:
                        continue
                    import calendar
                    ps = e.get("published_parsed")
                    pub = datetime.datetime.utcfromtimestamp(calendar.timegm(ps)) if ps else datetime.datetime.utcnow()
                    if pub < cutoff:
                        continue
                    self.seen.add(link)
                    articles.append(Article(
                        uri=link, title=e.get("title",""), body=e.get("summary",""),
                        published_utc=pub, crawled_utc=datetime.datetime.utcnow(),
                        source_name=feed.feed.get("title","rss"), url=link,
                        sentiment=0.0, concepts=[], categories=[], event_uri=None,
                        is_duplicate=False,
                    ))
            except Exception:
                pass
        return articles


# ── Market loader & matcher ───────────────────────────────────────────────────

def load_open_markets(session=None, limit: int = 3000) -> list[dict]:
    s = session or requests.Session()
    s.headers["User-Agent"] = "latency-arb/1.0"
    markets, offset, batch = [], 0, 500
    while len(markets) < limit:
        try:
            r = s.get(f"{GAMMA_API}/markets",
                      params={"closed": "false", "active": "true", "limit": batch, "offset": offset},
                      timeout=20)
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


class ConceptMatcher:
    """
    Matches news concepts (people, locations, organizations) to market questions.

    Two-stage:
      1. Concept overlap — exact match on entity labels extracted by EventRegistry
      2. TF-IDF fallback — for markets without strong entity matches
    """

    def __init__(self, markets: list[dict]):
        self.markets = markets
        self._build_tfidf()

    def _build_tfidf(self):
        questions = [m.get("question", "") for m in self.markets]
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity
            import numpy as np
            self._vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1)
            self._tfidf = self._vectorizer.fit_transform(questions)
            self._cosine = cosine_similarity
            self._np = np
            self._tfidf_ok = True
        except ImportError:
            self._tfidf_ok = False

    def _concept_score(self, concept_labels: list[str], question: str) -> float:
        """How many concept labels appear in the market question? Normalised 0-1."""
        if not concept_labels:
            return 0.0
        q_lower = question.lower()
        hits = sum(1 for c in concept_labels if c and c.lower() in q_lower)
        return hits / len(concept_labels)

    def match(
        self,
        title: str,
        body: str,
        concepts: list[dict],
        top_k: int = 5,
        min_score: float = 0.15,
    ) -> list[tuple[dict, float, list[str]]]:
        """
        Returns list of (market, score, matched_concepts).

        Scoring logic:
          - If article has structured concepts AND at least one matches the market
            question: blended score (concept-weighted). These are high-confidence.
          - If article has concepts but NONE match: TF-IDF only, require score >= 0.50
            to avoid spurious word-overlap matches (e.g., 'convicted' → wrong market).
          - If article has no concepts: TF-IDF only, require score >= 0.40.
        """
        concept_labels = [c.get("label", {}).get("eng", "") for c in concepts
                          if isinstance(c.get("label"), dict)]
        has_concepts = len(concept_labels) > 0

        # Sports bracket markets (pure bracket outcomes) are unaffected by news articles
        _SPORTS_SKIP = {"nba finals", "nba western", "nba eastern", "fifa world cup",
                        "stanley cup", "nfl super bowl", "super bowl", "world series",
                        "champions league", "premier league title", "la liga title"}

        results = []
        text = title + " " + body[:500]

        if self._tfidf_ok:
            vec  = self._vectorizer.transform([text])
            sims = self._cosine(vec, self._tfidf).flatten()
            top_idx = self._np.argsort(sims)[::-1][:top_k * 5]
            for i in top_idx:
                m = self.markets[i]
                q_lower = m.get("question", "").lower()
                # Skip pure-bracket sports markets
                if any(s in q_lower for s in _SPORTS_SKIP):
                    continue
                tfidf_score   = float(sims[i])
                matched = [c for c in concept_labels
                           if c and c.lower() in q_lower]
                concept_score = len(matched) / max(len(concept_labels), 1) if matched else 0.0

                if matched:
                    # Concept overlap confirmed — strong signal.
                    # Use blended score: concept overlap is the reliable signal,
                    # TF-IDF provides a secondary sanity check.
                    blended = 0.3 * tfidf_score + 0.7 * concept_score
                    threshold = min_score
                else:
                    # No concept matched the market question.
                    # TF-IDF alone produces too many false positives from keyword overlap
                    # (e.g., "sentenced in Nalchik" matching "Weinstein sentencing" markets).
                    # Require at least one named-entity hit in the market question.
                    continue

                if blended >= threshold:
                    results.append((m, blended, matched))
        else:
            words = set(text.lower().split())
            for m in self.markets:
                qwords = set(m.get("question", "").lower().split())
                overlap = words & qwords
                score = len(overlap) / max(len(words | qwords), 1)
                matched = [c for c in concept_labels
                           if c and c.lower() in m.get("question", "").lower()]
                if score >= (min_score if matched else 0.40):
                    results.append((m, score, matched))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]


# ── Price checker ─────────────────────────────────────────────────────────────

class PriceChecker:
    def __init__(self, session=None):
        self.s = session or requests.Session()
        self.s.headers["User-Agent"] = "latency-arb/1.0"

    def get_yes_price(self, market: dict) -> tuple[float, str]:
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

        # Try Gamma price first (cheapest)
        try:
            yes_price = float(prices[0]) if prices else 0.0
            if yes_price > 0:
                return yes_price, token_id
        except Exception:
            pass

        # Fallback: CLOB order book mid-price
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

        return 0.5, token_id  # unknown


# ── Signal emitter ────────────────────────────────────────────────────────────

class SignalEmitter:
    def __init__(self, log_path: Optional[str] = None):
        self.log_path = log_path
        self.emitted: set[str] = set()
        self.total = 0

    def emit(self, signal: LatArbSignal):
        sig_id = hashlib.md5((signal.market_id + signal.headline_url).encode()).hexdigest()[:10]
        if sig_id in self.emitted:
            return
        self.emitted.add(sig_id)
        self.total += 1

        d = signal.direction
        arrow = "↑ BUY  YES" if d == "BUY" else "↓ SELL YES"
        conf  = f"({signal.article_count} outlets)" if signal.article_count > 1 else "(1 article)"
        lag_s = f"{signal.residual_pct:.1%}"

        print(f"""
┌── LATENCY ARB SIGNAL #{self.total} {'─'*52}
│  {arrow}  │  YES = {signal.current_yes_price:.1%}  │  Lag: {lag_s} to resolution  {conf}
│  Market:   {signal.market_question[:70]}
│  Headline: {signal.headline_title[:70]}
│  Source:   {signal.headline_url[:70]}
│  Published:{signal.published_utc}  ({signal.minutes_since_headline:.1f} min ago)
│  Sentiment:{signal.sentiment:+.2f}  │  Match: {signal.match_score:.3f}
│  Concepts: {', '.join(signal.confirming_concepts[:5])}
│  Trade at: {signal.market_url}
└{'─'*74}""")

        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(asdict(signal), default=str) + "\n")


# ── Main monitor loop ─────────────────────────────────────────────────────────

def run(args):
    log.info("Starting latency arb news monitor (NewsAPI.ai / EventRegistry)")
    log.info(f"Interval: {args.interval}s  |  Lookback: {args.lookback}min  |  Capital: ${args.capital:,.0f}")

    session    = requests.Session()
    ingester   = EventRegistryIngester(args.newsapi_key, lookback_minutes=args.lookback, session=session)
    rss_backup = RSSFallback(lookback_minutes=args.lookback * 2)
    checker    = PriceChecker(session=session)
    emitter    = SignalEmitter(log_path=args.log)

    markets              = load_open_markets(session=session, limit=args.max_markets)
    matcher              = ConceptMatcher(markets)
    last_market_refresh  = time.time()
    MARKET_REFRESH_SECS  = 1800  # refresh market list every 30 min
    poll_count           = 0

    log.info(f"Polling every {args.interval}s. Ctrl+C to stop.")

    while True:
        t0 = time.time()
        poll_count += 1

        # Refresh market list
        if time.time() - last_market_refresh > MARKET_REFRESH_SECS:
            markets = load_open_markets(session=session, limit=args.max_markets)
            matcher = ConceptMatcher(markets)
            last_market_refresh = time.time()

        # ── Article stream (every poll) ───────────────────────────────────────
        articles = ingester.fetch_articles()
        if not articles:
            # Fallback to RSS
            articles = rss_backup.fetch()
            if articles:
                log.info(f"RSS fallback: {len(articles)} articles")

        for art in articles:
            if not art.is_relevant():
                continue
            age_min = (datetime.datetime.utcnow() - art.published_utc).total_seconds() / 60
            if age_min > args.max_age_minutes:
                continue

            matches = matcher.match(art.title, art.body, art.concepts, top_k=5, min_score=args.min_nlp_score)
            for market, score, matched_concepts in matches:
                yes_price, token_id = checker.get_yes_price(market)
                if not (0 < yes_price < 1):
                    continue

                if yes_price > MIN_BUY_YES and yes_price < (1 - MIN_RESIDUAL):
                    direction, residual = "BUY", 1.0 - yes_price
                elif yes_price < MAX_SELL_YES and yes_price > MIN_RESIDUAL:
                    direction, residual = "SELL", yes_price
                else:
                    continue
                if residual < args.min_residual:
                    continue

                slug = market.get("slug", market.get("conditionId", ""))
                emitter.emit(LatArbSignal(
                    headline_title=art.title,
                    headline_url=art.url,
                    published_utc=art.published_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    crawl_lag_seconds=(art.crawled_utc - art.published_utc).total_seconds(),
                    market_id=market.get("conditionId", ""),
                    market_question=market.get("question", ""),
                    market_url=f"https://polymarket.com/event/{slug}",
                    direction=direction,
                    current_yes_price=yes_price,
                    residual_pct=residual,
                    match_score=score,
                    sentiment=art.sentiment,
                    article_count=1,
                    detected_utc=datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    clob_token_id=token_id,
                    minutes_since_headline=age_min,
                    confirming_concepts=matched_concepts,
                ))

        # ── Event stream (every 6th poll = every ~3 min) ─────────────────────
        if poll_count % 6 == 0:
            events = ingester.fetch_events(min_articles=args.min_event_articles)
            for ev in events:
                age_min = 0.0  # events don't have exact minute timestamps
                matches = matcher.match(
                    ev.title, ev.summary, ev.concepts, top_k=3, min_score=args.min_nlp_score
                )
                for market, score, matched_concepts in matches:
                    yes_price, token_id = checker.get_yes_price(market)
                    if not (0 < yes_price < 1):
                        continue
                    if yes_price > MIN_BUY_YES and yes_price < (1 - MIN_RESIDUAL):
                        direction, residual = "BUY", 1.0 - yes_price
                    elif yes_price < MAX_SELL_YES and yes_price > MIN_RESIDUAL:
                        direction, residual = "SELL", yes_price
                    else:
                        continue
                    if residual < args.min_residual:
                        continue

                    slug = market.get("slug", market.get("conditionId", ""))
                    emitter.emit(LatArbSignal(
                        headline_title=ev.title,
                        headline_url=f"https://newsapi.ai/event/{ev.uri}",
                        published_utc=ev.event_date + "T00:00:00Z",
                        crawl_lag_seconds=0.0,
                        market_id=market.get("conditionId", ""),
                        market_question=market.get("question", ""),
                        market_url=f"https://polymarket.com/event/{slug}",
                        direction=direction,
                        current_yes_price=yes_price,
                        residual_pct=residual,
                        match_score=score,
                        sentiment=ev.sentiment,
                        article_count=ev.article_count,
                        detected_utc=datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                        clob_token_id=token_id,
                        minutes_since_headline=age_min,
                        confirming_concepts=matched_concepts,
                    ))

        if poll_count % 10 == 0:
            log.info(f"Polls: {poll_count}  |  API calls used: {ingester._calls}  |  Signals: {emitter.total}")

        elapsed = time.time() - t0
        time.sleep(max(0, args.interval - elapsed))


def main():
    p = argparse.ArgumentParser(description="Latency arb news monitor — NewsAPI.ai")
    p.add_argument("--newsapi-key",         required=True,  help="NewsAPI.ai API key")
    p.add_argument("--interval",            type=int,   default=30,   help="Poll interval seconds (default: 30)")
    p.add_argument("--lookback",            type=int,   default=5,    help="Article lookback minutes (default: 5)")
    p.add_argument("--max-age-minutes",     type=float, default=60,   help="Ignore headlines older than N min (default: 60)")
    p.add_argument("--max-markets",         type=int,   default=3000, help="Max open markets (default: 3000)")
    p.add_argument("--min-residual",        type=float, default=0.05, help="Min lag to fire signal (default: 5%%)")
    p.add_argument("--min-nlp-score",       type=float, default=0.15, help="Min match score (default: 0.15)")
    p.add_argument("--min-event-articles",  type=int,   default=3,    help="Min articles for confirmed event (default: 3)")
    p.add_argument("--capital",             type=float, default=10000, help="Capital for position sizing display")
    p.add_argument("--log",                 default="signals.jsonl",  help="JSONL output file")
    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()

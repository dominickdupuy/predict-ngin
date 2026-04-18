"""
SemanticMatcher — NER + sentence-transformer market matching for latency arb.

Two-stage pipeline:
  Stage 1: EventRegistry concept overlap (exact named-entity match)
           High precision — only fires when ER-extracted entities appear verbatim
           in the market question. Boosts the blended score when it hits.
  Stage 2: Sentence-transformer cosine similarity (all-MiniLM-L6-v2, dim=384)
           Catches paraphrase and synonym matches that keyword overlap misses.
           E.g. "cabinet reshuffle" ↔ "government personnel change"

Hybrid score (when concept hit):  0.4 * semantic + 0.6 * concept_overlap
Hybrid score (no concept hit):    semantic only, threshold raised to 0.50
                                  (requires strong semantic similarity without
                                  any named-entity confirmation)

Geographic consistency filter (require_geo_consistency=True):
  Rejects matches where the article's detected region (from EventRegistry concept
  URIs) is clearly unrelated to the market's geographic scope.  The filter uses
  a coarse continent-level mapping — it only rejects when there is an explicit
  continent mismatch (e.g., article is Africa-only, market mentions United States).
  It never rejects when either side lacks geographic context.

Why skip TF-IDF: sentence-transformers already subsumes TF-IDF via contextual
embeddings, and TF-IDF generates many false-positive word-overlap matches
(e.g., "convicted" matching wrong conviction markets).

Why concept overlap still matters: it provides high-precision named-entity
confirmation. A headline about "Kim Jong-un" reliably contains that exact string,
and ER extracts it as a concept. This allows us to lower the semantic threshold
for concept-confirmed matches from 0.50 → 0.20, capturing weaker paraphrases.
"""

import logging
import time
from typing import Optional

import numpy as np
import requests

log = logging.getLogger("semantic_matcher")

_MODEL_NAME = "all-MiniLM-L6-v2"

# Sports bracket markets are independent of news — exclude them
_SPORTS_SKIP = {
    "nba finals", "nba western", "nba eastern", "fifa world cup",
    "stanley cup", "nfl super bowl", "super bowl", "world series",
    "champions league", "premier league title", "la liga title",
    "nba championship", "mlb world series",
}

# Geographic consistency filter:
# Maps EventRegistry location concept URI substrings → continent label.
# Only the most unambiguous mappings are included to avoid over-filtering.
_GEO_URI_TO_CONTINENT: dict[str, str] = {
    # Africa
    "concept/location/nigeria": "africa",
    "concept/location/kenya": "africa",
    "concept/location/ghana": "africa",
    "concept/location/ethiopia": "africa",
    "concept/location/southafrica": "africa",
    "concept/location/egypt": "africa",
    "concept/location/cameroon": "africa",
    "concept/location/senegal": "africa",
    "concept/location/tanzania": "africa",
    "concept/location/uganda": "africa",
    "concept/location/zimbabwe": "africa",
    "concept/location/mozambique": "africa",
    "concept/location/angola": "africa",
    "concept/location/zambia": "africa",
    "concept/location/ivory": "africa",
    "concept/location/côted": "africa",
    # Latin America (not US/Canada)
    "concept/location/brazil": "latam",
    "concept/location/argentina": "latam",
    "concept/location/colombia": "latam",
    "concept/location/venezuela": "latam",
    "concept/location/peru": "latam",
    "concept/location/chile": "latam",
    "concept/location/bolivia": "latam",
    # North America
    "concept/location/unitedstates": "northam",
    "concept/location/canada": "northam",
    # Europe
    "concept/location/unitedkingdom": "europe",
    "concept/location/france": "europe",
    "concept/location/germany": "europe",
    "concept/location/spain": "europe",
    "concept/location/italy": "europe",
    "concept/location/poland": "europe",
    "concept/location/ukraine": "europe",
    "concept/location/russia": "europe",
    # Asia / Middle East
    "concept/location/china": "asia",
    "concept/location/japan": "asia",
    "concept/location/india": "asia",
    "concept/location/southkorea": "asia",
    "concept/location/northkorea": "asia",
    "concept/location/israel": "mideast",
    "concept/location/iran": "mideast",
    "concept/location/saudi": "mideast",
    "concept/location/turkey": "mideast",
}

# Market question keywords that signal geographic scope.
# If article is in a clearly different region, reject.
_MARKET_GEO_KEYWORDS: dict[str, str] = {
    # → northam
    "united states": "northam", "u.s.": "northam", "american": "northam",
    "congress": "northam", "senate": "northam", "republican": "northam",
    "democrat": "northam", "white house": "northam", "trump": "northam",
    "biden": "northam", "harris": "northam", "governor": "northam",
    # → europe
    "european union": "europe", " eu ": "europe", "nato": "europe",
    "ukraine": "europe", "russia": "europe", "germany": "europe",
    "france": "europe", "uk ": "europe", "british": "europe",
    # → asia / mideast
    "china": "asia", "taiwan": "asia", "japan": "asia",
    "south korea": "asia", "north korea": "asia",
    "israel": "mideast", "iran": "mideast", "hamas": "mideast",
    # → latam
    "brazil": "latam", "argentina": "latam", "colombia": "latam",
}

# Continent pairs that are clearly incompatible
_INCOMPATIBLE_PAIRS: frozenset = frozenset({
    frozenset({"africa", "northam"}),
    frozenset({"africa", "europe"}),
    frozenset({"africa", "asia"}),
    frozenset({"africa", "mideast"}),
    frozenset({"africa", "latam"}),
    frozenset({"latam", "northam"}),
    frozenset({"latam", "europe"}),
    frozenset({"latam", "asia"}),
    frozenset({"latam", "mideast"}),
})


class SemanticMatcher:
    """
    Matches news articles to Polymarket questions using NER + semantic similarity.

    Usage:
        matcher = SemanticMatcher(markets)       # encodes market questions at init
        results = matcher.match(title, body, concepts, top_k=5)
        # returns [(market_dict, score, matched_concepts), ...]

    Parameters
    ----------
    require_concepts : bool
        If True (default), only return matches where at least one EventRegistry
        named-entity appears verbatim in the market question.  Eliminates false
        positives from generic political vocabulary (e.g., "governor" or "party"
        matching unrelated markets across different countries).
    require_geo_consistency : bool
        If True (default), suppress matches where the article's detected region
        is clearly incompatible with the market's geographic scope (e.g., an
        article tagged as Africa-only should not match US-election markets).
    """

    def __init__(
        self,
        markets: list[dict],
        batch_size: int = 256,
        require_concepts: bool = True,
        require_geo_consistency: bool = True,
    ):
        self.markets: list[dict] = []
        self._embeddings: Optional[np.ndarray] = None
        self._model = None
        self._batch_size = batch_size
        self.require_concepts = require_concepts
        self.require_geo_consistency = require_geo_consistency
        self._load_model()
        self.set_markets(markets)

    # ── model loading ─────────────────────────────────────────────────────────

    def _load_model(self):
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(_MODEL_NAME)
            log.info(f"Loaded sentence-transformer: {_MODEL_NAME}")
        except Exception as e:
            log.warning(f"sentence-transformers unavailable: {e}. Falling back to keyword-only matching.")

    # ── market index ──────────────────────────────────────────────────────────

    def set_markets(self, markets: list[dict]):
        """Encode all market questions.  Call again after a market-list refresh."""
        # Filter to markets with non-empty questions
        self.markets = [m for m in markets if m.get("question", "").strip()]

        if self._model is None:
            self._embeddings = None
            return

        questions = [m["question"] for m in self.markets]
        t0 = time.time()
        self._embeddings = self._model.encode(
            questions,
            batch_size=self._batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,   # pre-normalise → dot product = cosine
        )
        log.info(
            f"Encoded {len(self.markets):,} market questions in {time.time()-t0:.1f}s "
            f"(dim={self._embeddings.shape[1]})"
        )

    # ── matching ──────────────────────────────────────────────────────────────

    def match(
        self,
        title: str,
        body: str,
        concepts: list[dict],
        top_k: int = 5,
        min_score: float = 0.20,
    ) -> list[tuple[dict, float, list[str]]]:
        """
        Match a headline to the most relevant open markets.

        Parameters
        ----------
        title : str
            Headline title.
        body : str
            Article body (first ~300 chars used).
        concepts : list[dict]
            EventRegistry concept objects — each has {"label": {"eng": "..."}, ...}.
        top_k : int
            Max results to return.
        min_score : float
            Minimum blended score threshold for concept-confirmed matches.
            Semantic-only matches use max(min_score, 0.50) regardless.

        Returns
        -------
        list of (market_dict, blended_score, matched_concept_labels)
        """
        if not self.markets:
            return []

        concept_labels = [
            c.get("label", {}).get("eng", "")
            for c in concepts
            if isinstance(c.get("label"), dict) and c.get("label", {}).get("eng", "")
        ]

        # Detect article's geographic region from concept URIs
        article_continent = self._detect_article_continent(concepts)

        text = (title + " " + body[:300]).strip()

        # ── semantic similarity ───────────────────────────────────────────────
        semantic_scores = self._semantic_scores(text)

        # ── build candidates ──────────────────────────────────────────────────
        n_candidates = min(len(self.markets), max(top_k * 10, 50))
        candidate_idx = np.argpartition(semantic_scores, -n_candidates)[-n_candidates:]
        candidate_idx = candidate_idx[np.argsort(semantic_scores[candidate_idx])[::-1]]

        results = []
        for i in candidate_idx:
            m = self.markets[i]
            q_lower = m.get("question", "").lower()

            # Skip bracket sports markets
            if any(s in q_lower for s in _SPORTS_SKIP):
                continue

            sem = float(semantic_scores[i])

            # Concept overlap: how many ER-extracted entities appear in the question?
            matched = [c for c in concept_labels if c and c.lower() in q_lower]
            has_concept_hit = len(matched) > 0

            # require_concepts: skip if no named-entity confirmed the match
            if self.require_concepts and not has_concept_hit:
                continue

            # Geographic consistency check
            if self.require_geo_consistency and article_continent:
                market_continent = self._detect_market_continent(q_lower)
                if market_continent and _incompatible(article_continent, market_continent):
                    continue

            concept_overlap = len(matched) / max(len(concept_labels), 1) if matched else 0.0

            if has_concept_hit:
                # Concept-confirmed: blended score with lower threshold
                blended = 0.4 * sem + 0.6 * concept_overlap
                threshold = min_score
            else:
                # Semantic-only (only reached here if require_concepts=False)
                blended = sem
                threshold = max(min_score, 0.50)

            if blended >= threshold:
                results.append((m, round(blended, 4), matched))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    # ── geographic helpers ────────────────────────────────────────────────────

    def _detect_article_continent(self, concepts: list[dict]) -> Optional[str]:
        """
        Detect the dominant continent of an article from EventRegistry concept URIs.
        Returns a continent label, or None if ambiguous / no location concepts.
        """
        continents: dict[str, int] = {}
        for c in concepts:
            uri = c.get("uri", "") or ""
            uri_lower = uri.lower().replace("-", "").replace("_", "")
            for key, continent in _GEO_URI_TO_CONTINENT.items():
                key_norm = key.replace("-", "").replace("_", "")
                if key_norm in uri_lower:
                    continents[continent] = continents.get(continent, 0) + 1
                    break  # one continent per concept

        if not continents:
            return None
        # Only commit to a continent if it is the only one present (no ambiguity)
        if len(continents) == 1:
            return next(iter(continents))
        # If multiple continents: return the dominant one, but only if it has
        # at least 2× as many hits as the second-most common (clear dominance)
        sorted_items = sorted(continents.items(), key=lambda x: -x[1])
        if sorted_items[0][1] >= 2 * sorted_items[1][1]:
            return sorted_items[0][0]
        return None  # ambiguous

    def _detect_market_continent(self, question_lower: str) -> Optional[str]:
        """Detect the geographic scope of a market from its question text."""
        hits: dict[str, int] = {}
        for kw, continent in _MARKET_GEO_KEYWORDS.items():
            if kw in question_lower:
                hits[continent] = hits.get(continent, 0) + 1
        if not hits:
            return None
        # Return dominant continent
        return max(hits, key=lambda k: hits[k])

    def _semantic_scores(self, text: str) -> np.ndarray:
        """Return cosine similarities between `text` and all market questions."""
        if self._model is None or self._embeddings is None:
            # Fallback: zero scores (concept matching only)
            return np.zeros(len(self.markets))

        vec = self._model.encode(
            [text],
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )[0]
        # dot product of unit vectors = cosine similarity
        return self._embeddings @ vec

    # ── utilities ─────────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self.markets)


def _incompatible(a: str, b: str) -> bool:
    """Return True if continents a and b are clearly unrelated."""
    return frozenset({a, b}) in _INCOMPATIBLE_PAIRS

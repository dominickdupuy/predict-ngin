# Polymarket Strategy Ideas — Research Backlog

**Date:** 2026-04-18
**Scope:** Strategy concepts to research, prototype, and backtest. This document is
deliberately implementation-light: the goal is to enumerate testable hypotheses,
their economic edge, and how each one would be validated against the existing data
stack before writing any production code.

**Already shipped** (do not re-derive):
- Whale-following (`src/whale_strategy/`) — informed-flow following with Bayesian shrinkage + IC blend
- Latency arbitrage (NewsAPI.ai + NER + price-crossing) — `scripts/live/news_monitor.py`
- Calendar monotonicity arb (riskless date-series violations) — `src/trading/strategies/`
- Calendar cascade arb (predictive lead→follow on date series)
- Pairs trading (z-score mean reversion on correlated markets)
- Skeleton modules (untuned): momentum, breakout, mean reversion, sentiment, smart money, time decay, cross-market, NLP correlation, composite

What follows is a backlog of **new** strategies plus enhancement layers that wrap the
existing ones. Each idea is rated on:
- **Edge type:** structural / informational / behavioral / microstructure
- **Horizon:** intratrade duration the edge persists
- **Frequency:** expected trades/yr at full universe (7 categories + Sports/Tech)
- **Capacity:** rough $ per trade before slippage eats edge
- **Effort:** S(mall, ≤1 day prototype) / M(edium, ≤1 wk) / L(arge, multi-wk)

---

## Table of contents

1. Microstructure & order-flow
2. Cross-platform & cross-venue arbitrage
3. Structural / no-arb constraint strategies
4. Information-edge strategies
5. Behavioral / counter-flow strategies
6. Calendar, event, and time-decay strategies
7. ML / signal-combination layer
8. Portfolio & risk overlays
9. Testing methodology — shared discipline
10. Prioritization matrix

---

## 1. Microstructure & order-flow

### 1.1 Iceberg / hidden-size detection

**Thesis.** When a participant repeatedly refills the same level after partial fills,
price prints linger at that level with abnormal cumulative volume. Hidden interest
implies a strong-hand price floor (or ceiling), and the next break usually retraces.

**Signal.** For each market, sliding 30-min window: count repeated trade prints
within 0.5¢ of a single price level, weighted by the count of distinct counterparty
addresses on the opposite side. Score = `same_side_volume / opposing_addresses`.
High score → iceberg present.

**Trade rule.** When an iceberg is detected at price L on the BUY side, BUY YES on
any sweep that takes price below L − 1¢ (target: revert to L; stop: 2¢ below entry).

**Edge type:** microstructure. **Horizon:** minutes–hours. **Frequency:** medium-high
in liquid Politics/Geopolitics markets. **Capacity:** small per trade ($500–$5k).
**Effort:** M.

**How to test.**
- Reuse `data/research/{cat}/trades.parquet`. Build per-market level histograms
  bucketed at 0.5¢ resolution.
- Backtest with 5-min VWAP entry/exit (consistent with existing arb backtests).
- Sanity check against the pairs-trading baseline: iceberg signals should *predate*
  the pairs-trading entry by minutes — this is the "first-mover" version of the
  same edge.
- Headline metric: hit rate conditional on signal strength deciles. Edge should be
  monotone in decile.

**Risks.** Polymarket's CLOB does not have native iceberg orders, so the pattern
is purely behavioral (a person/bot manually refilling). Could degrade if the actor
quits.

---

### 1.2 Order-book imbalance momentum (when L1/L2 data lands)

**Thesis.** Standard equity-microstructure result: short-horizon return is
predictable from depth imbalance `(B − A)/(B + A)` at top-of-book. Polymarket has
genuine CLOB depth; this likely works.

**Signal.** WebSocket subscribe to top-5 depth on liquid markets. Imbalance > 0.6 →
long YES at next tick; < −0.6 → long NO.

**Trade rule.** Entry: market order on signal. Exit: imbalance reverts to |x| < 0.2,
or 60s timeout, whichever first.

**Edge type:** microstructure. **Horizon:** seconds–minutes. **Frequency:** very high
on the 50–100 most liquid markets at any time. **Capacity:** small. **Effort:** L
(needs realtime book recorder, not yet built).

**How to test.**
- Pre-req: build a CLOB snapshot recorder (`scripts/data/realtime_prices.py` exists
  but only stores trades, not book). Burn 2 weeks of L2 snapshots at 1-Hz on the
  top-200 markets (~5GB).
- Train decision threshold on first half, test on second.
- Compare vs naive baseline (always-long YES). Sharpe must beat 1.5 net of 30bp
  round-trip cost.

**Risks.** Whale `MM` bots already do this — edge may be tiny / negative net of
spread crossing. Worth measuring the magnitude before committing.

---

### 1.3 Trade-burst aftermath (post-large-print drift)

**Thesis.** A single trade > $50k produces a price shock; the market either
mean-reverts (uninformed flow) or continues (informed flow). The disambiguator is
whether the print is from a known whale (use the existing whale set).

**Signal.** Detect a print with `size_usd > 95th-percentile market size`. Look up
the proxy wallet in the live whale registry.
- **Whale source** → momentum continuation: same-side entry within 5 minutes.
- **Non-whale** → mean reversion: opposite-side entry, target = pre-shock VWAP.

**Trade rule.** 5-min entry delay (let the initial impact wash out), exit at
30-min VWAP convergence to either the entry or the target.

**Edge type:** microstructure × informational. **Horizon:** 30 min–4 h.
**Frequency:** ~2–10/day across the universe. **Capacity:** medium ($1k–$10k).
**Effort:** S — leverages existing whale infra.

**How to test.**
- Already have `watch_whale_trades.py` running with a 2,157-whale registry. Replay
  the JSONL alert log against historical price paths; measure 30/60/120-min
  forward returns by whale tier and by trade size decile.
- Two-by-two table: `{whale, non-whale} × {continuation, reversion}`. Stat-sig if
  the diagonal beats the off-diagonal by >2σ.

**Risks.** This is a refinement of whale-following, not an independent strategy.
Validate that it adds incremental Sharpe over the baseline whale strategy, not just
re-discovers it.

---

### 1.4 Liquidity-grab fade

**Thesis.** Stop-loss clusters above prior highs (or below prior lows) get hit by
single-shot sweeps that immediately revert. Same setup as crypto/FX "liquidity grabs".

**Signal.** Identify horizontal price levels where >40% of historical reversals have
occurred (within 1¢). When price prints through that level on a single trade > 5×
median trade size and reverses within 2 minutes, enter the reversal.

**Trade rule.** Entry: 2-min after the sweep, in the reversal direction. Stop: 1.5¢
beyond the sweep extreme. Target: prior 1-h VWAP.

**Edge type:** microstructure. **Horizon:** 5–60 min. **Frequency:** low-medium.
**Effort:** M.

**How to test.**
- Build a "reversal-level" detector on the trades parquet. For each market, find
  prices where the path crossed and reversed within N minutes ≥ K times in the
  prior 30 days.
- Walk-forward: training window builds the level set; test window measures
  sweep-and-revert outcomes.

**Risks.** Polymarket prices are bounded [0, 1]. Near 0/1 the "level" concept is
degenerate (no room to revert). Restrict to mid-range markets.

---

## 2. Cross-platform & cross-venue arbitrage

### 2.1 Polymarket ↔ Kalshi spread arb

**Thesis.** Kalshi (US-regulated) and Polymarket (offshore crypto) often list the
same event with different prices because of distinct user bases (institutional
vs. retail crypto), different fee structures, and KYC frictions. The persistent
gap *can be earned* if you hold both legs to expiry.

**Signal.** Already partially built: `src/trading/arbitrage/cross_platform_*.py` and
`scripts/data/fetch_kalshi_data.py`. Need a market-matcher (`semantic_matcher.py`
exists for news → market; extend to market → market).

**Trade rule.** When `|p_poly − p_kalshi| > total_friction` (KYC cost amortized,
withdraw fees, time value of capital):
- Long the cheap side, short the expensive side, hold to settlement.
- If only one venue allows shorting that direction, hedge with the complementary
  market on the other.

**Edge type:** structural. **Horizon:** days–months. **Frequency:** dozens of pairs
at any time. **Capacity:** large ($10k–$100k+ per trade since holding to expiry).
**Effort:** M.

**How to test.**
- Pull Kalshi historical data (already have `download_kalshi_hf.py`, `fetch_kalshi_data.py`).
- Match markets by (a) entity overlap, (b) end-date alignment, (c) resolution-source
  language similarity. Manually QA the top 50 matches.
- Backtest: at every UTC day boundary, compute spread on all matched pairs; trigger
  when spread > friction; mark to settlement.
- Headline metric: trades/yr after friction; Sharpe is misleading because trades
  are highly autocorrelated (hold to expiry).

**Risks.** Capital lockup is the binding constraint, not edge. KYC issues moving
between venues. Polymarket access from US is restricted (geofencing risk for some
operators).

---

### 2.2 Polymarket internal "wrong-side" arb (NO + YES > $1)

**Thesis.** On thin markets, top-of-book quotes can drift such that
`best_ask_YES + best_ask_NO > 1.00 + fees`. Buying both sides locks in the
difference at resolution.

**Signal.** Realtime CLOB scan (or 1-min snapshots) for `ask_yes + ask_no − 1 − fees > 0`.

**Trade rule.** Hit both books simultaneously for matched size. Hold to resolution
(0 + 1 = $1 always).

**Edge type:** structural / mechanical. **Horizon:** until resolution.
**Frequency:** rare in liquid markets, common in long-tail. **Capacity:** small per
trade. **Effort:** S — purely a scanner + executor.

**How to test.**
- Need book snapshots (see §1.2 prereq). Until then, infer from trade prints:
  whenever a BUY YES at price A and a BUY NO at price B execute within 60s and
  A + B > 1.02, count as a candidate. This is a lower bound on the true frequency.
- Capacity test: for each detected event, simulate hitting both sides at the
  next-trade prices. Slippage on the second leg is the killer.

**Risks.** First-leg-fills-second-doesn't is the central execution risk. Mitigation:
post-only on both sides, accept partial fills.

---

### 2.3 Polymarket multi-outcome simplex arb

**Thesis.** Polymarket lists multi-outcome events (e.g. "next NYC mayor: Adams /
Mamdani / Cuomo / Sliwa / other") as a *set of binary YES markets*. Constraint:
`Σ p_i ≤ 1` (with strict equality if outcomes are exhaustive). Violations are
provable arbs.

**Signal.** Group binary markets sharing the same `eventId` (in `markets_filtered.csv`).
For each event, sum the YES bid prices. If `Σ ask_YES < 1 − fees` → buy all YES;
guaranteed +Σ. If `Σ bid_YES > 1 + fees` → sell all YES; same.

**Trade rule.** Mass entry; hold to resolution.

**Edge type:** structural (extension of monotonicity arb). **Horizon:** to resolution.
**Frequency:** medium — expected to fire whenever a long-tail "other" candidate is
mispriced. **Capacity:** medium. **Effort:** M.

**How to test.**
- Group by `eventId`. Build per-event price-sum series at 5-min VWAP frequency.
- Measure violation magnitude distribution; condition on number of outcomes (more
  outcomes → wider bid-ask sums, less arb).
- Compare to the existing monotonicity arb: that strategy uses the date-stochastic
  dominance constraint, which is one specific instance of this more general
  no-arb relation.

**Risks.** Polymarket's `negRisk` markets handle some of this internally and may
auto-rebalance. Filter to non-negRisk events first.

---

### 2.4 Kalshi → Polymarket information leakage

**Thesis.** Kalshi has CFTC-regulated bona-fide hedger flow on macroeconomic
events (CPI, NFP, FOMC). When Kalshi prints a sharp move, Polymarket lags by
seconds because the user bases barely overlap.

**Signal.** Subscribe to Kalshi top-of-book for all event-pair markets. On Kalshi
move > 5%, immediate same-direction entry on Polymarket sister market.

**Trade rule.** Entry: market order on Polymarket within 5s. Exit: Polymarket
catches up to Kalshi level (target = `p_kalshi − 0.5%` for safety), or 30-min
timeout.

**Edge type:** informational. **Horizon:** seconds–minutes. **Frequency:** event-driven
(2–10 per macro release). **Capacity:** medium. **Effort:** L (needs Kalshi
realtime feed + low-latency Polymarket execution).

**How to test.**
- Use historical Kalshi tick data + Polymarket trade ticks. Align timestamps to
  UTC ms.
- For each identified pair, measure cross-correlation function `ρ(τ)` of returns.
  Lead time = τ at peak ρ. If positive lead with significant peak, edge exists.
- Same approach as systematic_lag_analysis.py but cross-venue, not cross-market.

**Risks.** Kalshi's API may have rate limits or licensing restrictions on
high-frequency redistribution. Latency budget tight (<5s).

---

## 3. Structural / no-arb constraint strategies

### 3.1 Conditional probability constraint arb

**Thesis.** When markets exist for both `P(A)` and `P(A and B)`, then
`P(A and B) ≤ P(A)`. Same logic as date monotonicity. Polymarket has many of these
(e.g. "Trump wins AND signs EO", "Fed cuts AND inflation < 3%").

**Signal.** Hand-curate (or LLM-extract) conditional pairs from market questions.
Detect `P(A and B) > P(A)` violations. Trade: buy NO on the conditional, buy YES
on the marginal.

**Trade rule.** Same as monotonicity arb (riskless if held to resolution and both
markets settle correctly).

**Edge type:** structural. **Horizon:** to resolution. **Frequency:** low (50–200
identifiable pairs at any time). **Effort:** M (the matcher is the work).

**How to test.**
- LLM-pass over `markets_filtered.csv` to extract `(condition, event)` tuples.
  Validate top 100 manually.
- Walk through historical price series for matched pairs; count violations and
  mark to resolution PnL.

**Risks.** Resolution interpretation risk — UMA might resolve "AND" markets in
ways that violate the strict logical conjunction.

---

### 3.2 Bayes-update arb on news-conditioned markets

**Thesis.** When new information arrives that should update P(A) and P(A|news)
markets simultaneously, the conditional market often updates faster (more attentive
traders) and the unconditional lags. The lag is a tradeable Bayes-update error.

**Signal.** Detect rapid move in conditional market `P(A|B)` when B is realized.
The unconditional `P(A)` should update according to Bayes' rule:
`P(A)_new = P(A|B) × P(B)_new + P(A|¬B) × (1 − P(B)_new)`.
If `P(A)_market` lags this implied value by > 3¢, trade the gap.

**Trade rule.** Long the lagging side; exit when the gap closes within 1¢.

**Edge type:** informational / model-driven. **Horizon:** minutes–hours.
**Effort:** L.

**How to test.**
- Identify candidate triplets: `{A, B, A|B}`. NYC mayoral primary set is a good
  test corpus (lots of conditional language).
- For each triplet, simulate the Bayes-implied price series; measure the residual
  vs the market price. Half-life of the residual tells you the holding period.

**Risks.** Requires identifying `P(A|¬B)` which is rarely explicitly listed —
have to assume a value (e.g. 50%) or estimate from history. Sensitive to that
assumption.

---

## 4. Information-edge strategies

### 4.1 Polymarket-native social-sentiment momentum

**Thesis.** Polymarket trader Discord/Twitter chatter precedes price moves on
narrative-driven markets (especially Politics). The current latency-arb pipeline
listens to news; this listens to *trader chatter*.

**Signal.** Scrape (Twitter API, Discord bridge) mentions of market slugs or key
entities. Tokenize, compute sentiment via finBERT or similar. When sentiment
z-score on a market crosses 2σ within a 1-h window, signal direction.

**Trade rule.** Same direction as sentiment polarity, exit on z-score reversion to
< 0.5σ or 6h timeout.

**Edge type:** informational / behavioral. **Horizon:** hours. **Frequency:** medium.
**Effort:** L (data collection is the bulk).

**How to test.**
- Collect 30 days of mentions linked to top-200 markets by volume.
- Lead-lag correlation (sentiment_t vs return_{t+h}) for h ∈ {15min, 1h, 4h, 24h}.
- Out-of-sample: hold out the last 7 days; measure Sharpe of a simple long-short
  rule.

**Risks.** Astroturfing / coordinated pumping by participants. Filter to mentions
from accounts with >180 day history and >1k followers.

---

### 4.2 On-chain whale-deposit precursor

**Thesis.** Polymarket is on Polygon. Large USDC deposits to known Polymarket
wallets precede large bets by minutes-hours. Detecting deposits to whale-tier
wallets gives a forward signal on which markets are about to see whale flow.

**Signal.** Subscribe to Polygon mempool / new-block events. Filter for transfers
to addresses in the existing whale registry, value > $50k. Look up the wallet's
history: which markets do they trade most?

**Trade rule.** When a TIER-1 whale receives a $X deposit, lightly pre-position
in the markets they trade most often (Bayesian prior on their "favorite" market
set). Cap position at 1% of capital per signal — these are speculative pre-bets.

**Edge type:** informational. **Horizon:** minutes–hours. **Frequency:** 1–10/day
across the registry. **Effort:** M (Polygon RPC + the_graph queries).

**How to test.**
- Use historical Polygon transfers (from the_graph subgraph or Covalent). For each
  large deposit to a whale wallet, find the next trade by that wallet. Measure
  the time gap and the predictability of the destination market.
- A whale that deposits and then doesn't trade is noise; one that deposits and
  bets within 2h is the signal.

**Risks.** Front-running ethics — pre-positioning ahead of an identified actor's
known trade pattern. Acceptable if not interfering with their fill (small enough
size). Consider as a *priority alert* feeding the main whale-following strategy
rather than a standalone strategy.

---

### 4.3 Resolution-source monitoring

**Thesis.** Polymarket markets resolve based on identifiable sources (e.g.
`vote.pa.gov`, `nytimes.com`, "official UFC scoring"). Monitoring those sources
directly — in many cases their data feeds publish before the news cycle picks it
up — gives a sub-news-cycle edge.

**Signal.** Per market, parse `resolutionSource` URL/text from `markets_filtered.csv`.
Group markets by resolution-source domain. For each domain, set up a polling job
on the source's most authoritative endpoint (e.g. official vote totals, official
schedule changes, etc).

**Trade rule.** When the source publishes the resolving event, immediate same-side
position. This is *the* arrival-time edge — preceding even the news-pipeline
because the news scrapes the source you're already watching.

**Edge type:** informational / structural. **Horizon:** seconds (until news catches
up). **Frequency:** event-driven. **Capacity:** large. **Effort:** L per source,
but high-leverage.

**How to test.**
- Cluster markets by `resolutionSource` domain — find the top 20 domains by
  number-of-markets-resolved. These are the highest-leverage targets.
- Backtest: assume oracle detection at the moment the source updates; compute
  the edge over the existing latency-arb baseline. The delta = source-monitoring
  alpha.

**Risks.** Per-source engineering effort is real. Some sources (UMA disputes,
manual UMA settlements) have no programmatic precursor.

---

### 4.4 Polymarket comments / order-book chat as alpha

**Thesis.** Polymarket markets have a comments thread. Concentrated burst of new
comments (especially with informed-sounding language) precedes price moves.

**Signal.** Scrape comment counts per market over time. Detect 90th-percentile
comment-rate spikes within 1h windows. Cross-reference with existing whale
registry — if whales commented, weight higher.

**Trade rule.** Direction inferred from comment sentiment (LLM classification,
`bullish_yes / bullish_no / neutral`). Position size scales with z-score.

**Edge type:** behavioral / informational. **Horizon:** 1–24 h. **Effort:** M.

**How to test.**
- Need historical comment-thread snapshots. Polymarket has no public API for
  this — would have to scrape live and burn 30 days before backtesting.
- Tier-1 alternative: use the existing news pipeline's NLP score as a comment
  proxy. Weak proxy, but available immediately.

**Risks.** Comment data is sparse for long-tail markets. May only work on top-50
markets by attention.

---

## 5. Behavioral / counter-flow strategies

### 5.1 Loser fade (anti-whale, opposite of whale-following)

**Thesis.** Whales are 87% accurate (per the existing strategy). The complement:
identify *systematically wrong* traders ("dumb money") and fade them. The 5th
percentile of trader WR after Bayesian shrinkage are reliable counter-indicators.

**Signal.** Same machinery as whale identification, but invert the score. Qualifying
losers have `shrunk_WR < 0.40` (i.e. ≥10pp below random) and `surprise_WR < −0.05`.
When such a trader executes, fade them.

**Trade rule.** Opposite side, same size sizing logic as whale-following.

**Edge type:** informational / behavioral. **Horizon:** to resolution. **Effort:** S.

**How to test.**
- Reuse `whale_surprise.py` with inverted thresholds. Same backtest harness.
- Important sanity check: are losers losers because they bet on long-shot
  outcomes (negative skew in expected payouts) or because they're genuinely
  wrong directionally? Only the latter is fadeable; the former just has fat tails.

**Risks.** Survivorship bias: bad traders quit, so the loser cohort turns over
faster than the whale cohort. Recency-decay weight should be much shorter (30d).

---

### 5.2 Crowd-counter (extreme retail flow fade)

**Thesis.** When > 80% of distinct addresses are on one side of a market within a
short window, the market is at a sentiment extreme that historically reverts
(classic "everybody bullish = top" effect).

**Signal.** Per market, sliding 1-h window: count distinct addresses by side.
When `unique_buyers / (unique_buyers + unique_sellers) > 0.80` and trade count
> 50, signal contrarian.

**Trade rule.** Fade direction. Stop at 5¢ adverse. Target: 1-h prior VWAP.

**Edge type:** behavioral. **Horizon:** hours. **Effort:** S.

**How to test.**
- Compute on `trades.parquet`. Bucket signals by extremity (>80%, >85%, >90%) and
  measure 1h/4h/24h forward returns.
- The signal must survive controlling for momentum: extreme buying often
  *continues* in trending markets.

**Risks.** During genuine resolutions (e.g. election called), 100% one-side is
correct, not contrarian. Filter out periods within 24h of `endDateIso`.

---

### 5.3 New-account fade

**Thesis.** Accounts created in the last 30 days that immediately make large bets
are disproportionately wrong (FOMO retail). Fade their flow.

**Signal.** Per trade, look up wallet creation date (first-trade timestamp in our
data is a proxy). If `account_age_days < 30` and `trade_size > 90th-percentile`,
flag.

**Trade rule.** Opposite side at next-trade VWAP, exit at resolution or 7d stop.

**Edge type:** behavioral. **Horizon:** days. **Effort:** S.

**How to test.**
- Compute first-trade timestamp per `proxyWallet` in trades.parquet.
- Backtest: for the universe of "new-account large trades", compute hit rate of
  the *opposite* position by resolution. Compare to baseline (random opposite).

**Risks.** New whales also exist (a sophisticated trader who just opened a wallet).
Cross-reference with deposit size: a new wallet seeded with > $1M is more likely
informed than not.

---

## 6. Calendar, event, and time-decay strategies

### 6.1 Theta harvesting on near-resolution markets

**Thesis.** Markets within 24h of `endDateIso` exhibit accelerated price-to-truth
convergence. Selling NO on near-certain YES (or vice versa) at >0.95 captures the
theta-like decay to 1.0 with bounded downside.

**Signal.** Filter `markets_filtered.csv` for markets with `endDateIso` within
the next 12h and YES price ∈ [0.95, 0.99]. Add liquidity gate (volumeClob > $50k).

**Trade rule.** SELL YES (= BUY NO) at the offer; hold to resolution. Position
size proportional to `1 - p`.

**Edge type:** structural / time-decay. **Horizon:** ≤24h. **Frequency:** medium.
**Capacity:** small per trade (capped by `1 - p`). **Effort:** S.

**How to test.**
- Dead-simple. For every resolved market, find the price 24h before close and
  the resolution. Compute the strategy PnL across all qualifying entries.
- This is essentially the last-mile of the latency-arb strategy. Compare to the
  latency-arb baseline (which already targets this zone via news).

**Risks.** Tail outcomes: a market at 0.98 that resolves NO loses 50× the average
trade. Need explicit position cap (e.g. max payout per trade = $5k).

---

### 6.2 Pre-event volatility expansion

**Thesis.** Volatility on event-driven markets (FOMC, CPI, election night)
*expands* in the 6h before resolution as positions get squared. Straddle-equivalent
trades benefit.

**Signal.** Hard-coded calendar of high-impact events. Pre-event window: enter
*both* YES and NO at near-mid (e.g. each ≤ 0.55) on the relevant market. Profit
if either resolves at 1.0 (combined cost < $1.10 → guaranteed positive PnL).

**Trade rule.** Net cost ≤ $0.95 to enter (10% margin). Hold to resolution.

**Edge type:** structural / volatility. **Horizon:** to resolution. **Frequency:**
~1–4 events/month. **Effort:** S.

**How to test.**
- Assemble a calendar of FOMC dates, CPI dates, NFP, election dates. For each,
  scan the corresponding Polymarket market for the entry window where
  `ask_YES + ask_NO ≤ 0.95`. This is a strict subset of §2.2 (wrong-side arb)
  applied near events.
- Likely rare in liquid markets — measure to confirm.

**Risks.** Often the no-arb relation already binds tightly near events. The window
may be theoretical only.

---

### 6.3 Holiday / weekend microstructure

**Thesis.** Trading volume on Polymarket drops 60%+ on US holidays and Sunday
overnights. Spreads widen, mean-reversion alpha increases.

**Signal.** Time-of-week × time-of-day cell. Within each cell, recompute
mean-reversion strategy parameters (z-thresholds, half-life). Off-hours cells
get tighter z thresholds and shorter holding periods.

**Trade rule.** Existing pairs trading with cell-conditional parameters.

**Edge type:** microstructure × calendar. **Effort:** S — parameter overlay on
existing pairs trading.

**How to test.**
- Bucket existing pairs trades by hour-of-week. Measure Sharpe per bucket.
- Off-hours buckets should show monotonically higher Sharpe if the thesis holds.

**Risks.** Off-hours volumes are also small enough that capacity is the binding
constraint, not edge.

---

## 7. ML / signal-combination layer

### 7.1 Meta-model signal stacker

**Thesis.** Each individual strategy produces a signal stream with characteristic
hit rate, payoff distribution, and conditional alpha. A meta-model that learns
which signals to act on under which conditions outperforms any single strategy.

**Signal.** Each implemented strategy emits a `(market_id, side, conviction,
features_snapshot)` row to a unified signal log. Train a gradient-boosted model
to predict realized 24h forward return from features.

**Trade rule.** Trade only signals where the meta-model's predicted return
exceeds the cost threshold. Position-size by predicted return × confidence.

**Edge type:** combination. **Effort:** L (depends on ≥5 deployed signal
streams).

**How to test.**
- Pre-req: deploy 3–5 of the strategies above and accumulate 6+ months of
  signal history.
- Walk-forward train (6mo) / test (1mo) splits, no overlap. Compare meta-model
  Sharpe to best single strategy.
- Diagnostic: feature importance should show that the model is *adding* value
  beyond signal-of-strategy alone.

**Risks.** Overfitting on small signal-counts. Strict L2 regularization or shallow
trees mandatory.

---

### 7.2 Embedding-based market clustering for cross-market alpha

**Thesis.** Pairs trading currently relies on price correlation to identify
related markets. Semantic embeddings (already used in semantic_matcher) could
identify related-but-uncorrelated markets where information *should* link them
but pricing has not yet caught up — a richer signal universe.

**Signal.** Encode all market questions with sentence-transformer. Cluster.
Within a cluster, monitor for cases where one market moves but its
high-similarity neighbors do not.

**Trade rule.** When market A in cluster C moves > 5% and the cluster-mean price
has not moved > 1%, lightly position the other cluster members in the same
direction.

**Edge type:** informational / behavioral lag. **Horizon:** hours. **Effort:** M.

**How to test.**
- Reuse `compute_market_similarities.py`. Construct semantic clusters
  (similarity > 0.7).
- Backtest: when an in-cluster move happens, measure the average follow-through
  on neighbors over 1h, 6h, 24h. Strategy works if mean follow-through is positive
  net of costs.

**Risks.** Semantic similarity is not always a tradeable link. Filter clusters
by historical price correlation > 0.4 to weed out "lexically similar but
unrelated" markets.

---

### 7.3 Transformer-based price-path forecasting

**Thesis.** Per-market price paths are short, noisy, and bounded — but a
transformer trained across all markets could learn shared shape priors
(s-curve to resolution, bimodality near events, etc.) and forecast next-1h
direction better than the marginal price.

**Signal.** Tokenize price + volume + time-to-close + category as a sequence;
train a small transformer (~10M params) to predict `sign(return_{t+1h})`.

**Trade rule.** Trade when model probability > 0.6 (calibrated on holdout) and
position size by `|p − 0.5| × kelly`.

**Edge type:** informational / pattern-recognition. **Effort:** L.

**How to test.**
- Train on Politics, Geopolitics, Economy, Finance, Climate (5 cats). Test on
  Art_and_Culture and Other (held-out categories).
- Cross-category generalization is the proper test — within-category training is
  too easy to overfit.
- Headline metric: AUC > 0.55 on holdout categories.

**Risks.** Returns on Polymarket are dominated by news arrivals (exogenous, not
inferable from price path). Ceiling on what the model can learn from prices alone
may be low.

---

## 8. Portfolio & risk overlays

### 8.1 Kelly-cap with strategy-correlation matrix

**Thesis.** Multiple deployed strategies have correlated PnL during regime
shifts (all stat-arb suffers in news shocks; all whale-following suffers when
the registry is stale). A portfolio-level Kelly cap that accounts for cross-
strategy correlation prevents over-allocation.

**Implementation.** Maintain rolling 30-day PnL series per strategy. Compute
correlation matrix daily. Total deployed capital ≤ portfolio-Kelly cap derived
from the eigenvector decomposition of that matrix.

**How to test.** No backtest needed in isolation — apply to existing combined
backtest, measure drawdown reduction with constant Sharpe (the goal is risk-adj
improvement).

**Effort:** S. **Edge type:** risk overlay.

---

### 8.2 Regime detection switching

**Thesis.** Strategy edge varies by market regime: latency-arb works in
high-news periods, mean-reversion works in calm periods, calendar arb works
during active geopolitical events. A simple HMM or rule-based regime classifier
can scale strategy weights up/down by regime probability.

**Signal.** Daily features: news-volume z-score, average market price-volatility,
realized correlation across categories. Cluster into 3 regimes; estimate
per-regime per-strategy Sharpe.

**Trade rule.** Each strategy's deployed capital = `base_allocation × regime_score_for_that_strategy`.

**How to test.** Use existing `src/research/regime_detection.py`. Apply to historical
strategy PnL series. Measure ex-ante regime-conditional Sharpe.

**Effort:** M. **Edge type:** allocation overlay.

---

### 8.3 Concentration limits cross-strategy

**Thesis.** Multiple strategies might independently arrive at the same trade
(e.g. whale-following and pairs both signal the same market). Naive aggregation
double-counts position. A unified position book with cross-strategy de-dup is
needed before scaling strategies.

**Implementation.** Single position-keeper, strategies submit "intent" not
"orders". Position-keeper aggregates intents, sizes by net-new exposure not
gross.

**How to test.** Replay historical signal logs with vs. without the dedup layer.
Measure capital efficiency (returns per unit deployed capital) — should improve
if dedup catches overlap.

**Effort:** M.

---

## 9. Testing methodology — shared discipline

The following must apply uniformly to every strategy in this document. Repeat as
needed, this is the part that's easy to skip and ruinous to skip.

### 9.1 Look-ahead audit (mandatory)

For every signal: every input feature must be timestamped with `available_at`.
The backtest must reject any feature where `available_at > decision_time`.
Standard violations to test for:
- Resolution data leaking into whale-scoring before the resolution publish time
  (already fixed in whale strategy via `closedTime ≤ cutoff`).
- Market `closedTime` used in TTR filters (look-ahead — use `endDateIso` only).
- Volume statistics (`volumeClob`, etc.) reflect end-of-life values, not point-in-time.
- IC computation horizon overlapping with test period.

Run a "shuffle test": replay the strategy with feature timestamps shuffled by ±1
day. Sharpe should collapse if any look-ahead exists.

### 9.2 Walk-forward validation (mandatory for all)

Default protocol: 12-month training window, 3-month test window, 1-month step.
Refuse to ship a strategy where < 70% of folds are profitable.

### 9.3 Cost model

Every strategy backtest must apply at minimum:
- Taker fee: 0.2% per leg (Polymarket published)
- Slippage: linear in trade size as fraction of market liquidity
  (1 bp per 1% of `volumeClob` consumed, validated by `scripts/robustness/slippage_sweep.py`)
- Spread crossing: 1.0¢ for liquid markets (>$100k volume), 2.5¢ for illiquid

If a strategy has positive Sharpe at 0% cost and negative at the model above,
report this as a failed strategy — do not ship.

### 9.4 Multiple-hypothesis correction

Strategies derived from parameter sweeps must report deflated Sharpe by the
number of parameters/configurations tried (Bonferroni or PBO-style as in
López de Prado). The `param_sensitivity_holdout.py` framework already exists;
extend rather than re-implement.

### 9.5 Capacity test

For every signal, model the slippage of executing at *N times* the historical
trade size (N ∈ {1, 5, 25}). Strategies whose Sharpe collapses at N=5 are
toy strategies — useful for learning, not for capital.

### 9.6 Live paper-trading shadow period

Before any capital, every strategy runs in paper mode for ≥30 days, with daily
PnL compared to the same-day backtest projection. A divergence > 1σ daily
indicates a bug between research and live (data feed mismatch, signal definition
drift, etc.).

### 9.7 Headline metric set per strategy

Standardize on this comparable set:
- Trades / yr
- Hit rate (with 95% CI from binomial)
- Mean ROI / median ROI
- Sharpe (annualized from daily PnL)
- Sortino
- Max drawdown ($ and %)
- Time-in-market %
- Capacity (max trade size before slippage > 50% of edge)
- Ratio: `live_sharpe / backtest_sharpe` after 90 days live

### 9.8 Exclusions and reporting

Document explicitly: which markets / categories / time windows the strategy
*excludes* and why. Implicit filters are the most common source of look-ahead
in practice.

---

## 10. Prioritization matrix

Ranked by expected `(edge × capacity × low effort)` and complementarity with
existing book:

| # | Strategy | Edge | Capacity | Effort | Notes |
|---|---|---|---|---|---|
| 1 | §3.1 Conditional probability arb | Structural (riskless) | Medium | M | Direct extension of monotonicity arb. Highest cleanest edge. |
| 2 | §6.1 Theta harvesting | Structural | Medium | S | Trivial to implement. Likely high-Sharpe complement to latency arb. |
| 3 | §2.3 Multi-outcome simplex arb | Structural | Medium | M | Generalizes monotonicity. Worth doing right. |
| 4 | §1.3 Trade-burst aftermath | Microstructure × info | Medium | S | Reuses entire whale infra. Quick win. |
| 5 | §5.1 Loser fade | Behavioral | Medium | S | Mirror of whale-following with same machinery. |
| 6 | §2.1 Polymarket↔Kalshi spread | Structural | Large | M | Capital-locked but very clean. |
| 7 | §4.3 Resolution-source monitoring | Informational | Large | L per source | Highest *latent* edge but per-source eng cost. |
| 8 | §8.1+8.2 Portfolio overlays | Risk-adj uplift | n/a | S+M | Required before scaling capital across strategies. |
| 9 | §1.1 Iceberg detection | Microstructure | Small | M | Worth investigating, may not survive cost. |
| 10 | §7.1 Meta-model stacker | Combination | n/a | L | Wait until 5+ live signal streams exist. |
| 11 | §1.2 Order-book imbalance | Microstructure | Small | L | Needs new data infra; defer until book recorder built. |
| 12 | §7.3 Transformer price-path | ML | Medium | L | Speculative; ceiling on price-only signal is unclear. |

---

## Appendix A — Data prerequisites by strategy

| Need | Strategies | Status |
|---|---|---|
| Trades parquet (existing) | 1.1, 1.3, 1.4, 5.1, 5.2, 5.3, 6.1, 6.3 | ✓ |
| Markets metadata CSV | 3.1, 6.1, 6.2 | ✓ |
| Resolutions CSV | All strategies that mark to resolution | ✓ |
| News API (NewsAPI.ai) | 4.x | ✓ (live pipeline) |
| Kalshi historical | 2.1, 2.4 | partial — `download_kalshi_hf.py` exists |
| Realtime CLOB book L2 | 1.2, 2.2 | ✗ — needs new recorder |
| Polygon on-chain | 4.2 | ✗ — needs the_graph subgraph or RPC |
| Twitter / Discord scrape | 4.1 | ✗ — needs collection |
| Polymarket comments | 4.4 | ✗ — needs scrape (no API) |
| Resolution-source feeds | 4.3 | ✗ — per-source engineering |

---

## Appendix B — Suggested next concrete actions

1. Write the §3.1 conditional-probability matcher prototype (LLM extraction over
   `markets_filtered.csv`, manual QA top-100). Backtest against `trades.parquet`.
2. Build the §6.1 theta-harvest backtest — single afternoon's work, produces a
   clear yes/no on whether the edge survives costs.
3. Pull 30 days of Kalshi tick data via `fetch_kalshi_data.py`, run a simple
   §2.1 spread scan, document the spread distribution.
4. Stand up the cross-strategy position keeper (§8.3) before deploying any new
   strategy alongside whale-following — otherwise double-counted exposure will
   cause losses.

End of document.

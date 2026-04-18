# Polymarket Strategy Ideas V2 — ML-Forward, High Signal-to-Noise

**Date:** 2026-04-18
**Scope:** New strategy concepts that complement `STRATEGY_IDEAS.md`. Focus on
**ML-native**, **causally grounded**, and **structurally constrained** edges that
maximize signal-to-noise. Every idea below is *new* — it does not duplicate the
microstructure/arb/whale/crowd strategies in V1.

**Guiding principles:**
- **High S/N**: prefer structural / model-based edges over noisy heuristics.
  Hit rate × conviction > trade count.
- **ML-native**: ML is load-bearing, not cosmetic. Each idea justifies why
  a model is better than a rule.
- **Identifiable**: every edge has a causal / statistical story that explains
  *why* it would persist.
- **Falsifiable**: each hypothesis has a pre-specified kill-criterion.

Rating key (same as V1):
- **Edge type** · **Horizon** · **Frequency** · **Capacity** · **Effort** (S/M/L)

---

## Table of contents

1. Causal inference & treatment-effect strategies
2. Graph neural networks on the trader–market bipartite
3. Self-supervised representation learning on price paths
4. Probabilistic & structural time-series models
5. LLM-based multi-step resolution reasoning
6. Reinforcement learning for execution & market making
7. Jumps, extremes, and point-process clustering
8. Cross-domain transfer learning
9. Adversarial robustness & toxic-flow detection
10. Mechanism-design & game-theoretic edges
11. Prioritization matrix

---

## 1. Causal inference & treatment-effect strategies

### 1.1 Synthetic-control cross-market alpha

**Thesis.** For any "treated" market that receives a shock (news, whale print),
construct a **synthetic control** from the convex combination of non-treated
markets with similar pre-shock price paths. The residual `price_treated − price_synthetic`
is the causal impact of the shock, purged of common-factor drift. The residual's
half-life defines a tradable window.

**Why ML.** Classical synthetic controls (Abadie–Gardeazabal) are a convex
optimization; modern extensions use **matrix completion** (Athey et al. 2021)
which handle the prediction-market panel structure (many markets, short histories,
missing data) natively.

**Signal.** For each market A at time t, fit weights w over peer set such that
`||price_A[t-H:t] − Σ w_i × price_i[t-H:t]||` is minimized on the pre-treatment
window. Treatment = recent news / whale print / category move. Signal strength
= residual z-score.

**Trade rule.** Long direction of residual when |z| > 2, target revert to 0,
stop at |z| > 4. Half-life τ estimated from a fitted OU process on historical
residuals.

**Edge type:** causal / informational. **Horizon:** hours–days.
**Frequency:** high (every liquid market, every shock event). **Capacity:** medium.
**Effort:** M.

**How to test.**
- Build donor pools from same-category, same-time-to-resolution, non-overlapping
  events.
- **Placebo test** (critical): randomly pick "treatment" times when no shock
  occurred; residual z-scores should not exhibit |z| > 2 excess. If they do,
  the donor pool is misspecified.
- Headline metric: Sharpe of trading |z| > 2 signals, stratified by shock type.

**Risks.** Donor-pool leakage: if news affects all markets in the pool, there's
no untreated control. Mitigate by requiring pool members to have no news
mentions in the pre-window.

---

### 1.2 Causal forests for heterogeneous strategy edge

**Thesis.** The existing whale-following strategy has 69% overall hit rate but
that's averaged over an enormous universe. A **causal forest** (Wager–Athey 2018)
estimates conditional average treatment effects — which market/whale
characteristics amplify or erase edge. Deploy only on the high-CATE subpopulation.

**Why ML.** Linear interaction terms explode combinatorially. Causal forests
find the high-signal subspace without pre-specifying which interactions matter.

**Signal.** Define treatment = "follow whale W into market M at time t". Outcome
= 24h forward return. Features: whale tier, market category, time-to-resolution,
price regime, volume percentile, whale recency, etc. Train `econml`'s
`CausalForest` on historical whale trades + non-trades.

**Trade rule.** Only execute whale-follow trades in the top 20% CATE bucket.
Position size proportional to predicted CATE.

**Edge type:** meta / allocation overlay. **Horizon:** same as base strategy.
**Frequency:** ~20–30% of base strategy volume (the high-CATE slice).
**Effort:** M.

**How to test.**
- Required: counterfactual labels. For every "positive" trade (we did follow),
  record the outcome. For every "negative" (we didn't), synthesize with nearest-neighbor
  matching as a proxy.
- Walk-forward: train CATE on 12 months, evaluate top-quintile-only Sharpe on
  next 3 months.
- Kill criterion: if top-quintile Sharpe ≤ 1.2 × overall Sharpe, edge is not
  heterogeneous enough to warrant the filter.

**Risks.** The forest may pick up spurious interactions from low-n subgroups.
Enforce min leaf size ≥ 100 trades.

---

### 1.3 Regression discontinuity on resolution-boundary markets

**Thesis.** Some markets resolve on a continuous threshold (e.g. "CPI > 3.2%").
As the underlying approaches the threshold, the market price exhibits an
**RD-like** discontinuity: just-below and just-above data generating processes
differ. The approach path around the threshold is systematically biased because
traders anchor to the round number.

**Why ML.** Local-linear RD estimation (Calonico–Cattaneo–Titiunik 2014) with
ML-selected bandwidth gives a principled estimate of the bias, rather than
visual chart-reading.

**Signal.** Parse market questions for continuous thresholds (CPI/PPI/NFP levels,
vote-share thresholds, price targets). For each such market, compute the
threshold-implied probability as `Φ((θ − μ_t)/σ_t)` where `μ_t, σ_t` are rolling
estimates of the underlying from available data. Compare to market price.

**Trade rule.** Trade the gap when |implied − market| > 5%, exit at resolution.

**Edge type:** structural / behavioral. **Horizon:** days–weeks (to resolution).
**Frequency:** low (50–200 markets/yr). **Capacity:** large per trade.
**Effort:** L (threshold parser + underlying-series fetcher).

**How to test.**
- Start with CPI/PPI/NFP — clean numeric targets with Bloomberg consensus series.
- Backtest: on every market day, compute implied vs market probability.
  Threshold trades at 5% gap, mark to resolution.
- Placebo: randomize the threshold; edge should vanish.

**Risks.** The underlying-series model is the critical assumption. Bad μ, σ
estimates → bad implied probability → losses. Use implied volatility from
options markets where available.

---

## 2. Graph neural networks on the trader–market bipartite

### 2.1 Trader-market bipartite GNN for informed-cluster detection

**Thesis.** The whale strategy identifies individual informed traders. Traders
don't act alone — **informed clusters** (who co-trade the same markets with
similar timing) have joint alpha greater than the sum of parts. A GNN over the
bipartite (trader, market, trade) graph identifies clusters that individual-level
analysis misses.

**Why ML.** Clusters in a bipartite graph are not detectable by per-node statistics.
Modern GNN embeddings (GraphSAGE, GAT) learn node representations that encode
neighborhood structure up to K hops.

**Signal.** Build bipartite graph from `trades.parquet`:
- Nodes: traders (`proxyWallet`), markets (`conditionId`)
- Edges: trades, weighted by size × sign(direction)
- Train a GAT with forward-return objective (predict 24h P&L from trader–market
  neighborhood at trade time)
- Extract trader embeddings → cluster → identify "informed communities"

**Trade rule.** When ≥3 members of a high-alpha cluster trade the same side in
the same market within 30 min, enter same-direction. Size by cluster's ex-ante
Sharpe.

**Edge type:** informational / network. **Horizon:** hours–days.
**Frequency:** medium. **Capacity:** medium. **Effort:** L.

**How to test.**
- Pre-req: ~1 month on a single GPU to train (8M edges, ~100k trader nodes).
- Expanding-window retrain quarterly.
- Ablation: GNN-cluster signal vs. (a) per-whale signal (V1 strategy), (b) random
  cluster assignment. GNN must dominate both.

**Risks.** Clusters may just be "bot farms" that amplify a single operator. The
clustering can still be predictive if the operator is informed, but capacity
shrinks if they're on the other side of your trades.

---

### 2.2 Market-similarity GNN for cross-market signal propagation

**Thesis.** Markets share information substrates (same entities, same resolution
source, same event frame). When market A moves, markets in its "information
neighborhood" should move too — but with latency. A GNN that learns these
neighborhoods from both semantic and co-movement signals predicts which
markets will follow.

**Why ML.** Semantic similarity alone (V1 §7.2) over-groups; co-movement alone
over-specifies. A learned graph with message passing finds the operational
neighborhood.

**Signal.** Build market graph:
- Nodes: markets
- Edge features: (a) semantic similarity of question text, (b) resolution-source
  match, (c) entity overlap from NER, (d) pairwise return correlation
- Node features: category, TTR, volume, price, OHLCV stats
- Train a GNN: given market A moves by Δ at time t, predict move of every other
  market at t+1h.

**Trade rule.** When predicted |Δ_B| > 3% and observed |Δ_B| < 1% at t+30min,
enter direction of prediction on market B. Exit on prediction convergence or
2h timeout.

**Edge type:** informational lag. **Horizon:** 1–6h. **Effort:** L.

**How to test.**
- Hold out 2 full months after training cutoff.
- AUC for direction > 0.58 on held-out market-pair moves.
- Economic significance: Sharpe > 1.0 net of costs on top-decile predictions.

**Risks.** Overfitting to idiosyncratic cross-market correlations that don't
persist. Mandate walk-forward retraining monthly.

---

## 3. Self-supervised representation learning on price paths

### 3.1 Contrastive embeddings of price microstructure

**Thesis.** Raw price paths are noisy, bounded, non-stationary — bad inputs
for direct prediction. But a **self-supervised contrastive model** (SimCLR-style)
trained to produce similar embeddings for temporally adjacent windows produces
*denoised* representations whose clusters correspond to recurring market
microstates (accumulation, distribution, equilibrium, cascade).

**Why ML.** Classical features (RSI, momentum, volatility) are hand-crafted and
category-specific. SSL learns representations that generalize across markets
and horizons without labels. Downstream supervised probes need only tens of
examples instead of thousands.

**Signal.** Tokenize each market's tick stream into overlapping 5-min windows
(price, volume, tick count). Augment with (a) time shift, (b) magnitude scaling,
(c) random masking. Train contrastive loss: positive = same market ±30 min,
negative = random window. Output: 128-dim embedding per window.

**Trade rule.** Train a simple head (logistic regression) on ~500 labeled windows
("follow-through" vs "reversal" based on next 60min return). At inference,
trade when head probability > 0.65.

**Edge type:** microstructure + ML. **Horizon:** minutes–hours.
**Frequency:** very high (every liquid market, every 5 min). **Effort:** M.

**How to test.**
- Pre-train on 80% of the ticks parquet (no labels).
- Probe on labeled subset. Must beat a bag-of-classical-features baseline
  (RSI, VWAP dev, order-flow imbalance) by ≥3 percentage points AUC.
- Adversarial: embeddings should *not* linearly predict `category` — they should
  encode microstate, not identity.

**Risks.** Contrastive collapse (embeddings become trivial). Use BYOL-style
asymmetric architecture to avoid.

---

### 3.2 Masked-language-model-style pre-training on tick streams

**Thesis.** Adapt the BERT pre-training objective (masked token prediction) to
tick sequences. The model must reconstruct masked (price, size, side) tokens
from context — a self-supervised task that forces it to learn *conditional*
market dynamics. Use the pre-trained model as a feature extractor for any
downstream task (forecasting, anomaly detection, etc.).

**Why ML.** The single most successful transfer-learning recipe in ML (NLP,
vision, code) has barely been applied to prediction markets. Polymarket's rich
tick data (7.9M+ ticks) is enough for meaningful pre-training.

**Signal.** Tokenize each trade as `(price_bucket, size_bucket, side, Δt_bucket)`.
Mask 15% randomly. Train transformer encoder (~20M params) to predict the masks.
Use `[CLS]` token embedding as a "market state" representation.

**Trade rule.** Same as §3.1 but with a stronger base model. The same probe
should classify follow-through vs reversal with higher AUC.

**Edge type:** ML / microstructure. **Effort:** L (needs GPU for ~1 week
pre-training, or ~$200 of cloud compute).

**How to test.**
- Beat §3.1 contrastive embeddings on the same held-out probe task.
- Compute a **scaling curve**: probe AUC vs. pre-training tokens. Confirms the
  model is actually benefiting from pre-training (not just capacity).

**Risks.** Compute cost. A badly-tuned pre-training run wastes a week. De-risk
by starting with §3.1 (faster to iterate) and upgrading.

---

## 4. Probabilistic & structural time-series models

### 4.1 Bayesian structural time series for news-impact decomposition

**Thesis.** A price move after news is a mixture of (a) permanent
information impact (informed flow) and (b) transient liquidity impact (flow
imbalance). **Bayesian structural time series** (Brodersen et al. 2015) separates
these components with posterior uncertainty. Trade the *transient* component as
it reverts.

**Why ML / why not rules.** Rule-based "revert after N minutes" gives you a
point estimate and no uncertainty. BSTS gives you a full posterior; you can
size positions by posterior confidence and avoid trades where the permanent
component dominates.

**Signal.** On a news event at time t, fit BSTS on price[t−H:t+H] with a
post-event intervention regressor. Decompose into trend, seasonal, regression,
and residual components. If the regression (event) effect has `P(permanent) > 0.6`,
skip; otherwise, trade the expected revert.

**Trade rule.** Enter opposite to the event impact; exit at posterior-mean
revert level or max 6h.

**Edge type:** informational / microstructure. **Horizon:** 1–6h.
**Frequency:** medium (every news event on a liquid market). **Effort:** M.

**How to test.**
- Reuse `news_monitor.py` event log as treatment-time ground truth.
- Backtest: for each event, run the BSTS decomposition (causalimpact library),
  trade according to rule above.
- Compare to the existing latency-arb baseline. BSTS should dominate on events
  where news is already priced in (rule-based strategy over-fires).

**Risks.** BSTS is compute-heavy (~1s per event per market). Precompute priors
from historical events; use warm-starts.

---

### 4.2 Hidden Markov Models for per-market regime switching

**Thesis.** Each market alternates between distinct regimes (equilibrium,
news-driven, squeeze, pre-resolution). Regime identity is latent but
inferable from volatility, volume, and microstructure features. A per-market
HMM estimates the current regime; strategies are regime-conditional.

**Why ML.** Classical regime filters (1-feature thresholds) ignore feature
interactions. HMM jointly models state dynamics and observation likelihoods
with probabilistic transitions.

**Signal.** Fit a 4-state HMM per market on 5-min features: realized vol,
volume z-score, |price − 0.5|, buy–sell imbalance. States labeled post-hoc
by their (vol, volume, imbalance) centroid. Emit regime probability vector at
every observation.

**Trade rule.** Strategy-specific regime masks. Mean-reversion only in
`equilibrium` regime (high prob). Momentum only in `news-driven` regime.
Skip all strategies in `pre-resolution`.

**Edge type:** allocation overlay. **Horizon:** same as underlying strategies.
**Effort:** M.

**How to test.**
- Fit HMMs on 70% of history; evaluate regime transition predictions on the
  rest.
- For each deployed strategy, test Sharpe with vs. without regime mask.
  Mask must improve Sharpe with smaller max DD.

**Risks.** HMM state labels drift across retrainings. Pin labels by their
centroid features, not state indices, for cross-period comparability.

---

### 4.3 Gaussian-process price manifold for deviation detection

**Thesis.** Prices across markets in the same category share a latent "fair
value manifold" as a function of (time-to-resolution, entity type, volume,
news intensity). A Gaussian process fits this manifold with principled
uncertainty; 2σ deviations are tradable mispricings.

**Why ML.** GPs give non-parametric posteriors — you don't have to specify the
fair-value function, only the kernel. Uncertainty-aware position sizing falls
out naturally.

**Signal.** Per category, fit a GP with kernel `RBF(TTR) × Linear(volume) ×
RBF(news_intensity)` on a labeled training set of resolved-markets-pricetrajectories.
At inference, score each live market's current price relative to GP posterior
for similar features.

**Trade rule.** Enter in direction of GP posterior mean when |price − μ_GP| > 2σ_GP.
Exit at posterior mean or 6h timeout.

**Edge type:** structural / ML. **Horizon:** hours. **Effort:** M.

**How to test.**
- GP scales `O(n^3)`; use sparse GPs (`gpflow`, inducing points) for n > 10k.
- Held-out category test: train on 5 categories, evaluate on Sports + Tech.
  Cross-category generalization is the proper edge test.

**Risks.** Kernel misspecification → biased uncertainty. Validate with posterior
predictive checks before trading.

---

## 5. LLM-based multi-step resolution reasoning

### 5.1 LLM agent with retrieval for resolution forecasting

**Thesis.** GPT-4 / Claude + web search can forecast market resolutions with
non-trivial accuracy (Halawi et al. 2024 showed comparable to crowd for
short-horizon events). Systematically running an LLM forecaster over every
live market, then trading on the gap between LLM-implied and market-implied
probability, extracts this edge.

**Why ML / why not manual.** Manual forecasting on 4,000 markets is impossible.
An LLM agent with structured chain-of-thought + retrieval can process the
universe daily.

**Signal.** For each live market with volume > $10k, run a prompt chain:
1. Parse question + resolution source + end date.
2. Retrieve recent news (NewsAPI.ai) and base-rate data.
3. Explicit chain-of-thought: decompose into sub-questions.
4. Aggregate with calibration (the model's uncalibrated probabilities need a
   learned Platt-scaling adjustment).

**Trade rule.** Trade when |LLM_prob − market_prob| > 10%, exit at resolution
or on subsequent LLM update that closes the gap.

**Edge type:** informational. **Horizon:** days–weeks. **Frequency:** medium
(~50–200 actionable markets/week). **Capacity:** medium. **Effort:** L.

**How to test.**
- **Calibration is critical.** Plot LLM probabilities vs. realized frequencies
  across 500+ resolved markets. Fit Platt / isotonic scaling.
- On the adjusted probabilities, Brier score must beat market price's Brier.
- Pre-specify: if calibrated LLM Brier ≥ market Brier, kill the strategy.

**Risks.** (1) Cost: ~$0.50 per market × 4,000 markets = $2,000/scan. Cap
to high-volume universe. (2) LLM overconfidence on sports / entertainment
(known bias). Calibration set must include these.

---

### 5.2 LLM-extracted structural constraints (auto-arb miner)

**Thesis.** V1 §3.1 (conditional probability arb) requires manually finding
conditional pairs. An LLM pass over `markets_filtered.csv` can enumerate
all structural no-arb relations automatically: P(A) ≥ P(A∩B), P(A∪B) = P(A) + P(B) − P(A∩B),
logical equivalence, time-subset, etc.

**Why ML.** Enumerating 4,000² = 16M market pairs for structural relations by
rule is fragile — question phrasings vary wildly. LLMs are robust to
paraphrase.

**Signal.** Chunk markets by category × TTR bucket (reduces pairs). For each
chunk, prompt Claude with: "Given these N markets, list all pairs with a
logical/probabilistic relation (subset, conjunction, disjunction, identity,
mutually exclusive). Return as JSON." Manually QA top outputs.

**Trade rule.** For each extracted relation type, apply the corresponding
no-arb rule (as in V1 §3.1, §2.3).

**Edge type:** structural. **Horizon:** to resolution.
**Frequency:** depends on yield. **Capacity:** medium.
**Effort:** M.

**How to test.**
- Precision check: manually validate 200 LLM-extracted pairs. Require precision
  > 90% before trading.
- Backtest: for validated pairs, apply no-arb rules; measure PnL and false-arb rate.

**Risks.** LLM hallucinates relations that look plausible but are wrong. High
false-positive rate kills the strategy. Filter by LLM self-rated confidence
+ human spot-check.

---

### 5.3 Narrative-tracking agent with persistent memory

**Thesis.** Each major market has an associated "narrative" (the set of facts
and expectations driving its price). As news accumulates, the narrative shifts.
A stateful LLM agent that maintains a per-market narrative log and surfaces
**contradiction events** (new fact contradicts prior narrative) gives early
warning of regime changes.

**Why ML.** This is genuinely multi-step reasoning: (a) update a narrative
given new news, (b) check consistency with prior narrative, (c) flag
contradictions. Not a single LLM call — a persistent agent loop.

**Signal.** Per-market state = running narrative summary (~500 tokens) +
confidence score. Hourly cron:
1. Fetch new news / trades for this market.
2. Update narrative via LLM: "Given prior narrative {X} and new events {Y},
   produce updated narrative."
3. Score contradiction: "Does new evidence contradict any claim in the prior
   narrative? Rate 0–10."
4. If contradiction > 7, emit signal.

**Trade rule.** Fade the current market direction when contradiction fires
(the prior narrative is the one priced in).

**Edge type:** informational. **Horizon:** hours–days. **Frequency:** low.
**Effort:** L.

**How to test.**
- Backtest: replay historical news into the agent, log contradictions,
  measure forward returns conditional on contradiction.
- Baseline: news-volume spike alone (no LLM). Agent must beat the baseline.

**Risks.** Narrative drift: the agent's narrative becomes circular (self-supported).
Force anchoring to source quotes + periodic reset.

---

## 6. Reinforcement learning for execution & market making

### 6.1 RL market-making agent

**Thesis.** Posting two-sided quotes on Polymarket's CLOB earns the spread
minus adverse selection. The spread and cancel cadence are the agent's decision
variables. A PPO agent trained in a realistic simulator captures microstructure
alpha as a liquidity *provider* rather than taker.

**Why ML.** Classical MM models (Avellaneda–Stoikov) assume continuous diffusion,
which is a poor fit for bounded prediction markets with discrete resolution
shocks. RL learns the optimal policy end-to-end under realistic dynamics.

**Signal.** State: current inventory, recent trade flow, book imbalance,
time-to-resolution, volatility regime. Action: `(bid_offset, ask_offset, quote_size)`.
Reward: realized PnL − inventory penalty − risk penalty.

**Trade rule.** Continuous quoting within risk limits. Cap inventory at $X per
market; flatten at resolution.

**Edge type:** microstructure. **Horizon:** minutes. **Frequency:** continuous.
**Capacity:** large (scales with quote size). **Effort:** L.

**How to test.**
- Pre-req: CLOB book recorder (same as V1 §1.2) — order-book state is required
  for training.
- Simulator must model (a) toxic flow (some takers are informed), (b) resolution
  shocks, (c) cancel/post fees.
- Paper trade in the live environment with small size for 30 days before any
  capital.

**Risks.** Sim-to-real gap. Polymarket live flow has characteristics the sim
may miss; start with small inventory and validate before scaling.

---

### 6.2 RL optimal-execution agent for whale-follow

**Thesis.** The whale-follow strategy enters aggressively (market order) after
detection. Slippage on large entries eats significant edge. An RL execution
agent learns *when* to enter (market vs limit vs partial) to minimize
execution cost while still catching the whale's direction.

**Why ML.** Execution is a sequential decision under uncertainty (will the
price revert while I wait? will the limit fill?). RL is the right tool.

**Signal.** State: (signal strength, time since signal, current price,
book depth, volatility). Action: `(aggression ∈ [0, 1], order_size_pct)`.
Reward: execution PnL = (eventual exit price − entry price) × size − fees.

**Trade rule.** Replace the existing whale-follow market order with RL-chosen
execution strategy.

**Edge type:** execution improvement. **Frequency:** same as whale strategy.
**Effort:** M (builds on existing whale signal + simulator).

**How to test.**
- Benchmark vs. market-order baseline over 1,000 historical whale signals.
- Kill criterion: if mean cost saving ≤ fee × 0.3, not worth complexity.

**Risks.** Over-optimization to specific book state. Add training-time book
noise to prevent overfitting.

---

## 7. Jumps, extremes, and point-process clustering

### 7.1 Lee-Mykland jump test + classified jump trading

**Thesis.** Prediction-market prices exhibit both continuous diffusion and
discrete jumps (news arrivals). The **Lee–Mykland** test (2008) statistically
identifies jumps. Once detected, a classifier labels each jump as
**informed-persisting** or **noise-reverting**, using pre-jump features.
Trade accordingly.

**Why ML.** The jump *detection* is classical statistics, but the *classification*
is where ML earns its keep — predicting persistence from pre-jump microstructure
context (who traded, what size, what news concurrence).

**Signal.** Two-stage:
1. Run Lee–Mykland on 1-min returns; identify jumps at p < 0.01 (bonferroni
   across markets).
2. For each jump, build feature vector: pre-jump volume z, time-to-resolution,
   concurrent news flag, whale flag, category. Train gradient-boosted classifier
   on historical jumps labeled by 60-min follow-through (persist: |ret_60| > |ret_jump|).

**Trade rule.** Persisting jumps → continue; reverting jumps → fade. Size
proportional to classifier confidence.

**Edge type:** microstructure × ML. **Horizon:** 30min–2h.
**Frequency:** high (dozens/day universe-wide). **Effort:** M.

**How to test.**
- Rolling out-of-sample: train on 6 months, test on 1, slide. Require consistent
  AUC ≥ 0.62.
- Calibration: predicted persistence probabilities should match realized
  frequencies within 5%.

**Risks.** Jumps are rare → small training set. Use a bagging ensemble to
stabilize predictions.

---

### 7.2 Hawkes-process model of trade clustering for pre-positioning

**Thesis.** Trades on prediction markets exhibit **self-excitation**: one
trade begets more trades (clustering). A **Hawkes process** with parametric
kernel estimates the conditional intensity of future trades given past
arrivals. High predicted intensity → incoming flow → pre-position in the
net direction of the predicted flow.

**Why ML.** Classical volume heuristics (rolling average) are a zeroth-order
intensity estimator; Hawkes is a first-order process model with closed-form
intensity given the kernel.

**Signal.** For each market, fit a multivariate Hawkes process on (buy, sell)
arrivals with exponential kernels. At any time, compute short-term predicted
intensity for each side. Signal = imbalance in predicted intensities.

**Trade rule.** When predicted 5-min buy intensity > 2 × sell intensity (and
absolute level is high), long YES. Exit on mean-revert or 30min timeout.

**Edge type:** microstructure. **Horizon:** minutes. **Effort:** M.

**How to test.**
- Fit with `tick` or custom MLE; per-market kernels change monthly.
- Backtest: measure realized imbalance vs. predicted. Sharpe net of costs on
  the top-decile predicted imbalances.

**Risks.** Parametric Hawkes (exponential kernel) may miss true dynamics. A
neural Hawkes extension (Mei–Eisner 2017) is a future upgrade.

---

### 7.3 Extreme value theory for tail-risk position sizing

**Thesis.** Resolution outcomes near 0 or 1 have heavy-tailed loss
distributions (rare but catastrophic). Classical Kelly sizing under-penalizes
these. **Peak-over-threshold** GPD fits to historical loss tails give a
principled tail-risk-aware Kelly, which *reduces* position size for
near-boundary markets.

**Why ML-adjacent.** EVT is the right statistical framework for rare-event
risk, and machine-learned predictors of tail parameters (as a function of
market features) are possible but secondary.

**Signal.** Fit a GPD to historical losses above the 95th percentile for each
market category. At position sizing, compute tail-Kelly:
`f* = Kelly × (1 − P(tail_loss > capital × tolerance))`.

**Trade rule.** Overlay: scale down position size when tail-VaR > threshold.
Does not generate signals, modifies sizing.

**Edge type:** risk overlay. **Effort:** S.

**How to test.**
- Backtest existing strategies with vs. without tail-Kelly. Evaluate max DD
  and Calmar ratio (return / max DD).

**Risks.** Minimal — overlay only reduces position; cannot create new losses.

---

## 8. Cross-domain transfer learning

### 8.1 Pre-train on Betfair/Pinnacle sports history, fine-tune for Polymarket

**Thesis.** Polymarket Sports markets are a fraction of Betfair/Pinnacle
sports volume. Transfer-learning from rich sports odds history (decades, all
major leagues) to Polymarket Sports markets should dramatically improve hit
rates on Sports — and possibly on general market dynamics.

**Why ML.** A model pre-trained on Betfair tick data learns
pricing-dynamics priors (closing-line-accuracy, steam moves, sharp-vs-square
flow) that transfer cleanly.

**Signal.** Pre-train a transformer (§3.2 architecture) on Betfair data.
Fine-tune on Polymarket Sports. Use as the direction-classifier for any
Polymarket Sports strategy.

**Trade rule.** Combine with existing strategies as a *conditional* signal:
trade only when the Sports-fine-tuned model agrees with the base strategy.

**Edge type:** informational. **Horizon:** same as underlying strategies.
**Effort:** L (Betfair data is not free; see Betfair Historical Data service).

**How to test.**
- Pre-train / fine-tune / eval splits with no leakage.
- Sharpe uplift on Polymarket Sports must exceed the Sports category's
  baseline Sharpe × 1.3 to be worth maintaining two models.

**Risks.** Commercial / licensing constraints on Betfair data. Start with
publicly available aggregate odds (OddsPortal scrape) before committing to a
paid feed.

---

### 8.2 Economic-indicator → macro-market direct mapping

**Thesis.** Markets on CPI, GDP, NFP, etc. have explicit numeric targets.
Consensus forecasts (Bloomberg, FactSet, Philly Fed SPF) with uncertainty
bands give a direct model of the underlying distribution. Regress market
prices on consensus + uncertainty + momentum, fade deviations.

**Why ML.** Simple regression may underfit. A nonlinear model (GBM / small
neural net) learns when to trust consensus (stable regimes) vs. fade it
(regime breaks).

**Signal.** For each macro-market, compute: consensus mean μ, consensus std σ,
days-to-release, realized path of high-frequency nowcasts, prior surprise
history. Train regressor: `market_price = f(features)`. Deviation trade
when |residual| > 2σ_cv (cross-val std).

**Trade rule.** Fade direction of residual, hold to release.

**Edge type:** informational. **Horizon:** days. **Frequency:** low (~30
macro releases/month). **Capacity:** large (macro markets are liquid).
**Effort:** M.

**How to test.**
- Labeled set: match each historical macro release to the corresponding
  Polymarket market's trajectory.
- Walk-forward: retrain weekly, eval next week's release.

**Risks.** Data licensing for Bloomberg consensus feed. Alternative:
cross-publisher scrape (Reuters + WSJ + Philly Fed public series).

---

## 9. Adversarial robustness & toxic-flow detection

### 9.1 Adversarial label-flip detector for whale registry

**Thesis.** A sophisticated adversary seeds the whale registry with
deliberate losses to steer followers into bad trades, then exits on the back
of whale-follow flow. Detecting **adversarial patterns** in whale behavior
(trades timed to accumulate following flow, then reversing) protects the
whale-follow strategy.

**Why ML.** An anomaly-detection model trained on "normal" whale behavior
(per-whale autoencoder reconstruction error) flags suspicious shifts even
before performance degrades statistically.

**Signal.** Per whale, train an autoencoder on (size, timing, frequency,
directionality) features of their normal trades. At each new trade, compute
reconstruction error. Sustained anomalous trades → downweight this whale in
the whale set.

**Trade rule.** Not standalone — reduces weight on flagged whales in
whale-follow, preventing adversarial exploitation.

**Edge type:** defensive. **Effort:** S.

**How to test.**
- Inject synthetic adversarial patterns into the whale registry; check detector
  recall.
- Measure: does the whale-follow strategy's live Sharpe stay closer to
  backtest Sharpe when this overlay is on vs. off?

**Risks.** False positives (genuine whale strategy shifts) cost edge by
over-filtering. Tune threshold on historical shift-point data.

---

### 9.2 Toxic-flow probability (VPIN) for taker-mode timing

**Thesis.** **VPIN** (Easley, López de Prado, O'Hara 2011) measures the
probability of informed trading in volume time. When VPIN is high, taker
execution faces adverse selection — wait or switch to maker. Low VPIN is the
window for aggressive entries.

**Why ML.** VPIN itself is classical but its *translation to strategy gating*
is a learnable calibration problem per-strategy, per-market.

**Signal.** Compute VPIN per market at 5-min cadence. Bucket by VPIN quintile.
Per strategy, measure hit rate within each bucket on historical trades.

**Trade rule.** Gate every strategy's taker entries by VPIN < 80th-percentile
for that market's recent history.

**Edge type:** execution quality. **Effort:** S–M.

**How to test.**
- Overlay on existing strategies. Target: same PnL with smaller max DD, or
  higher PnL with same max DD.

**Risks.** If a strategy's edge *is* trading against informed flow (e.g.
fading whale mistakes), VPIN gating removes the edge. Evaluate per-strategy.

---

## 10. Mechanism-design & game-theoretic edges

### 10.1 UMA optimistic-oracle challenge arbitrage

**Thesis.** Polymarket resolves via **UMA optimistic oracle**. When a
resolution proposal is posted, there's a ~2h dispute window. If you detect a
*wrong* proposal, challenging it earns the dispute bond. Automating proposal
monitoring + fact-checking is a pure mechanism-design edge.

**Why ML.** Automated fact-checking against the resolution source requires
NLP (resolution-text ↔ source-text comparison). Rule-based comparison is
brittle to source format changes.

**Signal.** Subscribe to UMA proposal events. For each, retrieve the resolution
source, parse the relevant claim, and run an LLM-based check: "Does `resolution
source text X` support `proposed resolution Y`? Return confidence 0–10 and
cite exact passages." Challenge if confidence < 3.

**Trade rule.** Challenge incorrect proposals. Prize = UMA dispute bond reward.
Separate strategy: pre-position in the market when you're confident the
proposal will be overturned.

**Edge type:** mechanism. **Horizon:** hours. **Frequency:** low (~1–5/week).
**Capacity:** capped by dispute bond size. **Effort:** M.

**How to test.**
- Historical UMA disputes have public outcomes. Backtest: would our system
  have challenged correctly? Precision must be very high (~95%) because
  incorrect challenges lose the bond.

**Risks.** False-positive challenge = lost bond. Very high precision threshold
required.

---

### 10.2 Fee-rebate / maker-incentive exploitation

**Thesis.** Polymarket occasionally runs maker-rebate programs (per-market or
per-trader). These create game-able incentives: optimal strategy is to
provide liquidity on rebated markets even at slightly negative spread, earning
rebate > spread cost.

**Why ML / why not rules.** Which markets to quote, at what size, depends on
multiple factors (rebate rate, queue position, adverse-selection risk).
Small RL or bandit algorithm optimizes the trade-off.

**Signal.** Per-market rebate rate (from promo announcements) + market
microstructure state → choose quote parameters. Lightweight contextual
bandit suffices initially; upgrade to RL once data accumulates.

**Trade rule.** Market-make on rebated markets with size scaled by expected
net-of-adverse-selection edge.

**Edge type:** structural (only exists during promos). **Horizon:** promo
duration. **Capacity:** large during promos. **Effort:** M.

**How to test.**
- Historical promo periods → simulate quoting → compute realized edge.
- Kill criterion: if realized edge < 0.5× rebate-theoretical, microstructure
  is eating it.

**Risks.** Rebate programs end abruptly. Code must detect and halt cleanly.

---

### 10.3 negRisk-market internal arbitrage

**Thesis.** Polymarket's `negRisk` markets (multi-outcome events) have an
*internal* AMM that differs from the CLOB prices. When they diverge beyond
fees, there's a guaranteed arb.

**Why ML.** Pattern detection is straightforward; the ML opportunity is in
*predicting* when divergences are about to open (pre-news, pre-resolution-shift)
so capital is pre-staged.

**Signal.** Monitor `ask_CLOB(YES) − bid_AMM(YES)` across all negRisk markets.
Arb when divergence > fees. Additionally: train a classifier on historical
divergences to predict near-term divergence opens.

**Trade rule.** Instantaneous arbs when detected; pre-position when classifier
predicts high divergence probability within 1h.

**Edge type:** structural. **Horizon:** seconds–hours. **Effort:** M.

**How to test.**
- Audit the AMM code / config to confirm arb mechanism.
- Backtest divergences from trade history; validate with book snapshots.

**Risks.** AMM params change; monitoring alerts required. May be fixed by
Polymarket in future releases (edge has shelf-life).

---

## 11. Prioritization matrix

Ranked by `(novelty × signal-to-noise × achievable with current data)`.

| # | Strategy | Novelty | S/N | Data? | Effort | Notes |
|---|---|---|---|---|---|---|
| 1 | §1.1 Synthetic-control cross-market | High | High | ✓ | M | Immediately testable, strong causal grounding. |
| 2 | §7.1 Lee-Mykland jumps + ML classifier | High | High | ✓ | M | Clean stat foundation + ML add-on. |
| 3 | §5.2 LLM-extracted auto-arb miner | High | High | ✓ | M | Scales V1 §3.1 automatically. |
| 4 | §1.2 Causal forests for whale-CATE | Medium | High | ✓ | M | Layers on existing whale strategy; likely big uplift. |
| 5 | §3.1 Contrastive embeddings | High | Medium | ✓ | M | Foundation for downstream probes; reusable infra. |
| 6 | §4.1 BSTS news-impact decomposition | High | High | ✓ | M | Separates priced-in from not-priced-in news. |
| 7 | §2.1 Trader-market GNN | High | Medium | ✓ | L | Heavy, but true network edge beyond individual whales. |
| 8 | §7.2 Hawkes pre-positioning | Medium | Medium | ✓ | M | Short horizon; capacity limited but high frequency. |
| 9 | §5.1 LLM resolution forecaster | Medium | Medium | partial | L | Expensive; requires calibration discipline. |
| 10 | §10.1 UMA challenge arb | High | High (if precision holds) | partial | M | Distinct edge type, capacity small. |
| 11 | §4.2 Per-market HMM regimes | Medium | Medium | ✓ | M | Value as an overlay, not standalone. |
| 12 | §6.1 RL market maker | High | High | ✗ | L | Needs book recorder; long pole. |
| 13 | §3.2 MLM pre-training on ticks | High | Medium | ✓ | L | Upgrade path from §3.1. |
| 14 | §8.1 Betfair transfer learning | Medium | High (if licensing works) | ✗ | L | Data licensing is the main blocker. |
| 15 | §9.2 VPIN gating | Low | Medium | ✓ | S | Fast win as a defensive overlay. |

---

## Appendix — Shared ML discipline (supplements V1 §9)

Every ML-based strategy must additionally satisfy:

**A. Model-drift monitoring.** Deploy with rolling feature-distribution checks.
Fire an alert when KS-stat against training distribution exceeds 0.15 on any
input feature. Common cause: Polymarket product changes (new market types,
UI changes affecting who trades).

**B. Calibration before capital.** Every probabilistic output must be
calibrated on a held-out set. Report reliability diagram; max calibration
error < 10% across probability deciles.

**C. Baseline comparison.** Every ML strategy must beat a simple baseline
(linear model / rolling mean / rule-based version) by a pre-declared margin.
"ML beat nothing" is not a validation.

**D. Feature importance audit.** Before deploying, inspect top-10 feature
importances. If any are obviously look-ahead (e.g. using a future-timestamped
feature due to pipeline bug), kill and fix.

**E. Adversarial robustness spot-check.** Perturb key features by ±1σ; model
output should be smoothly varying, not piecewise catastrophic. Brittle
models indicate overfitting.

**F. Capacity under model error.** Backtest under the realistic scenario that
the model is wrong 5% more than validation suggests. Strategies that die
under this perturbation are not robust.

End of document.

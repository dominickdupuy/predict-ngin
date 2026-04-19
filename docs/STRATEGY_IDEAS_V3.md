---
title: Polymarket Strategy Ideas V3 — High-Probability, Liquid-Only, Capacity-Aware
date: 2026-04-18
---

# Polymarket Strategy Ideas V3 — High-Probability, Liquid-Only, Capacity-Aware

**Scope.** This is the third strategy backlog after `STRATEGY_IDEAS.md` (V1, structural/behavioral) and `STRATEGY_IDEAS_V2.md` (V2, ML-native). V3 restricts itself to ideas that satisfy *all* of the following filters — the intersection of the user's stated constraints with what the existing backtests say actually survives costs:

1. **Liquid markets only** — universe = 128 markets with >$500k volume (per `LIQUID_MARKETS_BACKTEST_CORRECTED.md`). No strategy in V3 is allowed to rely on illiquid edge.
2. **Strong economic prior** — every edge has a mechanism from published quant finance or microstructure literature, not a "maybe this works" heuristic.
3. **Capacity-modelled** — each strategy reports a *Sharpe vs. deployed capital* curve, not a single Sharpe number. The curve is the deliverable.
4. **Alt data or ML is load-bearing, not cosmetic** — where present, it does something a rule-based strategy demonstrably can't.
5. **Does not duplicate V1/V2** — if an idea has a meaningful overlap it's explicitly called out and positioned as a refinement, not a restatement.

---

## Table of contents

1. Why most of V1/V2 won't scale — the capacity-wall diagnosis
2. Cross-asset anchor strategies (Fed funds, FX, Treasuries, VIX)
3. Nowcast-driven macro-market strategies (public alt data)
4. Term-structure / date-ladder strategies (hazard-rate fitting)
5. Liquidity-provider edges on liquid CLOB
6. Resolution-uncertainty discount trades
7. Signal-decay-aware auto-scaling overlay
8. Capacity & Sharpe-scaling analysis (the cross-cutting deliverable)
9. Prioritization matrix
10. Kill criteria — what would make us retire each strategy

---

## 1. Why most of V1/V2 won't scale — the capacity-wall diagnosis

The existing backtest reports already hint at this, but nobody has stated it cleanly:

- **Microstructure strategies** (iceberg detection, order-book imbalance, jump-continuation, Hawkes pre-positioning) all have Sharpe that collapses as `capital^{-0.5}` above ~$5k per signal. Square-root impact is baked into the CLOB simulator (`clob_simulator.py:149`), so doubling trade size multiplies impact cost by √2. At liquid-tier depth of $200k, a $5k trade eats 16bps of impact; a $20k trade eats 32bps. Signal edge on these strategies is 30–80bps. Conclusion: **capacity cap ~$2k per signal, ~$10k–$25k deployed**.
- **Whale-following** caps out where whale trade sizes cap: most whale prints are $500–$2k, so following at >$2k starts front-running the very flow you're following and blowing out impact costs. `LIQUID_MARKETS_BACKTEST_CORRECTED.md:72` reports avg trade $75; realistic capacity is $20k–$50k total, not $500k.
- **Structural arbs** (monotonicity, conditional, multi-outcome simplex, wrong-side) have *linear* capacity in the number of mispricings per year, not in capital per trade. They scale by *finding more mispricings*, which means expanding universe or building better matchers, not deploying more capital.
- **Information-edge strategies** (latency arb, news, LLM forecasters) have a cliff: Sharpe is high up to the capacity where your own trading moves the price, then it falls off linearly. The cliff is typically at 5–10% of daily volume per market.

This means: **scaling beyond ~$100k across the whole existing book requires strategies whose capacity is anchored to an external, deep market** (Fed funds, Treasuries, FX), not to Polymarket's own depth. That is the organizing principle of V3 §2–§3.

---

## 2. Cross-asset anchor strategies

The core idea: **if a Polymarket question has a deterministic mapping to a liquid external asset, the Polymarket price is the tradeable leg and the external asset is the hedge / price anchor.** Capacity is bounded by Polymarket depth, not the anchor — but the *signal* comes from the anchor, which is orders of magnitude deeper, so signal quality is effectively free.

### 2.1 Fed Funds futures → Polymarket FOMC markets

**Thesis.** CME Fed Funds futures (ZQ contracts) price the expected Fed Funds rate with sub-basis-point tightness and $100B+ open interest. Polymarket lists markets like "Fed cuts 25bp at June meeting" that imply a *specific* probability. Given the ZQ curve and a piecewise-constant rate-path model, the implied probability for a 25bp cut at a specific FOMC meeting is **closed-form**. Deviations of Polymarket from this implied probability by more than friction cost are mispricings — and Polymarket is consistently late to FOMC repricings because its user base is crypto-retail, not rates-desk.

**Why this is high probability.** This is a direct port of the fixed-income term-structure literature (Piazzesi 2005, Bauer 2024). The Fed Funds futures → rate-path decomposition is *the* benchmark for monetary-policy expectations. No ambiguity about the mapping; the only risk is execution timing.

**Signal.**
- For each upcoming FOMC meeting, compute `p_impl = P(cut ≥ 25bp | Fed Funds futures)` using the Gürkaynak–Sack–Swanson decomposition (publicly documented method).
- Every 5 minutes, compare `p_market = polymarket_mid(FOMC_25bp_cut_market)` to `p_impl`.
- Signal = `p_market − p_impl` after subtracting friction.

**Trade rule.** Fade the gap when `|p_market − p_impl| > 0.03` (3¢ after fees). Hold to FOMC resolution or gap convergence within 0.5¢.

**Alt data.** Fed Funds futures (CME free delayed quotes, 10-min; paid feeds at $0 for academic/retail). NO licensing friction for delayed; paid real-time is ~$110/month for CME core rates data. Use `pandas-datareader` or Ninja Trader's free API for prototyping.

**Edge type.** Cross-asset / informational. **Horizon.** Days–weeks (to FOMC). **Frequency.** ~8 FOMC meetings/yr × ~4 actionable markets (25bp / 50bp / hold / hike) = ~30 event-markets/yr. **Capacity.** Medium–large — FOMC Polymarket markets are usually the top-3 markets by volume in the Finance category. **Effort.** M.

**How to test.**
- Pull 3 years of ZQ historical tick data (CME free public FTP). Compute implied probability time series.
- Pull matched Polymarket FOMC markets (`data/research/Finance/` already has them).
- Align on UTC 5-min bars. Measure lead-lag: Polymarket should *lag* ZQ by 1–60 minutes on FOMC-expectation shifts.
- **Placebo:** shuffle the ZQ time series by ±1 day; signal should vanish.

**Kill criterion.** If Polymarket leads ZQ on FOMC expectations (reverse causality — Polymarket driving rates), the mapping is broken and the strategy is dead. Unlikely but testable.

**Capacity curve.** `Sharpe(C) ≈ Sharpe_max × min(1, $50k / C)^{0.5}` — the $50k cap is set by FOMC market depth on Polymarket, which is typically $200–500k per market in the 24h pre-meeting.

---

### 2.2 Recession-market ↔ Treasury curve inversion

**Thesis.** The 10Y–3M Treasury term spread is the single best-validated recession predictor (Estrella–Mishkin 1998, Bauer–Mertens 2018). Polymarket lists recession-probability markets. The mapping is *probabilistic* — 10Y-3M at −0.5% implies ~40% 12-month recession probability from the academic curve — and Polymarket consistently prices these markets on the anchoring bias of recent news, not the yield curve.

**Why this is high probability.** This is not a novel finding: multiple hedge funds trade the 10Y-3M spread as a macro signal. The novelty is applying the Estrella logistic regression to Polymarket's recession-probability markets and trading the residual.

**Signal.**
- Fit Estrella's logistic: `P(recession_{t+12m}) = Φ(α + β × spread_t)` on NBER-dated data through 2025.
- Each day, compute `p_model = logistic(β × spread_today)` and `p_market = polymarket_recession_mid`.
- Trade when `|p_model − p_market| > 0.08`.

**Trade rule.** Long the cheaper side, hold to resolution or until `|p_model − p_market| < 0.02`.

**Alt data.** FRED has free daily 10Y-3M spread (`T10Y3M`). Zero cost, unlimited API.

**Edge type.** Cross-asset macro. **Horizon.** Months. **Frequency.** Low — maybe 4–10 trades/yr with this specific signal, but per trade can be $10k–$50k. **Capacity.** Large — Polymarket recession markets typically carry $1M+ volume. **Effort.** S.

**Capacity curve.** Sharpe nearly flat up to $100k deployed per market due to long horizon (fill spread out over days), then degrades above.

---

### 2.3 VIX term-structure as risk-gate for political-event markets

**Thesis.** Not a direct-tradable edge — a *gate* for existing strategies. When VIX is in backwardation (1M > 3M), realized volatility spikes are coming, which means news-arrival frequency is about to jump. Latency arb and news-driven strategies should up-size; mean-reversion strategies should down-size. This is the "regime filter" from V2 §4.2 but with a *causal external regressor* instead of a within-market HMM.

**Why this is high probability.** VIX term-structure is the single most well-studied volatility signal in equities (Bollerslev–Mougeot 2003, Whaley 2013). Its utility as a risk-on / risk-off gate is settled science. Applying it to Polymarket is new only because nobody's done it yet.

**Signal.**
- Daily: compute `VIX1M − VIX3M`. If `> 0` (backwardation), flag "high-vol regime".
- In high-vol regime, multiply whale-follow and latency-arb position sizes by 1.3×; multiply pairs-trading / mean-reversion by 0.7×.

**Alt data.** CBOE publishes VIX term structure free (SPVXSP index; also VIX1M via CBOE data feed). Zero cost.

**Edge type.** Allocation overlay. **Capacity.** n/a (overlay). **Effort.** S.

**Capacity curve.** By construction, doesn't affect capacity — it redistributes capital across existing strategies during regime.

---

### 2.4 FX-pair → sovereign-event market arb

**Thesis.** When a country's currency moves sharply, Polymarket markets about that country's politics / geopolitical outcomes often lag. Example: USDRUB spikes ahead of a Russian-event market move; USDTRY spikes on Turkey elections. FX markets price political risk via carry traders first, retail prediction markets second.

**Why this is high probability.** Carry-trade unwinds on political risk are the canonical risk-on/risk-off flow in EM FX. There is academic evidence (Della Corte et al. 2016) that EM FX lead political-news flow by 1–4 hours. Retail prediction markets lag even more.

**Signal.** Build a directory `(country_code → list of Polymarket markets)`. For each country with >$1M FX daily volume:
- Compute 5-min returns of USD-vs-local-currency.
- When `|return_{1h}| > 2σ` for the FX pair, enter same-direction on any linked Polymarket market if that market hasn't moved >1% in the same window.

**Trade rule.** Market order; exit at `FX reversion to <1σ` or 6h timeout.

**Alt data.** Free: FRED (daily), TradingView (5-min free), OANDA free tier (5-min tick). Paid: Refinitiv tick ($400+/mo, not recommended until validated).

**Edge type.** Cross-asset informational. **Horizon.** 1–6h. **Frequency.** Low — maybe 20–50 trades/yr on the EM-political-event subset. **Capacity.** Medium per trade ($2k–$10k depending on market depth). **Effort.** M.

**Capacity curve.** Sharpe stable up to ~$10k per trade, then falls as capital^-0.5. Aggregate deployment cap ~$50k given low trade frequency.

---

### 2.5 Crypto ↔ crypto-event market spillover

**Thesis.** Polymarket has recurring markets like "Bitcoin > $X by Y", "Ethereum > $X", etc. These are just options with binary payoffs; their fair value is computable from spot + implied vol on deep crypto options venues (Deribit). The Polymarket price frequently diverges from Deribit-implied by 5%+.

**Why this is high probability.** Crypto options on Deribit are deep ($20B+ open interest on BTC) and their implied vol surface is observable free. Polymarket's crypto-price markets are retail; they don't replicate the Deribit option correctly, especially for OTM strikes.

**Signal.**
- For each Polymarket "BTC > $X by date" market, compute `p_impl = 1 − Φ((ln(X/S) − (r − σ²/2)τ) / (σ√τ))` using Deribit ATM IV as σ.
- Trade when `|p_market − p_impl| > 0.05`.

**Trade rule.** Same as §2.1; fade the gap.

**Alt data.** Deribit public API (free, real-time, no auth required for mark IV).

**Edge type.** Cross-asset / options. **Horizon.** To resolution. **Frequency.** Medium — 20+ crypto-price markets resolve monthly. **Capacity.** Large ($10k+/trade on liquid crypto markets). **Effort.** S — math is closed-form, just needs implementation.

**Capacity curve.** Best-scaling strategy in V3: Sharpe stays flat to $20k–$50k per trade because crypto markets are the most liquid non-political markets on Polymarket.

---

## 3. Nowcast-driven macro-market strategies

### 3.1 CPI nowcast from Truflation + retailer alt data

**Thesis.** Polymarket "Will CPI be > X% YoY?" markets settle on BLS data. BLS data is a lagged survey; real-time price indices (Truflation, PriceStats, Adobe Digital Price Index) nowcast CPI with 2–3 day lead. Academic literature (Cavallo 2017, Cavallo–Rigobon 2016) shows online-price nowcasts beat consensus forecasts consistently.

**Why this is high probability.** Online-price nowcasting is established academic methodology. The Fed's own CPI nowcast (Cleveland Fed Inflation Nowcast) uses similar data and publishes publicly. Polymarket traders anchor on consensus forecasts, which are stale.

**Signal.**
- Ingest Truflation daily index (public API, free), Cleveland Fed nowcast (free RSS), ADP weekly reports (free).
- Fit a daily CPI nowcast model: `CPI_YoY_nowcast = f(Truflation_ΔYoY, Cleveland_nowcast, ADP_wage_growth, recent_CPI_prints)` using elastic net.
- Compare `nowcast > X` probability to Polymarket `> X` probability via empirical CDF on the model's residual distribution.

**Trade rule.** Enter when `|p_nowcast − p_market| > 0.05`, hold through CPI release.

**Alt data.** Truflation API (free tier), Cleveland Fed (free), ADP (free), BLS historical (free), Adobe DPI (paid but $0 for academic). **Cost:** $0 to prototype, $50/mo to productionize.

**Edge type.** Informational / nowcast. **Horizon.** 1–30 days (to CPI release). **Frequency.** 12 CPI releases/yr × 2–3 markets each = ~30–40 opportunities. **Capacity.** Medium — CPI markets on Polymarket carry $200k–$500k volume. **Effort.** M.

**Capacity curve.** Sharpe stable to ~$10k per trade, degrades above.

---

### 3.2 NFP nowcast from ADP + JOLTS leading series

**Thesis.** Identical structure to §3.1 but for Nonfarm Payrolls. ADP publishes private-employment nowcast 2 days before NFP. JOLTS (job openings) leads NFP by 1–2 months. Combining these gives a calibrated NFP distribution that outperforms consensus forecasts (Aruoba–Diebold 2010 style nowcasting).

**Why this is high probability.** ADP is literally the pre-release version of NFP; historical correlation is >0.85 monthly. The deviation between ADP and consensus captures surprise.

**Signal.** Same structure as §3.1 but targeting NFP markets.

**Alt data.** ADP (free), BLS JOLTS (free), FRED CES preliminary (free).

**Edge type.** Informational / nowcast. **Horizon.** 1–3 days (NFP release). **Frequency.** 12/yr × 2 markets = ~24 opportunities. **Capacity.** Small–medium (NFP markets have thinner volume than CPI). **Effort.** S.

**Capacity curve.** Capacity-limited; Sharpe drops above $5k per trade.

---

### 3.3 Election nowcast from combined public aggregators

**Thesis.** Five-to-seven reputable forecast sources publish election probabilities (538, Silver Bulletin, Economist, NYT Needle, PredictIt, Metaculus). Each has calibration errors. An *ensemble* of these — weighted by historical calibration on past elections — beats Polymarket when Polymarket overshoots news (which it does, systematically, during debate nights and polling-release days).

**Why this is high probability.** Ensemble forecasting is the gold standard in political forecasting. The historical record of ensembles beating any single forecaster and individual markets is well documented (Graefe et al. 2014, Lichtman 2020).

**Signal.**
- Daily scrape of 5 forecast aggregators (public JSON from Silver Bulletin, 538 has deprecated but Decision Desk HQ / Split Ticket fill in).
- Weighted ensemble = isotonic-regression calibration on historical elections.
- Signal when ensemble differs from Polymarket by >5pp.

**Alt data.** All sources public and free.

**Edge type.** Informational / ensemble. **Horizon.** Days–months to election. **Frequency.** Spikes during election cycles; ~50–100 trades during US presidential cycle. **Capacity.** Large — election markets are the deepest on Polymarket ($10M+ volume). **Effort.** M.

**Capacity curve.** Best-scaling in this category: election market depth is so high that Sharpe stays flat to $50k per trade.

---

### 3.4 Supreme Court / judicial-decision nowcast from docket timing

**Thesis.** SCOTUS decisions follow semi-predictable docket patterns: opinions released in certain months, by certain justices, on certain day-of-week patterns. Decisions that are taking unusually long are correlated with closely-divided outcomes. Also, Chief Justice authorship timing (assignment sheets are semi-public) predicts outcome direction with weak but non-zero signal.

**Why this is reasonable.** There is legal-scholarship literature (Epstein et al. 2013) on docket timing as predictor of ideology / outcome. Polymarket's SCOTUS markets are thinly traded relative to their information content — classic inefficiency.

**Signal.** Per-case features: case age, oral-argument date, opinion-author assignment, amicus-brief count, prior-precedent distance. Model = logistic regression on historical post-2005 decisions.

**Alt data.** Supreme Court Database (washu.edu, free academic), SCOTUSblog (scrapable).

**Edge type.** Informational. **Horizon.** Weeks. **Frequency.** Low (~10–30 decisions/yr trade-worthy). **Capacity.** Small ($5k max per market). **Effort.** M.

**Capacity curve.** Small-total strategy, caps quickly at $50k deployed.

---

## 4. Term-structure / date-ladder strategies

### 4.1 Hazard-rate curve fitting on date-laddered markets

**Thesis.** Polymarket frequently lists "Will X happen by [date]?" for the same X with multiple dates (e.g. "Trump impeached by end of Q1 / Q2 / Q3 / Q4"). These markets satisfy an arbitrage-free constraint: the implied hazard rate must be non-negative at every horizon. Fitting a survival-function curve (Weibull, Gompertz, or nonparametric Kaplan–Meier style) to the observed market prices reveals:
- Points where the curve is non-monotone (arb, already covered by V1 monotonicity arb).
- Points where the curve shape is *unusual* relative to the hazard-rate prior for that event type — those are mispricings.

This is the extension of monotonicity arb to the *shape* of the survival curve, not just its monotonicity.

**Why this is high probability.** Hazard-rate modeling is standard in insurance / credit / reliability engineering. Fitting priors by event category (political events have Weibull shape α=1.5; legal events have α=0.8, etc.) is straightforward.

**Signal.**
- Cluster date-laddered events by shared "X" (already have `eventId` in `markets_filtered.csv`).
- For each cluster, fit `S(t) = exp(−(t/λ)^k)` Weibull survival to observed market prices with time-to-resolution as regressor.
- Compute expected prices for each date under the fitted Weibull.
- Trade markets that deviate by >5¢ from fit.

**Trade rule.** Long the cheap side of the deviation, short the rich side (delta-hedged within the ladder). Hold to resolution or until deviation < 1¢.

**Edge type.** Structural / shape constraint. **Horizon.** To resolution. **Frequency.** ~50 ladder-events/yr. **Capacity.** Large (trades spread across multiple markets per event). **Effort.** M.

**Kill criterion.** If Weibull fit's residual is not mean-zero on holdout (model misspecification), retry with Gompertz or nonparametric. If neither fits, retire.

**Capacity curve.** Sharpe roughly flat to $100k per ladder-event (capital spread across 3–5 markets per event).

---

### 4.2 Calendar-butterfly on same-event, different-date markets

**Thesis.** For three consecutive date-ladder markets (e.g. "by Q2" / "by Q3" / "by Q4"), the butterfly payoff `p(Q3) − 0.5 × (p(Q2) + p(Q4))` should be zero under linear interpolation and non-negative under any concave (i.e. declining-hazard-rate) model. Negative butterflies are tradable mispricings. Equivalent to "convexity arb" from fixed income.

**Why this is high probability.** This is a strict subset of §4.1 but easier to detect without fitting a model. It's also the standard butterfly-curve check in IR / commodity term structures.

**Signal.** Direct arithmetic on 3-market triples.

**Trade rule.** Long wings, short belly if butterfly < 0 by >2¢. Hold to resolution.

**Edge type.** Structural (mechanical). **Capacity.** Medium. **Effort.** S.

**Capacity curve.** Small per trade but high frequency if many ladder-events exist; scales by count of events.

---

## 5. Liquidity-provider edges on liquid CLOB

### 5.1 Round-price retail-flow liquidity provision

**Thesis.** Retail flow on Polymarket clusters at round prices (5¢, 10¢, 15¢, 20¢, 25¢, 50¢). Analogous to equity markets where retail order flow clusters at round prices (Kumar 2009, Barberis–Thaler 2003). By posting limit orders *one tick inside* the round-price cluster, a maker collects most of the round-price retail flow without competing with professional MMs who post at true fair value.

**Why this is high probability.** Round-price retail-flow capture is one of the oldest and most consistently profitable market-making strategies in equities. The Polymarket application has not been mined.

**Signal.**
- Identify the 20 most-traded liquid Polymarket markets (they trade $50k–$200k/day).
- For each, observe historical trade-print distribution by cent bucket. Round prices should be ~2× over-represented.
- Post: bid at `round_price − 0.01`, ask at `round_price + 0.01`, size $100–$500 per level.

**Trade rule.** Replenish filled orders. Flat inventory end-of-day via paired market order.

**Edge type.** Microstructure / retail flow. **Horizon.** Minutes–hours. **Frequency.** Very high (continuous quoting). **Capacity.** Medium ($5k–$15k total deployed; capped by inventory risk). **Effort.** M (needs low-latency quote-maintenance infrastructure).

**Alt data.** None — pure Polymarket CLOB + trade data.

**Capacity curve.** Sharpe flat to $10k, degrades sharply above because you become the marginal liquidity and retail flow fills less of your size.

---

### 5.2 Pre-resolution liquidity vacuum capture

**Thesis.** In the final 2 hours before a Polymarket market resolves, MMs pull quotes (to avoid being picked off by informed flow on resolution news). This leaves a liquidity vacuum: a single $500 market order can move price 5–10¢. A counter-strategy posts thin limit orders *at* the theoretical fair value (0.99 for near-certain YES) to absorb forced-liquidation flow from MMs unwinding their books.

**Why this is high probability.** This is the same flow as end-of-day institutional rebalancing in equities (Chan–Lakonishok 1995). Liquidity providers *into* the vacuum systematically earn a premium.

**Signal.** Per market, last 2h before `endDateIso`, if `p_market > 0.90` or `< 0.10`, post limit order 0.5¢ outside the theoretical 0/1 value up to capped size.

**Trade rule.** Hold to resolution. Bounded downside (max loss = 1¢ per contract if wrong direction).

**Edge type.** Microstructure / structural. **Horizon.** 0–2h. **Frequency.** Per-resolution event (~100/yr on liquid markets). **Capacity.** Small per trade ($500–$2k) but many trades. **Effort.** S.

**Capacity curve.** Sharpe roughly flat to $20k total deployed.

---

## 6. Resolution-uncertainty discount trades

### 6.1 UMA dispute-risk discount mining

**Thesis.** Markets near 0.99 or 0.01 that "should be" at 1.0 or 0.0 are trading at a discount because of UMA-dispute risk. Historical UMA dispute rate is 2–3%; observed discount is often 3–5%. After controlling for dispute risk, residual discount is free money.

**Why this is high probability.** UMA dispute history is public. Computing dispute base-rates by market category (Politics disputes more than Sports, crypto-price disputes rare, etc.) is straightforward.

**Signal.**
- Historical: pull all resolved markets with final price ≥ 0.95 in the last 2h before resolution. Fraction that resolved YES = `p_resolved_YES | priced_near_YES`.
- Live: for each near-resolution market at price ≥ 0.95, compute `expected_value = p_resolved_YES × 1.0 + (1 − p_resolved_YES) × 0`. If `p_market < expected_value − fees`, buy YES.

**Trade rule.** Buy, hold to resolution. Equivalent to selling near-the-money binary put. Position-capped to limit tail loss.

**Alt data.** UMA dispute history (scraped from UMA protocol contract events, free via Polygon RPC / subgraph).

**Edge type.** Structural / time-decay. **Horizon.** <12h. **Frequency.** 50–100 opportunities/week on liquid universe. **Capacity.** Small per trade due to capped tail (max payout = 0.05 × size). **Effort.** S–M.

**Capacity curve.** Strictly capped by tail risk tolerance — cap position at 2% of capital per trade; total deployed limited to maybe $20k regardless of Sharpe.

**Overlap warning.** This is adjacent to V1 §6.1 (theta harvesting) but different: V1 trades the unconditional decay; V3 §6.1 *conditions on dispute probability*, which is the source of the residual edge after V1 is run. They compose multiplicatively, not additively.

---

### 6.2 Ambiguous-resolution premium (the long version of §6.1)

**Thesis.** The converse: markets where dispute risk is *under*priced and the market trades at 0.98 when historical dispute risk in that category implies it should be 0.92. Short (buy NO) at 0.02 costs 1.0 − 0.02 = 0.98 in capital but pays 0.08 × expected in the dispute-wins-for-NO case.

**Why this is high probability.** Same data source as §6.1, inverted. Worth having both.

**Capacity curve.** Tiny, by design; most dispute-risk is correctly priced. Maybe $5k total deployment.

---

## 7. Signal-decay-aware auto-scaling overlay

### 7.1 Rolling signal-decay detector + auto-unwind

**Thesis.** Every strategy has a finite shelf life. Whale-following will decay as whales diversify away from markets being copied. Latency arb will decay as Polymarket's latency improves. Round-price retail capture will decay as retail flow shifts. Most ruin comes from running dead strategies past their decay point. A *rolling* signal-decay detector that continuously compares live performance to backtest baseline — with a pre-registered decay threshold — prevents this.

**Why this is high probability.** This is the *only* overlay in the entire V1/V2/V3 backlog that directly addresses the #1 cause of real-world quant losses: strategy decay. (See López de Prado 2018 Chapter 12; also Harvey–Liu 2015.)

**Implementation.**
- For each deployed strategy: maintain 30-day rolling live PnL series.
- Compute `live_sharpe / backtest_sharpe` as a ratio.
- Pre-register: if ratio drops below 0.5 for 14 consecutive days, auto-reduce capital by 50%. Below 0.2 for 7 days, full unwind.
- Always report the ratio on a dashboard (already partially in `PAPER_TRADING_TOP10.md` scaffolding).

**Alt data.** None — purely a monitoring layer on existing strategy PnL.

**Edge type.** Risk overlay / survival. **Effort.** S.

**Capacity curve.** n/a (overlay). Its *contribution* is to keep strategies inside their capacity curve and retire them before they become negative-drift.

---

## 8. Capacity & Sharpe-scaling analysis

This is the cross-cutting deliverable the user explicitly asked for. What follows is a **quantitative model** of how Sharpe varies with deployed capital for each strategy class — derived from the CLOB simulator's square-root impact formula (`clob_simulator.py:149`) combined with the empirical market-depth distribution from the liquid-universe report.

### 8.1 The universal Sharpe-vs-capital equation

Under the square-root impact model, the per-trade cost as a fraction of position is:

```
cost_bps(size) = spread_bps + impact_coeff × √(size / depth) × 10_000 + fee_bps
```

Net edge per trade is:

```
net_edge_bps(size) = raw_edge_bps − cost_bps(size)
```

And the Sharpe of a strategy with N independent trades and position `size` per trade, deployed capital `C = N × size × time_share`:

```
Sharpe(C) ∝ √N × net_edge_bps(size) / σ_per_trade
```

Above the capacity point where `cost_bps(size) = raw_edge_bps`, `net_edge` turns negative and Sharpe collapses.

### 8.2 Strategy-by-strategy capacity curves

For liquid-tier parameters (spread 10bps, fee 20bps, impact_coeff 0.001, depth $200k):

| Strategy class | Raw edge (bps) | Capacity per trade | Total capacity | Sharpe at capacity | Sharpe at 2× capacity |
|---|---|---|---|---|---|
| Structural arb (monotonicity, conditional, simplex) | 100–500 | $20k+ (limited by market depth, not strategy) | $100k–$500k | 2.0–3.5 | 1.5–2.5 (grows with N, not size) |
| Whale-follow (CATE-filtered) | 150 | $2k (whale size cap) | $20k–$50k | 1.5–2.2 | 0.8–1.2 |
| Cross-asset anchor (Fed funds, Deribit) | 80–200 | $10k | $50k–$200k | 1.8–2.5 | 1.2–1.6 |
| Nowcast macro (CPI, NFP, elections) | 100–300 | $10k–$50k (market depth) | $50k–$200k | 1.5–2.2 | 1.0–1.6 |
| BSTS news decomposition | 60–100 | $5k | $20k–$40k | 1.2–1.5 | 0.6–0.9 |
| Hazard-rate / butterfly (§4) | 30–100 | $20k+ (spread over ladder) | $100k+ | 1.2–1.8 | 1.0–1.5 |
| Round-price LP (§5.1) | 30–50 | $500 (inventory) | $10k | 1.0–1.5 | 0.3–0.6 |
| Pre-resolution vacuum (§5.2) | 50–150 | $2k | $20k | 1.2–1.8 | 0.5–0.9 |
| UMA dispute-risk (§6) | 50–100 | $1k (tail-capped) | $20k | 0.8–1.2 | 0.4–0.6 |
| Pairs / mean-reversion | 40–80 | $1k | $10k–$20k | 1.0–1.3 | 0.3–0.5 |
| Lee-Mykland jumps | 30–60 | $500 | $5k–$10k | 0.8–1.2 | 0.2–0.4 |

**Key observations.**

1. **Three strategy classes have "nice" capacity curves** (Sharpe roughly flat to $50k+):
   - Structural / ladder arbs (§4)
   - Cross-asset anchor (§2)
   - Nowcast-driven macro (§3)

   These should be the *backbone* of any book scaling past $100k.

2. **Whale-follow and BSTS are the highest-Sharpe but most capacity-limited** strategies. They should be run at their saturation capacity ($20k each), not beyond.

3. **Microstructure strategies (LP, jumps, pairs, vacuum) are portfolio diversifiers, not scaling engines.** Each caps at $10k–$20k; their value is uncorrelated Sharpe with the anchor strategies, not absolute magnitude.

4. **The portfolio Sharpe at $100k deployed** with the anchor strategies + saturated microstructure:
   ```
   Anchor strategies:    $60k (Sharpe ~2.0, weight 60%) → 1.2 contribution
   Whale-follow:         $20k (Sharpe ~1.8, weight 20%) → 0.36 contribution
   Microstructure mix:   $20k (Sharpe ~1.2, weight 20%, ρ≈0.2) → 0.24 contribution
   Portfolio Sharpe (conservative, ρ=0.3 cross): ~1.6–1.9
   ```
   vs. the same $100k allocated naively to only whale-follow (which saturates): ~1.0 Sharpe.

### 8.3 The scaling schedule

If the stated target is to scale from the current $32k paper-trading book (per `LIQUID_MARKETS_BACKTEST_CORRECTED.md:250`) to $100k–$500k, the implied sequencing is:

- **$0–50k.** Whale-follow + BSTS + pairs. (What's already planned.)
- **$50k–$150k.** Add §2.1 (Fed-funds anchor), §3.1–§3.3 (nowcasts), §4.1–§4.2 (hazard/butterfly). These don't fight whale-follow for depth.
- **$150k–$300k.** Scale §2 + §3 + §4 proportionally. Microstructure strategies stay at saturation.
- **$300k+.** Capacity-bound. Only structural arbs can absorb more. Consider cross-platform (V1 §2.1 Kalshi arb) and multi-chain expansion before pushing further.

The Sharpe ceiling at each level:
- $50k: ~1.8
- $150k: ~1.7
- $300k: ~1.5
- $500k: ~1.2
- $1M+: likely <1.0 without structural-arb breakthrough or Kalshi integration.

The curve is *convex* — doubling capital from $50k to $100k costs 5% of Sharpe; from $500k to $1M costs 30%+. This is the central fact to internalize when sizing the book.

---

## 9. Prioritization matrix

Ranked by `(economic prior strength) × (liquid-market compatible) × (capacity × 1/effort)`.

| # | Strategy | Prior strength | Liquid-only? | Capacity | Effort | Notes |
|---|---|---|---|---|---|---|
| 1 | §2.1 Fed-funds → FOMC | Very high (rates literature) | ✓ | Large | M | Single cleanest new edge. Start here. |
| 2 | §2.5 Crypto ↔ crypto-price | Very high (Deribit surface) | ✓ | Large | S | Closed-form. Ship in a week. |
| 3 | §3.1 CPI nowcast | High (academic nowcasting) | ✓ | Medium | M | Alt data is free. |
| 4 | §3.3 Election ensemble | High (ensemble forecasting) | ✓ | Large | M | Best-scaling in its class. |
| 5 | §4.1 Hazard-rate ladder | Medium (own derivation) | ✓ | Large | M | Generalizes monotonicity arb. |
| 6 | §4.2 Calendar butterfly | High (IR analogy) | ✓ | Medium | S | Trivial once data layout is in place. |
| 7 | §2.2 Recession ↔ Treasury | High (Estrella literature) | ✓ | Large | S | Low frequency; steady contributor. |
| 8 | §7.1 Signal-decay overlay | Essential (survival) | n/a | n/a | S | Not optional — ship before scaling. |
| 9 | §6.1 UMA dispute-risk | Medium (empirical) | ✓ | Small | M | Composes with theta harvest. |
| 10 | §5.1 Round-price LP | High (retail-flow literature) | ✓ | Small | M | Needs quoting infra. |
| 11 | §3.2 NFP nowcast | High | ✓ | Small | S | Low capacity, quick to build. |
| 12 | §5.2 Pre-resolution vacuum | Medium (flow literature) | ✓ | Small | S | Clean, small. |
| 13 | §2.3 VIX gate | High (vol literature) | ✓ (overlay) | n/a | S | Overlay only. |
| 14 | §2.4 FX → sovereign markets | Medium | ✓ | Medium | M | EM-specific. |
| 15 | §3.4 SCOTUS docket | Low–medium | ✓ | Small | M | Niche. |

---

## 10. Kill criteria — what would make us retire each strategy

This is the section most V1/V2 ideas skip and it's where real P&L is lost. Every strategy in V3 must commit to a pre-registered kill criterion *before* deployment:

- **§2.1 Fed-funds.** Kill if ZQ → Polymarket lead-lag cross-correlation peak goes to zero or flips sign (would mean Polymarket is leading rates, i.e., thesis inverted).
- **§2.5 Crypto.** Kill if `|p_polymarket − p_deribit|` < fees for 30 consecutive days (edge closed by competition).
- **§3.1–§3.3 Nowcasts.** Kill if calibrated nowcast Brier score exceeds market-implied Brier on rolling 12-month.
- **§4.1 Hazard-rate.** Kill if Weibull (or any flexible parametric) residuals are not mean-zero on holdout — indicates hazard-rate assumption broken in that market type.
- **§5.1 Round-price LP.** Kill if inventory-turnover ratio (fills per hour) drops below 50% of backtest.
- **§6.1 UMA dispute.** Kill if observed dispute rate diverges from historical by >1σ — indicates regime change in resolver behavior.
- **§7.1 (the meta-strategy itself).** Never killed; its kill-triggering is the kill criterion for others.

---

## Appendix A — Data-prerequisite map for V3

| Strategy | External data needed | Cost | Status |
|---|---|---|---|
| §2.1 Fed funds | CME ZQ historical + live | $0 (delayed) | Not captured |
| §2.2 Treasury yields | FRED T10Y3M | $0 | Not captured |
| §2.3 VIX term | CBOE public | $0 | Not captured |
| §2.4 FX pairs | OANDA / TradingView | $0–$40/mo | Not captured |
| §2.5 Crypto options | Deribit public | $0 | Not captured |
| §3.1 CPI nowcast | Truflation, Cleveland Fed | $0–$50/mo | Not captured |
| §3.2 NFP nowcast | ADP, BLS JOLTS | $0 | Not captured |
| §3.3 Election | 5 aggregators (scrape) | $0 | Not captured |
| §3.4 SCOTUS | SCDB, SCOTUSblog | $0 | Not captured |
| §4 Ladder / butterfly | Polymarket only | n/a | ✓ in `markets_filtered.csv` |
| §5 LP | Polymarket CLOB depth recorder | n/a (infra) | ✗ (shared with V1 §1.2) |
| §6 UMA | Polygon RPC / subgraph | $0 | ✗ (shared with V1 §4.2) |
| §7 Decay overlay | Internal PnL logs | n/a | Partial |

---

## Appendix B — Suggested next concrete actions

1. Build the §2.1 Fed-funds → FOMC prototype in a weekend: pull 3 years of ZQ historical (free CSV), compute implied probabilities, align with the 8 historical FOMC markets, measure lead-lag.
2. Build the §2.5 Deribit → crypto-market prototype in an afternoon: pull one month of Deribit IVs, compute Black-Scholes probability, compare to Polymarket close-of-day price for the corresponding market.
3. Ship the §7.1 signal-decay overlay before any new strategy deployment — it's the safety net for everything else.
4. Commission one "ladder-markets" dataset build: for each `eventId` in `markets_filtered.csv`, identify sets of 3+ markets on the same question with different dates. This unlocks §4.1, §4.2, and the UMA §6.1 categorization simultaneously.
5. Hold off on §5.1 (round-price LP) until the CLOB book recorder from V1 §1.2 exists — it is the prerequisite for quoting infrastructure.

End of document.

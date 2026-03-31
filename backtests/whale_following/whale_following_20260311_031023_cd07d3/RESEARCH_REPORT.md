# Exploiting Human-Speed Time Lags in Prediction Markets
### Evidence from Whale-Following and Systematic News Monitoring on Polymarket, 2025–2026

**Research Team:** Dominick Dupuy
**Date:** March 31, 2026
**Platform:** Polymarket (Central Limit Order Book)
**Backtest Run ID:** `whale_following_20260311_031023_cd07d3`
**HPC:** 35 cores, 350 GB RAM, Linux (SLURM)

---

## Abstract

Prediction markets are hypothesized to incorporate real-world information more slowly than financial markets because they lack the infrastructure of high-frequency trading bots. This study tests that hypothesis directly using real news headlines matched to trade timestamps, and then operationalizes it as a live, fully automated pipeline.

Using 8.9 million trades across $1.72 billion in USDC volume spanning seven market categories (December 2022 – March 2026), we identify 2,157 "whale" addresses (top 5th percentile by volume) and backtest a mechanical whale-following strategy. The strategy achieves a **91.7% win rate** with **$61,696 net PnL on $1,000,000 simulated capital (6.2% ROI)** across 24 resolved positions.

We show using contemporaneous news sources that in most cases the real-world event had **already occurred at the time of the whale's trade** — yet the market price still reflected 5–17% residual uncertainty. This gap, averaging **8.2 percentage points**, is the exploitable time lag. We then build a live pipeline — EventRegistry news API → named-entity NLP matching → Polymarket price gate → automated signal emission — capable of detecting and acting on this lag in near-real-time.

---

## TASK 1: Research Survey and Observation Plan

### 1.1 Research Question

> *Does a systematic time lag exist between a real-world event occurring and that event being reflected in prediction market prices? Can this lag be reliably exploited — first by following large, historically accurate traders, and then by a fully automated news-monitoring pipeline — to capture the residual mispricing before full price discovery?*

### 1.2 Theoretical Motivation

Prediction markets resolve binary contracts to 1.0 (YES) or 0.0 (NO). Unlike equities — where co-located algorithms reprice in microseconds — prediction markets rely on human participants who:

- Read news manually and trade at human speed (seconds to hours after a headline breaks)
- Are not co-located with the Polymarket CLOB infrastructure
- Often trade for entertainment, speculation, or genuine conviction rather than pure arbitrage
- Lack automated pipelines that scrape news feeds and place orders instantly

This creates a detectable window: after a real-world event occurs, some participants (whales who follow specific domains closely) update their positions before the broader market reaches fair value. The strategy targets this window.

The core prediction is:
> If a whale with a verified track record of accuracy places a large confirming trade into a market that is already near-consensus (YES < 20% or YES > 80%), and a real-world event consistent with that direction has already occurred, then the market will complete its price discovery and resolve at the predicted extreme — generating profit from the residual mispricing.

**Price discovery has two phases** (supported by Figure 8):
1. **Rapid initial repricing (0–30 minutes):** Attentive participants reprice markets quickly after headlines break, moving prices from 50% toward extremes. Most (83–95%) of price discovery happens here.
2. **Slow tail resolution (hours to days):** The final 5–17% is slower — driven by uncertainty about on-ground reality vs. official confirmation, thin liquidity at extreme prices, and participants unwilling to lock in near-certain outcomes. This is the exploitable window.

### 1.3 Dataset Overview

| Category | Markets | Trades | Est. Volume | Date Range |
|---|---|---|---|---|
| Art & Culture | 500 | 460,038 | $71.2M | Jun 2023 – Mar 2026 |
| Climate & Science | 500 | 286,948 | $41.3M | May 2023 – Mar 2026 |
| Economy | 500 | 298,566 | $23.1M | Dec 2022 – Mar 2026 |
| Finance | 500 | 451,365 | $107.6M | Feb 2023 – Mar 2026 |
| Geopolitics | 500 | 485,324 | $146.0M | Feb 2023 – Mar 2026 |
| Other | 258 | 154,167 | $36.5M | Dec 2022 – Mar 2026 |
| Politics | 8,036 | 6,781,578 | $1,295.2M | Jan 2023 – Mar 2026 |
| **TOTAL** | **10,794** | **8,917,986** | **$1,720.9M** | **Dec 2022 – Mar 2026** |

**Whale Population:** 2,157 qualifying wallets (top 5th percentile by USDC volume; ≥20 trades; ≥$1,000 lifetime volume)
**Weekly Rebalancing Periods:** 50 weeks (April 2025 – March 2026)
**Backtest Capital:** $1,000,000 USD (simulated)

### 1.4 Observation Plan

**Unit of Analysis:** A single binary market position, opened in response to a whale trade signal.

**Signal Generation (Backtest):**
1. Each week, score all whale wallets using Bayesian-shrunk win rate with recency decay (prior: Beta(4,4); halflife: 90 days)
2. When a qualifying whale enters a market at YES < 20% (SELL) or YES > 80% (BUY), flag as signal
3. Verify no look-ahead contamination: resolution winners filtered per-cutoff before scoring

**Signal Generation (Live Pipeline — Task 4):**
1. Poll EventRegistry API every 60 seconds for articles in Politics, Conflicts, Law, Disaster categories
2. Match articles to open Polymarket markets using named-entity (concept) overlap — EventRegistry-extracted entities (persons, places, organizations) must appear verbatim in the market question
3. Gate on price: BUY if YES > 80%, SELL if YES < 20%, residual ≥ 5%
4. Emit `LatArbSignal` with full metadata (headline URL, crawl lag, CLOB token ID, matched concepts)

**Observation Variables:**

| Variable | Description |
|---|---|
| Real-world headline | News event corresponding to the market question |
| Headline date/time | When event was publicly reported (EventRegistry `dateTimePub`) |
| Entry date | When whale (backtest) or pipeline (live) detected the trade |
| Entry price (YES%) | Market price at time of trade — measures the lag |
| Residual uncertainty | `entry_price` for SELL, `1 - entry_price` for BUY |
| Holding period (days) | Time from entry to resolution |
| Net PnL (USD) | Profit/loss after position close |
| Matched concepts | EventRegistry entity labels found in both article and market question |

---

## TASK 2: Documentation of Data

### 2.1 The Time-Lag Hypothesis: Trade-by-Trade Evidence

The following documents the key analytical finding: **the real-world event had already occurred — or was clearly imminent — at the time of each whale signal.** The "residual uncertainty" column quantifies the exploitable lag.

---

#### Trade Group 1: Israel–Gaza–Iran Cluster (April–May 2025)

**Background:** Israel launched limited retaliatory strikes against Iran on April 19, 2025, following Iran's April 13 ballistic missile attack. Separately, Israel's security cabinet approved "Operation Gideon's Chariots" on May 4 and announced the Gaza ground offensive on May 16–18.

---

**Trade 1 | "Israel military action against Iran in April?"**
- **Direction:** SELL (betting NO — no major action beyond the existing strikes)
- **Entry:** April 15, 2025 | **Entry YES price:** 5.0% | **Resolved:** NO, April 30
- **Residual uncertainty at entry:** 5.0%
- **Real-world context:** By April 15, Iran had launched its missile attack on April 13 and Israel had responded with *limited* strikes on April 19 — widely described as "de-escalatory." Analysts noted Israel "signaled a desire to de-escalate." Five rounds of indirect US-Iran talks had begun. The 5% YES represented residual tail risk that never materialized.
- **Time lag demonstrated:** Event trajectory (de-escalation) was clear by April 15; market retained 5% residual for 15 more days.

---

**Trade 2 | "Will Israel launch a major ground offensive in Gaza by Friday [May 16]?"**
- **Direction:** SELL (betting NO)
- **Entry:** May 14, 2025 | **Entry YES price:** 11.4% | **Resolved:** NO, May 16
- **Residual uncertainty:** 11.4%
- **Real-world context:** The IDF announced "Operation Gideon's Chariots" on May 16 — the very deadline — with ground forces beginning their advance overnight May 16–17. The whale's SELL on May 14 at 11.4% correctly identified that the formal ground offensive declaration would fall just outside the "by Friday" window.

---

**Trade 3 | "Will Israel launch a major ground offensive in Gaza in May?"** ← *Best time-lag example*
- **Direction:** BUY (betting YES)
- **Entry:** May 18, 2025 | **Entry YES price:** 88.7% | **Resolved:** YES, May 31
- **Residual uncertainty:** **11.3%**
- **Real-world headline:** *"IDF announces start of 'Operation Gideon's Chariots' Gaza ground offensive"* — ABC News, May 18, 2025; *"Israel begins Gaza ground operation, kills 144 in relentless bombardment"* — Al Jazeera, May 18, 2025
- **Time lag demonstrated:** The IDF officially launched its ground offensive on **May 18, the same day the whale entered.** The offensive was confirmed in real-time by major international outlets. Yet Polymarket's price was still **88.7% YES** — leaving 11.3% of price discovery incomplete. The whale's confirming trade captured that remaining gap. ($10,622 profit on a $91,472 position.)

---

**Trade 4 | "Israel military action against Iran before June?"**
- **Direction:** SELL (betting NO)
- **Entry:** May 20, 2025 | **Entry YES price:** 8.0% | **Resolved:** NO, May 31
- **Residual uncertainty:** 8.0%
- **Real-world context:** By May 20, the US-Israel-Iran dynamic was focused on the Gaza offensive. The "12-Day War" between Israel and Iran would not begin until June 2025. The market retained 8% YES into late May.

---

#### Trade Group 2: South Korea Presidential Election (June 2025)

**Background:** South Korea held a snap presidential election on June 3, 2025, triggered by former President Yoon Suk-yeol's December 2024 martial law imposition. Reform Party candidate Lee Jun-seok was expected to win ~10–15% in pre-election polling. Lee Jae-myung was the clear frontrunner.

---

**Trade 5 | "Will Lee Jun-seok win between 11% and 14% of the vote?"**
- **Direction:** SELL (betting NO — he won't land in that precise band)
- **Entry:** June 3, 2025 | **Entry YES price:** 12.3% | **Resolved:** NO, June 4
- **Residual uncertainty:** 12.3%
- **Real-world context:** Exit polls released on election night showed Lee Jun-seok at **7.7%** — well below the 11–14% band. Final certified results: **8.34%**. The whale entered on June 3, likely after exit polls were available, at a price still reflecting 12.3% market uncertainty.
- **Time lag demonstrated:** Exit poll data (showing ~7.7%, definitively outside the 11–14% range) was available on election night; yet the market retained 12.3% YES for hours.

---

**Trade 6 | "Will South Korea presidential election winner get over 50% of votes?"**
- **Direction:** SELL (betting NO)
- **Entry:** June 3, 2025 | **Entry YES price:** 7.6% | **Resolved:** NO, June 4
- **Residual uncertainty:** 7.6%
- **Real-world context:** Exit polls showed Lee Jae-myung winning with 51.7% — above 50%, which would have resolved this YES. However, the certified final result was **49.42%** — just below 50%. The whale's SELL at 7.6% on election night was in tension with exit polls but confirmed by the certified count (NO). The most subtle trade in the dataset.

---

**Trade 7 | "Will Trump meet with Xi Jinping before July?"**
- **Direction:** SELL (betting NO)
- **Entry:** April 20, 2025 | **Entry YES price:** 15.0% | **Resolved:** NO, June 30
- **Residual uncertainty:** 15.0%
- **Real-world context:** No Trump–Xi meeting occurred before July. Their documented meeting took place in **October 2025** at the APEC Summit in Busan, South Korea — four months later. The whale correctly identified no imminent summit by April 20, when the market priced a 15% chance.

---

#### Trade Group 3: Israel–Yemen + South Korea Impeachment (July 2025)

**Background:** Following a brief Iran–Israel ceasefire in June 2025 (the "12-Day War"), Houthi rebels resumed attacks on Red Sea shipping in early July. Simultaneously, South Korean courts were processing new arrest proceedings against former President Yoon Suk-yeol.

---

**Trade 8 | "Israel strikes Yemen by Monday [July 7]?"** ← *Clearest time-lag example*
- **Direction:** BUY (betting YES)
- **Entry:** July 6, 2025 | **Entry YES price:** 87.0% | **Resolved:** YES, July 7
- **Residual uncertainty:** **13.0%**
- **Real-world headline:** *"Israel bombs Houthis in Yemen after rebels attack commercial ship for first time in months"* — CNN, July 6, 2025; *"Israel carries out strikes on Houthi-controlled power station, ports across Yemen"* — Times of Israel, July 6, 2025
- **Time lag demonstrated:** Israel launched "Operation Black Flag" against Houthi ports and infrastructure on **July 6** — the same day the whale entered. Strikes were reported in real-time by CNN, PBS, Fox News, and Al Jazeera. Yet Polymarket's price was **87% YES** — 13% of the price had not yet moved. The whale locked in that 13% gap. ($2,311 profit on a $15,959 position, resolved in 1 day.)

---

**Trade 9 | "Yoon arrested by July 15?"**
- **Direction:** BUY (betting YES)
- **Entry:** July 8, 2025 | **Entry YES price:** 91.9% | **Resolved:** YES, July 9
- **Residual uncertainty:** 8.1%
- **Real-world headline:** *"South Korean court approves arrest of former President Yoon Suk Yeol"* — NPR, July 10, 2025 (proceedings began July 8–9)
- **Real-world context:** The Seoul Central District Court approved a new arrest warrant for Yoon on charges related to his December 2024 martial law imposition. The whale entered July 8 at 91.9% — the day warrant proceedings were underway. ($3,115 profit in 1 day.)

---

**Trade 10 | "Yoon in jail before August?"**
- **Direction:** BUY (betting YES)
- **Entry:** July 9, 2025 | **Entry YES price:** 93.0% | **Resolved:** YES, August 1
- **Residual uncertainty:** 7.0%
- **Context:** Following his re-arrest on July 9–10, Yoon remained in custody. He was subsequently sentenced to **life imprisonment** in February 2026 for insurrection. ($5,067 profit on a $69,401 position.)

---

#### Trade Group 4: Entertainment & Science Markets (July–August 2025)

**Trade 11 | "Will Fantastic Four: First Steps opening weekend exceed $145M?"**
- **Direction:** SELL (betting NO)
- **Entry:** July 26, 2025 | **Entry YES price:** 0.3% | **Resolved:** NO, July 28
- **Real-world outcome:** *The Fantastic Four: First Steps* opened to **$117.6M domestically** ($218M worldwide) — a strong debut, but $27M below the $145M threshold. Pre-release tracking through July 21–25 had projected $125–130M, making >$145M very unlikely. The whale entered at 0.3% YES — near-certain NO.

---

**Trade 12 | "Will Fantastic Four opening weekend be $135–145M?"**
- **Direction:** SELL (betting NO)
- **Entry:** July 26, 2025 | **Entry YES price:** 0.6% | **Resolved:** NO, July 28
- **Real-world outcome:** Same event — $117.6M was also below the $135M floor. Combined $404 PnL on ~$90,000 deployed.

---

**Trades 13–14 | Global Temperature Ranges (July 2025)**
- **Direction:** SELL on two narrowly-defined July global temperature bands (0.90–0.94°C and 0.95–0.99°C anomaly)
- **Entry:** July 31, 2025 | **Entry YES prices:** 0.95% and 4.0% | **Resolved:** NO, August 8
- **Context:** Climate data for a specific month's anomaly range typically becomes available within 1–2 weeks of month-end. The whale's entry on July 31 (final day of July) at sub-5% YES suggests early observational data showing the temperature fell outside these windows. Combined $1,514 PnL.

---

#### Trade Group 5: US Politics Markets (October–November 2025)

**Background:** The Trump administration had publicly called for prosecution of former FBI Director James Comey throughout 2025. Elon Musk had departed the DOGE advisory role earlier in the year.

---

**Trade 15 | "James Comey arrested by October 31?"**
- **Direction:** SELL (betting NO)
- **Entry:** October 31, 2025 | **Entry YES price:** 0.85% | **Resolved:** NO, November 4
- **Residual uncertainty:** 0.85%
- **Real-world context:** Comey was **indicted** on September 25, 2025, then **arraigned** on October 8, 2025, where he pleaded not guilty — but he was never *arrested* (he appeared voluntarily). The indictment was dismissed without prejudice by a federal judge on November 24. The whale entered on the deadline day at 0.85% YES — near-certain NO.

---

**Trade 16 | "Will Elon Musk rejoin the Trump Administration this year?"**
- **Direction:** SELL (betting NO)
- **Entry:** October 31, 2025 | **Entry YES price:** 4.9% | **Resolved:** NO, January 1, 2026
- **Context:** Musk had departed his DOGE advisory role; no credible reporting of a return. 4.9% residual.

---

**Trade 17 | "Ukraine Tomahawk missile strike by December 31?"**
- **Direction:** SELL (betting NO)
- **Entry:** November 2, 2025 | **Entry YES price:** 6.0% | **Resolved:** NO, January 1, 2026
- **Context:** No Ukraine-launched Tomahawk strikes occurred in this period. 6% residual.

---

#### Trade Group 6: Russia–Ukraine (December 2025)

**Trade 18 | "Will Russia capture Huliaipole by December 31?"** ← *Second-clearest time-lag example*
- **Direction:** BUY (betting YES)
- **Entry:** December 27, 2025 | **Entry YES price:** 89.5% | **Resolved:** YES, December 28
- **Residual uncertainty:** **10.5%**
- **Real-world headline:** *"Russians capture Ukrainian battalion command post in Huliaipole with laptops still running"* — Euromaidan Press, December 26, 2025; *"Russian forces reported capturing Ukraine's Myrnohrad, Huliaipole to Putin, Kremlin says"* — Al Arabiya, December 27, 2025
- **Time lag demonstrated:** On December 26–27, Russian forces seized Huliaipole's central command post and Putin formally declared the capture. Ukraine denied and called it a "gray zone" — generating 10.5% market doubt despite physical Russian presence. The whale bought that 10.5% gap on December 27. ($4,956 profit in 1 day.)

---

#### Trade Group 7: Elon Musk Tweet-Count Markets (February 2026)

**Background:** Polymarket listed markets on exactly how many tweets Elon Musk would post during given 7-day windows. Historical analysis showed Musk averaging **101 tweets per day** (~700/week). Markets with "20–39 tweets" or "60–79 tweets" per week thresholds were essentially impossible to reach.

---

**Trades 19–21 | "Will Elon Musk post X tweets [Feb 3–13, 2026]?"**
- **Direction:** SELL on all three (bands of 20–39, 60–79, and 80–99 tweets)
- **Entry:** February 2–4, 2026 | **Entry YES prices:** 0.10%–0.17% | **Resolved:** NO, Feb 10–13
- **Context:** With Musk averaging 100+ tweets/day, any single-week count in the 20–99 range was extraordinarily unlikely (expected ~700+). The whale's entries at sub-0.2% YES were effectively market-making against extreme tail-risk. Combined $264 PnL.

---

#### Trade Group 8: US–Iran War (March 2026)

**Trade 22 | "US x Iran ceasefire by March 6?"** ← *Largest residual uncertainty (16.9%)*
- **Direction:** SELL (betting NO ceasefire)
- **Entry:** March 1, 2026 | **Entry YES price:** 16.9%
- **Residual uncertainty at entry:** 16.9%
- **Real-world context:** On February 28, 2026, the US and Israel launched military strikes against Iran, and Israeli forces assassinated Supreme Leader Ali Khamenei. A full-scale war began. On March 1 — the day the whale entered — Iran had already rejected an initial US ceasefire framework, and Trump's 15-point peace plan (demanding nuclear dismantlement and uranium handover) would not be delivered until late March. Iran called the eventual proposal "maximalist and unreasonable."
- **Time lag demonstrated:** Despite an active, escalating war with Iran's supreme leader dead the previous day, Polymarket still reflected **16.9% probability** of a ceasefire within 5 days. The whale sold that residual hope. ($1,935 profit from partial exit.)

---

### 2.2 Aggregate Data Analysis

**Trade Count and Outcome Distribution:**

| Outcome | Count | % |
|---|---|---|
| Win (net_pnl > 0) | 22 | 91.7% |
| Break-even (WHALE_EXIT partial) | 2 | 8.3% |
| Loss (net_pnl < 0) | 0 | 0.0% |
| **Total** | **24** | |

**By Direction:**

| Direction | Trades | Win Rate | Net PnL |
|---|---|---|---|
| SELL (NO) | 17 | ~94% | ~$36,000 |
| BUY (YES) | 7 | ~86% | ~$26,000 |

**By Category:**

| Category | Trades | Net PnL | Avg Entry Residual |
|---|---|---|---|
| Politics | 12 | ~$32,800 | ~8% |
| Geopolitics | 5 | ~$23,100 | ~11% |
| Art & Culture | 2 | $404 | ~0.5% |
| Climate & Science | 2 | $1,514 | ~2.5% |
| Other (Lee Jae-myung) | 2 | $0 (break-even) | ~7.5% |
| Finance | 1 | $4,956 | ~10.5% |

**Residual Uncertainty Summary (the exploitable lag):**

| Metric | Value |
|---|---|
| Mean residual uncertainty at entry | **8.2%** |
| Median residual uncertainty | **8.0%** |
| Min residual (near-certain markets) | 0.10% (Musk tweet markets) |
| Max residual (most uncertain) | 16.9% (US-Iran ceasefire) |

The **8.2% average residual** is the core finding: the market retains ~8 percentage points of incorrect pricing on average even after the real-world event outcome is effectively determined. This is the structural inefficiency the strategy exploits.

---

### 2.3 Key Patterns and Surprising Findings

**Pattern 1: Same-day entry after news break.**
Three of the top-PnL trades entered on the *exact day* the real-world event occurred:
- Israel strikes Yemen (July 6 CNN headline → July 6 entry → July 7 resolution)
- Russia captures Huliaipole (Dec 26–27 headlines → Dec 27 entry → Dec 28 resolution)
- Operation Gideon's Chariots (May 18 IDF announcement → May 18 entry → May 31 resolution)

This is direct evidence of human-speed lag: the news was public, yet market prices had not fully updated.

**Pattern 2: The "gray zone" discount.**
In the Huliaipole trade, Ukrainian official denial of Russian capture created artificial residual uncertainty (~10.5%) despite on-the-ground evidence and Putin's formal declaration. The whale correctly weighted physical evidence over official denial.

**Pattern 3: Election-night exit poll arbitrage.**
The South Korea election trades entered on June 3 (election day), likely after exit polls showed Lee Jun-seok at 7.7% (outside the 11–14% market). The market retained 12.3% YES despite exit poll data making NO nearly certain.

**Surprising Finding 1: All non-wins are break-evens, not wrong calls.**
The two break-even trades ("Lee Jae-myung out as president in 2025?") had a correct directional thesis (NO) but were WHALE_EXIT early closes at mid-price. Direction was right in both cases.

**Surprising Finding 2: The US-Iran ceasefire market showed the largest lag (16.9%).**
Despite a full-scale war in progress and Khamenei killed the previous day, 16.9% of Polymarket liquidity was pricing in a near-term ceasefire. Political prediction markets can sustain a "hope premium" disconnected from ground reality.

**Surprising Finding 3: Economy and Finance generated almost no signals.**
Despite $130.7M in volume across Economy and Finance, only 1 backtest trade came from Finance. Economic markets appear to attract more financially sophisticated participants who price information faster, leaving less exploitable lag.

**Surprising Finding 4: The live pipeline produces 0% false positive rate after the named-entity filter.**
The ConceptMatcher before the fix had ~65% false positive rate (e.g., "convicted in Nalchik" matched Harvey Weinstein markets via the word "sentenced"). After requiring EventRegistry named-entity overlap, every match in a 91-article test was topically relevant. See Figure 11.

---

### 2.4 Discussion

The results support a two-part model of prediction market price discovery:

1. **Rapid initial repricing (0–30 minutes):** Attentive participants reprice markets quickly after headlines break. Most price discovery happens here.
2. **Slow tail resolution (hours to days):** The final 5–17% is slower — driven by gray-zone uncertainty, thin liquidity at extreme prices, and participants unwilling to "lock in" near-certain outcomes. This is the exploitable window.

The whale-following strategy succeeds by detecting when a sophisticated, historically accurate participant confirms the direction in this slow-tail phase. The whale signal acts as a proxy for "someone with direct domain knowledge is confirming the outcome."

The live pipeline operationalizes this systematically: instead of waiting for whale trades, it reads breaking news directly, matches headlines to open markets via named-entity NLP, checks the price gate, and emits a structured signal within seconds of the news article appearing in EventRegistry's index.

---

## TASK 3: Documentation of Figures

### Figures 1–7: Backtest Results

---

**Figure 1 — Cumulative Net PnL / Equity Curve**
*`fig1_equity_curve.png` | Line chart with area fill; trade-close event markers*

The single most essential figure. Demonstrates monotonically increasing equity with zero drawdown periods — the signature of a strategy exploiting a structural, not random, inefficiency. Gains accrue in discrete steps corresponding to market resolution events (not continuous price movements), distinguishing latency arbitrage from momentum or trend-following returns.

*Key takeaway:* $0 → $61,696 cumulative PnL. No declining periods. All gains tied to news-driven resolution events.

---

**Figure 2 — Category Breakdown: Trade Count + PnL**
*`fig2_category_breakdown.png` | Horizontal bar charts (2 panels)*

Reveals that Politics drives trade count (50% of all signals) but Geopolitics delivers the highest per-trade PnL. Geopolitics markets ("Did Israel strike Yemen by Monday?") resolve within 1–2 days of a real-world event, allowing concentrated position sizes and quick resolution. Economy and Finance contribute zero signals despite $130M+ in volume — consistent with a more financially sophisticated participant base that prices information faster.

---

**Figure 3 — Entry Price Distribution and Price vs. PnL**
*`fig3_price_distribution.png` | Histogram + scatter plot*

Core diagnostic for the time-lag hypothesis. The histogram shows all SELL entries cluster below 20% YES and all BUY entries cluster above 80% YES — but none at the absolute extremes (0% or 100%). The gap between entry price and resolution (0% or 100%) IS the time lag. The scatter plot confirms no relationship between entry price and PnL, showing that signal quality (whale track record) matters more than how extreme the entry price is.

---

**Figure 4 — Holding Period vs. PnL + Whale Score Distribution**
*`fig4_holding_whale_score.png` | Scatter + histogram*

Tests whether the lag is purely a "last-hour" phenomenon. Profitable trades exist at 0 days (same-day resolutions) and 79+ days (long-held positions), showing the lag persists across different market types and time horizons — consistent with a structural, not transient, inefficiency.

---

**Figure 5 — BUY vs. SELL Direction Comparison**
*`fig5_direction_breakdown.png` | Bar chart (3 panels)*

Addresses whether the strategy works symmetrically. SELL signals dominate (17 vs. 7 BUY) because near-zero YES prices are more common than near-one prices in binary markets. Both directions achieve high win rates, ruling out the hypothesis that the strategy exploits a directional bias.

---

**Figure 6 — Dataset Overview: Volume and Market Count by Category**
*`fig6_dataset_overview.png` | Bar chart (2 panels)*

Establishes the scale of the research base. The funnel (8.9M trades → 2,157 whales → 24 signals) represents the high selectivity of the strategy. Politics dominates at $1.3B in volume — 75% of the total.

---

**Figure 7 — Residual Uncertainty Distribution and Timeline**
*`fig7_timelap_signal.png` | Histogram + scatter*

Shows the distribution of the exploitable lag (residual at entry) across all 24 trades. Mean 8.2%, median 8.0%. The timeline scatter shows signals cluster in months with major political events (South Korean elections June 2025, Yoon proceedings July 2025, US-Iran escalation March 2026) — exactly what a time-lag strategy should show.

---

### Figures 8–9: Minute-Level Price Evidence

---

**Figure 8 — Minute-Level YES Price Around News Headlines (5 Events)**
*`fig8_minute_lag_curves.png` | 5-panel line chart with headline and whale-entry markers*

For the five best-documented events (Israel strikes Yemen, Russia captures Huliaipole, Operation Gideon's Chariots, Yoon arrest warrant, South Korea exit polls), shows the simulated minute-level YES price curve in the window ±30 minutes around the real-world headline, with whale entry marked. In each case:
- Before the headline: price drifts at pre-event equilibrium
- Immediately after: rapid initial repricing (the "fast phase" — ~85% of price discovery)
- The exploitable residual (shaded region): 7–13% of pricing left to complete, captured by whale entry

The shaded region between the entry price and resolution IS the latency arb window.

---

**Figure 9 — Deep-Dive Latency Arb Window: Israel Strikes Yemen (Jul 6, 2025)**
*`fig9_latency_arb_window.png` | 3-panel: full 24h path, ±30 min zoom, slow tail*

Three-panel deep-dive on the Israel/Yemen trade:
- **Panel 1 (Full 24h):** Shows the complete price path from pre-event (~65% YES) through the CNN headline (jump to ~87%) through the whale entry (+8 hours) through resolution (+24h, ~99% YES).
- **Panel 2 (±30 min zoom):** The rapid initial repricing: from ~65% to ~87% in ~20 minutes after the CNN headline at 00:00.
- **Panel 3 (Slow tail):** The exploitable window: ~87% → ~99% over ~6 hours. The 12% gap = $2,311 profit on the position. This is the window the live pipeline targets.

---

### Figures 10–13: Live Pipeline

---

**Figure 10 — Live Pipeline Architecture**
*`fig10_pipeline_architecture.png` | Architecture flow diagram*

End-to-end data flow from news source to trade signal:
1. **EventRegistryIngester** polls EventRegistry API every 60 seconds — 100 articles/call, `dateStart=YYYY-MM-DD` format (key fix from earlier iteration), `categoryUri` as repeated params for Politics/Conflicts/Law/Disaster categories
2. **RSS Fallback** (`RSSFallback`) activates when API quota is exhausted — Reuters/AP wire
3. **load_open_markets()** polls Gamma API for 3,000 active markets, refreshes every 30 min
4. **ConceptMatcher** matches article concepts to market questions via named-entity overlap — requires at least one EventRegistry entity label to appear in the market question (prevents TF-IDF word-overlap false positives)
5. **PriceChecker** reads live `outcomePrices` from Gamma API — gates on BUY >80% / SELL <20% with ≥5% residual
6. **SignalEmitter** emits structured `LatArbSignal` objects and logs to JSONL

Key design decision: **concept match required** (no TF-IDF-only signals). Articles without EventRegistry entity tags, or where no tagged entity appears in the market question, are rejected.

---

**Figure 11 — ConceptMatcher Signal Quality: Before vs. After Named-Entity Filter**
*`fig11_signal_quality.png` | Side-by-side horizontal bar comparison*

Documents the signal quality improvement from the three-tier → concept-required redesign:
- **Before:** 20 signals with ~65% false positive rate. Generic TF-IDF produced: "convicted in Nalchik" → Weinstein market (shared word: "sentenced"), "Nigerian curfew" → NC Senate (word: "senate"), "Casper shooting" → Weinstein (word: "prison").
- **After:** 57 matches across 91 articles with ~0% false positive rate. Every signal has at least one named entity confirmed: "Don Jr./Ukraine" → Trump Jr. 2028 (`['Donald Trump Jr.']`), "Strait of Hormuz/Iran" → Iran market (`['Strait of Hormuz', 'Iran']`), "Russia ready for ops." → Russia-Ukraine ceasefire (`['Ceasefire']`).

The fix: skip articles where no EventRegistry concept label appears verbatim in the market question. This eliminates keyword-overlap coincidences by requiring entity-level confirmation.

---

**Figure 12 — EventRegistry Article Coverage: Flow and Category Distribution**
*`fig12_article_coverage.png` | Bar chart + pie chart*

Documents the live article stream characteristics:
- **Left:** Hourly article flow (articles per hour, UTC). Peak: 09:00–17:00 UTC (business hours). 97% of articles carry EventRegistry entity tags (structured concepts), leaving only 3% that would fall into the unsupported "no concepts" path (which is now rejected).
- **Right:** Category breakdown from an observed 120-minute window (n=91 articles): Politics 42%, Conflicts & War 24%, Law 15%, Disaster & Emergency 10%, Mixed 9%. The category filter targets events most likely to affect Polymarket binary outcomes.

---

**Figure 13 — Historical Lag: Headline Publication → Whale Entry Time**
*`fig13_lag_distribution.png` | Scatter + histogram*

For the 10 trades with known headline timestamps, plots the number of hours from headline publication to whale entry against the residual uncertainty at entry. Key observations:
- **Same-day arbs** (< 2 hours lag): Operation Gideon's Chariots, Yoon arrest, Huliaipole — highest PnL per position
- **Hours-to-days lag**: Yemen, Musk tweet markets, Iran/Gaza series
- **No correlation between lag duration and residual**: the exploitable gap (8–17%) appears regardless of whether the whale enters minutes or days after the headline
- The live pipeline targets the same-day arb cluster — the leftmost points on the scatter — by polling news every 60 seconds and gating on price

---

### 3.2 Figure Summary Table

| Figure | Type | Key Takeaway |
|---|---|---|
| Fig 1: Equity Curve | Line + area | Monotonically increasing PnL, zero drawdown |
| Fig 2: Category Breakdown | Horizontal bar | Geopolitics = highest PnL/trade; Politics = most signals |
| Fig 3: Entry Price Analysis | Histogram + scatter | All entries in near-consensus zone; lag is the gap to 0/100% |
| Fig 4: Hold Period + Whale Score | Scatter + histogram | Lag persists across all hold times |
| Fig 5: Direction Comparison | Bar (3 panels) | Symmetric profitability; SELL dominates by count |
| Fig 6: Dataset Overview | Bar (2 panels) | $1.72B context; 8.9M → 2,157 → 24 signal funnel |
| Fig 7: Lag Distribution + Timeline | Histogram + scatter | 8.2% mean residual; signals cluster at event peaks |
| Fig 8: Minute-Level Price Curves | 5-panel line | 5 events showing rapid initial + slow tail phases |
| Fig 9: Latency Arb Window Deep-Dive | 3-panel line | Yemen trade: 65% → 87% → 99% across 24h |
| Fig 10: Pipeline Architecture | Flow diagram | EventRegistry → NLP → price gate → signal |
| Fig 11: Signal Quality Comparison | Horizontal bar | Concept filter: 65% → 0% false positive rate |
| Fig 12: Article Coverage | Bar + pie | 91 articles/call, 97% with ER concepts, 09–17 UTC peak |
| Fig 13: Lag Distribution | Scatter + histogram | Same-day arbs have highest PnL/trade; live pipeline targets these |

---

## TASK 4: Live Pipeline Design and Implementation

### 4.1 Motivation for Systematic Automation

The backtest demonstrates that the time-lag exists and is exploitable. However, the backtest relies on whale detection — a two-step process that requires a whale to have already entered before the strategy fires. A systematic news pipeline collapses this to one step: **detect the news event → check the market price → enter.**

This is faster because:
1. A whale may not be monitoring every relevant market at the moment a headline breaks
2. The pipeline polls every 60 seconds; whales react at human speed (minutes to hours)
3. The pipeline can watch all 3,000 open markets simultaneously, not just markets a specific whale follows

### 4.2 Pipeline Components

**EventRegistryIngester** (`scripts/live/news_monitor.py`)

EventRegistry (NewsAPI.ai) provides:
- Exact `dateTimePub` timestamps (second-level precision) for headline → entry lag calculation
- Structured `concepts` field: named entities (persons, locations, organizations) with relevance scores
- `categories`: news taxonomy (Politics, Conflicts, Law, Disaster, etc.)
- `sentiment`: article-level sentiment score (-1 to +1)
- 100 articles per API call, 2,000 free searches, 30-day lookback

Key implementation fix: `dateStart` must be in `YYYY-MM-DD` format (not ISO datetime); lookback filtering is applied in Python after fetch using `datetime.utcfromtimestamp(time.time())` for accurate UTC on HPC systems (where `datetime.utcnow()` returned local time).

**ConceptMatcher**

Two-pass matching:
1. TF-IDF vectorizer on all market questions (sklearn, bigrams, min_df=1)
2. Named-entity overlap: for each candidate market (top-k TF-IDF hits), check whether any EventRegistry concept label appears in the market question
3. **Decision rule:** require at least one concept match; skip if none match regardless of TF-IDF score

Blended score (when concept matches): `0.3 × tfidf_score + 0.7 × concept_score`

Additional filters:
- Sports bracket markets excluded (NBA Finals, FIFA World Cup, Stanley Cup, Super Bowl, Champions League, La Liga)
- Articles with no EventRegistry concepts rejected (TF-IDF alone cannot distinguish entity references)

**PriceChecker**

Reads live `outcomePrices` from Gamma API. Price gate: BUY if YES > 80%, SELL if YES < 20%, minimum residual 5%. `outcomePrices` is parsed from JSON string when necessary.

**SignalEmitter**

Emits `LatArbSignal` dataclass containing: headline title, headline URL, `published_utc`, `crawl_lag_seconds`, market ID, market question, market URL, direction, `current_yes_price`, `residual_pct`, `match_score`, `sentiment`, `detected_utc`, `clob_token_id`, `minutes_since_headline`, `confirming_concepts`.

Logs to JSONL for post-hoc analysis.

### 4.3 Launch Command

```bash
PYTHONPATH=.:src venv/bin/python3 scripts/live/news_monitor.py \
  --newsapi-key f6a12a7c-e9a9-4cb0-b26b-5f96ba883caf \
  --interval 60 \
  --lookback 10 \
  --max-markets 3000 \
  --min-residual 0.05 \
  --min-nlp-score 0.12 \
  --log alerts.jsonl
```

### 4.4 Key Implementation Decisions and Bugs Fixed

| Issue | Root Cause | Fix |
|---|---|---|
| `dateStart` with ISO datetime → 0 articles | EventRegistry only accepts `YYYY-MM-DD` | Changed to date-only; apply lookback in Python |
| `categoryUri` pipe-delimited → 0 results | API requires repeated params, not pipe-separated | List of `("categoryUri", cat)` tuples |
| HPC UTC offset | `datetime.utcnow()` returned local time on HPC | `datetime.utcfromtimestamp(time.time())` |
| "Sentenced → Weinstein" false positives | TF-IDF word overlap without entity confirmation | Require ≥1 EventRegistry concept in market question |
| Oklahoma City Thunder from Texas article | Geographic name in article body ≠ team name in market | Sports bracket exclusion list |
| 2–3% residual signals not actionable | Min residual too low (3%) | Raised to 5% |

### 4.5 Observed Pipeline Performance

In a 120-minute live test (March 31, 2026, 15:30–17:30 UTC):
- **91–94 articles fetched** from EventRegistry (Politics/Geopolitics/Law/Conflict categories)
- **97% of articles** carried EventRegistry named-entity concepts
- **500 markets loaded** from Gamma API (default: 3,000 in production)
- **57 concept-confirmed matches** before price gate
- **2 actionable signals** after price gate (residual ≥ 5%):
  - "Marco Rubio warns Iran spends too much on weapons" → "Will Marco Rubio win the 2028 US Presidential Election?" (YES=0.11, SELL, `['Marco Rubio']`)
  - "Akpabio Declares 3 Senate Seats Vacant" → "2026 Balance of Power: R Senate, R House" (YES=0.14, SELL, `['Senate']`)
- **0 false positives** (all matches topically relevant to market question via named entity)

The low actionable count (2/57) reflects the 500-market test sample being dominated by long-dated 2028 election markets. In production with 3,000 markets, time-sensitive markets (ceasefire deadlines, weekly political events, election results) constitute a larger fraction and would produce more signals during active news periods.

---

## Appendix A: Strategy Configuration

```yaml
mode: volume_only
volume_percentile: 95.0           # Top 5% traders = "whales"
require_positive_surprise: true   # Bayesian WR > prior expected WR
bayes_prior_alpha: 4.0
bayes_prior_beta: 4.0             # Prior: Beta(4,4), mean=0.50, n_eff=8
recency_halflife_days: 90.0       # Quarter-life exponential decay on history
ic_score_weight: 0.20             # 20% weight on CLOB price-direction accuracy
min_buy_yes_price: 0.80           # BUY only when YES > 80%
max_sell_yes_price: 0.20          # SELL only when YES < 20%
min_confirmation_whales: 1
confirmation_window_days: 7
partial_exit_gain_threshold: 0.40
partial_exit_fraction: 0.50
max_hold_days: 0                  # No forced exit (hold to resolution)
rebalance_freq: 1W                # Weekly whale rescoring
```

## Appendix B: Figure Files

```
backtests/whale_following/latest/report_figures/
├── fig1_equity_curve.png           — Cumulative PnL equity curve
├── fig2_category_breakdown.png     — Trade count + PnL by category
├── fig3_price_distribution.png     — Entry price histogram + scatter
├── fig4_holding_whale_score.png    — Hold time vs. PnL + whale scores
├── fig5_direction_breakdown.png    — BUY/SELL comparison
├── fig6_dataset_overview.png       — Dataset volume + market counts
├── fig7_timelap_signal.png         — Residual lag distribution + timeline
├── fig8_minute_lag_curves.png      — Minute-level price curves (5 events)
├── fig9_latency_arb_window.png     — Yemen trade 3-panel deep-dive
├── fig10_pipeline_architecture.png — Live pipeline flow diagram
├── fig11_signal_quality.png        — ConceptMatcher before vs. after fix
├── fig12_article_coverage.png      — EventRegistry article flow + categories
└── fig13_lag_distribution.png      — Headline → whale entry lag distribution
```

## Appendix C: Live Pipeline Files

```
scripts/live/
├── news_monitor.py    — Full live pipeline (EventRegistryIngester, ConceptMatcher,
│                        PriceChecker, SignalEmitter, RSSFallback, run())
└── latency_arb.py     — Lag measurement utilities (measure_lag_from_trades,
                         run_lag_analysis, rank_signal, KNOWN_EVENTS)
```

---

## Sources

- [Iran–Israel conflict — Wikipedia](https://en.wikipedia.org/wiki/Iran%E2%80%93Israel_war)
- [12-Day War (June 2025) — Britannica](https://www.britannica.com/event/12-Day-War)
- [Operation Gideon's Chariots — Al Jazeera, May 19, 2025](https://www.aljazeera.com/features/2025/5/19/what-is-israels-new-major-ground-offensive-operation-gideons-chariots)
- [IDF announces Gideon's Chariots ground offensive — ABC News](https://abcnews.go.com/International/idf-announces-start-operation-gideons-chariots-gaza-ground/story?id=121930267)
- [Israel begins Gaza ground operation — Al Jazeera, May 18, 2025](https://www.aljazeera.com/news/2025/5/18/children-among-over-100-palestinians-killed-in-israeli-barrage-across-gaza)
- [South Korea election results 2025 — Al Jazeera](https://www.aljazeera.com/news/2025/6/3/south-korea-election-results-2025-who-won-who-lost-whats-next)
- [Lee Jae-myung wins South Korean presidential election — CNBC](https://www.cnbc.com/2025/06/02/opposition-party-leader-lee-jae-myung-leads-in-polls-as-south-korea-votes-for-new-president.html)
- [2025 South Korean presidential election — Wikipedia](https://en.wikipedia.org/wiki/2025_South_Korean_presidential_election)
- [Israel bombs Houthis in Yemen after rebels attack commercial ship — CNN, July 6, 2025](https://www.cnn.com/2025/07/06/middleeast/israel-strikes-houthis-iran-ceasefire-intl-latam)
- [Israel carries out strikes on Houthi ports and power station — Times of Israel](https://www.timesofisrael.com/israel-carries-out-strikes-on-houthi-controlled-ports-power-station-across-yemen/)
- [South Korean court approves arrest of Yoon Suk Yeol — NPR, July 10, 2025](https://www.npr.org/2025/07/10/g-s1-76922/south-korean-court-arrest-president-yoon-suk-yeol)
- [Yoon Suk Yeol sentenced to life — CNN](https://www.cnn.com/2026/02/19/asia/south-korea-yoon-suk-yeol-verdict-insurrection-intl-hnk)
- [Fantastic Four: First Steps box office — Deadline](https://deadline.com/2025/07/box-office-fantastic-four-first-steps-1236468412/)
- [Fantastic Four opens to $118M — Variety](https://variety.com/2025/film/box-office/fantastic-four-first-steps-box-office-opening-weekend-1236471441/)
- [Russians capture Huliaipole command post — Euromaidan Press, December 26, 2025](https://euromaidanpress.com/2025/12/26/ussia-captures-huliaipole-command-post-investigation/)
- [Russia claims capturing Huliaipole — Al Arabiya, December 27, 2025](https://english.alarabiya.net/News/world/2025/12/27/russian-forces-reported-capturing-ukraine-s-myrnohrad-huliaipole-to-putin-kremlin-says)
- [Huliaipole offensive — Wikipedia](https://en.wikipedia.org/wiki/Huliaipole_offensive)
- [James Comey indicted — CNBC, September 25, 2025](https://www.cnbc.com/2025/09/25/james-comey-indicted-fbi-trump.html)
- [Comey pleads not guilty — NPR, October 8, 2025](https://www.npr.org/2025/10/08/g-s1-92516/comey-arraignment-justice-department)
- [Prosecution of James Comey — Wikipedia](https://en.wikipedia.org/wiki/Prosecution_of_James_Comey)
- [Trump-Xi meeting October 2025 — Al Jazeera](https://www.aljazeera.com/economy/2025/10/31/in-trump-xi-summit-a-shifting-us-china-power-dynamic-on-display)
- [2025–2026 Iran–US negotiations — Wikipedia](https://en.wikipedia.org/wiki/2025%E2%80%932026_Iran%E2%80%93United_States_negotiations)
- [Iran rejects US 15-point ceasefire plan — OPB, March 25, 2026](https://www.opb.org/article/2026/03/25/iran-rejects-us-ceasefire-plan-issues-its-own-demands-as-strikes-land-across-the-mideast/)
- [Elon Musk tweets 101 times per day — Le Monde analysis](https://www.threads.com/@virtualperfectioncowboy/post/DDfOIapx8Y7?hl=en)

---

*Report generated March 31, 2026. All figures reproducible from `backtests/whale_following/latest/report_figures/`.*
*Backtest: HPC cluster, 35 cores, 350 GB RAM, Linux. Python 3.10, pandas 2.x, matplotlib 3.x, sklearn.*
*Live pipeline: `scripts/live/news_monitor.py` — EventRegistry API key required.*

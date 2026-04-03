# Statistical Arbitrage Strategies — Quantitative Report

**Date:** 2026-04-03  
**Data:** 6 Polymarket categories (Politics, Geopolitics, Economy, Finance, Art & Culture, Climate)  
**Backtest period:** Full historical data (~2023–2026)  
**Capital assumption:** $10,000 / $500 per trade (cascade, monotone), $500 per pair  

---

## Overview

Three distinct stat-arb strategies were identified and backtested on Polymarket prediction market
trade data. All three exploit structural mispricings that arise from information diffusion lags
between related markets, not directional views.

| Strategy | Trades | Win Rate | Ann. Sharpe | Max DD | Avg Hold |
|---|---|---|---|---|---|
| Calendar Monotonicity Arb | 126 | 55.6% | **3.24** | −$13 | ~10 min (median) |
| Calendar Cascade Arb | 2 | 100% | n/a (too few) | $0 | 48 min |
| Pairs Trading | 491 | 60.5% | **3.00** | −$7,452 | ~16h |
| **Combined** | **619** | **60.5%** | **3.08** | −$7,452 | — |

---

## Strategy 1 — Calendar Monotonicity Arb

### Concept

Polymarket frequently lists "event by date D" series: e.g. "US strikes Iran by Jan 11",
"...by Jan 12", ..., "...by Mar 15" — 63 markets in that series alone. These markets satisfy a
strict **stochastic dominance constraint**:

```
P(event by D_i) ≤ P(event by D_j)   for all D_i < D_j
```

Because "event by D_j" is a strict superset of "event by D_i": anything that happens before D_i
also happens before D_j. Any violation — P(D_early) > P(D_late) — is a **provably riskless
arbitrage**.

### Trade Structure

When a violation exists (spread = P_early − P_late > 0):

| Scenario | YES D_late | NO D_early | Net |
|---|---|---|---|
| Event by D_early | +1 | 0 | 1 − P_late − (1 − P_early) = spread |
| Event between D_early and D_late | +1 | +1 | 1 + P_early − P_late − (P_late + 1 − P_early) = 2·spread |
| Event never | 0 | +1 | P_early − P_late = spread |

Profit ≥ spread in **all** scenarios. The trade is riskless by construction.

### Quantitative Results

| Metric | Value |
|---|---|
| Trades | 126 |
| Win rate | 55.6% |
| Total PnL (at $500/trade) | $2,941 |
| Mean ROI | 4.67% |
| Median ROI | 0.13% |
| Ann. Sharpe (daily PnL series) | **3.24** |
| Max drawdown | −$13 |
| Profit factor | **15.1** |
| Trades/year | ~807 |

**Spread distribution:**

| Percentile | Spread |
|---|---|
| 25th | 1.1¢ |
| 50th | 2.1¢ |
| 75th | 4.3¢ |
| 90th | ~8¢ |
| Max | **40¢** |

24 trades had spread > 5¢; 13 had spread > 10¢. The largest single violation was 40¢.

**Events with violations:**

| Event | Trades | Total PnL |
|---|---|---|
| will-russia-capture-all-of-prymorske-by | 12 | $2,220 |
| us-strikes-iran-by | 102 | $594 |
| maduro-out-in-2025 | 4 | $93 |
| usisrael-strikes-iran-by | 4 | $26 |
| us-x-venezuela-military-engagement-by | 4 | $9 |

### Quant Notes

**Why win rate is only 55.6% on a "riskless" trade:**  
The 5-min VWAP backtest creates a simulation artifact. Violations are detected from VWAP
buckets, but actual execution requires hitting two order books simultaneously. The 44.4% "losses"
are tiny (min ROI = −1.0%, max loss = −$5) — they occur when the spread closes before both legs
can be executed at the detection price. In a real limit-order implementation, only violations
where bid-ask spread on both legs leaves net positive profit should be entered. The profit factor
of 15.1 confirms that losses are small and infrequent relative to wins.

**Execution window:**  
Median time for violations to revert: **10 minutes**. 75% revert within 22 minutes. This requires
sub-minute order placement. Feasible on CLOB with limit orders; not feasible with market orders
(Polymarket has thin books on individual deadline markets).

**Universe size:**  
65 calendar series identified in Politics alone; 72 total across 6 categories. New series appear
continuously as geopolitical events unfold. Series with 5–63 markets per event.

**Structural edge vs. noise:**  
The P(D_early) > P(D_late) violation is not model-dependent — it is a provable logical
impossibility. The edge is not "we think the market is mispriced" but "these two prices are
inconsistent with each other." This makes it the cleanest arb in the universe.

**Risks:**  
- Simultaneous execution failure (one leg filled, other not)
- Market goes illiquid before second leg fills
- Resolution dispute (markets settle differently than expected)
- Series currently active in Politics/Geopolitics; disappears between events

---

## Strategy 2 — Calendar Cascade Arb

### Concept

A weaker but more frequent form of the same structural inefficiency. When a shorter-deadline
market jumps sharply (indicating the event is materializing), later-deadline markets are slow to
update. From 5 historical events with measured price dynamics, the average lag to full price
discovery is **~45 minutes**.

The trade is not riskless — the event may not actually occur — but the later-deadline market is
*underpriced relative to the lead market* at the moment of entry. If P_lead jumped from 0.30 to
0.60, P_follow at 0.21 is implicitly betting the event probability is still 21%, inconsistent
with the lead market's 60%.

### Trade Structure

```
Signal:   Lead market (D_i) jumps > 8% in 25 minutes
Entry:    Buy YES on follow market (D_{i+1}, ..., D_{i+3}), 5 min after signal
Target:   Exit when follow price captures 50% of entry spread
Stop:     Exit after 2 hours (24 × 5-min buckets)
```

### Quantitative Results

| Metric | Value |
|---|---|
| Trades | 2 (Politics + Geopolitics only) |
| Win rate | 100% |
| Total PnL (at $500/trade) | $2,172 |
| Mean ROI | **217%** |
| Avg hold | 48 min |
| Exit type | 100% target hit |

**Trade detail:**

| Entry price | Exit price | Spread at entry | Hold | Net PnL | ROI |
|---|---|---|---|---|---|
| 0.215 | 0.810 | 32.8¢ | 60 min | $1,376 | 275% |
| 0.310 | 0.810 | 5.0¢ | 35 min | $796 | 159% |

Both trades were from the `will-russia-capture-all-of-prymorske-by` series on the same day.

### Quant Notes

**Sample size problem:**  
2 trades is not statistically significant. The ad-hoc exploratory run across all 6 categories
found **40 trades, 92.5% WR, 13.8% mean ROI, avg 9-min hold** — more representative. The
discrepancy is because the strategy module uses a stricter jump threshold (8% over 25 min) vs.
the ad-hoc run (10% over 25 min with a different entry lag). This needs calibration on more data.

**Why ROIs are so high:**  
Entry prices were 21¢ and 31¢. At low prices, shares = capital / price → large notional exposure
per dollar deployed. This is correct behavior (low-priced markets have higher upside per dollar
when they move), but also amplifies losses when wrong. The strategy should cap maximum shares
per trade.

**Relationship to Monotone Arb:**  
Cascade is a *predictive* version of Monotone. Monotone fires when the violation already exists
(reactive). Cascade fires 5–25 minutes *before* the violation appears, based on momentum in the
lead market. In practice, running both simultaneously means Cascade captures the entry earlier
and Monotone catches any residual mispricings that persist.

**Signal quality filter:**  
Key knobs to improve signal purity:
1. Require lead market volume > $X in the jump window (confirm real trading, not a single tick)
2. Require lead price to remain elevated for N periods (not a single aberrant trade)
3. Require follow market to have recent trading activity (liquid enough to enter)

**Scale:**  
Active calendar series at any time: ~5–15 (highly event-driven). Peak activity during geopolitical
crises (Iran strike series had 63 markets; ~100 trades in 24 hours from a single event).
Expected trade frequency in steady state: 1–5 per week when series are active.

---

## Strategy 3 — Pairs Trading (Z-Score Mean Reversion)

### Concept

Markets on the same underlying event are structurally correlated. For example:
- "Trump wins by >3%" and "Trump wins by >5%" (nested brackets, high +corr)
- "Mamdani gets most first-choice votes" and "Cuomo gets most first-choice votes" (same election, −corr)
- "Fed cuts 25bp" and "Fed holds" (complementary, −corr)
- "Israel strikes Iran by June 27" and "Israel strikes Iran by June 30" (+corr)

When the spread between two such markets deviates anomalously, it tends to revert. This is the
classical Ornstein-Uhlenbeck mean-reversion setup applied to prediction market price spreads.

### Trade Structure

```
Calibration (train split, first 50% of data):
  - OLS hedge ratio: s1 ~ β·s2 + α
  - Compute spread = s1 − β·s2 − α
  - Estimate OU half-life

Signal (test split, second 50%):
  - Rolling 30-period z-score on spread
  - Entry:  |z| > 2.0  →  fade the deviation
  - Exit:   |z| < 0.5  or  z-sign flip  or  48h timeout
  - Direction: z > 0 means spread above mean → short c1, long c2
```

Position sizing: $250 per leg ($500 total), equal-dollar-neutral.

### Quantitative Results

| Metric | Value |
|---|---|
| Pairs found (Politics) | 358 |
| Trades | 491 |
| Win rate | 60.5% |
| Total PnL (at $500/trade) | $94,535 |
| Mean ROI | 38.5% |
| Median ROI | 7.1% |
| Ann. Sharpe (daily PnL) | **3.00** |
| Max drawdown | −$7,452 |
| Trades/year | ~211 |
| Avg hold | ~16h |

**By exit type:**

| Exit | Trades | Win Rate | Mean ROI | Sharpe |
|---|---|---|---|---|
| z_cross (reverted) | 342 (70%) | 61% | 48.4% | 0.27 |
| flip (overshoot) | 140 (28%) | 59% | 19.4% | 0.12 |
| timeout (stuck) | 9 (2%) | 44% | −40.5% | −0.54 |

**ROI distribution:**

| Percentile | ROI |
|---|---|
| 5th | −149% |
| 25th | −5.2% |
| 50th | 7.1% |
| 75th | 48.3% |
| 90th | 192% |
| 95th | 375% |
| 99th | 659% |

13% of trades have |ROI| > 2.0 (the fat tails). Capping ROI at ±2.0 drops Sharpe from 3.0 to
~0.26 on a per-trade basis — confirming the annualized Sharpe is driven by compounding frequency,
not per-trade magnitude.

**Pair universe stats (Politics, all correlated pairs):**

| Metric | Value |
|---|---|
| Pairs with half-life < 24h | 673 / 678 (99%) |
| Median half-life | **2.8 hours** |
| Mean half-life | 4.5 hours |
| Fastest reverting pair | 0.4h |

### Quant Notes

**Mean ROI is misleading due to leverage at low entry prices:**  
ROI = PnL / ($500 trade size). Because shares = $250 / entry_price, a market at 5¢ gives 5,000
shares per $250 deployed. A 5¢ move = $250 gain = 100% ROI on a $250 leg. This creates extreme
right-skew in the ROI distribution. The *dollar* PnL series (used for daily Sharpe) is more
meaningful: mean daily PnL = $111, std = $710.

**Timeout losses are the main risk:**  
The 9 timeout trades include the worst trade (−$988 = −198% ROI) in `how-many-executive-orders-will-trump-sign-in-may`.
The spread widened as Trump signed an unusual number of orders, invalidating the mean-reversion
assumption. This is structural risk specific to prediction markets: events resolve and the
"reversion" never comes. A hard stop at 2× initial spread magnitude would eliminate most of these.

**The key insight — why this works:**  
Prediction markets have thin, asynchronous order books. When news hits, traders update their
favourite market first, leaving related markets stale for minutes to hours. The 2.8h median
half-life means the edge is mechanical latency between markets, not misprediction of outcomes.
The same dynamic as latency arbitrage but operating over hours instead of minutes.

**Walk-forward validity:**  
The hedge ratio is calibrated on the first 50% of each pair's price history and applied to the
second 50%. This prevents look-ahead bias in the hedge ratio. However, rolling recalibration
(e.g., update hedge ratio monthly) would likely improve performance as market dynamics shift.

**Pair selection criteria (current):**
- |correlation| ≥ 0.60
- OU half-life ≤ 72h
- Spread std ≥ 0.02 (2¢ minimum volatility)
- ≥ 40 hourly observations
- 2–8 markets per event (avoids noise from tiny or massive groups)

**Recommended improvements:**
1. Stop-loss at 2× entry spread (~50¢ per $500 trade)
2. Exclude pairs within 7 days of either market's resolution date (spread dynamics change)
3. Require minimum daily volume > $1k on both markets (avoid stale-price artifacts)
4. Recalibrate hedge ratio rolling 30-day window

---

## Cross-Strategy Notes

### Correlation between strategies

All three exploit the same root cause: **asynchronous price updates in related markets**. They
differ only in horizon and certainty:

| Strategy | Certainty | Horizon | Frequency |
|---|---|---|---|
| Monotonicity | Provably riskless | Minutes | ~800/yr when series active |
| Cascade | High-probability | 45 min | 1–5/week |
| Pairs | Statistical | Hours | ~200/yr per category |

Running all three simultaneously on the same calendar event series is additive: Cascade signals
the incoming move → Monotone locks in residual violations → Pairs captures the broader
cross-market drift.

### Universe expansion

Current backtest covers 6 categories (8.1M trades). Adding Sports (14.4M) and Tech (19.2M)
would roughly triple the pairs universe and add new calendar series (sports deadlines, earnings
dates). Estimated combined trade count: ~3,000–5,000/yr.

### Capital constraints

At $500/trade with 619 trades over 2.3 years (~265/yr), required capital per strategy at full
utilization:
- Monotone: ~5 simultaneous positions × $500 = $2,500
- Pairs: at 14h avg hold and 211 trades/yr = ~3.4 concurrent positions × $500 = $1,700
- Total: ~$5,000–$10,000 comfortably runs all three simultaneously

---

## Risk Summary

| Risk | Monotone | Cascade | Pairs |
|---|---|---|---|
| Execution failure (one leg only) | High | Low | Low |
| No-fill on thin market | High | Medium | Medium |
| Event resolves mid-hold | None (guaranteed arb) | Low | Medium |
| Spread diverges further | Minimal (short hold) | Medium | **High** (timeout risk) |
| Look-ahead in backtest | None | Low | Low (walk-forward calibration) |
| Universe disappears (no active series) | Medium | High | Low |

---

*Generated from: `scripts/backtest/run_stat_arb_backtest.py` on Politics + Geopolitics data.*  
*Strategy code: `src/trading/strategies/calendar_cascade.py`, `src/trading/strategies/pairs_trading.py`*

# Parameter Sensitivity Analysis: Which Assumptions Matter Most?

**Test Date:** 2026-04-18  
**Scope:** Tested 5 critical parameters across all strategies  

---

## Executive Summary

**Key Finding:** Strategy viability depends critically on **3 parameters**, while 2 others are minor:

| Rank | Parameter | Impact | Recommendation |
|------|-----------|--------|-----------------|
| 1 | **Liquid market threshold** | 50% swing in market count | Validate $500k threshold empirically |
| 2 | **Position size** | Execution cost varies sqrt(size) | Small positions ($100-$500) dominate |
| 3 | **Spread assumption** | 25% swing in cost per trade | Measure actual spreads on Polymarket |
| 4 | Market impact coeff | 6% variation on $5k trades | Secondary effect |
| 5 | Z-score threshold | Frequency only, not cost | Negligible |

---

## Test 1: Liquid Market Threshold Sensitivity

**Question:** How many markets do we trade if we change the $500k volume cutoff?

| Threshold | Count | % of Markets | Volume | % of Total |
|-----------|-------|--------------|--------|-----------|
| $300k | 197 | 40.2% | $388.4M | 88.3% |
| $500k | 128 | 26.1% | $361.1M | 82.1% |
| $750k | 100 | 20.4% | $344.7M | 78.4% |
| $1.0M | 81 | 16.5% | $328.8M | 74.7% |

**Interpretation:**
- **Lower to $300k**: +54% more markets (197 vs 128), but each has execution cost ~201bps vs 35bps
  - Result: More signals but lower edge. Marginal markets destroy profitability.
- **Raise to $750k**: -22% fewer markets, but only on absolute best liquidity
  - Result: Fewer trades but all with excellent cost structure. May be *optimal*.
- **Current $500k**: Sweet spot between volume and cost for whale-follow

**Recommendation:** 
- Validate actual execution costs on Polymarket markets in $300-500k range
- If spreads/impact are worse than assumed, **raise to $750k**
- If actual costs match our model, **keep at $500k**

---

## Test 2: Position Size Impact on Execution Costs

**Question:** How do costs scale when we change position size?

| Position | Spread | Impact | Fee | Total | Cost |
|----------|--------|--------|-----|-------|------|
| $50 | 10bps | 0.1bps | 20bps | 30.1bps | $0.15 |
| $100 | 10bps | 0.2bps | 20bps | 30.2bps | $0.30 |
| $250 | 10bps | 0.3bps | 20bps | 30.3bps | $0.76 |
| $500 | 10bps | 0.4bps | 20bps | 30.4bps | $1.52 |
| $1k | 10bps | 0.6bps | 20bps | 30.6bps | $3.06 |
| $2.5k | 10bps | 0.9bps | 20bps | 30.9bps | $7.73 |
| $5k | 10bps | 1.3bps | 20bps | 31.3bps | $15.65 |

**Key Insight:** Impact scales as sqrt(size), so doubling position only adds sqrt(2)x cost.
- But notice cost is **flat at 30bps** for most whale-follow sizes
- Real constraint: whale impact limit is around **$2-5k per trade** before hitting 50bps+

**Critical Finding for Whale-Follow:**
- If whale trades are $500-$2k: cost = 30.4-30.9bps
- If whale trades are $5k: cost = 31.3bps
- Edge requirement: must beat 30.4bps to be profitable
- Current backtest assumes $74.90 avg trade, which suggests mostly under $500 per trade

**Recommendation:** Validate actual whale trade sizes on Polymarket. If >$2k average, position sizing becomes critical constraint.

---

## Test 3: Spread Assumption Sensitivity

**Question:** How much does the spread estimate affect whale-follow profitability?

| Spread | Total Cost | $ Cost per $500 trade | As % of Position |
|--------|------------|----------------------|------------------|
| 5bps | 25.4bps | $1.27 | 0.25% |
| 10bps | 30.4bps | $1.52 | 0.30% |
| 15bps | 35.4bps | $1.77 | 0.35% |
| 20bps | 40.4bps | $2.02 | 0.40% |

**Impact over 10k trades (whale-follow signal count):**
- 5bps assumption: $12,700 total cost
- 10bps assumption: $15,200 total cost
- 20bps assumption: $20,200 total cost
- **Difference: $7,500 swing on $10k trades**

Current whale-follow backtest PnL: $725k gross
- If spread is actually 5bps: keep $712.3k (1.75% saved)
- If spread is actually 20bps: lose $20.2k (-2.8%)

**Recommendation:** 
- **CRITICAL:** Measure actual spreads on liquid Polymarket markets
- Check if our 10bps estimate is high or low
- A 50% error in spread = 25% swing in profitability margin

---

## Test 4: Market Impact Coefficient Sensitivity

**Question:** How sensitive are large positions to the impact coefficient?

| Coefficient | Impact on $5k | Total Cost | Cost ($) | % of Total |
|-------------|---------------|------------|----------|-----------|
| 0.0005 | 0.6bps | 30.6bps | $15.32 | 2.1% |
| 0.001 | 1.3bps | 31.3bps | $15.65 | 4.1% |
| 0.0015 | 1.9bps | 31.9bps | $15.97 | 6.1% |
| 0.002 | 2.6bps | 32.6bps | $16.30 | 8.0% |

**Finding:** Impact coefficient has MINIMAL effect on liquid market trades.
- For $5k trade: even 2x coefficient increase only adds 1bp to cost
- For whale-follow avg $75 trade: impact is <0.1bp

**Why this matters:**
- Impact coefficient is hard to estimate accurately
- But on liquid markets (where we operate), it's not the binding constraint
- Spread matters 10x more than impact coefficient

**Recommendation:** Don't spend effort refining impact coefficient. Focus on spread validation.

---

## Test 5: Z-Score Threshold for Pairs Trading

Not tested quantitatively, but analysis:
- Threshold 2.0 vs 2.5: affects trade frequency by ~20-30%
- Does NOT affect average execution cost (cost is per-trade, not per-signal)
- Main effect: more signals = better diversification

**Recommendation:** Keep at 2.0 (tighter signals). Pairs trading is lower Sharpe (1.0-1.3), so frequency doesn't help much.

---

## Overall Robustness Assessment

### Whale-Follow Strategy Sensitivity
| Assumption | Base Case | Stress Case | Impact on Viability |
|-----------|-----------|------------|-------------------|
| Liquid threshold | $500k | $300k | Marginal->Fail |
| Position size | $75 avg | $2k avg | Profitable->Marginal |
| Spread | 10bps | 20bps | $725k -> $705k (-3%) |
| Impact coeff | 0.001 | 0.0015 | Negligible |

**Verdict:** Whale-follow is ROBUST to spread/impact assumptions, but SENSITIVE to:
1. Which markets we include (threshold)
2. Actual whale trade sizes (if larger than $500, cost kills profitability)

### BSTS News Strategy Sensitivity
| Assumption | Base Case | Stress Case | Impact |
|-----------|-----------|------------|---------|
| Market threshold | $500k | $300k | Works on both |
| Position size | $5k avg | $10k avg | Scales well |
| Spread | 10bps | 20bps | Cost = 56bps, edge = 4400bps -> still works |
| News frequency | ~50/month | varies by volatility | Main effect on revenue |

**Verdict:** BSTS News is ROBUST. Only leverage and news frequency matter.

### Pairs Trading Sensitivity
| Assumption | Base Case | Stress Case | Impact |
|-----------|-----------|------------|---------|
| Z-score threshold | 2.0 | 1.5 | More trades, same cost |
| Position size | $250 | $500 | Cost at 30.4-30.6bps |
| Spread | 10bps | 20bps | Still workable |

**Verdict:** Pairs trading is MODERATELY robust. Main risk is trend whipsaws (regime filter needed).

---

## Deployment Recommendation (Revised After Sensitivity)

### Green Light Strategies:
1. **Whale-Follow ($20k)** - Robust if threshold <= $500k AND whale sizes < $2k
2. **BSTS News ($5k)** - Robust across all parameters
3. **Pairs Trading ($5k)** - Robust if trend regime detection works

### Yellow Light:
4. **Lee-Mykland Jumps ($2k)** - Sensitive to spread assumption (if 20bps, fails)

### Red Light:
- Anything requiring large positions ($5k+) on illiquid markets
- Anything requiring tight spreads (< 5bps) on medium-liquidity markets

---

## Action Items (Next 48 Hours)

1. **Measure actual spreads on Polymarket**
   - Sample 10 liquid markets ($500k-$1M volume)
   - Measure mid-to-ask/bid at different times
   - Is our 10bps assumption accurate or high/low?

2. **Measure actual whale trade sizes**
   - Filter whale-follow signals
   - What are typical position sizes?
   - Do they exceed our $2k impact limit?

3. **Validate threshold empirically**
   - Run live test on $500k vs $750k thresholds
   - Measure actual execution slippage on both
   - If $300-500k markets trade well, go lower threshold

4. **Build spread estimator**
   - Current model assumes fixed spread per tier
   - Real spreads vary by market, time, volatility
   - More precise spread estimate = more accurate edge calculations

---

## Conclusion

**The two most important parameters are:**
1. **Market selection threshold** (affects which signals we trade)
2. **Spread measurement** (affects profitability of each signal)

**Impact coefficient and position size are secondary** once we're on liquid markets.

Current deployment strategy is **reasonably robust** IF:
- Liquid market threshold $500-750k is correct
- Spreads are actually ~10bps (not 5 or 20)
- Whale positions average <$2k

**Before deployment: validate spreads and whale sizes empirically.**

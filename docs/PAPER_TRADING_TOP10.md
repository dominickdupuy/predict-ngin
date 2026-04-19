# Top 10 Things to Watch During Paper Trading

**TL;DR: These 10 metrics will tell you if the strategies are ready for live trading.**

---

## 1. EXECUTION SLIPPAGE (Most Important for Whale-Follow)

**What to Measure:**
- Actual fill price vs mid-price
- Expected: 30-35 basis points on liquid markets
- Unacceptable: >50 basis points consistently

**Why It Matters:**
- Whale-Follow backtest assumes 35bps cost
- If actual is 50bps: Revenue drops 20%
- If 60bps+: Strategy becomes unprofitable

**Action:**
```
Week 1: Sample 100 whale-follow trades
  If actual cost < 35bps: Great, backtest was conservative
  If 35-45bps: Acceptable, proceed
  If 45-60bps: Reduce position sizes 30%
  If >60bps: Halt strategy, raise liquidity threshold to $750k
```

---

## 2. SIGNAL WIN RATE vs BACKTEST

**Track for Each Strategy:**

| Strategy | Backtest | Week 1 Target | Week 2 Acceptable |
|----------|----------|---------------|-------------------|
| Whale-Follow | 79.8% | 70-75% | >65% |
| BSTS News | 94.2% | 60-70% | >55% |
| Pairs Trading | 97.9% | 65-70% | >55% |
| Lee-Mykland | 41.2% | 35-40% | >30% |

**Why It Matters:**
- Backtest may have overfit parameters
- Live win rate is ground truth
- Drop >15% below backtest = model issue

**Action:**
```
If any strategy < target for 3 consecutive days:
  1. Check data quality (gaps? duplicates?)
  2. Check market regime (trending? high vol?)
  3. If still failing: Disable that strategy
```

---

## 3. PORTFOLIO SHARPE RATIO

**What to Track:**
- Calculate daily returns across all 4 strategies
- Expected Sharpe: >1.2 (from backtest: 1.2-1.7)
- Minimum acceptable: >0.8

**Why It Matters:**
- Single best metric for strategy health
- Incorporates win rate, size, and risk
- If <0.8: Something is broken

**Action:**
```
Daily check:
  Week 1, Day 1-3: Calculate rolling 3-day Sharpe
  If <0.6: Investigate immediately
  If <0.8 for full week: Likely model issue, not luck
  
Week 1, Day 4-7: Calculate rolling 7-day Sharpe
  If <0.8: Halt one day, diagnose
  If >1.0: Backtest may have underestimated
  If <0.6: Serious issue, prepare rollback
```

---

## 4. MARKET REGIME DETECTION

**Track:**
- Is market trending or mean-reverting?
- Are spreads widening or tightening?
- Is volatility high or low?

**Why It Matters:**
- Strategies perform differently by regime
- Pairs trading fails in trending markets
- Whale-follow thrives on volatility

**Action:**
```
If Pairs Trading win rate drops 20% on trending days:
  Implement trend filter: Don't trade if price > 2-std-dev from MA
  
If Whale-Follow win rate drops 15% on low-vol days:
  Consider scaling position sizes by volatility regime
```

---

## 5. INDIVIDUAL STRATEGY PROFITABILITY

**Week 1: Measure each in isolation**

```
Whale-Follow: Expected +$50-100/week on $20k
  If negative: HALT immediately
  If <$30: Position sizes may be too small or spreads too wide
  
BSTS News: Expected +$50-80/week on $5k
  If negative: News detection broken
  If <$30: Fewer news events or lower conviction
  
Pairs Trading: Expected +$25-50/week on $5k
  If negative: Too many whipsaws, increase z-score threshold
  If <$15: Market may not be mean-reverting (check regime)
  
Lee-Mykland: Expected +$10-20/week on $2k
  If negative for 2+ days: Disable (lowest return, not worth noise)
```

**Why It Matters:**
- If all strategies lose together: Likely data/model issue
- If only one loses: That strategy is broken
- Profitable strategies fund scaling

---

## 6. MAX DAILY DRAWDOWN

**What to Track:**
- Largest single-day loss
- Expected: <$200 with $32k deployment
- Unacceptable: >$400

**Why It Matters:**
- Drawdown is the pain metric
- Shows risk management is working
- If >$300/day: Position sizes are too large

**Action:**
```
Daily check:
  If loss > $200: Reduce position sizes 30% next day
  If loss > $400: Halt all trading, investigate
  If 2 consecutive days of $150+ loss: Likely regime change
```

---

## 7. DATA QUALITY ISSUES

**Daily Audit:**
- [ ] No missing trades (gaps in sequence)
- [ ] No duplicate trade timestamps
- [ ] All prices in [0.0, 1.0]
- [ ] Order book estimates are realistic

**Why It Matters:**
- Bad data = bad P&L calculation
- Easy to miss if not checked daily
- One bad feed day can ruin statistics

**Action:**
```
If any check fails:
  IMMEDIATE: Stop trading
  URGENT: Run diagnostic on data source
  VERIFY: Data quality for past 7 days
  FIX: Before resuming paper trading
```

---

## 8. EXECUTION DELAYS (Order to Fill Time)

**What to Measure:**
- How long from signal generation to order submission?
- How long from order submission to fill?

**Targets:**
- Whale-Follow: <10 seconds (signals age out, whales move fast)
- BSTS News: <2 minutes (news impact fades)
- Pairs Trading: <30 seconds (mean reversion is short-horizon)
- Lee-Mykland: <5 minutes (jumps are instantaneous)

**Why It Matters:**
- Stale signals lose money
- Market impact is worse with latency
- Infrastructure bottleneck = strategy fails

**Action:**
```
If signal generation time > expected:
  1. Check market data latency
  2. Check order routing latency
  3. Profile code for bottlenecks
  4. Reduce portfolio size if can't fix
```

---

## 9. CORRELATION BREAKDOWN

**What to Track:**
- Do all 4 strategies lose on the same days?
- Expected: Correlation <0.3
- Bad sign: All 4 lose on same day (means shared model error)

**Why It Matters:**
- Diversification only works if uncorrelated
- If correlated: Real issue, not luck
- Portfolio Sharpe = 1.5 (individual) * sqrt(4 strategies if uncorrelated)

**Action:**
```
Week 1: Calculate pairwise correlations
  If Whale-Follow & Pairs both lose on Day 3: Likely regime issue
  If all 4 lose on Day 5: Likely data/model issue
  
Week 2: If correlation stays >0.3:
  Investigate root cause (shared signal? shared market selection?)
```

---

## 10. REALIZED SHARPE vs BACKTEST SHARPE

**The Final Scorecard:**

| Strategy | Backtest Sharpe | Week 2 Live | Ratio | Verdict |
|----------|-----------------|------------|-------|---------|
| Whale-Follow | 1.7 | 1.2 | 0.71 | Good (decay normal) |
| BSTS News | 1.3 | 1.1 | 0.85 | Excellent |
| Pairs Trading | 1.1 | 0.7 | 0.64 | Acceptable |
| Lee-Mykland | 1.0 | 0.4 | 0.40 | Problematic |

**Acceptance Criteria:**
- Whale-Follow: 0.6x backtest or higher
- BSTS News: 0.7x backtest or higher
- Pairs Trading: 0.6x backtest or higher
- Portfolio: 0.7x backtest or higher

**Why It Matters:**
- This is THE metric for "ready for live"
- Natural decay: 20-30% is normal
- Decay >40%: Something is broken
- Decay <10%: Backtest was conservative (good luck!)

---

## Red Flags: Stop Trading Immediately If...

1. **Win rate drops below 40%** (all strategies, all days)
   - Indicates fundamental model failure
   - Not a luck event, something is broken

2. **Single trade loss >$500** (on $32k deployment)
   - Exceeds risk limits
   - Indicates position sizing or execution failure

3. **Data gap >100 trades in a day**
   - Can't trust market stats or P&L
   - Stop trading until data is clean

4. **Order rejection rate >5%**
   - Liquidity dry-up or API failure
   - Can't execute the strategies

5. **Three consecutive days Sharpe <0.5**
   - Not variance, it's a trend
   - Time to diagnose and fix

6. **Correlation between strategies >0.8**
   - Diversification is broken
   - All strategies failing for same reason

7. **Drawdown >$600 in a week**
   - Risk management isn't working
   - Reduce position sizes immediately

---

## Daily Paper Trading Routine (10 Minutes)

```
Morning (after market opens):
  1. Check: All 4 strategies running? (Y/N)
  2. Check: Any error logs? (Y/N)
  3. Check: Any order rejections? (Y/N)

Afternoon (end of trading):
  4. Calculate: Daily Sharpe
  5. Check: Max daily drawdown
  6. Check: Win rates vs expected
  7. Audit: 10 largest trades for slippage
  8. Verify: P&L reconciliation
  9. Check: Data quality (gaps, duplicates)
  10. Decision: Continue or pause?
```

---

## Week 1 Exit Points

```
If by Day 3:
  - Portfolio Sharpe < 0.5: HALT, diagnose
  - Any strategy negative: INVESTIGATE
  - Slippage > 50bps: REDUCE position sizes
  
If by Day 7:
  - Portfolio Sharpe < 0.7: EXTEND by 1 week with reduced capital
  - Any strategy Sharpe < 0.5: DISABLE that strategy
  - Cumulative loss > $500: HALT, revise assumptions
```

---

## Scaling Plan Based on Week 1 Results

```
Sharpe >= 1.2:
  Scale to 50% capital ($16k deployed)
  Continue Week 2-3

Sharpe 0.9-1.2:
  Hold at 25% capital
  Continue Week 2-3, monitor closely

Sharpe 0.7-0.9:
  Reduce to 15% capital ($5k deployed)
  Extend paper trading by 2 weeks

Sharpe < 0.7:
  Halt all trading
  Spend 1 week diagnosing
  Restart only after fix
```

---

## You're Ready for Live When...

✓ Portfolio Sharpe >= 1.2 for 2 consecutive weeks  
✓ Win rates within 10% of backtest for all strategies  
✓ Max daily drawdown < $300 (no outliers)  
✓ Zero data quality issues over 2 weeks  
✓ Execution slippage matches CLOB simulator <20%  
✓ Individual strategies profitable in isolation  

**Then: Deploy 100% capital to live market.**

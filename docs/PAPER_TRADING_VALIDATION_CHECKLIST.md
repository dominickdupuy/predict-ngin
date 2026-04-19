# Paper Trading Validation Checklist

**Goal:** Bridge gap between backtest and live trading. Validate assumptions before risking capital.

**Duration:** 2-4 weeks at 25% capital allocation

---

## Phase 1: Pre-Launch (Day 1)

### A. Execution Model Validation
- [ ] CLOB simulator matches actual Polymarket fills on sample trades
  - Execute 5 test trades on each strategy
  - Compare simulator cost estimate vs actual slippage
  - Acceptable deviation: <20% (e.g., 30bps estimated, 36bps actual)
  - **Red flag:** Spread assumption is 50%+ off

- [ ] Order routing works correctly
  - Test on liquid market (>$1M volume)
  - Test on medium market ($100k-500k)
  - Test position size scaling
  - Acceptable: All orders fill within 10 minutes

- [ ] Market data latency is acceptable
  - Measure trade delay (signal generation -> order submission)
  - Acceptable: <2 minutes for news-driven (BSTS)
  - Acceptable: <10 seconds for pairs trading
  - **Red flag:** Delays > 30 seconds mean signals are stale

### B. Signal Generation Validation
- [ ] Whale-Follow signals are actually coming from liquid markets
  - Sample 100 signals
  - Check what % are >$500k volume markets
  - Expected: >80% on liquid markets
  - **Red flag:** If <50%, threshold should be $750k, not $500k

- [ ] BSTS news detection is working
  - Manually verify 10 news events trigger signals
  - Check that decomposition correctly identifies permanent vs transient
  - Expected: False positive rate <20%
  - **Red flag:** If >40% false positives, model is broken

- [ ] Pairs trading signals are in mean-reversion regime
  - Sample 50 pairs signals
  - Manually check: are prices actually reverting to mean?
  - Expected: Mean reversion within 1-4 hours
  - **Red flag:** If prices continue diverging, z-score threshold is too tight

- [ ] Lee-Mykland jumps are actually jumps
  - Sample 10 jump signals
  - Manually verify they're not just noise
  - Expected: Visible price spike
  - **Red flag:** If noise/signal ratio >50%, jump detection is overfitting

### C. Risk Infrastructure Check
- [ ] Position limits are enforced
  - Set limit to $500 per trade
  - Try to submit $1k order -> should fail
  - Acceptable: Immediate rejection with clear error message

- [ ] Drawdown monitoring works
  - Set max daily loss to $100
  - Lose $99 on a trade -> trading continues
  - Lose $101 on a trade -> trading halts
  - Acceptable: Halt triggers precisely at limit

- [ ] Exposure limits per market
  - Try to accumulate >$5k on single market -> should fail
  - Acceptable: Clear warning and rejection

---

## Phase 2: Live Paper Trading (Week 1)

### A. Execution Cost Measurement
**Track every trade and measure realized slippage vs backtest assumption.**

- [ ] Measure spreads on liquid markets
  - Sample 100 whale-follow trades
  - Calculate actual fill price vs mid
  - Expected: 30-35bps on liquid markets
  - **Yellow flag:** 35-50bps (acceptable but tight)
  - **Red flag:** 50-100bps (threshold should be higher)

- [ ] Measure market impact
  - For each large position ($1k+), measure impact
  - Calculate: (exit_price - entry_price) / size
  - Expected: Should match sqrt(size/depth) model
  - **Red flag:** If actual impact 2x larger, model is wrong

- [ ] Measure slippage by time of day
  - Does execution cost vary morning vs evening?
  - Expected: Should be flat across US trading hours
  - **Red flag:** If spreads widen after 6pm ET, limit trading hours

### B. Signal Quality Measurement
**For each strategy, measure realized win rate vs backtest.**

| Strategy | Backtest Win Rate | Paper Trading Target | Acceptable Range |
|----------|------------------|----------------------|------------------|
| Whale-Follow | 79.8% | 70-80% | >65% |
| BSTS News | 94.2% (unreliable) | 60-70% | >50% |
| Pairs Trading | 97.9% (unreliable) | 65-75% | >55% |
| Lee-Mykland | 41.2% | 35-45% | >30% |

- [ ] Whale-Follow win rate
  - Target: 70-80% (backtest 79.8% likely overstated)
  - If <65%: Signal quality degraded, check market regime
  - If >85%: Backtest assumptions were conservative (upside!)

- [ ] BSTS News win rate
  - Target: 60-70% (backtest 94.2% is unrealistic)
  - If <50%: Decomposition model has issues
  - If >75%: Great! Scale capital

- [ ] Pairs Trading win rate
  - Target: 65-75% (backtest 97.9% is definitely wrong)
  - If <60%: Too many whipsaws, increase z-score threshold to 2.5
  - If >80%: Increase z-score to 1.5 for more signals

- [ ] Lee-Mykland hit rate
  - Target: 40-50%
  - If <30%: Too many false positives, increase threshold
  - If >60%: Threshold is too loose, tighten for better edge

### C. Profitability Per Strategy
**Measure if each strategy is profitable in isolation.**

- [ ] Whale-Follow: Expected $50-100/week at $20k allocation
  - Calculate: PnL / allocation / weeks
  - Expected Sharpe: >1.5 (annualized)
  - If negative: Stop immediately, revert to backtest diagnostics

- [ ] BSTS News: Expected $50-80/week at $5k allocation
  - Should be highest Sharpe (1.2-1.5)
  - If losses: News detection is broken

- [ ] Pairs Trading: Expected $25-50/week at $5k allocation
  - Should be consistent (lower Sharpe ~1.0)
  - If declining: Market may be in trend mode

- [ ] Lee-Mykland: Expected $10-20/week at $2k allocation
  - Lowest expected return
  - If losses >2 consecutive days: Disable

### D. Correlation & Diversification
**Verify strategies aren't all failing together.**

- [ ] Day 1-3: Test all strategies simultaneously
  - If all lose money on same day: Likely data/model issue
  - If different strategies lose on different days: Diversification working
  - Expected: Max correlation 0.3 between strategies

- [ ] Measure portfolio Sharpe
  - Individual Sharpes: Whale (1.7), BSTS (1.3), Pairs (1.0), Jumps (0.9)
  - Portfolio Sharpe (with diversification): Should be >1.5
  - If <1.0: Strategies are correlated or failing

---

## Phase 3: Regime Detection (Week 2)

### A. Market Regime Analysis
**Strategies perform differently in trending vs mean-reverting markets.**

- [ ] Whale-Follow performance by regime
  - Trending markets: Whales drive momentum
  - Mean-reverting markets: Whales cause overshoots
  - Track: Win rate in trending vs reverting
  - If win rate drops >20% in trending: Implement trend filter

- [ ] Pairs Trading performance by regime
  - Trending markets: Z-scores widen (pairs diverge)
  - Mean-reverting markets: Z-scores mean-revert quickly
  - Track: Hold time in each regime
  - If avg hold time >4 hours in trending: Too many whipsaws

- [ ] BSTS News performance by regime
  - News impact should be independent of regime
  - If not: Check for macro regime (high vol vs low vol)
  - Track: Win rate in high vol vs low vol days

### B. Volatility Regime Monitoring
- [ ] Track VIX equivalent for prediction markets
  - High volatility: Win rates likely decrease
  - Low volatility: Whales more predictable
  - Action: Scale position size by volatility regime

- [ ] Identify "black swan" days
  - Days where all strategies underperform
  - Expected: 1-2 per month
  - Action: Auto-halt if daily P&L < -$200

---

## Phase 4: Parameter Fine-Tuning (Week 3)

### A. Position Size Optimization
**Current backtest uses fixed sizes. Live trading may require dynamic sizing.**

- [ ] Test 50% smaller positions
  - Compare win rate: smaller positions (less market impact)
  - vs current positions (more capital efficient)
  - Decision: If win rate improves >5%, use smaller sizes

- [ ] Test 50% larger positions
  - Check if market impact erases edge
  - Expected: Win rate should drop (impact penalty)
  - Decision: Only use larger sizes if capital excess

### B. Signal Threshold Tuning
**Backtests may have overfit parameters. Live data often requires adjustment.**

- [ ] Whale-Follow: Adjust CATE filter threshold
  - Current: Trades top 100 whale traders
  - Test: Top 50 (higher conviction) vs top 200 (more signals)
  - Measure: Win rate vs trade frequency tradeoff

- [ ] Pairs Trading: Adjust z-score threshold
  - Current: 2.0
  - Test: 2.5 (fewer, higher-conviction trades)
  - Test: 1.5 (more frequent but noisier)
  - Decision: Choose threshold that maximizes Sharpe (not win rate)

- [ ] Lee-Mykland: Adjust jump detection sensitivity
  - Current: sqrt(log(1/0.01) / len(returns))
  - If false positives >50%: Increase threshold by 20%
  - If missing jumps: Decrease by 20%

### C. Holding Period Optimization
**Backtest uses fixed hold periods. Live trading may benefit from adaptive exits.**

- [ ] Whale-Follow: Current hold ~5-10 min
  - Measure: What % of max profit is captured in first 2 min vs 10 min?
  - If 80% captured in 2 min: Exit earlier to reduce slippage

- [ ] BSTS News: Current hold 4-6 hours
  - Measure: Half-life of mean reversion
  - If mean reverts in <2 hours: Exit early
  - If still moving at 6 hours: Hold longer

---

## Phase 5: Drawdown & Risk Events (Week 4)

### A. Stress Test Scenarios
**What happens if assumptions break?**

- [ ] Liquidity event (top 10 markets become illiquid)
  - Simulation: Increase spreads on 10 markets to 200bps
  - Expected: Impact on revenue ~2-5% (diversification helps)
  - Action: If impact >10%, need better market selection

- [ ] News shock (unexpected major market movement)
  - Simulation: One market moves 20% instantly
  - Expected: Whale-follow loses on that market, but others unaffected
  - Action: Monitor correlation breakdown

- [ ] System outage (API down for 10 minutes)
  - Simulation: Stop trading for 10 min, then resume
  - Expected: Signals come back in, some stale
  - Action: Measure cost of stale signals

### B. Drawdown Limits
- [ ] Set hard stop: Daily loss > $200 -> halt trading
- [ ] Yellow flag: Daily loss > $100 -> reduce position size 50%
- [ ] Win rate drop: If <50% for 2 consecutive days -> pause
- [ ] Drift detection: If live Sharpe < 0.5x backtest -> investigate

---

## Phase 6: Data Quality & Infrastructure (Throughout)

### A. Data Feed Validation
- [ ] No missing trades (gaps in timestamp)
- [ ] No duplicate trades (timestamp collision)
- [ ] Prices stay in [0.0, 1.0] bounds
- [ ] Order book depth estimates are realistic
- [ ] **Check daily:** Audit logs for errors

### B. Order Management
- [ ] Orders are executed in correct order (FIFO)
- [ ] No partial fills that aren't accounted for
- [ ] Cancelled orders are properly logged
- [ ] Position tracking is accurate (compare to trades log)
- [ ] **Check daily:** Reconcile positions vs live orders

### C. P&L Reconciliation
- [ ] Manual verification of first 10 trades
  - Calculate: (exit_price - entry_price) * quantity - costs
  - Compare to system P&L
  - Acceptable: <1% difference
  - **Red flag:** >5% mismatch indicates calculation error

- [ ] Daily P&L reconciliation
  - Sum all trades for the day
  - Compare to reported daily P&L
  - Acceptable: Match exactly (within rounding)

---

## Scaling Decision Tree

### Week 1 Results
```
IF Sharpe >= 0.7 * backtest_sharpe AND win_rate >= 90% * backtest_target:
  SCALE to 50% capital
ELSE IF Sharpe >= 0.5 * backtest_sharpe AND win_rate >= 80% * backtest_target:
  HOLD at 25% capital, continue diagnostics
ELSE:
  HALT all strategies, investigate
```

### Week 2-3 Results
```
IF Sharpe >= 0.8 * backtest_sharpe AND no drawdown violations:
  SCALE to 100% capital
ELSE IF Sharpe >= 0.6 * backtest_sharpe AND drawdown <5%:
  HOLD at 50% capital
ELSE:
  SCALE back to 25% or halt
```

### Week 4 Decision
```
IF overall_sharpe >= 1.2 AND max_daily_drawdown <= 5%:
  DEPLOY to live (STOP paper trading)
ELSE IF overall_sharpe >= 1.0 AND max_daily_drawdown <= 10%:
  EXTEND paper trading 2 more weeks
ELSE:
  ARCHIVE strategy, revise assumptions
```

---

## Success Criteria (Minimum Viable Paper Trading Results)

| Metric | Target | Red Flag |
|--------|--------|----------|
| Whale-Follow Sharpe | >1.2 | <0.8 |
| BSTS News Sharpe | >1.0 | <0.6 |
| Pairs Trading Sharpe | >0.8 | <0.5 |
| Portfolio Sharpe | >1.2 | <0.8 |
| Max Daily Drawdown | <$200 | >$400 |
| Largest Win Rate Miss | <15% | >25% |
| Execution vs Backtest | <20% slippage miss | >50% |
| Data Quality | 0 gaps, 0 duplicates | >1 error/day |

---

## Common Failure Modes (Watch For)

### 1. Backtest Overfitting
- **Symptom:** Live win rate 30% below backtest
- **Cause:** Parameters tuned to historical data quirks
- **Action:** Loosen thresholds, reduce false-positives

### 2. Data Quality Issues
- **Symptom:** Sharpe degrades after 1 week
- **Cause:** Data feed degradation or gaps
- **Action:** Audit data quality daily, validate against exchange

### 3. Liquidity Dry-up
- **Symptom:** Slippage suddenly 2x larger
- **Cause:** Market concentration shift or exchange downtime
- **Action:** Tighten market selection filter

### 4. Signal Drift
- **Symptom:** Win rate slowly declining
- **Cause:** Market regime change or seasonal effect
- **Action:** Add regime detector, adjust thresholds

### 5. Infrastructure Lag
- **Symptom:** Execution prices worse than expected
- **Cause:** API latency or order routing delays
- **Action:** Measure latency, optimize connections

---

## Exit Criteria (When to Stop)

Stop paper trading immediately if:
- [ ] Live Sharpe < 0.4 for 3+ consecutive days
- [ ] Win rate drops below 40% (all strategies)
- [ ] Single trade loss > 50% of capital allocated
- [ ] Data feed has >10 missing trades in a day
- [ ] Orders not executing (API timeout, network issues)
- [ ] P&L reconciliation fails >3 days in a row

Resume after:
- Diagnosing root cause
- Fixing model/infrastructure
- Validating fix in offline backtest
- Getting sign-off on revised assumptions

---

## Daily Paper Trading Checklist (Repeat Each Day)

- [ ] All 4 strategies running without errors
- [ ] Orders executing within expected slippage range
- [ ] Daily P&L calculated and reconciled
- [ ] Win rates tracking backtest assumptions (within 10%)
- [ ] No suspicious data gaps or duplicates
- [ ] Position limits not exceeded
- [ ] Portfolio Sharpe tracking >0.6x live target
- [ ] Largest single-day loss <$200
- [ ] Review execution logs for anomalies

---

## Final Paper Trading Decision (After 4 Weeks)

**DECISION TREE:**

```
Portfolio Sharpe >= 1.2?
  YES -> Excellent. Deploy to live with 100% allocated capital.
  NO  -> Question 2.

Live win rates within 10% of backtest?
  YES -> Good. Model is stable. Deploy with 50% capital, scale in.
  NO  -> Question 3.

Can you identify the discrepancy?
  YES -> Model drift detected. Fix and re-test 1 week. Then deploy.
  NO  -> Question 4.

Are individual strategies profitable?
  YES -> Portfolio might be negatively correlated. Adjust allocations.
        Deploy only profitable strategies (e.g., BSTS + Pairs).
  NO  -> Model is broken. Archive all strategies.
        Return to backtesting phase. Revise assumptions.
```

---

## Success Metrics for Live Deployment

If you reach the end of 4-week paper trading and:
- ✓ Portfolio Sharpe > 1.2
- ✓ Win rates within 10% of backtest
- ✓ Max daily drawdown < $300
- ✓ No data quality issues
- ✓ All risk limits functioning correctly

**You're ready to deploy to live with real capital.**

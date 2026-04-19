# V3 Cost Model Audit

**Date**: 2026-04-19
**Scope**: Fee schedule, slippage model, and execution routing in `backtest_v3`.

## Findings

### 1. Engine executes every Signal as a taker (MAJOR)

`BacktestEngine.run` calls `executor.execute_market(book, side, notional)` for
every entry (engine.py:214) regardless of whether the Signal specifies a
`limit_price`. `RoundPriceLP` emits `features["limit_price"]` with the intent
of being backtested as a maker that collects spread — but the engine walks
the book as a taker that pays it.

**Impact**: The backtest is strictly pessimistic for any maker-intent
strategy. RoundPriceLP's reported `avg_entry_cost_bps` of 4,638–8,865 is
computed against the full round-trip of crossed spread + 20bps fee, when in
reality a filled maker quote would have negative slippage (rebate) and no
fee.

**Fix**: Route Signals that carry `features["limit_price"]` through a new
maker-fill path. A limit quote is considered filled if, within a
`maker_fill_window_s`, a contra-side trade prints at or through the limit
price. Fills receive `avg_fill_price = limit_price` (the quote price) and
`slippage_bps = -(half_spread_bps)` — a rebate relative to mid, because the
maker captured the spread.

### 2. Default taker fee overstates real cost (MODERATE)

`EngineConfig.taker_fee_bps = 20.0` (engine.py:140). On Polymarket's live
markets today:

| field | mean across 500 Politics markets | interpretation |
|---|---|---|
| `makerBaseFee` | 0 | makers pay zero |
| `takerBaseFee` | 2.15 (mode = 0, a few = 200) | almost all current markets = 0 bps |
| `feesEnabled` | 0 (all False) | fee collection disabled fleet-wide |

**Impact**: 20 bps × 2 legs = 40 bps round-trip of fee subtracted from every
trade that shouldn't have been there. On $250 notional that's $1.00/trade
of fictional cost — not fatal but cumulative across 15+ trades.

**Fix**: Default `taker_fee_bps = 0.0`. Keep it configurable so a future
fee-schedule change can be modeled by overriding at the EngineConfig call
site.

### 3. Reconstructor overstates spreads in the mid-liquid band (MODERATE)

`CLOBBookReconstructor` interpolates half-spreads between `liquid_spread_half
= 0.005` (0.5¢) at $500k+ volume and `thin_spread_half = 0.025` (2.5¢)
below. The $100k–$300k liquidity band — where V3 strategies actually fire
— gets interpolated toward the thin estimate.

Ground truth from the markets table (live snapshot, 500 markets):

```
spread      mean = $0.0132   min = $0.001   max = $1.00
```

Median live spread is ~1.3¢ = 0.65¢ half-spread. Our reconstructor estimates
~2-2.5¢ half-spread in the target band — a **3-4× overstatement**.

**Fix**: Tighten `thin_spread_half` from 2.5¢ to 1.0¢ (1¢ half = 2¢ total,
still conservative vs the 1.3¢ market median and captures true thin
markets). Keep `liquid_spread_half = 0.005` (matches liquid-market reality).

### 4. Polymarket maker rewards not modeled (INFORMATIONAL)

Markets carry `rewardsMaxSpread` (mean 3.2¢) and `rewardsMinSize` (mean
$120). Quoting inside the reward spread with at least the min size earns a
daily rebate paid by Polymarket. We do not simulate this subsidy. A
real-money maker deployment of `RoundPriceLP` would collect it; the
backtest therefore underestimates net carry for any maker strategy. Logged
as a known conservative-bias item — not fixed in this pass.

### 5. Gas / settlement costs (INFORMATIONAL)

Polymarket settles on Polygon. Per-trade gas is <$0.01, immaterial at our
$100–500 notional and ignored.

## Fixes applied in this pass

- [x] `EngineConfig.taker_fee_bps` default → `0.0`
- [x] `CLOBBookReconstructor.thin_spread_half` default → `0.010`
- [x] New maker-fill path for Signals with `features["limit_price"]`
- [x] Updated `_trade_pnl` to handle maker rebate sign correctly
- [ ] Maker rewards subsidy — deferred

## Conservative fallback

The new engine exposes `force_taker_execution: bool = False` in
`EngineConfig`. Setting it to `True` reverts to the pre-audit behavior — a
"would this survive if I had to take every fill" stress test. Use it as a
pessimistic bound before sizing a live deployment.

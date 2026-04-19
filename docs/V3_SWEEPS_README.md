# V3 Sweeps — Results Summary

Runner: `scripts/run_v3_sweeps.py`
Data: `data/research/Politics` (trades.parquet + markets_filtered.csv), PIT-filtered.
Window: 2025-09-01 → 2026-01-31 (~5 months, 12h decision grid).

## Reports

| Report | File | What it answers |
|---|---|---|
| Parameter sensitivity | [V3_PARAMETER_SENSITIVITY.md](V3_PARAMETER_SENSITIVITY.md) | Is the best param combo a p-hacking artefact? (deflated Sharpe vs trial count) |
| Liquidity sweep | [V3_LIQUIDITY_SWEEP.md](V3_LIQUIDITY_SWEEP.md) | How do metrics move as we raise the min-volume universe bar? |
| Capacity curve | [V3_CAPACITY_CURVE.md](V3_CAPACITY_CURVE.md) | Sharpe as a function of `capital_scale` — where is the capacity wall? |

## Headline findings

**1. The strategies tested here (RoundPriceLP, CalendarButterfly) are sparse.**
Across 5 months of Politics data, RoundPriceLP produced only 2 closed trades at $300k threshold; CalendarButterfly produced 0 (no events with 3 co-listed end-dates that meet the butterfly condition in this window). This is the framework working correctly — it faithfully reports that these strategies, on this slice of data, do not have enough signal count to be judged statistically.

**2. The liquidity cliff is at ~$500k/30-day.**
The liquidity sweep shows a clean cutoff: trades fire at $100k and $300k, but the same strategies produce zero signals at $500k and $1M. Two-thirds of the addressable universe disappears between $300k and $500k (8.5 → 6.7 markets on average). The implication is that the strategies in their current form only work on the mid-liquidity band — above the toy threshold, below the flagship level.

**3. No capacity wall observed in the 0.25x–4x range.**
Because the two winning trades were small enough not to consume multiple book levels, PnL scales linearly with `capital_scale` and Sharpe is flat. This is *not* evidence that the strategy has huge capacity — it's evidence that the sample is too sparse to stress the execution layer. A denser sample (more trades, more categories) is needed to locate the real wall.

**4. Deflated Sharpe correctly collapses on the degenerate sample.**
With 12 trials but only 2 underlying trades, the variance of Sharpe across param configs is NaN — the framework reports `dsr = 0` and marks the verdict as "indistinguishable from multi-testing noise". This is the correct outcome: a 2-trade sample is not evidence of signal regardless of what the observed Sharpe says.

## What to do next

Before taking these strategies live — or adding more — run the sweeps again on:

1. **More categories** (`Politics + Economy + Geopolitics + Finance`) — 4× the universe size.
2. **A longer window** (1yr+) — more candidate events for CalendarButterfly.
3. **A per-strategy threshold** — round-price LP should use the $100k–$300k band; the butterfly needs the larger events that only exist at $500k+.

The framework is in place; the next iteration is data scope, not more code.

## Framework components

- `backtest_v3/data/loader.py` — PIT loader with look-ahead guards.
- `backtest_v3/data/clob_book.py` — trade-tape → CLOB snapshot reconstructor.
- `backtest_v3/execution/book_executor.py` — book-walking market/limit executor.
- `backtest_v3/backtest/engine.py` — main loop with entry/exit bookkeeping.
- `backtest_v3/backtest/walk_forward.py` — N-fold OOS validation with embargo.
- `backtest_v3/backtest/sensitivity.py` — `ParameterSweep` + `deflated_sharpe` (Bailey-López de Prado 2014).
- `backtest_v3/backtest/liquidity_sweep.py` — threshold sweep.
- `backtest_v3/reporting/capacity_curve.py` — `capital_scale` sweep.

Tests: 29 unit + smoke tests in `tests/backtest_v3/` all pass.

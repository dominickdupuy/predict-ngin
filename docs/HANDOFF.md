# Handoff — V3 paper-trading evaluation

**Last updated:** 2026-04-19
**Machine migration:** stopping mid-run on old machine; resume on new machine.

## Current status

All cost-model and edge-shape fixes landed. The 1h ranking rerun was launched
and killed before completing — it needs to be rerun on the new machine, then
the menagerie DSR needs to be recomputed on the fresh numbers.

## Open tasks (in order)

1. **Rerun ranking at 1h step.** `scripts/rank_v3_strategies.py` already has
   `STEP_HOURS = 1`. Run:

   ```bash
   cd C:/Users/domdd/Documents/GitHub/predict-ngin
   PYTHONIOENCODING=utf-8 PYTHONWARNINGS=ignore python -u scripts/rank_v3_strategies.py 2>&1 | tee docs/rank_1h_run.log
   ```

   Expected duration: 1–8h depending on machine. Overwrites
   `docs/V3_STRATEGY_THRESHOLDS.csv`, `V3_STRATEGY_WALK_FORWARD.csv`,
   `V3_STRATEGY_RANKING.csv`, `V3_STRATEGY_RANKING.md`.

2. **Recompute menagerie DSR** on the fresh thresholds CSV:

   ```bash
   python scripts/compute_menagerie_dsr.py
   ```

   Overwrites `docs/V3_MENAGERIE_DSR.md`.

3. **Report top candidate + paper-trading recommendation.** Previous top on
   12h data was `round_price_lp` (composite 0.31). Post-fix numbers should
   look materially different — fees dropped to 0, spread half dropped to
   1¢, maker fills now simulated, stop/trail now enforced.

## What landed (committed in this push)

### Cost-model fixes
- `src/backtest_v3/backtest/engine.py`
  - `EngineConfig.taker_fee_bps` default 20.0 → 0.0
  - Added `force_taker_execution: bool = False`
  - `_OpenPosition` gained `entry_mid`, `max_favorable_mid`, `trail_armed`
  - New `_maybe_maker_fill`: if Signal has `limit_price`, scan trade tape for
    contra-side trades crossing the limit within `maker_fill_window_s`, fill
    at limit with rebate-style negative slippage_bps
  - Rewrote `_exit_condition_met` to enforce `stop_loss_bps` and trailing
    stop (`trail_trigger_bps` + `trail_giveback_bps`)
- `src/backtest_v3/data/clob_book.py` — `thin_spread_half` 0.025 → 0.010
  (live median on Polymarket is ~1.3¢)
- `src/backtest_v3/execution/book_executor.py` —
  `ExecutorConfig.taker_fee_bps` default 0.0

### Signal schema extension
- `src/backtest_v3/strategies/base.py` — added optional fields:
  `limit_price`, `maker_fill_window_s`, `stop_loss_bps`, `trail_trigger_bps`,
  `trail_giveback_bps`

### Strategy updates
- `src/backtest_v3/strategies/round_price_lp.py` — emits `limit_price`,
  cluster-quality sizing (`size_cap=3x`, scales with
  `cluster_count / min_cluster`), full stop/trail config

### New analysis scripts
- `scripts/audit_round_price_lp.py` — manual book reconstruction audit of a
  random sample of round_price_lp trades
- `scripts/analyze_yes_no_arb.py` — scans prices.parquet for YES+NO<$1 arb
  opportunities (concluded: not buildable from current extracted data — see
  `docs/YES_NO_ARB_ANALYSIS.md`)
- `scripts/compute_menagerie_dsr.py` — Bailey-López de Prado DSR across
  (strategy × threshold) menagerie

### Docs
- `docs/COST_MODEL_AUDIT.md` — cost issues + maker-reward/gas notes
- `docs/YES_NO_ARB_ANALYSIS.md` — data gap; cannot evaluate
- `docs/V3_MENAGERIE_DSR.md` — DSR from **pre-fix 12h** data; needs rerun
  (DSR firing=0.80, DSR all=0.56, verdict "Marginal")

## Known findings

- **round_price_lp audit:** 15 trades, $27,905 PnL on 12h data — but 2
  lottery-ticket trades contributed $27,988. The other 13 summed to –$82.
  Median trade: –$5.95. Strategy is an accidental longshot, not a consistent
  edge. Post-fix stop-loss (300 bps) should cut the longshots; need to see
  if any edge remains.

- **YES/NO arb:** Cannot be built from current data.
  - `trades.parquet` PIT rows (timestamp>0) have NULL `nonusdc_side` — can't
    tell YES leg from NO leg.
  - Legacy rows (timestamp=0) have the token tag but no usable timestamp.
  - `prices.parquet` has YES-only snapshots for a single headline market
    per category.
  - To evaluate: pull per-minute bid/ask on both token IDs from CLOB client.

- **Cost reality** (from `data/research/markets/markets_filtered.csv`):
  - `feesEnabled=False` fleet-wide
  - `makerBaseFee=0` on all sampled markets
  - Live median spread ~1.3¢, not 5¢
  - Gas: deposit/withdraw only, not per-trade (Polygon, negligible)
  - Maker rewards: real but not modeled — LP-style programs pay makers who
    post near touch in qualifying markets

## Tests

29 tests pass. Re-verify on new machine:
```bash
python -m pytest tests/backtest_v3/ -q
```

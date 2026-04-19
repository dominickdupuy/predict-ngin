# V3 Menagerie Deflated Sharpe

**Source:** `docs\V3_STRATEGY_THRESHOLDS.csv`  (12 rows)
**Firing trials:** 4 of 12

## Inputs
- Best observed Sharpe: **33.889** (strategy=`hazard_ladder`, threshold=$100,000)
- Trades in best config: 3
- Sharpe variance across trials: 356.2774

## Deflated Sharpe

| trial_pool | n_trials | cutoff_SR | z | DSR |
|---|---|---|---|---|
| all trials (incl. zero-firing) | 12 | 31.424 | 0.145 | **0.5578** |
| firing trials only | 4 | 19.859 | 0.827 | **0.7960** |

## Verdict

**Marginal — cannot reject null at 95% across the menagerie.**

## Per-strategy Sharpes

| strategy             |   threshold_usd |   n_trades |   sharpe |   total_pnl_usd |
|:---------------------|----------------:|-----------:|---------:|----------------:|
| round_price_lp       |          100000 |         67 |   5.9581 |      75318.6    |
| round_price_lp       |          300000 |         15 |   6.654  |      27905.2    |
| round_price_lp       |          500000 |          2 | -11.8588 |       -222.499  |
| hazard_ladder        |          100000 |          3 |  33.8887 |       1423.33   |
| uma_dispute_discount |          100000 |          1 | nan      |        -94.9176 |

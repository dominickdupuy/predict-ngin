# V3 Strategy Ranking — Paper Trading Candidates

**Window:** 2025-02-14 -> 2026-02-14 (12 months)
**Categories:** Politics, Economy, Geopolitics, Finance
**Decision step:** 12h
**Liquidity thresholds tested:** $100,000, $300,000, $500,000

## Ranked Candidates

| strategy             |   signal_density_per_mo |   mean_sharpe_across_thr |   threshold_robustness |   composite_score |
|:---------------------|------------------------:|-------------------------:|-----------------------:|------------------:|
| round_price_lp       |                    1.25 |                   0.2511 |                 1      |            0.3139 |
| calendar_butterfly   |                    0    |                   0      |                 0      |            0      |
| hazard_ladder        |                    0    |                  33.8887 |                 0.3333 |            0      |
| uma_dispute_discount |                    0    |                 nan      |                 0.3333 |          nan      |

## Per-threshold metrics

| strategy             |   threshold_usd |   wall_s |   total_pnl_usd |   n_trades |   n_days_with_pnl |   mean_pnl_per_trade_usd |   hit_rate |   sharpe |   sortino |   max_drawdown_usd |   avg_entry_cost_bps |   avg_exit_cost_bps |
|:---------------------|----------------:|---------:|----------------:|-----------:|------------------:|-------------------------:|-----------:|---------:|----------:|-------------------:|---------------------:|--------------------:|
| round_price_lp       |          100000 | 151.392  |      75318.6    |         67 |                53 |                1124.16   |     0.5075 |   5.9581 |  137.735  |          -817.497  |             4638.58  |            8639.14  |
| round_price_lp       |          300000 |  72.2439 |      27905.2    |         15 |                11 |                1860.35   |     0.4    |   6.654  |  411.806  |          -227.995  |             8865     |           13556.7   |
| round_price_lp       |          500000 |  61.3132 |       -222.499  |          2 |                 2 |                -111.249  |     0      | -11.8588 |  -11.8588 |            -5.9457 |              277.879 |             193.793 |
| calendar_butterfly   |          100000 | 500.628  |          0      |          0 |                 0 |                   0      |     0      |   0      |    0      |             0      |                0     |               0     |
| calendar_butterfly   |          300000 | 207.705  |          0      |          0 |                 0 |                   0      |     0      |   0      |    0      |             0      |                0     |               0     |
| calendar_butterfly   |          500000 | 145.694  |          0      |          0 |                 0 |                   0      |     0      |   0      |    0      |             0      |                0     |               0     |
| hazard_ladder        |          100000 | 518.329  |       1423.33   |          3 |                 2 |                 474.445  |     1      |  33.8887 |    0      |             0      |              391.574 |           47418.4   |
| hazard_ladder        |          300000 | 208.779  |          0      |          0 |                 0 |                   0      |     0      |   0      |    0      |             0      |                0     |               0     |
| hazard_ladder        |          500000 | 148.933  |          0      |          0 |                 0 |                   0      |     0      |   0      |    0      |             0      |                0     |               0     |
| uma_dispute_discount |          100000 | 418.137  |        -94.9176 |          1 |                 1 |                 -94.9176 |     0      | nan      |    0      |             0      |             4353.33  |            4725.88  |
| uma_dispute_discount |          300000 | 184.154  |          0      |          0 |                 0 |                   0      |     0      |   0      |    0      |             0      |                0     |               0     |
| uma_dispute_discount |          500000 | 131.44   |          0      |          0 |                 0 |                   0      |     0      |   0      |    0      |             0      |                0     |               0     |

## Walk-forward stability

| strategy             |   wf_folds |   wf_positive_fold_fraction |   wf_sharpe_stability |   wf_mean_sharpe |   wf_total_trades |
|:---------------------|-----------:|----------------------------:|----------------------:|-----------------:|------------------:|
| round_price_lp       |          2 |                           1 |                  0.92 |           6.7427 |                15 |
| calendar_butterfly   |          2 |                           0 |                  1    |           0      |                 0 |
| hazard_ladder        |          2 |                           0 |                  1    |           0      |                 0 |
| uma_dispute_discount |          2 |                           0 |                  1    |           0      |                 0 |

## Recommendation

**Top candidate:** `round_price_lp`  (score=0.31)

- Fires ~1.2 trades/month at the $300k threshold.
- Mean Sharpe across thresholds where it fires: 0.25.
- Fires at 100% of tested thresholds.


### Suggested paper-trading setup
- Initial capital: **\$5,000** (enough for ~20 $250-notional trades).
- Liquidity threshold: **\$300,000**/30-day rolling (best-scoring band).
- Kill criteria (per `docs/STRATEGY_IDEAS_V3.md` §10):
  - Live/backtest Sharpe ratio < 0.2 over 7 consecutive days → unwind.
  - Live/backtest Sharpe ratio < 0.5 over 14 consecutive days → halve notional.
  - Drawdown > $750 (15% of capital) → pause and review.

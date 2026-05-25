# Whale Following — Cash Backtest Results

**Date:** 2026-05-25  
**Script:** `scripts/backtest/whale_cash_backtest.py`  
**Data:** `data/historical/recent_trades/` (1,925,734 trades, 222,227 wallets)

## Configuration

| Parameter | Value |
|---|---|
| Starting capital | $1,000 |
| Position size | 10% of remaining cash |
| Whale qualification | min 5 trades, score > 0 |
| Scoring formula | `weighted_edge * sqrt(N) / (1 + stdev(edge))` |
| Score weights | `sqrt(capital) * sqrt(volume) * exp(-0.01 * t_days)` |
| Rolling filter | 1σ above rolling mean (window=100, warmup=30) |
| Exit timing | `closedTime` from markets.parquet, fallback = entry + 30 days |

## Results

| Metric | Value |
|---|---|
| Period | 2023-01-10 to 2025-01-03 |
| Starting capital | $1,000.00 |
| Final equity | $1,875.04 (+87.5%) |
| CAGR | +37.4% |
| Sharpe ratio | 0.32 |
| Max drawdown | 59.4% |
| Total P&L | +$875.04 |

## Trade Statistics

| Metric | Value |
|---|---|
| Trades closed | 721 |
| Win / Loss | 481 / 240 |
| Win rate | 66.7% |
| Avg position size | $26.58 |
| Avg trade ROI | +12.8% |
| Avg win ROI | +69.1% |
| Avg loss ROI | -100.0% |
| Profit factor | 1.14x |
| Warmup skipped | 30 |
| Filter rejected | 2,224 of 2,975 signals |

## Top 10 Trades by P&L

| Market | Dir | Entry | Size | P&L | ROI |
|---|---|---|---|---|---|
| Will Harvard get fewer applicants this year? | BUY | 0.020 | $13.53 | +$662.80 | +4900% |
| Will Hunter Biden be indicted by July 1, 2023? | BUY | 0.130 | $55.47 | +$371.21 | +669% |
| Another North Korea missile test by July 31? | BUY | 0.110 | $26.17 | +$211.78 | +809% |
| Will the first vote to oust McCarthy succeed? | BUY | 0.140 | $32.86 | +$201.87 | +614% |
| Will Donald Trump tweet by Sept? | BUY | 0.140 | $32.31 | +$198.50 | +614% |
| Will the Kansas City Chiefs win Super Bowl LVII? | BUY | 0.250 | $65.61 | +$196.83 | +300% |
| Will Israel regain control of all territory by Oct? | SELL | 0.900 | $20.98 | +$188.82 | +900% |
| NBA: Boston Celtics vs. Atlanta Hawks 2023-04-25 | SELL | 0.900 | $18.72 | +$168.48 | +900% |
| NBA: Boston Celtics vs. Philadelphia 76ers 2023-05 | SELL | 0.840 | $30.46 | +$159.91 | +525% |
| Did MrBeast's Twitter post make record? | BUY | 0.060 | $9.64 | +$150.99 | +1567% |

## Filter Comparison

| Filter | CAGR | Win Rate | Avg ROI | Trades | Final Equity |
|---|---|---|---|---|---|
| No filter | +24.4% | 64.3% | +5.2% | 2,961 | +$575 |
| 1σ + score > 0 | **+37.4%** | **66.7%** | **+12.8%** | **721** | **+$875** |
| 2σ + score > 0 | -32.7% | 60.8% | -15.9% | 74 | -$515 |

## Notes

- All losses are -100% (binary outcome markets, position goes to zero)
- Max drawdown of 59.4% driven by correlated directional bets (e.g. election markets)
- In-sample results — no out-of-sample validation yet
- Whale set: 606 qualified whales from 9,962 resolved markets

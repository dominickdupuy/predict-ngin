# Paper Trading Dashboard

Real-time web dashboard for monitoring paper trading positions, P&L, and performance metrics.

## Features

- **Real-time Updates**: Auto-refreshes every 5 seconds
- **Summary Metrics**: Win rate, ROI, total P&L, Sharpe ratio
- **Equity Curve**: Visual tracking of account equity over time
- **P&L Distribution**: Histogram of trade outcomes
- **Active Positions**: Table of open positions with entry prices
- **Recent Trades**: History of last 10 completed trades with P&L

## Quick Start

### Run the Dashboard

```bash
# Make sure paper trading is running in another terminal
python scripts/live/run_paper_trading.py

# In a new terminal, start the dashboard
python scripts/dashboard/paper_trading_dashboard.py
```

Then open your browser and navigate to:
```
http://localhost:8050
```

### Dashboard Components

1. **Summary Row**
   - Total Trades: number of completed trades
   - Win Rate: % of profitable trades
   - Total P&L: cumulative profit/loss
   - ROI: return on $1000 starting capital
   - Sharpe: risk-adjusted return metric
   - Max Win: largest single trade profit

2. **Equity Curve**
   - Line chart showing account equity over time
   - Interactive hover to see exact values
   - Tracks cumulative P&L including open trades

3. **P&L Distribution**
   - Histogram of trade outcomes
   - Shows clustering around small wins/losses
   - Useful for identifying strategy consistency

4. **Active Positions**
   - Table of currently open positions
   - Shows strategy, side (BUY/SELL), size, entry price
   - Updates as new trades are entered

5. **Recent Trades**
   - Last 10 completed trades
   - Shows entry/exit prices and P&L
   - Color-coded: green for wins, red for losses

## Configuration

The dashboard auto-discovers data from these paths:
- **Trades**: `data/paper_trading/trades.parquet`
- **Equity**: `data/paper_trading/equity.parquet`

No additional configuration needed. The dashboard will:
- Create output directories if they don't exist
- Gracefully handle missing data files
- Display "No data yet" during initial startup

## Browser Access

The dashboard runs on `0.0.0.0:8050`, so you can access it from:
- **Local**: `http://localhost:8050`
- **Network**: `http://<your-ip>:8050` (from other machines)

## Performance

- **Lightweight**: Minimal CPU impact (refreshes every 5 seconds)
- **Responsive**: Charts update smoothly as data arrives
- **No external dependencies**: Uses local data files only

## Troubleshooting

### Dashboard shows "No data yet"
- Make sure paper trading script is running
- Check that `data/paper_trading/` directory exists
- Wait for first trades to complete (takes ~30 seconds)

### Port already in use
```bash
# Change port in the script or use:
lsof -i :8050  # Find process using port
kill -9 <PID>   # Kill process
```

### Data not updating
- Verify paper trading process is still running
- Check file permissions on `data/paper_trading/`
- Refresh browser page

## Development

To modify the dashboard, edit `scripts/dashboard/paper_trading_dashboard.py`:

- **colors**: Adjust theme colors
- **update interval**: Change `interval=5000` (in ms) for refresh rate
- **charts**: Add more Plotly visualizations
- **metrics**: Compute additional statistics

Dashboard uses:
- **Plotly**: Interactive charts
- **Dash**: Web framework
- **Pandas**: Data loading and analysis

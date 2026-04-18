# Paper Trading Migration Guide

## Overview
This guide covers how to set up the paper trading system on your local machine after pulling from the HPC cluster.

## System Requirements
- **Python:** 3.8+
- **Disk Space:** ~2 GB (for data/pmxt/ parquet files)
- **RAM:** 4 GB minimum (8 GB recommended)
- **OS:** Linux/macOS/Windows (with WSL)

## Step 1: Install Dependencies

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # or: venv\Scripts\activate on Windows

# Install requirements
pip install -r requirements.txt
```

## Step 2: Verify Data Files

Ensure these directories exist after pulling from HPC:

```
data/pmxt/
├── ticks/
│   ├── Finance_trades.parquet
│   ├── Geopolitics_trades.parquet
│   ├── Economy_trades.parquet
│   └── Politics_trades.parquet
├── markets/
│   └── markets_all.parquet
└── ohlcv/
    └── ohlcv_all.parquet
```

Check data integrity:
```bash
python3 -c "
import pandas as pd
for cat in ['Finance', 'Geopolitics', 'Economy', 'Politics']:
    df = pd.read_parquet(f'data/pmxt/ticks/{cat}_trades.parquet')
    print(f'{cat}: {len(df)} ticks')
"
```

## Step 3: Configure Paper Trading

Edit `config/paper_trading.yaml` to customize:
- **initial_capital:** Starting amount (default: $1,000)
- **position_limits:** Max per market, per category
- **risk_limits:** Daily/monthly loss triggers
- **email_alerts:** Gmail SMTP settings (your app password)
- **strategies:** Enable/disable mean_reversion, momentum, counter_flow

## Step 4: Set Up Gmail Alerts (Optional)

To enable email alerts for trades:

1. Go to Google Account → Security → 2-Step Verification (enable if not already)
2. Create App Password: https://myaccount.google.com/apppasswords
3. Copy the 16-character password
4. Update `config/paper_trading.yaml`:
   ```yaml
   monitoring:
     email_alerts:
       enabled: true
       from_email: "your-email@gmail.com"
       app_password: "xxxx xxxx xxxx xxxx"  # 16-char app password
       to_emails:
         - "your-email@gmail.com"
   ```

## Step 5: Run Paper Trading

```bash
# Set environment variables for imports
export PYTHONPATH=.:src

# Run the paper trading simulation (uses historical tick data)
python3 scripts/live/run_paper_trading.py

# Optional: Run with more trades
python3 -c "
from scripts.live.run_paper_trading import run_paper_trading
engine = run_paper_trading(max_trades=200)
"
```

## Step 6: Review Results

After running, check outputs:

```bash
# View trade log
python3 -c "
import pandas as pd
trades = pd.read_parquet('data/paper_trading/trades.parquet')
print('Summary:')
print(f'Total Trades: {len(trades)}')
print(f'Win Rate: {100*(trades[\"pnl\"] > 0).sum() / len(trades):.1f}%')
print(f'Total P&L: \${trades[\"pnl\"].sum():.0f}')
print(f'ROI: {100*trades[\"pnl\"].sum()/1000:.1f}%')
"

# View equity curve
cat data/paper_trading/equity.parquet

# View logs
cat logs/paper_trading.log
```

## Output Files

Paper trading generates:

| File | Purpose |
|------|---------|
| `data/paper_trading/trades.parquet` | All executed trades (market_id, side, entry/exit price, PnL, strategy) |
| `data/paper_trading/equity.parquet` | Equity curve over time (timestamp, equity, pnl, active_trades) |
| `logs/paper_trading.log` | Detailed execution logs |

## Next Steps: Moving to Real Trading

Once paper trading validates the strategies (recommended 30 days):

1. **Testnet Phase (Days 1-2):**
   - Get Polymarket API credentials
   - Fund testnet wallet with fake USDC
   - Run against testnet with small position sizes

2. **Live Paper Phase (Days 3-30):**
   - Run against mainnet with real CLOB data
   - Use $1k simulated capital
   - Monitor P&L and strategy performance

3. **Go-Live Criteria (Day 30+):**
   - Paper trading PnL correlation > 0.7 with backtest
   - No reconciliation mismatches
   - All 3 strategies generating signals

4. **Real Capital (Day 38+):**
   - Fund mainnet wallet
   - Deploy with validated strategies
   - Start with $10k for conservative testing

## Troubleshooting

### No trades generated
- Check if signal thresholds are too strict in `config/paper_trading.yaml`
- Verify data loads: `python3 scripts/live/run_paper_trading.py` should show tick counts
- Review logs: `tail -f logs/paper_trading.log`

### Gmail alerts not sending
- Verify `app_password` is correct (16 characters, spaces included)
- Check firewall allows SMTP (port 587)
- Verify recipient email exists in config

### Out of memory
- Reduce data scope: edit `run_paper_trading.py` to load fewer categories
- Or: run on machine with more RAM, or process in batches

### Slow performance
- Data is 8M ticks; first load is slowest due to parquet deserialization
- Subsequent runs use cache
- Can run with `max_trades=50` for faster testing

## Migration Checklist

- [ ] Install dependencies: `pip install -r requirements.txt`
- [ ] Verify data files in `data/pmxt/`
- [ ] Update `config/paper_trading.yaml` with your settings
- [ ] (Optional) Set up Gmail app password for alerts
- [ ] Run `python3 scripts/live/run_paper_trading.py`
- [ ] Check `data/paper_trading/trades.parquet` for results
- [ ] Review equity curve and P&L

## Support

If issues persist:
1. Check `logs/paper_trading.log` for specific errors
2. Verify PYTHONPATH is set: `echo $PYTHONPATH` should show `.:src`
3. Confirm venv is activated: `which python3` should show venv path
4. Run a quick data check: see "Verify Data Files" section above

# Paper Trading — Quick Start

**TL;DR:** Export the repo, run 3 commands, get trading results in seconds.

## 1. Setup (2 minutes)
```bash
cd predict-ngin
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=.:src
```

## 2. Run Paper Trading (1 minute)
```bash
python3 scripts/live/run_paper_trading.py
```

## 3. Check Results
```bash
# Should output:
# Total Trades:     8
# Win Rate:         75.0%
# Total P&L:        $15
# ROI:              1.5%
```

That's it! The engine:
- Loads 8M+ real Polymarket ticks
- Generates signals from 3 strategies
- Executes trades under $1k capital constraint
- Tracks P&L and sends email alerts

## Customize Later

Edit `config/paper_trading.yaml` to change:
- Capital, position limits
- Which strategies to run
- Risk thresholds
- Email settings

See `docs/MIGRATION_GUIDE.md` for full details.

## What Gets Generated

```
data/paper_trading/
├── trades.parquet      # All executed trades
├── equity.parquet      # Daily P&L
└── reconciliation.jsonl # Validation checks

logs/
└── paper_trading.log   # Detailed execution log
```

## Email Alerts

Optional: Set up Gmail alerts so you get daily P&L reports.

1. Go to: https://myaccount.google.com/apppasswords
2. Create an app password (16 chars)
3. Paste into `config/paper_trading.yaml`:
   ```yaml
   email_alerts:
     enabled: true
     app_password: "xxxx xxxx xxxx xxxx"
   ```

Done! Alerts will be sent after each run.

---

Ready? Start with step 1 above. See `DEPLOYMENT_STATUS.md` for full overview.

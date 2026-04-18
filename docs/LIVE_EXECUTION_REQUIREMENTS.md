# Live Execution Engine — What I Need From You

Before I start building, provide answers to these questions. This will determine architecture + configuration.

---

## **SECTION A: API & Authentication**

### A1. Polymarket API Access
```
Have you signed up for Polymarket API access?
  ☐ Yes, I have API key + secret
  ☐ No, I need to sign up first
  ☐ Not sure
  
If yes, provide:
  - API Key: _______________
  - API Secret: _______________
  - Webhook endpoint (if any): _______________
```

**ACTION:** If no → go to https://polymarket.com/api and request access (2-4 hours)

---

### A2. Network Choice
```
Start on testnet (fake money) or mainnet (real money)?
  ☐ Testnet first (recommended) → switch to mainnet after validation
  ☐ Mainnet immediately (I have capital ready)
  ☐ Don't care, do both in parallel
  
Timeline preference:
  ☐ Testnet: 2 days, then mainnet paper trading
  ☐ Testnet: 1 week, then mainnet
  ☐ Skip testnet, go straight to mainnet
```

---

## **SECTION B: Wallet & Capital**

### B1. Wallet Setup
```
Do you have an existing Polygon/USDC wallet?
  ☐ Yes, address: 0x_________________
  ☐ No, create one for me
  ☐ Use existing address + key: _______________
  
Security preference:
  ☐ Software wallet (private key in .env file)
  ☐ Hardware wallet (Ledger/Trezor) - requires signing each tx
  ☐ Multi-sig (multiple signers required per trade)
```

**ACTION:** If software → I'll create a secure key management system

---

### B2. Capital Amount
```
Starting capital for paper trading:
  ☐ $1,000 (conservative)
  ☐ $5,000 (medium)
  ☐ $10,000 (aggressive)
  ☐ Custom: $____________
  
Mainnet capital (after 30-day paper validation):
  ☐ $10,000
  ☐ $25,000
  ☐ $50,000
  ☐ $100,000+
  ☐ Depends on results
```

---

## **SECTION C: Risk Parameters**

### C1. Position Sizing Limits
```
Max position per single market?
  ☐ $5,000 (conservative)
  ☐ $10,000 (medium)
  ☐ $20,000 (aggressive)
  ☐ Custom: $____________

Max position per category (Finance/Politics/etc)?
  ☐ $25,000
  ☐ $50,000
  ☐ $100,000
  ☐ No limit

Max total deployed capital (% of total):
  ☐ 50% (conservative, keep cash buffer)
  ☐ 75% (medium)
  ☐ 100% (aggressive, deploy all capital)
```

---

### C2. Loss Limits (Circuit Breaker)
```
Daily loss limit (stop trading if hit):
  ☐ -$1,000 (stop after losing 1%)
  ☐ -$5,000 (stop after losing 5%)
  ☐ -$10,000 (stop after losing 10%)
  ☐ No daily limit
  
Monthly loss limit:
  ☐ -$10,000
  ☐ -$25,000
  ☐ -$50,000
  ☐ No monthly limit
  
Max single-trade loss:
  ☐ Cap to position size (max loss = capital deployed)
  ☐ Hard cap: $5,000
  ☐ Hard cap: $10,000
  ☐ No cap
```

---

### C3. Hold Times
```
Max time to hold a position:
  ☐ 1 hour (scalper)
  ☐ 4 hours (medium)
  ☐ 24 hours (swing)
  ☐ To resolution (days)
  
Action on timeout:
  ☐ Force-close position (take loss)
  ☐ Move to stop-loss -5% (let market decide)
  ☐ Keep until resolution
```

---

## **SECTION D: Monitoring & Alerts**

### D1. Notification Preferences
```
Want Slack alerts?
  ☐ Yes, webhook URL: https://hooks.slack.com/___________
  ☐ Yes, channel: #trading
  ☐ No Slack
  
Want email alerts?
  ☐ Yes, email: ___________________
  ☐ Only on errors/CRITICAL
  ☐ No email
  
Want SMS alerts (critical only)?
  ☐ Yes, phone: +1_______________
  ☐ No
  
Want daily report?
  ☐ Yes, time: ______ UTC, format: Slack/Email/Both
  ☐ Yes, time: ______ UTC, format: Spreadsheet (Google Sheets)
  ☐ No automated report
```

---

### D2. Dashboard
```
Want a live dashboard?
  ☐ Yes, web dashboard (I'll build it)
  ☐ Yes, send me daily CSV/JSON (I'll make my own dashboard)
  ☐ No, just alerts
```

---

## **SECTION E: Strategy Configuration**

### E1. Which Strategies?
```
Run all 3 strategies or select?
  ☐ All 3 equally (1/3 capital each)
  ☐ Mean Reversion only (conservative)
  ☐ Momentum only (medium risk)
  ☐ Counter-Flow only (behavioral)
  ☐ Custom weights:
      Mean Reversion: ___%
      Momentum: ___%
      Counter-Flow: ___%
```

---

### E2. Paper Trading Duration
```
How long to run paper (fake money) before live capital?
  ☐ 7 days (quick validation)
  ☐ 14 days (medium)
  ☐ 30 days (recommended)
  ☐ 60+ days (very conservative)
  
What's "success" for paper trading?
  ☐ Sharpe > 0.5 (just positive)
  ☐ Sharpe > 1.0 (decent)
  ☐ Win rate > 60% (threshold)
  ☐ Total PnL > $10k (amount)
  ☐ Match backtest within ±20% Sharpe
```

---

## **SECTION F: Testing & Compliance**

### F1. Geolocation
```
Where are you trading from?
  ☐ US (Polymarket blocks US traffic)
  ☐ Non-US (EU, Asia, etc - no restrictions)
  ☐ Using VPN (not recommended, legal gray area)
  
If US: Will you use geofencing bypass?
  ☐ Yes, I understand the risks
  ☐ No, I'll use legal wrapper
  ☐ Not applicable
```

---

### F2. Dry-Run Testing
```
Want a 24-hour dry run before live?
  ☐ Yes, paper trading mode (generate signals, don't execute)
  ☐ Yes, with small test capital ($100)
  ☐ No, just launch
```

---

## **SECTION G: Existing Infrastructure**

### G1. Data Storage
```
Where should I save trade logs + P&L?
  ☐ Local disk (Parquet files)
  ☐ Cloud storage (AWS S3 / GCP)
  ☐ Database (PostgreSQL/TimescaleDB)
  ☐ Google Sheets (for manual monitoring)
  ☐ Your preference: _______________
```

---

### G2. Code Deployment
```
Where will the code run?
  ☐ Your HPC cluster (I'll package for SLURM)
  ☐ Laptop (I'll make sure it's lightweight)
  ☐ Cloud VM (EC2/GCP/Azure - which?)
  ☐ Dedicated server (you provide, I configure)
  
Should it run 24/7?
  ☐ Yes, always on
  ☐ Market hours only (when Polymarket active)
  ☐ Custom schedule: _______________
```

---

## **SUMMARY CHECKLIST**

Before I start, confirm you've answered:

- [ ] A1: API credentials (or will get them)
- [ ] A2: Testnet or mainnet?
- [ ] B1: Wallet address (or I create one)
- [ ] B2: Starting capital amount
- [ ] C1: Position limits
- [ ] C2: Loss limits
- [ ] C3: Hold times
- [ ] D1: Notification preferences
- [ ] D2: Dashboard yes/no
- [ ] E1: Which strategies?
- [ ] E2: Paper trading duration
- [ ] F1: Geolocation (US or not)
- [ ] F2: Dry-run test?
- [ ] G1: Data storage
- [ ] G2: Where to run code

---

## **NEXT STEPS**

1. **You answer the checklist above** (15 min)
2. **I build the execution engine** (Days 1-5)
3. **You fund a testnet wallet** (if I don't use your existing one)
4. **We do dry-run** (Day 6)
5. **Paper trading launches** (Day 7)
6. **You monitor for 30 days** (Days 8-37)
7. **Go/No-go decision** (Day 38)
8. **Real capital** (Day 39+, if validation passes)

---

**Send me your answers in this format:**

```
A1: Yes, API key: xxx, secret: yyy
A2: Testnet first, then mainnet
B1: Create new wallet, software
B2: $5,000 paper, $25,000 live
C1: $10k per market, $50k per category, 100% deploy
C2: -$5k daily, -$25k monthly, cap to position
C3: 4 hours hold, force-close on timeout
D1: Slack webhook: xxx, Daily report 12:00 UTC
D2: Yes, web dashboard
E1: All 3 equally (1/3 each)
E2: 30 days, Sharpe > 1.0
F1: Non-US
F2: Yes, $100 test capital
G1: Local disk (Parquet)
G2: HPC cluster, 24/7
```

Ready when you are.

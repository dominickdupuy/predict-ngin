# Polymarket Data Capacity & Storage Analysis

**Date:** 2026-04-18  
**Objective:** Determine maximum available data and optimal storage strategy

---

## Available Data Sources & Volumes

### 1. Market Metadata (Markets CSV)

**What:** Market details (question, category, volume, prices, liquidity, timestamps)

**Universe:**
- Total markets listed: ~52,000
- Active (open, tradeable): ~2,000
- Closed/resolved: ~50,000
- Fields per market: ~50 (question, description, outcomes, volume, liquidity, etc)

**Storage:**
```
52,000 markets × 50 KB/market = 2.6 GB (raw JSON/CSV)
Compressed (Parquet, 10:1):    = 260 MB
```

**API Endpoints:**
- `GET /markets` (Gamma API) — all markets, paginated
- `GET /markets/{id}` — single market detail
- Rate limit: 60 req/min (unauthenticated)
- **Fetch cost:** 52k markets ÷ 60 req/min ÷ 60 min = ~14 hours

---

### 2. Trade Ticks (Historical)

**What:** Every individual trade: timestamp, price, size, side, wallet addresses

**Universe:**
- Active markets with trades: ~1,000-5,000
- Average trades per market: 100-10,000
- **Estimated total ticks:** 50M - 500M
- Per tick record: ~100 bytes (timestamp, price, size, side, 2× wallet addresses)

**Storage:**
```
Low estimate:  50M ticks × 100 bytes = 5 GB (raw), 500 MB (10:1 compressed)
High estimate: 500M ticks × 100 bytes = 50 GB (raw), 5 GB (10:1 compressed)
Realistic:     150M ticks × 100 bytes = 15 GB (raw), 1.5 GB (10:1 compressed)
```

**API Endpoints:**
- `GET /trades?condition_id=X&limit=5000` (Data API)
- Paginated results (token-based)
- Rate limit: 60 req/min
- **Fetch cost:** ~5,000 markets × 20 pages/market ÷ 60 req/min = ~2,778 hours (**116 days** continuous)

**Optimization:** Parallel fetch (10-20 concurrent requests) → 6-12 days

---

### 3. OHLCV Candles (All Timeframes)

**What:** Open, High, Low, Close, Volume at various intervals

**Universe:**
- Markets with price history: ~2,000-5,000
- Timeframes: 1m, 5m, 15m, 1h, 1d (5 timeframes)
- Days of history: ~1,200 (3.3 years, Feb 2023 → Apr 2026)
- Per candle: ~30 bytes (timestamp, 4× prices, volume)

**Storage:**
```
2,000 markets × 5 timeframes × 1,200 days × 30 bytes = 360 MB (raw)
Compressed (5:1):                                      = 72 MB
```

For all 52k markets (extrapolated):
```
52,000 × 5 × 1,200 × 30 bytes = 9.4 GB (raw), 1.9 GB (compressed)
```

**API Endpoints:**
- `GET /markets/{id}/ohlcv?timeframe=1m` (Gamma API, via PMXT if available)
- Limit: ~2,000 candles per request
- **Fetch cost:** ~5,000 markets × 5 timeframes × 5 requests = 125k requests ÷ 60 req/min = ~34 hours

---

### 4. Order-Book Snapshots (CLOB Depth)

**What:** Current bid/ask levels (depth, prices, sizes) at point-in-time

**Universe:**
- Active markets (CLOB-enabled): ~2,000
- Snapshot frequency: 
  - **Live streaming:** 1 per second = 86,400/day
  - **Hourly archive:** 24/day = 8,760/year
  - **Historical (estimated):** 1,000 snapshots per market
- Depth per snapshot: 50 levels (bid+ask)
- Per snapshot: ~2 KB (50 × (price + size) pairs)

**Storage:**
```
Hourly snapshots (conservative):
  2,000 markets × 24 snapshots/day × 365 days × 2 KB = 35 GB/year
  Compressed (10:1):                                  = 3.5 GB/year
  
Live 1-sec (aggressive):
  2,000 × 86,400 /day × 365 × 2 KB = ~63 TB/year
  Compressed (10:1):                = ~6.3 TB/year
  
Practical (5-min snapshots):
  2,000 × 288/day × 365 × 2 KB = 420 GB/year
  Compressed (10:1):            = 42 GB/year
```

**API Endpoints:**
- `GET /book?market_id=X` (CLOB REST API)
- WebSocket `wss://ws.clob.polymarket.com/book` (real-time)
- Rate limit: 100 req/min (authenticated)

---

### 5. Real-Time Price Ticks (1-Second)

**What:** Price updates at 1-second granularity

**Universe:**
- Markets actively trading: ~100-500
- Duration: Continuous (24/7)
- Per tick: ~20 bytes (timestamp, price, side, volume)
- Annual volume: **1 year = 31,536,000 seconds**

**Storage (1-year, 500 markets):**
```
500 markets × 31.5M seconds × 20 bytes = 315 GB (raw)
Compressed (8:1):                       = 39 GB/year
```

**Note:** This is **not feasible long-term** without dedicated infrastructure. Practical limit: 1-5 GB/year (select markets only).

---

## Recommended Data Capture Strategy

### Tier 1: Essential (Low Storage, High Value)
✓ **Currently captured** (in `data/pmxt/ticks/`)

| Data Type | Universe | Storage | Fetch Time | Priority |
|-----------|----------|---------|-----------|----------|
| Market metadata | 52k | 260 MB | 14h | Critical |
| Trade ticks | 150M | 1.5 GB | 6-12 days | Critical |
| OHLCV (1d + 1h) | 52k | 500 MB | 8h | High |

**Total: ~2.3 GB compressed**  
**Status:** ✓ Can fetch in ~2 weeks with parallel requests

---

### Tier 2: Enhanced (Medium Storage, Research Value)
⚠ **Partially captured** (Finance/Geopolitics/Economy categories)

| Data Type | Universe | Storage | Fetch Time | Priority |
|-----------|----------|---------|-----------|----------|
| All OHLCV (5 timeframes) | 52k | 1.9 GB | 12h | Medium |
| Order-book hourly | 2k (1 year) | 3.5 GB | 48h | Medium |
| CLOB depth (live) | 2k | 42 GB/year | Streaming | Medium |

**Total: ~5-50 GB depending on depth/period**  
**Status:** ⚠ Feasible with focused parallel fetch

---

### Tier 3: Aggressive (Large Storage, Real-Time)
✗ **Not feasible without dedicated infrastructure**

| Data Type | Universe | Storage | Fetch Time | Priority |
|-----------|----------|---------|-----------|----------|
| 1-sec price ticks | 500 | 39 GB/year | Streaming | Low |
| Real-time order-book | 2k | 6.3 TB/year | Streaming | Low |
| WebSocket deltas | All | Unbounded | Streaming | Low |

**Total: 6+ TB/year**  
**Status:** ✗ Requires dedicated servers, storage pool, and data warehouse

---

## Optimal Capture Plan (This Week)

### Phase 1: Complete Market Universe (2-3 hours)

```python
# Fetch all 52k markets + metadata
# Storage: 260 MB
# Cost: ~14 hours parallel, 1-2 hours with 60 concurrent workers

fetch_all_markets(
    all_fields=True,
    output="data/pmxt/markets/all_markets.parquet"
)
```

**Output structure:**
```
data/pmxt/markets/
├── all_markets.parquet          (52k markets, all metadata)
└── markets_by_category.json     (indexed by category for quick lookup)
```

---

### Phase 2: Historical Trades (Complete Set) (3-5 days parallel)

```python
# Fetch trades for all markets that have them
# Storage: 1.5-5 GB (1-2 GB compressed)
# Cost: ~6-12 days with 10 parallel workers

fetch_all_trades(
    min_trades=1,
    parallel_workers=10,
    output="data/pmxt/ticks/"
)
```

**Output structure:**
```
data/pmxt/ticks/
├── {category}/
│   ├── {YYYY-MM}/
│   │   └── ticks_{YYYY-MM}.parquet
```

---

### Phase 3: OHLCV Candles (All Timeframes) (8-12 hours parallel)

```python
# Fetch 1m, 5m, 15m, 1h, 1d for all markets
# Storage: 1.9 GB (compressed)
# Cost: ~8-12 hours with 20 parallel workers

fetch_all_ohlcv(
    timeframes=["1m", "5m", "15m", "1h", "1d"],
    markets=52000,
    parallel_workers=20,
    output="data/pmxt/ohlcv/"
)
```

**Output structure:**
```
data/pmxt/ohlcv/
├── 1m/
│   └── {market_id}.parquet
├── 5m/
│   └── {market_id}.parquet
└── 1d/
    └── {market_id}.parquet
```

---

### Phase 4: Order-Book Snapshots (Hourly, 1 Week) (48 hours)

```python
# Fetch hourly order-book snapshots for active markets
# Storage: 3.5 GB/year (3-4 hours of work to collect 1 week)
# Cost: ~48 hours to fetch complete history

fetch_orderbook_history(
    markets="active",  # 2k markets
    snapshot_freq="hourly",
    history_days=7,  # Start with 1 week
    output="data/pmxt/orderbook/"
)
```

**Output structure:**
```
data/pmxt/orderbook/
├── {YYYY-MM-DD}/
│   ├── {market_id}_00h.parquet
│   ├── {market_id}_01h.parquet
│   └── ...
```

---

## Total Storage Required

### Conservative (Tier 1 + Tier 2 light)
```
Market metadata:        260 MB
Trade ticks (1.5M-50M): 1.5 GB
OHLCV candles:          1.9 GB
Order-book (1 week):    100 MB
────────────────────────────
Total:                  ~3.8 GB
Estimated time:         ~2-3 days (with parallel workers)
```

### Recommended (Tier 1 + Tier 2 full)
```
Market metadata:        260 MB
Trade ticks (150M):     1.5 GB
OHLCV all timeframes:   1.9 GB
Order-book (1 month):   400 MB
────────────────────────────
Total:                  ~4.1 GB
Estimated time:         ~3-4 days
```

### Aggressive (Tier 1 + Tier 2 + Tier 3 light)
```
Market metadata:        260 MB
Trade ticks (500M):     5 GB
OHLCV all timeframes:   1.9 GB
Order-book (quarterly):  10 GB
1-sec prices (1 month):  3 GB
────────────────────────────
Total:                  ~20 GB
Estimated time:         ~2 weeks
```

---

## Implementation Roadmap

| Phase | Target | Storage | Time | Status |
|-------|--------|---------|------|--------|
| **Phase 1** | All market metadata | 260 MB | 2h | Ready |
| **Phase 2** | All historical trades | 1.5 GB | 3d | Ready |
| **Phase 3** | All OHLCV candles | 1.9 GB | 8h | Ready |
| **Phase 4** | Order-book snapshots | 400 MB-10 GB | 2d | Ready |
| **Phase 5** | Live CLOB streaming | 42+ GB/year | Ongoing | Infrastructure ready |

---

## Questions Answered

### Q: How much total data is available?
**A:** ~5-20 GB compressed (realistic), up to 60+ TB/year with real-time price ticks

### Q: Can I get it all?
**A:** **Yes, for historical data.** All market metadata + trades + OHLCV can be fetched in 3-4 days.
Real-time data requires dedicated infrastructure (storage, compute).

### Q: What's the bottleneck?
**A:** **API rate limits.** Polymarket allows 60-100 req/min. With parallel workers (10-20), you can fetch all data in 3-5 days.

### Q: How much disk space do I need?
**A:** **4-5 GB** for all essential historical data. **20-50 GB** if including comprehensive order-book history.

### Q: How long to fetch everything?
**A:** **3-5 days** (parallel fetch with 10-20 workers). Sequential: 2-3 weeks.

### Q: Best strategy?
**A:** Fetch Tiers 1 & 2 (5 GB) immediately. Add real-time streaming afterward (live CLOB WebSocket).

---

## Architecture Recommendation

```
Week 1: Fetch all historical data
  → Market metadata (260 MB)
  → Trade ticks for all markets (1.5 GB)
  → OHLCV candles (1.9 GB)
  → Order-book hourly (400 MB)
  Total: ~4.1 GB in 3-4 days

Week 2+: Start live capture
  → CLOB WebSocket subscriber (5-10 GB/month)
  → Realtime price stream (select active markets)
  → Auto-flush to Parquet hourly

Result: Complete historical archive + continuous new data
```

---

## Cost Summary

| Item | Cost |
|------|------|
| Disk storage (SSD, 500 GB) | $50 once |
| Bandwidth (fetch all data) | Free (Polymarket APIs public) |
| Compute (parallel fetch) | Free (your HPC cluster) |
| Live streaming (EC2 t3.micro) | ~$10/month |
| **Total first month** | ~$60 |
| **Ongoing** | ~$10/month |

---

*Next step: Run comprehensive data fetch with parallel workers to capture all 3 tiers.*

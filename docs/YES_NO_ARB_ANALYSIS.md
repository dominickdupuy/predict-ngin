# YES/NO Arb Analysis — NOT BUILDABLE FROM CURRENT DATA

**Date:** 2026-04-19
**Scope:** 2025-02-14 → 2026-02-14, 4 categories
**Decision:** **Do not build the strategy yet.** Reason below is a data gap,
not a finding that the arb does not exist.

## What would be needed

A YES/NO arb requires, for a single market at a single instant, both:
- best ask for the YES token
- best ask for the NO token

If `ask(YES) + ask(NO) < $1.00 − round_trip_cost`, buying both guarantees a
$1.00 payout at resolution and a post-cost profit. This is structurally
risk-free.

## What we have

Two sources were examined, both inadequate:

### 1. `trades.parquet` — token identifier is missing where timestamps exist

```
timestamp_valid=T, nonusdc_side_present=T:         0 rows
timestamp_valid=T, nonusdc_side_present=F: 1,258,856 rows   (PIT-usable)
timestamp_valid=F, nonusdc_side_present=T: 2,337,189 rows   (legacy)
```

- **Legacy rows** (`timestamp=0`) have the `nonusdc_side` field populated
  (`token1` = YES, `token2` = NO). But they have no usable timestamp — we
  cannot place them on a time axis to detect simultaneous mispricing.
- **PIT-valid rows** (timestamp > 0, what `PITDataLoader` keeps) do not
  record the token side. We cannot tell whether a given trade happened on
  the YES or NO side, which is the core of the arb.

### 2. `prices.parquet` — YES-only snapshots, one market per category

```
category        markets  outcomes        rows
Politics             1   YES only     441,434
Economy              1   YES only     191,492
Geopolitics          1   YES only     484,048
Finance              1   YES only     191,517
```

Per-minute YES prices for a single headline market per category (e.g. the
2024 presidential election in Politics). No NO prices. Cannot compute the
sum.

## Implication

**We cannot evaluate YES/NO arb profitability on this dataset.** The arb
*might* exist in production — live Polymarket does publish both legs on the
CLOB — but with the extracted historical data we can neither confirm
opportunity density nor size post-cost edge.

## What to do instead

Before building a YES/NO arb bot:

1. **Extract a proper two-token price history**: for each conditionId, pull
   per-minute best-bid/best-ask on both token IDs (from `clobTokenIds`).
   Polymarket's public data API exposes this via the CLOB client.
2. **Sample-check live** for a week: run a read-only watcher that logs
   `ask(YES) + ask(NO)` every minute for the top ~50 markets. If the sum
   stays pinned at $1.00 + small jitter, the arb is being cleaned up by
   professional arbs in sub-second time — not worth chasing. If it dips
   below $1.00 for minutes at a time, the arb is worth building.

## Conclusion

**Strategy not built.** Prerequisite data not present. Cost gate cannot be
evaluated.

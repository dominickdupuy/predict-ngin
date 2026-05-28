#!/usr/bin/env python3
"""
Live whale-following strategy orchestrator.

Wires together the full pipeline end-to-end:
  Warmup  → load research trades → build whale set → build market liquidity map
  Live    → poll Polymarket data API → LiveTradeBuffer → position sizing → PaperTrader
  Risk    → drawdown kill-switch, weekly whale-set refresh, position persistence
  Exit    → background thread polls for market resolutions, closes positions at payout

Modes:
  default   Paper trading with live Polymarket data
  --live    Real orders via py-clob-client (requires POLYMARKET_PRIVATE_KEY)
  --replay  Feed historical parquet trades through the buffer to validate signals

Usage:
    # Paper trading (safe default)
    python scripts/live/run_live_strategy.py

    # Replay validation (compare signals to backtest)
    python scripts/live/run_live_strategy.py --replay --research-dir data/research

    # Live orders (requires credentials)
    python scripts/live/run_live_strategy.py --live

    # Custom parameters
    python scripts/live/run_live_strategy.py \\
        --capital 50000 \\
        --min-usd 500 \\
        --categories Finance,Geopolitics \\
        --interval 30
"""

import argparse
import dataclasses
import json
import math
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional, Set

import requests
import pandas as pd

_project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / "src"))

from src.trading.live.live_trade_buffer import LiveTradeBuffer
from src.trading.live.paper_trading import PaperTrader, OrderSide
from src.trading.live.order_router import OrderRouter
from src.whale_strategy.whale_config import load_whale_config, WhaleConfig
from src.whale_strategy.whale_following_strategy import (
    WhaleSignal, StrategyState, Position,
    calculate_position_size,
    RISK_LIMITS,
)
from src.whale_strategy.research_data_loader import (
    load_research_trades,
    load_research_markets,
    load_resolution_winners,
    get_research_categories,
    load_historical_trades,
)
from src.whale_strategy.whale_surprise import build_surprise_positive_whale_set

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"


def _refresh_data_if_stale(research_dir: Path, max_age_hours: float = 24) -> None:
    """Run incremental data refresh if any source in research_dir is older than max_age_hours."""
    import glob as _glob

    def _age(path: Path) -> float:
        return (time.time() - path.stat().st_mtime) / 3600 if path.exists() else float("inf")

    trades_files = list((research_dir / "recent_trades").glob("*.parquet")) if (research_dir / "recent_trades").is_dir() else []
    trades_age   = _age(max(trades_files, key=lambda p: p.stat().st_mtime)) if trades_files else float("inf")
    res_age      = _age(research_dir / "resolutions.csv")
    mkt_age      = _age(research_dir / "markets.parquet")

    oldest = max(trades_age, res_age, mkt_age)
    print(f"  trades={trades_age:.1f}h  resolutions={res_age:.1f}h  markets={mkt_age:.1f}h old")

    if oldest <= max_age_hours:
        print(f"  Data is fresh (oldest={oldest:.1f}h < {max_age_hours}h). Skipping refresh.")
        return

    print(f"  Data is stale (oldest={oldest:.1f}h). Running incremental refresh...")
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable, str(_project_root / "scripts" / "data" / "refresh_data.py")],
            cwd=str(_project_root),
            capture_output=False,
            timeout=900,
        )
        if result.returncode != 0:
            print("  Warning: data refresh exited with errors — proceeding with existing data.")
    except Exception as e:
        print(f"  Warning: data refresh failed ({e}) — proceeding with existing data.")

# ── Persistence paths ──────────────────────────────────────────────────────────
DEFAULT_STATE_PATH      = _project_root / "data" / "live" / "positions.json"
DEFAULT_BUFFER_STATE    = _project_root / "data" / "live" / "buffer_state.json"
DEFAULT_TRADE_LOG       = _project_root / "data" / "live" / "trades.jsonl"
DEFAULT_EQUITY_LOG      = _project_root / "data" / "live" / "equity_log.jsonl"


# ── Position state persistence ─────────────────────────────────────────────────

def _save_positions(state: StrategyState, path: Path) -> None:
    """Persist open positions to JSON so restarts don't lose context."""
    path.parent.mkdir(parents=True, exist_ok=True)
    positions = []
    for pos in state.positions:
        d = dataclasses.asdict(pos)
        d["entry_date"] = str(d["entry_date"])
        positions.append(d)
    with open(path, "w") as f:
        json.dump({
            "total_capital":    state.total_capital,
            "positions":        positions,
            "category_exposure": state.category_exposure,
            "whale_exposure":    state.whale_exposure,
            "market_exposure":   state.market_exposure,
            "tier_exposure":     state.tier_exposure,
            "saved_at":         datetime.now(timezone.utc).isoformat(),
        }, f, indent=2)


def _load_positions(path: Path, capital: float) -> StrategyState:
    """Restore StrategyState from JSON. Returns a fresh state if file missing."""
    state = StrategyState(total_capital=capital)
    if not path.exists():
        return state
    try:
        with open(path) as f:
            data = json.load(f)
        state.total_capital      = float(data.get("total_capital", capital))
        state.category_exposure  = data.get("category_exposure", {})
        state.whale_exposure     = data.get("whale_exposure", {})
        state.market_exposure    = data.get("market_exposure", {})
        state.tier_exposure      = data.get("tier_exposure", {})
        _pos_fields = {f.name for f in dataclasses.fields(Position)}
        for p in data.get("positions", []):
            p["entry_date"] = pd.Timestamp(p["entry_date"])
            filtered = {k: v for k, v in p.items() if k in _pos_fields}
            state.positions.append(Position(**filtered))
        print(f"  Restored {len(state.positions)} open positions from {path}")
    except Exception as e:
        print(f"Warning: could not restore positions: {e}")
    return state


def _log_trade(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


# ── Warmup: whale set + market liquidity ──────────────────────────────────────

def _is_flat_historical(research_dir: Path) -> bool:
    """Return True if this directory uses the flat recent_trades/ layout."""
    return (research_dir / "recent_trades").is_dir()


def build_whale_set(
    research_dir: Path,
    categories: Optional[list],
    resolutions_dir: Optional[Path],
    cfg: WhaleConfig,
    market_volumes: Optional[Dict[str, float]] = None,
):
    """Load research data and build whale set + scores + winrates."""
    all_trades = []

    if _is_flat_historical(research_dir):
        print("  Using flat historical layout (data/historical/recent_trades/)")
        df = load_historical_trades(research_dir)
        if not df.empty:
            all_trades.append(df)
    else:
        if categories is None:
            categories = get_research_categories(research_dir)
        for cat in categories:
            try:
                df = load_research_trades(research_dir, [cat])
                if not df.empty:
                    all_trades.append(df)
            except Exception as e:
                print(f"  Warning: could not load {cat}: {e}")

    if not all_trades:
        return set(), {}, {}

    trades_df = pd.concat(all_trades, ignore_index=True)

    resolution_winners: Dict[str, str] = {}
    for res_dir in [resolutions_dir, research_dir.parent / "poly_cat", research_dir]:
        if res_dir is None:
            continue
        csv = Path(res_dir) / "resolutions.csv"
        if csv.exists():
            try:
                rdf = pd.read_csv(csv)
                if "market_id" in rdf.columns and "winner" in rdf.columns:
                    resolution_winners = dict(
                        zip(rdf["market_id"].astype(str), rdf["winner"].astype(str))
                    )
                    break
            except Exception:
                pass

    cutoff = trades_df["datetime"].max() if "datetime" in trades_df.columns else pd.Timestamp.now()
    print(f"  {len(trades_df):,} trades  {len(resolution_winners):,} resolutions  cutoff {cutoff.date()}")

    whale_set, scores, winrates = build_surprise_positive_whale_set(
        trades_df,
        resolution_winners,
        min_surprise=cfg.min_surprise,
        min_trades=cfg.min_trades_for_surprise,
        require_positive_surprise=cfg.require_positive_surprise,
        volume_percentile=cfg.volume_percentile,
        cutoff=cutoff,
        recency_halflife_days=cfg.recency_halflife_days,
        bayes_prior_alpha=cfg.bayes_prior_alpha,
        bayes_prior_beta=cfg.bayes_prior_beta,
        market_volumes=market_volumes,
        lambda_decay=cfg.lambda_decay,
        min_score=cfg.min_score,
    )
    return whale_set, scores, winrates


def build_market_liquidity(
    research_dir: Path,
    categories: Optional[list],
) -> Dict[str, float]:
    """Build {conditionId: volume_usd} from markets data."""
    liquidity: Dict[str, float] = {}

    if _is_flat_historical(research_dir):
        markets_f = research_dir / "markets.parquet"
        if markets_f.exists():
            try:
                _want = ["conditionId", "volumeNum", "volume", "volume24hr", "liquidityNum"]
                import pyarrow.parquet as _pq
                _avail = {f.name for f in _pq.read_schema(markets_f)}
                mdf = pd.read_parquet(markets_f, columns=[c for c in _want if c in _avail])
                id_col = "conditionId" if "conditionId" in mdf.columns else None
                vol_col = next(
                    (c for c in ["volumeNum", "volume", "volume24hr", "liquidityNum"] if c in mdf.columns),
                    None,
                )
                if id_col:
                    for _, row in mdf.iterrows():
                        mid = str(row[id_col]).strip()
                        vol = float(row[vol_col]) if vol_col and pd.notna(row.get(vol_col)) else 100_000.0
                        if mid:
                            liquidity[mid] = vol
            except Exception as e:
                print(f"  Warning: could not load markets.parquet: {e}")
        return liquidity

    if categories is None:
        categories = get_research_categories(research_dir)
    for cat in categories:
        try:
            mdf = load_research_markets(research_dir, [cat])
            if mdf.empty:
                continue
            id_col = next((c for c in ["conditionId", "market_id"] if c in mdf.columns), None)
            if not id_col:
                continue
            vol_col = next(
                (c for c in ["volumeClob", "volume", "volumeNum", "liquidityNum"] if c in mdf.columns),
                None,
            )
            for _, row in mdf.iterrows():
                mid = str(row[id_col]).strip()
                vol = float(row[vol_col]) if vol_col and pd.notna(row.get(vol_col)) else 100_000.0
                if mid:
                    liquidity[mid] = vol
        except Exception:
            pass
    return liquidity


# ── Token ID lookup ────────────────────────────────────────────────────────────

_token_cache: Dict[str, Dict] = {}  # conditionId -> {yes_token, no_token, title}


def get_token_ids(condition_id: str, session: requests.Session) -> Dict:
    """
    Return {yes_token: str, no_token: str, title: str} for a conditionId.
    Cached after first lookup.
    """
    if condition_id in _token_cache:
        return _token_cache[condition_id]
    try:
        resp = session.get(
            f"{GAMMA_API}/markets",
            params={"conditionId": condition_id, "limit": 1},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data:
            m = data[0]
            tokens = m.get("clobTokenIds") or []
            if isinstance(tokens, str):
                import ast
                tokens = ast.literal_eval(tokens)
            result = {
                "yes_token": str(tokens[0]) if len(tokens) > 0 else "",
                "no_token":  str(tokens[1]) if len(tokens) > 1 else "",
                "title":     m.get("question", "")[:80],
                "active":    bool(m.get("active", True)),
                "closed":    bool(m.get("closed", False)),
            }
            _token_cache[condition_id] = result
            return result
    except Exception:
        pass
    return {"yes_token": "", "no_token": "", "title": "", "active": True, "closed": False}


# ── Resolution detection (background thread) ──────────────────────────────────

class ResolutionMonitor:
    """
    Background thread that polls Gamma API for open positions that have resolved.
    Calls on_resolved(market_id, winner) when a market closes.
    """

    def __init__(
        self,
        state: StrategyState,
        session: requests.Session,
        on_resolved,
        poll_interval: int = 300,
    ):
        self._state = state
        self._session = session
        self._on_resolved = on_resolved
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(self._poll_interval):
            open_ids = {p.market_id for p in self._state.positions}
            if not open_ids:
                continue
            for cid in list(open_ids):
                if self._stop.is_set():
                    break
                try:
                    resp = self._session.get(
                        f"{GAMMA_API}/markets",
                        params={"conditionId": cid, "limit": 1},
                        timeout=10,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    if not data:
                        continue
                    m = data[0]
                    if m.get("closed") or not m.get("active", True):
                        # Determine winner from outcomePrices
                        outcome_prices = m.get("outcomePrices") or []
                        if isinstance(outcome_prices, str):
                            import ast
                            outcome_prices = ast.literal_eval(outcome_prices)
                        if outcome_prices:
                            prices = [float(p) for p in outcome_prices]
                            winner = "YES" if prices[0] >= 0.99 else ("NO" if prices[0] <= 0.01 else None)
                            if winner:
                                self._on_resolved(cid, winner)
                    time.sleep(0.2)
                except Exception:
                    pass


# ── Signal → position ──────────────────────────────────────────────────────────

def query_live_spread(token_id: str, session: requests.Session) -> float:
    """
    Fetch the current best bid-ask spread for a token from the CLOB order book.
    Returns spread in price units (e.g. 0.05 = 5¢).  Returns 0.0 on failure.
    """
    if not token_id:
        return 0.0
    try:
        resp = session.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout=8,
        )
        resp.raise_for_status()
        book = resp.json()
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 1.0
        return max(0.0, best_ask - best_bid)
    except Exception:
        return 0.0


def query_market_price(condition_id: str, session: requests.Session) -> Optional[float]:
    """Get current YES price from Gamma outcomePrices. Returns None on failure."""
    try:
        resp = session.get(
            f"{GAMMA_API}/markets",
            params={"conditionId": condition_id, "limit": 1},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        if data:
            prices = data[0].get("outcomePrices") or []
            if isinstance(prices, str):
                import ast
                prices = ast.literal_eval(prices)
            if prices:
                return float(prices[0])
    except Exception:
        pass
    return None


def process_signal(
    signal: dict,
    state: StrategyState,
    market_liquidity: Dict[str, float],
    cfg: WhaleConfig,
    session: requests.Session,
    paper_trader: Optional[PaperTrader],
    order_router: Optional[OrderRouter],
    state_path: Path,
    log_path: Path,
    dry_run: bool,
    max_entry_spread: float,
    market_supporting_whales: Optional[Dict[str, Set[str]]] = None,
) -> bool:
    """
    Size a confirmed signal and submit to paper trader or live order router.
    Returns True if a position was opened.
    """
    mid        = signal["market_id"]
    direction  = signal["direction"]  # "buy" or "sell"
    price      = float(signal["price"])
    whale_addr = signal["whale_address"]
    score      = float(signal.get("whale_score", 7.0))
    wr         = float(signal.get("whale_winrate", 0.5))

    # Check for existing position in this market
    existing_pos = next((p for p in state.positions if p.market_id == mid), None)
    side_upper = "BUY" if direction == "buy" else "SELL"

    if existing_pos is not None:
        supporters = market_supporting_whales or {}
        if existing_pos.side == side_upper:
            # Same direction: layer up position size
            supporters.setdefault(mid, set()).add(whale_addr)
            add_size = calculate_position_size(
                WhaleSignal(
                    market_id=mid, category=signal.get("category", ""),
                    whale_address=whale_addr, side=side_upper, price=price,
                    size_usd=0.0, score=score,
                    datetime=pd.Timestamp.now(tz="UTC"), historical_winrate=wr,
                ),
                state,
                market_liquidity.get(mid, float(signal.get("market_liquidity", 100_000))),
            )
            if add_size and add_size >= 1_000 and state.available() >= add_size:
                if existing_pos.side == "BUY":
                    old_shares = existing_pos.size_usd / max(existing_pos.entry_price, 1e-6)
                    new_shares = add_size / max(price, 1e-6)
                    existing_pos.size_usd += add_size
                    existing_pos.entry_price = existing_pos.size_usd / (old_shares + new_shares)
                else:
                    old_no = max(1.0 - existing_pos.entry_price, 1e-6)
                    new_no = max(1.0 - price, 1e-6)
                    old_shares = existing_pos.size_usd / old_no
                    new_shares = add_size / new_no
                    existing_pos.size_usd += add_size
                    existing_pos.entry_price = 1.0 - (existing_pos.size_usd / (old_shares + new_shares))
                state.category_exposure[existing_pos.category] = (
                    state.category_exposure.get(existing_pos.category, 0) + add_size
                )
                state.market_exposure[mid] = existing_pos.size_usd
                state.whale_exposure[whale_addr] = state.whale_exposure.get(whale_addr, 0) + add_size
                _save_positions(state, state_path)
                _log_trade(log_path, {**signal, "action": "LAYER_UP", "add_size": add_size,
                                       "new_total": existing_pos.size_usd})
                print(f"  LAYER   {signal.get('market_title', mid[:30])}  +${add_size:,.0f}  "
                      f"total=${existing_pos.size_usd:,.0f}")
            return False
        else:
            # Opposite direction
            if whale_addr in supporters.get(mid, set()):
                # A supporting whale is reversing — remove from supporter set
                supporters[mid].discard(whale_addr)
                if not supporters.get(mid):
                    # No supporters left: exit position
                    current_price = query_market_price(mid, session) or price
                    close_position(mid, current_price, "WHALE_EXIT",
                                   state, paper_trader, state_path, log_path)
                    supporters.pop(mid, None)
            else:
                # Non-supporter conflicting signal: close and flip
                current_price = query_market_price(mid, session) or price
                close_position(mid, current_price, "CONFLICTING_SIGNAL",
                               state, paper_trader, state_path, log_path)
                # Fall through to open new position below
                existing_pos = None

        if existing_pos is not None:
            return False

    # Resolve token IDs from Gamma
    token_info = get_token_ids(mid, session)
    token_id   = token_info["yes_token"] if direction == "buy" else token_info["no_token"]

    # Pre-trade spread gate: query live order book, abort if illiquid
    if max_entry_spread > 0 and token_id:
        spread = query_live_spread(token_id, session)
        if spread > max_entry_spread:
            print(
                f"  SKIP {mid[:24]}  spread={spread*100:.1f}¢ > max={max_entry_spread*100:.1f}¢"
            )
            return False

    # Build a WhaleSignal for the position sizer
    ws = WhaleSignal(
        market_id=mid,
        category=signal.get("category", ""),
        whale_address=whale_addr,
        side="BUY" if direction == "buy" else "SELL",
        price=price,
        size_usd=0.0,
        score=score,
        datetime=pd.Timestamp.now(tz="UTC"),
        historical_winrate=wr,
    )

    liq = market_liquidity.get(mid, float(signal.get("market_liquidity", 100_000)))
    size = calculate_position_size(ws, state, liq)
    if size is None or size <= 0:
        return False
    size = math.floor(size)
    if size < 1:
        return False

    # Near-resolved guard
    if price > cfg.max_entry_yes_price:
        return False

    print(
        f"  SIGNAL  {signal.get('market_title', mid[:30])}  "
        f"{'BUY' if direction=='buy' else 'SELL'} ${size:,.0f} @ {price*100:.1f}¢  "
        f"score={score:.1f} wr={wr:.0%}  whale={whale_addr[:8]}..."
    )

    if dry_run:
        # Record without executing
        _log_trade(log_path, {**signal, "action": "DRY_RUN", "size_usd": size})
        return False

    # Paper trade execution
    if paper_trader is not None:
        paper_signal = {
            "market_id": mid,
            "direction": direction,
            "size_usd":  size,
            "token_id":  token_id,
            "source":    "confirmed_whale",
        }
        paper_trader.process_signal(paper_signal)

    # Update our own StrategyState so risk limits remain accurate
    pos = Position(
        market_id=mid,
        category=signal.get("category", ""),
        side=ws.side,
        entry_price=price,
        size_usd=size,
        whale_address=whale_addr,
        whale_score=score,
        entry_date=pd.Timestamp.now(tz="UTC"),
        whale_winrate=wr,
    )
    state.positions.append(pos)
    if market_supporting_whales is not None:
        market_supporting_whales[mid] = {whale_addr}
    state.category_exposure[pos.category] = state.category_exposure.get(pos.category, 0) + size
    state.whale_exposure[whale_addr]       = state.whale_exposure.get(whale_addr, 0) + size
    state.market_exposure[mid]             = size

    _save_positions(state, state_path)
    _log_trade(log_path, {**signal, "action": "OPEN", "size_usd": size, "entry_price": price})
    return True


def close_position(
    mid: str,
    exit_price: float,
    reason: str,
    state: StrategyState,
    paper_trader: Optional[PaperTrader],
    state_path: Path,
    log_path: Path,
) -> None:
    pos = next((p for p in state.positions if p.market_id == mid), None)
    if pos is None:
        return

    if pos.side == "BUY":
        gross_pnl = (exit_price - pos.entry_price) * (pos.size_usd / max(pos.entry_price, 1e-6))
    else:
        entry_no  = 1.0 - pos.entry_price
        gross_pnl = (exit_price - entry_no) * (pos.size_usd / max(entry_no, 1e-6))
    net_pnl = gross_pnl * 0.97

    print(
        f"  CLOSE  {mid[:30]}  {reason}  "
        f"pnl=${net_pnl:+,.0f}  exit={exit_price:.3f}"
    )

    state.positions = [p for p in state.positions if p.market_id != mid]
    state.category_exposure[pos.category] = max(
        0, state.category_exposure.get(pos.category, 0) - pos.size_usd
    )
    state.whale_exposure[pos.whale_address] = max(
        0, state.whale_exposure.get(pos.whale_address, 0) - pos.size_usd
    )
    state.market_exposure.pop(mid, None)

    _save_positions(state, state_path)
    _log_trade(log_path, {
        "action": "CLOSE", "market_id": mid, "reason": reason,
        "exit_price": exit_price, "net_pnl": net_pnl,
    })

    if paper_trader:
        # Find matching paper position and close it
        for pid, ppos in list(paper_trader.account.positions.items()):
            if ppos.market_id == mid:
                paper_trader.close_position(pid, notes=reason)
                break


# ── Live polling loop ──────────────────────────────────────────────────────────

def _log_equity(equity_log_path: Path, equity: float, state: StrategyState) -> None:
    equity_log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "equity": round(equity, 2),
        "positions": len(state.positions),
        "deployed": round(state.deployed(), 2),
    }
    with open(equity_log_path, "a") as f:
        f.write(json.dumps(record) + "\n")


def polling_loop(
    buffer: LiveTradeBuffer,
    state: StrategyState,
    market_liquidity: Dict[str, float],
    cfg: WhaleConfig,
    session: requests.Session,
    paper_trader: Optional[PaperTrader],
    order_router: Optional[OrderRouter],
    state_path: Path,
    log_path: Path,
    dry_run: bool,
    poll_interval: int,
    max_drawdown: float,
    max_entry_spread: float,
    research_dir: Path,
    categories: Optional[list],
    resolutions_dir: Optional[Path],
    whale_refresh_hours: int = 168,
    equity_log_path: Path = DEFAULT_EQUITY_LOG,
) -> None:
    last_ts     = int(time.time()) - 2 * poll_interval
    seen_hashes: Set[str] = set()
    alerts      = 0
    risk_paused = False
    last_whale_refresh = time.time()
    market_supporting_whales: Dict[str, Set[str]] = {}

    def _on_resolved(mid: str, winner: str):
        pos = next((p for p in state.positions if p.market_id == mid), None)
        if pos is None:
            return
        if pos.side == "BUY":
            exit_price = 1.0 if winner == "YES" else 0.0
        else:
            exit_price = 1.0 if winner == "NO" else 0.0
        close_position(mid, exit_price, f"RESOLVED_{winner}", state, paper_trader, state_path, log_path)
        market_supporting_whales.pop(mid, None)

    resolution_monitor = ResolutionMonitor(state, session, _on_resolved, poll_interval=300)
    resolution_monitor.start()

    mode_label = "DRY RUN" if dry_run else ("LIVE TRADING" if order_router else "PAPER TRADING")
    print(f"\n{'='*65}")
    print(f"  Live Strategy  —  {mode_label}")
    print(f"{'='*65}")
    print(f"  Capital:        ${state.total_capital:,.0f}")
    print(f"  Open positions: {len(state.positions)}")
    print(f"  Max drawdown:   {max_drawdown:.0%}")
    print(f"  Poll interval:  {poll_interval}s")
    print(f"  Whale refresh:  every {whale_refresh_hours}h")
    print(f"{'='*65}\n")

    try:
        while True:
            poll_start = time.time()

            # ── Weekly whale-set refresh ───────────────────────────────────────
            if time.time() - last_whale_refresh > whale_refresh_hours * 3600:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Refreshing whale set...")
                try:
                    ws, sc, wr = build_whale_set(research_dir, categories, resolutions_dir, cfg,
                                                 market_volumes=market_liquidity)
                    buffer.update_whale_set(ws, wr, sc)
                    print(f"  Updated: {len(ws):,} whales")
                    last_whale_refresh = time.time()
                except Exception as e:
                    print(f"  Warning: whale refresh failed: {e}")

            # ── Drawdown kill-switch ───────────────────────────────────────────
            if paper_trader:
                equity   = paper_trader.account.equity
                dd       = 1 - (equity / paper_trader.account.initial_capital)
                if dd >= max_drawdown and not risk_paused:
                    risk_paused = True
                    print(f"RISK PAUSE: drawdown {dd:.1%} >= {max_drawdown:.0%}. Pausing new positions.")
                elif dd < max_drawdown * 0.8 and risk_paused:
                    risk_paused = False
                    print(f"RISK RESUME: drawdown recovered to {dd:.1%}")

            # ── Poll trades ───────────────────────────────────────────────────
            try:
                resp = session.get(
                    f"{DATA_API}/trades",
                    params={"limit": 500, "after": last_ts},
                    timeout=20,
                )
                resp.raise_for_status()
                trades_raw = sorted(resp.json() or [], key=lambda t: int(t.get("timestamp", 0) or 0))
            except Exception as e:
                print(f"  [poll error] {e}")
                trades_raw = []

            new_last_ts = last_ts
            for raw in trades_raw:
                ts = int(float(raw.get("timestamp", 0) or 0))
                if ts > new_last_ts:
                    new_last_ts = ts

                tx = str(raw.get("transactionHash", "") or "")
                if tx:
                    if tx in seen_hashes:
                        continue
                    seen_hashes.add(tx)
                    if len(seen_hashes) > 50_000:
                        seen_hashes.clear()

                # Normalise to buffer format
                price = float(raw.get("price", 0) or 0)
                size  = float(raw.get("size", 0) or 0)
                usd   = float(raw.get("usdcSize", raw.get("amount", 0)) or 0) or price * size
                if price <= 0 or usd <= 0:
                    continue

                trade = {
                    "market_id":  str(raw.get("conditionId", "") or ""),
                    "maker":      str(raw.get("proxyWallet", raw.get("maker", "")) or ""),
                    "direction":  str(raw.get("side", "BUY") or "BUY").upper(),
                    "price":      price,
                    "usd_amount": usd,
                    "category":   "",
                    "token_id":   str(raw.get("asset", "") or ""),
                    "datetime":   datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc),
                    "market_title": str(raw.get("title", "") or ""),
                }

                signals = buffer.add(trade)
                for sig in signals:
                    sig["market_title"] = trade.get("market_title", "")
                    if not risk_paused:
                        opened = process_signal(
                            sig, state, market_liquidity, cfg, session,
                            paper_trader, order_router, state_path, log_path,
                            dry_run, max_entry_spread,
                            market_supporting_whales=market_supporting_whales,
                        )
                        if opened:
                            alerts += 1

            last_ts = new_last_ts

            # --- Partial exit: close 50% of position when gain >= threshold ---
            for pos in list(state.positions):
                if pos.partial_exit_done:
                    continue
                current_price = query_market_price(pos.market_id, session)
                if current_price is None:
                    continue
                if pos.side == "BUY":
                    gain = (current_price - pos.entry_price) / max(pos.entry_price, 1e-6)
                else:
                    gain = (pos.entry_price - current_price) / max(1.0 - pos.entry_price, 1e-6)
                if gain >= cfg.partial_exit_gain_threshold:
                    half = pos.size_usd * cfg.partial_exit_fraction
                    pos.size_usd -= half
                    pos.partial_exit_done = True
                    state.category_exposure[pos.category] = max(
                        0, state.category_exposure.get(pos.category, 0) - half
                    )
                    state.market_exposure[pos.market_id] = pos.size_usd
                    state.whale_exposure[pos.whale_address] = max(
                        0, state.whale_exposure.get(pos.whale_address, 0) - half
                    )
                    _save_positions(state, state_path)
                    _log_trade(log_path, {
                        "action": "PARTIAL_EXIT", "market_id": pos.market_id,
                        "exit_price": current_price, "size_closed": half,
                        "gain": gain,
                    })
                    print(f"  PARTIAL {pos.market_id[:30]}  gain={gain:.1%}  "
                          f"closed ${half:,.0f}, remaining ${pos.size_usd:,.0f}")

            elapsed = time.time() - poll_start
            ts_s = datetime.now().strftime("%H:%M:%S")
            buf_stats = buffer.stats()
            deployed  = state.deployed()
            equity    = paper_trader.account.equity if paper_trader else state.total_capital
            equity_s  = f"${equity:,.2f}" if paper_trader else "n/a"
            print(
                f"  [{ts_s}] trades={len(trades_raw)}  "
                f"signals={buf_stats['signals_emitted']}  "
                f"positions={len(state.positions)}  "
                f"deployed=${deployed:,.0f}  "
                f"equity={equity_s}  "
                f"{'PAUSED' if risk_paused else 'active'}",
                flush=True,
            )
            if paper_trader:
                _log_equity(equity_log_path, equity, state)
            time.sleep(max(0, poll_interval - elapsed))

    except KeyboardInterrupt:
        print(f"\n\nStopped. Signals emitted: {alerts}")
    finally:
        resolution_monitor.stop()
        _save_positions(state, state_path)
        if paper_trader:
            paper_trader._save_state()


# ── Replay mode ────────────────────────────────────────────────────────────────

def replay_loop(
    buffer: LiveTradeBuffer,
    research_dir: Path,
    categories: Optional[list],
    state: StrategyState,
    market_liquidity: Dict[str, float],
    cfg: WhaleConfig,
    session: requests.Session,
    state_path: Path,
    log_path: Path,
    speed: float = 0,
) -> None:
    """
    Feed historical parquet trades through the buffer in chronological order.
    Useful for validating that live signals match the backtest output.
    """
    if categories is None:
        categories = get_research_categories(research_dir)

    all_trades = []
    if _is_flat_historical(research_dir):
        df = load_historical_trades(research_dir)
        if not df.empty:
            all_trades.append(df)
    else:
        for cat in categories:
            try:
                df = load_research_trades(research_dir, [cat])
                if not df.empty:
                    df["_cat"] = cat
                    all_trades.append(df)
            except Exception:
                pass

    if not all_trades:
        print("No trades found for replay.")
        return

    trades_df = pd.concat(all_trades, ignore_index=True)
    trades_df = trades_df.sort_values("datetime").reset_index(drop=True)

    print(f"\nReplay: {len(trades_df):,} trades from {trades_df['datetime'].min().date()} "
          f"to {trades_df['datetime'].max().date()}")
    print("Press Ctrl+C to stop.\n")

    signals_emitted = 0
    try:
        for _, row in trades_df.iterrows():
            usd = float(row.get("usd_amount", 0) or 0)
            price = float(row.get("price", 0) or 0)

            trade = {
                "market_id":  str(row.get("market_id", row.get("conditionId", "")) or ""),
                "maker":      str(row.get("maker", row.get("proxyWallet", "")) or ""),
                "direction":  str(row.get("maker_direction", row.get("side", "BUY")) or "BUY").upper(),
                "price":      price,
                "usd_amount": usd,
                "category":   str(row.get("category", row.get("_cat", "")) or ""),
                "token_id":   str(row.get("asset_id", "") or ""),
                "datetime":   row.get("datetime", datetime.now(timezone.utc)),
            }

            signals = buffer.add(trade)
            for sig in signals:
                signals_emitted += 1
                ts_s = str(trade["datetime"])[:16]
                print(
                    f"[REPLAY] {ts_s}  {sig['market_id'][:20]}  "
                    f"{sig['direction'].upper()} @ {sig['price']*100:.1f}¢  "
                    f"score={sig['whale_score']:.1f}  wr={sig['whale_winrate']:.0%}  "
                    f"confs={sig['confirming_whales']}"
                )
                _log_trade(log_path, {**sig, "mode": "replay"})

            if speed > 0:
                time.sleep(speed)

    except KeyboardInterrupt:
        pass

    print(f"\nReplay complete. Signals emitted: {signals_emitted}")
    buf_stats = buffer.stats()
    print(f"  Trades seen:           {buf_stats['trades_seen']:,}")
    print(f"  Trades passed filter:  {buf_stats['trades_passed_filter']:,}")
    print(f"  Signals emitted:       {buf_stats['signals_emitted']:,}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live whale-following strategy orchestrator",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--research-dir",   type=Path, default=_project_root / "data" / "historical")
    parser.add_argument("--resolutions-dir", type=Path, default=None,
                        help="Extra resolutions CSV dir (default: data/poly_cat)")
    parser.add_argument("--categories",     default=None,
                        help="Comma-separated categories (default: all)")
    parser.add_argument("--capital",        type=float, default=None,
                        help="Trading capital in USD (default: fetch from Polymarket balance)")
    parser.add_argument("--min-position-usd", type=float, default=None,
                        help="Min position size in USD (auto-scales to 4%% of capital if unset)")
    parser.add_argument("--min-usd",        type=float, default=500.0,
                        help="Min USD trade size to consider as a whale signal")
    parser.add_argument("--min-wallet-wr",  type=float, default=0.60,
                        help="Min historical win-rate to follow a whale")
    parser.add_argument("--min-confirmations", type=int, default=1,
                        help="Distinct whales needed to confirm a signal")
    parser.add_argument("--confirmation-window-hours", type=int, default=168)
    parser.add_argument("--cooldown-hours", type=int, default=168)
    parser.add_argument("--max-drawdown",   type=float, default=0.15,
                        help="Risk kill-switch: pause at this drawdown fraction")
    parser.add_argument("--max-spread",     type=float, default=0.05,
                        help="Max bid-ask spread to enter a position (0=disabled)")
    parser.add_argument("--interval",       type=int, default=60,
                        help="Poll interval in seconds")
    parser.add_argument("--whale-refresh-hours", type=int, default=168,
                        help="How often to rebuild the whale set")
    parser.add_argument("--state-path",     type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--log",            type=Path, default=DEFAULT_TRADE_LOG)
    parser.add_argument("--skip-refresh",   action="store_true",
                        help="Skip data refresh and use existing data as-is")
    parser.add_argument("--dry-run",        action="store_true",
                        help="Log signals without placing orders")
    parser.add_argument("--live",           action="store_true",
                        help="Use real OrderRouter (requires POLYMARKET_PRIVATE_KEY)")
    parser.add_argument("--replay",         action="store_true",
                        help="Replay historical parquet trades through buffer")
    parser.add_argument("--replay-speed",   type=float, default=0,
                        help="Seconds to sleep between replay trades (0=fastest)")
    args = parser.parse_args()

    categories   = [c.strip() for c in args.categories.split(",")] if args.categories else None
    resolutions_dir = args.resolutions_dir or (_project_root / "data" / "poly_cat")
    session      = requests.Session()
    session.headers["User-Agent"] = "live-strategy/1.0"
    cfg          = load_whale_config()

    # Auto-detect capital from Polymarket balance if not specified
    if args.capital is None:
        try:
            import os
            from dotenv import load_dotenv
            load_dotenv(_project_root / ".env")
            from eth_account import Account
            from py_clob_client_v2 import ClobClient as _ClobV2
            from py_clob_client_v2.clob_types import BalanceAllowanceParams as _BAP, AssetType as _AT, ApiCreds as _AC
            _pk = os.getenv("POLYMARKET_PRIVATE_KEY", "")
            if _pk:
                _pk = _pk if _pk.startswith("0x") else "0x" + _pk
                _addr = Account.from_key(_pk).address
                _creds = _AC(
                    api_key=os.getenv("POLYMARKET_API_KEY", ""),
                    api_secret=os.getenv("POLYMARKET_API_SECRET", ""),
                    api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE", ""),
                ) if os.getenv("POLYMARKET_API_KEY") else None
                _c = _ClobV2("https://clob.polymarket.com", chain_id=137, key=_pk,
                              creds=_creds, signature_type=2, funder=_addr)
                _r = _c.get_balance_allowance(_BAP(asset_type=_AT.COLLATERAL, signature_type=2))
                _bal = int(_r.get("balance", 0)) / 1e6
                if _bal > 0:
                    args.capital = _bal
                    print(f"  Detected Polymarket balance: ${_bal:,.2f}")
        except Exception as _e:
            print(f"  Warning: could not fetch balance ({_e})")
        if args.capital is None:
            args.capital = 1000.0
            print(f"  Using default capital: ${args.capital:,.2f}")

    # Scale minimum position size to capital (enables small-capital live trading).
    # Default RISK_LIMITS["min_position_usd"] = 5000 which rejects all trades on <$1k capital.
    min_pos = args.min_position_usd
    if min_pos is None:
        min_pos = max(1.0, args.capital * 0.04)
    RISK_LIMITS["min_position_usd"] = min_pos
    print(f"  min_position_usd set to ${min_pos:.2f}")

    # ── [0/3] Refresh historical data if stale ────────────────────────────────
    if args.skip_refresh:
        print("\n[0/3] Skipping data refresh (--skip-refresh)")
    else:
        print("\n[0/3] Checking data freshness...")
        _refresh_data_if_stale(args.research_dir, max_age_hours=24)

    # ── [1/3] Build market liquidity map (needed for whale scoring) ───────────
    print("\n[1/3] Building market liquidity map...")
    market_liquidity = build_market_liquidity(args.research_dir, categories)
    print(f"  {len(market_liquidity):,} markets")

    # ── [2/3] Build whale set ──────────────────────────────────────────────────
    print("\n[2/3] Building whale set from research data...")
    whale_set, scores, winrates = build_whale_set(
        args.research_dir, categories, resolutions_dir, cfg,
        market_volumes=market_liquidity,
    )
    n = len(whale_set)
    print(f"  {n:,} qualified whales")
    if scores:
        top = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:20]
        print(f"\n  Top 20 whales by score:")
        print(f"  {'Address':<12}  {'Score':>6}  {'WinRate':>7}  {'Trades':>6}")
        print(f"  {'-'*12}  {'-'*6}  {'-'*7}  {'-'*6}")
        for addr, sc in top:
            wr = winrates.get(addr, 0.0)
            print(f"  {addr[:10]}..  {sc:6.2f}  {wr:7.1%}  ")
        print()

    # ── [3/3] Restore / init position state ──────────────────────────────────
    print("\n[3/3] Restoring position state...")
    state = _load_positions(args.state_path, args.capital)

    # ── Build LiveTradeBuffer ──────────────────────────────────────────────────
    buffer = LiveTradeBuffer(
        whale_set=whale_set,
        whale_winrates=winrates,
        market_liquidity=market_liquidity,
        whale_scores=scores,
        min_size_usd=args.min_usd,
        min_wallet_wr=args.min_wallet_wr,
        min_confirmations=args.min_confirmations,
        confirmation_window_hours=args.confirmation_window_hours,
        cooldown_hours=args.cooldown_hours,
        max_entry_yes_price=cfg.max_entry_yes_price,
        state_path=DEFAULT_BUFFER_STATE,
    )

    # ── Replay mode ────────────────────────────────────────────────────────────
    if args.replay:
        print("\nRunning in REPLAY mode — no orders will be placed\n")
        replay_loop(
            buffer=buffer,
            research_dir=args.research_dir,
            categories=categories,
            state=state,
            market_liquidity=market_liquidity,
            cfg=cfg,
            session=session,
            state_path=args.state_path,
            log_path=args.log,
            speed=args.replay_speed,
        )
        return 0

    # ── Set up paper trader ────────────────────────────────────────────────────
    paper_trader: Optional[PaperTrader] = None
    order_router: Optional[OrderRouter] = None

    if args.live:
        order_router = OrderRouter(dry_run=False)
        if not order_router.is_authenticated():
            print("ERROR: Live mode requires POLYMARKET_PRIVATE_KEY env var.")
            print("       Install py-clob-client:  pip install py-clob-client")
            return 1
        print("Live order router initialised.")
    elif not args.dry_run:
        paper_trader = PaperTrader(
            initial_capital=args.capital,
            state_path=str(args.state_path.parent / "paper_state.json"),
            log_path=str(args.log.parent / "paper_log.jsonl"),
            max_position_size=min(args.capital * 0.20, 50_000),
            max_positions=30,
        )

    dry_run = args.dry_run or (paper_trader is None and order_router is None)

    # ── Live polling loop ──────────────────────────────────────────────────────
    polling_loop(
        buffer=buffer,
        state=state,
        market_liquidity=market_liquidity,
        cfg=cfg,
        session=session,
        paper_trader=paper_trader,
        order_router=order_router,
        state_path=args.state_path,
        log_path=args.log,
        dry_run=dry_run,
        poll_interval=args.interval,
        max_drawdown=args.max_drawdown,
        max_entry_spread=args.max_spread,
        research_dir=args.research_dir,
        categories=categories,
        resolutions_dir=resolutions_dir,
        whale_refresh_hours=args.whale_refresh_hours,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

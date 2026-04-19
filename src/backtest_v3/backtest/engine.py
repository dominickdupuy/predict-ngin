"""
Core backtest engine.

Loop
----
  for t in decision_grid(start, end, step):
      universe = LiquidUniverse.snapshot(t)
      for strategy in strategies:
          signals = strategy.emit(t, universe.condition_ids)
          for s in signals:
              book = reconstruct_book(s.condition_id, t)
              fill = executor.execute_market(book, s.side, s.notional_usd * s.conviction)
              open_position(s, fill)
      # check-and-close any positions whose exit condition met
      close_matured(t)
  close_all_at_horizon(end)

Look-ahead guards
-----------------
1. `Signal.__post_init__` requires available_at_s <= as_of_s.
2. The loader only ever returns rows at timestamp <= as_of.
3. The executor consumes a book snapshot built from trades <= as_of; no
   future trades leak in.
4. Exits at end of backtest are valued at last-known mid <= horizon_end_s,
   or at final resolution if known (resolution date is a strictly-future
   event that is *fair* to use at exit time).

Capacity model
--------------
Every fill already encodes slippage (book walk) + fees. The per-strategy
PnL is therefore already net of realistic execution costs. The
`capital_scale` multiplier lets you sweep Sharpe-vs-capital without
changing strategy params: all notional sizes get multiplied by this,
so fills hit more levels and slippage rises accordingly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ..data.clob_book import CLOBBookReconstructor, BookSnapshot
from ..data.loader import PITDataLoader
from ..data.universe import LiquidUniverse, UniverseSnapshot
from ..execution.book_executor import BookExecutor, Fill
from ..strategies.base import Signal, SignalSide, V3Strategy


@dataclass
class _OpenPosition:
    signal: Signal
    entry_fill: Fill
    category: str
    opened_at_s: int
    exit_deadline_s: int
    entry_mid: float = 0.0              # mid at entry for stop/trail math
    max_favorable_mid: float = 0.0      # best mid seen so far (for trail)
    trail_armed: bool = False


@dataclass
class Trade:
    strategy_name: str
    condition_id: str
    category: str
    entry_s: int
    exit_s: int
    side: str
    requested_usd: float
    entry_filled_usd: float
    entry_px: float
    exit_px: float
    pnl_usd: float
    entry_cost_bps: float
    exit_cost_bps: float
    exit_reason: str
    capital_scale: float
    liquidity_threshold: float
    param_hash: str
    features: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BacktestResult:
    trades: pd.DataFrame           # one row per closed trade
    daily_pnl: pd.Series           # indexed by date
    metrics: Dict[str, float]
    config: Dict[str, Any]
    per_strategy_metrics: Dict[str, Dict[str, float]]

    def summary(self) -> str:
        lines = [f"=== Backtest summary ({self.config.get('label', 'run')}) ==="]
        for k, v in self.metrics.items():
            lines.append(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")
        for name, m in self.per_strategy_metrics.items():
            lines.append(f"  -- {name}:")
            for k, v in m.items():
                lines.append(f"     {k}: {v:.4f}" if isinstance(v, float) else f"     {k}: {v}")
        return "\n".join(lines)


def _sharpe(daily_pnl: pd.Series) -> float:
    if daily_pnl.empty:
        return 0.0
    std = float(daily_pnl.std(ddof=1))
    if std <= 0:
        return 0.0
    return float(daily_pnl.mean()) / std * np.sqrt(252)


def _sortino(daily_pnl: pd.Series) -> float:
    if daily_pnl.empty:
        return 0.0
    downside = daily_pnl[daily_pnl < 0]
    std = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
    if std <= 0:
        return 0.0
    return float(daily_pnl.mean()) / std * np.sqrt(252)


def _max_drawdown(daily_pnl: pd.Series) -> float:
    if daily_pnl.empty:
        return 0.0
    cum = daily_pnl.cumsum()
    running = cum.cummax()
    dd = (cum - running)
    return float(dd.min())


@dataclass
class EngineConfig:
    start_s: int
    end_s: int
    step_s: int = 4 * 3600              # 4h decision grid default
    liquidity_threshold_usd: float = 500_000.0
    liquidity_lookback_s: int = 30 * 24 * 3600
    capital_scale: float = 1.0          # multiplies every signal notional
    executor_level_fill_fraction: float = 0.60
    executor_max_depth_fraction: float = 0.50
    taker_fee_bps: float = 0.0          # Polymarket currently charges 0 (see COST_MODEL_AUDIT)
    reconstructor_kwargs: Dict[str, Any] = field(default_factory=dict)
    label: str = "run"
    max_open_per_market: int = 1        # prevents double-positioning same market
    force_taker_execution: bool = False # pessimistic bound: ignore limit_price


class BacktestEngine:
    def __init__(
        self,
        loader: PITDataLoader,
        strategies: Sequence[V3Strategy],
        config: EngineConfig,
    ):
        self.loader = loader
        self.strategies = list(strategies)
        self.config = config
        self.reconstructor = CLOBBookReconstructor(**config.reconstructor_kwargs)
        self.universe = LiquidUniverse(
            loader,
            threshold_usd=config.liquidity_threshold_usd,
            lookback_s=config.liquidity_lookback_s,
        )
        from ..execution.book_executor import ExecutorConfig
        self.executor = BookExecutor(ExecutorConfig(
            taker_fee_bps=config.taker_fee_bps,
            level_fill_fraction=config.executor_level_fill_fraction,
            max_depth_fraction=config.executor_max_depth_fraction,
        ))

    # -------------------------------------------------------------- public API

    def run(self) -> BacktestResult:
        cfg = self.config
        open_positions: List[_OpenPosition] = []
        closed_trades: List[Trade] = []
        decision_times = list(self.loader.iter_decision_times(cfg.start_s, cfg.end_s, cfg.step_s))

        # Pre-compute which category each condition_id belongs to (for snapshot
        # lookups). We cache lazily in a dict to avoid O(N) scans per signal.
        cat_index: Dict[str, str] = self._build_category_index()

        for t in decision_times:
            # Close matured positions first (so freed capital & avoid self-overlap)
            open_positions, newly_closed = self._close_matured(open_positions, t, cat_index)
            closed_trades.extend(newly_closed)

            # Build universe at t (PIT-safe by construction)
            uni = self.universe.snapshot(t)
            if not uni.condition_ids:
                continue

            # Ask each strategy for signals
            live_markets = {p.signal.condition_id for p in open_positions}
            for strat in self.strategies:
                signals = strat.emit(t, uni.condition_ids)
                if not signals:
                    continue
                for sig in signals:
                    if sig.available_at_s > t:   # defensive re-check
                        continue
                    if cfg.max_open_per_market and sig.condition_id in live_markets:
                        continue
                    cat = cat_index.get(sig.condition_id)
                    if cat is None:
                        continue
                    trades = self.loader.get_trades(cat, as_of_s=t, condition_id=sig.condition_id,
                                                   lookback_s=self.reconstructor.local_window_s)
                    book = self.reconstructor.reconstruct(sig.condition_id, t, trades)
                    if book is None:
                        continue
                    scaled_notional = sig.notional_usd * sig.conviction * cfg.capital_scale
                    if scaled_notional <= 0:
                        continue
                    side_str = "BUY" if sig.side == SignalSide.BUY else "SELL"
                    fill = self._maybe_maker_fill(sig, cat, book, t, scaled_notional)
                    if fill is None:
                        fill = self.executor.execute_market(book, side_str, scaled_notional)
                    if not fill.filled or fill.filled_usd <= 0:
                        continue
                    deadline = t + sig.expected_hold_s
                    open_positions.append(_OpenPosition(
                        signal=sig, entry_fill=fill, category=cat,
                        opened_at_s=t, exit_deadline_s=deadline,
                        entry_mid=float(book.mid),
                        max_favorable_mid=float(book.mid),
                    ))
                    live_markets.add(sig.condition_id)

        # Close anything still open at the horizon
        final_positions, tail = self._close_at_horizon(open_positions, cfg.end_s, cat_index)
        closed_trades.extend(tail)

        df = self._trades_to_df(closed_trades)
        daily = self._daily_pnl(df)
        metrics = self._metrics(daily, df)
        per_strat = self._per_strategy_metrics(df)

        return BacktestResult(
            trades=df,
            daily_pnl=daily,
            metrics=metrics,
            config={
                "start_s": cfg.start_s, "end_s": cfg.end_s, "step_s": cfg.step_s,
                "liquidity_threshold_usd": cfg.liquidity_threshold_usd,
                "capital_scale": cfg.capital_scale,
                "label": cfg.label,
                "strategies": [s.name for s in self.strategies],
            },
            per_strategy_metrics=per_strat,
        )

    # -------------------------------------------------------------- internals

    def _build_category_index(self) -> Dict[str, str]:
        idx: Dict[str, str] = {}
        for cat in self.loader.categories_available():
            m = self.loader._load_markets(cat)
            if m.empty or "conditionId" not in m.columns:
                continue
            for cid in m["conditionId"].tolist():
                if cid not in idx:
                    idx[cid] = cat
        return idx

    def _exit_condition_met(
        self,
        pos: _OpenPosition,
        now_s: int,
        cat_index: Dict[str, str],
    ) -> Tuple[bool, str]:
        # Deadline first
        if now_s >= pos.exit_deadline_s:
            return True, "hold_deadline"

        cat = pos.category
        cur = self.loader.get_mid_price(cat, pos.signal.condition_id, now_s, max_staleness_s=3 * 3600)
        if cur is None:
            return False, ""

        side = pos.signal.side
        entry = pos.entry_mid if pos.entry_mid > 0 else pos.entry_fill.avg_fill_price
        if entry <= 0:
            return False, ""

        # Track favorable extreme for trailing stop
        if side == SignalSide.BUY and cur > pos.max_favorable_mid:
            pos.max_favorable_mid = float(cur)
        elif side == SignalSide.SELL and (pos.max_favorable_mid == pos.entry_mid or cur < pos.max_favorable_mid):
            pos.max_favorable_mid = float(cur)

        # Hard stop-loss (bps of entry mid, adverse direction)
        sl_bps = pos.signal.stop_loss_bps
        if sl_bps is not None and sl_bps > 0:
            if side == SignalSide.BUY:
                adverse_bps = (entry - cur) / entry * 10_000.0
            else:
                adverse_bps = (cur - entry) / entry * 10_000.0
            if adverse_bps >= sl_bps:
                return True, "stop_loss"

        # Trailing stop: arm after move > trigger in our favor, then exit if
        # we give back more than `giveback` from the favorable extreme.
        trig = pos.signal.trail_trigger_bps
        give = pos.signal.trail_giveback_bps
        if trig is not None and give is not None and trig > 0 and give > 0:
            if side == SignalSide.BUY:
                favor_bps = (pos.max_favorable_mid - entry) / entry * 10_000.0
            else:
                favor_bps = (entry - pos.max_favorable_mid) / entry * 10_000.0
            if favor_bps >= trig:
                pos.trail_armed = True
            if pos.trail_armed:
                if side == SignalSide.BUY:
                    giveback_bps = (pos.max_favorable_mid - cur) / entry * 10_000.0
                else:
                    giveback_bps = (cur - pos.max_favorable_mid) / entry * 10_000.0
                if giveback_bps >= give:
                    return True, "trail_stop"

        # Target (if any)
        target = pos.signal.exit_price
        if target is not None:
            if side == SignalSide.BUY and cur >= target:
                return True, "target_hit"
            if side == SignalSide.SELL and cur <= target:
                return True, "target_hit"
        return False, ""

    # ------------------------------------------------- maker-fill simulation
    def _maybe_maker_fill(
        self,
        sig: Signal,
        cat: str,
        book: BookSnapshot,
        as_of_s: int,
        notional: float,
    ) -> Optional[Fill]:
        """
        Simulate a resting maker quote. Return a Fill if a contra-side print
        crossed the limit within `maker_fill_window_s`, else None so the
        caller can fall back to taker execution.
        """
        if self.config.force_taker_execution:
            return None
        lp = sig.limit_price
        if lp is None:
            return None
        if book.mid <= 0:
            return None

        window = max(60, int(sig.maker_fill_window_s))
        trades = self.loader.get_trades(
            cat,
            as_of_s=as_of_s + window,
            condition_id=sig.condition_id,
            lookback_s=window,
        )
        if trades.empty:
            return None
        # Only count trades strictly after as_of_s (PIT hygiene).
        future = trades[trades["timestamp"] > as_of_s]
        if future.empty:
            return None

        # A BUY maker quote at `lp` fills when a SELL trade prints at price <= lp.
        # A SELL maker quote at `lp` fills when a BUY trade prints at price >= lp.
        if sig.side == SignalSide.BUY:
            filled_mask = (future["side"] == "SELL") & (future["price"] <= lp)
        else:
            filled_mask = (future["side"] == "BUY") & (future["price"] >= lp)
        hits = future[filled_mask]
        if hits.empty:
            return None

        filled_usd = float(hits["usd_amount"].sum())
        filled_usd = min(filled_usd, float(notional))
        if filled_usd <= 0:
            return None

        mid = float(book.mid)
        # Slippage is the signed cost relative to mid. A maker fills at `lp`
        # which is inside the spread — for a BUY, lp <= mid, so slip < 0
        # (a credit). We encode "cost" convention: positive = cost, so a
        # rebate reads as a negative number.
        if sig.side == SignalSide.BUY:
            slip_bps = (lp - mid) / mid * 10_000.0 if mid > 0 else 0.0
        else:
            slip_bps = (mid - lp) / mid * 10_000.0 if mid > 0 else 0.0
        return Fill(
            filled=True,
            side="BUY" if sig.side == SignalSide.BUY else "SELL",
            requested_usd=float(notional),
            filled_usd=filled_usd,
            avg_fill_price=float(lp),
            mid_before=mid,
            slippage_bps=slip_bps,
            taker_fee_bps=0.0,              # makers pay no taker fee
            total_cost_bps=slip_bps,        # no fee component for a maker
            levels_consumed=1,
            partial=filled_usd < notional,
            reason="maker fill",
        )

    def _resolve_exit_price(self, pos: _OpenPosition, now_s: int) -> Optional[float]:
        """Exit mid at now_s. If mid unavailable, try last known <= now_s."""
        cat = pos.category
        px = self.loader.get_mid_price(cat, pos.signal.condition_id, now_s, max_staleness_s=24 * 3600)
        if px is None:
            # last known before now
            trades = self.loader.get_trades(cat, as_of_s=now_s, condition_id=pos.signal.condition_id)
            if trades.empty:
                return None
            return float(trades["price"].iloc[-1])
        return px

    def _compute_exit_fill(self, pos: _OpenPosition, now_s: int) -> Optional[Fill]:
        cat = pos.category
        trades = self.loader.get_trades(cat, as_of_s=now_s, condition_id=pos.signal.condition_id,
                                        lookback_s=self.reconstructor.local_window_s)
        book = self.reconstructor.reconstruct(pos.signal.condition_id, now_s, trades)
        if book is None:
            return None
        # Reverse the side
        exit_side = "SELL" if pos.signal.side == SignalSide.BUY else "BUY"
        exit_usd = pos.entry_fill.filled_usd  # equal notional round-trip
        return self.executor.execute_market(book, exit_side, exit_usd)

    def _close_matured(
        self,
        positions: List[_OpenPosition],
        now_s: int,
        cat_index: Dict[str, str],
    ) -> Tuple[List[_OpenPosition], List[Trade]]:
        still_open: List[_OpenPosition] = []
        closed: List[Trade] = []
        for p in positions:
            met, reason = self._exit_condition_met(p, now_s, cat_index)
            if not met:
                still_open.append(p)
                continue
            fill = self._compute_exit_fill(p, now_s)
            if fill is None or not fill.filled:
                still_open.append(p)
                continue
            pnl = self._trade_pnl(p, fill)
            closed.append(Trade(
                strategy_name=p.signal.strategy_name,
                condition_id=p.signal.condition_id,
                category=p.category,
                entry_s=p.opened_at_s,
                exit_s=now_s,
                side=p.signal.side.value,
                requested_usd=p.signal.notional_usd,
                entry_filled_usd=p.entry_fill.filled_usd,
                entry_px=p.entry_fill.avg_fill_price,
                exit_px=fill.avg_fill_price,
                pnl_usd=pnl,
                entry_cost_bps=p.entry_fill.total_cost_bps,
                exit_cost_bps=fill.total_cost_bps,
                exit_reason=reason,
                capital_scale=self.config.capital_scale,
                liquidity_threshold=self.config.liquidity_threshold_usd,
                param_hash=str(self._params_hash(p.signal)),
                features=dict(p.signal.features),
            ))
        return still_open, closed

    def _close_at_horizon(
        self,
        positions: List[_OpenPosition],
        horizon_s: int,
        cat_index: Dict[str, str],
    ) -> Tuple[List[_OpenPosition], List[Trade]]:
        closed: List[Trade] = []
        for p in positions:
            fill = self._compute_exit_fill(p, horizon_s)
            if fill is None or not fill.filled:
                continue
            pnl = self._trade_pnl(p, fill)
            closed.append(Trade(
                strategy_name=p.signal.strategy_name,
                condition_id=p.signal.condition_id,
                category=p.category,
                entry_s=p.opened_at_s,
                exit_s=horizon_s,
                side=p.signal.side.value,
                requested_usd=p.signal.notional_usd,
                entry_filled_usd=p.entry_fill.filled_usd,
                entry_px=p.entry_fill.avg_fill_price,
                exit_px=fill.avg_fill_price,
                pnl_usd=pnl,
                entry_cost_bps=p.entry_fill.total_cost_bps,
                exit_cost_bps=fill.total_cost_bps,
                exit_reason="horizon",
                capital_scale=self.config.capital_scale,
                liquidity_threshold=self.config.liquidity_threshold_usd,
                param_hash=str(self._params_hash(p.signal)),
                features=dict(p.signal.features),
            ))
        return [], closed

    @staticmethod
    def _params_hash(sig: Signal) -> int:
        key = (sig.strategy_name, sig.side.value)
        return hash(key)

    @staticmethod
    def _trade_pnl(pos: _OpenPosition, exit_fill: Fill) -> float:
        entry_px = pos.entry_fill.avg_fill_price
        exit_px = exit_fill.avg_fill_price
        if entry_px <= 0 or exit_px <= 0:
            return 0.0
        shares = pos.entry_fill.filled_usd / entry_px
        if pos.signal.side == SignalSide.BUY:
            gross = shares * (exit_px - entry_px)
        else:
            gross = shares * (entry_px - exit_px)
        # Fees were already embedded in fill costs via book walk + taker fee.
        # Explicitly subtract notional * fee_bps both sides for safety.
        entry_fee = pos.entry_fill.filled_usd * (pos.entry_fill.taker_fee_bps / 10_000.0)
        exit_fee = exit_fill.filled_usd * (exit_fill.taker_fee_bps / 10_000.0)
        return float(gross - entry_fee - exit_fee)

    @staticmethod
    def _trades_to_df(trades: List[Trade]) -> pd.DataFrame:
        if not trades:
            return pd.DataFrame(columns=[
                "strategy_name", "condition_id", "category", "entry_s", "exit_s",
                "side", "requested_usd", "entry_filled_usd", "entry_px", "exit_px",
                "pnl_usd", "entry_cost_bps", "exit_cost_bps", "exit_reason",
                "capital_scale", "liquidity_threshold", "param_hash",
            ])
        rows = []
        for t in trades:
            row = {
                "strategy_name": t.strategy_name, "condition_id": t.condition_id,
                "category": t.category, "entry_s": t.entry_s, "exit_s": t.exit_s,
                "side": t.side, "requested_usd": t.requested_usd,
                "entry_filled_usd": t.entry_filled_usd, "entry_px": t.entry_px,
                "exit_px": t.exit_px, "pnl_usd": t.pnl_usd,
                "entry_cost_bps": t.entry_cost_bps, "exit_cost_bps": t.exit_cost_bps,
                "exit_reason": t.exit_reason, "capital_scale": t.capital_scale,
                "liquidity_threshold": t.liquidity_threshold, "param_hash": t.param_hash,
            }
            rows.append(row)
        return pd.DataFrame(rows)

    @staticmethod
    def _daily_pnl(df: pd.DataFrame) -> pd.Series:
        if df.empty:
            return pd.Series(dtype=float, name="daily_pnl")
        x = df.copy()
        x["date"] = pd.to_datetime(x["exit_s"], unit="s", utc=True).dt.date
        return x.groupby("date")["pnl_usd"].sum().astype(float).rename("daily_pnl")

    @staticmethod
    def _metrics(daily: pd.Series, trades: pd.DataFrame) -> Dict[str, float]:
        m = {
            "total_pnl_usd": float(daily.sum()) if not daily.empty else 0.0,
            "n_trades": int(len(trades)),
            "n_days_with_pnl": int(len(daily)),
            "mean_pnl_per_trade_usd": float(trades["pnl_usd"].mean()) if not trades.empty else 0.0,
            "hit_rate": float((trades["pnl_usd"] > 0).mean()) if not trades.empty else 0.0,
            "sharpe": _sharpe(daily),
            "sortino": _sortino(daily),
            "max_drawdown_usd": _max_drawdown(daily),
            "avg_entry_cost_bps": float(trades["entry_cost_bps"].mean()) if not trades.empty else 0.0,
            "avg_exit_cost_bps": float(trades["exit_cost_bps"].mean()) if not trades.empty else 0.0,
        }
        return m

    def _per_strategy_metrics(self, df: pd.DataFrame) -> Dict[str, Dict[str, float]]:
        out: Dict[str, Dict[str, float]] = {}
        if df.empty:
            return out
        for name, grp in df.groupby("strategy_name"):
            daily = self._daily_pnl(grp)
            out[name] = self._metrics(daily, grp)
        return out

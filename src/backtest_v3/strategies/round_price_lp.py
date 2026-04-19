"""
Round-price retail-flow liquidity provision (V3 §5.1).

Thesis: retail flow clusters at round prices (0.05, 0.10, ..., 0.50, ...).
A maker that posts one tick inside the round price captures most of that
flow without competing with professional MMs at true mid. In the backtest
we approximate maker fills by observing trade-tape clustering at round
prices and crediting fills at (round_price +/- tick) proportional to the
over-representation.

In a real deployment this runs as a quote-maintenance loop on the live
CLOB. Here, we simulate it by scanning trades at as_of and creating
"filled" positions that would have been earned by posting one tick inside
the round-price cluster in the prior minute.

Position sizing is capped by `max_inventory_usd` to bound adverse
selection.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from ..data.loader import PITDataLoader
from .base import Signal, SignalSide, StrategyParams, V3Strategy


_ROUND_TICKS = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50,
                0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95)


class RoundPriceLP(V3Strategy):
    name = "round_price_lp"

    default_params = StrategyParams(
        name=name,
        values={
            "tick_inside": 0.002,            # post 0.2¢ inside the round price
            "scan_window_s": 600,            # 10-min trade window
            "min_round_cluster_count": 10,   # require N prints near round
            "cluster_tolerance": 0.005,      # ±0.5¢ counts as "at round"
            "notional_usd": 200.0,           # base notional; scaled by conviction
            "size_multiplier_cap": 3.0,      # cluster-quality size bonus cap
            "max_inventory_usd": 2_000.0,
            "expected_hold_s": 30 * 60,      # exit within 30 min
            "price_staleness_s": 600,
            "maker_fill_window_s": 30 * 60,  # how long quote rests for fill
            # Risk controls (in bps of entry mid)
            "stop_loss_bps": 300,            # exit on 300 bps adverse move
            "trail_trigger_bps": 500,        # arm trail after +500 bps favorable
            "trail_giveback_bps": 200,       # exit if +500 drops back 200
        },
    )

    param_grid = {
        "tick_inside": [0.001, 0.002, 0.005],
        "scan_window_s": [300, 600, 1800],
        "min_round_cluster_count": [5, 10, 20],
        "notional_usd": [100.0, 200.0, 500.0],
    }

    def emit(self, as_of_s: int, universe_condition_ids: Iterable[str]) -> List[Signal]:
        p = self.params
        tick = p.get("tick_inside")
        window = p.get("scan_window_s")
        min_cluster = p.get("min_round_cluster_count")
        tol = p.get("cluster_tolerance")
        base_notional = p.get("notional_usd")
        size_cap = p.get("size_multiplier_cap", 3.0)
        hold = p.get("expected_hold_s")
        staleness = p.get("price_staleness_s")
        fill_window = p.get("maker_fill_window_s", hold)
        sl_bps = p.get("stop_loss_bps")
        trig_bps = p.get("trail_trigger_bps")
        give_bps = p.get("trail_giveback_bps")

        signals: List[Signal] = []
        universe = set(universe_condition_ids)
        if not universe:
            return signals

        for cat in self.loader.categories_available():
            trades_cat = self.loader.get_trades(cat, as_of_s=as_of_s, lookback_s=window)
            if trades_cat.empty:
                continue
            trades_cat = trades_cat[trades_cat["conditionId"].isin(universe)]
            if trades_cat.empty:
                continue
            for cid, grp in trades_cat.groupby("conditionId"):
                if len(grp) < min_cluster:
                    continue
                prices = grp["price"].to_numpy()
                mid = self.loader.get_mid_price(cat, cid, as_of_s, max_staleness_s=staleness)
                if mid is None:
                    continue
                for rp in _ROUND_TICKS:
                    if abs(mid - rp) > 0.05:
                        continue
                    cluster_count = int(((prices >= rp - tol) & (prices <= rp + tol)).sum())
                    if cluster_count < min_cluster:
                        continue
                    # Decide side: if mid < rp (buyers hitting round), post BUY
                    # one tick below rp; if mid > rp (sellers hitting), post
                    # SELL one tick above. Conservative: only emit one side
                    # per round cluster per scan to avoid double-counting.
                    buy_count = int(((grp["side"] == "BUY") & (prices.round(3) == round(rp, 3))).sum())
                    sell_count = int(((grp["side"] == "SELL") & (prices.round(3) == round(rp, 3))).sum())
                    if buy_count == 0 and sell_count == 0:
                        # trades aren't perfectly at round; use direction of mid deviation
                        dominant = "BUY" if mid < rp else "SELL"
                    else:
                        dominant = "BUY" if buy_count >= sell_count else "SELL"
                    side = SignalSide.BUY if dominant == "BUY" else SignalSide.SELL
                    limit_px = rp - tick if dominant == "BUY" else rp + tick
                    # Size by cluster quality — more prints = cleaner signal,
                    # capped so one outlier event can't blow up sizing.
                    quality = cluster_count / float(min_cluster)
                    size_mult = min(size_cap, max(1.0, quality))
                    conviction = float(min(1.0, quality / 3.0))
                    signals.append(Signal(
                        strategy_name=self.name,
                        condition_id=cid,
                        as_of_s=as_of_s,
                        available_at_s=as_of_s,
                        side=side,
                        notional_usd=base_notional * size_mult,
                        exit_price=float(limit_px + (tick if dominant == "BUY" else -tick)),
                        expected_hold_s=hold,
                        conviction=conviction,
                        reason=f"round-price LP at {rp:.2f} ({cluster_count} prints)",
                        features={
                            "round_price": rp, "cluster_count": cluster_count,
                            "limit_price": limit_px, "mid": mid,
                            "size_multiplier": size_mult,
                        },
                        limit_price=float(limit_px),
                        maker_fill_window_s=int(fill_window),
                        stop_loss_bps=float(sl_bps) if sl_bps is not None else None,
                        trail_trigger_bps=float(trig_bps) if trig_bps is not None else None,
                        trail_giveback_bps=float(give_bps) if give_bps is not None else None,
                    ))
        return signals

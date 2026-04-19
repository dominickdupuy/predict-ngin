"""
Book-walking executor.

Takes a CLOB snapshot (real or reconstructed) and fills orders against it
level by level. This is qualitatively different from a parametric
sqrt-impact formula:

- Small orders fill at-or-near best, realistic micro fills.
- Large orders consume multiple levels — each unit is priced at the marginal
  level, not the average depth. That's where large-order slippage really
  lives.
- Orders that exhaust the book come back with `partial=True` and
  `filled_usd < requested_usd`. Strategies must handle partial fills.

This models the same physics as the existing `CLOBSimulator` from
`src/trading/execution/clob_simulator.py`, but without the aggregate
closed-form formula. For small sizes on liquid books the two agree; for
large sizes or thin books, the book walk is strictly more realistic.

Fees / rebates
--------------
- Taker fee: 20 bps of notional (Polymarket published).
- Maker fee: 0 (Polymarket maker-rebate program status varies).
- Post-only orders earn 0 spread in the conservative model — we do not
  assume rebates unless the caller passes `maker_rebate_bps > 0`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional

from ..data.clob_book import BookSnapshot, BookLevel


Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class Fill:
    """Result of a single execution attempt."""
    filled: bool
    side: Side
    requested_usd: float
    filled_usd: float
    avg_fill_price: float
    mid_before: float
    slippage_bps: float         # avg_fill vs mid, signed in cost direction
    taker_fee_bps: float
    total_cost_bps: float
    levels_consumed: int
    partial: bool
    reason: str = ""

    @property
    def filled_shares(self) -> float:
        """How many contract units we bought/sold."""
        if self.avg_fill_price <= 0:
            return 0.0
        return self.filled_usd / self.avg_fill_price


@dataclass
class ExecutorConfig:
    taker_fee_bps: float = 0.0          # Polymarket currently charges 0; see COST_MODEL_AUDIT
    maker_rebate_bps: float = 0.0
    # Optional volume-per-level cap as fraction of book-level size; some CLOBs
    # have phantom depth. Conservatively fill only this fraction of each level.
    level_fill_fraction: float = 0.60
    # Refuse to take more than this fraction of total book depth in a single order
    max_depth_fraction: float = 0.50


class BookExecutor:
    """
    Walks a BookSnapshot and returns a Fill.

    Stateless. The caller is responsible for updating the book between calls
    (usually by re-requesting a new snapshot at a later as_of).
    """

    def __init__(self, config: Optional[ExecutorConfig] = None):
        self.config = config or ExecutorConfig()

    def execute_market(
        self,
        book: BookSnapshot,
        side: Side,
        size_usd: float,
    ) -> Fill:
        """
        Marketable order that crosses the book.

        BUY consumes asks (pays up). SELL consumes bids (sells down).
        """
        if size_usd <= 0:
            return Fill(
                filled=False, side=side, requested_usd=0.0, filled_usd=0.0,
                avg_fill_price=0.0, mid_before=book.mid, slippage_bps=0.0,
                taker_fee_bps=0.0, total_cost_bps=0.0,
                levels_consumed=0, partial=True, reason="non-positive size",
            )

        levels: List[BookLevel] = book.asks if side == "BUY" else book.bids
        if not levels:
            return Fill(
                filled=False, side=side, requested_usd=size_usd, filled_usd=0.0,
                avg_fill_price=0.0, mid_before=book.mid, slippage_bps=0.0,
                taker_fee_bps=0.0, total_cost_bps=0.0,
                levels_consumed=0, partial=True, reason="empty side",
            )

        # Enforce max_depth_fraction of total book depth
        total_depth = sum(lv.size_usd for lv in levels) * self.config.level_fill_fraction
        cap_usd = total_depth * self.config.max_depth_fraction
        target_usd = min(size_usd, cap_usd)

        filled_usd = 0.0
        weighted_px_num = 0.0
        consumed = 0
        for lv in levels:
            takeable = lv.size_usd * self.config.level_fill_fraction
            remaining = target_usd - filled_usd
            if remaining <= 0:
                break
            take = min(takeable, remaining)
            filled_usd += take
            weighted_px_num += take * lv.price
            consumed += 1
            if filled_usd >= target_usd - 1e-9:
                break

        if filled_usd <= 0:
            return Fill(
                filled=False, side=side, requested_usd=size_usd, filled_usd=0.0,
                avg_fill_price=0.0, mid_before=book.mid, slippage_bps=0.0,
                taker_fee_bps=0.0, total_cost_bps=0.0,
                levels_consumed=0, partial=True, reason="no fill",
            )

        avg_px = weighted_px_num / filled_usd
        # Slippage in BPS, always signed as a cost (positive = cost)
        if side == "BUY":
            slip_bps = (avg_px - book.mid) / book.mid * 10_000 if book.mid > 0 else 0.0
        else:
            slip_bps = (book.mid - avg_px) / book.mid * 10_000 if book.mid > 0 else 0.0
        fee_bps = self.config.taker_fee_bps
        total_bps = slip_bps + fee_bps
        partial = filled_usd < size_usd - 1e-9

        return Fill(
            filled=True,
            side=side,
            requested_usd=size_usd,
            filled_usd=filled_usd,
            avg_fill_price=avg_px,
            mid_before=book.mid,
            slippage_bps=slip_bps,
            taker_fee_bps=fee_bps,
            total_cost_bps=total_bps,
            levels_consumed=consumed,
            partial=partial,
            reason="partial: exhausted cap" if partial else "full fill",
        )

    def execute_limit_at_or_better(
        self,
        book: BookSnapshot,
        side: Side,
        size_usd: float,
        limit_price: float,
    ) -> Fill:
        """
        Aggressive limit: cross only levels at-or-better than `limit_price`.
        """
        levels: List[BookLevel] = book.asks if side == "BUY" else book.bids
        if not levels:
            return Fill(
                filled=False, side=side, requested_usd=size_usd, filled_usd=0.0,
                avg_fill_price=0.0, mid_before=book.mid, slippage_bps=0.0,
                taker_fee_bps=0.0, total_cost_bps=0.0,
                levels_consumed=0, partial=True, reason="empty side",
            )

        filled_usd = 0.0
        weighted_px_num = 0.0
        consumed = 0
        for lv in levels:
            accept = (lv.price <= limit_price) if side == "BUY" else (lv.price >= limit_price)
            if not accept:
                break
            takeable = lv.size_usd * self.config.level_fill_fraction
            remaining = size_usd - filled_usd
            if remaining <= 0:
                break
            take = min(takeable, remaining)
            filled_usd += take
            weighted_px_num += take * lv.price
            consumed += 1

        if filled_usd <= 0:
            return Fill(
                filled=False, side=side, requested_usd=size_usd, filled_usd=0.0,
                avg_fill_price=0.0, mid_before=book.mid, slippage_bps=0.0,
                taker_fee_bps=0.0, total_cost_bps=0.0,
                levels_consumed=0, partial=True, reason="no levels inside limit",
            )

        avg_px = weighted_px_num / filled_usd
        if side == "BUY":
            slip_bps = (avg_px - book.mid) / book.mid * 10_000 if book.mid > 0 else 0.0
        else:
            slip_bps = (book.mid - avg_px) / book.mid * 10_000 if book.mid > 0 else 0.0
        fee_bps = self.config.taker_fee_bps
        total_bps = slip_bps + fee_bps
        partial = filled_usd < size_usd - 1e-9
        return Fill(
            filled=True, side=side, requested_usd=size_usd,
            filled_usd=filled_usd, avg_fill_price=avg_px, mid_before=book.mid,
            slippage_bps=slip_bps, taker_fee_bps=fee_bps, total_cost_bps=total_bps,
            levels_consumed=consumed, partial=partial,
            reason="partial: limit" if partial else "full fill",
        )

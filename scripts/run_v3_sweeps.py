"""
Run the V3 backtest sweeps end-to-end on real Polymarket data.

Produces three reports into docs/:
  V3_PARAMETER_SENSITIVITY.md — full grid for CalendarButterfly + deflated SR
  V3_LIQUIDITY_SWEEP.md       — metrics across liquidity thresholds
  V3_CAPACITY_CURVE.md        — Sharpe vs capital_scale, capacity wall

Scope is deliberately narrow (Politics category, 14-day window) so the full
sweep completes in a couple of minutes. Widen via the CLI knobs below when
you want a longer or broader run.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backtest_v3.backtest.engine import EngineConfig
from backtest_v3.backtest.liquidity_sweep import LiquiditySweep
from backtest_v3.backtest.sensitivity import ParameterSweep
from backtest_v3.data.loader import PITDataLoader
from backtest_v3.reporting.capacity_curve import CapacityCurve
from backtest_v3.strategies.base import StrategyParams
from backtest_v3.strategies.calendar_butterfly import CalendarButterfly
from backtest_v3.strategies.round_price_lp import RoundPriceLP


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-09-01")
    ap.add_argument("--end", default="2026-01-31")
    ap.add_argument("--category", default="Politics")
    ap.add_argument("--step-hours", type=int, default=12)
    ap.add_argument("--data-root", default=str(ROOT / "data" / "research"))
    ap.add_argument("--out-dir", default=str(ROOT / "docs"))
    return ap.parse_args()


def _base_config(args, liquidity_threshold_usd: float = 300_000.0) -> EngineConfig:
    return EngineConfig(
        start_s=int(pd.Timestamp(args.start, tz="UTC").timestamp()),
        end_s=int(pd.Timestamp(args.end, tz="UTC").timestamp()),
        step_s=args.step_hours * 3600,
        liquidity_threshold_usd=liquidity_threshold_usd,
        liquidity_lookback_s=30 * 24 * 3600,
        capital_scale=1.0,
        label="v3_sweep",
    )


def _shrink_grid() -> None:
    """
    Replace the full strategy grids with compact versions that finish quickly.
    Two knobs per strategy is enough to detect sensitivity without paying for
    the full Cartesian product at runtime.
    """
    # Calendar butterfly rarely fires in narrow windows — keep a tight grid.
    CalendarButterfly.param_grid = {
        "min_butterfly_bps": [50, 100, 200],
        "notional_usd_per_leg": [250.0, 500.0],
    }
    # Round-price LP: loosen cluster threshold and widen the scan window so
    # the strategy fires against Polymarket's realistic (sparse) trade density.
    RoundPriceLP.param_grid = {
        "min_round_cluster_count": [2, 3, 5],
        "scan_window_s": [3600, 14400],
        "notional_usd": [100.0, 250.0],
    }
    # Also relax the round-price LP defaults used by the liquidity / capacity
    # sweeps (which don't go through ParameterSweep).
    RoundPriceLP.default_params = RoundPriceLP.default_params.merge({
        "min_round_cluster_count": 3,
        "scan_window_s": 14400,
        "notional_usd": 250.0,
    })


def run_parameter_sensitivity(loader: PITDataLoader, base_cfg: EngineConfig,
                               out_dir: Path) -> None:
    # Use RoundPriceLP: calendar butterfly rarely finds 3 co-event markets
    # in the horizons we can afford to backtest.
    print(f"\n[1/3] Parameter sensitivity: RoundPriceLP")
    sweep = ParameterSweep(
        loader=loader,
        strategy_cls=RoundPriceLP,
        base_config=base_cfg,
        strategy_factory=lambda l, p: RoundPriceLP(l, params=p),
    )
    t0 = time.time()
    result = sweep.run()
    dt = time.time() - t0
    print(f"  done in {dt:.1f}s — {len(result.rows)} trials")

    # Write report
    table = result.table.sort_values("sharpe", ascending=False)
    body = ["# V3 Parameter Sensitivity -- RoundPriceLP", ""]
    body.append(f"- **Window**: {pd.Timestamp(base_cfg.start_s, unit='s', tz='UTC').date()} "
                f"→ {pd.Timestamp(base_cfg.end_s, unit='s', tz='UTC').date()}")
    body.append(f"- **Liquidity threshold**: ${base_cfg.liquidity_threshold_usd:,.0f}")
    body.append(f"- **Decision step**: {base_cfg.step_s // 3600}h")
    body.append(f"- **Trials**: {len(result.rows)}")
    body.append(f"- **Wall time**: {dt:.1f}s")
    body.append("")
    if result.best_row is not None:
        body.append("## Best config")
        body.append(f"- **params**: `{result.best_row.params}`")
        body.append(f"- **sharpe**: {result.best_row.metrics.get('sharpe', 0):.3f}")
        body.append(f"- **n_trades**: {int(result.best_row.metrics.get('n_trades', 0))}")
        body.append(f"- **total_pnl_usd**: {result.best_row.metrics.get('total_pnl_usd', 0):.2f}")
        body.append("")
    if result.deflated:
        body.append("## Deflated Sharpe (Bailey-López de Prado 2014)")
        for k, v in result.deflated.items():
            body.append(f"- **{k}**: {v:.4f}")
        body.append("")
        dsr = result.deflated.get("dsr", 0.0)
        verdict = ("Strong — unlikely to be a p-hacking artefact." if dsr > 0.95
                   else "Marginal — cannot reject null at 95%." if dsr > 0.5
                   else "Weak — indistinguishable from multi-testing noise.")
        body.append(f"**Verdict**: {verdict}")
        body.append("")
    body.append("## Full trial table")
    body.append("")
    body.append(table.round(4).to_markdown(index=False))
    body.append("")

    (out_dir / "V3_PARAMETER_SENSITIVITY.md").write_text("\n".join(body), encoding="utf-8")
    table.to_csv(out_dir / "V3_PARAMETER_SENSITIVITY.csv", index=False)


def run_liquidity_sweep(loader: PITDataLoader, base_cfg: EngineConfig,
                        out_dir: Path) -> None:
    print(f"\n[2/3] Liquidity sweep")
    thresholds = [100_000.0, 300_000.0, 500_000.0, 1_000_000.0]
    sweep = LiquiditySweep(
        loader=loader,
        strategy_factory=lambda l: [CalendarButterfly(l), RoundPriceLP(l)],
        base_config=base_cfg,
        thresholds_usd=thresholds,
    )
    t0 = time.time()
    result = sweep.run()
    dt = time.time() - t0
    print(f"  done in {dt:.1f}s — {len(thresholds)} thresholds")

    body = ["# V3 Liquidity Sweep", ""]
    body.append(f"- **Window**: {pd.Timestamp(base_cfg.start_s, unit='s', tz='UTC').date()} "
                f"→ {pd.Timestamp(base_cfg.end_s, unit='s', tz='UTC').date()}")
    body.append(f"- **Strategies**: CalendarButterfly + RoundPriceLP")
    body.append(f"- **Wall time**: {dt:.1f}s")
    body.append("")
    body.append(result.table.round(4).to_markdown(index=False))
    body.append("")
    (out_dir / "V3_LIQUIDITY_SWEEP.md").write_text("\n".join(body), encoding="utf-8")
    result.table.to_csv(out_dir / "V3_LIQUIDITY_SWEEP.csv", index=False)


def run_capacity_curve(loader: PITDataLoader, base_cfg: EngineConfig,
                       out_dir: Path) -> None:
    print(f"\n[3/3] Capacity curve")
    scales = [0.25, 0.5, 1.0, 2.0, 4.0]
    curve = CapacityCurve(
        loader=loader,
        strategy_factory=lambda l: [CalendarButterfly(l), RoundPriceLP(l)],
        base_config=base_cfg,
        scales=scales,
    )
    t0 = time.time()
    result = curve.run()
    dt = time.time() - t0
    wall = result.capacity_wall_scale
    print(f"  done in {dt:.1f}s — {len(scales)} scales"
          f"{'' if wall is None else f' | capacity wall @ {wall}×'}")

    body = ["# V3 Capacity Curve", ""]
    body.append(f"- **Window**: {pd.Timestamp(base_cfg.start_s, unit='s', tz='UTC').date()} "
                f"→ {pd.Timestamp(base_cfg.end_s, unit='s', tz='UTC').date()}")
    body.append(f"- **Capacity wall**: "
                f"{f'{wall:.2f}×' if wall is not None else 'not reached in ladder'}")
    body.append(f"- **Wall time**: {dt:.1f}s")
    body.append("")
    body.append(result.table.round(4).to_markdown(index=False))
    body.append("")
    (out_dir / "V3_CAPACITY_CURVE.md").write_text("\n".join(body), encoding="utf-8")
    result.table.to_csv(out_dir / "V3_CAPACITY_CURVE.csv", index=False)


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    data_root = Path(args.data_root)
    if not (data_root / args.category / "trades.parquet").exists():
        print(f"ERROR: no trades.parquet at {data_root / args.category}", file=sys.stderr)
        return 1

    _shrink_grid()
    loader = PITDataLoader(data_root, categories=(args.category,))
    base_cfg = _base_config(args)

    print(f"V3 sweeps — category={args.category} "
          f"window={args.start}..{args.end} step={args.step_hours}h")
    print(f"output -> {out_dir}")

    run_parameter_sensitivity(loader, base_cfg, out_dir)
    run_liquidity_sweep(loader, base_cfg, out_dir)
    run_capacity_curve(loader, base_cfg, out_dir)
    print("\nAll reports written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

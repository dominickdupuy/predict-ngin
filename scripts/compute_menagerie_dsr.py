"""
Compute the deflated Sharpe across the whole V3 strategy menagerie.

Each (strategy x threshold) row in docs/V3_STRATEGY_THRESHOLDS.csv counts as
one trial. Bailey–López de Prado's DSR asks: given the best-observed Sharpe
across N trials and the variance of Sharpes between them, what is the
probability that the selected strategy's true Sharpe exceeds zero?

This is more honest than per-strategy DSR because the decision to paper
trade is made across the whole menagerie — so multi-testing correction
must account for *every* trial considered, not just the one the selected
strategy ran.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backtest_v3.backtest.sensitivity import deflated_sharpe


def main() -> int:
    thr_csv = ROOT / "docs" / "V3_STRATEGY_THRESHOLDS.csv"
    if not thr_csv.exists():
        print(f"ERROR: run scripts/rank_v3_strategies.py first to produce {thr_csv}")
        return 1

    df = pd.read_csv(thr_csv)
    print(f"Loaded {len(df)} trial rows from {thr_csv.name}")

    # Treat only firing configs as valid trials; zero-trade configs have SR=0
    # by definition and including them deflates artificially.
    fired = df[df["n_trades"] > 0].copy()
    sr = fired["sharpe"].dropna()
    if len(sr) < 2:
        print(f"Only {len(sr)} firing trials — DSR not meaningful.")
        return 0

    n_trials_total = len(df)
    n_trials_firing = len(sr)
    best_sr = float(sr.max())
    sr_var = float(sr.var(ddof=1)) if len(sr) > 1 else 0.0

    # Pull the best row for return-moment inputs
    best_row = fired.loc[sr.idxmax()]
    n_trades = int(best_row["n_trades"])
    # Approx: 1 return per trade (closed-trade PnL series).
    # skew/kurtosis: use sample moments from the winning row if available,
    # otherwise assume normal.
    result_all = deflated_sharpe(
        observed_sharpe=best_sr,
        n_trials=n_trials_total,
        sr_variance=sr_var,
        n_returns=n_trades,
        skew=0.0,
        kurtosis=3.0,
    )
    result_firing = deflated_sharpe(
        observed_sharpe=best_sr,
        n_trials=n_trials_firing,
        sr_variance=sr_var,
        n_returns=n_trades,
        skew=0.0,
        kurtosis=3.0,
    )

    body = ["# V3 Menagerie Deflated Sharpe", ""]
    body.append(f"**Source:** `{thr_csv.relative_to(ROOT)}`  ({len(df)} rows)")
    body.append(f"**Firing trials:** {n_trials_firing} of {n_trials_total}")
    body.append("")
    body.append("## Inputs")
    body.append(f"- Best observed Sharpe: **{best_sr:.3f}** "
                f"(strategy=`{best_row['strategy']}`, threshold=${int(best_row['threshold_usd']):,})")
    body.append(f"- Trades in best config: {n_trades}")
    body.append(f"- Sharpe variance across trials: {sr_var:.4f}")
    body.append("")
    body.append("## Deflated Sharpe")
    body.append("")
    body.append("| trial_pool | n_trials | cutoff_SR | z | DSR |")
    body.append("|---|---|---|---|---|")
    body.append(f"| all trials (incl. zero-firing) | {n_trials_total} | "
                f"{result_all['cutoff_sr']:.3f} | {result_all['z']:.3f} | "
                f"**{result_all['dsr']:.4f}** |")
    body.append(f"| firing trials only | {n_trials_firing} | "
                f"{result_firing['cutoff_sr']:.3f} | {result_firing['z']:.3f} | "
                f"**{result_firing['dsr']:.4f}** |")
    body.append("")
    body.append("## Verdict")
    body.append("")
    dsr = result_firing["dsr"]
    if dsr > 0.95:
        verdict = "Strong — best Sharpe survives multi-testing correction."
    elif dsr > 0.5:
        verdict = "Marginal — cannot reject null at 95% across the menagerie."
    else:
        verdict = "Weak — menagerie-level DSR says the winner is consistent with p-hacking."
    body.append(f"**{verdict}**")
    body.append("")
    body.append("## Per-strategy Sharpes")
    body.append("")
    body.append(fired[["strategy", "threshold_usd", "n_trades", "sharpe", "total_pnl_usd"]]
                .round(4).to_markdown(index=False))
    body.append("")

    out = ROOT / "docs" / "V3_MENAGERIE_DSR.md"
    out.write_text("\n".join(body), encoding="utf-8")

    print(f"\nDSR (firing trials, n={n_trials_firing}): {result_firing['dsr']:.4f}")
    print(f"DSR (all trials,     n={n_trials_total}):  {result_all['dsr']:.4f}")
    print(f"Cutoff SR (firing): {result_firing['cutoff_sr']:.3f}")
    print(f"Best observed SR:   {best_sr:.3f}")
    print(f"Wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

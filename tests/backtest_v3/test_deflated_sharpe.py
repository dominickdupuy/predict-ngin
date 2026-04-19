"""
Unit tests for deflated Sharpe + parameter sweep helpers.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from backtest_v3.backtest.sensitivity import (
    _hash_params, _moments, _phi, _phi_inv, _sr_cutoff, deflated_sharpe,
)


def test_phi_inv_roundtrip() -> None:
    for p in [0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99]:
        assert _phi(_phi_inv(p)) == pytest.approx(p, abs=1e-6)


def test_phi_symmetry() -> None:
    assert _phi(0.0) == pytest.approx(0.5, abs=1e-9)
    assert _phi(-1.0) + _phi(1.0) == pytest.approx(1.0, abs=1e-9)


def test_sr_cutoff_monotone_in_trials() -> None:
    """More trials → harder to beat the null."""
    c10 = _sr_cutoff(10, 0.5)
    c100 = _sr_cutoff(100, 0.5)
    c1000 = _sr_cutoff(1000, 0.5)
    assert c10 < c100 < c1000


def test_sr_cutoff_zero_for_single_trial() -> None:
    assert _sr_cutoff(1, 0.5) == 0.0


def test_deflated_sharpe_null_case() -> None:
    """SR=0 with many trials → DSR ~ 0 (can't reject null)."""
    d = deflated_sharpe(observed_sharpe=0.0, n_trials=20, sr_variance=0.5, n_returns=252)
    assert d["dsr"] < 0.01


def test_deflated_sharpe_strong_signal() -> None:
    """High SR with low variance across trials → DSR ~ 1."""
    d = deflated_sharpe(observed_sharpe=3.0, n_trials=5, sr_variance=0.1, n_returns=252)
    assert d["dsr"] > 0.95


def test_deflated_sharpe_corrects_for_trials() -> None:
    """Same observed SR but more trials → lower DSR."""
    d_few = deflated_sharpe(observed_sharpe=1.5, n_trials=5, sr_variance=0.5, n_returns=252)
    d_many = deflated_sharpe(observed_sharpe=1.5, n_trials=500, sr_variance=0.5, n_returns=252)
    assert d_few["dsr"] > d_many["dsr"]
    assert d_few["cutoff_sr"] < d_many["cutoff_sr"]


def test_hash_params_deterministic() -> None:
    a = _hash_params({"x": 1, "y": 2})
    b = _hash_params({"y": 2, "x": 1})
    assert a == b
    assert _hash_params({"x": 1, "y": 3}) != a


def test_moments_on_normal_ish_series() -> None:
    import numpy as np
    rng = np.random.default_rng(42)
    x = pd.Series(rng.standard_normal(5000))
    skew, kurt = _moments(x)
    assert abs(skew) < 0.2
    assert 2.5 < kurt < 3.5


def test_moments_empty_defaults() -> None:
    skew, kurt = _moments(pd.Series(dtype=float))
    assert skew == 0.0 and kurt == 3.0

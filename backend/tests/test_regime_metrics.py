"""T1-D — sub-period / regime stability metrics.

Detects a Sharpe propped up by a single lucky window, tolerates the flat-filled
equity grid (skips unevaluable windows rather than scoring them 0), and uses the
engine's 8760-hour annualisation so sub-period Sharpes are comparable.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

import backend.core.regime_metrics as RM


def _per_bar(returns):
    return [{"pnl_pct": float(x)} for x in returns]


def test_flat_series_yields_no_regime_keys():
    # 200 fully-flat bars: every window std≈0 → all skipped → no keys.
    assert RM.compute_subperiod_stability([0.0] * 200, n_windows=6) == {}


def test_single_window_propped_sharpe():
    rng = np.random.default_rng(0)
    w0 = 0.01 + rng.normal(0.0, 0.002, 60)       # strongly positive window
    rest = -0.001 + rng.normal(0.0, 0.01, 300)   # 5 negative-ish windows
    returns = np.concatenate([w0, rest])         # 360 bars / 6 windows
    out = RM.compute_subperiod_stability(returns, n_windows=6)
    assert out["regime_n_windows"] == 6
    # Only ~1 of 6 windows is positive — the prop is detectable.
    assert out["regime_positive_fraction"] <= 0.34
    assert out["regime_worst_sharpe"] < 0.0
    assert out["regime_best_sharpe"] > 0.0


def test_uniformly_good_series_passes():
    returns = [0.01, -0.004] * 200  # net-positive, stable across windows
    out = RM.compute_subperiod_stability(returns, n_windows=6)
    assert out["regime_positive_fraction"] == 1.0
    assert out["regime_worst_sharpe"] > 0.0


def test_annualization_matches_engine():
    rng = np.random.default_rng(1)
    r = 0.001 + rng.normal(0.0, 0.01, 100)
    got = RM._subperiod_sharpe(np.asarray(r, dtype=float))
    expected = (float(np.mean(r)) / float(np.std(r, ddof=1))) * math.sqrt(24 * 365)
    assert got == pytest.approx(expected, rel=1e-9)


def test_too_few_bars_returns_empty():
    assert RM.compute_subperiod_stability([0.01, -0.005] * 25, n_windows=6) == {}  # 50 < 90


def test_equal_split_indices_covers_all():
    idx = RM.equal_split_indices(100, 6)
    assert idx[0][0] == 0 and idx[-1][1] == 100
    # contiguous, no gaps/overlap
    for (a0, a1), (b0, b1) in zip(idx, idx[1:]):
        assert a1 == b0
    sizes = [b - a for a, b in idx]
    assert sum(sizes) == 100
    # remainder (100 % 6 = 4) spread to the leading windows
    assert sizes[:4] == [17, 17, 17, 17] and sizes[4:] == [16, 16]


def test_vol_regime_split_tolerates_flat_grid():
    rng = np.random.default_rng(2)
    sparse = np.zeros(1000)
    idxs = rng.choice(1000, size=120, replace=False)
    sparse[idxs] = rng.normal(0.0, 0.02, 120)
    out = RM.compute_vol_regime_stability(sparse)  # must not raise
    assert isinstance(out, dict)


def test_compute_from_per_bar_all_json_clean():
    rng = np.random.default_rng(3)
    returns = 0.0005 + rng.normal(0.0, 0.008, 400)
    out = RM.compute_from_per_bar(_per_bar(returns), n_windows=6)
    assert "regime_positive_fraction" in out
    for k, v in out.items():
        assert isinstance(v, (int, float))
        assert math.isfinite(float(v)), f"{k} not finite"


def test_compute_from_per_bar_empty():
    assert RM.compute_from_per_bar(None) == {}
    assert RM.compute_from_per_bar([]) == {}
    assert RM.compute_from_per_bar(_per_bar([0.0] * 50)) == {}  # too short

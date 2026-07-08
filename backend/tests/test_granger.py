"""Unit tests for backend/core/granger.py (finding id: 110).

Covers:
  - subnormal-std guard (_safe_std returns 0.0 for subnormal / near-constant input)
  - _align_pair forward-fill behaviour and non-overlapping range handling
  - distinct-value gate (< _MIN_DISTINCT unique values -> None)
  - MIN_OBSERVATIONS gate (too-few aligned points -> None)
  - constant series gate (std ~ 0 -> None)
  - valid AR(1)-like pair produces dict with p_value in [0, 1]
  - p_value clamped to [0, 1] (D-H4/P16 guard)
  - is_enabled() returns False when GRANGER_ENABLED is unset
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import List, Tuple
from unittest.mock import patch

import numpy as np
import pytest

# conftest.py already sets up sys.path; import production module directly.
from backend.core.granger import (
    MIN_OBSERVATIONS,
    _align_pair,
    _safe_std,
    compute_granger_for_pair,
    is_enabled,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts_series(
    values: List[float],
    *,
    start: str = "2023-01-01",
    freq_hours: int = 24,
) -> List[Tuple[datetime, float]]:
    """Build a List[Tuple[datetime, float]] with one point per freq_hours."""
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    if start != "2023-01-01":
        base = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    delta = timedelta(hours=freq_hours)
    return [(base + i * delta, float(v)) for i, v in enumerate(values)]


def _ar1_series(
    length: int,
    phi: float = 0.7,
    seed: int = 42,
    start: str = "2023-01-01",
) -> List[Tuple[datetime, float]]:
    """AR(1) process with memory — many distinct values, non-constant."""
    rng = np.random.default_rng(seed)
    x = np.zeros(length)
    x[0] = rng.standard_normal()
    for i in range(1, length):
        x[i] = phi * x[i - 1] + rng.standard_normal()
    return _ts_series(x.tolist(), start=start)


def _make_series(length: int, seed: int = 0, start: str = "2023-01-01") -> List[Tuple[datetime, float]]:
    """Random normal values — sufficient distinct count for non-trivial length."""
    rng = np.random.default_rng(seed)
    vals = rng.standard_normal(length).tolist()
    return _ts_series(vals, start=start)


# ---------------------------------------------------------------------------
# 1. _safe_std: subnormal input returns 0.0
# ---------------------------------------------------------------------------

def test_safe_std_subnormal_returns_zero():
    """5e-324 is the IEEE 754 minimum subnormal; std of a constant subnormal
    array must trigger the `not (s > 1e-14)` guard and return 0.0."""
    arr = np.full(10, 5e-324, dtype=float)
    result = _safe_std(arr)
    assert result == 0.0


def test_safe_std_constant_array_returns_zero():
    """Constant array has std = 0.0 exactly, must return 0.0."""
    arr = np.ones(20)
    result = _safe_std(arr)
    assert result == 0.0


def test_safe_std_normal_array_positive():
    """Random-normal array has meaningful std > 1e-14."""
    rng = np.random.default_rng(0)
    arr = rng.standard_normal(50)
    result = _safe_std(arr)
    assert isinstance(result, float)
    assert result > 1e-14


# ---------------------------------------------------------------------------
# 2. _align_pair: non-overlapping date ranges -> empty arrays
# ---------------------------------------------------------------------------

def test_align_pair_non_overlapping_returns_empty():
    """Series A ends 2023-01-10; series B starts 2024-01-01 -> zero overlap."""
    a = _make_series(10, start="2023-01-01")  # ends 2023-01-10
    b = _make_series(10, start="2024-01-01")  # starts 2024-01-01
    arr_a, arr_b = _align_pair(a, b)
    assert len(arr_a) == 0
    assert len(arr_b) == 0


def test_align_pair_empty_input_returns_empty():
    """Empty series A -> early exit, empty arrays returned."""
    a: List[Tuple[datetime, float]] = []
    b = _make_series(5)
    arr_a, arr_b = _align_pair(a, b)
    assert len(arr_a) == 0
    assert len(arr_b) == 0


# ---------------------------------------------------------------------------
# 3. _align_pair: forward-fill fills the bucketed grid
# ---------------------------------------------------------------------------

def test_align_pair_forward_fill_no_nans():
    """Sparse series with only 3 non-NaN observations, forward-filled across 30 days.
    After _align_pair, output arrays must be fully populated (no NaN)."""
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    # 30-day series for series B (dense)
    b_vals = list(np.random.default_rng(1).standard_normal(30))
    b = [(base + timedelta(days=i), b_vals[i]) for i in range(30)]

    # Series A: only 3 observations spread across the 30-day window
    a = [
        (base, 1.0),
        (base + timedelta(days=10), 2.0),
        (base + timedelta(days=20), 3.0),
    ]
    arr_a, arr_b = _align_pair(a, b)
    # The grid spans from max(start_a, start_b)=base to min(end_a, end_b)=base+20d
    # Forward-fill means the first bucket uses a[0][1]=1.0; no NaN ever.
    assert len(arr_a) > 0
    assert len(arr_b) > 0
    assert not np.any(np.isnan(arr_a)), "forward-fill left NaN in arr_a"
    assert not np.any(np.isnan(arr_b)), "forward-fill left NaN in arr_b"


def test_align_pair_forward_fill_values_correct():
    """Verify that forward-fill propagates the last known value."""
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    # a: observation at day 0 (val=5.0) and day 2 (val=9.0)
    a = [
        (base, 5.0),
        (base + timedelta(days=2), 9.0),
    ]
    # b: daily observations for 5 days (all 1.0)
    b = [(base + timedelta(days=i), 1.0) for i in range(5)]

    arr_a, arr_b = _align_pair(a, b)
    # Grid: base, base+1d, base+2d (overlap is [base, base+2d])
    # Bucket 0 (base):       a obs at base -> 5.0
    # Bucket 1 (base+1d):    no new obs -> forward-fill 5.0
    # Bucket 2 (base+2d):    obs at base+2d -> 9.0
    assert len(arr_a) == 3
    assert arr_a[0] == pytest.approx(5.0)
    assert arr_a[1] == pytest.approx(5.0)
    assert arr_a[2] == pytest.approx(9.0)


# ---------------------------------------------------------------------------
# 4. compute_granger_for_pair: too few observations -> None
# ---------------------------------------------------------------------------

def test_compute_granger_too_few_samples_returns_none():
    """10 points < MIN_OBSERVATIONS (20) -> must return None."""
    s1 = _make_series(10, seed=0)
    s2 = _make_series(10, seed=1)
    result = compute_granger_for_pair(s1, s2)
    assert result is None


def test_min_observations_boundary():
    """MIN_OBSERVATIONS-1 aligned points must still return None."""
    n = MIN_OBSERVATIONS - 1
    s1 = _ar1_series(n, seed=10)
    s2 = _ar1_series(n, seed=20)
    result = compute_granger_for_pair(s1, s2)
    assert result is None


# ---------------------------------------------------------------------------
# 5. compute_granger_for_pair: constant series (std ~ 0) -> None
# ---------------------------------------------------------------------------

def test_compute_granger_constant_series_returns_none():
    """A fully constant series has std = 0 -> subnormal guard returns None."""
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    constant = [(base + timedelta(days=i), 1.0) for i in range(30)]
    varied = _ar1_series(30, seed=5)
    result = compute_granger_for_pair(constant, varied)
    assert result is None


def test_compute_granger_both_constant_returns_none():
    """Both series constant -> either std guard fires, returns None."""
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    c1 = [(base + timedelta(days=i), 2.5) for i in range(30)]
    c2 = [(base + timedelta(days=i), 7.0) for i in range(30)]
    result = compute_granger_for_pair(c1, c2)
    assert result is None


# ---------------------------------------------------------------------------
# 6. compute_granger_for_pair: fewer than _MIN_DISTINCT=5 unique values -> None
# ---------------------------------------------------------------------------

def test_compute_granger_few_distinct_values_returns_none():
    """Series with only 3 distinct values after forward-fill must be rejected."""
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    # 3 distinct values spread over 30 days -> forward-filled to give same 3 values
    # but total grid points >= MIN_OBSERVATIONS
    sparse_raw = [1.0, 2.0, 3.0]  # only 3 distinct values
    # We need 30+ aligned points; create both series at daily frequency
    # but ensure series_x has only 3 distinct values
    vals_x = []
    for i in range(30):
        vals_x.append(sparse_raw[i % 3])
    s1 = [(base + timedelta(days=i), vals_x[i]) for i in range(30)]
    s2 = _ar1_series(30, seed=7)
    result = compute_granger_for_pair(s1, s2)
    assert result is None


# ---------------------------------------------------------------------------
# 7. compute_granger_for_pair: subnormal std guard via 5e-324 values
# ---------------------------------------------------------------------------

def test_compute_granger_subnormal_std_returns_none():
    """Array filled with 5e-324 (IEEE 754 minimum subnormal) must be treated as
    constant (std ~ 0) by _safe_std and cause compute_granger_for_pair -> None."""
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    # subnormal series: all values are 5e-324
    subnormal_val = 5e-324
    s_subnormal = [(base + timedelta(days=i), subnormal_val) for i in range(30)]
    s_varied = _ar1_series(30, seed=9)
    result = compute_granger_for_pair(s_subnormal, s_varied)
    assert result is None


# ---------------------------------------------------------------------------
# 8. compute_granger_for_pair: valid AR(1) pair -> dict with p_value in [0,1]
# ---------------------------------------------------------------------------

def test_compute_granger_valid_pair_returns_dict():
    """Two AR(1) processes with sufficient points -> result dict with correct keys."""
    s1 = _ar1_series(40, phi=0.7, seed=10)
    s2 = _ar1_series(40, phi=0.5, seed=20)
    result = compute_granger_for_pair(s1, s2)
    assert result is not None, "expected a result dict for valid AR(1) pair"
    assert "p_value" in result
    assert "lag" in result
    assert "sample_size" in result


def test_compute_granger_valid_pair_p_value_in_unit_interval():
    """p_value from a valid pair must be clamped to [0.0, 1.0] (D-H4/P16 guard)."""
    s1 = _ar1_series(40, phi=0.7, seed=30)
    s2 = _ar1_series(40, phi=0.5, seed=40)
    result = compute_granger_for_pair(s1, s2)
    assert result is not None
    p = result["p_value"]
    assert isinstance(p, float)
    assert 0.0 <= p <= 1.0


def test_compute_granger_valid_pair_sample_size():
    """sample_size in result must equal the number of aligned grid points."""
    s1 = _ar1_series(40, phi=0.7, seed=50)
    s2 = _ar1_series(40, phi=0.5, seed=60)
    result = compute_granger_for_pair(s1, s2)
    assert result is not None
    assert isinstance(result["sample_size"], int)
    assert result["sample_size"] >= MIN_OBSERVATIONS


def test_compute_granger_valid_pair_lag():
    """lag in result must equal max_lag passed (default 1)."""
    s1 = _ar1_series(40, phi=0.7, seed=70)
    s2 = _ar1_series(40, phi=0.5, seed=80)
    result = compute_granger_for_pair(s1, s2)
    assert result is not None
    assert result["lag"] == 1


# ---------------------------------------------------------------------------
# 9. compute_granger_for_pair: p_value clamp guard (D-H4/P16)
# ---------------------------------------------------------------------------

def test_compute_granger_p_value_clamp_via_mock():
    """If statsmodels returns a p-value slightly outside [0,1] due to
    floating-point, the clamp must bring it back into [0.0, 1.0]."""
    # Patch grangercausalitytests to return a p-value of 1.0000000001
    bad_p = 1.0000000001
    mock_result = {1: [{
        "ssr_ftest": ("stat", bad_p, "df_denom", "df_num"),
        "ssr_chi2test": ("stat", bad_p, "df"),
        "lrtest": ("stat", bad_p, "df"),
        "params_ftest": ("stat", bad_p, "df_denom", "df_num"),
    }, []]}

    s1 = _ar1_series(30, seed=11)
    s2 = _ar1_series(30, seed=22)

    # compute_granger_for_pair does a FUNCTION-LOCAL `from statsmodels.tsa.stattools
    # import grangercausalitytests`, so the symbol is re-resolved from its SOURCE
    # module on every call. Patching backend.core.granger.<name> is NOT seen by
    # that local import — the mock must be installed on the source module so the
    # local `from ... import` binds to it.
    with patch("statsmodels.tsa.stattools.grangercausalitytests", return_value=mock_result):
        result = compute_granger_for_pair(s1, s2)

    # AR(1) series with 30 points passes all guards (MIN_OBSERVATIONS, distinct
    # values, non-zero std) so result must not be None.
    assert result is not None, "Expected a result dict from the mocked granger test"
    # The clamped p_value must equal exactly 1.0 (clamped from 1.0000000001).
    assert result["p_value"] == 1.0, (
        f"Expected p_value clamped to 1.0, got {result['p_value']!r}"
    )
    assert 0.0 <= result["p_value"] <= 1.0


# ---------------------------------------------------------------------------
# 10. is_enabled: returns False when GRANGER_ENABLED is unset
# ---------------------------------------------------------------------------

def test_is_enabled_false_when_unset():
    """GRANGER_ENABLED not in environment -> is_enabled() must return False."""
    env_without = {k: v for k, v in os.environ.items() if k != "GRANGER_ENABLED"}
    with patch.dict(os.environ, env_without, clear=True):
        assert is_enabled() is False


def test_is_enabled_false_when_explicitly_false():
    """GRANGER_ENABLED=false -> is_enabled() must return False."""
    with patch.dict(os.environ, {"GRANGER_ENABLED": "false"}):
        assert is_enabled() is False


def test_is_enabled_true_when_set():
    """GRANGER_ENABLED=true -> is_enabled() must return True."""
    with patch.dict(os.environ, {"GRANGER_ENABLED": "true"}):
        assert is_enabled() is True

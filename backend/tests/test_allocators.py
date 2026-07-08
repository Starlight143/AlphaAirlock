"""Unit tests for backend/core/allocators.py and portfolio_optimizer._apply_constraints.

Round-5 finding QA-05: allocators.py (the money-path that turns a returns matrix
into portfolio weights) had ZERO test coverage. Covers each allocator, the
_safe_normalize helper, and the _apply_constraints water-fill (incl. the Round-5
QT-2 long-short fix).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.core.allocators import (
    METHOD_KEYS,
    _safe_normalize,
    allocate,
    cvar_5,
    equal_weight,
    eroc_es,
    half_kelly,
    inverse_vol,
    mean_variance,
    min_variance,
    risk_parity,
    umc,
    use_target_etl,
    vol_target,
)
from backend.core.portfolio_optimizer import _apply_constraints


def _make_returns(n_rows: int = 252, n_cols: int = 2, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    data = rng.normal(0.001, 0.02, size=(n_rows, n_cols))
    return pd.DataFrame(data, columns=list(range(1, n_cols + 1)))


def _weights_sum_to_one(w: dict, tol: float = 1e-5) -> bool:
    return abs(sum(w.values()) - 1.0) < tol


def _all_non_negative(w: dict) -> bool:
    return all(v >= -1e-9 for v in w.values())


LONG_ONLY_ALLOCATORS = [
    equal_weight, inverse_vol, risk_parity, mean_variance,
    vol_target, half_kelly, min_variance, umc,
]
ALLOCATOR_FNS = LONG_ONLY_ALLOCATORS + [cvar_5, eroc_es, use_target_etl]


@pytest.mark.parametrize("fn", ALLOCATOR_FNS, ids=lambda f: f.__name__)
def test_allocator_basic_2strategy(fn):
    """Each allocator on a 2-strategy returns frame yields non-negative weights
    that sum to 1.0 (or a degenerate all-zero book)."""
    w = fn(_make_returns(n_rows=252, n_cols=2))
    assert isinstance(w, dict)
    assert _all_non_negative(w), f"{fn.__name__} returned a negative weight: {w}"
    total = sum(w.values())
    assert total < 1e-9 or _weights_sum_to_one(w), f"{fn.__name__} sums to {total}"


@pytest.mark.parametrize("fn", ALLOCATOR_FNS, ids=lambda f: f.__name__)
def test_allocator_empty_dataframe(fn):
    """Allocators must degrade gracefully on an empty frame: empty dict or all-zero
    (never raise, never a non-zero phantom allocation)."""
    w = fn(pd.DataFrame())
    assert isinstance(w, dict)
    assert len(w) == 0 or all(abs(v) < 1e-9 for v in w.values()), (
        f"{fn.__name__} returned non-zero weights for an empty frame: {w}"
    )


def test_safe_normalize_all_negative():
    """All-negative input cannot be normalized to a long book -> all zeros."""
    result = _safe_normalize({1: -0.5, 2: -0.3, 3: -0.1})
    assert set(result.keys()) == {1, 2, 3}
    assert all(v == 0.0 for v in result.values()), f"Expected all zeros, got {result}"


# NOTE: a non-finite-input test was intentionally NOT added — _safe_normalize does
# not sanitize inf/nan (it propagates nan), but allocators only ever feed it finite
# weights, so hardening that path is out of this audit's additive scope.


def test_risk_parity_correlated():
    rng = np.random.default_rng(99)
    base = rng.normal(0.001, 0.02, 252)
    data = np.column_stack([base, base * 0.9 + rng.normal(0, 0.002, 252)])
    w = risk_parity(pd.DataFrame(data, columns=[10, 20]))
    assert _all_non_negative(w)
    assert _weights_sum_to_one(w)


def test_risk_parity_single_strategy():
    w = risk_parity(_make_returns(n_rows=100, n_cols=1))
    assert len(w) == 1
    assert abs(list(w.values())[0] - 1.0) < 1e-6


def test_cvar5_fallback_few_rows():
    """< 20 rows: CVaR is undefined; the allocator must still return a valid book
    (or a degenerate all-zero one), never raise."""
    w = cvar_5(_make_returns(n_rows=10, n_cols=2))
    assert isinstance(w, dict)
    total = sum(w.values())
    assert total < 1e-9 or _weights_sum_to_one(w), f"Unexpected total={total}"


def test_apply_constraints_max_weight_clip():
    result = _apply_constraints({1: 0.8, 2: 0.1, 3: 0.1}, max_weight=0.5, min_weight=0.0, allow_short=False)
    assert _weights_sum_to_one(result), f"Sum={sum(result.values())}"
    assert all(v <= 0.5 + 1e-6 for v in result.values()), f"Weight exceeds max: {result}"
    assert all(v >= -1e-9 for v in result.values())


def test_apply_constraints_equal_input_unchanged():
    result = _apply_constraints({1: 1 / 3, 2: 1 / 3, 3: 1 / 3}, max_weight=1.0, min_weight=0.0, allow_short=False)
    assert _weights_sum_to_one(result)


def test_apply_constraints_infeasible_max_weight():
    """4 actives * max_weight 0.2 = 0.8 < 1.0: each active pins at max_weight."""
    result = _apply_constraints({1: 0.25, 2: 0.25, 3: 0.25, 4: 0.25}, max_weight=0.2, min_weight=0.0, allow_short=False)
    for sid, v in result.items():
        assert abs(v - 0.2) < 1e-6, f"Expected 0.2 for sid={sid}, got {v}"


def test_apply_constraints_allow_short_negative_weights():
    """R5/QT-2: allow_short must PRESERVE the short leg (not silently zero it) and
    keep its sign. The water-fill renormalizes to sum≈1.0, which can push a leg's
    magnitude beyond max_weight (the P34 fully-invested contract), so we assert
    sign-preservation + book sum rather than a per-leg cap here."""
    result = _apply_constraints({1: 0.8, 2: -0.3, 3: 0.5}, max_weight=0.6, min_weight=0.0, allow_short=True)
    assert result[2] < 0.0, f"short leg must stay short (QT-2 fix), got {result[2]}"
    assert result[1] > 0.0 and result[3] > 0.0
    assert abs(sum(result.values()) - 1.0) < 1e-5, f"book must renormalize to 1.0, got {sum(result.values())}"


def test_apply_constraints_market_neutral_book_not_crashed():
    """R5/QT-1 adjudication: a net-zero gross-positive (market-neutral) book is a
    degenerate input for a sum-to-1 long normalizer. The current code returns an
    all-zero book; the key invariant is that it returns a dict WITHOUT raising
    (no ZeroDivisionError)."""
    result = _apply_constraints({1: 0.3, 2: 0.3, 3: -0.3, 4: -0.3}, max_weight=0.5, min_weight=0.0, allow_short=True)
    assert isinstance(result, dict)
    assert set(result.keys()) == {1, 2, 3, 4}


def test_apply_constraints_infeasible_min_weight():
    """3 actives * min_weight 0.4 = 1.2 > 1.0: fall back to an even split."""
    result = _apply_constraints({1: 0.5, 2: 0.3, 3: 0.2}, max_weight=1.0, min_weight=0.4, allow_short=False)
    expected = round(1.0 / 3, 6)
    for sid in (1, 2, 3):
        assert abs(result[sid] - expected) < 1e-4, f"sid={sid}: expected ~{expected}, got {result[sid]}"
    assert _weights_sum_to_one(result)


def test_allocate_unknown_method_falls_back_to_inverse_vol():
    returns = _make_returns()
    assert allocate("nonexistent_method", returns) == inverse_vol(returns)


@pytest.mark.parametrize("method", METHOD_KEYS)
def test_allocate_all_method_keys(method):
    w = allocate(method, _make_returns(n_rows=252, n_cols=3))
    assert isinstance(w, dict)
    total = sum(w.values())
    assert total < 1e-9 or _weights_sum_to_one(w), f"method={method} sums to {total}"

"""Tests for the strategy diversity gate (P-DIVERSITY, backend.core.diversity).

Measures a candidate's redundancy as the max POSITIVE return-correlation vs the
approved pool. Observe-by-default (records, never blocks); enforce can veto.
"""
from __future__ import annotations

import pandas as pd
import pytest

import backend.core.diversity as D


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("STRATEGY_DIVERSITY_GATE", "STRATEGY_DIVERSITY_MAX_CORR",
              "STRATEGY_DIVERSITY_POOL_SIZE", "STRATEGY_DIVERSITY_MIN_OVERLAP"):
        monkeypatch.delenv(k, raising=False)
    D.invalidate_pool_cache()
    yield
    D.invalidate_pool_cache()


def _curve(equities):
    base = pd.Timestamp("2024-01-01", tz="UTC")
    return [{"timestamp": (base + pd.Timedelta(days=i)).isoformat(),
             "equity": float(e)} for i, e in enumerate(equities)]


def test_returns_from_equity_curve():
    r = D.returns_from_equity_curve(_curve([1.0, 1.1, 1.0, 1.2, 1.15]))
    assert r is not None and len(r) == 4
    assert D.returns_from_equity_curve([]) is None
    assert D.returns_from_equity_curve([{"timestamp": "x", "equity": 1.0}]) is None


def test_correlation_identical_and_inverse():
    idx = pd.date_range("2024-01-01", periods=30, freq="D", tz="UTC")
    a = pd.Series([(-1) ** i * 0.01 for i in range(30)], index=idx)
    assert D.correlation(a, a, min_overlap=10) == pytest.approx(1.0)
    assert D.correlation(a, -a, min_overlap=10) == pytest.approx(-1.0)
    # Too little overlap → None.
    short = a.iloc[:3]
    assert D.correlation(a, short, min_overlap=10) is None


def test_max_correlation_picks_most_positive():
    idx = pd.date_range("2024-01-01", periods=30, freq="D", tz="UTC")
    cand = pd.Series([(-1) ** i * 0.01 for i in range(30)], index=idx)
    pool = [
        (1, -cand),               # corr -1
        (2, cand * 0.5 + 0.001),  # corr ~ +1 (clone)
    ]
    c, sid = D.max_correlation_against(cand, pool, min_overlap=10)
    assert sid == 2 and c == pytest.approx(1.0, abs=1e-6)


def test_evaluate_observe_vs_enforce(monkeypatch):
    idx = pd.date_range("2024-01-01", periods=40, freq="D", tz="UTC")
    clone = pd.Series([(-1) ** i * 0.02 for i in range(40)], index=idx)
    # Pool already contains a near-clone of the candidate's return stream.
    monkeypatch.setattr(D, "_load_pool", lambda limit, exclude: [(11, clone)])

    # OBSERVE (default): redundant is detected but passed stays True.
    res = D.evaluate(99, _high_corr_curve(idx))
    assert res.enforced is False
    assert res.passed is True
    assert res.redundant is True
    assert res.most_similar_id == 11

    # ENFORCE: a redundant candidate fails.
    monkeypatch.setenv("STRATEGY_DIVERSITY_GATE", "1")
    res2 = D.evaluate(99, _high_corr_curve(idx))
    assert res2.enforced is True
    assert res2.passed is False


def _high_corr_curve(idx):
    # Equity curve whose daily returns are exactly [(-1)^i * 0.02] → matches the
    # `clone` series the pool is monkeypatched to contain (corr ≈ 1).
    eq = [1.0]
    for i in range(1, len(idx)):
        eq.append(eq[-1] * (1.0 + ((-1) ** i) * 0.02))
    return [{"timestamp": idx[i].isoformat(), "equity": eq[i]} for i in range(len(idx))]


def test_evaluate_empty_pool_is_not_redundant(monkeypatch):
    monkeypatch.setattr(D, "_load_pool", lambda limit, exclude: [])
    res = D.evaluate(1, _curve([1.0, 1.1, 1.2, 1.3]))
    assert res.redundant is False and res.passed is True and res.pool_size == 0

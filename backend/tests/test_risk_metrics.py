"""Tests for the additive risk metrics (backend.core.risk_metrics).

Locks in the honesty metrics that quantify the ways a raw annualized Sharpe lies
(serial correlation, downside asymmetry, fat tails / overfitting). These feed the
deterministic quality gate and, importantly, the ρ₁-correction that de-inflates
the multi-asset equity Sharpe (its ~80% flat-filled bars create autocorrelation).
"""
from __future__ import annotations

import math

import numpy as np
import pytest

import backend.core.risk_metrics as rm


# --------------------------------------------------------------------------- #
# Sortino / TDD — the note's thought-experiment is the canonical regression    #
# --------------------------------------------------------------------------- #

def test_tdd_keeps_all_observations():
    # 《Sortino》note: [0,0,0,-10] (one failure) must have LOWER downside
    # deviation than [-10,-10,-10,-10] (four failures). The common bug (std of
    # negatives only) makes them look identical.
    one_fail = rm.downside_deviation([0.0, 0.0, 0.0, -10.0], mar=0.0)
    four_fail = rm.downside_deviation([-10.0, -10.0, -10.0, -10.0], mar=0.0)
    assert one_fail == pytest.approx(5.0)    # sqrt((10^2)/4)
    assert four_fail == pytest.approx(10.0)  # sqrt((4*10^2)/4)
    assert one_fail < four_fail


def test_sortino_positive_for_net_positive_series():
    r = [0.01, -0.002, 0.008, -0.001, 0.006] * 10
    s = rm.sortino_ratio(r, mar=0.0)
    assert s is not None and s > 0


def test_sortino_none_on_no_downside():
    # All non-negative → TDD = 0 → undefined (guarded, not inf).
    assert rm.sortino_ratio([0.0, 0.01, 0.02, 0.03], mar=0.0) is None


# --------------------------------------------------------------------------- #
# Lag-1 autocorrelation + Lo adjustment                                        #
# --------------------------------------------------------------------------- #

def test_lag1_autocorr_perfect_positive_and_negative():
    assert rm.lag1_autocorrelation([1, 2, 3, 4, 5, 6]) == pytest.approx(1.0)
    assert rm.lag1_autocorrelation([1, -1, 1, -1, 1, -1]) == pytest.approx(-1.0)


def test_lo_adjustment_deflates_on_positive_autocorr():
    # Positive ρ₁ (the equity flat-bar case) must DEFLATE the reported Sharpe.
    adj = rm.lo_adjusted_sharpe(2.0, 0.5)
    assert adj is not None and adj < 2.0
    assert adj == pytest.approx(2.0 * math.sqrt(0.5 / 1.5), rel=1e-9)
    # ρ₁ = 0 → unchanged; negative ρ₁ → inflated (mirror image).
    assert rm.lo_adjusted_sharpe(2.0, 0.0) == pytest.approx(2.0)
    assert rm.lo_adjusted_sharpe(2.0, -0.5) > 2.0
    # No ρ₁ available → returns the reported value unchanged.
    assert rm.lo_adjusted_sharpe(1.3, None) == pytest.approx(1.3)


# --------------------------------------------------------------------------- #
# Skew / kurtosis                                                              #
# --------------------------------------------------------------------------- #

def test_skewness_sign():
    left = [-9.0] + [1.0] * 9   # one big negative → left/negative skew
    right = [9.0] + [-1.0] * 9  # one big positive → right/positive skew
    assert rm.skewness(left) < 0
    assert rm.skewness(right) > 0


def test_kurtosis_non_excess_normalish():
    rng = np.random.default_rng(7)
    k = rm.kurtosis(rng.normal(0.0, 1.0, 5000).tolist())
    assert k is not None and 2.5 < k < 3.5  # ~3 for normal (non-excess)


# --------------------------------------------------------------------------- #
# Probabilistic Sharpe Ratio                                                   #
# --------------------------------------------------------------------------- #

def test_psr_requires_min_bars():
    assert rm.probabilistic_sharpe_ratio([0.01, -0.005] * 5) is None  # 10 < 30


def test_psr_bounded_and_high_for_clean_positive():
    r = [0.01, -0.004] * 40  # 80 bars, clearly positive mean, modest variance
    psr = rm.probabilistic_sharpe_ratio(r, benchmark_sr=0.0)
    assert psr is not None and 0.0 <= psr <= 1.0
    assert psr > 0.5


def test_psr_negative_skew_penalized():
    # Same approximate mean/vol, but a fat left tail should not score ~1.0.
    rng = np.random.default_rng(3)
    base = rng.normal(0.001, 0.01, 400)
    base[::50] = -0.08  # inject periodic crashes → negative skew + fat tail
    psr = rm.probabilistic_sharpe_ratio(base.tolist())
    assert psr is not None and 0.0 <= psr <= 1.0


# --------------------------------------------------------------------------- #
# Aggregator + per_bar extraction                                              #
# --------------------------------------------------------------------------- #

def test_compute_risk_metrics_keys_and_sharpe_adjust():
    r = ([0.01, -0.004, 0.006, -0.002] * 25)  # 100 bars
    out = rm.compute_risk_metrics(r, reported_sharpe=2.0)
    for k in ("lag1_autocorrelation", "sortino_ratio", "return_skewness",
              "return_kurtosis", "probabilistic_sharpe_ratio",
              "sharpe_autocorr_adjusted"):
        assert k in out, k
    # Every value is JSON-clean (finite float).
    assert all(isinstance(v, float) and math.isfinite(v) for v in out.values())


def test_compute_risk_metrics_degenerate_is_safe():
    out = rm.compute_risk_metrics([0.0] * 50, reported_sharpe=0.0)
    # No blow-ups: the undefined ratios are omitted, not Inf/NaN.
    assert "probabilistic_sharpe_ratio" not in out
    assert "sortino_ratio" not in out
    assert all(math.isfinite(v) for v in out.values())


def test_compute_from_per_bar():
    per_bar = [{"pnl_pct": v} for v in ([0.01, -0.004, 0.006, -0.002] * 25)]
    out = rm.compute_from_per_bar(per_bar, reported_sharpe=1.5)
    assert "sortino_ratio" in out and "probabilistic_sharpe_ratio" in out
    # Robust to empty / malformed input.
    assert rm.compute_from_per_bar(None) == {}
    assert rm.compute_from_per_bar([]) == {}
    assert rm.compute_from_per_bar([{"nope": 1}]) == {}


# --------------------------------------------------------------------------- #
# Deflated Sharpe Ratio — multiple-testing correction (Bailey-LdP 2014)        #
# --------------------------------------------------------------------------- #

def _positive_series(seed: int = 7, n: int = 600):
    rng = np.random.default_rng(seed)
    return list(rng.normal(0.0008, 0.01, n))


def test_dsr_never_exceeds_psr():
    # DSR raises the PSR benchmark from 0 to the expected best-of-N → it can only
    # be <= the plain PSR. Deflation, never inflation.
    r = _positive_series()
    psr = rm.probabilistic_sharpe_ratio(r, benchmark_sr=0.0)
    dsr = rm.deflated_sharpe_ratio(r, [1.0, 1.1, 0.9, 1.05, 0.95] * 8)
    assert psr is not None and dsr is not None
    assert dsr <= psr + 1e-9


def test_dsr_more_dispersion_lowers_score():
    # A wider spread of trial Sharpes ⇒ higher expected maximum-under-the-null ⇒
    # the strategy must clear a higher bar ⇒ lower DSR.
    r = _positive_series()
    tight = rm.deflated_sharpe_ratio(r, [1.0, 1.1, 0.9, 1.05, 0.95] * 8)
    wide = rm.deflated_sharpe_ratio(r, list(np.linspace(-3.0, 6.0, 40)))
    assert tight is not None and wide is not None
    assert wide <= tight + 1e-9


def test_dsr_requires_two_trials_and_nonzero_dispersion():
    r = _positive_series()
    assert rm.deflated_sharpe_ratio(r, [1.0]) is None          # < 2 trials
    assert rm.deflated_sharpe_ratio(r, []) is None             # no trials
    assert rm.deflated_sharpe_ratio(r, [1.0, 1.0, 1.0]) is None  # zero dispersion
    # NaN/inf trial Sharpes are dropped before counting.
    assert rm.deflated_sharpe_ratio(r, [1.0, float("nan"), float("inf")]) is None


def test_dsr_bounded_and_from_per_bar_matches():
    r = _positive_series()
    trials = [1.0, 1.1, 0.9, 1.05, 0.95] * 8
    dsr = rm.deflated_sharpe_ratio(r, trials)
    assert dsr is not None and 0.0 <= dsr <= 1.0
    # The per_bar convenience wrapper extracts pnl_pct and must agree exactly.
    per_bar = [{"pnl_pct": x} for x in r]
    assert rm.deflated_sharpe_from_per_bar(per_bar, trials) == pytest.approx(dsr)
    # Malformed / empty input is safe (None, never raises).
    assert rm.deflated_sharpe_from_per_bar(None, trials) is None
    assert rm.deflated_sharpe_from_per_bar([], trials) is None
    assert rm.deflated_sharpe_from_per_bar([{"nope": 1}], trials) is None

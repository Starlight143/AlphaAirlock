"""Tests for the deterministic strategy gates (backend.core.strategy_gates).

Locks in: (1) the pre-critic liveness gate rejects dead backtests (0 trades) so
the Critic LLM call is skipped; (2) the post-critic quality gate is OBSERVE-only
by default (never changes the verdict) and only vetoes when explicitly enforced.
"""
from __future__ import annotations

import pytest

import backend.core.strategy_gates as G

_GATE_ENV_KEYS = (
    "STRATEGY_PRECRITIC_GATE",
    "STRATEGY_PRECRITIC_MIN_TRADES",
    "STRATEGY_GATE_ENFORCE",
    "STRATEGY_GATE_MIN_TRADES",
    "STRATEGY_GATE_MIN_PSR",
    "STRATEGY_GATE_MIN_SORTINO",
    "STRATEGY_GATE_MIN_DSR",
    "STRATEGY_GATE_MIN_CALMAR",
    "STRATEGY_GATE_SHORTVOL_SHARPE",
    "STRATEGY_GATE_SHORTVOL_SKEW",
    # T1-D regime gate
    "STRATEGY_REGIME_GATE",
    "STRATEGY_REGIME_WINDOWS",
    "STRATEGY_REGIME_MIN_POSITIVE_FRAC",
    "STRATEGY_REGIME_MIN_WORST_SHARPE",
    "STRATEGY_REGIME_MAX_DISPERSION",
    # T3-B score card
    "STRATEGY_SCORECARD_GATE",
    "STRATEGY_SCORECARD_MAX_TRADES",
)


@pytest.fixture(autouse=True)
def _clean_gate_env(monkeypatch):
    for k in _GATE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    yield


# --------------------------------------------------------------------------- #
# Pre-critic liveness gate                                                     #
# --------------------------------------------------------------------------- #

def test_precritic_rejects_zero_trades_by_default():
    assert G.is_backtest_alive({"num_trades": 0}) is False
    assert G.is_backtest_alive({"num_trades": 5}) is True
    # Missing key is treated as 0 → dead.
    assert G.is_backtest_alive({}) is False


def test_precritic_can_be_disabled(monkeypatch):
    monkeypatch.setenv("STRATEGY_PRECRITIC_GATE", "0")
    assert G.is_backtest_alive({"num_trades": 0}) is True  # gate off → always alive


def test_precritic_min_trades_override(monkeypatch):
    monkeypatch.setenv("STRATEGY_PRECRITIC_MIN_TRADES", "10")
    assert G.is_backtest_alive({"num_trades": 9}) is False
    assert G.is_backtest_alive({"num_trades": 10}) is True


def test_precritic_reason_is_descriptive():
    reason = G.precritic_reject_reason({"num_trades": 0})
    assert "0 trades" in reason and "Critic" in reason


# --------------------------------------------------------------------------- #
# Quality gate — OBSERVE default                                               #
# --------------------------------------------------------------------------- #

def test_quality_gate_observe_never_blocks():
    # Awful metrics, but default (observe) mode must NOT block the verdict.
    metrics = {"num_trades": 1, "probabilistic_sharpe_ratio": 0.10,
               "annualized_sharpe": 0.0}
    res = G.evaluate_quality(metrics)
    assert res.enforced is False
    assert res.passed is True            # observe → never blocks
    assert res.vetoes                    # ...but vetoes ARE recorded for telemetry
    assert any("min_trades" in v for v in res.vetoes)
    assert any("psr" in v for v in res.vetoes)


def test_quality_gate_clean_metrics_no_veto():
    metrics = {"num_trades": 120, "probabilistic_sharpe_ratio": 0.97,
               "annualized_sharpe": 1.4, "sortino_ratio": 2.1,
               "return_skewness": 0.2}
    res = G.evaluate_quality(metrics)
    assert res.vetoes == []
    assert res.passed is True


# --------------------------------------------------------------------------- #
# Quality gate — ENFORCE                                                       #
# --------------------------------------------------------------------------- #

def test_quality_gate_enforce_blocks_on_veto(monkeypatch):
    monkeypatch.setenv("STRATEGY_GATE_ENFORCE", "1")
    res = G.evaluate_quality({"num_trades": 3, "probabilistic_sharpe_ratio": 0.4})
    assert res.enforced is True
    assert res.passed is False
    assert res.reason  # non-empty human-readable veto reason


def test_quality_gate_enforce_passes_clean(monkeypatch):
    monkeypatch.setenv("STRATEGY_GATE_ENFORCE", "1")
    res = G.evaluate_quality({"num_trades": 120, "probabilistic_sharpe_ratio": 0.97})
    assert res.passed is True


def test_psr_floor_zero_disables_check(monkeypatch):
    monkeypatch.setenv("STRATEGY_GATE_ENFORCE", "1")
    monkeypatch.setenv("STRATEGY_GATE_MIN_PSR", "0")     # disable PSR floor
    monkeypatch.setenv("STRATEGY_GATE_MIN_TRADES", "0")  # disable trades floor
    res = G.evaluate_quality({"num_trades": 1, "probabilistic_sharpe_ratio": 0.01})
    assert res.vetoes == [] and res.passed is True


def test_sortino_floor_opt_in(monkeypatch):
    monkeypatch.setenv("STRATEGY_GATE_ENFORCE", "1")
    monkeypatch.setenv("STRATEGY_GATE_MIN_TRADES", "0")
    monkeypatch.setenv("STRATEGY_GATE_MIN_PSR", "0")
    monkeypatch.setenv("STRATEGY_GATE_MIN_SORTINO", "1.0")
    assert G.evaluate_quality({"sortino_ratio": 0.5}).passed is False
    assert G.evaluate_quality({"sortino_ratio": 1.5}).passed is True


def test_dsr_and_calmar_floors_default_disabled():
    # Both floors default to 0 (observe-first): the metrics are present but no
    # veto fires until an operator sets a positive floor.
    assert G.gate_min_dsr() == 0.0
    assert G.gate_min_calmar() == 0.0
    res = G.evaluate_quality({"num_trades": 120, "deflated_sharpe_ratio": 0.01,
                              "calmar_ratio": 0.01})
    assert not any("dsr(" in v or "calmar(" in v for v in res.vetoes)


def test_dsr_floor_opt_in(monkeypatch):
    monkeypatch.setenv("STRATEGY_GATE_ENFORCE", "1")
    monkeypatch.setenv("STRATEGY_GATE_MIN_TRADES", "0")
    monkeypatch.setenv("STRATEGY_GATE_MIN_PSR", "0")
    monkeypatch.setenv("STRATEGY_GATE_MIN_DSR", "0.5")
    assert G.evaluate_quality({"deflated_sharpe_ratio": 0.2}).passed is False
    assert G.evaluate_quality({"deflated_sharpe_ratio": 0.8}).passed is True
    # Absent DSR (too few prior trials to deflate) is never vetoed.
    assert G.evaluate_quality({"num_trades": 99}).passed is True


def test_calmar_floor_opt_in(monkeypatch):
    monkeypatch.setenv("STRATEGY_GATE_ENFORCE", "1")
    monkeypatch.setenv("STRATEGY_GATE_MIN_TRADES", "0")
    monkeypatch.setenv("STRATEGY_GATE_MIN_PSR", "0")
    monkeypatch.setenv("STRATEGY_GATE_MIN_CALMAR", "1.0")
    assert G.evaluate_quality({"calmar_ratio": 0.5}).passed is False
    assert G.evaluate_quality({"calmar_ratio": 1.5}).passed is True
    assert G.evaluate_quality({"num_trades": 99}).passed is True  # absent → skip


# --------------------------------------------------------------------------- #
# Short-vol red flag (flag, not veto)                                          #
# --------------------------------------------------------------------------- #

def test_short_vol_flag_independent_of_enforcement():
    # High Sharpe + negative skew → flag, but NOT a veto (it's advisory).
    res = G.evaluate_quality({"num_trades": 200, "probabilistic_sharpe_ratio": 0.99,
                              "annualized_sharpe": 3.0, "return_skewness": -1.5})
    assert any("short_vol_signature" in f for f in res.flags)
    assert res.passed is True  # observe mode, and flags never veto


# --------------------------------------------------------------------------- #
# T1-D — Regime / sub-period stability gate                                   #
# --------------------------------------------------------------------------- #

def _regime_metrics(pos_frac, worst, n_windows=6, disp=0.5):
    return {
        "regime_n_windows": n_windows,
        "regime_positive_fraction": pos_frac,
        "regime_worst_sharpe": worst,
        "regime_best_sharpe": 3.0,
        "regime_sharpe_dispersion": disp,
        "regime_mean_sharpe": 1.0,
    }


def test_regime_gate_observe_never_blocks():
    # Awful regime metrics, but env unset → OBSERVE → passes, vetoes recorded.
    res = G.evaluate_regime(_regime_metrics(0.1, -5.0))
    assert res.enforced is False
    assert res.passed is True
    assert res.insufficient_data is False
    assert res.vetoes  # computed for telemetry


def test_regime_gate_enforce_blocks_low_positive_fraction(monkeypatch):
    monkeypatch.setenv("STRATEGY_REGIME_GATE", "1")
    res = G.evaluate_regime(_regime_metrics(0.1, -5.0))
    assert res.enforced is True
    assert res.passed is False
    assert res.reason


def test_regime_gate_enforce_passes_stable(monkeypatch):
    monkeypatch.setenv("STRATEGY_REGIME_GATE", "1")
    res = G.evaluate_regime(_regime_metrics(1.0, 0.8))
    assert res.passed is True
    assert not res.vetoes


def test_regime_gate_insufficient_data_passes_even_enforced(monkeypatch):
    monkeypatch.setenv("STRATEGY_REGIME_GATE", "1")
    # No regime_* keys at all → never veto on missing telemetry.
    res = G.evaluate_regime({"num_trades": 50})
    assert res.insufficient_data is True
    assert res.passed is True
    # A single evaluated window is also insufficient.
    res2 = G.evaluate_regime(_regime_metrics(0.0, -9.0, n_windows=1))
    assert res2.insufficient_data is True and res2.passed is True


def test_regime_dispersion_floor_opt_in(monkeypatch):
    monkeypatch.setenv("STRATEGY_REGIME_GATE", "1")
    # Default max-dispersion 0 → disabled → high dispersion alone doesn't veto.
    res = G.evaluate_regime(_regime_metrics(1.0, 0.5, disp=2.0))
    assert res.passed is True
    monkeypatch.setenv("STRATEGY_REGIME_MAX_DISPERSION", "0.5")
    res2 = G.evaluate_regime(_regime_metrics(1.0, 0.5, disp=2.0))
    assert res2.passed is False


# --------------------------------------------------------------------------- #
# T3-B — Pre-critic Score Card                                                #
# --------------------------------------------------------------------------- #

def _good_metrics(**over):
    m = {
        "num_trades": 50,
        "annualized_sharpe": 1.2,
        "max_drawdown": -0.1,
        "profit_factor": 1.4,
        "std_hourly_return": 0.01,
    }
    m.update(over)
    return m


def test_scorecard_default_matches_liveness():
    assert G.evaluate_scorecard({"num_trades": 0}).alive is False
    assert G.evaluate_scorecard(_good_metrics()).alive is True


def test_scorecard_disabled_collapses_to_liveness(monkeypatch):
    monkeypatch.setenv("STRATEGY_SCORECARD_GATE", "0")
    # A non-finite core metric would normally be culled, but with the score card
    # off only the liveness trade-floor applies → alive (== is_backtest_alive).
    m = _good_metrics(annualized_sharpe=float("nan"))
    assert G.evaluate_scorecard(m).alive is True
    assert G.is_backtest_alive(m) is True


def test_scorecard_rejects_nonfinite_core_metric():
    res = G.evaluate_scorecard(_good_metrics(annualized_sharpe=float("nan")))
    assert res.alive is False
    assert res.checks["core_metrics_finite"] is False
    assert res.reason


def test_scorecard_rejects_overtrading(monkeypatch):
    monkeypatch.setenv("STRATEGY_SCORECARD_MAX_TRADES", "100")
    res = G.evaluate_scorecard(_good_metrics(num_trades=5000))
    assert res.alive is False
    assert res.checks["trades_sane_upper"] is False


def test_scorecard_rejects_constant_returns():
    res = G.evaluate_scorecard(_good_metrics(std_hourly_return=0.0))
    assert res.alive is False
    assert res.checks["returns_nondegenerate"] is False


def test_scorecard_rejects_zero_regime_windows():
    res = G.evaluate_scorecard(_good_metrics(regime_n_windows=0))
    assert res.alive is False
    assert res.checks["regime_observed"] is False


def test_scorecard_passes_clean():
    res = G.evaluate_scorecard(_good_metrics(regime_n_windows=6))
    assert res.alive is True
    assert all(res.checks.values())

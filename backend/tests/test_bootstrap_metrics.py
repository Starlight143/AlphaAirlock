"""T-BOOTSTRAP — block-bootstrap Sharpe CI + the observe-default quality floor."""
from __future__ import annotations

import math

import pytest

import backend.core.bootstrap_metrics as BM
from backend.core import strategy_gates


def _per_bar(returns):
    return [{"pnl_pct": r} for r in returns]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("STRATEGY_BOOTSTRAP_ENABLED", "STRATEGY_BOOTSTRAP_N",
              "STRATEGY_GATE_MIN_BOOTSTRAP_PROB", "STRATEGY_GATE_ENFORCE"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_too_few_bars_returns_empty():
    assert BM.compute_from_per_bar(_per_bar([0.01, -0.01] * 10)) == {}  # 20 < 100


def test_constant_returns_empty():
    assert BM.compute_from_per_bar(_per_bar([0.0] * 200)) == {}


def test_disabled_returns_empty(monkeypatch):
    monkeypatch.setenv("STRATEGY_BOOTSTRAP_ENABLED", "0")
    rets = [0.01 if i % 2 else -0.005 for i in range(300)]
    assert BM.compute_from_per_bar(_per_bar(rets)) == {}


def test_keys_and_ordering():
    rets = [0.002 + (0.001 if i % 3 else -0.0015) for i in range(400)]
    out = BM.compute_from_per_bar(_per_bar(rets), n_boot=300)
    assert set(out) >= {
        "bootstrap_sharpe_ci_low", "bootstrap_sharpe_ci_high",
        "bootstrap_sharpe_median", "bootstrap_prob_sharpe_positive",
        "bootstrap_n", "bootstrap_block",
    }
    assert (out["bootstrap_sharpe_ci_low"] <= out["bootstrap_sharpe_median"]
            <= out["bootstrap_sharpe_ci_high"])
    assert 0.0 <= out["bootstrap_prob_sharpe_positive"] <= 1.0
    assert out["bootstrap_n"] == 300
    assert all(math.isfinite(out[k]) for k in
               ("bootstrap_sharpe_ci_low", "bootstrap_sharpe_ci_high",
                "bootstrap_sharpe_median"))


def test_deterministic():
    rets = [0.001 * ((i % 7) - 3) for i in range(250)]
    a = BM.compute_from_per_bar(_per_bar(rets), n_boot=200)
    b = BM.compute_from_per_bar(_per_bar(rets), n_boot=200)
    assert a == b  # fixed seed ⇒ identical


def test_strong_positive_drift_prob_high():
    rets = [0.003 + (0.0005 if i % 2 else -0.0003) for i in range(300)]
    out = BM.compute_from_per_bar(_per_bar(rets), n_boot=300)
    assert out["bootstrap_prob_sharpe_positive"] > 0.8


def test_gate_floor_observe_then_enforce(monkeypatch):
    metrics = {"bootstrap_prob_sharpe_positive": 0.40, "num_trades": 100}
    # Floor unset (default 0) → no bootstrap veto.
    r0 = strategy_gates.evaluate_quality(metrics)
    assert not any("bootstrap" in v for v in r0.vetoes)
    # Floor set, observe (ENFORCE off) → veto recorded but the gate still passes.
    monkeypatch.setenv("STRATEGY_GATE_MIN_BOOTSTRAP_PROB", "0.6")
    r1 = strategy_gates.evaluate_quality(metrics)
    assert any("bootstrap_prob" in v for v in r1.vetoes)
    assert r1.passed is True
    # Enforce → the bootstrap veto now blocks.
    monkeypatch.setenv("STRATEGY_GATE_ENFORCE", "1")
    r2 = strategy_gates.evaluate_quality(metrics)
    assert r2.passed is False

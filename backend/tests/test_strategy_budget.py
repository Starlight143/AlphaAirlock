"""Tests for the per-strategy LLM USD cap (P-STRATBUDGET, backend.core.llm_budget).

The per-strategy ceiling is independent of the daily cap, scoped via a context
variable the orchestrator sets, and raises the SAME ``LLMBudgetExceededError`` —
so a runaway strategy is stopped exactly like a daily-cap breach. Default off.
"""
from __future__ import annotations

import pytest

import backend.core.llm_budget as B


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    # Isolate the on-disk daily-state dir and reset all in-memory accounting.
    monkeypatch.setattr(B, "_STATE_DIR", tmp_path)
    B._STATE_CACHE.clear()
    B._RESERVATIONS.clear()
    B._PER_STRATEGY_SPENT.clear()
    for k in ("ALPHA_LLM_DAILY_USD_CAP", "ALPHA_LLM_PER_STRATEGY_USD_CAP"):
        monkeypatch.delenv(k, raising=False)
    # Deterministic, price-independent cost: every settled call costs $0.01.
    monkeypatch.setattr(B, "_chars_to_usd", lambda i, o: 0.01)
    yield
    B._PER_STRATEGY_SPENT.clear()


def test_per_strategy_cap_disabled_by_default(monkeypatch):
    # No cap configured ⇒ reserve is a no-op (returns None), never raises.
    with B.strategy_budget_scope(1):
        for _ in range(100):
            tok = B.reserve_budget(1000, agent="x")
            B.settle_reservation(tok, 5000, 5000, agent="x")
    assert B.reserve_budget(1000, agent="x") is None


def test_per_strategy_cap_enforced(monkeypatch):
    monkeypatch.setenv("ALPHA_LLM_PER_STRATEGY_USD_CAP", "0.025")  # daily stays off
    with B.strategy_budget_scope(7):
        t1 = B.reserve_budget(10, agent="a")
        B.settle_reservation(t1, 1, 1, agent="a")   # spent 0.01
        t2 = B.reserve_budget(10, agent="a")
        B.settle_reservation(t2, 1, 1, agent="a")   # spent 0.02
        # Next projection 0.02 + 0.01 = 0.03 ≥ 0.025 → blocked.
        with pytest.raises(B.LLMBudgetExceededError):
            B.reserve_budget(10, agent="a")


def test_per_strategy_cap_is_scoped(monkeypatch):
    monkeypatch.setenv("ALPHA_LLM_PER_STRATEGY_USD_CAP", "0.025")
    # Strategy A burns its budget...
    with B.strategy_budget_scope(100):
        B.settle_reservation(B.reserve_budget(10), 1, 1)
        B.settle_reservation(B.reserve_budget(10), 1, 1)
    # ...and on scope exit its accumulated spend is cleared (bounded memory).
    assert 100 not in B._PER_STRATEGY_SPENT
    # A different strategy starts fresh — not blocked by A's spend.
    with B.strategy_budget_scope(200):
        assert B.reserve_budget(10) is not None  # no raise


def test_no_strategy_context_means_no_per_strategy_enforcement(monkeypatch):
    monkeypatch.setenv("ALPHA_LLM_PER_STRATEGY_USD_CAP", "0.001")
    # Outside any strategy scope (sid=None), the per-strategy cap never fires.
    for _ in range(50):
        tok = B.reserve_budget(10)
        B.settle_reservation(tok, 1, 1)  # accumulates nothing (sid None)
    assert B.reserve_budget(10) is not None

"""T-REVISE — critic-feedback story revision (researcher.revise_alpha_story)."""
from __future__ import annotations

import pytest

import backend.agents.researcher as R
from backend.agents._client import LLMProviderError


def test_empty_inputs_return_none():
    assert R.revise_alpha_story("", "crit") is None
    assert R.revise_alpha_story("story", "") is None


def test_identical_echo_rejected(monkeypatch):
    monkeypatch.setattr(R, "call_messages", lambda **kw: "  Same story.  ")
    assert R.revise_alpha_story("Same story.", "fix the trades") is None


def test_valid_revision(monkeypatch):
    monkeypatch.setattr(
        R, "call_messages",
        lambda **kw: "# New headline\n\nSharper edge.\n```yaml\nx: 1\n```",
    )
    out = R.revise_alpha_story("Old story.", "psr too low; insufficient_trades")
    assert out is not None and "Sharper edge" in out


def test_provider_error_returns_none(monkeypatch):
    def _boom(**kw):
        raise LLMProviderError("503")
    monkeypatch.setattr(R, "call_messages", _boom)
    assert R.revise_alpha_story("Old story.", "fix") is None


def test_budget_error_propagates(monkeypatch):
    # LLMBudgetExceededError is a RuntimeError (not an LLMProviderError) and must
    # NOT be swallowed — it has to propagate so the per-strategy cap still bites.
    from backend.core.llm_budget import LLMBudgetExceededError

    def _budget(**kw):
        raise LLMBudgetExceededError("cap")
    monkeypatch.setattr(R, "call_messages", _budget)
    with pytest.raises(LLMBudgetExceededError):
        R.revise_alpha_story("Old story.", "fix")

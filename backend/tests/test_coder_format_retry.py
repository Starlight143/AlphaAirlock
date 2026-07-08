"""T1-C — Coder format-failure retry (bounded re-prompt on parse/AST failure).

When CODER_FORMAT_RETRY_MAX=0 (default) the coder raises on the first invalid
output exactly as before. When >0, it re-prompts (with the error appended) up to
N extra times BEFORE backtest, never interacting with the orchestrator's
separate Critic-feedback loop, and never swallowing a budget breach.
"""
from __future__ import annotations

import pytest

import backend.agents.coder as coder
from backend.agents._client import LLMBudgetExceededError
from backend.agents.coder import CoderValidationError, generate_factor_code

_VALID = "```python\ndef compute_factor(df):\n    return df['close'].rolling(5).mean().fillna(0)\n```"
_DUP = (  # two top-level compute_factor -> CoderValidationError (AST)
    "```python\ndef compute_factor(df):\n    return df['close']\n"
    "def compute_factor(df):\n    return df['open']\n```"
)
_MISSING = "```python\nx = 1\n```"  # no compute_factor anywhere -> ValueError

_STORY = "Buy when close crosses its 5-bar mean."


class _StubLLM:
    def __init__(self, returns):
        self._returns = list(returns)
        self.calls = []

    def __call__(self, *, system, user, **kwargs):
        self.calls.append({"system": system, "user": user, **kwargs})
        nxt = self._returns.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CODER_FORMAT_RETRY_MAX", raising=False)
    yield


def test_format_retry_off_raises_first_attempt(monkeypatch):
    stub = _StubLLM([_DUP])
    monkeypatch.setattr(coder, "call_messages", stub)
    with pytest.raises(CoderValidationError):
        generate_factor_code(_STORY)
    assert len(stub.calls) == 1  # no retry when disabled


def test_format_retry_off_missing_compute_factor(monkeypatch):
    stub = _StubLLM([_MISSING])
    monkeypatch.setattr(coder, "call_messages", stub)
    with pytest.raises(ValueError) as ei:
        generate_factor_code(_STORY)
    assert "Raw response head" in str(ei.value)
    assert len(stub.calls) == 1


def test_format_retry_recovers_on_second_attempt(monkeypatch):
    monkeypatch.setenv("CODER_FORMAT_RETRY_MAX", "2")
    stub = _StubLLM([_DUP, _VALID])
    monkeypatch.setattr(coder, "call_messages", stub)
    code = generate_factor_code(_STORY)
    assert "def compute_factor" in code
    assert len(stub.calls) == 2
    # The retried prompt must carry the format-error suffix.
    assert "REJECTED before backtest" in stub.calls[1]["user"]


def test_format_retry_bound_exhausted(monkeypatch):
    monkeypatch.setenv("CODER_FORMAT_RETRY_MAX", "2")
    stub = _StubLLM([_DUP, _DUP, _DUP])
    monkeypatch.setattr(coder, "call_messages", stub)
    with pytest.raises(CoderValidationError):
        generate_factor_code(_STORY)
    assert len(stub.calls) == 3  # 1 original + 2 retries


def test_format_retry_clamp(monkeypatch):
    monkeypatch.setenv("CODER_FORMAT_RETRY_MAX", "99")
    assert coder._coder_format_retry_max() == 3
    monkeypatch.setenv("CODER_FORMAT_RETRY_MAX", "-1")
    assert coder._coder_format_retry_max() == 0
    monkeypatch.setenv("CODER_FORMAT_RETRY_MAX", "abc")
    assert coder._coder_format_retry_max() == 0


def test_format_retry_preserves_critique_suffix(monkeypatch):
    monkeypatch.setenv("CODER_FORMAT_RETRY_MAX", "2")
    stub = _StubLLM([_DUP, _VALID])
    monkeypatch.setattr(coder, "call_messages", stub)
    code = generate_factor_code(_STORY, critique="too much drawdown")
    assert "def compute_factor" in code
    retried = stub.calls[1]["user"]
    # Both the Critic-feedback suffix AND the format-error suffix compose.
    assert "adversarial review" in retried
    assert "too much drawdown" in retried
    assert "REJECTED before backtest" in retried


def test_format_retry_budget_error_not_caught(monkeypatch):
    monkeypatch.setenv("CODER_FORMAT_RETRY_MAX", "2")
    stub = _StubLLM([LLMBudgetExceededError("cap")])
    monkeypatch.setattr(coder, "call_messages", stub)
    with pytest.raises(LLMBudgetExceededError):
        generate_factor_code(_STORY)
    assert len(stub.calls) == 1  # budget error propagates, no retry

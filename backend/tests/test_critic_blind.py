"""T1-A — Blind / anti-confirmation-bias Critic pass.

OFF (default): single story-aware call, byte-identical to before. ON: a first
mechanical pass sees ONLY code + metrics (no story); OBSERVE records agreement,
ENFORCE lets a blind No-Go veto a story-aware Go. A blind failure never
spuriously rejects; a budget breach on the blind call propagates.
"""
from __future__ import annotations

import json

import pytest

import backend.agents.critic as critic
from backend.agents._client import LLMBudgetExceededError
from backend.agents.critic import review_strategy

_STORY = "STORYSENTINEL — funding-rate mean reversion on BTC perps."
_CODE = "def compute_factor(df):\n    return df['funding_rate']  # FACTORSENTINEL"

# Metrics that clear every hard floor (Sharpe>=0.5, DD>=-0.35, PF>=1.05, trades>=20).
_PASS_METRICS = {
    "annualized_sharpe": 1.5,
    "max_drawdown": -0.10,
    "profit_factor": 1.5,
    "win_rate": 0.55,
    "annualized_return": 0.4,
    "cumulative_return": 0.6,
    "mean_hourly_return": 0.0001,
    "std_hourly_return": 0.01,
    # Augmented honesty metrics — only the BLIND whitelist should surface these.
    "probabilistic_sharpe_ratio": 0.95,
    "sortino_ratio": 2.1,
}

_FULL_GO = json.dumps({
    "verdict": "Go",
    "critique_markdown": "Mechanically sound.",
    "violation_tags": [],
    "severity_tag": "LOW OVERLAP",
    "alpha_category": "Momentum",
    "flags": [],
    "confidence": 0.8,
    "soul_questions": {},
})
_FULL_NOGO = json.dumps({
    "verdict": "No-Go",
    "critique_markdown": "Weak.",
    "violation_tags": ["thin_edge"],
    "severity_tag": "MODERATE OVERLAP",
    "alpha_category": "Other",
    "flags": [],
    "confidence": 0.4,
    "soul_questions": {},
})
_BLIND_NOGO = json.dumps({"blind_verdict": "No-Go", "blind_confidence": 0.2, "blind_reasons": ["overfit smell"]})
_BLIND_GO = json.dumps({"blind_verdict": "Go", "blind_confidence": 0.9, "blind_reasons": []})


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
    for k in ("CRITIC_BLIND_PASS", "CRITIC_BLIND_ENFORCE", "CRITIC_BLIND_MAX_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    yield


def _review(stub):
    return review_strategy(
        alpha_story=_STORY, factor_code=_CODE,
        backtest_metrics=_PASS_METRICS, trades=30,
    )


def test_blind_off_single_call_unchanged(monkeypatch):
    stub = _StubLLM([_FULL_GO])
    monkeypatch.setattr(critic, "call_messages", stub)
    out = _review(stub)
    assert len(stub.calls) == 1            # only the story-aware call
    assert out["verdict"] == "Go"
    assert out["blind_verdict"] is None
    assert out["blind_reasons"] == []
    # Existing contract keys all present.
    for k in ("critique_markdown", "violation_tags", "severity_tag",
              "alpha_category", "flags", "confidence", "soul_questions", "metrics_snapshot"):
        assert k in out


def test_blind_on_two_calls_story_hidden_from_blind(monkeypatch):
    monkeypatch.setenv("CRITIC_BLIND_PASS", "1")
    stub = _StubLLM([_BLIND_NOGO, _FULL_GO])
    monkeypatch.setattr(critic, "call_messages", stub)
    _review(stub)
    assert len(stub.calls) == 2
    blind_user, full_user = stub.calls[0]["user"], stub.calls[1]["user"]
    # The blind (first) call must see the code but NOT the story.
    assert "FACTORSENTINEL" in blind_user
    assert "STORYSENTINEL" not in blind_user
    # The story-aware (second) call sees the story.
    assert "STORYSENTINEL" in full_user


def test_blind_metrics_whitelist_visible(monkeypatch):
    monkeypatch.setenv("CRITIC_BLIND_PASS", "1")
    stub = _StubLLM([_BLIND_NOGO, _FULL_GO])
    monkeypatch.setattr(critic, "call_messages", stub)
    _review(stub)
    blind_user = stub.calls[0]["user"]
    # The honesty metrics appear only via the blind whitelist.
    assert "probabilistic_sharpe_ratio" in blind_user
    assert "sortino_ratio" in blind_user


def test_blind_observe_does_not_flip(monkeypatch):
    monkeypatch.setenv("CRITIC_BLIND_PASS", "1")  # ENFORCE unset → observe
    stub = _StubLLM([_BLIND_NOGO, _FULL_GO])
    monkeypatch.setattr(critic, "call_messages", stub)
    out = _review(stub)
    assert out["verdict"] == "Go"             # observe never flips
    assert out["blind_verdict"] == "No-Go"
    assert out["blind_agreement"] is False


def test_blind_enforce_flips_go_to_nogo(monkeypatch):
    monkeypatch.setenv("CRITIC_BLIND_PASS", "1")
    monkeypatch.setenv("CRITIC_BLIND_ENFORCE", "1")
    stub = _StubLLM([_BLIND_NOGO, _FULL_GO])
    monkeypatch.setattr(critic, "call_messages", stub)
    out = _review(stub)
    assert out["verdict"] == "No-Go"
    assert "blind_critic_veto" in out["violation_tags"]


def test_blind_go_never_rescues_full_nogo(monkeypatch):
    monkeypatch.setenv("CRITIC_BLIND_PASS", "1")
    monkeypatch.setenv("CRITIC_BLIND_ENFORCE", "1")
    stub = _StubLLM([_BLIND_GO, _FULL_NOGO])
    monkeypatch.setattr(critic, "call_messages", stub)
    out = _review(stub)
    assert out["verdict"] == "No-Go"                    # blind Go cannot rescue
    assert "blind_critic_veto" not in out["violation_tags"]


def test_blind_parse_failure_degrades_to_none(monkeypatch):
    monkeypatch.setenv("CRITIC_BLIND_PASS", "1")
    stub = _StubLLM(["not json at all", _FULL_GO])
    monkeypatch.setattr(critic, "call_messages", stub)
    out = _review(stub)
    assert out["verdict"] == "Go"          # broken blind pass doesn't sink it
    assert out["blind_verdict"] is None


def test_blind_budget_error_propagates(monkeypatch):
    monkeypatch.setenv("CRITIC_BLIND_PASS", "1")
    stub = _StubLLM([LLMBudgetExceededError("cap")])
    monkeypatch.setattr(critic, "call_messages", stub)
    with pytest.raises(LLMBudgetExceededError):
        _review(stub)


def test_blind_max_tokens_clamp(monkeypatch):
    monkeypatch.setenv("CRITIC_BLIND_MAX_TOKENS", "999999")
    assert critic._critic_blind_max_tokens() == 4000
    monkeypatch.setenv("CRITIC_BLIND_MAX_TOKENS", "-5")
    assert critic._critic_blind_max_tokens() == 128

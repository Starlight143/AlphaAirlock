"""Tests for per-agent model routing (P-MODELROUTE, backend.agents._client).

An explicit model wins; else a per-agent ``LLM_MODEL_<AGENT>`` env override; else
None (provider default). Never reads or affects the API key.
"""
from __future__ import annotations

import pytest

import backend.agents._client as C


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("LLM_MODEL_INTAKE", "LLM_MODEL_CRITIC", "LLM_MODEL_RESEARCHER",
              "LLM_MODEL_CODER", "ALPHA_LLM_DAILY_USD_CAP",
              "ALPHA_LLM_PER_STRATEGY_USD_CAP"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_model_for_agent_resolution(monkeypatch):
    assert C._model_for_agent("critic") is None
    assert C._model_for_agent("") is None
    monkeypatch.setenv("LLM_MODEL_CRITIC", "vendor/strong-model")
    assert C._model_for_agent("critic") == "vendor/strong-model"
    assert C._model_for_agent("CRITIC") == "vendor/strong-model"  # case-insensitive
    monkeypatch.setenv("LLM_MODEL_CRITIC", "   ")  # blank → None
    assert C._model_for_agent("critic") is None


class _StubProvider:
    name = "stub"

    def __init__(self, sink):
        self._sink = sink

    def call(self, *, system, user, max_tokens, temperature, model,
             response_format, images):
        self._sink["model"] = model
        return "ok"


def test_call_messages_routes_by_agent(monkeypatch):
    sink: dict = {}
    monkeypatch.setattr(C, "get_provider", lambda: _StubProvider(sink))

    # Per-agent env override is used when no explicit model is passed.
    monkeypatch.setenv("LLM_MODEL_INTAKE", "cheap/model")
    assert C.call_messages(system="s", user="u", agent="intake") == "ok"
    assert sink["model"] == "cheap/model"

    # Explicit model always wins over the env override.
    sink.clear()
    C.call_messages(system="s", user="u", model="explicit/x", agent="intake")
    assert sink["model"] == "explicit/x"

    # No explicit model + no env ⇒ None ⇒ provider default (unchanged behavior).
    monkeypatch.delenv("LLM_MODEL_INTAKE", raising=False)
    sink.clear()
    C.call_messages(system="s", user="u", agent="intake")
    assert sink["model"] is None

    # An agent with no override is unaffected even if a different agent has one.
    monkeypatch.setenv("LLM_MODEL_CRITIC", "vendor/strong")
    sink.clear()
    C.call_messages(system="s", user="u", agent="coder")
    assert sink["model"] is None

"""Tests for P-CACHE — the prompt-cache system-prompt shaping in agents/_client.

The agent factory re-uses the same per-agent system prompt across every strategy,
critic-revise retry and evolution child, so marking it as an Anthropic prompt-cache
breakpoint (``cache_control: ephemeral``) lets repeat calls re-read it at ~0.1×
input cost. These lock down ``_system_param_for`` (the pure shaper both providers
call) WITHOUT constructing a provider or needing an API key:

  * enabled + non-empty  → single multipart text block carrying the breakpoint;
  * disabled / empty     → byte-identical legacy plain string;
  * OpenRouter (require_claude) only caches Claude models; native Anthropic always.
"""
from __future__ import annotations

import pytest

import backend.agents._client as cl


def _is_cached_block(val, system: str) -> bool:
    """True iff val is the multipart-with-breakpoint shape for `system`."""
    return (
        isinstance(val, list)
        and len(val) == 1
        and val[0].get("type") == "text"
        and val[0].get("text") == system
        and val[0].get("cache_control") == {"type": "ephemeral"}
    )


def test_default_on_wraps_system_block(monkeypatch):
    monkeypatch.delenv("LLM_PROMPT_CACHE", raising=False)  # default ON
    sys = "You are the Researcher. " * 50
    out = cl._system_param_for(sys, "claude-3-5-sonnet-latest", require_claude=False)
    assert _is_cached_block(out, sys)


def test_kill_switch_restores_plain_string(monkeypatch):
    monkeypatch.setenv("LLM_PROMPT_CACHE", "0")
    sys = "system prompt"
    assert cl._system_param_for(sys, "claude-3-5-sonnet-latest", require_claude=False) == sys
    assert cl._system_param_for(sys, "anthropic/claude-sonnet-4.6", require_claude=True) == sys


def test_empty_system_never_wrapped(monkeypatch):
    # An empty cached text block would be rejected by the API → must stay a string.
    monkeypatch.delenv("LLM_PROMPT_CACHE", raising=False)
    assert cl._system_param_for("", "claude-3-5-sonnet-latest", require_claude=False) == ""


def test_openrouter_only_caches_claude_models(monkeypatch):
    monkeypatch.delenv("LLM_PROMPT_CACHE", raising=False)  # default ON
    sys = "system prompt body"
    # Claude via OpenRouter → cached; non-Claude (e.g. MiniMax) → plain string so a
    # provider that dislikes multipart system content is never surprised.
    assert _is_cached_block(
        cl._system_param_for(sys, "anthropic/claude-sonnet-4.6", require_claude=True), sys)
    assert _is_cached_block(
        cl._system_param_for(sys, "anthropic/claude-3.5-haiku", require_claude=True), sys)
    assert cl._system_param_for(sys, "minimax/minimax-m1", require_claude=True) == sys
    assert cl._system_param_for(sys, "openai/gpt-4o", require_claude=True) == sys


def test_anthropic_native_ignores_model_gate(monkeypatch):
    # The native Anthropic SDK path is always a Claude model, so require_claude is
    # False and even an oddly-named model still gets the cache breakpoint.
    monkeypatch.delenv("LLM_PROMPT_CACHE", raising=False)
    sys = "system"
    assert _is_cached_block(
        cl._system_param_for(sys, "whatever-model", require_claude=False), sys)

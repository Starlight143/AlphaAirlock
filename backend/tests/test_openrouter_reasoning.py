"""Regression tests for MiniMax (reasoning-model) handling in agents/_client.py.

Two guarantees this locks in:

1. ``max_tokens`` is OMITTED from the OpenRouter request by default (uncapped),
   so a reasoning model's hidden tokens never truncate the visible answer to
   empty (``finish_reason='length'`` — the original ``coder.failed`` error).
   ``OPENROUTER_MAX_TOKENS`` > 0 re-imposes a ceiling.

2. Reasoning is NOT suppressed at the request (no ``reasoning.exclude`` — which
   on MiniMax can blank the answer and trip the empty-content guard, a false
   "no response"). The model keeps reasoning fully; the EXTRACTOR strips any
   inline ``<think>…</think>`` so chain-of-thought is never treated as output.
"""
from __future__ import annotations

import httpx
import pytest

import backend.agents._client as cl


# --------------------------------------------------------------------------- #
# _strip_reasoning — extraction-side separation of chain-of-thought           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "src,expected",
    [
        # Well-formed paired block is removed; the code answer survives.
        (
            "<think>compute momentum</think>\ndef compute_factor(df):\n    return df['c']",
            "def compute_factor(df):\n    return df['c']",
        ),
        # Reasoning then a bare close tag, no opening tag → keep only the tail.
        ("hidden reasoning...\n</think>\nFINAL ANSWER", "FINAL ANSWER"),
        ("<thinking>x</thinking>answer", "answer"),
        ("<reasoning>y</reasoning>\nresult", "result"),
        ("< think >spaced</ think > visible", "visible"),
        # No markup → unchanged.
        ("def compute_factor(df):\n    return df['x']", "def compute_factor(df):\n    return df['x']"),
        # A real answer may contain bare angle brackets — must NOT be touched.
        ("a < b and c > d", "a < b and c > d"),
        ("", ""),
    ],
)
def test_strip_reasoning(src, expected):
    assert cl._strip_reasoning(src) == expected


def test_strip_reasoning_multiple_blocks():
    assert cl._strip_reasoning("<think>one</think>keep1<think>two</think>keep2") == "keep1keep2"


# --------------------------------------------------------------------------- #
# _openrouter_max_tokens — default uncapped, env re-imposes a ceiling          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "env,expected",
    [(None, None), ("0", None), ("", None), ("8000", 8000), ("-5", None), ("garbage", None)],
)
def test_openrouter_max_tokens(monkeypatch, env, expected):
    if env is None:
        monkeypatch.delenv("OPENROUTER_MAX_TOKENS", raising=False)
    else:
        monkeypatch.setenv("OPENROUTER_MAX_TOKENS", env)
    assert cl._openrouter_max_tokens() == expected


# --------------------------------------------------------------------------- #
# Body construction + extraction, end-to-end with a stubbed httpx client       #
# --------------------------------------------------------------------------- #


class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200
        self.request = httpx.Request(
            "POST", "https://openrouter.ai/api/v1/chat/completions"
        )

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, payload: dict):
        self.captured_body: dict | None = None
        self._payload = payload

    def post(self, url, headers=None, json=None):  # noqa: A002 — mirrors httpx API
        self.captured_body = json
        return _FakeResp(self._payload)


def _provider_with_fake(monkeypatch, payload: dict):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-testkey0000")
    monkeypatch.setenv("OPENROUTER_MODEL", "minimax/minimax-m3")
    prov = cl.OpenRouterProvider()
    fake = _FakeClient(payload)
    prov._client = fake  # type: ignore[assignment]
    return prov, fake


_OK_PAYLOAD = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}


def test_body_omits_max_tokens_and_reasoning_by_default(monkeypatch):
    monkeypatch.delenv("OPENROUTER_MAX_TOKENS", raising=False)
    prov, fake = _provider_with_fake(monkeypatch, _OK_PAYLOAD)
    out = prov.call(
        system="s", user="u", max_tokens=1800, temperature=0.15,
        model=None, response_format=None,
    )
    assert out == "ok"
    # Uncapped by default → max_tokens absent so reasoning can't starve the answer.
    assert "max_tokens" not in fake.captured_body
    # Reasoning is NOT suppressed at the request (no false "no response").
    assert "reasoning" not in fake.captured_body


def test_body_includes_cap_when_env_set(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MAX_TOKENS", "5000")
    prov, fake = _provider_with_fake(monkeypatch, _OK_PAYLOAD)
    prov.call(
        system="s", user="u", max_tokens=1800, temperature=0.15,
        model=None, response_format=None,
    )
    assert fake.captured_body["max_tokens"] == 5000


def test_inline_reasoning_stripped_from_answer(monkeypatch):
    monkeypatch.delenv("OPENROUTER_MAX_TOKENS", raising=False)
    payload = {
        "choices": [
            {
                "message": {
                    "content": "<think>let me think hard about this</think>\n"
                    "def compute_factor(df):\n    return df['close']"
                },
                "finish_reason": "stop",
            }
        ]
    }
    prov, _ = _provider_with_fake(monkeypatch, payload)
    out = prov.call(
        system="s", user="u", max_tokens=1800, temperature=0.15,
        model=None, response_format=None,
    )
    assert out == "def compute_factor(df):\n    return df['close']"
    assert "<think>" not in out
    assert "think hard" not in out


def test_separate_reasoning_field_is_never_read(monkeypatch):
    # OpenRouter puts reasoning in a SEPARATE message.reasoning field; the
    # extractor reads only message.content, so reasoning never leaks as output.
    monkeypatch.delenv("OPENROUTER_MAX_TOKENS", raising=False)
    payload = {
        "choices": [
            {
                "message": {
                    "content": "the answer",
                    "reasoning": "secret chain of thought that must not surface",
                },
                "finish_reason": "stop",
            }
        ]
    }
    prov, _ = _provider_with_fake(monkeypatch, payload)
    out = prov.call(
        system="s", user="u", max_tokens=1800, temperature=0.15,
        model=None, response_format=None,
    )
    assert out == "the answer"
    assert "secret" not in out

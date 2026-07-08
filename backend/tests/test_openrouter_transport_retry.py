"""Regression tests for transport-error retry in agents/_client.py.

The bug these lock down (the "Mac mini loses OpenRouter after the screen
locks" report): on the OpenRouter path the HTTP client is raw ``httpx``, so a
socket that died during a macOS sleep/wake raises ``httpx.ConnectError`` /
``httpx.RemoteProtocolError`` **directly** — not wrapped in an Anthropic SDK
class. ``_retry_call``'s transient-class allowlist originally listed only
Anthropic class names, so those httpx transport errors fell through as
"non-retryable" and the whole agent call hard-failed instead of reconnecting.

These tests assert that:
  1. every transient httpx transport error is retried and self-heals once the
     network returns, and
  2. a genuine client-side protocol bug (``LocalProtocolError``) is NOT retried
     — it must still fail fast.
"""
from __future__ import annotations

import httpx
import pytest

import backend.agents._client as cl


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch):
    """Make retry backoff instant + deterministic so the suite is fast and
    never flaky on a slow CI runner."""
    monkeypatch.setattr(cl._retry_time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(cl._retry_random, "uniform", lambda *_a, **_k: 0.0)


_TRANSIENT_TRANSPORT_ERRORS = [
    httpx.ConnectError("connection refused"),
    httpx.ConnectTimeout("connect timed out"),
    httpx.ReadError("server disconnected without sending a response"),
    httpx.ReadTimeout("read timed out"),
    httpx.WriteError("broken pipe"),
    httpx.PoolTimeout("pool timed out"),
    httpx.RemoteProtocolError("Server disconnected without sending a response"),
]


@pytest.mark.parametrize(
    "exc", _TRANSIENT_TRANSPORT_ERRORS, ids=lambda e: type(e).__name__
)
def test_transport_error_is_retried_then_succeeds(exc):
    # Fails twice (the dead post-wake sockets), succeeds on the third attempt
    # once the network is back — exactly the sleep/wake recovery path.
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise exc
        return "ok"

    assert cl._retry_call(fn, max_attempts=3) == "ok"
    assert calls["n"] == 3


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_transport_error_exhausts_then_reraises(exc):
    # When the network never comes back, all attempts are used and the original
    # transport error is re-raised (no silent swallow) — the caller still sees a
    # clean terminal failure after the retries.
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise exc

    with pytest.raises(type(exc)):
        cl._retry_call(fn, max_attempts=3)
    assert calls["n"] == 3


def test_local_protocol_error_is_not_retried():
    # A client-side protocol bug is NOT a transient network blip — it must fail
    # on the first attempt, never looping.
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise httpx.LocalProtocolError("illegal header value")

    with pytest.raises(httpx.LocalProtocolError):
        cl._retry_call(fn, max_attempts=3)
    assert calls["n"] == 1


def test_auth_error_status_is_not_retried():
    # A 401 carried on an httpx.HTTPStatusError is a hard auth failure (bad
    # key), not a transient transport blip — fail fast, do not burn retries.
    calls = {"n": 0}
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(401, request=request)

    def fn():
        calls["n"] += 1
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    with pytest.raises(httpx.HTTPStatusError):
        cl._retry_call(fn, max_attempts=3)
    assert calls["n"] == 1

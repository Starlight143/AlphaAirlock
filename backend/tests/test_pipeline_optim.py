"""Tests for three pipeline optimizations:
  * B  — resizable concurrency limiter (backend.core.auto_pipeline)
  * C3 — learning loop: recent failures injected into story gen (researcher)
  * C2 — out-of-sample metrics (orchestrator._oos_metrics)
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

import backend.core.auto_pipeline as AP
import backend.agents.researcher as R
from backend.core.orchestrator import WorkflowOrchestrator


# --------------------------------------------------------------------------- #
# B — resizable concurrency limiter                                            #
# --------------------------------------------------------------------------- #

def test_limiter_default_single_flight():
    lim = AP._ResizableLimiter(lambda: 1)
    assert lim.acquire(blocking=False) is True
    assert lim.acquire(blocking=False) is False   # full at 1 (single-flight)
    lim.release()
    assert lim.acquire(blocking=False) is True
    lim.release()


def test_limiter_higher_limit():
    lim = AP._ResizableLimiter(lambda: 2)
    assert lim.acquire(blocking=False) is True
    assert lim.acquire(blocking=False) is True
    assert lim.acquire(blocking=False) is False
    lim.release()
    lim.release()


def test_limiter_resizes_live_without_restart():
    cur = {"n": 1}
    lim = AP._ResizableLimiter(lambda: cur["n"])
    assert lim.acquire(blocking=False) is True
    assert lim.acquire(blocking=False) is False   # full at 1
    cur["n"] = 2                                   # operator bumps the env, no restart
    assert lim.acquire(blocking=False) is True     # new slot available immediately
    lim.release()
    lim.release()


def test_limiter_timeout_returns_false():
    lim = AP._ResizableLimiter(lambda: 1)
    assert lim.acquire(blocking=False) is True
    t0 = time.monotonic()
    assert lim.acquire(blocking=True, timeout=0.05) is False
    assert time.monotonic() - t0 >= 0.04
    lim.release()


# --------------------------------------------------------------------------- #
# C3 — learning loop                                                           #
# --------------------------------------------------------------------------- #

class _Node:
    def __init__(self, title, content):
        self.title = title
        self.content = content


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):
        return _FakeQuery(list(self._rows))


def test_learning_loop_formats_recent_failures(monkeypatch):
    for k in ("RESEARCHER_LEARN_FROM_FAILURES", "RESEARCHER_FAILURE_CONTEXT_K"):
        monkeypatch.delenv(k, raising=False)
    s = _FakeSession([
        _Node("Funding momentum dud", "Sharpe -2.1, overfit to early 2024"),
        _Node("OI breakout", "zero trades — signal never fired"),
    ])
    ctx = R._recent_failure_context(s)
    assert "Funding momentum dud" in ctx
    assert "OI breakout" in ctx
    assert len([ln for ln in ctx.split("\n") if ln.strip()]) == 2


def test_learning_loop_can_be_disabled(monkeypatch):
    monkeypatch.setenv("RESEARCHER_LEARN_FROM_FAILURES", "0")
    assert R._recent_failure_context(_FakeSession([_Node("x", "y")])) == ""


def test_learning_loop_k_zero(monkeypatch):
    monkeypatch.delenv("RESEARCHER_LEARN_FROM_FAILURES", raising=False)
    monkeypatch.setenv("RESEARCHER_FAILURE_CONTEXT_K", "0")
    assert R._recent_failure_context(_FakeSession([_Node("x", "y")])) == ""


def test_learning_loop_truncates_long_content(monkeypatch):
    for k in ("RESEARCHER_LEARN_FROM_FAILURES", "RESEARCHER_FAILURE_CONTEXT_K"):
        monkeypatch.delenv(k, raising=False)
    s = _FakeSession([_Node("T" * 500, "C" * 5000)])
    ctx = R._recent_failure_context(s)
    # Bounded: title ≤120, snippet ≤240 (+ "- " + ": ") ⇒ well under 400 chars.
    assert len(ctx) < 400


# --------------------------------------------------------------------------- #
# C2 — out-of-sample metrics                                                   #
# --------------------------------------------------------------------------- #

def _synth_df(n: int = 300) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    rng = np.random.default_rng(0)
    close = 100.0 + np.cumsum(rng.normal(0.0, 0.3, n))
    close = np.maximum(close, 1.0)
    df = pd.DataFrame({
        "open": close, "high": close * 1.002, "low": close * 0.998,
        "close": close, "volume": 10.0,
        "open_interest": 0.0, "funding_rate": 0.0, "liquidations": 0.0,
    }, index=idx)
    df.index.name = "timestamp"
    return df


def test_oos_metrics_attached(monkeypatch):
    monkeypatch.delenv("STRATEGY_OOS_FRACTION", raising=False)
    orch = WorkflowOrchestrator()
    df = _synth_df(300)
    sig = pd.Series(np.sign(df["close"].pct_change().fillna(0.0)).to_numpy(),
                    index=df.index)
    oos = orch._oos_metrics(df, sig)
    assert oos.get("oos_fraction") == 0.3
    assert "oos_num_trades" in oos
    assert "oos_annualized_sharpe" in oos


def test_oos_disabled_when_fraction_zero(monkeypatch):
    monkeypatch.setenv("STRATEGY_OOS_FRACTION", "0")
    orch = WorkflowOrchestrator()
    df = _synth_df(300)
    sig = pd.Series([1.0] * len(df), index=df.index)
    assert orch._oos_metrics(df, sig) == {}


def test_oos_skipped_when_too_short(monkeypatch):
    monkeypatch.delenv("STRATEGY_OOS_FRACTION", raising=False)
    orch = WorkflowOrchestrator()
    df = _synth_df(50)  # < 120-bar minimum
    sig = pd.Series([1.0] * len(df), index=df.index)
    assert orch._oos_metrics(df, sig) == {}

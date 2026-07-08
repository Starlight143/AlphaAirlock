"""Regression tests for the ingest scheduler's write-concurrency control.

Locks in the fix for the intermittent ``database is locked [SQL: UPDATE
ingest_sources ...]`` thread-crashes seen in the Mission Panel incidents feed:

  1. Per-tick dispatch is now bounded by an ``asyncio.Semaphore`` so simultaneous
     SQLite writers can't pile up on the single WAL writer lock (at startup
     EVERY source is due, which used to fan out into dozens of writer threads).
  2. The short ``ingest_sources`` write transactions retry the transient
     ``SQLITE_BUSY`` / ``SQLITE_BUSY_SNAPSHOT`` "database is locked"
     OperationalError (which ``busy_timeout`` does NOT cover for snapshot
     upgrades) instead of crashing the worker thread.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

import backend.core.scheduler as scheduler


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _locked_error() -> OperationalError:
    """An OperationalError shaped like pysqlite's real 'database is locked'
    (the human message lives on ``.orig``)."""
    return OperationalError(
        "UPDATE ingest_sources SET last_polled_at=? WHERE id=?",
        {},
        Exception("database is locked"),
    )


# --------------------------------------------------------------------------- #
# _is_sqlite_lock_error — only transient lock contention is retryable          #
# --------------------------------------------------------------------------- #


def test_is_sqlite_lock_error_classification():
    assert scheduler._is_sqlite_lock_error(_locked_error()) is True
    assert scheduler._is_sqlite_lock_error(
        OperationalError("x", {}, Exception("database table is locked"))
    ) is True
    # Non-lock OperationalError (e.g. schema problem) is NOT retryable.
    assert scheduler._is_sqlite_lock_error(
        OperationalError("x", {}, Exception("no such table: foo"))
    ) is False
    # A different exception family is never a lock error.
    assert scheduler._is_sqlite_lock_error(
        IntegrityError("x", {}, Exception("UNIQUE constraint failed"))
    ) is False
    assert scheduler._is_sqlite_lock_error(ValueError("nope")) is False


# --------------------------------------------------------------------------- #
# _retry_on_locked — re-run the short txn on transient locks only              #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch):
    # Make backoff instant + deterministic so the suite is fast and never flaky.
    monkeypatch.setattr(scheduler.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(scheduler.random, "uniform", lambda *_a, **_k: 0.0)


def test_retry_on_locked_succeeds_after_transient_lock(monkeypatch):
    monkeypatch.setenv("ALPHA_SQLITE_LOCK_RETRIES", "5")
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _locked_error()
        return "committed"

    assert scheduler._retry_on_locked(fn, what="t") == "committed"
    assert calls["n"] == 3  # failed twice, then committed


def test_retry_on_locked_exhausts_then_reraises(monkeypatch):
    monkeypatch.setenv("ALPHA_SQLITE_LOCK_RETRIES", "3")
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _locked_error()

    with pytest.raises(OperationalError):
        scheduler._retry_on_locked(fn, what="t")
    assert calls["n"] == 3  # all attempts used, original error surfaced


def test_retry_on_locked_does_not_retry_integrity_error():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise IntegrityError("x", {}, Exception("UNIQUE constraint failed"))

    with pytest.raises(IntegrityError):
        scheduler._retry_on_locked(fn, what="t")
    assert calls["n"] == 1  # NOT a lock — must fail fast


def test_retry_on_locked_does_not_retry_non_lock_operational_error():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise OperationalError("x", {}, Exception("no such table: foo"))

    with pytest.raises(OperationalError):
        scheduler._retry_on_locked(fn, what="t")
    assert calls["n"] == 1  # genuine schema error — must fail fast


# --------------------------------------------------------------------------- #
# Concurrency cap — env clamping + actual dispatch bound                       #
# --------------------------------------------------------------------------- #


def test_max_concurrent_polls_clamped(monkeypatch):
    monkeypatch.setenv("ALPHA_INGEST_MAX_CONCURRENCY", "999")
    assert scheduler.max_concurrent_polls() == 16  # clamped to ceiling
    monkeypatch.setenv("ALPHA_INGEST_MAX_CONCURRENCY", "0")
    assert scheduler.max_concurrent_polls() == 1  # clamped to floor
    monkeypatch.setenv("ALPHA_INGEST_MAX_CONCURRENCY", "garbage")
    assert scheduler.max_concurrent_polls() == 4  # bad value -> default
    monkeypatch.delenv("ALPHA_INGEST_MAX_CONCURRENCY", raising=False)
    assert scheduler.max_concurrent_polls() == 4  # unset -> default


def test_scan_once_bounds_concurrency(monkeypatch):
    # 30 sources all due at once (the startup thundering-herd shape). With the
    # cap at 4, no more than 4 _poll_one coroutines may run concurrently.
    monkeypatch.setenv("ALPHA_INGEST_MAX_CONCURRENCY", "4")
    due = list(range(30))
    monkeypatch.setattr(scheduler, "_due_source_ids", lambda: due)
    monkeypatch.setattr(scheduler, "_live_source_ids", lambda: set(due))
    scheduler._POLL_LOCKS.clear()

    state = {"cur": 0, "max": 0, "done": 0}

    async def _fake_poll(sid: int) -> None:
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        await asyncio.sleep(0.005)  # hold the slot so overlap is observable
        state["cur"] -= 1
        state["done"] += 1

    monkeypatch.setattr(scheduler, "_poll_one", _fake_poll)

    asyncio.run(scheduler._scan_once())

    assert state["done"] == 30  # every source still polled
    assert 1 <= state["max"] <= 4  # but never more than the cap at once

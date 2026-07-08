"""Unit tests for backend/core/llm_budget.py.

Covers finding #32: reservation/settle TOCTOU fix, subnormal-cap guard,
daily cap enforcement, cross-day rollover, atomic persist, and is_enabled().

All tests are hermetic: no network, no real LLM calls.  Storage is redirected
to a pytest tmp_path via a _state_path patch so the production storage/
directory is never touched.

IMPORTANT: importlib.reload() is used in selected tests to reset module-level
globals (_RESERVATIONS, _STATE_CACHE, _RES_COUNTER).  Every test that reloads
must also patch _state_path immediately after reload before any call that
touches the disk.
"""
from __future__ import annotations

import importlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

# conftest.py already put the repo root on sys.path.
import backend.core.llm_budget as llm_budget_module


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _reload(tmp_path: Path) -> Any:
    """Reload the module and immediately patch _state_path to use tmp_path.

    Returns the fresh module reference.  The _state_path patch is applied via
    monkeypatching the module attribute so that _persist_state and _load_state
    both use the temp directory — no real storage/ file is created.
    """
    m = importlib.reload(llm_budget_module)
    # Redirect disk I/O to the test's tmp dir.
    def _fake_state_path(day_iso: str) -> Path:
        return tmp_path / f".llm_budget_{day_iso.replace('-', '')}.json"
    m._state_path = _fake_state_path
    # Also clear the in-memory cache so _ensure_today_state re-reads from the
    # patched path rather than from a stale cache left by a previous test.
    m._STATE_CACHE.clear()
    return m


# ---------------------------------------------------------------------------
# 1. is_enabled() — cap=0 disables enforcement
# ---------------------------------------------------------------------------

def test_is_enabled_false_when_cap_zero(tmp_path):
    with patch.dict(os.environ, {"ALPHA_LLM_DAILY_USD_CAP": "0"}):
        m = _reload(tmp_path)
        assert m.is_enabled() is False


def test_is_enabled_true_when_cap_positive(tmp_path):
    with patch.dict(os.environ, {"ALPHA_LLM_DAILY_USD_CAP": "1.0"}):
        m = _reload(tmp_path)
        assert m.is_enabled() is True


# ---------------------------------------------------------------------------
# 2. check_budget no-ops when cap is zero (legacy disabled path)
# ---------------------------------------------------------------------------

def test_check_budget_noop_when_cap_zero(tmp_path):
    with patch.dict(os.environ, {"ALPHA_LLM_DAILY_USD_CAP": "0"}):
        m = _reload(tmp_path)
        # Must not raise even for absurdly large inputs.
        m.check_budget(10_000_000, max_output_tokens=100_000, agent="test")


# ---------------------------------------------------------------------------
# 3. Subnormal cap is treated as disabled (finding #32 critical path)
#
# 5e-324 is the IEEE 754 minimum positive subnormal.  `5e-324 > 0.0` is True
# so a naive `cap > 0.0` check would enable enforcement with a nonsensical cap.
# The module must use `cap > 1e-14` (subnormal-safe guard) internally.
# ---------------------------------------------------------------------------

def test_subnormal_cap_check_budget_does_not_raise(tmp_path):
    """check_budget must NOT raise for any normal usage when cap is subnormal."""
    subnormal = 5e-324  # IEEE 754 minimum positive subnormal; passes `> 0.0`
    with patch.dict(os.environ, {"ALPHA_LLM_DAILY_USD_CAP": str(subnormal)}):
        m = _reload(tmp_path)
        try:
            m.check_budget(1_000, max_output_tokens=100, agent="test")
        except m.LLMBudgetExceededError:
            pytest.fail(
                "check_budget raised LLMBudgetExceededError for subnormal cap "
                "— the subnormal-safe guard `not (cap > 1e-14)` is broken"
            )


def test_subnormal_cap_current_state_shows_disabled(tmp_path):
    """current_state() must report enabled=False for a subnormal cap."""
    subnormal = 5e-324
    with patch.dict(os.environ, {"ALPHA_LLM_DAILY_USD_CAP": str(subnormal)}):
        m = _reload(tmp_path)
        state = m.current_state()
        assert state["enabled"] is False
        assert state["cap_usd"] is None
        assert state["remaining_usd"] is None


def test_subnormal_cap_reserve_budget_returns_none(tmp_path):
    """reserve_budget must return None (disabled) for a subnormal cap. Both the
    daily AND per-strategy caps must be off for the disabled path (a live .env
    per-strategy cap must not leak in)."""
    subnormal = 5e-324
    with patch.dict(os.environ, {"ALPHA_LLM_DAILY_USD_CAP": str(subnormal),
                                 "ALPHA_LLM_PER_STRATEGY_USD_CAP": "0"}):
        m = _reload(tmp_path)
        token = m.reserve_budget(50_000, est_output_chars=0, agent="test")
        assert token is None


def test_subnormal_cap_is_enabled_returns_false(tmp_path):
    """is_enabled() must return False for a subnormal cap (B14-4 regression guard).

    is_enabled() uses `_cap_usd() > 0.0` internally.  For the IEEE 754 minimum
    positive subnormal (5e-324), `5e-324 > 0.0` evaluates to True in CPython,
    so a naive `> 0.0` check would return True — contradicting current_state()
    which correctly reports enabled=False via the subnormal-safe `> 1e-14` guard.
    This test pins the expected behaviour so that inconsistency cannot silently
    regress.
    """
    subnormal = 5e-324  # IEEE 754 minimum positive subnormal; `> 0.0` is True
    with patch.dict(os.environ, {"ALPHA_LLM_DAILY_USD_CAP": str(subnormal)}):
        m = _reload(tmp_path)
        assert m.is_enabled() is False, (
            f"is_enabled() returned True for subnormal cap {subnormal!r} — "
            "B14-1/B14-4 regression: is_enabled() must use `> 1e-14`, not `> 0.0`"
        )
        # Consistency check: is_enabled() must agree with current_state()['enabled'].
        state = m.current_state()
        assert state["enabled"] is False, (
            "current_state()['enabled'] disagrees with is_enabled() for subnormal cap"
        )


# ---------------------------------------------------------------------------
# 4. check_budget raises LLMBudgetExceededError when projected spend >= cap
# ---------------------------------------------------------------------------

def test_check_budget_raises_at_cap(tmp_path):
    with patch.dict(os.environ, {
        "ALPHA_LLM_DAILY_USD_CAP": "0.001",
        "ALPHA_LLM_INPUT_PRICE_PER_1M": "3.0",
        "ALPHA_LLM_OUTPUT_PRICE_PER_1M": "15.0",
    }):
        m = _reload(tmp_path)
        # 350k chars * (3.0/1e6) / 3.5 chars-per-token ≈ $0.0003 per call.
        # After recording 350k chars, the spent total (~$0.0003) is below cap;
        # but adding another 10k chars projection crosses the 0.001 threshold.
        # Use a big enough block to guarantee cap is crossed.
        m.record_usage(input_chars=1_200_000, output_chars=0, agent="test")
        with pytest.raises(m.LLMBudgetExceededError):
            m.check_budget(10_000, max_output_tokens=0, agent="test")


def test_check_budget_does_not_raise_below_cap(tmp_path):
    with patch.dict(os.environ, {
        "ALPHA_LLM_DAILY_USD_CAP": "1.0",
        "ALPHA_LLM_INPUT_PRICE_PER_1M": "3.0",
        "ALPHA_LLM_OUTPUT_PRICE_PER_1M": "15.0",
    }):
        m = _reload(tmp_path)
        # Tiny usage, cap is $1.0 — should never raise.
        m.record_usage(input_chars=1_000, output_chars=0, agent="test")
        m.check_budget(1_000, max_output_tokens=0, agent="test")  # no exception expected


# ---------------------------------------------------------------------------
# 5. reserve_budget blocks a second call that would push total over cap
#    This is the TOCTOU fix: both calls happen before either settles.
# ---------------------------------------------------------------------------

def test_reservation_blocks_concurrent_over_cap(tmp_path):
    """First reserve consumes most of the cap; second must raise before settle.

    Math: _chars_to_usd(X, 0) = (X / chars_per_token / 1_000_000) * price_per_1m.
    With chars_per_token=3.5 and input_price=$3.0/1M:
      _chars_to_usd(X, 0) = X * 3.0 / (3.5 * 1_000_000)
    For cap=$0.01:
      max chars = 0.01 * 3.5 * 1_000_000 / 3.0 ≈ 11_667 chars
    First call: 9_900 chars -> ~$0.00849 (under cap).
    Second call: 2_000 chars -> ~$0.00171; total ~$0.01020 >= cap -> must raise.
    """
    with patch.dict(os.environ, {
        "ALPHA_LLM_DAILY_USD_CAP": "0.01",
        "ALPHA_LLM_INPUT_PRICE_PER_1M": "3.0",
        "ALPHA_LLM_OUTPUT_PRICE_PER_1M": "15.0",
    }):
        m = _reload(tmp_path)
        # First reservation: 9_900 chars ≈ $0.00849, under the $0.01 cap.
        token = m.reserve_budget(9_900, est_output_chars=0, agent="agent-A")
        assert token is not None
        # Second reservation: 2_000 chars ≈ $0.00171; total ~$0.01020 >= cap → must raise.
        with pytest.raises(m.LLMBudgetExceededError):
            m.reserve_budget(2_000, est_output_chars=0, agent="agent-B")
        # Clean up: settle the first reservation.
        m.settle_reservation(token, actual_input_chars=9_900, actual_output_chars=0)


# ---------------------------------------------------------------------------
# 6. settle_reservation removes the reservation token from _RESERVATIONS
# ---------------------------------------------------------------------------

def test_settle_clears_reservation(tmp_path):
    with patch.dict(os.environ, {
        "ALPHA_LLM_DAILY_USD_CAP": "1.0",
        "ALPHA_LLM_INPUT_PRICE_PER_1M": "3.0",
        "ALPHA_LLM_OUTPUT_PRICE_PER_1M": "15.0",
    }):
        m = _reload(tmp_path)
        token = m.reserve_budget(1_000, est_output_chars=0, agent="test")
        assert token is not None
        assert token in m._RESERVATIONS
        m.settle_reservation(token, actual_input_chars=1_000, actual_output_chars=0)
        assert token not in m._RESERVATIONS


# ---------------------------------------------------------------------------
# 7. Leaked reservation (settle never called) keeps in-flight cost visible —
#    it acts as a ceiling, not a floor.
# ---------------------------------------------------------------------------

def test_leaked_reservation_blocks_future_calls(tmp_path):
    """A reservation whose settle is never called must still block future checks.

    Math: cap=$0.01; 9_900 chars ≈ $0.00849 reserved but not settled.
    A follow-up check_budget for 2_000 chars → total ~$0.01020 >= cap → raises.
    """
    with patch.dict(os.environ, {
        "ALPHA_LLM_DAILY_USD_CAP": "0.01",
        "ALPHA_LLM_INPUT_PRICE_PER_1M": "3.0",
        "ALPHA_LLM_OUTPUT_PRICE_PER_1M": "15.0",
    }):
        m = _reload(tmp_path)
        # Reserve most of the cap — do NOT settle (simulates a crash/leak).
        _leaked_token = m.reserve_budget(9_900, est_output_chars=0, agent="leaker")
        assert _leaked_token is not None
        # The in-flight reservation must make the next check_budget raise.
        with pytest.raises(m.LLMBudgetExceededError):
            m.check_budget(2_000, max_output_tokens=0, agent="newcomer")
        # Clean up (module is reloaded per test anyway; this keeps the test tidy).
        m.settle_reservation(_leaked_token, actual_input_chars=0, actual_output_chars=0)


# ---------------------------------------------------------------------------
# 8. settle_reservation with token=None is a no-op (cap was disabled at reserve)
# ---------------------------------------------------------------------------

def test_settle_none_token_records_usage(tmp_path):
    """settle_reservation(token=None, ...) must not raise AND must record usage.

    Despite token=None (cap was disabled at reserve time), settle_reservation
    always calls _ensure_today_state() and increments the usage counters.
    This is intentional: the usage ledger stays accurate even when the cap guard
    is disabled. See also test_reserve_and_settle_noop_when_cap_disabled (test 15).
    """
    with patch.dict(os.environ, {"ALPHA_LLM_DAILY_USD_CAP": "0"}):
        m = _reload(tmp_path)
        # Should not raise.
        m.settle_reservation(None, actual_input_chars=1_000, actual_output_chars=500)
        # Usage must be recorded even when cap is disabled (by design).
        state = m.current_state()
        assert state["calls"] == 1
        assert state["input_chars"] == 1_000
        assert state["output_chars"] == 500


# ---------------------------------------------------------------------------
# 9. settle_reservation records actual chars in the state counter
# ---------------------------------------------------------------------------

def test_settle_records_actual_usage(tmp_path):
    with patch.dict(os.environ, {
        "ALPHA_LLM_DAILY_USD_CAP": "1.0",
        "ALPHA_LLM_INPUT_PRICE_PER_1M": "3.0",
        "ALPHA_LLM_OUTPUT_PRICE_PER_1M": "15.0",
    }):
        m = _reload(tmp_path)
        token = m.reserve_budget(1_000, est_output_chars=0, agent="test")
        m.settle_reservation(token, actual_input_chars=2_500, actual_output_chars=300)
        state = m.current_state()
        assert state["input_chars"] == 2_500
        assert state["output_chars"] == 300
        assert state["calls"] == 1


# ---------------------------------------------------------------------------
# 10. Cross-day rollover resets the counter
# ---------------------------------------------------------------------------

def test_cross_day_rollover(tmp_path):
    with patch.dict(os.environ, {
        "ALPHA_LLM_DAILY_USD_CAP": "1.0",
        "ALPHA_LLM_INPUT_PRICE_PER_1M": "3.0",
        "ALPHA_LLM_OUTPUT_PRICE_PER_1M": "15.0",
    }):
        m = _reload(tmp_path)
        # Record usage for "yesterday".
        with patch.object(m, "_today_iso", return_value="2024-01-01"):
            m.record_usage(input_chars=100_000, output_chars=10_000, agent="test")

        # Verify yesterday's state was non-zero.
        with patch.object(m, "_today_iso", return_value="2024-01-01"):
            # Clear the in-memory cache so _ensure_today_state re-reads the file.
            m._STATE_CACHE.clear()
            snapshot = m.current_state()
        assert snapshot["input_chars"] == 100_000

        # Advance to "today" — rollover must reset to zero.
        m._STATE_CACHE.clear()
        with patch.object(m, "_today_iso", return_value="2024-01-02"):
            state_today = m.current_state()
        assert state_today["input_chars"] == 0
        assert state_today["output_chars"] == 0
        assert state_today["calls"] == 0


# ---------------------------------------------------------------------------
# 11. _persist_state atomic round-trip: write then _load_state reads back correctly
# ---------------------------------------------------------------------------

def test_atomic_persist_roundtrip(tmp_path):
    with patch.dict(os.environ, {"ALPHA_LLM_DAILY_USD_CAP": "1.0"}):
        m = _reload(tmp_path)
        day = "2024-06-15"
        state: Dict[str, Any] = {
            "day": day,
            "input_chars": 12345,
            "output_chars": 678,
            "calls": 9,
        }
        m._persist_state(state)
        loaded = m._load_state(day)
        assert loaded["input_chars"] == 12345
        assert loaded["output_chars"] == 678
        assert loaded["calls"] == 9
        assert loaded["day"] == day


def test_load_state_returns_zeros_for_missing_file(tmp_path):
    with patch.dict(os.environ, {"ALPHA_LLM_DAILY_USD_CAP": "1.0"}):
        m = _reload(tmp_path)
        # No file exists for this day in tmp_path.
        loaded = m._load_state("2099-12-31")
        assert loaded["input_chars"] == 0
        assert loaded["output_chars"] == 0
        assert loaded["calls"] == 0


def test_load_state_returns_zeros_for_corrupt_file(tmp_path):
    with patch.dict(os.environ, {"ALPHA_LLM_DAILY_USD_CAP": "1.0"}):
        m = _reload(tmp_path)
        day = "2024-07-01"
        corrupt_path = tmp_path / f".llm_budget_{day.replace('-', '')}.json"
        corrupt_path.write_text("{{not valid json}}", encoding="utf-8")
        loaded = m._load_state(day)
        assert loaded["input_chars"] == 0
        assert loaded["output_chars"] == 0


# ---------------------------------------------------------------------------
# 12. current_state() returns the expected schema keys with correct types
# ---------------------------------------------------------------------------

def test_current_state_schema(tmp_path):
    with patch.dict(os.environ, {
        "ALPHA_LLM_DAILY_USD_CAP": "5.0",
        "ALPHA_LLM_INPUT_PRICE_PER_1M": "3.0",
        "ALPHA_LLM_OUTPUT_PRICE_PER_1M": "15.0",
    }):
        m = _reload(tmp_path)
        state = m.current_state()
        assert isinstance(state["day"], str)
        assert isinstance(state["calls"], int)
        assert isinstance(state["input_chars"], int)
        assert isinstance(state["output_chars"], int)
        assert isinstance(state["estimated_usd_spent"], float)
        assert isinstance(state["cap_usd"], float)
        assert state["enabled"] is True
        assert isinstance(state["remaining_usd"], float)
        assert state["remaining_usd"] >= 0.0


# ---------------------------------------------------------------------------
# 13. record_usage increments state counters and persists to disk
# ---------------------------------------------------------------------------

def test_record_usage_increments_counters(tmp_path):
    with patch.dict(os.environ, {
        "ALPHA_LLM_DAILY_USD_CAP": "1.0",
        "ALPHA_LLM_INPUT_PRICE_PER_1M": "3.0",
        "ALPHA_LLM_OUTPUT_PRICE_PER_1M": "15.0",
    }):
        m = _reload(tmp_path)
        m.record_usage(input_chars=5_000, output_chars=1_000, agent="test")
        m.record_usage(input_chars=3_000, output_chars=500, agent="test")
        state = m.current_state()
        assert state["input_chars"] == 8_000
        assert state["output_chars"] == 1_500
        assert state["calls"] == 2


# ---------------------------------------------------------------------------
# 14. Concurrency: two threads both able to reserve if total stays under cap
# ---------------------------------------------------------------------------

def test_concurrent_reservations_within_cap(tmp_path):
    """Two threads each reserving a modest amount, both should succeed."""
    with patch.dict(os.environ, {
        "ALPHA_LLM_DAILY_USD_CAP": "1.0",
        "ALPHA_LLM_INPUT_PRICE_PER_1M": "3.0",
        "ALPHA_LLM_OUTPUT_PRICE_PER_1M": "15.0",
    }):
        m = _reload(tmp_path)
        tokens = []
        errors = []

        def _reserve():
            try:
                tok = m.reserve_budget(1_000, est_output_chars=0, agent="thread")
                tokens.append(tok)
            except m.LLMBudgetExceededError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_reserve) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 5 reservations should succeed — total is tiny vs $1.0 cap.
        assert len(errors) == 0
        assert len(tokens) == 5
        # Clean up.
        for tok in tokens:
            m.settle_reservation(tok, actual_input_chars=1_000, actual_output_chars=0)


# ---------------------------------------------------------------------------
# 15. reserve_budget returns None and settle is a no-op when cap=0
# ---------------------------------------------------------------------------

def test_reserve_and_settle_noop_when_cap_disabled(tmp_path):
    """When cap=0, reserve_budget returns None and settle_reservation is safe to call.

    Note: settle_reservation(None, ...) still records actual usage in the state
    file (this is intentional — it keeps the usage counter accurate even when
    the cap guard is disabled).  The critical assertions are: (a) no exception,
    (b) the returned token is None.
    """
    with patch.dict(os.environ, {"ALPHA_LLM_DAILY_USD_CAP": "0",
                                 "ALPHA_LLM_PER_STRATEGY_USD_CAP": "0"}):
        m = _reload(tmp_path)
        token = m.reserve_budget(999_999, est_output_chars=999_999, agent="test")
        assert token is None
        # settle with None must not raise, even for large actual usage values.
        m.settle_reservation(None, actual_input_chars=999_999, actual_output_chars=999_999)
        # Usage is recorded (by design) even when the cap is disabled.
        state = m.current_state()
        assert state["calls"] == 1
        assert state["input_chars"] == 999_999

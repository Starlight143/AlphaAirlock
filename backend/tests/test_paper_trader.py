"""Unit tests for backend/core/paper_trader.py — Audit Finding #34.

Covers:
  1. _health_check vetoes strategy with Sharpe < MIN_SHARPE
  2. _health_check vetoes strategy with max_drawdown < PAPER_MAX_DRAWDOWN
  3. _health_check treats NaN metric as 0.0 and does NOT crash (D-L6 / P16 fix)
  4. _health_check passes when all metrics are above thresholds
  5. _health_check vetoes strategy with too few trades
  6. _health_check vetoes strategy with profit_factor < MIN_PROFIT_FACTOR
  7. _persist + latest_for atomic round-trip (tmp+os.replace)
  8. latest_for returns None for non-existent strategy_id
  9. _get_strategy_lock LRU eviction skips a held lock (blocking=False guard)
  10. run_paper_trade raises LookupError for unknown strategy_id
  11. _LATEST_CACHE FIFO eviction at _LATEST_CACHE_MAX boundary
  12. PaperTradeResult.to_dict() serializes all fields correctly
"""
from __future__ import annotations

import json
import math
import tempfile
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

# conftest.py already puts repo root + backend/ on sys.path and loads .env.
from backend.core.paper_trader import (
    PaperTradeResult,
    _get_strategy_lock,
    _health_check,
    _persist,
    latest_for,
)
from backend.core.thresholds import (
    MIN_PROFIT_FACTOR,
    MIN_SHARPE,
    MIN_TRADES_PAPER,
    PAPER_MAX_DRAWDOWN,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _good_metrics() -> Dict[str, float]:
    """Metrics that pass every health-check threshold."""
    return {
        "annualized_sharpe": MIN_SHARPE + 1.0,          # e.g. 1.5
        "max_drawdown": PAPER_MAX_DRAWDOWN / 2.0,        # e.g. -0.125 (less severe)
        "profit_factor": MIN_PROFIT_FACTOR + 1.0,        # e.g. 2.05
    }


_GOOD_TRADES = MIN_TRADES_PAPER + 10  # comfortably above threshold


def _make_result(strategy_id: int = 42) -> PaperTradeResult:
    return PaperTradeResult(
        strategy_id=strategy_id,
        window_days=30,
        metrics={"annualized_sharpe": 1.5, "max_drawdown": -0.05, "profit_factor": 2.0},
        equity_curve=[{"t": "2024-01-01", "v": 1.0}, {"t": "2024-01-02", "v": 1.01}],
        trades=_GOOD_TRADES,
        is_healthy=True,
        health_notes=["Sharpe 1.50 OK", "MaxDD -5.0% OK"],
        run_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


# ---------------------------------------------------------------------------
# 1. Sharpe below threshold => is_healthy=False, notes mention Sharpe
# ---------------------------------------------------------------------------

def test_health_check_sharpe_fail():
    metrics = _good_metrics()
    metrics["annualized_sharpe"] = MIN_SHARPE - 0.1  # below threshold
    is_healthy, notes = _health_check(metrics, trades=_GOOD_TRADES)
    assert is_healthy is False
    assert any("sharpe" in n.lower() for n in notes), f"Expected 'sharpe' in notes: {notes}"


# ---------------------------------------------------------------------------
# 2. Drawdown worse than floor => is_healthy=False
#    PAPER_MAX_DRAWDOWN = -0.25; dd=-0.99 is LESS than -0.25 => vetoed
# ---------------------------------------------------------------------------

def test_health_check_drawdown_fail():
    metrics = _good_metrics()
    metrics["max_drawdown"] = -0.99  # worse than -0.25 (more negative)
    is_healthy, notes = _health_check(metrics, trades=_GOOD_TRADES)
    assert is_healthy is False
    assert any("maxdd" in n.lower() or "drawdown" in n.lower() or "floor" in n.lower() for n in notes), (
        f"Expected drawdown note in {notes}"
    )


# ---------------------------------------------------------------------------
# 3. NaN metric handled: no exception, treated as 0.0 => below MIN_SHARPE
# ---------------------------------------------------------------------------

def test_health_check_nan_sharpe_no_crash():
    """D-L6 / P16 fix: NaN sharpe must not raise, must be treated as 0.0."""
    metrics = {
        "annualized_sharpe": float("nan"),
        "max_drawdown": PAPER_MAX_DRAWDOWN / 2.0,
        "profit_factor": MIN_PROFIT_FACTOR + 1.0,
    }
    # Must not raise any exception.
    is_healthy, notes = _health_check(metrics, trades=_GOOD_TRADES)
    # NaN -> 0.0 -> 0.0 < MIN_SHARPE (0.5) -> unhealthy
    assert is_healthy is False
    # Sharpe note must appear and reference the 0.0 value
    sharpe_notes = [n for n in notes if "sharpe" in n.lower()]
    assert sharpe_notes, f"No Sharpe note found in {notes}"


def test_health_check_nan_drawdown_no_crash():
    """NaN drawdown must not raise, and must cause unhealthy (0.0 > PAPER_MAX_DRAWDOWN is True, so dd check passes, but this ensures no crash)."""
    metrics = {
        "annualized_sharpe": MIN_SHARPE + 1.0,
        "max_drawdown": float("nan"),  # -> 0.0
        "profit_factor": MIN_PROFIT_FACTOR + 1.0,
    }
    # Must not raise any exception.
    is_healthy, notes = _health_check(metrics, trades=_GOOD_TRADES)
    # dd=NaN -> 0.0; 0.0 >= PAPER_MAX_DRAWDOWN (-0.25) -> dd check passes
    # So is_healthy depends on other metrics.
    assert isinstance(is_healthy, bool)
    assert isinstance(notes, list)
    # All metrics above threshold and dd=NaN->0.0 passes the floor: must be healthy.
    assert is_healthy is True, (
        f"NaN drawdown must coerce to 0.0 and pass the dd floor; got is_healthy=False. "
        f"notes={notes}"
    )


# ---------------------------------------------------------------------------
# 4. All metrics above thresholds => is_healthy=True
# ---------------------------------------------------------------------------

def test_health_check_all_pass():
    is_healthy, notes = _health_check(_good_metrics(), trades=_GOOD_TRADES)
    assert is_healthy is True
    assert len(notes) > 0


# ---------------------------------------------------------------------------
# 5. Too few trades => is_healthy=False
# ---------------------------------------------------------------------------

def test_health_check_too_few_trades_fail():
    metrics = _good_metrics()
    is_healthy, notes = _health_check(metrics, trades=MIN_TRADES_PAPER - 1)
    assert is_healthy is False
    assert any("trade" in n.lower() for n in notes), f"Expected trade count note in {notes}"


# ---------------------------------------------------------------------------
# 6. Profit factor below threshold => is_healthy=False
# ---------------------------------------------------------------------------

def test_health_check_profit_factor_fail():
    metrics = _good_metrics()
    metrics["profit_factor"] = MIN_PROFIT_FACTOR - 0.5  # below threshold
    is_healthy, notes = _health_check(metrics, trades=_GOOD_TRADES)
    assert is_healthy is False
    assert any("profit" in n.lower() for n in notes), f"Expected profit note in {notes}"


# ---------------------------------------------------------------------------
# 7. _persist + latest_for atomic round-trip
#    Verifies the tmp+os.replace atomic write pattern and JSON deserialization.
# ---------------------------------------------------------------------------

def test_persist_and_latest_for_roundtrip():
    result = _make_result(strategy_id=9001)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with patch("backend.core.paper_trader.PAPER_DIR", tmp_path):
            # Clear cache entry for this sid to force re-read.
            import backend.core.paper_trader as pt_mod
            with pt_mod._LATEST_CACHE_LOCK:
                pt_mod._LATEST_CACHE.pop(9001, None)

            _persist(result, name="test_strategy_9001")

            # Verify file exists and no .tmp leftover.
            out_file = tmp_path / "9001.json"
            assert out_file.exists(), "Output JSON file was not created"
            tmp_file = tmp_path / "9001.tmp"
            assert not tmp_file.exists(), ".tmp file should have been replaced"

            loaded = latest_for(9001)

    assert loaded is not None, "latest_for returned None after persist"
    assert loaded["strategy_id"] == 9001
    assert loaded["is_healthy"] is True
    assert loaded["trades"] == _GOOD_TRADES
    assert loaded["name"] == "test_strategy_9001"
    assert isinstance(loaded["equity_curve"], list)
    assert len(loaded["equity_curve"]) == 2


# ---------------------------------------------------------------------------
# 8. latest_for returns None for non-existent strategy_id
# ---------------------------------------------------------------------------

def test_latest_for_missing_strategy_returns_none():
    import backend.core.paper_trader as pt_mod
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with patch("backend.core.paper_trader.PAPER_DIR", tmp_path):
            with pt_mod._LATEST_CACHE_LOCK:
                pt_mod._LATEST_CACHE.pop(99999, None)
            result = latest_for(99999)
    assert result is None


# ---------------------------------------------------------------------------
# 9. _get_strategy_lock LRU eviction skips a held lock (blocking=False guard)
#    P15/D-M24: a lock that is currently held must NOT be evicted.
# ---------------------------------------------------------------------------

def test_get_strategy_lock_skips_held_lock_during_eviction():
    import backend.core.paper_trader as pt_mod

    # Save original state.
    original_locks = pt_mod._STRATEGY_WRITE_LOCKS
    original_guard = pt_mod._STRATEGY_WRITE_LOCKS_GUARD
    original_max = pt_mod._STRATEGY_WRITE_LOCKS_MAX

    # Use a small capacity (2) so eviction triggers on the 3rd insertion.
    fake_locks: "OrderedDict[int, Lock]" = OrderedDict()
    fake_guard = Lock()

    try:
        pt_mod._STRATEGY_WRITE_LOCKS = fake_locks
        pt_mod._STRATEGY_WRITE_LOCKS_GUARD = fake_guard
        pt_mod._STRATEGY_WRITE_LOCKS_MAX = 2

        # Insert 2 locks to fill the dict.
        lock_a = _get_strategy_lock(1001)
        lock_b = _get_strategy_lock(1002)

        # Hold lock_a (oldest) so eviction must skip it.
        held = lock_a.acquire(blocking=True)
        assert held, "Should acquire lock_a"
        try:
            # Inserting a 3rd strategy triggers eviction of oldest (1001).
            # Since lock_a (sid=1001) is held, acquire(blocking=False) returns False
            # -> eviction is skipped -> dict grows to 3 entries.
            lock_c = _get_strategy_lock(1003)
            # lock_a must still be in the dict (not evicted).
            assert 1001 in pt_mod._STRATEGY_WRITE_LOCKS, (
                "Held lock 1001 was incorrectly evicted from the LRU dict"
            )
        finally:
            lock_a.release()

        # After releasing, inserting a 4th triggers eviction again;
        # this time 1001 is unheld so it SHOULD be evicted.
        lock_d = _get_strategy_lock(1004)
        # Dict should now have at most 3 entries (1002, 1003, 1004) — 1001 evicted.
        assert 1001 not in pt_mod._STRATEGY_WRITE_LOCKS, (
            "Released lock 1001 should have been evicted when 4th lock was added"
        )

    finally:
        pt_mod._STRATEGY_WRITE_LOCKS = original_locks
        pt_mod._STRATEGY_WRITE_LOCKS_GUARD = original_guard
        pt_mod._STRATEGY_WRITE_LOCKS_MAX = original_max


# ---------------------------------------------------------------------------
# 10. run_paper_trade raises LookupError for unknown strategy_id
#     Mocked: session_scope returns a session where .get() returns None.
# ---------------------------------------------------------------------------

def test_run_paper_trade_missing_strategy_raises_lookup_error():
    from backend.core.paper_trader import run_paper_trade

    mock_session = MagicMock()
    mock_session.get.return_value = None  # simulate "not found"

    class _FakeCtx:
        def __enter__(self):
            return mock_session
        def __exit__(self, *a):
            return False

    with patch("backend.core.paper_trader.session_scope", return_value=_FakeCtx()):
        with pytest.raises(LookupError, match="not found"):
            run_paper_trade(strategy_id=999_999_999)


# ---------------------------------------------------------------------------
# 11. _LATEST_CACHE FIFO eviction at _LATEST_CACHE_MAX boundary
#     When cache is full and a new (unknown) sid is added, oldest entry evicted.
# ---------------------------------------------------------------------------

def test_latest_cache_fifo_eviction():
    import backend.core.paper_trader as pt_mod

    original_cache = pt_mod._LATEST_CACHE
    original_lock = pt_mod._LATEST_CACHE_LOCK
    original_max = pt_mod._LATEST_CACHE_MAX

    fake_cache: Dict[int, Any] = {}
    fake_lock = Lock()

    try:
        pt_mod._LATEST_CACHE = fake_cache
        pt_mod._LATEST_CACHE_LOCK = fake_lock
        pt_mod._LATEST_CACHE_MAX = 3

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("backend.core.paper_trader.PAPER_DIR", tmp_path):
                # Pre-fill the cache with 3 entries (sids 1, 2, 3).
                for sid in [100, 200, 300]:
                    result = _make_result(strategy_id=sid)
                    _persist(result, name=f"strat_{sid}")
                    # Manually populate cache as latest_for would (mtime, payload).
                    p = tmp_path / f"{sid}.json"
                    mtime = p.stat().st_mtime
                    payload = json.loads(p.read_text(encoding="utf-8"))
                    with fake_lock:
                        fake_cache[sid] = (mtime, payload)

                assert len(fake_cache) == 3
                assert 100 in fake_cache  # oldest

                # Write sid=400 to disk so latest_for has a real file to read.
                result_400 = _make_result(strategy_id=400)
                _persist(result_400, name="strat_400")

                # latest_for(400) should trigger FIFO eviction of oldest (100).
                loaded = latest_for(400)

        assert loaded is not None
        assert loaded["strategy_id"] == 400
        # Cache should have at most 3 entries; sid=100 must have been evicted.
        assert 100 not in fake_cache, "Oldest cache entry (100) was not evicted"
        assert 400 in fake_cache

    finally:
        pt_mod._LATEST_CACHE = original_cache
        pt_mod._LATEST_CACHE_LOCK = original_lock
        pt_mod._LATEST_CACHE_MAX = original_max


# ---------------------------------------------------------------------------
# 12. PaperTradeResult.to_dict() serializes all required fields
# ---------------------------------------------------------------------------

def test_paper_trade_result_to_dict():
    result = _make_result(strategy_id=77)
    d = result.to_dict()
    assert d["strategy_id"] == 77
    assert d["window_days"] == 30
    assert d["is_healthy"] is True
    assert d["trades"] == _GOOD_TRADES
    assert isinstance(d["metrics"], dict)
    assert isinstance(d["equity_curve"], list)
    assert isinstance(d["health_notes"], list)
    assert isinstance(d["run_at"], str)
    # Verify exact keys — all dataclass fields must be represented, including per_bar.
    # An issubset check would stay green even if per_bar were accidentally dropped
    # from to_dict(), silently breaking live_trade_ops._position_series.
    expected_keys = {"strategy_id", "window_days", "metrics", "equity_curve", "trades",
                     "is_healthy", "health_notes", "run_at", "per_bar"}
    assert set(d.keys()) == expected_keys, (
        f"Keys mismatch — extra: {set(d.keys()) - expected_keys}, "
        f"missing: {expected_keys - set(d.keys())}"
    )
    assert isinstance(d["per_bar"], list), "per_bar must be a list (required by live_trade_ops._position_series)"


# ---------------------------------------------------------------------------
# 13. _health_check handles string metric values without crashing (D-L6 coverage)
#     The health gate must be robust to JSON-deserialized non-numeric values.
# ---------------------------------------------------------------------------

def test_health_check_string_metric_no_crash():
    metrics = {
        "annualized_sharpe": "not-a-number",
        "max_drawdown": None,
        "profit_factor": "N/A",
    }
    # Must not raise.
    is_healthy, notes = _health_check(metrics, trades=_GOOD_TRADES)
    # All non-numeric -> treated as 0.0 -> sharpe=0.0 < MIN_SHARPE -> unhealthy
    assert is_healthy is False


# ---------------------------------------------------------------------------
# 14. latest_for returns None and removes cache entry for corrupt JSON file
# ---------------------------------------------------------------------------

def test_latest_for_corrupt_json_returns_none():
    import backend.core.paper_trader as pt_mod

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with patch("backend.core.paper_trader.PAPER_DIR", tmp_path):
            corrupt_file = tmp_path / "5555.json"
            corrupt_file.write_text("{this is not valid json", encoding="utf-8")

            with pt_mod._LATEST_CACHE_LOCK:
                pt_mod._LATEST_CACHE.pop(5555, None)

            result = latest_for(5555)

    # corrupt JSON -> payload=None is stored in cache, and None is returned.
    assert result is None

"""P-SIM auto-advance: the periodic market-data refresh must ALSO tick the
forward-sim accounts, and be on by default.

Before this, ``market_data_refresh`` refreshed the price CSVs but never called
``sim_account.tick_all_active`` (that lived only in the manual
``POST /api/market-data/ingest`` handler), so the sim accounts drifted a full day
behind the data sitting in their own CSV. These lock in:

  * ``refresh_market_and_tick`` refreshes THEN ticks, and survives a refresh error;
  * the ``market_data_refresh`` periodic task is default-ON, hourly, and wired to
    the refresh+tick wrapper (not the bare refresh).
"""
from __future__ import annotations

import backend.core.market_data as md
import backend.core.periodic_tasks as pt
import backend.core.sim_account as sa


def test_refresh_market_and_tick_calls_refresh_then_tick(monkeypatch):
    calls: list[str] = []

    def fake_refresh():
        calls.append("refresh")
        return {"mode": "incremental", "ok_count": 3}

    def fake_tick():
        calls.append("tick")
        return {"considered": 2, "ran": 2, "errors": 0}

    monkeypatch.setattr(md, "refresh_task", fake_refresh)
    monkeypatch.setattr(sa, "tick_all_active", fake_tick)

    out = sa.refresh_market_and_tick()

    assert calls == ["refresh", "tick"]  # refresh must land before the sims advance
    assert out["refresh"] == {"mode": "incremental", "ok_count": 3}
    assert out["sim_tick"] == {"considered": 2, "ran": 2, "errors": 0}


def test_refresh_market_and_tick_survives_refresh_failure(monkeypatch):
    # A market-data refresh blow-up must NOT stop the sim tick — accounts still
    # advance over whatever bars are already on disk.
    ticked: list[bool] = []

    def boom():
        raise RuntimeError("binance 503")

    def fake_tick():
        ticked.append(True)
        return {"considered": 1, "ran": 1, "errors": 0}

    monkeypatch.setattr(md, "refresh_task", boom)
    monkeypatch.setattr(sa, "tick_all_active", fake_tick)

    out = sa.refresh_market_and_tick()

    assert ticked == [True]
    assert out["refresh"] is None
    assert out["sim_tick"]["ran"] == 1


def _spec_for(task_id: str):
    for tid, env_flag, module_path, callable_name in pt._TASK_SPECS:
        if tid == task_id:
            return env_flag, module_path, callable_name
    raise AssertionError(f"{task_id} not in _TASK_SPECS")


def test_market_data_refresh_periodic_default_on_hourly_and_wired(monkeypatch):
    monkeypatch.delenv("MARKET_DATA_REFRESH_ENABLED", raising=False)
    monkeypatch.delenv("PERIODIC_MARKET_DATA_REFRESH_SECONDS", raising=False)

    env_flag, module_path, callable_name = _spec_for("market_data_refresh")
    # Default ON (no env set) so sims stay current out-of-the-box.
    assert pt._flag_enabled("market_data_refresh", env_flag) is True
    # Hourly default cadence.
    assert pt._interval_seconds("market_data_refresh") == 3600
    # Wired to the refresh+tick wrapper, and it actually resolves.
    assert (module_path, callable_name) == ("backend.core.sim_account", "refresh_market_and_tick")
    assert pt._resolve_callable(module_path, callable_name) is sa.refresh_market_and_tick


def test_market_data_refresh_opt_out(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_REFRESH_ENABLED", "0")
    env_flag, _, _ = _spec_for("market_data_refresh")
    assert pt._flag_enabled("market_data_refresh", env_flag) is False

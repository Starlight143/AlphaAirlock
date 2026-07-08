"""Regression tests for the pipeline orphan reaper (orchestrator.reap_orphaned_pipelines).

Locks in the fix for strategies stuck forever in a non-terminal stage
(INTAKE/STORY_GEN/…) after their pipeline worker was hard-killed mid-run
(process restart / macOS sleep freeze) — the "stuck at Stage 0, never
continues" Mission Control symptom. The reaper flushes such orphans to
REJECTED, but must NEVER touch a live run, a fresh row, or a terminal/promoted
strategy.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.core.orchestrator as orch
from backend.core.database import AlphaStrategy, Base
from backend.core.orchestrator import PipelineStatus, RunState


@pytest.fixture()
def tmpdb(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'orphan.db').as_posix()}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)  # alpha_strategies + stage_transitions
    Maker = sessionmaker(
        bind=engine, autoflush=False, autocommit=False,
        expire_on_commit=False, future=True,
    )

    @contextmanager
    def _scope():
        s = Maker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # The reaper resolves session_scope from the orchestrator module global.
    monkeypatch.setattr(orch, "session_scope", _scope)
    orch._REGISTRY.clear()
    yield _scope, Maker
    orch._REGISTRY.clear()
    engine.dispose()


def _add(Maker, *, name, status, stage, age_minutes):
    ts = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    s = Maker()
    try:
        row = AlphaStrategy(
            name=name, status=status, stage=stage, created_at=ts, updated_at=ts,
        )
        s.add(row)
        s.commit()
        return int(row.id)
    finally:
        s.close()


def _status_of(Maker, sid):
    s = Maker()
    try:
        row = s.get(AlphaStrategy, sid)
        return (row.status, int(row.stage))
    finally:
        s.close()


def test_reaps_stale_nonterminal_orphans(tmpdb):
    _scope, Maker = tmpdb
    intake = _add(Maker, name="stuck-intake", status="INTAKE", stage=0, age_minutes=120)
    critic = _add(Maker, name="stuck-critic", status="CRITIC_LOOP", stage=3, age_minutes=90)

    reaped = orch.reap_orphaned_pipelines(stale_minutes=30)

    assert reaped == 2
    assert _status_of(Maker, intake) == ("REJECTED", 7)
    assert _status_of(Maker, critic) == ("REJECTED", 7)
    # A stage_transitions audit row was written for the flush.
    s = Maker()
    try:
        from backend.core.database import StageTransition
        rows = s.query(StageTransition).filter(StageTransition.strategy_id == intake).all()
        assert any(r.to_status == "REJECTED" and r.actor == "orphan_reaper" for r in rows)
    finally:
        s.close()


def test_does_not_reap_fresh_rows(tmpdb):
    _scope, Maker = tmpdb
    fresh = _add(Maker, name="fresh", status="INTAKE", stage=0, age_minutes=1)

    reaped = orch.reap_orphaned_pipelines(stale_minutes=30)

    assert reaped == 0
    assert _status_of(Maker, fresh) == ("INTAKE", 0)  # untouched — likely live


def test_does_not_reap_live_run_in_registry(tmpdb):
    _scope, Maker = tmpdb
    sid = _add(Maker, name="running", status="STORY_GEN", stage=1, age_minutes=120)
    # A live run in THIS process owns it — even though it's stale, skip it.
    orch._REGISTRY[sid] = RunState(strategy_id=sid, status=PipelineStatus.STORY_GEN)

    reaped = orch.reap_orphaned_pipelines(stale_minutes=30)

    assert reaped == 0
    assert _status_of(Maker, sid) == ("STORY_GEN", 1)


def test_does_not_reap_terminal_or_promoted(tmpdb):
    _scope, Maker = tmpdb
    approved = _add(Maker, name="approved", status="APPROVED", stage=4, age_minutes=600)
    live = _add(Maker, name="live", status="LIVE", stage=6, age_minutes=600)
    rejected = _add(Maker, name="rej", status="REJECTED", stage=7, age_minutes=600)

    reaped = orch.reap_orphaned_pipelines(stale_minutes=30)

    assert reaped == 0
    assert _status_of(Maker, approved) == ("APPROVED", 4)
    assert _status_of(Maker, live) == ("LIVE", 6)
    assert _status_of(Maker, rejected) == ("REJECTED", 7)


def test_stale_minutes_floor_is_enforced(tmpdb, monkeypatch):
    # Even if an operator passes a tiny / bogus threshold, the 15-min floor
    # protects a 5-minute-old (plausibly still-running) strategy.
    _scope, Maker = tmpdb
    recent = _add(Maker, name="five-min", status="CODE_GEN", stage=2, age_minutes=5)

    reaped = orch.reap_orphaned_pipelines(stale_minutes=1)  # clamps up to 15

    assert reaped == 0
    assert _status_of(Maker, recent) == ("CODE_GEN", 2)

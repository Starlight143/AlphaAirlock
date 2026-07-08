"""Regression: the ingest fetch must run OUTSIDE the DB transaction.

Locks in the fix for the intermittent ``database is locked`` crash on
``UPDATE ingest_sources SET last_polled_at``. ``_run_fetcher_sync`` used to hold
a read snapshot open across ``fetch_for()``'s network I/O; the deferred
``last_polled_at`` UPDATE then had to upgrade that now-stale snapshot at commit
time → SQLITE_BUSY_SNAPSHOT, which ``busy_timeout`` does NOT retry. The fix
commits ``last_polled_at`` in its own short transaction BEFORE the fetch.

The decisive observable: at the moment ``fetch_for`` runs, ``last_polled_at`` is
already committed and therefore visible to an INDEPENDENT connection. Under the
old single-transaction shape it would still be uncommitted (None) to that
connection. A real on-disk SQLite file is used so cross-connection commit
visibility is genuine (``:memory:`` gives each connection its own database).
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.core.scheduler as scheduler
from backend.core.database import IngestEvent, IngestSource, KnowledgeNode
from backend.core.ingest_fetchers import FetchedItem, FetchOutcome


@pytest.fixture()
def memdb(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'sched.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    IngestSource.__table__.create(bind=engine, checkfirst=True)
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

    # _run_fetcher_sync / _persist_outcome resolve session_scope from the module
    # global — point it at the throwaway DB.
    monkeypatch.setattr(scheduler, "session_scope", _scope)
    with _scope() as s:
        s.add(IngestSource(name="x", source_type="rss", url="https://e/feed", enabled=True))
    yield engine, Maker
    engine.dispose()


def test_last_polled_committed_before_fetch_runs(memdb, monkeypatch):
    engine, Maker = memdb
    with Maker() as s0:
        sid = s0.query(IngestSource).one().id

    seen: dict = {}

    def _probe_fetch(src):
        # Independent connection. If Phase 1 committed before us (the fix),
        # last_polled_at is already visible here; under the old single-txn code
        # it would still be None (uncommitted in the still-open fetch txn).
        probe = Maker()
        try:
            seen["last_polled_at"] = probe.get(IngestSource, sid).last_polled_at
        finally:
            probe.close()
        # src is detached here (session closed in Phase 1) — reading a loaded
        # scalar must NOT raise (expire_on_commit=False contract).
        seen["url_via_detached_src"] = src.url
        return FetchOutcome(items=[])

    monkeypatch.setattr(scheduler, "fetch_for", _probe_fetch)

    outcome, snap = scheduler._run_fetcher_sync(sid)

    assert outcome.error is None
    assert snap is not None and snap["url"] == "https://e/feed"
    # The detached src stayed usable across the (transaction-free) fetch.
    assert seen["url_via_detached_src"] == "https://e/feed"
    # The decisive assertion: the timestamp was committed BEFORE fetch_for ran.
    assert seen["last_polled_at"] is not None


def test_source_vanished_mid_tick_is_handled(memdb, monkeypatch):
    _engine, _Maker = memdb
    monkeypatch.setattr(
        scheduler, "fetch_for",
        lambda *_a, **_k: pytest.fail("fetch_for must not run for a missing source"),
    )
    outcome, snap = scheduler._run_fetcher_sync(999_999)  # no such id
    assert snap is None
    assert outcome.error == "source vanished mid-tick"


# --------------------------------------------------------------------------- #
# _persist_outcome dedup-by-url + keep-longer: a re-poll whose body GREW must   #
# UPDATE the same node in place, never mint a near-duplicate.                   #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def persistdb(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'persist.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    for tbl in (IngestSource.__table__, KnowledgeNode.__table__, IngestEvent.__table__):
        tbl.create(bind=engine, checkfirst=True)
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

    monkeypatch.setattr(scheduler, "session_scope", _scope)
    # Keep the loop to pure node logic — no image downloads, no pipeline hook.
    import backend.core.asset_cache as _ac
    import backend.core.auto_pipeline as _ap
    monkeypatch.setattr(_ac, "is_enabled", lambda: False)
    monkeypatch.setattr(_ap, "maybe_trigger_pipeline_for_nodes", lambda *a, **k: None)
    with _scope() as s:
        s.add(IngestSource(name="x", source_type="rss", url="https://e/feed", enabled=True))
    yield engine, Maker
    engine.dispose()


def _outcome(url: str, body: str) -> FetchOutcome:
    return FetchOutcome(items=[FetchedItem(title="T", url=url, body_markdown=body)])


def test_persist_updates_in_place_when_body_grows(persistdb):
    _engine, Maker = persistdb
    with Maker() as s:
        sid = s.query(IngestSource).one().id

    # 1) First poll: teaser → one node.
    scheduler._persist_outcome(sid, _outcome("https://e/a", "short teaser [...]"), None)
    with Maker() as s:
        nodes = s.query(KnowledgeNode).all()
        assert len(nodes) == 1
        nid = nodes[0].id
        assert nodes[0].content == "short teaser [...]"

    # 2) Re-poll, SAME url, longer body → UPDATE the same node (no new row).
    full = "the complete article body, considerably longer than the teaser was"
    scheduler._persist_outcome(sid, _outcome("https://e/a", full), None)
    with Maker() as s:
        nodes = s.query(KnowledgeNode).all()
        assert len(nodes) == 1            # still ONE node — no duplicate minted
        assert nodes[0].id == nid         # same node, updated in place
        assert nodes[0].content == full

    # 3) Re-poll, SAME url, shorter body → SKIP (keep the fuller version).
    scheduler._persist_outcome(sid, _outcome("https://e/a", "teaser again"), None)
    with Maker() as s:
        nodes = s.query(KnowledgeNode).all()
        assert len(nodes) == 1
        assert nodes[0].content == full   # unchanged


def test_persist_distinct_urls_insert_separate_nodes(persistdb):
    _engine, Maker = persistdb
    with Maker() as s:
        sid = s.query(IngestSource).one().id
    scheduler._persist_outcome(sid, _outcome("https://e/a", "body of article one"), None)
    scheduler._persist_outcome(sid, _outcome("https://e/b", "body of article two"), None)
    with Maker() as s:
        assert s.query(KnowledgeNode).count() == 2  # different urls → two nodes

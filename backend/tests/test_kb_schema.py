"""T1-B / T2-B — KnowledgeNode memory-record schema + additive migration.

Locks in the highest-risk step of the optimisation blueprint: adding the new
columns (``summary``/``summary_generated_at``/``confidence``/
``revisit_flagged_at``/``status``) to a LIVE, pre-existing ``knowledge_nodes``
table via the same ``ensure_columns()`` path the app runs at boot — proving a
legacy row backfills to ``status='active'`` with a NULL summary, the migration
is idempotent, and the model helpers / ``to_dict()`` stay NULL-safe.
"""
from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from backend.core.database import (
    KB_STATUS_ACTIVE,
    KnowledgeNode,
)
from backend.core.schema_migrations import ensure_columns


# The exact knowledge_nodes DDL as it existed BEFORE the T1-B/T2-B columns, so
# the migration's ALTER TABLE path is genuinely exercised (not just create_all).
_LEGACY_DDL = """
CREATE TABLE knowledge_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title VARCHAR(512) NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    tags VARCHAR(1024) NOT NULL DEFAULT '',
    links TEXT NOT NULL DEFAULT '[]',
    ic_score FLOAT NOT NULL DEFAULT 0.0,
    kind VARCHAR(32) NOT NULL DEFAULT 'concept',
    source_url VARCHAR(1024),
    source_type VARCHAR(32),
    category VARCHAR(64),
    content_hash VARCHAR(64),
    ingested_at DATETIME,
    origin_strategy_id INTEGER,
    auto_pipeline_strategy_id INTEGER,
    last_queue_eval_at DATETIME,
    ic_decayed_at DATETIME,
    created_at DATETIME NOT NULL
)
"""

# Mirrors the new specs added to database._run_lightweight_migrations.
_NEW_COLUMN_SPECS = [
    ("knowledge_nodes", "summary", "TEXT", {}),
    ("knowledge_nodes", "summary_generated_at", "DATETIME", {}),
    ("knowledge_nodes", "confidence", "FLOAT", {}),
    ("knowledge_nodes", "revisit_flagged_at", "DATETIME", {}),
    (
        "knowledge_nodes",
        "status",
        "VARCHAR(16)",
        {"not_null": True, "default_sql": "'active'"},
    ),
]


def _legacy_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'kb.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as conn:
        conn.execute(text(_LEGACY_DDL))
        conn.execute(
            text(
                "INSERT INTO knowledge_nodes (title, content, ic_score, kind, created_at) "
                "VALUES ('legacy', 'old body', 0.4, 'concept', '2024-01-01T00:00:00+00:00')"
            )
        )
    return engine


def test_migration_adds_columns_to_live_table(tmp_path):
    engine = _legacy_engine(tmp_path)
    cols_before = {c["name"] for c in inspect(engine).get_columns("knowledge_nodes")}
    assert "summary" not in cols_before and "status" not in cols_before

    added = ensure_columns(engine, _NEW_COLUMN_SPECS)
    assert added == 5

    cols_after = {c["name"] for c in inspect(engine).get_columns("knowledge_nodes")}
    for c in ("summary", "summary_generated_at", "confidence", "revisit_flagged_at", "status"):
        assert c in cols_after


def test_legacy_row_backfills_status_active(tmp_path):
    engine = _legacy_engine(tmp_path)
    ensure_columns(engine, _NEW_COLUMN_SPECS)
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT status, summary, confidence FROM knowledge_nodes WHERE title='legacy'"
            )
        ).one()
    # NOT NULL DEFAULT 'active' (constant literal) backfilled the legacy row with
    # zero row-rewrite — this is the live-DB safety guarantee.
    assert row.status == "active"
    assert row.summary is None
    assert row.confidence is None


def test_migration_idempotent(tmp_path):
    engine = _legacy_engine(tmp_path)
    assert ensure_columns(engine, _NEW_COLUMN_SPECS) == 5
    # Second run adds nothing and must not raise.
    assert ensure_columns(engine, _NEW_COLUMN_SPECS) == 0


def test_model_helpers_and_to_dict(tmp_path):
    engine = _legacy_engine(tmp_path)
    ensure_columns(engine, _NEW_COLUMN_SPECS)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = maker()
    try:
        node = s.query(KnowledgeNode).filter_by(title="legacy").one()
        assert node.status_value() == KB_STATUS_ACTIVE
        # ic_score 0.4 -> confidence (0.4+1)/2 = 0.7
        assert node.confidence_value() == (0.4 + 1.0) / 2.0
        d = node.to_dict()
        for k in ("summary", "summary_generated_at", "confidence", "status", "revisit_flagged_at"):
            assert k in d
        # Existing keys must remain present (invariant: additive only).
        for k in ("id", "title", "content", "content_hash", "ic_score", "kind", "created_at"):
            assert k in d
        assert d["status"] == "active"
        assert d["summary"] is None
        assert d["confidence"] == (0.4 + 1.0) / 2.0
    finally:
        s.close()


def test_status_and_confidence_clamp():
    node = KnowledgeNode(title="x", content="y")
    node.status = "GARBAGE"
    assert node.status_value() == KB_STATUS_ACTIVE  # unknown -> active
    node.status = "Revisit"
    assert node.status_value() == "revisit"  # case-normalised
    node.confidence = 5.0
    assert node.confidence_value() == 1.0  # clamp high
    node.confidence = -3.0
    assert node.confidence_value() == 0.0  # clamp low
    node.confidence = None
    node.ic_score = 1.0
    assert node.confidence_value() == 1.0  # ic 1.0 -> conf 1.0
    node.ic_score = -1.0
    assert node.confidence_value() == 0.0  # ic -1.0 -> conf 0.0

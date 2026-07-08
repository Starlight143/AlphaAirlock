"""Thread-safe synchronous SQLAlchemy 2.0 layer for the Agentic Alpha Research System.

Tables:
- KnowledgeNode: raw market-research nodes harvested by the Intake agent.
- AlphaStrategy: full strategy lifecycle records (Stage 0 -> Stage 4).
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Session,
    relationship,
    sessionmaker,
)

# ---------------------------------------------------------------------------
# Path / engine bootstrap
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "backend" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# KnowledgeNode.content cap (single source of truth)
# ---------------------------------------------------------------------------
# The ``content`` column is unbounded TEXT; this is a *code* cap applied at every
# write site so a single runaway article can't bloat a row, the UI, or any LLM
# consumer. It also caps the body slice fed into the dedup content_hash, so the
# SAME constant MUST be used for storage AND hashing — otherwise two distinct
# articles sharing a long prefix would falsely dedup.
#
# Raised from 32 000 → 150 000 (≈25k words) so full-length tutorials and papers
# are stored complete instead of truncated. Existing rows were all written under
# the 32 000 cap (content ≤ 32 000), so their dedup hashes are unchanged at the
# wider cap (``content[:150000] == content[:32000]`` when ``len ≤ 32000``) — no
# recompute migration was needed for this widening (see ingest_fetchers.
# _content_hash for the migration rationale). If you NARROW this value, add a
# keyed recompute migration first.
KB_CONTENT_MAX_CHARS = 150_000

DB_PATH = DATA_DIR / "alpha_system.db"
DATABASE_URL = os.environ.get(
    "ALPHA_DATABASE_URL",
    f"sqlite:///{DB_PATH.as_posix()}",
)

# SQLite + multiple threads (FastAPI workers, BackgroundTasks, test harness)
# requires check_same_thread=False. We layer a thread-safe sessionmaker on top
# of a single Engine instance per process so connections are pooled correctly.
engine: Engine = create_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)


# P6 — SQLite multi-writer safety. WAL mode + busy_timeout lets concurrent
# workers (ingest scheduler + paper tick + auto-pipeline + relink + Granger
# nightly) coexist without `database is locked` errors. NORMAL synchronous is
# still WAL-durable and ~3x faster than FULL on modest hardware.
@event.listens_for(engine, "connect")
def _apply_sqlite_pragmas(dbapi_conn, _connection_record):  # noqa: ANN001
    if not DATABASE_URL.startswith("sqlite"):
        return
    cursor = dbapi_conn.cursor()
    try:
        # P6/audit — busy_timeout raised from 5000ms. With the single shared
        # engine and the documented background-writer fan-out (paper tick,
        # reaper, telegram reports, ic_decay, granger, alpha_queue, idempotency
        # purge) plus interactive terminal submit/cancel all contending for the
        # single WAL writer, 5s was realistic to exceed under load -> 'database
        # is locked'. 15s gives far more headroom; override via env if needed.
        try:
            _bt = int(os.environ.get("ALPHA_SQLITE_BUSY_TIMEOUT_MS", "15000"))
        except (TypeError, ValueError):
            _bt = 15000
        _bt = max(5000, min(_bt, 60000))  # clamp 5s..60s
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={_bt}")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
    class_=Session,
)


class Base(DeclarativeBase):
    """Project-wide declarative base."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def slugify(text: str, max_len: int = 40) -> str:
    """ASCII-safe lowercase slug, used for both KnowledgeNode markdown filenames
    and AlphaStrategy.to_dict()'s derived `slug` field.

    Deterministic, no external deps. NFKD-normalises CJK/diacritics, drops
    non-alphanumeric runs, collapses underscores, truncates to `max_len`.
    Returns "node" if the input collapses to an empty string.
    """
    norm = unicodedata.normalize("NFKD", text or "")
    norm = norm.encode("ascii", "ignore").decode("ascii")
    norm = re.sub(r"[^a-zA-Z0-9]+", "_", norm).strip("_").lower()
    return (norm or "node")[:max_len]


# Backwards-compat alias for callers that imported the legacy intake.py name.
_slug = slugify


def _new_alpha_id() -> str:
    """Generate ALPHA-{8hex}. 32 bits entropy; UNIQUE index in DB catches the rare collision."""
    return f"ALPHA-{uuid.uuid4().hex[:8].upper()}"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

# 4-way classification used by the Mission Control knowledge-graph view.
# Stored as a free-form String to stay future-proof when more kinds are added.
KIND_CONCEPT = "concept"
KIND_PAST_ALPHA = "past_alpha"
KIND_POSTMORTEM = "postmortem"
KIND_ACTIVE = "active"
KIND_CHOICES = (KIND_CONCEPT, KIND_PAST_ALPHA, KIND_POSTMORTEM, KIND_ACTIVE)

# T1-B / T2-B — memory-record lifecycle, ORTHOGONAL to ``kind`` (which describes
# the knowledge TYPE). ``status`` describes the record STATE so the retrieval
# loop can summarise-first, archive stale rows, and re-surface decayed alphas
# without overloading the colour-mapped ``kind`` axis. Default ``active`` keeps
# every legacy row's behaviour byte-identical.
KB_STATUS_ACTIVE = "active"      # default; node is live, summary trusted
KB_STATUS_STUB = "stub"          # ingested but enrich/summary pending
KB_STATUS_DRAFT = "draft"        # draft, not yet promoted
KB_STATUS_REVISIT = "revisit"    # T2-B flagged it: confidence decayed, needs re-review
KB_STATUS_ARCHIVED = "archived"  # soft-retired; excluded from default retrieval
KB_STATUS_CHOICES = (
    KB_STATUS_ACTIVE,
    KB_STATUS_STUB,
    KB_STATUS_DRAFT,
    KB_STATUS_REVISIT,
    KB_STATUS_ARCHIVED,
)


class KnowledgeNode(Base):
    __tablename__ = "knowledge_nodes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(512), nullable=False)
    content = Column(Text, nullable=False, default="")
    tags = Column(String(1024), nullable=False, default="")  # comma-separated
    links = Column(Text, nullable=False, default="[]")        # json-encoded list[int]
    ic_score = Column(Float, nullable=False, default=0.0)
    kind = Column(
        String(32),
        nullable=False,
        default=KIND_CONCEPT,
        server_default=KIND_CONCEPT,
    )
    # P3 — provenance + de-dup
    source_url = Column(String(1024), nullable=True)
    source_type = Column(String(32), nullable=True)
    category = Column(String(64), nullable=True)  # 19-cat taxonomy bucket
    # P13/D-H2 — UNIQUE enforces DB-level dedup, replacing the unsafe
    # SELECT-then-INSERT pattern in scheduler.py / agents/intake.py.
    # See schema_migrations.run_pending() for the production data
    # backfill that dedups existing duplicates before installing the
    # constraint.
    content_hash = Column(String(64), nullable=True, unique=True)
    ingested_at = Column(DateTime(timezone=True), nullable=True)
    # P4 — postmortem linkage
    origin_strategy_id = Column(Integer, nullable=True)
    # P6 — auto-pipeline + queue + IC-decay bookkeeping
    auto_pipeline_strategy_id = Column(Integer, nullable=True)
    last_queue_eval_at = Column(DateTime(timezone=True), nullable=True)
    ic_decayed_at = Column(DateTime(timezone=True), nullable=True)
    # T1-B — summary-first memory. ``summary`` is a short agent-facing digest
    # DERIVED from ``content`` (never feeds ``content_hash``). NULL = not yet
    # compiled (lazy-backfilled on first retrieval).
    summary = Column(Text, nullable=True)
    summary_generated_at = Column(DateTime(timezone=True), nullable=True)
    # Agent-trust score in [0,1]; DISTINCT from ic_score (a [-1,1] correlation
    # stat). NULL = never scored → confidence_value() derives it from ic_score.
    confidence = Column(Float, nullable=True)
    # T2-B — confidence-decay revisit flag. Set when a past_alpha/postmortem's
    # live/OOS edge decays below threshold; NULL = not flagged.
    revisit_flagged_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(
        String(16),
        nullable=False,
        default=KB_STATUS_ACTIVE,
        server_default=KB_STATUS_ACTIVE,
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def tag_list(self) -> List[str]:
        return [t.strip() for t in (self.tags or "").split(",") if t.strip()]

    def link_list(self) -> List[int]:
        try:
            data = json.loads(self.links or "[]")
            return [int(x) for x in data if isinstance(x, (int, float, str))]
        except (ValueError, TypeError):
            return []

    def kind_value(self) -> str:
        raw = (self.kind or KIND_CONCEPT).strip().lower()
        return raw if raw in KIND_CHOICES else KIND_CONCEPT

    def status_value(self) -> str:
        raw = (self.status or KB_STATUS_ACTIVE).strip().lower()
        return raw if raw in KB_STATUS_CHOICES else KB_STATUS_ACTIVE

    def confidence_value(self) -> float:
        """Agent-trust in [0,1]. NULL falls back to ic_score mapped [-1,1]→[0,1]
        so un-backfilled legacy rows are still usable (never returns None)."""
        if self.confidence is not None:
            return max(0.0, min(1.0, float(self.confidence)))
        return max(0.0, min(1.0, (float(self.ic_score or 0.0) + 1.0) / 2.0))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "tags": self.tag_list(),
            "links": self.link_list(),
            "ic_score": float(self.ic_score or 0.0),
            "kind": self.kind_value(),
            "source_url": self.source_url,
            "source_type": self.source_type,
            "category": self.category,
            "content_hash": self.content_hash,
            "origin_strategy_id": self.origin_strategy_id,
            "auto_pipeline_strategy_id": self.auto_pipeline_strategy_id,
            "last_queue_eval_at": self.last_queue_eval_at.isoformat() if self.last_queue_eval_at else None,
            "ic_decayed_at": self.ic_decayed_at.isoformat() if self.ic_decayed_at else None,
            "ingested_at": self.ingested_at.isoformat() if self.ingested_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            # T1-B / T2-B — additive memory-record fields (existing keys above
            # are untouched; consumers reading by explicit key are unaffected).
            "summary": self.summary,
            "summary_generated_at": self.summary_generated_at.isoformat() if self.summary_generated_at else None,
            "confidence": self.confidence_value(),
            "status": self.status_value(),
            "revisit_flagged_at": self.revisit_flagged_at.isoformat() if self.revisit_flagged_at else None,
        }


class AlphaStrategy(Base):
    __tablename__ = "alpha_strategies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(512), nullable=False)
    # P17/H1 — Human-readable canonical id. UNIQUE NULLABLE so legacy rows can be back-filled.
    alpha_id = Column(String(20), nullable=True, unique=True, index=True, default=_new_alpha_id)
    stage = Column(Integer, nullable=False, default=0)  # 0..4
    formula_code = Column(Text, nullable=False, default="")
    config_json = Column(Text, nullable=False, default="{}")
    backtest_metrics = Column(Text, nullable=False, default="{}")
    team_b_review = Column(Text, nullable=False, default="")
    status = Column(String(64), nullable=False, default="INTAKE")
    # P15/D-M4 — created_at parallels updated_at; backfilled from updated_at
    # for legacy rows by schema_migrations._add_alpha_strategy_created_at.
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def config(self) -> dict:
        try:
            return json.loads(self.config_json or "{}")
        except (ValueError, TypeError):
            return {}

    def metrics(self) -> dict:
        try:
            return json.loads(self.backtest_metrics or "{}")
        except (ValueError, TypeError):
            return {}

    def slug(self) -> str:
        """Derived dated-slug filename, e.g.
        ``2026-05-24-funding-rate-mean-reversion-s47``.

        Used by the frontend as a human-readable identifier on Strategy Detail
        + Paper Trade pages (short ``S#{id}`` is kept in tables/log streams).

        D-M17/P16 — prefer ``created_at`` over ``updated_at`` so the slug is
        stable across the lifetime of the strategy. Falling back to
        ``updated_at`` keeps legacy rows (pre-created_at column) working.
        The ``-s{id}`` suffix still guarantees uniqueness if name + date
        collide across two strategies created on the same day.
        """
        date_str = (
            self.created_at or self.updated_at or datetime.now(timezone.utc)
        ).strftime("%Y-%m-%d")
        name_slug = slugify((self.name or "untitled").strip() or "untitled", max_len=40)
        return f"{date_str}-{name_slug}-s{int(self.id or 0)}"

    def to_dict(self) -> dict:
        # D-M16/P16 — emit ``created_at`` alongside ``updated_at``; the column
        # was added in P15 but the serializer was never updated, so the
        # frontend has been silently missing the creation timestamp.
        return {
            "id": self.id,
            "alpha_id": self.alpha_id,
            "slug": self.slug(),
            "name": self.name,
            "stage": int(self.stage or 0),
            "formula_code": self.formula_code or "",
            "config": self.config(),
            "metrics": self.metrics(),
            "team_b_review": self.team_b_review or "",
            "status": self.status or "INTAKE",
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ---------------------------------------------------------------------------
# P3 — Source registry, ingest events, asset cache
# ---------------------------------------------------------------------------

# Supported source-type values. Kept in sync with frontend
# ``lib/sourceTypes.ts`` (see ``SOURCE_TYPE_LABELS``). P6 added ``arxiv`` —
# real fetcher via the ``arxiv`` pip package. P8-FIX/M-3 added ``glassnode``
# as a first-class type so the reference UI's "Glassnode Insights" section
# can render even when the source is technically delivered via RSS.
SOURCE_TYPES = (
    "rss",
    "patreon",
    "medium",
    "substack",
    "reddit",
    "twitter_tag",
    "twitter_article",
    "youtube_video",
    "tiktok",
    "arxiv",
    "glassnode",
    "manual",
)

# Reference-UI categorical tabs on /sources (P5-BE-08). These are content
# categorisations, not source-type values — every IngestSource can be tagged
# with one. Keep this list in sync with frontend `lib/sourceTypes.ts` so the
# UI never displays a tab the backend won't accept.
SOURCE_CATEGORIES = (
    "apps",
    "youtube",
    "dps",
    "invoice",
    "trading_tool",
    "quant_fund",
    "algo_trading",
    "course",
    "tradeview",
    "research",
    "cloud",
    "grafana_data",
    "ai_image",
    "ai",
)


class IngestSource(Base):
    __tablename__ = "ingest_sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(512), nullable=False)
    source_type = Column(String(32), nullable=False)
    url = Column(String(1024), nullable=False)
    cadence_minutes = Column(Integer, nullable=False, default=60, server_default="60")
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    # P5 — categorical tab assignment for /sources top toolbar.
    category = Column(String(32), nullable=True)
    last_polled_at = Column(DateTime(timezone=True), nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_item_hash = Column(String(64), nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0, server_default="0")
    disabled_until = Column(DateTime(timezone=True), nullable=True)
    last_error_message = Column(String(1024), nullable=True)
    # P6 — auto-pipeline throttle (per-source daily quota for B1).
    last_auto_pipeline_at = Column(DateTime(timezone=True), nullable=True)
    auto_pipeline_count_today = Column(Integer, nullable=False, default=0, server_default="0")
    auto_pipeline_day = Column(String(10), nullable=True)  # YYYY-MM-DD UTC, cross-day reset
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        # Localized import to avoid module-load cycle with ingest_fetchers
        # (which itself imports IngestSource).
        from backend.core.ingest_fetchers import is_stub_source_type

        return {
            "id": self.id,
            "name": self.name,
            "source_type": self.source_type,
            "url": self.url,
            "cadence_minutes": int(self.cadence_minutes or 60),
            "enabled": bool(self.enabled),
            "is_stub": is_stub_source_type(self.source_type or ""),
            "category": self.category,
            "last_polled_at": self.last_polled_at.isoformat() if self.last_polled_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_item_hash": self.last_item_hash,
            "consecutive_failures": int(self.consecutive_failures or 0),
            "disabled_until": self.disabled_until.isoformat() if self.disabled_until else None,
            "last_error_message": self.last_error_message,
            "last_auto_pipeline_at": self.last_auto_pipeline_at.isoformat() if self.last_auto_pipeline_at else None,
            "auto_pipeline_count_today": int(self.auto_pipeline_count_today or 0),
            "auto_pipeline_day": self.auto_pipeline_day,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class IngestEvent(Base):
    __tablename__ = "ingest_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(Integer, ForeignKey("ingest_sources.id"), nullable=False, index=True)
    fetched_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    item_url = Column(String(1024), nullable=True)
    item_hash = Column(String(64), nullable=True, index=True)
    status = Column(String(16), nullable=False, default="ok")  # ok | skip | fail
    error_msg = Column(String(1024), nullable=True)
    # P31-DB2: index for FK reverse lookups and FK validation on knowledge_nodes deletes.
    resulting_node_id = Column(Integer, ForeignKey("knowledge_nodes.id"), nullable=True, index=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "item_url": self.item_url,
            "item_hash": self.item_hash,
            "status": self.status,
            "error_msg": self.error_msg,
            "resulting_node_id": self.resulting_node_id,
        }


class AssetCache(Base):
    __tablename__ = "asset_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # P31-DB3: index for FK reverse lookups and faster FK validation on parent deletes.
    source_id = Column(Integer, ForeignKey("ingest_sources.id"), nullable=True, index=True)
    node_id = Column(Integer, ForeignKey("knowledge_nodes.id"), nullable=True, index=True)
    original_url = Column(String(1024), nullable=False, index=True)
    local_path = Column(String(1024), nullable=False)
    mime_type = Column(String(64), nullable=False, default="application/octet-stream")
    size_bytes = Column(Integer, nullable=False, default=0)
    # P6 — content sha256 for cross-source de-duplication + /api/assets/{hash} lookup.
    sha256 = Column(String(64), nullable=True, index=True)
    fetched_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "node_id": self.node_id,
            "original_url": self.original_url,
            "local_path": self.local_path,
            "mime_type": self.mime_type,
            "size_bytes": int(self.size_bytes or 0),
            "sha256": self.sha256,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }


# ---------------------------------------------------------------------------
# P4 — Alpha Lab chat sessions
# ---------------------------------------------------------------------------

class AlphaChatSession(Base):
    __tablename__ = "alpha_chat_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(512), nullable=False, default="New conversation")
    started_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_msg_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    message_count = Column(Integer, nullable=False, default=0)
    extracted_to_strategy_id = Column(Integer, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_msg_at": self.last_msg_at.isoformat() if self.last_msg_at else None,
            "message_count": int(self.message_count or 0),
            "extracted_to_strategy_id": self.extracted_to_strategy_id,
        }


class AlphaChatMessage(Base):
    __tablename__ = "alpha_chat_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer,
        ForeignKey("alpha_chat_sessions.id"),
        nullable=False,
        index=True,
    )
    role = Column(String(16), nullable=False)  # user | assistant | system
    content = Column(Text, nullable=False, default="")
    image_paths = Column(Text, nullable=False, default="[]")  # JSON list[str]
    tokens_in = Column(Integer, nullable=False, default=0)
    tokens_out = Column(Integer, nullable=False, default=0)
    ts = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def image_path_list(self) -> List[str]:
        try:
            data = json.loads(self.image_paths or "[]")
            return [str(p) for p in data if isinstance(p, str)]
        except (ValueError, TypeError):
            return []

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "image_paths": self.image_path_list(),
            "tokens_in": int(self.tokens_in or 0),
            "tokens_out": int(self.tokens_out or 0),
            "ts": self.ts.isoformat() if self.ts else None,
        }


# ---------------------------------------------------------------------------
# P6 — IC ledger, Granger edges, Telegram automatic report log
# ---------------------------------------------------------------------------

# Append-only ledger of every IC observation. Lets us reconstruct decay curves
# (B3) and feed time-series to Granger causality tests (B5). Following the
# "immutable ledger" rule from CLAUDE.md for high-risk derived data.
class IcHistory(Base):
    __tablename__ = "ic_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_id = Column(Integer, ForeignKey("knowledge_nodes.id"), nullable=False, index=True)
    ic_value = Column(Float, nullable=False)
    source = Column(String(32), nullable=False, default="decay")  # 'decay' | 'manual' | 'pipeline' | 'recompute'
    source_strategy_id = Column(Integer, nullable=True)
    recorded_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_ic_history_node_recorded", "node_id", "recorded_at"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "node_id": self.node_id,
            "ic_value": float(self.ic_value or 0.0),
            "source": self.source,
            "source_strategy_id": self.source_strategy_id,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None,
        }


# FinOps — append-only ledger of every settled LLM call's estimated cost. One
# row per call (agent / model / char-counts / USD estimate / strategy). Immutable:
# INSERT-only, never UPDATEd. Powers /api/cost/summary; pruned by retention task.
class LLMCostLedger(Base):
    __tablename__ = "llm_cost_ledger"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(Integer, nullable=True, index=True)
    agent = Column(String(32), nullable=False, default="unknown")
    model = Column(String(128), nullable=False, default="<default>")
    input_chars = Column(Integer, nullable=False, default=0)
    output_chars = Column(Integer, nullable=False, default=0)
    # est_cost_usd holds the BEST available cost: OpenRouter's real billed USD
    # when cost_source='openrouter', else the flat-pricing char estimate.
    est_cost_usd = Column(Float, nullable=False, default=0.0)
    # Real token counts from the provider's usage block (NULL when estimated).
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    cost_source = Column(String(16), nullable=True)  # 'openrouter' | 'estimate'
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "strategy_id": self.strategy_id,
            "agent": self.agent,
            "model": self.model,
            "input_chars": int(self.input_chars or 0),
            "output_chars": int(self.output_chars or 0),
            "input_tokens": (int(self.input_tokens) if self.input_tokens is not None else None),
            "output_tokens": (int(self.output_tokens) if self.output_tokens is not None else None),
            "est_cost_usd": float(self.est_cost_usd or 0.0),
            "cost_source": self.cost_source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# Derived Granger-causality p-values between high-IC KnowledgeNode pairs.
# Recomputed on a scheduled cadence; not in the hot path.
class GrangerEdge(Base):
    __tablename__ = "granger_edges"

    id = Column(Integer, primary_key=True, autoincrement=True)
    src_node_id = Column(Integer, ForeignKey("knowledge_nodes.id"), nullable=False)
    dst_node_id = Column(Integer, ForeignKey("knowledge_nodes.id"), nullable=False)
    p_value = Column(Float, nullable=False)
    lag = Column(Integer, nullable=False, default=1)
    sample_size = Column(Integer, nullable=False, default=0)
    computed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("src_node_id", "dst_node_id", "lag", name="ux_granger_pair_lag"),
        Index("ix_granger_pvalue", "p_value"),
        # P31-DB4: standalone indexes on FK columns so incoming-edge / outgoing-
        # edge lookups and node-delete FK validation use index scans instead of
        # full table scans.
        Index("ix_granger_edges_src_node_id", "src_node_id"),
        Index("ix_granger_edges_dst_node_id", "dst_node_id"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "src_node_id": self.src_node_id,
            "dst_node_id": self.dst_node_id,
            "p_value": float(self.p_value) if self.p_value is not None else 1.0,
            "lag": int(self.lag or 1),
            "sample_size": int(self.sample_size or 0),
            "computed_at": self.computed_at.isoformat() if self.computed_at else None,
        }


# Tracks every automated Telegram report send. Provides:
#  (a) idempotency across process restarts (avoid resending 6h report after a crash);
#  (b) audit trail for "when was the last morning briefing?" queries;
#  (c) per-report cooldown enforcement at the application layer.
class TelegramReportLog(Base):
    __tablename__ = "telegram_report_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_type = Column(String(32), nullable=False, index=True)  # 'six_hour' | 'morning' | 'paper_health' | 'test'
    sent_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    success = Column(Boolean, nullable=False, default=True, server_default="1")
    summary = Column(Text, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "report_type": self.report_type,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "success": bool(self.success),
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# P7 — Stage transition log + audit log + idempotency + factor + portfolio +
#       backtest sweep + manual order tables.
#
# All append-only, additive only. Backfill is performed in
# ``_run_lightweight_migrations`` so older deployments get a synthetic single
# row per existing strategy on first boot (enough to populate Sankey + funnel
# charts immediately).
# ---------------------------------------------------------------------------


class StageTransition(Base):
    """Append-only audit log of every (from_status → to_status) flip.

    Powers ``/pipeline-analytics`` (throughput, time-in-stage, gate pass-rate)
    and ``/alpha-flow`` (Sankey aggregate, single-strategy timeline).
    Written by :func:`backend.core.transition_log.record_transition` from
    orchestrator + promote/retire/pause endpoints. Never mutated.
    """

    __tablename__ = "stage_transitions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(
        Integer, ForeignKey("alpha_strategies.id"), nullable=False, index=True
    )
    from_status = Column(String(32), nullable=True)
    to_status = Column(String(32), nullable=False)
    from_stage = Column(Integer, nullable=True)
    to_stage = Column(Integer, nullable=False, default=0)
    transitioned_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    actor = Column(String(32), nullable=False, default="orchestrator")
    reason = Column(String(512), nullable=True)

    __table_args__ = (
        Index("ix_stage_transitions_strategy_at", "strategy_id", "transitioned_at"),
        Index("ix_stage_transitions_to_status_at", "to_status", "transitioned_at"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "strategy_id": self.strategy_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "from_stage": self.from_stage,
            "to_stage": int(self.to_stage or 0),
            "transitioned_at": self.transitioned_at.isoformat() if self.transitioned_at else None,
            "actor": self.actor,
            "reason": self.reason,
        }


class AuditLog(Base):
    """Universal audit trail for high-risk endpoints.

    Used by ``/live-trade/pause-all``, ``/live-trade/resume``,
    ``/trading-terminal/submit``, ``/trading-terminal/cancel``, and any future
    operator action that mutates the strategy book or paper-trade ledger.
    """

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    actor = Column(String(128), nullable=False, default="anonymous")
    action = Column(String(64), nullable=False, index=True)
    subject_type = Column(String(64), nullable=True)
    subject_id = Column(String(64), nullable=True)
    payload_json = Column(Text, nullable=True)
    response_json = Column(Text, nullable=True)
    request_ip = Column(String(64), nullable=True)
    user_agent = Column(String(512), nullable=True)
    idempotency_key = Column(String(80), nullable=True, index=True)
    success = Column(Boolean, nullable=False, default=True, server_default="1")
    error_text = Column(Text, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "actor": self.actor,
            "action": self.action,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "payload": _safe_load_json(self.payload_json),
            "response": _safe_load_json(self.response_json),
            "request_ip": self.request_ip,
            "user_agent": self.user_agent,
            "idempotency_key": self.idempotency_key,
            "success": bool(self.success),
            "error_text": self.error_text,
        }


class IdempotencyKey(Base):
    """One row per processed Idempotency-Key. TTL purged.

    Powers :mod:`backend.core.idempotency` — see its docstring for the
    end-to-end protocol. Composite uniqueness on ``key`` means concurrent
    inserts deterministically pick one winner.
    """

    __tablename__ = "idempotency_keys"

    key = Column(String(80), primary_key=True)
    request_hash = Column(String(64), nullable=False)
    response_json = Column(Text, nullable=False, default="null")
    status_code = Column(Integer, nullable=False, default=200)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class Factor(Base):
    """Reusable named factor formula (P7 — /factor-studio).

    Decouples factor definitions from full strategies so users can iterate on
    formulas without spinning up a strategy row per attempt. Promoting a
    Factor to a Strategy is one-way (see ``/api/factor-studio/promote``).
    """

    __tablename__ = "factors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(256), nullable=False, unique=True, index=True)
    formula_code = Column(Text, nullable=False)
    dsl_version = Column(String(16), nullable=False, default="py-v1")
    author = Column(String(128), nullable=False, default="user")
    ic_score_cached = Column(Float, nullable=False, default=0.0)
    sharpe_cached = Column(Float, nullable=False, default=0.0)
    asset_symbol = Column(String(32), nullable=False, default="BTC")
    period_start = Column(DateTime(timezone=True), nullable=True)
    period_end = Column(DateTime(timezone=True), nullable=True)
    params_json = Column(Text, nullable=False, default="{}")
    # P31-DB9: index for FK reverse lookups (find-the-factor-that-was-promoted)
    # and FK validation on alpha_strategies deletes.
    promoted_strategy_id = Column(
        Integer, ForeignKey("alpha_strategies.id"), nullable=True, index=True
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "formula_code": self.formula_code or "",
            "dsl_version": self.dsl_version,
            "author": self.author,
            "ic_score_cached": float(self.ic_score_cached or 0.0),
            "sharpe_cached": float(self.sharpe_cached or 0.0),
            "asset_symbol": self.asset_symbol,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "params": _safe_load_json(self.params_json) or {},
            "promoted_strategy_id": self.promoted_strategy_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Portfolio(Base):
    """Named saved combination of N strategies (P7 — /portfolio-optimizer)."""

    __tablename__ = "portfolios"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(256), nullable=False, unique=True)
    strategy_ids_json = Column(Text, nullable=False, default="[]")
    weights_json = Column(Text, nullable=False, default="{}")
    method = Column(String(64), nullable=False, default="equal_weight")
    constraints_json = Column(Text, nullable=False, default="{}")
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "strategy_ids": _safe_load_json(self.strategy_ids_json) or [],
            "weights": _safe_load_json(self.weights_json) or {},
            "method": self.method,
            "constraints": _safe_load_json(self.constraints_json) or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class BacktestSweep(Base):
    """One parameter-sweep run (P7 — /backtest-lab).

    ``results_json`` stores only the per-cell metric summary (no equity curves)
    so a 100-cell sweep stays well under 25 KB.
    """

    __tablename__ = "backtest_sweeps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(
        Integer, ForeignKey("alpha_strategies.id"), nullable=False, index=True
    )
    param_x_name = Column(String(64), nullable=False)
    param_x_values = Column(Text, nullable=False, default="[]")
    param_y_name = Column(String(64), nullable=True)
    param_y_values = Column(Text, nullable=True)
    cells_total = Column(Integer, nullable=False, default=0)
    cells_done = Column(Integer, nullable=False, default=0)
    status = Column(String(16), nullable=False, default="queued")
    results_json = Column(Text, nullable=False, default="[]")
    error_message = Column(Text, nullable=True)
    seed = Column(Integer, nullable=False, default=42)
    duration_ms = Column(Integer, nullable=False, default=0)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_backtest_sweeps_strategy_created", "strategy_id", "created_at"),
    )

    def to_dict(self) -> dict:
        return {
            "sweep_id": self.id,
            "strategy_id": self.strategy_id,
            "param_x_name": self.param_x_name,
            "param_x_values": _safe_load_json(self.param_x_values) or [],
            "param_y_name": self.param_y_name,
            "param_y_values": _safe_load_json(self.param_y_values) if self.param_y_values else None,
            "cells_total": int(self.cells_total or 0),
            "cells_done": int(self.cells_done or 0),
            "status": self.status,
            "cells": _safe_load_json(self.results_json) or [],
            "error_message": self.error_message,
            "seed": int(self.seed or 42),
            "duration_ms": int(self.duration_ms or 0),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ManualOrder(Base):
    """One manual order placed via the trading terminal (P7 — /trading-terminal).

    Lives alongside (not inside) the strategy-driven paper trade engine. All
    inserts go through an idempotent endpoint guarded by
    :mod:`backend.core.idempotency`.
    """

    __tablename__ = "manual_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_uid = Column(String(40), nullable=False, unique=True, index=True)
    terminal_session_id = Column(String(40), nullable=True, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    side = Column(String(8), nullable=False)             # buy | sell
    order_type = Column(String(8), nullable=False)       # market | limit | stop
    qty = Column(Float, nullable=False)
    limit_price = Column(Float, nullable=True)
    stop_price = Column(Float, nullable=True)
    tif = Column(String(8), nullable=False, default="gtc")  # gtc | ioc | fok
    mode = Column(String(8), nullable=False, default="paper")  # paper | live
    status = Column(String(16), nullable=False, default="pending", index=True)
    placed_by = Column(String(64), nullable=False, default="manual_terminal")
    notes = Column(Text, nullable=True)
    requested_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    decided_at = Column(DateTime(timezone=True), nullable=True)
    request_ip = Column(String(64), nullable=True)
    request_user_agent = Column(String(512), nullable=True)
    idempotency_key = Column(String(80), nullable=True, index=True)
    # P18/D3 — venue order ID for future live-trade correlation with exchange
    # fills (Binance/OKX/Bybit). Nullable + indexed; not UNIQUE since the
    # column will sit empty until exchange SDK lands and we want no friction
    # for paper orders.
    exchange_order_id = Column(String(128), nullable=True, index=True)

    __table_args__ = (
        Index("ix_manual_orders_symbol_status", "symbol", "status"),
    )

    def to_dict(self) -> dict:
        return {
            "order_uid": self.order_uid,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "qty": float(self.qty or 0.0),
            "limit_price": float(self.limit_price) if self.limit_price is not None else None,
            "stop_price": float(self.stop_price) if self.stop_price is not None else None,
            "tif": self.tif,
            "mode": self.mode,
            "status": self.status,
            "placed_by": self.placed_by,
            "notes": self.notes,
            "requested_at": self.requested_at.isoformat() if self.requested_at else None,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }


class ManualFill(Base):
    """One fill against a manual order. Append-only."""

    __tablename__ = "manual_fills"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("manual_orders.id"), nullable=False, index=True)
    filled_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    filled_qty = Column(Float, nullable=False)
    filled_price = Column(Float, nullable=False)
    fee_quote = Column(Float, nullable=False, default=0.0)
    fee_bps = Column(Float, nullable=False, default=0.0)
    is_maker = Column(Boolean, nullable=False, default=False, server_default="0")
    slippage_bps = Column(Float, nullable=False, default=0.0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "order_id": self.order_id,
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
            "filled_qty": float(self.filled_qty or 0.0),
            "filled_price": float(self.filled_price or 0.0),
            "fee_quote": float(self.fee_quote or 0.0),
            "fee_bps": float(self.fee_bps or 0.0),
            "is_maker": bool(self.is_maker),
            "slippage_bps": float(self.slippage_bps or 0.0),
        }


class ManualPosition(Base):
    """Current manual-terminal position per (symbol, mode). Mutated by fills.

    ``qty_signed`` is positive for long, negative for short. ``avg_entry_price``
    is the volume-weighted entry of the *open* portion (reset to current fill
    price when the position flips through flat).
    """

    __tablename__ = "manual_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False)
    mode = Column(String(8), nullable=False, default="paper")
    qty_signed = Column(Float, nullable=False, default=0.0)
    avg_entry_price = Column(Float, nullable=False, default=0.0)
    realized_pnl_quote = Column(Float, nullable=False, default=0.0)
    last_update_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("symbol", "mode", name="ux_manual_position_symbol_mode"),
    )

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "mode": self.mode,
            "qty_signed": float(self.qty_signed or 0.0),
            "avg_entry_price": float(self.avg_entry_price or 0.0),
            "realized_pnl_quote": float(self.realized_pnl_quote or 0.0),
            "last_update_at": self.last_update_at.isoformat() if self.last_update_at else None,
        }


class SimAccount(Base):
    """A strategy pinned to forward simulation (P-SIM — /sim-account).

    Unlike the trailing-window paper_trader, a SIM account fixes ``start_bar_ts``
    at pin time and only scores bars AFTER it — a genuine walk-forward as ingest
    advances. SIMULATION ONLY: never routes to a venue, never touches live.

    Position + cash are NOT stored here as authoritative balances; they are
    derived by folding the append-only ``sim_fills`` + ``sim_funding`` ledgers
    (high-risk-system rule). This row is account config + the cursor
    (``last_bar_ts``) for idempotent incremental ticking.
    """

    __tablename__ = "sim_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(
        Integer, ForeignKey("alpha_strategies.id"), nullable=False, unique=True, index=True
    )
    symbol = Column(String(32), nullable=False, default="BTC")
    status = Column(String(16), nullable=False, default="active", index=True)  # active|stopped|liquidated
    initial_capital = Column(Float, nullable=False, default=100000.0)
    fee_bps = Column(Float, nullable=False, default=0.0005)        # per side
    slippage_bps = Column(Float, nullable=False, default=0.0002)   # per side
    started_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    start_bar_ts = Column(DateTime(timezone=True), nullable=False)  # forward bars are > this
    last_bar_ts = Column(DateTime(timezone=True), nullable=True)    # tick cursor
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "strategy_id": int(self.strategy_id),
            "symbol": self.symbol,
            "status": self.status,
            "initial_capital": float(self.initial_capital or 0.0),
            "fee_bps": float(self.fee_bps or 0.0),
            "slippage_bps": float(self.slippage_bps or 0.0),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "start_bar_ts": self.start_bar_ts.isoformat() if self.start_bar_ts else None,
            "last_bar_ts": self.last_bar_ts.isoformat() if self.last_bar_ts else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class SimFill(Base):
    """Append-only simulated fill for a SIM account. One row per position change.

    ``UNIQUE(account_id, bar_ts)`` makes re-ticking a processed bar a guaranteed
    no-op at the DB layer (no double-fill); the factor's state machine produces
    at most one position change per bar so the constraint is never a false block.
    """

    __tablename__ = "sim_fills"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("sim_accounts.id"), nullable=False, index=True)
    bar_ts = Column(DateTime(timezone=True), nullable=False)
    side = Column(String(8), nullable=False)             # buy | sell
    signed_qty_delta = Column(Float, nullable=False)     # change in signed base qty
    price = Column(Float, nullable=False)                # fill price incl slippage
    fee_quote = Column(Float, nullable=False, default=0.0)
    slippage_bps = Column(Float, nullable=False, default=0.0)
    reason = Column(String(16), nullable=False, default="entry")  # entry|exit|flip|rebalance
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("account_id", "bar_ts", name="ux_sim_fills_acct_bar"),
    )

    def to_dict(self) -> dict:
        return {
            "bar_ts": self.bar_ts.strftime("%Y-%m-%dT%H:%M:%SZ") if self.bar_ts else None,
            "side": self.side,
            "signed_qty_delta": float(self.signed_qty_delta or 0.0),
            "price": float(self.price or 0.0),
            "fee_quote": float(self.fee_quote or 0.0),
            "slippage_bps": float(self.slippage_bps or 0.0),
            "reason": self.reason,
        }


class SimFunding(Base):
    """Append-only funding settlement for a SIM account (8h perp funding).

    ``cashflow = -signed_notional * funding_rate`` — a long pays when funding is
    positive, a short receives. ``UNIQUE(account_id, bar_ts)`` keeps re-ticks
    idempotent.
    """

    __tablename__ = "sim_funding"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("sim_accounts.id"), nullable=False, index=True)
    bar_ts = Column(DateTime(timezone=True), nullable=False)
    funding_rate = Column(Float, nullable=False, default=0.0)
    position_notional = Column(Float, nullable=False, default=0.0)  # signed
    cashflow_quote = Column(Float, nullable=False, default=0.0)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("account_id", "bar_ts", name="ux_sim_funding_acct_bar"),
    )

    def to_dict(self) -> dict:
        return {
            "bar_ts": self.bar_ts.strftime("%Y-%m-%dT%H:%M:%SZ") if self.bar_ts else None,
            "funding_rate": float(self.funding_rate or 0.0),
            "position_notional": float(self.position_notional or 0.0),
            "cashflow_quote": float(self.cashflow_quote or 0.0),
        }


def _safe_load_json(raw: Optional[str]) -> Optional[object]:
    """Best-effort json.loads with None fallback on parse failure."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create all tables if they don't exist, then run lightweight column migrations.

    Idempotent — safe to call on every process start. The column-migration step
    handles columns added after the original schema (e.g. `kind` on
    `knowledge_nodes`) without requiring Alembic.
    """
    Base.metadata.create_all(bind=engine)
    _run_lightweight_migrations()
    # P13/D-H2 — run production data migrations (dedup duplicate content_hash
    # rows, then install the UNIQUE index). Idempotent: tracked via the
    # ``_schema_meta`` table so subsequent boots skip applied migrations.
    from backend.core.schema_migrations import run_pending as _run_p13_migrations
    _run_p13_migrations(engine)


def _run_lightweight_migrations() -> None:
    """Apply additive column changes for tables that pre-existed.

    Until Alembic is introduced in P3, this is our forward-only migration path.
    Every change MUST be idempotent and additive (no renames, no type changes).
    """
    # Local import to avoid circular ref at module load time.
    from backend.core.schema_migrations import add_index_if_missing, ensure_columns

    ensure_columns(
        engine,
        [
            (
                "knowledge_nodes",
                "kind",
                "VARCHAR(32)",
                {"not_null": True, "default_sql": "'concept'"},
            ),
            # P3 — provenance + dedup metadata
            ("knowledge_nodes", "source_url", "VARCHAR(1024)", {}),
            ("knowledge_nodes", "source_type", "VARCHAR(32)", {}),
            ("knowledge_nodes", "category", "VARCHAR(64)", {}),
            ("knowledge_nodes", "content_hash", "VARCHAR(64)", {}),
            ("knowledge_nodes", "ingested_at", "DATETIME", {}),
            # P4 — postmortem linkage
            ("knowledge_nodes", "origin_strategy_id", "INTEGER", {}),
            # P5 — /sources top-tab categorisation
            ("ingest_sources", "category", "VARCHAR(32)", {}),
            # P6 — auto-pipeline / queue / IC-decay bookkeeping
            ("knowledge_nodes", "auto_pipeline_strategy_id", "INTEGER", {}),
            ("knowledge_nodes", "last_queue_eval_at", "DATETIME", {}),
            ("knowledge_nodes", "ic_decayed_at", "DATETIME", {}),
            # T1-B — summary-first memory (all nullable, no backfill required).
            ("knowledge_nodes", "summary", "TEXT", {}),
            ("knowledge_nodes", "summary_generated_at", "DATETIME", {}),
            ("knowledge_nodes", "confidence", "FLOAT", {}),
            # T2-B — confidence-decay revisit flag (nullable).
            ("knowledge_nodes", "revisit_flagged_at", "DATETIME", {}),
            # T1-B — record-state lifecycle. NOT-NULL is safe ONLY because the
            # default is a CONSTANT literal ('active') — SQLite forbids
            # non-constant defaults on ADD COLUMN, but constants are fine
            # (same proven pattern as ``kind`` above). All legacy rows backfill
            # to 'active' at the metadata level; no row rewrite.
            (
                "knowledge_nodes",
                "status",
                "VARCHAR(16)",
                {"not_null": True, "default_sql": "'active'"},
            ),
            ("ingest_sources", "last_auto_pipeline_at", "DATETIME", {}),
            (
                "ingest_sources",
                "auto_pipeline_count_today",
                "INTEGER",
                {"not_null": True, "default_sql": "0"},
            ),
            ("ingest_sources", "auto_pipeline_day", "VARCHAR(10)", {}),
            # P6 — image cache content hash for /api/assets/{hash} lookup
            ("asset_cache", "sha256", "VARCHAR(64)", {}),
            # T-FINOPS round-3 — real OpenRouter usage on the cost ledger. The
            # table is created by create_all; these ADD COLUMNs only matter for a
            # DB where llm_cost_ledger was first created without them (all nullable).
            ("llm_cost_ledger", "input_tokens", "INTEGER", {}),
            ("llm_cost_ledger", "output_tokens", "INTEGER", {}),
            ("llm_cost_ledger", "cost_source", "VARCHAR(16)", {}),
        ],
    )
    # Index on ingest_events.fetched_at so the events_24h aggregate on
    # /api/sources stays cheap as the event table grows.
    add_index_if_missing(engine, "ingest_events", ["fetched_at"])
    # P6 — supporting indexes for new lookups
    add_index_if_missing(engine, "asset_cache", ["sha256"], name="ix_asset_cache_sha256")
    add_index_if_missing(
        engine,
        "knowledge_nodes",
        ["auto_pipeline_strategy_id"],
        name="ix_knowledge_nodes_auto_pipe",
    )
    add_index_if_missing(
        engine,
        "knowledge_nodes",
        ["ic_score"],
        name="ix_knowledge_nodes_ic_score",
    )
    # T1-B — status filter (archived/revisit) for retrieval queries.
    add_index_if_missing(
        engine,
        "knowledge_nodes",
        ["status"],
        name="ix_knowledge_nodes_status",
    )
    # T2-B — partial index over only the flagged rows so the revisit
    # re-surfacing query stays an index scan, never a full table scan.
    try:
        from backend.core.schema_migrations import _has_index as _has_revisit_idx
        _revisit_idx = "ix_knowledge_nodes_revisit_flagged_at"
        if not _has_revisit_idx(engine, _revisit_idx):
            with engine.begin() as _conn:
                _conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS {_revisit_idx} "
                    "ON knowledge_nodes(revisit_flagged_at) "
                    "WHERE revisit_flagged_at IS NOT NULL"
                ))
    except Exception:  # noqa: BLE001
        import logging as _lg
        _lg.getLogger("alpha.schema").exception(
            "T2-B: partial idx revisit_flagged_at failed"
        )

    # P7 — supporting indexes for new analytics + ops tables. Tables
    # themselves are created by ``Base.metadata.create_all`` above; only
    # add_index_if_missing handles indexes idempotently.
    add_index_if_missing(
        engine,
        "audit_logs",
        ["action", "created_at"],
        name="ix_audit_logs_action_created",
    )
    add_index_if_missing(
        engine,
        "manual_orders",
        ["requested_at"],
        name="ix_manual_orders_requested_at",
    )

    # P29-T8: hot-path indexes (telegram_reports, mission_panel, live_trade,
    # ingest, genealogy, report dedup).
    add_index_if_missing(
        engine, "alpha_strategies", ["status", "updated_at"],
        name="ix_alpha_strategies_status_updated",
    )
    add_index_if_missing(
        engine, "alpha_strategies", ["updated_at"],
        name="ix_alpha_strategies_updated_at",
    )
    add_index_if_missing(
        engine, "ingest_events", ["source_id", "fetched_at"],
        name="ix_ingest_events_source_fetched",
    )
    add_index_if_missing(
        engine, "ingest_events", ["status", "fetched_at"],
        name="ix_ingest_events_status_fetched",
    )
    # Partial index — only non-null origin_strategy_id (genealogy hot path).
    try:
        from backend.core.schema_migrations import _has_index as _has_idx
        _idx_name = "ix_knowledge_nodes_origin_strategy_id"
        if not _has_idx(engine, _idx_name):
            with engine.begin() as _conn:
                _conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS {_idx_name} "
                    "ON knowledge_nodes(origin_strategy_id) "
                    "WHERE origin_strategy_id IS NOT NULL"
                ))
    except Exception:  # noqa: BLE001
        import logging as _lg
        _lg.getLogger("alpha.schema").exception(
            "P29-T8: partial idx origin_strategy_id failed"
        )
    add_index_if_missing(
        engine, "telegram_report_log", ["report_type", "sent_at"],
        name="ix_telegram_report_log_type_sent",
    )

    # P7 — backfill stage_transitions on first boot: one synthetic row per
    # strategy that has no transition history yet, anchored at
    # ``alpha_strategies.updated_at``. Gives Sankey + funnel charts something
    # to plot immediately. Marked actor='backfill' so analytics queries can
    # exclude these synthetic rows from time-in-stage calculations.
    # P15/D-L14 — capped at 5000 rows so the LIMIT below doesn't tie up boot
    # for huge installations. Strategies with id > 5000 won't get backfilled;
    # operators wanting full coverage can DELETE FROM stage_transitions and
    # reboot, or run a one-off SQL backfill matching this query manually.
    _backfill_stage_transitions()


def _backfill_stage_transitions() -> None:
    """Seed ``stage_transitions`` from existing ``alpha_strategies`` rows.

    Idempotent — only runs when the table is empty AND at least one strategy
    exists. Safe to call on every boot.
    """
    try:
        with engine.begin() as conn:
            count_row = conn.execute(
                text("SELECT COUNT(*) AS n FROM stage_transitions")
            ).first()
            if count_row and int(count_row[0]) > 0:
                return
            # P15/D-M4 — prefer created_at when present (added in P15 migration),
            # falling back to updated_at for legacy rows that never carried
            # an honest creation timestamp.
            existing_strats = conn.execute(
                text(
                    "SELECT id, status, stage, COALESCE(created_at, updated_at) AS ts "
                    "FROM alpha_strategies "
                    "ORDER BY id ASC LIMIT 5000"
                )
            ).fetchall()
            if not existing_strats:
                return
            insert_sql = text(
                "INSERT INTO stage_transitions "
                "(strategy_id, from_status, to_status, from_stage, to_stage, "
                " transitioned_at, actor, reason) "
                "VALUES (:sid, NULL, :st, NULL, :sg, :ts, 'backfill', "
                "        'Synthesized from updated_at at first P7 boot')"
            )
            for row in existing_strats:
                sid = int(row[0])
                status = (row[1] or "INTAKE")[:32]
                stage = int(row[2] or 0)
                ts = row[3] or datetime.now(timezone.utc)
                conn.execute(insert_sql, {
                    "sid": sid, "st": status, "sg": stage, "ts": ts,
                })
    except Exception:  # noqa: BLE001
        # Best-effort — backfill is a UX nicety, never block boot.
        import logging
        logging.getLogger("alpha.schema").exception("backfill_stage_transitions failed")


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context-managed session for scripts and orchestrator workflows."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency — yields a SQLAlchemy Session for the duration of a
    single HTTP request.

    CONTRACT (caller responsibility):
        Callers **must** call ``db.commit()`` explicitly after mutating ORM
        objects.  Unlike ``session_scope()``, this dependency does *not*
        auto-commit on success.  Forgetting ``db.commit()`` causes all writes
        to be silently discarded when the session is closed at request teardown.

    Development guard:
        Set env var ``ALPHA_DEBUG_DB=1`` to enable a warning log whenever the
        dependency closes a session that still has uncommitted changes.  The
        guard is off by default so production deployments are unaffected.
    """
    import logging as _logging
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        # Dev-mode guard: warn if caller forgot db.commit().
        # Checked only when ALPHA_DEBUG_DB=1; no-op in production by default.
        _debug_db = str(os.environ.get("ALPHA_DEBUG_DB", "") or "").strip().lower()
        if _debug_db in {"1", "true", "yes", "on"}:
            if session.new or session.dirty or session.deleted:
                _logging.getLogger("alpha.db").warning(
                    "get_db: session closed with uncommitted changes "
                    "(new=%d dirty=%d deleted=%d) — did you forget db.commit()?",
                    len(session.new),
                    len(session.dirty),
                    len(session.deleted),
                )
        session.close()


# Auto-create on import so first-run scripts never hit "no such table".
# P15/D-M2 — gated behind ALPHA_AUTO_INIT_DB=1 (default ON for back-compat) so
# deployments managing schema externally (Alembic, manual SQL) can disable with
# ALPHA_AUTO_INIT_DB=0.
from backend._envloader import env_bool as _env_bool_init
if _env_bool_init("ALPHA_AUTO_INIT_DB", True):
    init_db()


__all__ = [
    "Base",
    "KnowledgeNode",
    "AlphaStrategy",
    "IngestSource",
    "IngestEvent",
    "AssetCache",
    "AlphaChatSession",
    "AlphaChatMessage",
    "IcHistory",
    "LLMCostLedger",
    "GrangerEdge",
    "TelegramReportLog",
    "KIND_CONCEPT",
    "KIND_PAST_ALPHA",
    "KIND_POSTMORTEM",
    "KIND_ACTIVE",
    "KIND_CHOICES",
    "KB_STATUS_ACTIVE",
    "KB_STATUS_STUB",
    "KB_STATUS_DRAFT",
    "KB_STATUS_REVISIT",
    "KB_STATUS_ARCHIVED",
    "KB_STATUS_CHOICES",
    "SOURCE_TYPES",
    "SOURCE_CATEGORIES",
    "engine",
    "SessionLocal",
    "session_scope",
    "get_db",
    "init_db",
    "slugify",
    "_slug",
    "DATA_DIR",
    "PROJECT_ROOT",
]

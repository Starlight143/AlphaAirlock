"""IC (Information Coefficient) ledger + decay (P6-B3).

Why a ledger
------------
The reference YouTube system shows IC as a *time series*, not a static field.
Concepts get hotter and colder; we want both the current cached value
(``KnowledgeNode.ic_score``) and the audit trail of how it got there.

This module owns the ``ic_history`` table. Every IC mutation flows through
``record_ic_snapshot`` so the table stays append-only — the same "immutable
ledger" pattern CLAUDE.md mandates for credit / payment systems, applied here
because IC drives both the alpha queue (B2) and Granger pairs (B5).

Decay
-----
``decay_all_ic`` is the nightly batch hook used by ``periodic_tasks.py``. It
applies exponential decay (half-life configurable via env, default 30 days) and
writes one ``ic_history`` row per node with ``source='decay'``. Decayed values
floor at ``KB_IC_FLOOR`` (default 0.05) so old concepts don't disappear from the
queue entirely.

Default behaviour
-----------------
* ``decay_all_ic`` is gated by ``IC_DECAY_ENABLED`` (default OFF). Calling it
  with the env off returns immediately — no DB writes.
* The endpoint reads (``get_history``) work without the flag — viewing history
  doesn't trigger any background work.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

from sqlalchemy import update as _sa_update
from sqlalchemy.orm import Session

from backend._envloader import env_bool, env_float
from backend.core.database import IcHistory, KnowledgeNode, session_scope

logger = logging.getLogger("alpha.ic_history")


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite returns naive datetimes even on Column(DateTime(timezone=True)).
    Coerce to UTC-aware so arithmetic with ``datetime.now(timezone.utc)`` works.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def is_decay_enabled() -> bool:
    return env_bool("IC_DECAY_ENABLED", False)


def _half_life_days() -> float:
    return env_float("KB_IC_HALF_LIFE_DAYS", 30.0, minimum=1.0)


def _floor() -> float:
    return env_float("KB_IC_FLOOR", 0.05, minimum=0.0)


# T2-B — confidence-decay → revisit loop. DISTINCT from IC decay: ``confidence``
# is a [0,1] agent-trust score that decays toward a floor when a node's live/OOS
# outcome is poor, lifting slightly on good outcomes. A past_alpha/postmortem
# whose confidence falls below the revisit threshold is flagged + status='revisit'
# so the Researcher re-surfaces it as suspect. Gated OFF by default (additive).
def confidence_decay_enabled() -> bool:
    return env_bool("KB_CONFIDENCE_DECAY_ENABLED", False)


def _conf_decay_factor() -> float:
    return env_float("KB_CONF_DECAY_FACTOR", 0.7, minimum=0.0, maximum=1.0)


def _conf_reward() -> float:
    return env_float("KB_CONF_REWARD", 0.1, minimum=0.0, maximum=1.0)


def _conf_floor() -> float:
    return env_float("KB_CONF_FLOOR", 0.05, minimum=0.0, maximum=1.0)


def _conf_revisit_threshold() -> float:
    return env_float("KB_CONF_REVISIT_THRESHOLD", 0.25, minimum=0.0, maximum=1.0)


def _conf_bad_ic() -> float:
    return env_float("KB_CONF_BAD_IC", 0.0, minimum=-1.0, maximum=1.0)


def _conf_good_ic() -> float:
    return env_float("KB_CONF_GOOD_IC", 0.02, minimum=-1.0, maximum=1.0)


def _confidence_from(conf: Optional[float], ic_score: Optional[float]) -> float:
    """NULL-safe current confidence: stored value, else ic_score mapped to [0,1]."""
    if conf is not None:
        return max(0.0, min(1.0, float(conf)))
    return max(0.0, min(1.0, (float(ic_score or 0.0) + 1.0) / 2.0))


def record_ic_snapshot(
    session: Session,
    node_id: int,
    ic_value: float,
    *,
    source: str = "manual",
    source_strategy_id: Optional[int] = None,
) -> IcHistory:
    """Append a single observation. Caller is responsible for the surrounding
    ``session.commit()`` if needed.

    ``ic_value`` is an Information Coefficient — a correlation-derived statistic
    mathematically bounded to [-1, 1]. We defensively clamp at the ledger
    boundary (per CLAUDE.md's IC/correlation clamping rule) so a single
    mis-clamped caller cannot poison Granger inputs (``granger._series_for``)
    or any downstream IC-floor comparison. Non-finite inputs (NaN/Inf) coerce
    to 0.0 rather than persisting a corrupt row.
    """
    try:
        _v = float(ic_value)
    except (TypeError, ValueError):
        _v = 0.0
    if not math.isfinite(_v):
        _v = 0.0
    _v = max(-1.0, min(1.0, _v))
    row = IcHistory(
        node_id=int(node_id),
        ic_value=_v,
        source=str(source)[:32] or "manual",
        source_strategy_id=int(source_strategy_id) if source_strategy_id else None,
    )
    session.add(row)
    return row


def get_history(session: Session, node_id: int, days: int = 90) -> List[dict]:
    """Return ascending-time IC observations for the node."""
    days = max(1, min(3650, int(days)))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        session.query(IcHistory)
        .filter(IcHistory.node_id == int(node_id))
        .filter(IcHistory.recorded_at >= cutoff)
        .order_by(IcHistory.recorded_at.asc())
        .all()
    )
    return [r.to_dict() for r in rows]


def decay_all_ic(session: Optional[Session] = None) -> dict:
    """Apply exponential IC decay to every KnowledgeNode.

    Algorithm: ``new_ic = max(floor, old_ic * 0.5 ** (days_since_decay / half_life))``.
    First-ever decay for a node uses the gap from ``created_at`` so freshly
    inserted nodes age normally from creation, not from never.

    Idempotency: a per-node ``ic_decayed_at`` < 12h ago short-circuits to no-op,
    so accidental re-runs in the same day don't compound decay.

    Returns a stats dict for logging. Designed to be invoked by
    ``periodic_tasks.py``; opens its own session if none provided.
    """
    if not is_decay_enabled():
        return {"enabled": False, "decayed": 0, "skipped": 0}

    own_session = session is None
    stats = {"enabled": True, "decayed": 0, "skipped": 0, "floored": 0}
    half_life = _half_life_days()
    floor = _floor()
    now = datetime.now(timezone.utc)

    def _run(s: Session) -> None:
        # P29-C4: load only the columns needed (skip 32KB content body).
        # Bulk update() bypasses identity map for memory + GC efficiency.
        # NOTE: yield_per(1000) was previously used here for memory efficiency
        # (P32-D11 / MEM32-13), but on SQLite the pysqlite driver does not
        # support concurrent statements on a single connection. Any DML
        # (UPDATE/INSERT) issued inside the iteration loop closes the open
        # SELECT cursor, silently truncating decay to the first 1000 rows on
        # tables larger than that batch. We materialise the 4-column tuples
        # (no ORM identity map overhead, no 32KB content body) into a plain
        # Python list first, close the cursor, then iterate and issue all DML.
        # For very large tables the env var IC_DECAY_MAX_ROWS caps the fetch.
        import os as _os
        _max_rows = int(_os.environ.get("IC_DECAY_MAX_ROWS", 0)) or None
        _q = s.query(
            KnowledgeNode.id,
            KnowledgeNode.ic_score,
            KnowledgeNode.ic_decayed_at,
            KnowledgeNode.created_at,
        )
        if _max_rows:
            _q = _q.limit(_max_rows)
        rows = _q.all()
        for nid, ic_score, ic_decayed_at, created_at in rows:
            last = _as_utc(ic_decayed_at) or _as_utc(created_at) or now
            elapsed_days = max(0.0, (now - last).total_seconds() / 86400.0)
            if elapsed_days < 0.5:
                stats["skipped"] += 1
                continue
            old = float(ic_score or 0.0)
            if old <= floor + 1e-12:
                s.execute(
                    _sa_update(KnowledgeNode)
                    .where(KnowledgeNode.id == nid)
                    .values(ic_decayed_at=now)
                )
                # If the stored score is genuinely below floor (e.g., a negative
                # IC written by record_pipeline_outcomes), raise it to floor now.
                # Without this, the node exits the floored branch with its
                # sub-floor value intact in the DB; the next pass hits the 12h
                # idempotency gate and treats the node as skipped, silently
                # freezing the negative score in perpetuity.
                if old < floor - 1e-9:
                    s.execute(
                        _sa_update(KnowledgeNode)
                        .where(KnowledgeNode.id == nid)
                        .values(ic_score=float(floor))
                    )
                # Only write the snapshot the first time this node is clamped
                # (when old > floor). Subsequent passes where old is already
                # at floor are no-ops for the ledger — only the timestamp
                # anchor is advanced to prevent re-triggering on the next run.
                if abs(old - floor) > 1e-9:
                    record_ic_snapshot(s, nid, old, source="decay")
                stats["floored"] += 1
                continue
            decay_factor = math.pow(0.5, elapsed_days / half_life)
            new_val = max(floor, old * decay_factor)
            # P30-R13: always advance ic_decayed_at even when value didn't
            # change. Previously continue skipped both score AND timestamp
            # update; the next pass then computed elapsed_days against an
            # unboundedly stale anchor, causing instant near-zero collapse
            # the moment IC rose above the near-floor zone.
            if abs(new_val - old) < 1e-9:
                s.execute(
                    _sa_update(KnowledgeNode)
                    .where(KnowledgeNode.id == nid)
                    .values(ic_decayed_at=now)
                )
                stats["skipped"] += 1
                continue
            s.execute(
                _sa_update(KnowledgeNode)
                .where(KnowledgeNode.id == nid)
                .values(ic_score=float(new_val), ic_decayed_at=now)
            )
            record_ic_snapshot(s, nid, new_val, source="decay")
            stats["decayed"] += 1
        s.flush()

    if own_session:
        with session_scope() as s:
            _run(s)
    else:
        _run(session)
    logger.info("IC decay pass complete: %s", stats)
    return stats


def record_pipeline_outcomes(
    node_ids: Iterable[int],
    *,
    strategy_id: int,
    ic_value: float,
    session: Optional[Session] = None,
) -> int:
    """When a strategy lands APPROVED/REJECTED, write an IC observation against
    every source node referenced by its config. Used by orchestrator hooks
    (called from B1 auto-pipeline + manual /api/pipeline/run flows).
    """
    ids = [int(x) for x in node_ids if x]
    if not ids:
        return 0
    own_session = session is None

    def _run(s: Session) -> int:
        now = datetime.now(timezone.utc)
        count = 0
        # T2-B — when confidence decay is on, pre-load (confidence, ic_score,
        # kind, status) for all source nodes in ONE materialised query, then
        # fold confidence + revisit-flag writes into the SAME per-node UPDATE
        # below. Materialise-then-DML respects the pysqlite single-cursor rule
        # (no second SELECT inside the loop).
        decay_on = confidence_decay_enabled()
        conf_map: dict = {}
        if decay_on:
            for nid, conf, ic_s, kind, status in (
                s.query(
                    KnowledgeNode.id, KnowledgeNode.confidence,
                    KnowledgeNode.ic_score, KnowledgeNode.kind, KnowledgeNode.status,
                ).filter(KnowledgeNode.id.in_(ids)).all()
            ):
                conf_map[int(nid)] = (conf, ic_s, kind, status)
        for nid in ids:
            row = record_ic_snapshot(
                s, nid, ic_value, source="pipeline", source_strategy_id=strategy_id,
            )
            # Sync the live ic_score column so that granger._eligible_nodes,
            # alpha_queue, and auto_pipeline all see the updated value without
            # waiting for the next decay pass (which may never run when
            # IC_DECAY_ENABLED is off, the default).
            values = {"ic_score": float(row.ic_value), "ic_decayed_at": now}
            if decay_on and int(nid) in conf_map:
                conf, ic_s, kind, status = conf_map[int(nid)]
                old_conf = _confidence_from(conf, ic_s)
                outcome = float(row.ic_value)
                kind_l = (kind or "").strip().lower()
                decayable = kind_l in ("past_alpha", "postmortem")
                # Flag/clear is tied to the OUTCOME DIRECTION, not the absolute
                # confidence level: a bad outcome can flag a now-suspect thesis;
                # a good outcome redeems it and clears any prior revisit latch
                # (prevents permanent freezing — same lesson as the IC floor fix).
                if outcome < _conf_bad_ic():
                    new_conf = max(_conf_floor(), old_conf * _conf_decay_factor())
                    if decayable and new_conf < _conf_revisit_threshold():
                        values["revisit_flagged_at"] = now
                        values["status"] = "revisit"
                else:
                    new_conf = min(1.0, old_conf + _conf_reward() * max(0.0, outcome))
                    if outcome >= _conf_good_ic() and (status or "").strip().lower() == "revisit":
                        values["revisit_flagged_at"] = None
                        values["status"] = "active"
                values["confidence"] = max(0.0, min(1.0, new_conf))
            s.execute(
                _sa_update(KnowledgeNode)
                .where(KnowledgeNode.id == nid)
                .values(**values)
            )
            count += 1
        s.flush()
        return count

    if own_session:
        with session_scope() as s:
            return _run(s)
    return _run(session)


__all__ = [
    "is_decay_enabled",
    "record_ic_snapshot",
    "get_history",
    "decay_all_ic",
    "record_pipeline_outcomes",
]

"""IC-priority alpha-idea queue (P6-B2).

What this gives the system
--------------------------
Even without fresh ingest, the system should keep working through high-IC
KnowledgeNodes that have not yet been turned into AlphaStrategies. This module
ranks the backlog by ``composite_score = 0.7·IC + 0.3·recency_decay`` and
exposes:

* ``next_candidates(session, limit, ic_floor)`` — pure-read ranked list.
* ``promote_node(node_id, raw_text)`` — synchronous "fire one pipeline now",
  used by the ``POST /api/alpha-queue/promote/{id}`` endpoint and by the
  scheduled queue tick (when enabled).

Why ``composite_score`` is the right metric
-------------------------------------------
* IC alone over-rewards stale-but-once-high concepts.
* Recency alone over-rewards new noise.
* ``0.7·IC + 0.3·exp(-hours/168)`` (week half-life) keeps fresh ideas competitive
  for a week, then deprioritises them unless their IC was strong.

Reservation reuse
-----------------
We use the same ``KnowledgeNode.auto_pipeline_strategy_id`` sentinel as B1 so a
node selected by the queue can't also be picked up by an ingest hook. This is
why both features share the same migration column.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from backend._envloader import env_bool, env_float, env_int
from backend.core.database import KnowledgeNode, session_scope

logger = logging.getLogger("alpha.queue")

# Composite-score weights — surfaced as constants so the future ML-tuned
# version can swap them without touching call sites.
_WEIGHT_IC = 0.7
_WEIGHT_RECENCY = 0.3
_RECENCY_HALFLIFE_HOURS = 168.0  # 7 days


@dataclass
class AlphaQueueItem:
    node_id: int
    title: str
    category: Optional[str]
    ic_score: float
    recency_hours: float
    composite_score: float
    eligible: bool
    blocked_reason: Optional[str]

    def to_dict(self) -> dict:
        return asdict(self)


def is_enabled() -> bool:
    """Master flag for the queue's *scheduled* promotion path. Read endpoints
    work regardless — the gate is only about *automatic* execution."""
    return env_bool("ALPHA_QUEUE_ENABLED", False)


def _ic_floor() -> float:
    return env_float("ALPHA_QUEUE_IC_FLOOR", 0.5, minimum=0.0, maximum=2.0)


def _batch_size() -> int:
    return env_int("ALPHA_QUEUE_BATCH_SIZE", 1, minimum=1, maximum=10)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _composite(ic: float, recency_hours: float) -> float:
    decay = math.exp(-recency_hours / _RECENCY_HALFLIFE_HOURS) if recency_hours >= 0 else 1.0
    return _WEIGHT_IC * ic + _WEIGHT_RECENCY * decay


def next_candidates(
    session: Session,
    *,
    limit: int = 20,
    ic_floor: Optional[float] = None,
) -> List[AlphaQueueItem]:
    """Ranked alpha-idea backlog. Read-only."""
    floor = float(ic_floor) if ic_floor is not None else _ic_floor()
    pool_size = max(int(limit) * 5, 50)
    now = datetime.now(timezone.utc)
    rows = (
        session.query(KnowledgeNode)
        .filter(KnowledgeNode.auto_pipeline_strategy_id.is_(None))
        .filter(KnowledgeNode.kind.in_(["concept", "past_alpha"]))
        .filter(KnowledgeNode.ic_score >= floor)
        # ORDER BY ic_score DESC so the pool contains the highest-IC candidates.
        # IC carries weight 0.7 in composite_score, so the top-IC pool is a sound
        # approximation of the top-composite_score set. Python sort below remains
        # the authoritative ranking within the pool.
        .order_by(KnowledgeNode.ic_score.desc())
        .limit(pool_size)
        .all()
    )
    items: List[AlphaQueueItem] = []
    for n in rows:
        created = _as_utc(n.created_at) or now
        hours = max(0.0, (now - created).total_seconds() / 3600.0)
        ic = float(n.ic_score or 0.0)
        items.append(
            AlphaQueueItem(
                node_id=int(n.id),
                title=n.title or "",
                category=n.category,
                ic_score=ic,
                recency_hours=round(hours, 2),
                composite_score=round(_composite(ic, hours), 6),
                eligible=True,
                blocked_reason=None,
            )
        )
    items.sort(key=lambda x: x.composite_score, reverse=True)
    return items[: int(limit)]


def promote_node(node_id: int) -> dict:
    """Synchronously turn one node into an AlphaStrategy via the orchestrator.

    Used by ``POST /api/alpha-queue/promote/{id}`` (manual button on
    Mission Control) and the periodic queue tick.

    Returns ``{strategy_id, status, node_id}`` on success or ``{error}`` dict.
    Honours the LLM budget cap (raises propagate to caller for 503 conversion).
    """
    with session_scope() as s:
        node = s.get(KnowledgeNode, int(node_id))
        if node is None:
            return {"error": "node not found", "node_id": int(node_id)}
        if node.auto_pipeline_strategy_id is not None and node.auto_pipeline_strategy_id > 0:
            return {
                "error": "node already promoted",
                "node_id": int(node_id),
                "existing_strategy_id": int(node.auto_pipeline_strategy_id),
            }
        # P29-S10: SELECT-then-UPDATE was non-atomic — two concurrent promotes
        # both saw NULL and both spent LLM budget. Conditional UPDATE only
        # writes when column still NULL; rowcount==0 means we lost the race.
        from sqlalchemy import text as _sa_text
        _now_utc = datetime.now(timezone.utc)
        _res = s.execute(
            _sa_text(
                "UPDATE knowledge_nodes "
                "SET auto_pipeline_strategy_id = -1, "
                "    last_queue_eval_at = :now_utc "
                "WHERE id = :nid "
                "  AND auto_pipeline_strategy_id IS NULL"
            ),
            {"nid": int(node_id), "now_utc": _now_utc},
        )
        if int(_res.rowcount or 0) == 0:
            # R5/BE-DATA-006: the conditional UPDATE lost the race. Read the
            # authoritative value via raw SQL to bypass the ORM identity map +
            # in-transaction snapshot — s.expire(node) + attribute access can
            # lazy-load the pre-winner-commit value on SQLite WAL / PG READ
            # COMMITTED, returning a stale existing_strategy_id in the error dict.
            other_sid = s.execute(
                _sa_text(
                    "SELECT auto_pipeline_strategy_id FROM knowledge_nodes WHERE id = :nid"
                ),
                {"nid": int(node_id)},
            ).scalar()
            return {
                "error": "node already reserved",
                "node_id": int(node_id),
                "existing_strategy_id": int(other_sid) if other_sid is not None else None,
            }
        s.expire(node)
        node.auto_pipeline_strategy_id = -1  # reservation sentinel (mirror B1)
        # P13/D-M6 — stamp last_queue_eval_at so the orphan recovery sweep
        # can pick up crashed reservations. Without this, the IS NULL filter
        # would veto this node forever after the sentinel is set, leaking a
        # reservation slot if the orchestrator crashes before line 168.
        # P29-S10: UPDATE above already set last_queue_eval_at; kept here for
        # symmetry with the ORM in-memory copy (s.expire refreshed it but the
        # explicit assignment makes the lifecycle obvious for code review).
        node.last_queue_eval_at = _now_utc
        raw_text = (node.content or node.title or "")[:1200]

    # P15/D-H6 — scope strategy_id OUTSIDE the try so the rollback handler can
    # reach it after bootstrap_strategy returns but before run_full_pipeline_for_id
    # finishes. This is required so the rollback can flush an orphaned INTAKE
    # strategy row to REJECTED with an audit-trail reason.
    strategy_id: Optional[int] = None
    try:
        from backend.core.orchestrator import WorkflowOrchestrator
        from backend.core import universe

        # P-MULTISYM: pick the target instrument from the multi-asset universe,
        # exactly like the ingest-driven auto_pipeline does. The backlog queue is
        # the dominant generator (it grinds KB nodes even without fresh ingest),
        # so without this it pins every strategy to BTC and the universe rotation
        # never reaches production. Pre-flight the data; fall back to BTC (always
        # available) if the pick's price data can't be fetched.
        inst = universe.pick_for_strategy()
        if not universe.ensure_price_data(inst.app_symbol):
            logger.warning(
                "alpha_queue: price data unavailable for %s; falling back to BTC",
                inst.app_symbol,
            )
            inst = universe.btc_instrument()
        orch = WorkflowOrchestrator(asset_symbol=inst.app_symbol)
        strategy_id = orch.bootstrap_strategy(raw_text or "(queue promote)")
        # T2-A — bridge-node seeding (identity fallback when disabled/empty).
        try:
            from backend.core import graph_intel
            _seeded = graph_intel.seed_node_ids_for_auto([int(node_id)])
        except Exception:  # noqa: BLE001
            _seeded = [int(node_id)]
        result = orch.run_full_pipeline_for_id(
            strategy_id=int(strategy_id),
            raw_text=raw_text,
            prior_node_ids=_seeded,
        )
        with session_scope() as s:
            n = s.get(KnowledgeNode, int(node_id))
            if n is not None:
                n.auto_pipeline_strategy_id = int(strategy_id)
                n.last_queue_eval_at = datetime.now(timezone.utc)
        return {
            "node_id": int(node_id),
            "strategy_id": int(strategy_id),
            "status": str(result.get("status") or "UNKNOWN") if isinstance(result, dict) else "UNKNOWN",
        }
    except Exception as exc:  # noqa: BLE001
        # Re-raise budget cap immediately so tick_queue can catch it by type
        # rather than relying on fragile string-match of the exception class name.
        from backend.agents._client import LLMBudgetExceededError as _BudgetExc
        if isinstance(exc, _BudgetExc):
            # Reset the reservation sentinel before re-raising so the node
            # is not permanently locked. The outer session_scope block has
            # already committed -1; we must undo it here.
            try:
                with session_scope() as _s:
                    _n = _s.get(KnowledgeNode, int(node_id))
                    if _n is not None and _n.auto_pipeline_strategy_id == -1:
                        _n.auto_pipeline_strategy_id = None
            except Exception:  # noqa: BLE001
                logger.exception(
                    "alpha_queue: sentinel reset failed for node %s on budget cap",
                    node_id,
                )
            raise
        logger.exception("alpha_queue: promote failed for node %s", node_id)
        try:
            with session_scope() as s:
                n = s.get(KnowledgeNode, int(node_id))
                if n is not None and n.auto_pipeline_strategy_id == -1:
                    n.auto_pipeline_strategy_id = None
                # P15/D-H6 — flush orphan strategy to REJECTED so the audit
                # trail captures the queue-promote failure cause. Guard
                # `status == "INTAKE"` keeps this idempotent: if the
                # orchestrator's own catch already transitioned it, this is a
                # no-op.
                if strategy_id is not None:
                    from backend.core.database import AlphaStrategy
                    row = s.get(AlphaStrategy, int(strategy_id))
                    if row is not None and row.status == "INTAKE":
                        from backend.core.orchestrator import STAGE_FOR_STATUS, PipelineStatus
                        from backend.core.transition_log import record_transition
                        prior = row.status
                        row.status = "REJECTED"
                        row.stage = STAGE_FOR_STATUS[PipelineStatus.REJECTED]
                        row.team_b_review = (
                            f"Queue-promote crashed before pipeline reached the orchestrator's "
                            f"own REJECT handler. Reason: {type(exc).__name__}: {exc}"[:1024]
                        )
                        try:
                            record_transition(
                                s,
                                strategy_id=int(strategy_id),
                                from_status=prior,
                                to_status="REJECTED",
                                from_stage=0,
                                to_stage=7,
                                actor="alpha_queue",
                                reason="promote_node exception path",
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception("alpha_queue: transition_log write failed for orphan")
        except Exception:  # noqa: BLE001
            logger.exception("alpha_queue: rollback failed for node %s", node_id)
        return {
            "node_id": int(node_id),
            "strategy_id": strategy_id,
            "error": f"{type(exc).__name__}: {exc}",
        }


def tick_queue() -> dict:
    """Scheduled tick: pull top ``batch_size`` from queue, promote each.

    Wired into ``periodic_tasks.py`` only when the user opts in. Each
    promotion runs synchronously inside this thread (the periodic runner uses
    ``asyncio.to_thread`` so the asyncio loop stays free).
    """
    if not is_enabled():
        return {"enabled": False, "promoted": 0, "evaluated": 0}
    batch = _batch_size()
    with session_scope() as s:
        candidates = next_candidates(s, limit=batch)
    if not candidates:
        return {"enabled": True, "promoted": 0, "evaluated": 0}

    promoted = 0
    errors: List[str] = []
    # P11-B-07: stop the whole tick the moment LLM budget cap blows, so we don't
    # keep firing already-doomed promotions back-to-back inside one tick.
    budget_capped = False
    from backend.agents._client import LLMBudgetExceededError as _BudgetExc
    for item in candidates:
        try:
            result = promote_node(item.node_id)
        except _BudgetExc:
            # promote_node re-raises LLMBudgetExceededError so we catch by type
            # (robust against class/message renames that break string-match).
            budget_capped = True
            errors.append(f"node{item.node_id}: LLMBudgetExceededError (budget cap)")
            logger.warning(
                "alpha_queue.tick: LLM budget cap hit on node %s; "
                "halting tick (evaluated=%d, promoted=%d)",
                item.node_id, len(candidates), promoted,
            )
            break
        if "error" in result:
            err = str(result["error"])
            errors.append(f"node{item.node_id}: {err[:120]}")
            # Legacy fallback: if any other path still returns a budget error
            # as a dict (e.g. a future code path not yet migrated), keep the
            # string-match as a secondary guard so we don't lose the halt.
            if "LLMBudgetExceeded" in err:
                budget_capped = True
                logger.warning(
                    "alpha_queue.tick: LLM budget cap hit on node %s (dict path); "
                    "halting tick (evaluated=%d, promoted=%d)",
                    item.node_id, len(candidates), promoted,
                )
                break
        else:
            promoted += 1
    out = {
        "enabled": True,
        "promoted": promoted,
        "evaluated": len(candidates),
        "errors": errors[:5],
    }
    if budget_capped:
        out["budget_capped"] = True
    return out


__all__ = [
    "AlphaQueueItem",
    "is_enabled",
    "next_candidates",
    "promote_node",
    "tick_queue",
]

"""Source → KnowledgeNode → auto-pipeline hook (P6-B1).

What this enables
-----------------
Closes the "I leave the system running overnight and it discovers fresh alphas"
loop from the reference YouTube demo. When the ingestion scheduler writes new
KnowledgeNodes for a source, this module conditionally fires
``WorkflowOrchestrator.run_full_pipeline_for_id`` on the high-IC ones in a
background thread.

Gates (all must pass; ALL default-OFF / conservative)
-----------------------------------------------------
1. ``AUTO_PIPELINE_FROM_INGEST`` env (default OFF).
2. **Daily per-source quota** ``AUTO_PIPELINE_MAX_PER_SOURCE_PER_DAY`` (default 3),
   tracked via ``ingest_sources.auto_pipeline_day`` + ``auto_pipeline_count_today``
   so it survives restarts and self-resets at UTC midnight.
3. **IC floor** ``AUTO_PIPELINE_IC_THRESHOLD`` (default 0.6) — only auto-trigger
   on nodes the intake/critic flagged as high-quality.
4. **Batch size** ``AUTO_PIPELINE_BATCH_SIZE`` (default 1) — never dispatch more
   than N strategies per ingest tick.
5. **LLM budget cap** (cross-cutting, set by ``ALPHA_LLM_DAILY_USD_CAP``)
   raises ``LLMBudgetExceededError`` at the agent layer; we catch it here and
   release reservations gracefully so the same node retries next day.

Concurrency model
-----------------
* Reservation: setting ``KnowledgeNode.auto_pipeline_strategy_id = -1`` BEFORE
  spawning the background thread is the "claim" — a future tick won't re-trigger
  the same node. On pipeline success we update it to the actual strategy id; on
  failure we restore it to NULL so the node is retry-eligible.
* Single-flight: a process-level ``threading.Semaphore`` enforces the global
  concurrency limit ``AUTO_PIPELINE_CONCURRENCY`` (default 1) so the LLM
  rate-limit + budget cap can plan around a fixed worker count.
* Status tracking: the last ``MAX_RECENT_TRIGGERS`` triggers (success or fail)
  are kept in an in-process deque for the ``/api/auto-pipeline/status`` endpoint.

Why not asyncio?
----------------
``WorkflowOrchestrator.run_full_pipeline_for_id`` is sync and may block for
minutes (LLM call + sandbox run + critic call). Spawning an asyncio task on
the scheduler loop would block ingest until pipeline completes; a thread is
both simpler and matches how ``poll_source_now`` already wraps fetchers.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from backend._envloader import env_bool, env_float, env_int
from backend.core.database import IngestSource, KnowledgeNode, session_scope

logger = logging.getLogger("alpha.auto_pipeline")

_RESERVATION_SENTINEL = -1
_MAX_RECENT_TRIGGERS = 50
_RECENT: deque = deque(maxlen=_MAX_RECENT_TRIGGERS)
_RECENT_LOCK = threading.Lock()

# F3 — global "dispatched today" counter for Stage 0 queue overlay.
# Reset at UTC midnight; survives only within process.
_DISPATCH_DAY: Optional[str] = None
_DISPATCH_COUNT: int = 0
_DISPATCH_LOCK = threading.Lock()


def _record_dispatch(count: int) -> None:
    """Increment the global UTC-day dispatch counter."""
    global _DISPATCH_DAY, _DISPATCH_COUNT
    today = datetime.now(timezone.utc).date().isoformat()
    with _DISPATCH_LOCK:
        if _DISPATCH_DAY != today:
            _DISPATCH_DAY = today
            _DISPATCH_COUNT = 0
        _DISPATCH_COUNT += int(max(0, count))


def _record_dispatch_refund(count: int) -> None:
    """Decrement the global UTC-day dispatch counter (clamped at 0).

    Called in the three failure paths of ``_run_pipeline_safely`` that already
    refund the DB quota counter (``auto_pipeline_count_today``) but previously
    left ``_DISPATCH_COUNT`` un-decremented, causing the in-process
    ``/api/auto-pipeline/status.dispatched_today`` to permanently over-report
    for the rest of the UTC day.  Mirror of ``_record_dispatch``.
    """
    global _DISPATCH_COUNT
    today = datetime.now(timezone.utc).date().isoformat()
    with _DISPATCH_LOCK:
        # If the day has rolled over, the counter already reset to 0 —
        # there is nothing to refund.
        if _DISPATCH_DAY != today:
            return
        _DISPATCH_COUNT = max(0, _DISPATCH_COUNT - int(max(0, count)))


def _dispatched_today() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    with _DISPATCH_LOCK:
        if _DISPATCH_DAY != today:
            return 0
        return int(_DISPATCH_COUNT)


_CONCURRENCY_LOCK = threading.Lock()
_CONCURRENCY_LIMITER: "Optional[_ResizableLimiter]" = None


def is_enabled() -> bool:
    return env_bool("AUTO_PIPELINE_FROM_INGEST", False)


def _ic_threshold() -> float:
    return env_float("AUTO_PIPELINE_IC_THRESHOLD", 0.6, minimum=0.0, maximum=2.0)


def _daily_quota() -> int:
    return env_int("AUTO_PIPELINE_MAX_PER_SOURCE_PER_DAY", 3, minimum=0, maximum=100)


def _batch_size() -> int:
    return env_int("AUTO_PIPELINE_BATCH_SIZE", 1, minimum=1, maximum=10)


def _concurrency_limit() -> int:
    # P-CONCURRENCY: re-read live so the limiter resizes without a restart.
    return env_int("AUTO_PIPELINE_CONCURRENCY", 1, minimum=1, maximum=16)


class _ResizableLimiter:
    """A bounded concurrency limiter whose ceiling is re-read on EVERY acquire,
    so changing ``AUTO_PIPELINE_CONCURRENCY`` takes effect WITHOUT a process
    restart (``threading.Semaphore`` fixes its count at construction and cannot
    resize). Default limit 1 ⇒ identical single-flight behavior to the previous
    semaphore. Exposes the ``acquire(blocking, timeout)`` / ``release()``
    interface the call sites already use."""

    def __init__(self, limit_fn) -> None:
        self._limit_fn = limit_fn
        self._cond = threading.Condition()
        self._active = 0

    def acquire(self, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else (time.monotonic() + timeout)
        with self._cond:
            while self._active >= max(1, int(self._limit_fn())):
                if not blocking:
                    return False
                remaining: Optional[float] = None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                self._cond.wait(timeout=remaining)
            self._active += 1
            return True

    def release(self) -> None:
        with self._cond:
            if self._active > 0:
                self._active -= 1
            self._cond.notify()


def _get_semaphore() -> "_ResizableLimiter":
    """Lazy-init the global resizable concurrency limiter (P-CONCURRENCY).

    Replaces the old fixed semaphore so ``AUTO_PIPELINE_CONCURRENCY`` can be
    tuned at runtime. Default remains 1 (single-flight); raise it only with the
    LLM provider's rate limits AND the daily/per-strategy budget caps in mind —
    parallelism multiplies both throughput and spend.
    """
    global _CONCURRENCY_LIMITER
    with _CONCURRENCY_LOCK:
        if _CONCURRENCY_LIMITER is None:
            _CONCURRENCY_LIMITER = _ResizableLimiter(_concurrency_limit)
        return _CONCURRENCY_LIMITER


def _record_trigger(payload: Dict[str, Any]) -> None:
    with _RECENT_LOCK:
        _RECENT.append({**payload, "at": datetime.now(timezone.utc).isoformat()})


def recent_triggers() -> List[Dict[str, Any]]:
    """Snapshot for /api/auto-pipeline/status. Thread-safe copy."""
    with _RECENT_LOCK:
        return list(_RECENT)


def _reset_quota_if_new_day(src: IngestSource) -> None:
    """In-place cross-day reset. Caller must commit."""
    today = datetime.now(timezone.utc).date().isoformat()
    if src.auto_pipeline_day != today:
        src.auto_pipeline_day = today
        src.auto_pipeline_count_today = 0


def _select_candidates(
    s: Session, source_id: int, node_ids: List[int], *, ic_floor: float, take: int,
) -> List[KnowledgeNode]:
    rows = (
        s.query(KnowledgeNode)
        .filter(KnowledgeNode.id.in_([int(x) for x in node_ids]))
        .filter(KnowledgeNode.auto_pipeline_strategy_id.is_(None))
        .filter(KnowledgeNode.ic_score >= ic_floor)
        .order_by(KnowledgeNode.ic_score.desc(), KnowledgeNode.id.desc())
        .limit(int(take))
        # P-AP-LOCK: make the docstring's "row-locked" reservation true on
        # Postgres — without FOR UPDATE two concurrent triggers can both read
        # the same NULL-reservation rows before either stamps the sentinel,
        # double-dispatching the pipeline. No-op on SQLite (dialect omits the
        # clause; correctness there relies on the single-writer WAL lock).
        .with_for_update()
        .all()
    )
    return rows


def maybe_trigger_pipeline_for_nodes(
    source_id: int, new_node_ids: List[int],
) -> List[int]:
    """Gate + reserve + spawn. Returns the list of node ids actually dispatched.

    Safe to call from the scheduler thread. NEVER raises — failures log and
    return ``[]``. Idempotent across rapid duplicate calls because the
    reservation step is row-locked.
    """
    if not is_enabled() or not new_node_ids:
        return []

    triggered: List[int] = []
    raw_text: str = ""

    try:
        with session_scope() as s:
            # P11-B-14: source_id == 0 is a "bot intake" trigger (telegram/discord
            # /intake). There is no IngestSource row and no per-source quota; we
            # still honour batch size + IC floor + reservation semantics.
            src: Optional[IngestSource] = None
            if int(source_id) != 0:
                # P34: lock the source row so two concurrent ticks can't both
                # read the quota counter, both pass the cap gate, and both
                # increment — over-dispatching past the daily quota.
                src = s.get(IngestSource, int(source_id), with_for_update=True)
                if src is None:
                    return []
                _reset_quota_if_new_day(src)

                quota = _daily_quota()
                already = int(src.auto_pipeline_count_today or 0)
                if quota <= 0 or already >= quota:
                    logger.info(
                        "auto-pipeline: source %s quota exhausted (%s/%s)",
                        source_id, already, quota,
                    )
                    return []

                slots = min(quota - already, _batch_size())
            else:
                slots = _batch_size()
            ic_floor = _ic_threshold()
            candidates = _select_candidates(
                s, source_id, new_node_ids, ic_floor=ic_floor, take=slots,
            )
            if not candidates:
                logger.debug(
                    "auto-pipeline: source %s — no candidates above IC %.2f",
                    source_id, ic_floor,
                )
                return []

            # Reserve + bump counter atomically (single transaction).
            now = datetime.now(timezone.utc)
            if src is not None:
                src.auto_pipeline_count_today = int(src.auto_pipeline_count_today or 0) + len(candidates)
                src.last_auto_pipeline_at = now
            for n in candidates:
                n.auto_pipeline_strategy_id = _RESERVATION_SENTINEL
                n.last_queue_eval_at = now
                triggered.append(int(n.id))
            # The primary candidate's content seeds the pipeline's raw_text.
            raw_text = (candidates[0].content or candidates[0].title or "")[:1200]
    except Exception:  # noqa: BLE001
        logger.exception("auto-pipeline: reservation failed for source %s", source_id)
        return []

    if not triggered:
        return []

    # Dispatch the heavy work off the scheduler thread.
    thread = threading.Thread(
        target=_run_pipeline_safely,
        args=(triggered, raw_text, int(source_id)),
        daemon=True,
        name=f"auto-pipeline-src{source_id}",
    )
    try:
        thread.start()
    except Exception as exc:  # noqa: BLE001
        # Thread.start() can raise RuntimeError ('can't start new thread')
        # under OS thread exhaustion. The reservation sentinel and the
        # per-source quota counter were already committed at reservation
        # time, and _run_pipeline_safely never ran, so compensate here
        # instead of leaking until recover_orphan_reservations / UTC reset.
        logger.exception(
            "auto-pipeline: thread.start() failed for source %s nodes %s",
            source_id, triggered,
        )
        _release_reservation(triggered)
        if int(source_id) != 0:
            try:
                with session_scope() as _s:
                    # P11-R2 (round-2): atomic clamp-decrement via two single-
                    # statement UPDATEs (no Python read-modify-write), so two
                    # concurrent refunds for the same source cannot lose an
                    # update on SQLite WAL.
                    _n = len(triggered)
                    _dec = (
                        _s.query(IngestSource)
                        .filter(IngestSource.id == int(source_id))
                        .filter(IngestSource.auto_pipeline_count_today >= _n)
                        .update(
                            {IngestSource.auto_pipeline_count_today:
                                IngestSource.auto_pipeline_count_today - _n},
                            synchronize_session=False,
                        )
                    )
                    if not _dec:
                        (
                            _s.query(IngestSource)
                            .filter(IngestSource.id == int(source_id))
                            .update(
                                {IngestSource.auto_pipeline_count_today: 0},
                                synchronize_session=False,
                            )
                        )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "auto-pipeline: quota refund after start() failure failed for source %s",
                    source_id,
                )
        _record_trigger({
            "source_id": source_id,
            "node_ids": triggered,
            "status": "dispatch_failed",
            "error": f"{type(exc).__name__}: {exc}"[:300],
        })
        return []
    # Only count the dispatch once the worker thread is actually running,
    # so /api/auto-pipeline/status.dispatched_today stays truthful.
    _record_dispatch(len(triggered))
    logger.info(
        "auto-pipeline: dispatched source=%s nodes=%s (raw_text=%d chars)",
        source_id, triggered, len(raw_text),
    )
    return triggered


def _run_pipeline_safely(node_ids: List[int], raw_text: str, source_id: int) -> None:
    """Worker thread body. Catches every exception and releases reservations."""
    semaphore = _get_semaphore()
    acquired = semaphore.acquire(blocking=True, timeout=600.0)
    if not acquired:
        logger.warning("auto-pipeline: concurrency semaphore timeout for nodes %s", node_ids)
        _release_reservation(node_ids)
        # P34: the quota counter was committed at reservation time; a semaphore
        # timeout means the pipeline never ran, so refund the slots (clamped at 0)
        # instead of leaking them until the UTC-midnight reset. source_id == 0 is
        # bot-intake with no IngestSource row / no per-source quota.
        if int(source_id) != 0:
            try:
                with session_scope() as _s:
                    # P11-R2 (round-2): atomic clamp-decrement via two single-
                    # statement UPDATEs (no Python read-modify-write), so two
                    # concurrent refunds for the same source cannot lose an
                    # update on SQLite WAL.
                    _n = len(node_ids)
                    _dec = (
                        _s.query(IngestSource)
                        .filter(IngestSource.id == int(source_id))
                        .filter(IngestSource.auto_pipeline_count_today >= _n)
                        .update(
                            {IngestSource.auto_pipeline_count_today:
                                IngestSource.auto_pipeline_count_today - _n},
                            synchronize_session=False,
                        )
                    )
                    if not _dec:
                        (
                            _s.query(IngestSource)
                            .filter(IngestSource.id == int(source_id))
                            .update(
                                {IngestSource.auto_pipeline_count_today: 0},
                                synchronize_session=False,
                            )
                        )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "auto-pipeline: quota refund failed for source %s", source_id
                )
        _record_dispatch_refund(len(node_ids))
        _record_trigger({
            "source_id": source_id,
            "node_ids": node_ids,
            "status": "concurrency_timeout",
        })
        return

    # P15/D-H8 — Scope strategy_id outside the try so the rollback handler can
    # reach it. Required to flush an orphaned INTAKE strategy row to REJECTED
    # when the orchestrator's own outer catch fails (rare DB-transient case).
    strategy_id: Optional[int] = None
    try:
        from backend.agents._client import LLMBudgetExceededError
        from backend.core.orchestrator import WorkflowOrchestrator
        from backend.core import universe

        # P-MULTISYM: pick the target instrument for this strategy (crypto base
        # or US equity, per STRATEGY_UNIVERSE_MODE). Pre-flight its price data so
        # an equity whose Yahoo fetch fails doesn't sink the whole run — fall back
        # to BTC (always available) and keep the label honest.
        inst = universe.pick_for_strategy()
        if not universe.ensure_price_data(inst.app_symbol):
            logger.warning(
                "auto-pipeline: price data unavailable for %s; falling back to BTC",
                inst.app_symbol,
            )
            inst = universe.btc_instrument()
        orch = WorkflowOrchestrator(asset_symbol=inst.app_symbol)
        strategy_id = orch.bootstrap_strategy(raw_text or "(auto-pipeline)")
        logger.info(
            "auto-pipeline: strategy %s targets %s (%s)",
            strategy_id, inst.app_symbol, inst.asset_class,
        )
        # T2-A — preferentially augment the seed with bridge nodes (novel
        # cross-domain connectors). Identity fallback when GRAPH_RAG_SEED_SELECTION
        # is off / the graph is empty, so today's behaviour is unchanged by default.
        try:
            from backend.core import graph_intel
            _seeded = graph_intel.seed_node_ids_for_auto(list(node_ids))
        except Exception:  # noqa: BLE001 — seeding must never block dispatch
            _seeded = list(node_ids)
        result = orch.run_full_pipeline_for_id(
            strategy_id=int(strategy_id),
            raw_text=raw_text,
            prior_node_ids=_seeded,
        )

        # Pin reservation to the real strategy id.
        with session_scope() as s:
            (
                s.query(KnowledgeNode)
                .filter(KnowledgeNode.id.in_(node_ids))
                .filter(KnowledgeNode.auto_pipeline_strategy_id == _RESERVATION_SENTINEL)
                .update(
                    {"auto_pipeline_strategy_id": int(strategy_id)},
                    synchronize_session=False,
                )
            )
        _record_trigger({
            "source_id": source_id,
            "node_ids": node_ids,
            "strategy_id": int(strategy_id),
            "status": str(result.get("status") or "UNKNOWN") if isinstance(result, dict) else "UNKNOWN",
        })
    except LLMBudgetExceededError as exc:
        logger.warning("auto-pipeline: LLM budget cap hit: %s", exc)
        _release_reservation(node_ids)
        _flush_orphan_strategy(strategy_id, exc, "budget_capped")
        # Refund the quota counter: budget was capped before any pipeline
        # completed, so the slots should not count toward the daily limit.
        if int(source_id) != 0:
            try:
                with session_scope() as _s:
                    _n = len(node_ids)
                    _dec = (
                        _s.query(IngestSource)
                        .filter(IngestSource.id == int(source_id))
                        .filter(IngestSource.auto_pipeline_count_today >= _n)
                        .update(
                            {IngestSource.auto_pipeline_count_today:
                                IngestSource.auto_pipeline_count_today - _n},
                            synchronize_session=False,
                        )
                    )
                    if not _dec:
                        (
                            _s.query(IngestSource)
                            .filter(IngestSource.id == int(source_id))
                            .update(
                                {IngestSource.auto_pipeline_count_today: 0},
                                synchronize_session=False,
                            )
                        )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "auto-pipeline: quota refund after budget cap failed for source %s",
                    source_id,
                )
        _record_dispatch_refund(len(node_ids))
        _record_trigger({
            "source_id": source_id,
            "node_ids": node_ids,
            "status": "budget_capped",
            "error": str(exc)[:300],
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("auto-pipeline: orchestrator crashed")
        _release_reservation(node_ids)
        _flush_orphan_strategy(strategy_id, exc, "error")
        # Refund the quota counter: pipeline crashed before completing,
        # so the slots should not count toward the daily limit.
        if int(source_id) != 0:
            try:
                with session_scope() as _s:
                    _n = len(node_ids)
                    _dec = (
                        _s.query(IngestSource)
                        .filter(IngestSource.id == int(source_id))
                        .filter(IngestSource.auto_pipeline_count_today >= _n)
                        .update(
                            {IngestSource.auto_pipeline_count_today:
                                IngestSource.auto_pipeline_count_today - _n},
                            synchronize_session=False,
                        )
                    )
                    if not _dec:
                        (
                            _s.query(IngestSource)
                            .filter(IngestSource.id == int(source_id))
                            .update(
                                {IngestSource.auto_pipeline_count_today: 0},
                                synchronize_session=False,
                            )
                        )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "auto-pipeline: quota refund after orchestrator crash failed for source %s",
                    source_id,
                )
        _record_dispatch_refund(len(node_ids))
        _record_trigger({
            "source_id": source_id,
            "node_ids": node_ids,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}"[:300],
        })
    finally:
        semaphore.release()


def _flush_orphan_strategy(strategy_id: Optional[int], exc: BaseException, status: str) -> None:
    """P15/D-H8 — transition a bootstrapped-but-never-progressed strategy to
    REJECTED so the audit trail captures the auto-pipeline crash cause.

    Idempotent: ``if row.status != "INTAKE"`` makes re-execution a no-op (if
    the orchestrator's own outer catch already transitioned it, we do nothing).
    All operations wrapped in try/except — never crashes the worker.
    """
    if strategy_id is None:
        return
    try:
        from backend.core.database import AlphaStrategy
        from backend.core.orchestrator import STAGE_FOR_STATUS, PipelineStatus
        from backend.core.transition_log import record_transition
        with session_scope() as s:
            row = s.get(AlphaStrategy, int(strategy_id))
            if row is None or row.status != "INTAKE":
                return
            prior = row.status
            row.status = "REJECTED"
            row.stage = STAGE_FOR_STATUS[PipelineStatus.REJECTED]
            row.team_b_review = (
                f"Auto-pipeline worker crashed before orchestrator REJECT handler "
                f"could run ({status}). Reason: {type(exc).__name__}: {exc}"[:1024]
            )
            try:
                record_transition(
                    s,
                    strategy_id=int(strategy_id),
                    from_status=prior,
                    to_status="REJECTED",
                    from_stage=0,
                    to_stage=7,
                    actor="auto_pipeline",
                    reason=f"_run_pipeline_safely exception path: {status}",
                )
            except Exception:  # noqa: BLE001
                logger.exception("auto_pipeline: transition_log write failed for orphan")
    except Exception:  # noqa: BLE001
        logger.exception("auto_pipeline: _flush_orphan_strategy crashed (non-fatal)")


def _release_reservation(node_ids: List[int]) -> None:
    """Reset the sentinel back to NULL so the same nodes are eligible again."""
    try:
        with session_scope() as s:
            (
                s.query(KnowledgeNode)
                .filter(KnowledgeNode.id.in_([int(x) for x in node_ids]))
                .filter(KnowledgeNode.auto_pipeline_strategy_id == _RESERVATION_SENTINEL)
                .update({"auto_pipeline_strategy_id": None}, synchronize_session=False)
            )
    except Exception:  # noqa: BLE001
        logger.exception("auto-pipeline: release_reservation failed for %s", node_ids)


def _recover_orphan_30min() -> int:
    """Thin wrapper used by the periodic task runner (stale_minutes=30).

    The periodic sweep fires every 30 min; using stale_minutes=30 ensures only
    sentinels older than a full sweep interval are released, preventing the sweep
    from wiping a reservation for a legitimate pipeline run still in progress.
    """
    return recover_orphan_reservations(stale_minutes=30)


def recover_orphan_reservations(*, stale_minutes: int = 15) -> int:
    """P11-B-06 — release stale reservation sentinels on startup.

    A node whose ``auto_pipeline_strategy_id`` is still ``_RESERVATION_SENTINEL``
    long after it was reserved indicates the process crashed mid-pipeline. We
    scan for those nodes (last_queue_eval_at older than ``stale_minutes`` ago,
    or NULL with created_at older than the cutoff) and reset them so the next
    tick can pick them up again.

    Returns the number of orphan nodes released. Never raises.
    """
    if int(stale_minutes) <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=int(stale_minutes))
    released = 0
    try:
        with session_scope() as s:
            from sqlalchemy import or_, and_
            orphan_q = (
                s.query(KnowledgeNode)
                .filter(KnowledgeNode.auto_pipeline_strategy_id == _RESERVATION_SENTINEL)
                .filter(
                    or_(
                        KnowledgeNode.last_queue_eval_at < cutoff,
                        and_(
                            KnowledgeNode.last_queue_eval_at.is_(None),
                            KnowledgeNode.created_at < cutoff,
                        ),
                    )
                )
            )
            orphan_ids = [int(n.id) for n in orphan_q.all()]
            if orphan_ids:
                (
                    s.query(KnowledgeNode)
                    .filter(KnowledgeNode.id.in_(orphan_ids))
                    .filter(KnowledgeNode.auto_pipeline_strategy_id == _RESERVATION_SENTINEL)
                    .update({"auto_pipeline_strategy_id": None}, synchronize_session=False)
                )
                released = len(orphan_ids)
        if released:
            logger.info(
                "auto-pipeline: recovered %d orphan reservations (stale > %d min)",
                released, int(stale_minutes),
            )
    except Exception:  # noqa: BLE001
        logger.exception("auto-pipeline: recover_orphan_reservations failed")
    return released


def status_snapshot() -> Dict[str, Any]:
    """Single dict for ``GET /api/auto-pipeline/status`` — pure read-only."""
    return {
        "enabled": is_enabled(),
        "ic_threshold": _ic_threshold(),
        "daily_quota_per_source": _daily_quota(),
        "batch_size": _batch_size(),
        "concurrency_limit": _concurrency_limit(),
        "recent_triggers": recent_triggers(),
        "dispatched_today": _dispatched_today(),
    }


__all__ = [
    "is_enabled",
    "maybe_trigger_pipeline_for_nodes",
    "recent_triggers",
    "recover_orphan_reservations",
    "status_snapshot",
]

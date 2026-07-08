"""StageTransition audit-log helper (P7-F).

Single source of truth for writing ``stage_transitions`` rows. Used by:

* :mod:`backend.core.orchestrator` — every ``_persist_status()`` call
* :mod:`backend.app.main` promote / retire / pause-all endpoints
* :mod:`backend.core.live_trade_ops` — when ``pause-all`` flips strategies to PAUSED

The function NEVER raises — transition logging is best-effort and must never
break the calling pipeline (matches the existing
``_maybe_notify_transition`` failure mode in ``orchestrator.py``).

Why a dedicated module:
~~~~~~~~~~~~~~~~~~~~~~~

If we inlined the SQL into each call-site, drift between writers would silently
corrupt the audit trail (different ``actor`` values, missing rows on retry, etc.).
A single helper guarantees uniform shape and lets us add tracing / metrics in
one place if we ever need to.

The transitions table is append-only.  ``REJECTED → APPROVED`` retries write two
rows (one per direction).  Self-loops (e.g. ``CRITIC_LOOP → CRITIC_LOOP`` retry)
are tolerated but never written here — the orchestrator decides whether a
``_persist_status`` is a real transition before invoking us.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger("alpha.transition")


def record_transition(
    session: Session,
    strategy_id: int,
    *,
    from_status: Optional[str],
    to_status: str,
    from_stage: Optional[int] = None,
    to_stage: Optional[int] = None,
    actor: str = "orchestrator",
    reason: Optional[str] = None,
    raise_on_error: bool = False,
) -> bool:
    """Append a row to ``stage_transitions``. Returns True on success.

    Args:
        session: live SQLAlchemy session. The caller owns the transaction —
            we do NOT commit here so the transition write stays atomic with
            the strategy update that triggered it.
        strategy_id: required FK to alpha_strategies.id
        from_status: previous PipelineStatus.value, or None on initial create
        to_status: new PipelineStatus.value (required)
        from_stage: optional 0..7 bucket index; defaults to None
        to_stage: optional 0..7 bucket index; resolved from to_status if None
            and a stage table is importable
        actor: who initiated the change. Recommended values:
            * ``"orchestrator"`` (default) — automatic pipeline transition
            * ``"operator"`` — promote/retire endpoint
            * ``"auto"`` — auto-pipeline / scheduled tick
            * ``"system"`` — pause-all / emergency stop / cleanup
            * ``"backfill"`` — synthesized from existing ``updated_at`` at boot
        reason: short free-text (truncated to 512 chars in the model)
        raise_on_error: when True, re-raises the exception after logging so
            fail-closed callers (e.g. promote/retire endpoints) can detect the
            failure and roll back the enclosing transaction.  Defaults to False
            to preserve the best-effort / never-raise contract for orchestrator
            and background pipeline callers.
    """
    try:
        # Local import to avoid load-order cycle (database.py imports this
        # module indirectly via orchestrator.py at boot).
        from backend.core.database import StageTransition

        # Resolve to_stage from to_status if caller didn't provide one.
        if to_stage is None:
            to_stage = _resolve_stage(to_status)
        if from_stage is None and from_status:
            from_stage = _resolve_stage(from_status)

        # P11-R2 (round-2): surface unmapped statuses. _resolve_stage returns None
        # for any to_status not in STAGE_FOR_STATUS; the row below then buckets it as
        # 0/INTAKE, silently skewing stage analytics. Full fix (nullable to_stage
        # column) is a schema change deferred to human sign-off; this makes the
        # data-quality gap visible in logs.
        if to_stage is None:
            logger.warning(
                "record_transition: unmapped to_status=%r (strategy_id=%s) -> bucketed "
                "as stage 0; check STAGE_FOR_STATUS / PipelineStatus",
                to_status, strategy_id,
            )

        row = StageTransition(
            strategy_id=int(strategy_id),
            from_status=(from_status or "")[:32] or None,
            to_status=str(to_status)[:32],
            from_stage=from_stage,
            to_stage=int(to_stage) if to_stage is not None else 0,
            actor=str(actor)[:32] or "orchestrator",
            reason=(reason or None) and str(reason)[:512],
            transitioned_at=datetime.now(timezone.utc),
        )
        session.add(row)
        # Caller commits.
        return True
    except Exception:  # noqa: BLE001
        logger.exception("record_transition failed")
        if raise_on_error:
            raise
        return False


def _resolve_stage(status: str) -> Optional[int]:
    """Best-effort status→stage resolver. Returns None on lookup failure."""
    try:
        from backend.core.orchestrator import PipelineStatus, STAGE_FOR_STATUS

        return STAGE_FOR_STATUS.get(PipelineStatus(status))
    except (ImportError, ValueError, KeyError):
        return None


__all__ = ["record_transition"]

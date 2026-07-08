"""HTTP idempotency middleware (P7 — shared by live-trade + trading-terminal).

Pattern:
~~~~~~~~

Every state-changing endpoint that may receive a retry from a flaky network
must accept an ``Idempotency-Key`` header containing a UUID-v4 (or any opaque
ASCII string 8..80 chars).

* Same key + same request body → return the *cached* response (no DB writes).
* Same key + different body  → 409 Conflict with ``IDEMPOTENCY_KEY_REUSED``.
* New key                     → execute, persist response, return.

This protects every high-risk endpoint (pause-all, manual-order submit, manual-
order cancel) against the canonical double-submit failure mode where the client
times out before reading the response and retries.

Why a dedicated module:
~~~~~~~~~~~~~~~~~~~~~~~

* The unique key constraint + race-loser-rereads-winner dance must be identical
  across all callers; subtle drift here = silent double-execution.
* Lets us swap the store (e.g. Redis) without touching call-sites.
* Tests can stub a single function instead of patching every endpoint.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Tuple

from fastapi import HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend._envloader import env_int

logger = logging.getLogger("alpha.idempotency")

# UUID-v4 + general opaque token; permissive on purpose (some clients use sha256
# digests, some use ULID, some use UUID). 8-80 ASCII chars matches Stripe-style
# idempotency keys.
_KEY_RE = re.compile(r"^[A-Za-z0-9\-_]{8,80}$")


def _ttl_hours() -> int:
    return env_int("IDEMPOTENCY_TTL_HOURS", 24, minimum=1, maximum=720)


def require_idempotency_key(request: Request) -> str:
    """FastAPI dependency. Returns validated key, raises 400 otherwise."""
    raw = (request.headers.get("Idempotency-Key") or "").strip()
    if not raw:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key header required for this endpoint",
        )
    if not _KEY_RE.match(raw):
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key must be 8..80 ASCII chars [A-Za-z0-9_-]",
        )
    return raw


# P15/D-M13 — bound the per-row response_json blob so a single huge response
# can't blow up the idempotency_keys table (and consequently the DB file).
# 1 MB is comfortably above any reasonable API response and prevents the
# replay-cache from growing unbounded under pathological responses.
_RESPONSE_BLOB_MAX_BYTES = 1_000_000


def _truncate_blob(blob: str) -> str:
    """Truncate huge response_json blobs so a single replay doesn't blow up the DB row."""
    if len(blob.encode("utf-8")) <= _RESPONSE_BLOB_MAX_BYTES:
        return blob
    # Replace with a marker payload; replays will see the marker instead of the
    # original response, but the dedup semantics still hold (same key → same
    # cached row). Callers' json.loads(...) still parses successfully.
    return json.dumps({
        "error": "RESPONSE_TOO_LARGE",
        "message": (
            f"Original response exceeded {_RESPONSE_BLOB_MAX_BYTES} bytes and was "
            "discarded on persist to keep the idempotency table bounded. Re-execute "
            "the request with a fresh Idempotency-Key to get the live response."
        ),
        "original_size_bytes": len(blob.encode("utf-8")),
    })


def canonical_request_hash(payload: Any) -> str:
    """SHA-256 of JSON-canonicalized payload. Stable across reorderings."""
    try:
        blob = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            ensure_ascii=False,
        )
    except (TypeError, ValueError):
        # Fall back to repr — shouldn't happen for our pydantic-validated bodies
        # but the test must still pass with weird inputs.
        blob = repr(payload)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class IdempotencyOutcome:
    """Result of an idempotency lookup."""

    replay: bool                    # True if the response came from cache
    response_payload: Any           # the cached response (when replay) or new (when miss)
    status_code: int                # HTTP status code attached to the response


def lookup_or_record(
    session: Session,
    *,
    key: str,
    request_hash: str,
    compute_fn: Callable[[], Tuple[Any, int]],
) -> IdempotencyOutcome:
    """Lookup ``key`` in idempotency_keys; replay or compute+store.

    The function is split into 3 phases so the SQL race window is minimal:

    1. SELECT existing row. If hit + hash match → return cached.
    2. Hit + hash mismatch → 409 Conflict.
    3. Miss → run compute_fn, INSERT. On IntegrityError (concurrent INSERT
       won by another worker), re-SELECT and replay that row.

    The caller owns the outer transaction; we do NOT commit here.
    """
    # Local import to avoid load-order cycle.
    from backend.core.database import IdempotencyKey

    existing: Optional[IdempotencyKey] = (
        session.query(IdempotencyKey)
        .filter(IdempotencyKey.key == key)
        .one_or_none()
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "IDEMPOTENCY_KEY_REUSED",
                    "message": (
                        "Idempotency-Key has been used previously with a "
                        "different request body. Re-mint a fresh UUID."
                    ),
                    "original_hash": existing.request_hash,
                    "given_hash": request_hash,
                },
            )
        # Cached replay — return without invoking compute_fn.
        try:
            payload = json.loads(existing.response_json or "null")
        except (TypeError, ValueError):
            payload = None
        return IdempotencyOutcome(
            replay=True,
            response_payload=payload,
            status_code=int(existing.status_code or 200),
        )

    # Miss path: compute, then persist atomically.
    payload, status_code = compute_fn()
    row = IdempotencyKey(
        key=key,
        request_hash=request_hash,
        # P15/D-M13 — truncate over-size blobs to a structured marker so the
        # row stays bounded; live response is still returned to the caller.
        response_json=_truncate_blob(json.dumps(payload, default=str, ensure_ascii=False)),
        status_code=int(status_code),
        created_at=datetime.now(timezone.utc),
    )
    try:
        # Wrap ONLY the idempotency-row INSERT in a SAVEPOINT so a UNIQUE
        # collision on `key` (concurrent winner already inserted) rolls back
        # ONLY this nested unit — never the caller-owned outer transaction.
        # The `with session.begin_nested()` context manager automatically
        # releases the SAVEPOINT on success and rolls it back on exception,
        # so we do NOT call session.rollback() on the shared session (which
        # would discard the caller's other work and violate the documented
        # "caller owns the outer transaction; we do NOT commit here" contract).
        with session.begin_nested():
            session.add(row)
            session.flush()  # surfaces IntegrityError without committing
    except IntegrityError:
        # Race-loser: re-read winner and replay.
        winner: Optional[IdempotencyKey] = (
            session.query(IdempotencyKey)
            .filter(IdempotencyKey.key == key)
            .one_or_none()
        )
        if winner is None:
            # Extremely improbable: insert failed but row also gone.
            raise HTTPException(
                status_code=500,
                detail="Idempotency table desync; please retry",
            )
        if winner.request_hash != request_hash:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "IDEMPOTENCY_KEY_REUSED",
                    "original_hash": winner.request_hash,
                    "given_hash": request_hash,
                },
            )
        try:
            cached = json.loads(winner.response_json or "null")
        except (TypeError, ValueError):
            cached = None
        return IdempotencyOutcome(
            replay=True,
            response_payload=cached,
            status_code=int(winner.status_code or 200),
        )

    return IdempotencyOutcome(
        replay=False,
        response_payload=payload,
        status_code=int(status_code),
    )


def purge_expired(session: Session) -> int:
    """Delete idempotency rows older than the configured TTL. Returns count."""
    from backend.core.database import IdempotencyKey

    cutoff = datetime.now(timezone.utc) - timedelta(hours=_ttl_hours())
    deleted = (
        session.query(IdempotencyKey)
        .filter(IdempotencyKey.created_at < cutoff)
        .delete(synchronize_session=False)
    )
    return int(deleted or 0)


def purge_expired_task() -> dict:
    """P11-B-18 — periodic-task wrapper around :func:`purge_expired`.

    Opens its own SQLAlchemy session so it can run from the periodic-task
    runner without a caller-managed session. Returns a dict so the runner's
    stats / logs look uniform with every other periodic task. Never raises.
    """
    from backend.core.database import session_scope
    try:
        with session_scope() as s:
            deleted = purge_expired(s)
        return {"deleted": int(deleted)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("idempotency.purge_expired_task failed")
        return {"deleted": 0, "error": f"{type(exc).__name__}: {exc}"[:200]}


__all__ = [
    "require_idempotency_key",
    "canonical_request_hash",
    "lookup_or_record",
    "purge_expired",
    "purge_expired_task",
    "IdempotencyOutcome",
]

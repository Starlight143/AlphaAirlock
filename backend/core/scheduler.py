"""In-process ingestion scheduler (P3).

Why asyncio loop instead of APScheduler:
  - One less dep on Windows
  - The validator flagged APScheduler + uvicorn `--reload` double-fires
  - For < 50 sources the diff in features is negligible

Behaviour:
  - Every TICK_SECONDS (default 30s) it scans all enabled IngestSource rows.
  - For each source it checks the circuit breaker (disabled_until) and the
    cadence (last_polled_at + cadence_minutes). If both gates allow, it runs
    `fetch_for(source)` in a thread (sync function inside asyncio.to_thread).
  - Successful items become KnowledgeNode rows (de-dup'd by content_hash).
  - Every attempt — success, skip, or fail — writes an IngestEvent row.

Cost guards (the validator was firm on these):
  - DEFAULT-OFF via ALPHA_INGEST_ENABLED env var (must be "1"/"true").
  - Tick interval clamped to >= 5 seconds.
  - Per-source consecutive_failures triggers exponential backoff
    capped at 7 days via `disabled_until`.
  - 401/403/404/410 immediately set `enabled=False` (hard-disable; need manual
    re-enable through the UI).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError, OperationalError

from backend.core.database import (
    KB_CONTENT_MAX_CHARS,
    IngestEvent,
    IngestSource,
    KnowledgeNode,
    session_scope,
)
from backend.core.ingest_fetchers import (
    FetchOutcome,
    fetch_for,
    is_stub_source_type,
    items_to_knowledge_nodes,
)

logger = logging.getLogger("alpha.scheduler")

_TASK: Optional[asyncio.Task] = None
_STOP = asyncio.Event()

# Per-source single-flight: the scheduler scan (`_scan_once` -> `_poll_one`) and
# the manual `/sources/{id}/poll` endpoint (`poll_source_now` -> `_poll_one`) can
# fire for the SAME source concurrently. `_run_fetcher_sync` reads
# `consecutive_failures` in one transaction and `_persist_outcome`
# read-modify-writes it in another, so without serialisation the backoff counter
# loses updates and the fetch runs twice (content-hash dedups nodes but NOT the
# failure counter / IngestEvent rows). One asyncio.Lock per source id serialises
# them; distinct sources still poll in parallel.
_POLL_LOCKS: dict[int, asyncio.Lock] = {}


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


# D-M6 — delegate to the canonical strict-whitelist env_bool from _envloader
# so typo'd values ("treu", "1.0", "Y") collapse to the default instead of
# accidentally evaluating True.
from backend._envloader import env_bool as _env_bool


def is_ingest_enabled() -> bool:
    """The master switch — UI shows this in the Sources page."""
    return _env_bool("ALPHA_INGEST_ENABLED", False)


def tick_seconds() -> int:
    # P15/D-M1 — delegate to canonical env_int helper for consistent parsing.
    from backend._envloader import env_int
    return env_int("ALPHA_INGEST_TICK_SECONDS", 30, minimum=5)


def max_concurrent_polls() -> int:
    """Upper bound on how many sources fetch + persist CONCURRENTLY per tick.

    The scheduler used to dispatch EVERY due source at once (``asyncio.gather``
    over all eligible ids). At startup — when every source's ``last_polled_at``
    is NULL so ALL are due — that fanned out into dozens of worker threads each
    issuing ``UPDATE ingest_sources`` against SQLite's single WAL writer lock,
    producing the intermittent ``database is locked`` thread-crashes. Capping
    concurrency turns that thundering herd into an orderly bounded pipeline;
    fetches are I/O-bound so a small cap barely affects throughput for < 50
    sources. Tunable via ``ALPHA_INGEST_MAX_CONCURRENCY`` (clamped 1..16).
    """
    from backend._envloader import env_int
    return env_int("ALPHA_INGEST_MAX_CONCURRENCY", 4, minimum=1, maximum=16)


def _sqlite_lock_retries() -> int:
    """How many times a short write transaction re-runs on a transient SQLite
    ``database is locked`` (including the non-``busy_timeout``-retryable
    ``SQLITE_BUSY_SNAPSHOT``) before giving up. Tunable via
    ``ALPHA_SQLITE_LOCK_RETRIES`` (clamped 1..12)."""
    from backend._envloader import env_int
    return env_int("ALPHA_SQLITE_LOCK_RETRIES", 5, minimum=1, maximum=12)


# Substrings that identify a *transient* SQLite write-lock contention. They all
# surface as sqlalchemy.exc.OperationalError and are safe to retry by re-running
# the whole short transaction (a fresh snapshot commits once the competing
# writer releases the WAL writer lock). Anything else (no-such-table, syntax,
# IntegrityError, real I/O) must propagate so genuine bugs still surface.
_SQLITE_LOCK_MARKERS = (
    "database is locked",
    "database table is locked",
    "database is busy",
)


def _is_sqlite_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    msg = str(getattr(exc, "orig", None) or exc).lower()
    return any(m in msg for m in _SQLITE_LOCK_MARKERS)


def _retry_on_locked(fn, *, what: str):
    """Run ``fn`` (a self-contained short DB transaction) and, on a TRANSIENT
    SQLite ``database is locked`` error, re-run it with exponential backoff +
    jitter. ``session_scope`` has already rolled the failed attempt back, so a
    clean retry re-reads a fresh snapshot and commits.

    ``time.sleep`` is safe here because every caller runs inside
    ``asyncio.to_thread`` (a worker thread), never on the event loop.
    """
    attempts = _sqlite_lock_retries()
    for i in range(attempts):
        try:
            return fn()
        except OperationalError as exc:
            if not _is_sqlite_lock_error(exc) or i >= attempts - 1:
                raise
            # 50ms, 100ms, 200ms, 400ms, 800ms … capped at 2s, plus up to 60ms
            # jitter so concurrent retriers desynchronise instead of re-colliding.
            delay = min(2.0, 0.05 * (2 ** i)) + random.uniform(0.0, 0.06)
            logger.warning(
                "SQLite write-lock on %s (attempt %d/%d) — retrying in %.3fs",
                what, i + 1, attempts, delay,
            )
            time.sleep(delay)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def start_scheduler() -> None:
    """Idempotent — safe to call multiple times during lifespan setup."""
    global _TASK
    if not is_ingest_enabled():
        logger.info("Scheduler not started (ALPHA_INGEST_ENABLED is off)")
        return
    if _TASK and not _TASK.done():
        return
    _STOP.clear()
    _TASK = asyncio.create_task(_run_loop(), name="alpha-ingest-loop")
    logger.info("Ingestion scheduler started (tick=%ss)", tick_seconds())


async def stop_scheduler() -> None:
    """Cooperative cancel — gives the loop a chance to finish its current pass."""
    _STOP.set()
    if _TASK and not _TASK.done():
        try:
            await asyncio.wait_for(_TASK, timeout=5.0)
        except asyncio.TimeoutError:
            _TASK.cancel()
    # R5/BE-DATA-003 (scheduler variant): drop stale locks bound to the old event
    # loop so that a subsequent start_scheduler() (e.g. after uvicorn --reload)
    # does not hit RuntimeError("Task got Future attached to a different loop")
    # when _poll_one re-uses the cached Lock objects via setdefault.
    _POLL_LOCKS.clear()


def is_running() -> bool:
    return bool(_TASK and not _TASK.done())


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------


async def _run_loop() -> None:
    try:
        while not _STOP.is_set():
            try:
                await _scan_once()
            except Exception:
                logger.exception("Scheduler tick crashed; will retry next interval")
            tick = tick_seconds()
            try:
                await asyncio.wait_for(_STOP.wait(), timeout=tick)
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        logger.info("Scheduler cancelled")
    finally:
        logger.info("Scheduler exiting")


def _live_source_ids() -> set[int]:
    """Return the set of currently-enabled source IDs (sync, thread-safe)."""
    with session_scope() as s:
        return {int(r.id) for r in s.query(IngestSource.id).filter(IngestSource.enabled == True).all()}  # noqa: E712


async def _scan_once() -> None:
    """Single tick: scan eligible sources and dispatch fetchers."""
    eligible_ids = await asyncio.to_thread(_due_source_ids)
    # Prune locks for sources that are no longer enabled/present to avoid
    # unbounded growth in long-running instances with high source churn.
    if _POLL_LOCKS:
        live_ids = await asyncio.to_thread(_live_source_ids)
        # Only prune locks for stale sources that are NOT currently held.
        # A held lock means a _poll_one coroutine is mid-fetch for that source;
        # popping the dict entry while the lock is held discards the object from
        # the dict so a concurrent poll_source_now call creates a fresh unlocked
        # Lock via setdefault, bypassing the serialisation guarantee entirely.
        # Once the in-flight fetch completes and releases the lock, the entry
        # will be cleaned up on the next tick.
        stale = [sid for sid in list(_POLL_LOCKS) if sid not in live_ids and not _POLL_LOCKS[sid].locked()]
        for sid in stale:
            _POLL_LOCKS.pop(sid, None)
    if not eligible_ids:
        return
    logger.debug("Scheduler tick — %s sources due", len(eligible_ids))
    # Bound the write fan-out: dispatch at most ``max_concurrent_polls()``
    # sources at a time instead of all of them at once, so simultaneous
    # ``UPDATE ingest_sources`` writers never pile up on SQLite's single WAL
    # writer lock (the root of the intermittent 'database is locked' crashes).
    # A per-tick semaphore is sufficient — each tick's gather owns its own, and
    # the per-source _POLL_LOCKS still guarantee single-flight per source.
    sem = asyncio.Semaphore(max_concurrent_polls())

    async def _bounded_poll(sid: int) -> None:
        async with sem:
            await _poll_one(sid)

    await asyncio.gather(*[_bounded_poll(sid) for sid in eligible_ids])


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """P32-TZ32-1 — SQLite returns naive datetimes for `DateTime(timezone=True)`
    columns; re-attach UTC so comparison with aware `now` doesn't TypeError."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _due_source_ids() -> list[int]:
    now = datetime.now(timezone.utc)
    out: list[int] = []
    with session_scope() as s:
        rows = s.query(IngestSource).filter(IngestSource.enabled == True).all()  # noqa: E712
        for src in rows:
            # Stub source types never have a real fetcher — don't waste a tick
            # producing a `skip` event every cadence interval.
            if is_stub_source_type(src.source_type or ""):
                continue
            disabled_until = _as_utc(src.disabled_until)
            if disabled_until and disabled_until > now:
                continue
            cadence = max(5, int(src.cadence_minutes or 60))
            last_polled = _as_utc(src.last_polled_at)
            if last_polled:
                next_due = last_polled + timedelta(minutes=cadence)
                if next_due > now:
                    continue
            out.append(int(src.id))
    return out


async def _poll_one(source_id: int) -> None:
    """Run the fetcher off the event loop, then persist outcome.

    Serialised per source via ``_POLL_LOCKS`` so a manual poll and a scheduled
    scan can't interleave the read-modify-write of the source's failure counter.
    """
    lock = _POLL_LOCKS.setdefault(source_id, asyncio.Lock())
    async with lock:
        try:
            outcome, src_snapshot = await asyncio.to_thread(_run_fetcher_sync, source_id)
        except Exception as exc:
            logger.exception("Fetcher thread crashed for source %s", source_id)
            outcome = FetchOutcome(error=f"thread-crash: {exc}")
            src_snapshot = None
        # Persist under the same transient-lock retry: the success/failure path
        # also issues ``UPDATE ingest_sources`` (counters, circuit breaker) and
        # can hit SQLITE_BUSY_SNAPSHOT under cross-daemon write contention.
        # _retry_on_locked re-runs the whole persist transaction — it is
        # idempotent (content_hash dedup + per-row SAVEPOINTs), and the failure
        # path carries no items so its retry does no network work.
        await asyncio.to_thread(
            _persist_outcome_with_retry, source_id, outcome, src_snapshot
        )


def _run_fetcher_sync(source_id: int):
    """Load the source row, stamp ``last_polled_at``, then run its fetcher.

    The network fetch (``fetch_for``) runs **outside any open transaction**.
    Holding a read snapshot open across it was the root cause of the
    intermittent ``database is locked`` crash on
    ``UPDATE ingest_sources SET last_polled_at``:

      * SQLite (WAL) takes a read snapshot on the first SELECT — ``s.get`` below.
      * ``fetch_for`` then performs seconds-to-minutes of network I/O (RSS +
        arXiv full-text + article enrichment + Reddit comments).
      * The deferred ``last_polled_at`` UPDATE is flushed only at commit time,
        which must upgrade that now-stale snapshot to a write. If *any* other
        writer (paper tick, alpha_queue, ic_decay, …) committed during the
        fetch, SQLite returns ``SQLITE_BUSY_SNAPSHOT`` — which ``busy_timeout``
        does **not** retry — surfacing as ``OperationalError: database is
        locked`` and bubbling up as the ``thread-crash`` IngestEvent.

    Fix: commit the ``last_polled_at`` write in its own short transaction and
    release it *before* the fetch. ``src`` stays usable afterwards because the
    session is configured ``expire_on_commit=False`` and every fetcher reads
    only already-loaded scalar columns (never a lazy relationship), so the
    detached instance never triggers a post-close DB load.

    Returns ``(outcome, snapshot)``.
    """
    # Phase 1 — short write txn: snapshot the fields the persister may want and
    # stamp last_polled_at. Committing here also guarantees the timestamp sticks
    # even when the fetch later raises (busy-loop guard) — the old single-txn
    # shape silently rolled the timestamp back on a crash.
    #
    # Wrapped in _retry_on_locked: this is the EXACT statement in the reported
    # crash (``UPDATE ingest_sources SET last_polled_at``). Even as a short txn
    # it opens with a SELECT (``s.get``) read-snapshot that the deferred UPDATE
    # must upgrade at commit; a competing writer in that window yields
    # SQLITE_BUSY_SNAPSHOT, which busy_timeout does NOT retry. Re-running the
    # whole short txn takes a fresh snapshot and commits cleanly.
    def _stamp_polled():
        with session_scope() as s:
            src = s.get(IngestSource, source_id)
            if src is None:
                return None, None
            # Snapshot fields the persister needs (avoid lazy-load after close).
            snap = {
                "source_type": src.source_type,
                "name": src.name,
                "url": src.url,
                "consecutive_failures": int(src.consecutive_failures or 0),
            }
            # Mark "polled now" up-front so a hang doesn't busy-loop.
            src.last_polled_at = datetime.now(timezone.utc)
            return src, snap

    src, snap = _retry_on_locked(_stamp_polled, what="ingest_sources last_polled_at")
    if src is None:
        return FetchOutcome(error="source vanished mid-tick"), None
    # Phase 2 — network fetch with NO transaction held. ``src`` is now detached;
    # the fetchers read only its already-loaded scalar columns.
    outcome = fetch_for(src)
    return outcome, snap


def _persist_outcome(source_id: int, outcome: FetchOutcome, snap: Optional[dict]) -> None:
    """Translate the FetchOutcome into KnowledgeNode + IngestEvent rows + update
    the source's failure counters / circuit breaker.

    P6-B1: after the transaction commits, hand newly inserted node ids to the
    auto-pipeline hook. The hook itself is double-gated (env flag + per-source
    daily quota) so this stays a no-op for cost-conscious deployments.
    """
    now = datetime.now(timezone.utc)
    new_node_ids: list[int] = []

    # P6/concurrency — pre-download inline images OUTSIDE the persist transaction
    # below. The asset cache interleaves network downloads with INSERTs; running
    # it inside the transaction held a SQLite read-snapshot / write-lock across
    # seconds of image I/O, which both risked SQLITE_BUSY_SNAPSHOT ('database is
    # locked') on the first write and blocked every other writer for the whole
    # download. Downloading first (no session) and handing the bytes to
    # enrich_markdown_with_images(prefetched=...) keeps the transaction
    # network-free. A combined {url: bytes|None} map dedups images shared across
    # items; the enrich pass still applies the per-item cap to each item.
    prefetched: dict = {}
    if outcome.items and not outcome.error:
        try:
            from backend.core.asset_cache import (
                is_enabled as _asset_cache_enabled,
                prefetch_image_bytes,
            )
            if _asset_cache_enabled():
                from backend.core.ingest_fetchers import _content_hash_for_item
                # Short READ-ONLY pass: items whose content_hash already exists
                # are dups the persist loop will skip — don't waste a network
                # round-trip re-downloading their (unchanged) images on every
                # poll. A race here is harmless: the persist loop's own dedup +
                # IntegrityError SAVEPOINT remain the source of truth.
                hashes = [_content_hash_for_item(_it) for _it in outcome.items]
                existing: set = set()
                with session_scope() as _s:
                    for (_h,) in (
                        _s.query(KnowledgeNode.content_hash)
                        .filter(KnowledgeNode.content_hash.in_(hashes))
                        .all()
                    ):
                        existing.add(_h)
                for _it, _hash in zip(outcome.items, hashes):
                    if _hash in existing:
                        continue  # already ingested — skip its image downloads
                    raw = getattr(_it, "raw_html", "") or ""
                    if not raw:
                        continue
                    for _u, _b in prefetch_image_bytes(raw).items():
                        prefetched.setdefault(_u, _b)
        except Exception:  # noqa: BLE001 — prefetch is best-effort; never block persist
            logger.exception("Asset prefetch failed for source %s", source_id)
            prefetched = {}

    with session_scope() as s:
        src = s.get(IngestSource, source_id)
        if src is None:
            return

        if outcome.error:
            # Failure path — backoff.
            # R5/SRE-001: honour an upstream Retry-After (e.g. HTTP 429). When the
            # source politely told us when to retry, respect that exact window and
            # do NOT increment consecutive_failures, so a well-behaved rate-limited
            # source does not accumulate toward the exponential circuit-break.
            if getattr(outcome, "retry_after_seconds", None) is not None and int(outcome.retry_after_seconds) > 0:
                src.disabled_until = now + timedelta(seconds=int(outcome.retry_after_seconds))
                src.last_error_message = outcome.error[:1024]
                s.add(
                    IngestEvent(
                        source_id=source_id,
                        fetched_at=now,
                        item_url=None,
                        item_hash=None,
                        status="fail",
                        error_msg=outcome.error[:1024],
                    )
                )
                return
            src.consecutive_failures = int(src.consecutive_failures or 0) + 1
            src.last_error_message = outcome.error[:1024]
            backoff_min = min(60 * 24 * 7, 2 ** min(src.consecutive_failures, 14))
            src.disabled_until = now + timedelta(minutes=backoff_min)
            # Hard-disable on permanent-looking errors.
            for code in ("401", "403", "404", "410"):
                if code in outcome.error:
                    src.enabled = False
                    src.disabled_until = None
                    src.last_error_message = (
                        f"Auto-disabled after permanent error ({code}). "
                        "Re-enable from /sources after fixing the URL."
                    )
                    break
            s.add(
                IngestEvent(
                    source_id=source_id,
                    fetched_at=now,
                    item_url=None,
                    item_hash=None,
                    status="fail",
                    error_msg=outcome.error[:1024],
                )
            )
            return

        # Success path.
        src.consecutive_failures = 0
        src.disabled_until = None
        src.last_error_message = None
        src.last_success_at = now

        if outcome.skipped and not outcome.items:
            # Eg manual / twitter STUBs — log a skip event, no nodes.
            s.add(
                IngestEvent(
                    source_id=source_id,
                    fetched_at=now,
                    status="skip",
                    error_msg=outcome.notes[:1024] if outcome.notes else None,
                )
            )
            return

        # Lazy import keeps asset_cache out of the cold path when the feature
        # is off — the module touches disk just to ensure its storage dir.
        from backend.core.asset_cache import (
            attach_asset_node_ids,
            enrich_markdown_with_images,
            is_enabled as asset_cache_enabled,
        )

        node_rows = items_to_knowledge_nodes(outcome.items, src)
        for item_dict in node_rows:
            src_url = item_dict["source_url"]
            new_hash = item_dict["content_hash"]

            # Identify the node already representing THIS article. Prefer
            # source_url (stable article identity) so a re-poll whose body grew
            # (teaser → full text) UPDATES the same node in place — the old
            # content_hash-only dedup let every such growth mint a fresh
            # near-duplicate, which is how the KB accumulated teaser+partial+full
            # copies of one article. Fall back to content_hash when the item has
            # no url.
            existing = None
            if src_url:
                existing = (
                    s.query(KnowledgeNode)
                    .filter(KnowledgeNode.source_url == src_url)
                    .order_by(KnowledgeNode.id)
                    .first()
                )
            if existing is None:
                existing = (
                    s.query(KnowledgeNode)
                    .filter(KnowledgeNode.content_hash == new_hash)
                    .first()
                )

            # Skip when the article is unchanged or the new body is no longer
            # than what we already store (keep-the-fuller-version) — no wasted
            # image fetch, no churn.
            if existing is not None and (
                new_hash == existing.content_hash
                or len(item_dict["content"]) <= len(existing.content or "")
            ):
                s.add(
                    IngestEvent(
                        source_id=source_id,
                        fetched_at=now,
                        item_url=src_url,
                        item_hash=new_hash,
                        status="skip",
                        error_msg="dup content_hash" if new_hash == existing.content_hash
                        else "existing copy not shorter",
                        resulting_node_id=existing.id,
                    )
                )
                continue

            # P6 — inline-image cache: rewrite body to include /api/assets/{hash}
            # markdown refs BEFORE persisting, so the stored content is what the
            # KB viewer renders. Asset rows are also created here; linked back to
            # the node after flush.
            enriched_body = item_dict["content"]
            asset_ids: list[int] = []
            if asset_cache_enabled():
                try:
                    enriched_body, asset_ids = enrich_markdown_with_images(
                        body_markdown=item_dict["content"],
                        raw_html=item_dict.get("raw_html", ""),
                        source_id=source_id,
                        session=s,
                        prefetched=prefetched,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Asset cache enrichment failed for source %s", source_id)
                    enriched_body = item_dict["content"]
                    asset_ids = []

            if existing is not None:
                # UPDATE the existing node in place with the fuller body. SAVEPOINT
                # so a content_hash collision (the new hash already belongs to a
                # DIFFERENT article/node) rolls back only this row and leaves the
                # existing node untouched.
                try:
                    with s.begin_nested():
                        existing.content = enriched_body[:KB_CONTENT_MAX_CHARS]
                        existing.content_hash = new_hash
                        if item_dict["category"]:
                            existing.category = item_dict["category"]
                        existing.ingested_at = item_dict["ingested_at"]
                        s.flush()
                except IntegrityError:
                    s.add(
                        IngestEvent(
                            source_id=source_id,
                            fetched_at=now,
                            item_url=src_url,
                            item_hash=new_hash,
                            status="skip",
                            error_msg="update hash-collision",
                            resulting_node_id=existing.id,
                        )
                    )
                    continue
                if asset_ids:
                    try:
                        attach_asset_node_ids(asset_ids, int(existing.id), s)
                    except Exception:  # noqa: BLE001
                        logger.exception("Asset-node linkback failed for node %s", existing.id)
                src.last_item_hash = new_hash
                s.add(
                    IngestEvent(
                        source_id=source_id,
                        fetched_at=now,
                        item_url=src_url,
                        item_hash=new_hash,
                        status="ok",
                        error_msg="updated",
                        resulting_node_id=existing.id,
                    )
                )
                # An update is NOT a new node for the auto-pipeline gate.
                continue

            # Brand-new article → INSERT.
            node = KnowledgeNode(
                title=item_dict["title"],
                content=enriched_body[:KB_CONTENT_MAX_CHARS],
                tags=item_dict["tags"],
                links="[]",
                ic_score=0.0,
                kind=item_dict["kind"],
                source_url=src_url,
                source_type=item_dict["source_type"],
                category=item_dict["category"],
                content_hash=new_hash,
                ingested_at=item_dict["ingested_at"],
            )
            try:
                # R5/BE-DATA-001: wrap the INSERT in a SAVEPOINT so a mid-batch
                # duplicate content_hash rolls back ONLY this insert, not the whole
                # session (which would discard prior src updates + earlier rows).
                # Mirrors idempotency.py / asset_cache.py begin_nested() usage.
                with s.begin_nested():
                    s.add(node)
                    s.flush()  # P13/D-H2 — surface IntegrityError immediately
            except IntegrityError:
                # P13/D-H2 — Another worker raced ahead and inserted the same
                # content_hash. Find the winning row and record a skip event
                # against it instead of dropping the work silently.
                winner = (
                    s.query(KnowledgeNode)
                    .filter(KnowledgeNode.content_hash == new_hash)
                    .first()
                )
                if winner is not None:
                    s.add(
                        IngestEvent(
                            source_id=source_id,
                            fetched_at=now,
                            item_url=src_url,
                            item_hash=new_hash,
                            status="skip",
                            error_msg="dup content_hash (race-loser)",
                            resulting_node_id=winner.id,
                        )
                    )
                    continue
                raise  # genuine error, not the race we expected
            if asset_ids:
                try:
                    attach_asset_node_ids(asset_ids, int(node.id), s)
                except Exception:  # noqa: BLE001
                    logger.exception("Asset-node linkback failed for node %s", node.id)
            src.last_item_hash = new_hash
            s.add(
                IngestEvent(
                    source_id=source_id,
                    fetched_at=now,
                    item_url=src_url,
                    item_hash=new_hash,
                    status="ok",
                    resulting_node_id=node.id,
                )
            )
            new_node_ids.append(int(node.id))

    # P6-B1: AFTER commit, hand new nodes to the auto-pipeline gate. The hook
    # is double-disabled by default (env flag + daily per-source quota); when
    # disabled this lookup is a single bool check.
    if new_node_ids:
        try:
            from backend.core.auto_pipeline import maybe_trigger_pipeline_for_nodes
            maybe_trigger_pipeline_for_nodes(source_id, new_node_ids)
        except Exception:  # noqa: BLE001
            logger.exception("auto-pipeline hook failed for source %s", source_id)
    return


def _persist_outcome_with_retry(
    source_id: int, outcome: FetchOutcome, snap: Optional[dict]
) -> None:
    """``_persist_outcome`` under transient-SQLite-lock retry. A re-run is safe:
    a lock error rolls the whole transaction back uncommitted, and the persist
    body is idempotent (source_url / content_hash dedup + SAVEPOINT-guarded
    inserts), so the retry re-derives the same rows from a fresh snapshot."""
    _retry_on_locked(
        lambda: _persist_outcome(source_id, outcome, snap),
        what="ingest_sources persist",
    )


# Exposed for the /api/sources/{id}/poll endpoint.
async def poll_source_now(source_id: int) -> None:
    await _poll_one(source_id)


__all__ = [
    "start_scheduler",
    "stop_scheduler",
    "is_running",
    "is_ingest_enabled",
    "poll_source_now",
    "tick_seconds",
]

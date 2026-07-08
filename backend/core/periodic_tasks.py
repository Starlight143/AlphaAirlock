"""Independent periodic-task runner (P6 — cross-cutting safety guard #3).

Why this exists separately from ``scheduler.py``
------------------------------------------------
``scheduler.py`` is dedicated to the ingest tick loop (sources → KnowledgeNode).
Stuffing other periodic jobs (KB relink, IC decay, Granger recompute, Telegram
6-hour reports, paper-trade tick) into the same loop would:
  - lengthen each tick from <1s to potentially minutes;
  - block ingest while a long-running batch runs;
  - couple unrelated cadences (ingest=30s vs Granger=weekly) to the same retry
    semantics.

This module instead spawns one asyncio task per scheduled job, each owning its
own sleep loop, error handling, and last-run timestamp. Jobs are loaded lazily
so optional features (B3/B5/B6) can be shipped to production with their env
flags off and their modules absent — no import errors at startup.

Design rules
------------
* **Every job's env flag defaults OFF** (CLAUDE.md cost-guard rule). The lifecycle
  function checks ``is_enabled()`` per job; disabled jobs never start a task.
* **Cooperative cancel via ``_STOP``** matches the pattern in scheduler.py and
  telegram_inbound.py — keeps lifespan shutdown predictable.
* **Lazy import inside the task** — a missing module surfaces as a single error
  log line and disables that job for the remainder of the process lifetime,
  rather than crashing startup.
* **Per-task lock** prevents reentrant runs if a job runs longer than its
  cadence (e.g. Granger recompute > 1h).
* **Best-effort persistence** — last-run timestamps live in-memory; the DB-side
  ``TelegramReportLog`` table provides cross-restart idempotency for Telegram
  reports specifically.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, List, Optional

from backend._envloader import env_bool, env_int

logger = logging.getLogger("alpha.periodic")


# ---------------------------------------------------------------------------
# Task registry
# ---------------------------------------------------------------------------
#
# Each entry: (id, interval_seconds, module_path, callable_name, env_flag,
# default_enabled). The runner imports `module_path` lazily on first tick;
# if the import fails (module not present yet), the task disables itself.
#
# `callable_name` may be sync or async. The runner detects with
# `asyncio.iscoroutinefunction` and wraps sync calls in `asyncio.to_thread`.

_DEFAULT_INTERVALS = {
    "paper_trade_tick": 30,                # 30 s — M12
    "telegram_paper_health": 60 * 60,      # hourly — B6
    "telegram_six_hour": 6 * 60 * 60,      # 6 h — B6
    "telegram_morning": 24 * 60 * 60,      # daily — B6
    "kb_relink": 12 * 60 * 60,             # 12 h — B3
    "ic_decay": 24 * 60 * 60,              # daily — B3
    "granger_recompute": 7 * 24 * 60 * 60, # weekly — B5
    "alpha_queue_tick": 60 * 60,           # hourly — B2 (when ALPHA_QUEUE_ENABLED)
    "evolution_tick": 60 * 60,             # hourly — T3-A (when EVOLUTION_ENABLED)
    "orphan_reservation_sweep": 30 * 60,   # 30 min — B2/P11-B-06 stale-sentinel recovery
    "pipeline_orphan_reap": 10 * 60,       # 10 min — flush strategies stuck in a non-terminal stage after a worker crash/restart
    "idempotency_purge": 24 * 60 * 60,     # daily — P11-B-18
    "manual_order_reap": 60,               # 1 min — P15/D-H5 reap pending limits + 7d orphan sweep
    "agent_dialogue_gc": 24 * 60 * 60,     # daily — P15/D-M20 GC stale turn counters
    "market_data_refresh": 60 * 60,        # hourly — P-REALDATA incremental price refresh + forward-sim advance
    "cost_ledger_prune": 24 * 60 * 60,     # daily — FinOps ledger retention sweep
}


# (id, env_flag, module_path, callable_name)
_TASK_SPECS: List[tuple[str, str, str, str]] = [
    # M12 — paper-trade tick. Lazy import so an env-disabled deployment never
    # touches the paper_scheduler module.
    ("paper_trade_tick", "ALPHA_PAPER_TICK_ENABLED",
     "backend.core.paper_scheduler", "tick_all_active"),
    # B6 — Telegram reports
    ("telegram_six_hour", "TG_SIX_HOUR_ENABLED",
     "backend.core.telegram_reports", "send_six_hour_report"),
    ("telegram_morning", "TG_MORNING_ENABLED",
     "backend.core.telegram_reports", "send_morning_briefing"),
    ("telegram_paper_health", "TG_PAPER_HEALTH_ENABLED",
     "backend.core.telegram_reports", "send_paper_trade_health_report"),
    # B3 — KB relink + IC decay
    ("kb_relink", "KB_NIGHTLY_ENABLED",
     "backend.core.kb_relink", "relink_recent"),
    ("ic_decay", "IC_DECAY_ENABLED",
     "backend.core.ic_history", "decay_all_ic"),
    # B5 — Granger weekly recompute
    ("granger_recompute", "GRANGER_ENABLED",
     "backend.core.granger", "recompute_top_pairs"),
    # B2 — IC-priority alpha queue tick
    ("alpha_queue_tick", "ALPHA_QUEUE_ENABLED",
     "backend.core.alpha_queue", "tick_queue"),
    # T3-A — evolutionary hypothesis-tree search. Default OFF (EVOLUTION_ENABLED).
    # Mutates/specialises surviving alphas; tick_evolution is a no-op unless the
    # EVOLUTION_FRACTION coin-flip fires. Idempotent, never raises.
    ("evolution_tick", "EVOLUTION_ENABLED",
     "backend.core.evolution", "tick_evolution"),
    # P11-B-06 — orphan reservation sweep (B2 / alpha_queue stuck-sentinel recovery).
    # Runs every 30 min; uses stale_minutes=30 so only genuinely stuck sentinels
    # (stamped > 30 min ago) are released.  Callable is idempotent and never raises.
    # NOTE: _invoke calls fn() with no args; the wrapper below supplies stale_minutes=30.
    ("orphan_reservation_sweep", "ORPHAN_RESERVATION_SWEEP_ENABLED",
     "backend.core.auto_pipeline", "_recover_orphan_30min"),
    # Pipeline orphan reaper — flush strategies stuck in a non-terminal stage
    # (INTAKE/STORY_GEN/CODE_GEN/BACKTESTING/CRITIC_LOOP) whose worker died
    # without running the REJECT handler (process restart / freeze). Default ON
    # (zero-cost pure-DB sweep, like idempotency_purge); skips any strategy with
    # a live in-process run and only flushes rows stale > 30 min. Idempotent,
    # never raises. Disable with PIPELINE_ORPHAN_REAP_ENABLED=0.
    ("pipeline_orphan_reap", "PIPELINE_ORPHAN_REAP_ENABLED",
     "backend.core.orchestrator", "reap_orphaned_pipelines"),
    # P11-B-18 — idempotency-table TTL purge. Default ON so the 24h TTL is
    # enforced out-of-the-box; opt out with IDEMPOTENCY_PURGE_ENABLED=0.
    # Daily cadence keeps the table small.
    ("idempotency_purge", "IDEMPOTENCY_PURGE_ENABLED",
     "backend.core.idempotency", "purge_expired_task"),
    # P15/D-H5 — manual order reaper (TRADING_TERMINAL_ENABLED=1 gates it).
    # 60s cadence cross-checks pending limit orders against current mid + 7d
    # orphan sweep. Lazy import so disabled deployments don't pay the cost.
    ("manual_order_reap", "TRADING_TERMINAL_ENABLED",
     "backend.core.manual_order_reaper", "tick"),
    # P15/D-M20 — agent_dialogue turn-counter GC. Default ON; drops in-memory
    # bookkeeping for strategies untouched > 24h.
    ("agent_dialogue_gc", "AGENT_DIALOGUE_GC_ENABLED",
     "backend.core.agent_dialogue", "_gc_turn_counter"),
    # P-REALDATA / P-SIM — hourly incremental refresh of real market-data CSVs,
    # THEN advance every active forward-sim account over the freshly-landed bars.
    # Default ON so the sim curves stay current out-of-the-box; the price feed is
    # free (Binance public / Yahoo), only network egress. Disable with
    # MARKET_DATA_REFRESH_ENABLED=0; cadence via
    # PERIODIC_MARKET_DATA_REFRESH_SECONDS (default 3600). The callable lives in
    # sim_account (which already depends on market_data) so the data layer never
    # imports the trading-sim layer. Idempotent + never raises (best-effort).
    ("market_data_refresh", "MARKET_DATA_REFRESH_ENABLED",
     "backend.core.sim_account", "refresh_market_and_tick"),
    # FinOps — prune cost-ledger rows older than LLM_COST_LEDGER_RETENTION_DAYS.
    # Default ON (cheap pure-DB sweep like idempotency_purge); keeps the
    # append-only ledger bounded on multi-year deployments. Idempotent, no args.
    ("cost_ledger_prune", "LLM_COST_LEDGER_ENABLED",
     "backend.core.cost_ledger", "prune_old"),
]


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------

_TASKS: Dict[str, asyncio.Task] = {}
_LAST_RUN: Dict[str, Optional[datetime]] = {}
_LAST_ERROR: Dict[str, Optional[str]] = {}
_DISABLED_BY_IMPORT: set[str] = set()
_STOP: Optional[asyncio.Event] = None
# Per-task asyncio.Lock — populated lazily in _job_loop.  The lock is
# non-blocking (acquire_nowait): if a previous tick is still running the
# current tick is skipped with a warning, matching the docstring guarantee.
_TASK_LOCKS: Dict[str, asyncio.Lock] = {}


def _stop_event() -> asyncio.Event:
    """Return the module-level stop event, creating it lazily inside the running loop."""
    global _STOP
    if _STOP is None:
        _STOP = asyncio.Event()
    return _STOP


# Per-task enable default. EVERY task defaults OFF (CLAUDE.md cost-guard rule)
# EXCEPT those listed here. idempotency_purge defaults ON so the 24h
# IDEMPOTENCY_TTL_HOURS purge is enforced out-of-the-box and the
# idempotency_keys table stays bounded; operators can still opt out with
# IDEMPOTENCY_PURGE_ENABLED=0 (env_bool whitelist honors 0/false/no/off).
_FLAG_DEFAULT_ON: frozenset[str] = frozenset({
    "idempotency_purge", "agent_dialogue_gc", "pipeline_orphan_reap",
    "cost_ledger_prune",
    # P-SIM — keep the real price CSVs current AND advance the forward-sim
    # accounts automatically. Free feed (Binance public / Yahoo); opt out with
    # MARKET_DATA_REFRESH_ENABLED=0.
    "market_data_refresh",
})


def _flag_enabled(task_id: str, env_flag: str) -> bool:
    """Resolve a task's enabled state, honoring its per-task default."""
    return env_bool(env_flag, task_id in _FLAG_DEFAULT_ON)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _interval_seconds(task_id: str) -> int:
    """Allow per-task override via env (e.g. PERIODIC_PAPER_TRADE_TICK_SECONDS)."""
    env_key = f"PERIODIC_{task_id.upper()}_SECONDS"
    return env_int(env_key, _DEFAULT_INTERVALS.get(task_id, 3600), minimum=10)


def _resolve_callable(module_path: str, callable_name: str) -> Optional[Callable]:
    try:
        mod = importlib.import_module(module_path)
    except ImportError as exc:
        logger.warning("Periodic task module not available (%s): %s", module_path, exc)
        return None
    fn = getattr(mod, callable_name, None)
    if fn is None or not callable(fn):
        logger.warning(
            "Periodic task callable missing: %s.%s", module_path, callable_name,
        )
        return None
    return fn


async def _invoke(fn: Callable) -> object:
    """Call sync or async function uniformly. Returns the callable's result."""
    if asyncio.iscoroutinefunction(fn):
        return await fn()
    return await asyncio.to_thread(fn)


async def _job_loop(task_id: str, env_flag: str, module_path: str, callable_name: str) -> None:
    interval = _interval_seconds(task_id)
    logger.info(
        "Periodic task started: %s every %ss (env=%s)",
        task_id, interval, env_flag,
    )
    # First-fire delay: don't smash all jobs at boot — stagger by 1 s × task ordinal.
    initial_delay = 5  # small constant; cron-like jobs don't need instant first-fire
    try:
        await asyncio.wait_for(_stop_event().wait(), timeout=initial_delay)
        return  # stop requested during startup grace
    except asyncio.TimeoutError:
        pass

    while not _stop_event().is_set():
        # Re-check env flag every iteration so live env edits take effect on
        # the next tick (matters for ops who want to disable a noisy task
        # without restarting the API).
        if not _flag_enabled(task_id, env_flag):
            try:
                await asyncio.wait_for(_stop_event().wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
            return  # stop requested

        if task_id in _DISABLED_BY_IMPORT:
            logger.info("Periodic task %s disabled (import failed earlier)", task_id)
            return

        fn = _resolve_callable(module_path, callable_name)
        if fn is None:
            _DISABLED_BY_IMPORT.add(task_id)
            _LAST_ERROR[task_id] = f"module {module_path} unavailable"
            return

        # --- reentrant-run guard (P50) ---
        # Lazily populate a per-task asyncio.Lock.  acquire_nowait semantics:
        # if the lock is already held, skip this tick and log a warning.
        if task_id not in _TASK_LOCKS:
            _TASK_LOCKS[task_id] = asyncio.Lock()
        lock = _TASK_LOCKS[task_id]
        if lock.locked():
            logger.warning(
                "Periodic task %s: previous tick still running, skipping this tick",
                task_id,
            )
            try:
                await asyncio.wait_for(_stop_event().wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
            continue  # R5/BE-DATA-004: stop fired; outer while-guard handles exit (survives transient stop/start)
        await lock.acquire()
        try:
            result = await _invoke(fn)
            _LAST_RUN[task_id] = datetime.now(timezone.utc)
            _LAST_ERROR[task_id] = None
            if result is not None:
                logger.debug("Periodic task %s returned %r", task_id, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Periodic task %s crashed", task_id)
            _LAST_ERROR[task_id] = f"{type(exc).__name__}: {exc}"
        finally:
            lock.release()

        try:
            await asyncio.wait_for(_stop_event().wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


# ---------------------------------------------------------------------------
# Public lifecycle
# ---------------------------------------------------------------------------


async def start_periodic_tasks() -> None:
    """Start every registered task whose env flag is currently ON.

    Idempotent: safe to call multiple times during lifespan setup. Tasks whose
    env flag flips ON after startup will be picked up on the next call (the
    lifespan in main.py only calls this once, so to dynamically enable a task
    operators must restart the API).
    """
    _stop_event().clear()
    for task_id, env_flag, module_path, callable_name in _TASK_SPECS:
        if task_id in _TASKS and not _TASKS[task_id].done():
            continue
        if not _flag_enabled(task_id, env_flag):
            logger.info("Periodic task %s not started (%s is off)", task_id, env_flag)
            continue
        # Clear any stale import-failure record so a lifespan restart (e.g.
        # uvicorn --reload after pip-installing a missing optional dependency)
        # gets a clean slate.  The guard at _job_loop line 216-218 would
        # otherwise make the freshly-created task exit immediately.
        _DISABLED_BY_IMPORT.discard(task_id)
        _TASKS[task_id] = asyncio.create_task(
            _job_loop(task_id, env_flag, module_path, callable_name),
            name=f"alpha-periodic-{task_id}",
        )
    if _TASKS:
        logger.info("Periodic task runner: %s active jobs", len(_TASKS))


async def stop_periodic_tasks() -> None:
    """Cooperative cancel — best-effort wait for each in-flight job to finish."""
    _stop_event().set()
    if not _TASKS:
        _TASK_LOCKS.clear()  # R5/BE-DATA-003: drop stale locks bound to the old loop
        return
    pending = [t for t in _TASKS.values() if not t.done()]
    if not pending:
        _TASK_LOCKS.clear()
        _TASKS.clear()
        return
    try:
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=10.0)
    except asyncio.TimeoutError:
        for t in pending:
            if not t.done():
                t.cancel()
        # Consume the CancelledError from each cancelled task so CPython doesn't
        # log "Task was destroyed but it is pending!" and tasks transition to done().
        await asyncio.gather(*[t for t in pending if not t.done()], return_exceptions=True)
    finally:
        # R5/BE-DATA-003: clear module-level state so a later start_periodic_tasks()
        # (e.g. after uvicorn --reload) always creates fresh asyncio.Lock and Task
        # objects bound to the NEW event loop. A stale Lock from the old loop raises
        # RuntimeError on its first acquire() under CPython 3.10+.
        _TASK_LOCKS.clear()
        _TASKS.clear()


def is_running() -> bool:
    return any(not t.done() for t in _TASKS.values())


def snapshot() -> Dict[str, Dict[str, object]]:
    """Read-only state for /api/health or an admin debug view."""
    out: Dict[str, Dict[str, object]] = {}
    for task_id, env_flag, module_path, _ in _TASK_SPECS:
        last = _LAST_RUN.get(task_id)
        out[task_id] = {
            "env_flag": env_flag,
            "enabled": _flag_enabled(task_id, env_flag),
            "running": bool(_TASKS.get(task_id) and not _TASKS[task_id].done()),
            "interval_seconds": _interval_seconds(task_id),
            "last_run_at": last.isoformat() if last else None,
            "last_error": _LAST_ERROR.get(task_id),
            "module_available": task_id not in _DISABLED_BY_IMPORT,
            "module": module_path,
        }
    return out


__all__ = [
    "start_periodic_tasks",
    "stop_periodic_tasks",
    "is_running",
    "snapshot",
]

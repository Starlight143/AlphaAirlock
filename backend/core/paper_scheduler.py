"""Paper-trade automatic tick (P6-M12).

Wraps :pyfunc:`backend.core.paper_trader.run_paper_trade` so the periodic
task runner can keep every APPROVED / PAPER_TRADE / SMALL_CAPITAL / LIVE
strategy's paper run fresh on a cadence.

Per CLAUDE.md cost-guard rule the *driver* (periodic_tasks.py) gates this
behind ``ALPHA_PAPER_TICK_ENABLED``; this module just exposes the work
function. We additionally rate-limit per strategy via the
``PAPER_TRADE_MIN_REFRESH_SECONDS`` env knob so an aggressive 30 s tick
doesn't re-simulate the same strategy more than once per minute by default
(strategies don't change that often).
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List

from backend._envloader import env_int
from backend.core.database import AlphaStrategy, session_scope
from backend.core.paper_trader import run_paper_trade

logger = logging.getLogger("alpha.paper.scheduler")

# In-process map of {strategy_id: monotonic_ts_of_last_run}. Cheaper than
# round-tripping to disk every tick; lost on process restart, which is fine
# (worst case is one extra simulation per restart per strategy).
_LAST_RUN: Dict[int, float] = {}
# P31-D6: GC dead entries (strategies that haven't been ticked in 7 days
# i.e. deleted/graveyarded). Mirrors the agent_dialogue GC pattern.
_LAST_RUN_GC_MAX_AGE_SEC = 7 * 24 * 60 * 60


def _gc_last_run() -> int:
    """Drop _LAST_RUN entries older than _LAST_RUN_GC_MAX_AGE_SEC.

    Safe to call from anywhere; opportunistically pruned inside
    tick_all_active. Returns the count of entries dropped.
    """
    cutoff = time.monotonic() - _LAST_RUN_GC_MAX_AGE_SEC
    stale = [sid for sid, ts in _LAST_RUN.items() if ts < cutoff]
    for sid in stale:
        _LAST_RUN.pop(sid, None)
    return len(stale)

# Stages eligible for automatic refresh — pre-approval strategies don't have
# a stable formula_code yet, so simulating them would burn CPU on noise.
_ELIGIBLE_STATUSES = frozenset({"APPROVED", "PAPER_TRADE", "SMALL_CAPITAL", "LIVE"})


def _min_refresh_seconds() -> int:
    return env_int("PAPER_TRADE_MIN_REFRESH_SECONDS", 60, minimum=10, maximum=86400)


def _window_days() -> int:
    return env_int("PAPER_TRADE_WINDOW_DAYS", 30, minimum=1, maximum=180)


def _eligible_ids() -> List[int]:
    with session_scope() as s:
        rows = (
            s.query(AlphaStrategy.id, AlphaStrategy.status)
            .filter(AlphaStrategy.status.in_(list(_ELIGIBLE_STATUSES)))
            .all()
        )
    return [int(r[0]) for r in rows]


def tick_all_active() -> dict:
    """Run paper-trade once for every eligible strategy whose last refresh is
    older than ``PAPER_TRADE_MIN_REFRESH_SECONDS``. Returns a stats dict for
    logging — never raises.
    """
    _gc_last_run()
    ids = _eligible_ids()
    if not ids:
        return {"considered": 0, "ran": 0, "skipped": 0, "errors": 0}

    cooldown = _min_refresh_seconds()
    window = _window_days()
    now = time.monotonic()
    stats = {"considered": len(ids), "ran": 0, "skipped": 0, "errors": 0}

    for sid in ids:
        last = _LAST_RUN.get(sid, 0.0)
        if now - last < cooldown:
            stats["skipped"] += 1
            continue
        # P11-B-11: re-check status right before running. _eligible_ids was a
        # snapshot at the top of the tick; a paused/graveyarded strategy might
        # have flipped between then and now.
        try:
            with session_scope() as s:
                current_status = (
                    s.query(AlphaStrategy.status)
                    .filter(AlphaStrategy.id == int(sid))
                    .scalar()
                )
            if current_status not in _ELIGIBLE_STATUSES:
                logger.info(
                    "paper tick skip strategy %s: status flipped to %s mid-tick",
                    sid, current_status,
                )
                stats["skipped"] += 1
                continue
        except Exception:  # noqa: BLE001
            logger.exception("paper tick status re-check failed for %s; running anyway", sid)
        try:
            _LAST_RUN[sid] = time.monotonic()  # Record attempt time before running so any
            # exception path still enforces the cooldown on the next tick.
            run_paper_trade(sid, window_days=window)
            stats["ran"] += 1
        except LookupError as exc:
            # Strategy disappeared / no formula_code — log + skip.
            logger.debug("paper tick skip strategy %s: %s", sid, exc)
            stats["skipped"] += 1
        except Exception:  # noqa: BLE001
            logger.exception("paper tick crashed for strategy %s", sid)
            stats["errors"] += 1
    return stats


def record_last_run(strategy_id: int) -> None:
    """Mark *strategy_id* as having been run right now.

    Call this from any code path that executes run_paper_trade outside of
    tick_all_active (e.g. the manual /api/paper-trade/run endpoint) so that
    the periodic ticker respects the PAPER_TRADE_MIN_REFRESH_SECONDS cooldown
    and does not immediately re-simulate the same strategy on the next tick.
    """
    _LAST_RUN[int(strategy_id)] = time.monotonic()


__all__ = ["tick_all_active", "record_last_run"]

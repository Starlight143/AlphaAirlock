"""Mission-panel snapshot aggregator (P7-02 — /mission-panel).

The web mirror of the Telegram 6h-pulse report. Provides one fan-out
``snapshot()`` call returning everything the page needs: incidents, daemon
health, recent dispatches, daily ticker, system pulse preview, and the last
N Telegram report rows.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.core.database import (
    AlphaStrategy,
    IngestEvent,
    KnowledgeNode,
    StageTransition,
    TelegramReportLog,
)

logger = logging.getLogger("alpha.mission_panel")


def _as_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _safe_paper_runs() -> List[Dict[str, Any]]:
    # P11-B-17: JOIN-style filter — only emit unhealthy alerts for strategies the
    # DB still considers active (PAPER_TRADE / SMALL_CAPITAL / LIVE). Without
    # this gate, the mission panel keeps shouting about strategies the operator
    # already paused / graveyarded.
    try:
        from backend.core import paper_trader
        from backend.core.database import session_scope
        runs = paper_trader.list_all() if hasattr(paper_trader, "list_all") else []
        if not runs:
            return []
        try:
            with session_scope() as s:
                active_ids = set(
                    int(r[0]) for r in (
                        s.query(AlphaStrategy.id)
                        .filter(AlphaStrategy.status.in_(("PAPER_TRADE", "SMALL_CAPITAL", "LIVE")))
                        .all()
                    )
                )
        except Exception:  # noqa: BLE001
            logger.exception("mission_panel._safe_paper_runs: active_ids query failed")
            active_ids = set()
        out: List[Dict[str, Any]] = []
        for r in (runs or []):
            try:
                sid_int = int(r.get("strategy_id") or 0)
            except (TypeError, ValueError):
                continue
            if sid_int not in active_ids:
                continue
            if r.get("is_healthy") is False:
                out.append({
                    "strategy_id": sid_int,
                    "reason": "unhealthy paper run",
                    "notes": r.get("health_notes", []),
                })
        return out
    except (ImportError, AttributeError):
        return []


def _daemon_snapshot() -> List[Dict[str, Any]]:
    """Snapshot of all 8 periodic tasks with on_time/lagging/overdue status."""
    try:
        from backend.core import periodic_tasks
        snap = periodic_tasks.snapshot() if hasattr(periodic_tasks, "snapshot") else {}
    except (ImportError, AttributeError):
        return []
    now = datetime.now(timezone.utc)
    out: List[Dict[str, Any]] = []
    for task_id, st in snap.items():
        last_raw = st.get("last_run_at")
        last_dt: Optional[datetime] = None
        if last_raw:
            try:
                last_dt = datetime.fromisoformat(str(last_raw).replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
            except Exception:  # noqa: BLE001
                last_dt = None
        elapsed = (now - last_dt).total_seconds() if last_dt else None
        interval = float(st.get("interval_seconds") or 0)
        if elapsed is None:
            schedule_status = "never_ran" if st.get("enabled") else "disabled"
        elif interval > 0 and elapsed > interval * 2:
            schedule_status = "overdue"
        elif interval > 0 and elapsed > interval * 1.2:
            schedule_status = "lagging"
        else:
            schedule_status = "on_time"
        out.append({
            "task_id": task_id,
            **st,
            "seconds_since_run": elapsed,
            "schedule_status": schedule_status,
        })
    return out


# P15/D-M19 — module-level TTL cache for the paper-PnL aggregate. _ticker_window
# walks every PAPER_TRADE strategy, hitting disk + JSON-parsing for each one.
# For a fleet of hundreds of strategies this is the dominant cost of the
# /mission-panel snapshot, which is polled every few seconds by the FE. 30s
# cache cuts re-fetches >95% without making operators wait for fresh numbers.
_TICKER_CACHE: Dict[Tuple[str, str], Tuple[float, Any]] = {}
_TICKER_CACHE_LOCK = threading.Lock()
_TICKER_CACHE_TTL_SEC = 30.0
_TICKER_CACHE_MAX_ENTRIES = 32


def _ticker_window_cached(session: Session, start: datetime, end: datetime) -> Dict[str, Any]:
    """Cached wrapper around _ticker_window (30s TTL, bounded entries)."""
    # P15/D-M19-fix — quantize the cache key to the TTL bucket. For the 'today'
    # window `end` is datetime.now() with microsecond precision, so a key that
    # carries `end.isoformat()` changes every poll and the 30s cache below NEVER
    # hits for the hot path. Bucketing by floor(wall_clock / TTL) supplies the
    # time dimension as a value that is stable within each TTL window while never
    # serving data older than one TTL. `start` (midnight cutoff) stays in the key
    # so the today/yesterday windows remain distinct; the microsecond `end` is
    # intentionally dropped from the key — it is the source of the cache miss and
    # the bucket already bounds staleness.
    bucket = int(time.time() // _TICKER_CACHE_TTL_SEC)
    cache_key = (start.isoformat(), bucket)
    now = time.time()
    with _TICKER_CACHE_LOCK:
        cached = _TICKER_CACHE.get(cache_key)
        if cached and (now - cached[0]) < _TICKER_CACHE_TTL_SEC:
            return cached[1]
        # Heavy path is executed under the lock so that concurrent threads
        # that all miss the cache at the same TTL boundary do not all call
        # _ticker_window in parallel (thundering herd). _ticker_window is
        # I/O-bound and its worst-case duration is bounded by the 30s TTL,
        # so holding the lock here is acceptable. Subsequent waiters that
        # were blocked on the lock will find a fresh cache entry and return
        # early without re-running the expensive scan.
        payload = _ticker_window(session, start, end)  # heavy path
        # FIFO eviction at the cap so the cache can't grow unbounded as
        # operators scroll through historical windows.
        if len(_TICKER_CACHE) >= _TICKER_CACHE_MAX_ENTRIES:
            _TICKER_CACHE.pop(next(iter(_TICKER_CACHE)))
        _TICKER_CACHE[cache_key] = (now, payload)
    return payload


def _ticker_window(session: Session, start: datetime, end: datetime) -> Dict[str, Any]:
    """Daily-ticker stats for the [start, end) window.

    D-M16 — replace the prior `updated_at + status==INTAKE` heuristic (which
    miscounts strategies that moved past INTAKE later in the same window) with
    a count of distinct strategy_ids whose FIRST `StageTransition` row falls
    within the window. Each strategy contributes at most once per day.

    D-M17 — populate `paper_pnl_pct` from the sum of paper-trade equity_curve
    last/first delta across all PAPER_TRADE strategies that produced a run
    inside the window. Defensive: defaults to 0.0 (not None) so downstream
    arithmetic never coalesces to NaN.
    """
    # New strategies: distinct strategy_ids whose earliest StageTransition is
    # in the window. Subquery selects (sid, MIN(transitioned_at)).
    first_seen_subq = (
        session.query(
            StageTransition.strategy_id.label("sid"),
            func.min(StageTransition.transitioned_at).label("first_at"),
        )
        .group_by(StageTransition.strategy_id)
        .subquery()
    )
    new_strats = (
        session.query(func.count(first_seen_subq.c.sid))
        .filter(first_seen_subq.c.first_at >= start)
        .filter(first_seen_subq.c.first_at < end)
        .scalar()
    ) or 0

    ic_ingested = (
        session.query(func.count(IngestEvent.id))
        .filter(IngestEvent.fetched_at >= start)
        .filter(IngestEvent.fetched_at < end)
        .filter(IngestEvent.status == "ok")
        .scalar()
    ) or 0

    # D-M17 — paper PnL. Walk PAPER_TRADE strategies and sum equity_curve
    # deltas for runs whose run_at falls inside the window. Each run
    # contributes (last_equity / first_equity - 1.0). Mean across runs.
    paper_pnl_pct: float = 0.0
    try:
        from backend.core import paper_trader as _pt
        paper_strats = (
            session.query(AlphaStrategy.id)
            .filter(AlphaStrategy.status == "PAPER_TRADE")
            .all()
        )
        deltas: List[float] = []
        for (sid,) in paper_strats:
            try:
                latest = _pt.latest_for(int(sid)) or {}
            except Exception:  # noqa: BLE001
                continue
            if not latest:
                continue
            run_at_raw = str(latest.get("run_at") or "").strip()
            if not run_at_raw:
                continue
            try:
                run_at_dt = datetime.strptime(run_at_raw, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
            if not (start <= run_at_dt < end):
                continue
            curve = latest.get("equity_curve") or []
            if not curve or len(curve) < 2:
                continue
            try:
                first_eq = float(curve[0].get("equity", 1.0) or 1.0)
                last_eq = float(curve[-1].get("equity", first_eq) or first_eq)
                if first_eq > 1e-14:
                    deltas.append((last_eq / first_eq) - 1.0)
            except (TypeError, ValueError, AttributeError):
                continue
        if deltas:
            paper_pnl_pct = round(sum(deltas) / len(deltas) * 100.0, 4)
    except Exception:  # noqa: BLE001
        logger.exception("mission_panel._ticker_window paper_pnl_pct compute failed (non-fatal)")
        paper_pnl_pct = 0.0

    return {
        "new_strategies": int(new_strats),
        "ic_ingested": int(ic_ingested),
        "paper_pnl_pct": float(paper_pnl_pct),
    }


def snapshot(session: Session) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_yesterday = cutoff_today - timedelta(days=1)

    # Incidents
    ingest_fail_24h = (
        session.query(IngestEvent)
        .filter(IngestEvent.fetched_at >= cutoff_24h)
        .filter(IngestEvent.status == "fail")
        .order_by(IngestEvent.fetched_at.desc())
        .limit(20)
        .all()
    )
    daemons = _daemon_snapshot()
    daemon_errors = [
        {"task_id": d["task_id"], "last_error": d.get("last_error"), "last_run_at": d.get("last_run_at")}
        for d in daemons if d.get("last_error")
    ]
    paper_unhealthy = _safe_paper_runs()

    # Recent transitions — proxy via AlphaStrategy.updated_at.
    recent_trans = (
        session.query(AlphaStrategy)
        .filter(AlphaStrategy.updated_at >= cutoff_24h)
        .order_by(AlphaStrategy.updated_at.desc())
        .limit(25)
        .all()
    )
    transitions_out = []
    for s in recent_trans:
        transitions_out.append({
            "strategy_id": int(s.id),
            "name": s.name,
            "status": s.status,
            "updated_at": _as_utc(s.updated_at).isoformat() if s.updated_at else None,
        })

    # System pulse preview (the markdown that the next 6h report would send).
    try:
        from backend.core.telegram_reports import render_six_hour_body
        pulse_md = render_six_hour_body(session, now)
    except Exception:  # noqa: BLE001
        logger.exception("pulse preview render failed")
        pulse_md = "_(pulse preview unavailable)_"

    try:
        from backend.core.telegram_reports import (
            _within_cooldown,
            RPT_SIX_HOUR,
        )
        cooldown_blocking: bool = _within_cooldown(session, RPT_SIX_HOUR)
    except Exception:  # noqa: BLE001
        cooldown_blocking = False

    try:
        from backend.core.telegram_notifier import is_enabled as tg_is_enabled
        tg_configured = tg_is_enabled()
    except Exception:  # noqa: BLE001
        tg_configured = False

    # Last 10 Telegram reports.
    last_reports = (
        session.query(TelegramReportLog)
        .order_by(TelegramReportLog.sent_at.desc())
        .limit(10)
        .all()
    )

    return {
        "generated_at": now.isoformat(),
        "window_hours": 6,
        "incidents": {
            "ingest_failures_24h": len(ingest_fail_24h),
            "ingest_failures_recent": [
                {
                    "id": int(e.id),
                    "source_id": int(e.source_id) if e.source_id else None,
                    "fetched_at": _as_utc(e.fetched_at).isoformat() if e.fetched_at else None,
                    "error_msg": e.error_msg,
                }
                for e in ingest_fail_24h
            ],
            "daemon_errors": daemon_errors,
            "unhealthy_paper_runs": paper_unhealthy,
        },
        "daemons": daemons,
        "dispatches": {"recent_transitions": transitions_out},
        "daily_ticker": {
            "today": _ticker_window_cached(session, cutoff_today, now),
            "yesterday": _ticker_window_cached(session, cutoff_yesterday, cutoff_today),
        },
        "pulse_preview": {
            "would_send_at": (cutoff_today + timedelta(hours=6)).isoformat(),
            "cooldown_blocking": cooldown_blocking,
            "telegram_configured": tg_configured,
            "rendered_markdown": pulse_md,
        },
        "last_telegram_reports": [r.to_dict() for r in last_reports],
    }


def incidents_paged(session: Session, *, hours: int = 24, limit: int = 50) -> Dict[str, Any]:
    hours = max(1, min(int(hours), 24 * 30))
    limit = max(1, min(int(limit), 200))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    items: List[Dict[str, Any]] = []
    for e in (
        session.query(IngestEvent)
        .filter(IngestEvent.fetched_at >= since)
        .filter(IngestEvent.status == "fail")
        .order_by(IngestEvent.fetched_at.desc())
        .limit(limit)
        .all()
    ):
        items.append({
            "kind": "ingest_fail",
            "at": _as_utc(e.fetched_at).isoformat() if e.fetched_at else None,
            "severity": "warn",
            "summary": f"Source #{e.source_id}: {e.error_msg or 'unknown'}",
            "ref": {"source_id": e.source_id},
        })
    for r in (
        session.query(TelegramReportLog)
        .filter(TelegramReportLog.sent_at >= since)
        .filter(TelegramReportLog.success.is_(False))
        .filter(TelegramReportLog.summary != "_claiming")
        .order_by(TelegramReportLog.sent_at.desc())
        .limit(limit)
        .all()
    ):
        items.append({
            "kind": "telegram_fail",
            "at": _as_utc(r.sent_at).isoformat() if r.sent_at else None,
            "severity": "warn",
            "summary": f"Telegram report '{r.report_type}' failed",
            "ref": {"report_id": r.id},
        })
    for u in _safe_paper_runs():
        items.append({
            "kind": "paper_unhealthy",
            "at": datetime.now(timezone.utc).isoformat(),
            "severity": "error",
            "summary": f"Strategy {u.get('strategy_id')}: {u.get('reason')}",
            "ref": {"strategy_id": u.get("strategy_id")},
        })
    items.sort(key=lambda x: x.get("at") or "", reverse=True)
    return {
        "hours": hours,
        "items": items[:limit],
        "total": len(items),
    }


__all__ = ["snapshot", "incidents_paged"]

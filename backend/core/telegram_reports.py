"""Telegram automatic reports — Conor 6h / Morning Briefing / Paper Trade Health (P6-B6).

What this is
------------
The reference YouTube demo features three Telegram message types pushed by a
"Conor Supervisor" persona:

1. **Six-hour pulse** — every 6 hours, a compact stats snapshot of ingest,
   pipeline outcomes, and active incidents.
2. **Morning briefing** — once per day at a configurable UTC hour, a longer
   summary of the prior 24h including top-IC new nodes and paper-trade health.
3. **Paper-trade health** — hourly check of every active paper run, with an
   alert if any run is unhealthy.

The existing ``telegram_notifier.py`` already owns outbound mechanics
(``send_markdown``, rate limit, ``TELEGRAM_ENABLED`` gate). This module adds
report **templates** plus **idempotency** via the new ``telegram_report_log``
table.

Idempotency design
------------------
Process restarts must not resend a report that already went out earlier in the
window. Every send writes a ``TelegramReportLog`` row; before sending, we check
the last successful row for that report type. If it was sent within the report's
cooldown window, we skip silently.

Hour gating
-----------
``send_morning_briefing`` and ``send_paper_trade_health_report`` honour a UTC
hour env knob so operators can pin "9am local" without dealing with timezone
conversion in code.

Default behaviour
-----------------
* All three functions default to OFF via their own env flag. Each also requires
  ``TELEGRAM_ENABLED=1`` plus ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID``.
* If any check fails, the function returns False without raising.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend._envloader import env_bool, env_int
from backend.core.database import (
    AlphaStrategy,
    IngestEvent,
    KnowledgeNode,
    TelegramReportLog,
    session_scope,
)
from backend.core.telegram_notifier import is_enabled as notifier_enabled, send_markdown

logger = logging.getLogger("alpha.telegram.reports")

# Report-type identifiers (kept short for log readability).
RPT_SIX_HOUR = "six_hour"
RPT_MORNING = "morning"
RPT_PAPER_HEALTH = "paper_health"

# Minimum wait between same-type sends; the periodic loop's own cadence is a
# separate matter — these guard against accidental re-fires (process restart,
# manual /api/telegram/test trigger, etc.).
_COOLDOWN_SECONDS = {
    RPT_SIX_HOUR: 5 * 60 * 60,           # 5h (loop ticks every 6h; tolerate jitter)
    RPT_MORNING: 22 * 60 * 60,           # 22h (loop ticks every 24h)
    RPT_PAPER_HEALTH: 55 * 60,           # 55m (loop ticks every 60m)
}


def _last_sent(session: Session, report_type: str) -> Optional[TelegramReportLog]:
    return (
        session.query(TelegramReportLog)
        .filter(TelegramReportLog.report_type == report_type)
        .filter(TelegramReportLog.success == True)  # noqa: E712
        .order_by(TelegramReportLog.sent_at.desc())
        .first()
    )


def _within_cooldown(session: Session, report_type: str) -> bool:
    cooldown = _COOLDOWN_SECONDS.get(report_type, 60 * 60)
    last = _last_sent(session, report_type)
    if last is None or last.sent_at is None:
        return False
    last_ts = last.sent_at
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last_ts).total_seconds() < cooldown


def _claim_window(session: Session, report_type: str) -> bool:
    """Atomically claim the current cooldown window so only ONE process sends.

    Returns True if THIS caller won the claim and may send; False if another
    process already holds a successful send inside the cooldown window. Uses a
    SELECT ... FOR UPDATE row lock on the latest successful row so concurrent
    workers serialize on the same row before re-checking the cooldown. On
    SQLite (which ignores FOR UPDATE) the with_for_update() is a harmless no-op;
    SQLite deployments are single-process so the in-process path already serializes.

    Race-safe design: when no prior row exists (last is None) or the prior row is
    outside the cooldown, we INSERT a sentinel row (success=False) and flush
    immediately BEFORE returning True. The flush makes the sentinel visible to any
    concurrent transaction that also holds a FOR UPDATE on the same table, causing
    the second worker's query to see our sentinel and wait / re-evaluate cooldown.
    After the caller sends and calls _log_send(..., success=True), the real row
    supersedes the sentinel. If the send fails, the sentinel row with success=False
    is committed but does not block future sends (cooldown only gates on success=True
    rows, matching _within_cooldown behaviour).
    """
    cooldown = _COOLDOWN_SECONDS.get(report_type, 60 * 60)
    last = (
        session.query(TelegramReportLog)
        .filter(TelegramReportLog.report_type == report_type)
        .filter(TelegramReportLog.success == True)  # noqa: E712
        .order_by(TelegramReportLog.sent_at.desc())
        .with_for_update()
        .first()
    )
    if last is not None and last.sent_at is not None:
        last_ts = last.sent_at
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - last_ts).total_seconds() < cooldown:
            return False
    # Before inserting our own sentinel, check whether a concurrent caller has
    # already inserted one (within the last 5 minutes). This mirrors the guard
    # in _claim_window_skip_cooldown and closes the TOCTOU gap that exists when
    # last=None (no success=True rows to lock): two concurrent callers would
    # both see last=None, both pass the cooldown check above, and without this
    # guard both would insert a sentinel and return True, causing duplicate sends.
    existing_claim = (
        session.query(TelegramReportLog)
        .filter(TelegramReportLog.report_type == report_type)
        .filter(TelegramReportLog.success == False)  # noqa: E712
        .filter(TelegramReportLog.summary == "_claiming")
        .filter(TelegramReportLog.sent_at >= datetime.now(timezone.utc) - timedelta(minutes=5))
        .with_for_update()
        .first()
    )
    if existing_claim is not None:
        return False
    # Insert a sentinel row before returning True so that a concurrent worker
    # that also sees last=None (or an expired row) will observe this row on its
    # own FOR UPDATE query and skip sending. success=False means it will not
    # count as a cooldown gate itself; the real success row written by _log_send
    # after a successful send is what future calls gate against.
    session.add(
        TelegramReportLog(
            report_type=report_type,
            success=False,
            summary="_claiming",
        )
    )
    session.flush()
    return True


def _claim_window_skip_cooldown(session: Session, report_type: str) -> bool:
    """Like _claim_window but WITHOUT the time-based cooldown check.

    Used by the force=True path to prevent duplicate sends when two concurrent
    HTTP requests both trigger a force fire for the same report_type. Inserts a
    sentinel row (success=False) and flushes before returning True so that a
    second concurrent caller will observe the sentinel and return False.
    Returns False if a sentinel row with summary='_claiming' already exists for
    this report_type (meaning another process has already claimed this fire).
    """
    existing_claim = (
        session.query(TelegramReportLog)
        .filter(TelegramReportLog.report_type == report_type)
        .filter(TelegramReportLog.success == False)  # noqa: E712
        .filter(TelegramReportLog.summary == "_claiming")
        .filter(TelegramReportLog.sent_at >= datetime.now(timezone.utc) - timedelta(minutes=5))
        .with_for_update()
        .first()
    )
    if existing_claim is not None:
        return False
    session.add(
        TelegramReportLog(
            report_type=report_type,
            success=False,
            summary="_claiming",
        )
    )
    session.flush()
    return True


def _log_send(session: Session, report_type: str, success: bool, summary: str) -> None:
    session.add(
        TelegramReportLog(
            report_type=report_type,
            success=bool(success),
            summary=summary[:2000] if summary else None,
        )
    )
    session.flush()


def _percentage_change(metrics: dict, key: str = "max_drawdown") -> str:
    raw = metrics.get(key)
    if raw is None:
        return "—"
    try:
        return f"{float(raw) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


# ---------------------------------------------------------------------------
# Six-hour pulse
# ---------------------------------------------------------------------------


def render_six_hour_body(session, now=None) -> str:
    """Pure renderer for the 6h pulse markdown.

    Extracted so the /mission-panel snapshot endpoint can preview what the
    next auto-fire would send WITHOUT actually sending or logging.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=6)
    ingest_ok = (
        session.query(func.count(IngestEvent.id))
        .filter(IngestEvent.fetched_at >= cutoff)
        .filter(IngestEvent.status == "ok")
        .scalar() or 0
    )
    ingest_fail = (
        session.query(func.count(IngestEvent.id))
        .filter(IngestEvent.fetched_at >= cutoff)
        .filter(IngestEvent.status == "fail")
        .scalar() or 0
    )
    new_kb = (
        session.query(func.count(KnowledgeNode.id))
        .filter(KnowledgeNode.created_at >= cutoff)
        .scalar() or 0
    )
    status_rows = (
        session.query(AlphaStrategy.status, func.count(AlphaStrategy.id))
        .filter(AlphaStrategy.updated_at >= cutoff)
        .group_by(AlphaStrategy.status)
        .all()
    )
    status_summary = ", ".join(f"`{st}`: {cnt}" for st, cnt in status_rows) or "_no transitions_"
    return (
        "*🕕 6h Pulse*\n"
        f"Ingest: `{ingest_ok}` ok / `{ingest_fail}` fail\n"
        f"New KB nodes: `{new_kb}`\n"
        f"Strategy transitions:\n{status_summary}"
    )


def send_six_hour_report(*, force: bool = False) -> bool:
    """Compact 6h activity snapshot. Gated by ``TG_SIX_HOUR_ENABLED``.

    Pass ``force=True`` to bypass the cooldown check (used by
    ``/api/mission-panel/fire-six-hour-now``). Cooldown bypass still writes
    a TelegramReportLog row so the audit trail is intact.
    """
    if not env_bool("TG_SIX_HOUR_ENABLED", False):
        return False
    if not notifier_enabled():
        return False

    with session_scope() as s:
        if not force and not _claim_window(s, RPT_SIX_HOUR):
            logger.debug("Skipping six_hour: within cooldown (claim lost)")
            return False
        if force and not _claim_window_skip_cooldown(s, RPT_SIX_HOUR):
            # A concurrent force send already inserted a sentinel for this
            # report type; skip to avoid duplicate Telegram messages.
            logger.debug("Skipping six_hour force: concurrent force send detected")
            return False
        body = render_six_hour_body(s)
        ok = send_markdown(body, disable_notification=True)
        _log_send(s, RPT_SIX_HOUR, ok, body)
        return ok


# ---------------------------------------------------------------------------
# Morning briefing
# ---------------------------------------------------------------------------


def _morning_hour_utc() -> int:
    return env_int("TG_MORNING_HOUR_UTC", 1, minimum=0, maximum=23)


def send_morning_briefing(*, force: bool = False) -> bool:
    """Daily morning summary. Gated by ``TG_MORNING_ENABLED`` AND only fires
    once a day on or after the configured UTC hour window (default 01:00 UTC =
    09:00 GMT+8).

    P11-B-05: hour gate is `>= target_hour` (not `==`) so a scheduler that
    happens to be down at the briefing hour and recovers later in the day still
    delivers the briefing. The ``_within_cooldown`` check below ensures only one
    fire per 24h.

    Pass ``force=True`` to bypass both the hour gate and the cooldown check
    (used by manual operator triggers). Cooldown bypass still writes a
    TelegramReportLog row so the audit trail is intact.
    """
    if not env_bool("TG_MORNING_ENABLED", False):
        return False
    if not notifier_enabled():
        return False
    now = datetime.now(timezone.utc)
    target_hour = _morning_hour_utc()
    if not force and now.hour < target_hour:
        return False  # too early today

    with session_scope() as s:
        if not force and not _claim_window(s, RPT_MORNING):
            return False
        if force and not _claim_window_skip_cooldown(s, RPT_MORNING):
            # A concurrent force send already inserted a sentinel for this
            # report type; skip to avoid duplicate Telegram messages.
            logger.debug("Skipping morning_briefing force: concurrent force send detected")
            return False

        cutoff = now - timedelta(hours=24)
        new_nodes = (
            s.query(KnowledgeNode)
            .filter(KnowledgeNode.created_at >= cutoff)
            .order_by(KnowledgeNode.ic_score.desc())
            .limit(5)
            .all()
        )
        approved = (
            s.query(AlphaStrategy)
            .filter(AlphaStrategy.status.in_(["APPROVED", "PAPER_TRADE", "SMALL_CAPITAL", "LIVE"]))
            .filter(AlphaStrategy.updated_at >= cutoff)
            .order_by(AlphaStrategy.updated_at.desc())
            .limit(5)
            .all()
        )
        rejected_count = (
            s.query(func.count(AlphaStrategy.id))
            .filter(AlphaStrategy.status == "REJECTED")
            .filter(AlphaStrategy.updated_at >= cutoff)
            .scalar() or 0
        )

        top_node_lines = [
            f"  • `K#{n.id}` {n.title[:50]} (IC `{float(n.ic_score or 0.0):.2f}`)"
            for n in new_nodes
        ] or ["  • _no new high-IC nodes_"]
        approved_lines = [
            f"  • `S#{st.id}` {st.name[:50]} → `{st.status}`"
            for st in approved
        ] or ["  • _no approvals in last 24h_"]

        body = (
            "*🌅 Morning Briefing*\n"
            f"_Window: last 24h ending {now.strftime('%Y-%m-%d %H:%M UTC')}_\n\n"
            "*Top new KB nodes:*\n" + "\n".join(top_node_lines) + "\n\n"
            "*Recent approvals / promotions:*\n" + "\n".join(approved_lines) + "\n\n"
            f"Rejected (last 24h): `{rejected_count}`"
        )
        ok = send_markdown(body, disable_notification=False)
        _log_send(s, RPT_MORNING, ok, body)
        return ok


# ---------------------------------------------------------------------------
# Paper-trade health
# ---------------------------------------------------------------------------


def send_paper_trade_health_report() -> bool:
    """Hourly paper-trade health digest. Gated by ``TG_PAPER_HEALTH_ENABLED``."""
    if not env_bool("TG_PAPER_HEALTH_ENABLED", False):
        return False
    if not notifier_enabled():
        return False

    with session_scope() as s:
        try:
            from backend.core import paper_trader  # noqa: WPS433 — lazy
            try:
                runs = paper_trader.list_all() or []
            except AttributeError:
                # Older paper_trader didn't expose list_all.
                runs = []
        except ImportError:
            runs = []

        # P11-B-19: filter by DB AlphaStrategy.status to avoid stale paper-trader
        # rows lying about being active. JOIN-style: query active ids then keep
        # only runs whose strategy_id is in that set.
        active_ids = set(
            int(r[0]) for r in (
                s.query(AlphaStrategy.id)
                .filter(AlphaStrategy.status.in_(("PAPER_TRADE", "SMALL_CAPITAL", "LIVE")))
                .all()
            )
        )
        active_runs = [
            r for r in runs
            if int(r.get("strategy_id") or 0) in active_ids
        ]

        if not active_runs:
            # No active paper runs in this window. We intentionally do NOT
            # send a "0 runs" digest to avoid hourly spam. (No recovery-on-
            # zero-to-nonzero alert is implemented; the next non-empty tick
            # produces the normal digest.)
            # IMPORTANT: check active_runs BEFORE _claim_window to avoid inserting
            # an orphaned sentinel row (success=False, summary='_claiming') that
            # would block the next legitimate send for up to 5 minutes.
            return False

        if not _claim_window(s, RPT_PAPER_HEALTH):
            return False

        unhealthy = [r for r in active_runs if not r.get("is_healthy", True)]
        healthy_count = len(active_runs) - len(unhealthy)
        body_lines = [
            "*📋 Paper-Trade Health*",
            f"Active: `{len(active_runs)}` ({healthy_count} healthy, {len(unhealthy)} unhealthy)",
        ]
        if unhealthy:
            body_lines.append("\n*Unhealthy runs:*")
            for r in unhealthy[:10]:
                sid = r.get("strategy_id") or r.get("id") or "?"
                reason = (r.get("health_reason") or r.get("status") or "n/a")[:60]
                body_lines.append(f"  • `S#{sid}` — {reason}")
        body = "\n".join(body_lines)
        ok = send_markdown(body, disable_notification=len(unhealthy) == 0)
        _log_send(s, RPT_PAPER_HEALTH, ok, body)
        return ok


__all__ = [
    "send_six_hour_report",
    "send_morning_briefing",
    "send_paper_trade_health_report",
    "RPT_SIX_HOUR",
    "RPT_MORNING",
    "RPT_PAPER_HEALTH",
]

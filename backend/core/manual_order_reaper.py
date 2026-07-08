"""Background reaper for pending limit orders on the manual terminal.

Runs from :mod:`backend.core.periodic_tasks` whenever
``TRADING_TERMINAL_ENABLED=1``. Two responsibilities:

1. Cross any ``status='pending' AND order_type='limit'`` order whose price
   has crossed the current mid.
2. On startup, sweep orders that have been pending >7 days and mark them
   ``orphaned`` so they don't accumulate forever.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List

from sqlalchemy.orm import Session

from backend._envloader import env_bool
from backend.core.database import (
    AuditLog,
    ManualFill,
    ManualOrder,
    session_scope,
)
from backend.core.trading_terminal import (
    _recompute_position,
    market_info,
    maker_bps,
)

logger = logging.getLogger("alpha.terminal_reaper")


def is_enabled() -> bool:
    return env_bool("TRADING_TERMINAL_ENABLED", False)


def _as_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def reap_pending_limits(session: Session) -> int:
    """Cross any pending limit order whose limit_price has been reached."""
    if not is_enabled():
        return 0
    rows = (
        session.query(ManualOrder)
        .filter(ManualOrder.status == "pending")
        .filter(ManualOrder.order_type == "limit")
        .order_by(ManualOrder.requested_at.asc(), ManualOrder.id.asc())
        .all()
    )
    filled = 0
    for o in rows:
        try:
            info = market_info(o.symbol)
        except Exception:  # noqa: BLE001
            # P32-D8 / OBS32-4 — log instead of bare continue so transient
            # exchange-feed failures are visible in the reaper's log stream.
            logger.warning(
                "reaper: market_info failed for order=%s symbol=%s; skipping this tick",
                o.order_uid, o.symbol, exc_info=True,
            )
            continue
        # P30-T6 / P32-T3: stale-quote guard — mirrors the check in
        # submit_paper (lines 792, 833) and validate_order (line 337).
        # When the CSV feed is stale, market_info() sets quote_stale=True
        # and returns potentially 60+-second-old or fallback prices; filling
        # a resting GTC limit against those prices is unsound.
        if info.get("quote_stale"):
            logger.warning(
                "reaper: stale quote for %s, skipping limit order=%s",
                o.symbol, o.order_uid,
            )
            continue
        last = float(info["last"])
        # P31-MKT3: maker buy fills only when best ASK <= lp (an aggressor
        # seller crosses our resting bid). Comparing to `last` triggered on
        # every trade print at-or-below lp, producing ~2x the realistic
        # maker-fill rate. Same logic mirrored for sells against best BID.
        bid = float(info.get("bid") or last)
        ask = float(info.get("ask") or last)
        lp = float(o.limit_price or 0.0)
        if lp <= 0:
            continue
        crossable = (
            (o.side == "buy" and ask <= lp)
            or (o.side == "sell" and bid >= lp)
        )
        if not crossable:
            continue
        # P-TXN1: per-row SAVEPOINT so a failure on THIS order (e.g. an
        # error inside _recompute_position or serialization) rolls back only
        # this row's writes — NOT the fills already crossed earlier this tick.
        # Without this, session_scope's single end-of-tick commit means one
        # bad row discards every legitimate fill computed in the same tick.
        try:
            with session.begin_nested():
                fee_bps = maker_bps()
                fee_q = abs(float(o.qty) * lp) * (fee_bps / 10000.0)
                # P31-STATE4: conditional UPDATE — only flip pending->filled if
                # status is STILL pending in DB (a concurrent cancel_order may
                # have committed while we were calling market_info).
                updated = (
                    session.query(ManualOrder)
                    .filter(ManualOrder.id == o.id)
                    .filter(ManualOrder.status == "pending")
                    .update(
                        {"status": "filled", "decided_at": datetime.now(timezone.utc)},
                        synchronize_session=False,
                    )
                )
                if not updated:
                    session.expire(o)
                    continue
                session.refresh(o)
                session.add(ManualFill(
                    order_id=o.id,
                    filled_qty=float(o.qty),
                    filled_price=lp,
                    fee_quote=fee_q,
                    fee_bps=fee_bps,
                    is_maker=True,
                    slippage_bps=0.0,
                ))
                session.flush()  # ensure fill row is visible to _recompute_position's SQL query (autoflush=False)
                _recompute_position(session, o.symbol, o.mode)
                session.add(AuditLog(
                    actor="reaper",
                    action="trading_terminal.reap_fill",
                    subject_type="order",
                    subject_id=o.order_uid,
                    payload_json=json.dumps({"last": last, "limit_price": lp}, default=str),
                    response_json=json.dumps(o.to_dict(), default=str),
                    success=True,
                ))
            filled += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "reaper: failed to fill limit order=%s symbol=%s; rolled back this row only",
                o.order_uid, o.symbol,
            )
            session.expire(o)
            continue
    return filled


def reap_pending_stops(session: Session) -> int:
    """P30-T1: Cross any pending stop order whose stop_price has been triggered.

    Trigger semantics:
      - buy stop:  last >= stop_price  (protective stop above market)
      - sell stop: last <= stop_price  (protective stop below market)

    Fills are recorded at the current ``last`` price (market execution after
    trigger) with the configured ``taker_bps`` fee. Stop-limit is intentionally
    NOT supported here.
    """
    if not is_enabled():
        return 0
    from backend.core.trading_terminal import taker_bps as _taker_bps
    rows = (
        session.query(ManualOrder)
        .filter(ManualOrder.status == "pending")
        .filter(ManualOrder.order_type == "stop")
        .order_by(ManualOrder.requested_at.asc(), ManualOrder.id.asc())
        .all()
    )
    filled = 0
    for o in rows:
        try:
            info = market_info(o.symbol)
        except Exception:  # noqa: BLE001
            # P32-D8 / OBS32-4 — log instead of bare continue so transient
            # exchange-feed failures are visible in the reaper's log stream.
            logger.warning(
                "reaper: market_info failed for order=%s symbol=%s; skipping this tick",
                o.order_uid, o.symbol, exc_info=True,
            )
            continue
        # P30-T6 / P32-T3: stale-quote guard — mirrors the check in
        # submit_paper (lines 792, 833) and validate_order (line 337).
        # When the CSV feed is stale, market_info() sets quote_stale=True
        # and returns potentially 60+-second-old or fallback prices; triggering
        # a resting stop order against those prices is unsound.
        if info.get("quote_stale"):
            logger.warning(
                "reaper: stale quote for %s, skipping stop order=%s",
                o.symbol, o.order_uid,
            )
            continue
        last = float(info["last"])
        bid = float(info.get("bid") or last)
        ask = float(info.get("ask") or last)
        sp = float(o.stop_price or 0.0)
        if sp <= 0:
            continue
        triggered = (
            (o.side == "buy" and last >= sp)
            or (o.side == "sell" and last <= sp)
        )
        if not triggered:
            continue
        # P-TXN1: per-row SAVEPOINT so a failure on THIS order (e.g. an
        # error inside _recompute_position or serialization) rolls back only
        # this row's writes — NOT the fills already crossed earlier this tick.
        # Without this, session_scope's single end-of-tick commit means one
        # bad row discards every legitimate fill computed in the same tick.
        try:
            with session.begin_nested():
                # P32-M32-1: on a gap-through, `last` (post-gap touch) is BETTER
                # for the holder than the stop. Filling at `last` gives free
                # liquidity the venue never offered. Fill at the WORST of (stop,
                # current quote): buy stop -> max(sp, ask); sell stop ->
                # min(sp, bid). This models a taker market order placed the
                # moment the stop triggered.
                if o.side == "buy":
                    fill_price = max(sp, ask)
                else:
                    fill_price = min(sp, bid)
                fee_bps = _taker_bps()
                fee_q = abs(float(o.qty) * fill_price) * (fee_bps / 10000.0)
                # P31-STATE4: conditional UPDATE — only flip pending->filled if
                # status is STILL pending in DB (a concurrent cancel_order may
                # have committed while we were calling market_info).
                updated = (
                    session.query(ManualOrder)
                    .filter(ManualOrder.id == o.id)
                    .filter(ManualOrder.status == "pending")
                    .update(
                        {"status": "filled", "decided_at": datetime.now(timezone.utc)},
                        synchronize_session=False,
                    )
                )
                if not updated:
                    session.expire(o)
                    continue
                session.refresh(o)
                # P32-M32-1: slippage_bps records how much WORSE than the stop
                # the fill was (so DD attribution shows the gap cost).
                if sp > 1e-14:
                    slip_bps = abs(fill_price - sp) / sp * 10000.0
                else:
                    slip_bps = 0.0
                session.add(ManualFill(
                    order_id=o.id,
                    filled_qty=float(o.qty),
                    filled_price=fill_price,
                    fee_quote=fee_q,
                    fee_bps=fee_bps,
                    is_maker=False,
                    slippage_bps=slip_bps,
                ))
                session.flush()  # ensure fill row is visible to _recompute_position's SQL query (autoflush=False)
                _recompute_position(session, o.symbol, o.mode)
                session.add(AuditLog(
                    actor="reaper",
                    action="trading_terminal.reap_stop_fill",
                    subject_type="order",
                    subject_id=o.order_uid,
                    payload_json=json.dumps({"last": last, "stop_price": sp, "fill_price": fill_price}, default=str),
                    response_json=json.dumps(o.to_dict(), default=str),
                    success=True,
                ))
            filled += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "reaper: failed to fill stop order=%s symbol=%s; rolled back this row only",
                o.order_uid, o.symbol,
            )
            session.expire(o)
            continue
    return filled


def orphan_stale_orders(session: Session, *, max_age_days: int = 7) -> int:
    """Mark pending orders older than N days as 'orphaned'."""
    if not is_enabled():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(max_age_days))
    rows = (
        session.query(ManualOrder)
        .filter(ManualOrder.status == "pending")
        .filter(ManualOrder.requested_at < cutoff)  # B6-3: push age predicate to DB; ix_manual_orders_requested_at covers this
        .order_by(ManualOrder.requested_at.asc(), ManualOrder.id.asc())
        .all()
    )
    n = 0
    now_utc = datetime.now(timezone.utc)
    for o in rows:
        requested = _as_utc(o.requested_at)
        if requested is None:  # belt-and-suspenders: column is non-nullable but guard _as_utc's None path
            continue
        # P-TXN1: per-row SAVEPOINT so a failure on THIS order (e.g. an
        # error during serialization) rolls back only this row's writes —
        # NOT the orphans already flipped earlier this tick. Without this,
        # session_scope's single end-of-tick commit means one bad row
        # discards every legitimate orphan computed in the same tick.
        try:
            with session.begin_nested():
                # P32-DB32-8: conditional UPDATE — only flip pending->orphaned
                # if status is STILL pending in DB (a concurrent
                # reap_pending_limits / reap_pending_stops / cancel_order may
                # have committed while we were iterating). updated==0 => race
                # lost, skip silently.
                updated = (
                    session.query(ManualOrder)
                    .filter(ManualOrder.id == o.id)
                    .filter(ManualOrder.status == "pending")
                    .update(
                        {"status": "orphaned", "decided_at": now_utc},
                        synchronize_session=False,
                    )
                )
                if not updated:
                    session.expire(o)
                    continue
                session.refresh(o)
                session.add(AuditLog(
                    actor="reaper",
                    action="trading_terminal.orphan",
                    subject_type="order",
                    subject_id=o.order_uid,
                    payload_json=json.dumps({"max_age_days": max_age_days}, default=str),
                    response_json=json.dumps(o.to_dict(), default=str),
                    success=True,
                ))
            n += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "reaper: failed to orphan order=%s; rolled back this row only",
                o.order_uid,
            )
            session.expire(o)
            continue
    return n


def tick() -> int:
    """Called from periodic_tasks. Returns total fills + orphans this tick."""
    if not is_enabled():
        return 0
    total = 0
    try:
        # P-TXN1: each sub-reaper runs in its own session_scope so that a
        # crash in one function (e.g. a bad session.query() call at the top of
        # orphan_stale_orders) rolls back ONLY that sub-reaper's uncommitted
        # SAVEPOINTs, while fills already committed by earlier sub-reapers are
        # durably preserved. The previous single-scope design with an
        # s.commit() at the end of the composed expression provided NO such
        # isolation — the commit was only reachable after all three functions
        # had already returned normally, making it purely redundant with
        # session_scope's own auto-commit.
        with session_scope() as s:
            total += reap_pending_limits(s)
        with session_scope() as s:
            total += reap_pending_stops(s)
        with session_scope() as s:
            total += orphan_stale_orders(s)
    except Exception:  # noqa: BLE001
        logger.exception("manual order reaper tick failed (non-fatal)")
    return total


__all__ = ["tick", "reap_pending_limits", "reap_pending_stops", "orphan_stale_orders", "is_enabled"]

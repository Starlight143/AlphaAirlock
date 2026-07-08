"""FinOps cost ledger — one immutable row per settled LLM call.

The daily / per-strategy budget caps in ``llm_budget`` already COMPUTE each
call's token+cost estimate, but only keep a running daily total. This module
persists the per-call detail (agent, model, char counts, estimated USD, strategy
id) as an append-only ledger so cost can be sliced by strategy / agent / model /
day — the FinOps matrix the budget JSON can't answer ("which agent burns the
most?", "what did strategy #204 cost end-to-end?").

Immutable-ledger discipline (per CLAUDE.md accounting rule): rows are only ever
INSERTed, never UPDATEd. ``est_cost_usd`` is an ESTIMATE on the same flat-pricing
basis as the cap; ``input_chars`` / ``output_chars`` / ``model`` are the source
of truth so a truer cost can be recomputed later from real per-model prices.

Best-effort + never raises into the caller: a ledger write failure must never
sink an LLM call. Default ON; disable with ``LLM_COST_LEDGER_ENABLED=0``.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from backend._envloader import env_bool, env_int

logger = logging.getLogger("alpha.cost_ledger")


def is_enabled() -> bool:
    # Default ON in production, but OFF under pytest unless a test explicitly
    # opts in — so the unit suite's stubbed call_messages() never leaks rows into
    # the live DB (record_call is reached from _client on every successful call).
    default = "PYTEST_CURRENT_TEST" not in os.environ
    return env_bool("LLM_COST_LEDGER_ENABLED", default)


def record_call(
    *,
    agent: str,
    model: str,
    input_chars: int,
    output_chars: int,
    usage: Optional[Dict[str, Any]] = None,
    strategy_id: Optional[int] = None,
) -> None:
    """Append one ledger row for a settled LLM call. Best-effort; never raises.

    ``usage`` — when the provider returned one (OpenRouter with usage.include) —
    carries the REAL billed ``cost`` + ``prompt_tokens`` / ``completion_tokens``.
    That real cost is stored with ``cost_source='openrouter'``. Without it, the
    cost falls back to the flat-pricing char estimate (``cost_source='estimate'``).
    """
    if not is_enabled():
        return
    try:
        from backend.core import llm_budget
        from backend.core.database import LLMCostLedger, session_scope

        ic = max(0, int(input_chars or 0))
        oc = max(0, int(output_chars or 0))
        u = usage or {}
        real_cost = u.get("cost")
        itok = u.get("prompt_tokens")
        otok = u.get("completion_tokens")
        if real_cost is not None:
            try:
                cost, source = float(real_cost), "openrouter"
            except (TypeError, ValueError):
                cost, source = float(llm_budget.estimate_cost_usd(ic, oc)), "estimate"
        else:
            cost, source = float(llm_budget.estimate_cost_usd(ic, oc)), "estimate"
        with session_scope() as s:
            s.add(LLMCostLedger(
                strategy_id=(int(strategy_id) if strategy_id is not None else None),
                agent=(str(agent or "")[:32] or "unknown"),
                model=(str(model or "")[:128] or "<default>"),
                input_chars=ic,
                output_chars=oc,
                input_tokens=(int(itok) if isinstance(itok, (int, float)) else None),
                output_tokens=(int(otok) if isinstance(otok, (int, float)) else None),
                est_cost_usd=cost,
                cost_source=source,
            ))
    except Exception:  # noqa: BLE001 — a cost-ledger write must never break a call
        logger.debug("cost_ledger.record_call failed (non-fatal)", exc_info=True)


def summary(*, days: int = 7, top_strategies: int = 10) -> Dict[str, Any]:
    """Aggregate ledger cost over the trailing ``days`` window: totals, by-agent,
    by-model, today's spend, and the top-N most expensive strategies. Never
    raises — returns a zeroed shape on any error / empty ledger."""
    empty = {"window_days": int(days), "total_cost_usd": 0.0, "total_calls": 0,
             "total_input_chars": 0, "total_output_chars": 0,
             "real_cost_calls": 0, "estimated_calls": 0,
             "by_agent": [], "by_model": [], "top_strategies": [],
             "today_cost_usd": 0.0, "today_calls": 0}
    try:
        from sqlalchemy import func
        from backend.core.database import LLMCostLedger, session_scope

        d = max(1, int(days))
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=d)
        start_today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        out: Dict[str, Any] = {"window_days": d}
        with session_scope() as s:
            total_cost, total_calls, total_in, total_out = (
                s.query(
                    func.coalesce(func.sum(LLMCostLedger.est_cost_usd), 0.0),
                    func.count(LLMCostLedger.id),
                    func.coalesce(func.sum(LLMCostLedger.input_chars), 0),
                    func.coalesce(func.sum(LLMCostLedger.output_chars), 0),
                ).filter(LLMCostLedger.created_at >= since).one()
            )
            out["total_cost_usd"] = round(float(total_cost or 0.0), 4)
            out["total_calls"] = int(total_calls or 0)
            out["total_input_chars"] = int(total_in or 0)
            out["total_output_chars"] = int(total_out or 0)

            # How much of the window's cost is OpenRouter's real billed figure vs
            # the flat-pricing char estimate (trust signal for the USD totals).
            real_calls = (
                s.query(func.count(LLMCostLedger.id))
                .filter(LLMCostLedger.created_at >= since,
                        LLMCostLedger.cost_source == "openrouter").scalar()
            )
            out["real_cost_calls"] = int(real_calls or 0)
            out["estimated_calls"] = max(0, int(out["total_calls"]) - int(real_calls or 0))

            out["by_agent"] = [
                {"agent": a, "cost_usd": round(float(c or 0.0), 4), "calls": int(n or 0)}
                for a, c, n in (
                    s.query(LLMCostLedger.agent,
                            func.coalesce(func.sum(LLMCostLedger.est_cost_usd), 0.0),
                            func.count(LLMCostLedger.id))
                    .filter(LLMCostLedger.created_at >= since)
                    .group_by(LLMCostLedger.agent).all())
            ]
            out["by_model"] = [
                {"model": m, "cost_usd": round(float(c or 0.0), 4), "calls": int(n or 0)}
                for m, c, n in (
                    s.query(LLMCostLedger.model,
                            func.coalesce(func.sum(LLMCostLedger.est_cost_usd), 0.0),
                            func.count(LLMCostLedger.id))
                    .filter(LLMCostLedger.created_at >= since)
                    .group_by(LLMCostLedger.model).all())
            ]
            tcost, tcalls = (
                s.query(func.coalesce(func.sum(LLMCostLedger.est_cost_usd), 0.0),
                        func.count(LLMCostLedger.id))
                .filter(LLMCostLedger.created_at >= start_today).one()
            )
            out["today_cost_usd"] = round(float(tcost or 0.0), 4)
            out["today_calls"] = int(tcalls or 0)

            out["top_strategies"] = [
                {"strategy_id": int(sid), "cost_usd": round(float(c or 0.0), 4),
                 "calls": int(n or 0)}
                for sid, c, n in (
                    s.query(LLMCostLedger.strategy_id,
                            func.coalesce(func.sum(LLMCostLedger.est_cost_usd), 0.0),
                            func.count(LLMCostLedger.id))
                    .filter(LLMCostLedger.created_at >= since,
                            LLMCostLedger.strategy_id.isnot(None))
                    .group_by(LLMCostLedger.strategy_id)
                    .order_by(func.sum(LLMCostLedger.est_cost_usd).desc())
                    .limit(max(1, int(top_strategies))).all())
                if sid is not None
            ]
        return out
    except Exception:  # noqa: BLE001
        logger.exception("cost_ledger.summary failed")
        return empty


def prune_old(retention_days: Optional[int] = None) -> int:
    """Delete ledger rows older than the retention window; returns rows deleted.
    Periodic-task entry — never raises. ``LLM_COST_LEDGER_RETENTION_DAYS`` default
    90: long enough for monthly FinOps review, bounded so the append-only table
    can't grow without limit on a multi-year deployment."""
    try:
        from backend.core.database import LLMCostLedger, session_scope
        days = int(retention_days if retention_days is not None
                   else env_int("LLM_COST_LEDGER_RETENTION_DAYS", 90,
                                minimum=1, maximum=3650))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with session_scope() as s:
            # Materialise ids THEN delete: pysqlite can't interleave DML with an
            # open SELECT cursor. Bounded batch keeps the write-lock window short.
            ids = [r[0] for r in s.query(LLMCostLedger.id)
                   .filter(LLMCostLedger.created_at < cutoff)
                   .limit(50000).all()]
            if not ids:
                return 0
            deleted = (s.query(LLMCostLedger)
                       .filter(LLMCostLedger.id.in_(ids))
                       .delete(synchronize_session=False))
        return int(deleted or 0)
    except Exception:  # noqa: BLE001
        logger.exception("cost_ledger.prune_old failed")
        return 0


__all__ = ["record_call", "summary", "prune_old", "is_enabled"]

"""Alpha flow aggregations (P7-05 — /alpha-flow).

Powers three endpoints registered in :mod:`backend.app.main`:

* ``/api/alpha-flow/sankey`` — node/link aggregate for the whole forest
* ``/api/alpha-flow/strategy/{id}/timeline`` — per-strategy stage history
* ``/api/alpha-flow/dropout-stats`` — REJECTED-vs-advanced per source status

Reuses the ``stage_transitions`` table populated by the foundation phase.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from backend.core.database import AlphaStrategy, StageTransition


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite returns naive datetimes even for timezone=True columns."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

logger = logging.getLogger("alpha.alpha_flow")

# Canonical column order (left-to-right) for the Sankey + dropout chart.
_STAGE_ORDER: List[str] = [
    "INTAKE", "STORY_GEN", "CODE_GEN", "BACKTESTING", "CRITIC_LOOP",
    "APPROVED", "PAPER_TRADE", "SMALL_CAPITAL", "LIVE",
    "REJECTED", "GRAVEYARD", "PAUSED",
]
_TERMINAL_FAIL = {"REJECTED", "GRAVEYARD", "PAUSED"}


def _classify_link_kind(from_stage: Optional[int], to_stage: int, to_status: str) -> str:
    """advance | reject | loop | retreat — drives the frontend colour."""
    if to_status.upper() in _TERMINAL_FAIL:
        return "reject"
    if from_stage is None:
        return "advance"
    if to_stage > from_stage:
        return "advance"
    if to_stage < from_stage:
        return "retreat"
    return "loop"


def _format_dwell(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m"
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    return f"{d}d {h}h"


def sankey(session: Session, *, days: int = 30) -> Dict[str, Any]:
    days = max(1, min(int(days), 365))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        session.query(StageTransition)
        .filter(StageTransition.transitioned_at >= since)
        .filter(StageTransition.actor != "backfill")
        .order_by(StageTransition.strategy_id, StageTransition.transitioned_at)
        .all()
    )

    # Compute dwell time = time spent in the from-stage before this transition.
    # Need prior transition per strategy for each row.
    by_strategy: Dict[int, List[StageTransition]] = defaultdict(list)
    for r in rows:
        by_strategy[int(r.strategy_id)].append(r)

    link_buckets: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(lambda: {
        "value": 0, "dwells": [], "kind": "advance", "self_loops": 0,
    })

    for sid, evts in by_strategy.items():
        for i, ev in enumerate(evts):
            from_status = (ev.from_status or "").upper() or "INTAKE"
            to_status = (ev.to_status or "").upper()
            if not to_status:
                continue
            if from_status == to_status:
                link_buckets[(from_status, to_status)]["self_loops"] += 1
                continue
            link_buckets[(from_status, to_status)]["value"] += 1
            # kind is determined solely by the (from_status, to_status) pair;
            # set it only on the first event so subsequent events never overwrite it.
            if link_buckets[(from_status, to_status)]["value"] == 1:
                kind = _classify_link_kind(ev.from_stage, int(ev.to_stage or 0), to_status)
                link_buckets[(from_status, to_status)]["kind"] = kind
            if i > 0:
                prev = evts[i - 1]
                # Only credit dwell when the prior event's to_status matches
                # this event's from_status. If they differ (operator manual
                # transition, re-promotion, or out-of-order row), the elapsed
                # time spans multiple intermediate stages and must not be
                # attributed to this (from_status, to_status) bucket.
                prev_to = (prev.to_status or "").upper()
                if prev_to == from_status:
                    ta = _as_utc(prev.transitioned_at)
                    tb = _as_utc(ev.transitioned_at)
                    if ta is not None and tb is not None:
                        dwell = (tb - ta).total_seconds()
                        if dwell >= 0:
                            link_buckets[(from_status, to_status)]["dwells"].append(dwell)

    # Build node list with stable column ordering.
    node_set = {s for (s, _t) in link_buckets} | {t for (_s, t) in link_buckets}
    sorted_nodes = sorted(
        node_set,
        key=lambda s: _STAGE_ORDER.index(s) if s in _STAGE_ORDER else 99,
    )
    node_index = {name: i for i, name in enumerate(sorted_nodes)}

    nodes_out = []
    for name in sorted_nodes:
        outgoing = sum(b["value"] for (s, _t), b in link_buckets.items() if s == name)
        incoming = sum(b["value"] for (_s, t), b in link_buckets.items() if t == name)
        self_loops = sum(b["self_loops"] for (s, t), b in link_buckets.items() if s == t == name)
        nodes_out.append({
            "name": name,
            "stage": _STAGE_ORDER.index(name) if name in _STAGE_ORDER else 99,
            "incoming": incoming,
            "outgoing": outgoing,
            "self_loops": self_loops,
        })

    links_out = []
    for (src, dst), bucket in link_buckets.items():
        dwells = sorted(bucket["dwells"])
        median = dwells[len(dwells) // 2] if dwells else 0.0
        links_out.append({
            "source": node_index[src],
            "target": node_index[dst],
            "source_name": src,
            "target_name": dst,
            "value": int(bucket["value"]),
            "kind": bucket["kind"],
            "median_dwell_sec": round(median, 1),
            "median_dwell_human": _format_dwell(median),
        })
    # Sort links by value desc, cap at top 80 to keep Sankey legible.
    links_out.sort(key=lambda x: x["value"], reverse=True)
    links_out = links_out[:80]

    return {
        "days": days,
        "nodes": nodes_out,
        "links": links_out,
        "total_transitions": sum(b["value"] for b in link_buckets.values()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def strategy_timeline(session: Session, strategy_id: int) -> Dict[str, Any]:
    row = session.get(AlphaStrategy, int(strategy_id))
    if row is None:
        return {"strategy": None, "events": [], "current_status": None}
    events = (
        session.query(StageTransition)
        .filter(StageTransition.strategy_id == int(strategy_id))
        .order_by(StageTransition.transitioned_at.asc())
        .all()
    )
    out_events: List[Dict[str, Any]] = []
    prev_at: Optional[datetime] = None
    for ev in events:
        cur_at = _as_utc(ev.transitioned_at)
        dwell_sec = None
        if prev_at is not None and cur_at is not None:
            dwell_sec = (cur_at - prev_at).total_seconds()
        out_events.append({
            **ev.to_dict(),
            "dwell_sec_in_from": dwell_sec,
        })
        prev_at = cur_at
    first_at = _as_utc(events[0].transitioned_at) if events else None
    total_age_sec = (
        (datetime.now(timezone.utc) - first_at).total_seconds()
        if first_at else 0
    )
    return {
        "strategy": row.to_dict(),
        "events": out_events,
        "current_status": row.status,
        "total_age_sec": round(total_age_sec, 1),
    }


def dropout_stats(session: Session, *, days: int = 30) -> Dict[str, Any]:
    days = max(1, min(int(days), 365))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        session.query(StageTransition.from_status, StageTransition.to_status)
        .filter(StageTransition.transitioned_at >= since)
        .filter(StageTransition.actor != "backfill")
        .all()
    )

    per_from: Dict[str, Dict[str, int]] = defaultdict(lambda: {"advanced": 0, "rejected": 0, "total": 0})
    for from_s, to_s in rows:
        from_s = (from_s or "").upper()
        to_s = (to_s or "").upper()
        if not from_s or not to_s or from_s == to_s:
            continue
        bucket = per_from[from_s]
        bucket["total"] += 1
        if to_s in _TERMINAL_FAIL:
            bucket["rejected"] += 1
        else:
            bucket["advanced"] += 1

    stages_out: List[Dict[str, Any]] = []
    for status in _STAGE_ORDER:
        if status not in per_from:
            continue
        b = per_from[status]
        reject_rate = (b["rejected"] / b["total"]) if b["total"] > 1e-14 else None
        stages_out.append({
            "status": status,
            "label": status.replace("_", " ").title(),
            "advanced": b["advanced"],
            "rejected": b["rejected"],
            "total": b["total"],
            "reject_rate": round(reject_rate, 4) if reject_rate is not None else None,
        })
    return {
        "days": days,
        "stages": stages_out,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


__all__ = ["sankey", "strategy_timeline", "dropout_stats"]

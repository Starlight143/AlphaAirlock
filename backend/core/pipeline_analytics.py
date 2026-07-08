"""Pipeline analytics aggregations (P7-01 — /pipeline-analytics).

Powers four endpoints registered in :mod:`backend.app.main`:

* ``/api/pipeline-analytics/throughput`` — stacked stage counts per day/week
* ``/api/pipeline-analytics/time-in-stage`` — p50/p90/p99 + log-bin histograms
* ``/api/pipeline-analytics/gate-pass-rate`` — funnel + rolling pass-rate trend
* ``/api/pipeline-analytics/occupancy`` — current count per status + stuck list

All four read exclusively from ``stage_transitions`` (audit log) +
``alpha_strategies`` (current state). The audit table is backfilled on first
boot so the page is non-empty even on fresh deployments.

Numeric correctness:
* Every divisor guarded with ``not (x > 1e-14)`` (CLAUDE.md rule).
* Pass-rates clamped to ``[0, 1]``.
* Empty windows return ``None`` for rate metrics, not ``0.0`` — the frontend
  renders ``—`` so the operator can tell "no data" from "true zero".
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from backend.core.database import AlphaStrategy, StageTransition

logger = logging.getLogger("alpha.pipeline_analytics")

# Log-scale histogram bins for time-in-stage (minutes). Covers ~2-min auto
# pipeline runs through 90-day paper holds.
TIME_BINS_MINUTES: List[float] = [1, 5, 15, 30, 60, 240, 1440, 4320, 10080, 43200, 129600]
TIME_BIN_LABELS: List[str] = [
    "<1m", "1-5m", "5-15m", "15-30m", "30-60m", "1-4h", "4-24h",
    "1-3d", "3-7d", "7-30d", "30-90d", ">90d",
]

# Funnel gates — every (from_status → preferred_to / fail_to) flow that gates
# a strategy's advance. Stays in sync with the orchestrator's known transitions.
GATES: List[Dict[str, Any]] = [
    {"key": "intake_to_research",    "label": "Intake → Research",   "from": "INTAKE",        "pass_to": "STORY_GEN",     "fail_to": ["REJECTED"]},
    {"key": "research_to_code",      "label": "Research → Code",     "from": "STORY_GEN",     "pass_to": "CODE_GEN",      "fail_to": ["REJECTED"]},
    {"key": "code_to_backtest",      "label": "Code → Backtest",     "from": "CODE_GEN",      "pass_to": "BACKTESTING",   "fail_to": ["REJECTED"]},
    {"key": "backtest_to_critic",    "label": "Backtest → Critic",   "from": "BACKTESTING",   "pass_to": "CRITIC_LOOP",   "fail_to": ["REJECTED"]},
    {"key": "critic_to_approved",    "label": "Critic → Approved",   "from": "CRITIC_LOOP",   "pass_to": "APPROVED",      "fail_to": ["REJECTED"]},
    {"key": "approved_to_paper",     "label": "Approved → Paper",    "from": "APPROVED",      "pass_to": "PAPER_TRADE",   "fail_to": ["GRAVEYARD"]},
    {"key": "paper_to_small_cap",    "label": "Paper → Small Cap",   "from": "PAPER_TRADE",   "pass_to": "SMALL_CAPITAL", "fail_to": ["GRAVEYARD", "PAUSED"]},
    {"key": "small_to_live",         "label": "Small Cap → Live",    "from": "SMALL_CAPITAL", "pass_to": "LIVE",          "fail_to": ["GRAVEYARD", "PAUSED"]},
]

# Status → bucket-key map for the throughput stacked bar.
_STATUS_TO_BUCKET: Dict[str, str] = {
    "INTAKE": "alpha_ideas",
    "STORY_GEN": "research",
    "CODE_GEN": "factor_dev",
    "BACKTESTING": "full_backtest",
    "CRITIC_LOOP": "full_backtest",
    "APPROVED": "paper_trade",
    "PAPER_TRADE": "paper_trade",
    "SMALL_CAPITAL": "small_capital",
    "LIVE": "live",
    "REJECTED": "graveyard",
    "GRAVEYARD": "graveyard",
    "PAUSED": "graveyard",
}

# ---- TTL cache --------------------------------------------------------------

_CACHE: Dict[str, Tuple[float, Any]] = {}
_CACHE_TTL_SECONDS = 30.0
# P13/D-M2 — lock prevents dict-during-iteration RuntimeError under load
# when concurrent requests warm/expire the same key simultaneously.
_CACHE_LOCK = threading.Lock()

# Stuck-strategy detection threshold.  Strategies whose age exceeds this value
# (or the historical p90, whichever is smaller) are included in the "stuck"
# list returned by occupancy().  Override at runtime via PIPELINE_STUCK_MINUTES
# env var.  Default 1440 = 24 h, matching the prior hardcoded value so existing
# deployments see no behavior change.
_STUCK_THRESHOLD_MINUTES: float = max(1.0, float(os.environ.get("PIPELINE_STUCK_MINUTES", 1440)))


def _cache_get(key: str) -> Optional[Any]:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is None:
            return None
        ts, val = hit
        if time.monotonic() - ts > _CACHE_TTL_SECONDS:
            _CACHE.pop(key, None)
            return None
        return val


def _cache_put(key: str, val: Any) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), val)


def _safe_div(num: float, den: float) -> Optional[float]:
    if not (den > 1e-14):
        return None
    return float(num) / float(den)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite returns naive datetimes even for timezone=True columns.
    Coerce to UTC-aware so arithmetic against ``datetime.now(timezone.utc)``
    doesn't raise.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---- Throughput --------------------------------------------------------------


def throughput(session: Session, *, days: int = 30, bucket: str = "day") -> Dict[str, Any]:
    """Stacked stage counts per day or week within the window."""
    days = max(1, min(int(days), 365))
    bucket = bucket if bucket in ("day", "week") else "day"
    ck = f"throughput:{days}:{bucket}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        session.query(StageTransition.transitioned_at, StageTransition.to_status)
        .filter(StageTransition.transitioned_at >= since)
        .filter(StageTransition.actor.notin_(("backfill", "system", "operator")))  # P11-B-16: exclude seeded + system + operator rows
        .all()
    )

    # Aggregate counts[(bucket_label, bucket_key)] -> count
    series_map: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    totals: Dict[str, int] = defaultdict(int)

    for ts, status in rows:
        ts = _as_utc(ts)
        if ts is None or not status:
            continue
        if bucket == "week":
            # ISO week starting Monday
            iso_year, iso_week, _ = ts.isocalendar()
            bucket_label = f"{iso_year}-W{int(iso_week):02d}"
        else:
            bucket_label = ts.strftime("%Y-%m-%d")
        bkey = _STATUS_TO_BUCKET.get(status.upper(), "other")
        series_map[bucket_label][bkey] += 1
        totals[bkey] += 1

    series = [
        {"date": d, "counts": dict(counts)}
        for d, counts in sorted(series_map.items())
    ]
    out = {
        "days": days,
        "bucket": bucket,
        "series": series,
        "totals": dict(totals),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache_put(ck, out)
    return out


# ---- Time-in-stage histograms ----------------------------------------------


def time_in_stage(session: Session, *, days: int = 90) -> Dict[str, Any]:
    """Per-status p50/p90/p99 + 12-bin log-histograms.

    Time-in-stage = time between this transition and the next transition for
    the same strategy. Strategies still in their current stage are excluded
    (no "next" event yet) — they show up in occupancy() instead.
    """
    days = max(1, min(int(days), 365))
    ck = f"tis:{days}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        session.query(StageTransition)
        .filter(StageTransition.transitioned_at >= since)
        .filter(StageTransition.actor.notin_(("backfill", "system", "operator")))
        .order_by(StageTransition.strategy_id, StageTransition.transitioned_at, StageTransition.id)
        .all()
    )

    # Pair consecutive transitions per strategy. duration = next.t - this.t
    by_strategy: Dict[int, List[StageTransition]] = defaultdict(list)
    for r in rows:
        by_strategy[int(r.strategy_id)].append(r)

    durations_per_status: Dict[str, List[float]] = defaultdict(list)
    for sid, evts in by_strategy.items():
        if len(evts) < 2:
            continue
        for i in range(len(evts) - 1):
            this_, next_ = evts[i], evts[i + 1]
            ta = _as_utc(this_.transitioned_at)
            tb = _as_utc(next_.transitioned_at)
            if ta is None or tb is None:
                continue
            dt_min = (tb - ta).total_seconds() / 60.0
            if dt_min < 0:
                logger.warning(
                    "time_in_stage: negative duration %.3f min for strategy %s "
                    "(transition ids %s -> %s); timestamps may lack sub-second "
                    "precision — skipping pair",
                    dt_min, sid, this_.id, next_.id,
                )
                continue
            # Attribute the interval to the stage the strategy was ACTUALLY in
            # during it. A transition row records entry INTO to_status at
            # transitioned_at, so the gap until the next transition is time spent
            # in this_.to_status (matches occupancy()'s current-stage notion and
            # the docstring). Using from_status would shift every metric back by
            # exactly one stage.
            stage = (this_.to_status or "").upper()
            if stage:
                durations_per_status[stage].append(dt_min)

    per_status: List[Dict[str, Any]] = []
    for status, samples in sorted(durations_per_status.items()):
        if not samples:
            continue
        samples_sorted = sorted(samples)
        p50 = _percentile(samples_sorted, 50)
        p90 = _percentile(samples_sorted, 90)
        p99 = _percentile(samples_sorted, 99)
        # Bin into 12 log buckets.
        bins = [0] * len(TIME_BIN_LABELS)
        for s in samples_sorted:
            placed = False
            for idx, threshold in enumerate(TIME_BINS_MINUTES):
                if s < threshold:
                    bins[idx] += 1
                    placed = True
                    break
            if not placed:
                bins[-1] += 1
        per_status.append({
            "status": status,
            "bucket_key": _STATUS_TO_BUCKET.get(status, "other"),
            "n": len(samples),
            "p50_minutes": round(p50, 2),
            "p90_minutes": round(p90, 2),
            "p99_minutes": round(p99, 2),
            "histogram": [
                {"bin_label": TIME_BIN_LABELS[i], "count": int(bins[i])}
                for i in range(len(bins))
            ],
        })

    out = {
        "days": days,
        "per_status": per_status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache_put(ck, out)
    return out


def _percentile(sorted_arr: List[float], q: float) -> float:
    if not sorted_arr:
        return 0.0
    if len(sorted_arr) == 1:
        return float(sorted_arr[0])
    k = (len(sorted_arr) - 1) * (q / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_arr) - 1)
    if f == c:
        return float(sorted_arr[f])
    return float(sorted_arr[f] + (sorted_arr[c] - sorted_arr[f]) * (k - f))


# ---- Gate pass-rate --------------------------------------------------------


def gate_pass_rate(session: Session, *, days: int = 90, window: int = 14) -> Dict[str, Any]:
    """Per-gate aggregate pass-rate + rolling window trend.

    pass_rate = passed / total. None when total == 0 — frontend renders "—"
    so operators distinguish "no data" from "true zero".
    """
    days = max(1, min(int(days), 365))
    window = max(1, min(int(window), days))
    ck = f"gpr:{days}:{window}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        session.query(StageTransition.from_status, StageTransition.to_status,
                      StageTransition.transitioned_at)
        .filter(StageTransition.transitioned_at >= since)
        # P11-B-16 / gate-pass-rate fix: keep excluding seeded ('backfill') and
        # 'system' (pause-all emergency stop) rows, but INCLUDE 'operator' — the
        # late-stage capital-allocation gates (Approved->Paper->SmallCap->Live)
        # are written ONLY by the operator-driven /promote + /retire endpoints
        # (backend/app/main.py:3558, 3688), so excluding 'operator' zeroed them
        # out and the funnel/trend rendered '—'. Throughput/time-in-stage keep
        # their stricter filter for de-dup; gate-pass-rate must count operator rows.
        .filter(StageTransition.actor.notin_(("backfill", "system")))
        .all()
    )

    aggregate = []
    for gate in GATES:
        from_s = gate["from"]
        pass_s = gate["pass_to"]
        fail_s = set(gate["fail_to"])
        gate_rows = [r for r in rows if (r[0] or "").upper() == from_s]
        passed = sum(1 for r in gate_rows if (r[1] or "").upper() == pass_s)
        failed = sum(1 for r in gate_rows if (r[1] or "").upper() in fail_s)
        total = passed + failed
        rate = _safe_div(passed, total)
        aggregate.append({
            **gate,
            "total": int(total),
            "passed": int(passed),
            "failed": int(failed),
            "pass_rate": (round(max(0.0, min(1.0, rate)), 4) if rate is not None else None),
        })

    # Rolling trend: bucket transitions per day, compute per-gate rate over
    # trailing `window` days.
    #
    # Pre-bucket rows by calendar date once (O(|rows|)) so the inner loop only
    # touches at most `window+1` date-buckets per day instead of scanning all
    # rows on every iteration.  Reduces O(days × |rows| × gates) to
    # O(|rows| + days × window × gates).
    trend_days: List[Dict[str, Any]] = []
    cur = since.replace(hour=0, minute=0, second=0, microsecond=0)
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows_norm = [(r[0], r[1], _as_utc(r[2])) for r in rows if _as_utc(r[2]) is not None]

    # Build a dict mapping calendar date → list of (from_s, to_s) tuples.
    _by_date: Dict[Any, List[Tuple[str, str]]] = defaultdict(list)
    for from_s_raw, to_s_raw, ts in rows_norm:
        _by_date[ts.date()].append(((from_s_raw or "").upper(), (to_s_raw or "").upper()))

    # Pre-compute frozensets for fail_to so they are not re-created per iteration.
    _gate_fail_sets = [frozenset(s.upper() for s in gate["fail_to"]) for gate in GATES]

    while cur <= end:
        window_start = cur - timedelta(days=window)
        # Collect only the day-buckets that fall inside [window_start, cur].
        in_window: List[Tuple[str, str]] = []
        d = window_start.date()
        cur_date = cur.date()
        while d <= cur_date:
            bucket = _by_date.get(d)
            if bucket:
                in_window.extend(bucket)
            d += timedelta(days=1)

        rates_today: Dict[str, Optional[float]] = {}
        for gate, fail_set in zip(GATES, _gate_fail_sets):
            from_s = gate["from"]
            pass_s = gate["pass_to"]
            passed = 0
            failed = 0
            for fs, ts in in_window:
                if fs != from_s:
                    continue
                if ts == pass_s:
                    passed += 1
                elif ts in fail_set:
                    failed += 1
            total = passed + failed
            rate = _safe_div(passed, total)
            rates_today[gate["key"]] = (
                round(max(0.0, min(1.0, rate)), 4) if rate is not None else None
            )
        trend_days.append({"date": cur.strftime("%Y-%m-%d"), "rates": rates_today})
        cur += timedelta(days=1)

    out = {
        "days": days,
        "window": window,
        "gates": aggregate,
        "trend": trend_days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache_put(ck, out)
    return out


# ---- Occupancy + bottleneck -----------------------------------------------


def occupancy(session: Session) -> Dict[str, Any]:
    """Current strategy count per status + median age + stuck list.

    "Stuck" = age > 24 hours OR age > p90 of historical time-in-stage for
    that status (whichever is smaller). Sorted desc by age.
    """
    ck = "occupancy"
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    now = datetime.now(timezone.utc)
    strategies = session.query(AlphaStrategy).all()
    # Latest transition per strategy (entry time into current status).
    # Exclude backfill/system/operator rows consistent with time_in_stage so
    # that seeded rows and emergency-pause transitions do not displace the real
    # pipeline-driven entry time. Strategies with only excluded-actor rows fall
    # back to s.updated_at via the existing entered_at.get() miss path below.
    # R6/PERF-17: bound the scan to the latest transition PER strategy via a
    # MAX(transitioned_at) GROUP BY subquery instead of materialising the entire
    # append-only stage_transitions history on every (cache-miss) call. The join
    # can return >1 row for a strategy only on an exact transitioned_at tie; the
    # seen-loop below (id.desc) still deterministically picks the highest id, so
    # the result is byte-identical to the previous full-scan-then-dedup approach.
    _excluded_actors = ("backfill", "system", "operator")
    _latest_ts = (
        session.query(
            StageTransition.strategy_id.label("sid"),
            func.max(StageTransition.transitioned_at).label("max_ts"),
        )
        .filter(StageTransition.actor.notin_(_excluded_actors))
        .group_by(StageTransition.strategy_id)
        .subquery()
    )
    latest_transitions = (
        session.query(StageTransition)
        .join(
            _latest_ts,
            and_(
                StageTransition.strategy_id == _latest_ts.c.sid,
                StageTransition.transitioned_at == _latest_ts.c.max_ts,
            ),
        )
        .filter(StageTransition.actor.notin_(_excluded_actors))  # P11-B-16 consistent
        .order_by(StageTransition.strategy_id, StageTransition.transitioned_at.desc(), StageTransition.id.desc())
        .all()
    )
    seen: set = set()
    entered_at: Dict[int, datetime] = {}
    for tr in latest_transitions:
        if tr.strategy_id in seen:
            continue
        seen.add(tr.strategy_id)
        ts = _as_utc(tr.transitioned_at)
        if ts is not None:
            entered_at[int(tr.strategy_id)] = ts

    by_status: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for s in strategies:
        status = (s.status or "").upper()
        if not status:
            continue
        entry = entered_at.get(int(s.id))
        if entry is None:
            entry = _as_utc(s.updated_at) or now
        age_minutes = max(0.0, (now - entry).total_seconds() / 60.0)
        by_status[status].append({
            "id": int(s.id),
            "name": s.name or f"strategy_{s.id}",
            "age_minutes": round(age_minutes, 2),
        })

    # Historical p90 time-in-stage per status (reuses the 30s-cached
    # time_in_stage result so this is a cache-hit, not a re-query).
    tis = time_in_stage(session)
    hist_p90: Dict[str, float] = {
        row["status"]: row["p90_minutes"] for row in tis.get("per_status", [])
    }

    per_status: List[Dict[str, Any]] = []
    bottleneck_status: Optional[str] = None
    bottleneck_median: float = -1.0
    for status, items in sorted(by_status.items()):
        if not items:
            continue
        ages = sorted([it["age_minutes"] for it in items])
        median_age = _percentile(ages, 50)
        # "Stuck" = age > configured threshold OR age > historical p90 for
        # this status (whichever is smaller). Fall back to the threshold cap
        # when no historical samples exist for the status.
        # Override via PIPELINE_STUCK_MINUTES env var (default: 1440 = 24 h).
        # Read dynamically per call so runtime changes to the env var are
        # respected without requiring a process restart (mirrors the
        # PIPELINE_STUCK_FLOOR_MINUTES pattern below).
        stuck_ceiling = max(1.0, float(os.environ.get("PIPELINE_STUCK_MINUTES", 1440)))
        _stuck_floor = max(1.0, float(os.environ.get("PIPELINE_STUCK_FLOOR_MINUTES", 30)))
        stuck_threshold = max(_stuck_floor, min(stuck_ceiling, hist_p90.get(status, stuck_ceiling)))
        stuck = [it for it in items if it["age_minutes"] >= stuck_threshold]
        stuck.sort(key=lambda x: x["age_minutes"], reverse=True)
        per_status.append({
            "status": status,
            "bucket_key": _STATUS_TO_BUCKET.get(status, "other"),
            "count": len(items),
            "median_age_minutes": round(median_age, 2),
            "stuck": stuck[:20],  # cap UI list
            "stuck_threshold_minutes": stuck_threshold,
        })
        if median_age > bottleneck_median:
            bottleneck_median = median_age
            bottleneck_status = status

    # Report the ceiling actually used (re-read so it matches what each
    # per-status row used for filtering, avoiding operator confusion when
    # the env var is changed and the top-level value differs from per-row).
    out = {
        "per_status": per_status,
        "bottleneck_status": bottleneck_status,
        "stuck_threshold_minutes": max(1.0, float(os.environ.get("PIPELINE_STUCK_MINUTES", 1440))),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache_put(ck, out)
    return out


__all__ = [
    "throughput",
    "time_in_stage",
    "gate_pass_rate",
    "occupancy",
    "GATES",
]

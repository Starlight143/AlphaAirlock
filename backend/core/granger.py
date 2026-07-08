"""Granger causality edges between high-IC KnowledgeNodes (P6-B5).

Why this exists
---------------
The reference UI's factor network includes edge weights derived from
Granger causality p-values: "does X's recent dynamics predict Y's?". Our
``/api/factor-network`` payload previously hard-coded ``granger_p: None`` for
every edge — this module finally populates that field.

Data source
-----------
We feed the ``ic_history`` ledger (B3) as the per-node time series: each node
contributes one observation per IC mutation (decay tick, pipeline outcome,
manual annotation). A pair needs ``MIN_OBSERVATIONS`` matched observations
to be testable; below that we skip silently.

Cold-start behaviour
--------------------
Fresh deployments have empty ``ic_history``. The recompute function still runs
without crashing — it produces zero edges and logs the reason. As the system
runs, IC decay + pipeline outcomes populate the ledger, and successive
recomputes start producing real edges.

Scheduling
----------
``recompute_top_pairs`` is wired into ``periodic_tasks.py`` and runs weekly by
default (env ``PERIODIC_GRANGER_RECOMPUTE_SECONDS`` to override). It is also
exposed as ``POST /api/factor-network/granger/recompute`` for on-demand
recomputation.

Verified intact post P11-B-02
-----------------------------
P11-B-02 wired ``record_pipeline_outcomes`` into every APPROVED/REJECTED exit
of the orchestrator. That increases the rate at which ``ic_history`` rows
accumulate, which directly feeds this module: future Granger recomputes will
see denser per-node series and produce more (and more reliable) edges. The
threshold gates (``MIN_OBSERVATIONS``, p_max) and the test surface remain
unchanged — no behavioural change here, just a richer upstream signal.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from sqlalchemy.orm import Session

from backend._envloader import env_bool, env_float, env_int
from backend.core.database import (
    GrangerEdge,
    IcHistory,
    KnowledgeNode,
    session_scope,
)

logger = logging.getLogger("alpha.granger")

MIN_OBSERVATIONS = 20


def is_enabled() -> bool:
    return env_bool("GRANGER_ENABLED", False)


def _ic_floor() -> float:
    return env_float("GRANGER_IC_FLOOR", 0.3, minimum=0.0, maximum=1.0)


def _max_pairs() -> int:
    """Maximum number of *unordered* node pairs to test per recompute run.

    Each unordered pair (A, B) produces two *directional* Granger tests
    (A→B and B→A) and therefore up to two GrangerEdge rows.  Setting
    ``GRANGER_MAX_PAIRS=N`` thus allows up to ``2*N`` DB writes.
    The ``cap_nodes`` node-selection limit is derived from this value via
    ``sqrt(2 * max_pairs)`` so that the node pool is always large enough to
    fill the budget — this is intentional and consistent with the unordered
    pair interpretation.
    """
    return env_int("GRANGER_MAX_PAIRS", 50, minimum=1, maximum=500)


def _max_lag() -> int:
    return env_int("GRANGER_MAX_LAG", 1, minimum=1, maximum=4)


def _eligible_nodes(session: Session, ic_floor: float, limit: int) -> List[int]:
    """Top-N KnowledgeNodes by IC that are at or above the floor."""
    rows = (
        session.query(KnowledgeNode.id)
        .filter(KnowledgeNode.ic_score >= ic_floor)
        .order_by(KnowledgeNode.ic_score.desc())
        .limit(int(limit))
        .all()
    )
    return [int(r[0]) for r in rows]


def _series_for(session: Session, node_id: int) -> Optional[List[Tuple[datetime, float]]]:
    """Return ``[(ts, ic_value), ...]`` ordered by ts ascending."""
    rows = (
        session.query(IcHistory)
        .filter(IcHistory.node_id == int(node_id))
        .order_by(IcHistory.recorded_at.asc())
        .all()
    )
    if not rows:
        return None
    out: List[Tuple[datetime, float]] = []
    for r in rows:
        ts = r.recorded_at
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ic_val = r.ic_value
        if ic_val is None:
            continue
        out.append((ts, float(ic_val)))
    return out


def _align_pair(
    a: Sequence[Tuple[datetime, float]],
    b: Sequence[Tuple[datetime, float]],
    *,
    bucket_hours: int = 24,
) -> Tuple[np.ndarray, np.ndarray]:
    """Bucket both series to a fixed daily grid, fill-forward, intersect range.

    Returns ``(arr_a, arr_b)`` aligned. Either may be empty if no overlap.
    """
    if not a or not b:
        return np.array([]), np.array([])
    start = max(a[0][0], b[0][0])
    end = min(a[-1][0], b[-1][0])
    if end <= start:
        return np.array([]), np.array([])
    delta = timedelta(hours=int(bucket_hours))
    grid: List[datetime] = []
    t = start
    while t <= end:
        grid.append(t)
        t += delta
    if not grid:
        return np.array([]), np.array([])

    def _bucket(series: Sequence[Tuple[datetime, float]]) -> np.ndarray:
        # Walk series + grid in tandem; for each grid timestamp use the most
        # recent IC observation at or before it (forward-fill).
        out = np.zeros(len(grid), dtype=float)
        idx = 0
        last_val = series[0][1]
        for i, g in enumerate(grid):
            while idx < len(series) and series[idx][0] <= g:
                last_val = series[idx][1]
                idx += 1
            out[i] = last_val
        return out

    return _bucket(a), _bucket(b)


def _safe_std(x: np.ndarray) -> float:
    """Reject subnormal std per CLAUDE.md float rule."""
    try:
        s = float(np.std(x))
    except (TypeError, ValueError):
        return 0.0
    return s if s > 1e-14 else 0.0


def compute_granger_for_pair(
    series_x: Sequence[Tuple[datetime, float]],
    series_y: Sequence[Tuple[datetime, float]],
    *,
    max_lag: int = 1,
) -> Optional[Dict[str, float]]:
    """Run grangercausalitytests on aligned series. Returns ``{p_value, lag, sample_size}``
    or None when computation isn't possible (too few samples, constant series, etc.).
    """
    try:
        # P15/D-L18 — requires statsmodels >= 0.14. Earlier versions accept
        # `maxlag` as an int only; the `maxlag=[int]` list form (used below)
        # was added in 0.14 to return one lag's results without computing the
        # full sweep. requirements.txt pins statsmodels appropriately.
        from statsmodels.tsa.stattools import grangercausalitytests
    except ImportError:
        logger.warning("granger: statsmodels missing — skipping computation")
        return None

    arr_x, arr_y = _align_pair(series_x, series_y)
    n = len(arr_x)
    if n < MIN_OBSERVATIONS:
        return None
    # P31-GRANGER-DOF1: `n` counts the forward-filled daily grid, not distinct
    # IC observations. A handful of sparse IC events forward-filled across many
    # days inflates the regression's degrees of freedom → anti-conservative
    # p-values. Require a minimum count of DISTINCT values on BOTH series so a
    # near-constant forward-filled grid is skipped (same `return None` contract
    # the std guard and caller already use).
    _MIN_DISTINCT = 5
    if np.unique(arr_x).size < _MIN_DISTINCT or np.unique(arr_y).size < _MIN_DISTINCT:
        return None
    # R5/QR-3: `n` above counts the forward-filled daily GRID, not the number of
    # real IC observations. A handful of sparse events ffilled across many days
    # passes the grid (MIN_OBSERVATIONS) and unique-value (_MIN_DISTINCT) checks
    # yet feeds grangercausalitytests n≈grid_size, inflating the F-test DoF →
    # anti-conservative p-values. Require a floor of RAW observations on both
    # series within the shared overlap window (same `return None` contract).
    _MIN_DISTINCT_OBS = 15
    if series_x and series_y:
        _start = max(series_x[0][0], series_y[0][0])
        _end = min(series_x[-1][0], series_y[-1][0])
        _nx_obs = sum(1 for ts, _ in series_x if _start <= ts <= _end)
        _ny_obs = sum(1 for ts, _ in series_y if _start <= ts <= _end)
        if _nx_obs < _MIN_DISTINCT_OBS or _ny_obs < _MIN_DISTINCT_OBS:
            return None
    # P13/D-M3 — IEEE 754 subnormal floats (e.g. 5e-324) pass `> 0.0` checks
    # but blow up the regression when used as divisors. Use the project-wide
    # `not (x > 1e-14)` guard from CLAUDE.md.
    if not (_safe_std(arr_x) > 1e-14) or not (_safe_std(arr_y) > 1e-14):
        return None

    # statsmodels expects shape (n, 2) with target in column 0, predictor in column 1.
    data = np.column_stack([arr_y, arr_x])
    try:
        # statsmodels deprecated the `verbose` kwarg in 0.14 (warns even when
        # passing False). The function never prints with default kwargs, so
        # omitting verbose is the clean modern call signature.
        result = grangercausalitytests(data, maxlag=[int(max_lag)])
    except Exception as exc:  # noqa: BLE001
        logger.debug("granger: test failed for pair (n=%s): %s", n, exc)
        return None

    try:
        # result[lag][0] is a dict of test_name → (stat, p, df_denom, df_num)
        p_val = float(result[int(max_lag)][0]["ssr_ftest"][1])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.debug("granger: result parse failed: %s", exc)
        return None
    if not math.isfinite(p_val):
        return None
    # D-H4/P16 — clamp to [0, 1] so floating-point rounding from the F-test
    # can't push a stored p_value slightly outside the valid probability range
    # (downstream operator p_max thresholds assume a clean unit interval).
    return {
        "p_value": max(0.0, min(1.0, p_val)),
        "lag": int(max_lag),
        "sample_size": n,
    }


def recompute_top_pairs(session: Optional[Session] = None) -> Dict[str, int]:
    """Recompute Granger edges for every eligible high-IC pair.

    Default-OFF; honors ``GRANGER_ENABLED``. Uses unique upsert on
    ``(src_node_id, dst_node_id, lag)`` so the function is idempotent.
    """
    if not is_enabled():
        return {"enabled": 0, "evaluated": 0, "written": 0, "skipped": 0}

    own_session = session is None
    stats = {"enabled": 1, "evaluated": 0, "written": 0, "skipped": 0, "skipped_directional": 0}
    ic_floor = _ic_floor()
    cap_nodes = max(10, int(math.sqrt(2 * _max_pairs())) + 5)
    max_lag = _max_lag()

    def _run(s: Session) -> None:
        node_ids = _eligible_nodes(s, ic_floor, cap_nodes)
        if len(node_ids) < 2:
            return
        # Cache series per node — same series feeds N-1 pair computations.
        # Bulk-fetch all IcHistory rows for the eligible nodes in one query
        # instead of one SELECT per node (N+1 → 1 round trip).
        series_cache: Dict[int, Optional[List[Tuple[datetime, float]]]] = {}
        bulk_rows = (
            s.query(IcHistory)
            .filter(IcHistory.node_id.in_(node_ids))
            .order_by(IcHistory.node_id, IcHistory.recorded_at.asc())
            .all()
        )
        # Group rows by node_id and apply the same validation as _series_for.
        _raw: Dict[int, List[Tuple[datetime, float]]] = {nid: [] for nid in node_ids}
        for r in bulk_rows:
            nid = int(r.node_id)
            ts = r.recorded_at
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ic_val = r.ic_value
            if ic_val is None:
                continue
            _raw[nid].append((ts, float(ic_val)))
        for nid in node_ids:
            series_cache[nid] = _raw[nid] if _raw[nid] else None

        pair_budget = _max_pairs()
        pairs_done = 0
        for i, src in enumerate(node_ids):
            sx = series_cache[src]
            if sx is None:
                # R3 BE8-4: count every (src, dst) pair skipped because the src
                # series is unavailable, so `skipped` is an honest complement to
                # `evaluated` instead of remaining a permanent 0.
                stats["skipped"] += len(node_ids) - (i + 1)
                continue
            for dst in node_ids[i + 1:]:
                if pairs_done >= pair_budget:
                    break
                sy = series_cache[dst]
                if sy is None:
                    stats["skipped"] += 1  # R3 BE8-4: this pair skipped (dst series absent)
                    continue
                stats["evaluated"] += 1
                # Run both directions; Granger is not symmetric.
                for src_id, dst_id, sxx, syy in (
                    (src, dst, sx, sy),
                    (dst, src, sy, sx),
                ):
                    out = compute_granger_for_pair(sxx, syy, max_lag=max_lag)
                    if out is None:
                        stats["skipped_directional"] += 1
                        continue
                    existing = (
                        s.query(GrangerEdge)
                        .filter(
                            GrangerEdge.src_node_id == int(src_id),
                            GrangerEdge.dst_node_id == int(dst_id),
                            GrangerEdge.lag == int(out["lag"]),
                        )
                        .first()
                    )
                    if existing is not None:
                        existing.p_value = float(out["p_value"])
                        existing.sample_size = int(out["sample_size"])
                        existing.computed_at = datetime.now(timezone.utc)
                    else:
                        s.add(
                            GrangerEdge(
                                src_node_id=int(src_id),
                                dst_node_id=int(dst_id),
                                p_value=float(out["p_value"]),
                                lag=int(out["lag"]),
                                sample_size=int(out["sample_size"]),
                            )
                        )
                    stats["written"] += 1
                pairs_done += 1
            if pairs_done >= pair_budget:
                break
        s.flush()

    if own_session:
        with session_scope() as s:
            _run(s)
    else:
        _run(session)
    logger.info("granger recompute complete: %s", stats)
    return stats


def edges_for_factor_network(session: Session, p_max: float = 0.05) -> List[Dict[str, object]]:
    """Return current Granger edges below the p-value threshold. Used by
    ``/api/factor-network`` to enrich edges with ``granger_p`` + ``lag``."""
    rows = (
        session.query(GrangerEdge)
        .filter(GrangerEdge.p_value <= float(p_max))
        .order_by(GrangerEdge.p_value.asc())
        .all()
    )
    return [r.to_dict() for r in rows]


__all__ = [
    "is_enabled",
    "compute_granger_for_pair",
    "recompute_top_pairs",
    "edges_for_factor_network",
]

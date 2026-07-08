"""Strategy diversity gate (P-DIVERSITY).

QFLM's core insight (Obsidian: 《Autoresearch — AI 自動研究框架》): you don't want
the single highest-IC factor, you want MANY *decorrelated* signals — "我們要的
不是一個最強的因子，而是很多彼此不同、各有用的信號". The pipeline today has no
quantitative check that a newly-approved strategy isn't a near-duplicate of one
already in the book, so it can accumulate 50 momentum clones.

This module measures the candidate's redundancy against the already-approved
pool as the maximum POSITIVE correlation of daily equity-curve returns (a
negatively-correlated strategy is a diversifier and is welcomed, so only strong
positive correlation counts as redundant).

Observe-first, mirroring :mod:`backend.core.strategy_gates`:
``STRATEGY_DIVERSITY_GATE`` defaults OFF — the max correlation is recorded on the
strategy for telemetry without changing the verdict until an operator opts in.

The approved pool's return series are cached and only reloaded when the set of
approved strategy ids changes (approvals are infrequent), so repeated
evaluations are cheap.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from backend._envloader import env_bool, env_float, env_int
from backend.core.database import PROJECT_ROOT, AlphaStrategy, session_scope

logger = logging.getLogger("alpha.diversity")

RESULTS_DIR: Path = PROJECT_ROOT / "storage" / "results"

# Post-approval "good" states whose strategies form the elite/comparison pool.
_POOL_STATUSES: Tuple[str, ...] = ("APPROVED", "PAPER_TRADE", "SMALL_CAPITAL", "LIVE")


# --------------------------------------------------------------------------- #
# Config (env-overridable)                                                    #
# --------------------------------------------------------------------------- #

def gate_enforced() -> bool:
    """When False (default) the gate is OBSERVE-only: the max correlation is
    recorded but the verdict is unchanged. Set 1 to let it veto an LLM "Go"."""
    return env_bool("STRATEGY_DIVERSITY_GATE", False)


def max_corr_threshold() -> float:
    """A candidate whose return-correlation with any approved strategy exceeds
    this is 'redundant'. Default 0.90."""
    return env_float("STRATEGY_DIVERSITY_MAX_CORR", 0.90, minimum=0.0, maximum=1.0)


def pool_size() -> int:
    return env_int("STRATEGY_DIVERSITY_POOL_SIZE", 30, minimum=1, maximum=500)


def min_overlap_bars() -> int:
    """Minimum overlapping daily points for a correlation to be trusted."""
    return env_int("STRATEGY_DIVERSITY_MIN_OVERLAP", 20, minimum=2, maximum=100_000)


# --------------------------------------------------------------------------- #
# Result                                                                      #
# --------------------------------------------------------------------------- #

@dataclass
class DiversityResult:
    max_correlation: Optional[float]
    most_similar_id: Optional[int]
    enforced: bool
    passed: bool
    redundant: bool
    pool_size: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_correlation": self.max_correlation,
            "most_similar_id": self.most_similar_id,
            "enforced": self.enforced,
            "passed": self.passed,
            "redundant": self.redundant,
            "pool_size": self.pool_size,
        }


# --------------------------------------------------------------------------- #
# Return-series + correlation primitives (pure, unit-testable)                #
# --------------------------------------------------------------------------- #

def returns_from_equity_curve(curve: Optional[List[Dict[str, Any]]]) -> Optional[pd.Series]:
    """Daily return series from an engine equity curve (``[{timestamp, equity}]``).
    Returns None when there are too few clean points."""
    if not curve:
        return None
    pairs: List[Tuple[pd.Timestamp, float]] = []
    for p in curve:
        if not isinstance(p, dict):
            continue
        try:
            ts = pd.Timestamp(p.get("timestamp"))
            eq = float(p.get("equity"))
        except (TypeError, ValueError):
            continue
        if pd.isna(ts) or not math.isfinite(eq):
            continue
        pairs.append((ts, eq))
    if len(pairs) < 3:
        return None
    s = pd.Series(dict(pairs)).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    r = s.pct_change().replace([float("inf"), float("-inf")], pd.NA).dropna()
    return r if len(r) >= 2 else None


def correlation(a: Optional[pd.Series], b: Optional[pd.Series],
                min_overlap: int) -> Optional[float]:
    """Pearson correlation of two return series aligned on their shared dates.
    None if the overlap is too small or degenerate."""
    if a is None or b is None:
        return None
    joined = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(joined) < min_overlap:
        return None
    try:
        c = joined.iloc[:, 0].corr(joined.iloc[:, 1])
    except Exception:  # noqa: BLE001
        return None
    if c is None or not math.isfinite(c):
        return None
    return max(-1.0, min(1.0, float(c)))


def max_correlation_against(candidate: Optional[pd.Series],
                            pool: List[Tuple[int, pd.Series]],
                            min_overlap: int) -> Tuple[Optional[float], Optional[int]]:
    """Most positively-correlated pool member (the redundancy signal)."""
    if candidate is None or not pool:
        return None, None
    best_c: Optional[float] = None
    best_id: Optional[int] = None
    for sid, series in pool:
        c = correlation(candidate, series, min_overlap)
        if c is None:
            continue
        if best_c is None or c > best_c:
            best_c, best_id = c, sid
    return best_c, best_id


# --------------------------------------------------------------------------- #
# Approved-pool loading (cached; reloaded only when the id-set changes)        #
# --------------------------------------------------------------------------- #

_POOL_LOCK = threading.Lock()
_POOL_CACHE: Dict[str, Any] = {"key": None, "pool": []}


def _approved_ids(limit: int, exclude_id: Optional[int]) -> List[int]:
    with session_scope() as s:
        rows = (
            s.query(AlphaStrategy.id)
            .filter(AlphaStrategy.status.in_(_POOL_STATUSES))
            .order_by(AlphaStrategy.id.desc())
            .limit(limit * 2)  # headroom: some result files may be missing
            .all()
        )
    out: List[int] = []
    for r in rows:
        sid = int(r[0])
        if exclude_id is not None and sid == int(exclude_id):
            continue
        out.append(sid)
        if len(out) >= limit:
            break
    return out


def _load_pool(limit: int, exclude_id: Optional[int]) -> List[Tuple[int, pd.Series]]:
    ids = _approved_ids(limit, exclude_id)
    key = ",".join(str(i) for i in sorted(ids))
    with _POOL_LOCK:
        if _POOL_CACHE.get("key") == key:
            return list(_POOL_CACHE["pool"])
    pool: List[Tuple[int, pd.Series]] = []
    for sid in ids:
        path = RESULTS_DIR / f"strategy_{sid}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        r = returns_from_equity_curve(data.get("equity_curve"))
        if r is not None:
            pool.append((sid, r))
    with _POOL_LOCK:
        _POOL_CACHE["key"] = key
        _POOL_CACHE["pool"] = pool
    return pool


def invalidate_pool_cache() -> None:
    """Drop the cached pool (e.g. after a new approval). Best-effort hook."""
    with _POOL_LOCK:
        _POOL_CACHE["key"] = None
        _POOL_CACHE["pool"] = []


# --------------------------------------------------------------------------- #
# Public entry                                                                #
# --------------------------------------------------------------------------- #

def evaluate(candidate_id: Optional[int],
             equity_curve: Optional[List[Dict[str, Any]]]) -> DiversityResult:
    """Score a candidate's redundancy against the approved pool. Never raises."""
    enforced = gate_enforced()
    candidate = returns_from_equity_curve(equity_curve)
    try:
        pool = _load_pool(pool_size(), candidate_id)
    except Exception:  # noqa: BLE001
        logger.exception("diversity: approved-pool load failed (non-fatal)")
        pool = []
    if candidate is None or not pool:
        # Nothing to compare against (empty book or unusable curve) → not redundant.
        return DiversityResult(None, None, enforced, True, False, len(pool))
    best_c, best_id = max_correlation_against(candidate, pool, min_overlap_bars())
    if best_c is None:
        return DiversityResult(None, None, enforced, True, False, len(pool))
    redundant = best_c > max_corr_threshold()
    passed = True if not enforced else (not redundant)
    return DiversityResult(round(best_c, 4), best_id, enforced, passed, redundant, len(pool))


__all__ = [
    "DiversityResult",
    "gate_enforced",
    "max_corr_threshold",
    "returns_from_equity_curve",
    "correlation",
    "max_correlation_against",
    "evaluate",
    "invalidate_pool_cache",
]

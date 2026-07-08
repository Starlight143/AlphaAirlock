"""Portfolio optimizer extension (P7-07 — /portfolio-optimizer).

Builds on :mod:`backend.core.portfolio.combine_portfolio` to add:

* Constraint application (max/min weight clip, water-filling renormalize)
* Per-strategy marginal-risk-contribution + diversification ratio
* Efficient-frontier sweep across vol_target_annual values
* Persistence of named portfolios via the ``Portfolio`` model
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from sqlalchemy.orm import Session

from backend.core.allocators import (
    ALLOCATORS,
    METHOD_KEYS,
    METHOD_LABELS,
    allocate,
    diversification_ratio,
    marginal_risk_contribution,
    portfolio_realized_vol,
    vol_target,
)
from backend.core.database import AlphaStrategy, Portfolio
from backend.core.portfolio import (
    _combined_metrics,
    _equity_to_returns,
    _load_equity_series,
    combine_portfolio,
)

logger = logging.getLogger("alpha.portfolio_optimizer")


def _apply_constraints(
    weights: Dict[int, float],
    *,
    max_weight: float,
    min_weight: float,
    allow_short: bool,
) -> Dict[int, float]:
    """Clip then water-fill renormalize until sum == 1.0 (best-effort, 50 iters).

    P31-PM2 invariants:
      - ``min_weight`` only applies to strategies the allocator gave a
        POSITIVE weight — strategies the allocator zeroed are left at 0 so
        they don't get force-allocated despite being excluded for cause.
      - Final weights respect ``max_weight`` AND sum to 1.0 within 1e-9; if
        both cannot be satisfied (n_active * max_weight < 1.0) we split
        evenly across actives at max_weight and surface the cash drag.
    """
    # Preserve which sids were "active" (non-zero) at input — these are the
    # only ones the min_weight floor applies to.
    active_sids = {sid for sid, v in weights.items() if (abs(v) if allow_short else v) > 1e-14}
    w = dict(weights)
    if not allow_short:
        w = {sid: max(0.0, v) for sid, v in w.items()}
    # Pre-feasibility check: if every active strategy gets at most
    # max_weight, we need n_active * max_weight >= 1.0 to reach full
    # allocation. If infeasible at this cap, split evenly at max_weight and
    # let the residual remain cash so the operator can widen the cap.
    if active_sids:
        if not allow_short and len(active_sids) * max_weight < 1.0 - 1e-9:
            return {
                sid: round(max_weight if sid in active_sids else 0.0, 6)
                for sid in weights
            }
        # P31-PM-MIN1: symmetric min_weight infeasibility pre-check. If every
        # active must hold at least min_weight but n_active * min_weight > 1.0,
        # the clip→renormalize loop below can never get the sum down to 1.0
        # (the floor re-clips each iteration) and returns weights summing to
        # >1.0 (e.g. min=0.40 over 3 actives → 1.30, i.e. 130% invested). Fall
        # back to an even split across actives, which sums to exactly 1.0.
        if len(active_sids) * min_weight > 1.0 + 1e-9:
            even = 1.0 / float(len(active_sids))
            return {
                sid: round(even if sid in active_sids else 0.0, 6)
                for sid in weights
            }
    # B8-2 fix: when allow_short=True, use -max_weight as the floor for
    # negative active weights instead of min_weight (which is always >= 0.0,
    # validated at the call-site). Without this, every short position was
    # silently zeroed by max(min_weight>=0, min(max_weight, v<0)) == 0.
    if allow_short and any(v < -1e-14 for sid, v in w.items() if sid in active_sids):
        logger.debug(
            "_apply_constraints: allow_short=True with %d short active position(s); "
            "using [-max_weight, max_weight] clip (max_weight=%.4f)",
            sum(1 for sid, v in w.items() if sid in active_sids and v < -1e-14),
            max_weight,
        )
    for _ in range(50):
        w = {
            sid: (
                max(
                    (min_weight if (not allow_short or v >= -1e-14) else -max_weight),
                    min(max_weight, v),
                )
                if sid in active_sids
                else min(max_weight, max(0.0, v))
            )
            for sid, v in w.items()
        }
        total = sum(w.values())
        if total <= 1e-14:
            return {sid: 0.0 for sid in weights}
        if abs(total - 1.0) < 1e-9:
            return {sid: round(v, 6) for sid, v in w.items()}
        pinned_high = {
            sid: max_weight for sid, v in w.items()
            if abs(v - max_weight) < 1e-12 and sid in active_sids
        }
        # R5/QT-2: symmetric short-leg pin. When allow_short=True, a leg clipped to
        # exactly -max_weight must be excluded from `movable` and subtracted in
        # target_movable, mirroring pinned_high; otherwise the short leg skews
        # movable_sum and the scale factor and the long-short book never converges
        # to sum=1.0 (oscillates across all 50 iterations). For allow_short=False
        # pinned_low is always empty, so every path reduces to the previous
        # behaviour: in the regime that reaches `scale`, target_movable equals the
        # old max(0.0, 1 - n_high*max_weight); the negative/zero case is caught
        # identically by the movable_sum/target_movable <= 1e-14 guard below.
        pinned_low = {
            sid: -max_weight for sid, v in w.items()
            if allow_short and abs(v + max_weight) < 1e-12 and sid in active_sids
        }
        movable = {
            sid: v for sid, v in w.items()
            if sid not in pinned_high and sid not in pinned_low and sid in active_sids
        }
        movable_sum = sum(movable.values())
        target_movable = 1.0 - sum(pinned_high.values()) - sum(pinned_low.values())
        # QT-SHORT: When allow_short=True, movable can contain net-short legs
        # whose sum is negative. The original guard `movable_sum <= 1e-14`
        # conflates two distinct cases:
        #   (a) movable_sum ≈ 0: true degeneracy — no capacity to redistribute.
        #   (b) movable_sum < 0: net-short movable with a positive target_movable.
        # In case (b) the old code fell through to `v / total` renormalization,
        # which with total < 1.0 (e.g. 0.40 = 0.30+0.30-0.20) rescales long
        # legs above max_weight (e.g. 0.30/0.40 = 0.75), bypassing the entire
        # water-fill loop. Computing scale = target_movable / movable_sum would
        # also be wrong (negative scale flips short→long). Fix: distinguish the
        # two cases. For (b), zero the movable legs so the loop can redistribute
        # through the pinned legs on the next iteration.
        if target_movable <= 1e-14:
            # P34: every active is pinned; renormalize the current book so the
            # returned allocation is fully invested rather than leaving cash drag.
            return {sid: round(v / total, 6) for sid, v in w.items()}
        if not (movable_sum > 1e-14):
            if movable_sum < -1e-14:
                # Net-short movable cannot cover a positive target; zero them out
                # and let the loop re-clip / redistribute on the next iteration.
                w = {
                    **pinned_high,
                    **pinned_low,
                    **{sid: 0.0 for sid in movable},
                    **{sid: 0.0 for sid in w if sid not in pinned_high and sid not in pinned_low and sid not in movable},
                }
                continue
            # movable_sum ≈ 0: true degeneracy — renormalize (P34 fallback).
            return {sid: round(v / total, 6) for sid, v in w.items()}
        scale = target_movable / movable_sum
        w = {
            **pinned_high,
            **pinned_low,  # R5/QT-2: preserve short-pinned legs through the rebuild
            **{sid: v * scale for sid, v in movable.items()},
            # Zero-input strategies stay at 0 (never water-filled).
            **{sid: 0.0 for sid in w if sid not in pinned_high and sid not in pinned_low and sid not in movable},
        }
    return {sid: round(v, 6) for sid, v in w.items()}


def combine_extended(
    *,
    strategy_ids: Sequence[int],
    method: str,
    constraints: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Extended combine: standard payload + MRC + DR + realized vol + components."""
    constraints = constraints or {}
    max_w = float(constraints.get("max_weight", 0.30))
    min_w = float(constraints.get("min_weight", 0.0))
    # P-PO-VAL: guard the operator-facing weight bounds. Without this a negative
    # max_weight yields a net-short book despite allow_short=False, max_weight>1
    # makes the cap inert, and min_weight>max_weight is nonsensical. (When called
    # via the API these are pre-validated by _PoConstraints; this guard covers
    # any direct/dict caller so the contract holds regardless of entry point.)
    if not (max_w == max_w) or max_w in (float("inf"), float("-inf")):
        raise ValueError("max_weight must be a finite number")
    if not (min_w == min_w) or min_w in (float("inf"), float("-inf")):
        raise ValueError("min_weight must be a finite number")
    if not (0.0 < max_w <= 1.0):
        raise ValueError("max_weight must be in (0, 1]")
    if not (0.0 <= min_w <= max_w):
        raise ValueError("min_weight must be in [0, max_weight]")
    allow_short = bool(constraints.get("allow_short", False))
    vol_target_annual = float(constraints.get("vol_target_annual", 0.15))

    base = combine_portfolio(list(strategy_ids), method=method)

    # Apply constraints to weights and re-run if changed.
    raw_w: Dict[int, float] = {int(k): float(v) for k, v in (base.get("weights") or {}).items()}
    capped_w = _apply_constraints(
        raw_w, max_weight=max_w, min_weight=min_w, allow_short=allow_short
    )

    # Pull aligned returns to compute MRC / DR / realized vol.
    series_map: Dict[int, pd.Series] = {}
    for sid in strategy_ids:
        eq = _load_equity_series(int(sid))
        if eq.empty:
            continue
        series_map[int(sid)] = eq
    if not series_map:
        return {
            **base,
            "weights": capped_w,
            "marginal_risk_contribution": {},
            "diversification_ratio": None,
            "realized_vol_annual": None,
            "vol_target_hit": False,
            "constraints": constraints,
            "components": [],
        }

    eq_df = pd.concat(series_map, axis=1).dropna(how="any")
    ret_df = eq_df.pct_change().dropna().replace([np.inf, -np.inf], np.nan).fillna(0.0)
    ret_df.columns = [int(c) for c in ret_df.columns]

    mrc = marginal_risk_contribution(capped_w, ret_df)
    dr = diversification_ratio(capped_w, ret_df)
    real_vol = portfolio_realized_vol(capped_w, ret_df, annualize=True)
    vol_hit = real_vol is not None and abs(real_vol - vol_target_annual) < 0.02

    # Component per-strategy stats.
    components: List[Dict[str, Any]] = []
    for sid in ret_df.columns:
        s_ret = ret_df[sid]
        # P30-R3: 252 (US equity) → 365 (crypto, 24/7)
        sharpe = (
            (s_ret.mean() / s_ret.std(ddof=1) * math.sqrt(365))
            if s_ret.std(ddof=1) > 1e-14
            else 0.0
        )
        components.append({
            "id": int(sid),
            "weight": round(float(capped_w.get(int(sid), 0.0)), 6),
            "mrc_pct": round(float(mrc.get(int(sid), 0.0)), 6),
            "sharpe": round(float(sharpe), 4),
            # P30-R3: 252 (US equity) → 365 (crypto, 24/7)
            "vol": round(float(s_ret.std(ddof=1) * math.sqrt(365)), 6),
        })

    # P-FIX — recompute equity_curve + metrics from the CONSTRAINED weights
    # so the headline Sharpe / Max DD / equity chart reflect the same book
    # the UI shows in the Weights pie. Mirrors combine_portfolio()'s exact
    # math (portfolio.py:307-321) over the aligned, inf-cleaned ret_df above.
    capped_vec = np.array([float(capped_w.get(int(c), 0.0)) for c in ret_df.columns])
    if capped_vec.sum() > 1e-14:
        capped_net = pd.Series(
            ret_df.to_numpy() @ capped_vec, index=ret_df.index, name="combined_return"
        )
        capped_equity = (
            (1.0 + capped_net).cumprod().replace([np.inf, -np.inf], np.nan).ffill().fillna(1.0)
        )
        capped_drawdown = (capped_equity / capped_equity.cummax().clip(lower=1.0)) - 1.0
        capped_metrics = _combined_metrics(capped_net, capped_equity, capped_drawdown)
        capped_curve: List[Dict[str, Any]] = []
        for ts, eq in capped_equity.items():
            capped_curve.append({
                "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(ts, "strftime") else str(ts),
                "equity": round(float(eq), 6),
                "drawdown": round(
                    float(capped_drawdown.loc[ts]) if ts in capped_drawdown.index else 0.0, 6
                ),
            })
    else:
        # Constraints zeroed the entire book (e.g. infeasible water-fill).
        # Surface an empty curve + zeroed metrics rather than the stale
        # unconstrained values from base.
        capped_metrics = _combined_metrics(
            pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float)
        )
        capped_curve = []

    return {
        **base,
        "weights": capped_w,
        "equity_curve": capped_curve,
        "metrics": capped_metrics,
        "marginal_risk_contribution": mrc,
        "diversification_ratio": dr,
        "realized_vol_annual": real_vol,
        "vol_target_annual": vol_target_annual,
        "vol_target_hit": bool(vol_hit),
        "constraints": constraints,
        "components": components,
    }


def efficient_frontier(
    *,
    strategy_ids: Sequence[int],
    vol_min: float = 0.05,
    vol_max: float = 0.30,
    steps: int = 10,
) -> Dict[str, Any]:
    """Sweep ``vol_target_annual`` across N steps. Returns frontier points."""
    sids = [int(s) for s in strategy_ids if s is not None]
    if len(sids) < 2:
        raise ValueError("efficient frontier requires >= 2 strategies")
    steps = max(2, min(int(steps), 20))
    series_map: Dict[int, pd.Series] = {}
    for sid in sids:
        eq = _load_equity_series(sid)
        if eq.empty:
            continue
        series_map[sid] = eq
    if len(series_map) < 2:
        return {"points": [], "steps": steps, "missing": [s for s in sids if s not in series_map]}
    eq_df = pd.concat(series_map, axis=1).dropna(how="any")
    # P24 — mirror the ``combine_extended`` pattern at line 116. Without the
    # inf-replacement, an equity series that touches zero (catastrophic loss
    # / data glitch) produces ±inf rows from pct_change(), which then
    # propagate through ``vol_target``, ``mean()``, ``std()`` and the
    # ``(1.0 + port_ret).prod()`` annualization below — silently corrupting
    # every frontier point on the sweep with NaN/inf Sharpe and return.
    ret_df = eq_df.pct_change().dropna().replace([np.inf, -np.inf], np.nan).fillna(0.0)
    ret_df.columns = [int(c) for c in ret_df.columns]

    points: List[Dict[str, Any]] = []
    grid = np.linspace(float(vol_min), float(vol_max), steps)
    for vt in grid:
        # vol_target() returns a UNIT-SUM inverse-vol direction; the vt factor
        # cancels in _safe_normalize, so the *direction* is identical for every
        # vt. The frontier sweep is meaningful only if we apply the implicit
        # leverage vol_target is meant to encode: scale the unit-sum direction
        # so the portfolio's gross annualized vol hits vt. (Additive: does not
        # change vol_target's public unit-sum contract; reported `weights`
        # below remain the unit-sum direction.)
        weights = vol_target(ret_df, vol_target_annual=float(vt))
        base_ret = (ret_df * pd.Series(weights)).sum(axis=1)
        # P30-R3: 365 (crypto 24/7). Guard subnormals per project rule.
        base_vol = base_ret.std(ddof=1) * math.sqrt(365)
        if not (base_vol > 1e-14):
            # Degenerate direction (zero-variance / single-survivor): no
            # meaningful leverage target. Skip this point rather than emit a
            # misleading dot.
            continue
        leverage = float(vt) / base_vol
        port_ret = base_ret * leverage
        # P30-R3: 252 (US equity) → 365 (crypto, 24/7)
        port_vol = (
            port_ret.std(ddof=1) * math.sqrt(365)
            if port_ret.std(ddof=1) > 1e-14
            else None
        )
        # P30-R3: 252 (US equity) → 365 (crypto, 24/7)
        sharpe = (
            (port_ret.mean() / port_ret.std(ddof=1) * math.sqrt(365))
            if port_ret.std(ddof=1) > 1e-14
            else 0.0
        )
        # P29-S2: guard (a) ZeroDivisionError when len==0, and (b) complex
        # result when (1+port_ret).prod() < 0 (negative base, fractional
        # exponent maps onto complex plane). Coerce to float, clamp the
        # base to a positive epsilon so the frontier point reports a real
        # ann_ret rather than a complex(...) that JSON serialization chokes on.
        if len(port_ret) > 0:
            _prod = float((1.0 + port_ret).prod())
            # Use calendar-day span (mirrors _combined_metrics in portfolio.py)
            # so that multi-day gaps in the aligned eq_df do not inflate the
            # annualisation exponent (bar-count < actual calendar days => 365/bars
            # is too large, overstating ann_ret for every frontier dot).
            if isinstance(port_ret.index, pd.DatetimeIndex) and len(port_ret) > 1:
                years = (port_ret.index[-1] - port_ret.index[0]).days / 365.0
            else:
                years = len(port_ret) / 365.0
            if _prod > 1e-14 and years > 1e-14:
                ann_ret = _prod ** (1.0 / years) - 1.0
            else:
                ann_ret = -1.0
        else:
            ann_ret = 0.0
        points.append({
            "vol_target": round(float(vt), 4),
            "realized_vol": round(port_vol, 6) if port_vol is not None else None,
            "expected_return": round(float(ann_ret), 6),
            "sharpe": round(float(sharpe), 4),
            # weights are the unit-sum direction from vol_target(); multiply
            # each weight by `leverage` to get the actual gross exposure
            # required to hit the vol target. Both are emitted so callers
            # can display direction weights and simultaneously warn the
            # operator when leverage > 1.0 (e.g. grey out or annotate dots).
            "weights": {int(k): round(float(v), 6) for k, v in weights.items()},
            "leverage": round(float(leverage), 6),
        })
    return {"points": points, "steps": steps, "frontier_type": "leverage_sweep"}


# ---- Persistence -----------------------------------------------------------


def save_portfolio(
    session: Session,
    *,
    name: str,
    strategy_ids: Sequence[int],
    weights: Dict[int, float],
    method: str,
    constraints: Optional[Dict[str, Any]] = None,
) -> Portfolio:
    """Persist a named combination. Auto-suffix on name conflict."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name required")
    final_name = name
    for suffix in range(2, 102):
        existing = session.query(Portfolio).filter(Portfolio.name == final_name).first()
        if existing is None:
            break
        final_name = f"{name} ({suffix})"
    else:
        # All 100 candidate names (original + suffixes (2)..(101)) are taken.
        # Raise explicitly rather than letting session.flush() hit the DB
        # unique constraint and surface an opaque IntegrityError.
        raise ValueError(
            f"Cannot save portfolio: all name variants of {name!r} up to suffix 101 are already taken."
        )
    row = Portfolio(
        name=final_name,
        strategy_ids_json=json.dumps([int(s) for s in strategy_ids], default=str),
        weights_json=json.dumps({str(int(k)): float(v) for k, v in weights.items()}, default=str),
        method=method,
        constraints_json=json.dumps(constraints or {}, default=str),
    )
    session.add(row)
    session.flush()
    return row


def list_saved(session: Session) -> Dict[str, Any]:
    rows = session.query(Portfolio).order_by(Portfolio.created_at.desc()).all()
    # Enrich with "stale" flag: any underlying strategy no longer APPROVED+.
    active_ids: set = {
        int(r[0]) for r in session.query(AlphaStrategy.id).filter(
            AlphaStrategy.status.in_(("APPROVED", "PAPER_TRADE", "SMALL_CAPITAL", "LIVE"))
        ).all()
    }
    enriched: List[Dict[str, Any]] = []
    for p in rows:
        d = p.to_dict()
        sids = d.get("strategy_ids") or []
        d["stale"] = any(int(s) not in active_ids for s in sids)
        enriched.append(d)
    return {"portfolios": enriched}


def delete_saved(session: Session, portfolio_id: int) -> bool:
    row = session.get(Portfolio, int(portfolio_id))
    if row is None:
        return False
    session.delete(row)
    return True


__all__ = [
    "combine_extended",
    "efficient_frontier",
    "save_portfolio",
    "list_saved",
    "delete_saved",
]

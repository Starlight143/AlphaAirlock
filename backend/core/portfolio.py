"""Portfolio Allocator + Combined-Portfolio Backtest (P0 + P2).

P0 — Inverse-volatility allocator with epsilon floor (kept for backwards
compat with the existing /api/portfolio/allocate endpoint).

P2 — `combine_portfolio()` runs ANY of the 8 weighting methods registered in
`backend.core.allocators` and returns:
  - the chosen weights per strategy
  - the combined equity curve (daily-aligned)
  - aggregate metrics (Sharpe, MaxDD, Sortino, etc.)
plus per-leg contribution series so the UI can stack the components.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from backend.core.allocators import ALLOCATORS, METHOD_KEYS, METHOD_LABELS, allocate
from backend.core.database import AlphaStrategy, PROJECT_ROOT, session_scope

logger = logging.getLogger("alpha.portfolio")

EPSILON: float = 1e-6
MIN_TRADES: int = 3
RESULTS_DIR: Path = PROJECT_ROOT / "storage" / "results"


def _load_equity_curve(strategy_id: int) -> List[float]:
    """Pull the daily equity series (list of floats) for a strategy from disk."""
    p = RESULTS_DIR / f"strategy_{strategy_id}.json"
    if not p.exists():
        return []
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        return [float(pt.get("equity", 1.0)) for pt in payload.get("equity_curve", [])]
    except Exception:
        return []


def _daily_returns(equity: List[float]) -> List[float]:
    out: List[float] = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        cur = equity[i]
        if prev is None or not math.isfinite(prev) or abs(prev) < 1e-14:
            continue
        out.append((cur / prev) - 1.0)
    return out


def _trade_count(row: AlphaStrategy) -> int:
    cfg = row.config()
    cand = cfg.get("trades", None)
    if cand is None:
        cand = (row.metrics() or {}).get("trades", 0)
    try:
        return int(cand)
    except (TypeError, ValueError):
        return 0


def allocate_portfolio_weights(strategy_ids: List[int]) -> Dict[int, float]:
    """Return {strategy_id: weight} with epsilon-safe inverse-volatility weights.

    - Strategies missing equity data or with fewer than MIN_TRADES trades
      receive 0% weight.
    - Among survivors: weight_i = 1 / (sigma_i + EPSILON), normalized so the
      sum of survivor weights = 1.0.
    - All requested ids appear in the returned dict (possibly with 0.0).
    """
    if not strategy_ids:
        return {}

    weights: Dict[int, float] = {int(sid): 0.0 for sid in strategy_ids}

    raw_inverse: Dict[int, float] = {}
    with session_scope() as s:
        for sid in strategy_ids:
            row = s.get(AlphaStrategy, int(sid))
            if row is None:
                continue
            trades = _trade_count(row)
            if trades < MIN_TRADES:
                continue
            equity = _load_equity_curve(int(sid))
            returns = _daily_returns(equity)
            if len(returns) < 2:
                continue
            sigma = float(np.std(np.asarray(returns, dtype=np.float64), ddof=1))
            if not math.isfinite(sigma) or sigma < 0:
                sigma = 0.0
            raw_inverse[int(sid)] = 1.0 / (sigma + EPSILON)

    total = sum(raw_inverse.values())
    if total <= 0 or not math.isfinite(total):
        return weights  # all zeros — nothing survived

    for sid, inv in raw_inverse.items():
        weights[sid] = round(inv / total, 6)

    # Re-normalize to absorb rounding drift so the sum equals exactly 1.0.
    rounded_total = sum(weights.values())
    if rounded_total > 1e-14 and abs(rounded_total - 1.0) > 1e-9:
        scale = 1.0 / rounded_total
        for sid in raw_inverse:
            weights[sid] = round(weights[sid] * scale, 6)
    # Per-weight round(.,6) cannot guarantee the survivors sum to exactly 1.0
    # (errors accumulate up to n*5e-7). Absorb the final 6-dp remainder onto
    # the largest survivor so the surviving weights sum to 1.0 within ~1e-9.
    residual = round(1.0 - sum(weights.values()), 6)
    if raw_inverse and abs(residual) > 0:
        kmax = max(raw_inverse, key=lambda k: weights[k])
        weights[kmax] = round(weights[kmax] + residual, 6)
    return weights


# ---------------------------------------------------------------------------
# P2 — combined portfolio backtest
# ---------------------------------------------------------------------------


def _load_equity_series(strategy_id: int) -> pd.Series:
    """Load a strategy's equity curve as a DatetimeIndex pd.Series of floats."""
    p = RESULTS_DIR / f"strategy_{strategy_id}.json"
    if not p.exists():
        return pd.Series(dtype=float)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not parse %s: %s", p, exc)
        return pd.Series(dtype=float)
    pts = payload.get("equity_curve", [])
    if not pts:
        return pd.Series(dtype=float)
    idx: List[datetime] = []
    vals: List[float] = []
    for row in pts:
        ts = pd.to_datetime(row.get("timestamp"), utc=True, errors="coerce")
        if pd.isna(ts):
            continue
        try:
            v = float(row.get("equity", 1.0))
        except (TypeError, ValueError):
            continue
        idx.append(ts)
        vals.append(v)
    if not idx:
        return pd.Series(dtype=float)
    series = pd.Series(vals, index=pd.DatetimeIndex(idx)).sort_index()
    # Guard: if the median timestep is sub-daily (e.g. hourly bars stored
    # by a non-standard writer), resample to daily so annualisation with
    # sqrt(365) in _combined_metrics remains consistent with the daily
    # resolution that engine._daily_aligned always produces.
    if len(series) >= 2:
        median_step = pd.Series(series.index).diff().dropna().median()
        if pd.notna(median_step) and median_step < pd.Timedelta(hours=20):
            logger.warning(
                "strategy_%s equity_curve appears sub-daily (median step %s); "
                "resampling to 1D to preserve annualisation consistency.",
                strategy_id, median_step,
            )
            series = series.resample("1D").last().dropna()
    return series


def _equity_to_returns(series: pd.Series) -> pd.Series:
    if series.empty:
        return series
    rets = series.pct_change().dropna()
    return rets.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def _combined_metrics(net: pd.Series, equity: pd.Series, drawdown: pd.Series) -> Dict[str, float]:
    if equity.empty or net.empty:
        return {
            "annualized_sharpe": 0.0,
            "annualized_return": 0.0,
            "cumulative_return": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "sortino": 0.0,
        }
    # P30-R3: 252 (US equity) → 365 (crypto, 24/7)
    sqrt252 = math.sqrt(365)
    mean = float(net.mean())
    std = float(net.std(ddof=1)) if len(net) > 1 else 0.0
    sharpe = (mean / std * sqrt252) if std > 1e-14 else 0.0
    cumulative = float(equity.iloc[-1] - 1.0)
    # P30-R3: 252 (US equity) → 365 (crypto, 24/7).
    # Use actual calendar-day span from the DatetimeIndex rather than the
    # point count so that data gaps (exchange downtime, non-overlapping
    # strategy windows) do not inflate the annualisation exponent.
    if isinstance(equity.index, pd.DatetimeIndex) and len(equity) >= 2:
        years = (equity.index[-1] - equity.index[0]).days / 365.0
    else:
        n_days = len(equity)
        years = n_days / 365.0 if n_days > 0 else 0.0
    # P29-S3: tighten subnormal-permissive ``> 0`` to 1e-14 floor so
    # near-total-loss cumulative returns annualise to 0.0 rather than a
    # garbage-magnitude number that pollutes the metrics payload.
    annualized = (
        (1.0 + cumulative) ** (1.0 / years) - 1.0
    ) if years > 0.25 and (1.0 + cumulative) > 1e-14 else 0.0
    wins = net[net > 0]
    losses = net[net < 0]
    win_rate = (len(wins) / len(net[net.abs() > 1e-12])) if len(net[net.abs() > 1e-12]) else 0.0
    gp = float(wins.sum())
    gl = float(-losses.sum())
    profit_factor = (gp / gl) if gl > 1e-14 else (999.0 if gp > 0 else 0.0)
    # P29-T4 / P31-R9: Sortino vs MAR=0. Canonical downside deviation is
    # sqrt(mean(min(0, r_i)^2)) over the FULL sample (positives count as 0
    # in the squared-shortfall, not excluded). The previous implementation
    # took sample stddev of the negatives-only subset, which (a) lost the
    # zeros that should drag the denominator down and (b) divided a
    # full-sample mean by a subsample stddev, breaking the unit ratio.
    if len(net) > 0:
        shortfall_sq = (-net.clip(upper=0.0)).pow(2)
        downside_dev_daily = math.sqrt(float(shortfall_sq.mean()))
    else:
        downside_dev_daily = 0.0
    # Annualise the daily downside deviation by sqrt(365) (crypto 24/7).
    downside_std = downside_dev_daily * sqrt252
    sortino = (mean * 365.0) / downside_std if downside_std > 1e-14 else 0.0
    if not math.isfinite(sortino):
        sortino = 0.0
    return {
        "annualized_sharpe": round(sharpe, 4),
        "annualized_return": round(annualized, 6),
        "cumulative_return": round(cumulative, 6),
        "max_drawdown": round(float(drawdown.min()) if not drawdown.empty else 0.0, 6),
        "win_rate": round(win_rate, 6),
        "profit_factor": round(profit_factor, 4),
        "sortino": round(sortino, 4),
    }


def combine_portfolio(
    strategy_ids: List[int],
    method: str = "equal_weight",
) -> Dict[str, Any]:
    """Run the requested allocator across `strategy_ids` and return the
    combined equity / drawdown / metrics payload used by /backtest-panel.

    Output schema:
        {
          "method": "equal_weight" | ...,
          "method_label": "Even (EW)" | ...,
          "weights": {sid: weight, ...},
          "equity_curve": [{timestamp, equity, drawdown}, ...],
          "metrics": {sharpe, return, dd, ...},
          "n_strategies": int,
          "n_aligned_days": int,
          "missing": [sid for which no equity curve was found],
        }
    """
    sids = [int(s) for s in strategy_ids if s is not None]
    if not sids:
        return {
            "method": method,
            "method_label": METHOD_LABELS.get(method, method),
            "weights": {},
            "equity_curve": [],
            "metrics": _combined_metrics(pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float)),
            "n_strategies": 0,
            "n_aligned_days": 0,
            "missing": [],
        }

    # Load + align equity curves on the intersection of dates.
    equities: Dict[int, pd.Series] = {}
    missing: List[int] = []
    for sid in sids:
        series = _load_equity_series(sid)
        if series.empty:
            missing.append(sid)
            continue
        equities[sid] = series

    if not equities:
        return {
            "method": method,
            "method_label": METHOD_LABELS.get(method, method),
            "weights": {sid: 0.0 for sid in sids},
            "equity_curve": [],
            "metrics": _combined_metrics(pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float)),
            "n_strategies": 0,
            "n_aligned_days": 0,
            "missing": missing,
        }

    eq_df = pd.concat(equities, axis=1).dropna(how="any")
    if eq_df.empty:
        return {
            "method": method,
            "method_label": METHOD_LABELS.get(method, method),
            "weights": {sid: 0.0 for sid in sids},
            "equity_curve": [],
            "metrics": _combined_metrics(pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float)),
            "n_strategies": len(equities),
            "n_aligned_days": 0,
            "missing": missing,
        }

    # Each column is a strategy's equity series; derive returns.
    ret_df = eq_df.pct_change().dropna(how="any").replace([np.inf, -np.inf], np.nan).fillna(0.0)
    # Ensure column types are int for allocator dispatch.
    ret_df.columns = [int(c) for c in ret_df.columns]

    weights = allocate(method, ret_df)
    # Restrict to the columns actually in ret_df.
    w_vec = np.array([float(weights.get(int(c), 0.0)) for c in ret_df.columns])

    if w_vec.sum() <= 1e-14:
        # Nothing got weight — return an empty payload but preserve weights map.
        return {
            "method": method,
            "method_label": METHOD_LABELS.get(method, method),
            "weights": {int(sid): float(weights.get(int(sid), 0.0)) for sid in sids},
            "equity_curve": [],
            "metrics": _combined_metrics(pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float)),
            "n_strategies": len(equities),
            "n_aligned_days": int(len(ret_df)),
            "missing": missing,
        }

    net = pd.Series(ret_df.to_numpy() @ w_vec, index=ret_df.index, name="combined_return")
    equity = (1.0 + net).cumprod().replace([np.inf, -np.inf], np.nan).ffill().fillna(1.0)
    # P32-NUM32-3 (matches P31-R3): clamp cummax floor to 1.0 so a strategy
    # that opens below peg (equity[0] < 1.0) does not produce a phantom
    # positive drawdown denominator that swallows the true MDD signal.
    drawdown = (equity / equity.cummax().clip(lower=1.0)) - 1.0

    metrics = _combined_metrics(net, equity, drawdown)
    curve = []
    for ts, eq in equity.items():
        curve.append({
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(ts, "strftime") else str(ts),
            "equity": round(float(eq), 6),
            "drawdown": round(float(drawdown.loc[ts]) if ts in drawdown.index else 0.0, 6),
        })

    # Backfill 0% weights for missing strategies for downstream display.
    final_weights = {int(sid): float(weights.get(int(sid), 0.0)) for sid in sids}
    for sid in missing:
        final_weights.setdefault(int(sid), 0.0)

    return {
        "method": method,
        "method_label": METHOD_LABELS.get(method, method),
        "weights": final_weights,
        "equity_curve": curve,
        "metrics": metrics,
        "n_strategies": len(equities),
        "n_aligned_days": int(len(ret_df)),
        "missing": missing,
    }


__all__ = [
    "allocate_portfolio_weights",
    "combine_portfolio",
    "EPSILON",
    "MIN_TRADES",
    "METHOD_KEYS",
    "METHOD_LABELS",
]

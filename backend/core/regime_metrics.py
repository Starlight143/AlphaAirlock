"""T1-D — Sub-period / regime stability metrics for backtest honesty.

The engine reports a single full-sample Sharpe. A strategy whose entire edge
comes from ONE lucky window (e.g. a single 2024 trend leg) can show a high
full-sample Sharpe while being worthless out-of-window. This module slices the
engine's per-bar NET return tape into contiguous sub-periods (and a coarse
high/low-vol split) and measures how *stable* the risk-adjusted return is across
them — the missing leg of the DS-STAR decomposition (signal exists → survives
costs → **not regime-fragile** → diversifying).

All functions are pure, NaN/Inf-guarded (denominators use the project's
``not (x > 1e-14)`` rule), dependency-light, and annualize with the engine's
hourly ``HOURS_PER_YEAR`` so every sub-period Sharpe is directly comparable to
the engine's number. Designed for the multi-asset *equity* path too: ~80% of
equity bars are flat-filled, so windows that are entirely flat are SKIPPED (not
scored as 0, which would falsely tank the positive-fraction).
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# Mirror engine.py / risk_metrics.py so annualization is identical.
HOURS_PER_YEAR: int = 24 * 365  # 8760

# Whole-series floor: below this the sub-period split is noise → emit nothing.
REGIME_MIN_BARS: int = 90
# Per-window floor (mirrors the engine's >=30-bar Sharpe floor).
REGIME_MIN_SUBWINDOW_BARS: int = 30


def _clean(returns: Sequence[float]) -> np.ndarray:
    """Coerce to a finite 1-D float array (drop NaN/Inf). Mirrors risk_metrics."""
    arr = np.asarray(list(returns) if not isinstance(returns, np.ndarray) else returns,
                     dtype=float)
    if arr.ndim != 1:
        arr = arr.reshape(-1)
    return arr[np.isfinite(arr)]


def _subperiod_sharpe(returns: np.ndarray,
                      annualization: float = HOURS_PER_YEAR) -> Optional[float]:
    """Annualized Sharpe for ONE window, engine-identical: mean/std(ddof=1) ×
    sqrt(annualization). None when the window is too short or std is degenerate
    (a fully flat-filled run) — such windows are SKIPPED, never scored as 0."""
    nr = returns[np.isfinite(returns)]
    if nr.size < REGIME_MIN_SUBWINDOW_BARS:
        return None
    std = float(nr.std(ddof=1))
    if not (std > 1e-14):
        return None
    sharpe_ann = (float(nr.mean()) / std) * math.sqrt(annualization)
    return sharpe_ann if math.isfinite(sharpe_ann) else None


def equal_split_indices(n: int, n_windows: int) -> List[Tuple[int, int]]:
    """Contiguous near-equal ``[start, end)`` slices covering ``[0, n)``. The
    remainder is spread across the leading windows so no window is starved.
    Returns ``[]`` for non-positive inputs."""
    if n < 1 or n_windows < 1:
        return []
    n_windows = min(n_windows, n)
    base, rem = divmod(n, n_windows)
    out: List[Tuple[int, int]] = []
    start = 0
    for i in range(n_windows):
        size = base + (1 if i < rem else 0)
        out.append((start, start + size))
        start += size
    return out


def compute_subperiod_stability(returns: Sequence[float], *,
                                n_windows: int = 6,
                                annualization: float = HOURS_PER_YEAR) -> Dict[str, Any]:
    """Slice into ``n_windows`` contiguous sub-periods and measure stability of
    the per-window annualized Sharpe. Windows that fail the bar/std floor are
    skipped. Returns ``{}`` when usable bars < ``REGIME_MIN_BARS``.

    Keys: regime_n_windows, regime_positive_fraction, regime_worst_sharpe,
    regime_best_sharpe, regime_sharpe_dispersion, regime_mean_sharpe.
    """
    r = _clean(returns)
    if r.size < REGIME_MIN_BARS:
        return {}
    sharpes: List[float] = []
    for start, end in equal_split_indices(r.size, n_windows):
        s = _subperiod_sharpe(r[start:end], annualization)
        if s is not None:
            sharpes.append(s)
    if not sharpes:
        return {}
    arr = np.asarray(sharpes, dtype=float)
    n_eval = int(arr.size)
    positive_fraction = float((arr > 0.0).sum()) / max(1, n_eval)
    dispersion = float(arr.std(ddof=0)) if n_eval >= 2 else 0.0
    return {
        "regime_n_windows": n_eval,
        "regime_positive_fraction": round(positive_fraction, 4),
        "regime_worst_sharpe": round(float(arr.min()), 4),
        "regime_best_sharpe": round(float(arr.max()), 4),
        "regime_sharpe_dispersion": round(dispersion, 4),
        "regime_mean_sharpe": round(float(arr.mean()), 4),
    }


def compute_vol_regime_stability(returns: Sequence[float], *,
                                 annualization: float = HOURS_PER_YEAR) -> Dict[str, Any]:
    """Coarse 2nd axis: split bars by the median of NONZERO-return magnitude into
    high-vol / low-vol buckets and annualized-Sharpe each. The median is taken
    over ``|r| > 1e-12`` bars only so the ~80% flat equity grid cannot collapse
    it to 0. Omits a bucket (or both keys) when it lacks enough nonzero bars."""
    r = _clean(returns)
    nonzero = r[np.abs(r) > 1e-12]
    if nonzero.size < 2 * REGIME_MIN_SUBWINDOW_BARS:
        return {}
    med = float(np.median(np.abs(nonzero)))
    if not (med > 1e-14):
        return {}
    mask_hi = np.abs(r) >= med
    out: Dict[str, Any] = {}
    lo_sharpe = _subperiod_sharpe(r[~mask_hi], annualization)
    hi_sharpe = _subperiod_sharpe(r[mask_hi], annualization)
    if lo_sharpe is not None:
        out["regime_lowvol_sharpe"] = round(lo_sharpe, 4)
    if hi_sharpe is not None:
        out["regime_highvol_sharpe"] = round(hi_sharpe, 4)
    return out


def compute_from_per_bar(per_bar: Optional[List[Dict[str, Any]]], *,
                         n_windows: int = 6,
                         annualization: float = HOURS_PER_YEAR) -> Dict[str, Any]:
    """Convenience wrapper mirroring risk_metrics.compute_from_per_bar: pull the
    NET per-bar returns (``pnl_pct``) and merge sub-period + vol-regime stability.
    Empty/invalid input yields ``{}`` (never raises)."""
    if not per_bar:
        return {}
    try:
        rets = [row.get("pnl_pct") for row in per_bar if isinstance(row, dict)]
    except Exception:  # noqa: BLE001
        return {}
    rets = [float(x) for x in rets if x is not None]
    if len(rets) < REGIME_MIN_BARS:
        return {}
    out = compute_subperiod_stability(rets, n_windows=n_windows, annualization=annualization)
    if not out:
        return {}
    out.update(compute_vol_regime_stability(rets, annualization=annualization))
    return out


__all__ = [
    "HOURS_PER_YEAR",
    "REGIME_MIN_BARS",
    "REGIME_MIN_SUBWINDOW_BARS",
    "equal_split_indices",
    "compute_subperiod_stability",
    "compute_vol_regime_stability",
    "compute_from_per_bar",
]

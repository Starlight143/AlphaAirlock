"""Market regime classifier (P7 — IR Explorer + Alpha Flow decorations).

Classifies each day in BTC's price history as ``bull`` / ``bear`` / ``range``
using a simple lookback-period return threshold.  Used by:

* :mod:`backend.core.ir_explorer` — slice book IR by regime
* :mod:`backend.app.routers.alpha_flow` (future) — colour Sankey edges by regime

Why kept separate from ``engine.py``:
The backtester operates on a single deterministic price series; the regime
classifier is an interpretive overlay used by the analytics layer.  Keeping it
isolated means the engine has zero coupling to "interpretive" concepts and the
regime thresholds can evolve without touching simulation code.

Defaults intentionally conservative:

* ``lookback_days = 30`` — short enough to react within a quarter, long enough
  to avoid whipsaw from a single news-day rally.
* ``bull_threshold = +0.05`` (5% / 30d)
* ``bear_threshold = -0.05`` (-5% / 30d)

Override via ``REGIME_LOOKBACK_DAYS`` / ``REGIME_BULL_PCT`` / ``REGIME_BEAR_PCT``
env vars (parsed via :mod:`backend._envloader`).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from backend._envloader import env_float, env_int
from backend.core.database import PROJECT_ROOT

logger = logging.getLogger("alpha.regime")

_DEFAULT_CSV = PROJECT_ROOT / "backend" / "data" / "synthetic_btc.csv"

REGIME_BULL = "bull"
REGIME_BEAR = "bear"
REGIME_RANGE = "range"
# P-FIX: distinct sentinel for bars with no defined return (first `lookback`
# bars + internal gaps). Kept OUT of REGIME_VALUES so analytics that iterate
# REGIME_VALUES (ir_explorer.by_regime) report only genuine regimes and do
# not conflate "undefined" with a real ranging market.
REGIME_UNKNOWN = "unknown"
REGIME_VALUES = (REGIME_BULL, REGIME_BEAR, REGIME_RANGE)


def _lookback() -> int:
    return env_int("REGIME_LOOKBACK_DAYS", 30, minimum=2, maximum=365)


def _bull_pct() -> float:
    return env_float("REGIME_BULL_PCT", 0.05, minimum=0.0, maximum=1.0)


def _bear_pct() -> float:
    return env_float("REGIME_BEAR_PCT", -0.05, minimum=-1.0, maximum=0.0)


def classify_regime_series(close: pd.Series, lookback_days: Optional[int] = None) -> pd.Series:
    """Per-day regime label aligned to ``close.index``.

    Bars with no defined ``lookback`` return (the first ``lookback`` rows and
    any internal gaps, where ``pct_change`` is NaN) are labelled
    ``REGIME_UNKNOWN`` rather than ``range`` so "undefined regime" is never
    conflated with a genuinely ranging market. The series still contains no
    NaN, so callers can do straight ``.value_counts()`` without dropna.
    """
    if close is None or close.empty:
        return pd.Series(dtype=object)
    lb = int(lookback_days or _lookback())
    if lb < 2:
        lb = 2
    ret = close.pct_change(lb)
    ret = ret.replace([np.inf, -np.inf], np.nan)  # B9-2: inf from zero-close bar -> REGIME_UNKNOWN
    out = pd.Series(REGIME_RANGE, index=close.index, dtype=object)
    bull_t = _bull_pct()
    bear_t = _bear_pct()
    out[ret > bull_t] = REGIME_BULL
    out[ret < bear_t] = REGIME_BEAR
    out[ret.isna()] = REGIME_UNKNOWN
    return out


def load_btc_daily(path: Optional[Path] = None) -> pd.Series:
    """Load BTC close as a daily-frequency Series.

    Returns empty Series when the CSV is missing — callers must check
    ``.empty`` and degrade gracefully.
    """
    csv = path or _DEFAULT_CSV
    if not csv.exists():
        logger.warning("regime: BTC CSV missing at %s — returning empty series", csv)
        return pd.Series(dtype=float)
    try:
        df = pd.read_csv(csv)
    except Exception:  # noqa: BLE001
        logger.exception("regime: failed to read %s", csv)
        return pd.Series(dtype=float)
    if "timestamp" not in df.columns or "close" not in df.columns:
        logger.warning("regime: CSV missing timestamp/close columns")
        return pd.Series(dtype=float)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp", "close"]).set_index("timestamp").sort_index()
    # P32-D4 / DAT32-5 — drop duplicate timestamps (keep latest revision)
    # so the daily resample below is deterministic for repeated bars.
    df = df[~df.index.duplicated(keep="last")]
    daily = df["close"].resample("1D").last().dropna()
    return daily.astype(float)


__all__ = [
    "REGIME_BULL",
    "REGIME_BEAR",
    "REGIME_RANGE",
    "REGIME_UNKNOWN",
    "REGIME_VALUES",
    "classify_regime_series",
    "load_btc_daily",
]

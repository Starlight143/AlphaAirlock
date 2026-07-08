"""T-BOOTSTRAP — Monte-Carlo robustness interval for the backtest Sharpe.

The reported annualized Sharpe is a single point estimate over ONE realised
path. Because the equity feature's bars are heavily autocorrelated (~80% flat-
filled), that point Sharpe is both inflated AND fragile. This module resamples
the realised per-bar net returns with a MOVING-BLOCK bootstrap — blocks preserve
the local autocorrelation an i.i.d. bootstrap would destroy — to build an
empirical distribution of the Sharpe, and reports:

  * a confidence interval (5th / 95th percentile of resampled Sharpe), and
  * P(Sharpe > 0): the empirical fraction of resamples with a positive Sharpe.

Complements risk_metrics' analytic PSR (Bailey & López de Prado): PSR assumes a
parametric form; this bootstrap is non-parametric and block-aware. All keys are
additive telemetry; nothing here gates by default.

Pure + deterministic — a fixed RNG seed means identical input ⇒ identical output,
so it is reproducible and unit-testable. Never raises: a failure returns ``{}``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend._envloader import env_bool, env_int

logger = logging.getLogger("alpha.bootstrap_metrics")

HOURS_PER_YEAR = 24 * 365  # 8760 — MUST match engine.py / regime_metrics.py
BOOTSTRAP_MIN_BARS = 100   # below this a resampled CI is meaningless
_ANNUALIZER = float(HOURS_PER_YEAR) ** 0.5
_SEED = 20260616           # fixed ⇒ deterministic / reproducible / testable


def is_enabled() -> bool:
    return env_bool("STRATEGY_BOOTSTRAP_ENABLED", True)


def _n_boot() -> int:
    return env_int("STRATEGY_BOOTSTRAP_N", 1000, minimum=100, maximum=20000)


def _block_len(n: int, override: int) -> int:
    if override > 0:
        return max(1, min(override, n))
    # Moving-block optimal length grows ~ n**(1/3) (Politis & White) — a cheap,
    # autocorrelation-aware default that needs no tuning.
    return max(1, min(n, int(round(n ** (1.0 / 3.0)))))


def _per_bar_returns(per_bar: Any) -> List[float]:
    if not per_bar or not isinstance(per_bar, (list, tuple)):
        return []
    out: List[float] = []
    for row in per_bar:
        if not isinstance(row, dict):
            continue
        v = row.get("pnl_pct")
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f:  # drop NaN (NaN != NaN)
            out.append(f)
    return out


def compute_from_per_bar(
    per_bar: Any,
    *,
    n_boot: Optional[int] = None,
    block: int = 0,
) -> Dict[str, Any]:
    """Moving-block bootstrap of the annualized Sharpe over the realised per-bar
    net returns. Returns ``bootstrap_*`` keys, or ``{}`` when disabled / too few
    bars / degenerate (constant) returns. Never raises."""
    try:
        if not is_enabled():
            return {}
        rets = _per_bar_returns(per_bar)
        n = len(rets)
        if n < BOOTSTRAP_MIN_BARS:
            return {}
        import numpy as np

        arr = np.asarray(rets, dtype="float64")
        # Degenerate (constant) returns → no dispersion to resample.
        if not (float(arr.std(ddof=1)) > 1e-14):
            return {}

        nb = int(n_boot if n_boot is not None else _n_boot())
        blk = _block_len(n, int(block))
        n_blocks = int(np.ceil(n / blk))
        rng = np.random.default_rng(_SEED)
        starts = rng.integers(0, n, size=(nb, n_blocks))  # block start indices
        offsets = np.arange(blk)
        sharpes = np.empty(nb, dtype="float64")
        # ponytail: per-resample loop (not a full (nb, n) gather) keeps peak
        # memory at O(n) instead of O(nb*n) — for nb=1000, n~17k that is ~140MB
        # avoided. Still <1s; vectorise only if profiling ever flags it.
        for i in range(nb):
            idx = (starts[i][:, None] + offsets[None, :]).reshape(-1)[:n] % n
            sample = arr[idx]
            sd = float(sample.std(ddof=1))
            sharpes[i] = 0.0 if not (sd > 1e-14) else (
                float(sample.mean()) / sd * _ANNUALIZER)

        return {
            "bootstrap_sharpe_ci_low": round(float(np.percentile(sharpes, 5.0)), 4),
            "bootstrap_sharpe_ci_high": round(float(np.percentile(sharpes, 95.0)), 4),
            "bootstrap_sharpe_median": round(float(np.percentile(sharpes, 50.0)), 4),
            "bootstrap_prob_sharpe_positive": round(float((sharpes > 0.0).mean()), 4),
            "bootstrap_n": int(nb),
            "bootstrap_block": int(blk),
        }
    except Exception:  # noqa: BLE001
        logger.exception("bootstrap metrics failed (non-fatal)")
        return {}


__all__ = ["compute_from_per_bar", "is_enabled", "HOURS_PER_YEAR", "BOOTSTRAP_MIN_BARS"]

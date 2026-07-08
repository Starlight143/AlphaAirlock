"""Portfolio weighting methods (P2).

P7 — adds three risk-decomposition helpers (``marginal_risk_contribution``,
``diversification_ratio``, ``portfolio_realized_vol``) for /portfolio-optimizer.



Eight allocators sharing one pure-function signature:

    fn(asset_returns: pd.DataFrame, *, vol_target_annual: float = 0.15) -> Dict[int, float]

`asset_returns` columns are strategy ids (int), rows are aligned per-period
returns (daily by default — caller decides). Output is `{id: weight}` summing
to 1.0 across columns that received nonzero weight.

Hard rules — all methods enforce these:
  - Strategies with std <= 1e-14 receive 0% weight (degenerate / no edge).
  - Any unsolvable / singular covariance falls back to equal weight across
    survivors with a logger.warning. Never raise.
  - Final weights are renormalized so survivors sum to 1.0 (rounding-safe).
"""

from __future__ import annotations

import logging
import math
from typing import Callable, Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("alpha.allocators")

# Annualization factor for daily returns. Matches AlphaBacktester's daily
# resample. Allocators are timeframe-agnostic in math, but vol_target uses
# this constant to translate "15% annual vol" → daily vol budget.
# P30-R3: 252 (US equity) → 365 (crypto, 24/7)
DAILY_TO_ANNUAL: float = math.sqrt(365)

# Public allocator method keys — order matches the reference UI dropdown.
# P6-M16: padded out to 11 methods to match the reference screenshot's
# WeightingPicker. The new entries (UMC / Use Target ETL / ERoC) are thin
# wrappers over existing solvers with different parameterisations rather
# than independent optimisations — keeps the math footprint tight while
# giving the UI the right number of options.
METHOD_KEYS: list[str] = [
    "equal_weight",       # EW (1/N)
    "inverse_vol",        # 1/sigma
    "risk_parity",        # equal risk contribution
    "mean_variance",      # max-Sharpe Markowitz
    "vol_target_15",      # scaled inverse-vol to 15% annual portfolio vol
    "umc",                # Uniform Marginal Contribution (≈ risk_parity baseline)
    "use_target_etl",     # ETL-targeted scaling (rescaled inverse_vol)
    "half_kelly",         # 0.5 * mu / sigma^2
    "min_variance",       # argmin w'Sigma w
    "eroc_es",            # Excess Return over Expected Shortfall ratio
    "cvar_5",             # 5% CVaR-aware
]

METHOD_LABELS: dict[str, str] = {
    "equal_weight": "Even (1/N)",
    "inverse_vol": "Inverse Vol",
    "risk_parity": "Risk Parity",
    "mean_variance": "Mean Variance",
    "vol_target_15": "Vol Target 15%",
    "umc": "UMC",
    "use_target_etl": "Use Target ETL",
    "half_kelly": "Half-Kelly",
    "min_variance": "Min Variance",
    "eroc_es": "ERoC (ES)",
    "cvar_5": "CVaR (5%)",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _safe_normalize(raw: Dict[int, float]) -> Dict[int, float]:
    """Sum-to-1 with epsilon-aware fallback."""
    total = sum(v for v in raw.values() if math.isfinite(v) and v > 0)
    if not (total > 1e-14):
        return {sid: 0.0 for sid in raw}
    out = {sid: max(0.0, v) / total for sid, v in raw.items()}
    # Final rounding-drift correction.
    s = sum(out.values())
    if s > 0 and abs(s - 1.0) > 1e-9:
        out = {sid: w / s for sid, w in out.items()}
    return {sid: round(w, 6) for sid, w in out.items()}


def _equal_weight_fallback(ids: list[int]) -> Dict[int, float]:
    if not ids:
        return {}
    w = 1.0 / len(ids)
    return {int(sid): round(w, 6) for sid in ids}


def _clean_returns(df: pd.DataFrame) -> pd.DataFrame:
    """Drop columns with no variance and impute NaNs to 0."""
    if df is None or df.empty:
        return df
    df = df.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    # Identify strategies with near-zero variance — they get 0 weight.
    stds = df.std(ddof=1)
    keep = stds.index[stds > 1e-14]
    return df[keep]


def _zero_weights_for(ids: list[int], surviving: list[int]) -> Dict[int, float]:
    """Pre-seed full output dict with 0.0 for non-survivors."""
    return {int(sid): 0.0 for sid in ids if int(sid) not in surviving}


# ---------------------------------------------------------------------------
# Allocators
# ---------------------------------------------------------------------------

def equal_weight(returns: pd.DataFrame, **_: object) -> Dict[int, float]:
    cleaned = _clean_returns(returns)
    survivors = list(cleaned.columns)
    if not survivors:
        return _equal_weight_fallback(list(returns.columns.astype(int))) if not returns.empty else {}
    raw = {int(sid): 1.0 for sid in survivors}
    out = _safe_normalize(raw)
    out.update(_zero_weights_for(list(returns.columns), survivors))
    return out


def inverse_vol(returns: pd.DataFrame, **_: object) -> Dict[int, float]:
    cleaned = _clean_returns(returns)
    survivors = list(cleaned.columns)
    if not survivors:
        return {int(sid): 0.0 for sid in returns.columns}
    sigma = cleaned.std(ddof=1)
    raw = {int(sid): 1.0 / max(float(sigma[sid]), 1e-14) for sid in survivors}
    out = _safe_normalize(raw)
    out.update(_zero_weights_for(list(returns.columns), survivors))
    return out


def risk_parity(returns: pd.DataFrame, **_: object) -> Dict[int, float]:
    """Equal risk-contribution (true RP via iterative renormalization).

    For uncorrelated assets RP collapses to inverse-vol. We initialize with
    inverse-vol then run 8 cycles of the standard equal-RC update:
        w_i_new ∝ w_i / (Sigma @ w)_i
    This converges quickly for our small portfolios.
    """
    cleaned = _clean_returns(returns)
    survivors = list(cleaned.columns)
    if not survivors:
        return {int(sid): 0.0 for sid in returns.columns}
    if len(survivors) == 1:
        out = {int(survivors[0]): 1.0}
        out.update(_zero_weights_for(list(returns.columns), survivors))
        return out

    cov = cleaned.cov().to_numpy()
    if not np.all(np.isfinite(cov)) or cov.shape[0] != len(survivors):
        logger.warning("risk_parity: bad covariance; falling back to equal weight")
        return equal_weight(returns)

    w = 1.0 / np.sqrt(np.diag(cov) + 1e-12)
    w = w / w.sum()
    for _ in range(8):
        mrc = cov @ w
        if not np.all(np.isfinite(mrc)):
            break
        rc = w * mrc
        if not np.all(np.isfinite(rc)) or np.any(np.abs(rc) < 1e-14):
            break
        target = rc.mean()
        w = w * (target / rc)
        w = np.maximum(w, 0.0)
        s = w.sum()
        if not (s > 1e-14):
            break
        w = w / s

    raw = {int(survivors[i]): float(w[i]) for i in range(len(survivors))}
    out = _safe_normalize(raw)
    out.update(_zero_weights_for(list(returns.columns), survivors))
    return out


def mean_variance(returns: pd.DataFrame, **_: object) -> Dict[int, float]:
    """Markowitz max-Sharpe (assumes risk-free rate 0).

    Closed form (up to normalization): w ∝ Sigma^{-1} @ mu.
    Caps at zero (no shorting) to keep the chart readable. Falls back to
    equal weight on singular Sigma.
    """
    cleaned = _clean_returns(returns)
    survivors = list(cleaned.columns)
    if not survivors:
        return {int(sid): 0.0 for sid in returns.columns}
    cov = cleaned.cov().to_numpy()
    mu = cleaned.mean().to_numpy()
    ridge = max(1e-6, 1e-4 * float(np.trace(cov)) / cov.shape[0]) * np.eye(cov.shape[0])
    try:
        inv = np.linalg.inv(cov + ridge)
    except np.linalg.LinAlgError:
        logger.warning("mean_variance: singular covariance; falling back to equal weight")
        return equal_weight(returns)
    w = inv @ mu
    # Zero out negatives (no-short constraint) and renormalize.
    w = np.maximum(w, 0.0)
    if not (w.sum() > 1e-14):
        logger.warning("mean_variance: no positive weight after no-short clip; inverse_vol fallback")
        return inverse_vol(returns)
    w = w / w.sum()
    raw = {int(survivors[i]): float(w[i]) for i in range(len(survivors))}
    out = _safe_normalize(raw)
    out.update(_zero_weights_for(list(returns.columns), survivors))
    return out


def vol_target(
    returns: pd.DataFrame,
    *,
    vol_target_annual: float = 0.15,
) -> Dict[int, float]:
    """Inverse-vol direction scaled so the portfolio's realised vol ≈ target.

    Cap any single strategy at 100% of the budget — combined weights still
    sum to 1.0 (the leverage stays implicit in the strategy's own sizing
    decisions). The return dict therefore mirrors `inverse_vol` exactly for
    P2 — vol-target as an explicit leverage layer ships in P3 with the live
    deploy hook.
    """
    cleaned = _clean_returns(returns)
    survivors = list(cleaned.columns)
    if not survivors:
        return {int(sid): 0.0 for sid in returns.columns}
    sigma = cleaned.std(ddof=1) * DAILY_TO_ANNUAL
    raw = {int(sid): float(vol_target_annual) / max(float(sigma[sid]), 1e-14) for sid in survivors}
    out = _safe_normalize(raw)
    out.update(_zero_weights_for(list(returns.columns), survivors))
    return out


def half_kelly(returns: pd.DataFrame, **_: object) -> Dict[int, float]:
    """0.5 * mu / variance, normalized.

    Negative-mu assets get 0 weight (no shorting in this iteration).
    """
    cleaned = _clean_returns(returns)
    survivors = list(cleaned.columns)
    if not survivors:
        return {int(sid): 0.0 for sid in returns.columns}
    mu = cleaned.mean()
    var = cleaned.var(ddof=1)
    raw: Dict[int, float] = {}
    for sid in survivors:
        m = float(mu[sid])
        v = float(var[sid])
        if not (v > 1e-14) or m <= 0:
            raw[int(sid)] = 0.0
            continue
        raw[int(sid)] = 0.5 * m / v
    out = _safe_normalize(raw)
    if all(abs(w) < 1e-14 for w in out.values()):
        logger.info("half_kelly: every survivor has non-positive expected return; falling back to inverse_vol")
        return inverse_vol(returns)  # fallback already zeroes non-survivors; skip half_kelly's survivor mask
    out.update(_zero_weights_for(list(returns.columns), survivors))
    return out


def min_variance(returns: pd.DataFrame, **_: object) -> Dict[int, float]:
    """Closed-form min-variance (long-only via clip + renorm).

    Solves w = Sigma^{-1} @ 1 then clips negatives + renormalizes. For small
    portfolios this is close enough to the true projected min-variance frontier.
    """
    cleaned = _clean_returns(returns)
    survivors = list(cleaned.columns)
    if not survivors:
        return {int(sid): 0.0 for sid in returns.columns}
    cov = cleaned.cov().to_numpy()
    ridge = max(1e-6, 1e-4 * float(np.trace(cov)) / cov.shape[0]) * np.eye(cov.shape[0])
    try:
        inv = np.linalg.inv(cov + ridge)
    except np.linalg.LinAlgError:
        logger.warning("min_variance: singular covariance; falling back to equal weight")
        return equal_weight(returns)
    ones = np.ones(cov.shape[0])
    w = inv @ ones
    w = np.maximum(w, 0.0)
    if not (w.sum() > 1e-14):
        return equal_weight(returns)
    w = w / w.sum()
    raw = {int(survivors[i]): float(w[i]) for i in range(len(survivors))}
    out = _safe_normalize(raw)
    out.update(_zero_weights_for(list(returns.columns), survivors))
    return out


def cvar_5(returns: pd.DataFrame, **_: object) -> Dict[int, float]:
    """CVaR-aware allocator placeholder.

    Full CVaR optimization requires LP solver setup that's out of scope for
    P2. We approximate by weighting INVERSELY to each strategy's empirical
    5% CVaR (mean of the worst 5% of returns).
    """
    cleaned = _clean_returns(returns)
    survivors = list(cleaned.columns)
    if not survivors:
        return {int(sid): 0.0 for sid in returns.columns}
    raw: Dict[int, float] = {}
    for sid in survivors:
        col = cleaned[sid].to_numpy()
        if col.size < 20:
            raw[int(sid)] = 0.0
            continue
        cutoff = np.quantile(col, 0.05)
        tail = col[col <= cutoff]
        if tail.size < 5:
            tail = col[col < 0.0]
        if tail.size == 0 or not np.all(np.isfinite(tail)):
            raw[int(sid)] = 0.0
            continue
        cvar = float(-tail.mean())
        if not (cvar > 1e-14):
            raw[int(sid)] = 0.0
            continue
        raw[int(sid)] = 1.0 / cvar
    out = _safe_normalize(raw)
    if all(abs(w) < 1e-14 for w in out.values()):
        return risk_parity(returns)  # fallback already zeroes non-survivors; skip cvar_5's survivor mask
    out.update(_zero_weights_for(list(returns.columns), survivors))
    return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def umc(returns: pd.DataFrame, **_: object) -> Dict[int, float]:
    """Uniform Marginal Contribution — P6-M16.

    Risk-parity is the textbook UMC solution at equilibrium; we forward to it
    so the math stays consistent. The separate method key gives operators a
    distinct dropdown option to record their intent (e.g. for audit trails).
    """
    return risk_parity(returns)


def use_target_etl(returns: pd.DataFrame, target_etl_monthly: float = 0.05, **_: object) -> Dict[int, float]:
    """ETL (Expected Tail Loss) targeted scaling — P6-M16.

    Builds an inverse-vol base and then rescales toward a target monthly tail
    loss. We approximate target ETL via 5th-percentile mean (same engine as
    ``cvar_5``); when a strategy's empirical ETL is below target, its weight
    is held; when above, it is shrunk proportionally.
    """
    cleaned = _clean_returns(returns)
    survivors = list(cleaned.columns)
    if not survivors:
        return {int(sid): 0.0 for sid in returns.columns}
    base = inverse_vol(returns)
    raw: Dict[int, float] = {}
    for sid in survivors:
        col = cleaned[sid].to_numpy()
        if col.size < 20:
            raw[int(sid)] = float(base.get(int(sid), 0.0))
            continue
        cutoff = np.quantile(col, 0.05)
        tail = col[col <= cutoff]
        if tail.size == 0:
            raw[int(sid)] = float(base.get(int(sid), 0.0))
            continue
        etl = float(-tail.mean())
        if not (etl > 1e-14):
            raw[int(sid)] = float(base.get(int(sid), 0.0))
            continue
        scale = min(1.0, max(0.0, float(target_etl_monthly) / etl))
        raw[int(sid)] = float(base.get(int(sid), 0.0)) * scale
    out = _safe_normalize(raw)
    if all(abs(w) < 1e-14 for w in out.values()):
        out = base
    out.update(_zero_weights_for(list(returns.columns), survivors))
    return out


def eroc_es(returns: pd.DataFrame, **_: object) -> Dict[int, float]:
    """Excess Return over Expected Shortfall (ES) — P6-M16.

    Weight ∝ ``max(0, mean) / CVaR(5%)``. Strategies with negative mean get 0
    weight (no contribution to a long-only mix); the CVaR denominator falls
    back to the equal-weight allocator if every strategy has degenerate tail
    behaviour.
    """
    cleaned = _clean_returns(returns)
    survivors = list(cleaned.columns)
    if not survivors:
        return {int(sid): 0.0 for sid in returns.columns}
    raw: Dict[int, float] = {}
    for sid in survivors:
        col = cleaned[sid].to_numpy()
        if col.size < 20:
            raw[int(sid)] = 0.0
            continue
        cutoff = np.quantile(col, 0.05)
        tail = col[col <= cutoff]
        if tail.size < 5:
            tail = col[col < 0.0]
        if tail.size == 0 or not np.all(np.isfinite(tail)):
            raw[int(sid)] = 0.0
            continue
        cvar = float(-tail.mean())
        mean = float(np.nanmean(col))
        if not (cvar > 1e-14) or not (mean > 1e-14):
            raw[int(sid)] = 0.0
            continue
        raw[int(sid)] = mean / cvar
    out = _safe_normalize(raw)
    if all(abs(w) < 1e-14 for w in out.values()):
        return equal_weight(returns)  # fallback already zeroes non-survivors; skip eroc_es's survivor mask
    out.update(_zero_weights_for(list(returns.columns), survivors))
    return out


ALLOCATORS: Dict[str, Callable[..., Dict[int, float]]] = {
    "equal_weight": equal_weight,
    "inverse_vol": inverse_vol,
    "risk_parity": risk_parity,
    "mean_variance": mean_variance,
    "vol_target_15": lambda r, **kw: vol_target(r, vol_target_annual=0.15, **kw),
    "umc": umc,
    "use_target_etl": use_target_etl,
    "half_kelly": half_kelly,
    "min_variance": min_variance,
    "eroc_es": eroc_es,
    "cvar_5": cvar_5,
}


def allocate(method: str, returns: pd.DataFrame, **kwargs) -> Dict[int, float]:
    """Method-key dispatcher with a stable fallback."""
    fn = ALLOCATORS.get(method)
    if fn is None:
        logger.warning("Unknown allocator '%s'; falling back to inverse_vol", method)
        fn = ALLOCATORS["inverse_vol"]
    return fn(returns, **kwargs)


# ---------------------------------------------------------------------------
# P7 — Risk decomposition helpers (used by /portfolio-optimizer)
# ---------------------------------------------------------------------------


def portfolio_realized_vol(
    weights: Dict[int, float], returns: pd.DataFrame, *, annualize: bool = True
) -> Optional[float]:
    """Realized portfolio vol = sqrt(w' Σ w). Annualized when annualize=True."""
    if returns is None or returns.empty:
        return None
    cleaned = _clean_returns(returns)
    if cleaned.empty:
        return None
    cols = [int(c) for c in cleaned.columns]
    w = np.array([float(weights.get(c, 0.0)) for c in cols])
    if not (np.abs(w).sum() > 1e-14):
        return None
    cov = cleaned.cov().to_numpy()
    var = float(w @ cov @ w)
    if not (var > 1e-14):
        return None
    vol = math.sqrt(var)
    if annualize:
        vol *= DAILY_TO_ANNUAL
    return round(vol, 6)


def marginal_risk_contribution(
    weights: Dict[int, float], returns: pd.DataFrame
) -> Dict[int, float]:
    """Per-strategy fraction of total portfolio risk (sums to ~1.0).

    MRC_i = w_i * (Σ w)_i / sqrt(w' Σ w)
    Returns ``{}`` when portfolio vol is degenerate.
    """
    if returns is None or returns.empty:
        return {}
    cleaned = _clean_returns(returns)
    if cleaned.empty:
        return {}
    cols = [int(c) for c in cleaned.columns]
    w = np.array([float(weights.get(c, 0.0)) for c in cols])
    if not (np.abs(w).sum() > 1e-14):
        return {}
    cov = cleaned.cov().to_numpy()
    # P29-S5: reject at variance level so subnormal var (~1e-320) doesn't
    # produce ~1e-160 sigma that passes the >1e-14 check falsely.
    _var_p = float(w @ cov @ w)
    if not (_var_p > 1e-14):
        return {}
    sigma_p = math.sqrt(_var_p)
    if not (sigma_p > 1e-14):
        return {}
    sigma_w = cov @ w  # marginal contributions vector
    mrc = w * sigma_w / sigma_p
    # Normalize by the SIGNED sum of risk contributions (which equals sigma_p by
    # Euler's theorem) so the fractions genuinely sum to 1.0 and a diversifying
    # (negatively-correlated) leg is reported as a true negative contribution
    # rather than mangled by an abs() denominator. Using np.abs(...).sum() here
    # breaks the documented 'sums to ~1.0' contract whenever any (cov@w)_i < 0.
    total = float(mrc.sum())
    if not (abs(total) > 1e-14):
        return {}
    return {int(cols[i]): round(float(mrc[i] / total), 6) for i in range(len(cols))}


def diversification_ratio(
    weights: Dict[int, float], returns: pd.DataFrame
) -> Optional[float]:
    """DR = (Σ w_i σ_i) / sqrt(w' Σ w). 1.0 = no diversification, >1 = beneficial."""
    if returns is None or returns.empty:
        return None
    cleaned = _clean_returns(returns)
    if cleaned.empty:
        return None
    cols = [int(c) for c in cleaned.columns]
    w = np.array([float(weights.get(c, 0.0)) for c in cols])
    if not (np.abs(w).sum() > 1e-14):
        return None
    stds = cleaned.std(ddof=1).to_numpy()
    cov = cleaned.cov().to_numpy()
    num = float(np.sum(np.abs(w) * stds))
    # P29-S5: reject at variance level (same as risk_contribution).
    _var_p = float(w @ cov @ w)
    if not (_var_p > 1e-14):
        return None
    den = math.sqrt(_var_p)
    if not (den > 1e-14):
        return None
    return round(num / den, 4)


__all__ = [
    "ALLOCATORS",
    "METHOD_KEYS",
    "METHOD_LABELS",
    "allocate",
    "equal_weight",
    "inverse_vol",
    "risk_parity",
    "mean_variance",
    "vol_target",
    "half_kelly",
    "min_variance",
    "cvar_5",
    "umc",
    "use_target_etl",
    "eroc_es",
    "portfolio_realized_vol",
    "marginal_risk_contribution",
    "diversification_ratio",
]

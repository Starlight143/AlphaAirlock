"""Additive risk metrics for backtest honesty (P-RISKMETRICS).

The engine (:mod:`backend.core.engine`) reports an *annualized Sharpe* that
assumes IID hourly returns. The user's research (Obsidian: 《年化夏普比率的膨脹
機制》/《Sortino比率》) documents three ways that number lies:

  * **Serial correlation** inflates Sharpe (Andrew Lo 2002: ignoring it inflates
    hedge-fund Sharpe >65%). This bites the multi-asset *equity* path especially:
    ~80% of equity bars are flat-filled overnight/weekend bars → high lag-1
    autocorrelation → an inflated raw Sharpe.
  * **Negative skew / fat tails** (short-vol "Peso problem") — a high Sharpe with
    skew < −1 hides left-tail blow-up risk.
  * **Multiple-testing / overfitting** — a raw Sharpe says nothing about whether
    the track record is statistically distinguishable from luck.

This module computes *additional* metrics that quantify those effects, WITHOUT
modifying the engine or any stored strategy:

  * ``sortino_ratio``                — correct Target-Downside-Deviation (TDD: keep
                                       all N obs, not std of negatives only).
  * ``downside_deviation_annual``    — annualized TDD (MAR = 0).
  * ``lag1_autocorrelation``         — serial correlation ρ₁ of net returns.
  * ``sharpe_autocorr_adjusted``     — Lo ρ₁-corrected Sharpe
                                       (= reported × sqrt((1−ρ₁)/(1+ρ₁))).
  * ``return_skewness`` / ``return_kurtosis`` — distribution shape.
  * ``probabilistic_sharpe_ratio``   — PSR vs SR*=0 (Bailey & López de Prado 2012):
                                       P(true Sharpe > 0) given skew, kurtosis, T.

All functions are pure, NaN/Inf-guarded (denominators use the project's
``not (x > 1e-14)`` rule), and dependency-light (``math.erf`` for the normal CDF
— no scipy). Returns are treated as a per-bar NET return series (the engine's
``per_bar[].pnl_pct``); annualization defaults to the engine's hourly
``HOURS_PER_YEAR`` so the numbers are directly comparable to the engine's Sharpe.
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

# Mirror engine.py so annualization is identical and the metrics are comparable.
HOURS_PER_YEAR: int = 24 * 365  # 8760

# Minimum bars for a statistically meaningful PSR (mirrors engine's >=30 floor
# for Sharpe; below this the moment estimates are noise).
PSR_MIN_BARS: int = 30

# Deflated Sharpe Ratio (Bailey-LdP 2014): the expected-maximum-of-N-Gaussians
# term needs the Euler–Mascheroni constant and the standard-normal quantile
# (stdlib probit via NormalDist().inv_cdf — no scipy). Module singletons so the
# hot path never reconstructs them.
_EULER_MASCHERONI: float = 0.5772156649015329
_STD_NORMAL = NormalDist()


def _clean(returns: Sequence[float]) -> np.ndarray:
    """Coerce to a finite 1-D float array (drop NaN/Inf)."""
    arr = np.asarray(list(returns) if not isinstance(returns, np.ndarray) else returns,
                     dtype=float)
    if arr.ndim != 1:
        arr = arr.reshape(-1)
    return arr[np.isfinite(arr)]


def lag1_autocorrelation(returns: Sequence[float]) -> Optional[float]:
    """Pearson correlation of (r[t], r[t+1]); clamped to [-1, 1]. None if degenerate."""
    r = _clean(returns)
    if r.size < 3:
        return None
    a = r[:-1]
    b = r[1:]
    am = a.mean()
    bm = b.mean()
    den = math.sqrt(float(((a - am) ** 2).sum()) * float(((b - bm) ** 2).sum()))
    if not (den > 1e-14):
        return None
    rho = float(((a - am) * (b - bm)).sum()) / den
    if not math.isfinite(rho):
        return None
    return max(-1.0, min(1.0, rho))


def skewness(returns: Sequence[float]) -> Optional[float]:
    """Population (Fisher-Pearson) skewness. None if std is degenerate."""
    r = _clean(returns)
    if r.size < 3:
        return None
    m = r.mean()
    s = r.std(ddof=0)
    if not (s > 1e-14):
        return None
    z = (r - m) / s
    val = float((z ** 3).mean())
    return val if math.isfinite(val) else None


def kurtosis(returns: Sequence[float]) -> Optional[float]:
    """Non-excess kurtosis (normal == 3.0). None if std is degenerate."""
    r = _clean(returns)
    if r.size < 4:
        return None
    m = r.mean()
    s = r.std(ddof=0)
    if not (s > 1e-14):
        return None
    z = (r - m) / s
    val = float((z ** 4).mean())
    return val if math.isfinite(val) else None


def downside_deviation(returns: Sequence[float], mar: float = 0.0) -> Optional[float]:
    """Per-bar Target Downside Deviation (TDD). Correct definition: keep ALL N
    observations, set above-MAR excess to 0 (do NOT drop them — that is the
    common bug that inflates Sortino). None if too short."""
    r = _clean(returns)
    n = r.size
    if n < 2:
        return None
    diff = np.minimum(r - mar, 0.0)
    tdd = math.sqrt(float((diff ** 2).sum()) / n)
    return tdd if math.isfinite(tdd) else None


def sortino_ratio(returns: Sequence[float], mar: float = 0.0,
                  annualization: float = HOURS_PER_YEAR) -> Optional[float]:
    """Annualized Sortino = (mean − MAR) / TDD × sqrt(annualization). Mirrors the
    engine's Sharpe annualization style so the two are directly comparable."""
    r = _clean(returns)
    if r.size < 2:
        return None
    tdd = downside_deviation(r, mar)
    if tdd is None or not (tdd > 1e-14):
        return None
    excess = float(r.mean()) - mar
    val = (excess / tdd) * math.sqrt(annualization)
    return val if math.isfinite(val) else None


def lo_adjusted_sharpe(reported_sharpe: Optional[float],
                       rho1: Optional[float]) -> Optional[float]:
    """Lo (2002) serial-correlation correction: SR_adj = SR × sqrt((1−ρ₁)/(1+ρ₁)).
    Positive ρ₁ (e.g. flat-filled equity bars) deflates the inflated Sharpe.
    Returns the reported Sharpe unchanged when ρ₁ is unavailable."""
    if reported_sharpe is None or not math.isfinite(float(reported_sharpe)):
        return None
    if rho1 is None:
        return float(reported_sharpe)
    rho = max(-0.999, min(0.999, float(rho1)))
    denom = 1.0 + rho
    if not (denom > 1e-14):
        return None
    factor = math.sqrt((1.0 - rho) / denom)
    if not math.isfinite(factor):
        return None
    return float(reported_sharpe) * factor


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf (no scipy dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def probabilistic_sharpe_ratio(returns: Sequence[float],
                               benchmark_sr: float = 0.0) -> Optional[float]:
    """Probabilistic Sharpe Ratio — Bailey & López de Prado (2012).

    PSR(SR*) = Φ( (SR_hat − SR*)·sqrt(T−1) / sqrt(1 − γ₃·SR_hat + ((γ₄−1)/4)·SR_hat²) )

    where SR_hat is the *per-bar* (non-annualized) Sharpe, γ₃/γ₄ are the sample
    skewness/kurtosis, T the sample length. Interprets as P(true Sharpe > SR*).
    Negative skew and fat tails (high γ₄) lower the score — exactly the hidden
    risks a raw Sharpe ignores. None if the sample is too short/degenerate.
    """
    r = _clean(returns)
    T = r.size
    if T < PSR_MIN_BARS:
        return None
    m = float(r.mean())
    s = float(r.std(ddof=1)) if T > 1 else 0.0
    if not (s > 1e-14):
        return None
    sr_hat = m / s
    g3 = skewness(r)
    g3 = 0.0 if g3 is None else g3
    g4 = kurtosis(r)
    g4 = 3.0 if g4 is None else g4
    denom_var = 1.0 - g3 * sr_hat + ((g4 - 1.0) / 4.0) * sr_hat * sr_hat
    if not (denom_var > 1e-14):
        return None
    z = ((sr_hat - float(benchmark_sr)) * math.sqrt(T - 1.0)) / math.sqrt(denom_var)
    if not math.isfinite(z):
        return None
    return max(0.0, min(1.0, _norm_cdf(z)))


def deflated_sharpe_ratio(returns: Sequence[float],
                          trial_sharpes: Sequence[float],
                          *,
                          annualization: float = HOURS_PER_YEAR) -> Optional[float]:
    """Deflated Sharpe Ratio — Bailey, Borwein, López de Prado & Zhu (2014),
    《The Deflated Sharpe Ratio》 (Obsidian: 去 p-hacking / 多重檢定).

    PSR tests ONE track record against SR*=0. But this system is a *strategy
    factory*: it generates many candidates and keeps the best Sharpe. Under that
    selection the expected MAXIMUM Sharpe across N worthless trials is already
    > 0, so a high Sharpe can be pure multiple-testing luck. DSR corrects for it
    by raising the benchmark from 0 to that expected maximum:

        SR* = sqrt(Var(SR_trials)) · [ (1−γ)·Z(1−1/N) + γ·Z(1−1/(N·e)) ]

    with γ the Euler–Mascheroni constant, Z the standard-normal quantile and N the
    number of trials. DSR = PSR(SR*): the probability this strategy's Sharpe beats
    the best-of-N-under-the-null, given its own skew/kurtosis/length.

    ``trial_sharpes`` is the population of *annualized* Sharpes of the sibling
    trials (the factory's search history); they are converted to PSR's per-bar
    units. Returns None when there are < 2 trials, the trial Sharpes are
    degenerate (zero dispersion), or the sample is too short — the caller then
    falls back to the plain PSR (no deflation, i.e. SR*=0).
    """
    cleaned = [float(s) for s in trial_sharpes
               if s is not None and math.isfinite(float(s))]
    n_trials = len(cleaned)
    if n_trials < 2:
        return None
    scale = math.sqrt(annualization)
    if not (scale > 1e-14):
        return None
    # PSR works in per-bar Sharpe units; the stored trial Sharpes are annualized.
    per_bar = np.asarray(cleaned, dtype=float) / scale
    var_sr = float(per_bar.var(ddof=1))
    if not (var_sr > 1e-14):
        return None
    # Expected maximum of N i.i.d. standard normals (Bailey-LdP approximation).
    # n_trials>=2 ⇒ 1−1/N ∈ [0.5,1) and 1−1/(N·e) ∈ [0.63,1): inv_cdf never sees
    # 0 or 1, so no ±inf.
    z1 = _STD_NORMAL.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = _STD_NORMAL.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    expected_max = (1.0 - _EULER_MASCHERONI) * z1 + _EULER_MASCHERONI * z2
    sr_star = math.sqrt(var_sr) * expected_max
    if not math.isfinite(sr_star):
        return None
    return probabilistic_sharpe_ratio(returns, benchmark_sr=sr_star)


def deflated_sharpe_from_per_bar(per_bar: Optional[List[Dict[str, Any]]],
                                 trial_sharpes: Sequence[float],
                                 *,
                                 annualization: float = HOURS_PER_YEAR) -> Optional[float]:
    """Pull NET per-bar returns (``pnl_pct``) from the engine tape and compute the
    Deflated Sharpe Ratio against the sibling-trial Sharpe population. None on
    empty/invalid input or too few trials (never raises). Mirrors
    :func:`compute_from_per_bar`'s extraction so the units match PSR exactly."""
    if not per_bar:
        return None
    try:
        rets = [row.get("pnl_pct") for row in per_bar if isinstance(row, dict)]
    except Exception:  # noqa: BLE001
        return None
    rets = [float(x) for x in rets if x is not None]
    if len(rets) < 2:
        return None
    return deflated_sharpe_ratio(rets, trial_sharpes, annualization=annualization)


def compute_risk_metrics(returns: Sequence[float], *,
                         reported_sharpe: Optional[float] = None,
                         annualization: float = HOURS_PER_YEAR) -> Dict[str, float]:
    """Bundle the additive risk metrics for a per-bar NET return series. Returns a
    dict of only the metrics that could be computed (degenerate ones are omitted)."""
    r = _clean(returns)
    out: Dict[str, float] = {}
    if r.size < 2:
        return out

    def _put(key: str, val: Optional[float], ndigits: int = 6) -> None:
        if val is not None and math.isfinite(float(val)):
            out[key] = round(float(val), ndigits)

    rho1 = lag1_autocorrelation(r)
    dd = downside_deviation(r, 0.0)
    _put("lag1_autocorrelation", rho1)
    _put("sortino_ratio", sortino_ratio(r, 0.0, annualization), 4)
    if dd is not None:
        _put("downside_deviation_annual", dd * math.sqrt(annualization))
    _put("return_skewness", skewness(r))
    _put("return_kurtosis", kurtosis(r))
    _put("probabilistic_sharpe_ratio", probabilistic_sharpe_ratio(r, 0.0))
    if reported_sharpe is not None:
        _put("sharpe_autocorr_adjusted", lo_adjusted_sharpe(reported_sharpe, rho1), 4)
    return out


def compute_from_per_bar(per_bar: Optional[List[Dict[str, Any]]], *,
                         reported_sharpe: Optional[float] = None,
                         annualization: float = HOURS_PER_YEAR) -> Dict[str, float]:
    """Convenience wrapper: pull the NET per-bar returns (``pnl_pct``) from the
    engine's ``per_bar`` tape and compute the risk metrics. Empty/invalid input
    yields ``{}`` (never raises)."""
    if not per_bar:
        return {}
    try:
        rets = [row.get("pnl_pct") for row in per_bar if isinstance(row, dict)]
    except Exception:  # noqa: BLE001
        return {}
    rets = [float(x) for x in rets if x is not None]
    if len(rets) < 2:
        return {}
    return compute_risk_metrics(rets, reported_sharpe=reported_sharpe,
                                annualization=annualization)


__all__ = [
    "HOURS_PER_YEAR",
    "PSR_MIN_BARS",
    "lag1_autocorrelation",
    "skewness",
    "kurtosis",
    "downside_deviation",
    "sortino_ratio",
    "lo_adjusted_sharpe",
    "probabilistic_sharpe_ratio",
    "deflated_sharpe_ratio",
    "deflated_sharpe_from_per_bar",
    "compute_risk_metrics",
    "compute_from_per_bar",
]

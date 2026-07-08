"""Pairwise cointegration scan (P8-FIX/C-2).

Computes Engle-Granger cointegration p-values for every (i, j) pair across a
universe of synthetic perpetual symbols. Results are cached on disk; cold
compute over ~30 symbols takes a few seconds, warm reads are instant.

Price universe is sourced from the multi-asset synthetic generator
(:mod:`backend.core.data_gen.generate_multiasset`). The generator is invoked
lazily the first time the universe is queried and the prices directory is
empty, so a freshly-cloned repo can render the page without manual steps.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from backend.core.database import PROJECT_ROOT

logger = logging.getLogger("alpha.cointegration")

# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

PRICES_DIR: Path = PROJECT_ROOT / "storage" / "prices"
CACHE_DIR: Path = PROJECT_ROOT / "storage"
CACHE_PATH: Path = CACHE_DIR / "cointegration_cache.json"

COINT_CACHE_TTL_SECONDS: int = 12 * 3600  # 12h — long enough that any synthetic
                                          # universe is stable across a working day.

DEFAULT_LOOKBACK_DAYS: int = 180
DEFAULT_P_THRESHOLD: float = 0.05

# Universe sizing — keep manageable so cold compute stays under ~10s.
DEFAULT_SYMBOL_COUNT: int = 30

_CACHE_LOCK = threading.Lock()
_GEN_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Universe + price loading
# ---------------------------------------------------------------------------


def _ensure_universe() -> None:
    """Generate the multi-asset synthetic CSVs if the prices dir is empty.

    Single-flight via ``_GEN_LOCK`` so concurrent first-time requests don't
    fight to write the same files.
    """
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    existing = list(PRICES_DIR.glob("*-USDT.csv"))
    if existing:
        return
    with _GEN_LOCK:
        # Re-check after acquiring lock (another caller may have populated).
        if list(PRICES_DIR.glob("*-USDT.csv")):
            return
        logger.info("Prices dir empty — generating multi-asset synthetic universe")
        from backend.core.data_gen import generate_multiasset
        generate_multiasset(out_dir=PRICES_DIR)


def list_symbols() -> List[str]:
    _ensure_universe()
    files = sorted(PRICES_DIR.glob("*-USDT.csv"))
    return [p.stem for p in files]


def _load_close_series(symbol: str) -> Optional[pd.Series]:
    path = PRICES_DIR / f"{symbol}.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to read %s", path)
        return None
    if "close" not in df.columns or "timestamp" not in df.columns:
        return None
    ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    closes = pd.to_numeric(df["close"], errors="coerce")
    s = pd.Series(closes.values, index=ts)
    s = s.dropna()
    # P32-D4 / DAT32-5 — drop duplicate timestamps (keep latest revision) and
    # sort, matching factor_evaluator / regime. A non-unique index here makes the
    # outer-join concat in `_aligned_log_prices` row-explode and the subsequent
    # ffill leak values across the explosion, corrupting coint() p-values and
    # OLS hedge ratios. This path builds the Series via `pd.Series(..., index=ts)`
    # (not set_index), so the dedup is applied to the Series directly.
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s.name = symbol
    return s


def _aligned_log_prices(
    symbols: List[str],
    *,
    lookback_days: int,
) -> Tuple[List[str], Optional[pd.DataFrame]]:
    """Load + align log-close prices for the given symbols.

    Returns ``(symbols_with_data, frame)`` where ``frame`` is a DataFrame with
    timestamp index and one column per loaded symbol. Symbols missing CSVs are
    dropped from the returned list silently.
    """
    series_list: List[pd.Series] = []
    kept: List[str] = []
    for sym in symbols:
        s = _load_close_series(sym)
        if s is None or s.empty:
            continue
        series_list.append(s)
        kept.append(sym)
    if not series_list:
        return [], None
    df = pd.concat(series_list, axis=1, join="outer").sort_index()
    # R5/QR-5: bound the forward-fill to 2 bars. An unbounded ffill over the full
    # outer-joined history silently papers over multi-bar exchange-downtime gaps
    # inside the lookback window with repeated log-prices, creating autocorrelation
    # runs that bias AIC lag selection and the ADF/half-life fit. Gaps > 2 bars now
    # become NaN and the pair is dropped rather than scored with a spurious p-value.
    df = df.ffill(limit=2).dropna(how="any")
    if df.empty:
        return kept, None
    if lookback_days > 0:
        # Infer bar frequency from the loaded data rather than assuming 1h.
        # Median spacing guards against occasional duplicates or gaps.
        if len(df) >= 2:
            median_spacing_hours = (
                df.index.to_series().diff().dt.total_seconds().median() / 3600.0
            )
            if not (median_spacing_hours > 1e-6):
                median_spacing_hours = 1.0  # fallback: assume 1h if indeterminate
            bars_per_day = max(1, round(24.0 / median_spacing_hours))
        else:
            bars_per_day = 24  # default: 1h bars
        bars = int(lookback_days) * bars_per_day
        df = df.tail(bars)
    # Drop zero / negative prices before log (would produce -inf).
    # Use > 1e-14 (project-wide subnormal guard) so IEEE 754 subnormals such as
    # 5e-324 (which satisfy `> 0.0`) are converted to NaN and dropped rather
    # than producing np.log(5e-324) ≈ -745, which would corrupt OLS/ADF results.
    df = df.where(df > 1e-14)  # subnormal-safe; matches project convention
    df = df.dropna(how="any")
    if df.empty:
        return kept, None
    return kept, np.log(df)


# ---------------------------------------------------------------------------
# Engle-Granger
# ---------------------------------------------------------------------------


def _engle_granger(
    s1: pd.Series,
    s2: pd.Series,
) -> Tuple[float, float, Optional[float]]:
    """Return ``(p_value, beta, half_life_bars)`` for the s1 ~ s2 pair.

    Uses statsmodels ``coint`` (Engle-Granger with constant trend). Hedge
    ratio (``beta``) is the OLS slope of ``s1`` regressed on ``s2`` with
    intercept. Half-life of mean reversion is computed from an AR(1) fit on
    the residual spread; ``None`` when the spread is not mean-reverting.
    """
    from statsmodels.tsa.stattools import coint
    import statsmodels.api as sm

    a = s1.values.astype(float)
    b = s2.values.astype(float)
    if a.shape[0] < 50 or b.shape[0] < 50:
        return 1.0, 0.0, None
    # 1) p-value via Engle-Granger
    try:
        _, p_value, _ = coint(a, b, trend="c", autolag="AIC")
    except Exception:  # noqa: BLE001
        return 1.0, 0.0, None
    if not np.isfinite(p_value):
        return 1.0, 0.0, None
    # Clamp to [0, 1]: statsmodels coint() interpolates MacKinnon tables and can
    # emit a probability marginally outside the unit interval when the test stat
    # falls outside the tabulated grid (downstream p_threshold filter and the UI
    # assume a clean [0,1] range — cf. granger.py:224).
    p_value = max(0.0, min(1.0, float(p_value)))
    # 2) hedge ratio via OLS s1 = alpha + beta * s2
    _ols_succeeded = False
    try:
        X = sm.add_constant(b)
        ols_res = sm.OLS(a, X).fit()
        beta = float(ols_res.params[1])
        _ols_succeeded = True
    except Exception:  # noqa: BLE001
        beta = 0.0
    # 3) half-life via AR(1) on residual spread.
    # Guard: if OLS failed, skip half-life — the spread would be the raw
    # log-price of s1 (non-stationary), not the cointegrating spread,
    # producing a meaningless half_life. Use a dedicated boolean rather than
    # comparing beta == 0.0, which is a valid hedge ratio for certain pairs.
    half_life: Optional[float] = None
    if _ols_succeeded:
        try:
            spread = a - beta * b
            # Do not subtract the full-sample mean: the AR(1) regression
            # below includes a constant via sm.add_constant(lag), which
            # absorbs any non-zero mean of delta. Subtracting np.mean(spread)
            # here would use future observations to centre early deltas,
            # introducing lookahead bias — especially harmful at n=30 (min).
            if len(spread) > 2:
                lag = spread[:-1]
                cur = spread[1:]
                delta = cur - lag
                X2 = sm.add_constant(lag)
                ar_res = sm.OLS(delta, X2).fit()
                rho = float(ar_res.params[1])
                # Stable mean-reverting AR(1): rho in (-1, 0). Outside that range
                # the half-life is undefined / explosive.
                if math.isfinite(rho) and rho < -1e-4 and (1.0 + rho) > 1e-14:
                    hl = -math.log(2.0) / math.log(1.0 + rho)
                    if math.isfinite(hl) and 0 < hl <= 2000:
                        half_life = float(round(hl, 2))
        except Exception:  # noqa: BLE001
            half_life = None
    return float(round(float(p_value), 6)), float(round(beta, 6)), half_life


# ---------------------------------------------------------------------------
# Top-level compute
# ---------------------------------------------------------------------------


def compute_pairs(
    *,
    symbols: Optional[List[str]] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    p_threshold: float = DEFAULT_P_THRESHOLD,
    seed: int = 42,
) -> Dict[str, Any]:
    """Compute every below-threshold cointegration pair.

    Returns:
        {
          "assets":      List[str],
          "pairs":       [{src, dst, p_value, beta, half_life_bars,
                           q_value, bh_rank}, ...],
          "method":      "engle_granger",
          "lookback_days": int,
          "p_threshold": float,
          "correction":  "benjamini_hochberg",
          "computed_at": ISO Z,
          "is_synthetic": True,
          "pair_count":  int,
          "tested_pair_count": int,
        }
    """
    lookback_days = max(7, min(720, int(lookback_days or DEFAULT_LOOKBACK_DAYS)))
    p_threshold = max(1e-6, min(0.50, float(p_threshold or DEFAULT_P_THRESHOLD)))

    universe = symbols or list_symbols()
    if not universe:
        return {
            "assets": [],
            "pairs": [],
            "method": "engle_granger",
            "lookback_days": lookback_days,
            "p_threshold": p_threshold,
            "correction": "benjamini_hochberg",
            "computed_at": _utc_iso(),
            "is_synthetic": True,
            "pair_count": 0,
            "tested_pair_count": 0,
            "error": "no price data — generator did not produce any CSVs",
        }
    assets, log_prices = _aligned_log_prices(universe, lookback_days=lookback_days)
    if log_prices is None or log_prices.empty or len(assets) < 2:
        return {
            "assets": assets,
            "pairs": [],
            "method": "engle_granger",
            "lookback_days": lookback_days,
            "p_threshold": p_threshold,
            "correction": "benjamini_hochberg",
            "computed_at": _utc_iso(),
            "is_synthetic": True,
            "pair_count": 0,
            "tested_pair_count": 0,
            "error": "insufficient overlapping data after alignment",
        }

    # P-FIX — collect EVERY tested pair first, then apply a Benjamini-Hochberg
    # FDR gate across the whole family before surfacing survivors. Gating on
    # the raw per-test p_value over C(n,2) tests (435 at n=30) yields an
    # expected ~p_threshold*tested false positives and FWER ~= 1.0, so the
    # raw list is dominated by statistical noise. BH controls the expected
    # false-discovery proportion at `p_threshold` without changing the public
    # return shape (only additive `q_value` / `bh_rank` fields are added, and
    # `p_value` is retained for the existing frontend / colour mapping).
    candidates: List[Dict[str, Any]] = []
    tested = 0
    for i in range(len(assets)):
        for j in range(i + 1, len(assets)):
            a = assets[i]
            b = assets[j]
            s1 = log_prices[a]
            s2 = log_prices[b]
            tested += 1
            p_value, beta, half_life = _engle_granger(s1, s2)
            candidates.append({
                "src": a,
                "dst": b,
                "p_value": p_value,
                "beta": beta,
                "half_life_bars": half_life,
            })

    pairs: List[Dict[str, Any]] = []
    if candidates:
        from statsmodels.stats.multitest import multipletests
        raw_p = [float(c["p_value"]) for c in candidates]
        try:
            reject, q_values, _, _ = multipletests(
                raw_p, alpha=float(p_threshold), method="fdr_bh"
            )
        except Exception:  # noqa: BLE001 — degrade to raw gate, never crash the scan
            logger.exception("BH FDR correction failed; falling back to raw p gate")
            reject = [p < p_threshold for p in raw_p]
            q_values = raw_p
        for cand, keep, q in zip(candidates, reject, q_values):
            if not keep:
                continue
            qf = float(q)
            cand["q_value"] = round(min(1.0, max(0.0, qf)), 6)
            pairs.append(cand)
    # Sort survivors by adjusted q first (true discovery ranking), then raw p.
    pairs.sort(key=lambda r: (r.get("q_value", r["p_value"]), r["p_value"]))
    for rank, pr in enumerate(pairs, start=1):
        pr["bh_rank"] = rank

    return {
        "assets": assets,
        "pairs": pairs,
        "method": "engle_granger",
        "lookback_days": lookback_days,
        "p_threshold": p_threshold,
        "correction": "benjamini_hochberg",
        "computed_at": _utc_iso(),
        "is_synthetic": True,
        "pair_count": len(pairs),
        "tested_pair_count": tested,
    }


# ---------------------------------------------------------------------------
# On-disk cache
# ---------------------------------------------------------------------------


def _cache_key(lookback_days: int, p_threshold: float) -> str:
    return f"{int(lookback_days)}_{float(p_threshold):.6f}"


def _load_cache() -> Dict[str, Any]:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.exception("Failed to read cointegration cache")
        return {}


def _write_cache(data: Dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write cointegration cache")


def compute_pairs_cached(
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    p_threshold: float = DEFAULT_P_THRESHOLD,
    force: bool = False,
) -> Dict[str, Any]:
    """Cached wrapper around :func:`compute_pairs`.

    Cache is keyed on ``(lookback_days, p_threshold)``; entries older than
    ``COINT_CACHE_TTL_SECONDS`` are recomputed. ``force=True`` always
    recomputes.
    """
    key = _cache_key(lookback_days, p_threshold)
    # Phase 1: read-under-lock — only hold the lock for the cheap disk read + check.
    with _CACHE_LOCK:
        cache = _load_cache()
        entry = cache.get(key)
        if not force and entry:
            try:
                computed_at = entry.get("computed_at")
                if computed_at:
                    ts = datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - ts).total_seconds()
                    if age < COINT_CACHE_TTL_SECONDS:
                        return entry
            except Exception:  # noqa: BLE001
                pass  # fall through to recompute on parse error
    # Phase 2: compute outside the lock — this is the expensive O(n²) scan.
    # Concurrent callers with the same key may each compute independently; the
    # last writer wins on the write-back, which is acceptable (idempotent result).
    fresh = compute_pairs(
        lookback_days=lookback_days,
        p_threshold=p_threshold,
    )
    # Phase 3: write-under-lock — double-check freshness before storing so a
    # concurrent caller that already finished does not get overwritten by a
    # stale result from a slower compute that started earlier.
    with _CACHE_LOCK:
        cache = _load_cache()
        existing = cache.get(key)
        if not force and existing:
            try:
                computed_at = existing.get("computed_at")
                if computed_at:
                    ts = datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - ts).total_seconds()
                    if age < COINT_CACHE_TTL_SECONDS:
                        # A concurrent caller already refreshed the entry.
                        return existing
            except Exception:  # noqa: BLE001
                pass
        cache[key] = fresh
        # Keep cache size bounded — limit to last 12 distinct params.
        if len(cache) > 12:
            sorted_keys = sorted(
                cache.keys(),
                key=lambda k: cache[k].get("computed_at") or "",
                reverse=True,
            )
            cache = {k: cache[k] for k in sorted_keys[:12]}
        _write_cache(cache)
        return fresh


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "compute_pairs",
    "compute_pairs_cached",
    "list_symbols",
    "PRICES_DIR",
    "CACHE_PATH",
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_P_THRESHOLD",
]

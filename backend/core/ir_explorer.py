"""Information Ratio aggregations (P7-06 — /ir-explorer).

Renamed from "IRR Explorer" — IR (Information Ratio) is the correct quant
metric. IRR is for cash-flow valuation. Six endpoints:

* ``/api/ir-explorer/book-ir``     — aggregate IR over APPROVED+ strategies
* ``/api/ir-explorer/by-category`` — IR per ``alpha_category``
* ``/api/ir-explorer/by-regime``   — IR per bull/bear/range market regime
* ``/api/ir-explorer/by-asset``    — IR per traded asset (BTC/ETH/STABLE/ALT)
* ``/api/ir-explorer/rolling``     — rolling-window IR time series
* ``/api/ir-explorer/waterfall``   — IR contribution per category waterfall

IR formula:
    IR = mean(active_returns) / std(active_returns) × √252
    active_returns = book_returns − benchmark_returns (BTC default)
When ``benchmark='none'`` the IR collapses to annualized Sharpe; the response
labels this so the frontend can flip the tile label.

Aggregation: equal-weight average of strategy daily returns. Inner-joined on
overlap dates. Strategies missing equity curves silently dropped (listed in
``missing`` array).

Cost guard: in-process TTL cache (1h) keyed by (statuses, benchmark).
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from sqlalchemy.orm import Session

from backend.core.database import AlphaStrategy, PROJECT_ROOT
from backend.core.regime import (
    REGIME_BULL,
    REGIME_BEAR,
    REGIME_RANGE,
    REGIME_VALUES,
    classify_regime_series,
    load_btc_daily,
)

logger = logging.getLogger("alpha.ir_explorer")

DEFAULT_BOOK_STATUSES: Sequence[str] = ("APPROVED", "PAPER_TRADE", "SMALL_CAPITAL", "LIVE")
# P31-R1: 252 (US equity) -> 365 (crypto trades 24/7). Mirrors the change
# applied in portfolio.py / portfolio_optimizer.py / allocators.py at P30.
TRADING_DAYS_PER_YEAR = 365
EPS_DENOM = 1e-14
IR_MIN_SAMPLE = 30  # R6/QR-7: min observations for a statistically meaningful annualised IR
RESULTS_DIR: Path = PROJECT_ROOT / "storage" / "results"

_CACHE: Dict[str, tuple[float, Any]] = {}
_CACHE_TTL_SECONDS = 3600.0
# P13/D-M2 — lock prevents dict-during-iteration RuntimeError under load
# when concurrent /ir-explorer requests warm/expire the same key.
_CACHE_LOCK = threading.Lock()


def _cache_get(key: str) -> Optional[Any]:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is None:
            return None
        ts, val = hit
        if time.monotonic() - ts > _CACHE_TTL_SECONDS:
            _CACHE.pop(key, None)
            return None
        return val


def _cache_put(key: str, val: Any) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), val)


def _cache_key(*parts: Any) -> str:
    return "::".join(str(p) for p in parts)


def _compute_ir(active: pd.Series) -> Optional[float]:
    if active is None or active.empty or len(active) < IR_MIN_SAMPLE:
        return None
    std = float(active.std(ddof=1))
    if not (std > EPS_DENOM):
        return None
    mean = float(active.mean())
    raw = (mean / std) * math.sqrt(TRADING_DAYS_PER_YEAR)
    if not math.isfinite(raw):
        return None
    return round(max(-25.0, min(25.0, raw)), 4)


def _annualized_return(daily_returns: pd.Series) -> Optional[float]:
    if daily_returns is None or daily_returns.empty:
        return None
    total = float((1.0 + daily_returns).prod())
    # P31-R2: ``total <= 0`` misses IEEE 754 subnormals (5e-324 <= 0 is False),
    # producing a deceptive ~-1.0 annualized return instead of an explicit
    # signal for catastrophic-loss curves. Use the project-wide
    # ``not (x > 1e-14)`` guard from CLAUDE.md.
    if not math.isfinite(total):
        return None
    if not (total > 1e-14):
        # Cumulative wealth <= ~0: annualisation undefined (negative-base
        # fractional exponent); report -100% so the UI surfaces the loss
        # rather than swallowing as None.
        return -1.0
    n = len(daily_returns)
    if n < 30:
        # Too few observations to annualise meaningfully — return raw
        # cumulative return instead of extrapolating a tiny window to a year.
        return round(total - 1.0, 6)
    # P32-R-IR: use calendar-day span (mirrors _combined_metrics in portfolio.py
    # and the frontier annualisation in portfolio_optimizer.py) so that data gaps
    # (exchange downtime, staggered strategy windows) do not inflate the exponent
    # by undercounting the actual elapsed time.
    if hasattr(daily_returns.index, 'to_pydatetime') and n >= 2:
        years = (daily_returns.index[-1] - daily_returns.index[0]).days / 365.0
    else:
        years = n / TRADING_DAYS_PER_YEAR
    if not (years > 0.25):
        return round(total - 1.0, 6)
    return round(total ** (1.0 / years) - 1.0, 6)


def _max_drawdown(daily_returns: pd.Series) -> Optional[float]:
    if daily_returns is None or daily_returns.empty:
        return None
    eq = (1.0 + daily_returns).cumprod()
    peak = eq.cummax()
    # P31-DD-NAN1: a return of exactly -1.0 drives eq (and the running peak) to
    # 0, so eq/peak becomes 0/0 = NaN, which propagates through dd.min(). The
    # upstream pct_change replace/fillna only kills ±inf, not this locally
    # regenerated NaN. Inf-guard then fillna(0.0) before reducing so a -100%
    # bar reports a clean drawdown floor instead of NaN.
    dd = (eq / peak - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    v = float(dd.min())
    if not math.isfinite(v):
        return None
    return round(v, 6)


def _load_equity_returns(strategy_id: int) -> Optional[pd.Series]:
    """Load daily returns for a single strategy. None if missing/malformed."""
    p = RESULTS_DIR / f"strategy_{int(strategy_id)}.json"
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        curve = payload.get("equity_curve") or []
        if not curve:
            return None
        rows = []
        for pt in curve:
            ts = pt.get("timestamp")
            eq = pt.get("equity")
            if ts is None or eq is None:
                continue
            rows.append((pd.Timestamp(ts), float(eq)))
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["timestamp", "equity"]).set_index("timestamp")
        df = df.sort_index()
        # P25 — inf-guard the pct_change chain. An equity curve that touches
        # zero (catastrophic loss / data glitch) produces ±inf rows from
        # pct_change(); those propagate downstream into ``_compute_ir`` which
        # calls ``.std(ddof=1)`` and ``.mean()`` at lines 89-92, silently
        # corrupting the IR with inf/NaN before the ``math.isfinite`` guard at
        # line 94 can catch it (the corruption already poisoned mean/std). The
        # canonical pattern matches ``portfolio_optimizer.py:181`` (P24 fix).
        returns = (
            df["equity"].pct_change().dropna()
            .replace([np.inf, -np.inf], np.nan).fillna(0.0)
        )
        return returns
    except Exception:  # noqa: BLE001
        logger.exception("ir_explorer: load_equity_returns(%d) failed", strategy_id)
        return None


def _resolve_book(session: Session, statuses: Sequence[str]) -> List[AlphaStrategy]:
    statuses_norm = [s.upper() for s in statuses if s]
    return (
        session.query(AlphaStrategy)
        .filter(AlphaStrategy.status.in_(statuses_norm))
        .all()
    )


def _derive_asset(strategy: AlphaStrategy) -> str:
    cfg = strategy.config() or {}
    params = cfg.get("params") or {}
    raw = params.get("symbol") or params.get("asset") or cfg.get("symbol")
    if not raw:
        return "BTC"
    s = str(raw).upper().split("-")[0].split("/")[0]
    if s in ("BTC", "XBT"):
        return "BTC"
    if s == "ETH":
        return "ETH"
    if s in ("USDT", "USDC", "BUSD"):
        return "STABLE"
    return "ALT"


def _load_book_returns(
    session: Session, statuses: Sequence[str]
) -> tuple[Dict[int, pd.Series], List[int]]:
    """Return {strategy_id: returns_series}, missing_ids."""
    book = _resolve_book(session, statuses)
    out: Dict[int, pd.Series] = {}
    missing: List[int] = []
    for s in book:
        r = _load_equity_returns(int(s.id))
        if r is None or r.empty:
            missing.append(int(s.id))
            continue
        out[int(s.id)] = r
    return out, missing


def _benchmark_returns(benchmark: str) -> pd.Series:
    if benchmark == "btc":
        close = load_btc_daily()
        if close.empty:
            return pd.Series(dtype=float)
        # P25 — inf-guard. ``concat([book_r, bench], join="inner").dropna()`` in
        # ``benchmark_returns`` does NOT remove inf (only NaN), so inf rows from
        # a glitched close series propagate into the active-returns subtraction
        # and poison ``_compute_ir``'s mean/std.
        return (
            close.pct_change().dropna()
            .replace([np.inf, -np.inf], np.nan).fillna(0.0)
        )
    return pd.Series(dtype=float)


def _aggregate_book_returns(book: Dict[int, pd.Series]) -> pd.Series:
    if not book:
        return pd.Series(dtype=float)
    # R5/QR-4: outer-join on date so the book IR spans every date at least one
    # strategy was active, equal-weighting over the ACTIVE strategies each day
    # (mean skipna). The previous dropna(how="any") was an inner join that
    # silently collapsed the series to the all-strategies-simultaneously-active
    # intersection, discarding early-period performance for staggered start dates
    # and contradicting the documented "equal weight per strategy each day" intent.
    df = pd.concat(book.values(), axis=1).dropna(how="all")
    if df.empty:
        return pd.Series(dtype=float)
    # Date-only index for join with benchmark.
    df.index = pd.to_datetime(df.index).normalize()
    return df.mean(axis=1, skipna=True)


def _book_active_returns(
    book: Dict[int, pd.Series], benchmark: str
) -> tuple[pd.Series, pd.Series, bool]:
    """Returns (active, bench, degraded). degraded=True if BTC benchmark
    requested but file missing → falls back to absolute mode."""
    book_r = _aggregate_book_returns(book)
    if book_r.empty:
        return book_r, pd.Series(dtype=float), False
    if benchmark != "btc":
        return book_r, pd.Series(0.0, index=book_r.index), False
    bench = _benchmark_returns("btc")
    if bench.empty:
        return book_r, pd.Series(0.0, index=book_r.index), True
    bench.index = pd.to_datetime(bench.index).normalize()
    df = pd.concat([book_r, bench], axis=1, join="inner").dropna()
    df.columns = ["book", "bench"]
    return df["book"] - df["bench"], df["bench"], False


# ---- Endpoint helpers --------------------------------------------------------


def book_ir(
    session: Session, *, statuses: Sequence[str] = DEFAULT_BOOK_STATUSES, benchmark: str = "btc"
) -> Dict[str, Any]:
    statuses = list(statuses) or list(DEFAULT_BOOK_STATUSES)
    ck = _cache_key("book_ir", "|".join(statuses), benchmark)
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    book, missing = _load_book_returns(session, statuses)
    if not book:
        out = {
            "book_ir": None, "annualized_return": None, "max_drawdown": None,
            "n_strategies": 0, "n_aligned_days": 0, "benchmark": benchmark,
            "empty": True, "missing": missing, "degraded": False,
        }
        _cache_put(ck, out)
        return out
    active, bench, degraded = _book_active_returns(book, benchmark)
    # Derive the aligned book return series (before benchmark subtraction) so
    # annualized_return and max_drawdown are computed on the same date window as
    # book_ir. When benchmark != 'btc' or bench is empty, bench is a zero Series
    # on book_r's index, so active + bench == book_r identically. When benchmark
    # is 'btc' and alignment truncated the series, active + bench == df["book"]
    # (the inner-joined book slice), giving a consistent date window.
    book_r_aligned = active + bench
    out = {
        "book_ir": _compute_ir(active),
        "annualized_return": _annualized_return(book_r_aligned),
        "max_drawdown": _max_drawdown(book_r_aligned),
        "n_strategies": len(book),
        "n_aligned_days": int(len(active)),
        "benchmark": benchmark if not degraded else "none",
        "empty": False,
        "missing": missing,
        "degraded": degraded,
    }
    _cache_put(ck, out)
    return out


def by_category(
    session: Session, *, statuses: Sequence[str] = DEFAULT_BOOK_STATUSES, benchmark: str = "btc"
) -> Dict[str, Any]:
    statuses = list(statuses) or list(DEFAULT_BOOK_STATUSES)
    ck = _cache_key("by_category", "|".join(statuses), benchmark)
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    book_strategies = _resolve_book(session, statuses)
    by_cat: Dict[str, List[int]] = {}
    for s in book_strategies:
        cat = (s.config() or {}).get("alpha_category") or "Uncategorised"
        by_cat.setdefault(cat, []).append(int(s.id))

    rows: List[Dict[str, Any]] = []
    for cat, sids in sorted(by_cat.items()):
        sub_book: Dict[int, pd.Series] = {}
        for sid in sids:
            r = _load_equity_returns(sid)
            if r is not None and not r.empty:
                sub_book[sid] = r
        if not sub_book:
            rows.append({
                "category": cat, "ir": None, "contribution": 0.0,
                "n_strategies": len(sids), "annualized_return": None,
            })
            continue
        active, _bench, _ = _book_active_returns(sub_book, benchmark)
        rows.append({
            "category": cat,
            "ir": _compute_ir(active),
            "contribution": round(len(sub_book) / max(1, len(book_strategies)), 4),
            "n_strategies": len(sub_book),
            "annualized_return": _annualized_return(_aggregate_book_returns(sub_book)),
        })
    out = {"rows": rows, "benchmark": benchmark}
    _cache_put(ck, out)
    return out


def by_regime(
    session: Session, *, statuses: Sequence[str] = DEFAULT_BOOK_STATUSES, benchmark: str = "btc"
) -> Dict[str, Any]:
    statuses = list(statuses) or list(DEFAULT_BOOK_STATUSES)
    ck = _cache_key("by_regime", "|".join(statuses), benchmark)
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    btc_close = load_btc_daily()
    if btc_close.empty:
        out = {
            "rows": [], "available": False, "benchmark": benchmark,
            "reason": "btc_price_history_unavailable",
        }
        _cache_put(ck, out)
        return out

    regime = classify_regime_series(btc_close)
    book, _ = _load_book_returns(session, statuses)
    if not book:
        out = {"rows": [], "available": True, "benchmark": benchmark}
        _cache_put(ck, out)
        return out
    active, _bench, _ = _book_active_returns(book, benchmark)
    active.index = pd.to_datetime(active.index).normalize()
    regime.index = pd.to_datetime(regime.index).normalize()
    joined = pd.concat([active.rename("ret"), regime.rename("regime")], axis=1, join="inner").dropna()

    rows: List[Dict[str, Any]] = []
    for r in REGIME_VALUES:
        mask = joined["regime"] == r
        slice_ = joined.loc[mask, "ret"]
        rows.append({
            "regime": r,
            "ir": _compute_ir(slice_),
            "n_days": int(mask.sum()),
        })
    out = {"rows": rows, "available": True, "benchmark": benchmark}
    _cache_put(ck, out)
    return out


def by_asset(
    session: Session, *, statuses: Sequence[str] = DEFAULT_BOOK_STATUSES, benchmark: str = "btc"
) -> Dict[str, Any]:
    statuses = list(statuses) or list(DEFAULT_BOOK_STATUSES)
    ck = _cache_key("by_asset", "|".join(statuses), benchmark)
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    book_strategies = _resolve_book(session, statuses)
    by_a: Dict[str, List[int]] = {}
    for s in book_strategies:
        by_a.setdefault(_derive_asset(s), []).append(int(s.id))

    total = max(1, len(book_strategies))
    rows: List[Dict[str, Any]] = []
    for asset, sids in sorted(by_a.items()):
        sub_book: Dict[int, pd.Series] = {}
        for sid in sids:
            r = _load_equity_returns(sid)
            if r is not None and not r.empty:
                sub_book[sid] = r
        if not sub_book:
            rows.append({"asset": asset, "ir": None, "n_strategies": len(sids), "weight": round(len(sids) / total, 4)})
            continue
        active, _b, _d = _book_active_returns(sub_book, benchmark)
        rows.append({
            "asset": asset,
            "ir": _compute_ir(active),
            "n_strategies": len(sub_book),
            "weight": round(len(sub_book) / total, 4),
        })
    out = {"rows": rows, "benchmark": benchmark}
    _cache_put(ck, out)
    return out


def rolling(
    session: Session, *, statuses: Sequence[str] = DEFAULT_BOOK_STATUSES, benchmark: str = "btc", window: int = 30
) -> Dict[str, Any]:
    statuses = list(statuses) or list(DEFAULT_BOOK_STATUSES)
    # Minimum IR_MIN_SAMPLE-day window: mirrors the gate in _compute_ir() so that
    # rolling IR values meet the same statistical-meaningfulness standard (SE < 1/√30
    # × √365 ≈ 3.49, 95% CI ≤ ±6.8 IR units) as all other IR computations in this
    # module.  Windows below IR_MIN_SAMPLE produce IR values the module's own constant
    # defines as sub-threshold; aligning the floor removes the inconsistency.
    window = max(IR_MIN_SAMPLE, min(int(window), 252))
    ck = _cache_key("rolling", "|".join(statuses), benchmark, window)
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    book, _ = _load_book_returns(session, statuses)
    if not book:
        out = {"window": window, "series": [], "benchmark": benchmark}
        _cache_put(ck, out)
        return out
    active, _bench, _ = _book_active_returns(book, benchmark)
    if active.empty:
        out = {"window": window, "series": [], "benchmark": benchmark}
        _cache_put(ck, out)
        return out
    rolling_mean = active.rolling(window).mean()
    rolling_std = active.rolling(window).std(ddof=1)
    ir_series = []
    for ts, mu, sd in zip(active.index, rolling_mean, rolling_std):
        if pd.isna(mu) or pd.isna(sd) or not (sd > EPS_DENOM):
            ir_series.append({"date": ts.strftime("%Y-%m-%d"), "ir": None})
            continue
        raw = (mu / sd) * math.sqrt(TRADING_DAYS_PER_YEAR)
        if not math.isfinite(raw):
            ir_series.append({"date": ts.strftime("%Y-%m-%d"), "ir": None})
            continue
        ir_series.append({
            "date": ts.strftime("%Y-%m-%d"),
            "ir": round(max(-25.0, min(25.0, raw)), 4),
        })
    out = {"window": window, "series": ir_series, "benchmark": benchmark}
    _cache_put(ck, out)
    return out


def waterfall(
    session: Session, *, statuses: Sequence[str] = DEFAULT_BOOK_STATUSES, benchmark: str = "btc"
) -> Dict[str, Any]:
    """Total IR broken into per-category +contribution / -drag deltas."""
    cats = by_category(session, statuses=statuses, benchmark=benchmark)["rows"]
    contrib = [c for c in cats if c["ir"] is not None]
    contrib.sort(key=lambda r: (r["ir"] or 0.0), reverse=True)
    steps: List[Dict[str, Any]] = []
    running = 0.0
    for c in contrib:
        ir = float(c["ir"] or 0.0)
        # contribution is a strategy-count fraction (not return-weight), so the
        # step deltas are directionally correct (positive IR = positive delta,
        # negative IR = negative delta) but their sum does not reconstruct
        # book_ir. A reconciliation residual step is appended below so the
        # waterfall chart always closes to the true book_ir total.
        delta = ir * float(c["contribution"] or 0.0)
        running += delta
        steps.append({
            "label": c["category"],
            "delta": round(delta, 4),
            "running": round(running, 4),
        })
    book = book_ir(session, statuses=statuses, benchmark=benchmark)
    total = book.get("book_ir")  # may be None when IR is uncomputable
    # Append a residual step so the chart always closes: the sum of count-weighted
    # deltas rarely equals the book IR exactly. The residual makes the invariant
    # explicit rather than silently broken.
    total_f = float(total) if total is not None else None
    residual = round((total_f if total_f is not None else 0.0) - running, 4)
    if abs(residual) > 1e-6:
        steps.append({
            "label": "_residual",
            "delta": residual,
            "running": round(total_f, 4) if total_f is not None else round(running, 4),
        })
    return {
        "total_ir": round(total_f, 4) if total_f is not None else None,
        "steps": steps,
        "benchmark": benchmark,
    }


__all__ = [
    "book_ir",
    "by_category",
    "by_regime",
    "by_asset",
    "rolling",
    "waterfall",
    "DEFAULT_BOOK_STATUSES",
]

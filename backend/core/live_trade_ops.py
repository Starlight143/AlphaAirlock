"""Live-trade dashboard data + emergency-pause operator action (P7-10).

This module is **read-only** with respect to the paper trade engine — it
derives positions / fills / PnL from the existing
``paper_trader.latest_for()`` JSON snapshots. The one write path is
``pause_all()`` which flips every ``status IN ('SMALL_CAPITAL', 'LIVE')``
strategy to ``'PAUSED'`` inside a single transaction with idempotency
protection.

Resume is per-strategy by design — no bulk resume to prevent operators from
accidentally undoing an entire pause-all with one click.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from backend._envloader import env_bool, env_str
from backend.core.database import AlphaStrategy, AuditLog
from backend.core.exchange_adapter import ExchangeAdapter, is_live_enabled

logger = logging.getLogger("alpha.live_trade")

_PAUSE_LOCK = threading.Lock()

LIVE_STATUSES = ("SMALL_CAPITAL", "LIVE")

# D-M9 — in-process cache for the exchange ping (10s TTL). The /dashboard
# endpoint refreshes on a 5s React poll and each call previously hit the
# exchange's HTTP ping, which is expensive and rate-limited. 10s TTL is short
# enough that operators still see fresh status without 12 req/min outbound.
_PING_CACHE_LOCK = threading.Lock()
_PING_CACHE: Dict[str, Any] = {"ts": 0.0, "payload": None}
_PING_CACHE_TTL_SEC = 10.0

# D-PERF — in-process memo for the full /dashboard payload. The endpoint is
# polled every 5s by the React dashboard (frontend live-trade page,
# refetchInterval: 5_000) and previously did an unbounded LIVE-fleet query +
# an O(N) per-strategy disk stat()/parse + equity-curve float conversion on
# EVERY poll. We cache the assembled payload behind a short TTL keyed on the
# max paper-trade mtime across the fleet, so a poll that arrives before the
# TTL expires AND before any strategy's snapshot changed reuses the payload.
# Mirrors the _cached_exchange_ping pattern above. Additive: identical
# response shape, just fewer recomputations.
_DASH_CACHE_LOCK = threading.Lock()
_DASH_CACHE: Dict[str, Any] = {"ts": 0.0, "key": None, "payload": None}
_DASH_CACHE_TTL_SEC = 4.0

# P15/D-H4 — Position-magnitude epsilon: accumulated fills can leave ~1e-10
# residue in a "flat" position; 1e-9 keeps flat-detection meaningful without
# false negatives. NOT a divisor — do NOT tighten to 1e-14 (would surface
# phantom 1e-10 BTC positions). Divisor checks (unrealized_pnl_pct denominator)
# use 1e-14 separately.
_POSITION_EPS = 1e-9


def _cached_exchange_ping() -> Any:
    """Return a cached ExchangeAdapter().ping() result (10s TTL)."""
    import time as _time
    now = _time.time()
    with _PING_CACHE_LOCK:
        ts = float(_PING_CACHE.get("ts") or 0.0)
        payload = _PING_CACHE.get("payload")
        if payload is not None and (now - ts) < _PING_CACHE_TTL_SEC:
            return payload
    # Cache miss / stale — call out. We do this OUTSIDE the lock so a slow
    # ping doesn't serialize every reader; the small window of duplicate
    # computes during a cold start is acceptable (still bounded by 1 every
    # 10s steady-state).
    try:
        fresh = ExchangeAdapter().ping()
    except Exception as _ping_exc:  # noqa: BLE001
        import logging as _logging
        _logging.getLogger("alpha.live_trade").warning(
            "_cached_exchange_ping: ping failed, returning disconnected fallback: %s",
            _ping_exc,
        )
        from backend.core.exchange_adapter import ExchangePing as _ExchangePing, configured_venue as _cv
        return _ExchangePing(
            status="disconnected",
            latency_ms=None,
            venue=_cv() or None,
            note=str(_ping_exc),
        )
    # P15/D-H10 — capture timestamp AFTER the ping so the TTL counts down
    # from when we actually finished the network call, not when we started.
    # A ping that takes ~3s would otherwise be "3s old" the moment it's
    # cached and consumers 7s later would re-ping unnecessarily.
    fresh_ts = _time.time()
    with _PING_CACHE_LOCK:
        _PING_CACHE["ts"] = fresh_ts
        _PING_CACHE["payload"] = fresh
    return fresh


def _as_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _position_series(paper: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Prefer a position+price bearing series for position/fill derivation.

    The down-sampled ``equity_curve`` only carries {timestamp, equity,
    drawdown}; the engine's per-bar tape (``per_bar``) carries the traded
    signal (as ``signal``) and ``mark_price``. Map it to the {position,
    price} keys the derivation expects. Falls back to equity_curve (which
    yields a flat/no-fill result) when no per-bar tape is available.
    """
    per_bar = paper.get("per_bar") or []
    if per_bar and isinstance(per_bar[0], dict) and "signal" in per_bar[0]:
        return [
            {
                "timestamp": b.get("start_time") or b.get("timestamp"),
                "position": float(b.get("signal", 0.0) or 0.0),
                "price": float(b.get("mark_price", 0.0) or 0.0),
            }
            for b in per_bar
        ]
    return paper.get("equity_curve") or []


def base_ccy() -> str:
    return env_str("LIVE_TRADE_BASE_CCY", "USDT")


def _derive_position_row(strategy_id: int, slug: str, name: str, paper: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Walk equity_curve backward to find current open position. None if flat."""
    if not paper:
        return None
    eq = _position_series(paper)
    if not eq:
        return None
    # Last non-zero position record.
    last = eq[-1]
    pos = float(last.get("position", 0.0) or 0.0)
    if abs(pos) < _POSITION_EPS:
        return None
    # Walk back while the prior bar is the SAME SIGN and non-zero, so scale-ins
    # (e.g. 0 -> 0.5 -> 1.0) stay part of the same open position instead of
    # stopping at the most recent add. Equality of magnitude was wrong: it
    # truncated the run at the first leg whose size differed.
    pos_sign = 1.0 if pos > 0 else -1.0
    open_idx = len(eq) - 1
    while open_idx > 0:
        prev_pos = float(eq[open_idx - 1].get("position", 0.0) or 0.0)
        if abs(prev_pos) < _POSITION_EPS or (1.0 if prev_pos > 0 else -1.0) != pos_sign:
            break
        open_idx -= 1
    entry_pt = eq[open_idx]
    last_price = float(last.get("price", last.get("close", 0.0)) or 0.0)
    # Size-weighted average entry across the open run-length: weight each bar's
    # price by the absolute INCREMENT in same-sign exposure at that bar. Falls
    # back to the first bar's price if no positive increments are seen.
    _w_price_sum = 0.0
    _w_qty_sum = 0.0
    _run_prev = 0.0
    for _j in range(open_idx, len(eq)):
        _pt = eq[_j]
        _ppos = float(_pt.get("position", 0.0) or 0.0)
        _pprice = float(_pt.get("price", _pt.get("close", last_price)) or last_price)
        _inc = abs(_ppos) - abs(_run_prev)
        if _inc > _POSITION_EPS:
            _w_price_sum += _pprice * _inc
            _w_qty_sum += _inc
        _run_prev = _ppos
    if _w_qty_sum > 1e-14:
        entry_price = float(_w_price_sum / _w_qty_sum)
    else:
        entry_price = float(entry_pt.get("price", entry_pt.get("close", last_price)) or last_price)
    unrealized_pnl = (last_price - entry_price) * pos
    unrealized_pnl_pct = (
        ((last_price / entry_price) - 1.0) * (1.0 if pos > 0 else -1.0)
        if entry_price > 1e-14
        else 0.0
    )
    entry_ts = entry_pt.get("timestamp")
    last_ts = last.get("timestamp")
    try:
        held = (datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
                - datetime.fromisoformat(str(entry_ts).replace("Z", "+00:00")))
        holding_hours = round(held.total_seconds() / 3600.0, 2)
    except Exception:  # noqa: BLE001
        holding_hours = None
    return {
        "strategy_id": int(strategy_id),
        "slug": slug,
        "name": name,
        "side": "long" if pos > 0 else "short",
        "qty": abs(pos),
        "entry_ts": entry_ts,
        "entry_price": round(entry_price, 6),
        "last_price": round(last_price, 6),
        "unrealized_pnl": round(unrealized_pnl, 6),
        "unrealized_pnl_pct": round(unrealized_pnl_pct, 6),
        "holding_hours": holding_hours,
        "entry_basis": "size_weighted_equity_curve",
    }


def _today_equity_delta(eq: List[Dict[str, Any]]) -> float:
    """Same-UTC-day marked-equity change for one strategy's equity_curve.

    Returns equity at the last bar minus equity at the first bar whose
    timestamp falls on the current UTC calendar day. If no bar lands on today
    (stale/frozen curve), returns 0.0 — there is genuinely no intraday move to
    report. Uses only the timestamped equity_curve dicts already persisted by
    paper_trader, so it adds no new data dependency.
    """
    if not eq:
        return 0.0
    day_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    base_equity: Optional[float] = None
    last_equity: Optional[float] = None
    for pt in eq:
        ts_raw = pt.get("timestamp")
        if ts_raw is None:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        try:
            equity = float(pt.get("equity", 1.0))
        except (TypeError, ValueError):
            continue
        last_equity = equity
        if ts >= day_start and base_equity is None:
            base_equity = equity
    if base_equity is None or last_equity is None:
        return 0.0
    return last_equity - base_equity


def _derive_recent_fills(strategy_id: int, slug: str, paper: Dict[str, Any], limit: int = 50) -> List[Dict[str, Any]]:
    if not paper:
        return []
    eq = _position_series(paper)
    fills: List[Dict[str, Any]] = []
    prev_pos = 0.0
    for pt in eq:
        pos = float(pt.get("position", 0.0) or 0.0)
        delta = pos - prev_pos
        # P15/D-H4: skip only bars with NO net exposure change. A same-bar
        # close+reopen that nets ~0 delta is still a real round-trip; we keep
        # prev_pos advancing but emit a fill when the bar's own price differs
        # from the prior level. Here the sampled curve only exposes net delta,
        # so a true ~0-delta bar is genuinely indistinguishable — guard the
        # divisor-free skip on |delta| AND treat an explicit reopen flag if the
        # upstream ledger provides one.
        if abs(delta) < _POSITION_EPS and not pt.get("reopened"):
            prev_pos = pos
            continue
        price = float(pt.get("price", pt.get("close", 0.0)) or 0.0)
        fills.append({
            "ts": pt.get("timestamp"),
            "strategy_id": strategy_id,
            "slug": slug,
            "side": "buy" if delta > 0 else "sell",
            "qty_delta": abs(delta),
            "price": price,
            "cash_delta": -delta * price,
        })
        prev_pos = pos
    return fills[-limit:]


def _derive_risk(equities: List[float]) -> Dict[str, Any]:
    """DEPRECATED single-list form, kept for back-compat.

    Prefer ``_derive_risk_v2`` (per-strategy curves). This shim retains
    extra keys (var_95_pct, cvar_95_pct) set to None for shape stability.
    """
    if not equities:
        return {
            "total_exposure_ccy": 0.0,
            "peak_equity": 0.0,
            "current_equity": 0.0,
            "drawdown_from_peak_pct": 0.0,
            "margin_used_ccy": None,
            "var_95": None,
            "var_95_pct": None,
            "cvar_95_pct": None,
        }
    peak = max(equities)
    cur = equities[-1]
    dd_pct = ((cur / peak) - 1.0) if peak > 1e-14 else 0.0
    return {
        "total_exposure_ccy": round(sum(equities), 6),
        "peak_equity": round(peak, 6),
        "current_equity": round(cur, 6),
        "drawdown_from_peak_pct": round(dd_pct, 6),
        "margin_used_ccy": None,
        "var_95": None,
        "var_95_pct": None,
        "cvar_95_pct": None,
    }


def _derive_risk_v2(
    per_strategy_curves: List[List[Any]],
    weights: Optional[Dict[int, float]] = None,
    strategy_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Per-strategy aggregation, timestamp-aligned.

    P30-R6/R7: caller passes ``List[List[Dict]]`` of raw equity_curve dicts
    (``{timestamp, equity, drawdown}``). We assemble a timestamp-aligned
    pandas frame so:

      * portfolio_equity[t] = Σ_i w_i * equity_i[t]
      * peak = portfolio_equity.cummax().iloc[-1]  (NOT Σ_i max_t e_i)
      * portfolio_return[t] = Σ_i w_i * r_i[t]     (NOT concat of per-strat)

    ``weights`` is optional; missing -> equal-weight. ``strategy_ids`` aligns
    indices in ``per_strategy_curves`` to weight keys; missing -> positional.

    VaR_95 / CVaR_95 are percentiles of the WEIGHTED portfolio return series.

    Backward-compat: when called with List[List[float]] (old shape), the
    function degrades to the original behaviour via a fallback.
    """
    import math as _m

    if not per_strategy_curves:
        return {
            "total_exposure_ccy": 0.0,
            "peak_equity": 0.0,
            "current_equity": 0.0,
            "drawdown_from_peak_pct": 0.0,
            "margin_used_ccy": None,
            "var_95": None,
            "var_95_pct": None,
            "cvar_95_pct": None,
        }

    # Detect schema: dicts (new) vs floats (legacy).
    first_curve = next((c for c in per_strategy_curves if c), None)
    is_dict_schema = bool(first_curve) and isinstance(first_curve[0], dict)

    if not is_dict_schema:
        # Legacy float-list path — preserve original semantics with a warning.
        currents: List[float] = []
        # P4-FIX: track the running portfolio sum at each index position so the
        # portfolio peak is the max of the combined series, NOT sum-of-per-strategy
        # peaks (which overestimates denominator and understates drawdown).
        portfolio_by_idx: List[float] = []
        pooled_returns: List[float] = []
        for curve in per_strategy_curves:
            clean = [float(v) for v in curve if isinstance(v, (int, float)) and _m.isfinite(float(v))]
            if not clean:
                continue
            currents.append(clean[-1])
            # Accumulate into portfolio timeline (zero-pad shorter curves on left).
            pad = max(len(portfolio_by_idx), len(clean))
            portfolio_by_idx = [
                (portfolio_by_idx[pad - len(portfolio_by_idx) + i] if i >= pad - len(portfolio_by_idx) else 0.0)
                + (clean[pad - len(clean) + i] if i >= pad - len(clean) else 0.0)
                for i in range(pad)
            ]
            for i in range(1, len(clean)):
                prev = clean[i - 1]
                if prev > 1e-14:
                    pooled_returns.append((clean[i] / prev) - 1.0)
        sum_current = float(sum(currents))
        sum_peak = float(max(portfolio_by_idx)) if portfolio_by_idx else sum_current
        dd_pct = (sum_current / sum_peak) - 1.0 if sum_peak > 1e-14 else 0.0
        var_pct: Optional[float] = None
        cvar_pct: Optional[float] = None
        if len(pooled_returns) >= 20:
            try:
                import numpy as _np
                arr = _np.asarray(pooled_returns, dtype=float)
                arr = arr[_np.isfinite(arr)]
                if arr.size >= 20:
                    v = float(_np.percentile(arr, 5))
                    tail = arr[arr <= v]
                    var_pct = round(v, 6)
                    cvar_pct = round(float(tail.mean()), 6) if tail.size else var_pct
            except Exception:  # noqa: BLE001
                var_pct = None
                cvar_pct = None
        return {
            "total_exposure_ccy": round(sum_current, 6),
            "peak_equity": round(sum_peak, 6),
            "current_equity": round(sum_current, 6),
            "drawdown_from_peak_pct": round(dd_pct, 6),
            "margin_used_ccy": None,
            "var_95": var_pct,
            "var_95_pct": var_pct,
            "cvar_95_pct": cvar_pct,
        }

    # P30-R6/R7 path: timestamp-aligned portfolio path.
    try:
        import pandas as _pd
    except ImportError:
        _pd = None  # type: ignore

    if _pd is None:
        # Degenerate fallback: last value of each curve.
        currents2 = [float(c[-1].get("equity", 0.0) or 0.0) for c in per_strategy_curves if c and isinstance(c[-1], dict)]
        sum_current = float(sum(currents2))
        return {
            "total_exposure_ccy": round(sum_current, 6),
            "peak_equity": round(sum_current, 6),
            "current_equity": round(sum_current, 6),
            "drawdown_from_peak_pct": 0.0,
            "margin_used_ccy": None,
            "var_95": None,
            "var_95_pct": None,
            "cvar_95_pct": None,
        }

    series_map: Dict[int, Any] = {}
    for idx, curve in enumerate(per_strategy_curves):
        if not curve:
            continue
        ts_vals: List[Any] = []
        eq_vals: List[float] = []
        for pt in curve:
            if not isinstance(pt, dict):
                continue
            ts = pt.get("timestamp")
            eq = pt.get("equity")
            if ts is None or eq is None:
                continue
            try:
                eq_f = float(eq)
            except (TypeError, ValueError):
                continue
            if not _m.isfinite(eq_f):
                continue
            ts_parsed = _pd.to_datetime(ts, utc=True, errors="coerce")
            ts_vals.append(ts_parsed)
            eq_vals.append(eq_f)
        if not ts_vals:
            continue
        sid = int((strategy_ids or [])[idx]) if strategy_ids and idx < len(strategy_ids) else idx
        series_map[sid] = _pd.Series(eq_vals, index=_pd.DatetimeIndex(ts_vals)).sort_index()

    if not series_map:
        return {
            "total_exposure_ccy": 0.0,
            "peak_equity": 0.0,
            "current_equity": 0.0,
            "drawdown_from_peak_pct": 0.0,
            "margin_used_ccy": None,
            "var_95": None,
            "var_95_pct": None,
            "cvar_95_pct": None,
        }

    eq_df = _pd.concat(series_map, axis=1).sort_index().ffill().dropna(how="all")
    if eq_df.empty:
        return {
            "total_exposure_ccy": 0.0,
            "peak_equity": 0.0,
            "current_equity": 0.0,
            "drawdown_from_peak_pct": 0.0,
            "margin_used_ccy": None,
            "var_95": None,
            "var_95_pct": None,
            "cvar_95_pct": None,
        }

    sids = list(eq_df.columns)
    if weights and isinstance(weights, dict):
        w = {int(sid): float(weights.get(int(sid), 0.0) or 0.0) for sid in sids}
        wsum = sum(w.values())
        if wsum <= 1e-14:
            w = {sid: 1.0 / len(sids) for sid in sids}
        else:
            w = {k: v / wsum for k, v in w.items()}
    else:
        w = {sid: 1.0 / len(sids) for sid in sids}

    w_series = _pd.Series(w).reindex(sids).fillna(0.0)
    portfolio_equity = (eq_df * w_series).sum(axis=1)
    sum_current = float(portfolio_equity.iloc[-1])
    sum_peak = float(portfolio_equity.cummax().iloc[-1])
    dd_pct = (sum_current / sum_peak) - 1.0 if sum_peak > 1e-14 else 0.0

    ret_df = eq_df.pct_change().dropna(how="all")
    port_ret = (ret_df * w_series).sum(axis=1, skipna=True)
    port_ret = port_ret.replace([float("inf"), float("-inf")], _pd.NA).dropna()

    var_pct = None
    cvar_pct = None
    if len(port_ret) >= 20:
        try:
            import numpy as _np
            arr = _np.asarray(port_ret.values, dtype=float)
            arr = arr[_np.isfinite(arr)]
            if arr.size >= 20:
                v = float(_np.percentile(arr, 5))
                tail = arr[arr <= v]
                var_pct = round(v, 6)
                cvar_pct = round(float(tail.mean()), 6) if tail.size else var_pct
        except Exception:  # noqa: BLE001
            var_pct = None
            cvar_pct = None

    return {
        "total_exposure_ccy": round(sum_current, 6),
        "peak_equity": round(sum_peak, 6),
        "current_equity": round(sum_current, 6),
        "drawdown_from_peak_pct": round(dd_pct, 6),
        "margin_used_ccy": None,
        "var_95": var_pct,
        "var_95_pct": var_pct,
        "cvar_95_pct": cvar_pct,
    }


def dashboard(session: Session) -> Dict[str, Any]:
    """Top-level dashboard payload (one call per page load).

    D-PERF — memoized behind _DASH_CACHE_TTL_SEC keyed on the max paper-trade
    mtime across the live fleet, so the 5s React poll doesn't re-stat/parse
    every strategy on every refresh. Same payload shape as _build_dashboard.
    """
    import time as _time
    try:
        from backend.core import paper_trader
    except ImportError:
        paper_trader = None  # type: ignore

    live_ids: List[int] = [
        int(r[0])
        for r in session.query(AlphaStrategy.id)
        .filter(AlphaStrategy.status.in_(LIVE_STATUSES))
        .all()
    ]
    # Key = (fleet membership, newest snapshot mtime). Any new tick, promote,
    # or retire changes the key and forces a rebuild on the next poll.
    max_mtime = 0.0
    if paper_trader is not None:
        for sid in live_ids:
            try:
                m = paper_trader.latest_mtime(sid)
            except Exception:  # noqa: BLE001
                m = None
            if m is not None and m > max_mtime:
                max_mtime = m
    cache_key = (tuple(sorted(live_ids)), round(max_mtime, 6))

    now = _time.time()
    with _DASH_CACHE_LOCK:
        ts = float(_DASH_CACHE.get("ts") or 0.0)
        if (
            _DASH_CACHE.get("payload") is not None
            and _DASH_CACHE.get("key") == cache_key
            and (now - ts) < _DASH_CACHE_TTL_SEC
        ):
            return _DASH_CACHE["payload"]

    payload = _build_dashboard(session)
    with _DASH_CACHE_LOCK:
        _DASH_CACHE["ts"] = _time.time()
        _DASH_CACHE["key"] = cache_key
        _DASH_CACHE["payload"] = payload
    return payload


def _build_dashboard(session: Session) -> Dict[str, Any]:
    """Assemble the full dashboard payload (uncached). See dashboard()."""
    # Local import to avoid circular dep at boot.
    try:
        from backend.core import paper_trader
    except ImportError:
        paper_trader = None  # type: ignore

    strategies = (
        session.query(AlphaStrategy)
        .filter(AlphaStrategy.status.in_(LIVE_STATUSES))
        .all()
    )
    positions: List[Dict[str, Any]] = []
    recent_fills: List[Dict[str, Any]] = []
    equities: List[float] = []
    # P30-R6/R7: curves now carry raw equity_curve dicts (timestamp+equity)
    # so the risk aggregator can timestamp-align before computing peak/VaR.
    per_strategy_curves: List[List[Any]] = []  # P29-T1, P30-R6
    per_strategy_ids: List[int] = []  # P30-R7 — index-aligned with curves
    strategy_health: List[Dict[str, Any]] = []
    today_pnl_total = 0.0

    for s in strategies:
        paper: Dict[str, Any] = {}
        if paper_trader is not None:
            try:
                paper = paper_trader.latest_for(int(s.id)) or {}
            except Exception:  # noqa: BLE001
                paper = {}
        pos = _derive_position_row(int(s.id), s.slug(), s.name or "", paper)
        eq = paper.get("equity_curve") or []
        today_delta = _today_equity_delta(eq)
        today_pnl_total += today_delta
        if pos is not None:
            pos["today_pnl"] = round(today_delta, 6)
            positions.append(pos)
        recent_fills.extend(_derive_recent_fills(int(s.id), s.slug(), paper, limit=10))
        if eq:
            curve_vals = [float(p.get("equity", 1.0)) for p in eq]
            equities.extend(curve_vals)
            # P30-R6/R7: pass full dicts (with timestamp) so risk aggregator
            # can timestamp-align across strategies.
            per_strategy_curves.append(eq)
            per_strategy_ids.append(int(s.id))
        latest_sharpe = float((paper.get("metrics") or {}).get("annualized_sharpe") or 0.0)
        backtest_sharpe = float((s.metrics() or {}).get("annualized_sharpe") or 0.0)
        # D-M10 — `if backtest_sharpe` is False for backtest_sharpe == 0.0,
        # which silently hides a real "live tracks zero" drift signal. Be
        # explicit about magnitude AND non-None.
        ic_drift = (
            round(latest_sharpe - backtest_sharpe, 4)
            if backtest_sharpe is not None and abs(backtest_sharpe) > 1e-14
            else 0.0
        )
        run_at = paper.get("run_at")
        last_run_age = None
        if run_at:
            try:
                ts = datetime.fromisoformat(str(run_at).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                last_run_age = (datetime.now(timezone.utc) - ts).total_seconds()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "live_trade: failed to parse run_at=%r for strategy",
                    run_at,
                )
                last_run_age = None
        strategy_health.append({
            "sid": int(s.id),
            "slug": s.slug(),
            "status": s.status,
            "last_tick_at": run_at,
            "latest_sharpe": round(latest_sharpe, 4),
            "ic_drift": ic_drift,
            "last_run_age_seconds": last_run_age,
            "is_healthy": bool(paper.get("is_healthy", False)),
        })

    recent_fills.sort(key=lambda f: f.get("ts") or "", reverse=True)
    # P29-T1: prefer per-strategy aggregation; legacy form for empty case.
    # P30-R6/R7: equal-weight fallback used inside _derive_risk_v2; a future
    # change should plumb weights from the saved portfolio composer state.
    risk = (
        _derive_risk_v2(per_strategy_curves, strategy_ids=per_strategy_ids)
        if per_strategy_curves else _derive_risk(equities)
    )

    pnl = {
        "today_realized": 0.0,  # P7 paper-mirror doesn't track realized PnL separately
        # today_* now reflect the genuine same-UTC-day marked-equity change
        # (sum of per-strategy day deltas), NOT lifetime open PnL.
        "today_unrealized": round(today_pnl_total, 6),
        "today_total": round(today_pnl_total, 6),
        # all_time stays the full-window open-position unrealized figure.
        "all_time_total": round(sum(p["unrealized_pnl"] for p in positions), 6),
        "per_strategy": [
            {"sid": p["strategy_id"], "today": p.get("today_pnl", 0.0), "all_time": p["unrealized_pnl"]}
            for p in positions
        ],
    }
    # D-M9 — use cached ping so the 5s React poll doesn't fan out to a
    # network call every refresh.
    ping = _cached_exchange_ping()
    # P17/C-M10 — expose auto-tick state so UI can warn when frozen.
    paper_tick_enabled = env_bool("ALPHA_PAPER_TICK_ENABLED", False)
    return {
        "mode": "live" if is_live_enabled() else "paper",
        "base_ccy": base_ccy(),
        "positions": positions,
        "pnl": pnl,
        "recent_fills": recent_fills[:50],
        "risk": risk,
        "exchange_status": {
            "status": ping.status,
            "latency_ms": ping.latency_ms,
            "venue": ping.venue,
            "note": ping.note,
        },
        "strategies": strategy_health,
        "paper_tick_enabled": paper_tick_enabled,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def pause_all(
    session: Session,
    *,
    actor: str,
    idempotency_key: str,
    request_ip: Optional[str],
    user_agent: Optional[str],
) -> Dict[str, Any]:
    """Flip every SMALL_CAPITAL/LIVE strategy to PAUSED in one transaction.

    The idempotency layer in :mod:`backend.core.idempotency` handles replay
    detection. This function assumes it's being invoked exactly once per key.
    """
    with _PAUSE_LOCK:
        rows = (
            session.query(AlphaStrategy)
            .filter(AlphaStrategy.status.in_(LIVE_STATUSES))
            .with_for_update(read=False)
            .all()
        ) if session.bind and session.bind.dialect.name != "sqlite" else (
            session.query(AlphaStrategy)
            .filter(AlphaStrategy.status.in_(LIVE_STATUSES))
            .all()
        )
        before: List[Dict[str, Any]] = []
        from backend.core.transition_log import record_transition

        # P-Lx: per-row CONDITIONAL UPDATE — only flip to PAUSED if the row is
        # STILL in a LIVE status in the DB at write time. _PAUSE_LOCK is
        # per-process and with_for_update is skipped on SQLite, so a concurrent
        # /retire (which conditionally UPDATEs status->GRAVEYARD) could commit
        # between the SELECT above and this write. The filter(status IN
        # LIVE_STATUSES) guard makes pause idempotent and prevents resurrecting
        # a row that was retired/transitioned out from under us. updated==0 =>
        # the row is no longer LIVE; skip its snapshot + transition entirely.
        claimed: List[int] = []
        for s in rows:
            from_status = s.status
            prior_stage = int(s.stage or 0)
            # Build the pre-pause snapshot from the SELECTed row first.
            try:
                cfg = s.config() or {}
            except Exception:  # noqa: BLE001
                logger.exception("pause_all: failed to read config for sid=%s", int(s.id))
                cfg = {}
            cfg["pre_pause_status"] = from_status
            cfg["pre_pause_stage"] = prior_stage
            cfg["pre_pause_at"] = datetime.now(timezone.utc).isoformat()
            updated = (
                session.query(AlphaStrategy)
                .filter(AlphaStrategy.id == int(s.id))
                .filter(AlphaStrategy.status.in_(LIVE_STATUSES))
                .update(
                    {
                        "status": "PAUSED",
                        "stage": 7,
                        "config_json": json.dumps(cfg, default=str),
                    },
                    synchronize_session=False,
                )
            )
            if not updated:
                # Row was retired/transitioned by a concurrent writer between the
                # SELECT and now — do NOT resurrect it or write a phantom audit row.
                continue
            claimed.append(int(s.id))
            before.append({"id": int(s.id), "status": from_status})
            record_transition(
                session,
                strategy_id=int(s.id),
                from_status=from_status,
                to_status="PAUSED",
                from_stage=prior_stage,
                to_stage=7,
                actor="system",
                reason="pause_all emergency stop",
            )
        # Audit
        # D-M14 — cap the before-snapshot at 100 entries so a pause-all hitting
        # thousands of strategies doesn't blow up the audit payload (and the
        # downstream JSON cell in the DB).
        audit_payload: Dict[str, Any] = {
            "reason": "pause_all",
            "affected_count": len(before),
            "before": before[:100],
        }
        if len(before) > 100:
            audit_payload["before_truncated"] = True
            audit_payload["before_count"] = len(before)
        result_payload = {"paused_count": len(claimed), "ids": claimed}
        if not claimed:
            audit_payload["note"] = "no_live_strategies_found"
        session.add(AuditLog(
            actor=actor or "anonymous",
            action="live_trade.pause_all",
            subject_type="strategies",
            subject_id=None,
            payload_json=json.dumps(audit_payload, default=str),
            response_json=json.dumps(result_payload, default=str),
            request_ip=request_ip,
            user_agent=user_agent,
            idempotency_key=idempotency_key,
            success=True,
        ))
        return result_payload


def resume_one(
    session: Session,
    strategy_id: int,
    *,
    actor: str,
    request_ip: Optional[str],
    user_agent: Optional[str],
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Resume a single PAUSED strategy back to SMALL_CAPITAL."""
    row = session.get(AlphaStrategy, int(strategy_id))
    if row is None:
        raise ValueError(f"strategy {strategy_id} not found")
    if row.status in ("SMALL_CAPITAL", "LIVE"):
        # Already resumed — idempotent no-op. Return current state so a
        # page-reload retry (new idempotency key) does not produce a 409.
        logger.info(
            "live_trade.resume_one: strategy %s already in %r — returning no-op",
            strategy_id,
            row.status,
        )
        session.add(AuditLog(
            actor=actor or "anonymous",
            action="live_trade.resume_noop",
            subject_type="strategy",
            subject_id=str(strategy_id),
            payload_json=json.dumps(
                {"strategy_id": strategy_id, "current_status": row.status},
                default=str,
            ),
            response_json=json.dumps({"noop": True, "status": row.status}, default=str),
            request_ip=request_ip,
            user_agent=user_agent,
            idempotency_key=idempotency_key,
            success=True,
        ))
        return row.to_dict()
    if row.status != "PAUSED":
        raise ValueError(f"strategy {strategy_id} is {row.status!r}, not PAUSED")
    from backend.core.transition_log import record_transition

    from_status = row.status
    # P31-STATE1: restore to pre-pause status; LIVE strategies must come back
    # as LIVE, not be silently demoted to SMALL_CAPITAL. Fall back to
    # SMALL_CAPITAL only for legacy rows paused before this field existed.
    cfg = row.config() or {}
    target_status = str(cfg.get("pre_pause_status") or "SMALL_CAPITAL").upper()
    if target_status not in ("SMALL_CAPITAL", "LIVE"):
        target_status = "SMALL_CAPITAL"
    target_stage = 6 if target_status == "LIVE" else 5
    # P32-STATE: conditional UPDATE — only flip PAUSED->target if status is
    # STILL "PAUSED" in DB (a concurrent /retire or /pause-all may have
    # committed while we built target_status). updated==0 => race lost.
    updated = (
        session.query(AlphaStrategy)
        .filter(AlphaStrategy.id == int(strategy_id))
        .filter(AlphaStrategy.status == "PAUSED")
        .update(
            {"status": target_status, "stage": target_stage},
            synchronize_session=False,
        )
    )
    if not updated:
        raise ValueError(
            f"strategy {strategy_id} is no longer PAUSED (concurrent transition)"
        )
    session.refresh(row)
    # P-LOSTUPDATE: re-read config_json AFTER refresh so a concurrent committed
    # config write (tuner / other operator action) is not clobbered by the
    # pre-UPDATE snapshot. The status UPDATE above never touched config_json, so
    # the freshly-loaded row still carries the pre_pause_* keys to pop. (Mirrors
    # the refresh-then-re-read ordering already used in main.py gate-promotion.)
    fresh_cfg = row.config() or {}
    # Clear the snapshot so a future pause-resume cycle re-captures fresh state.
    fresh_cfg.pop("pre_pause_status", None)
    fresh_cfg.pop("pre_pause_stage", None)
    fresh_cfg.pop("pre_pause_at", None)
    row.config_json = json.dumps(fresh_cfg, default=str)
    record_transition(
        session,
        strategy_id=int(strategy_id),
        from_status=from_status,
        to_status=target_status,
        from_stage=7,
        to_stage=target_stage,
        actor="operator",
        reason="resume after pause-all",
    )
    payload = row.to_dict()
    session.add(AuditLog(
        actor=actor or "anonymous",
        action="live_trade.resume",
        subject_type="strategy",
        subject_id=str(int(strategy_id)),
        payload_json=json.dumps({"strategy_id": int(strategy_id)}, default=str),
        response_json=json.dumps(payload, default=str),
        request_ip=request_ip,
        user_agent=user_agent,
        # P31-AUDIT5: preserve idempotency_key in the audit row so retries
        # are reverse-attributable through the AuditLog (caller's lookup_or_record
        # already persists the key but the per-action audit was losing it).
        idempotency_key=idempotency_key,
        success=True,
    ))
    return payload


def recent_audit(session: Session, *, limit: int = 50) -> List[Dict[str, Any]]:
    rows = (
        session.query(AuditLog)
        .filter(AuditLog.action.like("live_trade.%"))
        .order_by(AuditLog.created_at.desc())
        .limit(max(1, min(int(limit), 200)))
        .all()
    )
    return [r.to_dict() for r in rows]


__all__ = [
    "dashboard",
    "pause_all",
    "resume_one",
    "recent_audit",
    "base_ccy",
    "LIVE_STATUSES",
]

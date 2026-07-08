"""Manual order entry domain (P7-11 — /trading-terminal).

Validation + paper fill simulation + position recomputation. Reuses the
existing synthetic BTC CSV for prices; other symbols use deterministic
mock prices (clearly flagged via ``price_source: "mock_synthetic"``).

High-risk module per CLAUDE.md — every state-changing endpoint requires
``Idempotency-Key`` (enforced by :mod:`backend.core.idempotency`) and writes
an :class:`AuditLog` row regardless of outcome.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from sqlalchemy.orm import Session

from backend._envloader import env_bool, env_float, env_int, env_str
from backend.core.database import (
    AlphaStrategy,
    AuditLog,
    ManualFill,
    ManualOrder,
    ManualPosition,
    PROJECT_ROOT,
)

logger = logging.getLogger("alpha.trading_terminal")

DATA_CSV: Path = PROJECT_ROOT / "backend" / "data" / "synthetic_btc.csv"

# Top-20 perp whitelist with deterministic mock ratios (relative to BTC).
_SYMBOLS: List[Dict[str, Any]] = [
    {"symbol": "BTC-USDT", "base": "BTC", "quote": "USDT", "min_qty": 0.0001, "qty_step": 0.0001, "price_tick": 0.01, "ratio": 1.0},
    {"symbol": "ETH-USDT", "base": "ETH", "quote": "USDT", "min_qty": 0.001,  "qty_step": 0.001,  "price_tick": 0.01, "ratio": 0.055},
    {"symbol": "SOL-USDT", "base": "SOL", "quote": "USDT", "min_qty": 0.01,   "qty_step": 0.01,   "price_tick": 0.001,"ratio": 0.0035},
    {"symbol": "BNB-USDT", "base": "BNB", "quote": "USDT", "min_qty": 0.001,  "qty_step": 0.001,  "price_tick": 0.01, "ratio": 0.012},
    {"symbol": "XRP-USDT", "base": "XRP", "quote": "USDT", "min_qty": 1,      "qty_step": 1,      "price_tick": 0.0001,"ratio": 0.000013},
    {"symbol": "ADA-USDT", "base": "ADA", "quote": "USDT", "min_qty": 1,      "qty_step": 1,      "price_tick": 0.0001,"ratio": 0.000010},
    {"symbol": "DOGE-USDT","base": "DOGE","quote": "USDT", "min_qty": 1,      "qty_step": 1,      "price_tick": 0.00001,"ratio":0.0000035},
    {"symbol": "AVAX-USDT","base": "AVAX","quote": "USDT", "min_qty": 0.01,   "qty_step": 0.01,   "price_tick": 0.001,"ratio": 0.0009},
    {"symbol": "LINK-USDT","base": "LINK","quote": "USDT", "min_qty": 0.01,   "qty_step": 0.01,   "price_tick": 0.001,"ratio": 0.0004},
    {"symbol": "MATIC-USDT","base":"MATIC","quote":"USDT", "min_qty": 1,      "qty_step": 1,      "price_tick": 0.0001,"ratio":0.00002},
    {"symbol": "DOT-USDT", "base": "DOT", "quote": "USDT", "min_qty": 0.1,    "qty_step": 0.1,    "price_tick": 0.001,"ratio": 0.00015},
    {"symbol": "TRX-USDT", "base": "TRX", "quote": "USDT", "min_qty": 1,      "qty_step": 1,      "price_tick": 0.00001,"ratio":0.0000022},
    {"symbol": "LTC-USDT", "base": "LTC", "quote": "USDT", "min_qty": 0.01,   "qty_step": 0.01,   "price_tick": 0.01, "ratio": 0.0017},
    {"symbol": "BCH-USDT", "base": "BCH", "quote": "USDT", "min_qty": 0.01,   "qty_step": 0.01,   "price_tick": 0.01, "ratio": 0.0085},
    {"symbol": "NEAR-USDT","base": "NEAR","quote": "USDT", "min_qty": 0.1,    "qty_step": 0.1,    "price_tick": 0.001,"ratio": 0.0001},
    {"symbol": "ATOM-USDT","base": "ATOM","quote": "USDT", "min_qty": 0.01,   "qty_step": 0.01,   "price_tick": 0.001,"ratio": 0.00025},
    {"symbol": "ARB-USDT", "base": "ARB", "quote": "USDT", "min_qty": 0.1,    "qty_step": 0.1,    "price_tick": 0.0001,"ratio":0.00002},
    {"symbol": "OP-USDT",  "base": "OP",  "quote": "USDT", "min_qty": 0.1,    "qty_step": 0.1,    "price_tick": 0.0001,"ratio":0.00003},
    {"symbol": "SUI-USDT", "base": "SUI", "quote": "USDT", "min_qty": 0.1,    "qty_step": 0.1,    "price_tick": 0.0001,"ratio":0.00004},
    {"symbol": "APT-USDT", "base": "APT", "quote": "USDT", "min_qty": 0.01,   "qty_step": 0.01,   "price_tick": 0.001,"ratio": 0.0001},
]
_SYMBOL_INDEX: Dict[str, Dict[str, Any]] = {s["symbol"]: s for s in _SYMBOLS}


def is_enabled() -> bool:
    return env_bool("TRADING_TERMINAL_ENABLED", False)


def rate_limit_per_min() -> int:
    return env_int("TRADING_TERMINAL_RATE_LIMIT_PER_MIN", 30, minimum=1, maximum=600)


def fat_finger_pct() -> float:
    # P29-T7: default tightened from 0.20 to 0.03 (3%). Legacy env var name
    # preserved; new TRADING_FAT_FINGER_PCT name accepted for symmetry.
    legacy = env_float("TRADING_TERMINAL_FAT_FINGER_PCT", 0.03, minimum=0.001, maximum=1.0)
    return env_float("TRADING_FAT_FINGER_PCT", legacy, minimum=0.001, maximum=1.0)


def max_order_notional_usdt() -> float:
    return env_float("TRADING_MAX_ORDER_NOTIONAL_USDT", 50_000.0, minimum=0.0)


def max_quote_drift_pct() -> float:
    return env_float("TRADING_MAX_QUOTE_DRIFT_PCT", 0.005, minimum=0.0, maximum=1.0)


def max_quote_age_sec() -> float:
    # P30-T6: stale-quote TTL. CSV mtime older than this -> snapshot flagged
    # stale; validate_order rejects with STALE_QUOTE. Default 60s matches
    # typical bar cadence.
    #
    # P32-T4: TRADING_MAX_QUOTE_AGE_SEC=0 disables the staleness signal, which
    # also disables the fat-finger drift reference integrity (validate_order
    # uses the same `last`). Disabling it is only legitimate for deterministic
    # backtest replay where file mtime is unreliable. To prevent a single
    # mis-set env from silently turning off a core safety check in this
    # HIGH-RISK module, only honour 0 when TRADING_REPLAY_MODE is explicitly
    # truthy (whitelist-bool per CLAUDE.md); otherwise clamp to a sane floor.
    age = env_float("TRADING_MAX_QUOTE_AGE_SEC", 60.0, minimum=0.0, maximum=86400.0)
    if age <= 0.0 and not env_bool("TRADING_REPLAY_MODE", False):
        return 60.0
    return age


def maker_bps() -> float:
    return env_float("TRADING_TERMINAL_MAKER_BPS", 10.0, minimum=0.0, maximum=1000.0)


def taker_bps() -> float:
    return env_float("TRADING_TERMINAL_TAKER_BPS", 10.0, minimum=0.0, maximum=1000.0)


def default_cash() -> float:
    return env_float("TRADING_TERMINAL_DEFAULT_CASH_USDT", 100000.0, minimum=0.0)


def enforce_balance() -> bool:
    # HIGH-RISK guard (CLAUDE.md): when enabled, paper buys are rejected if
    # they would drive derived buying-power negative. Default ON so the
    # advertised `default_cash_usdt` budget is actually binding (a fully-
    # committed account cannot over-spend); set
    # TRADING_TERMINAL_ENFORCE_BALANCE=0 to disable the guard (e.g. replay/
    # backtest harnesses that intentionally over-commit paper cash).
    return env_bool("TRADING_TERMINAL_ENFORCE_BALANCE", True)


def supported_symbols() -> List[Dict[str, Any]]:
    """Symbol list enriched with latest price + 24h delta."""
    out: List[Dict[str, Any]] = []
    last_btc, vol_24h, change_24h, stale = _btc_market_snapshot()
    for s in _SYMBOLS:
        last = round(last_btc * float(s["ratio"]), 6) if s["symbol"] != "BTC-USDT" else last_btc
        out.append({
            **s,
            "last": last,
            "change_24h_pct": change_24h,
            "vol_24h": vol_24h * float(s["ratio"]),
            "price_source": "csv" if s["symbol"] == "BTC-USDT" else "mock_synthetic",
            "quote_stale": bool(stale),
        })
    return out


def market_info(symbol: str) -> Dict[str, Any]:
    s = _SYMBOL_INDEX.get(symbol)
    if s is None:
        raise ValueError(f"symbol {symbol!r} not supported")
    last_btc, vol_24h, change_24h, stale = _btc_market_snapshot()
    last = round(last_btc * float(s["ratio"]), 6) if symbol != "BTC-USDT" else last_btc
    bid = round(last * 0.9995, 6)
    ask = round(last * 1.0005, 6)
    spread_bps = round(((ask - bid) / last) * 10000.0, 2) if last > 1e-14 else 0.0
    return {
        "symbol": symbol,
        "last": last,
        "bid": bid,
        "ask": ask,
        "spread_bps": spread_bps,
        "vol_24h": vol_24h * float(s["ratio"]),
        "change_24h_pct": change_24h,
        "ts": datetime.now(timezone.utc).isoformat(),
        "price_source": "csv" if symbol == "BTC-USDT" else "mock_synthetic",
        "quote_stale": bool(stale),
    }


def _btc_market_snapshot() -> tuple[float, float, float, bool]:
    """Return (last, vol_24h, change_24h, stale).

    P30-T6: ``stale`` is True when CSV is missing, parse failed, or the file
    mtime is older than ``max_quote_age_sec()``. ``validate_order`` rejects
    orders against stale quotes.
    """
    import time as _time
    if not DATA_CSV.exists():
        # P29-S7: previously silent — masquerading 50k as real price for
        # trade validation. Log per call so ops can correlate fallbacks.
        logger.warning(
            "trading_terminal: BTC snapshot fallback — DATA_CSV missing at %s",
            DATA_CSV,
        )
        return 50000.0, 1000.0, 0.0, True
    try:
        # P31-MKT11: tail(25) covers 25 hourly bars to enable a 24h delta
        # (last vs 24-bars-ago). Sum volume over the LAST 24 bars (iloc[1:]
        # of tail(25)) for true 24h turnover — summing all 25 bars produced
        # a 25h window mislabelled as 24h.
        df = pd.read_csv(DATA_CSV).tail(25)
        last = float(df["close"].iloc[-1])
        first = float(df["close"].iloc[0])
        vol_24h = float(df["volume"].iloc[1:].sum()) if "volume" in df.columns else 0.0
        change_24h = ((last / first) - 1.0) if first > 1e-14 else 0.0
        # P30-T6: TTL on CSV mtime. We have no per-bar timestamp, so file
        # mtime is the cheapest staleness proxy.
        max_age = max_quote_age_sec()
        stale = False
        if max_age > 0.0:
            try:
                mtime = DATA_CSV.stat().st_mtime
                if (_time.time() - mtime) > max_age:
                    stale = True
                    logger.warning(
                        "trading_terminal: BTC snapshot stale — mtime age %.1fs > TTL %.1fs",
                        _time.time() - mtime,
                        max_age,
                    )
            except OSError:
                stale = True
        return last, vol_24h, round(change_24h, 6), stale
    except Exception:  # noqa: BLE001
        # P29-S7: corrupt CSV — same fallback but log root cause.
        logger.exception(
            "trading_terminal: BTC snapshot fallback — failed to parse %s",
            DATA_CSV,
        )
        return 50000.0, 1000.0, 0.0, True


def _is_market_halted(symbol: str) -> bool:
    """P18/D2 circuit-breaker hook — returns True if the symbol is currently
    halted and orders must be rejected.

    Default implementation: returns False unless the symbol appears in the
    comma/semicolon-separated ``ALPHA_MARKET_HALTED_SYMBOLS`` env var (manual
    override for ops). Intended as an extension point — wire in venue-level
    circuit-breaker / halt feeds (Binance ``/sapi/v1/system/status``, OKX
    ``/api/v5/system/status``, etc.) by replacing the body. CLAUDE.md global
    rule for trading-domain modules requires this defensive guard.
    """
    from backend._envloader import env_str_set
    halted = env_str_set("ALPHA_MARKET_HALTED_SYMBOLS")
    if not halted:
        return False
    return symbol.strip().upper() in {s.strip().upper() for s in halted}


# ---- Validation -----------------------------------------------------------


VALID_SIDES = {"buy", "sell"}
# P15/D-L8 — 'stop_limit' is intentionally NOT in VALID_TYPES. If a desk needs
# stop-limit behaviour, implement it as a two-step (stop trigger + limit
# submit) at the orchestrator layer rather than adding a single VALID_TYPES
# entry; the venue specs in exchange_adapter would need parallel work and
# the manual-order reaper would otherwise mis-classify them.
VALID_TYPES = {"market", "limit", "stop"}
VALID_TIFS = {"gtc", "ioc", "fok"}


def validate_order(payload: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    sym = (payload.get("symbol") or "").strip().upper()
    side = (payload.get("side") or "").strip().lower()
    otype = (payload.get("order_type") or "").strip().lower()
    tif = (payload.get("tif") or "gtc").strip().lower()
    mode = (payload.get("mode") or "paper").strip().lower()

    spec = _SYMBOL_INDEX.get(sym)
    if spec is None:
        errors.append(f"symbol {sym!r} not supported")
    elif _is_market_halted(sym):
        # P18/D2 — circuit-breaker / market-halt guard. Halts pre-empt every
        # subsequent check; we still let downstream validators run so the
        # caller sees the full diagnostic list (matches existing style of
        # accumulating errors rather than early-return).
        errors.append(f"MARKET_HALTED:{sym}")
    if side not in VALID_SIDES:
        errors.append(f"side must be one of {sorted(VALID_SIDES)}")
    if otype not in VALID_TYPES:
        errors.append(f"order_type must be one of {sorted(VALID_TYPES)}")
    if tif not in VALID_TIFS:
        errors.append(f"tif must be one of {sorted(VALID_TIFS)}")
    if otype == "market" and tif != "ioc":
        errors.append("market orders must use tif=ioc")
    if otype == "stop" and tif in ("ioc", "fok"):
        errors.append("stop orders only support tif=gtc; ioc/fok semantics for stop orders are not implemented")
    if mode not in ("paper", "live"):
        errors.append(f"mode must be paper|live")
    if mode == "live" and not env_bool("LIVE_TRADE_ENABLED", False):
        errors.append("live mode requires LIVE_TRADE_ENABLED=1")
    # Live routing is not yet implemented (M14). Block at validation so the
    # idempotency layer never records a stub attempt and callers receive a
    # clear 422 validation error rather than a 501 after session registration.
    if mode == "live":
        errors.append("LIVE_ROUTING_NOT_IMPLEMENTED: live order routing is deferred to M14")

    # qty / price using Decimal to avoid float drift on step checks.
    try:
        qty_d = Decimal(str(payload.get("qty", "0")))
        # P31-NUM1: Decimal('NaN') passes the constructor but raises
        # InvalidOperation on any comparison (Decimal('nan') > 0 raises);
        # reject NaN/Inf here so qty_d > 0 / qty_d % step_d don't crash.
        if qty_d.is_nan() or qty_d.is_infinite():
            qty_d = Decimal("0")
            errors.append("qty must be a finite number")
    except Exception:  # noqa: BLE001
        qty_d = Decimal("0")
        errors.append("qty must be numeric")
    if spec is not None and qty_d > 0:
        # P15/D-M12 — Decimal step check. We compare `qty_d % step_d` against
        # 0 using Decimal arithmetic so the test isn't subject to float drift
        # (e.g. 0.1 + 0.2 != 0.3 in float; exact in Decimal). The `step_d > 0`
        # guard protects against a misconfigured spec that would otherwise
        # raise ZeroDivisionError inside the modulo.
        step_d = Decimal(str(spec["qty_step"]))
        min_d = Decimal(str(spec["min_qty"]))
        if qty_d < min_d:
            errors.append(f"qty {qty_d} < min_qty {min_d}")
        # P30-T7: tolerance band on the step check. JSON floats round-trip
        # through `str()` as e.g. '0.30000000000000004', so the strict
        # `(qty_d % step_d) != 0` rejected legitimate UI inputs (qty=balance*pct
        # from JS). 0.1% of step is well below any venue resolution and still
        # catches order-of-magnitude misalignments (qty=0.15 vs step=0.1 gives
        # residue 50% of step -> still rejected).
        if step_d > 0:
            residue = qty_d % step_d
            tol = step_d * Decimal("0.001")
            if residue > tol and (step_d - residue) > tol:
                errors.append(f"qty {qty_d} not aligned to step {step_d}")
    if qty_d <= 0:
        errors.append("qty must be > 0")

    limit_price = payload.get("limit_price")
    stop_price = payload.get("stop_price")
    if otype == "limit" and limit_price is None:
        errors.append("limit order requires limit_price")
    if otype == "stop" and stop_price is None:
        errors.append("stop order requires stop_price")

    if spec is not None:
        _mi = market_info(sym)
        last = _mi["last"]
        # P30-T6: stale-quote guard. Refuse to validate an order against
        # a price feed older than TRADING_MAX_QUOTE_AGE_SEC — silently
        # accepting a stale CSV mark allowed orders that drift 40%+ from
        # real market and bypassed the fat-finger cap entirely.
        if _mi.get("quote_stale"):
            errors.append(f"STALE_QUOTE:{sym}")
            # P32-T3: also clear `last` so the downstream drift/fat-finger
            # block does not emit a misleading "price X > N% from last Y"
            # error computed against a stale quote (the STALE_QUOTE error is
            # the actionable one — the drift number is meaningless).
            last = None
    else:
        last = None
    # Stop-price direction sanity: a sell-stop above current mark triggers
    # immediately (converts to market sell); a buy-stop below mark also
    # triggers immediately. Both are almost certainly operator errors.
    if otype == "stop" and stop_price is not None and last is not None and last > 1e-14:
        try:
            sp = float(stop_price)
            if math.isfinite(sp) and sp > 0:
                if side == "sell" and sp >= float(last):
                    errors.append(
                        f"sell-stop stop_price {sp} >= mark {last}: would trigger immediately"
                    )
                elif side == "buy" and sp <= float(last):
                    errors.append(
                        f"buy-stop stop_price {sp} <= mark {last}: would trigger immediately"
                    )
        except (TypeError, ValueError):
            pass  # non-numeric stop_price already caught above
    fat_warn = False
    # P29-T7: fat-finger applies to ALL order types including market.
    if last is not None and last > 1e-14:
        if otype in ("limit", "stop"):
            price_for_ref = limit_price if otype == "limit" else stop_price
        else:
            # Market order: caller may supply quote_price for venue-quoted
            # indicative; falls back to last (always within cap by def).
            price_for_ref = payload.get("quote_price", last)
        if price_for_ref is None:
            ref = float(last)
        else:
            try:
                ref = float(price_for_ref)
                # P31-NUM7: float("nan")/float("inf")/float("Infinity")
                # succeed silently and return real non-finite floats. Reject
                # them here so the fat-finger / notional checks downstream
                # receive a usable ref.
                if not math.isfinite(ref):
                    raise ValueError("non-finite price")
            except (TypeError, ValueError):
                ref = float(last)
                errors.append("price must be a finite number")
        # P31-NUM2: ``ref <= 0`` is False for NaN/inf (NaN <= 0 is False),
        # so a non-finite ref slips through and then drift = NaN bypasses
        # both the fat-finger and >5% checks. Require finiteness explicitly.
        if not math.isfinite(ref) or ref <= 0:
            errors.append("price must be a finite number > 0")
        else:
            drift = abs(ref / last - 1.0)
            if drift > fat_finger_pct():
                errors.append(
                    f"price {ref} > {fat_finger_pct() * 100:.2f}% from last {last}"
                )
            elif drift > 0.05:
                fat_warn = True

    # P29-T7: max order notional cap (paper + live).
    # P-TOUCH1: a MARKET order fills at the executable touch (ask for buy /
    # bid for sell), never the mid — see submit_paper L581-583. Check the cap
    # against the touch so a near-cap market order can't slip over the cap by
    # the half-spread. Slippage is excluded here (it's size-dependent and only
    # widens the fill further; the touch alone is the deterministic floor).
    notional_ref: Optional[float] = None
    if last is not None and qty_d > 0:
        if otype == "market" and spec is not None and _mi.get("bid") is not None and _mi.get("ask") is not None:
            try:
                touch = float(_mi["ask"]) if side == "buy" else float(_mi["bid"])
            except (TypeError, ValueError, KeyError):
                touch = float(last)
            ref_for_cap = touch if math.isfinite(touch) and touch > 0 else float(last)
        else:
            ref_for_cap = float(last)
        notional_ref = float(qty_d) * ref_for_cap
    elif qty_d > 0 and (limit_price is not None or stop_price is not None):
        try:
            notional_ref = float(qty_d) * float(limit_price if limit_price is not None else stop_price)
        except (TypeError, ValueError):
            notional_ref = None
    cap = max_order_notional_usdt()
    if notional_ref is not None and cap > 0.0 and notional_ref > cap:
        errors.append(
            f"order notional {notional_ref:.2f} USDT exceeds cap {cap:.2f} USDT"
        )

    # P29-T7: market quote_price drift cap.
    if otype == "market" and last is not None and "quote_price" in payload:
        try:
            qp = float(payload.get("quote_price"))
        except (TypeError, ValueError):
            qp = None
        # P31-NUM3: ``qp > 0`` is False for NaN (NaN > 0 is False), letting
        # a non-finite quote_price slip past the drift cap silently. Require
        # finiteness so the cap stays effective for malformed payloads.
        if qp is not None and math.isfinite(qp) and qp > 0 and last > 1e-14:
            drift_q = abs(qp / last - 1.0)
            cap_q = max_quote_drift_pct()
            if cap_q > 0.0 and drift_q > cap_q:
                errors.append(
                    f"quote_price drift {drift_q * 100:.3f}% exceeds cap {cap_q * 100:.3f}%"
                )

    return {
        "ok": not errors,
        "errors": errors,
        "fat_finger_warning": fat_warn,
        "mark_price": last,
        "symbol_spec": spec,
    }


def estimate_fees(order_type: str, side: str, qty: float, price: float, tif: str = "gtc") -> Dict[str, Any]:
    # R5/QT-8: IOC/FOK limit orders cross the book and remove liquidity -> taker.
    # `tif` defaults to "gtc" so existing callers keep maker-for-limit behaviour.
    is_maker = order_type == "limit" and (tif or "gtc").lower() not in ("ioc", "fok")
    bps = maker_bps() if is_maker else taker_bps()
    notional = abs(qty * price)
    fee_quote = notional * (bps / 10000.0)
    return {
        "is_maker": is_maker,
        "fee_bps": bps,
        "fee_quote": round(fee_quote, 6),
    }


def estimate_slippage(symbol: str, qty: float) -> Dict[str, Any]:
    info = market_info(symbol)
    if info.get("quote_stale"):
        return {"slippage_warning": False, "est_slippage_bps": 0.0}
    # R5/QT-3: vol_24h is stored as btc_raw_vol * ratio (base-asset units) and
    # info["last"] is btc_price * ratio, so info["vol_24h"] * info["last"] double-
    # applies `ratio` (ratio^2) and collapses the alt USDT-volume estimate (e.g.
    # ETH ~18x too small, XRP ~77000x), making nearly every alt order trip the
    # slippage warning and hit the 50 bps cap. Recover `ratio` from _SYMBOL_INDEX
    # and divide it back out so the quote volume is btc_raw_vol * btc_price * ratio
    # (the alt's true USDT turnover). BTC has ratio=1.0, so it is unchanged.
    _s = _SYMBOL_INDEX.get(symbol)
    _ratio = float(_s["ratio"]) if _s is not None else 1.0
    if not (_ratio > 1e-30):
        _ratio = 1.0
    vol_24h_quote = info["vol_24h"] * info["last"] / _ratio
    hourly = vol_24h_quote / 24.0
    notional = abs(qty * info["last"])
    threshold = 0.10 * hourly
    warning = notional > threshold
    # D-M5 — subnormal-safe denominator. `max(threshold, 1e-9)` still divides
    # by an effectively-zero number when hourly volume is degenerate; clamp
    # explicitly to a non-subnormal floor.
    denom = threshold if threshold > 1e-14 else 1e-14
    est_bps = (notional / denom) * 2.0  # rough heuristic
    return {
        "slippage_warning": bool(warning),
        "est_slippage_bps": round(min(est_bps, 50.0), 2),
    }


def preview(payload: Dict[str, Any]) -> Dict[str, Any]:
    val = validate_order(payload)
    sym = (payload.get("symbol") or "").upper()
    side = (payload.get("side") or "buy").lower()
    otype = (payload.get("order_type") or "market").lower()
    qty = float(payload.get("qty", 0.0) or 0.0)
    last = val.get("mark_price") or 0.0
    ref_price = float(
        payload.get("limit_price")
        or payload.get("stop_price")
        or last
        or 0.0
    )
    slip = estimate_slippage(sym, qty)
    # P-TOUCH2: for a MARKET order the actual debit is touch*(1+/-slip)+fee on
    # the fill price (submit_paper L581-588), not mid. Compute est_cost from the
    # same touch+slippage so the operator's "Est cost" matches what will be
    # charged. Limit/stop keep ref_price (their resting/limit price).
    if otype == "market" and last > 1e-14:
        try:
            _mi_p = market_info(sym)
            touch = float(_mi_p["ask"]) if side == "buy" else float(_mi_p["bid"])
        except (ValueError, KeyError, TypeError):
            touch = ref_price
        if not (math.isfinite(touch) and touch > 0):
            touch = ref_price
        slip_bps = float(slip["est_slippage_bps"])
        fill_ref = touch * (1.0 + (slip_bps / 10000.0) * (1 if side == "buy" else -1))
        fees = estimate_fees(otype, side, qty, fill_ref, tif=(payload.get("tif") or "gtc").lower())
        # For a buy: operator debits notional + fee.
        # For a sell: operator receives notional - fee (net proceeds).
        if side == "buy":
            est_cost = abs(qty * fill_ref) + fees["fee_quote"]
        else:
            est_cost = abs(qty * fill_ref) - fees["fee_quote"]
    else:
        fees = estimate_fees(otype, side, qty, ref_price, tif=(payload.get("tif") or "gtc").lower())
        if side == "buy":
            est_cost = abs(qty * ref_price) + fees["fee_quote"]
        else:
            est_cost = abs(qty * ref_price) - fees["fee_quote"]
    return {
        "ok": val["ok"],
        "validation": val["errors"],
        "est_cost_quote": round(est_cost, 6),
        "est_fee_quote": fees["fee_quote"],
        "est_fee_bps": fees["fee_bps"],
        "est_slippage_bps": slip["est_slippage_bps"],
        "slippage_warning": slip["slippage_warning"],
        "fat_finger_warning": val["fat_finger_warning"],
        "mark_price": last,
        "mode": (payload.get("mode") or "paper").lower(),
    }


# ---- Submit / fill / cancel ----------------------------------------------


def _recompute_position(session: Session, symbol: str, mode: str) -> ManualPosition:
    """Recompute (qty_signed, avg_entry, realized_pnl) from all fills."""
    # P-TT-LOCK: on lock-capable backends (Postgres/MySQL) acquire a row lock
    # on the ManualPosition row so concurrent submit/reap recomputes for the
    # same (symbol,mode) serialize the read-modify-write of the position
    # snapshot. No-op on SQLite (single-writer WAL serializes writes anyway);
    # mirrors auto_pipeline.py:160-166 and live_trade_ops.py:534. Locking only
    # covers an EXISTING row — the create-from-flat path below is unchanged and
    # the ManualPosition UniqueConstraint(symbol,mode) still prevents duplicate
    # rows on the rare concurrent first-insert.
    _pos_q = session.query(ManualPosition).filter(
        ManualPosition.symbol == symbol, ManualPosition.mode == mode
    )
    if session.bind is not None and session.bind.dialect.name != "sqlite":
        _pos_q = _pos_q.with_for_update(of=ManualPosition, nowait=False)
    pos = _pos_q.one_or_none()
    if pos is None:
        pos = ManualPosition(symbol=symbol, mode=mode)
        session.add(pos)
    qty_signed = 0.0
    avg_entry = 0.0
    realized = 0.0
    fills = (
        session.query(ManualFill, ManualOrder)
        .join(ManualOrder, ManualFill.order_id == ManualOrder.id)
        .filter(ManualOrder.symbol == symbol, ManualOrder.mode == mode)
        # P31-T2: exclude fills attached to cancelled/rejected/orphaned orders
        # — those rows shouldn't contribute to position state. The cancel-vs-
        # reaper race (now closed by STATE-4) could leave a stray fill on a
        # cancelled order; even with STATE-4 in place, this filter is the
        # defence-in-depth that keeps position state honest.
        #
        # Partial fills are NOT supported in this paper engine: every fill
        # path (market, limit-IOC, limit-FOK, GTC-reap, stop-reap) writes
        # filled_qty == order.qty and sets status='filled' or fully
        # cancels/rejects. There is therefore no reachable 'partial' status —
        # it was removed from this filter to stop implying a feature that does
        # not exist. IOC currently behaves all-or-nothing (same as FOK) rather
        # than filling the crossable portion and cancelling the remainder.
        .filter(ManualOrder.status == "filled")
        # P31-AUDIT10: stable tiebreaker so same-millisecond fills produce
        # deterministic VWAP / realized-PnL reconstructions.
        .order_by(ManualFill.filled_at.asc(), ManualFill.id.asc())
        .all()
    )
    for fill, order in fills:
        sign = 1 if order.side == "buy" else -1
        size = sign * float(fill.filled_qty)
        price = float(fill.filled_price)
        if qty_signed * size >= 0:
            # Same-direction or opening from flat — VWAP entry.
            new_qty = qty_signed + size
            if abs(new_qty) < 1e-14:
                avg_entry = 0.0
            else:
                avg_entry = (avg_entry * abs(qty_signed) + price * abs(size)) / abs(new_qty)
            qty_signed = new_qty
        else:
            # Reducing or flipping — realize PnL on the offset portion.
            offset = min(abs(qty_signed), abs(size))
            if qty_signed > 0:
                # Closing long → profit = (sell_price - avg_entry) * offset
                realized += (price - avg_entry) * offset
            else:
                realized += (avg_entry - price) * offset
            qty_signed += size  # signed addition handles flip
            if qty_signed * (qty_signed - size) < 0:
                # Flipped through flat; reset avg to current fill price.
                avg_entry = price
        # Fees reduce realized PnL regardless of direction.
        realized -= float(fill.fee_quote)
    pos.qty_signed = qty_signed
    pos.avg_entry_price = avg_entry
    pos.realized_pnl_quote = realized
    return pos


def _paper_available_cash(session: Session, mode: str = "paper") -> float:
    """Derived buying-power = default_cash() minus net quote consumed by all
    paper fills (buys consume, sells return) minus fees. Mirrors the
    status/order filter used by _recompute_position so cancelled/rejected/
    orphaned orders never affect cash. No DB schema change — pure derivation.
    """
    fills = (
        session.query(ManualFill, ManualOrder)
        .join(ManualOrder, ManualFill.order_id == ManualOrder.id)
        .filter(ManualOrder.mode == mode)
        .filter(ManualOrder.status == "filled")
        .all()
    )
    cash = default_cash()
    for fill, order in fills:
        sign = 1.0 if order.side == "buy" else -1.0
        cash -= sign * float(fill.filled_qty) * float(fill.filled_price)
        cash -= float(fill.fee_quote)
    # Reserve quote already committed by pending buy orders (not yet filled).
    # Without this deduction, concurrent submit_paper calls for GTC/stop buys
    # all read the same `cash` value (based only on filled orders) and each
    # pass the balance guard independently, allowing N×cost to be committed
    # against a single budget.  No schema change — pure derivation over
    # existing columns (limit_price, stop_price, qty, status, side, mode).
    pending_buys = (
        session.query(ManualOrder)
        .filter(
            ManualOrder.mode == mode,
            ManualOrder.status == "pending",
            ManualOrder.side == "buy",
        )
        .all()
    )
    for o in pending_buys:
        # Use limit_price for limit orders, stop_price for stop orders.
        # If neither is set (should not occur for a resting order) skip safely.
        ref = float(o.limit_price or o.stop_price or 0.0)
        # QT-27 (ongoing reservation): for a buy-stop the reaper fills at
        # max(sp, ask) — mirrors submit_paper line 736.  Update the reservation
        # each tick so sibling orders are not approved against a stale (too-low)
        # stop_price when the market has moved above the stop level.
        if (
            o.order_type == "stop"
            and o.side == "buy"
            and ref > 1e-14
        ):
            try:
                _info = market_info(o.symbol)
                ask_now = float(_info.get("ask") or ref)
                ref = max(ref, ask_now)
            except Exception:
                pass  # symbol lookup failure: keep stop_price as conservative floor
        if ref > 1e-14:
            notional = abs(float(o.qty) * ref)
            cash -= notional
            # Also reserve the estimated fee so this mirrors the submit_paper
            # balance check (line 734: required = qty*ref + fee_quote).
            # Without this, N pending buys over-report available cash by ΣFee_i.
            fees = estimate_fees(
                o.order_type or "limit",
                o.side,
                float(o.qty),
                ref,
                tif=(o.tif or "gtc"),
            )
            cash -= float(fees["fee_quote"])
    return cash


def submit_paper(
    session: Session,
    *,
    payload: Dict[str, Any],
    terminal_session_id: Optional[str],
    request_ip: Optional[str],
    user_agent: Optional[str],
    idempotency_key: str,
) -> Dict[str, Any]:
    """Validate + persist order + simulate fill (paper mode only)."""
    val = validate_order(payload)
    if not val["ok"]:
        raise ValueError("; ".join(val["errors"]))

    sym = payload["symbol"].upper()
    side = payload["side"].lower()
    otype = payload["order_type"].lower()
    qty = float(payload["qty"])
    tif = (payload.get("tif") or "gtc").lower()
    mode = (payload.get("mode") or "paper").lower()
    limit_price = payload.get("limit_price")
    stop_price = payload.get("stop_price")

    if mode == "live":
        # Live path: not yet wired — raise to caller (idempotency layer logs).
        raise NotImplementedError("Live order routing deferred (M14)")

    last = val["mark_price"] or 0.0
    # Use the worst-case fill reference price for the balance pre-check:
    # - limit order  → limit_price (resting price)
    # - stop order   → stop_price (worst-case trigger; reaper fills at max(sp, ask))
    # - market buy   → ask * (1 + est_slippage): R5/QT-4 mirrors the actual market
    #                  fill path below (touch = ask, then positive slippage). Using
    #                  `last` (mid) underestimated the reserve by half-spread +
    #                  slippage and could let a buy through whose real fill drove
    #                  paper cash negative (CLAUDE.md negative-balance guard).
    # - market sell  → bid (conservative; sells return quote, no negative-cash risk)
    if limit_price is not None:
        _balance_ref = float(limit_price)
    elif stop_price is not None:
        # QT-27: for a buy-stop the reaper fills at max(sp, ask) and charges
        # fee on that actual fill price.  Use the same worst-case reference so
        # the balance pre-check and fee estimate match what will be charged.
        # For a sell-stop the reaper fills at min(sp, bid); fees reduce proceeds
        # rather than increase cash consumption, so the same worst-case logic
        # applies symmetrically (though sells cannot drive cash negative).
        _mi_stop = market_info(sym)
        if side == "buy":
            _balance_ref = max(float(stop_price), float(_mi_stop.get("ask") or stop_price))
        else:
            _balance_ref = min(float(stop_price), float(_mi_stop.get("bid") or stop_price))
    elif otype == "market":
        _mi_pre = market_info(sym)
        if side == "buy":
            _touch_pre = float(_mi_pre["ask"])
            _slip_pre = estimate_slippage(sym, qty)
            _balance_ref = _touch_pre * (1.0 + float(_slip_pre["est_slippage_bps"]) / 10000.0)
        else:
            _balance_ref = float(_mi_pre["bid"])
    else:
        _balance_ref = float(last)
    fees = estimate_fees(otype, side, qty, _balance_ref, tif=tif)

    # HIGH-RISK negative-balance guard (CLAUDE.md). Opt-in via
    # TRADING_TERMINAL_ENFORCE_BALANCE. Only buys can reduce cash below zero;
    # sells return quote. Use a subnormal-safe comparison per project rules.
    if enforce_balance() and side == "buy":
        ref_price = _balance_ref
        required_quote = abs(qty * ref_price) + float(fees["fee_quote"])
        available = _paper_available_cash(session, mode)
        if not (available - required_quote > 1e-9):
            raise ValueError(
                f"INSUFFICIENT_BALANCE: need {required_quote:.2f} have {available:.2f} USDT"
            )

    order = ManualOrder(
        order_uid=str(uuid.uuid4()),
        terminal_session_id=terminal_session_id,
        symbol=sym,
        side=side,
        order_type=otype,
        qty=qty,
        limit_price=float(limit_price) if limit_price is not None else None,
        stop_price=float(stop_price) if stop_price is not None else None,
        tif=tif,
        mode=mode,
        status="pending",
        request_ip=request_ip,
        request_user_agent=user_agent,
        idempotency_key=idempotency_key,
    )
    session.add(order)
    session.flush()

    fills_out: List[Dict[str, Any]] = []
    # Simulate fill semantics.
    if otype == "market":
        slip = estimate_slippage(sym, qty)
        slip_bps = slip["est_slippage_bps"]
        # P31-MKT1: market order pays half-spread first, THEN size-impact
        # slippage. Real exchanges fill market BUY at >= best ask, SELL at
        # <= best bid; using `last` here gave free price improvement worth
        # ~half the spread that doesn't exist on real venues.
        _mi = market_info(sym)
        if _mi.get("quote_stale"):
            order.status = "rejected"
            order.decided_at = datetime.now(timezone.utc)
            session.flush()
            raise ValueError(f"STALE_QUOTE:{sym} fill aborted after validation race")
        touch = float(_mi["ask"]) if side == "buy" else float(_mi["bid"])
        fill_price = touch * (1.0 + (slip_bps / 10000.0) * (1 if side == "buy" else -1))
        if not (fill_price > 1e-14):
            order.status = "rejected"
            order.decided_at = datetime.now(timezone.utc)
            session.flush()
            raise ValueError(
                f"fill_price {fill_price} <= 0 after slippage ({slip_bps} bps) — order rejected"
            )
        # P30-T4: recompute fees on the post-slippage fill_price. Previously
        # `fees` (computed on the pre-slip `last`) under-charged taker fees on
        # market buys with positive slippage — venues charge fee on the
        # executed price, not the indicative quote.
        fees = estimate_fees(otype, side, qty, fill_price)
        fee_q = fees["fee_quote"]
        fill = ManualFill(
            order_id=order.id,
            filled_qty=qty,
            filled_price=fill_price,
            fee_quote=fee_q,
            fee_bps=fees["fee_bps"],
            is_maker=False,
            slippage_bps=slip_bps,
        )
        session.add(fill)
        session.flush()  # populate fill.id (autoincrement PK) and make fill visible to _recompute_position (autoflush=False)
        order.status = "filled"
        order.decided_at = datetime.now(timezone.utc)
        fills_out.append({**fill.to_dict(), "order_uid": order.order_uid})
    elif otype == "limit" and tif in ("ioc", "fok"):
        # IOC/FOK: only fills if crossable instantly. NOTE: this paper engine
        # treats IOC as all-or-nothing (identical to FOK) — it does NOT model
        # available touch liquidity, so it cannot fill a crossable PORTION and
        # cancel the remainder. A fully-crossing IOC fills 100%; a non-crossing
        # IOC is fully cancelled. Real partial-fill IOC is intentionally out of
        # scope for the paper simulator.
        # P31-MKT-IOC1: a marketable
        # IOC/FOK must cross the TOUCH (best ask for buy / best bid for sell),
        # not the mid (`last`). Comparing to `last` filled a buy priced between
        # mid and ask at a price the ask never reached (free half-spread). This
        # mirrors the market path (uses ask/bid) and the reaper (ask<=lp / bid>=lp).
        _mi_ioc = market_info(sym)
        if _mi_ioc.get("quote_stale"):
            order.status = "cancelled" if tif == "ioc" else "rejected"
            order.decided_at = datetime.now(timezone.utc)
            session.flush()
            session.add(AuditLog(
                actor="manual_terminal",
                action="trading_terminal.cancel_stale_quote",
                subject_type="order",
                subject_id=order.order_uid,
                payload_json=json.dumps(payload, default=str),
                response_json=json.dumps(
                    {"order": order.to_dict(), "reason": "STALE_QUOTE"}, default=str
                ),
                request_ip=request_ip,
                user_agent=user_agent,
                idempotency_key=idempotency_key,
                success=True,
            ))
            return {
                "order": order.to_dict(),
                "fills": [],
                "position": None,
            }
        _touch = float(_mi_ioc["ask"]) if side == "buy" else float(_mi_ioc["bid"])
        crossable = (
            (side == "buy" and float(limit_price) >= _touch)
            or (side == "sell" and float(limit_price) <= _touch)
        )
        if crossable:
            # P31-MKT-IOC2: an instantly-crossing IOC/FOK REMOVES liquidity
            # (taker). Record is_maker=False and charge the taker rate; the
            # `fees` dict above was computed with order_type=="limit" → maker.
            _taker_bps_ioc = taker_bps()
            fee_q = abs(qty * _touch) * (_taker_bps_ioc / 10000.0)
            fill = ManualFill(
                order_id=order.id,
                filled_qty=qty,
                filled_price=_touch,
                fee_quote=fee_q,
                fee_bps=_taker_bps_ioc,
                is_maker=False,
                slippage_bps=0.0,
            )
            session.add(fill)
            session.flush()  # populate fill.id and make fill visible to _recompute_position (autoflush=False)
            order.status = "filled"
            order.decided_at = datetime.now(timezone.utc)
            fills_out.append({**fill.to_dict(), "order_uid": order.order_uid})
        else:
            order.status = "cancelled" if tif == "ioc" else "rejected"
            order.decided_at = datetime.now(timezone.utc)
    # limit + gtc → stays pending, reaper picks it up
    # stop → stays pending (reaper triggers when stop hit)

    # D-M12 — single _recompute_position call. The previous code recomputed
    # twice (once to persist, once to serialize the response), wasting cycles
    # and risking divergence between persisted state and the returned snapshot.
    new_position = _recompute_position(session, sym, mode) if fills_out else None

    session.flush()
    audit_payload = {"order": order.to_dict(), "fills": fills_out}
    session.add(AuditLog(
        actor="manual_terminal",
        action="trading_terminal.submit",
        subject_type="order",
        subject_id=order.order_uid,
        payload_json=json.dumps(payload, default=str),
        response_json=json.dumps(audit_payload, default=str),
        request_ip=request_ip,
        user_agent=user_agent,
        idempotency_key=idempotency_key,
        success=True,
    ))
    return {
        "order": order.to_dict(),
        "fills": fills_out,
        "position": new_position.to_dict() if new_position is not None else None,
    }


# P31-STATE5: terminal statuses. "cancelled" returns idempotent success.
# "filled"/"rejected"/"orphaned" are terminal non-OK.
_CANCEL_ALREADY_DONE = frozenset({"cancelled"})
_CANCEL_TERMINAL_NON_OK = frozenset({"filled", "rejected", "orphaned"})


def cancel_order(
    session: Session,
    *,
    order_uid: str,
    idempotency_key: str,
    request_ip: Optional[str],
    user_agent: Optional[str],
) -> Dict[str, Any]:
    row = session.query(ManualOrder).filter(ManualOrder.order_uid == order_uid).one_or_none()
    if row is None:
        raise ValueError(f"order {order_uid} not found")
    if row.status in _CANCEL_ALREADY_DONE:
        # Idempotent no-op: client retry, or cancel-after-cancel race lost
        # to a prior cancel. Audit the no-op but do not raise.
        session.add(AuditLog(
            actor="manual_terminal",
            action="trading_terminal.cancel_noop",
            subject_type="order",
            subject_id=order_uid,
            payload_json=json.dumps({"order_uid": order_uid, "already": row.status}, default=str),
            response_json=json.dumps(row.to_dict(), default=str),
            request_ip=request_ip,
            user_agent=user_agent,
            idempotency_key=idempotency_key,
            success=True,
        ))
        return row.to_dict()
    if row.status in _CANCEL_TERMINAL_NON_OK:
        raise ValueError(
            f"order {order_uid} status={row.status!r} is terminal — "
            "cannot cancel a filled/rejected/orphaned order"
        )
    if row.status != "pending":
        raise ValueError(f"order {order_uid} status={row.status!r} — only pending orders cancellable")
    # P31-T3: conditional UPDATE for cross-process race with the reaper
    # (see STATE-4). 0 rows updated => reaper flipped to filled between
    # our SELECT and UPDATE; re-read and re-decide.
    updated = (
        session.query(ManualOrder)
        .filter(ManualOrder.id == row.id)
        .filter(ManualOrder.status == "pending")
        .update(
            {"status": "cancelled", "decided_at": datetime.now(timezone.utc)},
            synchronize_session=False,
        )
    )
    if not updated:
        session.refresh(row)
        raise ValueError(
            f"order {order_uid} no longer pending (now {row.status!r}); "
            "concurrent reaper fill won the race — cancel rejected"
        )
    session.refresh(row)
    session.add(AuditLog(
        actor="manual_terminal",
        action="trading_terminal.cancel",
        subject_type="order",
        subject_id=order_uid,
        payload_json=json.dumps({"order_uid": order_uid}, default=str),
        response_json=json.dumps(row.to_dict(), default=str),
        request_ip=request_ip,
        user_agent=user_agent,
        idempotency_key=idempotency_key,
        success=True,
    ))
    return row.to_dict()


def cancel_all_open(
    session: Session,
    *,
    mode: str,
    idempotency_key: str,
    request_ip: Optional[str],
    user_agent: Optional[str],
) -> Dict[str, Any]:
    """Bulk-cancel all pending orders for the given mode (paper|live).

    Uses a conditional UPDATE identical to cancel_order so concurrent reaper
    fills cannot be cancelled after they have already transitioned to 'filled'.
    Returns a summary of cancelled order UIDs and a count of rows skipped
    (already terminal).
    """
    if mode not in ("paper", "live"):
        raise ValueError(f"mode must be paper|live, got {mode!r}")

    rows = (
        session.query(ManualOrder)
        .filter(
            ManualOrder.mode == mode,
            ManualOrder.status == "pending",
        )
        .all()
    )
    cancelled_uids: List[str] = []
    skipped: int = 0
    now = datetime.now(timezone.utc)
    for row in rows:
        # P31-T3 (bulk): race-safe conditional UPDATE, identical guard to
        # cancel_order(). A plain `row.status = "cancelled"` would emit
        # `UPDATE ... WHERE id=?` with NO status predicate, so if the reaper
        # (separate session) flipped this row to 'filled' between our SELECT
        # above and this flush, we would silently overwrite the fill and
        # cancel an already-executed order. Filtering on status == 'pending'
        # in the WHERE clause makes the DB the arbiter: 0 rows updated => the
        # reaper won the race => count it as skipped, never clobbered.
        updated = (
            session.query(ManualOrder)
            .filter(ManualOrder.id == row.id)
            .filter(ManualOrder.status == "pending")
            .update(
                {"status": "cancelled", "decided_at": now},
                synchronize_session=False,
            )
        )
        if updated:
            cancelled_uids.append(row.order_uid)
        else:
            skipped += 1

    session.flush()
    session.add(AuditLog(
        actor="manual_terminal",
        action="trading_terminal.cancel_all_open",
        subject_type="order",
        subject_id="bulk",
        payload_json=json.dumps({"mode": mode, "cancelled": cancelled_uids}, default=str),
        response_json=json.dumps({"cancelled_count": len(cancelled_uids), "skipped": skipped}, default=str),
        request_ip=request_ip,
        user_agent=user_agent,
        idempotency_key=idempotency_key,
        success=True,
    ))
    return {
        "cancelled_count": len(cancelled_uids),
        "cancelled_order_uids": cancelled_uids,
        "skipped": skipped,
    }


def list_orders(session: Session, *, limit: int = 50, status: Optional[str] = None, symbol: Optional[str] = None, mode: Optional[str] = None) -> List[Dict[str, Any]]:
    q = session.query(ManualOrder).order_by(ManualOrder.requested_at.desc())
    if status:
        q = q.filter(ManualOrder.status == status)
    if symbol:
        q = q.filter(ManualOrder.symbol == symbol.upper())
    if mode:
        q = q.filter(ManualOrder.mode == mode)
    rows = q.limit(max(1, min(int(limit), 200))).all()
    return [r.to_dict() for r in rows]


def list_positions(session: Session, *, mode: str = "paper") -> List[Dict[str, Any]]:
    rows = (
        session.query(ManualPosition)
        .filter(ManualPosition.mode == mode)
        .all()
    )
    return [r.to_dict() for r in rows]


__all__ = [
    "is_enabled",
    "supported_symbols",
    "market_info",
    "validate_order",
    "preview",
    "submit_paper",
    "cancel_order",
    "cancel_all_open",
    "list_orders",
    "list_positions",
    "rate_limit_per_min",
]

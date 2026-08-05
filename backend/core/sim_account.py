"""Forward paper-trade SIM account — full-ledger matching engine (P-SIM).

The difference from :mod:`backend.core.paper_trader`: that module re-backtests a
*trailing* window of the same history every run, so it never tests data the
strategy was not selected on. A SIM account instead **pins a start bar** when
you "send a strategy to sim" and only ever scores bars *after* that pin — a
genuine walk-forward as real ingest advances the dataset.

This is a SIMULATION. It never routes an order to a real venue and never touches
``LIVE_TRADE_ENABLED``; ``mode`` is implicitly "sim" everywhere. It is the safe
side of the paper<->live gate.

Accounting (high-risk-system rules — see project CLAUDE.md):
  * Immutable ledgers. ``sim_fills`` + ``sim_funding`` are append-only; position
    and cash are DERIVED by folding the ledger, never read from a single mutable
    balance column. Re-ticking recomputes from the ledger, so it is idempotent
    and self-healing.
  * No double-fill. DB ``UNIQUE(account_id, bar_ts)`` on both ledgers is the
    cross-process backstop; the per-strategy lock is the in-process guarantee.
  * Next-bar-open execution. A position change decided from the signal at bar
    ``i-1`` fills at bar ``i``'s open (+ slippage), matching the backtester's
    ``signals.shift(1)`` and the strategy's own next-bar-open contract.
  * Funding. Perp funding is settled every 8h (00:00 / 08:00 / 16:00 UTC) on the
    held notional: ``cashflow = -signed_notional * funding_rate`` (a long pays
    when funding > 0, a short receives). Only modelled when the dataset carries a
    ``funding_rate`` column (BTC primary); degrades to 0 otherwise.

ponytail: no partial fills / order-book — single-asset market fill at open±slip
is the honest ceiling without an L2 book. Upgrade path: depth-aware fill model.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from backend.core.database import (
    AlphaStrategy,
    PROJECT_ROOT,
    SimAccount,
    SimFill,
    SimFunding,
    session_scope,
)
from backend.core.market_data import PRICES_DIR
from backend.core.sandbox import (
    SandboxExecutionError,
    SandboxValidationError,
    safe_execute_factor,
)
from backend.core.thresholds import (
    MIN_PROFIT_FACTOR,
    MIN_SHARPE,
    MIN_TRADES_PAPER,
    PAPER_MAX_DRAWDOWN,
)

logger = logging.getLogger("alpha.sim")

SIM_DIR: Path = PROJECT_ROOT / "storage" / "sim_account"
SIM_DIR.mkdir(parents=True, exist_ok=True)
BTC_PRIMARY_CSV: Path = PROJECT_ROOT / "backend" / "data" / "synthetic_btc.csv"

HOURS_PER_YEAR: int = 24 * 365  # 8760, matches engine.AlphaBacktester
SQRT_HOURS_PER_YEAR: float = math.sqrt(HOURS_PER_YEAR)

DEFAULT_INITIAL_CAPITAL: float = 100_000.0
DEFAULT_FEE_BPS: float = 0.0005      # per side, matches engine.FEE_BPS
DEFAULT_SLIPPAGE_BPS: float = 0.0002  # per side, matches engine.SLIPPAGE_BPS

# Minimum forward bars before the health gate is meaningful (1 week of hourly
# bars). Below this the account is "warming up" — Sharpe on a handful of bars is
# noise, not signal.
MIN_FORWARD_BARS_FOR_HEALTH: int = 168

# Trade only when the SIGNAL changes by more than this — i.e. a state change, not
# intra-hold price drift. This matches how the strategy (and the backtester it
# was approved on) trades: enter once at a sized position, hold a fixed base qty
# until the signal flips, then exit. Discrete {-1,0,1} factors cross this on
# every state change; continuous factors avoid micro-rebalance churn.
SIGNAL_DEADBAND: float = 0.05

_EPS: float = 1e-14


# ---------------------------------------------------------------------------
# Per-strategy lock (mirror of paper_trader; serializes tick vs manual run).
# ---------------------------------------------------------------------------
_LOCKS_MAX = 5000
_LOCKS: "OrderedDict[int, Lock]" = OrderedDict()
_LOCKS_GUARD = Lock()


def _get_lock(sid: int) -> Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(sid)
        if lock is None:
            lock = Lock()
            _LOCKS[sid] = lock
            while len(_LOCKS) > _LOCKS_MAX:
                oldest_sid, oldest_lock = next(iter(_LOCKS.items()))
                if oldest_lock.acquire(blocking=False):
                    try:
                        _LOCKS.pop(oldest_sid, None)
                    finally:
                        oldest_lock.release()
                else:
                    break
        else:
            _LOCKS.move_to_end(sid)
        return lock


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
def _resolve_symbol(config_json: Optional[str]) -> str:
    """Pull the target instrument from a strategy's config_json (default BTC)."""
    if config_json:
        try:
            cfg = json.loads(config_json)
            sym = str(cfg.get("asset_symbol") or "").strip().upper()
            if sym:
                return sym
        except (TypeError, ValueError):
            pass
    return "BTC"


def _market_csv_for(symbol: str) -> Path:
    """BTC -> the 9-col primary (carries funding); else the per-asset file via
    the canonical universe resolver (equities are ``<SYM>.csv``, crypto
    ``<SYM>-USDT.csv``). Mirrors market_data's on-disk layout."""
    sym = (symbol or "BTC").strip().upper()
    if sym in {"BTC", "BTC-USDT", "BTCUSDT"}:
        return BTC_PRIMARY_CSV
    # Route non-BTC through the universe so equities (SPY, AAPL, …) resolve to
    # ``<SYM>.csv`` instead of a non-existent ``<SYM>-USDT.csv``.
    from backend.core import universe
    return universe.price_csv_path(sym)


def _to_utc(ts: Any) -> pd.Timestamp:
    """Normalize any timestamp to a tz-aware UTC pd.Timestamp.

    Critical: SQLite round-trips ``DateTime`` columns as tz-NAIVE, while the CSV
    index is tz-aware UTC. Without this, ``>`` comparisons raise and — worse —
    ledger dict lookups silently miss, so a re-tick would try to double-insert.
    """
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def _load_full_market(symbol: str) -> pd.DataFrame:
    path = _market_csv_for(symbol)
    if not path.exists():
        raise FileNotFoundError(
            f"Market data missing for {symbol}: {path}. Run market-data ingest first."
        )
    df = pd.read_csv(path, parse_dates=["timestamp"])
    df = df.set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if df.empty:
        raise FileNotFoundError(f"Market data for {symbol} is empty: {path}")
    # Normalize index to tz-aware UTC so DB-sourced (tz-naive) account/ledger
    # timestamps compare and hash-match against it.
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df


def _latest_bar_ts(df: pd.DataFrame) -> datetime:
    ts = df.index.max()
    return ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts


# ---------------------------------------------------------------------------
# Core: decide + append new fills, then replay the forward ledger.
# ---------------------------------------------------------------------------
def _is_funding_settlement(ts: pd.Timestamp) -> bool:
    """Binance USDT-M perps settle funding at 00:00 / 08:00 / 16:00 UTC."""
    return int(ts.hour) % 8 == 0 and int(ts.minute) == 0


def _signal_array(factor_code: str, df: pd.DataFrame) -> "pd.Series":
    """Run the (untrusted, LLM-generated) factor in the sandbox and return a
    signal Series aligned to ``df.index``, clipped to [-1, 1]."""
    # Equities carry only OHLCV, but the sandbox requires the full canonical
    # column set (open_interest / funding_rate / liquidations). Add them as 0.0
    # on a COPY — matching the backtest/factor_evaluator normalization — so the
    # factor sees the same schema without mutating the ledger df (which keys
    # funding modelling off funding_rate's presence in ``_decide_and_replay``).
    feed = df
    missing = [c for c in ("open_interest", "funding_rate", "liquidations") if c not in df.columns]
    if missing:
        feed = df.copy()
        for c in missing:
            feed[c] = 0.0
    try:
        res = safe_execute_factor(factor_code, feed)
    except (SandboxValidationError, SandboxExecutionError) as exc:
        raise RuntimeError(f"Sandbox rejected factor code: {exc}") from exc
    sig = pd.to_numeric(res.signal, errors="coerce")
    sig = sig.reindex(df.index).fillna(0.0).clip(-1.0, 1.0)
    return sig


def _decide_and_replay(
    account: SimAccount,
    df: pd.DataFrame,
    signal: "pd.Series",
    existing_fills: Dict[pd.Timestamp, SimFill],
    existing_funding: Dict[pd.Timestamp, SimFunding],
) -> Tuple[List[SimFill], List[SimFunding], List[Dict[str, Any]], Dict[str, Any]]:
    """Single forward pass over bars after the pin.

    For bars already in the ledger -> apply their stored fill/funding (rebuild
    state). For genuinely new bars (ts > last_bar_ts) -> decide a position change
    from signal[i-1], execute at this bar's open±slippage, and emit NEW ledger
    rows. Returns (new_fills, new_funding, equity_curve, summary).
    """
    init_cap = float(account.initial_capital)
    fee_bps = float(account.fee_bps)
    slip_bps = float(account.slippage_bps)
    start_bar_ts = _to_utc(account.start_bar_ts)
    last_bar_ts = _to_utc(account.last_bar_ts) if account.last_bar_ts else start_bar_ts

    idx = df.index
    opens = df["open"].astype(float).to_numpy() if "open" in df.columns else df["close"].astype(float).to_numpy()
    closes = df["close"].astype(float).to_numpy()
    has_funding = "funding_rate" in df.columns
    fundings = df["funding_rate"].astype(float).to_numpy() if has_funding else None
    sig = signal.to_numpy()

    # First forward bar: strictly after the pin. pos>=1 guaranteed (the pin is an
    # existing bar with history before it), so signal[pos-1] is always valid.
    n = len(idx)
    start_pos = int(df.index.searchsorted(start_bar_ts, side="right"))
    if start_pos < 1:
        start_pos = 1

    cash = init_cap
    qty = 0.0                 # signed base qty held
    current_signal_level = 0.0  # the signal level the current position was sized to
    new_fills: List[SimFill] = []
    new_funding: List[SimFunding] = []
    equity_curve: List[Dict[str, Any]] = []
    gross_pos = 0.0
    gross_neg = 0.0
    liquidated = False
    prev_equity = init_cap

    for pos in range(start_pos, n):
        ts = idx[pos]
        px_open = opens[pos]
        px_close = closes[pos]
        desired = float(sig[pos - 1])  # next-bar-open execution
        is_new = ts > last_bar_ts

        # --- Fill: realize an existing one, or decide a new one ---------------
        # Trade ONLY on a signal STATE change (enter once, hold fixed qty, exit) —
        # not on intra-hold price drift. Sizing is to the signal level at the
        # moment of change; the base qty is then held until the next change.
        stored = existing_fills.get(ts)
        if stored is not None:
            qty += float(stored.signed_qty_delta)
            cash += -float(stored.signed_qty_delta) * float(stored.price) - float(stored.fee_quote)
            current_signal_level = desired
        elif (
            is_new
            and not liquidated
            and px_open > _EPS
            and px_close > _EPS
            and abs(desired - current_signal_level) > SIGNAL_DEADBAND
        ):
            equity_mark = cash + qty * px_open
            if equity_mark > _EPS:
                # Provisional target at mid to pick the trade direction, then
                # re-price the target at the slipped fill price.
                delta_mid = desired * equity_mark / px_open - qty
                if abs(delta_mid) > _EPS:
                    if delta_mid > 0:
                        fill_px = px_open * (1.0 + slip_bps)
                        side = "buy"
                    else:
                        fill_px = px_open * (1.0 - slip_bps)
                        side = "sell"
                    delta = desired * equity_mark / fill_px - qty
                    if abs(current_signal_level) <= _EPS:
                        reason = "entry"
                    elif abs(desired) <= _EPS:
                        reason = "exit"
                    elif (desired > 0) != (current_signal_level > 0):
                        reason = "flip"
                    else:
                        reason = "rebalance"
                    fee = abs(delta) * fill_px * fee_bps
                    qty += delta
                    cash += -delta * fill_px - fee
                    new_fills.append(
                        SimFill(
                            account_id=account.id,
                            bar_ts=ts.to_pydatetime(),
                            side=side,
                            signed_qty_delta=float(delta),
                            price=float(fill_px),
                            fee_quote=float(fee),
                            slippage_bps=float(slip_bps),
                            reason=reason,
                        )
                    )
                    current_signal_level = desired

        # --- Funding settlement ---------------------------------------------
        stored_f = existing_funding.get(ts)
        if stored_f is not None:
            cash += float(stored_f.cashflow_quote)
        elif is_new and abs(qty) > _EPS and _is_funding_settlement(ts) and has_funding:
            fr = float(fundings[pos])
            signed_notional = qty * px_close
            cashflow = -signed_notional * fr  # long pays when funding>0
            cash += cashflow
            new_funding.append(
                SimFunding(
                    account_id=account.id,
                    bar_ts=ts.to_pydatetime(),
                    funding_rate=fr,
                    position_notional=float(signed_notional),
                    cashflow_quote=float(cashflow),
                )
            )

        # --- Mark to market at close ----------------------------------------
        equity = cash + qty * px_close
        if not liquidated and equity <= init_cap * 0.01:
            # Margin wipeout guard. At 1x notional on BTC this is essentially
            # unreachable, but a sustained adverse short could compound there.
            liquidated = True
            equity = max(0.0, equity)
        bar_ret = (equity / prev_equity - 1.0) if prev_equity > _EPS else 0.0
        if bar_ret > 0:
            gross_pos += bar_ret
        elif bar_ret < 0:
            gross_neg += -bar_ret
        prev_equity = equity if equity > _EPS else prev_equity
        equity_curve.append(
            {
                "t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "equity": round(equity, 4),
                "ret": round(bar_ret, 8),
            }
        )

    summary = {
        "cash": round(cash, 4),
        "qty_base": float(qty),
        "position_notional": round(qty * (closes[-1] if n else 0.0), 4),
        "equity": round(cash + qty * (closes[-1] if n else 0.0), 4),
        "gross_pos": gross_pos,
        "gross_neg": gross_neg,
        "liquidated": liquidated,
        "last_bar_ts": idx[-1] if n else None,
    }
    return new_fills, new_funding, equity_curve, summary


def _metrics_from_curve(
    equity_curve: List[Dict[str, Any]],
    init_cap: float,
    n_fills: int,
    gross_pos: float,
    gross_neg: float,
) -> Dict[str, float]:
    if not equity_curve:
        return {
            "forward_bars": 0,
            "num_trades": 0,
            "cumulative_return": 0.0,
            "annualized_sharpe": 0.0,
            "max_drawdown": 0.0,
            "profit_factor": 0.0,
        }
    rets = [float(b["ret"]) for b in equity_curve]
    equities = [float(b["equity"]) for b in equity_curve]
    nbars = len(rets)
    mean = sum(rets) / nbars
    var = sum((r - mean) ** 2 for r in rets) / nbars if nbars > 1 else 0.0
    std = math.sqrt(var)
    sharpe = (mean / std * SQRT_HOURS_PER_YEAR) if std > _EPS else 0.0
    # Max drawdown over the marked equity, anchored at starting capital.
    peak = init_cap
    max_dd = 0.0
    for e in equities:
        if e > peak:
            peak = e
        dd = (e / peak - 1.0) if peak > _EPS else 0.0
        if dd < max_dd:
            max_dd = dd
    cum = (equities[-1] / init_cap - 1.0) if init_cap > _EPS else 0.0
    pf = (gross_pos / gross_neg) if gross_neg > _EPS else (gross_pos if gross_pos > _EPS else 0.0)
    return {
        "forward_bars": nbars,
        "num_trades": int(n_fills),
        "cumulative_return": round(cum, 6),
        "annualized_sharpe": round(sharpe, 4),
        "max_drawdown": round(max_dd, 6),
        "profit_factor": round(min(pf, 999.0), 4),
    }


def _health_check(metrics: Dict[str, float]) -> Tuple[Optional[bool], List[str]]:
    """Forward health gate. Returns (is_healthy, notes). is_healthy is None while
    the account is still warming up (too few forward bars to judge)."""
    notes: List[str] = []
    fbars = int(metrics.get("forward_bars", 0) or 0)
    if fbars < MIN_FORWARD_BARS_FOR_HEALTH:
        notes.append(
            f"Warming up: {fbars}/{MIN_FORWARD_BARS_FOR_HEALTH} forward bars "
            f"(~{fbars / 24:.1f}d) before health is meaningful"
        )
        return None, notes
    healthy = True

    def _m(key: str) -> float:
        try:
            v = float(metrics.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return v if v == v else 0.0  # NaN -> 0

    sharpe = _m("annualized_sharpe")
    dd = _m("max_drawdown")
    pf = _m("profit_factor")
    trades = int(metrics.get("num_trades", 0) or 0)
    if sharpe < MIN_SHARPE:
        healthy = False
        notes.append(f"Sharpe {sharpe:.2f} < {MIN_SHARPE}")
    else:
        notes.append(f"Sharpe {sharpe:.2f} OK")
    if dd < PAPER_MAX_DRAWDOWN:
        healthy = False
        notes.append(f"MaxDD {dd * 100:.1f}% breaches -{int(round(abs(PAPER_MAX_DRAWDOWN) * 100))}% floor")
    else:
        notes.append(f"MaxDD {dd * 100:.1f}% OK")
    if trades < MIN_TRADES_PAPER:
        healthy = False
        notes.append(f"Only {trades} forward trades (need >= {MIN_TRADES_PAPER})")
    else:
        notes.append(f"{trades} trades OK")
    if pf < MIN_PROFIT_FACTOR:
        healthy = False
        notes.append(f"Profit factor {pf:.2f} < {MIN_PROFIT_FACTOR}")
    else:
        notes.append(f"Profit factor {pf:.2f} OK")
    return healthy, notes


# ---------------------------------------------------------------------------
# Snapshot persistence (heavy time-series go to disk, like paper_trader)
# ---------------------------------------------------------------------------
def _snapshot_path(strategy_id: int) -> Path:
    return SIM_DIR / f"{strategy_id}.json"


def _persist_snapshot(payload: Dict[str, Any]) -> None:
    out = _snapshot_path(int(payload["strategy_id"]))
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, out)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def start_account(
    strategy_id: int,
    *,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
    fee_bps: float = DEFAULT_FEE_BPS,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> Dict[str, Any]:
    """Pin a strategy to forward simulation from the latest available bar.

    Re-pinning an existing account RESETS it (clears its ledger) so a fresh
    walk-forward begins from now.
    """
    sid = int(strategy_id)
    initial_capital = max(1.0, float(initial_capital))
    fee_bps = max(0.0, float(fee_bps))
    slippage_bps = max(0.0, float(slippage_bps))

    lock = _get_lock(sid)
    with lock:
        with session_scope() as s:
            row = s.get(AlphaStrategy, sid)
            if row is None:
                raise LookupError(f"AlphaStrategy {sid} not found")
            factor_code = (row.formula_code or "").strip()
            if not factor_code:
                raise LookupError(f"Strategy {sid} has no factor_code yet — run the pipeline first.")
            symbol = _resolve_symbol(row.config_json)
            name = row.name or f"strategy_{sid}"

        df = _load_full_market(symbol)
        start_bar = _latest_bar_ts(df)

        with session_scope() as s:
            acct = s.query(SimAccount).filter(SimAccount.strategy_id == sid).one_or_none()
            if acct is not None:
                # Reset: drop prior ledger so the new pin is a clean forward test.
                s.query(SimFill).filter(SimFill.account_id == acct.id).delete()
                s.query(SimFunding).filter(SimFunding.account_id == acct.id).delete()
                acct.symbol = symbol
                acct.status = "active"
                acct.initial_capital = initial_capital
                acct.fee_bps = fee_bps
                acct.slippage_bps = slippage_bps
                acct.started_at = datetime.now(timezone.utc)
                acct.start_bar_ts = start_bar
                acct.last_bar_ts = start_bar
            else:
                acct = SimAccount(
                    strategy_id=sid,
                    symbol=symbol,
                    status="active",
                    initial_capital=initial_capital,
                    fee_bps=fee_bps,
                    slippage_bps=slippage_bps,
                    started_at=datetime.now(timezone.utc),
                    start_bar_ts=start_bar,
                    last_bar_ts=start_bar,
                )
                s.add(acct)
            s.flush()
            acct_dict = acct.to_dict()

    _persist_snapshot(
        {
            "strategy_id": sid,
            "name": name,
            "symbol": symbol,
            "status": "active",
            "started_at": acct_dict["started_at"],
            "start_bar_ts": acct_dict["start_bar_ts"],
            "last_bar_ts": acct_dict["last_bar_ts"],
            "initial_capital": initial_capital,
            "metrics": _metrics_from_curve([], initial_capital, 0, 0.0, 0.0),
            "equity_curve": [],
            "fills": [],
            "funding": {"settlements": 0, "net_quote": 0.0},
            "position": {"qty_base": 0.0, "position_notional": 0.0, "equity": initial_capital},
            "is_healthy": None,
            "health_notes": ["Just pinned — no forward bars yet"],
            "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    return latest_for(sid) or acct_dict


def tick_account(strategy_id: int) -> Optional[Dict[str, Any]]:
    """Advance one SIM account over any bars that landed since its last tick.

    Idempotent: re-running with no new bars rewrites an identical snapshot.
    Returns the snapshot dict, or None if the account is missing/stopped.
    """
    sid = int(strategy_id)
    lock = _get_lock(sid)
    with lock:
        with session_scope() as s:
            acct = s.query(SimAccount).filter(SimAccount.strategy_id == sid).one_or_none()
            if acct is None:
                return None
            if acct.status != "active":
                return latest_for(sid)
            row = s.get(AlphaStrategy, sid)
            if row is None:
                return None
            factor_code = (row.formula_code or "").strip()
            name = row.name or f"strategy_{sid}"
            symbol = acct.symbol or _resolve_symbol(row.config_json)
            init_cap = float(acct.initial_capital)
            started_at = acct.started_at
            start_bar_ts = acct.start_bar_ts
            acct_id = int(acct.id)
        if not factor_code:
            return latest_for(sid)

        df = _load_full_market(symbol)
        signal = _signal_array(factor_code, df)

        with session_scope() as s:
            acct = s.query(SimAccount).filter(SimAccount.strategy_id == sid).one()
            fills = s.query(SimFill).filter(SimFill.account_id == acct_id).all()
            funding = s.query(SimFunding).filter(SimFunding.account_id == acct_id).all()
            fills_by_ts = {_to_utc(f.bar_ts): f for f in fills}
            funding_by_ts = {_to_utc(f.bar_ts): f for f in funding}

            new_fills, new_funding, equity_curve, summ = _decide_and_replay(
                acct, df, signal, fills_by_ts, funding_by_ts
            )
            for f in new_fills:
                s.add(f)
            for f in new_funding:
                s.add(f)
            if summ["last_bar_ts"] is not None:
                acct.last_bar_ts = pd.Timestamp(summ["last_bar_ts"]).to_pydatetime()
            if summ["liquidated"]:
                acct.status = "liquidated"
            acct.updated_at = datetime.now(timezone.utc)
            total_fills = len(fills) + len(new_fills)
            total_funding = funding + new_funding
            net_funding = sum(float(f.cashflow_quote) for f in total_funding)
            status = acct.status

        metrics = _metrics_from_curve(
            equity_curve, init_cap, total_fills, summ["gross_pos"], summ["gross_neg"]
        )
        is_healthy, notes = _health_check(metrics)
        # Recent fills tape (last 200), newest-last for chart alignment.
        all_fills = sorted(
            [f.to_dict() for f in fills] + [f.to_dict() for f in new_fills],
            key=lambda d: d.get("bar_ts") or "",
        )
        snapshot = {
            "strategy_id": sid,
            "name": name,
            "symbol": symbol,
            "status": status,
            "started_at": started_at.isoformat() if hasattr(started_at, "isoformat") else str(started_at),
            "start_bar_ts": start_bar_ts.isoformat() if hasattr(start_bar_ts, "isoformat") else str(start_bar_ts),
            "last_bar_ts": str(summ["last_bar_ts"]) if summ["last_bar_ts"] is not None else None,
            "initial_capital": init_cap,
            "metrics": metrics,
            "equity_curve": equity_curve,
            "fills": all_fills[-200:],
            "funding": {"settlements": len(total_funding), "net_quote": round(net_funding, 4)},
            "position": {
                "qty_base": summ["qty_base"],
                "position_notional": summ["position_notional"],
                "equity": summ["equity"],
            },
            "is_healthy": is_healthy,
            "health_notes": notes,
            "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        _persist_snapshot(snapshot)
        return snapshot


def tick_all_active() -> Dict[str, Any]:
    """Tick every active account. Best-effort: one failure never aborts the rest.
    Safe to call from the post-ingest hook (wrap-and-log at the call site too)."""
    with session_scope() as s:
        sids = [
            int(a.strategy_id)
            for a in s.query(SimAccount).filter(SimAccount.status == "active").all()
        ]
    ran, errors = 0, 0
    for sid in sids:
        try:
            tick_account(sid)
            ran += 1
        except Exception:  # noqa: BLE001
            errors += 1
            logger.exception("sim_account tick failed for strategy %s", sid)
    return {"considered": len(sids), "ran": ran, "errors": errors}


def refresh_market_and_tick() -> Dict[str, Any]:
    """Periodic job (P-SIM): incrementally refresh the real market-data CSVs,
    then advance every active forward-sim account over the bars that just landed.

    Wired into the ``market_data_refresh`` periodic task so the forward-sim curves
    keep moving on their own. Without this the periodic refresh updates the price
    CSVs but the sims only advance when someone manually hits
    ``POST /api/market-data/ingest`` (whose handler is the sole other caller of
    :func:`tick_all_active`) — which is exactly how accounts drift a full day
    behind the data sitting in their own CSV.

    Best-effort, never raises: a refresh failure is logged and the tick still runs
    over whatever bars are already on disk; ``tick_all_active`` swallows per-account
    failures itself. Returns a small report for the periodic-runner log.
    """
    refresh: Optional[Dict[str, Any]] = None
    try:
        from backend.core import market_data as md
        refresh = md.refresh_task()
    except Exception:  # noqa: BLE001 — a data-refresh error must not block the sim tick
        logger.exception("sim refresh: market-data refresh failed (ticking on-disk bars)")
    stats = tick_all_active()
    return {"refresh": refresh, "sim_tick": stats}


def stop_account(strategy_id: int) -> Optional[Dict[str, Any]]:
    sid = int(strategy_id)
    with _get_lock(sid):
        with session_scope() as s:
            acct = s.query(SimAccount).filter(SimAccount.strategy_id == sid).one_or_none()
            if acct is None:
                return None
            acct.status = "stopped"
            acct.updated_at = datetime.now(timezone.utc)
    snap = latest_for(sid)
    if snap is not None:
        snap["status"] = "stopped"
        _persist_snapshot(snap)
    return snap


def latest_for(strategy_id: int) -> Optional[Dict[str, Any]]:
    p = _snapshot_path(int(strategy_id))
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("sim_account.latest_for unreadable sid=%s err=%s", strategy_id, exc)
        return None


def list_accounts() -> List[Dict[str, Any]]:
    """All accounts (DB rows merged with their latest snapshot summary)."""
    with session_scope() as s:
        rows = [a.to_dict() for a in s.query(SimAccount).order_by(SimAccount.updated_at.desc()).all()]
    out: List[Dict[str, Any]] = []
    for r in rows:
        snap = latest_for(int(r["strategy_id"]))
        if snap is not None:
            r = {
                **r,
                "name": snap.get("name"),
                "metrics": snap.get("metrics"),
                "is_healthy": snap.get("is_healthy"),
                "health_notes": snap.get("health_notes"),
                "position": snap.get("position"),
                "funding": snap.get("funding"),
                "run_at": snap.get("run_at"),
            }
        out.append(r)
    return out


__all__ = [
    "start_account",
    "tick_account",
    "tick_all_active",
    "stop_account",
    "latest_for",
    "list_accounts",
    "DEFAULT_INITIAL_CAPITAL",
]

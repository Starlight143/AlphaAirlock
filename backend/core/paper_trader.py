"""Paper trading forward-simulation engine (P4).

Re-runs an APPROVED strategy's factor code on the most-recent N days of the
synthetic dataset to simulate forward trading. Output goes to
`storage/paper_trade/<strategy_id>.json` so /api/paper-trade can stream it.

Production note — this is INTENTIONALLY a simulation rather than a live
exchange feed. Live exchange integration is part of the Stage 6 Live Deploy
hook (P4 stub endpoint). Paper trade in the reference demo is also a
forward simulation, not real money — it's the "is the alpha still alive?"
gate before Small Capital.
"""

from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from backend.core.database import AlphaStrategy, PROJECT_ROOT, session_scope
from backend.core.engine import AlphaBacktester
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

logger = logging.getLogger("alpha.paper")

PAPER_DIR: Path = PROJECT_ROOT / "storage" / "paper_trade"
PAPER_DIR.mkdir(parents=True, exist_ok=True)
DATA_CSV: Path = PROJECT_ROOT / "backend" / "data" / "synthetic_btc.csv"

# Default: last 30 days of hourly bars = 720 bars.
DEFAULT_FORWARD_DAYS: int = 30
HOURS_PER_DAY: int = 24


@dataclass
class PaperTradeResult:
    strategy_id: int
    window_days: int
    metrics: Dict[str, float]
    equity_curve: List[Dict[str, Any]]
    trades: int
    is_healthy: bool
    health_notes: List[str]
    run_at: str
    # B7-1: per-bar tape from the backtest engine (signal + mark_price per bar).
    # Required by live_trade_ops._position_series to derive dashboard positions
    # and recent_fills. Must come last (has a default) to satisfy dataclass ordering.
    per_bar: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "window_days": self.window_days,
            "metrics": self.metrics,
            "equity_curve": self.equity_curve,
            "trades": self.trades,
            "is_healthy": self.is_healthy,
            "health_notes": self.health_notes,
            "run_at": self.run_at,
            "per_bar": self.per_bar,
        }


def _load_recent_market(window_days: int) -> pd.DataFrame:
    if not DATA_CSV.exists():
        raise FileNotFoundError(
            f"Synthetic market data missing: {DATA_CSV}. "
            "Run `python backend/core/data_gen.py` first."
        )
    df = pd.read_csv(DATA_CSV, parse_dates=["timestamp"])
    df = df.set_index("timestamp").sort_index()
    # P32-D4 / DAT32-5 — drop duplicate timestamps (keep latest revision).
    df = df[~df.index.duplicated(keep="last")]
    rows = max(64, window_days * HOURS_PER_DAY)  # need enough lookback for any rolling factor
    return df.iloc[-rows:].copy()


def _health_check(metrics: Dict[str, float], trades: int) -> tuple[bool, List[str]]:
    """Paper-trade health gate. Same rubric as the live promotion endpoint.

    Any failing line vetoes the strategy. The full list is always returned so
    the UI can show which checks tripped.
    """
    notes: List[str] = []
    healthy = True
    # D-L6/P16 — the previous ``float(... or 0.0)`` pattern silently swallowed
    # values like NaN, strings ("None"), or any other non-numeric metric (the
    # caller's metric dict is sometimes regenerated from JSON, so the contract
    # is "best-effort floats"). Wrap each cast in try/except so a single bad
    # metric becomes a logged 0.0 instead of a crash inside the health gate.
    def _safe_metric(key: str) -> float:
        raw = metrics.get(key, 0.0)
        try:
            v = float(raw if raw is not None else 0.0)
        except (TypeError, ValueError):
            logger.warning(
                "paper_trader._health_check: non-numeric metric %r=%r, treating as 0.0",
                key, raw,
            )
            return 0.0
        if not (v == v):  # NaN check (NaN != NaN)
            logger.warning(
                "paper_trader._health_check: NaN metric %r, treating as 0.0", key,
            )
            return 0.0
        return v
    sharpe = _safe_metric("annualized_sharpe")
    dd = _safe_metric("max_drawdown")
    pf = _safe_metric("profit_factor")
    dd_floor_pct = int(round(abs(PAPER_MAX_DRAWDOWN) * 100))
    if sharpe < MIN_SHARPE:
        healthy = False
        notes.append(f"Sharpe {sharpe:.2f} < {MIN_SHARPE}")
    else:
        notes.append(f"Sharpe {sharpe:.2f} OK")
    if dd < PAPER_MAX_DRAWDOWN:
        healthy = False
        notes.append(f"MaxDD {dd * 100:.1f}% breaches -{dd_floor_pct}% floor")
    else:
        notes.append(f"MaxDD {dd * 100:.1f}% OK")
    if trades < MIN_TRADES_PAPER:
        healthy = False
        notes.append(
            f"Only {trades} trades in window (need >= {MIN_TRADES_PAPER})"
        )
    else:
        notes.append(f"{trades} trades OK")
    if pf < MIN_PROFIT_FACTOR:
        healthy = False
        notes.append(f"Profit factor {pf:.2f} < {MIN_PROFIT_FACTOR}")
    else:
        notes.append(f"Profit factor {pf:.2f} OK")
    return healthy, notes


# P13/D-H4 — per-strategy write locks for atomic JSON persistence.
# Concurrent paper_scheduler ticks + manual /run requests could otherwise
# corrupt the file (interleaved writes) or let readers (latest_for) see a
# half-flushed file. The guard dict insertion is itself protected.
# D-M13 — lock acquisition relocated to the START of `run_paper_trade` so the
# whole sandbox + backtest + persist sequence serializes per strategy. The
# previous narrow lock around _persist() let two concurrent runs duplicate
# all the expensive LLM/sandbox work; only the file write was protected.
# P15/D-M24 — bounded LRU dict so a fleet of strategies that runs once and
# never again can't leak unlimited Lock objects. 5000 is a generous cap for
# any realistic deployment. Eviction is HELD-LOCK-SAFE: we only drop the
# oldest entry if we can acquire its lock non-blockingly, so we never evict
# a lock another thread is depending on (preserves serialization guarantee).
_STRATEGY_WRITE_LOCKS_MAX = 5000
_STRATEGY_WRITE_LOCKS: "OrderedDict[int, Lock]" = OrderedDict()
_STRATEGY_WRITE_LOCKS_GUARD = Lock()


def _get_strategy_lock(sid: int) -> Lock:
    """Lazily create and return a per-strategy write lock with bounded LRU."""
    with _STRATEGY_WRITE_LOCKS_GUARD:
        lock = _STRATEGY_WRITE_LOCKS.get(sid)
        if lock is None:
            lock = Lock()
            _STRATEGY_WRITE_LOCKS[sid] = lock
            # P15/D-M24 — evict only if NOT CURRENTLY HELD. Otherwise we'd
            # drop a lock mid-acquire, defeating the serialization guarantee.
            while len(_STRATEGY_WRITE_LOCKS) > _STRATEGY_WRITE_LOCKS_MAX:
                oldest_sid, oldest_lock = next(iter(_STRATEGY_WRITE_LOCKS.items()))
                if oldest_lock.acquire(blocking=False):
                    try:
                        _STRATEGY_WRITE_LOCKS.pop(oldest_sid, None)
                    finally:
                        oldest_lock.release()
                else:
                    # Held lock at the front of the LRU — skip and retry on the
                    # next call rather than blocking the caller.
                    break
        else:
            _STRATEGY_WRITE_LOCKS.move_to_end(sid)
        return lock


def run_paper_trade(
    strategy_id: int,
    *,
    window_days: int = DEFAULT_FORWARD_DAYS,
) -> PaperTradeResult:
    """Forward-simulate a single strategy over the last `window_days` of data.

    The strategy must have a non-empty `formula_code` (i.e. has been through
    CODE_GEN at least once). The result is persisted to disk + returned.

    D-M13 — wraps the ENTIRE run (sandbox + backtest + persist) in the
    per-strategy lock so concurrent /api/paper-trade/run + scheduler ticks
    don't duplicate expensive LLM/sandbox work. The previous narrow lock only
    around _persist() left the heavy lifting unprotected.
    """
    window_days = max(7, min(180, int(window_days)))

    # D-M13 — acquire BEFORE any heavy work.
    lock = _get_strategy_lock(int(strategy_id))
    with lock:
        with session_scope() as s:
            row = s.get(AlphaStrategy, strategy_id)
            if row is None:
                raise LookupError(f"AlphaStrategy {strategy_id} not found")
            factor_code = (row.formula_code or "").strip()
            name = row.name or f"strategy_{strategy_id}"
        if not factor_code:
            raise LookupError(
                f"Strategy {strategy_id} has no factor_code yet — run the pipeline first."
            )

        df = _load_recent_market(window_days)

        try:
            sandbox_res = safe_execute_factor(factor_code, df)
        except (SandboxValidationError, SandboxExecutionError) as exc:
            # Paper trade failure mirrors a backtest failure — record + raise.
            raise RuntimeError(f"Sandbox rejected factor code: {exc}") from exc

        bt = AlphaBacktester(df.reset_index(), sandbox_res.signal).run()
        bt_dict = bt.to_dict()
        healthy, notes = _health_check(bt_dict["metrics"], int(bt_dict["trades"]))
        run_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        result = PaperTradeResult(
            strategy_id=strategy_id,
            window_days=window_days,
            metrics=bt_dict["metrics"],
            equity_curve=bt_dict["equity_curve"],
            trades=int(bt_dict["trades"]),
            is_healthy=healthy,
            health_notes=notes,
            run_at=run_at,
            per_bar=bt_dict.get("per_bar") or [],  # B7-1: propagate tape for live dashboard
        )
        _persist(result, name=name)
        return result


def _persist(result: PaperTradeResult, *, name: str) -> None:
    """Atomic write of the latest paper-trade payload.

    D-M13 — the per-strategy lock has been hoisted to `run_paper_trade` so
    this function no longer takes it (caller already holds it). Atomic
    `os.replace` still protects against partial reads.
    """
    out_path = PAPER_DIR / f"{result.strategy_id}.json"
    payload = {
        **result.to_dict(),
        "name": name,
    }
    tmp_path = out_path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    os.replace(tmp_path, out_path)


# mtime-keyed cache for the per-strategy latest payload. Avoids re-parsing
# the JSON on every /api/paper-trade poll when nothing on disk has changed.
# Bounded so a fleet of thousands of strategies cannot blow process memory.
_LATEST_CACHE: Dict[int, Tuple[float, Optional[Dict[str, Any]]]] = {}
_LATEST_CACHE_LOCK = Lock()
_LATEST_CACHE_MAX = 128


def _latest_path(strategy_id: int) -> Path:
    return PAPER_DIR / f"{strategy_id}.json"


def latest_mtime(strategy_id: int) -> Optional[float]:
    """Return the on-disk mtime of the latest paper-trade payload, or None."""
    p = _latest_path(strategy_id)
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return None


def latest_for(strategy_id: int) -> Optional[Dict[str, Any]]:
    p = _latest_path(strategy_id)
    try:
        mtime = p.stat().st_mtime
    except FileNotFoundError:
        with _LATEST_CACHE_LOCK:
            _LATEST_CACHE.pop(int(strategy_id), None)
        return None
    sid = int(strategy_id)
    with _LATEST_CACHE_LOCK:
        cached = _LATEST_CACHE.get(sid)
        if cached and cached[0] == mtime:
            return cached[1]
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        # P29-S6: previously silent — corrupt payload would disappear with
        # no log line to attribute the gap to. Tolerate the failure but log.
        logger.warning(
            "paper_trader.latest_for: unreadable payload sid=%s path=%s err=%s",
            sid, p, exc,
        )
        payload = None
    with _LATEST_CACHE_LOCK:
        # P15/D-M6 — FIFO eviction: Python 3.7+ guarantees dict iteration in
        # insertion order, so popping the first key drops the oldest entry.
        # Trade-off vs LRU: simpler, no per-access bookkeeping, sufficient for
        # the 128-entry cap. Hot keys keep getting re-inserted by `latest_for`
        # on cache-miss, so they don't accumulate at the bottom of the dict.
        # D-M13/P16 — only evict when we're about to ADD a NEW key. The prior
        # logic evicted-then-overwrote even when `sid` was already in the
        # cache, off-by-one-ing the actual entry count down by 1.
        if sid not in _LATEST_CACHE and len(_LATEST_CACHE) >= _LATEST_CACHE_MAX:
            _LATEST_CACHE.pop(next(iter(_LATEST_CACHE)))
        _LATEST_CACHE[sid] = (mtime, payload)
    return payload


def list_all() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for path in sorted(PAPER_DIR.glob("*.json")):
        try:
            sid = int(path.stem)
        except ValueError:
            # Non-integer filename — not written by this module; skip silently.
            continue
        payload = latest_for(sid)  # uses mtime-keyed cache; re-reads only on change
        if payload is None:
            # File disappeared or is corrupt — latest_for already logged the error.
            continue
        out.append(payload)
    return out


__all__ = [
    "PaperTradeResult",
    "run_paper_trade",
    "latest_for",
    "latest_mtime",
    "list_all",
    "DEFAULT_FORWARD_DAYS",
]

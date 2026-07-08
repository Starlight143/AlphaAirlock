"""Parameter-sweep runner (P7-03 — /backtest-lab).

Re-runs ``AlphaBacktester`` for every cell of a 1D/2D grid over whitelisted
strategy params and aggregates per-cell metrics. Default OFF via
``BACKTEST_LAB_ENABLED=0``; even when enabled, hard caps prevent runaway
compute:

* ``BACKTEST_LAB_MAX_CELLS`` (default 25 = 5×5)
* ``BACKTEST_LAB_WORKERS`` (default 2, max 4)
* In-process single-flight lock per strategy_id (concurrent POST → 409)
* Per-cell wall-clock cap (60 s) — cell stored as ``{error: 'timeout'}``

Only metrics go into ``results_json`` (no equity curves) so a full 100-cell
sweep stays under 25 KB.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from sqlalchemy.orm import Session

from backend._envloader import env_bool, env_int
from backend.core.database import (
    AlphaStrategy,
    BacktestSweep,
    PROJECT_ROOT,
    session_scope,
)
from backend.core.engine import AlphaBacktester
from backend.core.sandbox import (
    SandboxExecutionError,
    SandboxValidationError,
    _run_with_timeout,
    safe_execute_factor,
)

logger = logging.getLogger("alpha.backtest_lab")

DATA_CSV: Path = PROJECT_ROOT / "backend" / "data" / "synthetic_btc.csv"

# Whitelist of sweepable parameters with their type, range, and the path into
# the strategy config that mutates when the value is set. The applier is a
# pure function that takes a deep-copied backtest_config + the cell value.
#
# ``effective``: whether sweeping this axis CURRENTLY changes a cell's metrics.
# Only fee_bps / slippage_bps reach the backtest math today: _run_one_cell
# re-runs a fixed formula_code string through safe_execute_factor (whose
# sandbox globals expose only pd/np — see sandbox._build_globals) and
# normalize_to_signal (window/thresholds hardcoded — see sandbox.normalize_to_signal),
# then AlphaBacktester(price_df, signal, fee, slippage) (engine.AlphaBacktester
# takes no config). The signal/lookback/sizing/risk_limits values written into
# backtest_config by _apply_value are therefore never read, so a sweep over them
# returns an identical metric for every cell. They are kept in the whitelist (so
# _apply_value still type-checks/clamps them) but marked effective=False so
# list_params does not advertise them and _validate_param refuses them — an
# operator is never offered a no-op axis that silently renders a flat,
# "looks-robust" heatmap. Flip effective=True only once the value is actually
# wired into signal generation / the backtester.
#
# ``unit`` / ``label``: operator-facing display metadata. NOTE the fee_bps /
# slippage_bps KEYS keep their historical names (persisted in
# BacktestSweep.param_x_name and used as the frontend dropdown option values, so
# renaming them is a breaking data-model/API change), but the VALUE is a decimal
# FRACTION passed straight to AlphaBacktester(fee=/slippage=) where FEE_BPS=0.0005
# == 0.05% per side. 0.002 == 0.2% per side == 20 bps. The label spells this out
# so an operator does not mistake the [0, 0.002] range for basis points.
SWEEPABLE_PARAMS: Dict[str, Dict[str, Any]] = {
    "signal.window":             {"type": "int",   "min": 8,    "max": 720,  "effective": False, "unit": "bars",     "label": "signal.window (bars)"},
    "signal.threshold_entry":    {"type": "float", "min": -3.5, "max": 3.5,  "effective": False, "unit": "zscore",   "label": "signal.threshold_entry (z-score)"},
    "signal.threshold_exit":     {"type": "float", "min": -3.5, "max": 3.5,  "effective": False, "unit": "zscore",   "label": "signal.threshold_exit (z-score)"},
    "lookback_bars":             {"type": "int",   "min": 24,   "max": 2160, "effective": False, "unit": "bars",     "label": "lookback_bars (bars)"},
    "sizing.value":              {"type": "float", "min": 0.1,  "max": 1.0,  "effective": False, "unit": "fraction", "label": "sizing.value (fraction)"},
    "risk_limits.max_position":  {"type": "float", "min": 0.25, "max": 1.0,  "effective": False, "unit": "fraction", "label": "risk_limits.max_position (fraction)"},
    "risk_limits.stop_loss_pct": {"type": "float", "min": 0.005,"max": 0.10, "effective": False, "unit": "fraction", "label": "risk_limits.stop_loss_pct (fraction, 0.01 = 1%)"},
    "risk_limits.take_profit_pct": {"type": "float","min": 0.005,"max": 0.20, "effective": False, "unit": "fraction", "label": "risk_limits.take_profit_pct (fraction, 0.01 = 1%)"},
    "fee_bps":                   {"type": "float", "min": 0.0,  "max": 0.002, "effective": True, "unit": "fraction", "label": "fee per side (fraction, 0.0005 = 5 bps)"},
    "slippage_bps":              {"type": "float", "min": 0.0,  "max": 0.002, "effective": True, "unit": "fraction", "label": "slippage per side (fraction, 0.0002 = 2 bps)"},
}

# In-process per-strategy lock so two sweeps on the same strategy serialize.
# D-M11/P16 — bounded LRU so a fleet of strategies that runs a single sweep
# and never again can't leak unlimited Lock objects. Mirrors the held-lock-
# safe eviction pattern from paper_trader._STRATEGY_WRITE_LOCKS: we only drop
# the oldest entry if we can acquire its lock non-blockingly so we never evict
# a lock another thread is depending on (preserves serialization guarantee).
_STRATEGY_LOCKS_MAX = 5000
_STRATEGY_LOCKS: "OrderedDict[int, threading.Lock]" = OrderedDict()
_STRATEGY_LOCKS_GUARD = threading.Lock()

# Process-wide ceiling on concurrent sweeps. Even with the per-strategy lock a
# fleet of distinct strategy_ids could each kick off a ThreadPoolExecutor at
# the same time and saturate the host. Capping at (cpu // 2) leaves headroom
# for the API server, the agent workers, and the OS itself.
# P15/D-M16 — expose the cap as an env var so deployments with extra capacity
# can raise it without code edits. Default keeps the conservative
# "use at most half the cores" behaviour.
_GLOBAL_SWEEP_SEM = threading.BoundedSemaphore(
    env_int(
        "BACKTEST_LAB_GLOBAL_CONCURRENCY",
        max(1, (os.cpu_count() or 2) // 2),
        minimum=1,
        maximum=16,
    )
)


def is_enabled() -> bool:
    return env_bool("BACKTEST_LAB_ENABLED", False)


def max_cells() -> int:
    base = env_int("BACKTEST_LAB_MAX_CELLS", 25, minimum=2, maximum=200)
    if env_bool("BACKTEST_LAB_ALLOW_LARGE", False):
        return min(100, max(base, 25))
    return min(base, 25)


def workers() -> int:
    return env_int("BACKTEST_LAB_WORKERS", 2, minimum=1, maximum=4)


def _strategy_lock(sid: int) -> threading.Lock:
    """D-M11/P16 — bounded LRU per-strategy lock with held-lock-safe eviction.

    Mirrors `paper_trader._get_strategy_lock`. Insertion order tracks LRU; we
    move to end on every successful lookup and evict from the front only if
    the oldest lock can be acquired non-blockingly (i.e. nobody currently
    holds it).
    """
    with _STRATEGY_LOCKS_GUARD:
        lk = _STRATEGY_LOCKS.get(sid)
        if lk is None:
            lk = threading.Lock()
            _STRATEGY_LOCKS[sid] = lk
            while len(_STRATEGY_LOCKS) > _STRATEGY_LOCKS_MAX:
                oldest_sid, oldest_lock = next(iter(_STRATEGY_LOCKS.items()))
                if oldest_lock.acquire(blocking=False):
                    try:
                        _STRATEGY_LOCKS.pop(oldest_sid, None)
                    finally:
                        oldest_lock.release()
                else:
                    # Held lock at the front of the LRU — skip and retry on
                    # the next call rather than blocking the caller.
                    break
        else:
            _STRATEGY_LOCKS.move_to_end(sid)
        return lk


def _validate_param(name: str, values: Sequence[Any]) -> List[float]:
    spec = SWEEPABLE_PARAMS.get(name)
    if spec is None:
        raise ValueError(f"param '{name}' not in SWEEPABLE_PARAMS whitelist")
    if not spec.get("effective", True):
        # The axis is whitelisted (so _apply_value can type-check it) but does
        # not yet affect cell output, which would yield a misleadingly flat
        # heatmap. Refuse it up front instead of silently producing no-op cells.
        raise ValueError(
            f"param '{name}' is not yet wired into the backtest "
            f"(sweeping it has no effect on results); choose an effective param"
        )
    out: List[float] = []
    lo = float(spec["min"])
    hi = float(spec["max"])
    for v in values:
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"param '{name}' value {v!r} not numeric")
        if not (lo <= x <= hi):
            raise ValueError(f"param '{name}' value {x} out of range [{lo}, {hi}]")
        out.append(x)
    if len(out) < 1:
        raise ValueError(f"param '{name}' must have at least 1 value")
    return out


def _apply_value(
    cfg: Dict[str, Any], param_name: str, value: float
) -> Tuple[Dict[str, Any], Optional[float], Optional[float]]:
    """Apply param value into a deep-copied config. Returns (mutated_cfg, fee, slippage).

    P31-SWEEP-COST1: fee/slip are returned as ``None`` when *this* axis is not the
    corresponding cost axis, and as the (possibly 0.0) swept value when it is. A
    genuine ``0.0`` sweep endpoint must be distinguishable from "axis not swept"
    so the zero-cost endpoint of a cost-sensitivity sweep isn't silently replaced
    by defaults downstream. ``None`` is the "use default" sentinel; ``0.0`` is honored.
    """
    cfg = copy.deepcopy(cfg)
    spec = SWEEPABLE_PARAMS.get(param_name) or {}
    if spec.get("type") == "int":
        v: Any = int(round(value))
    else:
        v = float(value)
    # Top-level fee/slippage are special — they're passed to AlphaBacktester
    # constructor rather than into the strategy config.
    if param_name == "fee_bps":
        return cfg, float(v), None
    if param_name == "slippage_bps":
        return cfg, None, float(v)
    # Dotted path → nested dict mutation.
    parts = param_name.split(".")
    target: Any = cfg.setdefault("backtest_config", {})
    for p in parts[:-1]:
        target = target.setdefault(p, {})
    target[parts[-1]] = v
    return cfg, None, None


def create_sweep(
    session: Session,
    *,
    strategy_id: int,
    param_x_name: str,
    param_x_values: Sequence[Any],
    param_y_name: Optional[str] = None,
    param_y_values: Optional[Sequence[Any]] = None,
    seed: int = 42,
) -> BacktestSweep:
    """Validate + persist a queued sweep row. Caller runs ``run_sweep`` async."""
    if not is_enabled():
        raise PermissionError("BACKTEST_LAB_ENABLED=0 — sweep refused")
    strat = session.get(AlphaStrategy, int(strategy_id))
    if strat is None:
        raise ValueError(f"strategy {strategy_id} not found")
    xs = _validate_param(param_x_name, param_x_values)
    ys: Optional[List[float]] = None
    if param_y_name:
        # P27 — defense-in-depth: a 2D sweep where X and Y reference the same
        # whitelisted param is logically a 1D sweep with two conflicting axes,
        # and historically allowed the UI's stale Y defaults (e.g. yMin=0.5)
        # to slip past _validate_param's range gate for the chosen param
        # (e.g. signal.window ∈ [8, 720]) only after the request reached this
        # function, producing a confusing HTTP 422 mid-flow. Reject early so
        # direct API callers see the constraint up front.
        if param_y_name == param_x_name:
            raise ValueError(
                f"param_y_name must differ from param_x_name "
                f"(both are '{param_x_name}')"
            )
        if not param_y_values:
            raise ValueError("param_y_values required when param_y_name set")
        ys = _validate_param(param_y_name, param_y_values)
    cells_total = len(xs) * (len(ys) if ys else 1)
    if cells_total > max_cells():
        raise ValueError(f"grid too large: {cells_total} cells > cap {max_cells()}")

    # P29-T10: single-flight check + INSERT must be atomic against concurrent
    # POSTs for the same strategy. Acquire per-strategy lock BEFORE the SELECT
    # to close the TOCTOU window without requiring a schema change.
    with _strategy_lock(int(strategy_id)):
        running = (
            session.query(BacktestSweep)
            .filter(BacktestSweep.strategy_id == int(strategy_id))
            .filter(BacktestSweep.status.in_(("queued", "running")))
            .first()
        )
        if running is not None:
            raise PermissionError(f"sweep {running.id} already running for strategy {strategy_id}")

        row = BacktestSweep(
            strategy_id=int(strategy_id),
            param_x_name=param_x_name,
            param_x_values=json.dumps(xs, default=str),
            param_y_name=param_y_name,
            param_y_values=(json.dumps(ys, default=str) if ys else None),
            cells_total=cells_total,
            cells_done=0,
            status="queued",
            results_json="[]",
            seed=int(seed),
        )
        session.add(row)
        session.flush()
        return row


def cancel_sweep(session: Session, sweep_id: int) -> bool:
    row = session.get(BacktestSweep, int(sweep_id))
    if row is None:
        return False
    if row.status in ("done", "cancelled", "failed", "partial"):
        return False
    row.status = "cancelled"
    return True


def _run_one_cell(
    formula_code: str,
    price_df: pd.DataFrame,
    cfg: Dict[str, Any],
    fee: Optional[float],
    slippage: Optional[float],
) -> Dict[str, float]:
    """Execute one sweep cell (sandbox + AlphaBacktester) and return metrics.

    WARNING: this runs in a non-daemon worker thread. fut.result(timeout=60)
    cancels the Future return path, NOT the running thread; a runaway
    AlphaBacktester or sandbox call will continue burning CPU until natural
    completion. The process-level _GLOBAL_SWEEP_SEM caps concurrent sweeps
    to (cpu // 2) so a leak cannot saturate the host.
    """
    sandbox = safe_execute_factor(formula_code, price_df, timeout_seconds=8.0)
    # P31-SWEEP-COST1: ``None`` means "axis not swept → use default"; a swept
    # value (including a deliberate 0.0 zero-cost endpoint) is honored verbatim.
    # Negative inputs are clamped to 0.0 (costs cannot be negative).
    _bt_obj = AlphaBacktester(
        price_df,
        sandbox.signal,
        fee=(0.0005 if fee is None else max(0.0, float(fee))),
        slippage=(0.0002 if slippage is None else max(0.0, float(slippage))),
    )
    # P-SWEEP-TIMEOUT: bound the backtest run() the same way the sandbox exec is
    # bounded. run() executes in this non-daemon worker thread with no timeout of
    # its own; a pathological signal causing a heavy per_bar build could run far
    # longer than the caller's fut.result() window and then block pool __exit__.
    bt = _run_with_timeout(_bt_obj.run, 30.0)
    m = bt.metrics
    return {
        "sharpe": float(m.get("annualized_sharpe", 0.0) or 0.0),
        "max_drawdown": float(m.get("max_drawdown", 0.0) or 0.0),
        "cum_return": float(m.get("cumulative_return", 0.0) or 0.0),
        "profit_factor": float(m.get("profit_factor", 0.0) or 0.0),
        "win_rate": float(m.get("win_rate", 0.0) or 0.0),
        "trades": int(bt.trades),
    }


def run_sweep(sweep_id: int) -> None:
    """Execute one sweep. Updates progress in DB after each cell."""
    started = time.monotonic()
    lock: Optional[threading.Lock] = None
    lock_held = False
    try:
        with session_scope() as s:
            row = s.get(BacktestSweep, int(sweep_id))
            if row is None or row.status == "cancelled":
                return
            sid = int(row.strategy_id)
            xs: List[float] = json.loads(row.param_x_values or "[]")
            ys: Optional[List[float]] = (
                json.loads(row.param_y_values) if row.param_y_values else None
            )
            x_name = row.param_x_name
            y_name = row.param_y_name
            strat = s.get(AlphaStrategy, sid)
            if strat is None:
                row.status = "failed"
                row.error_message = "strategy missing"
                return
            cfg_base = strat.config() or {}
            formula = strat.formula_code or ""
            # Do NOT set status='running' yet — acquire the per-strategy lock
            # first so no concurrent create_sweep call sees a 'running' row
            # before we actually hold the lock.

        lock = _strategy_lock(sid)
        try:
            lock.acquire()
            lock_held = True
        except Exception:
            raise

        # Now that we hold the lock, set status='running' atomically.
        try:
            with session_scope() as _s:
                _row = _s.get(BacktestSweep, int(sweep_id))
                if _row is None or _row.status in ("cancelled", "done", "partial", "failed"):
                    return
                _row.status = "running"
        except Exception:
            # If we cannot persist 'running', release the lock and propagate.
            lock.release()
            lock_held = False
            raise

        # Load price data once outside the worker loop.
        if not DATA_CSV.exists():
            with session_scope() as s:
                row = s.get(BacktestSweep, int(sweep_id))
                if row is not None:
                    row.status = "failed"
                    row.error_message = f"price data missing at {DATA_CSV}"
            return
        price_df = pd.read_csv(DATA_CSV)
        # AlphaBacktester handles the timestamp column itself.

        cells: List[Dict[str, Any]] = []
        max_workers = workers()
        # Process-level concurrency cap — see _GLOBAL_SWEEP_SEM. Acquired here
        # (after the per-strategy lock) so a queued sweep blocks on the global
        # ceiling rather than burning a worker slot mid-execution.
        with _GLOBAL_SWEEP_SEM:
            # P-SWEEP-TIMEOUT: build the (x, y, args) work list first, then run it
            # in batches of `max_workers`. The previous code submitted ALL futures
            # up-front and awaited result(timeout=60) in submission order, so a cell
            # that had been queued+running for minutes still got a FRESH 60s window
            # — the cap was not wall-clock. Batching means a future is awaited within
            # ~its own execution window, so the 60s is an effective per-cell ceiling.
            work: List[tuple] = []
            for xi in xs:
                if ys:
                    for yi in ys:
                        mut_cfg, fee_x, slip_x = _apply_value(cfg_base, x_name, xi)
                        if y_name:
                            mut_cfg, fee_y, slip_y = _apply_value(mut_cfg, y_name, yi)
                        else:
                            fee_y, slip_y = None, None
                        # P31-SWEEP-COST1: pick the axis that actually swept
                        # this cost (non-None wins); leave None so a genuine
                        # 0.0 endpoint is preserved and only a truly-unswept
                        # cost falls back to the default in _run_one_cell.
                        fee = fee_x if fee_x is not None else fee_y
                        slip = slip_x if slip_x is not None else slip_y
                        work.append((xi, yi, mut_cfg, fee, slip))
                else:
                    mut_cfg, fee, slip = _apply_value(cfg_base, x_name, xi)
                    work.append((xi, None, mut_cfg, fee, slip))

            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                for batch_start in range(0, len(work), max_workers):
                    # Cooperative cancellation check before launching each batch.
                    with session_scope() as s:
                        cur = s.get(BacktestSweep, int(sweep_id))
                        if cur is None or cur.status == "cancelled":
                            return
                    batch = work[batch_start:batch_start + max_workers]
                    futures = [
                        (bx, by, pool.submit(_run_one_cell, formula, price_df, bcfg, bfee, bslip))
                        for (bx, by, bcfg, bfee, bslip) in batch
                    ]
                    for xi, yi, fut in futures:
                        cell: Dict[str, Any] = {"x": xi, "y": yi}
                        try:
                            metrics = fut.result(timeout=60)
                            cell["metrics"] = metrics
                        except FutTimeout:
                            cell["error"] = "timeout"
                        except (SandboxValidationError, SandboxExecutionError) as exc:
                            cell["error"] = f"sandbox: {exc}"
                        except Exception as exc:  # noqa: BLE001
                            cell["error"] = f"runtime: {type(exc).__name__}: {exc}"
                        cells.append(cell)
                        # Persist progress after each cell so the polling UI can render.
                        with session_scope() as s:
                            cur = s.get(BacktestSweep, int(sweep_id))
                            if cur is None:
                                return
                            cur.results_json = json.dumps(cells, default=str)
                            cur.cells_done = len(cells)

        # Finalize.
        with session_scope() as s:
            cur = s.get(BacktestSweep, int(sweep_id))
            if cur is None:
                return
            errors = [c for c in cells if "error" in c]
            success = [c for c in cells if "metrics" in c]
            if not success:
                cur.status = "failed"
                cur.error_message = errors[0].get("error") if errors else "no cells succeeded"
            elif errors:
                cur.status = "partial"
            else:
                cur.status = "done"
            cur.duration_ms = int((time.monotonic() - started) * 1000)
    except Exception:  # noqa: BLE001
        logger.exception("run_sweep failed")
        try:
            with session_scope() as s:
                cur = s.get(BacktestSweep, int(sweep_id))
                if cur is not None and cur.status not in ("cancelled", "done"):
                    cur.status = "failed"
                    cur.error_message = "unhandled error — see server log"
        except Exception as _db_exc:  # noqa: BLE001
            logger.error(
                "run_sweep: failed to persist error status for sweep_id=%s: %s",
                sweep_id,
                _db_exc,
            )
    finally:
        # P31-CC1: ``lock.locked()`` returns True if ANY thread holds the
        # lock (not just this thread). Track local ownership explicitly so
        # we never call release() on a lock owned by another thread.
        if lock is not None and lock_held:
            try:
                lock.release()
            except RuntimeError:
                logger.warning("backtest_lab: lock.release() raised (already released?)")


def list_params() -> Dict[str, Any]:
    return {
        "params": [
            {
                "name": name,
                "type": spec["type"],
                "min": spec["min"],
                "max": spec["max"],
                "kind": "numeric",
                "unit": spec.get("unit", "numeric"),
                "label": spec.get("label", name),
                "effective": True,
            }
            for name, spec in SWEEPABLE_PARAMS.items()
            if spec.get("effective", True)
        ],
        "max_cells": max_cells(),
        "workers": workers(),
        "enabled": is_enabled(),
    }


__all__ = [
    "is_enabled",
    "max_cells",
    "create_sweep",
    "cancel_sweep",
    "run_sweep",
    "list_params",
    "SWEEPABLE_PARAMS",
]

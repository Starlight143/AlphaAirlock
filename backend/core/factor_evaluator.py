"""Factor formula evaluator (P7-08 — /factor-studio).

Reuses ``backend.core.sandbox.safe_execute_factor`` verbatim — same AST
whitelist + restricted globals + threaded watchdog used by the existing
Coder Agent. No new sandbox written.

Public API:

* :func:`evaluate` — run a candidate ``compute_factor(df)`` against a sliced
  price window and return IC + Sharpe + equity curve + monthly returns
* :func:`promote_to_strategy` — wrap the formula into a fresh AlphaStrategy
  and kick off the existing CODE_GEN → BACKTESTING pipeline

Per-IP rate limit lives in :mod:`backend.app.main` as a FastAPI dependency.
"""

from __future__ import annotations

import functools
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from sqlalchemy.orm import Session

from backend._envloader import env_bool, env_int
from backend.core.database import (
    AlphaStrategy,
    Factor,
    PROJECT_ROOT,
    session_scope,
)
from backend.core.engine import AlphaBacktester
from backend.core.sandbox import (
    SandboxExecutionError,
    SandboxValidationError,
    safe_execute_factor,
)

logger = logging.getLogger("alpha.factor_studio")

DATA_CSV: Path = PROJECT_ROOT / "backend" / "data" / "synthetic_btc.csv"
ASSET_PRICES_DIR: Path = PROJECT_ROOT / "storage" / "prices"
MAX_EVAL_TIMEOUT_SEC = 10.0
# R5/QR-7: minimum aligned (factor[t], return[t+1]) pairs for a meaningful
# Spearman IC. Below ~30 the IC confidence interval spans essentially [-1, 1] —
# noise. Gates that screen on IC must not treat a 4-5 sample correlation as real.
IC_MIN_BARS = 30


def is_enabled() -> bool:
    return env_bool("FACTOR_STUDIO_ENABLED", True)


def rate_limit_per_min() -> int:
    return env_int("FACTOR_STUDIO_EVAL_RATE_PER_MIN", 10, minimum=1, maximum=600)


def _auto_fetch_enabled() -> bool:
    """Whether to download a per-asset price CSV on demand when a strategy
    requests a pair we don't have yet (P-REALDATA). Default ON."""
    return env_bool("MARKET_DATA_AUTO_FETCH", True)


@functools.lru_cache(maxsize=4)
def _load_full_price_df_cached(source_path: str, mtime_ns: int, size_bytes: int) -> pd.DataFrame:
    """Parse a price CSV once per (path, mtime, size). P30-R10: size_bytes is
    part of the cache key alongside mtime_ns to defend against same-mtime
    overwrites on coarse-granularity filesystems."""
    _ = mtime_ns  # part of cache key; intentionally unused
    _ = size_bytes  # part of cache key; intentionally unused
    df = pd.read_csv(source_path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()
    # P32-D4 / DAT32-5 — drop duplicate timestamps (keep latest revision).
    df = df[~df.index.duplicated(keep="last")]
    return df


def _load_price_window(
    period_start: Optional[str],
    period_end: Optional[str],
    asset_symbol: str = "BTC",
) -> pd.DataFrame:
    # P12/C-L5 — prefer the real per-asset CSV via the canonical universe
    # resolver (equities are ``<SYM>.csv``, crypto ``<SYM>-USDT.csv``); otherwise
    # fall back to the bundled synthetic BTC series so the factor studio still
    # works in a fresh checkout.
    sym = (asset_symbol or "BTC").strip().upper() or "BTC"
    from backend.core import universe
    asset_csv = universe.price_csv_path(sym)
    # P-REALDATA — on-demand fetch: if a strategy asks for an instrument we have
    # not downloaded yet, pull its real data now, routed per asset class (crypto
    # via Binance, equities via Yahoo). Best-effort, gated by MARKET_DATA_AUTO_FETCH
    # (default ON); never blocks evaluation on failure.
    if not asset_csv.exists() and _auto_fetch_enabled():
        try:
            if universe.ensure_price_data(sym):
                logger.info("factor_evaluator: ensured real data for %s", sym)
        except Exception:  # noqa: BLE001
            logger.warning("factor_evaluator: auto-fetch of %s failed; "
                           "falling back to bundled series", sym, exc_info=True)
    if asset_csv.exists():
        source = asset_csv
    elif DATA_CSV.exists():
        source = DATA_CSV
    else:
        raise RuntimeError(
            f"price data missing — neither {asset_csv} nor {DATA_CSV} exists"
        )
    # P29-C3: cache parsed DataFrame by (path, mtime). Mutates cache when file changes.
    # P30-R10: (path, mtime, size) cache key. Size catches same-mtime
    # overwrites that coarse-mtime filesystems (FAT32, network mounts)
    # would otherwise serve as stale data.
    try:
        _stat = source.stat()
        mtime_ns = _stat.st_mtime_ns
        size_bytes = int(_stat.st_size)
    except OSError:
        mtime_ns = 0
        size_bytes = 0
    full_df = _load_full_price_df_cached(str(source), mtime_ns, size_bytes)
    df = full_df
    if period_start:
        try:
            ts_start = pd.Timestamp(period_start, tz="UTC")
            df = df[df.index >= ts_start]
        except (ValueError, TypeError) as exc:
            # P13/D-L3 — surface bad operator input instead of silently
            # ignoring it; otherwise the factor studio shows full-history IC
            # when the user expected a narrow window.
            logger.warning(
                "factor_evaluator: ignoring invalid period_start=%r (%s)",
                period_start, exc,
            )
    if period_end:
        try:
            ts_end = pd.Timestamp(period_end, tz="UTC")
            df = df[df.index <= ts_end]
        except (ValueError, TypeError) as exc:
            logger.warning(
                "factor_evaluator: ignoring invalid period_end=%r (%s)",
                period_end, exc,
            )
    # P29-C3: hand callers an independent copy so AlphaBacktester /
    # safe_execute_factor mutations cannot poison subsequent calls that
    # share the cache entry.
    out = df.copy()
    # P-REALDATA — pad the sandbox's required derivative columns when a 6-col
    # per-asset CSV (OHLCV-only universe / on-demand fetch) is loaded, so
    # safe_execute_factor's REQUIRED_LOWERCASE_COLS check never rejects a real
    # altcoin series. Real BTC already carries these; altcoins get 0.0 (the free
    # data source has no per-altcoin funding/OI — honest neutral value).
    for _col in ("open_interest", "funding_rate", "liquidations"):
        if _col not in out.columns:
            out[_col] = 0.0
    return out


def _time_series_ic(factor: pd.Series, returns: pd.Series) -> Optional[float]:
    """Spearman rank-IC of factor[t] vs returns[t+1] (lag-1 forward returns)."""
    if factor is None or returns is None:
        return None
    s_fwd = returns.shift(-1)
    df = pd.concat([factor, s_fwd], axis=1, join="inner").dropna()
    if len(df) < IC_MIN_BARS:  # R5/QR-7: was < 5 — too few for a meaningful IC
        return None
    try:
        ic = df.iloc[:, 0].corr(df.iloc[:, 1], method="spearman")
    except Exception:  # noqa: BLE001
        return None
    if ic is None or not math.isfinite(ic):
        return None
    return round(max(-1.0, min(1.0, float(ic))), 6)


def _monthly_returns_from_equity(eq_curve: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not eq_curve:
        return []
    rows = [(pd.Timestamp(p["timestamp"]), float(p.get("equity", 1.0))) for p in eq_curve if p.get("timestamp")]
    if not rows:
        return []
    s = pd.Series({ts: eq for ts, eq in rows}).sort_index()
    monthly_eq = s.resample("1ME").last()
    # P25 — inf-guard. If a strategy's monthly equity touches zero (total loss
    # in a single month), pct_change() produces ±inf; ``round(float(inf), 6)``
    # is still inf which then breaks JSON serialization in the /factor-studio/
    # evaluate endpoint response (inf is non-JSON per RFC 8259). Mirror the
    # ``portfolio_optimizer.py:181`` pattern.
    monthly_ret = (
        monthly_eq.pct_change().dropna()
        .replace([np.inf, -np.inf], np.nan).fillna(0.0)
    )
    out: List[Dict[str, Any]] = []
    for ts, r in monthly_ret.items():
        out.append({
            "year": int(ts.year),
            "month": int(ts.month),
            "ret": round(float(r), 6),
        })
    return out


def evaluate(
    formula_code: str,
    *,
    asset_symbol: str = "BTC",
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    timeout_seconds: float = 8.0,
) -> Dict[str, Any]:
    """Sandbox-execute the formula and run a full backtest. Raises
    SandboxValidationError / SandboxExecutionError on failure paths so the
    endpoint can map them to 400 / 422.
    """
    timeout = min(float(timeout_seconds or 8.0), MAX_EVAL_TIMEOUT_SEC)
    started = time.monotonic()

    df = _load_price_window(period_start, period_end, asset_symbol=asset_symbol)
    if df.empty:
        raise SandboxExecutionError(
            f"No price data in window [{period_start}, {period_end}]"
        )

    sandbox_result = safe_execute_factor(formula_code, df, timeout_seconds=timeout)
    signal = sandbox_result.signal
    backtest = AlphaBacktester(df, signal).run()
    metrics = backtest.metrics
    eq_curve = backtest.equity_curve

    # P30-R4: compute IC against the continuous factor (not the ternary
    # signal). Spearman rank-IC on a 3-valued series collapses to a
    # contingency-like statistic — gross loss of information that made
    # strong factors look statistically indistinguishable from noise.
    hourly_returns = df["close"].pct_change().replace([np.inf, -np.inf], np.nan)
    ic = _time_series_ic(sandbox_result.factor, hourly_returns)
    monthly_returns = _monthly_returns_from_equity(eq_curve)

    elapsed = max(0.001, time.monotonic() - started)
    warnings: List[str] = []
    warnings.append(
        "IC is computed in-sample on the selected price window — "
        "do not promote a formula based solely on in-sample IC; "
        "validate on a held-out period before adding to the pipeline."
    )
    if len(df) < 200:
        warnings.append(f"Short window ({len(df)} bars) — IC/Sharpe may be noisy")
    if ic is None:
        warnings.append("IC undefined (insufficient overlap or zero variance)")

    return {
        "ic": ic,
        "sharpe": float(metrics.get("annualized_sharpe", 0.0) or 0.0),
        "max_drawdown": float(metrics.get("max_drawdown", 0.0) or 0.0),
        "win_rate": float(metrics.get("win_rate", 0.0) or 0.0),
        "profit_factor": float(metrics.get("profit_factor", 0.0) or 0.0),
        "cumulative_return": float(metrics.get("cumulative_return", 0.0) or 0.0),
        "equity_curve": eq_curve,
        "monthly_returns": monthly_returns,
        "n_bars": int(len(df)),
        "elapsed_seconds": round(elapsed, 3),
        "warnings": warnings,
        "asset_symbol": asset_symbol,
    }


def save_factor(
    session: Session,
    *,
    name: str,
    formula_code: str,
    asset_symbol: str = "BTC",
    params: Optional[Dict[str, Any]] = None,
    eval_result: Optional[Dict[str, Any]] = None,
    overwrite: bool = False,
) -> Factor:
    import json

    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    existing = session.query(Factor).filter(Factor.name == name).one_or_none()
    if existing is not None and not overwrite:
        raise ValueError(f"Factor '{name}' already exists; pass overwrite=true to replace")
    row = existing or Factor(name=name)
    row.formula_code = formula_code
    row.asset_symbol = asset_symbol
    row.params_json = json.dumps(params or {}, default=str)
    if eval_result is not None:
        # P11-R2 (round-2): only cache a real IC; None = eval failed/absent, so keep
        # the prior cached value rather than corrupting it to 0.0.
        _ic_val = eval_result.get("ic")
        if _ic_val is not None:
            row.ic_score_cached = float(_ic_val)
        row.sharpe_cached = float(eval_result.get("sharpe") or 0.0)
        row.updated_at = datetime.now(timezone.utc)
    if existing is None:
        session.add(row)
    session.flush()
    return row


def promote_to_strategy(session: Session, factor_id: int, *, name_override: Optional[str] = None) -> int:
    """Wrap a saved Factor into a new AlphaStrategy + run BACKTESTING.

    The new strategy reuses ``Factor.formula_code`` so the existing
    orchestrator pipeline can run it as if it had come from CODE_GEN.
    """
    import json

    row = session.get(Factor, int(factor_id))
    if row is None:
        raise ValueError(f"Factor {factor_id} not found")
    strat_name = (name_override or row.name).strip() or row.name
    cfg = {"alpha_category": "factor_studio", "from_factor_id": int(row.id)}
    new_strat = AlphaStrategy(
        name=strat_name,
        stage=2,  # CODE_GEN done — ready for BACKTESTING
        formula_code=row.formula_code or "",
        config_json=json.dumps(cfg, default=str),
        status="CODE_GEN",
    )
    session.add(new_strat)
    session.flush()
    new_id = int(new_strat.id)
    row.promoted_strategy_id = new_id
    session.flush()
    return new_id


__all__ = [
    "evaluate",
    "save_factor",
    "promote_to_strategy",
    "is_enabled",
    "rate_limit_per_min",
    "MAX_EVAL_TIMEOUT_SEC",
]

"""Tradable instrument universe + per-symbol market-data loading (multi-asset).

Single source of truth for WHICH instruments the autonomous pipeline may
propose strategies on, spanning two asset classes:

  * ``crypto`` — Binance USDT-perp bases (BTC, ETH, SOL, …). Data already lives
                 on disk under ``storage/prices/<BASE>-USDT.csv`` (BTC is the
                 full 9-col primary; altcoins are 6-col OHLCV). Produced by
                 :func:`backend.core.market_data.ingest_universe`.
  * ``equity`` — US equities / ETFs (AAPL, MSFT, SPY, …) fetched on demand from
                 Yahoo Finance (yfinance) at 1h granularity and cached under
                 ``storage/prices/<TICKER>.csv`` (6-col OHLCV).

Why a dedicated module
----------------------
The backtester (:mod:`backend.core.engine`) is *hourly*: it annualizes Sharpe
with ``sqrt(8760)`` and derives elapsed years from ``bar_count / 8760``.
Equities must therefore ride the SAME continuous hourly grid as crypto
(overnight / weekend bars flat-filled) so the annualization stays internally
consistent — feeding daily bars would inflate Sharpe by ``~sqrt(34.8)`` and let
junk strategies pass the gates. This module guarantees every instrument is
loaded with the identical 8-column, hourly-grid contract the sandbox + engine
expect, padding the derivative columns (``open_interest`` / ``funding_rate`` /
``liquidations``) to ``0.0`` for any series that lacks them — which today is
every non-BTC instrument (the free data source only carries those for BTC).

Additive by design
-------------------
This module never changes crypto-BTC behavior. When the master switch
``STRATEGY_MULTI_SYMBOL_ENABLED`` is off, or a symbol resolves to BTC, the
pipeline path is byte-for-byte identical to the original BTC-only flow.

Configuration (all env-overridable, sane production defaults)
-------------------------------------------------------------
``STRATEGY_MULTI_SYMBOL_ENABLED``  bool   default ``true``  — master kill-switch.
``STRATEGY_UNIVERSE_MODE``         enum   default ``mixed`` — ``crypto`` | ``equity`` | ``mixed``.
``STRATEGY_SYMBOL_SELECTION``      enum   default ``rotate``— ``rotate`` (round-robin) | ``random``.
``STRATEGY_CRYPTO_SYMBOLS``        csv    default = market_data.DEFAULT_UNIVERSE bases.
``STRATEGY_EQUITY_SYMBOLS``        csv    default = :data:`DEFAULT_EQUITY_SYMBOLS`.
``EQUITY_DATA_MAX_AGE_DAYS``       int    default ``3``     — re-fetch a cached equity CSV older than this.

Quick verification::

    python -m backend.core.universe        # prints the active universe + paths
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from backend._envloader import env_bool, env_int, env_str
from backend.core.database import PROJECT_ROOT

logger = logging.getLogger("alpha.universe")

# --------------------------------------------------------------------------- #
# Paths + schema contract                                                     #
# --------------------------------------------------------------------------- #

PRICES_DIR: Path = PROJECT_ROOT / "storage" / "prices"
BUNDLED_BTC_CSV: Path = PROJECT_ROOT / "backend" / "data" / "synthetic_btc.csv"

# Columns the sandbox (REQUIRED_LOWERCASE_COLS) and the Coder contract assume
# exist on every frame. The first five come from OHLCV; the last three are
# derivative columns that only BTC carries with real values.
FEATURE_COLS = (
    "open", "high", "low", "close", "volume",
    "open_interest", "funding_rate", "liquidations",
)
DERIVATIVE_COLS = ("open_interest", "funding_rate", "liquidations")

# Curated, liquid, Yahoo-available default equities/ETFs with usable 1h history.
# None of these collide with a crypto base in market_data.DEFAULT_UNIVERSE.
DEFAULT_EQUITY_SYMBOLS = (
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD",
    "NFLX", "JPM", "SPY", "QQQ",
)


@dataclass(frozen=True)
class Instrument:
    """A single tradable instrument the pipeline can target."""

    app_symbol: str   # canonical asset_symbol key (e.g. "BTC", "ETH", "AAPL", "SPY")
    asset_class: str  # "crypto" | "equity"
    provider: str     # "binance_public" | "yahoo"
    csv_name: str     # filename under storage/prices (e.g. "BTC-USDT.csv", "AAPL.csv")
    display: str       # human label (e.g. "BTC-USDT", "AAPL")

    def to_dict(self) -> Dict[str, str]:
        return {
            "app_symbol": self.app_symbol,
            "asset_class": self.asset_class,
            "provider": self.provider,
            "csv_name": self.csv_name,
            "display": self.display,
        }


# --------------------------------------------------------------------------- #
# Configuration (env-overridable)                                             #
# --------------------------------------------------------------------------- #

def multi_symbol_enabled() -> bool:
    """Master kill-switch. When off, the pipeline only ever targets BTC and the
    behavior is identical to the original single-asset flow."""
    return env_bool("STRATEGY_MULTI_SYMBOL_ENABLED", True)


def universe_mode() -> str:
    """``crypto`` | ``equity`` | ``mixed`` (default ``mixed``)."""
    m = (env_str("STRATEGY_UNIVERSE_MODE", "mixed") or "mixed").strip().lower()
    return m if m in {"crypto", "equity", "mixed"} else "mixed"


def selection_mode() -> str:
    """``rotate`` (deterministic round-robin) | ``random`` (default ``rotate``)."""
    m = (env_str("STRATEGY_SYMBOL_SELECTION", "rotate") or "rotate").strip().lower()
    return m if m in {"rotate", "random"} else "rotate"


def equity_data_max_age_days() -> int:
    """Cached equity CSVs older than this are re-fetched on next use. 0 disables
    the staleness re-fetch (fetch-if-missing only)."""
    return env_int("EQUITY_DATA_MAX_AGE_DAYS", 3, minimum=0, maximum=3650)


def _env_symbol_list(name: str) -> Optional[List[str]]:
    """Parse a comma-separated, upper-cased, de-duplicated symbol override.
    Returns ``None`` when unset/blank so callers fall back to their default."""
    raw = env_str(name, "")
    if not raw or not raw.strip():
        return None
    out: List[str] = []
    for tok in raw.split(","):
        t = tok.strip().upper()
        if t and t not in out:
            out.append(t)
    return out or None


def _default_crypto_bases() -> List[str]:
    """The crypto bases from market_data.DEFAULT_UNIVERSE (BTC first), de-duped."""
    from backend.core import market_data as md

    bases: List[str] = []
    for pair in md.DEFAULT_UNIVERSE:
        base = str(pair).split("-")[0].strip().upper()
        if base and base not in bases:
            bases.append(base)
    if "BTC" not in bases:
        bases.insert(0, "BTC")
    return bases


def crypto_symbols() -> List[str]:
    return _env_symbol_list("STRATEGY_CRYPTO_SYMBOLS") or _default_crypto_bases()


def equity_symbols() -> List[str]:
    return _env_symbol_list("STRATEGY_EQUITY_SYMBOLS") or list(DEFAULT_EQUITY_SYMBOLS)


# --------------------------------------------------------------------------- #
# Instrument construction + registry                                          #
# --------------------------------------------------------------------------- #

def _crypto_instrument(base: str) -> Instrument:
    b = (base or "").strip().upper()
    return Instrument(
        app_symbol=b, asset_class="crypto", provider="binance_public",
        csv_name=f"{b}-USDT.csv", display=f"{b}-USDT",
    )


def _equity_instrument(ticker: str) -> Instrument:
    t = (ticker or "").strip().upper()
    return Instrument(
        app_symbol=t, asset_class="equity", provider="yahoo",
        csv_name=f"{t}.csv", display=t,
    )


def btc_instrument() -> Instrument:
    return _crypto_instrument("BTC")


def _full_registry() -> Dict[str, Instrument]:
    """Every instrument the system knows how to resolve, regardless of the
    active mode — so a strategy assigned an equity symbol still resolves even if
    the operator later flips the mode back to crypto. Crypto wins on an
    app_symbol collision (a ticker that is both a crypto base and an equity)."""
    reg: Dict[str, Instrument] = {}
    for base in crypto_symbols():
        inst = _crypto_instrument(base)
        reg.setdefault(inst.app_symbol, inst)
    for tkr in equity_symbols():
        inst = _equity_instrument(tkr)
        if inst.app_symbol in reg:
            logger.warning(
                "universe: equity '%s' shadows a crypto base of the same name; "
                "keeping crypto. Rename the equity override to disambiguate.",
                inst.app_symbol,
            )
            continue
        reg[inst.app_symbol] = inst
    # BTC must always resolve even if the crypto override excluded it.
    reg.setdefault("BTC", btc_instrument())
    return reg


def resolve(app_symbol: str) -> Optional[Instrument]:
    """Resolve an ``app_symbol`` to its :class:`Instrument`, or ``None`` if the
    symbol is unknown to the registry."""
    if not app_symbol:
        return None
    return _full_registry().get(app_symbol.strip().upper())


def _interleave_by_class(crypto: List[Instrument],
                         equity: List[Instrument]) -> List[Instrument]:
    """Fairly weave two class lists so neither clusters — equities stay spread
    across the cryptos (BTC still leads). Keeps each list's internal order."""
    if not crypto:
        return list(equity)
    if not equity:
        return list(crypto)
    out: List[Instrument] = []
    i = j = 0
    nc, ne = len(crypto), len(equity)
    for _ in range(nc + ne):
        # Emit from whichever class is proportionally most "behind" (smaller
        # progress fraction); ties favor crypto so BTC leads and the larger
        # class never starves.
        take_crypto = (i < nc) and (j >= ne or i * ne <= j * nc)
        if take_crypto:
            out.append(crypto[i]); i += 1
        else:
            out.append(equity[j]); j += 1
    return out


def active_universe() -> List[Instrument]:
    """The instruments eligible for selection under the current config. Always
    non-empty (falls back to ``[BTC]``). In ``mixed`` mode the two asset classes
    are interleaved so equities appear early in the rotation, not after every
    crypto has been exhausted."""
    if not multi_symbol_enabled():
        return [btc_instrument()]
    mode = universe_mode()

    def _dedup(insts: List[Instrument]) -> List[Instrument]:
        seen: set[str] = set()
        out: List[Instrument] = []
        for inst in insts:
            if inst.app_symbol in seen:
                continue
            seen.add(inst.app_symbol)
            out.append(inst)
        return out

    crypto = _dedup([_crypto_instrument(b) for b in crypto_symbols()])
    equity = _dedup([_equity_instrument(t) for t in equity_symbols()])
    # Drop any equity that shadows a crypto base (crypto already won in dedup of
    # the combined registry; keep selection consistent with resolve()).
    crypto_names = {i.app_symbol for i in crypto}
    equity = [i for i in equity if i.app_symbol not in crypto_names]

    if mode == "crypto":
        out = crypto
    elif mode == "equity":
        out = equity
    else:  # mixed
        out = _interleave_by_class(crypto, equity)
    return out or [btc_instrument()]


# --------------------------------------------------------------------------- #
# Per-strategy selection (thread-safe; auto-pipeline runs in worker threads)  #
# --------------------------------------------------------------------------- #

_PICK_LOCK = threading.Lock()
_pick_counter: int = 0


def pick_for_strategy() -> Instrument:
    """Choose the instrument for the next strategy. ``rotate`` (default) gives a
    deterministic round-robin over :func:`active_universe` for even coverage;
    ``random`` samples uniformly. Thread-safe."""
    universe = active_universe()
    if len(universe) == 1:
        return universe[0]
    if selection_mode() == "random":
        return random.choice(universe)
    global _pick_counter
    with _PICK_LOCK:
        idx = _pick_counter % len(universe)
        _pick_counter += 1
    return universe[idx]


def _reset_pick_counter() -> None:
    """Test hook — reset the round-robin cursor so rotation is reproducible."""
    global _pick_counter
    with _PICK_LOCK:
        _pick_counter = 0


# --------------------------------------------------------------------------- #
# Metadata helpers (used to stamp strategy rows / results)                    #
# --------------------------------------------------------------------------- #

def asset_class_of(app_symbol: str) -> str:
    inst = resolve(app_symbol)
    return inst.asset_class if inst is not None else "crypto"


def display_of(app_symbol: str) -> str:
    inst = resolve(app_symbol)
    if inst is not None:
        return inst.display
    return (app_symbol or "").strip().upper()


def price_csv_path(app_symbol: str) -> Path:
    """Absolute path to the per-asset price CSV for ``app_symbol``. Falls back to
    the crypto ``<BASE>-USDT.csv`` convention for symbols not in the registry."""
    inst = resolve(app_symbol)
    if inst is not None:
        return PRICES_DIR / inst.csv_name
    base = (app_symbol or "").strip().upper().replace("-USDT", "").replace("USDT", "").replace("-", "")
    return PRICES_DIR / f"{base}-USDT.csv"


def asset_context_note(app_symbol: str) -> Optional[str]:
    """A short directive prepended to the Alpha Story before the Coder runs,
    whenever the target instrument lacks BTC's full derivative columns. Steers
    the Coder away from funding/OI/liquidations factors that would be uniformly
    ``0`` (and thus rejected) on every non-BTC series. Returns ``None`` for BTC
    so the BTC Coder prompt is unchanged."""
    inst = resolve(app_symbol)
    if inst is None or inst.app_symbol == "BTC":
        return None
    if inst.asset_class == "equity":
        return (
            f"INSTRUMENT CONTEXT — the factor will be backtested on {inst.display}, "
            f"a US equity. In this dataset the columns open_interest, funding_rate "
            f"and liquidations are ALWAYS 0.0 for equities, so DO NOT build the "
            f"signal from them; use only price/volume (open, high, low, close, "
            f"volume). Bars are hourly during the US trading session; overnight and "
            f"weekend bars are flat-filled (zero volume, unchanged price), so prefer "
            f"signals that tolerate gaps (close-to-close returns, rolling windows) "
            f"over ones that assume continuous 24/7 trading."
        )
    return (
        f"INSTRUMENT CONTEXT — the factor will be backtested on {inst.display}. "
        f"In this dataset open_interest, funding_rate and liquidations are 0.0 for "
        f"every non-BTC pair, so DO NOT rely on them; build the signal from "
        f"price/volume (open, high, low, close, volume) only."
    )


# --------------------------------------------------------------------------- #
# Data availability + loading                                                 #
# --------------------------------------------------------------------------- #

def _file_present(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


def _file_is_fresh(path: Path, max_age_days: int) -> bool:
    """True when ``path`` exists and was written within ``max_age_days``.
    ``max_age_days <= 0`` means staleness is never a reason to re-fetch."""
    if max_age_days <= 0:
        return _file_present(path)
    try:
        age_sec = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age_sec <= max_age_days * 86400.0


def ensure_price_data(app_symbol: str) -> bool:
    """Guarantee a usable price CSV exists for ``app_symbol``, fetching on demand.

    * BTC — always available (bundled).
    * other crypto — present on disk from ``ingest_universe``; otherwise pulled
      via :func:`market_data.ensure_symbol`.
    * equity — fetched / refreshed from Yahoo via
      :func:`market_data.ensure_equity_symbol` (stale CSVs older than
      :func:`equity_data_max_age_days` are re-fetched).

    Returns ``True`` iff a non-empty CSV is present afterwards. Never raises.
    """
    inst = resolve(app_symbol) or btc_instrument()
    path = price_csv_path(inst.app_symbol)

    if inst.app_symbol == "BTC":
        return _file_present(BUNDLED_BTC_CSV) or _file_present(path)

    from backend.core import market_data as md

    if inst.asset_class == "equity":
        if _file_present(path) and _file_is_fresh(path, equity_data_max_age_days()):
            return True
        try:
            ok = md.ensure_equity_symbol(inst.app_symbol, out_path=path)
        except Exception:  # noqa: BLE001 — never let a fetch sink the pipeline
            logger.exception("universe: equity fetch failed for %s", inst.app_symbol)
            return _file_present(path)
        return bool(ok) or _file_present(path)

    # crypto altcoin
    if _file_present(path):
        return True
    try:
        ok = md.ensure_symbol(f"{inst.app_symbol}-USDT")
    except Exception:  # noqa: BLE001
        logger.exception("universe: crypto fetch failed for %s", inst.app_symbol)
        return _file_present(path)
    return bool(ok) or _file_present(path)


def load_market_df(app_symbol: str, *, auto_fetch: bool = True) -> pd.DataFrame:
    """Load the hourly price frame for ``app_symbol``, padded to the 8-column
    sandbox/engine contract and indexed by a tz-aware UTC timestamp.

    Mirrors the structure of ``WorkflowOrchestrator._load_market_df`` and
    ``factor_evaluator._load_price_window`` so the backtester sees an identical
    frame regardless of asset class. Resolution order:

        per-asset CSV  →  (auto-fetch on demand)  →  bundled BTC fallback.

    Raises ``FileNotFoundError`` only when nothing at all is available.
    """
    inst = resolve(app_symbol) or btc_instrument()
    path = price_csv_path(inst.app_symbol)

    if auto_fetch and not _file_present(path):
        ensure_price_data(inst.app_symbol)

    source: Optional[Path] = path if _file_present(path) else None
    if source is None and _file_present(BUNDLED_BTC_CSV):
        logger.warning(
            "universe: no data for %s; falling back to bundled BTC series",
            inst.app_symbol,
        )
        source = BUNDLED_BTC_CSV
    if source is None:
        raise FileNotFoundError(
            f"price data missing for {inst.app_symbol}: neither {path} nor "
            f"{BUNDLED_BTC_CSV} exists"
        )

    df = pd.read_csv(source)
    if "timestamp" not in df.columns:
        raise ValueError(f"price CSV {source} is missing the 'timestamp' column")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()
    # Keep the latest revision of any duplicated bar so downstream slicing stays
    # monotonic (mirrors the BTC path + factor_evaluator).
    df = df[~df.index.duplicated(keep="last")]
    if df.empty:
        raise ValueError(f"price CSV {source} produced an empty frame after parse")
    # Pad the derivative columns absent from 6-col OHLCV series (every non-BTC
    # instrument) so safe_execute_factor's REQUIRED_LOWERCASE_COLS check passes.
    for col in DERIVATIVE_COLS:
        if col not in df.columns:
            df[col] = 0.0
    return df


# --------------------------------------------------------------------------- #
# Introspection                                                               #
# --------------------------------------------------------------------------- #

def describe() -> Dict[str, object]:
    """A JSON-friendly snapshot of the active configuration — handy for an API
    endpoint or quick CLI verification."""
    universe = active_universe()
    return {
        "multi_symbol_enabled": multi_symbol_enabled(),
        "mode": universe_mode(),
        "selection": selection_mode(),
        "count": len(universe),
        "by_class": {
            "crypto": [i.app_symbol for i in universe if i.asset_class == "crypto"],
            "equity": [i.app_symbol for i in universe if i.asset_class == "equity"],
        },
        "equity_data_max_age_days": equity_data_max_age_days(),
        "instruments": [i.to_dict() for i in universe],
    }


__all__ = [
    "Instrument",
    "FEATURE_COLS",
    "DERIVATIVE_COLS",
    "DEFAULT_EQUITY_SYMBOLS",
    "multi_symbol_enabled",
    "universe_mode",
    "selection_mode",
    "crypto_symbols",
    "equity_symbols",
    "active_universe",
    "resolve",
    "btc_instrument",
    "pick_for_strategy",
    "asset_class_of",
    "display_of",
    "price_csv_path",
    "asset_context_note",
    "ensure_price_data",
    "load_market_df",
    "describe",
]


if __name__ == "__main__":  # pragma: no cover — manual verification helper
    import json as _json

    print(_json.dumps(describe(), indent=2, ensure_ascii=False))

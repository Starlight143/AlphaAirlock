"""Real market-data ingestion from Binance public data (``data.binance.vision``).

Free, no API key, no geo-restriction (a public data CDN — not the geo-fenced
``api.binance.com`` / ``fapi.binance.com`` REST hosts). Writes the **exact** CSV
schema the existing backtest / cointegration / factor pipeline already consumes,
so every downstream consumer runs on real data with **zero** code changes:

  * BTC primary  -> ``backend/data/synthetic_btc.csv``   (9 cols, incl. funding/OI)
  * BTC primary  -> ``storage/prices/BTC-USDT.csv``       (9 cols; factor studio
                    prefers the per-asset file for ``asset_symbol="BTC"``)
  * Universe     -> ``storage/prices/<SYM>-USDT.csv``     (6 cols OHLCV)

Why the BTC file carries 9 columns: ``backend/core/sandbox.py`` requires the
lowercase set ``{open, high, low, close, volume, open_interest, funding_rate,
liquidations}`` before executing any factor; a 6-column frame raises
``SandboxValidationError``. OHLCV-only is therefore safe **only** for the
non-BTC universe (cointegration + per-asset IC use price/volume).

Derivative coverage from the free Binance public source:
  * OHLCV         : full history (monthly + daily kline dumps), hourly bars.
  * funding_rate  : full history, native 8h cadence forward-filled to hourly.
  * open_interest : from daily ``metrics`` dumps (BTC primary only — the dataset
                    is daily-file-only, so fetching it for all 30 symbols would
                    be ~27k requests; the funding/OI factors run on BTC anyway).
  * liquidations  : **NOT available** from the free source. Written as ``0.0``
                    and flagged in the coverage report. A faithful liquidation
                    series requires a paid feed (Coinglass / Amberdata / Kaiko);
                    that adapter slots in via :func:`ingest_universe` later.

Entry points:
  * Programmatic : :func:`ingest_universe`.
  * CLI          : ``python -m backend.core.market_data --symbols BTC-USDT,ETH-USDT``
  * HTTP         : ``POST /api/market-data/ingest`` (see ``backend/app/main.py``).

The module is intentionally additive: it never imports from the consumers and
never mutates the database. It only writes CSV files (atomically) + one JSON
coverage sidecar at ``storage/market_data_meta.json``.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

try:  # httpx ships in requirements.txt; guard so import never hard-fails.
    import httpx
except Exception:  # pragma: no cover - exercised only in stripped envs
    httpx = None  # type: ignore[assignment]

# Centralized .env load + typed env helpers (mirrors the rest of backend/core).
from backend._envloader import env_bool, env_int, env_str

logger = logging.getLogger("alpha.market_data")

# --------------------------------------------------------------------------- #
# Paths + universe                                                            #
# --------------------------------------------------------------------------- #

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
PRICES_DIR: Path = PROJECT_ROOT / "storage" / "prices"
BTC_PRIMARY_CSV: Path = PROJECT_ROOT / "backend" / "data" / "synthetic_btc.csv"
META_SIDECAR: Path = PROJECT_ROOT / "storage" / "market_data_meta.json"

DEFAULT_BASE_URL = "https://data.binance.vision"
INTERVAL = "1h"
DEFAULT_START = "2024-01-01"

# Default 30-symbol universe — mirrors ``data_gen.DEFAULT_MULTIASSET_SYMBOLS``
# (BTC first) so a real ingest replaces exactly the synthetic universe on disk.
DEFAULT_UNIVERSE: Tuple[str, ...] = (
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT", "ADA-USDT",
    "DOGE-USDT", "TRX-USDT", "AVAX-USDT", "LINK-USDT", "DOT-USDT", "MATIC-USDT",
    "LTC-USDT", "BCH-USDT", "NEAR-USDT", "ATOM-USDT", "ETC-USDT", "FIL-USDT",
    "APT-USDT", "ARB-USDT", "OP-USDT", "SUI-USDT", "INJ-USDT", "TIA-USDT",
    "SEI-USDT", "RNDR-USDT", "IMX-USDT", "STX-USDT", "GRT-USDT", "AAVE-USDT",
)

# Binance ticker rename chains: app base -> ordered binance tickers to stitch.
# MATIC was migrated to POL (Sep 2024); RNDR was migrated to RENDER (2024).
# Fetching both and concatenating reconstructs the full continuous history.
_TICKER_ALIASES: Dict[str, Tuple[str, ...]] = {
    "MATIC": ("MATICUSDT", "POLUSDT"),
    "RNDR": ("RNDRUSDT", "RENDERUSDT"),
}

OUT_COLS_FULL: Tuple[str, ...] = (
    "open", "high", "low", "close", "volume",
    "open_interest", "funding_rate", "liquidations",
)
OUT_COLS_OHLCV: Tuple[str, ...] = ("open", "high", "low", "close", "volume")

# Binance Vision CSV column layouts (older dumps ship without a header row).
_KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
]
_FUNDING_COLS = ["calc_time", "funding_interval_hours", "last_funding_rate"]
_METRICS_COLS = [
    "create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
    "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
    "count_long_short_ratio", "sum_taker_long_short_vol_ratio",
]

_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


# --------------------------------------------------------------------------- #
# Config (env-overridable)                                                    #
# --------------------------------------------------------------------------- #

def base_url() -> str:
    return env_str("MARKET_DATA_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def max_workers() -> int:
    return env_int("MARKET_DATA_MAX_WORKERS", 8, minimum=1, maximum=32)


def default_start() -> str:
    return (env_str("MARKET_DATA_START", DEFAULT_START) or DEFAULT_START).strip()


def configured_universe() -> List[str]:
    raw = env_str("MARKET_DATA_SYMBOLS", "")
    if raw.strip():
        return [s.strip().upper() for s in raw.split(",") if s.strip()]
    return list(DEFAULT_UNIVERSE)


# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def _as_utc_ns(s: pd.Series) -> pd.Series:
    """Normalize a tz-aware datetime Series to nanosecond UTC resolution.

    pandas 3.0 preserves the *parsed* datetime unit (epoch-ms -> ``ms``,
    string timestamps -> ``us``) and ``merge_asof`` rejects keys whose units
    differ. Coercing every join key to ``ns`` up front keeps the OHLCV /
    funding / open-interest merges resolution-safe.
    """
    return s.dt.as_unit("ns")


def _symbol_base(app_symbol: str) -> str:
    """``"BTC-USDT"`` / ``"btcusdt"`` -> ``"BTC"``."""
    s = (app_symbol or "").upper().strip()
    for suffix in ("-USDT", "USDT"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s.replace("-", "")


def _binance_tickers(app_symbol: str) -> List[str]:
    base = _symbol_base(app_symbol)
    if base in _TICKER_ALIASES:
        return list(_TICKER_ALIASES[base])
    return [f"{base}USDT"]


def _add_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def _cutoff_date(end: Optional[str]) -> date:
    """Last fully-closed UTC day we can reliably fetch (Binance publishes the
    daily dump only after the UTC day rolls over)."""
    if end:
        return _parse_date(end)
    return datetime.now(timezone.utc).date() - timedelta(days=1)


# --------------------------------------------------------------------------- #
# HTTP + ZIP                                                                  #
# --------------------------------------------------------------------------- #

def _make_client() -> "httpx.Client":
    if httpx is None:  # pragma: no cover
        raise RuntimeError(
            "httpx is required for market-data ingestion (declared in requirements.txt)"
        )
    return httpx.Client(
        timeout=httpx.Timeout(30.0, connect=15.0),
        follow_redirects=True,
        headers={"User-Agent": "alpha-market-data/1.0 (+real-data ingest)"},
    )


def _get_bytes(client: "httpx.Client", url: str, *, retries: int = 4,
               backoff: float = 0.6) -> Optional[bytes]:
    """GET raw bytes. Returns ``None`` for an expected 404 (symbol not yet
    listed that month) or after exhausting retries on transient failures."""
    last = ""
    for attempt in range(retries):
        try:
            resp = client.get(url)
            if resp.status_code == 404:
                return None
            if resp.status_code in _RETRYABLE_STATUS:
                last = f"HTTP {resp.status_code}"
                time.sleep(backoff * (2 ** attempt))
                continue
            resp.raise_for_status()
            return resp.content
        except Exception as exc:  # noqa: BLE001 — network/timeout: retry
            last = repr(exc)
            time.sleep(backoff * (2 ** attempt))
    logger.warning("market_data: GET failed after %d tries: %s (%s)", retries, url, last)
    return None


def _looks_like_header(first_cell: str, header_first: str) -> bool:
    return first_cell.strip().strip('"').lower() == header_first.lower()


def _read_zip_csv(content: bytes, names: List[str], header_first: str) -> Optional[pd.DataFrame]:
    """Unzip the first ``.csv`` member and parse it, tolerating the
    header-present (newer) and header-absent (older) Binance dump variants."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        logger.warning("market_data: corrupt zip (%d bytes) — skipping", len(content))
        return None
    members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    if not members:
        return None
    raw = zf.read(members[0])
    if not raw:
        return None
    first_line = raw.split(b"\n", 1)[0].decode("utf-8", "replace")
    first_cell = first_line.split(",", 1)[0]
    try:
        if _looks_like_header(first_cell, header_first):
            return pd.read_csv(io.BytesIO(raw))
        return pd.read_csv(io.BytesIO(raw), header=None, names=names)
    except Exception:  # noqa: BLE001
        logger.exception("market_data: CSV parse failed (header_first=%s)", header_first)
        return None


def _download_parse_many(urls: Sequence[str], names: List[str],
                         header_first: str) -> List[pd.DataFrame]:
    """Download + parse a batch of dump URLs concurrently. Missing files
    (404) are silently skipped; the caller reasons about coverage afterwards."""
    if not urls:
        return []
    out: List[pd.DataFrame] = []
    with _make_client() as client:
        with ThreadPoolExecutor(max_workers=max_workers()) as pool:
            futs = {pool.submit(_get_bytes, client, u): u for u in urls}
            for fut in as_completed(futs):
                content = fut.result()
                if content is None:
                    continue
                df = _read_zip_csv(content, names, header_first)
                if df is not None and not df.empty:
                    out.append(df)
    return out


# --------------------------------------------------------------------------- #
# URL builders                                                                #
# --------------------------------------------------------------------------- #

def _kline_urls(ticker: str, start_d: date, cutoff_d: date) -> List[str]:
    base = base_url()
    urls: List[str] = []
    cutoff_month = date(cutoff_d.year, cutoff_d.month, 1)
    m = date(start_d.year, start_d.month, 1)
    # Complete past months -> efficient monthly dumps.
    while m < cutoff_month:
        urls.append(
            f"{base}/data/futures/um/monthly/klines/{ticker}/{INTERVAL}/"
            f"{ticker}-{INTERVAL}-{m.year:04d}-{m.month:02d}.zip"
        )
        m = _add_month(m)
    # Current (incomplete) month -> per-day dumps up to the cutoff.
    day = max(start_d, cutoff_month)
    while day <= cutoff_d:
        urls.append(
            f"{base}/data/futures/um/daily/klines/{ticker}/{INTERVAL}/"
            f"{ticker}-{INTERVAL}-{day.isoformat()}.zip"
        )
        day += timedelta(days=1)
    return urls


def _funding_urls(ticker: str, start_d: date, cutoff_d: date) -> List[str]:
    base = base_url()
    urls: List[str] = []
    cutoff_month = date(cutoff_d.year, cutoff_d.month, 1)
    m = date(start_d.year, start_d.month, 1)
    # Funding dumps are monthly-only on Binance Vision. Try every month in the
    # span incl. the cutoff month (published incrementally near month end); a
    # missing tail simply forward-fills the last known rate.
    while m <= cutoff_month:
        urls.append(
            f"{base}/data/futures/um/monthly/fundingRate/{ticker}/"
            f"{ticker}-fundingRate-{m.year:04d}-{m.month:02d}.zip"
        )
        m = _add_month(m)
    return urls


def _metrics_urls(ticker: str, start_d: date, cutoff_d: date) -> List[str]:
    base = base_url()
    urls: List[str] = []
    day = start_d
    while day <= cutoff_d:  # metrics (open interest) ships daily-only.
        urls.append(
            f"{base}/data/futures/um/daily/metrics/{ticker}/"
            f"{ticker}-metrics-{day.isoformat()}.zip"
        )
        day += timedelta(days=1)
    return urls


# --------------------------------------------------------------------------- #
# Fetch -> tidy frames (tz-aware ``ts`` column throughout for merge_asof)      #
# --------------------------------------------------------------------------- #

def fetch_klines(app_symbol: str, start_d: date, cutoff_d: date) -> pd.DataFrame:
    """Real hourly OHLCV. Index = tz-aware UTC ``ts``; columns
    ``[open, high, low, close, volume]`` as float. Empty frame if nothing
    could be fetched (e.g. symbol never listed)."""
    frames: List[pd.DataFrame] = []
    for ticker in _binance_tickers(app_symbol):
        parts = _download_parse_many(_kline_urls(ticker, start_d, cutoff_d),
                                     _KLINE_COLS, "open_time")
        if parts:
            frames.append(pd.concat(parts, ignore_index=True))
    if not frames:
        return pd.DataFrame(columns=list(OUT_COLS_OHLCV))
    raw = pd.concat(frames, ignore_index=True)
    ts = pd.to_datetime(pd.to_numeric(raw["open_time"], errors="coerce"),
                        unit="ms", utc=True)
    df = pd.DataFrame({
        "ts": _as_utc_ns(ts),
        "open": pd.to_numeric(raw["open"], errors="coerce"),
        "high": pd.to_numeric(raw["high"], errors="coerce"),
        "low": pd.to_numeric(raw["low"], errors="coerce"),
        "close": pd.to_numeric(raw["close"], errors="coerce"),
        "volume": pd.to_numeric(raw["volume"], errors="coerce"),
    }).dropna(subset=["ts", "close"])
    df = (df.drop_duplicates(subset=["ts"], keep="last")
            .set_index("ts").sort_index())
    start_ts = pd.Timestamp(start_d, tz="UTC")
    end_ts = pd.Timestamp(cutoff_d, tz="UTC") + pd.Timedelta(days=1)
    return df[(df.index >= start_ts) & (df.index < end_ts)]


def fetch_funding(app_symbol: str, start_d: date, cutoff_d: date) -> pd.DataFrame:
    """Real funding rate (native 8h cadence). Returns a frame with a ``ts``
    column + ``funding_rate``; empty frame if unavailable."""
    frames: List[pd.DataFrame] = []
    for ticker in _binance_tickers(app_symbol):
        parts = _download_parse_many(_funding_urls(ticker, start_d, cutoff_d),
                                     _FUNDING_COLS, "calc_time")
        if parts:
            frames.append(pd.concat(parts, ignore_index=True))
    if not frames:
        return pd.DataFrame(columns=["ts", "funding_rate"])
    raw = pd.concat(frames, ignore_index=True)
    ts = pd.to_datetime(pd.to_numeric(raw["calc_time"], errors="coerce"),
                        unit="ms", utc=True)
    out = pd.DataFrame({
        "ts": _as_utc_ns(ts),
        "funding_rate": pd.to_numeric(raw["last_funding_rate"], errors="coerce"),
    }).dropna()
    return out.drop_duplicates(subset=["ts"], keep="last").sort_values("ts")


def fetch_open_interest(app_symbol: str, start_d: date, cutoff_d: date) -> pd.DataFrame:
    """Real open interest from the daily ``metrics`` dumps, resampled to the
    hourly bar (last observation in each hour). Frame with ``ts`` +
    ``open_interest``; empty if unavailable (e.g. predates the metrics dataset)."""
    frames: List[pd.DataFrame] = []
    for ticker in _binance_tickers(app_symbol):
        parts = _download_parse_many(_metrics_urls(ticker, start_d, cutoff_d),
                                     _METRICS_COLS, "create_time")
        if parts:
            frames.append(pd.concat(parts, ignore_index=True))
    if not frames:
        return pd.DataFrame(columns=["ts", "open_interest"])
    raw = pd.concat(frames, ignore_index=True)
    ts = _as_utc_ns(pd.to_datetime(raw["create_time"], utc=True, errors="coerce"))
    oi = pd.to_numeric(raw["sum_open_interest"], errors="coerce")
    tidy = (pd.DataFrame({"ts": ts, "open_interest": oi}).dropna()
              .drop_duplicates(subset=["ts"], keep="last")
              .set_index("ts").sort_index())
    if tidy.empty:
        return pd.DataFrame(columns=["ts", "open_interest"])
    hourly = tidy["open_interest"].resample("1h").last().dropna()
    return hourly.rename("open_interest").reset_index().rename(columns={"index": "ts"})


# --------------------------------------------------------------------------- #
# Provider routing (Binance public default; Yahoo Finance alternative)        #
# --------------------------------------------------------------------------- #

_YAHOO_PROVIDERS = frozenset({"yahoo", "yfinance", "yahoo_finance"})


def provider_default() -> str:
    return (env_str("MARKET_DATA_PROVIDER", "binance_public") or "binance_public").strip().lower()


def _is_binance_provider(provider: Optional[str]) -> bool:
    return (provider or provider_default()).strip().lower() not in _YAHOO_PROVIDERS


def _yahoo_ticker(app_symbol: str) -> str:
    """Map an app ticker to its Yahoo Finance symbol.

    Crypto USDT pairs become ``<BASE>-USD`` (Yahoo quotes crypto vs USD, not
    USDT). Anything that is not a ``*USDT`` pair is passed through unchanged so
    callers can fetch equities / indices / FX / commodities directly
    (e.g. ``AAPL``, ``^GSPC``, ``GC=F``, ``EURUSD=X``).
    """
    s = (app_symbol or "").upper().strip()
    if s.endswith("-USDT") or s.endswith("USDT"):
        return f"{_symbol_base(s)}-USD"
    return s


def fetch_klines_yahoo(app_symbol: str, start_d: date, cutoff_d: date,
                       interval: str = "1h") -> pd.DataFrame:
    """Real OHLCV from Yahoo Finance via yfinance. Index = tz-aware UTC ``ts``;
    columns ``[open, high, low, close, volume]``.

    Note: Yahoo only serves intraday (``1h``) history for ~the last 730 days —
    older ranges silently return fewer rows. For long histories use ``1d`` or
    the Binance provider. Empty frame on any failure (import/network/no data).
    """
    try:
        import yfinance as yf
    except Exception:  # noqa: BLE001
        logger.warning("market_data: yfinance not installed — cannot use Yahoo provider")
        return pd.DataFrame(columns=list(OUT_COLS_OHLCV))
    ticker = _yahoo_ticker(app_symbol)
    try:
        raw = yf.download(
            ticker,
            start=start_d.isoformat(),
            end=(cutoff_d + timedelta(days=1)).isoformat(),
            interval=interval,
            auto_adjust=False,
            actions=False,
            progress=False,
            threads=False,
        )
    except Exception:  # noqa: BLE001
        logger.exception("market_data: yfinance download failed for %s (%s)", app_symbol, ticker)
        return pd.DataFrame(columns=list(OUT_COLS_OHLCV))
    if raw is None or raw.empty:
        return pd.DataFrame(columns=list(OUT_COLS_OHLCV))
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):  # single-ticker download still nests
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).strip().lower() for c in df.columns]
    needed = ["open", "high", "low", "close", "volume"]
    if any(c not in df.columns for c in needed):
        logger.warning("market_data: Yahoo frame missing OHLCV columns for %s: %s",
                       app_symbol, list(df.columns))
        return pd.DataFrame(columns=list(OUT_COLS_OHLCV))
    out = df[needed].apply(pd.to_numeric, errors="coerce")
    idx = pd.to_datetime(df.index, utc=True, errors="coerce")  # tz-aware UTC
    out.index = idx
    out = out[out.index.notna()]
    out = out.dropna(subset=["close"])
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out.index = out.index.as_unit("ns")  # match Binance keys' resolution
    out.index.name = "ts"
    start_ts = pd.Timestamp(start_d, tz="UTC")
    end_ts = pd.Timestamp(cutoff_d, tz="UTC") + pd.Timedelta(days=1)
    return out[(out.index >= start_ts) & (out.index < end_ts)]


def _fetch_klines_provider(app_symbol: str, start_d: date, cutoff_d: date,
                           provider: Optional[str]) -> pd.DataFrame:
    """Route OHLCV fetch to the configured provider."""
    if not _is_binance_provider(provider):
        return fetch_klines_yahoo(app_symbol, start_d, cutoff_d)
    return fetch_klines(app_symbol, start_d, cutoff_d)


# --------------------------------------------------------------------------- #
# Assemble -> app schema                                                       #
# --------------------------------------------------------------------------- #

def _hourly_grid_ohlcv(klines: pd.DataFrame) -> Tuple[pd.DataFrame, int, int]:
    """Reindex OHLCV onto a gap-free hourly grid. Filled (exchange-downtime)
    bars carry the prior close as O/H/L/C and zero volume. Returns
    ``(frame_indexed_by_ts, real_bar_count, filled_bar_count)``."""
    grid = pd.date_range(
        klines.index.min().floor("h"), klines.index.max().floor("h"),
        freq="1h", tz="UTC",
    ).as_unit("ns")
    reixed = klines.reindex(grid)
    filled = int(reixed["close"].isna().sum())
    real = int(len(grid) - filled)
    for col in ("open", "high", "low", "close"):
        reixed[col] = reixed[col].ffill()
    reixed["volume"] = reixed["volume"].fillna(0.0)
    reixed = reixed.dropna(subset=["close"])  # drop any leading gap before first bar
    reixed.index.name = "ts"
    return reixed, real, filled


def build_full(app_symbol: str, start_d: date, cutoff_d: date, *,
               with_oi: bool = True, with_funding: bool = True,
               provider: Optional[str] = None) -> Tuple[pd.DataFrame, Dict]:
    """Assemble the full 9-column dataset (OHLCV + funding + OI + liquidations)
    for ``app_symbol``. funding/OI are Binance-only (Yahoo has neither) and are
    written as 0.0 when the provider or the source can't supply them."""
    klines = _fetch_klines_provider(app_symbol, start_d, cutoff_d, provider)
    if klines.empty:
        return pd.DataFrame(columns=list(OUT_COLS_FULL)), {"status": "no_data"}
    df, real, filled = _hourly_grid_ohlcv(klines)
    grid = df.reset_index()[["ts"]]
    on_binance = _is_binance_provider(provider)

    # funding_rate — backward-asof onto each hourly bar, then ffill the tail.
    funding_present = False
    if with_funding and on_binance:
        fund = fetch_funding(app_symbol, start_d, cutoff_d)
        if not fund.empty:
            merged = pd.merge_asof(grid, fund.sort_values("ts"), on="ts",
                                   direction="backward")
            df["funding_rate"] = merged["funding_rate"].ffill().fillna(0.0).to_numpy()
            funding_present = True
    if not funding_present:
        df["funding_rate"] = 0.0

    # open_interest — backward-asof, ffill then bfill leading gap.
    oi_present = False
    if with_oi and on_binance:
        oi = fetch_open_interest(app_symbol, start_d, cutoff_d)
        if not oi.empty:
            merged = pd.merge_asof(grid, oi.sort_values("ts"), on="ts",
                                   direction="backward")
            df["open_interest"] = (
                merged["open_interest"].ffill().bfill().fillna(0.0).to_numpy()
            )
            oi_present = True
    if not oi_present:
        df["open_interest"] = 0.0

    # liquidations — unavailable from the free Binance source.
    df["liquidations"] = 0.0

    df = df[list(OUT_COLS_FULL)]
    cov = {
        "status": "ok",
        "schema": "full9",
        "provider": (provider or provider_default()),
        "rows": int(len(df)),
        "real_bars": real,
        "filled_bars": filled,
        "first": df.index.min().strftime("%Y-%m-%d %H:%M:%S%z"),
        "last": df.index.max().strftime("%Y-%m-%d %H:%M:%S%z"),
        "funding_rate": "real" if funding_present else "missing",
        "open_interest": "real" if oi_present else "missing",
        "liquidations": "zero_unavailable_free_source",
    }
    return df, cov


def build_btc_primary(start_d: date, cutoff_d: date, *, with_oi: bool = True,
                      with_funding: bool = True,
                      provider: Optional[str] = None) -> Tuple[pd.DataFrame, Dict]:
    """Assemble the full 9-column BTC dataset (thin wrapper over build_full)."""
    return build_full("BTC-USDT", start_d, cutoff_d, with_oi=with_oi,
                      with_funding=with_funding, provider=provider)


def build_ohlcv(app_symbol: str, start_d: date, cutoff_d: date, *,
                provider: Optional[str] = None) -> Tuple[pd.DataFrame, Dict]:
    """Assemble a 6-column OHLCV dataset for a non-BTC universe symbol."""
    klines = _fetch_klines_provider(app_symbol, start_d, cutoff_d, provider)
    if klines.empty:
        return pd.DataFrame(columns=list(OUT_COLS_OHLCV)), {"status": "no_data"}
    df, real, filled = _hourly_grid_ohlcv(klines)
    df = df[list(OUT_COLS_OHLCV)]
    cov = {
        "status": "ok",
        "schema": "ohlcv6",
        "rows": int(len(df)),
        "real_bars": real,
        "filled_bars": filled,
        "first": df.index.min().strftime("%Y-%m-%d %H:%M:%S%z"),
        "last": df.index.max().strftime("%Y-%m-%d %H:%M:%S%z"),
    }
    return df, cov


# --------------------------------------------------------------------------- #
# Atomic CSV write (exact existing format: tz-aware "timestamp" first column)  #
# --------------------------------------------------------------------------- #

def _write_csv(df_indexed_by_ts: pd.DataFrame, path: Path,
               value_cols: Sequence[str]) -> None:
    out = df_indexed_by_ts.copy()
    out.index.name = "timestamp"
    out = out.reset_index()[["timestamp", *value_cols]]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.stem + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        out.to_csv(tmp, index=False)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _write_meta(report: Dict) -> None:
    try:
        META_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
        META_SIDECAR.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.exception("market_data: failed to write meta sidecar")


def read_meta() -> Optional[Dict]:
    """Return the last ingest coverage report, or ``None`` if never run."""
    try:
        if META_SIDECAR.exists():
            return json.loads(META_SIDECAR.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("market_data: failed to read meta sidecar")
    return None


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #

def ingest_universe(symbols: Optional[Sequence[str]] = None, *,
                    start: Optional[str] = None, end: Optional[str] = None,
                    with_oi: bool = True, with_funding: bool = True,
                    provider: Optional[str] = None) -> Dict:
    """Fetch real data for ``symbols`` and overwrite the on-disk price CSVs in
    the schema each consumer expects. Returns (and persists to
    ``storage/market_data_meta.json``) a per-symbol coverage report.

    Robust by design: a failure on one symbol is captured in the report and
    never aborts the rest of the universe.
    """
    syms = [s.strip().upper() for s in (symbols or configured_universe()) if s.strip()]
    start_d = _parse_date(start or default_start())
    cutoff_d = _cutoff_date(end)
    if cutoff_d < start_d:
        raise ValueError(f"end ({cutoff_d}) precedes start ({start_d})")

    report: Dict = {
        "started_at": _utcnow_iso(),
        "source": (provider or provider_default()),
        "base_url": base_url(),
        "interval": INTERVAL,
        "start": start_d.isoformat(),
        "end": cutoff_d.isoformat(),
        "liquidations": "unavailable_free_source (written as 0.0)",
        "symbols": {},
    }
    logger.info("market_data: ingest %d symbols %s..%s", len(syms), start_d, cutoff_d)

    for sym in syms:
        base = _symbol_base(sym)
        try:
            if base == "BTC":
                df, cov = build_btc_primary(
                    start_d, cutoff_d, with_oi=with_oi, with_funding=with_funding,
                    provider=provider)
                if cov.get("status") != "ok" or df.empty:
                    report["symbols"][sym] = {"status": "no_data"}
                    continue
                # BTC primary is consumed via BOTH the bundled CSV and the
                # per-asset path; write the full 9-col frame to each.
                _write_csv(df, BTC_PRIMARY_CSV, OUT_COLS_FULL)
                _write_csv(df, PRICES_DIR / "BTC-USDT.csv", OUT_COLS_FULL)
                report["symbols"][sym] = cov
            else:
                df, cov = build_ohlcv(sym, start_d, cutoff_d, provider=provider)
                if cov.get("status") != "ok" or df.empty:
                    report["symbols"][sym] = {"status": "no_data"}
                    continue
                _write_csv(df, PRICES_DIR / f"{base}-USDT.csv", OUT_COLS_OHLCV)
                report["symbols"][sym] = cov
            logger.info("market_data: %-10s %s", sym, report["symbols"][sym])
        except Exception as exc:  # noqa: BLE001 — isolate per-symbol failures
            logger.exception("market_data: ingest failed for %s", sym)
            report["symbols"][sym] = {"status": "error",
                                      "error": f"{type(exc).__name__}: {exc}"}

    report["finished_at"] = _utcnow_iso()
    report["ok_count"] = sum(
        1 for v in report["symbols"].values() if v.get("status") == "ok")
    report["total"] = len(syms)
    _write_meta(report)
    logger.info("market_data: ingest done — %d/%d ok",
                report["ok_count"], report["total"])
    return report


# --------------------------------------------------------------------------- #
# On-demand single-symbol fetch + incremental daily refresh                    #
# --------------------------------------------------------------------------- #

def _read_csv_indexed(path: Path) -> Optional[pd.DataFrame]:
    """Load an existing price CSV into a ts-indexed (ns, UTC) frame, or None."""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        old = pd.read_csv(path)
        if "timestamp" not in old.columns:
            return None
        ts = _as_utc_ns(pd.to_datetime(old["timestamp"], utc=True, errors="coerce"))
        old = old.drop(columns=["timestamp"])
        old.index = pd.DatetimeIndex(ts, name="ts")
        old = old[~old.index.isna()]
        return old[~old.index.duplicated(keep="last")].sort_index()
    except Exception:  # noqa: BLE001
        logger.exception("market_data: failed to read existing CSV %s", path)
        return None


def _last_ts_of_csv(path: Path) -> Optional[pd.Timestamp]:
    df = _read_csv_indexed(path)
    if df is None or df.empty:
        return None
    return df.index.max()


def _csv_is_full9(path: Path) -> bool:
    df = _read_csv_indexed(path)
    return bool(df is not None and all(c in df.columns for c in OUT_COLS_FULL))


def _merge_and_write(path: Path, new_df: pd.DataFrame, value_cols: Sequence[str]) -> int:
    """Merge a freshly-fetched (ts-indexed) frame into the existing CSV — new
    bars win on overlap — and rewrite atomically. Returns total row count."""
    cols = list(value_cols)
    new_part = new_df[[c for c in cols if c in new_df.columns]].copy()
    existing = _read_csv_indexed(path)
    if existing is not None and not existing.empty:
        for c in cols:
            if c not in existing.columns:
                existing[c] = 0.0
        combined = new_part.combine_first(existing[cols])  # new wins on overlap
    else:
        combined = new_part
    for c in cols:
        if c not in combined.columns:
            combined[c] = 0.0
    combined = combined[cols]
    combined = combined[~combined.index.duplicated(keep="first")].sort_index()
    combined.index.name = "ts"
    _write_csv(combined, path, value_cols)
    return int(len(combined))


def ensure_symbol(app_symbol: str, *, provider: Optional[str] = None,
                  force: bool = False, start: Optional[str] = None,
                  end: Optional[str] = None) -> bool:
    """Guarantee a per-asset price CSV exists for ``app_symbol``, downloading it
    on demand when missing. The factor studio calls this so a strategy that
    needs a new trading pair gets its real data fetched automatically.

    Non-BTC symbols are written as 6-col OHLCV (factor_evaluator pads the
    sandbox's derivative columns to 0); BTC is the 9-col primary. Returns True
    if the CSV is present afterwards.
    """
    base = _symbol_base(app_symbol)
    if not base:
        return False
    path = PRICES_DIR / f"{base}-USDT.csv"
    if path.exists() and path.stat().st_size > 0 and not force:
        return True
    start_d = _parse_date(start or default_start())
    cutoff_d = _cutoff_date(end)
    try:
        if base == "BTC":
            df, cov = build_full("BTC-USDT", start_d, cutoff_d,
                                 with_oi=True, with_funding=True, provider=provider)
            if cov.get("status") == "ok" and not df.empty:
                _write_csv(df, BTC_PRIMARY_CSV, OUT_COLS_FULL)
                _write_csv(df, path, OUT_COLS_FULL)
                return True
            return path.exists()
        df, cov = build_ohlcv(f"{base}-USDT", start_d, cutoff_d, provider=provider)
        if cov.get("status") == "ok" and not df.empty:
            _write_csv(df, path, OUT_COLS_OHLCV)
            logger.info("market_data: on-demand fetch %s -> %d rows (%s)",
                        f"{base}-USDT", len(df), cov.get("provider"))
            return True
        return path.exists()
    except Exception:  # noqa: BLE001
        logger.exception("market_data: ensure_symbol(%s) failed", app_symbol)
        return path.exists()


def ensure_equity_symbol(ticker: str, *, out_path: Path,
                         start: Optional[str] = None, end: Optional[str] = None,
                         force: bool = False) -> bool:
    """Guarantee a per-equity hourly OHLCV CSV at ``out_path``, fetched from
    Yahoo Finance (yfinance) on demand.

    Equities are NOT ``*USDT`` pairs, so — unlike :func:`ensure_symbol`, which
    hard-codes the crypto ``<base>-USDT.csv`` convention — they are written under
    their bare ticker (the caller decides ``out_path``, e.g.
    ``storage/prices/AAPL.csv``). Two equity-specific quirks are handled here:

    * **Bar alignment.** Yahoo's 1h equity bars are stamped at ``:30`` (the US
      session opens 09:30 ET), so they would miss the ``:00`` continuous hourly
      grid that :func:`_hourly_grid_ohlcv` (and the crypto data) use, yielding an
      empty frame. We floor the index to the hour before assembly so every bar
      lands on ``:00``.
    * **History horizon.** Yahoo only serves 1h intraday for ``~730`` days, so
      the requested ``start`` is clamped into that window — an over-wide range
      makes yfinance return an empty/erroring frame.

    Returns ``True`` iff a non-empty CSV is present at ``out_path`` afterwards.
    Never raises — a fetch failure leaves any existing file untouched.
    """
    tkr = (ticker or "").strip().upper()
    if not tkr:
        return False
    if out_path.exists() and out_path.stat().st_size > 0 and not force:
        return True

    cutoff_d = _cutoff_date(end)
    start_d = _parse_date(start or default_start())
    # Clamp into Yahoo's hard 730-day 1h horizon. fetch_klines_yahoo queries
    # ``[start, cutoff + 1day)``, so the effective span is ``cutoff + 1 - start``;
    # a 720-day floor keeps that comfortably under 730 even across tz/second
    # boundaries (a too-wide range makes Yahoo reject the whole request).
    min_start = cutoff_d - timedelta(days=720)
    if start_d < min_start:
        start_d = min_start
    if cutoff_d < start_d:
        logger.warning("market_data: equity %s window empty after clamp (%s..%s)",
                       tkr, start_d, cutoff_d)
        return out_path.exists() and out_path.stat().st_size > 0

    try:
        klines = fetch_klines_yahoo(tkr, start_d, cutoff_d, interval="1h")
    except Exception:  # noqa: BLE001
        logger.exception("market_data: Yahoo 1h fetch failed for equity %s", tkr)
        return out_path.exists() and out_path.stat().st_size > 0
    if klines is None or klines.empty:
        logger.warning("market_data: no Yahoo 1h data for equity %s (%s..%s)",
                       tkr, start_d, cutoff_d)
        return out_path.exists() and out_path.stat().st_size > 0

    # Floor the :30 session bars to the hour so they align to the :00 grid, then
    # drop any collision (keep latest) before assembly.
    klines = klines.copy()
    klines.index = klines.index.floor("h")
    klines = klines[~klines.index.duplicated(keep="last")].sort_index()

    try:
        df, real, filled = _hourly_grid_ohlcv(klines)
    except Exception:  # noqa: BLE001
        logger.exception("market_data: hourly-grid assembly failed for equity %s", tkr)
        return out_path.exists() and out_path.stat().st_size > 0
    if df.empty:
        logger.warning("market_data: equity %s produced an empty hourly grid", tkr)
        return out_path.exists() and out_path.stat().st_size > 0

    _write_csv(df, out_path, OUT_COLS_OHLCV)
    logger.info("market_data: equity fetch %s -> %d rows (%d real / %d filled) -> %s",
                tkr, len(df), real, filled, out_path.name)
    return True


def update_universe(symbols: Optional[Sequence[str]] = None, *,
                    lookback_days: Optional[int] = None,
                    provider: Optional[str] = None) -> Dict:
    """Incrementally refresh existing price CSVs: per symbol, re-fetch from a
    small overlap before its last on-disk bar up to yesterday and merge (new
    bars win). A missing CSV is back-filled from MARKET_DATA_START. Each file's
    schema is preserved (9-col stays 9-col, 6-col stays 6-col).

    This is what the daily periodic task calls — far cheaper than a full ingest.
    """
    syms = [s.strip().upper() for s in (symbols or configured_universe()) if s.strip()]
    cutoff_d = _cutoff_date(None)
    prev = read_meta() or {}
    report: Dict = {
        "started_at": _utcnow_iso(),
        "mode": "incremental",
        "source": (provider or provider_default()),
        "start": prev.get("start", default_start()),
        "end": cutoff_d.isoformat(),
        "symbols": {},
    }
    for sym in syms:
        base = _symbol_base(sym)
        try:
            is_btc = base == "BTC"
            path = PRICES_DIR / f"{base}-USDT.csv"
            last = _last_ts_of_csv(path)
            if last is None:
                start_d = _parse_date(default_start())
            elif lookback_days:
                start_d = max(_parse_date(default_start()),
                              cutoff_d - timedelta(days=int(lookback_days)))
            else:  # incremental: 2-day overlap before the last on-disk bar
                start_d = max(_parse_date(default_start()),
                              last.date() - timedelta(days=2))
            if start_d > cutoff_d:
                report["symbols"][sym] = {"status": "up_to_date"}
                continue
            if is_btc or _csv_is_full9(path):
                df, cov = build_full(f"{base}-USDT", start_d, cutoff_d,
                                     with_oi=is_btc, with_funding=True, provider=provider)
                cols = OUT_COLS_FULL
            else:
                df, cov = build_ohlcv(f"{base}-USDT", start_d, cutoff_d, provider=provider)
                cols = OUT_COLS_OHLCV
            if cov.get("status") != "ok" or df.empty:
                report["symbols"][sym] = {"status": cov.get("status", "no_data")}
                continue
            total = _merge_and_write(path, df, cols)
            if is_btc:
                _merge_and_write(BTC_PRIMARY_CSV, df, cols)
            report["symbols"][sym] = {
                "status": "ok",
                "refreshed_from": start_d.isoformat(),
                "new_bars": int(len(df)),
                "total_rows": total,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("market_data: refresh failed for %s", sym)
            report["symbols"][sym] = {"status": "error",
                                      "error": f"{type(exc).__name__}: {exc}"}
    report["finished_at"] = _utcnow_iso()
    report["ok_count"] = sum(
        1 for v in report["symbols"].values() if v.get("status") in ("ok", "up_to_date"))
    report["total"] = len(syms)
    _write_meta(report)
    logger.info("market_data: incremental refresh done — %d/%d ok",
                report["ok_count"], report["total"])
    return report


def refresh_task() -> Dict:
    """No-arg entry point for the daily periodic task runner — incrementally
    refreshes the configured universe (provider/symbols from env)."""
    return update_universe()


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m backend.core.market_data",
        description="Ingest real Binance public market data into the app CSV schema.",
    )
    p.add_argument("--symbols", default="",
                   help="Comma-separated app tickers (default: MARKET_DATA_SYMBOLS "
                        "env or the built-in 30-symbol universe). e.g. BTC-USDT,ETH-USDT")
    p.add_argument("--start", default="",
                   help=f"UTC start date YYYY-MM-DD (default: {DEFAULT_START}).")
    p.add_argument("--end", default="",
                   help="UTC end date YYYY-MM-DD (default: yesterday UTC).")
    p.add_argument("--no-oi", action="store_true",
                   help="Skip open-interest (metrics) fetch for BTC — much faster.")
    p.add_argument("--no-funding", action="store_true",
                   help="Skip funding-rate fetch for BTC.")
    p.add_argument("--provider", default="",
                   help="Data source: 'binance_public' (default) or 'yahoo'. "
                        "Yahoo gives OHLCV only (no funding/OI) and ~730d of hourly.")
    p.add_argument("--incremental", action="store_true",
                   help="Incrementally refresh existing CSVs (cheap) instead of a "
                        "full re-ingest. This is what the daily task runs.")
    p.add_argument("--lookback-days", type=int, default=0,
                   help="With --incremental: re-fetch exactly the last N days "
                        "(default: from each file's last bar with a 2-day overlap).")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _build_arg_parser().parse_args(argv)
    symbols = [s for s in (args.symbols.split(",") if args.symbols else []) if s.strip()]
    provider = args.provider or None
    if args.incremental:
        report = update_universe(
            symbols or None,
            lookback_days=(args.lookback_days or None),
            provider=provider,
        )
    else:
        report = ingest_universe(
            symbols or None,
            start=args.start or None,
            end=args.end or None,
            with_oi=not args.no_oi,
            with_funding=not args.no_funding,
            provider=provider,
        )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("ok_count", 0) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "ingest_universe",
    "update_universe",
    "refresh_task",
    "ensure_symbol",
    "build_full",
    "build_btc_primary",
    "build_ohlcv",
    "fetch_klines",
    "fetch_klines_yahoo",
    "fetch_funding",
    "fetch_open_interest",
    "read_meta",
    "provider_default",
    "configured_universe",
    "DEFAULT_UNIVERSE",
    "BTC_PRIMARY_CSV",
    "PRICES_DIR",
    "META_SIDECAR",
]

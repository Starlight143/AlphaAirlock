"""Synthetic 2-year hourly crypto dataset generator.

Output schema (strict lowercase) matches the Mission 2 contract:
    timestamp, open, high, low, close, volume, open_interest,
    funding_rate, liquidations.

Economic correlations injected:
- Short-squeeze regimes: clusters of bars where heavily negative funding_rate
  spikes (longs paying shorts an extreme premium) coincide with abnormally
  high liquidations volume. These are seeded so backtests of mean-reversion
  factors can find a recognizable signature.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import math

import numpy as np
import pandas as pd

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DATA_DIR: Final[Path] = PROJECT_ROOT / "backend" / "data"
OUTPUT_CSV: Final[Path] = DATA_DIR / "synthetic_btc.csv"

HOURS_PER_YEAR = 8760
TOTAL_HOURS = HOURS_PER_YEAR * 2  # 17,520 hourly bars
START_PRICE = 30_000.0
SEED = 20260524


def generate(n_hours: int = TOTAL_HOURS, seed: int = SEED) -> pd.DataFrame:
    """Generate `n_hours` of synthetic BTC-style perpetual data."""
    rng = np.random.default_rng(seed)

    timestamps = pd.date_range(
        start="2024-01-01 00:00:00",
        periods=n_hours,
        freq="1h",
        tz="UTC",
    )

    # ------------------------------------------------------------------
    # 1. Base log-price walk with mild trend and regime-driven volatility.
    # ------------------------------------------------------------------
    hourly_drift = 0.00003                              # tiny positive drift
    base_sigma = 0.008                                  # ~0.8% hourly vol
    sigma = np.full(n_hours, base_sigma, dtype=np.float64)

    # Inject ~12 short-squeeze regimes uniformly spaced through the timeline.
    n_regimes = 12
    regime_centers = rng.integers(low=200, high=n_hours - 200, size=n_regimes)
    regime_mask = np.zeros(n_hours, dtype=bool)
    for c in regime_centers:
        width = int(rng.integers(8, 36))                # 8-36h regime length
        lo, hi = max(0, c - width), min(n_hours, c + width)
        regime_mask[lo:hi] = True
    # Apply the vol spike exactly once per bar regardless of how many regime
    # windows overlap — prevents 2.6^N× blowup on overlapping centers.
    sigma[regime_mask] *= 2.6                           # vol spike inside regime

    log_returns = rng.normal(loc=hourly_drift, scale=sigma)
    log_price = np.cumsum(log_returns) + np.log(START_PRICE)
    close = np.exp(log_price)

    # ------------------------------------------------------------------
    # 2. Build OHLC consistent with the close path.
    # ------------------------------------------------------------------
    open_ = np.empty(n_hours)
    open_[0] = START_PRICE
    open_[1:] = close[:-1]

    intra_range = np.abs(rng.normal(0.0, sigma * 0.8))
    high = np.maximum(open_, close) * (1.0 + intra_range)
    low = np.minimum(open_, close) * (1.0 - intra_range)
    # Guarantee high >= max(open, close) and low <= min(open, close)
    high = np.maximum.reduce([high, open_, close])
    low = np.minimum.reduce([low, open_, close])

    # ------------------------------------------------------------------
    # 3. Volume — log-normal baseline with diurnal cycle and vol-shock spikes.
    # ------------------------------------------------------------------
    hour_of_day = np.arange(n_hours) % 24
    diurnal = 1.0 + 0.35 * np.sin((hour_of_day - 6) * (np.pi / 12.0))
    base_volume = np.exp(rng.normal(loc=np.log(1200.0), scale=0.45, size=n_hours))
    volume = base_volume * diurnal
    volume[regime_mask] *= rng.uniform(2.5, 5.0, size=int(regime_mask.sum()))

    # ------------------------------------------------------------------
    # 4. Open interest — mean-reverting AR(1) drifting around a trend.
    # ------------------------------------------------------------------
    open_interest = np.empty(n_hours)
    open_interest[0] = 50_000.0
    trend = np.linspace(0.0, 20_000.0, n_hours)
    ar_shocks = rng.normal(0.0, 600.0, size=n_hours)
    for t in range(1, n_hours):
        mean_rev = 0.985 * (open_interest[t - 1] - trend[t - 1]) + trend[t]
        open_interest[t] = max(1_000.0, mean_rev + ar_shocks[t])
    # Spike OI right before/at squeeze regime onset (longs piling in).
    for c in regime_centers:
        pre_lo = max(0, c - 24)
        open_interest[pre_lo:c] *= rng.uniform(1.10, 1.30)

    # ------------------------------------------------------------------
    # 5. Funding rate — small persistent value, sharp negative inside squeezes.
    # ------------------------------------------------------------------
    funding_noise = rng.normal(0.00005, 0.00015, size=n_hours)
    funding_rate = funding_noise.copy()
    # Drift with OI vs trend: high OI -> more positive funding (longs crowded).
    funding_rate += 0.000004 * (open_interest - trend) / 1000.0
    # Inject extreme negative spikes inside squeeze regimes (longs liquidated).
    squeeze_idx = np.where(regime_mask)[0]
    if squeeze_idx.size:
        funding_rate[squeeze_idx] -= np.abs(
            rng.normal(0.0012, 0.0006, size=squeeze_idx.size)
        )

    # ------------------------------------------------------------------
    # 6. Liquidations — heavy-tailed; explode inside squeeze regimes and
    #    correlate strongly with the magnitude of negative funding spikes.
    # ------------------------------------------------------------------
    base_liq = np.abs(rng.normal(0.0, 80.0, size=n_hours))
    liquidations = base_liq.copy()
    if squeeze_idx.size:
        squeeze_funding = np.abs(funding_rate[squeeze_idx])
        liquidations[squeeze_idx] += (
            squeeze_funding * 5.0e5
            + np.abs(rng.normal(2_000.0, 800.0, size=squeeze_idx.size))
        )

    # ------------------------------------------------------------------
    # 7. Assemble strict lowercase schema.
    # ------------------------------------------------------------------
    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": np.round(open_, 4),
            "high": np.round(high, 4),
            "low": np.round(low, 4),
            "close": np.round(close, 4),
            "volume": np.round(volume, 4),
            "open_interest": np.round(open_interest, 4),
            "funding_rate": np.round(funding_rate, 8),
            "liquidations": np.round(liquidations, 4),
        }
    )
    return df


# ---------------------------------------------------------------------------
# P8-FIX/C-2 — multi-asset synthetic universe for cointegration scan
# ---------------------------------------------------------------------------

# Default 30-symbol universe — mirrors common USDT-perp tickers so the
# cointegration page reads naturally even though the prices are synthetic.
DEFAULT_MULTIASSET_SYMBOLS: Final[tuple] = (
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT", "ADA-USDT",
    "DOGE-USDT", "TRX-USDT", "AVAX-USDT", "LINK-USDT", "DOT-USDT", "MATIC-USDT",
    "LTC-USDT", "BCH-USDT", "NEAR-USDT", "ATOM-USDT", "ETC-USDT", "FIL-USDT",
    "APT-USDT", "ARB-USDT", "OP-USDT", "SUI-USDT", "INJ-USDT", "TIA-USDT",
    "SEI-USDT", "RNDR-USDT", "IMX-USDT", "STX-USDT", "GRT-USDT", "AAVE-USDT",
)
DEFAULT_PRICES_DIR: Final[Path] = PROJECT_ROOT / "storage" / "prices"


def generate_multiasset(
    symbols: tuple | list | None = None,
    n_hours: int = TOTAL_HOURS,
    seed: int = SEED,
    out_dir: Path | None = None,
    base_log_returns: "pd.Series | None" = None,
) -> dict:
    """Generate ``len(symbols)`` partially-correlated synthetic price CSVs.

    Each non-base symbol is modelled as ``ret_i = beta_i * btc_ret + noise_i``
    with ``beta_i ~ Uniform[0.4, 1.6]`` and per-symbol idiosyncratic noise
    scaled so realised correlations land in roughly ``[0.30, 0.85]`` — enough
    spread that Engle-Granger over the universe produces a meaningful range
    of p-values (some pairs cointegrated, most not).

    The first symbol in ``symbols`` is taken as the **factor** (BTC by
    default); its log-return path drives every other asset.

    Returns a dict ``{symbol: Path}`` mapping each emitted CSV file. Existing
    CSVs are overwritten — callers wanting idempotency should ``rmtree`` the
    target directory first.
    """
    symbols = tuple(symbols) if symbols else DEFAULT_MULTIASSET_SYMBOLS
    if not symbols:
        return {}
    out_dir = (out_dir or DEFAULT_PRICES_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(seed)

    timestamps = pd.date_range(
        start="2024-01-01 00:00:00",
        periods=n_hours,
        freq="1h",
        tz="UTC",
    )

    # 1) Base BTC log-returns — either supplied externally or freshly drawn.
    if base_log_returns is None:
        sigma_base = 0.008
        base_log_returns = rng.normal(loc=0.00003, scale=sigma_base, size=n_hours)
    else:
        base_log_returns = np.asarray(base_log_returns, dtype=np.float64)[:n_hours]
    btc_log_price = np.cumsum(base_log_returns) + np.log(START_PRICE)
    btc_close = np.exp(btc_log_price)

    written: dict = {}
    for idx, symbol in enumerate(symbols):
        # Per-symbol seed so each symbol's idiosyncratic noise is reproducible
        # independently of universe ordering.
        sym_rng = np.random.default_rng(seed + 1009 * (idx + 1))

        if idx == 0:
            # Factor asset — use the base directly so the universe always
            # contains a "true" BTC-like series at index 0.
            sym_log_ret = base_log_returns.copy()
            start = START_PRICE
        else:
            beta = float(sym_rng.uniform(0.4, 1.6))
            mu_idio = float(sym_rng.normal(0.0, 0.00002))
            sigma_idio = float(sym_rng.uniform(0.0040, 0.0080))
            noise = sym_rng.normal(loc=mu_idio, scale=sigma_idio, size=n_hours)
            sym_log_ret = beta * base_log_returns + noise
            start = float(sym_rng.uniform(0.5, 250.0))  # absolute starting price varies

        log_price = np.cumsum(sym_log_ret) + np.log(start)
        close = np.exp(log_price)

        # Build OHLC consistent with close path.
        open_ = np.empty(n_hours)
        open_[0] = start
        open_[1:] = close[:-1]
        intra = np.abs(sym_rng.normal(0.0, np.abs(sym_log_ret) * 0.8 + 1e-9))
        high = np.maximum(open_, close) * (1.0 + intra)
        low = np.minimum(open_, close) * (1.0 - intra)
        high = np.maximum.reduce([high, open_, close])
        low = np.minimum.reduce([low, open_, close])
        low = np.maximum(low, 1e-9)  # clamp away from zero / negative

        # Volume — log-normal with mild diurnal cycle.
        hod = np.arange(n_hours) % 24
        diurnal = 1.0 + 0.30 * np.sin((hod - 6) * (np.pi / 12.0))
        base_vol = np.exp(sym_rng.normal(loc=np.log(800.0), scale=0.50, size=n_hours))
        volume = base_vol * diurnal

        df = pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": np.round(open_, 6),
                "high": np.round(high, 6),
                "low": np.round(low, 6),
                "close": np.round(close, 6),
                "volume": np.round(volume, 4),
            }
        )
        out_path = out_dir / f"{symbol}.csv"
        df.to_csv(out_path, index=False)
        written[symbol] = out_path

    # Sanity print so the operator running `python -m backend.core.data_gen
    # --multiasset` sees that the universe is non-degenerate.
    try:
        sample_a = pd.read_csv(out_dir / f"{symbols[0]}.csv")["close"].pct_change().dropna()
        sample_b = pd.read_csv(out_dir / f"{symbols[min(1, len(symbols)-1)]}.csv")["close"].pct_change().dropna()
        corr_mat = np.corrcoef(sample_a.values, sample_b.values)
        corr = float(corr_mat[0, 1])
        corr_str = f"{corr:+.3f}" if math.isfinite(corr) else "nan (degenerate series — check seed)"
        print(
            f"[data_gen] Multi-asset universe: {len(written)} symbols -> {out_dir}\n"
            f"[data_gen] Sample correlation {symbols[0]} vs {symbols[min(1,len(symbols)-1)]}: {corr_str}"
        )
    except Exception as _exc:  # noqa: BLE001
        print(f"[data_gen] Multi-asset universe: {len(written)} symbols -> {out_dir} (corr check failed: {_exc})")
    return written


def main() -> None:
    import sys

    if "--multiasset" in sys.argv[1:]:
        generate_multiasset()
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df = generate()
    df.to_csv(OUTPUT_CSV, index=False)
    print(f"[data_gen] Wrote {len(df):,} rows -> {OUTPUT_CSV}")
    # Sanity print: confirm short-squeeze correlation actually appears.
    neg = df["funding_rate"] < -0.0008
    print(
        f"[data_gen] Negative-funding bars: {int(neg.sum()):,} "
        f"| mean liquidations (squeeze): {df.loc[neg, 'liquidations'].mean():.1f} "
        f"| mean liquidations (calm): {df.loc[~neg, 'liquidations'].mean():.1f}"
    )

    if "--all" in sys.argv[1:]:
        generate_multiasset()


if __name__ == "__main__":
    main()

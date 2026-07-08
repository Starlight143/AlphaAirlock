"""Tests for the multi-asset instrument universe (backend.core.universe) and the
equity fetch path (backend.core.market_data.ensure_equity_symbol).

Locks in the P-MULTISYM feature: the autonomous pipeline can target crypto bases
*and* US equities, not just BTC. The high-value invariants verified here:

  * The loaded frame ALWAYS carries the 8-column sandbox/engine contract — the
    derivative columns (open_interest/funding_rate/liquidations) are padded to
    0.0 for the 6-col non-BTC series. A regression here = KeyError in every
    generated factor that references those columns.
  * Yahoo's :30-aligned equity bars are floored to :00 so they land on the same
    continuous hourly grid crypto uses. A regression here = an empty backtest
    (all-NaN reindex), which is exactly the failure the floor fix prevents.
  * BTC stays the untouched default everywhere (kill-switch, resolution, the
    Coder context note).

All tests are network-free: the Yahoo fetch is monkeypatched.
"""
from __future__ import annotations

import pandas as pd
import pytest

import backend.core.market_data as md
import backend.core.universe as U


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

_UNIVERSE_ENV_KEYS = (
    "STRATEGY_MULTI_SYMBOL_ENABLED",
    "STRATEGY_UNIVERSE_MODE",
    "STRATEGY_SYMBOL_SELECTION",
    "STRATEGY_CRYPTO_SYMBOLS",
    "STRATEGY_EQUITY_SYMBOLS",
    "EQUITY_DATA_MAX_AGE_DAYS",
)


@pytest.fixture(autouse=True)
def _clean_universe_env(monkeypatch):
    """Strip any STRATEGY_*/EQUITY_* leakage from the developer's .env so each
    test sees the documented defaults, and reset the round-robin cursor."""
    for key in _UNIVERSE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    U._reset_pick_counter()
    yield
    U._reset_pick_counter()


# --------------------------------------------------------------------------- #
# Resolution + path naming                                                    #
# --------------------------------------------------------------------------- #

def test_resolve_crypto_vs_equity_and_csv_naming():
    btc = U.resolve("btc")  # case-insensitive
    assert btc is not None
    assert (btc.app_symbol, btc.asset_class, btc.csv_name) == ("BTC", "crypto", "BTC-USDT.csv")

    eth = U.resolve("ETH")
    assert eth is not None and eth.asset_class == "crypto"
    assert eth.csv_name == "ETH-USDT.csv"

    aapl = U.resolve("AAPL")  # in DEFAULT_EQUITY_SYMBOLS
    assert aapl is not None
    assert (aapl.app_symbol, aapl.asset_class, aapl.provider) == ("AAPL", "equity", "yahoo")
    assert aapl.csv_name == "AAPL.csv"  # bare ticker, NOT AAPL-USDT.csv

    assert U.resolve("DEFINITELY_NOT_A_SYMBOL") is None


def test_price_csv_path_naming(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "PRICES_DIR", tmp_path)
    assert U.price_csv_path("ETH") == tmp_path / "ETH-USDT.csv"
    assert U.price_csv_path("AAPL") == tmp_path / "AAPL.csv"
    # Unknown symbol falls back to the crypto <BASE>-USDT.csv convention.
    assert U.price_csv_path("ZZZ") == tmp_path / "ZZZ-USDT.csv"


def test_metadata_helpers():
    assert U.asset_class_of("ETH") == "crypto"
    assert U.asset_class_of("AAPL") == "equity"
    assert U.asset_class_of("unknown") == "crypto"  # safe default
    assert U.display_of("BTC") == "BTC-USDT"
    assert U.display_of("AAPL") == "AAPL"


# --------------------------------------------------------------------------- #
# Active universe + kill switch + modes                                       #
# --------------------------------------------------------------------------- #

def test_kill_switch_forces_btc_only(monkeypatch):
    monkeypatch.setenv("STRATEGY_MULTI_SYMBOL_ENABLED", "false")
    uni = U.active_universe()
    assert [i.app_symbol for i in uni] == ["BTC"]
    # And selection is therefore always BTC.
    assert U.pick_for_strategy().app_symbol == "BTC"


def test_universe_modes(monkeypatch):
    monkeypatch.setenv("STRATEGY_CRYPTO_SYMBOLS", "BTC,ETH")
    monkeypatch.setenv("STRATEGY_EQUITY_SYMBOLS", "AAPL,SPY")

    monkeypatch.setenv("STRATEGY_UNIVERSE_MODE", "crypto")
    assert {i.app_symbol for i in U.active_universe()} == {"BTC", "ETH"}
    assert all(i.asset_class == "crypto" for i in U.active_universe())

    monkeypatch.setenv("STRATEGY_UNIVERSE_MODE", "equity")
    assert {i.app_symbol for i in U.active_universe()} == {"AAPL", "SPY"}
    assert all(i.asset_class == "equity" for i in U.active_universe())

    monkeypatch.setenv("STRATEGY_UNIVERSE_MODE", "mixed")
    assert {i.app_symbol for i in U.active_universe()} == {"BTC", "ETH", "AAPL", "SPY"}

    # Bad value falls back to the documented default ("mixed").
    monkeypatch.setenv("STRATEGY_UNIVERSE_MODE", "garbage")
    assert U.universe_mode() == "mixed"


def test_equity_override_shadowed_by_crypto(monkeypatch):
    # A ticker that is both a crypto base and an equity override must resolve to
    # crypto (no duplicate, no equity entry).
    monkeypatch.setenv("STRATEGY_CRYPTO_SYMBOLS", "BTC,LINK")
    monkeypatch.setenv("STRATEGY_EQUITY_SYMBOLS", "LINK,AAPL")
    monkeypatch.setenv("STRATEGY_UNIVERSE_MODE", "mixed")
    uni = {i.app_symbol: i for i in U.active_universe()}
    assert uni["LINK"].asset_class == "crypto"
    assert uni["AAPL"].asset_class == "equity"
    assert U.resolve("LINK").asset_class == "crypto"


# --------------------------------------------------------------------------- #
# Selection                                                                   #
# --------------------------------------------------------------------------- #

def test_rotate_selection_is_deterministic_round_robin(monkeypatch):
    monkeypatch.setenv("STRATEGY_CRYPTO_SYMBOLS", "BTC,ETH")
    monkeypatch.setenv("STRATEGY_EQUITY_SYMBOLS", "AAPL")
    monkeypatch.setenv("STRATEGY_UNIVERSE_MODE", "mixed")
    monkeypatch.setenv("STRATEGY_SYMBOL_SELECTION", "rotate")
    U._reset_pick_counter()
    # mixed mode interleaves the classes, so the equity (AAPL) lands second.
    picks = [U.pick_for_strategy().app_symbol for _ in range(5)]
    assert picks == ["BTC", "AAPL", "ETH", "BTC", "AAPL"]


def test_mixed_mode_interleaves_equities_early():
    # Defaults: 30 crypto + 12 equity. The interleave must surface an equity
    # within the first few picks (not after all 30 cryptos) and cover all 42.
    uni = U.active_universe()
    assert len(uni) == 42
    assert uni[0].app_symbol == "BTC"  # BTC always leads
    classes = [i.asset_class for i in uni]
    first_equity = classes.index("equity")
    assert first_equity <= 3, f"first equity too late at {first_equity}"
    # No class clusters for too long (ratio ~2.5:1 => runs of <=4).
    run = 1
    longest = 1
    for a, b in zip(classes, classes[1:]):
        run = run + 1 if a == b else 1
        longest = max(longest, run)
    assert longest <= 4, f"class run too long: {longest}"
    assert {i.app_symbol for i in uni} == set(U.crypto_symbols()) | set(U.equity_symbols())


def test_random_selection_stays_in_universe(monkeypatch):
    monkeypatch.setenv("STRATEGY_CRYPTO_SYMBOLS", "BTC,ETH")
    monkeypatch.setenv("STRATEGY_EQUITY_SYMBOLS", "AAPL")
    monkeypatch.setenv("STRATEGY_UNIVERSE_MODE", "mixed")
    monkeypatch.setenv("STRATEGY_SYMBOL_SELECTION", "random")
    allowed = {"BTC", "ETH", "AAPL"}
    for _ in range(30):
        assert U.pick_for_strategy().app_symbol in allowed


# --------------------------------------------------------------------------- #
# load_market_df — the 8-column contract                                      #
# --------------------------------------------------------------------------- #

def test_load_market_df_pads_derivative_columns(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "PRICES_DIR", tmp_path)
    # Write a 6-col OHLCV altcoin CSV (the on-disk altcoin schema).
    idx = pd.date_range("2024-01-01", periods=48, freq="1h", tz="UTC")
    six = pd.DataFrame({
        "timestamp": idx,
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 5.0,
    })
    six.to_csv(tmp_path / "ETH-USDT.csv", index=False)

    df = U.load_market_df("ETH", auto_fetch=False)
    # Every column the sandbox + Coder contract require must be present.
    for col in U.FEATURE_COLS:
        assert col in df.columns, col
    # The padded derivatives are exactly 0.0.
    for col in U.DERIVATIVE_COLS:
        assert (df[col] == 0.0).all()
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.is_monotonic_increasing
    assert len(df) == 48


def test_load_market_df_dedups_and_sorts(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "PRICES_DIR", tmp_path)
    rows = pd.DataFrame({
        "timestamp": [
            "2024-01-01 01:00:00+00:00",
            "2024-01-01 00:00:00+00:00",
            "2024-01-01 01:00:00+00:00",  # duplicate of bar 0, later revision wins
        ],
        "open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3],
        "close": [10.0, 20.0, 30.0], "volume": [1, 1, 1],
    })
    rows.to_csv(tmp_path / "SOL-USDT.csv", index=False)
    df = U.load_market_df("SOL", auto_fetch=False)
    assert df.index.is_monotonic_increasing
    assert len(df) == 2  # duplicate dropped
    # keep="last" => the 30.0 revision wins for the 01:00 bar.
    assert df.loc[pd.Timestamp("2024-01-01 01:00:00+00:00"), "close"] == 30.0


# --------------------------------------------------------------------------- #
# Coder context note                                                          #
# --------------------------------------------------------------------------- #

def test_asset_context_note_btc_is_none():
    assert U.asset_context_note("BTC") is None


def test_asset_context_note_altcoin_and_equity():
    eth_note = U.asset_context_note("ETH")
    assert eth_note is not None
    assert "price/volume" in eth_note
    assert "funding_rate" in eth_note

    aapl_note = U.asset_context_note("AAPL")
    assert aapl_note is not None
    assert "equity" in aapl_note.lower()
    # Equities must be steered away from the derivative columns.
    assert "0.0" in aapl_note


# --------------------------------------------------------------------------- #
# ensure_equity_symbol — the :30 -> :00 grid floor (the critical fix)         #
# --------------------------------------------------------------------------- #

def _fake_yahoo_session_frame() -> pd.DataFrame:
    """Two US sessions of 1h bars, stamped at :30 (exactly how Yahoo returns
    intraday equity data)."""
    stamps = []
    for day in ("2026-06-01", "2026-06-02"):
        for hour in (13, 14, 15, 16, 17, 18, 19):  # 09:30..15:30 ET in UTC
            stamps.append(pd.Timestamp(f"{day} {hour}:30:00", tz="UTC"))
    idx = pd.DatetimeIndex(stamps, name="ts")
    n = len(idx)
    return pd.DataFrame({
        "open": [100.0 + i for i in range(n)],
        "high": [101.0 + i for i in range(n)],
        "low": [99.0 + i for i in range(n)],
        "close": [100.5 + i for i in range(n)],
        "volume": [1000.0 + i for i in range(n)],
    }, index=idx)


def test_ensure_equity_symbol_floors_to_hour_grid(monkeypatch, tmp_path):
    captured = {}

    def _fake_fetch(ticker, start_d, cutoff_d, interval="1h"):
        captured["ticker"] = ticker
        captured["interval"] = interval
        return _fake_yahoo_session_frame()

    monkeypatch.setattr(md, "fetch_klines_yahoo", _fake_fetch)
    out = tmp_path / "AAPL.csv"

    ok = md.ensure_equity_symbol("aapl", out_path=out)
    assert ok is True
    assert out.exists() and out.stat().st_size > 0
    # Passed through as upper-cased ticker at hourly interval.
    assert captured["ticker"] == "AAPL"
    assert captured["interval"] == "1h"

    written = pd.read_csv(out)
    assert list(written.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    ts = pd.to_datetime(written["timestamp"], utc=True)
    # EVERY bar must now sit on the :00 grid (the floor fix). Before the fix the
    # :30 bars would miss the grid and the frame would be empty.
    assert (ts.dt.minute == 0).all()
    assert (ts.dt.second == 0).all()
    # The continuous hourly grid spans the overnight gap (real session bars are
    # fewer than total -> some bars were flat-filled).
    real_bars = 14
    assert len(written) > real_bars  # overnight/weekend fill present
    assert written["close"].notna().all()


def test_ensure_equity_symbol_empty_fetch_is_safe(monkeypatch, tmp_path):
    monkeypatch.setattr(md, "fetch_klines_yahoo",
                        lambda *a, **k: pd.DataFrame(columns=["open", "high", "low", "close", "volume"]))
    out = tmp_path / "ZZZ.csv"
    assert md.ensure_equity_symbol("ZZZ", out_path=out) is False
    assert not out.exists()


def test_ensure_equity_symbol_existing_file_short_circuits(monkeypatch, tmp_path):
    out = tmp_path / "MSFT.csv"
    out.write_text("timestamp,open,high,low,close,volume\n2026-01-01 00:00:00+00:00,1,1,1,1,1\n")

    def _boom(*a, **k):  # must NOT be called when a non-empty file already exists
        raise AssertionError("fetch_klines_yahoo should not be called")

    monkeypatch.setattr(md, "fetch_klines_yahoo", _boom)
    assert md.ensure_equity_symbol("MSFT", out_path=out) is True

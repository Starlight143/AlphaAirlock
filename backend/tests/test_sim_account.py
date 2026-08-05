"""SIM-account ledger math: fills, funding sign, costs, idempotency.

Exercises the pure accounting core (`_decide_and_replay`) with a synthetic frame
so no DB / sandbox / CSV is required. This is the money path — if the ledger math
or the idempotency guard breaks, these asserts fail.
"""

from __future__ import annotations

import pandas as pd

from backend.core import sim_account as sa
from backend.core.database import SimAccount


def _mk_df(n: int = 30, price: float = 100.0, funding: float = 0.001) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01 00:00:00+00:00", periods=n, freq="h")
    return pd.DataFrame(
        {"open": price, "high": price, "low": price, "close": price, "funding_rate": funding},
        index=idx,
    )


def _mk_account(df: pd.DataFrame) -> SimAccount:
    acct = SimAccount(
        strategy_id=1,
        symbol="BTC",
        status="active",
        initial_capital=100_000.0,
        fee_bps=0.0005,
        slippage_bps=0.0002,
        start_bar_ts=df.index[0].to_pydatetime(),
        last_bar_ts=df.index[0].to_pydatetime(),
    )
    acct.id = 1
    return acct


def _long_signal(df: pd.DataFrame) -> pd.Series:
    # Long armed for index 4..9 -> next-bar-open entry at pos 5, exit at pos 11.
    sig = pd.Series(0.0, index=df.index)
    sig.iloc[4:10] = 1.0
    return sig


def test_long_round_trip_fills_and_costs() -> None:
    df = _mk_df()
    acct = _mk_account(df)
    new_fills, new_funding, curve, summ = sa._decide_and_replay(acct, df, _long_signal(df), {}, {})

    # Entry then exit, at next-bar open.
    assert [f.side for f in new_fills] == ["buy", "sell"], [f.side for f in new_fills]
    assert new_fills[0].reason == "entry" and new_fills[1].reason == "exit"
    assert pd.Timestamp(new_fills[0].bar_ts) == df.index[5]
    assert pd.Timestamp(new_fills[1].bar_ts) == df.index[11]

    # Round trip at flat price loses only costs (fees + slippage + funding paid).
    assert summ["liquidated"] is False
    assert abs(summ["qty_base"]) < 1e-9            # flat at the end
    assert summ["equity"] < 100_000.0              # bled costs
    assert summ["equity"] > 99_000.0               # but only ~costs, not a blowup
    assert len(curve) == len(df) - 1               # one row per forward bar


def test_long_pays_positive_funding() -> None:
    df = _mk_df(funding=0.001)
    acct = _mk_account(df)
    _, new_funding, _, _ = sa._decide_and_replay(acct, df, _long_signal(df), {}, {})
    # Exactly one 8h settlement falls inside the hold (pos 8, hour 08).
    assert len(new_funding) == 1
    # Long + positive funding => the account PAYS (cashflow negative).
    assert new_funding[0].cashflow_quote < 0.0
    assert pd.Timestamp(new_funding[0].bar_ts).hour == 8


def test_short_receives_positive_funding() -> None:
    df = _mk_df(funding=0.001)
    acct = _mk_account(df)
    sig = pd.Series(0.0, index=df.index)
    sig.iloc[4:10] = -1.0
    _, new_funding, _, _ = sa._decide_and_replay(acct, df, sig, {}, {})
    assert len(new_funding) == 1
    # Short + positive funding => the account RECEIVES (cashflow positive).
    assert new_funding[0].cashflow_quote > 0.0


def test_reticking_is_idempotent() -> None:
    df = _mk_df()
    acct = _mk_account(df)
    sig = _long_signal(df)
    new_fills, new_funding, curve, summ = sa._decide_and_replay(acct, df, sig, {}, {})

    # Persisted state: advance the cursor + feed the prior ledger back in.
    acct.last_bar_ts = pd.Timestamp(summ["last_bar_ts"]).to_pydatetime()
    fills_by_ts = {pd.Timestamp(f.bar_ts): f for f in new_fills}
    funding_by_ts = {pd.Timestamp(f.bar_ts): f for f in new_funding}

    f2, fu2, curve2, summ2 = sa._decide_and_replay(acct, df, sig, fills_by_ts, funding_by_ts)
    # No new rows on replay, and the marked equity is identical.
    assert f2 == [] and fu2 == []
    assert abs(summ2["equity"] - summ["equity"]) < 1e-6
    assert curve2[-1]["equity"] == curve[-1]["equity"]


def test_flat_signal_no_fills() -> None:
    df = _mk_df()
    acct = _mk_account(df)
    sig = pd.Series(0.0, index=df.index)
    new_fills, new_funding, curve, summ = sa._decide_and_replay(acct, df, sig, {}, {})
    assert new_fills == [] and new_funding == []
    assert abs(summ["equity"] - 100_000.0) < 1e-9   # untouched
    assert all(abs(b["ret"]) < 1e-12 for b in curve)


def test_signal_array_backfills_equity_derivative_columns() -> None:
    # Regression (SPY sim bug): equities carry only OHLCV, but the sandbox
    # requires the full canonical column set. _signal_array must backfill the
    # derivative columns (open_interest / funding_rate / liquidations) on a COPY
    # so an equity factor runs — without mutating the ledger df, which keys
    # funding modelling off funding_rate's presence in _decide_and_replay.
    idx = pd.date_range("2024-01-01 00:00:00+00:00", periods=40, freq="h")
    close = pd.Series(range(100, 140), index=idx, dtype=float)
    df = pd.DataFrame(
        {"open": close, "high": close + 1.0, "low": close - 1.0,
         "close": close, "volume": 1000.0},
        index=idx,
    )
    sig = sa._signal_array(
        "def compute_factor(df):\n    return df['close'].diff().fillna(0.0)", df
    )
    assert list(sig.index) == list(df.index)                 # aligned, no raise
    assert set(df.columns) == {"open", "high", "low", "close", "volume"}  # not mutated


if __name__ == "__main__":
    test_long_round_trip_fills_and_costs()
    test_long_pays_positive_funding()
    test_short_receives_positive_funding()
    test_reticking_is_idempotent()
    test_flat_signal_no_fills()
    test_signal_array_backfills_equity_derivative_columns()
    print("sim_account ledger tests passed")

"""Look-ahead-bias-free hourly backtester.

Contract:
    AlphaBacktester(price_df, signals).run() -> dict with metrics +
    aligned daily equity / drawdown series for charting.

Key invariants enforced for quant correctness:
1. Signals are shifted forward by one period BEFORE multiplication with
   returns to eliminate look-ahead bias.
2. Transaction fee (0.05%) + slippage (0.02%) are deducted on every position
   change (entry or exit), proportional to the absolute change in position.
3. Sharpe ratio is annualized using sqrt(8760) (hourly bars -> annual).
4. Every division is guarded against zero/NaN.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

HOURS_PER_YEAR: int = 24 * 365  # 8760
SQRT_HOURS_PER_YEAR: float = math.sqrt(HOURS_PER_YEAR)

FEE_BPS: float = 0.0005   # 0.05% per side
SLIPPAGE_BPS: float = 0.0002  # 0.02% per side
ROUND_TRIP_COST: float = FEE_BPS + SLIPPAGE_BPS  # paid proportional to |delta position|


@dataclass
class BacktestResult:
    metrics: Dict[str, float]
    equity_curve: List[Dict[str, Any]]   # daily-aligned for charting
    trades: int
    raw: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "metrics": self.metrics,
            "equity_curve": self.equity_curve,
            "trades": self.trades,
        }
        out.update({k: v for k, v in self.raw.items() if k not in out})
        return out


class AlphaBacktester:
    """Hourly long/short/flat backtester for a single asset."""

    def __init__(
        self,
        price_df: pd.DataFrame,
        signals: pd.Series,
        fee: float = FEE_BPS,
        slippage: float = SLIPPAGE_BPS,
    ) -> None:
        if not isinstance(price_df, pd.DataFrame):
            raise TypeError("price_df must be a pandas DataFrame")
        if not isinstance(signals, pd.Series):
            raise TypeError("signals must be a pandas Series")
        if "close" not in price_df.columns:
            raise ValueError("price_df must contain lowercase 'close' column")

        # P29-C2: skip copy+to_datetime when caller already supplied a
        # DatetimeIndex-ed frame (e.g. sweep workers reuse one frame across
        # many cells).
        if (
            isinstance(price_df.index, pd.DatetimeIndex)
            and "timestamp" not in price_df.columns
        ):
            self._raw_df = price_df
        else:
            self._raw_df = price_df.copy()
            if "timestamp" in self._raw_df.columns:
                ts = pd.to_datetime(self._raw_df["timestamp"], utc=True, errors="coerce")
                self._raw_df = self._raw_df.assign(timestamp=ts)
                self._raw_df = self._raw_df.dropna(subset=["timestamp"]).set_index("timestamp")
                # P32-D4 / DAT32-5 + P32-D5 / DAT32-7 — drop duplicate timestamps
                # (keep latest revision) and sort so all downstream slicing is monotonic.
                self._raw_df = self._raw_df[~self._raw_df.index.duplicated(keep="last")].sort_index()
            elif not isinstance(self._raw_df.index, pd.DatetimeIndex):
                raise ValueError("price_df needs either a 'timestamp' column or a DatetimeIndex")
            # P32-D6 / DAT32-12 — coerce-then-dropna can wipe every row if the
            # CSV's timestamp column is malformed; fail fast with a clear error
            # rather than producing an empty backtest with NaN metrics.
            if self._raw_df.empty:
                raise ValueError("price_df is empty after timestamp coercion — nothing to backtest")

        # Align signals to price index. Coerce to numeric, clip to {-1,0,1}.
        sig = signals.copy()
        if isinstance(sig.index, pd.RangeIndex) and len(sig) == len(self._raw_df):
            sig.index = self._raw_df.index
        sig = sig.reindex(self._raw_df.index)
        sig = pd.to_numeric(sig, errors="coerce").fillna(0.0)
        sig = sig.clip(lower=-1.0, upper=1.0)

        self.signals: pd.Series = sig
        self.fee: float = float(fee)
        self.slippage: float = float(slippage)
        self.round_trip_cost: float = self.fee + self.slippage

    # ------------------------------------------------------------------
    # Core pipeline
    # ------------------------------------------------------------------
    def run(self) -> BacktestResult:
        df = self._raw_df
        close = df["close"].astype(float)
        returns = close.pct_change().fillna(0.0)

        # **Critical**: shift signals forward by 1 to remove look-ahead bias.
        shifted = self.signals.shift(1).fillna(0.0)

        # Position-change cost: pay (fee + slippage) * |delta position|.
        position_delta = shifted.diff().abs().fillna(shifted.abs())
        cost_series = position_delta * self.round_trip_cost

        gross = shifted * returns
        net = gross - cost_series

        equity = (1.0 + net).cumprod()
        equity = equity.replace([np.inf, -np.inf], np.nan).ffill().fillna(1.0)

        # P31-R3: anchor running max at the initial-capital baseline (1.0)
        # so a bar-0 loss reports its true peak-to-trough drawdown instead
        # of 0.0 (cummax of a single losing bar = the loss itself, hiding
        # the worst peak-to-trough that starts at t=0).
        running_max = equity.cummax().clip(lower=1.0)
        drawdown = (equity / running_max) - 1.0

        metrics = self._compute_metrics(net, equity, drawdown)
        equity_curve = self._daily_aligned(equity, drawdown)
        trades = int((position_delta > 1e-12).sum())
        per_bar = self._build_per_bar(shifted, net, equity, drawdown, close, position_delta)

        return BacktestResult(
            metrics=metrics,
            equity_curve=equity_curve,
            trades=trades,
            raw={
                "annualization_factor": HOURS_PER_YEAR,
                "fee_per_side": self.fee,
                "slippage_per_side": self.slippage,
                "round_trip_cost": self.round_trip_cost,
                "bars": int(len(net)),
                "per_bar": per_bar,
            },
        )

    # ------------------------------------------------------------------
    # Per-bar trade tape (P5-BE-04, extended in P8-FIX/H-11)
    # ------------------------------------------------------------------
    @staticmethod
    def _direction_for(signal: float) -> str:
        if signal > 1e-9:
            return "long"
        if signal < -1e-9:
            return "short"
        return "flat"

    @staticmethod
    def _build_per_bar(
        shifted_signal: pd.Series,
        net: pd.Series,
        equity: pd.Series,
        drawdown: pd.Series,
        close: Optional[pd.Series] = None,
        position_delta: Optional[pd.Series] = None,
    ) -> List[Dict[str, Any]]:
        """One row per backtester bar — drives the BACKTEST CSV tab in the UI.

        Columns are aligned with the frontend ``TradeTapeRow`` contract:
          - start_time:      ISO Z timestamp of the bar
          - signal:          position actually traded (shift(1) applied), [-1, 1]
          - direction:       'long' | 'short' | 'flat' chip (P8-FIX/H-11)
          - mark_price:      reference close at this bar (P8-FIX/H-11)
          - position_delta:  |Δsignal| this bar — drives trade markers (P8-FIX/H-11)
          - pnl_pct:         this bar's net return (decimal)
          - cum_pnl_pct:     cumulative return so far (= equity - 1.0)
          - drawdown_pct:    cumulative drawdown at this bar
        All floats are rounded to 6 decimals to stay JSON-clean.
        """
        if len(net) == 0:
            return []
        idx = net.index
        # P29-C1: vectorized build. O(N) numpy ops instead of O(N) Series.get
        # loop. Numerically identical to the per-row path; preserves the same
        # JSON rounding (6 decimals) and start_time schema.
        try:
            sig_arr = pd.to_numeric(
                shifted_signal.reindex(idx), errors="coerce"
            ).fillna(0.0).to_numpy(dtype=float)
            pnl_arr = pd.to_numeric(net, errors="coerce").fillna(0.0).to_numpy(dtype=float)
            eq_arr = pd.to_numeric(equity, errors="coerce").fillna(1.0).to_numpy(dtype=float)
            dd_arr = pd.to_numeric(drawdown, errors="coerce").fillna(0.0).to_numpy(dtype=float)
            if close is not None:
                mark_series = pd.to_numeric(close.reindex(idx), errors="coerce")
                mark_arr = mark_series.replace(
                    [np.inf, -np.inf], np.nan
                ).fillna(0.0).to_numpy(dtype=float)
            else:
                mark_arr = np.zeros(len(idx), dtype=float)
            if position_delta is not None:
                pd_arr = pd.to_numeric(
                    position_delta.reindex(idx), errors="coerce"
                ).fillna(0.0).to_numpy(dtype=float)
            else:
                pd_arr = np.zeros(len(idx), dtype=float)
        except (TypeError, ValueError):
            return []

        if isinstance(idx, pd.DatetimeIndex):
            stamps = idx.strftime("%Y-%m-%dT%H:%M:%SZ").tolist()
        else:
            stamps = [
                ts.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(ts, "strftime") else str(ts)
                for ts in idx
            ]

        sig_r = np.round(sig_arr, 6)
        pnl_r = np.round(pnl_arr, 6)
        cum_r = np.round(eq_arr - 1.0, 6)
        dd_r = np.round(dd_arr, 6)
        mark_r = np.round(mark_arr, 6)
        pd_r = np.round(pd_arr, 6)

        out: List[Dict[str, Any]] = [None] * len(idx)  # type: ignore[list-item]
        _dir = AlphaBacktester._direction_for
        for i in range(len(idx)):
            s_val = float(sig_r[i])
            out[i] = {
                "start_time": stamps[i],
                "signal": s_val,
                "direction": _dir(s_val),
                "mark_price": float(mark_r[i]),
                "position_delta": float(pd_r[i]),
                "pnl_pct": float(pnl_r[i]),
                "cum_pnl_pct": float(cum_r[i]),
                "drawdown_pct": float(dd_r[i]),
            }
        return out

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------
    @staticmethod
    def _safe_div(num: float, den: float, default: float = 0.0) -> float:
        if den is None or not np.isfinite(den) or abs(den) < 1e-14:
            return default
        return float(num) / float(den)

    def _compute_metrics(
        self,
        net_returns: pd.Series,
        equity: pd.Series,
        drawdown: pd.Series,
    ) -> Dict[str, float]:
        nr = pd.to_numeric(net_returns, errors="coerce").fillna(0.0)
        # ---- aggregate returns ----
        cumulative_return = float(equity.iloc[-1] - 1.0) if len(equity) else 0.0
        bars = int(len(nr))
        years = self._safe_div(bars, HOURS_PER_YEAR, default=0.0)

        # P29-S4: tighten ``> 0`` to 1e-14 floor (catches IEEE 754 subnormals),
        # bump ``years`` floor from 1e-9 to 1e-14 so ultra-short backtests
        # don't blow up the exponent base to garbage; clamp NaN.
        # P34: gate annualization at the same >=30-bar floor Sharpe uses.
        # Annualizing a sub-30-bar return extrapolates a few hours of noise across
        # a full year (e.g. a single +1% bar → thousands of % annualized, since
        # 1/years ≈ 8760). ``years > 1e-14`` does NOT catch this (years ≈ 1.1e-4
        # for 1 bar). Ultra-short backtests now report 0.0; bars>=30 is unchanged.
        if years > 1e-14 and bars >= 30 and (1.0 + cumulative_return) > 1e-14:
            annualized_return = (1.0 + cumulative_return) ** (1.0 / years) - 1.0
        else:
            annualized_return = 0.0
        if not math.isfinite(annualized_return):
            annualized_return = 0.0

        # ---- risk ----
        std_hourly = float(nr.std(ddof=1)) if bars > 1 else 0.0
        mean_hourly = float(nr.mean()) if bars else 0.0
        # P29-S4: ``nr.std(ddof=1)`` can return NaN; guard before division.
        if not math.isfinite(std_hourly):
            std_hourly = 0.0
        if not math.isfinite(mean_hourly):
            mean_hourly = 0.0
        # P32-NUM32-15: Sharpe with <30 bars is statistically meaningless
        # (sub-hourly noise dominates the mean); previously any 2-bar series
        # produced a "real-looking" annualized Sharpe that downstream gates
        # then promoted. Require ~1 day of hourly observations minimum.
        if std_hourly > 1e-14 and bars >= 30:
            sharpe_hourly = mean_hourly / std_hourly
            sharpe = sharpe_hourly * SQRT_HOURS_PER_YEAR
        else:
            sharpe = 0.0

        max_drawdown = float(drawdown.min()) if len(drawdown) else 0.0

        # P6-D07: average drawdown recovery duration in days. A recovery episode
        # = a contiguous stretch where drawdown < -1e-6, terminated by a return
        # to (or above) the prior all-time-high. Trailing in-DD segment counts
        # too (worst-case bound for the "still recovering" tail).
        in_dd = drawdown < -1e-6
        current_underwater_bars = 0
        if in_dd.any():
            runs: List[int] = []
            cur = 0
            for v in in_dd.values:
                if bool(v):
                    cur += 1
                elif cur > 0:
                    runs.append(cur)
                    cur = 0
            # P29-T2: only count trailing run if it ENDED in recovery; an
            # unfinished underwater segment is "current_underwater_bars".
            last_in_dd = bool(in_dd.iloc[-1]) if len(in_dd) else False
            if cur > 0 and not last_in_dd:
                runs.append(cur)
            if cur > 0 and last_in_dd:
                current_underwater_bars = cur
            avg_recovery_bars = float(sum(runs) / len(runs)) if runs else 0.0
        else:
            avg_recovery_bars = 0.0
        avg_recovery_days = round(avg_recovery_bars / 24.0, 2)
        current_underwater_days = round(current_underwater_bars / 24.0, 2)

        # ---- win rate & profit factor (per-bar P&L, only nonzero exposure) ----
        active = nr[nr.abs() > 1e-12]
        wins = active[active > 0]
        losses = active[active < 0]
        win_rate = self._safe_div(len(wins), len(active), default=0.0)

        gross_profit = float(wins.sum())
        gross_loss = float(-losses.sum())
        profit_factor = self._safe_div(
            gross_profit,
            gross_loss,
            default=float("inf") if gross_profit > 0 else 0.0,
        )
        # Replace +inf with a sentinel large number so JSON serialization works.
        if not math.isfinite(profit_factor):
            profit_factor = 999.0

        # P29-T3: per-trade win-rate/PF (additive — per-bar metrics retained).
        # Trade opens when |position| 0->nonzero, closes when nonzero->0.
        trade_win_rate: float = 0.0
        trade_profit_factor: float = 0.0
        try:
            shifted_pos = self.signals.shift(1).fillna(0.0).reindex(nr.index).fillna(0.0)
            prev = 0.0
            cur_compound = 1.0
            cur_open = False
            trade_pnls: List[float] = []
            for ts, val in shifted_pos.items():
                cur_pos = float(val)
                bar_ret = float(nr.get(ts, 0.0) or 0.0)
                if not cur_open and abs(prev) < 1e-12 and abs(cur_pos) >= 1e-12:
                    cur_open = True
                    cur_compound = 1.0
                    cur_compound *= (1.0 + bar_ret)  # accumulate opening bar's return
                # P31-TRADE-FLIP1: a direct long→short (or short→long) reversal
                # with no intervening flat bar matches neither the open
                # (requires prev≈0) nor the close (requires cur≈0) branch,
                # leaving cur_open True and compounding the new leg onto the old
                # — merging two opposite-direction trades into one.
                # bar_ret at the flip bar = shifted[ts]*returns[ts] - cost[ts]
                # where shifted[ts] = signals[ts-1] = OLD direction, so bar_ret
                # belongs to the OLD trade. Fold it in BEFORE closing the old
                # trade, then reset for the new trade.
                elif (
                    cur_open
                    and abs(prev) >= 1e-12
                    and abs(cur_pos) >= 1e-12
                    and (prev * cur_pos) < 0
                ):
                    cur_compound *= (1.0 + bar_ret)  # this bar belongs to OLD trade
                    trade_pnls.append(cur_compound - 1.0)
                    cur_compound = 1.0
                    # cur_open stays True — the new-direction trade is now open.
                elif cur_open:
                    cur_compound *= (1.0 + bar_ret)
                if cur_open and abs(prev) >= 1e-12 and abs(cur_pos) < 1e-12:
                    trade_pnls.append(cur_compound - 1.0)
                    cur_open = False
                    cur_compound = 1.0
                prev = cur_pos
            if cur_open:
                trade_pnls.append(cur_compound - 1.0)
            if trade_pnls:
                wins_t = [p for p in trade_pnls if p > 0]
                losses_t = [p for p in trade_pnls if p < 0]
                trade_win_rate = float(len(wins_t)) / float(len(trade_pnls))
                gp_t = float(sum(wins_t))
                gl_t = float(-sum(losses_t))
                if gl_t > 1e-14:
                    trade_profit_factor = gp_t / gl_t
                elif gp_t > 0:
                    trade_profit_factor = 999.0
                else:
                    trade_profit_factor = 0.0
                if not math.isfinite(trade_profit_factor):
                    trade_profit_factor = 999.0
        except Exception:  # noqa: BLE001
            trade_win_rate = 0.0
            trade_profit_factor = 0.0

        # P8-FIX/M-13: annualized vol + Calmar so the strategy detail KPI strip
        # can show 8+ tiles without client-side derivation.
        if std_hourly > 1e-14:
            annualized_volatility = float(std_hourly * SQRT_HOURS_PER_YEAR)
        else:
            annualized_volatility = 0.0
        # R5/QR-6: mirror the >=30-bar gate used for annualized_return and Sharpe.
        # annualized_return is already forced to 0.0 for sub-30-bar runs, so make
        # calmar_ratio explicitly 0.0 too. It computes to 0.0 anyway today, but the
        # explicit guard documents the "undefined for <30 bars" contract and stops
        # a future change to annualized_return from silently resurrecting a
        # meaningless Calmar from a few bars of noise.
        if bars < 30 or not (abs(max_drawdown) > 1e-14):
            calmar_ratio = 0.0
        else:
            calmar_ratio = float(annualized_return / abs(max_drawdown))
            if not math.isfinite(calmar_ratio):
                calmar_ratio = 0.0

        return {
            "cumulative_return": round(cumulative_return, 6),
            "annualized_return": round(annualized_return, 6),
            "annualized_sharpe": round(sharpe, 4),
            "annualized_volatility": round(annualized_volatility, 6),
            "calmar_ratio": round(calmar_ratio, 4),
            "max_drawdown": round(max_drawdown, 6),
            "win_rate": round(win_rate, 6),
            "profit_factor": round(profit_factor, 4),
            "mean_hourly_return": round(mean_hourly, 8),
            "std_hourly_return": round(std_hourly, 8),
            "avg_recovery_days": avg_recovery_days,
            "current_underwater_days": current_underwater_days,
            "trade_win_rate": round(trade_win_rate, 6),
            "trade_profit_factor": round(trade_profit_factor, 4),
        }

    # ------------------------------------------------------------------
    # Charting series
    # ------------------------------------------------------------------
    def _daily_aligned(
        self,
        equity: pd.Series,
        drawdown: pd.Series,
    ) -> List[Dict[str, Any]]:
        if equity.empty:
            return []
        daily_equity = equity.resample("1D").last().dropna()
        # R5/QR-1: align daily drawdown to END-of-day like daily_equity (which uses
        # resample("1D").last()). The previous reindex(..., method="ffill") matched
        # each midnight index against the intraday drawdown by carrying the PRIOR
        # bar forward — i.e. start-of-day drawdown, up to a 23h mismatch vs equity.
        daily_dd = drawdown.resample("1D").last().reindex(daily_equity.index).fillna(0.0)
        out: List[Dict[str, Any]] = []
        for ts, eq in daily_equity.items():
            dd_val = float(daily_dd.loc[ts]) if ts in daily_dd.index else 0.0
            out.append(
                {
                    "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "equity": round(float(eq), 6),
                    "drawdown": round(dd_val, 6),
                }
            )
        return out


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------
def _smoke() -> None:
    import os
    from pathlib import Path

    csv = Path(__file__).resolve().parents[1] / "data" / "synthetic_btc.csv"
    if not csv.exists():
        print("synthetic_btc.csv not found; run data_gen.py first.")
        return
    df = pd.read_csv(csv)
    rng = np.random.default_rng(42)
    sig = pd.Series(rng.choice([-1, 0, 1], size=len(df)), index=df.index)
    bt = AlphaBacktester(df, sig)
    result = bt.run()
    print("metrics:", result.metrics)
    print("daily samples:", len(result.equity_curve), "trades:", result.trades)


if __name__ == "__main__":
    _smoke()

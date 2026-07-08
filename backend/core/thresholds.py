"""Single source of truth for production-gate numeric thresholds (P5).

Every threshold that shows up in either the Critic's hard guardrails, the
Paper-Trade health check, or the Backtest Panel's Gate Criteria UI lives in
this module. The values are intentionally split when their semantic meaning
differs (see `critic_max_drawdown` vs `paper_max_drawdown` below) — DO NOT
collapse them without consulting the docstrings.

Downstream consumers:
- backend/agents/critic.py — hard rejection floor for production approval
- backend/core/paper_trader.py — forward-sim health-check floor
- backend/app/main.py     — `_GATE_CRITERIA` and `/api/gate-criteria` endpoint
- backend/app/main.py     — `api_strategy_promote` health-gate (P5-BE-02)

If you need a new threshold, add it here with a one-sentence docstring rather
than scattering literals. Tests can import THRESHOLDS and assert on the
canonical values.
"""

from __future__ import annotations

from typing import Final, Mapping

# ---------------------------------------------------------------------------
# Canonical thresholds
# ---------------------------------------------------------------------------

# Critic / production-rejection floor for the historical backtest. Strategies
# whose backtest MaxDD is worse than this are auto-rejected by the Critic agent
# regardless of the LLM verdict. Held looser than the paper-trade floor because
# the historical sample includes outlier regimes that forward-sim windows skip.
CRITIC_MAX_DRAWDOWN: Final[float] = -0.35

# Paper-trade health-check floor for forward-simulation windows. Held tighter
# than the critic floor because a healthy alpha should NOT reproduce its
# historical worst-case in a recent 30-day window — if it does, the edge is
# decaying.
PAPER_MAX_DRAWDOWN: Final[float] = -0.25

# Display threshold used by the Backtest Panel "Worst MaxDD" gate-criteria
# row. Matches the paper-trade floor so the operator-facing UI tells the same
# story end-to-end (history → forward sim → live).
GATE_DISPLAY_MAX_DRAWDOWN: Final[float] = -0.25

# Minimum annualized Sharpe ratio for both critic approval and paper-trade
# health. Below this, the alpha is statistically indistinguishable from luck.
MIN_SHARPE: Final[float] = 0.5

# Minimum profit factor (gross winning $ / gross losing $). Below 1.05 the
# strategy is round-trip costs from break-even — not investable.
MIN_PROFIT_FACTOR: Final[float] = 1.05

# Minimum trade count for a backtest to be statistically credible. Less than
# this and Sharpe is noise.
MIN_TRADES_BACKTEST: Final[int] = 20

# Minimum trade count for a paper-trade window. Laxer than the backtest gate
# because forward windows are short (default 30 days hourly = ~720 bars).
MIN_TRADES_PAPER: Final[int] = 5

# Maximum age in hours for a paper-trade health-check result to be considered
# fresh for the promotion gate (P5-BE-02). Older runs require an explicit
# rerun before the operator can promote to SMALL_CAPITAL/LIVE.
PAPER_RUN_MAX_AGE_HOURS: Final[int] = 72


# ---------------------------------------------------------------------------
# Aggregated registry (for tests + observability)
# ---------------------------------------------------------------------------

THRESHOLDS: Final[Mapping[str, float]] = {
    "critic_max_drawdown": CRITIC_MAX_DRAWDOWN,
    "paper_max_drawdown": PAPER_MAX_DRAWDOWN,
    "gate_display_max_drawdown": GATE_DISPLAY_MAX_DRAWDOWN,
    "min_sharpe": MIN_SHARPE,
    "min_profit_factor": MIN_PROFIT_FACTOR,
    "min_trades_backtest": float(MIN_TRADES_BACKTEST),
    "min_trades_paper": float(MIN_TRADES_PAPER),
    "paper_run_max_age_hours": float(PAPER_RUN_MAX_AGE_HOURS),
}


__all__ = [
    "CRITIC_MAX_DRAWDOWN",
    "PAPER_MAX_DRAWDOWN",
    "GATE_DISPLAY_MAX_DRAWDOWN",
    "MIN_SHARPE",
    "MIN_PROFIT_FACTOR",
    "MIN_TRADES_BACKTEST",
    "MIN_TRADES_PAPER",
    "PAPER_RUN_MAX_AGE_HOURS",
    "THRESHOLDS",
]

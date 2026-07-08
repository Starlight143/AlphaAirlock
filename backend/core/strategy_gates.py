"""Deterministic strategy quality gates (P-GATES).

Today the Critic's ``"Go"`` is the *sole* acceptance gate — there is no
quantitative floor, so a weak-but-positive backtest can be approved on an LLM
judgment alone, and an obviously-dead backtest (0 trades) still burns a Critic
LLM call. This module adds two deterministic, LLM-independent checks, inspired by
the user's research (Obsidian: 《Production Agent Harness》's machine confidence
cap + 《Regime Filter》's deployment thresholds + 《年化夏普膨脹》/《Sortino》):

  1. **Pre-critic liveness gate** — a cheap "is this backtest even alive?" check
     run *before* the Critic. A backtest with fewer than N trades cannot be a
     real alpha, so it is rejected deterministically and the Critic call is
     skipped (cost saving). Default ON (only trips on truly dead backtests).

  2. **Post-critic quality gate** — deterministic floors (min trades, PSR floor,
     optional Sortino floor) plus a short-vol red-flag (high Sharpe + negative
     skew). This runs AFTER a Critic "Go".

Safety by design — **observe-first**:
  * The quality gate defaults to ``STRATEGY_GATE_ENFORCE=0`` (OBSERVE mode): it
    computes the vetoes/flags and attaches them for telemetry but does NOT change
    the verdict. The approval rate is unchanged until an operator has seen the
    data and explicitly flips ``STRATEGY_GATE_ENFORCE=1``.
  * All thresholds are env-overridable. A floor set to 0 (PSR/Sortino) disables
    that particular check.

Nothing here modifies the engine, stored strategies, or existing behavior when
the env flags are at their defaults beyond skipping the Critic for 0-trade runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend._envloader import env_bool, env_float, env_int


# --------------------------------------------------------------------------- #
# Config (env-overridable)                                                    #
# --------------------------------------------------------------------------- #

def precritic_gate_enabled() -> bool:
    """Skip the Critic + reject deterministically for dead backtests. Default ON."""
    return env_bool("STRATEGY_PRECRITIC_GATE", True)


def precritic_min_trades() -> int:
    """Minimum trades for a backtest to be worth a Critic call. Default 1 (a
    0-trade backtest is unambiguously dead)."""
    return env_int("STRATEGY_PRECRITIC_MIN_TRADES", 1, minimum=0, maximum=100_000)


def quality_gate_enforced() -> bool:
    """When False (default) the quality gate is OBSERVE-only: vetoes are computed
    and recorded but the verdict is unchanged. Set 1 to enforce."""
    return env_bool("STRATEGY_GATE_ENFORCE", False)


def gate_min_trades() -> int:
    return env_int("STRATEGY_GATE_MIN_TRADES", 20, minimum=0, maximum=100_000)


def gate_min_psr() -> float:
    """Probabilistic Sharpe Ratio floor (0..1). 0 disables. Default 0.90 —
    matches the 《年化夏普膨脹》 note's 'PSR < 0.90 → suspect' threshold."""
    return env_float("STRATEGY_GATE_MIN_PSR", 0.90, minimum=0.0, maximum=1.0)


def gate_min_sortino() -> float:
    """Annualized Sortino floor. 0 (default) disables this check."""
    return env_float("STRATEGY_GATE_MIN_SORTINO", 0.0, minimum=0.0, maximum=1000.0)


def gate_min_bootstrap_prob() -> float:
    """Observe-default floor on the bootstrap P(Sharpe>0). 0 disables (default).
    When >0 AND the quality gate is enforced, a strategy whose block-bootstrap
    Sharpe is positive in fewer than this fraction of resamples is vetoed as
    fragile (complements the analytic PSR floor with a non-parametric one)."""
    return env_float("STRATEGY_GATE_MIN_BOOTSTRAP_PROB", 0.0, minimum=0.0, maximum=1.0)


def gate_min_dsr() -> float:
    """Deflated Sharpe Ratio floor (0..1). 0 (default) disables. DSR (Bailey-LdP
    2014) corrects the PSR for multiple-testing selection bias across the strategy
    factory's trials: a Sharpe that cannot beat the best-of-N-under-the-null scores
    low here even when its raw PSR is high. Absent (too few prior trials to
    deflate) ⇒ the check is skipped, never vetoed."""
    return env_float("STRATEGY_GATE_MIN_DSR", 0.0, minimum=0.0, maximum=1.0)


def gate_min_calmar() -> float:
    """Calmar ratio floor (annualized return / |max drawdown|, engine-computed).
    0 (default) disables. A positive floor vetoes strategies whose return is small
    relative to their worst peak-to-trough loss."""
    return env_float("STRATEGY_GATE_MIN_CALMAR", 0.0, minimum=0.0, maximum=1000.0)


def gate_flag_sharpe() -> float:
    return env_float("STRATEGY_GATE_SHORTVOL_SHARPE", 2.0, minimum=0.0, maximum=1000.0)


def gate_flag_skew() -> float:
    return env_float("STRATEGY_GATE_SHORTVOL_SKEW", -1.0, minimum=-1000.0, maximum=0.0)


# T1-D — regime / sub-period stability gate (reads regime_* keys attached by
# regime_metrics; OBSERVE by default, mirrors the quality gate).
def regime_gate_enforced() -> bool:
    return env_bool("STRATEGY_REGIME_GATE", False)


def regime_windows() -> int:
    return env_int("STRATEGY_REGIME_WINDOWS", 6, minimum=2, maximum=24)


def regime_min_positive_fraction() -> float:
    """Veto if fewer than this fraction of evaluated sub-windows are Sharpe>0.
    0 disables this check. Default 0.50 (an edge present in <half the windows is
    regime-fragile)."""
    return env_float("STRATEGY_REGIME_MIN_POSITIVE_FRAC", 0.50, minimum=0.0, maximum=1.0)


def regime_min_worst_sharpe() -> float:
    """Veto if the worst-window annualized Sharpe is below this. Default -1.0
    (deeply negative worst window = one lucky leg propping the whole record)."""
    return env_float("STRATEGY_REGIME_MIN_WORST_SHARPE", -1.0, minimum=-1000.0, maximum=1000.0)


def regime_max_dispersion() -> float:
    """Veto if the dispersion (std) of window Sharpes exceeds this. 0 (default)
    disables — opt-in only."""
    return env_float("STRATEGY_REGIME_MAX_DISPERSION", 0.0, minimum=0.0, maximum=1000.0)


# T3-B — deterministic pre-critic Score Card (a superset of the liveness gate
# that cheaply culls junk the Critic would hard-fail anyway, saving an LLM call).
def scorecard_enabled() -> bool:
    return env_bool("STRATEGY_SCORECARD_GATE", True)


def scorecard_max_trades() -> int:
    """Upper trade bound; above this on the hourly grid the factor is thrashing.
    Default 50000 (>5.7 round-trips/bar on a 8760-bar year is degenerate)."""
    return env_int("STRATEGY_SCORECARD_MAX_TRADES", 50_000, minimum=1, maximum=10_000_000)


# --------------------------------------------------------------------------- #
# Result                                                                      #
# --------------------------------------------------------------------------- #

@dataclass
class GateResult:
    """Outcome of the deterministic quality gate.

    ``passed`` is the effective verdict the caller should honor: in OBSERVE mode
    it is always True (gate never blocks); in ENFORCE mode it is True iff there
    are no vetoes. ``vetoes`` / ``flags`` are always populated for telemetry."""

    passed: bool
    enforced: bool
    vetoes: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "enforced": self.enforced,
            "vetoes": list(self.vetoes),
            "flags": list(self.flags),
        }

    @property
    def reason(self) -> str:
        return "; ".join(self.vetoes) if self.vetoes else ""


# --------------------------------------------------------------------------- #
# Gates                                                                       #
# --------------------------------------------------------------------------- #

def _as_int(metrics: Dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(metrics.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _as_float(metrics: Dict[str, Any], key: str) -> Optional[float]:
    val = metrics.get(key)
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


def is_backtest_alive(metrics: Dict[str, Any]) -> bool:
    """Pre-critic liveness check. True ⇒ worth a Critic call. When the pre-critic
    gate is disabled this is always True (Critic always runs)."""
    if not precritic_gate_enabled():
        return True
    return _as_int(metrics, "num_trades", 0) >= precritic_min_trades()


def precritic_reject_reason(metrics: Dict[str, Any]) -> str:
    """Human-readable reason for a pre-critic rejection (for the postmortem)."""
    n = _as_int(metrics, "num_trades", 0)
    return (f"dead backtest: {n} trades (< {precritic_min_trades()} required) "
            f"— skipped Critic to save an LLM call")


def evaluate_quality(metrics: Dict[str, Any]) -> GateResult:
    """Deterministic post-critic quality gate. In OBSERVE mode (default) returns
    ``passed=True`` regardless of vetoes; in ENFORCE mode ``passed`` requires no
    vetoes. Always returns the computed vetoes/flags for telemetry."""
    vetoes: List[str] = []
    flags: List[str] = []

    num_trades = _as_int(metrics, "num_trades", 0)
    min_trades = gate_min_trades()
    if num_trades < min_trades:
        vetoes.append(f"min_trades({num_trades}<{min_trades})")

    psr = _as_float(metrics, "probabilistic_sharpe_ratio")
    min_psr = gate_min_psr()
    if min_psr > 0.0 and psr is not None and psr < min_psr:
        vetoes.append(f"psr({psr:.3f}<{min_psr:.2f})")

    # DSR — multiple-testing-corrected Sharpe probability (Bailey-LdP 2014).
    # Attached by _augment_metrics only when there were enough prior trials to
    # estimate the trial-Sharpe dispersion; absent ⇒ skipped. Observe-default.
    dsr = _as_float(metrics, "deflated_sharpe_ratio")
    min_dsr = gate_min_dsr()
    if min_dsr > 0.0 and dsr is not None and dsr < min_dsr:
        vetoes.append(f"dsr({dsr:.3f}<{min_dsr:.2f})")

    # Calmar — annualized return per unit of worst drawdown (engine-computed).
    # Observe-default; 0 floor disables.
    calmar = _as_float(metrics, "calmar_ratio")
    min_calmar = gate_min_calmar()
    if min_calmar > 0.0 and calmar is not None and calmar < min_calmar:
        vetoes.append(f"calmar({calmar:.2f}<{min_calmar:.2f})")

    # T-BOOTSTRAP — non-parametric robustness floor (reads the bootstrap_* keys
    # attached in _augment_metrics; absent ⇒ skipped). Observe-default.
    boot_prob = _as_float(metrics, "bootstrap_prob_sharpe_positive")
    min_boot = gate_min_bootstrap_prob()
    if min_boot > 0.0 and boot_prob is not None and boot_prob < min_boot:
        vetoes.append(f"bootstrap_prob({boot_prob:.3f}<{min_boot:.2f})")

    sortino = _as_float(metrics, "sortino_ratio")
    min_sortino = gate_min_sortino()
    if min_sortino > 0.0 and sortino is not None and sortino < min_sortino:
        vetoes.append(f"sortino({sortino:.2f}<{min_sortino:.2f})")

    # Short-vol signature: high Sharpe + negative skew hides left-tail risk.
    sharpe = _as_float(metrics, "annualized_sharpe")
    skew = _as_float(metrics, "return_skewness")
    if (sharpe is not None and skew is not None
            and sharpe > gate_flag_sharpe() and skew < gate_flag_skew()):
        flags.append(
            f"short_vol_signature(sharpe={sharpe:.2f},skew={skew:.2f})")

    enforced = quality_gate_enforced()
    passed = True if not enforced else (len(vetoes) == 0)
    return GateResult(passed=passed, enforced=enforced, vetoes=vetoes, flags=flags)


# --------------------------------------------------------------------------- #
# T1-D — Regime / sub-period stability gate                                   #
# --------------------------------------------------------------------------- #

@dataclass
class RegimeGateResult(GateResult):
    """Quality-gate result extended with an explicit insufficient-data flag.
    When the backtest is too short to slice into stable sub-periods the gate
    NEVER vetoes (passed=True) regardless of enforcement."""

    insufficient_data: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = super().to_dict()
        d["insufficient_data"] = self.insufficient_data
        return d


def evaluate_regime(metrics: Dict[str, Any]) -> RegimeGateResult:
    """Read the already-attached ``regime_*`` keys (does NOT recompute) and apply
    the stability floors. OBSERVE (default) records vetoes but passes; ENFORCE
    vetoes a regime-fragile strategy. Missing/insufficient telemetry always
    passes — never veto on absent data."""
    vetoes: List[str] = []
    flags: List[str] = []
    enforced = regime_gate_enforced()

    n_windows = metrics.get("regime_n_windows")
    if n_windows is None or _as_int(metrics, "regime_n_windows", 0) < 2:
        # Too short to judge stability → never block.
        return RegimeGateResult(passed=True, enforced=enforced,
                                vetoes=[], flags=[], insufficient_data=True)

    pos_frac = _as_float(metrics, "regime_positive_fraction")
    min_frac = regime_min_positive_fraction()
    if min_frac > 0.0 and pos_frac is not None and pos_frac < min_frac:
        vetoes.append(f"regime_positive_frac({pos_frac:.2f}<{min_frac:.2f})")

    worst = _as_float(metrics, "regime_worst_sharpe")
    min_worst = regime_min_worst_sharpe()
    if worst is not None and worst < min_worst:
        vetoes.append(f"regime_worst_sharpe({worst:.2f}<{min_worst:.2f})")

    disp = _as_float(metrics, "regime_sharpe_dispersion")
    max_disp = regime_max_dispersion()
    if max_disp > 0.0 and disp is not None and disp > max_disp:
        vetoes.append(f"regime_dispersion({disp:.2f}>{max_disp:.2f})")

    passed = True if not enforced else (len(vetoes) == 0)
    return RegimeGateResult(passed=passed, enforced=enforced,
                            vetoes=vetoes, flags=flags, insufficient_data=False)


# --------------------------------------------------------------------------- #
# T3-B — Deterministic pre-critic Score Card                                  #
# --------------------------------------------------------------------------- #

@dataclass
class ScoreCardResult:
    """Outcome of the pre-critic deterministic auditor. ``alive`` is the
    effective pre-critic verdict the caller should honor (True ⇒ proceed to the
    Critic). ``rejects`` are the hard reasons; ``checks`` is the per-check pass
    map for telemetry."""

    alive: bool
    rejects: List[str] = field(default_factory=list)
    checks: Dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alive": self.alive,
            "rejects": list(self.rejects),
            "checks": dict(self.checks),
        }

    @property
    def reason(self) -> str:
        return "; ".join(self.rejects) if self.rejects else ""


def evaluate_scorecard(metrics: Dict[str, Any]) -> ScoreCardResult:
    """Deterministic pre-critic auditor. SUPERSET of ``is_backtest_alive``: it
    always runs the liveness trade-floor first, then (when scorecard_enabled())
    cheap structural rejects on the already-augmented metrics. When the Score
    Card is disabled, ``alive`` equals ``is_backtest_alive`` exactly (today's
    behaviour). Pure function over the metrics dict; never raises."""
    rejects: List[str] = []
    checks: Dict[str, bool] = {}

    live = is_backtest_alive(metrics)
    checks["liveness"] = live
    if not live:
        rejects.append(precritic_reject_reason(metrics))

    if scorecard_enabled():
        # Engine metric calc failed (all-NaN return series) → Critic would
        # hard-fail anyway; reject now and save the LLM call.
        core_ok = all(
            _as_float(metrics, k) is not None
            for k in ("annualized_sharpe", "max_drawdown", "profit_factor")
        )
        checks["core_metrics_finite"] = core_ok
        if not core_ok:
            rejects.append("core_metrics_nonfinite (engine metric calc failed)")

        n = _as_int(metrics, "num_trades", 0)
        upper_ok = n <= scorecard_max_trades()
        checks["trades_sane_upper"] = upper_ok
        if not upper_ok:
            rejects.append(f"overtrading({n}>{scorecard_max_trades()})")

        std = _as_float(metrics, "std_hourly_return")
        nondegenerate = not (std is not None and not (std > 1e-14))
        checks["returns_nondegenerate"] = nondegenerate
        if not nondegenerate:
            rejects.append("constant_returns(std~0)")

        # Only when regime telemetry is present (T1-D ran): 0 evaluable windows
        # means an unusable series.
        if metrics.get("regime_n_windows") is not None:
            regime_ok = _as_int(metrics, "regime_n_windows", 0) > 0
            checks["regime_observed"] = regime_ok
            if not regime_ok:
                rejects.append("regime_unusable(0 windows cleared the bar floor)")

    return ScoreCardResult(alive=(len(rejects) == 0), rejects=rejects, checks=checks)


__all__ = [
    "GateResult",
    "RegimeGateResult",
    "ScoreCardResult",
    "precritic_gate_enabled",
    "quality_gate_enforced",
    "regime_gate_enforced",
    "regime_windows",
    "scorecard_enabled",
    "is_backtest_alive",
    "precritic_reject_reason",
    "evaluate_quality",
    "evaluate_regime",
    "evaluate_scorecard",
]

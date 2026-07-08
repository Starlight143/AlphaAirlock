"""LLM daily USD budget cap (P6 cross-cutting safety guard).

Why this exists
---------------
B1 (auto-pipeline from ingest) and B2 (alpha queue by IC) can each fire dozens
of full pipeline runs per day without human review. A single ``call_messages``
costs $0.01–$0.05 on Sonnet; an unattended pipeline producing 100 strategies
overnight = $50+/day. This module enforces a hard daily USD cap so cost spikes
become deterministic 503-style errors rather than surprise invoices.

Design
------
* **Default OFF** — when ``ALPHA_LLM_DAILY_USD_CAP`` is empty or 0, this module
  is a no-op (the same call paths exist but never trigger a budget check).
* **Process + disk persistence** — running total is held in process memory for
  speed and journaled to ``storage/.llm_budget_YYYYMMDD.json`` so a uvicorn
  reload doesn't reset the counter mid-day.
* **Cross-day reset** — the active counter file is keyed by UTC date; midnight
  rollover creates a fresh file naturally.
* **Token estimation, not exact** — we measure the *prompt + completion char
  count* and divide by ``CHARS_PER_TOKEN`` (default 3.5). This is intentionally
  conservative (slight over-estimate); the goal is a cost ceiling, not a billing
  reconciler.
* **Per-call check happens BEFORE the LLM call** — so the budget guard never
  pays for the very call that puts it over.

Public API
----------
``reserve_budget(prompt_chars, est_output_chars=None)``
    Concurrency-safe: atomically check-and-reserve this call's projected cost
    (input + an output estimate) under one lock acquisition, returning a token.
    Raises ``LLMBudgetExceededError`` if the projected total (current spend +
    in-flight reservations + this call's cost) would meet or exceed the cap.
    Closes the check->call->record TOCTOU window for parallel agents.

``settle_reservation(token, actual_input_chars, actual_output_chars)``
    Release the reservation and persist actual usage. MUST run in ``finally``.

``check_budget(prompt_chars, max_output_tokens=0)``
    Back-compat shim (no reservation). Projects input cost plus, when
    ``max_output_tokens`` is supplied, an estimate of the output cost. Prefer
    ``reserve_budget``/``settle_reservation`` for concurrency safety.

``record_usage(input_chars, output_chars)``
    Persist the actual usage after the call returns.

``current_state()``
    Read-only snapshot for ``/api/health`` and the upcoming budget dashboard.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from backend._envloader import env_float, env_int

logger = logging.getLogger("alpha.llm.budget")

# Pricing defaults aim at Sonnet (Anthropic public 2024 Q4 pricing):
#   input  $3 / 1M tokens
#   output $15 / 1M tokens
# Models cheaper than Sonnet just under-utilise the cap (safe); pricier models
# over-utilise (cap fires earlier — also safe).
_DEFAULT_INPUT_PER_1M_USD = 3.0
_DEFAULT_OUTPUT_PER_1M_USD = 15.0
_DEFAULT_CHARS_PER_TOKEN = 3.5

_STATE_DIR = Path(__file__).resolve().parents[2] / "storage"
_STATE_DIR.mkdir(parents=True, exist_ok=True)

_LOCK = threading.Lock()
_STATE_CACHE: Dict[str, Any] = {}  # {"day": "YYYY-MM-DD", "input_chars": int, "output_chars": int, "calls": int}

# P-RACE: in-flight reservations guarded by the SAME _LOCK as _STATE_CACHE.
# Each entry: token -> {"usd": <reserved-usd float>}. reserve_budget() adds a
# reservation under the lock atomically with the check; settle_reservation()
# removes it and applies actual usage. This closes the check->call->record
# TOCTOU window where N concurrent call_messages() all passed check_budget()
# before any recorded its usage.
import itertools as _itertools  # placed here to keep imports local to the patch
_RESERVATIONS: Dict[int, Dict[str, Any]] = {}
_RES_COUNTER = _itertools.count(1)

# P-STRATBUDGET: optional per-strategy USD ceiling. The active strategy id is
# carried on a context variable (set by the orchestrator around a pipeline run;
# the synchronous agent calls run in the SAME thread so they observe it).
# Per-strategy settled spend accumulates here under the SAME _LOCK as the daily
# state, and is cleared when the strategy's scope exits (bounded memory).
_CURRENT_STRATEGY: "contextvars.ContextVar[Optional[int]]" = contextvars.ContextVar(
    "llm_current_strategy", default=None)
_PER_STRATEGY_SPENT: Dict[int, float] = {}


def _reserved_usd_unlocked() -> float:
    """Sum of all in-flight reservations. CALLER MUST HOLD _LOCK."""
    total = 0.0
    for r in _RESERVATIONS.values():
        try:
            total += float(r.get("usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def _est_output_chars_default(prompt_chars: int) -> int:
    """Conservative in-flight output estimate when caller gives none.
    Assumes the completion is as large as the prompt (cost-ceiling bias).
    """
    return int(prompt_chars) if prompt_chars > 0 else 0


class LLMBudgetExceededError(RuntimeError):
    """Raised by ``check_budget`` when projected spend exceeds the configured cap.

    Callers (orchestrator, scheduler hooks) should catch this and convert it to
    a graceful "REJECTED: budget" outcome rather than letting it propagate as
    a 500.
    """


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _state_path(day_iso: str) -> Path:
    return _STATE_DIR / f".llm_budget_{day_iso.replace('-', '')}.json"


def _load_state(day_iso: str) -> Dict[str, Any]:
    """Read counter from disk; missing/corrupt file → fresh zero state."""
    p = _state_path(day_iso)
    if not p.exists():
        return {"day": day_iso, "input_chars": 0, "output_chars": 0, "calls": 0}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"day": day_iso, "input_chars": 0, "output_chars": 0, "calls": 0}
    return {
        "day": str(data.get("day", day_iso)),
        "input_chars": int(data.get("input_chars", 0) or 0),
        "output_chars": int(data.get("output_chars", 0) or 0),
        "calls": int(data.get("calls", 0) or 0),
    }


def _persist_state(state: Dict[str, Any]) -> None:
    # P31-B5: atomic write so a crash mid-write never leaves a truncated
    # JSON that _load_state would reset to zero, silently disabling the cap.
    p = _state_path(state["day"])
    try:
        tmp = p.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        logger.warning("Could not persist LLM budget counter to %s", p)


def _ensure_today_state() -> Dict[str, Any]:
    """Get the cached state, refreshing from disk on day rollover or first call."""
    today = _today_iso()
    if not _STATE_CACHE or _STATE_CACHE.get("day") != today:
        _STATE_CACHE.clear()
        _RESERVATIONS.clear()  # stale cross-midnight reservations are irrelevant to D+1 cap
        _PER_STRATEGY_SPENT.clear()  # P-STRATBUDGET: per-strategy spend resets daily too
        _STATE_CACHE.update(_load_state(today))
    return _STATE_CACHE


def _cap_usd() -> float:
    """Daily USD cap. 0 or unset → disabled (no enforcement)."""
    return env_float("ALPHA_LLM_DAILY_USD_CAP", 0.0, minimum=0.0)


def _per_strategy_cap_usd() -> float:
    """Per-strategy USD ceiling. 0 or unset → disabled. Independent of the daily
    cap; when both are configured, whichever projects over first fires."""
    return env_float("ALPHA_LLM_PER_STRATEGY_USD_CAP", 0.0, minimum=0.0)


def _check_strategy_projection_unlocked(sid: int, extra_usd: float, cap: float,
                                        *, agent: str) -> None:
    """Raise if (this strategy's settled spend + this call's estimate) >= the
    per-strategy cap. CALLER MUST HOLD _LOCK. Within one strategy's pipeline the
    agent calls are sequential, so settled-spend + estimate is an accurate
    projection (at most one call is in flight per strategy at a time)."""
    spent = float(_PER_STRATEGY_SPENT.get(sid, 0.0))
    projected = spent + extra_usd
    if projected >= cap:
        raise LLMBudgetExceededError(
            f"LLM per-strategy USD cap reached for strategy {sid}: spent "
            f"${spent:.4f} of ${cap:.2f}; next call projected to ${projected:.4f}. "
            f"Raise ALPHA_LLM_PER_STRATEGY_USD_CAP to allow deeper retries. "
            f"Agent: {agent or 'unknown'}"
        )


@contextlib.contextmanager
def strategy_budget_scope(strategy_id: Optional[int]):
    """Bind the per-strategy budget context for the duration of a pipeline run.
    Set by the orchestrator around ``_run_pipeline``; the synchronous agent calls
    in the same thread observe it via :data:`_CURRENT_STRATEGY`. Clears the
    strategy's accumulated spend on exit so the per-strategy map stays bounded.
    A ``None`` id is a transparent no-op (e.g. Factor Studio manual eval)."""
    token = _CURRENT_STRATEGY.set(strategy_id)
    try:
        yield
    finally:
        _CURRENT_STRATEGY.reset(token)
        if strategy_id is not None:
            with _LOCK:
                _PER_STRATEGY_SPENT.pop(int(strategy_id), None)


def _input_price_per_1m() -> float:
    return env_float("ALPHA_LLM_INPUT_PRICE_PER_1M", _DEFAULT_INPUT_PER_1M_USD, minimum=0.0)


def _output_price_per_1m() -> float:
    return env_float("ALPHA_LLM_OUTPUT_PRICE_PER_1M", _DEFAULT_OUTPUT_PER_1M_USD, minimum=0.0)


def _chars_per_token() -> float:
    raw = env_float("ALPHA_LLM_CHARS_PER_TOKEN", _DEFAULT_CHARS_PER_TOKEN, minimum=1.0)
    # Guard against divide-by-zero via subnormal floats (CLAUDE.md float rule).
    return raw if raw > 1e-14 else _DEFAULT_CHARS_PER_TOKEN


def _chars_to_usd(input_chars: int, output_chars: int) -> float:
    cpt = _chars_per_token()
    input_tokens = input_chars / cpt
    output_tokens = output_chars / cpt
    in_usd = (input_tokens / 1_000_000.0) * _input_price_per_1m()
    out_usd = (output_tokens / 1_000_000.0) * _output_price_per_1m()
    return in_usd + out_usd


def _est_output_chars(max_output_tokens: int) -> int:
    """Conservative output-char reservation for the pre-call budget gate.

    The actual completion length is unknown before the call, so we reserve the
    requested ``max_tokens`` ceiling converted to chars. This matches the
    module's 'intentionally conservative (slight over-estimate)' design intent
    and is reconciled to the true output by ``record_usage`` afterwards. A
    non-positive ceiling reserves nothing (preserves legacy behaviour).
    """
    if max_output_tokens <= 0:
        return 0
    return int(round(max_output_tokens * _chars_per_token()))


def is_enabled() -> bool:
    """True iff a positive daily cap is configured.

    Uses the subnormal-safe threshold `> 1e-14` (same as every other
    enforcement site in this module) so that IEEE 754 subnormals such as
    5e-324 — which satisfy `> 0.0` — are treated as "disabled".  A caller
    that gates on is_enabled() before calling check_budget() must get the
    same answer both places; using `> 0.0` here while check_budget() uses
    `> 1e-14` internally would create a false-enabled signal.
    """
    return _cap_usd() > 1e-14


def current_state() -> Dict[str, Any]:
    """Snapshot for /api/health and admin tools. Safe to call from any thread."""
    with _LOCK:
        state = _ensure_today_state()
        spent = _chars_to_usd(state["input_chars"], state["output_chars"])
        cap = _cap_usd()
        return {
            "day": state["day"],
            "calls": int(state["calls"]),
            "input_chars": int(state["input_chars"]),
            "output_chars": int(state["output_chars"]),
            "estimated_usd_spent": round(spent, 4),
            # D-M3 — subnormal-safe comparisons: a tiny non-zero cap from a
            # bad env value MUST still be treated as "disabled" because
            # `cap > 0.0` is True for IEEE 754 subnormals like 5e-324.
            "cap_usd": round(cap, 4) if cap > 1e-14 else None,
            "enabled": cap > 1e-14,
            "remaining_usd": round(max(0.0, cap - spent), 4) if cap > 1e-14 else None,
        }


def _check_projection_unlocked(extra_usd: float, *, agent: str) -> None:
    """Raise if (current spend + in-flight reservations + extra) >= cap.
    CALLER MUST HOLD _LOCK. extra_usd is this call's projected cost.
    """
    cap = _cap_usd()
    if not (cap > 1e-14):
        return  # cap disabled — no-op
    state = _ensure_today_state()
    current_spent = _chars_to_usd(state["input_chars"], state["output_chars"])
    reserved = _reserved_usd_unlocked()
    projected = current_spent + reserved + extra_usd
    if projected >= cap:
        raise LLMBudgetExceededError(
            f"LLM daily USD cap reached: spent ${current_spent:.4f} "
            f"(+${reserved:.4f} in-flight) of ${cap:.2f} "
            f"({state['calls']} calls today). Next call projected to ${projected:.4f}. "
            f"Increase ALPHA_LLM_DAILY_USD_CAP or wait for UTC midnight rollover. "
            f"Agent: {agent or 'unknown'}"
        )


def check_budget(prompt_chars: int, *, max_output_tokens: int = 0, agent: str = "") -> None:
    """Back-compat shim: projection with no reservation.

    Prefer ``reserve_budget``/``settle_reservation`` for concurrency safety.
    Kept so existing callers keep working unchanged. ``max_output_tokens``
    (the call's ``max_tokens`` ceiling, default 0 = legacy input-only behaviour)
    lets the projection reserve an estimate of the output cost; output is priced
    ~5x input, so omitting it lets a single large completion overshoot the cap.
    The actual output is reconciled via ``record_usage``.
    """
    cap = _cap_usd()
    # D-M3 — subnormal-safe: `<= 0.0` misses subnormals, treat them as disabled.
    if not (cap > 1e-14):
        return
    extra_usd = _chars_to_usd(prompt_chars, _est_output_chars(max_output_tokens))
    with _LOCK:
        _check_projection_unlocked(extra_usd, agent=agent)


def reserve_budget(
    prompt_chars: int,
    est_output_chars: Optional[int] = None,
    *,
    agent: str = "",
) -> Optional[int]:
    """Atomically check-and-reserve under one lock acquisition.

    Returns an opaque reservation token to pass to ``settle_reservation``,
    or ``None`` when the cap is disabled (settle is then a no-op). Raises
    ``LLMBudgetExceededError`` if the projected total (current spend +
    in-flight reservations + this call's estimated input+output cost) would
    meet or exceed the cap. Estimating output cost up front (conservatively)
    prevents N in-flight calls' completions from being invisible during the
    provider round-trip.
    """
    daily_cap = _cap_usd()
    strat_cap = _per_strategy_cap_usd()
    if not (daily_cap > 1e-14) and not (strat_cap > 1e-14):
        return None  # both caps disabled — no reservation needed
    if prompt_chars < 0:
        prompt_chars = 0
    if est_output_chars is None:
        est_output_chars = _est_output_chars_default(prompt_chars)
    if est_output_chars < 0:
        est_output_chars = 0
    est_usd = _chars_to_usd(int(prompt_chars), int(est_output_chars))
    sid = _CURRENT_STRATEGY.get()
    with _LOCK:
        if daily_cap > 1e-14:
            _check_projection_unlocked(est_usd, agent=agent)
        if strat_cap > 1e-14 and sid is not None:
            # Refresh the day state first so a cross-midnight rollover clears any
            # stale per-strategy spend before this projection check.
            _ensure_today_state()
            _check_strategy_projection_unlocked(int(sid), est_usd, strat_cap, agent=agent)
        token = next(_RES_COUNTER)
        _RESERVATIONS[token] = {"usd": est_usd}
        return token


def settle_reservation(
    token: Optional[int],
    actual_input_chars: int,
    actual_output_chars: int,
    *,
    agent: str = "",
) -> None:
    """Convert a reservation into actual recorded usage. MUST run in a
    ``finally`` so a reservation is never leaked, even on provider error.

    ``token is None`` (cap was disabled at reserve time) is a no-op. A token
    not found (e.g. day rollover cleared nothing — tokens persist across
    days but reservations are transient) is tolerated: usage is still
    recorded.
    """
    if actual_input_chars < 0:
        actual_input_chars = 0
    if actual_output_chars < 0:
        actual_output_chars = 0
    sid = _CURRENT_STRATEGY.get()
    with _LOCK:
        if token is not None:
            _RESERVATIONS.pop(token, None)
        state = _ensure_today_state()
        # P-STRATBUDGET: accumulate this strategy's actual spend AFTER
        # _ensure_today_state() — that call clears the per-strategy map on a day
        # rollover (and on first use), so accumulating before it would wipe the
        # amount we just added. Only track while a per-strategy cap is configured
        # (keeps _PER_STRATEGY_SPENT bounded).
        if sid is not None and _per_strategy_cap_usd() > 1e-14:
            actual_usd = _chars_to_usd(int(actual_input_chars), int(actual_output_chars))
            _PER_STRATEGY_SPENT[int(sid)] = (
                _PER_STRATEGY_SPENT.get(int(sid), 0.0) + actual_usd)
        state["input_chars"] = int(state["input_chars"]) + int(actual_input_chars)
        state["output_chars"] = int(state["output_chars"]) + int(actual_output_chars)
        state["calls"] = int(state["calls"]) + 1
        _persist_state(state)


def record_usage(input_chars: int, output_chars: int, *, agent: str = "") -> None:
    """Persist actual call usage. Safe to call even if cap is disabled."""
    if input_chars < 0:
        input_chars = 0
    if output_chars < 0:
        output_chars = 0
    with _LOCK:
        state = _ensure_today_state()
        state["input_chars"] = int(state["input_chars"]) + int(input_chars)
        state["output_chars"] = int(state["output_chars"]) + int(output_chars)
        state["calls"] = int(state["calls"]) + 1
        _persist_state(state)


def current_strategy_id() -> Optional[int]:
    """The strategy id currently in scope (set by ``strategy_budget_scope``), or
    None. Public accessor so the cost ledger can attribute a call without
    reaching into the private contextvar."""
    return _CURRENT_STRATEGY.get()


def estimate_cost_usd(input_chars: int, output_chars: int) -> float:
    """Public wrapper over the SAME flat pricing the cap uses, so the cost ledger
    records a USD estimate on an identical basis (no second pricing source)."""
    return _chars_to_usd(max(0, int(input_chars or 0)), max(0, int(output_chars or 0)))


__all__ = [
    "LLMBudgetExceededError",
    "check_budget",
    "reserve_budget",
    "settle_reservation",
    "strategy_budget_scope",
    "record_usage",
    "current_state",
    "current_strategy_id",
    "estimate_cost_usd",
    "is_enabled",
]

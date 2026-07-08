"""Agent self-dialogue ring buffer (P8-FIX/H-7).

The reference demo emphasises that agents "talk to each other" — Team A
(intake/researcher/coder/backtester) hands off to/from Team B (critic), and
the operator can watch the conversation transcript in Mission Control.

This module is the single source of truth for that transcript. Implementation
deliberately mirrors ``orchestrator._REGISTRY``: an in-process ring buffer
(``collections.deque(maxlen=...)``) protected by a single ``threading.Lock``
so producer/consumer races never produce torn reads. No DB writes — keeps the
hot path cheap.

The frontend reads via ``GET /api/agent-dialogue?strategy_id=&limit=``;
``orchestrator`` calls :func:`record_dialogue` at each handoff / critic
decision / coder retry boundary.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, List, Literal, Optional, TypedDict

from backend._envloader import env_int

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

DialogueIntent = Literal[
    "question",
    "answer",
    "handoff",
    "critique",
    "approval",
    "veto",
    "note",
]


class DialogueTurn(TypedDict, total=False):
    strategy_id: int
    turn: int
    from_agent: str
    to_agent: str
    intent: DialogueIntent
    payload: str
    ts: str  # ISO Z


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_BUF_MAX: int = 2000
_BUF: Deque[DialogueTurn] = deque(maxlen=_BUF_MAX)
_LOCK = threading.Lock()
# Per-strategy monotonic turn counter so the UI can render "Turn N" ordering
# even when timestamps tie within the same millisecond.
_TURN_COUNTER: Dict[int, int] = {}
# P15/D-M20 — last-touch map so we can GC turn counters for strategies that
# haven't been mentioned in 24h. Without this the dict grows monotonically over
# the process lifetime; in long-running deployments this is a slow leak.
_TURN_LAST_TOUCH: Dict[int, float] = {}
# D-L8/P16 — make the GC window configurable. Default bumped to 72h so a
# strategy that runs once over a long weekend doesn't have its turn counter
# reset between polls. Override via env when long-tail debugging.
_TURN_GC_MAX_AGE_SEC = (
    env_int("AGENT_DIALOGUE_GC_MAX_AGE_HOURS", 72, minimum=1, maximum=720) * 60 * 60
)

_PAYLOAD_CAP = 4096
_TRUNC_SUFFIX = "...[truncated]"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _cap_payload(text: str) -> str:
    s = str(text or "")
    if len(s) <= _PAYLOAD_CAP:
        return s
    return s[: _PAYLOAD_CAP - len(_TRUNC_SUFFIX)] + _TRUNC_SUFFIX


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_dialogue(
    *,
    strategy_id: int,
    from_agent: str,
    to_agent: str,
    intent: DialogueIntent,
    payload: str,
) -> DialogueTurn:
    """Append a single dialogue turn. Thread-safe.

    Returns the turn as stored (with monotonic ``turn`` index assigned).
    """
    # P15/D-L10 — defensive int coercion guard. If a caller passes a string-like
    # id, normalize quietly rather than crashing the orchestrator.
    try:
        sid_int = int(strategy_id)
    except (TypeError, ValueError):
        raise ValueError(f"strategy_id must be int-coercible, got {strategy_id!r}")
    if sid_int <= 0:
        raise ValueError(f"strategy_id must be a positive int, got {strategy_id!r}")
    strategy_id = sid_int
    with _LOCK:
        _TURN_COUNTER[strategy_id] = _TURN_COUNTER.get(strategy_id, 0) + 1
        # P15/D-M20 — refresh last-touch so the GC sees this strategy as live.
        _TURN_LAST_TOUCH[strategy_id] = time.monotonic()
        turn_idx = _TURN_COUNTER[strategy_id]
        entry: DialogueTurn = {
            "strategy_id": int(strategy_id),
            "turn": turn_idx,
            "from_agent": str(from_agent or "system"),
            "to_agent": str(to_agent or "broadcast"),
            "intent": intent if intent in {
                "question", "answer", "handoff", "critique",
                "approval", "veto", "note",
            } else "note",
            "payload": _cap_payload(payload),
            "ts": _utc_iso(),
        }
        _BUF.append(entry)
        return entry


def recent_dialogue(
    *,
    strategy_id: Optional[int] = None,
    limit: int = 200,
) -> List[DialogueTurn]:
    """Return the most recent dialogue turns (newest first).

    If ``strategy_id`` is provided, filters to that strategy only.
    ``limit`` is clamped to [1, 1000].
    """
    capped = max(1, min(1000, int(limit or 200)))
    with _LOCK:
        items = list(_BUF)
    if strategy_id is not None:
        sid = int(strategy_id)
        items = [t for t in items if int(t.get("strategy_id", 0)) == sid]
    # Newest first.
    items.sort(key=lambda t: (t.get("ts") or "", t.get("turn") or 0), reverse=True)
    return items[:capped]


def reset_for_strategy(strategy_id: int) -> int:
    """Drop all turns for one strategy and reset its turn counter.

    Returns the number of turns dropped. Used by orchestrator when a strategy
    is fully retried from scratch (rare). Safe to call on missing strategies.
    """
    sid = int(strategy_id)
    dropped = 0
    with _LOCK:
        kept: Deque[DialogueTurn] = deque(maxlen=_BUF_MAX)
        for t in _BUF:
            if int(t.get("strategy_id", 0)) == sid:
                dropped += 1
            else:
                kept.append(t)
        _BUF.clear()
        _BUF.extend(kept)
        _TURN_COUNTER.pop(sid, None)
        _TURN_LAST_TOUCH.pop(sid, None)
    return dropped


def buffer_size() -> int:
    with _LOCK:
        return len(_BUF)


def _gc_turn_counter() -> int:
    """P15/D-M20 — drop turn-counter entries for strategies untouched > 24h.

    Best-effort cleanup wired into ``periodic_tasks`` as a daily job. Returns
    the number of entries dropped so the periodic-task runner can log it.
    Monotonic: only drops entries older than the threshold; never touches
    active strategies (because record_dialogue refreshes their last-touch).
    """
    cutoff = time.monotonic() - _TURN_GC_MAX_AGE_SEC
    dropped = 0
    with _LOCK:
        stale = [sid for sid, ts in _TURN_LAST_TOUCH.items() if ts < cutoff]
        for sid in stale:
            _TURN_COUNTER.pop(sid, None)
            _TURN_LAST_TOUCH.pop(sid, None)
            dropped += 1
    return dropped


__all__ = [
    "DialogueIntent",
    "DialogueTurn",
    "record_dialogue",
    "recent_dialogue",
    "reset_for_strategy",
    "buffer_size",
    # Referenced by periodic_tasks as a scheduled daily GC job (P15/D-M20).
    # Listed here so maintainers know this function is an external contract
    # and must not be removed without updating periodic_tasks.py.
    "_gc_turn_counter",
]

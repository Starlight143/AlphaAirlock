"""Centralized .env loader.

Import this module FIRST in every backend entry point (`backend.app.main`,
`backend.test_pipeline`, any future CLI) so process-wide environment variables
are populated before any other backend module reads them.

Why a dedicated module:
- We can't rely on the shell to export keys; users frequently keep them in a
  project `.env` file that the FastAPI server never sees otherwise.
- Importing this early means `_client.py`, `database.py`, etc. all see the
  fully populated environment when they read `os.environ.get(...)` at import
  time (e.g. DEFAULT_MODEL evaluation).

Behaviour:
- Looks for `<project_root>/.env`. Project root is two parents up from this
  file (mirrors `backend/core/database.py:PROJECT_ROOT`).
- If python-dotenv is installed (it is, via requirements.txt), uses it.
- Falls back to a minimal hand-rolled parser if python-dotenv is missing, so
  the system still boots in stripped-down environments.
- NEVER overwrites variables that are already set in os.environ — explicit
  shell exports always win over the file. This matches dotenv's `override=False`
  default and keeps CI / Docker behaviour predictable.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ENV_PATH = _PROJECT_ROOT / ".env"


def _fallback_parse(path: Path) -> None:
    """Minimal KEY=VALUE parser used when python-dotenv is unavailable."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # Strip a single matching wrap of " or '
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_env_once() -> bool:
    """Load `.env` exactly once per process. Returns True if a file was found."""
    if os.environ.get("_ALPHA_ENV_LOADED") == "1":
        return True
    if not _ENV_PATH.exists():
        os.environ["_ALPHA_ENV_LOADED"] = "1"
        return False
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(dotenv_path=_ENV_PATH, override=False)
    except ImportError:
        _fallback_parse(_ENV_PATH)
    os.environ["_ALPHA_ENV_LOADED"] = "1"
    return True


# Eager load on import — that's the whole point of this module.
load_env_once()


# ---------------------------------------------------------------------------
# Typed env-var helpers (P6 — shared whitelist parsers per CLAUDE.md rule)
# ---------------------------------------------------------------------------
#
# Single source of truth for env-var parsing across the backend. Replaces the
# duplicated `_env_bool` blocks in scheduler.py, telegram_inbound.py, etc.
#
# Bool parsing follows the whitelist pattern: only documented tokens flip the
# value; anything else (including typos like "ture") returns the default. This
# prevents accidental "this looks truthy" outcomes.


_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})


def env_bool(name: str, default: bool) -> bool:
    """Parse env var as bool via strict whitelist. Typo → default, NOT True."""
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE_TOKENS:
        return True
    if raw in _FALSE_TOKENS:
        return False
    return default


def env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    """Parse env var as int with optional min/max clamping."""
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


def env_float(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    """Parse env var as float with optional min/max clamping.

    P24 — NaN guard: ``float("nan")`` is a *valid* Python parse so the
    ``except (TypeError, ValueError)`` branch does NOT catch it. Once a NaN
    leaks into ``value``, the min/max clamps below silently fail because
    IEEE 754 mandates that **every** comparison with NaN evaluates to
    ``False`` (including ``<`` and ``>``). The function would then return
    an unbounded NaN despite the caller specifying ``minimum=...`` /
    ``maximum=...``. Callers like ``backend/core/regime.py:env_float(
    'REGIME_BULL_PCT', 0.05, minimum=0.0, maximum=1.0)`` would propagate
    NaN into regime-detection percentile comparisons that are then
    silently rubber-stamped (see also critic.py's ``_metric_hard_fail``
    helper which uses the same NaN-as-hard-fail pattern).

    Treating NaN as "parse failed" → ``return default`` keeps existing
    semantics for every other input (None, empty, "nan" handled the same
    as malformed strings), and only affects the NaN edge case which is
    always operator misconfiguration anyway.
    """
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if math.isnan(value):
        return default
    if minimum is not None and value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


def env_str(name: str, default: str = "") -> str:
    """Trimmed string env var, falls back to default when unset or whitespace."""
    raw = str(os.environ.get(name, "") or "").strip()
    return raw if raw else default


def env_str_set(name: str, default: set[str] | None = None, *, separators: str = ",;") -> set[str]:
    """Parse comma/semicolon-separated env list into a trimmed set."""
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return set(default or [])
    parts: list[str] = [raw]
    for sep in separators:
        parts = [p for chunk in parts for p in chunk.split(sep)]
    return {p.strip() for p in parts if p.strip()}


# ---------------------------------------------------------------------------
# Secret / placeholder helpers (P15/D-H1)
# ---------------------------------------------------------------------------
#
# Single canonical placeholder list + secret reader. Previously this prefix
# tuple was duplicated in backend/agents/_client.py, telegram_notifier.py,
# telegram_inbound.py, discord_inbound.py — each with the same literal
# ("your_", "xxxx", "placeholder", "changeme"). Moving the source of truth
# here means future placeholder prefixes are added in one place.

_PLACEHOLDER_PREFIXES = ("your_", "xxxx", "placeholder", "changeme")


def is_real_secret(value: str | None) -> bool:
    """Return True iff value is a usable secret (non-empty + not a placeholder)."""
    if not value:
        return False
    s = str(value).strip()
    if not s:
        return False
    return not s.lower().startswith(_PLACEHOLDER_PREFIXES)


def env_secret_or_none(name: str) -> str | None:
    """Read env var as a real secret; return None when unset or a placeholder."""
    raw = (os.environ.get(name) or "").strip()
    return raw if is_real_secret(raw) else None


__all__ = [
    "load_env_once",
    "env_bool",
    "env_int",
    "env_float",
    "env_str",
    "env_str_set",
    "is_real_secret",
    "env_secret_or_none",
]

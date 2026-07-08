"""Shared pytest bootstrap for the backend unit-test suite.

This file is auto-loaded by pytest before any test module is collected. It
mirrors the import bootstrap used by ``backend/test_pipeline.py`` so the unit
tests can import production modules via the canonical ``backend.core.*`` /
``backend.agents.*`` paths regardless of the directory pytest is invoked from.

It is intentionally side-effect-light:
  * Puts the repository root AND the ``backend`` package dir on ``sys.path`` so
    both ``import backend.core.x`` and ``import core.x`` style imports resolve.
  * Loads ``backend._envloader`` so any module that reads ``os.environ`` at
    import time sees the same values as the running app. Loading is wrapped in a
    try/except: a missing or unreadable ``.env`` must never break unit tests.
  * Forces ``PYTHONHASHSEED``-independent, network-free defaults by pointing the
    LLM budget / provider env at safe placeholders ONLY IF they are unset, so a
    developer's real keys are never overwritten.

No test data, DB, or network connection is created here — each test module is
responsible for its own fixtures (temp SQLite path, in-memory state, etc.).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# backend/tests/conftest.py -> parents[0]=tests, [1]=backend, [2]=repo root
_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "backend"

for _p in (str(_ROOT), str(_BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Load .env exactly like test_pipeline.py does, but never fail the suite if the
# loader or file is missing.
try:  # pragma: no cover - bootstrap convenience
    from backend import _envloader  # noqa: F401  (side-effect import)
except Exception:  # noqa: BLE001 - env load is best-effort for unit tests
    pass

# Safe, non-secret defaults so modules that *require* an env var at import time
# do not crash during collection. Only set when absent (never clobber real keys).
_SAFE_DEFAULTS = {
    "LLM_PROVIDER": "anthropic",
    "ALPHA_LLM_DAILY_USD_CAP": "0",  # 0 => disabled cap; tests set their own
}
for _k, _v in _SAFE_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

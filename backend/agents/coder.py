"""Stage 2 — Coder Agent.

Converts an Alpha Story (plain-English thesis) into executable Pandas factor
code conforming to the exact `compute_factor(df) -> pd.Series` interface.
"""

from __future__ import annotations

import ast
from typing import Optional

from backend._envloader import env_int
from backend.agents._client import call_messages, extract_code_block


class CoderValidationError(ValueError):
    """Raised when generated factor code fails structural validation."""


def _coder_format_retry_max() -> int:
    """T1-C — number of EXTRA coder re-prompts on parse/AST-validation failure,
    beyond the first attempt. ``0`` (default) = today's behaviour (raise on first
    invalid output). Hard-clamped to [0, 3] so a persistently malformed coder
    can never loop unboundedly."""
    return env_int("CODER_FORMAT_RETRY_MAX", 0, minimum=0, maximum=3)


# P29-T6: keep in sync with sandbox._ALLOWED_IMPORTS.
_ALLOWED_IMPORT_MODULES = frozenset({"pandas", "numpy", "math", "statistics"})


def _validate_coder_output(code: str) -> None:
    """Static-validate the coder agent's output.

    - Code is syntactically valid Python.
    - Exactly one top-level ``def compute_factor`` exists.
    - All top-level imports refer to whitelisted modules.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise CoderValidationError(f"coder output is not valid Python: {exc}") from exc

    fn_defs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    compute_defs = [n for n in fn_defs if n.name == "compute_factor"]
    if len(compute_defs) != 1:
        raise CoderValidationError(
            f"expected exactly one top-level `compute_factor` function, found {len(compute_defs)}"
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = (alias.name or "").split(".")[0]
                if root and root not in _ALLOWED_IMPORT_MODULES:
                    raise CoderValidationError(
                        f"disallowed import '{alias.name}' (allowed: {sorted(_ALLOWED_IMPORT_MODULES)})"
                    )
        elif isinstance(node, ast.ImportFrom):
            # B10-2: reject relative imports (from . import X / from .. import Y).
            # For these, node.level >= 1 and node.module may be None, which causes
            # (node.module or "").split(".")[0] == "" — an empty string that silently
            # passes the `if root and ...` guard below.  Catch them explicitly first.
            if node.level and node.level > 0:
                dots = "." * node.level
                raise CoderValidationError(
                    f"relative import not allowed (from {dots}{node.module or ''} import ...)"
                )
            root = (node.module or "").split(".")[0]
            if root and root not in _ALLOWED_IMPORT_MODULES:
                raise CoderValidationError(
                    f"disallowed import-from '{node.module}' (allowed: {sorted(_ALLOWED_IMPORT_MODULES)})"
                )

CODER_SYSTEM_PROMPT = """You are the Coder Agent of an Agentic Alpha Research System.
You translate a plain-English Alpha Story into executable Pandas factor code.

STRICT OUTPUT CONTRACT — failure to comply blocks the entire pipeline:
- Reply with EXACTLY ONE python code block (```python ... ```) and no prose
  before or after.
- The code must define exactly this function signature:

    def compute_factor(df: pd.DataFrame) -> pd.Series:
        ...

- You MAY use only these imports (already provided in the sandbox globals):
    import pandas as pd
    import numpy as np
- You MUST reference columns using lowercase keys only:
    df['close'], df['open'], df['high'], df['low'], df['volume'],
    df['open_interest'], df['funding_rate'], df['liquidations']
- You MUST return a pandas Series whose index equals df.index.
- You MUST NOT use any file I/O, subprocess, network, eval/exec, importlib,
  attribute introspection (__class__, __globals__, etc.), or any unlisted
  imports. Anything outside pandas/numpy/math is forbidden.
- You MUST avoid look-ahead bias. Only use information available at or before
  each timestamp. Forward-fills, .shift(-1), or .iloc[i+...] are forbidden.
- Always handle NaNs/Inf with .fillna(0) or .replace([np.inf,-np.inf], 0) at
  the end so the sandbox normalization can convert the factor to a signal.

The returned factor is a continuous numerical Series; the downstream pipeline
will normalize it into {-1, 0, +1} positions via a rolling z-score band."""


CODER_RETRY_SUFFIX = """\n\nThe previous attempt was rejected by the Risk Critic. Below is the
adversarial review. Refactor the factor to address EVERY listed concern
without violating the strict output contract above:

---
{critique}
---"""


# T1-C — appended when a previous attempt failed PARSE/AST validation (a
# different failure axis from the Critic's No-Go above). Re-states the contract
# and shows the exact error so the model can self-correct before backtest.
_CODER_FORMAT_RETRY_SUFFIX = """\n\nYour previous attempt was REJECTED before backtest because the output did
not satisfy the strict contract. Error:
---
{error}
---
Re-emit the corrected code. Reply with EXACTLY ONE ```python ...``` block
defining compute_factor(df) and nothing else."""


def generate_factor_code(
    alpha_story: str,
    *,
    critique: Optional[str] = None,
    max_tokens: int = 1800,
) -> str:
    """Return a fenced-stripped python source for compute_factor(df).

    If `critique` is provided, the prompt appends the Risk Critic feedback so
    the agent can fix the previous attempt (Mission 5 retry loop, attempt #2).
    """
    if not alpha_story or not alpha_story.strip():
        raise ValueError("alpha_story is empty")

    base_user = (
        "Translate the following Alpha Story into Pandas factor code.\n\n"
        f"<<<ALPHA_STORY>>>\n{alpha_story.strip()}\n<<<END>>>"
    )
    if critique and critique.strip():
        base_user += CODER_RETRY_SUFFIX.format(critique=critique.strip())

    # T1-C — bounded retry on PARSE/AST-validation failure. When
    # CODER_FORMAT_RETRY_MAX=0 (default) this loop runs exactly once and raises
    # the identical exception type as before, byte-for-byte today's behaviour.
    # The retry composes with the orchestrator's Critic-feedback loop without
    # double-counting: the orchestrator only ever observes a valid code string
    # or a terminal exception. An LLMBudgetExceededError (RuntimeError) is NOT
    # caught here, so it propagates straight to the orchestrator's reject sink.
    retries = _coder_format_retry_max()
    last_err: Optional[Exception] = None
    for _fmt_attempt in range(retries + 1):
        user = base_user
        if last_err is not None:
            user = base_user + _CODER_FORMAT_RETRY_SUFFIX.format(error=str(last_err)[:600])
        raw = call_messages(
            system=CODER_SYSTEM_PROMPT,
            user=user,
            max_tokens=max_tokens,
            temperature=0.15,
            agent="coder",  # P13/D-L2 — attribute LLM spend per agent for budget telemetry
        )
        try:
            code = extract_code_block(raw, language="python")
            if "def compute_factor" not in code:
                # Defensive: occasionally Claude drops the fence; raw might be valid.
                if "def compute_factor" in raw:
                    code = raw.strip()
                else:
                    raise ValueError(
                        "Coder agent failed to produce compute_factor(df). "
                        f"Raw response head: {raw[:300]}"
                    )
            # P29-T6: structural validation (AST). Catches duplicate defs and
            # disallowed imports before the sandbox layer sees the code.
            _validate_coder_output(code)
            return code
        except (ValueError, CoderValidationError) as exc:
            # CoderValidationError IS-A ValueError; listed explicitly for clarity.
            last_err = exc
            if _fmt_attempt >= retries:
                raise
    # Unreachable: the loop always returns a valid code or re-raises on the
    # final iteration. Present only to satisfy static analysers.
    raise last_err if last_err is not None else RuntimeError("coder retry loop exited unexpectedly")


__all__ = [
    "generate_factor_code",
    "CODER_SYSTEM_PROMPT",
    "CoderValidationError",
    "_validate_coder_output",
]

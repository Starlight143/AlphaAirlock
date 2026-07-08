"""End-to-end integration test for the Agentic Alpha Research System.

Bypasses the browser. Drives a real LLM-backed pipeline and asserts:
- ANTHROPIC_API_KEY is present (otherwise hard-exits with code 1).
- A KnowledgeNode row appears in SQLite.
- The Coder agent's output executes inside the sandbox without traceback.
- A strategy_<id>.json equity-curve report lands in storage/results/.

Usage:
    python backend/test_pipeline.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Allow `python backend/test_pipeline.py` from the repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load .env BEFORE any backend module reads os.environ.
from backend import _envloader  # noqa: F401,E402  - keep first

from backend.core.database import (  # noqa: E402  (path bootstrap above)
    AlphaStrategy,
    KnowledgeNode,
    init_db,
    session_scope,
)
from backend.core.orchestrator import (  # noqa: E402
    RESULTS_DIR,
    WorkflowOrchestrator,
)

SEED_TEXT = (
    "BTC perpetual funding rates printed -0.18% on two consecutive 8h windows "
    "while open interest hit a 6-month high above $24B. Long liquidations "
    "spiked to $420M in a single 4h candle. Historically when funding flips "
    "this deeply negative immediately after a parabolic OI buildup, BTC mean-"
    "reverts +3 to +5% over the following 12-36 hours as crowded longs are "
    "flushed and shorts cover. Liquidations volume above the 95th percentile "
    "while funding sits in the bottom decile has produced a >65% hit rate on "
    "12h forward returns over the past 18 months."
)


GREEN = "\033[1;32m"
RED = "\033[1;31m"
YELLOW = "\033[1;33m"
RESET = "\033[0m"


def banner(msg: str, color: str = GREEN) -> None:
    bar = "=" * 78
    print(f"{color}{bar}\n {msg}\n{bar}{RESET}")


def fail(msg: str) -> None:
    print(f"{RED}[FAIL] {msg}{RESET}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"{GREEN}[ OK ] {msg}{RESET}")


def main() -> int:
    banner("Agentic Alpha - End-to-End Pipeline Verification")

    # ---- Pre-flight ----
    from backend.agents._client import describe_provider_config  # local import: avoids LLM init at module load
    llm_cfg = describe_provider_config()
    if llm_cfg.get("error"):
        print(f"{RED}\n>>> {llm_cfg['error']}{RESET}\n", file=sys.stderr)
        sys.exit(1)

    resolved = llm_cfg.get("resolved") or "?"
    key_env = llm_cfg.get("key_env_var") or "?"
    if not llm_cfg.get("configured"):
        print(
            f"{RED}\n>>> {key_env} is missing or a placeholder "
            f"(LLM_PROVIDER resolved to '{resolved}').{RESET}\n"
            f"{YELLOW}Set the variable before running this test:{RESET}\n"
            f"  PowerShell:  $env:{key_env} = '<your-key>'\n"
            f"  bash:        export {key_env}=<your-key>\n"
            f"{YELLOW}Or switch providers via:{RESET}\n"
            f"  PowerShell:  $env:LLM_PROVIDER = 'openrouter'\n"
            f"  bash:        export LLM_PROVIDER=openrouter\n",
            file=sys.stderr,
        )
        sys.exit(1)
    ok(f"LLM provider: {resolved} | model: {llm_cfg.get('model')} | key env: {key_env}")

    init_db()
    ok("Database initialized")

    # Snapshot existing record counts so we can assert a new node was added.
    with session_scope() as s:
        kn_before = s.query(KnowledgeNode).count()
        as_before = s.query(AlphaStrategy).count()
    ok(f"Pre-run counts: KnowledgeNode={kn_before} AlphaStrategy={as_before}")

    # ---- Execute pipeline ----
    started = time.monotonic()
    orchestrator = WorkflowOrchestrator()
    result = orchestrator.run_full_pipeline(SEED_TEXT)
    elapsed = time.monotonic() - started
    sid = int(result.get("strategy_id", 0))
    status = str(result.get("status", "UNKNOWN"))
    ok(f"Pipeline finished sid={sid} status={status} in {elapsed:.1f}s")
    assert isinstance(elapsed, float) and elapsed > 0.0, (
        f"time.monotonic() returned unexpected value: {elapsed!r}"
    )
    if elapsed > 300.0:
        fail(f"Pipeline took {elapsed:.1f}s — likely hung or LLM call stalled (limit: 300s)")

    # ---- Assertions ----
    with session_scope() as s:
        kn_after = s.query(KnowledgeNode).count()
        as_after = s.query(AlphaStrategy).count()
        strategy = s.get(AlphaStrategy, sid)

    if kn_after <= kn_before:
        fail(f"No KnowledgeNode rows were added (before={kn_before} after={kn_after})")
    ok(f"KnowledgeNode rows: {kn_before} -> {kn_after}")

    if strategy is None:
        fail(f"AlphaStrategy id={sid} missing from database")
    ok(f"AlphaStrategy row persisted (id={sid}, status={strategy.status})")

    if not (strategy.formula_code or "").strip():
        fail("AlphaStrategy.formula_code is empty — Coder agent did not produce code")
    if "def compute_factor" not in (strategy.formula_code or ""):
        fail("formula_code missing required compute_factor(df) signature")
    ok(f"Coder agent produced compute_factor() ({len(strategy.formula_code)} chars)")

    # ---- Sandbox executability check (as documented in the module docstring) ----
    # Execute the formula_code through safe_execute_factor so a runtime crash
    # (wrong column name, division by zero, bad import) is caught here, not silently.
    try:
        from backend.core.data_gen import load_synthetic_btc  # local import: avoids heavy init
        from backend.core.sandbox import safe_execute_factor, SandboxExecutionError, SandboxValidationError
        import pandas as pd
        df_test = load_synthetic_btc()
        sandbox_result = safe_execute_factor(strategy.formula_code, df_test)
        if not isinstance(sandbox_result.signal, pd.Series):
            fail("sandbox returned non-Series signal — formula_code output type is wrong")
        if len(sandbox_result.signal) != len(df_test):
            fail(
                f"sandbox signal length {len(sandbox_result.signal)} != "
                f"df length {len(df_test)} — formula_code produced misaligned output"
            )
        ok(f"Sandbox execution passed — signal length {len(sandbox_result.signal)} rows")
    except (SandboxExecutionError, SandboxValidationError) as exc:
        fail(f"formula_code crashed inside sandbox: {exc}")
    except ImportError as exc:
        # Non-fatal: sandbox or data_gen not available in this environment.
        # Log a warning rather than hard-failing so CI without heavy deps still runs.
        print(f"{YELLOW}[WARN] Sandbox check skipped (import error): {exc}{RESET}")

    results_path = RESULTS_DIR / f"strategy_{sid}.json"
    if not results_path.exists():
        fail(f"Expected backtest results file missing: {results_path}")
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    equity_curve = payload.get("equity_curve", [])
    if not isinstance(equity_curve, list) or len(equity_curve) < 5:
        fail(f"equity_curve too short ({len(equity_curve)} pts) in {results_path}")
    ok(f"Equity-curve report saved with {len(equity_curve)} daily points")

    metrics = payload.get("metrics", {})
    if not metrics:
        fail("Backtest metrics block is empty")
    ok("Backtest metrics:")
    for k, v in metrics.items():
        print(f"        {k:25s} = {v}")

    if status not in {"APPROVED", "REJECTED"}:
        fail(f"Pipeline ended in non-terminal status: {status}")
    ok(f"Final pipeline status: {status} (terminal)")

    # Verify DB-level status is consistent with the pipeline return value.
    # This catches regressions where the orchestrator returns REJECTED but
    # fails to persist that status on the AlphaStrategy row (or vice versa).
    with session_scope() as s:
        db_strategy = s.get(AlphaStrategy, sid)
    if db_strategy is None:
        fail(f"AlphaStrategy id={sid} missing from DB after status check (sid={sid})")
    if str(db_strategy.status) != status:
        fail(
            f"Pipeline returned status={status!r} but DB row has "
            f"strategy.status={db_strategy.status!r} — orchestrator did not persist terminal status"
        )
    ok(f"DB strategy.status consistent with pipeline return: {db_strategy.status}")

    banner("Mission 7 end-to-end test PASSED", color=GREEN)
    print(
        f"""{GREEN}
+-----------------------------------------------+
|   STATUS GRID — agentic alpha pipeline OK     |
+-----------------------------------------------+
|  strategy_id : {sid:<31}|
|  status      : {status:<31}|
|  elapsed_s   : {elapsed:<31.1f}|
|  knowledge   : {kn_before} -> {kn_after}{' ':22}|
|  results_path: {str(results_path)[-31:]:>31}|
+-----------------------------------------------+
{RESET}"""
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

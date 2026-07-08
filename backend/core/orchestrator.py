"""Stage 0 -> Stage 4 Workflow Orchestrator.

Drives the end-to-end pipeline:
    INTAKE -> STORY_GEN -> CODE_GEN -> BACKTESTING -> CRITIC_LOOP
        -> (Go) APPROVED  |  (No-Go after 1 retry) REJECTED

Persists every transition to SQLite via the AlphaStrategy table and keeps a
ring-buffer of terminal logs in an in-process registry so the
GET /api/pipeline/status/{strategy_id} endpoint can stream them to the UI.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from backend.agents.coder import generate_factor_code
from backend.agents.critic import review_strategy
from backend.agents.intake import process_text_to_node
from backend.agents.researcher import generate_alpha_story, revise_alpha_story
from backend.core.agent_dialogue import record_dialogue
from backend.core.database import (
    KB_CONTENT_MAX_CHARS,
    AlphaStrategy,
    KnowledgeNode,
    PROJECT_ROOT,
    session_scope,
)
from backend.core.engine import AlphaBacktester
from backend.core import bootstrap_metrics, diversity, regime_metrics, risk_metrics, strategy_gates
from backend.core.sandbox import (
    SandboxExecutionError,
    SandboxValidationError,
    safe_execute_factor,
)

logger = logging.getLogger("alpha.orchestrator")

RESULTS_DIR: Path = PROJECT_ROOT / "storage" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
ERROR_LOG: Path = PROJECT_ROOT / "pipeline_errors.log"
DATA_CSV: Path = PROJECT_ROOT / "backend" / "data" / "synthetic_btc.csv"


def _maybe_notify_transition(
    strategy_id: int,
    *,
    name: str,
    from_status: str,
    to_status: str,
    metrics: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort Telegram outbound. Never raises."""
    try:
        from backend.core.telegram_notifier import notify_strategy_transition
        notify_strategy_transition(
            strategy_id,
            name=name,
            from_status=from_status,
            to_status=to_status,
            metrics=metrics,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Telegram notification failed (non-fatal)")
    # P-DISCORD-OUT — parallel best-effort Discord post (off by default).
    try:
        from backend.core.discord_notifier import (
            notify_strategy_transition as _discord_transition,
        )
        _discord_transition(
            strategy_id,
            name=name,
            from_status=from_status,
            to_status=to_status,
            metrics=metrics,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Discord notification failed (non-fatal)")


def _maybe_create_postmortem_node(
    strategy_id: int,
    *,
    name: str,
    verdict: str,
    critique_markdown: str,
    metrics: Dict[str, Any],
    source_node_ids: List[int],
    soul_questions: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist the critic verdict as a postmortem KnowledgeNode that links back
    to the originating research nodes. Fires for both APPROVED and REJECTED.

    P6-D05: also marks every source KnowledgeNode referenced by the strategy
    with ``kind = KIND_PAST_ALPHA`` so the factor-network graph paints those
    nodes red (matching the reference "曾經做過的 Alpha" demo). Postmortem
    nodes themselves are not overwritten — postmortem takes priority.
    """
    try:
        from datetime import datetime, timezone
        from sqlalchemy import func
        from backend.core.database import (
            KIND_PAST_ALPHA,
            KIND_POSTMORTEM,
            KnowledgeNode,
            session_scope as _scope,
        )
        title = f"Postmortem: S#{strategy_id} {name[:120]} ({verdict})"
        body_parts = [
            f"# {title}",
            "",
            f"**Verdict**: `{verdict}`",
            "",
            "## Metrics",
            "```",
            "\n".join(f"{k}: {v}" for k, v in metrics.items()),
            "```",
            "",
            "## Critic Review",
            critique_markdown.strip() or "(empty)",
        ]
        if soul_questions:
            try:
                sq_json = json.dumps(dict(soul_questions), indent=2, ensure_ascii=False)
            except (TypeError, ValueError):
                sq_json = json.dumps({"_error": "non-serializable soul_questions"})
            body_parts.extend([
                "",
                "## Soul Questions (structured)",
                "```json",
                sq_json,
                "```",
            ])
        content = "\n".join(body_parts)
        clean_source_ids = [int(x) for x in source_node_ids if isinstance(x, int)]
        with _scope() as s:
            node = KnowledgeNode(
                title=title[:512],
                content=content[:KB_CONTENT_MAX_CHARS],
                tags="postmortem,critic",
                links=json.dumps(clean_source_ids),
                ic_score=0.0,
                kind=KIND_POSTMORTEM,
                category="postmortem",
                origin_strategy_id=strategy_id,
                ingested_at=datetime.now(timezone.utc),
            )
            s.add(node)
            s.flush()
            # P6-D05: upgrade source nodes to past_alpha so they render in red.
            # Also stamp origin_strategy_id on the (non-postmortem) source nodes
            # so backend.core.genealogy._load_parent_map can resolve the
            # parent->child edge (the genealogy forest reads origin_strategy_id
            # off the past_alpha source node, not off the postmortem node).
            # COALESCE preserves any previously-stamped owner so the FIRST
            # strategy that consumed the node remains its genealogical parent.
            if clean_source_ids:
                (
                    s.query(KnowledgeNode)
                    .filter(KnowledgeNode.id.in_(clean_source_ids))
                    .filter(KnowledgeNode.kind != KIND_POSTMORTEM)
                    .update(
                        {
                            "kind": KIND_PAST_ALPHA,
                            "origin_strategy_id": func.coalesce(
                                KnowledgeNode.origin_strategy_id, strategy_id
                            ),
                        },
                        synchronize_session=False,
                    )
                )
            logger.info(
                "Postmortem node #%s created for strategy %s (verdict=%s, source_nodes=%s)",
                node.id, strategy_id, verdict, clean_source_ids,
            )
    except Exception:  # noqa: BLE001
        logger.exception("Postmortem node creation failed (non-fatal)")


def _finalize_rejection(
    strategy_id: int,
    state: "RunState",
    *,
    reason: str,
    factor_code: Optional[str],
    alpha_story: str,
    source_node_ids: List[int],
    metrics: Optional[Dict[str, Any]] = None,
    soul_questions: Optional[Dict[str, Any]] = None,
) -> None:
    """P11-B-01 - single sink for every REJECTED exit (Coder/Sandbox/retry-exhaust).

    Always emits: (a) postmortem KnowledgeNode, (b) Telegram transition notify,
    (c) IC ledger negative observation against source nodes (P11-B-02). All
    side-effects are best-effort and never raise.

    P15/D-H7 — accepts optional ``soul_questions`` so the retry-exhaust call
    sites (which previously hand-rolled their own postmortem call to preserve
    structured soul_questions data) can collapse into this single sink without
    losing that field. Default ``None`` keeps existing callers behaviour.
    """
    try:
        name = alpha_story.split("\n", 1)[0][:160] if alpha_story else f"strategy_{strategy_id}"
    except Exception:  # noqa: BLE001
        name = f"strategy_{strategy_id}"
    metrics_clean: Dict[str, Any] = dict(metrics or {})

    _maybe_create_postmortem_node(
        strategy_id,
        name=name,
        verdict="REJECTED",
        critique_markdown=reason or "(no reason captured)",
        metrics=metrics_clean,
        source_node_ids=list(source_node_ids or []),
        soul_questions=soul_questions,
    )
    _maybe_notify_transition(
        strategy_id,
        name=name[:80],
        from_status=state.status.value if state and state.status else "UNKNOWN",
        to_status="REJECTED",
        metrics=metrics_clean,
    )
    # P11-B-02 - record mild negative IC observation against every source node.
    try:
        from backend.core.ic_history import record_pipeline_outcomes
        sharpe = 0.0
        try:
            sharpe = float(metrics_clean.get("annualized_sharpe", 0.0) or 0.0)
        except (TypeError, ValueError):
            sharpe = 0.0
        # Formula: rejected -> mild negative observation, never below -1.
        ic_value = max(-1.0, min(0.0, sharpe * 0.05))
        record_pipeline_outcomes(
            source_node_ids,
            strategy_id=int(strategy_id),
            ic_value=float(ic_value),
        )
    except Exception:  # noqa: BLE001
        logger.exception("IC ledger write (rejection) failed (non-fatal)")


class PipelineStatus(str, Enum):
    """Lifecycle statuses spanning Stage 0 through post-deployment.

    The orchestrator's `run_full_pipeline()` only transitions automatically
    through INTAKE -> ... -> APPROVED/REJECTED. The post-approval transitions
    (SMALL_CAPITAL, LIVE, GRAVEYARD) are operator-driven via dedicated promote
    / retire endpoints — never auto-promoted by the pipeline.
    """

    INTAKE = "INTAKE"
    STORY_GEN = "STORY_GEN"
    CODE_GEN = "CODE_GEN"
    BACKTESTING = "BACKTESTING"
    CRITIC_LOOP = "CRITIC_LOOP"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    # Operator-promoted statuses (post P0 plumbing; UI exists from P0, write APIs land in P4):
    PAPER_TRADE = "PAPER_TRADE"
    SMALL_CAPITAL = "SMALL_CAPITAL"
    LIVE = "LIVE"
    GRAVEYARD = "GRAVEYARD"
    # P7 — emergency-stop sink for /live-trade pause-all. Mirrors GRAVEYARD's
    # stage rank (7) so it appears in the Graveyard bucket on Mission Control
    # but is distinct enough that operators can /api/live-trade/resume it.
    PAUSED = "PAUSED"


# Bucket index used by the Mission Control pipeline pills and the strategy
# stepper. Reference convention (from the original demo):
#   0 Alpha Ideas | 1 Research | 2 Factor Dev | 3 Full Backtest |
#   4 Paper Trade | 5 Small Capital | 6 Live | 7 Graveyard
# `REJECTED` is mapped to Graveyard so failed candidates render in the same
# bucket as retired post-production strategies — visually consistent with the
# reference.
STAGE_FOR_STATUS: Dict[PipelineStatus, int] = {
    PipelineStatus.INTAKE: 0,
    PipelineStatus.STORY_GEN: 1,
    PipelineStatus.CODE_GEN: 2,
    PipelineStatus.BACKTESTING: 3,
    PipelineStatus.CRITIC_LOOP: 3,
    PipelineStatus.APPROVED: 4,
    PipelineStatus.PAPER_TRADE: 4,
    PipelineStatus.SMALL_CAPITAL: 5,
    PipelineStatus.LIVE: 6,
    # P15/D-L7 — REJECTED and GRAVEYARD intentionally share stage=7 (and PAUSED
    # also lands there). Mission Control's Graveyard bucket renders all three
    # together so retired post-production strategies + failed candidates +
    # operator-paused strategies all show up in the same "not currently active"
    # column. The status string still disambiguates them in the detail view.
    PipelineStatus.REJECTED: 7,
    PipelineStatus.GRAVEYARD: 7,
    PipelineStatus.PAUSED: 7,
}


# Terminal statuses for the automated pipeline (i.e. `run_full_pipeline()` will
# never transition past these without explicit operator action). Used by both
# the status endpoint and the frontend pipeline poller.
TERMINAL_PIPELINE_STATUSES: frozenset[PipelineStatus] = frozenset({
    PipelineStatus.APPROVED,
    PipelineStatus.REJECTED,
})


# All operator-driven post-approval transitions. Reserved for promote/retire
# endpoints introduced in P4 — exposed here in P0 so the frontend can render
# accurate stepper colors today.
POST_APPROVAL_STATUSES: frozenset[PipelineStatus] = frozenset({
    PipelineStatus.PAPER_TRADE,
    PipelineStatus.SMALL_CAPITAL,
    PipelineStatus.LIVE,
    PipelineStatus.GRAVEYARD,
})

# P31-STATE3 guard set: union of terminal + post-approval statuses plus PAUSED.
# Defined once at module level so that adding a new member to either source set
# automatically extends the guard — no risk of the inline frozenset drifting.
_LOCKED_STATUSES: frozenset[str] = (
    frozenset(s.value for s in TERMINAL_PIPELINE_STATUSES | POST_APPROVAL_STATUSES)
    | {PipelineStatus.PAUSED.value}
)


@dataclass
class RunState:
    strategy_id: int
    status: PipelineStatus = PipelineStatus.INTAKE
    active_agent: str = "intake"
    started_at: float = field(default_factory=time.monotonic)
    logs: List[str] = field(default_factory=list)


# Module-level concurrency-safe registry of in-flight pipelines, keyed by id.
# P15/D-M23 — bounded LRU so operators kicking off thousands of pipelines
# without restarting the process can't leak unlimited RunState objects. 2000
# entries is large enough for any realistic in-flight count; least-recently
# used entries fall out the back. Use _registry_put / _registry_get helpers
# so the LRU touch is consistent across call sites.
_REGISTRY_MAX_ENTRIES = 2000
_REGISTRY: "OrderedDict[int, RunState]" = OrderedDict()
_REGISTRY_LOCK = threading.Lock()


def _registry_put(strategy_id: int, state: RunState) -> None:
    """Insert / refresh a RunState entry; evict oldest if at cap."""
    with _REGISTRY_LOCK:
        if strategy_id in _REGISTRY:
            _REGISTRY.move_to_end(strategy_id)
        _REGISTRY[strategy_id] = state
        while len(_REGISTRY) > _REGISTRY_MAX_ENTRIES:
            _REGISTRY.popitem(last=False)


def _registry_get(strategy_id: int) -> Optional[RunState]:
    """Look up + refresh recency in one atomic step."""
    with _REGISTRY_LOCK:
        state = _REGISTRY.get(strategy_id)
        if state is not None:
            _REGISTRY.move_to_end(strategy_id)
        return state


# ---------------------------------------------------------------------------
# Orphaned-pipeline reaper
# ---------------------------------------------------------------------------
#
# A strategy is "orphaned" when its in-process pipeline worker died WITHOUT
# running the orchestrator's own REJECT handler — e.g. the process was
# hard-killed or frozen (macOS sleep / uvicorn --reload / OOM / kill -9) while
# the run was mid-flight. The in-memory `_REGISTRY` of live runs is lost on
# restart, so such rows sit forever in INTAKE/STORY_GEN/… with no worker
# advancing them — the Mission Control "stuck at Stage 0, never continues"
# symptom. `auto_pipeline._flush_orphan_strategy` only covers the *caught
# exception* path (worker still alive); nothing covers a dead worker, hence
# this periodic sweep.

# Non-terminal pipeline statuses the reaper may flush. Terminal + operator-
# driven statuses (APPROVED / PAPER_TRADE / SMALL_CAPITAL / LIVE / REJECTED /
# GRAVEYARD / PAUSED) are deliberately excluded so the reaper can NEVER touch a
# promoted or already-finished strategy.
_REAPABLE_STATUSES: frozenset[str] = frozenset({
    PipelineStatus.INTAKE.value,
    PipelineStatus.STORY_GEN.value,
    PipelineStatus.CODE_GEN.value,
    PipelineStatus.BACKTESTING.value,
    PipelineStatus.CRITIC_LOOP.value,
})


def _orphan_reap_stale_minutes() -> int:
    """Age (minutes) past which a non-terminal strategy with no live run is
    treated as an interrupted orphan. Clamped to a 15-min floor so a
    legitimately slow in-flight run (each transition bumps ``updated_at``) is
    never reaped. Tunable via ``PIPELINE_ORPHAN_REAP_STALE_MINUTES``."""
    import os
    raw = str(os.environ.get("PIPELINE_ORPHAN_REAP_STALE_MINUTES", "30") or "30").strip()
    try:
        v = int(raw)
    except (TypeError, ValueError):
        v = 30
    return max(15, min(v, 24 * 60))  # 15 min .. 24 h


def reap_orphaned_pipelines(*, stale_minutes: Optional[int] = None) -> int:
    """Flush strategies orphaned in a non-terminal pipeline stage to REJECTED.

    Recovery policy = FLUSH (consistent with
    ``auto_pipeline._flush_orphan_strategy``), NOT resume: the KB-node
    reservation sweep already re-releases an orphan's source nodes after
    15-30 min, so a genuinely good idea is re-evaluated as a fresh strategy.
    Resuming the empty shell here would only duplicate that work and re-spend
    LLM budget.

    Safe by construction:
      * only ``_REAPABLE_STATUSES`` are candidates (never APPROVED/LIVE/…);
      * a strategy with a live run in THIS process (``_REGISTRY``) is skipped;
      * only rows untouched for > ``stale_minutes`` are eligible;
      * each flush is its own short transaction with a terminal-state re-check,
        so it can't clobber a late-arriving real transition.

    Never raises — returns the number of strategies flushed. Designed for the
    periodic-task runner (called with no args; reads its threshold from env).
    """
    from datetime import timedelta
    from backend.core.transition_log import record_transition

    mins = int(stale_minutes if stale_minutes is not None else _orphan_reap_stale_minutes())
    mins = max(15, min(mins, 24 * 60))
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=mins)
    rej_stage = STAGE_FOR_STATUS[PipelineStatus.REJECTED]

    def _stale(ts) -> bool:
        """True when timestamp ts is None or strictly older than the cutoff.
        SQLite returns naive datetimes for tz-aware columns — re-attach UTC."""
        if ts is None:
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts < cutoff

    reaped = 0
    try:
        # 1) Snapshot candidate ids (short read txn) so the write txns stay tiny.
        with session_scope() as s:
            rows = (
                s.query(AlphaStrategy.id, AlphaStrategy.updated_at)
                .filter(AlphaStrategy.status.in_(tuple(_REAPABLE_STATUSES)))
                .all()
            )
        candidates: List[int] = []
        for sid, updated_at in rows:
            if not _stale(updated_at):
                continue  # touched recently — a live or just-finished run
            with _REGISTRY_LOCK:
                if int(sid) in _REGISTRY:
                    continue  # a live run in this process owns it
            candidates.append(int(sid))

        # 2) Flush each candidate in its own short transaction with a re-check.
        for sid in candidates:
            try:
                with session_scope() as s:
                    row = s.get(AlphaStrategy, sid)
                    if row is None or row.status not in _REAPABLE_STATUSES:
                        continue  # raced a real transition / promotion — leave it
                    if not _stale(row.updated_at):
                        continue
                    with _REGISTRY_LOCK:
                        if sid in _REGISTRY:
                            continue
                    prior = row.status
                    prior_stage = int(row.stage or 0)
                    row.status = PipelineStatus.REJECTED.value
                    row.stage = rej_stage
                    note = (
                        f"Auto-rejected by orphan reaper: stuck in {prior} with no "
                        f"active pipeline run for > {mins} min (worker interrupted — "
                        f"process restart / freeze). Re-clone to retry."
                    )
                    existing_review = (row.team_b_review or "").strip()
                    row.team_b_review = (
                        f"{note}\n\n{existing_review}" if existing_review else note
                    )[:1024]
                    try:
                        record_transition(
                            s,
                            sid,
                            from_status=prior,
                            to_status=PipelineStatus.REJECTED.value,
                            from_stage=prior_stage,
                            to_stage=rej_stage,
                            actor="orphan_reaper",
                            reason=f"orphaned >{mins}min in {prior}, no active run",
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "orphan_reaper: transition_log write failed for %s", sid
                        )
                    reaped += 1
                    logger.warning(
                        "orphan_reaper: flushed strategy %s (%s -> REJECTED, stale >%dmin)",
                        sid, prior, mins,
                    )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "orphan_reaper: flush failed for strategy %s (skipped)", sid
                )
    except Exception:  # noqa: BLE001
        logger.exception("orphan_reaper: reap_orphaned_pipelines crashed (non-fatal)")
    return reaped


def _critic_revise_story_enabled() -> bool:
    """T-REVISE — when True, a No-Go on a non-final critic attempt revises the
    ALPHA STORY (not just re-runs the Coder) using the critic's feedback before
    the next attempt. Off by default ⇒ byte-identical to the legacy coder-only
    retry. One extra researcher call per revised attempt, charged to the
    strategy's budget scope."""
    from backend._envloader import env_bool
    return env_bool("CRITIC_REVISE_STORY", False)


# --------------------------------------------------------------------------- #
# T-DSR — Deflated Sharpe Ratio (multiple-testing correction)                 #
# --------------------------------------------------------------------------- #

def _dsr_enabled() -> bool:
    """Attach the Deflated Sharpe Ratio (Bailey-LdP 2014) to backtest metrics.
    Default ON; the metric is purely additive telemetry (the DSR *gate* is a
    separate observe-default floor in strategy_gates). Set STRATEGY_DSR_ENABLED=0
    to skip the trial-Sharpe DB read entirely."""
    from backend._envloader import env_bool
    return env_bool("STRATEGY_DSR_ENABLED", True)


def _dsr_min_trials() -> int:
    """Minimum prior trials before DSR is computed. Below this the trial-Sharpe
    dispersion is too noisy to deflate meaningfully, so we leave the metric absent
    and the plain PSR stands (no deflation). Default 20."""
    from backend._envloader import env_int
    return env_int("STRATEGY_DSR_MIN_TRIALS", 20, minimum=2, maximum=100_000)


def _dsr_max_trials() -> int:
    """Cap on how many recent prior strategies form the DSR trial population
    (bounds the per-backtest DB read). Default 1000."""
    from backend._envloader import env_int
    return env_int("STRATEGY_DSR_MAX_TRIALS", 1000, minimum=2, maximum=1_000_000)


def _recent_trial_sharpes(limit: int) -> List[float]:
    """Annualized Sharpes of the most recent prior strategies — the DSR trial
    population (the factory's search history). Reads the ``backtest_metrics`` JSON
    of the last ``limit`` AlphaStrategy rows and keeps the finite Sharpes. Never
    raises (returns ``[]`` on any error ⇒ caller falls back to the plain PSR)."""
    import math
    try:
        with session_scope() as s:
            rows = (
                s.query(AlphaStrategy.backtest_metrics)
                .order_by(AlphaStrategy.id.desc())
                .limit(int(limit))
                .all()
            )
    except Exception:  # noqa: BLE001
        logger.exception("DSR trial-sharpe fetch failed (non-fatal)")
        return []
    out: List[float] = []
    for (raw,) in rows:
        if not raw:
            continue
        try:
            m = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(m, dict):
            continue
        sr = m.get("annualized_sharpe")
        if sr is None:
            continue
        try:
            srf = float(sr)
        except (TypeError, ValueError):
            continue
        if math.isfinite(srf):
            out.append(srf)
    return out


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_to_state(state: RunState, line: str) -> None:
    """Append to state.logs and emit a logger.info entry.

    P15/D-M5 locking semantics: callers obtain ``state`` from ``_REGISTRY``
    while holding ``_REGISTRY_LOCK``, but once they have the reference they
    mutate ``state.logs`` / ``state.status`` without the lock. This is safe
    because each strategy_id has exactly one writer (the worker thread that
    owns its pipeline); cross-strategy reads see eventually-consistent log
    buffers, which is acceptable for the operator-facing log tail.
    """
    stamp = f"[{_now()}] {line}"
    logger.info("strategy=%s | %s", state.strategy_id, line)
    state.logs.append(stamp)
    # Cap log buffer so memory is bounded for long-running pipelines.
    if len(state.logs) > 500:
        state.logs[:] = state.logs[-500:]


def _persist_status(
    strategy_id: int,
    *,
    status: PipelineStatus,
    name: Optional[str] = None,
    formula_code: Optional[str] = None,
    config_update: Optional[Dict[str, Any]] = None,
    backtest_metrics: Optional[Dict[str, Any]] = None,
    team_b_review: Optional[str] = None,
) -> None:
    """Update the AlphaStrategy row and append a StageTransition audit row.

    P7 — every real status flip writes ``stage_transitions`` via
    :func:`backend.core.transition_log.record_transition`. Self-loops (same
    status flip — e.g. CRITIC_LOOP retrying itself) are skipped so the audit
    table stays meaningful for time-in-stage / pass-rate analytics.
    """
    with session_scope() as s:
        row = s.get(AlphaStrategy, strategy_id)
        created_new = False
        prior_status: Optional[str] = None
        prior_stage: Optional[int] = None
        if row is None:
            row = AlphaStrategy(id=strategy_id, name=name or f"strategy_{strategy_id}")
            s.add(row)
            created_new = True
        else:
            prior_status = row.status
            prior_stage = int(row.stage or 0)
            # P31-STATE3: terminal/post-approval guard. Pipeline auto-transitions
            # MUST NOT reset a strategy that has already moved past CRITIC_LOOP
            # (APPROVED/REJECTED/PAPER_TRADE/SMALL_CAPITAL/LIVE/GRAVEYARD/PAUSED).
            # Only operator-driven endpoints (/promote, /retire, /resume) may
            # move those rows. A re-run from STORY_GEN onto a LIVE row would
            # otherwise silently demote it back to INTAKE.
            if (
                prior_status in _LOCKED_STATUSES
                and status.value not in _LOCKED_STATUSES
            ):
                logger.error(
                    "P31-STATE3: refused pipeline re-entry strategy=%s "
                    "current=%s requested=%s",
                    strategy_id, prior_status, status.value,
                )
                return
        if name is not None:
            row.name = name
        new_stage = STAGE_FOR_STATUS[status]
        row.stage = new_stage
        row.status = status.value
        if formula_code is not None:
            row.formula_code = formula_code
        if config_update is not None:
            cfg = row.config()
            cfg.update(config_update)
            row.config_json = json.dumps(cfg, default=str)
        if backtest_metrics is not None:
            row.backtest_metrics = json.dumps(backtest_metrics, default=str)
        if team_b_review is not None:
            row.team_b_review = team_b_review

        # Audit-log this transition. Skip self-loops to keep the table useful.
        try:
            from backend.core.transition_log import record_transition
            should_log = created_new or (prior_status or "") != status.value
            if should_log:
                record_transition(
                    s,
                    strategy_id=strategy_id,
                    from_status=prior_status,
                    to_status=status.value,
                    from_stage=prior_stage,
                    to_stage=new_stage,
                    actor="orchestrator",
                )
        except Exception:  # noqa: BLE001
            logger.exception("StageTransition write failed (non-fatal)")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class WorkflowOrchestrator:
    """Coordinates the Stage 0 -> 4 pipeline."""

    def __init__(self, data_csv: Path = DATA_CSV, asset_symbol: str = "BTC") -> None:
        self.data_csv = data_csv
        # Multi-asset universe (P-MULTISYM): the instrument this pipeline run
        # targets. Defaults to "BTC" so every existing caller — and the bundled
        # 9-col synthetic_btc.csv path — behaves exactly as before. Non-BTC
        # symbols (altcoins + equities) load via backend.core.universe.
        self.asset_symbol: str = (asset_symbol or "BTC").strip().upper() or "BTC"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def bootstrap_strategy(self, raw_text: str, *,
                           config_extra: Optional[Dict[str, Any]] = None) -> int:
        """Create a placeholder AlphaStrategy row + RunState, return its id.

        ``config_extra`` (T3-A) merges ADDITIONAL config_json keys AFTER the four
        canonical ones, so a caller (e.g. the evolution layer) can stamp lineage
        without ever overwriting ``asset_symbol``/``asset_class``. When omitted,
        the config_json is byte-identical to before."""
        from backend.core import universe
        _cfg: Dict[str, Any] = {
            "raw_text_preview": raw_text[:240],
            "asset_symbol": self.asset_symbol,
            "asset_class": universe.asset_class_of(self.asset_symbol),
            "instrument_display": universe.display_of(self.asset_symbol),
        }
        if config_extra:
            # Never let extras clobber the canonical instrument keys.
            for k, v in config_extra.items():
                if k not in _cfg:
                    _cfg[k] = v
        with session_scope() as s:
            row = AlphaStrategy(
                name=f"alpha_{int(time.time())}",
                stage=0,
                status=PipelineStatus.INTAKE.value,
                # P-MULTISYM: stamp the target instrument so the UI / postmortem /
                # results JSON can attribute the strategy to its trading pair.
                config_json=json.dumps(_cfg),
            )
            s.add(row)
            s.flush()
            sid = int(row.id)
        _new_state = RunState(strategy_id=sid)
        _registry_put(sid, _new_state)
        _log_to_state(_new_state, f"Strategy bootstrapped (id={sid})")
        return sid

    def get_status(self, strategy_id: int) -> Dict[str, Any]:
        """Snapshot used by GET /api/pipeline/status/{id}."""
        state = _registry_get(strategy_id)
        # If the registry was cleared (e.g. process restart), fall back to DB.
        if state is None:
            with session_scope() as s:
                row = s.get(AlphaStrategy, strategy_id)
                if row is None:
                    return {
                        "strategy_id": strategy_id,
                        "current_status": "UNKNOWN",
                        "execution_time_seconds": 0.0,
                        "active_agent": "n/a",
                        "terminal_logs": [],
                        # D-L13/P16 — distinguish "no in-memory state because
                        # we don't know about this strategy" from "no in-
                        # memory state because the process was restarted".
                        "restored_from_db": False,
                    }
                return {
                    "strategy_id": strategy_id,
                    "current_status": row.status,
                    "execution_time_seconds": 0.0,
                    "active_agent": "n/a",
                    "terminal_logs": [
                        f"[restored from db] last_status={row.status} stage={row.stage}"
                    ],
                    # D-L13/P16 — flag fallback path so callers can render a
                    # "process restarted — wall-clock unavailable" hint
                    # instead of trusting the 0.0 execution time.
                    "restored_from_db": True,
                }
        return {
            "strategy_id": strategy_id,
            "current_status": state.status.value,
            "execution_time_seconds": round(time.monotonic() - state.started_at, 3),
            "active_agent": state.active_agent,
            "terminal_logs": list(state.logs),
            # D-L13/P16 — explicit False so the frontend can always rely on
            # this key being present.
            "restored_from_db": False,
        }

    def run_full_pipeline_for_id(
        self,
        strategy_id: int,
        raw_text: str,
        prior_node_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Background task wrapper that catches all unexpected exceptions.

        P15/D-M7 race semantics: ``_registry_get`` may miss when the caller is
        a fresh background task that hasn't gone through ``bootstrap_strategy``
        (e.g. a retried promotion). In that case we synthesise a ``RunState``
        whose ``started_at`` defaults to ``time.monotonic()`` at construction
        time — the resulting pipeline ``execution_time_seconds`` will under-
        report any time the strategy spent waiting in a queue, but accurately
        reflects the wall-clock spent inside this orchestrator invocation.
        """
        state = _registry_get(strategy_id)
        if state is None:
            state = RunState(strategy_id=strategy_id)
            _registry_put(strategy_id, state)
        try:
            # P-STRATBUDGET: bind the per-strategy budget context so a runaway
            # strategy (deep retries / huge prompts) trips its own USD ceiling and
            # is REJECTED, instead of silently eating the whole daily cap. No-op
            # unless ALPHA_LLM_PER_STRATEGY_USD_CAP is set.
            from backend.core import llm_budget as _llm_budget
            with _llm_budget.strategy_budget_scope(strategy_id):
                return self._run_pipeline(state, raw_text, prior_node_ids)
        except Exception as exc:  # noqa: BLE001
            self._log_pipeline_error(strategy_id, exc)
            _log_to_state(state, f"FATAL: {type(exc).__name__}: {exc}")
            self._transition(state, PipelineStatus.REJECTED)
            _persist_status(strategy_id, status=PipelineStatus.REJECTED,
                            team_b_review=f"Pipeline failed: {exc}")
            _finalize_rejection(
                strategy_id,
                state,
                reason=f"Pipeline failed: {exc}",
                factor_code=None,
                alpha_story="",
                source_node_ids=list(prior_node_ids or []),
            )
            return {"strategy_id": strategy_id, "status": "REJECTED", "error": str(exc)}

    # Synchronous helper used by tests + scripts.
    def run_full_pipeline(self, raw_text: str) -> Dict[str, Any]:
        sid = self.bootstrap_strategy(raw_text)
        return self.run_full_pipeline_for_id(sid, raw_text)

    def run_pipeline_from_story(
        self,
        strategy_id: int,
        *,
        alpha_story: str,
        backtest_config_yaml: str = "",
        backtest_config: Optional[Dict[str, Any]] = None,
        config_yaml_invalid: bool = False,
        source_node_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Entry point for the Alpha Lab "Extract → Backtest" flow (P5-BE-05).

        Skips INTAKE and STORY_GEN — the chat session already supplied both.
        Persists the story + YAML to ``config_json`` (mirroring what the normal
        STORY_GEN phase would do), then runs CODE_GEN → BACKTESTING →
        CRITIC_LOOP → APPROVED/REJECTED with the existing retry loop.

        Side-effects on failure (logged + REJECTED transition) are identical to
        ``run_full_pipeline_for_id``.
        """
        state = _registry_get(strategy_id)
        if state is None:
            state = RunState(strategy_id=strategy_id)
            _registry_put(strategy_id, state)
        try:
            # P-STRATBUDGET (gap fix): the chat-extract path was missing the
            # per-strategy budget scope that run_full_pipeline_for_id has, so
            # extracted strategies escaped ALPHA_LLM_PER_STRATEGY_USD_CAP. Bind
            # it here too — no-op unless the cap is set.
            from backend.core import llm_budget as _llm_budget
            with _llm_budget.strategy_budget_scope(strategy_id):
                return self._run_pipeline_from_story(
                    state,
                    alpha_story=alpha_story,
                    backtest_config_yaml=backtest_config_yaml,
                    backtest_config=backtest_config,
                    config_yaml_invalid=config_yaml_invalid,
                    source_node_ids=source_node_ids or [],
                )
        except Exception as exc:  # noqa: BLE001
            self._log_pipeline_error(strategy_id, exc, stage="EXTRACT_BOOT")
            _log_to_state(state, f"FATAL: {type(exc).__name__}: {exc}")
            self._transition(state, PipelineStatus.REJECTED)
            _persist_status(
                strategy_id,
                status=PipelineStatus.REJECTED,
                team_b_review=f"Pipeline failed: {exc}",
            )
            _finalize_rejection(
                strategy_id,
                state,
                reason=f"Pipeline failed: {exc}",
                factor_code=None,
                alpha_story=alpha_story,
                source_node_ids=list(source_node_ids or []),
            )
            return {"strategy_id": strategy_id, "status": "REJECTED", "error": str(exc)}

    # ------------------------------------------------------------------
    # Internal flow
    # ------------------------------------------------------------------

    def _transition(self, state: RunState, new_status: PipelineStatus, agent: Optional[str] = None) -> None:
        prev_agent = state.active_agent or "system"
        prev_status = state.status.value if state.status else "INIT"
        state.status = new_status
        if agent:
            state.active_agent = agent
        _log_to_state(state, f"-> status={new_status.value} agent={state.active_agent}")
        # P8-FIX/H-7: record an inter-agent handoff in the dialogue buffer so
        # /api/agent-dialogue can render the live agent-to-agent transcript.
        try:
            target_agent = state.active_agent or "system"
            record_dialogue(
                strategy_id=int(state.strategy_id),
                from_agent=prev_agent,
                to_agent=target_agent,
                intent="handoff",
                payload=f"{prev_status} -> {new_status.value}",
            )
        except Exception:  # noqa: BLE001 — never fail the pipeline over a transcript hiccup
            logger.exception("agent_dialogue record_dialogue failed (handoff)")

    def _run_pipeline(
        self,
        state: RunState,
        raw_text: str,
        prior_node_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        sid = state.strategy_id

        # ---- Phase 1: INTAKE ----
        self._transition(state, PipelineStatus.INTAKE, agent="intake")
        with session_scope() as s:
            node = process_text_to_node(raw_text, session=s)
            s.commit()
        node_id = int(node["id"])
        node_ids = list(prior_node_ids or []) + [node_id]
        _log_to_state(state, f"intake.completed node_id={node_id}")
        _persist_status(
            sid,
            status=PipelineStatus.INTAKE,
            name=node["title"][:120],
            config_update={"source_node_ids": node_ids, "intake_node": node["id"]},
        )

        # ---- Phase 2: STORY_GEN ----
        self._transition(state, PipelineStatus.STORY_GEN, agent="researcher")
        try:
            story_payload = generate_alpha_story(node_ids)
        except Exception as exc:  # noqa: BLE001
            self._log_pipeline_error(sid, exc, stage="STORY_GEN")
            _log_to_state(state, f"researcher.failed {type(exc).__name__}: {exc}")
            _persist_status(sid, status=PipelineStatus.REJECTED,
                            team_b_review=f"Researcher failed: {exc}")
            self._transition(state, PipelineStatus.REJECTED)
            _finalize_rejection(
                sid,
                state,
                reason=f"Researcher failed: {exc}",
                factor_code=None,
                alpha_story="",
                source_node_ids=list(node_ids),
            )
            return self._final_payload(sid, state, verdict="REJECTED")
        alpha_story = story_payload["story"]
        _log_to_state(
            state,
            f"researcher.completed story_chars={len(alpha_story)} "
            f"yaml_ok={story_payload.get('backtest_config') is not None}",
        )
        story_config_update: Dict[str, Any] = {
            "story_path": story_payload["story_path"],
            "alpha_story": alpha_story,
            "backtest_config_yaml": story_payload.get("backtest_config_yaml", ""),
        }
        # Only attach the parsed YAML if it actually parsed — keeps the config
        # blob small and obvious when the model emits malformed YAML.
        if story_payload.get("backtest_config") is not None:
            story_config_update["backtest_config"] = story_payload["backtest_config"]
        if story_payload.get("config_yaml_invalid"):
            story_config_update["config_yaml_invalid"] = True
        _persist_status(
            sid,
            status=PipelineStatus.STORY_GEN,
            config_update=story_config_update,
        )

        # ---- Phase 3: CODE_GEN ----
        self._transition(state, PipelineStatus.CODE_GEN, agent="coder")
        df = self._load_market_df()

        attempt = 1
        critique: Optional[str] = None
        verdict_payload: Optional[Dict[str, Any]] = None
        bt_dict: Optional[Dict[str, Any]] = None
        factor_code: Optional[str] = None

        max_attempts = 2  # original + EXACTLY ONE retry
        while attempt <= max_attempts:
            _log_to_state(state, f"coder.attempt={attempt}")
            try:
                factor_code = generate_factor_code(
                    self._story_for_coder(alpha_story), critique=critique)
            except Exception as exc:
                self._log_pipeline_error(sid, exc, stage="CODE_GEN")
                _log_to_state(state, f"coder.failed {type(exc).__name__}: {exc}")
                _persist_status(sid, status=PipelineStatus.REJECTED,
                                team_b_review=f"Coder failed: {exc}")
                self._transition(state, PipelineStatus.REJECTED)
                _finalize_rejection(
                    sid,
                    state,
                    reason=f"Coder failed: {exc}",
                    factor_code=None,
                    alpha_story=alpha_story,
                    source_node_ids=list(prior_node_ids or []) + [node_id],
                )
                return self._final_payload(sid, state, verdict="REJECTED")

            _persist_status(sid, status=PipelineStatus.CODE_GEN, formula_code=factor_code)

            # ---- Phase 4: BACKTESTING ----
            self._transition(state, PipelineStatus.BACKTESTING, agent="backtester")
            try:
                sandbox_res = safe_execute_factor(factor_code, df)
            except (SandboxValidationError, SandboxExecutionError) as exc:
                self._log_pipeline_error(sid, exc, stage="SANDBOX")
                _log_to_state(state, f"sandbox.rejected {exc}")
                _persist_status(sid, status=PipelineStatus.REJECTED,
                                formula_code=factor_code,
                                team_b_review=f"Sandbox rejection: {exc}")
                self._transition(state, PipelineStatus.REJECTED)
                _finalize_rejection(
                    sid,
                    state,
                    reason=f"Sandbox rejection: {exc}",
                    factor_code=factor_code,
                    alpha_story=alpha_story,
                    source_node_ids=list(prior_node_ids or []) + [node_id],
                )
                return self._final_payload(sid, state, verdict="REJECTED")

            bt_result = AlphaBacktester(df, sandbox_res.signal).run()
            bt_dict = bt_result.to_dict()
            self._augment_metrics(bt_dict, df=df, signal=sandbox_res.signal)
            self._save_results_file(sid, factor_code, alpha_story, bt_dict)
            _persist_status(
                sid,
                status=PipelineStatus.BACKTESTING,
                backtest_metrics=bt_dict["metrics"],
                config_update={"trades": bt_dict["trades"]},
            )
            _log_to_state(
                state,
                "backtester.completed " + ", ".join(
                    f"{k}={v}" for k, v in bt_dict["metrics"].items()
                ),
            )

            # ---- Pre-critic Score Card (P-GATES + T3-B) ----
            # Superset of the liveness gate: a dead backtest (0 trades) OR a
            # structurally-junk one (non-finite core metrics, over-trading,
            # constant returns, 0 evaluable regime windows) cannot be a real
            # alpha — reject it deterministically and skip the (expensive) Critic
            # LLM call. When STRATEGY_SCORECARD_GATE=0 the Score Card collapses to
            # exactly the original liveness check (byte-identical behaviour).
            _sc = strategy_gates.evaluate_scorecard(bt_dict["metrics"])
            if not _sc.alive:
                _pg_reason = _sc.reason or strategy_gates.precritic_reject_reason(bt_dict["metrics"])
                _log_to_state(state, f"precritic.rejected {_pg_reason}")
                _persist_status(sid, status=PipelineStatus.REJECTED,
                                formula_code=factor_code,
                                team_b_review=_pg_reason,
                                config_update={"scorecard": _sc.to_dict()})
                self._transition(state, PipelineStatus.REJECTED)
                _finalize_rejection(
                    sid,
                    state,
                    reason=_pg_reason,
                    factor_code=factor_code,
                    alpha_story=alpha_story,
                    source_node_ids=list(prior_node_ids or []) + [node_id],
                    metrics=bt_dict["metrics"],
                )
                return self._final_payload(sid, state, verdict="REJECTED",
                                           metrics=bt_dict["metrics"])

            # ---- Phase 5: CRITIC_LOOP ----
            self._transition(state, PipelineStatus.CRITIC_LOOP, agent="critic")
            try:
                verdict_payload = review_strategy(
                    alpha_story=alpha_story,
                    factor_code=factor_code,
                    backtest_metrics=bt_dict["metrics"],
                    trades=bt_dict["trades"],
                )
            except Exception as exc:  # noqa: BLE001
                self._log_pipeline_error(sid, exc, stage="CRITIC_LOOP")
                _log_to_state(state, f"critic.failed {type(exc).__name__}: {exc}")
                _persist_status(sid, status=PipelineStatus.REJECTED,
                                formula_code=factor_code,
                                team_b_review=f"Critic failed: {exc}")
                self._transition(state, PipelineStatus.REJECTED)
                _finalize_rejection(
                    sid,
                    state,
                    reason=f"Critic failed: {exc}",
                    factor_code=factor_code,
                    alpha_story=alpha_story,
                    source_node_ids=list(prior_node_ids or []) + [node_id],
                    metrics=(bt_dict or {}).get("metrics", {}),
                )
                return self._final_payload(sid, state, verdict="REJECTED",
                                           metrics=(bt_dict or {}).get("metrics", {}))
            _log_to_state(
                state,
                f"critic.attempt={attempt} verdict={verdict_payload['verdict']} "
                f"violations={verdict_payload['violation_tags']} "
                f"severity={verdict_payload.get('severity_tag')} "
                f"category={verdict_payload.get('alpha_category')}",
            )
            critic_config_update = {
                "critic_severity_tag": verdict_payload.get("severity_tag"),
                "critic_violation_tags": verdict_payload.get("violation_tags", []),
                "critic_flags": verdict_payload.get("flags", []),
                "critic_confidence": verdict_payload.get("confidence"),
                "alpha_category": verdict_payload.get("alpha_category"),
                # P8-FIX/H-4: 6-key soul questions dict for CriticVerdict UI.
                "critic_soul_questions": verdict_payload.get("soul_questions") or {},
            }
            # P8-FIX/H-7: emit critic verdict into agent_dialogue.
            try:
                _verdict = verdict_payload.get("verdict", "No-Go")
                record_dialogue(
                    strategy_id=int(state.strategy_id),
                    from_agent="critic",
                    to_agent="team_a_lead",
                    intent="approval" if _verdict == "Go" else "veto",
                    payload=(
                        f"{_verdict} severity={verdict_payload.get('severity_tag')}"
                        f" category={verdict_payload.get('alpha_category')}"
                        f" violations={verdict_payload.get('violation_tags', [])}"
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.exception("agent_dialogue critic emit failed")
            _persist_status(
                sid,
                status=PipelineStatus.CRITIC_LOOP,
                team_b_review=verdict_payload["critique_markdown"],
                config_update=critic_config_update,
            )

            # ---- Deterministic gates (P-GATES + P-DIVERSITY) ----
            # Quality gate: PSR/Sortino/min-trades vetoes + short-vol flags from
            # the augmented metrics. Diversity gate: max return-correlation vs the
            # approved pool (QFLM — favor decorrelated alphas over the single
            # best). Both default to OBSERVE (record only); ENFORCE can veto an
            # LLM "Go" — the deterministic floors the Critic-only gate lacks.
            _gate = strategy_gates.evaluate_quality(bt_dict["metrics"])
            critic_config_update["quality_gate"] = _gate.to_dict()
            _div = diversity.evaluate(sid, bt_dict.get("equity_curve"))
            critic_config_update["diversity"] = _div.to_dict()
            # T1-D — regime / sub-period stability gate (reads regime_* keys
            # attached in _augment_metrics; OBSERVE by default).
            _regime = strategy_gates.evaluate_regime(bt_dict["metrics"])
            critic_config_update["regime_gate"] = _regime.to_dict()
            if _gate.flags:
                _log_to_state(state, "quality_gate.flags " + ", ".join(_gate.flags))
            if _regime.vetoes:
                _log_to_state(state, "regime_gate.vetoes " + ", ".join(_regime.vetoes))
            if _div.redundant:
                _log_to_state(
                    state,
                    f"diversity.redundant max_corr={_div.max_correlation} "
                    f"vs_strategy={_div.most_similar_id}")
            _veto_reasons: List[str] = []
            if _gate.enforced and not _gate.passed:
                _veto_reasons.append("quality: " + _gate.reason)
            if _div.enforced and not _div.passed:
                _veto_reasons.append(
                    f"diversity: corr {_div.max_correlation} vs strategy "
                    f"{_div.most_similar_id}")
            if _regime.enforced and not _regime.passed:
                _veto_reasons.append("regime: " + _regime.reason)
            if verdict_payload["verdict"] == "Go" and _veto_reasons:
                _veto_msg = "gate veto — " + "; ".join(_veto_reasons)
                _log_to_state(state, _veto_msg)
                _persist_status(sid, status=PipelineStatus.REJECTED,
                                formula_code=factor_code,
                                team_b_review=_veto_msg,
                                config_update=critic_config_update)
                self._transition(state, PipelineStatus.REJECTED)
                _finalize_rejection(
                    sid,
                    state,
                    reason=_veto_msg,
                    factor_code=factor_code,
                    alpha_story=alpha_story,
                    source_node_ids=list(prior_node_ids or []) + [node_id],
                    metrics=bt_dict["metrics"],
                    soul_questions=verdict_payload.get("soul_questions"),
                )
                return self._final_payload(sid, state, verdict="REJECTED",
                                           metrics=bt_dict["metrics"])

            if verdict_payload["verdict"] == "Go":
                self._transition(state, PipelineStatus.APPROVED)
                _persist_status(
                    sid,
                    status=PipelineStatus.APPROVED,
                    team_b_review=verdict_payload["critique_markdown"],
                    config_update=critic_config_update,
                )
                # Side-effects (best-effort; never crash the pipeline):
                _maybe_create_postmortem_node(
                    sid,
                    name=alpha_story.split("\n", 1)[0][:160] if alpha_story else f"strategy_{sid}",
                    verdict="APPROVED",
                    critique_markdown=verdict_payload["critique_markdown"],
                    metrics=bt_dict["metrics"],
                    source_node_ids=list(prior_node_ids or []) + [node_id],
                    soul_questions=verdict_payload.get("soul_questions"),
                )
                _maybe_notify_transition(
                    sid,
                    name=alpha_story.split("\n", 1)[0][:80] if alpha_story else f"strategy_{sid}",
                    from_status="CRITIC_LOOP",
                    to_status="APPROVED",
                    metrics=bt_dict["metrics"],
                )
                # P11-B-02 - IC ledger positive observation.
                # P30-R5: compute the REAL Spearman rank-IC of factor[t]
                # vs returns[t+1]; fall back to a sharpe-derived proxy only
                # if factor evaluation didn't leave a usable series.
                # Previously sharpe*0.1 was stored as IC — not a correlation
                # coefficient by any definition.
                try:
                    from backend.core.ic_history import record_pipeline_outcomes
                    from backend.core.factor_evaluator import _time_series_ic
                    _ic_val: Optional[float] = None
                    try:
                        _factor = getattr(sandbox_res, "factor", None)
                        if _factor is not None and df is not None and "close" in df.columns:
                            _ret = df["close"].pct_change()
                            _ic_real = _time_series_ic(_factor, _ret)
                            if _ic_real is not None:
                                _ic_val = float(_ic_real)
                    except Exception:  # noqa: BLE001
                        _ic_val = None
                    if _ic_val is None:
                        # Cannot compute a valid rank-IC; record a neutral 0.0
                        # rather than a Sharpe-derived proxy that has no statistical
                        # meaning as a correlation coefficient.
                        _ic_val = 0.0
                    record_pipeline_outcomes(
                        list(prior_node_ids or []) + [node_id],
                        strategy_id=int(sid),
                        ic_value=float(_ic_val),
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("IC ledger write (approval) failed (non-fatal)")
                return self._final_payload(
                    sid,
                    state,
                    verdict="APPROVED",
                    metrics=bt_dict["metrics"],
                )

            # No-Go: prep retry critique
            critique = (
                verdict_payload["critique_markdown"]
                + "\n\nViolation tags: "
                + ", ".join(verdict_payload.get("violation_tags", []))
            )
            # T-REVISE — optionally revise the STORY (not just re-run the Coder)
            # from the critic's feedback before the next attempt, so thesis-level
            # objections can actually be fixed. Off by default. Only when a next
            # attempt exists; revise returns None (→ unrevised retry) on a soft
            # failure, and a budget breach propagates to the outer reject handler.
            if attempt < max_attempts and _critic_revise_story_enabled():
                _revised = revise_alpha_story(alpha_story, critique)
                if _revised:
                    alpha_story = _revised
                    _log_to_state(state, "critic.revise — story revised from feedback")
                    _persist_status(sid, status=PipelineStatus.CODE_GEN,
                                    config_update={"alpha_story": alpha_story,
                                                   "revised_from_critic": True})
            attempt += 1

        # P15/D-H7 — Two attempts exhausted: REJECT through the canonical
        # _finalize_rejection sink (consolidates postmortem + telegram + IC
        # ledger emit) so retry-exhaust matches Coder/Sandbox failure semantics.
        self._transition(state, PipelineStatus.REJECTED)
        _reason = (verdict_payload or {}).get("critique_markdown", "Rejected after retries")
        _persist_status(
            sid,
            status=PipelineStatus.REJECTED,
            team_b_review=_reason,
        )
        _finalize_rejection(
            sid,
            state,
            reason=_reason,
            factor_code=factor_code,
            alpha_story=alpha_story,
            source_node_ids=list(prior_node_ids or []) + [node_id],
            metrics=(bt_dict or {}).get("metrics", {}),
            soul_questions=(verdict_payload or {}).get("soul_questions"),
        )
        return self._final_payload(sid, state, verdict="REJECTED",
                                   metrics=(bt_dict or {}).get("metrics", {}))

    def _run_pipeline_from_story(
        self,
        state: RunState,
        *,
        alpha_story: str,
        backtest_config_yaml: str,
        backtest_config: Optional[Dict[str, Any]],
        config_yaml_invalid: bool,
        source_node_ids: List[int],
    ) -> Dict[str, Any]:
        """Mirrors `_run_pipeline` starting at CODE_GEN.

        The story + yaml are persisted as a STORY_GEN result before CODE_GEN
        kicks so the strategy detail page renders complete metadata while the
        pipeline is still running.
        """
        sid = state.strategy_id

        # ---- Persist story+yaml as STORY_GEN result (skip INTAKE) ----
        self._transition(state, PipelineStatus.STORY_GEN, agent="researcher")
        story_config_update: Dict[str, Any] = {
            "alpha_story": alpha_story,
            "backtest_config_yaml": backtest_config_yaml or "",
            "source_node_ids": source_node_ids,
            "extracted_from_chat": True,
        }
        if backtest_config is not None:
            story_config_update["backtest_config"] = backtest_config
        if config_yaml_invalid:
            story_config_update["config_yaml_invalid"] = True
        _persist_status(
            sid,
            status=PipelineStatus.STORY_GEN,
            config_update=story_config_update,
        )

        # ---- Phase 3: CODE_GEN (lifted verbatim from _run_pipeline) ----
        self._transition(state, PipelineStatus.CODE_GEN, agent="coder")
        df = self._load_market_df()

        attempt = 1
        critique: Optional[str] = None
        verdict_payload: Optional[Dict[str, Any]] = None
        bt_dict: Optional[Dict[str, Any]] = None
        factor_code: Optional[str] = None
        max_attempts = 2
        while attempt <= max_attempts:
            _log_to_state(state, f"coder.attempt={attempt}")
            try:
                factor_code = generate_factor_code(
                    self._story_for_coder(alpha_story), critique=critique)
            except Exception as exc:
                self._log_pipeline_error(sid, exc, stage="CODE_GEN")
                _log_to_state(state, f"coder.failed {type(exc).__name__}: {exc}")
                _persist_status(sid, status=PipelineStatus.REJECTED,
                                team_b_review=f"Coder failed: {exc}")
                self._transition(state, PipelineStatus.REJECTED)
                _finalize_rejection(
                    sid,
                    state,
                    reason=f"Coder failed: {exc}",
                    factor_code=None,
                    alpha_story=alpha_story,
                    source_node_ids=list(source_node_ids or []),
                )
                return self._final_payload(sid, state, verdict="REJECTED")

            _persist_status(sid, status=PipelineStatus.CODE_GEN, formula_code=factor_code)

            self._transition(state, PipelineStatus.BACKTESTING, agent="backtester")
            try:
                sandbox_res = safe_execute_factor(factor_code, df)
            except (SandboxValidationError, SandboxExecutionError) as exc:
                self._log_pipeline_error(sid, exc, stage="SANDBOX")
                _log_to_state(state, f"sandbox.rejected {exc}")
                _persist_status(sid, status=PipelineStatus.REJECTED,
                                formula_code=factor_code,
                                team_b_review=f"Sandbox rejection: {exc}")
                self._transition(state, PipelineStatus.REJECTED)
                _finalize_rejection(
                    sid,
                    state,
                    reason=f"Sandbox rejection: {exc}",
                    factor_code=factor_code,
                    alpha_story=alpha_story,
                    source_node_ids=list(source_node_ids or []),
                )
                return self._final_payload(sid, state, verdict="REJECTED")

            bt_result = AlphaBacktester(df, sandbox_res.signal).run()
            bt_dict = bt_result.to_dict()
            self._augment_metrics(bt_dict, df=df, signal=sandbox_res.signal)
            self._save_results_file(sid, factor_code, alpha_story, bt_dict)
            _persist_status(
                sid,
                status=PipelineStatus.BACKTESTING,
                backtest_metrics=bt_dict["metrics"],
                config_update={"trades": bt_dict["trades"]},
            )
            _log_to_state(
                state,
                "backtester.completed " + ", ".join(
                    f"{k}={v}" for k, v in bt_dict["metrics"].items()
                ),
            )

            self._transition(state, PipelineStatus.CRITIC_LOOP, agent="critic")
            try:
                verdict_payload = review_strategy(
                    alpha_story=alpha_story,
                    factor_code=factor_code,
                    backtest_metrics=bt_dict["metrics"],
                    trades=bt_dict["trades"],
                )
            except Exception as exc:  # noqa: BLE001
                # P34: mirror the _run_pipeline twin — route story-path critic
                # failures through _finalize_rejection so postmortem + notify +
                # IC-ledger stay consistent instead of a bare REJECTED with no
                # audit sink.
                self._log_pipeline_error(sid, exc, stage="CRITIC_LOOP")
                _log_to_state(state, f"critic.failed {type(exc).__name__}: {exc}")
                _persist_status(sid, status=PipelineStatus.REJECTED,
                                formula_code=factor_code,
                                team_b_review=f"Critic failed: {exc}")
                self._transition(state, PipelineStatus.REJECTED)
                _finalize_rejection(
                    sid,
                    state,
                    reason=f"Critic failed: {exc}",
                    factor_code=factor_code,
                    alpha_story=alpha_story,
                    source_node_ids=list(source_node_ids or []),
                    metrics=(bt_dict or {}).get("metrics", {}),
                )
                return self._final_payload(sid, state, verdict="REJECTED",
                                           metrics=(bt_dict or {}).get("metrics", {}))
            critic_config_update = {
                "critic_severity_tag": verdict_payload.get("severity_tag"),
                "critic_violation_tags": verdict_payload.get("violation_tags", []),
                "critic_flags": verdict_payload.get("flags", []),
                "critic_confidence": verdict_payload.get("confidence"),
                "alpha_category": verdict_payload.get("alpha_category"),
                # P8-FIX/H-4: 6-key soul questions dict for CriticVerdict UI.
                "critic_soul_questions": verdict_payload.get("soul_questions") or {},
            }
            # P8-FIX/H-7: emit critic verdict into agent_dialogue.
            try:
                _verdict = verdict_payload.get("verdict", "No-Go")
                record_dialogue(
                    strategy_id=int(state.strategy_id),
                    from_agent="critic",
                    to_agent="team_a_lead",
                    intent="approval" if _verdict == "Go" else "veto",
                    payload=(
                        f"{_verdict} severity={verdict_payload.get('severity_tag')}"
                        f" category={verdict_payload.get('alpha_category')}"
                        f" violations={verdict_payload.get('violation_tags', [])}"
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.exception("agent_dialogue critic emit failed")
            _persist_status(
                sid,
                status=PipelineStatus.CRITIC_LOOP,
                team_b_review=verdict_payload["critique_markdown"],
                config_update=critic_config_update,
            )
            if verdict_payload["verdict"] == "Go":
                self._transition(state, PipelineStatus.APPROVED)
                _persist_status(
                    sid,
                    status=PipelineStatus.APPROVED,
                    team_b_review=verdict_payload["critique_markdown"],
                    config_update=critic_config_update,
                )
                _maybe_create_postmortem_node(
                    sid,
                    name=alpha_story.split("\n", 1)[0][:160] or f"strategy_{sid}",
                    verdict="APPROVED",
                    critique_markdown=verdict_payload["critique_markdown"],
                    metrics=bt_dict["metrics"],
                    source_node_ids=source_node_ids,
                    soul_questions=verdict_payload.get("soul_questions"),
                )
                _maybe_notify_transition(
                    sid,
                    name=alpha_story.split("\n", 1)[0][:80] or f"strategy_{sid}",
                    from_status="CRITIC_LOOP",
                    to_status="APPROVED",
                    metrics=bt_dict["metrics"],
                )
                # P11-B-02 - IC ledger positive observation.
                # P30-R5: compute the REAL Spearman rank-IC of factor[t]
                # vs returns[t+1]; fall back to a sharpe-derived proxy only
                # if factor evaluation didn't leave a usable series.
                # Previously sharpe*0.1 was stored as IC — not a correlation
                # coefficient by any definition.
                try:
                    from backend.core.ic_history import record_pipeline_outcomes
                    from backend.core.factor_evaluator import _time_series_ic
                    _ic_val: Optional[float] = None
                    try:
                        _factor = getattr(sandbox_res, "factor", None)
                        if _factor is not None and df is not None and "close" in df.columns:
                            _ret = df["close"].pct_change()
                            _ic_real = _time_series_ic(_factor, _ret)
                            if _ic_real is not None:
                                _ic_val = float(_ic_real)
                    except Exception:  # noqa: BLE001
                        _ic_val = None
                    if _ic_val is None:
                        # Cannot compute a valid rank-IC; record a neutral 0.0
                        # rather than a Sharpe-derived proxy that has no statistical
                        # meaning as a correlation coefficient.
                        _ic_val = 0.0
                    record_pipeline_outcomes(
                        list(source_node_ids or []),
                        strategy_id=int(sid),
                        ic_value=float(_ic_val),
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("IC ledger write (approval) failed (non-fatal)")
                return self._final_payload(sid, state, verdict="APPROVED",
                                           metrics=bt_dict["metrics"])
            critique = (
                verdict_payload["critique_markdown"]
                + "\n\nViolation tags: "
                + ", ".join(verdict_payload.get("violation_tags", []))
            )
            # T-REVISE — mirror the _run_pipeline twin: revise the STORY from the
            # critic feedback before the next attempt when CRITIC_REVISE_STORY is
            # set. Off by default ⇒ unchanged coder-only retry.
            if attempt < max_attempts and _critic_revise_story_enabled():
                _revised = revise_alpha_story(alpha_story, critique)
                if _revised:
                    alpha_story = _revised
                    _log_to_state(state, "critic.revise — story revised from feedback")
                    _persist_status(sid, status=PipelineStatus.CODE_GEN,
                                    config_update={"alpha_story": alpha_story,
                                                   "revised_from_critic": True})
            attempt += 1

        # P15/D-H7 — Two attempts exhausted: REJECT through the canonical
        # _finalize_rejection sink so postmortem + telegram + IC ledger match
        # the _run_pipeline path exactly.
        self._transition(state, PipelineStatus.REJECTED)
        _reason = (verdict_payload or {}).get("critique_markdown", "Rejected after retries")
        _persist_status(
            sid,
            status=PipelineStatus.REJECTED,
            team_b_review=_reason,
        )
        _finalize_rejection(
            sid,
            state,
            reason=_reason,
            factor_code=factor_code,
            alpha_story=alpha_story,
            source_node_ids=list(source_node_ids or []),
            metrics=(bt_dict or {}).get("metrics", {}),
            soul_questions=(verdict_payload or {}).get("soul_questions"),
        )
        return self._final_payload(sid, state, verdict="REJECTED",
                                   metrics=(bt_dict or {}).get("metrics", {}))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_market_df(self) -> pd.DataFrame:
        # P-MULTISYM: non-BTC instruments (altcoins + equities) load via the
        # universe loader, which resolves the per-asset CSV, fetches it on demand
        # (equities from Yahoo), and pads the derivative columns to the 8-col
        # sandbox contract. BTC keeps the original bundled-CSV path byte-for-byte.
        if self.asset_symbol != "BTC":
            from backend.core import universe
            return universe.load_market_df(self.asset_symbol)
        if not self.data_csv.exists():
            raise FileNotFoundError(
                f"Required market data missing: {self.data_csv}. "
                "Run `python backend/core/data_gen.py` first."
            )
        df = pd.read_csv(self.data_csv, parse_dates=["timestamp"])
        # P32-D5 / DAT32-7 — sort + dedup so downstream slicing is monotonic
        # and any duplicate bar (keep latest revision) doesn't poison alignment.
        df = df.set_index("timestamp").sort_index()
        return df[~df.index.duplicated(keep="last")]

    def _story_for_coder(self, alpha_story: str) -> str:
        """P-MULTISYM: prepend an instrument-context directive when the target
        lacks BTC's full derivative columns (all non-BTC crypto + equities), so
        the Coder builds a price/volume factor instead of a funding/OI one that
        would be uniformly zero (and thus rejected). Returns the story unchanged
        for BTC — the default path is byte-for-byte identical."""
        if self.asset_symbol == "BTC":
            return alpha_story
        from backend.core import universe
        note = universe.asset_context_note(self.asset_symbol)
        if not note:
            return alpha_story
        return f"{note}\n\n---\n\n{alpha_story}"

    def _augment_metrics(self, bt_dict: Dict[str, Any],
                         df: Optional[pd.DataFrame] = None,
                         signal: Optional[pd.Series] = None) -> None:
        """P-RISKMETRICS: enrich the backtest metrics dict IN PLACE with the
        trade count and the additive honesty metrics (Sortino with correct TDD,
        PSR, lag-1 autocorrelation, ρ₁-adjusted Sharpe, skew/kurtosis). When
        ``df``/``signal`` are supplied it ALSO attaches out-of-sample (``oos_*``)
        metrics from a holdout-tail re-backtest (P-OOS). All keys are purely
        additive — the engine and every stored strategy are untouched. Never
        raises: a metrics failure must not sink a backtest."""
        try:
            metrics = bt_dict.get("metrics")
            if not isinstance(metrics, dict):
                return
            metrics["num_trades"] = int(bt_dict.get("trades", 0) or 0)
            extra = risk_metrics.compute_from_per_bar(
                bt_dict.get("per_bar"),
                reported_sharpe=metrics.get("annualized_sharpe"),
            )
            if extra:
                metrics.update(extra)
            # T-DSR — deflate the Sharpe for multiple-testing selection bias over
            # the factory's prior trials (Bailey-LdP 2014). Additive telemetry,
            # only attached when there are enough prior trials to estimate the
            # trial-Sharpe dispersion (else the plain PSR stands). The downstream
            # gate (STRATEGY_GATE_MIN_DSR) reads `deflated_sharpe_ratio`.
            if _dsr_enabled():
                trials = _recent_trial_sharpes(_dsr_max_trials())
                if len(trials) >= _dsr_min_trials():
                    dsr = risk_metrics.deflated_sharpe_from_per_bar(
                        bt_dict.get("per_bar"), trials)
                    if dsr is not None:
                        metrics["deflated_sharpe_ratio"] = round(float(dsr), 6)
            # T1-D — sub-period / vol-regime stability (additive, observe-default).
            # Attached BEFORE the OOS block so the Score Card's regime_observed
            # check sees it. Full-series per_bar (NOT the OOS tail).
            regime_extra = regime_metrics.compute_from_per_bar(
                bt_dict.get("per_bar"),
                n_windows=strategy_gates.regime_windows(),
            )
            if regime_extra:
                metrics.update(regime_extra)
            # T-BOOTSTRAP — block-bootstrap CI + P(Sharpe>0) over the realised
            # per-bar returns (additive, observe-default). Same full-series per_bar.
            boot_extra = bootstrap_metrics.compute_from_per_bar(bt_dict.get("per_bar"))
            if boot_extra:
                metrics.update(boot_extra)
            if df is not None and signal is not None:
                oos = self._oos_metrics(df, signal)
                if oos:
                    metrics.update(oos)
        except Exception:  # noqa: BLE001
            logger.exception("risk-metrics augmentation failed (non-fatal)")

    @staticmethod
    def _oos_fraction() -> float:
        """Holdout tail fraction for out-of-sample metrics. 0 disables. Default
        0.30 — the last ~30% of bars are never used to pick the factor, so a
        large in-sample/out-of-sample gap flags overfitting (esp. price-only
        equity factors). Additive telemetry; does NOT gate by default."""
        from backend._envloader import env_float
        return env_float("STRATEGY_OOS_FRACTION", 0.30, minimum=0.0, maximum=0.9)

    def _oos_metrics(self, df: pd.DataFrame, signal: pd.Series) -> Dict[str, Any]:
        """Re-run the backtest on the holdout tail and return ``oos_*`` metrics.
        Pure additive — the primary (full-series) metrics are unchanged. Returns
        ``{}`` when disabled or the series is too short to split meaningfully."""
        try:
            frac = self._oos_fraction()
            if not (frac > 1e-9):
                return {}
            n = int(len(df))
            if n < 120:  # too short for a meaningful split
                return {}
            cut = int(n * (1.0 - frac))
            df_oos = df.iloc[cut:]
            sig_oos = signal.iloc[cut:] if hasattr(signal, "iloc") else signal[cut:]
            if len(df_oos) < risk_metrics.PSR_MIN_BARS:
                return {}
            oos_bt = AlphaBacktester(df_oos, sig_oos).run().to_dict()
            om = oos_bt.get("metrics", {})
            out: Dict[str, Any] = {"oos_fraction": round(frac, 3),
                                   "oos_num_trades": int(oos_bt.get("trades", 0) or 0)}
            for k in ("annualized_sharpe", "annualized_return", "max_drawdown",
                      "calmar_ratio"):
                if k in om:
                    out[f"oos_{k}"] = om[k]
            oos_risk = risk_metrics.compute_from_per_bar(
                oos_bt.get("per_bar"),
                reported_sharpe=om.get("annualized_sharpe"),
            )
            for k in ("probabilistic_sharpe_ratio", "sortino_ratio"):
                if k in oos_risk:
                    out[f"oos_{k}"] = oos_risk[k]
            return out
        except Exception:  # noqa: BLE001
            logger.exception("OOS metrics computation failed (non-fatal)")
            return {}

    def _save_results_file(
        self,
        strategy_id: int,
        factor_code: str,
        alpha_story: str,
        bt_dict: Dict[str, Any],
    ) -> None:
        payload = {
            "strategy_id": strategy_id,
            "saved_at": _now(),
            # P-MULTISYM: which instrument this strategy was backtested on.
            "asset_symbol": self.asset_symbol,
            "metrics": bt_dict["metrics"],
            "equity_curve": bt_dict["equity_curve"],
            "trades": bt_dict["trades"],
            "per_bar": bt_dict.get("per_bar") or [],
            "alpha_story": alpha_story,
            "factor_code": factor_code,
        }
        out_path = RESULTS_DIR / f"strategy_{strategy_id}.json"
        # P29-S9: atomic write via tmp + os.replace (mirrors paper_trader._persist).
        import os as _os_replace_only
        tmp_path = out_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
        _os_replace_only.replace(tmp_path, out_path)

    def _log_pipeline_error(
        self,
        strategy_id: int,
        exc: BaseException,
        stage: str = "PIPELINE",
    ) -> None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        line = (
            f"[{_now()}] strategy_id={strategy_id} stage={stage} "
            f"error={type(exc).__name__}: {exc}\n{tb}\n"
        )
        try:
            with ERROR_LOG.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.write("-" * 80 + "\n")
        except Exception:
            logger.exception("Failed to write pipeline_errors.log")

    def _final_payload(
        self,
        strategy_id: int,
        state: RunState,
        *,
        verdict: str,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "strategy_id": strategy_id,
            "status": verdict,
            "execution_time_seconds": round(time.monotonic() - state.started_at, 3),
            "metrics": metrics or {},
            "terminal_logs": list(state.logs),
        }


__all__ = [
    "PipelineStatus",
    "STAGE_FOR_STATUS",
    "TERMINAL_PIPELINE_STATUSES",
    "POST_APPROVAL_STATUSES",
    "WorkflowOrchestrator",
    "RESULTS_DIR",
    "ERROR_LOG",
]

"""T3-A — evolutionary / hypothesis-tree search over alpha generation.

Instead of always proposing fresh, MUTATE / SPECIALISE the best SURVIVING
strategies (status in the approved pool, ranked by realised OOS Sharpe) so
generation compounds on proven edges. Runs ALONGSIDE the fresh path, env-gated by
``EVOLUTION_FRACTION`` (default 0 = today's behaviour exactly). Each evolved child
is a normal strategy that flows through the SAME pipeline + gates + per-strategy
budget; its lineage is stamped additively in ``config_json``.

Cold-start safe (empty pool → caller falls back to fresh proposals), budget-safe
(operator LLM call runs inside the child's ``strategy_budget_scope``), and
diversity-safe (a near-clone is caught by the existing diversity gate downstream;
a byte-identical seed is rejected pre-dispatch to avoid wasting a run).
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend._envloader import env_bool, env_float, env_int
from backend.agents._client import call_messages
from backend.agents.researcher import (
    RESEARCHER_SYSTEM_PROMPT,
    _extract_yaml_block,
    _parse_yaml,
)
from backend.core.database import AlphaStrategy, session_scope

logger = logging.getLogger("alpha.evolution")

# Reuse diversity's canonical "surviving / live book" definition verbatim.
_POOL_STATUSES = ("APPROVED", "PAPER_TRADE", "SMALL_CAPITAL", "LIVE")


# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #

def is_enabled() -> bool:
    return env_bool("EVOLUTION_ENABLED", False)


def evolution_fraction() -> float:
    return env_float("EVOLUTION_FRACTION", 0.0, minimum=0.0, maximum=1.0)


def parent_pool_size() -> int:
    return env_int("EVOLUTION_PARENT_POOL_SIZE", 20, minimum=1, maximum=200)


def min_parent_oos_sharpe() -> float:
    return env_float("EVOLUTION_MIN_PARENT_OOS_SHARPE", 0.5, minimum=-1000.0, maximum=1000.0)


def max_lineage_depth() -> int:
    return env_int("EVOLUTION_MAX_DEPTH", 4, minimum=1, maximum=32)


def operator_max_tokens() -> int:
    return env_int("EVOLUTION_OP_MAX_TOKENS", 2400, minimum=256, maximum=8000)


def _operator_name() -> str:
    raw = (__import__("os").environ.get("EVOLUTION_OPERATOR", "") or "").strip().lower()
    if raw in _OPERATORS:
        return raw
    if raw == "auto":
        return random.choice(("mutate", "specialize"))
    return "mutate"


# --------------------------------------------------------------------------- #
# Operators                                                                   #
# --------------------------------------------------------------------------- #

_MUTATE_PROMPT = """Below is a SURVIVING alpha story that passed the risk critic and the
deterministic gates. Produce a NEW alpha story that changes EXACTLY ONE lever —
a single threshold, one lookback window, OR swap one feature column — while
keeping the core thesis intact. Emit the FULL story in the same format (headline
+ the standard sections + a single ```yaml backtest-config fence), NOT a diff.

<<<PARENT_STORY>>>
{story}
<<<END>>>"""

_SPECIALIZE_PROMPT = """Below is a SURVIVING alpha story. Produce a NEW alpha story that SPECIALISES
it to a specific market regime (e.g. high-volatility, trending, or a named
trading session) by adding an explicit regime entry condition, while keeping the
core edge. Emit the FULL story in the same format (headline + the standard
sections + a single ```yaml backtest-config fence), NOT a diff.

<<<PARENT_STORY>>>
{story}
<<<END>>>"""

_OPERATORS = {"mutate": _MUTATE_PROMPT, "specialize": _SPECIALIZE_PROMPT}


@dataclass(frozen=True)
class Parent:
    strategy_id: int
    name: str
    alpha_story: str
    source_node_ids: List[int]
    asset_symbol: str
    asset_class: str
    depth: int
    oos_sharpe: float


@dataclass(frozen=True)
class EvolvedSeed:
    alpha_story: str
    source_node_ids: List[int]
    asset_symbol: str
    operator: str
    parent_ids: List[int]
    parent_depth_max: int


def should_use_evolution() -> bool:
    """One coin-flip per dispatch: True ⇒ spend this slot on an evolved child."""
    frac = evolution_fraction()
    return is_enabled() and frac > 0.0 and random.random() < frac


def _selection_score(oos_sharpe: float, depth: int) -> float:
    # Prefer realised OOS quality; mildly prefer shallower lineages (exploration).
    return float(oos_sharpe) - 0.1 * (depth / max(1, max_lineage_depth()))


def select_parents(session=None, *, k: int = 1) -> List[Parent]:
    """The surviving-strategy pool, filtered to those with a stored story +
    source nodes, under the depth cap, above the OOS-Sharpe floor; ranked by
    selection score. Empty list on cold start."""
    def _run(s) -> List[Parent]:
        rows = (
            s.query(AlphaStrategy)
            .filter(AlphaStrategy.status.in_(_POOL_STATUSES))
            .order_by(AlphaStrategy.id.desc())
            .limit(parent_pool_size() * 3)
            .all()
        )
        floor = min_parent_oos_sharpe()
        depth_cap = max_lineage_depth()
        cands: List[Parent] = []
        for r in rows:
            cfg = r.config() or {}
            story = (cfg.get("alpha_story") or "").strip()
            src = cfg.get("source_node_ids") or []
            if not story or not src:
                continue
            depth = int(cfg.get("evolution_depth", 0) or 0)
            if depth >= depth_cap:
                continue
            m = r.metrics() or {}
            oos = m.get("oos_annualized_sharpe")
            if oos is None:
                oos = m.get("annualized_sharpe")
            try:
                oos = float(oos) if oos is not None else 0.0
            except (TypeError, ValueError):
                oos = 0.0
            if oos < floor:
                continue
            cands.append(Parent(
                strategy_id=int(r.id), name=r.name or "",
                alpha_story=story,
                source_node_ids=[int(x) for x in src if isinstance(x, (int, float))],
                asset_symbol=str(cfg.get("asset_symbol") or "BTC"),
                asset_class=str(cfg.get("asset_class") or "crypto"),
                depth=depth, oos_sharpe=oos,
            ))
        cands.sort(key=lambda p: _selection_score(p.oos_sharpe, p.depth), reverse=True)
        return cands[: max(1, int(k))]

    if session is None:
        with session_scope() as s:
            return _run(s)
    return _run(session)


def _weighted_pick(parents: List[Parent]) -> Parent:
    """Weighted-random over the top-K (weight ∝ shifted OOS Sharpe) so the whole
    tree doesn't collapse onto a single elite (inbreeding guard)."""
    if len(parents) == 1:
        return parents[0]
    weights = [max(0.01, p.oos_sharpe + 1.0) for p in parents]
    return random.choices(parents, weights=weights, k=1)[0]


def build_evolved_seed(parents: List[Parent], *, operator: str = "mutate") -> Optional[EvolvedSeed]:
    """Apply one operator (a single researcher-grade LLM call reusing the
    researcher system prompt, so the output schema matches a fresh story).
    Returns None when the operator yields nothing usable or a byte-identical
    clone of the parent (caught pre-dispatch to avoid wasting a pipeline run)."""
    if not parents:
        return None
    op = operator if operator in _OPERATORS else "mutate"
    parent = parents[0]
    prompt = _OPERATORS[op].format(story=parent.alpha_story)
    new_story = call_messages(
        system=RESEARCHER_SYSTEM_PROMPT, user=prompt,
        max_tokens=operator_max_tokens(), temperature=0.4, agent="researcher",
    ).strip()
    if not new_story or new_story == parent.alpha_story.strip():
        return None
    return EvolvedSeed(
        alpha_story=new_story, source_node_ids=list(parent.source_node_ids),
        asset_symbol=parent.asset_symbol, operator=op,
        parent_ids=[parent.strategy_id], parent_depth_max=parent.depth,
    )


def _reject_strategy(sid: int, reason: str) -> None:
    try:
        with session_scope() as s:
            row = s.get(AlphaStrategy, sid)
            if row is not None:
                row.status = "REJECTED"
                row.team_b_review = str(reason)[:2000]
    except Exception:  # noqa: BLE001
        logger.exception("evolution: failed to reject sid=%s", sid)


def run_evolution_once(*, operator: Optional[str] = None) -> Dict[str, Any]:
    """Pick a surviving parent, mutate/specialise its story, and run the result
    through the SAME pipeline (same gates + budget). Never raises. Returns a
    status dict; callers fall back to fresh proposals on ``disabled``/``no_parents``."""
    if not is_enabled():
        return {"status": "disabled"}
    from backend.core import llm_budget, universe
    from backend.core.orchestrator import WorkflowOrchestrator

    try:
        parents = select_parents(k=parent_pool_size())
    except Exception as exc:  # noqa: BLE001
        logger.exception("evolution: parent selection failed")
        return {"status": "error", "error": str(exc)}
    if not parents:
        return {"status": "no_parents"}

    parent = _weighted_pick(parents)
    op = (operator or _operator_name())

    asset = parent.asset_symbol or "BTC"
    try:
        universe.ensure_price_data(asset)
    except Exception:  # noqa: BLE001 — fall back to the always-present BTC series
        asset = "BTC"

    orch = WorkflowOrchestrator(asset_symbol=asset)
    depth = parent.depth + 1
    lineage = {
        "evolution": True,
        "evolution_operator": op,
        "evolution_parent_ids": [parent.strategy_id],
        "evolution_depth": depth,
        "evolution_lineage_note": f"{op} of S#{parent.strategy_id}",
    }
    sid = orch.bootstrap_strategy("(evolution)", config_extra=lineage)
    try:
        # One scope spans BOTH the operator call and the pipeline so the whole
        # evolved run is charged cumulatively to the child's per-strategy cap.
        # run_pipeline_from_story opens its own (re-entrant, same-sid) scope —
        # harmless nesting; the spend is cleared once at the outer exit.
        with llm_budget.strategy_budget_scope(sid):
            seed = build_evolved_seed([parent], operator=op)
            if seed is None:
                _reject_strategy(sid, "evolution: operator produced no usable / non-clone seed")
                return {"status": "no_seed", "strategy_id": sid, "parent_ids": [parent.strategy_id]}
            yaml_raw = _extract_yaml_block(seed.alpha_story) or ""
            parsed = _parse_yaml(yaml_raw) if yaml_raw else None
            result = orch.run_pipeline_from_story(
                sid,
                alpha_story=seed.alpha_story,
                backtest_config_yaml=yaml_raw,
                backtest_config=parsed,
                config_yaml_invalid=bool(yaml_raw) and parsed is None,
                source_node_ids=seed.source_node_ids,
            )
        return {"status": "ran", "strategy_id": sid, "operator": op,
                "parent_ids": [parent.strategy_id], "depth": depth, "result": result}
    except Exception as exc:  # noqa: BLE001
        logger.exception("evolution: run failed for sid=%s", sid)
        _reject_strategy(sid, f"evolution failed: {exc}")
        return {"status": "error", "strategy_id": sid, "error": str(exc)}


def tick_evolution() -> Dict[str, Any]:
    """Periodic-task entry: run one evolved proposal IFF the coin-flip fires.
    Safe to call on every tick — a no-op unless EVOLUTION_ENABLED and the
    fraction coin-flip passes."""
    if not should_use_evolution():
        return {"status": "skipped"}
    return run_evolution_once()


__all__ = [
    "is_enabled",
    "evolution_fraction",
    "should_use_evolution",
    "select_parents",
    "build_evolved_seed",
    "run_evolution_once",
    "tick_evolution",
    "Parent",
    "EvolvedSeed",
]

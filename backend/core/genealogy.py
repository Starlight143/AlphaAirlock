"""Alpha genealogy derivations (P7-09 — /alpha-genealogy).

Builds the strategy family forest using **existing** columns (zero schema
change). The rule:

* A strategy ``C`` is a child of strategy ``P`` iff some
  ``KnowledgeNode`` ``n`` satisfies::

      n.id           ∈ C.config["source_node_ids"]
      n.kind         == 'past_alpha'   # postmortem node
      n.origin_strategy_id == P.id

* A strategy with no postmortem-kind source nodes is a *root*.

Three endpoints registered in :mod:`backend.app.main`:

* ``/api/alpha-genealogy/forest``       — nested forest + summary stats
* ``/api/alpha-genealogy/tree/{root}``  — subtree starting from a root
* ``/api/alpha-genealogy/lineage/{id}`` — ancestors + siblings + descendants
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from backend.core.database import KIND_PAST_ALPHA, AlphaStrategy, KnowledgeNode

logger = logging.getLogger("alpha.genealogy")

APPROVED_STATUSES: Set[str] = {"APPROVED", "PAPER_TRADE", "SMALL_CAPITAL", "LIVE"}
REJECTED_STATUSES: Set[str] = {"REJECTED", "GRAVEYARD", "PAUSED"}


def _load_parent_map(
    session: Session,
) -> Tuple[Dict[int, Tuple[int, int]], Dict[int, KnowledgeNode], Dict[int, "AlphaStrategy"]]:
    """Build {child_strategy_id: (parent_strategy_id, postmortem_node_id)}.

    Returns the parent map, a node-id-keyed dict of postmortem nodes
    (for the lineage endpoint's postmortem reference), and the pre-fetched
    strategies dict keyed by int id — so callers (build_forest) do not need
    to issue a second full-table scan against AlphaStrategy.
    """
    # Index every source node that carries a parent strategy id. The orchestrator
    # stamps origin_strategy_id on the source (past_alpha) nodes a strategy
    # consumed (see _maybe_create_postmortem_node), so the parent edge resolves
    # off those nodes — NOT off the postmortem node (whose id never appears in
    # any strategy's source_node_ids). Matches this module's docstring rule.
    pm_nodes: Dict[int, KnowledgeNode] = {
        int(n.id): n
        for n in session.query(KnowledgeNode)
        .filter(KnowledgeNode.kind == KIND_PAST_ALPHA)
        .filter(KnowledgeNode.origin_strategy_id.isnot(None))
        .all()
    }
    # Fetch all strategies once; reuse for both parent-map building and the
    # caller's node dict — avoids the duplicate full-table scan (B12-1).
    strategies: Dict[int, "AlphaStrategy"] = {
        int(s.id): s for s in session.query(AlphaStrategy).all()
    }
    # Walk every strategy's source_node_ids and pick the first postmortem hit.
    parent_map: Dict[int, Tuple[int, int]] = {}
    for sid, s in strategies.items():
        cfg = s.config() or {}
        source_ids = cfg.get("source_node_ids") or []
        if not isinstance(source_ids, list):
            continue
        for nid in source_ids:
            try:
                nid_int = int(nid)
            except (TypeError, ValueError):
                continue
            n = pm_nodes.get(nid_int)
            if n is None:
                continue
            # Skip source nodes that were stamped by this strategy itself (INTAKE
            # nodes promoted to past_alpha by _maybe_create_postmortem_node).
            # Without this guard, every first-generation strategy resolves to
            # itself as its own parent and gets a false 'cycle' badge.
            if int(n.origin_strategy_id) == sid:
                continue
            parent_map[sid] = (int(n.origin_strategy_id), nid_int)
            break  # first match wins
    return parent_map, pm_nodes, strategies


def _finite_or_zero(v: Any) -> float:
    """Coerce None/NaN/inf to 0.0 so the genealogy forest stays valid JSON.

    P34: ``float('nan') or 0.0`` returns NaN (NaN is truthy), so the prior
    ``... or 0.0`` idiom let non-finite metrics leak into the JSON payload —
    stdlib json.dumps emits a bare ``NaN`` token, which breaks JSON.parse.
    """
    try:
        f = float(v if v is not None else 0.0)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _finite_or_none(v: Any) -> Optional[float]:
    """Coerce a metric to a finite float, or None when missing/non-finite.

    P34: preserves the ``None``-for-missing semantics of the genealogy payload
    while ensuring NaN/inf never leak into the JSON (which json.dumps would emit
    as a bare ``NaN``/``Infinity`` token and break JSON.parse on the client).
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _strategy_to_tree_node(
    s: AlphaStrategy, *, parent_id: Optional[int], postmortem_id: Optional[int], depth: int
) -> Dict[str, Any]:
    metrics = s.metrics() or {}
    return {
        "id": int(s.id),
        "slug": s.slug(),
        "name": s.name or f"strategy_{s.id}",
        "stage": int(s.stage or 0),
        "status": s.status or "INTAKE",
        "ic_score": _finite_or_zero(metrics.get("annualized_sharpe", 0.0)),
        "sharpe": _finite_or_none(metrics.get("annualized_sharpe")),
        "max_drawdown": _finite_or_none(metrics.get("max_drawdown")),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        "parent_id": parent_id,
        "postmortem_node_id": postmortem_id,
        "depth": depth,
        "badges": [],
        "children": [],
    }


def _detect_cycles(parent_map: Dict[int, Tuple[int, int]]) -> Set[int]:
    """Return the set of strategy ids that participate in a cycle (data corruption)."""
    cycle_ids: Set[int] = set()
    for sid in parent_map:
        visited: Set[int] = {sid}
        path: List[int] = [sid]
        cur: Optional[int] = sid
        while True:
            link = parent_map.get(cur) if cur is not None else None
            if link is None:
                break
            pid = link[0]
            if pid in cycle_ids:
                # Already confirmed cyclic — no further traversal needed.
                break
            if pid in visited:
                # Only the nodes from the first repeated node onward are on the
                # cycle; pure lead-in ancestors (which point into the cycle but
                # are not part of it) must stay untagged.
                cycle_ids.update(path[path.index(pid):])
                break
            visited.add(pid)
            path.append(pid)
            cur = pid
    return cycle_ids


def _assign_badges(
    forest_roots: List[Dict[str, Any]], parent_map: Dict[int, Tuple[int, int]]
) -> None:
    """In-place tag fertile / improving / trapped / barren on every node."""
    # Flatten and compute child-count + best-descendant-status per node.
    flat: List[Dict[str, Any]] = []
    def walk(node: Dict[str, Any]) -> None:
        flat.append(node)
        for c in node["children"]:
            walk(c)
    for r in forest_roots:
        walk(r)

    def descendants(node: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        stack = list(node["children"])
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(cur["children"])
        return out

    for node in flat:
        descs = descendants(node)
        # fertile = >=2 descendants in APPROVED+
        approved_descs = [d for d in descs if d["status"] in APPROVED_STATUSES]
        if len(approved_descs) >= 2:
            node["badges"].append("fertile")
        # improving = every direct child has higher Sharpe than parent
        children = node["children"]
        if children:
            parent_sh = node["sharpe"] if node["sharpe"] is not None else float("-inf")
            if all(((c["sharpe"] or float("-inf")) > parent_sh) for c in children):
                node["badges"].append("improving")
        # trapped = >=3 consecutive descendant rejections along a single path.
        # Walk every path independently: a non-rejected node resets to 0 for that
        # branch only; siblings continue their own chains unaffected.
        def _min_rejection_chain(n: Dict[str, Any]) -> int:
            """Return the shortest consecutive-rejection chain across all branches.

            Uses min() so that a non-rejected sibling (returning 0) breaks the
            chain: a lineage is only 'trapped' if EVERY branch is stuck in
            consecutive rejections — a surviving branch means the line escaped.
            """
            if n["status"] not in REJECTED_STATUSES:
                return 0
            if not n["children"]:
                return 1
            return 1 + min(_min_rejection_chain(ch) for ch in n["children"])

        for c in children:
            if _min_rejection_chain(c) >= 3:
                node["badges"].append("trapped")
                break
        # barren = self is leaf and rejected
        if not children and node["status"] in REJECTED_STATUSES and node["depth"] > 0:
            node["badges"].append("barren")


def build_forest(session: Session) -> Dict[str, Any]:
    """Full forest + summary stats."""
    # _load_parent_map now fetches AlphaStrategy once and returns the dict;
    # we reuse it here to avoid the second full-table scan (fix B12-1).
    parent_map, pm_nodes, strategies = _load_parent_map(session)
    cycle_ids = _detect_cycles(parent_map)
    # First-pass: instantiate tree nodes.
    nodes: Dict[int, Dict[str, Any]] = {}
    for sid, s in strategies.items():
        link = parent_map.get(sid)
        parent_id = link[0] if link else None
        pm_id = link[1] if link else None
        nodes[sid] = _strategy_to_tree_node(s, parent_id=parent_id, postmortem_id=pm_id, depth=0)
        if sid in cycle_ids:
            nodes[sid]["badges"].append("cycle")

    # Second-pass: hang children, set depth.
    roots: List[Dict[str, Any]] = []
    for sid, node in nodes.items():
        if node["parent_id"] is None or sid in cycle_ids:
            roots.append(node)
        else:
            parent = nodes.get(node["parent_id"])
            if parent is None:
                # Parent doesn't exist (orphan postmortem reference) → treat as root.
                roots.append(node)
                continue
            parent["children"].append(node)

    # Compute depth recursively.
    def set_depth(node: Dict[str, Any], d: int) -> None:
        node["depth"] = d
        for c in node["children"]:
            set_depth(c, d + 1)
    for r in roots:
        set_depth(r, 0)

    # Sort: roots by created_at desc, children by created_at asc.
    roots.sort(key=lambda n: n.get("created_at") or "", reverse=True)
    def sort_children(node: Dict[str, Any]) -> None:
        node["children"].sort(key=lambda n: n.get("created_at") or "")
        for c in node["children"]:
            sort_children(c)
    for r in roots:
        sort_children(r)

    _assign_badges(roots, parent_map)

    # Summary stats.
    def collect(node: Dict[str, Any], buf: List[Dict[str, Any]]) -> None:
        buf.append(node)
        for c in node["children"]:
            collect(c, buf)
    all_nodes: List[Dict[str, Any]] = []
    for r in roots:
        collect(r, all_nodes)

    n_fertile = sum(1 for n in all_nodes if "fertile" in n["badges"])
    n_improving = sum(1 for n in all_nodes if "improving" in n["badges"])
    n_trapped = sum(1 for n in all_nodes if "trapped" in n["badges"])
    n_barren = sum(1 for n in all_nodes if "barren" in n["badges"])
    max_depth = max((n["depth"] for n in all_nodes), default=0)

    return {
        "trees": roots,
        "stats": {
            "n_roots": len(roots),
            "n_strategies": len(all_nodes),
            "max_depth": max_depth,
            "n_fertile": n_fertile,
            "n_improving": n_improving,
            "n_trapped": n_trapped,
            "n_barren": n_barren,
            "n_cycle": len(cycle_ids),
        },
    }


def get_tree(session: Session, root_strategy_id: int) -> Dict[str, Any]:
    forest = build_forest(session)
    for r in forest["trees"]:
        if r["id"] == int(root_strategy_id):
            return {"tree": r}
    return {"tree": None}


def get_lineage(session: Session, strategy_id: int) -> Dict[str, Any]:
    """Ancestors + self + siblings + descendants for a single strategy."""
    forest = build_forest(session)
    target: Optional[Dict[str, Any]] = None
    parent_chain: List[Dict[str, Any]] = []

    def find(node: Dict[str, Any], path: List[Dict[str, Any]]) -> bool:
        nonlocal target, parent_chain
        if node["id"] == int(strategy_id):
            target = node
            parent_chain = list(path)
            return True
        for c in node["children"]:
            if find(c, path + [node]):
                return True
        return False

    for r in forest["trees"]:
        if find(r, []):
            break

    if target is None:
        return {"target": None, "ancestors": [], "siblings": [], "descendants": []}

    siblings: List[Dict[str, Any]] = []
    if parent_chain:
        immediate_parent = parent_chain[-1]
        siblings = [c for c in immediate_parent["children"] if c["id"] != target["id"]]

    def flat_descendants(node: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for c in node["children"]:
            out.append(c)
            out.extend(flat_descendants(c))
        return out

    return {
        "target": target,
        "ancestors": parent_chain,
        "siblings": siblings,
        "descendants": flat_descendants(target),
    }


__all__ = ["build_forest", "get_tree", "get_lineage"]

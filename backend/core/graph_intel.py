"""T2-A / T2-C — knowledge-graph intelligence.

Two LLM-free capabilities layered on graph_retrieval's cached DiGraph:

  * **Bridge / gap analytics** — bridge nodes (high betweenness on the undirected
    projection) link otherwise-disconnected concept clusters and are the richest
    sites for novel cross-domain hypotheses; gap categories (many concepts, few
    realised past_alpha nodes) flag under-explored factor families. ``seed_node_
    ids_for_auto`` preferentially augments the auto-pipeline's seed with bridge
    nodes — env-gated, with a strict fallback to today's selection.

  * **KB vital signs** — diversity_index (category entropy), gap_pressure, orphan
    rate, connectivity, bridge count — a first-class health snapshot for the
    Mission Control rail and the diversity-gate context.

The expensive betweenness pass is memoised on (node_count, edge_count) so it runs
only when graph_retrieval rebuilds its graph, never per request. Everything here
falls back to a zero-filled cold snapshot (never raises) when networkx is absent
or the graph is empty.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from backend._envloader import env_bool, env_float, env_int
from backend.core import graph_retrieval


# --------------------------------------------------------------------------- #
# Config (env-overridable; defaults reproduce today's behaviour)              #
# --------------------------------------------------------------------------- #

def is_graph_seed_enabled() -> bool:
    """When False (default) seed_node_ids_for_auto is the identity — today's
    selection is byte-unchanged."""
    return env_bool("GRAPH_RAG_SEED_SELECTION", False)


def _seed_max_extra() -> int:
    return env_int("GRAPH_RAG_SEED_MAX_EXTRA", 3, minimum=0, maximum=20)


def _bridge_top_n() -> int:
    return env_int("GRAPH_INTEL_BRIDGE_TOP_N", 50, minimum=1, maximum=1000)


def _bridge_betweenness_min() -> float:
    return env_float("GRAPH_INTEL_BRIDGE_BETWEENNESS_MIN", 0.01, minimum=0.0, maximum=1.0)


def _gap_min_concepts() -> int:
    return env_int("GRAPH_INTEL_GAP_MIN_CONCEPTS", 3, minimum=1, maximum=10_000)


def _gap_pressure_min() -> float:
    return env_float("GRAPH_INTEL_GAP_PRESSURE_MIN", 4.0, minimum=0.0, maximum=1e6)


def _exact_bc_max() -> int:
    return env_int("GRAPH_INTEL_EXACT_BC_MAX", 4000, minimum=1, maximum=1_000_000)


def _bc_sample_k() -> int:
    return env_int("GRAPH_INTEL_BC_SAMPLE_K", 500, minimum=1, maximum=100_000)


def _bc_seed() -> int:
    return env_int("GRAPH_INTEL_BC_SEED", 1337, minimum=0, maximum=2_000_000_000)


# --------------------------------------------------------------------------- #
# Result                                                                      #
# --------------------------------------------------------------------------- #

@dataclass
class GraphIntel:
    diversity_index: float
    gap_pressure: float
    orphan_rate: float
    connectivity: float
    largest_component_fraction: float
    bridge_count: int
    node_count: int
    edge_count: int
    category_count: int
    bridges: List[Dict[str, Any]] = field(default_factory=list)
    gaps: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "diversity_index": round(self.diversity_index, 4),
            "gap_pressure": round(self.gap_pressure, 4),
            "orphan_rate": round(self.orphan_rate, 4),
            "connectivity": round(self.connectivity, 6),
            "largest_component_fraction": round(self.largest_component_fraction, 4),
            "bridge_count": self.bridge_count,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "category_count": self.category_count,
            "bridges": list(self.bridges),
            "gaps": list(self.gaps),
        }


_INTEL_LOCK = threading.RLock()
_INTEL_CACHE: Dict[str, Any] = {"key": None, "intel": None}


def _zero_intel() -> GraphIntel:
    return GraphIntel(0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, 0, 0, [], [])


def _get_cached_graph(session: Session):
    """Reuse graph_retrieval's cached DiGraph (warm-path is a dict lookup; cold
    path builds once). Returns None on any failure — never raises."""
    try:
        return graph_retrieval._ensure_graph(session)  # noqa: SLF001 — intended reuse
    except Exception:  # noqa: BLE001
        return None


def _compute_analytics(G) -> GraphIntel:
    """Single pass producing bridges + gaps + vital signs. Memoised on the
    graph's (node_count, edge_count) so betweenness runs only on rebuild."""
    if G is None or G.number_of_nodes() == 0:
        return _zero_intel()
    try:
        import networkx as nx
    except ImportError:
        return _zero_intel()

    n_nodes = G.number_of_nodes()
    n_edges = G.number_of_edges()
    key = (n_nodes, n_edges)
    with _INTEL_LOCK:
        if _INTEL_CACHE["key"] == key and _INTEL_CACHE["intel"] is not None:
            return _INTEL_CACHE["intel"]

    U = G.to_undirected(as_view=True)

    # Bridge metric: betweenness centrality, exact for normal graphs, k-sampled
    # (fixed seed → deterministic) above the size cap.
    if U.number_of_nodes() > _exact_bc_max():
        bc = nx.betweenness_centrality(
            U, k=min(_bc_sample_k(), U.number_of_nodes()),
            normalized=True, seed=_bc_seed())
    else:
        bc = nx.betweenness_centrality(U, normalized=True)

    # Category tally over concept + past_alpha nodes.
    concept_by_cat: Dict[str, int] = {}
    realized_by_cat: Dict[str, int] = {}
    cat_counts: Dict[str, int] = {}
    for _nid, data in G.nodes(data=True):
        kind = (data.get("kind") or "concept").lower()
        cat = data.get("category") or "uncategorized"
        if kind in ("concept", "past_alpha"):
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        if kind == "concept":
            concept_by_cat[cat] = concept_by_cat.get(cat, 0) + 1
        elif kind == "past_alpha":
            realized_by_cat[cat] = realized_by_cat.get(cat, 0) + 1

    # diversity_index = normalised Shannon entropy of the category distribution.
    total = sum(cat_counts.values())
    n_cats = sum(1 for c in cat_counts.values() if c > 0)
    diversity_index = 0.0
    if total > 0 and n_cats > 1:
        H = 0.0
        for cnt in cat_counts.values():
            if cnt > 0:
                p = cnt / total
                H -= p * math.log(p)
        denom = math.log(n_cats)
        diversity_index = (H / denom) if (denom > 1e-14) else 0.0

    # Gap pressure per category (Laplace-smoothed: concepts / (realised + 1)).
    gaps: List[Dict[str, Any]] = []
    gap_min = _gap_min_concepts()
    gap_thr = _gap_pressure_min()
    max_pressure = 0.0
    for cat, ccount in concept_by_cat.items():
        rcount = realized_by_cat.get(cat, 0)
        pressure = ccount / (rcount + 1)
        max_pressure = max(max_pressure, pressure)
        if ccount >= gap_min and pressure >= gap_thr:
            gaps.append({
                "category": cat, "concept_count": ccount,
                "realized_count": rcount, "gap_pressure": round(pressure, 3),
            })
    gaps.sort(key=lambda g: g["gap_pressure"], reverse=True)

    # Bridges (concept/past_alpha only — we seed NEW hypotheses from these).
    bmin = _bridge_betweenness_min()
    bridges: List[Dict[str, Any]] = []
    bridge_count = 0
    for nid, data in G.nodes(data=True):
        score = float(bc.get(nid, 0.0))
        if score >= bmin:
            bridge_count += 1
        kind = (data.get("kind") or "concept").lower()
        if kind in ("concept", "past_alpha"):
            bridges.append({
                "node_id": int(nid), "score": round(score, 5),
                "degree": int(U.degree(nid)), "category": data.get("category") or None,
            })
    # Deterministic: highest score, then highest degree, then lowest id.
    bridges.sort(key=lambda b: (-b["score"], -b["degree"], b["node_id"]))
    bridges = bridges[:_bridge_top_n()]

    orphans = sum(1 for nid in U.nodes() if U.degree(nid) == 0)
    orphan_rate = orphans / n_nodes if n_nodes else 0.0
    connectivity = float(nx.density(U))
    largest = max((len(c) for c in nx.connected_components(U)), default=0)
    largest_frac = largest / n_nodes if n_nodes else 0.0

    intel = GraphIntel(
        diversity_index=diversity_index, gap_pressure=max_pressure,
        orphan_rate=orphan_rate, connectivity=connectivity,
        largest_component_fraction=largest_frac, bridge_count=bridge_count,
        node_count=n_nodes, edge_count=n_edges, category_count=n_cats,
        bridges=bridges, gaps=gaps,
    )
    with _INTEL_LOCK:
        _INTEL_CACHE["key"] = key
        _INTEL_CACHE["intel"] = intel
    return intel


def invalidate_intel_cache() -> None:
    """Drop the analytics cache. Call alongside graph_retrieval.invalidate_graph_cache."""
    with _INTEL_LOCK:
        _INTEL_CACHE["key"] = None
        _INTEL_CACHE["intel"] = None


def vital_signs(session: Optional[Session] = None) -> Dict[str, Any]:
    """Cached KB health snapshot. Never raises — returns a zero-filled 'cold'
    dict when networkx/graph are unavailable."""
    try:
        if session is None:
            from backend.core.database import session_scope
            with session_scope() as s:
                return _compute_analytics(_get_cached_graph(s)).to_dict()
        return _compute_analytics(_get_cached_graph(session)).to_dict()
    except Exception:  # noqa: BLE001
        return _zero_intel().to_dict()


def seed_node_ids_for_auto(
    base_node_ids: List[int], *, limit: Optional[int] = None,
    session: Optional[Session] = None,
) -> List[int]:
    """Augment ``base_node_ids`` with up to ``limit`` extra BRIDGE context nodes
    (novel cross-domain connectors). DETERMINISTIC; never raises; never removes a
    base id. Falls back to ``base_node_ids`` unchanged when disabled, the graph
    is empty, or no bridges are found."""
    base = [int(x) for x in (base_node_ids or [])]
    if not is_graph_seed_enabled():
        return list(base)
    cap = _seed_max_extra() if limit is None else max(0, int(limit))
    if cap <= 0:
        return list(base)
    try:
        if session is None:
            from backend.core.database import session_scope
            with session_scope() as s:
                intel = _compute_analytics(_get_cached_graph(s))
        else:
            intel = _compute_analytics(_get_cached_graph(session))
    except Exception:  # noqa: BLE001
        return list(base)
    if intel is None or intel.node_count == 0 or not intel.bridges:
        return list(base)
    base_set = set(base)
    extras: List[int] = []
    for b in intel.bridges:
        if len(extras) >= cap:
            break
        nid = int(b["node_id"])
        if nid not in base_set and nid not in extras:
            extras.append(nid)
    return list(base) + extras


__all__ = [
    "GraphIntel",
    "is_graph_seed_enabled",
    "vital_signs",
    "seed_node_ids_for_auto",
    "invalidate_intel_cache",
]

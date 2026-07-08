"""Graph RAG retrieval engine for AI Alpha System (P6-D04 upgrade).

Replaces flat TF-IDF cosine scan with a two-phase Graph RAG approach:

  Phase 1 – Anchor finding
    TF-IDF entry-point nodes via ``semantic_retrieval._tfidf_search``.
    Re-uses the existing index cache — no double build.

  Phase 2 – Graph expansion
    Weighted best-first BFS from anchors via two edge kinds:
      • granger_edges   — statistically-validated causal links
                         weight = max(0.50, 1 − p_value)   directed A → B
      • node.links JSON — TF-IDF semantic similarity links
                         weight = 0.35                      bidirectional A ↔ B
                         (skipped if a Granger edge already claims the slot)

Score propagation per BFS step:
  child_score = parent_score × edge_weight × HOP_DECAY (0.60)
IC quality boost on arrival (up to +30 %):
  child_score *= 1 + min(ic_score, 1.0) × 0.30

Public API — identical contract to ``semantic_retrieval``:
  search(query, *, top_k=8, include_categories=None, session=None) → List[SearchHit]
  invalidate_graph_cache() → None
  is_graph_rag_enabled() → bool

Environment variables (all ``GRAPH_RAG_*`` prefix):
  GRAPH_RAG_ENABLED        bool   default True   master switch
  GRAPH_RAG_P_THRESHOLD    float  default 0.10   max p-value for Granger edges
  GRAPH_RAG_MAX_HOPS       int    default 2      max BFS depth from anchor
  GRAPH_RAG_ANCHOR_K       int    default 3      number of TF-IDF anchor nodes
  GRAPH_RAG_EXPAND_BUDGET  int    default 80     max nodes explored per query
  GRAPH_RAG_CACHE_TTL      int    default 1800   graph rebuild age limit (s)
  GRAPH_RAG_REBUILD_PCT    float  default 5.0    rebuild if node count drifts ≥ X %
"""

from __future__ import annotations

import heapq
import json
import logging
import re
import threading
import time
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from backend._envloader import env_bool, env_float, env_int
from backend.core.database import GrangerEdge, KnowledgeNode, session_scope
from backend.core.semantic_retrieval import (  # reuse same public dataclass + boost fns
    SearchHit,
    _category_boost as _cat_boost_fn,
    _tag_boost_each as _tag_boost_fn,
)

logger = logging.getLogger("alpha.graph_retrieval")

# ── Hardcoded traversal constants ──────────────────────────────────────────────
_HOP_DECAY        = 0.60    # score multiplier per BFS hop
_IC_BOOST_FACTOR  = 0.30    # ic_score ∈ (0,1] boosts arrival score by up to +30 %
_GRANGER_W_FLOOR  = 0.50    # minimum edge weight for any Granger edge
_SEMANTIC_W       = 0.35    # fixed weight for node.links (TF-IDF similarity) edges
# _CATEGORY_BOOST and _TAG_BOOST_EACH removed — use _cat_boost_fn() / _tag_boost_fn()
# (env vars SEMANTIC_RETRIEVAL_CATEGORY_BOOST / SEMANTIC_RETRIEVAL_TAG_BOOST) so both
# the graph-RAG path and the TF-IDF fallback path honour the same operator tuning.


# ── Env-driven tunables ────────────────────────────────────────────────────────

def is_graph_rag_enabled() -> bool:
    return env_bool("GRAPH_RAG_ENABLED", True)


def _p_threshold() -> float:
    return env_float("GRAPH_RAG_P_THRESHOLD", 0.10, minimum=0.001, maximum=0.50)


def _max_hops() -> int:
    return env_int("GRAPH_RAG_MAX_HOPS", 2, minimum=1, maximum=4)


def _anchor_k() -> int:
    return env_int("GRAPH_RAG_ANCHOR_K", 3, minimum=1, maximum=10)


def _expand_budget() -> int:
    return env_int("GRAPH_RAG_EXPAND_BUDGET", 80, minimum=10, maximum=500)


def _cache_ttl() -> int:
    return env_int("GRAPH_RAG_CACHE_TTL", 1800, minimum=60)


def _rebuild_pct() -> float:
    return env_float("GRAPH_RAG_REBUILD_PCT", 5.0, minimum=0.5)


# ── Graph cache (process-lifetime, RLock-protected) ────────────────────────────
# Follows the same pattern as semantic_retrieval._INDEX / _INDEX_LOCK.
_GRAPH_LOCK = threading.RLock()
_GRAPH_CACHE: Dict = {
    "built_at":   0.0,
    "graph":      None,   # nx.DiGraph | None
    "node_count": 0,
    "edge_count": 0,
}


# ── Graph construction ─────────────────────────────────────────────────────────

def _build_graph(session: Session):
    """Build a fresh NetworkX DiGraph.  Called *inside* _GRAPH_LOCK.

    Returns a populated DiGraph on success, None on failure (networkx missing,
    empty DB, etc.).
    """
    try:
        import networkx as nx
    except ImportError:
        logger.warning("graph_retrieval: networkx not installed — graph RAG disabled")
        return None

    G: nx.DiGraph = nx.DiGraph()

    # ── 1. Nodes ───────────────────────────────────────────────────────────────
    rows = session.query(
        KnowledgeNode.id,
        KnowledgeNode.title,
        KnowledgeNode.ic_score,
        KnowledgeNode.kind,
        KnowledgeNode.category,
        KnowledgeNode.links,
    ).all()

    if not rows:
        return None

    for r in rows:
        G.add_node(
            r.id,
            title=r.title or "",
            ic_score=float(r.ic_score) if r.ic_score is not None else 0.0,
            kind=r.kind or "concept",
            category=r.category or "",
        )

    # ── 2. Granger causal edges (directed, high-quality) ──────────────────────
    p_thr = _p_threshold()
    granger_rows = (
        session.query(GrangerEdge)
        .filter(GrangerEdge.p_value < p_thr)
        .all()
    )
    granger_count = 0
    for e in granger_rows:
        if not G.has_node(e.src_node_id) or not G.has_node(e.dst_node_id):
            continue
        weight = max(_GRANGER_W_FLOOR, 1.0 - float(e.p_value))
        # Multiple lags may produce duplicate pairs — keep the highest weight.
        if G.has_edge(e.src_node_id, e.dst_node_id):
            if G[e.src_node_id][e.dst_node_id].get("weight", 0.0) >= weight:
                continue
        G.add_edge(
            e.src_node_id, e.dst_node_id,
            weight=weight,
            edge_type="granger",
            p_value=float(e.p_value),
            lag=int(e.lag),
        )
        granger_count += 1

    # ── 3. Semantic similarity edges (bidirectional, from node.links JSON) ─────
    semantic_count = 0
    for r in rows:
        raw = (r.links or "").strip()
        if not raw or raw == "[]":
            continue
        try:
            link_ids: List[int] = [int(x) for x in json.loads(raw) if x is not None]
        except (ValueError, TypeError, json.JSONDecodeError):
            continue

        for link_id in link_ids:
            if not G.has_node(link_id):
                continue
            # Forward: r.id → link_id (skip if Granger already owns this slot)
            if not (
                G.has_edge(r.id, link_id)
                and G[r.id][link_id].get("edge_type") == "granger"
            ):
                G.add_edge(r.id, link_id, weight=_SEMANTIC_W, edge_type="semantic")
                semantic_count += 1
            # Reverse: link_id → r.id (bidirectional — semantic similarity is symmetric)
            if not (
                G.has_edge(link_id, r.id)
                and G[link_id][r.id].get("edge_type") == "granger"
            ):
                G.add_edge(link_id, r.id, weight=_SEMANTIC_W, edge_type="semantic")

    n_nodes = G.number_of_nodes()
    n_edges = G.number_of_edges()
    logger.info(
        "graph_retrieval: graph built — %d nodes, %d edges "
        "(%d granger p<%.2f, %d semantic)",
        n_nodes, n_edges, granger_count, p_thr, semantic_count,
    )
    return G if n_nodes > 0 else None


def _ensure_graph(session: Session):
    """Return the cached DiGraph, rebuilding if stale.  Thread-safe via RLock."""
    # Fetch node count outside the lock to avoid blocking readers during I/O.
    node_count = session.query(KnowledgeNode.id).count()

    with _GRAPH_LOCK:
        # Fast-path: validate cache without rebuilding.
        # Also applies when graph is None (networkx absent / empty DB) — the
        # None result itself is cached for the full TTL so we do not hammer
        # the DB on every call during a permanent-failure condition.
        if _GRAPH_CACHE["built_at"] > 0.0:
            age = time.monotonic() - _GRAPH_CACHE["built_at"]
            prior = _GRAPH_CACHE["node_count"]
            if prior > 0:
                delta_pct = abs(node_count - prior) / prior * 100.0
            else:
                delta_pct = 100.0  # force rebuild if prior count is unknown
            if age <= _cache_ttl() and delta_pct <= _rebuild_pct():
                return _GRAPH_CACHE["graph"]
        # Mark that a rebuild is needed, then release the lock before I/O.
        _GRAPH_CACHE["graph"] = None      # signal rebuild in progress
        _GRAPH_CACHE["built_at"] = 0.0    # prevent concurrent readers from treating stale None as a valid cached result

    # Slow-path: build graph OUTSIDE the lock to avoid convoy starvation.
    # If two threads race here, the second write below is idempotent —
    # both produce equivalent graphs from the same DB snapshot.
    G = _build_graph(session)

    with _GRAPH_LOCK:
        # Double-checked: only write if cache is still unset (another thread
        # may have already populated it while we were building).
        if _GRAPH_CACHE["graph"] is None:
            _GRAPH_CACHE["graph"]      = G
            _GRAPH_CACHE["built_at"]   = time.monotonic()
            _GRAPH_CACHE["node_count"] = G.number_of_nodes() if G else 0
            _GRAPH_CACHE["edge_count"] = G.number_of_edges() if G else 0
    return _GRAPH_CACHE["graph"]


def invalidate_graph_cache() -> None:
    """Force a full graph rebuild on the next ``search()`` call.

    Callers: ``kb_relink.relink_node`` (after edge mutations),
             future callers: post-Granger-run, post-bulk-ingest.
    """
    with _GRAPH_LOCK:
        _GRAPH_CACHE["built_at"] = 0.0
        _GRAPH_CACHE["graph"]    = None
    logger.debug("graph_retrieval: cache invalidated")


# ── Graph traversal ────────────────────────────────────────────────────────────

def _weighted_bfs(
    G,
    anchor_scores: Dict[int, float],
    max_hops: int,
    budget: int,
) -> Dict[int, float]:
    """Best-first BFS from anchor nodes.

    Each edge traversal: child_score = parent_score × edge_weight × HOP_DECAY.
    IC quality boost applied on arrival.

    Returns {node_id: best_composite_score} for every visited node.
    """
    # Heap entries: (-score, node_id, hop_depth)
    heap: list = [
        (-score, nid, 0)
        for nid, score in anchor_scores.items()
        if G.has_node(nid)
    ]
    heapq.heapify(heap)

    visited: Dict[int, float] = {}

    while heap and len(visited) < budget:
        neg_score, node_id, hop = heapq.heappop(heap)
        if node_id in visited:
            continue

        score = -neg_score
        visited[node_id] = score

        if hop >= max_hops:
            continue

        for neighbor_id in G.successors(node_id):
            if neighbor_id in visited:
                continue

            edge_w  = float(G[node_id][neighbor_id].get("weight", _SEMANTIC_W))
            n_score = score * edge_w * _HOP_DECAY

            # CLAUDE.md float rule: guard subnormals before division/multiplication.
            n_ic = float(G.nodes[neighbor_id].get("ic_score", 0.0))
            if n_ic > 1e-14:
                n_score *= 1.0 + min(n_ic, 1.0) * _IC_BOOST_FACTOR

            heapq.heappush(heap, (-n_score, neighbor_id, hop + 1))

    return visited


# ── Public API ─────────────────────────────────────────────────────────────────

def search(
    query: str,
    *,
    top_k: int = 8,
    include_categories: Optional[Sequence[str]] = None,
    session: Optional[Session] = None,
) -> List[SearchHit]:
    """Graph RAG search: TF-IDF anchor finding + weighted BFS graph expansion.

    Algorithm
    ---------
    1. Ensure graph cache is fresh (rebuild if age > TTL or node count drifts).
    2. Run TF-IDF anchor search → GRAPH_RAG_ANCHOR_K entry-point nodes.
    3. Expand subgraph via best-first BFS along granger + semantic edges.
    4. Apply category / tag boosts (identical coefficients to semantic_retrieval).
    5. Return top_k SearchHits sorted by composite score descending.

    Degrades gracefully:
    • networkx missing or DB empty → []
    • No TF-IDF anchors found     → []
    • Anchors outside graph range  → returns TF-IDF hits directly ([:top_k])
    • Any unhandled exception      → [] (never raises)
    """
    q = (query or "").strip()
    if not q:
        return []

    own_session = session is None
    _ctx = None
    _ctx_entered = False
    if own_session:
        _ctx = session_scope()
        session = _ctx.__enter__()
        _ctx_entered = True

    try:
        # ── 1. Graph ──────────────────────────────────────────────────────────
        G = _ensure_graph(session)
        if G is None:
            return []

        # ── 2. TF-IDF anchor nodes ────────────────────────────────────────────
        # Lazy import avoids circular dependency at module-load time.
        # semantic_retrieval is fully loaded before this function is ever called
        # (it is the module that routes to us via its own search() wrapper).
        from backend.core.semantic_retrieval import _tfidf_search  # noqa: PLC0415

        anchor_hits = _tfidf_search(
            q,
            top_k=_anchor_k(),
            include_categories=include_categories,
            session=session,
        )
        if not anchor_hits:
            return []

        anchor_scores: Dict[int, float] = {
            h.node_id: h.score
            for h in anchor_hits
            if G.has_node(h.node_id)
        }
        if not anchor_scores:
            # Graph is stale or anchor nodes are outside the indexed set —
            # degrade to TF-IDF results rather than returning nothing.
            return anchor_hits[:top_k]

        # ── 3. Graph expansion ────────────────────────────────────────────────
        subgraph = _weighted_bfs(
            G,
            anchor_scores,
            max_hops=_max_hops(),
            budget=_expand_budget(),
        )

        # ── 4. Category / tag boosts + metadata fetch ─────────────────────────
        cat_set = set(include_categories) if include_categories else set()
        q_lower = q.lower()
        # Whole-token set mirrors semantic_retrieval._tfidf_search so the graph
        # path and the TF-IDF fallback rank identical input identically.
        q_tokens = {w.lower() for w in re.findall(r"\w+", q)}

        meta_rows = (
            session.query(
                KnowledgeNode.id,
                KnowledgeNode.title,
                KnowledgeNode.category,
                KnowledgeNode.tags,
            )
            .filter(KnowledgeNode.id.in_(list(subgraph.keys())))
            .all()
        )
        meta: Dict[int, object] = {r.id: r for r in meta_rows}

        hits: List[SearchHit] = []
        for node_id, score in subgraph.items():
            row = meta.get(node_id)
            if row is None:
                continue

            final = score
            why: List[str] = []

            if cat_set and row.category and row.category in cat_set:
                final += _cat_boost_fn()
                why.append(f"category={row.category}")

            tag_list = [t.strip() for t in (row.tags or "").split(",") if t.strip()]
            matched_tags = [t for t in tag_list if t.lower() in q_tokens]
            if matched_tags:
                final += _tag_boost_fn() * len(matched_tags)
                why.append(f"tags={','.join(matched_tags)}")

            why.append("anchor" if node_id in anchor_scores else "graph-expanded")

            hits.append(SearchHit(
                node_id=node_id,
                score=round(final, 6),
                title=row.title or "",
                category=row.category,
                why_matched=why,
            ))

        hits = [h for h in hits if h.score > 0.0]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    except Exception:  # noqa: BLE001
        logger.exception("graph_retrieval.search failed for query=%r", q)
        return []
    finally:
        if own_session and _ctx_entered:
            try:
                _ctx.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


__all__ = [
    "SearchHit",
    "invalidate_graph_cache",
    "is_graph_rag_enabled",
    "search",
]

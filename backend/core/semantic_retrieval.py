"""TF-IDF semantic retrieval (P6-D04).

Lightweight alternative to sentence-transformers (avoids 3 GB torch download
on a single-user SQLite deployment). When the user says "tell me about
funding-rate arbitrage", this lets the system pull the top-K most relevant
``KnowledgeNode`` rows by cosine similarity over TF-IDF vectors — same idea
as the reference YouTube demo, much smaller blast radius.

Implementation notes
--------------------
* **Process-lifetime index cache**: rebuilds when the node count changes by
  more than ``REBUILD_THRESHOLD_PCT`` (default 10%) or the cache is older
  than ``REBUILD_AGE_SECONDS`` (default 1 h).
* **Category + tag boosts**: a hit gets ``+0.15`` for sharing a category
  with the query (when ``include_categories`` is non-empty) and ``+0.05``
  per shared tag with the query string. This lets the user steer retrieval
  toward a specific knowledge space without per-search re-indexing.
* **Lazy sklearn import** — module import succeeds even if sklearn is gone,
  with the search function returning an empty result. The retrieval endpoint
  surfaces a 412 in that case so the frontend can hint at the missing dep.
* **Default OFF for *automatic* triggers**: ``SEMANTIC_RETRIEVAL_ENABLED``
  controls only whether the orchestrator's auto-pipeline path uses semantic
  retrieval. The explicit ``POST /api/knowledge/semantic-search`` endpoint
  always works (it's a manual operator action with no recurring cost).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from backend._envloader import env_bool, env_float, env_int, env_str
from backend.core.database import KnowledgeNode, session_scope

logger = logging.getLogger("alpha.semantic")

# Defaults — tunable by env.
REBUILD_THRESHOLD_PCT = 10.0
REBUILD_AGE_SECONDS = 60 * 60
MAX_INDEX_NODES = 5000


@dataclass
class SearchHit:
    node_id: int
    score: float
    title: str
    category: Optional[str]
    why_matched: List[str]


# ---- Index cache ----------------------------------------------------------

# P13/D-M1 — RLock (not bare bool) so concurrent /api/knowledge/semantic-search
# requests cannot race the TF-IDF fit. RLock lets the rebuild path safely
# re-enter helper functions that themselves take the lock.
_INDEX_LOCK = threading.RLock()
_INDEX: dict = {
    "built_at": 0.0,
    "node_ids": [],          # type: List[int]
    "node_meta": {},         # type: dict[int, dict]
    "vectorizer": None,
    "matrix": None,
    "node_count_at_build": 0,
}


def is_enabled() -> bool:
    return env_bool("SEMANTIC_RETRIEVAL_ENABLED", False)


def _max_index() -> int:
    return env_int("SEMANTIC_RETRIEVAL_MAX_NODES", MAX_INDEX_NODES, minimum=50, maximum=50_000)


def _rebuild_threshold() -> float:
    return env_float("SEMANTIC_RETRIEVAL_REBUILD_PCT", REBUILD_THRESHOLD_PCT, minimum=0.0)


def _rebuild_age_seconds() -> int:
    return env_int("SEMANTIC_RETRIEVAL_REBUILD_AGE", REBUILD_AGE_SECONDS, minimum=60)


def _category_boost() -> float:
    return env_float("SEMANTIC_RETRIEVAL_CATEGORY_BOOST", 0.15, minimum=0.0, maximum=1.0)


def _tag_boost_each() -> float:
    return env_float("SEMANTIC_RETRIEVAL_TAG_BOOST", 0.05, minimum=0.0, maximum=0.5)


def _ngram_max() -> int:
    return env_int("SEMANTIC_RETRIEVAL_NGRAM_MAX", 2, minimum=1, maximum=3)


def _index_is_stale(current_count: int) -> bool:
    if _INDEX["vectorizer"] is None:
        return True
    age = time.time() - float(_INDEX["built_at"])
    if age > _rebuild_age_seconds():
        return True
    prior = max(1, int(_INDEX["node_count_at_build"]))
    delta_pct = abs(current_count - prior) / prior * 100.0
    return delta_pct > _rebuild_threshold()


def _build_index(session: Session) -> bool:
    """Fit a fresh TF-IDF index over the most-recent N KnowledgeNodes.

    Returns True if successful. Logs and returns False on any failure
    (e.g. sklearn missing, empty corpus).
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError:
        logger.warning("semantic_retrieval: sklearn missing — index build skipped")
        return False

    rows = (
        session.query(KnowledgeNode)
        .order_by(KnowledgeNode.id.desc())
        .limit(_max_index())
        .all()
    )
    docs: List[Tuple[int, str]] = []
    meta: dict = {}
    for n in rows:
        text = (n.content or "").strip() or n.title or ""
        if len(text) < 32:
            continue
        docs.append((int(n.id), text))
        meta[int(n.id)] = {
            "title": n.title,
            "category": n.category,
            "tags": [t.lower() for t in n.tag_list()],
        }
    if len(docs) < 2:
        logger.info("semantic_retrieval: corpus too small (%d docs)", len(docs))
        with _INDEX_LOCK:
            _INDEX["vectorizer"] = None
        return False

    # P15/D-M17 — let operators swap the stop-words language (or disable
    # entirely with an empty string) without code edits. Default "english"
    # preserves prior behaviour.
    _sw = env_str("SEMANTIC_STOP_WORDS_LANG", "english").strip()
    _stop_words = _sw if _sw else None
    vec = TfidfVectorizer(
        max_features=4096,
        ngram_range=(1, _ngram_max()),
        stop_words=_stop_words,
        min_df=1,
    )
    try:
        matrix = vec.fit_transform(text for _, text in docs)
    except ValueError as exc:
        logger.warning("semantic_retrieval: TF-IDF fit failed: %s", exc)
        return False

    # P13/D-M1 — atomic swap of all index fields. Concurrent readers see
    # either the old index or the new one, never a half-flushed mix.
    with _INDEX_LOCK:
        _INDEX.update(
            {
                "built_at": time.time(),
                "node_ids": [nid for nid, _ in docs],
                "node_meta": meta,
                "vectorizer": vec,
                "matrix": matrix,
                "node_count_at_build": len(docs),
            }
        )
    logger.info("semantic_retrieval: index built over %d nodes", len(docs))
    return True


def _ensure_index(session: Session) -> bool:
    count = session.query(KnowledgeNode).count()
    # P13/D-M1 (amended) — fast-path stale check under the lock. If the index
    # is fresh, return immediately. Otherwise release the lock before the heavy
    # SQL + TF-IDF work so concurrent readers are not serialised behind the
    # rebuild. _build_index already performs its own atomic swap under
    # _INDEX_LOCK (line 168) so a second concurrent rebuild is benign: the
    # last writer wins and readers always see a consistent snapshot.
    with _INDEX_LOCK:
        if not _index_is_stale(count) and _INDEX["vectorizer"] is not None:
            return True
    # Lock released — build outside the critical section.
    return _build_index(session)


def _tfidf_search(
    query: str,
    *,
    top_k: int = 8,
    include_categories: Optional[Sequence[str]] = None,
    session: Optional[Session] = None,
) -> List[SearchHit]:
    """Internal TF-IDF flat-scan search.  Do not call directly from outside
    this package — use ``search()`` which routes to Graph RAG when enabled.
    Called by ``graph_retrieval`` as the anchor-finding step."""
    q = (query or "").strip()
    if not q:
        return []
    own = session is None
    cats_set = {c for c in (include_categories or []) if c}
    query_tokens = {t.lower() for t in re.findall(r"\w+", q)}

    def _do(s: Session) -> List[SearchHit]:
        if not _ensure_index(s):
            return []
        try:
            from sklearn.metrics.pairwise import cosine_similarity
        except ImportError:
            return []
        # P13/D-M1 — snapshot all index handles under the lock so a concurrent
        # rebuild cannot swap them mid-compute (avoiding a torn read).
        with _INDEX_LOCK:
            vectorizer = _INDEX["vectorizer"]
            matrix = _INDEX["matrix"]
            node_ids = list(_INDEX["node_ids"])
            meta = dict(_INDEX["node_meta"])
        if vectorizer is None or matrix is None:
            return []
        try:
            qv = vectorizer.transform([q])
            sims = cosine_similarity(qv, matrix).ravel()
        except Exception:  # noqa: BLE001
            logger.exception("semantic_retrieval: similarity compute failed")
            return []
        cb = _category_boost()
        tbe = _tag_boost_each()
        scored: List[SearchHit] = []
        for i, base in enumerate(sims):
            nid = int(node_ids[i])
            m = meta.get(nid, {})
            score = float(base)
            why: List[str] = []
            if cats_set and m.get("category") in cats_set:
                score += cb
                why.append(f"category={m.get('category')}")
            if query_tokens:
                tag_hits = query_tokens & set(m.get("tags", []))
                if tag_hits:
                    score += tbe * len(tag_hits)
                    why.append(f"tags={','.join(sorted(tag_hits))}")
            scored.append(
                SearchHit(
                    node_id=nid,
                    score=round(score, 6),
                    title=m.get("title") or "",
                    category=m.get("category"),
                    why_matched=why,
                )
            )
        scored.sort(key=lambda h: h.score, reverse=True)
        return [h for h in scored[: int(top_k)] if h.score > 0.0]

    if own:
        with session_scope() as s:
            return _do(s)
    return _do(session)


def search(
    query: str,
    *,
    top_k: int = 8,
    include_categories: Optional[Sequence[str]] = None,
    session: Optional[Session] = None,
) -> List[SearchHit]:
    """Return ranked SearchHits for the query.

    When ``GRAPH_RAG_ENABLED`` is True (the default), delegates to
    ``graph_retrieval.search`` which runs TF-IDF anchor finding followed by
    weighted BFS over ``granger_edges`` + ``node.links``.

    Falls back to the internal TF-IDF flat scan (``_tfidf_search``) when:
    • ``GRAPH_RAG_ENABLED=false`` is set explicitly, or
    • ``graph_retrieval`` raises any exception (automatic degradation).

    Empty list on any failure — never raises.
    """
    if env_bool("GRAPH_RAG_ENABLED", True):
        try:
            # Lazy import avoids circular dependency at module-load time.
            from backend.core.graph_retrieval import search as _graph_search  # noqa: PLC0415
            return _graph_search(
                query,
                top_k=top_k,
                include_categories=include_categories,
                session=session,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "graph_retrieval.search raised unexpectedly; falling back to TF-IDF"
            )
    return _tfidf_search(
        query,
        top_k=top_k,
        include_categories=include_categories,
        session=session,
    )


__all__ = [
    "SearchHit",
    "is_enabled",
    "search",
]

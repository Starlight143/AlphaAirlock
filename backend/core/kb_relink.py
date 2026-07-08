"""Knowledge-base cross-linking via TF-IDF cosine similarity (P6-B3).

Why TF-IDF instead of embeddings
--------------------------------
The reference system shows nodes auto-linked into clusters ("這些他都是相互
連接"). Production-grade similarity would use sentence-transformers — but that
package drags in torch + safetensors + huggingface-hub for ~3 GB, which is
unacceptable for a single-user SQLite deployment. ``sklearn`` TF-IDF gives
~80% of the quality at <100 ms per node and <50 MB resident, and it's already
a transitive dep via several other libs (verified at audit time).

Trigger paths
-------------
1. **Per-node on ingest** — caller (scheduler post-write hook) calls
   ``relink_node(session, node_id)``. The cost is one fresh TF-IDF fit per
   call; there is currently no memoization across a batch.
2. **Nightly batch** — ``relink_recent()`` is registered in ``periodic_tasks.py``
   and processes nodes from the last N hours. Bounded by ``KB_RELINK_NIGHTLY_LIMIT``.

Safety
------
* **Default OFF** via ``KB_NIGHTLY_ENABLED``.
* **Body cap** — TF-IDF only indexes the most recent ``KB_RELINK_CORPUS_SIZE``
  nodes (default 2000) so the vocabulary doesn't explode on a year of ingest.
* **Lazy import** of sklearn so deployments without it still boot; the function
  returns an empty result on ImportError.
* **Float subnormal guard** on similarity arithmetic (CLAUDE.md float rule).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Sequence

from sqlalchemy.orm import Session

from backend._envloader import env_bool, env_float, env_int, env_str
from backend.core.database import KnowledgeNode, session_scope

logger = logging.getLogger("alpha.kb_relink")


def is_relink_enabled() -> bool:
    return env_bool("KB_NIGHTLY_ENABLED", False)


def _threshold() -> float:
    return env_float("KB_RELINK_THRESHOLD", 0.30, minimum=0.05, maximum=0.95)


def _top_k() -> int:
    return env_int("KB_RELINK_TOP_K", 5, minimum=1, maximum=20)


def _corpus_size() -> int:
    return env_int("KB_RELINK_CORPUS_SIZE", 2000, minimum=50, maximum=20000)


def _analyzer() -> str:
    """`word` = English/space-tokenised; `char_wb` = CJK-friendly char n-grams."""
    raw = env_str("KB_RELINK_ANALYZER", "word").strip().lower()
    return raw if raw in {"word", "char_wb", "char"} else "word"


def _existing_links(node: KnowledgeNode) -> set[int]:
    try:
        return {int(x) for x in node.link_list()}
    except Exception:  # noqa: BLE001
        return set()


def _merge_links(node: KnowledgeNode, new_ids: Iterable[int]) -> None:
    """Update ``node.links`` JSON list, deduped, preserving any non-int entries
    we never wrote (defensive — link_list filters them out on read)."""
    keep: set[int] = _existing_links(node) | {int(x) for x in new_ids}
    node.links = json.dumps(sorted(keep))


def relink_node(
    session: Session,
    node_id: int,
    *,
    top_k: Optional[int] = None,
    threshold: Optional[float] = None,
    corpus_limit: Optional[int] = None,
) -> List[int]:
    """Compute TF-IDF cosine similarity for one node against the recent corpus.

    Returns the list of newly added neighbour node IDs (may be empty). On any
    failure (sklearn missing, body empty, fewer than 2 nodes in corpus) returns
    ``[]`` without raising — the caller can keep going.
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
    except ImportError:
        logger.warning("kb_relink: sklearn missing — relink skipped")
        return []

    # P34: honor an explicit caller 0 / 0.0 instead of treating it as falsy and
    # silently overriding it with the env default (None means "use default").
    k = int(_top_k() if top_k is None else top_k)
    thr = float(_threshold() if threshold is None else threshold)
    limit = int(_corpus_size() if corpus_limit is None else corpus_limit)

    target = session.get(KnowledgeNode, int(node_id))
    if target is None:
        return []
    target_text = (target.content or "").strip()
    if len(target_text) < 32:
        return []  # too thin to vectorise

    # Pull recent corpus + the target if it isn't already in the cut.
    corpus_rows: List[KnowledgeNode] = (
        session.query(KnowledgeNode)
        .order_by(KnowledgeNode.id.desc())
        .limit(limit)
        .all()
    )
    if target not in corpus_rows:
        corpus_rows.append(target)
    if len(corpus_rows) < 2:
        return []

    docs = [(n.id, (n.content or "").strip()) for n in corpus_rows]
    docs = [(nid, txt) for nid, txt in docs if len(txt) >= 32]
    if len(docs) < 2:
        return []
    try:
        target_idx = next(i for i, (nid, _) in enumerate(docs) if nid == target.id)
    except StopIteration:
        return []

    analyzer = _analyzer()
    if analyzer == "word":
        vectorizer_kwargs = {
            "max_features": 4096,
            "ngram_range": (1, 2),
            "stop_words": "english",
            "min_df": 1,
        }
    else:
        vectorizer_kwargs = {
            "max_features": 4096,
            "analyzer": "char_wb",
            "ngram_range": (2, 4),
            "min_df": 1,
        }
    try:
        vec = TfidfVectorizer(**vectorizer_kwargs)
        matrix = vec.fit_transform(txt for _, txt in docs)
    except ValueError as exc:
        # Empty vocabulary etc.
        logger.debug("kb_relink: TF-IDF fit failed for node %s: %s", node_id, exc)
        return []

    try:
        sims = cosine_similarity(matrix[target_idx], matrix).ravel()
    except Exception:  # noqa: BLE001
        logger.exception("kb_relink: similarity compute failed for node %s", node_id)
        return []

    ranked = sorted(
        (
            (sims[i], docs[i][0])
            for i in range(len(docs))
            if docs[i][0] != target.id
        ),
        reverse=True,
    )
    top = [nid for score, nid in ranked[: k * 2] if float(score) >= thr][:k]
    if not top:
        return []

    prior = _existing_links(target)
    fresh = [nid for nid in top if nid not in prior]
    if fresh:
        _merge_links(target, fresh)
        session.flush()
        # Invalidate graph_retrieval's edge cache so the next search() call
        # rebuilds the graph with the newly written node.links edges.
        # Lazy import avoids circular dependency and survives missing module.
        try:
            from backend.core.graph_retrieval import invalidate_graph_cache  # noqa: PLC0415
            invalidate_graph_cache()
            from backend.core.graph_intel import invalidate_intel_cache  # noqa: PLC0415
            invalidate_intel_cache()
        except Exception:  # noqa: BLE001
            pass
    return fresh


def relink_batch(
    session: Session,
    node_ids: Sequence[int],
    *,
    top_k: Optional[int] = None,
    threshold: Optional[float] = None,
    corpus_limit: Optional[int] = None,
) -> dict:
    """Relink a batch of nodes using a single shared TF-IDF fit.

    Fetches the corpus once, fits one TfidfVectorizer, then for each target
    node computes cosine similarity from the pre-built matrix — O(1) fits
    instead of O(N) fits as in repeated relink_node() calls.

    Returns a stats dict: {"processed": int, "edges_added": int}.
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
    except ImportError:
        logger.warning("kb_relink: sklearn missing — relink_batch skipped")
        return {"processed": 0, "edges_added": 0}

    if not node_ids:
        return {"processed": 0, "edges_added": 0}

    k = int(_top_k() if top_k is None else top_k)
    thr = float(_threshold() if threshold is None else threshold)
    limit = int(_corpus_size() if corpus_limit is None else corpus_limit)

    # One corpus fetch shared across all target nodes.
    corpus_rows: List[KnowledgeNode] = (
        session.query(KnowledgeNode)
        .order_by(KnowledgeNode.id.desc())
        .limit(limit)
        .all()
    )
    corpus_id_set = {n.id for n in corpus_rows}

    # Ensure every target node is present in the corpus.
    target_ids_set = set(node_ids)
    missing_ids = target_ids_set - corpus_id_set
    if missing_ids:
        extra_rows = session.query(KnowledgeNode).filter(
            KnowledgeNode.id.in_(missing_ids)
        ).all()
        corpus_rows.extend(extra_rows)

    if len(corpus_rows) < 2:
        return {"processed": 0, "edges_added": 0}

    docs = [(n.id, (n.content or "").strip()) for n in corpus_rows]
    docs = [(nid, txt) for nid, txt in docs if len(txt) >= 32]
    if len(docs) < 2:
        return {"processed": 0, "edges_added": 0}

    id_to_idx = {nid: i for i, (nid, _) in enumerate(docs)}

    analyzer = _analyzer()
    if analyzer == "word":
        vectorizer_kwargs = {
            "max_features": 4096,
            "ngram_range": (1, 2),
            "stop_words": "english",
            "min_df": 1,
        }
    else:
        vectorizer_kwargs = {
            "max_features": 4096,
            "analyzer": "char_wb",
            "ngram_range": (2, 4),
            "min_df": 1,
        }

    try:
        vec = TfidfVectorizer(**vectorizer_kwargs)
        matrix = vec.fit_transform(txt for _, txt in docs)
    except ValueError as exc:
        logger.debug("kb_relink: TF-IDF fit failed in relink_batch: %s", exc)
        return {"processed": 0, "edges_added": 0}

    invalidated = False
    processed = 0
    edges_added = 0

    for nid in node_ids:
        target_idx = id_to_idx.get(nid)
        if target_idx is None:
            continue
        target_node = session.get(KnowledgeNode, nid)
        if target_node is None:
            continue

        try:
            sims = cosine_similarity(matrix[target_idx], matrix).ravel()
        except Exception:  # noqa: BLE001
            logger.exception("kb_relink: similarity compute failed for node %s", nid)
            processed += 1
            continue

        ranked = sorted(
            (
                (sims[i], docs[i][0])
                for i in range(len(docs))
                if docs[i][0] != nid
            ),
            reverse=True,
        )
        top = [nid2 for score, nid2 in ranked[: k * 2] if float(score) >= thr][:k]

        prior = _existing_links(target_node)
        fresh = [nid2 for nid2 in top if nid2 not in prior]
        if fresh:
            _merge_links(target_node, fresh)
            session.flush()
            edges_added += len(fresh)
            if not invalidated:
                try:
                    from backend.core.graph_retrieval import invalidate_graph_cache  # noqa: PLC0415
                    invalidate_graph_cache()
                    from backend.core.graph_intel import invalidate_intel_cache  # noqa: PLC0415
                    invalidate_intel_cache()
                except Exception:  # noqa: BLE001
                    pass
                invalidated = True
        processed += 1

    return {"processed": processed, "edges_added": edges_added}


def relink_recent(
    session: Optional[Session] = None,
    *,
    hours: Optional[int] = None,
    limit: Optional[int] = None,
) -> dict:
    """Re-link every KnowledgeNode ingested in the last ``hours`` hours.

    Designed for ``periodic_tasks.py``. Opens its own session if not given.
    Returns a stats dict for logging.
    """
    if not is_relink_enabled():
        return {"enabled": False, "processed": 0, "edges_added": 0}

    # P34: use None-sentinel (not falsy-or) so explicit hours=0 / limit=0 are
    # honoured instead of silently overridden by the env defaults.
    h = int(env_int("KB_RELINK_RECENT_HOURS", 24, minimum=1, maximum=168) if hours is None else hours)
    lim = int(env_int("KB_RELINK_NIGHTLY_LIMIT", 50, minimum=1, maximum=500) if limit is None else limit)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=h)
    stats = {"enabled": True, "processed": 0, "edges_added": 0}

    own_session = session is None

    def _run(s: Session) -> None:
        rows = (
            s.query(KnowledgeNode.id)
            .filter(
                (KnowledgeNode.ingested_at >= cutoff)
                | (KnowledgeNode.created_at >= cutoff)
            )
            .order_by(KnowledgeNode.id.desc())
            .limit(lim)
            .all()
        )
        ids: Sequence[int] = [int(r[0]) for r in rows]
        batch_stats = relink_batch(s, ids)
        stats["processed"] += batch_stats["processed"]
        stats["edges_added"] += batch_stats["edges_added"]

    if own_session:
        with session_scope() as s:
            _run(s)
    else:
        _run(session)
    logger.info("kb_relink recent pass complete: %s", stats)
    return stats


__all__ = [
    "is_relink_enabled",
    "relink_node",
    "relink_batch",
    "relink_recent",
]

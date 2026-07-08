"""T1-B — extractive (non-LLM) summary compilation for KnowledgeNode.

A short, deterministic, agent-facing digest derived from a node's ``content``.
Used to populate ``KnowledgeNode.summary`` at ingestion and on-read, so the
Researcher can load context summary-first instead of dumping full node bodies
(cuts tokens + the lost-in-the-middle effect). Pure, network-free, no LLM, no
budget impact — so it can run on every insert and every retrieval for free.

``summary`` is DERIVED from ``content`` and is computed AFTER the dedup
``content_hash`` — it never feeds the hash, so it cannot affect de-duplication.
"""
from __future__ import annotations

import re

from backend._envloader import env_int

_WS_RE = re.compile(r"\s+")
# Sentence boundary for Latin + CJK terminators.
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")


def summary_max_chars() -> int:
    return env_int("KB_SUMMARY_MAX_CHARS", 360, minimum=80, maximum=2000)


def compile_summary(title: str, content: str, kind: str = "") -> str:
    """Deterministic extractive summary: the leading whole sentences of the
    content, whitespace-normalised, hard-capped at ``KB_SUMMARY_MAX_CHARS``.
    Falls back to the (capped) title when content is empty. Never raises."""
    cap = summary_max_chars()
    body = _WS_RE.sub(" ", str(content or "").strip())
    if not body:
        return str(title or "").strip()[:cap]
    if len(body) <= cap:
        return body
    out = ""
    for sent in _SENT_SPLIT_RE.split(body):
        sent = sent.strip()
        if not sent:
            continue
        candidate = (out + " " + sent).strip() if out else sent
        if len(candidate) > cap:
            break
        out = candidate
    if not out:
        # First sentence alone already exceeds the cap — hard-truncate.
        out = body[:cap]
    return out.strip()


__all__ = ["compile_summary", "summary_max_chars"]

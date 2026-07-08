"""T1-B — extractive summary compilation + summary-first researcher retrieval.

compile_summary is deterministic/bounded/content-derived; _format_nodes emits a
compact summary for every selected node and expands FULL content only for the
top-K (IC-ordered) within a char budget, lazily back-filling missing summaries.
"""
from __future__ import annotations

import backend.agents.researcher as R
from backend.core.database import KnowledgeNode
from backend.core.kb_summary import compile_summary


def test_compile_summary_basic(monkeypatch):
    monkeypatch.delenv("KB_SUMMARY_MAX_CHARS", raising=False)
    s = compile_summary("Title", "First sentence. Second sentence. Third one.")
    assert s.startswith("First sentence.")
    # Whitespace normalised.
    assert compile_summary("T", "a\n\n  b\t c") == "a b c"
    # Empty content falls back to title.
    assert compile_summary("My Title", "") == "My Title"
    assert compile_summary("", "") == ""


def test_compile_summary_capped(monkeypatch):
    monkeypatch.setenv("KB_SUMMARY_MAX_CHARS", "80")
    long = "word " * 500
    s = compile_summary("T", long)
    assert len(s) <= 80


def test_compile_summary_long_first_sentence_truncates(monkeypatch):
    monkeypatch.setenv("KB_SUMMARY_MAX_CHARS", "100")  # 80 is the floor (env_int min)
    s = compile_summary("T", "x" * 300 + ". short.")
    assert len(s) <= 100 and s  # hard-truncated, non-empty


def _node(nid, title, content, ic, summary=None):
    n = KnowledgeNode(id=nid, title=title, content=content, ic_score=ic, kind="concept", tags="")
    n.summary = summary
    return n


def test_format_nodes_summary_first_and_topk(monkeypatch):
    for k in ("RESEARCHER_EXPAND_TOP_K", "RESEARCHER_CONTEXT_CHAR_BUDGET",
              "KB_SUMMARY_MAX_CHARS", "KB_SUMMARY_LAZY_BACKFILL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RESEARCHER_EXPAND_TOP_K", "1")
    nodes = [
        _node(1, "Alpha", "AAA full body content one. more.", 0.9, summary="sumA"),
        _node(2, "Beta", "BBB full body content two. more.", 0.5, summary="sumB"),
        _node(3, "Gamma", "CCC full body content three. more.", 0.1, summary="sumC"),
    ]
    out = R._format_nodes(nodes)
    # Every node's summary appears.
    assert "sumA" in out and "sumB" in out and "sumC" in out
    # Full content only for the top-1 (node #1); not for #2/#3.
    assert "full content of Node #1" in out
    assert "full content of Node #2" not in out
    assert "BBB full body" not in out


def test_format_nodes_lazy_backfill(monkeypatch):
    for k in ("RESEARCHER_EXPAND_TOP_K", "KB_SUMMARY_LAZY_BACKFILL"):
        monkeypatch.delenv(k, raising=False)
    n = _node(7, "NoSummary", "Body sentence one. Body sentence two.", 0.4, summary=None)
    out = R._format_nodes([n])
    # Extractive summary computed on the fly and shown...
    assert "Body sentence one." in out
    # ...and lazily back-filled onto the ORM object (autoflush=False → safe).
    assert n.summary and "Body sentence one." in n.summary
    assert n.summary_generated_at is not None


def test_format_nodes_backfill_can_be_disabled(monkeypatch):
    monkeypatch.setenv("KB_SUMMARY_LAZY_BACKFILL", "0")
    n = _node(8, "NoSummary", "Body one. Body two.", 0.4, summary=None)
    R._format_nodes([n])
    assert n.summary is None  # not persisted when backfill disabled


def test_format_nodes_expand_all_when_k_huge(monkeypatch):
    monkeypatch.setenv("RESEARCHER_EXPAND_TOP_K", "99")
    monkeypatch.delenv("RESEARCHER_CONTEXT_CHAR_BUDGET", raising=False)
    nodes = [
        _node(1, "A", "AAA body alpha.", 0.9, summary="sa"),
        _node(2, "B", "BBB body beta.", 0.5, summary="sb"),
    ]
    out = R._format_nodes(nodes)
    # Near-legacy: all nodes' full content expanded (rollback escape hatch).
    assert "full content of Node #1" in out and "full content of Node #2" in out

"""T2-A / T2-C — knowledge-graph intelligence (bridge/gap analytics + seeding).

Bridge nodes (high betweenness) are the cross-domain connectors used to seed
novel hypotheses; gap categories flag under-explored factor families; vital
signs summarise KB health. Seeding is env-gated with a strict identity fallback.
"""
from __future__ import annotations

import networkx as nx
import pytest

import backend.core.graph_intel as GI


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # The analytics cache is memoised on (n_nodes, n_edges); two tests with the
    # same shape would otherwise collide — invalidate around each test.
    for k in ("GRAPH_RAG_SEED_SELECTION", "GRAPH_RAG_SEED_MAX_EXTRA",
              "GRAPH_INTEL_BRIDGE_TOP_N", "GRAPH_INTEL_GAP_MIN_CONCEPTS",
              "GRAPH_INTEL_GAP_PRESSURE_MIN", "GRAPH_INTEL_BRIDGE_BETWEENNESS_MIN"):
        monkeypatch.delenv(k, raising=False)
    GI.invalidate_intel_cache()
    yield
    GI.invalidate_intel_cache()


def _stub_graph(monkeypatch, G):
    monkeypatch.setattr(GI, "_get_cached_graph", lambda session=None: G)


def _two_triangles_with_bridge():
    """Two triangles {1,2,3} and {4,5,6} joined only through node 0."""
    G = nx.DiGraph()
    for nid in range(7):
        G.add_node(nid, kind="concept", category="x", ic_score=0.5)
    for a, b in [(1, 2), (2, 3), (3, 1), (4, 5), (5, 6), (6, 4)]:
        G.add_edge(a, b)
    G.add_edge(3, 0)
    G.add_edge(0, 4)
    return G


def test_bridge_detection_picks_articulation_node(monkeypatch):
    _stub_graph(monkeypatch, _two_triangles_with_bridge())
    intel = GI._compute_analytics(GI._get_cached_graph())
    assert intel.bridges
    assert intel.bridges[0]["node_id"] == 0   # the articulation node ranks #1
    assert intel.bridge_count >= 1


def test_gap_pressure_ranks_unrealized_category(monkeypatch):
    G = nx.DiGraph()
    # Category A: 5 concepts, 0 realised. Category B: 2 concepts, 2 past_alpha.
    nid = 0
    for _ in range(5):
        G.add_node(nid, kind="concept", category="A", ic_score=0.5); nid += 1
    for _ in range(2):
        G.add_node(nid, kind="concept", category="B", ic_score=0.5); nid += 1
    for _ in range(2):
        G.add_node(nid, kind="past_alpha", category="B", ic_score=0.5); nid += 1
    _stub_graph(monkeypatch, G)
    intel = GI._compute_analytics(GI._get_cached_graph())
    assert intel.gaps and intel.gaps[0]["category"] == "A"
    assert intel.gap_pressure >= 4.0


def test_diversity_index_bounds(monkeypatch):
    # Uniform spread across 4 categories → entropy near max → index near 1.
    G = nx.DiGraph()
    nid = 0
    for cat in ("A", "B", "C", "D"):
        for _ in range(5):
            G.add_node(nid, kind="concept", category=cat, ic_score=0.5); nid += 1
    _stub_graph(monkeypatch, G)
    assert GI._compute_analytics(GI._get_cached_graph()).diversity_index == pytest.approx(1.0, abs=1e-9)

    GI.invalidate_intel_cache()
    G2 = nx.DiGraph()
    for i in range(6):
        G2.add_node(i, kind="concept", category="solo", ic_score=0.5)
    _stub_graph(monkeypatch, G2)
    assert GI._compute_analytics(GI._get_cached_graph()).diversity_index == 0.0


def test_orphan_rate(monkeypatch):
    G = nx.DiGraph()
    for i in range(4):
        G.add_node(i, kind="concept", category="x", ic_score=0.5)
    G.add_edge(0, 1)   # 2 and 3 are orphans
    _stub_graph(monkeypatch, G)
    assert GI._compute_analytics(GI._get_cached_graph()).orphan_rate == pytest.approx(0.5)


def test_seed_disabled_is_identity(monkeypatch):
    _stub_graph(monkeypatch, _two_triangles_with_bridge())
    assert GI.seed_node_ids_for_auto([99, 100]) == [99, 100]


def test_seed_enabled_appends_bridges(monkeypatch):
    monkeypatch.setenv("GRAPH_RAG_SEED_SELECTION", "1")
    _stub_graph(monkeypatch, _two_triangles_with_bridge())
    out = GI.seed_node_ids_for_auto([99])
    assert out[0] == 99                       # base id preserved at the front
    assert len(out) > 1                       # bridge nodes appended
    assert 0 in out                           # the top bridge node
    assert out.count(99) == 1                 # no dupes


def test_seed_never_drops_base_on_empty_graph(monkeypatch):
    monkeypatch.setenv("GRAPH_RAG_SEED_SELECTION", "1")
    _stub_graph(monkeypatch, None)            # cold/empty graph
    assert GI.seed_node_ids_for_auto([1, 2, 3]) == [1, 2, 3]


def test_vital_signs_cold_start_returns_zero_dict(monkeypatch):
    _stub_graph(monkeypatch, None)
    v = GI.vital_signs()
    assert v["node_count"] == 0 and v["bridge_count"] == 0
    assert v["diversity_index"] == 0.0
    assert v["bridges"] == [] and v["gaps"] == []

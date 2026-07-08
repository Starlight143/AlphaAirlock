"""T2-B — confidence-decay → revisit loop (outcome-driven).

When KB_CONFIDENCE_DECAY_ENABLED=1, a poor live/OOS outcome decays a source
node's confidence and (for past_alpha/postmortem) flags it status='revisit'; a
good outcome rewards confidence and clears a prior revisit latch. Default OFF =
no confidence change.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.core.ic_history as ICH
from backend.core.database import Base, KIND_CONCEPT, KIND_PAST_ALPHA, KnowledgeNode


@pytest.fixture()
def maker(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'conf.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("KB_CONFIDENCE_DECAY_ENABLED", "KB_CONF_DECAY_FACTOR", "KB_CONF_REWARD",
              "KB_CONF_FLOOR", "KB_CONF_REVISIT_THRESHOLD", "KB_CONF_BAD_IC", "KB_CONF_GOOD_IC"):
        monkeypatch.delenv(k, raising=False)
    yield


def _mk(s, kind, conf=0.3, ic=0.0, status="active", flagged=False):
    n = KnowledgeNode(title="t", content="body sentence.", kind=kind,
                      ic_score=ic, confidence=conf, status=status)
    if flagged:
        n.revisit_flagged_at = datetime.now(timezone.utc)
    s.add(n)
    s.flush()
    return n.id


def test_decay_disabled_no_confidence_change(maker):
    s = maker()
    nid = _mk(s, KIND_PAST_ALPHA, conf=0.3)
    s.commit()
    ICH.record_pipeline_outcomes([nid], strategy_id=1, ic_value=-0.5, session=s)
    s.commit()
    n = s.get(KnowledgeNode, nid)
    assert n.confidence == 0.3
    assert n.revisit_flagged_at is None


def test_bad_outcome_decays_and_flags(maker, monkeypatch):
    monkeypatch.setenv("KB_CONFIDENCE_DECAY_ENABLED", "1")
    s = maker()
    nid = _mk(s, KIND_PAST_ALPHA, conf=0.3)
    s.commit()
    ICH.record_pipeline_outcomes([nid], strategy_id=1, ic_value=-0.5, session=s)
    s.commit()
    n = s.get(KnowledgeNode, nid)
    assert n.confidence == pytest.approx(0.21)   # 0.3 * 0.7
    assert n.revisit_flagged_at is not None
    assert n.status == "revisit"                 # below 0.25 threshold


def test_good_outcome_clears_revisit(maker, monkeypatch):
    monkeypatch.setenv("KB_CONFIDENCE_DECAY_ENABLED", "1")
    s = maker()
    nid = _mk(s, KIND_PAST_ALPHA, conf=0.2, status="revisit", flagged=True)
    s.commit()
    ICH.record_pipeline_outcomes([nid], strategy_id=2, ic_value=0.1, session=s)
    s.commit()
    n = s.get(KnowledgeNode, nid)
    assert n.confidence > 0.2          # rewarded
    assert n.revisit_flagged_at is None
    assert n.status == "active"


def test_concept_node_decays_but_not_flagged(maker, monkeypatch):
    monkeypatch.setenv("KB_CONFIDENCE_DECAY_ENABLED", "1")
    s = maker()
    nid = _mk(s, KIND_CONCEPT, conf=0.1)
    s.commit()
    ICH.record_pipeline_outcomes([nid], strategy_id=3, ic_value=-0.9, session=s)
    s.commit()
    n = s.get(KnowledgeNode, nid)
    assert n.confidence < 0.1           # confidence still decays
    assert n.revisit_flagged_at is None  # concepts never flagged for revisit

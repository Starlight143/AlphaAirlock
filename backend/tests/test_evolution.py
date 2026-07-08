"""T3-A — evolutionary hypothesis-tree search.

OFF by default (no-op). When enabled, parent selection filters the surviving
pool by stored-story / source-nodes / depth-cap / OOS-Sharpe floor and ranks by
selection score; the operator mutates a parent's story (rejecting byte-identical
clones); the whole thing is gated by a per-dispatch coin-flip.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.core.evolution as EV
from backend.core.database import AlphaStrategy, Base


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'ev.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("EVOLUTION_ENABLED", "EVOLUTION_FRACTION", "EVOLUTION_PARENT_POOL_SIZE",
              "EVOLUTION_MIN_PARENT_OOS_SHARPE", "EVOLUTION_MAX_DEPTH", "EVOLUTION_OPERATOR"):
        monkeypatch.delenv(k, raising=False)
    yield


def _add(s, status, story="A surviving thesis.", src=(1, 2), oos=1.0, depth=0, sharpe=None):
    cfg = {"alpha_story": story, "source_node_ids": list(src),
           "asset_symbol": "BTC", "asset_class": "crypto", "evolution_depth": depth}
    m = {}
    if oos is not None:
        m["oos_annualized_sharpe"] = oos
    if sharpe is not None:
        m["annualized_sharpe"] = sharpe
    row = AlphaStrategy(name="s", stage=4, status=status,
                        config_json=json.dumps(cfg), backtest_metrics=json.dumps(m))
    s.add(row)
    s.flush()
    return row.id


def test_disabled_is_noop():
    assert EV.run_evolution_once()["status"] == "disabled"
    assert EV.should_use_evolution() is False


def test_fraction_zero_never_fires(monkeypatch):
    monkeypatch.setenv("EVOLUTION_ENABLED", "1")
    monkeypatch.setenv("EVOLUTION_FRACTION", "0")
    assert all(EV.should_use_evolution() is False for _ in range(50))


def test_parent_selection_filters(session):
    s = session
    good = _add(s, "APPROVED", oos=1.2)
    _add(s, "REJECTED", oos=2.0)             # wrong status
    _add(s, "APPROVED", oos=0.1)             # below the 0.5 OOS floor
    _add(s, "APPROVED", story="", oos=2.0)   # no story
    _add(s, "APPROVED", src=(), oos=2.0)     # no source nodes
    _add(s, "APPROVED", oos=2.0, depth=4)    # at the depth cap (default 4)
    s.commit()
    parents = EV.select_parents(session=s, k=10)
    ids = [p.strategy_id for p in parents]
    assert ids == [good]                     # only the one valid parent


def test_parent_falls_back_to_annualized_sharpe(session):
    s = session
    pid = _add(s, "LIVE", oos=None, sharpe=0.9)
    s.commit()
    parents = EV.select_parents(session=s, k=5)
    assert parents and parents[0].strategy_id == pid
    assert parents[0].oos_sharpe == 0.9


def test_selection_ranks_by_oos(session):
    s = session
    _add(s, "APPROVED", oos=0.7)
    top = _add(s, "APPROVED", oos=3.0)
    s.commit()
    parents = EV.select_parents(session=s, k=5)
    assert parents[0].strategy_id == top     # highest OOS Sharpe leads


def test_build_seed_rejects_clone(monkeypatch):
    monkeypatch.setattr(EV, "call_messages", lambda **kw: "Same thesis.")
    p = EV.Parent(1, "s", "Same thesis.", [1], "BTC", "crypto", 0, 1.0)
    assert EV.build_evolved_seed([p], operator="mutate") is None


def test_build_seed_valid(monkeypatch):
    monkeypatch.setattr(
        EV, "call_messages",
        lambda **kw: "# New headline\n\nDifferent thesis.\n```yaml\nx: 1\n```",
    )
    p = EV.Parent(1, "s", "Old thesis.", [1, 2], "BTC", "crypto", 1, 1.0)
    seed = EV.build_evolved_seed([p], operator="mutate")
    assert seed is not None
    assert "Different thesis" in seed.alpha_story
    assert seed.source_node_ids == [1, 2]
    assert seed.parent_ids == [1] and seed.operator == "mutate"


def test_no_parents_status(monkeypatch):
    monkeypatch.setenv("EVOLUTION_ENABLED", "1")
    monkeypatch.setattr(EV, "select_parents", lambda **kw: [])
    assert EV.run_evolution_once()["status"] == "no_parents"


def test_tick_skips_when_coinflip_false(monkeypatch):
    monkeypatch.setattr(EV, "should_use_evolution", lambda: False)
    assert EV.tick_evolution()["status"] == "skipped"

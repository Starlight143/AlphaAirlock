"""Regression guard: the alpha-queue backlog generator must honor the multi-asset
universe, not pin every strategy to BTC.

The multi-symbol feature (P-MULTISYM) was originally wired only into the
ingest-driven ``auto_pipeline`` path. ``alpha_queue.promote_node`` — the dominant
generator that grinds the KnowledgeNode backlog even without fresh ingest — kept
constructing a bare ``WorkflowOrchestrator()`` (default ``asset_symbol="BTC"``),
so the rotation never reached production and 1000+ strategies came out BTC-only.

These lock in that ``promote_node`` constructs the orchestrator with the
universe-picked symbol, and falls back to BTC only when the pick's price data
can't be fetched.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.core.alpha_queue as aq
import backend.core.graph_intel as gi
import backend.core.orchestrator as orch_mod
import backend.core.universe as universe
from backend.core.database import Base, KnowledgeNode


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """A throwaway SQLite DB wired into the exact ``session_scope`` symbol that
    ``promote_node`` imported, so its reservation + write-back hit this DB."""
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'queue.db').as_posix()}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def _scope():
        s = Maker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(aq, "session_scope", _scope)
    return Maker


def _seed_node(Maker) -> int:
    with Maker() as s:
        n = KnowledgeNode(title="ETH momentum idea", content="some hypothesis", ic_score=0.9)
        s.add(n)
        s.commit()
        return int(n.id)


class _CapturingOrch:
    """Stub orchestrator that records the asset_symbol it was built with and
    skips all real LLM / backtest work."""

    last_symbol: str | None = None

    def __init__(self, *args, asset_symbol: str = "BTC", **kwargs):
        _CapturingOrch.last_symbol = asset_symbol

    def bootstrap_strategy(self, raw_text):
        return 4242

    def run_full_pipeline_for_id(self, *, strategy_id, raw_text, prior_node_ids):
        return {"status": "OK"}


def _patch_common(monkeypatch, *, data_ok: bool):
    _CapturingOrch.last_symbol = None
    monkeypatch.setattr(orch_mod, "WorkflowOrchestrator", _CapturingOrch)
    monkeypatch.setattr(gi, "seed_node_ids_for_auto", lambda ids: list(ids))
    monkeypatch.setattr(
        universe, "pick_for_strategy",
        lambda: universe.Instrument(
            app_symbol="ETH", asset_class="crypto", provider="binance_public",
            csv_name="ETH-USDT.csv", display="ETH-USDT",
        ),
    )
    monkeypatch.setattr(universe, "ensure_price_data", lambda sym: data_ok)


def test_promote_node_uses_universe_pick(temp_db, monkeypatch):
    # Universe rotates to ETH and its data is available → orchestrator must be
    # built for ETH, not the default BTC.
    _patch_common(monkeypatch, data_ok=True)
    node_id = _seed_node(temp_db)
    out = aq.promote_node(node_id)
    assert out.get("strategy_id") == 4242
    assert _CapturingOrch.last_symbol == "ETH"


def test_promote_node_falls_back_to_btc_on_missing_data(temp_db, monkeypatch):
    # Pick is ETH but its price data can't be fetched → honest fallback to BTC
    # (always available) so a single bad feed never sinks the run.
    _patch_common(monkeypatch, data_ok=False)
    node_id = _seed_node(temp_db)
    out = aq.promote_node(node_id)
    assert out.get("strategy_id") == 4242
    assert _CapturingOrch.last_symbol == "BTC"

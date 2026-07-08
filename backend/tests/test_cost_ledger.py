"""FinOps cost ledger — record / aggregate / prune (T-FINOPS)."""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.core.cost_ledger as CL
from backend.core.database import Base, LLMCostLedger


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("LLM_COST_LEDGER_ENABLED", "LLM_COST_LEDGER_RETENTION_DAYS"):
        monkeypatch.delenv(k, raising=False)
    yield


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'cost.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextlib.contextmanager
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

    # cost_ledger lazy-imports session_scope per call, so patching the module
    # attribute redirects every ledger write/read at this temp DB.
    monkeypatch.setattr("backend.core.database.session_scope", _scope)
    # Ledger defaults OFF under pytest (live-DB guard) — opt in for these tests.
    monkeypatch.setenv("LLM_COST_LEDGER_ENABLED", "1")
    return Maker


def test_disabled_is_noop(temp_db, monkeypatch):
    monkeypatch.setenv("LLM_COST_LEDGER_ENABLED", "0")
    CL.record_call(agent="researcher", model="m", input_chars=1000,
                   output_chars=500, strategy_id=1)
    with temp_db() as s:
        assert s.query(LLMCostLedger).count() == 0


def test_record_and_summary(temp_db):
    CL.record_call(agent="researcher", model="minimax/minimax-m3",
                   input_chars=3500, output_chars=700, strategy_id=10)
    CL.record_call(agent="critic", model="anthropic/claude-sonnet-4.6",
                   input_chars=7000, output_chars=1400, strategy_id=10)
    CL.record_call(agent="critic", model="anthropic/claude-sonnet-4.6",
                   input_chars=3500, output_chars=700, strategy_id=11)
    out = CL.summary(days=7)
    assert out["total_calls"] == 3
    assert out["total_cost_usd"] > 0.0
    agents = {r["agent"]: r for r in out["by_agent"]}
    assert agents["critic"]["calls"] == 2
    assert agents["researcher"]["calls"] == 1
    # The critic ran on more chars → it must out-spend the researcher.
    assert agents["critic"]["cost_usd"] > agents["researcher"]["cost_usd"]
    models = {r["model"] for r in out["by_model"]}
    assert "anthropic/claude-sonnet-4.6" in models
    top = {r["strategy_id"]: r for r in out["top_strategies"]}
    assert top[10]["calls"] == 2
    assert out["today_calls"] == 3
    assert out["real_cost_calls"] == 0 and out["estimated_calls"] == 3  # no usage given


def test_summary_empty_is_zeroed(temp_db):
    out = CL.summary(days=7)
    assert out["total_calls"] == 0
    assert out["total_cost_usd"] == 0.0
    assert out["by_agent"] == [] and out["top_strategies"] == []


def test_prune_old(temp_db):
    with temp_db() as s:
        s.add(LLMCostLedger(agent="a", model="m", input_chars=100,
                            output_chars=10, est_cost_usd=0.001, strategy_id=1))
        old = LLMCostLedger(agent="a", model="m", input_chars=100,
                            output_chars=10, est_cost_usd=0.001, strategy_id=1)
        old.created_at = datetime.now(timezone.utc) - timedelta(days=200)
        s.add(old)
        s.commit()  # a raw Session context closes WITHOUT committing
    assert CL.prune_old(retention_days=90) == 1
    with temp_db() as s:
        assert s.query(LLMCostLedger).count() == 1


def test_real_usage_overrides_estimate(temp_db):
    CL.record_call(agent="critic", model="anthropic/claude-sonnet-4.6",
                   input_chars=4000, output_chars=800,
                   usage={"cost": 0.0123, "prompt_tokens": 1100, "completion_tokens": 240},
                   strategy_id=7)
    out = CL.summary(days=7)
    assert out["total_calls"] == 1
    assert out["real_cost_calls"] == 1 and out["estimated_calls"] == 0
    # The stored cost is OpenRouter's real figure, not the char estimate.
    assert abs(out["total_cost_usd"] - 0.0123) < 1e-9
    with temp_db() as s:
        row = s.query(LLMCostLedger).one()
        assert row.cost_source == "openrouter"
        assert row.input_tokens == 1100 and row.output_tokens == 240


def test_no_usage_falls_back_to_estimate(temp_db):
    CL.record_call(agent="researcher", model="minimax/minimax-m3",
                   input_chars=3500, output_chars=700, strategy_id=8)
    out = CL.summary(days=7)
    assert out["real_cost_calls"] == 0 and out["estimated_calls"] == 1
    assert out["total_cost_usd"] > 0.0
    with temp_db() as s:
        row = s.query(LLMCostLedger).one()
        assert row.cost_source == "estimate"
        assert row.input_tokens is None and row.output_tokens is None


def test_parse_openrouter_usage():
    from backend.agents._client import _parse_openrouter_usage
    assert _parse_openrouter_usage(None) is None
    assert _parse_openrouter_usage({}) is None
    assert _parse_openrouter_usage(
        {"cost": 0.004, "prompt_tokens": 900, "completion_tokens": 120}
    ) == {"cost": 0.004, "prompt_tokens": 900, "completion_tokens": 120}
    # tokens present, cost absent → still returned (cost None)
    u = _parse_openrouter_usage({"prompt_tokens": 10, "completion_tokens": 5})
    assert u["cost"] is None and u["prompt_tokens"] == 10
    # garbage cost coerces to None for that field, others survive
    u2 = _parse_openrouter_usage({"cost": "abc", "prompt_tokens": 1})
    assert u2["cost"] is None and u2["prompt_tokens"] == 1


def test_usage_tls_roundtrip():
    from backend.agents import _client as C
    C._stash_usage({"cost": 1.0})
    assert C._pop_usage() == {"cost": 1.0}
    assert C._pop_usage() is None  # read-and-clear

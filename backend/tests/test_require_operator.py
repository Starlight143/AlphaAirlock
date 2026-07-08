"""Unit tests for the require_operator auth dependency (Round-5 finding QA-07).

Tests all distinct code paths of require_operator in backend/app/main.py:
  (a) ALPHA_OPERATOR_TOKEN unset + ALPHA_DISABLE_OPERATOR_GUARD not true  -> 503
  (b) ALPHA_OPERATOR_TOKEN unset + ALPHA_DISABLE_OPERATOR_GUARD=true       -> passthrough
  (c) ALPHA_OPERATOR_TOKEN set + wrong / missing credential               -> 401
  (d) ALPHA_OPERATOR_TOKEN set + correct Bearer token                     -> principal
  (e) ALPHA_OPERATOR_TOKEN set + correct X-Operator-Token header          -> principal
  (f) correct token + explicit X-Actor header                            -> X-Actor value

No network, no DB, no background tasks. Uses FastAPI TestClient against a
minimal app that mounts only require_operator (not the full production app).
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


def _fresh_require_operator(monkeypatch, env: dict):
    """Patch the env snapshot, then return require_operator.

    require_operator reads os.environ at *call* time (not import time), so we
    simply set/clear the relevant keys and return the already-loaded function.
    """
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for key in ("ALPHA_OPERATOR_TOKEN", "ALPHA_DISABLE_OPERATOR_GUARD"):
        if key not in env:
            monkeypatch.delenv(key, raising=False)
    from backend.app.main import require_operator
    return require_operator


def _app_with_guard(require_operator_fn):
    """Minimal FastAPI app with one route protected by require_operator."""
    mini = FastAPI()

    @mini.get("/probe")
    def _probe(principal: str = Depends(require_operator_fn)):
        return {"principal": principal}

    return mini


# --- Path (a): token not configured, guard NOT disabled -> 503 fail-closed ---

class TestGuardFailClosed:
    def test_no_token_no_flag_returns_503(self, monkeypatch):
        fn = _fresh_require_operator(monkeypatch, {})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        assert client.get("/probe").status_code == 503

    def test_503_detail_mentions_operator_token(self, monkeypatch):
        fn = _fresh_require_operator(monkeypatch, {})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        assert "ALPHA_OPERATOR_TOKEN" in client.get("/probe").json().get("detail", "")

    def test_explicit_false_still_503(self, monkeypatch):
        fn = _fresh_require_operator(monkeypatch, {"ALPHA_DISABLE_OPERATOR_GUARD": "false"})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        assert client.get("/probe").status_code == 503

    def test_typo_in_flag_still_503(self, monkeypatch):
        """A typo like 'ture' must NOT accidentally open the guard (whitelist)."""
        fn = _fresh_require_operator(monkeypatch, {"ALPHA_DISABLE_OPERATOR_GUARD": "ture"})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        assert client.get("/probe").status_code == 503


# --- Path (b): token unset + ALPHA_DISABLE_OPERATOR_GUARD=true -> passthrough ---

class TestGuardDisabled:
    def test_disable_guard_true_returns_200(self, monkeypatch):
        fn = _fresh_require_operator(monkeypatch, {"ALPHA_DISABLE_OPERATOR_GUARD": "true"})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        assert client.get("/probe").status_code == 200

    def test_disable_guard_returns_default_principal(self, monkeypatch):
        fn = _fresh_require_operator(monkeypatch, {"ALPHA_DISABLE_OPERATOR_GUARD": "true"})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        assert client.get("/probe").json()["principal"] == "operator"

    def test_disable_guard_respects_x_actor_header(self, monkeypatch):
        fn = _fresh_require_operator(monkeypatch, {"ALPHA_DISABLE_OPERATOR_GUARD": "true"})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        assert client.get("/probe", headers={"X-Actor": "alice"}).json()["principal"] == "alice"

    @pytest.mark.parametrize("flag", ["1", "yes", "on", "true"])
    def test_all_truthy_tokens_accepted(self, monkeypatch, flag):
        fn = _fresh_require_operator(monkeypatch, {"ALPHA_DISABLE_OPERATOR_GUARD": flag})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        assert client.get("/probe").status_code == 200


# --- Path (c): token configured, wrong / missing credential -> 401 ---

class TestTokenConfiguredUnauth:
    TOKEN = "correct-secret-token-xyz"

    def test_no_credentials_returns_401(self, monkeypatch):
        fn = _fresh_require_operator(monkeypatch, {"ALPHA_OPERATOR_TOKEN": self.TOKEN})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        assert client.get("/probe").status_code == 401

    def test_wrong_bearer_returns_401(self, monkeypatch):
        fn = _fresh_require_operator(monkeypatch, {"ALPHA_OPERATOR_TOKEN": self.TOKEN})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        assert client.get("/probe", headers={"Authorization": "Bearer wrongtoken"}).status_code == 401

    def test_wrong_x_operator_token_returns_401(self, monkeypatch):
        fn = _fresh_require_operator(monkeypatch, {"ALPHA_OPERATOR_TOKEN": self.TOKEN})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        assert client.get("/probe", headers={"X-Operator-Token": "wrongtoken"}).status_code == 401

    def test_bearer_prefix_only_returns_401(self, monkeypatch):
        fn = _fresh_require_operator(monkeypatch, {"ALPHA_OPERATOR_TOKEN": self.TOKEN})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        assert client.get("/probe", headers={"Authorization": "Bearer "}).status_code == 401


# --- Path (d): token configured, correct Bearer token -> principal ---

class TestTokenConfiguredBearer:
    TOKEN = "correct-secret-token-xyz"

    def test_correct_bearer_returns_200(self, monkeypatch):
        fn = _fresh_require_operator(monkeypatch, {"ALPHA_OPERATOR_TOKEN": self.TOKEN})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        assert client.get("/probe", headers={"Authorization": f"Bearer {self.TOKEN}"}).status_code == 200

    def test_correct_bearer_returns_operator_principal(self, monkeypatch):
        fn = _fresh_require_operator(monkeypatch, {"ALPHA_OPERATOR_TOKEN": self.TOKEN})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        r = client.get("/probe", headers={"Authorization": f"Bearer {self.TOKEN}"})
        assert r.json()["principal"] == "operator"

    def test_correct_bearer_with_x_actor_returns_x_actor(self, monkeypatch):
        fn = _fresh_require_operator(monkeypatch, {"ALPHA_OPERATOR_TOKEN": self.TOKEN})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        r = client.get("/probe", headers={"Authorization": f"Bearer {self.TOKEN}", "X-Actor": "bob"})
        assert r.json()["principal"] == "bob"

    def test_bearer_case_insensitive_prefix(self, monkeypatch):
        fn = _fresh_require_operator(monkeypatch, {"ALPHA_OPERATOR_TOKEN": self.TOKEN})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        assert client.get("/probe", headers={"Authorization": f"BEARER {self.TOKEN}"}).status_code == 200


# --- Path (e): token configured, correct X-Operator-Token fallback header ---

class TestTokenConfiguredXOperatorHeader:
    TOKEN = "correct-secret-token-xyz"

    def test_correct_x_operator_token_returns_200(self, monkeypatch):
        fn = _fresh_require_operator(monkeypatch, {"ALPHA_OPERATOR_TOKEN": self.TOKEN})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        assert client.get("/probe", headers={"X-Operator-Token": self.TOKEN}).status_code == 200

    def test_bearer_takes_priority_over_x_operator_token(self, monkeypatch):
        fn = _fresh_require_operator(monkeypatch, {"ALPHA_OPERATOR_TOKEN": self.TOKEN})
        client = TestClient(_app_with_guard(fn), raise_server_exceptions=False)
        r = client.get(
            "/probe",
            headers={"Authorization": f"Bearer {self.TOKEN}", "X-Operator-Token": "wrongvalue"},
        )
        assert r.status_code == 200

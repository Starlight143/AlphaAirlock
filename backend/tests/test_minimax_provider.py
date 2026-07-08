"""Direct MiniMax provider (P-MINIMAX-DIRECT, backend.agents._client).

MiniMax international (api.minimax.io) is reached directly via its OWN key, not
through OpenRouter. These lock in the wiring plus the two safety-critical
properties:

  * the provider reads ONLY ``MINIMAX_API_KEY`` — ``OPENROUTER_API_KEY`` is never
    consulted (so adding MiniMax cannot disturb an existing OpenRouter setup);
  * it suppresses OpenRouter's ``usage.include`` request extension, which a
    strict server like MiniMax may reject, and sends no OpenRouter analytics
    headers.

No network: constructing a provider only builds an httpx.Client; the suite never
issues a request.
"""
from __future__ import annotations

import pytest

import backend.agents._client as C

_REAL_KEY = "mm-sk-realtoken-1234567890"


@pytest.fixture(autouse=True)
def _isolate_provider(monkeypatch):
    """Fresh provider singleton + clean LLM env around every test."""
    for k in ("LLM_PROVIDER", "MINIMAX_API_KEY", "MINIMAX_MODEL",
              "MINIMAX_BASE_URL", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    C.reset_provider_for_tests()
    yield
    C.reset_provider_for_tests()


def test_resolve_provider_name_maps_minimax(monkeypatch):
    for alias in ("minimax", "MiniMax", "minimaxi", "minimax-m3"):
        monkeypatch.setenv("LLM_PROVIDER", alias)
        assert C._resolve_provider_name() == "minimax"
    # An unknown provider still raises, and the message now lists minimax.
    monkeypatch.setenv("LLM_PROVIDER", "nope")
    with pytest.raises(C.LLMProviderError) as exc:
        C._resolve_provider_name()
    assert "minimax" in str(exc.value)


def test_minimax_provider_defaults_and_independence(monkeypatch):
    # Only the MiniMax key is set; OpenRouter key is a placeholder. Construction
    # must succeed AND ignore the OpenRouter key entirely.
    monkeypatch.setenv("MINIMAX_API_KEY", _REAL_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "your_openrouter_key_here")  # placeholder

    prov = C.MiniMaxProvider()
    try:
        d = prov.describe()
        assert d["provider"] == "minimax"
        assert d["model"] == "MiniMax-M3"
        assert d["base_url"] == "https://api.minimax.io/v1"
        # The provider authenticates with the MiniMax token, never the OR key.
        assert prov._api_key == _REAL_KEY
        assert prov.API_KEY_ENV == "MINIMAX_API_KEY"
    finally:
        prov._client.close()


def test_minimax_missing_key_raises_about_minimax(monkeypatch):
    # No MiniMax key (even with a real OpenRouter key present) → fail naming the
    # MiniMax var, proving it does not fall back to OPENROUTER_API_KEY.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-realbutirrelevant-999")
    with pytest.raises(C.LLMProviderError) as exc:
        C.MiniMaxProvider()
    assert "MINIMAX_API_KEY" in str(exc.value)
    assert "OPENROUTER_API_KEY" not in str(exc.value)


def test_minimax_model_and_base_url_overrides(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", _REAL_KEY)
    monkeypatch.setenv("MINIMAX_MODEL", "MiniMax-M2.5")
    monkeypatch.setenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1/")  # trailing /
    prov = C.MiniMaxProvider()
    try:
        assert prov._default_model == "MiniMax-M2.5"
        assert prov._base_url == "https://api.minimax.io/v1"  # normalized
    finally:
        prov._client.close()


@pytest.mark.parametrize("bad_url", [
    "https://evil.com/v1",                 # wrong host
    "https://api.minimax.io.attacker.net", # suffix-spoof, not a *.minimax.io host
    "http://api.minimax.io/v1",            # not https
])
def test_minimax_ssrf_guard_rejects_foreign_host(monkeypatch, bad_url):
    monkeypatch.setenv("MINIMAX_API_KEY", _REAL_KEY)
    monkeypatch.setenv("MINIMAX_BASE_URL", bad_url)
    with pytest.raises(C.LLMProviderError) as exc:
        C.MiniMaxProvider()
    assert "minimax.io" in str(exc.value)


def test_minimax_suppresses_usage_include_and_or_headers(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", _REAL_KEY)
    # Even with usage accounting globally ON, MiniMax must not send the flag.
    monkeypatch.setenv("OPENROUTER_USAGE_ACCOUNTING", "1")
    prov = C.MiniMaxProvider()
    try:
        assert prov._wants_usage_accounting() is False
        h = prov._headers()
        assert h["Authorization"] == f"Bearer {_REAL_KEY}"
        assert h["Content-Type"] == "application/json"
        assert "X-Title" not in h and "HTTP-Referer" not in h
    finally:
        prov._client.close()


def test_openrouter_still_wants_usage_accounting_by_default(monkeypatch):
    # Guard the seam: the base provider's behaviour is unchanged (default ON).
    monkeypatch.setenv("OPENROUTER_API_KEY", _REAL_KEY)
    monkeypatch.delenv("OPENROUTER_USAGE_ACCOUNTING", raising=False)
    prov = C.OpenRouterProvider()
    try:
        assert prov._wants_usage_accounting() is True
    finally:
        prov._client.close()


def test_describe_provider_config_minimax(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "minimax")
    monkeypatch.setenv("MINIMAX_API_KEY", _REAL_KEY)
    cfg = C.describe_provider_config()
    assert cfg["resolved"] == "minimax"
    assert cfg["model"] == "MiniMax-M3"
    assert cfg["base_url"] == "https://api.minimax.io/v1"
    assert cfg["key_env_var"] == "MINIMAX_API_KEY"
    assert cfg["key_present"] is True
    assert cfg["configured"] is True


def test_get_provider_builds_minimax(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "minimax")
    monkeypatch.setenv("MINIMAX_API_KEY", _REAL_KEY)
    prov = C.get_provider()
    assert isinstance(prov, C.MiniMaxProvider)
    assert prov.name == "minimax"

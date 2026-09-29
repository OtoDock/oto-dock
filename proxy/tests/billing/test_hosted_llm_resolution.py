"""Hosted Direct-LLM resolution (``auth_type='relay'``).

``resolve_subscription_env`` / ``acquire_subscription`` mint a per-user relay token
and point the adapter at the per-provider relay endpoint; a configured BYO key
always wins. ``subscription_store`` + ``relay_client`` are mocked (no DB/network).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _relay_sub(provider: str = "anthropic", **o) -> dict:
    s = {"id": f"relay-{provider}", "layer": "direct-llm", "provider": provider,
         "auth_type": "relay", "is_primary": 1, "status": "active", "active_sessions": 0}
    s.update(o)
    return s


def _key_sub(provider: str = "anthropic", **o) -> dict:
    s = {"id": f"key-{provider}", "layer": "direct-llm", "provider": provider,
         "auth_type": "api_key", "is_primary": 1, "status": "active", "active_sessions": 0}
    s.update(o)
    return s


@pytest.fixture
def store(monkeypatch):
    m = MagicMock()
    m.get_user_allow_platform_auth.return_value = True
    m.get_credential_data.return_value = {}
    m.list_personal.return_value = []          # no personal sub unless a test sets one
    m.list_platform_pool.return_value = []     # tests populate the platform pool
    monkeypatch.setattr("services.engines.subscription_pool.subscription_store", m)
    monkeypatch.setattr("config.get_model_provider", lambda model, layer="": {
        "claude-sonnet-5": "anthropic",
        "gpt-5.4-mini": "openai",
        "qwen/qwen3.6-27b": "groq",
    }.get(model, "anthropic"))
    monkeypatch.setattr("config.OTODOCK_RELAY_BASE", "https://api.otodock.io")
    from services.engines import subscription_pool
    subscription_pool._session_subscriptions.clear()
    return m


@pytest.mark.parametrize("model,provider,suffix", [
    ("claude-sonnet-5", "anthropic", "/v1/relay/anthropic"),
    ("gpt-5.4-mini", "openai", "/v1/relay/openai/v1"),
    ("qwen/qwen3.6-27b", "groq", "/v1/relay/groq/v1"),
])
def test_relay_resolves_per_provider_endpoint(store, monkeypatch, model, provider, suffix):
    from services.engines import subscription_pool
    store.list_platform_pool.return_value = [_relay_sub(provider)]
    monkeypatch.setattr("services.billing.relay_client.mint_session_token",
                        lambda u: "TKN-" + (u or ""))

    sub_id, env = subscription_pool.resolve_subscription_env("direct-llm", "user-1", model)

    assert sub_id == f"relay-{provider}"
    assert env["_PROVIDER"] == provider
    assert env["_API_KEY"] == "TKN-user-1"            # minted per-user token
    assert env["_ENDPOINT_URL"] == "https://api.otodock.io" + suffix
    store.increment_active_sessions.assert_called_once_with(f"relay-{provider}")


def test_byo_key_wins_over_relay(store, monkeypatch):
    from services.engines import subscription_pool
    # Platform pool has BOTH a relay sub and a BYO api_key sub for anthropic.
    store.list_platform_pool.return_value = [_relay_sub("anthropic"), _key_sub("anthropic")]
    store.get_credential_data.side_effect = (
        lambda sid: {"api_key": "sk-byo"} if sid == "key-anthropic" else {}
    )
    minted: list = []
    monkeypatch.setattr("services.billing.relay_client.mint_session_token",
                        lambda u: minted.append(u) or "TKN")

    sub_id, env = subscription_pool.resolve_subscription_env(
        "direct-llm", "user-1", "claude-sonnet-5",
    )

    assert sub_id == "key-anthropic"
    assert env["_API_KEY"] == "sk-byo"     # BYO wins — relay is the fallback
    assert "_ENDPOINT_URL" not in env       # api_key sub points at the vendor
    assert minted == []                     # the relay token was never minted


def test_user_own_key_wins_over_platform_relay(store, monkeypatch):
    from services.engines import subscription_pool
    user_key = _key_sub("anthropic", id="user-key")
    store.list_personal.return_value = [user_key]
    store.list_platform_pool.return_value = [_relay_sub("anthropic")]
    store.get_credential_data.side_effect = (
        lambda sid: {"api_key": "sk-mine"} if sid == "user-key" else {}
    )
    minted: list = []
    monkeypatch.setattr("services.billing.relay_client.mint_session_token",
                        lambda u: minted.append(u) or "TKN")

    sub_id, env = subscription_pool.resolve_subscription_env(
        "direct-llm", "user-1", "claude-sonnet-5",
    )
    assert env["_API_KEY"] == "sk-mine" and minted == []


def test_relay_mint_failure_returns_no_creds(store, monkeypatch):
    from services.billing import relay_client
    from services.engines import subscription_pool
    store.list_platform_pool.return_value = [_relay_sub("anthropic")]

    def boom(u):
        raise relay_client.RelayNotConfigured("Out of OtoDock credits")
    monkeypatch.setattr("services.billing.relay_client.mint_session_token", boom)

    sub_id, env = subscription_pool.resolve_subscription_env(
        "direct-llm", "user-1", "claude-sonnet-5",
    )
    # Fail-soft: no creds (clean "no LLM credentials" error) + pool slot released.
    assert sub_id == "" and env == {}
    store.decrement_active_sessions.assert_called_once_with("relay-anthropic")


def test_acquire_returns_relay_when_only_option(store):
    from services.engines import subscription_pool
    store.list_platform_pool.return_value = [_relay_sub("openai")]

    handle = subscription_pool.acquire_subscription("direct-llm", "user-1", provider="openai")

    assert handle is not None
    assert handle.auth_type == "relay"
    assert handle.api_key is None and handle.endpoint_url is None
    store.increment_active_sessions.assert_called_once_with("relay-openai")


def test_relay_credentials_none_when_relay_base_unset(store, monkeypatch):
    from services.engines import subscription_pool
    monkeypatch.setattr("config.OTODOCK_RELAY_BASE", "")
    assert subscription_pool.relay_llm_credentials("direct-llm", "anthropic", "u") is None


def test_relay_credentials_none_for_local_provider(store):
    from services.engines import subscription_pool
    # Ollama / LiteLLM are local — never relay-backed.
    assert subscription_pool.relay_llm_credentials("direct-llm", "ollama", "u") is None

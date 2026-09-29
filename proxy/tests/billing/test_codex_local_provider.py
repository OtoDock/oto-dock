"""Codex pool acquisition follows the MODEL's provider.

With a ChatGPT account and a local OpenAI-compatible endpoint both in the
codex-cli pool, a local-model chat must get the local endpoint and a GPT chat
the ChatGPT account. Before, the codex branch read a non-existent agent field
and acquired by consumption alone. A key on the local endpoint rides its own
variable, never CODEX_API_KEY.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _oauth_sub(**o) -> dict:
    s = {"id": "oauth-openai", "layer": "codex-cli", "provider": "openai",
         "auth_type": "oauth", "status": "active", "active_sessions": 0}
    s.update(o)
    return s


def _local_sub(**o) -> dict:
    s = {"id": "local-compat", "layer": "codex-cli", "provider": "openai_compatible",
         "auth_type": "local_endpoint", "status": "active", "active_sessions": 0}
    s.update(o)
    return s


@pytest.fixture
def store(monkeypatch):
    m = MagicMock()
    m.get_user_allow_platform_auth.return_value = True
    m.list_personal.return_value = []
    pool = [_oauth_sub(), _local_sub()]

    def _pool(layer=None, provider=None):
        return [s for s in pool if not provider or s["provider"] == provider]

    m.list_platform_pool.side_effect = _pool
    m.get_credential_data.side_effect = lambda sid: {
        "oauth-openai": {"oauth_token": {"accessToken": "tok", "expiresAt": 4102444800000}},
        "local-compat": {"endpoint_url": "http://192.168.1.8:8080/v1", "api_key": "llama-key"},
    }[sid]
    m.get_subscription_consumption.return_value = 0.0
    monkeypatch.setattr("services.engines.subscription_pool.subscription_store", m)
    monkeypatch.setattr("config.get_model_provider", lambda model, layer="": {
        "gpt-5.6-sol": "openai",
        "qwen3.6-35b-a3b": "openai_compatible",
    }[model])
    from services.engines import subscription_pool
    monkeypatch.setattr(
        subscription_pool, "_resolve_oauth_access_token",
        lambda sub, oauth: ("tok", 4102444800000),
    )
    subscription_pool._session_subscriptions.clear()
    return m


def test_local_model_acquires_the_local_endpoint(store):
    from services.engines import subscription_pool
    sub_id, env = subscription_pool.resolve_subscription_env(
        "codex-cli", None, "qwen3.6-35b-a3b",
    )
    assert sub_id == "local-compat"
    assert env["_CODEX_ENDPOINT_URL"] == "http://192.168.1.8:8080/v1"
    assert env["_CODEX_LOCAL_API_KEY"] == "llama-key"
    assert env["_CODEX_ENDPOINT_PROVIDER"] == "openai_compatible"
    assert "CODEX_API_KEY" not in env


def test_gpt_model_acquires_the_chatgpt_account(store):
    from services.engines import subscription_pool
    sub_id, env = subscription_pool.resolve_subscription_env(
        "codex-cli", None, "gpt-5.6-sol",
    )
    assert sub_id == "oauth-openai"
    assert "_CODEX_ENDPOINT_URL" not in env
    assert "_CODEX_LOCAL_API_KEY" not in env
    assert "_CODEX_ENDPOINT_PROVIDER" not in env


def test_a_single_provider_engine_never_filters_by_a_guessed_provider(store, monkeypatch):
    # Claude declares one provider (engine-contract phase 5e); the pool's
    # model filter is for engines whose rows come from SEVERAL. A model id the
    # registry and the rows do not know is guessed by prefix — "gpt-…" reads
    # as openai — and that guess must not exclude every Claude account.
    from services.engines import subscription_pool
    claude_key = {"id": "key-anthropic", "layer": "claude-code-cli", "provider": "anthropic",
                  "auth_type": "api_key", "status": "active", "active_sessions": 0}
    store.list_platform_pool.side_effect = (
        lambda layer=None, provider=None: [claude_key] if not provider or provider == "anthropic" else []
    )
    store.get_credential_data.side_effect = lambda sid: {"key-anthropic": {"api_key": "sk-ant"}}[sid]
    monkeypatch.setattr("config.get_model_provider", lambda model, layer="": "openai")
    sub_id, _env = subscription_pool.resolve_subscription_env(
        "claude-code-cli", None, "gpt-mystery-1",
    )
    assert sub_id == "key-anthropic"

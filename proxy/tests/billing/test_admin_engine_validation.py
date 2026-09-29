"""The admin subscription API validates a request against the ENGINE's
declaration (engine-contract lane, phase 3a), not against module-level maps
that listed the union across every engine.

Three requests the old code accepted and stored as rows the pool could never
use are refused now: a provider the engine does not speak (an ``openai`` key
on ``claude-code-cli`` was handed out as ``ANTHROPIC_API_KEY``), an auth type
the engine does not take (a ``local_endpoint`` on Claude Code, which dials
nothing), and ``auth_type="oauth"`` (which stored a row with an EMPTY
credential — an OAuth row comes only from its login flow). The
Direct-LLM-only phone push became a hook every engine may implement.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

import api.admin.execution_layers as api_mod
from services.engines import subscription_pool


def _admin(sub="admin-1"):
    return SimpleNamespace(sub=sub, role="admin", is_admin=True, is_api_key=False)


def _run(coro):
    return asyncio.run(coro)


def _add(layer, provider, auth_type, **kw):
    req = api_mod.AddSubscriptionRequest(
        provider=provider, auth_type=auth_type, api_key=kw.pop("api_key", "sk-x"),
        endpoint_url=kw.pop("endpoint_url", None), **kw,
    )
    return api_mod.admin_add_subscription(layer, req, user=_admin())


class TestAdminAddValidatesAgainstTheEngine:
    def _refused(self, layer, provider, auth_type, **kw) -> str:
        with patch.object(api_mod, "subscription_store") as store, \
             patch.object(api_mod, "subscription_pool"), \
             patch.object(api_mod, "_subscriptions_changed", new_callable=AsyncMock), \
             patch.object(api_mod.config, "OTODOCK_CLOUD", False):
            with pytest.raises(HTTPException) as ei:
                _run(_add(layer, provider, auth_type, **kw))
            store.add_subscription.assert_not_called()
        assert ei.value.status_code == 400
        return ei.value.detail

    def test_provider_must_be_one_the_engine_speaks(self):
        detail = self._refused("claude-code-cli", "openai", "api_key")
        assert "provider" in detail and "claude-code-cli" in detail

    def test_auth_type_must_be_one_the_engine_takes(self):
        detail = self._refused("claude-code-cli", "anthropic", "local_endpoint",
                               endpoint_url="http://h:8080/v1")
        assert "auth_type" in detail

    def test_relay_only_where_the_engine_declares_it(self):
        assert "auth_type" in self._refused("codex-cli", "openai", "relay")

    def test_oauth_rows_come_only_from_the_login_flow(self):
        detail = self._refused("claude-code-cli", "anthropic", "oauth")
        assert "login flow" in detail

    def test_hosted_message_wins_for_a_local_provider_on_cloud(self):
        with patch.object(api_mod, "subscription_store") as store, \
             patch.object(api_mod.config, "OTODOCK_CLOUD", True):
            with pytest.raises(HTTPException) as ei:
                _run(_add("claude-code-cli", "ollama", "local_endpoint",
                          endpoint_url="http://h:11434/v1"))
            store.add_subscription.assert_not_called()
        assert ei.value.detail == api_mod._CLOUD_LOCAL_MSG

    def test_a_valid_key_is_stored_and_the_engine_is_told(self):
        with patch.object(api_mod, "subscription_store") as store, \
             patch.object(api_mod, "subscription_pool") as pool, \
             patch.object(api_mod, "_subscriptions_changed", new_callable=AsyncMock) as told, \
             patch.object(api_mod.config, "OTODOCK_CLOUD", False):
            store.add_subscription.return_value = {"id": "s1"}
            out = _run(_add("claude-code-cli", "anthropic", "api_key", api_key="sk-ant"))
        assert out == {"id": "s1"}
        kw = store.add_subscription.call_args.kwargs
        assert kw["layer"] == "claude-code-cli" and kw["provider"] == "anthropic"
        assert kw["credential_data"] == {"api_key": "sk-ant"}
        pool.schedule_rebind.assert_called_once()
        told.assert_awaited_once_with("claude-code-cli")

    def test_every_provider_of_a_multi_provider_engine_is_accepted(self):
        with patch.object(api_mod, "subscription_store") as store, \
             patch.object(api_mod, "subscription_pool"), \
             patch.object(api_mod, "_subscriptions_changed", new_callable=AsyncMock), \
             patch.object(api_mod.config, "OTODOCK_CLOUD", False):
            store.add_subscription.return_value = {"id": "s"}
            for provider in ("anthropic", "openai", "groq"):
                _run(_add("direct-llm", provider, "api_key"))
            _run(_add("codex-cli", "openai", "api_key"))
        assert store.add_subscription.call_count == 4


class TestSubscriptionsChangedHook:
    def test_direct_llm_pushes_the_phone_config_and_others_do_not(self):
        with patch("services.phone.phone_config.notify_phone_config_changed",
                   new_callable=AsyncMock) as push:
            _run(api_mod._subscriptions_changed("claude-code-cli", "codex-cli"))
            push.assert_not_awaited()
            _run(api_mod._subscriptions_changed("direct-llm", "codex-cli"))
            push.assert_awaited_once()

    def test_an_unregistered_engine_is_skipped_not_raised(self):
        # A mutation of an EXISTING row on an engine that is no longer
        # registered must not start raising.
        _run(api_mod._subscriptions_changed("acme-cli"))


class TestUserKeyProvider:
    def test_an_engine_with_a_vendor_takes_the_vendors_key(self):
        assert api_mod._user_key_provider("claude-code-cli") == "anthropic"
        assert api_mod._user_key_provider("codex-cli") == "openai"

    def test_a_multi_provider_engine_without_a_vendor_takes_none(self):
        assert api_mod._user_key_provider("direct-llm") == ""
        assert api_mod._user_key_provider("acme-cli") == ""

    def test_user_add_names_the_eligible_engines(self):
        req = api_mod.AddSubscriptionRequest(provider="anthropic", auth_type="api_key", api_key="k")
        with pytest.raises(HTTPException) as ei:
            _run(api_mod.user_add_subscription("direct-llm", req, user=_admin()))
        assert ei.value.status_code == 400
        assert "claude-code-cli" in ei.value.detail and "codex-cli" in ei.value.detail


class TestLayerProviders:
    def test_single_vendor_engine_lists_its_vendor(self):
        from core.session.session_manager import capabilities_for_path
        assert api_mod._layer_providers(capabilities_for_path("claude-code-cli")) == ["anthropic"]

    def test_multi_provider_engines_list_their_declared_providers(self):
        from core.session.session_manager import capabilities_for_path
        codex = api_mod._layer_providers(capabilities_for_path("codex-cli"))
        direct = api_mod._layer_providers(capabilities_for_path("direct-llm"))
        assert codex[0] == "openai" and "anthropic" not in codex
        assert {"anthropic", "openai", "groq"} <= set(direct)

    def test_local_endpoint_engines_come_from_the_declarations(self):
        assert api_mod._layers_with_auth_type("local_endpoint") == ["direct-llm", "codex-cli"]
        assert api_mod._layers_with_auth_type("oauth") == ["claude-code-cli", "codex-cli"]
        assert api_mod._layers_with_auth_type("relay") == ["direct-llm"]


class TestAutoEnableCandidates:
    def test_coding_engines_in_page_order(self):
        assert subscription_pool._auto_enable_candidates() == ["claude-code-cli", "codex-cli"]

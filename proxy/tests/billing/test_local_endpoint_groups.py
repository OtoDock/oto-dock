"""Local endpoints listed once across the engines.

Subscription rows stay per layer; the grouping by (provider, endpoint_url)
is the only link between the direct-llm and codex-cli siblings. Covers the
pure grouping, the create-for-both-engines endpoint, the per-engine toggle
(create the missing sibling / re-activate / disable) and removal."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from storage.billing import subscription_store as store_mod


def _row(layer, *, sub_id, provider="openai_compatible", status="active",
         owner="admin-1", label="", sessions=0):
    return {
        "id": sub_id, "layer": layer, "provider": provider, "auth_type": "local_endpoint",
        "status": status, "owner_sub": owner, "label": label, "active_sessions": sessions,
    }


def test_group_by_provider_and_normalized_url():
    rows = [
        (_row("direct-llm", sub_id="d1", label="Local Qwen"),
         {"endpoint_url": "http://192.168.1.8:8080/v1/", "api_key": "k"}),
        (_row("codex-cli", sub_id="c1"),
         {"endpoint_url": " http://192.168.1.8:8080/v1"}),
        (_row("direct-llm", sub_id="d2", provider="ollama"),
         {"endpoint_url": "http://192.168.1.8:11434"}),
    ]
    groups = store_mod.group_local_endpoint_rows(rows)
    assert [g["provider"] for g in groups] == ["openai_compatible", "ollama"]
    shared = groups[0]
    assert shared["endpoint_url"] == "http://192.168.1.8:8080/v1"
    assert shared["label"] == "Local Qwen"
    assert shared["has_api_key"] is True
    assert set(shared["engines"]) == {"direct-llm", "codex-cli"}
    assert shared["engines"]["codex-cli"]["id"] == "c1"
    assert shared["group"] == store_mod.local_endpoint_group_key(
        "openai_compatible", "http://192.168.1.8:8080/v1/")
    assert groups[1]["has_api_key"] is False


class TestLocalEndpointApi:
    def _admin(self, sub="admin-1"):
        return SimpleNamespace(sub=sub, role="admin", is_admin=True)

    def _run(self, coro):
        return asyncio.run(coro)

    def _group(self, engines, provider="openai_compatible", url="http://h:8080/v1",
               has_key=False, owner="admin-1"):
        return {
            "group": store_mod.local_endpoint_group_key(provider, url),
            "provider": provider, "endpoint_url": url, "label": "", "has_api_key": has_key,
            "engines": {
                layer: {"id": f"{layer}-id", "status": status, "active_sessions": 0,
                        "owner_sub": owner}
                for layer, status in engines.items()
            },
        }

    def test_add_creates_one_row_per_engine(self):
        import api.admin.execution_layers as api_mod
        req = api_mod.AddLocalEndpointRequest(
            provider="openai_compatible", endpoint_url="http://h:8080/v1/",
            label="L", api_key="secret", layers=["direct-llm", "codex-cli", "codex-cli"],
        )
        groups = [[], [self._group({"direct-llm": "active", "codex-cli": "active"})]]
        with patch.object(api_mod, "subscription_store") as store, \
             patch.object(api_mod, "subscription_pool") as pool, \
             patch.object(api_mod, "_subscriptions_changed", new_callable=AsyncMock) as notify, \
             patch.object(api_mod.config, "OTODOCK_CLOUD", False):
            store.normalize_endpoint_url.side_effect = store_mod.normalize_endpoint_url
            store.local_endpoint_group_key.side_effect = store_mod.local_endpoint_group_key
            store.list_local_endpoint_groups.side_effect = lambda: groups.pop(0)
            out = self._run(api_mod.admin_add_local_endpoint(req, user=self._admin()))
        layers = [c.kwargs["layer"] for c in store.add_subscription.call_args_list]
        assert layers == ["direct-llm", "codex-cli"]
        for c in store.add_subscription.call_args_list:
            assert c.kwargs["credential_data"] == {"endpoint_url": "http://h:8080/v1", "api_key": "secret"}
            assert c.kwargs["contribute_platform"] is True and c.kwargs["owner_sub"] == "admin-1"
        assert set(out["engines"]) == {"direct-llm", "codex-cli"}
        assert out["engines"]["codex-cli"]["is_mine"] is True
        pool.schedule_rebind.assert_called_once()
        # Each engine the endpoint was added to hears about it (Direct LLM
        # pushes the phone config; Codex has nothing to do).
        notify.assert_awaited_once_with("direct-llm", "codex-cli")

    def test_add_stores_ollama_as_its_openai_compatible_base(self):
        import api.admin.execution_layers as api_mod
        req = api_mod.AddLocalEndpointRequest(
            provider="ollama", endpoint_url="http://h:11434/", layers=["codex-cli"],
        )
        url = "http://h:11434/v1"
        groups = [[], [self._group({"codex-cli": "active"}, provider="ollama", url=url)]]
        with patch.object(api_mod, "subscription_store") as store, \
             patch.object(api_mod, "subscription_pool"), \
             patch.object(api_mod, "_subscriptions_changed", new_callable=AsyncMock), \
             patch.object(api_mod.config, "OTODOCK_CLOUD", False):
            store.normalize_endpoint_url.side_effect = store_mod.normalize_endpoint_url
            store.local_endpoint_group_key.side_effect = store_mod.local_endpoint_group_key
            store.list_local_endpoint_groups.side_effect = lambda: groups.pop(0)
            self._run(api_mod.admin_add_local_endpoint(req, user=self._admin()))
        assert store.add_subscription.call_args.kwargs["credential_data"] == {"endpoint_url": url}

    def test_add_rejects_empty_layers_duplicates_and_cloud(self):
        import api.admin.execution_layers as api_mod
        from fastapi import HTTPException
        with patch.object(api_mod, "subscription_store") as store, \
             patch.object(api_mod.config, "OTODOCK_CLOUD", False):
            store.normalize_endpoint_url.side_effect = store_mod.normalize_endpoint_url
            store.local_endpoint_group_key.side_effect = store_mod.local_endpoint_group_key
            store.list_local_endpoint_groups.return_value = [self._group({"direct-llm": "active"})]
            with pytest.raises(HTTPException) as ei:
                self._run(api_mod.admin_add_local_endpoint(api_mod.AddLocalEndpointRequest(
                    provider="ollama", endpoint_url="http://x", layers=[]), user=self._admin()))
            assert ei.value.status_code == 400
            with pytest.raises(HTTPException) as ei:
                self._run(api_mod.admin_add_local_endpoint(api_mod.AddLocalEndpointRequest(
                    provider="ollama", endpoint_url="http://x", layers=["claude-code-cli"]),
                    user=self._admin()))
            assert ei.value.status_code == 400
            with pytest.raises(HTTPException) as ei:
                self._run(api_mod.admin_add_local_endpoint(api_mod.AddLocalEndpointRequest(
                    provider="openai_compatible", endpoint_url="http://h:8080/v1",
                    layers=["codex-cli"]), user=self._admin()))
            assert ei.value.status_code == 409
        with patch.object(api_mod.config, "OTODOCK_CLOUD", True):
            with pytest.raises(HTTPException) as ei:
                self._run(api_mod.admin_add_local_endpoint(api_mod.AddLocalEndpointRequest(
                    provider="ollama", endpoint_url="http://x", layers=["direct-llm"]),
                    user=self._admin()))
            assert ei.value.status_code == 400

    def test_enable_missing_engine_copies_the_credential(self):
        import api.admin.execution_layers as api_mod
        g = self._group({"direct-llm": "active"}, has_key=True)
        with patch.object(api_mod, "subscription_store") as store, \
             patch.object(api_mod, "subscription_pool"), \
             patch.object(api_mod, "_subscriptions_changed", new_callable=AsyncMock):
            store.list_local_endpoint_groups.return_value = [g]
            store.get_credential_data.return_value = {"endpoint_url": "http://h:8080/v1", "api_key": "k"}
            self._run(api_mod.admin_set_local_endpoint_engine(
                g["group"], api_mod.SetLocalEndpointEngineRequest(layer="codex-cli", enabled=True),
                user=self._admin()))
            kw = store.add_subscription.call_args.kwargs
            assert kw["layer"] == "codex-cli" and kw["provider"] == "openai_compatible"
            assert kw["credential_data"] == {"endpoint_url": "http://h:8080/v1", "api_key": "k"}
            store.update_subscription.assert_not_called()

    def test_toggle_flips_status_and_leaves_models_alone(self):
        import api.admin.execution_layers as api_mod
        g = self._group({"direct-llm": "active", "codex-cli": "disabled"})
        with patch.object(api_mod, "subscription_store") as store, \
             patch.object(api_mod, "subscription_pool"), \
             patch.object(api_mod, "_subscriptions_changed", new_callable=AsyncMock) as notify:
            store.list_local_endpoint_groups.return_value = [g]
            self._run(api_mod.admin_set_local_endpoint_engine(
                g["group"], api_mod.SetLocalEndpointEngineRequest(layer="direct-llm", enabled=False),
                user=self._admin()))
            store.update_subscription.assert_called_once_with("direct-llm-id", status="disabled")
            notify.assert_awaited_once_with("direct-llm")
            store.update_subscription.reset_mock()
            self._run(api_mod.admin_set_local_endpoint_engine(
                g["group"], api_mod.SetLocalEndpointEngineRequest(layer="codex-cli", enabled=True),
                user=self._admin()))
            store.update_subscription.assert_called_once_with("codex-cli-id", status="active")
            store.add_subscription.assert_not_called()
            store.update_model.assert_not_called()

    def test_other_admins_endpoint_is_read_only(self):
        import api.admin.execution_layers as api_mod
        from fastapi import HTTPException
        g = self._group({"direct-llm": "active"}, owner="admin-2")
        with patch.object(api_mod, "subscription_store") as store, \
             patch.object(api_mod, "subscription_pool"):
            store.list_local_endpoint_groups.return_value = [g]
            with pytest.raises(HTTPException) as ei:
                self._run(api_mod.admin_set_local_endpoint_engine(
                    g["group"], api_mod.SetLocalEndpointEngineRequest(layer="codex-cli", enabled=True),
                    user=self._admin()))
            assert ei.value.status_code == 403
            with pytest.raises(HTTPException) as ei:
                self._run(api_mod.admin_delete_local_endpoint(g["group"], user=self._admin()))
            assert ei.value.status_code == 403

    def test_delete_removes_every_sibling_unless_busy(self):
        # Live bindings decide (the stored counter is reconciled first):
        # every sibling row goes when nothing is live; a live session on
        # any engine refuses with 409 unless force=true.
        import api.admin.execution_layers as api_mod
        from fastapi import HTTPException
        g = self._group({"direct-llm": "active", "codex-cli": "active"})
        live = {"codex-cli-id": 0, "direct-llm-id": 0}
        with patch.object(api_mod, "subscription_store") as store, \
             patch.object(api_mod, "subscription_pool") as pool, \
             patch.object(api_mod, "_subscriptions_changed", new_callable=AsyncMock):
            store.list_local_endpoint_groups.return_value = [g]
            pool.reconcile_active_sessions.side_effect = lambda sid: (live[sid], live[sid])
            self._run(api_mod.admin_delete_local_endpoint(g["group"], user=self._admin()))
            deleted = sorted(c.args[0] for c in store.delete_subscription.call_args_list)
            assert deleted == ["codex-cli-id", "direct-llm-id"]
            live["codex-cli-id"] = 2
            with pytest.raises(HTTPException) as ei:
                self._run(api_mod.admin_delete_local_endpoint(g["group"], user=self._admin()))
            assert ei.value.status_code == 409
            assert "codex-cli (2)" in ei.value.detail
            store.delete_subscription.reset_mock()
            self._run(api_mod.admin_delete_local_endpoint(
                g["group"], force=True, user=self._admin()))
            assert store.delete_subscription.call_count == 2

    def test_bulk_add_targets_every_listed_engine(self):
        import api.admin.execution_layers as api_mod
        req = api_mod.BulkAddModelsRequest(
            models=[{"model_id": "m1", "display_name": "m1"}], provider="openai_compatible",
            layers=["direct-llm", "codex-cli", "direct-llm"],
        )
        with patch.object(api_mod, "subscription_store") as store:
            store.add_model.side_effect = lambda **kw: {"layer": kw["layer"], "model_id": kw["model_id"]}
            out = self._run(api_mod.admin_bulk_add_models("direct-llm", req, user=self._admin()))
        assert [(m["layer"], m["model_id"]) for m in out["models"]] == [
            ("direct-llm", "m1"), ("codex-cli", "m1"),
        ]

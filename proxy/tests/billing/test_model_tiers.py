"""Capability tiers on the model catalog: the registry carries them, the
sync stamps builtins, an admin tags custom rows, and every read surface
gets the same answer for a model id."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config as app_config
from auth.providers import UserContext, get_current_user
from storage.billing import subscription_store


class TestRegistry:
    def test_every_builtin_carries_a_tier_and_a_good_at_line(self):
        for model_id, entry in app_config.MODEL_REGISTRY.items():
            assert entry.get("tier") in app_config.MODEL_TIER_LABELS, model_id
            good_at = entry.get("good_at") or ""
            assert good_at, model_id
            assert len(good_at) <= app_config.MODEL_GOOD_AT_MAX_CHARS, model_id

    def test_the_ranking_the_registry_comments_describe(self):
        tier = {m: e["tier"] for m, e in app_config.MODEL_REGISTRY.items()}
        assert tier["claude-fable-5-1"] < tier["claude-opus-5-5"] < tier["claude-sonnet-5"]
        assert tier["gpt-6-astra"] < tier["gpt-6-sol"] < tier["gpt-5.6-terra"] < tier["gpt-6-luna"]
        assert tier["claude-fable-5-1"] == tier["gpt-6-astra"] == 1

    def test_get_model_tier_reads_the_registry_first(self):
        assert app_config.get_model_tier("claude-fable-5-1") == (
            1, "frontier", app_config.MODEL_REGISTRY["claude-fable-5-1"]["good_at"])
        assert app_config.get_model_tier("no-such-model") == (None, "", "")

    def test_layer_models_emit_the_tier_fields(self):
        by_id = {m["value"]: m for m in app_config.get_layer_models("claude-code-cli")}
        assert by_id["claude-opus-5-5"]["tier"] == 2
        assert by_id["claude-opus-5-5"]["tier_label"] == "strong"
        assert by_id["claude-opus-5-5"]["good_at"]
        assert by_id[""]["label"] == "System Default" and "tier" not in by_id[""]

    def test_sort_key_orders_tier_then_builtin_then_registry_order(self):
        rows = [
            {"model_id": "custom-untiered", "is_builtin": False, "created_at": "2"},
            {"model_id": "claude-sonnet-5", "is_builtin": True, "tier": 3},
            {"model_id": "custom-fast", "is_builtin": False, "tier": 4, "created_at": "1"},
            {"model_id": "claude-fable-5-1", "is_builtin": True, "tier": 1},
            {"model_id": "gpt-6-astra", "is_builtin": True, "tier": 1},
        ]
        rows.sort(key=app_config.model_catalog_sort_key)
        assert [r["model_id"] for r in rows] == [
            "claude-fable-5-1", "gpt-6-astra", "claude-sonnet-5", "custom-fast", "custom-untiered",
        ]


@pytest.mark.usefixtures("temp_db")
class TestStore:
    def test_sync_stamps_builtins_and_keeps_a_custom_tag(self):
        subscription_store.sync_builtin_models(
            "claude-code-cli", app_config.get_layer_models("claude-code-cli"))
        rows = {m["model_id"]: m for m in subscription_store.list_models("claude-code-cli")}
        assert rows["claude-fable-5-1"]["tier"] == 1
        assert rows["claude-fable-5-1"]["good_at"] == app_config.MODEL_REGISTRY["claude-fable-5-1"]["good_at"]
        custom = subscription_store.add_model(
            "claude-code-cli", "local-x", "Local X", provider="ollama", tier=4, good_at="quick")
        assert custom["tier"] == 4 and custom["good_at"] == "quick"
        # A second sync leaves the admin's tag alone.
        subscription_store.sync_builtin_models(
            "claude-code-cli", app_config.get_layer_models("claude-code-cli"))
        again = subscription_store.get_model(custom["id"])
        assert again["tier"] == 4 and again["good_at"] == "quick"
        # get_model_tier falls through to the tagged row for a custom id.
        assert app_config.get_model_tier("local-x") == (4, "fast", "quick")

    def test_update_propagates_the_tier_to_every_row_of_the_id(self):
        a = subscription_store.add_model("codex-cli", "local-y", "Local Y", provider="ollama")
        b = subscription_store.add_model("direct-llm", "local-y", "Local Y", provider="ollama")
        assert a["tier"] is None and b["tier"] is None
        subscription_store.update_model(a["id"], tier=3, good_at="drafts")
        assert subscription_store.get_model(b["id"])["tier"] == 3
        assert subscription_store.get_model(b["id"])["good_at"] == "drafts"
        subscription_store.update_model(b["id"], clear_tier=True)
        assert subscription_store.get_model(a["id"])["tier"] is None
        # A re-add never changes a tier (ON CONFLICT DO NOTHING).
        subscription_store.add_model("codex-cli", "local-y", "Local Y", tier=1)
        assert subscription_store.get_model(a["id"])["tier"] is None


@pytest.fixture
def admin_client(temp_db):
    from api.admin import execution_layers as admin_api

    app = FastAPI()
    app.include_router(admin_api.router)

    async def _admin():
        return UserContext(sub="user-admin", email="admin@test.com", name="Admin",
                           role="admin", agents=[], agent_roles={})

    app.dependency_overrides[get_current_user] = _admin
    return TestClient(app)


class TestAdminRoutes:
    def test_custom_row_takes_a_tier_and_refuses_bad_values(self, admin_client):
        r = admin_client.post("/v1/admin/execution-layers/codex-cli/models", json={
            "model_id": "local-z", "display_name": "Local Z", "provider": "ollama",
            "tier": 4, "good_at": "quick",
        })
        assert r.status_code == 200 and r.json()["tier"] == 4
        row_id = r.json()["id"]
        r = admin_client.put(f"/v1/admin/execution-layers/codex-cli/models/{row_id}", json={"tier": 2})
        assert r.status_code == 200 and r.json()["tier"] == 2
        r = admin_client.put(f"/v1/admin/execution-layers/codex-cli/models/{row_id}", json={"tier": None})
        assert r.status_code == 200 and r.json()["tier"] is None
        r = admin_client.put(f"/v1/admin/execution-layers/codex-cli/models/{row_id}", json={"tier": 9})
        assert r.status_code == 400
        r = admin_client.put(f"/v1/admin/execution-layers/codex-cli/models/{row_id}",
                             json={"good_at": "x" * 200})
        assert r.status_code == 400

    def test_builtin_row_refuses_a_tier_edit(self, admin_client):
        subscription_store.sync_builtin_models(
            "claude-code-cli", app_config.get_layer_models("claude-code-cli"))
        row = next(m for m in subscription_store.list_models("claude-code-cli")
                   if m["model_id"] == "claude-opus-5-5")
        r = admin_client.put(f"/v1/admin/execution-layers/claude-code-cli/models/{row['id']}",
                             json={"tier": 1})
        assert r.status_code == 400
        # Enable/disable still works on a builtin.
        r = admin_client.put(f"/v1/admin/execution-layers/claude-code-cli/models/{row['id']}",
                             json={"enabled": False})
        assert r.status_code == 200 and r.json()["enabled"] is False

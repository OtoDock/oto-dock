"""The execution-layer catalog's per-install extras (engine-contract phase 5).

``GET /v1/execution-layers`` serves, beside each engine's descriptor and its
``configured`` flag, ``auto_model`` / ``auto_model_label`` — the model an
UNPINNED agent on that engine runs right now, as the resolver decides it
(``config.resolve_layer_default_model``: declared default → fall-down, within
what the platform pool serves). The dashboard's "Auto — <label>" option
renders THAT and never re-derives it; the label rides along because the
catalog's ``models`` filter is not the resolver's filter (see the mismatch
test), so the model may be absent from the list it would otherwise be looked
up in.

``GET /v1/users/me/execution-layers`` rows carry the engine's ``capabilities``
so the user card reads the descriptor instead of comparing the engine id.

Run: cd proxy && venv/bin/pytest tests/agents/test_execution_layers_catalog.py -v
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ADMIN = "user-admin"
MEMBER = "user-viewer"


@pytest.fixture
def client(temp_db):
    from app import app
    return TestClient(app)


def _cookie(sub: str, email: str, role: str) -> dict[str, str]:
    from auth.providers import create_session_jwt
    return {"Cookie": f"session={create_session_jwt(sub, email, 'T User', role)}"}


def _member():
    return _cookie(MEMBER, "viewer@test.com", "member")


@pytest.fixture(autouse=True)
def _builtins(temp_db):
    """The builtin model rows, synced the way boot syncs them
    (``startup.py``): the catalog GET only reads, so a fresh database has
    no rows until this runs."""
    _sync_builtins()


def _sync_builtins() -> None:
    from core.session.session_manager import get_all_capabilities
    from storage.billing import subscription_store
    for path, caps in get_all_capabilities().items():
        subscription_store.sync_builtin_models(path, caps.get("models", []))


def _catalog(client) -> dict:
    r = client.get("/v1/execution-layers", headers=_member())
    assert r.status_code == 200
    return r.json()


def _declared(layer: str) -> str:
    from core.session.session_manager import capabilities_for_path
    return capabilities_for_path(layer).model_policy.default_model


def _set_enabled(layer: str, model_id: str, enabled: bool) -> None:
    from storage.billing import subscription_store
    row = next(m for m in subscription_store.list_models(layer=layer)
               if m["model_id"] == model_id)
    subscription_store.update_model(row["id"], enabled=enabled)


def _pool_sub(layer: str, provider: str) -> None:
    """An admin's pool contribution — what ``list_platform_pool`` (the
    resolver's filter) and ``list_subscriptions(contribute_platform=True)``
    (the catalog's) both count."""
    from storage.billing import subscription_store
    subscription_store.add_subscription(
        layer, provider, "api_key", owner_sub=ADMIN,
        use_personal=False, contribute_platform=True,
    )


class TestAutoModel:
    def test_auto_is_the_declared_default_when_it_is_enabled(self, client, temp_db):
        for layer in ("claude-code-cli", "codex-cli", "direct-llm"):
            entry = _catalog(client)[layer]
            declared = _declared(layer)
            assert entry["auto_model"] == declared, layer
            served = {m["value"]: m["label"] for m in entry["models"]}
            assert declared in served, layer
            # The label is the row's display name — what the "Auto — …" option shows.
            assert entry["auto_model_label"] == served[declared], layer

    def test_disabling_the_declared_default_falls_down_the_tiers(self, client, temp_db):
        import config
        layer = "claude-code-cli"
        declared = _declared(layer)
        _set_enabled(layer, declared, False)
        entry = _catalog(client)[layer]
        auto = entry["auto_model"]
        assert auto and auto != declared
        # Never up: the fallback's tier is at or below the declared default's
        # (numerically higher or equal), and the disabled row is gone from the list.
        assert config.get_model_tier(auto)[0] >= config.get_model_tier(declared)[0]
        served = {m["value"] for m in entry["models"]}
        assert declared not in served and auto in served
        assert entry["auto_model_label"]

    def test_nothing_enabled_gives_an_empty_auto(self, client, temp_db):
        from storage.billing import subscription_store
        layer = "codex-cli"
        rows = subscription_store.list_models(layer=layer)
        assert rows
        for m in rows:
            subscription_store.update_model(m["id"], enabled=False)
        entry = _catalog(client)[layer]
        assert entry["auto_model"] == "" and entry["auto_model_label"] == ""
        assert [m["value"] for m in entry["models"]] == [""]  # only "System Default"

    def test_auto_survives_a_models_list_that_filtered_it_out(self, client, temp_db):
        """The two filters differ, and the catalog says what RUNS.

        The resolver restricts to providers the pool serves only when that
        leaves something (``served = []`` → no filter); the catalog's
        ``model_filter_policy='all'`` filter (Direct LLM) is hard. A pool of
        one provider with none of its models enabled therefore lists only
        "System Default" while the resolver still answers the declared
        default — and the catalog must carry that answer with its label.
        """
        layer = "direct-llm"
        before = _catalog(client)[layer]
        real = [m for m in before["models"] if m["value"]]  # not the "System Default" placeholder
        declared = _declared(layer)
        declared_provider = next(m["provider"] for m in real if m["value"] == declared)
        other = next(p for p in sorted({m["provider"] for m in real}) if p and p != declared_provider)
        for m in real:
            if m["provider"] == other:
                _set_enabled(layer, m["value"], False)
        _pool_sub(layer, other)

        entry = _catalog(client)[layer]
        assert [m["value"] for m in entry["models"]] == [""]
        assert entry["auto_model"] == declared
        assert entry["auto_model_label"] and entry["auto_model_label"] != declared


class TestResolverFactoring:
    def test_agent_resolution_and_layer_resolution_agree_for_an_unpinned_agent(self, client, temp_db):
        import config
        from storage.agents import agent_store
        agent_store.create_agent("catalog-demo", "Catalog Demo", created_by=ADMIN)
        for layer in ("claude-code-cli", "codex-cli", "direct-llm"):
            resolved = config.resolve_agent_model("catalog-demo", layer=layer)
            assert resolved and resolved == config.resolve_layer_default_model(layer), layer

    def test_the_error_names_the_agent_when_it_has_one(self, client, temp_db):
        import config
        from storage.billing import subscription_store
        rows = subscription_store.list_models(layer="codex-cli")
        assert rows
        for m in rows:
            subscription_store.update_model(m["id"], enabled=False)
        with pytest.raises(RuntimeError, match=r"^No enabled model available \(execution_path='codex-cli'\)"):
            config.resolve_layer_default_model("codex-cli")
        with pytest.raises(RuntimeError, match=r"for agent 'x' \(execution_path='codex-cli'\)"):
            config.resolve_layer_default_model("codex-cli", agent_name="x")


class TestUserRowsCarryTheDescriptor:
    def test_each_user_row_has_its_engine_descriptor(self, client, temp_db):
        r = client.get("/v1/users/me/execution-layers", headers=_member())
        assert r.status_code == 200
        rows = r.json()["layers"]
        assert rows
        for row in rows:
            caps = row["capabilities"]
            assert caps["name"] == row["name"]
            assert caps["display_name"] == row["display_name"]
            for group in ("identity", "runtime", "behaviour", "model_policy", "auth", "usage"):
                assert isinstance(caps[group], dict), (row["name"], group)
            assert "oauth_flow" in caps["auth"]


class TestTheCatalogOnlyReads:
    def test_the_get_never_syncs_the_builtin_rows(self, client, temp_db, monkeypatch):
        """The builtin sync runs at boot and on the admin pages; a page load
        never writes (a sync holds the loop about 0.3 s per new-chat page)."""
        from storage.billing import subscription_store

        def wrote(*a, **k):
            raise AssertionError("the catalog GET synced the builtin rows")

        monkeypatch.setattr(subscription_store, "sync_builtin_models", wrote)
        entry = _catalog(client)["claude-code-cli"]
        assert entry["models"] and entry["auto_model"]

    def test_a_fresh_database_stays_empty_after_a_get(self, client, temp_db):
        from storage.billing import subscription_store
        with _fresh_rows():
            assert subscription_store.list_models() == []
            entry = _catalog(client)["claude-code-cli"]
            # The registry's models still list (they come from the layer's
            # capabilities); nothing is enabled in the store, so "Auto" is empty.
            assert entry["models"] and entry["auto_model"] == ""
            assert subscription_store.list_models() == []
        _sync_builtins()
        assert subscription_store.list_models()

    def test_the_catalog_reads_off_the_loop(self, temp_db, loop_db_guard):
        import asyncio
        from api.agents.discovery import list_execution_layers
        from auth.providers import UserContext
        u = UserContext(sub=MEMBER, email="viewer@test.com", name="T", role="member")

        async def scenario():
            with loop_db_guard.active():
                return await list_execution_layers(user=u)

        data = asyncio.run(scenario())
        assert set(data) >= {"claude-code-cli", "codex-cli", "direct-llm"}
        assert data["claude-code-cli"]["auto_model"] == _declared("claude-code-cli")


import contextlib  # noqa: E402


@contextlib.contextmanager
def _fresh_rows():
    """Empty the builtin rows for the block (the autouse fixture seeded them)."""
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("DELETE FROM execution_layer_models")
        conn.commit()
    yield

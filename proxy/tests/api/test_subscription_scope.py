"""Subscription scope on create: the service-scope gates of the subscriptions
API, the delete-with-linked-triggers refusal, the ``service_bindings`` entry
of the account summary and the binding-clear cascade.

Run: cd proxy && venv/bin/pytest tests/api/test_subscription_scope.py -v
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from psycopg import errors as pg_errors

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from auth.providers import UserContext  # noqa: E402
from services.mcp import mcp_registry  # noqa: E402
from services.oauth import credential_resolver  # noqa: E402
from services.webhooks import subscription_manager  # noqa: E402
from storage.agents import agent_store  # noqa: E402
from storage.automation import trigger_store, webhook_subscription_store  # noqa: E402
from storage.identity import credential_store  # noqa: E402

AG = "shared-agent"
SOLO = "solo-agent"
MCP = "github-mcp"
M = "user-manager"
V = "user-viewer"

_WEBHOOKS = {
    "available": True,
    "provider_id": "github",
    "event_catalog": [{"key": "push", "label": "Push"}],
    "vendor_target_spec": {"kind": "free_text", "label": "Repo",
                           "account_extra_key": "team_id"},
    "registration": {"mode": "manual"},
}


def _manifest(name=MCP):
    return SimpleNamespace(
        name=name, label="GitHub", description="", config=[],
        credentials=SimpleNamespace(webhooks=dict(_WEBHOOKS), oauth={}),
    )


def _admin() -> UserContext:
    return UserContext(sub="user-admin", email="a@t.com", name="Admin", role="admin")


def _manager(agent: str = AG) -> UserContext:
    return UserContext(sub=M, email="m@t.com", name="Manager", role="creator",
                       agents=[agent], agent_roles={agent: "manager"})


def _viewer(agent: str = AG) -> UserContext:
    return UserContext(sub=V, email="v@t.com", name="Viewer", role="member",
                       agents=[agent], agent_roles={agent: "viewer"})


def _stranger() -> UserContext:
    return UserContext(sub="user-viewer2", email="v2@t.com", name="Two", role="member")


def _seed_agents():
    agent_store.create_agent(AG, "Shared", created_by="user-admin")
    agent_store.create_agent(SOLO, "Solo", created_by="user-admin",
                             collaborative=False, default_scope="user")


def _bind(owner: str, label: str = "acct", agent: str = AG):
    """A binding lends its owner's account while they manage the agent (the
    store re-checks that standing on every read), so the lender gets the row."""
    from storage import database as task_store
    task_store.add_user_agent(owner, agent, "manager", "test")
    credential_store.set_user_credentials(owner, MCP, {"k": "v"}, account_label=label)
    assert credential_store.set_service_agent_binding(
        MCP, agent, account_label=label, owner_sub=owner, set_by=owner)


def _sub(**kw) -> dict:
    base = dict(
        mcp_name=MCP, provider_id="github", account_label="acct",
        selected_events=["push"], selected_subevents={}, signing_secret="",
    )
    base.update(kw)
    return webhook_subscription_store.create_subscription(**base)


# --- create: the permission matrix ------------------------------------------------

class TestEnforceCreatePermission:
    def test_user_scope_returns_caller(self, temp_db):
        from api.events.subscriptions import _enforce_create_permission
        assert _enforce_create_permission(scope="user", agent=None, user=_viewer()) == V

    def test_service_needs_agent(self, temp_db):
        from api.events.subscriptions import _enforce_create_permission
        with pytest.raises(HTTPException) as ei:
            _enforce_create_permission(scope="service", agent=None, user=_manager())
        assert ei.value.status_code == 400

    def test_service_refuses_non_manager(self, temp_db):
        from api.events.subscriptions import _enforce_create_permission
        _seed_agents()
        with pytest.raises(HTTPException) as ei:
            _enforce_create_permission(scope="service", agent=AG, user=_viewer())
        assert ei.value.status_code == 403

    def test_service_refuses_personal_only_agent(self, temp_db):
        from api.events.subscriptions import _enforce_create_permission
        _seed_agents()
        with pytest.raises(HTTPException) as ei:
            _enforce_create_permission(scope="service", agent=SOLO, user=_manager(SOLO))
        assert ei.value.status_code == 400
        assert "agent-scope" in ei.value.detail

    def test_service_manager_ok(self, temp_db):
        from api.events.subscriptions import _enforce_create_permission
        _seed_agents()
        assert _enforce_create_permission(scope="service", agent=AG, user=_manager()) == M


class TestCreateRoute:
    def test_unique_violation_is_409(self, temp_db, monkeypatch):
        from api.events.subscriptions import CreateSubscriptionRequest, create_subscription
        _seed_agents()

        async def dup(**kw):
            raise pg_errors.UniqueViolation("duplicate key")

        monkeypatch.setattr(subscription_manager, "create_subscription", dup)
        body = CreateSubscriptionRequest(
            scope="service", agent=AG, mcp_name=MCP, account_label="acct",
            vendor_target="o/r", selected_events=["push"])
        with pytest.raises(HTTPException) as ei:
            asyncio.run(create_subscription(body, user=_manager(), x_on_behalf_of=None))
        assert ei.value.status_code == 409
        assert ei.value.detail["error"] == "exists"

    def test_target_kind_reaches_the_manager(self, temp_db, monkeypatch):
        from api.events.subscriptions import CreateSubscriptionRequest, create_subscription
        _seed_agents()
        seen: dict = {}

        async def capture(**kw):
            seen.update(kw)
            return {"id": "s1", **kw}

        monkeypatch.setattr(subscription_manager, "create_subscription", capture)
        body = CreateSubscriptionRequest(
            scope="service", agent=AG, mcp_name=MCP, account_label="acct",
            vendor_target="OtoDock", selected_events=["push"],
            vendor_target_kind="organization")
        asyncio.run(create_subscription(body, user=_manager(), x_on_behalf_of=None))
        assert seen["target_kind"] == "organization"
        body = CreateSubscriptionRequest(
            scope="service", agent=AG, mcp_name=MCP, account_label="acct",
            vendor_target="o/r", selected_events=["push"])
        asyncio.run(create_subscription(body, user=_manager(), x_on_behalf_of=None))
        assert seen["target_kind"] == ""

    def test_vendor_detach_failure_is_reported(self, temp_db, monkeypatch):
        from api.events.subscriptions import delete_subscription
        _seed_agents()
        row = _sub(scope="user", owner=M, agent=None, vendor_target="o/r", created_by=M)

        async def vendor_refuses(r):
            raise subscription_manager.VendorAPIError(
                "vendor 401", vendor_status=401, vendor_body="bad credentials")

        monkeypatch.setattr(subscription_manager, "_vendor_delete", vendor_refuses)
        out = asyncio.run(delete_subscription(row["id"], force=False, user=_manager(),
                                              x_on_behalf_of=None))
        assert out == {"deleted": True, "vendor_detached": False}
        assert webhook_subscription_store.get_subscription(row["id"]) is None


# --- the catalog route: service scope reads the bound owner's token ----------------

class TestCatalogServiceGate:
    def _call(self, ctx, **kw):
        from api.events.subscriptions import get_event_catalog
        args = dict(mcp_name=MCP, account_label="acct", scope="service", agent=AG)
        args.update(kw)
        return asyncio.run(get_event_catalog(user=ctx, **args))

    @pytest.fixture(autouse=True)
    def _manifest(self, monkeypatch):
        monkeypatch.setattr(mcp_registry, "get_manifest", _manifest)
        monkeypatch.setattr(
            subscription_manager, "_resolve_account_extra",
            lambda **kw: {"team_id": "T-SECRET"})

    def test_needs_agent(self, temp_db):
        with pytest.raises(HTTPException) as ei:
            self._call(_manager(), agent=None)
        assert ei.value.status_code == 400

    def test_no_access_is_403(self, temp_db):
        _seed_agents()
        _bind(M)
        with pytest.raises(HTTPException) as ei:
            self._call(_stranger())
        assert ei.value.status_code == 403

    def test_no_binding_is_400(self, temp_db):
        _seed_agents()
        with pytest.raises(HTTPException) as ei:
            self._call(_manager())
        assert ei.value.status_code == 400

    def test_other_label_is_400_and_names_the_bound_one(self, temp_db):
        _seed_agents()
        _bind(M)
        with pytest.raises(HTTPException) as ei:
            self._call(_manager(), account_label="other")
        assert ei.value.status_code == 400
        assert ei.value.detail["bound_account_label"] == "acct"

    def test_bound_label_reads_prefill(self, temp_db):
        _seed_agents()
        _bind(M)
        out = self._call(_viewer())
        assert out["vendor_target_prefill"] == "T-SECRET"

    def test_user_scope_untouched(self, temp_db):
        out = self._call(_viewer(), scope="user", agent=None)
        assert out["provider_id"] == "github"


# --- delete: linked triggers make it a 409 unless forced ----------------------------

class TestDeleteWithLinkedTriggers:
    def _seed(self):
        _seed_agents()
        row = _sub(scope="service", owner="", agent=AG, vendor_target="o/r",
                   created_by=M)
        trigger_store.create_trigger(
            slug="on-push", name="On push", scope="agent", agent=AG,
            created_by=M, subscription_id=row["id"], notify_enabled=True,
            notify_title="t", notify_body="b")
        return row["id"]

    def test_409_lists_the_triggers(self, temp_db):
        from api.events.subscriptions import delete_subscription
        sid = self._seed()
        with pytest.raises(HTTPException) as ei:
            asyncio.run(delete_subscription(sid, force=False, user=_admin(),
                                            x_on_behalf_of=None))
        assert ei.value.status_code == 409
        names = [t["name"] for t in ei.value.detail["triggers"]]
        assert names == ["On push"]
        assert webhook_subscription_store.get_subscription(sid) is not None

    def test_force_deletes_and_detaches(self, temp_db, monkeypatch):
        from api.events.subscriptions import delete_subscription
        sid = self._seed()

        async def no_vendor(row):
            return None

        monkeypatch.setattr(subscription_manager, "_vendor_delete", no_vendor)
        out = asyncio.run(delete_subscription(sid, force=True, user=_admin(),
                                              x_on_behalf_of=None))
        assert out == {"deleted": True, "vendor_detached": True}
        assert webhook_subscription_store.get_subscription(sid) is None
        trig = trigger_store.list_triggers(agent=AG)[0]
        assert trig["subscription_id"] is None

    def test_unlinked_deletes_without_force(self, temp_db, monkeypatch):
        from api.events.subscriptions import delete_subscription
        _seed_agents()
        row = _sub(scope="user", owner=M, agent=None, vendor_target="o/r",
                   created_by=M)

        async def no_vendor(r):
            return None

        monkeypatch.setattr(subscription_manager, "_vendor_delete", no_vendor)
        out = asyncio.run(delete_subscription(row["id"], force=False, user=_manager(),
                                              x_on_behalf_of=None))
        assert out == {"deleted": True, "vendor_detached": True}


class TestListDecoration:
    def test_created_by_name(self, temp_db):
        from api.events.subscriptions import list_subscriptions
        _seed_agents()
        _sub(scope="service", owner="", agent=AG, vendor_target="o/r", created_by=M)
        res = asyncio.run(list_subscriptions(
            scope="service", agent=AG, mcp_name=None, provider_id=None,
            account_label=None, user=_viewer()))
        assert res["subscriptions"][0]["created_by_name"] == "Manager User"


# --- the account summary names the agents an account serves ------------------------

class TestServiceBindingsSummary:
    def test_summary_fields(self, temp_db):
        from api.mcp.credentials import _service_binding_summary
        _seed_agents()
        shared = _service_binding_summary(AG, _manager())
        assert shared == {"agent_name": AG, "display_name": "Shared",
                          "can_manage": True, "agent_scope_available": True}
        solo = _service_binding_summary(SOLO, _viewer(SOLO))
        assert solo["can_manage"] is False
        assert solo["agent_scope_available"] is False

    def test_integrations_list_filters_by_owner_and_label(self, temp_db, monkeypatch):
        from api.mcp import credentials as cred_api
        from storage import database as task_store
        from storage.mcp import mcp_store
        _seed_agents()
        _bind(M, label="acct")
        _bind(V, label="theirs", agent=SOLO)
        credential_store.set_user_credentials(M, MCP, {"k": "v"}, account_label="spare")

        monkeypatch.setattr(task_store, "get_user_agents", lambda sub: [AG, SOLO])
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda agent: [_manifest()])
        monkeypatch.setattr(mcp_registry, "get_credential_schema",
                            lambda name: {"type": "per_user", "fields": [], "oauth": False})
        monkeypatch.setattr(mcp_registry, "get_manifest", _manifest)
        monkeypatch.setattr(mcp_store, "get_mcp_config_values", lambda name: {})

        out = asyncio.run(cred_api.list_my_integrations(user=_manager()))
        accounts = {a["account_label"]: a for a in out[0]["accounts"]}
        assert [b["agent_name"] for b in accounts["acct"]["service_bindings"]] == [AG]
        assert accounts["acct"]["service_bindings"][0]["can_manage"] is True
        # Another user's binding and an unbound account carry nothing.
        assert accounts["spare"]["service_bindings"] == []


class TestClearBindingCascade:
    def test_agent_rows_are_cleaned_before_the_binding_goes(self, temp_db, monkeypatch):
        from api.mcp import credentials as cred_api
        _seed_agents()
        _bind(M)
        monkeypatch.setattr(mcp_registry, "get_credential_schema",
                            lambda name: {"type": "per_user", "has_service_account": True})
        monkeypatch.setattr(mcp_registry, "get_visible_mcps_for_agent",
                            lambda agent: [_manifest()])
        seen: dict = {}

        async def fake_cleanup(**kw):
            seen.update(kw)
            assert credential_resolver.pick_account(MCP, AG) is not None
            return 1

        monkeypatch.setattr(subscription_manager, "cleanup_account_subscriptions", fake_cleanup)
        out = asyncio.run(cred_api.clear_agent_service_binding(AG, MCP, user=_manager()))
        assert out["status"] == "ok"
        assert seen == {"scope": "service", "owner": M, "mcp_name": MCP,
                        "account_label": "acct", "agent": AG}
        assert credential_store.get_service_agent_binding(MCP, AG) is None

    def test_no_binding_is_a_noop(self, temp_db, monkeypatch):
        from api.mcp import credentials as cred_api
        _seed_agents()
        called = []

        async def fake_cleanup(**kw):
            called.append(kw)

        monkeypatch.setattr(subscription_manager, "cleanup_account_subscriptions", fake_cleanup)
        asyncio.run(cred_api.clear_agent_service_binding(AG, MCP, user=_manager()))
        assert called == []

"""The app's one webhook (api/events/webhooks.py ``/v1/webhooks/<p>/app``,
services/webhooks/webhook_dispatcher.py ``dispatch_app_webhook``,
services/oauth/github_app_lifecycle.py) and the installation target kind
of a subscription (services/webhooks/subscription_manager.py): the
refusals before the body, the signature against the app secret and its
re-read after a rotation, the routing by installation, the dedup by
delivery id, every lifecycle action with its confirmation, the forwarded
path (OtoDock's hosted app) re-listing the accounts a delivery touches
instead of applying its body, subscribing on an installation with no
vendor call, the catalog's answer, and the route matrix.

Uses the connect rig (tests/auth/test_github_app_connect.py) with its
webhooks block and the connected GitHub-App account.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid

import pytest

from services.oauth import github_apps
from services.webhooks import subscription_manager, webhook_dispatcher
from storage import database as task_store
from storage.automation import trigger_store, webhook_subscription_store
from storage.identity import credential_store, oauth_app_kinds as kinds
from storage.pg import get_conn
from tests.auth.test_github_app_connect import (  # noqa: F401 (fixtures)
    MCP,
    PROVIDER,
    WEBHOOKS_BLOCK,
    _connect,
    _connect_with_services,
    _inst,
    _mark,
    hosted,
    pem,
    person,
    rig,
)

AGENT = "gh-hook-agent"
SECRET = "whsec"


@pytest.fixture
def hook(rig, monkeypatch):  # noqa: F811
    """The rig with the webhook and subscription routers, a connected
    account holding installation 10, the fan-out recorded."""
    from api.events import subscriptions as subscriptions_api, webhooks as webhooks_api
    rig.client.app.include_router(webhooks_api.router)
    rig.client.app.include_router(subscriptions_api.router)
    webhook_dispatcher.reset_caches()
    webhook_dispatcher._DEDUP_RING.clear()
    _connect(rig)
    fired: list[tuple] = []

    async def fan_out(*, triggers, body, event, trigger_source, event_id=""):
        fired.append((event.event_type, [t.get("id") for t in triggers], event.target.get("id")))
        return (len(triggers), [])
    monkeypatch.setattr(webhook_dispatcher, "_fan_out_fire", fan_out)
    rig.fired = fired
    yield rig
    with get_conn() as conn:
        conn.execute("DELETE FROM webhook_subscriptions WHERE mcp_name=%s", (MCP,))
        conn.execute("DELETE FROM service_agent_bindings WHERE mcp_name=%s", (MCP,))
        conn.commit()


def _deliver(hook, event: str, body: dict, *, delivery_id: str | None = None,
             secret: str = SECRET, path: str = f"/v1/webhooks/{PROVIDER}/app"):
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hook.client.post(path, content=raw, headers={
        "content-type": "application/json", "x-hub-signature-256": sig,
        "x-github-event": event, "x-github-delivery": delivery_id or uuid.uuid4().hex,
    })


def _issue_body(installation_id: int = 10) -> dict:
    return {"action": "opened", "installation": {"id": installation_id},
            "repository": {"full_name": "alice/repo"}, "sender": {"id": 1, "login": "alice"},
            "issue": {"number": 7, "title": "t"}}


def _subscribe(hook, target: str = "10", *, scope: str = "user", agent: str | None = None,
               kind: str = kinds.INSTALLATION_TARGET_KIND, events=("issues",), label="alice"):
    body = {"scope": scope, "mcp_name": MCP, "account_label": label, "vendor_target": target,
            "selected_events": list(events), "vendor_target_kind": kind}
    if agent:
        body["agent"] = agent
    return hook.client.post("/v1/subscriptions", json=body)


def _trigger_for(monkeypatch, subscription_id: str):
    monkeypatch.setattr(trigger_store, "list_triggers",
                        lambda **kw: [{"id": "t1", "subscription_id": subscription_id, "event_filter": "{}"}]
                        if kw.get("subscription_id") == subscription_id else [])


def _meta(hook):
    return credential_store.get_account_meta(hook.sub, MCP, "alice")


def _bind_hosted_agent(env, monkeypatch, *, subscribe=True) -> str:
    """The hosted account bound as the agent's identity (the account
    itself: hosted sign-in has no installation identity), and with
    ``subscribe`` the agent's subscription on installation 10."""
    env.holder["role"] = "admin"
    task_store.add_user_agent(env.sub, AGENT, "manager", "test")
    from services.mcp import mcp_registry
    manifest = mcp_registry.get_manifest(MCP)
    monkeypatch.setattr(mcp_registry, "get_visible_mcps_for_agent", lambda agent: [manifest])
    resp = env.client.put(f"/v1/agents/{AGENT}/mcps/{MCP}/service-binding",
                          json={"account_label": "alice", "installation_id": ""})
    assert resp.status_code == 200, resp.text
    if not subscribe:
        return ""
    resp = _subscribe(env, "10", scope="service", agent=AGENT)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


class TestTheRoute:
    def test_refused_before_the_body_without_a_secret_or_another_kind(self, hook):
        credential_store.set_infra_credentials(hook.bundle, {"GITHUB_APP_WEBHOOK_SECRET": ""})
        webhook_dispatcher.reset_caches()
        resp = _deliver(hook, "issues", _issue_body())
        assert resp.status_code == 404 and "secret" in resp.json()["error"]
        credential_store.set_infra_credentials(hook.bundle, {"GITHUB_APP_WEBHOOK_SECRET": SECRET,
                                                             "GITHUB_APP_KIND": kinds.KIND_OAUTH_APP})
        webhook_dispatcher.reset_caches()
        assert _deliver(hook, "issues", _issue_body()).status_code == 404

    def test_a_bad_signature_is_refused_and_a_rotated_secret_is_re_read(self, hook):
        assert _deliver(hook, "issues", _issue_body(), secret="wrong").status_code == 401
        credential_store.set_infra_credentials(hook.bundle, {"GITHUB_APP_WEBHOOK_SECRET": "rotated"})
        # Within the five-second window the refused delivery is refused
        # again; past it the secret is read again after the refusal.
        assert _deliver(hook, "issues", _issue_body(), secret="rotated").status_code == 401
        value, at = webhook_dispatcher._app_secrets[PROVIDER]
        webhook_dispatcher._app_secrets[PROVIDER] = (value, at - 10)
        resp = _deliver(hook, "issues", _issue_body(), secret="rotated")
        assert resp.status_code == 200, resp.text

    def test_routing_by_installation_and_the_dedup(self, hook, monkeypatch):
        resp = _subscribe(hook)
        assert resp.status_code == 200, resp.text
        sid = resp.json()["id"]
        _trigger_for(monkeypatch, sid)
        resp = _deliver(hook, "issues", _issue_body(), delivery_id="d-1")
        assert resp.status_code == 200 and resp.json()["status"] == "ok" and resp.json()["fired"] == 1
        assert hook.fired == [("issues", ["t1"], "alice/repo")]
        assert _deliver(hook, "issues", _issue_body(), delivery_id="d-1").json()["status"] == "duplicate"
        resp = _deliver(hook, "issues", _issue_body(99), delivery_id="d-2")
        assert resp.json() == {"status": "ignored", "reason": "unknown_installation", "fired": 0}
        assert len(hook.fired) == 1

    def test_the_route_matrix(self, hook):
        assert hook.client.post("/v1/webhooks/relay/app", content=b"{}").status_code == 404
        assert hook.client.get(f"/v1/webhooks/{PROVIDER}/app").status_code == 404
        assert _deliver(hook, "issues", _issue_body(99)).json()["reason"] == "unknown_installation"


class TestLifecycle:
    def test_created_attaches_to_the_installer_after_confirmation(self, hook):
        body = {"action": "created", "installation": _inst(20), "sender": {"id": 1, "login": "alice"},
                "repositories": [{"full_name": "alice/new"}]}
        resp = _deliver(hook, "installation", body, delivery_id="c-1")
        assert resp.status_code == 200
        assert [i["id"] for i in _meta(hook)["installations"]] == [10]   # GitHub did not confirm (404)
        hook.fake.routes[("GET", "/app/installations/20")] = (200, {"id": 20, "suspended_at": None})
        _deliver(hook, "installation", body, delivery_id="c-2")
        rows = _meta(hook)["installations"]
        assert [i["id"] for i in rows] == [10, 20]
        assert rows[1]["admin"] is True and rows[1]["repositories"] == ["alice/new"]
        # Someone else's install never lands on this account.
        other = {**body, "installation": _inst(21, login="bob", account_id=2), "sender": {"id": 2, "login": "bob"}}
        hook.fake.routes[("GET", "/app/installations/21")] = (200, {"id": 21, "suspended_at": None})
        _deliver(hook, "installation", other, delivery_id="c-3")
        assert [i["id"] for i in _meta(hook)["installations"]] == [10, 20]

    def test_an_organization_install_is_owned_only_by_a_listing(self, hook):
        # A repository admin who is no owner can install there: the event
        # never grants the standing; the person's own listing does.
        org = _inst(22, login="octo", account_id=9, target="Organization")
        hook.fake.routes[("GET", "/app/installations/22")] = (200, {"id": 22, "suspended_at": None})
        _deliver(hook, "installation", {"action": "created", "installation": org, "sender": {"id": 1, "login": "alice"},
                                        "repositories": [{"full_name": "octo/a"}]}, delivery_id="c-org")
        row = next(i for i in _meta(hook)["installations"] if i["id"] == 22)
        assert row["admin"] is False and row["repositories"] == ["octo/a"]
        assert kinds.installation_standing(_meta(hook), "22") == kinds.INSTALLATION_NOT_OWNED
        hook.fake.installations = [_inst(10), org]
        hook.fake.routes[("GET", "/user/memberships/orgs/octo")] = (200, {"role": "admin", "state": "active"})
        hook.client.post(f"/v1/oauth/{PROVIDER}/app/installations/refresh", json={"mcp_name": MCP, "account_label": "alice"})
        assert next(i for i in _meta(hook)["installations"] if i["id"] == 22)["admin"] is True

    def test_the_lifecycle_runs_off_the_event_loop(self, hook, monkeypatch):
        from services.oauth import github_app_lifecycle
        seen: list[bool] = []

        def apply(*a, **k):
            seen.append(github_apps._on_loop())
            return github_app_lifecycle.IGNORED
        monkeypatch.setattr(github_app_lifecycle, "apply", apply)
        assert _deliver(hook, "installation", {"action": "created", "installation": _inst(10)}).status_code == 200
        assert seen == [False]

    def test_suspend_and_unsuspend_are_confirmed(self, hook):
        hook.fake.routes[("GET", "/app/installations/10")] = (200, {"id": 10, "suspended_at": "2026-10-15T00:00:00Z"})
        _deliver(hook, "installation", {"action": "suspend", "installation": {**_inst(10), "suspended_at": "2026-10-15T00:00:00Z"},
                                        "sender": {"id": 1}}, delivery_id="s-1")
        assert _meta(hook)["installations"][0]["suspended_at"] == "2026-10-15T00:00:00Z"
        # A replayed suspend after GitHub unsuspended changes nothing.
        hook.fake.routes[("GET", "/app/installations/10")] = (200, {"id": 10, "suspended_at": None})
        _deliver(hook, "installation", {"action": "unsuspend", "installation": _inst(10), "sender": {"id": 1}}, delivery_id="s-2")
        assert _meta(hook)["installations"][0]["suspended_at"] is None
        _deliver(hook, "installation", {"action": "suspend", "installation": {**_inst(10), "suspended_at": "2026-10-15T00:00:00Z"},
                                        "sender": {"id": 1}}, delivery_id="s-3")
        assert _meta(hook)["installations"][0]["suspended_at"] is None
        # A confirmed suspend without a stamp in the payload is stamped now.
        hook.fake.routes[("GET", "/app/installations/10")] = (200, {"id": 10, "suspended_at": "2026-10-16T00:00:00Z"})
        _deliver(hook, "installation", {"action": "suspend", "installation": _inst(10), "sender": {"id": 1}}, delivery_id="s-4")
        assert _meta(hook)["installations"][0]["suspended_at"]

    def test_repositories_and_the_target_rename(self, hook):
        _deliver(hook, "installation_repositories", {
            "action": "added", "installation": _inst(10), "repository_selection": "selected",
            "repositories_added": [{"full_name": "alice/a"}, {"full_name": "alice/b"}], "repositories_removed": [],
            "sender": {"id": 1}}, delivery_id="r-1")
        row = _meta(hook)["installations"][0]
        assert row["repositories"] == ["alice/a", "alice/b"] and row["repository_selection"] == "selected"
        _deliver(hook, "installation_repositories", {
            "action": "removed", "installation": _inst(10), "repository_selection": "selected",
            "repositories_added": [], "repositories_removed": [{"full_name": "alice/a"}], "sender": {"id": 1}},
            delivery_id="r-2")
        assert _meta(hook)["installations"][0]["repositories"] == ["alice/b"]
        _deliver(hook, "installation_target", {"action": "renamed", "installation": {"id": 10},
                                               "account": {"login": "alice-renamed", "id": 1},
                                               "changes": {"login": {"from": "alice"}}}, delivery_id="t-1")
        assert _meta(hook)["installations"][0]["account"] == "alice-renamed"

    def test_deleted_removes_everything_that_named_it(self, hook, monkeypatch):
        hook.holder["role"] = "admin"
        task_store.add_user_agent(hook.sub, AGENT, "manager", "test")
        from services.mcp import mcp_registry
        manifest = mcp_registry.get_manifest(MCP)
        monkeypatch.setattr(mcp_registry, "get_visible_mcps_for_agent", lambda agent: [manifest])
        hook.fake.routes[("POST", "/app/installations/10/access_tokens")] = (201, {
            "token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"})
        assert hook.client.put(f"/v1/agents/{AGENT}/mcps/{MCP}/service-binding",
                               json={"account_label": "alice", "installation_id": "10"}).status_code == 200
        assert _subscribe(hook).status_code == 200
        assert _subscribe(hook, scope="service", agent=AGENT).status_code == 200
        path = github_apps.installation_file(PROVIDER, 10)
        assert path.exists()
        # Not confirmed (GitHub still has it): nothing changes.
        hook.fake.routes[("GET", "/app/installations/10")] = (200, {"id": 10, "suspended_at": None})
        _deliver(hook, "installation", {"action": "deleted", "installation": _inst(10), "sender": {"id": 1}}, delivery_id="x-1")
        assert [i["id"] for i in _meta(hook)["installations"]] == [10]
        del hook.fake.routes[("GET", "/app/installations/10")]      # the fake's default: 404
        _deliver(hook, "installation", {"action": "deleted", "installation": _inst(10), "sender": {"id": 1}}, delivery_id="x-2")
        assert _meta(hook)["installations"] == []
        assert credential_store.get_service_agent_binding(MCP, AGENT) is None
        assert webhook_subscription_store.list_subscriptions(mcp_name=MCP) == []
        assert not path.exists()

    def test_a_revoked_authorization_marks_the_token_file(self, hook):
        _deliver(hook, "github_app_authorization", {"action": "revoked", "sender": {"id": 1, "login": "alice"}}, delivery_id="a-1")
        from services.oauth import oauth_account_store
        raw = oauth_account_store.read_account_token(hook.token_dir(), "alice")
        assert raw["extra"]["refresh_failed"] == "revoked"

    def test_the_admin_apps_revoke_never_marks_an_account_of_otodocks_app(self, hook):
        """The admin's own app speaks for its own accounts: a hosted account
        of the same GitHub person (its meta, or its file) is not touched."""
        from services.oauth import oauth_account_store
        meta = _meta(hook)
        credential_store.set_account_meta(hook.sub, MCP, "alice", {**meta, "hosted": True})
        _deliver(hook, "github_app_authorization", {"action": "revoked", "sender": {"id": 1}}, delivery_id="h-1")
        assert "refresh_failed" not in oauth_account_store.read_account_token(hook.token_dir(), "alice")["extra"]
        credential_store.set_account_meta(hook.sub, MCP, "alice", meta)
        path = oauth_account_store.account_token_path(hook.token_dir(), "alice")
        raw = oauth_account_store.read_account_token(hook.token_dir(), "alice")
        oauth_account_store.write_token_payload(path, {**raw, "extra": {**raw["extra"], "via_relay": True}})
        _deliver(hook, "github_app_authorization", {"action": "revoked", "sender": {"id": 1}}, delivery_id="h-2")
        assert "refresh_failed" not in oauth_account_store.read_account_token(hook.token_dir(), "alice")["extra"]

    def test_the_forwarded_path_relists_before_its_early_return(self, hook, monkeypatch):
        from services.billing import relay_client
        monkeypatch.setattr(credential_store, "get_infra_credentials", _with_forward_secret(credential_store.get_infra_credentials))
        webhook_dispatcher.reset_caches()
        hook.fake.installations = [_inst(10, selection="selected")]
        body = json.dumps({"action": "added", "installation": _inst(10), "repository_selection": "selected",
                           "repositories_added": [{"full_name": "alice/fwd"}], "repositories_removed": [],
                           "sender": {"id": 1}}).encode()
        ts = str(int(time.time()))
        sig = "v0=" + hmac.new(b"fwd-secret", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
        resp = hook.client.post(f"/v1/webhooks/relay/{PROVIDER}", content=body, headers={
            "content-type": "application/json", "x-otodock-event-signature": sig,
            "x-otodock-event-timestamp": ts, "x-otodock-event-provider": PROVIDER,
            "x-github-event": "installation_repositories", "x-github-delivery": "f-1"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["reason"] == "unknown_installation"            # no relay row
        # The listing, never the body.
        assert _meta(hook)["installations"][0]["repositories"] == ["alice/repo"]
        assert relay_client.EVENTS_FORWARD_SECRET_SLUG


def _with_forward_secret(real):
    from services.billing import relay_client

    def read(slug):
        if slug == relay_client.EVENTS_FORWARD_SECRET_SLUG:
            return {relay_client.EVENTS_FORWARD_SECRET_KEY: "fwd-secret"}
        return real(slug)
    return read


@pytest.fixture
def fwd(hosted, monkeypatch):  # noqa: F811
    """A hosted account holding installation 10 behind OtoDock's relay:
    its connect registered the events endpoint (the forward secret is
    held), a subscription on an installation is relay-delivered, the
    fan-out recorded."""
    from api.events import subscriptions as subscriptions_api, webhooks as webhooks_api
    from services.billing import relay_client
    hosted.client.app.include_router(webhooks_api.router)
    hosted.client.app.include_router(subscriptions_api.router)
    monkeypatch.setattr(relay_client, "is_available", lambda: True)
    webhook_dispatcher.reset_caches()
    webhook_dispatcher._DEDUP_RING.clear()
    _connect(hosted)
    fired: list[tuple] = []

    async def fan_out(*, triggers, body, event, trigger_source, event_id=""):
        fired.append((event.event_type, [t.get("id") for t in triggers], event.target.get("id")))
        return (len(triggers), [])
    monkeypatch.setattr(webhook_dispatcher, "_fan_out_fire", fan_out)
    hosted.fired = fired
    yield hosted
    with get_conn() as conn:
        conn.execute("DELETE FROM webhook_subscriptions WHERE mcp_name=%s", (MCP,))
        conn.execute("DELETE FROM service_agent_bindings WHERE mcp_name=%s", (MCP,))
        conn.commit()


def _forward(hook, event: str, body: dict, *, delivery_id: str | None = None):
    """A delivery as the relay forwards it: GitHub's headers beside the
    verbatim body, signed with this install's forward secret."""
    from services.billing import relay_client
    held = credential_store.get_infra_credentials(relay_client.EVENTS_FORWARD_SECRET_SLUG) or {}
    secret = str(held.get(relay_client.EVENTS_FORWARD_SECRET_KEY) or "")
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    return hook.client.post(f"/v1/webhooks/relay/{PROVIDER}", content=raw, headers={
        "content-type": "application/json", "x-otodock-event-signature": sig,
        "x-otodock-event-timestamp": ts, "x-otodock-event-provider": PROVIDER,
        "x-github-event": event, "x-github-delivery": delivery_id or uuid.uuid4().hex})


def _listings(hook) -> int:
    return len([r for r in hook.fake.requests if r.url.path == "/user/installations"])


class TestForwarded:
    def test_a_forwarded_created_relists_the_senders_accounts(self, fwd):
        fwd.fake.installations = [_inst(10), _inst(20)]
        # The body names 21; the person's own listing shows 20.
        body = {"action": "created", "installation": _inst(21), "sender": {"id": 1, "login": "alice"},
                "repositories": [{"full_name": "alice/claimed"}]}
        resp = _forward(fwd, "installation", body, delivery_id="fc-1")
        assert resp.status_code == 200, resp.text
        assert [i["id"] for i in _meta(fwd)["installations"]] == [10, 20]
        assert not any(r.url.path.startswith("/app/installations") for r in fwd.fake.requests)
        assert _meta(fwd)["hosted"] is True
        assert fwd.relay.bodies("/v1/oauth/app-relist") == []        # GitHub asked, never the relay

    def test_a_forwarded_deleted_the_listing_still_shows_changes_nothing(self, fwd):
        resp = _subscribe(fwd)
        assert resp.status_code == 200 and resp.json()["delivery_mode"] == "relay", resp.text
        before = _listings(fwd)
        _forward(fwd, "installation", {"action": "deleted", "installation": _inst(10), "sender": {"id": 1}},
                 delivery_id="fd-1")
        assert _listings(fwd) == before + 1
        assert [i["id"] for i in _meta(fwd)["installations"]] == [10]
        assert [r["vendor_target"] for r in webhook_subscription_store.list_subscriptions(mcp_name=MCP)] == ["10"]

    def test_a_forwarded_deleted_the_listing_dropped_detaches(self, fwd):
        assert _subscribe(fwd).status_code == 200
        # A second account still lists 10 but cannot list now (no token).
        credential_store.set_user_credentials(fwd.sub, MCP, {"GITHUB_EMAIL": "x"}, account_label="stale")
        credential_store.set_account_meta(fwd.sub, MCP, "stale", {**_meta(fwd), "login": "stale"})
        fwd.fake.installations = []
        _forward(fwd, "installation", {"action": "deleted", "installation": _inst(10), "sender": {"id": 1}},
                 delivery_id="fd-2")
        assert _meta(fwd)["installations"] == []
        assert credential_store.get_account_meta(fwd.sub, MCP, "stale")["installations"] == []
        assert webhook_subscription_store.list_subscriptions(mcp_name=MCP) == []

    def test_a_forwarded_suspend_is_not_applied_from_the_body(self, fwd):
        stamp = "2026-10-15T00:00:00Z"
        _forward(fwd, "installation", {"action": "suspend", "installation": {**_inst(10), "suspended_at": stamp},
                                       "sender": {"id": 1}}, delivery_id="fs-1")
        assert _meta(fwd)["installations"][0]["suspended_at"] is None      # GitHub lists it live
        fwd.fake.installations = [{**_inst(10), "suspended_at": stamp}]
        _forward(fwd, "installation", {"action": "unsuspend", "installation": _inst(10), "sender": {"id": 1}},
                 delivery_id="fs-2")
        assert _meta(fwd)["installations"][0]["suspended_at"] == stamp     # and now suspended
        assert kinds.installation_standing(_meta(fwd), "10") == kinds.INSTALLATION_SUSPENDED

    def test_a_forwarded_revoked_marks_only_a_refused_token(self, fwd):
        from services.oauth import oauth_account_store
        revoked = {"action": "revoked", "sender": {"id": 1, "login": "alice"}}
        _forward(fwd, "github_app_authorization", revoked, delivery_id="fr-1")
        raw = oauth_account_store.read_account_token(fwd.token_dir(), "alice")
        assert "refresh_failed" not in raw["extra"]                 # GitHub still takes the token
        # The relay dropped the person at the revoke: the account re-lists there.
        assert [b["access_token"] for b in fwd.relay.bodies("/v1/oauth/app-relist")] == ["ghu_1"]
        fwd.fake.user_status = 401
        _forward(fwd, "github_app_authorization", revoked, delivery_id="fr-2")
        raw = oauth_account_store.read_account_token(fwd.token_dir(), "alice")
        assert raw["extra"]["refresh_failed"] == "revoked"
        assert len(fwd.relay.bodies("/v1/oauth/app-relist")) == 1

    def test_a_forwarded_revoke_never_sends_an_admin_app_token_to_the_relay(self, fwd):
        from tests.auth.test_github_app_connect import _from_the_admins_own_app
        _connect(fwd, label="old")
        _from_the_admins_own_app(fwd, "old")
        _forward(fwd, "github_app_authorization", {"action": "revoked", "sender": {"id": 1}}, delivery_id="fr-4")
        # GitHub accepts both tokens: the hosted one re-binds at the relay, the other stays here.
        assert [b["access_token"] for b in fwd.relay.bodies("/v1/oauth/app-relist")] == ["ghu_1"]
        assert "ghu_adminapp" not in json.dumps(fwd.relay.calls)

    def test_a_forwarded_revoke_never_marks_an_account_of_the_admins_app(self, fwd):
        from services.oauth import oauth_account_store
        from tests.auth.test_github_app_connect import _from_the_admins_own_app
        _connect(fwd, label="old")
        _from_the_admins_own_app(fwd, "old")
        fwd.fake.user_status = 401
        _forward(fwd, "github_app_authorization", {"action": "revoked", "sender": {"id": 1}}, delivery_id="fr-5")
        assert oauth_account_store.read_account_token(fwd.token_dir(), "alice")["extra"]["refresh_failed"] == "revoked"
        assert "refresh_failed" not in oauth_account_store.read_account_token(fwd.token_dir(), "old")["extra"]

    def test_a_forwarded_revoke_leaves_an_expired_token_to_the_refresh(self, fwd):
        from services.oauth import oauth_account_store
        path = oauth_account_store.account_token_path(fwd.token_dir(), "alice")
        raw = oauth_account_store.read_account_token(fwd.token_dir(), "alice")
        oauth_account_store.write_token_payload(path, {**raw, "expires_at": "2020-01-01T00:00:00Z"})
        fwd.fake.user_status = 401
        before = _listings(fwd)
        _forward(fwd, "github_app_authorization", {"action": "revoked", "sender": {"id": 1}}, delivery_id="fr-3")
        assert "refresh_failed" not in oauth_account_store.read_account_token(fwd.token_dir(), "alice")["extra"]
        assert _listings(fwd) == before

    def test_a_failed_forward_is_processed_again_on_the_relays_retry(self, fwd, monkeypatch):
        real = webhook_dispatcher._dispatch_installation_event
        calls: list[int] = []

        async def once_failing(**kw):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("store down")
            return await real(**kw)
        monkeypatch.setattr(webhook_dispatcher, "_dispatch_installation_event", once_failing)
        body = {"action": "added", "installation": _inst(10), "repository_selection": "all",
                "repositories_added": [], "repositories_removed": [], "sender": {"id": 1}}
        assert _forward(fwd, "installation_repositories", body, delivery_id="retry-1").status_code == 500
        again = _forward(fwd, "installation_repositories", body, delivery_id="retry-1")
        assert again.status_code == 200 and again.json()["status"] != "duplicate"
        assert len(calls) == 2

    def test_a_forwarded_lifecycle_that_fails_in_the_window_is_retried(self, fwd, monkeypatch):
        from services.oauth import github_app_lifecycle
        real = github_app_lifecycle.apply
        calls: list[int] = []

        def once_failing(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("store down")
            return real(*a, **k)
        monkeypatch.setattr(github_app_lifecycle, "apply", once_failing)
        fwd.fake.installations = [_inst(10, selection="selected")]
        body = {"action": "added", "installation": _inst(10), "repository_selection": "selected",
                "repositories_added": [{"full_name": "alice/new"}], "repositories_removed": [],
                "sender": {"id": 1}}
        first = _forward(fwd, "installation_repositories", body, delivery_id="lf-1")
        assert first.status_code == 500
        assert _meta(fwd)["installations"][0]["repositories"] == []        # the connect's listing, unchanged
        again = _forward(fwd, "installation_repositories", body, delivery_id="lf-1")
        assert again.status_code == 200 and again.json()["status"] != "duplicate"
        assert len(calls) == 2
        assert _meta(fwd)["installations"][0]["repositories"] == ["alice/repo"]

    def test_a_slow_forwarded_relist_finishes_behind_the_answer_in_order(self, monkeypatch):
        from core import background
        from services.oauth import github_app_lifecycle
        monkeypatch.setattr(webhook_dispatcher, "_FORWARDED_ANSWER_S", 0.05)
        monkeypatch.setattr(webhook_subscription_store, "list_subscriptions", lambda **kw: [])
        order: list[tuple[str, int]] = []

        def apply(provider_id, manifest, event_type, body, *, installation_id, confirm):
            assert confirm is False
            order.append(("start", body["n"]))
            time.sleep(0.3 if body["n"] == 1 else 0)
            order.append(("end", body["n"]))
            return github_app_lifecycle.APPLIED
        monkeypatch.setattr(github_app_lifecycle, "apply", apply)

        async def two_deliveries():
            answers = []
            for n in (1, 2):
                answers.append(await webhook_dispatcher._dispatch_installation_event(
                    provider_id=PROVIDER, provider=None, manifest=None, webhooks_block=WEBHOOKS_BLOCK,
                    body={"n": n, "installation": {"id": 10}},
                    headers_lc={"x-github-event": "installation_repositories"},
                    confirm=False, delivery_mode="relay"))
                if n == 1:
                    assert ("end", 1) not in order          # answered while it re-lists
            await asyncio.gather(*background.running("github-app"))
            return answers
        answers = asyncio.run(two_deliveries())
        assert [a[0] for a in answers] == [200, 200]
        assert order == [("start", 1), ("end", 1), ("start", 2), ("end", 2)]

    def test_a_forwarded_app_delivery_repeat_answers_duplicate(self, fwd, monkeypatch):
        sid = _subscribe(fwd).json()["id"]
        _trigger_for(monkeypatch, sid)
        first = _forward(fwd, "issues", _issue_body(), delivery_id="dup-1")
        assert first.json()["status"] == "ok" and first.json()["fired"] == 1
        again = _forward(fwd, "issues", _issue_body(), delivery_id="dup-1")
        assert again.status_code == 200 and again.json() == {"status": "duplicate", "fired": 0}
        before = _listings(fwd)
        body = {"action": "added", "installation": _inst(10), "repository_selection": "all",
                "repositories_added": [], "repositories_removed": [], "sender": {"id": 1}}
        _forward(fwd, "installation_repositories", body, delivery_id="dup-2")
        _forward(fwd, "installation_repositories", body, delivery_id="dup-2")
        assert _listings(fwd) == before + 1
        assert len(fwd.fired) == 1


class TestSubscribing:
    def test_an_installation_target_registers_nothing_and_is_active(self, hook):
        resp = _subscribe(hook, events=("issues", "push"))
        assert resp.status_code == 200, resp.text
        row = resp.json()
        assert row["status"] == "active" and row["target_kind"] == kinds.INSTALLATION_TARGET_KIND
        assert row["vendor_target"] == "10" and row["delivery_mode"] == "vendor"
        assert webhook_subscription_store.get_signing_secret(row["id"]) == ""
        assert not any(r.url.path.endswith("/hooks") for r in hook.fake.requests)
        deleted = hook.client.delete(f"/v1/subscriptions/{row['id']}")
        assert deleted.status_code == 200 and deleted.json()["deleted"] is True
        assert not any(r.method == "DELETE" for r in hook.fake.requests)

    def test_only_an_owned_installation(self, hook):
        hook.fake.installations = [_inst(10), _inst(11, login="octo", account_id=9, target="Organization")]
        hook.fake.routes[("GET", "/user/memberships/orgs/octo")] = (200, {"role": "member", "state": "active"})
        hook.client.post(f"/v1/oauth/{PROVIDER}/app/installations/refresh", json={"mcp_name": MCP, "account_label": "alice"})
        resp = _subscribe(hook, "11")
        assert resp.status_code == 400 and "not one you own on GitHub" in resp.text
        assert "pick one of the account's installations: 10 (alice, all repositories)" in resp.text
        resp = _subscribe(hook, "12")
        assert resp.status_code == 400 and "not installed there any more" in resp.text
        # An older client's repository target on a GitHub-App account.
        resp = _subscribe(hook, "alice/repo", kind="")
        assert resp.status_code == 400 and "pick one of the account's installations: 10" in resp.text

    def test_a_subscriber_who_lost_the_standing_stops_receiving(self, hook, monkeypatch):
        org = _inst(11, login="octo", account_id=9, target="Organization")
        hook.fake.installations = [_inst(10), org]
        hook.fake.routes[("GET", "/user/memberships/orgs/octo")] = (200, {"role": "admin", "state": "active"})
        hook.client.post(f"/v1/oauth/{PROVIDER}/app/installations/refresh", json={"mcp_name": MCP, "account_label": "alice"})
        row = _subscribe(hook, "11").json()
        _trigger_for(monkeypatch, row["id"])
        resp = _deliver(hook, "issues", _issue_body(11))
        assert resp.json()["fired"] == 1 and len(hook.fired) == 1
        hook.fake.routes[("GET", "/user/memberships/orgs/octo")] = (200, {"role": "member", "state": "active"})
        github_apps.refresh_installations(hook.sub, MCP, "alice")     # the card's Refresh, past its throttle
        resp = _deliver(hook, "issues", _issue_body(11))
        assert resp.json()["fired"] == 0 and len(hook.fired) == 1
        assert resp.json()["results"][0]["skipped"] == kinds.INSTALLATION_NOT_OWNED

    def test_a_service_row_stops_with_its_binding(self, hook, monkeypatch):
        hook.holder["role"] = "admin"
        task_store.add_user_agent(hook.sub, AGENT, "manager", "test")
        from services.mcp import mcp_registry
        manifest = mcp_registry.get_manifest(MCP)
        monkeypatch.setattr(mcp_registry, "get_visible_mcps_for_agent", lambda agent: [manifest])
        hook.fake.routes[("POST", "/app/installations/10/access_tokens")] = (201, {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"})
        assert hook.client.put(f"/v1/agents/{AGENT}/mcps/{MCP}/service-binding",
                               json={"account_label": "alice", "installation_id": "10"}).status_code == 200
        row = _subscribe(hook, "10", scope="service", agent=AGENT).json()
        _trigger_for(monkeypatch, row["id"])
        assert _deliver(hook, "issues", _issue_body(10)).json()["fired"] == 1
        credential_store.remove_service_agent_binding(MCP, AGENT)
        resp = _deliver(hook, "issues", _issue_body(10))
        assert resp.json()["fired"] == 0 and resp.json()["results"][0]["skipped"] == "not the agent's bound installation"

    def test_a_service_subscription_is_tied_to_the_binding(self, hook, monkeypatch):
        hook.holder["role"] = "admin"
        task_store.add_user_agent(hook.sub, AGENT, "manager", "test")
        from services.mcp import mcp_registry
        manifest = mcp_registry.get_manifest(MCP)
        monkeypatch.setattr(mcp_registry, "get_visible_mcps_for_agent", lambda agent: [manifest])
        hook.fake.installations = [_inst(10), _inst(12)]
        hook.client.post(f"/v1/oauth/{PROVIDER}/app/installations/refresh", json={"mcp_name": MCP, "account_label": "alice"})
        hook.fake.routes[("POST", "/app/installations/10/access_tokens")] = (201, {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"})
        hook.fake.routes[("POST", "/app/installations/12/access_tokens")] = (201, {"token": "ghs_y", "expires_at": "2099-01-01T00:00:00Z"})
        assert hook.client.put(f"/v1/agents/{AGENT}/mcps/{MCP}/service-binding",
                               json={"account_label": "alice", "installation_id": "10"}).status_code == 200
        resp = _subscribe(hook, "12", scope="service", agent=AGENT)
        assert resp.status_code == 400 and resp.json()["detail"]["bound_installation_id"] == "10"
        assert _subscribe(hook, "10", scope="service", agent=AGENT).status_code == 200
        # A rebind to another installation drops the old installation's rows.
        assert hook.client.put(f"/v1/agents/{AGENT}/mcps/{MCP}/service-binding",
                               json={"account_label": "alice", "installation_id": "12"}).status_code == 200
        assert webhook_subscription_store.list_subscriptions(mcp_name=MCP, scope="service") == []

    def test_an_older_client_without_a_kind_means_the_installation(self, hook):
        resp = _subscribe(hook, "10", kind="")
        assert resp.status_code == 200 and resp.json()["target_kind"] == kinds.INSTALLATION_TARGET_KIND

    def test_the_catalog_for_a_github_app_account(self, hook):
        resp = hook.client.get(f"/v1/mcps/{MCP}/webhook-event-catalog",
                               params={"account_label": "alice", "scope": "user"})
        assert resp.status_code == 200, resp.text
        spec = resp.json()["vendor_target_spec"]
        assert spec["kind"] == "static_list" and "target_kinds" not in spec
        assert spec["static_options"] == [{"value": "10", "label": "alice (all repositories)"}]
        assert spec["help_text"] == "One subscription covers every repository the installation reaches, now and later."
        assert resp.json()["registration"]["mode"] == kinds.REGISTRATION_MODE_APP
        assert resp.json()["target_kind"] == kinds.INSTALLATION_TARGET_KIND
        # A cut repository list says so; no owned installation says what to do.
        meta = _meta(hook)
        big = {**_inst(13, login="big", selection="selected"), "account": "big", "account_id": 1, "admin": True,
               "repositories": [f"big/r{i}" for i in range(200)], "repositories_truncated": True}
        credential_store.set_account_meta(hook.sub, MCP, "alice", {**meta, "installations": [meta["installations"][0], big]})
        spec = hook.client.get(f"/v1/mcps/{MCP}/webhook-event-catalog",
                               params={"account_label": "alice", "scope": "user"}).json()["vendor_target_spec"]
        assert spec["static_options"][1] == {"value": "13", "label": "big (200+ repositories)"}
        credential_store.set_account_meta(hook.sub, MCP, "alice", {**meta, "installations": []})
        spec = hook.client.get(f"/v1/mcps/{MCP}/webhook-event-catalog",
                               params={"account_label": "alice", "scope": "user"}).json()["vendor_target_spec"]
        assert spec["static_options"] == []
        assert spec["help_text"] == "Install the app on GitHub first. You can subscribe only to an installation you own."

    def test_the_catalog_answers_the_recorded_scopes(self, hook):
        def catalog(label):
            return hook.client.get(f"/v1/mcps/{MCP}/webhook-event-catalog",
                                   params={"account_label": label, "scope": "user"}).json()
        # A GitHub-App account: the app's permissions decide, nothing to say.
        body = catalog("alice")
        assert body["granted_scopes"] is None and body["scope_services"] == {"repo": "Repositories"}
        # The same label reconnected through the OAuth App: the scopes it recorded.
        _mark(hook, kinds.KIND_OAUTH_APP)
        _connect_with_services(hook)
        body = catalog("alice")
        assert body["vendor_target_spec"]["kind"] == "free_text" and body["granted_scopes"] == ["repo"]
        assert body["target_kind"] == ""
        # A token records none: not known.
        resp = hook.client.post(f"/v1/oauth/{PROVIDER}/pat/save", json={
            "mcp_name": MCP, "token": "ghp_x", "services": ["repos"], "account_label": "pat-1"})
        assert resp.status_code == 200, resp.text
        assert catalog("pat-1")["granted_scopes"] is None
        # No account asked: not known either.
        assert hook.client.get(f"/v1/mcps/{MCP}/webhook-event-catalog").json()["granted_scopes"] is None

    def test_an_agents_catalog_shows_the_bound_scopes_to_its_managers_alone(self, hook, monkeypatch):
        _mark(hook, kinds.KIND_OAUTH_APP)
        _connect_with_services(hook)
        hook.holder["role"] = "admin"
        task_store.add_user_agent(hook.sub, AGENT, "manager", "test")
        from services.mcp import mcp_registry
        manifest = mcp_registry.get_manifest(MCP)
        monkeypatch.setattr(mcp_registry, "get_visible_mcps_for_agent", lambda agent: [manifest])
        assert hook.client.put(f"/v1/agents/{AGENT}/mcps/{MCP}/service-binding",
                               json={"account_label": "alice"}).status_code == 200
        params = {"account_label": "alice", "scope": "service", "agent": AGENT}
        assert hook.client.get(f"/v1/mcps/{MCP}/webhook-event-catalog", params=params).json()["granted_scopes"] == ["repo"]
        # A member of the agent who cannot manage it reads the catalog, not the scopes.
        from auth.providers import UserContext
        monkeypatch.setattr(UserContext, "can_manage_agent", lambda self, agent: False)
        body = hook.client.get(f"/v1/mcps/{MCP}/webhook-event-catalog", params=params).json()
        assert body["granted_scopes"] is None and body["event_catalog"]

    def test_the_catalog_for_an_agent_offers_its_bound_installation_alone(self, hook, monkeypatch):
        hook.holder["role"] = "admin"
        task_store.add_user_agent(hook.sub, AGENT, "manager", "test")
        from services.mcp import mcp_registry
        manifest = mcp_registry.get_manifest(MCP)
        monkeypatch.setattr(mcp_registry, "get_visible_mcps_for_agent", lambda agent: [manifest])
        hook.fake.installations = [_inst(10), _inst(12)]
        hook.client.post(f"/v1/oauth/{PROVIDER}/app/installations/refresh", json={"mcp_name": MCP, "account_label": "alice"})
        params = {"account_label": "alice", "scope": "service", "agent": AGENT}
        assert hook.client.put(f"/v1/agents/{AGENT}/mcps/{MCP}/service-binding",
                               json={"account_label": "alice", "installation_id": ""}).status_code == 200
        spec = hook.client.get(f"/v1/mcps/{MCP}/webhook-event-catalog", params=params).json()["vendor_target_spec"]
        assert spec["static_options"] == []
        assert spec["help_text"] == "First pick an installation as this agent's GitHub account in Agent Settings, MCPs."
        hook.fake.routes[("POST", "/app/installations/12/access_tokens")] = (201, {"token": "ghs_y", "expires_at": "2099-01-01T00:00:00Z"})
        assert hook.client.put(f"/v1/agents/{AGENT}/mcps/{MCP}/service-binding",
                               json={"account_label": "alice", "installation_id": "12"}).status_code == 200
        spec = hook.client.get(f"/v1/mcps/{MCP}/webhook-event-catalog", params=params).json()["vendor_target_spec"]
        assert [o["value"] for o in spec["static_options"]] == ["12"]

    def test_a_hosted_account_bound_to_an_agent_subscribes_its_owned_installations(self, fwd, monkeypatch):
        sid = _bind_hosted_agent(fwd, monkeypatch)
        row = webhook_subscription_store.get_subscription(sid)
        assert row["status"] == "active" and row["target_kind"] == kinds.INSTALLATION_TARGET_KIND
        assert row["agent"] == AGENT and row["delivery_mode"] == "relay"
        # Only an installation the account owns now, unsuspended and listed.
        meta = _meta(fwd)
        org = {**meta["installations"][0], "id": 11, "account": "octo", "admin": False}
        held = {**meta["installations"][0], "id": 13, "suspended_at": "2026-10-15T00:00:00Z"}
        credential_store.set_account_meta(fwd.sub, MCP, "alice", {**meta, "installations": [
            meta["installations"][0], org, held]})
        for target, words in (("11", "not one you own on GitHub"), ("13", "suspended"),
                              ("12", "not installed there any more")):
            resp = _subscribe(fwd, target, scope="service", agent=AGENT)
            assert resp.status_code == 400 and words in resp.text, (target, resp.text)

    def test_a_hosted_agents_catalog_lists_every_owned_installation(self, fwd, monkeypatch):
        _bind_hosted_agent(fwd, monkeypatch, subscribe=False)
        params = {"account_label": "alice", "scope": "service", "agent": AGENT}
        spec = fwd.client.get(f"/v1/mcps/{MCP}/webhook-event-catalog", params=params).json()["vendor_target_spec"]
        assert [o["value"] for o in spec["static_options"]] == ["10"]
        assert spec["help_text"] == "One subscription covers every repository the installation reaches, now and later."
        credential_store.set_account_meta(fwd.sub, MCP, "alice", {**_meta(fwd), "installations": []})
        spec = fwd.client.get(f"/v1/mcps/{MCP}/webhook-event-catalog", params=params).json()["vendor_target_spec"]
        assert spec["static_options"] == []
        assert spec["help_text"] == "Install the app on GitHub first. You can subscribe only to an installation you own."

    def test_a_forwarded_delivery_fires_the_hosted_agents_row(self, fwd, monkeypatch):
        sid = _bind_hosted_agent(fwd, monkeypatch)
        _trigger_for(monkeypatch, sid)
        resp = _forward(fwd, "issues", _issue_body(10), delivery_id="ha-1")
        assert resp.json()["fired"] == 1 and len(fwd.fired) == 1
        # The binding moved to another of the person's accounts: the row stops.
        credential_store.set_user_credentials(fwd.sub, MCP, {"GITHUB_EMAIL": "x"}, account_label="other")
        assert credential_store.set_service_agent_binding(MCP, AGENT, account_label="other", owner_sub=fwd.sub)
        resp = _forward(fwd, "issues", _issue_body(10), delivery_id="ha-2")
        assert resp.json()["fired"] == 0
        assert resp.json()["results"][0]["skipped"] == "not the agent's bound installation"

    def test_a_hosted_oauth_app_repository_subscription_stays_vendor(self, hook, monkeypatch):
        from services.billing import relay_client
        monkeypatch.setattr(relay_client, "is_available", lambda: True)
        monkeypatch.setattr(subscription_manager, "_resolve_account_extra", lambda **kw: {"via_relay": True})
        mode = subscription_manager._effective_registration_mode(
            webhooks_block=WEBHOOKS_BLOCK, scope="user", owner=hook.sub, agent=None,
            mcp_name=MCP, account_label="alice", target_kind="",
        )
        assert mode == "manual"
        mode = subscription_manager._effective_registration_mode(
            webhooks_block=WEBHOOKS_BLOCK, scope="user", owner=hook.sub, agent=None,
            mcp_name=MCP, account_label="alice", target_kind=kinds.INSTALLATION_TARGET_KIND,
        )
        assert mode == "relay"

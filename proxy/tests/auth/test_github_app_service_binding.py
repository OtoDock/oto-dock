"""An installation of a GitHub-App account as an agent's service identity
(services/oauth/github_apps.py, credential_resolver.py, the binding
routes): the bind needs the owner standing, the installation's token file
is minted eagerly at the bind and lazily for a session (the request body
pinned), never on the event loop, the command-line tools get it whatever
the MCP server gets, the refusals carry their reasons, a passing failure
keeps an unexpired file, the worker renews a standing binding's file and
deletes an orphan, a disconnect reaps a file no other binding names, and
OtoDock's hosted app binds an account only (a hosted installation binding
left from before stops with ``app_changed``).

Uses the connect rig (tests/auth/test_github_app_connect.py): the fake
GitHub behind both clients and a connected GitHub-App account.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from auth.oauth_providers.base import OAuthTokenError
from core.credentials import mcp_gateway
from services.mcp import mcp_registry
from services.oauth import (
    credential_resolver,
    github_apps,
    oauth_account_store,
    oauth_refresh_worker,
)
from storage import database as task_store
from storage.identity import credential_store, oauth_app_kinds as kinds
from storage.pg import get_conn
from tests.auth.test_github_app_connect import (  # noqa: F401 (fixtures)
    MCP,
    PROVIDER,
    _connect,
    _inst,
    pem,
    person,
    rig,
)

AGENT = "gh-bound-agent"


async def _async(fn):
    return fn()


def _expires_at(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _mint_route(token: str = "ghs_minted", seconds: int = 3600, status: int = 201, body: dict | None = None):
    def route(request: httpx.Request) -> httpx.Response:
        if status != 201:
            return httpx.Response(status, json=body or {"message": "no"})
        return httpx.Response(201, json={
            "token": token, "expires_at": _expires_at(seconds),
            "permissions": {"contents": "write"}, "repository_selection": "all",
        })
    return route


@pytest.fixture
def bound(rig, monkeypatch):  # noqa: F811
    """The rig's person owns installation 10 and manages the agent; the
    MCP is visible to it; the binding routes see an admin."""
    _connect(rig)
    rig.holder["role"] = "admin"
    task_store.add_user_agent(rig.sub, AGENT, "manager", "test")
    manifest = mcp_registry.get_manifest(MCP)
    monkeypatch.setattr(mcp_registry, "get_visible_mcps_for_agent", lambda agent: [manifest])
    monkeypatch.setattr(mcp_registry, "get_agent_mcps_all_placements", lambda agent: [manifest])
    rig.fake.routes[("POST", "/app/installations/10/access_tokens")] = _mint_route()
    github_apps._relisted.clear()
    yield rig
    with get_conn() as conn:
        conn.execute("DELETE FROM service_agent_bindings WHERE agent_name=%s", (AGENT,))
        conn.commit()


def _bind(rig, installation_id, label="alice"):  # noqa: F811
    body = {"account_label": label}
    if installation_id is not None:
        body["installation_id"] = installation_id
    return rig.client.put(f"/v1/agents/{AGENT}/mcps/{MCP}/service-binding", json=body)


def _app_file(rig):  # noqa: F811
    return github_apps.installation_file(PROVIDER, 10)


def _mints(rig):  # noqa: F811
    return [r for r in rig.fake.requests if r.url.path.endswith("/access_tokens")]


class TestBind:
    def test_the_bind_mints_the_first_token_and_the_options_show_it(self, bound):
        resp = _bind(bound, "10")
        assert resp.status_code == 200, resp.text
        assert resp.json()["installation_id"] == "10" and "warning" not in resp.json()
        assert credential_store.get_service_agent_binding(MCP, AGENT)[:3] == ("alice", bound.sub, "10")
        raw = json.loads(_app_file(bound).read_text())
        assert raw["access_token"] == "ghs_minted" and raw["refresh_token"] == "" and raw["client_secret"] == ""
        assert raw["extra"]["flow"] == kinds.INSTALLATION_FLOW and raw["extra"]["installation_id"] == 10
        assert raw["extra"]["app_id"] == "4242" and raw["extra"]["account"] == "alice"
        assert raw["expires_at"].endswith("Z")
        assert _mints(bound)[-1].content == b""     # the whole installation
        assert _mints(bound)[-1].headers["authorization"].startswith("Bearer eyJ")
        opts = bound.client.get(f"/v1/agents/{AGENT}/mcps/{MCP}/service-account-options").json()
        acc = opts["my_accounts"][0]
        assert acc["kind"] == kinds.KIND_GITHUB_APP
        assert acc["installations"] == [{"id": 10, "account": "alice", "target_type": "User",
                                         "repository_selection": "all", "repositories_count": 0,
                                         "repositories_truncated": False, "suspended": False, "admin": True}]
        assert opts["current_binding"]["installation_id"] == "10"
        assert opts["current_binding"]["installation_account"] == "alice"

    def test_reaching_is_not_owning(self, bound):
        bound.fake.installations = [_inst(10), _inst(11, login="octo", account_id=9, target="Organization")]
        bound.fake.routes[("GET", "/user/memberships/orgs/octo")] = (200, {"role": "member", "state": "active"})
        bound.client.post(f"/v1/oauth/{PROVIDER}/app/installations/refresh",
                          json={"mcp_name": MCP, "account_label": "alice"})
        resp = _bind(bound, "11")
        assert resp.status_code == 404 and "not one you own on GitHub" in resp.json()["detail"]
        assert credential_store.get_service_agent_binding(MCP, AGENT) is None
        assert _bind(bound, "12").status_code == 404

    def test_an_older_dashboard_keeps_the_installation_and_an_empty_id_clears_it(self, bound):
        assert _bind(bound, "10").status_code == 200
        assert _bind(bound, None).status_code == 200
        assert credential_store.get_service_agent_binding(MCP, AGENT).installation_id == "10"
        assert _app_file(bound).exists()
        assert _bind(bound, "").status_code == 200
        assert credential_store.get_service_agent_binding(MCP, AGENT).installation_id == ""
        assert not _app_file(bound).exists()        # no binding names it any more

    def test_a_mint_failure_at_the_bind_is_a_warning(self, bound):
        bound.fake.routes[("POST", "/app/installations/10/access_tokens")] = _mint_route(status=500)
        resp = _bind(bound, "10")
        assert resp.status_code == 200 and "could not be reached" in resp.json()["warning"]
        assert credential_store.get_service_agent_binding(MCP, AGENT).installation_id == "10"
        assert not _app_file(bound).exists()

    def test_the_clear_reaps_the_file(self, bound):
        assert _bind(bound, "10").status_code == 200 and _app_file(bound).exists()
        resp = bound.client.delete(f"/v1/agents/{AGENT}/mcps/{MCP}/service-binding")
        assert resp.status_code == 200 and not _app_file(bound).exists()


class TestSessions:
    def test_the_command_line_tools_get_the_installation_token(self, bound):
        assert _bind(bound, "10").status_code == 200
        ref = credential_resolver.pick_account(MCP, AGENT)
        assert ref.installation_id == "10"
        resolved = credential_resolver.resolve_credentials(AGENT, None, task_scope="agent")
        assert MCP in resolved.available_mcps, resolved.exclusion_reasons
        cred = resolved.cli_credentials[MCP]
        assert cred.token_ref.account_label == "10" and cred.token_ref.token_dir.endswith(f"/{kinds.APP_TOKEN_DIR}")
        got = mcp_gateway.resolve_cli_credential(cred)
        assert isinstance(got, mcp_gateway.ResolvedCli)
        assert got.secret == "ghs_minted" and got.username == "x-access-token"
        assert got.env_names == ("GH_TOKEN", "GITHUB_TOKEN")

    def test_the_mcp_server_gets_the_owners_token_unless_the_kind_says_otherwise(self, bound, monkeypatch):
        assert _bind(bound, "10").status_code == 200
        manifest = mcp_registry.get_manifest(MCP)
        asked: list[str] = []
        real = github_apps.app_in_use
        monkeypatch.setattr(github_apps, "app_in_use", lambda name: (asked.append(name), real(name))[1])
        ref, reason = mcp_registry._gateway_bearer_source(manifest, None, AGENT, "agent")
        assert reason is None and ref.account_label == "alice" and bound.username in ref.token_dir
        assert asked == []                              # no kind says installation: nobody asked
        manifest.credentials.oauth["app_kinds"]["kinds"][1]["mcp_token"] = kinds.MCP_TOKEN_INSTALLATION
        ref, reason = mcp_registry._gateway_bearer_source(manifest, None, AGENT, "agent")
        assert reason is None and ref.account_label == "10" and ref.token_dir.endswith(kinds.APP_TOKEN_DIR)
        assert asked and set(asked) == {MCP}             # the kind's answer, then the mint's own read
        got = mcp_gateway._token_secret(ref, MCP)
        assert got[0] == "ghs_minted"
        # On the event loop the relay is never asked: the user token then.
        asked.clear()
        assert asyncio.run(_async(lambda: github_apps.mcp_gets_installation_token(MCP))) is False
        assert asked == []

    def test_a_users_own_session_is_untouched(self, bound):
        assert _bind(bound, "10").status_code == 200
        manifest = mcp_registry.get_manifest(MCP)
        ref, reason = mcp_registry._gateway_bearer_source(manifest, bound.sub, AGENT, "user")
        assert reason is None and ref.account_label == "alice"

    def test_the_lazy_mint_renews_an_old_file_and_keeps_a_fresh_one(self, bound):
        assert _bind(bound, "10").status_code == 200
        path = _app_file(bound)
        first = json.loads(path.read_text())
        assert github_apps.ensure_installation_token(MCP, bound.sub, "alice", "10") == path
        assert len(_mints(bound)) == 1                 # fresh: reused
        path.write_text(json.dumps({**first, "expires_at": _expires_at(100)}))
        bound.fake.routes[("POST", "/app/installations/10/access_tokens")] = _mint_route("ghs_renewed")
        github_apps.ensure_installation_token(MCP, bound.sub, "alice", "10")
        assert json.loads(path.read_text())["access_token"] == "ghs_renewed"
        assert len(_mints(bound)) == 2

    def test_nothing_is_minted_on_the_event_loop(self, bound, monkeypatch):
        assert _bind(bound, "10").status_code == 200
        path = _app_file(bound)
        raw = json.loads(path.read_text())
        path.write_text(json.dumps({**raw, "expires_at": _expires_at(100)}))
        # Nor is the relay or the bundle read there: the file answers alone.
        monkeypatch.setattr(github_apps, "app_in_use", lambda name: (_ for _ in ()).throw(AssertionError("asked")))

        async def on_loop(expect_path: bool):
            if expect_path:
                return github_apps.ensure_installation_token(MCP, bound.sub, "alice", "10")
            with pytest.raises(credential_resolver.CredentialUnavailable):
                github_apps.ensure_installation_token(MCP, bound.sub, "alice", "10")
        assert asyncio.run(on_loop(True)) == path           # alive: served as it is
        assert len(_mints(bound)) == 1
        path.write_text(json.dumps({**raw, "expires_at": _expires_at(-10)}))
        asyncio.run(on_loop(False))                           # expired: waits for a thread
        assert len(_mints(bound)) == 1

    def test_an_expired_file_never_serves_a_session(self, bound):
        assert _bind(bound, "10").status_code == 200
        path = _app_file(bound)
        raw = json.loads(path.read_text())
        expired = {**raw, "expires_at": _expires_at(-10)}
        manifest = mcp_registry.get_manifest(MCP)
        assert credential_resolver.account_unusable_reason(expired, manifest) == kinds.INSTALLATION_EXPIRED
        assert credential_resolver.account_unusable_reason(raw, manifest) == ""
        assert oauth_account_store.token_dead_reason(expired) == ""   # the worker renews it
        path.write_text(json.dumps(expired))
        ref = mcp_gateway.TokenRef(str(path.parent), "10")
        refused = mcp_gateway._token_secret(ref, MCP)
        assert isinstance(refused, mcp_gateway.Refusal) and "lapsed" in refused.detail

    @pytest.mark.parametrize("status, body, reason", [
        (404, {"message": "Not Found"}, kinds.INSTALLATION_GONE),
        (401, {"message": "bad key"}, kinds.APP_KEY_REJECTED),
        (403, {"message": "This installation has been suspended"}, kinds.INSTALLATION_SUSPENDED),
    ])
    def test_a_permanent_refusal_marks_the_file_and_says_why(self, bound, status, body, reason):
        assert _bind(bound, "10").status_code == 200
        path = _app_file(bound)
        raw = json.loads(path.read_text())
        path.write_text(json.dumps({**raw, "expires_at": _expires_at(100)}))
        bound.fake.routes[("POST", "/app/installations/10/access_tokens")] = _mint_route(status=status, body=body)
        with pytest.raises(credential_resolver.AccountNeedsReconnect) as exc:
            github_apps.ensure_installation_token(MCP, bound.sub, "alice", "10")
        assert str(exc.value) == reason
        assert json.loads(path.read_text())["extra"]["refresh_failed"] == reason
        resolved = credential_resolver.resolve_credentials(AGENT, None, task_scope="agent")
        assert MCP in resolved.excluded_mcps
        assert credential_resolver.reconnect_wording(reason) in resolved.exclusion_reasons[MCP]

    def test_a_passing_failure_keeps_an_unexpired_file(self, bound):
        assert _bind(bound, "10").status_code == 200
        path = _app_file(bound)
        raw = json.loads(path.read_text())
        path.write_text(json.dumps({**raw, "expires_at": _expires_at(100)}))
        bound.fake.routes[("POST", "/app/installations/10/access_tokens")] = _mint_route(status=500)
        assert github_apps.ensure_installation_token(MCP, bound.sub, "alice", "10") == path
        assert json.loads(path.read_text())["access_token"] == "ghs_minted"
        path.unlink()
        with pytest.raises(credential_resolver.CredentialUnavailable):
            github_apps.ensure_installation_token(MCP, bound.sub, "alice", "10")
        resolved = credential_resolver.resolve_credentials(AGENT, None, task_scope="agent")
        assert MCP in resolved.excluded_mcps and "could not be reached" in resolved.exclusion_reasons[MCP]

    def test_a_suspended_or_vanished_installation_is_refused_before_any_mint(self, bound):
        assert _bind(bound, "10").status_code == 200
        n = len(_mints(bound))
        credential_store.update_account_meta(bound.sub, MCP, "alice", lambda m: {
            **m, "installations": [{**m["installations"][0], "suspended_at": "2026-10-15T00:00:00Z"}]})
        with pytest.raises(credential_resolver.AccountNeedsReconnect) as exc:
            github_apps.ensure_installation_token(MCP, bound.sub, "alice", "10")
        assert str(exc.value) == kinds.INSTALLATION_SUSPENDED
        credential_store.set_account_meta(bound.sub, MCP, "alice", {"kind": kinds.KIND_GITHUB_APP, "installations": []})
        with pytest.raises(credential_resolver.AccountNeedsReconnect) as exc:
            github_apps.ensure_installation_token(MCP, bound.sub, "alice", "10")
        assert str(exc.value) == kinds.INSTALLATION_GONE
        assert credential_resolver.pick_account(MCP, AGENT) is not None   # the row stays
        assert len(_mints(bound)) == n

    def test_the_worker_re_checks_every_lender_before_it_mints(self, bound):
        org = _inst(11, login="octo", account_id=9, target="Organization")
        bound.fake.installations = [_inst(10), org]
        bound.fake.routes[("GET", "/user/memberships/orgs/octo")] = (200, {"role": "admin", "state": "active"})
        bound.client.post(f"/v1/oauth/{PROVIDER}/app/installations/refresh",
                          json={"mcp_name": MCP, "account_label": "alice"})
        bound.fake.routes[("POST", "/app/installations/11/access_tokens")] = _mint_route("ghs_org")
        assert _bind(bound, "11").status_code == 200
        path = github_apps.installation_file(PROVIDER, 11)
        raw = json.loads(path.read_text())
        path.write_text(json.dumps({**raw, "expires_at": _expires_at(100)}))
        # The meta still says owner; GitHub says the person was demoted.
        bound.fake.routes[("GET", "/user/memberships/orgs/octo")] = (200, {"role": "member", "state": "active"})
        github_apps._relisted.clear()
        oauth_refresh_worker._failure_state.clear()
        mints_before = len([r for r in bound.fake.requests if r.url.path.endswith("/11/access_tokens")])
        renewed = asyncio.run(oauth_refresh_worker._maybe_refresh_token_file(
            token_file=path, username=kinds.APP_TOKEN_DIR, provider_id=PROVIDER))
        assert renewed is False
        assert json.loads(path.read_text())["extra"]["refresh_failed"] == kinds.INSTALLATION_NOT_OWNED
        assert len([r for r in bound.fake.requests if r.url.path.endswith("/11/access_tokens")]) == mints_before
        meta = credential_store.get_account_meta(bound.sub, MCP, "alice")
        assert kinds.installation_standing(meta, "11") == kinds.INSTALLATION_NOT_OWNED
        # The re-check is per lender and installation: another's standing is its own.
        assert (bound.sub, "alice", "11") in github_apps._relisted

    def test_the_owner_is_reconfirmed_with_their_own_token(self, bound):
        assert _bind(bound, "10").status_code == 200
        path = _app_file(bound)
        raw = json.loads(path.read_text())
        path.write_text(json.dumps({**raw, "expires_at": _expires_at(100)}))
        github_apps._relisted.clear()
        bound.fake.installations = []          # GitHub says the installation is gone
        with pytest.raises(credential_resolver.AccountNeedsReconnect) as exc:
            github_apps.ensure_installation_token(MCP, bound.sub, "alice", "10")
        assert str(exc.value) == kinds.INSTALLATION_GONE
        assert credential_store.get_account_meta(bound.sub, MCP, "alice")["installations"] == []

    def test_an_app_step_gets_a_token_with_room(self, bound, monkeypatch):
        assert _bind(bound, "10").status_code == 200
        manifest = mcp_registry.get_manifest(MCP)
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda agent, placement=None: [manifest])
        path = _app_file(bound)
        raw = json.loads(path.read_text())
        path.write_text(json.dumps({**raw, "expires_at": _expires_at(1200)}))   # under the step's margin
        bound.fake.routes[("POST", "/app/installations/10/access_tokens")] = _mint_route("ghs_for_step")
        env = credential_resolver.cli_token_env(AGENT, None, task_scope="agent", providers=[PROVIDER])
        assert env["GH_TOKEN"] == "ghs_for_step"


class TestWorkerAndReaping:
    def test_the_worker_renews_a_standing_bindings_file_and_deletes_an_orphan(self, bound):
        assert _bind(bound, "10").status_code == 200
        path = _app_file(bound)
        raw = json.loads(path.read_text())
        path.write_text(json.dumps({**raw, "expires_at": _expires_at(100)}))
        bound.fake.routes[("POST", "/app/installations/10/access_tokens")] = _mint_route("ghs_by_worker")
        oauth_refresh_worker._failure_state.clear()
        renewed = asyncio.run(oauth_refresh_worker._maybe_refresh_token_file(
            token_file=path, username=kinds.APP_TOKEN_DIR, provider_id=PROVIDER))
        assert renewed is True
        assert json.loads(path.read_text())["access_token"] == "ghs_by_worker"
        credential_store.remove_service_agent_binding(MCP, AGENT)
        path.write_text(json.dumps({**raw, "expires_at": _expires_at(100)}))
        renewed = asyncio.run(oauth_refresh_worker._maybe_refresh_token_file(
            token_file=path, username=kinds.APP_TOKEN_DIR, provider_id=PROVIDER))
        assert renewed is False and not path.exists()

    def test_a_shared_file_survives_one_persons_disconnect(self, bound):
        assert _bind(bound, "10").status_code == 200
        path = _app_file(bound)
        other_sub = f"test-user-{uuid.uuid4().hex[:12]}"
        other_name = f"u{uuid.uuid4().hex[:8]}"
        other_agent = "gh-other-agent"
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO users (sub, email, name, username, role, auth_provider, created_at, last_login) "
                "VALUES (%s, %s, 'Other', %s, 'creator', 'local', NOW()::text, NOW()::text)",
                (other_sub, f"{other_name}@test.example", other_name))
            conn.commit()
        try:
            credential_store.set_user_credentials(other_sub, MCP, {"GITHUB_EMAIL": "bob"}, account_label="bob")
            credential_store.set_account_meta(other_sub, MCP, "bob", {
                "kind": kinds.KIND_GITHUB_APP, "login": "bob", "user_id": 2,
                "installations": [{**_inst(10), "admin": True, "repositories": []}]})
            task_store.add_user_agent(other_sub, other_agent, "manager", "test")
            assert credential_store.set_service_agent_binding(
                MCP, other_agent, account_label="bob", owner_sub=other_sub, installation_id="10")
            resp = bound.client.post(f"/v1/oauth/{PROVIDER}/disconnect",
                                     json={"mcp_name": MCP, "account_label": "alice"})
            assert resp.status_code == 200, resp.text
            assert credential_store.get_service_agent_binding(MCP, AGENT) is None
            assert path.exists()                      # bob's binding still names it
            credential_store.remove_service_agent_binding(MCP, other_agent)
            assert github_apps.reap_if_orphan(PROVIDER, "10") is True and not path.exists()
        finally:
            with get_conn() as conn:
                conn.execute("DELETE FROM service_agent_bindings WHERE agent_name=%s", (other_agent,))
                conn.execute("DELETE FROM users WHERE sub=%s", (other_sub,))
                conn.commit()


def _hosted(monkeypatch, posted: list[str]) -> None:
    """OtoDock's app through the relay from now on: the relay says the same
    app id and records every path it is asked."""
    from services.billing import relay_client
    monkeypatch.setattr(relay_client, "hosted_oauth_active", lambda *a, **k: True)
    monkeypatch.setattr(relay_client, "oauth_app_info", lambda provider_id: {
        "kind": kinds.KIND_GITHUB_APP, "app_id": "4242", "slug": "otodock", "name": "OtoDock"})
    monkeypatch.setattr(relay_client, "_relay_post_sync", lambda path, payload: posted.append(path) or {})


class TestHosted:
    def test_a_hosted_installation_binding_is_refused(self, bound, monkeypatch):
        posted: list[str] = []
        _hosted(monkeypatch, posted)
        resp = _bind(bound, "10")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Hosted sign-in has no app identity. Bind the account itself."
        assert credential_store.get_service_agent_binding(MCP, AGENT) is None
        assert _bind(bound, "").status_code == 200          # the account itself binds
        assert credential_store.get_service_agent_binding(MCP, AGENT)[:3] == ("alice", bound.sub, "")
        assert posted == [] and not _mints(bound)

    def test_an_old_hosted_installation_binding_stops_resolving(self, bound, monkeypatch):
        assert _bind(bound, "10").status_code == 200
        path = _app_file(bound)
        raw = json.loads(path.read_text())
        # A live file the relay minted before it held no key any more.
        path.write_text(json.dumps({**raw, "extra": {**raw["extra"], "via_relay": True}}))
        posted: list[str] = []
        _hosted(monkeypatch, posted)
        github_apps._relisted.clear()
        with pytest.raises(credential_resolver.AccountNeedsReconnect) as why:
            github_apps.ensure_installation_token(MCP, bound.sub, "alice", "10")
        assert str(why.value) == kinds.INSTALLATION_HOSTED
        assert credential_resolver.reconnect_wording(str(why.value)) == (
            "hosted sign-in gives an agent no app identity; a manager binds the account itself "
            "in Agent Settings, MCPs")

        async def on_loop():
            github_apps.ensure_installation_token(MCP, bound.sub, "alice", "10")
        with pytest.raises(credential_resolver.AccountNeedsReconnect) as why:
            asyncio.run(on_loop())
        assert str(why.value) == kinds.INSTALLATION_HOSTED              # not "try again in a minute"
        # A relay outage changes nothing: the relay is never asked first.
        monkeypatch.setattr(github_apps, "app_in_use", lambda name: (_ for _ in ()).throw(httpx.ConnectError("down")))
        with pytest.raises(credential_resolver.AccountNeedsReconnect) as why:
            github_apps.ensure_installation_token(MCP, bound.sub, "alice", "10")
        assert str(why.value) == kinds.INSTALLATION_HOSTED
        listings = len([r for r in bound.fake.requests if r.url.path == "/user/installations"])
        with pytest.raises(OAuthTokenError) as refused:
            github_apps.remint_file(path, json.loads(path.read_text()), provider_id=PROVIDER)
        assert refused.value.code == kinds.INSTALLATION_HOSTED
        # Refused before the lenders' re-check spends a listing.
        assert len([r for r in bound.fake.requests if r.url.path == "/user/installations"]) == listings
        assert posted == [] and len(_mints(bound)) == 1     # the bind's mint, nothing since

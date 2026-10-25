"""The connect flow of the GitHub-App kind (api/auth/oauth.py,
services/oauth/github_apps.py): the start asks for no services and no
scope with the kind's client, the callback records the kind and
the installations the person can reach, the install page returns to the
setup URL which re-lists them, Refresh re-lists them on demand, a reconnect
through another kind clears them, the integrations list shows them, and
an admin's GitHub-App bundle is verified at save.

GitHub is a fake behind both clients (the generic provider's and the app
half's); the app key is a throwaway generated here.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import config
from auth.oauth_providers import github_app
from auth.providers import UserContext, get_current_user
from services.mcp import mcp_manifest_parse, mcp_registry
from services.oauth import github_apps, oauth_account_store, oauth_engine
from storage.identity import credential_store, oauth_app_kinds as kinds
from storage.pg import get_conn
from tests.auth.test_github_app import FIELDS, OAUTH_FIELDS

MCP = "github-connect-mcp"
PROVIDER = "github"
DASH = "https://dash.example"
BUNDLE_PREFIX = "gh-connect-app-"
_REAL_ASYNC_CLIENT = httpx.AsyncClient


WEBHOOKS_BLOCK = {
    "available": True, "provider_id": PROVIDER,
    "signature": {"algorithm": "hmac-sha256", "header": "X-Hub-Signature-256", "prefix": "sha256=",
                  "per_subscription_secret": True, "secret_credential_key": "", "max_age_seconds": 0},
    "url_verification": {"kind": "none"},
    "registration": {"mode": "manual",
                     "manual_instructions_url": "https://github.com/settings/apps"},
    "event_catalog": [
        {"key": "issues", "label": "Issues", "description": "d", "required_scopes": ["repo"]},
        {"key": "push", "label": "Push", "description": "d", "required_scopes": ["repo"]},
    ],
    "payload_normalization": {
        "event_type_path": "headers.X-GitHub-Event",
        "actor": {"id_path": "body.sender.id", "name_path": "body.sender.login"},
        "subject": {"type_path": "body.action", "id_path": ["body.issue.number"]},
        "target": {"type": "repository", "id_path": "body.repository.full_name"},
    },
    "vendor_target_spec": {
        "kind": "free_text", "label": "Repository (owner/name)", "placeholder": "octocat/hello-world",
        "validation_regex": "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
        "target_kinds": [{"key": "repository", "label": "One repository", "placeholder": "octocat/hello-world",
                          "validation_regex": "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"}],
    },
    "event_id_field": "headers.X-GitHub-Delivery",
    "app_webhook": {"secret_credential_key": "GITHUB_APP_WEBHOOK_SECRET",
                    "installation_id_path": "body.installation.id"},
}


def _manifest_data(bundle: str) -> dict:
    return {
        "name": MCP, "label": "GitHub", "description": "d", "version": "2.1.0", "category": "community",
        "server": {"transport": "streamable_http", "url_template": "https://api.githubcopilot.com/mcp/",
                   "source": "remote:api.githubcopilot.com"},
        "credentials": {
            "type": "per_user", "label": "GitHub Account", "service_account": True,
            "oauth": {
                "provider_id": PROVIDER,
                "flows": ["authorization_code", "personal_access_token"],
                "authorization_url": "https://github.com/login/oauth/authorize",
                "token_url": "https://github.com/login/oauth/access_token",
                "revoke_url": "https://github.com/login/oauth/revoke",
                "userinfo_url": "https://api.github.com/user",
                "userinfo_id_field": "login",
                "bearer_required": True, "proposed_hosts": ["api.githubcopilot.com"],
                "env_injection": ["GH_TOKEN", "GITHUB_TOKEN"],
                "git_credential_helper": {"host": "github.com", "helper": "!gh auth git-credential"},
                "app_credential": bundle, "app_credential_fields": OAUTH_FIELDS,
                "app_kinds": {"key": "GITHUB_APP_KIND", "kinds": [
                    {"kind": kinds.KIND_OAUTH_APP, "label": "OAuth App", "fields": OAUTH_FIELDS},
                    {"kind": kinds.KIND_GITHUB_APP, "label": "GitHub App", "mcp_token": kinds.MCP_TOKEN_USER,
                     "fields": FIELDS},
                ]},
                "services": [{"key": "repos", "label": "Repositories", "description": "d", "scopes": ["repo"]}],
            },
            "webhooks": WEBHOOKS_BLOCK,
        },
    }


def _inst(iid: int, *, login="alice", account_id=1, target="User", selection="all") -> dict:
    return {"id": iid, "app_id": 4242, "target_type": target, "repository_selection": selection,
            "account": {"login": login, "id": account_id}, "permissions": {"contents": "write"},
            "suspended_at": None}


class FakeGitHub:
    """The token endpoint, the API and the installations: ``(method, path)``
    → an answer; every request logged."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.installations: list[dict] = [_inst(10)]
        self.user_status = 200
        self.routes: dict[tuple[str, str], object] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key in self.routes:
            route = self.routes[key]
            return route(request) if callable(route) else httpx.Response(route[0], json=route[1])
        if key == ("POST", "/login/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "ghu_1", "refresh_token": "ghr_1",
                                             "expires_in": 28800, "token_type": "bearer", "scope": ""})
        if key == ("GET", "/user"):
            if self.user_status != 200:
                return httpx.Response(self.user_status, json={"message": "Bad credentials"})
            return httpx.Response(200, json={"id": 1, "login": "alice", "name": "Alice", "email": ""})
        if key == ("GET", "/user/installations"):
            return httpx.Response(200, json={"installations": self.installations})
        if request.url.path.startswith("/user/installations/") and request.url.path.endswith("/repositories"):
            return httpx.Response(200, json={"repository_selection": "selected",
                                             "repositories": [{"full_name": "alice/repo"}]})
        if key == ("GET", "/app"):
            return httpx.Response(200, json={"id": 4242, "slug": "otodock-dev", "name": "OtoDock dev"})
        return httpx.Response(404, json={"message": "Not Found"})

    def sync_client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), headers=github_app._API_HEADERS)

    def async_client(self, **kw) -> httpx.AsyncClient:
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(self.handler), **kw)

    def exchanges(self) -> list[dict]:
        return [dict(httpx.QueryParams(r.content.decode())) for r in self.requests
                if r.url.path == "/login/oauth/access_token"]


@pytest.fixture(scope="module")
def pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


@pytest.fixture
def person():
    sub = f"test-user-{uuid.uuid4().hex[:12]}"
    username = f"u{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, username, role, auth_provider, created_at, last_login) "
            "VALUES (%s, %s, 'Test', %s, 'creator', 'local', NOW()::text, NOW()::text)",
            (sub, f"{username}@test.example", username),
        )
        conn.commit()
    yield sub, username
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE sub = %s", (sub,))
        conn.commit()


@pytest.fixture
def rig(monkeypatch, tmp_path, person, pem):
    sub, username = person
    bundle = BUNDLE_PREFIX + uuid.uuid4().hex[:8]
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", DASH)
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(mcp_registry, "_manifests", {})
    from auth import oauth_providers
    oauth_providers.clear_manifest_cache()
    path = tmp_path / MCP / "manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_manifest_data(bundle)))
    manifest = mcp_manifest_parse._parse_manifest(path)
    mcp_registry._manifests[manifest.name] = manifest

    fake = FakeGitHub()
    monkeypatch.setattr(github_app, "_client", fake.sync_client)
    github_app.forget_jwts()
    from services.billing import relay_client
    monkeypatch.setattr(relay_client, "hosted_oauth_active", lambda *a, **k: False)
    from api.auth import oauth as oauth_api, oauth_app as oauth_app_api
    oauth_app_api._refresh_last.clear()

    credential_store.set_infra_credentials(bundle, {
        "GITHUB_CLIENT_ID": "oauth-cid", "GITHUB_CLIENT_SECRET": "oauth-sec",
        "GITHUB_APP_KIND": kinds.KIND_GITHUB_APP,
        "GITHUB_APP_ID": "4242", "GITHUB_APP_CLIENT_ID": "Iv1.cafe", "GITHUB_APP_CLIENT_SECRET": "app-sec",
        "GITHUB_APP_PRIVATE_KEY": pem, "GITHUB_APP_WEBHOOK_SECRET": "whsec",
        github_app.APP_SLUG_KEY: "otodock-dev", github_app.APP_NAME_KEY: "OtoDock dev",
    })

    holder = {"role": "creator"}
    from api.mcp import credentials as credentials_api

    async def _stub():
        return UserContext(sub=sub, email=f"{username}@test.example", name="T", role=holder["role"],
                           agents=[], agent_roles={})
    app = FastAPI()
    app.include_router(oauth_api.router)
    app.include_router(oauth_app_api.router)
    app.include_router(credentials_api.router)
    app.dependency_overrides[get_current_user] = _stub
    client = TestClient(app)

    class Rig:
        pass
    r = Rig()
    r.fake, r.client, r.sub, r.username, r.bundle, r.holder = fake, client, sub, username, bundle, holder
    r.token_dir = lambda: oauth_account_store.get_token_dir(username, provider_id=PROVIDER)
    with patch("auth.oauth_providers.generic.httpx.AsyncClient", side_effect=fake.async_client):
        yield r
    oauth_providers.clear_manifest_cache()
    credential_store.delete_infra_credentials(bundle)


def _start(rig, services=(), label="", mobile=False, app_kind=None):
    body = {"mcp_name": MCP, "services": list(services), "account_label": label, "mobile": mobile}
    # A dashboard older than the choice sends no kind at all.
    if app_kind is not None:
        body["app_kind"] = app_kind
    return rig.client.post(f"/v1/oauth/{PROVIDER}/start", json=body)


def _params(url):
    return dict(httpx.QueryParams(url.split("?", 1)[1]))


def _connect(rig, label=""):
    resp = _start(rig, label=label)
    assert resp.status_code == 200, resp.text
    q = _params(resp.json()["url"])
    cb = rig.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"code": "c-1", "state": q["state"]})
    assert cb.status_code == 200 and "Account Connected" in cb.text, cb.text
    return q


def _mark(rig, kind: str) -> None:
    credential_store.set_infra_credentials(rig.bundle, {"GITHUB_APP_KIND": kind})


class TestStart:
    def test_the_github_app_kind_asks_for_no_services_and_no_scope(self, rig):
        resp = _start(rig)
        assert resp.status_code == 200, resp.text
        q = _params(resp.json()["url"])
        assert q.get("scope", "") == "" and q["client_id"] == "Iv1.cafe"
        assert "set-cookie" in resp.headers
        # Names an older dashboard still sends are ignored for the kind.
        assert _start(rig, services=["repos"]).status_code == 200

    def test_the_oauth_app_kind_keeps_its_services(self, rig):
        _mark(rig, kinds.KIND_OAUTH_APP)
        assert _start(rig).status_code == 400
        resp = _start(rig, services=["repos"])
        assert resp.status_code == 200, resp.text
        q = _params(resp.json()["url"])
        assert q["scope"] == "repo" and q["client_id"] == "oauth-cid"

    def test_a_picked_kind_must_be_the_one_the_admin_registered(self, rig):
        resp = _start(rig, services=["repos"], app_kind=kinds.KIND_OAUTH_APP)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "This way to sign in is not set up here. Pick another one."
        resp = _start(rig, services=["repos"], app_kind=kinds.KIND_GITHUB_APP)
        assert resp.status_code == 200 and _params(resp.json()["url"]).get("scope", "") == ""
        resp = _start(rig, app_kind="device")
        assert resp.status_code == 400 and resp.json()["detail"] == "Pick one of the ways to sign in shown."
        _mark(rig, kinds.KIND_OAUTH_APP)
        assert _start(rig, services=["repos"], app_kind=kinds.KIND_GITHUB_APP).status_code == 409
        resp = _start(rig, services=["repos"], app_kind=kinds.KIND_OAUTH_APP)
        assert resp.status_code == 200 and _params(resp.json()["url"])["scope"] == "repo"

    def test_a_kind_with_no_client_set_reads_a_sentence(self, rig):
        from api.auth import oauth as oauth_api
        sentence = "This way to sign in is not set up on this install yet. An admin sets it up in Admin, MCP Servers."
        credential_store.delete_infra_credentials(rig.bundle)
        credential_store.set_infra_credentials(rig.bundle, {"GITHUB_APP_KIND": kinds.KIND_OAUTH_APP})
        resp = _start(rig, services=["repos"])
        assert resp.status_code == 500 and resp.json()["detail"] == sentence
        # The device-code and token routes read the same client the same way.
        with pytest.raises(HTTPException) as refused:
            oauth_api._app_creds_for(PROVIDER, MCP, flow="device_code")
        assert refused.value.status_code == 500 and refused.value.detail == sentence

    def test_a_hosted_connect_asks_the_relay_only_for_a_provider_with_kinds(self, rig, monkeypatch):
        from api.auth import oauth as oauth_api
        from services.billing import relay_client
        asked: list[str] = []

        def hosted_kind(provider):
            asked.append(provider)
            return kinds.KIND_GITHUB_APP
        monkeypatch.setattr(relay_client, "hosted_app_kind", hosted_kind)
        plain = asyncio.run(oauth_api._app_kind_for("google", {"provider_id": "google"}, {}, registered=False, hosted=True))
        assert plain == kinds.KIND_OAUTH_APP and asked == []
        oauth = mcp_registry.get_manifest(MCP).credentials.oauth
        assert asyncio.run(oauth_api._app_kind_for(PROVIDER, oauth, {}, registered=False, hosted=True)) == kinds.KIND_GITHUB_APP
        assert asked == [PROVIDER]
        assert asyncio.run(oauth_api._app_kind_for(PROVIDER, oauth, {}, registered=True, hosted=True)) == ""
        # A pick on a hosted provider with kinds goes to the relay as asked,
        # a kind its manifest does not declare is refused before the relay.
        picked = asyncio.run(oauth_api._app_kind_for(
            PROVIDER, oauth, {}, registered=False, hosted=True, requested=kinds.KIND_OAUTH_APP))
        assert picked == kinds.KIND_OAUTH_APP and asked == [PROVIDER]
        one_kind = {**oauth, "app_kinds": {"key": "K", "kinds": [{"kind": kinds.KIND_OAUTH_APP, "fields": []}]}}
        with pytest.raises(HTTPException) as refused:
            asyncio.run(oauth_api._app_kind_for(
                PROVIDER, one_kind, {}, registered=False, hosted=True, requested=kinds.KIND_GITHUB_APP))
        assert refused.value.status_code == 409 and asked == [PROVIDER]
        with pytest.raises(HTTPException):
            asyncio.run(oauth_api._app_kind_for(
                "google", {"provider_id": "google"}, {}, registered=False, hosted=True,
                requested=kinds.KIND_GITHUB_APP))

    def test_a_refusal_of_the_relay_is_remembered_for_the_ttl(self, monkeypatch):
        from services.billing import relay_client
        calls: list[int] = []

        def refuse(path, payload):
            calls.append(1)
            raise relay_client.RelayError("http_404")
        monkeypatch.setattr(relay_client, "_require_relay", lambda: None)
        monkeypatch.setattr(relay_client, "_relay_identity", lambda: {})
        monkeypatch.setattr(relay_client, "_relay_post_sync", refuse)
        relay_client._app_info_cache.clear()
        assert relay_client.hosted_app_kind("github-ttl") == kinds.KIND_OAUTH_APP
        assert relay_client.hosted_app_kind("github-ttl") == kinds.KIND_OAUTH_APP
        assert calls == [1]
        with pytest.raises(relay_client.RelayError):
            relay_client.oauth_app_info("github-ttl")
        relay_client._app_info_cache.clear()


class TestCallback:
    def test_the_account_records_the_kind_and_the_installations(self, rig):
        rig.fake.installations = [_inst(10), _inst(11, login="octo", account_id=9, target="Organization",
                                                    selection="selected")]
        rig.fake.routes[("GET", "/user/memberships/orgs/octo")] = (200, {"role": "admin", "state": "active"})
        _connect(rig)
        assert rig.fake.exchanges()[-1]["client_id"] == "Iv1.cafe"
        raw = oauth_account_store.read_account_token(rig.token_dir(), "alice")
        assert raw["access_token"] == "ghu_1" and raw["refresh_token"] == "ghr_1"
        assert raw["extra"]["app_kind"] == kinds.KIND_GITHUB_APP and "flow" not in raw["extra"]
        assert raw["scopes"] == []
        meta = credential_store.get_account_meta(rig.sub, MCP, "alice")
        assert (meta["kind"], meta["login"], meta["user_id"]) == (kinds.KIND_GITHUB_APP, "alice", 1)
        assert [(i["id"], i["admin"], i["repositories"]) for i in meta["installations"]] == [
            (10, True, []), (11, True, ["alice/repo"])]
        assert credential_store.get_user_credentials(rig.sub, MCP, "alice")["GITHUB_SERVICES"] == ""

    def test_the_exchange_uses_the_kind_the_start_chose(self, rig):
        resp = _start(rig)
        q = _params(resp.json()["url"])
        _mark(rig, kinds.KIND_OAUTH_APP)      # the admin switches kinds mid-flow
        cb = rig.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"code": "c-1", "state": q["state"]})
        assert cb.status_code == 200 and "Account Connected" in cb.text
        assert rig.fake.exchanges()[-1]["client_id"] == "Iv1.cafe"
        assert credential_store.get_account_meta(rig.sub, MCP, "alice")["kind"] == kinds.KIND_GITHUB_APP

    def test_a_callback_after_an_install_with_authorization_is_the_normal_exchange(self, rig):
        resp = _start(rig)
        q = _params(resp.json()["url"])
        cb = rig.client.get(f"/v1/oauth/{PROVIDER}/callback", params={
            "code": "c-1", "state": q["state"], "installation_id": "999", "setup_action": "install"})
        assert cb.status_code == 200 and "Account Connected" in cb.text
        ids = [i["id"] for i in credential_store.get_account_meta(rig.sub, MCP, "alice")["installations"]]
        assert ids == [10]      # the listing, never the query

    def test_a_listing_failure_keeps_the_account_with_no_installations(self, rig):
        rig.fake.routes[("GET", "/user/installations")] = (500, {"message": "boom"})
        _connect(rig)
        meta = credential_store.get_account_meta(rig.sub, MCP, "alice")
        assert meta["kind"] == kinds.KIND_GITHUB_APP and meta["installations"] == []

    def test_a_reconnect_as_another_github_user_starts_from_no_installations(self, rig):
        _connect(rig)
        assert [i["id"] for i in credential_store.get_account_meta(rig.sub, MCP, "alice")["installations"]] == [10]
        rig.fake.routes[("GET", "/user/installations")] = (500, {"message": "boom"})
        _connect(rig, label="alice")           # the same person, the listing fails: kept
        assert [i["id"] for i in credential_store.get_account_meta(rig.sub, MCP, "alice")["installations"]] == [10]
        rig.fake.routes[("GET", "/user")] = (200, {"id": 2, "login": "bob", "name": "Bob", "email": ""})
        _connect(rig, label="alice")           # another GitHub user under the same label
        meta = credential_store.get_account_meta(rig.sub, MCP, "alice")
        assert (meta["login"], meta["user_id"], meta["installations"]) == ("bob", 2, [])

    def test_a_down_relay_never_turns_a_connect_into_an_error(self, rig, monkeypatch):
        _connect(rig)
        monkeypatch.setattr(github_apps, "app_in_use", lambda mcp_name: (_ for _ in ()).throw(httpx.ConnectError("down")))
        github_apps.record_installations_after_connect(rig.sub, MCP, "alice")     # no raise
        assert [i["id"] for i in credential_store.get_account_meta(rig.sub, MCP, "alice")["installations"]] == [10]
        for path in ("app/install-url", "app/installations/refresh"):
            resp = rig.client.post(f"/v1/oauth/{PROVIDER}/{path}", json={"mcp_name": MCP, "account_label": "alice"})
            assert resp.status_code == 503, (path, resp.text)

    def test_a_reconnect_through_another_kind_clears_the_installations(self, rig):
        _connect(rig)
        assert credential_store.get_account_meta(rig.sub, MCP, "alice")["installations"]
        resp = rig.client.post(f"/v1/oauth/{PROVIDER}/pat/save",
                               json={"mcp_name": MCP, "token": "ghp_x", "services": ["repos"], "account_label": ""})
        assert resp.status_code == 200, resp.text
        assert credential_store.get_account_meta(rig.sub, MCP, "alice") == {}
        _connect(rig)
        assert credential_store.get_account_meta(rig.sub, MCP, "alice")["installations"]


class TestInstallAndSetup:
    def test_the_install_page_and_the_setup_round_trip(self, rig):
        _connect(rig)
        resp = rig.client.post(f"/v1/oauth/{PROVIDER}/app/install-url",
                               json={"mcp_name": MCP, "account_label": "alice"})
        assert resp.status_code == 200, resp.text
        url = resp.json()["url"]
        assert url.startswith("https://github.com/apps/otodock-dev/installations/new?state=")
        state = _params(url)["state"]
        assert "set-cookie" in resp.headers
        rig.fake.installations = [_inst(10), _inst(12)]
        page = rig.client.get(f"/v1/oauth/{PROVIDER}/app/setup",
                              params={"installation_id": "777", "setup_action": "install", "state": state})
        assert page.status_code == 200 and "Installed on GitHub" in page.text
        assert "Account Connected" not in page.text and '"oauth-complete"' in page.text
        ids = [i["id"] for i in credential_store.get_account_meta(rig.sub, MCP, "alice")["installations"]]
        assert ids == [10, 12]
        # The state is spent: a second visit is the plain page.
        again = rig.client.get(f"/v1/oauth/{PROVIDER}/app/setup", params={"state": state})
        assert "click Refresh" in again.text and "Installed on GitHub" not in again.text

    def test_the_setup_page_without_a_usable_state(self, rig):
        _connect(rig)
        page = rig.client.get(f"/v1/oauth/{PROVIDER}/app/setup", params={"installation_id": "1"})
        assert page.status_code == 200 and "click Refresh" in page.text
        resp = rig.client.post(f"/v1/oauth/{PROVIDER}/app/install-url",
                               json={"mcp_name": MCP, "account_label": "alice"})
        state = _params(resp.json()["url"])["state"]
        other = TestClient(rig.client.app)   # another browser: no cookie
        page = other.get(f"/v1/oauth/{PROVIDER}/app/setup", params={"state": state})
        assert "click Refresh" in page.text
        assert oauth_engine.peek_state_identity(state) is None   # spent, listed nothing
        requested = rig.client.get(f"/v1/oauth/{PROVIDER}/app/setup", params={"setup_action": "request"})
        assert "owner must approve" in requested.text

    def test_the_install_page_is_for_the_callers_github_app_accounts(self, rig):
        _connect(rig)
        assert rig.client.post(f"/v1/oauth/{PROVIDER}/app/install-url",
                               json={"mcp_name": MCP, "account_label": "ghost"}).status_code == 404
        credential_store.set_user_credentials(rig.sub, MCP, {"GITHUB_EMAIL": "x"}, account_label="plain")
        assert rig.client.post(f"/v1/oauth/{PROVIDER}/app/install-url",
                               json={"mcp_name": MCP, "account_label": "plain"}).status_code == 400
        _mark(rig, kinds.KIND_OAUTH_APP)
        assert rig.client.post(f"/v1/oauth/{PROVIDER}/app/install-url",
                               json={"mcp_name": MCP, "account_label": "alice"}).status_code == 409


def _next_of(page: str) -> str:
    """The install page a connect's success page goes on to, ``""`` for none."""
    import html as html_mod
    import re
    m = re.search(r'data-next="([^"]*)"', page)
    return html_mod.unescape(m.group(1)) if m else ""


class TestOneStep:
    """Sign in and install in one window: a GitHub-App connect whose
    listing finds no installation goes on to GitHub's install page."""

    def test_a_connect_with_no_installation_goes_on_to_the_install_page(self, rig):
        rig.fake.installations = []
        q = _params(_start(rig).json()["url"])
        cb = rig.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"code": "c-1", "state": q["state"]})
        assert cb.status_code == 200 and "Account Connected" in cb.text
        assert "Next, choose where to install the app on GitHub." in cb.text
        assert '"oauth-complete"' in cb.text and "window.location.replace" in cb.text
        url = _next_of(cb.text)
        assert url.startswith("https://github.com/apps/otodock-dev/installations/new?state=")
        state = _params(url)["state"]
        assert "set-cookie" in cb.headers and oauth_engine.peek_state_install(state)
        # GitHub's return reaches the setup page in the same browser.
        rig.fake.installations = [_inst(12)]
        page = rig.client.get(f"/v1/oauth/{PROVIDER}/app/setup",
                              params={"installation_id": "12", "setup_action": "install", "state": state})
        assert "Installed on GitHub" in page.text
        assert [i["id"] for i in credential_store.get_account_meta(rig.sub, MCP, "alice")["installations"]] == [12]

    def test_a_connect_with_an_installation_or_a_failed_listing_closes_as_before(self, rig):
        q = _connect(rig)
        cb_state = q["state"]
        assert oauth_engine.peek_state_identity(cb_state) is None
        rig.fake.installations = [_inst(10)]
        q = _params(_start(rig).json()["url"])
        cb = rig.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"code": "c-1", "state": q["state"]})
        assert _next_of(cb.text) == "" and "This window will close automatically." in cb.text
        rig.fake.routes[("GET", "/user/installations")] = (500, {"message": "boom"})
        q = _params(_start(rig).json()["url"])
        cb = rig.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"code": "c-1", "state": q["state"]})
        assert "Account Connected" in cb.text and _next_of(cb.text) == ""

    def test_no_install_page_to_go_on_to_closes_as_before(self, rig):
        rig.fake.installations = []
        q = _params(_start(rig).json()["url"])
        _mark(rig, kinds.KIND_OAUTH_APP)          # the admin switched kinds mid-flow
        cb = rig.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"code": "c-1", "state": q["state"]})
        assert "Account Connected" in cb.text and _next_of(cb.text) == ""

    def test_an_install_pages_return_to_the_callback_is_never_exchanged(self, rig):
        """An admin's app with GitHub's "Request user authorization during
        installation" on: the card's install page comes back to the callback
        with a code, which is the person signed in to GitHub at that moment."""
        _connect(rig)
        exchanged = len(rig.fake.exchanges())
        state = _params(rig.client.post(f"/v1/oauth/{PROVIDER}/app/install-url",
                                        json={"mcp_name": MCP, "account_label": "alice"}).json()["url"])["state"]
        rig.fake.installations = [_inst(10), _inst(13)]
        page = rig.client.get(f"/v1/oauth/{PROVIDER}/callback", params={
            "code": "c-2", "state": state, "installation_id": "13", "setup_action": "install"})
        assert "Installed on GitHub" in page.text and len(rig.fake.exchanges()) == exchanged
        assert [i["id"] for i in credential_store.get_account_meta(rig.sub, MCP, "alice")["installations"]] == [10, 13]
        # The engine refuses such a state whatever route hands it over.
        st = oauth_engine.create_state(user_sub=rig.sub, mcp_name=MCP, provider_id=PROVIDER,
                                       account_label_hint="alice", install=True)
        with pytest.raises(RuntimeError, match="not a sign-in"):
            asyncio.run(oauth_engine.do_oauth_exchange(code="c-3", state_token=st))

    def test_a_return_with_no_state_reads_the_plain_notice(self, rig):
        page = rig.client.get(f"/v1/oauth/{PROVIDER}/callback",
                              params={"code": "c", "installation_id": "5", "setup_action": "update"})
        assert page.status_code == 200 and "click Refresh" in page.text
        page = rig.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"setup_action": "request"})
        assert "owner must approve" in page.text
        page = rig.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"code": "c"})
        assert "Missing code or state parameter" in page.text


class TestDisconnect:
    def test_the_revoke_uses_the_client_that_issued_the_token(self, rig):
        _connect(rig)
        _mark(rig, kinds.KIND_OAUTH_APP)        # the admin switched kinds since
        resp = rig.client.post(f"/v1/oauth/{PROVIDER}/disconnect", json={"mcp_name": MCP, "account_label": "alice"})
        assert resp.status_code == 200, resp.text
        revokes = [dict(httpx.QueryParams(r.content.decode())) for r in rig.fake.requests
                   if r.url.path == "/login/oauth/revoke"]
        assert revokes and revokes[-1]["client_id"] == "Iv1.cafe" and revokes[-1]["client_secret"] == "app-sec"


class TestRefresh:
    def test_refresh_relists_and_is_throttled(self, rig):
        _connect(rig)
        rig.fake.installations = [_inst(10), _inst(13)]
        resp = rig.client.post(f"/v1/oauth/{PROVIDER}/app/installations/refresh",
                               json={"mcp_name": MCP, "account_label": "alice"})
        assert resp.status_code == 200, resp.text
        assert [i["id"] for i in resp.json()["installations"]] == [10, 13]
        assert [i["id"] for i in credential_store.get_account_meta(rig.sub, MCP, "alice")["installations"]] == [10, 13]
        again = rig.client.post(f"/v1/oauth/{PROVIDER}/app/installations/refresh",
                                json={"mcp_name": MCP, "account_label": "alice"})
        assert again.status_code == 429

    def test_a_dead_user_token_says_reconnect(self, rig):
        _connect(rig)
        rig.fake.user_status = 401
        resp = rig.client.post(f"/v1/oauth/{PROVIDER}/app/installations/refresh",
                               json={"mcp_name": MCP, "account_label": "alice"})
        assert resp.status_code == 409 and "reconnect" in resp.json()["detail"]


class TestIntegrationsList:
    def test_the_account_shows_its_kind_and_installations(self, rig, monkeypatch):
        from storage import database as task_store
        monkeypatch.setattr(task_store, "get_user_agents", lambda sub: [])
        _connect(rig)
        resp = rig.client.get("/v1/users/me/integrations")
        assert resp.status_code == 200, resp.text
        # The list surfaces the MCPs of the person's agents; none here, so
        # the entry is read through the admin's view of every MCP.
        rig.holder["role"] = "admin"
        monkeypatch.setattr("storage.agents.agent_store.get_agent_slugs", lambda: ["a1"])
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda agent: list(mcp_registry._manifests.values()))
        resp = rig.client.get("/v1/users/me/integrations")
        assert resp.status_code == 200, resp.text
        entry = next(e for e in resp.json() if e["mcp_name"] == MCP)
        assert entry["oauth_meta"]["app_kind"] == kinds.KIND_GITHUB_APP
        assert entry["oauth_meta"]["app_install_available"] is True
        # The admin registered the GitHub App: that one browser way alone.
        assert entry["oauth_meta"]["connect_kinds"] == [kinds.KIND_GITHUB_APP]
        assert entry["oauth_meta"]["app_hosted"] is False
        acc = entry["accounts"][0]
        assert acc["kind"] == kinds.KIND_GITHUB_APP and [i["id"] for i in acc["installations"]] == [10]
        # The relay down (a hosted install): the page still answers.
        monkeypatch.setattr(github_apps, "app_in_use",
                            lambda mcp_name: (_ for _ in ()).throw(httpx.ReadTimeout("slow")))
        resp = rig.client.get("/v1/users/me/integrations")
        assert resp.status_code == 200, resp.text
        entry = next(e for e in resp.json() if e["mcp_name"] == MCP)
        assert entry["oauth_meta"]["app_kind"] == kinds.KIND_OAUTH_APP
        assert entry["oauth_meta"]["connect_kinds"] == [kinds.KIND_OAUTH_APP]
        assert entry["accounts"][0]["kind"] == kinds.KIND_GITHUB_APP
        # OtoDock's relay offering its GitHub App keeps its OAuth App beside it.
        for in_use, offered in (
            (github_apps.AppInUse(kind=kinds.KIND_GITHUB_APP, hosted=True, slug="otodock"),
             [kinds.KIND_GITHUB_APP, kinds.KIND_OAUTH_APP]),
            (github_apps.AppInUse(kind=kinds.KIND_OAUTH_APP, hosted=True), [kinds.KIND_OAUTH_APP]),
        ):
            monkeypatch.setattr(github_apps, "app_in_use", lambda mcp_name, found=in_use: found)
            entry = next(e for e in rig.client.get("/v1/users/me/integrations").json() if e["mcp_name"] == MCP)
            assert entry["oauth_meta"]["connect_kinds"] == offered
            assert entry["oauth_meta"]["app_kind"] == offered[0] and entry["oauth_meta"]["app_hosted"] is True


class TestAdminSave:
    def test_a_github_app_bundle_is_verified_and_the_slug_filled(self, rig, pem):
        rig.holder["role"] = "admin"
        one_line = " ".join(pem.split("\n"))
        resp = rig.client.put(f"/v1/admin/integrations/infra/{rig.bundle}", json={"credentials": {
            "GITHUB_APP_KIND": kinds.KIND_GITHUB_APP, "GITHUB_APP_ID": "4242",
            "GITHUB_APP_CLIENT_ID": "Iv1.new", "GITHUB_APP_PRIVATE_KEY": one_line,
        }})
        assert resp.status_code == 200, resp.text
        stored = credential_store.get_infra_credentials(rig.bundle)
        assert stored["GITHUB_APP_PRIVATE_KEY"] == pem and stored["GITHUB_APP_CLIENT_ID"] == "Iv1.new"
        assert stored[github_app.APP_SLUG_KEY] == "otodock-dev"
        assert stored["GITHUB_CLIENT_ID"] == "oauth-cid"       # the other kind's keys untouched

    def test_a_wrong_key_is_refused_and_nothing_stored(self, rig, pem):
        rig.holder["role"] = "admin"
        rig.fake.routes[("GET", "/app")] = (200, {"id": 9, "slug": "other", "name": "Other"})
        resp = rig.client.put(f"/v1/admin/integrations/infra/{rig.bundle}", json={"credentials": {
            "GITHUB_APP_KIND": kinds.KIND_GITHUB_APP, "GITHUB_APP_CLIENT_ID": "Iv1.changed",
        }})
        assert resp.status_code == 400 and "another app" in resp.json()["detail"]
        assert credential_store.get_infra_credentials(rig.bundle)["GITHUB_APP_CLIENT_ID"] == "Iv1.cafe"

    def test_a_rotated_key_marks_the_files_only_after_it_is_saved(self, rig, monkeypatch):
        rig.holder["role"] = "admin"
        order: list[str] = []
        real_save = credential_store.set_infra_credentials
        monkeypatch.setattr(credential_store, "set_infra_credentials",
                            lambda *a, **k: (order.append("save"), real_save(*a, **k))[1])
        monkeypatch.setattr(github_apps, "mark_installation_tokens_for_remint",
                            lambda provider_id: (order.append(f"mark:{provider_id}"), 0)[1])
        resp = rig.client.put(f"/v1/admin/integrations/infra/{rig.bundle}", json={"credentials": {
            "GITHUB_APP_KIND": kinds.KIND_GITHUB_APP, "GITHUB_APP_CLIENT_ID": "Iv1.rotated",
        }})
        assert resp.status_code == 200, resp.text
        assert order == ["save", f"mark:{PROVIDER}"]
        order.clear()
        resp = rig.client.put(f"/v1/admin/integrations/infra/{rig.bundle}", json={"credentials": {
            "GITHUB_APP_KIND": kinds.KIND_OAUTH_APP, "GITHUB_CLIENT_SECRET": "x",
        }})
        assert resp.status_code == 200 and order == ["save"]

    def test_the_oauth_app_kind_is_stored_as_posted(self, rig):
        rig.holder["role"] = "admin"
        resp = rig.client.put(f"/v1/admin/integrations/infra/{rig.bundle}", json={"credentials": {
            "GITHUB_APP_KIND": kinds.KIND_OAUTH_APP, "GITHUB_CLIENT_SECRET": "new-sec",
        }})
        assert resp.status_code == 200, resp.text
        assert credential_store.get_infra_credentials(rig.bundle)["GITHUB_CLIENT_SECRET"] == "new-sec"
        assert not any(r.url.path == "/app" for r in rig.fake.requests)


class FakeRelay:
    """OtoDock's relay with its GitHub App offered: every call recorded
    with its body; ``refuse`` maps a path to the code it answers with."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.refuse: dict[str, str] = {}
        self.kind = kinds.KIND_GITHUB_APP
        self.connect_kind = self.kind
        # A relay that answers another kind than the one asked.
        self.answer_kind = ""
        self.issued = 0
        # Another answer to the install page's address than GitHub's page.
        self.install_url: str | None = None

    def answer(self, path: str, payload: dict) -> dict:
        from services.billing import relay_client
        self.calls.append((path, dict(payload)))
        if path in self.refuse:
            raise relay_client.RelayError(self.refuse[path])
        if path == "/v1/oauth/app-info":
            return {"kind": self.kind, "app_id": "4242", "slug": "otodock", "name": "OtoDock"}
        if path == "/v1/oauth/authorize-url":
            # The relay's pick (`_connect_kind`): the kind asked, else the
            # GitHub App while offered and no scope asked, else the OAuth App.
            self.connect_kind = self.answer_kind or payload.get("app_kind") or (
                kinds.KIND_GITHUB_APP if self.kind == kinds.KIND_GITHUB_APP and not payload.get("scopes")
                else kinds.KIND_OAUTH_APP)
            return {"url": f"https://github.com/login/oauth/authorize?client_id=Iv1.otodock&state={payload['state']}"}
        if path == "/v1/oauth/exchange":
            self.issued += 1
            raw = {"access_token": f"ghu_{self.issued}", "refresh_token": f"ghr_{self.issued}",
                   "expires_in": 28800, "token_type": "bearer", "scope": "", "via_relay": True,
                   "app_kind": self.connect_kind}
            return {**{k: raw[k] for k in ("access_token", "refresh_token", "expires_in", "token_type", "scope")},
                    "raw": raw}
        if path == "/v1/oauth/app-install-url":
            if self.install_url is not None:
                return {"url": self.install_url}
            return {"url": f"https://github.com/apps/otodock/installations/new?state={payload['state']}"}
        if path == "/v1/events/register":
            return {"ok": True, "enabled": payload.get("enabled"),
                    "forward_secret": "fwd-secret" if payload.get("rotate_secret") else None}
        if path in ("/v1/oauth/app-relist", "/v1/oauth/revoke"):
            return {"ok": True}
        raise relay_client.RelayError("http_404")

    async def post(self, path: str, payload: dict) -> dict:
        return self.answer(path, payload)

    def bodies(self, path: str) -> list[dict]:
        return [body for p, body in self.calls if p == path]


@pytest.fixture
def hosted(rig, monkeypatch):
    """The rig behind OtoDock's hosted relay: the GitHub tool in hosted
    mode, the relay a fake behind the real routes over the same fake
    GitHub, the forward secret's bundle put back after."""
    from services.billing import relay_client
    relay = FakeRelay()
    monkeypatch.setattr(relay_client, "hosted_oauth_active", lambda *a, **k: True)
    monkeypatch.setattr(relay_client, "_require_relay", lambda: None)
    monkeypatch.setattr(relay_client, "_relay_identity", lambda: {"install_id": "inst-1", "license": ""})
    monkeypatch.setattr(relay_client, "_relay_post", relay.post)
    monkeypatch.setattr(relay_client, "_relay_post_sync", relay.answer)
    relay_client._app_info_cache.clear()
    slug = relay_client.EVENTS_FORWARD_SECRET_SLUG
    saved = credential_store.get_infra_credentials(slug) or {}
    credential_store.delete_infra_credentials(slug)
    rig.relay = relay
    yield rig
    relay_client._app_info_cache.clear()
    credential_store.delete_infra_credentials(slug)
    if saved:
        credential_store.set_infra_credentials(slug, saved)


def _forward_secret() -> str:
    from services.billing import relay_client
    held = credential_store.get_infra_credentials(relay_client.EVENTS_FORWARD_SECRET_SLUG) or {}
    return str(held.get(relay_client.EVENTS_FORWARD_SECRET_KEY) or "")


def _install_url(rig, label="alice"):
    return rig.client.post(f"/v1/oauth/{PROVIDER}/app/install-url", json={"mcp_name": MCP, "account_label": label})


class TestHostedConnect:
    def test_a_hosted_github_app_connect_registers_the_events_endpoint(self, hosted):
        _connect(hosted)
        start = hosted.relay.bodies("/v1/oauth/authorize-url")[-1]
        assert start["app_kind"] == kinds.KIND_GITHUB_APP and start["scopes"] == []
        raw = oauth_account_store.read_account_token(hosted.token_dir(), "alice")
        assert raw["extra"]["via_relay"] is True and raw["client_secret"] == ""
        meta = credential_store.get_account_meta(hosted.sub, MCP, "alice")
        assert meta["hosted"] is True and [i["id"] for i in meta["installations"]] == [10]
        assert credential_store.any_hosted_app_account([MCP]) is True
        registered = hosted.relay.bodies("/v1/events/register")
        assert len(registered) == 1 and registered[0]["provider_id"] == PROVIDER
        assert registered[0]["events_url"] == f"{DASH}/v1/webhooks/relay/{PROVIDER}"
        assert registered[0]["enabled"] is True and registered[0]["rotate_secret"] is True
        assert _forward_secret() == "fwd-secret"
        # A reconnect registers again, keeping the secret it holds.
        _connect(hosted)
        again = hosted.relay.bodies("/v1/events/register")
        assert len(again) == 2 and again[1]["rotate_secret"] is False
        assert credential_store.get_account_meta(hosted.sub, MCP, "alice")["hosted"] is True
        # The connect asked the relay for no re-list: it re-lists at the redeem itself.
        assert hosted.relay.bodies("/v1/oauth/app-relist") == []

    def test_a_failed_events_registration_keeps_the_connect(self, hosted):
        hosted.relay.refuse["/v1/events/register"] = "events_host_changed"
        _connect(hosted)
        meta = credential_store.get_account_meta(hosted.sub, MCP, "alice")
        assert meta["hosted"] is True and [i["id"] for i in meta["installations"]] == [10]
        assert _forward_secret() == ""

    def test_a_self_managed_connect_registers_nothing(self, rig, monkeypatch):
        from services.webhooks import subscription_manager
        asked: list[str] = []

        async def register(*, provider_id):
            asked.append(provider_id)
        monkeypatch.setattr(subscription_manager, "_relay_register_events", register)
        _connect(rig)
        meta = credential_store.get_account_meta(rig.sub, MCP, "alice")
        assert meta["kind"] == kinds.KIND_GITHUB_APP and meta["hosted"] is False
        assert asked == [] and credential_store.any_hosted_app_account([MCP]) is False

    def test_a_hosted_oauth_app_connect_names_no_kind_and_registers_nothing(self, hosted):
        hosted.relay.kind = kinds.KIND_OAUTH_APP
        _connect_with_services(hosted)
        start = hosted.relay.bodies("/v1/oauth/authorize-url")[-1]
        assert start["app_kind"] == "" and start["scopes"] == ["repo"]
        assert hosted.relay.bodies("/v1/events/register") == []
        assert credential_store.get_account_meta(hosted.sub, MCP, "alice") == {}

    def test_github_sign_in_picked_beside_the_app_lands_an_oauth_app_account(self, hosted):
        _connect_with_services(hosted, app_kind=kinds.KIND_OAUTH_APP)
        start = hosted.relay.bodies("/v1/oauth/authorize-url")[-1]
        assert start["app_kind"] == kinds.KIND_OAUTH_APP and start["scopes"] == ["repo"]
        raw = oauth_account_store.read_account_token(hosted.token_dir(), "alice")
        assert raw["extra"]["app_kind"] == kinds.KIND_OAUTH_APP and raw["scopes"] == ["repo"]
        assert credential_store.get_account_meta(hosted.sub, MCP, "alice") == {}
        assert hosted.relay.bodies("/v1/events/register") == []

    def test_the_picked_app_asks_no_scope_and_the_oauth_pick_needs_a_service(self, hosted):
        resp = _start(hosted, services=["repos"], app_kind=kinds.KIND_GITHUB_APP)
        assert resp.status_code == 200, resp.text
        start = hosted.relay.bodies("/v1/oauth/authorize-url")[-1]
        assert start["app_kind"] == kinds.KIND_GITHUB_APP and start["scopes"] == []
        resp = _start(hosted, app_kind=kinds.KIND_OAUTH_APP)
        assert resp.status_code == 400 and resp.json()["detail"] == "Tick at least one service."
        assert len(hosted.relay.bodies("/v1/oauth/authorize-url")) == 1

    def test_a_picked_kind_the_relay_lacks_reads_a_sentence(self, hosted):
        hosted.relay.refuse["/v1/oauth/authorize-url"] = "provider_disabled"
        resp = _start(hosted, services=["repos"], app_kind=kinds.KIND_OAUTH_APP)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "This way to sign in is not available right now. Pick another one."
        # A start with no pick keeps the sentence it always had.
        resp = _start(hosted, services=["repos"])
        assert resp.json()["detail"] == "OtoDock could not do this right now. Try again later."
        hosted.relay.refuse["/v1/oauth/authorize-url"] = "provider_not_app"
        resp = _start(hosted, app_kind=kinds.KIND_GITHUB_APP)
        assert resp.json()["detail"] == "OtoDock's GitHub App is not available right now. Try again in a minute."
        # A refusal that is not the kind's fault never says to pick another.
        hosted.relay.refuse["/v1/oauth/authorize-url"] = "bad_callback"
        resp = _start(hosted, app_kind=kinds.KIND_GITHUB_APP)
        assert resp.json()["detail"] == "OtoDock could not do this right now. Try again later."

    def test_an_app_the_relay_stopped_offering_leaves_the_list_at_once(self, hosted, monkeypatch):
        from services.billing import relay_client
        monkeypatch.setattr("storage.database.get_user_agents", lambda sub: [])
        hosted.holder["role"] = "admin"
        monkeypatch.setattr("storage.agents.agent_store.get_agent_slugs", lambda: ["a1"])
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda agent: list(mcp_registry._manifests.values()))

        def offered():
            entry = next(e for e in hosted.client.get("/v1/users/me/integrations").json() if e["mcp_name"] == MCP)
            return entry["oauth_meta"]["connect_kinds"]
        assert offered() == [kinds.KIND_GITHUB_APP, kinds.KIND_OAUTH_APP]
        # The relay turns its app off within the cached minute.
        hosted.relay.kind = kinds.KIND_OAUTH_APP
        assert offered() == [kinds.KIND_GITHUB_APP, kinds.KIND_OAUTH_APP]
        hosted.relay.refuse["/v1/oauth/authorize-url"] = "provider_not_app"
        assert _start(hosted, app_kind=kinds.KIND_GITHUB_APP).status_code == 403
        assert offered() == [kinds.KIND_OAUTH_APP]
        # Another refusal keeps the cached answer.
        relay_client._app_info_cache["github-other"] = ({"kind": kinds.KIND_GITHUB_APP}, 1e12)
        relay_client._forget_app_info("github-other", relay_client.RelayError("bad_callback"))
        assert "github-other" in relay_client._app_info_cache

    def test_the_callback_words_the_relays_codes(self, hosted):
        import html
        for code, words in (
            ("provider_disabled", "This way to sign in is not available right now. Try again later."),
            ("exchange_failed", "The sign-in did not finish. Try again."),
            ("no_code", "The sign-in did not finish. Try again."),
            ("access_denied", "The sign-in was cancelled. Connect again to retry."),
            ("temporarily_unavailable", "Provider returned: temporarily_unavailable"),
        ):
            page = hosted.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"error": code, "state": "s-x"})
            assert html.escape(words) in page.text, (code, page.text)
            assert "Provider returned: " + code not in page.text or code == "temporarily_unavailable"

    def test_the_account_follows_the_kind_the_relay_answers(self, hosted):
        hosted.relay.answer_kind = kinds.KIND_GITHUB_APP
        _connect_with_services(hosted, app_kind=kinds.KIND_OAUTH_APP)
        raw = oauth_account_store.read_account_token(hosted.token_dir(), "alice")
        assert raw["extra"]["app_kind"] == kinds.KIND_GITHUB_APP
        assert credential_store.get_account_meta(hosted.sub, MCP, "alice")["kind"] == kinds.KIND_GITHUB_APP

    def test_an_app_the_relay_stopped_offering_is_refused_with_a_sentence(self, hosted):
        hosted.relay.refuse["/v1/oauth/authorize-url"] = "provider_not_app"
        resp = _start(hosted)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "OtoDock's GitHub App is not available right now. Try again in a minute."


def _connect_with_services(rig, app_kind=None):
    resp = _start(rig, services=["repos"], app_kind=app_kind)
    assert resp.status_code == 200, resp.text
    q = _params(resp.json()["url"])
    cb = rig.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"code": "c-1", "state": q["state"]})
    assert cb.status_code == 200 and "Account Connected" in cb.text, cb.text


class TestHostedInstallPage:
    def test_a_hosted_connect_with_no_installation_goes_on_to_the_relays_install_page(self, hosted):
        hosted.fake.installations = []
        q = _params(_start(hosted).json()["url"])
        cb = hosted.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"code": "c-1", "state": q["state"]})
        asked = hosted.relay.bodies("/v1/oauth/app-install-url")
        assert len(asked) == 1 and asked[0]["install_callback"] == f"{DASH}/v1/oauth/{PROVIDER}/app/setup"
        assert _next_of(cb.text) == f"https://github.com/apps/otodock/installations/new?state={asked[0]['state']}"
        assert oauth_engine.peek_state_install(asked[0]["state"])
        # A relay that refuses the page: connected all the same, the card offers Install on GitHub.
        hosted.relay.refuse["/v1/oauth/app-install-url"] = "rate_limited"
        q = _params(_start(hosted, label="alice").json()["url"])
        cb = hosted.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"code": "c-2", "state": q["state"]})
        assert "Account Connected" in cb.text and _next_of(cb.text) == ""
        assert oauth_engine.peek_state_identity(hosted.relay.bodies("/v1/oauth/app-install-url")[-1]["state"]) is None

    def test_an_install_page_that_is_not_githubs_is_never_followed(self, hosted):
        """The onward page opens its address by itself on the dashboard's
        origin: an empty answer or another site closes as before, the state
        discarded."""
        hosted.fake.installations = []
        for n, answer in enumerate(("", "https://example.com/apps/otodock", "javascript:alert(1)",
                                    "https://github.com.example.com/x")):
            hosted.relay.install_url = answer
            q = _params(_start(hosted, label=f"a{n}").json()["url"])
            cb = hosted.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"code": f"c-{n}", "state": q["state"]})
            assert "Account Connected" in cb.text and _next_of(cb.text) == "", answer
            state = hosted.relay.bodies("/v1/oauth/app-install-url")[-1]["state"]
            assert oauth_engine.peek_state_identity(state) is None
        resp = _install_url(hosted, label="a1")
        assert resp.status_code == 503
        assert resp.json()["detail"] == "OtoDock could not get GitHub's install page. Try again later."

    def test_a_hosted_install_url_comes_from_the_relay(self, hosted):
        _connect(hosted)
        resp = _install_url(hosted)
        assert resp.status_code == 200, resp.text
        asked = hosted.relay.bodies("/v1/oauth/app-install-url")
        assert len(asked) == 1
        assert asked[0]["install_callback"] == f"{DASH}/v1/oauth/{PROVIDER}/app/setup"
        assert asked[0]["provider_id"] == PROVIDER and asked[0]["user_sub"] == hosted.sub
        assert resp.json()["url"] == f"https://github.com/apps/otodock/installations/new?state={asked[0]['state']}"
        assert "set-cookie" in resp.headers
        assert oauth_engine.peek_state_identity(asked[0]["state"]) is not None

    def test_a_relay_refusal_of_the_install_page_discards_the_state(self, hosted, monkeypatch):
        from services.billing import relay_client
        _connect(hosted)
        hosted.relay.refuse["/v1/oauth/app-install-url"] = "rate_limited"
        resp = _install_url(hosted)
        assert resp.status_code == 429
        assert resp.json()["detail"] == "GitHub's install page was asked for too often. Wait a minute and try again."
        state = hosted.relay.bodies("/v1/oauth/app-install-url")[-1]["state"]
        assert oauth_engine.peek_state_identity(state) is None
        hosted.relay.refuse["/v1/oauth/app-install-url"] = "state_reused"
        resp = _install_url(hosted)
        assert resp.status_code == 503
        assert resp.json()["detail"] == "OtoDock could not do this right now. Try again later."
        assert oauth_engine.peek_state_identity(hosted.relay.bodies("/v1/oauth/app-install-url")[-1]["state"]) is None
        del hosted.relay.refuse["/v1/oauth/app-install-url"]

        def down(path, payload):
            if path == "/v1/oauth/app-install-url":
                hosted.relay.calls.append((path, dict(payload)))
                raise httpx.ConnectError("down")
            return hosted.relay.answer(path, payload)
        monkeypatch.setattr(relay_client, "_relay_post_sync", down)
        resp = _install_url(hosted)
        assert resp.status_code == 503 and resp.json()["detail"] == "OtoDock could not be reached. Try again later."
        assert oauth_engine.peek_state_identity(hosted.relay.bodies("/v1/oauth/app-install-url")[-1]["state"]) is None

    def test_an_app_the_relay_no_longer_offers_has_no_install_page(self, hosted):
        _connect(hosted)
        from services.billing import relay_client
        relay_client._app_info_cache.clear()
        hosted.relay.kind = kinds.KIND_OAUTH_APP
        resp = _install_url(hosted)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "OtoDock's GitHub App is not available right now. Try again later."
        assert hosted.relay.bodies("/v1/oauth/app-install-url") == []

    def test_the_setup_page_and_refresh_relist_at_the_relay(self, hosted):
        _connect(hosted)
        _install_url(hosted)
        state = hosted.relay.bodies("/v1/oauth/app-install-url")[-1]["state"]
        hosted.fake.installations = [_inst(10), _inst(12)]
        page = hosted.client.get(f"/v1/oauth/{PROVIDER}/app/setup",
                                 params={"state": state, "setup_action": "install", "installation_id": "12"})
        assert page.status_code == 200 and "Installed on GitHub" in page.text
        assert [i["id"] for i in credential_store.get_account_meta(hosted.sub, MCP, "alice")["installations"]] == [10, 12]
        relists = hosted.relay.bodies("/v1/oauth/app-relist")
        assert [(b["provider_id"], b["access_token"]) for b in relists] == [(PROVIDER, "ghu_1")]
        resp = hosted.client.post(f"/v1/oauth/{PROVIDER}/app/installations/refresh",
                                  json={"mcp_name": MCP, "account_label": "alice"})
        assert resp.status_code == 200, resp.text
        assert [b["access_token"] for b in hosted.relay.bodies("/v1/oauth/app-relist")] == ["ghu_1", "ghu_1"]

    def test_the_hourly_recheck_does_not_relist_at_the_relay(self, hosted):
        _connect(hosted)
        github_apps._relisted.clear()
        meta = github_apps._reconfirm_owner(MCP, hosted.sub, "alice", "10")
        assert [i["id"] for i in meta["installations"]] == [10]
        github_apps.refresh_installations(hosted.sub, MCP, "alice")
        assert hosted.relay.bodies("/v1/oauth/app-relist") == []

    def test_a_relay_failure_of_the_relist_keeps_the_page(self, hosted):
        _connect(hosted)
        hosted.relay.refuse["/v1/oauth/app-relist"] = "token_invalid"
        _install_url(hosted)
        state = hosted.relay.bodies("/v1/oauth/app-install-url")[-1]["state"]
        page = hosted.client.get(f"/v1/oauth/{PROVIDER}/app/setup", params={"state": state})
        assert "Installed on GitHub" in page.text
        resp = hosted.client.post(f"/v1/oauth/{PROVIDER}/app/installations/refresh",
                                  json={"mcp_name": MCP, "account_label": "alice"})
        assert resp.status_code == 200 and [i["id"] for i in resp.json()["installations"]] == [10]
        assert len(hosted.relay.bodies("/v1/oauth/app-relist")) == 2


def _from_the_admins_own_app(rig, label: str, token: str = "ghu_adminapp") -> None:
    """Turn a connected account into one of the admin's own GitHub App, left
    from before the MCP turned hosted: its file never came through the
    relay."""
    path = oauth_account_store.account_token_path(rig.token_dir(), label)
    raw = oauth_account_store.read_account_token(rig.token_dir(), label)
    extra = {k: v for k, v in raw["extra"].items() if k != "via_relay"}
    oauth_account_store.write_token_payload(path, {**raw, "access_token": token, "extra": extra})
    credential_store.update_account_meta(rig.sub, MCP, label, lambda meta: {**meta, "hosted": False})


class TestOnlyTheRelaysTokensReachIt:
    def test_an_account_of_the_admins_own_app_never_relists_at_the_relay(self, hosted):
        _connect(hosted, label="old")              # GitHub user 1, ghu_1
        _from_the_admins_own_app(hosted, "old")
        _connect(hosted)                           # alice: the same person through OtoDock's app, ghu_2

        def sent() -> list[str]:
            return [b["access_token"] for b in hosted.relay.bodies("/v1/oauth/app-relist")]
        # Refresh on each card: only the hosted account's token goes.
        for label in ("old", "alice"):
            resp = hosted.client.post(f"/v1/oauth/{PROVIDER}/app/installations/refresh",
                                      json={"mcp_name": MCP, "account_label": label})
            assert resp.status_code == 200, resp.text
        assert sent() == ["ghu_2"]
        # The setup page of the old card lists it and sends nothing.
        _install_url(hosted, "old")
        state = hosted.relay.bodies("/v1/oauth/app-install-url")[-1]["state"]
        page = hosted.client.get(f"/v1/oauth/{PROVIDER}/app/setup", params={"state": state})
        assert "Installed on GitHub" in page.text
        assert sent() == ["ghu_2"]
        # A disconnect of the hosted account re-lists the person's others:
        # the old account is one of them, and its token stays here.
        resp = hosted.client.post(f"/v1/oauth/{PROVIDER}/disconnect", json={"mcp_name": MCP, "account_label": "alice"})
        assert resp.status_code == 200, resp.text
        assert sent() == ["ghu_2"]
        assert "ghu_adminapp" not in json.dumps(hosted.relay.calls)


class TestHostedDisconnect:
    def test_a_hosted_disconnect_relists_the_persons_other_accounts(self, hosted):
        _connect(hosted)                       # alice: GitHub user 1, ghu_1
        _connect(hosted, label="work")         # the same GitHub person, ghu_2
        assert credential_store.get_account_meta(hosted.sub, MCP, "work")["user_id"] == 1
        resp = hosted.client.post(f"/v1/oauth/{PROVIDER}/disconnect", json={"mcp_name": MCP, "account_label": "alice"})
        assert resp.status_code == 200, resp.text
        assert [b["token"] for b in hosted.relay.bodies("/v1/oauth/revoke")] == ["ghr_1"]
        assert [b["access_token"] for b in hosted.relay.bodies("/v1/oauth/app-relist")] == ["ghu_2"]
        # The relay's revoke came first: the re-list binds the person back.
        paths = [p for p, _ in hosted.relay.calls]
        assert paths.index("/v1/oauth/revoke") < paths.index("/v1/oauth/app-relist")
        # The last account of that person: nothing left to re-list.
        resp = hosted.client.post(f"/v1/oauth/{PROVIDER}/disconnect", json={"mcp_name": MCP, "account_label": "work"})
        assert resp.status_code == 200 and len(hosted.relay.bodies("/v1/oauth/app-relist")) == 1

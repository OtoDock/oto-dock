"""A connect through the client this install registered at the MCP server's
own authorization server: the start route (the allowlist gate, the consent
URL), the callback (the exchange body, the state's registration binding,
the ``iss`` checks, the token file, the identity), the disconnect (the
revocation, the deletion), the persist-failure revocation, and what the
resolver, the bearer injector and the webhook token resolver make of such a
file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from auth.providers import UserContext, get_current_user
from services.mcp import mcp_manifest_parse, mcp_registry
from services.oauth import credential_resolver, mcp_authorization as ma, oauth_account_store
from storage.identity import bearer_allowlist, credential_store
from storage.identity import oauth_client_registrations as regs
from storage.pg import get_conn
from tests.auth.test_mcp_authorization_discovery import AS_META, ISSUER, PRM_PATH, RESOURCE, Vendor

MCP = "notion-hosted-mcp"
PROVIDER = "notion-hosted"
DASH = "https://dash.example"
CB = f"{DASH}/v1/oauth/{PROVIDER}/callback"
TOKEN = f"{ISSUER}/token"
REGISTER = f"{ISSUER}/register"


def _manifest_data(userinfo_url=""):
    oauth = {
        "provider_id": PROVIDER,
        "flows": ["authorization_code_pkce"],
        "authorization_server": {
            "registration": "dynamic", "confidential": False,
            "identity": {"label_field": "workspace_id", "display_field": "email_domain",
                         "id_field": "user_id"},
        },
        "bearer_required": True,
        "proposed_hosts": ["mcp.example.com"],
        "services": [
            {"key": "read", "label": "Read", "description": "d", "scopes": ["read"]},
            {"key": "write", "label": "Write", "description": "d", "scopes": ["write"]},
        ],
    }
    if userinfo_url:
        oauth["userinfo_url"] = userinfo_url
        oauth["userinfo_email_field"] = "email"
    return {
        "name": MCP, "label": "Notion (hosted)", "description": "d", "version": "1.0.0",
        "category": "community",
        "server": {"transport": "streamable_http", "url_template": RESOURCE,
                   "source": "remote:mcp.example.com"},
        "credentials": {"type": "per_user", "label": "Notion Account", "oauth": oauth},
    }


@pytest.fixture
def person():
    sub = f"test-user-{uuid.uuid4().hex[:12]}"
    username = f"u{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, username, role, auth_provider, "
            "created_at, last_login) VALUES (%s, %s, 'Test', %s, 'creator', 'local', "
            "NOW()::text, NOW()::text)",
            (sub, f"{username}@test.example", username),
        )
        conn.commit()
    yield sub, username
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE sub = %s", (sub,))
        conn.commit()


@pytest.fixture
def rig(monkeypatch, tmp_path, person):
    """The manifest in the registry, the admin's allowlist row, the dashboard
    URL, a fake vendor and a signed-in person on the oauth router."""
    sub, username = person
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", DASH)
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(mcp_registry, "_manifests", {})
    from auth import oauth_providers
    oauth_providers.clear_manifest_cache()
    ma.clear_caches()
    vendor = Vendor()
    monkeypatch.setattr(ma, "_client", vendor.client)
    monkeypatch.setattr(ma, "_resolve_addresses", lambda host: ["8.8.8.8"])
    vendor.routes[PRM_PATH] = (200, {"resource": RESOURCE, "authorization_servers": [ISSUER]}, {})
    vendor.routes[AS_META] = (200, {
        "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize?tenant=t",
        "token_endpoint": TOKEN, "registration_endpoint": REGISTER,
        "revocation_endpoint": TOKEN, "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "authorization_response_iss_parameter_supported": True,
    }, {})
    posted: dict[str, list] = {"register": [], "token": [], "revoke": []}

    def register(request):
        body = json.loads(request.content)
        posted["register"].append(body)
        return httpx.Response(201, json={"client_id": "cid-live", "redirect_uris": body["redirect_uris"],
                                         "token_endpoint_auth_method": "none",
                                         "grant_types": body["grant_types"]})

    def token(request):
        form = dict(httpx.QueryParams(request.content.decode()))
        if form.get("grant_type") == "authorization_code":
            posted["token"].append(form)
            return httpx.Response(200, json={
                "access_token": "AT-1", "refresh_token": "RT-1", "expires_in": 3600,
                "workspace_id": "ws-1", "email_domain": "acme.com", "user_id": "u-1",
            })
        posted["revoke"].append(form)
        return httpx.Response(200, text="")
    vendor.routes[("POST", REGISTER)] = register
    vendor.routes[("POST", TOKEN)] = token

    def install(data):
        path = tmp_path / data["name"] / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        m = mcp_manifest_parse._parse_manifest(path)
        mcp_registry._manifests[m.name] = m
        oauth_providers.clear_manifest_cache()
        return m
    manifest = install(_manifest_data())
    bearer_allowlist.add_allowed(PROVIDER, "mcp.example.com", "test")

    from api.auth import oauth as oauth_api
    user = UserContext(sub=sub, email=f"{username}@test.example", name="T", role="creator",
                       agents=[], agent_roles={})

    async def _stub():
        return user
    app = FastAPI()
    app.include_router(oauth_api.router)
    app.dependency_overrides[get_current_user] = _stub
    client = TestClient(app)

    class Rig:
        pass
    r = Rig()
    r.vendor, r.posted, r.client, r.sub, r.username = vendor, posted, client, sub, username
    r.manifest, r.install, r.tmp_path = manifest, install, tmp_path
    r.token_dir = lambda: oauth_account_store.get_token_dir(username, provider_id=PROVIDER)
    yield r
    ma.clear_caches()
    oauth_providers.clear_manifest_cache()
    with get_conn() as conn:
        conn.execute("DELETE FROM oauth_bearer_allowlist WHERE provider_id = %s", (PROVIDER,))
        conn.commit()


def _app_shape(data):
    """The manifest with an admin app's fields and URLs beside the block."""
    o = data["credentials"]["oauth"]
    o["app_credential"] = "notion-hosted-app"
    o["app_credential_fields"] = [
        {"key": "CLIENT_ID", "label": "id", "input_type": "text"},
        {"key": "CLIENT_SECRET", "label": "s", "input_type": "password"}]
    o["authorization_url"] = f"{ISSUER}/authorize"
    o["token_url"] = TOKEN
    return data


def _start(rig, services=("read", "write"), label=""):
    return rig.client.post(f"/v1/oauth/{PROVIDER}/start",
                           json={"mcp_name": MCP, "services": list(services), "account_label": label})


def _params(url):
    return dict(httpx.QueryParams(url.split("?", 1)[1]))


class TestStart:
    def test_allowlist_gate_runs_before_any_vendor_call(self, rig):
        with get_conn() as conn:
            conn.execute("DELETE FROM oauth_bearer_allowlist WHERE provider_id = %s", (PROVIDER,))
            conn.commit()
        resp = _start(rig)
        assert resp.status_code == 400
        assert "An admin must allow mcp.example.com for notion-hosted" in resp.json()["detail"]
        assert rig.vendor.requests == []

    def test_consent_url_and_one_registration(self, rig):
        resp = _start(rig)
        assert resp.status_code == 200, resp.text
        q = _params(resp.json()["url"])
        assert resp.json()["url"].startswith(f"{ISSUER}/authorize?")
        assert q["tenant"] == "t"
        assert q["client_id"] == "cid-live"
        assert q["redirect_uri"] == CB
        assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) >= 43
        assert q["resource"] == RESOURCE and q["scope"] == "read write"
        assert q["response_type"] == "code" and q["state"]
        assert not {"include_granted_scopes", "prompt", "owner"} & set(q)
        assert len(rig.posted["register"]) == 1
        sent = rig.posted["register"][0]
        assert sent["redirect_uris"] == [CB] and sent["scope"] == "read write"
        again = _start(rig, services=("read",))
        assert _params(again.json()["url"])["scope"] == "read"
        assert len(rig.posted["register"]) == 1
        assert len(regs.list_all()) == 1

    def test_a_public_client_handed_a_secret_registers_once(self, rig):
        """A vendor that answers a public request with a secret: the row is
        judged against what was requested (public), so it is reused, not
        superseded as a changed client type at every connect."""
        def register(request):
            body = json.loads(request.content)
            rig.posted["register"].append(body)
            return httpx.Response(201, json={"client_id": "cid-sec", "client_secret": "handed",
                                             "redirect_uris": body["redirect_uris"],
                                             "token_endpoint_auth_method": "client_secret_post"})
        rig.vendor.routes[("POST", REGISTER)] = register
        assert _start(rig).status_code == 200
        assert _start(rig).status_code == 200
        assert len(rig.posted["register"]) == 1
        rows = regs.list_all()
        assert len(rows) == 1 and not rows[0]["revoked_at"]
        assert rows[0]["has_secret"] and rows[0]["requested_auth_method"] == "none"

    def test_the_registration_records_the_mcp_it_was_made_for(self, rig):
        """The row names the MCP server URL it serves, so the admin card
        finds it after a restart (the discovery cache is in memory)."""
        assert _start(rig).status_code == 200
        ma.clear_caches()
        row = regs.list_all()[0]
        assert row["resources"].split() == [RESOURCE]
        from api.mcp import credentials as cred_api
        assert cred_api._registration_mcps("https://auth.elsewhere.example",
                                           row["resources"]) == [MCP]

    def test_no_registration_endpoint(self, rig):
        meta = dict(rig.vendor.routes[AS_META][1])
        meta.pop("registration_endpoint")
        rig.vendor.routes[AS_META] = (200, meta, {})
        resp = _start(rig)
        assert resp.status_code == 400
        assert "does not register clients" in resp.json()["detail"]

    def test_vendor_refusal_reaches_the_card(self, rig):
        rig.vendor.routes[("POST", REGISTER)] = (400, {"error": "invalid_redirect_uri",
                                                       "error_description": "Redirect URI must use HTTPS"}, {})
        resp = _start(rig)
        assert resp.status_code == 400
        assert "invalid_redirect_uri: Redirect URI must use HTTPS" in resp.json()["detail"]

    def test_app_credentials_take_the_classic_path_when_the_server_accepts_app_tokens(self, rig, monkeypatch):
        """A server that takes the vendor's app tokens (Linear) keeps the
        app flow once an admin configures an app; the registered client is
        the fallback."""
        data = _app_shape(_manifest_data())
        data["credentials"]["oauth"]["authorization_server"]["accepts_app_tokens"] = True
        m = rig.install(data)
        # no app configured: the registered client
        assert ma.registered_client_mode(m) is True
        assert _params(_start(rig).json()["url"])["client_id"] == "cid-live"
        credential_store.set_infra_credentials("notion-hosted-app", {"CLIENT_ID": "app", "CLIENT_SECRET": "s"})
        ma.clear_caches()
        assert ma.registered_client_mode(m) is False
        resp = _start(rig)
        assert resp.status_code == 200
        q = _params(resp.json()["url"])
        assert q["client_id"] == "app" and "resource" not in q
        assert len(rig.posted["register"]) == 1

    def test_hosted_mode_signs_in_through_the_relay_only_when_the_server_accepts_app_tokens(self, rig, monkeypatch):
        from services.billing import relay_client
        monkeypatch.setattr(relay_client, "hosted_oauth_active", lambda *a, **k: True)

        async def _relay_url(**kw):
            return "https://relay.example/consent"
        monkeypatch.setattr(relay_client, "oauth_authorize_url", _relay_url)
        # the default shape: the server takes its own tokens only
        m = rig.manifest
        assert ma.registered_client_mode(m) is True
        assert _params(_start(rig).json()["url"])["client_id"] == "cid-live"
        data = _manifest_data()
        data["credentials"]["oauth"]["authorization_server"]["accepts_app_tokens"] = True
        m = rig.install(data)
        ma.clear_caches()
        assert ma.registered_client_mode(m) is False
        assert _start(rig).json()["url"] == "https://relay.example/consent"

    def test_an_admin_app_changes_nothing_when_the_server_takes_its_own_tokens_only(self, rig):
        """Notion's shape: the app or the relay may serve events later, the
        sign-in stays the registered client."""
        m = rig.install(_app_shape(_manifest_data()))
        credential_store.set_infra_credentials("notion-hosted-app", {"CLIENT_ID": "app", "CLIENT_SECRET": "s"})
        assert ma.registered_client_mode(m) is True
        q = _params(_start(rig).json()["url"])
        assert q["client_id"] == "cid-live" and q["resource"] == RESOURCE


class TestCallback:
    def _consent(self, rig, **kw):
        resp = _start(rig, **kw)
        assert resp.status_code == 200, resp.text
        return _params(resp.json()["url"])

    def test_round_trip_writes_the_token_file(self, rig):
        q = self._consent(rig)
        resp = rig.client.get(f"/v1/oauth/{PROVIDER}/callback",
                              params={"code": "c-1", "state": q["state"], "iss": ISSUER})
        assert resp.status_code == 200 and "Account Connected" in resp.text
        assert "acme.com" in resp.text
        form = rig.posted["token"][0]
        verifier = form["code_verifier"]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        assert challenge == q["code_challenge"]
        assert form["redirect_uri"] == CB and form["resource"] == RESOURCE
        assert form["client_id"] == "cid-live" and "client_secret" not in form
        raw = json.loads((rig.token_dir() / "ws-1.json").read_text())
        assert raw["access_token"] == "AT-1" and raw["refresh_token"] == "RT-1"
        assert raw["client_id"] == "cid-live" and raw["client_secret"] == ""
        assert raw["token_url"] == TOKEN and raw["scopes"] == ["read", "write"]
        assert raw["extra"]["flow"] == "mcp_authorization"
        assert raw["extra"]["issuer"] == ISSUER and raw["extra"]["resource"] == RESOURCE
        assert raw["extra"]["registration_id"] == regs.list_all()[0]["id"]
        assert raw["extra"]["revocation_endpoint"] == TOKEN
        assert raw["extra"]["workspace_id"] == "ws-1"
        assert "refresh_token" not in raw["extra"] and "expires_in" not in raw["extra"]
        accounts = credential_store.list_user_accounts(rig.sub, MCP)
        assert [(a["account_label"], a["display_email"]) for a in accounts] == [("ws-1", "acme.com")]
        # the state was consumed: a replay of the same callback is refused
        again = rig.client.get(f"/v1/oauth/{PROVIDER}/callback",
                               params={"code": "c-1", "state": q["state"], "iss": ISSUER})
        assert "Token exchange failed" in again.text and len(rig.posted["token"]) == 1

    def test_typed_label_wins(self, rig):
        q = self._consent(rig, label="Work")
        rig.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"code": "c", "state": q["state"], "iss": ISSUER})
        assert (rig.token_dir() / "Work.json").exists()

    def test_iss_mismatch_refused(self, rig):
        q = self._consent(rig)
        resp = rig.client.get(f"/v1/oauth/{PROVIDER}/callback",
                              params={"code": "c", "state": q["state"], "iss": "https://evil.example"})
        assert "Token exchange failed" in resp.text
        assert rig.posted["token"] == []

    def test_iss_required_when_advertised(self, rig):
        q = self._consent(rig)
        resp = rig.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"code": "c", "state": q["state"]})
        assert "Token exchange failed" in resp.text and rig.posted["token"] == []

    def test_iss_optional_when_not_advertised(self, rig):
        meta = dict(rig.vendor.routes[AS_META][1])
        meta.pop("authorization_response_iss_parameter_supported")
        rig.vendor.routes[AS_META] = (200, meta, {})
        q = self._consent(rig)
        resp = rig.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"code": "c", "state": q["state"]})
        assert "Account Connected" in resp.text

    def test_error_response_iss_is_checked_without_burning_the_state(self, rig):
        q = self._consent(rig)
        resp = rig.client.get(f"/v1/oauth/{PROVIDER}/callback",
                              params={"error": "access_denied", "state": q["state"], "iss": "https://evil.example"})
        assert "names another issuer" in resp.text
        ok = rig.client.get(f"/v1/oauth/{PROVIDER}/callback",
                            params={"code": "c", "state": q["state"], "iss": ISSUER})
        assert "Account Connected" in ok.text

    def test_an_error_response_without_iss_is_refused_when_advertised(self, rig):
        q = self._consent(rig)
        resp = rig.client.get(f"/v1/oauth/{PROVIDER}/callback",
                              params={"error": "access_denied", "state": q["state"]})
        assert "carries no issuer" in resp.text
        ok = rig.client.get(f"/v1/oauth/{PROVIDER}/callback",
                            params={"code": "c", "state": q["state"], "iss": ISSUER})
        assert "Account Connected" in ok.text

    def test_the_mobile_exchange_carries_iss_and_reflects_no_vendor_text(self, rig):
        q = self._consent(rig)
        resp = rig.client.post(f"/v1/oauth/{PROVIDER}/exchange",
                               json={"code": "c", "state": q["state"], "iss": "https://evil.example"})
        assert resp.status_code == 400 and resp.json()["detail"] == "Token exchange failed (RuntimeError)"
        assert rig.posted["token"] == []
        q = self._consent(rig)
        rig.vendor.routes[("POST", TOKEN)] = (400, {"error": "invalid_grant", "error_description": "secret vendor words"}, {})
        resp = rig.client.post(f"/v1/oauth/{PROVIDER}/exchange",
                               json={"code": "c", "state": q["state"], "iss": ISSUER})
        assert resp.status_code == 400 and "secret vendor words" not in resp.text
        assert resp.json()["detail"] == "Token exchange failed (OAuthTokenError)"

    def test_an_unusable_identity_label_revokes_the_grant(self, rig):
        def token(request):
            form = dict(httpx.QueryParams(request.content.decode()))
            if form.get("grant_type") == "authorization_code":
                return httpx.Response(200, json={"access_token": "AT-1", "refresh_token": "RT-1",
                                                 "expires_in": 3600, "workspace_id": "../ws"})
            rig.posted["revoke"].append(form)
            return httpx.Response(200)
        rig.vendor.routes[("POST", TOKEN)] = token
        q = self._consent(rig)
        resp = rig.client.get(f"/v1/oauth/{PROVIDER}/callback",
                              params={"code": "c", "state": q["state"], "iss": ISSUER})
        assert "Token exchange failed" in resp.text
        assert rig.posted["revoke"] and rig.posted["revoke"][0]["token"] == "RT-1"
        assert not any(rig.token_dir().iterdir())

    def test_a_forgotten_registration_refuses_the_callback(self, rig):
        q = self._consent(rig)
        regs.revoke(regs.list_all()[0]["id"], "forgotten")
        resp = rig.client.get(f"/v1/oauth/{PROVIDER}/callback",
                              params={"code": "c", "state": q["state"], "iss": ISSUER})
        assert "Token exchange failed" in resp.text and rig.posted["token"] == []

    def test_a_replaced_registration_refuses_the_callback(self, rig):
        q = self._consent(rig)
        row = regs.list_all()[0]
        with get_conn() as conn:
            conn.execute("UPDATE oauth_client_registrations SET client_id = 'cid-other' WHERE id = %s",
                         (row["id"],))
            conn.commit()
        resp = rig.client.get(f"/v1/oauth/{PROVIDER}/callback",
                              params={"code": "c", "state": q["state"], "iss": ISSUER})
        assert "Token exchange failed" in resp.text and rig.posted["token"] == []

    def test_invalid_client_revokes_the_registration(self, rig):
        q = self._consent(rig)
        rig.vendor.routes[("POST", TOKEN)] = (401, {"error": "invalid_client"}, {})
        resp = rig.client.get(f"/v1/oauth/{PROVIDER}/callback",
                              params={"code": "c", "state": q["state"], "iss": ISSUER})
        assert "Token exchange failed" in resp.text
        assert regs.list_all()[0]["revoked_reason"] == "vendor_revoked"
        # the next start registers afresh
        _start(rig)
        assert len(rig.posted["register"]) == 2

    @pytest.mark.parametrize("userinfo_url", ["https://api.example.com/v1/users/me"])
    def test_userinfo_off_the_resource_host_is_never_called(self, rig, monkeypatch, userinfo_url):
        """An http userinfo_url never installs (the validator refuses it);
        one on another host installs and is never probed."""
        rig.install(_manifest_data(userinfo_url=userinfo_url))
        rig.vendor.routes[("GET", userinfo_url)] = (200, {"email": "probe@acme.com", "sub": "u-1"}, {})
        real = httpx.AsyncClient

        def through_the_vendor(*a, **kw):
            kw["transport"] = httpx.MockTransport(rig.vendor.handler)
            return real(*a, **kw)
        monkeypatch.setattr(httpx, "AsyncClient", through_the_vendor)
        q = self._consent(rig)
        rig.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"code": "c", "state": q["state"], "iss": ISSUER})
        assert not any(str(r.url) == userinfo_url for r in rig.vendor.requests)
        assert (rig.token_dir() / "ws-1.json").exists()
        assert not (rig.token_dir() / "probe@acme.com.json").exists()

    def test_userinfo_on_the_resource_host_names_the_account(self, rig, monkeypatch):
        rig.install(_manifest_data(userinfo_url=f"{ISSUER}/me"))
        rig.vendor.routes[("GET", f"{ISSUER}/me")] = (200, {"email": "alice@acme.com", "sub": "u-1"}, {})
        real = httpx.AsyncClient

        def through_the_vendor(*a, **kw):
            kw["transport"] = httpx.MockTransport(rig.vendor.handler)
            return real(*a, **kw)
        monkeypatch.setattr(httpx, "AsyncClient", through_the_vendor)
        q = self._consent(rig)
        rig.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"code": "c", "state": q["state"], "iss": ISSUER})
        probe = [r for r in rig.vendor.requests if str(r.url) == f"{ISSUER}/me"]
        assert probe and probe[0].headers["authorization"] == "Bearer AT-1"
        assert (rig.token_dir() / "alice@acme.com.json").exists()

    def test_persist_failure_revokes_the_grant(self, rig):
        q = self._consent(rig)
        with patch("services.oauth.oauth_account_store.persist_oauth_account", side_effect=OSError("disk")):
            resp = rig.client.get(f"/v1/oauth/{PROVIDER}/callback",
                                  params={"code": "c", "state": q["state"], "iss": ISSUER})
        assert "Token exchange failed" in resp.text
        assert rig.posted["revoke"] and rig.posted["revoke"][0]["token"] == "RT-1"
        assert rig.posted["revoke"][0]["client_id"] == "cid-live"


class TestDisconnect:
    def test_revokes_at_the_issuer_and_deletes(self, rig):
        q = _params(_start(rig).json()["url"])
        rig.client.get(f"/v1/oauth/{PROVIDER}/callback", params={"code": "c", "state": q["state"], "iss": ISSUER})
        resp = rig.client.post(f"/v1/oauth/{PROVIDER}/disconnect", json={"mcp_name": MCP, "account_label": "ws-1"})
        assert resp.status_code == 200
        assert rig.posted["revoke"][0]["token"] == "RT-1" and rig.posted["revoke"][0]["client_id"] == "cid-live"
        assert not (rig.token_dir() / "ws-1.json").exists()
        assert credential_store.list_user_accounts(rig.sub, MCP) == []

    def test_bad_label_is_a_400(self, rig):
        resp = rig.client.post(f"/v1/oauth/{PROVIDER}/disconnect", json={"mcp_name": MCP, "account_label": "../x"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# The file's verdict and the token-origin invariant
# ---------------------------------------------------------------------------

def _file(expires_in=3600, refresh="RT", extra=None, **over):
    body = {
        "provider": PROVIDER, "account_id": "a", "access_token": "AT",
        "refresh_token": refresh, "token_url": TOKEN, "client_id": "cid", "client_secret": "",
        "scopes": ["read"], "extra": extra or {},
    }
    if expires_in is None:
        body["expires_at"] = ""
    else:
        body["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body.update(over)
    return body


class TestDeadReason:
    def test_expired_without_refresh_is_expired(self):
        assert oauth_account_store.token_dead_reason(_file(expires_in=-60, refresh="")) == "expired"

    def test_live_refresh_or_never_expiring_or_no_refresh_flows_are_fine(self):
        assert oauth_account_store.token_dead_reason(_file(expires_in=-60, refresh="RT")) == ""
        assert oauth_account_store.token_dead_reason(_file(expires_in=None, refresh="")) == ""
        assert oauth_account_store.token_dead_reason(_file(expires_in=-60, refresh="", extra={"flow": "client_credentials"})) == ""
        assert oauth_account_store.token_dead_reason(_file(expires_in=-60, refresh="", extra={"flow": "personal_access_token"})) == ""
        assert oauth_account_store.token_dead_reason(_file(expires_in=60, refresh="")) == ""

    def test_a_recorded_permanent_failure_is_revoked(self):
        assert oauth_account_store.token_dead_reason(_file(extra={"refresh_failed": "invalid_grant"})) == "revoked"
        assert oauth_account_store.token_dead_reason(_file(extra={"refresh_failed": "mechanism_changed"})) == "mechanism_changed"


class TestTokenOrigin:
    def test_registered_client_mode_needs_the_flow_and_the_resource(self, rig):
        m = rig.manifest
        assert ma.token_origin_problem(_file(extra={"flow": "mcp_authorization", "resource": RESOURCE}), m) == ""
        # the resource the server's metadata named: the origin (a root document),
        # a prefix, or the URL without the declared trailing slash
        assert ma.token_origin_problem(_file(extra={"flow": "mcp_authorization", "resource": ISSUER}), m) == ""
        assert ma.token_origin_problem(_file(extra={"flow": "mcp_authorization", "resource": RESOURCE + "/"}), m) == ""
        assert ma.token_origin_problem(_file(extra={"flow": "mcp_authorization", "resource": ""}), m) == "mechanism_changed"
        assert ma.token_origin_problem(_file(extra={}), m) == "mechanism_changed"
        assert ma.token_origin_problem(_file(extra={"via_relay": True}), m) == "mechanism_changed"
        assert ma.token_origin_problem(_file(extra={"flow": "mcp_authorization", "resource": "https://other/mcp"}), m) == "mechanism_changed"

    def test_a_manifest_without_the_block_has_no_invariant(self, rig):
        data = _manifest_data()
        o = data["credentials"]["oauth"]
        o.pop("authorization_server")
        o.update(flows=["authorization_code"], authorization_url=f"{ISSUER}/a", token_url=TOKEN)
        m = rig.install(data)
        assert ma.token_origin_problem(_file(extra={}), m) == ""

    def test_the_resolver_leaves_the_mcp_out_with_the_reason(self, rig, monkeypatch):
        token_dir = rig.token_dir()
        (token_dir / "ws-1.json").write_text(json.dumps(_file(extra={"flow": "mcp_authorization", "resource": RESOURCE},
                                                               expires_in=-60, refresh="")))
        monkeypatch.setattr(credential_resolver, "pick_account",
                            lambda mcp, agent, *, user_sub="": credential_resolver.AccountRef("ws-1", user_sub))
        monkeypatch.setattr(mcp_registry, "get_agent_mcps_all_placements", lambda agent: [rig.manifest])
        out = credential_resolver.resolve_credentials("pa", rig.sub, task_scope="user")
        assert MCP in out.excluded_mcps
        assert out.exclusion_reasons[MCP] == (
            "Notion Account: the account's access expired and the vendor issued no refresh token; "
            "reconnect it in Settings > Integrations."
        )
        (token_dir / "ws-1.json").write_text(json.dumps(_file(extra={"flow": "mcp_authorization", "resource": RESOURCE})))
        out = credential_resolver.resolve_credentials("pa", rig.sub, task_scope="user")
        assert MCP in out.available_mcps

    def test_the_gateway_source_refuses_a_dead_or_foreign_file(self, rig, monkeypatch):
        """The builder's bearer source leaves a registered-client MCP out of a
        session when its file is an app or relay token (the reconnect wording
        names the mechanism), and hands the gateway the file's reference when
        it is the server's own; the value never leaves the file."""
        token_dir = rig.token_dir()
        monkeypatch.setattr(credential_resolver, "pick_account",
                            lambda mcp, agent, *, user_sub="": credential_resolver.AccountRef("ws-1", user_sub))
        (token_dir / "ws-1.json").write_text(json.dumps(_file(extra={"via_relay": True})))
        ref, reason = mcp_registry._gateway_bearer_source(rig.manifest, rig.sub, "pa", "user")
        assert ref is None and "signs in through the vendor's own authorization server" in reason
        (token_dir / "ws-1.json").write_text(json.dumps(_file(extra={"flow": "mcp_authorization", "resource": RESOURCE})))
        ref, reason = mcp_registry._gateway_bearer_source(rig.manifest, rig.sub, "pa", "user")
        assert reason is None and ref.account_label == "ws-1" and ref.token_dir == str(token_dir)
        assert "AT" not in repr(ref)

    def test_the_webhook_resolver_refuses_the_file(self, rig, monkeypatch):
        from services.webhooks import subscription_manager as sm
        (rig.token_dir() / "ws-1.json").write_text(json.dumps(_file(extra={"flow": "mcp_authorization", "resource": RESOURCE})))
        monkeypatch.setattr(sm, "_owner_username_for", lambda **kw: rig.username)
        with pytest.raises(sm.SubscriptionError) as info:
            sm._resolve_token_or_raise(provider_id=PROVIDER, scope="user", owner=rig.sub, agent=None,
                                       mcp_name=MCP, account_label="ws-1")
        assert "serves the MCP server only" in str(info.value)

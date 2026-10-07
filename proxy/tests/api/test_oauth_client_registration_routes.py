"""The admin's view of the OAuth clients the install registered at vendors
(``GET /v1/admin/oauth-client-registrations``, ``POST .../{id}/forget``)
and the integrations list's reconnect verdict per account.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from api.mcp import credentials as credentials_api
from auth.providers import UserContext, get_current_user
from services.mcp import mcp_manifest_parse, mcp_registry
from services.oauth import mcp_authorization as ma, oauth_account_store
from storage.identity import credential_store
from storage.identity import oauth_client_registrations as regs
from storage.pg import get_conn
from tests.auth.test_mcp_authorization_discovery import ISSUER, RESOURCE
from tests.auth.test_oauth_mcp_authorization_flow import MCP, PROVIDER, _manifest_data


def _client(principal: UserContext | None) -> TestClient:
    async def _stub():
        return principal
    app = FastAPI()
    app.include_router(credentials_api.router)
    app.dependency_overrides[get_current_user] = _stub
    return TestClient(app)


def _admin(**over) -> UserContext:
    kw = dict(sub="user-admin", email="a@t.com", name="Admin", role="admin")
    kw.update(over)
    return UserContext(**kw)


@pytest.fixture
def rows():
    live = regs.insert(issuer=ISSUER, redirect_uri="https://dash.example/cb",
                       registration_endpoint=f"{ISSUER}/register", client_id="cid-live",
                       client_secret="s3cr3t", token_endpoint_auth_method="client_secret_post")
    old = regs.insert(issuer=ISSUER, redirect_uri="http://localhost:8400/cb",
                      registration_endpoint=f"{ISSUER}/register", client_id="cid-old")
    regs.revoke(old["id"], "x")
    return live, old


@pytest.fixture
def registry(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_registry, "_manifests", {})
    from auth import oauth_providers
    oauth_providers.clear_manifest_cache()
    ma.clear_caches()

    def install(data):
        path = tmp_path / data["name"] / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        m = mcp_manifest_parse._parse_manifest(path)
        mcp_registry._manifests[m.name] = m
        return m
    yield install
    ma.clear_caches()
    oauth_providers.clear_manifest_cache()


class TestAdminRoutes:
    def test_list_has_no_secret_and_names_the_mcps(self, rows, registry):
        registry(_manifest_data())
        resp = _client(_admin()).get("/v1/admin/oauth-client-registrations")
        assert resp.status_code == 200
        listed = resp.json()["registrations"]
        assert [r["client_id"] for r in listed] == ["cid-live", "cid-old"]
        assert listed[0]["has_secret"] is True
        assert "s3cr3t" not in resp.text and "client_secret_enc" not in resp.text
        assert listed[0]["mcps"] == [MCP] and listed[1]["revoked_at"]

    def test_mcps_match_by_discovery_or_declared_issuer(self, rows, registry):
        data = _manifest_data()
        data["server"]["url_template"] = "https://api.other.example/mcp"
        data["credentials"]["oauth"]["proposed_hosts"] = ["api.other.example"]
        registry(data)
        listed = _client(_admin()).get("/v1/admin/oauth-client-registrations").json()["registrations"]
        assert listed[0]["mcps"] == []
        ma._discovery[("https://api.other.example/mcp", "")] = ma.AuthorizationServer(
            issuer=ISSUER, authorization_endpoint="a", token_endpoint="t", resource="r", fetched_at=1e12)
        listed = _client(_admin()).get("/v1/admin/oauth-client-registrations").json()["registrations"]
        assert listed[0]["mcps"] == [MCP]

    def test_forget_revokes_once(self, rows):
        live, old = rows
        c = _client(_admin())
        assert c.post(f"/v1/admin/oauth-client-registrations/{live['id']}/forget").status_code == 200
        assert regs.get(live["id"])["revoked_reason"].startswith("forgotten by user-adm")
        assert c.post(f"/v1/admin/oauth-client-registrations/{live['id']}/forget").status_code == 404
        assert c.post(f"/v1/admin/oauth-client-registrations/{old['id']}/forget").status_code == 404
        assert c.post("/v1/admin/oauth-client-registrations/999999/forget").status_code == 404

    def test_a_person_only(self, rows):
        live, _ = rows
        token_principal = _admin(is_api_key=True, session_id="sid", agent="pa")
        assert _client(token_principal).get("/v1/admin/oauth-client-registrations").status_code == 403
        assert _client(token_principal).post(
            f"/v1/admin/oauth-client-registrations/{live['id']}/forget").status_code == 403
        creator = UserContext(sub="user-manager", email="m@t.com", name="M", role="creator")
        assert _client(creator).get("/v1/admin/oauth-client-registrations").status_code == 403
        assert _client(None).get("/v1/admin/oauth-client-registrations").status_code == 401
        assert regs.get(live["id"])["revoked_at"] == ""


class TestIntegrationsReconnect:
    @pytest.fixture
    def person(self):
        sub = f"test-user-{uuid.uuid4().hex[:12]}"
        username = f"u{uuid.uuid4().hex[:8]}"
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO users (sub, email, name, username, role, auth_provider, created_at, last_login) "
                "VALUES (%s, %s, 'T', %s, 'creator', 'local', NOW()::text, NOW()::text)",
                (sub, f"{username}@t.example", username),
            )
            conn.commit()
        yield sub, username
        with get_conn() as conn:
            conn.execute("DELETE FROM users WHERE sub = %s", (sub,))
            conn.commit()

    def test_a_dead_file_says_reconnect(self, registry, person, monkeypatch, tmp_path):
        sub, username = person
        monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
        m = registry(_manifest_data())
        credential_store.set_user_credentials(sub, MCP, {"NOTION-HOSTED_EMAIL": "acme.com",
                                                          "NOTION-HOSTED_SERVICES": "read"}, account_label="ws-1")
        credential_store.set_account_display_email(sub, MCP, "ws-1", "acme.com")
        token_dir = oauth_account_store.get_token_dir(username, provider_id=PROVIDER)
        expired = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (token_dir / "ws-1.json").write_text(json.dumps({
            "provider": PROVIDER, "account_id": "u", "access_token": "AT", "refresh_token": "",
            "expires_at": expired, "scopes": ["read"], "client_id": "cid", "client_secret": "",
            "token_url": f"{ISSUER}/token",
            "extra": {"flow": "mcp_authorization", "resource": RESOURCE}}))
        monkeypatch.setattr(credentials_api.task_store, "get_user_agents", lambda s: ["pa"])
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda agent: [m])
        me = UserContext(sub=sub, email=f"{username}@t.example", name="T", role="creator",
                         agents=["pa"], agent_roles={"pa": "manager"})
        resp = _client(me).get("/v1/users/me/integrations")
        assert resp.status_code == 200, resp.text
        [entry] = [e for e in resp.json() if e["mcp_name"] == MCP]
        [account] = entry["accounts"]
        assert account["needs_reconnect"] is True and account["reconnect_reason"] == "expired"
        assert entry["oauth_meta"]["authorization_server"]["resource_host"] == "mcp.example.com"
        assert entry["oauth_meta"]["authorization_server"]["active"] is True
        # a live refresh token: no reconnect
        raw = json.loads((token_dir / "ws-1.json").read_text())
        raw["refresh_token"] = "RT"
        (token_dir / "ws-1.json").write_text(json.dumps(raw))
        [entry] = [e for e in _client(me).get("/v1/users/me/integrations").json() if e["mcp_name"] == MCP]
        assert entry["accounts"][0]["needs_reconnect"] is False

"""An MCP account connect completes in the browser that started it: every
start (the registered client, the hosted relay, the admin's own app, the
tenant-admin consent) answers with the HttpOnly cookie naming the state (a
ring of the latest four), host-prefixed at the root path on https; the web
callback refuses a state the cookie does not name before any exchange and
spends it; a state that expired or was spent gets its own page; a start from
a page open at another address of the install than DASHBOARD_PUBLIC_URL is
refused at once with the address to use; a vendor's error response leaves
the state unspent; a connect the app started takes the other road: the
callback redirects the code and the state to the app, whose exchange runs
under its own login and refuses a web state and another person before any
exchange.
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

import config
from api.auth import oauth as oauth_api
from services.oauth import oauth_engine
from storage.identity import credential_store
from tests.auth.test_mcp_authorization_discovery import ISSUER
from tests.auth.test_oauth_mcp_authorization_flow import (  # noqa: F401 (fixtures)
    DASH, MCP, PROVIDER, TOKEN, _app_shape, _manifest_data, _params, _start, person, rig,
)

COOKIE = "oto_oauth_connect"
OTHER_ADDRESS = "http://127.0.0.1:8400"
TENANT = "A7F6E4C2-1234-5678-9ABC-DEF012345678"
ADMIN_START = "/v1/oauth/microsoft/admin-consent/start"
ADMIN_CALLBACK = "/v1/oauth/microsoft/admin-consent/callback"


def _consent(rig, **kw):  # noqa: F811
    resp = _start(rig, **kw)
    assert resp.status_code == 200, resp.text
    return resp, _params(resp.json()["url"])


def _callback(client, state, code="c-1", **params):
    return client.get(f"/v1/oauth/{PROVIDER}/callback",
                      params={"code": code, "state": state, "iss": ISSUER, **params})


def test_the_start_sets_the_binding_cookie(rig, monkeypatch):  # noqa: F811
    resp, q = _consent(rig)
    header = resp.headers["set-cookie"]
    assert header.startswith(f"{COOKIE}=") and "HttpOnly" in header and "Path=/v1/oauth/" in header
    assert "SameSite=lax" in header and f"Max-Age={oauth_engine.STATE_TTL_SECONDS}" in header
    assert "Secure" not in header   # plain http in this rig
    assert q["state"] not in header  # the hash, never the state itself


def test_on_https_the_cookie_is_host_prefixed_at_the_root_and_the_callback_reads_it(rig, monkeypatch):  # noqa: F811
    """The ``__Host-`` prefix pins the cookie to this host and the root path
    (a sibling subdomain or another path cannot plant one), as the login's
    state cookie; the callback reads it under that name."""
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    secure = TestClient(rig.client.app, base_url="https://testserver")
    resp = secure.post(f"/v1/oauth/{PROVIDER}/start",
                       json={"mcp_name": MCP, "services": ["read"], "account_label": "", "mobile": False})
    assert resp.status_code == 200, resp.text
    header = resp.headers["set-cookie"]
    assert header.startswith(f"__Host-{COOKIE}=") and "Secure" in header and "HttpOnly" in header
    assert "Path=/;" in header or header.endswith("Path=/")
    q = _params(resp.json()["url"])
    assert "did not start this connect" in _callback(TestClient(rig.client.app, base_url="https://testserver"), q["state"]).text
    resp = secure.post(f"/v1/oauth/{PROVIDER}/start",
                       json={"mcp_name": MCP, "services": ["read"], "account_label": "", "mobile": False})
    assert "Account Connected" in _callback(secure, _params(resp.json()["url"])["state"]).text


def test_the_hosted_and_the_self_managed_starts_set_the_cookie_too(rig, monkeypatch):  # noqa: F811
    """The relay's consent URL and the admin's own app take the same
    binding as the registered client."""
    from services.billing import relay_client
    from services.oauth import mcp_authorization as ma
    data = _app_shape(_manifest_data())
    data["credentials"]["oauth"]["authorization_server"]["accepts_app_tokens"] = True
    rig.install(data)
    ma.clear_caches()
    seen: dict = {}

    async def _relay_url(**kw):
        seen["state"] = kw["state"]
        return "https://relay.example/consent"
    monkeypatch.setattr(relay_client, "hosted_oauth_active", lambda *a, **k: True)
    monkeypatch.setattr(relay_client, "oauth_authorize_url", _relay_url)
    resp = _start(rig)
    assert resp.status_code == 200 and resp.json()["url"] == "https://relay.example/consent"
    assert oauth_api._state_hash(seen["state"]) in resp.headers["set-cookie"]

    monkeypatch.setattr(relay_client, "hosted_oauth_active", lambda *a, **k: False)
    credential_store.set_infra_credentials("notion-hosted-app", {"CLIENT_ID": "app", "CLIENT_SECRET": "s"})
    ma.clear_caches()
    resp = _start(rig)
    assert resp.status_code == 200, resp.text
    q = _params(resp.json()["url"])
    assert q["client_id"] == "app"
    assert oauth_api._state_hash(q["state"]) in resp.headers["set-cookie"]


def test_a_start_from_another_address_is_refused_with_the_address_to_use(rig):  # noqa: F811
    """A page open at another address of the install than
    DASHBOARD_PUBLIC_URL can never finish a connect (the vendor sends the
    browser back to the configured address, without this page's cookie):
    refused at once, before any vendor call, naming both addresses. The
    app's connect and a caller without an Origin are not judged."""
    body = {"mcp_name": MCP, "services": ["read"], "account_label": "", "mobile": False}
    resp = rig.client.post(f"/v1/oauth/{PROVIDER}/start", json=body, headers={"Origin": OTHER_ADDRESS})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert DASH in detail and OTHER_ADDRESS in detail and "DASHBOARD_PUBLIC_URL" in detail
    assert rig.posted["register"] == [] and "set-cookie" not in resp.headers
    assert rig.client.post(f"/v1/oauth/{PROVIDER}/start", json=body, headers={"Origin": DASH}).status_code == 200
    assert rig.client.post(f"/v1/oauth/{PROVIDER}/start", json=body,
                           headers={"Origin": "HTTPS://Dash.Example:443"}).status_code == 200
    assert rig.client.post(f"/v1/oauth/{PROVIDER}/start", json={**body, "mobile": True},
                           headers={"Origin": OTHER_ADDRESS}).status_code == 200
    assert rig.client.post(f"/v1/oauth/{PROVIDER}/start", json=body).status_code == 200


def test_the_browser_that_started_it_completes_and_another_is_refused(rig):  # noqa: F811
    _, q = _consent(rig)
    other = TestClient(rig.client.app)          # another browser: no cookie
    resp = _callback(other, q["state"])
    assert resp.status_code == 200 and "did not start this connect" in resp.text
    assert f"on the dashboard at {DASH}" in resp.text
    assert rig.posted["token"] == []           # nothing exchanged
    assert credential_store.list_user_accounts(rig.sub, MCP) == []
    # the state is spent: the starting browser cannot finish it either
    resp = _callback(rig.client, q["state"])
    assert "expired or was already used" in resp.text and rig.posted["token"] == []
    # a fresh start in the starting browser completes
    _, q = _consent(rig)
    resp = _callback(rig.client, q["state"])
    assert "Account Connected" in resp.text and len(rig.posted["token"]) == 1


def test_an_expired_or_spent_state_gets_its_own_page_at_info(rig, caplog):  # noqa: F811
    """Not the generic exchange failure, and no traceback in the log: the
    cookie never outlives the state, so a slow consent lands here."""
    with caplog.at_level(logging.INFO, logger="otodock.oauth-api"):
        resp = _callback(rig.client, "no-such-state")
    assert "expired or was already used" in resp.text and "Token exchange failed" not in resp.text
    assert rig.posted["token"] == []
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("expired or spent state" in r.getMessage() for r in caplog.records)


def test_a_vendor_error_response_leaves_the_state_unspent(rig):  # noqa: F811
    """A decline at the vendor is reported without consuming the state; the
    same connect can still finish (the vendor's retry link)."""
    _, q = _consent(rig)
    resp = rig.client.get(f"/v1/oauth/{PROVIDER}/callback",
                          params={"error": "access_denied", "state": q["state"], "iss": ISSUER})
    assert "Provider returned: access_denied" in resp.text
    assert oauth_engine.peek_state_identity(q["state"]) is not None
    assert "Account Connected" in _callback(rig.client, q["state"]).text


def test_two_connects_started_side_by_side_both_complete(rig):  # noqa: F811
    _, q1 = _consent(rig)
    _, q2 = _consent(rig, label="Second")
    assert len(rig.client.cookies.get(COOKIE).split(".")) == 2
    assert "Account Connected" in _callback(rig.client, q1["state"]).text
    assert "Account Connected" in _callback(rig.client, q2["state"], code="c-2").text
    assert len(rig.posted["token"]) == 2


def test_the_ring_keeps_the_latest_four(rig):  # noqa: F811
    states = [_consent(rig, label=f"L{i}")[1]["state"] for i in range(5)]
    assert len(rig.client.cookies.get(COOKIE).split(".")) == 4
    # the oldest fell out of the ring: refused and spent
    assert "did not start this connect" in _callback(rig.client, states[0]).text
    assert "Account Connected" in _callback(rig.client, states[4]).text


def test_an_app_connect_goes_back_to_the_app_and_its_exchange_completes_it(rig):  # noqa: F811
    _, q = _consent(rig, mobile=True)
    other = TestClient(rig.client.app)          # the system browser: no cookie
    resp = other.get(f"/v1/oauth/{PROVIDER}/callback",
                     params={"code": "c-m", "state": q["state"], "iss": ISSUER},
                     follow_redirects=False)
    assert resp.status_code == 307
    link = resp.headers["location"]
    assert link.startswith(f"otodock://oauth/{PROVIDER}/complete?code=c-m&state=")
    from urllib.parse import quote
    assert f"state={q['state']}" in link and f"iss={quote(ISSUER)}" in link
    assert rig.posted["token"] == []           # nothing exchanged on the callback
    resp = rig.client.post(f"/v1/oauth/{PROVIDER}/exchange",
                           json={"code": "c-m", "state": q["state"], "iss": ISSUER})
    assert resp.status_code == 200 and resp.json()["status"] == "ok"
    assert len(rig.posted["token"]) == 1
    assert credential_store.list_user_accounts(rig.sub, MCP)


def test_the_exchange_refuses_a_web_state_and_another_person_before_any_exchange(rig, monkeypatch):  # noqa: F811
    _, q = _consent(rig)                        # a web state
    resp = rig.client.post(f"/v1/oauth/{PROVIDER}/exchange",
                           json={"code": "c", "state": q["state"], "iss": ISSUER})
    assert resp.status_code == 403 and "browser that started it" in resp.json()["detail"]
    assert rig.posted["token"] == []
    # the web state is still the starting browser's to finish
    assert "Account Connected" in _callback(rig.client, q["state"]).text
    # another person's app state
    _, q = _consent(rig, mobile=True)
    from auth.providers import UserContext, get_current_user
    stranger = UserContext(sub="someone-else", email="s@test.example", name="S", role="creator",
                           agents=[], agent_roles={})

    async def _stub():
        return stranger
    rig.client.app.dependency_overrides[get_current_user] = _stub
    resp = rig.client.post(f"/v1/oauth/{PROVIDER}/exchange",
                           json={"code": "c", "state": q["state"], "iss": ISSUER})
    assert resp.status_code == 403 and "different user" in resp.json()["detail"]
    assert len(rig.posted["token"]) == 1       # the earlier web completion only
    assert oauth_engine.peek_state_identity(q["state"]) is None   # spent
    resp = rig.client.post(f"/v1/oauth/{PROVIDER}/exchange",
                           json={"code": "c", "state": "nope", "iss": ISSUER})
    assert resp.status_code == 400


def test_the_tenant_admin_consent_is_bound_the_same_way(rig, monkeypatch):  # noqa: F811
    """Over HTTP, so the cookie wiring of the start and the callback runs:
    the cookie names the state, another browser is refused and spends it,
    the starting browser completes, another address is refused at the
    start, and the app's consent goes back to the app without a cookie."""
    monkeypatch.setattr(oauth_api, "_validate_provider_for_mcp", lambda mcp, provider: None)
    monkeypatch.setattr(oauth_api, "_app_creds_for", lambda provider, mcp, flow="": ("cid", "sec", {}))
    monkeypatch.setattr(oauth_api, "_microsoft_extra", lambda mcp, flow="": {"tenant_id": TENANT})
    start = rig.client.post(ADMIN_START, json={"mcp_name": "m365-mcp"})
    assert start.status_code == 200, start.text
    state = _params(start.json()["url"])["state"]
    assert start.headers["set-cookie"].startswith(f"{COOKIE}=")
    assert oauth_api._state_hash(state) in start.headers["set-cookie"]
    other = TestClient(rig.client.app)
    resp = other.get(ADMIN_CALLBACK, params={"admin_consent": "True", "tenant": TENANT, "state": state})
    assert "did not start this connect" in resp.text
    assert oauth_engine.peek_admin_consent_identity(state) is None   # spent
    assert "Invalid or expired" in rig.client.get(
        ADMIN_CALLBACK, params={"admin_consent": "True", "tenant": TENANT, "state": state}).text
    state = _params(rig.client.post(ADMIN_START, json={"mcp_name": "m365-mcp"}).json()["url"])["state"]
    resp = rig.client.get(ADMIN_CALLBACK, params={"admin_consent": "True", "tenant": TENANT, "state": state})
    assert "Tenant consent granted" in resp.text and TENANT.lower() in resp.text
    resp = rig.client.post(ADMIN_START, json={"mcp_name": "m365-mcp"}, headers={"Origin": OTHER_ADDRESS})
    assert resp.status_code == 409 and "DASHBOARD_PUBLIC_URL" in resp.json()["detail"]
    start = rig.client.post(ADMIN_START, json={"mcp_name": "m365-mcp", "mobile": True})
    state = _params(start.json()["url"])["state"]
    resp = other.get(ADMIN_CALLBACK, params={"admin_consent": "True", "tenant": TENANT, "state": state},
                     follow_redirects=False)
    assert resp.status_code == 307 and resp.headers["location"].startswith("otodock://oauth/microsoft/admin-consent/complete")

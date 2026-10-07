"""The credential gateway route (api/mcp/gateway.py): the session token
alone opens it, the path names the MCP and the token the session, the
forward is confined to the declared endpoint with the credential added and
the session's own headers dropped, a refusal is a JSON-RPC error the client
reads, and the answer streams back with the vendor's challenge, cookies and
redirects kept out.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.session_token import create_session_token
from core.credentials import mcp_broker, mcp_gateway
from core.credentials.mcp_gateway import GatewayCredential
from storage.identity import bearer_allowlist
from tests.conftest import live_session_token

client = TestClient(app)

_RPC = {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}


@pytest.fixture(autouse=True)
def _no_internal_listener(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", 0)
    monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda u: None)
    mcp_gateway.forget_memos()
    yield
    mcp_gateway.forget_memos()


class _Upstream:
    """A fake vendor behind the gateway's client: records every request,
    answers what the test asks."""

    def __init__(self, status=200, headers=None, body=b'{"jsonrpc":"2.0","id":7,"result":{"tools":[]}}'):
        self.requests: list[httpx.Request] = []
        self.status, self.headers, self.body = status, headers or {}, body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # a streaming body, as the network gives one (a byte body would be
        # read at construction and the route's streaming read would find
        # it consumed)
        return httpx.Response(self.status, headers=self.headers, stream=_Body(self.body))


class _Body(httpx.AsyncByteStream):
    def __init__(self, body: bytes):
        self._body = body

    async def __aiter__(self):
        yield self._body


def _wire(monkeypatch, upstream: _Upstream):
    c = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    monkeypatch.setattr(mcp_gateway, "client", lambda proxy_local: c)


def _provision(sid: str, key: str = "vendor", **cred) -> str:
    provider = f"gw-{uuid.uuid4().hex[:8]}"
    bearer_allowlist.add_allowed(provider, "mcp.example.com", "test")
    bearer_allowlist.add_allowed(provider, "localhost", "test")
    base = dict(upstream="https://mcp.example.com", path="/mcp", allowlist_key=provider,
                value="xoxb-real")
    base.update(cred)
    mcp_broker.provision(sid, {key: mcp_broker.SecretBundle(gateway=GatewayCredential(**base))})
    return provider


def _token(sid: str) -> dict:
    # user-admin is a person the test database seeds: the holder check reads it.
    return {"Authorization": f"Bearer {live_session_token(sid, 'agent', 'user-admin')}"}


def test_no_bearer_a_cookie_the_master_key_and_a_dead_session_are_refused():
    r = client.post("/v1/mcp-gateway/vendor/mcp", json=_RPC)
    assert r.status_code == 401
    r = client.post("/v1/mcp-gateway/vendor/mcp", json=_RPC, cookies={"session": "x"})
    assert r.status_code == 401
    r = client.post("/v1/mcp-gateway/vendor/mcp", json=_RPC,
                    headers={"Authorization": f"Bearer {config.API_KEY}"})
    assert r.status_code in (401, 403)
    dead = create_session_token(str(uuid.uuid4()), "agent", "user-1")
    r = client.post("/v1/mcp-gateway/vendor/mcp", json=_RPC, headers={"Authorization": f"Bearer {dead}"})
    assert r.status_code == 401


def test_two_authorization_headers_are_refused():
    sid = str(uuid.uuid4())
    tok = live_session_token(sid, "agent", "user-admin")
    r = client.post("/v1/mcp-gateway/vendor/mcp", content=json.dumps(_RPC),
                    headers=[("Authorization", f"Bearer {tok}"), ("authorization", f"Bearer {tok}"),
                             ("Content-Type", "application/json")])
    assert r.status_code == 401


def test_a_session_without_the_mcp_gets_a_json_rpc_error_never_a_401(monkeypatch):
    sid = str(uuid.uuid4())
    up = _Upstream()
    _wire(monkeypatch, up)
    r = client.post("/v1/mcp-gateway/vendor/mcp", json=_RPC, headers=_token(sid))
    assert r.status_code == 200
    err = r.json()["error"]
    assert r.json()["id"] == 7 and "No credential is provisioned" in err["message"]
    assert up.requests == []
    # a notification, a GET and a DELETE take their own shapes
    r = client.post("/v1/mcp-gateway/vendor/mcp", json={"jsonrpc": "2.0", "method": "notifications/x"},
                    headers=_token(sid))
    assert r.status_code == 202
    r = client.get("/v1/mcp-gateway/vendor/mcp", headers=_token(sid))
    assert r.status_code == 405
    r = client.delete("/v1/mcp-gateway/vendor/mcp", headers=_token(sid))
    assert r.status_code == 200
    assert "WWW-Authenticate" not in r.headers


def test_another_mcps_path_and_another_endpoint_path_are_refused(monkeypatch):
    sid = str(uuid.uuid4())
    _provision(sid)
    up = _Upstream()
    _wire(monkeypatch, up)
    r = client.post("/v1/mcp-gateway/other/mcp", json=_RPC, headers=_token(sid))
    assert r.status_code == 200 and "No credential" in r.json()["error"]["message"]
    r = client.post("/v1/mcp-gateway/vendor/admin/keys", json=_RPC, headers=_token(sid))
    assert r.status_code == 200 and "declared endpoint" in r.json()["error"]["message"]
    r = client.post("/v1/mcp-gateway/vendor/", json=_RPC, headers=_token(sid))
    assert r.status_code == 200 and "declared endpoint" in r.json()["error"]["message"]
    assert up.requests == []
    mcp_broker.purge_session(sid)


def test_the_forward_adds_the_credential_and_drops_the_sessions_own_headers(monkeypatch):
    sid = str(uuid.uuid4())
    _provision(sid)
    up = _Upstream(headers={"Mcp-Session-Id": "abc", "Set-Cookie": "v=1", "WWW-Authenticate": "Bearer",
                            "Content-Type": "application/json"})
    _wire(monkeypatch, up)
    r = client.post("/v1/mcp-gateway/vendor/mcp?session_id=zzz", json=_RPC,
                    headers={**_token(sid), "Mcp-Session-Id": "abc", "Cookie": "a=b",
                             "X-Forwarded-For": "1.2.3.4", "Accept": "application/json, text/event-stream",
                             "Accept-Encoding": "br"})
    assert r.status_code == 200 and r.json()["result"] == {"tools": []}
    assert len(up.requests) == 1
    req = up.requests[0]
    assert str(req.url) == "https://mcp.example.com/mcp"
    assert req.headers["Authorization"] == "Bearer xoxb-real"
    assert "cookie" not in req.headers and "x-forwarded-for" not in req.headers
    # the client's own encoding preference passes verbatim (the bytes do too)
    assert req.headers["Accept-Encoding"] == "br"
    assert mcp_gateway.forward_request_headers([("Accept", "x")])["Accept-Encoding"] == "identity"
    assert req.headers["Mcp-Session-Id"] == "abc"
    assert req.headers["Accept"] == "application/json, text/event-stream"
    assert req.headers["Host"] == "mcp.example.com"
    assert json.loads(req.content) == _RPC
    assert r.headers["Mcp-Session-Id"] == "abc"
    assert "set-cookie" not in r.headers and "www-authenticate" not in r.headers
    mcp_broker.purge_session(sid)


def test_a_header_style_key_and_a_sidecar_query(monkeypatch):
    sid = str(uuid.uuid4())
    _provision(sid, key="maps", header="X-Goog-Api-Key", prefix="", value="AIza-1",
               upstream="https://mcp.example.com")
    _provision(sid, key="github-mcp", upstream="http://localhost:8935", proxy_local=True, value="ghp_1")
    mcp_broker.provision(sid, {
        "maps": mcp_broker.SecretBundle(gateway=GatewayCredential(
            upstream="https://mcp.example.com", path="/mcp", allowlist_key=_provision(sid),
            header="X-Goog-Api-Key", prefix="", value="AIza-1")),
        "github-mcp": mcp_broker.SecretBundle(gateway=GatewayCredential(
            upstream="http://localhost:8935", path="/mcp", allowlist_key=_provision(sid),
            value="ghp_1", proxy_local=True)),
    })
    up = _Upstream()
    _wire(monkeypatch, up)
    r = client.post("/v1/mcp-gateway/maps/mcp", json=_RPC, headers=_token(sid))
    assert r.status_code == 200
    assert up.requests[-1].headers["X-Goog-Api-Key"] == "AIza-1"
    assert "authorization" not in up.requests[-1].headers
    r = client.post("/v1/mcp-gateway/github-mcp/mcp/?session_id=forged&x=1", json=_RPC, headers=_token(sid))
    assert r.status_code == 200
    u = up.requests[-1].url
    assert u.host == "localhost" and u.port == 8935 and u.path == "/mcp"
    assert dict(u.params) == {"x": "1", "session_id": sid}
    assert up.requests[-1].headers["Authorization"] == "Bearer ghp_1"
    mcp_broker.purge_session(sid)


def test_a_removed_allowlist_row_a_redirect_and_a_vendor_401_answer_as_json_rpc(monkeypatch):
    sid = str(uuid.uuid4())
    provider = _provision(sid)
    up = _Upstream(status=302, headers={"Location": "https://evil.example.net/x"})
    _wire(monkeypatch, up)
    r = client.post("/v1/mcp-gateway/vendor/mcp", json=_RPC, headers=_token(sid))
    assert r.status_code == 200 and "redirect" in r.json()["error"]["message"]
    assert "location" not in r.headers
    up = _Upstream(status=401, headers={"WWW-Authenticate": 'Bearer resource_metadata="https://x"'})
    _wire(monkeypatch, up)
    r = client.post("/v1/mcp-gateway/vendor/mcp", json=_RPC, headers=_token(sid))
    assert r.status_code == 200 and "reconnect" in r.json()["error"]["message"]
    assert "www-authenticate" not in r.headers
    for row in [e for e in bearer_allowlist.list_allowed() if e["provider_id"] == provider]:
        bearer_allowlist.delete_allowed(row["id"])
    up = _Upstream()
    _wire(monkeypatch, up)
    r = client.post("/v1/mcp-gateway/vendor/mcp", json=_RPC, headers=_token(sid))
    assert r.status_code == 200 and "Admin > Security" in r.json()["error"]["message"]
    assert up.requests == []
    mcp_broker.purge_session(sid)


def test_an_oversized_body_is_refused_before_any_upstream_call(monkeypatch):
    sid = str(uuid.uuid4())
    _provision(sid)
    up = _Upstream()
    _wire(monkeypatch, up)
    monkeypatch.setattr(mcp_gateway, "MAX_BODY_BYTES", 16)
    r = client.post("/v1/mcp-gateway/vendor/mcp", content=b"x" * 64, headers=_token(sid))
    assert r.status_code == 413 and up.requests == []
    mcp_broker.purge_session(sid)


def test_the_route_answers_on_the_internal_listener_only_when_one_is_bound(monkeypatch):
    sid = str(uuid.uuid4())
    _provision(sid)
    up = _Upstream()
    _wire(monkeypatch, up)
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", 9)  # the test client is on port 80
    r = client.post("/v1/mcp-gateway/vendor/mcp", json=_RPC, headers=_token(sid))
    assert r.status_code == 404 and up.requests == []
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", 80)
    r = client.post("/v1/mcp-gateway/vendor/mcp", json=_RPC, headers=_token(sid))
    assert r.status_code == 200 and len(up.requests) == 1
    mcp_broker.purge_session(sid)


def test_oauth_discovery_against_the_proxy_fails_fast():
    r = client.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 404
    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 404


def test_the_direct_layer_dials_the_internal_listener(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", 45678)
    url = mcp_gateway.entry_url("vendor", "/mcp") + "?session_id=s"
    assert mcp_gateway.internal_entry_url(url) == "http://127.0.0.1:45678/v1/mcp-gateway/vendor/mcp?session_id=s"
    assert mcp_gateway.internal_entry_url("http://localhost:8932/mcp/") == "http://localhost:8932/mcp/"
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", 0)
    assert mcp_gateway.internal_entry_url(url) == url

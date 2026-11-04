"""The credential gateway route (api/mcp/gateway.py): the session token
alone opens it, the path names the MCP and the token the session, the
forward is confined to the declared endpoint with the credential added and
the session's own headers dropped, a refusal is a JSON-RPC error the client
reads, and the answer streams back with the vendor's challenge, cookies and
redirects kept out.
"""

from __future__ import annotations

import asyncio
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
    monkeypatch.setattr(mcp_gateway, "client", lambda *a, **k: c)


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


_REACH = {"repositories": ["octo/hello"], "read_only": False}


def _tool(name: str, rid: int = 7, **arguments) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def test_a_narrowed_credential_judges_each_call_through_the_route(monkeypatch):
    sid = str(uuid.uuid4())
    up = _Upstream()
    _wire(monkeypatch, up)
    _provision(sid, reach=_REACH)
    try:
        ok = _tool("get_file_contents", owner="octo", repo="hello", path="README.md")
        r = client.post("/v1/mcp-gateway/vendor/mcp", json=ok, headers=_token(sid))
        assert r.status_code == 200 and len(up.requests) == 1
        assert json.loads(up.requests[0].content) == ok
        r = client.post("/v1/mcp-gateway/vendor/mcp", json=_tool("issue_write", owner="octo", repo="secret"),
                        headers=_token(sid))
        assert r.status_code == 200 and len(up.requests) == 1
        assert r.json()["id"] == 7 and "octo/secret is not one of them" in r.json()["error"]["message"]
        r = client.post("/v1/mcp-gateway/vendor/mcp", json=_tool("search_code", query="foo"), headers=_token(sid))
        assert json.loads(up.requests[-1].content)["params"]["arguments"]["query"] == "foo repo:octo/hello"
        batch = [_tool("get_me", rid=1), _tool("list_issues", rid=2, owner="x", repo="y"),
                 {"jsonrpc": "2.0", "method": "notifications/progress"}]
        r = client.post("/v1/mcp-gateway/vendor/mcp", json=batch, headers=_token(sid))
        assert [e["id"] for e in r.json()] == [1, 2] and len(up.requests) == 2
        r = client.post("/v1/mcp-gateway/vendor/mcp", content=json.dumps(ok),
                        headers={**_token(sid), "Content-Type": "application/json", "Content-Encoding": "gzip"})
        assert "could not be read" in r.json()["error"]["message"] and len(up.requests) == 2
    finally:
        mcp_broker.purge_session(sid)


def test_a_read_only_reach_turns_on_githubs_read_only_mode(monkeypatch):
    sid = str(uuid.uuid4())
    up = _Upstream()
    _wire(monkeypatch, up)
    _provision(sid, reach={"repositories": [], "read_only": True})
    try:
        r = client.post("/v1/mcp-gateway/vendor/mcp", json=_RPC, headers={
            **_token(sid), "x-mcp-readonly": "false", "X-MCP-Toolsets": "all", "X-Mcp-Tools": "x"})
        assert r.status_code == 200
        sent = up.requests[0]
        assert sent.headers.get_list("x-mcp-readonly") == ["true"]
        assert "x-mcp-toolsets" not in sent.headers and "x-mcp-tools" not in sent.headers
        assert sent.url.path == "/mcp/readonly"
    finally:
        mcp_broker.purge_session(sid)


def test_an_unnarrowed_credential_forwards_as_it_always_did(monkeypatch):
    sid = str(uuid.uuid4())
    up = _Upstream()
    _wire(monkeypatch, up)
    _provision(sid)
    raw = b'{"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}, "params": 1}'
    try:
        client.post("/v1/mcp-gateway/vendor/mcp", content=raw,
                    headers={**_token(sid), "Content-Type": "application/json", "X-MCP-Toolsets": "all"})
        sent = up.requests[0]
        assert sent.content == raw and sent.url.path == "/mcp"
        assert sent.headers["x-mcp-toolsets"] == "all"
    finally:
        mcp_broker.purge_session(sid)


def test_the_reach_judges_the_method_the_upstream_client_will_send():
    """A tunnel frame's method reaches httpx as given and is uppercased
    there: a lower-case POST is judged, GET and DELETE pass without a body,
    anything else is refused."""
    from auth import github_reach
    cred = GatewayCredential(upstream="https://mcp.example.com", path="/mcp", allowlist_key="k",
                             reach=_REACH)
    outside = json.dumps(_tool("list_issues", owner="octo", repo="secret")).encode()
    assert "octo/secret is not one of them" in mcp_gateway.judge_reach(cred, "vendor", "post", outside)[0]
    assert mcp_gateway.judge_reach(cred, "vendor", "get", b"") == ("", None)
    assert mcp_gateway.judge_reach(cred, "vendor", "DELETE", b"") == ("", None)
    for method, body in (("GET", outside), ("PATCH", outside), ("PUT", b""), ("", outside)):
        assert mcp_gateway.judge_reach(cred, "vendor", method, body)[0] == github_reach.METHOD_REFUSED, method


def test_the_reach_survives_the_descriptor_and_keeps_the_token_on_the_platform():
    cred = GatewayCredential(upstream="https://api.githubcopilot.com", path="/mcp/", allowlist_key="github",
                             value="", reach={"repositories": ["octo/hello"], "read_only": True})
    back = GatewayCredential.from_descriptor(cred.descriptor())
    assert back.reach == cred.reach and back.forwarded_here
    assert not GatewayCredential(upstream="https://x.example", path="/", allowlist_key="k").forwarded_here
    cli = mcp_gateway.CliCredential(host="github.com", token_ref=mcp_gateway.TokenRef("/t", "a"),
                                    reach={"repositories": [], "read_only": True})
    assert mcp_gateway.CliCredential.from_descriptor(cli.descriptor()).reach == cli.reach


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


_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
               "NO_PROXY", "no_proxy")


def test_the_vendor_pool_is_checked_the_sidecar_and_a_proxied_upstream_plain(monkeypatch):
    """Three pools per loop: vendors dial only addresses outside the
    platform's own address space, sidecars keep the plain client, and a
    vendor the environment's proxy carries keeps a plain client too (the
    proxy resolves the name)."""
    from services.infra import pinned_http
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    mcp_gateway._clients.clear()
    mcp_gateway.forget_memos()

    async def pools():
        vendor = mcp_gateway.client(False, "https://mcp.example.com")
        again = mcp_gateway.client(False, "https://other.example.com")
        sidecar = mcp_gateway.client(True, "http://127.0.0.1:8932")
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
        proxied = mcp_gateway.client(False, "https://proxied.example.com")
        return vendor, again, sidecar, proxied
    vendor, again, sidecar, proxied = asyncio.run(pools())
    assert isinstance(vendor._transport, pinned_http.CheckedTransport)
    assert vendor._transport.policy is pinned_http.control_plane
    assert again is vendor
    assert sidecar is not vendor and not isinstance(sidecar._transport, pinned_http.CheckedTransport)
    assert proxied is not sidecar and not isinstance(proxied._transport, pinned_http.CheckedTransport)
    assert sorted(k[1] for k in mcp_gateway._clients) == ["proxied", "sidecar", "vendor"]
    mcp_gateway._clients.clear()
    mcp_gateway.forget_memos()


def test_a_vendor_that_rebinds_to_loopback_between_the_check_and_the_dial_is_refused(monkeypatch):
    """The pre-check passed (patched, as in every test here); the dial
    resolves the name again, gets loopback and refuses before a byte goes
    out: the route answers its generic JSON-RPC error, the listener sees no
    connection."""
    from services.infra import pinned_http
    from tests.core.test_pinned_http import Listener
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    target = Listener()
    monkeypatch.setattr(pinned_http, "_getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("127.0.0.1", port))])
    mcp_gateway._clients.clear()
    sid = str(uuid.uuid4())
    _provision(sid, upstream=f"https://mcp.example.com:{target.port}")
    r = client.post("/v1/mcp-gateway/vendor/mcp", json=_RPC, headers=_token(sid))
    assert r.status_code == 200
    assert "did not answer" in r.json()["error"]["message"]
    assert target.connections == 0
    target.close()
    mcp_broker.purge_session(sid)
    mcp_gateway._clients.clear()


# ---------------------------------------------------------------------------
# The server's own 401 for a token file: the account says "Reconnect needed"
# ---------------------------------------------------------------------------

_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
_MARKED_MCP = "refusing-mcp"


@pytest.fixture
def refusals(monkeypatch):
    """The module's spawned work, collected and run in the test thread after
    each request, and its clock."""
    from types import SimpleNamespace

    from services.oauth import token_refusals
    token_refusals.forget()
    coros: list = []
    clock = {"now": 1000.0}
    monkeypatch.setattr(token_refusals, "background",
                        SimpleNamespace(spawn=lambda coro, **k: coros.append(coro)))
    monkeypatch.setattr(token_refusals, "_monotonic", lambda: clock["now"])

    def run() -> int:
        n = 0
        while coros:
            asyncio.run(coros.pop(0))
            n += 1
        return n
    yield SimpleNamespace(run=run, clock=clock, coros=coros, module=token_refusals)
    for c in coros:
        c.close()
    token_refusals.forget()


def _token_file(tmp_path, *, access_token="at-1", refresh_token="", expires_at="", extra=None,
                client_secret=""):
    d = tmp_path / "sessions" / "refusing-tokens" / "alice"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "acct.json"
    f.write_text(json.dumps({
        "provider": "refusing", "account_id": "a", "access_token": access_token,
        "refresh_token": refresh_token, "expires_at": expires_at, "client_id": "ci",
        "client_secret": client_secret, "token_url": "https://auth.example.com/token",
        "scopes": [], "extra": extra or {}}))
    return f


def _provision_file(sid, f, **cred):
    base = dict(value="", token_ref=mcp_gateway.TokenRef(str(f.parent), "acct"), manifest=_MARKED_MCP)
    base.update(cred)
    return _provision(sid, **base)


def _marks(f) -> dict:
    return json.loads(f.read_text())["extra"].get("refused_by") or {}


def _post(sid, msg, mcp="vendor", path="mcp"):
    return client.post(f"/v1/mcp-gateway/{mcp}/{path}", json=msg, headers=_token(sid))


def test_a_second_sessions_refusal_marks_the_mcp_and_one_sessions_burst_does_not(monkeypatch, tmp_path, refusals, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="otodock.mcp-gateway")
    f = _token_file(tmp_path)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    _provision_file(a, f)
    _provision_file(b, f)
    _wire(monkeypatch, _Upstream(status=401))
    for _ in range(3):
        r = _post(a, _tool("search"))
        assert r.status_code == 200 and "reconnect" in r.json()["error"]["message"]
        refusals.run()
    assert _marks(f) == {}
    r = _post(b, _INIT)
    refusals.run()
    mark = _marks(f)[_MARKED_MCP]
    assert mark["methods"] == ["initialize", "tools/call"] and mark["at"]
    assert "acct" not in caplog.text and "at-1" not in caplog.text
    assert "refused the token again" in caplog.text
    # marked: the gateway notes nothing more, and still forwards
    up = _Upstream(status=401)
    _wire(monkeypatch, up)
    _post(a, _tool("search"))
    assert len(up.requests) == 1 and refusals.coros == []
    mcp_broker.purge_session(a)
    mcp_broker.purge_session(b)


def test_one_session_counts_again_ten_minutes_later(monkeypatch, tmp_path, refusals):
    f = _token_file(tmp_path)
    a = str(uuid.uuid4())
    _provision_file(a, f)
    _wire(monkeypatch, _Upstream(status=401))
    _post(a, _tool("search"))
    refusals.clock["now"] += 599
    _post(a, _tool("search"))
    refusals.run()
    assert _marks(f) == {}
    refusals.clock["now"] += 2
    _post(a, _tool("search"))
    refusals.run()
    assert _marks(f)[_MARKED_MCP]["methods"] == ["tools/call"]
    mcp_broker.purge_session(a)


def test_what_is_not_the_servers_verdict_on_the_token_never_counts(monkeypatch, tmp_path, refusals):
    f = _token_file(tmp_path)
    sids = [str(uuid.uuid4()) for _ in range(6)]
    _provision_file(sids[0], f)
    _provision_file(sids[1], f)
    _wire(monkeypatch, _Upstream(status=401, headers={
        "WWW-Authenticate": 'Bearer error="insufficient_scope", scope="admin"'}))
    _post(sids[0], _tool("a"))
    _post(sids[1], _tool("a"))
    # a gateway refusal: another endpoint path, an MCP the session does not hold
    _wire(monkeypatch, _Upstream(status=401))
    _post(sids[0], _tool("a"), path="other")
    _post(sids[1], _tool("a"), mcp="nobody")
    # a GET
    client.get("/v1/mcp-gateway/vendor/mcp", headers=_token(sids[0]))
    # a static value and a sidecar's token file
    _provision(sids[2], value="key-1")
    _provision_file(sids[3], f, upstream="http://localhost:8935", proxy_local=True)
    _post(sids[2], _tool("a"))
    _post(sids[3], _tool("a"))

    def dial_fails(request):
        raise httpx.ConnectError("refused")
    _wire(monkeypatch, dial_fails)
    _post(sids[0], _tool("a"))
    assert refusals.module._entries == {} and refusals.coros == []
    _wire(monkeypatch, _Upstream(status=401))
    _post(sids[0], _tool("a"))
    assert len(refusals.module._entries) == 1
    for s in sids:
        mcp_broker.purge_session(s)


def test_an_answered_call_ends_the_count_and_clears_the_mark(monkeypatch, tmp_path, refusals):
    f = _token_file(tmp_path)
    a, b, c = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    for s in (a, b, c):
        _provision_file(s, f)
    _wire(monkeypatch, _Upstream(status=401))
    _post(a, _tool("search"))
    _wire(monkeypatch, _Upstream())
    _post(a, _tool("search"))
    _wire(monkeypatch, _Upstream(status=401))
    _post(b, _tool("search"))
    refusals.run()
    assert _marks(f) == {}
    _post(c, _tool("search"))
    refusals.run()
    assert _MARKED_MCP in _marks(f)
    # the token works again: an answered initialize is not the refused call,
    # an answered tool call is
    _wire(monkeypatch, _Upstream(body=b'{"jsonrpc":"2.0","id":1,"result":{}}'))
    _post(a, _INIT)
    refusals.run()
    assert _MARKED_MCP in _marks(f)
    _post(a, _tool("search"))
    refusals.run()
    assert _marks(f) == {}
    for s in (a, b, c):
        mcp_broker.purge_session(s)


def test_an_answered_tool_call_ends_any_count_and_clears_any_mark(monkeypatch, tmp_path, refusals):
    """New chats are refused at ``initialize`` alone, which a running session
    never sends again: its answered tool call still ends the count and takes
    the mark off."""
    f = _token_file(tmp_path)
    a, b, c, running = (str(uuid.uuid4()) for _ in range(4))
    for s in (a, b, c, running):
        _provision_file(s, f)
    _wire(monkeypatch, _Upstream(status=401))
    _post(a, _INIT)
    _wire(monkeypatch, _Upstream())
    _post(running, _tool("search"))
    _wire(monkeypatch, _Upstream(status=401))
    _post(b, _INIT)
    refusals.run()
    assert _marks(f) == {}
    _post(c, _INIT)
    refusals.run()
    assert _marks(f)[_MARKED_MCP]["methods"] == ["initialize"]
    _wire(monkeypatch, _Upstream())
    _post(running, _tool("search"))
    refusals.run()
    assert _marks(f) == {}
    for s in (a, b, c, running):
        mcp_broker.purge_session(s)


def test_a_token_the_worker_can_renew_is_renewed_not_marked(monkeypatch, tmp_path, refusals):
    from services.oauth import oauth_refresh_worker
    f = _token_file(tmp_path, refresh_token="RT", client_secret="cs")
    a, b, c = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    for s in (a, b, c):
        _provision_file(s, f)
    asked: list = []

    async def renew_now(path, *, still):
        asked.append(path)
        raw = json.loads(path.read_text())
        assert still(raw)
        _token_file(tmp_path, access_token="at-2", refresh_token="RT", client_secret="cs")
        return oauth_refresh_worker.RENEWED
    monkeypatch.setattr(oauth_refresh_worker, "renew_now", renew_now)
    _wire(monkeypatch, _Upstream(status=401))
    _post(a, _tool("search"))
    refusals.run()
    assert asked == [f] and _marks(f) == {}
    # the renewed token is refused too, from another session: marked, no
    # second renewal
    _post(b, _tool("search"))
    refusals.run()
    assert asked == [f] and _MARKED_MCP in _marks(f)
    for s in (a, b, c):
        mcp_broker.purge_session(s)


def test_a_token_past_its_expiry_is_marked_at_the_first_refusal(monkeypatch, tmp_path, refusals):
    f = _token_file(tmp_path, expires_at="2026-01-01T00:00:00Z", extra={"flow": "personal_access_token"})
    a = str(uuid.uuid4())
    _provision_file(a, f)
    _wire(monkeypatch, _Upstream(status=401))
    _post(a, _tool("search"))
    refusals.run()
    assert _MARKED_MCP in _marks(f)
    mcp_broker.purge_session(a)


def test_a_refusal_of_a_token_the_file_no_longer_holds_marks_nothing(monkeypatch, tmp_path, refusals):
    f = _token_file(tmp_path)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    _provision_file(a, f)
    _provision_file(b, f)
    _wire(monkeypatch, _Upstream(status=401))
    _post(a, _tool("search"))

    def reconnect_then_refuse(request):
        # a reconnect lands while the old token's request is out
        _token_file(tmp_path, access_token="at-new")
        return httpx.Response(401, stream=_Body(b""))
    _wire(monkeypatch, reconnect_then_refuse)
    _post(b, _tool("search"))
    refusals.run()
    assert _marks(f) == {} and json.loads(f.read_text())["access_token"] == "at-new"
    mcp_broker.purge_session(a)
    mcp_broker.purge_session(b)


def test_a_refusal_of_the_renewed_token_during_the_renewal_still_carries_it(monkeypatch, tmp_path, refusals):
    """The renewed token is refused before the renewal's answer is back: the
    new count carries the renewal, so the next event marks instead of asking
    for a second renewal."""
    from services.oauth import oauth_refresh_worker
    f = _token_file(tmp_path, refresh_token="RT", client_secret="cs")
    a, c = str(uuid.uuid4()), str(uuid.uuid4())
    for s in (a, c):
        _provision_file(s, f)
    ref = mcp_gateway.TokenRef(str(f.parent), "acct")
    asked: list = []

    async def renew_now(path, *, still):
        asked.append(path)
        _token_file(tmp_path, access_token="at-2", refresh_token="RT", client_secret="cs")
        renewed = mcp_gateway.Prepared(url=None, headers={}, proxy_local=False, client=None, token_ref=ref,
                                       manifest=_MARKED_MCP, digest=mcp_gateway._digest("at-2"))
        refusals.module.refused(renewed, session_id="session-b", method="POST",
                                body=json.dumps(_tool("search")).encode(), challenge="")
        return oauth_refresh_worker.RENEWED
    monkeypatch.setattr(oauth_refresh_worker, "renew_now", renew_now)
    _wire(monkeypatch, _Upstream(status=401))
    _post(a, _tool("search"))
    refusals.run()
    assert asked == [f] and _marks(f) == {}
    _post(c, _tool("search"))
    refusals.run()
    assert asked == [f] and _MARKED_MCP in _marks(f)
    for s in (a, c):
        mcp_broker.purge_session(s)


@pytest.mark.parametrize("late", ["during the renewal", "after the renewal"])
def test_a_late_refusal_of_the_old_token_keeps_the_renewal(monkeypatch, tmp_path, refusals, late):
    """Another session's request resolved the file before the renewal wrote
    it, and its refusal lands while the renewal runs or just after: the
    renewal stands, and the renewed token's refusal marks with no second
    renewal (a token of another length, so the gateway's file memo re-reads)."""
    from services.oauth import oauth_refresh_worker
    f = _token_file(tmp_path, refresh_token="RT", client_secret="cs")
    a, c = str(uuid.uuid4()), str(uuid.uuid4())
    for s in (a, c):
        _provision_file(s, f)
    ref = mcp_gateway.TokenRef(str(f.parent), "acct")
    asked: list = []

    def late_refusal():
        old = mcp_gateway.Prepared(url=None, headers={}, proxy_local=False, client=None, token_ref=ref,
                                   manifest=_MARKED_MCP, digest=mcp_gateway._digest("at-1"))
        refusals.module.refused(old, session_id="session-b", method="POST",
                                body=json.dumps(_tool("search")).encode(), challenge="")

    async def renew_now(path, *, still):
        asked.append(path)
        _token_file(tmp_path, access_token="at-renewed", refresh_token="RT", client_secret="cs")
        if late == "during the renewal":
            late_refusal()
        return oauth_refresh_worker.RENEWED
    monkeypatch.setattr(oauth_refresh_worker, "renew_now", renew_now)
    _wire(monkeypatch, _Upstream(status=401))
    _post(a, _tool("search"))
    refusals.run()
    if late == "after the renewal":
        late_refusal()
        refusals.run()
    assert asked == [f] and _marks(f) == {}
    _post(c, _tool("search"))
    refusals.run()
    assert asked == [f] and _MARKED_MCP in _marks(f)
    for s in (a, c):
        mcp_broker.purge_session(s)


def test_the_table_forgets_old_counts_and_old_renewals(monkeypatch, refusals):
    m = refusals.module
    now = 1_000_000.0
    old, new = ("dir", "old", "mcp"), ("dir", "new", "mcp")
    m._entries[old] = m._Entry(digest="d", first_at=now - m._WINDOW_S - 1)
    m._entries[new] = m._Entry(digest="d", first_at=now)
    m._renewed_at[old] = now - m._RENEWED_CARRY_S - 1
    m._renewed_at[new] = now
    assert not m._renewed_lately(old, now) and m._renewed_lately(new, now)
    m._prune(now)
    assert len(m._entries) == 2 and len(m._renewed_at) == 2
    monkeypatch.setattr(m, "_TABLE_MAX", 4)
    m._prune(now)
    assert list(m._entries) == [new] and list(m._renewed_at) == [new]

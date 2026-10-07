"""The satellite-local credential gateway (satellite/transport/mcp_gateway.py):
the pushed tokens' table (keyed by the session token's hash and the MCP,
leased, swept, wiped), and the loopback handler: the credential added on
the way out, the session's own headers kept on the machine, the forward
confined to the pushed endpoint, refusals in the JSON-RPC shape, no
redirect followed, the vendor's challenge and cookies kept out.
"""
import asyncio
import hashlib
import json
import time

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from satellite.transport import mcp_gateway as gw
from satellite.transport.http_tunnel import LocalTunnelServer


class FakeWSClient:
    def __init__(self):
        self._authenticated = False  # the gateway serves while the link is down
        self.sent: list[dict] = []
        self.tunnel = None

    async def enqueue_send(self, msg: dict) -> None:
        self.sent.append(msg)


class _Vendor:
    """A loopback vendor: records every request, answers as told."""

    def __init__(self):
        self.requests: list[dict] = []
        self.status = 200
        self.headers = {"Mcp-Session-Id": "v-1", "Set-Cookie": "c=1", "WWW-Authenticate": "Bearer",
                        "Content-Type": "application/json"}
        self.body = b'{"jsonrpc":"2.0","id":7,"result":{"tools":[]}}'
        self.port = 0
        self._runner = None

    async def _handle(self, request: web.Request):
        self.requests.append({"method": request.method, "path": request.path_qs,
                              "headers": dict(request.headers), "body": await request.read()})
        return web.Response(status=self.status, headers=self.headers, body=self.body)

    async def start(self):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self):
        await self._runner.cleanup()


@pytest_asyncio.fixture
async def rig():
    ws = FakeWSClient()
    tunnel = LocalTunnelServer(ws)
    ws.tunnel = tunnel
    port = await tunnel.start()
    vendor = _Vendor()
    await vendor.start()
    yield ws, tunnel, port, vendor
    await tunnel.stop()
    await vendor.stop()


BEARER = "eyJ.session.token"
RPC = {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}


def _push(table: gw.GatewayTable, vendor: _Vendor, mcp="vendor", bearer=BEARER, **over):
    msg = {"type": "mcp_gateway_token", "session_id": "s-1", "token_hash": gw.token_hash_of(bearer),
           "mcp": mcp, "header": "Authorization", "value": "Bearer xoxb-real",
           "expires_in": 600, "upstream": f"http://127.0.0.1:{vendor.port}/mcp"}
    msg.update(over)
    assert table.push(msg)
    return msg


def test_the_table_keys_by_token_hash_and_mcp_leases_and_wipes():
    t = gw.GatewayTable()
    assert not t.push({"type": "mcp_gateway_token", "mcp": "x"})  # incomplete
    msg = {"session_id": "s-1", "token_hash": "h1", "mcp": "vendor", "header": "X-Key",
           "value": "k", "expires_in": 3600, "upstream": "https://mcp.example.com/mcp"}
    assert t.push(msg)
    tok = t.get("h1", "vendor")
    assert tok.upstream == "https://mcp.example.com" and tok.path == "/mcp"
    assert tok.lease_until <= time.monotonic() + gw.LEASE_MAX_S  # capped at the lease
    assert t.get("h2", "vendor") is None and t.get("h1", "other") is None
    # a second session's token for the same mcp is its own row
    t.push({**msg, "token_hash": "h2", "session_id": "s-2"})
    assert len(t) == 2
    assert t.wipe({"token_hash": "h1"}) == 1 and t.get("h1", "vendor") is None
    assert t.wipe({"token_hash": "h1"}) == 0  # idempotent
    assert t.wipe({"session_id": "s-2"}) == 1 and len(t) == 0
    t.push({**msg, "expires_in": 0})
    assert t.get("h1", "vendor") is None
    assert t.sweep() == 1 and len(t) == 0


@pytest.mark.asyncio
async def test_wait_for_sees_a_push_in_flight():
    t = gw.GatewayTable()

    async def _late():
        await asyncio.sleep(0.1)
        t.push({"session_id": "s", "token_hash": "h", "mcp": "m", "header": "A", "value": "v",
                "expires_in": 60, "upstream": "https://x.example/mcp"})
    asyncio.create_task(_late())
    tok = await t.wait_for("h", "m", 2.0)
    assert tok is not None
    assert await t.wait_for("h", "nope", 0.3) is None


@pytest.mark.asyncio
async def test_the_forward_adds_the_credential_and_keeps_the_sessions_headers_here(rig):
    ws, tunnel, port, vendor = rig
    _push(tunnel.gateway.table, vendor)
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{port}/v1/mcp-gateway/vendor/mcp?session_id=x",
                          json=RPC, headers={"Authorization": f"Bearer {BEARER}", "Cookie": "a=b",
                                             "Mcp-Session-Id": "v-1", "X-Forwarded-For": "1.1.1.1",
                                             "Accept-Encoding": "br"}) as r:
            assert r.status == 200
            assert (await r.json())["result"] == {"tools": []}
            assert r.headers.get("Mcp-Session-Id") == "v-1"
            assert "Set-Cookie" not in r.headers and "WWW-Authenticate" not in r.headers
    assert ws.sent == []  # never tunneled
    req = vendor.requests[-1]
    assert req["method"] == "POST" and req["path"] == "/mcp"
    assert req["headers"]["Authorization"] == "Bearer xoxb-real"
    assert "Cookie" not in req["headers"] and "X-Forwarded-For" not in req["headers"]
    assert req["headers"]["Mcp-Session-Id"] == "v-1" and req["headers"]["Accept-Encoding"] == "br"
    assert BEARER not in json.dumps(req["headers"])
    assert json.loads(req["body"]) == RPC


@pytest.mark.asyncio
async def test_a_header_style_key_and_the_default_encoding(rig):
    ws, tunnel, port, vendor = rig
    _push(tunnel.gateway.table, vendor, mcp="maps", header="X-Goog-Api-Key", value="AIza-1")
    async with aiohttp.ClientSession(auto_decompress=False) as s:
        async with s.post(f"http://127.0.0.1:{port}/v1/mcp-gateway/maps/mcp", json=RPC,
                          headers={"Authorization": f"Bearer {BEARER}"}, skip_auto_headers=["Accept-Encoding"]) as r:
            assert r.status == 200
    req = vendor.requests[-1]
    assert req["headers"]["X-Goog-Api-Key"] == "AIza-1" and "Authorization" not in req["headers"]
    assert req["headers"]["Accept-Encoding"] == "identity"


@pytest.mark.asyncio
async def test_refusals_take_the_json_rpc_shape_and_never_a_401(rig):
    ws, tunnel, port, vendor = rig
    auth = {"Authorization": f"Bearer {BEARER}"}
    async with aiohttp.ClientSession() as s:
        # no token pushed for this session and mcp
        async with s.post(f"http://127.0.0.1:{port}/v1/mcp-gateway/vendor/mcp", json=RPC, headers=auth) as r:
            assert r.status == 200
            doc = await r.json()
            assert doc["id"] == 7 and "No credential has reached this machine" in doc["error"]["message"]
        async with s.post(f"http://127.0.0.1:{port}/v1/mcp-gateway/vendor/mcp",
                          json={"jsonrpc": "2.0", "method": "notifications/x"}, headers=auth) as r:
            assert r.status == 202
        async with s.get(f"http://127.0.0.1:{port}/v1/mcp-gateway/vendor/mcp", headers=auth) as r:
            assert r.status == 405
        async with s.delete(f"http://127.0.0.1:{port}/v1/mcp-gateway/vendor/mcp", headers=auth) as r:
            assert r.status == 200
        # no bearer at all
        async with s.post(f"http://127.0.0.1:{port}/v1/mcp-gateway/vendor/mcp", json=RPC) as r:
            assert r.status == 401
        # another session's bearer: its own hash, no row
        _push(tunnel.gateway.table, vendor)
        async with s.post(f"http://127.0.0.1:{port}/v1/mcp-gateway/vendor/mcp", json=RPC,
                          headers={"Authorization": "Bearer other.session"}) as r:
            assert r.status == 200 and "No credential" in (await r.json())["error"]["message"]
        # the pushed endpoint only
        async with s.post(f"http://127.0.0.1:{port}/v1/mcp-gateway/vendor/admin", json=RPC, headers=auth) as r:
            assert r.status == 200 and "declared endpoint" in (await r.json())["error"]["message"]
        # an encoded separator never reaches the table (a plain ``..`` is
        # collapsed by the client before it is sent)
        from yarl import URL
        async with s.post(URL(f"http://127.0.0.1:{port}/v1/mcp-gateway/vendor/%2e%2e/x", encoded=True),
                          json=RPC, headers=auth) as r:
            assert r.status == 403
    assert vendor.requests == []


@pytest.mark.asyncio
async def test_a_redirect_a_vendor_401_and_an_expired_lease(rig):
    ws, tunnel, port, vendor = rig
    auth = {"Authorization": f"Bearer {BEARER}"}
    _push(tunnel.gateway.table, vendor)
    async with aiohttp.ClientSession() as s:
        vendor.status, vendor.headers = 302, {"Location": "https://evil.example.net/"}
        async with s.post(f"http://127.0.0.1:{port}/v1/mcp-gateway/vendor/mcp", json=RPC, headers=auth) as r:
            assert r.status == 200 and "redirect" in (await r.json())["error"]["message"]
            assert "Location" not in r.headers
        vendor.status, vendor.headers = 401, {"WWW-Authenticate": "Bearer"}
        async with s.post(f"http://127.0.0.1:{port}/v1/mcp-gateway/vendor/mcp", json=RPC, headers=auth) as r:
            assert r.status == 200 and "reconnect" in (await r.json())["error"]["message"]
            assert "WWW-Authenticate" not in r.headers
        _push(tunnel.gateway.table, vendor, expires_in=0)
        vendor.status, vendor.headers = 200, {"Content-Type": "application/json"}
        async with s.post(f"http://127.0.0.1:{port}/v1/mcp-gateway/vendor/mcp", json=RPC, headers=auth) as r:
            assert r.status == 200 and "No credential" in (await r.json())["error"]["message"]
    assert len(vendor.requests) == 2


@pytest.mark.asyncio
async def test_a_wipe_ends_the_session_and_the_body_cap_refuses(rig, monkeypatch):
    ws, tunnel, port, vendor = rig
    auth = {"Authorization": f"Bearer {BEARER}"}
    msg = _push(tunnel.gateway.table, vendor)
    tunnel.gateway.table.wipe({"session_id": "s-1", "token_hash": msg["token_hash"]})
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{port}/v1/mcp-gateway/vendor/mcp", json=RPC, headers=auth) as r:
            assert r.status == 200 and "No credential" in (await r.json())["error"]["message"]
        _push(tunnel.gateway.table, vendor)
        monkeypatch.setattr(gw, "MAX_BODY_BYTES", 16)
        async with s.post(f"http://127.0.0.1:{port}/v1/mcp-gateway/vendor/mcp", data=b"x" * 64, headers=auth) as r:
            assert r.status == 413
    assert vendor.requests == []


def test_the_token_hash_is_sha256_of_the_bearer():
    assert gw.token_hash_of("abc") == hashlib.sha256(b"abc").hexdigest()

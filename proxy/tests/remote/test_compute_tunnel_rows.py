"""The tunnel rows the platform's own tools need on a machine, and the
credential gateway's row a compute machine alone may use.

Both allowlists (the proxy's dispatcher and the satellite's local link)
admit the five paths; the gateway row is refused on a paired machine's
link, where the machine serves the gateway itself, and admitted as a
session's tool traffic (the ``mcp`` class) on a compute machine's. Every
path a bundled stdio tool names next to ``PROXY_URL`` is on the list, so
no tool silently loses a route on a machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.remote.satellite_http_tunnel import (
    _ALLOWLIST_REGEXES,
    SatelliteHttpTunnelDispatcher,
    _is_allowed_path,
)

_FIVE = (
    "/v1/knowledge-libraries",
    "/v1/knowledge-libraries/lib-1/documents",
    "/v1/mcps/local-package/validate",
    "/v1/conversations",
    "/v1/conversations/ext-1/export",
    "/v1/phone/call-control/hangup",
    "/v1/mcp-gateway/github/repos/x",
)

_MCPS = Path(__file__).resolve().parents[3] / "mcps" / "custom"


def test_both_lists_admit_the_five_paths():
    from satellite.transport.http_tunnel import _is_allowed_path as _sat_allowed
    for path in _FIVE:
        assert _is_allowed_path(path), path
        assert _sat_allowed(path), path
    assert not _is_allowed_path("/v1/mcp-gateway/../admin")
    assert not _is_allowed_path("/v1/mcp-gateway")
    assert not _is_allowed_path("/v1/phone/call-control-admin")


class _Conn:
    def __init__(self):
        self.sent: list[dict] = []

    async def enqueue_send(self, msg):
        self.sent.append(msg)


class _Manager:
    def __init__(self, conn, *, compute: bool):
        self._conn = conn
        self._compute = compute

    def get_connection(self, _mid):
        return self._conn

    def is_compute(self, _mid):
        return self._compute


def _frame(path: str) -> dict:
    return {"type": "http_request", "stream_id": str(uuid.uuid4()), "method": "GET", "path": path,
            "headers": {}, "body_b64": "", "body_eof": True, "timeout_s": 30}


@pytest.mark.asyncio
async def test_the_gateway_row_is_refused_on_a_paired_machines_link():
    disp = SatelliteHttpTunnelDispatcher()
    conn = _Conn()
    frame = _frame("/v1/mcp-gateway/github/repos")
    await disp.handle_request_frame(_Manager(conn, compute=False), "m-1", frame)
    assert conn.sent and conn.sent[0]["status"] == 403 and conn.sent[0]["error"] == "path-not-allowlisted"
    assert ("m-1", frame["stream_id"]) not in disp._streams


@pytest.mark.asyncio
async def test_the_gateway_row_is_a_compute_machines_tool_stream():
    disp = SatelliteHttpTunnelDispatcher()
    conn = _Conn()
    frame = _frame("/v1/mcp-gateway/github/repos")
    await disp.handle_request_frame(_Manager(conn, compute=True), "c-1", frame)
    stream = disp._streams.get(("c-1", frame["stream_id"]))
    assert stream is not None and stream.kind == "mcp" and stream.admitted
    assert not conn.sent  # nothing refused: the dispatch runs
    stream.task.cancel()
    with contextlib.suppress(BaseException):  # the cancelled dispatch is the point
        await stream.task


def _admitted(literal: str) -> bool:
    if _is_allowed_path(literal) or _is_allowed_path(literal.rstrip("/")):
        return True
    return any(rx.pattern.startswith("^" + literal) for rx in _ALLOWLIST_REGEXES)


def test_every_path_a_bundled_stdio_tool_names_is_on_the_list():
    """The paths the stdio tools shipped with the platform call through
    ``PROXY_URL`` (what a session's sandbox or a machine's link forwards).
    A tool served from a container calls the proxy itself and is left out."""
    if not _MCPS.is_dir():
        pytest.skip("the bundled tools are not in this checkout")
    found: dict[str, set[str]] = {}
    for manifest in sorted(_MCPS.glob("*/manifest.json")):
        try:
            server = json.loads(manifest.read_text()).get("server") or {}
        except (OSError, ValueError):
            continue
        if (server.get("transport") or "stdio") != "stdio":
            continue
        for src in manifest.parent.rglob("*"):
            if src.suffix not in (".py", ".ts", ".js", ".mjs") or "node_modules" in src.parts or "venv" in src.parts:
                continue
            try:
                text = src.read_text(errors="ignore")
            except OSError:
                continue
            for m in re.finditer(r"PROXY_URL[^\n]{0,80}?(/v1/[A-Za-z0-9_/-]+)", text):
                found.setdefault(m.group(1), set()).add(manifest.parent.name)
    assert found, "no bundled tool names a proxy path"
    missing = {path: sorted(tools) for path, tools in found.items() if not _admitted(path)}
    assert not missing, f"paths a bundled tool calls that the tunnel refuses: {missing}"


# ── An app server's own listener (via "app") ────────────────────────────────

APP_ID = "00000000-0000-4000-8000-000000000001"


class _AppConn:
    def __init__(self):
        self.sent: list[dict] = []

    async def enqueue_send(self, msg):
        self.sent.append(msg)


class _AppManager:
    def __init__(self, compute=True, own_copy=False):
        self.conn = _AppConn()
        self.compute = compute
        self.own_copy = own_copy

    def get_connection(self, machine_id):
        return self.conn

    def is_compute(self, machine_id):
        return self.compute

    def shares_folders(self, machine_id):
        return self.compute and not self.own_copy


def _app_frame(path, **over):
    return {"type": "http_request", "stream_id": str(uuid.uuid4()), "method": "POST", "path": path, "headers": {},
            "body_b64": "", "body_eof": True, "timeout_s": 30, "via": "app", **over}


def test_the_app_list_is_the_same_on_both_sides():
    from core.remote.satellite_http_tunnel import _APP_ALLOWLIST_REGEXES as proxy_rows
    from satellite.transport.http_tunnel import _APP_ALLOWLIST_REGEXES as sat_rows
    assert [r.pattern for r in proxy_rows] == [r.pattern for r in sat_rows]


@pytest.mark.parametrize("route,admitted", [
    ("platform/notifications.create", True), ("egress/api.stripe.com/v1/x", True), ("events", True),
    ("bindings/orders/call", True), ("push", True), ("state", True), ("api/items", True), ("actions/press-1", True),
    ("actions/batch/x", False), ("client/abc/", False), ("ws/x", False), ("catalog/tasks", False),
])
def test_the_app_list_is_an_app_servers_routes_alone(route, admitted):
    from core.remote.satellite_http_tunnel import _is_app_path
    assert _is_app_path(f"/v1/apps/{APP_ID}/{route}") is admitted


@pytest.mark.parametrize("path", ["/mcp/file-tools/mcp", "/v1/hooks/permission", "/v1/tasks", "/v1/agents/a"])
def test_the_app_list_never_opens_a_session_route(path):
    from core.remote.satellite_http_tunnel import _is_app_path
    assert not _is_app_path(path)


def test_the_app_list_refuses_a_traversal():
    from core.remote.satellite_http_tunnel import _is_app_path
    assert not _is_app_path(f"/v1/apps/{APP_ID}/platform/../../hooks/permission")
    assert not _is_app_path(f"/v1/apps/{APP_ID}/egress/a%2Fb")


@pytest.mark.asyncio
async def test_an_app_frame_from_a_paired_machine_is_refused():
    disp = SatelliteHttpTunnelDispatcher()
    mgr = _AppManager(compute=False)
    await disp.handle_request_frame(mgr, "m1", _app_frame(f"/v1/apps/{APP_ID}/egress/a.example/x"))
    assert mgr.conn.sent[0]["status"] == 403 and mgr.conn.sent[0]["error"] == "path-not-allowlisted"


@pytest.mark.asyncio
async def test_an_app_frame_from_a_compute_machine_with_its_own_copy_is_refused():
    # App servers run only beside the platform: an app's data is the platform's.
    disp = SatelliteHttpTunnelDispatcher()
    mgr = _AppManager(compute=True, own_copy=True)
    await disp.handle_request_frame(mgr, "c2", _app_frame(f"/v1/apps/{APP_ID}/egress/a.example/x"))
    assert mgr.conn.sent[0]["status"] == 403 and mgr.conn.sent[0]["error"] == "path-not-allowlisted"


@pytest.mark.asyncio
async def test_an_app_frame_for_a_session_route_is_refused():
    disp = SatelliteHttpTunnelDispatcher()
    mgr = _AppManager()
    await disp.handle_request_frame(mgr, "m1", _app_frame("/v1/hooks/permission"))
    assert mgr.conn.sent[0]["status"] == 403


@pytest.mark.asyncio
async def test_egress_events_and_bindings_are_not_on_the_session_list():
    """An app-only route without the mark is refused like any other."""
    disp = SatelliteHttpTunnelDispatcher()
    mgr = _AppManager()
    for route in ("egress/a.example/x", "events", "bindings/x/y"):
        await disp.handle_request_frame(mgr, "m1", _app_frame(f"/v1/apps/{APP_ID}/{route}", via=""))
    assert [m["status"] for m in mgr.conn.sent] == [403, 403, 403]


def _client_answering():
    async def _body():
        yield b"{}"

    resp = MagicMock(status_code=200, headers={})
    resp.aiter_raw = _body
    resp.aclose = AsyncMock()
    client = MagicMock()
    client.build_request = MagicMock(return_value="REQ")
    client.send = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_a_session_token_on_the_app_listener_is_refused(monkeypatch):
    from core.remote import satellite_http_tunnel as tun
    monkeypatch.setattr(tun, "_bearer_session", lambda headers: ("sess-1", {"sid": "sess-1"}))
    disp = SatelliteHttpTunnelDispatcher()
    mgr = _AppManager()
    client = _client_answering()
    with patch.object(disp, "_get_client", return_value=client):
        await disp.handle_request_frame(mgr, "m1", _app_frame(f"/v1/apps/{APP_ID}/platform/x"))
        for _ in range(100):
            if mgr.conn.sent:
                break
            await asyncio.sleep(0.01)
    assert mgr.conn.sent[0]["status"] == 401 and mgr.conn.sent[0]["error"] == "session-token-on-app-listener"
    client.send.assert_not_called()


@pytest.mark.asyncio
async def test_an_app_frame_reaches_the_platform_with_the_path_it_sent():
    disp = SatelliteHttpTunnelDispatcher()
    mgr = _AppManager()
    client = _client_answering()
    raw = f"/v1/apps/{APP_ID}/egress/a.example/x%20y?q=1%2B2"
    with patch.object(disp, "_get_client", return_value=client):
        await disp.handle_request_frame(mgr, "m1", _app_frame(raw))
        for _ in range(100):
            if len(mgr.conn.sent) >= 2:
                break
            await asyncio.sleep(0.01)
    assert client.build_request.call_args.args[1].endswith(raw)


@pytest.mark.asyncio
async def test_one_app_cannot_hold_another_apps_room():
    from core.remote import satellite_http_tunnel as tun
    disp = SatelliteHttpTunnelDispatcher()
    mgr = _AppManager()
    other = "00000000-0000-4000-8000-000000000002"
    full = (("m1", f"app:{APP_ID}"), tun._MAX_APP_STREAMS_PER_APP)
    disp._open[full[0]] = full[1]
    await disp.handle_request_frame(mgr, "m1", _app_frame(f"/v1/apps/{APP_ID}/platform/x"))
    assert mgr.conn.sent[0]["status"] == 503 and mgr.conn.sent[0]["error"] == "too-many-streams"
    mgr.conn.sent.clear()
    client = _client_answering()
    with patch.object(disp, "_get_client", return_value=client):
        await disp.handle_request_frame(mgr, "m1", _app_frame(f"/v1/apps/{other}/platform/x"))
        for _ in range(100):
            if client.send.called:
                break
            await asyncio.sleep(0.01)
    client.send.assert_called_once()

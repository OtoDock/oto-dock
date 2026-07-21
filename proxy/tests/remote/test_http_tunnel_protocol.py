"""HTTP-over-WS tunnel — protocol-level tests.

Covers the proxy-side dispatcher:
  - Allowlist enforcement (defense-in-depth)
  - Path traversal rejection
  - Frame protocol (http_request/http_request_chunk/http_response/http_response_chunk)
  - Stream lifecycle (creation, dispatch, cleanup)
  - Reconnect cleanup (cancel_machine_streams)

The dispatcher's upstream calls are made via httpx against the platform's
own loopback (port 8400). For these protocol-level tests we don't run
the real platform — we patch httpx to return canned responses.
"""

import asyncio
import base64
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.remote.satellite_http_tunnel import (
    SatelliteHttpTunnelDispatcher,
    _is_allowed_path,
    _resolve_upstream_url,
)


class FakeConnection:
    """Stand-in for SatelliteConnection — captures enqueued frames."""

    def __init__(self):
        self.sent: list[dict] = []

    async def enqueue_send(self, msg: dict) -> None:
        self.sent.append(msg)


class FakeManager:
    """Stand-in for SatelliteConnectionManager."""

    def __init__(self, conn: FakeConnection):
        self.conn = conn

    def get_connection(self, machine_id: str):
        return self.conn


# ===== Allowlist =====

def test_allowlist_accepts_known_hook_paths():
    assert _is_allowed_path("/v1/hooks/permission")
    assert _is_allowed_path("/v1/hooks/file")
    assert _is_allowed_path("/v1/hooks/tool-result")
    assert _is_allowed_path("/v1/hooks/document-preview")
    # SubagentStop completion hook (remote subagents reach the proxy via this).
    assert _is_allowed_path("/v1/hooks/subagent")


def test_allowlist_accepts_artifact_and_app_hooks():
    """display-mcp on a satellite session: display_ui + the mini-app tools —
    the live 403 path-not-allowlisted found on the first trusted-VM test
    (2026-07-10). Exact hook paths only; no wildcard under /v1/hooks/apps."""
    assert _is_allowed_path("/v1/hooks/ui")
    assert _is_allowed_path("/v1/hooks/apps/pin")
    assert _is_allowed_path("/v1/hooks/apps/unpin")
    assert _is_allowed_path("/v1/hooks/apps/list")
    assert not _is_allowed_path("/v1/hooks/apps")
    assert not _is_allowed_path("/v1/hooks/apps/evil")
    assert not _is_allowed_path("/v1/hooks/uiX")


def test_allowlist_accepts_location_request():
    assert _is_allowed_path("/v1/location/request")


def test_allowlist_accepts_mcp_paths():
    assert _is_allowed_path("/mcp/file-tools/sse")
    assert _is_allowed_path("/mcp/camoufox/")
    assert _is_allowed_path("/mcp/github-mcp/anything/here")


def test_allowlist_accepts_platform_mcp_endpoints():
    """The 8 platform-management stdio MCPs call these back over the
    tunnel via the framework-standard PROXY_URL (base routes + subpaths)."""
    assert _is_allowed_path("/v1/session/current")  # gates 4 MCPs' first call
    assert _is_allowed_path("/v1/notifications")
    assert _is_allowed_path("/v1/notifications/abc-123/pause")
    assert _is_allowed_path("/v1/tasks")
    assert _is_allowed_path("/v1/tasks/runs/r1/stream")  # SSE
    assert _is_allowed_path("/v1/meetings")
    assert _is_allowed_path("/v1/meetings/m1/start")
    assert _is_allowed_path("/v1/triggers")
    assert _is_allowed_path("/v1/triggers/t1/fire")
    assert _is_allowed_path("/v1/subscriptions")
    assert _is_allowed_path("/v1/internal/memory/remember")
    assert _is_allowed_path("/v1/internal/memory/agent-settings/foo")
    assert _is_allowed_path("/v1/agents/my-agent/mcps")
    assert _is_allowed_path("/v1/agents/my-agent")
    assert _is_allowed_path("/v1/community/mcps")
    assert _is_allowed_path("/v1/execution-layers")


def test_allowlist_still_rejects_sensitive():
    """The broadened allowlist must NOT open admin / user / auth surfaces."""
    assert not _is_allowed_path("/v1/admin/remote-machines")
    assert not _is_allowed_path("/v1/users/me/remote-targets")
    assert not _is_allowed_path("/v1/agents")            # bare collection — no slug
    assert not _is_allowed_path("/v1/internal/secrets")  # only /internal/memory opened
    assert not _is_allowed_path("/v1/subscriptions/secret")  # subscriptions is exact-match


def test_allowlist_rejects_admin_paths():
    assert not _is_allowed_path("/v1/admin/users")
    assert not _is_allowed_path("/v1/admin/platform-settings")


def test_allowlist_rejects_users_me():
    assert not _is_allowed_path("/v1/users/me/integrations")


def test_allowlist_rejects_traversal_attempts():
    """Anchored regexes must defeat ../ traversal."""
    assert not _is_allowed_path("/v1/hooks/permission/../admin")
    assert not _is_allowed_path("/v1/hooks/../admin")
    assert not _is_allowed_path("/something/v1/hooks/permission")


def test_allowlist_rejects_root_and_random_paths():
    assert not _is_allowed_path("/")
    assert not _is_allowed_path("/random")
    assert not _is_allowed_path("/v1/sessions/abc/permission-response")


# ===== Dispatch =====

@pytest.mark.asyncio
async def test_dispatch_rejects_non_allowlisted_path():
    """A http_request for /admin/users gets a synthetic 403 without
    ever touching httpx."""
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)

    stream_id = str(uuid.uuid4())
    await disp.handle_request_frame(mgr, "m1", {
        "type": "http_request",
        "stream_id": stream_id,
        "method": "POST",
        "path": "/v1/admin/users",
        "headers": {},
        "body_b64": "",
        "body_eof": True,
        "timeout_s": 30,
    })

    # One synthetic 403 sent immediately
    assert len(conn.sent) == 1
    resp = conn.sent[0]
    assert resp["type"] == "http_response"
    assert resp["stream_id"] == stream_id
    assert resp["status"] == 403
    assert resp["error"] == "path-not-allowlisted"
    assert resp["body_eof"] is True
    # Stream cleaned up
    assert ("m1", stream_id) not in disp._streams


@pytest.mark.asyncio
async def test_dispatch_collision_rejects_with_409():
    """A second http_request with the same stream_id gets 409."""
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)

    stream_id = str(uuid.uuid4())
    # First request — set up the stream entry manually so we can probe
    # the collision branch without racing the real dispatch.
    from core.remote.satellite_http_tunnel import _HttpStream
    async with disp._lock:
        disp._streams[("m1", stream_id)] = _HttpStream(
            stream_id=stream_id, machine_id="m1",
        )

    # Second request with the same id
    await disp.handle_request_frame(mgr, "m1", {
        "type": "http_request",
        "stream_id": stream_id,
        "method": "POST",
        "path": "/v1/hooks/permission",
        "headers": {},
        "body_b64": "",
        "body_eof": True,
        "timeout_s": 30,
    })

    assert len(conn.sent) == 1
    assert conn.sent[0]["status"] == 409
    assert conn.sent[0]["error"] == "stream-id-collision"


@pytest.mark.asyncio
async def test_dispatch_missing_stream_id_drops_silently():
    """Frame without stream_id is dropped, no enqueue."""
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)
    await disp.handle_request_frame(mgr, "m1", {
        "type": "http_request",
        "path": "/v1/hooks/permission",
    })
    assert conn.sent == []


@pytest.mark.asyncio
async def test_dispatch_small_json_response_single_frame():
    """A typical hook call returns small JSON. The dispatcher now ALWAYS
    streams — sends the headers as ``http_response`` (body_eof=False),
    each upstream chunk as ``http_response_chunk``, then a final empty
    chunk with body_eof=True. Streaming is required even for small
    responses because the only way to tell the response is "small + done"
    is to wait for it to finish, which would re-introduce the buffering
    bug that timed out MCP streamable-http calls (camoufox/playwright)
    when the upstream took > 60 s — the satellite never saw a byte
    before claude-code's HTTP timeout fired.
    """
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)

    async def _fake_iter_raw():
        yield b'{"decision":"allow"}'

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.headers = {"content-type": "application/json"}
    # _dispatch streams via resp.aiter_raw() (forwards each upstream byte as it
    # arrives, preserving Content-Encoding — the camoufox no-rebuffer fix).
    fake_resp.aiter_raw = _fake_iter_raw
    fake_resp.aclose = AsyncMock()
    fake_client = MagicMock()
    fake_client.build_request = MagicMock(return_value="REQ")
    fake_client.send = AsyncMock(return_value=fake_resp)

    with patch.object(disp, "_get_client", return_value=fake_client):
        stream_id = str(uuid.uuid4())
        await disp.handle_request_frame(mgr, "m1", {
            "type": "http_request",
            "stream_id": stream_id,
            "method": "POST",
            "path": "/v1/hooks/permission",
            "headers": {"Authorization": "Bearer xyz"},
            "body_b64": base64.b64encode(b'{"tool_name":"Bash"}').decode(),
            "body_eof": True,
            "timeout_s": 30,
        })

        # Dispatch runs as a background task — wait briefly for completion.
        for _ in range(50):
            # Three frames expected: headers, data chunk, EOF marker.
            if len(conn.sent) >= 3:
                break
            await asyncio.sleep(0.01)

        # Frame 1: headers + empty body, body_eof=False (stream begins)
        assert conn.sent[0]["type"] == "http_response"
        assert conn.sent[0]["status"] == 200
        assert conn.sent[0]["body_eof"] is False
        assert conn.sent[0]["body_b64"] == ""

        # Frame 2: the actual body chunk (decision JSON)
        assert conn.sent[1]["type"] == "http_response_chunk"
        assert conn.sent[1]["body_eof"] is False
        body = base64.b64decode(conn.sent[1]["body_b64"])
        assert json.loads(body) == {"decision": "allow"}

        # Frame 3: empty EOF marker — flushes claude-code's HTTP reader.
        assert conn.sent[2]["type"] == "http_response_chunk"
        assert conn.sent[2]["body_eof"] is True
        assert conn.sent[2]["body_b64"] == ""


@pytest.mark.asyncio
async def test_dispatch_sse_streams_chunks_back():
    """An SSE response from upstream → first http_response (status+headers,
    body_eof=False), then http_response_chunk frames, then final EOF chunk."""
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)

    sse_chunks = [b"data: event1\n\n", b"data: event2\n\n", b"data: event3\n\n"]

    async def aiter_raw_gen():
        for c in sse_chunks:
            yield c

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.headers = {"content-type": "text/event-stream"}
    # _dispatch streams via resp.aiter_raw() (see the small-JSON test above).
    fake_resp.aiter_raw = aiter_raw_gen
    fake_resp.aclose = AsyncMock()
    fake_client = MagicMock()
    fake_client.build_request = MagicMock(return_value="REQ")
    fake_client.send = AsyncMock(return_value=fake_resp)

    # Mock the MCP manifest lookup so /mcp/file-tools/sse resolves to a port
    fake_server = MagicMock(port=8932)
    fake_manifest = MagicMock(server=fake_server)

    with patch.object(disp, "_get_client", return_value=fake_client), \
         patch("services.mcp.mcp_registry.get_manifest_by_config_key", return_value=fake_manifest):
        stream_id = str(uuid.uuid4())
        await disp.handle_request_frame(mgr, "m1", {
            "type": "http_request",
            "stream_id": stream_id,
            "method": "GET",
            "path": "/mcp/file-tools/sse",
            "headers": {},
            "body_b64": "",
            "body_eof": True,
            "timeout_s": 60,
        })

        # Wait for the streaming dispatch to complete (3 chunks + EOF + first frame)
        for _ in range(100):
            if conn.sent and conn.sent[-1].get("body_eof"):
                break
            await asyncio.sleep(0.01)

        # First frame: http_response with status+headers, body_eof=False
        first = conn.sent[0]
        assert first["type"] == "http_response"
        assert first["status"] == 200
        assert first["body_eof"] is False

        # Followed by chunks, each as http_response_chunk
        chunk_frames = [m for m in conn.sent[1:] if m["type"] == "http_response_chunk"]
        non_eof_chunks = [c for c in chunk_frames if not c.get("body_eof")]
        eof_chunks = [c for c in chunk_frames if c.get("body_eof")]
        # 3 SSE events as chunks + 1 EOF marker
        assert len(non_eof_chunks) == 3
        assert len(eof_chunks) == 1
        # Last frame is EOF
        assert conn.sent[-1]["body_eof"] is True

        # Decoded chunks should match input
        decoded = [base64.b64decode(c["body_b64"]) for c in non_eof_chunks]
        assert b"".join(decoded) == b"".join(sse_chunks)


@pytest.mark.asyncio
async def test_cancel_machine_streams_clears_all():
    """When a satellite deregisters, all its pending streams clean up."""
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)

    # Inject pending streams for two machines
    from core.remote.satellite_http_tunnel import _HttpStream
    s1 = _HttpStream(stream_id="s1", machine_id="m1")
    s2 = _HttpStream(stream_id="s2", machine_id="m1")
    s3 = _HttpStream(stream_id="s3", machine_id="m2")
    disp._streams[("m1", "s1")] = s1
    disp._streams[("m1", "s2")] = s2
    disp._streams[("m2", "s3")] = s3

    await disp.cancel_machine_streams(mgr, "m1")

    # m1 streams gone, m2 stream intact
    assert ("m1", "s1") not in disp._streams
    assert ("m1", "s2") not in disp._streams
    assert ("m2", "s3") in disp._streams
    # Cancel events were set
    assert s1.cancel_event.is_set()
    assert s2.cancel_event.is_set()
    assert not s3.cancel_event.is_set()


@pytest.mark.asyncio
async def test_request_chunk_to_unknown_stream_drops():
    """A http_request_chunk for a stream_id we don't track is dropped silently."""
    disp = SatelliteHttpTunnelDispatcher()
    # No streams registered
    disp.handle_request_chunk("m1", {
        "type": "http_request_chunk",
        "stream_id": "never-seen",
        "body_b64": "",
        "body_eof": True,
    })
    # No errors, no state mutation.
    assert disp._streams == {}


# ===== Upstream URL resolution =====

def test_resolve_hook_path_routes_to_proxy_loopback():
    """Hook paths route to the proxy's own loopback (Authentik bypass)."""
    url = _resolve_upstream_url("/v1/hooks/permission")
    assert url is not None
    assert url.startswith("http://localhost:")
    assert url.endswith("/v1/hooks/permission")


def test_resolve_mcp_path_returns_none_when_manifest_missing():
    """If the MCP slug isn't installed (manifest registry returns None),
    the resolver returns None so the dispatcher can synthesize a 404."""
    with patch("services.mcp.mcp_registry.get_manifest", return_value=None):
        url = _resolve_upstream_url("/mcp/nonexistent-mcp/sse")
        assert url is None


def test_resolve_mcp_path_routes_to_mcp_port():
    """For /mcp/<slug>/<rest>, the resolver hits the MCP's actual port
    from manifest.server.port."""
    fake_server = MagicMock(port=8932)
    fake_manifest = MagicMock(server=fake_server)
    with patch("services.mcp.mcp_registry.get_manifest_by_config_key", return_value=fake_manifest):
        url = _resolve_upstream_url("/mcp/file-tools/sse")
        assert url == "http://localhost:8932/sse"


def test_resolve_mcp_path_falls_back_to_server_name():
    """When the path slug doesn't match any manifest's `name`, the resolver
    falls back to scanning by `server_name`. This is how camoufox (manifest
    name = "camoufox", server_name = "platform") gets reached at
    /mcp/platform/... — without the fallback the dispatcher would 404.
    Mirrors the same fallback already in `_rewrite_mcp_json_for_remote`.
    """
    fake_server = MagicMock(port=8931)
    fake_manifest = MagicMock(server=fake_server, server_name="platform")
    with patch("services.mcp.mcp_registry.get_manifest", return_value=None), \
         patch.object(
             __import__("services.mcp.mcp_registry", fromlist=["_manifests"]),
             "_manifests",
             {"camoufox": fake_manifest},
         ):
        url = _resolve_upstream_url("/mcp/platform/mcp/")
        assert url == "http://localhost:8931/mcp/"


@pytest.mark.asyncio
async def test_dispatch_mcp_not_found_returns_404():
    """When the MCP isn't installed on the platform, dispatch sends 404."""
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)

    with patch("services.mcp.mcp_registry.get_manifest", return_value=None):
        stream_id = str(uuid.uuid4())
        await disp.handle_request_frame(mgr, "m1", {
            "type": "http_request",
            "stream_id": stream_id,
            "method": "GET",
            "path": "/mcp/ghost-mcp/sse",
            "headers": {},
            "body_b64": "",
            "body_eof": True,
            "timeout_s": 30,
        })

        for _ in range(50):
            if conn.sent:
                break
            await asyncio.sleep(0.01)

        assert len(conn.sent) == 1
        assert conn.sent[0]["status"] == 404
        assert conn.sent[0]["error"] == "mcp-not-found"


@pytest.mark.asyncio
async def test_dispatch_upstream_error_returns_502():
    """When httpx raises (upstream unreachable), the satellite gets a
    synthetic 502 with diagnostic error."""
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)

    fake_client = MagicMock()
    fake_client.build_request = MagicMock(return_value="REQ")
    fake_client.send = AsyncMock(side_effect=httpx.ConnectError("refused"))

    with patch.object(disp, "_get_client", return_value=fake_client):
        stream_id = str(uuid.uuid4())
        await disp.handle_request_frame(mgr, "m1", {
            "type": "http_request",
            "stream_id": stream_id,
            "method": "POST",
            "path": "/v1/hooks/permission",
            "headers": {},
            "body_b64": "",
            "body_eof": True,
            "timeout_s": 30,
        })

        for _ in range(50):
            if conn.sent:
                break
            await asyncio.sleep(0.01)

        assert len(conn.sent) == 1
        resp = conn.sent[0]
        assert resp["status"] == 502
        assert resp["error"] == "upstream-ConnectError"
        assert resp["body_eof"] is True


def test_allowlist_accepts_delegation_and_continuations():
    # Twin of the satellite-side test — the task-mcp split's new endpoints
    # must pass BOTH tunnels (missed hand-off, found live post-redeploy).
    assert _is_allowed_path("/v1/delegation/spawn")
    assert _is_allowed_path("/v1/delegation/sessions")
    assert _is_allowed_path("/v1/delegation/sessions/abc-123/peek")
    assert _is_allowed_path("/v1/continuations")
    assert not _is_allowed_path("/v1/delegationX")
    assert not _is_allowed_path("/v1/delegation/../admin")

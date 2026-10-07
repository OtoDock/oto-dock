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
    # Hook parity (satellite 0.5.121): the Stop hook on both engines and the
    # Codex question bridge — the satellite's own list admits the same two.
    assert _is_allowed_path("/v1/hooks/stop")
    assert _is_allowed_path("/v1/hooks/codex-question")
    import sys
    from tests._paths import REPO_ROOT
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from satellite.transport.http_tunnel import _is_allowed_path as _sat_allowed
    for path in ("/v1/hooks/stop", "/v1/hooks/codex-question", "/v1/hooks/subagent",
                 "/v1/hooks/permission", "/v1/hooks/tool-result"):
        assert _sat_allowed(path), path


def test_allowlist_accepts_the_checks_routes():
    """checks-mcp on a satellite session (CHECKS.md, 0.5.123): attached,
    attach, detach and run ride the tunnel; both lists admit them."""
    import sys
    from tests._paths import REPO_ROOT
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from satellite.transport.http_tunnel import _is_allowed_path as _sat_allowed
    for path in ("/v1/checks/attached", "/v1/checks/attach", "/v1/checks/detach", "/v1/checks/run"):
        assert _is_allowed_path(path), path
        assert _sat_allowed(path), path
    assert not _is_allowed_path("/v1/checks")
    assert not _is_allowed_path("/v1/checks/run/../admin")


def test_allowlist_accepts_artifact_and_app_hooks():
    """display-mcp on a satellite session: display_ui + the app tools —
    the live 403 path-not-allowlisted found on the first trusted-VM test
    (2026-07-10). Exact hook paths only; no wildcard under /v1/hooks/apps."""
    assert _is_allowed_path("/v1/hooks/ui")
    assert _is_allowed_path("/v1/hooks/apps/pin")
    assert _is_allowed_path("/v1/hooks/apps/unpin")
    assert _is_allowed_path("/v1/hooks/apps/list")
    # The live-apps hooks (found as a live 403 on the first T1 test of the
    # lane, 2026-09-12: the satellite session's tools reached the pin hook
    # and nothing else).
    assert _is_allowed_path("/v1/hooks/apps/push")
    assert _is_allowed_path("/v1/hooks/apps/state")
    assert _is_allowed_path("/v1/hooks/apps/describe")
    assert _is_allowed_path("/v1/hooks/apps/export")
    assert _is_allowed_path("/v1/hooks/apps/import")
    assert _is_allowed_path("/v1/hooks/apps/open")
    # The rendered check's pictures (APPS.md "Deploy pipeline").
    assert _is_allowed_path("/v1/hooks/apps/screenshot")
    # Releases (APPS.md "Releases and rollback").
    assert _is_allowed_path("/v1/hooks/apps/rollback")
    # Folder apps (APPS.md): the deploy family.
    for op in ("deploy", "check", "status", "preview", "logs", "restart", "purge"):
        assert _is_allowed_path(f"/v1/hooks/apps/{op}"), op
    assert not _is_allowed_path("/v1/hooks/apps")
    assert not _is_allowed_path("/v1/hooks/apps/evil")
    assert not _is_allowed_path("/v1/hooks/uiX")
    # Agents calling apps from a satellite sandbox (APPS.md): the app's own
    # API, the platform methods and the push/state twins — never the client
    # files, the socket bridge or the human routes.
    app = "0f4d7c2e-1b3a-4c5d-8e6f-7a8b9c0d1e2f"
    assert _is_allowed_path(f"/v1/apps/{app}/api/cards")
    assert _is_allowed_path(f"/v1/apps/{app}/api")
    assert _is_allowed_path(f"/v1/apps/{app}/platform/viewer.me")
    assert _is_allowed_path(f"/v1/apps/{app}/push")
    assert _is_allowed_path(f"/v1/apps/{app}/state")
    assert not _is_allowed_path(f"/v1/apps/{app}/client/abc/app.js")
    assert not _is_allowed_path(f"/v1/apps/{app}/ws/chat")
    assert not _is_allowed_path(f"/v1/apps/{app}/viewer-token")
    assert not _is_allowed_path(f"/v1/apps/{app}/approve")
    assert not _is_allowed_path(f"/v1/apps/{app}/api/../approve")
    assert not _is_allowed_path("/v1/apps/not-an-id/api/x")


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
async def test_dispatch_drops_the_forwarding_headers():
    """A machine never speaks for a client address: X-Forwarded-*,
    Forwarded and X-Real-IP never reach the platform through the tunnel."""
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)

    async def _fake_iter_raw():
        yield b"{}"

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.headers = {"content-type": "application/json"}
    fake_resp.aiter_raw = _fake_iter_raw
    fake_resp.aclose = AsyncMock()
    fake_client = MagicMock()
    fake_client.build_request = MagicMock(return_value="REQ")
    fake_client.send = AsyncMock(return_value=fake_resp)

    with patch.object(disp, "_get_client", return_value=fake_client):
        await disp.handle_request_frame(mgr, "m1", {
            "type": "http_request",
            "stream_id": str(uuid.uuid4()),
            "method": "POST",
            "path": "/v1/hooks/permission",
            "headers": {"Authorization": "Bearer xyz", "X-Forwarded-For": "203.0.113.7",
                        "x-forwarded-proto": "https", "Forwarded": "for=203.0.113.7",
                        "X-Real-IP": "203.0.113.7", "Content-Type": "application/json"},
            "body_b64": base64.b64encode(b"{}").decode(),
            "body_eof": True,
            "timeout_s": 30,
        })
        for _ in range(50):
            if fake_client.build_request.called:
                break
            await asyncio.sleep(0.01)

    sent = fake_client.build_request.call_args.kwargs["headers"]
    assert {k.lower() for k in sent} == {"authorization", "content-type"}


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

def test_resolve_hook_path_routes_to_the_internal_listener(monkeypatch):
    """Hook paths route to the proxy's own internal listener (no reverse
    proxy, no forwarding header read), or the main port when there is none."""
    import config
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", 45123)
    assert _resolve_upstream_url("/v1/hooks/permission") == \
        "http://127.0.0.1:45123/v1/hooks/permission"
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", 0)
    assert _resolve_upstream_url("/v1/hooks/permission") == \
        f"http://127.0.0.1:{config.PORT}/v1/hooks/permission"


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


@pytest.mark.asyncio
async def test_the_stop_hook_outlives_the_stream_clamp():
    """The Stop hook waits on the turn-end verdict (a default judge check's
    budget is past 15 min) and the two prompt hooks wait on a person who may
    leave the card parked for longer: their upstream reads are not cut at
    the clamp (the hook class has its own pool and cap). Every other hook
    keeps the clamp."""
    from core.remote import satellite_http_tunnel as tun

    reads: dict[str, float] = {}

    async def _run(path: str) -> None:
        disp = SatelliteHttpTunnelDispatcher()
        conn = FakeConnection()
        fake_client = MagicMock()

        def _build(method, url, **kw):
            reads[path] = kw["timeout"].read
            return "REQ"

        fake_client.build_request = MagicMock(side_effect=_build)
        fake_client.send = AsyncMock(side_effect=httpx.ConnectError("refused"))
        with patch.object(disp, "_get_client", return_value=fake_client):
            await disp.handle_request_frame(FakeManager(conn), "m1", {
                "type": "http_request",
                "stream_id": str(uuid.uuid4()),
                "method": "POST",
                "path": path,
                "headers": {},
                "body_b64": "",
                "body_eof": True,
                # What the satellite sends for a hook: the permission gate's week.
                "timeout_s": 604800,
            })
            for _ in range(50):
                if conn.sent:
                    break
                await asyncio.sleep(0.01)

    for path in ("/v1/hooks/stop", "/v1/hooks/permission",
                 "/v1/hooks/codex-question", "/v1/hooks/tool-result"):
        await _run(path)
    assert reads["/v1/hooks/stop"] == float(tun._STREAM_MAX_AGE_S)
    # A prompt waits on a person up to three days; its stream an hour more.
    assert reads["/v1/hooks/permission"] == float(tun._PROMPT_STREAM_MAX_S)
    assert reads["/v1/hooks/codex-question"] == float(tun._PROMPT_STREAM_MAX_S)
    assert reads["/v1/hooks/tool-result"] == float(tun._MAX_STREAM_TIMEOUT_S)


def test_allowlist_accepts_delegation_and_continuations():
    # Twin of the satellite-side test — the task-mcp split's new endpoints
    # must pass BOTH tunnels (missed hand-off, found live post-redeploy).
    assert _is_allowed_path("/v1/delegation/spawn")
    assert _is_allowed_path("/v1/delegation/sessions")
    assert _is_allowed_path("/v1/delegation/sessions/abc-123/peek")
    assert _is_allowed_path("/v1/continuations")
    assert not _is_allowed_path("/v1/delegationX")
    assert not _is_allowed_path("/v1/delegation/../admin")


# ===== Two clients, two stream classes =====


def _pool(client):
    return client._transport._pool


@pytest.mark.asyncio
async def test_mcp_and_hook_requests_go_to_their_own_clients(monkeypatch):
    """A tunneled ``/mcp/*`` stream is sent by the MCP client, everything
    else by the hook client; each pool carries its explicit limits, so a
    held MCP stream can never take a hook's connection."""
    from core.remote import satellite_http_tunnel as tun

    disp = SatelliteHttpTunnelDispatcher()
    mcp, hook = disp._get_client(True), disp._get_client(False)
    try:
        assert mcp is not hook
        assert _pool(mcp)._max_connections == tun._MCP_POOL_MAX
        assert _pool(mcp)._max_keepalive_connections == tun._MCP_POOL_MAX
        assert _pool(hook)._max_connections == tun._HOOK_POOL_MAX
        assert _pool(hook)._max_keepalive_connections == tun._HOOK_POOL_MAX
        assert tun._MCP_POOL_MAX > tun._MAX_MCP_STREAMS_TOTAL
    finally:
        await disp.shutdown()

    sent: dict[str, list[str]] = {"mcp": [], "hook": []}

    def _fake(kind):
        client = MagicMock()
        client.build_request = MagicMock(side_effect=lambda m, url, **kw: url)
        client.send = AsyncMock(
            side_effect=lambda req, stream=False: sent[kind].append(req) or
            (_ for _ in ()).throw(httpx.ConnectError("refused")))
        return client

    disp._mcp_client, disp._hook_client = _fake("mcp"), _fake("hook")
    monkeypatch.setattr(tun, "_resolve_upstream_url", lambda p: f"http://u{p}")
    conn = FakeConnection()
    for path in ("/mcp/file-tools/mcp?session_id=1", "/v1/hooks/permission"):
        await disp.handle_request_frame(FakeManager(conn), "m1", {
            "stream_id": str(uuid.uuid4()), "method": "POST", "path": path,
            "headers": {}, "body_b64": "", "body_eof": True, "timeout_s": 30,
        })
    for _ in range(50):
        if len(conn.sent) >= 2:
            break
        await asyncio.sleep(0.01)
    assert sent == {"mcp": ["http://u/mcp/file-tools/mcp?session_id=1"],
                    "hook": ["http://u/v1/hooks/permission"]}


@pytest.mark.asyncio
async def test_shutdown_closes_both_clients():
    disp = SatelliteHttpTunnelDispatcher()
    await disp.start()
    mcp, hook = disp._mcp_client, disp._hook_client
    assert mcp is not None and hook is not None and mcp is not hook
    await disp.shutdown()
    assert mcp.is_closed and hook.is_closed
    assert disp._mcp_client is None and disp._hook_client is None


class _SseUpstream:
    """A loopback HTTP server: every GET is an event stream that never ends,
    every POST is answered at once (a standing MCP GET and a hook)."""

    def __init__(self):
        self.server = None
        self.port = None
        self.writers: set = set()

    async def _handle(self, reader, writer):
        self.writers.add(writer)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            method = head.split(b" ", 1)[0]
            clen = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    clen = int(line.split(b":", 1)[1])
            if clen:
                await reader.readexactly(clen)
            if method == b"GET":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                             b"Transfer-Encoding: chunked\r\n\r\n")
                while True:
                    writer.write(b"7\r\n: ping\n\r\n")
                    await writer.drain()
                    await asyncio.sleep(0.2)
            body = b'{"decision":"allow"}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.writers.discard(writer)
            writer.close()

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        for w in list(self.writers):
            w.close()
        self.server.close()


@pytest.mark.asyncio
async def test_hooks_do_not_wait_behind_held_mcp_streams(monkeypatch):
    """The held-streams shape in small: with the MCP pool pinned to two connections
    and two standing MCP GETs held, a hook is answered at once through its
    own pool instead of waiting the pool timeout for a 502."""
    import time
    from core.remote import satellite_http_tunnel as tun

    monkeypatch.setattr(tun, "_MCP_POOL_MAX", 2)
    up = _SseUpstream()
    await up.start()
    monkeypatch.setattr(tun, "_resolve_upstream_url",
                        lambda p: f"http://127.0.0.1:{up.port}{p.split('?', 1)[0]}")
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)
    try:
        for i in range(2):
            await disp.handle_request_frame(mgr, "m1", {
                "stream_id": f"get-{i}", "method": "GET", "path": "/mcp/file-tools/mcp/",
                "headers": {"Accept": "text/event-stream"}, "body_b64": "",
                "body_eof": True, "timeout_s": 604800,
            })
        for _ in range(100):
            if sum(1 for s in disp._streams.values() if s.upstream_open) == 2:
                break
            await asyncio.sleep(0.02)
        assert sum(1 for s in disp._streams.values() if s.upstream_open) == 2
        assert _pool(disp._get_client(True))._max_connections == 2
        t0 = time.perf_counter()
        await disp.handle_request_frame(mgr, "m1", {
            "stream_id": "hook", "method": "POST", "path": "/v1/hooks/permission",
            "headers": {"Content-Type": "application/json"}, "body_b64": "e30=",
            "body_eof": True, "timeout_s": 604800,
        })
        for _ in range(200):
            if any(f.get("stream_id") == "hook" and f["type"] == "http_response"
                   for f in conn.sent):
                break
            await asyncio.sleep(0.01)
        first = next(f for f in conn.sent
                     if f.get("stream_id") == "hook" and f["type"] == "http_response")
        assert first["status"] == 200 and first.get("error") is None
        assert time.perf_counter() - t0 < 2.0
    finally:
        for key in list(disp._streams):
            disp._reap(key)
        await asyncio.sleep(0.05)
        await disp.shutdown()
        await up.stop()


# ===== The holder of a tunneled session token (D1) =====


@pytest.mark.asyncio
async def test_a_tunneled_request_from_a_gone_holder_is_refused(monkeypatch):
    """A session token naming a person who no longer exists is refused at
    the tunnel with a synthetic 401 before any upstream contact, on the
    brokered MCPs and on the callback MCPs alike; a living person's and an
    agent-scope token pass; the answer is cached for the TTL and the cache
    is bounded."""
    from auth import providers as auth_providers, token_holder
    from tests.conftest import live_session_token
    from core.remote import satellite_http_tunnel as tun

    token_holder.reset_for_tests()
    monkeypatch.setattr(tun, "_resolve_upstream_url", lambda p: "http://127.0.0.1:1/x")
    judged: list[str] = []
    real_judge = auth_providers.session_token_holder_ok

    def _judge(payload):
        judged.append(payload.get("user_sub") or "")
        return real_judge(payload)

    monkeypatch.setattr(auth_providers, "session_token_holder_ok", _judge)
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)
    fake_client = MagicMock()
    fake_client.build_request = MagicMock(return_value="REQ")
    fake_client.send = AsyncMock(side_effect=httpx.ConnectError("refused"))
    monkeypatch.setattr(disp, "_get_client", lambda is_mcp=False: fake_client)

    async def _ask(token: str, path: str) -> dict:
        n = len(conn.sent)
        await disp.handle_request_frame(mgr, "m1", {
            "stream_id": str(uuid.uuid4()), "method": "POST", "path": path,
            "headers": {"Authorization": f"Bearer {token}"}, "body_b64": "",
            "body_eof": True, "timeout_s": 30,
        })
        for _ in range(50):
            if len(conn.sent) > n:
                break
            await asyncio.sleep(0.01)
        return conn.sent[-1]

    ghost = live_session_token("sid-ghost", "pa", user_sub="user-nobody")
    for path in ("/mcp/github/mcp", "/mcp/file-tools/mcp/"):
        frame = await _ask(ghost, path)
        assert frame["status"] == 401 and frame["error"] == "session-holder-gone"
    fake_client.send.assert_not_called()
    assert judged == ["user-nobody"]            # the second ask hit the cache

    alive = live_session_token("sid-alive", "pa", user_sub="user-admin")
    frame = await _ask(alive, "/mcp/github/mcp")
    assert frame["error"] == "upstream-ConnectError"   # reached the client
    service = live_session_token("sid-svc", "pa")
    frame = await _ask(service, "/mcp/github/mcp")
    assert frame["error"] == "upstream-ConnectError"
    assert judged == ["user-nobody", "user-admin"]     # no person, no judgement
    assert fake_client.send.await_count == 2

    # A pass expires with the TTL, a refusal stands for the token's life (the
    # routes' rule, one cache), and the cache never outgrows its bound.
    monkeypatch.setattr(token_holder, "HOLDER_TTL_S", 0.0)
    monkeypatch.setattr(token_holder, "ANSWERS_MAX", 1)
    token_holder.reset_for_tests()
    await _ask(ghost, "/mcp/github/mcp")
    await _ask(ghost, "/mcp/github/mcp")
    await _ask(alive, "/mcp/github/mcp")
    await _ask(alive, "/mcp/github/mcp")
    assert judged == ["user-nobody", "user-admin",
                      "user-nobody", "user-admin", "user-admin"]
    assert len(token_holder._answers) <= 1


@pytest.mark.asyncio
async def test_the_holder_check_reads_the_bearer_as_the_routes_do(monkeypatch):
    """The routes accept the scheme in any case, so the tunnel reads it so
    too; a request with two Authorization headers (keys differing in case)
    is refused, since which one a server reads is its own choice."""
    from auth import token_holder
    from tests.conftest import live_session_token
    from core.remote import satellite_http_tunnel as tun

    token_holder.reset_for_tests()
    monkeypatch.setattr(tun, "_resolve_upstream_url", lambda p: "http://127.0.0.1:1/x")
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)
    fake_client = MagicMock()
    fake_client.build_request = MagicMock(return_value="REQ")
    fake_client.send = AsyncMock(side_effect=httpx.ConnectError("refused"))
    monkeypatch.setattr(disp, "_get_client", lambda is_mcp=False: fake_client)

    async def _ask(headers: dict) -> dict:
        n = len(conn.sent)
        await disp.handle_request_frame(mgr, "m1", {
            "stream_id": str(uuid.uuid4()), "method": "POST", "path": "/mcp/file-tools/mcp/",
            "headers": headers, "body_b64": "", "body_eof": True, "timeout_s": 30,
        })
        for _ in range(50):
            if len(conn.sent) > n:
                break
            await asyncio.sleep(0.01)
        return conn.sent[-1]

    ghost = live_session_token("sid-ghost", "pa", user_sub="user-nobody")
    for value in (f"bearer {ghost}", f"BEARER {ghost}", f"Bearer  {ghost}"):
        frame = await _ask({"authorization": value})
        assert frame["status"] == 401 and frame["error"] == "session-holder-gone", value
    frame = await _ask({"Authorization": "Basic x", "authorization": f"Bearer {ghost}"})
    assert frame["status"] == 400
    fake_client.send.assert_not_called()


class _RecordingUpstream:
    """A loopback HTTP server that records each request's head and answers
    a JSON-RPC result (a sidecar behind the gateway's forward)."""

    def __init__(self):
        self.server = None
        self.port = None
        self.heads: list[bytes] = []

    async def _handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            self.heads.append(head)
            clen = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    clen = int(line.split(b":", 1)[1])
            if clen:
                await reader.readexactly(clen)
            body = b'{"jsonrpc":"2.0","id":1,"result":{}}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Mcp-Session-Id: s-1\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()


async def _first_frame(conn, stream_id: str) -> dict:
    for _ in range(300):
        for f in conn.sent:
            if f.get("stream_id") == stream_id and f["type"] == "http_response":
                return f
        await asyncio.sleep(0.01)
    raise AssertionError("no response frame")


@pytest.mark.asyncio
async def test_a_credentialed_sidecar_request_is_forwarded_through_the_gateway(monkeypatch):
    """A tunneled ``/mcp/<name>/`` request of a session that holds a gateway
    credential for that sidecar reaches it with the credential added and the
    session token kept on the platform; one without a credential takes the
    direct hop as before."""
    import base64
    import uuid
    from core.credentials import mcp_broker, mcp_gateway
    from core.credentials.mcp_gateway import GatewayCredential
    from core.remote import satellite_http_tunnel as tun
    from storage.identity import bearer_allowlist
    from tests.conftest import live_session_token

    up = _RecordingUpstream()
    await up.start()
    sid = str(uuid.uuid4())
    provider = f"gw-{uuid.uuid4().hex[:6]}"
    bearer_allowlist.add_allowed(provider, "localhost", "test")
    mcp_broker.provision(sid, {"github-mcp": mcp_broker.SecretBundle(gateway=GatewayCredential(
        upstream=f"http://127.0.0.1:{up.port}", path="/mcp", allowlist_key=provider,
        value="ghp_real", proxy_local=True))})
    monkeypatch.setattr(tun, "_resolve_upstream_url",
                        lambda p: f"http://127.0.0.1:{up.port}{p.split('?', 1)[0]}")
    monkeypatch.setattr("auth.token_holder.holder_ok", _ok)
    token = live_session_token(sid, "agent", "user-1")
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)
    body = base64.b64encode(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}').decode()
    try:
        await disp.handle_request_frame(mgr, "m1", {
            "stream_id": "gh", "method": "POST", "path": "/mcp/github-mcp/mcp/?session_id=forged",
            "headers": {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                        "Mcp-Session-Id": "s-1"},
            "body_b64": body, "body_eof": True, "timeout_s": 30,
        })
        first = await _first_frame(conn, "gh")
        assert first["status"] == 200 and first.get("error") is None
        assert first["headers"].get("mcp-session-id") == "s-1"
        head = up.heads[-1].decode()
        assert "Authorization: Bearer ghp_real" in head
        assert token not in head and "forged" not in head and f"session_id={sid}" in head
        assert head.startswith("POST /mcp?")
        # the direct hop for a sidecar the session holds no credential for
        await disp.handle_request_frame(mgr, "m1", {
            "stream_id": "ft", "method": "POST", "path": "/mcp/file-tools/mcp/?session_id=x",
            "headers": {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            "body_b64": body, "body_eof": True, "timeout_s": 30,
        })
        first = await _first_frame(conn, "ft")
        assert first["status"] == 200
        assert f"Authorization: Bearer {token}" in up.heads[-1].decode()
    finally:
        mcp_broker.purge_session(sid)
        mcp_gateway.forget_memos()
        await disp.shutdown()
        await up.stop()


@pytest.mark.asyncio
async def test_a_vendor_credential_never_leaves_the_platform_through_the_tunnel(monkeypatch):
    """A machine session's vendor credential is pushed to the machine's own
    gateway; a ``/mcp/<name>/`` request for it answers a JSON-RPC refusal
    and no upstream is dialled."""
    import base64
    import uuid
    from core.credentials import mcp_broker, mcp_gateway
    from core.credentials.mcp_gateway import GatewayCredential
    from core.remote import satellite_http_tunnel as tun
    from tests.conftest import live_session_token

    up = _RecordingUpstream()
    await up.start()
    sid = str(uuid.uuid4())
    mcp_broker.provision(sid, {"vendor": mcp_broker.SecretBundle(gateway=GatewayCredential(
        upstream="https://mcp.example.com", path="/mcp", allowlist_key="x", value="xoxb"))})
    monkeypatch.setattr(tun, "_resolve_upstream_url",
                        lambda p: f"http://127.0.0.1:{up.port}{p.split('?', 1)[0]}")
    monkeypatch.setattr("auth.token_holder.holder_ok", _ok)
    token = live_session_token(sid, "agent", "user-1")
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)
    body = base64.b64encode(b'{"jsonrpc":"2.0","id":3,"method":"tools/list"}').decode()
    try:
        await disp.handle_request_frame(mgr, "m1", {
            "stream_id": "v", "method": "POST", "path": "/mcp/vendor/mcp/",
            "headers": {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            "body_b64": body, "body_eof": True, "timeout_s": 30,
        })
        first = await _first_frame(conn, "v")
        assert first["status"] == 200
        doc = json.loads(base64.b64decode(first["body_b64"]))
        assert doc["id"] == 3 and "machine's own gateway" in doc["error"]["message"]
        assert up.heads == []
    finally:
        mcp_broker.purge_session(sid)
        mcp_gateway.forget_memos()
        await disp.shutdown()
        await up.stop()


@pytest.mark.asyncio
async def test_a_credentialed_sidecar_with_no_credential_is_refused_not_hopped(monkeypatch):
    """A store miss (a session adopted with no gateway descriptor) for a
    sidecar whose manifest takes a credential answers the JSON-RPC refusal:
    the session token is never handed to the sidecar as its token and the
    upstream is never dialled. A sidecar that takes no credential keeps the
    direct hop, its session token forwarded as before."""
    import base64
    import uuid
    from types import SimpleNamespace
    from core.remote import satellite_http_tunnel as tun
    from tests.conftest import live_session_token

    credentialed = SimpleNamespace(credentials=SimpleNamespace(
        oauth={"bearer_required": True, "provider_id": "github"}, api_key_header=None))
    keyed = SimpleNamespace(credentials=SimpleNamespace(
        oauth=None, api_key_header={"name": "X-Key", "value_from": "K", "proposed_hosts": ["h"]}))
    plain = SimpleNamespace(credentials=SimpleNamespace(oauth=None, api_key_header=None))
    manifests = {"github-mcp": credentialed, "keyed-mcp": keyed, "file-tools": plain}
    monkeypatch.setattr("services.mcp.mcp_registry.get_manifest_by_config_key", manifests.get)
    up = _RecordingUpstream()
    await up.start()
    sid = str(uuid.uuid4())
    monkeypatch.setattr(tun, "_resolve_upstream_url",
                        lambda p: f"http://127.0.0.1:{up.port}{p.split('?', 1)[0]}")
    monkeypatch.setattr("auth.token_holder.holder_ok", _ok)
    token = live_session_token(sid, "agent", "user-1")
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)
    body = base64.b64encode(b'{"jsonrpc":"2.0","id":9,"method":"tools/list"}').decode()
    try:
        for stream_id, name in (("gh", "github-mcp"), ("kk", "keyed-mcp")):
            await disp.handle_request_frame(mgr, "m1", {
                "stream_id": stream_id, "method": "POST", "path": f"/mcp/{name}/mcp/",
                "headers": {"Authorization": f"Bearer {token}",
                            "Content-Type": "application/json"},
                "body_b64": body, "body_eof": True, "timeout_s": 30,
            })
            first = await _first_frame(conn, stream_id)
            assert first["status"] == 200
            doc = json.loads(base64.b64decode(first["body_b64"]))
            assert doc["id"] == 9 and "No credential" in doc["error"]["message"]
        assert up.heads == []
        await disp.handle_request_frame(mgr, "m1", {
            "stream_id": "ft", "method": "POST", "path": "/mcp/file-tools/mcp/",
            "headers": {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            "body_b64": body, "body_eof": True, "timeout_s": 30,
        })
        first = await _first_frame(conn, "ft")
        assert first["status"] == 200
        assert f"Authorization: Bearer {token}" in up.heads[-1].decode()
    finally:
        await disp.shutdown()
        await up.stop()


async def _ok(payload):
    return True


def test_the_two_tunnel_allowlists_are_the_same_list():
    """The proxy's and the satellite's allowlists are mirrored by hand: this
    compares them whole, pattern by pattern, so an entry added on one side
    fails here instead of on a machine."""
    import sys
    from tests._paths import REPO_ROOT
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from core.remote import satellite_http_tunnel as tun
    from satellite.transport import http_tunnel as sat
    proxy_patterns = [r.pattern for r in tun._ALLOWLIST_REGEXES]
    satellite_patterns = [r.pattern for r in sat._ALLOWLIST_REGEXES]
    assert proxy_patterns == satellite_patterns
    assert "/v1/mcp-gateway" not in "".join(proxy_patterns)


@pytest.mark.asyncio
async def test_a_tunneled_mcp_request_of_a_closed_or_earlier_life_is_refused(monkeypatch):
    """The tunnel's MCP path judges the session before the holder check and
    the bearer swap: a token of a session nothing holds, and one minted
    before its session's floor, answer 401 ``session-not-live`` and the
    upstream is never contacted; the current life's token passes."""
    import time as _time
    from auth import token_holder
    from auth.session_token import create_session_token
    from core.layers.direct.session import _direct_sessions
    from core.remote import satellite_http_tunnel as tun
    from core.session import session_state

    token_holder.reset_for_tests()
    monkeypatch.setattr(tun, "_resolve_upstream_url", lambda p: "http://127.0.0.1:1/x")
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)
    fake_client = MagicMock()
    fake_client.build_request = MagicMock(return_value="REQ")
    fake_client.send = AsyncMock(side_effect=httpx.ConnectError("refused"))
    monkeypatch.setattr(disp, "_get_client", lambda is_mcp=False: fake_client)

    async def _ask(token: str) -> dict:
        n = len(conn.sent)
        await disp.handle_request_frame(mgr, "m1", {
            "stream_id": str(uuid.uuid4()), "method": "POST", "path": "/mcp/file-tools/mcp/",
            "headers": {"Authorization": f"Bearer {token}"}, "body_b64": "",
            "body_eof": True, "timeout_s": 30,
        })
        for _ in range(50):
            if len(conn.sent) > n:
                break
            await asyncio.sleep(0.01)
        return conn.sent[-1]

    now = int(_time.time())
    held = str(uuid.uuid4())
    session_state.mark_starting(held, 60)
    session_state.register_session_state(held, "default", None, token_minted_at=now)
    _direct_sessions[held] = object()
    try:
        for token in (create_session_token(str(uuid.uuid4()), "pa", "user-admin"),
                      create_session_token(held, "pa", "user-admin", issued_at=now - 100)):
            frame = await _ask(token)
            assert frame["status"] == 401 and frame["error"] == "session-not-live"
        fake_client.send.assert_not_called()
        frame = await _ask(create_session_token(held, "pa", "user-admin", issued_at=now))
        assert frame["error"] == "upstream-ConnectError"      # reached the client
    finally:
        _direct_sessions.pop(held, None)
        session_state.cleanup_session_permission_state(held)

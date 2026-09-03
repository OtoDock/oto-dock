"""HTTP-over-WS tunnel — satellite-side integration tests.

Exercises the LocalTunnelServer end-to-end with a fake WS client that
captures outbound frames. Verifies subprocess HTTP calls get translated
correctly to http_request frames and that injected http_response frames
return the right response back to the caller.
"""

import asyncio
import base64
import json

import aiohttp
import pytest
import pytest_asyncio

from satellite.transport.http_tunnel import LocalTunnelServer


class FakeWSClient:
    """Stand-in for SatelliteWSClient — captures outbound frames."""

    def __init__(self):
        self._authenticated = True  # tunnel server checks this
        self.sent: list[dict] = []
        self.tunnel: LocalTunnelServer | None = None

    async def enqueue_send(self, msg: dict) -> None:
        self.sent.append(msg)


@pytest_asyncio.fixture
async def tunnel_pair():
    """Start a LocalTunnelServer + connected FakeWSClient. Yields both."""
    ws = FakeWSClient()
    tunnel = LocalTunnelServer(ws)
    ws.tunnel = tunnel
    port = await tunnel.start()
    yield ws, tunnel, port
    await tunnel.stop()


@pytest.mark.asyncio
async def test_rejects_non_allowlisted_path(tunnel_pair):
    """A request to a path not in the allowlist returns 403 immediately
    without sending any WS frame."""
    ws, tunnel, port = tunnel_pair
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{port}/v1/admin/users",
        ) as resp:
            assert resp.status == 403
            body = await resp.json()
            assert body["error"] == "path-not-allowlisted"
    # No frame was sent to the platform
    assert ws.sent == []


@pytest.mark.asyncio
async def test_rejects_traversal(tunnel_pair):
    """Path traversal must be rejected at the allowlist."""
    ws, tunnel, port = tunnel_pair
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{port}/v1/hooks/permission/../admin",
        ) as resp:
            assert resp.status == 403
    assert ws.sent == []


def test_audio_transcribe_is_allowlisted():
    """transcribe-mcp POSTs audio back through the tunnel to /v1/audio/transcribe —
    it must be allowlisted (only that exact endpoint, not all of /v1/audio/*)."""
    from satellite.transport.http_tunnel import _is_allowed_path
    assert _is_allowed_path("/v1/audio/transcribe")
    assert not _is_allowed_path("/v1/audio/tts/synthesize")


def test_audio_tts_generate_is_allowlisted():
    """tts-mcp reaches the voice-over endpoints through the tunnel — the four
    exact paths only, never the policy-gated chat synthesize or admin CRUD."""
    from satellite.transport.http_tunnel import _is_allowed_path
    assert _is_allowed_path("/v1/audio/tts/generate")
    assert _is_allowed_path("/v1/audio/tts/voices")
    assert _is_allowed_path("/v1/audio/tts/voices/search")
    assert _is_allowed_path("/v1/audio/tts/voices/add")
    assert not _is_allowed_path("/v1/audio/tts/synthesize")
    assert not _is_allowed_path("/v1/audio/tts/session")
    assert not _is_allowed_path("/v1/admin/audio/providers")


def test_artifact_and_app_hooks_are_allowlisted():
    """display-mcp on a satellite session: display_ui + the mini-app tools
    reach the proxy through the tunnel — the live 403 path-not-allowlisted
    on the first trusted-VM test (2026-07-10). Exact hook paths only."""
    from satellite.transport.http_tunnel import _is_allowed_path
    assert _is_allowed_path("/v1/hooks/ui")
    assert _is_allowed_path("/v1/hooks/apps/pin")
    assert _is_allowed_path("/v1/hooks/apps/unpin")
    assert _is_allowed_path("/v1/hooks/apps/list")
    assert not _is_allowed_path("/v1/hooks/apps")
    assert not _is_allowed_path("/v1/hooks/apps/evil")


def test_phone_relay_is_allowlisted():
    """phone-mcp reaches the proxy's phone relay through the tunnel — the
    /v1/phone/calls surface must be allowlisted (but not other /v1/phone/*
    admin endpoints like the usage reporter)."""
    from satellite.transport.http_tunnel import _is_allowed_path
    assert _is_allowed_path("/v1/phone/calls")
    assert _is_allowed_path("/v1/phone/calls/abc-123/wait")
    assert _is_allowed_path("/v1/phone/calls/abc-123/answer")
    assert not _is_allowed_path("/v1/phone/usage/turn-classifier")


@pytest.mark.asyncio
async def test_unauthenticated_returns_503(tunnel_pair):
    """If the WS isn't authenticated, hooks get 503 fast (no hang)."""
    ws, tunnel, port = tunnel_pair
    ws._authenticated = False  # simulate disconnected WS

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{port}/v1/hooks/permission",
            json={"tool_name": "Bash"},
        ) as resp:
            assert resp.status == 503
            body = await resp.json()
            assert body["error"] == "tunnel-not-connected"
    assert ws.sent == []


@pytest.mark.asyncio
async def test_small_json_round_trip(tunnel_pair):
    """A subprocess POST gets translated to a http_request frame; injecting
    a matching http_response returns the body to the subprocess."""
    ws, tunnel, port = tunnel_pair

    # Start the subprocess-side request as a task so we can inject the
    # response into the stream while it's waiting.
    async def make_request():
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/v1/hooks/permission",
                json={"tool_name": "Bash"},
            ) as resp:
                return resp.status, await resp.read()

    req_task = asyncio.create_task(make_request())

    # Wait for the tunnel to send the http_request frame
    for _ in range(100):
        if ws.sent:
            break
        await asyncio.sleep(0.01)
    assert len(ws.sent) == 1
    sent = ws.sent[0]
    assert sent["type"] == "http_request"
    assert sent["method"] == "POST"
    assert sent["path"] == "/v1/hooks/permission"
    assert sent["body_eof"] is True
    decoded_body = base64.b64decode(sent["body_b64"])
    assert json.loads(decoded_body) == {"tool_name": "Bash"}
    stream_id = sent["stream_id"]

    # Inject the response into the tunnel's pending stream
    tunnel.dispatch_response({
        "type": "http_response",
        "stream_id": stream_id,
        "status": 200,
        "headers": {"Content-Type": "application/json"},
        "body_b64": base64.b64encode(b'{"decision":"allow"}').decode(),
        "body_eof": True,
        "error": None,
    })

    status, body = await asyncio.wait_for(req_task, timeout=5)
    assert status == 200
    assert json.loads(body) == {"decision": "allow"}


@pytest.mark.asyncio
async def test_sse_streaming_round_trip(tunnel_pair):
    """SSE response (first frame + chunks) streams correctly to the subprocess."""
    ws, tunnel, port = tunnel_pair

    async def make_sse_request():
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{port}/mcp/file-tools/sse",
            ) as resp:
                assert resp.status == 200
                assert "text/event-stream" in resp.headers["Content-Type"]
                # Read the full streamed body
                return await resp.read()

    req_task = asyncio.create_task(make_sse_request())

    # Wait for the tunnel to send the http_request frame
    for _ in range(100):
        if ws.sent:
            break
        await asyncio.sleep(0.01)
    stream_id = ws.sent[0]["stream_id"]

    # First frame: status+headers, body_eof=False
    tunnel.dispatch_response({
        "type": "http_response",
        "stream_id": stream_id,
        "status": 200,
        "headers": {"Content-Type": "text/event-stream"},
        "body_b64": "",
        "body_eof": False,
        "error": None,
    })

    # Three chunks
    chunks = [b"data: a\n\n", b"data: b\n\n", b"data: c\n\n"]
    for chunk in chunks:
        tunnel.dispatch_response({
            "type": "http_response_chunk",
            "stream_id": stream_id,
            "body_b64": base64.b64encode(chunk).decode(),
            "body_eof": False,
        })

    # Final EOF marker
    tunnel.dispatch_response({
        "type": "http_response_chunk",
        "stream_id": stream_id,
        "body_b64": "",
        "body_eof": True,
    })

    body = await asyncio.wait_for(req_task, timeout=5)
    assert body == b"".join(chunks)


@pytest.mark.asyncio
async def test_fail_all_streams_on_reconnect(tunnel_pair):
    """When fail_all_streams() is called (simulating WS disconnect), every
    pending subprocess request gets a 502 instead of hanging."""
    ws, tunnel, port = tunnel_pair

    async def make_request():
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/v1/hooks/permission",
                json={},
            ) as resp:
                return resp.status, await resp.json()

    req_task = asyncio.create_task(make_request())

    # Wait for the request to be in-flight (frame sent, waiting for response)
    for _ in range(100):
        if ws.sent and len(tunnel._streams) > 0:
            break
        await asyncio.sleep(0.01)

    # Simulate WS disconnect — fail all streams
    tunnel.fail_all_streams("tunnel-disconnected")

    status, body = await asyncio.wait_for(req_task, timeout=5)
    assert status == 502
    assert body["error"] == "tunnel-disconnected"


@pytest.mark.asyncio
async def test_dispatch_response_to_unknown_stream_drops(tunnel_pair):
    """A response with a stream_id we don't track is dropped silently
    (e.g., late arrival after timeout)."""
    ws, tunnel, port = tunnel_pair

    # No request in flight — inject anyway
    tunnel.dispatch_response({
        "type": "http_response",
        "stream_id": "ghost",
        "status": 200,
        "body_b64": "",
        "body_eof": True,
    })
    # No crash, no state change
    assert tunnel._streams == {}


@pytest.mark.asyncio
async def test_large_body_not_rejected(tunnel_pair):
    """A request body over aiohttp's 1 MB default must NOT be rejected with
    413 at ingestion — regression for the display-image "tunnel-handler-error"
    500 (a screenshot's base64 body crossed 1 MB → HTTPRequestEntityTooLarge
    at request.read()). The body is read in full and re-chunked into
    http_request_chunk frames; the injected response returns to the caller."""
    ws, tunnel, port = tunnel_pair

    # 2 MB + change — comfortably past the old 1 MB cap, spans many chunks.
    payload = b"P" * (2 * 1024 * 1024 + 7)

    async def make_request():
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/v1/hooks/images",
                data=payload,
            ) as resp:
                return resp.status, await resp.read()

    req_task = asyncio.create_task(make_request())

    # Wait until every request frame is on the WS (the http_request opener
    # plus the continuation chunks, the last with body_eof=True).
    for _ in range(200):
        if ws.sent and ws.sent[-1].get("body_eof") is True:
            break
        await asyncio.sleep(0.01)

    # Large body → chunked: opener carries no inline body.
    assert ws.sent[0]["type"] == "http_request"
    assert ws.sent[0]["path"] == "/v1/hooks/images"
    assert ws.sent[0]["body_eof"] is False
    stream_id = ws.sent[0]["stream_id"]

    # Reassembling from the chunk frames proves the FULL 2 MB was read past
    # the old cap (not rejected/truncated).
    reassembled = b"".join(
        base64.b64decode(f["body_b64"])
        for f in ws.sent
        if f["type"] == "http_request_chunk" and f["body_b64"]
    )
    assert reassembled == payload

    # Injecting a response completes the round-trip with a normal status.
    tunnel.dispatch_response({
        "type": "http_response",
        "stream_id": stream_id,
        "status": 200,
        "headers": {"Content-Type": "application/json"},
        "body_b64": base64.b64encode(b'{"status":"ok"}').decode(),
        "body_eof": True,
        "error": None,
    })

    status, body = await asyncio.wait_for(req_task, timeout=5)
    assert status == 200
    assert json.loads(body) == {"status": "ok"}


def test_delegation_and_continuations_are_allowlisted():
    # delegation-mcp (spawn/list/peek) + schedules-mcp schedule_continuation
    # ride the tunnel from satellite sessions — missed at the task-mcp split
    # (live 403 path-not-allowlisted on the first post-redeploy delegate).
    from satellite.transport.http_tunnel import _is_allowed_path
    assert _is_allowed_path("/v1/delegation/spawn")
    assert _is_allowed_path("/v1/delegation/sessions")
    assert _is_allowed_path("/v1/delegation/sessions/abc-123/peek")
    assert _is_allowed_path("/v1/continuations")
    assert not _is_allowed_path("/v1/delegationX")
    assert not _is_allowed_path("/v1/delegation/../admin")

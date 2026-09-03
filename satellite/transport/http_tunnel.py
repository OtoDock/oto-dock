"""Local HTTP server that tunnels hook + MCP HTTP traffic over the WS.

The satellite-spawned CLI / Codex / MCP subprocesses make HTTP calls to
the platform (permission hooks, tool result forwarding, Docker MCP SSE).
Instead of routing those over a public network, we run a local aiohttp
server on 127.0.0.1:<ephemeral> and rewrite PROXY_URL to point at it. The server multiplexes each request as an
``http_request`` WebSocket frame back to the platform, awaits the response,
and streams it back to the subprocess.

Architecture:

    subprocess (urllib, sync)
        │ HTTP to 127.0.0.1:<port>
        ▼
    aiohttp request handler (this module)
        │ allocate stream_id, send http_request frame
        ▼
    ws_client.enqueue_send → platform tunnel server
                                  │
                                  ▼
                                upstream platform endpoint
                                  │
                                  ▼
                          http_response[_chunk] frames back
        │
        ▼
    aiohttp response writer streams to subprocess
"""

import asyncio
import base64
import contextlib
import json
import logging
import re
import socket
import uuid
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from .ws_client import SatelliteWSClient

logger = logging.getLogger("satellite.tunnel")


# Path allowlist enforced on the SATELLITE side as defense-in-depth.
# The platform allowlist mirrors this; both must agree before a request
# can reach an internal endpoint. Anchored ^...$ to defeat traversal.
_ALLOWLIST_REGEXES = [
    re.compile(
        r"^/v1/hooks/(resolve-path|resolve-tool-arg-paths|permission|"
        r"mcp-credentials|session-files|"
        r"images|image-generating|image-gen-failed|url|file|media|ui|"
        r"document-preview|tool-result|file-written|subagent)$"
    ),
    # display-mcp pinned mini-apps (pin/unpin/list) — session-JWT gated
    # proxy-side like every hook (verify_session_match + scope from the
    # session ctx); the artifact hook itself is the `ui` entry above.
    re.compile(r"^/v1/hooks/apps/(pin|unpin|list)$"),
    # display-mcp Dock file pins — same session-JWT gating; content is read
    # dashboard-side via the files API (the platform mirror for remotes).
    re.compile(r"^/v1/hooks/files/(pin|unpin)$"),
    re.compile(r"^/v1/location/request$"),
    # Temp-URL mint endpoint for image-search-mcp.search_by_image (SerpAPI
    # Google Lens requires a public URL — the MCP requests a tokenized one
    # via this endpoint, then SerpAPI fetches the public GET counterpart).
    re.compile(r"^/v1/images/temp$"),
    # Audio file transcription for transcribe-mcp (the satellite-side MCP POSTs
    # the audio here; the proxy runs STT + records usage). Session-JWT gated.
    re.compile(r"^/v1/audio/transcribe$"),
    # Voice-over generation + voice discovery for tts-mcp (exact paths — never
    # all of /v1/audio/*; voices/add is additionally admin-gated proxy-side).
    re.compile(r"^/v1/audio/tts/(generate|voices|voices/search|voices/add)$"),
    # phone-mcp → proxy phone relay (originate/wait/answer/status; the proxy
    # forwards to the phone daemon). Session-JWT + phone-mcp-assignment gated.
    re.compile(r"^/v1/phone/calls(/.*)?$"),
    # Platform-management stdio MCPs (notifications/task/meetings/triggers/
    # memory/mcps/agent-config) call these back via the framework-standard
    # PROXY_URL (remote-rewritten to the loopback tunnel). verify_session_match
    # still gates each by the session JWT. Keep BOTH allowlists identical.
    re.compile(r"^/v1/session/current$"),
    re.compile(r"^/v1/notifications(/.*)?$"),
    re.compile(r"^/v1/tasks(/.*)?$"),
    # delegation-mcp (spawn/sessions/peek) + schedules-mcp continuations —
    # both endpoints are session-JWT gated proxy-side (spawn_authz et al.).
    re.compile(r"^/v1/delegation(/.*)?$"),
    re.compile(r"^/v1/continuations(/.*)?$"),
    re.compile(r"^/v1/meetings(/.*)?$"),
    re.compile(r"^/v1/triggers(/.*)?$"),
    re.compile(r"^/v1/subscriptions$"),
    re.compile(r"^/v1/internal/memory(/.*)?$"),
    re.compile(r"^/v1/agents/[a-zA-Z0-9_-]+(/.*)?$"),
    re.compile(r"^/v1/community/mcps$"),
    # mcps-mcp skills catalog listing (2026-08-27; the per-agent skills +
    # request routes ride the /v1/agents wildcard above).
    re.compile(r"^/v1/community/skills$"),
    re.compile(r"^/v1/execution-layers$"),
    re.compile(r"^/mcp/[a-z0-9_-]+/.*$"),
]


# Response queue capacity per stream. 64 chunks × 256KB max-per-chunk = 16MB
# headroom per stream before backpressure kicks in (queue.put blocks).
_RESPONSE_QUEUE_SIZE = 64


def _has_traversal(base: str) -> bool:
    """True if ``base`` has a dot-segment or encoded separator. Rejected before
    allowlist matching so a `../`-style path can't match an allowlisted prefix
    and then be normalized onto a different endpoint upstream. Mirrors the
    platform side (``core/remote/satellite_http_tunnel.py``) — keep both identical.
    """
    low = base.lower()
    if "%2e" in low or "%2f" in low or "%5c" in low or "\\" in base:
        return True
    return any(seg in (".", "..") for seg in base.split("/"))


def _is_allowed_path(path: str) -> bool:
    base = path.split("?", 1)[0]
    if _has_traversal(base):
        return False
    return any(rx.match(base) for rx in _ALLOWLIST_REGEXES)


def _pick_local_port() -> int:
    """Pick an ephemeral 127.0.0.1 port via the OS, return it.

    Binds + immediately closes to grab the port; the actual aiohttp bind
    re-binds to the same port. There's a tiny race where another process
    could grab the port between bind/close and aiohttp's bind — acceptable
    in practice on a single-user laptop.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _PendingStream:
    """Per-tunnel-request state on the satellite side.

    The aiohttp handler awaits on this queue for response frames. The
    ws_client deposits frames into it as they arrive over the WS.
    """

    def __init__(self) -> None:
        self.response_queue: asyncio.Queue = asyncio.Queue(
            maxsize=_RESPONSE_QUEUE_SIZE
        )


class LocalTunnelServer:
    """aiohttp server bound to 127.0.0.1:<ephemeral> that tunnels HTTP
    requests over the satellite's WS connection.
    """

    def __init__(self, ws_client: "SatelliteWSClient") -> None:
        self.ws_client = ws_client
        self.port: int = 0
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        # stream_id → _PendingStream
        self._streams: dict[str, _PendingStream] = {}

    async def start(self) -> int:
        """Bind aiohttp to 127.0.0.1:<ephemeral>. Returns the chosen port."""

        # Generous body cap. Inline image/media hook POSTs (display-mcp →
        # /v1/hooks/images base64-encode file bytes) routinely exceed
        # aiohttp's 1 MB default `client_max_size`, which raised
        # HTTPRequestEntityTooLarge at `request.read()` → surfaced to the
        # agent as a 500 "tunnel-handler-error" for any display image whose
        # resized/encoded body crossed ~1 MB. The body is chunked into
        # ≤256 KB WS frames downstream (`_send_request_frames`), so a large
        # read here is only a transient in-memory buffer, never a wire limit.
        app = web.Application(client_max_size=128 * 1024 * 1024)
        # Match all paths via wildcard; allowlist enforcement happens in
        # the handler.
        app.router.add_route("*", "/{tail:.*}", self._handle_request)

        self._runner = web.AppRunner(app, handle_signals=False)
        await self._runner.setup()

        # Pick port via dedicated socket, then bind aiohttp to it.
        port = _pick_local_port()
        self._site = web.TCPSite(self._runner, "127.0.0.1", port)
        await self._site.start()
        self.port = port

        logger.info("Local tunnel server listening on 127.0.0.1:%d", port)
        return port

    async def stop(self) -> None:
        """Shut down the aiohttp server and reject all pending streams."""
        # Reject pending streams first so handlers wake up and return.
        for stream_id, stream in list(self._streams.items()):
            with contextlib.suppress(asyncio.QueueFull):
                stream.response_queue.put_nowait({
                    "type": "http_response",
                    "status": 503,
                    "error": "tunnel-shutting-down",
                    "headers": {},
                    "body_b64": "",
                    "body_eof": True,
                })
        self._streams.clear()

        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()
        self._site = None
        self._runner = None

    def dispatch_response(self, msg: dict) -> None:
        """Route an inbound http_response/http_response_chunk to the
        matching pending stream. Called from ws_client's inbound dispatcher.
        """
        stream_id = msg.get("stream_id", "")
        if not stream_id:
            return
        stream = self._streams.get(stream_id)
        if stream is None:
            # Unknown stream — likely already timed out / disconnected.
            logger.debug(
                "tunnel: dropping response for unknown stream %s",
                stream_id[:8],
            )
            return
        try:
            stream.response_queue.put_nowait(msg)
        except asyncio.QueueFull:
            logger.warning(
                "tunnel: response queue full for stream %s; dropping chunk",
                stream_id[:8],
            )

    def fail_all_streams(self, reason: str = "tunnel-disconnected") -> None:
        """Reject every pending stream with a synthetic 502.

        Called when the WS reconnects — every in-flight tunneled request
        gets a clean failure rather than hanging until timeout.
        """
        for stream_id, stream in list(self._streams.items()):
            with contextlib.suppress(asyncio.QueueFull):
                stream.response_queue.put_nowait({
                    "type": "http_response",
                    "status": 502,
                    "error": reason,
                    "headers": {},
                    "body_b64": "",
                    "body_eof": True,
                })

    # --- Request handling ---

    async def _handle_request(self, request: web.Request) -> web.StreamResponse:
        """aiohttp handler — tunnels one HTTP request over the WS."""
        path = request.path
        # Preserve query string for upstream
        if request.query_string:
            path_with_query = f"{path}?{request.query_string}"
        else:
            path_with_query = path

        # Defense-in-depth: reject non-allowlisted paths without ever
        # sending a frame to the platform. Codex CLI specifically probes
        # ``/.well-known/oauth-authorization-server*`` on every MCP at
        # connect time (RFC 8414 OAuth discovery) — that's 3 paths × N
        # MCPs per turn, all expected to 403 because none of our MCPs
        # implement OAuth discovery. Log those at debug so they don't
        # flood the WARN log; everything else stays at warning.
        if not _is_allowed_path(path):
            if "/.well-known/oauth-authorization-server" in path:
                logger.debug("tunnel: ignoring OAuth-discovery probe: %s", path)
            else:
                logger.warning("tunnel: rejected non-allowlisted path: %s", path)
            return web.Response(
                status=403,
                body=json.dumps({"error": "path-not-allowlisted"}).encode(),
                content_type="application/json",
            )

        # If we're not authenticated to the platform, fail fast with 503
        # so the hook script doesn't hang.
        if not self.ws_client._authenticated:
            return web.Response(
                status=503,
                body=json.dumps({"error": "tunnel-not-connected"}).encode(),
                content_type="application/json",
            )

        stream_id = str(uuid.uuid4())
        stream = _PendingStream()
        self._streams[stream_id] = stream

        try:
            # Read the full request body into memory (bounded by the app's
            # client_max_size, set generously above so large inline image/media
            # hook POSTs aren't rejected). It's re-chunked onto the WS below.
            body = await request.read()

            # Tunnel path translation for MCP requests: the agent on the
            # satellite uses native paths (Windows / macOS), but the
            # proxy-side MCPs validate against the platform-host agent
            # tree. Rewrite any satellite-host paths inside the synced
            # agent tree to their sandbox-virtual equivalents
            # (/workspace/..., /users/X/..., /knowledge/..., /config/...)
            # before forwarding. Paths outside the synced tree pass
            # through unchanged — those MCPs will reject the path, which
            # is the current expected limitation (see prompt guidance).
            if path.startswith("/mcp/") and body:
                try:
                    from ..host import path_translator
                    body_text = body.decode("utf-8", errors="strict")
                    rewritten = path_translator.translate_satellite_to_virtual_in_text(
                        body_text,
                        agents_dir=self.ws_client.config.agents_dir,
                    )
                    if rewritten != body_text:
                        body = rewritten.encode("utf-8")
                except (UnicodeDecodeError, Exception):  # noqa: BLE001
                    # Non-UTF-8 body or unexpected failure: forward as-is.
                    pass

            # Strip hop-by-hop headers before forwarding.
            headers = {
                k: v for (k, v) in request.headers.items()
                if k.lower() not in _HOP_BY_HOP_HEADERS
            }

            # Honor the longest hook timeout (permission_gate uses 7 days).
            timeout_header = request.headers.get("X-Tunnel-Timeout-S")
            timeout_s = 604800  # 7 days default to cover the permission case
            try:
                if timeout_header:
                    timeout_s = max(1, int(timeout_header))
            except ValueError:
                pass

            # Frame and send the request.
            # Inline body if ≤ 256KB; else chunk continuation.
            await self._send_request_frames(
                stream_id, request.method, path_with_query,
                headers, body, timeout_s,
            )

            # Await the first response frame (status + headers + maybe body).
            try:
                first = await asyncio.wait_for(
                    stream.response_queue.get(),
                    timeout=min(timeout_s + 5, 604800 + 60),
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "tunnel: stream %s timed out waiting for first frame",
                    stream_id[:8],
                )
                return web.Response(
                    status=504,
                    body=json.dumps({
                        "error": "tunnel-no-response",
                    }).encode(),
                    content_type="application/json",
                )

            if first.get("error"):
                # Synthetic failure from the platform side (allowlist deny,
                # upstream connect failure, etc.)
                return web.Response(
                    status=first.get("status", 502),
                    body=json.dumps({"error": first["error"]}).encode(),
                    content_type="application/json",
                )

            status = first.get("status", 200)
            resp_headers = first.get("headers", {})
            # Strip hop-by-hop from upstream response too
            resp_headers = {
                k: v for (k, v) in resp_headers.items()
                if k.lower() not in _HOP_BY_HOP_HEADERS
            }

            body_b64 = first.get("body_b64", "")
            initial_body = base64.b64decode(body_b64) if body_b64 else b""
            body_eof = first.get("body_eof", False)

            if body_eof:
                # Single-frame response — return a normal aiohttp Response.
                return web.Response(
                    status=status,
                    headers=resp_headers,
                    body=initial_body,
                )

            # Streaming response: prepare StreamResponse and pump chunks.
            stream_resp = web.StreamResponse(
                status=status, headers=resp_headers,
            )
            await stream_resp.prepare(request)
            if initial_body:
                await stream_resp.write(initial_body)

            while True:
                try:
                    chunk = await asyncio.wait_for(
                        stream.response_queue.get(),
                        timeout=timeout_s + 5,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "tunnel: stream %s timed out mid-stream",
                        stream_id[:8],
                    )
                    break

                if chunk.get("error"):
                    logger.warning(
                        "tunnel: stream %s aborted mid-stream: %s",
                        stream_id[:8], chunk.get("error"),
                    )
                    break

                chunk_b64 = chunk.get("body_b64", "")
                if chunk_b64:
                    try:
                        await stream_resp.write(base64.b64decode(chunk_b64))
                    except ConnectionResetError:
                        # Subprocess closed the connection mid-stream.
                        # Tell the platform via abort frame (future work).
                        break

                if chunk.get("body_eof"):
                    break

            # Client (claude-code / hook script) already closed the
            # socket on its end — nothing left to flush. Common for
            # streamable-http MCPs (camoufox) when the client times
            # out waiting for a slow first response. Not an error.
            with contextlib.suppress(ConnectionResetError, ConnectionError):
                await stream_resp.write_eof()
            return stream_resp

        except (ConnectionResetError, ConnectionError) as e:
            # Same race for non-streaming responses: client gave up
            # before we could write the response. Log at debug — there's
            # nothing to do and the connection is gone anyway.
            logger.debug(
                "tunnel: client disconnected mid-handler for %s: %s",
                path, e,
            )
            return web.Response(
                status=499,  # nginx-style "client closed request"
                body=b"",
            )
        except Exception:
            logger.exception("tunnel: handler error for %s", path)
            return web.Response(
                status=500,
                body=json.dumps({"error": "tunnel-handler-error"}).encode(),
                content_type="application/json",
            )
        finally:
            self._streams.pop(stream_id, None)

    async def _send_request_frames(
        self,
        stream_id: str,
        method: str,
        path: str,
        headers: dict,
        body: bytes,
        timeout_s: int,
    ) -> None:
        """Frame an HTTP request as one or more WS messages.

        Body ≤ 256 KB inlined in the first frame with body_eof=True.
        Larger bodies use http_request (body_eof=False, no body) followed
        by http_request_chunk frames.
        """
        if len(body) <= _MAX_FRAME_BODY:
            await self.ws_client.enqueue_send({
                "type": "http_request",
                "stream_id": stream_id,
                "method": method,
                "path": path,
                "headers": headers,
                "body_b64": base64.b64encode(body).decode(),
                "body_eof": True,
                "timeout_s": timeout_s,
            })
            return

        # Chunked request body
        await self.ws_client.enqueue_send({
            "type": "http_request",
            "stream_id": stream_id,
            "method": method,
            "path": path,
            "headers": headers,
            "body_b64": "",
            "body_eof": False,
            "timeout_s": timeout_s,
        })
        offset = 0
        while offset < len(body):
            chunk = body[offset:offset + _MAX_FRAME_BODY]
            offset += _MAX_FRAME_BODY
            await self.ws_client.enqueue_send({
                "type": "http_request_chunk",
                "stream_id": stream_id,
                "body_b64": base64.b64encode(chunk).decode(),
                "body_eof": offset >= len(body),
            })


# Max body bytes per WS frame. Well under the 16 MB ws frame limit;
# base64 + JSON overhead keeps the encoded frame ~340 KB on the wire.
_MAX_FRAME_BODY = 256 * 1024


# Hop-by-hop headers per RFC 7230 — must not be forwarded.
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",  # re-derived by receiving framework
}

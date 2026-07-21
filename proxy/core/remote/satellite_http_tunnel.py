"""Platform-side dispatcher for the HTTP-over-WS tunnel.

When a satellite-spawned subprocess hits its local tunnel server with a
hook callback or Docker MCP HTTP call, the satellite frames it as an
``http_request`` WS message and sends it to the platform. This module
receives those frames, dispatches them to the platform's internal HTTP
endpoints via ``httpx.AsyncClient`` (loopback — bypasses Authentik), and
streams the response back as ``http_response`` / ``http_response_chunk``
frames.

Defense-in-depth: a strict allowlist gates which paths can be tunneled,
mirrored on the satellite side. Any non-allowlisted path returns a
synthetic 403 without touching the upstream endpoint.

Streaming semantics:
- Hooks return small JSON: single ``http_response`` frame with ``body_eof=true``.
- Docker MCP SSE: ``httpx.stream(...)`` plus ``aiter_bytes()`` produces
  chunked ``http_response_chunk`` frames until the upstream closes.

Reconnect semantics: on satellite ``deregister``, every pending stream
gets a synthetic 502 + ``body_eof=true`` so the subprocess sees a clean
failure rather than hanging until timeout.
"""

import asyncio
import base64
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field

import httpx

import config

logger = logging.getLogger("claude-proxy.satellite-tunnel")


# Allowlist mirrored from satellite/http_tunnel.py. Both sides must agree.
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
    re.compile(r"^/v1/execution-layers$"),
    re.compile(r"^/mcp/[a-z0-9_-]+/.*$"),
]

# Hop-by-hop headers — never forward upstream or downstream.
_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# Chunk body cap on the wire (matches satellite/http_tunnel.py).
_MAX_FRAME_BODY = 256 * 1024

# Per-stream request body queue cap. Sized for sustained uploads
# (5 MB attachment ÷ 256 KB ≈ 20 chunks).
_REQUEST_QUEUE_SIZE = 64

# Stream sweep interval and slack — leaked streams (request without EOF)
# get cleaned up `_STREAM_GRACE_S` past their declared timeout.
_STREAM_SWEEP_INTERVAL = 60
_STREAM_GRACE_S = 30


def _has_traversal(base: str) -> bool:
    """True if ``base`` contains a dot-segment or an encoded separator.

    httpx collapses ``../`` (RFC-3986 remove_dot_segments) when it builds the
    upstream request, so a frame path that MATCHES an allowlisted prefix could
    be forwarded to a DIFFERENT endpoint (``/v1/tasks/../admin/users`` matches
    ``^/v1/tasks(/.*)?$`` but is sent to ``/admin/users``). We REJECT any
    traversal up front so the matched path is byte-identical to the forwarded
    path — normalization can never diverge.
    """
    low = base.lower()
    if "%2e" in low or "%2f" in low or "%5c" in low or "\\" in base:
        return True
    return any(seg in (".", "..") for seg in base.split("/"))


def _is_allowed_path(path: str) -> bool:
    # Strip query string for matching
    base = path.split("?", 1)[0]
    if _has_traversal(base):
        return False
    return any(rx.match(base) for rx in _ALLOWLIST_REGEXES)


# Matches `/mcp/<name>/<rest>` so the dispatcher can resolve `<name>` → the
# Docker MCP's actual localhost port via the manifest registry.
_MCP_PATH_RE = re.compile(r"^/mcp/([a-z0-9_-]+)(/.*)?$")


def _resolve_upstream_url(path: str) -> str | None:
    """Resolve a tunneled path to the upstream URL on the platform.

    - ``/v1/hooks/*`` and ``/v1/location/request`` → ``http://localhost:{PORT}{path}``
      (call the proxy's own loopback endpoint; bypasses any reverse proxy).
    - ``/mcp/{name}/{rest}`` → ``http://localhost:{mcp_port}{rest}`` resolved
      via ``mcp_registry.get_manifest(name).server.port``.

    Returns None when the MCP name doesn't match any installed manifest.
    """
    base = path.split("?", 1)[0]
    query = path.split("?", 1)[1] if "?" in path else ""

    mcp_match = _MCP_PATH_RE.match(base)
    if mcp_match:
        mcp_name = mcp_match.group(1)
        rest = mcp_match.group(2) or "/"
        try:
            from services.mcp import mcp_registry
            from core.config import deployment
            # The path slug is the mcpServers config key, which may be the
            # manifest's `server_name` rather than its canonical `name` (e.g.
            # camoufox registers as ``[mcp_servers.playwright]`` even though the
            # manifest name is "camoufox") — get_manifest_by_config_key applies
            # the same server_name fallback the outbound config rewriter uses, so
            # tunnel routing and URL rewriting stay in lockstep. Without it
            # Docker MCPs with a distinct server_name 404 here ("mcp-not-found").
            manifest = mcp_registry.get_manifest_by_config_key(mcp_name)
        except Exception:
            manifest = None
        if manifest is None:
            return None
        port = getattr(getattr(manifest, "server", None), "port", None)
        if not port:
            return None
        # Bare-metal (T1): the container publishes a loopback port, so the proxy
        # reaches it on ``localhost``. Docker-Compose (T2): the MCP is a sibling
        # container reached by service-DNS on the shared network. The satellite
        # tunnel terminates in the proxy, so this hop runs on the proxy host in
        # both cases — deployment.docker_mcp_host picks the right name.
        host = deployment.docker_mcp_host(manifest)
        url = f"http://{host}:{port}{rest}"
        return url + (f"?{query}" if query else "")

    # Default: route through the proxy's own loopback (hooks, location).
    url = f"http://localhost:{config.PORT}{path}"
    return url


def _swap_brokered_bearer(path: str, headers: dict) -> None:
    """Swap a per-session-JWT ``Authorization`` bearer for the real upstream
    token from the in-memory broker store. Mutates ``headers`` in place.

    A proxy-terminable HTTP MCP (github/m365) ships the per-session JWT as its
    Authorization bearer (agent-readable, leaks nothing); this runs at the tunnel
    boundary — just before forwarding to the localhost sidecar — and replaces it
    with the REAL token so the real secret never reaches the satellite disk.

    Self-gating: only fires when the store holds an ``http_bearer`` for this
    ``(session, mcp)`` pair, so non-bearer tunneled MCPs (file-tools) and any
    non-JWT / non-MCP Authorization header are forwarded untouched. A store miss
    leaves the JWT in place → the sidecar 401s (fail-closed)."""
    mcp_match = _MCP_PATH_RE.match(path.split("?", 1)[0])
    if not mcp_match:
        return
    auth_key = next((k for k in headers if k.lower() == "authorization"), None)
    auth_val = headers.get(auth_key, "") if auth_key else ""
    if not auth_val.startswith("Bearer "):
        return
    from auth.session_token import validate_session_token
    from core.credentials import mcp_broker
    payload = validate_session_token(auth_val[7:])
    if not payload:
        return
    bundle = mcp_broker.get(payload.get("sid") or "", mcp_match.group(1))
    if bundle and bundle.http_bearer:
        if auth_key:
            headers.pop(auth_key, None)
        headers["Authorization"] = f"Bearer {bundle.http_bearer}"
    elif bundle is None:
        # Store miss on a valid session JWT → the sidecar 401s (fail-closed).
        # Post-restart this is a re-adopted session whose broker bundles died
        # with the process; log once per (session, mcp) so field reports
        # self-identify — the recovery is a session re-warm.
        key = (payload.get("sid") or "", mcp_match.group(1))
        if key not in _swap_miss_logged:
            _swap_miss_logged.add(key)
            logger.info(
                "tunnel bearer swap miss for session %s mcp %s — empty broker "
                "store (proxy restarted?); MCP auth fails until re-warm",
                key[0][:8], key[1],
            )


# (session_id, mcp) pairs whose bearer-swap miss was already logged — the miss
# repeats on every request of the session, one line is enough.
_swap_miss_logged: set[tuple[str, str]] = set()


@dataclass
class _HttpStream:
    """Server-side per-stream state. One per in-flight tunneled request."""

    stream_id: str
    machine_id: str
    # Body chunks arriving from the satellite (for streamed request bodies).
    # Sentinel: None marks EOF.
    request_chunks: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=_REQUEST_QUEUE_SIZE)
    )
    timeout_s: int = 30
    created_at: float = field(default_factory=time.monotonic)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


class SatelliteHttpTunnelDispatcher:
    """Per-manager dispatcher for tunneled HTTP traffic.

    Held by SatelliteConnectionManager. Tracks pending streams keyed by
    ``(machine_id, stream_id)``. Each stream owns its request-body queue
    and is consumed by exactly one ``_dispatch`` coroutine.
    """

    def __init__(self) -> None:
        self._streams: dict[tuple[str, str], _HttpStream] = {}
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._sweep_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background sweeper. Idempotent."""
        if self._sweep_task is not None:
            return
        self._client = httpx.AsyncClient(timeout=None)
        self._sweep_task = asyncio.create_task(
            self._sweep_leaked_streams(), name="http-tunnel-sweeper",
        )

    async def shutdown(self) -> None:
        """Cancel the sweeper and close the httpx client."""
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sweep_task
            self._sweep_task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=None)
        return self._client

    # --- Inbound frame routing (called from SatelliteConnectionManager.handle_message) ---

    async def handle_request_frame(self, manager, machine_id: str, msg: dict) -> None:
        """Process an inbound ``http_request`` frame.

        Spawned as a task so the satellite's main message loop doesn't block
        on slow upstream calls.
        """
        stream_id = msg.get("stream_id", "")
        if not stream_id:
            logger.warning("http_request missing stream_id; dropping")
            return

        path = msg.get("path", "")
        if not _is_allowed_path(path):
            logger.warning(
                "tunnel: rejected non-allowlisted path: %s (machine %s)",
                path, machine_id[:8],
            )
            await self._send_response(
                manager, machine_id, stream_id,
                status=403, headers={}, body=b"",
                error="path-not-allowlisted", body_eof=True,
            )
            return

        # Set up stream tracking BEFORE awaiting anything — if more
        # request_chunk frames are already on the WS, they need a queue.
        key = (machine_id, stream_id)
        stream = _HttpStream(
            stream_id=stream_id,
            machine_id=machine_id,
            timeout_s=int(msg.get("timeout_s", 30)),
        )
        async with self._lock:
            if key in self._streams:
                logger.warning(
                    "tunnel: stream_id collision %s on machine %s",
                    stream_id[:8], machine_id[:8],
                )
                await self._send_response(
                    manager, machine_id, stream_id,
                    status=409, headers={}, body=b"",
                    error="stream-id-collision", body_eof=True,
                )
                return
            self._streams[key] = stream

        # Run the dispatch in the background so the main message loop returns.
        asyncio.create_task(
            self._dispatch(manager, machine_id, stream_id, stream, msg),
            name=f"tunnel-dispatch-{stream_id[:8]}",
        )

    def handle_request_chunk(self, machine_id: str, msg: dict) -> None:
        """Route a request-body continuation chunk into its stream's queue."""
        stream_id = msg.get("stream_id", "")
        stream = self._streams.get((machine_id, stream_id))
        if stream is None:
            logger.debug(
                "tunnel: request_chunk for unknown stream %s",
                stream_id[:8],
            )
            return
        try:
            stream.request_chunks.put_nowait(msg)
        except asyncio.QueueFull:
            logger.warning(
                "tunnel: request_chunks queue full for %s; dropping",
                stream_id[:8],
            )

    async def cancel_machine_streams(
        self, manager, machine_id: str,
    ) -> None:
        """Fail every pending stream for a machine with synthetic 502.

        Called from SatelliteConnectionManager.deregister so subprocesses
        get a clean failure instead of hanging until timeout.
        """
        keys = [k for k in list(self._streams.keys()) if k[0] == machine_id]
        for key in keys:
            stream = self._streams.pop(key, None)
            if stream is None:
                continue
            stream.cancel_event.set()
            # No response is sent here — the WS is going away. The satellite
            # side will see the WS close and fail its handlers locally via
            # the LocalTunnelServer.fail_all_streams() path.

    # --- Dispatch implementation ---

    async def _dispatch(
        self,
        manager,
        machine_id: str,
        stream_id: str,
        stream: _HttpStream,
        first_msg: dict,
    ) -> None:
        """Call the platform-internal endpoint and stream the response back."""
        try:
            method = first_msg.get("method", "GET")
            path = first_msg.get("path", "")
            headers = {
                k: v for (k, v) in first_msg.get("headers", {}).items()
                if k.lower() not in _HOP_BY_HOP_HEADERS
            }
            timeout_s = stream.timeout_s
            url = _resolve_upstream_url(path)
            if url is None:
                # MCP not installed on the platform (manifest missing).
                await self._send_response(
                    manager, machine_id, stream_id,
                    status=404, headers={}, body=b"",
                    error="mcp-not-found", body_eof=True,
                )
                return

            # HTTP bearer-swap: a proxy-terminable HTTP MCP (github/m365)
            # ships the per-session JWT as its Authorization bearer; swap it for
            # the real upstream token at the tunnel boundary so the real secret
            # never reaches the satellite disk.
            _swap_brokered_bearer(path, headers)

            # Build the request body. If the first frame is body_eof=True,
            # the body is inline; else collect chunks until eof.
            first_body_b64 = first_msg.get("body_b64", "")
            first_body = base64.b64decode(first_body_b64) if first_body_b64 else b""
            if first_msg.get("body_eof", True):
                body_bytes = first_body
            else:
                body_chunks = [first_body] if first_body else []
                while True:
                    chunk = await asyncio.wait_for(
                        stream.request_chunks.get(),
                        timeout=timeout_s,
                    )
                    chunk_b64 = chunk.get("body_b64", "")
                    if chunk_b64:
                        body_chunks.append(base64.b64decode(chunk_b64))
                    if chunk.get("body_eof"):
                        break
                body_bytes = b"".join(body_chunks)

            # Make the upstream call. Use stream=True so SSE/large responses
            # don't buffer in memory.
            client = self._get_client()
            # Streaming MCP calls (e.g. camoufox browser actions) can legitimately
            # run far longer than a hook callback and stream their result sparsely
            # — a fixed read-timeout would sever a slow-but-valid browser op midway
            # (the original camoufox "Issue B" failure). Drop the read-timeout for
            # /mcp/* (the connect-timeout still guards a dead upstream); keep the
            # bounded read for fast hook callbacks.
            is_mcp = _MCP_PATH_RE.match(path.split("?", 1)[0]) is not None
            req = client.build_request(
                method, url,
                headers=headers,
                content=body_bytes,
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=None if is_mcp else float(timeout_s),
                    write=30.0,
                    pool=10.0,
                ),
            )

            try:
                resp = await client.send(req, stream=True)
            except httpx.RequestError as e:
                logger.warning(
                    # strip the query string — it can carry session ids / tokens
                    "tunnel: upstream error for %s: %s", path.split("?", 1)[0], e,
                )
                await self._send_response(
                    manager, machine_id, stream_id,
                    status=502, headers={}, body=b"",
                    error=f"upstream-{type(e).__name__}", body_eof=True,
                )
                return

            # Forward response status + headers (strip hop-by-hop).
            resp_headers = {
                k: v for (k, v) in resp.headers.items()
                if k.lower() not in _HOP_BY_HOP_HEADERS
            }

            # ALWAYS stream the upstream body. Previous behavior used
            # ``resp.aread()`` for non-SSE responses, which buffered the
            # entire body before forwarding the first byte to the
            # satellite. That was catastrophic for MCP streamable-http
            # transport (e.g. playwright/camoufox): the response is
            # ``Content-Type: application/json`` with ``Transfer-Encoding:
            # chunked``, so it didn't match the SSE branch — and any
            # browser operation that took > 60 s upstream blew past
            # claude-code's HTTP client timeout because the proxy hadn't
            # sent so much as the response headers yet. Streaming via
            # ``aiter_bytes`` forwards each chunk as it arrives, so a
            # slow camoufox response no longer eats the whole deadline
            # before the satellite sees a single byte.
            #
            # Trade-off: a tiny response is now ≥ 3 WS frames (headers,
            # data, eof) instead of 1. Negligible — frames are cheap,
            # and the latency win on slow / large responses is huge.
            await self._send_response(
                manager, machine_id, stream_id,
                status=resp.status_code,
                headers=resp_headers,
                body=b"",
                body_eof=False,
            )
            try:
                # Forward each upstream chunk AS IT ARRIVES via aiter_raw() — do
                # NOT rebuffer to a fixed size. The previous
                # aiter_bytes(chunk_size=_MAX_FRAME_BODY) accumulated the stream
                # until 256KB OR the response closed: fine for a body that ends
                # promptly, but it STALLED long-lived streamable-HTTP **standalone
                # GET** streams. A server→client request on that GET (e.g. MCP
                # `roots/list`, ~50 bytes — playwright-mcp sends one on the first
                # tool call when the client declares the `roots` capability) sat
                # in the buffer and never reached the remote CLI, which therefore
                # never answered → playwright-mcp hit its 60s server-request
                # timeout on EVERY tool call, then the session churned (the
                # camoufox "≈60s per call + 404 re-init" bug; local was immune
                # because no proxy hop buffered the GET). aiter_raw() also keeps
                # the bytes consistent with the forwarded Content-Encoding header.
                # Split only to honour the WS frame cap.
                async for chunk in resp.aiter_raw():
                    if stream.cancel_event.is_set():
                        break
                    if not chunk:
                        continue
                    for i in range(0, len(chunk), _MAX_FRAME_BODY):
                        await self._send_chunk(
                            manager, machine_id, stream_id,
                            chunk[i:i + _MAX_FRAME_BODY], body_eof=False,
                        )
            except (httpx.TransportError, httpx.StreamError) as e:
                # Upstream body dropped mid-stream. This is the ROUTINE outcome
                # when the satellite WS reconnects: every long-lived
                # streamable-HTTP MCP stream in flight (file-tools, github-mcp,
                # platform, camoufox, …) is torn down at once, surfacing here as
                # httpx.ReadError. Headers were already forwarded (status can't
                # change), so just close the stream cleanly below — the satellite
                # reader sees EOF and the MCP client reconnects. One WARN line,
                # no traceback: it's connection churn, not a dispatch fault.
                # (Without this it fell through to the generic handler → an
                # ERROR+traceback ×N-per-blip and a misleading 500 error frame.)
                logger.warning(
                    "tunnel: upstream stream ended early for %s: %s",
                    path.split("?", 1)[0], type(e).__name__,
                )
            finally:
                # Final EOF marker — flushes claude-code's HTTP reader on both a
                # clean end and an early upstream drop. Guarded so a send failure
                # (conn already gone) can't mask the close.
                try:
                    await self._send_chunk(
                        manager, machine_id, stream_id,
                        b"", body_eof=True,
                    )
                finally:
                    await resp.aclose()

        except asyncio.TimeoutError:
            logger.warning("tunnel: timeout on stream %s", stream_id[:8])
            await self._send_response(
                manager, machine_id, stream_id,
                status=504, headers={}, body=b"",
                error="upstream-timeout", body_eof=True,
            )
        except Exception:
            logger.exception(
                "tunnel: unhandled error on stream %s", stream_id[:8],
            )
            await self._send_response(
                manager, machine_id, stream_id,
                status=500, headers={}, body=b"",
                error="dispatch-exception", body_eof=True,
            )
        finally:
            self._streams.pop((machine_id, stream_id), None)

    async def _send_response(
        self,
        manager,
        machine_id: str,
        stream_id: str,
        *,
        status: int,
        headers: dict,
        body: bytes,
        body_eof: bool,
        error: str | None = None,
    ) -> None:
        """Send an ``http_response`` frame back to the satellite."""
        conn = manager.get_connection(machine_id)
        if conn is None:
            return
        await conn.enqueue_send({
            "type": "http_response",
            "stream_id": stream_id,
            "status": status,
            "headers": headers,
            "body_b64": base64.b64encode(body).decode() if body else "",
            "body_eof": body_eof,
            "error": error,
        })

    async def _send_chunk(
        self,
        manager,
        machine_id: str,
        stream_id: str,
        chunk: bytes,
        *,
        body_eof: bool,
    ) -> None:
        """Send an ``http_response_chunk`` frame back to the satellite."""
        conn = manager.get_connection(machine_id)
        if conn is None:
            return
        await conn.enqueue_send({
            "type": "http_response_chunk",
            "stream_id": stream_id,
            "body_b64": base64.b64encode(chunk).decode() if chunk else "",
            "body_eof": body_eof,
        })

    async def _sweep_leaked_streams(self) -> None:
        """Force-fail streams that haven't seen EOF past their declared timeout.

        Defense against bad satellite implementations that send a request
        and never send EOF. Without this, _streams would grow unbounded.
        """
        try:
            while True:
                await asyncio.sleep(_STREAM_SWEEP_INTERVAL)
                now = time.monotonic()
                expired = []
                for key, stream in list(self._streams.items()):
                    if now - stream.created_at > stream.timeout_s + _STREAM_GRACE_S:
                        expired.append(key)
                for key in expired:
                    machine_id, stream_id = key
                    stream = self._streams.pop(key, None)
                    if stream is not None:
                        stream.cancel_event.set()
                    logger.warning(
                        "tunnel: swept leaked stream %s on machine %s",
                        stream_id[:8], machine_id[:8],
                    )
        except asyncio.CancelledError:
            return


# Module-level singleton, mirrors the connection-manager pattern.
_dispatcher: SatelliteHttpTunnelDispatcher | None = None


def get_dispatcher() -> SatelliteHttpTunnelDispatcher:
    """Return the singleton tunnel dispatcher (creates on first call)."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = SatelliteHttpTunnelDispatcher()
    return _dispatcher

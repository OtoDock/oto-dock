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
from auth.request_path import has_traversal

logger = logging.getLogger("claude-proxy.satellite-tunnel")


# Allowlist mirrored from satellite/http_tunnel.py. Both sides must agree.
_ALLOWLIST_REGEXES = [
    re.compile(
        r"^/v1/hooks/(resolve-path|resolve-tool-arg-paths|permission|"
        r"mcp-credentials|session-files|"
        r"images|image-generating|image-gen-failed|url|file|media|ui|"
        r"document-preview|tool-result|file-written|subagent|"
        # The Stop hook (turn end, both engines) and the Codex question
        # bridge (request_user_input → the dashboard card); satellite 0.5.121
        # admits the same two.
        r"stop|codex-question)$"
    ),
    # display-mcp pinned apps (pin/unpin/list) and the live-apps hooks
    # (push/state/open, APPS.md "Live apps") — session-JWT gated
    # proxy-side like every hook (verify_session_match + scope from the
    # session ctx); the artifact hook itself is the `ui` entry above.
    re.compile(r"^/v1/hooks/apps/(pin|unpin|list|push|state|open|rollback|"
               r"deploy|check|status|preview|logs|restart|purge|describe|export|import|screenshot)$"),
    # Agents calling apps (APPS.md "Agents call apps"): an app's own API,
    # its platform methods and the push/state twins, with the session JWT
    # the routes judge as basis `agent`. Never the client files or the
    # socket bridge (browser surfaces).
    re.compile(r"^/v1/apps/[0-9a-f-]{36}/(api|platform|push|state)(/.*)?$"),
    # An app step's script pressing one of its app's buttons (APPS.md
    # "Steps"; satellite 0.5.122 admits the same): the route accepts only
    # the step's own in-flight claim as the bearer; never the batch route.
    re.compile(r"^/v1/apps/[0-9a-f-]{36}/actions/[A-Za-z0-9_-]{1,64}$"),
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
    # checks-mcp (CHECKS.md): what is attached to this session's chat, attach,
    # detach, run by hand — all session-JWT gated proxy-side (0.5.123).
    re.compile(r"^/v1/checks/(attached|attach|detach|run)$"),
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

# Hop-by-hop headers — never forward upstream or downstream.
_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# Request headers that name a client address or an edge.
_FORWARDING_PREFIX = "x-forwarded-"
_FORWARDING_HEADERS = {"forwarded", "x-real-ip"}

# Chunk body cap on the wire (matches satellite/http_tunnel.py).
_MAX_FRAME_BODY = 256 * 1024

# Per-stream request body queue cap. Sized for sustained uploads
# (5 MB attachment ÷ 256 KB ≈ 20 chunks).
_REQUEST_QUEUE_SIZE = 64

# Stream sweep interval and slack — leaked streams (request without EOF)
# get cleaned up `_STREAM_GRACE_S` past their declared timeout.
_STREAM_SWEEP_INTERVAL = 60
_STREAM_GRACE_S = 30
# Ceilings on satellite-supplied stream parameters. The sweeper only reaps a
# stream past timeout_s + grace, and request bodies buffer in proxy memory —
# both must be bounded per stream or one compromised machine can park
# immortal multi-GB buffers. The body cap matches the satellite-side
# tunnel's own client_max_size (128 MB).
_MAX_STREAM_TIMEOUT_S = 15 * 60
_MAX_REQUEST_BODY_BYTES = 128 * 1024 * 1024
# Sweep semantics (2026-09-04): a stream is reaped when it has been IDLE (no
# frame in either direction) past timeout_s + grace — a long-lived MCP
# streamable-HTTP GET that keeps delivering is never cut (the old rule cut
# every such stream at the 15-min clamp: "swept leaked stream" every ~15 min
# on any machine with tunneled HTTP MCPs) — or when it is older than the
# absolute cap regardless of activity. Reaping CLOSES the upstream response
# (cancels the dispatch task) instead of only forgetting the entry, so a
# reaped stream can no longer hold an httpx connection.
_STREAM_MAX_AGE_S = 24 * 3600

# Two stream classes, two httpx clients, separate caps. Every remote CLI
# holds one standing streamable-HTTP GET per tunneled HTTP MCP for the life
# of the session (``/mcp/<name>/...``, the ``mcp`` class); every hook, MCP
# callback and app call is a short loopback request (the ``hook`` class),
# and a parked permission prompt or a Stop-verdict wait holds one for
# minutes. On one shared pool the standing GETs of 50 to 100 remote
# sessions fill it and every hook waits the pool timeout for a 502, and a
# single per-machine count lets a machine's own GETs refuse its hooks. So
# the classes never share a connection, and a hook is never refused
# because MCP streams are held.
#
# The per-machine MCP cap is a satellite's session ceiling (64) times the
# HTTP MCPs a session holds at most (four: file-tools, video-tools, camoufox
# or m365, github). The fleet MCP cap bounds the descriptors a fleet of
# leaking satellites can hold (one per stream; the soft limit is 65536
# since the boot raise). The per-machine hook cap is 64 sessions times a
# parked prompt, a verdict wait and an in-flight callback, rounded up; only
# a satellite that leaks streams reaches it. There is no fleet hook cap: a
# hook is never refused fleet-wide, and past the hook pool's size (two
# descriptors per loopback request) the pool timeout answers with a 502.
_MAX_MCP_STREAMS_PER_MACHINE = 256
_MAX_MCP_STREAMS_TOTAL = 2048
_MAX_HOOK_STREAMS_PER_MACHINE = 256
# The pools. The MCP pool carries one machine's worth of headroom over the
# fleet cap: the cap, not the pool, is the bound (it refuses with a protocol
# frame before the pool could wait), and a reaped stream frees its slot at
# once while its socket closes when the cancelled task's finally runs.
# Keepalive equals the pool size on both: httpcore prunes every idle
# connection whenever the total number of connections (active ones
# included) exceeds the keepalive bound, so a smaller number would open a
# new socket per tool-call POST and per hook while streams are held.
_MCP_POOL_MAX = _MAX_MCP_STREAMS_TOTAL + _MAX_MCP_STREAMS_PER_MACHINE
_HOOK_POOL_MAX = 4096
# The Stop hook waits on the turn-end verdict, which the proxy bounds itself
# (each check's own budget; a default judge check alone is past 15 min), so
# its ceiling is the absolute age cap: the clamp would cut the verdict off
# and the hook would fail open with the fix round lost. The two prompt hooks
# wait on a person and share that ceiling.
_VERDICT_HOOK_PATH = "/v1/hooks/stop"
_UNCLAMPED_HOOK_PATHS = frozenset({
    _VERDICT_HOOK_PATH, "/v1/hooks/permission", "/v1/hooks/codex-question",
})


def _stream_timeout_ceiling(path: str) -> int:
    # The verdict hook waits on a check's budget and the two prompt hooks
    # wait on a person; each holds one stream of the hook class on its own
    # pool, so they may outlive the clamp every other stream keeps.
    if path.split("?", 1)[0] in _UNCLAMPED_HOOK_PATHS:
        return _STREAM_MAX_AGE_S
    return _MAX_STREAM_TIMEOUT_S


class _BodyTooLarge(Exception):
    """Tunneled request body exceeded _MAX_REQUEST_BODY_BYTES."""


def _is_allowed_path(path: str) -> bool:
    # Strip query string for matching
    # A dot segment or an encoded separator is refused before the regexes
    # run, so the matched path is byte-identical to the forwarded path
    # (httpx collapses ``../`` when it builds the upstream request).
    base = path.split("?", 1)[0]
    if has_traversal(base):
        return False
    return any(rx.match(base) for rx in _ALLOWLIST_REGEXES)


# Matches `/mcp/<name>/<rest>` so the dispatcher can resolve `<name>` → the
# Docker MCP's actual localhost port via the manifest registry.
_MCP_PATH_RE = re.compile(r"^/mcp/([a-z0-9_-]+)(/.*)?$")


def _resolve_upstream_url(path: str) -> str | None:
    """Resolve a tunneled path to the upstream URL on the platform.

    - ``/v1/hooks/*`` and ``/v1/location/request`` → the proxy's internal
      listener, ``http://127.0.0.1:{INTERNAL_LISTENER_PORT}{path}`` (the main
      ``PORT`` when there is none; bypasses any reverse proxy).
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

    # Default: the proxy's internal listener (hooks, location), where
    # forwarding headers are never read; the main port when there is none.
    port = config.INTERNAL_LISTENER_PORT or config.PORT
    return f"http://127.0.0.1:{port}{path}"


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
        # No bundle for this (session, mcp): a callback MCP (file-tools)
        # never has one and its JWT is forwarded as designed; a brokered MCP
        # has none only after a proxy restart (the store died with the
        # process) and its sidecar 401s until the session re-warms. The
        # tunnel cannot tell the two apart, so the line names both, once
        # per (session, mcp).
        key = (payload.get("sid") or "", mcp_match.group(1))
        if _first_swap_miss(key):
            logger.info(
                "tunnel: no broker bundle for session %s mcp %s (a callback MCP "
                "carries its JWT; a brokered MCP after a proxy restart needs a re-warm)",
                key[0][:8], key[1],
            )


# (session_id, mcp) pairs whose bearer-swap miss was already logged — the miss
# repeats on every request of the session, one line is enough. Bounded: every
# callback-MCP session adds a pair for the life of the process, so the set
# starts over past the cap (a repeated line after that is harmless).
_swap_miss_logged: set[tuple[str, str]] = set()
_SWAP_MISS_LOG_CAP = 4096


def _first_swap_miss(key: tuple[str, str]) -> bool:
    if key in _swap_miss_logged:
        return False
    if len(_swap_miss_logged) >= _SWAP_MISS_LOG_CAP:
        _swap_miss_logged.clear()
    _swap_miss_logged.add(key)
    return True


# The holder of a tunneled session token. Every tunneled HTTP MCP carries
# the session JWT as its bearer (the brokered ones swap it for the real
# upstream secret above; the callback ones forward it to reach the hooks),
# and the signature alone would honour it for its 24 h life after the person
# was removed or changed their credentials. The judge is the session routes'
# (``auth.providers.session_token_holder_ok``: the person still exists and
# the token predates no credential change), its answer cached per (sub, iat)
# for the same TTL; the cache is the tunnel's own because ``core/remote``
# imports nothing from ``api/``. A token with no person (an agent-scope run)
# is not judged.
_HOLDER_TTL_S = 60.0
_HOLDER_ANSWERS_MAX = 4096
_holder_answers: dict[tuple[str, int], tuple[bool, float]] = {}


class _AmbiguousAuthorization(Exception):
    """A tunneled request carries more than one Authorization header."""


def _session_bearer_payload(headers: dict) -> dict | None:
    """The payload of a request's session-JWT bearer, or None when the
    request carries no bearer or one that is not a session token. The scheme
    is read in any case, as the routes read it. More than one Authorization
    header (keys differing in case) raises ``_AmbiguousAuthorization``:
    which one a server reads is its own choice, so none is judged."""
    keys = [k for k in headers if isinstance(k, str) and k.lower() == "authorization"]
    if len(keys) > 1:
        raise _AmbiguousAuthorization()
    value = headers.get(keys[0]) if keys else None
    if not isinstance(value, str):
        return None
    scheme, _, token = value.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    from auth.session_token import validate_session_token
    return validate_session_token(token.strip())


async def _holder_current(payload: dict) -> bool:
    sub = payload.get("user_sub") or ""
    if not sub:
        return True
    iat = payload.get("iat")
    key = (sub, iat if isinstance(iat, int) else 0)
    now = time.monotonic()
    hit = _holder_answers.get(key)
    if hit is not None and hit[1] >= now:
        return hit[0]
    from auth import providers
    from storage.pg import run_db_fast
    ok = bool(await run_db_fast(providers.session_token_holder_ok, payload))
    if len(_holder_answers) >= _HOLDER_ANSWERS_MAX:
        _holder_answers.clear()
    _holder_answers[key] = (ok, now + _HOLDER_TTL_S)
    return ok


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
    # Last frame in EITHER direction (request chunk in, response chunk out)
    # — the sweeper's idle clock.
    last_activity: float = field(default_factory=time.monotonic)
    # Set while the upstream httpx response is open (diagnostics / tests).
    upstream_open: bool = False
    # The dispatch task — cancelled by reap/abort so the upstream response is
    # actually closed, not merely forgotten.
    task: asyncio.Task | None = None
    # ``mcp`` for a ``/mcp/<name>/...`` stream, ``hook`` for everything else:
    # picks the client and the cap. ``admitted`` is set once the stream was
    # counted, so a slot is freed exactly once and a stream a test inserts
    # by hand frees nothing.
    kind: str = "hook"
    admitted: bool = False


def _new_client(is_mcp: bool) -> httpx.AsyncClient:
    size = _MCP_POOL_MAX if is_mcp else _HOOK_POOL_MAX
    return httpx.AsyncClient(
        timeout=None,
        limits=httpx.Limits(max_connections=size, max_keepalive_connections=size),
    )


class SatelliteHttpTunnelDispatcher:
    """Per-manager dispatcher for tunneled HTTP traffic.

    Held by SatelliteConnectionManager. Tracks pending streams keyed by
    ``(machine_id, stream_id)``. Each stream owns its request-body queue
    and is consumed by exactly one ``_dispatch`` coroutine.
    """

    def __init__(self) -> None:
        self._streams: dict[tuple[str, str], _HttpStream] = {}
        self._lock = asyncio.Lock()
        self._mcp_client: httpx.AsyncClient | None = None
        self._hook_client: httpx.AsyncClient | None = None
        self._sweep_task: asyncio.Task | None = None
        # Open streams per (machine, class) and across the fleet for the
        # MCP class; kept by ``handle_request_frame`` and ``_forget``.
        self._open: dict[tuple[str, str], int] = {}
        self._open_mcp_total = 0

    async def start(self) -> None:
        """Start the background sweeper. Idempotent."""
        if self._sweep_task is not None:
            return
        self._mcp_client = _new_client(True)
        self._hook_client = _new_client(False)
        self._sweep_task = asyncio.create_task(
            self._sweep_leaked_streams(), name="http-tunnel-sweeper",
        )

    async def shutdown(self) -> None:
        """Cancel the sweeper and close both httpx clients."""
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sweep_task
            self._sweep_task = None
        for attr in ("_mcp_client", "_hook_client"):
            client = getattr(self, attr)
            if client is not None:
                await client.aclose()
                setattr(self, attr, None)

    def _get_client(self, is_mcp: bool) -> httpx.AsyncClient:
        if is_mcp:
            if self._mcp_client is None:
                self._mcp_client = _new_client(True)
            return self._mcp_client
        if self._hook_client is None:
            self._hook_client = _new_client(False)
        return self._hook_client

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
                # strip the query string — it can carry session ids / tokens
                # (same rule as the upstream-error log below)
                "tunnel: rejected non-allowlisted path: %s (machine %s)",
                str(path).split("?", 1)[0][:200], machine_id[:8],
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
        # Clamp the satellite-supplied timeout: the leak sweeper only reaps a
        # stream past timeout_s + grace, so an absurd value would make it
        # immortal and let one machine park unbounded buffered streams.
        try:
            timeout_s = int(msg.get("timeout_s", 30))
        except (TypeError, ValueError):
            timeout_s = 30
        is_mcp = _MCP_PATH_RE.match(str(path).split("?", 1)[0]) is not None
        stream = _HttpStream(
            stream_id=stream_id,
            machine_id=machine_id,
            timeout_s=max(1, min(timeout_s, _stream_timeout_ceiling(path))),
            kind="mcp" if is_mcp else "hook",
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
            refused = self._cap_reached(stream)
            if refused:
                logger.warning(
                    "tunnel: %s; refusing %s from machine %s",
                    refused, stream_id[:8], machine_id[:8],
                )
                await self._send_response(
                    manager, machine_id, stream_id,
                    status=503, headers={}, body=b"",
                    error="too-many-streams", body_eof=True,
                )
                return
            # Counted and inserted with no await in between, so the count a
            # concurrent admission reads is never stale.
            self._streams[key] = stream
            stream.admitted = True
            per_machine = (machine_id, stream.kind)
            self._open[per_machine] = self._open.get(per_machine, 0) + 1
            if is_mcp:
                self._open_mcp_total += 1

        # Run the dispatch in the background so the main message loop returns.
        stream.task = asyncio.create_task(
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
        stream.last_activity = time.monotonic()
        try:
            stream.request_chunks.put_nowait(msg)
        except asyncio.QueueFull:
            logger.warning(
                "tunnel: request_chunks queue full for %s; dropping",
                stream_id[:8],
            )

    def _cap_reached(self, stream: _HttpStream) -> str | None:
        """Why this stream must be refused, or None when its class has room
        on its machine (and, for an MCP stream, across the fleet)."""
        open_here = self._open.get((stream.machine_id, stream.kind), 0)
        if stream.kind == "mcp":
            if open_here >= _MAX_MCP_STREAMS_PER_MACHINE:
                return f"machine has {open_here} open MCP streams"
            if self._open_mcp_total >= _MAX_MCP_STREAMS_TOTAL:
                return f"{self._open_mcp_total} open MCP streams across the fleet"
            return None
        if open_here >= _MAX_HOOK_STREAMS_PER_MACHINE:
            return f"machine has {open_here} open hook streams"
        return None

    def _forget(self, stream: _HttpStream) -> None:
        """Drop the stream's registry entry (only while it is still this
        stream: a same-id successor is never evicted) and free its slot
        exactly once. Synchronous on purpose: the dispatch finally runs it
        outside ``_lock``, and a decrement with no await is atomic on the
        loop against an admission's count-and-insert."""
        key = (stream.machine_id, stream.stream_id)
        if self._streams.get(key) is stream:
            del self._streams[key]
        if not stream.admitted:
            return
        stream.admitted = False
        per_machine = (stream.machine_id, stream.kind)
        left = self._open.get(per_machine, 0) - 1
        if left > 0:
            self._open[per_machine] = left
        else:
            self._open.pop(per_machine, None)
        if stream.kind == "mcp":
            self._open_mcp_total = max(0, self._open_mcp_total - 1)

    def _reap(self, key: tuple[str, str]) -> "_HttpStream | None":
        """Forget a stream AND close it: set the cancel flag (the chunk loop
        checks it) and cancel the dispatch task so its finally closes the
        upstream httpx response now, not whenever upstream next speaks."""
        stream = self._streams.get(key)
        if stream is None:
            return None
        self._forget(stream)
        stream.cancel_event.set()
        task = stream.task
        if task is not None and not task.done():
            task.cancel()
        return stream

    def abort_stream(self, machine_id: str, stream_id: str) -> bool:
        """Satellite ``http_abort``: its local client went away (hook script
        or MCP client disconnected). Close our side at once instead of
        holding the upstream until the sweep. Returns True if a stream was
        open."""
        stream = self._reap((machine_id, stream_id))
        if stream is None:
            return False
        logger.debug(
            "tunnel: stream %s aborted by machine %s", stream_id[:8], machine_id[:8],
        )
        return True

    async def cancel_machine_streams(
        self, manager, machine_id: str,
    ) -> None:
        """Close every pending stream for a machine.

        Called from SatelliteConnectionManager.deregister. No response frame
        is sent — the WS is going away; the satellite side fails its handlers
        locally via LocalTunnelServer.fail_all_streams(). The dispatch tasks
        ARE cancelled so their upstream responses close instead of lingering
        until upstream next speaks.
        """
        keys = [k for k in list(self._streams.keys()) if k[0] == machine_id]
        for key in keys:
            self._reap(key)

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
            # A machine never speaks for a client address: the forwarding
            # headers go before the platform sees the request.
            headers = {
                k: v for (k, v) in first_msg.get("headers", {}).items()
                if k.lower() not in _HOP_BY_HOP_HEADERS
                and not k.lower().startswith(_FORWARDING_PREFIX)
                and k.lower() not in _FORWARDING_HEADERS
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

            # A tunneled MCP request is judged on its token's holder before
            # the bearer swap: a person who is gone gets a 401 and the
            # upstream is never contacted.
            if stream.kind == "mcp":
                try:
                    payload = _session_bearer_payload(headers)
                except _AmbiguousAuthorization:
                    await self._send_response(
                        manager, machine_id, stream_id,
                        status=400, headers={}, body=b"",
                        error="ambiguous-authorization", body_eof=True,
                    )
                    return
                if payload is not None and not await _holder_current(payload):
                    logger.warning(
                        "tunnel: session token holder refused for %s (machine %s)",
                        path.split("?", 1)[0], machine_id[:8],
                    )
                    await self._send_response(
                        manager, machine_id, stream_id,
                        status=401, headers={}, body=b"",
                        error="session-holder-gone", body_eof=True,
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
                if len(body_bytes) > _MAX_REQUEST_BODY_BYTES:
                    raise _BodyTooLarge()
            else:
                body_chunks = [first_body] if first_body else []
                body_total = len(first_body)
                while True:
                    chunk = await asyncio.wait_for(
                        stream.request_chunks.get(),
                        timeout=timeout_s,
                    )
                    chunk_b64 = chunk.get("body_b64", "")
                    if chunk_b64:
                        decoded = base64.b64decode(chunk_b64)
                        body_total += len(decoded)
                        # Same ceiling the satellite-side tunnel enforces —
                        # without it a machine could drip chunks into an
                        # unbounded proxy-side buffer (OOM from one peer).
                        if body_total > _MAX_REQUEST_BODY_BYTES:
                            raise _BodyTooLarge()
                        body_chunks.append(decoded)
                    if chunk.get("body_eof"):
                        break
                body_bytes = b"".join(body_chunks)

            # Make the upstream call on the stream class's own client. Use
            # stream=True so SSE/large responses don't buffer in memory.
            is_mcp = stream.kind == "mcp"
            client = self._get_client(is_mcp)
            # Streaming MCP calls (e.g. camoufox browser actions) can legitimately
            # run far longer than a hook callback and stream their result sparsely
            # — a fixed read-timeout would sever a slow-but-valid browser op midway
            # (the original camoufox "Issue B" failure). Drop the read-timeout for
            # /mcp/* (the connect-timeout still guards a dead upstream); keep the
            # bounded read for fast hook callbacks.
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

            stream.upstream_open = True
            stream.last_activity = time.monotonic()

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
                    stream.last_activity = time.monotonic()
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
                # (conn already gone) can't mask the close. Runs on
                # cancellation too (reap / abort / machine disconnect), which
                # is what actually releases the upstream connection.
                stream.upstream_open = False
                try:
                    await self._send_chunk(
                        manager, machine_id, stream_id,
                        b"", body_eof=True,
                    )
                finally:
                    await resp.aclose()

        except _BodyTooLarge:
            logger.warning(
                "tunnel: request body over %d bytes on stream %s — refused",
                _MAX_REQUEST_BODY_BYTES, stream_id[:8],
            )
            await self._send_response(
                manager, machine_id, stream_id,
                status=413, headers={}, body=b"",
                error="request-body-too-large", body_eof=True,
            )
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
            self._forget(stream)

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

    def _sweep_once(self, now: float | None = None) -> list[tuple[str, str]]:
        """One sweep pass (also the unit under test): reap streams IDLE past
        their declared timeout + grace, or older than the absolute cap.
        Returns the reaped keys."""
        now = time.monotonic() if now is None else now
        expired = []
        for key, stream in list(self._streams.items()):
            idle = now - max(stream.created_at, stream.last_activity)
            age = now - stream.created_at
            if age > _STREAM_MAX_AGE_S or idle > stream.timeout_s + _STREAM_GRACE_S:
                expired.append(key)
        for key in expired:
            machine_id, stream_id = key
            self._reap(key)
            logger.warning(
                "tunnel: swept leaked stream %s on machine %s",
                stream_id[:8], machine_id[:8],
            )
        return expired

    async def _sweep_leaked_streams(self) -> None:
        """Force-fail streams that go idle past their declared timeout.

        Defense against bad satellite implementations that send a request
        and never send EOF. Without this, _streams would grow unbounded.
        """
        try:
            while True:
                await asyncio.sleep(_STREAM_SWEEP_INTERVAL)
                self._sweep_once()
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

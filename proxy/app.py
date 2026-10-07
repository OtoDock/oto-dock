"""OtoDock Proxy — multi-agent LLM gateway with per-agent routing.

Execution layers (core/layers/ + core/remote/):
  - Claude Code CLI (core/layers/cli/): persistent `claude -p` subprocesses —
    session resumption, image uploads, permission hooks, AskUserQuestion.
  - Codex CLI (core/layers/codex/): `codex` app-server sessions (TOML MCP
    config, rollout files).
  - Direct LLM (core/layers/direct/): calls provider APIs directly — MCP tool
    execution, prompt caching, session state.
  - Remote (core/remote/): brokers a session onto a paired satellite machine
    over the satellite WS.

WebSocket routes:
  - /ws/dashboard          dashboard chat / streaming / notifications
  - /ws/phone              low-latency phone-call turns
  - /ws/phone-management   config push to the phone daemon
  - /v1/satellite          paired remote machines
  - /ws/audio/stt, /ws/audio/tts   chat-audio speech sessions
  - /ws/duplex, /ws/duplex-engine/{id}   full-duplex chat voice bridge
  - /v1/twilio/media/{server_id}   Twilio media stream, bridged to the phone daemon

Client adapters (adapters/): handle client-specific behavior (prompt context,
file display, task result delivery) for phone, dashboard, etc.

Unified event format across layers:
  {"type": "<event>", "data": {...}}
  Types: session, text, tool_start, tool_end, done, error
"""

import logging
import time

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol
from uvicorn.protocols.utils import ClientDisconnected
from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol

import config
from auth import lan_check
from core import log_queue
from startup import lifespan
from middleware import register_middlewares
from static_assets import PrecompressedStaticFiles, is_hashed_asset, negotiated_file_response

# --- Logging ---

# stderr + a self-rotating file: 20 MB x (1 live + 5 backups) = 120 MB hard
# cap, no external logrotate dependency (proxy.log grew unbounded before).
# Every write happens on log_queue's writer thread — nothing may write a
# log line from the event loop (see that module).
log_queue.configure(
    log_path=config.BASE_DIR / "proxy.log",
    max_bytes=20 * 1024 * 1024, backup_count=5,
)
logger = logging.getLogger("claude-proxy")

# The WebSocket keepalive the satellites answer — twins of the satellite's
# ``ws_client._WS_PING_INTERVAL`` / ``_WS_PING_TIMEOUT`` (pinned by the gate's
# twin rule): the interval stays below a reverse proxy's 60 s read timeout,
# the timeout is generous so a busy laptop satellite mid-spawn is not cut.
WS_PING_INTERVAL_S = 20
WS_PING_TIMEOUT_S = 30


# --- App ---

# No public API schema or docs pages: the schema maps every route for a
# visitor who has no account.
app = FastAPI(
    title="OtoDock Proxy",
    version=config.PINNED_OTODOCK_VERSION or "0.0.0",
    lifespan=lifespan,
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)

register_middlewares(app)


# --- Error bodies ---

# FastAPI's default 422 echoes the whole request body (``input``) once per
# validation error, rendered by a pure-Python walk on the event loop: one
# unauthenticated 1 MB body froze the loop ~0.4 s, 8 MB ~3 s. This body keeps
# type, loc and msg only, capped in count and size (a list of 200k invalid
# items would otherwise answer with 26 MB; a dict key lands in ``loc`` verbatim).
_VALIDATION_ERRORS_MAX = 20
_LOC_PART_MAX = 64
_MSG_MAX = 200


def _short(part):
    return part[:_LOC_PART_MAX] if isinstance(part, str) else part


async def _validation_error(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    body: dict = {"detail": [
        {"type": err.get("type"), "loc": [_short(p) for p in err.get("loc", ())],
         "msg": str(err.get("msg", ""))[:_MSG_MAX]}
        for err in errors[:_VALIDATION_ERRORS_MAX]
    ]}
    if len(errors) > _VALIDATION_ERRORS_MAX:
        body["truncated"] = len(errors) - _VALIDATION_ERRORS_MAX
    return JSONResponse(body, status_code=422)


app.add_exception_handler(RequestValidationError, _validation_error)


# The ASGI scope types a request arrives as (a lifespan scope is neither).
_HTTP_SCOPE = "http"
_WS_SCOPE = "websocket"


# --- Database outage → 503 ---

# Connection-level failures only (no connection, a lost or refused one, the
# server shutting down or full). A statement timeout, a deadlock or a full
# disk stay 500s with their tracebacks: those are bugs to see, not outages.
_DB_DOWN_SQLSTATES = frozenset({"57P01", "57P02", "57P03", "53300"})
_DB_LOG_EVERY_S = 10.0
_db_log: dict = {"at": None, "suppressed": 0}


def _db_unavailable(exc: BaseException) -> bool:
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    if not isinstance(exc, psycopg.OperationalError):
        return False
    state = exc.sqlstate
    return state is None or state.startswith("08") or state in _DB_DOWN_SQLSTATES


def _db_unavailable_response(path: str, exc: BaseException) -> JSONResponse:
    now = time.monotonic()
    if _db_log["at"] is None or now - _db_log["at"] >= _DB_LOG_EVERY_S:
        more = _db_log["suppressed"]
        _db_log.update(at=now, suppressed=0)
        logger.warning("database unavailable: 503 for %s (%s: %s)%s", path,
                       type(exc).__name__, exc, f"; {more} more since the last line" if more else "")
    else:
        _db_log["suppressed"] += 1
    return JSONResponse({"detail": "Database temporarily unavailable"},
                        status_code=503, headers={"Retry-After": "5"})


async def _db_error_handler(request: Request, exc: Exception):
    # WebSocket scopes keep their own error paths (a response after accept
    # would mask the error).
    if request.scope.get("type") != _HTTP_SCOPE or not _db_unavailable(exc):
        raise exc
    return _db_unavailable_response(request.url.path, exc)


class _DatabaseUnavailableGuard:
    """Outermost: the same 503 for a database error raised inside a
    middleware (the sliding cookie refresh reads the user after the route
    has answered), which the route-level handler never sees."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != _HTTP_SCOPE:
            return await self.app(scope, receive, send)
        started = False

        async def _send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception as exc:
            if started or not _db_unavailable(exc):
                raise
            await _db_unavailable_response(scope.get("path", ""), exc)(scope, receive, send)


def install_db_unavailable_guards(target: FastAPI) -> None:
    """Route-level handler (it answers before any middleware turns the error
    into its own 500) plus the outermost guard; call after the middlewares
    are registered."""
    target.add_exception_handler(psycopg.OperationalError, _db_error_handler)
    target.add_middleware(_DatabaseUnavailableGuard)


install_db_unavailable_guards(app)


# --- Connection limits ---

class _HeaderDeadlineProtocol(HttpToolsProtocol):
    """uvicorn's httptools protocol plus a deadline for the request headers.

    uvicorn arms no timer on a new connection, so a socket that never sends
    a byte (or sends half a header) is held forever: a few thousand of them
    fill the descriptor table or ``limit_concurrency`` and every new request
    is refused. The timer runs from the connection (and from each new
    request on a kept-alive one) until the headers are complete, so
    WebSocket upgrades, slow bodies and streaming responses are unaffected.
    uvicorn is pinned (proxy/requirements.in); its keep-alive timer is untouched."""

    header_deadline_s = 15.0
    _header_timer = None

    def connection_made(self, transport):
        super().connection_made(transport)
        self._arm_header_deadline()

    def on_message_begin(self):
        super().on_message_begin()
        self._arm_header_deadline()

    def on_headers_complete(self):
        self._cancel_header_deadline()
        super().on_headers_complete()

    def connection_lost(self, exc):
        self._cancel_header_deadline()
        super().connection_lost(exc)

    def _arm_header_deadline(self):
        self._cancel_header_deadline()
        self._header_timer = self.loop.call_later(self.header_deadline_s, self._header_deadline_expired)

    def _cancel_header_deadline(self):
        if self._header_timer is not None:
            self._header_timer.cancel()
            self._header_timer = None

    def _header_deadline_expired(self):
        self._header_timer = None
        if self.transport is not None and not self.transport.is_closing():
            self.transport.close()


class _BoundedWebSocketProtocol(WebSocketsSansIOProtocol):
    """uvicorn's sans-I/O WebSocket protocol plus a bound on what a slow
    reader may hold.

    The base protocol writes every frame straight into the transport and
    never pauses a sender, so a reader that stops draining grows the
    buffer without limit. Here the transport's write-buffer limits are
    set (``WS_WRITE_BUFFER_MAX_BYTES``), uvloop's ``pause_writing`` clears
    the ``writable`` event every ``send`` awaits (a frame of any size still
    goes out whole: the check runs before the write), and a pause arms a
    stall check: a buffer that shrank since is progress and is checked
    again; one that did not shrink in ``WS_WRITE_STALL_S`` is aborted,
    never closed (uvloop's close waits for the buffer to drain, the one
    thing a dead reader never does; the keepalive's pong timeout starts
    such a close first, and the stall check aborts it when it cannot
    drain). uvloop counts the buffered bytes down as the kernel takes
    them, in the kernel's own chunks, so a reader slower than the buffer
    per stall window (about 70 KiB/s at the defaults) can read as
    stalled. A close while paused aborts at once
    so nothing that closes sockets under a lock waits on a dead reader,
    and a sender woken by the connection's loss sees a disconnect instead
    of a write to a closed handle. uvicorn is pinned
    (proxy/requirements.in)."""

    _stall_timer = None
    _paused_at_bytes = 0
    _lost = False

    def connection_made(self, transport):
        super().connection_made(transport)
        high = config.WS_WRITE_BUFFER_MAX_BYTES
        transport.set_write_buffer_limits(high=high, low=high // 4)

    def pause_writing(self):
        self.writable.clear()
        self._paused_at_bytes = self.transport.get_write_buffer_size()
        self._arm_stall_check()

    def resume_writing(self):
        self._cancel_stall_check()
        self.writable.set()

    def connection_lost(self, exc):
        self._cancel_stall_check()
        self._lost = True
        self.writable.set()
        super().connection_lost(exc)

    async def send(self, message):
        if not self.writable.is_set():
            if message["type"] == "websocket.close":
                self.close_sent = True
                self.queue.put_nowait({"type": "websocket.disconnect",
                                       "code": message.get("code", 1000)})
                self.transport.abort()
                return
            await self.writable.wait()
            if self._lost:
                raise ClientDisconnected()
        await super().send(message)

    def _arm_stall_check(self):
        self._cancel_stall_check()
        self._stall_timer = self.loop.call_later(config.WS_WRITE_STALL_S, self._stall_check)

    def _cancel_stall_check(self):
        if self._stall_timer is not None:
            self._stall_timer.cancel()
            self._stall_timer = None

    def _stall_check(self):
        self._stall_timer = None
        if self.transport is None or self._lost or self.writable.is_set():
            return
        size = self.transport.get_write_buffer_size()
        if size < self._paused_at_bytes:
            self._paused_at_bytes = size
            self._arm_stall_check()
            return
        logger.warning(
            "WebSocket %s read nothing for %.0fs with %d bytes waiting; dropping it",
            "%s:%s" % self.client if self.client else "?", config.WS_WRITE_STALL_S, size,
        )
        self.transport.abort()


class _ClientAddressShim:
    """The outermost ASGI layer: the one resolver of the client address
    (``auth.lan_check``) runs once per connection and puts the resolved
    client in ``scope["client"]``, so uvicorn's access log, the middleware's
    log and every handler see it. uvicorn's own forwarded-header rewrite is
    off (``proxy_headers=False``): it trusted loopback only, never the
    configured proxies, and lan_check reads the headers itself."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in (_HTTP_SCOPE, _WS_SCOPE):
            lan_check.stamp_scope(scope)
        await self.app(scope, receive, send)


def _bind_internal_listener():
    """The internal listener: 127.0.0.1 on a port the kernel picks.
    Sandboxes and the satellite tunnel reach the proxy there; forwarding
    headers are never read on it (``auth.lan_check``)."""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock


def _build_server(asgi_app):
    """The server and its two sockets, bound but not served: the main
    listener (``PROXY_HOST``:``PORT``) and the internal one. Publishes the
    internal port as ``config.INTERNAL_LISTENER_PORT`` before the lifespan
    runs (it may spawn sessions that need it)."""
    import uvicorn

    cfg = uvicorn.Config(
        _ClientAddressShim(asgi_app), host=config.HOST, port=config.PORT, log_level="info",
        timeout_keep_alive=2,
        http=_HeaderDeadlineProtocol,
        # "on", not "auto": under "auto" an exception on the lifespan scope
        # reads as "lifespan unsupported" and the proxy serves without its
        # startup.
        lifespan="on",
        interface="asgi3",
        proxy_headers=False,
        # Above this many open connections (WebSockets included) new HTTP
        # requests get a 503; WebSocket upgrades return before the check, so
        # dashboards and satellites are never refused by it.
        limit_concurrency=config.HTTP_LIMIT_CONCURRENCY or None,
        access_log=True,
        # No uvicorn-owned handlers: its default config installs synchronous
        # stream handlers (one access line per request, written on the
        # loop). With none, the uvicorn loggers propagate to the root queue
        # handler and take the root format (timestamped, off the loop).
        log_config=None,
        # Use the modern sans-I/O websockets implementation, NOT uvicorn's
        # default legacy one. The legacy impl (websockets/legacy/protocol.py)
        # asserts in `_drain_helper` when two coroutines drain the socket at
        # once, e.g. the keepalive PING / auto-PONG colliding with a large
        # app send. Under a fresh satellite's full sync (heavy concurrent
        # writes) that reliably crashed the satellite WS (1011) mid-install;
        # it also caused sporadic dashboard drops. The sans-I/O impl serializes
        # writes correctly and is the maintained path (legacy is deprecated).
        # Requires uvicorn>=0.35; we pin 0.49.
        ws=_BoundedWebSocketProtocol,
        ws_ping_interval=WS_PING_INTERVAL_S,
        # Generous pong timeout (30s, not 10s) so a slow/busy satellite (e.g. a
        # laptop mid-spawn that briefly starves its event loop) isn't dropped with
        # "1011 keepalive ping timeout" on a transient stall. INTERVAL stays 20s
        # (< the 60s reverse-proxy read timeout) so sockets stay warm. Symmetric
        # with satellite/ws_client.py _WS_PING_TIMEOUT.
        ws_ping_timeout=WS_PING_TIMEOUT_S,
        # Force-close lingering connections (satellite / dashboard / phone
        # WebSockets) 10s after SIGTERM so `systemctl restart` doesn't hang in
        # uvicorn's graceful drain until systemd's 90s TimeoutStopSec SIGKILL.
        timeout_graceful_shutdown=10,
    )
    main = cfg.bind_socket()
    internal = _bind_internal_listener()
    for sock in (main, internal):
        sock.set_inheritable(False)
    config.INTERNAL_LISTENER_PORT = internal.getsockname()[1]
    return uvicorn.Server(cfg), [main, internal]


class _EveryFewSeconds(logging.Filter):
    """Lets one record with ``message`` through per ``every_s`` (the next one
    carries the count it held back): uvicorn logs a WARNING per request it
    refuses at the concurrency limit, a flood exactly when overloaded."""

    def __init__(self, message: str, every_s: float):
        super().__init__()
        self.message = message
        self.every_s = every_s
        self._at: float | None = None
        self._held = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if record.getMessage() != self.message:
            return True
        now = time.monotonic()
        if self._at is not None and now - self._at < self.every_s:
            self._held += 1
            return False
        if self._held:
            record.msg = f"{self.message} ({self._held} more held back)"
            record.args = ()
        self._at, self._held = now, 0
        return True


# --- Include routers ---

from api.auth import auth as auth_router
from api.sessions import sessions as sessions_router
from api.hooks import hooks as hooks_router
from api.tasks import tasks as tasks_router
from api.tasks import delegation as delegation_router
from api.tasks import continuations as continuations_router
from api.events import triggers as triggers_router
from api.agents import agents as agents_router
from api.agents import chats as chats_router
from api.departments import departments as departments_router
from api.notifications import notifications as notifications_router
from api.mcp import credentials as credentials_router
from api.mcp import gateway as mcp_gateway_router
from api.auth import oauth as oauth_router
from api.mcp import mcps as mcps_router
from api.mcp import community as community_router
from api.mcp import icons as mcp_icons_router
from api.mcp import local_templates as local_templates_router
from api.media import uploads as uploads_router
from api.media import images as images_router
from api.media import media as media_router
from api.media import ui as ui_router
from api.apps import apps as apps_router
from api.apps import app_actions as app_actions_router
from api.apps import app_proxy as app_proxy_router
from api.apps import app_bindings as app_bindings_router
from api.apps import app_secrets as app_secrets_router
from api.apps import app_egress as app_egress_router
from api.apps import app_inbound as app_inbound_router
from api.sharing import shares as shares_router
from api.sharing import external as external_shares_router
from api.media import wopi as wopi_router
from api.billing import usage as usage_router
from api.admin import admin_storage as admin_storage_router
from api.meetings import meetings as meetings_router
from api.admin import execution_layers as execution_layers_router
from api.auth import claude_oauth as claude_oauth_router
from api.auth import openai_oauth as openai_oauth_router
from api.auth import setup as setup_router
from api.phone import phone as phone_router
from api.phone import phone_relay as phone_relay_router
from api.phone import phone_usage as phone_usage_router
from api.phone import twilio_relay as twilio_relay_router
from api.audio import audio as audio_router
from api.duplex import duplex as duplex_router
from api.admin import title_generation as title_generation_router
from api.internal import internal as internal_router
from api.agent_data import memory as memory_router
from api.agent_data import git_history as git_history_router
from api.remote import remote_machines as remote_machines_router
from api.media import collabora_proxy as collabora_proxy_router
from api.auth import agent_api_keys as agent_api_keys_router
from api.auth import user_api_keys as user_api_keys_router
from api.events import webhooks as webhooks_router
from api.events import subscriptions as subscriptions_router
from api.billing import billing as billing_router
from api.billing import account as account_router
from api.checks import checks as checks_router

app.include_router(auth_router.router)
app.include_router(setup_router.router)
app.include_router(sessions_router.router)
app.include_router(hooks_router.router)
app.include_router(tasks_router.router)
app.include_router(delegation_router.router)
app.include_router(continuations_router.router)
app.include_router(triggers_router.router)
app.include_router(webhooks_router.router)
app.include_router(subscriptions_router.router)
app.include_router(agent_api_keys_router.router)
app.include_router(user_api_keys_router.router)
app.include_router(billing_router.router)
app.include_router(account_router.router)
app.include_router(agents_router.router)
app.include_router(chats_router.router)
app.include_router(departments_router.router)
app.include_router(notifications_router.router)
app.include_router(credentials_router.router)
app.include_router(mcp_gateway_router.router)
# claude_oauth and openai_oauth use the same `/v1/oauth/{provider}/*` prefix
# as the generic MCP OAuth router; FastAPI routing is first-match-wins, so
# they MUST register before `oauth_router` to keep `/v1/oauth/claude/*`
# and `/v1/oauth/openai/*` from being shadowed.
app.include_router(claude_oauth_router.router)
app.include_router(openai_oauth_router.router)
app.include_router(oauth_router.router)
app.include_router(mcps_router.router)
app.include_router(community_router.router)
app.include_router(mcp_icons_router.router)
app.include_router(local_templates_router.router)
app.include_router(execution_layers_router.router)
app.include_router(uploads_router.router)
app.include_router(images_router.router)
app.include_router(media_router.router)
app.include_router(ui_router.router)
app.include_router(apps_router.router)
app.include_router(app_actions_router.router)
app.include_router(app_proxy_router.router)
app.include_router(app_bindings_router.router)
app.include_router(app_secrets_router.router)
app.include_router(app_egress_router.router)
app.include_router(app_inbound_router.router)
app.include_router(checks_router.router)
app.include_router(shares_router.router)
app.include_router(external_shares_router.router)
app.include_router(wopi_router.router)
app.include_router(usage_router.router)
app.include_router(admin_storage_router.router)
app.include_router(meetings_router.router)
app.include_router(phone_router.router)
app.include_router(phone_relay_router.router)
app.include_router(phone_usage_router.router)
app.include_router(twilio_relay_router.router)
app.include_router(audio_router.router)
app.include_router(duplex_router.router)
app.include_router(title_generation_router.router)
app.include_router(internal_router.router)
app.include_router(memory_router.router)
app.include_router(git_history_router.router)
app.include_router(remote_machines_router.router)
app.include_router(collabora_proxy_router.router)


# --- WebSocket endpoints ---

from ws.phone import ws_phone_handler
from ws.dashboard import ws_dashboard_handler
from ws.phone_management import ws_phone_management_handler
from ws.satellite import ws_satellite_handler
from ws.audio import ws_audio_stt_handler, ws_audio_tts_handler
from ws.duplex import ws_duplex_handler, ws_duplex_engine_handler

app.add_api_websocket_route("/ws/phone", ws_phone_handler)
app.add_api_websocket_route("/ws/dashboard", ws_dashboard_handler)
app.add_api_websocket_route("/ws/phone-management", ws_phone_management_handler)
app.add_api_websocket_route("/v1/satellite", ws_satellite_handler)
app.add_api_websocket_route("/ws/audio/stt", ws_audio_stt_handler)
app.add_api_websocket_route("/ws/audio/tts", ws_audio_tts_handler)
app.add_api_websocket_route("/ws/duplex", ws_duplex_handler)
app.add_api_websocket_route("/ws/duplex-engine/{duplex_id}", ws_duplex_engine_handler)
# Twilio's media WebSocket enters through the public proxy and is bridged to
# the phone daemon (see api/phone/twilio_relay.py — daemon-side signature auth).
app.add_api_websocket_route(
    "/v1/twilio/media/{server_id}", twilio_relay_router.ws_twilio_media_relay)


# --- Dashboard static files ---

# The build writes .br/.gz siblings next to every text asset and the mount
# serves whichever the client accepts (static_assets.py). Vite content-hashes
# the file names, so those are immutable for a year; the unhashed favicon
# copied from public/ stays revalidated.
if config.DASHBOARD_ENABLED and config.DASHBOARD_DIST.exists():
    app.mount(
        "/assets",
        PrecompressedStaticFiles(
            directory=str(config.DASHBOARD_DIST / "assets"), immutable=is_hashed_asset),
        name="dashboard-assets",
    )

# The wake-word wasm/model bundle (~18 MB the browser must never re-download):
# the version directory in the URL is the cache buster, so every file is
# immutable.
if config.DASHBOARD_ENABLED and config.KWS_ASSETS_DIR.exists():
    app.mount(
        "/kws-assets",
        PrecompressedStaticFiles(directory=str(config.KWS_ASSETS_DIR), immutable=True),
        name="kws-assets",
    )


def _safe_dashboard_file(path: str):
    """Resolve ``path`` under DASHBOARD_DIST, returning the file Path only if it
    stays inside DIST — else None (caller falls through to index.html / 404).

    uvicorn has already percent-decoded ``scope['path']`` (so ``%2e%2f`` →
    ``../``) and the ``{path:path}`` convertor does NOT normalize dot-segments,
    so a substring ``".." not in path`` check is insufficient. Resolve and
    confine instead.
    """
    dist = config.DASHBOARD_DIST.resolve()
    candidate = (dist / path).resolve()
    if candidate.is_relative_to(dist) and candidate.is_file():
        return candidate
    return None


# GET and HEAD on the pages: a HEAD the catch-all below takes must find the
# same route the GET finds.
@app.api_route("/dashboard/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def dashboard_legacy_redirect(path: str):
    """Redirect old /dashboard/* URLs to the new subdomain root."""
    if config.DASHBOARD_PUBLIC_URL:
        from starlette.responses import RedirectResponse
        return RedirectResponse(url=f"{config.DASHBOARD_PUBLIC_URL}/{path}")
    if not config.DASHBOARD_ENABLED or not config.DASHBOARD_DIST.exists():
        raise HTTPException(status_code=404, detail="Dashboard not enabled")
    safe = _safe_dashboard_file(path)
    if safe is not None:
        return FileResponse(str(safe))
    return FileResponse(str(config.DASHBOARD_DIST / "index.html"))


# The API docs routes are off (see the FastAPI call); their paths answer 404,
# not the SPA's index.html.
_RETIRED_API_DOCS = frozenset({"docs", "docs/oauth2-redirect", "redoc", "openapi.json"})


# SPA catch-all: serve index.html for all paths that don't match API/auth/ws routes.
# This MUST be registered last so it doesn't shadow other routes. HEAD is
# answered as GET with no body (uptime probes); a HEAD on a GET-only API route
# also lands here and meets the reserved prefixes' 404.
@app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def spa_catchall(path: str, request: Request):
    """Serve the React SPA for client-side routing on the subdomain."""
    # kws-assets/ included: a worker importScripts miss must 404 loudly, never
    # serve index.html-as-JS (same discipline as ui-kit below).
    # ``.well-known/oauth-*``: an MCP client's OAuth discovery against the
    # credential gateway's origin must fail fast, never find the SPA.
    if path.startswith(("v1/", "auth/", "ws/", "api/", "assets/", "wopi/",
                        "collabora/", "kws-assets/", "s/", ".well-known/oauth-")) \
            or path in _RETIRED_API_DOCS:
        raise HTTPException(status_code=404, detail="Not found")
    if not config.DASHBOARD_ENABLED or not config.DASHBOARD_DIST.exists():
        raise HTTPException(status_code=404, detail="Dashboard not enabled")
    # Serve actual files from dist/ (favicon, APK downloads, etc.)
    safe = _safe_dashboard_file(path)
    if safe is not None:
        if path.startswith("ui-kit/"):
            # Artifact iframes run at an OPAQUE origin (Origin: null), and
            # @font-face fetches are CORS-mode requests (unlike script/style/
            # img) — without this header the kit woff2s are CORS-blocked and
            # artifacts silently fall back to system fonts. Public static
            # assets, no credentials: '*' is correct. The kit JS (echarts,
            # three, tailwind) has precompressed siblings like /assets; the
            # kit names are stable across releases, so no immutable caching —
            # and `no-cache` (revalidate on every load, a 304 on the ETag),
            # or a browser's heuristic freshness keeps running the previous
            # share-host.js after an upgrade (SHARING.md "External links").
            return negotiated_file_response(
                safe, safe.stat(), request.headers,
                extra_headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})
        # Unhashed names that keep their URL across releases (the wake-word
        # worker, the manifest, the APKs): revalidate on every load (a 304
        # on the ETag), or a browser's heuristic freshness keeps running the
        # previous file after the page reloaded onto a new build.
        return FileResponse(str(safe), headers={"Cache-Control": "no-cache"})
    # /ui-kit/* are subresources of sandboxed artifact iframes (echarts, tokens
    # CSS, fonts) — a miss must 404 loudly, not serve index.html with 200
    # (a <script src> would silently load HTML-as-JS on a build/copy mistake).
    if path.startswith("ui-kit/"):
        raise HTTPException(status_code=404, detail="Not found")
    # index.html must never be cached — it references hashed JS/CSS bundles
    return FileResponse(
        str(config.DASHBOARD_DIST / "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


if __name__ == "__main__":
    logger.info(f"Starting OtoDock proxy on {config.HOST}:{config.PORT}")
    from storage.agents import agent_store as _as
    try:
        logger.info(f"Agents: {', '.join(_as.get_agent_slugs())}")
    except Exception as e:
        # Fresh DB: the schema (init_schema / run_migrations) is
        # created by the lifespan startup AFTER this banner runs. Don't crash
        # the boot just to log agent slugs — they exist once the app serves.
        logger.info(f"Agents: (schema not yet initialized — {type(e).__name__})")
    logger.info(f"Working dir: {config.AGENTS_DIR}")
    logger.info("MCP configs: per-agent (in agents/<name>/mcp-config.json)")
    # uvicorn re-raises the SIGTERM/SIGINT it served once its graceful
    # shutdown is done (the process exits by the signal, so atexit never
    # runs) — with this as the restored handler the log queue is drained
    # before the default disposition ends the process.
    import signal
    for _sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(_sig, log_queue.exit_signal_handler)
    logging.getLogger("uvicorn.error").addFilter(
        _EveryFewSeconds("Exceeded concurrency limit.", 10.0))
    server, sockets = _build_server(app)
    logger.info(f"Internal listener on 127.0.0.1:{config.INTERNAL_LISTENER_PORT}")
    server.run(sockets=sockets)
    if not server.started:
        # The startup failed (uvicorn.run exits with the same code).
        import sys
        sys.exit(3)

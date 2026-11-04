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
import secrets
import socket
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiohttp import web

from .. import config
from ..background import spawn
from ..config import HOOK_WAIT_S
from .mcp_gateway import GATEWAY_PATH_PREFIX, LocalMcpGateway

if TYPE_CHECKING:
    from .ws_client import SatelliteWSClient

logger = logging.getLogger("satellite.tunnel")


# Path allowlist enforced on the SATELLITE side as defense-in-depth.
# The platform allowlist mirrors this; both must agree before a request
# can reach an internal endpoint. Anchored ^...$ to defeat traversal.
_ALLOWLIST_REGEXES = [
    re.compile(
        r"^/v1/hooks/(resolve-path|resolve-tool-arg-paths|permission|"
        # The command-line credential git and gh ask for per command
        # (0.5.139), session-JWT gated proxy-side.
        r"mcp-credentials|session-files|cli-credential|"
        r"images|image-generating|image-gen-failed|url|file|media|ui|"
        r"document-preview|tool-result|file-written|subagent|"
        # The Stop hook (turn end, both engines) and the Codex question
        # bridge (request_user_input → the dashboard card), 0.5.121; both
        # session-JWT gated proxy-side like every hook.
        r"stop|codex-question)$"
    ),
    # display-mcp pinned apps (pin/unpin/list) — session-JWT gated
    # proxy-side like every hook (verify_session_match + scope from the
    # session ctx); the artifact hook itself is the `ui` entry above.
    re.compile(r"^/v1/hooks/apps/(pin|unpin|list|push|state|open|rollback|"
               r"deploy|check|status|preview|logs|restart|purge|describe|export|import|screenshot)$"),
    # Agents calling apps (APPS.md "Agents call apps"): an app's own API,
    # its platform methods and the push/state twins, judged proxy-side by
    # the session JWT as basis `agent`. Mirrored in the proxy's list.
    re.compile(r"^/v1/apps/[0-9a-f-]{36}/(api|platform|push|state)(/.*)?$"),
    # An app step's script pressing one of its app's buttons (APPS.md
    # "Steps", 0.5.122): the route accepts only the step's own in-flight
    # claim as the bearer; never the batch route. Mirrored in the proxy.
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
    # delegation-mcp's move_chat (the chat the session runs in, satellite
    # 0.5.157): session-token only proxy-side; never the person's
    # /v1/chats/<id>/move.
    re.compile(r"^/v1/chats/agent-move$"),
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
    # The settings tool (settings-mcp; 0.5.159): the catalog a
    # person's own session reads and the card it asks for.
    re.compile(r"^/v1/settings/(catalog|card)$"),
    # The agent-creator tool's proposal cards (agent-creator-mcp; 0.5.162):
    # the draw and the read of a card's state, exact shapes. Never the
    # person's click (`/v1/cards/<id>/act` takes a sign-in, never a session).
    re.compile(r"^/v1/cards(/[A-Za-z0-9-]{1,64})?$"),
    # The same tool's reads (0.5.163): the community catalog's templates and
    # department sets, and the departments the person may see. Exact paths;
    # the routes judge the session's role proxy-side.
    re.compile(r"^/v1/community/(agents|departments)$"),
    re.compile(r"^/v1/departments$"),
    re.compile(r"^/mcp/[a-z0-9_-]+/.*$"),
]


# Response queue capacity per stream. 64 chunks × 256KB max-per-chunk = 16MB
# headroom per stream before backpressure kicks in (queue.put blocks).
_RESPONSE_QUEUE_SIZE = 64


def _has_traversal(base: str) -> bool:
    """True if ``base`` has a dot segment, an encoded separator, a backslash
    or a NUL. Rejected before allowlist matching so a `../`-style path can't
    match an allowlisted prefix and then be normalized onto a different
    endpoint upstream. A byte twin of the platform's authority
    (``proxy/auth/request_path.py``), pinned by the release gate's twin rule
    — keep the body identical.
    """
    low = base.lower()
    if "%2e" in low or "%2f" in low or "%5c" in low or "%00" in low or "\\" in base or "\x00" in base:
        return True
    return any(seg in (".", "..") for seg in base.split("/"))


def _is_allowed_path(path: str) -> bool:
    base = path.split("?", 1)[0]
    if _has_traversal(base):
        return False
    return any(rx.match(base) for rx in _ALLOWLIST_REGEXES)


# The local link's self-check (0.5.168). Answered by this server from its own
# memory, ahead of the gateway, the allowlist and the link check: never
# tunneled, never on either allowlist.
_SELF_CHECK_PATH = "/v1/tunnel/self-check"
# One round trip's wait. A refused loopback connect takes about a second on
# Windows (the SYN is retried after the reset).
_CHECK_TIMEOUT_S = 3.0
# Slow answers in a row before a rebind: a starved loop answers late, a
# wedged listener never does.
_SLOW_BEFORE_REBIND = 3
# A link that stays down repeats its warning this often at most.
_DOWN_REPEAT_S = 300.0
# The 503 a request gets while the link to the platform is down is logged
# this often at most.
_REFUSAL_LOG_EVERY_S = 60.0
# CPython's Proactor loop (Windows) closes a listening socket for good after
# one failed accept and reports it to the loop's exception handler with this
# message (proactor_events._start_serving).
_ACCEPT_FAILED = "Accept failed on a socket"
_ERROR_MAX = 200


def _short(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text if isinstance(exc, OSError) else f"{type(exc).__name__}: {text}"


def _clean(text: str) -> str:
    """One line of printable text, bounded: it reaches the platform's log."""
    return "".join(ch if ch.isprintable() else " " for ch in text)[:_ERROR_MAX]


def _sock_address(sock) -> str:
    try:
        host, port = sock.getsockname()[:2]
        return f"{host}:{port}"
    except Exception:  # noqa: BLE001  # a socket the loop already closed names nothing
        return "a socket"


class _SelfCheckAccessFilter(logging.Filter):
    """Keeps the self-check (one request every heartbeat) out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        return _SELF_CHECK_PATH not in record.getMessage()


def make_loop_exception_handler(tunnel: "LocalTunnelServer", *, quiet_close_resets: bool):
    """The satellite loop's exception handler.

    An accept failure is never swallowed, whatever its exception: the
    Proactor closes the listening socket right after this handler returns,
    so it is logged and a heal is started (a task, which runs after the
    close). ``quiet_close_resets`` (Windows) drops the close-time
    ``ConnectionResetError`` noise of the Proactor's transports, a socket
    torn down after its peer went. Everything else reaches the default
    handler.
    """
    def handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        message = str(context.get("message") or "")
        exc = context.get("exception")
        if message.startswith(_ACCEPT_FAILED):
            logger.warning(
                "tunnel: a listening socket stopped accepting (%s on %s); checking the local link",
                _short(exc) if exc is not None else "no error", _sock_address(context.get("socket")),
            )
            tunnel.heal_soon("an accept failure")
            return
        if quiet_close_resets and (isinstance(exc, ConnectionResetError) or "WinError 10054" in message):
            return
        loop.default_exception_handler(context)
    return handler


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
        # The listening socket is ours (``SockSite``): a check sees it closed
        # under the server, as the Proactor leaves it after a failed accept.
        self._site: web.SockSite | None = None
        self._sock: socket.socket | None = None
        # stream_id → _PendingStream
        self._streams: dict[str, _PendingStream] = {}
        # The credential gateway for vendor MCPs (``/v1/mcp-gateway/``):
        # served here, never tunneled.
        self.gateway = LocalMcpGateway()
        # The local link's state, in every heartbeat and the auth
        # capabilities (``state()``).
        self.ok = True
        self.rebinds = 0
        self.last_error = ""
        self._slow = 0
        self._down_logged_at = 0.0
        self._refusal_logged_at: float | None = None
        # The self-check's answer: another program on a reused port never passes.
        self._token = secrets.token_hex(16)
        self._heal_lock = asyncio.Lock()
        self._heal_task: asyncio.Task | None = None
        self._heal_chained = False
        self._stopping = False
        # Told the new port when a heal could not keep the old one.
        self.on_port: Callable[[int], None] | None = None
        access = logging.getLogger("aiohttp.access")
        if not any(isinstance(f, _SelfCheckAccessFilter) for f in access.filters):
            access.addFilter(_SelfCheckAccessFilter())

    async def start(self) -> int:
        """Serve on 127.0.0.1:<ephemeral>. Returns the chosen port."""

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

        port = await self._listen(0)
        self.gateway.start()

        logger.info("Local tunnel server listening on 127.0.0.1:%d", port)
        return port

    async def _listen(self, port: int, *, share: bool = False) -> int:
        """Serve on 127.0.0.1:<port> (0: the OS picks) through a socket of our
        own. ``socket.create_server`` sets SO_REUSEADDR on POSIX only, as
        asyncio does, so a plain bind never takes a port another program
        holds. ``share`` sets it where the OS does not (Windows), for a rebind
        onto a port only this server's old connections still hold."""
        if share:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", port))
                sock.listen(128)
            except OSError:
                sock.close()
                raise
        else:
            sock = socket.create_server(("127.0.0.1", port), backlog=128)
        site = web.SockSite(self._runner, sock, backlog=128)
        try:
            await site.start()
        except BaseException:
            with contextlib.suppress(Exception):
                await site.stop()
            sock.close()
            raise
        self._site, self._sock = site, sock
        self.port = sock.getsockname()[1]
        return self.port

    async def _close_site(self) -> None:
        """Stop serving on the current socket, best effort. The handlers of
        connections it accepted run on (the runner stays)."""
        site, sock = self._site, self._sock
        self._site = self._sock = None
        if site is not None:
            # On a selector loop the first stop of a server whose socket was
            # closed under it raises before aiohttp unregisters the site; a
            # second stop finds the server closed and unregisters it. A site
            # the runner no longer holds raises RuntimeError.
            for _ in range(2):
                try:
                    await site.stop()
                    break
                except RuntimeError:
                    break
                except Exception:  # noqa: BLE001  # a dead listener's own close error
                    continue
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

    async def stop(self) -> None:
        """Shut down the aiohttp server and reject all pending streams."""
        self._stopping = True
        task = self._heal_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        async with self._heal_lock:
            # Reject pending streams first so handlers wake up and return.
            for _stream_id, stream in list(self._streams.items()):
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

            await self.gateway.stop()
            await self._close_site()
            if self._runner is not None:
                await self._runner.cleanup()
            self._runner = None

    # --- The local link's self-check ---

    def state(self) -> dict:
        """The link's state as the heartbeat and the auth capabilities carry it."""
        return {
            "tunnel_ok": self.ok,
            "local_tunnel_port": self.port,
            "tunnel_rebinds": self.rebinds,
            "last_tunnel_error": self.last_error,
        }

    def _listener_closed(self) -> bool:
        sock = self._sock
        return sock is None or sock.fileno() == -1

    async def _round_trip(self) -> tuple[str, bool]:
        """One request to the self-check path through the listening socket,
        as a hook script makes it. ``("", False)`` when this server answers;
        else the failure and whether it is definitive (a slow answer is not:
        a starved loop answers late)."""
        where = f"127.0.0.1:{self.port}"
        if self._listener_closed():
            return f"the listening socket at {where} was closed", True

        async def _ask() -> bytes:
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            try:
                writer.write(
                    f"GET {_SELF_CHECK_PATH} HTTP/1.1\r\nHost: {where}\r\n"
                    "Connection: close\r\n\r\n".encode()
                )
                await writer.drain()
                return await reader.read()
            finally:
                writer.close()

        try:
            answer = await asyncio.wait_for(_ask(), _CHECK_TIMEOUT_S)
        except asyncio.TimeoutError:
            return f"no answer at {where} within {_CHECK_TIMEOUT_S:.0f} s", False
        except ConnectionRefusedError:
            return f"connection refused at {where}", True
        except OSError as e:
            return f"{_short(e)} at {where}", True
        if not answer.startswith(b"HTTP/1.1 200") or self._token.encode() not in answer:
            return f"another program answers at {where}", True
        return "", False

    async def check_and_heal(self, trigger: str) -> bool:
        """Check the local link and heal it when it is broken; ``self.ok``.
        The work is a task this server owns, awaited through a shield: a
        caller cancelled (the heartbeat of a dropped link) never stops a
        rebind halfway, and a check already running is joined."""
        task = self._heal_task
        if task is None or task.done():
            task = self._heal_task = spawn(
                self._check_and_heal(trigger, report=False), name="tunnel-heal", owner="tunnel")
        return await asyncio.shield(task)

    def heal_soon(self, trigger: str) -> None:
        """Start a heal from a callback (the loop's exception handler). A
        check already running may have passed its round trip before the
        socket closed: the heal follows it at its end (once, however many
        failures arrive meanwhile). A heal that rebinds tells the platform
        at once (``heartbeat_now``)."""
        if self._stopping:
            return
        task = self._heal_task
        if task is not None and not task.done():
            if not self._heal_chained:
                self._heal_chained = True
                task.add_done_callback(lambda _t, trigger=trigger: self._chained_heal(trigger))
            return
        self._heal_task = spawn(
            self._check_and_heal(trigger, report=True), name="tunnel-heal", owner="tunnel")

    def _chained_heal(self, trigger: str) -> None:
        self._heal_chained = False
        self.heal_soon(trigger)

    async def _check_and_heal(self, trigger: str, *, report: bool) -> bool:
        async with self._heal_lock:
            if self._stopping or self._runner is None:
                return self.ok
            error, definitive = await self._round_trip()
            if not error:
                self._slow = 0
                self._set_state(True)
                return True
            if not definitive:
                self._slow += 1
                if self._slow < _SLOW_BEFORE_REBIND:
                    logger.info("tunnel: the local link answered slowly (%s, %d in a row)",
                                error, self._slow)
                    return self.ok
            self._slow = 0
            # A link already down retries every tick: its own warning
            # repeats every _DOWN_REPEAT_S (``_set_state``), the tries do not.
            level = logging.WARNING if self.ok else logging.INFO
            logger.log(level, "tunnel: the local link failed its check (%s, on %s); rebinding",
                       error, trigger)
            self.last_error = _clean(error)
            rebound = await self._rebind()
            after, after_definitive = await self._round_trip()
            # A slow answer right after a rebind is not a failure: only a
            # definitive one reports the link down. A rebind that could not
            # listen keeps its own reason.
            self._set_state(not (after and after_definitive), after if rebound else "")
        if rebound and report:
            heartbeat_now = getattr(self.ws_client, "heartbeat_now", None)
            if heartbeat_now is not None:
                await heartbeat_now()
        return self.ok

    def _set_state(self, ok: bool, error: str = "") -> None:
        now = time.monotonic()
        if ok and not self.ok:
            logger.warning("tunnel: the local link at 127.0.0.1:%d answers again", self.port)
        elif not ok and (self.ok or now - self._down_logged_at >= _DOWN_REPEAT_S):
            logger.warning("tunnel: the local link is down: %s", error or self.last_error)
            self._down_logged_at = now
        if error:
            self.last_error = _clean(error)
        self.ok = ok

    async def _nobody_listens(self, port: int) -> bool:
        """True when no program accepts on 127.0.0.1:<port> (refused, or no
        answer): the port is held only by connections our old listener
        accepted."""
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), _CHECK_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError):
            return True
        writer.close()
        return False

    async def _rebind(self) -> bool:
        """Listen again on a fresh socket, on the same port when it can: every
        session on this machine holds that port in its environment and its
        MCP addresses. True when it listens again."""
        port = self.port
        await self._close_site()
        how = ""
        try:
            await self._listen(port)
            how = f"on the same port {port}"
        except OSError as e:
            first = e
            if not config.HOST.posix and await self._nobody_listens(port):
                # Windows refuses a plain bind while connections the old
                # listener accepted still hold the port. With nobody else
                # listening there, sharing it with them is this server's own
                # restart, never another program's port.
                try:
                    await self._listen(port, share=True)
                    how = f"on the same port {port}, beside its open connections"
                except OSError:
                    pass
            if not how:
                try:
                    await self._listen(0)
                    how = f"on a new port {self.port} (port {port}: {_short(first)})"
                except OSError as again:
                    self.last_error = _clean(f"could not listen again: {_short(again)}")
                    logger.log(logging.WARNING if self.ok else logging.INFO,
                               "tunnel: the local link could not listen again: %s", _short(again))
                    return False
        self.rebinds += 1
        logger.warning("tunnel: the local link listens again %s (rebind %d)", how, self.rebinds)
        if self.port != port and self.on_port is not None:
            self.on_port(self.port)
        return True

    def _log_refusal(self, path: str) -> None:
        """The 503 of a request made while the link to the platform is down,
        logged once a minute at most with how long the link has been down."""
        now = time.monotonic()
        if self._refusal_logged_at is not None and now - self._refusal_logged_at < _REFUSAL_LOG_EVERY_S:
            return
        self._refusal_logged_at = now
        down_for = getattr(self.ws_client, "link_down_for", None)
        seconds = down_for() if callable(down_for) else None
        logger.warning(
            "tunnel: refused %s with 503: the link to the platform has been down for %s",
            path, f"{seconds:.0f} s" if seconds is not None else "an unknown time",
        )

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
        for _stream_id, stream in list(self._streams.items()):
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

        # The self-check: answered here, whatever the link's state.
        if path == _SELF_CHECK_PATH:
            return web.Response(text=self._token)

        # The credential gateway's own path: served from this machine's
        # memory (the vendor is dialled from here), never tunneled, and
        # ahead of the link check so it serves within its lease while the
        # link is down. A dot segment or an encoded separator is refused
        # before anything else, as for every path.
        if path.startswith(GATEWAY_PATH_PREFIX + "/"):
            if _has_traversal(path):
                return web.Response(
                    status=403,
                    body=json.dumps({"error": "path-not-allowlisted"}).encode(),
                    content_type="application/json",
                )
            tail = path[len(GATEWAY_PATH_PREFIX) + 1:]
            mcp, _, rest = tail.partition("/")
            return await self.gateway.handle(request, mcp, "/" + rest)

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
            self._log_refusal(path)
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
                except (UnicodeDecodeError, Exception):  # noqa: BLE001  # a body the rewrite cannot read is forwarded as it came
                    # Non-UTF-8 body or unexpected failure: forward as-is.
                    pass

            # Strip hop-by-hop headers before forwarding.
            headers = {
                k: v for (k, v) in request.headers.items()
                if k.lower() not in _HOP_BY_HOP_HEADERS
            }

            # The longest hook wait by default (a prompt waits on a person).
            timeout_header = request.headers.get("X-Tunnel-Timeout-S")
            timeout_s = HOOK_WAIT_S
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
            # Polled in short slices so a client that gives up while waiting
            # (claude-code's HTTP timeout, an MCP client reconnecting its GET
            # stream) is noticed and the platform told to close its side
            # (``http_abort``) — before this the proxy held such streams
            # until its idle sweep.
            first = await self._wait_frame(
                stream, request, min(timeout_s + 5, HOOK_WAIT_S + 60),
            )
            if first is _CLIENT_GONE:
                await self._send_abort(stream_id)
                return web.Response(status=499, body=b"")
            if first is None:
                logger.warning(
                    "tunnel: stream %s timed out waiting for first frame",
                    stream_id[:8],
                )
                await self._send_abort(stream_id)
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
                chunk = await self._wait_frame(stream, request, timeout_s + 5)
                if chunk is _CLIENT_GONE:
                    # Subprocess closed the connection mid-stream (an MCP
                    # client reconnecting its GET stream, a hook script that
                    # gave up) — close the platform side too.
                    await self._send_abort(stream_id)
                    break
                if chunk is None:
                    logger.warning(
                        "tunnel: stream %s timed out mid-stream",
                        stream_id[:8],
                    )
                    await self._send_abort(stream_id)
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
                    except (ConnectionResetError, ConnectionError):
                        # Subprocess closed the connection mid-stream — tell
                        # the platform so it closes its upstream now.
                        await self._send_abort(stream_id)
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
            # nothing to do locally; tell the platform so it drops the
            # stream instead of waiting for its idle sweep.
            logger.debug(
                "tunnel: client disconnected mid-handler for %s: %s",
                path, e,
            )
            await self._send_abort(stream_id)
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

    @staticmethod
    def _client_gone(request: web.Request) -> bool:
        """True once the local client (hook script / MCP client) has closed
        its socket. aiohttp does not cancel handlers on disconnect, so the
        frame waits below poll this between slices."""
        try:
            transport = request.transport
        except Exception:  # noqa: BLE001  # defensive: mocked requests in tests
            return False
        return transport is None or transport.is_closing()

    async def _wait_frame(self, stream: "_PendingStream", request: web.Request,
                          timeout_s: float):
        """Await the next platform frame for ``stream`` for up to
        ``timeout_s``, checking every ``_CLIENT_POLL_S`` whether the local
        client is still there. Returns the frame, ``_CLIENT_GONE`` when the
        client disconnected, or ``None`` on timeout."""
        deadline = asyncio.get_running_loop().time() + max(0.0, float(timeout_s))
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            try:
                return await asyncio.wait_for(
                    stream.response_queue.get(),
                    timeout=min(remaining, _CLIENT_POLL_S),
                )
            except asyncio.TimeoutError:
                if self._client_gone(request):
                    return _CLIENT_GONE

    async def _send_abort(self, stream_id: str) -> None:
        """Best-effort ``http_abort`` so the platform closes its upstream for
        a stream whose local client is gone. Older proxies ignore the frame
        (unknown message types are dropped)."""
        try:
            await self.ws_client.enqueue_send({
                "type": "http_abort",
                "stream_id": stream_id,
            })
        except Exception:  # noqa: BLE001  # never let cleanup raise
            logger.debug("tunnel: abort frame for %s not sent", stream_id[:8], exc_info=True)

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

# Local-client liveness polling while a handler waits for platform frames
# (see ``_wait_frame``): aiohttp does not cancel a handler when its client
# disconnects, so without this a client that gave up left the platform-side
# stream open until the proxy's idle sweep. Sentinel returned on disconnect.
_CLIENT_POLL_S = 5.0
_CLIENT_GONE = object()


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

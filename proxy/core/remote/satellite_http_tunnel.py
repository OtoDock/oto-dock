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

logger = logging.getLogger("otodock.satellite-tunnel")


# What an app server may reach through its machine's app listener, on a
# compute machine's link alone (frames marked ``via: "app"``): a byte twin of
# the satellite's list. No hook, no Docker tool, nothing a session's token opens.
_APP_ALLOWLIST_REGEXES = [
    re.compile(r"^/v1/apps/[0-9a-f-]{36}/(api|platform|push|state|egress|events|bindings)(/.*)?$"),
    re.compile(r"^/v1/apps/[0-9a-f-]{36}/actions/[A-Za-z0-9_-]{1,64}$"),
]
_APP_ID_RE = re.compile(r"^/v1/apps/([0-9a-f-]{36})/")
#: One app's concurrent calls on one machine: an app cannot take another's room.
_MAX_APP_STREAMS_PER_APP = 32

# Allowlist mirrored from satellite/http_tunnel.py. Both sides must agree.
_ALLOWLIST_REGEXES = [
    re.compile(
        r"^/v1/hooks/(resolve-path|resolve-tool-arg-paths|permission|"
        # The command-line credential (git and gh, satellite 0.5.139).
        r"mcp-credentials|cli-credential|"
        # A tool's saved answers and its start, on its fetch token (0.5.176).
        r"tool-listing|tool-start|"
        # The session's SSH agent socket, forwarded by the satellite (0.5.173).
        r"ssh-agent|"
        r"images|image-generating|image-gen-failed|url|file|media|ui|"
        r"document-preview|tool-result|file-written|subagent|"
        # The Stop hook (turn end, both engines) and the Codex question
        # bridge (request_user_input → the dashboard card); satellite 0.5.121
        # admits the same two.
        r"stop|codex-question|"
        # A terminal's status line report (hooks/usage_report.py); an older
        # satellite writes no status line and admits none.
        r"usage-report|"
        # A terminal's start hook (the start's riding part); a satellite that
        # reports start_context_hook admits it.
        r"start-context)$"
    ),
    # display-mcp pinned apps (pin/unpin/list) and the live-apps hooks
    # (push/state/open, APPS.md "Live apps") — session-JWT gated
    # proxy-side like every hook (verify_session_match_async + scope from
    # the session ctx); the artifact hook itself is the `ui` entry above.
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
    # PROXY_URL (remote-rewritten to the loopback tunnel).
    # verify_session_match_async still gates each by the session JWT. Keep
    # BOTH allowlists identical.
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
    # The platform's own tools on a machine (satellite 0.5.172): the agent's
    # knowledge libraries (mcps-mcp), a local tool package's check (mcps-mcp),
    # the conversations of an external identity, and the phone's call
    # control; every one session-JWT gated by its route.
    re.compile(r"^/v1/knowledge-libraries(/.*)?$"),
    re.compile(r"^/v1/mcps/local-package/validate$"),
    re.compile(r"^/v1/conversations(/.*)?$"),
    re.compile(r"^/v1/phone/call-control(/.*)?$"),
    # The credential gateway's path, from a compute machine alone (a paired
    # machine serves it itself under its lease; ``handle_request_frame``
    # refuses the row on every other link). Classed ``mcp``: its streams
    # are a session's tool traffic.
    re.compile(r"^/v1/mcp-gateway/[A-Za-z0-9_-]+(/.*)?$"),
    re.compile(r"^/mcp/[a-z0-9_-]+/.*$"),
]

# The credential gateway's path on the tunnel, admitted from a compute
# machine's link alone.
_GATEWAY_PATH_RE = re.compile(r"^/v1/mcp-gateway/[A-Za-z0-9_-]+(/.*)?$")

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
_PROMPT_HOOK_PATHS = frozenset({"/v1/hooks/permission", "/v1/hooks/codex-question"})
_UNCLAMPED_HOOK_PATHS = frozenset({_VERDICT_HOOK_PATH, *_PROMPT_HOOK_PATHS})
# A signature for the session's SSH client: answered at once, never held
# behind the asking machine's file pushes (``_asker_settled``).
_NO_SETTLE_PATHS = frozenset({"/v1/hooks/ssh-agent"})
# A prompt waits on a person up to ``session_state.PROMPT_WAIT_S`` (the
# proxy answers by then; its stream carries keepalives meanwhile): its stream
# lives an hour past that, beyond the absolute cap every other stream keeps.
_PROMPT_STREAM_MAX_S = 3 * 24 * 3600 + 3600


def _stream_timeout_ceiling(path: str) -> int:
    # The verdict hook waits on a check's budget and the two prompt hooks
    # wait on a person; each holds one stream of the hook class on its own
    # pool, so they may outlive the clamp every other stream keeps.
    bare = path.split("?", 1)[0]
    if bare in _PROMPT_HOOK_PATHS:
        return _PROMPT_STREAM_MAX_S
    if bare in _UNCLAMPED_HOOK_PATHS:
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


def _is_app_path(path: str) -> bool:
    """A path an app server may call through its listener."""
    base = path.split("?", 1)[0]
    if has_traversal(base):
        return False
    return any(rx.match(base) for rx in _APP_ALLOWLIST_REGEXES)


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
            from core.config import deployment
            from services.mcp import mcp_registry
            # The path slug is the mcpServers config key, which may be the
            # manifest's `server_name` rather than its canonical `name` (e.g.
            # camoufox registers as ``[mcp_servers.playwright]`` even though the
            # manifest name is "camoufox") — get_manifest_by_config_key applies
            # the same server_name fallback the outbound config rewriter uses, so
            # tunnel routing and URL rewriting stay in lockstep. Without it
            # Docker MCPs with a distinct server_name 404 here ("mcp-not-found").
            manifest = mcp_registry.get_manifest_by_config_key(mcp_name)
        except ImportError:
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


def _takes_gateway_credential(name: str) -> bool:
    """Whether the MCP behind the tunnel slug ``name`` is reached only
    through the gateway's forward: one that takes a credential the gateway
    adds (``credentials.oauth.bearer_required`` or
    ``credentials.api_key_header``), and every Docker tool (its door). Such
    an MCP is never reached by the direct hop."""
    try:
        from services.mcp import mcp_manifest_types as _mt, mcp_registry
        manifest = mcp_registry.get_manifest_by_config_key(name)
    except ImportError:
        return False
    if manifest is not None and _mt.is_container(getattr(manifest, "server", None)):
        return True
    creds = getattr(manifest, "credentials", None)
    oauth = getattr(creds, "oauth", None)
    header = getattr(creds, "api_key_header", None)
    return bool((isinstance(oauth, dict) and oauth.get("bearer_required"))
                or isinstance(header, dict))


async def _gateway_forward(session_id: str, path: str, method: str, headers: dict, body: bytes = b""):
    """The credential gateway's part of a tunneled ``/mcp/<name>/`` request:
    None when the MCP takes no credential and is no Docker tool (the direct
    hop), a refusal when the session holds a vendor credential the
    proxy does not forward itself (a machine session's vendor traffic leaves
    the machine through its own gateway; only a credential an agent's GitHub
    reach narrows comes through here, judged), when the MCP takes a credential
    the session does not hold (a store miss: a session adopted with no
    gateway descriptor; the session token is never handed to the sidecar as
    its token) or when the gateway refuses, else the prepared forward (the
    sidecar's credential added, the session token kept here)."""
    base, _, query = path.partition("?")
    m = _MCP_PATH_RE.match(base)
    if not m:
        return None
    from core.credentials import mcp_gateway
    name = m.group(1)
    cred = mcp_gateway.credential(session_id, name)
    if cred is None:
        if _takes_gateway_credential(name):
            return mcp_gateway.Refusal("no_credential", mcp_gateway._REFUSAL_NO_CREDENTIAL)
        return None
    if not cred.forwarded_here:
        return mcp_gateway.Refusal(
            "vendor_not_tunneled",
            "A machine session reaches a vendor MCP through the machine's own "
            "gateway; the platform forwards none of that traffic.",
        )
    return await mcp_gateway.prepare_forward(
        session_id, name, method, m.group(2) or "/", query.encode("latin-1"), headers.items(), body,
    )


def _gateway_only(path: str) -> bool:
    """A tunneled ``/mcp/<name>/`` path the gateway's forward is the only
    way to: a Docker tool (its door) or an MCP the gateway adds a credential
    to (a vendor MCP an agent's GitHub reach narrows)."""
    m = _MCP_PATH_RE.match(path.split("?", 1)[0])
    return m is not None and _takes_gateway_credential(m.group(1))


class _AmbiguousAuthorization(Exception):
    """A tunneled request carries more than one Authorization header."""


def _bearer_token(headers: dict) -> str:
    """The request's bearer, "" when it carries none. The scheme is read in
    any case, as the routes read it. More than one Authorization header
    (keys differing in case) raises ``_AmbiguousAuthorization``: which one a
    server reads is its own choice, so none is judged."""
    keys = [k for k in headers if isinstance(k, str) and k.lower() == "authorization"]
    if len(keys) > 1:
        raise _AmbiguousAuthorization()
    value = headers.get(keys[0]) if keys else None
    if not isinstance(value, str):
        return ""
    scheme, _, token = value.strip().partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _session_bearer_payload(headers: dict) -> dict | None:
    """The payload of a request's session-JWT bearer, or None when the
    request carries no bearer or one that is not a session token."""
    token = _bearer_token(headers)
    if not token:
        return None
    from auth.session_token import validate_session_token
    return validate_session_token(token)


def _bearer_session(headers: dict) -> tuple[str, dict | None]:
    """``(session id, session payload)`` of a bearer that names a session:
    a session token (its payload second), the command-line token or one of
    the capability tokens (None second). ``("", None)`` for no bearer or
    one that names no session (an app step's claim, a key)."""
    token = _bearer_token(headers)
    if not token:
        return "", None
    from auth import tokens
    from auth.session_token import validate_session_token
    payload = validate_session_token(token)
    if payload is not None:
        return str(payload.get("sid") or ""), payload
    for kind in (tokens.CLI, tokens.MCP_CREDENTIALS, tokens.MCP_TOKEN):
        other = tokens.verify(token, kind)
        if other is not None:
            return str(other.get("sid") or ""), None
    return "", None


# session id → (machine, expires): where a session was last found, so the
# calls of its closing moments (its tables are gone by then) still resolve to
# its own machine and to no other.
_RECENT_MACHINE_S = 120.0
_RECENT_MACHINE_MAX = 4096
_recent_machine: dict[str, tuple[str, float]] = {}

HERE, ELSEWHERE, STARTING, UNKNOWN = "here", "elsewhere", "starting", "unknown"


def _machine_of(manager, session_id: str) -> str:
    """The machine that holds ``session_id``, "" when no table knows it.
    Read first from the tables that are set before a start frame leaves:
    a session's queue on a machine's connection or in its reconnect window,
    the terminal table. Then the remote layer's record, then the machine
    the session's registered context names (a task's, a terminal's)."""
    from core.remote import remote_pty
    for mid in manager.get_connected_machines():
        conn = manager.get_connection(mid)
        if conn is not None and session_id in conn.session_queues:
            return str(mid)
    for mid, held in list(getattr(manager, "_grace_sessions", {}).items()):
        if session_id in held:
            return str(mid)
    for mid, sid in list(remote_pty._remote_ptys):
        if sid == session_id:
            return str(mid)
    from core.remote.satellite_file_transfer import _machine_of_session
    found = _machine_of_session(session_id)
    if found:
        return found
    from core.session.session_state import get_session_security
    sec = get_session_security(session_id)
    placed = getattr(getattr(sec, "placement", None), "machine_id", "") if sec is not None else ""
    return str(placed or "")


def session_place(manager, machine_id: str, session_id: str) -> str:
    """Where ``session_id`` is, seen from ``machine_id``'s tunnel: ``HERE``,
    ``ELSEWHERE`` (another machine, or the platform itself), ``STARTING``
    (no table knows it yet while it is marked starting: the caller may ask
    again) or ``UNKNOWN``."""
    now = time.monotonic()
    found = _machine_of(manager, session_id)
    if found:
        if len(_recent_machine) >= _RECENT_MACHINE_MAX:
            for sid in [s for s, (_m, exp) in _recent_machine.items() if exp <= now]:
                del _recent_machine[sid]
            if len(_recent_machine) >= _RECENT_MACHINE_MAX:
                _recent_machine.clear()
        _recent_machine[session_id] = (found, now + _RECENT_MACHINE_S)
        return HERE if found == machine_id else ELSEWHERE
    from core.session.session_state import session_is_held, session_is_live
    if session_is_held(session_id):
        return ELSEWHERE        # the platform's own sandbox holds it
    recent = _recent_machine.get(session_id)
    if recent is not None and recent[1] > now:
        return HERE if recent[0] == machine_id else ELSEWHERE
    return STARTING if session_is_live(session_id) else UNKNOWN


async def _holder_current(payload: dict) -> bool:
    from auth import token_holder
    return await token_holder.holder_ok(payload)


# How often a producer waiting for room looks at its stream again.
_ROOM_POLL_S = 5.0


async def _asker_settled(machine_id: str, agent_slug: str, since: int,
                         stream: "_HttpStream") -> None:
    """Wait until ``machine_id`` holds the files of ``agent_slug`` that the
    platform changed since ``since`` (its lane's count when the request
    began). Bounded by the stream's own timeout, ended by its cancel or by
    the machine's drop. Never raises: the answer goes out either way."""
    from core.remote import machine_push_lane
    if machine_push_lane.seq(machine_id) == since:
        return
    try:
        await asyncio.wait_for(
            machine_push_lane.settled(machine_id, agent_slug, since=since,
                                      idle_s=float(stream.timeout_s),
                                      cancel=stream.cancel_event),
            timeout=float(stream.timeout_s))
    except asyncio.TimeoutError:
        logger.info("tunnel: machine %s still lacks files of %s after %d s; answering",
                    machine_id[:8], agent_slug, stream.timeout_s)
    stream.last_activity = time.monotonic()


async def _room_for(manager, machine_id: str, conn, stream: "_HttpStream") -> bool:
    """Wait until ``conn`` holds less than its room bound. False when the
    stream was cancelled or the connection is no longer the machine's (the
    chunk is dropped: its reader is gone). A waiter counts again after it
    wakes, so many waiting streams never queue together, and the time it
    waits counts as the stream's activity (a slow download is no leak)."""
    room = getattr(conn, "room", None)
    while room is not None and not room.is_set():
        if stream.cancel_event.is_set() or manager.get_connection(machine_id) is not conn:
            return False
        stream.last_activity = time.monotonic()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(room.wait(), _ROOM_POLL_S)
    return not stream.cancel_event.is_set() and manager.get_connection(machine_id) is conn


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
    """The MCP class dials Docker tools by service name: on a hosted
    install only inside the install's own tool networks
    (``pinned_http.docker_tool``). The hook class dials this proxy's own
    listener."""
    size = _MCP_POOL_MAX if is_mcp else _HOOK_POOL_MAX
    limits = httpx.Limits(max_connections=size, max_keepalive_connections=size)
    if is_mcp:
        from services.infra import pinned_http
        return httpx.AsyncClient(**pinned_http.tool_transport(timeout=None, limits=limits))
    return httpx.AsyncClient(timeout=None, limits=limits)


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
        bare = str(path).split("?", 1)[0]
        gateway = _GATEWAY_PATH_RE.match(bare) is not None
        via_app = msg.get("via") == "app"
        if via_app:
            # App servers run only on a compute machine beside the platform
            # (an app's data folder is the platform's alone).
            denied = not manager.shares_folders(machine_id) or not _is_app_path(path)
        else:
            denied = not _is_allowed_path(path) or (gateway and not manager.is_compute(machine_id))
        if denied:
            logger.warning(
                # strip the query string — it can carry session ids / tokens
                # (same rule as the upstream-error log below)
                "tunnel: rejected non-allowlisted path: %s (machine %s)",
                bare[:200], machine_id[:8],
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
        is_mcp = gateway or _MCP_PATH_RE.match(bare) is not None
        stream = _HttpStream(
            stream_id=stream_id,
            machine_id=machine_id,
            timeout_s=max(1, min(timeout_s, _stream_timeout_ceiling(path))),
            kind=(f"app:{_APP_ID_RE.match(bare).group(1)}" if via_app  # type: ignore[union-attr]  # _is_app_path matched it above
                  else "mcp" if is_mcp else "hook"),
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
        if stream.kind.startswith("app:"):
            if open_here >= _MAX_APP_STREAMS_PER_APP:
                return f"app has {open_here} open streams"
            return None
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
            if url is None and not (stream.kind == "mcp" and _gateway_only(path)):
                # MCP not installed on the platform (manifest missing).
                await self._send_response(
                    manager, machine_id, stream_id,
                    status=404, headers={}, body=b"",
                    error="mcp-not-found", body_eof=True,
                )
                return

            # A bearer that names a session (a session's token, its
            # command-line token, a capability token) works only through the
            # tunnel of the machine that holds that session: one printed in
            # another session, on another machine or on the platform itself,
            # is refused here and the upstream is never contacted. A session
            # no table knows yet while it starts is told to ask again.
            try:
                bearer_sid, payload = _bearer_session(headers)
            except _AmbiguousAuthorization:
                await self._send_response(
                    manager, machine_id, stream_id,
                    status=400, headers={}, body=b"",
                    error="ambiguous-authorization", body_eof=True,
                )
                return
            if bearer_sid and stream.kind.startswith("app:"):
                # An app server holds no session: a token that names one
                # did not come from it.
                await self._send_response(
                    manager, machine_id, stream_id,
                    status=401, headers={}, body=b"",
                    error="session-token-on-app-listener", body_eof=True,
                )
                return
            if bearer_sid:
                place = session_place(manager, machine_id, bearer_sid)
                if place != HERE:
                    logger.warning(
                        "tunnel: a token of session %s (%s) refused for %s through machine %s",
                        bearer_sid[:8], place, path.split("?", 1)[0], machine_id[:8],
                    )
                    await self._send_response(
                        manager, machine_id, stream_id,
                        status=503 if place == STARTING else 401, headers={}, body=b"",
                        error=("session-starting" if place == STARTING
                               else "session-not-on-machine"),
                        body_eof=True,
                    )
                    return
            # The asking machine's wait (below): the lane's count before the
            # route runs.
            asker_agent = ""
            if (stream.kind == "hook" and payload is not None
                    and path.split("?", 1)[0] not in _UNCLAMPED_HOOK_PATHS | _NO_SETTLE_PATHS):
                asker_agent = str(payload.get("agent") or "")
            from core.remote import machine_push_lane
            asked_from = machine_push_lane.seq(machine_id)

            # A tunneled MCP request is also judged on its session's liveness
            # and its token's holder: a closed session or a person who is
            # gone gets a 401.
            if stream.kind == "mcp":
                from core.session.session_state import session_token_refusal
                if payload is not None and session_token_refusal(payload):
                    logger.info(
                        "tunnel: session token refused for %s (machine %s, session %s)",
                        path.split("?", 1)[0], machine_id[:8],
                        str(payload.get("sid") or "")[:8],
                    )
                    await self._send_response(
                        manager, machine_id, stream_id,
                        status=401, headers={}, body=b"",
                        error="session-not-live", body_eof=True,
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
            # stream=True so SSE/large responses don't buffer in memory. A
            # sidecar the session holds a gateway credential for goes through
            # the gateway's own forward instead: the credential is added
            # here, the session token stays with the tunnel, and a refusal
            # answers in the shape the MCP client reads.
            is_mcp = stream.kind == "mcp"
            client = self._get_client(is_mcp)
            gateway_sid = str(payload.get("sid") or "") if (is_mcp and payload) else ""
            if is_mcp and not gateway_sid and _gateway_only(path):
                # A Docker tool, or a tool the gateway adds a credential to:
                # reached by a session's own token alone, never bare.
                await self._send_response(
                    manager, machine_id, stream_id,
                    status=401, headers={}, body=b"",
                    error="session-required", body_eof=True,
                )
                return
            answered = None
            if gateway_sid:
                from core.credentials import mcp_gateway
                prepared = await _gateway_forward(gateway_sid, path, method, headers, body_bytes)
                if isinstance(prepared, mcp_gateway.Refusal):
                    logger.info(
                        "tunnel: gateway refused %s for session %s", prepared.reason,
                        gateway_sid[:8],
                    )
                    r_status, r_headers, r_body = mcp_gateway.refusal_shape(
                        method, body_bytes, prepared.detail,
                    )
                    await self._send_response(
                        manager, machine_id, stream_id,
                        status=r_status, headers=r_headers, body=r_body, body_eof=True,
                    )
                    return
                if isinstance(prepared, mcp_gateway.Answered):
                    # A Docker tool's door answered (``services/containers/door.py``).
                    answered = prepared
                elif prepared is not None:
                    url, headers, client = str(prepared.url), prepared.headers, prepared.client
                    if prepared.content is not None:
                        body_bytes = prepared.content
            if url is None:
                # A gateway-only MCP with no session to forward for.
                await self._send_response(
                    manager, machine_id, stream_id,
                    status=404, headers={}, body=b"",
                    error="mcp-not-found", body_eof=True,
                )
                return
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
                resp = answered if answered is not None else await client.send(req, stream=True)
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
                            stream=stream,
                        )
                if asker_agent and not stream.cancel_event.is_set():
                    # The route has answered in full: what it wrote for this
                    # agent while it ran reaches the asking session's own
                    # machine before the answer's last frame does, so the
                    # session's next step finds the files there.
                    await _asker_settled(machine_id, asker_agent, asked_from, stream)
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
        stream: "_HttpStream | None" = None,
    ) -> None:
        """Send an ``http_response_chunk`` frame back to the satellite. A
        body chunk of ``stream`` first waits while the machine's connection
        holds ``ROOM_BYTES`` for its writer, so the upstream is read only as
        fast as the machine takes it. The last frame never waits."""
        conn = manager.get_connection(machine_id)
        if conn is None:
            return
        if stream is not None and not body_eof and not await _room_for(manager, machine_id, conn, stream):
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
            # A prompt stream's ceiling outlives the absolute cap (its own
            # timeout); every other stream's ceiling is below it.
            max_age = max(_STREAM_MAX_AGE_S, stream.timeout_s + _STREAM_GRACE_S)
            if age > max_age or idle > stream.timeout_s + _STREAM_GRACE_S:
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

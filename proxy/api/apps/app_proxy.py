"""The routes of a folder app (APPS.md "Proxied routes", "Client files",
"Viewer identity", "Agents call apps").

``/v1/apps/{id}/api/*`` and ``/v1/apps/{id}/ws/*`` reach the app's own
server through the supervisor's splice; their caller is resolved from the
``Authorization`` header (or the first socket frame) ALONE — never the
dashboard cookie, which the sandboxed frame does not send anyway: an
Ed25519 viewer claim the host minted, the app's own launch token, or an
agent session's token (basis ``agent``). The client files are served by
content address (``/client/{tree hash}/…``): the document behind the
cookie, every other file on the hash alone. The viewer-token route mints
the claim for a person at the keyboard; ``/logs`` reads the server's log;
the push and state routes are the REST twins of the session hooks.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import http.cookiejar
import json
import logging
import mimetypes
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.websockets import WebSocketDisconnect, WebSocketState

import config
from api.media.ui import (
    _placeholder, _ui_response, inject_runtime, is_full_document, request_origin, wrap_fragment,
)
from auth.lan_check import get_client_ip
from auth.providers import UserContext, get_current_user, require_auth, require_human
from auth.request_path import has_traversal
from auth.session_token import validate_session_token
from services.apps import app_supervisor, app_tokens, releases
from storage import database as task_store
from storage.pg import run_db
from storage import db_apps
from auth import roles
from ws import wire_events as wire

logger = logging.getLogger("claude-proxy.apps")
router = APIRouter()

MAX_BODY_BYTES = 1024 * 1024
UPSTREAM_TIMEOUT_S = 30.0
RATE_PER_ACTOR = 20.0
RATE_PER_APP = 200.0
WS_MAX_FRAME = 1024 * 1024
WS_PER_VIEWER = 2
# A link's anonymous viewers share one actor per client IP (an office
# behind one address, until they sign in): a few more slots than a person.
WS_PER_IP = 8
WS_PER_APP = 64
WS_AUTH_TIMEOUT_S = 5.0
VIEWER_TOKEN_INTERVAL_S = 2.0   # thirty a minute per (app, user): a deploy or an approval reloads the frame, and each load mints
# The platform's own render holds its claim for the length of one render.
RENDER_VIEWER_TTL_S = 120
LOG_TAIL_MAX = 2000

_APP_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
_CORS = {
    "Access-Control-Allow-Origin": "*",
    # `x-otodock-challenge` rides a link page's call to a challenged path
    # (APPS.md "External links"); the proxy consumes it and never forwards it.
    "Access-Control-Allow-Headers": "authorization, content-type, x-otodock-challenge",
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Expose-Headers": "content-type, retry-after, x-otodock-server",
    "Access-Control-Max-Age": "600",
}
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}
_DROP_REQUEST = _HOP_BY_HOP | {"cookie", "authorization", "host", "origin", "referer",
                               "content-length"}
_DROP_REQUEST_PREFIXES = ("x-forwarded-", "x-otodock-", "sec-", "cf-")
_DROP_RESPONSE = _HOP_BY_HOP | {
    "set-cookie", "content-security-policy", "content-security-policy-report-only",
    "strict-transport-security", "x-frame-options", "link", "www-authenticate",
    "clear-site-data", "content-length",
}
_ASSET_TYPES = {
    ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
    ".json": "application/json", ".map": "application/json", ".txt": "text/plain",
    ".md": "text/plain", ".csv": "text/csv", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
    ".ico": "image/x-icon", ".svg": "image/svg+xml", ".woff": "font/woff",
    ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
    ".wasm": "application/wasm", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
    ".wav": "audio/wav", ".mp4": "video/mp4", ".webm": "video/webm",
}
_NEVER_ASSETS = (".html", ".htm", ".xhtml", ".xml")


# ── the caller ──────────────────────────────────────────────────────────────


@dataclass
class Caller:
    basis: str                  # viewer | external | agent | app
    sub: str = ""
    username: str = ""
    role: str = "viewer"
    grant: str = ""
    claim: str = ""             # what X-OtoDock-Viewer carries
    exp: int = 0
    user: UserContext | None = None
    extra: dict = field(default_factory=dict)
    # Which process answers: the live release, the owner's preview copy, or
    # the check instance a render runs against (APPS.md "Deploy pipeline").
    instance: str = app_supervisor.LIVE

    @property
    def wire_basis(self) -> str:
        """The ``X-OtoDock-Basis`` an app reads: a placed agent's session
        says so (its claim's principal says the same), every other caller
        its basis."""
        return app_tokens.PRINCIPAL_PLACEMENT if self.extra.get("placement") else self.basis

    @property
    def actor(self) -> str:
        if self.extra.get("render"):
            # The platform's render: its own pace and socket slots, so a
            # human with the app open loses nothing to it.
            return f"render:{self.extra['render']}"
        if self.basis == "app":
            # The preview copy runs code nobody approved: its own buckets,
            # never the live server's.
            if self.instance == app_supervisor.LIVE:
                return f"app:{self.sub}"
            return f"app:{self.sub}:{self.instance}"
        if self.basis == "step":
            return f"step:{str(self.extra.get('delivery') or '')[:8]}"
        if self.basis == "external":
            # A link's viewers are actors by their app session (APPS.md
            # "External links"), else by their client IP (the link routes
            # hand it in) — never one actor per link.
            session = str(self.extra.get("session") or "")
            if session:
                return f"share:{self.grant}:s:{hashlib.sha256(session.encode('utf-8')).hexdigest()[:16]}"
            return f"share:{self.grant}:ip:{self.extra.get('ip') or '-'}"
        if self.basis == "agent":
            return f"agent:{self.sub}"
        return f"user:{self.sub}"


def _bearer(headers) -> str:
    auth = headers.get("authorization", "") or ""
    parts = auth.split(" ", 1)
    return parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else ""


def _refuse(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail, headers=dict(_CORS))


def _refuse_with_retry(status: int, detail: str, retry_after: int) -> HTTPException:
    """A refusal the caller may retry after ``retry_after`` seconds (a 429
    from a bucket): the header lets a page wait instead of failing."""
    return HTTPException(status_code=status, detail=detail,
                         headers={**_CORS, "Retry-After": str(max(1, retry_after))})


async def caller_from_token(token: str, row: dict) -> Caller:
    """The caller a bearer token names for ``row``; 401 for a token that
    names nobody (a cookie-only request has no token and lands here too),
    404 for an agent session the row is not reachable from."""
    if not token:
        raise _refuse(401, "an app token is required")
    app_id = row["id"]
    claims = app_tokens.verify(token, app_id, app_tokens.PURPOSE_VIEWER)
    if claims:
        basis = "external" if claims.get("external") else "viewer"
        instance = str(claims.get("instance") or app_supervisor.LIVE)
        return Caller(basis=basis, sub=str(claims.get("sub") or ""),
                      username=str(claims.get("username") or ""),
                      role=str(claims.get("role") or roles.VIEWER),
                      grant=str(claims.get("grant") or ""), claim=token,
                      exp=int(claims.get("exp") or 0), extra=claims,
                      instance=instance if instance in app_supervisor.INSTANCES else app_supervisor.LIVE)
    launch = app_tokens.verify(token, app_id, app_tokens.PURPOSE_LAUNCH)
    if launch:
        # Never a check instance's token: a check runs unapproved code
        # without the approval gate, so it is never the app identity.
        for name in (app_supervisor.LIVE, app_supervisor.PREVIEW):
            inst = app_supervisor.get(app_id, name)
            if inst is not None and inst.token == token:
                return Caller(basis="app", sub=app_id, role="app", claim="",
                              exp=int(launch.get("exp") or 0), extra=launch, instance=name)
        raise _refuse(401, "the launch token is not the running instance's")
    payload = validate_session_token(token)
    if not payload:
        raise _refuse(401, "an app token is required")
    return await _agent_caller(payload, row)


async def _agent_caller(payload: dict, row: dict) -> Caller:
    """Basis ``agent``: the session agent's own shared apps and the session
    user's personal apps on that same agent (a personal app lives in its
    agent's tree, and a session never crosses one), and, through a share
    that placed the app in the session's agent, the app's signed exports
    at the capped role (SHARING.md "Agents use a placed app"); never an
    external session."""
    if payload.get("ext"):
        raise _refuse(401, "external sessions cannot call apps")
    # The session must be live and the token of its current life (the app
    # routes and the app socket's auth frames reach no middleware for it).
    from core.session.session_state import get_session_security, session_token_refusal
    from core.session.visibility import SCOPE_USER
    if session_token_refusal(payload):
        raise _refuse(401, "the session token is no longer valid")
    agent = payload.get("agent") or ""
    sid = payload.get("sid") or ""
    # A person's own placement answers only a session that mounts their
    # scope: a Shared-only chat carries the person but writes the agent's
    # one shared history, so it sees none; an unregistered context neither.
    ctx = get_session_security(sid)
    person_scope = ctx is not None and ctx.session_scope == SCOPE_USER
    holder_ok, user, username, placement = await run_db(_agent_principal, payload, row, person_scope)
    # A token whose person is gone, or was minted before their last password
    # change, reaches no app, never the no-user principal.
    if not holder_ok:
        raise _refuse(401, "the session token is no longer valid")
    allowed = bool(agent) and row.get("agent") == agent
    if allowed and row.get("username"):
        allowed = user is not None and (row.get("owner_sub") or "") == user.sub
    if allowed and (row.get("scope_chat_id") or row.get("scope_project_id")):
        from api.apps.apps import _scope_access
        allowed = user is not None and await run_db(_scope_access, row, user)
    if not allowed and placement is None:
        raise _refuse(404, "App not found")
    sub = user.sub if user else f"session:{sid}"
    if allowed:
        # The row alone, never the owner's platform role: a bearer principal
        # presents its per-agent row to an app (viewer without one) so a
        # prompt never carries an admin's standing into a floored button.
        role = (roles.row_role(user.agent_roles, row.get("agent") or "") or roles.VIEWER) if user else roles.SERVICE
        claim = app_tokens.mint(row["id"], app_tokens.PURPOSE_CALLER, {
            "principal": app_tokens.PRINCIPAL_AGENT, "sub": sub, "username": username, "role": role,
            "agent": agent, "session": sid, "external": False,
        }, app_tokens.CALLER_TTL_S)
        return Caller(basis="agent", sub=sub, username=username, role=role, claim=claim,
                      exp=int(time.time()) + app_tokens.CALLER_TTL_S, user=user)
    # A placed app: the role the share gives on the receiving agent; the
    # marker is what every guard tests, and the claim names the placement.
    marker = {"kind": placement["source"], "share_id": placement["share_id"],
              "from_agent": row.get("agent") or "", "agent": agent, "role_cap": placement["role_cap"]}
    role = placement["role"]
    claim = _placement_claim(row, sub, username, role, agent, sid, marker)
    return Caller(basis="agent", sub=sub, username=username, role=role, claim=claim,
                  exp=int(time.time()) + app_tokens.CALLER_TTL_S, user=user,
                  extra={"placement": marker})


def _placement_claim(row: dict, sub: str, username: str, role: str, agent: str, sid: str,
                     marker: dict) -> str:
    """The claim a placed agent's session presents to the app: the
    ``placement`` principal (never ``agent``, so an app that takes that
    word for its own agent never takes a placed session for it), the role
    the share gives, the CALLING session's agent, and the placement itself
    (``from_agent`` names the app's own agent). Basis ``placement`` on the
    wire; the platform route and the broker refuse it when relayed."""
    return app_tokens.mint(row["id"], app_tokens.PURPOSE_CALLER, {
        "principal": app_tokens.PRINCIPAL_PLACEMENT, "sub": sub, "username": username, "role": role,
        "agent": agent, "session": sid, "external": False, "placement": marker,
    }, app_tokens.CALLER_TTL_S)


def _agent_principal(payload: dict, row: dict, person_scope: bool
                     ) -> tuple[bool, UserContext | None, str, dict | None]:
    """``(holder ok, the user, their username, the placement)`` of a session
    token in one executor job. The placement is read only for another
    agent's unscoped row: the share placing it in the token's agent and the
    role the session acts at (``share_store.placement_role``), the person's
    own placement only when the session mounts their scope. Synchronous."""
    from auth.providers import session_token_holder_ok, user_context_for_sub
    if not session_token_holder_ok(payload):
        return False, None, "", None
    user_sub = payload.get("user_sub") or ""
    user = user_context_for_sub(user_sub) if user_sub else None
    username = ((task_store.get_user(user.sub) or {}).get("username") or "") if user else ""
    placement = None
    agent = payload.get("agent") or ""
    if agent and row.get("agent") != agent and not (row.get("scope_chat_id") or row.get("scope_project_id")):
        from storage.sharing import share_store
        own = (roles.row_role(user.agent_roles, agent) or roles.VIEWER) if user else roles.SERVICE
        person_sub = user.sub if (user is not None and person_scope) else None
        placement = share_store.placement_role(row["id"], agent, person_sub, own)
    return True, user, username, placement


async def resolve_caller(request: Request, row: dict) -> Caller:
    return await caller_from_token(_bearer(request.headers), row)


# ── limits and paths ────────────────────────────────────────────────────────

_buckets: dict[str, tuple[float, float]] = {}


def _take(key: str, rate: float, burst: float) -> bool:
    now = time.monotonic()
    tokens, at = _buckets.get(key, (burst, now))
    tokens = min(burst, tokens + (now - at) * rate)
    if tokens < 1.0:
        _buckets[key] = (tokens, now)
        return False
    _buckets[key] = (tokens - 1.0, now)
    if len(_buckets) > 4096:
        stale = [k for k, (_t, a) in _buckets.items() if now - a > 120]
        for k in stale:
            _buckets.pop(k, None)
    return True


def check_rate(app_id: str, actor: str) -> None:
    if not _take(f"{app_id}|{actor}", RATE_PER_ACTOR, RATE_PER_ACTOR) \
            or not _take(f"{app_id}|*", RATE_PER_APP, RATE_PER_APP):
        raise _refuse_with_retry(429, "too many requests", 1)


def check_app_path(path: str, raw: str = "") -> None:
    """Refuse dot segments and encoded separators up front — on the raw
    path as the client sent it, so the checked path is the forwarded path —
    and any first segment starting with ``_`` (the app's own endpoints,
    reachable from the proxy alone)."""
    if has_traversal(path) or (raw and has_traversal(raw)):
        raise _refuse(404, "Not found")
    # `api//_handler/x` arrives as `/_handler/x`: the first segment is the
    # first non-empty one, or a doubled slash walks past the refusal.
    first = path.lstrip("/").split("/", 1)[0]
    if first.startswith("_"):
        raise _refuse(404, "Not found")


def _row_or_404(app_id: str) -> dict:
    if not _APP_ID_RE.match(app_id):
        raise _refuse(404, "App not found")
    row = task_store.get_app(app_id)
    if not row or row.get("hidden") or not db_apps.app_kind_of(row).may_serve \
            or db_apps.personal_row_dormant(row):
        raise _refuse(404, "App not found")
    return row


def _forward_headers(request: Request, caller: Caller) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in _DROP_REQUEST or lk.startswith(_DROP_REQUEST_PREFIXES):
            continue
        out.append((k, v))
    if caller.claim:
        out.append(("X-OtoDock-Viewer", caller.claim))
    out.append(("X-OtoDock-Basis", caller.wire_basis))
    origin = urlsplit(config.DASHBOARD_PUBLIC_URL or "") if config.DASHBOARD_PUBLIC_URL else None
    if origin and origin.scheme and origin.netloc:
        out.append(("X-Forwarded-Proto", origin.scheme))
        out.append(("X-Forwarded-Host", origin.netloc))
    else:
        o = urlsplit(request_origin(request))
        out.append(("X-Forwarded-Proto", o.scheme))
        out.append(("X-Forwarded-Host", o.netloc))
    out.append(("X-Forwarded-For", get_client_ip(request)))
    return out


def _redirect_ok(location: str, app_id: str) -> bool:
    if not location:
        return True
    if location.startswith(f"/v1/apps/{app_id}/api/"):
        return True
    return bool(re.match(r"^[A-Za-z0-9_][^:/]*$", location)) and ".." not in location


def _unavailable(e: app_supervisor.AppUnavailable) -> JSONResponse:
    headers = {**_CORS, "Retry-After": str(max(1, e.retry_after)),
               "X-OtoDock-Server": e.state}
    return JSONResponse({"detail": e.reason, "state": e.state,
                         "retry_after": max(1, e.retry_after)}, status_code=503, headers=headers)


_client: httpx.AsyncClient | None = None


class _NoCookieJar(http.cookiejar.CookieJar):
    """A jar that keeps nothing: every app server lives on the same
    loopback host, so a jar would replay one app's ``Set-Cookie`` to every
    other app."""

    def extract_cookies(self, response, request) -> None:  # noqa: ARG002
        return None

    def add_cookie_header(self, request) -> None:  # noqa: ARG002
        return None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_S, follow_redirects=False)
        _client.cookies.jar = _NoCookieJar()
    return _client


async def _read_body(request: Request) -> bytes:
    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise _refuse(413, "request body larger than 1 MB")
        chunks.append(chunk)
    return b"".join(chunks)


def _rest_path(request: Request, app_id: str, path: str, prefix: str = "") -> str:
    """The raw path after ``/api`` as the browser sent it (no re-encoding,
    no dot-segment collapse), so the app receives exactly what was checked."""
    raw = request.scope.get("raw_path") or b""
    raw_s = raw.decode("latin-1") if isinstance(raw, bytes) else str(raw)
    prefix = prefix or f"/v1/apps/{app_id}/api"
    if raw_s.startswith(prefix):
        rest = raw_s[len(prefix):]
        return rest if rest.startswith("/") else "/" + rest
    return "/" + path


async def forward(inst: app_supervisor.Instance, method: str, path: str,
                  headers: list[tuple[str, str]], body: bytes, *,
                  timeout: float = UPSTREAM_TIMEOUT_S) -> httpx.Response:
    """One request to a running instance over the shared client (no cookie
    jar, no redirects), streamed — the caller reads and closes it. Shared by
    the proxied routes, the handler fire (``services/apps/app_handlers.py``)
    and the bindings broker."""
    client = _http()
    req = client.build_request(method, f"{inst.base_url}{path}", headers=headers,
                               content=body, timeout=timeout)
    return await client.send(req, stream=True)


async def instance_for(row: dict, caller: Caller) -> app_supervisor.Instance:
    """The process the caller's claim names (APPS.md "Deploy pipeline"):
    the live release wakes on demand, the preview copy too (an approver's
    claim), the check instance never — it exists only while a render job
    runs it, and a claim that outlives it answers 503."""
    if caller.instance == app_supervisor.CHECK:
        inst = app_supervisor.get(row["id"], app_supervisor.CHECK)
        if inst is None or inst.state not in app_supervisor.SERVING:
            raise app_supervisor.AppUnavailable("the check instance is not running", 5,
                                                state=app_supervisor.STOPPED)
        inst.touch()
        return inst
    return await app_supervisor.ensure_up(row, caller.instance)


async def proxy_request(row: dict, caller: Caller, request: Request, rest: str):
    """Forward one checked request to the app's server (shared by the
    dashboard route and the external link's twin)."""
    app_id = row["id"]
    try:
        inst = await instance_for(row, caller)
    except app_supervisor.AppUnavailable as e:
        return _unavailable(e)
    if inst.state == app_supervisor.STATIC:
        raise _refuse(404, "this app has no server")
    body = await _read_body(request)
    headers = _forward_headers(request, caller)
    query = request.scope.get("query_string") or b""
    path = rest
    if query:
        path += "?" + (query.decode("latin-1") if isinstance(query, bytes) else str(query))
    try:
        upstream = await forward(inst, request.method, path, headers, body)
    except httpx.HTTPError as e:
        logger.warning("App %s: upstream error on %s: %s", row.get("slug"), rest, e)
        raise _refuse(502, "the app's server did not answer")
    app_supervisor.touch(app_id)
    if 300 <= upstream.status_code < 400 and not _redirect_ok(
            upstream.headers.get("location", ""), app_id):
        await upstream.aclose()
        raise _refuse(502, "the app answered with a redirect outside its own routes")
    out_headers: list[tuple[str, str]] = [
        (k, v) for k, v in upstream.headers.multi_items()
        if k.lower() not in _DROP_RESPONSE and not k.lower().startswith("access-control-")
    ]
    out_headers.extend(_CORS.items())
    if request.method == "HEAD":
        await upstream.aclose()
        return Response(status_code=upstream.status_code, headers=dict(out_headers))
    return StreamingResponse(
        upstream.aiter_raw(), status_code=upstream.status_code,
        headers=dict(out_headers), background=BackgroundTask(upstream.aclose),
    )


# ── the proxied API ─────────────────────────────────────────────────────────


@router.options("/v1/apps/{app_id}/api/{path:path}")
async def preflight_api(app_id: str, path: str) -> Response:
    return Response(status_code=204, headers=dict(_CORS))


@router.api_route("/v1/apps/{app_id}/api/{path:path}",
                  methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
async def proxy_api(app_id: str, path: str, request: Request):
    row = await run_db(_row_or_404, app_id)
    caller = await resolve_caller(request, row)
    if caller.basis == "external":
        # A link's claim works under the link's own prefix, where the share
        # is re-read on every request (SHARING.md); lifted here it would
        # outlive a revoke.
        raise _refuse(401, "a link's claim is for the link's own routes")
    rest = _rest_path(request, app_id, path)
    check_app_path(path, rest)
    if caller.extra.get("placement"):
        _check_placed_export(row, path, caller)
        # The exported method's own route, where the broker forwards a
        # binding's call too (``app_bindings.broker``): one route answers
        # both, so an app that offers a method serves it once.
        rest = "/api" + rest
    check_rate(app_id, caller.actor)
    return await proxy_request(row, caller, request, rest)


def _check_placed_export(row: dict, path: str, caller: Caller) -> None:
    """A placed agent's session reaches the app's signed ``exports.methods``
    alone (SHARING.md "Agents use a placed app"), as a brokered call does:
    the manifest approved, the first path segment an exported method, its
    floor met at the role the share gives."""
    from api.apps import manifest as _mf
    if not task_store.app_actions_approved(row):
        raise _refuse(404, "not available")
    entry = _mf.exported_method(row, path)
    if entry is None:
        raise _refuse(404, "not exported by that app")
    if not _mf.meets_floor(entry, caller.role):
        raise _refuse(403, f"this call needs the {entry.get('min_role')} role on "
                           f"{caller.extra['placement']['agent']} as the share gives it")


# ── the WebSocket bridge ────────────────────────────────────────────────────

_ws_counts: dict[str, int] = {}
# Sockets the cap refused, per app, since this process started. An agent
# writing an app has no browser console, and a page that reconnects a
# socket the runtime already reconnects burns through the cap in seconds:
# this is how ``app_logs`` tells the author, instead of leaving them a
# page that says "reconnecting" for ever (found live, 2026-09-13).
_ws_refused: dict[str, int] = {}


def refused_sockets(app_id: str) -> int:
    return _ws_refused.get(app_id, 0)


def _ws_refuse(app_id: str) -> bool:
    _ws_refused[app_id] = _ws_refused.get(app_id, 0) + 1
    return False


def _ws_take(app_id: str, actor: str) -> bool:
    per_actor = WS_PER_IP if ":ip:" in actor else WS_PER_VIEWER
    if _ws_counts.get(f"{app_id}|{actor}", 0) >= per_actor:
        return _ws_refuse(app_id)
    if _ws_counts.get(f"{app_id}|*", 0) >= WS_PER_APP:
        return _ws_refuse(app_id)
    _ws_counts[f"{app_id}|{actor}"] = _ws_counts.get(f"{app_id}|{actor}", 0) + 1
    _ws_counts[f"{app_id}|*"] = _ws_counts.get(f"{app_id}|*", 0) + 1
    return True


def _ws_release(app_id: str, actor: str) -> None:
    for key in (f"{app_id}|{actor}", f"{app_id}|*"):
        n = _ws_counts.get(key, 0) - 1
        if n <= 0:
            _ws_counts.pop(key, None)
        else:
            _ws_counts[key] = n


def _origin_ok(websocket: WebSocket) -> bool:
    origin = (websocket.headers.get("origin") or "").strip().lower()
    if not origin or origin == "null":
        return True
    return origin == request_origin(websocket).lower()


async def _close(ws: WebSocket, code: int, reason: str = "") -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=code, reason=reason[:120])


@router.websocket("/v1/apps/{app_id}/ws")
async def proxy_ws_root(websocket: WebSocket, app_id: str):
    await proxy_ws(websocket, app_id, "")


@router.websocket("/v1/apps/{app_id}/ws/{path:path}")
async def proxy_ws(websocket: WebSocket, app_id: str, path: str):
    """Accept, take the auth frame within five seconds, only then wake the
    app and open the upstream socket; forward frames both ways; rotate the
    token on later auth frames; close 4401 on an expired one, 1013 while
    the app is in backoff, 1012 when the supervisor stops it."""
    if not _origin_ok(websocket):
        await _close(websocket, 1008, "origin")
        return
    await websocket.accept()
    try:
        row = await run_db(_row_or_404, app_id)
    except HTTPException:
        await _close(websocket, 1008, "unknown app")
        return
    try:
        first = await asyncio.wait_for(websocket.receive_text(), timeout=WS_AUTH_TIMEOUT_S)
        msg = json.loads(first)
        if not isinstance(msg, dict) or msg.get("type") != "auth":
            raise ValueError("auth frame expected")
        caller = await caller_from_token(str(msg.get("token") or ""), row)
    except HTTPException as e:
        await _close(websocket, 4401 if e.status_code == 401 else 1008, str(e.detail))
        return
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
        await _close(websocket, 4400, "auth frame expected")
        return
    if caller.basis == "external":
        await _close(websocket, 4401, "a link's claim is for the link's own routes")
        return
    if caller.extra.get("placement"):
        # Exports are HTTP methods; a placed agent's session opens no socket.
        await _close(websocket, 1008, "a placed app answers its exported methods only")
        return
    try:
        raw = websocket.scope.get("raw_path") or b""
        check_app_path(path, raw.decode("latin-1") if isinstance(raw, bytes) else str(raw))
    except HTTPException:
        await _close(websocket, 1008, "path")
        return
    await bridge_ws(websocket, row, path, caller)


async def bridge_ws(websocket: WebSocket, row: dict, path: str, caller: Caller) -> None:
    """The bridge proper, after the socket was accepted and its caller
    judged (shared with the external link's twin)."""
    app_id = row["id"]
    if not _ws_take(app_id, caller.actor):
        await _close(websocket, 4429, "too many sockets")
        return
    opened = False
    try:
        try:
            inst = await instance_for(row, caller)
        except app_supervisor.AppUnavailable as e:
            await _close(websocket, 1013, e.reason)
            return
        if inst.state == app_supervisor.STATIC:
            await _close(websocket, 1008, "this app has no server")
            return
        query = websocket.scope.get("query_string") or b""
        url = f"ws://127.0.0.1:{inst.host_port}/{path}"
        if query:
            url += "?" + (query.decode("latin-1") if isinstance(query, bytes) else str(query))
        headers = [("X-OtoDock-Viewer", caller.claim)] if caller.claim else []
        headers.append(("X-OtoDock-Basis", caller.basis))
        headers.append(("X-Forwarded-For", get_client_ip(websocket)))
        try:
            upstream = await websockets.connect(url, additional_headers=headers,
                                                max_size=WS_MAX_FRAME, open_timeout=10)
        except Exception as e:
            logger.info("App %s: upstream socket refused: %s", row.get("slug"), e)
            await _close(websocket, 1011, "the app's server did not accept the socket")
            return
        app_supervisor.ws_opened(app_id)
        opened = True
        deadline = {"exp": caller.exp}

        async def client_to_upstream() -> None:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                text = msg.get("text")
                if text is not None:
                    if len(text) > WS_MAX_FRAME:
                        await _close(websocket, 1009, "frame too large")
                        return
                    if text.startswith('{"type":"auth"') or text.startswith('{"type": "auth"'):
                        try:
                            fresh = await caller_from_token(
                                str((json.loads(text) or {}).get("token") or ""), row)
                        except (HTTPException, ValueError):
                            await _close(websocket, 4401, "token refused")
                            return
                        # The upstream socket was opened for one identity: a
                        # token naming anyone else (another link, a person
                        # who lost access presenting a link's claim) must not
                        # keep it alive — close, and the page reconnects as
                        # whoever it now is.
                        if (fresh.basis, fresh.sub, fresh.grant, fresh.instance) != \
                                (caller.basis, caller.sub, caller.grant, caller.instance) \
                                or fresh.extra.get("placement"):
                            await _close(websocket, 4401, "the token names another viewer")
                            return
                        deadline["exp"] = fresh.exp
                        continue
                    await upstream.send(text)
                else:
                    data = msg.get("bytes") or b""
                    if len(data) > WS_MAX_FRAME:
                        await _close(websocket, 1009, "frame too large")
                        return
                    await upstream.send(data)
                app_supervisor.touch(app_id)

        async def upstream_to_client() -> None:
            async for frame in upstream:
                if isinstance(frame, str):
                    await websocket.send_text(frame)
                else:
                    await websocket.send_bytes(frame)

        async def watchdog() -> None:
            while True:
                exp = deadline["exp"]
                if not exp:
                    await asyncio.sleep(3600)
                    continue
                wait = exp - time.time()
                if wait > 0:
                    await asyncio.sleep(min(wait, 60))
                    continue
                await _close(websocket, 4401, "token expired")
                return

        async def instance_gone() -> None:
            # Polled, not awaited on the instance's event: a bridge must
            # never bind a supervisor object to its own loop's lifetime.
            # The claim names its process: a preview or check socket watches
            # that instance, not the live one.
            while app_supervisor.get(app_id, caller.instance) is inst and inst.state == app_supervisor.UP:
                await asyncio.sleep(1.0)
            await _close(websocket, 1012, "the app is restarting")

        tasks = [asyncio.create_task(c()) for c in
                 (client_to_upstream, upstream_to_client, watchdog, instance_gone)]
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = t.exception() if not t.cancelled() else None
                if exc is not None and not isinstance(exc, (WebSocketDisconnect,
                                                            websockets.ConnectionClosed)):
                    logger.warning("App %s: socket bridge ended with %r", row.get("slug"), exc)
        finally:
            for t in tasks:
                t.cancel()
            with contextlib.suppress(Exception):
                await upstream.close()
    finally:
        if opened:
            app_supervisor.ws_closed(app_id)
        _ws_release(app_id, caller.actor)
        if websocket.client_state != WebSocketState.DISCONNECTED:
            await _close(websocket, 1000)


# ── the viewer token ────────────────────────────────────────────────────────


@router.post("/v1/apps/{app_id}/viewer-token")
async def mint_viewer_token(app_id: str, preview: int = 0,
                            user: UserContext | None = Depends(get_current_user)):
    """A ten-minute claim for a person at the keyboard (never a bearer
    principal), naming who is viewing: the app reads it from
    ``X-OtoDock-Viewer`` on every proxied request. ``preview=1`` from an
    approver names the preview copy's process, so the working copy's page
    talks to the working copy's server; the platform's render principal
    gets a two-minute claim naming the check instance."""
    from api.apps.app_actions import _check_fire_rate
    from api.apps.apps import _can_approve_surface, _viewer_username, _visible_row
    from api.apps import manifest as _mf
    u = require_human(user)
    row = await run_db(_visible_row, app_id, u)
    if not row:
        raise HTTPException(status_code=404, detail="App not found")
    render = bool(u.render_app) and u.render_app == app_id
    if not render:
        _check_fire_rate(app_id, "\x00viewer-token", u.sub, interval=VIEWER_TOKEN_INTERVAL_S)
    from storage.sharing import share_store
    username = "" if render else await run_db(_viewer_username, u)
    grant = ""
    direct = render or u.is_admin or (
        (row.get("owner_sub") or "") == u.sub if row.get("username")
        else u.can_access_agent(row.get("agent") or ""))
    if not direct:
        # The share that admits them: their own, else the strongest
        # placement on an agent they hold (SHARING.md).
        def _admitting_share() -> str:
            share = share_store.internal_grant("app", app_id, u.sub)
            if share:
                return share["id"]
            placed = share_store.placements_for_user(app_id, u.sub, list(u.agents))
            return placed[0]["share_id"] if placed else ""
        grant = await run_db(_admitting_share)
    role = await run_db(_mf.caller_role, row, u)
    instance = app_supervisor.LIVE
    if render:
        instance = app_supervisor.CHECK
    elif preview and _can_approve_surface(row, u):
        instance = app_supervisor.PREVIEW
    claims = {
        "principal": "viewer", "sub": u.sub, "username": username,
        "role": role, "grant": grant, "agent": row.get("agent") or "",
        "visibility": db_apps.app_scope(row.get("username")), "external": False,
        "instance": instance,
    }
    ttl = app_tokens.VIEWER_TTL_S
    if render:
        claims["render"] = u.render_jti or "render"
        ttl = RENDER_VIEWER_TTL_S
    token = app_tokens.mint(app_id, app_tokens.PURPOSE_VIEWER, claims, ttl)
    return {"token": token, "exp": int(time.time()) + ttl, "ttl": ttl}


@router.get("/v1/internal/render/verify")
async def verify_render_token(request: Request) -> Response:
    """file-tools asks whether the bearer is a live render token before it
    starts a browser (``auth/render_principal.py``): 204 while the job runs,
    401 after, so a token that leaks from a log is dead with the job."""
    from auth import render_principal
    if render_principal.verify(_bearer(request.headers)) is None:
        raise HTTPException(status_code=401, detail="not a live render token")
    return Response(status_code=204)


# ── client files ────────────────────────────────────────────────────────────


def connect_sources(origin: str, app_id: str, base: str = "") -> tuple[str, ...]:
    """The path-scoped ``connect-src`` of a folder document: the app's API
    and socket under its own prefix (``/v1/apps/<id>`` on the dashboard,
    ``/s/<token>`` on a link, where the platform methods are not offered)."""
    ws = "wss://" if origin.startswith("https://") else "ws://"
    host = origin.split("://", 1)[1] if "://" in origin else origin
    if base:
        return (f"{origin}{base}/api/", f"{ws}{host}{base}/ws/", f"{ws}{host}{base}/ws")
    return (f"{origin}/v1/apps/{app_id}/api/", f"{origin}/v1/apps/{app_id}/platform/",
            f"{ws}{host}/v1/apps/{app_id}/ws/", f"{ws}{host}/v1/apps/{app_id}/ws")


def document_response(row: dict, release_dir, origin: str, base: str = ""):
    """The wrapped ``client/index.html`` of a release (sync)."""
    return _document_response(row, release_dir, origin, base)


def _document_response(row: dict, release_dir, origin: str, base: str = ""):
    from api.apps.apps import APP_RUNTIME
    from services.apps import app_sandbox
    try:
        data = releases.read_release_file(release_dir, "client/index.html")
    except releases.ReleaseDamaged:
        data = None
    if data is None:
        return _ui_response(_placeholder("This app's release copy is damaged — ask the agent to deploy it again."),
                            origin, 404)
    content = data.decode("utf-8", "replace")
    connect = connect_sources(origin, row["id"], base) if app_sandbox.server_entry(release_dir) else ()
    if is_full_document(content):
        return _ui_response(inject_runtime(content, runtime_extra=APP_RUNTIME), origin,
                            connect=connect)
    return _ui_response(wrap_fragment(content, runtime_extra=APP_RUNTIME), origin, connect=connect)


def _resolve_document_dir(row: dict, user: UserContext, sha: str, preview: bool):
    """The release directory a document request names: the live release,
    or the preview copy for the owner or an editor who asked for it; None
    when ``sha`` is neither."""
    from api.apps.apps import _can_approve_surface
    if user.render_app and user.render_app == row.get("id"):
        # The platform's render loads the copy its job named by the hash
        # (a release, or a scratch copy of the working tree).
        return releases.find_release_by_sha(row, sha)
    if preview and _can_approve_surface(row, user):
        d = releases.preview_dir(row)
        if (d / releases.MANIFEST_NAME).is_file() and releases.tree_sha(d) == sha:
            return d
    if not sha or sha != (row.get("release_sha256") or ""):
        return None
    try:
        return releases.live_release_dir(row)
    except releases.ReleaseDamaged:
        return None


async def folder_document(row: dict, user: UserContext, request: Request, sha: str = "",
                          preview: bool = False):
    """Shared by the client document route and ``serve_app`` for folder
    rows: ``sha`` empty means "the live release, whatever its hash"."""
    origin = request_origin(request)
    if not sha:
        try:
            d = await asyncio.to_thread(releases.live_release_dir, row)
        except releases.ReleaseDamaged:
            d = None
        if preview:
            from api.apps.apps import _can_approve_surface
            pd = releases.preview_dir(row)
            if _can_approve_surface(row, user) and (pd / releases.MANIFEST_NAME).is_file():
                d = pd
    else:
        d = await asyncio.to_thread(_resolve_document_dir, row, user, sha, preview)
    if d is None:
        words = ("This app's first release waits for approval on its card."
                 if int(row.get("pending_release") or 0) and not row.get("release_path")
                 else "This app has no release yet — ask the agent to deploy it.")
        return _ui_response(_placeholder(words), origin, 404)
    return await asyncio.to_thread(_document_response, row, d, origin)


@router.get("/v1/apps/{app_id}/client/{sha}/")
async def client_document(app_id: str, sha: str, request: Request, preview: int = 0,
                          user: UserContext | None = Depends(get_current_user)):
    from api.apps.apps import _visible_row
    origin = request_origin(request)
    if user is None:
        return _ui_response(_placeholder("Sign in to OtoDock to view this app."), origin, 401)
    row = await run_db(_visible_row, app_id, user)
    if not row or not db_apps.app_kind_of(row).may_serve:
        return _ui_response(_placeholder("This app no longer exists."), origin, 404)
    return await folder_document(row, user, request, sha, bool(preview))


def _asset_path_ok(path: str) -> bool:
    if not path or has_traversal(path):
        return False
    parts = path.split("/")
    if any(not p or p.startswith(".") for p in parts) or parts[0].startswith("_"):
        return False
    return not path.lower().endswith(_NEVER_ASSETS)


def asset_response(row: dict, sha: str, path: str) -> Response:
    """One release file for the frame, or the 404 (shared with the link's
    twin). Sync."""
    if not _asset_path_ok(path):
        raise HTTPException(status_code=404, detail="Not found")
    found = _read_asset_of(row, sha, path)
    if found is None:
        raise HTTPException(status_code=404, detail="Not found")
    data, ctype = found
    name = path.rsplit("/", 1)[-1].replace('"', "")
    return Response(content=data, media_type=ctype, headers={
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="{name}"',
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cache-Control": "private, max-age=31536000, immutable",
    })


def _read_asset(app_id: str, sha: str, path: str) -> tuple[bytes, str] | None:
    row = task_store.get_app(app_id)
    if not row or row.get("hidden") or not db_apps.app_kind_of(row).may_serve:
        return None
    return _read_asset_of(row, sha, path)


def _read_asset_of(row: dict, sha: str, path: str) -> tuple[bytes, str] | None:
    d = releases.find_release_by_sha(row, sha)
    if d is None:
        return None
    try:
        data = releases.read_release_file(d, "client/" + path)
    except releases.ReleaseDamaged:
        return None
    if data is None:
        return None
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    ctype = _ASSET_TYPES.get(ext) or (mimetypes.guess_type(path)[0] or "application/octet-stream")
    if ctype.startswith("text/html"):
        ctype = "application/octet-stream"
    return data, ctype


@router.get("/v1/apps/{app_id}/client/{sha}/{path:path}")
async def client_asset(app_id: str, sha: str, path: str, request: Request, preview: int = 0,
                       user: UserContext | None = Depends(get_current_user)):
    """A release file on the tree hash alone (the frame sends no cookie):
    verified against the manifest, a content type from the extension
    allowlist, never rendered as a document."""
    if path in ("", "index.html"):
        return await client_document(app_id, sha, request, preview, user)
    if not _APP_ID_RE.match(app_id) or not _asset_path_ok(path):
        raise HTTPException(status_code=404, detail="Not found")
    found = await run_db(_read_asset, app_id, sha, path)
    if found is None:
        raise HTTPException(status_code=404, detail="Not found")
    data, ctype = found
    name = path.rsplit("/", 1)[-1].replace('"', "")
    return Response(content=data, media_type=ctype, headers={
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="{name}"',
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cache-Control": "private, max-age=31536000, immutable",
    })


# ── logs ────────────────────────────────────────────────────────────────────


@router.get("/v1/apps/{app_id}/logs")
async def read_logs(app_id: str, tail: int = 200,
                    user: UserContext | None = Depends(get_current_user)):
    from api.apps.apps import _can_manage, _visible_row
    u = require_auth(user)
    row = await run_db(_visible_row, app_id, u)
    if not row:
        raise HTTPException(status_code=404, detail="App not found")
    if not _can_manage(row, u):
        raise HTTPException(status_code=403, detail="Not authorized to read this app's logs")
    lines = max(1, min(int(tail), LOG_TAIL_MAX))
    text = await asyncio.to_thread(app_supervisor.read_log_tail, row, lines)
    return {"log": text, **app_supervisor.status(app_id)}


# ── platform methods and feeds for servers (APPS.md) ──────────────────────

PLATFORM_RATE = 4.0


# A check's handler wake (CHECKS.md "The handler kind") is a synchronous
# POST with no delivery row: the run is registered here for its minute so
# the handler's platform calls verify like a handler's.
_inflight_checks: dict[str, dict] = {}


def register_inflight_check(run_id: str, app_id: str, handler: str, event: str, *, ttl_s: int) -> None:
    from storage import db_app_deliveries as deliveries
    _inflight_checks[run_id] = {"id": run_id, "app_id": app_id, "handler": handler, "event": event,
                                "status": deliveries.INFLIGHT, "payload": {},
                                "expires": time.monotonic() + ttl_s}


def unregister_inflight_check(run_id: str) -> None:
    _inflight_checks.pop(run_id, None)


async def inflight_delivery(claim: str, row: dict) -> dict:
    """The delivery a forwarded claim of principal ``platform`` belongs to,
    while it is in flight (APPS.md "Handlers"); a refusal otherwise. The
    launch token alone never unlocks a write — every request handler in the
    app holds it — so the claim the proxy sent with the fire plus the row
    it names are the gate. A check's wake (``kind: check``) is in flight
    while its registration lives."""
    from storage import db_app_deliveries as deliveries
    claims = app_tokens.verify(claim, row["id"], app_tokens.PURPOSE_CALLER)
    if not claims or claims.get("principal") != "platform":
        raise _refuse(400, "the forwarded viewer claim is not this app's")
    if claims.get("kind") == "check":
        reg = _inflight_checks.get(str(claims.get("delivery") or ""))
        if not reg or reg["app_id"] != row["id"] or reg["expires"] < time.monotonic():
            raise _refuse(403, "the check claim is not in flight")
        return dict(reg)
    d = await run_db(deliveries.get, str(claims.get("delivery") or ""))
    if not d or d["app_id"] != row["id"] or d["status"] != deliveries.INFLIGHT:
        raise _refuse(403, "the handler claim is not in flight")
    return d


async def step_caller(request_headers, row: dict) -> tuple[Caller, dict] | None:
    """A step's script calling the platform (APPS.md "Steps"): the bearer is
    the caller claim the fire minted (``OTODOCK_STEP_TOKEN``, purpose
    ``app_caller``, ``kind: step``) and its delivery is in flight. None when
    the bearer is not such a claim (the other callers decide then); a step
    claim whose delivery ended is refused, never demoted to another basis.
    Only the platform-method and the action routes accept it — every other
    route sees an unknown token."""
    token = _bearer(request_headers)
    if not token:
        return None
    claims = app_tokens.verify(token, row["id"], app_tokens.PURPOSE_CALLER)
    if not claims or claims.get("kind") != "step":
        return None
    d = await inflight_delivery(token, row)
    caller = Caller(basis="step", sub=row["id"], role="app", claim=token,
                    exp=int(claims.get("exp") or 0), extra=claims)
    return caller, d


async def _viewer_behind(request_headers, row: dict, caller: Caller):
    """The principal a platform call runs as: the viewer named by a
    forwarded ``X-OtoDock-Viewer`` claim (verified for this app), a viewer
    or agent caller's own user, the app identity on the platform basis (a
    handler forwarding the claim the fire brought, while its delivery is in
    flight), else the app identity within its agent-scope slice.
    ``(principal, is_viewer, basis, floor role)``: an agent session, direct
    or relayed by the app's server, is judged at its per-agent row (the
    claim's role, viewer when it names none); None judges the principal."""
    from api.apps import catalog
    from auth.providers import user_context_for_sub
    forwarded = request_headers.get("x-otodock-viewer", "") if caller.basis == "app" else ""
    if forwarded:
        claims = app_tokens.verify(forwarded, row["id"], app_tokens.PURPOSE_VIEWER)
        if not claims:
            relayed = app_tokens.verify(forwarded, row["id"], app_tokens.PURPOSE_CALLER) or {}
            if relayed.get("placement"):
                # A placed agent's session reaches the exports alone, and its
                # claim passed on by the server reaches no more than that.
                raise _refuse(403, "not available to a placed agent's session")
            if relayed.get("principal") == app_tokens.PRINCIPAL_AGENT:
                # An agent's call the server passes on (APPS.md "Agents call
                # apps"): the session's user, as the agent's own call to this
                # route runs; a no-user session runs as the app.
                user = await run_db(user_context_for_sub, str(relayed.get("sub") or ""))
                if user is None:
                    return catalog.app_principal(row), False, "agent", None
                role = str(relayed.get("role") or "")
                return user, True, "agent", (role if role in roles.RANK else roles.VIEWER)
            await inflight_delivery(forwarded, row)
            # An inbound wake's claim (APPS.md "Inbound hooks") is server-only:
            # the basis says so and the write gate below refuses it.
            kind = relayed.get("kind")
            return catalog.app_principal(row), False, ("inbound" if kind == "inbound" else "platform"), None
        if claims.get("external"):
            raise _refuse(403, "not available on shared links")
        if claims.get("render"):
            raise _refuse(403, "not available in the rendered check")
        user = await run_db(user_context_for_sub, str(claims.get("sub") or ""))
        if user is None:
            raise _refuse(400, "the forwarded viewer is unknown")
        return user, True, "viewer", None
    if caller.basis == "viewer":
        user = await run_db(user_context_for_sub, caller.sub)
        if user is None:
            raise _refuse(400, "the viewer is unknown")
        return user, True, "viewer", None
    if caller.basis == "agent" and caller.user is not None:
        return caller.user, True, "agent", caller.role
    return catalog.app_principal(row), False, caller.basis, None


NOTIFY_PER_HOUR = 30


async def _notify_from_handler(row: dict, args: dict) -> dict:
    """``notifications.create`` on the platform basis: a shared app tells
    every member of its agent, a personal app its owner; never a named user,
    never everyone (APPS.md "Handlers")."""
    from services.notifications.notification_manager import fire_notification
    title = str((args or {}).get("title") or "").strip()[:200]
    body = str((args or {}).get("body") or "").strip()[:2000]
    severity = str((args or {}).get("severity") or "info")
    if severity not in ("info", "success", "warning", "danger"):
        severity = "info"
    if not title:
        return {"ok": False, "reason": "title is required"}
    if not _take(f"{row['id']}|notify", NOTIFY_PER_HOUR / 3600.0, float(NOTIFY_PER_HOUR)):
        raise _refuse(429, "too many notifications this hour")
    personal = bool(row.get("username"))
    deliveries = await fire_notification(
        title, body, severity=severity,
        scope="user" if personal else "agent",
        target=(row.get("owner_sub") or "") if personal else row["agent"],
        source="app", source_id=row["id"], agent_slug=row["agent"], href=f"/apps/{row['id']}",
    )
    return {"ok": True, "result": {"delivered": len(deliveries or []),
                                   "to": "owner" if personal else "members"}}


@router.options("/v1/apps/{app_id}/platform/{method}")
async def preflight_platform(app_id: str, method: str) -> Response:
    return Response(status_code=204, headers=dict(_CORS))


@router.post("/v1/apps/{app_id}/platform/{method}")
async def platform_method(app_id: str, method: str, request: Request):
    """A platform method for an app's server or an agent: reads as the
    viewer behind the call when one is forwarded, else as the app identity;
    a write with no viewer is refused; four calls per second per caller."""
    from api.apps import catalog
    from api.apps import manifest as _mf
    from api.apps.apps import _catalog_entry, finish_platform_result
    row = await run_db(_row_or_404, app_id)
    stepping = await step_caller(request.headers, row)
    if stepping is not None:
        # A step's script: the app identity on the platform basis for the
        # life of its delivery (APPS.md "Steps"), as a handler's forwarded
        # claim gives its server.
        caller, _d = stepping
    else:
        caller = await resolve_caller(request, row)
    if caller.basis == "external":
        raise _refuse(403, "not available on shared links")
    # The render's claim names the owner of a personal app so the page
    # renders as they see it; it reads and writes nothing as them.
    if caller.extra.get("render"):
        raise _refuse(403, "not available in the rendered check")
    # Before the viewer behind the call is resolved: with no person, the
    # fall-through would hand such a session the home app's own identity.
    if caller.extra.get("placement"):
        raise _refuse(403, "not available to a placed agent's session")
    if method not in catalog.METHODS:
        raise _refuse(404, "unknown platform method")
    if not _take(f"{app_id}|platform|{caller.actor}", PLATFORM_RATE, PLATFORM_RATE):
        raise _refuse(429, "too many requests")
    if caller.basis == "step":
        principal, has_viewer, basis, floor_role = catalog.app_principal(row), False, "platform", None
    else:
        principal, has_viewer, basis, floor_role = await _viewer_behind(request.headers, row, caller)
    # Writes: a viewer behind the call, or the platform basis of a wake in
    # flight. An inbound wake (basis `inbound`) is server-only (APPS.md
    # "Inbound hooks"). `notifications.create` has one more caller: the app
    # identity alone — the launch token with no forwarded claim — tells its
    # own people (the members, or the owner of a personal app), which is how
    # a server says "a booking landed" from a customer's request or a
    # vendor's event alike (APPS.md "Secrets" / "Inbound hooks").
    own_notify = method == "notifications.create" and basis == "app" and not has_viewer
    # The app reads its own audience the same way, and so does a session of
    # its own agent with no person behind it: the approval is the floor (a
    # person is judged at editor or above).
    own_audience = method == catalog.AUDIENCE_METHOD and not has_viewer and basis in ("app", "agent")
    if own_notify and caller.instance != app_supervisor.LIVE:
        raise _refuse(403, "the preview copy never notifies the app's people")
    if own_audience and caller.instance != app_supervisor.LIVE:
        raise _refuse(403, "the preview copy never reads the app's audience")
    if method == "files.write" and not has_viewer and basis != "platform":
        raise _refuse(403, "a viewer claim is required for a write"
                      if basis != "inbound" else "not available to an inbound wake")
    if method == "notifications.create" and not has_viewer and basis != "platform" and not own_notify:
        raise _refuse(403, "a viewer claim is required for a write"
                      if basis != "inbound" else "not available to an inbound wake")
    # A person's setup is theirs: the page's own call, or a server call that
    # forwards that person's claim — never the app identity, a wake, an
    # agent session or an inbound event.
    if method in catalog.PERSON_METHODS and basis != "viewer":
        raise _refuse(403, "a person's own page is required")
    if floor_role is None and basis == "viewer":
        # A forwarded viewer: the role their floors are judged at (a share's
        # role for a person a share admits), which viewer.me reports too.
        floor_role = await run_db(_mf.caller_role, row, principal)
    try:
        entry = _catalog_entry(row, principal, method=method,
                               unattended=(basis == "platform" or own_notify or own_audience),
                               role=floor_role)
    except HTTPException as e:
        raise _refuse(e.status_code, str(e.detail))
    body = await _read_body(request)
    try:
        doc = json.loads(body or b"{}")
    except ValueError:
        raise _refuse(400, "a JSON body is required")
    args = (doc or {}).get("args") if isinstance(doc, dict) else None
    if entry.get("args_schema"):
        validated, err = _mf.validate_args(entry["args_schema"], args)
        if err:
            raise _refuse(400, err)
        args = validated
    if method == "notifications.create" and (basis == "platform" or own_notify):
        out = await _notify_from_handler(row, args if isinstance(args, dict) else {})
        return JSONResponse(out, headers=dict(_CORS))

    def _run() -> dict:
        try:
            return {"ok": True, "result": catalog.run_method(method, row.get("agent") or "",
                                                              row, principal, args,
                                                              role=floor_role)}
        except (ValueError, PermissionError) as e:
            return {"ok": False, "reason": str(e)}

    out = await asyncio.to_thread(_run)
    await finish_platform_result(out, principal.sub)
    return JSONResponse(out, headers=dict(_CORS))


def _row_declaring_feed(app_id: str, feed: str) -> dict | None:
    """The app's row as it is now when its approved manifest declares
    ``feed`` as a ``data_feed``, else None: a server reads only the feeds
    its approval card listed, the rule the page's own feed reads follow
    (``_catalog_entry``, with the approval as the floor). Read afresh per
    frame, so an approval that lands or a manifest that changes while the
    socket is open counts at once."""
    from api.apps.apps import _catalog_entry
    try:
        row = _row_or_404(app_id)
        _catalog_entry(row, None, feed=feed, unattended=True)
    except HTTPException:
        return None
    return row


@router.websocket("/v1/apps/{app_id}/platform/ws")
async def platform_ws(websocket: WebSocket, app_id: str):
    """An app server's own subscription socket: the launch token first,
    then ``catalog_subscribe`` / ``catalog_unsubscribe`` / ``catalog_snapshot``
    frames; deltas of the agent-scope slice arrive as ``catalog`` frames
    with a sequence per feed. Never counts as viewer activity."""
    from api.apps import catalog
    await websocket.accept()
    try:
        row = await run_db(_row_or_404, app_id)
    except HTTPException:
        await _close(websocket, 1008, "unknown app")
        return
    try:
        first = await asyncio.wait_for(websocket.receive_text(), timeout=WS_AUTH_TIMEOUT_S)
        msg = json.loads(first)
        caller = await caller_from_token(str((msg or {}).get("token") or ""), row)
    except HTTPException:
        await _close(websocket, 4401, "the launch token is required")
        return
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
        await _close(websocket, 4400, "auth frame expected")
        return
    if caller.basis != "app":
        await _close(websocket, 4401, "the launch token is required")
        return
    agent = row.get("agent") or ""
    # The launch claim names the process: the preview copy's server keeps
    # its own subscriptions and never takes the live server's down.
    sub = catalog.open_app_subscription(app_id, agent, caller.instance)

    async def reader() -> None:
        while True:
            raw = await websocket.receive_text()
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            kind = str(frame.get("type") or "")
            feed = str(frame.get("feed") or "")
            if kind in (wire.IN_CATALOG_SUBSCRIBE, wire.IN_CATALOG_UNSUBSCRIBE):
                on = kind == wire.IN_CATALOG_SUBSCRIBE
                # Dropping a feed is always allowed; taking one needs it declared.
                ok = ((not on or await run_db(_row_declaring_feed, app_id, feed) is not None)
                      and catalog.app_subscribe(sub, feed, on))
                await websocket.send_json({"type": "catalog_ack", "feed": feed, "ok": ok})
            elif kind == "catalog_snapshot":
                current = (await run_db(_row_declaring_feed, app_id, feed)
                           if feed in catalog.FEEDS and feed not in catalog.CLIENT_FEEDS else None)
                if current is None:
                    await websocket.send_json({"type": "catalog_ack", "feed": feed, "ok": False})
                    continue
                rows = await asyncio.to_thread(catalog.snapshot_for_app, feed, agent, current)
                await websocket.send_json({
                    "type": wire.CATALOG, "agent": agent, "feed": feed, "snapshot": rows,
                    "seq": catalog.current_seq(sub.seq_key, agent, feed),
                })

    async def writer() -> None:
        # Polled: the queue is filled from whichever thread emits, and this
        # socket must not bind a waiter to its loop.
        while True:
            frame = sub.queue.get_nowait()
            if frame is None:
                await asyncio.sleep(0.2)
                continue
            await websocket.send_json(frame)

    tasks = [asyncio.create_task(reader()), asyncio.create_task(writer())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        catalog.close_app_subscription(sub)
        if websocket.client_state != WebSocketState.DISCONNECTED:
            await _close(websocket, 1000)


# ── the REST twins of push and state ───────────────────────────────────────


@router.options("/v1/apps/{app_id}/push")
@router.options("/v1/apps/{app_id}/state")
async def preflight_twins(app_id: str) -> Response:
    return Response(status_code=204, headers=dict(_CORS))


async def _writer(request: Request, app_id: str) -> tuple[dict, Caller]:
    row = await run_db(_row_or_404_any, app_id)
    caller = await resolve_caller(request, row)
    if caller.basis not in ("agent", "app"):
        raise _refuse(403, "the page never writes the shared document")
    if caller.extra.get("placement"):
        raise _refuse(403, "not available to a placed agent's session")
    if caller.instance != app_supervisor.LIVE:
        raise _refuse(403, "the preview copy never writes the live app's document")
    # The pin authority of the session hooks (api/hooks/pins.py
    # ``_require_shared_pin_authority``): a shared row takes a human session
    # at editor or above. Only an agent caller carries a user; a no-user
    # session and the app's own launch token pass as they do on the hooks.
    if (caller.user is not None and not row.get("username")
            and not roles.can_edit(caller.user.effective_role(row.get("agent") or ""))):
        raise _refuse(403, "your role cannot update a shared (team) dashboard: editor or manager required")
    check_rate(app_id, caller.actor)
    return row, caller


def _row_or_404_any(app_id: str) -> dict:
    """Push and state work for file apps too (an agent without the display
    tools, or an app's own server)."""
    if not _APP_ID_RE.match(app_id):
        raise _refuse(404, "App not found")
    row = task_store.get_app(app_id)
    if not row or row.get("hidden") or db_apps.personal_row_dormant(row):
        raise _refuse(404, "App not found")
    return row


@router.post("/v1/apps/{app_id}/push")
async def push_rest(app_id: str, request: Request):
    row, caller = await _writer(request, app_id)
    body = await _read_body(request)
    try:
        doc = json.loads(body or b"{}")
    except ValueError:
        raise _refuse(400, "a JSON body is required")
    from api.hooks import pins
    out = await pins.push_row(row, (doc or {}).get("payload") if isinstance(doc, dict) else None)
    return JSONResponse(out, headers=dict(_CORS))


@router.patch("/v1/apps/{app_id}/state")
async def state_rest(app_id: str, request: Request):
    row, caller = await _writer(request, app_id)
    body = await _read_body(request)
    try:
        doc = json.loads(body or b"{}")
    except ValueError:
        raise _refuse(400, "a JSON body is required")
    if not isinstance(doc, dict):
        raise _refuse(400, "a JSON object is required")
    from api.hooks import pins
    if doc.get("doc") is not None:
        patch, replace = doc["doc"], True
    else:
        patch, replace = doc.get("patch"), bool(doc.get("replace"))
    who = caller.username or caller.sub if caller.basis == "agent" else f"app:{app_id}"
    out = await pins.write_state_row(row, patch, replace=replace, updated_by=who)
    return JSONResponse(out, headers=dict(_CORS))

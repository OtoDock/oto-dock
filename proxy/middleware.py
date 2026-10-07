"""HTTP middleware: one pure-ASGI layer, registered by ``register_middlewares(app)``.

Every HTTP request passes one ``PlatformHttpMiddleware``, which judges in
this order, outermost first: the sliding session refresh and the security
headers on the way out, the 500 guard with timing, the render,
external-session and service-key confinements, then the request body cap
and the body deadline of a request without a valid credential. It is one
pure-ASGI layer, never a stack of ``BaseHTTPMiddleware`` wrappers: each of
those builds a task group and a memory stream per request and per streamed
chunk, on the loop. ``app.install_db_unavailable_guards`` is registered after it and
stays outermost. WebSocket and lifespan scopes pass straight through.
"""

import asyncio
import logging
import re
import time

import psycopg
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request, cookie_parser
from starlette.responses import JSONResponse, Response

import config

logger = logging.getLogger("claude-proxy")


# Static-asset extensions whose responses must stay cacheable — never attach a
# Set-Cookie to them in the sliding-session refresh below.
_STATIC_EXTS = {
    "js", "css", "map", "png", "jpg", "jpeg", "svg", "gif", "ico",
    "woff", "woff2", "ttf", "webp", "json", "txt",
}

# Paths whose unhandled exceptions are logged with their timing and answered
# with a JSON 500 (the dashboard and task APIs).
_LOGGED_PREFIXES = ("/dashboard", "/v1/tasks", "/v1/schedules", "/v1/triggers",
                    "/v1/agents", "/v1/admin", "/v1/chats", "/auth/")

_KB = 1024
_MB = 1024 * 1024


# --- the request body caps ---------------------------------------------------
#
# The cap is chosen from the path (``/auth/``, the webhook receivers, the
# per-route caps below, else the default), then narrowed to
# ``MAX_UNAUTH_BODY_BYTES`` for a request that carries no platform credential.
# It counts the streamed bytes, so a chunked body with no Content-Length is
# capped like any other. A tier set to 0 is off; nothing exceeds the
# backstop ``MAX_REQUEST_BODY_BYTES``.

def _backstop() -> int:
    return config.MAX_REQUEST_BODY_BYTES


def _mcp_gateway_cap() -> int:
    from core.credentials.mcp_gateway import MAX_BODY_BYTES
    return MAX_BODY_BYTES


def _file_save_cap() -> int:
    # The editor saves, as JSON, a file it opened inline, so the body is
    # bounded by INLINE_TEXT_MAX_BYTES; escaping a control character takes
    # six bytes. 0 = no inline limit, so no bound here either.
    inline = config.INLINE_TEXT_MAX_BYTES
    return 6 * inline + 64 * _KB if inline > 0 else _backstop()


# The routes that take large bodies, each with its own bound (the route's own
# limit plus framing): only these reach past the default, since a credentialed
# caller may send up to the cap into memory or a JSON parse on the loop. The
# app import, release and seed hooks take a path, not the bytes.
_ROUTE_CAPS = (
    # multipart, spooled to disk by the parser; the dashboard sends up to
    # 32 MB in one request
    ("POST", re.compile(r"^/v1/upload$"), _backstop),
    ("PUT", re.compile(r"^/v1/upload/chunked/[A-Za-z0-9_-]+/[0-9]+$"),
     lambda: config.UPLOAD_CHUNK_BYTES + 64 * _KB),
    # the zip installs' own 100 MB cap (``api/mcp/mcps.py``); the multipart
    # is parsed before the admin check
    ("POST", re.compile(r"^/v1/admin/(mcps|skills)/install$"), lambda: 101 * _MB),
    # multipart spooled to disk; its cap is an admin setting
    ("POST", re.compile(r"^/v1/audio/transcribe$"), _backstop),
    # base64 images in JSON, parsed on the loop
    ("POST", re.compile(r"^/v1/hooks/images$"), lambda: 64 * _MB),
    # WOPI PutFile / PutRelativeFile: the token is checked before the read
    ("POST", re.compile(r"^/wopi/files/[^/]+(/contents)?$"), _backstop),
    # Collabora's insert-file upload, held in memory and forwarded
    ("POST", re.compile(r"^/collabora/.+"), lambda: 100 * _MB),
    ("PUT", re.compile(r"^/v1/agents/[^/]+/files/.+$"), _file_save_cap),
    # a browser's CSP violation report, with or without the session cookie
    ("POST", re.compile(r"^/v1/csp-report$"), lambda: 64 * _KB),
    # an MCP client's request through the credential gateway, under the
    # gateway's own cap (``api/mcp/gateway.py``)
    ("POST", re.compile(r"^/v1/mcp-gateway/[^/]+/"), _mcp_gateway_cap),
)
# Routes authenticated by their own app or link tokens: the unauthenticated
# tier does not apply. These read the body themselves after their token
# check, under the app proxy's own 1 MB cap.
_OWN_TOKEN_ROUTES = (
    re.compile(r"^/v1/apps/[^/]+/(api|platform|egress|bindings)/"),
    re.compile(r"^/v1/apps/[^/]+/(push|state|events)$"),
    re.compile(r"^/s/[^/]+/api/"),
)
# The action routes (an app's launch token or a step claim, a share link)
# parse a small JSON body before their own token check: 1 MB for anyone.
_ACTION_ROUTES = (
    re.compile(r"^/v1/apps/[^/]+/actions/"),
    re.compile(r"^/s/[^/]+/actions/"),
)
_ACTION_CAP = 1 * _MB


def _tier(value: int) -> int:
    return min(value, _backstop()) if value > 0 else _backstop()


def body_cap(method: str, path: str) -> tuple[int, bool]:
    """The body cap for ``method path`` (the routed path) and whether the
    unauthenticated tier may narrow it."""
    if path.startswith("/auth/"):
        return _tier(config.MAX_AUTH_BODY_BYTES), True
    if path.startswith("/v1/webhooks/"):
        return _tier(config.MAX_WEBHOOK_BODY_BYTES), False
    for m, rx, cap in _ROUTE_CAPS:
        if method == m and rx.match(path):
            return min(cap(), _backstop()), True
    if any(rx.match(path) for rx in _ACTION_ROUTES):
        return min(_ACTION_CAP, _backstop()), False
    return _tier(config.MAX_JSON_BODY_BYTES), not any(rx.match(path) for rx in _OWN_TOKEN_ROUTES)


def credential_state(request: Request, cookies: dict, path: str) -> str:
    """``valid`` when the request carries a platform credential that verifies
    without the database, ``invalid`` when it presents one that does not,
    else ``none``. Pinned to the validators, not to the signature (the same
    secret signs the 2FA step, reset, invite and link tokens): a dashboard
    session cookie, the master key or a session token as the bearer, and on
    WOPI and Collabora paths a WOPI ``access_token``."""
    from auth.external_endpoints import session_token_claims
    from auth.service_endpoints import extract_master_key
    presented = request.headers.get("authorization", "") != ""
    if extract_master_key(request) is not None or session_token_claims(request) is not None:
        return "valid"
    session = cookies.get("session")
    if session:
        presented = True
        from auth.providers import validate_session_jwt
        if validate_session_jwt(session) is not None:
            return "valid"
    if path.startswith(("/wopi/", "/collabora/")):
        token = request.query_params.get("access_token", "")
        if token:
            presented = True
            from api.media.wopi import validate_wopi_token
            if validate_wopi_token(token) is not None:
                return "valid"
    return "invalid" if presented else "none"


# --- the confinements --------------------------------------------------------


def routed_path(request: Request) -> str:
    """The path the router matches (``scope["path"]``) with a decoded ``?``
    or ``#`` put back in its encoded form. ``request.url.path`` re-parses
    the decoded path and cuts it at either, so an allowlist judging it saw
    a shorter path than the one routed (``/v1/sessions/warmup%3F/abort``
    read as the allowed ``/v1/sessions/warmup``)."""
    return scope_routed_path(request.scope)


def scope_routed_path(scope) -> str:
    """``routed_path`` read straight from an ASGI scope."""
    path = scope.get("path") or ""
    return path.replace("?", "%3F").replace("#", "%23")


def _service_key_refusal(request: Request) -> Response | None:
    """The master ``PROXY_API_KEY`` reaches only its service-to-service
    allowlist (``auth/service_endpoints.py``): 403 elsewhere, so a leaked
    key cannot drive arbitrary user or admin routes."""
    from auth.service_endpoints import extract_master_key, is_service_endpoint_allowed
    if extract_master_key(request) is None:
        return None
    if is_service_endpoint_allowed(request.method, routed_path(request)):
        return None
    logger.warning("Master key blocked from non-S2S endpoint: %s %s",
                   request.method, request.url.path)
    return JSONResponse({"detail": "This endpoint is not available to the service key"},
                        status_code=403)


# The (sid, reason) pairs already logged, so a dead token retried by a hook
# ladder or an MCP client logs once.
_REFUSED_LOGGED: set[tuple[str, str]] = set()
_REFUSED_LOGGED_CAP = 512


def _log_session_refusal(request: Request, sid: str, reason: str) -> None:
    key = (sid, reason)
    if key in _REFUSED_LOGGED:
        return
    if len(_REFUSED_LOGGED) >= _REFUSED_LOGGED_CAP:
        _REFUSED_LOGGED.clear()
    _REFUSED_LOGGED.add(key)
    logger.info("Session token refused (%s): %s %s sid=%s",
                reason, request.method, request.url.path, sid[:8])


def _session_refusal(request: Request) -> Response | None:
    """A session token as the bearer is accepted only while its session is
    live, and never when it was minted for an earlier life of the same
    session id (``session_state.session_token_refusal``, decision 8):
    401 before any database read, on both listeners. A token carrying an
    ``ext`` claim and no real user (an external caller) then reaches only
    ``auth/external_endpoints.py``'s allowlist."""
    from auth.external_endpoints import (
        EXTERNAL_BLOCKED_DETAIL,
        SESSION_DEAD_DETAIL,
        is_external_endpoint_allowed,
        session_token_claims,
    )
    claims = session_token_claims(request)
    if not claims:
        return None
    from core.session.session_state import session_token_refusal
    sid = claims.get("sid") or ""
    reason = session_token_refusal(claims)
    if reason:
        _log_session_refusal(request, sid if isinstance(sid, str) else "", reason)
        return JSONResponse({"detail": SESSION_DEAD_DETAIL}, status_code=401)
    if claims.get("ext") and not claims.get("user_sub") and not is_external_endpoint_allowed(
            request.method, routed_path(request)):
        logger.warning("External session blocked from endpoint: %s %s sid=%s",
                       request.method, request.url.path, sid[:8])
        return JSONResponse({"detail": EXTERNAL_BLOCKED_DETAIL}, status_code=403)
    return None


def _render_refusal(request: Request) -> Response | None:
    """The platform's own headless render (``auth/render_principal.py``)
    reaches the app it was minted for and the shell that shows it, nothing
    else. Judged on the render cookie alone, when no session cookie and no
    bearer ride with it (those principals are judged as themselves), and
    answered with 403, never 401: the dashboard turns a 401 into a page
    redirect, which would end the render."""
    from auth import render_principal as rp
    token = request.cookies.get(rp.COOKIE_NAME)
    if not token or request.cookies.get("session") or request.headers.get("authorization"):
        return None
    path = routed_path(request)
    if not path.startswith(rp._JUDGED_PREFIXES):
        return None
    claims = rp.verify(token)
    if claims is None or not rp.is_allowed(request.method, path, str(claims.get("app") or "")):
        return JSONResponse({"detail": rp.BLOCKED_DETAIL}, status_code=403)
    return None


# --- the response side -------------------------------------------------------

# Connection-level database failures, answered 503 by the outermost guard.
# The same predicate as ``app._db_unavailable`` (a test keeps the two in
# step): the 500 guard re-raises these and keeps every other error its own.
_DB_DOWN_SQLSTATES = frozenset({"57P01", "57P02", "57P03", "53300"})


def db_unavailable(exc: BaseException) -> bool:
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    if not isinstance(exc, psycopg.OperationalError):
        return False
    state = exc.sqlstate
    return state is None or state.startswith("08") or state in _DB_DOWN_SQLSTATES


# The dashboard's script policy, report-only while its reports are read
# (``/v1/csp-report``): the built shell has no inline script, the only
# script from elsewhere is Turnstile's on the login page, and the wake-word
# engine compiles WebAssembly.
SHELL_SCRIPT_POLICY = (
    "script-src 'self' 'wasm-unsafe-eval' https://challenges.cloudflare.com; "
    "object-src 'none'; base-uri 'self'; report-uri /v1/csp-report"
)
# HTML served under these is not the dashboard's shell: the API (the OAuth
# and account pop-ups carry inline scripts), the link pages and the editor
# keep their own policies or none.
_NOT_SHELL = ("/v1/", "/s/", "/collabora/", "/wopi/", "/ui-kit/", "/setup.html")


def _security_headers(headers: MutableHeaders, path: str) -> None:
    """``nosniff`` + ``Referrer-Policy`` on everything; HSTS only when the
    deployment is HTTPS (``COOKIE_SECURE``). Framing is denied everywhere
    except ``/collabora/*`` (the dashboard embeds the editor in an iframe).
    The dashboard's shell (an HTML document with no policy of its own)
    carries the script policy, report-only. ``setdefault``: a handler's own
    choice (a file response's nosniff) wins."""
    own_policy = "content-security-policy" in headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "same-origin")
    if config.COOKIE_SECURE:
        headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if not path.startswith("/collabora/"):
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    if (not own_policy and not path.startswith(_NOT_SHELL)
            and headers.get("content-type", "").startswith("text/html")):
        headers.setdefault("Content-Security-Policy-Report-Only", SHELL_SCRIPT_POLICY)


# --- the origin check on cookie writes ---------------------------------------

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# WOPI is Collabora's server calling with its token; the link pages hold
# their own same-origin check; a CSP report needs no session.
_ORIGIN_EXEMPT = ("/wopi/", "/s/", "/v1/csp-report")
_DEFAULT_PORTS = {"http": "80", "https": "443", "ws": "80", "wss": "443"}


def _host_key(netloc: str, scheme: str = "") -> str:
    """``host[:port]`` lowercased with a default port dropped (the scheme's,
    or either for a ``Host`` header, which carries none)."""
    netloc = netloc.strip().lower()
    host, sep, port = netloc.rpartition(":")
    if sep and port.isdigit():
        default = _DEFAULT_PORTS.get(scheme)
        if port == default or (not scheme and port in ("80", "443")):
            return host
    return netloc


def _origin_matches(request: Request, origin: str) -> bool:
    """The rule the dashboard socket applies (``ws/dashboard``), with ports:
    the origin's host is the request's own ``Host``, the public URL's, or
    the ``X-Forwarded-Host`` a trusted hop sent."""
    from urllib.parse import urlsplit

    from auth.lan_check import trusted_forwarded_host
    if not origin or origin == "null":
        return False
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    want = _host_key(parts.netloc, parts.scheme)
    candidates = [request.headers.get("host", ""), trusted_forwarded_host(request.scope)]
    if config.DASHBOARD_PUBLIC_URL:
        pub = urlsplit(config.DASHBOARD_PUBLIC_URL)
        candidates.append(_host_key(pub.netloc, pub.scheme))
    return any(c and _host_key(c) == want for c in candidates)


def _origin_refusal(request: Request) -> Response | None:
    """A state-changing request the dashboard session cookie authenticates
    (no bearer beside it) that names an origin other than this install's
    is refused: SameSite=Lax alone let a same-site page write. No Origin
    (a non-browser client) passes; bearer callers never meet this. Only a
    ``Bearer`` credential counts: a browser sends an edge's cached Basic or
    Negotiate header by itself, and the cookie still authenticates."""
    if request.method not in _UNSAFE_METHODS or not request.cookies.get("session"):
        return None
    if request.headers.get("authorization", "")[:7].lower() == "bearer ":
        return None
    if routed_path(request).startswith(_ORIGIN_EXEMPT):
        return None
    origin = request.headers.get("origin")
    if origin is None or _origin_matches(request, origin):
        return None
    logger.warning("Cross-site request refused: %s %s from origin %s",
                   request.method, request.url.path, origin[:120])
    return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)


# The session lifetime setting, read strictly (a failure raises, where
# ``config.get_jwt_expiry_hours`` falls back to the default) and kept a
# minute: the refresh runs for every request past a cookie's half-life.
_EXPIRY_TTL_S = 60.0
_REFRESH_READ_TIMEOUT_S = 1.0
_expiry_cache: tuple[int, float] = (0, 0.0)


def _read_expiry_hours() -> int:
    from storage import database as task_store
    value = task_store.get_platform_setting("jwt_expiry_hours")
    return int(value) if value else config.JWT_EXPIRY_HOURS


async def _expiry_hours() -> int:
    global _expiry_cache
    hours, until = _expiry_cache
    now = time.monotonic()
    if until > now:
        return hours
    from storage.pg import run_db_fast
    hours = await asyncio.wait_for(run_db_fast(_read_expiry_hours), _REFRESH_READ_TIMEOUT_S)
    _expiry_cache = (hours, now + _EXPIRY_TTL_S)
    return hours


async def _refresh_session_cookie(scope, headers: MutableHeaders, cookie: str) -> None:
    """Sliding session: re-issue the ``session`` cookie once it is past the
    halfway point of its lifetime, so an actively-used session never
    expires: the configured duration is a max-INACTIVITY window rather than
    a hard cap from login.

    Skipped for cacheable static assets, share pages (``/s/``: a link opened
    in a signed-in browser stays a link), a response whose handler set or
    deleted ``session`` itself (login, 2FA, SSO, and logout's delete: a
    logged-out session is never resurrected), an invalid or expired cookie,
    one still in the first half of its life, a deleted user's, and one that
    predates the user's last password change (this also runs on 401s, so it
    must never launder a dead cookie into a fresh one). Re-mint preserves
    identity from the decoded token; authz is unaffected because
    ``get_current_user`` resolves role and identity from the DB live.
    """
    path = scope.get("path") or ""
    if path.startswith(("/assets/", "/s/")) or path.rsplit(".", 1)[-1].lower() in _STATIC_EXTS:
        return
    if any(v.startswith("session=") for v in headers.getlist("set-cookie")):
        return
    from auth.providers import (
        apply_session_cookie, create_session_jwt, session_cookie_current,
        validate_session_jwt,
    )
    from auth.session_revocation import session_cookie_id
    payload = validate_session_jwt(cookie)
    if not payload:
        return
    iat, exp, now = payload.get("iat"), payload.get("exp"), int(time.time())
    if (isinstance(iat, int) and isinstance(exp, int) and exp > iat
            and (now - iat) < (exp - iat) / 2):
        return
    # The route resolved this exact cookie: it passed the credential
    # timeline there, no second read. The re-mint carries the instant of that
    # check as its ``iat``, so a token epoch moved while the request ran (a
    # "sign out everywhere", a password change) refuses the new cookie too.
    state = scope.get("state") or {}
    checked_at = state.get("otodock_session_cookie_checked_at")
    if state.get("otodock_session_cookie") != cookie or not isinstance(checked_at, int):
        from storage import database as task_store
        from storage.pg import run_db_fast
        checked_at = int(time.time())
        user = await asyncio.wait_for(run_db_fast(task_store.get_user, payload.get("sub", "")),
                                      _REFRESH_READ_TIMEOUT_S)
        if not user or not session_cookie_current(user, payload):
            return
    hours = await _expiry_hours()
    # The same sign-in id: a logout revokes the whole lineage of one sign-in.
    token = create_session_jwt(
        payload["sub"], payload.get("email", ""), payload.get("name", ""),
        payload.get("role", "member"),
        auth_provider=payload.get("auth_provider", "local"), expiry_hours=hours,
        jti=session_cookie_id(payload), issued_at=checked_at,
    )
    carrier = Response()
    apply_session_cookie(carrier, token, expiry_hours=hours)
    for key, value in carrier.raw_headers:
        if key == b"set-cookie":
            headers.append("set-cookie", value.decode("latin-1"))


# --- the middleware ------------------------------------------------------------

# The ASGI scope type of a plain HTTP request (WebSocket and lifespan scopes
# pass straight through).
_HTTP_SCOPE = "http"
_TOO_LARGE = b'{"detail":"Request body too large"}'
_BODY_TIMEOUT = b'{"detail":"Request body timeout"}'
_UNAUTHENTICATED = b'{"detail":"Authentication required"}'
_SERVER_ERROR = b'{"detail":"Internal Server Error"}'

# The body deadline of a request with no valid credential: the longest wait
# for one body chunk, and for the whole body from the first read. uvicorn
# waits for body bytes with no bound and the header deadline ends at the
# headers, so without it a body sent a byte at a time holds its connection
# (and a place in ``limit_concurrency``) for as long as the sender likes.
# The credential is tested only when a wait runs out: a signed-in upload on
# a slow link is never timed.
_BODY_GAP_S = 10.0
_BODY_WHOLE_S = 30.0
# The webhook receivers bound their own read (``api/events/webhooks.py``)
# and answer a slow body in their own shape.
_SELF_TIMED_PREFIXES = ("/v1/webhooks/",)


def _times_body(path: str) -> bool:
    """Whether the middleware bounds the body read of ``path`` in time."""
    return not path.startswith(_SELF_TIMED_PREFIXES)


class _Exchange:
    """One request's state across the wrapped ``receive`` and ``send``."""

    __slots__ = ("scope", "send", "cookie", "started", "status", "answered", "cut", "body_open")

    def __init__(self, scope, send, cookie: str):
        self.scope = scope
        self.send = send
        self.cookie = cookie
        self.started = False
        self.status = 0
        self.answered = False   # the middleware answered itself: the app's messages are dropped
        self.cut = False        # the body cap or the body deadline cut the request
        # The request carries a body the app has not read to its end: an
        # answer that starts now closes the connection, or uvicorn would
        # go on reading the rest with no deadline.
        self.body_open = False

    async def forward(self, message, *, own: bool = False) -> None:
        if message["type"] == "http.response.start":
            self.started = True
            self.status = message["status"]
            headers = MutableHeaders(scope=message)
            _security_headers(headers, self.scope.get("path") or "")
            if self.body_open and "connection" not in headers:
                headers["connection"] = "close"
            if self.cookie and not own:
                try:
                    await _refresh_session_cookie(self.scope, headers, self.cookie)
                except Exception:
                    # The route's answer goes out as it is (a database
                    # outage, a timeout, shutdown).
                    logger.debug("session refresh skipped", exc_info=True)
        await self.send(message)

    async def app_send(self, message) -> None:
        if not self.answered:
            await self.forward(message)

    async def answer(self, status: int, body: bytes, *, close: bool = False) -> None:
        """The middleware's own JSON answer; the app's later messages are
        dropped. ``close``: uvicorn otherwise keeps the connection and
        drains the rest of a refused body with no timeout."""
        self.answered = True
        headers = [(b"content-type", b"application/json"),
                   (b"content-length", str(len(body)).encode())]
        if close:
            headers.append((b"connection", b"close"))
        await self.forward({"type": "http.response.start", "status": status, "headers": headers},
                           own=True)
        await self.forward({"type": "http.response.body", "body": body}, own=True)


class _BodyCap:
    """The request's body cap: the path's tier, provisionally narrowed to
    the unauthenticated tier until a credential is proven (checked at most
    once, and only when the body would pass the narrow cap or a body read
    outlasts the body deadline).

    A webhook route instead lifts its own cap once it has checked its
    sender (``lift`` reads what the route set, ``api/events/webhook_body``):
    a declared length is judged against the most any webhook route may
    lift to (``ceiling``), and the streamed bytes against what this one
    lifted to by the time they arrive."""

    __slots__ = ("cap", "full", "check", "credential", "received", "lift", "ceiling")

    def __init__(self, cap: int, full: int, check, *, lift=None, ceiling: int = 0):
        self.cap = cap
        self.full = full
        self.check = check
        self.credential = ""
        self.received = 0
        self.lift = lift
        self.ceiling = ceiling

    def proven(self) -> bool:
        """Whether the request carries a valid credential; a proven one
        lifts the narrow cap."""
        if not self.credential:
            self.credential = self.check()
            if self.credential == "valid":
                self.cap = self.full
        return self.credential == "valid"

    def exceeded(self, size: int) -> bool:
        if size <= self.cap:
            return False
        if self.lift is not None:
            self.cap = max(self.cap, self.lift())
            return size > self.cap
        if self.cap < self.full:
            self.proven()
        return size > self.cap

    def declared_exceeded(self, size: int) -> bool:
        """The early judgement of a declared length, before the route ran."""
        if self.lift is not None:
            return size > self.ceiling
        return self.exceeded(size)

    def refusal(self) -> tuple[int, bytes]:
        # A presented credential that does not verify (an expired cookie on
        # an upload) is a sign-in problem, not a size problem.
        if self.credential == "invalid":
            return 401, _UNAUTHENTICATED
        return 413, _TOO_LARGE


class _BodyDeadline:
    """The body deadline of one request (``_BODY_GAP_S``, ``_BODY_WHOLE_S``):
    armed until the body is complete, a wait runs out or the request proves
    a credential."""

    __slots__ = ("timed", "due")

    def __init__(self, timed: bool):
        self.timed = timed
        self.due = 0.0

    def wait_s(self) -> float:
        """The longest the next body read may wait; the whole-body clock
        starts at the first read."""
        now = asyncio.get_running_loop().time()
        if not self.due:
            self.due = now + _BODY_WHOLE_S
        return min(_BODY_GAP_S, max(self.due - now, 0.0))


class PlatformHttpMiddleware:
    """The platform's HTTP middleware (see the module docstring)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != _HTTP_SCOPE:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        raw_cookie = headers.get("cookie")
        cookies = cookie_parser(raw_cookie) if raw_cookie else {}
        x = _Exchange(scope, send, cookies.get("session", ""))
        path = scope.get("path") or ""
        logged = path.startswith(_LOGGED_PREFIXES)
        started_at = time.monotonic() if logged else 0.0
        try:
            await self._handle(scope, receive, x, headers, cookies)
        except Exception as exc:
            if x.cut:
                # The app saw the disconnect that followed the cut.
                logger.debug("request body cut: %s after the cut on %s", type(exc).__name__, path)
                return
            if not logged or x.started or db_unavailable(exc):
                raise
            client = scope.get("client")
            logger.error("DASH %s %s → 500 EXCEPTION (%.3fs) from %s: %s",
                         scope.get("method"), path, time.monotonic() - started_at,
                         client[0] if client else "?", exc, exc_info=True)
            await x.answer(500, _SERVER_ERROR)
            return
        if logged and logger.isEnabledFor(logging.DEBUG):
            client = scope.get("client")
            logger.debug("DASH %s %s → %s (%.3fs) from %s", scope.get("method"), path,
                         x.status, time.monotonic() - started_at, client[0] if client else "?")

    async def _handle(self, scope, receive, x: _Exchange, headers: Headers, cookies: dict):
        request = Request(scope)
        for judge in (_render_refusal, _session_refusal, _service_key_refusal, _origin_refusal):
            refusal = judge(request)
            if refusal is not None:
                x.answered = True
                await refusal(scope, receive, lambda m: x.forward(m, own=True))
                return

        routed = scope_routed_path(scope)
        full, narrowable = body_cap(scope.get("method", ""), routed)
        unauth = config.MAX_UNAUTH_BODY_BYTES
        lift, ceiling = None, 0
        if routed.startswith("/v1/webhooks/"):
            from api.events import webhook_body
            ceiling = min(_backstop(), max(full, webhook_body.ceiling()))
            lift = lambda: min(ceiling, scope.get(webhook_body.SCOPE_KEY) or 0)  # noqa: E731
        cap = _BodyCap(
            min(unauth, full) if narrowable and unauth > 0 else full, full,
            lambda: credential_state(request, cookies, routed),
            lift=lift, ceiling=ceiling,
        )

        declared = headers.get("content-length")
        if declared is not None:
            try:
                size = int(declared)
            except ValueError:
                size = -1
            if cap.declared_exceeded(size):
                await x.answer(*cap.refusal(), close=True)
                return
            x.body_open = size != 0
        else:
            x.body_open = "chunked" in (headers.get("transfer-encoding") or "").lower()

        deadline = _BodyDeadline(_times_body(routed))

        async def timed_receive():
            if not deadline.timed:
                return await receive()
            try:
                async with asyncio.timeout(deadline.wait_s()):
                    return await receive()
            except TimeoutError:
                deadline.timed = False
                if cap.proven():
                    return await receive()
                x.cut = True
                if not x.started:
                    await x.answer(408, _BODY_TIMEOUT, close=True)
                return {"type": "http.disconnect"}

        async def capped_receive():
            if x.cut:
                return {"type": "http.disconnect"}
            message = await timed_receive()
            if message["type"] != "http.request" or not message.get("more_body", False):
                # The body is complete: a later read waits for the
                # disconnect, which is never timed.
                deadline.timed = False
                x.body_open = False
            if message["type"] == "http.request":
                cap.received += len(message.get("body", b""))
                if cap.exceeded(cap.received):
                    x.cut = True
                    scope["otodock.body_cut"] = True
                    if not x.started:
                        await x.answer(*cap.refusal(), close=True)
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, capped_receive, x.app_send)


def register_middlewares(app):
    """Attach the platform middleware. ``app.install_db_unavailable_guards``
    runs after this and stays outermost."""
    app.add_middleware(PlatformHttpMiddleware)

"""HTTP middleware stack, registered by ``register_middlewares(app)``.

Extracted from ``app.py``. Registration order is preserved: Starlette inserts
each new middleware at the top of the stack, so calling these in source order
reproduces the exact processing order the inline ``@app.middleware`` decorators
had (outermost first: refresh_session_cookie -> ... -> security_headers).
"""

import logging
import time

from fastapi import Request

import config

logger = logging.getLogger("claude-proxy")


# Static-asset extensions whose responses must stay cacheable — never attach a
# Set-Cookie to them in the sliding-session refresh below.
_STATIC_EXTS = {
    "js", "css", "map", "png", "jpg", "jpeg", "svg", "gif", "ico",
    "woff", "woff2", "ttf", "webp", "json", "txt",
}


async def security_headers(request: Request, call_next):
    """Attach defensive response headers.

    ``nosniff`` + ``Referrer-Policy`` go on everything; HSTS only when the
    deployment is HTTPS (``COOKIE_SECURE``). Clickjacking protection
    (``X-Frame-Options`` + CSP ``frame-ancestors``) is applied everywhere
    EXCEPT ``/collabora/*`` — the dashboard embeds the Collabora editor in an
    iframe, so framing that subtree must stay allowed. ``setdefault`` so a
    handler that already chose a value (e.g. a file response's own nosniff)
    isn't clobbered.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if config.COOKIE_SECURE:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains",
        )
    if not request.url.path.startswith("/collabora/"):
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    return response


async def limit_request_body_size(request: Request, call_next):
    """Backstop cap on request body size via ``Content-Length`` → 413.

    A declared length over ``config.MAX_REQUEST_BODY_BYTES`` is rejected before
    the body is read, so an abusive oversized payload can't exhaust memory.
    Per-endpoint caps (uploads, WOPI, MCP zip) still apply underneath.
    """
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > config.MAX_REQUEST_BODY_BYTES:
                from starlette.responses import JSONResponse
                return JSONResponse({"detail": "Request body too large"}, status_code=413)
        except ValueError:
            pass
    return await call_next(request)


async def service_key_confinement(request: Request, call_next):
    """Confine the master ``PROXY_API_KEY`` to its service-to-service allowlist.

    A request presenting the master key on a non-allowlisted endpoint is
    rejected (403), so a leaked key cannot drive arbitrary user/admin routes.
    See ``auth/service_endpoints.py`` for the allowlist + contributor contract.
    """
    from auth.service_endpoints import (
        extract_master_key,
        is_service_endpoint_allowed,
    )
    if extract_master_key(request) is not None:
        if not is_service_endpoint_allowed(request.method, request.url.path):
            logger.warning(
                "Master key blocked from non-S2S endpoint: %s %s",
                request.method, request.url.path,
            )
            from starlette.responses import JSONResponse
            return JSONResponse(
                {"detail": "This endpoint is not available to the service key"},
                status_code=403,
            )
    return await call_next(request)


async def log_dashboard_requests(request: Request, call_next):
    """Log dashboard/task API requests with timing (DEBUG; errors at ERROR).

    The per-request success line duplicates uvicorn's access log, so it sits
    at DEBUG — enable it when chasing a hang; the timing is the point.
    """
    path = request.url.path
    if path.startswith(("/dashboard", "/v1/tasks", "/v1/schedules", "/v1/triggers",
                        "/v1/agents", "/v1/admin", "/v1/chats", "/auth/")):
        client = request.client.host if request.client else "?"
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.error(
                f"DASH {request.method} {path} → 500 EXCEPTION "
                f"({elapsed:.3f}s) from {client}: {exc}",
                exc_info=True,
            )
            from starlette.responses import JSONResponse
            return JSONResponse({"detail": "Internal Server Error"}, status_code=500)
        elapsed = time.monotonic() - start
        logger.debug(
            f"DASH {request.method} {path} → {response.status_code} "
            f"({elapsed:.3f}s) from {client}"
        )
        return response
    return await call_next(request)


async def refresh_session_cookie(request: Request, call_next):
    """Sliding session: re-issue the ``session`` cookie on activity once it is
    past the halfway point of its lifetime, so an actively-used session never
    expires — the configured duration becomes a max-INACTIVITY window rather
    than a hard cap from login.

    Skips re-issue when: there is no session cookie (bearer / API-key / agent
    session / anonymous); the path is a cacheable static asset; the handler
    already set OR deleted a ``session`` cookie this response (login / 2FA / SSO
    re-issue, and crucially logout's delete — we must never resurrect a
    just-logged-out session); the cookie is invalid/expired (force a re-login);
    or the cookie is still in the first half of its life (avoids a Set-Cookie on
    every response). Re-mint preserves identity from the decoded token; authz is
    unaffected because get_current_user resolves role/identity from the DB live.
    """
    cookie = request.cookies.get("session")
    response = await call_next(request)
    if not cookie:
        return response
    path = request.url.path
    if path.startswith("/assets/") or path.rsplit(".", 1)[-1].lower() in _STATIC_EXTS:
        return response
    # The handler owns the session cookie this round — leave it authoritative.
    if any(sc.startswith("session=") for sc in response.headers.getlist("set-cookie")):
        return response
    from auth.providers import (
        validate_session_jwt, create_session_jwt, apply_session_cookie,
        session_iat_after_password_change,
    )
    payload = validate_session_jwt(cookie)
    if not payload:
        return response
    iat, exp, now = payload.get("iat"), payload.get("exp"), int(time.time())
    if (isinstance(iat, int) and isinstance(exp, int) and exp > iat
            and (now - iat) < (exp - iat) / 2):
        return response  # still fresh — no re-issue yet
    # Never LAUNDER a cookie that predates the user's last password change into
    # a fresh-iat one — that would make a stolen pre-change cookie immortal.
    # (This runs even on 401 responses, so the check is load-bearing here.)
    from storage import database as _task_store
    _u = _task_store.get_user(payload.get("sub", ""))
    if _u and not session_iat_after_password_change(_u, payload):
        return response
    token = create_session_jwt(
        payload["sub"], payload.get("email", ""), payload.get("name", ""),
        payload.get("role", "member"),
        auth_provider=payload.get("auth_provider", "local"),
    )
    apply_session_cookie(response, token)
    return response


def register_middlewares(app):
    """Attach the HTTP middleware stack in the original order."""
    app.middleware("http")(security_headers)
    app.middleware("http")(limit_request_body_size)
    app.middleware("http")(service_key_confinement)
    app.middleware("http")(log_dashboard_requests)
    app.middleware("http")(refresh_session_cookie)

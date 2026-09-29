"""The render principal (APPS.md "Deploy pipeline", "Security model").

The platform's own headless render (file-tools' ``/render``) loads an app
through the real dashboard page, so it needs to be somebody: a cookie
principal that the app's routes and the shell that shows it accept, and
nothing else does. It is a JWT of purpose ``app_render`` in its own cookie
(``otodock_render``, so the six readers of the ``session`` cookie never see
it), minted by the render job alone for one app, alive for two minutes and
only while its ``jti`` is in the in-process live set the job empties when
it ends: a token that leaks from a log is dead with the job. It is accepted
only on a request that carries no session cookie and no bearer, from a peer
that is not a trusted proxy and without ``X-Forwarded-For``: the renderer
talks to the proxy directly, never through the gateway.

Who it is: for a shared app a synthetic viewer of the app's agent (feeds
answer the agent-scope slice, never a person's inbox); for a personal app
the owner. What it may reach is the allowlist below, judged by the
``render_confinement`` middleware (``middleware.py``) with a 403 and never
a 401, which the dashboard would turn into a page redirect.
"""

from __future__ import annotations

import logging
import re
import secrets
import time

import jwt

import config
from auth.request_path import has_traversal
from auth import roles

logger = logging.getLogger("claude-proxy.auth")

COOKIE_NAME = "otodock_render"
PURPOSE = "app_render"
TTL_S = 120
SUB_PREFIX = "render:"
BLOCKED_DETAIL = "This endpoint is not available to a render"

# jti → (app id, exp). In this process only: a render is seconds long and
# never survives a restart.
_live: dict[str, tuple[str, float]] = {}

_APP = r"[0-9a-f-]{36}"
# (method, pattern with {app}) — anchored, bound to the token's app.
_ALLOW: list[tuple[str, str]] = [
    ("GET", r"/auth/me"),
    ("GET", r"/auth/config"),
    ("GET", r"/health"),
    ("GET", r"/v1/apps/{app}"),
    ("GET", r"/v1/apps/{app}/state"),
    ("GET", r"/v1/apps/{app}/html"),
    ("GET", r"/v1/apps/{app}/client/[0-9a-f]{64}/.*"),
    ("GET", r"/v1/apps/{app}/deploy/status"),
    ("POST", r"/v1/apps/{app}/viewer-token"),
    ("GET", r"/v1/apps/{app}/catalog/[a-z_]+"),
    ("POST", r"/v1/apps/{app}/catalog/(viewer\.me|integrations\.status|files\.list|files\.read|tasks\.run_result)"),
    ("GET", r"/v1/users/me/audio-prefs"),
    ("GET", r"/v1/audio/capability"),
]
# Only these prefixes are judged; the SPA shell and its static files are
# public by construction and the app's own API is bearer-authed.
_JUDGED_PREFIXES = ("/v1/", "/auth/", "/ws/", "/api/")


def synthetic_sub(app_id: str) -> str:
    return f"{SUB_PREFIX}{app_id}"


def mint(app_id: str, sub: str, agent: str) -> tuple[str, str]:
    """A live render token for ``app_id`` as ``sub``; returns (token, jti).
    The caller releases the jti when its render ends."""
    now = int(time.time())
    jti = secrets.token_urlsafe(16)
    payload = {"purpose": PURPOSE, "sub": sub, "app": app_id, "agent": agent,
               "jti": jti, "iat": now, "exp": now + TTL_S}
    _live[jti] = (app_id, float(now + TTL_S))
    _sweep(now)
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256"), jti


def release(jti: str) -> None:
    _live.pop(jti, None)


def _sweep(now: float) -> None:
    for k in [k for k, (_a, exp) in _live.items() if exp < now]:
        _live.pop(k, None)


def verify(token: str) -> dict | None:
    """The claims iff the token is a live render token; None otherwise."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    if payload.get("purpose") != PURPOSE:
        return None
    jti = str(payload.get("jti") or "")
    live = _live.get(jti)
    if not live or live[0] != payload.get("app") or live[1] < time.time():
        return None
    return payload


def is_allowed(method: str, path: str, app_id: str) -> bool:
    """True if a render principal for ``app_id`` may call ``method path``."""
    base = path.split("?", 1)[0]
    if not base.startswith(_JUDGED_PREFIXES):
        return True
    if has_traversal(base):
        return False
    if not re.fullmatch(_APP, app_id or ""):
        return False
    for m, pattern in _ALLOW:
        if m == method.upper() and re.fullmatch(pattern.replace("{app}", app_id), base):
            return True
    return False


def direct_peer(request) -> bool:
    """The renderer reaches the proxy directly: a request through a trusted
    proxy, or carrying X-Forwarded-For at all, is not it."""
    if request.headers.get("x-forwarded-for"):
        return False
    peer = request.client.host if request.client else ""
    if config.TRUSTED_PROXIES:
        from auth.lan_check import _ip_in_trusted
        if peer and _ip_in_trusted(peer):
            return False
    return True


async def principal_from_cookie(request, token: str):
    """The ``UserContext`` a live render cookie names, or None. Only on a
    request with no session cookie and no Authorization header: the same
    condition ``middleware.render_confinement`` judges on, so a principal
    from this cookie never skips the allowlist (a junk bearer next to it
    used to)."""
    from auth.providers import UserContext, _load_user
    from storage.pg import run_db
    if request.headers.get("authorization") or request.cookies.get("session"):
        return None
    if not direct_peer(request):
        logger.info("Render cookie refused: not a direct peer (%s)", request.url.path)
        return None
    claims = verify(token)
    if claims is None:
        return None
    sub = str(claims.get("sub") or "")
    app_id = str(claims.get("app") or "")
    agent = str(claims.get("agent") or "")
    jti = str(claims.get("jti") or "")
    if sub.startswith(SUB_PREFIX):
        return UserContext(
            sub=sub, email="render@internal", name="render", role=roles.MEMBER,
            agents=[agent] if agent else [], display_name="render",
            agent_roles={agent: roles.VIEWER} if agent else {}, auth_provider="render",
            render_app=app_id, render_jti=jti,
        )
    user, agent_roles, _default = await run_db(_load_user, sub, with_default=False)
    if not user:
        return None
    return UserContext(
        sub=user["sub"], email=user["email"], name=user["name"], role=user["role"],
        agents=list(agent_roles.keys()), display_name=user.get("display_name", ""),
        agent_roles=agent_roles, auth_provider="render", is_owner=bool(user.get("is_owner")),
        render_app=app_id, render_jti=jti,
    )

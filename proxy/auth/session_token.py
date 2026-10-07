"""Session-scoped JWT tokens for agent subprocess authentication.

Replaces the master PROXY_API_KEY in agent subprocess environments.
Agents get a short-lived, session-scoped token instead of the platform key.
The proxy accepts both master key (service-to-service) and session tokens.
"""

import threading
import time
from collections import OrderedDict

import jwt


# Sentinel Authorization bearer for Docker MCPs that call back to the proxy
# (manifest ``server.proxy_callbacks``). The per-session MCP config carries this
# placeholder at BUILD time (``build_session_mcp_config``); each per-layer
# ``?session_id=`` injection site swaps it for a real, session-scoped JWT once
# the session_id is known. Mirrors ``core.credentials.mcp_broker.BROKER_BEARER_PLACEHOLDER``
# (vendor bearers) — a Docker MCP container is shared across sessions, so it
# can't hold a session-scoped env token the way stdio MCPs do; the per-request
# header is its equivalent.
SESSION_JWT_PLACEHOLDER = "OTO_SESSION_JWT"
SESSION_JWT_SENTINEL_BEARER = f"Bearer {SESSION_JWT_PLACEHOLDER}"


# The person each session's tokens are minted for, so the Docker MCP callback
# bearer (swapped in by the layer, which has no person at hand) carries the
# same user as the session's own token: minted without one it would resolve
# to the no-user agent principal and outlive the person's deletion or
# password change. The session's own token is minted with its config, before
# any swap site runs. The newest mint that names a person wins (a Shared-only
# chat changes hands; the sandbox env's own mint names no one there);
# bounded, the oldest sessions fall out first.
_SESSION_USERS: OrderedDict[str, str] = OrderedDict()
_SESSION_USERS_MAX = 4096
_session_users_lock = threading.Lock()


def _remember_session_user(session_id: str, user_sub: str) -> None:
    with _session_users_lock:
        _SESSION_USERS[session_id] = user_sub
        _SESSION_USERS.move_to_end(session_id)
        while len(_SESSION_USERS) > _SESSION_USERS_MAX:
            _SESSION_USERS.popitem(last=False)


def _session_user(session_id: str) -> str:
    with _session_users_lock:
        return _SESSION_USERS.get(session_id, "")


def swap_session_jwt_bearer(
    auth_value: str, session_id: str, agent_name: str, user_sub: str = ""
) -> str | None:
    """If ``auth_value`` is the session-JWT sentinel bearer, return the real
    ``Bearer <jwt>`` minted for this session (for the person its own token
    names, unless ``user_sub`` says otherwise); else return ``None`` (caller
    leaves the header untouched — e.g. a real vendor bearer).
    """
    if auth_value != SESSION_JWT_SENTINEL_BEARER:
        return None
    user_sub = user_sub or _session_user(session_id)
    return f"Bearer {create_session_token(session_id, agent_name, user_sub)}"


def _live_external_claim(session_id: str) -> str:
    """The ``ext`` claim of the session's registered SecurityContext ("" when
    the session is not external, or not registered yet)."""
    if not session_id:
        return ""
    try:
        from core.session.session_state import get_session_security
    except ImportError:  # pragma: no cover — import-order safety only
        return ""
    ctx = get_session_security(session_id)
    return getattr(ctx, "external_claim", "") or ""


def create_session_token(
    session_id: str,
    agent_name: str,
    user_sub: str = "",
    *,
    external: str | None = None,
    issued_at: int | None = None,
) -> str:
    """Generate a JWT scoped to one agent session.

    Token is valid for 24h (sessions rarely last longer; reaped at 15min idle).
    It carries ``iat``: a token minted before its user's last password
    change is refused (``auth/providers``), and one minted before its
    session's floor (``session_state.register_session_state``) too; a
    builder that records the floor it mints against passes ``issued_at``
    so the two are the same instant.

    Args:
        session_id: chat / task / phone session id.
        agent_name: agent slug.
        user_sub: optional user_sub of the session's owner. When present,
            the auth path uses it to resolve the calling user back to a
            real ``users`` row (so API-call attribution — e.g. the
            ``requested_by`` column on ``mcp_assignment_requests`` —
            picks up the real identity instead of a synthetic
            ``session:<sid>`` string). Empty for agent-scope sessions
            with no real owner (phone service, triggers service, etc.).
        external: the ``ext`` claim for a session on an EXTERNAL route
            (``core/session/external_identity.py``). ``None`` (the default
            at every mint site) derives it from the session's registered
            SecurityContext — the layers register the context BEFORE the
            spawn precisely so the tokens minted into the process env carry
            it; "" mints a plain token. Tokens carrying ``ext`` are accepted
            only while the session is live (``middleware.py``).
    """
    import config
    if external is None:
        external = _live_external_claim(session_id)
    if session_id and user_sub:
        _remember_session_user(session_id, user_sub)
    now = int(time.time()) if issued_at is None else int(issued_at)
    payload = {
        "type": "session",
        "sid": session_id,
        "agent": agent_name,
        "user_sub": user_sub,
        "iat": now,
        "exp": now + 24 * 3600,
    }
    if external:
        payload["ext"] = external
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def validate_session_token(token: str) -> dict | None:
    """Validate a session JWT. Returns the payload dict or None."""
    import config
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
        if payload.get("type") == "session":
            return payload
    except (jwt.InvalidTokenError, jwt.ExpiredSignatureError):
        pass
    return None

"""Session-scoped JWT tokens for agent subprocess authentication.

Replaces the master PROXY_API_KEY in agent subprocess environments.
Agents get a short-lived, session-scoped token instead of the platform key.
The proxy accepts both master key (service-to-service) and session tokens.
"""

from datetime import datetime, timedelta, timezone

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


def swap_session_jwt_bearer(
    auth_value: str, session_id: str, agent_name: str, user_sub: str = ""
) -> str | None:
    """If ``auth_value`` is the session-JWT sentinel bearer, return the real
    ``Bearer <jwt>`` minted for this session; else return ``None`` (caller
    leaves the header untouched — e.g. a real vendor bearer).
    """
    if auth_value != SESSION_JWT_SENTINEL_BEARER:
        return None
    return f"Bearer {create_session_token(session_id, agent_name, user_sub)}"


def create_session_token(
    session_id: str,
    agent_name: str,
    user_sub: str = "",
) -> str:
    """Generate a JWT scoped to one agent session.

    Token is valid for 24h (sessions rarely last longer; reaped at 15min idle).

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
    """
    import config
    payload = {
        "type": "session",
        "sid": session_id,
        "agent": agent_name,
        "user_sub": user_sub,
        "exp": datetime.now(timezone.utc) + timedelta(hours=24),
    }
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

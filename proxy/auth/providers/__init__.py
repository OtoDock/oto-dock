"""OAuth2 + JWT authentication and role-based access control.

Provides:
  - OAuth CSRF state management for the platform login flow
  - JWT session tokens (HttpOnly cookies)
  - UserContext dataclass for per-request user info
  - Unified auth dependency: API key OR session cookie
  - Permission helpers for role-based endpoint gating
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

import jwt
from fastapi import HTTPException, Request

import config
from storage import database as task_store

logger = logging.getLogger("claude-proxy.auth")

# In-memory CSRF state store: state -> metadata dict
# ({"expiry": monotonic timestamp, "redirect_uri": str | None})
_oauth_states: dict[str, dict] = {}
_STATE_TTL = 300  # 5 minutes

# Synthetic ``sub`` prefix for a session-JWT caller that carried NO real
# user_sub (agent-scope / phone / trigger / meeting service session with no
# human owner). The single source of truth for ``UserContext.is_no_user_session``
# — used both where the synthetic sub is built (``get_current_user``) and where
# it is tested, so the two can never drift.
SESSION_SUB_PREFIX = "session:"


class PrincipalKind(str, Enum):
    """How a request authenticated — its security principal.

    COOKIE        — a human in the dashboard (session cookie).
    SERVICE       — the master PROXY_API_KEY: service-to-service callers only
                    (phone server, standalone scheduler). Confined to the S2S
                    endpoint allowlist; never reaches user/admin web routes.
    USER_SESSION  — an agent subprocess acting for a real user (its session JWT
                    carried that user's sub). Has the user's REAL role — never
                    inflated to owner/admin.
    AGENT_SESSION — an agent subprocess with no human owner (phone call,
                    trigger, scheduled agent-scope task, meeting service). A
                    low-privilege principal: it may act on the single agent it
                    was minted for, but is neither admin nor a manager.
    """

    COOKIE = "cookie"
    SERVICE = "service"
    USER_SESSION = "user_session"
    AGENT_SESSION = "agent_session"


# --- UserContext ---


@dataclass
class UserContext:
    sub: str
    email: str
    name: str
    role: str  # "admin" | "creator" | "member" (platform-level role)
    agents: list[str] = field(default_factory=list)
    default_agent: str = ""
    display_name: str = ""
    is_api_key: bool = False  # True when authenticated via API key
    agent_roles: dict[str, str] = field(default_factory=dict)  # {agent: "manager"|"editor"|"viewer"}
    auth_provider: str = "local"  # "local" | "oidc:authentik" | "oidc:authelia" etc.
    is_owner: bool = False  # True for the first admin created during setup
    # Session JWT id (``sid`` claim). Populated only when the caller
    # authenticated via a session-scoped JWT (agent subprocesses). Empty
    # for master API key callers and dashboard cookie sessions.
    session_id: str = ""
    # Agent slug from the session token's ``agent`` claim. Populated only for
    # session-JWT callers (agent subprocesses); empty for master key and
    # dashboard cookie sessions.
    agent: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def kind(self) -> PrincipalKind:
        """The security principal, derived from how the caller authenticated.
        ``is_api_key`` records the low-level fact (token vs. cookie); ``kind``
        is the security-relevant classification built from it.
        """
        if not self.is_api_key:
            return PrincipalKind.COOKIE
        if self.sub == "api-key":
            return PrincipalKind.SERVICE
        if self.sub.startswith(SESSION_SUB_PREFIX):
            return PrincipalKind.AGENT_SESSION
        return PrincipalKind.USER_SESSION

    @property
    def is_service(self) -> bool:
        """The trusted master key (service-to-service). Allowlist-confined."""
        return self.kind == PrincipalKind.SERVICE

    @property
    def is_session(self) -> bool:
        """An agent-subprocess session token (interactive or agent-scope)."""
        return self.kind in (
            PrincipalKind.USER_SESSION,
            PrincipalKind.AGENT_SESSION,
        )

    @property
    def is_no_user_session(self) -> bool:
        """True for a session-JWT caller whose token carried NO real
        ``user_sub`` — i.e. an agent-scope / phone / trigger / meeting service
        session with no human owner (``sub == "session:<sid>"``).

        Such a caller is trusted for service-to-service plumbing but MUST NOT
        be allowed to assert a *user* identity — it cannot create user-scoped
        tasks / notifications / triggers, and any ``created_by`` / on-behalf it
        supplies is ignored. This is the structural fix for the identity-bleed
        bug: a phone/agent session can no longer act as a real user.

        Distinguished from: the master key (``sub == "api-key"``, not session-
        prefixed → full s2s access), a real-user-backed session token
        (``sub`` is the real user_sub → legitimate identity), and a dashboard
        cookie (``is_api_key`` False).
        """
        return self.kind == PrincipalKind.AGENT_SESSION

    @property
    def acting_sub(self) -> str | None:
        """The real user this caller acts as — derived SOLELY from the
        token/cookie, NEVER from a client header. Returns the real user_sub
        for a dashboard cookie or a real-user-backed session token; None for
        the master key (service-to-service) and for a no-user session
        (phone / agent / trigger / meeting service with no human owner).

        This is the single seam through which identity enters server-side
        attribution + permission checks across tasks / notifications /
        triggers — which is what closes the identity-bleed bug.
        """
        if self.sub == "api-key":
            return None  # master key — s2s, no user identity
        if self.is_no_user_session:
            return None  # service session — no human owner
        return self.sub  # dashboard cookie OR real-user-backed session token

    def can_access_agent(self, name: str) -> bool:
        """Can this caller access this agent at all? Admins (incl. the trusted
        master key) see all; a human sees their assigned agents; a session
        (interactive or agent-scope) may act on the single agent it was minted
        for — even when it carries no per-agent role.
        """
        return (
            self.is_admin
            or name in self.agents
            or (self.is_session and bool(self.agent) and name == self.agent)
        )

    def get_agent_role(self, agent: str) -> str:
        """Effective role for a specific agent."""
        if self.is_admin:
            return "admin"
        return self.agent_roles.get(agent, "viewer")

    def can_manage_agent(self, agent: str) -> bool:
        """Owner-tier check: can this user CHANGE this agent's behavior
        (config, MCP assignments, delegation, service-account bindings,
        memory consolidation)? True for admin + per-agent manager. Editors are
        explicitly NOT included — editor is the collaborative workspace tier,
        not the owner tier. A session token (interactive or agent-scope) gets
        NO blanket bypass here: it resolves the real per-agent role, so a
        prompt-injected session cannot manage an agent it doesn't own.
        """
        if self.is_admin:
            return True
        return self.agent_roles.get(agent) == "manager"

    def can_edit_agent(self, agent: str) -> bool:
        """Editor-tier check: can this user WRITE to this agent's
        workspace + create their own agent-scope tasks / notifications /
        triggers? True for admin + per-agent manager + per-agent editor.
        Viewers are excluded — they're read-only collaborators.

        Editor is the workspace-collaboration tier; manager is the
        owner tier. The two tiers compose: every manager is also an
        editor.
        """
        if self.is_admin:
            return True
        return self.agent_roles.get(agent) in ("manager", "editor")

    def can_write_files(self) -> bool:
        """Platform-level: is this user a creator/admin for ANY agent?"""
        return self.role in ("admin", "creator")

    def can_manage_tasks(self) -> bool:
        """Platform-level: is this user a creator/admin? Used for platform-level checks."""
        return self.role in ("admin", "creator")


# --- CSRF state ---


def create_oauth_state(redirect_uri: str | None = None) -> str:
    """Generate a random state parameter and store it with a TTL."""
    import secrets
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {
        "expiry": time.monotonic() + _STATE_TTL,
        "redirect_uri": redirect_uri,
    }
    # Purge expired states
    now = time.monotonic()
    expired = [k for k, v in _oauth_states.items() if v["expiry"] < now]
    for k in expired:
        _oauth_states.pop(k, None)
    return state


def validate_oauth_state(state: str) -> dict | None:
    """Check and consume a state parameter. Returns metadata dict or None."""
    meta = _oauth_states.pop(state, None)
    if meta is None:
        return None
    if time.monotonic() > meta["expiry"]:
        return None
    return meta


# --- JWT ---

# OAuth state-token management for the MCP credential flow lives in
# ``services/oauth_engine`` (``create_state`` / ``validate_state``);
# this module owns only the platform-auth state (Authentik etc.).


def create_session_jwt(sub: str, email: str, name: str, role: str,
                       auth_provider: str = "local") -> str:
    """Create an HS256 JWT for the dashboard session cookie."""
    payload = {
        # Discriminator: marks this as a dashboard session cookie. Required by
        # validate_session_jwt so that OTHER JWTs signed with the same
        # JWT_SECRET (the 2FA step token, the password-reset token) can never be
        # replayed as a session cookie. NEVER reuse "session" for another token.
        "purpose": "session",
        "sub": sub,
        "email": email,
        "name": name,
        "role": role,
        "auth_provider": auth_provider,
        "iat": int(time.time()),
        "exp": int(time.time()) + config.get_jwt_expiry_hours() * 3600,
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def apply_session_cookie(response, token: str) -> None:
    """Set the HttpOnly session cookie with the canonical attributes.

    Single source of truth for the cookie's shape — used by login
    (``api/auth/identity._issue_session_cookie``) and the sliding-refresh
    middleware (``middleware.refresh_session_cookie``), so the two can never
    drift. ``max_age``
    tracks the configured (possibly operator-forced) login-session duration.
    """
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="lax",
        path="/",
        max_age=config.get_jwt_expiry_hours() * 3600,
    )


def validate_session_jwt(token: str) -> dict | None:
    """Decode and validate a dashboard session-cookie JWT. Returns payload or None.

    Enforces ``purpose == "session"``: the 2FA step token (``purpose="2fa"``,
    handed to the client *before* the second factor) and the password-reset
    token (``purpose="password_reset"``) are signed with the SAME ``JWT_SECRET``,
    so without this check either could be presented as the ``session`` cookie for
    a full authenticated session (2FA / reset bypass). Bearer session tokens are
    a separate scheme (``type="session"``) validated by
    ``auth.session_token.validate_session_token``.
    """
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        logger.debug("JWT expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.debug(f"JWT invalid: {e}")
        return None
    if payload.get("purpose") != "session":
        logger.debug("JWT rejected: not a session cookie (purpose=%r)", payload.get("purpose"))
        return None
    return payload


# --- Unified auth dependency ---


async def get_current_user(request: Request) -> UserContext | None:
    """Extract user from API key header OR session cookie.

    Returns UserContext or None (caller decides whether to 401).
    """
    req = request

    # 1. API key / session token header → synthetic admin UserContext
    auth_header = req.headers.get("authorization", "")
    if auth_header:
        parts = auth_header.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
            # Master API key (Docker MCPs, standalone scheduler, phone)
            if config.is_master_key(token):
                return UserContext(
                    sub="api-key",
                    email="api@internal",
                    name="API Key",
                    role="admin",
                    agents=[],
                    is_api_key=True,
                )
            # Session-scoped JWT (agent subprocesses: schedules-mcp, notifications, etc.)
            from auth.session_token import validate_session_token
            session_payload = validate_session_token(token)
            if session_payload:
                # If the token was minted with a real user_sub, resolve it
                # back to the actual users row so API-call attribution
                # (e.g. mcp_assignment_requests.requested_by) records the
                # real identity rather than a synthetic session string.
                # Falls back to the legacy synthetic placeholder for old
                # tokens minted before this contract bump and for
                # agent-scope sessions with no human owner.
                sid = session_payload.get("sid") or ""
                agent_name = session_payload.get("agent") or ""
                user_sub = session_payload.get("user_sub") or ""
                if user_sub:
                    user = task_store.get_user(user_sub)
                    if user:
                        agent_roles = task_store.get_user_agent_roles(user_sub)
                        return UserContext(
                            sub=user["sub"],
                            email=user["email"],
                            name=user["name"],
                            role=user["role"],
                            agents=list(agent_roles.keys()),
                            display_name=user.get("display_name", ""),
                            agent_roles=agent_roles,
                            is_owner=bool(user.get("is_owner")),
                            is_api_key=True,
                            session_id=sid,
                            agent=agent_name,
                        )
                # No-user session (phone / trigger / scheduled agent-scope /
                # meeting service): a low-privilege agent principal. NOT admin —
                # it can act on its own agent (see can_access_agent) and create
                # agent-scope work, but cannot manage agents or reach admin
                # routes. Its lack of a real user is enforced via acting_sub /
                # is_no_user_session, not via role.
                return UserContext(
                    sub=f"{SESSION_SUB_PREFIX}{sid}",
                    email="session@internal",
                    name="Session Token",
                    role="agent",
                    agents=[],
                    is_api_key=True,
                    session_id=sid,
                    agent=agent_name,
                )

    # 2. Session cookie → decode JWT → fetch user from DB
    session_cookie = req.cookies.get("session")
    if session_cookie:
        payload = validate_session_jwt(session_cookie)
        if payload:
            sub = payload["sub"]
            user = task_store.get_user(sub)
            if user:
                agent_roles = task_store.get_user_agent_roles(sub)
                default_agent = task_store.get_user_default_agent(sub) or ""
                # auth_provider: prefer DB value, then JWT claim, else "local"
                auth_prov = user.get("auth_provider") or payload.get("auth_provider", "local")
                return UserContext(
                    sub=user["sub"],
                    email=user["email"],
                    name=user["name"],
                    role=user["role"],
                    agents=list(agent_roles.keys()),
                    default_agent=default_agent,
                    display_name=user.get("display_name", ""),
                    agent_roles=agent_roles,
                    auth_provider=auth_prov,
                    is_owner=bool(user.get("is_owner")),
                )

    return None


# --- Permission helpers ---


def require_auth(user: UserContext | None) -> UserContext:
    """Raise 401 if user is None."""
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_admin(user: UserContext | None) -> UserContext:
    """Raise 401/403 if not authenticated or not admin."""
    u = require_auth(user)
    if not u.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return u


def require_agent_access(user: UserContext, agent_name: str) -> None:
    """Raise 403 if user cannot access the given agent."""
    if not user.can_access_agent(agent_name):
        raise HTTPException(
            status_code=403,
            detail=f"Access denied for agent '{agent_name}'",
        )


def require_write(user: UserContext, agent: str | None = None) -> None:
    """Raise 403 if user lacks OWNER-tier access.

    Owner-tier = admin + api_key + per-agent manager. Editors are NOT
    sufficient — this helper gates config-level writes (agent prompt,
    MCP wiring, knowledge folder, service-account bindings, delegation
    targets). For workspace-level writes, use ``require_edit`` instead.

    When agent is provided, checks per-agent role (manager required).
    When agent is None, checks platform-level role (admin or creator).
    """
    if agent:
        if not user.can_manage_agent(agent):
            raise HTTPException(status_code=403, detail="Manager access required for this agent")
    else:
        if not user.can_write_files():
            raise HTTPException(status_code=403, detail="Write access denied (member role)")


def require_edit(user: UserContext, agent: str) -> None:
    """Raise 403 if user lacks EDITOR-tier access for this agent.

    Editor-tier = admin + api_key + per-agent manager + per-agent
    editor. Viewers are excluded. Used by workspace file CRUD endpoints,
    agent-scope task/notification/trigger creation, and other
    collaborative-workspace actions that don't change agent BEHAVIOR.

    Config-level writes (prompt, MCP wiring, knowledge curation, service-
    account binding, delegation) must use ``require_write`` instead.
    """
    if not user.can_edit_agent(agent):
        raise HTTPException(status_code=403, detail="Editor access required for this agent")


def require_editor_or_manager(user: UserContext, agent: str) -> None:
    """Semantic alias for ``require_edit`` — readable at call sites that
    grant agent-scope create rights to the editor + manager tiers.

    Identical behavior to ``require_edit``. Kept distinct purely for code
    readability where the intent ("editor OR manager can do this") is
    clearer than the abstract tier name.
    """
    if not user.can_edit_agent(agent):
        raise HTTPException(status_code=403, detail="Editor or manager access required for this agent")


def mask_email(email: str) -> str:
    """Redact an email address for logs: keep the first local-part char + the
    domain (``b***@example.com``). PII-reducing while keeping audit logs useful.
    Returns ``"***"`` for empty/malformed input."""
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"

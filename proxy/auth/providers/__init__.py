"""OAuth2 + JWT authentication and role-based access control.

Provides:
  - OAuth CSRF state management for the platform login flow
  - JWT session tokens (HttpOnly cookies)
  - UserContext dataclass for per-request user info
  - Unified auth dependency: API key OR session cookie
  - Permission helpers for role-based endpoint gating
"""

import contextlib
import logging
import math
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum

import jwt
from fastapi import Depends, HTTPException, Request
from starlette.requests import HTTPConnection

import config
from storage import database as task_store
from storage.pg import run_db_fast
from auth import roles

logger = logging.getLogger("claude-proxy.auth")

# In-memory CSRF state store: state -> metadata dict
# ({"expiry": monotonic timestamp, "redirect_uri": str | None})
_oauth_states: dict[str, dict] = {}
_STATE_TTL = 300  # 5 minutes
_STATE_MAX = 10_000
# Live states per client (``create_oauth_state(client=)``): one address, or
# one IPv6 /64, cannot fill the store alone. A browser holds at most four
# (its state-binding ring), so the cap leaves room for a few at one address.
_STATE_MAX_PER_CLIENT = 8
# client -> its live state ids, oldest first.
_client_states: dict[str, list[str]] = {}

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
    role: str  # the platform role (auth/roles.PLATFORM_ROLES); roles.SERVICE for a no-user session
    agents: list[str] = field(default_factory=list)
    default_agent: str = ""
    display_name: str = ""
    is_api_key: bool = False  # True when authenticated via API key
    agent_roles: dict[str, str] = field(default_factory=dict)  # {agent: a member of roles.AGENT_ROLES}
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
    # External routes (``core/session/external_identity.py``): the session
    # token's ``ext`` claim — ``"<channel>:<id>"``, id-less ``"<channel>:"``
    # or ``"<channel>:ephemeral:<sid>"`` — set on every phone-minted token.
    # ``external_channel`` / ``external_id`` are its parsed parts (id "" when
    # withheld or shared). See ``is_external``.
    external_claim: str = ""
    external_channel: str = ""
    external_id: str = ""
    # The platform's own headless render (``auth/render_principal.py``): the
    # one app this principal may reach, and the live token's id. Empty for
    # every other principal.
    render_app: str = ""
    render_jti: str = ""
    # A browser session held by the forced password change or 2FA enrolment
    # (``auth_gate``): ``must_change_password``, ``must_enroll_2fa`` or "".
    # Only the cookie branch sets it.
    auth_gate: str = ""

    @property
    def is_admin(self) -> bool:
        return roles.is_admin(self.role)

    @property
    def is_external(self) -> bool:
        """True for a session on an external route whose caller is NOT a
        platform user (the token carries ``ext`` and no real ``user_sub``).
        Such a principal reaches only the endpoints its tools use
        (``auth/external_endpoints.py``), never delegates, and has no shared
        memory. A route tied to a platform user mints ``ext`` too (audit
        trail) but its caller IS that user — not external."""
        return bool(self.external_claim) and self.is_no_user_session

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

    def effective_role(self, agent: str) -> str:
        """This principal's role on ``agent``: ``roles.ADMIN`` for a platform
        admin, else the per-agent row, else ``roles.NO_ACCESS`` — the
        membership reading (``auth/roles.effective_role``)."""
        return roles.effective_role(self.role, self.agent_roles, agent)

    def acting_role(self, agent: str) -> str:
        """The role this ADMITTED principal acts with on ``agent``:
        ``effective_role`` or, holding no row, ``roles.VIEWER`` — a session
        acting on the agent it was minted for, a share grantee. Every
        caller sits behind an admission gate (``can_access_agent``, a
        grant, a route check) that refused a stranger already."""
        return roles.acting_role(self.role, self.agent_roles, agent)

    def can_manage_agent(self, agent: str) -> bool:
        """Owner-tier check: can this user CHANGE this agent's behavior
        (config, MCP assignments, delegation, service-account bindings,
        memory consolidation)? True for admin + per-agent manager. Editors are
        explicitly NOT included — editor is the collaborative workspace tier,
        not the owner tier. A session token (interactive or agent-scope) gets
        NO blanket bypass here: it resolves the real per-agent role, so a
        prompt-injected session cannot manage an agent it doesn't own.
        """
        return roles.can_manage(self.effective_role(agent))

    def can_edit_agent(self, agent: str) -> bool:
        """Editor-tier check: can this user WRITE to this agent's
        workspace + create their own agent-scope tasks / notifications /
        triggers? True for admin + per-agent manager + per-agent editor.
        Viewers are excluded — they're read-only collaborators.

        Editor is the workspace-collaboration tier; manager is the
        owner tier. The two tiers compose: every manager is also an
        editor.
        """
        return roles.can_edit(self.effective_role(agent))

    def can_write_workspace(self, agent: str) -> bool:
        """Workspace-tier check: may this user WRITE this agent's shared
        workspace? True for admin + per-agent manager + editor +
        contributor. The contributor's whole grant is files: never the
        agent's identity (``can_edit_agent`` stays the automation tier).
        """
        return roles.can_write_workspace(self.effective_role(agent))

    def can_write_files(self) -> bool:
        """Platform-level: is this user a creator/admin for ANY agent?"""
        return roles.is_creator_or_above(self.role)

    def can_manage_tasks(self) -> bool:
        """Platform-level: is this user a creator/admin? Used for platform-level checks."""
        return roles.is_creator_or_above(self.role)


# --- CSRF state ---


class StateStoreFull(Exception):
    """A new sign-in state refused: the store holds ``_STATE_MAX`` live
    states. ``retry_after`` is the seconds until the oldest one lapses."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"the sign-in state store is full; retry in {retry_after} s")
        self.retry_after = retry_after


class ClientStatesFull(Exception):
    """A new sign-in state refused: the client holds
    ``_STATE_MAX_PER_CLIENT`` live states. ``retry_after`` is the seconds
    until its oldest one lapses."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"the client holds its cap of sign-in states; retry in {retry_after} s")
        self.retry_after = retry_after


def _forget(state: str, meta: dict) -> None:
    """Drop a state that left the store from its client's list."""
    ids = _client_states.get(meta.get("client") or "")
    if ids is None:
        return
    with contextlib.suppress(ValueError):
        ids.remove(state)
    if not ids:
        _client_states.pop(meta["client"], None)


def create_oauth_state(redirect_uri: str | None = None, *, purpose: str = "login",
                       sub: str = "", return_to: str = "", nonce: str = "",
                       code_verifier: str = "", client: str = "",
                       client_capped: bool = True) -> str:
    """Generate a random state parameter and store it with a TTL.

    ``purpose`` is what the callback may do with the state: ``login`` issues
    a session; ``confirm`` (SHARING.md "The confirm") mints a one-shot
    confirm token for ``sub`` and sends the page back to ``return_to``. A
    state never serves the other purpose; ``created_at`` (wall clock) is the
    instant a confirm's ``auth_time`` is judged against. ``nonce`` and
    ``code_verifier`` stay here, server-side: the ID token must carry the
    first, and the code exchange sends the second (PKCE).

    Anyone may start a sign-in, so the store is bounded: a ``client`` (the
    starting address, an IPv6 address by its /64) holding
    ``_STATE_MAX_PER_CLIENT`` live states is refused (``ClientStatesFull``)
    unless ``client_capped`` is off (an address every client shares); at
    ``_STATE_MAX`` live states in all a new one is refused
    (``StateStoreFull``). No sign-in in flight loses its state."""
    now = time.monotonic()
    # Every state lives _STATE_TTL, so insertion order is expiry order: the
    # lapsed ones are at the front.
    while _oauth_states:
        oldest = next(iter(_oauth_states))
        if _oauth_states[oldest]["expiry"] >= now:
            break
        _forget(oldest, _oauth_states.pop(oldest))
    if client and client_capped:
        ids = [s for s in _client_states.get(client, ()) if s in _oauth_states]
        if ids:
            _client_states[client] = ids
        else:
            _client_states.pop(client, None)
        if len(ids) >= _STATE_MAX_PER_CLIENT:
            raise ClientStatesFull(max(1, math.ceil(_oauth_states[ids[0]]["expiry"] - now)))
    if len(_oauth_states) >= _STATE_MAX:
        oldest_expiry = next(iter(_oauth_states.values()))["expiry"]
        raise StateStoreFull(max(1, math.ceil(oldest_expiry - now)))
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {
        "expiry": now + _STATE_TTL,
        "redirect_uri": redirect_uri,
        "purpose": purpose,
        "sub": sub,
        "return_to": return_to,
        "created_at": time.time(),
        "nonce": nonce,
        "code_verifier": code_verifier,
        "client": client,
    }
    if client:
        _client_states.setdefault(client, []).append(state)
    return state


def peek_oauth_state(state: str) -> dict | None:
    """A live state's metadata, left in the store (None when unknown or
    lapsed); the dict is the stored one."""
    meta = _oauth_states.get(state)
    if meta is None or time.monotonic() > meta["expiry"]:
        return None
    return meta


def discard_oauth_state(state: str, client: str) -> None:
    """Drop a live state the same ``client`` started (a state pushed out of
    its browser's binding ring, which can no longer complete there)."""
    meta = _oauth_states.get(state)
    if meta is not None and client and meta.get("client") == client:
        _forget(state, _oauth_states.pop(state))


def validate_oauth_state(state: str) -> dict | None:
    """Check and consume a state parameter. Returns metadata dict or None."""
    meta = _oauth_states.pop(state, None)
    if meta is None:
        return None
    _forget(state, meta)
    if time.monotonic() > meta["expiry"]:
        return None
    return meta


# --- JWT ---

# OAuth state-token management for the MCP credential flow lives in
# ``services/oauth_engine`` (``create_state`` / ``validate_state``);
# this module owns only the platform-auth state (Authentik etc.).


def create_session_jwt(sub: str, email: str, name: str, role: str,
                       auth_provider: str = "local", *,
                       expiry_hours: int | None = None,
                       jti: str | None = None,
                       issued_at: int | None = None) -> str:
    """Create an HS256 JWT for the dashboard session cookie.
    ``expiry_hours`` saves a caller that already read the setting a second
    read (``config.get_jwt_expiry_hours``). ``jti`` is the id of the
    SIGN-IN: a login mints a fresh one, the sliding refresh re-mints with
    the presented cookie's, so a logout revokes the whole lineage of one
    sign-in and no other (``auth/session_revocation.py``). ``issued_at``
    is the instant the sliding refresh last judged the cookie current: a
    token epoch that moved after it refuses the re-minted cookie too."""
    hours = expiry_hours if expiry_hours is not None else config.get_jwt_expiry_hours()
    now = int(time.time()) if issued_at is None else int(issued_at)
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
        "iat": now,
        "exp": now + hours * 3600,
        "jti": jti or secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def apply_session_cookie(response, token: str, *, expiry_hours: int | None = None) -> None:
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
        max_age=(expiry_hours if expiry_hours is not None else config.get_jwt_expiry_hours()) * 3600,
    )


def session_iat_after_password_change(user: dict, payload: dict) -> bool:
    """True if this session cookie was issued AT OR AFTER the user's last
    password change — i.e. it is still valid against the credential timeline.

    A cookie minted before ``password_changed_at`` must be rejected: password
    change / admin reset / self-service reset all stamp that column, and
    without this check a cookie stolen before the change stays valid forever
    (the sliding-refresh middleware would even keep re-minting it). Fails
    OPEN only when the column is absent/unparseable (legacy rows / OIDC
    accounts that never set a password) — there is no pre-change baseline to
    invalidate against, so existing sessions are unaffected.
    """
    changed_at = user.get("password_changed_at")
    if not changed_at:
        return True
    iat = payload.get("iat")
    if not isinstance(iat, int):
        return False  # a session cookie must carry iat; treat missing as stale
    try:
        from datetime import datetime
        changed_ts = datetime.fromisoformat(changed_at).timestamp()
    except (ValueError, TypeError):
        return True
    # 5 s grace: the fresh cookie issued IN the change response can carry an
    # iat a hair before the DB write timestamp under clock jitter.
    return iat >= changed_ts - 5


def _iso_ts(value) -> float | None:
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return None


def _after_token_epoch(user: dict, iat: int) -> bool:
    """``iat`` is at or after the person's token epoch (``users.token_epoch_at``,
    moved by a password set and by an admin's "sign out everywhere"; never
    bumped = no epoch). No grace beyond the epoch's own second: ``iat`` is
    whole seconds, so the cookie the password change re-issues right after
    its write lands in that second or a later one, while a sign-in made
    seconds before an admin's "sign out everywhere" ends with the rest."""
    epoch = _iso_ts(user.get("token_epoch_at"))
    return epoch is None or iat >= int(epoch)


def session_token_is_current(user: dict, payload: dict) -> bool:
    """The credential timeline for an agent session token: refused when
    minted before the user's last password change, before their token epoch
    or before the users row existed (a person deleted and re-created under
    the same sub). A token minted before tokens carried ``iat`` is
    tolerated: it expires within its 24 h. The password and creation rules
    keep the cookie's 5 s grace; the epoch has none beyond its own second
    (``_after_token_epoch``)."""
    iat = payload.get("iat")
    if not isinstance(iat, int):
        return True
    created = _iso_ts(user.get("created_at"))
    if created is not None and iat < created - 5:
        return False
    if not _after_token_epoch(user, iat):
        return False
    return session_iat_after_password_change(user, payload)


def session_cookie_current(user: dict, payload: dict) -> bool:
    """The credential timeline for a dashboard session cookie: the password
    rule (``session_iat_after_password_change``), the token epoch, and the
    users row's creation (a person deleted and re-created under the same
    sub, an identity provider's especially, does not revive an old
    cookie). A cookie carries ``iat`` since the first release."""
    if not session_iat_after_password_change(user, payload):
        return False
    iat = payload.get("iat")
    if not isinstance(iat, int):
        return False
    created = _iso_ts(user.get("created_at"))
    if created is not None and iat < created - 5:
        return False
    return _after_token_epoch(user, iat)


def session_token_holder_ok(payload: dict) -> bool:
    """For code that validates a session JWT itself instead of through
    ``get_current_user`` (hooks, the app proxy, the phone relay): False when
    the token names a user who is gone or whose credentials changed after it
    was minted. A token with no user is not judged here. Synchronous: call
    it on the DB executor."""
    sub = payload.get("user_sub") or ""
    if not sub:
        return True
    user = task_store.get_user(sub)
    return bool(user) and session_token_is_current(user, payload)


# --- The forced password change and 2FA enrolment ---

GATE_CHANGE_PASSWORD = "must_change_password"
GATE_ENROLL_2FA = "must_enroll_2fa"
_GATE_DETAIL = {
    GATE_CHANGE_PASSWORD: "Password change required",
    GATE_ENROLL_2FA: "Two-factor enrolment required",
}
# What a browser session held by the gate still reaches: the change and
# enrolment screens' own calls, the session routes, and the Android app's
# push registration (it runs once per launch, before the redirect).
_GATE_EXEMPT = frozenset({
    ("PUT", "/v1/users/me/password"),
    ("POST", "/v1/users/me/totp/setup"),
    ("POST", "/v1/users/me/totp/verify"),
    ("GET", "/v1/users/me/passkeys"),
    ("POST", "/v1/users/me/passkeys/register/options"),
    ("POST", "/v1/users/me/passkeys/register/verify"),
    ("GET", "/auth/me"),
    ("GET", "/auth/config"),
    ("POST", "/auth/logout"),
    ("POST", "/v1/push/subscribe"),
})
# Only the answers that cannot hold anyone wrongly are cached: the policy
# off, a person who has a passkey. A stale entry delays the gate by these
# seconds at most after the policy goes on or a last passkey goes; it never
# imposes the gate or keeps it after an enrolment.
# The auth provider of a password account; an SSO account's is "oidc:<name>".
_LOCAL_AUTH_PROVIDER = "local"
_POLICY_OFF_TTL_S = 10.0
_PASSKEY_TTL_S = 60.0
_PASSKEY_CACHE_MAX = 4096
_policy_off_until = 0.0
_passkey_until: dict[str, float] = {}


def clear_auth_gate_caches() -> None:
    global _policy_off_until
    _policy_off_until = 0.0
    _passkey_until.clear()


def _has_passkey_cached(sub: str, now: float) -> bool:
    until = _passkey_until.get(sub)
    if until is None:
        return False
    if until > now:
        return True
    _passkey_until.pop(sub, None)
    return False


def auth_gate(user: dict | None) -> str:
    """The gate that holds a browser session of ``user``: ``must_change_password``,
    ``must_enroll_2fa`` or "". For a local account it is the rule ``/auth/me``
    reports, so the server refuses where the dashboard redirects; an SSO
    account is never held (its identity provider owns the password and the
    MFA). Synchronous: call it on the DB executor."""
    global _policy_off_until
    if not user:
        return ""
    if not (user.get("auth_provider") or _LOCAL_AUTH_PROVIDER).startswith(_LOCAL_AUTH_PROVIDER):
        return ""
    if user.get("must_change_password"):
        return GATE_CHANGE_PASSWORD
    if user.get("totp_enabled"):
        return ""
    sub = user.get("sub") or ""
    now = time.monotonic()
    # A passkey is a second factor only while passkeys are enabled: without
    # them the password login never offers one.
    from api.auth.webauthn import passkeys_enabled
    passkeys_on = passkeys_enabled()
    if _policy_off_until > now or (passkeys_on and _has_passkey_cached(sub, now)):
        return ""
    if task_store.get_platform_setting("require_2fa") != "1":
        _policy_off_until = now + _POLICY_OFF_TTL_S
        return ""
    from storage.identity import webauthn_store
    if passkeys_on and webauthn_store.count_credentials(sub) > 0:
        if len(_passkey_until) >= _PASSKEY_CACHE_MAX:
            _passkey_until.clear()
        _passkey_until[sub] = now + _PASSKEY_TTL_S
        return ""
    return GATE_ENROLL_2FA


def _enforce_auth_gate(request, principal: "UserContext") -> None:
    """Refuse a held browser session everything but its exempt routes: 403
    with ``X-Auth-Gate``, never 401 (the dashboard reads a 401 as a lost
    session). HTTP routes are refused here; the dashboard socket applies
    the same ``auth_gate`` rule in ``ws/dashboard.py``, at the handshake and
    at each revalidation (close 4403)."""
    if not isinstance(request, Request):
        return
    scope = request.scope
    if (scope.get("method", ""), scope.get("path", "")) in _GATE_EXEMPT:
        return
    raise HTTPException(status_code=403, detail=_GATE_DETAIL[principal.auth_gate],
                        headers={"X-Auth-Gate": principal.auth_gate})


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
    from auth import session_revocation
    if session_revocation.is_revoked(session_revocation.session_cookie_id(payload)):
        logger.debug("JWT rejected: the sign-in was revoked")
        return None
    return payload


# --- Unified auth dependency ---


_PRINCIPAL_UNSET = object()


def _load_user(sub: str, *, with_default: bool = True) -> tuple[dict | None, dict, str]:
    """The reads a principal needs, in one executor hop: the users row, the
    per-agent roles and (dashboard cookies only) the default agent."""
    user = task_store.get_user(sub)
    if not user:
        return None, {}, ""
    roles = task_store.get_user_agent_roles(sub)
    default_agent = (task_store.get_user_default_agent(sub) or "") if with_default else ""
    return user, roles, default_agent


def _load_cookie_user(sub: str) -> tuple[dict | None, dict, str, str]:
    """``_load_user`` for a dashboard cookie, plus the person's gate."""
    user, agent_roles, default_agent = _load_user(sub)
    return user, agent_roles, default_agent, auth_gate(user)


def user_context_for_sub(sub: str) -> UserContext | None:
    """The cookie-shaped principal of a known user, for code that must
    apply an access rule to a user who is not the caller (an agent acting
    on a viewer's screen, a fan-out deciding who may see a row).
    Synchronous: call it on the DB executor."""
    if not sub:
        return None
    user, agent_roles, _ = _load_user(sub, with_default=False)
    if not user:
        return None
    return UserContext(
        sub=user["sub"],
        email=user["email"],
        name=user["name"],
        role=user["role"],
        agents=list(agent_roles.keys()),
        display_name=user.get("display_name", ""),
        agent_roles=agent_roles,
        is_owner=bool(user.get("is_owner")),
    )


def effective_role_of(sub: str, agent: str, *, fallback_user: dict | None = None) -> str:
    """The store-backed resolver: the live users row (or ``fallback_user``,
    the dict a socket kept from connect time, when the row is gone), the
    per-agent map, then ``auth/roles.effective_role`` — ``roles.ADMIN`` for a
    platform admin, the row, else ``roles.NO_ACCESS``. Synchronous: call it
    on the DB executor."""
    if not sub:
        return roles.NO_ACCESS
    user = task_store.get_user(sub) or fallback_user or {}
    return roles.effective_role(user.get("role"), task_store.get_user_agent_roles(sub), agent)


def acting_role_of(sub: str, agent: str, *, fallback_user: dict | None = None) -> str:
    """``effective_role_of`` read for an ADMITTED principal: ``roles.VIEWER``
    when the sub holds no row (the session builders, the fan-outs, a
    task's run identity). Synchronous: call it on the DB executor."""
    return effective_role_of(sub, agent, fallback_user=fallback_user) or roles.VIEWER


async def get_current_user(request: Request) -> UserContext | None:
    """Extract user from API key header OR session cookie.

    Returns UserContext or None (caller decides whether to 401). Resolved
    once per connection: the result is memoized on ``request.state`` so a
    handler calling this directly after the dependency ran, or a WebSocket
    handler asking twice, never repeats the reads — which run on the DB
    executor, never on the event loop. A browser session held by the forced
    password change or 2FA enrolment is refused here (403) on every HTTP
    route but its exempt ones.
    """
    state = request.state if isinstance(request, HTTPConnection) else None
    if state is not None:
        memo = getattr(state, "otodock_principal", _PRINCIPAL_UNSET)
    else:
        memo = _PRINCIPAL_UNSET
    if memo is not _PRINCIPAL_UNSET:
        principal = memo
    else:
        principal = await _resolve_principal(request)
        if state is not None:
            state.otodock_principal = principal
    if principal is not None and principal.auth_gate:
        _enforce_auth_gate(request, principal)
    return principal


async def _resolve_principal(req: Request) -> UserContext | None:
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
                    role=roles.ADMIN,
                    agents=[],
                    is_api_key=True,
                )
            # Session-scoped JWT (agent subprocesses: schedules-mcp, notifications, etc.)
            from auth.session_token import validate_session_token
            session_payload = validate_session_token(token)
            if session_payload:
                # The session must be live and the token of its current
                # life (the HTTP middleware refused it already; a WebSocket
                # scope has no middleware). In memory, before any read.
                from core.session.session_state import session_token_refusal
                if session_token_refusal(session_payload):
                    return None
                # If the token was minted with a real user_sub, resolve it
                # back to the actual users row so API-call attribution
                # (e.g. mcp_assignment_requests.requested_by) records the
                # real identity rather than a synthetic session string.
                # Agent-scope sessions with no human owner get the
                # synthetic no-user principal.
                sid = session_payload.get("sid") or ""
                agent_name = session_payload.get("agent") or ""
                user_sub = session_payload.get("user_sub") or ""
                ext = _parse_external_claim(session_payload.get("ext") or "")
                if user_sub:
                    user, agent_roles, _ = await run_db_fast(
                        _load_user, user_sub, with_default=False,
                    )
                    # A token that names a person is only as good as the
                    # person: gone, or a password changed since the mint,
                    # and it resolves to nobody, never to the broader
                    # no-user agent principal below.
                    if not user or not session_token_is_current(user, session_payload):
                        return None
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
                        **ext,
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
                    role=roles.SERVICE,
                    agents=[],
                    is_api_key=True,
                    session_id=sid,
                    agent=agent_name,
                    **ext,
                )

    # 2. Session cookie → decode JWT → fetch user from DB
    session_cookie = req.cookies.get("session")
    if session_cookie:
        payload = validate_session_jwt(session_cookie)
        if payload:
            sub = payload["sub"]
            checked_at = int(time.time())
            user, agent_roles, default_agent, gate = await run_db_fast(_load_cookie_user, sub)
            if user and not session_cookie_current(user, payload):
                # The cookie predates the last password change, the token
                # epoch or the row itself: dead.
                return None
            if user:
                # The sliding refresh re-mints this exact cookie without a
                # second read (``middleware``): it passed the check above,
                # which the re-mint dates itself to.
                if isinstance(req, HTTPConnection):
                    req.state.otodock_session_cookie = session_cookie
                    req.state.otodock_session_cookie_checked_at = checked_at
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
                    auth_gate=gate,
                )
        return None

    # 3. The platform's own headless render: its own cookie, never next to a
    # session or a bearer (those were judged above).
    render_cookie = req.cookies.get("otodock_render")
    if render_cookie:
        from auth import render_principal
        return await render_principal.principal_from_cookie(req, render_cookie)

    return None


def _parse_external_claim(claim: str) -> dict[str, str]:
    """``UserContext`` kwargs for a session token's ``ext`` claim. A malformed
    claim (the proxy signed it, so that is a bug) is logged and treated as
    absent — the token then has no external standing at all."""
    if not claim:
        return {}
    try:
        from core.session.external_identity import parse_claim
        channel, ident, _ephemeral = parse_claim(claim)
    except ValueError:
        logger.error("Session token carries a malformed external claim: %r", claim)
        return {}
    return {
        "external_claim": claim,
        "external_channel": channel,
        "external_id": ident,
    }


# --- Permission helpers ---


def require_auth(user: UserContext | None) -> UserContext:
    """Raise 401 if user is None."""
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


async def require_user(user: UserContext | None = Depends(get_current_user)) -> UserContext:
    """A router's dependency when none of its routes takes an anonymous
    caller: 401 for a request with no credential, decided before FastAPI
    validates the body, so an anonymous caller never learns a route's body
    from a 422 (a body that is not JSON at all is still refused 422 first:
    FastAPI reads it before any dependency, and that error names no field).
    Any principal passes; each route judges the role itself. Taken through
    ``Depends`` so the request's one principal lookup is shared with the
    route's own and an override of ``get_current_user`` applies."""
    return require_auth(user)


def require_admin(user: UserContext | None) -> UserContext:
    """Raise 401/403 unless a REAL admin user (dashboard session, not a key).

    API-key/session-token principals are rejected like ``require_creator``
    does: the per-session JWT every agent subprocess holds resolves to the
    session OWNER's real role, so without this gate agent code running in an
    admin-owned session could drive the entire /v1/admin/* surface (user
    creation, password resets) with raw HTTP from inside the sandbox. The
    master key never legitimately reaches admin routes either — the S2S
    confinement middleware limits it to its endpoint allowlist.
    """
    u = require_auth(user)
    if getattr(u, "is_api_key", False):
        raise HTTPException(
            status_code=403, detail="User authentication required (not API key)"
        )
    if not u.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return u


def require_human(user: UserContext | None) -> UserContext:
    """Raise 401/403 unless a dashboard-cookie principal: a person at the
    keyboard, whatever their role (the role check stays at the call site).

    Every bearer principal is refused, the real-user-backed session JWT
    included: that token resolves to the session OWNER's real role, so a
    route gated on the role alone lets agent code running inside the
    sandbox act as its owner. Approving an app manifest, sharing, confirming
    a deploy are decisions a human takes on a card, never a request a
    prompt can make — those routes use this gate.
    """
    u = require_auth(user)
    if getattr(u, "is_api_key", False):
        raise HTTPException(
            status_code=403, detail="User authentication required (not API key)"
        )
    return u


def require_creator(user: UserContext | None) -> UserContext:
    """Raise 401/403 unless a REAL user (not API key) with creator+ role.

    The canonical admin-or-creator gate (promoted from the six inline copies
    scattered across api/ — new call sites use this one). API-key principals
    are rejected: creator-tier surfaces are human dashboard actions, and an
    S2S key must never inherit them.
    """
    u = require_auth(user)
    if getattr(u, "is_api_key", False):
        raise HTTPException(
            status_code=403, detail="User authentication required (not API key)"
        )
    if not roles.is_creator_or_above(u.role):
        raise HTTPException(status_code=403, detail="Creator role required")
    return u


def require_creator_interactive(user: UserContext | None) -> UserContext:
    """``require_creator`` widened to REAL-USER-backed session principals.

    Accepts: dashboard cookies AND session JWTs whose ``user_sub`` resolved
    to a real users row (``acting_sub`` set) — with platform role
    admin/creator either way. Still rejects the master key and no-user
    agent sessions.

    This is the deliberate, operator-decided carve-out (2026-08-15,
    shared-libraries design § D-MCP) for platform-role
    actions driven from chat via self-config MCP tools. The residual
    prompt-injection risk is mitigated by the MCP layer: every tool that
    reaches a ``require_creator_interactive`` endpoint sits in the
    CRITICAL permission tier — always prompts (every mode incl. dontAsk),
    denied outright in no-human contexts — so the human approves each
    call in chat before it runs. Use ``require_creator`` for surfaces
    that must stay dashboard-only.
    """
    u = require_auth(user)
    if u.acting_sub is None:
        raise HTTPException(
            status_code=403,
            detail="User authentication required (not a service credential)",
        )
    if not roles.is_creator_or_above(u.role):
        raise HTTPException(status_code=403, detail="Creator role required")
    return u


def session_bound_to(param: str):
    """A route dependency: a session token acts only on the agent it was
    started for. Refuses (403) a session-token principal whose ``agent``
    claim (an empty one included) differs from the path parameter
    ``param``; every other principal goes on to the route's own gate. For
    the per-agent routes that change an agent's behaviour; the cross-agent
    reach of delegation, meetings and the session lists stays with their
    own routes, and the role map is untouched."""
    async def _bound(request: Request, user: UserContext | None = Depends(get_current_user)) -> None:
        if user is not None and user.is_session and user.agent != request.path_params.get(param, ""):
            raise HTTPException(
                status_code=403,
                detail=f"A session acts only on the agent it was started for ('{user.agent}')",
            )
    return _bound


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

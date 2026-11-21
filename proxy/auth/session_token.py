"""Session-scoped JWT tokens for agent subprocess authentication.

Replaces the master PROXY_API_KEY in agent subprocess environments.
Agents get a short-lived, session-scoped token instead of the platform key.
The proxy accepts both master key (service-to-service) and session tokens.
"""

import threading
import time
from collections import OrderedDict

from auth import tokens

# The session's command-line token (``attach_cli_token``): the shell's
# credential for ``POST /v1/hooks/cli-credential``, kept from every MCP child.
CLI_TOKEN_ENV = "OTO_CLI_TOKEN"

# The two sentinel Authorization bearers a per-session MCP config carries at
# BUILD time (``build_session_mcp_config``), each swapped by every per-layer
# ``?session_id=`` injection site for the token it stands for once the
# session id is known (``swap_session_jwt_bearer``). The session sentinel is
# a credentialed HTTP MCP's gateway entry: the gateway route takes the
# session's own token. The callback sentinel is a Docker tool that calls the
# proxy back (manifest ``server.proxy_callbacks``): a shared container cannot
# hold a session-scoped env token the way a stdio MCP does, so it carries
# the callback kind per request, good for the five file callbacks alone.
# Mirrors ``core.credentials.mcp_broker.BROKER_BEARER_PLACEHOLDER`` (vendor
# bearers).
SESSION_JWT_PLACEHOLDER = "OTO_SESSION_JWT"
SESSION_JWT_SENTINEL_BEARER = f"Bearer {SESSION_JWT_PLACEHOLDER}"
CALLBACK_JWT_PLACEHOLDER = "OTO_CALLBACK_JWT"
CALLBACK_JWT_SENTINEL_BEARER = f"Bearer {CALLBACK_JWT_PLACEHOLDER}"


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
    """The ``Bearer <token>`` a sentinel stands for, minted for this session
    (for the person its own token names, unless ``user_sub`` says
    otherwise): the session sentinel becomes the session's own token (a
    gateway entry, whose route takes it), the callback sentinel the callback
    kind a Docker tool calls back with, good for the five file callbacks and
    nothing else. ``None`` for any other value (a real vendor bearer: the
    caller leaves the header untouched)."""
    if auth_value == SESSION_JWT_SENTINEL_BEARER:
        user_sub = user_sub or _session_user(session_id)
        return f"Bearer {create_session_token(session_id, agent_name, user_sub)}"
    if auth_value == CALLBACK_JWT_SENTINEL_BEARER:
        user_sub = user_sub or _session_user(session_id)
        return f"Bearer {create_callback_token(session_id, agent_name, user_sub)}"
    return None


def create_callback_token(
    session_id: str, agent_name: str, user_sub: str = "", *, issued_at: int | None = None,
) -> str:
    """The token a Docker tool (``server.proxy_callbacks``: file-tools,
    video-tools) calls back with: the session token's claims under the
    ``TOOL_CALLBACK`` kind, so it is judged for liveness and its holder as
    the session's own token is, and reaches the five file callbacks alone
    (``auth/external_endpoints.is_callback_endpoint_allowed``) on either
    listener, since the container arrives on the public one."""
    now = int(time.time()) if issued_at is None else int(issued_at)
    return tokens.mint(tokens.TOOL_CALLBACK, {
        "type": "tool_callback",
        "sid": session_id,
        "agent": agent_name,
        "user_sub": user_sub,
        "iat": now,
        "exp": now + 24 * 3600,
    })


def validate_callback_token(token: str) -> dict | None:
    """The claims of a live callback token, or None."""
    return tokens.verify(token, tokens.TOOL_CALLBACK)


def callback_token_for(session_token: str) -> str | None:
    """A callback token with the session, agent, person and floor (``iat``)
    of ``session_token``: for the sites that hold a session's token and
    nothing else (the remote rewrite). None for a string that is no live
    session token."""
    claims = validate_session_token(session_token)
    if not claims:
        return None
    iat = claims.get("iat")
    return create_callback_token(
        str(claims.get("sid") or ""), str(claims.get("agent") or ""),
        str(claims.get("user_sub") or ""), issued_at=iat if isinstance(iat, int) else None,
    )


def _live_subject_claim(session_id: str) -> str:
    """The ``subj`` claim of the session's registered SecurityContext: the
    outside person a wrap-up writes the memory of ("" for every other
    session, and before the context is registered)."""
    if not session_id:
        return ""
    try:
        from core.session.session_state import get_session_security
    except ImportError:  # pragma: no cover — import-order safety only
        return ""
    ctx = get_session_security(session_id)
    return getattr(ctx, "subject_id", "") or ""


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


def _live_built_role(session_id: str) -> str:
    """The role a live session was built with (its registered
    SecurityContext's), "" when none is registered."""
    if not session_id:
        return ""
    from core.session.session_state import get_session_security
    ctx = get_session_security(session_id)
    role = getattr(ctx, "role", "") if ctx is not None else ""
    return role if isinstance(role, str) else ""


def create_session_token(
    session_id: str,
    agent_name: str,
    user_sub: str = "",
    *,
    external: str | None = None,
    issued_at: int | None = None,
    subject: str | None = None,
    role: str | None = None,
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
        subject: the ``subj`` claim of the wrap-up after a conversation,
            the outside person's identity row whose memory the memory API
            maps it to. ``None`` derives it from the registered context,
            as ``external``.
        role: the ``role`` claim of a token that names a person: the role
            its session was built with, which every route judges it at
            (``auth/providers.session_role_cap``). ``None`` derives it from
            the registered context, as ``external``.
    """
    if external is None:
        external = _live_external_claim(session_id)
    if subject is None:
        subject = _live_subject_claim(session_id)
    if role is None:
        role = _live_built_role(session_id)
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
    if subject:
        payload["subj"] = subject
    if user_sub and role:
        payload["role"] = role
    return tokens.mint(tokens.SESSION, payload)


def mint_session_env(
    env: dict[str, str],
    session_id: str,
    agent_name: str,
    person_sub: str,
    *,
    external: str | None,
    role: str,
    subject: str | None = None,
) -> int:
    """Write the session's token into ``env`` as ``PROXY_API_KEY``, naming
    ``person_sub`` ("" for nobody), and return its ``iat``: the session's
    token floor (``AgentConfig.token_minted_at``).

    The one mint of every session builder (the chat, task, meeting and phone
    builders, a wake's respawn). ``env`` is the session's credential
    environment, laid last over the process env and written into a Codex
    config's stdio MCPs, so an engine's own token, which names nobody, never
    reaches them. ``external`` is the ``ext`` claim ("" for none, ``None`` to
    derive it from the live session, as :func:`create_session_token`).
    ``role`` is the role the builder builds the session at: the token carries
    it, so a session built below its person's role is judged at it wherever
    the token is accepted."""
    issued_at = int(time.time())
    env["PROXY_API_KEY"] = create_session_token(
        session_id, agent_name, person_sub, external=external, issued_at=issued_at,
        subject=subject, role=role,
    )
    return issued_at


def validate_session_token(token: str, *, legacy: bool = True) -> dict | None:
    """The payload of a live session token, or None. ``legacy=False``
    answers only a token minted by this release (``auth/tokens.py``)."""
    return tokens.verify(token, tokens.SESSION, legacy=legacy)


# The session claims a command-line token mirrors.
_CLI_CLAIMS = ("sid", "agent", "user_sub", "iat", "exp", "ext")


def attach_cli_token(env: dict) -> None:
    """Set ``OTO_CLI_TOKEN`` in an engine process's ``env`` from its final
    ``PROXY_API_KEY``: the same session, person, life (``iat``) and expiry,
    the ``CLI`` kind's key. Called last where an env that hosts a shell is
    assembled; without a valid session token the key is removed."""
    claims = validate_session_token(str(env.get("PROXY_API_KEY") or ""))
    # An external caller's session (``ext`` and no person) has no shell.
    if not claims or (claims.get("ext") and not claims.get("user_sub")):
        env.pop(CLI_TOKEN_ENV, None)
        return
    env[CLI_TOKEN_ENV] = tokens.mint(
        tokens.CLI, {k: claims[k] for k in _CLI_CLAIMS if k in claims},
    )


def validate_cli_token(token: str) -> dict | None:
    """The claims of a live command-line token, or None."""
    return tokens.verify(token, tokens.CLI)

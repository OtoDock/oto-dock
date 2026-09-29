"""Who the caller IS on a phone route — the warmup-time identity resolver.

A phone route carries ``identity_mode`` (``caller`` / ``shared`` / ``user``)
and ``remember_callers`` (``storage/phone/phone_route_store.py``). This module
turns one call's facts (the route, the agent the daemon asked for, the
caller-id the trunk reported, the daemon's PIN-gate result) into a
:class:`RouteIdentity` the config builder consumes. It is the ONLY place that
decides between an EXTERNAL principal (``core/session/external_identity.py``)
and a session tied to a platform user, and it binds the route to the agent:
a daemon that names a route of another agent gets nothing from it.

An external principal always runs with the **viewer** role
(:data:`EXTERNAL_ROUTE_ROLE`): it reads the agent's shared space and writes
only its own tree. The ``phone_routes.role`` column is a leftover of the
per-route role selector (removed 2026-09-08) and is ignored here.

Every fallback is loud (WARNING / ERROR) and lands on the safest identity —
the per-caller external principal — never on a user's session.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from core.session import external_identity
from core.session.visibility import available_scopes_for
from storage.agents import agent_store
from storage.phone import phone_route_store
from storage import database as task_store
from auth import roles

logger = logging.getLogger("claude-proxy")

#: The role of every external principal on a phone line (a caller who is not
#: a platform user): viewer of the agent's shared space, writer only of its
#: own tree. Not a setting — a trusted line is a route tied to a platform
#: user, whose own per-agent role applies (capped below).
EXTERNAL_ROUTE_ROLE = roles.VIEWER
#: A route's ``identity_mode``: the caller's own external identity, or the
#: platform user the route is tied to (``identity_user_sub``). Named here,
#: once; ``shared`` is the retired spelling that resolves as ``caller``.
IDENTITY_MODE_CALLER = "caller"
IDENTITY_MODE_USER = "user"

#: The tied user's per-agent role is capped here: a phone line never runs the
#: unrestricted admin policy (admin-on-admin-agent fast paths, admin bash tier).
USER_ROUTE_ROLE_CAP = roles.MANAGER


@dataclass(frozen=True)
class RouteIdentity:
    """The resolved identity of one call."""

    mode: str                                   # caller | shared | user
    #: The external principal's role — always :data:`EXTERNAL_ROUTE_ROLE`
    #: (informational on a user route, whose session role is ``user_role``).
    role: str
    #: The external principal (caller / shared modes); None for a user route.
    external: external_identity.ExternalIdentity | None
    #: The tied platform user's row + effective (capped) per-agent role.
    user: dict | None = None
    user_role: str = ""
    #: For a user route: the caller's claim (``phone:<id>``) — rides the
    #: session token for liveness + the call-log audit; the principal stays
    #: the user.
    caller_claim: str = ""
    #: Why the resolver fell back from the route's configured mode ("" = none).
    fallback_reason: str = ""

    @property
    def is_external(self) -> bool:
        return self.external is not None

    @property
    def label(self) -> str:
        """Call-log identity label."""
        if self.external is not None:
            return self.external.label
        return f"user:{(self.user or {}).get('username') or ''}"


def _agent_has_personal_scope(agent: str) -> bool:
    """Whether the agent's visibility mode offers a personal scope — a caller
    gets a private tree only then (on a Shared-only agent the caller keeps
    their id but works in the shared space with no per-caller memory)."""
    row = agent_store.get_agent(agent) or {}
    return "user" in available_scopes_for(
        bool(row.get("collaborative", True)), row.get("default_scope") or "user",
    )


def tied_user_problem(sub: str, agent: str) -> str:
    """Why ``sub`` cannot back a ``user`` route on ``agent`` right now ("" =
    fine). The same rule the route API applies at save time, re-checked at
    warmup because users get deleted, locked or unassigned later."""
    user = task_store.get_user(sub) if sub else None
    if not user:
        return "the tied user no longer exists"
    if not user.get("username"):
        return "the tied user has no username"
    if user.get("locked_until"):
        return "the tied user account is locked"
    if not roles.is_admin(user.get("role")) and agent not in task_store.get_user_agent_roles(sub):
        return "the tied user has no access to this agent"
    return ""


def effective_user_role(user: dict, agent: str) -> str:
    """The tied user's per-agent role, capped for a phone line."""
    return roles.capped(
        roles.acting_role(user.get("role"), task_store.get_user_agent_roles(user["sub"]), agent),
        USER_ROUTE_ROLE_CAP)


def route_identity_mode(route: dict) -> str:
    """The route's identity mode as one of the two named members: the
    retired ``shared`` spelling (removed 2026-09-07) resolves as ``caller``,
    because per-caller on a Shared-only agent IS the shared space with no
    per-caller memory, and ``remember_callers`` off covers "no memory"
    everywhere else."""
    mode = route.get("identity_mode") or IDENTITY_MODE_CALLER
    return IDENTITY_MODE_USER if mode == IDENTITY_MODE_USER else IDENTITY_MODE_CALLER


def resolve_route_identity(
    route: dict | None,
    *,
    agent: str,
    caller_phone: str,
    session_id: str,
    pin_verified: bool = False,
) -> RouteIdentity:
    """Resolve the identity of one call.

    - No route, a disabled route, or a route bound to ANOTHER agent → the
      safest identity: the id-less external principal in the agent's shared
      space (WARNING).
    - ``user`` mode whose tied user no longer qualifies → per-caller external
      identity (ERROR), never a user's session.
    - ``caller`` mode on an agent without a personal scope (Shared-only) →
      the caller keeps their id but gets no tree (this IS the "shared" line;
      a stored legacy ``shared`` value resolves the same way).
    - ``pin_verified`` counts only when the route actually has a PIN.
    - An external call ALWAYS runs in the local sandbox on the server, on
      every engine: the external principal's role is :data:`EXTERNAL_ROUTE_ROLE`
      (viewer) and ``remote_store.resolve_execution_target`` forces every
      non-owner role to local (a satellite has no bwrap isolation), whatever
      the agent's execution target says. Codex takes external calls there
      like every other engine (no shell tool + the PreToolUse floor). Only a
      ``user`` route (the caller IS a platform user) can run on a remote
      machine, with that user's capped role.
    """
    if route is None or not route.get("enabled", True) or route.get("agent") != agent:
        if route is not None:
            logger.warning(
                "Phone route %s (agent=%s, enabled=%s) does not match warmup agent=%s "
                "— running as an external caller in the shared space",
                route.get("id"), route.get("agent"), route.get("enabled"), agent,
            )
        ident = external_identity.resolve(
            external_identity.PHONE, caller_phone, session_id=session_id, shared=True,
        )
        return RouteIdentity(
            mode="shared", role=EXTERNAL_ROUTE_ROLE, external=ident,
            fallback_reason="no route" if route is None else "route/agent mismatch",
        )

    mode = route_identity_mode(route)
    verified = bool(pin_verified) and bool(phone_route_store.get_route_pin(route["id"]))
    fallback = ""

    if mode == IDENTITY_MODE_USER:
        sub = route.get("identity_user_sub") or ""
        problem = tied_user_problem(sub, agent)
        if not problem:
            user = task_store.get_user(sub) or {}
            audit = external_identity.resolve(
                external_identity.PHONE, caller_phone, session_id=session_id,
                verified=verified, tree=False,
            )
            return RouteIdentity(
                mode="user", role=EXTERNAL_ROUTE_ROLE, external=None, user=user,
                user_role=effective_user_role(user, agent),
                caller_claim=audit.claim,
            )
        logger.error(
            "Phone route %s is tied to user %s but %s — running as an external "
            "caller instead", route.get("id"), sub[:12], problem,
        )
        mode, fallback = "caller", problem

    ident = external_identity.resolve(
        external_identity.PHONE, caller_phone, session_id=session_id,
        remember=bool(route.get("remember_callers", True)),
        verified=verified, tree=_agent_has_personal_scope(agent),
    )
    return RouteIdentity(
        mode=mode, role=EXTERNAL_ROUTE_ROLE, external=ident, fallback_reason=fallback,
    )


# ---------------------------------------------------------------------------
# Call identity → call log. The WS handler stamps the resolved label at
# warmup; the daemon's teardown report names the session id and the ingest
# (api/phone/phone_usage) joins the label from here. In-process with a 24 h
# TTL — a proxy restart between warmup and the report leaves the row's
# identity empty, nothing worse.
# ---------------------------------------------------------------------------

_CALL_IDENTITY_TTL_S = 24 * 3600
_call_identity: dict[str, tuple[str, float]] = {}


def remember_call_identity(session_id: str, label: str) -> None:
    """Stamp the call's identity label (``caller:<id>`` / ``caller-pin:<id>``
    / ``ephemeral`` / ``shared`` / ``user:<username>``) for the ingest."""
    now = time.time()
    for sid, (_label, ts) in list(_call_identity.items()):
        if now - ts > _CALL_IDENTITY_TTL_S:
            _call_identity.pop(sid, None)
    _call_identity[session_id] = (label, now)


def pop_call_identity(session_id: str) -> str:
    """The stamped label for a session ("" when unknown); consumed once."""
    entry = _call_identity.pop(session_id, None)
    return entry[0] if entry else ""

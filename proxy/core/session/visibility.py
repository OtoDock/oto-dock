"""Agent visibility-mode resolver — the single owner of mode → scope / mount /
memory / history / config decisions.

The platform has FOUR declarative visibility modes, stored as two independent
columns on the ``agents`` row (``collaborative`` × ``default_scope``):

    collaborative=TRUE,  scope=user   → Personal + shared
    collaborative=TRUE,  scope=agent   → Shared + personal
    collaborative=FALSE, scope=user   → Personal only   (no shared dirs at all)
    collaborative=FALSE, scope=agent   → Shared only    (no user dirs; one shared history)

Historically four concerns all collapsed onto one signal — ``username == ""``:
the mounted filesystem scope, the chat-history grouping, the human's role, and
the memory availability. **Shared only** breaks that: a human chats (so we keep
their real role + per-message attribution) but works in the *agent* filesystem
scope with a *single shared* chat history. This module is where those concerns
are decoupled, exactly once, so the sandbox / env / prompt / chat layers all
read a consistent answer.

Key distinction the rest of the codebase must honor:

  * **mount identity** (``mount_username`` / ``mount_scope``) — drives the bwrap
    mount set, the CWD, the ``.claude``/``.codex`` dir, and every ``path_roles``
    resolution. ``mount_username`` is ``""`` for any agent-scope mount (service
    sessions AND shared-only human chats).
  * **attribution identity** (the REAL ``username`` / ``user_sub``) — drives the
    session JWT, ``OTO_USER_SUB``, the ``# Session Context`` prompt line, chat
    ``author_sub``, and ``file_author``. NEVER replaced by the synthetic
    shared-history owner.
"""

from __future__ import annotations

from dataclasses import dataclass

# Owner-tier roles — the only roles that mount /config and curate knowledge.
_OWNER_TIER = ("manager", "admin")

# Stable mode keys (UI labels live in the dashboard; agent-config-mcp maps these).
MODE_PERSONAL_SHARED = "personal_shared"   # collaborative + user default
MODE_SHARED_PERSONAL = "shared_personal"   # collaborative + agent default
MODE_PERSONAL_ONLY = "personal_only"       # non-collab + user  → user dirs only
MODE_SHARED_ONLY = "shared_only"           # non-collab + agent → shared dirs only, one history


@dataclass(frozen=True)
class VisibilityResolution:
    """Everything a session-builder needs to know about an agent's mode."""

    mode: str                 # one of the MODE_* constants
    collaborative: bool
    default_scope: str        # the stored agents.default_scope ("user"|"agent")

    # Agent-level (mode) facts — independent of the session/user.
    available_scopes: tuple[str, ...]  # subset of ("user","agent") this mode offers
    mount_shared: bool        # does the mode include the shared /workspace + /knowledge?
    memory_user_enabled: bool   # master AND per-agent toggle AND user-scope available
    memory_agent_enabled: bool  # master AND per-agent toggle AND agent-scope available

    # Session-level facts — depend on the username / scope_override.
    mount_scope: str          # "user" | "agent" — which mount-set + CWD this session uses
    mount_username: str       # "" for an agent-scope mount; else the real username
    config_visible: bool      # owner-tier human → mounts /config + knowledge RW
    effective_default_scope: str  # clamped default scope arg for scope-aware MCPs
    is_service: bool          # True when there is no human owner (no username)

    # Chat-history grouping. Shared-only collapses every assigned user's chats
    # into ONE per-agent list via a synthetic owner; everyone else is per-user.
    history_owner: str        # "agent::{slug}" (shared-only) or the real user_sub


def _read_agent_mode(agent_name: str) -> tuple[bool, str]:
    """Return ``(collaborative, default_scope)`` for an agent, best-effort.

    Soft-fails to the collaborative / user-default (the safe, widest mode) so a
    missing row or a startup-time lookup race never blocks session start.
    """
    try:
        from storage import agent_store
        row = agent_store.get_agent(agent_name) or {}
        collaborative = bool(row.get("collaborative", True))
        default_scope = row.get("default_scope") or "user"
        if default_scope not in ("user", "agent"):
            default_scope = "user"
        return collaborative, default_scope
    except Exception:
        return True, "user"


def _read_memory_toggles(agent_name: str) -> tuple[bool, bool]:
    """Return effective ``(user, agent)`` memory toggles (master AND per-agent)."""
    try:
        from storage import memory_store
        settings = memory_store.get_settings()
        toggles = memory_store.get_agent_toggles(agent_name)
        memory_user = bool(
            settings.get("user_memory_enabled", True)
            and toggles.get("user_memory_enabled", True)
        )
        memory_agent = bool(
            settings.get("agent_memory_enabled", True)
            and toggles.get("agent_memory_enabled", True)
        )
        return memory_user, memory_agent
    except Exception:
        return True, True


def available_scopes_for(collaborative: bool, default_scope: str) -> tuple[str, ...]:
    """Agent-level scopes a mode offers (independent of any session)."""
    if collaborative:
        return ("user", "agent")
    if default_scope == "agent":
        return ("agent",)   # Shared only
    return ("user",)        # Personal only


def mode_for(collaborative: bool, default_scope: str) -> str:
    """Map the two columns to a stable mode key."""
    if collaborative:
        return MODE_PERSONAL_SHARED if default_scope == "user" else MODE_SHARED_PERSONAL
    return MODE_SHARED_ONLY if default_scope == "agent" else MODE_PERSONAL_ONLY


# Synthetic chat-row owner prefix for Shared-only agents. Every assigned user's
# dashboard chats collapse into ONE shared list per agent under this owner (the
# same pattern as ``ws/phone.py``'s ``"phone"`` sentinel). Attribution of who
# sent each message lives on ``chat_messages.author_sub`` instead.
SHARED_CHAT_OWNER_PREFIX = "agent::"


def is_shared_chat_owner(owner: str) -> bool:
    """True if a chat row's ``user_sub`` is a Shared-only synthetic owner."""
    return bool(owner) and owner.startswith(SHARED_CHAT_OWNER_PREFIX)


def is_shared_only(agent_name: str) -> bool:
    """True if the agent is in Shared-only mode — even a HUMAN chat mounts the
    agent scope (uploads, sessions, history all live in the shared agent space,
    not a per-user dir). The single signal upload/scope/access sites branch on to
    answer "is this an agent-scoped chat?" (e.g. the phone ``caller`` agent)."""
    collaborative, default_scope = _read_agent_mode(agent_name)
    return not collaborative and default_scope == "agent"


def nouser_read_targets(user) -> set[str]:
    """Agents a NO-USER session may additionally READ: its delegation roster
    (``agent_delegation_targets``, either source — merged observe semantics,
    2026-08-15: a wired edge that already lets the agent delegate work IS the
    read grant; the separate per-edge observe flag is retired).

    Empty for every other principal: user-backed sessions follow the USER's
    access (locked rule 1 — an edge never widens a chat session), and
    service callers are unfiltered anyway. Every consuming surface clamps
    edge reads to the target's AGENT-SCOPE rows, and an edge never grants
    writes or fire authority (rule 2).
    """
    if not (getattr(user, "is_no_user_session", False) and user.agent):
        return set()
    from storage import agent_store
    return set(agent_store.get_delegation_targets(user.agent))


def chat_history_owner(agent_name: str, user_sub: str) -> str:
    """The ``chats.user_sub`` owner for a dashboard chat on this agent.

    Shared-only agents return a synthetic per-agent owner (one shared history
    for every assigned user); every other mode returns the real ``user_sub``
    (per-user history). Phone chats keep their own ``"phone"`` sentinel and do
    not call this. Best-effort: an unknown agent falls back to per-user.
    """
    collaborative, default_scope = _read_agent_mode(agent_name)
    if not collaborative and default_scope == "agent":   # Shared-only
        return f"{SHARED_CHAT_OWNER_PREFIX}{agent_name}"
    return user_sub


def resolve_visibility(
    agent_name: str,
    *,
    username: str = "",
    user_role: str = "",
    user_sub: str = "",
    scope_override: str | None = None,
) -> VisibilityResolution:
    """Resolve an agent's visibility mode for one session.

    Args:
        agent_name: the agent slug.
        username: the REAL session username (the human owner). Empty for
            service sessions (phone / agent-scope task / trigger /
            meeting). Drives attribution AND, together with the mode, the
            mount scope.
        user_role: the human's per-agent role ("viewer"/"editor"/"manager"/
            "admin"/""). Drives ``config_visible`` and the viewer default-scope
            clamp.
        user_sub: the REAL OAuth subject of the owner — used as the per-user
            chat-history owner for every mode EXCEPT shared-only.
        scope_override: a task re-warm's stored scope ("user"|"agent"), or
            None for ordinary chats. Honored (clamped to the mode's available
            scopes) so a continued task always rebuilds in its own scope.

    Returns:
        A fully-populated :class:`VisibilityResolution`.
    """
    collaborative, default_scope = _read_agent_mode(agent_name)
    master_user, master_agent = _read_memory_toggles(agent_name)

    available = available_scopes_for(collaborative, default_scope)
    mount_shared = "agent" in available
    shared_only = (not collaborative and default_scope == "agent")

    # --- mount scope (which bwrap mount-set + CWD this session uses) ---
    if not username:
        # No human owner at all (phone / agent-task / trigger / meeting).
        mount_scope = "agent"
    elif scope_override is not None:
        # Task re-warm: honor the run's stored scope, clamped to the mode.
        mount_scope = scope_override if scope_override in available else available[0]
    else:
        # Human chat: shared-only mounts the agent scope; every other mode the
        # user scope. Both are guaranteed present in ``available`` by construction.
        mount_scope = "agent" if shared_only else "user"
    mount_username = username if mount_scope == "user" else ""

    # --- /config + knowledge-RW: owner-tier human only ---
    # Uses the REAL username (present for shared-only human chats even though the
    # mount is agent-scope). Agent-scope service sessions (username="") never
    # mount /config — this is the admin-only-task regression guard.
    config_visible = bool(username) and user_role in _OWNER_TIER

    # --- effective default scope for scope-aware MCPs (memory/tasks/...) ---
    if not username:
        # No user owner → agent scope is the only sensible default. This clause
        # WINS over the viewer clamp: an agent-scope session has no user dir, so
        # a viewer-role agent-scope session still defaults to agent.
        eff_default = "agent"
    elif user_role == "viewer" and "user" in available:
        # Viewers can never create agent-scope artifacts (API role-gate 403s);
        # default their tools to user scope so the schema matches the gate.
        eff_default = "user"
    else:
        eff_default = default_scope
    if eff_default not in available:
        eff_default = available[0]

    # --- memory availability (toggle AND the mode offers the scope) ---
    memory_user = master_user and ("user" in available)
    memory_agent = master_agent and ("agent" in available)

    # --- chat-history owner ---
    history_owner = f"agent::{agent_name}" if shared_only else user_sub

    return VisibilityResolution(
        mode=mode_for(collaborative, default_scope),
        collaborative=collaborative,
        default_scope=default_scope,
        available_scopes=available,
        mount_shared=mount_shared,
        memory_user_enabled=memory_user,
        memory_agent_enabled=memory_agent,
        mount_scope=mount_scope,
        mount_username=mount_username,
        config_visible=config_visible,
        effective_default_scope=eff_default,
        is_service=not bool(username),
        history_owner=history_owner,
    )

"""Standard ``OTO_*`` env vars auto-injected on every stdio MCP launch.

Community MCPs that follow the platform convention can read these env vars
to behave correctly without any per-MCP manifest declarations. The set is
intentionally small and stable — adding fields requires a deliberate
contract bump.

Values are sandbox-style virtual paths. Locally bwrap maps them to host
paths; remotely the satellite ``path_translator`` maps them to satellite-
absolute paths (see ``satellite/path_translator.py``).

Empty values are kept (with empty-string values) for non-applicable cells
— e.g. ``OTO_USER_ROOT`` is empty for agent-scoped sessions, ``OTO_CONFIG_DIR``
is empty for viewers. Community MCPs MUST treat empty as "this scope has
no value for this concept" and degrade gracefully.

NOT injected into Docker MCPs: those start once per container and don't
have per-session context. Docker MCPs use the proxy ``/v1/hooks/resolve-path``
API to translate sandbox paths in tool args (see ``api/hooks/hooks.py``).
"""

from __future__ import annotations

from services import path_roles


def build_oto_env(
    *,
    agent_name: str,
    username: str = "",
    user_sub: str = "",
    user_role: str = "",
    platform_role: str = "",
    session_id: str = "",
    memory_user_enabled: bool = True,
    memory_agent_enabled: bool = True,
    default_scope: str = "user",
    task_type: str = "",
    available_scopes: tuple[str, ...] = ("user", "agent"),
    force_config: bool = False,
) -> dict[str, str]:
    """Build the standard ``OTO_*`` env vars dict.

    Args:
        agent_name: agent slug.
        username: session's filesystem-safe username; empty for agent-scoped.
        user_sub: OAuth subject identifier for the session owner; empty for
            agent-scoped sessions (no human owner). Exposed as
            ``OTO_USER_SUB`` so MCPs scoping data per-user (e.g. memory-mcp)
            don't have to decode the session JWT.
        user_role: access level (``"viewer"``/``"manager"``/``"admin"``/``""``).
        platform_role: the session user's PLATFORM role
            (``"admin"``/``"creator"``/``"member"``), empty for sessions
            without a human user. Distinct from ``user_role``, which is the
            per-agent role and flattens platform admins into ``"admin"``.
            MCPs gating on platform capability (e.g. agent creation) read
            this; the proxy endpoint remains the real boundary.
        session_id: current session id (literal — used as-is, not the
            ``{session_id}`` template token).
        memory_user_enabled: effective user-memory toggle (master AND
            per-agent). Exposed as ``OTO_MEMORY_USER_ENABLED``.
        memory_agent_enabled: effective agent-memory toggle. Exposed as
            ``OTO_MEMORY_AGENT_ENABLED``.
        default_scope: per-agent default scope (``"user"`` / ``"agent"``).
            Drives the default scope arg for every scope-aware MCP (memory,
            tasks, notifications, triggers, meetings). The caller is
            responsible for forcing this to ``"agent"`` for sessions without
            a user (phone/task/trigger) — this function doesn't know whether
            the empty username is intentional or missing.
        task_type: task-type label for task sessions; empty string for
            chat / phone / non-task sessions. Generic session metadata any
            MCP may read to distinguish task shapes.

    Returns:
        dict of ``OTO_*`` env var names → values. Always contains all keys
        defined in this builder; some may be empty strings.
    """
    # ``username`` here is the MOUNT username — "" for any agent-scope mount
    # (service sessions AND shared-only human chats), so this derives the mount
    # scope correctly. The REAL attribution identity rides ``user_sub``.
    scope = "user" if username else "agent"

    # Resolve each role independently using the same path_roles logic that
    # drives manifest path_env injection. Lock-step by construction.
    workspace_dir = path_roles.resolve_role(
        "workspace", username=username, user_role=user_role,
    )
    user_root = path_roles.resolve_role(
        "user_root", username=username, user_role=user_role,
    )
    # ``force_config`` lets a Shared-only owner-tier human (agent-mount, so
    # ``username==""`` here) still resolve /config — kept in lock-step with the
    # bwrap mount via the resolver's ``config_visible``.
    config_dir = path_roles.resolve_role(
        "config", username=username, user_role=user_role, force_config=force_config,
    )
    shared_workspace = path_roles.resolve_role(
        "shared_workspace", username=username, user_role=user_role,
    )
    # Knowledge dir resolves to ``/knowledge`` for EVERY session (user-scope
    # and agent-scope alike) — the role ignores ``user_role`` because the
    # bwrap mount decides RW vs RO, and the path is universal.
    knowledge_dir = path_roles.resolve_role(
        "knowledge_dir", username=username, user_role=user_role,
    )

    # Personal-only mode has NO shared workspace + NO shared knowledge — drop
    # both so MCPs never see (or try to write) dirs that aren't mounted.
    # ``available_scopes`` lacking "agent" is exactly the personal-only case.
    if "agent" not in available_scopes:
        shared_workspace = ""
        knowledge_dir = ""

    # Allowed roots: the bwrap mount set (canonical "what dirs can this
    # session access?"). Drop empties before joining.
    roots = [r for r in (user_root, shared_workspace, knowledge_dir, config_dir) if r]
    allowed_roots = ":".join(roots)

    return {
        "OTO_AGENT_NAME": agent_name,
        "OTO_USERNAME": username,
        "OTO_USER_SUB": user_sub,
        "OTO_SCOPE": scope,
        "OTO_ROLE": user_role,
        "OTO_PLATFORM_ROLE": platform_role,
        "OTO_SESSION_ID": session_id,
        "OTO_WORKSPACE_DIR": workspace_dir,
        "OTO_USER_ROOT": user_root,
        "OTO_CONFIG_DIR": config_dir,
        "OTO_KNOWLEDGE_DIR": knowledge_dir,
        "OTO_SHARED_WORKSPACE": shared_workspace,
        "OTO_ALLOWED_ROOTS": allowed_roots,
        # The agent's visibility-mode scopes (":"-joined). Scope-aware MCPs
        # filter their scope enum to this set (e.g. a Personal-only agent
        # offers only "user"; a Shared-only agent only "agent").
        "OTO_AVAILABLE_SCOPES": ":".join(available_scopes),
        # Memory toggles + default-scope + task-type plumbing.
        "OTO_MEMORY_USER_ENABLED": "true" if memory_user_enabled else "false",
        "OTO_MEMORY_AGENT_ENABLED": "true" if memory_agent_enabled else "false",
        "OTO_DEFAULT_SCOPE": default_scope or "user",
        "OTO_TASK_TYPE": task_type or "",
    }


# Multi-value env vars produced by build_oto_env. Used by config builders
# to extend the per-session ``multi_value_envs`` map sent to the satellite,
# so the path translator splits ``OTO_ALLOWED_ROOTS`` correctly. Only the
# joined-list env var qualifies — single-path OTO_* vars don't need it.
OTO_MULTI_VALUE_ENVS: dict[str, str] = {
    "OTO_ALLOWED_ROOTS": ":",
}


def resolve_memory_and_scope(
    agent_name: str,
    *,
    username: str = "",
    user_role: str = "",
) -> tuple[bool, bool, str]:
    """Resolve effective ``(memory_user_enabled, memory_agent_enabled, default_scope)``.

    Thin compatibility shim over :func:`core.session.visibility.resolve_visibility` —
    the single source of truth for mode → scope / memory. Kept because the
    shared ``env_builder`` (and several tests) want just the 3-tuple.

    The composition is unchanged for collaborative agents: a memory scope is
    enabled iff master AND per-agent toggle are on; the default scope is the
    agent's ``default_scope`` clamped for service sessions (→ ``"agent"``) and
    viewers (→ ``"user"``). For the two non-collaborative modes the resolver
    ALSO clamps memory availability to the mode (Personal-only → no agent
    memory; Shared-only → no user memory).
    """
    from core.session.visibility import resolve_visibility
    vis = resolve_visibility(agent_name, username=username, user_role=user_role)
    return (
        vis.memory_user_enabled,
        vis.memory_agent_enabled,
        vis.effective_default_scope,
    )

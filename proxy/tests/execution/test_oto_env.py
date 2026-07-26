"""Tests for the standard OTO_* env var builder (proxy/core/sandbox/oto_env.py).

Covers the (scope × access-level) matrix to confirm each cell yields the
expected env values — particularly OTO_ALLOWED_ROOTS, which must mirror the
bwrap mount set per session class.
"""

from core.sandbox.oto_env import OTO_MULTI_VALUE_ENVS, build_oto_env


# ---------------------------------------------------------------------------
# Manager / Admin user-scoped: full mount set
# ---------------------------------------------------------------------------


def test_manager_user_scoped_all_fields():
    env = build_oto_env(
        agent_name="bot",
        username="alice",
        user_role="manager",
        session_id="sess-1",
    )
    assert env["OTO_AGENT_NAME"] == "bot"
    assert env["OTO_USERNAME"] == "alice"
    assert env["OTO_SCOPE"] == "user"
    assert env["OTO_ROLE"] == "manager"
    assert env["OTO_SESSION_ID"] == "sess-1"
    assert env["OTO_WORKSPACE_DIR"] == "/users/alice/workspace"
    assert env["OTO_USER_ROOT"] == "/users/alice"
    assert env["OTO_CONFIG_DIR"] == "/config"
    assert env["OTO_KNOWLEDGE_DIR"] == "/knowledge"
    assert env["OTO_SHARED_WORKSPACE"] == "/workspace"
    # OTO_ALLOWED_ROOTS includes /knowledge (universal RO mount).
    assert env["OTO_ALLOWED_ROOTS"] == "/users/alice:/workspace:/knowledge:/config"


def test_editor_user_scoped_all_fields():
    """Editor sees workspace + knowledge but NOT config
    (config is owner-only). bwrap mounts workspace RW for editor."""
    env = build_oto_env(
        agent_name="bot",
        username="alice",
        user_role="editor",
        session_id="s",
    )
    assert env["OTO_ROLE"] == "editor"
    # Editor in _PRIVILEGED for shared_workspace → /workspace resolves.
    assert env["OTO_WORKSPACE_DIR"] == "/users/alice/workspace"
    assert env["OTO_USER_ROOT"] == "/users/alice"
    # Editor NOT in _OWNER_TIER → /config empty.
    assert env["OTO_CONFIG_DIR"] == ""
    assert env["OTO_KNOWLEDGE_DIR"] == "/knowledge"
    assert env["OTO_SHARED_WORKSPACE"] == "/workspace"
    # Allowed roots: own user dir + workspace + knowledge (no config).
    assert env["OTO_ALLOWED_ROOTS"] == "/users/alice:/workspace:/knowledge"


def test_admin_user_scoped_same_as_manager():
    env_admin = build_oto_env(
        agent_name="bot", username="alice", user_role="admin",
        session_id="s",
    )
    env_manager = build_oto_env(
        agent_name="bot", username="alice", user_role="manager",
        session_id="s",
    )
    # All path-related fields identical (includes OTO_KNOWLEDGE_DIR).
    for key in (
        "OTO_WORKSPACE_DIR", "OTO_USER_ROOT", "OTO_CONFIG_DIR",
        "OTO_KNOWLEDGE_DIR", "OTO_SHARED_WORKSPACE", "OTO_ALLOWED_ROOTS",
    ):
        assert env_admin[key] == env_manager[key]
    assert env_admin["OTO_ROLE"] == "admin"
    assert env_manager["OTO_ROLE"] == "manager"


# ---------------------------------------------------------------------------
# Viewer user-scoped: only own user dir
# ---------------------------------------------------------------------------


def test_viewer_user_scoped():
    env = build_oto_env(
        agent_name="bot", username="viewer1", user_role="viewer",
        session_id="s",
    )
    assert env["OTO_SCOPE"] == "user"
    assert env["OTO_ROLE"] == "viewer"
    # Viewers DO have workspace via /users/{u}/workspace mount path.
    assert env["OTO_WORKSPACE_DIR"] == "/users/viewer1/workspace"
    # Viewers DO have user_root = /users/{u}.
    assert env["OTO_USER_ROOT"] == "/users/viewer1"
    # Viewers do NOT have agent config or shared workspace via path_env
    # (bwrap mounts them RO but path_env still returns empty for viewer —
    # viewer's path_env is the writable mount set, not the readable one).
    assert env["OTO_CONFIG_DIR"] == ""
    assert env["OTO_SHARED_WORKSPACE"] == ""
    # Viewer DOES have knowledge (universal — bwrap mounts it RO).
    assert env["OTO_KNOWLEDGE_DIR"] == "/knowledge"
    # Allowed roots: /users/{u} + /knowledge (knowledge is universal).
    assert env["OTO_ALLOWED_ROOTS"] == "/users/viewer1:/knowledge"


# ---------------------------------------------------------------------------
# Agent-scoped (tasks/meetings/phone): only /workspace
# ---------------------------------------------------------------------------


def test_agent_scoped_no_username():
    env = build_oto_env(
        agent_name="task-bot", username="", user_role="",
        session_id="task-s",
    )
    assert env["OTO_AGENT_NAME"] == "task-bot"
    assert env["OTO_USERNAME"] == ""
    assert env["OTO_SCOPE"] == "agent"
    assert env["OTO_ROLE"] == ""
    assert env["OTO_WORKSPACE_DIR"] == "/workspace"
    assert env["OTO_USER_ROOT"] == ""
    assert env["OTO_CONFIG_DIR"] == ""
    # Agent-scope sessions DO see /knowledge (RO, owner-curated).
    assert env["OTO_KNOWLEDGE_DIR"] == "/knowledge"
    assert env["OTO_SHARED_WORKSPACE"] == "/workspace"
    # Allowed roots: /workspace + /knowledge.
    assert env["OTO_ALLOWED_ROOTS"] == "/workspace:/knowledge"


def test_agent_scoped_role_ignored():
    """Even if a role is somehow passed, agent-scoped sees /workspace + /knowledge."""
    env = build_oto_env(
        agent_name="b", username="", user_role="manager",
        session_id="s",
    )
    assert env["OTO_USER_ROOT"] == ""
    assert env["OTO_CONFIG_DIR"] == ""  # config requires username
    assert env["OTO_KNOWLEDGE_DIR"] == "/knowledge"
    assert env["OTO_ALLOWED_ROOTS"] == "/workspace:/knowledge"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_unknown_role_user_scoped_treated_as_restrictive():
    """An unrecognized user_role acts like viewer (no shared/config).
    Knowledge is still universal."""
    env = build_oto_env(
        agent_name="b", username="alice", user_role="weird",
        session_id="s",
    )
    assert env["OTO_USER_ROOT"] == "/users/alice"
    assert env["OTO_SHARED_WORKSPACE"] == ""
    assert env["OTO_CONFIG_DIR"] == ""
    assert env["OTO_KNOWLEDGE_DIR"] == "/knowledge"
    assert env["OTO_ALLOWED_ROOTS"] == "/users/alice:/knowledge"


def test_empty_session_id_allowed():
    """Some call sites don't have session_id at config-build time."""
    env = build_oto_env(
        agent_name="b", username="alice", user_role="manager",
        session_id="",
    )
    assert env["OTO_SESSION_ID"] == ""


def test_all_keys_always_present():
    """Every call returns the full set of OTO_* keys, even if some are empty."""
    expected_keys = {
        "OTO_AGENT_NAME", "OTO_USERNAME", "OTO_SCOPE", "OTO_ROLE",
        "OTO_PLATFORM_ROLE",
        "OTO_SESSION_ID",
        "OTO_USER_SUB",
        "OTO_WORKSPACE_DIR", "OTO_USER_ROOT",
        "OTO_CONFIG_DIR", "OTO_KNOWLEDGE_DIR",
        "OTO_SHARED_WORKSPACE", "OTO_ALLOWED_ROOTS",
        # visibility-modes: the agent's mode scopes (":"-joined).
        "OTO_AVAILABLE_SCOPES",
        # v3 additions: memory toggles, default scope, task type.
        "OTO_MEMORY_USER_ENABLED", "OTO_MEMORY_AGENT_ENABLED",
        "OTO_DEFAULT_SCOPE", "OTO_TASK_TYPE",
    }
    for username, role in [
        ("alice", "manager"),
        ("alice", "editor"),
        ("alice", "viewer"),
        ("", ""),
        ("alice", "admin"),
        ("", "manager"),
    ]:
        env = build_oto_env(
            agent_name="b", username=username, user_role=role, session_id="s",
        )
        assert set(env.keys()) == expected_keys


def test_platform_role_is_independent_of_agent_role():
    """OTO_PLATFORM_ROLE carries admin/creator/member; OTO_ROLE stays per-agent.

    Agent creation gates on the PLATFORM role, which the per-agent role can't
    express (a per-agent manager may be a platform member).
    """
    env = build_oto_env(
        agent_name="bot", username="alice", user_role="manager",
        platform_role="creator", session_id="s",
    )
    assert env["OTO_PLATFORM_ROLE"] == "creator"
    assert env["OTO_ROLE"] == "manager"

    # Agent-scope service sessions have no human identity → empty.
    env = build_oto_env(agent_name="bot", username="", user_role="manager", session_id="s")
    assert env["OTO_PLATFORM_ROLE"] == ""


def test_memory_env_resolution():
    """v3: memory toggles + default_scope + task_type all surface."""
    env = build_oto_env(
        agent_name="b", username="alice", user_role="manager", session_id="s",
        memory_user_enabled=False, memory_agent_enabled=True,
        default_scope="agent", task_type="memory_run",
    )
    assert env["OTO_MEMORY_USER_ENABLED"] == "false"
    assert env["OTO_MEMORY_AGENT_ENABLED"] == "true"
    assert env["OTO_DEFAULT_SCOPE"] == "agent"
    assert env["OTO_TASK_TYPE"] == "memory_run"


def test_memory_env_defaults_when_omitted():
    """Sensible defaults when memory kwargs aren't passed: toggles ON,
    scope=user, task_type=''."""
    env = build_oto_env(
        agent_name="b", username="alice", user_role="manager", session_id="s",
    )
    assert env["OTO_MEMORY_USER_ENABLED"] == "true"
    assert env["OTO_MEMORY_AGENT_ENABLED"] == "true"
    assert env["OTO_DEFAULT_SCOPE"] == "user"
    assert env["OTO_TASK_TYPE"] == ""


def test_default_scope_empty_falls_back_to_user():
    """An empty string default_scope falls back to 'user' (the safest scope)."""
    env = build_oto_env(
        agent_name="b", username="alice", user_role="manager", session_id="s",
        default_scope="",
    )
    assert env["OTO_DEFAULT_SCOPE"] == "user"


def test_resolve_memory_and_scope_agent_scope_session(temp_db):
    """No username → forced to default_scope='agent' regardless of agent's row."""
    from core.sandbox.oto_env import resolve_memory_and_scope
    from storage import agent_store
    agent_store.create_agent("pa", "Personal Assistant", default_scope="user")
    # Even though pa's default_scope is "user", a session without a user
    # owner must default to agent.
    mu, ma, ds = resolve_memory_and_scope("pa", username="")
    assert ds == "agent"


def test_resolve_memory_and_scope_respects_agent_default(temp_db):
    from core.sandbox.oto_env import resolve_memory_and_scope
    from storage import agent_store
    agent_store.create_agent("ops", "Ops", default_scope="agent")
    mu, ma, ds = resolve_memory_and_scope("ops", username="alice")
    assert ds == "agent"


def test_resolve_memory_and_scope_master_toggle_off(temp_db):
    from core.sandbox.oto_env import resolve_memory_and_scope
    from storage import agent_store, memory_store
    agent_store.create_agent("pa", "PA")
    memory_store.update_settings(user_memory_enabled=False)
    mu, ma, ds = resolve_memory_and_scope("pa", username="alice")
    assert mu is False
    assert ma is True


def test_user_sub_propagation():
    """OTO_USER_SUB carries the OAuth subject so memory-mcp scopes per-user
    queries without decoding the session JWT. Empty for agent-scope."""
    env = build_oto_env(
        agent_name="b", username="alice", user_sub="auth0|abc123",
        user_role="manager", session_id="s",
    )
    assert env["OTO_USER_SUB"] == "auth0|abc123"
    # Agent-scope: no user_sub.
    env_agent = build_oto_env(
        agent_name="b", username="", user_role="", session_id="s",
    )
    assert env_agent["OTO_USER_SUB"] == ""
    # Defaults to empty when omitted (callers that don't need user_sub
    # routing — e.g. agent-scope sessions — leave it unset).
    env_no_sub = build_oto_env(
        agent_name="b", username="alice", user_role="manager", session_id="s",
    )
    assert env_no_sub["OTO_USER_SUB"] == ""


def test_agent_scope_when_username_empty():
    """An empty mount username (service / Shared-only session) → agent scope."""
    env = build_oto_env(
        agent_name="voice-service", username="", user_role="", session_id="s",
    )
    assert env["OTO_SCOPE"] == "agent"


# ---------------------------------------------------------------------------
# Multi-value envs constant
# ---------------------------------------------------------------------------


def test_oto_multi_value_envs_lists_allowed_roots():
    """OTO_ALLOWED_ROOTS is the colon-joined env that needs satellite split."""
    assert OTO_MULTI_VALUE_ENVS == {"OTO_ALLOWED_ROOTS": ":"}


# ---------------------------------------------------------------------------
# Allowed roots vs accessible_roots multi-value role match
# ---------------------------------------------------------------------------


def test_allowed_roots_matches_path_env_accessible_roots():
    """OTO_ALLOWED_ROOTS produced by oto_env should match what a
    user_root + shared_workspace + knowledge_dir + config multi-value
    path_env produces (order — knowledge before config)."""
    from services.path_roles import resolve_path_env_entry

    decl = {
        "values": [
            {"role": "user_root"},
            {"role": "shared_workspace"},
            {"role": "knowledge_dir"},
            {"role": "config"},
        ],
        "join": ":",
    }

    for username, role in [
        ("alice", "manager"),
        ("alice", "editor"),
        ("alice", "admin"),
        ("alice", "viewer"),
        ("", ""),
    ]:
        env = build_oto_env(
            agent_name="b", username=username, user_role=role, session_id="s",
        )
        decl_resolved = resolve_path_env_entry(decl, username=username, user_role=role)
        assert env["OTO_ALLOWED_ROOTS"] == decl_resolved

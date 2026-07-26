"""Tests for the new ``# Folders`` and ``# Building Agents`` sections.

Agent Context Deep Pass:
- ``# Folders`` — per-role folder map (visible to all session types).
- ``# Building Agents`` — manager/admin on user-scope only.
- Layer gates — direct-llm sessions get no Bash block, no plans-dir
  mention. Subagent paragraph in the task suffix is also layer-gated
  (covered indirectly here via prompt content).
"""

from __future__ import annotations

from auth.path_policy import build_permission_context, SecurityContext
from storage import agent_store


def _ctx(role: str, username: str, agent: str = "pa", display_name: str = "Alice") -> SecurityContext:
    return SecurityContext(
        role=role, username=username, agent=agent, is_admin_agent=False,
        display_name=display_name, email="alice@example.com",
    )


# ---------------------------------------------------------------------------
# # Folders — viewer
# ---------------------------------------------------------------------------

def test_folders_viewer_sees_4_entries_workspace_ro(temp_db):
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="viewer", username="alice"),
        assigned_mcp_names=(),
        execution_path="claude-code-cli",
    )
    folders = text.split("\n# Folders\n")[1].split("\n# File Permissions\n")[0]
    # 4 entries: own workspace + own context + /workspace/ RO + /knowledge/ RO
    assert "/users/alice/workspace/` (RW)" in folders
    assert "/users/alice/context/` (RW)" in folders
    assert "/workspace/` (RO)" in folders
    assert "/knowledge/` (RO)" in folders
    # Viewer does NOT see /config/
    assert "/config/" not in folders


def test_folders_viewer_includes_uploads_paths(temp_db):
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="viewer", username="alice"),
        assigned_mcp_names=(),
        execution_path="claude-code-cli",
    )
    assert "/users/alice/workspace/uploads/photos/" in text
    assert "/users/alice/workspace/uploads/files/" in text


# ---------------------------------------------------------------------------
# # Folders — editor
# ---------------------------------------------------------------------------

def test_folders_editor_sees_workspace_rw(temp_db):
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="editor", username="alice"),
        assigned_mcp_names=(),
        execution_path="claude-code-cli",
    )
    folders = text.split("\n# Folders\n")[1].split("\n# File Permissions\n")[0]
    # /workspace/ is now RW (editor can collaborate)
    assert "/workspace/` (RW)" in folders
    # /knowledge/ stays RO (owner-only)
    assert "/knowledge/` (RO)" in folders
    # Editor still does NOT see /config/
    assert "/config/" not in folders


# ---------------------------------------------------------------------------
# # Folders — manager/admin
# ---------------------------------------------------------------------------

def test_folders_manager_sees_all_5_entries(temp_db):
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="manager", username="alice"),
        assigned_mcp_names=(),
        execution_path="claude-code-cli",
    )
    folders = text.split("\n# Folders\n")[1].split("\n# Building Agents\n")[0]
    assert "/users/alice/workspace/` (RW)" in folders
    assert "/users/alice/context/` (RW)" in folders
    assert "/workspace/` (RW)" in folders
    assert "/knowledge/` (RW)" in folders
    # Manager sees /config/ too
    assert "/config/` (RW)" in folders


def test_folders_admin_same_as_manager(temp_db):
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="admin", username="alice"),
        assigned_mcp_names=(),
        execution_path="claude-code-cli",
    )
    folders = text.split("\n# Folders\n")[1].split("\n# Building Agents\n")[0]
    assert "/config/` (RW)" in folders
    assert "/knowledge/` (RW)" in folders


# ---------------------------------------------------------------------------
# # Folders — agent-scope (no user)
# ---------------------------------------------------------------------------

def test_folders_agent_scope_only_workspace_and_knowledge(temp_db):
    agent_store.create_agent("caller", "Caller", default_scope="agent", collaborative=False)
    ctx = SecurityContext(role="", username="", agent="caller", is_admin_agent=False)
    text = build_permission_context(
        ctx,
        assigned_mcp_names=(),
        execution_path="claude-code-cli",
    )
    folders = text.split("\n# Folders\n")[1].split("\n# File Permissions\n")[0]
    # Only workspace + knowledge — no /users/ at all, no /config/
    assert "/workspace/` (RW)" in folders
    assert "/knowledge/` (RO)" in folders
    assert "/users/" not in folders
    assert "/config/" not in folders
    # Default-workspace pointer.
    assert "Default workspace for this session is `/workspace/`" in folders


# ---------------------------------------------------------------------------
# # Folders — default-workspace pointer
# ---------------------------------------------------------------------------

def test_folders_default_workspace_user_default(temp_db):
    """Personal-leaning agent → default writes go to the user's workspace."""
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="manager", username="alice"),
        assigned_mcp_names=(),
        execution_path="claude-code-cli",
    )
    assert "Default writes for this session go to `/users/alice/workspace/`" in text
    # Mentions the shared workspace's purpose without prescribing a workflow.
    assert "/workspace/`" in text
    assert "shared workspace" in text


def test_folders_default_workspace_agent_default(temp_db):
    """Operational agent → default writes go to the shared workspace."""
    agent_store.create_agent("ops", "Ops", default_scope="agent")
    text = build_permission_context(
        _ctx(role="manager", username="alice", agent="ops"),
        assigned_mcp_names=(),
        execution_path="claude-code-cli",
    )
    assert "Default writes for this session go to `/workspace/`" in text
    assert "operational agent" in text.lower()


# ---------------------------------------------------------------------------
# # Building Agents — manager/admin only
# ---------------------------------------------------------------------------

def test_building_agents_renders_for_manager(temp_db):
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="manager", username="alice"),
        assigned_mcp_names=("agent-config-mcp", "mcps-mcp"),
        execution_path="claude-code-cli",
    )
    assert "# Building Agents" in text
    # Mentions all key folders
    assert "/config/agent.md" in text
    assert "/config/context/" in text
    assert "/knowledge/" in text
    assert "/workspace/" in text
    assert "/users/{u}/" in text


def test_building_agents_mentions_config_mcp_when_enabled(temp_db):
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="manager", username="alice"),
        assigned_mcp_names=("agent-config-mcp", "mcps-mcp"),
        execution_path="claude-code-cli",
    )
    bs = text.split("# Building Agents")[1]
    assert "`agent-config-mcp`" in bs
    assert "`mcps-mcp`" in bs


def test_building_agents_omits_mcps_pointer_when_not_enabled(temp_db):
    """If only agent-config-mcp is enabled, only that gets a pointer."""
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="manager", username="alice"),
        assigned_mcp_names=("agent-config-mcp",),
        execution_path="claude-code-cli",
    )
    bs = text.split("# Building Agents")[1]
    assert "`agent-config-mcp`" in bs
    # mcps-mcp NOT mentioned in the tools-line when disabled.
    assert "use `mcps-mcp`" not in bs


def test_building_agents_skipped_for_editor(temp_db):
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="editor", username="alice"),
        assigned_mcp_names=("agent-config-mcp", "mcps-mcp"),
        execution_path="claude-code-cli",
    )
    assert "# Building Agents" not in text


def test_building_agents_skipped_for_viewer(temp_db):
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="viewer", username="alice"),
        assigned_mcp_names=("agent-config-mcp", "mcps-mcp"),
        execution_path="claude-code-cli",
    )
    assert "# Building Agents" not in text


def test_building_agents_skipped_for_agent_scope(temp_db):
    agent_store.create_agent("caller", "Caller", default_scope="agent", collaborative=False)
    ctx = SecurityContext(role="", username="", agent="caller", is_admin_agent=False)
    text = build_permission_context(
        ctx,
        assigned_mcp_names=("agent-config-mcp", "mcps-mcp"),
        execution_path="claude-code-cli",
    )
    assert "# Building Agents" not in text


def test_building_agents_default_scope_close(temp_db):
    """Closing sentence mentions the agent's actual default_scope."""
    agent_store.create_agent("ops", "Ops", default_scope="agent")
    text = build_permission_context(
        _ctx(role="manager", username="alice", agent="ops"),
        assigned_mcp_names=("agent-config-mcp", "schedules-mcp"),
        execution_path="claude-code-cli",
    )
    bs = text.split("# Building Agents")[1]
    assert "`default_scope` is **agent**" in bs


# ---------------------------------------------------------------------------
# Layer gates — Bash block + plans-dir
# ---------------------------------------------------------------------------

def test_bash_block_present_on_cli(temp_db):
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="manager", username="alice"),
        assigned_mcp_names=(),
        execution_path="claude-code-cli",
    )
    assert "**Bash access**" in text
    # Dev tools advertised
    assert "`python3`" in text
    assert "`pdftotext`" in text


def test_bash_block_present_on_codex(temp_db):
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="manager", username="alice"),
        assigned_mcp_names=(),
        execution_path="codex-cli",
    )
    assert "**Bash access**" in text


def test_bash_block_omitted_on_direct_llm(temp_db):
    """Direct LLM has no built-in Bash tool — entire block dropped."""
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="manager", username="alice"),
        assigned_mcp_names=(),
        execution_path="direct-llm",
    )
    assert "**Bash access**" not in text
    # Other restrictions still appear.
    assert "**Other restrictions:**" in text


def test_bash_block_admin_callout_for_admin(temp_db):
    """Admins see host-touching commands as available; non-admins on local
    sandbox see them as denied. On remote satellites the gating flips
    (admin + manager + editor get them) — covered by environment tests
    below."""
    agent_store.create_agent("pa", "PA", default_scope="user")
    admin_text = build_permission_context(
        _ctx(role="admin", username="alice"),
        assigned_mcp_names=(),
        execution_path="claude-code-cli",
    )
    manager_text = build_permission_context(
        _ctx(role="manager", username="alice"),
        assigned_mcp_names=(),
        execution_path="claude-code-cli",
    )
    assert "available to you as platform admin" in admin_text
    assert "not available on local sandbox sessions" in manager_text


# ---------------------------------------------------------------------------
# # Execution Environment + admin-tier bash gating per environment
# ---------------------------------------------------------------------------

def test_env_local_sandbox_block(temp_db):
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="manager", username="alice"),
        assigned_mcp_names=(),
        execution_path="claude-code-cli",
    )
    assert "# Execution Environment" in text
    assert "local bwrap kernel sandbox" in text


def test_env_admin_remote_block(temp_db):
    """Admin-paired remote satellite gets specific framing + machine label."""
    agent_store.create_agent("ops", "Ops", default_scope="agent")
    ctx = SecurityContext(
        role="manager", username="alice", agent="ops", is_admin_agent=False,
        display_name="Alice", email="a@x.com",
        target_kind="admin_remote", target_label="prod-server-01",
    )
    text = build_permission_context(
        ctx, assigned_mcp_names=(), execution_path="claude-code-cli",
    )
    assert "# Execution Environment" in text
    assert "`prod-server-01`" in text
    assert "paired by the platform admin" in text
    # Host-touching commands open for ops roles on remote satellite.
    assert "available here" in text


def test_env_user_remote_block(temp_db):
    """User-paired remote satellite gets the user's-own-machine framing."""
    agent_store.create_agent("pa", "PA", default_scope="user")
    ctx = SecurityContext(
        role="manager", username="alice", agent="pa", is_admin_agent=False,
        display_name="Alice", email="a@x.com",
        target_kind="user_remote", target_label="alice-laptop",
    )
    text = build_permission_context(
        ctx, assigned_mcp_names=(), execution_path="claude-code-cli",
    )
    assert "# Execution Environment" in text
    assert "`alice-laptop`" in text
    assert "the user's own machine" in text


def test_admin_tier_open_on_remote_for_manager(temp_db):
    """Manager on a remote satellite can run docker (admin tier)."""
    from auth.path_policy import check_tool_access
    ctx = SecurityContext(
        role="manager", username="alice", agent="ops", is_admin_agent=False,
        target_kind="admin_remote", target_label="prod-01",
    )
    decision, _ = check_tool_access(
        "Bash", {"command": "docker compose up -d"}, ctx,
    )
    assert decision.allowed
    assert decision.permission_tier == "admin"


def test_admin_tier_blocked_locally_for_manager(temp_db):
    """Same manager on local sandbox cannot run docker."""
    from auth.path_policy import check_tool_access
    ctx = SecurityContext(
        role="manager", username="alice", agent="ops", is_admin_agent=False,
        target_kind="local", target_label="",
    )
    decision, _ = check_tool_access(
        "Bash", {"command": "docker compose up -d"}, ctx,
    )
    assert not decision.allowed
    assert "local sandbox sessions" in decision.reason


def test_admin_tier_blocked_for_viewer_on_remote(temp_db):
    """Viewers stay restricted on remote satellite — read-only collaborator."""
    from auth.path_policy import check_tool_access
    ctx = SecurityContext(
        role="viewer", username="alice", agent="ops", is_admin_agent=False,
        target_kind="admin_remote", target_label="prod-01",
    )
    decision, _ = check_tool_access(
        "Bash", {"command": "docker ps"}, ctx,
    )
    assert not decision.allowed


def test_plans_dir_mention_only_on_cli(temp_db):
    """Plans dir is a Claude Code CLI feature; codex/direct-llm omit it."""
    agent_store.create_agent("pa", "PA", default_scope="user")
    cli_text = build_permission_context(
        _ctx(role="manager", username="alice"),
        assigned_mcp_names=(),
        execution_path="claude-code-cli",
    )
    codex_text = build_permission_context(
        _ctx(role="manager", username="alice"),
        assigned_mcp_names=(),
        execution_path="codex-cli",
    )
    direct_text = build_permission_context(
        _ctx(role="manager", username="alice"),
        assigned_mcp_names=(),
        execution_path="direct-llm",
    )
    assert "/users/alice/.claude/plans/" in cli_text
    assert "/users/alice/.claude/plans/" not in codex_text
    assert "/users/alice/.claude/plans/" not in direct_text


# ---------------------------------------------------------------------------
# Visibility-modes: Shared-only + Personal-only folders / Building Agents
# ---------------------------------------------------------------------------

def _ctx_mode(role, username, agent, *, session_scope, available_scopes,
              config_visible):
    return SecurityContext(
        role=role, username=username, agent=agent, is_admin_agent=False,
        display_name="Alice", email="alice@example.com",
        session_scope=session_scope, available_scopes=available_scopes,
        config_visible=config_visible,
    )


def test_folders_shared_only_manager(temp_db):
    """Shared-only manager: agent-scope folders, RW knowledge+config, NO /users."""
    agent_store.create_agent("so", "SO", collaborative=False, default_scope="agent")
    text = build_permission_context(
        _ctx_mode("manager", "alice", "so", session_scope="agent",
                  available_scopes=("agent",), config_visible=True),
        assigned_mcp_names=(), execution_path="claude-code-cli",
    )
    folders = text.split("\n# Folders\n")[1].split("\n# File Permissions\n")[0]
    assert "/workspace/` (RW)" in folders
    assert "/knowledge/` (RW)" in folders
    assert "/config/` (RW)" in folders
    assert "/users/" not in folders          # no per-user dirs
    assert "no **personal space**" in folders or "no personal space" in folders


def test_folders_shared_only_viewer_read_only(temp_db):
    agent_store.create_agent("so", "SO", collaborative=False, default_scope="agent")
    text = build_permission_context(
        _ctx_mode("viewer", "bob", "so", session_scope="agent",
                  available_scopes=("agent",), config_visible=False),
        assigned_mcp_names=(), execution_path="claude-code-cli",
    )
    folders = text.split("\n# Folders\n")[1].split("\n# File Permissions\n")[0]
    assert "/workspace/` (RO)" in folders
    assert "/config/" not in folders
    assert "/users/" not in folders


def test_folders_personal_only_drops_shared(temp_db):
    """Personal-only: own dirs + config (manager), NO shared workspace/knowledge."""
    agent_store.create_agent("po", "PO", collaborative=False, default_scope="user")
    text = build_permission_context(
        _ctx_mode("manager", "alice", "po", session_scope="user",
                  available_scopes=("user",), config_visible=True),
        assigned_mcp_names=(), execution_path="claude-code-cli",
    )
    folders = text.split("\n# Folders\n")[1].split("\n# File Permissions\n")[0]
    assert "/users/alice/workspace/` (RW)" in folders
    assert "/config/` (RW)" in folders          # manager still curates persona
    # No shared workspace / knowledge bullets.
    assert "shared workspace" not in folders
    assert "/knowledge/" not in folders
    assert "personal only" in folders


def test_building_agents_shared_only_drops_per_user_bullet(temp_db):
    agent_store.create_agent("so", "SO", collaborative=False, default_scope="agent")
    text = build_permission_context(
        _ctx_mode("manager", "alice", "so", session_scope="agent",
                  available_scopes=("agent",), config_visible=True),
        assigned_mcp_names=("agent-config-mcp",), execution_path="claude-code-cli",
    )
    assert "# Building Agents" in text
    building = text.split("# Building Agents")[1]
    assert "/knowledge/" in building            # shared agent → knowledge bullet
    assert "Per-user files" not in building     # no user scope


def test_building_agents_personal_only_drops_shared_bullets(temp_db):
    agent_store.create_agent("po", "PO", collaborative=False, default_scope="user")
    text = build_permission_context(
        _ctx_mode("manager", "alice", "po", session_scope="user",
                  available_scopes=("user",), config_visible=True),
        assigned_mcp_names=("agent-config-mcp",), execution_path="claude-code-cli",
    )
    building = text.split("# Building Agents")[1]
    assert "Per-user files" in building         # personal-only HAS user dirs
    assert "Shared collaborative output" not in building
    assert "on-demand reference library" not in building

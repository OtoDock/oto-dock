"""Tests for the ``# Execution Scope`` blocks in build_permission_context.

Agent Context Deep Pass: six leaner blocks chosen by
``(session_scope, role, agent.default_scope)``:

- Block A — user-scope, manager/admin, default_scope='user'
- Block B — user-scope, manager/admin, default_scope='agent'
- Block C — user-scope, editor, default_scope='user'
- Block D — user-scope, editor, default_scope='agent'
- Block E — user-scope, viewer (regardless of default_scope)
- Block F — agent-scope session (no username)

The MCP-defaults sentence in each block is now dynamic — only mentions
scope-aware MCPs (`schedules-mcp`, `delegation-mcp`, `notifications-mcp`,
`triggers-mcp`, `meetings-mcp`, `memory-mcp`) that are actually in
``assigned_mcp_names``.
When the tuple is empty, the MCP sentence is omitted entirely. Folder
semantics + knowledge-vs-workspace prose moved to ``# Folders`` and
``# Building Agents`` (covered in test_permission_context_folders.py).
"""

from __future__ import annotations

from auth.path_policy import build_permission_context, SecurityContext
from storage.agents import agent_store


def _ctx(role: str, username: str, agent: str = "pa") -> SecurityContext:
    return SecurityContext(
        role=role, username=username, agent=agent, is_admin_agent=False,
        display_name="Alice Brown", email="alice@example.com",
    )


_ALL_SCOPE_MCPS = (
    "schedules-mcp", "delegation-mcp", "notifications-mcp", "triggers-mcp",
    "meetings-mcp", "memory-mcp",
)


# ---------------------------------------------------------------------------
# Block A — manager/admin on personal-leaning agent
# ---------------------------------------------------------------------------

def test_block_a_manager_personal_assistant(temp_db):
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="manager", username="alice", agent="pa"),
        assigned_mcp_names=_ALL_SCOPE_MCPS,
        execution_path="claude-code-cli",
    )
    assert "# Execution Scope" in text
    assert "user scope" in text
    assert "default to **user scope**" in text
    # All six scope-aware nouns listed (Oxford-comma joined; first word
    # capitalised as a sentence start).
    assert "Tasks, delegated sessions, notifications, triggers, meetings, and memories" in text
    # Override-to-agent guidance present.
    assert 'scope="agent"' in text


def test_block_a_no_scope_aware_mcps_omits_mcp_sentence(temp_db):
    """No scope-aware MCPs → the MCP-defaults sentence is suppressed."""
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="manager", username="alice", agent="pa"),
        assigned_mcp_names=("file-tools-mcp", "display-mcp"),
        execution_path="claude-code-cli",
    )
    assert "# Execution Scope" in text
    assert "user scope" in text
    # No scope-aware MCPs → no "default to" sentence at all.
    assert "default to" not in text.split("# Execution Scope")[1].split("# Folders")[0]


def test_block_a_partial_mcps_uses_subset(temp_db):
    """Only the enabled scope-aware MCPs appear in the dynamic sentence."""
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="manager", username="alice", agent="pa"),
        assigned_mcp_names=("schedules-mcp", "notifications-mcp"),
        execution_path="claude-code-cli",
    )
    assert "Tasks and notifications" in text
    # "triggers"/"meetings"/"memories" must NOT appear in the Execution Scope sentence.
    es = text.split("# Execution Scope")[1].split("# Folders")[0]
    assert "triggers" not in es
    assert "meetings" not in es
    assert "memories" not in es


# ---------------------------------------------------------------------------
# Block B — manager/admin on operational agent
# ---------------------------------------------------------------------------

def test_block_b_manager_system_admin(temp_db):
    agent_store.create_agent("ops", "Ops", default_scope="agent")
    text = build_permission_context(
        _ctx(role="manager", username="alice", agent="ops"),
        assigned_mcp_names=_ALL_SCOPE_MCPS,
        execution_path="claude-code-cli",
    )
    assert "# Execution Scope" in text
    assert "operational agent" in text
    assert "default to **agent scope**" in text
    assert 'scope="user"' in text


def test_block_b_no_scope_aware_mcps_still_marks_operational(temp_db):
    """Even with no scope-aware MCPs, the operational-agent framing stays."""
    agent_store.create_agent("ops", "Ops", default_scope="agent")
    text = build_permission_context(
        _ctx(role="manager", username="alice", agent="ops"),
        assigned_mcp_names=(),
        execution_path="claude-code-cli",
    )
    assert "operational agent" in text


# ---------------------------------------------------------------------------
# Block C — editor on personal-leaning agent
# ---------------------------------------------------------------------------

def test_block_c_editor_personal_assistant(temp_db):
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="editor", username="alice", agent="pa"),
        assigned_mcp_names=_ALL_SCOPE_MCPS,
        execution_path="claude-code-cli",
    )
    assert "# Execution Scope" in text
    assert "(editor)" in text
    assert "default to **user scope**" in text


# ---------------------------------------------------------------------------
# Block D — editor on operational agent
# ---------------------------------------------------------------------------

def test_block_d_editor_system_admin(temp_db):
    agent_store.create_agent("ops", "Ops", default_scope="agent")
    text = build_permission_context(
        _ctx(role="editor", username="alice", agent="ops"),
        assigned_mcp_names=_ALL_SCOPE_MCPS,
        execution_path="claude-code-cli",
    )
    assert "# Execution Scope" in text
    assert "(editor)" in text
    assert "operational agent" in text
    assert "default to **agent scope**" in text


# ---------------------------------------------------------------------------
# Block E — viewer (always user-scope; cannot create agent-scope items)
# ---------------------------------------------------------------------------

def test_block_e_viewer_session(temp_db):
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="viewer", username="alice", agent="pa"),
        assigned_mcp_names=_ALL_SCOPE_MCPS,
        execution_path="claude-code-cli",
    )
    assert "# Execution Scope" in text
    assert "(viewer)" in text
    # Viewers explicitly can't create agent-scope items.
    assert "cannot create agent-scope" in text


def test_block_e_viewer_no_scope_aware_mcps(temp_db):
    """Viewer with no scope-aware MCPs still gets the cannot-create line."""
    agent_store.create_agent("pa", "PA", default_scope="user")
    text = build_permission_context(
        _ctx(role="viewer", username="alice", agent="pa"),
        assigned_mcp_names=(),
        execution_path="claude-code-cli",
    )
    assert "cannot create agent-scope" in text


def test_block_e_viewer_on_operational_agent(temp_db):
    """Viewers always get Block E regardless of agent default_scope."""
    agent_store.create_agent("ops", "Ops", default_scope="agent")
    text = build_permission_context(
        _ctx(role="viewer", username="alice", agent="ops"),
        assigned_mcp_names=_ALL_SCOPE_MCPS,
        execution_path="claude-code-cli",
    )
    assert "(viewer)" in text
    assert "cannot create agent-scope" in text


# ---------------------------------------------------------------------------
# Block F — agent-scope session (no username)
# ---------------------------------------------------------------------------

def test_block_f_agent_scope_session(temp_db):
    agent_store.create_agent("caller", "Caller", default_scope="agent", collaborative=False)
    ctx = SecurityContext(role="", username="", agent="caller", is_admin_agent=False)
    text = build_permission_context(
        ctx,
        assigned_mcp_names=_ALL_SCOPE_MCPS,
        execution_path="claude-code-cli",
    )
    assert "# Execution Scope" in text
    assert "agent scope" in text
    assert "no user owner" in text
    assert "default to **agent scope**" in text
    # Agent-scope sessions have no # Session Context (no human identity).
    assert "# Session Context" not in text


# ---------------------------------------------------------------------------
# Defaults / robustness
# ---------------------------------------------------------------------------

def test_unknown_agent_uses_user_default(temp_db):
    """Missing agent row → safe 'user' default; no crash."""
    text = build_permission_context(
        _ctx(role="manager", username="alice", agent="nonexistent"),
        assigned_mcp_names=_ALL_SCOPE_MCPS,
        execution_path="claude-code-cli",
    )
    # Should render Block A (user default) without throwing.
    assert "# Execution Scope" in text
    assert "user scope" in text
    assert "default to **user scope**" in text


def test_session_context_uses_display_name_when_available(temp_db):
    """display_name preferred over username when set."""
    agent_store.create_agent("pa", "PA", default_scope="user")
    ctx = SecurityContext(
        role="manager", username="alice", agent="pa", is_admin_agent=False,
        display_name="Alice Brown", email="alice@example.com",
    )
    text = build_permission_context(
        ctx,
        assigned_mcp_names=_ALL_SCOPE_MCPS,
        execution_path="claude-code-cli",
    )
    assert "**Alice Brown**" in text
    assert "(alice@example.com)" in text
    assert "**manager**" in text
    # The Execution Scope sentence uses the display name too (not username).
    assert "for Alice Brown" in text


def test_session_context_falls_back_to_username(temp_db):
    """When display_name is empty, username is used in identity."""
    agent_store.create_agent("pa", "PA", default_scope="user")
    ctx = SecurityContext(
        role="manager", username="alice", agent="pa", is_admin_agent=False,
        display_name="", email="",
    )
    text = build_permission_context(
        ctx,
        assigned_mcp_names=_ALL_SCOPE_MCPS,
        execution_path="claude-code-cli",
    )
    assert "**alice**" in text
    # No email parentheses when email is empty.
    assert "(alice@" not in text


# ---------------------------------------------------------------------------
# Visibility-modes: Shared-only + Personal-only human chats (new blocks)
# ---------------------------------------------------------------------------

def _ctx_mode(role, username, agent, *, session_scope, available_scopes,
              config_visible):
    return SecurityContext(
        role=role, username=username, agent=agent, is_admin_agent=False,
        display_name="Alice Brown", email="alice@example.com",
        session_scope=session_scope, available_scopes=available_scopes,
        config_visible=config_visible,
    )


def test_shared_only_manager_scope_block(temp_db):
    """Shared-only human manager: agent-scope mount, but identity preserved."""
    agent_store.create_agent("so", "SO", collaborative=False, default_scope="agent")
    text = build_permission_context(
        _ctx_mode("manager", "alice", "so", session_scope="agent",
                  available_scopes=("agent",), config_visible=True),
        assigned_mcp_names=_ALL_SCOPE_MCPS, execution_path="claude-code-cli",
    )
    assert "# Execution Scope" in text
    assert "shared space" in text
    assert "no personal space" in text
    assert "shared with every user" in text
    # Identity line still names the human (attribution preserved).
    assert "Alice Brown" in text
    # NOT the service Block F.
    assert "no user owner" not in text


def test_shared_only_viewer_scope_block_is_read_only(temp_db):
    agent_store.create_agent("so", "SO", collaborative=False, default_scope="agent")
    text = build_permission_context(
        _ctx_mode("viewer", "bob", "so", session_scope="agent",
                  available_scopes=("agent",), config_visible=False),
        assigned_mcp_names=_ALL_SCOPE_MCPS, execution_path="claude-code-cli",
    )
    assert "shared space" in text
    assert "read-only" in text


def test_personal_only_scope_block(temp_db):
    agent_store.create_agent("po", "PO", collaborative=False, default_scope="user")
    text = build_permission_context(
        _ctx_mode("manager", "alice", "po", session_scope="user",
                  available_scopes=("user",), config_visible=True),
        assigned_mcp_names=_ALL_SCOPE_MCPS, execution_path="claude-code-cli",
    )
    assert "personal space" in text
    assert "no shared space" in text
    assert "private to" in text


def test_collaborative_still_uses_classic_blocks(temp_db):
    """Collaborative agents keep the A–E blocks unchanged (regression)."""
    agent_store.create_agent("ps", "PS", collaborative=True, default_scope="user")
    text = build_permission_context(
        _ctx_mode("manager", "alice", "ps", session_scope="user",
                  available_scopes=("user", "agent"), config_visible=True),
        assigned_mcp_names=_ALL_SCOPE_MCPS, execution_path="claude-code-cli",
    )
    assert "user scope" in text
    assert "shared space" not in text   # not a single-space mode

"""Tests for OTO_DEFAULT_SCOPE end-to-end propagation.

Validates that the per-agent ``default_scope`` reaches the MCP subprocess
env via ``build_oto_env`` for all three config builders (chat, task, the
shared env_builder). Also smokes the MCP-side fallback chain.
"""

from __future__ import annotations

from pathlib import Path

from core.sandbox import oto_env
from storage.agents import agent_store

# Custom MCP servers live at <repo-root>/mcps/custom/. Derive the path from this
# file's location so the tests are portable across checkouts / CI — never a
# hardcoded developer home directory.
from tests._paths import CUSTOM_MCPS as _CUSTOM_MCPS, load_mcp_server


# ---------------------------------------------------------------------------
# Resolver behaviour
# ---------------------------------------------------------------------------

def test_agent_row_drives_default_scope(temp_db):
    agent_store.create_agent("pa", "PA", default_scope="user")
    agent_store.create_agent("ops", "Ops", default_scope="agent")
    _, _, pa_scope = oto_env.resolve_memory_and_scope("pa", username="alice", user_role="manager")
    _, _, ops_scope = oto_env.resolve_memory_and_scope("ops", username="alice", user_role="manager")
    assert pa_scope == "user"
    assert ops_scope == "agent"


def test_agent_scope_session_forces_agent_default(temp_db):
    agent_store.create_agent("pa", "PA", default_scope="user")
    _, _, scope = oto_env.resolve_memory_and_scope("pa", username="")
    assert scope == "agent"


def test_unknown_agent_safe_default(temp_db):
    """A missing agent row → 'user' default; the resolver doesn't blow up."""
    _, _, scope = oto_env.resolve_memory_and_scope("nonexistent", username="alice")
    assert scope == "user"


def test_viewer_clamped_to_user_even_on_agent_default_agent(temp_db):
    """Viewers can never write agent-scope artifacts (API gate returns 403).
    The MCP-facing default must match: clamp to 'user' so the LLM doesn't
    pass scope='agent' and trigger a guaranteed 403 on every create."""
    agent_store.create_agent("ops", "Ops", default_scope="agent")
    _, _, viewer_scope = oto_env.resolve_memory_and_scope(
        "ops", username="alice", user_role="viewer",
    )
    _, _, manager_scope = oto_env.resolve_memory_and_scope(
        "ops", username="alice", user_role="manager",
    )
    _, _, admin_scope = oto_env.resolve_memory_and_scope(
        "ops", username="alice", user_role="admin",
    )
    assert viewer_scope == "user"
    assert manager_scope == "agent"
    assert admin_scope == "agent"


def test_editor_respects_agent_default_scope(temp_db):
    """Editor is NOT clamped — they CAN create agent-scope tasks
    (collaborative tier). default_scope follows the agent's row.
    """
    agent_store.create_agent("ops", "Ops", default_scope="agent")
    agent_store.create_agent("pa", "PA", default_scope="user")
    _, _, ops_scope = oto_env.resolve_memory_and_scope(
        "ops", username="alice", user_role="editor",
    )
    _, _, pa_scope = oto_env.resolve_memory_and_scope(
        "pa", username="alice", user_role="editor",
    )
    # Editor on operational agent → defaults to 'agent' (not clamped).
    assert ops_scope == "agent"
    # Editor on personal-leaning agent → defaults to 'user' (agent's row wins).
    assert pa_scope == "user"


def test_viewer_clamp_does_not_override_agent_session(temp_db):
    """Agent-scope sessions (no user owner) keep the 'agent' default even
    if user_role is somehow passed — the username-empty clause wins."""
    agent_store.create_agent("pa", "PA", default_scope="user")
    _, _, scope = oto_env.resolve_memory_and_scope(
        "pa", username="", user_role="viewer",
    )
    assert scope == "agent"


# ---------------------------------------------------------------------------
# Env propagation
# ---------------------------------------------------------------------------

def test_env_carries_resolved_default_scope(temp_db):
    """End-to-end: agent_store value reaches the env dict."""
    agent_store.create_agent("ops", "Ops", default_scope="agent")
    mu, ma, ds = oto_env.resolve_memory_and_scope("ops", username="alice", user_role="manager")
    env = oto_env.build_oto_env(
        agent_name="ops", username="alice", user_role="manager",
        session_id="s", default_scope=ds,
    )
    assert env["OTO_DEFAULT_SCOPE"] == "agent"


def test_env_task_type_propagates(temp_db):
    """Task sessions get OTO_TASK_TYPE; chat sessions get empty."""
    chat_env = oto_env.build_oto_env(
        agent_name="b", username="alice", user_role="manager", session_id="s",
    )
    task_env = oto_env.build_oto_env(
        agent_name="b", username="alice", user_role="manager", session_id="s",
        task_type="memory_run",
    )
    assert chat_env["OTO_TASK_TYPE"] == ""
    assert task_env["OTO_TASK_TYPE"] == "memory_run"


# ---------------------------------------------------------------------------
# MCP-side fallback chain
# ---------------------------------------------------------------------------

def test_mcp_fallback_chain_picks_oto_default_scope_first(monkeypatch):
    """OTO_DEFAULT_SCOPE wins over PROXY_TASK_SCOPE / OTO_SCOPE."""
    monkeypatch.setenv("OTO_DEFAULT_SCOPE", "agent")
    monkeypatch.setenv("PROXY_TASK_SCOPE", "user")
    monkeypatch.setenv("OTO_SCOPE", "user")
    # The MCP module reads env vars at import time, so reimport to refresh.
    # Use schedules-mcp as the canonical case — same pattern for others.
    server = load_mcp_server(_CUSTOM_MCPS / "schedules-mcp")
    # Module-level DEFAULT_SCOPE constant should reflect OTO_DEFAULT_SCOPE.
    assert server.DEFAULT_SCOPE == "agent"


def test_mcp_fallback_to_oto_scope_when_no_default(monkeypatch):
    """Without OTO_DEFAULT_SCOPE / PROXY_TASK_SCOPE → falls back to OTO_SCOPE."""
    monkeypatch.delenv("OTO_DEFAULT_SCOPE", raising=False)
    monkeypatch.delenv("PROXY_TASK_SCOPE", raising=False)
    monkeypatch.setenv("OTO_SCOPE", "agent")
    server = load_mcp_server(_CUSTOM_MCPS / "schedules-mcp")
    assert server.DEFAULT_SCOPE == "agent"


# ---------------------------------------------------------------------------
# Tool-schema embedding — the LLM-facing default. Regression for the bug
# where the JSON inputSchema hardcoded ``"default": "user"`` for the scope
# arg, causing the LLM to pass ``scope="user"`` explicitly and override the
# env-var fallback. The default MUST follow the resolved DEFAULT_SCOPE.
# ---------------------------------------------------------------------------

def _reload_mcp_server(mcp_dir: Path):
    """Reimport an MCP server.py with current env state."""
    return load_mcp_server(mcp_dir)


def _scope_property(tool) -> dict:
    return tool.inputSchema["properties"]["scope"]


def test_schedules_mcp_schema_default_follows_oto_default_scope(monkeypatch):
    """schedules-mcp: both create_scheduled_task + create_one_time_task schemas
    must embed the resolved DEFAULT_SCOPE, not a hardcoded 'user'."""
    import asyncio
    monkeypatch.setenv("OTO_DEFAULT_SCOPE", "agent")
    server = _reload_mcp_server(_CUSTOM_MCPS / "schedules-mcp")
    tools = asyncio.run(server.list_tools())
    by_name = {t.name: t for t in tools}
    assert _scope_property(by_name["create_scheduled_task"])["default"] == "agent"
    assert _scope_property(by_name["create_one_time_task"])["default"] == "agent"
    # Description should also reflect the resolved default so the LLM has a
    # consistent story between schema and prose.
    assert "agent" in _scope_property(by_name["create_one_time_task"])["description"]


def test_notifications_mcp_schema_default_follows_oto_default_scope(monkeypatch):
    """notifications-mcp create_notification scope default must follow env."""
    import asyncio
    monkeypatch.setenv("OTO_DEFAULT_SCOPE", "agent")
    server = _reload_mcp_server(_CUSTOM_MCPS / "notifications-mcp")
    tools = asyncio.run(server.list_tools())
    by_name = {t.name: t for t in tools}
    assert _scope_property(by_name["create_notification"])["default"] == "agent"


def test_triggers_mcp_schema_default_follows_oto_default_scope(monkeypatch):
    """triggers-mcp create_trigger scope default must follow env. Same fix
    covers vendor-subscribed triggers since they go through the
    same create_trigger tool."""
    import asyncio
    monkeypatch.setenv("OTO_DEFAULT_SCOPE", "agent")
    server = _reload_mcp_server(_CUSTOM_MCPS / "triggers-mcp")
    tools = asyncio.run(server.list_tools())
    by_name = {t.name: t for t in tools}
    assert _scope_property(by_name["create_trigger"])["default"] == "agent"


def test_contributor_clamped_to_user_like_a_viewer(temp_db):
    """A contributor writes the shared workspace but creates no agent-scope
    artifact (the editor tier's API gate 403s), so the MCP-facing default
    clamps to 'user' exactly like a viewer's."""
    agent_store.create_agent("ops", "Ops", default_scope="agent")
    _, _, scope = oto_env.resolve_memory_and_scope(
        "ops", username="alice", user_role="contributor",
    )
    assert scope == "user"

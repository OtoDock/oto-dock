"""schedules-mcp scope default: the advertised schema and the create
handlers must agree.

Regression: both create handlers fell back to a literal ``"user"`` while the
schema advertised ``SCOPE_DEFAULT`` (the agent's own default, clamped to the
scopes its visibility mode offers). On a SHARED-ONLY agent — which offers
``agent`` only — an LLM that trusted the schema and omitted ``scope`` got a
400 "This agent does not support 'user'-scoped tasks". That is every
shared-only agent (CEO / department heads) unable to schedule anything.

``notifications-mcp`` already resolved this correctly
(``args.get("scope") or _DEFAULT_SCOPE``); this pins the same contract here.
"""

from __future__ import annotations

import os

from tests._paths import CUSTOM_MCPS, load_mcp_server

_MCP_DIR = CUSTOM_MCPS / "schedules-mcp"

_SCOPE_ENV = (
    "SCHEDULES_MCP_AGENT", "OTO_AVAILABLE_SCOPES", "OTO_DEFAULT_SCOPE",
    "OTO_SCOPE", "PROXY_TASK_SCOPE", "PROXY_URL", "PROXY_API_KEY",
)


def _load(env: dict[str, str]):
    """Reload server.py under a fresh env — the scope constants are computed
    at import time, so each shape needs its own load."""
    saved = {k: os.environ.get(k) for k in _SCOPE_ENV}
    try:
        for k in _SCOPE_ENV:
            os.environ.pop(k, None)
        os.environ.update(env)
        return load_mcp_server(_MCP_DIR)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _scope_prop(mod, tool_name: str) -> dict:
    """The `scope` property of a tool's inputSchema."""
    import asyncio
    tools = asyncio.run(mod.list_tools())
    tool = next(t for t in tools if t.name == tool_name)
    return tool.inputSchema["properties"]["scope"]


class TestSharedOnlyAgent:
    """Visibility mode offers ONLY agent scope."""

    def _mod(self):
        return _load({
            "SCHEDULES_MCP_AGENT": "ceo",
            "OTO_AVAILABLE_SCOPES": "agent",
            "OTO_DEFAULT_SCOPE": "agent",
        })

    def test_schema_offers_agent_only(self):
        mod = self._mod()
        prop = _scope_prop(mod, "create_scheduled_task")
        assert prop["enum"] == ["agent"]
        assert prop["default"] == "agent"

    def test_handler_default_matches_the_schema(self):
        """The bug: SCOPE_DEFAULT said 'agent', the handler said 'user'."""
        mod = self._mod()
        assert mod.SCOPE_DEFAULT == "agent"
        # An omitted scope must resolve to what the schema promised, never
        # to a scope the agent cannot use.
        assert ({}.get("scope") or mod.SCOPE_DEFAULT) == "agent"

    def test_user_scope_never_reachable(self):
        mod = self._mod()
        assert "user" not in mod.AVAILABLE_SCOPES
        assert mod.SCOPE_DEFAULT in mod.AVAILABLE_SCOPES


class TestPersonalOnlyAgent:
    """Visibility mode offers ONLY user scope — the mirror case."""

    def test_defaults_to_user(self):
        mod = _load({
            "SCHEDULES_MCP_AGENT": "pa",
            "OTO_AVAILABLE_SCOPES": "user",
            "OTO_DEFAULT_SCOPE": "user",
        })
        assert mod.SCOPE_DEFAULT == "user"
        assert _scope_prop(mod, "create_one_time_task")["enum"] == ["user"]


class TestCollaborativeAgent:
    """Both scopes offered — the agent's own default wins."""

    def test_agent_default_wins(self):
        mod = _load({
            "SCHEDULES_MCP_AGENT": "dev",
            "OTO_AVAILABLE_SCOPES": "user:agent",
            "OTO_DEFAULT_SCOPE": "agent",
        })
        assert mod.SCOPE_DEFAULT == "agent"
        assert set(mod.AVAILABLE_SCOPES) == {"user", "agent"}

    def test_falls_back_to_user_when_unset(self):
        """No env at all (legacy / pre-visibility-modes): both scopes, user
        first — unchanged behaviour for existing installs."""
        mod = _load({"SCHEDULES_MCP_AGENT": "dev"})
        assert mod.AVAILABLE_SCOPES == ["user", "agent"]
        assert mod.SCOPE_DEFAULT == "user"


def test_no_handler_hardcodes_user_scope():
    """Guard the actual defect shape: a literal "user" fallback anywhere in
    the create bodies silently re-breaks shared-only agents."""
    src = (_MCP_DIR / "server.py").read_text()
    assert 'arguments.get("scope", "user")' not in src
    assert "arguments.get('scope', 'user')" not in src

"""The engines are told the functions a limit leaves out (``tool_limits.engine_pairs``):
one rule for Codex's walls and the Direct tool list, the outside grants kept
beside the limits; every engine declares that it hides them."""

from __future__ import annotations

from types import SimpleNamespace

from core.layers.codex.spawn import _CodexSpawn
from core.session.session_manager import get_all_layers
from services.mcp import tool_limits


def test_engine_pairs_merge_an_outside_sessions_grants_with_the_limits():
    chat = SimpleNamespace(principal="user", outside_grants=(("x", None),), tool_limits=(("mail", ("read",)),))
    assert tool_limits.engine_pairs(chat) == (("mail", ("read",)),)
    outside = SimpleNamespace(principal="external", outside_grants=(("crm", ("find",)), ("calls", None)),
                              tool_limits=())
    assert tool_limits.engine_pairs(outside) == (("crm", ("find",)), ("calls", None))
    assert tool_limits.engine_pairs(None) == ()


def test_codexs_walls_carry_the_limits_for_every_session():
    walls = _CodexSpawn._tool_walls(SimpleNamespace(
        principal="user", no_shell=False, no_web=False, tool_limits=(("mail", ("read",)),)))
    assert walls == {"no_shell": False, "web_off": False, "tool_grants": (("mail", ("read",)),), "apps_off": False}


def test_one_pair_per_server_reaches_every_reader_the_same(monkeypatch):
    """A manager's row on the self-config tool plus a run's own pair: the
    hook, Codex's TOML, the Direct tool list and Claude's argv all see one
    pair, what both allow."""
    from core.layers.codex.mcp_toml import _inject_enabled_tools_toml
    from core.layers.direct.session import _granted
    from services.mcp import mcp_permissions
    monkeypatch.setattr(mcp_permissions, "_hints", {"agent-config-mcp": {
        "get_agent_config": {}, "update_persona": {}, "reload_session": {}, "switch_account": {}}})
    monkeypatch.setattr(mcp_permissions, "_hints_loaded", True)
    manager = (("agent-config-mcp", ("get_agent_config", "update_persona", "reload_session", "switch_account")),)
    pairs = tool_limits.merged(manager, tool_limits.TASK_SESSION_LIMIT)
    assert pairs == (("agent-config-mcp", ("reload_session", "switch_account")),)
    ctx = SimpleNamespace(principal="user", no_shell=False, no_web=False, tool_limits=pairs)
    assert not tool_limits.allows(ctx.tool_limits, "agent-config-mcp", "update_persona")
    toml = _inject_enabled_tools_toml('[mcp_servers.agent-config-mcp]\ncommand = "x"\n', _CodexSpawn._tool_walls(ctx)["tool_grants"])
    assert 'enabled_tools = ["reload_session", "switch_account"]' in toml
    limits = {key: set(names) for key, names in tool_limits.engine_pairs(ctx) if names is not None}
    assert not _granted("mcp__agent-config-mcp__update_persona", limits)
    assert _granted("mcp__agent-config-mcp__switch_account", limits)
    denied = get_all_layers()["claude-code-cli"].session_denied_tools(external=False, read_only=False, ctx=ctx)
    assert {"mcp__agent-config-mcp__update_persona", "mcp__agent-config-mcp__get_agent_config"} <= set(denied)
    assert "mcp__agent-config-mcp__switch_account" not in denied
    mcp_permissions.reset_cache()


def test_every_engine_declares_that_it_hides_the_functions_left_off():
    for path, layer in get_all_layers().items():
        assert layer.capabilities.behaviour.hides_denied_tools is True, path
        assert layer.capabilities.to_dict()["behaviour"]["hides_denied_tools"] is True, path

"""services/mcp_updater.mcp_in_use — unions the Direct (precise) signal and the
agents of the sessions on machines (agent→MCP), used to defer in-use docker
MCP updates."""

import asyncio
from unittest.mock import MagicMock

import pytest

from services.mcp import mcp_updater


class _Manifest:
    def __init__(self, name):
        self.name = name


def _patch_layers(monkeypatch, *, direct, machine_sessions, agent_mcps):
    """``direct``: the MCP names Direct LLM's live sessions started.
    ``machine_sessions``: ``(agent, engine)`` of each session the remote
    layer holds."""
    import core.layers.direct.mcp as dm
    from core.remote.remote_execution import RemoteExecutionLayer
    from core.remote.remote_session_info import RemoteSessionInfo
    from core.session import session_manager

    async def _direct():
        return set(direct)

    remote = RemoteExecutionLayer(MagicMock())
    for i, (agent, engine) in enumerate(machine_sessions):
        remote._sessions[f"s-{i}"] = RemoteSessionInfo(
            session_id=f"s-{i}", machine_id="m-1", agent_name=agent,
            execution_path=engine, event_queue=asyncio.Queue())

    monkeypatch.setattr(dm.mcp_pool, "active_mcp_names", _direct)
    monkeypatch.setattr(session_manager, "_remote_layer", remote)
    monkeypatch.setattr(
        mcp_updater.mcp_registry, "get_agent_mcps",
        lambda agent: [_Manifest(n) for n in agent_mcps.get(agent, [])],
    )


@pytest.mark.asyncio
async def test_direct_layer_precise_hit(monkeypatch):
    _patch_layers(monkeypatch, direct={"notes"}, machine_sessions=[], agent_mcps={})
    assert await mcp_updater.mcp_in_use("notes") is True
    assert await mcp_updater.mcp_in_use("other") is False


@pytest.mark.asyncio
async def test_a_claude_code_session_on_a_machine_maps_its_agent_to_its_mcps(monkeypatch):
    _patch_layers(monkeypatch, direct=set(), machine_sessions=[("assistant", "claude-code-cli")],
                  agent_mcps={"assistant": ["slack", "github"]})
    assert await mcp_updater.mcp_in_use("slack") is True
    assert await mcp_updater.mcp_in_use("notion") is False


@pytest.mark.asyncio
async def test_a_codex_session_on_a_machine_maps_its_agent_to_its_mcps(monkeypatch):
    _patch_layers(monkeypatch, direct=set(), machine_sessions=[("coder", "codex-cli")],
                  agent_mcps={"coder": ["notes"]})
    assert await mcp_updater.mcp_in_use("notes") is True


@pytest.mark.asyncio
async def test_a_docker_tool_is_in_use_only_while_its_door_says_so(monkeypatch):
    """Every call to a Docker tool passes its door: a live session whose agent
    has the tool is no use of it by itself."""
    from types import SimpleNamespace

    from services.containers import state
    _patch_layers(monkeypatch, direct={"file-tools"}, machine_sessions=[("coder", "codex-cli")],
                  agent_mcps={"coder": ["file-tools"]})
    tool = SimpleNamespace(name="file-tools", server=SimpleNamespace(runtime="docker"))
    monkeypatch.setattr(mcp_updater.mcp_registry, "get_manifest", lambda name: tool if name == "file-tools" else None)
    state.reset_for_tests()
    assert await mcp_updater.mcp_in_use("file-tools") is False
    state.of("file-tools").in_flight = 1
    try:
        assert await mcp_updater.mcp_in_use("file-tools") is True
    finally:
        state.reset_for_tests()


@pytest.mark.asyncio
async def test_nothing_live_means_free(monkeypatch):
    _patch_layers(monkeypatch, direct=set(), machine_sessions=[], agent_mcps={})
    assert await mcp_updater.mcp_in_use("anything") is False

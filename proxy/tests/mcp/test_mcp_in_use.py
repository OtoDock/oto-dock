"""services/mcp_updater.mcp_in_use — unions the Direct (precise), CLI and Codex
(agent→MCP) live-session signals used to defer in-use docker MCP updates."""

import pytest

from services.mcp import mcp_updater


class _Manifest:
    def __init__(self, name):
        self.name = name


def _patch_layers(monkeypatch, *, direct, cli_agents, codex_agents, agent_mcps):
    import core.layers.cli.session as cli_s
    import core.layers.codex.session as codex_s
    import core.layers.direct.mcp as dm

    async def _direct():
        return set(direct)

    async def _cli():
        return set(cli_agents)

    async def _codex():
        return set(codex_agents)

    monkeypatch.setattr(dm.mcp_pool, "active_mcp_names", _direct)
    monkeypatch.setattr(cli_s, "active_agent_names", _cli)
    monkeypatch.setattr(codex_s, "active_agent_names", _codex)
    monkeypatch.setattr(
        mcp_updater.mcp_registry, "get_agent_mcps",
        lambda agent: [_Manifest(n) for n in agent_mcps.get(agent, [])],
    )


@pytest.mark.asyncio
async def test_direct_layer_precise_hit(monkeypatch):
    _patch_layers(monkeypatch, direct={"file-tools"}, cli_agents=set(),
                  codex_agents=set(), agent_mcps={})
    assert await mcp_updater.mcp_in_use("file-tools") is True
    assert await mcp_updater.mcp_in_use("other") is False


@pytest.mark.asyncio
async def test_cli_agent_maps_to_mcp(monkeypatch):
    _patch_layers(monkeypatch, direct=set(), cli_agents={"assistant"},
                  codex_agents=set(), agent_mcps={"assistant": ["slack", "github"]})
    assert await mcp_updater.mcp_in_use("slack") is True
    assert await mcp_updater.mcp_in_use("notion") is False


@pytest.mark.asyncio
async def test_codex_agent_maps_to_mcp(monkeypatch):
    _patch_layers(monkeypatch, direct=set(), cli_agents=set(),
                  codex_agents={"coder"}, agent_mcps={"coder": ["file-tools"]})
    assert await mcp_updater.mcp_in_use("file-tools") is True


@pytest.mark.asyncio
async def test_nothing_live_means_free(monkeypatch):
    _patch_layers(monkeypatch, direct=set(), cli_agents=set(),
                  codex_agents=set(), agent_mcps={})
    assert await mcp_updater.mcp_in_use("anything") is False

"""The tools of a Direct-LLM session: a call connects what a chat connects
for its identity, the HTTP tools in their own container and the
vendor-hosted ones included, with the chat's call cap, so a caller can ask
for a file to be made or edited; an outside caller still gets only what a
manager picked. A call's HTTP tool that does not answer within a short bound
is left out, so the call starts. The app buttons' executor keeps its own
60 s cap."""

from __future__ import annotations

import inspect
import json

import pytest

from core.layers.direct import mcp as mcpmod


class _FakeConn:
    seen: list = []

    def __init__(self, name, config, **kw):
        self.name = name
        self.config = config
        self.tools = []
        self.dead = False
        _FakeConn.seen.append((name, kw.get("tool_timeout"), kw.get("start_timeout")))

    async def start(self):
        pass

    async def rest_until_called(self):
        return False


@pytest.fixture
def conns(monkeypatch):
    _FakeConn.seen = []
    monkeypatch.setattr(mcpmod, "MCPServerConnection", _FakeConn)
    return _FakeConn.seen


def _config(tmp_path, servers: dict):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": servers}))
    return cfg


SERVERS = {
    "local": {"type": "stdio", "command": "x"},
    "sidecar": {"type": "http", "url": "http://127.0.0.1:1/mcp"},
    "vendor": {"type": "sse", "url": "https://mcp.example.com/sse"},
}


@pytest.mark.asyncio
async def test_a_call_starts_every_tool_a_chat_starts_with_the_chats_cap(conns, tmp_path):
    cfg = _config(tmp_path, SERVERS)
    for phone in (False, True):
        conns.clear()
        mgr = mcpmod.AgentMCPManager("agent-x", session_id=f"s-{phone}", prebuilt_config=(cfg, {}),
                                     phone_mode=phone, tool_timeout=mcpmod.TOOL_CALL_TIMEOUT_CHAT)
        await mgr._start_impl()
        assert sorted(n for n, _t, _s in conns) == ["local", "sidecar", "vendor"]
        assert {t for _n, t, _s in conns} == {300}


@pytest.mark.asyncio
async def test_a_calls_http_tool_starts_within_a_shorter_bound(conns, tmp_path):
    cfg = _config(tmp_path, SERVERS)
    call = mcpmod.AgentMCPManager("agent-x", session_id="s-call", prebuilt_config=(cfg, {}), phone_mode=True)
    await call._start_impl()
    # The connection keeps MCP_START_TIMEOUT_S for a stdio server whatever it is handed.
    assert {s for _n, _t, s in conns} == {mcpmod.PHONE_HTTP_START_S}
    assert mcpmod.PHONE_HTTP_START_S < mcpmod.MCP_START_TIMEOUT_S
    conns.clear()
    chat = mcpmod.AgentMCPManager("agent-x", session_id="s-chat", prebuilt_config=(cfg, {}))
    await chat._start_impl()
    assert {s for _n, _t, s in conns} == {None}


@pytest.mark.asyncio
async def test_a_tool_that_does_not_answer_in_its_bound_is_left_out(monkeypatch):
    import asyncio
    conn = mcpmod.MCPServerConnection("slow", {"type": "http", "url": "http://127.0.0.1:1/mcp"},
                                      start_timeout=0.05)

    async def _never(*a, **k):
        await asyncio.sleep(30)
    monkeypatch.setattr(conn, "_start_remote", _never)
    await asyncio.wait_for(conn.start(), timeout=5)
    assert conn.tools == [] and conn.session is None
    await asyncio.wait_for(conn.close(), timeout=5)
    # A stdio server keeps the long bound on a call.
    local = mcpmod.MCPServerConnection("local", {"type": "stdio", "command": "x"}, start_timeout=0.05)
    monkeypatch.setattr(mcpmod, "MCP_START_TIMEOUT_S", 0.3)
    started: list = []

    async def _slow_stdio(*a, **k):
        await asyncio.sleep(0.1)
        started.append(True)
        raise RuntimeError("stop after the bound held")
    monkeypatch.setattr(local, "_start_stdio", _slow_stdio)
    await asyncio.wait_for(local.start(), timeout=5)
    assert started == [True]
    await asyncio.wait_for(local.close(), timeout=5)


@pytest.mark.asyncio
async def test_an_outside_call_starts_only_what_was_picked(conns, monkeypatch, tmp_path):
    """The Direct manager builds through the one rule: an HTTP tool nobody
    picked never reaches the call, a picked one does, with the channel's
    own call tools."""
    from services.mcp import mcp_registry
    from services.mcp.outside_picks import OutsidePicks, ToolPick
    from tests.mcp.test_mcp_broker_activation import _FakeManifest, _stub_assembly
    manifests = [_FakeManifest(n) for n in ("file-tools", "video-tools", "crm", "call-control-mcp")]
    _stub_assembly(monkeypatch, manifests, env_by_mcp={}, tmp_path=tmp_path, server_entries={
        "file-tools": {"type": "http", "url": "http://127.0.0.1:1/mcp"},
        "video-tools": {"type": "http", "url": "http://127.0.0.1:2/mcp"},
    })
    picks = OutsidePicks(tools={"file-tools": ToolPick("read_write")}, channel=frozenset({"call-control-mcp"}))
    path, _env, _excluded, bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, phone_mode=True, external=True, outside_picks=picks)
    mgr = mcpmod.AgentMCPManager("agent", session_id="s-out", prebuilt_config=(path, bundles),
                                 phone_mode=True)
    await mgr._start_impl()
    assert sorted(n for n, _t, _s in conns) == ["call-control-mcp", "file-tools"]


@pytest.mark.asyncio
async def test_create_direct_session_gives_a_call_the_chats_cap(monkeypatch):
    from core.layers.direct import session as sess
    seen: dict = {}

    class _Mgr:
        servers: dict = {}

        def get_tools(self):
            return []

    async def fake_get_or_create(session_id, agent_name, **kw):
        seen.update(kw)
        return _Mgr()

    monkeypatch.setattr(sess.mcp_pool, "get_or_create", fake_get_or_create)
    try:
        await sess.create_direct_session("s-call", "ag", system_prompt="P", phone_mode=True)
        assert seen["tool_timeout"] == mcpmod.TOOL_CALL_TIMEOUT_CHAT and seen["phone_mode"] is True
        assert "enable_http_transport" not in seen
    finally:
        sess._direct_sessions.pop("s-call", None)


def test_no_builder_leaves_a_calls_http_tools_out_of_its_prompt():
    """The prompt describes the tools the call connects: no builder takes a
    switch that drops the HTTP tools."""
    import config as app_config
    from services.mcp import mcp_agent_skills, mcp_registry
    for fn in (app_config.build_agent_prompt, mcp_registry.build_available_mcps_section,
               mcp_registry.get_skills_for_agent, mcp_registry.get_skill_catalog_for_agent,
               mcp_agent_skills.get_agent_mcps_for_prompt if hasattr(mcp_agent_skills, "get_agent_mcps_for_prompt")
               else mcp_registry.get_skills_for_agent):
        assert "skip_http_mcps" not in inspect.signature(fn).parameters, fn


def test_the_engines_declare_no_call_only_tool_rule():
    from core.engine_records import BehaviourProfile
    assert "phone_http_mcps" not in BehaviourProfile.__dataclass_fields__


@pytest.mark.asyncio
async def test_create_direct_session_hands_the_builders_config_to_the_pool(monkeypatch):
    from core.layers.direct import session as sess
    seen: dict = {}

    class _Mgr:
        servers: dict = {}

        def get_tools(self):
            return []

    async def fake_get_or_create(session_id, agent_name, **kw):
        seen.update(kw)
        return _Mgr()

    monkeypatch.setattr(sess.mcp_pool, "get_or_create", fake_get_or_create)
    try:
        s = await sess.create_direct_session(
            "s-gate", "ag", system_prompt="P", prebuilt_config=("path", {"b": 1}), machine_id="compute-1",
        )
        assert s.session_id == "s-gate"
        assert seen["prebuilt_config"] == ("path", {"b": 1}) and seen["machine_id"] == "compute-1"
    finally:
        sess._direct_sessions.pop("s-gate", None)


@pytest.mark.asyncio
async def test_pool_passes_the_cap_to_a_new_manager(monkeypatch):
    created: list = []

    class _Mgr:
        def __init__(self, agent_name, **kw):
            self.agent_name = agent_name
            self.kw = kw
            self.last_activity = 0.0
            created.append(self)

        def tools_lost(self):
            return False

        async def start(self):
            pass

    monkeypatch.setattr(mcpmod, "AgentMCPManager", _Mgr)
    pool = mcpmod.MCPPool()
    mgr = await pool.get_or_create("sess-1", "agent-x", tool_timeout=300)
    assert mgr.kw["tool_timeout"] == 300
    again = await pool.get_or_create("sess-1", "agent-x")
    assert again is mgr and len(created) == 1  # existing manager reused as-is


def test_connection_call_uses_its_own_cap():
    """The app buttons' executor keeps the 60 s default."""
    assert mcpmod.TOOL_CALL_TIMEOUT == 60 and mcpmod.TOOL_CALL_TIMEOUT_CHAT == 300
    conn = mcpmod.MCPServerConnection("x", {"type": "stdio"}, tool_timeout=7)
    assert conn.tool_timeout == 7
    assert mcpmod.MCPServerConnection("y", {"type": "stdio"}).tool_timeout == 60

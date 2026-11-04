"""The Direct LLM pool starts none of the MCPs a session's builder left out.
The CLI engines never read an excluded MCP from the config file the builder
wrote; the Direct manager rebuilds the agent's set in process, knowing the
phone and external facts alone, so a task's, a meeting's and a
``shared_chat`` session's exclusions reach it by name
(``AgentConfig.excluded_mcps``, mapped to the config's server keys)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.execution_layer import AgentConfig
from core.layers.direct import layer as direct_layer, mcp as mcpmod
from tests.fixtures.loop_builder_rig import AGENT, SHARED, install, run

EXCLUDED = {"agent-config-mcp": "Excluded in task mode", "settings-mcp": "Excluded in shared_chat mode"}


@pytest.fixture
def rig(temp_db, monkeypatch, tmp_path):
    rec = install(monkeypatch, tmp_path)
    from services.mcp import mcp_registry

    def _mcp_build(*args, **kwargs):
        rec.calls.append({"call": "build_session_mcp_config", "args": list(args), "kwargs": dict(kwargs)})
        return (None, {}, dict(EXCLUDED), {}, set())

    monkeypatch.setattr(mcp_registry, "build_session_mcp_config", _mcp_build)
    return rec


# ── the builders carry the names ───────────────────────────────────────────

def test_a_task_a_meeting_and_a_shared_only_chat_carry_the_builders_exclusions(rig):
    from core.config.config_builder import build_agent_config
    from core.config.task_config_builder import build_task_agent_config
    from services.meetings.meeting_context import build_meeting_agent_config
    from storage import database as db
    from storage.identity import db_users

    task = SimpleNamespace(scope="agent", created_by="user-admin", task_type="scheduled",
                           notification_mode="manual", name="t", id="dyn-excl")
    cfg = run(build_task_agent_config(AGENT, task, "s-task"))
    assert cfg.excluded_mcps == frozenset(EXCLUDED)

    meeting = {"id": "meet-excl", "scope": "agent", "created_by": "user-manager"}
    cfg = run(build_meeting_agent_config(AGENT, meeting, "s-meet"))
    assert cfg.excluded_mcps == frozenset(EXCLUDED)

    db_users.add_user_agent("user-manager", SHARED, "editor", "user-admin")
    cfg = run(build_agent_config(SHARED, db.get_user("user-manager"), "user-manager", "editor",
                                 client_type="dashboard", session_id="s-shared"))
    assert cfg.excluded_mcps == frozenset(EXCLUDED)
    assert isinstance(AgentConfig(agent_name="x").excluded_mcps, frozenset)


# ── the layer maps them to server keys and hands them to the pool ──────────

def test_the_names_map_to_the_configs_server_keys(monkeypatch):
    from services.mcp import mcp_registry
    from tests.mcp.test_mcp_broker_activation import _FakeManifest
    renamed = _FakeManifest("browser-mcp")
    renamed.server_name = "local"
    monkeypatch.setitem(mcp_registry._manifests, "browser-mcp", renamed)
    monkeypatch.setitem(mcp_registry._manifests, "agent-config-mcp", _FakeManifest("agent-config-mcp"))
    assert direct_layer.excluded_server_keys(["browser-mcp", "agent-config-mcp", "gone-mcp"]) == {
        "local", "agent-config-mcp", "gone-mcp"}
    assert direct_layer.excluded_server_keys(frozenset()) == set()


@pytest.mark.asyncio
async def test_the_layer_hands_the_keys_to_the_session_factory(monkeypatch, tmp_path):
    from core.sandbox import sandbox as sb
    from services.mcp import mcp_registry
    seen: dict = {}

    async def fake_create(session_id, **kw):
        seen.update(kw)
        return SimpleNamespace(session_id=session_id, model="m", effort="", user_sub="", sandbox_cfg=None)

    monkeypatch.setattr(direct_layer, "create_direct_session", fake_create)
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: [])
    monkeypatch.setattr(sb, "resolve_sandbox_config", lambda **k: SimpleNamespace())
    monkeypatch.setattr(sb, "SandboxBuilder", lambda cfg: SimpleNamespace(cfg=cfg))
    monkeypatch.setattr(direct_layer, "_record_session_use", lambda *a, **k: None)
    layer = direct_layer.DirectLLMExecutionLayer()
    cfg = AgentConfig(agent_name="ag", client_type="task", sandbox_host_claude_dir=str(tmp_path),
                      excluded_mcps=frozenset({"agent-config-mcp"}))
    await layer._start_session_impl("s-excl", cfg)
    assert seen["excluded_mcps"] == {"agent-config-mcp"}
    seen.clear()
    await layer._start_session_impl("s-none", AgentConfig(agent_name="ag", sandbox_host_claude_dir=str(tmp_path)))
    assert seen["excluded_mcps"] is None


@pytest.mark.asyncio
async def test_the_session_factory_and_the_pool_pass_the_keys_on(monkeypatch):
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
        await sess.create_direct_session("s-excl-f", "ag", system_prompt="P", excluded_mcps={"agent-config-mcp"})
        assert seen["excluded_mcps"] == {"agent-config-mcp"}
    finally:
        sess._direct_sessions.pop("s-excl-f", None)

    created: list = []

    class _Made:
        def __init__(self, agent_name, **kw):
            self.kw = kw
            self.last_activity = 0.0
            created.append(self)

        async def start(self):
            pass

    monkeypatch.setattr(mcpmod, "AgentMCPManager", _Made)
    pool = mcpmod.MCPPool()
    mgr = await pool.get_or_create("sess-excl", "ag", excluded_mcps={"agent-config-mcp"})
    assert mgr.kw["excluded_mcps"] == {"agent-config-mcp"}


@pytest.mark.asyncio
async def test_the_manager_starts_none_of_the_excluded_servers(monkeypatch, tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "agent-config-mcp": {"type": "stdio", "command": "x"},
        "memory-mcp": {"type": "stdio", "command": "x"},
    }}))
    started: list = []

    class _FakeConn:
        def __init__(self, name, config, **kw):
            self.name = name
            self.config = config
            self.tools = []
            self.dead = False
            started.append(name)

        async def start(self):
            pass

    monkeypatch.setattr(mcpmod, "MCPServerConnection", _FakeConn)
    mgr = mcpmod.AgentMCPManager("ag", session_id="s-skip", prebuilt_config=(cfg, {}),
                                 excluded_mcps={"agent-config-mcp"})
    await mgr._start_impl()
    assert started == ["memory-mcp"] and list(mgr.servers) == ["memory-mcp"]


def test_a_meeting_names_only_the_mcps_it_attaches(rig, monkeypatch):
    from services.mcp import mcp_registry
    from services.meetings.meeting_context import build_meeting_agent_config

    def _mcp_build(*args, **kwargs):
        return (None, {}, {"char-files": "Excluded in meeting mode"}, {}, set())

    monkeypatch.setattr(mcp_registry, "build_session_mcp_config", _mcp_build)
    meeting = {"id": "meet-names", "scope": "user", "created_by": "user-manager"}
    run(build_meeting_agent_config(AGENT, meeting, "s-meet"))
    perm = next(c for c in rig.calls if c["call"] == "build_permission_context")
    dyn = next(c for c in rig.calls if c["call"] == "get_dynamic_contexts")
    assert perm["kwargs"]["assigned_mcp_names"] == ("phone-mcp",)
    assert dyn["args"][1] == ["phone-mcp"]

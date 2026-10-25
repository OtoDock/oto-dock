"""The Direct-LLM MCP policy per session kind (Plan B item 4): chats, tasks
and meetings connect the sidecar HTTP MCPs with the 300 s chat call cap;
phone calls stay stdio-only with the 60 s cap. The pool hands both to the
manager, the manager to every connection."""

from __future__ import annotations

import pytest

from core.layers.direct import mcp as mcpmod
from core.layers.direct.layer import direct_mcp_policy


@pytest.mark.parametrize("client_type,expected", [
    ("dashboard", (True, 300)),
    ("task", (True, 300)),
    ("meeting", (True, 300)),
    ("", (True, 300)),
    ("phone", (False, 60)),
])
def test_policy_by_client_type(client_type, expected):
    assert direct_mcp_policy(client_type) == expected
    assert mcpmod.TOOL_CALL_TIMEOUT == 60 and mcpmod.TOOL_CALL_TIMEOUT_CHAT == 300


@pytest.mark.asyncio
async def test_manager_hands_the_timeout_to_every_connection(monkeypatch, tmp_path):
    import json
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "local": {"type": "stdio", "command": "x"},
        "sidecar": {"type": "http", "url": "http://127.0.0.1:1/mcp"},
    }}))
    seen: list = []

    class _FakeConn:
        def __init__(self, name, config, **kw):
            self.name = name
            self.config = config
            self.tools = []
            self.dead = False
            seen.append((name, kw.get("tool_timeout")))

        async def start(self):
            pass

    monkeypatch.setattr(mcpmod, "MCPServerConnection", _FakeConn)
    mgr = mcpmod.AgentMCPManager(
        "agent-x", session_id="s-policy", prebuilt_config=(cfg, {}),
        enable_http_transport=True, tool_timeout=300,
    )
    await mgr._start_impl()
    assert sorted(seen) == [("local", 300), ("sidecar", 300)]
    assert mgr.tool_timeout == 300

    seen.clear()
    phone = mcpmod.AgentMCPManager("agent-x", session_id="s-phone", prebuilt_config=(cfg, {}))
    await phone._start_impl()
    assert seen == [("local", 60)]  # http skipped, default cap


def test_the_audience_gate_reads_the_sessions_context():
    """The role and the scope the Direct manager's build judges a manifest's
    audience with: the context's effective role and mount scope; an external
    caller's session is never the agent acting as itself; no context names
    no person."""
    from types import SimpleNamespace

    from core.layers.direct.layer import audience_gate
    assert audience_gate(None, external=False) == ("", "user")
    assert audience_gate(SimpleNamespace(role="viewer", session_scope="user"), external=False) == ("viewer", "user")
    shared = SimpleNamespace(role="viewer", session_scope="agent")
    assert audience_gate(shared, external=False) == ("viewer", "agent")
    assert audience_gate(shared, external=True) == ("viewer", "user")


@pytest.mark.asyncio
async def test_create_direct_session_hands_the_gate_to_the_pool(monkeypatch):
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
            "s-gate", "ag", system_prompt="P", user_role="viewer", scope="user",
        )
        assert s.session_id == "s-gate"
        assert seen["user_role"] == "viewer" and seen["scope"] == "user"
    finally:
        sess._direct_sessions.pop("s-gate", None)


@pytest.mark.asyncio
async def test_pool_passes_the_policy_to_a_new_manager(monkeypatch):
    created: list = []

    class _Mgr:
        def __init__(self, agent_name, **kw):
            self.agent_name = agent_name
            self.kw = kw
            self.last_activity = 0.0
            created.append(self)

        async def start(self):
            pass

    monkeypatch.setattr(mcpmod, "AgentMCPManager", _Mgr)
    pool = mcpmod.MCPPool()
    mgr = await pool.get_or_create(
        "sess-1", "agent-x", enable_http_transport=True, tool_timeout=300,
    )
    assert mgr.kw["enable_http_transport"] is True and mgr.kw["tool_timeout"] == 300
    again = await pool.get_or_create("sess-1", "agent-x")
    assert again is mgr and len(created) == 1  # existing manager reused as-is


def test_connection_call_uses_its_own_cap():
    conn = mcpmod.MCPServerConnection("x", {"type": "stdio"}, tool_timeout=7)
    assert conn.tool_timeout == 7
    assert mcpmod.MCPServerConnection("y", {"type": "stdio"}).tool_timeout == 60

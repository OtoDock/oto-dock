"""Regression tests for the direct-LLM MCP connection lifecycle.

Guards the bug where ``stdio_client`` / ``ClientSession`` (anyio context
managers) were entered in one asyncio task and exited in another. anyio
requires a cancel scope to be exited in the task that entered it; violating
that swallowed a RuntimeError at debug level, orphaned the stdio reader (which
then busy-looped on the closed pipe's EOF, pegging the mcp-io event loop at
100% CPU and starving the proxy's main loop), and leaked the MCP subprocess.

The invariant under test: a connection's context managers are entered AND
exited in the *same* task, even when start() and close() are themselves
dispatched from different tasks (as _start_impl / _close_impl do via separate
asyncio.gather children on the mcp-io loop).
"""

import asyncio

import pytest

from core.layers.direct import mcp as mcpmod


class _RecordingCM:
    """Async context manager that records the task it was entered/exited in."""

    def __init__(self, enter_result):
        self._enter_result = enter_result
        self.enter_task = None
        self.exit_task = None

    async def __aenter__(self):
        self.enter_task = asyncio.current_task()
        return self._enter_result

    async def __aexit__(self, *exc):
        self.exit_task = asyncio.current_task()
        return False


class _FakeSession:
    async def initialize(self):
        return type("_Result", (), {"protocolVersion": "test"})()

    async def list_tools(self):
        return type("_Tools", (), {"tools": []})()


@pytest.mark.asyncio
async def test_contexts_enter_and_exit_in_same_task(monkeypatch):
    # Fake the two anyio context managers a remote MCP connection enters.
    streams_cm = _RecordingCM((object(), object(), object()))  # (read, write, get_id)
    session_cm = _RecordingCM(_FakeSession())

    monkeypatch.setattr(mcpmod, "streamablehttp_client", lambda url, headers=None: streams_cm)
    monkeypatch.setattr(mcpmod, "ClientSession", lambda *a, **k: session_cm)

    conn = mcpmod.MCPServerConnection(
        "fake",
        {"type": "streamable-http", "url": "http://example.invalid/mcp"},
        session_id="s1",
    )

    # Dispatch start() and close() from DISTINCT tasks — this is what reproduced
    # the cross-task exit before the owner-task fix.
    await asyncio.gather(conn.start())
    assert streams_cm.enter_task is not None
    assert session_cm.enter_task is not None

    await asyncio.gather(conn.close())

    # The whole lifecycle must have run in one owner task.
    assert streams_cm.exit_task is streams_cm.enter_task
    assert session_cm.exit_task is session_cm.enter_task
    # And the owner task is finished + contexts dropped.
    assert conn._owner_task is None
    assert conn.session is None


@pytest.mark.asyncio
async def test_close_is_idempotent_and_terminates_owner(monkeypatch):
    streams_cm = _RecordingCM((object(), object(), object()))
    session_cm = _RecordingCM(_FakeSession())
    monkeypatch.setattr(mcpmod, "streamablehttp_client", lambda url, headers=None: streams_cm)
    monkeypatch.setattr(mcpmod, "ClientSession", lambda *a, **k: session_cm)

    conn = mcpmod.MCPServerConnection(
        "fake", {"type": "streamable-http", "url": "http://example.invalid/mcp"},
        session_id="s2",
    )
    await conn.start()
    await conn.close()
    # Second close must not raise (owner task already gone).
    await conn.close()
    assert session_cm.exit_task is session_cm.enter_task


@pytest.mark.asyncio
async def test_failed_start_tears_down_in_owner_task(monkeypatch):
    # initialize() raising must still tear down the entered contexts, in the
    # owner task, and leave start() non-fatal (error isolation).
    streams_cm = _RecordingCM((object(), object(), object()))

    class _BoomSession:
        async def initialize(self):
            raise RuntimeError("boom")

    session_cm = _RecordingCM(_BoomSession())
    monkeypatch.setattr(mcpmod, "streamablehttp_client", lambda url, headers=None: streams_cm)
    monkeypatch.setattr(mcpmod, "ClientSession", lambda *a, **k: session_cm)

    conn = mcpmod.MCPServerConnection(
        "fake", {"type": "streamable-http", "url": "http://example.invalid/mcp"},
        session_id="s3",
    )
    await conn.start()  # must not raise
    # Contexts that were entered get exited in the same (owner) task.
    assert streams_cm.exit_task is streams_cm.enter_task
    assert session_cm.exit_task is session_cm.enter_task
    assert conn.tools == []


@pytest.mark.asyncio
async def test_http_type_alias_uses_streamable_transport(monkeypatch):
    # The registry's Claude-JSON format spells streamable HTTP as "http" —
    # the connection must treat it as the same transport, not an unknown type.
    streams_cm = _RecordingCM((object(), object(), object()))
    session_cm = _RecordingCM(_FakeSession())
    monkeypatch.setattr(mcpmod, "streamablehttp_client", lambda url, headers=None: streams_cm)
    monkeypatch.setattr(mcpmod, "ClientSession", lambda *a, **k: session_cm)

    conn = mcpmod.MCPServerConnection(
        "sidecar", {"type": "http", "url": "http://example.invalid/mcp"},
        session_id="s4",
    )
    await conn.start()
    assert conn.session is not None
    await conn.close()


@pytest.mark.asyncio
async def test_http_servers_gated_to_opted_in_managers(tmp_path, monkeypatch):
    # Sidecar HTTP MCPs are app-exec-only: a default manager (Direct-LLM chat)
    # skips them; a manager that opted in (headless_exec) starts them.
    import json as _json

    cfg = tmp_path / "mcp.json"
    cfg.write_text(_json.dumps({"mcpServers": {
        "sidecar": {"type": "http", "url": "http://example.invalid/mcp"},
        "local": {"command": "true"},
    }}))

    class _FakeConn:
        def __init__(self, name, config, **kw):
            self.name = name
            self.config = config
            self.tools = []
            self.dead = False

        async def start(self):
            pass

    monkeypatch.setattr(mcpmod, "MCPServerConnection", _FakeConn)

    mgr = mcpmod.AgentMCPManager(
        "agent-x", session_id="s-gate", prebuilt_config=(cfg, {}))
    await mgr._start_impl()
    assert set(mgr.servers) == {"local"}

    mgr2 = mcpmod.AgentMCPManager(
        "agent-x", session_id="s-gate2", prebuilt_config=(cfg, {}),
        enable_http_transport=True)
    await mgr2._start_impl()
    assert set(mgr2.servers) == {"sidecar", "local"}


@pytest.mark.asyncio
async def test_a_tool_error_answered_by_the_server_does_not_mark_it_dead():
    """The github sidecar reports a tool's own failure as a JSON-RPC error
    (an McpError) instead of result.isError: a 404 for a repo with no
    releases marked the whole server dead on the internal install every ten
    minutes and cost the headless executor a rebuild per wake. A server that
    answered is alive; only transport trouble marks it dead."""
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    class _Answered:
        async def call_tool(self, name, arguments):
            raise McpError(ErrorData(code=-32603, message="failed to get latest release: 404 Not Found"))

    class _Gone:
        async def call_tool(self, name, arguments):
            raise RuntimeError("")  # a closed stream, empty message

    conn = mcpmod.MCPServerConnection("github-mcp", {"type": "http", "url": "http://x/mcp"})
    conn.session = _Answered()
    out = await conn.call_tool("get_latest_release", {"repo": "r"})
    assert "404 Not Found" in out and out.startswith("Error calling tool 'get_latest_release'")
    assert conn.dead is False
    conn.session = _Gone()
    await conn.call_tool("get_latest_release", {"repo": "r"})
    assert conn.dead is True


@pytest.mark.asyncio
async def test_a_server_that_forgot_the_session_is_dead():
    """"Session terminated" is the HTTP client's own McpError for a 404: the
    sidecar evicted the idle session, and every later call on it fails —
    a dead server, so the pooled executor rebuilds instead of erroring on
    every wake (seen on the internal install: the hourly sync after a
    quiet stretch)."""
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    class _Forgot:
        async def call_tool(self, name, arguments):
            raise McpError(ErrorData(code=32600, message="Session terminated"))

    conn = mcpmod.MCPServerConnection("github-mcp", {"type": "http", "url": "http://x/mcp"})
    conn.session = _Forgot()
    out = await conn.call_tool("search_issues", {"query": "q"})
    assert "Session terminated" in out and conn.dead is True
    assert mcpmod.session_gone(RuntimeError("Session not found"))
    assert not mcpmod.session_gone(RuntimeError("failed to get latest release: 404 Not Found"))


@pytest.mark.asyncio
async def test_stdio_servers_start_below_the_proxys_priority(monkeypatch):
    """A Direct-LLM session's stdio MCPs run at the session priority,
    like every session process, so a busy MCP never starves the proxy."""
    from core.sandbox import pty_relay, env_builder
    from core.credentials import mcp_broker
    captured = {}
    streams_cm = _RecordingCM((object(), object()))
    session_cm = _RecordingCM(_FakeSession())

    def _stdio(params):
        captured["params"] = params
        return streams_cm
    monkeypatch.setattr(mcpmod, "stdio_client", _stdio)
    monkeypatch.setattr(mcpmod, "ClientSession", lambda *a, **k: session_cm)
    monkeypatch.setattr(env_builder, "build_session_env", lambda *a, **k: {"PATH": "/bin"})
    monkeypatch.setattr(mcp_broker, "get", lambda sid, name: None)
    monkeypatch.setattr(pty_relay, "nice_prefix", lambda: ["nice", "-n", "10"])

    conn = mcpmod.MCPServerConnection(
        "srv", {"type": "stdio", "command": "srv-bin", "args": ["--x"]},
        session_id="s1", agent_name="a",
    )
    await conn.start()
    params = captured["params"]
    assert params.command == "nice"
    assert params.args == ["-n", "10", "srv-bin", "--x"]
    await conn.close()


@pytest.mark.asyncio
async def test_the_direct_builder_delivers_the_agent_scope_token_files(monkeypatch, tmp_path):
    """A Direct-LLM session provisions its bundles itself: the OAuth stdio
    MCPs' token files ride the bundle like on the Claude and Codex layers
    for the agent scope the Direct build runs as."""
    from core.credentials import credential_files as cf, mcp_broker
    seen: list = []
    monkeypatch.setattr(cf, "token_file_env",
                        lambda agent, **kw: seen.append((agent, kw)) or
                        {"gws": {cf.CREDENTIAL_FILES_ENV: "{}"}})
    provisioned: list = []
    monkeypatch.setattr(mcp_broker, "provision",
                        lambda sid, bundles: provisioned.append((sid, dict(bundles))))
    monkeypatch.setattr("services.mcp.mcp_registry.build_session_mcp_config",
                        lambda *a, **kw: (tmp_path / "absent.json", None, None, {}, None))
    mgr = mcpmod.AgentMCPManager("dev-agent")
    mgr.session_id = "s-direct"
    await mgr._start_impl()
    assert seen == [("dev-agent", {"user_sub": "", "session_scope": "agent"})]
    [(sid, bundles)] = provisioned
    assert sid == "s-direct" and set(bundles) == {"gws"}


@pytest.mark.asyncio
async def test_prebuilt_bundles_keep_the_builder_identity_files(monkeypatch, tmp_path):
    """An app button builds its bundles for the app's identity (a personal
    owner's files); the manager must not re-deliver the agent scope over
    them."""
    from core.credentials import credential_files as cf, mcp_broker
    from core.credentials.mcp_broker import SecretBundle
    monkeypatch.setattr(cf, "token_file_env",
                        lambda agent, **kw: {"gws": {cf.CREDENTIAL_FILES_ENV: "AGENT-SCOPE"}})
    provisioned: list = []
    monkeypatch.setattr(mcp_broker, "provision",
                        lambda sid, bundles: provisioned.append(dict(bundles)))
    owner = SecretBundle()
    owner.env[cf.CREDENTIAL_FILES_ENV] = "OWNER-SCOPE"
    mgr = mcpmod.AgentMCPManager("dev-agent")
    mgr.session_id = "s-button"
    mgr.prebuilt_config = (tmp_path / "absent.json", {"gws": owner})
    await mgr._start_impl()
    [bundles] = provisioned
    assert bundles["gws"].env[cf.CREDENTIAL_FILES_ENV] == "OWNER-SCOPE"

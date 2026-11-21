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

    monkeypatch.setattr(mcpmod, "streamablehttp_client", lambda url, headers=None, **_factory: streams_cm)
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
    monkeypatch.setattr(mcpmod, "streamablehttp_client", lambda url, headers=None, **_factory: streams_cm)
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
    monkeypatch.setattr(mcpmod, "streamablehttp_client", lambda url, headers=None, **_factory: streams_cm)
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
    monkeypatch.setattr(mcpmod, "streamablehttp_client", lambda url, headers=None, **_factory: streams_cm)
    monkeypatch.setattr(mcpmod, "ClientSession", lambda *a, **k: session_cm)

    conn = mcpmod.MCPServerConnection(
        "sidecar", {"type": "http", "url": "http://example.invalid/mcp"},
        session_id="s4",
    )
    await conn.start()
    assert conn.session is not None
    await conn.close()


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
async def test_a_results_pictures_and_blobs_ride_beside_its_text():
    """``call_tool_blocks`` keeps an MCP result's image blocks and embedded
    resources (MCP JSON) for the result-files service; ``call_tool`` still
    answers the text alone, the picture as ``[binary data: …]``."""
    from mcp.types import (
        BlobResourceContents,
        CallToolResult,
        EmbeddedResource,
        ImageContent,
        TextContent,
    )

    class _Pictures:
        async def call_tool(self, name, arguments):
            return CallToolResult(content=[
                ImageContent(type="image", data="iVBORw0KGgo=", mimeType="image/png"),
                TextContent(type="text", text="Route: 12 km"),
                EmbeddedResource(type="resource", resource=BlobResourceContents(
                    uri="gdrive://f/report.pdf", mimeType="application/pdf", blob="JVBERg==")),
            ])

    conn = mcpmod.MCPServerConnection("maps", {"type": "http", "url": "http://x/mcp"})
    conn.session = _Pictures()
    text, blocks = await conn.call_tool_blocks("static_map", {})
    assert text == "[binary data: image/png]\nRoute: 12 km"
    assert blocks[0] == {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}
    assert blocks[1]["type"] == "resource"
    assert blocks[1]["resource"]["uri"] == "gdrive://f/report.pdf"
    assert blocks[1]["resource"]["blob"] == "JVBERg=="
    assert await conn.call_tool("static_map", {}) == text


@pytest.mark.asyncio
async def test_the_builders_config_is_read_off_the_loop(monkeypatch, tmp_path):
    """The read-back runs in a worker thread: a session start holds neither
    loop."""
    import threading

    from core.credentials import mcp_broker
    monkeypatch.setattr(mcp_broker, "provision", lambda sid, bundles: None)
    seen: list[tuple[int, bool]] = []
    real = mcpmod.AgentMCPManager._read_config

    def _read(self):
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        seen.append((threading.get_ident(), on_loop))
        return real(self)

    monkeypatch.setattr(mcpmod.AgentMCPManager, "_read_config", _read)
    mgr = mcpmod.AgentMCPManager("ag", session_id="s-a", prebuilt_config=(tmp_path / "absent.json", {}))
    await mgr._start_impl()
    [(thread, on_loop)] = seen
    assert not on_loop and thread != threading.get_ident()


@pytest.mark.asyncio
async def test_a_manager_with_no_config_starts_nothing(monkeypatch):
    from core.credentials import mcp_broker
    provisioned: list = []
    monkeypatch.setattr(mcp_broker, "provision", lambda sid, bundles: provisioned.append(dict(bundles)))
    mgr = mcpmod.AgentMCPManager("ag", session_id="s-none")
    await mgr._start_impl()
    assert provisioned == [{}] and mgr.servers == {}


@pytest.mark.asyncio
async def test_the_pool_forgets_a_manager_whose_start_failed(monkeypatch):
    """A start that raises leaves no manager under the session id (the
    next warm builds afresh) and the half-started manager is closed."""
    pool = mcpmod.MCPPool()
    closed: list[str] = []

    async def _boom(self):
        raise RuntimeError("no build")

    async def _close(self):
        closed.append(self.session_id)

    monkeypatch.setattr(mcpmod.AgentMCPManager, "start", _boom)
    monkeypatch.setattr(mcpmod.AgentMCPManager, "close", _close)
    with pytest.raises(RuntimeError):
        await pool.get_or_create("s-fail", "ag")
    assert "s-fail" not in pool._managers and closed == ["s-fail"]


@pytest.mark.asyncio
async def test_a_second_caller_during_a_start_shares_its_outcome(monkeypatch):
    """Two callers for one session id while the first start runs: both get
    the one started manager (one start), or both get the start's failure
    and the pool is empty (nobody is handed a manager that was closed)."""
    pool = mcpmod.MCPPool()
    starts: list[str] = []
    closed: list[str] = []
    outcome = {"raise": True}

    async def _slow_start(self):
        starts.append(self.session_id)
        await asyncio.sleep(0.05)
        if outcome["raise"]:
            raise RuntimeError("no build")

    async def _close(self):
        closed.append(self.session_id)

    monkeypatch.setattr(mcpmod.AgentMCPManager, "start", _slow_start)
    monkeypatch.setattr(mcpmod.AgentMCPManager, "close", _close)
    results = await asyncio.gather(
        pool.get_or_create("s-race", "ag"), pool.get_or_create("s-race", "ag"),
        return_exceptions=True,
    )
    assert all(isinstance(r, RuntimeError) for r in results), results
    assert "s-race" not in pool._managers and closed == ["s-race"] and starts == ["s-race"]

    outcome["raise"] = False
    first, second = await asyncio.gather(
        pool.get_or_create("s-ok", "ag"), pool.get_or_create("s-ok", "ag"),
    )
    assert first is second and starts == ["s-race", "s-ok"]
    assert pool._managers["s-ok"] is first and first.started.done()


@pytest.mark.asyncio
async def test_a_server_that_never_answers_initialize_is_left_out(monkeypatch):
    """An HTTP MCP whose handshake never returns (an address that does not
    resolve) does not hold the session start: the bound fires, the
    entered contexts exit in the owner task, the server has no tools."""
    streams_cm = _RecordingCM((object(), object(), object()))

    class _SilentSession:
        async def initialize(self):
            await asyncio.Event().wait()

    session_cm = _RecordingCM(_SilentSession())
    monkeypatch.setattr(mcpmod, "streamablehttp_client", lambda url, headers=None, **_factory: streams_cm)
    monkeypatch.setattr(mcpmod, "ClientSession", lambda *a, **k: session_cm)
    monkeypatch.setattr(mcpmod, "MCP_START_TIMEOUT_S", 0.1)

    conn = mcpmod.MCPServerConnection(
        "silent", {"type": "streamable-http", "url": "http://file-tools:8901/mcp/"},
        session_id="s-silent",
    )
    await asyncio.wait_for(conn.start(), timeout=5)
    assert conn.tools == []
    assert streams_cm.exit_task is streams_cm.enter_task
    assert session_cm.exit_task is session_cm.enter_task
    await asyncio.wait_for(conn.close(), timeout=5)


@pytest.mark.asyncio
async def test_a_transport_stuck_in_its_own_task_group_is_left_out(monkeypatch):
    """The client's transports are anyio context managers that start a task
    group on entry (``mcp/client/sse.py``): one stuck there ends with the
    bound too, and ``start()`` returns."""
    import contextlib

    import anyio

    @contextlib.asynccontextmanager
    async def _stuck(url, headers=None, **_factory):
        async with anyio.create_task_group() as tg:
            async def _never(*, task_status=anyio.TASK_STATUS_IGNORED):
                await anyio.sleep_forever()
            await tg.start(_never)
            yield (object(), object(), object())

    monkeypatch.setattr(mcpmod, "streamablehttp_client", _stuck)
    monkeypatch.setattr(mcpmod, "MCP_START_TIMEOUT_S", 0.1)
    conn = mcpmod.MCPServerConnection(
        "stuck", {"type": "streamable-http", "url": "http://file-tools:8901/mcp/"},
        session_id="s-stuck",
    )
    await asyncio.wait_for(conn.start(), timeout=5)
    assert conn.tools == [] and conn.session is None
    await asyncio.wait_for(conn.close(), timeout=5)


# -- a stdio server on a compute machine --------------------------------------


class _Builder:
    """What the connection reads of a sandbox builder."""

    cfg = type("_Cfg", (), {"username": "alice", "role": "editor"})()

    def get_env_overrides(self):
        return {"HOME": "/home/agent", "PATH": "/sandbox/bin"}

    def describe(self):
        return type("_Desc", (), {"to_dict": lambda _self: {"agent": "pa"}})()


def _relayed_conn(monkeypatch, *, builder=None, machine="compute-1", main_loop="set"):
    """A stdio connection with the relay replaced by a recording context."""
    from core.remote import tool_relay
    streams_cm = _RecordingCM((object(), object()))
    session_cm = _RecordingCM(_FakeSession())
    calls: list[dict] = []

    def _relay(machine_id, session_id, server, **kw):
        calls.append({"machine_id": machine_id, "session_id": session_id, "server": server, **kw})
        return streams_cm

    monkeypatch.setattr(tool_relay, "relay_client", _relay)
    monkeypatch.setattr(mcpmod, "ClientSession", lambda *a, **k: session_cm)
    conn = mcpmod.MCPServerConnection(
        "memory-mcp", {"type": "stdio", "command": "/venv/bin/python", "args": ["server.py"],
                       "env": {"FROM_MANIFEST": "yes"}},
        session_id="s-relay", agent_name="pa",
        sandbox_builder=_Builder() if builder is None else builder,
        machine_id=machine, main_loop=asyncio.get_running_loop() if main_loop == "set" else main_loop,
    )
    return conn, calls, streams_cm, session_cm


@pytest.mark.asyncio
async def test_a_relayed_server_goes_through_the_relay_with_this_sessions_env(monkeypatch):
    """The environment is the local spawn's, sandbox overrides included;
    the description, not a wrapped command, travels."""
    monkeypatch.setattr("core.sandbox.env_builder.build_session_env",
                        lambda *_a, **_k: {"OTO_SESSION_ID": "s-relay", "PROXY_URL": "http://127.0.0.1:8400"})
    conn, calls, streams_cm, session_cm = _relayed_conn(monkeypatch)
    await conn.start()
    [call] = calls
    assert (call["machine_id"], call["session_id"], call["server"]) == ("compute-1", "s-relay", "memory-mcp")
    assert call["argv"] == ["/venv/bin/python", "server.py"]
    assert call["description"] == {"agent": "pa"}
    env = call["env"]
    assert env["OTO_SESSION_ID"] == "s-relay" and env["FROM_MANIFEST"] == "yes"
    assert env["HOME"] == "/home/agent" and env["PATH"] == "/sandbox/bin"   # the sandbox's own
    await conn.close()
    # One owner task, as for every other connection.
    assert streams_cm.exit_task is streams_cm.enter_task
    assert session_cm.exit_task is session_cm.enter_task


@pytest.mark.asyncio
async def test_a_server_with_no_sandbox_never_starts_on_a_machine(monkeypatch):
    monkeypatch.setattr("core.sandbox.env_builder.build_session_env", lambda *_a, **_k: {})
    conn, calls, _s, _c = _relayed_conn(monkeypatch)
    conn.sandbox_builder = None
    await conn.start()      # logged and left out, as any failed start
    assert calls == [] and conn.tools == [] and conn.session is None
    await conn.close()


@pytest.mark.asyncio
async def test_a_stdio_server_with_no_machine_is_refused_and_starts_nothing(monkeypatch):
    """The platform starts no tool itself: with no compute machine the
    server is left out, and its call answers the compute sentence."""
    from core import placement
    from core.remote import tool_relay

    def _no_relay(*_a, **_k):
        raise AssertionError("a server with no machine never reaches the relay")

    def _no_process(*_a, **_k):
        raise AssertionError("the platform starts no tool")

    monkeypatch.setattr(tool_relay, "relay_client", _no_relay)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_process)
    monkeypatch.setattr("anyio.open_process", _no_process)
    monkeypatch.setattr("core.sandbox.env_builder.build_session_env", lambda *_a, **_k: {"X": "1"})
    monkeypatch.setattr(mcpmod, "ClientSession", lambda *a, **k: _RecordingCM(_FakeSession()))
    conn = mcpmod.MCPServerConnection("local-mcp", {"type": "stdio", "command": "/bin/true"}, session_id="s-local")
    await conn.start()      # logged and left out, as any failed start
    assert conn.session is None and conn.tools == []
    assert conn.refused == placement.NO_COMPUTE_SENTENCE
    await conn.close()


@pytest.mark.asyncio
async def test_the_relays_end_marks_the_server_dead(monkeypatch):
    monkeypatch.setattr("core.sandbox.env_builder.build_session_env", lambda *_a, **_k: {})
    conn, calls, _s, _c = _relayed_conn(monkeypatch)
    await conn.start()
    assert conn.dead is False
    calls[0]["on_end"](True)
    assert conn.dead is True
    await conn.close()


class _RefusedOpen:
    def __init__(self, error):
        self.error = error

    async def __aenter__(self):
        raise self.error

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_an_open_that_lost_its_link_reads_lost_and_a_refused_one_does_not(monkeypatch):
    """A failed open drops its child, so the relay no longer knows it: the
    connection remembers a lost link. A tool that could not start on a
    machine that answered is left out, and must not rebuild the session at
    every message."""
    from core.remote import tool_relay
    monkeypatch.setattr("core.sandbox.env_builder.build_session_env", lambda *_a, **_k: {})
    monkeypatch.setattr(tool_relay, "session_lost", lambda _m, _s: False)
    for error, lost in ((tool_relay.LinkLost("gone"), True), (RuntimeError("the render refused it"), False)):
        conn, _calls, _s, _c = _relayed_conn(monkeypatch)
        monkeypatch.setattr(tool_relay, "relay_client", lambda *_a, _error=error, **_k: _RefusedOpen(_error))
        await conn.start()
        mgr = mcpmod.AgentMCPManager("ag", session_id="s-relay", machine_id="compute-1")
        mgr.servers["memory-mcp"] = conn
        assert conn.link_lost is lost and mgr.tools_lost() is lost
        await conn.close()


def test_a_manager_on_this_host_is_never_lost():
    assert mcpmod.AgentMCPManager("ag", session_id="s").tools_lost() is False


def test_a_managers_lost_tools_are_the_relays_answer(monkeypatch):
    from core.remote import tool_relay
    asked: list = []
    monkeypatch.setattr(tool_relay, "session_lost", lambda m, s: asked.append((m, s)) or True)
    assert mcpmod.AgentMCPManager("ag", session_id="s-1", machine_id="compute-1").tools_lost() is True
    assert asked == [("compute-1", "s-1")]


@pytest.mark.asyncio
async def test_a_lost_manager_is_closed_and_a_new_one_started_under_its_id(monkeypatch):
    pool = mcpmod.MCPPool()
    closed: list = []

    class _Mgr(mcpmod.AgentMCPManager):
        lost = False

        def tools_lost(self):
            return self.lost

        async def start(self):
            pass

        async def close(self):
            closed.append(self)

    monkeypatch.setattr(mcpmod, "AgentMCPManager", _Mgr)
    first = await pool.get_or_create("s-1", "ag", machine_id="compute-1")
    assert await pool.get_or_create("s-1", "ag", machine_id="compute-1") is first and closed == []
    first.lost = True
    second = await pool.get_or_create("s-1", "ag", machine_id="compute-1")
    assert second is not first and closed == [first]
    assert second.machine_id == "compute-1"

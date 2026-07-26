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

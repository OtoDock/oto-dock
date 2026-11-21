"""The platform's half of the relay of Direct LLM's tool processes.

Load-bearing: the MCP client sees a tool on a compute machine exactly as one
on this host (the same pair of streams, a real ``ClientSession`` over two
event loops); a frame from another machine or with another key reaches
nothing; a child's end and its machine's drop end the stream and say which;
a failed or cancelled open leaves nothing behind; a call too large for the
link is answered here and never sent; a frame handler never raises.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from mcp import ClientSession, types
from mcp.shared.message import SessionMessage

from core.remote import tool_relay

MACHINE, SESSION, SERVER = "compute-1", "sess-1", "memory-mcp"
DESCRIPTION = {"agent": "pa", "binds": []}


class _FakeManager:
    """Stands where the connection manager does: records what is sent, and
    a test plays the machine by calling the frame handlers."""

    def __init__(self, *, refuse: str = "", answer=None):
        self.sent: list[dict] = []
        self.refuse = refuse
        self.answer = answer            # (frame) -> list of lines, called per tool_in
        self.open_started = asyncio.Event()
        self.open_held = asyncio.Event()
        self.open_held.set()
        self.conn = SimpleNamespace(enqueue_send_nowait=self._frame)

    def tunnel_port(self, _machine: str) -> int:
        return 43210

    def get_connection(self, machine: str):
        return self.conn if machine == MACHINE else None

    async def send_command(self, machine: str, msg: dict, *, timeout: float = 30.0) -> dict:
        assert machine == MACHINE
        self.sent.append(msg)
        self.open_started.set()
        await self.open_held.wait()
        if self.refuse:
            raise RuntimeError(self.refuse)
        return {"status": "ok"}

    def _frame(self, msg: dict) -> None:
        self.sent.append(msg)
        if msg.get("type") == "tool_in" and self.answer is not None:
            for line in self.answer(msg):
                tool_relay.on_out(MACHINE, {**msg, "type": "tool_out", "line": line})

    def of(self, kind: str) -> list[dict]:
        return [m for m in self.sent if m.get("type") == kind]


@pytest.fixture
def manager(monkeypatch):
    fake = _FakeManager()
    monkeypatch.setattr(tool_relay, "_manager", lambda: fake)
    tool_relay._children.clear()
    tool_relay._refused_logged.clear()
    yield fake
    tool_relay._children.clear()


def _client(main_loop, on_end=None, **over):
    kw = {"description": DESCRIPTION, "argv": ["/venv/bin/python", "server.py"],
          "env": {"PATH": "/usr/bin"}, "main_loop": main_loop, "on_end": on_end}
    kw.update(over)
    return tool_relay.relay_client(MACHINE, SESSION, SERVER, **kw)


def _request(i: int = 1, **params) -> SessionMessage:
    return SessionMessage(types.JSONRPCMessage(types.JSONRPCRequest(
        jsonrpc="2.0", id=i, method="tools/call", params=params or None)))


async def _until(check, timeout: float = 5.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < end, "timed out"
        await asyncio.sleep(0.005)


# -- the open --------------------------------------------------------------


def test_the_open_names_the_session_its_key_and_the_sandbox(manager):
    async def run():
        loop = asyncio.get_running_loop()
        async with _client(loop):
            frame = manager.of("tool_open")[0]
            assert frame["session_id"] == SESSION and frame["server"] == SERVER
            assert len(frame["key"]) == 32 and frame["sandbox"] == DESCRIPTION
            assert frame["argv"] == ["/venv/bin/python", "server.py"]
            assert frame["env"] == {"PATH": "/usr/bin"} and frame["tunnel_port"] == 43210
        assert tool_relay.live_count() == 0
    asyncio.run(run())


def test_a_refused_open_raises_and_leaves_nothing(manager):
    async def run():
        manager.refuse = "the tool could not start"
        loop = asyncio.get_running_loop()
        with pytest.raises(RuntimeError, match="could not start") as refused:
            async with _client(loop):
                pass
        # A machine that answered: a refused render or a failed spawn is no lost link.
        assert not isinstance(refused.value, tool_relay.LinkLost)
        assert tool_relay.live_count() == 0
        await _until(lambda: manager.of("tool_close"))
    asyncio.run(run())


def test_an_open_with_no_link_is_a_lost_link(manager):
    async def run():
        manager.refuse = "Satellite compute- not connected"
        manager.conn = None
        with pytest.raises(tool_relay.LinkLost, match="not connected"):
            async with _client(asyncio.get_running_loop()):
                pass
        assert tool_relay.live_count() == 0
    asyncio.run(run())


def test_a_link_that_drops_during_the_open_is_a_lost_link(manager):
    async def run():
        manager.open_held.clear()
        manager.refuse = "Satellite compute- replaced by a new connection"

        async def enter():
            async with _client(asyncio.get_running_loop()):
                pass
        task = asyncio.ensure_future(enter())
        await manager.open_started.wait()
        tool_relay.machine_dropped(MACHINE)
        manager.open_held.set()
        with pytest.raises(tool_relay.LinkLost):
            await task
    asyncio.run(run())


def test_a_cancelled_open_closes_what_the_machine_may_have_spawned(manager):
    async def run():
        manager.open_held.clear()
        loop = asyncio.get_running_loop()

        async def enter():
            async with _client(loop):
                pass
        task = asyncio.ensure_future(enter())
        await manager.open_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _until(lambda: manager.of("tool_close"))
        assert tool_relay.live_count() == 0
    asyncio.run(run())


def test_a_second_open_of_the_same_tool_is_refused(manager):
    async def run():
        loop = asyncio.get_running_loop()
        async with _client(loop):
            with pytest.raises(RuntimeError, match="already open"):
                async with _client(loop):
                    pass
            assert tool_relay.live_count() == 1
    asyncio.run(run())


# -- the lines -------------------------------------------------------------


def test_a_line_crosses_each_way(manager):
    async def run():
        loop = asyncio.get_running_loop()
        async with _client(loop) as (read, write):
            await write.send(_request(1))
            await _until(lambda: manager.of("tool_in"))
            frame = manager.of("tool_in")[0]
            assert json.loads(frame["line"])["id"] == 1 and frame["key"] == manager.of("tool_open")[0]["key"]
            tool_relay.on_out(MACHINE, {**frame, "type": "tool_out",
                                        "line": '{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'})
            got = await asyncio.wait_for(read.receive(), timeout=5)
            assert got.message.root.id == 1
    asyncio.run(run())


@pytest.mark.parametrize("other", [{"key": "z" * 32}, {"session_id": "other"}, {"server": "other"}])
def test_a_frame_with_another_key_or_child_reaches_nothing(manager, other):
    async def run():
        loop = asyncio.get_running_loop()
        async with _client(loop) as (read, _write):
            frame = {**manager.of("tool_open")[0], "type": "tool_out",
                     "line": '{"jsonrpc":"2.0","id":9,"result":{}}'}
            tool_relay.on_out(MACHINE, {**frame, **other})
            tool_relay.on_exit(MACHINE, {**frame, **other, "type": "tool_exit", "code": 0})
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(read.receive(), timeout=0.1)
    asyncio.run(run())


def test_a_frame_from_another_machine_reaches_nothing(manager):
    async def run():
        loop = asyncio.get_running_loop()
        async with _client(loop) as (read, _write):
            frame = {**manager.of("tool_open")[0], "type": "tool_out",
                     "line": '{"jsonrpc":"2.0","id":9,"result":{}}'}
            tool_relay.on_out("compute-2", frame)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(read.receive(), timeout=0.1)
    asyncio.run(run())


def test_a_call_too_large_for_the_link_is_answered_here_and_never_sent(manager, monkeypatch):
    async def run():
        monkeypatch.setattr(tool_relay, "MAX_FRAME_BYTES", 200)
        monkeypatch.setattr(tool_relay, "_SAFE_LINE_BYTES", 10)
        loop = asyncio.get_running_loop()
        async with _client(loop) as (read, write):
            await write.send(_request(7, blob="x" * 500))
            got = await asyncio.wait_for(read.receive(), timeout=5)
            assert got.message.root.id == 7 and got.message.root.error.message == tool_relay.TOO_LARGE
            assert not manager.of("tool_in")
    asyncio.run(run())


# -- the end ---------------------------------------------------------------


def _a_line(n: int, size: int = 100) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": n, "result": {"x": "y" * size}})


def test_a_tool_that_outruns_its_reader_past_the_bound_is_ended(manager, monkeypatch):
    """The frame handler never waits on a tool, so what it hands over is
    bounded here: the child is told to end and the rest of its lines are
    dropped, the reader keeping what it had."""
    async def run():
        monkeypatch.setattr(tool_relay, "MAX_INBOX_CHARS", 300)
        ended: list[bool] = []
        async with _client(asyncio.get_running_loop(), on_end=ended.append) as (read, _write):
            frame = manager.of("tool_open")[0]
            for i in range(10):
                tool_relay.on_out(MACHINE, {**frame, "type": "tool_out", "line": _a_line(i)})
            assert len(manager.of("tool_close")) == 1
            got = 0
            with pytest.raises(Exception):  # noqa: B017  # anyio's end-of-stream, whichever class this version raises
                while True:
                    await asyncio.wait_for(read.receive(), timeout=5)
                    got += 1
            assert 0 < got < 10
        assert ended == [False]
    asyncio.run(run())


def test_a_reader_that_keeps_pace_never_trips_the_bound(manager, monkeypatch):
    """What the reader took stops counting."""
    async def run():
        monkeypatch.setattr(tool_relay, "MAX_INBOX_CHARS", 300)
        async with _client(asyncio.get_running_loop()) as (read, _write):
            frame = manager.of("tool_open")[0]
            for i in range(10):
                tool_relay.on_out(MACHINE, {**frame, "type": "tool_out", "line": _a_line(i)})
                got = await asyncio.wait_for(read.receive(), timeout=5)
                assert got.message.root.id == i
            assert not manager.of("tool_close")
    asyncio.run(run())


def test_the_tools_exit_ends_the_stream_and_is_no_loss(manager):
    async def run():
        ended: list[bool] = []
        loop = asyncio.get_running_loop()
        async with _client(loop, on_end=ended.append) as (read, _write):
            frame = manager.of("tool_open")[0]
            tool_relay.on_exit(MACHINE, {**frame, "type": "tool_exit", "code": 0, "reason": ""})
            with pytest.raises(Exception):  # noqa: B017  # anyio's end-of-stream, whichever class this version raises
                await asyncio.wait_for(read.receive(), timeout=5)
        assert ended == [False]
    asyncio.run(run())


def test_the_machines_drop_ends_its_tools_and_says_they_are_lost(manager):
    async def run():
        ended: list[bool] = []
        loop = asyncio.get_running_loop()
        async with _client(loop, on_end=ended.append) as (read, _write):
            tool_relay.machine_dropped("compute-2")     # another machine's drop: nothing
            await asyncio.sleep(0.01)
            assert ended == []
            tool_relay.machine_dropped(MACHINE)
            with pytest.raises(Exception):  # noqa: B017  # anyio's end-of-stream, whichever class this version raises
                await asyncio.wait_for(read.receive(), timeout=5)
        assert ended == [True]
    asyncio.run(run())


def test_the_close_asks_the_machine_and_waits_for_the_exit(manager, monkeypatch):
    async def run():
        monkeypatch.setattr(tool_relay, "_CLOSE_WAIT_S", 3.0)
        loop = asyncio.get_running_loop()
        go = asyncio.Event()

        async def hold():
            # Entered and left in one task, as the connection's owner task does.
            async with _client(loop):
                await go.wait()
        task = asyncio.ensure_future(hold())
        await _until(lambda: manager.of("tool_open"))
        frame = manager.of("tool_open")[0]
        go.set()
        await _until(lambda: manager.of("tool_close"))
        await asyncio.sleep(0.05)
        assert not task.done()       # it waits for the exit
        tool_relay.on_exit(MACHINE, {**frame, "type": "tool_exit", "code": 0, "reason": ""})
        await asyncio.wait_for(task, timeout=5)
        assert tool_relay.live_count() == 0
    asyncio.run(run())


def test_the_close_does_not_wait_past_its_bound(manager, monkeypatch):
    async def run():
        monkeypatch.setattr(tool_relay, "_CLOSE_WAIT_S", 0.05)
        loop = asyncio.get_running_loop()
        async with _client(loop):
            pass
        assert tool_relay.live_count() == 0
    asyncio.run(run())


def test_a_frame_handler_never_raises_after_the_client_loop_is_gone(manager):
    """A handler's exception would end the machine's whole connection."""
    other = asyncio.new_event_loop()
    other.close()
    child = tool_relay._Child(MACHINE, SESSION, SERVER, "k" * 32, other, None)
    tool_relay._children[(MACHINE, SESSION, SERVER)] = child
    frame = {"type": "tool_out", "session_id": SESSION, "server": SERVER, "key": "k" * 32, "line": "{}"}
    tool_relay.on_out(MACHINE, frame)
    tool_relay.on_exit(MACHINE, {**frame, "type": "tool_exit"})
    tool_relay.machine_dropped(MACHINE)


def test_a_port_the_machine_no_longer_serves_is_not_current(manager):
    assert tool_relay.port_current(MACHINE, 43210)
    assert not tool_relay.port_current(MACHINE, 50000)
    assert tool_relay.port_current(MACHINE, 0)      # no recorded port is never judged


def test_a_session_is_lost_when_its_machine_went_or_its_port_moved(manager, monkeypatch):
    async def run():
        loop = asyncio.get_running_loop()
        async with _client(loop):
            assert not tool_relay.session_lost(MACHINE, SESSION)
            monkeypatch.setattr(manager, "tunnel_port", lambda _m: 50000)
            assert tool_relay.session_lost(MACHINE, SESSION)    # the port moved
            monkeypatch.setattr(manager, "tunnel_port", lambda _m: 43210)
            assert not tool_relay.session_lost(MACHINE, SESSION)
            tool_relay.machine_dropped(MACHINE)
            assert tool_relay.session_lost(MACHINE, SESSION)
            assert not tool_relay.session_lost(MACHINE, "another-session")
    asyncio.run(run())


# -- a real MCP client across the two loops ---------------------------------


def _speak_mcp(frame: dict) -> list[str]:
    """Plays the tool: answers initialize and tools/list as a server does."""
    msg = json.loads(frame["line"])
    if msg.get("method") == "initialize":
        return [json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": msg["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"},
        }})]
    if msg.get("method") == "tools/list":
        return [json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
            {"name": "ping", "description": "d", "inputSchema": {"type": "object"}},
        ]}})]
    if msg.get("method") == "tools/call":
        return [json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "content": [{"type": "text", "text": "pong"}], "isError": False,
        }})]
    return []


def test_a_real_client_session_runs_across_the_two_loops(manager):
    """The client on its own thread's loop, the link on the main loop, as in
    the proxy: initialize, list, call, close."""
    manager.answer = _speak_mcp
    io_loop = asyncio.new_event_loop()
    thread = threading.Thread(target=io_loop.run_forever, daemon=True)
    thread.start()

    async def on_io(main_loop):
        async with _client(main_loop) as streams, ClientSession(*streams) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert [t.name for t in tools.tools] == ["ping"]
            result = await session.call_tool("ping", {})
            assert result.content[0].text == "pong"

    async def run():
        main_loop = asyncio.get_running_loop()
        # The machine's exit answers the close, on the link's loop.
        real = manager._frame

        def frame(msg):
            real(msg)
            if msg["type"] == "tool_close":
                tool_relay.on_exit(MACHINE, {**msg, "type": "tool_exit", "code": 0, "reason": ""})
        manager.conn.enqueue_send_nowait = frame
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(on_io(main_loop), io_loop))
        assert tool_relay.live_count() == 0

    try:
        asyncio.run(run())
    finally:
        io_loop.call_soon_threadsafe(io_loop.stop)
        thread.join(timeout=5)
        io_loop.close()


@pytest.mark.asyncio
async def test_the_open_carries_the_sessions_exit_door_when_given(manager):
    """A Direct session on compute whose machine runs its exit: the same block a start
    payload carries rides each tool's open; without one, nothing."""
    block = {"secret": "s" * 32, "token_hash": "h" * 64, "carves": [],
             "entries": [{"placeholder": "oto-exit-" + "a" * 32, "header": "xi-api-key", "prefix": "",
                          "key": "K", "hosts": ["api.example.test"]}]}
    cm = _client(description=DESCRIPTION, argv=["python", "server.py"], env={}, main_loop=asyncio.get_running_loop(),
                 exit_block=block)
    await cm.__aenter__()
    try:
        assert manager.sent[0]["type"] == "tool_open" and manager.sent[0]["exit"] == block
    finally:
        await cm.__aexit__(None, None, None)
    manager.sent.clear()
    cm = _client(description=DESCRIPTION, argv=["python", "server.py"], env={}, main_loop=asyncio.get_running_loop())
    await cm.__aenter__()
    try:
        assert "exit" not in manager.sent[0]
    finally:
        await cm.__aexit__(None, None, None)

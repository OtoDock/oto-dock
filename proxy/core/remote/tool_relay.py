"""Direct LLM's tool processes on a compute machine: the platform's half.

Direct LLM's model loop and its MCP client stay in the proxy. Only the stdio
pipe of a tool process moves: ``relay_client`` hands the MCP client the same
pair of streams ``mcp.client.stdio.stdio_client`` does, and carries the lines
over the machine's link (``satellite/sessions/tool_relay.py`` is the other
half, the frames are in ``satellite/PROTOCOL.md``).

Two event loops meet here. The client runs on the ``mcp-io`` thread's loop
(``core/layers/direct/mcp.py``) and the machine's link on the main loop. A
frame the client sends crosses with ``call_soon_threadsafe`` (the open with
``run_coroutine_threadsafe``, it waits for its ack); a line the machine sends
crosses the other way into the child's inbox, so the link's frame handler
never waits on a tool.

A child is known by ``(machine, session, server)`` and a key minted at its
open. A frame from another machine, or with another key, is dropped: a stale
frame after a reopen reaches nothing. The key is an incarnation id and no
credential: a compromised compute machine holds the session's secrets anyway.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.shared.message import SessionMessage

logger = logging.getLogger("otodock")

#: The largest frame sent, encoded: the link takes 16 MiB, and encoding grows
#: Greek text threefold. A stdio tool on the platform has no such cap.
MAX_FRAME_BYTES = 12 * 1024 * 1024
#: Below this a line cannot encode past the cap (the worst case is sixfold).
_SAFE_LINE_BYTES = 2 * 1024 * 1024
#: What one tool's unread lines may hold here, in characters: the link's frame
#: handler never waits on a tool, so a queue with no bound would be this file's.
MAX_INBOX_CHARS = 64 * 1024 * 1024
#: How long the machine has to spawn a tool and answer its open.
_OPEN_TIMEOUT_S = 30.0
#: How long a close waits for ``tool_exit``, a little past the machine's own
#: two seconds between a child's stdin ending and its SIGTERM.
_CLOSE_WAIT_S = 3.0

TOO_LARGE = "The tool's call was too large to send."


class LinkLost(RuntimeError):
    """The machine's link was gone, or went, while a tool opened: unlike a
    tool the machine could not start, the session has lost its place there."""


class _Child:
    __slots__ = ("machine_id", "session_id", "server", "key", "port", "loop", "inbox", "held", "flooded",
                 "ended", "lost", "on_end")

    def __init__(self, machine_id: str, session_id: str, server: str, key: str,
                 loop: asyncio.AbstractEventLoop, on_end: Callable[[bool], None] | None) -> None:
        self.machine_id = machine_id
        self.session_id = session_id
        self.server = server
        self.key = key
        self.port = 0
        self.loop = loop
        # The lines from the machine, read on ``loop``; None ends the stream.
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        # The characters waiting in the inbox, and whether they passed the
        # bound: written from both loops, under ``_lock``.
        self.held = 0
        self.flooded = False
        self.ended = asyncio.Event()
        # Its machine's link went: the session's tools are gone with it.
        self.lost = False
        self.on_end = on_end

    def frame(self, kind: str, **fields) -> dict:
        return {"type": kind, "session_id": self.session_id, "server": self.server, "key": self.key, **fields}


_children: dict[tuple[str, str, str], _Child] = {}
_lock = threading.Lock()
# One line per machine about refused frames: a stale burst must not flood the log.
_refused_logged: set[str] = set()


def _manager():
    from core.remote.satellite_connection import get_connection_manager
    return get_connection_manager()


def _encodes_too_large(line: str) -> bool:
    return len(line) > _SAFE_LINE_BYTES and len(json.dumps(line)) > MAX_FRAME_BYTES


def _send_soon(machine_id: str, frame: dict) -> None:
    """Runs on the main loop. A connection that is gone drops the frame:
    ``machine_dropped`` has ended the stream."""
    conn = _manager().get_connection(machine_id)
    if conn is not None:
        conn.enqueue_send_nowait(frame)


def _deliver(child: _Child, line: str | None) -> None:
    """Into the child's inbox, from any thread. The ``mcp-io`` loop is gone
    after the proxy's stop, and a frame handler must never raise."""
    with contextlib.suppress(RuntimeError):
        child.loop.call_soon_threadsafe(child.inbox.put_nowait, line)


def _hold(child: _Child, line: str) -> None:
    """One line of a tool's stdout for its reader. A tool that outruns the
    reader past the bound is told to end and its later lines are dropped."""
    with _lock:
        if child.flooded:
            return
        child.held += len(line)
        child.flooded = flooded = child.held > MAX_INBOX_CHARS
    if not flooded:
        _deliver(child, line)
        return
    logger.warning("tool %s of session %s: ended, its answers were not being read", child.server, child.session_id[:8])
    _send_soon(child.machine_id, child.frame("tool_close"))
    _finish(child)


def _finish(child: _Child, *, lost: bool = False) -> None:
    """The child's stream ends and whoever waits for its exit goes on."""
    if lost:
        child.lost = True
    _deliver(child, None)
    with contextlib.suppress(RuntimeError):
        child.loop.call_soon_threadsafe(child.ended.set)


def _accepted(machine_id: str, msg: dict) -> _Child | None:
    slot = (machine_id, str(msg.get("session_id")), str(msg.get("server")))
    with _lock:
        child = _children.get(slot)
    if child is not None and child.key == msg.get("key"):
        return child
    if machine_id not in _refused_logged:
        _refused_logged.add(machine_id)
        logger.warning("tool relay: dropped a %s from machine %s for a tool it does not run here",
                       msg.get("type"), machine_id[:8])
    return None


def on_out(machine_id: str, msg: dict) -> None:
    """``tool_out``: one line of a tool's stdout."""
    child = _accepted(machine_id, msg)
    line = msg.get("line")
    if child is not None and isinstance(line, str):
        _hold(child, line)


def on_exit(machine_id: str, msg: dict) -> None:
    """``tool_exit``: the tool ended."""
    child = _accepted(machine_id, msg)
    if child is not None:
        _finish(child)


def machine_dropped(machine_id: str) -> None:
    """The machine's link went: every tool on it ended (the machine closed
    them at the drop) and their sessions are lost."""
    with _lock:
        children = [c for c in _children.values() if c.machine_id == machine_id]
    for child in children:
        _finish(child, lost=True)
    _refused_logged.discard(machine_id)
    if children:
        logger.info("tool relay: %d tool process(es) ended with machine %s", len(children), machine_id[:8])


def port_current(machine_id: str, port: int) -> bool:
    """Whether a tool started on ``port`` can still reach the platform: the
    machine serves that port now (a tool holds it in its ``PROXY_URL``)."""
    live = _manager().tunnel_port(machine_id)
    return not live or not port or live == port


def session_lost(machine_id: str, session_id: str) -> bool:
    """Whether a session's tools on a machine are gone: its link went, or
    the machine no longer serves the port a tool reaches the platform by."""
    with _lock:
        children = [c for c in _children.values()
                    if c.machine_id == machine_id and c.session_id == session_id]
    return any(c.lost or not port_current(machine_id, c.port) for c in children)


def live_count() -> int:
    with _lock:
        return len(_children)


@asynccontextmanager
async def relay_client(
    machine_id: str, session_id: str, server: str, *,
    description: dict, argv: list[str], env: dict[str, str],
    main_loop: asyncio.AbstractEventLoop,
    on_end: Callable[[bool], None] | None = None,
    exit_block: dict | None = None,
    tool_dir: str = "",
) -> AsyncIterator[tuple]:
    """A tool process on ``machine_id``, as ``stdio_client`` is one on this
    host: yields its ``(read_stream, write_stream)``. Entered and exited in
    one task, like the SDK's own. ``on_end`` is told when the stream ends,
    with whether its machine went. ``exit_block`` is the session's door for
    the machine's credential exit (the start payload's ``exit`` shape),
    carried on the open. ``tool_dir`` is the folder of a tool the machine
    builds itself: the machine refuses the open while the folder is not
    there."""
    read_stream_writer: MemoryObjectSendStream[SessionMessage | Exception]
    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
    write_stream: MemoryObjectSendStream[SessionMessage]
    write_stream_reader: MemoryObjectReceiveStream[SessionMessage]
    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)
    loop = asyncio.get_running_loop()
    key = secrets.token_hex(16)
    child = _Child(machine_id, session_id, server, key, loop, on_end)
    slot = (machine_id, session_id, server)
    # Taken before the open's first await: a frame for the child finds it.
    with _lock:
        if slot in _children:
            raise RuntimeError(f"tool {server} is already open for session {session_id[:8]}")
        _children[slot] = child

    async def _open() -> None:
        manager = _manager()
        child.port = int(manager.tunnel_port(machine_id) or 0)
        await manager.send_command(
            machine_id,
            child.frame("tool_open", sandbox=description, argv=argv, env=env, tunnel_port=child.port,
                        **({"exit": exit_block} if exit_block else {}),
                        **({"tool_dir": tool_dir} if tool_dir else {})),
            timeout=_OPEN_TIMEOUT_S,
        )

    async def _close_streams() -> None:
        for stream in (read_stream, write_stream, read_stream_writer, write_stream_reader):
            with contextlib.suppress(Exception):
                await stream.aclose()

    def _drop_slot() -> None:
        with _lock:
            if _children.get(slot) is child:
                del _children[slot]

    opened = False
    try:
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_open(), main_loop))
        opened = True
    except RuntimeError as exc:
        if child.lost or _manager().get_connection(machine_id) is None:
            raise LinkLost(str(exc)) from exc
        raise
    finally:
        if not opened:
            # Cancelled or refused: a child the machine had already spawned closes.
            with contextlib.suppress(RuntimeError):
                main_loop.call_soon_threadsafe(_send_soon, machine_id, child.frame("tool_close"))
            _drop_slot()
            await _close_streams()

    async def reader() -> None:
        async with read_stream_writer:
            while True:
                line = await child.inbox.get()
                if line is None:
                    break
                with _lock:
                    child.held = max(0, child.held - len(line))
                try:
                    message = types.JSONRPCMessage.model_validate_json(line)
                except Exception as exc:  # noqa: BLE001  # the SDK's stdio reader hands a bad line to the session the same way
                    logger.warning("tool %s: an unreadable line from the machine", server)
                    await read_stream_writer.send(exc)
                    continue
                await read_stream_writer.send(SessionMessage(message))
        if child.on_end is not None:
            child.on_end(child.lost)

    async def writer() -> None:
        async with write_stream_reader:
            async for session_message in write_stream_reader:
                line = session_message.message.model_dump_json(by_alias=True, exclude_none=True)
                if _encodes_too_large(line):
                    root = session_message.message.root
                    if isinstance(root, types.JSONRPCRequest):
                        child.inbox.put_nowait(json.dumps(
                            {"jsonrpc": "2.0", "id": root.id,
                             "error": {"code": -32603, "message": TOO_LARGE}}))
                    logger.warning("tool %s: a call too large to send was refused", server)
                    continue
                # The main loop is gone only at the proxy's stop.
                with contextlib.suppress(RuntimeError):
                    main_loop.call_soon_threadsafe(_send_soon, machine_id, child.frame("tool_in", line=line))

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(reader)
            tg.start_soon(writer)
            try:
                yield read_stream, write_stream
            finally:
                # The tool ends, and its exit is waited for so a quick reopen
                # under the same id never finds it still running.
                with contextlib.suppress(RuntimeError):
                    main_loop.call_soon_threadsafe(_send_soon, machine_id, child.frame("tool_close"))
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(child.ended.wait(), timeout=_CLOSE_WAIT_S)
                tg.cancel_scope.cancel()
    finally:
        _drop_slot()
        await _close_streams()

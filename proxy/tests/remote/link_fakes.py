"""Machines faked at the frame level, for the tests that measure what one
machine's trouble costs the others.

A ``FakeMachine`` stands where a satellite's socket would: the connection
manager's writer hands it each frame (``ws.send_text``) and its answers come
back through ``handle_message``, as a satellite's would. Four behaviours:

- healthy: every frame is answered at once;
- frozen: frames are taken until the socket's buffer is full, then the
  writer waits, and nothing is answered (a stopped process: the kernel keeps
  taking bytes for a while, the process reads none);
- starved: every frame is answered ``delay`` seconds after the one before;
- slow: the socket takes ``rate`` bytes a second (the writer waits for each
  frame as a paused transport makes it wait), then answers;
- flapping: ``flap`` connects and drops in a loop.

``thaw`` lets a frozen machine run again: it answers what it took, in order.
Nothing here opens a socket, starts a process or reads a clock but the
loop's own.
"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

HEALTHY = "healthy"
FROZEN = "frozen"
STARVED = "starved"
SLOW = "slow"

# What a stopped process's socket still takes before the writer waits: the
# kernel's buffers, far below the transport's own bound.
FROZEN_BUFFER_BYTES = 256 * 1024


class _FakeSocket:
    def __init__(self, machine: FakeMachine) -> None:
        self.machine = machine
        self.closed: tuple[int, str] | None = None

    async def send_text(self, text: str) -> None:
        if self.closed is not None:
            raise RuntimeError("the socket is closed")
        await self.machine._take(self, text)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


class FakeMachine:
    def __init__(self, cm, machine_id: str, *, mode: str = HEALTHY, delay: float = 0.0,
                 rate: float = 0.0, version: str = "9.9.9",
                 capabilities: dict | None = None) -> None:
        self.cm = cm
        self.machine_id = machine_id
        self.mode = mode
        self.delay = delay
        self.rate = rate
        self.version = version
        self.capabilities = dict(capabilities or {"mcp_gateway": True, "paced_transfers": True})
        self.frames: list[dict] = []          # every frame the machine read
        self.files: dict[tuple[str, str], bytes] = {}
        self.deleted: list[tuple[str, str]] = []
        self.gateway: dict[tuple[str, str], dict] = {}
        self.connects = 0
        self.report_id = ""
        self.conn = None
        self.ws: _FakeSocket | None = None
        self._buffered = 0
        self._held: list[tuple[_FakeSocket, dict]] = []
        self._thawed = asyncio.Event()
        self._chunks: dict[tuple[str, str], list[bytes]] = {}
        self._work: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._answering: set[asyncio.Task] = set()
        if mode != FROZEN:
            self._thawed.set()

    # -- the link ---------------------------------------------------------
    async def connect(self):
        self.ws = _FakeSocket(self)
        self.connects += 1
        from core.remote import satellite_link
        self.report_id = satellite_link.new_report_id()
        self.conn = await self.cm.register(
            self.machine_id, self.ws, dict(self.capabilities), satellite_version=self.version,
            report_id=self.report_id)
        return self.conn

    async def report(self, *, terminals=(), sessions=(), others=()) -> None:
        """The two alive frames a satellite sends after each sign-in."""
        await self.say({"type": "pty_alive", "session_ids": list(terminals),
                        "report_id": self.report_id})
        await self.say({"type": "sessions_alive", "report_id": self.report_id,
                        "sessions": [{"session_id": s} for s in sessions],
                        "other_sessions": [{"session_id": s} for s in others]})

    async def drop(self) -> None:
        """The socket closed: the handler's deregister, as the WS route runs it."""
        conn, self.conn = self.conn, None
        if self.ws is not None and self.ws.closed is None:
            self.ws.closed = (1006, "dropped")
        if conn is not None:
            await self.cm.deregister(self.machine_id, expected=conn)

    def freeze(self) -> None:
        self.mode = FROZEN
        self._thawed.clear()
        self._buffered = 0

    def thaw(self) -> None:
        """The process runs again: what its socket took is answered, in order."""
        self.mode = HEALTHY
        held, self._held = self._held, []
        self._thawed.set()
        for ws, msg in held:
            if ws.closed is None:
                self._answer_soon(msg)

    async def flap(self, times: int, *, up_s: float = 0.0) -> None:
        for _ in range(times):
            await self.connect()
            await asyncio.sleep(up_s)
            await self.drop()

    async def say(self, msg: dict) -> None:
        """A frame from the machine, as its socket's read loop hands it on."""
        await self.cm.handle_message(self.machine_id, msg)

    # -- frames from the platform ------------------------------------------
    async def _take(self, ws: _FakeSocket, text: str) -> None:
        msg = json.loads(text)
        if self.mode == FROZEN:
            self._buffered += len(text)
            if self._buffered > FROZEN_BUFFER_BYTES:
                await self._thawed.wait()
                if ws.closed is not None:
                    raise RuntimeError("the socket is closed")
                self._answer_soon(msg)
                return
            self._held.append((ws, msg))
            return
        if self.mode == SLOW:
            await asyncio.sleep(len(text) / self.rate)
            if ws.closed is not None:
                raise RuntimeError("the socket is closed")
            self._answer_soon(msg)
            return
        if self.mode == STARVED:
            self._work.put_nowait(msg)
            if self._worker is None or self._worker.done():
                self._worker = asyncio.ensure_future(self._starved_worker())
            return
        self._answer_soon(msg)

    async def _starved_worker(self) -> None:
        while True:
            msg = await self._work.get()
            await asyncio.sleep(self.delay)
            await self._answer(msg)

    def _answer_soon(self, msg: dict) -> None:
        task = asyncio.ensure_future(self._answer(msg))
        self._answering.add(task)
        task.add_done_callback(self._answering.discard)

    async def _answer(self, msg: dict) -> None:
        self.frames.append(msg)
        kind = msg.get("type")
        cid = msg.get("command_id")
        reply: dict | None = None
        if kind == "file_push":
            reply = self._file_push(msg)
        elif kind == "request_manifest":
            reply = {"type": "file_manifest", "command_id": cid, "files": [
                {"path": path, "size": len(data), "hash": "", "mtime": 0}
                for (agent, path), data in self.files.items() if agent == msg.get("agent_slug")]}
        elif kind == "file_stat":
            data = self.files.get((msg.get("agent_slug", ""), msg.get("path", "")))
            reply = {"type": "ack", "command_id": cid, "status": "ok",
                     "exists": data is not None, "size": len(data or b""), "mtime_ns": 0}
        elif kind == "mcp_gateway_token":
            self.gateway[(msg.get("session_id", ""), msg.get("mcp", ""))] = msg
            reply = {"type": "ack", "command_id": cid, "status": "ok"}
        elif kind == "check_session_process":
            reply = {"type": "ack", "command_id": cid, "status": "ok", "alive": True}
        elif kind in ("pong", "pty_close", "pty_input", "pty_resize", "file_pull_cancel"):
            reply = None
        elif cid:
            reply = {"type": "ack", "command_id": cid, "status": "ok"}
        if reply is not None and self.conn is not None:
            await self.cm.handle_message(self.machine_id, reply)

    def _file_push(self, msg: dict) -> dict | None:
        key = (msg.get("agent_slug", ""), msg.get("path", ""))
        action = msg.get("action")
        if action == "delete":
            self.files.pop(key, None)
            self.deleted.append(key)
            return None
        data = base64.b64decode(msg.get("content_b64") or "")
        committed = None
        if action == "write":
            self.files[key] = committed = data
        elif action == "write_chunk":
            parts = self._chunks.setdefault(key, [])
            parts.append(data)
            if msg.get("chunk_index") == msg.get("total_chunks", 0) - 1:
                self.files[key] = committed = b"".join(self._chunks.pop(key))
        reply = {"type": "ack", "command_id": msg.get("command_id"), "status": "ok"}
        if committed is not None:
            # A satellite reports the file it committed on that frame's ok.
            reply.update(size=len(committed), mtime_ns=0)
        return reply


def session(agent: str, machine_id: str, **extra) -> SimpleNamespace:
    """A remote layer's record of one live session on ``machine_id``."""
    return SimpleNamespace(agent_name=agent, machine_id=machine_id, alive=True, cli_dead=False,
                           turn_active=False, last_activity=0.0, **extra)


def security(agent: str, machine_id: str, *, username: str = "alice",
             role: str = "manager") -> SimpleNamespace:
    return SimpleNamespace(
        agent=agent, username=username, role=role, mount_username=None, knowledge_rw=False,
        placement=SimpleNamespace(machine_id=machine_id))


class Ticker:
    """How late the loop ran while a block was open: a 5 ms tick's worst gap."""

    def __init__(self, every: float = 0.005) -> None:
        self.every = every
        self.worst = 0.0
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> Ticker:
        self._task = asyncio.ensure_future(self._run())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(self.every)
            now = loop.time()
            self.worst = max(self.worst, now - last - self.every)
            last = now

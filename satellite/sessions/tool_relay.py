"""Direct LLM's tool processes on a compute machine.

Direct LLM's model loop and its MCP client stay in the platform; each stdio
tool process of such a session runs here, in the platform's sandbox. The
platform sends ``tool_open`` with the session's sandbox description, the
tool's argv and its environment. This module renders the description with
this machine's values (``sandbox_host``, one launcher and so one network
namespace per child, as a tool of a local session has), spawns the child and
moves its lines: ``tool_in`` to its stdin, ``tool_out`` from its stdout,
``tool_exit`` at its end. Only JSON-RPC lines cross, the MCP client is the
platform's.

A child belongs to the link that opened it. When the link goes, every child
closes (``close_all``): after a platform restart nothing would own it.

``tool_out`` and ``tool_exit`` ride the bounded bulk lane: a chatty tool
holds only itself back, never another session's control frames.

An open may name the tool's own folder (``tool_dir``, a community tool's):
one this machine has not built yet is answered with ``NOT_INSTALLED`` and
nothing is spawned.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import signal
from pathlib import Path
from typing import TYPE_CHECKING

from .._vendored import sandbox_render as render
from ..background import spawn
from . import sandbox_host
from .step_runner import _kill_later, _signal_step

if TYPE_CHECKING:
    from ..config import SatelliteConfig
    from ..transport.ws_client import SatelliteWSClient

logger = logging.getLogger("satellite")

#: The largest frame the relay sends, encoded. The link takes 16 MiB, and the
#: writer's ``json.dumps`` grows a line of Greek text threefold.
MAX_FRAME_BYTES = 12 * 1024 * 1024
#: Below this a line cannot encode past the cap (the worst case is sixfold),
#: so it is not measured.
_SAFE_LINE_BYTES = 2 * 1024 * 1024
_READ_CHUNK = 64 * 1024
#: What a line over the cap keeps for finding its JSON-RPC id.
_HEAD_KEEP = 4096
_TAIL_KEEP = 256
#: How long a child has between its stdin closing and its SIGTERM: the MCP
#: client's own ``PROCESS_TERMINATION_TIMEOUT``.
_CLOSE_GRACE_S = 2.0
#: A child whose stdout ended is waited for this long before it is killed.
_EXIT_WAIT_S = 5.0
_STDERR_KEEP = 2048

TOO_LARGE = "The tool's answer was too large to send."
#: What an open of a tool whose folder is not on this machine is answered.
NOT_INSTALLED = "This tool is being installed. Try again in a minute."

# A top-level id at the head of a message (pydantic's order: jsonrpc, id) or
# at its end (the TypeScript SDK's order). An unanchored search would take a
# nested "id" of the result for it.
_ID_VALUE = r'("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?)'
_ID_AT_HEAD = re.compile(rb'^\s*\{\s*(?:"jsonrpc"\s*:\s*"2\.0"\s*,\s*)?"id"\s*:\s*' + _ID_VALUE.encode())
_ID_AT_TAIL = re.compile(rb'"id"\s*:\s*' + _ID_VALUE.encode() + rb'\s*\}\s*$')


def oversize_answer(head: bytes, tail: bytes) -> str:
    """The JSON-RPC error answering a line too large to send, from the part
    of it that was kept; ``""`` when no top-level id can be read there."""
    found = _ID_AT_TAIL.search(tail) or _ID_AT_HEAD.search(head)
    if found is None:
        return ""
    try:
        request_id = json.loads(found.group(1))
    except ValueError:
        return ""
    return json.dumps({"jsonrpc": "2.0", "id": request_id,
                       "error": {"code": -32603, "message": TOO_LARGE}})


class _Child:
    __slots__ = ("session_id", "server", "key", "proc", "inbox", "closing")

    def __init__(self, session_id: str, server: str, key: str) -> None:
        self.session_id = session_id
        self.server = server
        self.key = key
        self.proc: asyncio.subprocess.Process | None = None
        # The stdin lines in arrival order; None ends the stdin.
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        # Set by a close, also one that lands while the open is in flight.
        self.closing = False

    def frame(self, kind: str, **fields) -> dict:
        return {"type": kind, "session_id": self.session_id, "server": self.server,
                "key": self.key, **fields}


class ToolRelay:
    """The tool children of the sessions the platform relays here, by
    ``(session, server)``."""

    def __init__(self, sat_config: SatelliteConfig) -> None:
        self.config = sat_config
        self._children: dict[tuple[str, str], _Child] = {}

    def __len__(self) -> int:
        return len(self._children)

    def pids(self) -> list[tuple[int, str]]:
        """``(pid, session_id)`` of each running child, for the memory guard."""
        return [(c.proc.pid, c.session_id) for c in list(self._children.values()) if c.proc is not None]

    # -- the frames -------------------------------------------------------

    async def open(self, msg: dict, ws: SatelliteWSClient) -> None:
        """``tool_open``: render, spawn, answer the ack. The slot is taken
        before the first await, so a close that lands during the open marks
        the child and the open's end closes it."""
        command_id = msg.get("command_id", "")

        async def ack(error: str = "") -> None:
            await ws.enqueue_send({"type": "ack", "command_id": command_id,
                                   "status": "error" if error else "ok",
                                   **({"error": error} if error else {})})

        if not self.config.compute:
            await ack("This machine does not run relayed tools.")
            return
        session_id, server, key = msg.get("session_id"), msg.get("server"), msg.get("key")
        argv, env, sandbox = msg.get("argv"), msg.get("env"), msg.get("sandbox")
        if not (session_id and server and key and isinstance(session_id, str)
                and isinstance(server, str) and isinstance(key, str)):
            await ack("the open names no session, server or key")
            return
        if not isinstance(sandbox, dict):
            # Never a bare command: a tool runs in the session's sandbox or not at all.
            await ack("the open carries no sandbox description")
            return
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)
                and isinstance(env, dict)):
            await ack("the open's command is malformed")
            return
        if msg.get("tool_dir") is not None and (refusal := self._tool_dir_refusal(msg["tool_dir"])):
            await ack(refusal)
            return
        slot = (session_id, server)
        if slot in self._children:
            await ack("this tool is already running for the session")
            return
        child = _Child(session_id, server, key)
        self._children[slot] = child
        try:
            # The session's credential exit door from the frame's ``exit``
            # block (the same shape a start payload carries), ahead of the
            # render, which binds the bundle.
            from . import credential_exit
            await asyncio.to_thread(credential_exit.start, session_id, msg)
            command = self._command(msg, sandbox, argv)
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={str(k): str(v) for k, v in env.items()},
                start_new_session=True,
            )
        except (render.RenderRefused, OSError, ValueError, TypeError, KeyError, AttributeError) as e:
            self._forget(child)
            logger.warning("tool %s of session %s not started: %s", server, session_id[:8], e)
            await ack(f"the tool could not start: {e}")
            return
        child.proc = proc
        spawn(self._serve(child, ws), name="tool_serve", owner="tool_relay")
        if child.closing:
            self._terminate(child)
            await ack("the tool was closed while it started")
            return
        await ack()

    def _tool_dir_refusal(self, tool_dir) -> str:
        """Why an open that names its tool's folder is refused, ``""`` when
        the folder is there: one outside this machine's tools folder is
        malformed, one not there yet is a tool still to be installed."""
        from ..host import auth_paths
        root = Path(self.config.mcps_dir)
        if not (isinstance(tool_dir, str) and Path(tool_dir).is_absolute()
                and auth_paths.is_path_under_root(Path(tool_dir), root)
                and Path(tool_dir).resolve() != root.resolve()):
            return "the open's tool folder is malformed"
        return "" if Path(tool_dir).is_dir() else NOT_INSTALLED

    async def take_in(self, msg: dict, ws: SatelliteWSClient) -> None:
        """``tool_in``: one line for a child's stdin. The first step is
        synchronous, so frames spawned in arrival order keep it."""
        child = self._child_of(msg)
        if child is None:
            await self._refuse(msg, ws)
            return
        child.inbox.put_nowait(str(msg.get("line") or ""))

    async def close(self, msg: dict, ws: SatelliteWSClient) -> None:
        """``tool_close``: the child's stdin ends after what is queued, and
        its tree is told to end after the grace."""
        child = self._child_of(msg)
        if child is None:
            await self._refuse(msg, ws)
            return
        child.closing = True
        if child.proc is None:
            return
        child.inbox.put_nowait(None)
        await asyncio.sleep(_CLOSE_GRACE_S)
        self._terminate(child)

    def close_all(self, reason: str) -> int:
        """Every child ended (the link dropped, the satellite stops): no
        platform can read them any more."""
        children = list(self._children.values())
        self._children.clear()
        for child in children:
            child.closing = True
            self._terminate(child)
        from . import credential_exit
        for sid in {child.session_id for child in children}:
            credential_exit.stop(sid)
        if children:
            logger.info("tool relay: closed %d tool process(es): %s", len(children), reason)
        return len(children)

    # -- one child ---------------------------------------------------------

    def _command(self, msg: dict, sandbox: dict, argv: list[str]) -> list[str]:
        """The session's description rendered around the tool's argv, with
        this machine's values, below the satellite's CPU priority."""
        desc = render.SandboxDescription.from_dict(sandbox)
        host = sandbox_host.render_host(
            self.config, tunnel_port=sandbox_host.link_port(msg), tmp_size_mb=int(desc.tmp_size_mb or 0),
        )
        return sandbox_host.niced(sandbox_host.with_oom_raise(render.render(desc, host, argv)))

    def _child_of(self, msg: dict) -> _Child | None:
        child = self._children.get((msg.get("session_id"), msg.get("server")))
        return child if child is not None and child.key == msg.get("key") else None

    def _forget(self, child: _Child) -> None:
        slot = (child.session_id, child.server)
        if self._children.get(slot) is child:
            del self._children[slot]
        if not any(k[0] == child.session_id for k in self._children):
            # The session's last tool here is gone: its exit door with it.
            from . import credential_exit
            credential_exit.stop(child.session_id)

    async def _refuse(self, msg: dict, ws: SatelliteWSClient) -> None:
        """A frame for a child this machine does not run, or with another
        key: the platform's stream ends, and nothing here is touched."""
        logger.warning("tool relay: refused %s for session %s server %s",
                       msg.get("type"), str(msg.get("session_id"))[:8], msg.get("server"))
        await ws.enqueue_bulk({"type": "tool_exit", "session_id": msg.get("session_id"),
                               "server": msg.get("server"), "key": msg.get("key"),
                               "code": None, "reason": "not running"})

    def _terminate(self, child: _Child) -> None:
        proc = child.proc
        if proc is None or proc.returncode is not None:
            return
        try:
            _kill_later(proc, _signal_step(proc, signal.SIGTERM))
        except Exception:  # noqa: BLE001  # one child's kill must not spare the others
            logger.warning("tool %s: kill failed", child.server, exc_info=True)

    async def _serve(self, child: _Child, ws: SatelliteWSClient) -> None:
        """The child's whole life: its pipes, then ``tool_exit`` after the
        last ``tool_out`` (the same lane, so the end cannot pass the output)."""
        proc = child.proc
        assert proc is not None and proc.stdout is not None and proc.stderr is not None
        stdin = asyncio.ensure_future(self._pump_stdin(child))
        stderr = asyncio.ensure_future(self._drain_stderr(proc.stderr))
        reason = ""
        tail = b""
        try:
            reason = await self._pump_stdout(child, ws)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_EXIT_WAIT_S)
            with contextlib.suppress(asyncio.TimeoutError, OSError):
                tail = await asyncio.wait_for(asyncio.shield(stderr), timeout=1.0)
        finally:
            if proc.returncode is None:
                self._terminate(child)
            stdin.cancel()
            stderr.cancel()
            self._forget(child)
        if proc.returncode not in (0, None) and not child.closing:
            logger.info("tool %s of session %s exited %s: %s", child.server, child.session_id[:8],
                        proc.returncode, tail.decode("utf-8", "replace"))
        await ws.enqueue_bulk(child.frame("tool_exit", code=proc.returncode, reason=reason))

    async def _pump_stdin(self, child: _Child) -> None:
        proc = child.proc
        assert proc is not None and proc.stdin is not None
        try:
            while True:
                line = await child.inbox.get()
                if line is None:
                    break
                proc.stdin.write(line.encode("utf-8") + b"\n")
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            with contextlib.suppress(OSError, RuntimeError):
                proc.stdin.close()

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> bytes:
        """Read by chunks, keeping the last of it: a full pipe would hang the
        child, and a very long line would end a line reader."""
        tail = b""
        while True:
            chunk = await stream.read(_READ_CHUNK)
            if not chunk:
                return tail
            tail = (tail + chunk)[-_STDERR_KEEP:]

    async def _pump_stdout(self, child: _Child, ws: SatelliteWSClient) -> str:
        """Every complete line out as a ``tool_out``. A line past the cap is
        dropped as it arrives, its first and last bytes kept to answer its
        call. The return is why the child ended, ``""`` for its own end."""
        proc = child.proc
        assert proc is not None and proc.stdout is not None
        buf = bytearray()
        head: bytes | None = None
        tail = b""
        while True:
            chunk = await proc.stdout.read(_READ_CHUNK)
            if not chunk:
                return ""
            while chunk:
                end = chunk.find(b"\n")
                piece, chunk = (chunk, b"") if end < 0 else (chunk[:end], chunk[end + 1:])
                if head is None:
                    buf += piece
                    if len(buf) > MAX_FRAME_BYTES:
                        head, tail = bytes(buf[:_HEAD_KEEP]), bytes(buf[-_TAIL_KEEP:])
                        buf.clear()
                else:
                    tail = (tail + piece)[-_TAIL_KEEP:]
                if end < 0:
                    continue
                if head is None:
                    line = bytes(buf).decode("utf-8", "replace").rstrip("\r")
                    buf.clear()
                    if not line:
                        continue
                    if len(line) > _SAFE_LINE_BYTES and len(json.dumps(line)) > MAX_FRAME_BYTES:
                        data = line.encode("utf-8")
                        answer = oversize_answer(data[:_HEAD_KEEP], data[-_TAIL_KEEP:])
                    else:
                        answer = line
                else:
                    answer = oversize_answer(head, tail)
                    head, tail = None, b""
                if not answer:
                    self._terminate(child)
                    return "A line of the tool's output was too large to send."
                if not await ws.enqueue_bulk(child.frame("tool_out", line=answer)):
                    self._terminate(child)
                    return "the link dropped"

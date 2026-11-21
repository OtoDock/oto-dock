"""App servers on a compute machine.

The platform's supervisor owns every decision about an app server: when it
wakes, whether it is healthy, its backoff, its log. This module starts the
process the platform planned, in the sandbox as this machine renders it, and
reports what happens to it.

**The way in.** The server listens inside its namespace and the sandbox's
inbound splice stays on this machine's loopback. pasta delivers only a
loopback peer through a splice; any other peer reaches the namespace from its
real address, and the reply is refused by the sandbox's private-range routes.
So a relay on this machine's address on its link, which admits only the
platform's end of that link, pipes each connection to the loopback port. The
platform dials that relay. Nothing else of the machine is reachable that way.

**A server outlives the link, not the platform.** A dropped link stops
nothing: the platform dials the relay, not the link. Every start names the
platform process that asked (``boot``), and at each sign-in the platform
sweeps: a server another process started is stopped, because the launch token
it holds is one the platform that came back never minted. A link down past
``LINK_DOWN_STOP_S`` stops them all, so a server cannot outlive a platform that
never returns or no longer takes this machine. The satellite's own exit ends
them too (the launcher's parent-death chain).
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import dataclasses
import ipaddress
import logging
import os
import re
import shutil
import signal
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .._vendored import sandbox_render as render
from ..background import spawn
from . import sandbox_host

if TYPE_CHECKING:
    from ..config import SatelliteConfig

logger = logging.getLogger("satellite")

#: A server that dies in its first second never reaches the platform's health
#: wait: its tail is the reason, and a bind failure tries another port.
START_WAIT_S = 1.0
BIND_RETRIES = 3
STOP_GRACE_S = 5.0
# Above the platform's 75 s silent-link rule, so a link it still holds stops nothing.
LINK_DOWN_STOP_S = 90.0
_LINK_WATCH_S = 15.0
_FLUSH_BYTES = 16 * 1024
_FLUSH_S = 0.25
_DRAIN_S = 2.0
_TAIL_LINES = 12
_PIPE_LIMIT = 1024 * 1024
_RELAY_CHUNK = 64 * 1024
_ENTRY_RE = re.compile(r"^server/index\.(ts|js)$")
_INBOUND_RE = re.compile(r"^\d{1,5}:\d{1,5}$")


class AppServerRefused(Exception):
    """This server does not start here; the reason goes back in the ack."""


@dataclass
class _Server:
    server_id: str
    boot: str
    app_id: str
    instance: str
    proc: asyncio.subprocess.Process | None = None
    relay: asyncio.AbstractServer | None = None
    host_port: int = 0
    pump: asyncio.Task | None = None
    watch: asyncio.Task | None = None
    conns: set[asyncio.StreamWriter] = field(default_factory=set)
    tail: list[str] = field(default_factory=list)
    stopping: bool = False


def _uuid_ok(value: object) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def _ipv4(value: object) -> str:
    """``value`` as a plain IPv4 address, "" when it is anything else."""
    try:
        addr = ipaddress.ip_address(str(value))
    except ValueError:
        return ""
    return str(addr) if addr.version == 4 and not addr.is_unspecified else ""


def _link_addresses(ws) -> tuple[str, str]:
    """``(this machine's end, the platform's end)`` of the live link: the
    relay binds the first and admits only the second, the address the
    platform dials from. An address read from the socket, never from a frame."""
    conn = getattr(ws, "_ws", None)
    local = _ipv4((getattr(conn, "local_address", None) or ("",))[0])
    remote = _ipv4((getattr(conn, "remote_address", None) or ("",))[0])
    if not local or not remote:
        raise AppServerRefused("This machine's link to the platform has no IPv4 address.")
    return local, remote


def _free_loopback_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """One direction of a relayed connection, with backpressure; end of
    input half-closes the other side, so a request and its answer both finish."""
    with contextlib.suppress(ConnectionError, OSError, RuntimeError):
        while True:
            data = await reader.read(_RELAY_CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()


class AppServers:
    """The app servers this machine runs, by the platform's ``server_id``."""

    def __init__(self, config: SatelliteConfig) -> None:
        self.config = config
        self.servers: dict[str, _Server] = {}
        self._link_task: asyncio.Task | None = None

    def _plan(self, msg: dict) -> tuple[render.SandboxDescription, list[str], dict[str, str]]:
        if not getattr(self.config, "compute", False):
            raise AppServerRefused("App servers run on compute machines alone.")
        if not _uuid_ok(msg.get("server_id")):
            raise AppServerRefused("The app server has no valid id.")
        if str(msg.get("server_id")) in self.servers:
            raise AppServerRefused("This app server is already running here.")
        sandbox, argv, env = msg.get("sandbox"), msg.get("argv"), msg.get("env")
        if not isinstance(sandbox, dict):
            raise AppServerRefused("The app server has no sandbox description.")
        if (not isinstance(argv, list) or len(argv) != 3 or argv[:2] != ["bun", "run"]
                or not _ENTRY_RE.match(str(argv[2]))):
            raise AppServerRefused("The app server's command is not one this machine runs.")
        if not isinstance(env, dict):
            raise AppServerRefused("The app server has no environment.")
        desc = render.SandboxDescription.from_dict(sandbox)
        if not _INBOUND_RE.match(desc.inbound):
            raise AppServerRefused("The app server has no port to listen on.")
        return desc, [str(a) for a in argv], {str(k): str(v) for k, v in env.items()}

    async def start(self, msg: dict, ws) -> None:
        """``app_server_start``: ack ``host_port`` once the process survived
        its first second and the relay is open, else the reason and its tail.
        The id is recorded before the first await, so a sweep sees a start
        in flight and a stop can abort it."""
        command_id = str(msg.get("command_id") or "")

        async def ack(**fields) -> None:
            await ws.enqueue_send({"type": "ack", "command_id": command_id, **fields})

        try:
            desc, argv, env = self._plan(msg)
        except (AppServerRefused, render.RenderRefused, KeyError, TypeError, ValueError) as e:
            await ack(status="error", error=str(e))
            return
        server = _Server(server_id=str(msg["server_id"]), boot=str(msg.get("boot") or ""),
                         app_id=str(msg.get("app_id") or ""), instance=str(msg.get("instance") or ""))
        self.servers[server.server_id] = server
        try:
            local, remote = _link_addresses(ws)
            await self._spawn(server, desc, argv, env, ws)
            if server.stopping:
                raise AppServerRefused("The app server was stopped while it started.")
            await self._open_relay(server, local, remote)
        except (AppServerRefused, render.RenderRefused, OSError, ValueError) as e:
            await self._end(server)
            said = "\n".join(server.tail[-_TAIL_LINES:])
            await ack(status="error", error=f"{e}\n{said}" if said else str(e))
            return
        server.watch = spawn(self._watch(server, ws), name="app-server-watch", owner="apps")
        self._arm_link_watch(ws)
        logger.info("App server %s (%s) up on relay port %d", server.server_id[:8], server.instance,
                    server.host_port)
        await ack(status="ok", host_port=server.host_port, pid=server.proc.pid if server.proc else 0)

    async def _spawn(self, server: _Server, desc: render.SandboxDescription, argv: list[str],
                     env: dict[str, str], ws) -> None:
        bun = shutil.which("bun")
        if not bun:
            raise AppServerRefused("Bun is not installed on this machine.")
        tunnel = getattr(ws, "tunnel", None)
        listener = int(getattr(tunnel, "app_port", 0) or 0)
        if listener <= 0:
            # A zero here would splice the sandbox onto the bare proxy port.
            raise AppServerRefused("This machine cannot reach the platform, so the app did not start.")
        port = desc.inbound.split(":", 1)[1]
        for _attempt in range(BIND_RETRIES):
            loopback = _free_loopback_port()
            placed = dataclasses.replace(desc, inbound=f"{loopback}:{port}")
            cmd = sandbox_host.render_command(placed, self.config, tunnel_port=listener,
                                              bin_path=bun, argv=argv, swap_binary=True)
            proc = await asyncio.create_subprocess_exec(
                *cmd, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True, limit=_PIPE_LIMIT,
            )
            server.proc, server.host_port = proc, loopback
            server.pump = spawn(self._pump(server, ws), name="app-server-pump", owner="apps")
            await asyncio.sleep(START_WAIT_S)
            if proc.returncode is None:
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(server.pump, timeout=_DRAIN_S)
            said = "\n".join(server.tail[-_TAIL_LINES:]).lower()
            if "bind" not in said and "address" not in said:
                break
        raise AppServerRefused("The app server exited at once.")

    async def _open_relay(self, server: _Server, local: str, remote: str) -> None:
        target = server.host_port

        async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            peer = writer.get_extra_info("peername")
            if not peer or peer[0] != remote:
                logger.warning("App server %s: a connection from %s refused",
                               server.server_id[:8], peer[0] if peer else "?")
                writer.close()
                return
            try:
                up_reader, up_writer = await asyncio.open_connection("127.0.0.1", target,
                                                                     limit=_RELAY_CHUNK)
            except OSError:
                writer.close()
                return
            server.conns.update((writer, up_writer))
            try:
                await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
            finally:
                for w in (writer, up_writer):
                    server.conns.discard(w)
                    w.close()

        relay = await asyncio.start_server(on_connect, local, 0, limit=_RELAY_CHUNK)
        server.relay = relay
        server.host_port = int(relay.sockets[0].getsockname()[1])

    async def _pump(self, server: _Server, ws) -> None:
        """The child's one pipe, coalesced onto the bounded bulk lane: a
        full lane waits, so a server in a print loop slows to the link's
        speed, and a link that is down drops the lines (log lines only)."""
        proc = server.proc
        if proc is None or proc.stdout is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending: list[str] = []
        size = 0

        async def flush() -> None:
            nonlocal pending, size
            if pending:
                text = "".join(pending)
                pending, size = [], 0
                await ws.enqueue_bulk({"type": "app_server_output", "server_id": server.server_id,
                                       "text": text})

        with contextlib.suppress(ConnectionError, OSError):
            while True:
                try:
                    # A line a server prints before going quiet goes out now,
                    # not with its next print.
                    chunk = await asyncio.wait_for(proc.stdout.read(65536), timeout=_FLUSH_S if pending else None)
                except asyncio.TimeoutError:
                    await flush()
                    continue
                if not chunk:
                    break
                text = decoder.decode(chunk)
                server.tail.extend(text.splitlines())
                del server.tail[:-_TAIL_LINES]
                pending.append(text)
                size += len(text)
                if size >= _FLUSH_BYTES:
                    await flush()
            await flush()

    async def _watch(self, server: _Server, ws) -> None:
        """Report the exit after the last line (the same lane, so the exit
        cannot pass the output), unless a stop asked for it."""
        proc = server.proc
        if proc is None:
            return
        # The return code, not wait(): wait() also waits for the pipe, which
        # a stray child can hold open.
        while proc.returncode is None:  # noqa: ASYNC110  # wait() also waits for the pipe a stray child may hold
            await asyncio.sleep(0.1)
        if server.pump is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(server.pump), timeout=_DRAIN_S)
        self._kill_group(server, signal.SIGKILL)
        self._close_relay(server)
        self.servers.pop(server.server_id, None)
        if server.stopping:
            return
        frame = {"type": "app_server_exit", "server_id": server.server_id, "code": proc.returncode}
        if not await ws.enqueue_bulk(frame):
            await ws.enqueue_send(frame)

    def _kill_group(self, server: _Server, sig: int) -> None:
        proc = server.proc
        if proc is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, sig)

    def _close_relay(self, server: _Server) -> None:
        if server.relay is not None:
            server.relay.close()
            server.relay = None
        for w in list(server.conns):
            w.close()
        server.conns.clear()

    async def _end(self, server: _Server) -> None:
        """TERM to the group, a grace, KILL. Safe on a start in progress: the
        start reads ``stopping`` and refuses."""
        server.stopping = True
        self._close_relay(server)
        proc = server.proc
        if proc is not None and proc.returncode is None:
            self._kill_group(server, signal.SIGTERM)
            deadline = time.monotonic() + STOP_GRACE_S
            while proc.returncode is None and time.monotonic() < deadline:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
            self._kill_group(server, signal.SIGKILL)
        self.servers.pop(server.server_id, None)

    async def stop(self, msg: dict, ws) -> None:
        """``app_server_stop``: always acks; a server that is not here is
        already stopped."""
        server = self.servers.get(str(msg.get("server_id") or ""))
        if server is not None:
            await self._end(server)
        await ws.enqueue_send({"type": "ack", "command_id": str(msg.get("command_id") or ""),
                               "status": "ok", "stopped": server is not None})

    async def sweep(self, msg: dict, ws) -> None:
        """``app_server_sweep``: end every server another platform process
        started; ack the rest, those still starting included."""
        boot = str(msg.get("boot") or "")
        stale = [s for s in self.servers.values() if s.boot != boot]
        live = [s.server_id for s in self.servers.values() if s.boot == boot and not s.stopping]
        await ws.enqueue_send({"type": "ack", "command_id": str(msg.get("command_id") or ""),
                               "status": "ok", "live": live})
        for server in stale:
            logger.info("App server %s: stopped, another platform process started it",
                        server.server_id[:8])
            await self._end(server)

    def _arm_link_watch(self, ws) -> None:
        if self._link_task is None or self._link_task.done():
            self._link_task = spawn(self._watch_link(ws), name="app-server-link", owner="apps")

    async def _watch_link(self, ws) -> None:
        down_since: float | None = None
        while self.servers:
            await asyncio.sleep(_LINK_WATCH_S)
            if ws._authenticated:
                down_since = None
                continue
            if down_since is None:
                down_since = time.monotonic()
            elif time.monotonic() - down_since >= LINK_DOWN_STOP_S:
                logger.warning("The link has been down for %ds: stopping %d app server(s)",
                               int(LINK_DOWN_STOP_S), len(self.servers))
                for server in list(self.servers.values()):
                    await self._end(server)
                down_since = None

    def kill_all(self) -> int:
        """At the satellite's stop. Synchronous: nothing may outlive it."""
        count = 0
        for server in list(self.servers.values()):
            self._close_relay(server)
            self._kill_group(server, signal.SIGKILL)
            count += 1
        self.servers.clear()
        return count

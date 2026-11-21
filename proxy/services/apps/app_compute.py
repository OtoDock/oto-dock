"""An app server on a compute machine.

The supervisor (``app_supervisor``) stays the only owner of a server's life:
when it wakes, its health, its backoff, its crash notice, its log. This module
is where the server runs: it plans a start for the machine, reads the frames
that come back and hands them to the supervisor as a ``RemoteProcess``.

**The way in.** The machine's relay listens on its own end of its link, and
the proxy dials the link's raw peer, the address the link's own rule already
judged (``ws/satellite.compute_link_refusal``). The address comes from the
socket, never a frame.

**A restart closes a server.** Each proxy process has a ``BOOT``, every start
names it, and at a compute machine's sign-in the sweep stops whatever another
process started. A launch token is judged against this process's registry, and
a server is restartable on demand, so nothing else recovers it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import ipaddress
import logging
import secrets
import time
import uuid
from pathlib import Path

from core.background import spawn
from core.sandbox import sandbox_render as _render
from core.sandbox.sandbox import SandboxBuilder
from services.apps import app_sandbox
from services.scripts.runner import scrub

logger = logging.getLogger("otodock.apps")

#: This process's identity to the machines: a server that names another one
#: holds a launch token this process never minted.
BOOT = secrets.token_hex(8)
START_ACK_S = 30.0
SWEEP_ACK_S = 15.0
_STOP_ACK_MARGIN_S = 10.0
_LINE_LIMIT = 1024 * 1024
#: What may wait for the log writer. No pipe backs this reader, so past this
#: a line is dropped: it is a log, never the proxy's memory.
_OUTPUT_HELD_MAX = 8 * 1024 * 1024
_REFUSED_PREFIX = "Satellite command error:"
#: For this long after a boot a live server waits for the install's own compute
#: machine rather than open a database the previous process's server still holds.
BOOT_GRACE_S = 30.0
_grace_until = 0.0

# server id -> the process the supervisor holds for it.
_servers: dict[str, RemoteProcess] = {}


def _cm():
    from core.remote.satellite_connection import get_connection_manager
    return get_connection_manager()


#: What an app's page, its deploy and its render say with no compute machine.
NOT_CONNECTED = "The compute service is not connected, so the app did not start."


def place() -> dict | None:
    """The compute machine a new app server, step or script starts on, None
    when none is signed in (nothing starts: the platform runs none itself):
    the one answer a new session gets, among the machines that share the
    platform's folders (an app's data folder is the platform's alone)."""
    from services.remote.remote_status import serving_compute_machine
    return serving_compute_machine(shared_folder=True)


def awaited() -> bool:
    """True while a new live server waits for the install's own compute machine."""
    from services.remote import compute_bootstrap
    return time.monotonic() < _grace_until and place() is None and compute_bootstrap.runs_own()


def machine_address(machine_id: str) -> str:
    """The compute machine's address on the network only the proxy shares
    with it: the raw peer of its live link, IPv4, judged as the link was."""
    from auth import lan_check
    from core.config import deployment
    conn = _cm().get_connection(machine_id)
    if conn is None:
        raise app_sandbox.AppStartError(NOT_CONNECTED)
    peer = lan_check.resolve(getattr(conn.ws, "scope", None) or {}).peer
    try:
        ok = ipaddress.ip_address(peer).version == 4 and deployment.compute_link_allowed(peer)
    except ValueError:
        ok = False
    if not ok:
        raise app_sandbox.AppStartError("The platform cannot reach the compute service, so the app did not start.")
    return str(peer)


def describe(row: dict, release_dir: Path, data_dir: Path, allow_hosts: list[str] | None,
             port: int = app_sandbox.APP_PORT) -> dict:
    """The sandbox of a server the machine renders: every source below the
    agent's own folder and held by the shim, the machine's own Bun, the
    inbound port its own to pick."""
    cfg = app_sandbox.build_config(row, release_dir, data_dir, allow_hosts, port=port)
    desc = SandboxBuilder(cfg).describe()
    if any(b.root == _render.ROOT_ABS for b in (*desc.binds, *desc.extra_binds)):
        raise app_sandbox.AppStartError("The app's files are not in its agent's folder.")
    return dataclasses.replace(desc, beneath=True).to_dict()


class RemoteProcess:
    """An app server on a machine, read through the attributes of a process:
    ``pid``, ``returncode``, ``stdout`` and ``wait()``."""

    def __init__(self, machine_id: str, inst) -> None:
        self.machine_id = machine_id
        self.server_id = str(uuid.uuid4())
        self.inst = inst
        self.pid = 0
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader(limit=_LINE_LIMIT)
        self.registered_at = time.monotonic()
        self._done = asyncio.Event()
        _servers[self.server_id] = self

    def feed(self, text: str) -> None:
        if self.returncode is None and len(self.stdout._buffer) < _OUTPUT_HELD_MAX:  # type: ignore[attr-defined]  # the reader has no public size
            self.stdout.feed_data(text.encode("utf-8", "replace"))

    def finish(self, code: int) -> None:
        """The server is over: its last line was fed, its code recorded."""
        if self.returncode is not None:
            return
        self.returncode = code
        self.stdout.feed_eof()
        self._done.set()
        _servers.pop(self.server_id, None)

    async def wait(self) -> int:
        await self._done.wait()
        return self.returncode if self.returncode is not None else -1

    async def end(self, grace: float) -> None:
        """Stop it on the machine and wait for the answer. A machine that is
        gone has already ended it."""
        if self.returncode is not None:
            return
        try:
            await _cm().send_command(self.machine_id, {"type": "app_server_stop", "server_id": self.server_id},
                                     timeout=grace + _STOP_ACK_MARGIN_S)
        except RuntimeError:
            logger.debug("App server %s: the stop had no answer", self.server_id[:8])
        self.finish(-15)

    def lost(self) -> None:
        """The machine no longer runs it, through no fault of the app's: no
        backoff and no notice (``_watch`` reads a stop), and the next request
        starts a new one. A stop goes out in case it only looks gone."""
        if self.returncode is not None:
            return
        if self.inst is not None:
            self.inst.stop_requested = True
        self.finish(-1)
        spawn(_stop_quietly(self.machine_id, self.server_id), name="app-server-stop", owner="apps")


async def _stop_quietly(machine_id: str, server_id: str) -> None:
    try:
        await _cm().send_command(machine_id, {"type": "app_server_stop", "server_id": server_id}, timeout=15.0)
    except RuntimeError:
        logger.debug("App server %s: the stop had no answer", server_id[:8])


async def launch(inst, proc: RemoteProcess, *, preview: bool, allow_hosts: list[str] | None,
                 secrets_env: dict[str, str] | None) -> None:
    """Start ``inst``'s server on ``proc``'s machine. The ack carries the
    relay's port; a refusal's text may hold what the server printed, so it is
    scrubbed before anything reads it."""
    from services.apps import app_tokens
    row = inst.row
    inst.address = machine_address(proc.machine_id)
    frame = {
        "type": "app_server_start", "server_id": proc.server_id, "boot": BOOT, "app_id": row["id"],
        "instance": inst.name, "sandbox": describe(row, inst.release_dir, inst.data_dir, allow_hosts),
        "argv": ["bun", "run", inst.entry],
        "env": app_sandbox.build_env(row, inst.token, app_tokens.public_key_b64(row["id"]),
                                     preview=preview, secrets=secrets_env),
    }
    try:
        ack = await _cm().send_command(proc.machine_id, frame, timeout=START_ACK_S)
    except RuntimeError as e:
        refused = str(e).startswith(_REFUSED_PREFIX)
        if not refused:
            # No answer: the machine may still bring it up, and with the link
            # up no sweep would ever find it.
            spawn(_stop_quietly(proc.machine_id, proc.server_id), name="app-server-stop", owner="apps")
        proc.finish(-1)
        text = scrub(str(e), inst.scrub)
        raise app_sandbox.AppStartError(
            text.split(_REFUSED_PREFIX, 1)[-1].strip() if refused
            else "The compute service did not answer, so the app did not start.") from e
    proc.pid = int(ack.get("pid") or 0)
    inst.host_port = int(ack.get("host_port") or 0)
    if inst.host_port <= 0:
        proc.finish(-1)
        raise app_sandbox.AppStartError("The compute service did not start the app.")


def on_frame(machine_id: str, msg: dict) -> None:
    """``app_server_output`` and ``app_server_exit``. A frame for a server
    another machine started is ignored."""
    proc = _servers.get(str(msg.get("server_id") or ""))
    if proc is None or proc.machine_id != machine_id:
        return
    if msg.get("type") == "app_server_output":
        proc.feed(str(msg.get("text") or ""))
    elif msg.get("type") == "app_server_exit":
        code = msg.get("code")
        proc.finish(code if isinstance(code, int) else -1)


def unreachable(inst) -> None:
    """A dial of a server failed. With the machine's link down it is gone
    for all we know, so the next request starts a new one; with the link up
    the machine's exit frame decides, so a crash keeps its backoff."""
    proc = inst.proc
    if isinstance(proc, RemoteProcess) and not _cm().is_connected(proc.machine_id):
        proc.lost()


async def on_alive_report(report) -> None:
    """At a compute machine's sign-in: the sweep, in a task of its own so the
    sign-in never waits on it."""
    if _cm().is_compute(report.machine_id):
        spawn(_sweep(report), name="app-server-sweep", owner="apps")


async def _sweep(report) -> None:
    """Close what another process started, and reconcile what this one holds:
    a server the machine no longer runs is lost (one registered after the
    sweep went out is a start still in flight), and a server the machine runs
    that nobody here holds is stopped."""
    cm = _cm()
    sent_at = time.monotonic()
    try:
        ack = await cm.send_command(report.machine_id, {"type": "app_server_sweep", "boot": BOOT},
                                    timeout=SWEEP_ACK_S)
    except RuntimeError:
        logger.debug("App servers: the sweep of %s had no answer", report.machine_id[:8])
        return
    live = {str(s) for s in ack.get("live") or []}
    if not report.is_current():
        return
    for proc in list(_servers.values()):
        if proc.machine_id == report.machine_id and proc.registered_at < sent_at and proc.server_id not in live:
            logger.info("App server %s is gone from its machine", proc.server_id[:8])
            proc.lost()
    for server_id in live - set(_servers):
        spawn(_stop_quietly(report.machine_id, server_id), name="app-server-orphan", owner="apps")


def register() -> None:
    """Boot: the sweep at every compute machine's sign-in, after the start's
    own wait for that machine, and the grace counted from here."""
    from core.remote import satellite_link
    global _grace_until
    _grace_until = time.monotonic() + BOOT_GRACE_S
    satellite_link.add_alive_report_hook(on_alive_report, name="app_servers")

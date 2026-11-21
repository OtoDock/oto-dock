"""Install ahead: a machine gets its tools before a chat needs them.

A personal or admin machine gets the tools of the agents that run on it
about a minute after an agent is assigned to it (``after_assignment``: an
assignment taken back within the minute installs nothing, the run reads what
is wanted when it fires) and within seconds of a tool being enabled for such
an agent (``after_enable``). A compute machine on another host gets every
tool any agent can use there, since any chat may land on it. A tool installed,
updated or enabled on the platform goes to the machines ``machines_for_tool``
names. A machine's sign-in installs what it lacks and what it holds outdated
(``after_signin``: a release's updates of the bundled tools too), spread over
a minute and a few machines at a time, so a release's reconnects never send
every tool to every machine at once. Whether a machine takes updates ahead
at all is one rule, ``takes_updates``, which every run reads.

One run per machine at a time (``_locks``): a door that fires during a run
waits for it and then plans what is left. Every run goes through the one
sync, ``mcp_sync.sync_mcps_for_session``, one tool at a time: the machine's
install lock is taken per tool, so a session start waits for one tool at
most, and a chat that starts meanwhile reads the run's other tools as not
installed yet, with no promise. An ahead run never removes a tool the
platform still has.

The compute machine beside the platform keeps the community tools in a
folder of its own and builds them there (``builds_own_tools``): it gets every
community tool any agent can use, at once at its sign-in (one machine, no
spread), and a run also takes away the tools the platform no longer has (no
session's sync ever reaches it). One that claims no tools folder gets none
of this. The state the machine's card shows (what is installing, what
failed) is the machine's memo (``machine_tools``), in memory: the next run
or sign-in reports again.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass

from core import placement
from core.background import spawn
from services.remote import machine_tools

logger = logging.getLogger("otodock.tools-ahead")

#: How long after an agent's assignment its machine is synced: a misclick
#: taken back within it installs nothing.
ASSIGN_DELAY_S = 60.0
#: How long after a tool's enablement (a burst of toggles makes one run).
ENABLE_DELAY_S = 5.0
#: A run is never pushed later than this after its first schedule.
MAX_DELAY_S = 180.0
#: A sign-in's run starts within this spread, a few machines at a time.
SIGNIN_SPREAD_S = 60.0
SIGNIN_AT_ONCE = 4


@dataclass
class _Pending:
    first: float
    due: float
    task: asyncio.Task | None = None


_pending: dict[str, _Pending] = {}
_locks: dict[str, asyncio.Lock] = {}
_signin_gate: asyncio.Semaphore | None = None


def card(machine_id: str) -> dict | None:
    """The machine's tools as its card shows them, by their labels, or None
    when there is nothing to say (nothing installing, nothing failed)."""
    st = machine_tools.of(machine_id)
    if not (st.installing or st.failed):
        return None
    return {"installing": [_label(n) for n in st.installing],
            "failed": {_label(n): cause for n, cause in st.failed.items()}}


def forget(machine_id: str) -> None:
    """The machine was removed: its pending run, its lock and its memo go
    with its row."""
    _pending.pop(machine_id, None)
    _locks.pop(machine_id, None)
    machine_tools.forget(machine_id)


# ── the doors ─────────────────────────────────────────────────────────────────

def after_assignment(machine_id: str) -> None:
    """An agent's machine was set (by an admin, an agent's manager or a
    person for their own machine), or a device grant on it changed."""
    schedule(machine_id, ASSIGN_DELAY_S)


def after_enable(agent: str) -> None:
    """A tool was enabled (or made reachable) for ``agent``: every machine
    the agent runs on gets it."""
    spawn(_after_enable(agent), name="tools_ahead_enable", owner="tools_ahead")


def after_tool_change(mcp_name: str, *, update: bool) -> None:
    """A tool was installed (``update`` False) or updated on the platform,
    or enabled platform-wide: the machines ``machines_for_tool`` names get it."""
    spawn(_after_tool_change(mcp_name, update), name="tools_ahead_tool", owner="tools_ahead")


def after_uninstall() -> None:
    """A tool was uninstalled: the compute machine that builds its own tools
    takes it away at a run of its own (``mcp_sync.remove_gone``: no
    session's sync reaches it). A paired machine drops it at its next
    session's start, as before."""
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    for machine_id in cm.compute_machine_ids():
        if cm.builds_own_tools(machine_id):
            schedule(machine_id, ENABLE_DELAY_S)


async def after_signin(machine_id: str) -> None:
    """A machine signed in: it gets what it lacks and, when it
    ``takes_updates``, what it holds outdated, after a random part of
    ``SIGNIN_SPREAD_S`` and with at most ``SIGNIN_AT_ONCE`` machines at a
    time across the platform."""
    global _signin_gate
    from core.remote.satellite_connection import get_connection_manager
    if get_connection_manager().builds_own_tools(machine_id):
        # The install's own machine: its sessions wait on this run alone.
        await run(machine_id)
        return
    if _signin_gate is None:
        _signin_gate = asyncio.Semaphore(SIGNIN_AT_ONCE)
    await asyncio.sleep(random.uniform(0, SIGNIN_SPREAD_S))
    async with _signin_gate:
        await run(machine_id)


async def _after_enable(agent: str) -> None:
    for machine_id in await asyncio.to_thread(_machines_of_agent, agent):
        schedule(machine_id, ENABLE_DELAY_S)


async def _after_tool_change(mcp_name: str, update: bool) -> None:
    for machine_id in await asyncio.to_thread(machines_for_tool, mcp_name, update=update):
        schedule(machine_id, ENABLE_DELAY_S)


def schedule(machine_id: str, delay_s: float) -> None:
    """One pending run per machine: a later call moves it to ``delay_s``
    from now, a shorter one brings it closer, and never past
    ``MAX_DELAY_S`` after its first schedule."""
    now = time.monotonic()
    pending = _pending.get(machine_id)
    if pending is None:
        pending = _pending[machine_id] = _Pending(first=now, due=now + delay_s)
    else:
        pending.due = min(now + delay_s, pending.first + MAX_DELAY_S)
    if pending.task is None or pending.task.done():
        pending.task = spawn(_fire(machine_id), name="tools_ahead_fire", owner="tools_ahead")


async def _fire(machine_id: str) -> None:
    while True:
        pending = _pending.get(machine_id)
        if pending is None:
            return
        wait = pending.due - time.monotonic()
        if wait <= 0:
            break
        await asyncio.sleep(min(wait, 1.0))
    _pending.pop(machine_id, None)
    await run(machine_id)


# ── who gets what ─────────────────────────────────────────────────────────────

def takes_updates(row: dict) -> bool:
    """Whether a machine gets a tool's new version ahead, not only a tool it
    lacks: every machine does (the operator, 2026-11-11). Every run reads
    this rule, so narrowing updates to compute machines on another host is
    this one line: ``return placement.machine_is_compute(row)``."""
    return True


def machines_for_tool(mcp_name: str, *, update: bool) -> list[str]:
    """The connected machines that get ``mcp_name`` now: every machine whose
    agents may use it (a compute machine on another host whenever any agent
    can), and for an update only those that ``takes_updates``. The run then
    installs it where it is missing or outdated."""
    return [machine_id for machine_id, row in _connected_machines()
            if (not update or takes_updates(row)) and mcp_name in desired_for(machine_id, row=row)]


def _connected_machines() -> list[tuple[str, dict]]:
    """``(id, row)`` of every connected machine an ahead run serves: the
    ones the platform installs tools on (``takes_tools``: personal and admin
    machines, compute machines on another host, and the one beside the
    platform once it keeps a tools folder of its own)."""
    from core.remote.satellite_connection import get_connection_manager
    from storage import remote_store
    cm = get_connection_manager()
    return [(machine_id, row) for row in remote_store.get_all_remote_machines()
            if cm.takes_tools(machine_id := str(row.get("id") or ""))]


def _machines_of_agent(agent: str) -> list[str]:
    """The connected machines ``agent``'s sessions may run on: its own, a
    person's own choice for it, and every compute machine on another host."""
    return [mid for mid, row in _connected_machines() if _serves_agent(row, agent)]


def _serves_agent(row: dict, agent: str) -> bool:
    if placement.machine_is_compute(row):
        return True
    from storage import remote_store
    machine_id = str(row.get("id") or "")
    return (agent in remote_store.get_agents_for_machine(machine_id)
            or any(a == agent for _, a in remote_store.get_user_targets_on_machine(machine_id)))


def desired_for(machine_id: str, *, row: dict | None = None) -> set[str]:
    """The tools the machine should hold: for every agent that runs there
    (a compute machine: every agent), what a session of it there could get
    (``mcp_sync.machine_admitted``), never more. The machine that builds its
    own tools gets those of every agent whatever its engine: a Direct LLM
    agent's tools run there, a chat may run an agent on another engine than
    its default, and no session's sync ever installs a tool on it. Reads
    the database: off the loop."""
    from core.remote.satellite_connection import get_connection_manager
    from storage import remote_store
    if row is None:
        row = remote_store.get_remote_machine(machine_id)
    if not row:
        return set()
    from services.mcp import mcp_sync
    every_engine = get_connection_manager().builds_own_tools(machine_id)
    out: set[str] = set()
    for agent, kind, role in _agents_on(row):
        if every_engine or _runs_on_machines(agent):
            out |= mcp_sync.machine_admitted(agent, role=role, placement=placement.from_machine(kind, row))
    return out


def _agents_on(row: dict) -> list[tuple[str, str, str]]:
    """``(agent, placement kind, role)`` of every agent whose sessions run on
    the machine. On a person's own machine the owner's role (a viewer's when
    it cannot be read, so nothing is installed that a session would refuse);
    on an admin machine a manager's, as its sessions are the admins' trust."""
    from auth import providers, roles
    from storage import remote_store
    from storage.agents import agent_store
    machine_id = str(row.get("id") or "")
    if placement.machine_is_compute(row):
        return [(a["slug"], placement.KIND_COMPUTE, roles.MANAGER) for a in agent_store.get_all_agents()]
    owner = str(row.get("registered_by") or "")
    admin_paired = placement.machine_is_admin_paired(row)
    out: list[tuple[str, str, str]] = []
    for agent in remote_store.get_agents_for_machine(machine_id):
        kind = placement.KIND_ADMIN_REMOTE if admin_paired else placement.KIND_USER_REMOTE
        out.append((agent, kind, roles.MANAGER if admin_paired else _role(providers, owner, agent)))
    for user_sub, agent in remote_store.get_user_targets_on_machine(machine_id):
        out.append((agent, placement.KIND_USER_REMOTE, _role(providers, user_sub, agent)))
    return out


def _role(providers, user_sub: str, agent: str) -> str:
    from auth import roles
    try:
        return providers.effective_role_of(user_sub, agent) or roles.VIEWER
    except Exception:  # noqa: BLE001  # an unreadable role installs the least a session could get
        return roles.VIEWER


def _runs_on_machines(agent: str) -> bool:
    """False for an agent whose engine starts no session on a machine
    (Direct LLM: its tools run on the machine beside the platform alone)."""
    from core.session.session_manager import get_layer_capabilities
    from storage.agents import agent_store
    caps = get_layer_capabilities((agent_store.get_agent(agent) or {}).get("execution_path") or "")
    return caps is None or caps.runtime.supports_remote_execution


# ── a run ─────────────────────────────────────────────────────────────────────

async def run(machine_id: str) -> None:
    """Install what the machine lacks of ``desired_for`` and, when it
    ``takes_updates``, what it holds outdated (``mcp_sync.pending_for``),
    one tool at a time, never removing one; the card shows those tools alone.
    One run per machine: a second waits for the first and then plans what
    is left. Offline: nothing (its sign-in runs it)."""
    async with _locks.setdefault(machine_id, asyncio.Lock()):
        await _run_alone(machine_id)


async def _run_alone(machine_id: str) -> None:
    from core.remote.satellite_connection import get_connection_manager
    from services.mcp import mcp_sync
    from storage import remote_store
    if not get_connection_manager().is_connected(machine_id):
        return
    row = await asyncio.to_thread(remote_store.get_remote_machine, machine_id)
    if not row:
        return
    install_only = not takes_updates(row)
    wanted = await asyncio.to_thread(desired_for, machine_id, row=row)
    st = machine_tools.of(machine_id)
    todo = sorted(await mcp_sync.pending_for(machine_id, wanted, install_only=install_only)) if wanted else []
    # The card shows what this run installs: a failure of a tool the machine
    # no longer gets, or now holds, is not its to show.
    st.failed = {n: cause for n, cause in st.failed.items() if n in todo}
    st.failed_at = {n: at for n, at in st.failed_at.items() if n in st.failed}
    if get_connection_manager().builds_own_tools(machine_id):
        # No session's sync reaches this machine: the run also takes away
        # what the platform no longer has (an uninstalled tool).
        await mcp_sync.remove_gone(machine_id)
    if not todo:
        return
    try:
        for i, name in enumerate(todo):
            st.installing = todo[i:]
            result = await mcp_sync.sync_mcps_for_session(
                machine_id, "", [name], remove_unwanted=False, install_only=install_only)
            failed = result.failed or {}
            if failed.get("__connection__"):
                break
            if failed.get(name):
                machine_tools.note_failed(machine_id, name, failed[name])
            elif result.ok:
                machine_tools.note_built(machine_id, name)
    finally:
        st.installing = []


def _label(name: str) -> str:
    from services.mcp import mcp_registry
    manifest = mcp_registry.get_manifest(name)
    return (getattr(manifest, "label", "") or name) if manifest else name

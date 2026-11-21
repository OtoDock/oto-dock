"""One machine's trouble stays that machine's.

A frozen machine (a stopped process: its socket takes a little, answers
nothing), a starved one (every answer half a minute late) and a flapping one
(it signs in and drops, over and over) sit beside two healthy machines, all
holding one agent. Every bound of the platform is the shipped one (a push's
90 s stall, the barrier's 60 s, an ack's 10 and 15 s): anything that waited
on a troubled machine would overrun the test, whose bounds sit below those and
well above a slow runner's disk. What is measured is the
healthy machines and the platform's own work: a tool's write, a save, a tree
of files, a turn's barrier, a machine's own file change, its credentials,
its idle session, a new machine's sign-in, and the loop itself.

Then the frozen machine is cut and comes back, and nothing it missed is
lost: the files it was owed, its credentials, its held session.

What this file reaches and what it does not. It catches the couplings of a
write waiting on a machine, of the loop, of a flapping machine's sign-ins, of
the credential tick, of the barrier and of the reaper. These sit in narrower
modules, because no machine here does what they need:

- a slow machine's file changes holding fleet slots, and the path and fan-out
  locks (no troubled machine sends a file change here):
  ``test_file_changed_bounds.py``;
- the background merge's gates (merges are mocks here):
  ``test_sync_pipelining.py``;
- the byte bound on a machine's queue: ``test_send_queue_bound.py``;
- a frozen machine noticed by the monitor, every frame counting as contact
  (the monitor does not run here, the drop is called by hand):
  ``test_satellite_link.py``;
- a name kept, retried, judged again or overflowing:
  ``test_machine_push_lane.py``.

Machines are faked at the frame level (``link_fakes``): no socket, no
process, no real wait. With ``LINK_MEASURE_OUT`` set, the times go to that
file as JSON lines.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
from core.credentials import mcp_broker, mcp_gateway
from core.credentials.mcp_gateway import GatewayCredential
from core.remote import (
    machine_push_lane,
    mcp_gateway_push,
    remote_file_flow,
    remote_reaper,
    satellite_connection as sc,
    satellite_file_transfer as sft,
    upload_inflight,
)
from services.infra import file_bookkeeping
from services.remote import workspace_fanout
from tests._timing import host_factor
from tests.remote import link_fakes as lf

AGENT = "isolation-agent"
# Enough that a write held per file would overrun: what it measures past that is
# the tree's own store writes, which a slow runner's disk stretches.
TREE_FILES = 300
OUT = os.environ.get("LINK_MEASURE_OUT", "")


class _Times:
    """What each step took, judged once the block is over: the host's
    factor is a reference run on this thread, and one inside the block would
    be the loop's worst gap."""

    def __init__(self) -> None:
        self.factor = host_factor()
        self.rows: list[tuple[str, float, float]] = []

    def step(self, name: str, limit: float) -> "_Step":
        return _Step(self, name, limit)

    def note(self, name: str, seconds: float, limit: float) -> None:
        self.rows.append((name, seconds, limit))

    async def until(self, check, timeout: float) -> None:
        deadline = time.monotonic() + timeout * self.factor
        while not check():
            assert time.monotonic() < deadline, "the condition never held"
            await asyncio.sleep(0.01)

    def judge(self) -> None:
        factor = max(self.factor, host_factor())
        if OUT:
            with open(OUT, "a", encoding="utf-8") as fh:
                for name, seconds, limit in self.rows:
                    fh.write(json.dumps({"measure": name, "seconds": round(seconds, 4),
                                         "bound_s": limit}) + "\n")
        late = [f"{name}: {seconds:.2f} s (bound {limit} s)"
                for name, seconds, limit in self.rows if seconds >= limit * factor]
        assert not late, late


class _Step:
    def __init__(self, times: _Times, name: str, limit: float) -> None:
        self.times, self.name, self.limit = times, name, limit

    def __enter__(self) -> "_Step":
        self.started = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.times.note(self.name, time.monotonic() - self.started, self.limit)


@pytest.fixture
def fleet(temp_db, monkeypatch, tmp_path):
    agents = tmp_path / "agents"
    (agents / AGENT / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    cm = sc.SatelliteConnectionManager()
    monkeypatch.setattr(sc, "_connection_manager", cm)
    monkeypatch.setattr(sc, "_persist_write", lambda *a, **k: None)
    from core.session import session_manager, session_state
    from storage import remote_store
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda mid: {"name": mid})
    layer = SimpleNamespace(
        _sessions={}, _cm=cm,
        sync_all_agents_on_reconnect=AsyncMock(), sync_agent_in_background=AsyncMock(),
        moved_port_idle_sessions=lambda mid: [], is_spawning=lambda sid: False,
        resolve_machine_sync_identity=AsyncMock(return_value=None),
        probe_session_process_dead=AsyncMock(return_value=False),
    )
    layer.owns_session = lambda sid: sid in layer._sessions
    layer.machine_of_session = lambda sid: getattr(layer._sessions.get(sid), "machine_id", "")
    layer.is_session_grace_held = lambda sid: False

    async def close_session(sid: str) -> None:
        info = layer._sessions.pop(sid, None)
        if info is not None:
            with contextlib.suppress(RuntimeError):
                await cm.send_command(info.machine_id, {"type": "close_session",
                                                        "session_id": sid}, timeout=15.0)

    layer.close_session = close_session
    monkeypatch.setattr(session_manager, "_remote_layer", layer)
    monkeypatch.setattr(session_state, "cached_idle_timeout", AsyncMock(return_value=60.0))
    monkeypatch.setattr(session_state, "has_pending_prompt", lambda sid: False)
    monkeypatch.setattr(remote_reaper, "_idle_session_has_pending_work", lambda *a, **k: False)
    from services.remote import chat_move, switch_leftovers
    from services.scheduler import run_recovery
    for mod, name in ((run_recovery, "sweep_expired"), (chat_move, "sweep"),
                      (switch_leftovers, "sweep")):
        monkeypatch.setattr(mod, name, AsyncMock())
    monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda u: None)
    machine_push_lane.reset()
    remote_file_flow._platform_ahead.clear()
    remote_file_flow._global_path_locks.clear()
    remote_file_flow._fanout_locks.clear()
    workspace_fanout._idle_identities.clear()
    mcp_gateway_push._registry.clear()
    remote_reaper._running.clear()
    sft.LAST_FILE_CHANGED.clear()
    made: list[str] = []

    def hold(sid: str, machine_id: str, *, idle_for: float = 0.0) -> None:
        layer._sessions[sid] = lf.session(AGENT, machine_id)
        layer._sessions[sid].last_activity = time.monotonic() - idle_for
        session_state._session_security[sid] = lf.security(AGENT, machine_id)
        made.append(sid)

    def write(rel: str, data: bytes):
        path = agents / AGENT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    yield SimpleNamespace(cm=cm, layer=layer, agents=agents, hold=hold, write=write)
    machine_push_lane.reset()
    remote_file_flow._platform_ahead.clear()
    remote_file_flow._global_path_locks.clear()
    remote_file_flow._fanout_locks.clear()
    mcp_gateway_push._registry.clear()
    for _loop, task in list(mcp_gateway_push._machine_ticks.values()):
        task.cancel()
    mcp_gateway_push._machine_ticks.clear()
    mcp_gateway_push._machine_again.clear()
    for task in list(remote_reaper._running.values()):
        task.cancel()
    remote_reaper._running.clear()
    sft._IN_FLIGHT.clear()
    for sid in made:
        session_state._session_security.pop(sid, None)
        mcp_broker.purge_session(sid)


def _credential(sid: str, machine_id: str) -> None:
    from storage.identity import bearer_allowlist
    key = f"gw-{uuid.uuid4().hex[:6]}"
    bearer_allowlist.add_allowed(key, "mcp.example.com", "test")
    mcp_broker.provision(sid, {"vendor": mcp_broker.SecretBundle(gateway=GatewayCredential(
        upstream="https://mcp.example.com", path="/mcp", allowlist_key=key, value="tok"))})
    mcp_gateway_push.register(sid, machine_id, uuid.uuid4().hex * 2)


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_frozen_a_starved_and_a_flapping_machine_leave_the_rest_as_they_were(fleet):
    cm = fleet.cm
    h1, h2 = lf.FakeMachine(cm, "m-h1"), lf.FakeMachine(cm, "m-h2")
    frozen = lf.FakeMachine(cm, "m-frozen")
    starved = lf.FakeMachine(cm, "m-starved", mode=lf.STARVED, delay=30.0)
    flapper = lf.FakeMachine(cm, "m-flap")
    for machine in (h1, h2, frozen, starved):
        await machine.connect()
    fleet.hold("s-h1", "m-h1")
    fleet.hold("s-h2", "m-h2")
    fleet.hold("s-frozen", "m-frozen")
    fleet.hold("s-starved", "m-starved")
    held_queue = cm.create_session_queue("m-frozen", "s-frozen", "claude-code-cli", maxsize=8)
    for sid, mid in (("s-h1", "m-h1"), ("s-frozen", "m-frozen"), ("s-starved", "m-starved")):
        _credential(sid, mid)
    # Idle sessions for the reaper: three on the frozen machine, one healthy.
    for n in range(3):
        fleet.hold(f"s-frozen-idle{n}", "m-frozen", idle_for=10_000)
    fleet.hold("s-h2-idle", "m-h2", idle_for=10_000)
    for n in range(TREE_FILES):
        fleet.write(f"workspace/tree/f{n:04d}.txt", f"file {n}".encode())
    await asyncio.sleep(0.05)
    frozen.freeze()

    times = _Times()
    flapping = asyncio.ensure_future(flapper.flap(40, up_s=0.02))
    async with lf.Ticker() as ticker:
        # A local chat's tool writes a file (Direct LLM's Write, a display
        # tool's file): the platform's copy, then the hand-over.
        path = fleet.write("workspace/tool.txt", b"from a local tool")
        with times.step("a local tool's write returns", 5.0):
            await file_bookkeeping.push_file_write(AGENT, "workspace/tool.txt", path, writer="alice")
        with times.step("the healthy machines hold the file", 5.0):
            await times.until(lambda: all(m.files.get((AGENT, "workspace/tool.txt")) == b"from a local tool"
                                          for m in (h1, h2)), 15)

        # A workspace save in the dashboard is the same call with another writer.
        path = fleet.write("workspace/notes.md", b"saved in the dashboard")
        with times.step("a dashboard save returns", 5.0):
            await file_bookkeeping.push_file_write(AGENT, "workspace/notes.md", path, writer="bob")

        # A tree of files (a folder move, an import): names, not held work.
        with times.step("a tree write returns", 20.0):
            await file_bookkeeping.push_tree_write(
                AGENT, fleet.agents / AGENT / "workspace" / "tree", fleet.agents / AGENT, writer="bob")
        with times.step("a healthy machine holds the whole tree", 30.0):
            await times.until(lambda: sum(1 for (_a, rel) in h1.files
                                          if rel.startswith("workspace/tree/")) == TREE_FILES, 60)

        # A turn on a healthy machine: its barrier is open, with its files there.
        with times.step("a healthy machine's turn barrier", 5.0):
            assert await upload_inflight.wait_settled(AGENT, machine_id="m-h1") is True
        assert h1.files[(AGENT, "workspace/notes.md")] == b"saved in the dashboard"

        # A file pasted on a lane quiet for longer than the wait's bound: the
        # turn waits for it, with the clock starting at the write.
        await asyncio.sleep(1.1)
        photo = fleet.write("workspace/photo.png", b"a pasted photo")
        h1.delay, h1.mode = 0.05, lf.STARVED
        await file_bookkeeping.push_file_write(AGENT, "workspace/photo.png", photo, writer="alice")
        with times.step("a turn waits for a file on a quiet lane", 5.0):
            assert await upload_inflight.wait_settled(AGENT, machine_id="m-h1", timeout=1.0) is True
        assert h1.files[(AGENT, "workspace/photo.png")] == b"a pasted photo"
        h1.mode = lf.HEALTHY

        # A one-line save behind a large push of the same healthy machine.
        h2.rate, h2.mode = 1024 * 1024, lf.SLOW
        large = fleet.write("workspace/large.bin", b"x" * (4 * 1024 * 1024))
        await file_bookkeeping.push_file_write(AGENT, "workspace/large.bin", large, writer="alice")
        await times.until(lambda: (AGENT, "workspace/large.bin") in machine_push_lane._lanes["m-h2"].inflight, 5)
        line = fleet.write("workspace/one-line.txt", b"one line")
        await file_bookkeeping.push_file_write(AGENT, "workspace/one-line.txt", line, writer="alice")
        with times.step("a one-line save passes a large push", 5.0):
            await times.until(lambda: (AGENT, "workspace/one-line.txt") in h2.files, 15)
        assert (AGENT, "workspace/large.bin") not in h2.files
        h2.mode = lf.HEALTHY
        await times.until(lambda: (AGENT, "workspace/large.bin") in h2.files, 10)

        # A healthy machine's own file change lands and reaches the other one.
        await h1.say({"type": "file_changed", "agent_slug": AGENT, "action": "write",
                      "path": "workspace/from-h1.txt", "session_id": "s-h1",
                      "hash": "sha256:" + hashlib.sha256(b"written on h1").hexdigest(),
                      "content_b64": base64.b64encode(b"written on h1").decode()})
        with times.step("a healthy machine's change reaches the platform and the other machine", 8.0):
            await times.until(lambda: (fleet.agents / AGENT / "workspace/from-h1.txt").is_file()
                              and h2.files.get((AGENT, "workspace/from-h1.txt")) == b"written on h1", 15)

        # Its credential is renewed while two machines answer nothing in time.
        started = time.monotonic()
        whole_tick = asyncio.ensure_future(mcp_gateway_push.tick())
        await times.until(lambda: ("s-h1", "vendor") in h1.gateway, 15)
        times.note("a healthy machine's credential is renewed", time.monotonic() - started, 5.0)
        assert not whole_tick.done()

        # Its idle session is reaped while the frozen machine's closes wait.
        with times.step("the reaper's tick returns", 5.0):
            await remote_reaper.reap_once()
        with times.step("a healthy machine's idle session is closed", 8.0):
            await times.until(lambda: "s-h2-idle" not in fleet.layer._sessions, 15)
        assert "s-frozen-idle2" in fleet.layer._sessions

        # A new machine signs in.
        with times.step("another machine signs in", 5.0):
            await lf.FakeMachine(cm, "m-new").connect()

        await asyncio.wait_for(flapping, 20 * times.factor)
    times.note("the loop's worst gap", ticker.worst, 0.25)
    times.judge()

    # The flapping machine cost its sign-ins and nothing more: each step
    # once in its window (the verify frame, the machine row's read).
    assert flapper.connects == 40
    assert len([f for f in flapper.frames if f.get("type") == "sync_mcps_verify"]) <= 2

    # The frozen machine is still owed everything, by name.
    assert machine_push_lane.owed("m-frozen", AGENT, "workspace/tool.txt") == machine_push_lane.WRITE
    assert machine_push_lane.owed("m-frozen", AGENT, f"workspace/tree/f{TREE_FILES - 1:04d}.txt") == machine_push_lane.WRITE

    # It is cut (the heartbeat monitor's drop) and comes back: nothing is lost.
    whole_tick.cancel()
    await frozen.drop()
    assert cm.is_session_in_grace("m-frozen", "s-frozen")
    frozen.thaw()
    conn = await frozen.connect()
    assert conn.session_queues["s-frozen"] is held_queue            # its held session, adopted again
    times.rows.clear()
    with times.step("the returned machine holds every file it missed", 30.0):
        await times.until(lambda: frozen.files.get((AGENT, "workspace/tool.txt")) == b"from a local tool"
                          and frozen.files.get((AGENT, "workspace/notes.md")) == b"saved in the dashboard"
                          and frozen.files.get((AGENT, "workspace/from-h1.txt")) == b"written on h1"
                          and sum(1 for (_a, rel) in frozen.files
                                  if rel.startswith("workspace/tree/")) == TREE_FILES, 60)
    mcp_gateway_push.tick_machine_soon("m-frozen")                   # the sign-in's own call
    await times.until(lambda: ("s-frozen", "vendor") in frozen.gateway, 5)
    times.judge()
    fleet.layer.sync_all_agents_on_reconnect.assert_any_await("m-frozen")
    assert await workspace_fanout.drain_fan_outs(timeout=1.0) is False     # the starved machine still lags, alone
    assert machine_push_lane.owed("m-h1", AGENT, "workspace/tool.txt") == ""

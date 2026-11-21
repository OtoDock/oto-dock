"""A machine's push lane: what it is owed by name, pushed as the platform
holds it when its turn comes, with no write waiting for a machine."""

import asyncio
import hashlib
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
from core.remote import (
    file_sync,
    machine_push_lane as lane_mod,
    remote_file_cache,
    remote_file_flow,
    remote_workspace_apply,
    satellite_connection as sc,
    transfer_registry,
    upload_inflight,
)
from core.remote.remote_execution import RemoteExecutionLayer
from core.session import visibility
from services.remote import workspace_fanout
from storage.files import file_tombstones_store, recover_bin_store, sync_state_store
from tests._timing import bound
from tests.remote import link_fakes as lf

AGENT = "lane-agent"


@pytest.fixture
def world(temp_db, monkeypatch, tmp_path):
    agents = tmp_path / "agents"
    (agents / AGENT / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    cm = sc.SatelliteConnectionManager()
    monkeypatch.setattr(sc, "_connection_manager", cm)
    monkeypatch.setattr(sc, "_persist_write", lambda *a, **k: None)
    monkeypatch.setattr(cm, "_kick_reconnect_steps", lambda machine_id: None)
    from core.session import session_manager, session_state
    layer = SimpleNamespace(_sessions={}, _cm=cm, sync_agent_in_background=AsyncMock(),
                            resolve_machine_sync_identity=AsyncMock(return_value=None))
    layer.owns_session = lambda sid: sid in layer._sessions
    monkeypatch.setattr(session_manager, "_remote_layer", layer)
    lane_mod.reset()
    remote_file_flow._platform_ahead.clear()
    remote_file_flow._pull_stat_records.clear()
    remote_file_cache._merged_agents.clear()
    workspace_fanout._idle_identities.clear()
    # A path lock a read and its re-push contended binds to that test's loop.
    remote_file_flow._global_path_locks.clear()
    held: list[str] = []

    def hold(sid: str, machine_id: str) -> None:
        layer._sessions[sid] = lf.session(AGENT, machine_id)
        session_state._session_security[sid] = lf.security(AGENT, machine_id)
        held.append(sid)

    def write(rel: str, data: bytes):
        path = agents / AGENT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    yield SimpleNamespace(cm=cm, layer=layer, agents=agents, hold=hold, write=write)
    lane_mod.reset()
    remote_file_flow._global_path_locks.clear()
    remote_file_flow._platform_ahead.clear()
    remote_file_flow._pull_stat_records.clear()
    remote_file_cache._merged_agents.clear()
    for sid in held:
        session_state._session_security.pop(sid, None)


async def _until(check, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "the condition never held"
        await asyncio.sleep(0.01)


def _hash(path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _pushes(machine: lf.FakeMachine, rel: str) -> list[dict]:
    return [f for f in machine.frames if f.get("type") == "file_push" and f.get("path") == rel]


@pytest.mark.asyncio
async def test_a_fan_out_returns_before_a_frozen_target_and_the_healthy_one_has_the_file(world):
    healthy, frozen = lf.FakeMachine(world.cm, "m-h"), lf.FakeMachine(world.cm, "m-f", mode=lf.FROZEN)
    await healthy.connect()
    await frozen.connect()
    world.hold("s-h", "m-h")
    world.hold("s-f", "m-f")
    path = world.write("workspace/a.txt", b"hello")
    started = time.monotonic()
    await workspace_fanout.fan_out_write(AGENT, "workspace/a.txt", path)
    assert time.monotonic() - started < bound(0.5)
    await _until(lambda: healthy.files.get((AGENT, "workspace/a.txt")) == b"hello")
    assert lane_mod.owed("m-f", AGENT, "workspace/a.txt") == lane_mod.WRITE
    # The healthy machine's ack ends its name.
    await _until(lambda: lane_mod.owed("m-h", AGENT, "workspace/a.txt") == "")
    # A read on the frozen machine serves the platform copy meanwhile.
    assert remote_file_flow._owed_by_lane("m-f", AGENT, "workspace/a.txt") == "write"


@pytest.mark.asyncio
async def test_a_machine_back_before_its_push_learns_of_the_drop_keeps_the_name(world):
    m = lf.FakeMachine(world.cm, "m-1", mode=lf.FROZEN)
    await m.connect()
    world.hold("s-1", "m-1")
    path = world.write("workspace/a.txt", b"hello")
    await workspace_fanout.fan_out_write(AGENT, "workspace/a.txt", path)
    await asyncio.sleep(0.05)                         # the push is on its way
    await m.drop()
    m.thaw()
    await m.connect()                                 # before the worker runs again
    await _until(lambda: m.files.get((AGENT, "workspace/a.txt")) == b"hello")
    await _until(lambda: lane_mod.owed("m-1", AGENT, "workspace/a.txt") == "")


@pytest.mark.asyncio
async def test_a_name_changed_again_is_pushed_once_more_with_the_newest_bytes(world):
    m = lf.FakeMachine(world.cm, "m-1", mode=lf.FROZEN)
    await m.connect()
    world.hold("s-1", "m-1")
    path = world.write("workspace/a.txt", b"v1")
    await workspace_fanout.fan_out_write(AGENT, "workspace/a.txt", path)
    await asyncio.sleep(0.05)                         # the first push is on its way
    for version in (b"v2", b"v3", b"v4"):
        world.write("workspace/a.txt", version)
        await workspace_fanout.fan_out_write(AGENT, "workspace/a.txt", path)
    m.thaw()
    assert await workspace_fanout.drain_fan_outs()
    assert m.files[(AGENT, "workspace/a.txt")] == b"v4"
    # The push that was on its way, then one for the three changes after it.
    assert len(_pushes(m, "workspace/a.txt")) == 2


@pytest.mark.asyncio
async def test_a_delete_and_a_write_of_one_path_reach_a_machine_in_their_order(world):
    m = lf.FakeMachine(world.cm, "m-1", mode=lf.FROZEN)
    await m.connect()
    world.hold("s-1", "m-1")
    path = world.write("workspace/tmp.txt", b"temporary")
    await workspace_fanout.fan_out_write(AGENT, "workspace/tmp.txt", path)
    await asyncio.sleep(0.05)
    path.unlink()
    await workspace_fanout.fan_out_delete(AGENT, "workspace/tmp.txt")
    assert lane_mod.owed("m-1", AGENT, "workspace/tmp.txt") in (lane_mod.DELETE, lane_mod.WRITE)
    m.thaw()
    assert await workspace_fanout.drain_fan_outs()
    await _until(lambda: (AGENT, "workspace/tmp.txt") in m.deleted)
    assert (AGENT, "workspace/tmp.txt") not in m.files           # the write never lands after it
    # The other order: removed, then written again.
    await workspace_fanout.fan_out_delete(AGENT, "workspace/tmp.txt")
    path = world.write("workspace/tmp.txt", b"back")
    await workspace_fanout.fan_out_write(AGENT, "workspace/tmp.txt", path)
    assert await workspace_fanout.drain_fan_outs()
    assert m.files[(AGENT, "workspace/tmp.txt")] == b"back"


@pytest.mark.asyncio
async def test_a_name_the_machine_already_holds_by_its_merge_base_is_not_pushed(world):
    from storage.files import sync_state_store
    m = lf.FakeMachine(world.cm, "m-1")
    await m.connect()
    world.hold("s-1", "m-1")
    path = world.write("workspace/a.txt", b"same")
    sync_state_store.record_one("m-1", AGENT, "workspace/a.txt",
                                _hash(path), path.stat().st_mtime)
    await workspace_fanout.fan_out_write(AGENT, "workspace/a.txt", path)
    assert await workspace_fanout.drain_fan_outs()
    assert _pushes(m, "workspace/a.txt") == []
    assert sync_state_store.get_one("m-1", AGENT, "workspace/a.txt")[0] == _hash(path)


@pytest.mark.asyncio
async def test_an_acked_push_records_the_base_and_a_refused_one_marks_the_platform_ahead(world):
    from storage.files import sync_state_store
    good, bad = lf.FakeMachine(world.cm, "m-good"), lf.FakeMachine(world.cm, "m-bad")
    await good.connect()
    await bad.connect()
    bad._file_push = lambda msg: {"type": "ack", "command_id": msg["command_id"],
                                  "status": "error", "error": "disk full"}
    world.hold("s-g", "m-good")
    world.hold("s-b", "m-bad")
    path = world.write("workspace/a.txt", b"bytes")
    await workspace_fanout.fan_out_write(AGENT, "workspace/a.txt", path)
    assert await workspace_fanout.drain_fan_outs()
    assert sync_state_store.get_one("m-good", AGENT, "workspace/a.txt")[0] == _hash(path)
    assert ("m-good", AGENT, "workspace/a.txt") not in remote_file_flow._platform_ahead
    assert sync_state_store.get_one("m-bad", AGENT, "workspace/a.txt") is None
    marker = remote_file_flow._platform_ahead[("m-bad", AGENT, "workspace/a.txt")]
    assert marker["in_flight_until"] == 0.0


@pytest.mark.asyncio
async def test_names_owed_to_a_machine_that_dropped_are_pushed_when_it_returns(world):
    m = lf.FakeMachine(world.cm, "m-1", mode=lf.FROZEN)
    await m.connect()
    world.hold("s-1", "m-1")
    for n in range(5):
        path = world.write(f"workspace/f{n}.txt", f"file {n}".encode())
        await workspace_fanout.fan_out_write(AGENT, f"workspace/f{n}.txt", path)
    await asyncio.sleep(0.05)
    await m.drop()                                    # the monitor's cut, or its socket's end
    await asyncio.sleep(0.05)
    assert all(lane_mod.owed("m-1", AGENT, f"workspace/f{n}.txt") for n in range(5))
    m.thaw()
    await m.connect()
    assert await workspace_fanout.drain_fan_outs()
    assert {m.files[(AGENT, f"workspace/f{n}.txt")] for n in range(5)} == {
        f"file {n}".encode() for n in range(5)}


@pytest.mark.asyncio
async def test_more_names_than_the_bound_become_one_merge_of_that_agent(world, monkeypatch):
    monkeypatch.setattr(config, "SAT_PUSH_LANE_MAX_NAMES", 20)
    monkeypatch.setattr(lane_mod, "WORKERS", 1)
    merged = asyncio.Event()

    async def _merge(machine_id, agent):
        await merged.wait()
        return True

    world.layer.sync_agent_in_background = AsyncMock(side_effect=_merge)
    m = lf.FakeMachine(world.cm, "m-1", mode=lf.FROZEN)
    await m.connect()
    for n in range(30):
        lane_mod.mark("m-1", AGENT, f"workspace/f{n}.txt")
    await asyncio.sleep(0.05)
    # Every path of the agent reads as owed while the merge is.
    assert lane_mod.owed("m-1", AGENT, "workspace/anything-at-all.txt") == lane_mod.MERGE
    merged.set()
    m.thaw()
    assert await workspace_fanout.drain_fan_outs()
    world.layer.sync_agent_in_background.assert_awaited_once_with("m-1", AGENT)
    assert lane_mod.owed("m-1", AGENT, "workspace/f3.txt") == ""


@pytest.mark.asyncio
async def test_a_turn_waits_for_its_own_machines_files_and_never_for_another_machines(world):
    healthy, frozen = lf.FakeMachine(world.cm, "m-h"), lf.FakeMachine(world.cm, "m-f", mode=lf.FROZEN)
    await healthy.connect()
    await frozen.connect()
    world.hold("s-h", "m-h")
    world.hold("s-f", "m-f")
    path = world.write("workspace/a.txt", b"hello")
    # A person's save holds turns; a fan-out that started on a machine does not.
    await workspace_fanout.fan_out_write(AGENT, "workspace/a.txt", path, holds_turns=True)
    started = time.monotonic()
    assert await upload_inflight.wait_settled(AGENT, machine_id="m-h") is True
    assert time.monotonic() - started < bound(1.0)
    assert healthy.files[(AGENT, "workspace/a.txt")] == b"hello"
    # The frozen machine's own turn waits, and gives up when its lane stops moving.
    started = time.monotonic()
    assert await upload_inflight.wait_settled(AGENT, machine_id="m-f", timeout=0.3) is False
    assert time.monotonic() - started < bound(2.0)
    lane_mod.reset()
    path = world.write("workspace/b.txt", b"from a machine")
    await workspace_fanout.fan_out_write(AGENT, "workspace/b.txt", path, holds_turns=False)
    assert await upload_inflight.wait_settled(AGENT, machine_id="m-f", timeout=5) is True


@pytest.mark.asyncio
async def test_the_asking_machines_own_push_is_waited_for(world):
    m = lf.FakeMachine(world.cm, "m-1", mode=lf.STARVED, delay=0.2)
    await m.connect()
    world.hold("s-1", "m-1")
    path = world.write("workspace/a.txt", b"hello")
    started = time.monotonic()
    await workspace_fanout.fan_out_write(AGENT, "workspace/a.txt", path, only_machine_id="m-1")
    assert time.monotonic() - started >= 0.15
    assert m.files[(AGENT, "workspace/a.txt")] == b"hello"


@pytest.mark.asyncio
async def test_a_tracked_uploads_rows_end_per_machine(world, monkeypatch):
    transfer_registry._items.clear()
    transfer_registry.set_broadcaster(None)
    monkeypatch.setattr(transfer_registry, "_machine_name", lambda mid: mid)
    monkeypatch.setattr(transfer_registry, "_resolve_recipients", lambda a, p: frozenset({"u1"}))
    healthy, frozen = lf.FakeMachine(world.cm, "m-h"), lf.FakeMachine(world.cm, "m-f", mode=lf.FROZEN)
    await healthy.connect()
    await frozen.connect()
    world.hold("s-h", "m-h")
    world.hold("s-f", "m-f")
    path = world.write("workspace/up.bin", b"u" * 1000)
    await workspace_fanout.fan_out_write(AGENT, "workspace/up.bin", path, transfer_kind="upload",
                                         transfer_id="upl-1")
    item = transfer_registry.get("upl-1")
    await _until(lambda: item.machines["m-h"].state == "done")
    assert item.machines["m-f"].state != "done" and item.done_at is None
    frozen.thaw()
    assert await workspace_fanout.drain_fan_outs()
    assert item.machines["m-f"].state == "done" and item.done_at is not None
    transfer_registry._items.clear()


# -- a lane that was quiet ---------------------------------------------------


async def _aged_lane(world, quiet_s: float) -> None:
    """A lane's usual state: it pushed something and stayed quiet since."""
    first = world.write("workspace/first.txt", b"an earlier file")
    await workspace_fanout.fan_out_write(AGENT, "workspace/first.txt", first)
    assert await lane_mod.drain(5.0)
    await asyncio.sleep(quiet_s)


@pytest.mark.asyncio
async def test_a_turn_waits_for_a_file_written_onto_a_lane_that_was_quiet(world):
    m = lf.FakeMachine(world.cm, "m-1", mode=lf.STARVED, delay=0.01)
    await m.connect()
    world.hold("s-1", "m-1")
    await _aged_lane(world, 1.1)
    m.delay = 0.1                                     # the push takes longer than nothing
    photo = world.write("workspace/photo.png", b"a pasted photo")
    await workspace_fanout.fan_out_write(AGENT, "workspace/photo.png", photo, include_idle=True)
    assert await upload_inflight.wait_settled(AGENT, machine_id="m-1", timeout=1.0) is True
    await _until(lambda: m.files.get((AGENT, "workspace/photo.png")) == b"a pasted photo")


@pytest.mark.asyncio
async def test_the_asking_machines_wait_counts_from_the_write_on_a_quiet_lane(world, monkeypatch):
    monkeypatch.setattr(config, "PUSH_STALL_S", 1.0)
    m = lf.FakeMachine(world.cm, "m-1", mode=lf.STARVED, delay=0.01)
    await m.connect()
    world.hold("s-1", "m-1")
    await _aged_lane(world, 1.1)
    m.delay = 0.1
    path = world.write("workspace/attachment.png", b"an attachment")
    await workspace_fanout.fan_out_write(AGENT, "workspace/attachment.png", path,
                                         include_idle=True, only_machine_id="m-1")
    assert m.files.get((AGENT, "workspace/attachment.png")) == b"an attachment"


@pytest.mark.asyncio
async def test_marks_toward_a_frozen_machine_keep_no_waiter_alive(world):
    """The idle clock starts with the work and only on an empty lane."""
    m = lf.FakeMachine(world.cm, "m-1", mode=lf.FROZEN)
    await m.connect()
    for n in range(lane_mod.WORKERS + 1):
        world.write(f"workspace/f{n}.txt", b"x" * 400_000)
        lane_mod.mark("m-1", AGENT, f"workspace/f{n}.txt", holds_turns=True)
    await asyncio.sleep(0.05)
    waiter = asyncio.ensure_future(lane_mod.settled("m-1", AGENT, turns_only=True, idle_s=0.2))
    started = time.monotonic()
    while not waiter.done():
        lane_mod.mark("m-1", AGENT, f"workspace/f{lane_mod.WORKERS}.txt", holds_turns=True)
        await asyncio.sleep(0.02)
        assert time.monotonic() - started < bound(2.0)
    assert waiter.result() is False


@pytest.mark.asyncio
async def test_a_turn_counts_a_name_written_again_after_its_wait_began(world):
    """The wait's limit is the lane's count when it began, and a change of a
    queued name raises that name's number past it."""
    m = lf.FakeMachine(world.cm, "m-1", mode=lf.FROZEN)
    await m.connect()
    world.hold("s-1", "m-1")
    for n in range(lane_mod.WORKERS):
        lane_mod.mark("m-1", AGENT, f"workspace/held{n}.txt")
    notes = world.write("workspace/notes.md", b"first")
    for n in range(lane_mod.WORKERS):
        world.write(f"workspace/held{n}.txt", b"x" * 400_000)
    await asyncio.sleep(0.05)
    lane_mod.mark("m-1", AGENT, "workspace/notes.md", holds_turns=True)
    limit = lane_mod.seq("m-1")
    lane_mod.mark("m-1", AGENT, "workspace/notes.md", holds_turns=True)
    assert notes.is_file()
    lane = lane_mod._lanes["m-1"]
    assert lane_mod._waiting(lane, AGENT, limit - 1, limit, True) is True
    # A name first changed after the wait began is not this wait's.
    lane_mod.mark("m-1", AGENT, "workspace/later.md", holds_turns=True)
    assert lane.names[(AGENT, "workspace/later.md")].first > limit


# -- the workers ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_small_save_does_not_wait_behind_a_large_push(world):
    m = lf.FakeMachine(world.cm, "m-1", mode=lf.SLOW, rate=512 * 1024)
    await m.connect()
    world.hold("s-1", "m-1")
    path = world.write("workspace/large.bin", b"x" * (5 * 1024 * 1024))
    await workspace_fanout.fan_out_write(AGENT, "workspace/large.bin", path)
    await _until(lambda: (AGENT, "workspace/large.bin") in lane_mod._lanes["m-1"].inflight)
    small = world.write("workspace/small.txt", b"a one line edit")
    await workspace_fanout.fan_out_write(AGENT, "workspace/small.txt", small)
    await _until(lambda: (AGENT, "workspace/small.txt") in m.files, timeout=bound(5.0))
    assert (AGENT, "workspace/large.bin") not in m.files
    lane = lane_mod._lanes["m-1"]
    for task in list(lane.workers):
        task.cancel()
    await asyncio.gather(*list(lane.workers), return_exceptions=True)
    await m.drop()


@pytest.mark.asyncio
async def test_no_more_than_the_workers_run_however_many_names_wait(world):
    m = lf.FakeMachine(world.cm, "m-1", mode=lf.FROZEN)
    await m.connect()
    for n in range(lane_mod.WORKERS * 3):
        world.write(f"workspace/f{n}.txt", b"x" * 400_000)
        lane_mod.mark("m-1", AGENT, f"workspace/f{n}.txt")
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    lane = lane_mod._lanes["m-1"]
    assert sum(1 for t in lane.workers if not t.done()) == lane_mod.WORKERS
    assert len(lane.inflight) == lane_mod.WORKERS


# -- the barrier's ceiling --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_waits_past_the_idle_bound_while_the_lane_moves_and_stops_at_the_ceiling(world):
    m = lf.FakeMachine(world.cm, "m-1", mode=lf.SLOW, rate=2 * 1024 * 1024)
    await m.connect()
    world.hold("s-1", "m-1")
    path = world.write("workspace/large.bin", b"x" * (4 * 1024 * 1024))
    await workspace_fanout.fan_out_write(AGENT, "workspace/large.bin", path, include_idle=True)
    # The whole file takes more than two seconds: the ceiling stops a wait while
    # it moves, and an idle bound shorter than the file does not.
    started = time.monotonic()
    assert await upload_inflight.wait_settled(AGENT, machine_id="m-1", timeout=30.0, ceiling=0.3) is False
    assert 0.25 < time.monotonic() - started < bound(5.0)
    assert (AGENT, "workspace/large.bin") not in m.files      # it goes on behind the turn
    started = time.monotonic()
    assert await upload_inflight.wait_settled(AGENT, machine_id="m-1", timeout=1.5) is True
    assert time.monotonic() - started > 1.5
    assert m.files[(AGENT, "workspace/large.bin")] == b"x" * (4 * 1024 * 1024)


# -- a push that fails for another reason than the drop -----------------------------


@pytest.mark.asyncio
async def test_a_store_error_in_a_push_is_tried_again_and_the_file_arrives(world, monkeypatch):
    from storage.files import sync_state_store
    monkeypatch.setattr(lane_mod, "_PUSH_PAUSE_MAX_S", 0.01)
    real = sync_state_store.get_one
    calls: list = []

    def get_one(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("the pool timed out")
        return real(*args, **kwargs)

    monkeypatch.setattr(sync_state_store, "get_one", get_one)
    m = lf.FakeMachine(world.cm, "m-1")
    await m.connect()
    world.hold("s-1", "m-1")
    path = world.write("workspace/notes.md", b"the person's save")
    await workspace_fanout.fan_out_write(AGENT, "workspace/notes.md", path)
    assert await lane_mod.drain(5.0)
    assert m.files[(AGENT, "workspace/notes.md")] == b"the person's save" and len(calls) == 2


@pytest.mark.asyncio
async def test_a_push_that_keeps_raising_leaves_a_mark_so_a_read_serves_the_save(world, monkeypatch):
    from storage.files import sync_state_store
    monkeypatch.setattr(lane_mod, "_PUSH_PAUSE_MAX_S", 0.01)
    monkeypatch.setattr(sync_state_store, "get_one",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the pool timed out")))
    m = lf.FakeMachine(world.cm, "m-1")
    await m.connect()
    world.hold("s-1", "m-1")
    path = world.write("workspace/notes.md", b"the person's save")
    await transfer_registry.begin(AGENT, "workspace/notes.md", kind="sync", bytes_total=path.stat().st_size,
                                  machine_ids=["m-1"], transfer_id="t-1")
    lane_mod.mark("m-1", AGENT, "workspace/notes.md", transfer=("t-1", path.stat().st_size))
    assert await lane_mod.drain(5.0)
    assert lane_mod.owed("m-1", AGENT, "workspace/notes.md") == ""
    assert (AGENT, "workspace/notes.md") not in m.files
    mark = remote_file_flow._platform_ahead[("m-1", AGENT, "workspace/notes.md")]
    assert mark["size"] == path.stat().st_size and mark["mtime_ns"] == path.stat().st_mtime_ns
    await _until(lambda: transfer_registry.get("t-1") is None
                 or transfer_registry.get("t-1").machines["m-1"].state == "failed")


@pytest.mark.asyncio
async def test_a_change_during_a_failed_push_keeps_its_rows_and_its_turns(world, monkeypatch):
    monkeypatch.setattr(lane_mod, "_PUSH_PAUSE_MAX_S", 0.2)
    from storage.files import sync_state_store
    monkeypatch.setattr(sync_state_store, "get_one",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the pool timed out")))
    m = lf.FakeMachine(world.cm, "m-1")
    await m.connect()
    world.write("workspace/notes.md", b"first")
    first = lane_mod.mark("m-1", AGENT, "workspace/notes.md", holds_turns=True, transfer=("t-1", 5))
    await _until(lambda: (AGENT, "workspace/notes.md") in lane_mod._lanes["m-1"].inflight)
    await asyncio.sleep(0.05)
    second = lane_mod.mark("m-1", AGENT, "workspace/notes.md")
    await _until(lambda: not lane_mod._lanes["m-1"].inflight
                 or lane_mod._lanes["m-1"].inflight[(AGENT, "workspace/notes.md")].seq == second)
    lane = lane_mod._lanes["m-1"]
    name = lane.names.get((AGENT, "workspace/notes.md")) or lane.inflight[(AGENT, "workspace/notes.md")]
    assert name.first == first and name.holds_turns and ("t-1", 5) in name.transfers
    for task in list(lane.workers):
        task.cancel()
    await asyncio.gather(*list(lane.workers), return_exceptions=True)


# -- a name that waited is judged again ------------------------------------------


@pytest.mark.asyncio
async def test_a_name_that_waited_is_not_pushed_to_a_machine_that_may_no_longer_hold_it(world, monkeypatch):
    monkeypatch.setattr(lane_mod, "REJUDGE_AFTER_S", 0.0)
    m = lf.FakeMachine(world.cm, "m-1")
    await m.connect()
    world.hold("s-1", "m-1")
    await m.drop()
    path = world.write("workspace/plan.md", b"written while it was allowed")
    await workspace_fanout.fan_out_write(AGENT, "workspace/plan.md", path)
    assert lane_mod.owed("m-1", AGENT, "workspace/plan.md") == lane_mod.WRITE
    # The session ended and its person lost the agent while the machine was off.
    world.layer._sessions.pop("s-1")
    path.write_bytes(b"written after the person lost the agent")
    await m.connect()
    assert await lane_mod.drain(5.0)
    assert (AGENT, "workspace/plan.md") not in m.files
    assert lane_mod.owed("m-1", AGENT, "workspace/plan.md") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed_by", ["its pairing", "a live session"])
async def test_a_name_that_waited_reaches_a_machine_still_allowed(world, monkeypatch, allowed_by):
    from storage.files import sync_state_store
    monkeypatch.setattr(lane_mod, "REJUDGE_AFTER_S", 0.0)
    m = lf.FakeMachine(world.cm, "m-1")
    await m.connect()
    world.hold("s-1", "m-1")
    await m.drop()
    path = world.write("workspace/plan.md", b"still allowed")
    await workspace_fanout.fan_out_write(AGENT, "workspace/plan.md", path)
    if allowed_by == "its pairing":
        world.layer._sessions.pop("s-1")
        sync_state_store.record_one("m-1", AGENT, "workspace/other.md", "sha256:0", 1.0)
        world.layer.resolve_machine_sync_identity = AsyncMock(return_value=(None, "admin"))
    await m.connect()
    assert await lane_mod.drain(5.0)
    assert m.files[(AGENT, "workspace/plan.md")] == b"still allowed"


@pytest.mark.asyncio
async def test_a_name_on_a_healthy_lane_pays_for_no_second_judgement(world, monkeypatch):
    m = lf.FakeMachine(world.cm, "m-1")
    await m.connect()
    world.hold("s-1", "m-1")
    judged = AsyncMock(return_value=False)
    monkeypatch.setattr(workspace_fanout, "machine_may_hold", judged)
    path = world.write("workspace/plan.md", b"at once")
    await workspace_fanout.fan_out_write(AGENT, "workspace/plan.md", path)
    assert await lane_mod.drain(5.0)
    assert m.files[(AGENT, "workspace/plan.md")] == b"at once"
    judged.assert_not_awaited()


# -- what an overflow owes -----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_overflow_merge_that_did_not_run_is_owed_again(world, monkeypatch):
    monkeypatch.setattr(config, "SAT_PUSH_LANE_MAX_NAMES", 5)
    monkeypatch.setattr(lane_mod, "MERGE_RETRY_S", 0.05)
    answers = iter([False, True])
    world.layer.sync_agent_in_background = AsyncMock(side_effect=lambda *a: next(answers))
    m = lf.FakeMachine(world.cm, "m-1")
    await m.connect()
    await m.drop()
    for n in range(6):
        lane_mod.mark("m-1", AGENT, f"workspace/t/f{n}.txt")
    lane = lane_mod._lanes["m-1"]
    assert AGENT in lane.merge_owed and not lane.names
    await m.connect()
    await _until(lambda: world.layer.sync_agent_in_background.await_count == 2)
    assert await lane_mod.drain(5.0)
    assert AGENT not in lane.merge_owed and not lane.merging


@pytest.mark.asyncio
async def test_a_merge_that_raised_on_a_connected_machine_is_owed_again(world, monkeypatch):
    monkeypatch.setattr(config, "SAT_PUSH_LANE_MAX_NAMES", 5)
    monkeypatch.setattr(lane_mod, "MERGE_RETRY_S", 0.05)
    outcomes = iter([RuntimeError("the store did not answer"), True])

    async def merge(*_args):
        got = next(outcomes)
        if isinstance(got, Exception):
            raise got
        return got

    world.layer.sync_agent_in_background = AsyncMock(side_effect=merge)
    m = lf.FakeMachine(world.cm, "m-1")
    await m.connect()
    for n in range(6):
        lane_mod.mark("m-1", AGENT, f"workspace/t/f{n}.txt")
    await _until(lambda: world.layer.sync_agent_in_background.await_count == 2)
    assert await lane_mod.drain(5.0)


@pytest.mark.asyncio
async def test_names_past_their_byte_bound_become_one_merge_whatever_their_count(world, monkeypatch):
    monkeypatch.setattr(lane_mod, "NAMES_MAX_BYTES", 2_000)
    m = lf.FakeMachine(world.cm, "m-1")
    await m.connect()
    await m.drop()
    lane_mod.mark("m-1", "other-agent", "workspace/a.txt")
    long_name = "workspace/" + "n" * 800 + ".txt"
    lane_mod.mark("m-1", AGENT, long_name + "1")
    lane = lane_mod._lanes["m-1"]
    assert lane.names and lane.name_bytes < lane_mod.NAMES_MAX_BYTES
    lane_mod.mark("m-1", AGENT, long_name + "2")
    assert not lane.names and lane.name_bytes == 0
    assert lane.merge_owed == {AGENT, "other-agent"}
    assert lane_mod.owed("m-1", AGENT, "workspace/anything.txt") == lane_mod.MERGE


@pytest.mark.asyncio
async def test_the_name_bytes_come_back_as_names_are_pushed(world):
    m = lf.FakeMachine(world.cm, "m-1")
    await m.connect()
    world.write("workspace/a.txt", b"a")
    lane_mod.mark("m-1", AGENT, "workspace/a.txt")
    assert await lane_mod.drain(5.0)
    assert lane_mod._lanes["m-1"].name_bytes == 0


# -- a read after a restart ---------------------------------------------------------


def _forget_everything_a_restart_forgets(machine: lf.FakeMachine) -> None:
    lane_mod.reset()
    remote_file_flow._platform_ahead.clear()
    remote_file_flow._pull_stat_records.clear()
    remote_file_cache._merged_agents.clear()


async def _read_after_restart(world, monkeypatch, *, platform: bytes, base: bytes, machine: bytes):
    """A file known at ``base`` on the machine. The platform now holds
    ``platform`` and the machine ``machine``, and the process restarted with no
    merge yet. Answers the read's path and what it pulled."""
    from storage.files import sync_state_store
    m = lf.FakeMachine(world.cm, "m-1")
    await m.connect()
    world.hold("s-1", "m-1")
    await m.drop()
    path = world.write("workspace/notes.md", platform)
    sync_state_store.record_one("m-1", AGENT, "workspace/notes.md",
                                "sha256:" + hashlib.sha256(base).hexdigest(), 1.0)
    _forget_everything_a_restart_forgets(m)
    await m.connect()
    monkeypatch.setattr(remote_file_flow, "write_back_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(remote_file_flow, "_probe_stat",
                        AsyncMock(return_value={"exists": True, "size": len(machine), "mtime_ns": 1}))
    pulled: list = []

    async def pull(machine_id, ref, dest, **kw):
        pulled.append(dest)
        dest.write_bytes(machine)
        return True

    monkeypatch.setattr(world.cm, "pull_file_to_path", pull)
    served = await remote_file_flow.pull_through("s-1", "workspace/notes.md")
    return served, path, pulled


@pytest.mark.asyncio
async def test_a_read_after_a_restart_never_pulls_older_bytes_over_a_save(world, monkeypatch):
    served, path, pulled = await _read_after_restart(
        world, monkeypatch, platform=b"the person's save", base=b"older", machine=b"older")
    assert served == path.resolve() and not pulled
    assert path.read_bytes() == b"the person's save"


@pytest.mark.asyncio
async def test_a_read_after_a_restart_still_pulls_the_machines_edit_when_the_platform_changed_nothing(world, monkeypatch):
    served, path, pulled = await _read_after_restart(
        world, monkeypatch, platform=b"older", base=b"older", machine=b"the machine's edit")
    assert pulled and path.read_bytes() == b"the machine's edit"


@pytest.mark.asyncio
async def test_both_sides_changed_after_a_restart_goes_to_the_merges_conflict_rule(world, monkeypatch):
    """The read serves the platform copy and writes nothing, on either side:
    the machine's newer edit is still there for the merge, which sees both."""
    served, path, pulled = await _read_after_restart(
        world, monkeypatch, platform=b"the person's save", base=b"older", machine=b"the machine's newer edit")
    assert served == path.resolve() and not pulled
    assert path.read_bytes() == b"the person's save"


# -- what a turn waits for, once it went on without it -------------------------------


@pytest.mark.asyncio
async def test_a_turn_waits_on_a_lane_busy_with_something_that_has_not_moved(world):
    """A long merge of another agent moves no clock, and a pasted file gets a
    worker of its own: the turn still waits its whole bound for it."""
    m = lf.FakeMachine(world.cm, "m-1", mode=lf.STARVED, delay=0.1)
    await m.connect()
    lane = lane_mod._lane("m-1")
    lane.merging.add("other-agent")
    lane.moved_at = time.monotonic() - 120.0
    world.write("workspace/photo.png", b"a pasted photo")
    lane_mod.mark("m-1", AGENT, "workspace/photo.png", holds_turns=True)
    assert await upload_inflight.wait_settled(AGENT, machine_id="m-1", timeout=1.0) is True
    assert m.files[(AGENT, "workspace/photo.png")] == b"a pasted photo"
    lane.merging.discard("other-agent")


@pytest.mark.asyncio
async def test_the_next_turn_does_not_wait_for_files_a_turn_went_on_without(world):
    m = lf.FakeMachine(world.cm, "m-1", mode=lf.FROZEN)
    await m.connect()
    for n in range(lane_mod.WORKERS + 1):
        world.write(f"workspace/f{n}.txt", b"x" * 400_000)
        lane_mod.mark("m-1", AGENT, f"workspace/f{n}.txt", holds_turns=True)
    await asyncio.sleep(0.05)
    assert await upload_inflight.wait_settled(AGENT, machine_id="m-1", timeout=0.1) is False
    lane_mod.release_turns("m-1", AGENT)
    started = time.monotonic()
    assert await upload_inflight.wait_settled(AGENT, machine_id="m-1", timeout=5.0) is True
    assert time.monotonic() - started < bound(1.0)
    # The names are still owed: a read still serves the platform's copy.
    assert lane_mod.owed("m-1", AGENT, "workspace/f0.txt") == lane_mod.WRITE
    # Another agent's files and a new write of this one's wait as ever.
    lane_mod.mark("m-1", "other-agent", "workspace/b.txt", holds_turns=True)
    assert await upload_inflight.wait_settled("other-agent", machine_id="m-1", timeout=0.1) is False
    lane_mod.mark("m-1", AGENT, f"workspace/f{lane_mod.WORKERS}.txt", holds_turns=True)
    assert await upload_inflight.wait_settled(AGENT, machine_id="m-1", timeout=0.1) is False


@pytest.mark.asyncio
async def test_the_next_turn_does_not_wait_for_a_merge_a_turn_went_on_without(world, monkeypatch):
    monkeypatch.setattr(config, "SAT_PUSH_LANE_MAX_NAMES", 2)
    gate = asyncio.Event()

    async def _merge(machine_id, agent):
        await gate.wait()
        return True

    world.layer.sync_agent_in_background = AsyncMock(side_effect=_merge)
    m = lf.FakeMachine(world.cm, "m-1")
    await m.connect()
    for n in range(3):
        lane_mod.mark("m-1", AGENT, f"workspace/f{n}.txt", holds_turns=True)
    await _until(lambda: AGENT in lane_mod._lanes["m-1"].merging)
    assert await upload_inflight.wait_settled(AGENT, machine_id="m-1", timeout=0.1) is False
    lane_mod.release_turns("m-1", AGENT)
    assert await upload_inflight.wait_settled(AGENT, machine_id="m-1", timeout=5.0) is True
    assert lane_mod.owed("m-1", AGENT, "workspace/f0.txt") == lane_mod.MERGE     # still owed
    gate.set()
    assert await lane_mod.drain(5.0)
    assert not lane_mod._lanes["m-1"].merge_told


# -- whose bytes win: the real merge -----------------------------------------------------


REL = "workspace/notes.md"


def _h(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def merging(temp_db, monkeypatch, tmp_path):
    """The real merge (``_initial_workspace_sync``, ``diff_manifests``,
    ``_sync_apply``) over a machine faked at the frame level."""
    agents = tmp_path / "agents"
    (agents / AGENT / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    cm = sc.SatelliteConnectionManager()
    monkeypatch.setattr(sc, "_connection_manager", cm)
    monkeypatch.setattr(sc, "_persist_write", lambda *a, **k: None)
    monkeypatch.setattr(cm, "_kick_reconnect_steps", lambda machine_id: None)
    monkeypatch.setattr(visibility, "is_shared_only", lambda slug: False)
    from core.session import session_manager, session_state
    layer = RemoteExecutionLayer(cm)
    layer.resolve_machine_sync_identity = AsyncMock(return_value=(None, "admin"))
    monkeypatch.setattr(session_manager, "_remote_layer", layer)
    lane_mod.reset()
    remote_file_flow._platform_ahead.clear()
    remote_file_flow._pull_stat_records.clear()
    remote_file_cache._merged_agents.clear()
    workspace_fanout._idle_identities.clear()
    # A path lock a read and its re-push contended binds to that test's loop.
    remote_file_flow._global_path_locks.clear()
    held: list[str] = []

    def hold(sid: str, machine_id: str) -> None:
        layer._sessions[sid] = lf.session(AGENT, machine_id, session_id=sid)
        session_state._session_security[sid] = lf.security(AGENT, machine_id)
        held.append(sid)

    def write(rel: str, data: bytes):
        path = agents / AGENT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    monkeypatch.setattr(remote_file_flow, "write_back_allowed", AsyncMock(return_value=True))
    yield SimpleNamespace(cm=cm, layer=layer, agents=agents, hold=hold, write=write)
    lane_mod.reset()
    remote_file_flow._global_path_locks.clear()
    remote_file_flow._platform_ahead.clear()
    remote_file_flow._pull_stat_records.clear()
    remote_file_cache._merged_agents.clear()
    for sid in held:
        session_state._session_security.pop(sid, None)


def _manifest(merging, monkeypatch, files: dict[str, tuple[bytes, float]] | None) -> None:
    """The machine's manifest answer, its real hashes and mtimes. None: it does not come."""
    real = merging.cm.send_command

    async def answer(machine_id, msg, timeout=30.0, **kw):
        if msg.get("type") == "request_manifest":
            if files is None:
                raise RuntimeError("no manifest came")
            return {"files": [{"path": p, "size": len(d), "hash": _h(d), "mtime": m}
                              for p, (d, m) in files.items()]}
        return await real(machine_id, msg, timeout=timeout, **kw)

    monkeypatch.setattr(merging.cm, "send_command", answer)


def _machine_holds(merging, monkeypatch, data: bytes, *, mtime_ns: int = 1) -> list:
    """The machine's copy as a read's probe and pull see it; answers the pulls."""
    pulled: list = []
    monkeypatch.setattr(merging.cm, "stat_file", AsyncMock(
        return_value={"exists": True, "size": len(data), "mtime_ns": mtime_ns}))

    async def pull(machine_id, ref, dest, **kw):
        pulled.append(str(dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True

    monkeypatch.setattr(merging.cm, "pull_file_to_path", pull)
    return pulled


async def _merge_now(merging) -> bool:
    return await merging.layer._initial_workspace_sync(
        "m-1", AGENT, target_username=None, target_role="admin", background=True)


@pytest.mark.asyncio
async def test_a_merge_that_finds_the_trees_in_sync_counts_and_closes_the_window(merging, monkeypatch):
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    data = b"both sides hold this"
    merging.write(REL, data)
    sync_state_store.record_one("m-1", AGENT, REL, _h(data), 1.0)
    _manifest(merging, monkeypatch, {REL: (data, 1.0)})
    assert await _merge_now(merging) is True
    assert AGENT in remote_file_cache._merged_agents["m-1"]


@pytest.mark.asyncio
async def test_a_merge_with_no_manifest_did_not_run_and_the_window_stays_open(merging, monkeypatch):
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    merging.write(REL, b"a save")
    _manifest(merging, monkeypatch, None)
    assert await _merge_now(merging) is False
    assert AGENT not in remote_file_cache._merged_agents["m-1"]


@pytest.mark.asyncio
async def test_after_an_in_sync_merge_a_read_takes_each_edit_of_the_machines_with_no_record(merging, monkeypatch):
    """A record goes (past its bound, or a probe that did not answer at the
    pull). With the window closed the read pulls as ever, not the first pull's
    bytes for good."""
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    merging.hold("s-1", "m-1")
    data = b"both sides hold this"
    path = merging.write(REL, data)
    sync_state_store.record_one("m-1", AGENT, REL, _h(data), 1.0)
    _manifest(merging, monkeypatch, {REL: (data, 1.0)})
    assert await _merge_now(merging) is True
    _machine_holds(merging, monkeypatch, b"first edit on the machine", mtime_ns=2)
    await remote_file_flow.pull_through("s-1", REL)
    assert path.read_bytes() == b"first edit on the machine"
    remote_file_flow._pull_stat_records.clear()
    pulled = _machine_holds(merging, monkeypatch, b"second edit on the machine", mtime_ns=3)
    await remote_file_flow.pull_through("s-1", REL)
    assert pulled and path.read_bytes() == b"second edit on the machine"


@pytest.mark.asyncio
async def test_an_overflow_merge_in_sync_is_done_and_no_turn_waits(merging, monkeypatch):
    monkeypatch.setattr(lane_mod, "MERGE_RETRY_S", 0.02)
    monkeypatch.setattr(config, "SAT_PUSH_LANE_MAX_NAMES", 2)
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    merging.hold("s-1", "m-1")
    data = b"both sides hold this"
    path = merging.write(REL, data)
    sync_state_store.record_one("m-1", AGENT, REL, _h(data), 1.0)
    _manifest(merging, monkeypatch, {REL: (data, 1.0)})
    calls = []
    real_sync = merging.layer.sync_agent_in_background

    async def counted(machine_id, agent):
        calls.append(agent)
        return await real_sync(machine_id, agent)

    merging.layer.sync_agent_in_background = counted
    for i in range(3):
        lane_mod.mark("m-1", AGENT, f"workspace/gone-{i}.md", holds_turns=True)
    assert await lane_mod.drain(5.0)
    await asyncio.sleep(0.1)                    # past several retries, had it been owed again
    assert calls == [AGENT]
    lane = lane_mod._lanes["m-1"]
    assert AGENT not in lane.merge_owed and AGENT not in lane.merging
    started = time.monotonic()
    assert await upload_inflight.wait_settled(AGENT, machine_id="m-1", timeout=5.0) is True
    assert time.monotonic() - started < bound(1.0)
    # And a read takes the machine's edit: the lane no longer answers ``merge``.
    pulled = _machine_holds(merging, monkeypatch, b"the machine's edit", mtime_ns=2)
    await remote_file_flow.pull_through("s-1", REL)
    assert pulled and path.read_bytes() == b"the machine's edit"


@pytest.mark.asyncio
async def test_an_overflow_merge_with_no_manifest_is_owed_again_and_its_retries_are_no_movement(merging, monkeypatch):
    """Its retries wake the lane, faster than the idle bound. Were they movement,
    every turn would wait its whole ceiling."""
    monkeypatch.setattr(lane_mod, "MERGE_RETRY_S", 0.05)
    monkeypatch.setattr(config, "SAT_PUSH_LANE_MAX_NAMES", 2)
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    data = b"both sides hold this"
    merging.write(REL, data)
    sync_state_store.record_one("m-1", AGENT, REL, _h(data), 1.0)
    _manifest(merging, monkeypatch, None)
    for i in range(3):
        lane_mod.mark("m-1", AGENT, f"workspace/gone-{i}.md", holds_turns=True)
    started = time.monotonic()
    assert await upload_inflight.wait_settled(AGENT, machine_id="m-1", timeout=0.3, ceiling=30.0) is False
    assert time.monotonic() - started < bound(5.0)
    lane = lane_mod._lanes["m-1"]
    assert AGENT in lane.merge_owed or AGENT in lane.merging
    # The manifest comes: the merge runs, nothing is owed.
    _manifest(merging, monkeypatch, {REL: (data, 1.0)})
    await _until(lambda: AGENT not in lane.merge_owed and AGENT not in lane.merging)


async def _restart_with_a_save_the_machine_lacks(merging, monkeypatch, push):
    older, save = b"the older bytes", b"the person's save"
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    merging.hold("s-1", "m-1")
    path = merging.write(REL, save)
    sync_state_store.record_one("m-1", AGENT, REL, _h(older), 1.0)
    _manifest(merging, monkeypatch, {REL: (older, 1.0)})
    monkeypatch.setattr(merging.cm, "push_file", push)
    pulled = _machine_holds(merging, monkeypatch, older)
    return m, path, pulled, save, older


@pytest.mark.asyncio
@pytest.mark.parametrize("push", [AsyncMock(return_value=False),
                                  AsyncMock(side_effect=RuntimeError("the link went"))],
                         ids=["failed", "raised"])
async def test_a_merges_failed_push_leaves_the_mark_and_the_save_is_not_pulled_over(merging, monkeypatch, push):
    _m, path, pulled, save, older = await _restart_with_a_save_the_machine_lacks(merging, monkeypatch, push)
    # In the window the read serves the save.
    assert await remote_file_flow.pull_through("s-1", REL) == path.resolve() and not pulled
    # The merge finished and closed the window, and its push left a mark.
    assert await _merge_now(merging) is True
    assert AGENT in remote_file_cache._merged_agents["m-1"]
    assert ("m-1", AGENT, REL) in remote_file_flow._platform_ahead
    # So the read still serves the save, however often it asks.
    for _ in range(3):
        assert await remote_file_flow.pull_through("s-1", REL) == path.resolve()
    assert not pulled and path.read_bytes() == save and save != older
    await asyncio.gather(*list(remote_file_flow._repush_tasks), return_exceptions=True)
    # And the next merge that can push brings it to the machine.
    good = AsyncMock(return_value=True)
    monkeypatch.setattr(merging.cm, "push_file", good)
    assert await _merge_now(merging) is True
    good.assert_awaited()
    assert ("m-1", AGENT, REL) not in remote_file_flow._platform_ahead and path.read_bytes() == save


@pytest.mark.asyncio
async def test_a_machine_that_changed_after_a_merges_failed_push_still_wins(merging, monkeypatch):
    _m, path, _pulled, _save, _older = await _restart_with_a_save_the_machine_lacks(
        merging, monkeypatch, AsyncMock(return_value=False))
    assert await _merge_now(merging) is True
    pulled = _machine_holds(merging, monkeypatch, b"a newer edit on the machine", mtime_ns=99)
    await remote_file_flow.pull_through("s-1", REL)
    assert pulled and path.read_bytes() == b"a newer edit on the machine"


@pytest.mark.parametrize(("platform", "base", "machine", "pulls"), [
    # Edited on the machine while the proxy was down: the machine's bytes win.
    (b"same", b"same", b"edit on the machine", True),
    # No record, hash differs, base older than both: served, nothing written.
    (b"platform", b"older", b"machine", False),
    # An empty save is a save.
    (b"", b"older", b"older", False),
    # An empty machine copy with the platform unchanged is the machine's edit.
    (b"same", b"same", b"", True),
    # The machine created it; the platform holds an old version and no base.
    (b"old version", None, b"created on the machine", False),
])
@pytest.mark.asyncio
async def test_what_a_read_does_in_the_window(merging, monkeypatch, platform, base, machine, pulls):
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    merging.hold("s-1", "m-1")
    path = merging.write(REL, platform)
    if base is not None:
        sync_state_store.record_one("m-1", AGENT, REL, _h(base), 1.0)
    pulled = _machine_holds(merging, monkeypatch, machine)
    assert await remote_file_flow.pull_through("s-1", REL) == path.resolve()
    assert bool(pulled) is pulls
    assert path.read_bytes() == (machine if pulls else platform)


@pytest.mark.asyncio
async def test_a_both_changed_read_leaves_the_machines_newer_edit_for_the_merge_and_the_save_in_the_bin(merging, monkeypatch):
    save, edit = b"the person's save", b"the machine's newer edit"
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    m.conn.clock_offset = 0.0                   # no offset measured yet makes the platform win
    merging.hold("s-1", "m-1")
    path = merging.write(REL, save)
    os.utime(path, (1000.0, 1000.0))
    sync_state_store.record_one("m-1", AGENT, REL, _h(b"older"), 1.0)
    pulled = _machine_holds(merging, monkeypatch, edit)
    assert await remote_file_flow.pull_through("s-1", REL) == path.resolve() and not pulled
    _manifest(merging, monkeypatch, {REL: (edit, time.time())})
    captured: list = []
    real = recover_bin_store.capture

    def capture(agent, rel, data, reason):
        captured.append(data)
        return real(agent, rel, data, reason)

    monkeypatch.setattr(recover_bin_store, "capture", capture)
    assert await _merge_now(merging) is True
    assert path.read_bytes() == edit and captured == [save]


@pytest.mark.asyncio
async def test_a_read_in_the_window_does_not_undo_a_delete_the_restart_forgot(merging, monkeypatch):
    """The platform deleted the file and the restart forgot the queued delete,
    with the tombstone standing. A pull would bring it back and the next merge,
    seeing two matching copies, would retire the tombstone."""
    data = b"deleted on the platform"
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    merging.hold("s-1", "m-1")
    sync_state_store.record_one("m-1", AGENT, REL, _h(data), 1.0)
    file_tombstones_store.record(AGENT, REL, 2.0)
    pulled = _machine_holds(merging, monkeypatch, data)
    path = merging.agents / AGENT / REL
    assert await remote_file_flow.pull_through("s-1", REL) is None
    assert not pulled and not path.exists()
    # The merge deletes it on the machine, and the file stays gone.
    _manifest(merging, monkeypatch, {REL: (data, 1.0)})
    assert await _merge_now(merging) is True
    assert not path.exists() and file_tombstones_store.get(AGENT, REL) is not None
    assert (AGENT, REL) in m.deleted


@pytest.mark.asyncio
async def test_a_file_the_platform_never_held_is_still_pulled_in_the_window(merging, monkeypatch):
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    merging.hold("s-1", "m-1")
    pulled = _machine_holds(merging, monkeypatch, b"made on the machine")
    assert await remote_file_flow.pull_through("s-1", REL) == (merging.agents / AGENT / REL).resolve()
    assert pulled


@pytest.mark.asyncio
async def test_repeated_reads_in_the_window_hash_an_unchanged_copy_once(merging, monkeypatch):
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    merging.hold("s-1", "m-1")
    path = merging.write(REL, b"the person's save")
    os.utime(path, (1000.0, 1000.0))             # past the cache's racy window
    file_sync.forget_hash(path)
    sync_state_store.record_one("m-1", AGENT, REL, _h(b"older"), 1.0)
    _machine_holds(merging, monkeypatch, b"older")
    real = file_sync._hash_fd
    hashed: list = []
    monkeypatch.setattr(file_sync, "_hash_fd", lambda fd: hashed.append(fd) or real(fd))
    for _ in range(3):
        await remote_file_flow.pull_through("s-1", REL)
    assert len(hashed) == 1


# -- a delete stands, whichever read comes ---------------------------------------------


DELETED = b"deleted on the platform"


async def _deleted_here(merging):
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    merging.hold("s-1", "m-1")
    sync_state_store.record_one("m-1", AGENT, REL, _h(DELETED), 1.0)
    file_tombstones_store.record(AGENT, REL, time.time())
    remote_file_flow.note_merged("m-1", AGENT)
    return m, merging.agents / AGENT / REL


@pytest.mark.asyncio
async def test_a_delete_the_machine_kept_is_not_brought_back_and_the_next_merge_finishes_it(merging, monkeypatch):
    m, path = await _deleted_here(merging)
    lane_mod.mark("m-1", AGENT, REL, deleted=True)
    await _until(lambda: lane_mod.owed("m-1", AGENT, REL) == "")
    pulled = _machine_holds(merging, monkeypatch, DELETED)
    assert await remote_file_flow.pull_through("s-1", REL) is None
    assert not pulled and not path.exists()
    _manifest(merging, monkeypatch, {REL: (DELETED, 1.0)})
    assert await _merge_now(merging) is True
    assert not path.exists() and file_tombstones_store.get(AGENT, REL) is not None
    assert m.deleted.count((AGENT, REL)) == 2


@pytest.mark.asyncio
async def test_a_merge_whose_delete_could_not_capture_leaves_the_delete_standing(merging, monkeypatch):
    m, path = await _deleted_here(merging)
    _manifest(merging, monkeypatch, {REL: (DELETED, 1.0)})
    monkeypatch.setattr(merging.cm, "pull_file_to_path", AsyncMock(return_value=False))
    assert await _merge_now(merging) is True
    assert (AGENT, REL) not in m.deleted
    pulled = _machine_holds(merging, monkeypatch, DELETED)
    assert await remote_file_flow.pull_through("s-1", REL) is None
    assert not pulled and not path.exists() and file_tombstones_store.get(AGENT, REL) is not None


@pytest.mark.asyncio
async def test_a_read_while_an_overflow_merge_is_owed_does_not_bring_a_delete_back(merging, monkeypatch):
    _m, path = await _deleted_here(merging)
    monkeypatch.setattr(config, "SAT_PUSH_LANE_MAX_NAMES", 2)
    gate = asyncio.Event()

    async def waits(machine_id, agent):
        await gate.wait()
        return True

    merging.layer.sync_agent_in_background = waits
    lane_mod.mark("m-1", AGENT, REL, deleted=True)
    for i in range(3):
        lane_mod.mark("m-1", AGENT, f"workspace/other-{i}.md", deleted=True)
    assert lane_mod.owed("m-1", AGENT, REL) == lane_mod.MERGE
    pulled = _machine_holds(merging, monkeypatch, DELETED)
    assert await remote_file_flow.pull_through("s-1", REL) is None
    assert not pulled and not path.exists()
    gate.set()


@pytest.mark.parametrize(("offset", "made_after_s", "pulls"), [
    (0.0, 1000.0, True),      # made again after the delete: the merge's rule adopts it
    (0.0, -1000.0, False),    # older than the delete
    (0.0, 1.0, False),        # inside the jitter margin the delete wins, as in the merge
    (None, 1000.0, False),    # no offset measured, so nothing is provable
])
@pytest.mark.asyncio
async def test_a_read_judges_a_copy_past_a_delete_by_the_merges_rule(merging, monkeypatch, offset, made_after_s, pulls):
    m, path = await _deleted_here(merging)
    m.conn.clock_offset = offset
    deleted_at = time.time()
    file_tombstones_store.record(AGENT, REL, deleted_at)
    pulled = _machine_holds(merging, monkeypatch, b"the machine's copy",
                            mtime_ns=int((deleted_at + made_after_s) * 1e9))
    served = await remote_file_flow.pull_through("s-1", REL)
    assert bool(pulled) is pulls and (served is not None) is pulls and path.exists() is pulls


@pytest.mark.asyncio
async def test_a_hold_is_not_a_delete_so_a_read_still_takes_the_machines_copy(merging, monkeypatch):
    _m, path = await _deleted_here(merging)
    file_tombstones_store.record(AGENT, REL, time.time(), origin=file_tombstones_store.HOLD_ORIGIN)
    pulled = _machine_holds(merging, monkeypatch, b"the only copy left")
    assert await remote_file_flow.pull_through("s-1", REL) is not None
    assert pulled and path.read_bytes() == b"the only copy left"


@pytest.mark.asyncio
async def test_a_delete_with_no_copy_on_the_machine_finds_no_file_and_pulls_nothing(merging, monkeypatch):
    _m, path = await _deleted_here(merging)
    pulled = _machine_holds(merging, monkeypatch, DELETED)
    monkeypatch.setattr(merging.cm, "stat_file", AsyncMock(return_value={"exists": False}))
    assert await remote_file_flow.pull_through("s-1", REL) is None
    assert not pulled and not path.exists()


# -- a failed push's baseline with the link down ---------------------------------------


async def _a_merges_push_failed_with_the_link_down(merging, monkeypatch, listed=True):
    """A save the machine lacks, its push failed, and the probe got no answer."""
    older, save = b"the older bytes", b"the person's save"
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    merging.hold("s-1", "m-1")
    path = merging.write(REL, save)
    if listed:
        sync_state_store.record_one("m-1", AGENT, REL, _h(older), 1.0)
    ns = 1_762_000_000_123_456_789
    _manifest(merging, monkeypatch, {REL: (older, ns / 1e9)} if listed else {"workspace/other.md": (b"x", 1.0)})
    monkeypatch.setattr(merging.cm, "push_file", AsyncMock(return_value=False))
    monkeypatch.setattr(merging.cm, "stat_file", AsyncMock(return_value=None))
    if not listed:
        merging.write("workspace/other.md", b"x")
    assert await _merge_now(merging) is True
    pushes: list = []

    async def push(machine_id, ref, src, **kw):
        pushes.append(kw.get("expect"))
        return True

    monkeypatch.setattr(merging.cm, "push_file", push)
    return path, save, older, ns, pushes


@pytest.mark.asyncio
async def test_a_machines_edit_after_a_merges_failed_push_with_the_link_down_is_not_overwritten(merging, monkeypatch):
    path, save, _older, ns, pushes = await _a_merges_push_failed_with_the_link_down(merging, monkeypatch)
    assert remote_file_flow._platform_ahead[("m-1", AGENT, REL)]["machine"]["size"] == len(b"the older bytes")
    pulled = _machine_holds(merging, monkeypatch, b"edited on the machine", mtime_ns=ns + 60_000_000_000)
    captured: list = []
    monkeypatch.setattr(recover_bin_store, "capture",
                        lambda agent, rel, data, reason: captured.append((data, reason)) or {"entry_id": "e"})
    await remote_file_flow.pull_through("s-1", REL)
    await asyncio.gather(*list(remote_file_flow._repush_tasks), return_exceptions=True)
    assert pulled and not pushes and path.read_bytes() == b"edited on the machine"
    assert captured == [(save, "conflict")]


@pytest.mark.asyncio
async def test_a_baseline_read_off_the_manifest_is_the_same_copy_despite_its_rounding(merging, monkeypatch):
    """The manifest's mtime travels as seconds. Read as a change, the machine's
    older bytes would be pulled over the save."""
    path, save, older, ns, pushes = await _a_merges_push_failed_with_the_link_down(merging, monkeypatch)
    mark = remote_file_flow._platform_ahead[("m-1", AGENT, REL)]
    assert mark["machine"]["mtime_ns"] != ns and abs(mark["machine"]["mtime_ns"] - ns) <= 1_000
    pulled = _machine_holds(merging, monkeypatch, older, mtime_ns=ns)
    assert await remote_file_flow.pull_through("s-1", REL) == path.resolve()
    await asyncio.gather(*list(remote_file_flow._repush_tasks), return_exceptions=True)
    assert not pulled and path.read_bytes() == save
    assert pushes == [{"size": len(older), "mtime_ns": mark["machine"]["mtime_ns"]}]


@pytest.mark.asyncio
async def test_a_file_the_machine_made_after_a_failed_push_of_a_new_one_is_not_overwritten(merging, monkeypatch):
    path, _save, _older, _ns, pushes = await _a_merges_push_failed_with_the_link_down(merging, monkeypatch, listed=False)
    assert remote_file_flow._platform_ahead[("m-1", AGENT, REL)]["machine"] == {"exists": False}
    pulled = _machine_holds(merging, monkeypatch, b"made on the machine")
    await remote_file_flow.pull_through("s-1", REL)
    await asyncio.gather(*list(remote_file_flow._repush_tasks), return_exceptions=True)
    assert pulled and not pushes and path.read_bytes() == b"made on the machine"


@pytest.mark.asyncio
async def test_a_probe_that_answers_is_the_baseline_over_the_manifest(merging, monkeypatch):
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    older = b"the older bytes"
    merging.write(REL, b"the person's save")
    sync_state_store.record_one("m-1", AGENT, REL, _h(older), 1.0)
    _manifest(merging, monkeypatch, {REL: (older, 1.0)})
    monkeypatch.setattr(merging.cm, "push_file", AsyncMock(return_value=False))
    probed = {"exists": True, "size": 99, "mtime_ns": 42}
    monkeypatch.setattr(merging.cm, "stat_file", AsyncMock(return_value=probed))
    assert await _merge_now(merging) is True
    assert remote_file_flow._platform_ahead[("m-1", AGENT, REL)]["machine"] == probed


# -- a lane's push cut by a drop ----------------------------------------------------------


def _counting_pushes(merging, monkeypatch, m, *, drop_first):
    expects: list = []

    async def push(machine_id, ref, src, **kw):
        expects.append(kw.get("expect"))
        if drop_first and len(expects) == 1:
            await m.drop()
            return False
        return True

    monkeypatch.setattr(merging.cm, "push_file", push)
    return expects


@pytest.mark.asyncio
async def test_a_lane_push_cut_by_a_drop_retries_expecting_what_the_machine_held(merging, monkeypatch):
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    remote_file_flow.note_merged("m-1", AGENT)
    merging.write(REL, b"a save")
    remote_file_flow.record_pushed_stat("m-1", AGENT, REL, 5, 123)
    expects = _counting_pushes(merging, monkeypatch, m, drop_first=True)
    lane_mod.mark("m-1", AGENT, REL)
    await _until(lambda: len(expects) == 1 and not merging.cm.is_connected("m-1"))
    await asyncio.sleep(0.05)
    await m.connect()
    await _until(lambda: len(expects) == 2)
    assert expects == [{"size": 5, "mtime_ns": 123}, {"size": 5, "mtime_ns": 123}]


@pytest.mark.asyncio
async def test_a_lane_push_takes_the_baseline_a_merges_failed_push_left(merging, monkeypatch):
    _path, _save, older, _ns, _pushes = await _a_merges_push_failed_with_the_link_down(merging, monkeypatch)
    baseline = remote_file_flow._platform_ahead[("m-1", AGENT, REL)]["machine"]
    expects = _counting_pushes(merging, monkeypatch, None, drop_first=False)
    lane_mod.mark("m-1", AGENT, REL)
    await _until(lambda: expects)
    assert expects == [{"size": len(older), "mtime_ns": baseline["mtime_ns"]}]


# -- what a merge left to the next one --------------------------------------------------


def _manifest_skips(merging, monkeypatch, *entries):
    real = merging.cm.send_command

    async def answer(machine_id, msg, timeout=30.0, **kw):
        if msg.get("type") == "request_manifest":
            return {"files": list(entries)}
        return await real(machine_id, msg, timeout=timeout, **kw)

    monkeypatch.setattr(merging.cm, "send_command", answer)


@pytest.mark.parametrize("skipped", [
    {"path": REL, "skipped": "unreadable", "size": 15, "mtime": 1.0},
    {"path": "workspace", "skipped": "unreadable_dir", "dir": True},
    {"path": "", "skipped": "unreadable_dir", "dir": True},
], ids=["the file", "its folder", "the whole tree"])
@pytest.mark.asyncio
async def test_a_save_at_a_path_the_machine_could_not_read_waits_for_the_merge_that_can(merging, monkeypatch, skipped):
    older, save = b"the older bytes", b"the person's save"
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    merging.hold("s-1", "m-1")
    path = merging.write(REL, save)
    sync_state_store.record_one("m-1", AGENT, REL, _h(older), 1.0)
    _manifest_skips(merging, monkeypatch, skipped)
    push = AsyncMock(return_value=True)
    monkeypatch.setattr(merging.cm, "push_file", push)
    assert await _merge_now(merging) is True
    assert AGENT in remote_file_cache._merged_agents["m-1"]
    # The lock is gone: the read still takes nothing, and pushes over nothing nobody compared.
    pulled = _machine_holds(merging, monkeypatch, older)
    for _ in range(2):
        assert await remote_file_flow.pull_through("s-1", REL) == path.resolve()
    await asyncio.gather(*list(remote_file_flow._repush_tasks), return_exceptions=True)
    assert not pulled and path.read_bytes() == save
    push.assert_not_awaited()
    # The merge that can read it carries the save, and the path is an ordinary one again.
    _manifest(merging, monkeypatch, {REL: (older, 1.0)})
    assert await _merge_now(merging) is True
    push.assert_awaited_once()
    assert not remote_file_cache._merge_left


@pytest.mark.asyncio
async def test_a_path_left_unread_with_both_sides_changed_keeps_both_for_the_merge(merging, monkeypatch):
    save, edit = b"the person's save", b"the machine's newer edit"
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    m.conn.clock_offset = 0.0
    merging.hold("s-1", "m-1")
    path = merging.write(REL, save)
    os.utime(path, (1000.0, 1000.0))
    sync_state_store.record_one("m-1", AGENT, REL, _h(b"older"), 1.0)
    _manifest_skips(merging, monkeypatch, {"path": REL, "skipped": "unreadable", "size": 1, "mtime": 1.0})
    assert await _merge_now(merging) is True
    pulled = _machine_holds(merging, monkeypatch, edit)
    await remote_file_flow.pull_through("s-1", REL)
    assert not pulled and path.read_bytes() == save
    captured: list = []
    real = recover_bin_store.capture
    monkeypatch.setattr(recover_bin_store, "capture",
                        lambda agent, rel, data, reason: captured.append(data) or real(agent, rel, data, reason))
    _manifest(merging, monkeypatch, {REL: (edit, time.time())})
    assert await _merge_now(merging) is True
    assert path.read_bytes() == edit and captured == [save]


@pytest.mark.asyncio
async def test_a_copy_at_an_unread_path_that_matches_the_base_is_not_held_back(merging, monkeypatch):
    """Nothing changed on the platform, so a read loses nothing by taking the machine's."""
    same = b"both sides held this"
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    merging.hold("s-1", "m-1")
    path = merging.write(REL, same)
    sync_state_store.record_one("m-1", AGENT, REL, _h(same), 1.0)
    _manifest_skips(merging, monkeypatch, {"path": REL, "skipped": "unreadable", "size": 1, "mtime": 1.0})
    assert await _merge_now(merging) is True
    assert not remote_file_cache._merge_left
    pulled = _machine_holds(merging, monkeypatch, b"the machine's edit")
    await remote_file_flow.pull_through("s-1", REL)
    assert pulled and path.read_bytes() == b"the machine's edit"


@pytest.mark.asyncio
async def test_a_path_with_no_base_at_an_unread_path_is_left_too(merging, monkeypatch):
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    merging.hold("s-1", "m-1")
    path = merging.write(REL, b"the person's save")
    _manifest_skips(merging, monkeypatch, {"path": REL, "skipped": "unreadable", "size": 1, "mtime": 1.0})
    assert await _merge_now(merging) is True
    pulled = _machine_holds(merging, monkeypatch, b"the machine's copy")
    await remote_file_flow.pull_through("s-1", REL)
    assert not pulled and path.read_bytes() == b"the person's save"


# -- a save after the manifest ------------------------------------------------------------


def _save_before_apply(merging, path, data):
    real = merging.layer._sync_apply

    async def save_first(run):
        path.write_bytes(data)
        return await real(run)

    merging.layer._sync_apply = save_first
    return real


@pytest.mark.asyncio
async def test_a_save_after_the_manifest_is_not_pulled_over_and_the_next_merge_keeps_both(merging, monkeypatch):
    base, edit, save = b"base", b"the machine's edit", b"the person's save, later"
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    m.conn.clock_offset = 0.0
    merging.hold("s-1", "m-1")
    path = merging.write(REL, base)
    sync_state_store.record_one("m-1", AGENT, REL, _h(base), 1.0)
    _manifest(merging, monkeypatch, {REL: (edit, 2.0)})
    pulled = _machine_holds(merging, monkeypatch, edit)
    real = _save_before_apply(merging, path, save)
    captured: list = []
    real_capture = recover_bin_store.capture
    monkeypatch.setattr(recover_bin_store, "capture",
                        lambda agent, rel, data, reason: captured.append(data) or real_capture(agent, rel, data, reason))
    assert await _merge_now(merging) is True
    assert not pulled and path.read_bytes() == save and not captured
    assert sync_state_store.get_one("m-1", AGENT, REL)[0] == _h(base)
    # Nor does a read before the next merge take the machine's over it.
    await remote_file_flow.pull_through("s-1", REL)
    assert not pulled and path.read_bytes() == save
    # The next merge sees both changed: the platform is newer here, the machine's edit is kept.
    merging.layer._sync_apply = real
    assert await _merge_now(merging) is True
    assert path.read_bytes() == save and captured == [edit]


@pytest.mark.asyncio
async def test_a_save_of_the_same_size_after_the_manifest_is_not_pulled_over(merging, monkeypatch):
    base, edit, save = b"base", b"the machine's edit", b"save"
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    path = merging.write(REL, base)
    sync_state_store.record_one("m-1", AGENT, REL, _h(base), 1.0)
    _manifest(merging, monkeypatch, {REL: (edit, 2.0)})
    pulled = _machine_holds(merging, monkeypatch, edit)
    real = merging.layer._sync_apply

    async def save_first(run):
        path.write_bytes(save)
        os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 1.0))
        return await real(run)

    merging.layer._sync_apply = save_first
    assert await _merge_now(merging) is True
    assert not pulled and path.read_bytes() == save


@pytest.mark.asyncio
async def test_a_file_that_appeared_after_the_manifest_is_not_pulled_over(merging, monkeypatch):
    edit, save = b"made on the machine", b"made here in the same moment"
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    path = merging.agents / AGENT / REL
    _manifest(merging, monkeypatch, {REL: (edit, 2.0)})
    pulled = _machine_holds(merging, monkeypatch, edit)
    _save_before_apply(merging, path, save)
    assert await _merge_now(merging) is True
    assert not pulled and path.read_bytes() == save


@pytest.mark.asyncio
async def test_a_copy_that_went_after_the_manifest_is_not_pulled_back(merging, monkeypatch):
    base, edit = b"base", b"the machine's edit"
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    path = merging.write(REL, base)
    sync_state_store.record_one("m-1", AGENT, REL, _h(base), 1.0)
    _manifest(merging, monkeypatch, {REL: (edit, 2.0)})
    pulled = _machine_holds(merging, monkeypatch, edit)
    real = merging.layer._sync_apply

    async def gone_first(run):
        path.unlink()
        return await real(run)

    merging.layer._sync_apply = gone_first
    assert await _merge_now(merging) is True
    assert not pulled and not path.exists()


@pytest.mark.asyncio
async def test_a_pull_still_lands_when_the_platform_copy_did_not_move(merging, monkeypatch):
    """The compare reads what the manifest read, on a real file: an equal copy is equal."""
    base, edit = b"base", b"the machine's edit"
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    path = merging.write(REL, base)
    sync_state_store.record_one("m-1", AGENT, REL, _h(base), 1.0)
    _manifest(merging, monkeypatch, {REL: (edit, 2.0)})
    pulled = _machine_holds(merging, monkeypatch, edit)
    assert await _merge_now(merging) is True
    assert pulled and path.read_bytes() == edit
    assert sync_state_store.get_one("m-1", AGENT, REL)[0] == _h(edit)
    assert not remote_file_cache._merge_left


async def _deferred_ran(merging):
    await asyncio.gather(*list(merging.layer._deferred_sync_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_a_large_pull_lands_in_the_background_when_nothing_moved(merging, monkeypatch):
    base, edit = b"base", b"the machine's large edit"
    monkeypatch.setattr(remote_workspace_apply, "_DEFER_PULL_MIN_BYTES", 1)
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    path = merging.write(REL, base)
    sync_state_store.record_one("m-1", AGENT, REL, _h(base), 1.0)
    _manifest(merging, monkeypatch, {REL: (edit, 2.0)})
    pulled = _machine_holds(merging, monkeypatch, edit)
    assert await _merge_now(merging) is True
    await _deferred_ran(merging)
    assert pulled and path.read_bytes() == edit
    assert sync_state_store.get_one("m-1", AGENT, REL)[0] == _h(edit)


@pytest.mark.parametrize("had_a_copy", [True, False], ids=["a save", "a new file"])
@pytest.mark.asyncio
async def test_a_save_before_a_large_pull_runs_is_not_pulled_over(merging, monkeypatch, had_a_copy):
    """A large pull runs long after the manifest, off the merge."""
    base, edit, save = b"base", b"the machine's large edit", b"the person's save, later"
    monkeypatch.setattr(remote_workspace_apply, "_DEFER_PULL_MIN_BYTES", 1)
    m = lf.FakeMachine(merging.cm, "m-1")
    await m.connect()
    merging.hold("s-1", "m-1")
    path = merging.agents / AGENT / REL
    if had_a_copy:
        merging.write(REL, base)
        sync_state_store.record_one("m-1", AGENT, REL, _h(base), 1.0)
    _manifest(merging, monkeypatch, {REL: (edit, 2.0)})
    pulled = _machine_holds(merging, monkeypatch, edit)
    real = merging.layer._run_deferred_pulls

    async def save_first(*args):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(save)
        return await real(*args)

    merging.layer._run_deferred_pulls = save_first
    assert await _merge_now(merging) is True
    await _deferred_ran(merging)
    assert not pulled and path.read_bytes() == save
    await remote_file_flow.pull_through("s-1", REL)
    assert not pulled and path.read_bytes() == save


@pytest.mark.asyncio
async def test_a_file_an_upload_brought_back_and_a_session_removed_is_recorded_again(world, monkeypatch):
    """An upload, an attachment or a file tool's write lands on the platform's
    disk and leaves an older delete record standing. Its fan-out reaches a
    machine, whose ack records the base. A session on the platform then
    removes the file: the end-of-turn scan reads the record as from before
    the file came back and records the delete."""
    from auth.path_policy import SecurityContext
    from services.infra import file_bookkeeping
    from storage.pg import get_conn

    rel = "workspace/plan.md"
    # The scan asks the role matrix whether the session could write the path.
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", world.agents.resolve())
    m = lf.FakeMachine(world.cm, "m-1")
    await m.connect()
    world.hold("s-1", "m-1")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO remote_machines (id, name, registered_by, status, created_at) "
            "VALUES ('m-1','m-1','user-admin','online','2026-01-01T00:00:00+00:00') ON CONFLICT DO NOTHING")
        conn.commit()
    world.write("workspace/keep.md", b"keep")
    sync_state_store.record_one("m-1", AGENT, "workspace/keep.md", _h(b"keep"), 1.0)
    file_tombstones_store.record(AGENT, rel, time.time() - 600, origin="live-delete")
    file_bookkeeping._scans.clear()
    monkeypatch.setattr(file_bookkeeping, "_watching_since", "9999-01-01T00:00:00+00:00")
    file_bookkeeping.prime_session_delete_watch()

    path = world.write(rel, b"uploaded")
    await workspace_fanout.fan_out_write(AGENT, rel, path)
    await _until(lambda: sync_state_store.get_one("m-1", AGENT, rel) is not None)
    path.unlink()
    session = SecurityContext(role="manager", username="alice", agent=AGENT, is_admin_agent=False)

    try:
        assert file_bookkeeping._scan_agent_sync(AGENT, [session]) == [rel]
        assert file_tombstones_store.get(AGENT, rel)["origin"] == "session-delete"
    finally:
        file_bookkeeping._scans.clear()

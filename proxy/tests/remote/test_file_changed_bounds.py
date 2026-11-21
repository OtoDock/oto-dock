"""``file_changed`` frames from a satellite: admission before any state is
touched, one bounded lane per machine inside a global bound, shedding past
the in-flight ceiling (with the quiet-window stamp kept), a bounded stamp
map that survives a reconnect blip, and the fan-out outside the path lock
for an inline write only.
"""

import asyncio
import base64
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import config
from core.remote import remote_file_flow, satellite_file_transfer as sft
from core.remote.satellite_connection import SatelliteConnectionManager
from tests._timing import bound


def _sec(machine_id="machine-1", agent="my-agent", role="manager"):
    return SimpleNamespace(
        placement=SimpleNamespace(machine_id=machine_id), role=role, username="alice",
        agent=agent, mount_username=None, knowledge_rw=False,
    )


def _frame(i, session="sess-1", agent="my-agent", path=None):
    return {
        "type": "file_changed", "agent_slug": agent,
        "path": path or f"workspace/f{i:04d}.txt", "action": "write",
        "session_id": session, "hash": _X_HASH,
        "content_b64": base64.b64encode(b"x").decode(),
    }


# An inline write's hash is the hash of the bytes it carries.
_X_HASH = "sha256:" + __import__("hashlib").sha256(b"x").hexdigest()


@pytest.fixture(autouse=True)
def _clean():
    sft.LAST_FILE_CHANGED.clear()
    yield
    sft.LAST_FILE_CHANGED.clear()


class _Counter:
    def __init__(self):
        self.running = 0
        self.peak = 0
        self.total = 0
        self.pulling = 0


def _parked_applier(cm, gate: asyncio.Event, counter: _Counter):
    async def _inner(*a, **kw):
        counter.running += 1
        counter.peak = max(counter.peak, counter.running)
        counter.total += 1
        await gate.wait()
        counter.running -= 1
    return patch.object(cm, "_apply_admitted", new=_inner)


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_appliers_are_bounded_per_machine(monkeypatch):
    monkeypatch.setattr(config, "SAT_FILE_CHANGED_MAX_INFLIGHT_PER_MACHINE", 0)
    monkeypatch.setattr(config, "SAT_FILE_CHANGED_CONCURRENCY_PER_MACHINE", 4)
    monkeypatch.setattr(config, "SAT_FILE_CHANGED_CONCURRENCY_GLOBAL", 8)
    cm = SatelliteConnectionManager()
    gate, counter = asyncio.Event(), _Counter()
    with patch("core.session.session_state.get_session_security", return_value=_sec()), \
         _parked_applier(cm, gate, counter):
        for i in range(300):
            await cm.handle_message("machine-1", _frame(i))
        await _settle()
        assert counter.peak <= 4
        gate.set()
        for _ in range(200):
            await asyncio.sleep(0)
            if counter.total == 300 and counter.running == 0:
                break
    assert counter.total == 300
    assert counter.peak <= 4
    assert cm._fc_lanes["machine-1"].inflight == 0


def _parked_platform_work(cm, gate: asyncio.Event, counter: _Counter, *, pull_first=None):
    """The applier's own step, parked: it runs inside the path lock and the
    fleet slot, so its count is the count of fleet slots in use.
    ``pull_first``: an event a "large file" waits on with its slot given back,
    as a pull from its machine does."""
    async def _inner(agent_dir, msg, machine_id, agent_slug, action, *, slot=None):
        if pull_first is not None and "content_b64" not in msg:
            async with slot.released():
                counter.pulling += 1
                await pull_first.wait()
        counter.running += 1
        counter.peak = max(counter.peak, counter.running)
        counter.total += 1
        await gate.wait()
        counter.running -= 1
        return True
    return patch.object(cm, "_apply_file_changed_inner", new=_inner)


def _big(i, session, path=None):
    return {"type": "file_changed", "agent_slug": "my-agent", "action": "write",
            "path": path or f"workspace/big{i:04d}.bin", "session_id": session,
            "hash": f"sha256:{i:064x}", "size": 5_000_000}


@pytest.mark.asyncio
async def test_the_global_bound_holds_across_machines(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(config, "SAT_FILE_CHANGED_MAX_INFLIGHT_PER_MACHINE", 0)
    monkeypatch.setattr(config, "SAT_FILE_CHANGED_CONCURRENCY_PER_MACHINE", 4)
    monkeypatch.setattr(config, "SAT_FILE_CHANGED_CONCURRENCY_GLOBAL", 6)
    cm = SatelliteConnectionManager()
    gate, counter = asyncio.Event(), _Counter()
    secs = {"sess-1": _sec("machine-1"), "sess-2": _sec("machine-2")}
    with patch("core.session.session_state.get_session_security",
               side_effect=secs.get), \
         _parked_platform_work(cm, gate, counter):
        for i in range(20):
            await cm.handle_message("machine-1", _frame(i, session="sess-1", path=f"workspace/a{i}.txt"))
            await cm.handle_message("machine-2", _frame(i, session="sess-2", path=f"workspace/b{i}.txt"))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if counter.running == 6:
                break
        assert counter.peak == 6
        gate.set()
        for _ in range(500):
            await asyncio.sleep(0.01)
            if counter.total == 40 and counter.running == 0:
                break
    assert counter.total == 40 and counter.peak <= 6


@pytest.mark.asyncio
async def test_machines_that_wait_on_their_own_pulls_hold_no_fleet_slot(monkeypatch, tmp_path):
    """Two machines each report four large files and are slow to send them:
    every fleet slot stays free, and a healthy machine's change is applied
    at once. Measured before the change: it waited the pull's whole stall."""
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(config, "SAT_FILE_CHANGED_MAX_INFLIGHT_PER_MACHINE", 0)
    monkeypatch.setattr(config, "SAT_FILE_CHANGED_CONCURRENCY_PER_MACHINE", 4)
    monkeypatch.setattr(config, "SAT_FILE_CHANGED_CONCURRENCY_GLOBAL", 8)
    cm = SatelliteConnectionManager()
    gate, counter, pulling = asyncio.Event(), _Counter(), asyncio.Event()
    gate.set()
    secs = {"s-slow1": _sec("m-slow1"), "s-slow2": _sec("m-slow2"), "s-ok": _sec("m-ok")}
    with patch("core.session.session_state.get_session_security", side_effect=secs.get), \
         _parked_platform_work(cm, gate, counter, pull_first=pulling):
        for n, mid in enumerate(("m-slow1", "m-slow2")):
            for i in range(4):
                await cm.handle_message(mid, _big(n * 10 + i, f"s-slow{n + 1}"))
        async with asyncio.timeout(10):
            # Eight appliers wait on their machines, and every slot is free.
            while counter.pulling < 8 or cm._fc_global_sem._value != 8:  # noqa: ASYNC110  # the appliers' own counters: no event to wait on
                await asyncio.sleep(0.01)
        assert sft.file_changed_in_flight("m-slow1", "my-agent") == 4
        started = asyncio.get_running_loop().time()
        await cm.handle_message("m-ok", _frame(1, session="s-ok", path="workspace/small.txt"))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if counter.total == 1:
                break
        assert counter.total == 1
        assert asyncio.get_running_loop().time() - started < bound(1.0)
        pulling.set()
        for _ in range(500):
            await asyncio.sleep(0.01)
            if counter.total == 9 and not sft._IN_FLIGHT:
                break
    assert counter.total == 9 and sft._IN_FLIGHT == {}


@pytest.mark.asyncio
async def test_many_frames_of_one_large_path_never_stop_the_fleets_slots(monkeypatch, tmp_path):
    """The order is the path lock, then the fleet slot: an applier that pulls
    a large file gives its slot back and takes one again while it still
    holds the path lock, and the frames of the same path that wait for that
    lock hold no slot meanwhile. With the slot taken first, nine frames of
    one path and a bound of two would wait on each other for good."""
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(config, "SAT_FILE_CHANGED_MAX_INFLIGHT_PER_MACHINE", 0)
    monkeypatch.setattr(config, "SAT_FILE_CHANGED_CONCURRENCY_PER_MACHINE", 8)
    monkeypatch.setattr(config, "SAT_FILE_CHANGED_CONCURRENCY_GLOBAL", 2)
    cm = SatelliteConnectionManager()
    gate, counter, pulling = asyncio.Event(), _Counter(), asyncio.Event()
    gate.set()
    secs = {"s-1": _sec("m-1"), "s-2": _sec("m-2")}
    with patch("core.session.session_state.get_session_security", side_effect=secs.get), \
         _parked_platform_work(cm, gate, counter, pull_first=pulling):
        for i in range(9):
            mid = "m-1" if i % 2 else "m-2"
            await cm.handle_message(mid, _big(i, "s-1" if i % 2 else "s-2",
                                              path="workspace/shared.bin"))
        await _settle()
        pulling.set()
        async with asyncio.timeout(10):
            while counter.total < 9 or sft._IN_FLIGHT:  # noqa: ASYNC110  # the appliers' own counters: no event to wait on
                await asyncio.sleep(0.01)
    assert counter.total == 9 and cm._fc_global_sem._value == 2


@pytest.mark.asyncio
async def test_frames_past_the_inflight_ceiling_are_shed_but_still_stamp(monkeypatch, caplog):
    monkeypatch.setattr(config, "SAT_FILE_CHANGED_MAX_INFLIGHT_PER_MACHINE", 64)
    monkeypatch.setattr(config, "SAT_FILE_CHANGED_CONCURRENCY_PER_MACHINE", 4)
    cm = SatelliteConnectionManager()
    gate, counter = asyncio.Event(), _Counter()
    caplog.set_level(logging.WARNING, logger="otodock.satellite")
    with patch("core.session.session_state.get_session_security", return_value=_sec()), \
         _parked_applier(cm, gate, counter):
        for i in range(300):
            await cm.handle_message("machine-1", _frame(i))
        await _settle()
        assert cm._fc_lanes["machine-1"].inflight == 64
        assert sft.last_file_changed_at("machine-1", "my-agent") > 0
        gate.set()
        for _ in range(200):
            await asyncio.sleep(0)
            if counter.running == 0 and counter.total >= 64:
                break
    assert counter.total == 64
    shed = [r for r in caplog.records if "shed" in r.getMessage()]
    assert len(shed) == 1  # rate-limited: one warning for the burst


@pytest.mark.asyncio
async def test_a_frame_that_fails_admission_touches_nothing():
    cm = SatelliteConnectionManager()
    applied = AsyncMock()
    with patch("core.session.session_state.get_session_security", return_value=None), \
         patch.object(cm, "_apply_admitted", new=applied):
        await cm.handle_message("machine-1", _frame(1))          # no session
        await _settle()
    with patch("core.session.session_state.get_session_security",
               return_value=_sec(agent="other-agent")), \
         patch.object(cm, "_apply_admitted", new=applied):
        await cm.handle_message("machine-1", _frame(2))          # another agent's context
        await _settle()
    with patch("core.session.session_state.get_session_security",
               return_value=_sec(machine_id="machine-9")), \
         patch.object(cm, "_apply_admitted", new=applied):
        await cm.handle_message("machine-1", _frame(3))          # bound to another machine
        await _settle()
    applied.assert_not_awaited()
    assert sft.LAST_FILE_CHANGED == {}
    assert "machine-1" not in cm._fc_lanes


def test_the_stamp_map_is_bounded(monkeypatch):
    monkeypatch.setattr(config, "SAT_FILE_CHANGED_STAMP_MAX", 3)
    for i in range(5):
        sft._stamp_file_changed(f"m{i}", "agent")
    assert len(sft.LAST_FILE_CHANGED) == 3
    assert sft.last_file_changed_at("m0", "agent") == 0.0
    assert sft.last_file_changed_at("m4", "agent") > 0
    sft._stamp_file_changed("m4", "other")
    sft.evict_file_changed_stamps("m4")
    assert sft.last_file_changed_at("m4", "agent") == 0.0
    assert sft.last_file_changed_at("m4", "other") == 0.0
    assert sft.last_file_changed_at("m3", "agent") > 0


@pytest.mark.asyncio
async def test_deregister_keeps_the_stamps_of_a_machine_in_grace():
    cm = SatelliteConnectionManager()
    sft._stamp_file_changed("machine-1", "my-agent")
    cm._grace_sessions["machine-1"] = {"sess-1": (asyncio.Queue(), "")}
    await cm.deregister("machine-1")
    assert sft.last_file_changed_at("machine-1", "my-agent") > 0
    cm._grace_sessions.clear()
    await cm.deregister("machine-1")
    assert sft.last_file_changed_at("machine-1", "my-agent") == 0.0


@pytest.mark.asyncio
async def test_a_replaced_connection_finishes_on_its_own_lane(monkeypatch):
    monkeypatch.setattr(config, "SAT_FILE_CHANGED_MAX_INFLIGHT_PER_MACHINE", 0)
    cm = SatelliteConnectionManager()
    gate, counter = asyncio.Event(), _Counter()
    with patch("core.session.session_state.get_session_security", return_value=_sec()), \
         _parked_applier(cm, gate, counter):
        for i in range(3):
            await cm.handle_message("machine-1", _frame(i))
        await _settle()
        old = cm._fc_lanes["machine-1"]
        assert old.inflight == 3
        cm._reset_file_changed_lane("machine-1")
        assert "machine-1" not in cm._fc_lanes
        await cm.handle_message("machine-1", _frame(3))
        await _settle()
        new = cm._fc_lanes["machine-1"]
        assert new is not old and new.inflight == 1 and old.inflight == 3
        gate.set()
        for _ in range(100):
            await asyncio.sleep(0)
            if counter.running == 0 and counter.total == 4:
                break
    assert old.inflight == 0 and new.inflight == 0


def _real_applier_patches(fan_out, targets, pull_ok=None):
    return [
        patch("core.session.session_state.get_session_security", return_value=_sec()),
        patch("core.remote.file_sync.apply_incoming_file", lambda *a, **k: None),
        patch("storage.files.sync_state_store.get_one", return_value=None),
        patch("storage.files.sync_state_store.record_one"),
        patch("storage.files.file_author_store.record"),
        patch("storage.files.file_author_store.get", return_value=None),
        patch("storage.files.recover_bin_store.capture", return_value=None),
        patch("services.remote.workspace_fanout.fanout_targets", return_value=targets),
        patch("services.remote.workspace_fanout.fan_out_write", new=fan_out),
        patch("services.notifications.notification_manager.broadcast_file_updated",
              new=AsyncMock()),
        patch("core.remote.sync_delta_alerts.record_turn_write", return_value=None),
    ]


@pytest.mark.asyncio
async def test_an_inline_write_fans_out_bytes_after_the_path_lock_is_released(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    cm = SatelliteConnectionManager()
    seen = {}

    async def fan_out(agent_slug, rel_path, source, **kw):
        lock = await remote_file_flow._acquire_global_path_lock(agent_slug, rel_path)
        seen["locked"] = lock.locked()
        seen["source"] = source
    patches = _real_applier_patches(fan_out, ["m2"])
    for p in patches:
        p.start()
    try:
        await cm._apply_file_changed("machine-1", _frame(1, path="workspace/a.txt"))
    finally:
        for p in patches:
            p.stop()
    assert seen["locked"] is False
    assert seen["source"] == b"x"


@pytest.mark.asyncio
async def test_a_pulled_file_fans_out_a_path_after_the_path_lock_is_released(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    (tmp_path / "my-agent" / "workspace").mkdir(parents=True)
    (tmp_path / "my-agent" / "workspace" / "big.bin").write_bytes(b"y" * 8)
    cm = SatelliteConnectionManager()
    seen = {}

    async def fan_out(agent_slug, rel_path, source, **kw):
        lock = await remote_file_flow._acquire_global_path_lock(agent_slug, rel_path)
        seen["locked"] = lock.locked()
        seen["source"] = source
        fanout_lock = await remote_file_flow.acquire_fanout_lock(agent_slug, rel_path)
        seen["fanout_locked"] = fanout_lock.locked()
    frame = {"type": "file_changed", "agent_slug": "my-agent", "path": "workspace/big.bin",
             "action": "write", "session_id": "sess-1", "hash": "sha256:" + "0" * 64,
             "size": 8}
    patches = _real_applier_patches(fan_out, ["m2"]) + [
        patch.object(cm, "pull_file_to_path", new=AsyncMock(return_value=True)),
    ]
    for p in patches:
        p.start()
    try:
        await cm._apply_file_changed("machine-1", frame)
    finally:
        for p in patches:
            p.stop()
    assert seen["locked"] is False
    assert seen["fanout_locked"] is True
    assert isinstance(seen["source"], Path)
    lock = await remote_file_flow.acquire_fanout_lock("my-agent", "workspace/big.bin")
    assert not lock.locked()


@pytest.mark.asyncio
async def test_a_pulled_file_lands_at_the_named_path_and_fans_out_only_from_it(tmp_path, monkeypatch):
    """The pull and the fan-out source are the path as named: through an
    in-tree link on the way, the pull is handed the named path (which it
    refuses) and nothing fans out from the link's target."""
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    ws = tmp_path / "my-agent" / "workspace"
    (ws / "real").mkdir(parents=True)
    (ws / "real" / "big.bin").write_bytes(b"y" * 8)
    (ws / "alias").symlink_to("real")
    (ws / "big.bin").write_bytes(b"z" * 8)
    cm = SatelliteConnectionManager()
    sources = []

    async def fan_out(agent_slug, rel_path, source, **kw):
        sources.append(source)
    pull = AsyncMock(return_value=True)
    patches = _real_applier_patches(fan_out, ["m2"]) + [
        patch.object(cm, "pull_file_to_path", new=pull),
    ]
    for p in patches:
        p.start()
    try:
        for rel in ("workspace/alias/big.bin", "workspace/big.bin"):
            await cm._apply_file_changed("machine-1", {
                "type": "file_changed", "agent_slug": "my-agent", "path": rel,
                "action": "write", "session_id": "sess-1", "hash": "sha256:" + "0" * 64,
                "size": 8})
    finally:
        for p in patches:
            p.stop()
    assert [c.args[2] for c in pull.await_args_list] == [ws / "alias" / "big.bin", ws / "big.bin"]
    assert sources == [ws / "big.bin"]


@pytest.mark.asyncio
async def test_the_shared_only_read_of_a_personal_path_leaves_the_loop(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    (tmp_path / "my-agent" / "users" / "alice" / "workspace").mkdir(parents=True)
    cm = SatelliteConnectionManager()
    seen = {}

    def is_shared_only(agent_name):
        seen["agent"] = agent_name
        seen["thread"] = threading.current_thread()
        return False
    monkeypatch.setattr("core.session.visibility.is_shared_only", is_shared_only)
    frame = _frame(1, path="users/alice/workspace/note.txt")
    patches = _real_applier_patches(AsyncMock(), [])
    for p in patches:
        p.start()
    try:
        await cm._apply_file_changed("machine-1", frame)
    finally:
        for p in patches:
            p.stop()
    assert seen.get("agent") == "my-agent", "the store was never read for the personal path"
    assert seen["thread"] is not threading.main_thread()


@pytest.mark.asyncio
async def test_same_path_fan_outs_never_overlap(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    cm = SatelliteConnectionManager()
    state = {"running": 0, "peak": 0, "order": []}

    async def fan_out(agent_slug, rel_path, source, **kw):
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        state["order"].append(source)
        await asyncio.sleep(0.01)
        state["running"] -= 1
    patches = _real_applier_patches(fan_out, ["m2"])
    for p in patches:
        p.start()
    try:
        frames = []
        for i in range(4):
            f = _frame(i, path="workspace/same.txt")
            f["content_b64"] = base64.b64encode(bytes([65 + i])).decode()
            f["hash"] = "sha256:" + __import__("hashlib").sha256(bytes([65 + i])).hexdigest()
            frames.append(f)
        await asyncio.gather(*(cm._apply_file_changed("machine-1", f) for f in frames))
    finally:
        for p in patches:
            p.stop()
    assert state["peak"] == 1
    assert state["order"] == [b"A", b"B", b"C", b"D"]


@pytest.mark.asyncio
async def test_a_judge_waits_while_a_change_is_queued_however_old_its_stamp(monkeypatch):
    """A frame that waits for a slot, or a large file still being pulled,
    stamps the quiet window only at its start and its end: the judge reads
    the count in flight too."""
    from services.checks.kinds import judge_kind
    monkeypatch.setattr(judge_kind, "SYNC_QUIET_S", 0.05)
    monkeypatch.setattr(judge_kind, "SYNC_WAIT_MAX_S", 5.0)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(judge_kind.asyncio, "sleep", lambda s: real_sleep(0.01))
    sft._in_flight("m-1", "my-agent", +1)
    waiting = asyncio.ensure_future(judge_kind._wait_sync_quiet("m-1", "my-agent"))
    await real_sleep(0.2)
    assert not waiting.done()               # the stamp is long past quiet, a frame is not
    sft._in_flight("m-1", "my-agent", -1)
    await asyncio.wait_for(waiting, 2)
    assert sft._IN_FLIGHT == {}


@pytest.mark.asyncio
async def test_an_inline_write_whose_bytes_are_not_its_hash_is_not_applied(tmp_path, monkeypatch):
    """The frame's hash becomes the merge base: an inline write applies only
    when it is the hash of the bytes it carries. Nothing is written, recorded
    or fanned out otherwise; a frame with no hash applies as before."""
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    cm = SatelliteConnectionManager()
    fanned: list = []
    written: list = []

    async def fan_out(agent_slug, rel_path, source, **kw):
        fanned.append(rel_path)
    patches = _real_applier_patches(fan_out, ["m2"]) + [
        patch("core.remote.file_sync.apply_incoming_file",
              lambda agent_dir, rel, action, content: written.append(rel))]
    for p in patches:
        p.start()
    try:
        bad = _frame(1, path="workspace/bad.txt")
        bad["hash"] = "sha256:" + "0" * 64
        await cm._apply_file_changed("machine-1", bad)
        good = _frame(2, path="workspace/good.txt")
        await cm._apply_file_changed("machine-1", good)
        bare = _frame(3, path="workspace/bare.txt")
        del bare["hash"]
        await cm._apply_file_changed("machine-1", bare)
    finally:
        for p in reversed(patches):
            p.stop()
    assert written == fanned == ["workspace/good.txt", "workspace/bare.txt"]

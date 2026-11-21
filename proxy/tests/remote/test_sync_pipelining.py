"""Sync-performance W1-W3 (2026-07-19): windowed pipelined pushes, progress
ticks, and the enable-click pre-sync.

W1: the initial sync used to await each per-file ack before SENDING the next —
RTT × N wire serialization (the 25-minute first-remote-turn report). The
windowed apply keeps ≤8 actions in flight; per-path ordering stays with the
global path lock inside _apply and failures still log-and-continue.
"""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests._paths import PROXY_DIR as _PROXY_DIR

if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

from core.remote.remote_execution import RemoteExecutionLayer


def _push_action(rp: str):
    return SimpleNamespace(
        op="push", rel_path=rp, capture_side="", capture_reason="",
        notify_user="", base_hash="", drop_tombstone=False, clear_base=False,
    )


def _make_cm(push_delays: dict):
    """Fake connection manager: push_file resolves after a per-file delay so
    acks return OUT OF ORDER; records the max in-flight concurrency."""
    cm = MagicMock()
    state = {"active": 0, "max_active": 0, "pushed": []}

    @asynccontextmanager
    async def _lock(*a, **kw):
        yield

    cm.get_sync_lock = _lock
    cm.get_clock_offset = MagicMock(return_value=0.0)
    cm.send_command = AsyncMock(return_value={"files": []})
    # A bare MagicMock answers truthy: every merge here is a Linux one.
    cm.satellite_restores_holds = MagicMock(return_value=False)

    async def _push_file(machine_id, ref, content, agent_slug="", **kw):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(push_delays.get(ref.value, 0.01))
        state["active"] -= 1
        state["pushed"].append(ref.value)
        return True

    cm.push_file = _push_file
    return cm, state


async def _run_sync(tmp_path, monkeypatch, actions, cm, progress_cb=None,
                    manifest_fn=None, diff_fn=None, **sync_kw):
    import config as _cfg
    from core.remote import file_sync
    from core.session import visibility
    from storage.files import file_tombstones_store, sync_state_store

    agent_dir = tmp_path / "test-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    for a in actions:
        p = agent_dir / a.rel_path
        if os.path.lexists(p):  # noqa: ASYNC240  # a test's own temporary files
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"content-" + a.rel_path.encode())

    def _as_listed(*a, **kw):
        """What a walk lists: a pull checks the platform copy is still the one the plan saw."""
        paths = [(a.rel_path, os.lstat(agent_dir / a.rel_path)) for a in actions]
        return [file_sync.FileEntry(rel, "sha256:listed", st.st_size, st.st_mtime) for rel, st in paths]

    monkeypatch.setattr(_cfg, "AGENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(visibility, "is_shared_only", lambda slug: False)
    monkeypatch.setattr(file_sync, "compute_manifest", manifest_fn or _as_listed)
    monkeypatch.setattr(sync_state_store, "load_for_machine_agent",
                        sync_kw.pop("base_fn", lambda *a: {}))
    monkeypatch.setattr(file_tombstones_store, "load_for_agent", lambda *a: {})
    monkeypatch.setattr(file_tombstones_store, "load_holds_for_agent",
                        sync_kw.pop("holds_fn", lambda *a: {}))
    monkeypatch.setattr(
        file_sync, "diff_manifests",
        diff_fn or (lambda *a, **kw: SimpleNamespace(actions=actions, to_scrub=[], left_unread=[])),
    )

    layer = RemoteExecutionLayer(cm)
    await layer._initial_workspace_sync(
        "machine-1", "test-agent", target_username=None, target_role="admin",
        progress_cb=progress_cb, **sync_kw,
    )


@pytest.mark.asyncio
async def test_windowed_pushes_pipeline_and_all_complete(tmp_path, monkeypatch):
    # Reverse-sorted delays → the FIRST-sent pushes ack LAST. All must still
    # complete, and >1 must have been in flight at once (the window works).
    actions = [_push_action(f"workspace/f{i}.txt") for i in range(20)]
    delays = {a.rel_path: 0.001 * (20 - i) for i, a in enumerate(actions)}
    cm, state = _make_cm(delays)
    ticks = []

    async def _progress(done, total):
        ticks.append((done, total))

    await _run_sync(tmp_path, monkeypatch, actions, cm, progress_cb=_progress)

    assert sorted(state["pushed"]) == sorted(a.rel_path for a in actions)
    assert state["max_active"] > 1, "pushes never overlapped — window inert"
    assert state["max_active"] <= 8, "window bound exceeded"
    # Final tick always lands on (total, total) even under throttling.
    assert ticks and ticks[-1] == (20, 20)


@pytest.mark.asyncio
async def test_failed_push_logs_and_continues(tmp_path, monkeypatch):
    actions = [_push_action(f"workspace/g{i}.txt") for i in range(6)]
    cm, state = _make_cm({})
    real_push = cm.push_file

    async def _flaky(machine_id, ref, content, agent_slug="", **kw):
        if ref.value.endswith("g2.txt"):
            return False  # ack'd failure — must not abort the rest
        if ref.value.endswith("g3.txt"):
            raise RuntimeError("wire dropped")  # raised failure — same
        return await real_push(machine_id, ref, content, agent_slug=agent_slug)

    cm.push_file = _flaky
    await _run_sync(tmp_path, monkeypatch, actions, cm)
    assert sorted(state["pushed"]) == sorted(
        a.rel_path for a in actions
        if not a.rel_path.endswith(("g2.txt", "g3.txt"))
    )


@pytest.mark.asyncio
async def test_presync_machine_agent_offline_noop():
    cm = MagicMock()
    cm.is_connected = MagicMock(return_value=False)
    layer = RemoteExecutionLayer(cm)
    layer._initial_workspace_sync = AsyncMock()
    await layer.presync_machine_agent("m1", "agent-x")
    layer._initial_workspace_sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_presync_machine_agent_runs_with_pairing_identity():
    cm = MagicMock()
    cm.is_connected = MagicMock(return_value=True)
    layer = RemoteExecutionLayer(cm)
    layer.resolve_machine_sync_identity = AsyncMock(return_value=("alice", "editor"))
    layer._initial_workspace_sync = AsyncMock()
    await layer.presync_machine_agent("m1", "agent-x")
    layer._initial_workspace_sync.assert_awaited_once_with(
        "m1", "agent-x", target_username="alice", target_role="editor",
    )


@pytest.mark.asyncio
async def test_sync_respects_global_transfer_gate(tmp_path, monkeypatch):
    """The initial sync's per-machine window (8) is further bounded by the
    GLOBAL transfer gate for above-threshold files (Feature F)."""
    import config
    from core.remote import transfer_gate
    monkeypatch.setattr(config, "SYNC_FANOUT_CONCURRENCY", 2, raising=False)
    monkeypatch.setattr(config, "SYNC_FANOUT_MIN_MB", 0, raising=False)
    transfer_gate.reset_for_tests()
    try:
        actions = [_push_action(f"workspace/f{i}.txt") for i in range(12)]
        cm, state = _make_cm({f"workspace/f{i}.txt": 0.01 for i in range(12)})
        await _run_sync(tmp_path, monkeypatch, actions, cm)
        assert sorted(state["pushed"]) == sorted(a.rel_path for a in actions)
        assert state["max_active"] <= 2  # was ≤ 8 (the per-machine window)
    finally:
        transfer_gate.reset_for_tests()


@pytest.mark.asyncio
async def test_tiny_files_bypass_gate_held_by_large_fanout(tmp_path, monkeypatch):
    """A parked large fan-out holding the ONLY slot must not block a
    below-threshold warmup storm (the 4MB bypass)."""
    import asyncio

    import config
    from core.remote import transfer_gate
    monkeypatch.setattr(config, "SYNC_FANOUT_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(config, "SYNC_FANOUT_MIN_MB", 4, raising=False)
    transfer_gate.reset_for_tests()
    release = asyncio.Event()
    parked = asyncio.Event()

    async def _park():
        async with transfer_gate.slot("m-big", "agent-x", "big.bin", 100 * 1024 * 1024):
            parked.set()
            await release.wait()

    park_task = asyncio.create_task(_park())
    await parked.wait()
    try:
        # 30 small sync pushes complete while the slot is held.
        actions = [_push_action(f"workspace/s{i}.txt") for i in range(30)]
        cm, state = _make_cm({})
        async with asyncio.timeout(5):
            await _run_sync(tmp_path, monkeypatch, actions, cm)
        assert len(state["pushed"]) == 30
    finally:
        release.set()
        await park_task
        transfer_gate.reset_for_tests()


@pytest.mark.asyncio
async def test_no_deadlock_sync_plus_live_fanout_share_gate(tmp_path, monkeypatch):
    """Constructed cross-path scenario: initial sync (machine-1) + live
    fan-out (machines 2,3) with ONE gate slot per machine — everything
    completes, no machine runs two large pushes at once, no deadlock across
    sync-lock/_window/path-lock/gate."""
    import asyncio

    import config
    from core.remote import satellite_connection as sc, transfer_gate
    from services.remote import workspace_fanout
    monkeypatch.setattr(config, "SYNC_FANOUT_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(config, "SYNC_FANOUT_MIN_MB", 0, raising=False)
    transfer_gate.reset_for_tests()
    try:
        combined = {"active": {}, "max_active": {}}

        # Shared fake cm: the sync harness's cm records into `state`; wrap it
        # so BOTH paths count into `combined`.
        actions = [_push_action(f"workspace/f{i}.txt") for i in range(6)]
        cm, state = _make_cm({f"workspace/f{i}.txt": 0.01 for i in range(6)})
        inner_push = cm.push_file

        async def _counted(mid, ref, source, **kw):
            act = combined["active"]
            act[mid] = act.get(mid, 0) + 1
            combined["max_active"][mid] = max(combined["max_active"].get(mid, 0), act[mid])
            try:
                await asyncio.sleep(0.01)
                if mid == "machine-1":
                    return await inner_push(mid, ref, source, **kw)
                return True
            finally:
                act[mid] -= 1

        cm.push_file = _counted
        monkeypatch.setattr(
            workspace_fanout, "fanout_targets",
            lambda a, r, *, exclude_machine_id=None: ["m2", "m3"],
        )
        monkeypatch.setattr(sc, "get_connection_manager", lambda: cm)

        # The live fan-out names the file for each machine's lane, which
        # pushes the platform copy through the same gate.
        from core.remote import machine_push_lane
        machine_push_lane.reset()
        cm.is_connected = lambda mid: True

        async def _live():
            # The sync harness set the agents root before its first await.
            big = config.AGENTS_DIR / "agent-live" / "workspace" / "big.bin"
            big.parent.mkdir(parents=True, exist_ok=True)
            big.write_bytes(b"x" * (600 * 1024))
            await workspace_fanout.fan_out_write("agent-live", "workspace/big.bin", big)
            assert await workspace_fanout.drain_fan_outs()

        async with asyncio.timeout(10):
            await asyncio.gather(
                _run_sync(tmp_path, monkeypatch, actions, cm),
                _live(),
            )
        machine_push_lane.reset()
        assert len(state["pushed"]) == 6
        assert combined["max_active"] == {"machine-1": 1, "m2": 1, "m3": 1}
    finally:
        transfer_gate.reset_for_tests()


# --- the manifest work runs on its own small pool -------------------


@pytest.mark.asyncio
async def test_manifest_work_runs_on_the_sync_cpu_executor(tmp_path, monkeypatch):
    import threading

    from core import loop_watchdog

    seen = []

    def _manifest(*a, **kw):
        seen.append(threading.current_thread().name)
        return []

    def _diff(*a, **kw):
        seen.append(threading.current_thread().name)
        return SimpleNamespace(actions=[], to_scrub=[], left_unread=[])

    cm, _ = _make_cm({})
    await _run_sync(tmp_path, monkeypatch, [], cm, manifest_fn=_manifest, diff_fn=_diff)
    assert len(seen) == 2 and all(n.startswith("sync-cpu") for n in seen), seen
    assert loop_watchdog.stats()["executors"]["sync-cpu"]["workers"] == 2


@pytest.mark.asyncio
async def test_a_background_merge_holds_one_worker_and_a_person_gets_the_other(
        tmp_path, monkeypatch):
    """Background manifest work (a reconnect walk, the idle sweep) takes a
    one-slot gate before the pool; a person's merge takes none, so it never
    queues behind a wave of walks."""
    from core.remote import remote_workspace_sync as rws

    gate = rws._background_manifest_gate()
    await gate.acquire()
    seen = []

    def _manifest(*a, **kw):
        seen.append("manifest")
        return []

    cm, _ = _make_cm({})
    parked = asyncio.create_task(_run_sync(
        tmp_path, monkeypatch, [], cm, manifest_fn=_manifest, background=True))
    await asyncio.sleep(0.05)
    assert seen == []                       # waits on the background gate
    await _run_sync(tmp_path, monkeypatch, [], cm, manifest_fn=_manifest)
    assert seen == ["manifest"]             # the person's merge ran through
    gate.release()
    await parked
    assert seen == ["manifest", "manifest"]


# --- pulls and deletes act on the named path ------------------------


def _action(op: str, rp: str, **kw):
    fields = dict(op=op, rel_path=rp, capture_side="", capture_reason="",
                  notify_user="", base_hash="", drop_tombstone=False, clear_base=False,
                  keep_mtime=None)
    fields.update(kw)
    return SimpleNamespace(**fields)


def _record_delete_bookkeeping(monkeypatch):
    from services.notifications import notification_manager
    from services.remote import workspace_fanout
    from storage.files import (
        file_author_store,
        file_tombstones_store,
        recover_bin_store,
        sync_state_store,
    )
    seen = {"tombstone": [], "clear_base": [], "clear_author": [], "fan_out": [],
            "capture": []}
    monkeypatch.setattr(file_tombstones_store, "record",
                        lambda agent, rp, *a, **kw: seen["tombstone"].append(rp))
    monkeypatch.setattr(sync_state_store, "clear_one",
                        lambda mid, agent, rp: seen["clear_base"].append(rp))
    monkeypatch.setattr(file_author_store, "clear",
                        lambda agent, rp: seen["clear_author"].append(rp))
    monkeypatch.setattr(recover_bin_store, "capture",
                        lambda agent, rp, data, reason: seen["capture"].append((rp, data)))

    async def _fan_out_delete(agent, rp, **kw):
        seen["fan_out"].append(rp)
    monkeypatch.setattr(workspace_fanout, "fan_out_delete", _fan_out_delete)
    monkeypatch.setattr(notification_manager, "broadcast_file_updated", AsyncMock())
    return seen


@pytest.mark.asyncio
async def test_an_initial_sync_pull_is_handed_the_named_path(tmp_path, monkeypatch):
    ws = tmp_path / "test-agent" / "workspace"
    (ws / "real").mkdir(parents=True)
    (ws / "alias").symlink_to("real")
    cm, _ = _make_cm({})
    cm.pull_file_to_path = AsyncMock(return_value=False)
    await _run_sync(tmp_path, monkeypatch, [_action("pull", "workspace/alias/doc.txt")], cm)
    cm.pull_file_to_path.assert_awaited_once()
    assert cm.pull_file_to_path.await_args.args[2] == ws / "alias" / "doc.txt"


@pytest.mark.asyncio
async def test_a_platform_delete_removes_a_link_at_the_name_never_its_target(tmp_path, monkeypatch):
    ws = tmp_path / "test-agent" / "workspace"
    ws.mkdir(parents=True)
    (ws / "target.txt").write_bytes(b"kept")
    (ws / "doc.txt").symlink_to("target.txt")
    seen = _record_delete_bookkeeping(monkeypatch)
    cm, _ = _make_cm({})
    actions = [
        _action("delete_platform", "workspace/doc.txt", clear_base=True,
                capture_side="platform", capture_reason="deleted"),
        _action("delete_platform", "workspace/plain.txt", clear_base=True,
                capture_side="platform", capture_reason="deleted"),
    ]
    await _run_sync(tmp_path, monkeypatch, actions, cm)
    assert not os.path.lexists(ws / "doc.txt")  # noqa: ASYNC240  # a test's own temporary files
    assert (ws / "target.txt").is_file() and not (ws / "target.txt").is_symlink()
    assert not (ws / "plain.txt").exists()
    assert sorted(seen["tombstone"]) == ["workspace/doc.txt", "workspace/plain.txt"]
    assert sorted(seen["fan_out"]) == ["workspace/doc.txt", "workspace/plain.txt"]
    # The capture reads the platform copy itself: a link's target is not it.
    assert seen["capture"] == [("workspace/plain.txt", b"content-workspace/plain.txt")]


@pytest.mark.asyncio
async def test_a_platform_delete_through_a_link_above_removes_nothing(tmp_path, monkeypatch):
    ws = tmp_path / "test-agent" / "workspace"
    (ws / "real").mkdir(parents=True)
    (ws / "alias").symlink_to("real")
    (ws / "dir.txt").mkdir()
    seen = _record_delete_bookkeeping(monkeypatch)
    cm, _ = _make_cm({})
    actions = [_action("delete_platform", "workspace/alias/doc.txt", clear_base=True),
               _action("delete_platform", "workspace/dir.txt", clear_base=True)]
    await _run_sync(tmp_path, monkeypatch, actions[:1], cm)
    await _run_sync(tmp_path, monkeypatch, actions[1:], cm)
    assert (ws / "real" / "doc.txt").is_file() and (ws / "alias").is_symlink()
    assert (ws / "dir.txt").is_dir()
    # A refused delete claims nothing: no tombstone, base and author kept,
    # no fan-out.
    assert seen == {"tombstone": [], "clear_base": [], "clear_author": [], "fan_out": [],
                    "capture": []}


@pytest.mark.asyncio
async def test_an_initial_sync_push_pins_the_bytes_the_merge_planned(tmp_path, monkeypatch):
    """A push's base hash is the platform copy's hash from the manifest: it
    is the frame's hash, so a file changed since then fails its push
    instead of recording a base that names other bytes."""
    from storage.files import sync_state_store
    recorded = []
    monkeypatch.setattr(sync_state_store, "record_one",
                        lambda mid, agent, rp, h, m: recorded.append((rp, h)))
    cm, _ = _make_cm({})
    hashes = {}

    async def _push(machine_id, ref, content, agent_slug="", **kw):
        hashes[ref.value] = kw.get("content_hash")
        return ref.value != "workspace/changed.txt"
    cm.push_file = _push
    actions = [_action("push", "workspace/same.txt", base_hash="sha256:" + "1" * 64),
               _action("push", "workspace/changed.txt", base_hash="sha256:" + "2" * 64)]
    await _run_sync(tmp_path, monkeypatch, actions, cm)
    assert hashes == {"workspace/same.txt": "sha256:" + "1" * 64,
                      "workspace/changed.txt": "sha256:" + "2" * 64}
    assert recorded == [("workspace/same.txt", "sha256:" + "1" * 64)]


@pytest.mark.asyncio
async def test_a_failed_pinned_push_drops_the_cached_manifest_hash(tmp_path, monkeypatch):
    """The manifest hash cache is keyed on (size, mtime_ns): a same-size
    rewrite that keeps its mtime leaves a stale hash, and a push pinned to
    it fails. The failure drops that path's entry, so the next merge hashes
    the file again and the push heals."""
    from collections import OrderedDict

    from core.remote import file_sync
    from storage.files import sync_state_store
    monkeypatch.setattr(file_sync, "_HASH_CACHE", OrderedDict())
    monkeypatch.setattr(sync_state_store, "record_one", lambda *a: None)
    manifest = file_sync.compute_manifest
    cm, _ = _make_cm({})

    async def _push(machine_id, ref, content, agent_slug="", **kw):
        return ref.value != "workspace/stale.txt"
    cm.push_file = _push
    actions = [_action("push", "workspace/kept.txt", base_hash="sha256:" + "1" * 64),
               _action("push", "workspace/stale.txt", base_hash="sha256:" + "2" * 64)]
    await _run_sync(tmp_path, monkeypatch, actions, cm, manifest_fn=manifest)
    cached = {k.rsplit("/", 1)[-1] for k in file_sync._HASH_CACHE}
    assert cached == {"kept.txt"}


@pytest.mark.asyncio
async def test_a_platform_delete_under_a_vanished_parent_is_already_done(tmp_path, monkeypatch):
    """A parent gone since the manifest took the file with it: the delete
    is done and its bookkeeping follows (tombstone, base, author, fan-out)."""
    import shutil
    ws = tmp_path / "test-agent" / "workspace"
    seen = _record_delete_bookkeeping(monkeypatch)
    cm, _ = _make_cm({})

    def _manifest(*a, **kw):
        shutil.rmtree(ws / "gone")
        return []

    await _run_sync(tmp_path, monkeypatch,
                    [_action("delete_platform", "workspace/gone/doc.txt", clear_base=True)],
                    cm, manifest_fn=_manifest)
    assert seen["tombstone"] == seen["clear_base"] == seen["clear_author"] == \
        seen["fan_out"] == ["workspace/gone/doc.txt"]


@pytest.mark.asyncio
async def test_an_acked_merge_push_clears_the_platform_ahead_mark_and_moves_the_row(tmp_path, monkeypatch):
    """An acked push leaves the machine holding the platform copy: its
    platform-ahead mark goes (a read there would otherwise push the same
    bytes again), a failed push keeps it. The row moves with a push's acked
    bytes and never steps back."""
    from core.remote import remote_file_flow
    from storage.files import sync_state_store
    monkeypatch.setattr(sync_state_store, "record_one", lambda *a: None)
    for rp in ("workspace/ok.bin", "workspace/fails.bin"):
        remote_file_flow.note_platform_ahead("machine-1", "test-agent", rp, 1, 1)
    cm, _ = _make_cm({})

    async def _push(machine_id, ref, content, agent_slug="", progress_cb=None, **kw):
        if ref.value == "workspace/fails.bin":
            return False
        for sent in (256, 512, 768):
            await asyncio.sleep(0.55)    # past the row's 0.5 s throttle
            await progress_cb(sent, 1024)
        return True
    cm.push_file = _push
    ticks: list[float] = []

    async def _progress(done, total):
        ticks.append(done)

    actions = [_action("push", "workspace/ok.bin"), _action("push", "workspace/fails.bin")]
    try:
        await _run_sync(tmp_path, monkeypatch, actions, cm, progress_cb=_progress)
        assert ("machine-1", "test-agent", "workspace/ok.bin") not in remote_file_flow._platform_ahead
        assert ("machine-1", "test-agent", "workspace/fails.bin") in remote_file_flow._platform_ahead
        assert any(t != int(t) for t in ticks)         # the push's share moved it
        assert ticks == sorted(ticks) and ticks[-1] == 2
    finally:
        remote_file_flow._platform_ahead.clear()


@pytest.mark.asyncio
async def test_the_merge_asks_for_skips_and_hands_them_to_the_plan(tmp_path, monkeypatch):
    """The satellite is asked, in pages, to list what it could not read, and
    those entries reach the plan with the holds and the machine's restore."""
    cm, _ = _make_cm({})
    seen: dict = {}
    reply = {"files": [{"path": "workspace/locked.pst", "skipped": "unreadable",
                        "size": 1, "mtime": 0}]}

    async def _send(machine_id, msg, **kw):
        seen["msg"] = msg
        return reply

    cm.send_command = _send
    cm.satellite_restores_holds.return_value = True

    def _diff(local, remote, **kw):
        seen["remote"], seen["kw"] = remote, kw
        return SimpleNamespace(actions=[], to_scrub=[], left_unread=[])

    await _run_sync(tmp_path, monkeypatch, [], cm, diff_fn=_diff,
                    holds_fn=lambda *a: {"workspace/x.txt": 2.0})
    assert seen["msg"]["report_skipped"] is True and seen["msg"]["page_size"] > 0
    assert seen["remote"] == reply["files"]
    assert seen["kw"]["restore_holds"] is True
    assert seen["kw"]["holds"] == {"workspace/x.txt": 2.0}


@pytest.mark.asyncio
async def test_a_restore_pull_lands_with_the_machines_time(tmp_path, monkeypatch):
    from core.remote.file_sync import FileAction
    cm, _ = _make_cm({})
    cm.pull_file_to_path = AsyncMock(return_value=True)
    restore = FileAction("workspace/notes.md", "pull", drop_tombstone=True, keep_mtime=1000.0)
    plain = FileAction("workspace/other.md", "pull")
    from storage.files import file_tombstones_store
    monkeypatch.setattr(file_tombstones_store, "drop", lambda *a: None)
    await _run_sync(tmp_path, monkeypatch, [restore, plain], cm)
    agent_dir = tmp_path / "test-agent" / "workspace"
    assert os.stat(agent_dir / "notes.md").st_mtime == 1000.0
    assert os.stat(agent_dir / "other.md").st_mtime > 1000.0


@pytest.mark.asyncio
async def test_a_merge_push_expects_the_manifests_copy_and_a_stale_answer_keeps_the_hash(
    tmp_path, monkeypatch, caplog,
):
    """The frame's expectation is the machine's own stat from the manifest
    (its float mtime back in ns), "live" for a file it did not list. A push
    the machine refused as stale is counted apart, keeps the cached hash and
    marks the platform copy ahead there with the machine's stat as the
    baseline."""
    from collections import OrderedDict

    from core.remote import file_sync, remote_file_flow
    from storage.files import sync_state_store
    monkeypatch.setattr(file_sync, "_HASH_CACHE", OrderedDict())
    monkeypatch.setattr(sync_state_store, "record_one", lambda *a: None)
    remote_file_flow._platform_ahead.clear()
    cm, _ = _make_cm({})
    cm.send_command = AsyncMock(return_value={"files": [
        {"path": "workspace/busy.txt", "hash": "sha256:" + "9" * 64, "size": 7,
         "mtime": 1700000000.123456},
    ]})
    expects: dict[str, object] = {}

    async def _push(machine_id, ref, content, agent_slug="", **kw):
        expects[ref.value] = kw.get("expect")
        if ref.value == "workspace/busy.txt":
            kw["stale_out"].update(reason="live", size=9, mtime_ns=1_700_000_001_000_000_000)
            return False
        return True
    cm.push_file = _push
    actions = [_action("push", "workspace/busy.txt", base_hash="sha256:" + "1" * 64),
               _action("push", "workspace/new.txt", base_hash="sha256:" + "2" * 64)]
    try:
        with caplog.at_level("INFO", logger="remote-layer"):
            await _run_sync(tmp_path, monkeypatch, actions, cm, manifest_fn=file_sync.compute_manifest)
        assert expects["workspace/new.txt"] == "live"
        busy = expects["workspace/busy.txt"]
        assert busy["size"] == 7 and abs(busy["mtime_ns"] - 1_700_000_000_123_456_000) < 1_000
        assert {k.rsplit("/", 1)[-1] for k in file_sync._HASH_CACHE} == {"busy.txt", "new.txt"}
        assert "stale=1" in caplog.text and "Push of workspace/busy.txt held" in caplog.text
        mark = remote_file_flow._platform_ahead[("machine-1", "test-agent", "workspace/busy.txt")]
        st = (tmp_path / "test-agent" / "workspace" / "busy.txt").stat()
        assert (mark["size"], mark["mtime_ns"]) == (st.st_size, st.st_mtime_ns)
        assert mark["machine"] == {"exists": True, "size": 9, "mtime_ns": 1_700_000_001_000_000_000}
    finally:
        remote_file_flow._platform_ahead.clear()


def _stale_push_cm():
    """A cm whose every push the machine refuses as stale and whose pull
    writes the machine's bytes."""
    cm, _ = _make_cm({})

    async def _push(machine_id, ref, content, agent_slug="", **kw):
        kw["stale_out"].update(reason="live", size=9, mtime_ns=1_700_000_001_000_000_000)
        return False
    cm.push_file = _push

    async def _pull(machine_id, ref, dest, agent_slug="", **kw):
        from pathlib import Path
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"the machine's bytes")  # noqa: ASYNC240  # a test's own temporary files
        return True
    cm.pull_file_to_path = _pull
    return cm


@pytest.mark.asyncio
async def test_a_capture_before_a_push_the_machine_refuses_is_undone(tmp_path, monkeypatch):
    """The merge copies the losing machine copy to the recover bin before a
    push; the machine then refuses the push as stale, so nothing was
    replaced: the entry goes and its owner is not told their version was
    replaced."""
    from core.remote import remote_file_flow
    from storage.files import recover_bin_store, sync_state_store
    remote_file_flow._platform_ahead.clear()
    monkeypatch.setattr(sync_state_store, "record_one", lambda *a: None)
    captured, deleted = [], []
    monkeypatch.setattr(
        recover_bin_store, "capture",
        lambda agent, rp, data, reason: captured.append((rp, reason))
        or {"entry_id": "e-1", "original_name": "busy.txt"})
    monkeypatch.setattr(recover_bin_store, "delete", deleted.append)
    notify = AsyncMock()
    monkeypatch.setattr(RemoteExecutionLayer, "_notify_conflict_losers", notify)
    cm = _stale_push_cm()
    actions = [_action("push", "workspace/busy.txt", base_hash="sha256:" + "1" * 64,
                       capture_side="satellite", capture_reason="conflict", notify_user="bob")]
    try:
        await _run_sync(tmp_path, monkeypatch, actions, cm)
    finally:
        remote_file_flow._platform_ahead.clear()
    assert captured == [("workspace/busy.txt", "conflict")]
    assert deleted == ["e-1"]
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_merge_pull_that_settles_a_refused_save_keeps_the_save(tmp_path, monkeypatch):
    """The platform copy is a save the machine refused as stale; this merge
    takes the machine's copy for the path. The save goes to the recover bin
    as a conflict and its author is told, whatever the merge's own capture
    rule said for the path; the pull clears the mark."""
    from core.remote import remote_file_flow
    from storage.files import file_author_store, recover_bin_store
    remote_file_flow._platform_ahead.clear()
    captured = []
    monkeypatch.setattr(
        recover_bin_store, "capture",
        lambda agent, rp, data, reason: captured.append((rp, data, reason))
        or {"entry_id": "e-2", "original_name": "notes.md"})
    monkeypatch.setattr(file_author_store, "get", lambda agent, rp: "alice")
    notify = AsyncMock()
    monkeypatch.setattr(RemoteExecutionLayer, "_notify_conflict_losers", notify)
    cm = _stale_push_cm()
    saved = tmp_path / "test-agent" / "workspace" / "notes.md"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"the person's save")
    st = saved.stat()
    remote_file_flow.note_platform_ahead("machine-1", "test-agent", "workspace/notes.md",
                                         st.st_size, st.st_mtime_ns,
                                         machine={"exists": True, "size": 9, "mtime_ns": 1},
                                         stale=True)
    # A same-user divergence: the merge's own rule captures nothing.
    actions = [_action("pull", "workspace/notes.md"), _action("pull", "workspace/plain.md")]
    try:
        await _run_sync(tmp_path, monkeypatch, actions, cm)
        assert captured == [("workspace/notes.md", b"the person's save", "conflict")]
        assert saved.read_bytes() == b"the machine's bytes"
        notify.assert_awaited_once()
        assert notify.await_args.args[1] == [("alice", "notes.md")]
        assert not remote_file_flow.stale_hold("machine-1", "test-agent", "workspace/notes.md",
                                               platform_hash="sha256:save", base_hash=None)
    finally:
        remote_file_flow._platform_ahead.clear()


def _refused_save_rig(tmp_path, monkeypatch):
    """A platform copy whose push the machine refused as stale (the mark
    stands), a pull of the machine's copy planned, the bin and the notice
    recorded."""
    from core.remote import remote_file_flow
    from storage.files import file_author_store, recover_bin_store
    remote_file_flow._platform_ahead.clear()
    captured: list = []
    monkeypatch.setattr(
        recover_bin_store, "capture",
        lambda agent, rp, data, reason: captured.append((rp, data, reason))
        or {"entry_id": "e-3", "original_name": "notes.md"})
    monkeypatch.setattr(file_author_store, "get", lambda agent, rp: "alice")
    notify = AsyncMock()
    monkeypatch.setattr(RemoteExecutionLayer, "_notify_conflict_losers", notify)
    saved = tmp_path / "test-agent" / "workspace" / "notes.md"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"the platform copy")
    st = saved.stat()
    remote_file_flow.note_platform_ahead("machine-1", "test-agent", "workspace/notes.md",
                                         st.st_size, st.st_mtime_ns,
                                         machine={"exists": True, "size": 9, "mtime_ns": 1},
                                         stale=True)
    return captured, notify, saved


@pytest.mark.asyncio
async def test_a_change_after_the_copy_both_sides_held_is_no_conflict(tmp_path, monkeypatch):
    """The machine refused a push once, then the two copies agreed (a merge
    found them equal, or the machine's own write reached the platform) and the
    mark stayed. The machine then changed the file: the platform copy is that
    machine's base, so the two saves followed each other. The pull lands,
    nothing goes to the recover bin and nobody is told."""
    from core.remote import remote_file_flow
    captured, notify, saved = _refused_save_rig(tmp_path, monkeypatch)
    try:
        await _run_sync(tmp_path, monkeypatch, [_action("pull", "workspace/notes.md")],
                        _stale_push_cm(),
                        base_fn=lambda *a: {"workspace/notes.md": ("sha256:listed", 0.0)})
        assert captured == []
        notify.assert_not_awaited()
        assert saved.read_bytes() == b"the machine's bytes"
    finally:
        remote_file_flow._platform_ahead.clear()


@pytest.mark.asyncio
async def test_two_changes_since_the_last_common_copy_keep_the_save_and_tell_its_author(
        tmp_path, monkeypatch):
    """The save the machine refused is not the machine's base: both sides
    changed since the copy they last held in common. The save goes to the
    recover bin and its author is told, as before."""
    from core.remote import remote_file_flow
    captured, notify, saved = _refused_save_rig(tmp_path, monkeypatch)
    try:
        await _run_sync(tmp_path, monkeypatch, [_action("pull", "workspace/notes.md")],
                        _stale_push_cm(),
                        base_fn=lambda *a: {"workspace/notes.md": ("sha256:older", 0.0)})
        assert captured == [("workspace/notes.md", b"the platform copy", "conflict")]
        notify.assert_awaited_once()
        assert notify.await_args.args[1] == [("alice", "notes.md")]
        assert saved.read_bytes() == b"the machine's bytes"
    finally:
        remote_file_flow._platform_ahead.clear()


# --- the reconnect gate's slot is lent -------------------------------------


@pytest.mark.asyncio
async def test_merges_that_wait_on_their_machines_give_their_slots_back(monkeypatch):
    """Four background merges whose machines do not answer held the fleet's
    four slots for as long as they ran. A slot is lent: after the lend they
    go on without it and the merge of a machine that answers gets one."""
    import asyncio

    from core.remote import remote_workspace_sync as rws
    monkeypatch.setattr(rws, "_GATE_LEND_S", 0.1)
    monkeypatch.setattr(rws, "_unslotted", [0])
    rws._gates.pop("reconnect", None)
    stuck = asyncio.Event()

    async def slow_merge():
        async with rws._background_slot():
            await stuck.wait()

    slow = [asyncio.ensure_future(slow_merge()) for _ in range(rws._RECONNECT_SYNC_CONCURRENCY)]
    await asyncio.sleep(0.02)
    assert rws._reconnect_gate().locked()

    async def healthy_merge():
        async with rws._background_slot():
            return True

    assert await asyncio.wait_for(healthy_merge(), 2) is True
    assert rws._unslotted[0] == 4
    stuck.set()
    await asyncio.gather(*slow)
    assert rws._unslotted[0] == 0 and rws._reconnect_gate()._value == rws._RECONNECT_SYNC_CONCURRENCY
    rws._gates.pop("reconnect", None)


@pytest.mark.asyncio
async def test_a_long_merge_keeps_its_slot_when_no_place_without_one_is_free(monkeypatch):
    import asyncio

    from core.remote import remote_workspace_sync as rws
    monkeypatch.setattr(rws, "_GATE_LEND_S", 0.08)
    monkeypatch.setattr(rws, "_UNSLOTTED_MERGES", 1)
    monkeypatch.setattr(rws, "_unslotted", [0])
    rws._gates.pop("reconnect", None)
    stuck = asyncio.Event()

    async def slow_merge():
        async with rws._background_slot():
            await stuck.wait()

    slow = [asyncio.ensure_future(slow_merge()) for _ in range(rws._RECONNECT_SYNC_CONCURRENCY)]
    await asyncio.sleep(0.3)
    # One went on without a slot, the other three keep theirs: 3 + 1 run.
    assert rws._unslotted[0] == 1 and rws._reconnect_gate()._value == 1
    stuck.set()
    await asyncio.gather(*slow)
    assert rws._unslotted[0] == 0 and rws._reconnect_gate()._value == rws._RECONNECT_SYNC_CONCURRENCY
    rws._gates.pop("reconnect", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [True, False])
async def test_a_machine_many_people_share_reaches_the_merge_as_such(tmp_path, monkeypatch, shared):
    seen: dict = {}

    def _diff(*a, **kw):
        seen.update(kw)
        return SimpleNamespace(actions=[], to_scrub=[], left_unread=[])
    cm, _state = _make_cm({})
    await _run_sync(tmp_path, monkeypatch, [], cm, diff_fn=_diff, shared_by_many=shared)
    assert seen["shared_by_many"] is shared


@pytest.mark.asyncio
async def test_a_merge_push_sends_the_platform_copy_it_opened_never_a_name(tmp_path, monkeypatch):
    """The plan listed regular files; what stands at a name when its push runs
    is opened beneath the agents root with no link followed, and the push is
    handed that descriptor. A link at the name or on its way, or a pipe, is
    nothing to push: never read through, never waited on, no mark left."""
    import stat as _stat

    from core.remote import remote_file_flow
    from storage.files import sync_state_store
    monkeypatch.setattr(sync_state_store, "record_one", lambda *a: None)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"stand-in bytes")
    ws = tmp_path / "test-agent" / "workspace"
    ws.mkdir(parents=True)
    (ws / "honest.txt").write_bytes(b"honest bytes")
    (ws / "linked.txt").symlink_to(outside / "secret.txt")
    (ws / "sub").symlink_to(outside)
    os.mkfifo(ws / "pipe.txt")
    cm, _ = _make_cm({})
    sent: dict = {}

    async def _push(machine_id, ref, content, agent_slug="", **kw):
        fd = os.open(content, os.O_RDONLY | os.O_NONBLOCK)
        try:
            data = os.read(fd, 1024) if _stat.S_ISREG(os.fstat(fd).st_mode) else b""
        finally:
            os.close(fd)
        sent[ref.value] = (str(content), data)
        return True
    cm.push_file = _push
    actions = [_action("push", rp, base_hash="sha256:" + "1" * 64)
               for rp in ("workspace/honest.txt", "workspace/linked.txt",
                          "workspace/sub/secret.txt", "workspace/pipe.txt")]
    await asyncio.wait_for(_run_sync(tmp_path, monkeypatch, actions, cm), timeout=20)
    assert set(sent) == {"workspace/honest.txt"}
    source, data = sent["workspace/honest.txt"]
    assert source.startswith(("/proc/self/fd/", "/dev/fd/")) and data == b"honest bytes"
    assert not any(k[0] == "machine-1" for k in remote_file_flow._platform_ahead)


@pytest.mark.asyncio
async def test_a_merge_push_of_an_agent_folder_moved_behind_a_link_still_goes(tmp_path, monkeypatch):
    """An operator who moved an agent's folder to another disk behind a link
    keeps its merge: the push opens beneath the folder the manifest walked."""
    from storage.files import sync_state_store
    monkeypatch.setattr(sync_state_store, "record_one", lambda *a: None)
    moved = tmp_path / "disk2" / "test-agent"
    (moved / "workspace").mkdir(parents=True)
    (moved / "workspace" / "doc.txt").write_bytes(b"moved bytes")
    (tmp_path / "test-agent").symlink_to(moved)
    cm, _ = _make_cm({})
    sent: dict = {}

    async def _push(machine_id, ref, content, agent_slug="", **kw):
        sent[ref.value] = Path(content).read_bytes()  # noqa: ASYNC240  # a test's own temporary files
        return True
    cm.push_file = _push
    await _run_sync(tmp_path, monkeypatch, [_action("push", "workspace/doc.txt", base_hash="sha256:" + "1" * 64)], cm)
    assert sent == {"workspace/doc.txt": b"moved bytes"}

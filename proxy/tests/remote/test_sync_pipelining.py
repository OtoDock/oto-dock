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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

from core.remote.remote_execution import RemoteExecutionLayer  # noqa: E402


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
    from storage.files import sync_state_store
    from storage.files import file_tombstones_store

    agent_dir = tmp_path / "test-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    for a in actions:
        p = agent_dir / a.rel_path
        if os.path.lexists(p):
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"content-" + a.rel_path.encode())

    monkeypatch.setattr(_cfg, "AGENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(visibility, "is_shared_only", lambda slug: False)
    monkeypatch.setattr(file_sync, "compute_manifest",
                        manifest_fn or (lambda *a, **kw: []))
    monkeypatch.setattr(sync_state_store, "load_for_machine_agent",
                        lambda *a: {})
    monkeypatch.setattr(file_tombstones_store, "load_for_agent", lambda *a: {})
    monkeypatch.setattr(
        file_sync, "diff_manifests",
        diff_fn or (lambda *a, **kw: SimpleNamespace(actions=actions, to_scrub=[])),
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
    from core.remote import transfer_gate
    from services.remote import workspace_fanout
    from core.remote import satellite_connection as sc
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

        async with asyncio.timeout(10):
            await asyncio.gather(
                _run_sync(tmp_path, monkeypatch, actions, cm),
                workspace_fanout.fan_out_write(
                    "agent-live", "workspace/big.bin", b"x" * (600 * 1024),
                ),
            )
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
        return SimpleNamespace(actions=[], to_scrub=[])

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
                  notify_user="", base_hash="", drop_tombstone=False, clear_base=False)
    fields.update(kw)
    return SimpleNamespace(**fields)


def _record_delete_bookkeeping(monkeypatch):
    from services.notifications import notification_manager
    from services.remote import workspace_fanout
    from storage.files import file_author_store, file_tombstones_store
    from storage.files import recover_bin_store, sync_state_store
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
    assert not os.path.lexists(ws / "doc.txt")
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
    cm.effective_sync_cap = MagicMock(return_value=1 << 30)
    cm.effective_ignore_rules = MagicMock(return_value=None)

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

"""Tests for multi-user shared-workspace sync (fan-out + live versioned merge).

Covers:
- ``workspace_fanout.fanout_targets`` — active-session target selection +
  per-user/per-role isolation + source-machine exclusion + dedupe + fail-closed.
- ``workspace_fanout.fan_out_write`` / ``fan_out_delete`` — push/delete shapes.
- ``satellite_connection._apply_file_changed`` — the live path: cross-user
  clobber capture + base/author advance + delete tombstone (versioned LWW).
"""

import asyncio
import base64
import hashlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core import placement
from core.remote import remote_workspace_apply
from core.session import interactive_registry


def _h(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


@pytest.fixture(autouse=True)
def _no_platform_ahead_marks():
    # A fan-out marks its targets (remote_file_flow's platform-ahead map):
    # none may outlive a test into another module's reads.
    from core.remote import remote_file_flow
    remote_file_flow._platform_ahead.clear()
    yield
    remote_file_flow._platform_ahead.clear()


@pytest.fixture(autouse=True)
def _no_save_fan_out_left():
    # A save's fan-out runs after the save answered: each test that starts
    # one waits for it (``drain_save_fan_outs``); none may leave its queue
    # behind for the next test.
    # A contended path or fan-out lock binds to its test's event loop.
    from core.remote import remote_file_flow
    from services.remote import workspace_fanout as wf
    remote_file_flow._global_path_locks.clear()
    remote_file_flow._fanout_locks.clear()
    yield
    assert not wf._save_fan_out_tasks, "a test left a save's fan-out running"
    wf._queued_save_fan_outs.clear()
    remote_file_flow._global_path_locks.clear()
    remote_file_flow._fanout_locks.clear()


@pytest.fixture(autouse=True)
def _lanes(monkeypatch, tmp_path_factory):
    # A fan-out hands each target a file name and returns: the machine's
    # lane (``core/remote/machine_push_lane``) pushes the platform copy.
    # Every test starts with no lane and an agents root of its own.
    import config
    from core.remote import machine_push_lane
    machine_push_lane.reset()
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path_factory.mktemp("agents"), raising=False)
    yield
    machine_push_lane.reset()


def _cm() -> AsyncMock:
    """A connection manager stand-in a lane can drive: every machine reads
    as connected."""
    cm = AsyncMock()
    cm.is_connected = MagicMock(return_value=True)
    cm.get_connection = MagicMock(return_value=object())
    cm.get_connected_machines = MagicMock(return_value=[])
    cm.stat_file = AsyncMock(return_value=None)
    cm.push_file.return_value = True
    return cm


async def _fan(agent, rel, source, **kw):
    """``fan_out_write`` and the lanes' pushes to their end. Bytes are what
    the caller wrote to the platform copy first: written here when the test
    did not."""
    import config
    from services.remote import workspace_fanout
    if isinstance(source, (bytes, bytearray)):
        copy = config.AGENTS_DIR / agent / rel
        if not copy.exists():
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(source)
    write = workspace_fanout.fan_out_write
    await write(agent, rel, source, **kw)
    assert await workspace_fanout.drain_fan_outs()


# ---------------------------------------------------------------------------
# fanout_targets — selection + isolation
# ---------------------------------------------------------------------------


class _FakeInfo:
    def __init__(self, machine_id, agent_name, alive=True):
        self.machine_id = machine_id
        self.agent_name = agent_name
        self.alive = alive


class _FakeLayer:
    def __init__(self, sessions):
        self._sessions = sessions


def _setup_layer(monkeypatch, sessions, secs):
    """sessions: {sid: _FakeInfo}; secs: {sid: SimpleNamespace|None}."""
    import core.session.session_manager as sm
    import core.session.session_state as ss
    monkeypatch.setattr(sm, "_get_remote_layer", lambda: _FakeLayer(sessions))
    monkeypatch.setattr(ss, "get_session_security", secs.get)


def _sec(username, role):
    return SimpleNamespace(placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="mBob"), username=username, role=role)


def test_fanout_excludes_source_machine(temp_db, monkeypatch):
    _setup_layer(
        monkeypatch,
        {"s1": _FakeInfo("mA", "agent-1"), "s2": _FakeInfo("mB", "agent-1")},
        {"s1": _sec("alice", "manager"), "s2": _sec("bob", "editor")},
    )
    from services.remote.workspace_fanout import fanout_targets
    out = fanout_targets("agent-1", "workspace/x.md", exclude_machine_id="mA")
    assert out == ["mB"]


def test_fanout_filters_by_agent(temp_db, monkeypatch):
    _setup_layer(
        monkeypatch,
        {"s1": _FakeInfo("mA", "agent-1"), "s2": _FakeInfo("mB", "other")},
        {"s1": _sec("alice", "manager"), "s2": _sec("bob", "manager")},
    )
    from services.remote.workspace_fanout import fanout_targets
    assert fanout_targets("agent-1", "workspace/x.md") == ["mA"]


def test_fanout_skips_dead_sessions(temp_db, monkeypatch):
    _setup_layer(
        monkeypatch,
        {"s1": _FakeInfo("mA", "agent-1", alive=False)},
        {"s1": _sec("alice", "manager")},
    )
    from services.remote.workspace_fanout import fanout_targets
    assert fanout_targets("agent-1", "workspace/x.md") == []


def test_fanout_isolation_other_user_excluded(temp_db, monkeypatch):
    _setup_layer(
        monkeypatch,
        {"s1": _FakeInfo("mB", "agent-1")},
        {"s1": _sec("bob", "editor")},
    )
    from services.remote.workspace_fanout import fanout_targets
    assert fanout_targets("agent-1", "users/alice/x.md") == []  # other user
    assert fanout_targets("agent-1", "users/bob/x.md") == ["mB"]  # own dir


def test_fanout_isolation_config_non_owner_excluded(temp_db, monkeypatch):
    _setup_layer(
        monkeypatch,
        {"s1": _FakeInfo("mV", "agent-1")},
        {"s1": _sec("vic", "viewer")},
    )
    from services.remote.workspace_fanout import fanout_targets
    assert fanout_targets("agent-1", "config/p.md") == []      # viewer ≠ owner
    assert fanout_targets("agent-1", "workspace/p.md") == ["mV"]


def test_fanout_dedupes_machine(temp_db, monkeypatch):
    _setup_layer(
        monkeypatch,
        {"s1": _FakeInfo("mA", "agent-1"), "s2": _FakeInfo("mA", "agent-1")},
        {"s1": _sec("alice", "viewer"), "s2": _sec("bob", "viewer")},
    )
    from services.remote.workspace_fanout import fanout_targets
    assert fanout_targets("agent-1", "workspace/x.md") == ["mA"]


def test_fanout_machine_included_if_any_session_allowed(temp_db, monkeypatch):
    # Same machine, viewer (config denied) + manager (config allowed) → included.
    _setup_layer(
        monkeypatch,
        {"s1": _FakeInfo("mA", "agent-1"), "s2": _FakeInfo("mA", "agent-1")},
        {"s1": _sec("vic", "viewer"), "s2": _sec("mgr", "manager")},
    )
    from services.remote.workspace_fanout import fanout_targets
    assert fanout_targets("agent-1", "config/p.md") == ["mA"]


def test_fanout_failclosed_no_security(temp_db, monkeypatch):
    _setup_layer(
        monkeypatch,
        {"s1": _FakeInfo("mA", "agent-1")},
        {"s1": None},  # no authenticated context → fail-closed
    )
    from services.remote.workspace_fanout import fanout_targets
    assert fanout_targets("agent-1", "workspace/x.md") == []


def _setup_interactive(monkeypatch, sessions):
    """sessions: {sid: SimpleNamespace(agent_name, alive, target, username, role)}"""
    from core.session import interactive_session as isess
    monkeypatch.setattr(isess, "_sessions", sessions)
    monkeypatch.setattr(interactive_registry, "_sessions", sessions)


def test_fanout_includes_remote_interactive_sessions(temp_db, monkeypatch):
    # A machine running ONLY a TUI (PTY) session still receives live pushes —
    # identity from the interactive registry, same isolation predicate. Local
    # PTY sessions (target="local") run on the platform tree → excluded.
    _setup_layer(monkeypatch, {}, {})
    from types import SimpleNamespace as NS
    _setup_interactive(monkeypatch, {
        "i1": NS(agent_name="agent-1", alive=True, target="mI",
                 username="alice", role="manager"),
        "i2": NS(agent_name="agent-1", alive=True, target="local",
                 username="alice", role="manager"),
        "i3": NS(agent_name="agent-1", alive=False, target="mDead",
                 username="alice", role="manager"),
    })
    from services.remote.workspace_fanout import _active_machine_ids, fanout_targets
    assert fanout_targets("agent-1", "config/p.md") == ["mI"]  # owner-tier
    assert _active_machine_ids("agent-1") == {"mI"}


def test_fanout_interactive_isolation_and_active(temp_db, monkeypatch):
    # A viewer's TUI machine is excluded from config/ pushes by the same
    # predicate as headless sessions — but still counts ACTIVE, so the idle
    # fingerprint sweep never merges against its live, moving tree.
    _setup_layer(monkeypatch, {}, {})
    from types import SimpleNamespace as NS
    _setup_interactive(monkeypatch, {
        "i1": NS(agent_name="agent-1", alive=True, target="mI",
                 username="vic", role="viewer"),
    })
    from services.remote.workspace_fanout import _active_machine_ids, fanout_targets
    assert fanout_targets("agent-1", "config/p.md") == []
    assert fanout_targets("agent-1", "workspace/p.md") == ["mI"]
    assert _active_machine_ids("agent-1") == {"mI"}


def test_fanout_no_layer(temp_db, monkeypatch):
    import core.session.session_manager as sm

    def _raise():
        raise ImportError("the remote layer cannot load")

    monkeypatch.setattr(sm, "_get_remote_layer", _raise)
    from services.remote.workspace_fanout import fanout_targets
    assert fanout_targets("agent-1", "workspace/x.md") == []


# ---------------------------------------------------------------------------
# fan_out_write / fan_out_delete — push shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fan_out_write_pushes_to_targets(temp_db, monkeypatch):
    from services.remote import workspace_fanout
    monkeypatch.setattr(
        workspace_fanout, "fanout_targets",
        lambda a, r, *, exclude_machine_id=None: ["m1", "m2"],
    )
    fake_cm = _cm()
    pushed: dict[str, bytes] = {}

    async def push(mid, ref, source, **kw):
        with open(source, "rb") as fh:  # noqa: ASYNC230  # a fake push reads the descriptor it was handed
            pushed[mid] = fh.read()
        return True
    fake_cm.push_file.side_effect = push
    from core.remote import satellite_connection as sc
    monkeypatch.setattr(sc, "get_connection_manager", lambda: fake_cm)

    await _fan("agent-1", "workspace/x.md", b"data")

    assert pushed == {"m1": b"data", "m2": b"data"}
    for call in fake_cm.push_file.await_args_list:
        mid, ref = call.args[:2]
        assert ref.kind == "agent_tree" and ref.value == "workspace/x.md"
        assert call.kwargs.get("agent_slug") == "agent-1"
        assert call.kwargs.get("content_hash") == _h(b"data")


@pytest.mark.asyncio
async def test_a_target_the_fan_out_missed_keeps_the_platform_ahead(temp_db, tmp_path, monkeypatch):
    import config
    from core.remote import remote_file_flow
    from services.remote import workspace_fanout
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    target = tmp_path / "agent-1" / "workspace" / "x.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"data")
    monkeypatch.setattr(
        workspace_fanout, "fanout_targets",
        lambda a, r, *, exclude_machine_id=None: ["m1", "m2", "m3"],
    )
    fake_cm = _cm()
    fake_cm.stat_file = AsyncMock(return_value={"exists": True, "size": 1, "mtime_ns": 5})
    seen_during: dict[str, dict] = {}

    async def push(mid, *a, **kw):
        # While its push runs, a target is marked: a read there serves the
        # platform copy.
        seen_during[mid] = dict(remote_file_flow._platform_ahead[(mid, "agent-1", "workspace/x.md")])
        if mid == "m3":
            raise RuntimeError("socket gone")
        return mid == "m1"
    fake_cm.push_file.side_effect = push
    from core.remote import satellite_connection as sc
    monkeypatch.setattr(sc, "get_connection_manager", lambda: fake_cm)
    remote_file_flow._pull_stat_records.clear()
    try:
        await _fan("agent-1", "workspace/x.md", b"data")
        st = target.stat()
        assert all(m["in_flight_until"] > 0 for m in seen_during.values()) and len(seen_during) == 3
        assert set(remote_file_flow._platform_ahead) == {
            ("m2", "agent-1", "workspace/x.md"), ("m3", "agent-1", "workspace/x.md"),
        }
        for marker in remote_file_flow._platform_ahead.values():
            assert (marker["size"], marker["mtime_ns"], marker["in_flight_until"]) == (st.st_size, st.st_mtime_ns, 0.0)
            assert marker["machine"] == {"exists": True, "size": 1, "mtime_ns": 5}
    finally:
        remote_file_flow._pull_stat_records.clear()


@pytest.mark.asyncio
async def test_the_in_flight_mark_is_set_again_at_the_transfer_slot(temp_db, tmp_path, monkeypatch):
    import config
    from core.remote import remote_file_flow
    from services.remote import workspace_fanout
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    target = tmp_path / "agent-1" / "workspace" / "x.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"data")
    monkeypatch.setattr(
        workspace_fanout, "fanout_targets",
        lambda a, r, *, exclude_machine_id=None: ["m1"],
    )
    marks: list[str] = []
    real = remote_file_flow.note_platform_ahead

    def note(mid, *a, **kw):
        if kw.get("in_flight_s"):
            marks.append(mid)
        return real(mid, *a, **kw)
    monkeypatch.setattr(remote_file_flow, "note_platform_ahead", note)
    fake_cm = _cm()
    fake_cm.push_file = AsyncMock(return_value=True)
    from core.remote import satellite_connection as sc
    monkeypatch.setattr(sc, "get_connection_manager", lambda: fake_cm)
    await _fan("agent-1", "workspace/x.md", b"data")
    assert marks == ["m1"]  # at the transfer slot, for as long as the push may run
    assert ("m1", "agent-1", "workspace/x.md") not in remote_file_flow._platform_ahead


@pytest.mark.asyncio
async def test_a_fan_out_of_older_bytes_leaves_a_newer_writes_mark(temp_db, tmp_path, monkeypatch):
    # While the fan-out of B1 is on its way to m1 and m2, m1's and m2's own
    # session writes B2 and its push to them fails (a B2 mark on each). The
    # B1 fan-out then acks on m1 and fails on m2: neither clears nor
    # overwrites the B2 mark, so a read there keeps serving B2.
    import config
    from core.remote import remote_file_flow
    from services.remote import workspace_fanout
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    target = tmp_path / "agent-1" / "workspace" / "x.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"B1")
    monkeypatch.setattr(
        workspace_fanout, "fanout_targets",
        lambda a, r, *, exclude_machine_id=None: ["m1", "m2"],
    )
    fake_cm = _cm()
    fake_cm.stat_file = AsyncMock(return_value={"exists": True, "size": 2, "mtime_ns": 5})
    newer: dict[str, tuple[int, int]] = {}

    async def push(mid, *a, **kw):
        target.write_bytes(b"B2 bytes")
        st = target.stat()
        newer[mid] = (st.st_size, st.st_mtime_ns)
        remote_file_flow.note_platform_ahead(mid, "agent-1", "workspace/x.md", *newer[mid])
        if mid == "m2":
            return False
        return True
    fake_cm.push_file.side_effect = push
    from core.remote import satellite_connection as sc
    monkeypatch.setattr(sc, "get_connection_manager", lambda: fake_cm)
    remote_file_flow._pull_stat_records.clear()
    try:
        await _fan("agent-1", "workspace/x.md", b"B1")
        for mid in ("m1", "m2"):
            marker = remote_file_flow._platform_ahead[(mid, "agent-1", "workspace/x.md")]
            assert (marker["size"], marker["mtime_ns"]) == newer[mid]
    finally:
        remote_file_flow._pull_stat_records.clear()
        remote_file_flow._platform_ahead.clear()


@pytest.mark.asyncio
async def test_fan_out_delete_broadcasts(temp_db, monkeypatch):
    from services.remote import workspace_fanout
    monkeypatch.setattr(
        workspace_fanout, "fanout_targets",
        lambda a, r, *, exclude_machine_id=None: ["m1"],
    )
    fake_cm = _cm()
    from core.remote import satellite_connection as sc
    monkeypatch.setattr(sc, "get_connection_manager", lambda: fake_cm)

    await workspace_fanout.fan_out_delete("agent-1", "workspace/x.md")
    assert await workspace_fanout.drain_fan_outs()

    assert fake_cm.send_fire_and_forget.await_count == 1
    msg = fake_cm.send_fire_and_forget.await_args.args[1]
    assert msg == {
        "type": "file_push", "agent_slug": "agent-1",
        "action": "delete", "path": "workspace/x.md",
    }


@pytest.mark.asyncio
async def test_fan_out_write_no_targets_is_noop(temp_db, monkeypatch):
    from services.remote import workspace_fanout
    monkeypatch.setattr(
        workspace_fanout, "fanout_targets",
        lambda a, r, *, exclude_machine_id=None: [],
    )
    fake_cm = _cm()
    from core.remote import satellite_connection as sc
    monkeypatch.setattr(sc, "get_connection_manager", lambda: fake_cm)

    await _fan("agent-1", "workspace/x.md", b"d")
    assert fake_cm.push_file.await_count == 0


# ---------------------------------------------------------------------------
# propagate_write — atomic write + fan-out under the global lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propagate_write_persists_and_fans_out_after_the_answer(temp_db, tmp_path, monkeypatch):
    """propagate_write atomically writes the bytes to the agent tree, marks
    the targets in flight before it returns, and fans the platform copy out
    after it returned (source machine excluded)."""
    import config
    from core.remote import remote_file_flow
    from services.remote import workspace_fanout as wf
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    _setup_layer(monkeypatch, {"s1": _FakeInfo("mA", "agent-1"), "s2": _FakeInfo("mSrc", "agent-1")},
                 {"s1": _sec("", "manager"), "s2": _sec("", "manager")})

    fo = AsyncMock()
    monkeypatch.setattr(wf, "fan_out_write", fo)

    assert await wf.propagate_write(
        "agent-1", "workspace/x.md", b"hello", exclude_machine_id="mSrc",
    ) is True

    # Authoritative atomic write landed on the platform disk.
    target = tmp_path / "agent-1" / "workspace" / "x.md"
    assert target.read_bytes() == b"hello"
    # The target is marked in flight when the save answers; the fan-out has
    # not run yet.
    st = target.stat()
    mark = remote_file_flow._platform_ahead[("mA", "agent-1", "workspace/x.md")]
    assert (mark["size"], mark["mtime_ns"]) == (st.st_size, st.st_mtime_ns)
    assert mark["in_flight_until"] > 0
    assert ("mSrc", "agent-1", "workspace/x.md") not in remote_file_flow._platform_ahead
    fo.assert_not_awaited()

    await wf.drain_save_fan_outs()
    fo.assert_awaited_once()
    # The platform copy as a Path (opened when the fan-out runs), with the
    # marks it must settle.
    assert fo.await_args.args[:2] == ("agent-1", "workspace/x.md")
    assert fo.await_args.args[2] == tmp_path / "agent-1" / "workspace" / "x.md"
    assert fo.await_args.kwargs.get("exclude_machine_id") == "mSrc"
    assert fo.await_args.kwargs.get("marked") == {"mA": (st.st_size, st.st_mtime_ns)}


@pytest.mark.asyncio
async def test_a_save_with_no_target_schedules_nothing(temp_db, tmp_path, monkeypatch):
    import config
    from services.remote import workspace_fanout as wf
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    _setup_layer(monkeypatch, {}, {})
    fo = AsyncMock()
    monkeypatch.setattr(wf, "fan_out_write", fo)
    assert await wf.propagate_write("agent-1", "workspace/x.md", b"hello") is True
    assert not wf._save_fan_out_tasks and not wf._queued_save_fan_outs
    await wf.drain_save_fan_outs()
    fo.assert_not_awaited()


@pytest.mark.asyncio
async def test_propagate_write_creates_parent_dirs(temp_db, tmp_path, monkeypatch):
    """A fresh nested path has its parents created before fan-out."""
    import config
    from services.remote import workspace_fanout as wf
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(wf, "fan_out_write", AsyncMock())

    await wf.propagate_write("agent-1", "workspace/deep/new.txt", b"data")
    assert (tmp_path / "agent-1" / "workspace" / "deep" / "new.txt").read_bytes() == b"data"


# ---------------------------------------------------------------------------
# _apply_file_changed — conflict detection
# ---------------------------------------------------------------------------


def _patch_apply_deps(monkeypatch, *, sec, loser_sub_for, notifs):
    import config
    import core.session.session_state as ss
    import services.notifications.notification_manager as nm
    import storage.database as db
    from services.remote import workspace_fanout as wf
    monkeypatch.setattr(ss, "get_session_security", lambda sid: sec)
    monkeypatch.setattr(
        wf, "fanout_targets", lambda a, r, *, exclude_machine_id=None, shared_only=None: [])
    monkeypatch.setattr(db, "get_user_sub_by_username", loser_sub_for)
    monkeypatch.setattr(config, "RECOVER_BIN_DIR", config.AGENTS_DIR / "_recover-bin")

    async def _fire(**kw):
        notifs.append(kw)
        return []

    monkeypatch.setattr(nm, "fire_notification", _fire)


def _write_msg(agent, rel, content, session_id="sess-x"):
    return {
        "agent_slug": agent, "path": rel, "action": "write",
        "session_id": session_id,
        "content_b64": base64.b64encode(content).decode(),
        "hash": _h(content),
    }


def _delete_msg(agent, rel, session_id="sess-x"):
    return {"agent_slug": agent, "path": rel, "action": "delete", "session_id": session_id}


@pytest.mark.asyncio
async def test_apply_captures_conflict_on_cross_user_clobber(temp_db, tmp_path, monkeypatch):
    # Live path: the satellite overwrites a platform copy last written by a
    # DIFFERENT user that this machine never converged on → capture the loser to
    # the recover-bin (reason conflict) + notify the loser; advance base + author.
    import config
    from core.remote.satellite_connection import SatelliteConnectionManager
    from storage.files import file_author_store, recover_bin_store, sync_state_store

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    agent, rel = "agent-1", "workspace/shared.md"
    loser_bytes, winner_bytes = b"alice version", b"bob version"
    fpath = tmp_path / agent / rel
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_bytes(loser_bytes)
    file_author_store.record(agent, rel, "alice")  # platform copy is alice's

    cm = SatelliteConnectionManager()
    notifs = []
    _patch_apply_deps(
        monkeypatch,
        sec=SimpleNamespace(placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="mBob"), role="editor", username="bob", agent=agent, display_name="Bob"),
        loser_sub_for=lambda u: "user-alice" if u == "alice" else None,
        notifs=notifs,
    )

    await cm._apply_file_changed("mBob", _write_msg(agent, rel, winner_bytes, "sess-bob"))

    # Winner bytes on disk; base + author advanced to bob.
    assert fpath.read_bytes() == winner_bytes
    assert file_author_store.get(agent, rel) == "bob"
    assert sync_state_store.get_one("mBob", agent, rel)[0] == _h(winner_bytes)

    # Loser's bytes captured to the recover-bin (reason conflict).
    entries = recover_bin_store.list_for(agent, "admin", True, True, True)
    conflicts = [e for e in entries if e["rel_path"] == rel and e["reason"] == "conflict"]
    assert len(conflicts) == 1
    assert recover_bin_store.read_bytes(conflicts[0]) == loser_bytes

    # Notification to the loser (alice) — Recover-button style, NO download link.
    assert len(notifs) == 1 and notifs[0]["target"] == "user-alice"
    assert notifs[0]["source"] == "file_conflict"
    assert "shared.md" in notifs[0]["body"]
    assert "/backup" not in notifs[0]["body"] and "http" not in notifs[0]["body"]


@pytest.mark.asyncio
async def test_pre_overwrite_capture_never_reads_through_a_link(tmp_path, monkeypatch):
    """The bytes a conflict capture copies into the recover bin are read
    beneath the agents root, never by the checked name: a link swapped in
    after the check is refused, not followed."""
    from pathlib import Path

    from core.remote.satellite_connection import SatelliteConnectionManager

    agents = tmp_path / "agents"
    agent_dir = agents / "agent-1"
    (agent_dir / "workspace").mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"secret")
    (agent_dir / "workspace" / "shared.md").symlink_to(victim)
    real_resolve = Path.resolve

    def resolve_as_checked(self, *a, **kw):
        # The check window: the name looked like a plain in-tree file.
        if self.name == "shared.md":
            return agent_dir / "workspace" / "shared.md"
        return real_resolve(self, *a, **kw)
    monkeypatch.setattr(Path, "resolve", resolve_as_checked)
    cm = SatelliteConnectionManager()
    assert await cm._capture_pre_overwrite(agent_dir, "workspace/shared.md") == (None, None)
    (agent_dir / "workspace" / "plain.md").write_bytes(b"mine")
    data, digest = await cm._capture_pre_overwrite(agent_dir, "workspace/plain.md")
    assert data == b"mine" and digest == "sha256:" + hashlib.sha256(b"mine").hexdigest()


def test_the_merge_loser_read_never_reads_through_a_link(tmp_path):

    agents = tmp_path / "agents"
    agent_dir = agents / "agent-1"
    (agent_dir / "workspace").mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"secret")
    (agent_dir / "workspace" / "shared.md").symlink_to(victim)
    (agent_dir / "workspace" / "plain.md").write_bytes(b"mine")
    assert remote_workspace_apply._platform_bytes(agent_dir, "workspace/shared.md") is None
    assert remote_workspace_apply._platform_bytes(agent_dir, "workspace/plain.md") == b"mine"
    assert remote_workspace_apply._platform_bytes(agent_dir, "workspace/../../victim.txt") is None


@pytest.mark.asyncio
async def test_apply_same_user_no_conflict(temp_db, tmp_path, monkeypatch):
    import config
    from core.remote.satellite_connection import SatelliteConnectionManager
    from storage.files import file_author_store, recover_bin_store

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    agent, rel = "agent-1", "workspace/shared.md"
    old, new = b"v1 by bob", b"v2 by bob"
    fpath = tmp_path / agent / rel
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_bytes(old)
    file_author_store.record(agent, rel, "bob")  # platform copy is bob's own

    cm = SatelliteConnectionManager()
    notifs = []
    _patch_apply_deps(
        monkeypatch,
        sec=SimpleNamespace(placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="mBob"), role="editor", username="bob", agent=agent, display_name="Bob"),
        loser_sub_for=lambda u: "user-bob",
        notifs=notifs,
    )

    await cm._apply_file_changed("mBob", _write_msg(agent, rel, new, "sess-bob"))

    # Same user overwriting their own edit → no conflict, no capture.
    assert notifs == []
    assert file_author_store.get(agent, rel) == "bob"
    entries = recover_bin_store.list_for(agent, "admin", True, True, True)
    assert [e for e in entries if e["reason"] == "conflict"] == []


@pytest.mark.asyncio
async def test_the_applier_marks_its_targets_inside_the_path_lock(temp_db, tmp_path, monkeypatch):
    """The applier pushes after it released the path lock: its targets are
    marked in flight before that, and the fan-out settles the marks."""
    import config
    from core.remote import remote_file_flow
    from core.remote.satellite_connection import SatelliteConnectionManager
    from services.remote import workspace_fanout as wf

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    agent, rel = "agent-1", "workspace/shared.md"
    (tmp_path / agent / "workspace").mkdir(parents=True)
    cm = SatelliteConnectionManager()
    _patch_apply_deps(
        monkeypatch,
        sec=SimpleNamespace(placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="mBob"), role="editor", username="bob", agent=agent, display_name="Bob"),
        loser_sub_for=lambda u: "user-bob",
        notifs=[],
    )
    monkeypatch.setattr(
        wf, "fanout_targets", lambda a, r, *, exclude_machine_id=None, shared_only=None: ["mAlice"])
    seen = {}

    async def _mark(agent_slug, rel_path, *, exclude_machine_id=None):
        seen["path_lock_held"] = remote_file_flow._global_path_locks[(agent_slug, rel_path)].locked()
        seen["exclude"] = exclude_machine_id
        return {"mAlice": (4, 2)}

    fo = AsyncMock()
    monkeypatch.setattr(wf, "mark_fan_out_targets", _mark)
    monkeypatch.setattr(wf, "fan_out_write", fo)
    await cm._apply_file_changed("mBob", _write_msg(agent, rel, b"v2 by bob", "sess-bob"))
    assert seen == {"path_lock_held": True, "exclude": "mBob"}
    assert fo.await_args.kwargs["marked"] == {"mAlice": (4, 2)}


@pytest.mark.asyncio
async def test_apply_no_conflict_when_base_matches(temp_db, tmp_path, monkeypatch):
    """When this machine's converged base equals the on-disk hash, the write is a
    sequential edit (the machine SAW this version) → no conflict, even cross-user."""
    import config
    from core.remote.satellite_connection import SatelliteConnectionManager
    from storage.files import file_author_store, recover_bin_store, sync_state_store

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    agent, rel = "agent-1", "workspace/shared.md"
    seen, new = b"seen version", b"bob new"
    fpath = tmp_path / agent / rel
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_bytes(seen)
    file_author_store.record(agent, rel, "alice")
    sync_state_store.record_one("mBob", agent, rel, _h(seen), 1.0)  # base == on-disk

    cm = SatelliteConnectionManager()
    notifs = []
    _patch_apply_deps(
        monkeypatch,
        sec=SimpleNamespace(placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="mBob"), role="editor", username="bob", agent=agent, display_name="Bob"),
        loser_sub_for=lambda u: "user-alice",
        notifs=notifs,
    )

    await cm._apply_file_changed("mBob", _write_msg(agent, rel, new, "sess-bob"))

    assert notifs == []
    assert [e for e in recover_bin_store.list_for(agent, "admin", True, True, True)
            if e["reason"] == "conflict"] == []
    assert sync_state_store.get_one("mBob", agent, rel)[0] == _h(new)


@pytest.mark.asyncio
async def test_apply_delete_writes_tombstone_and_captures(temp_db, tmp_path, monkeypatch):
    # Live delete: write a tombstone (so idle satellites apply it), capture the
    # pre-delete bytes, and clear this machine's base + the author.
    import config
    from core.remote.satellite_connection import SatelliteConnectionManager
    from storage.files import (
        file_author_store,
        file_tombstones_store,
        recover_bin_store,
        sync_state_store,
    )

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    agent, rel = "agent-1", "workspace/doomed.md"
    fpath = tmp_path / agent / rel
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_bytes(b"bye")
    file_author_store.record(agent, rel, "bob")
    sync_state_store.record_one("mBob", agent, rel, _h(b"bye"), 1.0)

    cm = SatelliteConnectionManager()
    notifs = []
    _patch_apply_deps(
        monkeypatch,
        sec=SimpleNamespace(placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="mBob"), role="editor", username="bob", agent=agent, display_name="Bob"),
        loser_sub_for=lambda u: "user-bob",
        notifs=notifs,
    )

    await cm._apply_file_changed("mBob", _delete_msg(agent, rel, "sess-bob"))

    assert not fpath.exists()
    assert file_tombstones_store.get(agent, rel) is not None  # idle satellites apply it
    assert sync_state_store.get_one("mBob", agent, rel) is None  # base cleared
    assert file_author_store.get(agent, rel) is None  # author cleared
    entries = recover_bin_store.list_for(agent, "admin", True, True, True)
    assert any(e["rel_path"] == rel and e["reason"] == "deleted" for e in entries)


# --- A delete reported again: its record stays and nothing is sent on ---

_AGENT = "agent-1"


def _editor_on(machine_id, username):
    return SimpleNamespace(
        placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id=machine_id),
        role="editor", username=username, agent=_AGENT, display_name=username.title())


def _repeat_rig(monkeypatch, *, real_fan_out=False):
    """The live applier with ``sess-bob`` and ``sess-bob-2`` on ``mBob`` and
    ``sess-carol`` on ``mCarol``, all editors, and no fan-out target. Returns
    the delete's fan-out and the dashboard refresh, both recorded (the real
    fan-out with ``real_fan_out``)."""
    import core.session.session_state as ss
    import services.notifications.notification_manager as nm
    from services.remote import workspace_fanout as wf
    secs = {"sess-bob": _editor_on("mBob", "bob"), "sess-bob-2": _editor_on("mBob", "bob"),
            "sess-carol": _editor_on("mCarol", "carol")}
    _patch_apply_deps(monkeypatch, sec=None, loser_sub_for=lambda u: None, notifs=[])
    monkeypatch.setattr(ss, "get_session_security", secs.get)
    fan_out = AsyncMock(wraps=wf.fan_out_delete) if real_fan_out else AsyncMock()
    monkeypatch.setattr(wf, "fan_out_delete", fan_out)
    refreshed = AsyncMock()
    monkeypatch.setattr(nm, "broadcast_file_updated", refreshed)
    return fan_out, refreshed


def _agent_path(rel):
    import config
    return config.AGENTS_DIR / _AGENT / rel


def _platform_file(rel, data=b"bye"):
    path = _agent_path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _delete_frame(rel, session_id="sess-bob"):
    # The delete frame of every satellite in the field, the same keys in
    # 0.5.168 (release 1.9.1) and on master: detect_changes emits the path and
    # the action (satellite/transport/file_sync.py), the per-turn scan wraps it
    # with the type, the session and the agent (satellite/sessions/session_manager.py).
    return {"type": "file_changed", "session_id": session_id, "agent_slug": _AGENT,
            "path": rel, "action": "delete"}


async def _applied(cm, machine_id):
    """Wait until every frame the receive loop handed ``machine_id``'s lane
    has been applied."""
    from core.remote import satellite_file_transfer as sft
    for _ in range(500):
        if sft.file_changed_in_flight(machine_id, _AGENT) == 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the applier did not finish")


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["live-delete", "dashboard", "retention"])
@pytest.mark.parametrize("rel", ["workspace/gone.md", "workspace/notes/gone.md"])
async def test_a_delete_reported_again_leaves_its_record_and_sends_nothing(
        temp_db, monkeypatch, caplog, rel, origin):
    """A second report of a delete the platform recorded, for a file it no
    longer holds, from a machine with no base for it: the record keeps its
    time, its origin and its age, nothing is sent on and no dashboard is
    told. A report whose folder went with the first delete is no error."""
    from core.remote.satellite_connection import SatelliteConnectionManager
    from storage.files import file_tombstones_store, sync_state_store

    fan_out, refreshed = _repeat_rig(monkeypatch)
    _agent_path("workspace").mkdir(parents=True)
    file_tombstones_store.record(_AGENT, rel, 1000.0, origin=origin)
    before = file_tombstones_store.get(_AGENT, rel)

    with caplog.at_level(logging.INFO):
        await SatelliteConnectionManager()._apply_file_changed("mBob", _delete_frame(rel))

    assert file_tombstones_store.get(_AGENT, rel) == before
    fan_out.assert_not_awaited()
    refreshed.assert_not_awaited()
    assert sync_state_store.get_one("mBob", _AGENT, rel) is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_a_file_that_came_back_and_goes_again_is_a_new_deletion(temp_db, monkeypatch):
    """Delete, the same delete from a second session on the machine, the file
    written again by another machine, delete again: the second report
    changes nothing, the third is a deletion of its own with a new time and
    age, sent on."""
    from core.remote.satellite_connection import SatelliteConnectionManager
    from storage.files import file_tombstones_store, sync_state_store

    rel = "workspace/doc.md"
    fan_out, _ = _repeat_rig(monkeypatch)
    path = _platform_file(rel, b"v1")
    sync_state_store.record_one("mBob", _AGENT, rel, _h(b"v1"), 1.0)
    cm = SatelliteConnectionManager()

    await cm._apply_file_changed("mBob", _delete_frame(rel))
    first = file_tombstones_store.get(_AGENT, rel)
    await cm._apply_file_changed("mBob", _delete_frame(rel, "sess-bob-2"))
    assert file_tombstones_store.get(_AGENT, rel) == first
    assert fan_out.await_count == 1

    await cm._apply_file_changed("mCarol", _write_msg(_AGENT, rel, b"v2", "sess-carol"))
    assert path.read_bytes() == b"v2"
    sync_state_store.record_one("mBob", _AGENT, rel, _h(b"v2"), 2.0)  # the push to Bob
    await cm._apply_file_changed("mBob", _delete_frame(rel))

    second = file_tombstones_store.get(_AGENT, rel)
    assert not path.exists()
    assert second["deleted_at_mtime"] > first["deleted_at_mtime"]
    assert second["expires_at"] > first["expires_at"]
    assert fan_out.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "held, no base", "held, a base", "held above the capture size", "a link at the name",
    "the reporter's base", "a hold", "no record", "an expired record",
])
async def test_a_delete_that_is_not_a_repeat_is_recorded_and_sent_on(temp_db, monkeypatch, case):
    """A delete runs as it always has unless all three hold: nothing at the
    path, a live record that is not a hold, and no base of the reporting
    machine. A file held (a link at the name, a file too large to capture),
    a machine that still has a base for the path (it held a copy the
    platform knew), a hold, no record and an expired one are each a
    deletion of their own: recorded with a new time, sent on, the name
    gone."""
    from core.remote import satellite_file_transfer as sft
    from core.remote.satellite_connection import SatelliteConnectionManager
    from storage.files import file_tombstones_store, sync_state_store

    rel = "workspace/doc.md"
    fan_out, _ = _repeat_rig(monkeypatch)
    _agent_path("workspace").mkdir(parents=True)
    origin, ttl_days = "retention", 30
    if case.startswith("held"):
        _platform_file(rel, b"twelve bytes")
    if case == "held, a base" or case == "the reporter's base":
        sync_state_store.record_one("mBob", _AGENT, rel, _h(b"twelve bytes"), 1.0)
    if case == "held above the capture size":
        monkeypatch.setattr(sft, "CONFLICT_BACKUP_MAX_BYTES", 4)
    if case == "a link at the name":
        _platform_file("workspace/other.md")
        _agent_path(rel).symlink_to("other.md")
    if case == "a hold":
        origin = file_tombstones_store.HOLD_ORIGIN
    if case == "an expired record":
        ttl_days = -1
    if case != "no record":
        file_tombstones_store.record(_AGENT, rel, 1000.0, origin=origin, ttl_days=ttl_days)

    await SatelliteConnectionManager()._apply_file_changed("mBob", _delete_frame(rel))

    row = file_tombstones_store.get(_AGENT, rel)
    assert row["deleted_at_mtime"] > 1000.0 and row["origin"] == "live-delete"
    fan_out.assert_awaited_once()
    assert not _agent_path(rel).is_symlink() and not _agent_path(rel).exists()
    assert sync_state_store.get_one("mBob", _AGENT, rel) is None


@pytest.mark.asyncio
async def test_after_a_repeat_the_record_still_orders_copies_and_ends_on_time(temp_db, monkeypatch):
    """What the record decides after a repeat is what the first delete set:
    an old copy on a machine that was away is deleted at its merge and never
    pulled by a read, a copy made after the first delete is adopted, and
    the record ends 30 days after the first delete."""
    import datetime
    import time

    import config
    from core.remote import remote_file_cache
    from core.remote.file_sync import diff_manifests
    from core.remote.satellite_connection import SatelliteConnectionManager
    from storage.files import file_tombstones_store

    rel = "workspace/report.md"
    _repeat_rig(monkeypatch)
    _agent_path("workspace").mkdir(parents=True)
    deleted_at = time.time() - 20 * 86400
    at_delete = datetime.datetime.fromtimestamp(deleted_at, datetime.timezone.utc)
    with monkeypatch.context() as m:
        m.setattr(file_tombstones_store, "_now", lambda: at_delete)
        file_tombstones_store.record(_AGENT, rel, deleted_at, origin="live-delete")

    await SatelliteConnectionManager()._apply_file_changed("mBob", _delete_frame(rel))

    tombstones = file_tombstones_store.load_for_agent(_AGENT)
    cm = MagicMock()
    cm.get_clock_offset = MagicMock(return_value=0.0)
    for made_at, op, stands in ((deleted_at - 3600, "delete_satellite", True),
                                (deleted_at + 3600, "pull", False)):
        remote = [{"path": rel, "hash": "sha256:c", "size": 1, "mtime": made_at}]
        plan = diff_manifests([], remote, tombstones=tombstones, clock_offset=0.0,
                              target_role="editor", target_username="carol")
        assert [a.op for a in plan.actions if a.rel_path == rel] == [op]
        fresh = {"exists": True, "size": 1, "mtime_ns": int(made_at * 1e9)}
        assert await remote_file_cache.delete_stands(
            cm, ("mCarol", _AGENT, rel), _agent_path(rel), fresh) is stands

    past_its_age = at_delete + datetime.timedelta(days=config.FILE_TOMBSTONE_TTL_DAYS, seconds=1)
    monkeypatch.setattr(file_tombstones_store, "_now", lambda: past_its_age)
    assert file_tombstones_store.get(_AGENT, rel) is None
    assert file_tombstones_store.delete_expired() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["in one batch", "after a reconnect"])
async def test_one_delete_reported_twice_is_recorded_and_sent_once(temp_db, monkeypatch, second):
    """Two reports of one delete through the receive loop, from a second
    session in the same batch or the same frame again after a reconnect:
    the path lock orders them, the second finds the deletion recorded. The
    frame is the one 0.5.168 and today's satellite both send."""
    from core.remote.satellite_connection import SatelliteConnectionManager
    from storage.files import file_tombstones_store

    rel = "workspace/doc.md"
    fan_out, refreshed = _repeat_rig(monkeypatch)
    _platform_file(rel)
    cm = SatelliteConnectionManager()

    if second == "in one batch":
        assert cm._dispatch_file_changed("mBob", _delete_frame(rel))
        assert cm._dispatch_file_changed("mBob", _delete_frame(rel, "sess-bob-2"))
        await _applied(cm, "mBob")
        first = file_tombstones_store.get(_AGENT, rel)
    else:
        assert cm._dispatch_file_changed("mBob", _delete_frame(rel))
        await _applied(cm, "mBob")
        first = file_tombstones_store.get(_AGENT, rel)
        cm._reset_file_changed_lane("mBob")
        assert cm._dispatch_file_changed("mBob", _delete_frame(rel))
        await _applied(cm, "mBob")

    assert first is not None and file_tombstones_store.get(_AGENT, rel) == first
    assert fan_out.await_count == 1 and refreshed.await_count == 1


@pytest.mark.asyncio
async def test_a_repeat_while_the_first_delete_is_on_its_way_sends_it_once(temp_db, monkeypatch):
    """The first delete is still in Carol's lane when Bob's second session
    reports it: Carol gets it once."""
    from core.remote import satellite_connection as sc
    from core.remote.satellite_connection import SatelliteConnectionManager
    from services.remote import workspace_fanout as wf

    rel = "workspace/doc.md"
    _repeat_rig(monkeypatch, real_fan_out=True)
    monkeypatch.setattr(wf, "fanout_targets",
                        lambda a, r, *, exclude_machine_id=None, shared_only=None:
                        [m for m in ("mBob", "mCarol") if m != exclude_machine_id])
    sent, gate = [], asyncio.Event()

    async def _send(machine_id, msg):
        sent.append((machine_id, msg["action"], msg["path"]))
        await gate.wait()

    fake = _cm()
    fake.shares_folders = MagicMock(return_value=False)
    fake.send_fire_and_forget = AsyncMock(side_effect=_send)
    monkeypatch.setattr(sc, "get_connection_manager", lambda: fake)
    _platform_file(rel)
    cm = SatelliteConnectionManager()

    await cm._apply_file_changed("mBob", _delete_frame(rel))
    for _ in range(500):
        if sent:
            break
        await asyncio.sleep(0.01)
    await cm._apply_file_changed("mBob", _delete_frame(rel, "sess-bob-2"))
    gate.set()
    assert await wf.drain_fan_outs()

    assert sent == [("mCarol", "delete", rel)]


@pytest.mark.asyncio
async def test_a_repeat_removes_nothing_so_a_file_written_meanwhile_stays(temp_db, monkeypatch):
    """A person's upload takes no path lock: one that lands just after a
    repeat looked at the name keeps its file, since a repeat removes
    nothing."""
    from core.remote.satellite_connection import SatelliteConnectionManager
    from services.infra import safe_fs
    from storage.files import file_tombstones_store

    rel = "workspace/doc.md"
    fan_out, _ = _repeat_rig(monkeypatch)
    _agent_path("workspace").mkdir(parents=True)
    file_tombstones_store.record(_AGENT, rel, 1000.0, origin="live-delete")
    before = file_tombstones_store.get(_AGENT, rel)
    looked = safe_fs.kind_beneath

    def _look_then_upload(root, name):
        kind = looked(root, name)
        _platform_file(rel, b"uploaded")
        return kind

    monkeypatch.setattr(safe_fs, "kind_beneath", _look_then_upload)

    await SatelliteConnectionManager()._apply_file_changed("mBob", _delete_frame(rel))

    assert _agent_path(rel).read_bytes() == b"uploaded"
    assert file_tombstones_store.get(_AGENT, rel) == before
    fan_out.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_the_reporting_machines_own_base_counts(temp_db, monkeypatch):
    """Carol was away with an old copy and still has its base. Bob's second
    report of the recorded delete is a repeat all the same: another
    machine's base never makes it new."""
    from core.remote.satellite_connection import SatelliteConnectionManager
    from storage.files import file_tombstones_store, sync_state_store

    rel = "workspace/gone.md"
    fan_out, _ = _repeat_rig(monkeypatch)
    _agent_path("workspace").mkdir(parents=True)
    file_tombstones_store.record(_AGENT, rel, 1000.0, origin="live-delete")
    sync_state_store.record_one("mCarol", _AGENT, rel, _h(b"old"), 1.0)
    before = file_tombstones_store.get(_AGENT, rel)

    await SatelliteConnectionManager()._apply_file_changed("mBob", _delete_frame(rel))

    assert file_tombstones_store.get(_AGENT, rel) == before
    fan_out.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_link_to_nothing_at_the_name_is_a_delete_of_its_own(temp_db, monkeypatch):
    """A link whose target is gone is still a name on the platform: the
    delete removes it, records anew and is sent on."""
    from core.remote.satellite_connection import SatelliteConnectionManager
    from storage.files import file_tombstones_store

    rel = "workspace/doc.md"
    fan_out, _ = _repeat_rig(monkeypatch)
    _agent_path("workspace").mkdir(parents=True)
    _agent_path(rel).symlink_to("nowhere.md")
    file_tombstones_store.record(_AGENT, rel, 1000.0, origin="live-delete")

    await SatelliteConnectionManager()._apply_file_changed("mBob", _delete_frame(rel))

    assert not _agent_path(rel).is_symlink()
    assert file_tombstones_store.get(_AGENT, rel)["deleted_at_mtime"] > 1000.0
    fan_out.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_repeat_of_a_knowledge_delete_projects_nothing(temp_db, monkeypatch):
    """The first delete of a knowledge file is projected to the libraries,
    the repeat is not."""
    import core.session.session_state as ss
    from core.remote.satellite_connection import SatelliteConnectionManager
    from services.knowledge import library_projector

    rel = "knowledge/notes.md"
    fan_out, _ = _repeat_rig(monkeypatch)
    manager = _editor_on("mBob", "bob")
    manager.role = "manager"
    monkeypatch.setattr(ss, "get_session_security",
                        {"sess-bob": manager, "sess-bob-2": manager}.get)
    projected = AsyncMock()
    monkeypatch.setattr(library_projector, "propagate_source_write", projected)
    _platform_file(rel)
    cm = SatelliteConnectionManager()

    await cm._apply_file_changed("mBob", _delete_frame(rel))
    assert projected.call_count == 1
    await cm._apply_file_changed("mBob", _delete_frame(rel, "sess-bob-2"))

    assert projected.call_count == 1
    assert fan_out.await_count == 1


# --- A write that did not land: nothing is recorded or sent on ---

_LARGE = 5 * 1024 * 1024


async def _large_write_frame(machine_id, session_id, rel, new_hash, monkeypatch):
    """A machine's write above the inline size, whose pull by the platform
    fails (a link that dropped, a stall, a refused read)."""
    from core.remote.satellite_connection import SatelliteConnectionManager
    pull = AsyncMock(return_value=False)
    monkeypatch.setattr(SatelliteConnectionManager, "pull_file_to_path", pull)
    await SatelliteConnectionManager()._apply_file_changed(machine_id, {
        "type": "file_changed", "session_id": session_id, "agent_slug": _AGENT,
        "path": rel, "action": "write", "size": _LARGE, "hash": new_hash})
    return pull


@pytest.mark.asyncio
async def test_an_edit_whose_pull_failed_keeps_the_base_and_sends_nothing(temp_db, monkeypatch):
    """Carol edits a large file both sides hold, and the pull fails. The
    platform keeps its bytes, so her base keeps the old hash, the author
    stays, nothing is sent on, and her next merge pulls her edit instead of
    pushing the older copy over it."""
    import time

    from core.remote.file_sync import FileEntry, diff_manifests
    from services.remote import workspace_fanout as wf
    from storage.files import file_author_store, sync_state_store

    rel = "workspace/render.bin"
    _repeat_rig(monkeypatch)
    old = b"the old bytes"
    _platform_file(rel, old)
    sync_state_store.record_one("mCarol", _AGENT, rel, _h(old), 1.0)
    file_author_store.record(_AGENT, rel, "alice")
    monkeypatch.setattr(wf, "fanout_targets", lambda *a, **k: ["mDan"])
    marked, pushed = AsyncMock(return_value={}), AsyncMock()
    monkeypatch.setattr(wf, "mark_fan_out_targets", marked)
    monkeypatch.setattr(wf, "fan_out_write", pushed)
    new_hash = "sha256:" + "c" * 64
    made_at = time.time() - 5

    pull = await _large_write_frame("mCarol", "sess-carol", rel, new_hash, monkeypatch)

    pull.assert_awaited_once()
    assert _agent_path(rel).read_bytes() == old
    assert sync_state_store.get_one("mCarol", _AGENT, rel)[0] == _h(old)
    assert file_author_store.get(_AGENT, rel) == "alice"
    marked.assert_not_awaited()
    pushed.assert_not_awaited()
    plan = diff_manifests(
        [FileEntry(rel, _h(old), len(old), time.time() - 3600)],
        [{"path": rel, "hash": new_hash, "size": _LARGE, "mtime": made_at}],
        base=sync_state_store.load_for_machine_agent("mCarol", _AGENT), clock_offset=0.0,
        target_role="editor", target_username="carol")
    assert [a.op for a in plan.actions if a.rel_path == rel] == ["pull"]


@pytest.mark.asyncio
async def test_a_write_whose_pull_failed_captures_nothing_and_tells_nobody(temp_db, monkeypatch):
    """Alice edited the file on the platform after Carol last saw it, then
    Carol's large write fails to arrive. Alice's bytes were never replaced:
    nothing goes to the Recover bin and Alice is not told her version lost."""
    from storage.files import file_author_store, recover_bin_store, sync_state_store

    rel = "workspace/plan.md"
    notifs = []
    _patch_apply_deps(monkeypatch, sec=_editor_on("mCarol", "carol"),
                      loser_sub_for=lambda u: "user-alice", notifs=notifs)
    _platform_file(rel, b"alice's edit")
    file_author_store.record(_AGENT, rel, "alice")
    sync_state_store.record_one("mCarol", _AGENT, rel, _h(b"what carol saw"), 1.0)

    await _large_write_frame("mCarol", "sess-carol", rel, "sha256:" + "c" * 64, monkeypatch)

    assert _agent_path(rel).read_bytes() == b"alice's edit"
    assert notifs == []
    assert [e for e in recover_bin_store.list_for(_AGENT, "admin", True, True, True)
            if e["reason"] == "conflict"] == []


@pytest.mark.asyncio
async def test_a_write_frame_with_no_body_and_no_size_lands_nothing(temp_db, monkeypatch, caplog):
    """A write frame that carries neither the bytes nor a size names nothing
    the platform can fetch: no base, no author, nothing sent on, one warning."""
    from core.remote.satellite_connection import SatelliteConnectionManager
    from services.remote import workspace_fanout as wf
    from storage.files import file_author_store, sync_state_store

    rel = "workspace/mystery.md"
    _repeat_rig(monkeypatch)
    _agent_path("workspace").mkdir(parents=True)
    monkeypatch.setattr(wf, "fanout_targets", lambda *a, **k: ["mDan"])
    marked = AsyncMock(return_value={})
    monkeypatch.setattr(wf, "mark_fan_out_targets", marked)

    with caplog.at_level(logging.WARNING):
        await SatelliteConnectionManager()._apply_file_changed("mBob", {
            "type": "file_changed", "session_id": "sess-bob", "agent_slug": _AGENT,
            "path": rel, "action": "write", "hash": "sha256:" + "d" * 64})

    assert not _agent_path(rel).exists()
    assert sync_state_store.get_one("mBob", _AGENT, rel) is None
    assert file_author_store.get(_AGENT, rel) is None
    marked.assert_not_awaited()
    assert [r for r in caplog.records
            if r.levelno == logging.WARNING and "carries no body" in r.getMessage()]


@pytest.mark.asyncio
async def test_apply_agent_mismatch_rejected(temp_db, tmp_path, monkeypatch):
    """A file_changed whose payload agent_slug != the session's authenticated
    agent is rejected (no write, no fan-out)."""
    import config
    from core.remote.satellite_connection import SatelliteConnectionManager

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    cm = SatelliteConnectionManager()
    notifs = []
    _patch_apply_deps(
        monkeypatch,
        sec=SimpleNamespace(placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="mBob"), role="manager", username="bob", agent="real-agent", display_name="Bob"),
        loser_sub_for=lambda u: "user-viewer",
        notifs=notifs,
    )
    # Payload claims a DIFFERENT agent than the session's authenticated one.
    await cm._apply_file_changed("mBob", _write_msg("spoofed-agent", "workspace/x.md", b"data"))

    assert not (tmp_path / "spoofed-agent" / "workspace" / "x.md").exists()




# ---------------------------------------------------------------------------
# fan_out_write ↔ transfer_registry tracking (Feature E, 1.4.0)
# ---------------------------------------------------------------------------


def _tracked_setup(monkeypatch, machines=("m1", "m2")):
    from core.remote import satellite_connection as sc, transfer_registry as tr
    from services.remote import workspace_fanout
    monkeypatch.setattr(
        workspace_fanout, "fanout_targets",
        lambda a, r, *, exclude_machine_id=None: list(machines),
    )
    fake_cm = _cm()
    fake_cm.push_file.return_value = True
    monkeypatch.setattr(sc, "get_connection_manager", lambda: fake_cm)
    tr._items.clear()
    tr.set_broadcaster(None)
    monkeypatch.setattr(tr, "_machine_name", lambda mid: mid)
    monkeypatch.setattr(tr, "_resolve_recipients", lambda a, p: frozenset({"u1"}))
    return workspace_fanout, fake_cm, tr


@pytest.mark.asyncio
async def test_fan_out_big_payload_creates_item_and_done_rows(temp_db, monkeypatch):
    workspace_fanout, fake_cm, tr = _tracked_setup(monkeypatch)
    big = b"x" * (600 * 1024)  # > MAX_CHUNK_SIZE → tracked without explicit id
    await _fan("agent-1", "workspace/big.bin", big)
    items = tr.snapshot_inflight()
    assert len(items) == 1
    assert {r.state for r in items[0].machines.values()} == {"done"}
    assert items[0].done_at is not None
    # progress_cb was forwarded per machine
    for call in fake_cm.push_file.await_args_list:
        assert call.kwargs.get("progress_cb") is not None


@pytest.mark.asyncio
async def test_fan_out_small_sync_payload_untracked(temp_db, monkeypatch):
    workspace_fanout, fake_cm, tr = _tracked_setup(monkeypatch)
    await _fan("agent-1", "workspace/small.md", b"tiny")
    assert tr.snapshot_inflight() == []
    assert fake_cm.push_file.await_count == 2


@pytest.mark.asyncio
async def test_fan_out_explicit_transfer_id_tracks_any_size(temp_db, monkeypatch):
    workspace_fanout, fake_cm, tr = _tracked_setup(monkeypatch, machines=("m1",))
    await _fan(
        "agent-1", "workspace/small.md", b"tiny",
        transfer_kind="upload", transfer_id="upl-1",
    )
    item = tr.get("upl-1")
    assert item is not None and item.kind == "upload"
    assert item.machines["m1"].state == "done"


@pytest.mark.asyncio
async def test_fan_out_failed_push_marks_row_failed(temp_db, monkeypatch):
    workspace_fanout, fake_cm, tr = _tracked_setup(monkeypatch, machines=("m1", "m2"))

    async def _push(mid, ref, source, **kw):
        return mid != "m2"  # m2 offline

    fake_cm.push_file.side_effect = _push
    big = b"x" * (600 * 1024)
    await _fan("agent-1", "workspace/big.bin", big)
    item = tr.snapshot_inflight()[0]
    assert item.machines["m1"].state == "done"
    assert item.machines["m2"].state == "failed"
    assert "The next sync brings it" in item.machines["m2"].error
    assert item.done_at is not None


# ---------------------------------------------------------------------------
# fan_out_write ↔ transfer_gate (Feature F, 1.4.0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fan_out_gate_is_per_machine(temp_db, monkeypatch):
    import asyncio

    import config
    from core.remote import transfer_gate
    monkeypatch.setattr(config, "SYNC_FANOUT_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(config, "SYNC_FANOUT_MIN_MB", 0, raising=False)
    transfer_gate.reset_for_tests()
    workspace_fanout, fake_cm, tr = _tracked_setup(
        monkeypatch, machines=("m1", "m2", "m3"),
    )
    state = {"active": 0, "max_active": 0}

    async def _push(mid, ref, source, **kw):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(0.02)
        state["active"] -= 1
        return True

    fake_cm.push_file.side_effect = _push
    big = b"x" * (600 * 1024)
    await _fan("agent-1", "workspace/big.bin", big)
    transfer_gate.reset_for_tests()
    # One slot per machine: three machines push at once, none waits on another.
    assert state["max_active"] == 3
    assert fake_cm.push_file.await_count == 3  # every machine still pushed
    # base-advance intact for all acked machines
    item = tr.snapshot_inflight()[0]
    assert {r.state for r in item.machines.values()} == {"done"}


@pytest.mark.asyncio
async def test_gate_queued_state_reaches_registry(temp_db, monkeypatch):
    import asyncio

    import config
    from core.remote import transfer_gate
    monkeypatch.setattr(config, "SYNC_FANOUT_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(config, "SYNC_FANOUT_MIN_MB", 0, raising=False)
    transfer_gate.reset_for_tests()
    workspace_fanout, fake_cm, tr = _tracked_setup(monkeypatch, machines=("m1", "m2"))
    seen_states: list[tuple[str, str]] = []
    orig_set_state = tr.set_state

    async def _spy(tid, mid, state, **kw):
        seen_states.append((mid, state))
        await orig_set_state(tid, mid, state, **kw)

    monkeypatch.setattr(tr, "set_state", _spy)

    async def _push(mid, ref, source, **kw):
        await asyncio.sleep(0.02)
        return True

    fake_cm.push_file.side_effect = _push
    big = b"x" * (600 * 1024)
    # m1's one slot is held by another push: m1 queues, m2 never does.
    held = asyncio.Event()
    release = asyncio.Event()

    async def _hold():
        async with transfer_gate.slot("m1", "agent-1", "other.bin", 600 * 1024):
            held.set()
            await release.wait()

    holder = asyncio.create_task(_hold())
    await held.wait()
    fan_out = asyncio.create_task(
        _fan("agent-1", "workspace/big.bin", big))
    while ("m2", "done") not in seen_states:  # noqa: ASYNC110  # the recorded states list: the gate's callback has no event
        await asyncio.sleep(0.01)
    release.set()
    await asyncio.gather(holder, fan_out)
    transfer_gate.reset_for_tests()
    assert ("m1", "queued") in seen_states
    assert ("m2", "queued") not in seen_states
    # Both passed through active and reached done.
    for mid in ("m1", "m2"):
        assert (mid, "active") in seen_states
        assert (mid, "done") in seen_states


# ---------------------------------------------------------------------------
# The platform write beneath the root; the push reads the checked file
# ---------------------------------------------------------------------------


def _swap_component_on_open(monkeypatch, agent_dir, victim):
    """A component swapped for a link right as the helper opens the root,
    after every earlier check: the strict open must refuse it."""
    import contextlib
    import os

    from services.infra import safe_fs
    real = safe_fs.open_root
    state = {"done": False}

    @contextlib.contextmanager
    def _patched(root, rel=""):
        if not state["done"]:
            state["done"] = True
            d = agent_dir / "workspace" / "sub"
            d.rmdir()
            os.symlink(victim, d)
        with real(root, rel) as fd:
            yield fd

    monkeypatch.setattr(safe_fs, "open_root", _patched)


@pytest.mark.asyncio
async def test_atomic_write_refuses_a_component_swapped_after_the_check(temp_db, tmp_path, monkeypatch):
    import config
    from services.remote import workspace_fanout as wf
    agents = tmp_path / "agents"
    (agents / "agent-1" / "workspace" / "sub").mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "agent.md").write_text("ORIGINAL")
    monkeypatch.setattr(config, "AGENTS_DIR", agents, raising=False)
    _swap_component_on_open(monkeypatch, agents / "agent-1", victim)
    with pytest.raises(OSError):
        await wf._atomic_write_agent_file("agent-1", "workspace/sub/agent.md", b"NEW")
    assert (victim / "agent.md").read_text() == "ORIGINAL"
    assert sorted(p.name for p in victim.iterdir()) == ["agent.md"]


@pytest.mark.asyncio
async def test_atomic_write_refuses_a_bad_rel_as_value_error(temp_db, tmp_path, monkeypatch):
    import config
    from services.remote import workspace_fanout as wf
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    with pytest.raises(ValueError):
        await wf._atomic_write_agent_file("agent-1", "../x.md", b"NEW")
    with pytest.raises(ValueError):
        await wf._atomic_write_agent_file("../agent-1", "workspace/x.md", b"NEW")


@pytest.mark.asyncio
async def test_fan_out_write_pushes_the_checked_descriptor_and_records_its_hash(temp_db, tmp_path, monkeypatch):
    """A Path source is opened beneath the agents root once; ``push_file``
    receives the descriptor's own path and the hash taken from that
    descriptor (sent as the frame's hash, never taken again), and the merge
    base records that hash."""
    import config
    from services.remote import workspace_fanout as wf
    from storage.files import sync_state_store
    agents = tmp_path / "agents"
    (agents / "agent-1" / "workspace").mkdir(parents=True)
    f = agents / "agent-1" / "workspace" / "x.md"
    f.write_bytes(b"hello")
    monkeypatch.setattr(config, "AGENTS_DIR", agents, raising=False)
    monkeypatch.setattr(wf, "fanout_targets", lambda a, r, *, exclude_machine_id=None: ["m1"])
    seen = {}

    async def _push(mid, ref, source, **kw):
        seen["source"] = source
        seen["bytes"] = source if isinstance(source, bytes) else source.read_bytes()
        seen["content_hash"] = kw.get("content_hash")
        return True

    cm = _cm()
    cm.push_file.side_effect = _push
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    recorded = []
    monkeypatch.setattr(sync_state_store, "record_one",
                        lambda mid, a, r, h, m: recorded.append((mid, a, r, h, m)))
    await _fan("agent-1", "workspace/x.md", f)
    assert str(seen["source"]).startswith(("/proc/self/fd/", "/dev/fd/"))
    assert seen["bytes"] == b"hello"
    assert seen["content_hash"] == _h(b"hello")
    assert recorded and recorded[0][:4] == ("m1", "agent-1", "workspace/x.md", _h(b"hello"))
    # Bytes say what the caller wrote to the platform copy: the lane pushes
    # that copy from its own descriptor, hashed there.
    f.write_bytes(b"inline")
    await _fan("agent-1", "workspace/x.md", b"inline")
    assert seen["content_hash"] == _h(b"inline") and seen["bytes"] == b"inline"


@pytest.mark.asyncio
async def test_fan_out_write_refuses_a_source_that_is_not_the_platform_copy(temp_db, tmp_path, monkeypatch):
    import config
    from core.remote import transfer_registry
    from services.remote import workspace_fanout as wf
    agents = tmp_path / "agents"
    (agents / "agent-1" / "workspace").mkdir(parents=True)
    (agents / "agent-1" / "workspace" / "sub").symlink_to(tmp_path)
    (tmp_path / "x.md").write_bytes(b"outside")
    monkeypatch.setattr(config, "AGENTS_DIR", agents, raising=False)
    monkeypatch.setattr(wf, "fanout_targets", lambda a, r, *, exclude_machine_id=None: ["m1"])
    cm = _cm()
    pushed = cm.push_file
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    begun = []

    async def _begin(agent, rel, **kw):
        begun.append(kw.get("machine_ids"))
        return "tid"

    monkeypatch.setattr(transfer_registry, "begin", _begin)
    # A link component: the strict open refuses, nothing is pushed, and a
    # tracked transfer still gets its terminal (no rows).
    await _fan("agent-1", "workspace/sub/x.md",
                           agents / "agent-1" / "workspace" / "sub" / "x.md", transfer_id="t1")
    pushed.assert_not_awaited()
    assert begun == [[]]
    # A Path that is not the platform copy of ``rel_path`` at all.
    await _fan("agent-1", "workspace/y.md", tmp_path / "x.md")
    pushed.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_saves_fan_out_holds_the_fan_out_lock_and_never_the_path_lock(temp_db, tmp_path, monkeypatch):
    import config
    from core.remote import remote_file_flow
    from services.remote import workspace_fanout as wf
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    _setup_layer(monkeypatch, {"s1": _FakeInfo("mA", "agent-1")}, {"s1": _sec("", "manager")})
    seen = {}

    async def _fan_out(agent_slug, rel_path, source, **kw):
        path_lock = await remote_file_flow._acquire_global_path_lock(agent_slug, rel_path)
        fan_lock = await remote_file_flow.acquire_fanout_lock(agent_slug, rel_path)
        seen["path"] = path_lock.locked()
        seen["fanout"] = fan_lock.locked()

    monkeypatch.setattr(wf, "fan_out_write", _fan_out)
    await wf.propagate_write("agent-1", "workspace/x.md", b"hello")
    await wf.drain_save_fan_outs()
    assert seen == {"path": False, "fanout": True}


@pytest.mark.asyncio
async def test_a_save_answers_while_a_target_never_answers(temp_db, tmp_path, monkeypatch):
    """A machine that stalls (its socket drops only after 30 s) no longer
    holds the save: propagate_write returns while the push still waits."""
    import asyncio

    import config
    from core.remote import satellite_connection as sc
    from services.remote import workspace_fanout as wf
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    _setup_layer(monkeypatch, {"s1": _FakeInfo("mStalled", "agent-1")}, {"s1": _sec("", "manager")})
    stalled = asyncio.Event()
    release = asyncio.Event()
    pushed = []

    async def _push(machine_id, ref, source, **kw):
        pushed.append(open(source, "rb").read())  # noqa: ASYNC230  # a fake push reads its own tmp_path source
        stalled.set()
        await release.wait()
        return False

    cm = MagicMock()
    cm.push_file = AsyncMock(side_effect=_push)
    cm.stat_file = AsyncMock(return_value=None)
    monkeypatch.setattr(sc, "get_connection_manager", lambda: cm)

    # The waits are guards against a hang (the target never answers, so a
    # save that waited for it would wait for good), wide enough for a box
    # that stalls: their length is nobody's subject.
    await asyncio.wait_for(wf.propagate_write("agent-1", "workspace/x.md", b"v1"), timeout=20)
    await asyncio.wait_for(stalled.wait(), timeout=20)
    # A second save while the first's fan-out is stuck: it answers too, and
    # one more fan-out queues behind the running one.
    await asyncio.wait_for(wf.propagate_write("agent-1", "workspace/x.md", b"v2"), timeout=20)
    await asyncio.wait_for(wf.propagate_write("agent-1", "workspace/x.md", b"v3"), timeout=20)
    # Each save's fan-out only names the file for the machine's lane and
    # ends: nothing queues behind the push that is stuck.
    await asyncio.wait_for(wf.drain_save_fan_outs(), timeout=20)
    assert pushed == [b"v1"]
    release.set()
    assert await wf.drain_fan_outs()
    # The lane then pushed the newest copy, once.
    assert pushed == [b"v1", b"v3"]


@pytest.mark.asyncio
async def test_a_save_after_the_fan_out_started_schedules_its_own(temp_db, tmp_path, monkeypatch):
    """The queued mark is dropped as the fan-out takes its lock, before it
    opens the platform copy: a save after that point is pushed again."""
    import asyncio

    import config
    from services.remote import workspace_fanout as wf
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    _setup_layer(monkeypatch, {"s1": _FakeInfo("mA", "agent-1")}, {"s1": _sec("", "manager")})
    started = asyncio.Event()
    release = asyncio.Event()
    sources = []

    async def _fan_out(agent_slug, rel_path, source, **kw):
        sources.append(source.read_bytes())
        started.set()
        await release.wait()

    monkeypatch.setattr(wf, "fan_out_write", _fan_out)
    await wf.propagate_write("agent-1", "workspace/x.md", b"v1")
    await asyncio.wait_for(started.wait(), timeout=2)
    assert not wf._queued_save_fan_outs
    await wf.propagate_write("agent-1", "workspace/x.md", b"v2")
    assert len(wf._queued_save_fan_outs) == 1
    release.set()
    await wf.drain_save_fan_outs()
    assert sources == [b"v1", b"v2"]


@pytest.mark.asyncio
async def test_a_refused_save_writes_marks_and_schedules_nothing(temp_db, tmp_path, monkeypatch):
    import config
    from core.remote import remote_file_flow
    from services.remote import workspace_fanout as wf
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    _setup_layer(monkeypatch, {"s1": _FakeInfo("mA", "agent-1")}, {"s1": _sec("", "manager")})
    fo = AsyncMock()
    monkeypatch.setattr(wf, "fan_out_write", fo)

    async def _no():
        return False

    assert await wf.propagate_write("agent-1", "workspace/x.md", b"v1", precheck=_no) is False
    assert not (tmp_path / "agent-1" / "workspace" / "x.md").exists()
    assert not remote_file_flow._platform_ahead and not wf._save_fan_out_tasks
    fo.assert_not_awaited()


@pytest.mark.asyncio
async def test_bookkeeping_and_marks_never_fail_a_landed_save(temp_db, tmp_path, monkeypatch, caplog):
    """A 500 after the bytes landed would have Collabora retry into a
    refusal of its own save: what follows the write logs and goes on."""
    import config
    from services.remote import workspace_fanout as wf
    from storage.files import file_tombstones_store
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)

    def _boom(*a, **kw):
        raise RuntimeError("store down")

    monkeypatch.setattr(file_tombstones_store, "drop", _boom)
    monkeypatch.setattr(wf, "shared_only_of", AsyncMock(side_effect=RuntimeError("store down")))
    fo = AsyncMock()
    monkeypatch.setattr(wf, "fan_out_write", fo)
    with caplog.at_level("WARNING", logger="otodock.workspace-fanout"):
        assert await wf.propagate_write("agent-1", "users/u/workspace/x.md", b"v1") is True
    assert (tmp_path / "agent-1" / "users" / "u" / "workspace" / "x.md").read_bytes() == b"v1"
    assert "bookkeeping of a save" in caplog.text
    assert "marking the fan-out targets" in caplog.text
    # The marks failed, the fan-out still runs (it marks its targets itself),
    # bound for no known machine: it holds every machine's turn.
    from core.remote import upload_inflight
    (task,) = wf._save_fan_out_tasks
    assert task not in upload_inflight._bound_for
    await wf.drain_save_fan_outs()
    fo.assert_awaited_once()
    assert fo.await_args.kwargs["marked"] == {}


@pytest.mark.asyncio
async def test_a_save_whose_marks_failed_unbinds_the_queued_fan_out(temp_db, tmp_path, monkeypatch):
    import asyncio

    import config
    from core.remote import upload_inflight
    from services.remote import workspace_fanout as wf
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    release = asyncio.Event()

    async def _fan_out(*a, **kw):
        await release.wait()

    monkeypatch.setattr(wf, "fan_out_write", _fan_out)
    answers = iter([{"mA": (1, 1)}, {"mA": (2, 2)}, None])
    monkeypatch.setattr(wf, "mark_fan_out_targets", AsyncMock(side_effect=lambda *a, **kw: next(answers)))
    await wf.propagate_write("agent-1", "workspace/x.md", b"v1")    # runs, holds the lock
    await asyncio.sleep(0)
    await wf.propagate_write("agent-1", "workspace/x.md", b"v2")    # queued, bound for mA
    queued = next(iter(wf._queued_save_fan_outs.values()))
    assert upload_inflight._bound_for[queued["task"]] == {"mA"}
    await wf.propagate_write("agent-1", "workspace/x.md", b"v3")    # its marks failed
    assert queued["task"] not in upload_inflight._bound_for
    release.set()
    await wf.drain_save_fan_outs()


@pytest.mark.asyncio
async def test_a_marked_machine_the_fan_out_does_not_push_gets_the_failure_rules(
    temp_db, tmp_path, monkeypatch,
):
    """A machine marked in flight that the fan-out does not push to (its
    session ended meanwhile) is no longer served as "a push is on its way"
    for ten minutes: its mark ends its window and keeps its baseline."""
    import config
    from core.remote import remote_file_flow
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    target = tmp_path / "agent-1" / "workspace" / "x.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"v1")
    st = target.stat()
    stat = (st.st_size, st.st_mtime_ns)
    remote_file_flow._record_agent_stat(("mGone", "agent-1", "workspace/x.md"), {"exists": True, "size": 1, "mtime_ns": 1})
    remote_file_flow.note_platform_ahead("mGone", "agent-1", "workspace/x.md", *stat, in_flight_s=600)
    # Another copy's mark on a second machine is a later write's: untouched.
    remote_file_flow.note_platform_ahead("mOther", "agent-1", "workspace/x.md", *stat, in_flight_s=600)
    _setup_layer(monkeypatch, {}, {})
    await _fan("agent-1", "workspace/x.md", target,
                           marked={"mGone": stat, "mOther": (999, 999)})
    gone = remote_file_flow._platform_ahead[("mGone", "agent-1", "workspace/x.md")]
    assert gone["in_flight_until"] == 0.0
    assert gone["machine"] == {"exists": True, "size": 1, "mtime_ns": 1}
    assert remote_file_flow._platform_ahead[("mOther", "agent-1", "workspace/x.md")]["in_flight_until"] > 0


def test_fanout_targets_takes_the_shared_only_answer_without_a_store_read(temp_db, monkeypatch):
    """A caller that resolved ``is_shared_only`` off the loop passes it; the
    gate then never reads the store itself."""
    import core.session.visibility as vis
    _setup_layer(
        monkeypatch,
        {"s1": _FakeInfo("mA", "agent-1")},
        {"s1": _sec("alice", "manager")},
    )

    def _boom(agent):
        raise AssertionError("the store was read on the loop")

    monkeypatch.setattr(vis, "is_shared_only", _boom)
    from services.remote.workspace_fanout import fanout_targets, has_fanout_candidates
    assert fanout_targets("agent-1", "users/alice/workspace/x.md", shared_only=False) == ["mA"]
    assert fanout_targets("agent-1", "users/alice/workspace/x.md", shared_only=True) == []
    assert has_fanout_candidates("agent-1", "users/alice/workspace/x.md", shared_only=True) is False


@pytest.mark.asyncio
async def test_a_tracked_push_moves_its_row_at_most_twice_a_second(temp_db, monkeypatch):
    """A push now reports every acked chunk: the registry row is updated at
    most every 0.5 s, and its terminal always lands."""
    workspace_fanout, fake_cm, tr = _tracked_setup(monkeypatch, machines=("m1",))
    calls: list[tuple[int, int]] = []
    real = tr.progress

    async def _spy(tid, mid, sent, total):
        calls.append((sent, total))
        await real(tid, mid, sent, total)

    monkeypatch.setattr(tr, "progress", _spy)

    async def _push(mid, ref, source, *, progress_cb=None, **kw):
        for sent in range(1, 50):
            await progress_cb(sent * 1024, 50 * 1024)
        await progress_cb(50 * 1024, 50 * 1024)
        return True

    fake_cm.push_file.side_effect = _push
    await _fan("agent-1", "workspace/big.bin", b"x" * (600 * 1024))
    assert len(calls) <= 4
    assert calls[-1][0] == calls[-1][1]


@pytest.mark.asyncio
async def test_a_fan_out_target_changing_the_file_is_marked_ahead_with_its_own_stat(
    temp_db, tmp_path, monkeypatch, caplog,
):
    """Each push expects the copy last pulled or pushed there (taken before
    the push); a target that refuses as stale is marked with the stat its
    ack reported, no probe, and the others are probed as before."""
    import config
    from core.remote import remote_file_flow
    from services.remote import workspace_fanout
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    target = tmp_path / "agent-1" / "workspace" / "x.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"data")
    monkeypatch.setattr(
        workspace_fanout, "fanout_targets",
        lambda a, r, *, exclude_machine_id=None: ["m1", "m2", "m3"],
    )
    remote_file_flow._pull_stat_records.clear()
    remote_file_flow._record_agent_stat(
        ("m1", "agent-1", "workspace/x.md"), {"exists": True, "size": 1, "mtime_ns": 5})
    seen: dict[str, object] = {}

    async def push(mid, ref, content, *, agent_slug="", expect=None, stale_out=None, **kw):
        seen[mid] = expect
        if mid == "m1":
            stale_out.update(reason="live", size=3, mtime_ns=9)
            return False
        return mid == "m2"
    fake_cm = _cm()
    fake_cm.push_file.side_effect = push
    fake_cm.stat_file = AsyncMock(return_value={"exists": True, "size": 1, "mtime_ns": 6})
    from core.remote import satellite_connection as sc
    monkeypatch.setattr(sc, "get_connection_manager", lambda: fake_cm)
    try:
        with caplog.at_level("INFO", logger="otodock"):
            await _fan("agent-1", "workspace/x.md", b"data")
        assert seen == {"m1": {"size": 1, "mtime_ns": 5}, "m2": "live", "m3": "live"}
        assert ("m1", "agent-1", "workspace/x.md") not in remote_file_flow._pull_stat_records
        st = target.stat()
        marks = remote_file_flow._platform_ahead
        assert set(marks) == {("m1", "agent-1", "workspace/x.md"), ("m3", "agent-1", "workspace/x.md")}
        assert marks[("m1", "agent-1", "workspace/x.md")]["machine"] == {"exists": True, "size": 3, "mtime_ns": 9}
        assert marks[("m3", "agent-1", "workspace/x.md")]["machine"] == {"exists": True, "size": 1, "mtime_ns": 6}
        assert all((m["size"], m["mtime_ns"], m["in_flight_until"]) == (st.st_size, st.st_mtime_ns, 0.0)
                   for m in marks.values())
        # One probe, m3's: m1 told its stat in the ack.
        assert fake_cm.stat_file.await_count == 1
        assert "held: the file is being changed there" in caplog.text
    finally:
        remote_file_flow._pull_stat_records.clear()

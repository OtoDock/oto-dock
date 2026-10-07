"""Tests for multi-user shared-workspace sync (fan-out + live versioned merge).

Covers:
- ``workspace_fanout.fanout_targets`` — active-session target selection +
  per-user/per-role isolation + source-machine exclusion + dedupe + fail-closed.
- ``workspace_fanout.fan_out_write`` / ``fan_out_delete`` — push/delete shapes.
- ``satellite_connection._apply_file_changed`` — the live path: cross-user
  clobber capture + base/author advance + delete tombstone (versioned LWW).
"""

import base64
import hashlib
from types import SimpleNamespace

from core import placement
from unittest.mock import AsyncMock, MagicMock

import pytest


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
    from services.remote.workspace_fanout import fanout_targets, _active_machine_ids
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
    from services.remote.workspace_fanout import fanout_targets, _active_machine_ids
    assert fanout_targets("agent-1", "config/p.md") == []
    assert fanout_targets("agent-1", "workspace/p.md") == ["mI"]
    assert _active_machine_ids("agent-1") == {"mI"}


def test_fanout_no_layer(temp_db, monkeypatch):
    import core.session.session_manager as sm

    def _raise():
        raise RuntimeError("layer not registered")

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
    fake_cm = AsyncMock()
    from core.remote import satellite_connection as sc
    monkeypatch.setattr(sc, "get_connection_manager", lambda: fake_cm)

    await workspace_fanout.fan_out_write("agent-1", "workspace/x.md", b"data")

    assert fake_cm.push_file.await_count == 2
    for call in fake_cm.push_file.await_args_list:
        mid, ref, content = call.args[:3]
        assert mid in ("m1", "m2")
        assert ref.kind == "agent_tree" and ref.value == "workspace/x.md"
        assert content == b"data"
        assert call.kwargs.get("agent_slug") == "agent-1"


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
    fake_cm = AsyncMock()
    fake_cm.satellite_supports_file_stat = MagicMock(return_value=True)
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
        await workspace_fanout.fan_out_write("agent-1", "workspace/x.md", b"data")
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
    fake_cm = AsyncMock()
    fake_cm.push_file = AsyncMock(return_value=True)
    from core.remote import satellite_connection as sc
    monkeypatch.setattr(sc, "get_connection_manager", lambda: fake_cm)
    await workspace_fanout.fan_out_write("agent-1", "workspace/x.md", b"data")
    assert marks == ["m1", "m1"]  # before the gather, then at the slot
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
    fake_cm = AsyncMock()
    fake_cm.satellite_supports_file_stat = MagicMock(return_value=True)
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
        await workspace_fanout.fan_out_write("agent-1", "workspace/x.md", b"B1")
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
    fake_cm = AsyncMock()
    from core.remote import satellite_connection as sc
    monkeypatch.setattr(sc, "get_connection_manager", lambda: fake_cm)

    await workspace_fanout.fan_out_delete("agent-1", "workspace/x.md")

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
    fake_cm = AsyncMock()
    from core.remote import satellite_connection as sc
    monkeypatch.setattr(sc, "get_connection_manager", lambda: fake_cm)

    await workspace_fanout.fan_out_write("agent-1", "workspace/x.md", b"d")
    assert fake_cm.push_file.await_count == 0


# ---------------------------------------------------------------------------
# propagate_write — atomic write + fan-out under the global lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propagate_write_persists_and_fans_out(temp_db, tmp_path, monkeypatch):
    """propagate_write atomically writes the bytes to the agent tree AND fans
    them out (source machine excluded), under the global per-(agent,path) lock."""
    import config
    from services.remote import workspace_fanout as wf
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)

    fo = AsyncMock()
    monkeypatch.setattr(wf, "fan_out_write", fo)

    await wf.propagate_write(
        "agent-1", "workspace/x.md", b"hello", exclude_machine_id="mSrc",
    )

    # Authoritative atomic write landed on the platform disk.
    assert (tmp_path / "agent-1" / "workspace" / "x.md").read_bytes() == b"hello"
    # Fanned out with the source machine excluded.
    fo.assert_awaited_once()
    assert fo.await_args.args[:3] == ("agent-1", "workspace/x.md", b"hello")
    assert fo.await_args.kwargs.get("exclude_machine_id") == "mSrc"


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
    from services.remote import workspace_fanout as wf
    import storage.database as db
    import services.notifications.notification_manager as nm
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
    from storage.files import file_author_store
    from storage.files import sync_state_store
    from storage.files import recover_bin_store

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
    from core.remote import remote_workspace_sync as rws

    agents = tmp_path / "agents"
    agent_dir = agents / "agent-1"
    (agent_dir / "workspace").mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"secret")
    (agent_dir / "workspace" / "shared.md").symlink_to(victim)
    (agent_dir / "workspace" / "plain.md").write_bytes(b"mine")
    assert rws._platform_bytes(agent_dir, "workspace/shared.md") is None
    assert rws._platform_bytes(agent_dir, "workspace/plain.md") == b"mine"
    assert rws._platform_bytes(agent_dir, "workspace/../../victim.txt") is None


@pytest.mark.asyncio
async def test_apply_same_user_no_conflict(temp_db, tmp_path, monkeypatch):
    import config
    from core.remote.satellite_connection import SatelliteConnectionManager
    from storage.files import file_author_store
    from storage.files import recover_bin_store

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
async def test_apply_no_conflict_when_base_matches(temp_db, tmp_path, monkeypatch):
    """When this machine's converged base equals the on-disk hash, the write is a
    sequential edit (the machine SAW this version) → no conflict, even cross-user."""
    import config
    from core.remote.satellite_connection import SatelliteConnectionManager
    from storage.files import file_author_store
    from storage.files import sync_state_store
    from storage.files import recover_bin_store

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
    from storage.files import file_author_store
    from storage.files import sync_state_store
    from storage.files import file_tombstones_store
    from storage.files import recover_bin_store

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
    from services.remote import workspace_fanout
    from core.remote import satellite_connection as sc
    from core.remote import transfer_registry as tr
    monkeypatch.setattr(
        workspace_fanout, "fanout_targets",
        lambda a, r, *, exclude_machine_id=None: list(machines),
    )
    fake_cm = AsyncMock()
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
    await workspace_fanout.fan_out_write("agent-1", "workspace/big.bin", big)
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
    await workspace_fanout.fan_out_write("agent-1", "workspace/small.md", b"tiny")
    assert tr.snapshot_inflight() == []
    # untracked pushes carry no progress callback
    for call in fake_cm.push_file.await_args_list:
        assert call.kwargs.get("progress_cb") is None


@pytest.mark.asyncio
async def test_fan_out_explicit_transfer_id_tracks_any_size(temp_db, monkeypatch):
    workspace_fanout, fake_cm, tr = _tracked_setup(monkeypatch, machines=("m1",))
    await workspace_fanout.fan_out_write(
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
    await workspace_fanout.fan_out_write("agent-1", "workspace/big.bin", big)
    item = tr.snapshot_inflight()[0]
    assert item.machines["m1"].state == "done"
    assert item.machines["m2"].state == "failed"
    assert "retries at next sync" in item.machines["m2"].error
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
    await workspace_fanout.fan_out_write("agent-1", "workspace/big.bin", big)
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
        workspace_fanout.fan_out_write("agent-1", "workspace/big.bin", big))
    while ("m2", "done") not in seen_states:
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

    cm = SimpleNamespace(push_file=_push)
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    recorded = []
    monkeypatch.setattr(sync_state_store, "record_one",
                        lambda mid, a, r, h, m: recorded.append((mid, a, r, h, m)))
    await wf.fan_out_write("agent-1", "workspace/x.md", f)
    assert str(seen["source"]).startswith(("/proc/self/fd/", "/dev/fd/"))
    assert seen["bytes"] == b"hello"
    assert seen["content_hash"] == _h(b"hello")
    assert recorded and recorded[0][:4] == ("m1", "agent-1", "workspace/x.md", _h(b"hello"))
    await wf.fan_out_write("agent-1", "workspace/x.md", b"inline")
    assert seen["content_hash"] == _h(b"inline")


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
    pushed = AsyncMock(return_value=True)
    cm = SimpleNamespace(push_file=pushed)
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    begun = []

    async def _begin(agent, rel, **kw):
        begun.append(kw.get("machine_ids"))
        return "tid"

    monkeypatch.setattr(transfer_registry, "begin", _begin)
    # A link component: the strict open refuses, nothing is pushed, and a
    # tracked transfer still gets its terminal (no rows).
    await wf.fan_out_write("agent-1", "workspace/sub/x.md",
                           agents / "agent-1" / "workspace" / "sub" / "x.md", transfer_id="t1")
    pushed.assert_not_awaited()
    assert begun == [[]]
    # A Path that is not the platform copy of ``rel_path`` at all.
    await wf.fan_out_write("agent-1", "workspace/y.md", tmp_path / "x.md")
    pushed.assert_not_awaited()


@pytest.mark.asyncio
async def test_propagate_write_holds_the_fan_out_lock_inside_the_path_lock(temp_db, tmp_path, monkeypatch):
    import config
    from core.remote import remote_file_flow
    from services.remote import workspace_fanout as wf
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    seen = {}

    async def _fan_out(agent_slug, rel_path, source, **kw):
        path_lock = await remote_file_flow._acquire_global_path_lock(agent_slug, rel_path)
        fan_lock = await remote_file_flow.acquire_fanout_lock(agent_slug, rel_path)
        seen["path"] = path_lock.locked()
        seen["fanout"] = fan_lock.locked()

    monkeypatch.setattr(wf, "fan_out_write", _fan_out)
    await wf.propagate_write("agent-1", "workspace/x.md", b"hello")
    assert seen == {"path": True, "fanout": True}


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
    await workspace_fanout.fan_out_write("agent-1", "workspace/big.bin", b"x" * (600 * 1024))
    assert len(calls) <= 4
    assert calls[-1][0] == calls[-1][1]

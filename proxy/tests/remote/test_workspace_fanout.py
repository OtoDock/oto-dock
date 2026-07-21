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
from unittest.mock import AsyncMock

import pytest


def _h(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


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
    return SimpleNamespace(username=username, role=role)


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
    monkeypatch.setattr(wf, "fanout_targets", lambda a, r, *, exclude_machine_id=None: [])
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
    from storage import file_author_store, sync_state_store, recover_bin_store

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
        sec=SimpleNamespace(role="editor", username="bob", agent=agent, display_name="Bob"),
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
async def test_apply_same_user_no_conflict(temp_db, tmp_path, monkeypatch):
    import config
    from core.remote.satellite_connection import SatelliteConnectionManager
    from storage import file_author_store, recover_bin_store

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
        sec=SimpleNamespace(role="editor", username="bob", agent=agent, display_name="Bob"),
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
    from storage import file_author_store, sync_state_store, recover_bin_store

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
        sec=SimpleNamespace(role="editor", username="bob", agent=agent, display_name="Bob"),
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
    from storage import (
        file_author_store, sync_state_store, file_tombstones_store, recover_bin_store,
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
        sec=SimpleNamespace(role="editor", username="bob", agent=agent, display_name="Bob"),
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
        sec=SimpleNamespace(role="manager", username="bob", agent="real-agent", display_name="Bob"),
        loser_sub_for=lambda u: "user-viewer",
        notifs=notifs,
    )
    # Payload claims a DIFFERENT agent than the session's authenticated one.
    await cm._apply_file_changed("mBob", _write_msg("spoofed-agent", "workspace/x.md", b"data"))

    assert not (tmp_path / "spoofed-agent" / "workspace" / "x.md").exists()



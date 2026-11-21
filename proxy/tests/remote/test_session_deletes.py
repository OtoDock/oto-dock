"""A delete inside a session's sandbox (services/infra/file_bookkeeping.py):
the end-of-turn scan writes the tombstone the sandbox cannot, so a paired
machine's copy does not come back."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import config
from auth.path_policy import SecurityContext
from core import placement
from core.remote.file_sync import FileAction, diff_manifests
from core.remote.remote_workspace_apply import RemoteWorkspaceApplyMixin, _SyncRun
from core.session import session_events, session_state
from services.infra import file_bookkeeping as fb
from storage.files import file_tombstones_store as tombstones, sync_state_store
from storage.pg import get_conn

AGENT = "alpha"
MACHINE = "machine-1"


def _ctx(**over) -> SecurityContext:
    base = dict(role="manager", username="alice", agent=AGENT, is_admin_agent=False)
    base.update(over)
    return SecurityContext(**base)


def _agent_dir() -> Path:
    return Path(config.AGENTS_DIR) / AGENT


def _paired(machine: str = MACHINE) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO remote_machines (id, name, registered_by, status, created_at) "
            "VALUES (%s,%s,'user-admin',%s,%s) ON CONFLICT DO NOTHING",
            (machine, machine, "online", "2026-01-01T00:00:00+00:00"))
        conn.commit()


def _track(*rels: str, machine: str = MACHINE, write: bool = True) -> None:
    _paired(machine)
    for rel in rels:
        sync_state_store.record_one(machine, AGENT, rel, "sha256:b", 1.0)
        if write:
            path = _agent_dir() / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")


def _gone(rel: str) -> None:
    (_agent_dir() / rel).unlink()


def _reset() -> None:
    fb._scans.clear()


def _prime() -> None:
    fb.prime_session_delete_watch()


def _scan(*contexts: SecurityContext) -> list[str]:
    return fb._scan_agent_sync(AGENT, list(contexts) or [_ctx()])


def _origin(rel: str) -> str | None:
    row = tombstones.get(AGENT, rel)
    return row["origin"] if row else None


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    _reset()
    # Rows read as converged before this process started, unless a test says.
    monkeypatch.setattr(fb, "_watching_since", "9999-01-01T00:00:00+00:00")
    yield
    _reset()


# ── what is recorded ────────────────────────────────────────────────────────


def test_a_file_a_session_removed_gets_a_tombstone(temp_db):
    _track("workspace/a.txt", "workspace/keep.txt")
    _prime()
    _gone("workspace/a.txt")

    assert _scan() == ["workspace/a.txt"]
    assert _origin("workspace/a.txt") == "session-delete"
    assert tombstones.get(AGENT, "workspace/keep.txt") is None


def test_the_merge_then_deletes_the_machines_copy_instead_of_pulling_it(temp_db):
    _track("workspace/a.txt", "workspace/keep.txt")
    _prime()
    _gone("workspace/a.txt")
    _scan()

    remote = [{"path": "workspace/a.txt", "hash": "sha256:b", "size": 1, "mtime": 100.0}]
    row = tombstones.get(AGENT, "workspace/a.txt")
    plan = diff_manifests([], remote, tombstones={"workspace/a.txt": row["deleted_at_mtime"]},
                          target_role="editor", target_username="alice")
    action = next(a for a in plan.actions if a.rel_path == "workspace/a.txt")

    assert action.op == "delete_satellite" and action.capture_reason == "deleted"


def test_a_remove_and_a_new_file_in_a_scan(temp_db):
    _track("workspace/old.txt", "workspace/keep.txt")
    _prime()
    _gone("workspace/old.txt")
    (_agent_dir() / "workspace" / "new.txt").write_text("y")

    assert _scan() == ["workspace/old.txt"]


# ── what is not ─────────────────────────────────────────────────────────────


def test_a_file_still_there_is_left_alone(temp_db):
    _track("workspace/a.txt")
    _prime()

    assert _scan() == []
    assert tombstones.get(AGENT, "workspace/a.txt") is None


def test_an_absence_older_than_the_boot_is_never_acted_on(temp_db):
    """A 1.9.1 install's old ``rm``s stay as they were."""
    _track("workspace/a.txt", "workspace/keep.txt")
    _gone("workspace/a.txt")
    _prime()

    assert _scan() == []
    assert tombstones.get(AGENT, "workspace/a.txt") is None


def test_the_first_scan_of_an_unprimed_agent_only_looks(temp_db):
    _track("workspace/a.txt", "workspace/keep.txt")
    _gone("workspace/a.txt")

    assert _scan() == []
    assert tombstones.get(AGENT, "workspace/a.txt") is None


def test_a_first_scan_hears_a_delete_of_a_file_a_machine_synced_after_the_boot(temp_db, monkeypatch):
    """An agent first paired after the boot: its row says the file was there."""
    monkeypatch.setattr(fb, "_watching_since", "2000-01-01T00:00:00+00:00")
    _track("workspace/new.txt", "workspace/keep.txt")
    _gone("workspace/new.txt")

    assert _scan() == ["workspace/new.txt"]
    assert _origin("workspace/new.txt") == "session-delete"


def test_a_path_is_decided_once_and_an_expired_tombstone_is_not_rearmed(temp_db):
    _track("workspace/a.txt", "workspace/keep.txt")
    _prime()
    _gone("workspace/a.txt")
    assert _scan() == ["workspace/a.txt"]

    # The tombstone reached its 30 days: the row is gone.
    with get_conn() as conn:
        conn.execute("DELETE FROM file_tombstones WHERE agent_slug=%s", (AGENT,))
        conn.commit()

    assert _scan() == []
    assert tombstones.get(AGENT, "workspace/a.txt") is None


def test_a_live_tombstone_is_left_and_the_path_joins_the_standing_set(temp_db):
    _track("workspace/a.txt", "workspace/keep.txt")
    _prime()
    tombstones.record(AGENT, "workspace/a.txt", 5.0, origin="dashboard")
    _gone("workspace/a.txt")

    assert _scan() == []
    assert _origin("workspace/a.txt") == "dashboard"
    assert "workspace/a.txt" in fb._scans[AGENT].standing


def test_a_file_back_again_leaves_the_standing_set_and_a_later_delete_counts(temp_db):
    _track("workspace/a.txt", "workspace/keep.txt")
    _gone("workspace/a.txt")
    _prime()
    (_agent_dir() / "workspace" / "a.txt").write_text("again")
    assert _scan() == []
    _gone("workspace/a.txt")

    assert _scan() == ["workspace/a.txt"]


def test_a_path_outside_what_the_session_may_write_waits_for_its_own(temp_db):
    _track("users/bob/workspace/b.txt", "users/bob/workspace/keep.txt", "workspace/keep.txt")
    _prime()
    _gone("users/bob/workspace/b.txt")

    assert _scan(_ctx(username="alice")) == []
    assert tombstones.get(AGENT, "users/bob/workspace/b.txt") is None

    assert _scan(_ctx(username="bob", role="member")) == ["users/bob/workspace/b.txt"]


def test_a_viewer_on_the_shared_workspace_records_nothing(temp_db):
    _track("workspace/a.txt", "workspace/keep.txt")
    _prime()
    _gone("workspace/a.txt")

    assert _scan(_ctx(role="viewer")) == []
    assert tombstones.get(AGENT, "workspace/a.txt") is None


def test_a_mirror_and_config_are_left_alone(temp_db):
    _track("knowledge/shared/lib/x.md", "config/context/y.md", "workspace/keep.txt")
    _prime()
    _gone("knowledge/shared/lib/x.md")
    _gone("config/context/y.md")

    assert _scan() == []


def test_a_removed_machines_rows_are_not_tracked(temp_db):
    sync_state_store.record_one("machine-gone", AGENT, "workspace/ghost.txt", "sha256:b", 1.0)

    assert fb._tracked_paths(AGENT) == {}


def test_a_link_at_the_name_is_not_absent(temp_db, tmp_path):
    _track("workspace/a.txt", "workspace/keep.txt")
    _prime()
    target = tmp_path / "elsewhere"
    target.write_text("t")
    path = _agent_dir() / "workspace" / "a.txt"
    path.unlink()
    path.symlink_to(target)

    assert _scan() == []
    assert target.exists()


def test_a_link_on_the_way_is_not_absent(temp_db, tmp_path):
    _track("workspace/dir/a.txt", "workspace/keep.txt")
    _prime()
    folder = _agent_dir() / "workspace" / "dir"
    (folder / "a.txt").unlink()
    folder.rmdir()
    elsewhere = tmp_path / "dir"
    elsewhere.mkdir()
    folder.symlink_to(elsewhere)

    assert _scan() == []


def test_an_io_error_is_not_absent(temp_db, monkeypatch):
    _track("workspace/a.txt", "workspace/keep.txt")
    _prime()

    def boom(_root, rel):
        if rel.endswith("a.txt"):
            raise PermissionError(rel)
        return Path(_agent_dir() / "workspace" / "keep.txt").lstat()

    monkeypatch.setattr(fb.safe_fs, "lstat_beneath", boom)

    assert _scan() == []


# ── a rename in case only ───────────────────────────────────────────────────


def test_a_case_only_rename_of_a_file_writes_no_tombstone(temp_db):
    """A machine that folds case holds one file under both names."""
    _track("workspace/Report.pdf", "workspace/keep.txt")
    _prime()
    folder = _agent_dir() / "workspace"
    (folder / "Report.pdf").rename(folder / "report.pdf")

    assert _scan() == []
    assert tombstones.get(AGENT, "workspace/Report.pdf") is None
    assert "workspace/Report.pdf" in fb._scans[AGENT].standing


def test_a_case_only_rename_of_a_folder_writes_no_tombstone(temp_db):
    _track("workspace/Docs/a.txt", "workspace/Docs/sub/b.txt", "workspace/keep.txt")
    _prime()
    folder = _agent_dir() / "workspace"
    (folder / "Docs").rename(folder / "docs")

    assert _scan() == []
    assert tombstones.get(AGENT, "workspace/Docs/a.txt") is None
    assert tombstones.get(AGENT, "workspace/Docs/sub/b.txt") is None


def test_a_rename_in_unicode_form_only_writes_no_tombstone(temp_db):
    """A default macOS volume holds the two forms of one name as one file."""
    composed, decomposed = "caf\u00e9.pdf", "cafe\u0301.pdf"
    _track(f"workspace/{composed}", "workspace/keep.txt")
    _prime()
    folder = _agent_dir() / "workspace"
    (folder / composed).rename(folder / decomposed)

    assert _scan() == []


def test_a_sibling_link_in_another_case_counts(temp_db, tmp_path):
    _track("workspace/Report.pdf", "workspace/keep.txt")
    _prime()
    _gone("workspace/Report.pdf")
    (_agent_dir() / "workspace" / "report.pdf").symlink_to(tmp_path)

    assert _scan() == []


def test_a_sibling_with_another_name_is_no_excuse(temp_db):
    _track("workspace/Report.pdf", "workspace/keep.txt")
    _prime()
    folder = _agent_dir() / "workspace"
    (folder / "Report.pdf").rename(folder / "Report-final.pdf")

    assert _scan() == ["workspace/Report.pdf"]


def test_a_folder_replaced_by_a_file_is_a_delete(temp_db):
    _track("workspace/thing/a.txt", "workspace/keep.txt")
    _prime()
    folder = _agent_dir() / "workspace"
    (folder / "thing" / "a.txt").unlink()
    (folder / "thing").rmdir()
    (folder / "thing").write_text("now a file")

    assert _scan() == ["workspace/thing/a.txt"]


def test_a_folder_that_cannot_be_listed_writes_no_tombstone(temp_db, monkeypatch):
    _track("workspace/a.txt", "workspace/keep.txt")
    _prime()
    _gone("workspace/a.txt")

    def refused(*_a, **_k):
        raise PermissionError("no")

    monkeypatch.setattr(fb.safe_fs, "walk_beneath", refused)

    assert _scan() == []
    assert tombstones.get(AGENT, "workspace/a.txt") is None


def test_a_folder_of_many_deletes_is_listed_once(temp_db, monkeypatch):
    many = [f"workspace/many/{i}.txt" for i in range(20)]
    _track(*many, "workspace/keep.txt", *[f"workspace/other/{i}.txt" for i in range(40)])
    _prime()
    for rel in many:
        _gone(rel)
    listed: list[str] = []
    real = fb.safe_fs.walk_beneath

    def spy(root, rel="", **kw):
        listed.append(rel)
        return real(root, rel, **kw)

    monkeypatch.setattr(fb.safe_fs, "walk_beneath", spy)

    assert len(_scan()) == 20
    assert len(listed) == len(set(listed))


# ── the guards ──────────────────────────────────────────────────────────────


def test_a_scope_with_nothing_left_writes_nothing(temp_db, caplog):
    rels = [f"users/alice/workspace/{i}.txt" for i in range(5)]
    _track(*rels, "workspace/keep.txt")
    _prime()
    for rel in rels:
        _gone(rel)

    assert _scan(_ctx(username="alice")) == []
    assert "looks like a lost folder" in caplog.text
    assert all(tombstones.get(AGENT, rel) is None for rel in rels)


def test_a_scope_folder_that_is_itself_gone_writes_nothing(temp_db):
    """A lost mount reads as every file deleted, however few it tracks."""
    import shutil
    _track("users/alice/workspace/a.txt", "workspace/keep.txt")
    _prime()
    shutil.rmtree(_agent_dir() / "users" / "alice")

    assert _scan(_ctx(username="alice")) == []
    assert tombstones.get(AGENT, "users/alice/workspace/a.txt") is None
    assert "users/alice/workspace/a.txt" in fb._scans[AGENT].standing


def test_a_small_scope_losing_its_one_file_is_a_real_delete(temp_db):
    _track("users/alice/workspace/only.txt", "workspace/keep.txt")
    _prime()
    _gone("users/alice/workspace/only.txt")

    assert _scan(_ctx(username="alice")) == ["users/alice/workspace/only.txt"]


def test_fifty_paths_and_half_of_a_scope_write_nothing(temp_db, caplog):
    many = [f"workspace/many/{i}.txt" for i in range(50)]
    rest = [f"workspace/rest/{i}.txt" for i in range(50)]
    _track(*many, *rest)
    _prime()
    for rel in many:
        _gone(rel)

    assert _scan() == []
    assert "looks like a lost folder" in caplog.text


def test_a_mass_delete_joins_the_standing_set_and_is_not_reported_again(temp_db):
    many = [f"workspace/many/{i}.txt" for i in range(50)]
    rest = [f"workspace/rest/{i}.txt" for i in range(50)]
    _track(*many, *rest)
    _prime()
    for rel in many:
        _gone(rel)
    _scan()

    assert _scan() == []
    assert set(many) <= fb._scans[AGENT].standing.keys()


def test_many_deletes_below_half_of_a_scope_are_recorded(temp_db):
    many = [f"workspace/many/{i}.txt" for i in range(50)]
    rest = [f"workspace/rest/{i}.txt" for i in range(60)]
    _track(*many, *rest)
    _prime()
    for rel in many:
        _gone(rel)

    assert len(_scan()) == 50


# ── a file that came back ───────────────────────────────────────────────────


async def _written_again_by(machine: str, rel: str, data: bytes, monkeypatch) -> float:
    """A paired machine's live write of ``rel`` through the applier, which
    leaves a delete record standing: the platform holds the file again and
    the machine's base names it. Returns when it landed (epoch)."""
    import base64
    import hashlib
    import time
    from types import SimpleNamespace

    from core.remote.satellite_connection import SatelliteConnectionManager
    from services.remote import workspace_fanout

    sec = SimpleNamespace(
        agent=AGENT, username=machine, role="editor", mount_username=None, knowledge_rw=False,
        placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id=machine))
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: sec)
    monkeypatch.setattr(workspace_fanout, "fanout_targets", lambda *a, **k: [])
    _paired(machine)
    await SatelliteConnectionManager()._apply_file_changed(machine, {
        "type": "file_changed", "session_id": f"sess-{machine}", "agent_slug": AGENT,
        "path": rel, "action": "write", "content_b64": base64.b64encode(data).decode(),
        "hash": "sha256:" + hashlib.sha256(data).hexdigest()})
    assert (_agent_dir() / rel).read_bytes() == data
    return time.time()


def _merge_op(rel: str, data: bytes, made_at: float, who: str) -> list[str]:
    """What a machine's merge does with its copy of ``rel`` made at ``made_at``,
    the platform lacking it, against the agent's live records."""
    import hashlib
    plan = diff_manifests(
        [], [{"path": rel, "hash": "sha256:" + hashlib.sha256(data).hexdigest(),
              "size": len(data), "mtime": made_at}],
        tombstones=tombstones.load_for_agent(AGENT), clock_offset=0.0,
        target_role="editor", target_username=who)
    return [a.op for a in plan.actions if a.rel_path == rel]


@pytest.mark.asyncio
async def test_a_file_a_machine_wrote_again_and_a_session_removed_is_recorded_again(temp_db, monkeypatch):
    """Deleted with a record, written again by a paired machine's live write,
    removed by a session on the platform. The record is from before the file
    came back, so the scan writes one of its own, and that machine's merge
    deletes its copy instead of pulling it back."""
    import time
    rel = "workspace/plan.md"
    _track("workspace/keep.txt")
    tombstones.record(AGENT, rel, time.time() - 600, origin="live-delete")
    _prime()
    written_at = await _written_again_by("carol", rel, b"v2", monkeypatch)
    _gone(rel)

    assert _scan() == [rel]
    assert _origin(rel) == "session-delete"
    assert _merge_op(rel, b"v2", written_at, "carol") == ["delete_satellite"]


@pytest.mark.asyncio
async def test_a_decided_path_a_machine_wrote_again_is_decided_again(temp_db, monkeypatch):
    """A session's delete is recorded and the path stands. A machine writes it
    again and a session removes it again with no scan in between. The
    machine's newer base takes the path off the standing set, the scan records
    the delete, and both that machine's copy and the old copy of a machine
    away since before the first delete are deleted at their merges."""
    import time
    rel = "workspace/plan.md"
    old_at = time.time() - 3600
    _track(rel, "workspace/keep.txt")
    _track(rel, machine="dan")
    _prime()
    _gone(rel)
    assert _scan() == [rel]
    written_at = await _written_again_by("carol", rel, b"v2", monkeypatch)
    _gone(rel)

    assert _scan() == [rel]
    assert _merge_op(rel, b"v2", written_at, "carol") == ["delete_satellite"]
    assert _merge_op(rel, b"v1", old_at, "dan") == ["delete_satellite"]


def test_a_hold_still_stops_the_scan_after_a_machine_converged(temp_db):
    """A hold is never a delete, however old it is next to the bases."""
    rel = "workspace/a.txt"
    tombstones.record(AGENT, rel, 1.0, origin=tombstones.HOLD_ORIGIN)
    _track(rel, "workspace/keep.txt")
    _prime()
    _gone(rel)

    assert _scan() == []
    assert _origin(rel) == tombstones.HOLD_ORIGIN


def test_a_record_stamped_at_the_newest_bases_instant_stands(temp_db, monkeypatch):
    """Both stamps come from the proxy's own clock. Equal ones read as the
    record being current: doubt writes nothing new."""
    from datetime import datetime, timezone
    at = datetime(2026, 11, 15, 18, 0, 0, 123456, tzinfo=timezone.utc)
    monkeypatch.setattr(sync_state_store, "_now_iso", at.isoformat)
    monkeypatch.setattr(tombstones, "_now", lambda: at)
    rel = "workspace/a.txt"
    _track(rel, "workspace/keep.txt")
    tombstones.record(AGENT, rel, 5.0, origin="dashboard")
    _prime()
    _gone(rel)

    assert _scan() == []
    assert _origin(rel) == "dashboard"


# ── a write that did not land ───────────────────────────────────────────────

_BIG = 5 * 1024 * 1024


async def _large_write_not_landed(machine: str, rel: str, new_hash: str, monkeypatch) -> AsyncMock:
    """A paired machine reports a write above the inline size, and the
    platform's pull of it fails (a link that dropped, a stall, a refused
    read): the platform never holds those bytes."""
    from types import SimpleNamespace

    from core.remote.satellite_connection import SatelliteConnectionManager
    from services.remote import workspace_fanout

    sec = SimpleNamespace(
        agent=AGENT, username=machine, role="editor", mount_username=None, knowledge_rw=False,
        placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id=machine))
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: sec)
    monkeypatch.setattr(workspace_fanout, "fanout_targets", lambda *a, **k: [])
    pull = AsyncMock(return_value=False)
    monkeypatch.setattr(SatelliteConnectionManager, "pull_file_to_path", pull)
    _paired(machine)
    await SatelliteConnectionManager()._apply_file_changed(machine, {
        "type": "file_changed", "session_id": f"sess-{machine}", "agent_slug": AGENT,
        "path": rel, "action": "write", "size": _BIG, "hash": new_hash})
    return pull


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", [True, False])
async def test_a_large_write_whose_pull_failed_is_no_file_that_came_back(temp_db, monkeypatch, recorded):
    """Carol writes a large file on her laptop, deleted before with a record
    or new, and the platform's pull of it fails. Her laptop holds the only
    copy: no base is recorded, so a platform session's turn end records no
    delete, and her next merge brings the copy to the platform."""
    import time
    rel = "workspace/render.bin"
    _track("workspace/keep.txt")
    if recorded:
        tombstones.record(AGENT, rel, time.time() - 600, origin="dashboard")
    _prime()
    made_at = time.time() - 5
    new_hash = "sha256:" + "c" * 64

    pull = await _large_write_not_landed("carol", rel, new_hash, monkeypatch)

    pull.assert_awaited_once()
    assert not (_agent_dir() / rel).exists()
    assert sync_state_store.get_one("carol", AGENT, rel) is None
    assert _scan() == []
    assert _origin(rel) == ("dashboard" if recorded else None)
    plan = diff_manifests([], [{"path": rel, "hash": new_hash, "size": _BIG, "mtime": made_at}],
                          tombstones=tombstones.load_for_agent(AGENT), clock_offset=0.0,
                          target_role="editor", target_username="carol")
    assert [a.op for a in plan.actions if a.rel_path == rel] == ["pull"]


# ── the turn's end ──────────────────────────────────────────────────────────


def _end_turn(sid: str, ctx: SecurityContext, monkeypatch) -> None:
    monkeypatch.setattr(session_state, "get_session_security", lambda s: ctx if s == sid else None)

    tasks: list[asyncio.Future] = []
    monkeypatch.setattr(fb, "spawn", lambda coro, **_: tasks.append(asyncio.ensure_future(coro)))

    async def go():
        await fb._on_turn_end(session_events.TurnEndContext(session_id=sid, source="loop"))
        await asyncio.gather(*tasks)

    asyncio.run(go())


def _placed(kind: str) -> placement.PlacementCapabilities:
    if kind == placement.KIND_LOCAL:
        return placement.PlacementCapabilities(kind=kind, machine_id="")
    return placement.PlacementCapabilities(kind=kind, machine_id="m", label="m", os="linux",
                                           home_dir="/home/m", agents_dir="/home/m/.oto-dock/agents",
                                           os_user="m", allow_full_fs=False)


@pytest.mark.parametrize("kind", [placement.KIND_LOCAL, placement.KIND_COMPUTE])
def test_a_local_or_compute_session_is_scanned(temp_db, monkeypatch, kind):
    _track("workspace/a.txt", "workspace/keep.txt")
    _prime()
    _gone("workspace/a.txt")

    _end_turn("s-1", _ctx(placement=_placed(kind)), monkeypatch)

    assert _origin("workspace/a.txt") == "session-delete"


@pytest.mark.parametrize("kind", [placement.KIND_ADMIN_REMOTE, placement.KIND_USER_REMOTE])
def test_a_paired_machines_session_is_not_scanned(temp_db, monkeypatch, kind):
    _track("workspace/a.txt", "workspace/keep.txt")
    _prime()
    _gone("workspace/a.txt")

    _end_turn("s-1", _ctx(placement=_placed(kind)), monkeypatch)

    assert tombstones.get(AGENT, "workspace/a.txt") is None


def test_a_judge_is_not_scanned(temp_db, monkeypatch):
    _track("workspace/a.txt", "workspace/keep.txt")
    _prime()
    _gone("workspace/a.txt")
    monkeypatch.setitem(session_state._sessions, "s-judge", {"check": True})

    _end_turn("s-judge", _ctx(placement=_placed(placement.KIND_LOCAL)), monkeypatch)

    assert tombstones.get(AGENT, "workspace/a.txt") is None


def test_a_second_sessions_scope_is_not_lost_to_a_running_scan(temp_db, monkeypatch):
    _track("workspace/a.txt", "users/bob/workspace/b.txt", "workspace/keep.txt",
           "users/bob/workspace/keep.txt")
    _prime()
    _gone("workspace/a.txt")
    _gone("users/bob/workspace/b.txt")
    state = fb._scan_state(AGENT)
    seen: list[list[str]] = []
    real = fb._scan_agent_sync

    def scan(agent, contexts):
        seen.append(sorted(c.username for c in contexts))
        return real(agent, contexts)

    monkeypatch.setattr(fb, "_scan_agent_sync", scan)
    state.pending["s-1"] = _ctx(username="alice", role="manager")
    state.pending["s-2"] = _ctx(username="bob", role="member")

    asyncio.run(fb._drain_scans(AGENT))

    assert seen == [["alice", "bob"]]
    assert _origin("workspace/a.txt") == "session-delete"
    assert _origin("users/bob/workspace/b.txt") == "session-delete"


def test_a_scan_with_more_waiting_rests_before_the_next(temp_db, monkeypatch):
    state = fb._scan_state(AGENT)
    state.pending["s-1"] = _ctx()
    calls: list[int] = []
    rests: list[float] = []

    def scan(_agent, _contexts):
        calls.append(1)
        if len(calls) == 1:
            state.pending["s-2"] = _ctx()
        return []

    async def rest(seconds):
        rests.append(seconds)

    monkeypatch.setattr(fb, "_scan_agent_sync", scan)
    monkeypatch.setattr(fb.asyncio, "sleep", rest)

    asyncio.run(fb._drain_scans(AGENT))

    assert len(calls) == 2 and rests == [fb._SCAN_REST_S] and fb._SCAN_REST_S >= 30


# ── a machine's copy when the delete was inferred ───────────────────────────


class _Merge(RemoteWorkspaceApplyMixin):
    def __init__(self, data: bytes | None):
        self._data = data
        self.pulled: list[str] = []
        self._cm = AsyncMock()

    async def _sync_pull_bytes(self, run, rp):
        self.pulled.append(rp)
        return self._data


def _run(rp: str, size: int) -> _SyncRun:
    run = _SyncRun(machine_id=MACHINE, agent_slug=AGENT, agent_dir=_agent_dir(), progress_cb=None,
                   plan=None, local_entries=[], remote_size={rp: size}, remote_stat={}, satellite_user="")
    run.n = {"capture": 0, "delete": 0}
    return run


def _delete(merge: _Merge, rp: str, size: int) -> bool:
    action = FileAction(rp, "delete_satellite", clear_base=True, capture_side="satellite",
                        capture_reason="deleted")
    return asyncio.run(merge._sync_delete_satellite(_run(rp, size), action, rp))


REL = "workspace/film.mp4"


def _deletes_sent(merge: _Merge) -> int:
    return merge._cm.send_fire_and_forget.await_count


def _inferred(origin: str = "session-delete", rel: str = REL) -> None:
    _track(rel, write=False)
    tombstones.record(AGENT, rel, 5.0, origin=origin)


# A session's delete the scan inferred, and an upload the sweep aged out:
# nobody asked for either, so a machine's copy is held to the same rule.
UNASKED = ["session-delete", "retention"]


@pytest.mark.parametrize("origin", UNASKED)
def test_an_inferred_delete_leaves_a_copy_too_big_for_the_bin(temp_db, monkeypatch, origin):
    monkeypatch.setattr(config, "RECOVER_BIN_MAX_BYTES", 10)
    _inferred(origin)
    merge = _Merge(b"x" * 11)

    assert _delete(merge, REL, 11) is False

    assert merge.pulled == [] and _deletes_sent(merge) == 0
    assert tombstones.get(AGENT, REL) is not None
    assert sync_state_store.get_one(MACHINE, AGENT, REL) is not None


@pytest.mark.parametrize("origin", UNASKED)
def test_an_inferred_delete_leaves_a_copy_the_bin_did_not_take(temp_db, monkeypatch, origin):
    from storage.files import recover_bin_store
    monkeypatch.setattr(recover_bin_store, "capture", lambda *a, **k: None)
    _inferred(origin)
    merge = _Merge(b"the only copy")

    assert _delete(merge, REL, 13) is False

    assert _deletes_sent(merge) == 0
    assert sync_state_store.get_one(MACHINE, AGENT, REL) is not None


@pytest.mark.parametrize("origin", UNASKED)
def test_an_inferred_delete_leaves_a_copy_when_the_bin_has_no_room(temp_db, monkeypatch, origin):
    """The next copy must not push out the one before it."""
    from storage.files import recover_bin_store
    monkeypatch.setattr(config, "RECOVER_BIN_AGENT_MAX_BYTES", 15)
    other = "workspace/clip-b.mp4"
    _inferred(origin)
    _inferred(origin, other)
    first, second = _Merge(b"x" * 10), _Merge(b"y" * 10)

    _delete(first, REL, 10)

    assert _delete(second, other, 10) is False
    assert _deletes_sent(first) == 1 and _deletes_sent(second) == 0
    assert recover_bin_store.already_binned(AGENT, REL, b"x" * 10)
    assert tombstones.get(AGENT, other) is not None
    assert sync_state_store.get_one(MACHINE, AGENT, other) is not None


def test_a_delete_that_was_asked_for_still_makes_room_in_the_bin(temp_db, monkeypatch):
    from storage.files import recover_bin_store
    monkeypatch.setattr(config, "RECOVER_BIN_AGENT_MAX_BYTES", 15)
    assert recover_bin_store.capture(AGENT, "workspace/older.txt", b"x" * 10, "deleted") is not None
    _inferred("dashboard")
    merge = _Merge(b"y" * 10)

    _delete(merge, REL, 10)

    assert _deletes_sent(merge) == 1
    assert recover_bin_store.already_binned(AGENT, REL, b"y" * 10)
    assert not recover_bin_store.already_binned(AGENT, "workspace/older.txt", b"x" * 10)


@pytest.mark.parametrize("origin", UNASKED)
@pytest.mark.parametrize("rel", ["workspace/proj/.config/x.txt", "workspace/proj/.codex/x.txt"])
def test_a_path_the_bin_never_takes_stays_without_a_pull(temp_db, origin, rel):
    """Pulled at every merge for the tombstone's 30 days otherwise."""
    _inferred(origin, rel)
    merge = _Merge(b"bytes")

    assert _delete(merge, rel, 5) is False

    assert merge.pulled == [] and _deletes_sent(merge) == 0
    assert tombstones.get(AGENT, rel) is not None


def test_an_inferred_delete_goes_on_once_the_bin_holds_the_bytes(temp_db):
    from storage.files import recover_bin_store
    _inferred()
    assert recover_bin_store.capture(AGENT, REL, b"the bytes", "deleted") is not None
    merge = _Merge(b"the bytes")

    _delete(merge, REL, 9)

    assert _deletes_sent(merge) == 1
    assert sync_state_store.get_one(MACHINE, AGENT, REL) is None


def test_an_inferred_delete_with_its_capture_goes_on(temp_db):
    from storage.files import recover_bin_store
    _inferred()
    merge = _Merge(b"the bytes")

    _delete(merge, REL, 9)

    assert _deletes_sent(merge) == 1
    assert recover_bin_store.already_binned(AGENT, REL, b"the bytes")


def test_an_inferred_delete_of_an_empty_copy_goes_on(temp_db):
    """Nothing to lose, and kept it would come back after the tombstone's 30 days."""
    _inferred()
    merge = _Merge(b"")

    _delete(merge, REL, 0)

    assert _deletes_sent(merge) == 1


def test_a_delete_that_was_asked_for_goes_on_as_before(temp_db, monkeypatch):
    from storage.files import recover_bin_store
    monkeypatch.setattr(recover_bin_store, "capture", lambda *a, **k: None)
    _inferred("dashboard")
    merge = _Merge(b"bytes")

    _delete(merge, REL, 5)

    assert _deletes_sent(merge) == 1


def test_the_handler_is_registered_before_the_checks():
    """A check's continue ends the handler loop, so this one must come first:
    the two calls in startup keep that order."""
    source = (Path(__file__).parents[2] / "startup.py").read_text()
    assert source.index("install_session_delete_watch()") < source.index("_checks.install()")

"""The working-files window (services/infra/working_files.py): what ages out
of a workspace, and above all what never does."""

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
from services.infra import file_bookkeeping, retention, safe_fs, working_files
from services.infra.retention import LiveSnapshot
from storage import database as task_store
from storage.files import db_file_pins, file_tombstones_store, recover_bin_store
from storage.pg import get_conn

AGENT = "alpha"
OLD = 400
FRESH = 3


def _past(days: int) -> float:
    return time.time() - days * 86400


def _put(rel: str, *, agent: str = AGENT, days: int = OLD, data: bytes = b"the bytes") -> Path:
    """A file whose mtime and ctime both read ``days`` old.  ctime cannot be
    set, so ``_age`` patches the pass's stat through ``os.stat``."""
    path = Path(config.AGENTS_DIR) / agent / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = _past(days)
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture(autouse=True)
def _old_ctimes(monkeypatch):
    """Files written by a test have a ctime of now; the age is the newer of
    mtime and ctime. Read the ctime as the mtime, except where a test
    plants a fresh ctime on purpose (``_fresh_ctime``)."""
    real_stat = os.stat
    real_lstat = safe_fs.lstat_beneath

    class _St:
        def __init__(self, st):
            self._st = st

        def __getattr__(self, name):
            if name == "st_ctime" and self._st.st_mtime not in _fresh_ctime:
                return self._st.st_mtime
            return getattr(self._st, name)

    def _stat(path, *a, **k):
        return _St(real_stat(path, *a, **k))

    def _lstat(root, rel):
        return _St(real_lstat(root, rel))

    monkeypatch.setattr(working_files.os, "stat", _stat)
    monkeypatch.setattr(file_bookkeeping.safe_fs, "lstat_beneath", _lstat)


_fresh_ctime: set[float] = set()


def _grace_over() -> None:
    task_store.set_platform_setting(
        working_files.STARTED_KEY,
        (datetime.now(timezone.utc) - timedelta(days=working_files.GRACE_DAYS + 1)).isoformat())


def _stats() -> dict:
    return retention._run_sweep_sync(180, False, LiveSnapshot(), dry_run=False)


def _dry() -> dict:
    return retention._run_sweep_sync(180, False, LiveSnapshot(), dry_run=True)


def _binned(agent: str = AGENT) -> list[str]:
    with get_conn() as conn:
        return [r["rel_path"] for r in conn.execute(
            "SELECT rel_path FROM recover_bin WHERE agent_slug=%s", (agent,)).fetchall()]


# ── what ages out ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("rel", ["workspace/inbox/peer/report.pdf",
                                 "users/alice/workspace/inbox/peer/report.pdf"])
def test_a_received_file_past_the_window_stays(temp_db, rel):
    """It may be the only copy: the sender can clean up or be removed."""
    _grace_over()
    path = _put(rel)

    stats = _stats()

    assert path.exists()
    assert file_tombstones_store.get(AGENT, rel) is None
    assert _binned() == []
    assert "working_inbox_deleted" not in stats


def test_an_old_upload_goes_to_the_bin_with_a_tombstone(temp_db):
    _grace_over()
    path = _put("users/alice/workspace/uploads/files/cv.pdf")

    stats = _stats()

    assert not path.exists()
    assert stats["working_uploads_binned"] == 1
    assert _binned() == ["users/alice/workspace/uploads/files/cv.pdf"]
    assert file_tombstones_store.get(AGENT, "users/alice/workspace/uploads/files/cv.pdf") is not None


def test_a_screenshot_goes_after_its_own_bound_whatever_the_window(temp_db):
    _grace_over()
    task_store.set_platform_setting(working_files.DAYS_KEY, "3650")
    old = _put("workspace/.screenshots/s1/a.png", days=45)
    young = _put("workspace/.screenshots/s1/b.png", days=10)

    stats = _stats()

    assert not old.exists() and young.exists()
    assert stats["working_screenshots_deleted"] == 1
    assert file_tombstones_store.get(AGENT, "workspace/.screenshots/s1/a.png") is None


def test_a_forced_screenshot_bound_is_honoured(temp_db, monkeypatch):
    _grace_over()
    monkeypatch.setattr(config, "_FORCED_SETTINGS", {working_files.SCREENSHOTS_DAYS_KEY: "90"})
    kept = _put("workspace/.screenshots/s1/a.png", days=45)
    gone = _put("workspace/.screenshots/s1/b.png", days=120)

    _stats()

    assert kept.exists() and not gone.exists()


# ── what never ages ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("rel", ["workspace/uploads/placed.pdf",
                                 "workspace/uploads/reports/placed.pdf",
                                 "users/alice/workspace/uploads/keep/placed.pdf"])
def test_a_file_a_person_placed_elsewhere_under_uploads_stays(temp_db, rel):
    _grace_over()
    path = _put(rel)

    _stats()

    assert path.exists() and _binned() == []


@pytest.mark.parametrize("folder", ["photos", "files"])
def test_both_folders_the_platform_writes_age(temp_db, folder):
    _grace_over()
    path = _put(f"workspace/uploads/{folder}/old.bin")

    _stats()

    assert not path.exists() and _binned() == [f"workspace/uploads/{folder}/old.bin"]


def test_only_the_platforms_folders_age(temp_db):
    _grace_over()
    keep = [
        _put("workspace/generated-assets/poster.png"),
        _put("workspace/report.docx"),
        _put("workspace/project/uploads.txt"),
        _put("workspace/project/uploads/files/deep.pdf"),
        _put("users/alice/workspace/notes.md"),
        _put("users/alice/memories/topic.md"),
        _put("knowledge/inbox/doc.md"),
        _put("config/context/uploads/x.md"),
    ]

    stats = _stats()

    assert all(p.exists() for p in keep)
    assert stats["working_uploads_binned"] == 0


def test_a_file_newer_than_the_window_stays(temp_db):
    _grace_over()
    path = _put("workspace/uploads/files/new.pdf", days=FRESH)

    _stats()

    assert path.exists()


def test_a_fresh_ctime_keeps_an_unpacked_file(temp_db):
    """An unpacked or copied file keeps its old mtime but gets a new ctime."""
    _grace_over()
    path = _put("workspace/uploads/files/unpacked.pdf")
    _fresh_ctime.add(path.stat().st_mtime)
    try:
        stats = _stats()
    finally:
        _fresh_ctime.clear()

    assert path.exists()
    assert stats["working_uploads_binned"] == 0


def test_a_file_touched_after_the_listing_stays(temp_db, monkeypatch):
    """The age is read again right before the capture."""
    _grace_over()
    path = _put("workspace/uploads/files/late.pdf")
    real = file_bookkeeping.safe_fs.lstat_beneath

    class _Fresh:
        def __init__(self, st):
            self._st = st

        st_ctime = time.time()

        def __getattr__(self, name):
            return getattr(self._st, name)

    monkeypatch.setattr(file_bookkeeping.safe_fs, "lstat_beneath",
                        lambda root, rel: _Fresh(real(root, rel)))

    stats = _stats()

    assert path.exists() and stats["working_uploads_binned"] == 0


def test_another_agents_folder_is_its_own_and_a_link_out_is_not_followed(temp_db, tmp_path):
    _grace_over()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"not ours")
    stamp = _past(OLD)
    os.utime(outside, (stamp, stamp))
    other = _put("workspace/project/keep.pdf", agent="beta")
    uploads = Path(config.AGENTS_DIR) / AGENT / "workspace" / "uploads" / "files"
    uploads.mkdir(parents=True)
    link_out = uploads / "out.pdf"
    os.symlink(outside, link_out)
    link_over = uploads / "over.pdf"
    os.symlink(other, link_over)
    dir_link = uploads.parent / "photos"
    dir_link.symlink_to(tmp_path)

    stats = _stats()

    assert outside.exists() and outside.read_bytes() == b"not ours"
    assert other.exists()
    assert link_out.is_symlink() and link_over.is_symlink() and dir_link.is_symlink()
    assert stats["working_uploads_binned"] == 0 and _binned() == []
    assert stats["working_files_errors"] == 0


def test_a_pinned_upload_stays(temp_db):
    _grace_over()
    task_store.create_chat("chat-pin", "user-admin", AGENT)
    db_file_pins.upsert_file_pin(agent=AGENT, rel_path="workspace/uploads/files/pinned.pdf",
                                 title="t", scope_chat_id="chat-pin")
    path = _put("workspace/uploads/files/pinned.pdf")

    stats = _stats()

    assert path.exists() and stats["working_uploads_binned"] == 0


def test_a_pinned_screenshot_stays(temp_db):
    _grace_over()
    task_store.create_chat("chat-pin", "user-admin", AGENT)
    db_file_pins.upsert_file_pin(agent=AGENT, rel_path="workspace/.screenshots/s/pinned.png",
                                 title="t", scope_chat_id="chat-pin")
    path = _put("workspace/.screenshots/s/pinned.png", days=45)

    _stats()

    assert path.exists()


def test_an_external_callers_tree_is_not_this_pass(temp_db):
    _grace_over()
    path = _put("externals/phone/caller/workspace/uploads/x.pdf")

    _stats()

    assert path.exists()


def test_the_offboarding_archive_is_not_an_agent(temp_db):
    _grace_over()
    path = _put("alpha/workspace/uploads/x.pdf", agent=".offboarded/alice")

    _stats()

    assert path.exists()


# ── the bin ─────────────────────────────────────────────────────────────────


def test_a_file_over_the_bins_size_stays_and_is_counted(temp_db, monkeypatch):
    _grace_over()
    monkeypatch.setattr(config, "RECOVER_BIN_MAX_BYTES", 5)
    path = _put("workspace/uploads/files/big.mov", data=b"0123456789")

    stats = _stats()

    assert path.exists()
    assert stats["working_uploads_too_large"] == 1 and _binned() == []


def test_the_sweep_leaves_half_the_bin_and_evicts_nobody(temp_db, monkeypatch):
    _grace_over()
    monkeypatch.setattr(config, "RECOVER_BIN_AGENT_MAX_BYTES", 40)
    theirs = recover_bin_store.capture(AGENT, "workspace/deleted-by-a-person.txt", b"x" * 30, "deleted")
    assert theirs is not None
    path = _put("workspace/uploads/files/next.pdf", data=b"y" * 20)

    stats = _stats()

    assert path.exists()
    assert stats["working_uploads_deferred"] == 1
    assert _binned() == ["workspace/deleted-by-a-person.txt"]


def test_room_left_is_half_the_cap_less_what_the_bin_holds(temp_db, monkeypatch):
    monkeypatch.setattr(config, "RECOVER_BIN_AGENT_MAX_BYTES", 100)
    assert recover_bin_store.room_left(AGENT) == 50
    assert recover_bin_store.capture(AGENT, "workspace/a.txt", b"x" * 40, "deleted") is not None
    assert recover_bin_store.room_left(AGENT) == 10
    assert recover_bin_store.capture(AGENT, "workspace/b.txt", b"y" * 40, "deleted") is not None
    assert recover_bin_store.room_left(AGENT) == 0
    monkeypatch.setattr(config, "RECOVER_BIN_AGENT_MAX_BYTES", 0)
    assert recover_bin_store.room_left(AGENT) is None


def test_a_capture_that_may_not_evict_refuses_instead(temp_db, monkeypatch):
    monkeypatch.setattr(config, "RECOVER_BIN_AGENT_MAX_BYTES", 50)
    theirs = recover_bin_store.capture(AGENT, "workspace/theirs.txt", b"x" * 40, "deleted")

    refused = recover_bin_store.capture(AGENT, "workspace/ours.txt", b"y" * 20, "deleted", evict=False)
    evicting = recover_bin_store.capture(AGENT, "workspace/next.txt", b"z" * 20, "deleted")

    assert refused is None and theirs is not None and evicting is not None
    assert _binned() == ["workspace/next.txt"]


def test_a_race_for_the_last_room_keeps_the_file_and_evicts_nobody(temp_db, monkeypatch):
    """Room was there when the sweep looked; another delete took it first."""
    _grace_over()
    monkeypatch.setattr(config, "RECOVER_BIN_AGENT_MAX_BYTES", 40)
    theirs = recover_bin_store.capture(AGENT, "workspace/theirs.txt", b"x" * 35, "deleted")
    assert theirs is not None
    monkeypatch.setattr(recover_bin_store, "room_left", lambda _agent: 100)
    path = _put("workspace/uploads/files/late.pdf", data=b"y" * 20)

    stats = _stats()

    assert path.exists() and stats["working_files_kept"] == 1
    assert _binned() == ["workspace/theirs.txt"]


def test_a_failed_capture_keeps_the_file(temp_db, monkeypatch):
    _grace_over()
    monkeypatch.setattr(recover_bin_store, "capture", lambda *a, **k: None)
    path = _put("workspace/uploads/files/keep.pdf")

    stats = _stats()

    assert path.exists()
    assert stats["working_files_kept"] == 1
    assert file_tombstones_store.get(AGENT, "workspace/uploads/files/keep.pdf") is None


def test_an_empty_upload_goes_without_a_bin_entry(temp_db):
    _grace_over()
    path = _put("workspace/uploads/files/empty.txt", data=b"")

    stats = _stats()

    assert not path.exists() and stats["working_uploads_binned"] == 1 and _binned() == []


def test_a_name_that_is_not_utf8_is_left_and_the_pass_goes_on(temp_db):
    _grace_over()
    folder = Path(config.AGENTS_DIR) / AGENT / "workspace" / "uploads" / "files"
    folder.mkdir(parents=True)
    odd = os.path.join(os.fsencode(folder), b"caf\xe9.pdf")
    with open(odd, "wb") as f:
        f.write(b"x")
    stamp = _past(OLD)
    os.utime(odd, (stamp, stamp))
    other = _put("workspace/uploads/files/plain.pdf")

    stats = _stats()

    assert os.path.exists(odd) and not other.exists()
    assert stats["working_files_errors"] == 1 and stats["working_uploads_binned"] == 1


# ── the settings and the grace ──────────────────────────────────────────────


def test_the_first_real_sweep_starts_the_grace_and_removes_nothing(temp_db):
    path = _put("workspace/uploads/files/old.pdf")

    stats = _stats()

    assert path.exists()
    assert stats["working_uploads_binned"] == 0 and stats["working_files_grace_until"]
    assert task_store.get_platform_setting(working_files.STARTED_KEY)


def test_nothing_goes_until_the_grace_is_over(temp_db):
    task_store.set_platform_setting(
        working_files.STARTED_KEY,
        (datetime.now(timezone.utc) - timedelta(days=working_files.GRACE_DAYS - 1)).isoformat())
    path = _put("workspace/uploads/files/old.pdf")

    _stats()

    assert path.exists()


def test_a_dry_run_counts_during_the_grace_and_writes_nothing(temp_db):
    path = _put("workspace/uploads/files/old.pdf")
    shot = _put("workspace/.screenshots/s/old.png")

    stats = _dry()

    assert path.exists() and shot.exists()
    assert stats["working_uploads_binned"] == 1
    assert stats["working_screenshots_deleted"] == 1
    assert stats["working_files_grace_until"]
    assert not task_store.get_platform_setting(working_files.STARTED_KEY)
    assert _binned() == []
    assert file_tombstones_store.get(AGENT, "workspace/uploads/files/old.pdf") is None


def test_a_dry_runs_counts_match_the_real_run(temp_db, monkeypatch):
    _grace_over()
    monkeypatch.setattr(config, "RECOVER_BIN_MAX_BYTES", 15)
    monkeypatch.setattr(config, "RECOVER_BIN_AGENT_MAX_BYTES", 30)
    _put("workspace/uploads/files/a.pdf", data=b"x" * 10, days=500)
    _put("workspace/uploads/files/b.pdf", data=b"y" * 10, days=450)
    _put("workspace/uploads/files/big.mov", data=b"z" * 20)
    _put("workspace/.screenshots/s/a.png")
    keys = [*working_files.STAT_KEYS]

    first, second = _dry(), _stats()
    dry = {k: first[k] for k in keys}
    real = {k: second[k] for k in keys}

    assert dry == real
    assert real["working_uploads_binned"] == 1 and real["working_uploads_deferred"] == 1
    assert real["working_uploads_too_large"] == 1


def test_the_toggle_off_stops_both(temp_db):
    _grace_over()
    task_store.set_platform_setting(working_files.ENABLED_KEY, "0")
    paths = [_put("workspace/uploads/files/a.pdf"), _put("workspace/.screenshots/s/a.png")]

    _stats()

    assert all(p.exists() for p in paths)


@pytest.mark.parametrize("bad", ["0", "-5", "3", "soon", "99999"])
def test_a_bad_forced_window_keeps_everything(temp_db, monkeypatch, bad):
    _grace_over()
    monkeypatch.setattr(config, "_FORCED_SETTINGS", {
        working_files.DAYS_KEY: bad, working_files.SCREENSHOTS_DAYS_KEY: bad})
    paths = [_put("workspace/uploads/files/a.pdf"), _put("workspace/.screenshots/s/a.png")]

    _stats()

    assert all(p.exists() for p in paths)
    assert _binned() == []


def test_forced_values_drive_the_window(temp_db, monkeypatch):
    _grace_over()
    monkeypatch.setattr(config, "_FORCED_SETTINGS", {
        working_files.ENABLED_KEY: "1", working_files.DAYS_KEY: "90"})
    gone = _put("workspace/uploads/files/a.pdf", days=120)
    stays = _put("workspace/uploads/files/b.pdf", days=60)

    _stats()

    assert not gone.exists() and stays.exists()


def test_a_sweep_with_the_pass_failing_still_finishes_the_others(temp_db, monkeypatch):
    def boom(_stats, _dry_run):
        raise RuntimeError("no")
    monkeypatch.setattr(working_files, "run_pass", boom)

    stats = _stats()

    assert stats["errors"] == 1 and stats["notebook_entries_pruned"] == 0


# ── the route ───────────────────────────────────────────────────────────────


def _admin():
    from auth.providers import UserContext
    return UserContext(sub="user-admin", email="a@b.c", name="A", role="admin")


def _put_settings(**fields):
    import asyncio

    from api.auth import platform as api
    return asyncio.run(api.set_platform_settings(api.PlatformSettingsRequest(**fields), user=_admin()))


@pytest.mark.parametrize("value", [7, 180, 3650])
def test_the_card_saves_a_window_in_range(temp_db, value):
    _put_settings(working_files_retention_days=str(value))

    assert task_store.get_platform_setting(working_files.DAYS_KEY) == str(value)


@pytest.mark.parametrize("value", ["0", "6", "3651", "-1", "soon", ""])
def test_the_card_refuses_a_window_out_of_range_and_writes_nothing(temp_db, value):
    """A typed 0 means keep on the card below it: it must never become 7 days."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as refused:
        _put_settings(working_files_retention_days=value, working_files_retention_enabled=False)

    assert refused.value.status_code == 400
    assert "turn this cleanup off" in refused.value.detail
    assert task_store.get_platform_setting(working_files.DAYS_KEY) in ("", None)
    assert task_store.get_platform_setting(working_files.ENABLED_KEY) in ("", None)


def test_the_card_still_turns_the_cleanup_off(temp_db):
    _put_settings(working_files_retention_enabled=False)

    assert task_store.get_platform_setting(working_files.ENABLED_KEY) == "0"


@pytest.mark.parametrize(("forced", "shown"), [("0", ""), ("soon", ""), ("3", ""), ("365", "365")])
def test_the_card_reads_a_forced_chat_rows_window_as_the_sweep_does(temp_db, monkeypatch, forced, shown):
    import asyncio

    from api.auth import platform as api
    monkeypatch.setattr(config, "_FORCED_SETTINGS", {"chat_rows_retention_days": forced})

    data = asyncio.run(api.get_platform_settings(user=_admin()))

    assert data["chat_rows_retention_days"] == shown

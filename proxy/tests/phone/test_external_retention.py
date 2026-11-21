"""Caller-data retention (services/infra/external_retention.py).

The externals/ trees, the phone conversations and the call-log rows age out
together under one window; live calls are never touched; ephemeral leftovers
are reaped regardless of the toggle; "Forget all" removes everything now.
Filesystem fixtures live under the test-redirected config.AGENTS_DIR.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from auth.providers import UserContext, get_current_user
from services.infra import external_retention as er, retention
from services.infra.retention import LiveSnapshot
from storage import database as task_store
from storage.files import file_author_store, file_tombstones_store
from storage.phone import phone_call_log_store

AGENT = "support"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _age(root: Path, days: float) -> None:
    old = time.time() - days * 86400
    for p in [root, *root.rglob("*")]:
        os.utime(p, (old, old), follow_symlinks=False)


def _tree(slug: str, *, age_days: float = 0.0, ephemeral: bool = False,
          agent: str = AGENT, channel: str = "phone") -> Path:
    base = Path(config.AGENTS_DIR) / agent / "externals" / channel
    home = (base / "_ephemeral" / slug) if ephemeral else (base / slug)
    (home / "workspace").mkdir(parents=True)
    (home / "context" / "memory").mkdir(parents=True)
    (home / "workspace" / "notes.md").write_text("hello caller")
    proj = home / ".claude" / "projects" / "-caller"
    proj.mkdir(parents=True)
    (proj / f"{uuid.uuid4()}.jsonl").write_text("x" * 100)
    if age_days:
        _age(home, age_days)
    return home


def _phone_chat(*, days_old: float, agent: str = AGENT, source_type: str = "phone",
                with_file: bool = True) -> tuple[str, str, Path | None]:
    chat_id, sid = str(uuid.uuid4()), str(uuid.uuid4())
    task_store.create_chat(chat_id, "phone", agent, "auto", source_type=source_type)
    task_store.update_chat(chat_id, session_id=sid)
    task_store.add_chat_message(chat_id, "user", "hi")
    iso = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE chats SET updated_at=%s WHERE id=%s", (iso, chat_id))
        conn.commit()
    f = None
    if with_file:
        d = Path(config.AGENTS_DIR) / agent / "workspace" / ".claude" / "projects" / "-workspace"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{sid}.jsonl"
        f.write_text("y" * 50)
    return chat_id, sid, f


def _call_row(days_old: float) -> int:
    iso = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    return phone_call_log_store.insert_call({
        "direction": "inbound", "outcome": "completed", "started_at": iso,
    })


def _sweep(live: LiveSnapshot | None = None, *, dry_run: bool = False) -> dict:
    return retention._run_sweep_sync(30, True, live or LiveSnapshot(), dry_run)


@pytest.fixture(autouse=True)
def _quiet_passes(monkeypatch):
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)


def _chat_exists(chat_id: str) -> bool:
    return task_store.get_chat(chat_id) is not None


# ---------------------------------------------------------------------------
# Trees
# ---------------------------------------------------------------------------

def test_aged_tree_removed_fresh_kept_bookkeeping_purged(temp_db):
    old = _tree("302101234567", age_days=100)
    fresh = _tree("302109999999", age_days=10)
    file_author_store.record(AGENT, "externals/phone/302101234567/workspace/notes.md", "caller")
    file_tombstones_store.record(AGENT, "externals/phone/302101234567/workspace/gone.md", 1.0)
    file_author_store.record(AGENT, "externals/phone/302109999999/workspace/notes.md", "caller")
    # An unrelated agent path with a LIKE-sensitive name must survive.
    file_author_store.record(AGENT, "externals/phone/3021012345678/workspace/notes.md", "caller")

    stats = _sweep()
    assert stats["errors"] == 0, stats
    assert not old.exists() and fresh.exists(), stats
    assert stats["callers_forgotten"] == 1 and stats["caller_bytes_freed"] > 0
    assert file_author_store.get(AGENT, "externals/phone/302101234567/workspace/notes.md") is None
    assert file_tombstones_store.get(AGENT, "externals/phone/302101234567/workspace/gone.md") is None
    assert file_author_store.get(AGENT, "externals/phone/302109999999/workspace/notes.md") == "caller"
    assert file_author_store.get(AGENT, "externals/phone/3021012345678/workspace/notes.md") == "caller"


def test_activity_is_the_newest_mtime(temp_db):
    """A read-only call touches the root; a write bumps a file — either keeps
    the tree."""
    touched = _tree("a1", age_days=100)
    os.utime(touched, None)                     # warmup touch, now
    written = _tree("a2", age_days=100)
    (written / "workspace" / "new.md").write_text("fresh")
    stale = _tree("a3", age_days=100)
    _sweep()
    assert touched.exists() and written.exists() and not stale.exists()


def test_busy_tree_is_skipped(temp_db):
    home = _tree("busy", age_days=100)
    live = LiveSnapshot(busy_external_homes={str(home.resolve())})
    stats = _sweep(live)
    assert home.exists() and stats["callers_busy_skipped"] == 1


def test_ephemeral_reaped_after_grace_even_when_disabled(temp_db):
    task_store.set_platform_setting("external_retention_enabled", "0")
    leftover = _tree(str(uuid.uuid4()), age_days=2, ephemeral=True)
    live_eph = _tree(str(uuid.uuid4()), ephemeral=True)
    durable_old = _tree("kept", age_days=400)
    stats = _sweep()
    assert not leftover.exists() and live_eph.exists()
    assert durable_old.exists()                 # aging is off
    assert stats["ephemeral_reaped"] == 1 and stats["caller_data_pass_skipped"] is True
    assert stats["callers_forgotten"] == 0


def test_remove_home_refuses_paths_outside_externals(temp_db):
    ws = Path(config.AGENTS_DIR) / AGENT / "workspace"
    ws.mkdir(parents=True)
    stats = {"errors": 0, "caller_bytes_freed": 0}
    assert er.remove_home(AGENT, ws, stats, dry_run=False) is False
    assert ws.exists() and stats["errors"] == 1


def test_iter_local_homes_includes_caller_trees(temp_db):
    durable = _tree("d1")
    eph = _tree(str(uuid.uuid4()), ephemeral=True)
    (Path(config.AGENTS_DIR) / AGENT / "workspace").mkdir(parents=True)
    homes = {(a, u, str(h)) for a, u, h in retention.iter_local_homes()}
    assert (AGENT, "", str(durable)) in homes and (AGENT, "", str(eph)) in homes


# ---------------------------------------------------------------------------
# Conversations + call log
# ---------------------------------------------------------------------------

def test_phone_chats_and_call_log_age_out(temp_db):
    old_chat, old_sid, old_file = _phone_chat(days_old=100)
    fresh_chat, _, fresh_file = _phone_chat(days_old=5)
    dash_chat, _, _ = _phone_chat(days_old=100, source_type="dashboard")
    live_chat, live_sid, live_file = _phone_chat(days_old=100)
    _call_row(100)
    fresh_row = _call_row(5)

    stats = _sweep(LiveSnapshot(session_ids={live_sid}))
    assert not _chat_exists(old_chat) and not old_file.exists()
    assert _chat_exists(fresh_chat) and fresh_file.exists()
    # Not a phone chat: the row stays (its session file is Pass A's business —
    # the session-retention window, not the caller-data one).
    assert _chat_exists(dash_chat)
    assert _chat_exists(live_chat) and live_file.exists()      # in a live call
    assert stats["phone_chats_deleted"] == 1
    rows, total = phone_call_log_store.list_calls()
    assert total == 1 and rows[0]["id"] == fresh_row
    assert stats["call_log_rows_deleted"] == 1


def test_phone_chat_session_file_under_a_caller_tree(temp_db):
    home = _tree("caller-x")                    # fresh tree, stays
    chat_id, sid, _ = _phone_chat(days_old=100, with_file=False)
    d = home / ".claude" / "projects" / "-caller"
    f = d / f"{sid}.jsonl"
    f.write_text("z" * 10)
    _sweep()
    assert not _chat_exists(chat_id) and not f.exists() and home.exists()


def test_an_aged_phone_chat_takes_its_transcripts_folder(temp_db):
    chat_id, _sid, f = _phone_chat(days_old=100)
    folder = f.with_suffix("")
    (folder / "subagents").mkdir(parents=True)
    (folder / "subagents" / "a.jsonl").write_text("w" * 25)
    stats = _sweep()
    assert not _chat_exists(chat_id) and not f.exists() and not folder.exists()
    assert stats["caller_bytes_freed"] >= 75 and stats["errors"] == 0


def test_dry_run_reports_without_mutating(temp_db):
    home = _tree("dry", age_days=100)
    chat_id, _, f = _phone_chat(days_old=100)
    _call_row(100)
    stats = _sweep(dry_run=True)
    assert home.exists() and _chat_exists(chat_id) and f.exists()
    assert phone_call_log_store.count_older_than(None) == 1
    assert stats["callers_forgotten"] == 1 and stats["phone_chats_deleted"] == 1
    assert stats["call_log_rows_deleted"] == 1 and stats["caller_bytes_freed"] > 0


def test_window_setting_drives_the_cutoff(temp_db):
    task_store.set_platform_setting("external_retention_days", "7")
    home = _tree("w", age_days=10)
    chat_id, _, _ = _phone_chat(days_old=10)
    _sweep()
    assert not home.exists() and not _chat_exists(chat_id)


# ---------------------------------------------------------------------------
# Forget all
# ---------------------------------------------------------------------------

def test_forget_all_removes_everything_but_busy(temp_db):
    gone = _tree("g1")
    eph = _tree(str(uuid.uuid4()), ephemeral=True)
    busy = _tree("busy")
    chat_id, _, f = _phone_chat(days_old=0)
    live_chat, live_sid, _ = _phone_chat(days_old=0)
    dash_chat, _, _ = _phone_chat(days_old=0, source_type="dashboard")
    _call_row(0); _call_row(50)

    result = er.forget_all({str(busy.resolve())}, {live_sid}, set())
    assert not gone.exists() and not eph.exists() and busy.exists()
    assert not _chat_exists(chat_id) and not f.exists()
    assert _chat_exists(live_chat) and _chat_exists(dash_chat)
    assert phone_call_log_store.count_older_than(None) == 0
    assert result["callers_forgotten"] == 2 and result["callers_busy_skipped"] == 1
    assert result["phone_chats_deleted"] == 1 and result["phone_chats_busy_skipped"] == 1
    assert result["call_log_rows_deleted"] == 2


@pytest.mark.parametrize("how", ["the window", "forget all"])
def test_a_forgotten_callers_saved_tool_answers_go_with_their_tree(temp_db, how):
    """The answers their sessions' tools gave are kept under the caller's
    channel and number: when their tree goes (the window, or "Forget all"),
    so do they; another caller's stay."""
    from services.mcp import tool_listing
    from storage.agents import agent_store
    from storage.identity import external_identity_store
    agent_store.create_agent(AGENT, "Support")
    listing = {"initialize": {}, "lists": {"tools/list": [{"name": "echo"}]}}
    for number, days in (("302101234567", 100), ("302109999999", 10)):
        _tree(number, age_days=days)
        external_identity_store.touch(AGENT, "phone", f"+{number}", number)
        assert tool_listing.keep((AGENT, tool_listing.outside_person("phone", f"+{number}")), "x-mcp", "fp", listing)
    if how == "forget all":
        er.forget_all({str(_tree("busy").resolve())}, set(), set())
        assert tool_listing.saved((AGENT, tool_listing.outside_person("phone", "+302109999999")), "x-mcp", "fp") is None
    else:
        _sweep()
        assert tool_listing.saved((AGENT, tool_listing.outside_person("phone", "+302109999999")), "x-mcp", "fp")
    assert tool_listing.saved((AGENT, tool_listing.outside_person("phone", "+302101234567")), "x-mcp", "fp") is None
    assert external_identity_store.get(AGENT, "phone", "302101234567") is None


# ---------------------------------------------------------------------------
# Settings + usage + API
# ---------------------------------------------------------------------------

def test_settings_helpers(temp_db):
    assert er.settings_enabled() is True and er.settings_days() == 90
    assert er.prune_cutoff() is not None
    task_store.set_platform_setting("external_retention_days", "0")
    assert er.settings_days() == er.MIN_DAYS
    task_store.set_platform_setting("external_retention_days", "99999")
    assert er.settings_days() == er.MAX_DAYS
    task_store.set_platform_setting("external_retention_days", "junk")
    assert er.settings_days() == 90
    task_store.set_platform_setting("external_retention_enabled", "0")
    assert er.settings_enabled() is False and er.prune_cutoff() is None


def test_usage_shape(temp_db):
    _tree("u1"); _tree("u2", agent="other")
    _tree(str(uuid.uuid4()), ephemeral=True)
    _phone_chat(days_old=1)
    _call_row(1)
    usage = er.compute_usage()
    assert usage["callers"] == 2 and usage["bytes"] > 0
    assert set(usage["agents"]) == {AGENT, "other"}
    assert usage["agents"][AGENT]["callers"] == 1
    assert usage["phone_chats"] == 1 and usage["call_log_rows"] == 1
    assert usage["enabled"] is True and usage["days"] == 90


@pytest.fixture
def client(temp_db):
    from api.phone import phone as phone_router
    app = FastAPI()
    app.include_router(phone_router.router)

    async def _admin():
        return UserContext(sub="admin-sub", email="admin@test.com", name="Admin",
                           role="admin", agents=[], agent_roles={})
    app.dependency_overrides[get_current_user] = _admin
    return TestClient(app)


def test_external_data_endpoints(client):
    _tree("api1")
    body = client.get("/v1/admin/phone/external-data").json()
    assert body["callers"] == 1 and body["enabled"] is True and body["days"] == 90

    assert client.put("/v1/admin/phone/external-data", json={"days": 0}).status_code == 400
    assert client.put("/v1/admin/phone/external-data", json={"days": 5000}).status_code == 400
    body = client.put("/v1/admin/phone/external-data",
                      json={"enabled": False, "days": 30}).json()
    assert body["enabled"] is False and body["days"] == 30
    assert task_store.get_platform_setting("external_retention_enabled") == "0"

    _phone_chat(days_old=0)
    _call_row(0)
    result = client.post("/v1/admin/phone/external-data/forget").json()
    assert result["callers_forgotten"] == 1 and result["phone_chats_deleted"] == 1
    assert result["call_log_rows_deleted"] == 1
    assert client.get("/v1/admin/phone/external-data").json()["callers"] == 0


# ---------------------------------------------------------------------------
# Check verdicts (CHECKS.md): the same knob and window as Pass A
# ---------------------------------------------------------------------------

def _verdict(days_old: float, name: str = "coding") -> str:
    from storage.checks import db_checks
    row = db_checks.insert_verdict(
        agent=AGENT, owner="", check_name=name, section="judge", status="pass", passed=True,
        score=1.0, findings=[], summary="fine", reason="", session_id="s", chat_id="c", run_id="",
        judge_run_id="", user_sub="", round_no=1, ran_on="local", engine="", model="",
        cost_usd=0.0, duration_ms=1, script_sha256="")
    iso = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE check_verdicts SET created_at=%s WHERE id=%s", (iso, row["id"]))
        conn.commit()
    return row["id"]


def test_aged_check_verdicts_go_with_the_session_files_and_the_list_pages_by_time(temp_db):
    from storage.checks import db_checks
    old = _verdict(40)
    fresh = _verdict(3)
    mid = _verdict(20)
    # The page cursor: the rows older than a time, newest first.
    newest_first = [r["id"] for r in db_checks.list_verdicts(AGENT)]
    assert newest_first == [fresh, mid, old]
    cursor = db_checks.list_verdicts(AGENT, limit=1)[0]["created_at"]
    assert [r["id"] for r in db_checks.list_verdicts(AGENT, before=cursor)] == [mid, old]
    # A dry run counts, the sweep deletes, and the 30-day window is Pass A's.
    assert _sweep(dry_run=True)["check_verdicts_deleted"] == 1
    assert {r["id"] for r in db_checks.list_verdicts(AGENT)} == {old, mid, fresh}
    stats = _sweep()
    assert stats["check_verdicts_deleted"] == 1
    assert [r["id"] for r in db_checks.list_verdicts(AGENT)] == [fresh, mid]
    # With the knob off nothing of the verdicts is touched.
    _verdict(50)
    off = retention._run_sweep_sync(30, False, LiveSnapshot(), False)
    assert off["check_verdicts_deleted"] == 0 and len(db_checks.list_verdicts(AGENT)) == 3


def test_an_aged_phone_chat_takes_its_reports(temp_db):
    from storage import db_reply_reports
    from storage.pg import get_conn
    old_chat, _, _ = _phone_chat(days_old=100)
    fresh_chat, _, _ = _phone_chat(days_old=5)
    for cid, mid in ((old_chat, 201), (fresh_chat, 202)):
        db_reply_reports.add("user-admin", cid, mid, agent="a1", reason="harmful", note="",
                             reply_text="the caller's answer")
    _sweep(dry_run=True)
    with get_conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM reply_reports").fetchone()["n"] == 2
    _sweep()
    with get_conn() as conn:
        rows = conn.execute("SELECT chat_id FROM reply_reports").fetchall()
    assert [r["chat_id"] for r in rows] == [fresh_chat]


# ---------------------------------------------------------------------------
# The callers' folders, reached beneath the agent's real folder
# ---------------------------------------------------------------------------

def _outside(tmp_path: Path) -> Path:
    """A stand-in folder beside the agents folder, holding an old tree."""
    out = tmp_path / "outside"
    (out / "p" / "workspace").mkdir(parents=True)
    (out / "p" / "workspace" / "keep.md").write_text("kept")
    _age(out, 400)
    return out


def test_a_channel_that_is_a_link_is_never_listed(temp_db, tmp_path, caplog):
    out = _outside(tmp_path)
    base = Path(config.AGENTS_DIR) / AGENT / "externals"
    base.mkdir(parents=True)
    (base / "phone").symlink_to(out, target_is_directory=True)
    honest = _tree("302101234567", age_days=100, channel="web")
    with caplog.at_level("WARNING"):
        assert [h for *_r, h in er.iter_caller_homes()] == [honest]
    assert "not listed" not in caplog.text  # a link is passed over, never tried
    stats = _sweep()
    assert (out / "p" / "workspace" / "keep.md").read_text() == "kept"
    assert not honest.exists() and stats["callers_forgotten"] == 1


def test_a_link_at_externals_skips_that_agent_and_the_rest_go_on(temp_db, tmp_path):
    out = _outside(tmp_path)
    linked = Path(config.AGENTS_DIR) / "linked-agent"
    linked.mkdir(parents=True)
    (linked / "externals").symlink_to(out, target_is_directory=True)
    honest = _tree("302101234567", age_days=100)
    assert [h for *_r, h in er.iter_caller_homes()] == [honest]
    assert [h for _a, h in er.iter_ephemeral_homes()] == []
    stats = _sweep()
    assert not honest.exists() and stats["callers_forgotten"] == 1
    assert (out / "p" / "workspace" / "keep.md").read_text() == "kept"


def test_a_home_that_is_a_link_is_never_walked_for_its_age_or_size(temp_db, tmp_path):
    out = tmp_path / "outside"
    (out / "big").mkdir(parents=True)
    (out / "big" / "fresh.bin").write_bytes(b"x" * 4096)
    base = Path(config.AGENTS_DIR) / AGENT / "externals" / "phone"
    base.mkdir(parents=True)
    home = base / "302101234567"
    home.symlink_to(out / "big", target_is_directory=True)
    old = time.time() - 400 * 86400
    os.utime(home, (old, old), follow_symlinks=False)
    assert er._tree_bytes(home) == 0
    assert er.newest_mtime(home) == pytest.approx(old, abs=1)


def test_a_tree_swapped_for_a_link_after_its_check_is_not_removed(temp_db, tmp_path, monkeypatch):
    """The removal opens the tree it was given beneath the agent's real
    folder: a channel swapped for a link between the check and the removal
    refuses it."""
    out = _outside(tmp_path)
    home = _tree("p", age_days=100)
    channel = home.parent

    def swap(_home):
        channel.rename(channel.with_name("phone-moved"))
        channel.symlink_to(out, target_is_directory=True)
        return 0
    monkeypatch.setattr(er, "_tree_bytes", swap)
    stats = {"errors": 0, "caller_bytes_freed": 0}
    assert er.remove_home(AGENT, home, stats, dry_run=False) is False
    assert (out / "p" / "workspace" / "keep.md").read_text() == "kept"
    assert stats["errors"] == 1


def test_a_tree_of_any_other_shape_is_refused(temp_db):
    deep = _tree("p")
    inner = deep / "workspace"
    stats = {"errors": 0, "caller_bytes_freed": 0}
    assert er.remove_home(AGENT, inner, stats, dry_run=False) is False
    assert inner.is_dir() and stats["errors"] == 1
    own = Path(config.AGENTS_DIR) / AGENT / "users" / "p" / "workspace"
    own.mkdir(parents=True)
    (own / "notes.md").write_text("a person's own file")
    assert er.remove_home(AGENT, own, stats, dry_run=False) is False
    assert (own / "notes.md").is_file() and stats["errors"] == 2


def _starved(monkeypatch, *names):
    """``newest_mtime`` with each named look of ``safe_fs`` failing as a
    process with no descriptor left fails."""
    import errno

    from services.infra import safe_fs
    real = er.newest_mtime

    def no_descriptor(*_args, **_kwargs):
        raise OSError(errno.EMFILE, "Too many open files")

    def starved(path):
        with monkeypatch.context() as patch:
            for name in names:
                patch.setattr(safe_fs, name, no_descriptor)
            return real(path)
    monkeypatch.setattr(er, "newest_mtime", starved)


def test_a_tree_whose_age_cannot_be_read_is_kept(temp_db, monkeypatch, caplog):
    """A look that fails for a reason other than a missing tree never reads
    as an old tree: the tree waits for the next pass, and the refusal is
    logged. A caller's data never goes to the recover bin."""
    home = _tree("302101234567")
    _starved(monkeypatch, "lstat_beneath", "walk_beneath")
    with caplog.at_level("WARNING"):
        stats = _sweep()
    assert (home / "workspace" / "notes.md").is_file()
    assert stats["callers_forgotten"] == 0
    assert "could not be read" in caplog.text


def test_a_tree_whose_files_cannot_be_read_is_kept(temp_db, monkeypatch):
    """The tree's own time is old and a file inside is new: a walk that
    fails never leaves the old time as the tree's age."""
    home = _tree("302101234567", age_days=100)
    (home / "workspace" / "fresh.md").write_text("written today")
    _starved(monkeypatch, "walk_beneath")
    stats = _sweep()
    assert (home / "workspace" / "fresh.md").is_file()
    assert stats["callers_forgotten"] == 0


def test_an_agent_folder_behind_the_operators_link_keeps_its_retention(temp_db, tmp_path):
    """An agent folder the admin moved behind a link: its callers age out
    and are forgotten as before."""
    real = tmp_path / "disk2" / "moved-agent"
    (real / "externals" / "phone" / "302101234567" / "workspace").mkdir(parents=True)
    (real / "externals" / "phone" / "302101234567" / "workspace" / "notes.md").write_text("hi")
    _age(real, 100)
    (Path(config.AGENTS_DIR) / "moved-agent").symlink_to(real, target_is_directory=True)
    homes = [h for *_r, h in er.iter_caller_homes()]
    assert homes == [Path(config.AGENTS_DIR) / "moved-agent" / "externals" / "phone" / "302101234567"]
    assert er._tree_bytes(homes[0]) == 2
    stats = _sweep()
    assert stats["callers_forgotten"] == 1 and stats["errors"] == 0
    assert not (real / "externals" / "phone" / "302101234567").exists()

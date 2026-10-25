"""Session retention + disk cleanup (services/infra/retention.py).

Covers the four sweep passes (aged chats / orphans / codex junk / tarball GC),
the shared-session and live-session protections, dry-run, the settings
helpers, and the storage-helper queries. Filesystem fixtures are built under
the test-redirected config.AGENTS_DIR (wiped per test by conftest).
"""

import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from services.infra import retention
from services.infra.retention import LiveSnapshot

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _home(agent: str, username: str = "") -> Path:
    base = Path(config.AGENTS_DIR) / agent
    return (base / "users" / username) if username else (base / "workspace")


def _mk_claude(home: Path, sid: str, project: str = "-users-alice") -> Path:
    d = home / ".claude" / "projects" / project
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    f.write_text("x" * 200)
    return f


def _mk_rollout(home: Path, tid: str) -> Path:
    d = home / ".codex" / "sessions" / "2026" / "01" / "01"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-2026-01-01T00-00-00-{tid}.jsonl"
    f.write_text("y" * 200)
    return f


def _age_file(f: Path, days: float) -> None:
    old = time.time() - days * 86400
    os.utime(f, (old, old))


def _set_username(db, sub: str, username: str) -> None:
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", (username, sub))
        conn.commit()


def _backdate_chat(chat_id: str, days: int) -> str:
    from storage.pg import get_conn
    iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE chats SET updated_at=%s WHERE id=%s", (iso, chat_id))
        conn.commit()
    return iso


def _sweep(*, days: int = 30, enabled: bool = True,
           live: LiveSnapshot | None = None, dry_run: bool = False) -> dict:
    return retention._run_sweep_sync(days, enabled, live or LiveSnapshot(), dry_run)


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Pass A — aged chats
# ---------------------------------------------------------------------------

def test_aged_chat_files_deleted_and_flagged(temp_db, monkeypatch):
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    db = temp_db
    _set_username(db, "user-admin", "alice")
    sid, tid = _uuid(), _uuid()
    db.create_chat("c1", "user-admin", "a1")
    db.update_chat("c1", session_id=sid, codex_thread_id=tid)
    backdated = _backdate_chat("c1", 60)
    cf = _mk_claude(_home("a1", "alice"), sid)
    rf = _mk_rollout(_home("a1", "alice"), tid)

    stats = _sweep(days=30)

    assert not cf.exists() and not rf.exists()
    chat = db.get_chat("c1")
    assert chat["session_id"] is None
    assert chat["codex_thread_id"] is None
    assert chat["pending_history_seed"] == "retention"
    assert chat["updated_at"] == backdated  # no bump — chat-list order kept
    assert stats["chats_flagged"] == 1
    assert stats["session_files_deleted"] == 2
    assert stats["bytes_freed"] == 400


def test_a_chat_row_naming_no_agent_directory_never_stops_the_sweep(temp_db, monkeypatch):
    """``get_agent_dir`` refuses an empty name: the sweep skips that row and
    still ages out the others."""
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    db = temp_db
    _set_username(db, "user-admin", "alice")
    bad_sid, sid = _uuid(), _uuid()
    db.create_chat("c-bad", "user-admin", "")
    db.update_chat("c-bad", session_id=bad_sid)
    _backdate_chat("c-bad", 60)
    db.create_chat("c-good", "user-admin", "a1")
    db.update_chat("c-good", session_id=sid)
    _backdate_chat("c-good", 60)
    cf = _mk_claude(_home("a1", "alice"), sid)

    _sweep(days=30)

    assert not cf.exists() and db.get_chat("c-good")["session_id"] is None


def test_flag_skips_a_chat_resumed_after_the_candidate_query(temp_db, monkeypatch):
    """A chat resumed between the candidate query and the flag keeps its
    session id AND its files: the sweep flags first, with the candidate
    cutoff, and unlinks only what it flagged."""
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    db = temp_db
    _set_username(db, "user-admin", "alice")
    sid_a, sid_b = _uuid(), _uuid()
    for cid, sid in (("ca", sid_a), ("cb", sid_b)):
        db.create_chat(cid, "user-admin", "a1")
        db.update_chat(cid, session_id=sid)
        _backdate_chat(cid, 60)
    fa = _mk_claude(_home("a1", "alice"), sid_a)
    fb = _mk_claude(_home("a1", "alice"), sid_b)
    real_flag = retention.task_store.flag_chats_for_retention

    def resumed_then_flag(chat_ids, *args, **kwargs):
        db.update_chat("ca", session_id=sid_a)  # a resume bumps updated_at
        return real_flag(chat_ids, *args, **kwargs)

    monkeypatch.setattr(retention.task_store, "flag_chats_for_retention", resumed_then_flag)

    stats = _sweep(days=30)

    assert db.get_chat("ca")["session_id"] == sid_a and fa.exists()
    assert db.get_chat("ca")["pending_history_seed"] == ""
    assert db.get_chat("cb")["session_id"] is None and not fb.exists()
    assert stats["chats_flagged"] == 1
    assert stats["session_files_deleted"] == 1


def test_aged_agent_scope_chat_uses_workspace_home(temp_db, monkeypatch):
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    db = temp_db
    sid = _uuid()
    db.create_chat("t1", "task::a1", "a1")  # sentinel sub → no users row
    db.update_chat("t1", session_id=sid)
    _backdate_chat("t1", 60)
    cf = _mk_claude(_home("a1"), sid, project="-workspace")

    _sweep(days=30)

    assert not cf.exists()
    assert db.get_chat("t1")["pending_history_seed"] == "retention"


def test_exclusions_fresh_remote_directllm(temp_db, monkeypatch):
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    db = temp_db
    _set_username(db, "user-admin", "alice")
    keep: list[Path] = []

    db.create_chat("fresh", "user-admin", "a1")
    sid_fresh = _uuid()
    db.update_chat("fresh", session_id=sid_fresh)
    keep.append(_mk_claude(_home("a1", "alice"), sid_fresh))

    db.create_chat("remote", "user-admin", "a1")
    sid_remote = _uuid()
    db.update_chat("remote", session_id=sid_remote, execution_target="machine-1")
    _backdate_chat("remote", 90)
    keep.append(_mk_claude(_home("a1", "alice"), sid_remote))

    db.create_chat("direct", "user-admin", "a1", execution_path="direct-llm")
    sid_direct = _uuid()
    db.update_chat("direct", session_id=sid_direct)
    _backdate_chat("direct", 90)
    keep.append(_mk_claude(_home("a1", "alice"), sid_direct))

    stats = _sweep(days=30)

    assert all(f.exists() for f in keep)
    assert stats["chats_flagged"] == 0
    for cid in ("fresh", "remote", "direct"):
        assert db.get_chat(cid)["pending_history_seed"] == ""


def test_shared_session_protected_by_fresh_sibling(temp_db, monkeypatch):
    """continue_session delegation reuses one session id across chat rows —
    an aged sibling must never age out the shared file."""
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    db = temp_db
    _set_username(db, "user-admin", "alice")
    sid = _uuid()
    db.create_chat("old-row", "user-admin", "a1")
    db.update_chat("old-row", session_id=sid)
    _backdate_chat("old-row", 90)
    db.create_chat("fresh-row", "user-admin", "a1")
    db.update_chat("fresh-row", session_id=sid)
    f = _mk_claude(_home("a1", "alice"), sid)

    stats = _sweep(days=30)

    assert f.exists()
    assert stats["chats_flagged"] == 0
    assert db.get_chat("old-row")["session_id"] == sid  # skipped, not flagged


def test_live_session_and_pump_guards(temp_db, monkeypatch):
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    db = temp_db
    _set_username(db, "user-admin", "alice")

    sid_live = _uuid()
    db.create_chat("live-sess", "user-admin", "a1")
    db.update_chat("live-sess", session_id=sid_live)
    _backdate_chat("live-sess", 90)
    f1 = _mk_claude(_home("a1", "alice"), sid_live)

    sid_pump = _uuid()
    db.create_chat("live-pump", "user-admin", "a1")
    db.update_chat("live-pump", session_id=sid_pump)
    _backdate_chat("live-pump", 90)
    f2 = _mk_claude(_home("a1", "alice"), sid_pump)

    live = LiveSnapshot(session_ids={sid_live}, pump_chat_ids={"live-pump"})
    stats = _sweep(days=30, live=live)

    assert f1.exists() and f2.exists()
    assert stats["chats_flagged"] == 0


# ---------------------------------------------------------------------------
# Pass B — orphans
# ---------------------------------------------------------------------------

def test_orphans_deleted_referenced_and_young_kept(temp_db, monkeypatch):
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    db = temp_db
    _set_username(db, "user-admin", "alice")
    home = _home("a1", "alice")

    old_orphan = _mk_claude(home, _uuid())
    _age_file(old_orphan, 10)
    old_rollout = _mk_rollout(home, _uuid())
    _age_file(old_rollout, 10)
    young_orphan = _mk_claude(home, _uuid())  # mtime now → grace keeps it

    sid_ref = _uuid()
    db.create_chat("ref", "user-admin", "a1")
    db.update_chat("ref", session_id=sid_ref)
    referenced = _mk_claude(home, sid_ref)
    _age_file(referenced, 10)

    # dynamic_tasks session refs protect files even with no chat row
    tid_task = _uuid()
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO dynamic_tasks (id, agent, name, prompt, task_type, "
            "created_at, continue_session) VALUES "
            "('dt1','a1','t','p','once','2026-01-01T00:00:00+00:00',%s)",
            (tid_task,),
        )
        conn.commit()
    task_file = _mk_claude(home, tid_task)
    _age_file(task_file, 10)

    stats = _sweep(days=30)

    assert not old_orphan.exists() and not old_rollout.exists()
    assert young_orphan.exists()
    assert referenced.exists()
    assert task_file.exists()
    assert stats["orphans_deleted"] == 2


# ---------------------------------------------------------------------------
# Pass C — codex junk
# ---------------------------------------------------------------------------

def _mk_codex_junk(home: Path) -> dict[str, Path]:
    codex = home / ".codex"
    (codex / ".tmp" / "plugins").mkdir(parents=True, exist_ok=True)
    (codex / "sessions").mkdir(parents=True, exist_ok=True)
    files = {
        "logs": codex / "logs_2.sqlite",
        "logs_wal": codex / "logs_2.sqlite-wal",
        "state": codex / "state_5.sqlite",
        "tmp_file": codex / ".tmp" / "plugins" / "bundle.bin",
        "config": codex / "config.toml",
    }
    for f in files.values():
        f.write_text("z" * 100)
        _age_file(f, 1)  # past the 1h in-flight guard
    return files


def test_codex_junk_cleaned_state_kept(temp_db, monkeypatch):
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    _set_username(temp_db, "user-admin", "alice")
    home = _home("a1", "alice")
    files = _mk_codex_junk(home)

    stats = _sweep(days=30)

    assert not files["logs"].exists() and not files["logs_wal"].exists()
    assert not files["tmp_file"].exists()
    assert files["state"].exists()
    assert files["config"].exists()
    assert (home / ".codex" / ".tmp").is_dir()          # kept (emptied)
    assert not (home / ".codex" / ".tmp" / "plugins").exists()  # pruned
    assert stats["codex_junk_files"] == 3


def test_codex_junk_skips_busy_home_and_young_files(temp_db, monkeypatch):
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    _set_username(temp_db, "user-admin", "alice")
    busy_files = _mk_codex_junk(_home("a1", "alice"))
    young = _mk_codex_junk(_home("a2", "alice"))
    _age_file(young["logs"], 0)  # reset to now → in-flight guard keeps it

    live = LiveSnapshot(busy_homes={("a1", "alice")})
    _sweep(days=30, live=live)

    assert busy_files["logs"].exists()      # whole home skipped
    assert young["logs"].exists()           # too young
    assert not young["logs_wal"].exists()   # aged sibling still cleaned


# ---------------------------------------------------------------------------
# Recover bin: rows whose bytes are gone
# ---------------------------------------------------------------------------

def test_recover_bin_rows_without_bytes_are_pruned_by_the_sweep(temp_db, monkeypatch, tmp_path):
    from storage.files import recover_bin_store as rb
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    monkeypatch.setattr(config, "RECOVER_BIN_DIR", tmp_path / "recover-bin")
    kept = rb.capture("a1", "workspace/kept.txt", b"kept", "deleted")
    gone = rb.capture("a1", "workspace/gone.txt", b"gone", "deleted")
    (config.RECOVER_BIN_DIR / "a1" / gone["entry_id"]).unlink()

    assert _sweep(dry_run=True)["recover_bin_rows_pruned"] == 1
    assert rb.get(gone["entry_id"]) is not None
    stats = _sweep()
    assert stats["recover_bin_rows_pruned"] == 1 and stats["errors"] == 0
    assert rb.get(gone["entry_id"]) is None and rb.get(kept["entry_id"]) is not None


# ---------------------------------------------------------------------------
# Dry run / toggle / Pass D / settings
# ---------------------------------------------------------------------------

def test_dry_run_reports_without_mutating(temp_db, monkeypatch):
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    db = temp_db
    _set_username(db, "user-admin", "alice")
    sid = _uuid()
    db.create_chat("c1", "user-admin", "a1")
    db.update_chat("c1", session_id=sid)
    _backdate_chat("c1", 90)
    f = _mk_claude(_home("a1", "alice"), sid)
    orphan = _mk_claude(_home("a1", "alice"), _uuid())
    _age_file(orphan, 10)

    stats = _sweep(days=30, dry_run=True)

    assert f.exists() and orphan.exists()
    assert db.get_chat("c1")["session_id"] == sid
    assert db.get_chat("c1")["pending_history_seed"] == ""
    assert stats["dry_run"] is True
    assert stats["chats_flagged"] == 1
    assert stats["session_files_deleted"] == 1
    assert stats["orphans_deleted"] == 1


def test_disabled_toggle_skips_aged_pass_only(temp_db, monkeypatch):
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    db = temp_db
    _set_username(db, "user-admin", "alice")
    sid = _uuid()
    db.create_chat("c1", "user-admin", "a1")
    db.update_chat("c1", session_id=sid)
    _backdate_chat("c1", 90)
    aged = _mk_claude(_home("a1", "alice"), sid)
    orphan = _mk_claude(_home("a1", "alice"), _uuid())
    _age_file(orphan, 10)

    stats = _sweep(days=30, enabled=False)

    assert aged.exists()                       # Pass A skipped
    assert db.get_chat("c1")["session_id"] == sid
    assert not orphan.exists()                 # Pass B still ran
    assert stats["retention_pass_skipped"] is True
    assert stats["chats_flagged"] == 0


def test_tarball_gc_wired(temp_db, monkeypatch):
    from services.mcp import mcp_tarball
    monkeypatch.setattr(mcp_tarball, "gc", lambda: 4321)
    stats = _sweep(days=30)
    assert stats["tarball_bytes"] == 4321


def test_live_snapshot_ignores_stale_security_contexts(temp_db, monkeypatch):
    """SecurityContexts persist across proxy restarts (up to 24h) for
    sessions that never closed cleanly — they must NOT mark homes busy
    unless the session is in a live registry. (The first live run-now
    deleted 0 of 292 MB junk because every recently-used home looked busy.)
    """
    import core.events.stream_pump as stream_pump
    import core.layers.cli.session as cli_session
    import core.layers.codex.session as codex_session
    import core.session.session_state as session_state

    class _Ctx:
        def __init__(self, agent, username):
            self.agent = agent
            self.username = username

    class _Codex:
        thread_id = "019eb403-603e-7a02-bbe2-39d5a84b9f2a"
        config_dir = "/tmp/retention-test/.codex"

    monkeypatch.setattr(cli_session, "_persistent_sessions", {"sid-live": object()})
    monkeypatch.setattr(codex_session, "_codex_sessions", {"sid-codex": _Codex()})
    monkeypatch.setattr(session_state, "_session_security", {
        "sid-live": _Ctx("a1", "alice"),     # live cli session → busy
        "sid-stale": _Ctx("a2", "alice"),    # restart residue → ignored
    })
    monkeypatch.setattr(stream_pump, "_active_pumps", {})

    snap = retention._build_live_snapshot()
    assert ("a1", "alice") in snap.busy_homes
    assert all(agent != "a2" for agent, _u in snap.busy_homes)
    assert "sid-stale" not in snap.session_ids
    assert "sid-live" in snap.session_ids and "sid-codex" in snap.session_ids
    assert _Codex.thread_id in snap.codex_thread_ids


def test_settings_helpers(temp_db):
    db = temp_db
    assert retention.settings_enabled() is True          # unset → ON
    assert retention.settings_days() == retention.DEFAULT_DAYS
    db.set_platform_setting("session_retention_enabled", "0")
    assert retention.settings_enabled() is False
    db.set_platform_setting("session_retention_days", "3")
    assert retention.settings_days() == retention.MIN_DAYS  # clamped
    db.set_platform_setting("session_retention_days", "garbage")
    assert retention.settings_days() == retention.DEFAULT_DAYS


def test_storage_usage_shape(temp_db):
    _set_username(temp_db, "user-admin", "alice")
    _mk_claude(_home("a1", "alice"), _uuid())
    _mk_codex_junk(_home("a1", "alice"))
    usage = retention.compute_storage_usage()
    assert usage["session_files_bytes"] >= 200
    assert usage["codex_junk_bytes"] >= 300   # logs + wal + tmp file
    assert usage["retention"]["enabled"] is True
    assert usage["retention"]["days"] == retention.DEFAULT_DAYS
    assert usage["retention"]["last_sweep"] is None


def test_storage_usage_counts_the_upload_staging(temp_db, monkeypatch):
    """Chunked uploads in flight hold disk outside every agent tree; the
    admin breakdown shows them."""
    from services.media import upload_staging
    monkeypatch.setattr(upload_staging, "staged_bytes_total", lambda: 4321)
    assert retention.compute_storage_usage()["upload_staging_bytes"] == 4321


# ---------------------------------------------------------------------------
# The sweep entry point off the loop, and the batched verdict deletion
# ---------------------------------------------------------------------------

def test_run_sweep_reads_and_writes_off_the_loop(temp_db, loop_db_guard, monkeypatch):
    """The settings read and the last-run stats write of ``run_sweep`` are
    executor work: the loop-thread guard stays silent and the stats land."""
    import asyncio
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    monkeypatch.setattr(retention, "_build_live_snapshot", LiveSnapshot)
    db = temp_db

    async def scenario():
        with loop_db_guard.active():
            return await retention.run_sweep()

    stats = asyncio.run(scenario())
    assert stats["dry_run"] is False and stats["errors"] == 0
    assert db.get_platform_setting("session_retention_last_sweep")


def _verdict(days_old: float) -> str:
    from storage.checks import db_checks
    from storage.pg import get_conn
    row = db_checks.insert_verdict(
        agent="a1", owner="", check_name="coding", section="judge", status="pass", passed=True,
        score=1.0, findings=[], summary="fine", reason="", session_id="s", chat_id="c", run_id="",
        judge_run_id="", user_sub="", round_no=1, ran_on="local", engine="", model="",
        cost_usd=0.0, duration_ms=1, script_sha256="")
    iso = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE check_verdicts SET created_at=%s WHERE id=%s", (iso, row["id"]))
        conn.commit()
    return row["id"]


def test_verdict_deletion_runs_in_batches(temp_db, monkeypatch):
    """Aged verdicts go in committed batches, each statement bounded, so the
    first sweep of a large install never runs one delete for minutes."""
    import contextlib

    from storage import pg
    from storage.checks import db_checks
    aged = [_verdict(40) for _ in range(5)]
    fresh = _verdict(3)
    monkeypatch.setattr(db_checks, "_VERDICT_DELETE_BATCH", 2, raising=False)
    deletes: list[str] = []

    class _Spy:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            if sql.lstrip().upper().startswith("DELETE"):
                deletes.append(sql)
            return self._conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    real_get_conn = pg.get_conn

    @contextlib.contextmanager
    def spied(**kw):
        with real_get_conn(**kw) as conn:
            yield _Spy(conn)

    monkeypatch.setattr(db_checks, "get_conn", spied)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    assert db_checks.delete_verdicts_before(cutoff, dry_run=True) == 5
    assert db_checks.delete_verdicts_before(cutoff) == 5
    assert len(deletes) == 3  # 2 + 2 + 1, the short batch ends the loop
    assert [r["id"] for r in db_checks.list_verdicts("a1")] == [fresh]
    assert not any(r["id"] in aged for r in db_checks.list_verdicts("a1"))


# ---------------------------------------------------------------------------
# The .offboarded/ archive: its own setting, the sweep, list and purge
# ---------------------------------------------------------------------------

def _retired(username: str, *, archived_days: float | None, retired_days: float = 400) -> None:
    from storage.pg import get_conn
    now = datetime.now(timezone.utc)
    archived = "" if archived_days is None else (now - timedelta(days=archived_days)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO retired_usernames (username, sub, retired_at, archived_at) "
            "VALUES (%s,%s,%s,%s)",
            (username, f"sub-{username}", (now - timedelta(days=retired_days)).isoformat(), archived))
        conn.commit()


def _archive(username: str, *agents: str) -> Path:
    base = Path(config.AGENTS_DIR) / ".offboarded" / username
    for agent in agents or ("a1",):
        d = base / agent / "workspace"
        d.mkdir(parents=True, exist_ok=True)
        (d / "notes.txt").write_text("z" * 100)
    return base


def test_the_archive_setting_helpers(temp_db):
    db = temp_db
    assert retention.offboarded_enabled() is True        # unset → ON
    assert retention.offboarded_days() == 180
    db.set_platform_setting("offboarded_retention_days", "0")
    assert retention.offboarded_days() == 0               # keeps for ever
    # An unreadable value (a forced setting, an older write) keeps every
    # archive: "-1" meant as "for ever" must never become 180 days.
    for garbage in ("-4", "99999", "soon"):
        db.set_platform_setting("offboarded_retention_days", garbage)
        assert retention.offboarded_days() == 0
    db.set_platform_setting("offboarded_retention_enabled", "0")
    assert retention.offboarded_enabled() is False


def test_the_sweep_deletes_old_archives_only(temp_db):
    db = temp_db
    old, young, undated, odd = _archive("old-pat", "a1", "a2"), _archive("young"), \
        _archive("undated"), _archive("Odd.Name")
    _retired("old-pat", archived_days=200)
    _retired("young", archived_days=10)
    _retired("undated", archived_days=None)
    target = Path(config.AGENTS_DIR) / "a1" / "workspace"
    target.mkdir(parents=True, exist_ok=True)
    (target / "keep.txt").write_text("k")
    link = Path(config.AGENTS_DIR) / ".offboarded" / "linked"
    link.symlink_to(Path(config.AGENTS_DIR) / "a1")
    _retired("linked", archived_days=400)
    stats = {"offboarded_deleted": 0, "offboarded_bytes": 0, "errors": 0}
    retention._pass_offboarded_archives(stats, dry_run=True)
    assert stats["offboarded_deleted"] == 1 and stats["offboarded_bytes"] >= 200 and old.exists()
    stats = {"offboarded_deleted": 0, "offboarded_bytes": 0, "errors": 0}
    retention._pass_offboarded_archives(stats, dry_run=False)
    assert stats["offboarded_deleted"] == 1 and not old.exists()
    assert young.exists() and undated.exists() and odd.exists() and link.is_symlink()
    assert (target / "keep.txt").read_text() == "k"  # a link is never followed
    # 0 keeps for ever; the toggle off keeps too.
    _retired("older", archived_days=900)
    older = _archive("older")
    db.set_platform_setting("offboarded_retention_days", "0")
    retention._pass_offboarded_archives({"offboarded_deleted": 0, "offboarded_bytes": 0,
                                         "errors": 0}, dry_run=False)
    db.set_platform_setting("offboarded_retention_days", "30")
    db.set_platform_setting("offboarded_retention_enabled", "0")
    retention._pass_offboarded_archives({"offboarded_deleted": 0, "offboarded_bytes": 0,
                                         "errors": 0}, dry_run=False)
    assert older.exists()


def test_a_linked_archive_root_is_an_error_not_a_walk(temp_db, tmp_path):
    outside = tmp_path / "elsewhere" / "victim"
    outside.mkdir(parents=True)
    root = Path(config.AGENTS_DIR) / ".offboarded"
    root.symlink_to(tmp_path / "elsewhere")
    try:
        _retired("victim", archived_days=900)
        stats = {"offboarded_deleted": 0, "offboarded_bytes": 0, "errors": 0}
        retention._pass_offboarded_archives(stats, dry_run=False)
        assert stats["errors"] == 1 and stats["offboarded_deleted"] == 0 and outside.exists()
    finally:
        root.unlink()


def _admin_client(api_key=False):
    from fastapi.testclient import TestClient

    from app import app
    from auth.providers import UserContext, get_current_user

    async def _admin():
        return UserContext(sub="user-admin", email="admin@t.com", name="Ada", role="admin",
                           is_api_key=api_key)
    app.dependency_overrides[get_current_user] = _admin
    return app, TestClient(app)


def test_the_admin_lists_and_purges_an_archive(temp_db):
    from auth.providers import get_current_user
    _archive("old-pat", "a1", "a2")
    _retired("old-pat", archived_days=200)
    _archive("fresh")
    _retired("fresh", archived_days=None, retired_days=0)   # still being written
    _archive("restored")                                      # no retired row at all
    app, client = _admin_client()
    try:
        listed = client.get("/v1/admin/offboarded").json()
        by_name = {a["username"]: a for a in listed["archives"]}
        assert set(by_name) == {"old-pat", "fresh", "restored"}
        assert by_name["old-pat"]["agents"] == ["a1", "a2"] and by_name["old-pat"]["bytes"] >= 200
        assert by_name["old-pat"]["purge_after"] and by_name["restored"]["purge_after"] == ""
        assert listed["retention"] == {"enabled": True, "days": 180}
        assert client.delete("/v1/admin/offboarded/fresh").status_code == 409
        assert client.delete("/v1/admin/offboarded/Nope.Name").status_code == 400
        assert client.delete("/v1/admin/offboarded/nobody").status_code == 404
        resp = client.delete("/v1/admin/offboarded/old-pat")
        assert resp.status_code == 200 and resp.json()["bytes"] >= 200
        assert not (Path(config.AGENTS_DIR) / ".offboarded" / "old-pat").exists()
        assert client.delete("/v1/admin/offboarded/restored").status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    # A session token or API key never reaches it.
    app, client = _admin_client(api_key=True)
    try:
        assert client.get("/v1/admin/offboarded").status_code == 403
        assert client.delete("/v1/admin/offboarded/fresh").status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_the_platform_settings_carry_the_archive_setting(temp_db):
    from auth.providers import get_current_user
    app, client = _admin_client()
    try:
        got = client.get("/v1/admin/platform-settings").json()
        assert got["offboarded_retention_enabled"] is True
        assert got["offboarded_retention_days"] == "180"
        assert client.put("/v1/admin/platform-settings",
                          json={"offboarded_retention_days": "0"}).status_code == 200
        assert client.put("/v1/admin/platform-settings",
                          json={"offboarded_retention_enabled": False}).status_code == 200
        for bad in ("-1", "4000", "soon"):
            assert client.put("/v1/admin/platform-settings",
                              json={"offboarded_retention_days": bad}).status_code == 400
        got = client.get("/v1/admin/platform-settings").json()
        assert got["offboarded_retention_days"] == "0" and got["offboarded_retention_enabled"] is False
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_an_unreadable_or_read_only_tree_is_listed_and_purged(temp_db):
    """A tree the agent left read-only (a module cache) or unreadable stays
    listable and purgeable: the list names it, the purge makes the owner's
    own directories writable and deletes it."""
    import os as _os

    from auth.providers import get_current_user
    base = _archive("locked", "a1")
    _retired("locked", archived_days=5)
    ro = base / "a1" / "workspace" / "go" / "pkg" / "mod"
    ro.mkdir(parents=True)
    (ro / "f.go").write_text("x")
    _os.chmod(ro, 0o555)
    closed = base / "a1" / "closed"
    closed.mkdir()
    (closed / "g").write_text("y")
    _os.chmod(closed, 0o000)
    _archive("fine")
    _retired("fine", archived_days=5)
    app, client = _admin_client()
    try:
        listed = {a["username"]: a for a in client.get("/v1/admin/offboarded").json()["archives"]}
        assert set(listed) == {"locked", "fine"}
        assert listed["fine"]["bytes"] >= 100 and listed["locked"]["bytes"] >= 100
        resp = client.delete("/v1/admin/offboarded/locked")
        assert resp.status_code == 200, resp.text
        assert not base.exists()
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        if closed.exists():
            _os.chmod(closed, 0o700)
        if ro.exists():
            _os.chmod(ro, 0o755)


def test_a_refused_archive_setting_writes_nothing_else(temp_db):
    from auth.providers import get_current_user
    db = temp_db
    db.set_platform_setting("company_name", "Before")
    app, client = _admin_client()
    try:
        resp = client.put("/v1/admin/platform-settings",
                          json={"company_name": "After", "offboarded_retention_days": "-1"})
        assert resp.status_code == 400
        assert db.get_platform_setting("company_name") == "Before"
        # The page shows the value the sweep uses.
        db.set_platform_setting("offboarded_retention_days", "soon")
        assert client.get("/v1/admin/platform-settings").json()["offboarded_retention_days"] == "0"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Pass A, the remote half — chats pinned to a machine and the machines' files
# ---------------------------------------------------------------------------


class _FakeRemoteLayer:
    def __init__(self, sessions):
        self._sessions = sessions


class _FakeCM:
    """Connected machines by version; each answers its frame from
    ``answers`` (a dict is the ack's body, an exception the failure)."""

    def __init__(self, machines: dict, answers: dict):
        self._machines = machines
        self._answers = answers
        self.sent: list = []

    def get_connected_machines(self):
        return list(self._machines)

    def satellite_supports_session_retention(self, mid):
        return tuple(int(p) for p in self._machines[mid].split(".")) >= (0, 5, 150)

    def satellite_name(self, mid):
        return f"name-{mid}"

    async def send_command(self, mid, msg, *, timeout=30.0):
        self.sent.append((mid, dict(msg)))
        ans = self._answers.get(mid, {})
        if isinstance(ans, Exception):
            raise ans
        return {"status": "ok", **ans}


def _remote_pass(monkeypatch, cm, remote_sessions=(), *, days=30, enabled=True,
                 live=None, dry_run=False) -> dict:
    import asyncio
    from types import SimpleNamespace
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    sessions = {s[0]: SimpleNamespace(session_id=s[0], machine_id=s[1], resume_handle=s[2])
                for s in remote_sessions}
    monkeypatch.setattr(retention, "_remote_layer_for_sweep", lambda: _FakeRemoteLayer(sessions))
    snap = live or LiveSnapshot()
    stats = retention._run_sweep_sync(days, enabled, snap, dry_run)
    asyncio.run(retention._pass_remote_sessions(days, enabled, snap, stats, dry_run))
    return stats


def test_a_remote_chat_ages_out_and_its_machine_is_asked_with_its_keep_list(temp_db, monkeypatch):
    """An aged chat pinned to a machine is flagged like a local one; the
    machine then gets every id the rows still reference for it (its own
    pinned rows and the rows that name no place, the aged chat's ids gone),
    never another machine's nor a sandbox chat's; an older machine is not
    asked."""
    db = temp_db
    _set_username(db, "user-admin", "alice")
    aged_sid, aged_tid, fresh_sid, unpinned_sid, unplaced_sid, other_sid = (_uuid() for _ in range(6))
    db.create_chat("aged", "user-admin", "a1")
    db.update_chat("aged", session_id=aged_sid, codex_thread_id=aged_tid, execution_target="m-a")
    backdated = _backdate_chat("aged", 60)
    db.create_chat("fresh", "user-admin", "a1")
    db.update_chat("fresh", session_id=fresh_sid, execution_target="m-a")
    db.create_chat("unpinned", "user-admin", "a1")
    db.update_chat("unpinned", session_id=unpinned_sid)   # fresh, minted here: no pass ages it
    db.create_chat("unplaced", "user-admin", "a1")
    db.update_chat("unplaced", session_id=unplaced_sid, execution_target="")
    db.create_chat("other", "user-admin", "a1")
    db.update_chat("other", session_id=other_sid, execution_target="m-b")
    cm = _FakeCM({"m-a": "0.5.150", "m-b": "0.5.149"}, {
        "m-a": {"files": 3, "bytes": 300, "deleted": 1, "deleted_bytes": 100, "candidates": ["x"]},
    })

    stats = _remote_pass(monkeypatch, cm)

    aged = db.get_chat("aged")
    assert aged["session_id"] is None and aged["codex_thread_id"] is None
    assert aged["pending_history_seed"] == "retention" and aged["updated_at"] == backdated
    for cid in ("fresh", "unpinned", "unplaced", "other"):
        assert db.get_chat(cid)["pending_history_seed"] == ""
    assert [mid for mid, _ in cm.sent] == ["m-a"]
    frame = cm.sent[0][1]
    assert frame["type"] == "session_files_retention" and frame["dry_run"] is False
    assert frame["min_age_s"] == retention.ORPHAN_GRACE_S
    assert frame["keep"] == sorted({fresh_sid, unplaced_sid})
    assert stats["chats_flagged"] == 1 and stats["remote_chats_flagged"] == 1
    assert stats["remote_session_files_deleted"] == 1 and stats["remote_bytes_freed"] == 100
    assert stats["remote_session_files_bytes"] == 300
    assert stats["remote_machines"] == [{
        "machine_id": "m-a", "name": "name-m-a", "files": 3, "bytes": 300,
        "deleted": 1, "deleted_bytes": 100, "candidates": ["x"], "reached": True,
    }]
    assert stats["remote_machines_unreached"] == 0


def test_a_live_remote_session_keeps_its_chat_and_rides_its_machines_keep_list(temp_db, monkeypatch):
    db = temp_db
    _set_username(db, "user-admin", "alice")
    sid, tid, elsewhere = _uuid(), _uuid(), _uuid()
    db.create_chat("live", "user-admin", "a1")
    db.update_chat("live", session_id=sid, codex_thread_id=tid, execution_target="m-a")
    _backdate_chat("live", 60)
    cm = _FakeCM({"m-a": "0.5.150"}, {"m-a": {"files": 1, "bytes": 10}})

    stats = _remote_pass(monkeypatch, cm, remote_sessions=[(sid, "m-a", tid), (elsewhere, "m-b", "")])

    assert db.get_chat("live")["session_id"] == sid
    assert stats["chats_flagged"] == 0
    assert cm.sent[0][1]["keep"] == sorted({sid, tid})   # the live ids, never m-b's
    assert stats["remote_session_files_bytes"] == 10


def test_an_unanswered_machine_is_unreached_and_a_dry_run_flags_nothing(temp_db, monkeypatch):
    db = temp_db
    _set_username(db, "user-admin", "alice")
    sid = _uuid()
    db.create_chat("aged", "user-admin", "a1")
    db.update_chat("aged", session_id=sid, execution_target="m-a")
    _backdate_chat("aged", 60)
    cm = _FakeCM({"m-a": "0.5.150"}, {"m-a": RuntimeError("no answer in 60 s")})

    stats = _remote_pass(monkeypatch, cm, dry_run=True)

    assert db.get_chat("aged")["session_id"] == sid          # a dry run flags nothing
    assert stats["chats_flagged"] == 1                        # but counts what it would
    assert cm.sent[0][1]["dry_run"] is True
    # The aged chat's id leaves the keep list as a real run's flagging would
    # take it out: the machine names its transcript as a candidate.
    assert cm.sent[0][1]["keep"] == []
    assert stats["remote_machines"][0]["reached"] is False
    assert stats["remote_machines_unreached"] == 1
    assert stats["remote_session_files_bytes"] is None
    # The knob off: no flagging, the machines are still asked (the orphans).
    cm2 = _FakeCM({"m-a": "0.5.150"}, {"m-a": {"files": 2, "bytes": 20}})
    stats = _remote_pass(monkeypatch, cm2, enabled=False)
    assert db.get_chat("aged")["session_id"] == sid and stats["chats_flagged"] == 0
    assert cm2.sent[0][1]["keep"] == [sid]


def test_a_keep_list_past_the_frame_cap_leaves_the_machine_unasked(temp_db, monkeypatch, caplog):
    import logging
    db = temp_db
    _set_username(db, "user-admin", "alice")
    monkeypatch.setattr(retention, "_KEEP_MAX", 2)
    for i in range(3):
        db.create_chat(f"c{i}", "user-admin", "a1")
        db.update_chat(f"c{i}", session_id=_uuid(), execution_target="m-a")
    cm = _FakeCM({"m-a": "0.5.150"}, {"m-a": {"files": 9, "bytes": 90}})

    with caplog.at_level(logging.WARNING, logger="otodock"):
        stats = _remote_pass(monkeypatch, cm)

    assert cm.sent == []
    assert stats["remote_machines"][0]["reached"] is False
    assert stats["remote_machines_unreached"] == 1
    assert stats["remote_session_files_bytes"] is None
    assert "is not asked: its keep list holds 3 ids" in caplog.text


def test_the_refs_by_target_split_the_pinned_rows_from_the_rest(temp_db):
    db = temp_db
    a, b, c, d = _uuid(), _uuid(), _uuid(), _uuid()
    db.create_chat("p", "user-admin", "a1")
    db.update_chat("p", session_id=a, codex_thread_id=b, execution_target="m-a")
    db.create_chat("l", "user-admin", "a1")
    db.update_chat("l", session_id=c, execution_target="local")
    db.create_chat("u", "user-admin", "a1")
    db.update_chat("u", session_id=d)
    by_target = db.get_session_refs_by_target()
    # A row is minted on the sandbox ('local'); only a pin names a machine.
    assert by_target["m-a"] == {a, b}
    assert by_target["local"] == {c, d}
    assert by_target[""] == set()   # the dynamic tasks' refs: none here


def test_storage_usage_shows_what_the_machines_held_at_the_last_sweep(temp_db):
    import json
    assert retention.compute_storage_usage()["remote_session_files_bytes"] is None
    temp_db.set_platform_setting("session_retention_last_sweep",
                                 json.dumps({"ran_at": "x", "remote_session_files_bytes": 1234}))
    assert retention.compute_storage_usage()["remote_session_files_bytes"] == 1234
    temp_db.set_platform_setting("session_retention_last_sweep",
                                 json.dumps({"ran_at": "x", "remote_session_files_bytes": None}))
    assert retention.compute_storage_usage()["remote_session_files_bytes"] is None

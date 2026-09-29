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
    import core.layers.cli.session as cli_session
    import core.layers.codex.session as codex_session
    import core.session.session_state as session_state
    import core.events.stream_pump as stream_pump

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
    from api.media import uploads
    monkeypatch.setattr(uploads, "staged_bytes_total", lambda: 4321)
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
    from storage.checks import db_checks
    from storage import pg
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

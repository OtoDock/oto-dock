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

import pytest

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


def _machine_session(monkeypatch, sid: str, *, agent: str, username: str, thread: str = ""):
    """A Codex session a compute machine runs for ``username`` on ``agent``:
    the remote layer's record of it (its files are in the platform's folder)
    and the security context its start registered, the only context there
    is. Returns the layer."""
    import asyncio
    from unittest.mock import MagicMock

    from auth.path_policy import SecurityContext
    from core.remote.remote_execution import RemoteExecutionLayer
    from core.remote.remote_session_info import RemoteSessionInfo
    from core.session import session_manager, session_state
    layer = RemoteExecutionLayer(MagicMock())
    layer._sessions[sid] = RemoteSessionInfo(
        session_id=sid, machine_id="compute-1", agent_name=agent, execution_path="codex-cli",
        event_queue=asyncio.Queue(), resume_handle=thread, compute=True, files_synced=False)
    monkeypatch.setattr(session_manager, "_remote_layer", layer)
    monkeypatch.setattr(session_state, "_session_security", {sid: SecurityContext(
        role="manager", username=username, agent=agent, is_admin_agent=False)})
    return layer


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


def _mk_companion(transcript: Path) -> Path:
    """Claude's ``<id>/`` folder beside a transcript (subagents, tool results)."""
    d = transcript.with_suffix("")
    (d / "subagents").mkdir(parents=True)
    (d / "subagents" / "agent-1.jsonl").write_text("z" * 300)
    return d


def _age_tree(d: Path, days: float) -> None:
    for f in [d, *d.rglob("*")]:
        _age_file(f, days)


def test_an_orphans_folder_goes_with_it(temp_db, monkeypatch):
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    _set_username(temp_db, "user-admin", "alice")
    home = _home("a1", "alice")
    orphan = _mk_claude(home, _uuid())
    folder = _mk_companion(orphan)
    _age_file(orphan, 10)
    _age_tree(folder, 10)

    stats = _sweep(days=30)

    assert not orphan.exists() and not folder.exists()
    assert stats["orphans_deleted"] == 1 and stats["orphan_bytes"] == 500
    assert stats["errors"] == 0


def test_a_referenced_transcript_keeps_its_folder(temp_db, monkeypatch):
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    _set_username(temp_db, "user-admin", "alice")
    sid = _uuid()
    temp_db.create_chat("ref", "user-admin", "a1")
    temp_db.update_chat("ref", session_id=sid)
    kept = _mk_claude(_home("a1", "alice"), sid)
    folder = _mk_companion(kept)
    _age_file(kept, 10)
    _age_tree(folder, 10)

    _sweep(days=30)

    assert kept.exists() and folder.exists()


def test_a_folder_left_by_an_older_sweep_ages_out_alone(temp_db, monkeypatch):
    """A folder whose transcript is already gone: aged and unreferenced it
    goes, young or referenced it stays."""
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    _set_username(temp_db, "user-admin", "alice")
    home = _home("a1", "alice")
    projects = home / ".claude" / "projects" / "-users-alice"
    projects.mkdir(parents=True)
    old_lone, young_lone, ref_lone = (projects / _uuid() for _ in range(3))
    for d in (old_lone, young_lone, ref_lone):
        (d / "tool-results").mkdir(parents=True)
        (d / "tool-results" / "r.txt").write_text("q" * 50)
    _age_tree(old_lone, 10)
    _age_tree(ref_lone, 10)
    temp_db.create_chat("ref", "user-admin", "a1")
    temp_db.update_chat("ref", session_id=ref_lone.name)

    stats = _sweep(days=30)

    assert not old_lone.exists()
    assert young_lone.exists() and ref_lone.exists()
    assert stats["orphans_deleted"] == 1 and stats["orphan_bytes"] == 50


def test_a_link_in_place_of_a_folder_is_left_and_its_target_kept(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    _set_username(temp_db, "user-admin", "alice")
    home = _home("a1", "alice")
    outside = tmp_path / "outside"
    (outside / "keep").mkdir(parents=True)
    (outside / "keep" / "precious.txt").write_text("p")
    orphan = _mk_claude(home, _uuid())
    orphan.with_suffix("").symlink_to(outside / "keep")
    _age_file(orphan, 10)
    # A project folder that is itself a link: nothing behind it is removed.
    linked = home / ".claude" / "projects" / "-linked"
    linked.symlink_to(outside)
    planted = outside / f"{_uuid()}.jsonl"
    planted.write_text("x")
    _age_file(planted, 10)

    stats = _sweep(days=30)

    assert not orphan.exists()
    assert (outside / "keep" / "precious.txt").exists() and planted.exists()
    assert stats["errors"] == 0


def test_an_aged_chat_removes_its_folder_with_its_file(temp_db, monkeypatch):
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    _set_username(temp_db, "user-admin", "alice")
    sid = _uuid()
    temp_db.create_chat("c1", "user-admin", "a1")
    temp_db.update_chat("c1", session_id=sid)
    _backdate_chat("c1", 60)
    cf = _mk_claude(_home("a1", "alice"), sid)
    folder = _mk_companion(cf)

    stats = _sweep(days=30)

    assert not cf.exists() and not folder.exists()
    assert stats["session_files_deleted"] == 1 and stats["bytes_freed"] == 500


def test_a_dry_run_counts_the_folder_and_removes_nothing(temp_db, monkeypatch):
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    _set_username(temp_db, "user-admin", "alice")
    orphan = _mk_claude(_home("a1", "alice"), _uuid())
    folder = _mk_companion(orphan)
    _age_file(orphan, 10)
    _age_tree(folder, 10)

    stats = _sweep(days=30, dry_run=True)

    assert orphan.exists() and folder.exists()
    assert stats["orphans_deleted"] == 1 and stats["orphan_bytes"] == 500


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


def test_a_temporary_sign_in_file_codex_left_goes(temp_db, monkeypatch):
    """Codex 0.169 saves its sign-in by a temporary ``.auth-XXXXXX`` and a
    rename; one a killed save left holds the credentials. An hour old, it
    goes from a store whatever runs there (a save takes a moment); the two
    empty lock files stay (a live Codex may hold one)."""
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    _set_username(temp_db, "user-admin", "alice")
    codex = _home("a1", "alice") / ".codex"
    codex.mkdir(parents=True, exist_ok=True)
    left, fresh = codex / ".auth-Ab12Cd", codex / ".auth-Ef34Gh"
    locks = [codex / "auth.lock", codex / "auth.json.lock"]
    for f in (left, fresh, *locks):
        f.write_text("z")
    for f in (left, *locks):
        _age_file(f, 1)
    _sweep(days=30)
    assert not left.exists() and fresh.exists()
    assert all(f.exists() for f in locks)


def test_codex_junk_prunes_no_folder_through_a_link(temp_db, monkeypatch):
    """A ``.tmp`` that is a link lists and prunes nothing in its target; a
    real ``.tmp`` keeps a folder that is not empty."""
    import config
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    _set_username(temp_db, "user-admin", "alice")
    linked, real = _home("a1", "alice"), _home("a2", "alice")
    target = Path(config.AGENTS_DIR) / "elsewhere"
    (target / "empty").mkdir(parents=True)
    (target / "kept.bin").write_text("z" * 10)
    _age_file(target / "kept.bin", 1)
    (linked / ".codex").mkdir(parents=True)
    (linked / ".codex" / ".tmp").symlink_to(target)
    (real / ".codex" / ".tmp" / "empty" / "deeper").mkdir(parents=True)
    (real / ".codex" / ".tmp" / "full").mkdir()
    (real / ".codex" / ".tmp" / "full" / "young.bin").write_text("z")

    _sweep(days=30)

    assert (target / "empty").is_dir() and (target / "kept.bin").exists()
    assert not (real / ".codex" / ".tmp" / "empty").exists()
    assert (real / ".codex" / ".tmp" / "full" / "young.bin").exists()


def test_an_agent_whose_folder_is_a_link_is_reached_by_the_passes(temp_db, monkeypatch, tmp_path):
    """The operator may move an agent's folder and leave a link in its place:
    an aged chat's files and Codex's junk go beneath the real folder, and a
    link below it still leads no removal out."""
    import config
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    _set_username(temp_db, "user-admin", "alice")
    real = tmp_path / "disk2" / "a3"
    real.mkdir(parents=True)
    (Path(config.AGENTS_DIR) / "a3").symlink_to(real)
    home = _home("a3", "alice")
    sid, tid = _uuid(), _uuid()
    temp_db.create_chat("c3", "user-admin", "a3")
    temp_db.update_chat("c3", session_id=sid, codex_thread_id=tid)
    _backdate_chat("c3", 60)
    cf, rf = _mk_claude(home, sid), _mk_rollout(home, tid)
    files = _mk_codex_junk(home)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "kept.bin").write_text("z" * 10)
    _age_file(target / "kept.bin", 1)
    (real / "users" / "alice" / ".codex" / ".tmp" / "ext").symlink_to(target)

    stats = _sweep(days=30)

    assert not cf.exists() and not rf.exists()
    assert stats["session_files_deleted"] == 2
    assert not files["logs"].exists() and not files["tmp_file"].exists()
    assert not (real / "users" / "alice" / ".codex" / ".tmp" / "plugins").exists()
    assert files["state"].exists()
    assert (target / "kept.bin").exists()


def test_a_file_of_no_agent_is_removed_beneath_the_agents_root(tmp_path, monkeypatch):
    """A cache folder of the agents folder holds no agent: its file goes
    beneath the agents root, and one behind a link stays."""
    from services.infra import safe_fs
    agents = tmp_path / "agents"
    (agents / ".cache").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    own = agents / ".cache" / "a.partial"
    own.write_text("x")
    retention._remove_beneath(own, tree=False)
    assert not own.exists()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "kept.partial").write_text("z")
    (agents / ".linked").symlink_to(elsewhere)
    with pytest.raises(safe_fs.SymlinkRefused):
        retention._remove_beneath(agents / ".linked" / "kept.partial", tree=False)
    assert (elsewhere / "kept.partial").read_text() == "z"


@pytest.mark.parametrize("shape", ["dot-dot", "relative"])
def test_a_path_whose_parts_are_refused_removes_nothing(tmp_path, monkeypatch, shape):
    """No pass builds such a path (each comes from a listing of the agents
    folder, and a ``Path`` folds a dot or an empty part away); one that did
    would be refused, never removed in place."""
    from services.infra import safe_fs
    agents = tmp_path / "agents"
    (agents / "ag").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    monkeypatch.chdir(tmp_path)
    victim = agents / "victim.partial"
    victim.write_text("x")
    (agents / "ag" / "victim.partial").write_text("y")
    path = {"dot-dot": Path(f"{agents}/ag/../victim.partial"),
            "relative": Path("agents/victim.partial")}[shape]
    with pytest.raises(safe_fs.SafeFsError):
        retention._remove_beneath(path, tree=False)
    assert victim.read_text() == "x"
    assert (agents / "ag" / "victim.partial").read_text() == "y"


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


def test_codex_junk_keeps_the_logs_of_a_home_with_a_live_machine_session(temp_db, monkeypatch):
    """The home of a session a compute machine runs is busy for as long as
    the remote layer holds the session: its logs, its staging files and its
    thread's month-old record stay, an idle home's go, and its own go once
    the session closed."""
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    _set_username(temp_db, "user-admin", "alice")
    running = _mk_codex_junk(_home("a1", "alice"))
    idle = _mk_codex_junk(_home("a2", "alice"))
    sid, thread = _uuid(), _uuid()
    record = _home("a1", "alice") / ".codex" / f"{thread}.text-sha256"
    record.write_text("z")
    _age_file(record, 31)
    layer = _machine_session(monkeypatch, sid, agent="a1", username="alice", thread=thread)

    _sweep(days=30, live=retention._build_live_snapshot())

    assert running["logs"].exists() and running["logs_wal"].exists() and running["tmp_file"].exists()
    assert record.exists()
    assert not idle["logs"].exists() and not idle["tmp_file"].exists()

    layer._sessions.pop(sid)
    _sweep(days=30, live=retention._build_live_snapshot())

    assert not running["logs"].exists() and not running["logs_wal"].exists()
    assert not running["tmp_file"].exists() and not record.exists()
    assert running["state"].exists()


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
    from types import SimpleNamespace

    import core.events.stream_pump as stream_pump
    import core.layers.direct.session as direct_session
    import core.session.session_state as session_state

    class _Ctx:
        def __init__(self, agent, username):
            self.agent = agent
            self.username = username

    thread = "019eb403-603e-7a02-bbe2-39d5a84b9f2a"
    on_a_machine = SimpleNamespace(session_id="sid-codex", machine_id="m-1", resume_handle=thread)
    monkeypatch.setattr(retention, "_remote_layer_for_sweep",
                        lambda: _FakeRemoteLayer({"sid-codex": on_a_machine}))
    monkeypatch.setattr(direct_session, "_direct_sessions", {"sid-live": object()})
    monkeypatch.setattr(session_state, "_session_security", {
        "sid-live": _Ctx("a1", "alice"),     # a session the platform holds → busy
        "sid-stale": _Ctx("a2", "alice"),    # restart residue → ignored
    })
    monkeypatch.setattr(stream_pump, "_active_pumps", {})

    snap = retention._build_live_snapshot()
    assert ("a1", "alice") in snap.busy_homes
    assert all(agent != "a2" for agent, _u in snap.busy_homes)
    assert "sid-stale" not in snap.session_ids
    assert "sid-live" in snap.session_ids and "sid-codex" in snap.session_ids
    assert thread in snap.codex_thread_ids


def test_a_session_a_machine_runs_makes_its_home_busy_and_its_thread_live(temp_db, monkeypatch):
    """A session a machine runs is in no pool of the platform's own: the
    remote layer's record makes its id and its thread live, and the context
    of that session makes its home busy."""
    import core.events.stream_pump as stream_pump
    import core.layers.direct.session as direct_session
    sid, thread = _uuid(), _uuid()
    _machine_session(monkeypatch, sid, agent="a1", username="alice", thread=thread)
    monkeypatch.setattr(direct_session, "_direct_sessions", {})
    monkeypatch.setattr(stream_pump, "_active_pumps", {})

    snap = retention._build_live_snapshot()

    assert sid in snap.session_ids and thread in snap.codex_thread_ids
    assert snap.busy_homes == {("a1", "alice")}


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
    from services.infra import git_writer
    histories = git_writer.history_root() / "offboarded"
    for name in ("old-pat", "young"):
        (histories / name / "a1" / "context.git").mkdir(parents=True)
    stats = {"offboarded_deleted": 0, "offboarded_bytes": 0, "errors": 0}
    retention._pass_offboarded_archives(stats, dry_run=True)
    assert stats["offboarded_deleted"] == 1 and stats["offboarded_bytes"] >= 200 and old.exists()
    assert (histories / "old-pat").is_dir()
    stats = {"offboarded_deleted": 0, "offboarded_bytes": 0, "errors": 0}
    retention._pass_offboarded_archives(stats, dry_run=False)
    assert stats["offboarded_deleted"] == 1 and not old.exists()
    # The person's histories go with their archive, and nobody else's.
    assert not (histories / "old-pat").exists() and (histories / "young" / "a1" / "context.git").is_dir()
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

    def is_compute(self, machine_id):
        return False

    def shares_folders(self, machine_id):
        return False

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
    never another machine's nor a sandbox chat's."""
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
    cm = _FakeCM({"m-a": "0.5.150"}, {
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


def test_a_chat_on_a_compute_machine_on_another_host_is_on_that_machines_list(temp_db):
    db = temp_db
    a = _uuid()
    db.create_chat("o", "user-admin", "a1")
    db.update_chat("o", session_id=a, execution_target="local")
    from storage import remote_store
    remote_store.set_chat_compute_machine("o", "rack-two")
    by_target = db.get_session_refs_by_target()
    # Its pin stays 'local'; its engine record lives on the machine.
    assert by_target["rack-two"] == {a}
    assert a not in by_target.get("local", set())


def test_storage_usage_shows_what_the_machines_held_at_the_last_sweep(temp_db):
    import json
    assert retention.compute_storage_usage()["remote_session_files_bytes"] is None
    temp_db.set_platform_setting("session_retention_last_sweep",
                                 json.dumps({"ran_at": "x", "remote_session_files_bytes": 1234}))
    assert retention.compute_storage_usage()["remote_session_files_bytes"] == 1234
    temp_db.set_platform_setting("session_retention_last_sweep",
                                 json.dumps({"ran_at": "x", "remote_session_files_bytes": None}))
    assert retention.compute_storage_usage()["remote_session_files_bytes"] is None


# ---------------------------------------------------------------------------
# Reply reports: a year after they were filed
# ---------------------------------------------------------------------------

def _report_filed(chat_id: str, message_id: int, days_ago: int) -> int:
    from storage import db_reply_reports
    from storage.pg import get_conn
    report_id = db_reply_reports.add("user-admin", chat_id, message_id, agent="a1",
                                     reason="harmful", note="", reply_text="text")
    with get_conn() as conn:
        conn.execute("UPDATE reply_reports SET created_at = now() - make_interval(days => %s) "
                     "WHERE id = %s", (days_ago, report_id))
        conn.commit()
    return report_id


def _report_ids() -> list[int]:
    from storage.pg import get_conn
    with get_conn() as conn:
        return [r["id"] for r in conn.execute("SELECT id FROM reply_reports ORDER BY id").fetchall()]


def test_a_report_goes_a_year_after_it_was_filed(temp_db, monkeypatch):
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    from storage import db_reply_reports
    temp_db.create_chat("rep-c1", "user-admin", "a1")
    old = _report_filed("rep-c1", 101, db_reply_reports.KEEP_DAYS + 1)
    kept = _report_filed("rep-c1", 102, db_reply_reports.KEEP_DAYS - 1)
    stats = _sweep(enabled=False, dry_run=True)
    assert stats["reply_reports_deleted"] == 1 and _report_ids() == [old, kept]
    stats = _sweep(enabled=False)
    assert stats["reply_reports_deleted"] == 1 and _report_ids() == [kept]


def test_codex_junk_takes_a_crashed_sessions_files_and_old_records(temp_db, monkeypatch):
    """A Codex home's per-session files outlive a session only after a
    crash: an idle home loses them; a thread's record goes after a month."""
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    _set_username(temp_db, "user-admin", "alice")
    codex = _home("a1", "alice") / ".codex"
    codex.mkdir(parents=True, exist_ok=True)
    files = {name: codex / name for name in (
        "s-1.config.toml", "s-1.session.json", "t-old.text-sha256", "t-new.text-sha256")}
    for f in files.values():
        f.write_text("z")
        _age_file(f, 1)
    _age_file(files["t-old.text-sha256"], 31 * 24)
    _sweep(days=30)
    assert not files["s-1.config.toml"].exists() and not files["s-1.session.json"].exists()
    assert not files["t-old.text-sha256"].exists()
    assert files["t-new.text-sha256"].exists()


def test_codex_junk_keeps_the_files_of_a_running_session(temp_db, monkeypatch):
    """A running session's own files stay whatever their age, in a home the
    snapshot calls idle too (a terminal's is not in it): a terminal's and a
    compute session's stay while they run; a closed session's go."""
    from core.session import interactive_registry, session_manager
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    _set_username(temp_db, "user-admin", "alice")
    codex = _home("a1", "alice") / ".codex"
    codex.mkdir(parents=True, exist_ok=True)
    files = {name: codex / name for name in (
        "s-term.config.toml", "s-term.session.json", "s-compute.session.json",
        "s-closed.config.toml", "s-closed.session.json")}
    for f in files.values():
        f.write_text("z")
        _age_file(f, 1)
    monkeypatch.setitem(interactive_registry._sessions, "s-term", object())
    real = session_manager.live_session
    monkeypatch.setattr(session_manager, "live_session",
                        lambda sid: object() if sid == "s-compute" else real(sid))
    # A busy home (another session runs in it) still loses a closed
    # session's files: one closed after a proxy restart left them behind.
    busy = _home("a2", "alice") / ".codex"
    busy.mkdir(parents=True, exist_ok=True)
    gone, logs = busy / "s-gone.session.json", busy / "logs_1.sqlite"
    for f in (gone, logs):
        f.write_text("z")
        _age_file(f, 1)
    _sweep(days=30, live=LiveSnapshot(busy_homes={("a2", "alice")}))
    assert files["s-term.config.toml"].exists() and files["s-term.session.json"].exists()
    assert files["s-compute.session.json"].exists()
    assert not files["s-closed.config.toml"].exists() and not files["s-closed.session.json"].exists()
    assert not gone.exists() and logs.exists()


def test_codex_junk_keeps_session_files_until_a_compute_machine_is_back(temp_db, monkeypatch):
    """After a platform restart a session that runs on a compute machine is
    in no registry until the machine's first complete report adopts it: its
    own files stay while the start still waits for that machine
    (``compute_bootstrap.waiting``), and a closed one's go after. A thread's
    record keeps its own rule."""
    from services.remote import compute_bootstrap
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    _set_username(temp_db, "user-admin", "alice")
    codex = _home("a1", "alice") / ".codex"
    codex.mkdir(parents=True, exist_ok=True)
    held = codex / "s-on-compute.session.json"
    held.write_text("z")
    _age_file(held, 1)
    record = codex / "t-old.text-sha256"
    record.write_text("z")
    _age_file(record, 31)
    compute_bootstrap.reset_for_tests()
    compute_bootstrap._expected.add("compute-1")
    try:
        _sweep(days=30)
        assert held.exists()                   # the machine has not reported yet
        assert not record.exists()             # a month-old record goes regardless
        compute_bootstrap._expected.discard("compute-1")   # its report came
        _sweep(days=30)
        assert not held.exists()
    finally:
        compute_bootstrap.reset_for_tests()


# ---------------------------------------------------------------------------
# Sessions' homes (core/sandbox/session_home) in the platform's folders
# ---------------------------------------------------------------------------

def _homes(sids, *, age_s: float = 2 * 3600):
    """Homes made ``age_s`` ago (their folder's time set back)."""
    from core.sandbox import session_home
    store = _home("pa", "alice") / ".claude"
    store.mkdir(parents=True, exist_ok=True)
    then = time.time() - age_s
    for sid in sids:
        home = session_home.prepare(store, sid, ("projects",))
        (home / ".credentials.json").write_text(sid)
        os.utime(home, (then, then))
    return store


def test_a_home_made_in_the_last_hour_is_left(temp_db, monkeypatch):
    """A start makes its home long before its binding row (the files, the
    tools, the machine's answer come first), and a terminal is in no
    registry the pass reads: a home that changed in the last hour stays."""
    from services.engines import subscription_pool
    starting = "f" * 8 + "-1111-2222-3333-444444444444"
    store = _homes([starting], age_s=60)
    monkeypatch.setattr(subscription_pool, "within_boot_grace", lambda: False)
    monkeypatch.setattr(subscription_pool, "session_gone", lambda sid: True)
    stats = {"session_homes_removed": 0}
    retention._pass_session_homes(LiveSnapshot(), stats, dry_run=False)
    assert (store / "own" / starting).is_dir() and stats["session_homes_removed"] == 0


def test_a_start_of_an_existing_home_touches_it(tmp_path):
    """A chat started again in the home its last session left (the same id)
    counts as fresh to the daily pass."""
    from types import SimpleNamespace

    from core.layers.cli.layer import CLIExecutionLayer
    from core.sandbox import session_home
    sid = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    store = tmp_path / ".claude"
    store.mkdir()
    layer = CLIExecutionLayer.__new__(CLIExecutionLayer)
    spec = layer.capabilities.auth.session_home
    home = session_home.prepare(store, sid, spec.links, spec.seed_map())
    then = time.time() - 2 * 3600
    os.utime(home, (then, then))
    layer._make_home(sid, SimpleNamespace(sandbox_host_claude_dir=str(store)))
    assert time.time() - home.stat().st_mtime < 60


def test_a_start_touches_its_home_with_no_link_followed(tmp_path, monkeypatch):
    """The home's time is set through a handle opened beneath the agent's
    real folder: a home whose name is a link at the touch refuses the start,
    and the link's target keeps its time."""
    from types import SimpleNamespace

    import config as app_config
    from core.layers.cli.layer import CLIExecutionLayer
    from core.sandbox import session_home
    sid = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    agents = tmp_path / "agents"
    store = agents / "pa" / "workspace" / ".claude"
    (store / "own").mkdir(parents=True)
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents)
    stand_in = tmp_path / "stand-in"
    stand_in.mkdir()
    then = time.time() - 2 * 3600
    os.utime(stand_in, (then, then))
    linked = store / "own" / sid
    linked.symlink_to(stand_in)
    monkeypatch.setattr(session_home, "prepare", lambda *a, **k: linked)
    layer = CLIExecutionLayer.__new__(CLIExecutionLayer)
    with pytest.raises(OSError):
        layer._make_home(sid, SimpleNamespace(sandbox_host_claude_dir=str(store), agent_name="pa"))
    assert time.time() - stand_in.stat().st_mtime > 3600


def test_the_homes_of_gone_sessions_are_swept_and_live_ones_kept(temp_db, monkeypatch):
    """One answer decides (``subscription_pool.session_gone``): a home
    stays unless its session is gone."""
    from services.engines import subscription_pool
    gone, live, unknown = (f"{c * 8}-1111-2222-3333-444444444444" for c in "abc")
    store = _homes([gone, live, unknown])
    monkeypatch.setattr(subscription_pool, "within_boot_grace", lambda: False)
    monkeypatch.setattr(subscription_pool, "session_gone", lambda sid: sid == gone)
    dropped: list[str] = []
    monkeypatch.setattr(subscription_pool, "drop_ghost_binding", dropped.append)
    stats = {"session_homes_removed": 0}
    retention._pass_session_homes(LiveSnapshot(), stats, dry_run=False)
    assert stats["session_homes_removed"] == 1 and dropped == [gone]
    assert sorted(p.name for p in (store / "own").iterdir()) == sorted([live, unknown])
    assert (store / "projects").is_dir()


def _pool_alone(monkeypatch, *, held=(), connected=(), reported=(), compute=()):
    """The pool's liveness with its three sources as the test says: the
    registries (``held``), the machines here and the machines that reported
    since this start, and the compute machines' rows."""
    from core.remote import satellite_connection, satellite_link
    from core.session import session_state
    from services.engines import subscription_pool
    from storage import remote_store

    class _Machines:
        def is_connected(self, machine_id):
            return machine_id in connected

    monkeypatch.setattr(session_state, "session_is_held", lambda sid: sid in held)
    monkeypatch.setattr(subscription_pool, "within_boot_grace", lambda: False)
    monkeypatch.setattr(satellite_connection, "get_connection_manager", _Machines)
    monkeypatch.setattr(satellite_link, "reported_since_boot", lambda m: m in reported)
    monkeypatch.setattr(remote_store, "list_compute_machines", lambda: [{"id": m} for m in compute])


def _bind_row(db, sid: str, target: str, store) -> None:
    from storage.billing import subscription_store
    subscription_store.upsert_session_binding(
        sid, "sub-of-the-test", layer="claude-code-cli", user_sub=None,
        scope_key=f"{target}:{store / 'own' / sid}")


def test_a_stored_row_with_no_session_keeps_no_home(temp_db, monkeypatch):
    """Both units restarted together, or the platform declined the session
    after a restart: no registry names it and nothing released its row.
    Once its machine is back and has reported, the home goes with the row."""
    from storage.billing import subscription_store
    ghost, away, quiet = (f"{c * 8}-1111-2222-3333-444444444444" for c in "abc")
    store = _homes([ghost, away, quiet])
    _pool_alone(monkeypatch, connected={"c-here", "c-quiet"}, reported={"c-here"},
                compute={"c-here"})
    _bind_row(temp_db, ghost, "local", store)
    _bind_row(temp_db, away, "c-away", store)
    _bind_row(temp_db, quiet, "c-quiet", store)
    stats = {"session_homes_removed": 0}
    retention._pass_session_homes(LiveSnapshot(), stats, dry_run=False)
    from services.engines import subscription_pool
    subscription_pool.flush_binding_writes()
    assert sorted(p.name for p in (store / "own").iterdir()) == sorted([away, quiet])
    assert subscription_store.get_session_binding(ghost) is None
    assert subscription_store.get_session_binding(away) and subscription_store.get_session_binding(quiet)
    assert stats["session_homes_removed"] == 1


def test_a_live_terminal_with_no_row_keeps_its_home(temp_db, monkeypatch):
    """A terminal is in the interactive registry alone. One whose row the
    startup prune took (older than a week at a restart) still runs from its
    home."""
    terminal, gone = (f"{c * 8}-1111-2222-3333-444444444444" for c in "ab")
    store = _homes([terminal, gone])
    _pool_alone(monkeypatch, held={terminal})
    stats = {"session_homes_removed": 0}
    retention._pass_session_homes(LiveSnapshot(), stats, dry_run=False)
    assert [p.name for p in (store / "own").iterdir()] == [terminal]


def test_a_home_that_cannot_be_removed_does_not_keep_the_next(temp_db, monkeypatch):
    from core.sandbox import session_home
    from services.engines import subscription_pool
    stuck, next_one = (f"{c * 8}-1111-2222-3333-444444444444" for c in "ab")
    store = _homes([stuck, next_one])
    monkeypatch.setattr(subscription_pool, "within_boot_grace", lambda: False)
    monkeypatch.setattr(subscription_pool, "session_gone", lambda sid: True)
    monkeypatch.setattr(subscription_pool, "drop_ghost_binding", lambda sid: None)
    real = session_home.drop

    def _drop(folder, sid, links):
        if sid == stuck:
            raise OSError("a file that cannot be removed")
        return real(folder, sid, links)
    monkeypatch.setattr(session_home, "drop", _drop)
    stats = {"session_homes_removed": 0, "errors": 0}
    retention._pass_session_homes(LiveSnapshot(), stats, dry_run=False)
    assert [p.name for p in (store / "own").iterdir()] == [stuck]
    assert stats == {"session_homes_removed": 1, "errors": 1}


def test_the_daily_run_waits_for_the_boot_grace(temp_db, monkeypatch):
    """The first run after a start comes once the registries are whole: a
    run inside the grace would pass over every home and the next would be a
    day later, which an install that restarts every day never reaches."""
    import asyncio

    from services.engines import subscription_pool
    sid = "a" * 8 + "-1111-2222-3333-444444444444"
    store = _homes([sid])
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    monkeypatch.setattr(retention, "_build_live_snapshot", LiveSnapshot)
    monkeypatch.setattr(retention, "_last_run", 0.0)
    monkeypatch.setattr(subscription_pool, "session_gone", lambda s: True)
    monkeypatch.setattr(subscription_pool, "drop_ghost_binding", lambda s: None)
    monkeypatch.setattr(subscription_pool, "within_boot_grace", lambda: True)
    asyncio.run(retention.maybe_run_daily())
    assert retention._last_run == 0.0 and (store / "own" / sid).is_dir()
    monkeypatch.setattr(subscription_pool, "within_boot_grace", lambda: False)
    asyncio.run(retention.maybe_run_daily())
    assert retention._last_run > 0 and not (store / "own" / sid).exists()


def test_no_home_is_swept_inside_the_boot_grace_or_on_a_dry_run(temp_db, monkeypatch):
    from services.engines import subscription_pool
    sid = "e" * 8 + "-1111-2222-3333-444444444444"
    store = _homes([sid])
    monkeypatch.setattr(subscription_pool, "session_gone", lambda s: True)
    dropped: list[str] = []
    monkeypatch.setattr(subscription_pool, "drop_ghost_binding", dropped.append)
    monkeypatch.setattr(subscription_pool, "within_boot_grace", lambda: True)
    stats = {"session_homes_removed": 0}
    retention._pass_session_homes(LiveSnapshot(), stats, dry_run=False)
    assert (store / "own" / sid).is_dir() and stats["session_homes_removed"] == 0
    monkeypatch.setattr(subscription_pool, "within_boot_grace", lambda: False)
    retention._pass_session_homes(LiveSnapshot(), stats, dry_run=True)
    assert (store / "own" / sid).is_dir() and stats["session_homes_removed"] == 1
    assert dropped == []


def test_the_histories_of_an_agent_that_is_gone_are_removed_and_no_other(temp_db, caplog):
    """The backstop for a delete whose own removal failed: no row AND no
    folder. A folder alone, or a row alone, keeps the histories."""
    import logging

    from services.infra import git_writer
    from storage.agents import agent_store
    agent_store.create_agent("hist-kept", "Kept")
    root = git_writer.history_root()
    (Path(config.AGENTS_DIR) / "hist-folder-only" / "config").mkdir(parents=True)
    for name in ("hist-folder-only", "hist-gone"):
        (root / "agents" / name / "config.git").mkdir(parents=True)
    (root / "set-aside" / "hist-gone" / "config.old-git-1").mkdir(parents=True)
    with caplog.at_level(logging.INFO):
        retention._pass_orphan_histories({}, dry_run=True)
    assert (root / "agents" / "hist-gone").is_dir() and "would remove 2 histories" in caplog.text
    retention._pass_orphan_histories({}, dry_run=False)
    assert not (root / "agents" / "hist-gone").exists() and not (root / "set-aside" / "hist-gone").exists()
    assert (root / "agents" / "hist-kept" / "config.git").is_dir()
    assert (root / "agents" / "hist-folder-only" / "config.git").is_dir()
    assert any(name == "orphan-histories" for name in _pass_names())


def _pass_names() -> list[str]:
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(retention))
    return [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value.startswith("orphan-")]


def test_storage_usage_follows_no_link_in_a_home(temp_db, tmp_path):
    """The admin card's sizes of a home's engine folders follow no link: a
    sessions folder replaced by a link counts nothing of its target."""
    _set_username(temp_db, "user-admin", "alice")
    home = _home("a1", "alice")
    stand_in = tmp_path / "stand-in"
    (stand_in / "2026").mkdir(parents=True)
    (stand_in / "2026" / "big.jsonl").write_bytes(b"z" * 5000)
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "sessions").symlink_to(stand_in)
    assert retention.compute_storage_usage()["session_files_bytes"] < 5000


def test_a_homes_settings_come_from_the_store_with_no_link_followed(tmp_path, monkeypatch):
    """A home's settings are the store's own bytes, read beneath the
    agent's real folder: a store settings file that is a link is refused,
    and its target never reaches the home."""
    from types import SimpleNamespace

    import config as app_config
    from core.layers.cli.layer import CLIExecutionLayer
    sid = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    agents = tmp_path / "agents"
    store = agents / "pa" / "workspace" / ".claude"
    store.mkdir(parents=True)
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents)
    (tmp_path / "elsewhere.json").write_text('{"theirs": true}')
    (store / "settings.json").symlink_to(tmp_path / "elsewhere.json")
    layer = CLIExecutionLayer.__new__(CLIExecutionLayer)
    with pytest.raises(OSError):
        layer._make_home(sid, SimpleNamespace(sandbox_host_claude_dir=str(store), agent_name="pa"))
    assert not (store / "own" / sid / "settings.json").exists()

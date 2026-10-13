"""Regression test for ``detect_changes`` snapshot refresh.

The bug: ``file_sync.detect_changes`` was returning the diff between the
agent_dir and the snapshot, but NOT refreshing the snapshot afterwards.
Combined with the session lifecycle (snapshot taken ONCE at session
``prepare``, only mutated by ``apply_file_push``), this meant every
end-of-turn scan re-reported every file the session had ever modified.

User-visible symptom: after a few turns the satellite's send queue
filled with thousands of duplicate ``file_changed`` events. When the WS
dropped (e.g. laptop slept, clock jump triggered reconnect), the queue
hit its 10K cap and the satellite logged ``Send queue full ... dropped
oldest: file_changed`` repeatedly. The proxy was also applying the same
writes to the platform's agent_dir over and over.

Fix: ``detect_changes`` now mutates the caller's snapshot dict in place
after computing the diff. The session's ``_file_snapshot`` attribute is
a live reference to that dict, so the next call sees the refreshed
baseline and only emits genuinely new changes.
"""

from __future__ import annotations

import os

import pytest

from satellite.transport import file_sync as _file_sync
from satellite.transport.file_sync import (
    compute_manifest,
    detect_changes,
    snapshot_agent_dir,
)

_REAL_HASH_FILE = _file_sync._hash_file


def test_repeat_call_with_no_writes_returns_no_changes(tmp_path):
    """The bug: stable filesystem → still re-emits the original delta
    because the snapshot stays pinned. Fix: second call sees empty diff."""
    agent = tmp_path
    (agent / "workspace").mkdir()
    (agent / "workspace" / "a.txt").write_text("v1")

    snapshot: dict[str, str] = {}
    changes1 = detect_changes(agent, snapshot)
    # First call: ``a.txt`` is new vs. an empty snapshot → reported once.
    assert any(c.get("path") == "workspace/a.txt" for c in changes1)
    assert len(changes1) == 1

    # Second call without any writes: snapshot was refreshed by the
    # first call, so the diff is empty. Before the fix, this returned
    # ``[{"path": "workspace/a.txt", ...}]`` again — the bug.
    changes2 = detect_changes(agent, snapshot)
    assert changes2 == []


def test_second_turn_only_sees_new_writes(tmp_path):
    """Two-turn simulation: turn 1 writes file A, turn 2 writes file B.
    Turn 2's detect must report ONLY B — not A again."""
    agent = tmp_path
    (agent / "workspace").mkdir()

    # Session-prepare snapshot — empty dir.
    snapshot = snapshot_agent_dir(agent)

    # ── turn 1 ──
    (agent / "workspace" / "a.txt").write_text("turn-1")
    turn1 = detect_changes(agent, snapshot)
    paths1 = {c["path"] for c in turn1}
    assert paths1 == {"workspace/a.txt"}

    # ── turn 2: write a new file, leave a.txt untouched ──
    (agent / "workspace" / "b.txt").write_text("turn-2")
    turn2 = detect_changes(agent, snapshot)
    paths2 = {c["path"] for c in turn2}
    # Before the fix this was {"workspace/a.txt", "workspace/b.txt"}.
    # After the fix, snapshot was refreshed at end of turn 1, so only b
    # appears as new.
    assert paths2 == {"workspace/b.txt"}


def test_modification_after_first_turn_reported_once(tmp_path):
    """File modified across two turns: turn 1 reports it as new, turn 2
    reports the new content, turn 3 (no change) reports nothing."""
    agent = tmp_path
    (agent / "workspace").mkdir()
    f = agent / "workspace" / "a.txt"

    snapshot = snapshot_agent_dir(agent)

    f.write_text("v1")
    turn1 = detect_changes(agent, snapshot)
    assert len(turn1) == 1

    f.write_text("v2")
    turn2 = detect_changes(agent, snapshot)
    assert len(turn2) == 1
    # Hash of "v2" differs from hash of "v1" — must be reported.
    assert turn2[0]["path"] == "workspace/a.txt"

    # Turn 3: no writes. Must be empty.
    turn3 = detect_changes(agent, snapshot)
    assert turn3 == []


def test_deletion_reported_once_then_baseline_caught_up(tmp_path):
    """A deleted file is reported on the turn it disappears. Subsequent
    turns must NOT re-report the same deletion (would force the platform
    to ``unlink`` an already-missing file every turn)."""
    agent = tmp_path
    (agent / "workspace").mkdir()
    f = agent / "workspace" / "a.txt"
    f.write_text("v1")

    snapshot = snapshot_agent_dir(agent)

    f.unlink()
    turn1 = detect_changes(agent, snapshot)
    assert turn1 == [{"path": "workspace/a.txt", "action": "delete"}]

    turn2 = detect_changes(agent, snapshot)
    assert turn2 == []


def test_real_venv_skipped_but_named_dir_synced(tmp_path):
    """E1: a real venv (pyvenv.cfg) is skipped from the manifest, while a
    workspace dir merely NAMED ``venv`` (no pyvenv.cfg) still syncs."""
    agent = tmp_path
    ws = agent / "workspace"
    ws.mkdir()
    # Real venv under a non-SKIP_DIRS name → detected via pyvenv.cfg, skipped.
    real = ws / "myenv"
    real.mkdir()
    (real / "pyvenv.cfg").write_text("home = /usr\n")
    (real / "script.py").write_text("import x")
    # A workspace dir merely NAMED `venv` with no pyvenv.cfg → synced.
    fake = ws / "venv"
    fake.mkdir()
    (fake / "keep.txt").write_text("data")

    snap = snapshot_agent_dir(agent)
    assert "workspace/venv/keep.txt" in snap            # named-venv dir synced
    assert "workspace/myenv/script.py" not in snap       # real venv skipped
    assert not any("myenv/pyvenv.cfg" in k for k in snap)


def test_oversized_file_skipped(tmp_path, monkeypatch):
    """E2: files larger than MAX_FILE_SIZE are skipped from the manifest."""
    monkeypatch.setattr("satellite.transport.file_sync.MAX_FILE_SIZE", 10)  # 10-byte cap for the test
    agent = tmp_path
    ws = agent / "workspace"
    ws.mkdir()
    (ws / "small.txt").write_text("ok")       # 2 bytes ≤ 10
    (ws / "big.txt").write_text("x" * 50)     # 50 bytes > 10

    snap = snapshot_agent_dir(agent)
    assert "workspace/small.txt" in snap
    assert "workspace/big.txt" not in snap


def test_snapshot_dict_is_mutated_in_place(tmp_path):
    """Sanity: the caller's snapshot dict reference is the SAME dict
    after detect_changes returns — we mutate via .clear()/.update(), not
    replace. The session object holds this dict as ``_file_snapshot``;
    if we did ``snapshot = ...`` instead, the session's attribute would
    still point at the old dict and the fix would be invisible to it.
    """
    agent = tmp_path
    (agent / "workspace").mkdir()
    (agent / "workspace" / "a.txt").write_text("v1")

    snapshot: dict[str, str] = {}
    snapshot_id = id(snapshot)

    detect_changes(agent, snapshot)
    assert id(snapshot) == snapshot_id
    # And it now contains the file's hash.
    assert "workspace/a.txt" in snapshot
    assert snapshot["workspace/a.txt"].startswith("sha256:")


def test_codex_runtime_sqlite_excluded_from_snapshot(tmp_path):
    """Host-local ``.codex`` files must never appear in the snapshot: the
    app-server's per-machine SQLite state AND the per-session regenerated
    config files (this satellite writes its own ``config.toml``/``AGENTS.md``/
    ``hooks.json``/``auth.json`` at session start from the payload — the
    platform's copies carry real secrets and must never sync here).
    ``sessions/`` transcripts are excluded as runtime cruft."""
    agent = tmp_path
    cdir = agent / "users" / "u" / ".codex"
    (cdir / "sessions").mkdir(parents=True)
    (cdir / "config.toml").write_text("x = 1")
    (cdir / "AGENTS.md").write_text("# prompt")
    (cdir / "hooks.json").write_text("{}")
    (cdir / "auth.json").write_text("{}")
    (cdir / "sessions" / "thr.jsonl").write_text("{}")
    (cdir / "state_v3.sqlite").write_bytes(b"db")
    (cdir / "state_v3.sqlite-wal").write_bytes(b"wal")
    (cdir / "logs_2026.sqlite").write_bytes(b"db")
    (cdir / "goals_1.sqlite").write_bytes(b"db")
    (cdir / "memories_1.sqlite").write_bytes(b"db")

    snap = snapshot_agent_dir(agent)
    assert "users/u/.codex/auth.json" not in snap     # host-local
    assert "users/u/.codex/config.toml" not in snap   # host-local
    assert "users/u/.codex/AGENTS.md" not in snap     # host-local
    assert "users/u/.codex/hooks.json" not in snap    # host-local
    assert "users/u/.codex/sessions/thr.jsonl" not in snap  # runtime cruft
    assert "users/u/.codex/state_v3.sqlite" not in snap
    assert "users/u/.codex/state_v3.sqlite-wal" not in snap
    assert "users/u/.codex/logs_2026.sqlite" not in snap
    assert "users/u/.codex/goals_1.sqlite" not in snap
    assert "users/u/.codex/memories_1.sqlite" not in snap


def test_codex_runtime_sqlite_never_echoed_per_turn(tmp_path):
    """Regression for the noisy ``migration N … has been modified`` churn:
    the daemon rewrites ``.codex/state_*.sqlite`` every turn, but
    detect_changes must NOT emit a file_changed for it — only the real
    workspace write is reported. (detect_changes delegates to
    snapshot_agent_dir, so the snapshot exclusion covers it.)"""
    agent = tmp_path
    cdir = agent / "workspace" / ".codex"
    cdir.mkdir(parents=True)
    (cdir / "state_v3.sqlite").write_bytes(b"v1")

    snapshot = snapshot_agent_dir(agent)
    # Daemon rewrites its state DB mid-turn + a genuine workspace write.
    (cdir / "state_v3.sqlite").write_bytes(b"v2-changed-by-daemon")
    (agent / "workspace" / "out.txt").write_text("result")

    paths = {c["path"] for c in detect_changes(agent, snapshot)}
    assert paths == {"workspace/out.txt"}  # the SQLite churn is NOT reported


def test_codex_runtime_sqlite_excluded_from_compute_manifest(tmp_path):
    """The request-manifest path mirrors the snapshot exclusion."""
    agent = tmp_path
    cdir = agent / "workspace" / ".codex"
    cdir.mkdir(parents=True)
    (cdir / "config.toml").write_text("x = 1")
    (cdir / "auth.json").write_text("{}")
    (cdir / "state_v3.sqlite").write_bytes(b"db")
    (cdir / "logs_x.sqlite-shm").write_bytes(b"shm")

    paths = {e["path"] for e in compute_manifest(agent)}
    assert "workspace/.codex/auth.json" not in paths    # host-local
    assert "workspace/.codex/config.toml" not in paths  # host-local
    assert "workspace/.codex/state_v3.sqlite" not in paths
    assert "workspace/.codex/logs_x.sqlite-shm" not in paths


def test_symlinks_skipped_in_snapshot_and_manifest(tmp_path):
    """Symlinks must be skipped on BOTH paths to match the proxy
    (proxy/core/remote/file_sync.py) — else a symlink is satellite-only and the merge
    materializes its target as a regular file on the platform."""
    agent = tmp_path
    (agent / "workspace").mkdir()
    real = agent / "workspace" / "real.txt"
    real.write_text("content")
    link = agent / "workspace" / "link.txt"
    link.symlink_to(real)

    snap = snapshot_agent_dir(agent)
    assert "workspace/real.txt" in snap
    assert "workspace/link.txt" not in snap

    paths = {e["path"] for e in compute_manifest(agent)}
    assert "workspace/real.txt" in paths
    assert "workspace/link.txt" not in paths


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes are POSIX")
def test_a_named_pipe_is_skipped_without_blocking(tmp_path):
    """A FIFO in a workspace (a tool's control pipe) is listed by the walk
    with size 0; opening it for a hash would block until a writer appears,
    so the snapshot and the manifest skip it and hash only regular files."""
    import threading
    agent = tmp_path
    (agent / "workspace").mkdir()
    (agent / "workspace" / "real.txt").write_text("content")
    os.mkfifo(agent / "workspace" / "pipe")
    out: dict = {}

    def run():
        out["snap"] = snapshot_agent_dir(agent)
        out["paths"] = {e["path"] for e in compute_manifest(agent)}

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(10)
    if t.is_alive():
        # Unblock the stalled open so the thread can end, then fail.
        fd = os.open(agent / "workspace" / "pipe", os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        pytest.fail("hashing a named pipe blocked")
    assert "workspace/real.txt" in out["snap"] and "workspace/pipe" not in out["snap"]
    assert out["paths"] == {"workspace/real.txt"}


def _windows_os(monkeypatch):
    """The Windows shape of ``os`` on this host: no ``O_NONBLOCK``, and an
    ``os.set_blocking`` that takes only a pipe (Python 3.12+ raises
    "Incorrect function" for a regular file)."""
    import stat
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)

    def set_blocking(fd, blocking):
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            raise OSError(1, "Incorrect function")

    monkeypatch.setattr(os, "set_blocking", set_blocking)


def test_windows_hashes_every_file(tmp_path, monkeypatch):
    """On Windows every hash raised once the named-pipe guard called
    ``os.set_blocking`` on a regular file: the snapshot was empty, so no file
    a session wrote there was ever sent back. The snapshot, the manifest and
    the turn-end scan must all see the files there."""
    import hashlib

    from satellite.transport.file_sync import _hash_file
    _windows_os(monkeypatch)
    agent = tmp_path
    (agent / "workspace").mkdir()
    (agent / "workspace" / "a.txt").write_text("v1")
    expected = "sha256:" + hashlib.sha256(b"v1").hexdigest()

    assert _hash_file(agent / "workspace" / "a.txt") == expected
    snap = snapshot_agent_dir(agent)
    assert snap == {"workspace/a.txt": expected}
    assert [e["path"] for e in compute_manifest(agent)] == ["workspace/a.txt"]

    (agent / "workspace" / "written-by-bash.txt").write_text("new")
    changes = detect_changes(agent, snap)
    assert [(c["path"], c["action"]) for c in changes] == [
        ("workspace/written-by-bash.txt", "write"),
    ]


def _lock(monkeypatch, *names, vanished=()):
    """``_hash_file`` refusing the named files as a Windows lock does, and
    raising FileNotFoundError for the vanished ones."""
    from satellite.transport import file_sync
    real = _REAL_HASH_FILE

    def hash_file(path):
        if path.name in names:
            raise PermissionError(13, "The process cannot access the file")
        if path.name in vanished:
            raise FileNotFoundError(2, "No such file")
        return real(path)

    monkeypatch.setattr(file_sync, "_hash_file", hash_file)


def test_an_unreadable_file_is_summed_up_once_per_change(tmp_path, monkeypatch, caplog):
    """A skipped file never reaches the platform, so a walk sums its skips
    up, logged when the summary changes (a file locked for hours logs once,
    not every turn), at ERROR when nothing could be read, and once more when
    every file reads again. A file that vanished under the walk is expected
    and stays silent."""
    import logging

    from satellite.transport import file_sync
    monkeypatch.setattr(file_sync, "_last_skip_summary", file_sync.OrderedDict())
    agent = tmp_path
    (agent / "workspace").mkdir()
    (agent / "workspace" / "locked.docx").write_text("x")
    (agent / "workspace" / "gone.txt").write_text("y")
    (agent / "workspace" / "fine.txt").write_text("z")
    _lock(monkeypatch, "locked.docx", vanished=("gone.txt",))

    def logged():
        return [(r.levelno, r.getMessage()) for r in caplog.records
                if r.getMessage().startswith("sync snapshot")]

    with caplog.at_level(logging.INFO, logger="satellite"):
        assert set(snapshot_agent_dir(agent)) == {"workspace/fine.txt"}
        assert set(snapshot_agent_dir(agent)) == {"workspace/fine.txt"}
        assert [(lvl, "skipped 1 of 2" in m and "locked.docx" in m) for lvl, m in logged()] == [
            (logging.WARNING, True)]
        caplog.clear()
        _lock(monkeypatch, "locked.docx", "fine.txt", vanished=("gone.txt",))
        assert snapshot_agent_dir(agent) == {}
        assert [lvl for lvl, _ in logged()] == [logging.ERROR]
        caplog.clear()
        _lock(monkeypatch)
        assert len(snapshot_agent_dir(agent)) == 3
        snapshot_agent_dir(agent)
        assert [(lvl, "reads every file" in m) for lvl, m in logged()] == [(logging.INFO, True)]


def test_the_manifest_lists_what_it_could_not_read_when_asked(tmp_path, monkeypatch):
    """A file the manifest leaves out reads as deleted to the merge: a
    proxy that asks gets it as a skipped entry with no hash instead (locked,
    or over the size cap); a vanished file is gone and stays out. Without
    the flag the reply is what an older proxy expects."""
    from satellite.transport import file_sync
    monkeypatch.setattr(file_sync, "MAX_FILE_SIZE", 10)
    agent = tmp_path
    ws = agent / "workspace"
    ws.mkdir()
    (ws / "locked.pst").write_text("x")
    (ws / "gone.txt").write_text("y")
    (ws / "big.bin").write_text("z" * 50)
    (ws / "fine.txt").write_text("ok")
    _lock(monkeypatch, "locked.pst", vanished=("gone.txt",))

    assert [e["path"] for e in compute_manifest(agent)] == ["workspace/fine.txt"]
    by_path = {e["path"]: e for e in compute_manifest(agent, report_skipped=True)}
    assert set(by_path) == {"workspace/fine.txt", "workspace/locked.pst", "workspace/big.bin"}
    assert "hash" in by_path["workspace/fine.txt"]
    locked, big = by_path["workspace/locked.pst"], by_path["workspace/big.bin"]
    assert locked["skipped"] == "unreadable" and "hash" not in locked and locked["size"] == 1
    assert big["skipped"] == "too_large" and "hash" not in big and big["size"] == 50


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="a folder mode keeps nothing from root")
def test_a_folder_the_walk_cannot_list_is_unknown_not_deleted(tmp_path):
    """A folder that can no longer be listed (a root-owned 700 folder a
    container left) hides its files from the walk. The manifest lists it
    as a skipped folder when asked, and the turn-end scan keeps its files
    in the baseline instead of reporting them deleted."""
    agent = tmp_path
    locked = agent / "workspace" / "build"
    locked.mkdir(parents=True)
    (locked / "out.txt").write_text("v1")
    (agent / "workspace" / "a.txt").write_text("a")
    snap = snapshot_agent_dir(agent)
    assert "workspace/build/out.txt" in snap

    locked.chmod(0)
    try:
        entries = compute_manifest(agent, report_skipped=True)
        assert {"path": "workspace/build", "skipped": "unreadable_dir", "dir": True} in entries
        assert "workspace/build/out.txt" not in {e["path"] for e in entries}
        assert detect_changes(agent, snap) == []
        assert "workspace/build/out.txt" in snap
    finally:
        locked.chmod(0o755)
    assert detect_changes(agent, snap) == []


def test_a_junction_is_not_followed_and_is_reported(tmp_path, monkeypatch):
    """Windows' ``os.walk`` follows a junction, which can lead out of the
    folder or loop back into it: the walk leaves such a folder out, and a
    manifest that reports skips names it as a link so its files keep their
    platform copies."""
    from satellite import config
    from satellite.host import safe_fs
    agent = tmp_path
    (agent / "workspace" / "jn").mkdir(parents=True)
    (agent / "workspace" / "jn" / "far.txt").write_text("outside")
    (agent / "workspace" / "a.txt").write_text("a")
    monkeypatch.setattr(config, "HOST", config.of("windows"))
    monkeypatch.setattr(safe_fs, "is_link_like", lambda p: p.name == "jn")

    assert set(snapshot_agent_dir(agent)) == {"workspace/a.txt"}
    entries = compute_manifest(agent, report_skipped=True)
    assert {"path": "workspace/jn", "skipped": "link", "dir": True} in entries
    assert [e["path"] for e in entries if "hash" in e] == ["workspace/a.txt"]


def test_a_file_hashed_but_not_read_is_reported_next_turn(tmp_path, monkeypatch):
    """A write the scan hashed but could not read (locked in between) keeps
    the baseline the platform knows, so the next turn reports it instead of
    losing it until a merge."""
    from pathlib import Path
    agent = tmp_path
    ws = agent / "workspace"
    ws.mkdir()
    (ws / "doc.txt").write_text("v1")
    snap = snapshot_agent_dir(agent)
    (ws / "doc.txt").write_text("v2")
    (ws / "new.txt").write_text("n")
    real = Path.read_bytes

    def read_bytes(self):
        if self.name in ("doc.txt", "new.txt"):
            raise PermissionError(13, "locked")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert detect_changes(agent, snap) == []
    monkeypatch.setattr(Path, "read_bytes", real)
    assert sorted(c["path"] for c in detect_changes(agent, snap)) == [
        "workspace/doc.txt", "workspace/new.txt"]


def test_cli_runtime_cruft_excluded(tmp_path):
    """Satellite mirrors the proxy: session transcripts + temp/caches/snapshots/
    backups + hidden dirs under .claude/.codex are pruned from the snapshot AND
    the manifest, so the two manifests stay symmetric (no diff churn)."""
    base = tmp_path / "users" / "alice"
    cl, cx = base / ".claude", base / ".codex"
    cl.mkdir(parents=True); cx.mkdir(parents=True)
    (cl / "settings.json").write_text("{}")
    (cl / ".credentials.json").write_text("{}")
    (cx / "models_cache.json").write_text("{}")
    for cruft in ("projects", "tasks", "backups", "shell-snapshots"):
        (cl / cruft).mkdir(); (cl / cruft / "f").write_text("c")
    for cruft in ("sessions", ".tmp", "cache"):
        (cx / cruft).mkdir(); (cx / cruft / "f").write_text("c")
    (cl / ".claude.json.backup.1").write_text("b")
    # A hook-file write's temp, beside the file or a wrapper (a crash left it).
    (cl / ".settings.json.oto-tmp-0a1b").write_text("t")
    (cl / "bin").mkdir()
    (cl / "bin" / ".gh.oto-tmp-0a1b").write_text("t")
    (cx / "skills" / ".system").mkdir(parents=True)
    (cx / "skills" / ".system" / "sk.md").write_text("s")

    for paths in (set(snapshot_agent_dir(tmp_path)),
                  {e["path"] for e in compute_manifest(tmp_path)}):
        # .claude/settings.json is host-local (regenerated per session, carries
        # sandbox-internal hook paths) — never synced in either direction, so it
        # must NOT appear in the manifest/snapshot (see _CLAUDE_HOST_LOCAL_FILES).
        assert "users/alice/.claude/settings.json" not in paths
        # OAuth credential file — written per host (start payload + rotation
        # fan-out push); syncing it would race the dedicated push channel.
        assert "users/alice/.claude/.credentials.json" not in paths
        assert "users/alice/.claude/.settings.json.oto-tmp-0a1b" not in paths
        assert "users/alice/.claude/bin/.gh.oto-tmp-0a1b" not in paths
        # .codex/models_cache.json is real config and DOES sync.
        assert "users/alice/.codex/models_cache.json" in paths
        for leaked in ("users/alice/.claude/projects/f", "users/alice/.claude/tasks/f",
                       "users/alice/.claude/backups/f",
                       "users/alice/.claude/shell-snapshots/f", "users/alice/.codex/sessions/f",
                       "users/alice/.codex/.tmp/f", "users/alice/.codex/cache/f",
                       "users/alice/.claude/.claude.json.backup.1",
                       "users/alice/.codex/skills/.system/sk.md"):
            assert leaked not in paths, f"leaked: {leaked}"


def test_the_per_session_mcp_config_copies_never_sync(tmp_path):
    """The session MCP config copies carry that session's own tokens and are
    rewritten at every start: this satellite's ``mcp-config-<sid12>.json``,
    the proxy's ``<agent>-<sha256(sub)[:12]>-<sid12>.json`` and the shared
    names earlier releases wrote are host-local, so the manifest never lists
    them (the proxy would push its copies here and scrub ours)."""
    cl = tmp_path / "users" / "alice" / ".claude"
    cl.mkdir(parents=True)
    copies = ("mcp-config.json", "mcp-config-1a2b3c4d-5e6.json",
              "personal-assistant-0123456789ab-1a2b3c4d-5e6.json",
              "personal-assistant-0123456789ab.json")
    for name in copies:
        (cl / name).write_text("{}")
    (cl / "notes.json").write_text("{}")
    for paths in (set(snapshot_agent_dir(tmp_path)),
                  {e["path"] for e in compute_manifest(tmp_path)}):
        for name in copies:
            assert f"users/alice/.claude/{name}" not in paths, name
        assert "users/alice/.claude/notes.json" in paths


def test_large_changed_file_reports_hash_size_without_reading(tmp_path, monkeypatch):
    """A changed file over CHUNK_THRESHOLD is reported stat-first — hash+size,
    no inline content — so the satellite never holds a big file in memory on
    the per-turn path (Feature D, 1.4.0)."""
    from satellite.transport import file_sync as fs

    agent = tmp_path / "agent"
    (agent / "workspace").mkdir(parents=True)
    snapshot = snapshot_agent_dir(agent)

    monkeypatch.setattr(fs, "CHUNK_THRESHOLD", 8)
    big = agent / "workspace" / "video.mp4"
    big.write_bytes(b"m" * 20)
    changes = fs.detect_changes(agent, snapshot)
    assert len(changes) == 1
    ch = changes[0]
    assert ch["action"] == "write"
    assert ch["size"] == 20
    assert "content_b64" not in ch
    assert ch["hash"].startswith("sha256:")


def test_snapshot_hash_cache_skips_rehash(tmp_path, monkeypatch):
    """Unchanged files are never re-hashed across snapshot passes (LRU keyed
    on size+mtime_ns); a content change re-hashes exactly that file."""
    from satellite.transport import file_sync as fs

    agent = tmp_path / "agent"
    (agent / "workspace").mkdir(parents=True)
    (agent / "workspace" / "a.txt").write_bytes(b"aaa")
    (agent / "workspace" / "b.txt").write_bytes(b"bbb")
    # Backdate past the racily-clean window so cache hits are trusted
    # (fresh files are deliberately always re-hashed).
    import os as _os
    import time as _time
    old = _time.time() - 60
    for f in (agent / "workspace").iterdir():
        _os.utime(f, (old, old))

    calls = {"n": 0}
    real = fs._hash_file

    def _counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(fs, "_hash_file", _counting)
    fs._HASH_CACHE.clear()

    snapshot_agent_dir(agent)
    assert calls["n"] == 2  # cold
    snapshot_agent_dir(agent)
    assert calls["n"] == 2  # warm — zero re-hashes

    (agent / "workspace" / "a.txt").write_bytes(b"changed!")
    snapshot_agent_dir(agent)
    assert calls["n"] == 3  # exactly the changed file


def test_hash_cache_keeps_the_raw_digest_and_primes_tolerantly(tmp_path):
    from satellite.transport import file_sync as fs
    f = tmp_path / "f.bin"
    f.write_bytes(b"content")
    fs._HASH_CACHE.clear()
    fs.prime_hash_cache(f, "sha256:" + "ab" * 32)
    entry = fs._HASH_CACHE[str(f)]
    assert isinstance(entry[2], bytes) and len(entry[2]) == 32
    fs.prime_hash_cache(f, "not-a-hash")  # never raises, never poisons
    assert fs._HASH_CACHE[str(f)][2] == bytes.fromhex("ab" * 32)
    fs._HASH_CACHE.clear()
    import os as _os
    import time as _time
    old = _time.time() - 60
    _os.utime(f, (old, old))
    st = _os.stat(f)
    assert fs._hash_file_cached(f, st) == fs._hash_file(f)
    assert fs._hash_file_cached(f, st) == fs._hash_file(f)  # the hit answers the text form
    assert fs._HASH_CACHE_MAX >= 200_000


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="a folder mode keeps nothing from root")
def test_a_folder_listed_but_not_entered_never_aborts_the_walk(tmp_path):
    """Read but no traverse permission: the walk lists the names, and every
    look at a file there fails. The walk goes on; the files are reported
    as unreadable, never as deleted."""
    agent = tmp_path
    half = agent / "workspace" / "half"
    half.mkdir(parents=True)
    (half / "in.txt").write_text("x")
    (agent / "workspace" / "a.txt").write_text("a")
    snap = snapshot_agent_dir(agent)
    half.chmod(0o444)
    try:
        assert set(snapshot_agent_dir(agent)) == {"workspace/a.txt"}
        entries = {e["path"]: e for e in compute_manifest(agent, report_skipped=True)}
        assert entries["workspace/half/in.txt"]["skipped"] == "unreadable"
        assert detect_changes(agent, snap) == []
    finally:
        half.chmod(0o755)


def test_a_posix_symlinked_folder_is_not_reported_as_a_link(tmp_path):
    """On POSIX ``os.walk`` never enters a symlinked folder and the walk does
    not stat children for links: nothing changes there."""
    agent = tmp_path
    (agent / "elsewhere").mkdir()
    (agent / "elsewhere" / "far.txt").write_text("x")
    (agent / "workspace").mkdir()
    (agent / "workspace" / "a.txt").write_text("a")
    (agent / "workspace" / "ln").symlink_to(agent / "elsewhere")
    entries = compute_manifest(agent, report_skipped=True)
    assert not [e for e in entries if e.get("skipped")]


def test_a_junction_is_a_name_surrogate_and_a_placeholder_is_not():
    from types import SimpleNamespace

    from satellite.host.safe_fs import _is_surrogate
    regular_dir = 0o040755
    assert _is_surrogate(SimpleNamespace(st_mode=regular_dir, st_reparse_tag=0xA0000003))
    assert _is_surrogate(SimpleNamespace(st_mode=regular_dir, st_reparse_tag=0xA000000C))
    assert not _is_surrogate(SimpleNamespace(st_mode=regular_dir, st_reparse_tag=0x9000001A))
    assert not _is_surrogate(SimpleNamespace(st_mode=regular_dir, st_reparse_tag=0))

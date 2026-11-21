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

import base64
import hashlib
import os

import pytest

from satellite.transport import file_sync as _file_sync
from satellite.transport.file_sync import (
    compute_manifest,
    detect_changes,
    snapshot_agent_dir,
)

_REAL_OPEN_LISTED = _file_sync._open_listed


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
    import dataclasses
    import stat

    from satellite import config as sat_config
    monkeypatch.setattr(sat_config, "HOST", dataclasses.replace(sat_config.HOST, posix=False))
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
    """The open of a listed file refusing the named files as a Windows lock
    does, and raising FileNotFoundError for the vanished ones."""
    from satellite.transport import file_sync
    real = _REAL_OPEN_LISTED

    def open_listed(root, rel, path, cap):
        if path.name in names:
            raise PermissionError(13, "The process cannot access the file")
        if path.name in vanished:
            raise FileNotFoundError(2, "No such file")
        return real(root, rel, path, cap)

    monkeypatch.setattr(file_sync, "_open_listed", open_listed)


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
    from satellite.transport import file_sync
    agent = tmp_path
    ws = agent / "workspace"
    ws.mkdir()
    (ws / "doc.txt").write_text("v1")
    snap = snapshot_agent_dir(agent)
    (ws / "doc.txt").write_text("v2")
    (ws / "new.txt").write_text("n")
    real = file_sync._read_inline

    def read_inline(root, rel, path):
        if path.name in ("doc.txt", "new.txt"):
            raise PermissionError(13, "locked")
        return real(root, rel, path)

    monkeypatch.setattr(file_sync, "_read_inline", read_inline)
    assert detect_changes(agent, snap) == []
    monkeypatch.setattr(file_sync, "_read_inline", real)
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
        # .codex/models_cache.json is Codex's own catalog cache: a direct
        # file under the engine home, never synced (only skills/ is).
        assert "users/alice/.codex/models_cache.json" not in paths
        for leaked in ("users/alice/.claude/projects/f", "users/alice/.claude/tasks/f",
                       "users/alice/.claude/backups/f",
                       "users/alice/.claude/shell-snapshots/f", "users/alice/.codex/sessions/f",
                       "users/alice/.codex/.tmp/f", "users/alice/.codex/cache/f",
                       "users/alice/.claude/.claude.json.backup.1",
                       "users/alice/.codex/skills/.system/sk.md"):
            assert leaked not in paths, f"leaked: {leaked}"


def test_only_the_persons_folders_sync_under_an_engine_home(tmp_path):
    """Under ``workspace/.claude`` and ``users/<u>/.codex`` the CLIs keep
    their own files (``history.jsonl``, ``state/``, the ``bin`` wrappers, the
    model catalog cache): never listed by any surface, so the platform never
    scrubs them. What a person authors syncs (Claude's skills, commands,
    agents and output styles; Codex's prompts and skills); a ``.claude``
    nested deeper (a repository's project config) keeps the general rules."""
    for rel in ("users/alice/.claude/history.jsonl", "users/alice/.claude/bin/gh",
                "users/alice/.claude/skills/x/SKILL.md", "users/alice/.claude/commands/d.md",
                "users/alice/.claude/agents/r.md", "users/alice/.claude/output-styles/t.md",
                "users/alice/.claude/prompts/p.md", "workspace/.codex/installation_id",
                "workspace/.codex/state/s.json", "workspace/.codex/thread-writer-locks/t.lock",
                "workspace/.codex/models_cache.json", "workspace/.codex/skills/y/SKILL.md",
                "workspace/.codex/prompts/q.md", "workspace/.codex/commands/c.md",
                "workspace/repo/.claude/commands/deploy.md", "users/alice/notes.md"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    expected = {"users/alice/.claude/skills/x/SKILL.md", "users/alice/.claude/commands/d.md",
                "users/alice/.claude/agents/r.md", "users/alice/.claude/output-styles/t.md",
                "workspace/.codex/skills/y/SKILL.md", "workspace/.codex/prompts/q.md",
                "workspace/repo/.claude/commands/deploy.md", "users/alice/notes.md"}
    assert set(snapshot_agent_dir(tmp_path)) == expected
    assert {e["path"] for e in compute_manifest(tmp_path)} == expected
    for rel in ("users/alice/.claude/history.jsonl", "users/alice/.claude/bin/gh",
                "users/alice/.claude/bin", "users/alice/.claude/prompts/p.md",
                "workspace/.codex/state/s.json", "workspace/.codex/commands/c.md",
                "workspace/.codex/models_cache.json"):
        assert _file_sync.is_unsynced_engine_home_path(rel), rel
    for rel in ("users/alice/.claude/skills", "users/alice/.claude/skills/x/SKILL.md",
                "users/alice/.claude/commands/d.md", "workspace/.codex/prompts/q.md",
                "workspace/repo/.claude/commands/deploy.md", "users/alice/notes.md",
                "workspace/.claude", "users/alice/.codex"):
        assert not _file_sync.is_unsynced_engine_home_path(rel), rel


@pytest.mark.skipif(os.name == "nt", reason="links stand in for junctions on POSIX")
def test_a_sessions_home_never_syncs_nor_what_its_links_reach(tmp_path):
    """A session's home (``sessions/session_homes.py``) holds its sign-in:
    never listed, and the walk never enters ``own``, so a link in a home
    (a junction is walked as a folder on Windows) never syncs a file twice."""
    sid = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    from satellite.sessions import session_homes
    store = tmp_path / "users" / "alice" / ".claude"
    (store / "skills" / "x").mkdir(parents=True)
    (store / "skills" / "x" / "SKILL.md").write_text("x")
    home = session_homes.open_home(store, sid, {"links": ["skills"], "seed": {}})
    (home / ".credentials.json").write_text("{}")
    assert set(snapshot_agent_dir(tmp_path)) == {"users/alice/.claude/skills/x/SKILL.md"}
    for rel in (f"users/alice/.claude/own/{sid}/.credentials.json",
                f"users/alice/.claude/own/{sid}/skills/x/SKILL.md", "workspace/.claude/own"):
        assert _file_sync.is_unsynced_engine_home_path(rel), rel


def test_the_per_session_mcp_config_copies_never_sync(tmp_path):
    """The session MCP config copies carry that session's own tokens and are
    rewritten at every start: this satellite's ``mcp-config-<sid12>.json``,
    the proxy's ``<agent>-<sha256(sub)[:12]>-<sid12>.json`` and the shared
    names earlier releases wrote are host-local, so the manifest never lists
    them (the proxy would push its copies here and scrub ours). Under an
    engine home no direct file syncs at all, so the rule is pinned on a
    nested project config, where the general rules apply."""
    cl = tmp_path / "workspace" / "repo" / ".claude"
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
            assert f"workspace/repo/.claude/{name}" not in paths, name
        assert "workspace/repo/.claude/notes.json" in paths


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
    real = fs._hash_fd

    def _counting(fd):
        calls["n"] += 1
        return real(fd)

    monkeypatch.setattr(fs, "_hash_fd", _counting)
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
    assert isinstance(entry[3], bytes) and len(entry[3]) == 32
    fs.prime_hash_cache(f, "not-a-hash")  # never raises, never poisons
    assert fs._HASH_CACHE[str(f)][3] == bytes.fromhex("ab" * 32)
    fs._HASH_CACHE.clear()
    import os as _os
    import time as _time
    old = _time.time() - 60
    _os.utime(f, (old, old))
    st = _os.lstat(f)
    assert fs._hash_listed(str(tmp_path), f.name, f, st, 100) == fs._hash_file(f)
    assert fs._hash_listed(str(tmp_path), f.name, f, st, 100) == fs._hash_file(f)  # the hit answers the text form
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


def test_a_folder_that_became_a_link_after_its_listing_sends_nothing_from_it(tmp_path, monkeypatch):
    """The scan lists by name; a folder swapped for a link since its listing
    is refused where it stands: no change and no hash carries what lies
    behind the link, and the manifest names the folder as a link."""
    from satellite.transport import file_sync
    agent = tmp_path / "agent"
    (agent / "workspace").mkdir(parents=True)
    (agent / "workspace" / "ok.txt").write_text("ok")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("stand-in")
    (agent / "workspace" / "sub").symlink_to(outside)
    real_walk = os.walk

    def _listed_before_the_swap(top, *a, **kw):
        yield from real_walk(top, *a, **kw)
        yield str(agent / "workspace" / "sub"), [], ["secret.txt"]
    monkeypatch.setattr(file_sync.os, "walk", _listed_before_the_swap)
    changes = detect_changes(agent, {})
    assert [c["path"] for c in changes] == ["workspace/ok.txt"]
    assert not any(base64.b64decode(c.get("content_b64", "")) == b"stand-in" for c in changes)
    entries = compute_manifest(agent, report_skipped=True)
    assert {"path": "workspace/sub", "skipped": "link", "dir": True} in entries
    assert [e["path"] for e in entries if "hash" in e] == ["workspace/ok.txt"]


def test_an_inline_write_carries_the_hash_of_the_bytes_it_sends(tmp_path, monkeypatch):
    """A file rewritten between its hash and its read is sent with the hash
    of what was read, never the hash of the bytes before."""
    import hashlib

    from satellite.transport import file_sync
    agent = tmp_path / "agent"
    (agent / "workspace").mkdir(parents=True)
    (agent / "workspace" / "doc.txt").write_text("read")
    monkeypatch.setattr(file_sync, "snapshot_agent_dir",
                        lambda d: {"workspace/doc.txt": "sha256:" + "0" * 64})
    snap: dict = {}
    [change] = detect_changes(agent, snap)
    assert base64.b64decode(change["content_b64"]) == b"read"
    assert change["hash"] == "sha256:" + hashlib.sha256(b"read").hexdigest()
    assert snap == {"workspace/doc.txt": change["hash"]}


def test_an_agent_folder_moved_behind_a_link_keeps_its_turn_end_scan(tmp_path):
    """A person's agent folder moved to another disk behind a link keeps
    reporting its changes; a link below it is still never followed."""
    real = tmp_path / "disk2" / "agent"
    (real / "workspace").mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("stand-in")
    (real / "workspace" / "linked.txt").symlink_to(tmp_path / "secret.txt")
    (tmp_path / "agents").mkdir()
    agent = tmp_path / "agents" / "agent"
    agent.symlink_to(real)
    snap = snapshot_agent_dir(agent)
    (real / "workspace" / "new.txt").write_text("new")
    assert [(c["path"], base64.b64decode(c["content_b64"])) for c in detect_changes(agent, snap)] == [
        ("workspace/new.txt", b"new")]


def test_a_listed_file_is_opened_with_no_link_on_its_way_whatever_its_folder_check(tmp_path, monkeypatch):
    """A folder checked before it was swapped for a link: the open of each
    file below it refuses the link on the way, in the snapshot and in the
    turn-end read alike."""
    from satellite.transport import file_sync
    agent = tmp_path / "agent"
    (agent / "workspace").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("stand-in")
    (agent / "workspace" / "sub").symlink_to(outside)
    real_walk = os.walk

    def _listed_before_the_swap(top, *a, **kw):
        yield from real_walk(top, *a, **kw)
        yield str(agent / "workspace" / "sub"), [], ["secret.txt"]
    monkeypatch.setattr(file_sync.os, "walk", _listed_before_the_swap)
    monkeypatch.setattr(file_sync, "_folder_refused", lambda root, rel: None)
    assert snapshot_agent_dir(agent) == {}
    monkeypatch.setattr(file_sync, "snapshot_agent_dir",
                        lambda d: {"workspace/sub/secret.txt": "sha256:" + "0" * 64})
    assert detect_changes(agent, {}) == []


def _backdated(path, data: bytes):
    """``data`` at ``path``, its times a minute back (past the racy window)."""
    import time as _time
    path.write_bytes(data)
    old = _time.time() - 60
    os.utime(path, (old, old))
    return os.lstat(path)


def test_a_file_swapped_at_its_name_with_its_size_and_time_kept_is_hashed_again(tmp_path, monkeypatch):
    """Another file renamed onto the name with the same size and the first
    one's time is a different file: the snapshot reads it again."""
    import hashlib
    from collections import OrderedDict

    from satellite.transport import file_sync
    monkeypatch.setattr(file_sync, "_HASH_CACHE", OrderedDict())
    agent = tmp_path / "agent"
    (agent / "workspace").mkdir(parents=True)
    st = _backdated(agent / "workspace" / "doc.txt", b"aaaa")
    assert snapshot_agent_dir(agent) == {"workspace/doc.txt": "sha256:" + hashlib.sha256(b"aaaa").hexdigest()}
    other = tmp_path / "other.txt"
    other.write_bytes(b"bbbb")
    os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(other, agent / "workspace" / "doc.txt")
    assert snapshot_agent_dir(agent) == {"workspace/doc.txt": "sha256:" + hashlib.sha256(b"bbbb").hexdigest()}


def test_a_hash_read_while_the_file_changed_is_never_remembered(tmp_path, monkeypatch):
    """A file that changes while it is hashed leaves nothing in the cache,
    so the next snapshot reads it again."""
    from collections import OrderedDict

    from satellite.transport import file_sync
    monkeypatch.setattr(file_sync, "_HASH_CACHE", OrderedDict())
    agent = tmp_path / "agent"
    (agent / "workspace").mkdir(parents=True)
    doc = agent / "workspace" / "doc.txt"
    _backdated(doc, b"aaaa")
    real = file_sync._hash_fd
    hashed: list = []

    def _grows_while_hashed(fd):
        hashed.append(fd)
        digest = real(fd)
        if len(hashed) == 1:
            with open(doc, "ab") as fh:
                fh.write(b"b")
        return digest
    monkeypatch.setattr(file_sync, "_hash_fd", _grows_while_hashed)
    snapshot_agent_dir(agent)
    assert str(doc) not in file_sync._HASH_CACHE
    snapshot_agent_dir(agent)
    assert len(hashed) == 2


def test_a_link_above_the_visited_folder_is_a_skipped_link_folder_never_a_gap(tmp_path, monkeypatch):
    """A folder above the one the walk visits, swapped for a link after its
    own check: the visited folder is a skipped ``link`` folder, so the merge
    reads its files as unknown, never as gone."""
    from satellite.transport import file_sync
    agent = tmp_path / "agent"
    (agent / "workspace").mkdir(parents=True)
    (agent / "workspace" / "ok.txt").write_text("ok")
    outside = tmp_path / "outside"
    (outside / "deeper").mkdir(parents=True)
    (outside / "deeper" / "x.txt").write_text("stand-in")
    (agent / "workspace" / "sub").symlink_to(outside)
    real_walk = os.walk

    def _listed_before_the_swap(top, *a, **kw):
        yield from real_walk(top, *a, **kw)
        yield str(agent / "workspace" / "sub" / "deeper"), [], ["x.txt"]
    monkeypatch.setattr(file_sync.os, "walk", _listed_before_the_swap)
    entries = compute_manifest(agent, report_skipped=True)
    assert {"path": "workspace/sub/deeper", "skipped": "link", "dir": True} in entries
    assert [e["path"] for e in entries if "hash" in e] == ["workspace/ok.txt"]


def _fstat_with_another_inode(monkeypatch):
    """``os.fstat`` answering an inode that is not ``os.lstat``'s for the same
    file, as a Windows filesystem and Python may (the file index and the file
    id come from two calls there)."""
    real = os.fstat

    def fstat(fd):
        st = real(fd)
        return os.stat_result(
            (st.st_mode, st.st_ino + 1_000_003, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
             st.st_size, int(st.st_atime), int(st.st_mtime), int(st.st_ctime)),
            {"st_atime": st.st_atime, "st_mtime": st.st_mtime, "st_ctime": st.st_ctime,
             "st_atime_ns": st.st_atime_ns, "st_mtime_ns": st.st_mtime_ns, "st_ctime_ns": st.st_ctime_ns})
    monkeypatch.setattr(os, "fstat", fstat)


def test_an_unchanged_tree_opens_no_file_where_the_two_inodes_differ(tmp_path, monkeypatch):
    """The cache compares the listing's status with a listing's status, never
    with the open file's: on a Windows-shaped host whose open file reports
    another inode, a second scan of an unchanged tree still opens nothing."""
    from collections import OrderedDict

    from satellite.transport import file_sync
    _windows_os(monkeypatch)
    _fstat_with_another_inode(monkeypatch)
    monkeypatch.setattr(file_sync, "_HASH_CACHE", OrderedDict())
    agent = tmp_path / "agent"
    (agent / "workspace").mkdir(parents=True)
    for name in ("a.txt", "b.txt", "c.txt"):
        _backdated(agent / "workspace" / name, name.encode())
    first = snapshot_agent_dir(agent)
    opened: list = []
    real = file_sync._open_listed

    def _counted(root, rel, path, cap):
        opened.append(rel)
        return real(root, rel, path, cap)
    monkeypatch.setattr(file_sync, "_open_listed", _counted)
    assert snapshot_agent_dir(agent) == first and len(first) == 3
    assert opened == []


def test_a_file_changed_between_its_listing_and_its_read_is_never_remembered(tmp_path, monkeypatch):
    from collections import OrderedDict

    from satellite.transport import file_sync
    monkeypatch.setattr(file_sync, "_HASH_CACHE", OrderedDict())
    agent = tmp_path / "agent"
    (agent / "workspace").mkdir(parents=True)
    doc = agent / "workspace" / "doc.txt"
    _backdated(doc, b"aaaa")
    real = file_sync._open_listed

    def _grows_before_the_read(root, rel, path, cap):
        with open(doc, "ab") as fh:
            fh.write(b"b")
        return real(root, rel, path, cap)
    monkeypatch.setattr(file_sync, "_open_listed", _grows_before_the_read)
    snapshot_agent_dir(agent)
    assert str(doc) not in file_sync._HASH_CACHE


def test_a_push_refreshed_during_the_scan_keeps_its_hash(tmp_path, monkeypatch):
    """The scan runs in a thread while a push applied on the loop refreshes
    the same snapshot: the push's hash is kept, so the pushed file never
    echoes back to the platform as a change of this machine."""
    from satellite.transport import file_sync
    agent = tmp_path / "agent"
    (agent / "workspace").mkdir(parents=True)
    (agent / "workspace" / "a.txt").write_text("a")
    snap = snapshot_agent_dir(agent)
    real = file_sync.snapshot_agent_dir

    pushed = "sha256:" + hashlib.sha256(b"from the platform").hexdigest()

    def _push_lands_meanwhile(d):
        current = real(d)
        (agent / "workspace" / "pushed.txt").write_text("from the platform")
        snap["workspace/pushed.txt"] = pushed
        return current
    monkeypatch.setattr(file_sync, "snapshot_agent_dir", _push_lands_meanwhile)
    assert detect_changes(agent, snap) == []
    assert snap["workspace/pushed.txt"] == pushed
    monkeypatch.setattr(file_sync, "snapshot_agent_dir", real)
    assert detect_changes(agent, snap) == []


def _write_msg(rel: str, data: bytes) -> dict:
    return {"path": rel, "action": "write", "content_b64": base64.b64encode(data).decode(),
            "hash": "sha256:" + hashlib.sha256(data).hexdigest()}


def _agent_with_two_files(tmp_path):
    agent = tmp_path / "agents" / "a"
    (agent / "workspace").mkdir(parents=True)
    (agent / "workspace" / "kept.txt").write_text("kept")
    (agent / "workspace" / "doc.txt").write_text("first")
    return agent


def _push_lands(monkeypatch, agent, snap, msg, *, before_walk: bool):
    """The satellite's own ``apply_file_push`` with the session's snapshot,
    run whole between two steps of the scan, as the event loop runs it
    beside the scan's thread."""
    real = _file_sync.snapshot_agent_dir

    def _walk(d):
        if before_walk:
            _file_sync.apply_file_push(agent, msg, snapshots=[snap])
            return real(d)
        current = real(d)
        _file_sync.apply_file_push(agent, msg, snapshots=[snap])
        return current
    monkeypatch.setattr(_file_sync, "snapshot_agent_dir", _walk)
    return real


@pytest.mark.parametrize("rel", ["workspace/doc.txt", "workspace/new.txt"])
def test_a_write_pushed_before_the_scan_reads_the_file_is_never_reported_back(tmp_path, monkeypatch, rel):
    agent = _agent_with_two_files(tmp_path)
    snap = snapshot_agent_dir(agent)
    real = _push_lands(monkeypatch, agent, snap, _write_msg(rel, b"from the platform"), before_walk=True)
    assert detect_changes(agent, snap) == []
    monkeypatch.setattr(_file_sync, "snapshot_agent_dir", real)
    assert detect_changes(agent, snap) == []


@pytest.mark.parametrize("before_walk", [True, False], ids=["before-the-walk", "after-the-walk"])
def test_a_delete_pushed_during_the_scan_is_never_reported_back(tmp_path, monkeypatch, before_walk):
    agent = _agent_with_two_files(tmp_path)
    snap = snapshot_agent_dir(agent)
    real = _push_lands(monkeypatch, agent, snap, {"path": "workspace/doc.txt", "action": "delete"},
                       before_walk=before_walk)
    assert detect_changes(agent, snap) == []
    assert "workspace/doc.txt" not in snap
    monkeypatch.setattr(_file_sync, "snapshot_agent_dir", real)
    assert detect_changes(agent, snap) == []


def test_a_machine_change_beside_a_push_is_still_reported(tmp_path, monkeypatch):
    """The other half: what the session itself changed is reported, whatever
    a push did to another path meanwhile."""
    agent = _agent_with_two_files(tmp_path)
    snap = snapshot_agent_dir(agent)
    (agent / "workspace" / "kept.txt").write_text("changed on the machine")
    _push_lands(monkeypatch, agent, snap, _write_msg("workspace/doc.txt", b"from the platform"),
                before_walk=True)
    assert [(c["path"], c["action"]) for c in detect_changes(agent, snap)] == [("workspace/kept.txt", "write")]


def test_a_large_file_pushed_after_the_walk_is_never_reported_back(tmp_path, monkeypatch):
    """A file too large to send inline is reported by its hash alone: the
    walk's hash of the bytes the push replaced is never sent back."""
    monkeypatch.setattr(_file_sync, "CHUNK_THRESHOLD", 8)
    agent = _agent_with_two_files(tmp_path)
    (agent / "workspace" / "doc.txt").write_bytes(b"a" * 20)
    snap = snapshot_agent_dir(agent)
    real = _push_lands(monkeypatch, agent, snap, _write_msg("workspace/doc.txt", b"b" * 30),
                       before_walk=False)
    assert detect_changes(agent, snap) == []
    monkeypatch.setattr(_file_sync, "snapshot_agent_dir", real)
    assert detect_changes(agent, snap) == []


def _scan_inside(monkeypatch, name, agent, snap, *, after: bool) -> list:
    """Runs a whole scan inside the push's one disk step, before or after
    it, as the scan's thread runs while the loop applies the push."""
    from satellite.host import safe_fs
    real = getattr(safe_fs, name)
    reports: list = []

    def _step(*a, **kw):
        if not after:
            reports.append(detect_changes(agent, snap))
        out = real(*a, **kw)
        if after:
            reports.append(detect_changes(agent, snap))
        return out
    monkeypatch.setattr(safe_fs, name, _step)
    return reports


@pytest.mark.parametrize("after", [True, False], ids=["after-the-disk-step", "before-the-disk-step"])
@pytest.mark.parametrize("msg, step", [
    (_write_msg("workspace/doc.txt", b"from the platform"), "atomic_write_beneath"),
    (_write_msg("workspace/new.txt", b"new from the platform"), "atomic_write_beneath"),
    ({"path": "workspace/doc.txt", "action": "delete"}, "unlink_beneath"),
], ids=["write", "new-file", "delete"])
def test_a_scan_inside_a_push_never_reports_the_push(tmp_path, monkeypatch, msg, step, after):
    """The push's disk step and its baseline entry are never seen apart: a
    scan that runs between them leaves the path to the push."""
    agent = _agent_with_two_files(tmp_path)
    snap = snapshot_agent_dir(agent)
    reports = _scan_inside(monkeypatch, step, agent, snap, after=after)
    _file_sync.apply_file_push(agent, msg, snapshots=[snap])
    monkeypatch.undo()
    assert reports == [[]]
    assert detect_changes(agent, snap) == []
    assert (snap.get(msg["path"]) is None) == (msg["action"] == "delete")


@pytest.mark.parametrize("step, msg", [
    ("atomic_write_beneath", _write_msg("workspace/doc.txt", b"from the platform")),
    ("unlink_beneath", {"path": "workspace/doc.txt", "action": "delete"}),
], ids=["write", "delete"])
def test_a_push_that_fails_leaves_the_baseline_as_it_was(tmp_path, monkeypatch, step, msg):
    from satellite.host import safe_fs
    agent = _agent_with_two_files(tmp_path)
    snap = snapshot_agent_dir(agent)
    before = dict(snap)

    def _fails(*a, **kw):
        raise PermissionError(13, "refused")
    monkeypatch.setattr(safe_fs, step, _fails)
    with pytest.raises(PermissionError):
        _file_sync.apply_file_push(agent, msg, snapshots=[snap])
    monkeypatch.undo()
    assert snap == before
    (agent / "workspace" / "doc.txt").write_text("changed on the machine")
    assert [(c["path"], c["action"]) for c in detect_changes(agent, snap)] == [("workspace/doc.txt", "write")]


@pytest.mark.parametrize("hook", ["stat", "read"])
def test_two_scans_of_one_session_report_a_change_once(tmp_path, monkeypatch, hook):
    """A second scan of the session that runs inside the first (a turn's
    end beside a terminal's quiet moment) never reports the same change
    again: each move of the session's entry is one scan's."""
    agent = _agent_with_two_files(tmp_path)
    snap = snapshot_agent_dir(agent)
    if hook == "stat":
        (agent / "workspace" / "doc.txt").unlink()
        expected = ("workspace/doc.txt", "delete")
        target, name = _file_sync.os, "stat"
    else:
        (agent / "workspace" / "doc.txt").write_text("changed on the machine")
        expected = ("workspace/doc.txt", "write")
        target, name = _file_sync, "_read_inline"
    real = getattr(target, name)
    inner: list = []

    def _second_scan_meanwhile(*a, **kw):
        try:
            return real(*a, **kw)
        finally:
            if not inner and any(str(x).endswith("doc.txt") for x in a):
                inner.append(None)
                inner.extend(detect_changes(agent, snap))
    monkeypatch.setattr(target, name, _second_scan_meanwhile)
    outer = detect_changes(agent, snap)
    monkeypatch.undo()
    reported = [(c["path"], c["action"]) for c in outer + inner[1:]]
    assert reported == [expected]
    assert detect_changes(agent, snap) == []


def test_a_start_frame_rewrites_no_file_that_holds_its_bytes(tmp_path):
    """A rename over a file a running sandbox binds by name detaches the bind
    there: a file the frame carries unchanged is left as it is (and still
    counted the session's for its close)."""
    import base64

    from satellite.sessions import session_files
    agent = tmp_path / "agents" / "a"
    payload = {"host_files": {"users/alice/cfg/hooks.json": {
        "content_b64": base64.b64encode(b"{}").decode(), "mode": 0o600}}}
    session_files.materialize_agent_files(payload, "s-1", agent)
    path = agent / "users" / "alice" / "cfg" / "hooks.json"
    first = path.stat().st_ino
    session_files.materialize_agent_files(payload, "s-2", agent)
    assert path.stat().st_ino == first
    session_files.wipe_agent_files("s-1")
    assert path.exists()
    session_files.wipe_agent_files("s-2")
    assert not path.exists()
    changed = {"host_files": {"users/alice/cfg/hooks.json": {
        "content_b64": base64.b64encode(b"{\"a\": 1}").decode(), "mode": 0o600}}}
    session_files.materialize_agent_files(payload, "s-3", agent)
    session_files.materialize_agent_files(changed, "s-4", agent)
    assert path.read_bytes() == b'{"a": 1}'
    session_files.wipe_agent_files("s-3")
    session_files.wipe_agent_files("s-4")


def test_a_start_and_a_close_on_two_threads_never_leave_a_written_file_unregistered(tmp_path, monkeypatch):
    """A prepared file's write and its registration are one step for the
    close's removal: a close that runs while another session's start writes
    the same file never removes it under that start."""
    import base64
    import threading

    from satellite.host import safe_fs
    from satellite.sessions import session_files
    agent = tmp_path / "agents" / "a"
    payload = {"host_files": {"users/alice/cfg/settings.json": {
        "content_b64": base64.b64encode(b"{}").decode(), "mode": 0o600}}}
    session_files.materialize_agent_files(payload, "s-1", agent)
    written = threading.Event()
    removing = threading.Event()
    real_write = safe_fs.atomic_write_beneath

    def _slow_write(*a, **kw):
        real_write(*a, **kw)
        written.set()
        removing.wait(1.0)
    monkeypatch.setattr(safe_fs, "atomic_write_beneath", _slow_write)
    start = threading.Thread(target=session_files.materialize_agent_files, args=(payload, "s-2", agent))
    start.start()
    written.wait(5)
    close = threading.Thread(target=session_files.wipe_agent_files, args=("s-1",))
    close.start()
    removing.set()
    start.join(5)
    close.join(5)
    assert (agent / "users" / "alice" / "cfg" / "settings.json").exists()
    session_files.wipe_agent_files("s-2")
    assert not (agent / "users" / "alice" / "cfg" / "settings.json").exists()


@pytest.mark.parametrize("after", [True, False], ids=["after-the-disk-step", "before-the-disk-step"])
def test_a_scan_inside_a_chunked_pushs_commit_never_reports_the_push(tmp_path, monkeypatch, after):
    """A file sent in pieces changes on disk at its commit alone, and that
    step is marked as a whole write's is."""
    agent = _agent_with_two_files(tmp_path)
    snap = snapshot_agent_dir(agent)
    rel, data = "workspace/doc.txt", b"from the platform, in pieces"
    full = "sha256:" + hashlib.sha256(data).hexdigest()
    _file_sync.apply_file_push(agent, {"path": rel, "action": "write_chunk", "chunk_index": 0,
                                       "content_b64": base64.b64encode(data[:10]).decode()}, snapshots=[snap])
    reports = _scan_inside(monkeypatch, "commit_partial", agent, snap, after=after)
    _file_sync.apply_file_push(agent, {"path": rel, "action": "write_chunk", "chunk_index": 1,
                                       "content_b64": base64.b64encode(data[10:]).decode(), "hash": full},
                               snapshots=[snap])
    monkeypatch.undo()
    assert reports == [[]]
    assert snap[rel] == full
    assert detect_changes(agent, snap) == []


@pytest.mark.parametrize("msg, step", [
    (_write_msg("workspace/doc.txt", b"from the platform"), "atomic_write_beneath"),
    ({"path": "workspace/doc.txt", "action": "delete"}, "unlink_beneath"),
], ids=["write", "delete"])
def test_a_push_marks_and_settles_every_session_of_the_agent(tmp_path, monkeypatch, msg, step):
    """Two sessions of one agent on this machine: the push marks both
    baselines and settles both, so neither session's scan reports it and
    neither keeps the mark."""
    agent = _agent_with_two_files(tmp_path)
    one, two = snapshot_agent_dir(agent), snapshot_agent_dir(agent)
    reports = _scan_inside(monkeypatch, step, agent, two, after=True)
    _file_sync.apply_file_push(agent, msg, snapshots=[one, two])
    monkeypatch.undo()
    assert reports == [[]]
    assert one == two and _file_sync._PUSHING not in two.values()
    assert detect_changes(agent, two) == [] and detect_changes(agent, one) == []


def test_a_scans_claim_waits_for_the_baselines_lock(tmp_path):
    """The check and the move of an entry are one step under the lock: a
    scan's claim does not run while another holder is inside it."""
    import threading
    agent = _agent_with_two_files(tmp_path)
    snap = snapshot_agent_dir(agent)
    (agent / "workspace" / "doc.txt").write_text("changed on the machine")
    before = dict(snap)
    out: list = []
    with _file_sync._baseline_lock:
        scan = threading.Thread(target=lambda: out.extend(detect_changes(agent, snap)))
        scan.start()
        scan.join(0.5)
        assert scan.is_alive() and snap == before
    scan.join(5)
    assert [(c["path"], c["action"]) for c in out] == [("workspace/doc.txt", "write")]


def test_a_pushs_mark_waits_for_the_baselines_lock(tmp_path):
    """A push takes the lock before it marks: while another holder is inside
    it, the push has changed neither the baseline nor the disk."""
    import threading
    agent = _agent_with_two_files(tmp_path)
    snap = snapshot_agent_dir(agent)
    before = dict(snap)
    msg = _write_msg("workspace/doc.txt", b"from the platform")
    with _file_sync._baseline_lock:
        push = threading.Thread(target=_file_sync.apply_file_push, args=(agent, msg),
                                kwargs={"snapshots": [snap]})
        push.start()
        push.join(0.5)
        assert push.is_alive() and snap == before
        assert (agent / "workspace" / "doc.txt").read_text() == "first"
    push.join(5)
    assert snap["workspace/doc.txt"] == msg["hash"]

def test_an_agent_folder_that_is_gone_is_never_read_as_its_files_deleted(tmp_path):
    """The agent's folder itself out of reach at a turn's end (a disk not
    mounted, a folder moved away) is no delete of its files: nothing goes
    out, and the baseline stands for the turn end after it is back."""
    agent = _agent_with_two_files(tmp_path)
    snap = snapshot_agent_dir(agent)
    before = dict(snap)
    away = tmp_path / "agents" / "a-not-mounted"
    agent.rename(away)
    assert detect_changes(agent, snap) == []
    assert snap == before
    away.rename(agent)
    assert detect_changes(agent, snap) == []
    (agent / "workspace" / "doc.txt").unlink()
    assert [(c["path"], c["action"]) for c in detect_changes(agent, snap)] == [("workspace/doc.txt", "delete")]

def test_a_file_changed_back_before_its_read_is_not_reported(tmp_path, monkeypatch):
    """The walk saw new bytes, and the file holds the baseline's bytes again
    when the scan reads it: no change goes out."""
    agent = _agent_with_two_files(tmp_path)
    snap = snapshot_agent_dir(agent)
    doc = agent / "workspace" / "doc.txt"
    doc.write_text("for a moment")
    real = _file_sync._read_inline

    def _back_before_the_read(root, rel, path):
        if rel == "workspace/doc.txt":
            doc.write_text("first")
        return real(root, rel, path)
    monkeypatch.setattr(_file_sync, "_read_inline", _back_before_the_read)
    assert detect_changes(agent, snap) == []
    monkeypatch.undo()
    assert detect_changes(agent, snap) == []


def test_a_delete_pushed_after_the_scan_read_a_change_of_that_file_is_never_sent_back(tmp_path, monkeypatch):
    """This machine changed a file and the platform deleted it while the
    scan read it: the person's delete stands, and no scan sends it back."""
    agent = _agent_with_two_files(tmp_path)
    snap = snapshot_agent_dir(agent)
    (agent / "workspace" / "doc.txt").write_text("changed on the machine")
    real = _file_sync._read_inline

    def _delete_lands_after_the_read(root, rel, path):
        out = real(root, rel, path)
        if rel == "workspace/doc.txt":
            _file_sync.apply_file_push(agent, {"path": rel, "action": "delete"}, snapshots=[snap])
        return out
    monkeypatch.setattr(_file_sync, "_read_inline", _delete_lands_after_the_read)
    first = detect_changes(agent, snap)
    monkeypatch.undo()
    assert ("workspace/doc.txt", "delete") not in [(c["path"], c["action"]) for c in first]
    assert not (agent / "workspace" / "doc.txt").exists()
    assert "workspace/doc.txt" not in snap
    assert detect_changes(agent, snap) == []

def test_a_push_that_lands_while_the_scan_looks_for_deletes_never_breaks_it(tmp_path, monkeypatch):
    """The scan iterates its own copy of the snapshot: an entry a push adds
    on the loop meanwhile never ends the scan with an error."""
    agent = _agent_with_two_files(tmp_path)
    snap = snapshot_agent_dir(agent)
    (agent / "workspace" / "doc.txt").unlink()
    (agent / "workspace" / "kept.txt").unlink()
    real_stat = _file_sync.os.stat
    landed: list = []

    def _stat(path, *a, **kw):
        landed.append(path)
        snap[f"workspace/pushed-{len(landed)}.txt"] = "sha256:the-push"
        return real_stat(path, *a, **kw)
    monkeypatch.setattr(_file_sync.os, "stat", _stat)
    changes = detect_changes(agent, snap)
    monkeypatch.undo()
    assert sorted(c["path"] for c in changes if c["action"] == "delete") == [
        "workspace/doc.txt", "workspace/kept.txt"]


def test_a_close_that_is_removing_holds_back_a_start_of_the_same_file(tmp_path, monkeypatch):
    """The close's choice and its removal are one step: a start of another
    session of the scope waits for the removal, then writes, so the file it
    wrote is never the one the close removes."""
    import threading

    from satellite.host import safe_fs
    from satellite.sessions import session_files
    monkeypatch.setattr(session_files, "_agent_files", {})
    agent = tmp_path / "agents" / "a"
    payload = {"host_files": {"users/alice/cfg/settings.json": {
        "content_b64": base64.b64encode(b"{}").decode(), "mode": 0o600}}}
    session_files.materialize_agent_files(payload, "s-1", agent)
    removing, go = threading.Event(), threading.Event()
    real_unlink = safe_fs.unlink_beneath

    def _slow_unlink(*a, **kw):
        removing.set()
        go.wait(5)
        return real_unlink(*a, **kw)
    monkeypatch.setattr(safe_fs, "unlink_beneath", _slow_unlink)
    close = threading.Thread(target=session_files.wipe_agent_files, args=("s-1",))
    close.start()
    assert removing.wait(5)
    start = threading.Thread(target=session_files.materialize_agent_files, args=(payload, "s-2", agent))
    start.start()
    start.join(0.5)
    go.set()
    close.join(5)
    start.join(5)
    monkeypatch.setattr(safe_fs, "unlink_beneath", real_unlink)
    assert (agent / "users" / "alice" / "cfg" / "settings.json").exists()
    session_files.wipe_agent_files("s-2")
    assert not (agent / "users" / "alice" / "cfg" / "settings.json").exists()

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


from satellite.transport.file_sync import (
    compute_manifest,
    detect_changes,
    snapshot_agent_dir,
)


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
        # .codex/models_cache.json is real config and DOES sync.
        assert "users/alice/.codex/models_cache.json" in paths
        for leaked in ("users/alice/.claude/projects/f", "users/alice/.claude/tasks/f",
                       "users/alice/.claude/backups/f",
                       "users/alice/.claude/shell-snapshots/f", "users/alice/.codex/sessions/f",
                       "users/alice/.codex/.tmp/f", "users/alice/.codex/cache/f",
                       "users/alice/.claude/.claude.json.backup.1",
                       "users/alice/.codex/skills/.system/sk.md"):
            assert leaked not in paths, f"leaked: {leaked}"


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

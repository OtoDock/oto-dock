"""Tests for file sync protocol (proxy side)."""

import base64
import hashlib
import os
import tempfile
from pathlib import Path

import pytest

from core.remote.file_sync import (
    FileEntry,
    MergePlan,
    FileAction,
    compute_manifest,
    diff_manifests,
    prepare_outgoing_files,
    apply_incoming_file,
    should_sync_to_target,
    can_write_back,
    _hash_file,
)


def _act(plan, path):
    """The FileAction for ``path`` in a MergePlan, or None."""
    for a in plan.actions:
        if a.rel_path == path:
            return a
    return None


def test_credentials_and_cli_config_never_write_back():
    # Platform-authoritative / secret paths are NEVER accepted from a satellite,
    # for ANY role: .claude/.codex (CLI session state + config) and .credentials
    # (OAuth tokens). The platform is the sole source of truth for all three.
    assert can_write_back("users/alice/.credentials/google-tokens/a.json", "manager", "alice") is False
    assert can_write_back("users/alice/.credentials/x.json", "admin", "alice") is False
    assert can_write_back("knowledge/.credentials/sa.json", "manager", "bob") is False
    assert can_write_back("users/alice/.claude/settings.json", "admin", "alice") is False
    assert can_write_back(".codex/sessions/s.jsonl", "admin", "alice") is False


def test_viewer_writes_back_only_own_personal_content():
    # The viewer sync-back model: a viewer pushes back ONLY their own
    # users/<self>/ content — never the shared workspace, knowledge, config, or
    # another user's files.
    assert can_write_back("users/alice/workspace/doc.md", "viewer", "alice") is True
    assert can_write_back("users/alice/context/note.md", "viewer", "alice") is True
    assert can_write_back("workspace/shared.md", "viewer", "alice") is False
    assert can_write_back("knowledge/ref.md", "viewer", "alice") is False
    assert can_write_back("config/prompt.md", "viewer", "alice") is False
    assert can_write_back("users/bob/workspace/x.md", "viewer", "alice") is False


@pytest.fixture
def agent_dir(tmp_path):
    """Create a sample agent directory structure."""
    # config/
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "prompt.md").write_text("# Test Agent")
    (tmp_path / "config" / "context").mkdir()
    (tmp_path / "config" / "context" / "readme.md").write_text("Context content")

    # workspace/
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "data.txt").write_text("workspace data")

    # users/alice/
    (tmp_path / "users" / "alice").mkdir(parents=True)
    (tmp_path / "users" / "alice" / "notes.txt").write_text("alice notes")

    return tmp_path


class TestComputeManifest:
    def test_full_manifest(self, agent_dir):
        # config/ is owner-tier only — pass a manager role so the
        # manifest includes it (default "" = non-owner/agent-scope, no config).
        entries = compute_manifest(agent_dir, target_role="manager")
        paths = {e.path for e in entries}
        assert "config/prompt.md" in paths
        assert "config/context/readme.md" in paths
        assert "workspace/data.txt" in paths
        assert "users/alice/notes.txt" in paths

    def test_config_only_scope(self, agent_dir):
        entries = compute_manifest(agent_dir, scope="config_only", target_role="manager")
        paths = {e.path for e in entries}
        assert "config/prompt.md" in paths
        assert "config/context/readme.md" in paths
        assert "workspace/data.txt" not in paths
        assert "users/alice/notes.txt" not in paths

    def test_exclude_user_dirs(self, agent_dir):
        """Shared-only agents: the users/ subtree never enters the manifest
        (their mode has no per-user scope — stray dirs stay platform-side)."""
        entries = compute_manifest(agent_dir, target_role="manager",
                                   exclude_user_dirs=True)
        paths = {e.path for e in entries}
        assert "workspace/data.txt" in paths
        assert not any(p.startswith("users/") for p in paths)

    def test_skips_pycache(self, agent_dir):
        (agent_dir / "__pycache__").mkdir()
        (agent_dir / "__pycache__" / "test.pyc").write_bytes(b"bytecode")
        entries = compute_manifest(agent_dir)
        paths = {e.path for e in entries}
        assert "__pycache__/test.pyc" not in paths

    def test_excludes_cli_runtime_cruft(self, agent_dir):
        """Session transcripts + temp/caches/snapshots/backups under .claude/.codex
        are pruned from the manifest; real config (and a hidden .system skills dir,
        like every hidden dir, is NOT synced) — only the platform config files."""
        base = agent_dir / "users" / "alice"
        cl, cx = base / ".claude", base / ".codex"
        for d in (cl, cx):
            d.mkdir(parents=True)
        # .codex/models_cache.json is real synced config — KEEP. .claude
        # settings.json + mcp-config.json are HOST-LOCAL now (never synced).
        (cl / "settings.json").write_text("{}")
        (cl / "mcp-config.json").write_text("{}")
        (cl / "auth.json").write_text("{}")
        # OAuth credential file — written per host (start payload + rotation
        # fan-out push); syncing it would race the dedicated push channel.
        (cl / ".credentials.json").write_text("{}")
        (cx / "models_cache.json").write_text("{}")
        # Runtime cruft dirs — EXCLUDE. ``ssh`` holds the per-session
        # $OTO_SSH_KEY_DIR private keys (broker-delivered on satellites) —
        # syncing it would persist keys on every satellite of the agent.
        # ``tasks`` is the Claude Code task store (keyed by the session id
        # ``--resume`` keeps) — syncing it wiped the task list on re-warm.
        for cruft in ("projects", "tasks", "backups", "shell-snapshots", "plans", "ssh"):
            (cl / cruft).mkdir()
            (cl / cruft / "f").write_text("cruft")
        for cruft in ("sessions", ".tmp", "tmp", "cache", "shell_snapshots"):
            (cx / cruft).mkdir()
            (cx / cruft / "f").write_text("cruft")
        # Backup state files — EXCLUDE.
        (cl / ".claude.json.backup.1775").write_text("bak")
        (cl / ".claude.json.corrupted.1779").write_text("bad")
        # Hidden skills subdir — NOT synced (every hidden dir is pruned).
        (cx / "skills" / ".system").mkdir(parents=True)
        (cx / "skills" / ".system" / "sk.md").write_text("skill")
        # Platform-materialized on-demand skills (skills_materializer) — the
        # non-hidden folders MUST sync (that's how remote sessions get them);
        # the .quarantine dir is hidden → stays platform-local.
        (cl / "skills" / "voiceover").mkdir(parents=True)
        (cl / "skills" / "voiceover" / "SKILL.md").write_text("skill body")
        (cl / "skills" / "voiceover" / ".oto-skill.json").write_text("{}")
        (cl / "skills" / ".quarantine" / "old-skill").mkdir(parents=True)
        (cl / "skills" / ".quarantine" / "old-skill" / "SKILL.md").write_text("x")

        paths = {e.path for e in compute_manifest(agent_dir, target_role="manager")}
        # settings.json + mcp-config.json are HOST-LOCAL (carry sandbox-internal
        # paths, regenerated per host) — never synced (_CLAUDE_HOST_LOCAL_FILES).
        assert "users/alice/.claude/settings.json" not in paths
        assert "users/alice/.claude/mcp-config.json" not in paths
        assert "users/alice/.claude/auth.json" not in paths
        assert "users/alice/.claude/.credentials.json" not in paths
        assert "users/alice/.codex/models_cache.json" in paths
        for leaked in (
            "users/alice/.claude/projects/f", "users/alice/.claude/tasks/f",
            "users/alice/.claude/backups/f",
            "users/alice/.claude/shell-snapshots/f", "users/alice/.claude/plans/f",
            "users/alice/.claude/ssh/f",
            "users/alice/.codex/sessions/f", "users/alice/.codex/.tmp/f",
            "users/alice/.codex/tmp/f", "users/alice/.codex/cache/f",
            "users/alice/.codex/shell_snapshots/f",
            "users/alice/.claude/.claude.json.backup.1775",
            "users/alice/.claude/.claude.json.corrupted.1779",
            "users/alice/.codex/skills/.system/sk.md",
            "users/alice/.claude/skills/.quarantine/old-skill/SKILL.md",
        ):
            assert leaked not in paths, f"cruft leaked: {leaked}"
        # Materialized skills DO sync — remote sessions depend on it.
        assert "users/alice/.claude/skills/voiceover/SKILL.md" in paths

    def test_satellite_authored_skills_never_write_back(self):
        """A compromised satellite writing into .claude/skills/ must never
        propagate back to the platform (and from there into other sessions):
        skills paths sit under the engine-machinery push-only segments, so
        write-back is denied for EVERY role and the next session-start scrub
        reverts satellite-local tampering. Regression-pinned so a future
        'owners curate skills on their machine' change can't silently open
        the loop."""
        from core.remote.file_sync import can_write_back, is_engine_machinery_path
        for path in ("users/alice/.claude/skills/evil/SKILL.md",
                     "workspace/.codex/skills/evil/SKILL.md"):
            assert is_engine_machinery_path(path)
            assert not can_write_back(path, "admin", "alice")

    def test_hash_format(self, agent_dir):
        entries = compute_manifest(agent_dir)
        for e in entries:
            assert e.hash.startswith("sha256:")
            assert len(e.hash) == 7 + 64  # "sha256:" + 64 hex chars

    def test_credentials_dir_never_in_manifest(self, agent_dir):
        """OAuth token files in ``.credentials/`` must NOT be in the
        manifest: they are per-session transients delivered over the
        session-file broker channel and wiped at session close — a
        satellite disk never holds long-lived refresh tokens. Both the
        user-scope and agent-scope locations stay out of the sync.
        """
        (agent_dir / "users" / "alice" / ".credentials" / "google-tokens").mkdir(parents=True)
        (
            agent_dir / "users" / "alice" / ".credentials" / "google-tokens"
            / "alice@gmail.com.json"
        ).write_text('{"access_token": "x"}')
        (agent_dir / "knowledge" / ".credentials" / "google-tokens").mkdir(parents=True)
        (
            agent_dir / "knowledge" / ".credentials" / "google-tokens"
            / "svc@gmail.com.json"
        ).write_text('{"access_token": "y"}')

        entries = compute_manifest(agent_dir, target_role="manager")
        paths = {e.path for e in entries}
        assert not any(".credentials" in p for p in paths)

    def test_other_hidden_dirs_still_skipped(self, agent_dir):
        """Whitelist is narrow — random hidden dirs (`.ssh/`, `.aws/`, etc.)
        stay excluded. Only ``.claude`` and ``.codex`` get the carve-out."""
        (agent_dir / "workspace" / ".ssh").mkdir()
        (agent_dir / "workspace" / ".ssh" / "id_rsa").write_text("secret")
        (agent_dir / "workspace" / ".aws").mkdir()
        (agent_dir / "workspace" / ".aws" / "credentials").write_text("aws")

        entries = compute_manifest(agent_dir)
        paths = {e.path for e in entries}
        assert "workspace/.ssh/id_rsa" not in paths
        assert "workspace/.aws/credentials" not in paths

    def test_codex_runtime_sqlite_excluded(self, agent_dir):
        """Host-local ``.codex`` files must NOT sync: the app-server's
        per-machine SQLite state (a foreign copy aborts daemon init with
        "migration N … has been modified") AND the per-session regenerated
        config files — config.toml carries the platform's REAL bearer tokens
        + platform paths, which must never land on a satellite.
        ``sessions/`` transcripts are excluded as runtime cruft
        (satellite-owned resume state). ``auth.json`` joined the host-local
        set after the writeback audit (regenerated per session from the DB
        subscription; never read back)."""
        cdir = agent_dir / "users" / "alice" / ".codex"
        (cdir / "sessions").mkdir(parents=True)
        (cdir / "config.toml").write_text("project_doc_max_bytes = 300000")
        (cdir / "AGENTS.md").write_text("# prompt")
        (cdir / "hooks.json").write_text("{}")
        (cdir / "auth.json").write_text("{}")
        (cdir / "sessions" / "thr-1.jsonl").write_text('{"ok":1}')
        # per-machine runtime — every SQLite family directly under .codex/ is
        # excluded (state/logs/goals/memories + any future family, incl.
        # -wal/-shm sidecars):
        (cdir / "state_v3.sqlite").write_bytes(b"SQLite format 3\x00")
        (cdir / "state_v3.sqlite-wal").write_bytes(b"wal")
        (cdir / "logs_2026.sqlite").write_bytes(b"SQLite format 3\x00")
        (cdir / "goals_1.sqlite").write_bytes(b"SQLite format 3\x00")
        (cdir / "memories_1.sqlite").write_bytes(b"SQLite format 3\x00")

        paths = {e.path for e in compute_manifest(agent_dir, target_role="manager")}
        assert "users/alice/.codex/auth.json" not in paths    # host-local
        assert "users/alice/.codex/config.toml" not in paths  # host-local
        assert "users/alice/.codex/AGENTS.md" not in paths    # host-local
        assert "users/alice/.codex/hooks.json" not in paths   # host-local
        assert "users/alice/.codex/sessions/thr-1.jsonl" not in paths  # runtime cruft
        assert "users/alice/.codex/state_v3.sqlite" not in paths
        assert "users/alice/.codex/state_v3.sqlite-wal" not in paths
        assert "users/alice/.codex/logs_2026.sqlite" not in paths
        assert "users/alice/.codex/goals_1.sqlite" not in paths
        assert "users/alice/.codex/memories_1.sqlite" not in paths


def _r(path, h, mtime=1.0):
    """A satellite manifest entry dict."""
    return {"path": path, "hash": h, "mtime": mtime}


class TestDiffManifests:
    """Versioned 3-way merge → MergePlan(actions, to_scrub)."""

    def test_new_file_on_platform(self):
        local = [FileEntry("config/new.md", "sha256:abc", 100, 1.0)]
        a = _act(diff_manifests(local, []), "config/new.md")
        assert a and a.op == "push"

    def test_new_file_on_satellite_pulled_when_writable(self):
        # No tombstone → never a delete. A manager may write workspace → pull.
        remote = [_r("workspace/data.txt", "sha256:def")]
        a = _act(diff_manifests([], remote, target_role="manager"), "workspace/data.txt")
        assert a and a.op == "pull"

    def test_exclude_user_dirs_ignores_users_paths_both_sides(self):
        """Shared-only agents: users/ paths are invisible to the merge — a
        stray satellite-side copy is neither pulled nor scrubbed, and a
        platform-side stray is never pushed."""
        local = [FileEntry("users/alice/workspace/a.md", "sha256:abc", 10, 1.0)]
        remote = [_r("users/alice/workspace/b.md", "sha256:def")]
        plan = diff_manifests(local, remote, target_role="manager",
                              target_username="alice", exclude_user_dirs=True)
        assert plan.actions == [] and plan.to_scrub == []

    def test_new_satellite_file_role_gated(self):
        # Pulls are gated by can_write_back; a non-writable satellite-only file is
        # LEFT in place (no action) — never deleted from absence.
        #  - viewer's workspace/ file → noop (viewer can't write shared)
        #  - editor's knowledge/ file → noop (knowledge is manager-curated)
        #  - manager's knowledge/ file → pull
        #  - viewer's OWN user dir    → pull (own content)
        #  - empty/unknown role       → noop
        plan = diff_manifests([], [_r("workspace/v.txt", "sha256:1")],
                              target_role="viewer", target_username="alice")
        assert _act(plan, "workspace/v.txt") is None and not plan.to_scrub

        plan = diff_manifests([], [_r("knowledge/n.md", "sha256:2")],
                              target_role="editor", target_username="alice")
        assert _act(plan, "knowledge/n.md") is None and not plan.to_scrub

        plan = diff_manifests([], [_r("knowledge/n.md", "sha256:2")],
                              target_role="manager", target_username="alice")
        a = _act(plan, "knowledge/n.md")
        assert a and a.op == "pull"

        plan = diff_manifests([], [_r("users/alice/workspace/a.md", "sha256:3")],
                              target_role="viewer", target_username="alice")
        a = _act(plan, "users/alice/workspace/a.md")
        assert a and a.op == "pull"

        plan = diff_manifests([], [_r("workspace/x.txt", "sha256:4")])
        assert _act(plan, "workspace/x.txt") is None and not plan.to_scrub

    def test_push_only_subtrees_scrubbed_not_pulled(self):
        # .claude/.codex/config on the satellite without a platform copy are
        # platform-authoritative extras → to_scrub (never pulled/captured).
        remote = [
            _r("users/alice/.claude/settings.json", "sha256:a"),
            _r("users/alice/.codex/config.toml", "sha256:b"),
            _r("config/prompt.md", "sha256:c"),
        ]
        plan = diff_manifests([], remote, target_role="manager", target_username="alice")
        assert not plan.actions
        assert set(plan.to_scrub) == {
            "users/alice/.claude/settings.json",
            "users/alice/.codex/config.toml",
            "config/prompt.md",
        }

    def test_satellite_owned_state_untouched(self):
        # Satellite-authoritative session state (.claude/projects, .claude/tasks,
        # .codex/sessions) is ignored entirely — never pulled, deleted, or
        # scrubbed. projects/ = chat-resume; tasks/ = the Claude Code task
        # store, keyed by the session id --resume keeps (scrubbing it lost the
        # task list on every re-warm — also covers a pre-0.5.80 satellite whose
        # manifest still includes task files).
        remote = [
            _r("users/alice/.claude/projects/p/s.jsonl", "sha256:a"),
            _r("users/alice/.claude/tasks/sid-1/1.json", "sha256:c"),
            _r("users/alice/.claude/tasks/sid-1/.highwatermark", "sha256:d"),
            _r("users/alice/.codex/sessions/2026/s.jsonl", "sha256:b"),
        ]
        plan = diff_manifests([], remote, target_role="manager", target_username="alice")
        assert not plan.actions and not plan.to_scrub

    def test_config_dir_platform_authoritative(self):
        local = [FileEntry("config/prompt.md", "sha256:new", 100, 2.0)]
        remote = [_r("config/prompt.md", "sha256:old")]
        a = _act(diff_manifests(local, remote), "config/prompt.md")
        assert a and a.op == "push"

    def test_extra_config_scrubbed_on_user_paired_only(self):
        # USER-paired (target_username set): config/ never syncs to non-owners,
        # so a satellite-only config extra is an isolation anomaly → scrub.
        plan = diff_manifests([], [_r("config/extra.md", "sha256:abc")],
                              target_role="editor", target_username="alice")
        assert plan.to_scrub == ["config/extra.md"] and not plan.actions

    def test_extra_config_on_admin_shared_left_for_curation(self):
        # ADMIN-SHARED (target_username None): the machine legitimately holds
        # config/ — a satellite-only extra is PENDING CURATION (e.g. written by
        # an owner-tier session whose identity an identity-less merge — idle
        # fingerprint sweep, reconnect catch-up — doesn't carry). Leave it for
        # a merge with a real owner-tier identity to pull; scrubbing here
        # deleted owner-written config/context files.
        plan = diff_manifests([], [_r("config/extra.md", "sha256:abc")])
        assert not plan.to_scrub and not plan.actions
        # The platform-regenerated SEGMENTS still scrub even admin-shared.
        plan = diff_manifests([], [_r("users/alice/.claude/settings.json", "sha256:a")])
        assert plan.to_scrub == ["users/alice/.claude/settings.json"]

    def test_owner_config_pulls_back_when_push_only_dropped(self):
        # sync-a: an owner-tier session passes push_only_dirs=set() so config/ is
        # NOT push-only — a manager's satellite-side config/ edit syncs BACK (pull),
        # not clobbered, with no recover-bin capture (only the satellite changed
        # since base; config/ is static so there is nothing to regenerate-clobber).
        local = [FileEntry("config/prompt.md", "sha256:old", 10, 1.0)]
        remote = [_r("config/prompt.md", "sha256:new", mtime=2.0)]
        base = {"config/prompt.md": ("sha256:old", 1.0)}  # platform == last-converged
        plan = diff_manifests(
            local, remote, base=base, push_only_dirs=set(),
            target_role="manager", target_username="alice",
        )
        a = _act(plan, "config/prompt.md")
        assert a and a.op == "pull"
        assert a.capture_reason is None  # only-satellite-changed → no conflict capture
        assert not plan.to_scrub

    def test_owner_writeback_drops_config_only_not_claude(self):
        # push_only_dirs=set() drops `config/` ONLY; the `.claude`/`.codex`/
        # `.credentials` segments stay platform-authoritative even for an owner.
        remote = [
            _r("users/alice/.claude/settings.json", "sha256:a"),
            _r("config/notes.md", "sha256:c"),
        ]
        plan = diff_manifests(
            [], remote, push_only_dirs=set(),
            target_role="manager", target_username="alice",
        )
        assert "users/alice/.claude/settings.json" in plan.to_scrub  # segment still push-only
        assert "config/notes.md" not in plan.to_scrub                 # config/ now adoptable
        a = _act(plan, "config/notes.md")
        assert a and a.op == "pull"

    def test_admin_shared_owner_config_writeback_via_session_username(self):
        # ADMIN-SHARED machine: target_username is None BY DESIGN (it is the
        # machine-pairing isolation filter, not a person). The write-back
        # identity there is the SESSION's authenticated human
        # (session_username) — an admin's satellite-side config/context file
        # syncs BACK instead of being stranded (the 2026-07-05 context-file
        # loss: the owner-tier carve-out could never fire on the
        # highest-trust target because it keyed on target_username).
        remote = [_r("config/context/dev-users.md", "sha256:new")]
        plan = diff_manifests(
            [], remote, push_only_dirs=set(),
            target_role="admin", target_username=None,
            session_username="dimitris",
        )
        a = _act(plan, "config/context/dev-users.md")
        assert a and a.op == "pull"
        assert not plan.to_scrub

    def test_admin_shared_service_session_config_not_adoptable(self):
        # A SERVICE session (task/phone/trigger) carries session_username ""
        # — the pull stays denied (mirroring can_write_back's username gate);
        # the file is left satellite-side, neither adopted nor scrubbed, even
        # if a caller wrongly dropped push-only.
        remote = [_r("config/context/dev-users.md", "sha256:new")]
        plan = diff_manifests(
            [], remote, push_only_dirs=set(),
            target_role="admin", target_username=None,
            session_username="",
        )
        assert _act(plan, "config/context/dev-users.md") is None
        assert not plan.to_scrub

    def test_orphaned_owner_machine_no_identity_substitution(self):
        # target_username == "" (owner record deleted) is fail-closed: the
        # session identity is NOT substituted — config/ stays un-adoptable on
        # an orphaned-owner machine regardless of who drives the session.
        remote = [_r("config/context/dev-users.md", "sha256:new")]
        plan = diff_manifests(
            [], remote, push_only_dirs=set(),
            target_role="admin", target_username="",
            session_username="dimitris",
        )
        assert _act(plan, "config/context/dev-users.md") is None
        assert not plan.to_scrub

    def test_identical_files_no_action(self):
        local = [FileEntry("workspace/data.txt", "sha256:same", 100, 1.0)]
        remote = [_r("workspace/data.txt", "sha256:same")]
        plan = diff_manifests(
            local, remote, base={"workspace/data.txt": ("sha256:same", 1.0)},
        )
        assert _act(plan, "workspace/data.txt") is None and not plan.to_scrub

    def test_in_sync_heals_stale_base(self):
        local = [FileEntry("workspace/data.txt", "sha256:same", 100, 1.0)]
        remote = [_r("workspace/data.txt", "sha256:same")]
        a = _act(diff_manifests(local, remote), "workspace/data.txt")  # no base
        assert a and a.op == "noop" and a.base_hash == "sha256:same"

    def test_isolation_scrub_other_user(self):
        # Another user's data leaked onto a user-paired satellite → scrub, never pull.
        plan = diff_manifests([], [_r("users/bob/secret", "sha256:x")],
                              target_role="editor", target_username="alice")
        assert plan.to_scrub == ["users/bob/secret"] and not plan.actions

    def test_p2_platform_only_with_base_re_pushed_never_deleted(self):
        # Satellite wiped a file the platform still has (base recorded) → RE-PUSH,
        # never delete the platform copy (defuses wiped-satellite mass-delete).
        local = [FileEntry("workspace/a", "sha256:h", 10, 1.0)]
        a = _act(diff_manifests(local, [], base={"workspace/a": ("sha256:h", 1.0)}),
                 "workspace/a")
        assert a and a.op == "push"


class TestMergeDivergence:
    """Both-sides-changed resolution + strict cross-user capture."""

    def _diverge(self, author, satellite_user, p_mtime, s_mtime, path="workspace/a",
                 offset=0.0):
        local = [FileEntry(path, "sha256:P", 10, p_mtime)]
        remote = [_r(path, "sha256:S", s_mtime)]
        return diff_manifests(
            local, remote, base={path: ("sha256:B", 1.0)},
            clock_offset=offset, author_of=lambda _p: author,
            satellite_user=satellite_user, target_role="editor",
            target_username="me",
        )

    def test_only_satellite_changed_pull_no_capture(self):
        local = [FileEntry("workspace/a", "sha256:B", 10, 1.0)]
        remote = [_r("workspace/a", "sha256:S", 9.0)]
        a = _act(diff_manifests(local, remote, base={"workspace/a": ("sha256:B", 1.0)},
                                target_role="editor", target_username="me"), "workspace/a")
        assert a and a.op == "pull" and a.capture_reason is None

    def test_only_platform_changed_push_no_capture(self):
        local = [FileEntry("workspace/a", "sha256:P", 10, 2.0)]
        remote = [_r("workspace/a", "sha256:B", 1.0)]
        a = _act(diff_manifests(local, remote, base={"workspace/a": ("sha256:B", 1.0)},
                                target_role="editor", target_username="me"), "workspace/a")
        assert a and a.op == "push" and a.capture_reason is None

    def test_cross_user_platform_newer_capture_satellite_notify_me(self):
        a = _act(self._diverge("other", "me", 2000.0, 1000.0), "workspace/a")
        assert (a and a.op == "push" and a.capture_side == "satellite"
                and a.capture_reason == "conflict" and a.notify_user == "me")

    def test_cross_user_satellite_newer_capture_platform_notify_author(self):
        a = _act(self._diverge("other", "me", 1000.0, 2000.0), "workspace/a")
        assert (a and a.op == "pull" and a.capture_side == "platform"
                and a.notify_user == "other")

    def test_personal_divergence_no_capture(self):
        a = _act(self._diverge("other", "me", 2000.0, 1000.0, path="users/me/a"),
                 "users/me/a")
        assert a and a.op == "push" and a.capture_reason is None

    def test_unknown_author_silent_capture(self):
        a = _act(self._diverge(None, "me", 2000.0, 1000.0), "workspace/a")
        assert a and a.capture_reason == "conflict" and a.notify_user is None

    def test_same_user_divergence_no_capture(self):
        a = _act(self._diverge("me", "me", 2000.0, 1000.0), "workspace/a")
        assert a and a.capture_reason is None

    def test_unknown_clock_offset_platform_wins(self):
        a = _act(self._diverge("other", "me", 1000.0, 9999.0, offset=None), "workspace/a")
        assert a and a.op == "push"  # un-orderable → platform-wins


class TestTombstones:
    """Tombstone-driven deletes and re-create resolution."""

    def test_tombstone_delete_wins_captures_then_deletes(self):
        remote = [_r("workspace/a", "sha256:S", 100.0)]
        a = _act(diff_manifests([], remote, tombstones={"workspace/a": 500.0},
                                target_role="editor", target_username="me"), "workspace/a")
        assert (a and a.op == "delete_satellite" and a.capture_reason == "deleted"
                and a.capture_side == "satellite" and a.clear_base)

    def test_tombstone_recreate_wins_pulls_and_drops(self):
        remote = [_r("workspace/a", "sha256:S", 900.0)]
        a = _act(diff_manifests([], remote, tombstones={"workspace/a": 500.0},
                                clock_offset=0.0, target_role="editor",
                                target_username="me"), "workspace/a")
        assert a and a.op == "pull" and a.drop_tombstone

    def test_platform_recreate_after_delete_pushes_and_drops(self):
        local = [FileEntry("workspace/a", "sha256:P", 10, 9.0)]
        a = _act(diff_manifests(local, [], tombstones={"workspace/a": 500.0}),
                 "workspace/a")
        assert a and a.op == "push" and a.drop_tombstone

    def test_both_absent_with_stale_base_clears_it(self):
        a = _act(diff_manifests([], [], base={"workspace/a": ("sha256:old", 1.0)}),
                 "workspace/a")
        assert a and a.op == "noop" and a.clear_base


class TestPrepareOutgoingFiles:
    def test_encodes_file_as_base64(self, agent_dir):
        messages = prepare_outgoing_files(agent_dir, ["config/prompt.md"])
        assert len(messages) == 1
        msg = messages[0]
        assert msg["action"] == "write"
        assert msg["path"] == "config/prompt.md"
        content = base64.b64decode(msg["content_b64"])
        assert content == b"# Test Agent"
        assert msg["hash"].startswith("sha256:")

    def test_skips_missing_files(self, agent_dir):
        messages = prepare_outgoing_files(agent_dir, ["nonexistent.txt"])
        assert len(messages) == 0

    def test_path_traversal_blocked(self, agent_dir):
        messages = prepare_outgoing_files(agent_dir, ["../../etc/passwd"])
        assert len(messages) == 0


class TestApplyIncomingFile:
    def test_write_file(self, agent_dir):
        content = base64.b64encode(b"new content").decode()
        apply_incoming_file(agent_dir, "workspace/new.txt", "write", content)
        assert (agent_dir / "workspace" / "new.txt").read_text() == "new content"

    def test_creates_parent_dirs(self, agent_dir):
        content = base64.b64encode(b"deep content").decode()
        apply_incoming_file(agent_dir, "users/bob/workspace/deep/file.txt", "write", content)
        assert (agent_dir / "users" / "bob" / "workspace" / "deep" / "file.txt").read_text() == "deep content"

    def test_delete_file(self, agent_dir):
        target = agent_dir / "workspace" / "data.txt"
        assert target.exists()
        apply_incoming_file(agent_dir, "workspace/data.txt", "delete")
        assert not target.exists()

    def test_mkdir(self, agent_dir):
        apply_incoming_file(agent_dir, "workspace/newdir", "mkdir")
        assert (agent_dir / "workspace" / "newdir").is_dir()

    def test_path_traversal_blocked(self, agent_dir):
        content = base64.b64encode(b"malicious").decode()
        apply_incoming_file(agent_dir, "../../etc/passwd", "write", content)
        assert not Path("/etc/passwd_malicious").exists()


class TestHashFile:
    def test_correct_hash(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        h = _hash_file(f)
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert h == f"sha256:{expected}"


class TestShouldSyncToTarget:
    """Push-direction predicate (platform → satellite). The SINGLE source of
    truth shared by compute_manifest (session-start) and workspace_fanout
    (per-write active-session fan-out)."""

    def test_admin_shared_no_per_user_filter(self):
        # username=None → admin-shared target: other users' dirs DO sync.
        assert should_sync_to_target("users/alice/x.md", None, "manager") is True
        assert should_sync_to_target("workspace/x.md", None, "viewer") is True
        assert should_sync_to_target("service-accounts/k.json", None, "manager") is True

    def test_admin_shared_config_still_role_gated(self):
        assert should_sync_to_target("config/p.md", None, "viewer") is False
        assert should_sync_to_target("config/p.md", None, "editor") is False
        assert should_sync_to_target("config/p.md", None, "manager") is True
        assert should_sync_to_target("config/p.md", None, "admin") is True

    def test_user_paired_other_user_blocked(self):
        assert should_sync_to_target("users/alice/x.md", "bob", "manager") is False
        assert should_sync_to_target("users/bob/x.md", "bob", "viewer") is True

    def test_user_paired_sensitive_blocked(self):
        assert should_sync_to_target("service-accounts/k.json", "bob", "manager") is False
        assert should_sync_to_target("knowledge/.credentials/t.json", "bob", "manager") is False

    def test_config_owner_only(self):
        assert should_sync_to_target("config/p.md", "bob", "viewer") is False
        assert should_sync_to_target("config/p.md", "bob", "editor") is False
        assert should_sync_to_target("config/p.md", "bob", "manager") is True

    def test_workspace_and_knowledge_sync_to_all_roles(self):
        for role in ("viewer", "editor", "manager", "admin"):
            assert should_sync_to_target("workspace/x.md", "bob", role) is True
            assert should_sync_to_target("knowledge/x.md", "bob", role) is True

    def test_agentscope_blank_username_no_user_dirs(self):
        # Agent-scope (username="") never receives any users/{u}/ file.
        assert should_sync_to_target("users/alice/x.md", "", "manager") is False
        assert should_sync_to_target("workspace/x.md", "", "manager") is True

    def test_matches_compute_manifest_inline_filters(self, agent_dir):
        # compute_manifest is now a thin wrapper over should_sync_to_target, so
        # every path in a user-paired viewer manifest must pass the predicate,
        # and the rejected classes must be absent.
        (agent_dir / "users" / "bob").mkdir(parents=True)
        (agent_dir / "users" / "bob" / "b.txt").write_text("bob")
        entries = compute_manifest(
            agent_dir, target_username="bob", target_role="viewer",
        )
        paths = {e.path for e in entries}
        for p in paths:
            assert should_sync_to_target(p, "bob", "viewer") is True
        assert "users/alice/notes.txt" not in paths   # other user → excluded
        assert "config/prompt.md" not in paths          # non-owner config → excluded
        assert "users/bob/b.txt" in paths               # own dir → present
        assert "workspace/data.txt" in paths            # shared → present

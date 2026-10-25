"""Path policy + git_writer tests for the memory system.

Memory scope dirs (``knowledge/memory/`` + ``users/{u}/context/memory/``)
are agent-write-denied at the path-policy layer — the ``memory`` MCP tool
(→ ``/v1/internal/memory/op``) is the single agent write path. Reads stay
allowed. ``git_writer.commit_paths`` commits multi-file memory ops as one
attributed commit.
"""

from __future__ import annotations

import pytest

import config as app_config
from auth.path_policy import (
    SecurityContext,
    _check_write_path,
    _is_memory_file,
)

# ---------------------------------------------------------------------------
# Memory-path detection
# ---------------------------------------------------------------------------


def test_agent_scope_memory_detected(temp_db):
    base = app_config.AGENTS_DIR / "acme" / "knowledge" / "memory"
    assert _is_memory_file(base / "infrastructure.md") is True
    assert _is_memory_file(base / "MEMORY.md") is True
    assert _is_memory_file(base / "sub" / "deep.md") is True


def test_user_scope_memory_detected(temp_db):
    base = (
        app_config.AGENTS_DIR / "acme" / "users" / "alice" / "context" / "memory"
    )
    assert _is_memory_file(base / "preferences.md") is True
    assert _is_memory_file(base / "MEMORY.md") is True


def test_non_memory_paths_not_detected(temp_db):
    agent = app_config.AGENTS_DIR / "acme"
    assert _is_memory_file(agent / "knowledge" / "guide.md") is False
    assert _is_memory_file(agent / "workspace" / "memory" / "notes.md") is False
    assert _is_memory_file(
        agent / "users" / "alice" / "context" / "personal-info.md"
    ) is False
    assert _is_memory_file(
        agent / "users" / "alice" / "workspace" / "memory" / "x.md"
    ) is False
    # Old v3 single-file locations are no longer special.
    assert _is_memory_file(
        agent / "config" / "context" / "AGENT_MEMORY.md"
    ) is False


# ---------------------------------------------------------------------------
# Write-policy enforcement (every role — the tool is the only agent path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["viewer", "editor", "manager", "admin"])
def test_agent_memory_write_denied_every_role(temp_db, role):
    ctx = SecurityContext(
        role=role, username="alice", agent="acme",
        is_admin_agent=(role == "admin"),
    )
    p = (
        app_config.AGENTS_DIR / "acme" / "knowledge" / "memory" / "x.md"
    ).resolve()
    decision = _check_write_path(p, ctx)
    assert decision.allowed is False
    assert "`memory` tool" in decision.reason


@pytest.mark.parametrize("role", ["viewer", "editor", "manager", "admin"])
def test_user_memory_write_denied_every_role(temp_db, role):
    ctx = SecurityContext(
        role=role, username="alice", agent="acme",
        is_admin_agent=(role == "admin"),
    )
    p = (
        app_config.AGENTS_DIR / "acme" / "users" / "alice" / "context"
        / "memory" / "prefs.md"
    ).resolve()
    assert _check_write_path(p, ctx).allowed is False


def test_generated_index_write_denied(temp_db):
    ctx = SecurityContext(
        role="manager", username="alice", agent="acme", is_admin_agent=False,
    )
    p = (
        app_config.AGENTS_DIR / "acme" / "knowledge" / "memory" / "MEMORY.md"
    ).resolve()
    assert _check_write_path(p, ctx).allowed is False


def test_other_knowledge_files_still_writable_by_manager(temp_db):
    """The memory rule must be narrow — knowledge/ outside memory/ keeps its
    normal owner-tier writability."""
    ctx = SecurityContext(
        role="manager", username="alice", agent="acme", is_admin_agent=False,
    )
    p = (app_config.AGENTS_DIR / "acme" / "knowledge" / "guide.md").resolve()
    assert _check_write_path(p, ctx).allowed is True


def test_old_v3_memory_filenames_no_longer_blocked(temp_db):
    """AGENT_MEMORY.md under config/context is just a context doc now
    (migration deletes the live ones; the rule moved to the memory dirs)."""
    ctx = SecurityContext(
        role="manager", username="alice", agent="acme", is_admin_agent=False,
    )
    p = (
        app_config.AGENTS_DIR / "acme" / "config" / "context" / "AGENT_MEMORY.md"
    ).resolve()
    assert _check_write_path(p, ctx).allowed is True


# ---------------------------------------------------------------------------
# git_writer round-trips (tmp_path, no DB)
# ---------------------------------------------------------------------------


def test_git_init_and_commit_roundtrip(tmp_path):
    from services.infra import git_writer
    repo = tmp_path / "agent-knowledge"
    assert git_writer.init_if_missing(repo) is True
    # Idempotent — second call is a no-op.
    assert git_writer.init_if_missing(repo) is False
    (repo / "topic.md").write_text("v1")
    sha1 = git_writer.commit_file(repo, repo / "topic.md", "first commit")
    assert sha1 and len(sha1) == 40
    (repo / "topic.md").write_text("v2")
    sha2 = git_writer.commit_file(repo, repo / "topic.md", "second commit")
    assert sha2 and sha2 != sha1
    log = git_writer.log(repo, limit=10)
    # 3 commits: init (.gitignore), v1, v2.
    assert len(log) == 3
    assert log[0]["subject"] == "second commit"


def test_git_revert_restores_old_content(tmp_path):
    from services.infra import git_writer
    repo = tmp_path / "agent-knowledge"
    git_writer.init_if_missing(repo)
    (repo / "f.md").write_text("v1")
    sha1 = git_writer.commit_file(repo, repo / "f.md", "v1")
    (repo / "f.md").write_text("v2")
    git_writer.commit_file(repo, repo / "f.md", "v2")
    git_writer.revert_file_to(repo, sha1, repo / "f.md", message="revert")
    assert (repo / "f.md").read_text() == "v1"


def test_git_log_empty_when_no_repo(tmp_path):
    from services.infra import git_writer
    assert git_writer.log(tmp_path / "no-such-repo") == []


# ---------------------------------------------------------------------------
# git_writer.commit_paths (multi-file memory commits)
# ---------------------------------------------------------------------------


def test_commit_paths_single_commit_for_topic_and_index(tmp_path):
    from services.infra import git_writer
    repo = tmp_path / "knowledge"
    mem = repo / "memory"
    mem.mkdir(parents=True)
    (mem / "topic.md").write_text("# Fact\n")
    (mem / "MEMORY.md").write_text("# Memory index\n- topic.md — Fact\n")
    sha = git_writer.commit_paths(
        repo, [mem / "topic.md", mem / "MEMORY.md"], "memory: create",
    )
    assert sha
    log = git_writer.log(repo, limit=5)
    assert log[0]["subject"] == "memory: create"
    # Both files land in ONE commit.
    diff = git_writer.diff(repo, log[0]["sha"])
    assert "topic.md" in diff and "MEMORY.md" in diff


def test_commit_paths_stages_deletions(tmp_path):
    from services.infra import git_writer
    repo = tmp_path / "knowledge"
    repo.mkdir()
    f = repo / "gone.md"
    f.write_text("x")
    git_writer.commit_paths(repo, [f], "add")
    f.unlink()
    sha = git_writer.commit_paths(repo, [f], "memory: delete")
    assert sha
    diff = git_writer.diff(repo, sha)
    assert "deleted file" in diff


def test_commit_paths_rejects_outside_repo(tmp_path):
    from services.infra import git_writer
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("x")
    assert git_writer.commit_paths(repo, [outside], "nope") is None

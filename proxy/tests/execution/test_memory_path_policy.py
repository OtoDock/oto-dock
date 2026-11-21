"""Path policy tests for the memory system.

Memory scope dirs (``knowledge/memory/`` + ``users/{u}/context/memory/``)
are agent-write-denied at the path-policy layer — the ``memory`` MCP tool
(→ ``/v1/internal/memory/op``) is the single agent write path. Reads stay
allowed.
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

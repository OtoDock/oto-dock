"""Path policy for external sessions (a phone caller who is not a platform
user): their own tree under /caller, the shared dirs read-only (the builder
always makes a caller a viewer; the role cases below are defence in depth),
never the shared memory, never another caller, never users/ or config/.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from auth import path_policy
from auth.path_policy import SecurityContext, check_tool_access
from core import placement


@pytest.fixture
def tree(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    agent_dir = agents / "support"
    for sub in ("workspace", "knowledge/memory", "config", "users/alice/workspace",
                "externals/phone/302101234567/workspace",
                "externals/phone/302101234567/context/memory",
                "externals/phone/999/workspace"):
        (agent_dir / sub).mkdir(parents=True)
    monkeypatch.setattr(path_policy, "_AGENTS_DIR", agents.resolve())
    return agent_dir.resolve()


def _ctx(tree: Path, *, role="viewer", home=True, knowledge_rw=False) -> SecurityContext:
    return SecurityContext(
        role=role, username="", agent="support", is_admin_agent=False,
        session_scope="agent", config_visible=False, knowledge_rw=knowledge_rw,
        principal="external", external_channel="phone", external_id="+302101234567",
        external_home=str(tree / "externals" / "phone" / "302101234567") if home else "",
        external_claim="phone:+302101234567",
    )


def _read(ctx, path):
    return check_tool_access("Read", {"file_path": path}, ctx)[0]


def _write(ctx, path):
    return check_tool_access("Write", {"file_path": path, "content": "x"}, ctx)[0]


class TestReads:
    def test_own_tree_and_shared_dirs(self, tree):
        ctx = _ctx(tree)
        assert _read(ctx, "/caller/workspace/a.txt").allowed
        assert _read(ctx, "/caller/context/notes.md").allowed
        assert _read(ctx, "workspace/a.txt").allowed          # relative → the caller cwd
        assert _read(ctx, "/workspace/shared.txt").allowed
        assert _read(ctx, "/knowledge/faq.md").allowed

    def test_denials(self, tree):
        ctx = _ctx(tree)
        assert not _read(ctx, "/knowledge/memory/MEMORY.md").allowed
        assert "external routes" in _read(ctx, "/knowledge/memory/MEMORY.md").reason
        assert not _read(ctx, "/users/alice/workspace/x.txt").allowed
        assert not _read(ctx, "/config/agent.md").allowed
        other = tree / "externals" / "phone" / "999" / "workspace" / "x.txt"
        assert not _read(ctx, str(other)).allowed
        assert "another caller" in _read(ctx, str(other)).reason

    def test_shared_mode_has_no_caller_root(self, tree):
        ctx = _ctx(tree, home=False)
        assert not _read(ctx, "/caller/workspace/a.txt").allowed
        assert _read(ctx, "/workspace/shared.txt").allowed
        assert not _read(ctx, "/knowledge/memory/MEMORY.md").allowed
        own = tree / "externals" / "phone" / "302101234567" / "workspace" / "x"
        assert not _read(ctx, str(own)).allowed   # no tree on this session


class TestWrites:
    def test_own_tree_subdirs_only(self, tree):
        ctx = _ctx(tree)
        assert _write(ctx, "/caller/workspace/a.txt").allowed
        assert _write(ctx, "/caller/context/notes.md").allowed
        root = _write(ctx, "/caller/stray.txt")
        assert not root.allowed and "reserved" in root.reason
        memory = _write(ctx, "/caller/context/memory/topic.md")
        assert not memory.allowed and "memory" in memory.reason.lower()
        other = tree / "externals" / "phone" / "999" / "workspace" / "x.txt"
        assert not _write(ctx, str(other)).allowed

    def test_shared_dirs_follow_the_route_role(self, tree):
        viewer = _ctx(tree, role="viewer")
        assert not _write(viewer, "/workspace/shared.txt").allowed
        assert not _write(viewer, "/knowledge/faq.md").allowed
        editor = _ctx(tree, role="editor")
        assert _write(editor, "/workspace/shared.txt").allowed
        assert not _write(editor, "/knowledge/faq.md").allowed
        manager = _ctx(tree, role="manager", knowledge_rw=True)
        assert _write(manager, "/workspace/shared.txt").allowed
        assert _write(manager, "/knowledge/faq.md").allowed
        assert not _write(manager, "/knowledge/memory/x.md").allowed
        assert not _write(manager, "/config/agent.md").allowed
        assert not _write(manager, "/users/alice/workspace/x.txt").allowed

    def test_memory_file_rule_covers_caller_trees(self, tree):
        p = tree / "externals" / "phone" / "302101234567" / "context" / "memory" / "t.md"
        assert path_policy._is_memory_file(p)
        assert not path_policy._is_memory_file(tree / "externals" / "phone" / "1" / "workspace" / "memory" / "t.md")


def test_the_floored_shell_tools():
    """The shell in every spelling — the Claude engine's own shell names, and
    the ``shell`` role the hook floor keys on; the web tools are deliberately
    NOT floored (2026-09-08 — a caller can already hear anything the session
    reads)."""
    from core.events import tool_roles
    from core.layers.cli.layer import CLIExecutionLayer
    layer = CLIExecutionLayer()
    assert set(layer.capabilities.behaviour.tools["shell"]) == {"Bash", "Monitor", "PowerShell"}
    assert layer.session_denied_tools(external=True, read_only=False) == ["Bash", "Monitor", "PowerShell"]
    assert set(tool_roles.names_of(tool_roles.SHELL)) == {"Bash", "Monitor", "PowerShell"}
    assert tool_roles.role_of("WebFetch") != tool_roles.SHELL
    assert tool_roles.role_of("WebSearch") != tool_roles.SHELL


class TestConfigDirAndPatches:
    """The caller's CLI config dir is never a tool-write target (it holds the
    permission hook and the session's config), and a Codex ``apply_patch``
    is checked file by file — it cannot copy a protected file or write
    outside the caller's writable subfolders."""

    def _patch(self, ctx, text):
        return check_tool_access("apply_patch", {"command": text}, ctx)[0]

    def test_no_tool_writes_into_the_caller_config_dirs(self, tree):
        ctx = _ctx(tree)
        assert _write(ctx, "/caller/workspace/notes.md").allowed
        assert _write(ctx, "/caller/context/prefs.md").allowed
        # The hook scripts and settings are the universal protected set;
        # anything else in the config dirs falls to the caller-root rule.
        for path in ("/caller/.codex/permission_gate.py", "/caller/.codex/hooks.json",
                     "/caller/.claude/settings.json", "/caller/.claude/permission_gate.py",
                     "/caller/.codex/notes.md"):
            decision = _write(ctx, path)
            assert not decision.allowed, path
            assert "protected" in decision.reason
        for path in ("/caller/.claude/todos/x.json",):
            decision = _write(ctx, path)
            assert not decision.allowed, path
            assert "workspace/ and context/" in decision.reason

    def test_config_files_in_the_caller_tree_are_read_protected(self, tree):
        ctx = _ctx(tree)
        for path in ("/caller/.codex/config.toml", "/caller/.codex/auth.json",
                     "/caller/.claude/settings.json"):
            decision = _read(ctx, path)
            assert not decision.allowed and "protected" in decision.reason, path

    def test_apply_patch_writes_only_where_write_may(self, tree):
        ctx = _ctx(tree)
        ok = "*** Begin Patch\n*** Add File: /caller/workspace/notes.md\n+hello\n*** End Patch"
        assert self._patch(ctx, ok).allowed
        shared = "*** Begin Patch\n*** Add File: /workspace/notes.md\n+hello\n*** End Patch"
        decision = self._patch(ctx, shared)
        assert not decision.allowed and "Add File /workspace/notes.md" in decision.reason
        gate = "*** Begin Patch\n*** Update File: /caller/.codex/permission_gate.py\n@@\n-x\n+y\n*** End Patch"
        assert not self._patch(ctx, gate).allowed

    def test_apply_patch_cannot_copy_a_protected_file(self, tree):
        ctx = _ctx(tree)
        copy = (
            "*** Begin Patch\n*** Update File: /caller/.codex/config.toml\n"
            "*** Move to: /caller/workspace/leak.txt\n*** End Patch"
        )
        decision = self._patch(ctx, copy)
        assert not decision.allowed and "config.toml" in decision.reason
        other = "*** Begin Patch\n*** Delete File: /caller/../999/workspace/x.md\n*** End Patch"
        assert not self._patch(ctx, other).allowed
        # A patch with no file headers is Codex's problem, not ours.
        assert self._patch(ctx, "*** Begin Patch\n*** End Patch").allowed
        assert check_tool_access("apply_patch", {"command": ""}, ctx)[0].allowed


class TestWebFetch:
    def _fetch(self, ctx, url):
        return check_tool_access("WebFetch", {"url": url}, ctx)[0]

    def test_public_web_from_a_local_sandbox(self, tree):
        ctx = _ctx(tree)
        assert self._fetch(ctx, "https://docs.otodock.io/features/phone").allowed
        # The SSRF gate still applies (literal private addresses / names).
        assert not self._fetch(ctx, "http://10.0.0.5/").allowed
        assert not self._fetch(ctx, "http://nas.local/").allowed

    def test_no_webfetch_on_a_remote_target(self, tree):
        """No netns on a satellite and the gate is literal-URL only, so an
        external session gets no WebFetch there at all."""
        for kind in (placement.KIND_ADMIN_REMOTE, placement.KIND_USER_REMOTE):
            ctx = dataclasses.replace(_ctx(tree, home=False), placement=placement.PlacementCapabilities(kind=kind))
            decision = self._fetch(ctx, "https://docs.otodock.io/")
            assert not decision.allowed and "remote machine" in decision.reason
        # A user session on the same target keeps the ordinary gate.
        user = SecurityContext(
            role="viewer", username="alice", agent="support", is_admin_agent=False,
            session_scope="user",
            placement=placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE),
            )
        assert self._fetch(user, "https://docs.otodock.io/").allowed

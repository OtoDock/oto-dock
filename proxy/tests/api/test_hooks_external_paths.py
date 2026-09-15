"""The hook path translator and scope roots for external sessions: a caller's
/caller, /.claude, /.codex and /context map into THEIR tree; a viewer's
/workspace redirects there too (the same asymmetry as a user viewer), while
an editor's /workspace stays the shared one.
"""

from __future__ import annotations

from pathlib import Path

from api.hooks.paths import _sandbox_to_host, _session_scope_root
from auth.path_policy import SecurityContext


def _ctx(home: str, role="viewer") -> SecurityContext:
    return SecurityContext(
        role=role, username="", agent="support", is_admin_agent=False,
        session_scope="agent", principal="external", external_home=home,
        external_claim="phone:+1",
    )


def test_caller_paths_map_into_the_tree(tmp_path):
    agent_dir = tmp_path / "support"
    home = str(agent_dir / "externals" / "phone" / "1")
    ctx = _ctx(home)
    assert _sandbox_to_host("/caller/workspace/a.txt", ctx, agent_dir) == f"{home}/workspace/a.txt"
    assert _sandbox_to_host("/caller", ctx, agent_dir) == home
    assert _sandbox_to_host("/.claude/plans/p.md", ctx, agent_dir) == f"{home}/.claude/plans/p.md"
    assert _sandbox_to_host("/.codex/x", ctx, agent_dir) == f"{home}/.codex/x"
    assert _sandbox_to_host("/context/notes.md", ctx, agent_dir) == f"{home}/context/notes.md"
    # Viewer: /workspace is their own; editor: the shared one.
    assert _sandbox_to_host("/workspace/out.xlsx", ctx, agent_dir) == f"{home}/workspace/out.xlsx"
    editor = _ctx(home, role="editor")
    assert _sandbox_to_host("/workspace/out.xlsx", editor, agent_dir) == str(agent_dir / "workspace" / "out.xlsx")
    assert _session_scope_root(ctx, agent_dir) == Path(home) / "workspace"


def test_without_a_tree_the_agent_scope_rules_apply(tmp_path):
    agent_dir = tmp_path / "support"
    ctx = _ctx("")
    assert _sandbox_to_host("/workspace/out.xlsx", ctx, agent_dir) == str(agent_dir / "workspace" / "out.xlsx")
    assert _sandbox_to_host("/.claude/x", ctx, agent_dir) == str(agent_dir / "workspace" / ".claude" / "x")
    assert _session_scope_root(ctx, agent_dir) == agent_dir / "workspace"

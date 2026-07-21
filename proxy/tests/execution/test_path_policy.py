"""Tests for auth/path_policy.py — permission matrix for the new folder structure.

Tests the restructured agent folder hierarchy:
  agents/{agent}/
    config/          (prompt.md, mcp-config.json, docs/, cron/)
    workspace/       (agent-scoped)
    users/{username}/ (workspace/, context/)
"""

import sys
from pathlib import Path

import pytest

# Ensure proxy root on path
from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

import config
from auth.path_policy import (
    SecurityContext,
    _check_read_path,
    _check_write_path,
    _is_other_users_dir,
    build_permission_context,
)

# ---------------------------------------------------------------------------
# Resolved base paths (mirrors path_policy.py module-level constants)
# ---------------------------------------------------------------------------
_AGENTS = config.AGENTS_DIR.resolve()


def _ctx(role: str, username: str, agent: str = "personal-assistant",
         is_admin_agent: bool = False) -> SecurityContext:
    return SecurityContext(role=role, username=username, agent=agent,
                          is_admin_agent=is_admin_agent)


def _resolve(rel: str) -> Path:
    """Resolve a relative path under agents/."""
    return (_AGENTS / rel).resolve()


# ===== _is_other_users_dir =====

class TestIsOtherUsersDir:
    def test_own_user_dir(self):
        path = _resolve("personal-assistant/users/alice/workspace/file.txt")
        assert _is_other_users_dir(path, "alice") is False

    def test_other_user_dir(self):
        path = _resolve("personal-assistant/users/bob/workspace/file.txt")
        assert _is_other_users_dir(path, "alice") is True

    def test_no_username_blocks_all(self):
        path = _resolve("personal-assistant/users/alice/workspace/file.txt")
        assert _is_other_users_dir(path, "") is True

    def test_agent_workspace_not_user_dir(self):
        path = _resolve("personal-assistant/workspace/tracking/file.txt")
        assert _is_other_users_dir(path, "alice") is False

    def test_config_not_user_dir(self):
        path = _resolve("personal-assistant/config/prompt.md")
        assert _is_other_users_dir(path, "alice") is False


# ===== Viewer Read (workspace + config + knowledge readable) =====

class TestViewerRead:
    ctx = _ctx("viewer", "alice")

    def test_own_user_workspace(self):
        path = _resolve("personal-assistant/users/alice/workspace/file.txt")
        assert _check_read_path(path, self.ctx).allowed

    def test_own_user_context(self):
        path = _resolve("personal-assistant/users/alice/context/info.md")
        assert _check_read_path(path, self.ctx).allowed

    def test_config_denied(self):
        """Viewer CANNOT read /config — owner-only (agent
        behavior is not workspace collaboration)."""
        path = _resolve("personal-assistant/config/prompt.md")
        assert not _check_read_path(path, self.ctx).allowed

    def test_agent_workspace_allowed(self):
        """Viewer can READ workspace (RO collaborator)."""
        path = _resolve("personal-assistant/workspace/tracking/file.txt")
        assert _check_read_path(path, self.ctx).allowed

    def test_knowledge_allowed(self):
        """Viewer can READ knowledge library (universal RO)."""
        path = _resolve("personal-assistant/knowledge/reference.md")
        assert _check_read_path(path, self.ctx).allowed

    def test_other_user_denied(self):
        path = _resolve("personal-assistant/users/bob/workspace/file.txt")
        assert not _check_read_path(path, self.ctx).allowed


# ===== Shared-only human chat (mount identity ≠ attribution) =====

class TestSharedOnlyHumanChat:
    """A Shared-only agent's human chat: ``username`` stays set for
    attribution but ``session_scope == "agent"`` — the mode has NO per-user
    dirs, so EVERY users/ path (including the human's "own") is denied while
    shared workspace / knowledge / owner config access keeps working."""

    ctx = SecurityContext(role="manager", username="alice",
                          agent="personal-assistant", is_admin_agent=False,
                          session_scope="agent")

    def test_own_user_dir_read_denied(self):
        path = _resolve("personal-assistant/users/alice/workspace/file.txt")
        assert not _check_read_path(path, self.ctx).allowed

    def test_own_user_dir_write_denied(self):
        path = _resolve("personal-assistant/users/alice/workspace/file.txt")
        assert not _check_write_path(path, self.ctx).allowed

    def test_shared_workspace_still_writable(self):
        path = _resolve("personal-assistant/workspace/apps/board.html")
        assert _check_write_path(path, self.ctx).allowed

    def test_owner_config_still_readable(self):
        # config curation keys on the REAL username (visibility contract).
        path = _resolve("personal-assistant/config/prompt.md")
        assert _check_read_path(path, self.ctx).allowed

    def test_owner_knowledge_still_writable(self):
        path = _resolve("personal-assistant/knowledge/ref.md")
        assert _check_write_path(path, self.ctx).allowed


# ===== Viewer Write (own user dir only) =====

class TestViewerWrite:
    ctx = _ctx("viewer", "alice")

    def test_own_user_workspace(self):
        path = _resolve("personal-assistant/users/alice/workspace/file.txt")
        assert _check_write_path(path, self.ctx).allowed

    def test_own_user_context(self):
        path = _resolve("personal-assistant/users/alice/context/info.md")
        assert _check_write_path(path, self.ctx).allowed

    def test_agent_workspace_denied(self):
        path = _resolve("personal-assistant/workspace/tracking/file.txt")
        assert not _check_write_path(path, self.ctx).allowed

    def test_config_denied(self):
        path = _resolve("personal-assistant/config/prompt.md")
        assert not _check_write_path(path, self.ctx).allowed

    def test_knowledge_denied(self):
        """Viewer CANNOT write knowledge (owner-only curation)."""
        path = _resolve("personal-assistant/knowledge/note.md")
        assert not _check_write_path(path, self.ctx).allowed

    def test_other_user_denied(self):
        path = _resolve("personal-assistant/users/bob/workspace/file.txt")
        assert not _check_write_path(path, self.ctx).allowed


# ===== User-dir ROOT is write-reserved (strays next to workspace/) =====

class TestUserDirRootWriteReserved:
    """Writes inside users/{own}/ are allowed only within the known subdirs
    (_USER_DIR_WRITABLE_SUBDIRS) — a file at the dir root is denied for
    every role. Mirrors the RO-root + RW-subdir bwrap mount."""

    @pytest.mark.parametrize("role", ["viewer", "editor", "manager", "admin"])
    def test_root_level_file_denied(self, role):
        path = _resolve("personal-assistant/users/alice/google-home.png")
        decision = _check_write_path(path, _ctx(role, "alice"))
        assert not decision.allowed
        assert "personal folder" in decision.reason

    def test_unknown_root_subdir_denied(self):
        path = _resolve("personal-assistant/users/alice/scratch/notes.txt")
        assert not _check_write_path(path, _ctx("manager", "alice")).allowed

    def test_claude_plans_allowed(self):
        path = _resolve("personal-assistant/users/alice/.claude/plans/plan.md")
        assert _check_write_path(path, _ctx("viewer", "alice")).allowed

    def test_codex_state_allowed(self):
        path = _resolve("personal-assistant/users/alice/.codex/config.toml")
        assert _check_write_path(path, _ctx("manager", "alice")).allowed

    def test_admin_on_admin_agent_fast_path_kept(self):
        """Admin-on-admin-agent skips path checks (contract) — the bwrap
        RO root is what denies the stray write at the kernel there."""
        path = _resolve("system-admin/users/alice/stray.txt")
        ctx = _ctx("admin", "alice", agent="system-admin", is_admin_agent=True)
        assert _check_write_path(path, ctx).allowed


# ===== Editor Read (new role) =====

class TestEditorRead:
    ctx = _ctx("editor", "alice")

    def test_own_user_workspace(self):
        path = _resolve("personal-assistant/users/alice/workspace/file.txt")
        assert _check_read_path(path, self.ctx).allowed

    def test_config_denied(self):
        """Editor CANNOT read /config — owner-only."""
        path = _resolve("personal-assistant/config/prompt.md")
        assert not _check_read_path(path, self.ctx).allowed

    def test_workspace_allowed(self):
        path = _resolve("personal-assistant/workspace/tracking/file.txt")
        assert _check_read_path(path, self.ctx).allowed

    def test_knowledge_allowed(self):
        path = _resolve("personal-assistant/knowledge/reference.md")
        assert _check_read_path(path, self.ctx).allowed

    def test_other_user_denied(self):
        path = _resolve("personal-assistant/users/bob/workspace/file.txt")
        assert not _check_read_path(path, self.ctx).allowed


# ===== Editor Write (workspace RW; config + knowledge RO) =====

class TestEditorWrite:
    ctx = _ctx("editor", "alice")

    def test_own_user_workspace_allowed(self):
        path = _resolve("personal-assistant/users/alice/workspace/file.txt")
        assert _check_write_path(path, self.ctx).allowed

    def test_agent_workspace_allowed(self):
        """Editor CAN write to shared workspace (collaborative tier)."""
        path = _resolve("personal-assistant/workspace/output.json")
        assert _check_write_path(path, self.ctx).allowed

    def test_config_denied(self):
        """Editor CANNOT write to config (owner-only)."""
        path = _resolve("personal-assistant/config/prompt.md")
        assert not _check_write_path(path, self.ctx).allowed

    def test_knowledge_denied(self):
        """Editor CANNOT write to knowledge (owner-only)."""
        path = _resolve("personal-assistant/knowledge/note.md")
        assert not _check_write_path(path, self.ctx).allowed

    def test_other_user_denied(self):
        path = _resolve("personal-assistant/users/bob/workspace/file.txt")
        assert not _check_write_path(path, self.ctx).allowed


# ===== Manager Read =====

class TestManagerRead:
    ctx = _ctx("manager", "alice")

    def test_config(self):
        path = _resolve("personal-assistant/config/prompt.md")
        assert _check_read_path(path, self.ctx).allowed

    def test_config_context(self):
        path = _resolve("personal-assistant/config/context/guide.md")
        assert _check_read_path(path, self.ctx).allowed

    def test_agent_workspace(self):
        path = _resolve("personal-assistant/workspace/tracking/file.txt")
        assert _check_read_path(path, self.ctx).allowed

    def test_own_user_dir(self):
        path = _resolve("personal-assistant/users/alice/workspace/file.txt")
        assert _check_read_path(path, self.ctx).allowed

    def test_other_user_denied(self):
        path = _resolve("personal-assistant/users/bob/workspace/file.txt")
        assert not _check_read_path(path, self.ctx).allowed


# ===== Manager Write =====

class TestManagerWrite:
    ctx = _ctx("manager", "alice")

    def test_config(self):
        path = _resolve("personal-assistant/config/prompt.md")
        assert _check_write_path(path, self.ctx).allowed

    def test_agent_workspace(self):
        path = _resolve("personal-assistant/workspace/output.json")
        assert _check_write_path(path, self.ctx).allowed

    def test_own_user_dir(self):
        path = _resolve("personal-assistant/users/alice/workspace/file.txt")
        assert _check_write_path(path, self.ctx).allowed

    def test_other_user_denied(self):
        path = _resolve("personal-assistant/users/bob/workspace/file.txt")
        assert not _check_write_path(path, self.ctx).allowed


# ===== Admin on non-admin agent (same as manager) =====

class TestAdminNonAdminAgent:
    ctx = _ctx("admin", "alice", agent="personal-assistant", is_admin_agent=False)

    def test_read_config(self):
        path = _resolve("personal-assistant/config/prompt.md")
        assert _check_read_path(path, self.ctx).allowed

    def test_read_workspace(self):
        path = _resolve("personal-assistant/workspace/tracking/file.txt")
        assert _check_read_path(path, self.ctx).allowed

    def test_read_own_user(self):
        path = _resolve("personal-assistant/users/alice/workspace/file.txt")
        assert _check_read_path(path, self.ctx).allowed

    def test_read_other_user_denied(self):
        path = _resolve("personal-assistant/users/bob/workspace/file.txt")
        assert not _check_read_path(path, self.ctx).allowed

    def test_write_other_user_denied(self):
        path = _resolve("personal-assistant/users/bob/workspace/file.txt")
        assert not _check_write_path(path, self.ctx).allowed


# ===== Admin on admin agent (unrestricted) =====

class TestAdminAdminAgent:
    ctx = _ctx("admin", "alice", agent="system-admin", is_admin_agent=True)

    def test_read_anything(self):
        path = _resolve("personal-assistant/users/bob/workspace/file.txt")
        assert _check_read_path(path, self.ctx).allowed

    def test_write_other_user(self):
        path = _resolve("personal-assistant/users/bob/workspace/file.txt")
        assert _check_write_path(path, self.ctx).allowed

    def test_write_proxy_denied(self):
        """Even admin on admin agent cannot write to proxy/."""
        from auth.path_policy import _PROXY_DIR
        path = (_PROXY_DIR / "app.py").resolve()
        assert not _check_write_path(path, self.ctx).allowed


# ===== Agent-scoped tasks (username="") =====

class TestAgentScopedTask:
    """Agent-scoped tasks have username="" and role=manager."""
    ctx = _ctx("manager", "", agent="personal-assistant")

    def test_read_workspace_allowed(self):
        path = _resolve("personal-assistant/workspace/tracking/file.txt")
        assert _check_read_path(path, self.ctx).allowed

    def test_read_config_denied(self):
        path = _resolve("personal-assistant/config/prompt.md")
        assert not _check_read_path(path, self.ctx).allowed

    def test_read_any_user_dir_denied(self):
        path = _resolve("personal-assistant/users/alice/workspace/file.txt")
        assert not _check_read_path(path, self.ctx).allowed

    def test_write_workspace_allowed(self):
        path = _resolve("personal-assistant/workspace/output.json")
        assert _check_write_path(path, self.ctx).allowed

    def test_write_config_denied(self):
        path = _resolve("personal-assistant/config/prompt.md")
        assert not _check_write_path(path, self.ctx).allowed

    def test_write_any_user_dir_denied(self):
        path = _resolve("personal-assistant/users/alice/workspace/file.txt")
        assert not _check_write_path(path, self.ctx).allowed


# ===== Agent-scoped task on admin agent =====

class TestAgentScopedAdminAgent:
    ctx = _ctx("admin", "", agent="system-admin", is_admin_agent=True)

    def test_read_unrestricted(self):
        path = _resolve("personal-assistant/users/alice/workspace/file.txt")
        assert _check_read_path(path, self.ctx).allowed

    def test_write_unrestricted(self):
        path = _resolve("personal-assistant/users/alice/workspace/file.txt")
        assert _check_write_path(path, self.ctx).allowed


# ===== Always-deny paths =====

class TestAlwaysDeny:
    def test_env_file(self):
        ctx = _ctx("admin", "alice", agent="system-admin", is_admin_agent=True)
        path = _resolve("personal-assistant/.env")
        assert not _check_write_path(path, ctx).allowed


# ===== build_permission_context =====

class TestBuildPermissionContext:
    def test_viewer_mentions_users_dir(self):
        ctx = _ctx("viewer", "alice")
        text = build_permission_context(ctx)
        assert "users/alice/workspace/" in text
        assert "users/alice/context/" in text

    def test_manager_mentions_config(self):
        ctx = _ctx("manager", "alice")
        text = build_permission_context(ctx)
        assert "config/" in text
        assert "workspace/" in text

    def test_admin_agent_unrestricted(self):
        ctx = _ctx("admin", "alice", agent="system-admin", is_admin_agent=True)
        text = build_permission_context(ctx)
        assert "admin-only agent" in text


# ===== Bash with sandbox-style path args =====
#
# Regression: _check_bash used to skip _translate_sandbox_path on path args
# extracted from bash commands. Sandbox-style paths like /users/{u}/...
# are valid inside the bwrap mount but resolve to non-existent host paths,
# so the host-path check failed → bash hook denied valid commands.

class TestBashSandboxPaths:
    @staticmethod
    def _check(command: str, ctx: SecurityContext):
        from auth.path_policy import check_tool_access
        decision, _ = check_tool_access("Bash", {"command": command}, ctx)
        return decision

    def test_manager_can_ls_own_user_workspace_sandbox_path(self):
        ctx = _ctx("manager", "alice")
        assert self._check("ls /users/alice/workspace/foo", ctx).allowed

    def test_manager_can_ls_workspace_sandbox_path(self):
        ctx = _ctx("manager", "alice")
        assert self._check("ls /workspace/foo", ctx).allowed

    def test_admin_can_ls_user_workspace_sandbox_path(self):
        ctx = _ctx("admin", "alice")
        assert self._check("ls /users/alice/workspace/foo", ctx).allowed

    def test_viewer_can_ls_own_user_dir(self):
        ctx = _ctx("viewer", "alice")
        assert self._check("ls /users/alice/workspace/foo", ctx).allowed

    def test_viewer_cannot_ls_other_user_dir(self):
        ctx = _ctx("viewer", "alice")
        assert not self._check("ls /users/bob/workspace/foo", ctx).allowed

    def test_viewer_can_ls_workspace(self):
        """Viewer CAN ls /workspace (RO collaborator)."""
        ctx = _ctx("viewer", "alice")
        assert self._check("ls /workspace/foo", ctx).allowed

    def test_viewer_cannot_write_workspace(self):
        """Viewer can READ workspace but NOT write to it."""
        ctx = _ctx("viewer", "alice")
        assert not self._check("touch /workspace/foo.txt", ctx).allowed

    def test_viewer_can_ls_knowledge(self):
        """Viewer CAN ls /knowledge (reference library)."""
        ctx = _ctx("viewer", "alice")
        assert self._check("ls /knowledge/refs", ctx).allowed

    def test_viewer_cannot_write_knowledge(self):
        """Knowledge is owner-only for writes; viewer denied."""
        ctx = _ctx("viewer", "alice")
        assert not self._check("touch /knowledge/note.md", ctx).allowed

    def test_agent_scoped_can_ls_workspace(self):
        ctx = _ctx("admin", "")
        assert self._check("ls /workspace/foo", ctx).allowed

    def test_agent_scoped_can_ls_knowledge(self):
        """Agent-scoped sessions read /knowledge for reference material."""
        ctx = _ctx("admin", "")
        assert self._check("ls /knowledge/templates", ctx).allowed

    def test_agent_scoped_cannot_write_knowledge(self):
        """Knowledge stays owner-only — agent-scope is consumer-only."""
        ctx = _ctx("admin", "")
        assert not self._check("touch /knowledge/x.md", ctx).allowed

    def test_agent_scoped_cannot_ls_user_dir(self):
        """Agent-scoped session has no user dir; bash hook should deny."""
        ctx = _ctx("admin", "")
        assert not self._check("ls /users/alice/workspace/foo", ctx).allowed

    def test_manager_can_cat_config_sandbox_path(self):
        ctx = _ctx("manager", "alice")
        assert self._check("cat /config/prompt.md", ctx).allowed

    def test_viewer_cannot_cat_config_sandbox_path(self):
        """Viewer CANNOT read /config — owner-only."""
        ctx = _ctx("viewer", "alice")
        assert not self._check("cat /config/prompt.md", ctx).allowed

    def test_editor_can_ls_workspace(self):
        ctx = _ctx("editor", "alice")
        assert self._check("ls /workspace/foo", ctx).allowed

    def test_editor_can_write_workspace(self):
        ctx = _ctx("editor", "alice")
        assert self._check("touch /workspace/note.md", ctx).allowed

    def test_editor_cannot_read_config(self):
        """Editor cannot even READ /config (owner-only)."""
        ctx = _ctx("editor", "alice")
        assert not self._check("cat /config/prompt.md", ctx).allowed

    def test_editor_cannot_write_config(self):
        """Editor cannot write to /config (owner-only)."""
        ctx = _ctx("editor", "alice")
        assert not self._check("touch /config/extra.md", ctx).allowed

    def test_editor_cannot_write_knowledge(self):
        """Editor read-but-not-write knowledge (owner curates)."""
        ctx = _ctx("editor", "alice")
        assert not self._check("touch /knowledge/x.md", ctx).allowed

    def test_manager_can_redirect_to_workspace_sandbox_path(self):
        """Redirect targets get the same sandbox-path translation."""
        ctx = _ctx("manager", "alice")
        assert self._check("echo hi > /users/alice/workspace/note.txt", ctx).allowed

    def test_manager_can_grep_files_in_sandbox_dir(self):
        """Multiple read-path args still translated."""
        ctx = _ctx("manager", "alice")
        assert self._check("grep foo /users/alice/workspace/a /users/alice/workspace/b", ctx).allowed

    def test_unicode_filename_in_sandbox_path(self):
        """Greek/Unicode filenames pass through translation correctly."""
        ctx = _ctx("manager", "alice")
        assert self._check("cat /users/alice/workspace/Πρόγραμμα.xlsx", ctx).allowed


# ===== Bash tier classification for dev tools =====
#
# `git`/`gh` are edit-tier (auto-approve in acceptEdits — `git push` /
# `gh pr create` mutate remote state). `rg` (ripgrep) and `uv` are
# read-tier inspection tools. Path-arg extraction does not fire for
# these commands by design — bwrap enforces filesystem boundaries; the
# tier check is the manifest gate at the bash hook level.

class TestBashTierForDevTools:
    @staticmethod
    def _decision(command: str, ctx: SecurityContext):
        from auth.path_policy import check_tool_access
        decision, _ = check_tool_access("Bash", {"command": command}, ctx)
        return decision

    def test_git_clone_allowed_for_manager(self):
        ctx = _ctx("manager", "alice")
        d = self._decision("git clone https://github.com/example/repo.git", ctx)
        assert d.allowed
        assert d.permission_tier == "edit"

    def test_git_status_allowed_for_manager(self):
        ctx = _ctx("manager", "alice")
        d = self._decision("git status", ctx)
        assert d.allowed
        assert d.permission_tier == "edit"

    def test_gh_pr_create_allowed_for_manager(self):
        ctx = _ctx("manager", "alice")
        d = self._decision("gh pr create --fill", ctx)
        assert d.allowed
        assert d.permission_tier == "edit"

    def test_rg_allowed_for_viewer(self):
        ctx = _ctx("viewer", "alice")
        d = self._decision("rg foo /users/alice/workspace", ctx)
        assert d.allowed
        assert d.permission_tier == "read"

    def test_uv_is_extended_tier_runner(self):
        """B1: `uv`/`uvx` run arbitrary code (uv run / uvx <tool>) → extended
        tier (prompts in default mode), NOT read. Still allowed for all roles —
        bwrap (local) / per-uid scope (remote) bounds it, like python3.
        (`xargs` is now a prefix wrapper — see test_wrapper_unwrap_* in
        test_bash_policy_v2.py — so `echo x | xargs cat` classifies as `cat`.)"""
        ctx = _ctx("manager", "alice")
        for cmd in ("uv run script.py", "uvx ruff check"):
            d = self._decision(cmd, ctx)
            assert d.allowed, cmd
            assert d.permission_tier == "extended", f"{cmd} -> {d.permission_tier}"

    def test_b1_reader_cross_user_read_denied(self):
        """B1: casual cross-user reads via newly-extracted readers are now
        path-checked + denied (previously fell through to `([],[])` → allowed)."""
        ctx = _ctx("viewer", "alice")
        for cmd in (
            "rg secret /users/bob/workspace",
            "sort /users/bob/workspace/notes.txt",
            "awk '{print}' /users/bob/workspace/notes.txt",
            "cut -f1 /users/bob/workspace/data.csv",
            "jq .token /users/bob/workspace/creds.json",
            "nl /users/bob/workspace/notes.txt",
            "tac /users/bob/workspace/notes.txt",
        ):
            d = self._decision(cmd, ctx)
            assert not d.allowed, f"should deny cross-user: {cmd}"

    def test_b1_readers_own_dir_not_falsely_denied(self):
        """B1 value-flag handling: own-dir reader commands WITH value flags must
        NOT be denied (the flag value must not be parsed as a bogus path)."""
        ctx = _ctx("viewer", "alice")
        for cmd in (
            "rg -A 3 foo /users/alice/workspace",
            "rg -e foo /users/alice/workspace",
            "sort -k 2 /users/alice/workspace/f.txt",
            "cut -d : -f 1 /users/alice/workspace/f.txt",
            "awk -F: '{print $1}' /users/alice/workspace/f.txt",
            "jq .name /users/alice/workspace/f.json",
            "nl -w 3 /users/alice/workspace/f.txt",
            "tac -s X /users/alice/workspace/f.txt",
        ):
            d = self._decision(cmd, ctx)
            assert d.allowed, f"{cmd} -> {d.reason}"
            assert d.permission_tier == "read", f"{cmd} -> {d.permission_tier}"

    def test_b1_sort_output_to_other_user_denied(self):
        """B1: `sort -o /users/OTHER/...` routes the -o value to write_paths and
        is gated as a cross-user WRITE."""
        ctx = _ctx("viewer", "alice")
        d = self._decision(
            "sort -o /users/bob/workspace/out.txt /users/alice/workspace/in.txt", ctx,
        )
        assert not d.allowed

    def test_jq_allowed_for_viewer(self):
        """Regression: `jq` was already in read tier — keep it that way."""
        ctx = _ctx("viewer", "alice")
        d = self._decision("jq .name /users/alice/workspace/file.json", ctx)
        assert d.allowed
        assert d.permission_tier == "read"

    def test_unknown_dev_tool_now_asks(self):
        """Exec-env v2: unknown commands (e.g. `kubectl`) are NO LONGER
        hard-denied — they classify as tier "ask" (prompt in default/acceptEdits,
        run in dontAsk/auto). The LLM can't get past a hard-deny; a human can
        approve a prompt. This is the core UX fix."""
        ctx = _ctx("manager", "alice")
        d = self._decision("kubectl get pods", ctx)
        assert d.allowed
        assert d.permission_tier == "ask"

    # ---------------------------------------------------------------------
    # Extended tier (curl/python/node/pip/build tools) — ALL roles
    # ---------------------------------------------------------------------
    def test_curl_allowed_for_manager(self):
        ctx = _ctx("manager", "alice")
        d = self._decision("curl https://example.com", ctx)
        assert d.allowed
        assert d.permission_tier == "extended"

    def test_curl_allowed_for_editor(self):
        ctx = _ctx("editor", "alice")
        d = self._decision("curl https://example.com", ctx)
        assert d.allowed
        assert d.permission_tier == "extended"

    def test_curl_allowed_for_viewer(self):
        """Viewers can curl in their own user dir — bwrap restricts what they can write."""
        ctx = _ctx("viewer", "alice")
        d = self._decision("curl https://example.com", ctx)
        assert d.allowed
        assert d.permission_tier == "extended"

    def test_python3_allowed_for_manager(self):
        ctx = _ctx("manager", "alice")
        d = self._decision("python3 /users/alice/workspace/script.py", ctx)
        assert d.allowed
        assert d.permission_tier == "extended"

    def test_python3_allowed_for_viewer(self):
        """Full dev access — viewers can run scripts in their own dir."""
        ctx = _ctx("viewer", "alice")
        d = self._decision("python3 /users/alice/workspace/script.py", ctx)
        assert d.allowed

    def test_pip_install_allowed_for_manager(self):
        ctx = _ctx("manager", "alice")
        d = self._decision("pip install requests", ctx)
        assert d.allowed
        assert d.permission_tier == "extended"

    def test_npm_install_allowed_for_editor(self):
        ctx = _ctx("editor", "alice")
        d = self._decision("npm install", ctx)
        assert d.allowed

    # ---------------------------------------------------------------------
    # Document tools (poppler + sqlite3)
    # ---------------------------------------------------------------------
    def test_pdftotext_allowed_for_viewer(self):
        """PDF text extraction (poppler-utils) — pure read, every role."""
        ctx = _ctx("viewer", "alice")
        d = self._decision(
            "pdftotext /users/alice/workspace/doc.pdf /users/alice/workspace/doc.txt",
            ctx,
        )
        assert d.allowed
        assert d.permission_tier == "read"

    def test_sqlite3_allowed_for_manager(self):
        ctx = _ctx("manager", "alice")
        d = self._decision(
            "sqlite3 /users/alice/workspace/db.sqlite \"SELECT * FROM t\"",
            ctx,
        )
        assert d.allowed

    # ---------------------------------------------------------------------
    # Admin tier (docker/systemctl/ssh/apt) — admin only
    # ---------------------------------------------------------------------
    def test_docker_admin_only(self):
        for role in ("manager", "editor", "viewer"):
            ctx = _ctx(role, "alice")
            d = self._decision("docker ps", ctx)
            assert not d.allowed, f"docker should deny for {role}"
            assert "platform admin role" in d.reason

    def test_docker_allowed_for_admin(self):
        ctx = _ctx("admin", "alice")
        d = self._decision("docker ps", ctx)
        assert d.allowed
        assert d.permission_tier == "admin"

    def test_systemctl_admin_only(self):
        ctx = _ctx("manager", "alice")
        d = self._decision("systemctl status nginx", ctx)
        assert not d.allowed
        assert "platform admin role" in d.reason

    def test_ssh_admin_only(self):
        ctx = _ctx("manager", "alice")
        d = self._decision("ssh user@host", ctx)
        assert not d.allowed
        assert "platform admin role" in d.reason

    def test_apt_admin_only(self):
        ctx = _ctx("manager", "alice")
        d = self._decision("apt install foo", ctx)
        assert not d.allowed
        assert "platform admin role" in d.reason

    def test_printenv_no_args_allowed(self):
        """printenv with no args lists all env vars — read tier."""
        ctx = _ctx("manager", "alice")
        d = self._decision("printenv", ctx)
        assert d.allowed
        assert d.permission_tier == "read"

    def test_printenv_with_var_allowed(self):
        """printenv VAR is the canonical way to read a single env var."""
        ctx = _ctx("viewer", "alice")
        d = self._decision("printenv GH_TOKEN", ctx)
        assert d.allowed
        assert d.permission_tier == "read"

    def test_env_wrapper_unwraps_to_inner(self):
        """Exec-env v2: `env <cmd>` is a prefix WRAPPER — we strip `env` (+ its
        NAME=val / flag tokens) and classify the INNER command, so
        `env curl …` is extended (prompts in default) instead of the old
        hard-deny. The old bypass concern (`env` classified as read → silently
        auto-approving the inner) is gone: the inner is what's classified, and
        unknown inners are "ask" (prompt), never auto-read."""
        ctx = _ctx("admin", "alice")  # non-admin AGENT, so no fast-path
        d = self._decision("env curl https://example.com", ctx)
        assert d.allowed
        assert d.permission_tier == "extended"
        # `env FOO=1 <cmd>` skips the assignment too.
        d2 = self._decision("env DEBUG=1 curl https://example.com", ctx)
        assert d2.allowed
        assert d2.permission_tier == "extended"

    def test_env_var_assignment_form_still_works(self):
        """The legitimate alternative to `env VAR=x cmd` works natively:
        `VAR=x cmd` — parser skips KEY=value tokens, gates the real cmd."""
        ctx = _ctx("manager", "alice")
        # cat is read tier — the env-var assignment doesn't bump the tier.
        d = self._decision("DEBUG=1 cat /users/alice/workspace/file.txt", ctx)
        assert d.allowed
        assert d.permission_tier == "read"


# ===== Value-flag parser bug fix (head -c N, tail -n N, etc.) =====
#
# Surfaced during env-injection verification: the agent ran
# `printenv GH_TOKEN | head -c 8` and got denied because the parser
# treated `8` as a file path argument to head. Fixed via
# _VALUE_FLAGS_BY_CMD table + skip-next-after-flag rule.

class TestValueFlagParser:
    @staticmethod
    def _decision(command: str, ctx: SecurityContext):
        from auth.path_policy import check_tool_access
        decision, _ = check_tool_access("Bash", {"command": command}, ctx)
        return decision

    def test_head_c_N_no_file_arg_allowed(self):
        """`head -c 8` standalone (e.g. piped input) — N must not be
        treated as a path."""
        ctx = _ctx("manager", "alice")
        d = self._decision("printenv GH_TOKEN | head -c 8", ctx)
        assert d.allowed, f"denied: {d.reason}"

    def test_head_n_N_no_file_arg_allowed(self):
        ctx = _ctx("manager", "alice")
        d = self._decision("ls | head -n 5", ctx)
        assert d.allowed, f"denied: {d.reason}"

    def test_head_c_N_with_real_file_validates_file_path(self):
        """`head -c 100 file.txt` — N is the value, file.txt is the path
        and must still be validated against the role's read scope."""
        ctx = _ctx("manager", "alice")
        d = self._decision("head -c 100 /users/alice/workspace/x.txt", ctx)
        assert d.allowed, f"denied: {d.reason}"

    def test_head_c_N_with_out_of_scope_file_still_denied(self):
        """Regression guard — the fix must not also disable the path check.
        Reading another user's file is still denied."""
        ctx = _ctx("manager", "alice")
        d = self._decision("head -c 100 /users/bob/workspace/x.txt", ctx)
        assert not d.allowed

    def test_head_long_form_flag_separate_value(self):
        """`head --bytes 8` — long-form separated value also skipped."""
        ctx = _ctx("manager", "alice")
        d = self._decision("echo abc | head --bytes 8", ctx)
        assert d.allowed, f"denied: {d.reason}"

    def test_head_long_form_flag_fused_value(self):
        """`head --bytes=8` — fused form already worked (starts with `-`)."""
        ctx = _ctx("manager", "alice")
        d = self._decision("echo abc | head --bytes=8", ctx)
        assert d.allowed, f"denied: {d.reason}"

    def test_tail_n_N_allowed(self):
        ctx = _ctx("manager", "alice")
        d = self._decision("cat /users/alice/workspace/log.txt | tail -n 100", ctx)
        assert d.allowed, f"denied: {d.reason}"

    def test_tree_L_depth_allowed(self):
        ctx = _ctx("manager", "alice")
        d = self._decision("tree -L 3 /users/alice/workspace", ctx)
        assert d.allowed, f"denied: {d.reason}"

    def test_du_max_depth_allowed(self):
        ctx = _ctx("manager", "alice")
        d = self._decision("du -d 2 /users/alice/workspace", ctx)
        assert d.allowed, f"denied: {d.reason}"

    def test_ls_width_value_not_treated_as_path(self):
        """`ls -w 80` — `80` is column count, not a path."""
        ctx = _ctx("manager", "alice")
        d = self._decision("ls -w 80 /users/alice/workspace", ctx)
        assert d.allowed, f"denied: {d.reason}"

"""Unit tests for core/sandbox/sandbox.py — SandboxBuilder and helpers."""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from core.sandbox.sandbox import (
    SandboxBuilder,
    SandboxConfig,
    SandboxMount,
    ensure_persistent_claude_dir,
    resolve_sandbox_config,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_agents(tmp_path):
    """Create a temporary agents directory with a sample agent."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    # Create agent structure
    pa = agents_dir / "personal-assistant"
    (pa / "config" / "context").mkdir(parents=True)
    (pa / "workspace").mkdir(parents=True)
    (pa / "users" / "alice" / "workspace").mkdir(parents=True)
    (pa / "users" / "alice" / "context").mkdir(parents=True)
    (pa / "users" / "bob" / "workspace").mkdir(parents=True)

    # Create mcps dir
    mcps_dir = tmp_path / "mcps"
    (mcps_dir / "custom" / "schedules-mcp").mkdir(parents=True)
    (mcps_dir / "community" / "camoufox" / "screenshots").mkdir(parents=True)

    return agents_dir, mcps_dir


def _make_config(agents_dir, mcps_dir, role="manager", username="alice",
                 agent="personal-assistant", mcp_mounts=None,
                 mcp_dir_binds=None):
    """Helper to create a SandboxConfig."""
    claude_dir = agents_dir / agent / "users" / username / ".claude" if username else \
        agents_dir / agent / "workspace" / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    return SandboxConfig(
        role=role,
        username=username,
        agent_name=agent,
        is_admin_agent=False,
        host_agents_dir=agents_dir.resolve(),
        host_mcps_dir=mcps_dir.resolve(),
        host_claude_dir=claude_dir.resolve(),
        mcp_sandbox_mounts=mcp_mounts or [],
        mcp_dir_binds=mcp_dir_binds or [],
        # Isolation is always on: a resolved session carries at least the proxy
        # hook port, so build_command_prefix wraps + does not fail closed. The
        # value is cosmetic for these mount-only tests.
        net_forwards=["8400"],
    )


# ---------------------------------------------------------------------------
# SandboxBuilder tests
# ---------------------------------------------------------------------------

class TestSandboxBuilderViewer:
    def test_cwd(self, tmp_agents):
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="viewer")
        sb = SandboxBuilder(cfg)
        assert sb.get_cwd() == "/users/alice"

    def test_env_overrides(self, tmp_agents):
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="viewer")
        sb = SandboxBuilder(cfg)
        env = sb.get_env_overrides()
        assert env["CLAUDE_CONFIG_DIR"] == "/users/alice/.claude"

    def test_command_prefix_has_bwrap(self, tmp_agents):
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="viewer")
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude", "-p"])
        # Isolation is always on: the launcher wraps bwrap (argv[0] is the
        # launcher), bwrap appears after it, and the inner cmd is last.
        assert cmd[0].endswith("oto-sandbox-net")
        assert "bwrap" in cmd
        assert cmd[-2:] == ["claude", "-p"]

    def test_viewer_mounts_user_dir_and_ro_workspace_knowledge(self, tmp_agents):
        """Viewer mounts own user dir RW + knowledge + workspace RO.
        NO /config (owner-only)."""
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="viewer")
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude"])
        cmd_str = " ".join(cmd)
        # Viewer gets their user dir mounted (RW)
        assert "/users/alice" in cmd_str
        # Viewer gets /knowledge + /workspace (RO) but NOT /config
        assert "/knowledge" in cmd_str
        assert "/workspace" in cmd_str
        # Critical: /config must NOT be mounted for viewer.
        agent_dir = str(agents_dir / "personal-assistant")
        bind_pairs = list(zip(cmd, cmd[1:]))
        config_mounted = any(
            b == f"{agent_dir}/config" and a in ("--bind", "--ro-bind")
            for a, b in bind_pairs
        )
        assert not config_mounted, "viewer must not have /config mounted (owner-only)"

    def test_viewer_knowledge_mount_is_readonly(self, tmp_agents):
        """Viewer's /knowledge mount uses --ro-bind."""
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="viewer")
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude"])
        agent_dir = str(agents_dir / "personal-assistant")
        bind_pairs = list(zip(cmd, cmd[1:]))
        knowledge_ro = any(
            a == "--ro-bind" and b == f"{agent_dir}/knowledge"
            for a, b in bind_pairs
        )
        assert knowledge_ro


class TestSandboxBuilderEditor:
    """New tier between viewer and manager."""

    def test_editor_workspace_is_rw(self, tmp_agents):
        """Editor can WRITE to workspace (collaborative tier)."""
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="editor")
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude"])
        agent_dir = str(agents_dir / "personal-assistant")
        bind_pairs = list(zip(cmd, cmd[1:]))
        ws_rw = any(
            a == "--bind" and b == f"{agent_dir}/workspace"
            for a, b in bind_pairs
        )
        assert ws_rw

    def test_editor_config_not_mounted(self, tmp_agents):
        """Editor has NO /config mount (owner-only — config shapes
        agent behavior, that's owner curation not workspace collaboration)."""
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="editor")
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude"])
        agent_dir = str(agents_dir / "personal-assistant")
        bind_pairs = list(zip(cmd, cmd[1:]))
        config_mounted = any(
            b == f"{agent_dir}/config" and a in ("--bind", "--ro-bind")
            for a, b in bind_pairs
        )
        assert not config_mounted, "editor must not have /config mounted (owner-only)"

    def test_editor_knowledge_is_ro(self, tmp_agents):
        """Editor reads /knowledge but cannot write (owner-only)."""
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="editor")
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude"])
        agent_dir = str(agents_dir / "personal-assistant")
        bind_pairs = list(zip(cmd, cmd[1:]))
        knowledge_ro = any(
            a == "--ro-bind" and b == f"{agent_dir}/knowledge"
            for a, b in bind_pairs
        )
        assert knowledge_ro
        knowledge_rw = any(
            a == "--bind" and b == f"{agent_dir}/knowledge"
            for a, b in bind_pairs
        )
        assert not knowledge_rw

    def test_editor_user_dir_root_ro_subdirs_rw(self, tmp_agents):
        """Editor's user dir: ROOT is RO, workspace/ + context/ + CLI state
        dirs stack RW on top (stray root-level files are kernel-denied)."""
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="editor")
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude"])
        agent_dir = str(agents_dir / "personal-assistant")
        bind_pairs = list(zip(cmd, cmd[1:]))
        assert any(
            a == "--ro-bind" and b == f"{agent_dir}/users/alice"
            for a, b in bind_pairs
        ), "user dir root must be RO"
        assert not any(
            a == "--bind" and b == f"{agent_dir}/users/alice"
            for a, b in bind_pairs
        ), "user dir root must not be RW"
        for sub in ("workspace", "context", ".claude"):
            assert any(
                a == "--bind" and b == f"{agent_dir}/users/alice/{sub}"
                for a, b in bind_pairs
            ), f"users/alice/{sub} must be RW"
        # .codex doesn't exist on disk in this fixture → no bind emitted.
        assert f"{agent_dir}/users/alice/.codex" not in " ".join(cmd)

    def test_user_dir_rw_subdirs_ordered_after_ro_root(self, tmp_agents):
        """The RW subdir binds must come AFTER the RO root bind in argv —
        bwrap applies mounts in order, later mounts shadow earlier ones."""
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="manager")
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude"])
        agent_dir = str(agents_dir / "personal-assistant")
        root_idx = cmd.index(f"{agent_dir}/users/alice")
        ws_idx = cmd.index(f"{agent_dir}/users/alice/workspace")
        assert root_idx < ws_idx


class TestSandboxBuilderManager:
    def test_cwd(self, tmp_agents):
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="manager")
        sb = SandboxBuilder(cfg)
        assert sb.get_cwd() == "/users/alice"

    def test_manager_has_config_workspace_knowledge_user(self, tmp_agents):
        """Manager gets RW everywhere — config + knowledge + workspace + own user dir."""
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="manager")
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude"])
        cmd_str = " ".join(cmd)
        assert "/config" in cmd_str
        assert "/knowledge" in cmd_str
        assert "/workspace" in cmd_str
        assert "/users/alice" in cmd_str
        # All mounts should be RW (--bind) for manager
        agent_dir = str(agents_dir / "personal-assistant")
        bind_pairs = list(zip(cmd, cmd[1:]))
        for dirname in ("config", "knowledge", "workspace"):
            assert any(
                a == "--bind" and b == f"{agent_dir}/{dirname}"
                for a, b in bind_pairs
            ), f"manager should have RW --bind for {dirname}"

    def test_manager_no_other_users(self, tmp_agents):
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="manager", username="alice")
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude"])
        cmd_str = " ".join(cmd)
        assert "/users/bob" not in cmd_str


class TestSandboxBuilderAgentTask:
    def test_cwd(self, tmp_agents):
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="manager", username="")
        sb = SandboxBuilder(cfg)
        assert sb.get_cwd() == "/workspace"

    def test_workspace_and_knowledge_mounted(self, tmp_agents):
        """Agent-scope sessions get /workspace RW + /knowledge RO."""
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="manager", username="")
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude"])
        cmd_str = " ".join(cmd)
        assert "/workspace" in cmd_str
        assert "/knowledge" in cmd_str
        # Should not have /config or /users (agent-scope has no user dir,
        # no config — those are owner/user-tier resources)
        agent_dir = str(agents_dir / "personal-assistant")
        assert f"{agent_dir}/config" not in cmd_str
        assert f"{agent_dir}/users" not in cmd_str
        # Knowledge must be RO (not writable) for agent-scope sessions.
        bind_pairs = list(zip(cmd, cmd[1:]))
        knowledge_ro = any(
            a == "--ro-bind" and b == f"{agent_dir}/knowledge"
            for a, b in bind_pairs
        )
        assert knowledge_ro


class TestSandboxBuilderMCPs:
    def test_assigned_mcp_dirs_identity_mounted(self, tmp_agents):
        """Only the session's mcp_dir_binds are bound — identity, RO."""
        agents_dir, mcps_dir = tmp_agents
        task_dir = str((mcps_dir / "custom" / "schedules-mcp").resolve())
        cfg = _make_config(agents_dir, mcps_dir, mcp_dir_binds=[task_dir])
        cmd = SandboxBuilder(cfg).build_command_prefix(["claude"])
        assert task_dir in cmd  # exact arg → identity bind present
        # The OTHER (unassigned) MCP dir must not be bound.
        camoufox = str((mcps_dir / "community" / "camoufox").resolve())
        assert camoufox not in cmd

    def test_mcps_tree_never_mounted(self, tmp_agents):
        """The mcps/ ROOT is never bound — an agent must not see the code /
        config / data (e.g. another MCP's keys) of MCPs it isn't assigned."""
        agents_dir, mcps_dir = tmp_agents
        task_dir = str((mcps_dir / "custom" / "schedules-mcp").resolve())
        for binds in ([], [task_dir]):
            cfg = _make_config(agents_dir, mcps_dir, mcp_dir_binds=binds)
            cmd = SandboxBuilder(cfg).build_command_prefix(["claude"])
            # Exact-arg check: subdir binds contain the root as a PREFIX, so a
            # substring assert would false-positive.
            assert str(mcps_dir.resolve()) not in cmd

    def test_mcp_dir_binds_from_config_json_and_toml(self, tmp_agents, tmp_path, monkeypatch):
        """Derivation scans the session's generated config (either format) for
        MCPS_DIR-prefixed paths and returns unique existing dir roots +
        .uv-python; nonexistent dirs are dropped."""
        import config as app_config
        from core.sandbox.sandbox import mcp_dir_binds_from_config

        _, mcps_dir = tmp_agents
        monkeypatch.setattr(app_config, "MCPS_DIR", mcps_dir)
        (mcps_dir / ".uv-python").mkdir()
        task_dir = str((mcps_dir / "custom" / "schedules-mcp").resolve())
        root = str(mcps_dir.resolve())

        cfg_json = tmp_path / "mcp-config.json"
        cfg_json.write_text(
            '{"mcpServers": {"schedules-mcp": {"command": "%s/venv/bin/python", '
            '"args": ["%s/server.py", "%s/community/ghost-mcp/x.py"]}}}'
            % (task_dir, task_dir, root)
        )
        binds = mcp_dir_binds_from_config(cfg_json)
        assert task_dir in binds
        assert str(mcps_dir / ".uv-python") in binds
        assert not any("ghost-mcp" in b for b in binds)  # nonexistent → dropped
        assert binds.count(task_dir) == 1                # deduped

        cfg_toml = tmp_path / "config.toml"
        cfg_toml.write_text(
            f'[mcp_servers.schedules-mcp]\ncommand = "{task_dir}/venv/bin/python"\n'
            f'args = ["{task_dir}/server.py"]\n'
        )
        assert task_dir in mcp_dir_binds_from_config(cfg_toml)

        # No config / missing file → no binds (fail closed).
        assert mcp_dir_binds_from_config(None) == []
        assert mcp_dir_binds_from_config(tmp_path / "missing.json") == []

    def test_conditional_mcp_mount(self, tmp_agents):
        agents_dir, mcps_dir = tmp_agents
        screenshots_dir = str(mcps_dir / "community" / "camoufox" / "screenshots")
        mounts = [SandboxMount(host=screenshots_dir, sandbox="/screenshots", mode="rw")]
        cfg = _make_config(agents_dir, mcps_dir, mcp_mounts=mounts)
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude"])
        cmd_str = " ".join(cmd)
        assert "/screenshots" in cmd_str
        assert screenshots_dir in cmd_str

    def test_mount_dest_overlaying_protected_path_refused(self, tmp_agents):
        # S1 regression: a manifest mount must NOT overlay the permission-gate
        # hook (or any .claude/.codex/system/shared dest) — conditional mounts
        # run last, so a bind there would shadow the real one and disable gating.
        agents_dir, mcps_dir = tmp_agents
        evil = mcps_dir / "community" / "camoufox" / "evil.py"
        evil.write_text("# noop gate")
        for dest in (
            "/workspace/.claude/permission_gate.py",
            "/users/alice/.claude/settings.json",
            "/workspace/.codex/config.toml",
            "/etc/hosts", "/proc/1/environ", "/config/x", "/knowledge/y",
            "/workspace", "/users", "/",
        ):
            mounts = [SandboxMount(host=str(evil), sandbox=dest, mode="ro")]
            cfg = _make_config(agents_dir, mcps_dir, mcp_mounts=mounts)
            cmd_str = " ".join(SandboxBuilder(cfg).build_command_prefix(["claude"]))
            # The malicious bind must be absent — assert via the unique evil host
            # path (the dest strings like /workspace also appear in legit mounts).
            assert str(evil) not in cmd_str, f"protected dest mounted: {dest}"

    def test_mount_host_outside_agent_mcps_tree_refused(self, tmp_agents, tmp_path):
        # S2 regression: a manifest must NOT bind a host outside the agent / mcps
        # tree (e.g. the platform root holding config.env + sessions/), nor
        # another agent's tree.
        agents_dir, mcps_dir = tmp_agents
        secret = tmp_path / "config.env"
        secret.write_text("JWT_SECRET=x")
        other = agents_dir / "other-agent" / "workspace" / "secret.txt"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("x")
        for host in (str(secret), str(other)):
            mounts = [SandboxMount(host=host, sandbox="/workspace/leak", mode="ro")]
            cfg = _make_config(agents_dir, mcps_dir, mcp_mounts=mounts)
            cmd_str = " ".join(SandboxBuilder(cfg).build_command_prefix(["claude"]))
            assert host not in cmd_str, f"out-of-tree host mounted: {host}"


class TestSandboxBuilderNamespaceFlags:
    def test_has_required_flags(self, tmp_agents):
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir)
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude"])
        assert "--unshare-pid" in cmd
        assert "--die-with-parent" in cmd
        assert "--share-net" in cmd


# ---------------------------------------------------------------------------
# ensure_persistent_claude_dir tests
# ---------------------------------------------------------------------------

class TestEnsurePersistentClaudeDir:
    def test_creates_user_dir(self, tmp_agents, monkeypatch):
        agents_dir, _ = tmp_agents
        import config as app_config
        monkeypatch.setattr(app_config, "AGENTS_DIR", agents_dir)
        monkeypatch.setattr(app_config, "BASE_DIR", agents_dir.parent / "proxy")
        # Create proxy/hooks dir
        hooks_dir = agents_dir.parent / "proxy" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "permission_gate.py").write_text("# gate")
        (hooks_dir / "tool_result_forwarder.py").write_text("# forwarder")

        result = ensure_persistent_claude_dir(
            "personal-assistant", username="alice", scope="user",
        )
        assert result.exists()
        assert (result / "settings.json").exists()
        assert (result / "permission_gate.py").exists()
        assert (result / "tool_result_forwarder.py").exists()
        assert "personal-assistant/users/alice/.claude" in str(result)

    def test_creates_agent_scope_dir(self, tmp_agents, monkeypatch):
        agents_dir, _ = tmp_agents
        import config as app_config
        monkeypatch.setattr(app_config, "AGENTS_DIR", agents_dir)
        monkeypatch.setattr(app_config, "BASE_DIR", agents_dir.parent / "proxy")
        hooks_dir = agents_dir.parent / "proxy" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "permission_gate.py").write_text("# gate")
        (hooks_dir / "tool_result_forwarder.py").write_text("# forwarder")

        result = ensure_persistent_claude_dir(
            "personal-assistant", username="", scope="agent",
        )
        assert "personal-assistant/workspace/.claude" in str(result)

    def test_idempotent(self, tmp_agents, monkeypatch):
        agents_dir, _ = tmp_agents
        import config as app_config
        monkeypatch.setattr(app_config, "AGENTS_DIR", agents_dir)
        monkeypatch.setattr(app_config, "BASE_DIR", agents_dir.parent / "proxy")
        hooks_dir = agents_dir.parent / "proxy" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "permission_gate.py").write_text("# gate")
        (hooks_dir / "tool_result_forwarder.py").write_text("# forwarder")

        r1 = ensure_persistent_claude_dir("personal-assistant", username="alice")
        r2 = ensure_persistent_claude_dir("personal-assistant", username="alice")
        assert r1 == r2

    def test_settings_disables_inner_sandbox(self, tmp_agents, monkeypatch):
        """The platform's outer bwrap is the security boundary; Claude Code's
        own bwrap layer must be off so we don't nest sandboxes (which has
        broken on Ubuntu 24.04 and during 2.1.x rollouts).
        """
        import json

        agents_dir, _ = tmp_agents
        import config as app_config
        monkeypatch.setattr(app_config, "AGENTS_DIR", agents_dir)
        monkeypatch.setattr(app_config, "BASE_DIR", agents_dir.parent / "proxy")
        hooks_dir = agents_dir.parent / "proxy" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "permission_gate.py").write_text("# gate")
        (hooks_dir / "tool_result_forwarder.py").write_text("# forwarder")

        result = ensure_persistent_claude_dir(
            "personal-assistant", username="alice", scope="user",
        )

        data = json.loads((result / "settings.json").read_text())
        assert data["sandbox"]["enabled"] is False
        assert data["sandbox"]["failIfUnavailable"] is False
        # Hooks must remain alongside sandbox config
        assert "PreToolUse" in data["hooks"]
        assert "PostToolUse" in data["hooks"]
        # Permission deny list — covers all three settings.json build paths
        # via the canonical constant in core/sandbox/sandbox.py.
        denied = set(data["permissions"]["deny"])
        # claude.ai integrations that collide with platform features
        for t in ("RemoteTrigger", "CronCreate", "CronDelete", "CronList",
                  "PushNotification", "ScheduleWakeup"):
            assert t in denied, f"{t} should be in permissions.deny"
        # Personal-account claude.ai MCPs
        for t in ("mcp__claude_ai_Gmail__authenticate",
                  "mcp__claude_ai_Google_Calendar__authenticate",
                  "mcp__claude_ai_Google_Drive__authenticate"):
            assert t in denied
        # Task* family is intentionally KEPT — useful session-internal todo
        for t in ("TaskCreate", "TaskUpdate", "TaskList",
                  "TaskGet", "TaskOutput", "TaskStop"):
            assert t not in denied, f"{t} should NOT be in deny list"


class TestCopyHookLf:
    """Hook scripts MUST land in the sandbox with LF endings + the exec bit.

    A CRLF shebang (``#!/usr/bin/env python3\\r``) makes the kernel look for an
    interpreter literally named ``python3\\r`` → the hook fails to start
    (``/usr/bin/env: 'python3\\r': No such file or directory``) and silently
    bypasses enforcement (observed live after a CRLF crept into a hook source).
    """

    def test_strips_crlf_and_sets_executable(self, tmp_path):
        from core.sandbox.sandbox import _copy_hook_lf
        src = tmp_path / "h.py"
        src.write_bytes(b"#!/usr/bin/env python3\r\nimport os\r\nos.getpid()\r\n")
        dst = tmp_path / "out.py"
        _copy_hook_lf(src, dst)
        data = dst.read_bytes()
        assert b"\r" not in data
        assert data.startswith(b"#!/usr/bin/env python3\n")
        assert os.access(dst, os.X_OK)

    def test_lf_source_unchanged(self, tmp_path):
        from core.sandbox.sandbox import _copy_hook_lf
        src = tmp_path / "h.py"
        body = b"#!/usr/bin/env python3\nprint(1)\n"
        src.write_bytes(body)
        dst = tmp_path / "out.py"
        _copy_hook_lf(src, dst)
        assert dst.read_bytes() == body
        assert os.access(dst, os.X_OK)


class TestDisallowedBuiltinsConstant:
    """The deny list is a single source of truth in core/sandbox/sandbox.py and
    is wired into all three settings.json build paths. These tests guard
    the constant + the symmetry across builders."""

    def test_constant_contains_critical_entries(self):
        from core.sandbox.sandbox import _DISALLOWED_BUILTIN_TOOLS
        critical = {
            "RemoteTrigger", "CronCreate", "CronDelete", "CronList",
            "PushNotification", "ScheduleWakeup",
            "mcp__claude_ai_Gmail__authenticate",
            "mcp__claude_ai_Gmail__complete_authentication",
            "mcp__claude_ai_Google_Calendar__authenticate",
            "mcp__claude_ai_Google_Calendar__complete_authentication",
            "mcp__claude_ai_Google_Drive__authenticate",
            "mcp__claude_ai_Google_Drive__complete_authentication",
        }
        assert critical.issubset(set(_DISALLOWED_BUILTIN_TOOLS))

    def test_constant_does_not_contain_kept_tools(self):
        from core.sandbox.sandbox import _DISALLOWED_BUILTIN_TOOLS
        kept = {
            # Task* family (session-internal todo, kept on purpose)
            "TaskCreate", "TaskUpdate", "TaskList",
            "TaskGet", "TaskOutput", "TaskStop",
            # Plan-mode + worktree + monitor (useful platform features)
            "EnterPlanMode", "ExitPlanMode",
            "EnterWorktree", "ExitWorktree", "Monitor",
            # Web access + asking + Jupyter — all benign
            "WebFetch", "WebSearch", "AskUserQuestion", "NotebookEdit",
            # Core editor tools
            "Bash", "Read", "Edit", "Write", "Glob", "Grep",
            # Skill: allowed since 2026-07 — the activation surface for
            # platform-managed on-demand skills (skills_materializer);
            # the no-parallel-memory guarantee moved to reconciliation.
            "Skill",
        }
        denied = set(_DISALLOWED_BUILTIN_TOOLS)
        for t in kept:
            assert t not in denied, f"{t} must remain available"

    def test_sandbox_cli_settings_uses_same_list(self):
        from core.sandbox.sandbox import _build_sandbox_cli_settings, _DISALLOWED_BUILTIN_TOOLS
        settings = _build_sandbox_cli_settings("/users/test/.claude")
        assert set(settings["permissions"]["deny"]) == set(_DISALLOWED_BUILTIN_TOOLS)

    def test_sandbox_cli_settings_disables_plugins(self):
        """Platform is the only skill source: with the Skill tool allowed,
        plugin skills must not activate outside install/approval — the
        always-rewritten settings.json keeps every plugin off."""
        from core.sandbox.sandbox import _build_sandbox_cli_settings
        settings = _build_sandbox_cli_settings("/users/test/.claude")
        assert settings["enabledPlugins"] == {}


# ---------------------------------------------------------------------------
# resolve_sandbox_config tests
# ---------------------------------------------------------------------------

class TestResolveSandboxConfig:
    def test_returns_correct_config(self, tmp_agents):
        agents_dir, mcps_dir = tmp_agents
        claude_dir = agents_dir / "personal-assistant" / "users" / "alice" / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)

        cfg = resolve_sandbox_config(
            role="manager",
            username="alice",
            agent_name="personal-assistant",
            is_admin_agent=False,
            host_claude_dir=claude_dir,
        )
        assert cfg.role == "manager"
        assert cfg.username == "alice"
        assert cfg.host_claude_dir == claude_dir


# ---------------------------------------------------------------------------
# System RO binds — optional paths
# ---------------------------------------------------------------------------

class TestSystemROOptionalBinds:
    """/snap/bin is in _SYSTEM_RO_OPTIONAL so sandboxed agents
    can invoke snap-installed CLIs (gh, kubectl, ...) when the host has
    them. The existing os.path.exists() check makes this safe on hosts
    without snap (Docker, most servers)."""

    def test_snap_bin_in_optional_list(self):
        from core.sandbox.sandbox import _SYSTEM_RO_OPTIONAL
        assert "/snap/bin" in _SYSTEM_RO_OPTIONAL

    def test_optional_bind_mounted_when_path_exists(self, tmp_agents, monkeypatch):
        """When a path in _SYSTEM_RO_OPTIONAL exists on the host, it
        appears in the bwrap argv as a --ro-bind."""
        from core.sandbox import sandbox as sandbox_mod
        # Pretend /snap/bin exists on this host.
        real_exists = os.path.exists
        monkeypatch.setattr(
            sandbox_mod.os.path, "exists",
            lambda p: True if p == "/snap/bin" else real_exists(p),
        )

        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir)
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude"])

        # --ro-bind /snap/bin /snap/bin should appear as a pair.
        joined = " ".join(cmd)
        assert "--ro-bind /snap/bin /snap/bin" in joined

    def test_optional_bind_skipped_when_path_missing(self, tmp_agents, monkeypatch):
        """When the optional path does NOT exist on the host, it is
        absent from the bwrap argv — no error."""
        from core.sandbox import sandbox as sandbox_mod
        real_exists = os.path.exists
        monkeypatch.setattr(
            sandbox_mod.os.path, "exists",
            lambda p: False if p == "/snap/bin" else real_exists(p),
        )

        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir)
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude"])

        joined = " ".join(cmd)
        assert "/snap/bin" not in joined


# ---------------------------------------------------------------------------
# System RO files — /etc/gitconfig
# ---------------------------------------------------------------------------

class TestSystemROEtcGitconfig:
    """`/etc/gitconfig` is mounted RO into the sandbox so `git` sees the
    system credential helper config (`install-baseline-tools.sh` wires
    `/usr/local/bin/oto-git-credential-helper` as github.com's helper).
    Without this mount, `git push` falls back to interactive prompts even
    when GH_TOKEN is set."""

    def test_etc_gitconfig_in_ro_files_list(self):
        from core.sandbox.sandbox import _SYSTEM_RO_FILES
        assert "/etc/gitconfig" in _SYSTEM_RO_FILES

    def test_etc_gitconfig_mounted_when_present(self, tmp_agents, monkeypatch):
        from core.sandbox import sandbox as sandbox_mod
        real_exists = os.path.exists
        real_isdir = os.path.isdir
        monkeypatch.setattr(
            sandbox_mod.os.path, "exists",
            lambda p: True if p == "/etc/gitconfig" else real_exists(p),
        )
        monkeypatch.setattr(
            sandbox_mod.os.path, "isdir",
            lambda p: False if p == "/etc/gitconfig" else real_isdir(p),
        )

        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir)
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude"])

        joined = " ".join(cmd)
        assert "--ro-bind /etc/gitconfig /etc/gitconfig" in joined

    def test_etc_gitconfig_skipped_when_missing(self, tmp_agents, monkeypatch):
        from core.sandbox import sandbox as sandbox_mod
        real_exists = os.path.exists
        monkeypatch.setattr(
            sandbox_mod.os.path, "exists",
            lambda p: False if p == "/etc/gitconfig" else real_exists(p),
        )

        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir)
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude"])

        joined = " ".join(cmd)
        assert "/etc/gitconfig" not in joined


# ---------------------------------------------------------------------------
# Network namespace isolation (OTODOCK_SANDBOX_NETNS)
# ---------------------------------------------------------------------------

import shutil as _shutil

import config as _app_config
from core.sandbox import sandbox as _sandbox_mod


def _netns_cfg(agents_dir, mcps_dir, *, forwards, allow_hosts=None,
               role="manager", username="alice", agent="personal-assistant"):
    """SandboxConfig with an explicit egress set (bypasses the registry
    resolver so these stay pure build-only unit tests)."""
    base = _make_config(agents_dir, mcps_dir, role=role, username=username,
                        agent=agent)
    return SandboxConfig(
        role=base.role, username=base.username, agent_name=base.agent_name,
        is_admin_agent=base.is_admin_agent, host_agents_dir=base.host_agents_dir,
        host_mcps_dir=base.host_mcps_dir, host_claude_dir=base.host_claude_dir,
        mcp_sandbox_mounts=base.mcp_sandbox_mounts,
        net_forwards=list(forwards),
        net_allow_hosts=list(allow_hosts or []),
    )


class TestNetnsAlwaysOn:
    """Isolation is always on: every resolved session is launcher-wrapped."""

    def test_launcher_prefix_shape(self, tmp_agents):
        agents_dir, mcps_dir = tmp_agents
        cfg = _netns_cfg(agents_dir, mcps_dir, forwards=["8400", "8931", "8932"])
        cmd = SandboxBuilder(cfg).build_command_prefix(["claude", "-p"])

        # argv[0] is the launcher, not bwrap.
        assert cmd[0].endswith("oto-sandbox-net")
        assert "--block-private" in cmd

        # One --forward per port, in the order given.
        fwd_idx = [i for i, a in enumerate(cmd) if a == "--forward"]
        assert [cmd[i + 1] for i in fwd_idx] == ["8400", "8931", "8932"]

        # Structure: launcher … -- bwrap … -- claude -p
        sep = cmd.index("--")
        assert cmd[sep + 1] == "bwrap"
        assert cmd[-2:] == ["claude", "-p"]
        # Postgres is never forwarded.
        assert "5432" not in [cmd[i + 1] for i in fwd_idx]

    def test_allow_hosts_emitted(self, tmp_agents):
        """Routable carve-outs become one --allow-host per entry."""
        agents_dir, mcps_dir = tmp_agents
        cfg = _netns_cfg(
            agents_dir, mcps_dir, forwards=["8400"],
            allow_hosts=["192.168.1.10", "10.200.0.5", "fd00::5"],
        )
        cmd = SandboxBuilder(cfg).build_command_prefix(["claude"])
        ah_idx = [i for i, a in enumerate(cmd) if a == "--allow-host"]
        assert [cmd[i + 1] for i in ah_idx] == ["192.168.1.10", "10.200.0.5", "fd00::5"]

    def test_empty_forwards_fails_closed(self, tmp_agents):
        """An empty egress set is a build error — refuse to launch rather than
        run the agent un-isolated OR netns-wrapped without the hook port."""
        agents_dir, mcps_dir = tmp_agents
        cfg = _netns_cfg(agents_dir, mcps_dir, forwards=[])
        with pytest.raises(RuntimeError, match="net_forwards"):
            SandboxBuilder(cfg).build_command_prefix(["claude"])

    def test_uid_mapback_present_on_rootless(self, tmp_agents, monkeypatch):
        """Non-root proxy (rootless pasta) → bwrap maps the agent back to the
        proxy uid/gid so the in-sandbox identity is byte-identical to root."""
        monkeypatch.setattr(_sandbox_mod.os, "getuid", lambda: 1000)
        monkeypatch.setattr(_sandbox_mod.os, "getgid", lambda: 1000)
        agents_dir, mcps_dir = tmp_agents
        cfg = _netns_cfg(agents_dir, mcps_dir, forwards=["8400"])
        cmd = SandboxBuilder(cfg).build_command_prefix(["claude"])
        assert "--unshare-user" in cmd
        assert cmd[cmd.index("--uid") + 1] == "1000"
        assert cmd[cmd.index("--gid") + 1] == "1000"

    def test_no_uid_mapback_on_rootful_docker(self, tmp_agents, monkeypatch):
        """Root proxy (rootful pasta) → no userns nesting, so no map-back is
        emitted (bwrap already runs the agent as 0)."""
        monkeypatch.setattr(_sandbox_mod.os, "getuid", lambda: 0)
        monkeypatch.setattr(_sandbox_mod.os, "getgid", lambda: 0)
        agents_dir, mcps_dir = tmp_agents
        cfg = _netns_cfg(agents_dir, mcps_dir, forwards=["8400"])
        cmd = SandboxBuilder(cfg).build_command_prefix(["claude"])
        # Still launcher-wrapped, just without the uid flags.
        assert cmd[0].endswith("oto-sandbox-net")
        assert "--unshare-user" not in cmd

    def test_dns_forward_emitted_when_resolv_swap_exists(
        self, tmp_agents, monkeypatch, tmp_path,
    ):
        """When the stub-resolver swap file exists, the launcher gets
        --dns-forward and bwrap mounts the generated resolv.conf."""
        fake_resolv = tmp_path / "netns-resolv.conf"
        fake_resolv.write_text("nameserver 169.254.1.1\n")
        monkeypatch.setattr(_sandbox_mod, "netns_resolv_path", lambda: fake_resolv)

        agents_dir, mcps_dir = tmp_agents
        cfg = _netns_cfg(agents_dir, mcps_dir, forwards=["8400"])
        cmd = SandboxBuilder(cfg).build_command_prefix(["claude"])
        joined = " ".join(cmd)
        assert "--dns-forward" in cmd
        assert cmd[cmd.index("--dns-forward") + 1] == _sandbox_mod._NETNS_DNS_FORWARD_ADDR
        # The generated resolv.conf shadows the host's at /etc/resolv.conf.
        assert f"--ro-bind {fake_resolv} /etc/resolv.conf" in joined

    def test_no_dns_forward_without_resolv_swap(self, tmp_agents, monkeypatch):
        # Point the swap path at a definitely-missing file.
        monkeypatch.setattr(
            _sandbox_mod, "netns_resolv_path",
            lambda: _app_config.SESSIONS_DIR / "does-not-exist-netns-resolv.conf",
        )
        agents_dir, mcps_dir = tmp_agents
        cfg = _netns_cfg(agents_dir, mcps_dir, forwards=["8400"])
        cmd = SandboxBuilder(cfg).build_command_prefix(["claude"])
        assert "--dns-forward" not in cmd


class TestRootfulCapDropAndEnv:
    """Sandbox hardening: the agent always holds zero Linux capabilities
    (--cap-drop ALL, unconditional). IS_SANDBOX (the CLI's run-as-root guard
    bypass) stays gated on uid 0 so every non-root path keeps a byte-identical
    env — the norm, since no default topology runs the proxy as root."""

    def test_cap_drop_all_emitted_when_root(self, tmp_agents, monkeypatch):
        monkeypatch.setattr(_sandbox_mod.os, "getuid", lambda: 0)
        agents_dir, mcps_dir = tmp_agents
        cmd = SandboxBuilder(_make_config(agents_dir, mcps_dir)).build_command_prefix(["claude"])
        assert "--cap-drop" in cmd
        assert cmd[cmd.index("--cap-drop") + 1] == "ALL"
        # Caps dropped for the payload, before the inner command (the last `--`
        # separator — the first `--` now belongs to the netns launcher prefix).
        assert cmd.index("--cap-drop") < (len(cmd) - 1 - cmd[::-1].index("--"))

    def test_cap_drop_all_emitted_when_non_root(self, tmp_agents, monkeypatch):
        # Unconditional: the agent is capability-less on every path (rootless
        # bwrap included), so --cap-drop ALL is emitted at uid 1000 too.
        monkeypatch.setattr(_sandbox_mod.os, "getuid", lambda: 1000)
        agents_dir, mcps_dir = tmp_agents
        cmd = SandboxBuilder(_make_config(agents_dir, mcps_dir)).build_command_prefix(["claude"])
        assert "--cap-drop" in cmd
        assert cmd[cmd.index("--cap-drop") + 1] == "ALL"

    def test_is_sandbox_env_when_root(self, tmp_agents, monkeypatch):
        monkeypatch.setattr(_sandbox_mod.os, "getuid", lambda: 0)
        agents_dir, mcps_dir = tmp_agents
        env = SandboxBuilder(_make_config(agents_dir, mcps_dir)).get_env_overrides()
        assert env.get("IS_SANDBOX") == "1"

    def test_no_is_sandbox_env_when_non_root(self, tmp_agents, monkeypatch):
        monkeypatch.setattr(_sandbox_mod.os, "getuid", lambda: 1000)
        agents_dir, mcps_dir = tmp_agents
        env = SandboxBuilder(_make_config(agents_dir, mcps_dir)).get_env_overrides()
        assert "IS_SANDBOX" not in env


class TestNetnsPreflight:
    def test_hard_fails_when_pasta_missing(self, monkeypatch):
        # Isolation is mandatory — a missing tool hard-fails boot.
        real_which = _shutil.which
        monkeypatch.setattr(
            _sandbox_mod.shutil, "which",
            lambda tool: None if tool == "pasta" else real_which(tool),
        )
        with pytest.raises(RuntimeError, match="pasta"):
            _sandbox_mod.netns_preflight()

    def test_loopback_resolver_detection(self, monkeypatch, tmp_path):
        resolv = tmp_path / "resolv.conf"
        resolv.write_text("nameserver 127.0.0.53\noptions edns0\n")
        assert _sandbox_mod._host_resolv_has_loopback_ns(str(resolv)) is True

        resolv.write_text("nameserver 8.8.8.8\n")
        assert _sandbox_mod._host_resolv_has_loopback_ns(str(resolv)) is False

    def _failing_probe(self, monkeypatch):
        # All tools present; the `unshare -Urn true` capability probe fails.
        real_which = _shutil.which
        monkeypatch.setattr(_sandbox_mod.shutil, "which",
                            lambda tool: real_which(tool) or f"/usr/bin/{tool}")
        monkeypatch.setattr(_sandbox_mod.os, "access", lambda *a, **k: True)

        class _Probe:
            returncode = 1
            stderr = b"unshare: unshare failed: Operation not permitted"
        monkeypatch.setattr(_sandbox_mod.subprocess, "run",
                            lambda *a, **k: _Probe())

    def test_probe_failure_names_apparmor_restriction(self, monkeypatch, tmp_path):
        # Ubuntu 24.04+ (sysctl = 1): the error must name the sysctl and the
        # scoped-profile remedy, never suggest flipping the sysctl off.
        self._failing_probe(monkeypatch)
        sysctl = tmp_path / "apparmor_restrict_unprivileged_userns"
        sysctl.write_text("1\n")
        monkeypatch.setattr(_sandbox_mod, "_APPARMOR_USERNS_SYSCTL", sysctl)
        with pytest.raises(RuntimeError) as exc:
            _sandbox_mod.netns_preflight()
        msg = str(exc.value)
        assert "apparmor_restrict_unprivileged_userns" in msg
        assert "setup-apparmor-userns.sh" in msg
        assert "OTODOCK_APPARMOR_PROFILE" in msg
        assert "NOT set the sysctl to 0" in msg

    def test_probe_failure_generic_without_restriction(self, monkeypatch, tmp_path):
        # Sysctl absent (non-Ubuntu kernel): the generic message, no AppArmor blame.
        self._failing_probe(monkeypatch)
        monkeypatch.setattr(_sandbox_mod, "_APPARMOR_USERNS_SYSCTL",
                            tmp_path / "missing-sysctl")
        with pytest.raises(RuntimeError) as exc:
            _sandbox_mod.netns_preflight()
        msg = str(exc.value)
        assert "apparmor_restrict_unprivileged_userns" not in msg
        assert "cannot create" in msg


class TestResolveSandboxEgress:
    """Egress resolver — registry-derived (proxy port + Docker MCPs + targets).

    Returns ``(forwards, allow_hosts)``: loopback ports pasta -T-splices, and
    routable IPs carved /32·/128 out of the blackholes.
    """

    def _mk(self, name, runtime, port, url):
        from services.mcp.mcp_registry import McpManifest, ServerConfig
        m = McpManifest.__new__(McpManifest)
        m.name = name
        m.server = ServerConfig(runtime=runtime, transport="http",
                                port=port, url_template=url)
        m.mcp_dir = __import__("pathlib").Path("/tmp")
        m.network_targets = []        # not a homelab MCP
        m.placement = "any"
        return m

    def test_proxy_port_always_present_and_first(self, monkeypatch):
        from services.mcp import mcp_registry
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: [])
        forwards, allow = mcp_registry.resolve_sandbox_egress("any-agent")
        assert forwards == [str(_app_config.PORT)]
        assert allow == []

    def test_docker_mcp_ports_included_loopback_t1(self, monkeypatch):
        from services.mcp import mcp_registry
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)  # T1

        mcps = [
            self._mk("camoufox", "docker", 8931, "http://localhost:${port}"),
            self._mk("file-tools", "docker", 8932, "http://localhost:${port}"),
            self._mk("slack", "node", 0, ""),          # external stdio → no fwd
            self._mk("espo", "docker", 0, ""),         # docker but no port → skip
        ]
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: mcps)
        forwards, allow = mcp_registry.resolve_sandbox_egress("pa")
        assert forwards[0] == str(_app_config.PORT)    # proxy port first
        assert "8931" in forwards and "8932" in forwards
        assert "5432" not in forwards                  # never Postgres
        # External (non-docker) + portless docker contribute nothing.
        assert len([f for f in forwards if f != str(_app_config.PORT)]) == 2

    def test_extra_targets_carved_as_allow_hosts(self, monkeypatch):
        """A layer-supplied target URL (e.g. a Codex local-LLM endpoint) on a
        private IP is carved as an allow-host; a public one is skipped."""
        from services.mcp import mcp_registry
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: [])
        forwards, allow = mcp_registry.resolve_sandbox_egress(
            "pa", extra_targets=["http://192.168.1.50:11434/v1", "https://api.openai.com"],
        )
        assert "192.168.1.50" in allow          # private → carved
        assert "api.openai.com" not in allow    # public → not carved (resolves public)


# ---------------------------------------------------------------------------
# Integration: real launcher -> pasta -> shim -> bwrap chain.
# Skips unless pasta + bwrap are present (like the rest of the suite's
# bwrap-dependent paths). Self-contained: no proxy/DB/MCP containers needed.
# ---------------------------------------------------------------------------

import socket as _socket
import subprocess as _subprocess
import threading as _threading

_PASTA = _shutil.which("pasta")
_BWRAP = _shutil.which("bwrap")
_needs_netns = pytest.mark.skipif(
    not (_PASTA and _BWRAP),
    reason="requires pasta (passt) + bwrap on PATH",
)


def _free_port() -> int:
    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _accept_once(port: int, token: bytes) -> _threading.Thread:
    """A throwaway host TCP listener that sends `token` to the first client."""
    srv = _socket.socket()
    srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)

    def _run():
        try:
            conn, _ = srv.accept()
            conn.sendall(token)
            conn.close()
        except OSError:
            pass
        finally:
            srv.close()

    t = _threading.Thread(target=_run, daemon=True)
    t.start()
    return t


@_needs_netns
class TestNetnsIntegration:
    def test_chain_forwards_allowlisted_blocks_rest(self, tmp_agents):
        """End-to-end: a forwarded loopback port is reachable from inside the
        netns; an un-forwarded one and the metadata IP are not."""
        agents_dir, mcps_dir = tmp_agents

        ok_port = _free_port()
        blocked_port = _free_port()
        _accept_once(ok_port, b"REACHED")
        _accept_once(blocked_port, b"LEAK")

        # Build the real launcher+bwrap argv via the production code path,
        # forwarding ONLY ok_port.
        cfg = _netns_cfg(agents_dir, mcps_dir, forwards=[str(ok_port)])

        probe = (
            "import socket,sys\n"
            "def chk(p):\n"
            "  s=socket.socket(); s.settimeout(2)\n"
            "  try:\n"
            "    s.connect(('127.0.0.1',p)); d=s.recv(16); s.close(); return d\n"
            "  except Exception as e: return b'ERR:'+str(e).encode()\n"
            f"sys.stdout.write('ok='+chk({ok_port}).decode(errors='replace')+'\\n')\n"
            f"sys.stdout.write('blocked='+chk({blocked_port}).decode(errors='replace')+'\\n')\n"
            "import subprocess\n"
            "r=subprocess.run(['ip','route','get','169.254.169.254'],"
            "capture_output=True,text=True)\n"
            "sys.stdout.write('meta_rc='+str(r.returncode)+'\\n')\n"
        )
        cmd = SandboxBuilder(cfg).build_command_prefix(
            ["python3", "-c", probe]
        )
        assert cmd[0].endswith("oto-sandbox-net")

        out = _subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        stdout = out.stdout

        # Forwarded port reached the host listener through pasta -T.
        assert "ok=REACHED" in stdout, (stdout, out.stderr)
        # Un-forwarded loopback port did NOT (connection refused/timeout).
        assert "blocked=ERR:" in stdout, (stdout, out.stderr)
        # Metadata IP is unrouteable (ip route get returns non-zero).
        assert "meta_rc=0" not in stdout, (stdout, out.stderr)


# ---------------------------------------------------------------------------
# cli_install_ro_binds — CLI binaries installed outside the system mounts
# ---------------------------------------------------------------------------

class TestCliInstallRoBinds:
    """A user-prefix npm CLI (~/.npm-global) must be mounted into the sandbox
    or bwrap can't exec it — the T1 native-install `bwrap: execvp claude` bug.
    Shared by the CLI and Codex layers."""

    def test_bare_name_needs_no_mount(self):
        from core.sandbox.sandbox import cli_install_ro_binds
        # PATH-resolved inside the sandbox (system dirs are always bound).
        assert cli_install_ro_binds("claude") == []
        assert cli_install_ro_binds("") == []

    def test_npm_shim_mounts_bin_and_package_root(self, tmp_path):
        from core.sandbox.sandbox import cli_install_ro_binds
        pkg = tmp_path / "lib" / "node_modules" / "some-cli"
        (pkg / "bin").mkdir(parents=True)
        real = pkg / "bin" / "cli.js"
        real.write_text("// shim target")
        bindir = tmp_path / "bin"
        bindir.mkdir()
        shim = bindir / "some-cli"
        shim.symlink_to(real)
        out = cli_install_ro_binds(str(shim))
        assert out == [str(bindir), str(pkg)]

    def test_native_binary_mounts_only_its_dir(self, tmp_path):
        from core.sandbox.sandbox import cli_install_ro_binds
        bindir = tmp_path / "bin"
        bindir.mkdir()
        elf = bindir / "native-cli"
        elf.write_bytes(b"\x7fELF")
        assert cli_install_ro_binds(str(elf)) == [str(bindir)]

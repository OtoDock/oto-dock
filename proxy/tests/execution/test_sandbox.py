"""Unit tests for core/sandbox/sandbox.py — SandboxBuilder and helpers."""

import os

import pytest

from core.layers.cli.config_dir import ensure_persistent_claude_dir
from core.sandbox.sandbox import (
    SandboxBuilder,
    SandboxConfig,
    SandboxMount,
    empty_mount_dir,
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

    def test_codex_inner_sandbox_mountpoints_precreated(self, tmp_agents):
        """Codex's nested bwrap tmpfs-mounts <cwd>/{.git,.agents,.codex};
        the RO user-dir root refuses mountpoint creation, so the build must
        pre-create .git and .agents as EMPTY dirs (.codex is
        session_config_dir's job). An empty .git must read as "not a git
        repository" — a bare .git dir must never make git (or
        init_if_missing) treat the user dir root as a repo."""
        import subprocess
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="manager")
        SandboxBuilder(cfg).build_command_prefix(["codex"])
        user_dir = agents_dir / "personal-assistant" / "users" / "alice"
        for sub in (".git", ".agents"):
            assert (user_dir / sub).is_dir(), f"{sub} mountpoint missing"
            assert not any((user_dir / sub).iterdir()), f"{sub} must be empty"
        r = subprocess.run(
            ["git", "-C", str(user_dir), "rev-parse", "--git-dir"],
            capture_output=True, text=True,
        )
        assert r.returncode != 0
        assert "not a git repository" in r.stderr.lower()
        assert "invalid gitfile" not in r.stderr.lower()


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

    def test_knowledge_credentials_are_masked_not_bound(self, tmp_agents):
        """No OAuth token file is mounted into a sandbox:
        a knowledge/.credentials copy in the tree is never bound, and the
        empty platform dir is bound read-only over it AFTER the knowledge
        root, so the copy is out of every agent-scope session's view."""
        from core.sandbox.sandbox import empty_mount_dir
        agents_dir, mcps_dir = tmp_agents
        agent_dir = str(agents_dir / "personal-assistant")
        cred_dir = agents_dir / "personal-assistant" / "knowledge" / ".credentials"
        cred_dir.mkdir(parents=True)
        (cred_dir / "google-tokens").mkdir()
        (cred_dir / "google-tokens" / "a@b.json").write_text("{}")
        for role, username, sandbox in (
            ("manager", "", "/knowledge/.credentials"),
            ("editor", "", "/knowledge/.credentials"),
            ("manager", "alice", "/knowledge/.credentials"),
        ):
            cfg = _make_config(agents_dir, mcps_dir, role=role, username=username)
            cmd = SandboxBuilder(cfg).build_command_prefix(["claude"])
            assert f"{agent_dir}/knowledge/.credentials" not in cmd, (role, username)
            bind_pairs = list(zip(cmd, cmd[1:], cmd[2:]))
            masks = [i for i, (a, b, c) in enumerate(bind_pairs)
                     if a == "--ro-bind" and b == str(empty_mount_dir()) and c == sandbox]
            assert len(masks) == 1, (role, username, cmd)
            assert cmd.index(f"{agent_dir}/knowledge") < masks[0], "the mask shadows the root"

    def test_user_credentials_are_masked_not_bound(self, tmp_agents):
        """The same for a person's users/<u>/.credentials: never bound RW,
        masked read-only over the read-only user root."""
        from core.sandbox.sandbox import empty_mount_dir
        agents_dir, mcps_dir = tmp_agents
        user_dir = agents_dir / "personal-assistant" / "users" / "alice"
        (user_dir / ".credentials" / "google-tokens").mkdir(parents=True)
        (user_dir / ".credentials" / "google-tokens" / "a@b.json").write_text("{}")
        cfg = _make_config(agents_dir, mcps_dir, role="editor", username="alice")
        cmd = SandboxBuilder(cfg).build_command_prefix(["claude"])
        assert f"{user_dir}/.credentials" not in cmd
        joined = " ".join(cmd)
        assert f"--ro-bind {empty_mount_dir()} /users/alice/.credentials" in joined
        assert joined.index(f"{user_dir} /users/alice") < joined.index("/users/alice/.credentials")

    def test_no_credentials_bind_when_dir_absent(self, tmp_agents):
        """No .credentials dir on disk (no bound account) → no bind emitted;
        bwrap cannot create a mountpoint under an RO parent."""
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="manager", username="")
        sb = SandboxBuilder(cfg)
        cmd = sb.build_command_prefix(["claude"])
        agent_dir = str(agents_dir / "personal-assistant")
        assert f"{agent_dir}/knowledge/.credentials" not in " ".join(cmd)

    def test_symlinked_credentials_dir_is_removed_never_bound(self, tmp_agents, tmp_path):
        """A link at knowledge/.credentials (plantable by an owner-tier session
        holding /knowledge RW) is never bound: bwrap resolves bind sources, so
        binding it would mount the link's TARGET into the next agent-scope
        sandbox. The link is removed and the build goes on without it, so a
        planted link cannot hold the whole agent out of every session."""
        agents_dir, mcps_dir = tmp_agents
        knowledge = agents_dir / "personal-assistant" / "knowledge"
        knowledge.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "escape-target"
        outside.mkdir()
        (outside / "token.json").write_text("{}")
        os.symlink(outside, knowledge / ".credentials")
        cfg = _make_config(agents_dir, mcps_dir, role="manager", username="")
        cmd = SandboxBuilder(cfg).build_command_prefix(["claude"])
        assert not (knowledge / ".credentials").is_symlink()
        assert not (knowledge / ".credentials").exists()
        assert str(outside) not in " ".join(cmd)
        assert (outside / "token.json").read_text() == "{}"

    def test_namespace_flags_isolate_ipc_and_uts(self, tmp_agents):
        """Every sandbox unshares IPC (else same-uid agents share a SysV/POSIX
        shm channel) and UTS."""
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, role="manager", username="")
        cmd = SandboxBuilder(cfg).build_command_prefix(["claude"])
        assert "--unshare-ipc" in cmd
        assert "--unshare-uts" in cmd
        assert "--unshare-pid" in cmd


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
        # Claude Code ≥ 2.1.275 would sync the pool account's claude.ai skills
        # and plugins into every session — both off (satellite twin asserts
        # the same in satellite/tests/test_cli_session.py).
        assert data["syncClaudeAiSkills"] is False
        assert data["syncClaudeAiPlugins"] is False
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
        from core.sandbox.session_config_dir import _copy_hook_lf
        src = tmp_path / "h.py"
        src.write_bytes(b"#!/usr/bin/env python3\r\nimport os\r\nos.getpid()\r\n")
        dst = tmp_path / "out.py"
        _copy_hook_lf(src, dst)
        data = dst.read_bytes()
        assert b"\r" not in data
        assert data.startswith(b"#!/usr/bin/env python3\n")
        assert os.access(dst, os.X_OK)

    def test_lf_source_unchanged(self, tmp_path):
        from core.sandbox.session_config_dir import _copy_hook_lf
        src = tmp_path / "h.py"
        body = b"#!/usr/bin/env python3\nprint(1)\n"
        src.write_bytes(body)
        dst = tmp_path / "out.py"
        _copy_hook_lf(src, dst)
        assert dst.read_bytes() == body
        assert os.access(dst, os.X_OK)


class TestDisallowedBuiltinsConstant:
    """The deny list is a single source of truth in core/layers/cli/config_dir.py
    (the Claude engine's own) and is wired into every settings.json build
    path and the satellite payload. These tests guard the constant + the
    symmetry across builders."""

    def test_constant_contains_critical_entries(self):
        from core.layers.cli.config_dir import DISALLOWED_BUILTIN_TOOLS
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
        assert critical.issubset(set(DISALLOWED_BUILTIN_TOOLS))

    def test_constant_does_not_contain_kept_tools(self):
        from core.layers.cli.config_dir import DISALLOWED_BUILTIN_TOOLS
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
        denied = set(DISALLOWED_BUILTIN_TOOLS)
        for t in kept:
            assert t not in denied, f"{t} must remain available"

    def test_sandbox_cli_settings_uses_same_list(self):
        from core.layers.cli.config_dir import build_settings, DISALLOWED_BUILTIN_TOOLS
        settings = build_settings("/users/test/.claude")
        assert set(settings["permissions"]["deny"]) == set(DISALLOWED_BUILTIN_TOOLS)

    def test_sandbox_cli_settings_disables_plugins(self):
        """Platform is the only skill source: with the Skill tool allowed,
        plugin skills must not activate outside install/approval — the
        always-rewritten settings.json keeps every plugin off."""
        from core.layers.cli.config_dir import DISABLED_BUILTIN_PLUGINS, build_settings
        settings = build_settings("/users/test/.claude")
        # Claude Code 2.1.287 ships built-in mods that an empty map leaves ON
        # (verified on 2.1.289: plugin-authoring loaded and its skill reached
        # the session): every built-in but the four the platform keeps is
        # switched off by name; 2.1.281 ignores the ids it does not know.
        assert settings["enabledPlugins"] == {p: False for p in DISABLED_BUILTIN_PLUGINS}
        for off in ("cc-plugin-plugin-authoring@builtin", "cc-plugin-you-should-know@builtin",
                    "cc-plugin-claude-test@builtin", "cc-plugin-mods-guide@builtin"):
            assert off in DISABLED_BUILTIN_PLUGINS
        # The four the platform keeps on are never in the disable map.
        for keep in ("cc-plugin-agents-md", "cc-plugin-diff", "cc-plugin-telemetry", "cc-plugin-sec-default"):
            assert not any(p.startswith(keep) for p in DISABLED_BUILTIN_PLUGINS)
        assert set(settings["permissions"]["deny"]) >= {"Artifact", "ArtifactComments", "ArtifactData", "ArtifactCheck"}

    def test_the_satellite_switches_off_the_same_builtins(self):
        """The satellite writes settings.json itself (cli_session._write_cli_hooks):
        its list of built-ins is the proxy's, read from the file as a literal."""
        import ast
        from tests._paths import REPO_ROOT
        from core.layers.cli.config_dir import DISABLED_BUILTIN_PLUGINS
        tree = ast.parse((REPO_ROOT / "satellite" / "sessions" / "cli_session.py").read_text(encoding="utf-8"))
        twin = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "DISABLED_BUILTIN_PLUGINS" for t in node.targets):
                twin = ast.literal_eval(node.value)
        assert twin == tuple(DISABLED_BUILTIN_PLUGINS)


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

    def test_launcher_prefix_shape(self, tmp_agents, monkeypatch):
        import config as app_config
        monkeypatch.setattr(app_config, "INTERNAL_LISTENER_PORT", 0, raising=False)
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

    def test_proxy_port_forward_lands_on_the_internal_listener(self, tmp_agents, monkeypatch):
        """With an internal listener bound (``config.INTERNAL_LISTENER_PORT``),
        the proxy-port forward alone becomes pasta's ``<namespace>:<host>``
        splice; the other forwards and the sandbox side are unchanged, and
        with no listener the argv is byte-identical to before."""
        import config as app_config
        agents_dir, mcps_dir = tmp_agents
        proxy_port = str(app_config.PORT)
        cfg = _netns_cfg(agents_dir, mcps_dir, forwards=[proxy_port, "8931"])
        monkeypatch.setattr(app_config, "INTERNAL_LISTENER_PORT", 0, raising=False)
        plain = SandboxBuilder(cfg).build_command_prefix(["claude"])
        monkeypatch.setattr(app_config, "INTERNAL_LISTENER_PORT", 8123, raising=False)
        spliced = SandboxBuilder(cfg).build_command_prefix(["claude"])
        fwd = lambda cmd: [cmd[i + 1] for i, a in enumerate(cmd) if a == "--forward"]  # noqa: E731
        assert fwd(plain) == [proxy_port, "8931"]
        assert fwd(spliced) == [f"{proxy_port}:8123", "8931"]
        assert [a for a in plain if a != proxy_port] == [a for a in spliced if a != f"{proxy_port}:8123"]

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


class TestTmpfsCap:
    """Per-sandbox /tmp cap (bwrap --size): feature-gated on the bubblewrap,
    bounded by half the host's RAM, off with SANDBOX_TMP_SIZE_MB=0."""

    def _args(self, tmp_agents):
        agents_dir, mcps_dir = tmp_agents
        return SandboxBuilder(_make_config(agents_dir, mcps_dir))._system_mounts()

    def test_size_directly_precedes_tmpfs_when_supported(self, tmp_agents, monkeypatch):
        monkeypatch.setattr(_sandbox_mod, "_bwrap_has_size", True)
        monkeypatch.setattr(_sandbox_mod.app_config, "SANDBOX_TMP_SIZE_MB", 4096)
        monkeypatch.setattr(_sandbox_mod, "_mem_total_mb", lambda: 16000)
        args = self._args(tmp_agents)
        i = args.index("--tmpfs")
        assert args[i + 1] == "/tmp"
        assert args[i - 2:i] == ["--size", str(4096 * 1024 * 1024)]

    def test_cap_never_looser_than_half_ram(self, monkeypatch):
        monkeypatch.setattr(_sandbox_mod, "_bwrap_has_size", True)
        monkeypatch.setattr(_sandbox_mod.app_config, "SANDBOX_TMP_SIZE_MB", 4096)
        monkeypatch.setattr(_sandbox_mod, "_mem_total_mb", lambda: 4000)
        assert _sandbox_mod.tmpfs_cap_mb() == 2000
        monkeypatch.setattr(_sandbox_mod, "_mem_total_mb", lambda: 0)  # unreadable
        assert _sandbox_mod.tmpfs_cap_mb() == 4096

    def test_no_size_when_bwrap_lacks_it(self, tmp_agents, monkeypatch):
        monkeypatch.setattr(_sandbox_mod, "_bwrap_has_size", False)
        monkeypatch.setattr(_sandbox_mod.app_config, "SANDBOX_TMP_SIZE_MB", 4096)
        args = self._args(tmp_agents)
        assert "--size" not in args and "/tmp" in args

    def test_zero_disables(self, tmp_agents, monkeypatch):
        monkeypatch.setattr(_sandbox_mod, "_bwrap_has_size", True)
        monkeypatch.setattr(_sandbox_mod.app_config, "SANDBOX_TMP_SIZE_MB", 0)
        assert "--size" not in self._args(tmp_agents)

    def test_lazy_probe_when_preflight_never_ran(self, monkeypatch):
        monkeypatch.setattr(_sandbox_mod, "_bwrap_has_size", None)
        monkeypatch.setattr(_sandbox_mod.app_config, "SANDBOX_TMP_SIZE_MB", 4096)
        monkeypatch.setattr(_sandbox_mod, "_probe_bwrap_size", lambda: True)
        monkeypatch.setattr(_sandbox_mod, "_mem_total_mb", lambda: 16000)
        assert _sandbox_mod.tmpfs_cap_mb() == 4096
        assert _sandbox_mod._bwrap_has_size is True

    def test_preflight_logs_and_caches(self, monkeypatch, caplog):
        monkeypatch.setattr(_sandbox_mod, "_bwrap_has_size", None)
        monkeypatch.setattr(_sandbox_mod.app_config, "SANDBOX_TMP_SIZE_MB", 4096)
        monkeypatch.setattr(_sandbox_mod, "_probe_bwrap_size", lambda: False)
        with caplog.at_level("INFO", logger=_sandbox_mod.logger.name):
            _sandbox_mod.tmpfs_cap_preflight()
        assert _sandbox_mod._bwrap_has_size is False
        assert "uncapped" in caplog.text

    def test_claude_runtime_root_follows_uid(self, monkeypatch):
        monkeypatch.setattr(_sandbox_mod.os, "getuid", lambda: 1234)
        assert _sandbox_mod.claude_runtime_root() == "/tmp/claude-1234"


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

    def _failing_probe(self, monkeypatch, tmp_path=None):
        # All tools present; the `unshare -Urn true` capability probe fails.
        real_which = _shutil.which
        monkeypatch.setattr(_sandbox_mod.shutil, "which",
                            lambda tool: real_which(tool) or f"/usr/bin/{tool}")
        monkeypatch.setattr(_sandbox_mod.os, "access", lambda *a, **k: True)
        if tmp_path is not None:
            # Hermetic: don't let the HOST's Debian sysctl steer the message.
            monkeypatch.setattr(_sandbox_mod, "_DEBIAN_USERNS_SYSCTL",
                                tmp_path / "missing-debian-sysctl")

        class _Probe:
            returncode = 1
            stderr = b"unshare: unshare failed: Operation not permitted"
        monkeypatch.setattr(_sandbox_mod.subprocess, "run",
                            lambda *a, **k: _Probe())

    def _happy_gates(self, monkeypatch, tmp_path):
        # Tools present + one-level probe passes; keep the resolv side effects
        # inside tmp so the success path is exercisable from tests.
        real_which = _shutil.which
        monkeypatch.setattr(_sandbox_mod.shutil, "which",
                            lambda tool: real_which(tool) or f"/usr/bin/{tool}")
        monkeypatch.setattr(_sandbox_mod.os, "access", lambda *a, **k: True)
        monkeypatch.setattr(_sandbox_mod, "_host_resolv_has_loopback_ns",
                            lambda *a, **k: False)
        monkeypatch.setattr(_sandbox_mod, "netns_resolv_path",
                            lambda: tmp_path / "netns-resolv.conf")

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
        self._failing_probe(monkeypatch, tmp_path)
        monkeypatch.setattr(_sandbox_mod, "_APPARMOR_USERNS_SYSCTL",
                            tmp_path / "missing-sysctl")
        with pytest.raises(RuntimeError) as exc:
            _sandbox_mod.netns_preflight()
        msg = str(exc.value)
        assert "apparmor_restrict_unprivileged_userns" not in msg
        assert "cannot create" in msg

    def test_probe_failure_names_debian_knob(self, monkeypatch, tmp_path):
        # kernel.unprivileged_userns_clone=0 (Debian hardening): name the knob
        # and its remedy instead of the generic message.
        self._failing_probe(monkeypatch)
        monkeypatch.setattr(_sandbox_mod, "_APPARMOR_USERNS_SYSCTL",
                            tmp_path / "missing-sysctl")
        debian = tmp_path / "unprivileged_userns_clone"
        debian.write_text("0\n")
        monkeypatch.setattr(_sandbox_mod, "_DEBIAN_USERNS_SYSCTL", debian)
        with pytest.raises(RuntimeError) as exc:
            _sandbox_mod.netns_preflight()
        assert "unprivileged_userns_clone" in str(exc.value)

    def test_unshare_missing_probes_with_bwrap_instead(self, monkeypatch, tmp_path):
        # `unshare` absent used to SKIP the capability probe (host boots, every
        # spawn fails). Now it must fall back to probing with bwrap itself.
        self._happy_gates(monkeypatch, tmp_path)
        calls = []

        class _OK:
            returncode = 0
            stderr = b""

        def fake_run(argv, **kwargs):
            calls.append(argv[0])
            if argv[0] == "unshare":
                raise FileNotFoundError("unshare")
            return _OK()

        monkeypatch.setattr(_sandbox_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(_sandbox_mod, "_nested_sandbox_probe", lambda: None)
        _sandbox_mod.netns_preflight()  # must not raise
        assert calls[0] == "unshare" and "bwrap" in calls

    def test_nested_probe_failure_warns_never_fatal(self, monkeypatch, tmp_path, caplog):
        # A host can pass the one-level gate yet deny the NESTED level (what
        # the Codex engine's inner bwrap needs — the 2026-08-12 incident).
        # Boot must proceed; the flag flips False and the warning names the
        # doctor script.
        self._happy_gates(monkeypatch, tmp_path)

        class _OK:
            returncode = 0
            stderr = b""
        monkeypatch.setattr(_sandbox_mod.subprocess, "run", lambda *a, **k: _OK())
        monkeypatch.setattr(
            _sandbox_mod, "_nested_sandbox_probe",
            lambda: "bwrap: setting up uid map: Permission denied",
        )
        with caplog.at_level("WARNING"):
            _sandbox_mod.netns_preflight()  # must not raise
        assert _sandbox_mod.nested_sandbox_ok() is False
        assert any("sandbox-doctor" in r.message for r in caplog.records)

        # And a healthy nested probe flips it back True.
        monkeypatch.setattr(_sandbox_mod, "_nested_sandbox_probe", lambda: None)
        _sandbox_mod.netns_preflight()
        assert _sandbox_mod.nested_sandbox_ok() is True


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

    def test_ipv6_loopback_fails_at_once_so_localhost_falls_back(self, tmp_agents):
        """A forward reaches a host service on IPv4 loopback only (every
        platform publish is 127.0.0.1). pasta's splice also listens on ::1 and
        resets a connection it cannot complete host-side, which a client
        that dials ::1 first for "localhost" never recovers from. The
        namespace has no IPv6 loopback, so that dial fails at once and the
        client moves on to 127.0.0.1."""
        agents_dir, mcps_dir = tmp_agents
        port = _free_port()
        _accept_once(port, b"REACHED")
        cfg = _netns_cfg(agents_dir, mcps_dir, forwards=[str(port)])
        probe = (
            "import socket,sys,time\n"
            "t=time.monotonic()\n"
            "s=socket.socket(socket.AF_INET6); s.settimeout(5)\n"
            "try:\n"
            f"  s.connect(('::1',{port})); r='connected'\n"
            "except OSError as e: r='refused'\n"
            "sys.stdout.write('v6='+r+' %.2f\\n' % (time.monotonic()-t))\n"
            "s=socket.socket(); s.settimeout(5)\n"
            f"s.connect(('127.0.0.1',{port})); sys.stdout.write('v4='+s.recv(16).decode()+'\\n')\n"
        )
        cmd = SandboxBuilder(cfg).build_command_prefix(["python3", "-c", probe])
        out = _subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        assert "v4=REACHED" in out.stdout, (out.stdout, out.stderr)
        v6 = next(ln for ln in out.stdout.splitlines() if ln.startswith("v6="))
        verdict, took = v6[3:].split()
        assert verdict == "refused" and float(took) < 1.0, (out.stdout, out.stderr)


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


class TestSandboxBuilderContributor:
    """A contributor writes the shared workspace and nothing else: the
    editor's mounts (workspace RW, knowledge RO, no /config), read off the
    workspace tier, so a word the table does not know lands on the viewer's
    row in BOTH scopes (the agent-scope branch used to open RW to anything
    that was not the viewer word)."""

    def _pairs(self, tmp_agents, **kw):
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, **kw)
        cmd = SandboxBuilder(cfg).build_command_prefix(["claude"])
        return str(agents_dir / "personal-assistant"), list(zip(cmd, cmd[1:]))

    def test_workspace_rw_knowledge_ro_no_config(self, tmp_agents):
        agent_dir, pairs = self._pairs(tmp_agents, role="contributor")
        assert ("--bind", f"{agent_dir}/workspace") in pairs
        assert ("--ro-bind", f"{agent_dir}/knowledge") in pairs
        assert not any(b == f"{agent_dir}/config" for _a, b in pairs)

    def test_shared_only_chat_mounts_workspace_rw(self, tmp_agents):
        # A Shared-only human chat mounts the agent scope with the person's role.
        agent_dir, pairs = self._pairs(tmp_agents, role="contributor", username="")
        assert ("--bind", f"{agent_dir}/workspace") in pairs
        assert ("--ro-bind", f"{agent_dir}/knowledge") in pairs

    def test_unknown_word_is_read_only_in_both_scopes(self, tmp_agents):
        for username in ("alice", ""):
            agent_dir, pairs = self._pairs(tmp_agents, role="none", username=username)
            assert ("--ro-bind", f"{agent_dir}/workspace") in pairs, username
            assert ("--bind", f"{agent_dir}/workspace") not in pairs, username

    def _ext_pairs(self, tmp_agents, **kw):
        import dataclasses
        agents_dir, mcps_dir = tmp_agents
        cfg = dataclasses.replace(_make_config(agents_dir, mcps_dir, **kw), external=True)
        cmd = SandboxBuilder(cfg).build_command_prefix(["claude"])
        return str(agents_dir / "personal-assistant"), list(zip(cmd, cmd[1:]))

    def test_an_external_caller_keeps_the_cli_state_dirs_writable(self, tmp_agents):
        """An external caller with no tree of its own runs from the agent's
        CLI state under the read-only workspace (CLAUDE_CONFIG_DIR /
        CODEX_HOME = /workspace/.claude or .codex): the two stack RW on the
        RO root, or the CLI cannot even start (Codex's sqlite state runtime
        refused a read-only home, T1 2026-09-24)."""
        agents_dir, _ = tmp_agents
        workspace = agents_dir / "personal-assistant" / "workspace"
        for sub in (".claude", ".codex"):
            (workspace / sub).mkdir(parents=True, exist_ok=True)
        agent_dir, pairs = self._ext_pairs(tmp_agents, role="viewer", username="")
        assert ("--ro-bind", f"{agent_dir}/workspace") in pairs
        assert ("--bind", f"{agent_dir}/workspace/.claude") in pairs
        assert ("--bind", f"{agent_dir}/workspace/.codex") in pairs

    def test_a_person_below_the_editor_tier_never_sees_the_agents_state(self, tmp_agents):
        """A Shared-only viewer or contributor (whose CLI session the engines
        refuse) sees the agent's CLI state masked in any sandbox run as them,
        a script or an app button; an editor runs from it."""
        empty = str(empty_mount_dir())
        for role in ("viewer", "contributor"):
            agent_dir, pairs = self._pairs(tmp_agents, role=role, username="")
            for sub in (".claude", ".codex"):
                assert ("--ro-bind", empty) in pairs and (empty, f"/workspace/{sub}") in pairs, role
                assert ("--bind", f"{agent_dir}/workspace/{sub}") not in pairs, role
        agent_dir, pairs = self._pairs(tmp_agents, role="editor", username="")
        assert ("--bind", f"{agent_dir}/workspace") in pairs
        assert not any(b == "/workspace/.claude" for _a, b in pairs)

    def test_an_external_callers_state_dir_symlink_is_refused(self, tmp_agents, tmp_path):
        """The workspace is RW for the tiers above: a planted link must not
        become an RW bind of its target in a caller's sandbox, and a person's
        sandbox refuses to build over one."""
        agents_dir, mcps_dir = tmp_agents
        workspace = agents_dir / "personal-assistant" / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / ".claude").mkdir(exist_ok=True)
        (workspace / ".codex").symlink_to(tmp_path)
        agent_dir, pairs = self._ext_pairs(tmp_agents, role="viewer", username="")
        assert ("--bind", f"{agent_dir}/workspace/.claude") in pairs
        assert not any(b.endswith("/workspace/.codex") for _a, b in pairs)
        assert not any(b == str(tmp_path) for _a, b in pairs)
        with pytest.raises(RuntimeError, match="symlinked"):
            SandboxBuilder(_make_config(agents_dir, mcps_dir, role="viewer", username="")
                           ).workspace_mount_table()


class TestAgentStateMasked:
    """The agent scope's CLI state (workspace/.claude, workspace/.codex: the
    hooks, settings and MCP config every agent-scope session runs, the
    subscription login and the session tokens) is masked out of every
    session that does not run from it."""

    def _pairs(self, tmp_agents, **kw):
        agents_dir, mcps_dir = tmp_agents
        cfg = _make_config(agents_dir, mcps_dir, **kw)
        cmd = SandboxBuilder(cfg).build_command_prefix(["claude"])
        return str(agents_dir / "personal-assistant"), cmd

    @pytest.mark.parametrize("role", ["viewer", "contributor", "editor", "manager", "admin"])
    def test_a_user_scope_session_sees_an_empty_read_only_dir(self, tmp_agents, role):
        agent_dir, cmd = self._pairs(tmp_agents, role=role)
        empty = str(empty_mount_dir())
        ws = cmd.index(f"{agent_dir}/workspace")
        for sub in (".claude", ".codex"):
            at = cmd.index(f"/workspace/{sub}")
            assert cmd[at - 2:at + 1] == ["--ro-bind", empty, f"/workspace/{sub}"], role
            assert at > ws, role            # after the /workspace bind: it wins
            assert os.path.isdir(f"{agent_dir}/workspace/{sub}")   # the mountpoint

    def test_the_agent_scope_runs_from_its_own_state(self, tmp_agents):
        _agent_dir, cmd = self._pairs(tmp_agents, role="manager", username="")
        assert "/workspace/.claude" not in cmd and str(empty_mount_dir()) not in cmd

    def test_a_planted_link_refuses_the_build(self, tmp_agents, tmp_path):
        agents_dir, mcps_dir = tmp_agents
        (agents_dir / "personal-assistant" / "workspace" / ".codex").symlink_to(tmp_path)
        with pytest.raises(RuntimeError, match="symlinked"):
            SandboxBuilder(_make_config(agents_dir, mcps_dir, role="contributor")).workspace_mount_table()

    @_needs_netns
    def test_a_contributor_can_neither_read_nor_rewrite_the_agents_hooks(self, tmp_agents):
        agents_dir, mcps_dir = tmp_agents
        state = agents_dir / "personal-assistant" / "workspace" / ".claude"
        state.mkdir()
        (state / "permission_gate.py").write_text("GATE\n")
        (state / ".credentials.json").write_text("TOKEN\n")
        cfg = _netns_cfg(agents_dir, mcps_dir, forwards=[str(_free_port())], role="contributor")
        probe = (
            "import sys\n"
            "def t(f):\n"
            "  try: f(); return 'ok'\n"
            "  except Exception as e: return type(e).__name__\n"
            "base='/workspace/.cl'+'aude/'\n"
            "sys.stdout.write('read='+t(lambda: open(base+'.credentials.json').read())+'\\n')\n"
            "sys.stdout.write('hook='+t(lambda: open(base+'permission_gate.py','w').write('X'))+'\\n')\n"
            "sys.stdout.write('plant='+t(lambda: open(base+'json.py','w').write('X'))+'\\n')\n"
            "sys.stdout.write('shared='+t(lambda: open('/workspace/notes.txt','w').write('X'))+'\\n')\n"
        )
        out = _subprocess.run(SandboxBuilder(cfg).build_command_prefix(["python3", "-c", probe]),
                              capture_output=True, text=True, timeout=30)
        assert "read=FileNotFoundError" in out.stdout, (out.stdout, out.stderr)
        assert "hook=ok" not in out.stdout and "plant=ok" not in out.stdout, out.stdout
        assert "shared=ok" in out.stdout, (out.stdout, out.stderr)
        assert (state / "permission_gate.py").read_text() == "GATE\n"
        assert not (state / "json.py").exists()


class TestRefusalWording:
    """The refusal names what the session runs as: a Shared-only agent's
    chats, or an agent-scope chat on any other agent."""

    def test_shared_only_is_the_default_sentence(self):
        from core.sandbox.session_config_dir import AgentStateRefused, refuse_agent_state_below_editor
        with pytest.raises(AgentStateRefused, match="set to Shared only") as e:
            refuse_agent_state_below_editor("agent", "viewer")
        assert "run as viewer" in str(e.value) and "turn on personal chats" in str(e.value)

    def test_an_agent_scope_chat_elsewhere_says_what_it_runs_as(self):
        from core.sandbox.session_config_dir import AgentStateRefused, refuse_agent_state_below_editor
        with pytest.raises(AgentStateRefused) as e:
            refuse_agent_state_below_editor("agent", "contributor", shared_only=False)
        msg = str(e.value)
        assert msg.startswith("This chat runs as the agent itself")
        assert "Shared only" not in msg and "personal chats" not in msg
        assert "editor role" in msg and "run as contributor" in msg
        refuse_agent_state_below_editor("agent", "editor", shared_only=False)


class TestEnginesRefuseTheAgentStateBelowEditor:
    """The start-time floor: no engine runs a CLI from the agent's own state
    for a person below the editor tier (its sandbox masks that dir, so the
    CLI would start with no hooks), locally or on a machine."""

    def _ctx(self, role, username="", scope="agent", **kw):
        from auth.path_policy import SecurityContext
        return SecurityContext(role=role, username=username, agent="personal-assistant",
                               is_admin_agent=False, session_scope=scope, **kw)

    def test_the_floor(self):
        from core.sandbox.session_config_dir import AgentStateRefused, refuse_session_on_agent_state
        for role in ("viewer", "contributor", "", "none"):
            with pytest.raises(AgentStateRefused):
                refuse_session_on_agent_state(self._ctx(role))
        with pytest.raises(AgentStateRefused):
            refuse_session_on_agent_state(None)
        # A Shared-only chat carries the person's name but mounts the agent.
        with pytest.raises(AgentStateRefused):
            refuse_session_on_agent_state(self._ctx("viewer", username="alice", scope="agent"))
        for ctx in (self._ctx("editor"), self._ctx("manager"), self._ctx("admin"),
                    self._ctx("viewer", username="alice", scope="user"),
                    self._ctx("viewer", principal="external")):
            refuse_session_on_agent_state(ctx)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["claude-code-cli", "codex-cli"])
    async def test_a_local_engine_refuses_before_it_spawns(self, path, tmp_path):
        from core.execution_layer import AgentConfig
        from core.sandbox.session_config_dir import AgentStateRefused
        from core.session.session_manager import get_layer_by_path
        cfg = AgentConfig(agent_name="personal-assistant", sandbox_host_claude_dir=str(tmp_path),
                          security_context=self._ctx("contributor"), execution_path=path)
        with pytest.raises(AgentStateRefused):
            await get_layer_by_path(path)._start_session_impl("sess-x", cfg)

    def test_the_start_time_refusal_names_what_the_session_runs_as(self, temp_db):
        from core.sandbox.session_config_dir import AgentStateRefused, refuse_session_on_agent_state
        from storage.agents import agent_store
        agent_store.create_agent("personal-assistant", "PA", collaborative=True, default_scope="user")
        with pytest.raises(AgentStateRefused, match="runs as the agent itself"):
            refuse_session_on_agent_state(self._ctx("contributor"))
        agent_store.update_agent("personal-assistant", collaborative=False, default_scope="agent")
        with pytest.raises(AgentStateRefused, match="set to Shared only"):
            refuse_session_on_agent_state(self._ctx("contributor"))

    @pytest.mark.asyncio
    async def test_a_machine_refuses_before_any_frame(self):
        from unittest.mock import MagicMock
        from core.execution_layer import AgentConfig
        from core.remote.remote_execution import RemoteExecutionLayer
        from core.sandbox.session_config_dir import AgentStateRefused
        layer = RemoteExecutionLayer(MagicMock())
        cfg = AgentConfig(agent_name="personal-assistant", execution_target="machine-1",
                          security_context=self._ctx("viewer", username="alice", scope="agent"))
        with pytest.raises(AgentStateRefused):
            await layer.start_session("sess-x", cfg)
        layer._cm.is_connected.assert_not_called()


# ---------------------------------------------------------------------------
# Held bind sources (F68): every bind source under the agents root reaches
# bwrap as a descriptor the launcher's shim opened with no link followed,
# and the proxy's own mkdirs of those sources never follow a link either.
# ---------------------------------------------------------------------------

import dataclasses as _dataclasses
import stat

from core.sandbox.sandbox import Mount as _Mount


def _launcher_module():
    import importlib.util
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("oto_sandbox_net_held", str(_sandbox_mod._NETNS_LAUNCHER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _shim_held_binds(beneath):
    """The shim's own held-bind code, run without its network setup."""
    ns: dict = {}
    exec(_launcher_module()._HELD_BINDS, ns)
    ns["_BENEATH"] = list(beneath)
    return ns["_held_binds"]


def _close_fds(argv):
    for flag, fd in zip(argv, argv[1:]):
        if flag in ("--bind-fd", "--ro-bind-fd"):
            os.close(int(fd))


def _bwrap_argv(cmd):
    return cmd[cmd.index("--") + 1:]


class TestHeldBindSources:
    def test_an_agent_session_names_its_resolved_folder_to_the_launcher(self, tmp_agents):
        agents_dir, mcps_dir = tmp_agents
        for username in ("alice", ""):
            cfg = _make_config(agents_dir, mcps_dir, username=username)
            cmd = SandboxBuilder(cfg).build_command_prefix(["claude"])
            launcher = cmd[:cmd.index("--")]
            root = str((agents_dir / "personal-assistant").resolve())
            assert launcher[launcher.index("--beneath") + 1] == root

    def test_an_agent_folder_that_is_a_link_keeps_starting(self, tmp_agents, tmp_path):
        """An operator may move an agent's folder to another disk and leave
        a link: every source is spelled from the resolved folder, the
        launcher holds them beneath it, and the mkdirs follow no link below
        it."""
        agents_dir, mcps_dir = tmp_agents
        moved = tmp_path / "other-disk" / "moved-agent"
        for sub in ("workspace", "config", "users/alice/workspace"):
            (moved / sub).mkdir(parents=True)
        (agents_dir / "moved-agent").symlink_to(moved)
        for username in ("alice", ""):
            cfg = _make_config(agents_dir, mcps_dir, username=username, agent="moved-agent")
            cmd = SandboxBuilder(cfg).build_command_prefix(["claude"])
            launcher = cmd[:cmd.index("--")]
            real = str(moved.resolve())
            assert launcher[launcher.index("--beneath") + 1] == real
            bwrap = _bwrap_argv(cmd)
            assert not [a for a in bwrap if a.startswith(str(agents_dir / "moved-agent") + "/")]
            out = _shim_held_binds([real])(bwrap)
            try:
                assert out.count("--bind-fd") + out.count("--ro-bind-fd") >= 2
            finally:
                _close_fds(out[:out.index("--")])

    def test_every_option_the_builder_emits_is_known_to_the_shim(self, tmp_agents, tmp_path):
        """The shim steps over each bwrap option with its values; one it
        does not know refuses the launch, so every shape the builder emits
        must walk cleanly and every agent-tree source must be held."""
        from core.sandbox.session_config_dir import HOOK_SCRIPTS
        agents_dir, mcps_dir = tmp_agents
        pa = agents_dir / "personal-assistant"
        (pa / "knowledge" / "shared" / "lib").mkdir(parents=True)
        for state in (pa / "users" / "alice" / ".claude", pa / "workspace" / ".codex"):
            state.mkdir(parents=True, exist_ok=True)
            for name in HOOK_SCRIPTS:
                (state / name).write_text("# hook\n")
        home = pa / "externals" / "caller-1"
        home.mkdir(parents=True)
        shapes = [
            _make_config(agents_dir, mcps_dir, role="editor"),
            _make_config(agents_dir, mcps_dir, role="manager", username=""),
            _dataclasses.replace(_make_config(agents_dir, mcps_dir, role="manager"),
                                 knowledge_libraries=[("lib", "", False)], read_only=True),
            _dataclasses.replace(_make_config(agents_dir, mcps_dir, role="viewer", username=""),
                                 external=True, external_home=str(home.resolve())),
        ]
        root = str(pa.resolve())
        for cfg in shapes:
            bwrap = _bwrap_argv(SandboxBuilder(cfg).build_command_prefix(["claude", "--", "x"]))
            out = _shim_held_binds([root])(bwrap)
            try:
                head = out[:out.index("--")]
                assert not [s for f, s in zip(head, head[1:])
                            if f in ("--bind", "--ro-bind") and s.startswith(root + "/")]
            finally:
                _close_fds(out[:out.index("--")])

    def test_an_app_table_names_no_root(self, tmp_agents, tmp_path):
        agents_dir, mcps_dir = tmp_agents
        cfg = _dataclasses.replace(
            _make_config(agents_dir, mcps_dir, username=""),
            app_mounts=[_Mount(str(tmp_path), "/app", False)], app_cwd="/app")
        cmd = SandboxBuilder(cfg).build_command_prefix(["bun"])
        assert "--beneath" not in cmd[:cmd.index("--")]

    def test_the_launcher_takes_the_root_before_its_own_flags(self):
        mod = _launcher_module()
        roots, rest = mod._take_beneath(
            ["--beneath", "/srv/agents", "--forward", "8400", "--", "bwrap", "--beneath", "x"])
        assert roots == ["/srv/agents"]
        assert rest == ["--forward", "8400", "--", "bwrap", "--beneath", "x"]
        assert "_BENEATH = ['/srv/agents']" in mod._build_pyshim(
            ["8400"], [], "", True, False, beneath=["/srv/agents"])
        assert "_BENEATH = []" in mod._build_pyshim(["8400"], [], "", True, False)

    def test_the_shim_hands_bwrap_a_held_handle_for_each_source_below_the_root(self, tmp_path):
        root = tmp_path / "agent"
        ws = root / "workspace"
        state = root / "users" / "u" / "state"
        ws.mkdir(parents=True)
        state.mkdir(parents=True)
        hook = state / "permission_gate.py"
        hook.write_text("x")
        argv = ["bwrap", "--ro-bind", "/usr", "/usr",
                "--bind", str(ws), "/workspace",
                "--bind", str(state), "/users/u/state",
                "--ro-bind", str(hook), "/users/u/state/permission_gate.py",
                "--chdir", "/workspace", "--", "tool", "--bind", str(ws), "x"]
        out = _shim_held_binds([str(root)])(argv)
        try:
            assert out[:4] == ["bwrap", "--ro-bind", "/usr", "/usr"]
            for flag, src, dest in (("--bind-fd", ws, "/workspace"),
                                    ("--bind-fd", state, "/users/u/state"),
                                    ("--ro-bind-fd", hook, "/users/u/state/permission_gate.py")):
                at = out.index(dest)
                assert out[at - 2] == flag
                fd = int(out[at - 1])
                assert os.readlink(f"/proc/self/fd/{fd}") == str(src)
                assert os.get_inheritable(fd)
            # A FILE source (the hook script) is held as itself, not its dir.
            at = out.index("/users/u/state/permission_gate.py")
            assert stat.S_ISREG(os.fstat(int(out[at - 1])).st_mode)
            # The session's own argv after bwrap's separator is never touched.
            assert out[out.index("--"):] == argv[argv.index("--"):]
        finally:
            _close_fds(out[:out.index("--")])

    def test_the_shim_holds_any_source_but_a_link(self, tmp_path):
        """A socket or FIFO an MCP manifest names inside the agent's tree is
        bound today and stays bound: only a link is refused."""
        import socket
        root = tmp_path / "agent"
        root.mkdir()
        fifo = root / "pipe"
        os.mkfifo(fifo)
        sock_path = root / "sock"
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(sock_path))
        try:
            out = _shim_held_binds([str(root)])(
                ["bwrap", "--bind", str(fifo), "/p", "--bind", str(sock_path), "/s", "--", "true"])
            try:
                assert out[1] == "--bind-fd" and out[4] == "--bind-fd"
                assert stat.S_ISFIFO(os.fstat(int(out[2])).st_mode)
                assert stat.S_ISSOCK(os.fstat(int(out[5])).st_mode)
            finally:
                _close_fds(out[:out.index("--")])
        finally:
            sock.close()

    def test_the_shim_steps_over_option_values(self, tmp_path):
        """A value that reads like an option (``--``, ``--bind``) is a value:
        it neither ends the walk nor becomes a bind."""
        root = tmp_path / "agent"
        (root / "workspace").mkdir(parents=True)
        src = str(root / "workspace")
        argv = ["bwrap", "--setenv", "X", "--bind", "--chdir", "--", "--size", "10",
                "--bind", src, "/workspace", "--", "true"]
        out = _shim_held_binds([str(root)])(argv)
        try:
            assert out[:8] == argv[:8]
            assert out[8] == "--bind-fd" and out[10] == "/workspace"
            assert out[11:] == ["--", "true"]
        finally:
            _close_fds(out[:out.index("--", 8)])

    def test_the_shim_refuses_an_option_it_does_not_know(self, tmp_path):
        with pytest.raises(OSError):
            _shim_held_binds([str(tmp_path)])(["bwrap", "--brand-new", "v", "--", "true"])

    @pytest.mark.parametrize("where", ["component", "leaf", "root"])
    def test_the_shim_refuses_a_source_reached_through_a_link(self, tmp_path, where):
        root = tmp_path / "agent"
        root.mkdir()
        outside = tmp_path / "outside"
        (outside / "sub").mkdir(parents=True)
        (outside / "f.py").write_text("x")
        held_root = str(root)
        if where == "component":
            (root / "k").symlink_to(outside)
            src = root / "k" / "sub"
        elif where == "leaf":
            src = root / "hook.py"
            src.symlink_to(outside / "f.py")
        else:
            # The root the proxy resolved became a link before the spawn.
            held_root = str(tmp_path / "agent-link")
            (tmp_path / "agent-link").symlink_to(outside)
            src = tmp_path / "agent-link" / "sub"
        with pytest.raises(OSError):
            _shim_held_binds([held_root])(["bwrap", "--ro-bind", str(src), "/x", "--", "true"])

    def test_the_mirror_mkdir_never_creates_through_a_link_swapped_in_after_the_check(
            self, tmp_agents, tmp_path, monkeypatch):
        agents_dir, mcps_dir = tmp_agents
        shared = agents_dir / "personal-assistant" / "knowledge" / "shared"
        shared.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (shared / "src").symlink_to(outside)
        # The swap lands after the early realpath refusal: let that check pass.
        monkeypatch.setattr(_sandbox_mod, "_verified_literal_path",
                            lambda root, *parts: root.joinpath(*parts))
        cfg = _dataclasses.replace(_make_config(agents_dir, mcps_dir, role="manager"),
                                   knowledge_libraries=[("src", "sub", False)])
        with pytest.raises(RuntimeError):
            SandboxBuilder(cfg).workspace_mount_table()
        assert not (outside / "sub").exists()

    def test_a_session_dir_is_never_made_through_a_link_swapped_in_after_the_check(
            self, tmp_agents, tmp_path, monkeypatch):
        from core.sandbox import session_config_dir as scd
        agents_dir, _mcps_dir = tmp_agents
        monkeypatch.setattr(_app_config, "AGENTS_DIR", agents_dir)
        outside = tmp_path / "outside"
        outside.mkdir()
        (agents_dir / "personal-assistant" / "users" / "carol").symlink_to(outside)
        # The swap lands after the early realpath refusal: let that check pass.
        real = os.path.realpath
        monkeypatch.setattr(scd.os.path, "realpath",
                            lambda p, *a, **k: str(p) if "carol" in str(p) else real(p, *a, **k))
        with pytest.raises(RuntimeError):
            scd._verified_session_dir("personal-assistant", "users", "carol", ".claude")
        assert not (outside / ".claude").exists()

    @_needs_netns
    def test_a_mirror_source_swapped_for_a_link_after_the_build_never_mounts_its_target(
            self, tmp_agents, tmp_path):
        """The decisive end-to-end case: a Personal-only library mirror has no
        parent /knowledge bind, so its destination is a fresh mountpoint that
        does not traverse the swapped tree. A component of the source swapped
        for a link to an out-of-tree directory, between the build and the
        spawn, must refuse the start — never bind the link's target."""
        agents_dir, mcps_dir = tmp_agents
        shared = agents_dir / "personal-assistant" / "knowledge" / "shared"
        (shared / "src" / "sub").mkdir(parents=True)
        (shared / "src" / "sub" / "in-tree.txt").write_text("INTREE")
        cfg = _dataclasses.replace(
            _make_config(agents_dir, mcps_dir, role="manager"),
            mount_shared=False, knowledge_libraries=[("src", "sub", False)])
        cmd = SandboxBuilder(cfg).build_command_prefix(["ls", "/knowledge/shared/src/sub"])
        outside = tmp_path / "outside"
        (outside / "sub").mkdir(parents=True)
        (outside / "sub" / "host-only.txt").write_text("x")
        (shared / "src").rename(shared / "src.real")
        (shared / "src").symlink_to(outside)
        out = _subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        assert "host-only.txt" not in out.stdout, (out.stdout, out.stderr)
        assert out.returncode != 0

    @_needs_netns
    def test_held_sources_bind_and_no_descriptor_reaches_the_session(self, tmp_agents):
        agents_dir, mcps_dir = tmp_agents
        (agents_dir / "personal-assistant" / "users" / "alice" / "workspace" / "w.txt").write_text("SEEN")
        probe = ("import os\n"
                 "fds = sorted(int(f) for f in os.listdir('/proc/self/fd'))\n"
                 "print('extra=' + ','.join(str(f) for f in fds if f > 3))\n"
                 "print('w=' + open('/users/alice/workspace/w.txt').read())\n")
        cfg = _make_config(agents_dir, mcps_dir, role="editor")
        cmd = SandboxBuilder(cfg).build_command_prefix(["python3", "-c", probe])
        out = _subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        assert "w=SEEN" in out.stdout, (out.stdout, out.stderr)
        assert "extra=\n" in out.stdout, (out.stdout, out.stderr)

"""The `/caller` home: mount table, cwd, config dirs, path roles, OTO env.

An external session (a phone caller who is not a platform user) rides the
agent-scope mount branch plus two additions — the shared memory mask and
the caller's own tree — and the Direct-LLM file resolver sees exactly the
same decisions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import config as app_config
from core.layers.direct import files as direct_files
from core.sandbox.sandbox import (
    EXTERNAL_SANDBOX_HOME, SandboxBuilder, SandboxConfig, empty_mount_dir,
)
from services import path_roles
from core.sandbox.oto_env import build_oto_env


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents)
    monkeypatch.setattr(app_config, "SESSIONS_DIR", tmp_path / "sessions")
    agent_dir = agents / "support"
    for sub in ("workspace", "knowledge/memory", "config"):
        (agent_dir / sub).mkdir(parents=True)
    (agent_dir / "knowledge" / "memory" / "MEMORY.md").write_text("# shared\n")
    home = agent_dir / "externals" / "phone" / "302101234567"
    home.mkdir(parents=True)
    return agent_dir, home


def _cfg(agent_dir: Path, *, role="viewer", external=True, home: Path | None = None,
         knowledge_rw=False) -> SandboxConfig:
    return SandboxConfig(
        role=role, username="", agent_name="support", is_admin_agent=False,
        host_agents_dir=agent_dir.parent, host_mcps_dir=agent_dir.parent,
        host_claude_dir=(home or agent_dir / "workspace") / ".claude",
        config_visible=False, knowledge_rw=knowledge_rw,
        net_forwards=["8400"], external=external,
        external_home=str(home) if home else "",
    )


def _by_dest(mounts):
    return {m.sandbox: m for m in mounts}


class TestMountTable:
    def test_viewer_caller_with_tree(self, agent_tree):
        agent_dir, home = agent_tree
        b = SandboxBuilder(_cfg(agent_dir, home=home))
        mounts = _by_dest(b.workspace_mount_table())
        assert mounts["/workspace"].rw is False
        assert mounts["/knowledge"].rw is False
        # Shared memory masked by the platform-owned empty dir (later bind wins).
        assert mounts["/knowledge/memory"].host == str(empty_mount_dir())
        assert mounts["/knowledge/memory"].rw is False
        assert mounts[EXTERNAL_SANDBOX_HOME] == mounts["/caller"]
        assert mounts["/caller"].rw is False and mounts["/caller"].host == str(home)
        assert mounts["/caller/workspace"].rw and mounts["/caller/context"].rw
        assert (home / "workspace").is_dir() and (home / "context").is_dir()
        assert "/config" not in mounts and not any(d.startswith("/users") for d in mounts)
        assert b.get_cwd() == "/caller"
        assert b.get_env_overrides()["CLAUDE_CONFIG_DIR"] == "/caller/.claude"

    def test_roles_follow_the_route(self, agent_tree):
        agent_dir, home = agent_tree
        editor = _by_dest(SandboxBuilder(_cfg(agent_dir, role="editor", home=home)).workspace_mount_table())
        assert editor["/workspace"].rw is True and editor["/knowledge"].rw is False
        manager = _by_dest(SandboxBuilder(
            _cfg(agent_dir, role="manager", home=home, knowledge_rw=True)).workspace_mount_table())
        assert manager["/workspace"].rw and manager["/knowledge"].rw
        assert "/config" not in manager
        assert manager["/knowledge/memory"].host == str(empty_mount_dir())

    def test_shared_mode_has_no_tree_but_masks_memory(self, agent_tree):
        agent_dir, _home = agent_tree
        b = SandboxBuilder(_cfg(agent_dir))
        mounts = _by_dest(b.workspace_mount_table())
        assert "/caller" not in mounts
        assert mounts["/knowledge/memory"].host == str(empty_mount_dir())
        assert b.get_cwd() == "/workspace"
        assert b.get_env_overrides()["CLAUDE_CONFIG_DIR"] == "/workspace/.claude"

    def test_no_memory_dir_no_mask(self, agent_tree):
        agent_dir, home = agent_tree
        import shutil
        shutil.rmtree(agent_dir / "knowledge" / "memory")
        mounts = _by_dest(SandboxBuilder(_cfg(agent_dir, home=home)).workspace_mount_table())
        assert "/knowledge/memory" not in mounts

    def test_non_external_sessions_are_untouched(self, agent_tree):
        agent_dir, _home = agent_tree
        mounts = _by_dest(SandboxBuilder(_cfg(agent_dir, role="manager", external=False)).workspace_mount_table())
        assert "/knowledge/memory" not in mounts and "/caller" not in mounts

    def test_symlinked_home_is_refused(self, agent_tree, tmp_path):
        agent_dir, home = agent_tree
        outside = tmp_path / "outside"
        outside.mkdir()
        link = agent_dir / "externals" / "phone" / "999"
        os.symlink(outside, link)
        with pytest.raises(RuntimeError):
            SandboxBuilder(_cfg(agent_dir, home=link)).workspace_mount_table()

    def test_direct_resolver_sees_the_same_decisions(self, agent_tree):
        agent_dir, home = agent_tree
        cfg = _cfg(agent_dir, home=home)
        mounts = direct_files.mount_table(cfg)
        cwd = direct_files.session_cwd(cfg)
        assert cwd == "/caller"
        r = direct_files.resolve(mounts, "workspace/notes.txt", cwd=cwd, writing=True)
        assert Path(r.host) == home / "workspace" / "notes.txt"
        r = direct_files.resolve(mounts, "/knowledge/memory/MEMORY.md", cwd=cwd, writing=False)
        assert Path(r.host) == empty_mount_dir() / "MEMORY.md"   # the mask, not the shared file
        with pytest.raises(direct_files.FileToolError):
            direct_files.resolve(mounts, "/caller/stray.txt", cwd=cwd, writing=True)
        with pytest.raises(direct_files.FileToolError):
            direct_files.resolve(mounts, "/workspace/shared.txt", cwd=cwd, writing=True)


class TestConfigDirs:
    def test_claude_dir_in_the_caller_tree_with_no_shell(self, agent_tree, temp_db):
        from core.sandbox.session_config_dir import ensure_persistent_agent_dir
        agent_dir, home = agent_tree
        claude_dir = ensure_persistent_agent_dir(
            "support", execution_path="claude-code-cli", external_home=home, no_shell=True,
        )
        assert claude_dir == home / ".claude"
        settings = json.loads((claude_dir / "settings.json").read_text())
        # A session's own denials ride its argv and the hook floor, never the
        # settings file (a scope's file is shared and reloaded live).
        assert not {"Bash", "Monitor", "PowerShell"} & set(settings["permissions"]["deny"])
        from core.layers.cli.layer import CLIExecutionLayer
        deny = CLIExecutionLayer().session_denied_tools(external=True, read_only=False)
        assert {"Bash", "Monitor", "PowerShell"} <= set(deny)
        # The web tools are deliberately not denied (2026-09-08).
        assert not {"WebFetch", "WebSearch"} & set(deny)
        assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"].startswith("/caller/.claude/")
        codex_dir = ensure_persistent_agent_dir(
            "support", execution_path="codex-cli", external_home=home,
        )
        assert codex_dir == home / ".codex"
        assert "/caller/.codex/" in (codex_dir / "hooks.json").read_text()

    def test_symlinked_home_is_refused(self, agent_tree, tmp_path):
        from core.layers.cli.config_dir import ensure_persistent_claude_dir
        agent_dir, _home = agent_tree
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        link = agent_dir / "externals" / "phone" / "777"
        os.symlink(outside, link)
        with pytest.raises(RuntimeError):
            ensure_persistent_claude_dir("support", external_home=link)
        with pytest.raises(RuntimeError):
            ensure_persistent_claude_dir("support", external_home=outside)


class TestPathRolesAndEnv:
    def test_resolve_role_external(self):
        assert path_roles.resolve_role("workspace", external=True) == "/caller/workspace"
        assert path_roles.resolve_role("user_root", external=True) == "/caller"
        assert path_roles.resolve_role("shared_workspace", user_role="viewer", external=True) == ""
        assert path_roles.resolve_role("shared_workspace", user_role="editor", external=True) == "/workspace"
        assert path_roles.resolve_role("config", user_role="manager", external=True) == ""
        assert path_roles.resolve_role("knowledge_dir", external=True) == "/knowledge"
        assert path_roles.resolve_role("credentials_dir", subpath="x", external=True) == "/knowledge/.credentials/x"

    def test_oto_env_with_and_without_tree(self):
        env = build_oto_env(
            agent_name="support", user_role="viewer", session_id="s",
            memory_user_enabled=True, memory_agent_enabled=True, default_scope="agent",
            external=True, external_home_mounted=True,
            external_channel="phone", external_id="+3021", external_verified=True,
        )
        assert env["OTO_USER_ROOT"] == "/caller"
        assert env["OTO_WORKSPACE_DIR"] == "/caller/workspace"
        assert env["OTO_SHARED_WORKSPACE"] == ""
        assert env["OTO_CONFIG_DIR"] == ""
        assert env["OTO_SCOPE"] == "agent"
        assert env["OTO_MEMORY_AGENT_ENABLED"] == "false"
        assert env["OTO_MEMORY_USER_ENABLED"] == "true"
        assert env["OTO_DEFAULT_SCOPE"] == "user"
        assert "/caller" in env["OTO_ALLOWED_ROOTS"].split(":")
        assert env["OTO_EXTERNAL_CHANNEL"] == "phone" and env["OTO_EXTERNAL_ID"] == "+3021"
        assert env["OTO_EXTERNAL_VERIFIED"] == "true"

        env = build_oto_env(
            agent_name="support", user_role="editor", session_id="s",
            memory_user_enabled=True, memory_agent_enabled=True, default_scope="agent",
            external=True, external_home_mounted=False,
        )
        assert env["OTO_USER_ROOT"] == "" and env["OTO_WORKSPACE_DIR"] == "/workspace"
        assert env["OTO_MEMORY_USER_ENABLED"] == "false"
        assert env["OTO_MEMORY_AGENT_ENABLED"] == "false"
        assert env["OTO_DEFAULT_SCOPE"] == "agent"

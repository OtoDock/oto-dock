"""The `/caller` home: mount table, cwd, config dirs, path roles, OTO env.

An outside session (a caller who is not a platform user) gets a mount
table of its own: its home, the knowledge folders a manager picked, no
shared workspace and no knowledge root, the person's folders writable only
with the files pick and the memory never; the Direct-LLM file resolver
sees exactly the same decisions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import config as app_config
from core.layers.direct import files as direct_files
from core.sandbox.oto_env import build_oto_env
from core.sandbox.sandbox import (
    EXTERNAL_SANDBOX_HOME,
    SandboxBuilder,
    SandboxConfig,
    empty_mount_dir,
)
from services import path_roles


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


def _cfg(agent_dir: Path, *, role="external", external=True, home: Path | None = None,
         knowledge_rw=False, files_write=False, knowledge=(), libraries=()) -> SandboxConfig:
    return SandboxConfig(
        role=role, username="", agent_name="support", is_admin_agent=False,
        host_agents_dir=agent_dir.parent, host_mcps_dir=agent_dir.parent,
        host_claude_dir=(home or agent_dir / "workspace") / ".claude",
        config_visible=False, knowledge_rw=knowledge_rw,
        net_forwards=["8400"], external=external,
        external_home=str(home) if home else "",
        outside_files_write=files_write, outside_knowledge=tuple(knowledge),
        knowledge_libraries=list(libraries),
    )


def _by_dest(mounts):
    return {m.sandbox: m for m in mounts}


class TestMountTable:
    def test_nothing_picked_is_the_home_alone(self, agent_tree):
        """An outside session with nothing picked: its own home read-only,
        the CLI state dirs read-write, no shared workspace, no knowledge, the
        agent scope's CLI state masked."""
        agent_dir, home = agent_tree
        (home / ".claude").mkdir()
        b = SandboxBuilder(_cfg(agent_dir, home=home))
        mounts = _by_dest(b.workspace_mount_table())
        assert "/workspace" not in mounts and "/knowledge" not in mounts
        assert not any(d.startswith("/knowledge/") for d in mounts)
        assert "/config" not in mounts and not any(d.startswith("/users") for d in mounts)
        assert mounts[EXTERNAL_SANDBOX_HOME] == mounts["/caller"]
        assert mounts["/caller"].rw is False and mounts["/caller"].host == str(home)
        # The files pick at Read only: nothing of the person's tree is writable.
        assert "/caller/workspace" not in mounts and "/caller/context" not in mounts
        assert mounts["/caller/.claude"].rw is True
        assert (home / "workspace").is_dir() and (home / "context" / "memory").is_dir()
        assert mounts["/workspace/.claude"].host == str(empty_mount_dir())
        assert b.get_cwd() == "/caller"
        assert b.get_env_overrides()["CLAUDE_CONFIG_DIR"] == "/caller/.claude"

    def test_the_files_pick_opens_the_persons_folders_but_never_the_memory(self, agent_tree):
        agent_dir, home = agent_tree
        mounts = _by_dest(SandboxBuilder(_cfg(agent_dir, home=home, files_write=True)).workspace_mount_table())
        assert mounts["/caller"].rw is False
        assert mounts["/caller/workspace"].rw and mounts["/caller/context"].rw
        assert mounts["/caller/context/memory"].rw is False
        assert mounts["/caller/context/memory"].host == str(home / "context" / "memory")
        assert "/workspace" not in mounts

    def test_the_knowledge_picks_bind_at_their_own_place(self, agent_tree):
        agent_dir, home = agent_tree
        for sub in ("faq", "prices", "knowledge/shared/lib-a/docs"):
            (agent_dir / "knowledge" / sub if "/" not in sub else agent_dir / sub).mkdir(parents=True, exist_ok=True)
        cfg = _cfg(agent_dir, home=home,
                   knowledge=("faq", "shared/lib-a/docs", "memory", "gone", "../etc", ".credentials",
                              "shared/lib-b", "faq/sub"),
                   libraries=[("lib-a", "docs", False)])
        mounts = _by_dest(SandboxBuilder(cfg).workspace_mount_table())
        assert mounts["/knowledge/faq"].rw is False and mounts["/knowledge/faq"].host == str(agent_dir / "knowledge" / "faq")
        assert mounts["/knowledge/shared/lib-a/docs"].rw is False
        assert "/knowledge" not in mounts and "/knowledge/prices" not in mounts
        # The memory, a missing folder, a path outside, the credentials, an
        # unattached library and a nested path bind nothing.
        for bad in ("/knowledge/memory", "/knowledge/gone", "/knowledge/.credentials", "/knowledge/shared/lib-b",
                    "/knowledge/faq/sub"):
            assert bad not in mounts
        assert not any(".." in d for d in mounts)

    def test_a_linked_knowledge_folder_binds_nothing(self, agent_tree, tmp_path):
        agent_dir, home = agent_tree
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, agent_dir / "knowledge" / "linked")
        mounts = _by_dest(SandboxBuilder(_cfg(agent_dir, home=home, knowledge=("linked",))).workspace_mount_table())
        assert "/knowledge/linked" not in mounts

    def test_a_subfolder_binds_alone_at_its_own_place(self, agent_tree):
        """A folder inside a top-level folder or inside an attached library
        binds alone, read-only: its parent and its siblings stay out."""
        agent_dir, home = agent_tree
        k = agent_dir / "knowledge"
        for sub in ("projects/alpha/specs", "projects/beta", "shared/lib-a/docs/api", "shared/lib-a/docs/old"):
            (k / sub).mkdir(parents=True)
        cfg = _cfg(agent_dir, home=home, knowledge=("projects/alpha", "shared/lib-a/docs/api"),
                   libraries=[("lib-a", "docs", False)])
        mounts = _by_dest(SandboxBuilder(cfg).workspace_mount_table())
        knowledge = sorted(d for d in mounts if d.startswith("/knowledge"))
        assert knowledge == ["/knowledge/projects/alpha", "/knowledge/shared/lib-a/docs/api"]
        assert mounts["/knowledge/projects/alpha"].host == str(k / "projects" / "alpha")
        assert all(mounts[d].rw is False for d in knowledge)

    def test_a_subfolder_never_reaches_the_memory_the_credentials_or_a_folder_gone(self, agent_tree, tmp_path):
        agent_dir, home = agent_tree
        k = agent_dir / "knowledge"
        (k / "memory" / "topics").mkdir()
        (k / ".credentials" / "github").mkdir(parents=True)
        (k / "projects").mkdir()
        outside = tmp_path / "outside"
        (outside / "inner").mkdir(parents=True)
        os.symlink(outside, k / "projects" / "link")
        # Folders on disk outside every attachment (a mirror a detach left
        # behind): the attachment rule alone refuses them.
        (k / "shared" / "lib-a" / "other").mkdir(parents=True)
        (k / "shared" / "lib-b").mkdir(parents=True)
        cfg = _cfg(agent_dir, home=home,
                   knowledge=("memory/topics", ".credentials/github", "projects/link", "projects/link/inner",
                              "projects/gone", "shared/lib-a/docs/missing", "shared/lib-a/other", "shared/lib-b",
                              "/projects", "projects/../memory"),
                   libraries=[("lib-a", "docs", False)])
        mounts = _by_dest(SandboxBuilder(cfg).workspace_mount_table())
        assert not any(d.startswith("/knowledge") for d in mounts)
        # A library's folder that is not there is never made.
        assert not (k / "shared" / "lib-a" / "docs" / "missing").exists()

    def test_the_role_word_changes_nothing(self, agent_tree):
        """A legacy context carrying a member's role word: still no shared workspace."""
        agent_dir, home = agent_tree
        for role in ("viewer", "editor", "manager"):
            mounts = _by_dest(SandboxBuilder(_cfg(agent_dir, role=role, home=home, knowledge_rw=True)).workspace_mount_table())
            assert "/workspace" not in mounts and "/knowledge" not in mounts and "/config" not in mounts

    def test_no_home_binds_the_masks_alone(self, agent_tree):
        """A context with no home (a remote target's, defensive): nothing of
        the agent is bound."""
        agent_dir, _home = agent_tree
        b = SandboxBuilder(_cfg(agent_dir))
        mounts = _by_dest(b.workspace_mount_table())
        assert set(mounts) == {"/workspace/.claude", "/workspace/.codex"}

    def test_non_external_sessions_are_untouched(self, agent_tree):
        agent_dir, _home = agent_tree
        mounts = _by_dest(SandboxBuilder(_cfg(agent_dir, role="manager", external=False)).workspace_mount_table())
        assert mounts["/workspace"].rw and mounts["/knowledge"].rw is False and "/caller" not in mounts

    def test_symlinked_home_is_refused(self, agent_tree, tmp_path):
        agent_dir, home = agent_tree
        outside = tmp_path / "outside"
        outside.mkdir()
        link = agent_dir / "externals" / "phone" / "999"
        os.symlink(outside, link)
        with pytest.raises(RuntimeError):
            SandboxBuilder(_cfg(agent_dir, home=link)).workspace_mount_table()

    def test_the_direct_text_check_sees_the_same_decisions(self, agent_tree):
        """The Direct LLM file tools read this table as text for an early
        refusal, and their program runs under its mounts: the text check
        admits and refuses what the table says, and the record of a written
        file names its place on the server."""
        agent_dir, home = agent_tree
        (agent_dir / "knowledge" / "faq").mkdir()
        (agent_dir / "knowledge" / "faq" / "a.md").write_text("a")
        cfg = _cfg(agent_dir, home=home, files_write=True, knowledge=("faq",))
        mounts = direct_files.mount_table(cfg)
        cwd = direct_files.session_cwd(cfg)
        assert cwd == "/caller"

        def host_of(table, raw, *, writing):
            vpath, _row = direct_files.locate(table, raw, cwd=cwd, writing=writing)
            return direct_files.record_place(table, vpath, "support")[1]

        assert host_of(mounts, "workspace/notes.txt", writing=True) == home / "workspace" / "notes.txt"
        assert host_of(mounts, "/knowledge/faq/a.md", writing=False) == agent_dir / "knowledge" / "faq" / "a.md"
        with pytest.raises(direct_files.FileToolError):
            direct_files.locate(mounts, "/knowledge/memory/MEMORY.md", cwd=cwd)
        with pytest.raises(direct_files.FileToolError):
            direct_files.locate(mounts, "/caller/context/memory/x.md", cwd=cwd, writing=True)
        with pytest.raises(direct_files.FileToolError):
            direct_files.locate(mounts, "/workspace/shared.txt", cwd=cwd)
        sub = _cfg(agent_dir, home=home, knowledge=("projects/alpha",))
        (agent_dir / "knowledge" / "projects" / "alpha").mkdir(parents=True)
        (agent_dir / "knowledge" / "projects" / "beta").mkdir()
        (agent_dir / "knowledge" / "projects" / "alpha" / "a.md").write_text("a")
        sub_mounts = direct_files.mount_table(sub)
        assert str(host_of(sub_mounts, "/knowledge/projects/alpha/a.md", writing=False)).endswith("projects/alpha/a.md")
        for refused in ("/knowledge/projects/beta", "/knowledge/projects", "/knowledge/faq/a.md"):
            with pytest.raises(direct_files.FileToolError):
                direct_files.locate(sub_mounts, refused, cwd=cwd)
        with pytest.raises(direct_files.FileToolError):
            direct_files.locate(sub_mounts, "/knowledge/projects/alpha/new.md", cwd=cwd, writing=True)
        read_only = direct_files.mount_table(_cfg(agent_dir, home=home))
        with pytest.raises(direct_files.FileToolError):
            direct_files.locate(read_only, "workspace/notes.txt", cwd=cwd, writing=True)


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
        # The web tools are denied until a manager picks the web for outside people.
        assert {"WebFetch", "WebSearch"} <= set(deny)
        from core.sandbox.session_config_dir import sandbox_python
        assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == (
            f'{sandbox_python()} -I "/caller/.claude/permission_gate.py"')
        codex_dir = ensure_persistent_agent_dir(
            "support", execution_path="codex-cli", external_home=home,
        )
        assert codex_dir == home / ".codex"
        # Codex's hooks file is the start's (``config_dir.write_hooks``, in the
        # folder the engine reads), naming the scripts by the folder as the
        # caller's sandbox sees it; the funnel lays the scripts down.
        from core.layers.codex.config_dir import write_hooks
        from core.sandbox.session_config_dir import scope_config_dir
        assert not (codex_dir / "hooks.json").exists() and (codex_dir / "permission_gate.py").is_file()
        _host, seen = scope_config_dir("support", ".codex", external_home=home)
        write_hooks(codex_dir, seen)
        assert "/caller/.codex/" in (codex_dir / "hooks.json").read_text()

    def test_the_outside_homes_skills_follow_the_picks(self, agent_tree, monkeypatch):
        """Both config dir builders hand the picks to the materializer for an
        outside home, so the skills a caller can load are the picked tools'."""
        from core.layers.cli.config_dir import ensure_persistent_claude_dir
        from core.layers.codex.config_dir import ensure_persistent_codex_dir
        from core.sandbox import skills_materializer
        from services.mcp import outside_picks
        _agent_dir, home = agent_tree
        seen: list = []
        monkeypatch.setattr(skills_materializer, "materialize_skills_for_sandbox",
                            lambda agent, config_dir, **kw: seen.append((config_dir.name, kw)))
        picks = outside_picks.OutsidePicks(tools={"crm": outside_picks.ToolPick("read_only", ())})
        ensure_persistent_claude_dir("support", external_home=home, outside_picks=picks)
        ensure_persistent_codex_dir("support", external_home=home, outside_picks=picks)
        ensure_persistent_claude_dir("support", username="", scope="agent")
        assert seen == [(".claude", {"username": "", "outside_picks": picks, "kind_rule": None}),
                        (".codex", {"username": "", "outside_picks": picks, "kind_rule": None}),
                        (".claude", {"username": "", "outside_picks": None, "kind_rule": None})]

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


class TestCodexConfig:
    def test_enabled_tools_follow_the_grants(self):
        from core.layers.codex.layer import _inject_enabled_tools_toml
        content = (
            '[mcp_servers.crm]\ncommand = "x"\n\n'
            '[mcp_servers.crm.env]\nA = "1"\n\n'
            '[mcp_servers.files]\ncommand = "y"\n'
        )
        out = _inject_enabled_tools_toml(content, (("crm", ("find", "list")), ("files", None)))
        lines = out.splitlines()
        assert lines[lines.index("[mcp_servers.crm]") + 1] == 'enabled_tools = ["find", "list"]'
        assert out.count("enabled_tools") == 1
        # A pick naming no server, or picks naming servers whole, change nothing.
        assert _inject_enabled_tools_toml(content, ()) == content
        assert _inject_enabled_tools_toml(content, (("files", None),)) == content

    def test_the_apps_connector_is_off_for_an_outside_session(self, tmp_path):
        from core.layers.codex.layer import CodexCLIExecutionLayer
        for apps_off in (True, False):
            config_dir = tmp_path / ("outside" if apps_off else "own")
            config_dir.mkdir()
            _text, own = CodexCLIExecutionLayer._write_config_toml(config_dir, "P", apps_off=apps_off)
            # The session's own switch, never in the home's shared file.
            assert (own.get("features", {}).get("apps") is False) is apps_off
            assert "apps =" not in (config_dir / "config.toml").read_text()

    def test_the_web_key_is_written_only_for_an_outside_session_without_the_pick(self, tmp_path):
        from core.layers.codex.layer import CodexCLIExecutionLayer
        for web_off in (True, False):
            config_dir = tmp_path / ("off" if web_off else "on")
            config_dir.mkdir()
            _text, own = CodexCLIExecutionLayer._write_config_toml(
                config_dir, "P", no_shell=True, web_off=web_off, session_id="s-1", interactive=True)
            # The session's own keys: its profile (root keys before its
            # tables) and its thread map; the home's file has neither.
            profile = (config_dir / "s-1.config.toml").read_text()
            lines = profile.splitlines()
            first_table = next(i for i, line in enumerate(lines) if line.startswith("["))
            assert ('web_search = "disabled"' in lines[:first_table]) is web_off
            assert (own.get("web_search") == "disabled") is web_off
            assert own["features"]["shell_tool"] is False
            shared = (config_dir / "config.toml").read_text()
            assert "web_search" not in shared and "shell_tool" not in shared
            assert shared.count("[tools]") == 1

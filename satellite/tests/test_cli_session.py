"""Tests for CLI session management."""

import asyncio
import contextlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from satellite import config
from satellite.sessions import sandbox_host
from satellite.sessions.cli_session import CLISession, _write_cli_hooks


@pytest.fixture
def tmp_agent_dir(tmp_path):
    agent_dir = tmp_path / "agents" / "test-agent"
    agent_dir.mkdir(parents=True)
    return agent_dir


@pytest.fixture
def sat_config():
    from satellite.config import SatelliteConfig
    return SatelliteConfig(
        machine_id="test-machine",
        machine_secret="test-secret",
        platform_url="ws://localhost:8400/v1/satellite",
        agents_dir=Path("/tmp/test-agents"),
        mcps_dir=Path("/tmp/test-mcps"),
    )


@pytest.fixture
def cli_config():
    return {
        "cwd_relative": "users/alice",
        "claude_dir_relative": "users/alice/.claude",
        "system_prompt": "You are a test agent.",
        "mcp_config": {"mcpServers": {}},
        "model": "claude-sonnet-5",
        "effort": "high",
        "env": {
            "PROXY_URL": "http://100.1.2.3:8400",
            "PROXY_API_KEY": "test-key",
            "OTO_CLI_TOKEN": "cli-test-token",
            "ANTHROPIC_API_KEY": "sk-test",
        },
    }


class TestWriteCliHooks:
    def test_writes_settings_json(self, tmp_path):
        _write_cli_hooks(tmp_path)
        settings_file = tmp_path / "settings.json"
        assert settings_file.exists()
        settings = json.loads(settings_file.read_text())
        assert "hooks" in settings
        # Hook parity (0.5.121): the same four events the proxy's
        # _build_sandbox_cli_settings writes for the local sandbox.
        # PostToolUseFailure: Claude Code runs it instead of PostToolUse for a
        # call that failed, and the forwarder shows that result too.
        assert set(settings["hooks"]) == {"PreToolUse", "PostToolUse", "PostToolUseFailure",
                                          "SubagentStop", "Stop"}
        stop = settings["hooks"]["Stop"][0]["hooks"][0]
        assert "stop_tracker.py" in stop["command"] and stop["timeout"] == config.HOOK_WAIT_S
        # The gate blocks the call when it cannot run (Claude Code 2.1.295+,
        # the proxy builder writes the same); the trackers never block.
        assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["onFailure"] == "block"
        for event in ("PostToolUse", "PostToolUseFailure", "SubagentStop", "Stop"):
            assert "onFailure" not in settings["hooks"][event][0]["hooks"][0], event
        for event in ("PostToolUse", "PostToolUseFailure"):
            hook = settings["hooks"][event][0]["hooks"][0]
            assert "tool_result_forwarder.py" in hook["command"] and hook["timeout"] == 10
        # Claude Code ≥ 2.1.275 would sync the pool account's claude.ai skills
        # and plugins into the session — both off (mirrors the proxy builder).
        assert settings["syncClaudeAiSkills"] is False
        assert settings["syncClaudeAiPlugins"] is False
        assert settings["disableClaudeAiConnectors"] is True
        assert settings["useAutoModeDuringPlan"] is False
        # Every built-in plugin but the four the platform keeps (Claude Code
        # 2.1.287+) is off by name, as the proxy builder writes them.
        from satellite.sessions.cli_session import DISABLED_BUILTIN_PLUGINS
        assert settings["enabledPlugins"] == {p: False for p in DISABLED_BUILTIN_PLUGINS}
        assert "cc-plugin-plugin-authoring@builtin" in DISABLED_BUILTIN_PLUGINS
        assert "cc-plugin-easel@builtin" in DISABLED_BUILTIN_PLUGINS
        assert "cc-plugin-claude-platform@builtin" in DISABLED_BUILTIN_PLUGINS
        assert "cc-plugin-privacy@builtin" in DISABLED_BUILTIN_PLUGINS

    def test_the_start_entries_come_when_the_platform_asks(self, tmp_path):
        """One SessionStart entry per part of the start's riding part
        (Claude Code inlines a hook's context only up to 10,000 characters),
        only when the start payload asks; none for a platform that does not."""
        _write_cli_hooks(tmp_path)
        assert "SessionStart" not in json.loads((tmp_path / "settings.json").read_text())["hooks"]
        _write_cli_hooks(tmp_path, None, 3)
        starts = json.loads((tmp_path / "settings.json").read_text())["hooks"]["SessionStart"]
        cmds = [h["command"] for h in starts[0]["hooks"]]
        assert len(cmds) == 3 and all("start_context.py" in c for c in cmds)
        assert [c.rsplit(" ", 2)[-2:] for c in cmds] == [["1", "3"], ["2", "3"], ["3", "3"]]

    def test_hook_paths_point_to_dir(self, tmp_path):
        _write_cli_hooks(tmp_path)
        settings = json.loads((tmp_path / "settings.json").read_text())
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert str(tmp_path) in cmd
        assert "permission_gate.py" in cmd


class TestCLISessionInit:
    def test_creates_directories(self, tmp_agent_dir, cli_config, sat_config):
        session = CLISession("sess-1", tmp_agent_dir, cli_config, sat_config)
        # Directories aren't created until start() is called
        assert session.session_id == "sess-1"
        assert session.agent_dir == tmp_agent_dir
        assert session.proc is None

    def test_execution_path(self, tmp_agent_dir, cli_config, sat_config):
        session = CLISession("sess-1", tmp_agent_dir, cli_config, sat_config)
        assert session.execution_path == "claude-code-cli"


class TestCLISessionStart:
    @pytest.mark.asyncio
    async def test_creates_config_files(self, tmp_agent_dir, cli_config, sat_config):
        session = CLISession("sess-1", tmp_agent_dir, cli_config, sat_config)

        # Mock subprocess to avoid actually spawning claude
        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")  # EOF — stop _drain_stderr

        # Simulate init event
        init_event = json.dumps({
            "type": "system", "subtype": "init", "mcp_servers": [],
        }).encode() + b"\n"
        mock_proc.stdout.readline = AsyncMock(return_value=init_event)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await session.start()

        claude_dir = tmp_agent_dir / "users" / "alice" / ".claude"
        assert claude_dir.is_dir()
        from satellite.sessions.cli_session import prompt_filename
        assert (claude_dir / prompt_filename("sess-1")).exists()
        assert not (claude_dir / "system-prompt.md").exists()
        assert (claude_dir / "mcp-config-sess-1.json").exists()
        assert not (claude_dir / "mcp-config.json").exists()
        assert (claude_dir / "settings.json").exists()

        # Verify prompt content
        assert (claude_dir / prompt_filename("sess-1")).read_text() == "You are a test agent."
        # API-key session — no OAuth credential file to write.
        assert not (claude_dir / ".credentials.json").exists()

    @pytest.mark.asyncio
    async def test_writes_credentials_json_for_oauth(
        self, tmp_agent_dir, cli_config, sat_config,
    ):
        blob = {"claudeAiOauth": {"accessToken": "at", "refreshToken": ""}}
        cli_config = {**cli_config, "credentials_json": blob}
        session = CLISession("sess-1", tmp_agent_dir, cli_config, sat_config)

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        init_event = json.dumps({
            "type": "system", "subtype": "init", "mcp_servers": [],
        }).encode() + b"\n"
        mock_proc.stdout.readline = AsyncMock(return_value=init_event)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await session.start()

        creds = tmp_agent_dir / "users" / "alice" / ".claude" / ".credentials.json"
        assert json.loads(creds.read_text()) == blob

    @pytest.mark.asyncio
    async def test_builds_correct_command(self, tmp_agent_dir, cli_config, sat_config):
        session = CLISession("sess-1", tmp_agent_dir, cli_config, sat_config)

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")  # EOF — stop _drain_stderr
        init_event = json.dumps({"type": "system", "subtype": "init", "mcp_servers": []}).encode() + b"\n"
        mock_proc.stdout.readline = AsyncMock(return_value=init_event)

        captured_cmd = None
        captured_env = None

        async def capture_exec(*args, **kwargs):
            nonlocal captured_cmd, captured_env
            captured_cmd = args
            captured_env = kwargs.get("env", {})
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            await session.start()

        # The engine starts below the satellite's priority and, on Linux,
        # behind the raise of its memory-kill score
        # (``sandbox_host.paired_command``): ``nice`` and the shell exec it.
        head = sandbox_host.paired_command([])
        assert head[:3] == sandbox_host.niced([]) and head[3:] in (
            [], ["/bin/sh", "-c", sandbox_host._RAISE_OOM, "oto-session"])
        assert list(captured_cmd[:len(head)]) == head
        captured_cmd = captured_cmd[len(head):]
        # Bare names now pass through pin-verified resolution, which
        # which()-resolves them — accept bare or absolute.
        assert captured_cmd[0] == "claude" or captured_cmd[0].endswith("/claude")
        assert "-p" in captured_cmd
        assert "--model" in captured_cmd
        assert "claude-sonnet-5" in captured_cmd
        assert "--effort" in captured_cmd
        assert "high" in captured_cmd
        assert "--dangerously-skip-permissions" in captured_cmd
        # Bypass headless carries the stdio prompt tool (exposes
        # AskUserQuestion to -p — mirrors the local CLI layer).
        assert "--permission-prompt-tool" in captured_cmd
        assert captured_cmd[
            captured_cmd.index("--permission-prompt-tool") + 1] == "stdio"
        assert "--session-id" in captured_cmd
        assert "sess-1" in captured_cmd
        assert "--output-format" in captured_cmd
        assert "stream-json" in captured_cmd
        # Only the platform's settings.json is read (mirrors the local layer).
        assert captured_cmd[captured_cmd.index("--setting-sources") + 1] == "user"

        # Verify env
        assert captured_env["OTO_SESSION_ID"] == "sess-1"
        assert captured_env["PROXY_URL"] == "http://100.1.2.3:8400"
        assert captured_env["ANTHROPIC_API_KEY"] == "sk-test"
        # The shell's command-line token rides the payload env as it is: the
        # platform mints it, every satellite only passes it on.
        assert captured_env["OTO_CLI_TOKEN"] == "cli-test-token"
        assert "CLAUDECODE" not in captured_env
        # The credential helper's variables and the wrappers' dir first on
        # PATH, set after the path translation (a machine path as it is).
        import os
        import sys
        claude_dir = tmp_agent_dir / "users" / "alice" / ".claude"
        assert captured_env["OTO_HOOKS_DIR"] == str(claude_dir)
        assert captured_env["OTO_HOOK_PY"] == sys.executable
        assert captured_env["PATH"].startswith(str(claude_dir / "bin") + os.pathsep)

    @pytest.mark.asyncio
    async def test_writes_the_command_wrappers_the_payload_carries(
        self, tmp_agent_dir, cli_config, sat_config,
    ):
        """``command_wrappers`` land under the config dir's bin/ with their
        directory created, executable, bytes as sent; a name that escapes the
        dir is dropped."""
        cli_config["command_wrappers"] = {
            "bin/gh": "#!/bin/sh\nexec x \"$@\"\n",
            "bin/gh.cmd": "@echo off\r\nx %*\r\n",
            "../evil": "no", "/abs/evil": "no", "": "no", "bin/empty": "",
        }
        session = CLISession("sess-1", tmp_agent_dir, cli_config, sat_config)
        mock_proc = AsyncMock()
        mock_proc.pid = 1
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        init_event = json.dumps({"type": "system", "subtype": "init", "mcp_servers": []}).encode() + b"\n"
        mock_proc.stdout.readline = AsyncMock(return_value=init_event)
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await session.start()
        claude_dir = tmp_agent_dir / "users" / "alice" / ".claude"
        gh = claude_dir / "bin" / "gh"
        assert gh.read_bytes() == b"#!/bin/sh\nexec x \"$@\"\n" and gh.stat().st_mode & 0o111
        assert (claude_dir / "bin" / "gh.cmd").read_bytes() == b"@echo off\r\nx %*\r\n"
        assert not (claude_dir / "bin" / "empty").exists()
        assert not (tmp_agent_dir / "users" / "alice" / "evil").exists()
        assert not Path("/abs/evil").exists()  # noqa: ASYNC240  # a test's own temporary files

    @pytest.mark.asyncio
    async def test_resume_uses_resume_flag(self, tmp_agent_dir, sat_config):
        config = {
            "cwd_relative": "users/alice",
            "claude_dir_relative": "users/alice/.claude",
            "system_prompt": "test",
            "mcp_config": {},
            "model": "claude-sonnet-5",
            "env": {},
            "resume": True,
            "session_id_for_resume": "old-sess-123",
        }
        session = CLISession("sess-2", tmp_agent_dir, config, sat_config)

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")  # EOF — stop _drain_stderr
        init_event = json.dumps({"type": "system", "subtype": "init", "mcp_servers": []}).encode() + b"\n"
        mock_proc.stdout.readline = AsyncMock(return_value=init_event)

        captured_cmd = None

        async def capture_exec(*args, **kwargs):
            nonlocal captured_cmd
            captured_cmd = args
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            await session.start()

        assert "--resume" in captured_cmd
        assert "old-sess-123" in captured_cmd
        assert "--session-id" not in captured_cmd


async def _capture_start_cmd(session):
    """Spawn the session with a mocked subprocess and return the argv tuple."""
    mock_proc = AsyncMock()
    mock_proc.pid = 12345
    mock_proc.returncode = None
    mock_proc.stdout = AsyncMock()
    mock_proc.stderr = AsyncMock()
    mock_proc.stderr.read = AsyncMock(return_value=b"")  # EOF — stop _drain_stderr
    init_event = json.dumps({"type": "system", "subtype": "init", "mcp_servers": []}).encode() + b"\n"
    mock_proc.stdout.readline = AsyncMock(return_value=init_event)
    captured = {}

    async def capture_exec(*args, **kwargs):
        captured["cmd"] = args
        captured["env"] = kwargs.get("env") or {}
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
        await session.start()
    session._captured_env = captured["env"]
    return captured["cmd"]


class TestCLISessionFlagParity:
    """Satellite argv must mirror the local CLI layer for
    permission/plan mode, thinking tokens, and resume prompt handling."""

    @pytest.mark.asyncio
    async def test_plan_mode_uses_permission_mode_plan(self, tmp_agent_dir, cli_config, sat_config):
        cfg = {**cli_config, "permission_mode": "plan"}
        cmd = await _capture_start_cmd(CLISession("sess-p", tmp_agent_dir, cfg, sat_config))
        assert "--permission-mode" in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "plan"
        assert "--dangerously-skip-permissions" not in cmd
        # Claude Code 2.1.299+ asks for a call the hook allowed in plan
        # mode; the proxy answers that ask from the gate's record.
        assert cmd[cmd.index("--permission-prompt-tool") + 1] == "stdio"

    @pytest.mark.asyncio
    async def test_the_advisor_tool_is_off(self, tmp_agent_dir, cli_config, sat_config):
        """Claude Code's advisor is a model call the platform can neither
        meter nor show (mirrors the proxy env builder)."""
        session = CLISession("sess-adv", tmp_agent_dir, cli_config, sat_config)
        await _capture_start_cmd(session)
        assert session._captured_env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"] == "1"
        # Tools that really start connect together (mirrors the proxy env builder).
        assert 3 <= int(session._captured_env["MCP_SERVER_CONNECTION_BATCH_SIZE"]) <= 16

    @pytest.mark.asyncio
    async def test_native_permissions_uses_mode(self, tmp_agent_dir, cli_config, sat_config):
        cfg = {**cli_config, "use_native_permissions": True, "permission_mode": "acceptEdits"}
        cmd = await _capture_start_cmd(CLISession("sess-n", tmp_agent_dir, cfg, sat_config))
        assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
        assert "--dangerously-skip-permissions" not in cmd
        assert "--permission-prompt-tool" not in cmd

    @pytest.mark.asyncio
    async def test_default_mode_skips_permissions(self, tmp_agent_dir, cli_config, sat_config):
        cmd = await _capture_start_cmd(CLISession("sess-d", tmp_agent_dir, cli_config, sat_config))
        assert "--dangerously-skip-permissions" in cmd
        assert "--permission-mode" not in cmd
        assert cmd[cmd.index("--permission-prompt-tool") + 1] == "stdio"

    @pytest.mark.asyncio
    async def test_auto_runs_the_clis_own_auto_mode(self, tmp_agent_dir, cli_config, sat_config):
        """A 1.9 proxy ships the CLI's own word: Auto starts its auto mode
        (a call the hook leaves undecided goes to the CLI's classifier), the
        stdio prompt tool kept for the decline limit."""
        cfg = {**cli_config, "permission_mode": "autoReview", "cli_permission_mode": "auto"}
        cmd = await _capture_start_cmd(CLISession("sess-a", tmp_agent_dir, cfg, sat_config))
        assert cmd[cmd.index("--permission-mode") + 1] == "auto"
        assert "--dangerously-skip-permissions" not in cmd
        assert cmd[cmd.index("--permission-prompt-tool") + 1] == "stdio"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cfg", [
        {"permission_mode": "ask", "cli_permission_mode": "default"},
        {"permission_mode": "dontAsk", "cli_permission_mode": "dontAsk"},
        {"permission_mode": "acceptEdits"},  # a proxy before 1.9
    ])
    async def test_every_other_mode_leaves_the_hook_to_decide(
            self, tmp_agent_dir, cli_config, sat_config, cfg):
        cmd = await _capture_start_cmd(
            CLISession("sess-o", tmp_agent_dir, {**cli_config, **cfg}, sat_config))
        assert "--dangerously-skip-permissions" in cmd
        assert "--permission-mode" not in cmd

    def test_the_terminal_starts_in_auto_and_plan_by_itself(self):
        from satellite.terminal.pty_session import terminal_permission_mode
        for cli_word, start in [("auto", "auto"), ("plan", "plan"), ("default", "default"),
                                ("dontAsk", "default")]:
            assert terminal_permission_mode({"cli_permission_mode": cli_word}) == start
        # A proxy before 1.9 ships the platform word alone.
        assert terminal_permission_mode({"permission_mode": "acceptEdits"}) == "acceptEdits"
        assert terminal_permission_mode({"permission_mode": "dontAsk"}) == "default"

    def test_a_terminal_on_an_api_key_signs_in_through_the_key_helper(self):
        """The TUI reads a key in its environment as one a person must
        approve on screen ("Not logged in" until then): the key moves to the
        helper's variable and the helper prints it, with nothing on disk."""
        import json as _json
        import subprocess
        import sys

        from satellite.terminal.pty_session import KEY_HELPER_ENV, key_helper_args
        env = {"ANTHROPIC_API_KEY": "sk-ant-x", "PATH": "/usr/bin"}
        args = key_helper_args(env)
        assert args[0] == "--settings" and "ANTHROPIC_API_KEY" not in env
        assert env[KEY_HELPER_ENV] == "sk-ant-x"
        helper = _json.loads(args[1])["apiKeyHelper"]
        assert "sk-ant-x" not in helper and sys.executable in helper
        printed = subprocess.run(helper, shell=True, capture_output=True, text=True, timeout=20,
                                 env={**env, KEY_HELPER_ENV: "sk-ant-x"}, check=True).stdout
        assert printed == "sk-ant-x"
        # A sign-in by file (OAuth) carries no key and no helper.
        assert key_helper_args({"PATH": "/usr/bin"}) == []

    def test_the_otodock_command_sends_the_platforms_word(self):
        from satellite.terminal.otodock_cli import _MODES
        assert {w: _MODES[w] for w in ("ask", "auto", "dontAsk", "plan")} == {
            "ask": "ask", "auto": "autoReview", "dontAsk": "dontAsk", "plan": "plan"}
        assert (_MODES["default"], _MODES["acceptEdits"]) == ("ask", "autoReview")

    @pytest.mark.asyncio
    async def test_max_thinking_tokens_from_config(self, tmp_agent_dir, cli_config, sat_config):
        cfg = {**cli_config, "max_thinking_tokens": 42000}
        cmd = await _capture_start_cmd(CLISession("sess-t", tmp_agent_dir, cfg, sat_config))
        assert cmd[cmd.index("--max-thinking-tokens") + 1] == "42000"

    @pytest.mark.asyncio
    async def test_resume_appends_system_prompt(self, tmp_agent_dir, sat_config):
        # Transcripts persist messages only — the CLI rebuilds its system
        # prompt from each invocation's flags, so resume must re-ship it or
        # the re-warmed session runs with no agent identity.
        cfg = {
            "cwd_relative": "users/alice",
            "claude_dir_relative": "users/alice/.claude",
            "system_prompt": "test", "mcp_config": {},
            "model": "claude-sonnet-5", "env": {},
            "resume": True, "session_id_for_resume": "old-sess-123",
        }
        cmd = await _capture_start_cmd(CLISession("sess-r", tmp_agent_dir, cfg, sat_config))
        assert "--append-system-prompt-file" in cmd
        prompt_file = Path(cmd[cmd.index("--append-system-prompt-file") + 1])
        assert prompt_file.read_text() == "test"  # noqa: ASYNC240  # a test's own temporary files
        assert "--resume" in cmd
        # Claude Code ≥ 2.1.267 would re-send the prompt recorded on the
        # conversation's first request instead of this file — recording off.
        assert cmd[cmd.index("--system-prompt-snapshot") + 1] == "off"


class TestCLISessionDetectChanges:
    def test_detect_no_changes(self, tmp_agent_dir, cli_config, sat_config):
        session = CLISession("sess-1", tmp_agent_dir, cli_config, sat_config)
        session._file_snapshot = {}
        changes = session.detect_file_changes()
        assert changes == []


class _FakeStdin:
    def __init__(self):
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


class _FakeProc:
    """A CLI process whose stdout yields ``lines`` then EOF."""

    def __init__(self, lines: list[dict]):
        self.pid = 4242
        self.returncode = None
        self.stdin = _FakeStdin()
        self.stdout = AsyncMock()
        self.stderr = AsyncMock()
        self._lines = [json.dumps(ln).encode() + b"\n" for ln in lines]
        self.stdout.readline = AsyncMock(side_effect=self._readline)

    async def _readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    async def wait(self):
        return self.returncode


def _frames(proc: _FakeProc) -> list[dict]:
    return [json.loads(w.decode().strip()) for w in proc.stdin.writes]


class TestCLISessionSteer:
    """``steer`` writes a user frame into the RUNNING turn's stdin — the
    satellite half of the proxy's PersistentSession.steer (0.5.128). Strict:
    True only when the frame reached the pipe; the proxy queues otherwise."""

    def _session(self, tmp_agent_dir, cli_config, sat_config, proc):
        session = CLISession("sess-s", tmp_agent_dir, cli_config, sat_config)
        session.proc = proc
        session._stderr_buf = []
        return session

    @pytest.mark.asyncio
    async def test_refused_without_a_live_turn(self, tmp_agent_dir, cli_config, sat_config):
        proc = _FakeProc([])
        session = self._session(tmp_agent_dir, cli_config, sat_config, proc)
        assert await session.steer("hi") is False
        assert proc.stdin.writes == []

    @pytest.mark.asyncio
    async def test_mid_turn_frame_lands_after_the_prompt(
        self, tmp_agent_dir, cli_config, sat_config,
    ):
        proc = _FakeProc([
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": []}},
        ])
        session = self._session(tmp_agent_dir, cli_config, sat_config, proc)
        turn = session.send_message("do the thing")
        assert (await turn.__anext__())["type"] == "system"
        # The loop is open: a steer is accepted, with the chat's sandbox
        # paths rewritten for this host like the prompt's are.
        assert await session.steer(
            "also look at /users/alice/workspace/uploads/photos/p.jpg",
        ) is True
        frames = _frames(proc)
        assert [f["type"] for f in frames] == ["user", "user"]
        assert frames[0]["message"]["content"] == "do the thing"
        content = frames[1]["message"]["content"]
        assert content == (
            f"also look at {tmp_agent_dir}/users/alice/workspace/uploads/photos/p.jpg"
        )
        async for _ in turn:
            pass
        # The loop exited on EOF: nothing reads a turn now.
        assert await session.steer("late") is False
        assert len(proc.stdin.writes) == 2

    @pytest.mark.asyncio
    async def test_refused_when_the_process_is_gone(
        self, tmp_agent_dir, cli_config, sat_config,
    ):
        proc = _FakeProc([{"type": "system", "subtype": "init"}])
        session = self._session(tmp_agent_dir, cli_config, sat_config, proc)
        turn = session.send_message("go")
        await turn.__anext__()
        proc.returncode = 1
        assert await session.steer("hi") is False
        async for _ in turn:
            pass

    @pytest.mark.asyncio
    async def test_empty_text_never_writes(self, tmp_agent_dir, cli_config, sat_config):
        proc = _FakeProc([{"type": "system", "subtype": "init"}])
        session = self._session(tmp_agent_dir, cli_config, sat_config, proc)
        turn = session.send_message("go")
        await turn.__anext__()
        assert await session.steer("") is False
        assert len(proc.stdin.writes) == 1
        async for _ in turn:
            pass


class _SlowProc(_FakeProc):
    """A CLI whose stdout stays open with nothing to read (a turn whose
    ``stop_turn`` never came): ``readline`` blocks like a real pipe, and a
    second concurrent reader raises like asyncio's StreamReader does."""

    def __init__(self):
        super().__init__([])
        self._reading = False
        self.stdout.readline = AsyncMock(side_effect=self._blocking_readline)

    async def _blocking_readline(self) -> bytes:
        if self._reading:
            raise RuntimeError(
                "readuntil() called while another coroutine is already waiting for incoming data")
        self._reading = True
        try:
            await asyncio.sleep(3600)
        finally:
            self._reading = False
        return b""


class TestCLISessionOneReader:
    """A send that arrives while the previous turn's loop still reads (its
    ``stop_turn`` was lost or comes after this send) stops that loop and waits
    for it, so stdout never has two readers."""

    @pytest.mark.asyncio
    async def test_a_new_turn_stops_the_previous_reader_first(
        self, tmp_agent_dir, cli_config, sat_config,
    ):
        proc = _SlowProc()
        session = CLISession("sess-r", tmp_agent_dir, cli_config, sat_config)
        session.proc = proc
        session._stderr_buf = []
        first_events: list = []

        async def drive_first():
            async for ev in session.send_message("one"):
                first_events.append(ev)

        first = asyncio.create_task(drive_first())
        await asyncio.sleep(0.05)
        assert session._turn_active
        second = session.send_message("two")
        nxt = asyncio.create_task(second.__anext__())
        await asyncio.wait_for(first, timeout=5)  # the first loop ended on its own
        for _ in range(100):  # the second turn's write lands right after the first loop lets go
            if len(proc.stdin.writes) >= 2:
                break
            await asyncio.sleep(0.02)
        assert [json.loads(w.decode())["message"]["content"] for w in proc.stdin.writes] == ["one", "two"]
        nxt.cancel()
        with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
            await nxt
        await second.aclose()

    @pytest.mark.asyncio
    async def test_a_drainer_the_previous_turn_starts_is_stopped(
        self, tmp_agent_dir, cli_config, sat_config,
    ):
        """The previous turn's handler starts the post-turn drainer the
        moment its reader lets go (its stop_turn asked for one), before the
        new turn resumes: the new turn stops it before reading."""
        proc = _SlowProc()
        session = CLISession("sess-d", tmp_agent_dir, cli_config, sat_config)
        session.proc = proc
        session._stderr_buf = []
        forwarded: list = []

        async def forward(ev):
            forwarded.append(ev)

        async def drive_first():
            async for _ in session.send_message("one"):
                pass
            session.start_bg_drain(forward)  # the handler, without yielding

        first = asyncio.create_task(drive_first())
        await asyncio.sleep(0.05)
        session.request_stop_turn(drain_bg=True)
        second = session.send_message("two")
        nxt = asyncio.create_task(second.__anext__())
        await asyncio.wait_for(first, timeout=5)
        for _ in range(20):
            if len(proc.stdin.writes) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(proc.stdin.writes) == 2
        assert session._bg_drain_task is None
        nxt.cancel()
        with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
            await nxt
        await second.aclose()
        assert forwarded == []


class _ExitedProc(_FakeProc):
    """A CLI that printed ``lines`` and exited with ``code``: the exit is
    already known when the reader looks, and its lines still sit in the pipe."""

    def __init__(self, lines: list[dict], code: int):
        super().__init__(lines)
        self.returncode = code

    async def wait(self) -> int:
        return self.returncode


class _LiveProc(_FakeProc):
    """A CLI that runs until the test ends it. Its stdout says ``lines``,
    then nothing more while it runs."""

    def __init__(self, lines: list[dict] | None = None):
        super().__init__(lines or [])
        self._ended = asyncio.Event()

    async def _readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        await self._ended.wait()
        return b""

    def end(self, code: int) -> None:
        self.returncode = code
        self._ended.set()

    def send_signal(self, _sig) -> None:
        self.end(-2)

    terminate = kill = lambda self: self.end(-9)

    async def wait(self) -> int:
        await self._ended.wait()
        return self.returncode


class TestCLISessionExitBetweenTurns:
    """The engine's process ended with no turn reading it. Nothing noticed
    before: the platform sent the next message to a process that was gone,
    and its chat read that the machine was lost. The session tells it once,
    for an end it did not make itself."""

    def _session(self, tmp_agent_dir, cli_config, sat_config, proc):
        session = CLISession("sess-w", tmp_agent_dir, cli_config, sat_config)
        session.proc = proc
        session._stderr_buf = []
        told: list = []

        async def _told(code):
            told.append(code)
        session.on_exit = _told
        session.watch_exit()
        return session, told

    @staticmethod
    async def _settle() -> None:
        for _ in range(5):
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_an_exit_with_no_turn_reading_is_told_once(self, tmp_agent_dir, cli_config, sat_config):
        proc = _LiveProc()
        _session, told = self._session(tmp_agent_dir, cli_config, sat_config, proc)
        await self._settle()
        assert told == []                                   # it runs: nothing to tell
        proc.end(137)
        await self._settle()
        assert told == [137]

    @pytest.mark.asyncio
    async def test_an_exit_inside_a_turn_is_the_turns_own_end(self, tmp_agent_dir, cli_config, sat_config):
        proc = _LiveProc([{"type": "assistant"}])
        session, told = self._session(tmp_agent_dir, cli_config, sat_config, proc)
        events = session.send_message("go")
        assert await events.__anext__() == {"type": "assistant"}
        proc.end(3)
        assert [ev async for ev in events] == []
        await self._settle()
        assert session.take_turn_end()["reason"] == "exited"
        assert told == []

    @pytest.mark.asyncio
    async def test_an_exit_as_a_turn_is_stopped_is_told_after_the_turn(self, tmp_agent_dir, cli_config, sat_config):
        """The turn's reader left on the platform's stop and never read the
        exit: the turn's end says nothing of it, so the session does."""
        proc = _LiveProc([{"type": "result"}])
        session, told = self._session(tmp_agent_dir, cli_config, sat_config, proc)
        events = session.send_message("go")
        assert await events.__anext__() == {"type": "result"}
        session.request_stop_turn()
        proc.returncode = 1                                 # gone, and the reader looks at the stop first
        proc._ended.set()
        await self._settle()
        assert told == []                                   # the turn is not over yet
        assert [ev async for ev in events] == []
        await self._settle()
        assert session.take_turn_end() == {} and told == [1]

    @pytest.mark.asyncio
    async def test_a_stop_that_ends_the_process_is_the_sessions_own_end(self, tmp_agent_dir, cli_config, sat_config):
        proc = _LiveProc()
        session, told = self._session(tmp_agent_dir, cli_config, sat_config, proc)
        await session.abort()
        await self._settle()
        assert proc.returncode is not None and told == []

    @pytest.mark.asyncio
    async def test_a_close_is_the_sessions_own_end(self, tmp_agent_dir, cli_config, sat_config, monkeypatch):
        from satellite.sessions import cli_session
        monkeypatch.setattr(cli_session, "snapshot_descendants", lambda pid: [])
        monkeypatch.setattr(cli_session, "reap_descendants", lambda children, timeout: None)
        proc = _LiveProc()
        proc.stdin.close = lambda: proc.end(0)
        session, told = self._session(tmp_agent_dir, cli_config, sat_config, proc)
        await session.close()
        await self._settle()
        assert proc.returncode == 0 and told == []


class TestCLISessionExitInATurn:
    """The engine's process ended inside a turn. What it had already printed
    reaches the platform (the reader used to return on the exit code first
    and drop it), and the turn's end says the engine exited, with its exit
    code and the last line it said, so a chat reads that the engine stopped
    and never that the machine was lost."""

    def _session(self, tmp_agent_dir, cli_config, sat_config, proc, stderr=b""):
        session = CLISession("sess-x", tmp_agent_dir, cli_config, sat_config)
        session.proc = proc
        session._stderr_buf = [stderr] if stderr else []
        return session

    @pytest.mark.asyncio
    async def test_what_the_engine_printed_before_its_exit_is_read(self, tmp_agent_dir, cli_config, sat_config):
        said = [{"type": "assistant", "n": 1}, {"type": "result", "subtype": "error_during_execution"}]
        proc = _ExitedProc(said, 1)
        session = self._session(tmp_agent_dir, cli_config, sat_config, proc)
        # The exit shows after the prompt is written, as a real exit does.
        proc.returncode, code = None, proc.returncode

        async def _drain():
            proc.returncode = code
        proc.stdin.drain = _drain
        got = [ev async for ev in session.send_message("go")]
        assert got == said

    @pytest.mark.asyncio
    async def test_the_turns_end_carries_the_exit_code_and_the_last_line(
            self, tmp_agent_dir, cli_config, sat_config):
        proc = _FakeProc([{"type": "assistant"}])          # then EOF: the process is gone
        proc.returncode = None

        async def _wait() -> int:
            proc.returncode = 3
            return 3
        proc.wait = _wait
        session = self._session(tmp_agent_dir, cli_config, sat_config, proc,
                                stderr=b"warming up\n\nError: the config file is not valid JSON\n  \n")
        got = [ev async for ev in session.send_message("go")]
        assert got == [{"type": "assistant"}]
        assert session.take_turn_end() == {"reason": "exited", "exit_code": 3,
                                           "last_line": "Error: the config file is not valid JSON"}
        assert session.take_turn_end() == {}               # said once

    @pytest.mark.asyncio
    async def test_a_long_last_line_is_cut_and_no_stderr_gives_none(self, tmp_agent_dir, cli_config, sat_config):
        proc = _ExitedProc([], 137)
        session = self._session(tmp_agent_dir, cli_config, sat_config, proc, stderr=b"x" * 5000)
        proc.returncode = None

        async def _drain():
            proc.returncode = 137
        proc.stdin.drain = _drain
        assert [ev async for ev in session.send_message("go")] == []
        end = session.take_turn_end()
        assert end["exit_code"] == 137 and len(end["last_line"]) == 300
        quiet = self._session(tmp_agent_dir, cli_config, sat_config, _FakeProc([]))

        async def _never() -> int:
            await asyncio.sleep(3600)
            return 0
        quiet.proc.wait = _never                                 # EOF, and the exit code never comes
        import satellite.sessions.cli_session as cli_mod
        with patch.object(cli_mod, "_EXIT_CODE_WAIT_S", 0.05):
            assert [ev async for ev in quiet.send_message("go")] == []
        assert quiet.take_turn_end() == {"reason": "exited"}

    @pytest.mark.asyncio
    async def test_a_turn_the_platform_stopped_says_nothing_of_an_exit(self, tmp_agent_dir, cli_config, sat_config):
        proc = _SlowProc()
        session = self._session(tmp_agent_dir, cli_config, sat_config, proc)
        turn = asyncio.create_task(_collect(session.send_message("go")))
        await asyncio.sleep(0.05)
        session.request_stop_turn()
        assert await asyncio.wait_for(turn, 5) == []
        assert session.take_turn_end() == {}


async def _collect(gen) -> list:
    return [ev async for ev in gen]


class TestCLISessionDrainBound:
    """The post-turn forwarder runs as long as the proxy waits for the
    background work (``drain_bg_s``, its background-work ceiling, 0.5.167):
    before, it stopped after ten minutes, so a research sub-agent's lines,
    its end and the CLI's own wake turn never reached the proxy. A proxy
    that names no bound keeps ten minutes (it reads nothing longer)."""

    def test_the_proxys_bound_is_kept(self, tmp_agent_dir, cli_config, sat_config):
        session = CLISession("sess-b", tmp_agent_dir, cli_config, sat_config)
        session.request_stop_turn(drain_bg=True, drain_bg_s=7200)
        assert session._drain_bg and session._drain_bg_s == 7200.0

    @pytest.mark.parametrize("bound", [None, 0, -5, "7200"])
    def test_no_usable_bound_keeps_ten_minutes(self, tmp_agent_dir, cli_config, sat_config, bound):
        session = CLISession("sess-b", tmp_agent_dir, cli_config, sat_config)
        session.request_stop_turn(drain_bg=True, drain_bg_s=bound)
        assert session._drain_bg_s == 600.0

    @pytest.mark.asyncio
    async def test_the_drain_ends_at_its_bound(self, tmp_agent_dir, cli_config, sat_config):
        session = CLISession("sess-b", tmp_agent_dir, cli_config, sat_config)
        session.proc = _FakeProc([])

        async def quiet() -> bytes:
            await asyncio.sleep(0.02)
            return b"\n"

        session.proc.stdout.readline = AsyncMock(side_effect=quiet)
        session.request_stop_turn(drain_bg=True, drain_bg_s=0.1)

        async def forward(ev):
            pass

        await asyncio.wait_for(session._bg_drain_loop(forward), timeout=5)


class TestCLISessionStreamBetweenTurns:
    """A platform that reads between turns (``stream_between_turns`` on
    ``stop_turn``) gets every line after the turn with no time bound, so a
    turn the engine starts by itself is read live. The reader after the turn
    tracks that turn, and a steer may write into it. An older platform never
    asks: the bounded ``drain_bg`` rule stays."""

    def test_the_stream_has_no_bound_and_runs_with_nothing_pending(
            self, tmp_agent_dir, cli_config, sat_config):
        session = CLISession("sess-t", tmp_agent_dir, cli_config, sat_config)
        session.request_stop_turn(drain_bg=False, drain_bg_s=7200, stream=True)
        assert session._drain_bg and session._drain_bg_s == float("inf")

    def test_an_older_platform_keeps_the_bounded_rule(self, tmp_agent_dir, cli_config, sat_config):
        session = CLISession("sess-t", tmp_agent_dir, cli_config, sat_config)
        session.request_stop_turn(drain_bg=False, drain_bg_s=7200)
        assert not session._drain_bg
        session.request_stop_turn(drain_bg=True, drain_bg_s=7200)
        assert session._drain_bg and session._drain_bg_s == 7200.0

    @pytest.mark.asyncio
    async def test_the_reader_tracks_the_engines_own_turn_and_a_steer_reaches_it(
            self, tmp_agent_dir, cli_config, sat_config):
        """A sub-agent's line opens nothing, a main-thread init or first
        message opens the engine's own turn, its result closes it. While it is
        open a steer is written into stdin, outside it the steer is refused."""
        sub = {"type": "stream_event", "parent_tool_use_id": "toolu_x",
               "event": {"type": "message_start"}}
        opening = {"type": "stream_event", "event": {"type": "message_start"}}
        result = {"type": "result", "subtype": "success", "result": "REVIEWED"}
        session = CLISession("sess-t", tmp_agent_dir, cli_config, sat_config)
        session.proc = _FakeProc([sub])
        session._stderr_buf = []
        seen: list[dict] = []
        steers: list[bool] = []

        async def forward(ev):
            seen.append(ev)
            steers.append(await session.steer(f"after {ev.get('type')}"))

        session.request_stop_turn(stream=True)
        await asyncio.wait_for(session._bg_drain_loop(forward), timeout=5)
        assert steers == [False] and not session._engine_turn_open
        session.proc = _FakeProc([opening, result])
        await asyncio.wait_for(session._bg_drain_loop(forward), timeout=5)
        assert steers == [False, True, False]
        assert [f["message"]["content"] for f in _frames(session.proc)] == ["after stream_event"]
        session.proc = _FakeProc([{"type": "system", "subtype": "init"}])
        await asyncio.wait_for(session._bg_drain_loop(forward), timeout=5)
        assert session._engine_turn_open

    @pytest.mark.asyncio
    async def test_what_an_exited_process_printed_last_is_still_forwarded(
            self, tmp_agent_dir, cli_config, sat_config):
        session = CLISession("sess-t", tmp_agent_dir, cli_config, sat_config)
        session.proc = _FakeProc([{"type": "result", "subtype": "success", "result": "last words"}])
        session.proc.returncode = 0
        seen: list[dict] = []

        async def forward(ev):
            seen.append(ev)

        session.request_stop_turn(stream=True)
        await asyncio.wait_for(session._bg_drain_loop(forward), timeout=10)
        assert [ev.get("result") for ev in seen] == ["last words"]

    @pytest.mark.asyncio
    async def test_a_turn_the_platform_sends_takes_the_engines_turn_over(
            self, tmp_agent_dir, cli_config, sat_config):
        session = CLISession("sess-t", tmp_agent_dir, cli_config, sat_config)
        session.proc = _FakeProc([{"type": "result", "subtype": "success", "result": "ok"}])
        session._stderr_buf = []
        session._engine_turn_open = True
        gen = session.send_message("hello")
        first = await gen.__anext__()
        assert first["type"] == "result" and not session._engine_turn_open
        session.request_stop_turn()
        await gen.aclose()


class TestCLISessionPermissionAnswer:
    """The answer to the CLI's own ``can_use_tool`` request (a heredoc that
    writes a file asks even under bypass) must be in the shape the CLI
    parses: ``response.response`` with ``behavior`` (0.5.140; the proxy's
    ``engine_prompts.answer_frame``). Before, the satellite wrote
    ``response.permission``, which the CLI ignored, so the request was
    refused at once by a proxy that knew it."""

    def _session(self, tmp_agent_dir, cli_config, sat_config):
        session = CLISession("sess-p", tmp_agent_dir, cli_config, sat_config)
        session.proc = _FakeProc([])
        return session

    def _request(self, rid, tool_input):
        return {"type": "control_request", "request_id": rid,
                "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": tool_input}}

    @pytest.mark.asyncio
    async def test_an_allow_carries_the_input_the_cli_printed(self, tmp_agent_dir, cli_config, sat_config):
        session = self._session(tmp_agent_dir, cli_config, sat_config)
        session._note_control_request(self._request("r1", {"command": "cat <<EOF > f\nx\nEOF"}))
        proxy_frame = {"subtype": "success", "request_id": "r1",
                       "response": {"behavior": "allow", "updatedInput": {"command": "changed"}}}
        await session.send_permission_response("r1", True, response=proxy_frame)
        frame = _frames(session.proc)[-1]
        assert frame == {"type": "control_response", "response": {
            "subtype": "success", "request_id": "r1",
            "response": {"behavior": "allow", "updatedInput": {"command": "cat <<EOF > f\nx\nEOF"}}}}
        assert "r1" not in session._prompt_inputs

    @pytest.mark.asyncio
    async def test_a_deny_carries_the_proxys_message_and_nothing_else(
        self, tmp_agent_dir, cli_config, sat_config,
    ):
        session = self._session(tmp_agent_dir, cli_config, sat_config)
        proxy_frame = {"subtype": "success", "request_id": "r2", "response": {
            "behavior": "deny", "message": "Not now.", "updatedPermissions": [{"type": "setMode"}]}}
        await session.send_permission_response("r2", True, response=proxy_frame)
        assert _frames(session.proc)[-1]["response"]["response"] == {
            "behavior": "deny", "message": "Not now."}

    @pytest.mark.asyncio
    async def test_an_older_proxys_boolean_still_answers(self, tmp_agent_dir, cli_config, sat_config):
        session = self._session(tmp_agent_dir, cli_config, sat_config)
        await session.send_permission_response("r3", True)
        await session.send_permission_response("r4", False)
        allow, deny = (f["response"] for f in _frames(session.proc))
        assert allow == {"subtype": "success", "request_id": "r3", "response": {"behavior": "allow"}}
        assert deny["response"]["behavior"] == "deny" and deny["response"]["message"]

    @pytest.mark.asyncio
    async def test_a_frame_for_another_request_is_not_taken(self, tmp_agent_dir, cli_config, sat_config):
        session = self._session(tmp_agent_dir, cli_config, sat_config)
        other = {"request_id": "rX", "response": {"behavior": "deny", "message": "x"}}
        await session.send_permission_response("r5", True, response=other)
        assert _frames(session.proc)[-1]["response"]["response"]["behavior"] == "allow"

    def test_the_remembered_inputs_are_bounded(self, tmp_agent_dir, cli_config, sat_config):
        from satellite.sessions import cli_session as cs
        session = CLISession("sess-q", tmp_agent_dir, cli_config, sat_config)
        for i in range(cs._PROMPT_INPUTS_MAX + 10):
            session._note_control_request(self._request(f"r{i}", {"n": i}))
        session._note_control_request({"type": "control_request", "request_id": "z",
                                       "request": {"subtype": "interrupt"}})
        assert len(session._prompt_inputs) == cs._PROMPT_INPUTS_MAX
        assert "r0" not in session._prompt_inputs and "z" not in session._prompt_inputs

    @pytest.mark.asyncio
    async def test_the_session_manager_passes_the_proxys_response(self, tmp_path):
        from satellite.config import SatelliteConfig
        from satellite.sessions.session_manager import SessionManager
        sm = SessionManager(SatelliteConfig(
            machine_id="m", machine_secret="s", platform_url="ws://localhost:8400/v1/satellite",
            agents_dir=tmp_path / "agents", mcps_dir=tmp_path / "mcps"))
        got = []

        class _S:
            async def send_permission_response(self, rid, approved, response=None):
                got.append((rid, approved, response))
        sm.sessions["s1"] = _S()
        frame = {"request_id": "r", "response": {"behavior": "allow"}}
        await sm.control_response({"session_id": "s1", "request_id": "r", "approved": True,
                                   "response": frame})
        await sm.control_response({"session_id": "s1", "request_id": "r", "approved": False})
        assert got == [("r", True, frame), ("r", False, None)]


class TestStartContextMessage:
    """The proxy's ``start_context`` on a send: the start's data goes first as
    Claude Code's own message, marked as not typed by a person (``isSynthetic``,
    ``shouldQuery`` false), which its terminal never draws."""

    @pytest.mark.asyncio
    async def test_the_block_goes_first_as_its_own_message(self, tmp_agent_dir, cli_config, sat_config):
        proc = _FakeProc([{"type": "result", "subtype": "success", "result": "", "num_turns": 0}])
        session = CLISession("sess-sc", tmp_agent_dir, cli_config, sat_config)
        session.proc = proc
        session._stderr_buf = []
        events = []

        async def forward(ev):
            events.append(ev)
        await asyncio.wait_for(session.run_turn("hello", forward=forward,
                                                start_context="<start-context>\nX\n</start-context>"),
                               timeout=5)
        written = [json.loads(line) for w in proc.stdin.writes for line in w.decode().splitlines() if line]
        assert written[0] == {"type": "user", "message": {"role": "user", "content": "<start-context>\nX\n</start-context>"},
                              "isSynthetic": True, "shouldQuery": False}
        assert written[1]["message"]["content"].endswith("hello") and "isSynthetic" not in written[1]

    @pytest.mark.asyncio
    async def test_its_paths_are_translated_like_the_prompts(self, tmp_agent_dir, cli_config, sat_config):
        """The block names folders and files by their sandbox path, as the
        prompt does: on this machine they read as its own paths."""
        proc = _FakeProc([{"type": "result", "subtype": "success", "result": "", "num_turns": 0}])
        session = CLISession("sess-sc3", tmp_agent_dir, cli_config, sat_config)
        session.proc = proc
        session._stderr_buf = []

        async def forward(ev):
            pass
        await asyncio.wait_for(session.run_turn("hello", forward=forward,
                                                start_context="<start-context>\n/workspace/notes.md\n</start-context>"),
                               timeout=5)
        written = [json.loads(line) for w in proc.stdin.writes for line in w.decode().splitlines() if line]
        block = written[0]["message"]["content"]
        assert f"{tmp_agent_dir}/workspace/notes.md" in block and "\n/workspace/notes.md" not in block

    @pytest.mark.asyncio
    async def test_a_send_without_it_writes_the_prompt_alone(self, tmp_agent_dir, cli_config, sat_config):
        proc = _FakeProc([])
        session = CLISession("sess-sc2", tmp_agent_dir, cli_config, sat_config)
        session.proc = proc
        session._stderr_buf = []

        async def forward(ev):
            pass
        await asyncio.wait_for(session.run_turn("hello", forward=forward), timeout=5)
        written = [json.loads(line) for w in proc.stdin.writes for line in w.decode().splitlines() if line]
        assert len(written) == 1 and "isSynthetic" not in written[0]


class TestCLISessionHome:
    """A start that asks for a home (``session_home``, the platform's word for
    a satellite that reports ``session_homes``) runs the engine from it: the
    sign-in, the account record and the settings are the session's, the hook
    scripts, the prompt and the tools' config stay in the scope's folder, and
    the close removes the home but not the conversation."""

    SID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    SPEC = {"links": ["projects", "todos", "skills"], "own_files": [".credentials.json"],
            "seed": {".claude.json": ["oauthAccount"]}}

    @pytest.mark.asyncio
    async def test_the_engine_runs_from_its_home(self, tmp_agent_dir, cli_config, sat_config):
        blob = {"claudeAiOauth": {"accessToken": "at", "refreshToken": ""}}
        store = tmp_agent_dir / "users" / "alice" / ".claude"
        store.mkdir(parents=True)
        (store / ".claude.json").write_text(json.dumps(
            {"userID": "u", "oauthAccount": {"emailAddress": "someone@example.com"}}))
        session = CLISession(self.SID, tmp_agent_dir,
                             {**cli_config, "credentials_json": blob, "session_home": self.SPEC},
                             sat_config)
        await _capture_start_cmd(session)
        home = store / "own" / self.SID
        assert session._captured_env["CLAUDE_CONFIG_DIR"] == str(home)
        assert json.loads((home / ".credentials.json").read_text()) == blob
        assert not (store / ".credentials.json").exists()
        # The session's own settings, written in its home and naming the
        # store's scripts; the store gets none another start could change.
        assert str(store / "permission_gate.py") in (home / "settings.json").read_text()
        assert not (store / "settings.json").exists()
        assert json.loads((home / ".claude.json").read_text()) == {"userID": "u"}
        assert (home / "projects").is_symlink()
        from satellite.sessions.cli_session import prompt_filename
        assert (store / prompt_filename(self.SID)).exists()
        assert not (home / prompt_filename(self.SID)).exists()
        (home / "projects" / "conv.jsonl").write_text("kept")

        session.proc = None
        await session.close()
        assert not home.exists()
        assert (store / "projects" / "conv.jsonl").read_text() == "kept"

    @pytest.mark.asyncio
    async def test_a_close_whose_first_step_raises_still_removes_the_home(self, tmp_agent_dir, cli_config,
                                                                           sat_config, monkeypatch):
        """Whatever a close's earlier step raises, the home with the sign-in
        and the session's secret files go."""
        from satellite.sessions import session_files, ssh_agent
        blob = {"claudeAiOauth": {"accessToken": "at", "refreshToken": ""}}
        session = CLISession(self.SID, tmp_agent_dir,
                             {**cli_config, "credentials_json": blob, "session_home": self.SPEC}, sat_config)
        await _capture_start_cmd(session)
        home = tmp_agent_dir / "users" / "alice" / ".claude" / "own" / self.SID
        assert (home / ".credentials.json").is_file()
        wiped: list[str] = []
        monkeypatch.setattr(session_files, "wipe", wiped.append)

        def _stuck(_sid):
            raise RuntimeError("stuck")
        monkeypatch.setattr(ssh_agent, "stop", _stuck)
        session.proc = None
        with pytest.raises(RuntimeError):
            await session.close()
        assert not home.exists() and wiped == [self.SID]

    @pytest.mark.asyncio
    async def test_two_starts_of_one_store_each_keep_their_own_settings(
        self, tmp_agent_dir, cli_config, sat_config,
    ):
        """A start that asks for its riding parts and one that asks for none,
        in one store: each home carries its own session's hooks (a copy of the
        store's file was the last writer's)."""
        other = "11111111-2222-3333-4444-555555555555"
        with_parts = CLISession(self.SID, tmp_agent_dir,
                                {**cli_config, "session_home": self.SPEC, "start_context_parts": 2}, sat_config)
        without = CLISession(other, tmp_agent_dir, {**cli_config, "session_home": self.SPEC}, sat_config)
        await _capture_start_cmd(with_parts)
        await _capture_start_cmd(without)
        store = tmp_agent_dir / "users" / "alice" / ".claude"
        mine = json.loads((store / "own" / self.SID / "settings.json").read_text())
        theirs = json.loads((store / "own" / other / "settings.json").read_text())
        assert "SessionStart" in mine["hooks"] and "SessionStart" not in theirs["hooks"]

    @pytest.mark.asyncio
    async def test_without_the_word_it_runs_from_the_scopes_folder(
        self, tmp_agent_dir, cli_config, sat_config,
    ):
        session = CLISession(self.SID, tmp_agent_dir, cli_config, sat_config)
        await _capture_start_cmd(session)
        store = tmp_agent_dir / "users" / "alice" / ".claude"
        assert session._captured_env["CLAUDE_CONFIG_DIR"] == str(store)
        assert not (store / "own").exists()


async def _start_terminal(session) -> dict:
    """Start a terminal with its spawn captured: the env the TUI would get."""
    captured: dict = {}

    async def wire(cmd, env, cwd):
        captured.update(cmd=cmd, env=env)

    async def binary(*a, **k):
        return "claude"

    with patch.object(session, "_wire_and_spawn", side_effect=wire), \
            patch.object(session, "_session_agent_env", AsyncMock(return_value={})), \
            patch("satellite.host.cli_versions.resolve_spawn_bin_async", side_effect=binary):
        await session.start()
    return captured


class TestPtySessionHome:
    """The terminal (the chat on a paired computer since the interactive
    default) runs from the session's home as the headless start does: the
    sign-in, the settings and the first-run seed are the home's, the close
    removes it; without the word everything stays in the scope's folder."""

    SID = TestCLISessionHome.SID
    SPEC = TestCLISessionHome.SPEC
    BLOB = {"claudeAiOauth": {"accessToken": "at", "refreshToken": ""}}

    def _terminal(self, agent_dir, config, sat_config):
        from satellite.terminal.pty_session import PtySession
        return PtySession(self.SID, agent_dir, config, sat_config, ws=None)

    @pytest.mark.asyncio
    async def test_the_terminal_runs_from_its_home(self, tmp_agent_dir, cli_config, sat_config):
        store = tmp_agent_dir / "users" / "alice" / ".claude"
        store.mkdir(parents=True)
        (store / ".claude.json").write_text(json.dumps(
            {"userID": "u", "oauthAccount": {"emailAddress": "someone@example.com"}}))
        session = self._terminal(tmp_agent_dir, {**cli_config, "credentials_json": self.BLOB,
                                                 "session_home": self.SPEC}, sat_config)
        got = await _start_terminal(session)
        home = store / "own" / self.SID
        assert got["env"]["CLAUDE_CONFIG_DIR"] == str(home)
        assert json.loads((home / ".credentials.json").read_text()) == self.BLOB
        assert not (store / ".credentials.json").exists()
        assert str(store / "permission_gate.py") in (home / "settings.json").read_text()
        assert not (store / "settings.json").exists()
        seeded = json.loads((home / ".claude.json").read_text())
        assert seeded["hasCompletedOnboarding"] is True and "oauthAccount" not in seeded
        assert "hasCompletedOnboarding" not in json.loads((store / ".claude.json").read_text())
        assert (home / "projects").is_symlink()
        (home / "projects" / "conv.jsonl").write_text("kept")
        await session.close()
        assert not home.exists()
        assert (store / "projects" / "conv.jsonl").read_text() == "kept"

    @pytest.mark.asyncio
    async def test_without_the_word_the_terminal_runs_from_the_scopes_folder(
        self, tmp_agent_dir, cli_config, sat_config,
    ):
        session = self._terminal(tmp_agent_dir, {**cli_config, "credentials_json": self.BLOB}, sat_config)
        got = await _start_terminal(session)
        store = tmp_agent_dir / "users" / "alice" / ".claude"
        assert got["env"]["CLAUDE_CONFIG_DIR"] == str(store)
        assert json.loads((store / ".credentials.json").read_text()) == self.BLOB
        assert json.loads((store / ".claude.json").read_text())["hasCompletedOnboarding"] is True
        assert not (store / "own").exists()
        await session.close()

    @pytest.mark.asyncio
    async def test_a_failing_teardown_still_ends_the_terminal(self, tmp_agent_dir, cli_config, sat_config,
                                                             monkeypatch):
        """Whatever the terminal's own teardown raises (a home that cannot be
        removed), its process is ended and the rest of the close runs."""
        from types import SimpleNamespace

        from satellite.sessions import session_files
        session = self._terminal(tmp_agent_dir, {**cli_config, "session_home": self.SPEC}, sat_config)
        ended, wiped = [], []
        session.pty = SimpleNamespace(terminate=lambda: ended.append(True))

        def _fails():
            raise RecursionError("a tree too deep")
        monkeypatch.setattr(session, "_on_close", _fails)
        monkeypatch.setattr(session_files, "wipe", wiped.append)
        await session.close()
        assert ended == [True] and wiped == [self.SID]

    def test_a_home_that_cannot_be_removed_never_breaks_a_close(self, tmp_path, monkeypatch):
        from satellite._vendored import session_home
        from satellite.sessions import session_homes

        def _fails(*a, **k):
            raise RecursionError("a tree too deep")
        monkeypatch.setattr(session_home, "drop", _fails)
        session_homes.close_home(tmp_path, self.SID, self.SPEC)

    @pytest.mark.asyncio
    async def test_an_own_copy_terminals_close_removes_its_home(self, tmp_agent_dir, cli_config, sat_config):
        """On a compute machine with its own copy the platform made the
        home's files and the machine the links; the close removes the home."""
        from satellite._vendored import session_home
        store = tmp_agent_dir / "users" / "alice" / ".claude"
        store.mkdir(parents=True)
        home = session_home.prepare(store, self.SID, ("projects",))
        (home / ".credentials.json").write_text("{}")
        session = self._terminal(tmp_agent_dir, {**cli_config, "session_home": self.SPEC}, sat_config)
        session._compute, session._own_copy = True, True
        await session.close()
        assert not home.exists() and (store / "projects").is_dir()

"""Tests for CLI session management."""

import asyncio
import contextlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from satellite import config
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
        assert set(settings["hooks"]) == {"PreToolUse", "PostToolUse", "SubagentStop", "Stop"}
        stop = settings["hooks"]["Stop"][0]["hooks"][0]
        assert "stop_tracker.py" in stop["command"] and stop["timeout"] == config.HOOK_WAIT_S
        # The gate blocks the call when it cannot run (Claude Code 2.1.295+,
        # the proxy builder writes the same); the trackers never block.
        assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["onFailure"] == "block"
        for event in ("PostToolUse", "SubagentStop", "Stop"):
            assert "onFailure" not in settings["hooks"][event][0]["hooks"][0], event
        # Claude Code ≥ 2.1.275 would sync the pool account's claude.ai skills
        # and plugins into the session — both off (mirrors the proxy builder).
        assert settings["syncClaudeAiSkills"] is False
        assert settings["syncClaudeAiPlugins"] is False
        assert settings["disableClaudeAiConnectors"] is True
        # Every built-in plugin but the four the platform keeps (Claude Code
        # 2.1.287+) is off by name, as the proxy builder writes them.
        from satellite.sessions.cli_session import DISABLED_BUILTIN_PLUGINS
        assert settings["enabledPlugins"] == {p: False for p in DISABLED_BUILTIN_PLUGINS}
        assert "cc-plugin-plugin-authoring@builtin" in DISABLED_BUILTIN_PLUGINS

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
        assert (claude_dir / "system-prompt.md").exists()
        assert (claude_dir / "mcp-config-sess-1.json").exists()
        assert not (claude_dir / "mcp-config.json").exists()
        assert (claude_dir / "settings.json").exists()

        # Verify prompt content
        assert (claude_dir / "system-prompt.md").read_text() == "You are a test agent."
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
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
        await session.start()
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
        assert "--permission-prompt-tool" not in cmd

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

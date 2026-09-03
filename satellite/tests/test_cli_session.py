"""Tests for CLI session management."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

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
        claude_bin="claude",
        codex_bin="codex",
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
        assert "PreToolUse" in settings["hooks"]
        assert "PostToolUse" in settings["hooks"]

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
        assert (claude_dir / "mcp-config.json").exists()
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

        # Verify env
        assert captured_env["OTO_SESSION_ID"] == "sess-1"
        assert captured_env["PROXY_URL"] == "http://100.1.2.3:8400"
        assert captured_env["ANTHROPIC_API_KEY"] == "sk-test"
        assert "CLAUDECODE" not in captured_env

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
        assert prompt_file.read_text() == "test"
        assert "--resume" in cmd


class TestCLISessionDetectChanges:
    def test_detect_no_changes(self, tmp_agent_dir, cli_config, sat_config):
        session = CLISession("sess-1", tmp_agent_dir, cli_config, sat_config)
        session._file_snapshot = {}
        changes = session.detect_file_changes()
        assert changes == []

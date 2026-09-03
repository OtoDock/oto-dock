"""Tests for session manager routing and capabilities."""

import asyncio
import json
import platform
import shutil
import sys
from unittest.mock import AsyncMock, patch

import pytest

from satellite.config import SatelliteConfig
from satellite.sessions.session_manager import SessionManager


@pytest.fixture
def sat_config(tmp_path):
    return SatelliteConfig(
        machine_id="test-machine",
        machine_secret="test-secret",
        platform_url="ws://localhost:8400/v1/satellite",
        agents_dir=tmp_path / "agents",
        mcps_dir=tmp_path / "mcps",
        claude_bin="claude",
        codex_bin="codex",
    )


class TestDetectCapabilities:
    def test_returns_os_and_arch(self, sat_config):
        sm = SessionManager(sat_config)
        caps = sm.detect_capabilities()
        assert caps["os"] == platform.system().lower()
        assert caps["arch"] == platform.machine()

    def test_detects_installed_clis(self, sat_config):
        sm = SessionManager(sat_config)
        with patch.object(shutil, "which", side_effect=lambda x: "/usr/bin/claude" if "claude" in str(x) else None):
            caps = sm.detect_capabilities()
        assert "claude-code" in caps["installed_clis"]
        assert "codex" not in caps["installed_clis"]

    def test_detects_both_clis(self, sat_config):
        sm = SessionManager(sat_config)
        with patch.object(shutil, "which", return_value="/usr/bin/tool"):
            caps = sm.detect_capabilities()
        assert "claude-code" in caps["installed_clis"]
        assert "codex" in caps["installed_clis"]

    def test_detects_installed_mcps(self, sat_config):
        sm = SessionManager(sat_config)
        # Create a mock MCP directory
        sat_config.mcps_dir.mkdir(parents=True)
        mcp_dir = sat_config.mcps_dir / "test-mcp"
        mcp_dir.mkdir()
        (mcp_dir / "manifest.json").write_text("{}")

        caps = sm.detect_capabilities()
        assert "test-mcp" in caps["installed_mcps"]

    def test_no_mcps_dir(self, sat_config):
        sm = SessionManager(sat_config)
        # mcps_dir doesn't exist
        caps = sm.detect_capabilities()
        assert caps["installed_mcps"] == []


class TestSessionRegistry:
    def test_starts_empty(self, sat_config):
        sm = SessionManager(sat_config)
        assert len(sm.sessions) == 0

    def test_cleanup_orphans_no_crash(self, sat_config):
        sm = SessionManager(sat_config)
        # Should not crash even without pid dir
        sm.cleanup_orphans()


class TestStartSessionReplacement:
    @pytest.mark.asyncio
    async def test_same_id_respawn_closes_stale_process(self, sat_config):
        """A start for a live session id (platform lost its handle, e.g. proxy
        restart mid-turn) must close the old process before spawning the
        replacement — otherwise the old CLI lingers satellite-parented forever."""
        sm = SessionManager(sat_config)
        stale = AsyncMock()
        stale.pid = 111
        sm.sessions["sess-1"] = stale

        replacement = AsyncMock()
        replacement.pid = 222
        ws = AsyncMock()
        with patch(
            "satellite.sessions.session_manager.CLISession",
            return_value=replacement,
        ):
            await sm.start_session({
                "session_id": "sess-1",
                "execution_path": "claude-code-cli",
                "config": {},
                "command_id": "cmd-1",
                "agent_slug": "test-agent",
            }, ws)

        stale.close.assert_awaited_once()
        replacement.start.assert_awaited_once()
        assert sm.sessions["sess-1"] is replacement


class TestFileSyncHandlers:
    @pytest.mark.asyncio
    async def test_file_push_creates_file(self, sat_config):
        sm = SessionManager(sat_config)
        sat_config.agents_dir.mkdir(parents=True)

        import base64
        content = base64.b64encode(b"hello world").decode()
        msg = {
            "agent_slug": "test-agent",
            "path": "workspace/test.txt",
            "action": "write",
            "content_b64": content,
        }
        await sm.file_push(msg)

        target = sat_config.agents_dir / "test-agent" / "workspace" / "test.txt"
        assert target.exists()
        assert target.read_text() == "hello world"


class _DeadCLISession:
    execution_path = "claude-code-cli"
    is_alive = False


class _CapturingWS:
    def __init__(self):
        self.sent = []

    async def enqueue_send(self, msg):
        self.sent.append(msg)


class TestCredentialsUpdate:
    """Rotation fan-out: the platform pushes a rewritten CLI credential file
    (the generic file sync excludes these — this push is the only channel)."""

    @pytest.mark.asyncio
    async def test_claude_rewrite(self, sat_config):
        sm = SessionManager(sat_config)
        ws = _CapturingWS()
        blob = {"claudeAiOauth": {"accessToken": "new", "refreshToken": ""}}
        await sm.credentials_update({
            "command_id": "c1", "agent_slug": "test-agent",
            "dir_relative": "users/alice/.claude", "kind": "claude",
            "content": blob,
        }, ws)
        assert ws.sent[0]["status"] == "ok"
        path = (sat_config.agents_dir / "test-agent" / "users" / "alice"
                / ".claude" / ".credentials.json")
        assert json.loads(path.read_text()) == blob

    @pytest.mark.asyncio
    async def test_codex_rewrite(self, sat_config):
        sm = SessionManager(sat_config)
        ws = _CapturingWS()
        auth = {"auth_mode": "chatgpt",
                "tokens": {"access_token": "new", "refresh_token": ""}}
        await sm.credentials_update({
            "command_id": "c2", "agent_slug": "test-agent",
            "dir_relative": "workspace/.codex", "kind": "codex",
            "content": auth,
        }, ws)
        assert ws.sent[0]["status"] == "ok"
        path = (sat_config.agents_dir / "test-agent" / "workspace"
                / ".codex" / "auth.json")
        assert json.loads(path.read_text()) == auth

    @pytest.mark.asyncio
    async def test_rejects_traversal_and_wrong_dir(self, sat_config):
        sm = SessionManager(sat_config)
        for bad in (
            {"agent_slug": "a", "dir_relative": "../../etc", "kind": "claude"},
            {"agent_slug": "a", "dir_relative": "users/u/.claude", "kind": "wat"},
            {"agent_slug": "a", "dir_relative": "users/u/.codex", "kind": "claude"},
            {"agent_slug": "../up", "dir_relative": "users/u/.claude", "kind": "claude"},
            {"agent_slug": "a", "dir_relative": "users/u/.claude", "kind": "claude",
             "content": "not-a-dict"},
        ):
            ws = _CapturingWS()
            msg = {"command_id": "cx", "content": {}, **bad}
            await sm.credentials_update(msg, ws)
            assert ws.sent[0]["status"] == "error", f"accepted bad target: {bad}"


class TestSendMessageDeadCLI:
    """A dead persistent CLI must come back as an ACK error
    (not a session_event) so the proxy's cli_dead → auto-resume path fires."""

    @pytest.mark.asyncio
    async def test_dead_cli_acks_error(self, sat_config):
        sm = SessionManager(sat_config)
        sm.sessions["sess-x"] = _DeadCLISession()
        ws = _CapturingWS()
        await sm.send_message({"session_id": "sess-x", "command_id": "cmd-1"}, ws)
        ack = ws.sent[0]
        assert ack["type"] == "ack"
        assert ack["status"] == "error"
        assert "CLI process not running" in ack["error"]


class _FakeCLISession:
    """CLI session stub that streams a scripted event list, tags nothing
    itself (the manager tags), and reports file changes as none."""
    execution_path = "claude-code-cli"
    is_alive = True
    agent_slug = "agent-1"

    def __init__(self, events):
        self._events = events
        self.config = {}
        self.stop_turn_event = asyncio.Event()

    async def send_message(self, message, inject_time=False):
        for ev in self._events:
            yield dict(ev)

    def detect_file_changes(self):
        return []


class TestModeCTurnRetention:
    """The satellite retains the current CLI turn's forwarded events + a
    _seq/_command_id tag, reports live sessions post-auth, and replays the
    buffer on resume_session_stream — the primitives Mode C re-adopt uses."""

    @pytest.mark.asyncio
    async def test_send_message_buffers_and_tags(self, sat_config):
        sm = SessionManager(sat_config)
        sm.sessions["s1"] = _FakeCLISession([
            {"type": "assistant"}, {"type": "result", "subtype": "success"},
        ])
        ws = _CapturingWS()
        await sm.send_message({"session_id": "s1", "command_id": "cmd-1",
                               "message": "hi"}, ws)
        evs = [m["event"] for m in ws.sent if m["type"] == "session_event"]
        assert all(e["_command_id"] == "cmd-1" for e in evs)
        assert [e["_seq"] for e in evs] == [1, 2]
        # Buffer holds the turn + a finished sentinel; state marked inactive.
        buf = list(sm.turn_buffers["s1"])
        assert buf[-1]["type"] == "_turn_sentinel"
        assert sm.turn_state["s1"]["active"] is False

    @pytest.mark.asyncio
    async def test_turn_ended_precedes_file_scan_frames(self, sat_config):
        # turn_ended must be sent BEFORE the end-of-turn file scan: on a big
        # workspace the scan outlives the proxy's post-stop drain budget, and
        # a marker sent after it lands as a stale leftover — the turn
        # accounting then goes permanently one turn behind (every response
        # persists one prompt late; live repro 2026-07-21).
        class _ScanningSession(_FakeCLISession):
            def detect_file_changes(self):
                return [{"path": "notes.md", "content_b64": "eA=="}]

        sm = SessionManager(sat_config)
        sm.sessions["s1"] = _ScanningSession([
            {"type": "assistant"}, {"type": "result", "subtype": "success"},
        ])
        ws = _CapturingWS()
        await sm.send_message({"session_id": "s1", "command_id": "cmd-1",
                               "message": "hi"}, ws)
        types = [m["type"] for m in ws.sent]
        assert "turn_ended" in types and "file_changed" in types
        assert types.index("turn_ended") < types.index("file_changed")

    def test_sessions_alive_report(self, sat_config):
        sm = SessionManager(sat_config)
        sm.sessions["s1"] = _FakeCLISession([])
        sm.turn_state["s1"] = {"seq": 3, "active": True, "command_id": "c9"}
        sm.sessions["s2"] = _DeadCLISession()  # not alive → excluded
        report = sm.headless_sessions_alive()
        ids = {r["session_id"]: r for r in report}
        assert "s1" in ids and "s2" not in ids
        assert ids["s1"]["turn_active"] is True
        assert ids["s1"]["command_id"] == "c9"

    @pytest.mark.asyncio
    async def test_resume_replay_order(self, sat_config):
        sm = SessionManager(sat_config)
        sm.sessions["s1"] = _FakeCLISession([])
        # Simulate a FINISHED buffered turn.
        from collections import deque
        sm.turn_state["s1"] = {"seq": 3, "active": False,
                               "command_id": "cmd-1", "start_seq": 1}
        sm.turn_buffers["s1"] = deque([
            {"type": "assistant", "_seq": 1, "_command_id": "cmd-1"},
            {"type": "result", "_seq": 2, "_command_id": "cmd-1"},
            {"type": "_turn_sentinel", "_seq": 3, "command_id": "cmd-1"},
        ])
        ws = _CapturingWS()
        await sm.resume_session_stream({"session_id": "s1"}, ws)
        types = [
            (m.get("type"), m.get("event", {}).get("type"))
            for m in ws.sent
        ]
        # begin marker → replayed events → trailing turn_ended.
        assert types[0] == ("session_event", "_resume_replay_begin")
        assert ("session_event", "assistant") in types
        assert ("session_event", "result") in types
        assert types[-1][0] == "turn_ended"
        # The sentinel itself is NOT forwarded as a session_event.
        assert ("session_event", "_turn_sentinel") not in types


class TestCheckSessionProcess:
    """The proxy's stall-reap probes real process liveness before killing a
    turn that went event-silent — a live CLI with a quiet stream is a network
    stall, not a death."""

    @pytest.mark.asyncio
    async def test_live_session_reports_alive(self, sat_config):
        sm = SessionManager(sat_config)

        class _LiveCLI:
            execution_path = "claude-code-cli"
            is_alive = True
        sm.sessions["sess-a"] = _LiveCLI()
        ws = _CapturingWS()
        await sm.check_session_process(
            {"session_id": "sess-a", "command_id": "cmd-p1"}, ws)
        ack = ws.sent[0]
        assert ack["status"] == "ok" and ack["alive"] is True

    @pytest.mark.asyncio
    async def test_dead_and_missing_sessions_report_dead(self, sat_config):
        sm = SessionManager(sat_config)
        sm.sessions["sess-d"] = _DeadCLISession()
        ws = _CapturingWS()
        await sm.check_session_process(
            {"session_id": "sess-d", "command_id": "cmd-p2"}, ws)
        await sm.check_session_process(
            {"session_id": "sess-gone", "command_id": "cmd-p3"}, ws)
        assert ws.sent[0]["alive"] is False
        assert ws.sent[1]["alive"] is False


class TestCheckSessionResumable:
    """Verifies the new check_session_resumable RPC stats the CLI session
    JSONL the same way the proxy's local layer does, so stop+resume on a
    remote satellite doesn't silently lose chat memory (the file lives on
    the satellite — the proxy can't stat it locally).
    """

    @pytest.fixture
    def fake_ws(self):
        class _FakeWS:
            def __init__(self):
                self.sent = []

            async def enqueue_send(self, msg):
                self.sent.append(msg)
        return _FakeWS()

    def _write_session_file(
        self, agents_dir, agent_slug, username, session_id, content,
    ):
        claude_dir = (
            agents_dir / agent_slug / "users" / username / ".claude"
        )
        # The project-hash subdir name varies — any name works for the test.
        project_dir = claude_dir / "projects" / "-some-project-hash"
        project_dir.mkdir(parents=True)
        (project_dir / f"{session_id}.jsonl").write_text(
            content, encoding="utf-8"
        )

    @pytest.mark.asyncio
    async def test_returns_true_when_session_file_has_user_message(
        self, sat_config, fake_ws,
    ):
        sm = SessionManager(sat_config)
        sat_config.agents_dir.mkdir(parents=True)
        self._write_session_file(
            sat_config.agents_dir, "agent-1", "alice", "sess-X",
            '{"type":"user","content":"hi"}\n',
        )

        await sm.check_session_resumable({
            "command_id": "cmd-1",
            "session_id": "sess-X",
            "agent_slug": "agent-1",
            "username": "alice",
        }, fake_ws)

        assert len(fake_ws.sent) == 1
        ack = fake_ws.sent[0]
        assert ack["type"] == "ack"
        assert ack["command_id"] == "cmd-1"
        assert ack["status"] == "ok"
        assert ack["resumable"] is True

    @pytest.mark.asyncio
    async def test_returns_false_when_session_file_empty(
        self, sat_config, fake_ws,
    ):
        sm = SessionManager(sat_config)
        sat_config.agents_dir.mkdir(parents=True)
        self._write_session_file(
            sat_config.agents_dir, "agent-1", "alice", "sess-X",
            "",  # no user message
        )

        await sm.check_session_resumable({
            "command_id": "cmd-2",
            "session_id": "sess-X",
            "agent_slug": "agent-1",
            "username": "alice",
        }, fake_ws)

        assert fake_ws.sent[0]["resumable"] is False

    @pytest.mark.asyncio
    async def test_returns_false_when_file_missing(self, sat_config, fake_ws):
        sm = SessionManager(sat_config)
        sat_config.agents_dir.mkdir(parents=True)

        await sm.check_session_resumable({
            "command_id": "cmd-3",
            "session_id": "sess-Y",
            "agent_slug": "agent-1",
            "username": "alice",
        }, fake_ws)

        assert fake_ws.sent[0]["resumable"] is False

    @pytest.mark.asyncio
    async def test_returns_false_when_agent_dir_missing(
        self, sat_config, fake_ws,
    ):
        """Pristine satellite with no agents_dir at all."""
        sm = SessionManager(sat_config)
        # Don't create agents_dir.

        await sm.check_session_resumable({
            "command_id": "cmd-4",
            "session_id": "sess-Z",
            "agent_slug": "agent-1",
            "username": "alice",
        }, fake_ws)

        assert fake_ws.sent[0]["resumable"] is False

    @pytest.mark.asyncio
    async def test_uses_workspace_claude_dir_without_username(
        self, sat_config, fake_ws,
    ):
        """Internal / agent-scoped sessions have no username — the JSONL
        lives under <slug>/workspace/.claude/ instead of users/<u>/.claude/.
        """
        sm = SessionManager(sat_config)
        sat_config.agents_dir.mkdir(parents=True)
        claude_dir = (
            sat_config.agents_dir / "agent-1" / "workspace" / ".claude"
        )
        project_dir = claude_dir / "projects" / "-h"
        project_dir.mkdir(parents=True)
        (project_dir / "sess-W.jsonl").write_text(
            '{"type": "user","content":"hi"}\n', encoding="utf-8",
        )

        await sm.check_session_resumable({
            "command_id": "cmd-5",
            "session_id": "sess-W",
            "agent_slug": "agent-1",
            "username": "",
        }, fake_ws)

        assert fake_ws.sent[0]["resumable"] is True


class TestFixWindowsExePathsAfterSwap:
    """Verifies pip/distlib .exe wrappers get their embedded python.exe path
    rewritten after the ``<X>.new/`` → ``<X>/`` rename on Windows.

    The bug: pip on Windows builds entry-point .exe wrappers with structure
    [launcher PE][shebang ``#!<python_exe>\\n``][zip data]. The shebang
    embeds an absolute path to ``<X>.new/venv/Scripts/python.exe``, which
    is deleted by the swap — every spawn fails with WinError 2. Fix
    rewrites the embedded path in place with trailing-space padding so
    the zip's byte offsets stay stable.
    """

    def _build_fake_exe(self, python_exe_path: str) -> bytes:
        """Fake distlib launcher: PE-ish header, shebang, then zip data."""
        launcher_blob = b"MZ\x90\x00" + b"\x00" * 60 + b"PE\x00\x00"
        shebang = f"#!{python_exe_path}\n".encode("utf-8")
        # Real zip data not required for the unit test — any bytes after
        # the shebang stand in for the appended zip payload.
        zip_payload = b"PK\x03\x04zipped-script-bytes"
        return launcher_blob + shebang + zip_payload

    def test_rewrites_embedded_python_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        from satellite.sessions.mcp_install_support import _fix_windows_exe_paths_after_swap

        old_root = tmp_path / "workspace-mcp.new"
        new_root = tmp_path / "workspace-mcp"
        old_python = str(old_root / "venv" / "Scripts" / "python.exe")
        new_python = str(new_root / "venv" / "Scripts" / "python.exe")

        # Simulate the post-swap state: new_root exists with the .exe
        # wrappers that pip baked when the dir was still <X>.new.
        scripts_dir = new_root / "venv" / "Scripts"
        scripts_dir.mkdir(parents=True)
        wrapper = scripts_dir / "workspace-mcp.exe"
        original_size = 0
        wrapper.write_bytes(self._build_fake_exe(old_python))
        original_size = wrapper.stat().st_size

        _fix_windows_exe_paths_after_swap(old_root, new_root)

        rewritten = wrapper.read_bytes()
        # Old path is gone.
        assert old_python.encode("utf-8") not in rewritten
        # New path is present.
        assert new_python.encode("utf-8") in rewritten
        # Byte length preserved (zip-offset stability).
        assert len(rewritten) == original_size
        # Zip payload still intact (the launcher reads it from a fixed offset).
        assert b"PK\x03\x04zipped-script-bytes" in rewritten

    def test_noop_on_non_windows(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        from satellite.sessions.mcp_install_support import _fix_windows_exe_paths_after_swap

        old_root = tmp_path / "workspace-mcp.new"
        new_root = tmp_path / "workspace-mcp"
        old_python = str(old_root / "venv" / "Scripts" / "python.exe")
        scripts_dir = new_root / "venv" / "Scripts"
        scripts_dir.mkdir(parents=True)
        wrapper = scripts_dir / "workspace-mcp.exe"
        wrapper.write_bytes(self._build_fake_exe(old_python))

        _fix_windows_exe_paths_after_swap(old_root, new_root)
        # On non-Windows, the function returns early — content untouched.
        assert old_python.encode("utf-8") in wrapper.read_bytes()

    def test_noop_when_no_scripts_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        from satellite.sessions.mcp_install_support import _fix_windows_exe_paths_after_swap

        old_root = tmp_path / "workspace-mcp.new"
        new_root = tmp_path / "workspace-mcp"
        new_root.mkdir()  # No venv/Scripts/ subdir — node MCP, source-only, etc.
        # Should return without error.
        _fix_windows_exe_paths_after_swap(old_root, new_root)


class TestResolveWarmupCommand:
    """Unit-tests the manifest stdio command/args → argv resolution used by
    the pre-warm boot check. Mirrors resolve_server_config's rules
    but resolves venv binaries via the satellite's own layout (venv_exe), so
    the assertions hold on Linux/macOS (venv/bin) and Windows (venv/Scripts/
    *.exe) alike."""

    def test_python_with_server_py(self, tmp_path):
        from satellite.config import venv_exe
        from satellite.sessions.mcp_install_support import _resolve_warmup_command
        root = tmp_path / "display-mcp"
        cmd = _resolve_warmup_command(root, "venv/bin/python", ["display_server.py"])
        assert cmd == [
            str(venv_exe(root / "venv", "python")),
            str(root / "display_server.py"),
        ]

    def test_console_script_no_args(self, tmp_path):
        from satellite.config import venv_exe
        from satellite.sessions.mcp_install_support import _resolve_warmup_command
        root = tmp_path / "google-maps"
        cmd = _resolve_warmup_command(root, "venv/bin/google-maps-mcp-server", [])
        assert cmd == [str(venv_exe(root / "venv", "google-maps-mcp-server"))]

    def test_console_script_with_flags_and_plain_token(self, tmp_path):
        from satellite.config import venv_exe
        from satellite.sessions.mcp_install_support import _resolve_warmup_command
        root = tmp_path / "workspace-mcp"
        cmd = _resolve_warmup_command(
            root, "venv/bin/workspace-mcp",
            ["--single-user", "--transport", "stdio"],
        )
        assert cmd == [
            str(venv_exe(root / "venv", "workspace-mcp")),
            "--single-user", "--transport", "stdio",  # flags + plain token kept
        ]

    def test_node_with_mcp_dir_template(self, tmp_path):
        from satellite.sessions.mcp_install_support import _resolve_warmup_command
        root = tmp_path / "email-server"
        cmd = _resolve_warmup_command(
            root, "node",
            ["${mcp_dir}/node_modules/mcp-mail-server/dist/index.js"],
        )
        assert cmd == [
            "node",  # bare command stays (resolved via PATH)
            str(root / "node_modules" / "mcp-mail-server" / "dist" / "index.js"),
        ]

    def test_python3_collapses_to_python(self, tmp_path):
        from satellite.config import venv_exe
        from satellite.sessions.mcp_install_support import _resolve_warmup_command
        root = tmp_path / "x"
        cmd = _resolve_warmup_command(root, "venv/bin/python3", ["server.py"])
        assert cmd[0] == str(venv_exe(root / "venv", "python"))


# Minimal stdio MCP stubs for the warm-up handshake integration tests.
_FAKE_OK_SERVER = (
    "import sys, json\n"
    "line = sys.stdin.readline()\n"
    "msg = json.loads(line)\n"
    "sys.stdout.write(json.dumps("
    "{'jsonrpc': '2.0', 'id': msg.get('id'), 'result': {'ok': True}}) + '\\n')\n"
    "sys.stdout.flush()\n"
)
_FAKE_HANG_SERVER = "import sys, time\nsys.stdin.readline()\ntime.sleep(60)\n"
_FAKE_CRASH_SERVER = "import sys\nsys.exit(1)\n"


class TestWarmOneMcp:
    """Integration: spawn a fake stdio MCP and drive the MCP ``initialize``
    handshake. Uses ``sys.executable`` as the (absolute) command so no fake
    venv is needed — the venv-path resolution itself is covered by
    TestResolveWarmupCommand. This is the GENERIC protocol check: any
    compliant stdio server answers initialize."""

    def _make_mcp(self, root, server_body, *, transport="stdio", command=None, args=None):
        root.mkdir(parents=True, exist_ok=True)
        (root / "server.py").write_text(server_body, encoding="utf-8")
        manifest = {
            "name": root.name,
            "server": {
                "transport": transport,
                "command": command if command is not None else sys.executable,
                "args": args if args is not None else ["server.py"],
            },
        }
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    @pytest.mark.asyncio
    async def test_ok_on_initialize_reply(self, tmp_path):
        from satellite.sessions.mcp_install_support import _warm_one_mcp
        root = tmp_path / "fake-mcp"
        self._make_mcp(root, _FAKE_OK_SERVER)
        name, status = await _warm_one_mcp(root, "fake-mcp", asyncio.Semaphore(2))
        assert name == "fake-mcp"
        assert status == "ok"

    @pytest.mark.asyncio
    async def test_warn_on_timeout(self, tmp_path, monkeypatch):
        from satellite.sessions import mcp_install_support
        monkeypatch.setattr(mcp_install_support, "_WARMUP_TIMEOUT_S", 1.0)
        root = tmp_path / "hang-mcp"
        self._make_mcp(root, _FAKE_HANG_SERVER)
        name, status = await mcp_install_support._warm_one_mcp(
            root, "hang-mcp", asyncio.Semaphore(2),
        )
        assert status == "warn:timeout"

    @pytest.mark.asyncio
    async def test_warn_on_immediate_crash(self, tmp_path):
        from satellite.sessions.mcp_install_support import _warm_one_mcp
        root = tmp_path / "crash-mcp"
        self._make_mcp(root, _FAKE_CRASH_SERVER)
        name, status = await _warm_one_mcp(root, "crash-mcp", asyncio.Semaphore(2))
        assert status.startswith("warn:")  # advisory — never raises, never excludes

    @pytest.mark.asyncio
    async def test_skip_non_stdio(self, tmp_path):
        from satellite.sessions.mcp_install_support import _warm_one_mcp
        root = tmp_path / "http-mcp"
        root.mkdir(parents=True)
        (root / "manifest.json").write_text(json.dumps({
            "name": "http-mcp",
            "server": {"transport": "sse", "url_template": "https://x/sse"},
        }), encoding="utf-8")
        name, status = await _warm_one_mcp(root, "http-mcp", asyncio.Semaphore(2))
        assert status == "skip:not-stdio"

    @pytest.mark.asyncio
    async def test_skip_no_command(self, tmp_path):
        from satellite.sessions.mcp_install_support import _warm_one_mcp
        root = tmp_path / "weird-mcp"
        root.mkdir(parents=True)
        (root / "manifest.json").write_text(json.dumps({
            "name": "weird-mcp",
            "server": {"transport": "stdio", "command": ""},
        }), encoding="utf-8")
        name, status = await _warm_one_mcp(root, "weird-mcp", asyncio.Semaphore(2))
        assert status == "skip:no-command"


class _InterruptibleCLISession:
    """Fake CLI session: live proc + control-request recorder."""

    def __init__(self, returncode=None):
        self.proc = type("P", (), {"returncode": returncode})()
        self.control_requests: list = []

    async def send_control_request(self, subtype, **kwargs):
        self.control_requests.append(subtype)


class TestInterruptTurn:
    """Soft headless-CLI abort: interrupt_turn writes control_request
    {interrupt} into the CLI's stdin so the turn closes with a normal result
    event and the process + MCP sidecars survive (no re-warm)."""

    @pytest.mark.asyncio
    async def test_writes_interrupt_control_request(self, sat_config):
        sm = SessionManager(sat_config)
        sess = _InterruptibleCLISession()
        sm.sessions["s1"] = sess
        await sm.interrupt_turn({"session_id": "s1"})
        assert sess.control_requests == ["interrupt"]

    @pytest.mark.asyncio
    async def test_dead_process_is_a_noop(self, sat_config):
        # The proxy-side watchdog escalates to the hard abort; nothing to
        # write into a dead CLI's stdin.
        sm = SessionManager(sat_config)
        sess = _InterruptibleCLISession(returncode=1)
        sm.sessions["s1"] = sess
        await sm.interrupt_turn({"session_id": "s1"})
        assert sess.control_requests == []

    @pytest.mark.asyncio
    async def test_unknown_session_is_a_noop(self, sat_config):
        sm = SessionManager(sat_config)
        await sm.interrupt_turn({"session_id": "ghost"})  # must not raise

    @pytest.mark.asyncio
    async def test_session_without_control_seam_is_a_noop(self, sat_config):
        # interrupt_turn is CLI-only — a session object without the
        # send_control_request seam (codex) is skipped.
        sm = SessionManager(sat_config)
        sm.sessions["s1"] = object()
        await sm.interrupt_turn({"session_id": "s1"})


class TestFileStat:
    """file_stat (0.5.95): cheap pull-cache revalidation probe. Validation
    mirrors file_pull; replies as a command ack."""

    def _ws(self):
        ws = AsyncMock()
        return ws

    @pytest.mark.asyncio
    async def test_existing_file_reports_size_and_mtime(self, sat_config):
        sm = SessionManager(sat_config)
        target = sat_config.agents_dir / "work" / "workspace" / "a.txt"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"0123456789")
        ws = self._ws()
        await sm.file_stat({
            "command_id": "c1", "path_kind": "agent_tree",
            "agent_slug": "work", "path": "workspace/a.txt",
        }, ws)
        reply = ws.enqueue_send.await_args.args[0]
        assert reply["type"] == "ack" and reply["command_id"] == "c1"
        assert reply["status"] == "ok"
        assert reply["exists"] is True
        assert reply["size"] == 10
        assert reply["mtime_ns"] == target.stat().st_mtime_ns

    @pytest.mark.asyncio
    async def test_missing_file_is_ok_but_absent(self, sat_config):
        sm = SessionManager(sat_config)
        (sat_config.agents_dir / "work").mkdir(parents=True)
        ws = self._ws()
        await sm.file_stat({
            "command_id": "c2", "path_kind": "agent_tree",
            "agent_slug": "work", "path": "workspace/nope.txt",
        }, ws)
        reply = ws.enqueue_send.await_args.args[0]
        assert reply["status"] == "ok"
        assert reply["exists"] is False

    @pytest.mark.asyncio
    async def test_traversal_rejected_as_error(self, sat_config):
        """A stat leaks existence — gated exactly as hard as a read."""
        sm = SessionManager(sat_config)
        (sat_config.agents_dir / "work").mkdir(parents=True)
        ws = self._ws()
        await sm.file_stat({
            "command_id": "c3", "path_kind": "agent_tree",
            "agent_slug": "work", "path": "../../../etc/passwd",
        }, ws)
        reply = ws.enqueue_send.await_args.args[0]
        assert reply["status"] == "error"
        assert "not authorized" in reply["error"]


class TestSyncMcpsCategoryGate:
    """sync_mcps validates name + category before using them as path
    components under mcps_dir. Every category the platform ships must
    install — the platform's own MCPs are ``core`` — and anything else is
    refused without touching the tree."""

    @staticmethod
    def _tarball(name):
        import base64
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = json.dumps({"name": name, "server": {"transport": "stdio"}}).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        return base64.b64encode(buf.getvalue()).decode()

    def _spec(self, name, category):
        return {
            "name": name, "category": category, "runtime": "python",
            "source": "", "tarball_b64": self._tarball(name), "version_hash": "h1",
        }

    async def _run(self, sat_config, specs):
        from types import SimpleNamespace
        sm = SessionManager(sat_config)
        ws = AsyncMock()
        installed = SimpleNamespace(ok=True, log="", version_hash="h1")
        with patch("satellite._vendored.mcp_installer.install_mcp",
                   AsyncMock(return_value=installed)), \
             patch("satellite.sessions.session_manager._warm_one_mcp",
                   AsyncMock(side_effect=lambda root, name, sem: (name, "ok"))):
            await sm.sync_mcps(
                {"command_id": "c1", "mcps_to_install": specs, "mcps_to_remove": []},
                ws,
            )
        ack = ws.enqueue_send.call_args_list[-1].args[0]
        assert ack["type"] == "ack" and ack["command_id"] == "c1"
        return ack

    @pytest.mark.asyncio
    async def test_every_shipped_category_installs(self, sat_config):
        from satellite.sessions.session_manager import MCP_CATEGORIES
        specs = [self._spec(f"{cat}-mcp", cat) for cat in sorted(MCP_CATEGORIES)]
        ack = await self._run(sat_config, specs)
        for cat in sorted(MCP_CATEGORIES):
            name = f"{cat}-mcp"
            assert ack["results"][name]["status"] == "ok", ack["results"][name]
            assert (sat_config.mcps_dir / cat / name / "manifest.json").is_file()
            assert name in ack["installed_mcps"]
        assert "core" in MCP_CATEGORIES  # the platform's own MCPs

    @pytest.mark.asyncio
    async def test_unknown_category_and_bad_name_are_refused(self, sat_config):
        specs = [
            self._spec("escaper", "../outside"),
            self._spec("stranger", "skill"),
            self._spec("../traversal", "core"),
        ]
        ack = await self._run(sat_config, specs)
        assert ack["results"]["escaper"]["error"] == "unsafe mcp category: '../outside'"
        assert ack["results"]["stranger"]["error"] == "unsafe mcp category: 'skill'"
        assert ack["results"]["../traversal"]["error"] == "unsafe mcp name: '../traversal'"
        assert all(r["status"] == "error" for r in ack["results"].values())
        assert not (sat_config.mcps_dir.parent / "outside").exists()
        assert not (sat_config.mcps_dir / "skill").exists()
        assert ack["installed_mcps"] == []

    def test_allow_list_covers_every_shipped_manifest(self):
        """The platform ships install specs with the manifest's ``category``
        verbatim; a category the satellite refuses drops that MCP from every
        remote session. Docker and context-only MCPs never install on a
        satellite, so only satellite-installable manifests are checked."""
        from pathlib import Path
        from satellite.sessions.session_manager import MCP_CATEGORIES
        root = Path(__file__).resolve().parents[2] / "mcps"
        manifests = sorted(root.glob("*/*/manifest.json"))
        if not manifests:
            pytest.skip("platform mcps tree not present in this checkout")
        for mf in manifests:
            data = json.loads(mf.read_text())
            if (data.get("server") or {}).get("runtime") in ("docker", "none"):
                continue
            assert data.get("category") in MCP_CATEGORIES, mf


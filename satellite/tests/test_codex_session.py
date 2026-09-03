"""Tests for Codex session management (app-server model).

The satellite drives a persistent ``codex app-server`` JSON-RPC daemon: ``start()``
writes the ``.codex`` config tree, spawns the daemon, and opens/resumes a thread;
turns run via ``run_turn`` over the daemon (no per-turn ``codex exec`` subprocess).
The end-to-end turn/event semantics are covered proxy-side
(``test_codex_subagent_turn`` / ``test_remote_codex_bg``); these tests cover the
satellite-local pieces: config writing, the resume-vs-new-thread decision, and
control-request handling.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from satellite.sessions.codex_session import CodexSession, _write_codex_hooks


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
def codex_config():
    return {
        "cwd_relative": "users/alice",
        "codex_dir_relative": "users/alice/.codex",
        "system_prompt": "You are a test agent.",
        "agents_md_content": "# Test Agent\nYou are a test agent.",
        "mcp_config_toml": '[mcp_servers.task-mcp]\ncommand = "python3"',
        "model": "gpt-5.4",
        "effort": "high",
        "env": {
            "PROXY_URL": "http://100.1.2.3:8400",
            "PROXY_API_KEY": "test-key",
            "CODEX_API_KEY": "sk-codex-test",
        },
    }


def _patch_daemon(session, mock_client):
    """Patch out the real daemon spawn / warm / forwarder so start() exercises
    only the config-write + thread open/resume logic against a mock app-server
    client. Returns a list of context managers to enter."""
    def _connect(env):
        session._client = mock_client

    return (
        patch.object(session, "_connect_with_retry", new=AsyncMock(side_effect=_connect)),
        patch.object(session, "_warm_mcps", new=AsyncMock()),
        patch.object(session, "_run_forwarder", new=AsyncMock()),
    )


class TestWriteCodexHooks:
    def test_writes_hooks_json(self, tmp_path):
        _write_codex_hooks(tmp_path)
        hooks_file = tmp_path / "hooks.json"
        assert hooks_file.exists()
        hooks = json.loads(hooks_file.read_text())
        # Codex hook schema is an OBJECT keyed by event (matches the proxy's
        # core/sandbox._build_codex_hooks); a LIST is rejected by Codex's parser.
        assert isinstance(hooks, dict)
        events = hooks["hooks"]
        assert set(events) == {"PreToolUse", "PostToolUse"}

    def test_hook_commands_reference_dir(self, tmp_path):
        _write_codex_hooks(tmp_path)
        hooks = json.loads((tmp_path / "hooks.json").read_text())
        cmd = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert str(tmp_path) in cmd
        assert "permission_gate.py" in cmd


class TestCodexSessionStart:
    @pytest.mark.asyncio
    async def test_creates_config_files(self, tmp_agent_dir, codex_config, sat_config):
        session = CodexSession("sess-1", tmp_agent_dir, codex_config, sat_config)
        await session.start()

        codex_dir = tmp_agent_dir / "users" / "alice" / ".codex"
        assert codex_dir.is_dir()
        assert (codex_dir / "AGENTS.md").exists()
        assert (codex_dir / "config.toml").exists()
        assert (codex_dir / "hooks.json").exists()

        # Verify content
        assert "Test Agent" in (codex_dir / "AGENTS.md").read_text()
        assert "task-mcp" in (codex_dir / "config.toml").read_text()

    @pytest.mark.asyncio
    async def test_writes_auth_json(self, tmp_agent_dir, sat_config):
        config = {
            "cwd_relative": "users/alice",
            "codex_dir_relative": "users/alice/.codex",
            "system_prompt": "test",
            "agents_md_content": "test",
            "env": {},
            "auth_json": {"auth_mode": "chatgpt", "tokens": {"id_token": "tok-123"}},
        }
        session = CodexSession("sess-1", tmp_agent_dir, config, sat_config)
        await session.start()

        codex_dir = tmp_agent_dir / "users" / "alice" / ".codex"
        auth = json.loads((codex_dir / "auth.json").read_text())
        assert auth["auth_mode"] == "chatgpt"
        assert auth["tokens"]["id_token"] == "tok-123"


class TestCodexSessionThread:
    """start() opens a NEW thread (thread/start) or RESUMES a persisted one
    (thread/resume) over the app-server — the app-server analog of the old
    `codex exec` / `codex exec resume <id>` decision."""

    @pytest.mark.asyncio
    async def test_start_resumes_existing_thread(self, tmp_agent_dir, sat_config):
        config = {
            "cwd_relative": "users/alice",
            "codex_dir_relative": "users/alice/.codex",
            "system_prompt": "test",
            "agents_md_content": "test",
            "mcp_config_toml": "",
            "model": "gpt-5.4",
            "env": {},
            "thread_id": "thread-existing",
        }
        session = CodexSession("sess-2", tmp_agent_dir, config, sat_config)

        calls: list[tuple[str, dict]] = []
        mock_client = AsyncMock()
        mock_client.proc = None

        async def fake_request(method, params=None):
            calls.append((method, params or {}))
            return {"thread": {"id": (params or {}).get("threadId") or "new-thread"}}

        mock_client.request = fake_request

        c1, c2, c3 = _patch_daemon(session, mock_client)
        with c1, c2, c3:
            await session.start()
            await session.close()

        methods = [m for m, _ in calls]
        assert "thread/resume" in methods
        assert "thread/start" not in methods
        resume_params = next(p for m, p in calls if m == "thread/resume")
        assert resume_params.get("threadId") == "thread-existing"
        assert session.thread_id == "thread-existing"

    @pytest.mark.asyncio
    async def test_start_opens_new_thread_when_none(self, tmp_agent_dir, codex_config, sat_config):
        session = CodexSession("sess-new", tmp_agent_dir, codex_config, sat_config)

        calls: list[tuple[str, dict]] = []
        mock_client = AsyncMock()
        mock_client.proc = None

        async def fake_request(method, params=None):
            calls.append((method, params or {}))
            return {"thread": {"id": "thread-fresh"}}

        mock_client.request = fake_request

        c1, c2, c3 = _patch_daemon(session, mock_client)
        with c1, c2, c3:
            await session.start()
            await session.close()

        methods = [m for m, _ in calls]
        assert "thread/start" in methods
        assert "thread/resume" not in methods
        assert session.thread_id == "thread-fresh"


class TestCodexSessionControlRequest:
    @pytest.mark.asyncio
    async def test_set_model_updates_config(self, tmp_agent_dir, codex_config, sat_config):
        """set_model is stored as a per-turn override (applied on the next
        turn/start — no daemon respawn)."""
        session = CodexSession("sess-1", tmp_agent_dir, codex_config, sat_config)
        await session.send_control_request("set_model", model="gpt-4.1-mini")
        assert session.config["model"] == "gpt-4.1-mini"

    @pytest.mark.asyncio
    async def test_set_permission_mode_updates_sandbox(self, tmp_agent_dir, codex_config, sat_config):
        session = CodexSession("sess-1", tmp_agent_dir, codex_config, sat_config)
        await session.send_control_request("set_permission_mode", sandbox_mode="danger-full-access")
        assert session.config["sandbox_mode"] == "danger-full-access"


class TestRequestStopTurn:
    def test_accepts_drain_bg_kwarg(self, tmp_agent_dir, codex_config, sat_config):
        """session_manager forwards stop_turn's drain_bg to whichever session type
        holds the id; Codex has no bg-drain concept but must accept the kwarg
        (regression: TypeError on codex sessions when the proxy sent drain_bg)."""
        session = CodexSession("sess-stop", tmp_agent_dir, codex_config, sat_config)
        session.request_stop_turn(drain_bg=True)
        assert session._stop_requested
        assert session._main_turn_done.is_set()


class TestRunTurnSerialization:
    """The per-session _turn_lock must keep two run_turn calls from overlapping.

    Regression for the abort→resend race: the satellite create_tasks every WS
    command and the proxy drops its session lock on abort, so a fast Stop-then-
    send started a second run_turn while the aborted one was still unwinding;
    the two shared _current_turn_id + the single _main_turn_done event, so the
    new turn returned with zero streamed events (daemon ran it, dashboard blank).
    """

    @pytest.mark.asyncio
    async def test_run_turn_serializes_overlapping_calls(
        self, tmp_agent_dir, codex_config, sat_config,
    ):
        session = CodexSession("sess-lock", tmp_agent_dir, codex_config, sat_config)

        active = 0
        max_active = 0
        release = asyncio.Event()

        async def fake_inner(prompt, *, inject_time=False):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await release.wait()  # hold the lock until released
            active -= 1

        with patch.object(session, "_run_turn_locked", side_effect=fake_inner):
            t1 = asyncio.create_task(session.run_turn("A"))
            t2 = asyncio.create_task(session.run_turn("B"))
            await asyncio.sleep(0.02)  # let both reach the lock
            # Only ONE inner body may run; the other is parked on _turn_lock.
            assert active == 1, "second turn entered before the first released"
            assert max_active == 1
            release.set()  # first finishes → second now proceeds
            await asyncio.gather(t1, t2)
        assert max_active == 1

    @pytest.mark.asyncio
    async def test_abort_does_not_acquire_turn_lock(
        self, tmp_agent_dir, codex_config, sat_config,
    ):
        """abort() must stay lock-free — it has to interrupt a turn that is
        HOLDING _turn_lock, so taking the lock would deadlock against the very
        turn it is meant to stop."""
        session = CodexSession("sess-lock2", tmp_agent_dir, codex_config, sat_config)
        await session._turn_lock.acquire()  # simulate a turn in flight
        try:
            mock_client = AsyncMock()
            mock_client.is_alive = True
            session._client = mock_client
            session._current_turn_id = "turn-x"
            # Returns promptly (sends turn/interrupt) without waiting on the lock.
            await asyncio.wait_for(session.abort(), timeout=1.0)
            mock_client.request.assert_awaited()  # turn/interrupt was issued
        finally:
            session._turn_lock.release()


class TestAskQuestionRemote:
    """The remote request_user_input bridge: POST the questions to the proxy's
    /v1/hooks/codex-question and return the answers MAP; fail-closed to empty
    answers (never hang the held turn) on any missing-coords/transport error."""

    def _session(self, tmp_agent_dir, codex_config, sat_config, *, coords=True):
        session = CodexSession("sess-q", tmp_agent_dir, codex_config, sat_config)
        if coords:
            session._proxy_url = "http://127.0.0.1:9"
            session._proxy_api_key = "tok"
        return session

    @pytest.mark.asyncio
    async def test_returns_answers_on_200(self, tmp_agent_dir, codex_config, sat_config):
        session = self._session(tmp_agent_dir, codex_config, sat_config)
        answers = {"color": {"answers": ["Dark"]}}

        class _Resp:
            status = 200
            async def json(self): return {"answers": answers}
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        class _Http:
            def post(self, *a, **k): return _Resp()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        with patch("aiohttp.ClientSession", lambda *a, **k: _Http()):
            got = await session._ask_question_remote([{"id": "color"}])
        assert got == answers

    @pytest.mark.asyncio
    async def test_no_coords_returns_empty(self, tmp_agent_dir, codex_config, sat_config):
        session = self._session(tmp_agent_dir, codex_config, sat_config, coords=False)
        assert await session._ask_question_remote([{"id": "x"}]) == {}

    @pytest.mark.asyncio
    async def test_non_200_returns_empty(self, tmp_agent_dir, codex_config, sat_config):
        session = self._session(tmp_agent_dir, codex_config, sat_config)

        class _Resp:
            status = 500
            async def json(self): return {}
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        class _Http:
            def post(self, *a, **k): return _Resp()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        with patch("aiohttp.ClientSession", lambda *a, **k: _Http()):
            assert await session._ask_question_remote([{"id": "x"}]) == {}

    @pytest.mark.asyncio
    async def test_transport_error_returns_empty(self, tmp_agent_dir, codex_config, sat_config):
        session = self._session(tmp_agent_dir, codex_config, sat_config)

        class _Http:
            def post(self, *a, **k): raise RuntimeError("boom")
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        with patch("aiohttp.ClientSession", lambda *a, **k: _Http()):
            assert await session._ask_question_remote([{"id": "x"}]) == {}


# ---------------------------------------------------------------------------
# Interactive TUI config header — satellite twin of the proxy writer
# ---------------------------------------------------------------------------

def test_pty_config_toml_header_keys():
    import tomllib
    from satellite.terminal.codex_pty_session import _build_codex_config_toml
    cfg = tomllib.loads(_build_codex_config_toml("/home/u/work", ""))
    # Root keys must parse at root (not swallowed by a [table] header): the
    # update check would break the version pin, the suppress key pairs with
    # the request_user_input feature flag below.
    assert cfg["check_for_update_on_startup"] is False
    assert cfg["suppress_unstable_features_warning"] is True
    assert cfg["features"]["plugins"] is False
    assert cfg["features"]["hooks"] is True
    assert cfg["features"]["default_mode_request_user_input"] is True
    assert cfg["projects"]["/home/u/work"]["trust_level"] == "trusted"

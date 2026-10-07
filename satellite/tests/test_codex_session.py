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
import contextlib
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from satellite import config
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


def _thread_client(thread_id="thread-1"):
    """A mock app-server client that answers every request with ``thread_id``."""
    mock_client = AsyncMock()
    mock_client.proc = None

    async def fake_request(method, params=None):
        return {"thread": {"id": thread_id}}

    mock_client.request = fake_request
    return mock_client


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
        # Hook parity (0.5.121): the same four events the proxy's
        # _build_codex_hooks writes for the local sandbox.
        assert set(events) == {"PreToolUse", "PostToolUse", "SubagentStop", "Stop"}
        assert events["Stop"][0]["hooks"][0]["timeout"] == config.HOOK_WAIT_S == 3 * 24 * 3600 + 3600
        assert "stop_tracker.py" in events["Stop"][0]["hooks"][0]["command"]
        assert "subagent_tracker.py" in events["SubagentStop"][0]["hooks"][0]["command"]

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
        c1, c2, c3 = _patch_daemon(session, _thread_client())
        with c1, c2, c3:
            await session.start()
            await session.close()

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
        c1, c2, c3 = _patch_daemon(session, _thread_client())
        with c1, c2, c3:
            await session.start()
            await session.close()

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

    @pytest.mark.asyncio
    async def test_plan_follows_the_live_sandbox_unless_a_judge(self, tmp_agent_dir, codex_config, sat_config):
        """A mode change carries the sandbox alone: with no shipped mode (the
        proxy ships one only for a judge) plan follows it both ways."""
        session = CodexSession("sess-1", tmp_agent_dir, {**codex_config, "permission_mode": "",
                                                         "sandbox_mode": "read-only"}, sat_config)
        assert session._plan_mode(session.config["sandbox_mode"]) is True
        await session.send_control_request("set_permission_mode", sandbox_mode="workspace-write")
        assert session._plan_mode(session.config["sandbox_mode"]) is False
        await session.send_control_request("set_permission_mode", sandbox_mode="read-only")
        assert session._plan_mode(session.config["sandbox_mode"]) is True
        judge = CodexSession("sess-2", tmp_agent_dir, {**codex_config, "permission_mode": "judge",
                                                       "sandbox_mode": "read-only"}, sat_config)
        assert judge._plan_mode("read-only") is False


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


class TestConnectReportAndRevive:
    """What the connect report reads off a Codex session, and a revive that
    had to open a new thread telling the platform before the turn's events
    (it routes them by thread)."""

    @pytest.mark.asyncio
    async def test_the_report_properties(self, tmp_agent_dir, codex_config, sat_config):
        session = CodexSession("sess-r", tmp_agent_dir, codex_config, sat_config)
        assert session.resume_handle == ""
        session.thread_id = "thread-7"
        session._mcp_server_names = ["file-tools"]
        assert session.resume_handle == "thread-7"
        assert session.mcp_server_names == ["file-tools"]
        assert session.turn_active is False
        async with session._turn_lock:
            assert session.turn_active is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("new_thread, told", [("thread-new", True), ("thread-old", False)])
    async def test_a_revive_on_a_new_thread_is_announced_first(
            self, tmp_agent_dir, codex_config, sat_config, new_thread, told):
        session = CodexSession("sess-rv", tmp_agent_dir, codex_config, sat_config)
        session.thread_id = "thread-old"
        dead = AsyncMock()
        dead.is_alive = False
        session._client = dead
        forwarded: list[dict] = []

        async def _forward(event):
            forwarded.append(event)
        session.set_event_forwarder(_forward)

        class _TurnStarted(Exception):
            pass
        fresh = AsyncMock()
        fresh.is_alive = True
        fresh.request.side_effect = _TurnStarted()

        async def _start():
            session._client = fresh
            session.thread_id = new_thread

        with patch.object(session, "start", side_effect=_start):
            with pytest.raises(_TurnStarted):
                await session._run_turn_locked("go on")
        assert forwarded == ([{"type": "_resume_handle", "handle": "thread-new"}] if told else [])


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
            async def text(self): return json.dumps({"answers": answers})
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
    async def test_transport_error_returns_empty(self, tmp_agent_dir, codex_config, sat_config,
                                                 monkeypatch):
        from satellite.sessions import codex_session as cs
        monkeypatch.setattr(cs, "_HOOK_BACKOFF", (0.01,))
        monkeypatch.setattr(cs, "_HOOK_HOLD_S", 0.05)  # held, then empty
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
    from satellite.tests._toml import toml_loads
    from satellite.terminal.codex_pty_session import _build_codex_config_toml
    cfg = toml_loads(_build_codex_config_toml("/home/u/work", ""))
    # Root keys must parse at root (not swallowed by a [table] header): the
    # update check would break the version pin, the suppress key pairs with
    # the request_user_input feature flag below.
    assert cfg["check_for_update_on_startup"] is False
    assert cfg["suppress_unstable_features_warning"] is True
    assert cfg["features"]["plugins"] is False
    assert cfg["features"]["hooks"] is True
    assert cfg["features"]["default_mode_request_user_input"] is True
    assert cfg["projects"]["/home/u/work"]["trust_level"] == "trusted"
    # Codex 0.152.0 made update_plan opt-in — the plan tool stays on.
    assert cfg["tools"]["update_plan"]["enabled"] is True
    # No local endpoint in the payload → no selector and no provider table.
    assert "model_provider" not in cfg
    assert "model_providers" not in cfg


# ---------------------------------------------------------------------------
# Local OpenAI-compatible endpoint carried in the start payload
# (``local_model_provider``) — twin of the proxy's provider block
# ---------------------------------------------------------------------------

_LOCAL = {"base_url": "http://192.168.1.8:8080/v1", "env_key": "OTO_LOCAL_API_KEY"}


_CATALOG = '{"models": [{"slug": "qwen3.6-35b-a3b", "supports_search_tool": true}]}\n'
_LOCAL_FULL = {
    "base_url": "http://127.0.0.1:11434/v1", "env_key": "",
    "stream_idle_timeout_ms": 1800000, "catalog_json": _CATALOG,
}


def test_local_provider_toml_shapes():
    from satellite.sessions.codex_session import local_provider_toml
    assert local_provider_toml(None) == ("", "")
    assert local_provider_toml({}) == ("", "")
    assert local_provider_toml({"base_url": "  "}) == ("", "")
    root, table = local_provider_toml({"base_url": 'http://h/v1"x', "env_key": ""})
    assert root == 'model_provider = "oto_local"'
    assert 'base_url = "http://h/v1\\"x"' in table
    assert 'wire_api = "responses"' in table
    # Keyless → no env_key line (codex-rs refuses a provider naming an unset var).
    assert "env_key" not in table
    # A 0.5.116-era payload (no timeout, no catalog) → neither line.
    assert "stream_idle_timeout_ms" not in table
    assert "model_catalog_json" not in root
    _, keyed = local_provider_toml(_LOCAL)
    assert 'env_key = "OTO_LOCAL_API_KEY"' in keyed


def test_local_provider_toml_catalog_and_idle_timeout(tmp_path):
    from satellite.tests._toml import toml_loads
    from satellite.sessions.codex_session import local_provider_toml
    codex_dir = tmp_path / "users" / "alice" / ".codex"
    root, table = local_provider_toml(_LOCAL_FULL, codex_dir)
    cfg = toml_loads(root + "\n" + table + "\n")
    # Both root keys precede the table; the catalog path is this host's
    # absolute CODEX_HOME path (TOML-escaped, so a Windows path round-trips).
    assert cfg["model_provider"] == "oto_local"
    assert cfg["model_catalog_json"] == str(codex_dir / "models.json")
    assert cfg["model_providers"]["oto_local"]["stream_idle_timeout_ms"] == 1800000
    assert "env_key" not in cfg["model_providers"]["oto_local"]
    win = Path(r"C:\Users\d\.codex") if sys.platform == "win32" else None
    if win is not None:
        assert toml_loads(local_provider_toml(_LOCAL_FULL, win)[0])["model_catalog_json"] == str(win / "models.json")
    # No codex_dir (a caller without a CODEX_HOME) or an empty catalog → no key.
    assert "model_catalog_json" not in local_provider_toml(_LOCAL_FULL)[0]
    assert "model_catalog_json" not in local_provider_toml(dict(_LOCAL_FULL, catalog_json=""), codex_dir)[0]
    # A garbage timeout is ignored rather than breaking the TOML.
    _, bad = local_provider_toml(dict(_LOCAL_FULL, stream_idle_timeout_ms="soon"), codex_dir)
    assert "stream_idle_timeout_ms" not in bad


def test_write_or_drop_model_catalog(tmp_path):
    from satellite.sessions.codex_session import write_or_drop_model_catalog
    write_or_drop_model_catalog(tmp_path, _LOCAL_FULL)
    path = tmp_path / "models.json"
    assert path.read_text(encoding="utf-8") == _CATALOG
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
    # A session without one removes the previous session's file.
    write_or_drop_model_catalog(tmp_path, {"base_url": "http://h/v1", "catalog_json": ""})
    assert not path.exists()
    write_or_drop_model_catalog(tmp_path, None)
    assert not path.exists()


def test_pty_config_toml_local_provider_block(tmp_path):
    from satellite.tests._toml import toml_loads
    from satellite.terminal.codex_pty_session import _build_codex_config_toml
    text = _build_codex_config_toml(
        "/home/u/work", '[mcp_servers.task-mcp]\ncommand = "python3"',
        local_provider=_LOCAL,
    )
    cfg = toml_loads(text)
    # The selector is a ROOT key: it parses at root, i.e. precedes every table.
    assert cfg["model_provider"] == "oto_local"
    assert text.index('model_provider = "oto_local"') < text.index("[memories]")
    prov = cfg["model_providers"]["oto_local"]
    assert prov["base_url"] == "http://192.168.1.8:8080/v1"
    assert prov["wire_api"] == "responses"
    assert prov["env_key"] == "OTO_LOCAL_API_KEY"
    # Header keys unchanged; the MCP section survives after the provider table.
    assert cfg["features"]["hooks"] is True
    assert cfg["projects"]["/home/u/work"]["trust_level"] == "trusted"
    assert cfg["mcp_servers"]["task-mcp"]["command"] == "python3"
    # With the catalog + timeout (0.5.117 payload): the catalog root key sits
    # with the other root keys and the timeout in the provider table.
    text = _build_codex_config_toml(
        "/home/u/work", "", local_provider=_LOCAL_FULL, codex_dir=tmp_path,
    )
    cfg = toml_loads(text)
    assert cfg["model_catalog_json"] == str(tmp_path / "models.json")
    assert text.index("model_catalog_json") < text.index("[memories]")
    assert cfg["model_providers"]["oto_local"]["stream_idle_timeout_ms"] == 1800000


class TestCodexSessionLocalProvider:
    """Headless app-server path: the provider block lands in config.toml, the
    file is ALWAYS written (a hosted session never inherits a stale block), and
    the proxy's leading [features] block keeps the root key first."""

    def _config(self, **overrides):
        base = {
            "cwd_relative": "users/alice",
            "codex_dir_relative": "users/alice/.codex",
            "system_prompt": "You are a test agent.",
            "agents_md_content": "# Test Agent",
            "mcp_config_toml": '[mcp_servers.task-mcp]\ncommand = "python3"',
            "model": "qwen3.6-35b-a3b",
            "env": {"PROXY_URL": "http://100.1.2.3:8400", "PROXY_API_KEY": "test-key"},
        }
        base.update(overrides)
        return base

    async def _start(self, tmp_agent_dir, sat_config, config) -> Path:
        session = CodexSession("sess-lp", tmp_agent_dir, config, sat_config)
        mock_client = AsyncMock()
        mock_client.proc = None

        async def fake_request(method, params=None):
            return {"thread": {"id": "thread-lp"}}

        mock_client.request = fake_request
        c1, c2, c3 = _patch_daemon(session, mock_client)
        with c1, c2, c3:
            await session.start()
            await session.close()
        return tmp_agent_dir / "users" / "alice" / ".codex" / "config.toml"

    @pytest.mark.asyncio
    async def test_keyed_provider_block_with_mcp_sections(self, tmp_agent_dir, sat_config):
        from satellite.tests._toml import toml_loads
        path = await self._start(
            tmp_agent_dir, sat_config, self._config(local_model_provider=_LOCAL),
        )
        text = path.read_text()
        cfg = toml_loads(text)
        assert cfg["model_provider"] == "oto_local"
        # Root keys (the headless header's cap + the provider's) precede every
        # [table] header.
        assert text.index('model_provider = "oto_local"') < text.index("[")
        assert cfg["project_doc_max_bytes"] == 300000
        # The [tools] table sits between the root keys and the MCP sections.
        assert cfg["tools"]["update_plan"]["enabled"] is True
        assert text.index("[tools]") < text.index("[mcp_servers")
        prov = cfg["model_providers"]["oto_local"]
        assert prov["base_url"] == "http://192.168.1.8:8080/v1"
        assert prov["wire_api"] == "responses"
        assert prov["env_key"] == "OTO_LOCAL_API_KEY"
        assert "task-mcp" in cfg["mcp_servers"]
        # The provider table closes the file (after every MCP transform).
        assert text.rstrip().endswith('env_key = "OTO_LOCAL_API_KEY"')
        if sys.platform != "win32":
            assert path.stat().st_mode & 0o777 == 0o600
        # A 0.5.116-era payload: no catalog file, no key.
        assert not (path.parent / "models.json").exists()
        assert "model_catalog_json" not in cfg

    @pytest.mark.asyncio
    async def test_catalog_and_idle_timeout_land_in_codex_home(self, tmp_agent_dir, sat_config):
        from satellite.tests._toml import toml_loads
        path = await self._start(
            tmp_agent_dir, sat_config, self._config(local_model_provider=_LOCAL_FULL),
        )
        text = path.read_text()
        cfg = toml_loads(text)
        codex_dir = path.parent
        assert cfg["model_catalog_json"] == str(codex_dir / "models.json")
        assert text.index("model_catalog_json") < text.index("[mcp_servers")
        assert (codex_dir / "models.json").read_text(encoding="utf-8") == _CATALOG
        assert cfg["model_providers"]["oto_local"]["stream_idle_timeout_ms"] == 1800000
        # The warm gate knows the declared servers and the local model.
        assert "task-mcp" in cfg["mcp_servers"]

    @pytest.mark.asyncio
    async def test_hosted_session_drops_a_stale_catalog(self, tmp_agent_dir, sat_config):
        codex_dir = tmp_agent_dir / "users" / "alice" / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "models.json").write_text(_CATALOG)
        await self._start(
            tmp_agent_dir, sat_config, self._config(
                mcp_config_toml="", model="gpt-5.6-terra", env={"CODEX_API_KEY": "sk-test"},
            ),
        )
        assert not (codex_dir / "models.json").exists()

    @pytest.mark.asyncio
    async def test_keyless_provider_and_empty_mcp_toml_still_writes(
        self, tmp_agent_dir, sat_config,
    ):
        from satellite.tests._toml import toml_loads
        path = await self._start(
            tmp_agent_dir, sat_config, self._config(
                mcp_config_toml="",
                local_model_provider={"base_url": "http://127.0.0.1:8080/v1", "env_key": ""},
            ),
        )
        cfg = toml_loads(path.read_text())
        assert cfg["model_provider"] == "oto_local"
        assert "env_key" not in cfg["model_providers"]["oto_local"]
        assert "mcp_servers" not in cfg

    @pytest.mark.asyncio
    async def test_root_key_precedes_the_proxys_features_block(
        self, tmp_agent_dir, sat_config,
    ):
        from satellite.tests._toml import toml_loads
        path = await self._start(
            tmp_agent_dir, sat_config, self._config(
                mcp_config_toml=(
                    "[features]\ndefault_mode_request_user_input = true\n\n"
                    '[mcp_servers.task-mcp]\ncommand = "python3"'
                ),
                local_model_provider=_LOCAL,
            ),
        )
        text = path.read_text()
        cfg = toml_loads(text)
        assert cfg["model_provider"] == "oto_local"
        assert text.index("model_provider") < text.index("[features]")
        assert cfg["features"]["default_mode_request_user_input"] is True
        assert cfg["model_providers"]["oto_local"]["base_url"] == _LOCAL["base_url"]

    @pytest.mark.asyncio
    async def test_hosted_session_overwrites_a_stale_provider(
        self, tmp_agent_dir, sat_config,
    ):
        # A local-endpoint session leaves the block behind in the persistent
        # CODEX_HOME; the next hosted session with NO MCP toml must not inherit
        # it — config.toml is always rewritten (empty here).
        codex_dir = tmp_agent_dir / "users" / "alice" / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "config.toml").write_text(
            'model_provider = "oto_local"\n\n[model_providers.oto_local]\n'
            'base_url = "http://192.168.1.8:8080/v1"\nenv_key = "OTO_LOCAL_API_KEY"\n'
        )
        from satellite.tests._toml import toml_loads
        path = await self._start(
            tmp_agent_dir, sat_config, self._config(
                mcp_config_toml="", model="gpt-5.6-terra",
                env={"CODEX_API_KEY": "sk-test"},
            ),
        )
        assert path.exists()
        cfg = toml_loads(path.read_text())
        # Only the always-on headless header remains — no provider, no MCPs,
        # no hook floor (an attended session).
        assert cfg == {
            "project_doc_max_bytes": 300000,
            "tools": {"update_plan": {"enabled": True}},
            "memories": {"use_memories": False, "generate_memories": False},
            "features": {"plugins": False},
        }

    @pytest.mark.asyncio
    async def test_session_without_oauth_drops_a_stale_auth_json(
        self, tmp_agent_dir, sat_config,
    ):
        # A ChatGPT session left auth.json in the persistent CODEX_HOME; the
        # local-endpoint session that follows carries no auth_json and must not
        # let Codex load (and loop on refreshing) the stale token.
        codex_dir = tmp_agent_dir / "users" / "alice" / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "auth.json").write_text(
            '{"auth_mode": "chatgpt", "tokens": {"refresh_token": ""}}'
        )
        await self._start(
            tmp_agent_dir, sat_config, self._config(local_model_provider=_LOCAL),
        )
        assert not (codex_dir / "auth.json").exists()


def test_write_or_drop_auth_json(tmp_path):
    from satellite.sessions.codex_session import write_or_drop_auth_json
    write_or_drop_auth_json(tmp_path, {"auth_mode": "chatgpt", "tokens": {}})
    assert json.loads((tmp_path / "auth.json").read_text())["auth_mode"] == "chatgpt"
    if sys.platform != "win32":
        assert (tmp_path / "auth.json").stat().st_mode & 0o777 == 0o600
    write_or_drop_auth_json(tmp_path, None)
    assert not (tmp_path / "auth.json").exists()
    write_or_drop_auth_json(tmp_path, {})  # empty payload value == absent; idempotent
    assert not (tmp_path / "auth.json").exists()


class TestHooksFloor:
    """The PreToolUse permission floor for UNATTENDED remote Codex sessions
    (0.5.118): the proxy's ``codex_hooks_floor`` payload field turns on the
    three per-session pieces — ``[features] hooks = true``, thread-level
    ``bypass_hook_trust`` on thread/start AND thread/resume, and the deny-only
    / no-forward hook env. An attended session (no field) gets none of them
    and keeps the JSON-RPC approval bridge alone."""

    def _config(self, **overrides):
        base = {
            "cwd_relative": "users/alice",
            "codex_dir_relative": "users/alice/.codex",
            "system_prompt": "You are a test agent.",
            "agents_md_content": "# Test Agent",
            "mcp_config_toml": '[mcp_servers.task-mcp]\ncommand = "python3"',
            "model": "gpt-5.6-sol",
            "env": {"PROXY_URL": "http://100.1.2.3:8400", "PROXY_API_KEY": "test-key"},
        }
        base.update(overrides)
        return base

    async def _start(self, tmp_agent_dir, sat_config, config):
        """Run start()+close() against the mock daemon; return
        (config.toml text, [(method, params)], daemon env)."""
        session = CodexSession("sess-hf", tmp_agent_dir, config, sat_config)
        calls: list[tuple[str, dict]] = []
        seen_env: dict = {}
        mock_client = AsyncMock()
        mock_client.proc = None

        async def fake_request(method, params=None):
            calls.append((method, params or {}))
            return {"thread": {"id": (params or {}).get("threadId") or "thread-hf"}}

        mock_client.request = fake_request

        async def _connect(env):
            seen_env.update(env)
            session._client = mock_client

        with patch.object(session, "_connect_with_retry", new=AsyncMock(side_effect=_connect)), \
                patch.object(session, "_warm_mcps", new=AsyncMock()), \
                patch.object(session, "_run_forwarder", new=AsyncMock()):
            await session.start()
            await session.close()
        text = (tmp_agent_dir / "users" / "alice" / ".codex" / "config.toml").read_text()
        return text, calls, seen_env

    @pytest.mark.asyncio
    async def test_floor_writes_the_feature_trusts_the_thread_and_sets_the_env(
        self, tmp_agent_dir, sat_config,
    ):
        from satellite.tests._toml import toml_loads
        text, calls, env = await self._start(
            tmp_agent_dir, sat_config, self._config(codex_hooks_floor=True),
        )
        cfg = toml_loads(text)
        assert cfg["features"] == {"plugins": False, "hooks": True}
        assert text.count("[features]") == 1
        start = next(p for m, p in calls if m == "thread/start")
        assert start["config"] == {"bypass_hook_trust": True}
        assert start["approvalPolicy"] == "on-request"   # mode-derived, unchanged
        assert env["OTO_HOOK_DENY_ONLY"] == "1"
        assert env["OTO_HOOK_NO_FORWARD"] == "1"
        assert env["OTO_SESSION_ID"] == "sess-hf"

    @pytest.mark.asyncio
    async def test_attended_session_gets_none_of_it(self, tmp_agent_dir, sat_config):
        from satellite.tests._toml import toml_loads
        text, calls, env = await self._start(tmp_agent_dir, sat_config, self._config())
        cfg = toml_loads(text)
        assert cfg["features"] == {"plugins": False}
        assert "hooks" not in cfg["features"]
        start = next(p for m, p in calls if m == "thread/start")
        assert "config" not in start
        assert "OTO_HOOK_DENY_ONLY" not in env and "OTO_HOOK_NO_FORWARD" not in env
        # hooks.json is still written (dormant without trust).
        assert (tmp_agent_dir / "users" / "alice" / ".codex" / "hooks.json").exists()

    @pytest.mark.asyncio
    async def test_resume_carries_the_trust_too(self, tmp_agent_dir, sat_config):
        _, calls, _ = await self._start(
            tmp_agent_dir, sat_config,
            self._config(codex_hooks_floor=True, thread_id="thread-existing"),
        )
        resume = next(p for m, p in calls if m == "thread/resume")
        assert resume["threadId"] == "thread-existing"
        assert resume["config"] == {"bypass_hook_trust": True}

    @pytest.mark.asyncio
    async def test_floor_merges_with_the_proxys_features_block(self, tmp_agent_dir, sat_config):
        from satellite.tests._toml import toml_loads
        text, _, _ = await self._start(
            tmp_agent_dir, sat_config, self._config(
                codex_hooks_floor=True,
                mcp_config_toml=(
                    "[features]\ndefault_mode_request_user_input = true\n\n"
                    '[mcp_servers.task-mcp]\ncommand = "python3"'
                ),
                local_model_provider=_LOCAL,
            ),
        )
        cfg = toml_loads(text)
        assert text.count("[features]") == 1
        assert cfg["features"] == {
            "plugins": False, "default_mode_request_user_input": True, "hooks": True,
        }
        assert "task-mcp" in cfg["mcp_servers"]
        # Root keys still first, the provider table still closes the file.
        assert text.index("model_provider") < text.index("[")
        assert text.rstrip().endswith('env_key = "OTO_LOCAL_API_KEY"')

    @pytest.mark.asyncio
    async def test_headless_header_matches_the_local_writer(self, tmp_agent_dir, sat_config):
        from satellite.tests._toml import toml_loads
        text, _, _ = await self._start(tmp_agent_dir, sat_config, self._config())
        cfg = toml_loads(text)
        assert cfg["project_doc_max_bytes"] == 300000
        assert cfg["memories"] == {"use_memories": False, "generate_memories": False}
        assert cfg["features"]["plugins"] is False
        assert cfg["tools"]["update_plan"]["enabled"] is True
        assert text.index("project_doc_max_bytes") < text.index("[")


def test_features_table_shapes():
    from satellite.sessions.codex_session import features_table
    mcp = '[mcp_servers.task-mcp]\ncommand = "python3"'
    prepended = "[features]\ndefault_mode_request_user_input = true\n# note\n\n" + mcp

    assert features_table(False, "") == ("[features]\nplugins = false", "")
    assert features_table(True, mcp) == ("[features]\nplugins = false\nhooks = true", mcp)
    assert features_table(False, prepended) == (
        "[features]\nplugins = false\ndefault_mode_request_user_input = true", mcp,
    )
    assert features_table(True, prepended) == (
        "[features]\nplugins = false\ndefault_mode_request_user_input = true\nhooks = true",
        mcp,
    )
    # A prepended block that is the whole TOML leaves no MCP sections.
    assert features_table(True, "[features]\nhooks = true") == (
        "[features]\nplugins = false\nhooks = true", "",
    )
    # A key the proxy already set is not repeated.
    block, _ = features_table(True, "[features]\nplugins = false\nhooks = true\n\n" + mcp)
    assert block == "[features]\nplugins = false\nhooks = true"


class TestRetriedErrorKeepsTheTurn:
    """An ``error`` notification Codex retries (``willRetry`` at the params'
    top level, app-server 0.156.1) does not end the main turn; a final one
    does. Older builds nested the flag in ``error``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("params,ends", [
        ({"error": {"message": "stream disconnected"}, "threadId": "t1", "turnId": "u1",
          "willRetry": True}, False),
        ({"error": {"message": "stream disconnected", "willRetry": True}}, False),
        ({"error": {"message": "usage limit", "codexErrorInfo": "usageLimitExceeded"},
          "threadId": "t1", "turnId": "u1", "willRetry": False}, True),
    ])
    async def test_only_a_final_error_ends_the_main_turn(
            self, tmp_agent_dir, codex_config, sat_config, params, ends):
        session = CodexSession("sess-r", tmp_agent_dir, codex_config, sat_config)
        session.thread_id = "t1"
        forwarded: list = []

        async def forward(event):
            forwarded.append(event)

        session._forward = forward

        class Client:
            def __init__(self):
                self.notif_queue: asyncio.Queue = asyncio.Queue()

        session._client = Client()
        session._client.notif_queue.put_nowait(("error", params))
        task = asyncio.create_task(session._run_forwarder())
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert forwarded and forwarded[0]["method"] == "error"
        assert session._main_turn_done.is_set() is ends


class TestBridgeHold:
    """The approval and question bridges hold a call while the platform
    cannot be reached (the tunnel's 502/503, a connection error, a stream
    that ends before its JSON), read a streamed answer of keepalives and
    JSON, and deny at once on a definitive refusal."""

    async def _serve(self, answers):
        from aiohttp import web
        from aiohttp.test_utils import TestServer
        calls = {"n": 0}

        async def handler(request):
            i = min(calls["n"], len(answers) - 1)
            calls["n"] += 1
            status, body, *delay = answers[i]
            if delay:
                await asyncio.sleep(delay[0])
            if status == 200 and body.startswith(b" "):
                resp = web.StreamResponse(status=200, headers={"Content-Type": "application/json"})
                await resp.prepare(request)
                await resp.write(body)
                await resp.write_eof()
                return resp
            return web.Response(status=status, body=body, content_type="application/json")

        app = web.Application()
        app.router.add_post("/v1/hooks/permission", handler)
        app.router.add_post("/v1/hooks/codex-question", handler)
        server = TestServer(app)
        await server.start_server()
        return server, calls

    def _session(self, tmp_agent_dir, codex_config, sat_config, server):
        session = CodexSession("sess-b", tmp_agent_dir, codex_config, sat_config)
        session._proxy_url = str(server.make_url("")).rstrip("/")
        session._proxy_api_key = "tok"
        return session

    @pytest.mark.asyncio
    async def test_held_through_the_outage_then_the_streamed_answer(
            self, tmp_agent_dir, codex_config, sat_config, monkeypatch):
        from satellite.sessions import codex_session as cs
        monkeypatch.setattr(cs, "_HOOK_BACKOFF", (0.01,))
        server, calls = await self._serve([
            (503, b'{"error": "tunnel-not-connected"}'), (502, b"{}"),
            (200, b"     "),  # a stream that dropped before its JSON
            (200, b'   {"decision": "allow"}'),
        ])
        try:
            session = self._session(tmp_agent_dir, codex_config, sat_config, server)
            assert await session._decide_permission_remote("Bash", {}) == {"decision": "allow"}
            assert calls["n"] == 4
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_a_definitive_refusal_denies_at_once(
            self, tmp_agent_dir, codex_config, sat_config, monkeypatch):
        from satellite.sessions import codex_session as cs
        monkeypatch.setattr(cs, "_HOOK_BACKOFF", (0.01,))
        server, calls = await self._serve([(403, b'{"detail": "no"}')])
        try:
            session = self._session(tmp_agent_dir, codex_config, sat_config, server)
            assert await session._decide_permission_remote("Bash", {}) == {"decision": "deny"}
            assert await session._ask_question_remote([{"id": "q"}]) == {}
            assert calls["n"] == 2
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_the_hold_ends(self, tmp_agent_dir, codex_config, sat_config, monkeypatch):
        from satellite.sessions import codex_session as cs
        monkeypatch.setattr(cs, "_HOOK_BACKOFF", (0.01,))
        monkeypatch.setattr(cs, "_HOOK_HOLD_S", 0.05)
        server, calls = await self._serve([(503, b"{}")])
        try:
            session = self._session(tmp_agent_dir, codex_config, sat_config, server)
            assert await session._decide_permission_remote("Bash", {}) == {"decision": "deny"}
            assert calls["n"] >= 2
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_the_hold_counts_from_the_first_failure(
            self, tmp_agent_dir, codex_config, sat_config, monkeypatch):
        """A prompt answered slowly and then dropped is still held: the hold
        starts at the failure, not at the first call."""
        from satellite.sessions import codex_session as cs
        monkeypatch.setattr(cs, "_HOOK_BACKOFF", (0.01,))
        monkeypatch.setattr(cs, "_HOOK_HOLD_S", 0.05)
        server, calls = await self._serve([(503, b"{}", 0.2), (200, b' {"decision": "allow"}')])
        try:
            session = self._session(tmp_agent_dir, codex_config, sat_config, server)
            assert await session._decide_permission_remote("Bash", {}) == {"decision": "allow"}
            assert calls["n"] == 2
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_the_question_bridge_reads_its_answers(
            self, tmp_agent_dir, codex_config, sat_config):
        server, _ = await self._serve([(200, b' {"answers": {"q": {"answers": ["yes"]}}}')])
        try:
            session = self._session(tmp_agent_dir, codex_config, sat_config, server)
            assert await session._ask_question_remote([{"id": "q"}]) == {"q": {"answers": ["yes"]}}
        finally:
            await server.close()

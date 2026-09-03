"""Remote headless-Codex thread ops (sat 0.5.98/0.5.105): steer + compact +
background-terminal list twins.

The proxy's ``RemoteExecutionLayer.steer/compact`` and its bg-command drain
tunnel to the satellite as ``codex_steer`` / ``codex_compact`` /
``codex_bg_terminals`` RPCs → ``CodexSession.steer/compact/
list_background_terminals``. These cover the satellite-local pieces:

  * ``steer`` — strict accept semantics (True ONLY on daemon accept: the
    proxy queues the message otherwise, so a false positive loses input);
  * ``compact`` — the sniffer side-tap flow (the persistent forwarder stays
    the sole notif consumer; compact watches a tap for its completion
    signals) and the between-turns refusal;
  * ``list_background_terminals`` — verbatim row pass-through, None on error
    (the proxy's drain is fail-closed and must never see error-as-empty);
  * the ``session_manager`` RPC handlers' ack contracts;
  * ``_validate_config_toml`` — loud ERROR on invalid generated TOML.
"""

import asyncio
from pathlib import Path

import pytest

from satellite._vendored.app_server_client import AppServerError
from satellite.sessions.codex_session import (
    CodexSession, _validate_config_toml,
)
from satellite.sessions.session_manager import SessionManager


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


class _FakeClient:
    def __init__(self, error: Exception | None = None):
        self.calls: list[tuple[str, dict | None]] = []
        self.error = error
        self.is_alive = True
        self.notif_queue: asyncio.Queue = asyncio.Queue()

    async def request(self, method, params=None, timeout=None):
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        return {}


def _make_session(tmp_agent_dir, sat_config, client) -> CodexSession:
    session = CodexSession(
        "sess-ops",
        tmp_agent_dir,
        {"cwd_relative": "workspace", "codex_dir_relative": "workspace/.codex"},
        sat_config,
    )
    session._client = client
    session.thread_id = "thread-1"
    session._username = ""
    return session


class TestSteer:
    @pytest.mark.asyncio
    async def test_no_active_turn_returns_false_without_rpc(
        self, tmp_agent_dir, sat_config,
    ):
        client = _FakeClient()
        session = _make_session(tmp_agent_dir, sat_config, client)
        session._current_turn_id = None
        assert await session.steer("hello") is False
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_accept_returns_true_with_expected_turn(
        self, tmp_agent_dir, sat_config,
    ):
        client = _FakeClient()
        session = _make_session(tmp_agent_dir, sat_config, client)
        session._current_turn_id = "turn-42"
        assert await session.steer("go faster") is True
        method, params = client.calls[0]
        assert method == "turn/steer"
        assert params["expectedTurnId"] == "turn-42"
        assert params["threadId"] == "thread-1"
        assert params["input"][0]["text"] == "go faster"

    @pytest.mark.asyncio
    async def test_daemon_reject_returns_false(self, tmp_agent_dir, sat_config):
        client = _FakeClient(error=AppServerError("turn already completed"))
        session = _make_session(tmp_agent_dir, sat_config, client)
        session._current_turn_id = "turn-42"
        assert await session.steer("too late") is False

    @pytest.mark.asyncio
    async def test_dead_client_returns_false(self, tmp_agent_dir, sat_config):
        client = _FakeClient()
        client.is_alive = False
        session = _make_session(tmp_agent_dir, sat_config, client)
        session._current_turn_id = "turn-42"
        assert await session.steer("hello") is False
        assert client.calls == []


class TestCompact:
    @pytest.mark.asyncio
    async def test_refuses_while_turn_active(self, tmp_agent_dir, sat_config):
        client = _FakeClient()
        session = _make_session(tmp_agent_dir, sat_config, client)
        session._current_turn_id = "turn-1"
        assert await session.compact() is None
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_refuses_while_turn_lock_held(self, tmp_agent_dir, sat_config):
        client = _FakeClient()
        session = _make_session(tmp_agent_dir, sat_config, client)
        async with session._turn_lock:
            assert await session.compact() is None
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_happy_path_returns_post_tokens(self, tmp_agent_dir, sat_config):
        client = _FakeClient()
        session = _make_session(tmp_agent_dir, sat_config, client)
        task = asyncio.ensure_future(session.compact())
        for _ in range(50):
            await asyncio.sleep(0)
            if session._sniffers:
                break
        assert session._sniffers, "compact() should register a sniffer tap"
        tap = session._sniffers[0]
        tap.put_nowait(("thread/tokenUsage/updated",
                        {"tokenUsage": {"last": {"inputTokens": 4321}}}))
        tap.put_nowait(("item/completed",
                        {"item": {"type": "contextCompaction"}}))
        tap.put_nowait(("turn/completed", {"threadId": "thread-1"}))
        result = await asyncio.wait_for(task, timeout=5.0)
        assert result == {"post_tokens": 4321}
        assert client.calls[0][0] == "thread/compact/start"
        assert session._sniffers == []  # tap removed

    @pytest.mark.asyncio
    async def test_daemon_exit_aborts(self, tmp_agent_dir, sat_config):
        client = _FakeClient()
        session = _make_session(tmp_agent_dir, sat_config, client)
        task = asyncio.ensure_future(session.compact())
        for _ in range(50):
            await asyncio.sleep(0)
            if session._sniffers:
                break
        session._sniffers[0].put_nowait(("__daemon_exit__", {}))
        assert await asyncio.wait_for(task, timeout=5.0) is None

    @pytest.mark.asyncio
    async def test_turn_completed_without_item_fails(
        self, tmp_agent_dir, sat_config,
    ):
        client = _FakeClient()
        session = _make_session(tmp_agent_dir, sat_config, client)
        task = asyncio.ensure_future(session.compact())
        for _ in range(50):
            await asyncio.sleep(0)
            if session._sniffers:
                break
        session._sniffers[0].put_nowait(("turn/completed", {}))
        assert await asyncio.wait_for(task, timeout=5.0) is None

    @pytest.mark.asyncio
    async def test_rpc_error_returns_none(self, tmp_agent_dir, sat_config):
        client = _FakeClient(error=AppServerError("no such thread"))
        session = _make_session(tmp_agent_dir, sat_config, client)
        assert await session.compact() is None
        assert session._sniffers == []


class _FakeListClient(_FakeClient):
    """_FakeClient returning a scripted backgroundTerminals/list result."""

    def __init__(self, result, error: Exception | None = None):
        super().__init__(error)
        self.result = result

    async def request(self, method, params=None, timeout=None):
        await super().request(method, params, timeout)
        return self.result


class TestListBackgroundTerminals:
    @pytest.mark.asyncio
    async def test_rows_pass_through_verbatim(self, tmp_agent_dir, sat_config):
        rows = [{"itemId": "i1", "processId": "p-1", "command": "sleep 40"}]
        client = _FakeListClient({"data": rows})
        session = _make_session(tmp_agent_dir, sat_config, client)
        assert await session.list_background_terminals() == rows
        method, params = client.calls[0]
        assert method == "thread/backgroundTerminals/list"
        assert params == {"threadId": "thread-1"}

    @pytest.mark.asyncio
    async def test_missing_data_is_empty_list_not_none(
        self, tmp_agent_dir, sat_config,
    ):
        # A successful RPC with no rows is a REAL empty list (the proxy
        # resolves on it) — only failure may return None.
        client = _FakeListClient({})
        session = _make_session(tmp_agent_dir, sat_config, client)
        assert await session.list_background_terminals() == []

    @pytest.mark.asyncio
    async def test_rpc_error_returns_none(self, tmp_agent_dir, sat_config):
        client = _FakeClient(error=AppServerError("daemon busy"))
        session = _make_session(tmp_agent_dir, sat_config, client)
        assert await session.list_background_terminals() is None

    @pytest.mark.asyncio
    async def test_dead_client_returns_none_without_rpc(
        self, tmp_agent_dir, sat_config,
    ):
        client = _FakeClient()
        client.is_alive = False
        session = _make_session(tmp_agent_dir, sat_config, client)
        assert await session.list_background_terminals() is None
        assert client.calls == []


class TestForwarderSnifferTap:
    @pytest.mark.asyncio
    async def test_forwarder_feeds_sniffers_and_proxy(
        self, tmp_agent_dir, sat_config,
    ):
        client = _FakeClient()
        session = _make_session(tmp_agent_dir, sat_config, client)
        forwarded: list[dict] = []

        async def _collect(event):
            forwarded.append(event)

        session.set_event_forwarder(_collect)
        tap: asyncio.Queue = asyncio.Queue()
        session._sniffers.append(tap)
        fwd = asyncio.ensure_future(session._run_forwarder())
        client.notif_queue.put_nowait(("item/completed", {"item": {}}))
        client.notif_queue.put_nowait(("__daemon_exit__", {}))
        await asyncio.wait_for(fwd, timeout=5.0)
        assert tap.get_nowait() == ("item/completed", {"item": {}})
        assert tap.get_nowait()[0] == "__daemon_exit__"
        assert forwarded[0]["method"] == "item/completed"


class _FakeWs:
    def __init__(self):
        self.sent: list[dict] = []

    async def enqueue_send(self, msg):
        self.sent.append(msg)


def _manager_with(sessions: dict) -> SessionManager:
    sm = SessionManager.__new__(SessionManager)
    sm.sessions = sessions
    return sm


class TestRpcHandlers:
    @pytest.mark.asyncio
    async def test_compact_no_session(self):
        sm = _manager_with({})
        ws = _FakeWs()
        await sm.codex_compact({"command_id": "c1", "session_id": "nope"}, ws)
        ack = ws.sent[0]
        assert ack["ok"] is False and "not found" in ack["reason"]

    @pytest.mark.asyncio
    async def test_compact_non_codex_session(self):
        sm = _manager_with({"s1": object()})
        ws = _FakeWs()
        await sm.codex_compact({"command_id": "c1", "session_id": "s1"}, ws)
        assert ws.sent[0]["ok"] is False

    @pytest.mark.asyncio
    async def test_compact_success_carries_post_tokens(self):
        class _S:
            async def compact(self):
                return {"post_tokens": 777}
        sm = _manager_with({"s1": _S()})
        ws = _FakeWs()
        await sm.codex_compact({"command_id": "c1", "session_id": "s1"}, ws)
        ack = ws.sent[0]
        assert ack["ok"] is True and ack["post_tokens"] == 777

    @pytest.mark.asyncio
    async def test_compact_exception_is_contained(self):
        class _S:
            async def compact(self):
                raise RuntimeError("boom")
        sm = _manager_with({"s1": _S()})
        ws = _FakeWs()
        await sm.codex_compact({"command_id": "c1", "session_id": "s1"}, ws)
        ack = ws.sent[0]
        assert ack["ok"] is False and "boom" in ack["reason"]

    @pytest.mark.asyncio
    async def test_steer_strict_false_paths(self):
        sm = _manager_with({})
        ws = _FakeWs()
        await sm.codex_steer(
            {"command_id": "c1", "session_id": "nope", "text": "hi"}, ws,
        )
        assert ws.sent[0]["steered"] is False

        class _S:
            async def steer(self, text):
                raise RuntimeError("boom")
        sm = _manager_with({"s1": _S()})
        ws = _FakeWs()
        await sm.codex_steer(
            {"command_id": "c2", "session_id": "s1", "text": "hi"}, ws,
        )
        assert ws.sent[0]["steered"] is False

    @pytest.mark.asyncio
    async def test_steer_empty_text_never_calls(self):
        called = []

        class _S:
            async def steer(self, text):
                called.append(text)
                return True
        sm = _manager_with({"s1": _S()})
        ws = _FakeWs()
        await sm.codex_steer({"command_id": "c1", "session_id": "s1", "text": ""}, ws)
        assert ws.sent[0]["steered"] is False and called == []

    @pytest.mark.asyncio
    async def test_steer_accept_passthrough(self):
        class _S:
            async def steer(self, text):
                return True
        sm = _manager_with({"s1": _S()})
        ws = _FakeWs()
        await sm.codex_steer(
            {"command_id": "c1", "session_id": "s1", "text": "go"}, ws,
        )
        assert ws.sent[0]["steered"] is True

    @pytest.mark.asyncio
    async def test_bg_terminals_no_session(self):
        sm = _manager_with({})
        ws = _FakeWs()
        await sm.codex_bg_terminals(
            {"command_id": "c1", "session_id": "nope"}, ws,
        )
        ack = ws.sent[0]
        assert ack["type"] == "ack" and ack["command_id"] == "c1"
        assert ack["ok"] is False and ack["terminals"] is None
        assert "not found" in ack["reason"]

    @pytest.mark.asyncio
    async def test_bg_terminals_non_codex_session(self):
        sm = _manager_with({"s1": object()})
        ws = _FakeWs()
        await sm.codex_bg_terminals(
            {"command_id": "c1", "session_id": "s1"}, ws,
        )
        assert ws.sent[0]["ok"] is False

    @pytest.mark.asyncio
    async def test_bg_terminals_success_carries_rows(self):
        rows = [{"itemId": "i1", "processId": "p-1"}]

        class _S:
            async def list_background_terminals(self):
                return rows
        sm = _manager_with({"s1": _S()})
        ws = _FakeWs()
        await sm.codex_bg_terminals(
            {"command_id": "c1", "session_id": "s1"}, ws,
        )
        ack = ws.sent[0]
        assert ack["ok"] is True and ack["terminals"] == rows
        assert "reason" not in ack

    @pytest.mark.asyncio
    async def test_bg_terminals_list_failure_acks_not_ok(self):
        # None from the session (RPC failed) must ack ok=False — the proxy's
        # drain is fail-closed and an error-as-empty would resolve badges for
        # terminals that are still running.
        class _S:
            async def list_background_terminals(self):
                return None
        sm = _manager_with({"s1": _S()})
        ws = _FakeWs()
        await sm.codex_bg_terminals(
            {"command_id": "c1", "session_id": "s1"}, ws,
        )
        ack = ws.sent[0]
        assert ack["ok"] is False and ack["terminals"] is None

    @pytest.mark.asyncio
    async def test_bg_terminals_exception_is_contained(self):
        class _S:
            async def list_background_terminals(self):
                raise RuntimeError("boom")
        sm = _manager_with({"s1": _S()})
        ws = _FakeWs()
        await sm.codex_bg_terminals(
            {"command_id": "c1", "session_id": "s1"}, ws,
        )
        ack = ws.sent[0]
        assert ack["ok"] is False and "boom" in ack["reason"]


class TestValidateConfigToml:
    def test_valid_is_silent(self, tmp_path, caplog):
        with caplog.at_level("ERROR", logger="satellite"):
            _validate_config_toml('a = 1\n[t]\nb = "x"\n', tmp_path / "c.toml")
        assert caplog.records == []

    def test_invalid_logs_error(self, tmp_path, caplog):
        with caplog.at_level("ERROR", logger="satellite"):
            _validate_config_toml("bad = \n", tmp_path / "c.toml")
        assert any("INVALID" in r.message for r in caplog.records)

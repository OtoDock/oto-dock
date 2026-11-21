"""Remote steer and compact tunnel twins (proxy side) + the codex
config.toml write-time validation gate.

``RemoteExecutionLayer.steer/compact`` keep the strict accept semantics the
callers rely on: ``dashboard_chat`` queues the message iff steer returned
False, and ``_handle_compact_context`` reports "not supported" on None. Codex
steers through ``codex_steer``, the Claude CLI through ``steer_turn`` with the
result-seen guard held on this side.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.layers.cli.remote import ClaudeRemoteState
from core.layers.cli.settle import SettleController
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.remote.remote_execution import RemoteExecutionLayer


def _claude_state(**fields) -> ClaudeRemoteState:
    translator = ClaudeCLIEventTranslator("sess-1")
    return ClaudeRemoteState(
        translator=translator, settle=SettleController("sess-1", 0, translator),
        **fields,
    )


def _layer(*, execution_path="codex-cli", ack=None,
           send_error: Exception | None = None, engine_state=None):
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._sessions = {
        "sess-1": SimpleNamespace(
            session_id="sess-1", execution_path=execution_path,
            machine_id="machine-1", agent_name="agent-1",
            engine_state=engine_state, last_activity=0.0,
            last_event_at=time.monotonic(), task_turn=False, turn_active=False,
            files_synced=True, engine_turn=None,
        ),
    }
    send = AsyncMock(return_value=ack or {})
    if send_error is not None:
        send.side_effect = send_error
    layer._cm = SimpleNamespace(send_command=send)
    return layer


class TestRemoteSteer:
    @pytest.mark.asyncio
    async def test_unknown_session_false(self):
        layer = _layer()
        assert await layer.steer("nope", "hi") is False
        layer._cm.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_accept_passthrough(self):
        layer = _layer(ack={"steered": True})
        assert await layer.steer("sess-1", "hi") is True
        msg = layer._cm.send_command.await_args.args[1]
        assert msg["type"] == "codex_steer" and msg["text"] == "hi"

    @pytest.mark.asyncio
    async def test_reject_and_rpc_failure_false(self):
        assert await _layer(ack={"steered": False}).steer("sess-1", "hi") is False
        assert await _layer(
            send_error=RuntimeError("timeout"),
        ).steer("sess-1", "hi") is False

    @pytest.mark.asyncio
    async def test_empty_text_false(self):
        layer = _layer()
        assert await layer.steer("sess-1", "") is False
        layer._cm.send_command.assert_not_awaited()


    @pytest.mark.asyncio
    async def test_a_steer_waits_for_its_files_at_the_short_bound_and_tells_the_agent_and_the_chat(self, monkeypatch):
        from core.remote import satellite_connection as sc, upload_inflight
        wait = AsyncMock(return_value=False)
        told = AsyncMock()
        monkeypatch.setattr(upload_inflight, "wait_settled", wait)
        monkeypatch.setattr(upload_inflight, "_tell_chat_files_late", told)
        monkeypatch.setattr(sc, "get_connection_manager", lambda: SimpleNamespace(is_connected=lambda mid: True))
        layer = _layer(ack={"steered": True})
        assert await layer.steer("sess-1", "hi") is True
        wait.assert_awaited_once_with("agent-1", machine_id="machine-1",
                                      ceiling=upload_inflight.BARRIER_TIMEOUT_S)
        told.assert_awaited_once_with("sess-1", "machine-1")
        assert layer._cm.send_command.await_args.args[1]["text"] == f"{upload_inflight.FILES_LATE_PROMPT}\n\nhi"


class TestRemoteClaudeSteer:
    """The Claude CLI twin: ``steer_turn``, the result-seen guard on this side (the satellite cannot tell a foreign
    result from the driven one), strict accept."""

    @pytest.mark.asyncio
    async def test_accept_sends_steer_turn_and_marks_the_state(self):
        state = _claude_state()
        layer = _layer(execution_path="claude-code-cli", ack={"steered": True},
                       engine_state=state)
        assert await layer.steer("sess-1", "hi") is True
        msg = layer._cm.send_command.await_args.args[1]
        assert msg["type"] == "steer_turn" and msg["text"] == "hi"
        assert msg["session_id"] == "sess-1"
        assert state.steer_written is True and state.steer_pending == 0

    @pytest.mark.asyncio
    async def test_refused_while_a_permission_card_is_parked(self):
        """The card is the question: a typed message takes the queue (and
        the card's auto-deny), never the turn's stdin."""
        from core.events.stream_pump import _pending_permissions
        layer = _layer(execution_path="claude-code-cli", ack={"steered": True},
                       engine_state=_claude_state())
        _pending_permissions["sess-1"] = {"request_id": "r1"}
        try:
            assert await layer.steer("sess-1", "go") is False
            layer._cm.send_command.assert_not_awaited()
        finally:
            _pending_permissions.pop("sess-1", None)
        assert await layer.steer("sess-1", "go") is True

    @pytest.mark.asyncio
    async def test_no_turn_state_refuses_without_rpc(self):
        layer = _layer(execution_path="claude-code-cli", ack={"steered": True})
        assert await layer.steer("sess-1", "hi") is False
        layer._cm.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_result_seen_refuses_without_rpc(self):
        layer = _layer(execution_path="claude-code-cli", ack={"steered": True},
                       engine_state=_claude_state(result_seen=True))
        assert await layer.steer("sess-1", "hi") is False
        layer._cm.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reject_and_rpc_failure_leave_the_state_clean(self):
        state = _claude_state()
        layer = _layer(execution_path="claude-code-cli", ack={"steered": False},
                       engine_state=state)
        assert await layer.steer("sess-1", "hi") is False
        assert state.steer_written is False and state.steer_pending == 0
        state = _claude_state()
        layer = _layer(execution_path="claude-code-cli",
                       send_error=RuntimeError("timeout"), engine_state=state)
        assert await layer.steer("sess-1", "hi") is False
        assert state.steer_written is False and state.steer_pending == 0

    @pytest.mark.asyncio
    async def test_pending_counts_while_the_ack_is_in_flight(self):
        import asyncio
        state = _claude_state()
        gate = asyncio.Event()

        async def _slow(mid, msg, timeout=None):
            await gate.wait()
            return {"steered": True}

        layer = _layer(execution_path="claude-code-cli", engine_state=state)
        layer._cm.send_command = _slow
        task = asyncio.create_task(layer.steer("sess-1", "hi"))
        await asyncio.sleep(0)
        assert state.steer_pending == 1
        gate.set()
        assert await task is True
        assert state.steer_pending == 0 and state.steer_written is True


class TestRemoteCompact:
    @pytest.mark.asyncio
    async def test_non_codex_none(self):
        layer = _layer(execution_path="claude-code-cli")
        assert await layer.compact("sess-1") is None
        layer._cm.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_success_returns_post_tokens(self):
        layer = _layer(ack={"ok": True, "post_tokens": 1234})
        assert await layer.compact("sess-1") == {"post_tokens": 1234}
        msg = layer._cm.send_command.await_args.args[1]
        assert msg["type"] == "codex_compact"

    @pytest.mark.asyncio
    async def test_refusal_and_rpc_failure_none(self):
        assert await _layer(
            ack={"ok": False, "reason": "turn active"},
        ).compact("sess-1") is None
        assert await _layer(
            send_error=RuntimeError("timeout"),
        ).compact("sess-1") is None


class TestWriteConfigTomlGuard:
    """The write-time TOML validation gate (never hand codex invalid TOML —
    the strict TUI exits 1 with a blank terminal, the app-server silently
    drops every MCP)."""

    def test_valid_config_written(self, tmp_path):
        from core.layers.codex.layer import CodexCLIExecutionLayer
        CodexCLIExecutionLayer._write_config_toml(
            tmp_path, "prompt",
            '[mcp_servers.x]\ncommand = "python3"\nenv = { "A" = "1" }',
            interactive=True, trusted_cwd="/tmp/w", session_id="s-1",
        )
        import tomllib
        tomllib.loads((tmp_path / "config.toml").read_text())  # parses
        profile = tomllib.loads((tmp_path / "s-1.config.toml").read_text())
        assert profile["features"]["default_mode_request_user_input"] is True
        assert profile["mcp_servers"]["x"]["env"] == {"A": "1"}
        assert profile["developer_instructions"].startswith("prompt")

    def test_invalid_mcp_block_raises_and_writes_nothing(self, tmp_path):
        from core.layers.codex.layer import CodexCLIExecutionLayer
        with pytest.raises(RuntimeError, match="TOML validation"):
            CodexCLIExecutionLayer._write_config_toml(
                tmp_path, "prompt",
                '[mcp_servers.x]\nbroken = ',
            )
        assert not (tmp_path / "config.toml").exists()

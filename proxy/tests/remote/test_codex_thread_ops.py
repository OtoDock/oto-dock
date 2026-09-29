"""Remote steer and compact tunnel twins (proxy side) + the codex
config.toml write-time validation gate.

``RemoteExecutionLayer.steer/compact`` version-gate BEFORE sending (an old
satellite silently drops unknown frames — the ack would only burn its
timeout) and keep the strict accept semantics the callers rely on:
``dashboard_chat`` queues the message iff steer returned False, and
``_handle_compact_context`` reports "not supported" on None. Codex steers
through ``codex_steer`` (satellite ≥ 0.5.98), the Claude CLI through
``steer_turn`` (≥ 0.5.128) with the result-seen guard held on this side.
"""

from __future__ import annotations

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


def _layer(*, execution_path="codex-cli", supported=True, ack=None,
           send_error: Exception | None = None, engine_state=None):
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._sessions = {
        "sess-1": SimpleNamespace(
            session_id="sess-1", execution_path=execution_path,
            machine_id="machine-1", agent_name="agent-1",
            engine_state=engine_state, last_activity=0.0,
        ),
    }
    send = AsyncMock(return_value=ack or {})
    if send_error is not None:
        send.side_effect = send_error
    layer._cm = SimpleNamespace(
        supports_codex_thread_ops=lambda mid: supported,
        satellite_supports_steer_turn=lambda mid: supported,
        send_command=send,
    )
    return layer


class TestRemoteSteer:
    @pytest.mark.asyncio
    async def test_unknown_session_false(self):
        layer = _layer()
        assert await layer.steer("nope", "hi") is False
        layer._cm.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_old_satellite_gated_before_send(self):
        layer = _layer(supported=False)
        assert await layer.steer("sess-1", "hi") is False
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


class TestRemoteClaudeSteer:
    """The Claude CLI twin: ``steer_turn`` on a satellite that handles it,
    the result-seen guard on this side (the satellite cannot tell a foreign
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
    async def test_old_satellite_gated_before_send(self):
        layer = _layer(execution_path="claude-code-cli", supported=False,
                       engine_state=_claude_state())
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

    def test_the_gate_is_the_version(self):
        from core.remote.satellite_connection import (
            SatelliteConnection, SatelliteConnectionManager,
        )
        cm = SatelliteConnectionManager()
        for mid, ver in [("new", "0.5.128"), ("old", "0.5.127"), ("future", "0.6.0")]:
            cm._connections[mid] = SatelliteConnection(
                machine_id=mid, ws=None, satellite_version=ver,
            )
        assert cm.satellite_supports_steer_turn("new")
        assert cm.satellite_supports_steer_turn("future")
        assert not cm.satellite_supports_steer_turn("old")
        assert not cm.satellite_supports_steer_turn("absent")


class TestRemoteCompact:
    @pytest.mark.asyncio
    async def test_non_codex_none(self):
        layer = _layer(execution_path="claude-code-cli")
        assert await layer.compact("sess-1") is None
        layer._cm.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_old_satellite_gated_before_send(self):
        layer = _layer(supported=False)
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
            interactive=True, trusted_cwd="/tmp/w",
        )
        import tomllib
        text = (tmp_path / "config.toml").read_text()
        tomllib.loads(text)  # parses
        assert "default_mode_request_user_input" in text

    def test_invalid_mcp_block_raises_and_writes_nothing(self, tmp_path):
        from core.layers.codex.layer import CodexCLIExecutionLayer
        with pytest.raises(RuntimeError, match="TOML validation"):
            CodexCLIExecutionLayer._write_config_toml(
                tmp_path, "prompt",
                '[mcp_servers.x]\nbroken = ',
            )
        assert not (tmp_path / "config.toml").exists()

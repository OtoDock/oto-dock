"""Remote headless-Codex thread ops (proxy side): steer/compact tunnel twins
+ the codex config.toml write-time validation gate.

``RemoteExecutionLayer.steer/compact`` version-gate BEFORE sending (an old
satellite silently drops unknown frames — the ack would only burn its
timeout) and keep the strict accept semantics the callers rely on:
``dashboard_chat`` queues the message iff steer returned False, and
``_handle_compact_context`` reports "not supported" on None.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.remote.remote_execution import RemoteExecutionLayer


def _layer(*, execution_path="codex-cli", supported=True, ack=None,
           send_error: Exception | None = None):
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._sessions = {
        "sess-1": SimpleNamespace(
            execution_path=execution_path, machine_id="machine-1",
        ),
    }
    send = AsyncMock(return_value=ack or {})
    if send_error is not None:
        send.side_effect = send_error
    layer._cm = SimpleNamespace(
        supports_codex_thread_ops=lambda mid: supported,
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
    async def test_non_codex_false(self):
        layer = _layer(execution_path="claude-code-cli")
        assert await layer.steer("sess-1", "hi") is False
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

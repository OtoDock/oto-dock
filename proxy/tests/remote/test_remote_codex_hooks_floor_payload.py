"""Remote Codex sessions and the PreToolUse permission floor — the start
payload contract (satellite 0.5.118).

The proxy decides which sessions run ``permission_gate.py`` as the Codex
app-server's PreToolUse hook — ONE rule shared with the local layer
(``helpers.codex_hooks_floor``: every UNATTENDED client type, never the
interactive TUI) — and ships it as the ``codex_hooks_floor`` field. The
satellite writes ``[features] hooks = true``, trusts the hook per thread and
sets the deny-only / no-forward hook env. An older satellite ignores the field
and runs the session prompt-gated as before; the proxy warns, never refuses.
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from core.execution_layer import AgentConfig
from core.layers.codex.helpers import codex_hooks_floor
from core.remote.remote_execution import RemoteExecutionLayer
from core.session import session_kind

# The kinds with nobody on the line — the floor applies to exactly these.
_UNATTENDED = [k.name for k in session_kind.KINDS if not k.attended]


def _machine():
    return {
        "capabilities": json.dumps({"local_tunnel_port": 18400, "os": "linux"}),
        "pairing_scope": "admin",
    }


def _config(**overrides):
    base = dict(
        agent_name="test-agent",
        execution_target="machine-1",
        system_prompt="You are a test agent.",
        model="gpt-5.6-sol",
        effort="medium",
        permission_mode="auto",
        client_type="meeting",
        extra_env={},
    )
    base.update(overrides)
    return AgentConfig(**base)


@pytest.fixture()
def layer():
    lay = RemoteExecutionLayer(MagicMock())
    lay._cm.satellite_supports_local_model_provider.return_value = True
    lay._cm.satellite_supports_local_model_catalog.return_value = True
    lay._cm.satellite_supports_codex_hooks_floor.return_value = True
    lay._cm.satellite_version.return_value = "0.5.118"
    lay._cm.satellite_name.return_value = "drill-sat"
    return lay


async def _build(layer, config, execution_path="codex-cli"):
    with patch("storage.remote_store.get_remote_machine", return_value=_machine()):
        return (await layer._build_start_payload("sess-1", config, execution_path)).payload


def test_the_rule_is_the_unattended_set_minus_the_tui():
    for client_type in _UNATTENDED:
        assert codex_hooks_floor(client_type) is True
        assert codex_hooks_floor(client_type, interactive=True) is False
    assert codex_hooks_floor("dashboard") is False
    assert codex_hooks_floor("") is False


class TestCodexHooksFloorPayload:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("client_type", _UNATTENDED)
    async def test_unattended_sessions_carry_the_floor(self, layer, client_type, caplog):
        with caplog.at_level(logging.WARNING, logger="remote-layer"):
            payload = await _build(layer, _config(client_type=client_type))
        assert payload["codex_hooks_floor"] is True
        assert payload["client_type"] == client_type
        assert "permission floor" not in caplog.text

    @pytest.mark.asyncio
    async def test_dashboard_chat_keeps_the_bridge_alone(self, layer):
        payload = await _build(layer, _config(client_type="dashboard", permission_mode="default"))
        assert payload["codex_hooks_floor"] is False

    @pytest.mark.asyncio
    async def test_interactive_task_never_floors(self, layer):
        payload = await _build(layer, _config(client_type="task", interactive=True))
        assert payload["codex_hooks_floor"] is False

    @pytest.mark.asyncio
    async def test_claude_payload_has_no_such_field(self, layer):
        payload = await _build(layer, _config(execution_path="claude-code-cli"), "claude-code-cli")
        assert "codex_hooks_floor" not in payload

    @pytest.mark.asyncio
    async def test_old_satellite_still_gets_the_field_and_a_warning(self, layer, caplog):
        layer._cm.satellite_supports_codex_hooks_floor.return_value = False
        layer._cm.satellite_version.return_value = "0.5.117"
        with caplog.at_level(logging.WARNING, logger="remote-layer"):
            payload = await _build(layer, _config(client_type="task"))
        assert payload["codex_hooks_floor"] is True
        assert "drill-sat runs satellite 0.5.117" in caplog.text
        assert "no PreToolUse permission floor" in caplog.text

    @pytest.mark.asyncio
    async def test_old_satellite_attended_chat_is_silent(self, layer, caplog):
        layer._cm.satellite_supports_codex_hooks_floor.return_value = False
        with caplog.at_level(logging.WARNING, logger="remote-layer"):
            payload = await _build(layer, _config(client_type="dashboard", permission_mode="default"))
        assert payload["codex_hooks_floor"] is False
        assert "permission floor" not in caplog.text

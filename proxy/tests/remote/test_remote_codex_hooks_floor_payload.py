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
    lay._cm.satellite_supports_codex_attended_floor.return_value = True
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
    async def test_old_satellite_attended_chat_names_no_unattended_floor(self, layer, caplog):
        layer._cm.satellite_supports_codex_hooks_floor.return_value = False
        with caplog.at_level(logging.WARNING, logger="remote-layer"):
            payload = await _build(layer, _config(client_type="dashboard", permission_mode="default"))
        assert payload["codex_hooks_floor"] is False
        assert "no PreToolUse permission floor" not in caplog.text


class TestCodexAttendedFloorPayload:
    """An attended chat (satellite 0.5.143): ``codex_attended_floor`` makes
    the satellite trust the hook on every thread with the bridged env, and the
    live ``permission_mode`` lets it derive the approval policy by mode."""

    @pytest.mark.asyncio
    async def test_an_attended_chat_carries_the_field_and_its_mode(self, layer, caplog):
        with caplog.at_level(logging.WARNING, logger="remote-layer"):
            payload = await _build(layer, _config(client_type="dashboard", permission_mode="dontAsk"))
        assert payload["codex_attended_floor"] is True and payload["codex_hooks_floor"] is False
        assert payload["permission_mode"] == "dontAsk"
        assert "until it updates" not in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("client_type", _UNATTENDED)
    async def test_unattended_sessions_and_the_tui_take_no_attended_floor(self, layer, client_type):
        payload = await _build(layer, _config(client_type=client_type))
        assert payload["codex_attended_floor"] is False
        payload = await _build(layer, _config(client_type="dashboard", interactive=True))
        assert payload["codex_attended_floor"] is False

    @pytest.mark.asyncio
    async def test_an_older_satellite_gets_no_live_mode_and_one_warning(self, layer, caplog):
        layer._cm.satellite_supports_codex_attended_floor.return_value = False
        layer._cm.satellite_version.return_value = "0.5.142"
        with caplog.at_level(logging.WARNING, logger="remote-layer"):
            payload = await _build(layer, _config(client_type="dashboard", permission_mode="default"))
        assert payload["codex_attended_floor"] is True  # ignored by that satellite
        assert payload["permission_mode"] == ""          # its plan mode would freeze
        assert caplog.text.count("(< 0.5.143)") == 1
        assert "drill-sat runs satellite 0.5.142" in caplog.text
        # A task on it: no attended-floor warning (its floor is 0.5.118's).
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="remote-layer"):
            await _build(layer, _config(client_type="task"))
        assert "0.5.143" not in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("supported", [True, False])
    async def test_a_mode_change_carries_the_platform_mode_from_0_5_143(self, supported):
        import asyncio
        from unittest.mock import AsyncMock
        from core.layers.codex.remote import CodexRemoteAdapter
        from core.remote.remote_session_info import RemoteSessionInfo
        cm = MagicMock()
        cm.send_fire_and_forget = AsyncMock()
        cm.satellite_supports_codex_attended_floor.return_value = supported
        info = RemoteSessionInfo(session_id="sess-1", machine_id="machine-1",
                                 agent_name="a", execution_path="codex-cli",
                                 event_queue=asyncio.Queue(), allow_full_fs=True)
        assert await CodexRemoteAdapter(None).control_request(
            info, cm, "set_permission_mode", mode="default")
        frame = cm.send_fire_and_forget.call_args.args[1]
        assert frame["kwargs"]["sandbox_mode"] == "danger-full-access"
        if supported:
            assert frame["kwargs"]["permission_mode"] == "default"
        else:
            assert "permission_mode" not in frame["kwargs"]


def test_every_capability_floor_is_a_shipped_satellite_version():
    # A gate above the satellite this tree ships would leave its feature off
    # on every machine (the 0.5.143 floor waits on the closing version bump).
    from core.remote import satellite_connection as sc
    from ws.satellite import SATELLITE_VERSION_LATEST
    latest = tuple(int(x) for x in SATELLITE_VERSION_LATEST.split("."))
    floors = {name: value for name, value in vars(sc).items()
              if name.startswith("_") and name.endswith("_MIN_VERSION") and isinstance(value, tuple)}
    assert "_CODEX_ATTENDED_FLOOR_MIN_VERSION" in floors
    assert {name: v for name, v in floors.items() if v > latest} == {}

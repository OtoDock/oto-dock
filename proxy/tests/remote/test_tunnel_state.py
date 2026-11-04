"""A satellite's local link (satellite 0.5.168): the loopback tunnel its
sessions reach the platform through.

The heartbeat and the auth capabilities report ``tunnel_ok``,
``local_tunnel_port``, ``tunnel_rebinds`` and ``last_tunnel_error``. A link
reported down refuses new sessions on the machine with the reason; a moved
port reaches the next start at once (the row is written off-loop) and makes
every session started on the old port dead to the platform, so it resumes on
the live port. An older satellite reports nothing and nothing changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.execution_layer import AgentConfig
from core.remote import tunnel_state
from core.remote.remote_execution import RemoteExecutionLayer, RemoteSessionInfo
from core.remote.satellite_connection import (
    SatelliteConnection,
    SatelliteConnectionManager,
)


def _cm_with(caps: dict | None = None) -> tuple[SatelliteConnectionManager, SatelliteConnection]:
    cm = SatelliteConnectionManager()
    conn = SatelliteConnection(machine_id="m-1", ws=None, satellite_version="0.5.168")
    conn.capabilities = dict(caps or {"local_tunnel_port": 41000})
    tunnel_state.seed(conn, conn.capabilities)
    cm._connections["m-1"] = conn
    return cm, conn


async def _heartbeat(cm: SatelliteConnectionManager, **fields) -> list[dict]:
    """One heartbeat; the capabilities snapshots it handed the caps lane."""
    with patch.object(cm, "send_fire_and_forget", AsyncMock()), \
         patch.object(cm, "_request_persist") as persist, \
         patch.object(cm, "_retire_moved_port_sessions"):
        await cm._frame_heartbeat("m-1", {"type": "heartbeat", **fields})
    return [c.args[2] for c in persist.call_args_list if c.args[1] == "caps"]


def test_the_fields_are_read_bounded_and_typed():
    got = tunnel_state.read({
        "tunnel_ok": False, "local_tunnel_port": 41001, "tunnel_rebinds": 2,
        "last_tunnel_error": "refused\nat 127.0.0.1:1" + "x" * 500,
    })
    assert got["tunnel_ok"] is False and got["local_tunnel_port"] == 41001
    assert got["tunnel_rebinds"] == 2
    assert "\n" not in got["last_tunnel_error"] and len(got["last_tunnel_error"]) == 200
    # A bool is never a port nor a count, a string never a flag.
    assert tunnel_state.read({"tunnel_ok": "no", "local_tunnel_port": True,
                              "tunnel_rebinds": -1, "last_tunnel_error": 5}) == {}
    assert tunnel_state.read({"local_tunnel_port": 70000}) == {}


@pytest.mark.asyncio
async def test_an_older_satellites_heartbeat_changes_nothing():
    cm, conn = _cm_with()
    caps = await _heartbeat(cm, load={}, active_sessions=0)
    assert conn.tunnel_ok is True and cm.tunnel_ok("m-1") is True
    assert cm.tunnel_port("m-1") == 41000
    assert caps == []


@pytest.mark.asyncio
async def test_a_link_down_refuses_and_back_clears_each_logged_once(caplog):
    cm, conn = _cm_with()
    with caplog.at_level(logging.INFO, logger="otodock.remote.tunnel"):
        caps = await _heartbeat(cm, tunnel_ok=False, local_tunnel_port=41000,
                                tunnel_rebinds=1, last_tunnel_error="connection refused at 127.0.0.1:41000")
        assert cm.tunnel_ok("m-1") is False and conn.tunnel_down_since is not None
        assert len(caps) == 1 and caps[0]["tunnel_ok"] is False
        # Still down: nothing new to say or write.
        assert await _heartbeat(cm, tunnel_ok=False, local_tunnel_port=41000, tunnel_rebinds=1) == []
        await _heartbeat(cm, tunnel_ok=True, local_tunnel_port=41000, tunnel_rebinds=1)
    assert cm.tunnel_ok("m-1") is True and conn.tunnel_down_since is None
    lines = [r.getMessage() for r in caplog.records]
    assert sum("is down" in line for line in lines) == 1
    assert sum("answers again" in line for line in lines) == 1
    assert sum("rebound its local link" in line for line in lines) == 1


@pytest.mark.asyncio
async def test_a_moved_port_updates_the_capabilities_and_reaches_the_next_start(caplog):
    cm, conn = _cm_with()
    with caplog.at_level(logging.WARNING, logger="otodock.remote.tunnel"):
        caps = await _heartbeat(cm, tunnel_ok=True, local_tunnel_port=41999, tunnel_rebinds=1)
    assert conn.capabilities["local_tunnel_port"] == 41999 and cm.tunnel_port("m-1") == 41999
    assert caps[-1]["local_tunnel_port"] == 41999
    assert "moved from port 41000 to 41999" in caplog.text

    # The start payload takes the live port, not the row the lane writes later.
    layer = RemoteExecutionLayer(cm)
    row = {"capabilities": json.dumps({"local_tunnel_port": 41000, "os": "linux"}),
           "pairing_scope": "admin"}
    config = AgentConfig(agent_name="a", execution_target="m-1", model="m",
                         permission_mode="auto", client_type="dashboard", extra_env={})
    with patch("storage.remote_store.get_remote_machine", return_value=row):
        plan = await layer._build_start_payload("sess-1", config, "claude-code-cli")
    assert plan.payload["env"]["PROXY_URL"] == "http://127.0.0.1:41999"
    assert tunnel_state.payload_port(plan.payload) == 41999


def _info(sid: str, port: int, *, turn_active: bool = False) -> RemoteSessionInfo:
    info = RemoteSessionInfo(session_id=sid, machine_id="m-1", agent_name="a",
                             execution_path="claude-code-cli", event_queue=asyncio.Queue(),
                             tunnel_port=port)
    info.turn_active = turn_active
    return info


@pytest.mark.asyncio
async def test_a_session_on_the_old_port_is_dead_and_an_idle_one_is_retired():
    cm, _conn = _cm_with({"local_tunnel_port": 41999})
    cm.is_connected = MagicMock(return_value=True)
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._cm = cm
    layer._sessions = {
        "old-idle": _info("old-idle", 41000),
        "old-busy": _info("old-busy", 41000, turn_active=True),
        "live": _info("live", 41999),
        "unknown": _info("unknown", 0),
    }
    assert await layer.is_session_alive("old-busy") is False
    assert await layer.is_session_process_dead("old-busy") is True
    assert await layer.is_session_alive("live") is True
    assert await layer.is_session_alive("unknown") is True  # never judged
    assert await layer.is_session_process_dead("unknown") is False
    assert layer.moved_port_idle_sessions("m-1") == ["old-idle"]

    closed: list[str] = []

    async def _close(sid):
        layer._sessions.pop(sid, None)
        closed.append(sid)
    layer.close_session = _close
    await layer.retire_moved_port_sessions("m-1")
    assert closed == ["old-idle"]  # the busy one finishes its turn first


@pytest.mark.asyncio
async def test_the_start_is_refused_with_the_reason_before_any_eviction():
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    cm = MagicMock()
    cm.is_connected.return_value = True
    cm.tunnel_ok.return_value = False
    cm.satellite_name.return_value = "Office PC"
    cm.send_command = AsyncMock()
    layer._cm = cm
    layer._sessions = {}
    layer._spawning = set()
    layer._evict_idle_on_machine = AsyncMock()
    from auth.path_policy import SecurityContext
    config = AgentConfig(agent_name="agent-1", execution_target="m-1",
                         execution_path="claude-code-cli",
                         security_context=SecurityContext(role="manager", username="", agent="agent-1",
                                                          is_admin_agent=False, session_scope="agent"))
    with patch("core.remote.remote_session_start.placement_refuses", return_value=False), \
         pytest.raises(tunnel_state.TunnelDown) as refused:
        await layer.start_session("s-1", config)
    assert str(refused.value) == (
        "Office PC cannot start a session right now. Its sessions cannot reach OtoDock "
        "while its satellite restores the link. Try again in a minute, or restart the "
        "satellite on that machine.")
    layer._evict_idle_on_machine.assert_not_awaited()
    cm.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_connection_seeds_the_link_from_its_auth_capabilities():
    cm = SatelliteConnectionManager()
    with patch("storage.remote_store.update_machine_status"), \
         patch("storage.remote_store.update_machine_capabilities"), \
         patch("storage.remote_store.get_remote_machine", return_value=None):
        await cm.register("m-1", MagicMock(close=AsyncMock()), {
            "local_tunnel_port": 41000, "tunnel_ok": False, "tunnel_rebinds": 3,
            "last_tunnel_error": "could not listen again"})
        assert cm.tunnel_ok("m-1") is False
        assert cm.get_connection("m-1").tunnel_error == "could not listen again"
        await cm.register("m-2", MagicMock(close=AsyncMock()), {"local_tunnel_port": 41001})
        assert cm.tunnel_ok("m-2") is True
        assert await cm.drain_persists()


def test_the_live_status_carries_the_link():
    from services.remote import remote_status
    cm, conn = _cm_with()
    conn.tunnel_ok = False
    conn.tunnel_error = "connection refused at 127.0.0.1:41000"
    conn.tunnel_down_since = conn.last_heartbeat - 75
    with patch("core.remote.satellite_connection.get_connection_manager", return_value=cm):
        status = remote_status.get_live_machine_status("m-1")
    assert status["state"] == "online" and status["reachable"] is True
    assert status["link_ok"] is False and status["link_down_s"] >= 75
    assert status["link_error"] == "connection refused at 127.0.0.1:41000"
    with patch("core.remote.satellite_connection.get_connection_manager",
               return_value=SatelliteConnectionManager()), \
         patch("storage.remote_store.get_remote_machine", return_value=None):
        gone = remote_status.get_live_machine_status("m-9")
    assert gone["link_ok"] is True and gone["link_down_s"] is None and gone["link_error"] == ""

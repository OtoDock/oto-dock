"""Remote capacity eviction — parity with the local admit path.

A satellite at its session ceiling used to hard-refuse every new spawn
("at capacity — too many active sessions") while idle sessions held the
slots. ``_evict_idle_on_machine`` now mirrors the local
``_admit_with_eviction`` move at the ``start_session`` capacity pre-check:
close the most-idle idle sessions this proxy tracks on that machine (never
a turn in flight, never younger than the local floor) until the cached
count shows headroom. ``debit_reported_session`` keeps the heartbeat-cached
count honest between heartbeats so the loop and the post-eviction re-check
observe the reclaimed room immediately.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from core.remote.remote_execution import RemoteExecutionLayer, RemoteSessionInfo
from core.remote.satellite_connection import SatelliteConnectionManager


def _info(sid: str, machine: str, idle_s: float, *, turn_active: bool = False,
          alive: bool = True) -> RemoteSessionInfo:
    info = RemoteSessionInfo(
        session_id=sid,
        machine_id=machine,
        agent_name="agent-1",
        execution_path="claude-code-cli",
        event_queue=asyncio.Queue(),
    )
    info.last_activity = time.monotonic() - idle_s
    info.turn_active = turn_active
    info.alive = alive
    return info


def _layer_with_capacity(sessions: dict, *, live: int, cap: int):
    """Layer stub whose machine_at_capacity tracks a mutable live count and
    whose close_session behaves like the real one for the loop's purposes:
    pops the registry and debits the count."""
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._sessions = dict(sessions)
    state = {"live": live}
    cm = MagicMock()
    cm.machine_at_capacity = MagicMock(side_effect=lambda m: state["live"] >= cap)
    layer._cm = cm
    closed: list[str] = []

    async def _close(sid):
        layer._sessions.pop(sid, None)
        state["live"] -= 1
        closed.append(sid)

    layer.close_session = _close
    return layer, closed, state


@pytest.fixture(autouse=True)
def _fixed_knobs(monkeypatch):
    import config as app_config
    monkeypatch.setattr(app_config, "SESSION_EVICT_FLOOR_S", 300)
    monkeypatch.setattr(app_config, "get_idle_timeout", lambda: 900)


class TestEvictIdleOnMachine:
    @pytest.mark.asyncio
    async def test_evicts_oldest_idle_first_until_headroom(self):
        layer, closed, state = _layer_with_capacity({
            "old": _info("old", "m-1", idle_s=1200),
            "mid": _info("mid", "m-1", idle_s=600),
            "young": _info("young", "m-1", idle_s=60),      # under floor
            "busy": _info("busy", "m-1", idle_s=2000, turn_active=True),
            "other": _info("other", "m-2", idle_s=5000),    # different machine
        }, live=3, cap=3)

        n = await layer._evict_idle_on_machine("m-1")
        # One slot needed → exactly one eviction, the MOST idle first.
        assert n == 1
        assert closed == ["old"]
        assert "mid" in layer._sessions
        assert "busy" in layer._sessions
        assert "other" in layer._sessions

    @pytest.mark.asyncio
    async def test_keeps_evicting_while_over_ceiling(self):
        layer, closed, _ = _layer_with_capacity({
            "old": _info("old", "m-1", idle_s=1200),
            "mid": _info("mid", "m-1", idle_s=600),
        }, live=4, cap=3)  # two over (needs live<3 → two evictions)

        n = await layer._evict_idle_on_machine("m-1")
        assert n == 2
        assert closed == ["old", "mid"]

    @pytest.mark.asyncio
    async def test_floor_protects_recent_and_active_sessions(self):
        layer, closed, state = _layer_with_capacity({
            "young": _info("young", "m-1", idle_s=120),      # under 300s floor
            "busy": _info("busy", "m-1", idle_s=4000, turn_active=True),
        }, live=4, cap=3)

        n = await layer._evict_idle_on_machine("m-1")
        # Nothing evictable → break with capacity still standing (the caller
        # then raises the same at-capacity error as before).
        assert n == 0
        assert closed == []
        assert state["live"] == 4

    @pytest.mark.asyncio
    async def test_no_op_when_machine_has_headroom(self):
        layer, closed, _ = _layer_with_capacity({
            "old": _info("old", "m-1", idle_s=1200),
        }, live=2, cap=3)
        assert await layer._evict_idle_on_machine("m-1") == 0
        assert closed == []


class TestDebitReportedSession:
    def test_debits_and_floors_at_zero(self):
        cm = SatelliteConnectionManager.__new__(SatelliteConnectionManager)
        conn = MagicMock()
        conn.reported_sessions = 2
        cm._connections = {"m-1": conn}

        cm.debit_reported_session("m-1")
        assert conn.reported_sessions == 1
        cm.debit_reported_session("m-1")
        cm.debit_reported_session("m-1")  # would go negative — floors at 0
        assert conn.reported_sessions == 0
        cm.debit_reported_session("m-unknown")  # no connection: no crash

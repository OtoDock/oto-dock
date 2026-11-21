"""Remote capacity eviction — parity with the local admit path.

A satellite at its session ceiling used to hard-refuse every new spawn
("at capacity — too many active sessions") while idle sessions held the
slots. ``_evict_idle_on_machine`` now mirrors the local
``_admit_with_eviction`` move at the ``start_session`` capacity pre-check:
close the most-idle idle sessions this proxy tracks on that machine (never
a turn in flight, never younger than the local floor) until the cached
count shows headroom. ``debit_reported_session`` keeps the heartbeat-cached
count honest between heartbeats so the loop and the post-eviction re-check
observe the reclaimed room immediately. A terminal goes only after every
headless session, when nobody has it open and only its background report
keeps it: a stale report must not fill a machine for the whole ceiling.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from core.remote.remote_execution import RemoteExecutionLayer, RemoteSessionInfo
from core.remote.satellite_connection import SatelliteConnectionManager
from core.session import interactive_session as isess, session_state
from storage.agents.schema import DEFAULT_EXECUTION_PATH


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
    cm.debit_reported_session = MagicMock(side_effect=lambda m: state.update(live=state["live"] - 1))
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


class _FakeRemotePty:
    pid = 4242

    def __init__(self):
        self.closed = False

    def close(self, signal_child: bool = True) -> None:
        self.closed = True

    def scrollback(self) -> bytes:
        return b""

    def write(self, data: bytes) -> None:
        pass


@pytest_asyncio.fixture
async def terminal(monkeypatch):
    """Real terminals on a fake satellite. ``idle_s`` ages both clocks: a
    turn's Stop stamps the last hook as its signal stamps the idle clock."""
    from core import concurrency
    from core.remote import remote_pty, satellite_connection

    async def _spawn(**_kw):
        return _FakeRemotePty()

    class _Manager:
        def satellite_os(self, _machine_id):
            return "linux"

        def is_compute(self, _machine_id):
            return False

        def compute_machine_ids(self):
            return frozenset()

        def machine_memory_report(self, _machine_id, compute=False):
            return None   # no report: not checked by memory

        def is_pty_in_grace(self, _machine_id):
            return False

    concurrency.init()
    isess._lock = None
    isess._sessions.clear()
    monkeypatch.setattr(remote_pty, "spawn_remote_pty", _spawn)
    monkeypatch.setattr(satellite_connection, "get_connection_manager", _Manager)
    monkeypatch.setattr(session_state, "cached_idle_timeout", AsyncMock(return_value=900))
    made = []

    async def _make(sid, *, idle_s, machine="m-1", kinds=("shell",)):
        s = await isess.register_remote(
            session_id=sid, chat_id=f"chat-{sid}", agent_name="agent", machine_id=machine,
            execution_path=DEFAULT_EXECUTION_PATH, config_payload={})
        made.append(sid)
        now = time.monotonic()
        s.last_activity = now - idle_s
        session_state._session_hook_activity[sid] = now - idle_s
        isess.note_background_report(sid, list(kinds))
        return s

    yield _make
    await isess.close_all(reason="test-teardown")
    isess._sessions.clear()
    for sid in made:
        session_state._session_hook_activity.pop(sid, None)


def _layer_on_real_count(*, reported: int, cap: int):
    """The real capacity check and debit over one connection's cached count."""
    cm = SatelliteConnectionManager.__new__(SatelliteConnectionManager)
    conn = SimpleNamespace(reported_sessions=reported, max_sessions=cap, capabilities={})
    cm._connections = {"m-1": conn}
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._sessions = {}
    layer._cm = cm
    return layer, conn


class TestHeldTerminals:
    @pytest.mark.asyncio
    async def test_a_held_terminal_goes_after_every_headless_candidate(self, terminal):
        # Older than the headless session, and still second: a headless
        # session resumes where it was, a terminal's work ends with it.
        held = await terminal("held", idle_s=5000)
        layer, closed, state = _layer_with_capacity({
            "old": _info("old", "m-1", idle_s=1200),
        }, live=3, cap=3)
        assert await layer._evict_idle_on_machine("m-1") == 1
        assert closed == ["old"] and held.alive

        layer, closed, state = _layer_with_capacity({
            "old": _info("old", "m-1", idle_s=1200),
        }, live=4, cap=3)
        assert await layer._evict_idle_on_machine("m-1") == 2
        assert closed == ["old"] and not held.alive
        assert state["live"] == 2

    @pytest.mark.asyncio
    async def test_exactly_one_goes_when_one_slot_is_needed(self, terminal, caplog):
        # A terminal's close does not debit the cached count: without the
        # loop's own debit every held terminal would go until the heartbeat.
        older = await terminal("older", idle_s=2000)
        newer = await terminal("newer", idle_s=1500)
        layer, conn = _layer_on_real_count(reported=3, cap=3)
        with caplog.at_level("INFO", logger="remote-layer"):
            assert await layer._evict_idle_on_machine("m-1") == 1
        assert "only its background report keeps it" in caplog.text
        assert not older.alive and newer.alive
        assert conn.reported_sessions == 2
        assert not layer._cm.machine_at_capacity("m-1")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("keeps", [
        "a viewer", "a local otodock terminal", "an open turn", "a parked question",
        "no report", "housekeeping only", "a recent hook", "another machine",
        "past the ceiling",
    ])
    async def test_what_keeps_a_terminal_at_the_ceiling(self, terminal, monkeypatch, keeps):
        import config as app_config
        monkeypatch.setattr(app_config, "BACKGROUND_WORK_CEILING_S", 3000)
        s = await terminal(
            "kept", idle_s=5000 if keeps == "past the ceiling" else 2000,
            machine="m-2" if keeps == "another machine" else "m-1",
            kinds={"no report": (), "housekeeping only": ("dream",)}.get(keeps, ("shell",)))
        if keeps == "a viewer":
            s.add_output_listener(lambda b: None)
            s.last_activity = time.monotonic() - 2000
        elif keeps == "a local otodock terminal":
            s.otodock_attached = True
        elif keeps == "an open turn":
            s._turn_open = True
        elif keeps == "a parked question":
            s._apply_turn_signal("end_turn", question_pending=True)
            s.last_activity = time.monotonic() - 2000
        elif keeps == "a recent hook":
            session_state.record_hook_activity("kept")
        layer, conn = _layer_on_real_count(reported=3, cap=3)
        assert await layer._evict_idle_on_machine("m-1") == 0
        assert s.alive and conn.reported_sessions == 3
        assert layer._cm.machine_at_capacity("m-1")

    @pytest.mark.asyncio
    async def test_a_terminal_under_the_idle_timeout_stays_at_the_ceiling(self, terminal):
        # Past the eviction floor and under the idle timeout, the timeout
        # still keeps it, as it keeps the plain idle terminal beside it: the
        # one with running work must not be the first to go.
        held = await terminal("held", idle_s=600)
        plain = await terminal("plain", idle_s=800, kinds=())
        layer, conn = _layer_on_real_count(reported=3, cap=3)
        assert await layer._evict_idle_on_machine("m-1") == 0
        assert held.alive and plain.alive

    @pytest.mark.asyncio
    async def test_two_spawns_asking_at_once_take_one_terminal(self, terminal):
        # A close awaits: the count is debited before it, or the second spawn
        # still reads a full machine and ends a second terminal's work.
        first = await terminal("first", idle_s=2000)
        second = await terminal("second", idle_s=1500)
        layer, conn = _layer_on_real_count(reported=3, cap=3)
        counts = await asyncio.gather(
            layer._evict_idle_on_machine("m-1"), layer._evict_idle_on_machine("m-1"))
        assert sorted(counts) == [0, 1]
        assert not first.alive and second.alive
        assert conn.reported_sessions == 2

    @pytest.mark.asyncio
    async def test_a_closing_terminal_is_not_taken_twice(self, terminal):
        # Two slots needed by two spawns at once: each takes its own terminal,
        # or the count reads a room the machine does not have.
        first = await terminal("first", idle_s=2000)
        second = await terminal("second", idle_s=1500)
        third = await terminal("third", idle_s=1000)
        layer, conn = _layer_on_real_count(reported=4, cap=3)
        await asyncio.gather(
            layer._evict_idle_on_machine("m-1"), layer._evict_idle_on_machine("m-1"))
        assert not first.alive and not second.alive and third.alive
        assert conn.reported_sessions == 2


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

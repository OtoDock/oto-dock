"""The remote reaper's tick waits on no machine: each machine's sessions are
probed and closed in a task of that machine's own, and each sweep runs as a
task of its own. A session's idle seconds, which the wedge checks of the
dashboard and of a task lane read, count from its last stream event."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.layers.cli.remote import ClaudeRemoteState
from core.layers.cli.settle import SettleController
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.remote import remote_reaper as rr
from core.remote.remote_execution import RemoteExecutionLayer, RemoteSessionInfo
from tests._timing import bound


class _Cm:
    def is_connected(self, mid):
        return True

    def is_session_in_grace(self, mid, sid):
        return False


def _idle(machine_id: str, *, turn_active: bool = False) -> SimpleNamespace:
    return SimpleNamespace(last_activity=time.monotonic() - 10_000, machine_id=machine_id,
                           turn_active=turn_active, cli_dead=False)


def _engine_gone(machine_id: str, *, turn_active: bool = False) -> SimpleNamespace:
    """A session used a moment ago whose engine is gone."""
    return SimpleNamespace(last_activity=time.monotonic(), machine_id=machine_id,
                           turn_active=turn_active, cli_dead=True)


@pytest.fixture
def world(monkeypatch):
    from core.session import session_manager, session_state
    from services.remote import chat_move, switch_leftovers
    from services.scheduler import run_recovery
    closed: list[tuple[str, float]] = []
    spawning: set[str] = set()
    started = time.monotonic()
    hold = asyncio.Event()

    async def close_session(sid):
        if layer._sessions[sid].machine_id == "m-frozen":
            await hold.wait()          # the close's ack never comes (15 s shipped)
        closed.append((sid, time.monotonic() - started))
        layer._sessions.pop(sid, None)

    layer = SimpleNamespace(_sessions={}, _cm=_Cm(), close_session=close_session,
                            is_spawning=lambda sid: sid in spawning,
                            probe_session_process_dead=AsyncMock(return_value=True))
    monkeypatch.setattr(session_manager, "_get_remote_layer", lambda: layer)
    monkeypatch.setattr(session_state, "cached_idle_timeout", AsyncMock(return_value=60.0))
    monkeypatch.setattr(session_state, "has_pending_prompt", lambda sid: False)
    monkeypatch.setattr(rr, "_idle_session_has_pending_work", lambda *a, **k: False)
    sweeps = SimpleNamespace(recovery=AsyncMock(), move=AsyncMock(), leftovers=AsyncMock())
    monkeypatch.setattr(run_recovery, "sweep_expired", sweeps.recovery)
    monkeypatch.setattr(chat_move, "sweep", sweeps.move)
    monkeypatch.setattr(switch_leftovers, "sweep", sweeps.leftovers)
    rr._running.clear()
    yield SimpleNamespace(layer=layer, closed=closed, hold=hold, sweeps=sweeps, spawning=spawning)
    hold.set()
    rr._running.clear()


async def _until(check, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_machine_that_never_answers_delays_no_other_machines_reaping(world):
    """Measured before: a healthy machine's idle session was closed after
    every close on the silent machine had waited out its ack."""
    for n in range(3):
        world.layer._sessions[f"s-f{n}"] = _idle("m-frozen")
    world.layer._sessions["s-ok"] = _idle("m-ok")
    started = time.monotonic()
    await rr.reap_once()
    assert time.monotonic() - started < bound(0.5)             # the tick waits on nothing
    await _until(lambda: [sid for sid, _ in world.closed] == ["s-ok"])
    assert world.closed[0][1] < bound(1.0)
    # The silent machine's reaping still runs: the next tick skips it and
    # serves the others.
    world.layer._sessions["s-ok2"] = _idle("m-ok")
    await rr.reap_once()
    await _until(lambda: [sid for sid, _ in world.closed] == ["s-ok", "s-ok2"])
    assert set(world.layer._sessions) == {"s-f0", "s-f1", "s-f2"}
    world.hold.set()
    await rr.settle()
    assert world.layer._sessions == {}


@pytest.mark.asyncio
async def test_a_session_is_judged_again_right_before_its_close(world):
    world.layer._sessions["s-1"] = _idle("m-frozen")
    world.layer._sessions["s-2"] = _idle("m-frozen")
    await rr.reap_once()
    await asyncio.sleep(0.05)
    # While the first close waits, the second session gets a turn.
    world.layer._sessions["s-2"].last_activity = time.monotonic()
    world.hold.set()
    await rr.settle()
    assert [sid for sid, _ in world.closed] == ["s-1"]
    assert "s-2" in world.layer._sessions


@pytest.mark.asyncio
async def test_an_in_flight_turn_is_reaped_only_when_its_machine_says_it_is_dead(world):
    world.layer._sessions["s-dead"] = _idle("m-ok", turn_active=True)
    world.layer._sessions["s-dead"].last_activity = time.monotonic() - 120
    world.layer.probe_session_process_dead = AsyncMock(return_value=False)
    await rr.reap_once()
    await rr.settle()
    assert world.closed == []
    world.layer.probe_session_process_dead = AsyncMock(return_value=True)
    await rr.reap_once()
    await rr.settle()
    assert [sid for sid, _ in world.closed] == ["s-dead"]


@pytest.mark.asyncio
async def test_a_session_whose_engine_is_gone_is_closed_at_the_next_tick(world, monkeypatch):
    """Its record held a slot and a seat for nothing until the idle timeout:
    the next message starts a session either way. A turn still open, a start
    under way and a prompt that waits on a person keep it."""
    from core.session import session_state
    world.layer._sessions["s-gone"] = _engine_gone("m-ok")
    world.layer._sessions["s-turn"] = _engine_gone("m-ok", turn_active=True)
    world.layer._sessions["s-start"] = _engine_gone("m-ok")
    world.layer._sessions["s-asked"] = _engine_gone("m-ok")
    world.layer._sessions["s-fine"] = _idle("m-ok")
    world.layer._sessions["s-fine"].last_activity = time.monotonic()
    world.spawning.add("s-start")
    monkeypatch.setattr(session_state, "has_pending_prompt", lambda sid: sid == "s-asked")
    await rr.reap_once()
    await rr.settle()
    assert [sid for sid, _ in world.closed] == ["s-gone"]
    assert set(world.layer._sessions) == {"s-turn", "s-start", "s-asked", "s-fine"}


@pytest.mark.asyncio
async def test_a_session_its_machine_says_is_gone_is_closed_without_waiting_for_the_tick(world):
    world.layer._sessions["s-gone"] = _engine_gone("m-ok")
    world.layer._sessions["s-other"] = _engine_gone("m-ok")
    assert rr.reap_dead(world.layer, "m-ok", "s-gone") is True
    await rr.settle()
    # That session alone: the tick's own pass takes the rest.
    assert [sid for sid, _ in world.closed] == ["s-gone"]


@pytest.mark.asyncio
async def test_a_sweep_that_still_runs_holds_neither_the_tick_nor_the_reaping(world):
    stuck = asyncio.Event()

    async def sweep_that_waits():
        await stuck.wait()

    world.sweeps.move.side_effect = sweep_that_waits
    world.layer._sessions["s-ok"] = _idle("m-ok")
    await rr.reap_once()
    await _until(lambda: [sid for sid, _ in world.closed] == ["s-ok"])
    await rr.reap_once()                       # the sweep still runs: not started twice
    await asyncio.sleep(0.02)
    assert world.sweeps.move.await_count == 1
    assert world.sweeps.recovery.await_count == 2 and world.sweeps.leftovers.await_count == 2
    stuck.set()
    await rr.settle()


@pytest.mark.asyncio
async def test_an_away_machines_open_turn_is_ended_for_its_reader_before_the_close(world, monkeypatch):
    """A held turn silent past the turn ceiling while its machine is away:
    the close alone told the reader nothing, and a turn with a tool open
    waited for ever."""
    import config as app_config
    from services.remote import machine_sessions
    monkeypatch.setattr(app_config, "CLAUDE_TIMEOUT", 100)
    monkeypatch.setattr(world.layer._cm, "is_connected", lambda mid: False, raising=False)
    monkeypatch.setattr(world.layer._cm, "is_session_in_grace", lambda mid, sid: True, raising=False)
    order: list[str] = []

    async def _end(layer, info):
        order.append(f"turn ended:{info.machine_id}")
    monkeypatch.setattr(machine_sessions, "end_away_turn", _end)
    real_close = world.layer.close_session

    async def _close(sid):
        order.append(f"closed:{sid}")
        await real_close(sid)
    world.layer.close_session = _close
    world.layer._sessions["s-away"] = _idle("m-away", turn_active=True)
    world.layer._sessions["s-idle"] = _idle("m-away")
    await rr.reap_once()
    await rr.settle()
    assert order == ["turn ended:m-away", "closed:s-away", "closed:s-idle"]


@pytest.mark.asyncio
async def test_a_connected_machines_turn_is_closed_as_before(world, monkeypatch):
    from services.remote import machine_sessions
    ended: list[str] = []

    async def _end(layer, info):
        ended.append(info.machine_id)
    monkeypatch.setattr(machine_sessions, "end_away_turn", _end)
    world.layer._sessions["s-dead"] = _idle("m-ok", turn_active=True)
    await rr.reap_once()
    await rr.settle()
    assert ended == [] and [sid for sid, _ in world.closed] == ["s-dead"]


# ---------------------------------------------------------------------------
# A session's idle seconds
# ---------------------------------------------------------------------------

def _held(*, idle_s: float, in_grace: bool = False):
    """The remote layer holding one Claude Code session whose last event came
    ``idle_s`` ago, and that session's record."""
    cm = MagicMock()
    cm.send_fire_and_forget = AsyncMock()
    cm.is_session_in_grace = MagicMock(return_value=in_grace)
    layer = RemoteExecutionLayer(cm)
    info = RemoteSessionInfo(session_id="s-1", machine_id="m-1", agent_name="agent-1",
                             execution_path="claude-code-cli", event_queue=asyncio.Queue())
    info.last_activity = time.monotonic() - idle_s
    layer._sessions[info.session_id] = info
    return layer, info


def test_a_sessions_idle_seconds_count_from_its_last_activity():
    layer, _info = _held(idle_s=42.0)
    assert 41.0 < layer.session_idle_seconds("s-1") < 50.0
    assert layer.session_idle_seconds("no-such") is None


def test_a_session_whose_machine_is_reconnecting_has_no_idle_seconds():
    """Held in the reconnect grace the session is reconnecting, not idle: a
    wedge check that reads None leaves the turn to the grace's own expiry."""
    layer, _info = _held(idle_s=10_000.0, in_grace=True)
    assert layer.session_idle_seconds("s-1") is None
    layer._cm.is_session_in_grace.assert_called_once_with("m-1", "s-1")


@pytest.mark.asyncio
async def test_an_event_of_the_turns_stream_restarts_the_idle_seconds():
    """A long turn that keeps streaming never reads as idle: the idle seconds
    and the turn's own silence clock restart at each event of its stream."""
    layer, info = _held(idle_s=100.0)
    info.last_event_at = info.last_activity
    translator = ClaudeCLIEventTranslator(info.session_id)
    info.engine_state = ClaudeRemoteState(
        translator=translator, settle=SettleController(info.session_id, 0, translator))
    info.current_send_command_id = "cmd-1"
    info.event_queue.put_nowait({"type": "stream_event", "_command_id": "cmd-1", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "On it."}}})
    stream = layer._adapter(info).stream_turn(info, layer._cm)
    try:
        await asyncio.wait_for(stream.__anext__(), 5)
        assert layer.session_idle_seconds("s-1") < bound(5.0)
        assert time.monotonic() - info.last_event_at < bound(5.0)
    finally:
        await stream.aclose()
        await asyncio.sleep(0)   # the stop the closed stream sends goes out on its own task

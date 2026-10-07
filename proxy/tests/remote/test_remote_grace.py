"""Satellite WS-drop reconnect-grace + queue re-adoption.

On a transient WS drop the in-flight session event queues are HELD in a
per-machine grace area (not terminated); a reconnect re-adopts the SAME queue
objects so the turn continues; only a grace-window timeout terminates them with
a durable ⚠-incomplete marker. A close/abort during grace drops the held
session so a reconnect won't resume an abandoned turn.
"""

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import pytest


def _fake_conn(session_queues, exec_paths=None):
    """A minimal stand-in for SatelliteConnection (deregister only touches
    session_queues / session_execution_paths / writer_task)."""
    return types.SimpleNamespace(
        session_queues=session_queues,
        session_execution_paths=exec_paths or {},
        writer_task=None,
    )


@pytest.mark.asyncio
async def test_deregister_holds_sessions_in_grace_not_terminated():
    from core.remote.satellite_connection import SatelliteConnectionManager

    cm = SatelliteConnectionManager()
    q = asyncio.Queue(maxsize=10)
    cm._connections["m"] = _fake_conn({"s": q}, {"s": "codex-cli"})

    await cm.deregister("m")

    # Held in grace, NOT terminated (no error/None injected on the drop edge).
    assert cm.is_session_in_grace("m", "s")
    assert cm.is_session_stream_attached("m", "s")  # reap sees "reconnecting"
    assert cm._grace_sessions["m"]["s"][0] is q       # SAME queue object
    assert cm._grace_sessions["m"]["s"][1] == "codex-cli"
    assert q.empty()
    assert "m" in cm._grace_timers and not cm._grace_timers["m"].done()

    cm._grace_timers["m"].cancel()


@pytest.mark.asyncio
async def test_register_readopts_held_session_and_events_flow():
    from core.remote.satellite_connection import SatelliteConnectionManager

    cm = SatelliteConnectionManager()
    cm.send_command = AsyncMock()  # silence the reconnect _kick_verify task
    q = asyncio.Queue(maxsize=10)
    cm._connections["m"] = _fake_conn({"s": q}, {"s": "codex-cli"})
    await cm.deregister("m")
    assert cm.is_session_in_grace("m", "s")

    conn = await cm.register("m", MagicMock(), {})
    try:
        # The SAME queue object is restored into the new connection, so the
        # producer's info.event_queue reference stays valid.
        assert conn.session_queues["s"] is q
        assert conn.session_execution_paths["s"] == "codex-cli"
        # Grace cleared + timer cancelled.
        assert "m" not in cm._grace_sessions
        assert "m" not in cm._grace_timers
        # A post-reconnect session_event now flows to the preserved queue.
        await cm.handle_message("m", {
            "type": "session_event", "session_id": "s",
            "event": {"hello": 1},
        })
        queued = q.get_nowait()
        assert queued == {"hello": 1}
    finally:
        if conn.writer_task:
            conn.writer_task.cancel()


@pytest.mark.asyncio
async def test_grace_expiry_terminates_with_durable_marker():
    from core.remote import satellite_connection as sc
    from core.remote.satellite_connection import SatelliteConnectionManager

    cm = SatelliteConnectionManager()
    q = asyncio.Queue(maxsize=10)
    cm._connections["m"] = _fake_conn({"s": q}, {"s": "codex-cli"})

    orig = sc._GRACE_WINDOW_S
    sc._GRACE_WINDOW_S = 0.01  # fire the timer fast
    try:
        await cm.deregister("m")
        await asyncio.sleep(0.06)
    finally:
        sc._GRACE_WINDOW_S = orig

    # Terminal injected: error (durable marker) then the DONE sentinel.
    err = q.get_nowait()
    assert err["type"] == "error"
    assert err.get("durable_marker") is True
    assert "incomplete" in err["message"].lower()
    queued = q.get_nowait()
    assert queued is None
    # Grace state cleared on expiry.
    assert "m" not in cm._grace_sessions
    assert not cm.is_session_in_grace("m", "s")


@pytest.mark.asyncio
async def test_drop_grace_session_on_abort_removes_and_does_not_terminate():
    from core.remote.satellite_connection import SatelliteConnectionManager

    cm = SatelliteConnectionManager()
    q = asyncio.Queue(maxsize=10)
    cm._connections["m"] = _fake_conn({"s": q}, {"s": "claude-code-cli"})
    await cm.deregister("m")
    assert cm.is_session_in_grace("m", "s")

    cm.drop_grace_session("m", "s")  # the abort / close-during-grace path

    assert not cm.is_session_in_grace("m", "s")
    assert "m" not in cm._grace_sessions
    assert "m" not in cm._grace_timers          # timer cancelled + popped
    assert q.empty()                            # dropped, NOT terminated


@pytest.mark.asyncio
async def test_remove_session_queue_drops_grace():
    """close_session → remove_session_queue must also clear a grace-held copy."""
    from core.remote.satellite_connection import SatelliteConnectionManager

    cm = SatelliteConnectionManager()
    q = asyncio.Queue(maxsize=10)
    cm._connections["m"] = _fake_conn({"s": q}, {"s": "codex-cli"})
    await cm.deregister("m")
    assert cm.is_session_in_grace("m", "s")

    cm.remove_session_queue("m", "s")

    assert not cm.is_session_in_grace("m", "s")
    assert "m" not in cm._grace_timers


class TestPumpDurableMarker:
    """Every ERROR that ends a turn is PERSISTED by the pump as the chat's
    ``turn_ended`` system row (the card a reload shows), not just forwarded
    live, so a refresh after a lost turn shows its reason instead of a
    silent truncation. The pump's `_run` BREAKS on ERROR, so the row is saved
    on that path (it relies on the `finally`'s _save_turn_blocks). The remote
    adapter maps the grace expiry's `durable_marker` to the ``lost`` ending
    before the pump sees it."""

    @staticmethod
    def _mk_pump(chat_id, session_id, saved, monkeypatch):
        import asyncio as _aio
        from core.events import stream_pump as sp
        monkeypatch.setattr(sp.task_store, "add_chat_message",
                            lambda *a, **k: saved.append((a, k)))
        # The turn's rows go through the batch: one (args, kwargs) per row,
        # in the shape add_chat_message was called with.
        monkeypatch.setattr(
            sp.task_store, "add_chat_messages_batch",
            lambda chat_id, rows: saved.extend(
                ((chat_id, role, content), {"event_type": et, "event_data": ed})
                for role, content, et, ed in rows))
        monkeypatch.setattr(sp.task_store, "get_last_chat_message_id",
                            lambda cid: len(saved))
        eq: _aio.Queue = _aio.Queue()

        async def _idle_producer():
            await _aio.sleep(3600)

        prod = _aio.create_task(_idle_producer())
        return sp, eq, sp.ChatStreamPump(chat_id, session_id, prod, eq, None)

    @staticmethod
    def _ended_rows(saved) -> list[dict]:
        import json as _json
        out = []
        for a, k in saved:
            if len(a) >= 2 and a[1] == "event" and k.get("event_type") == "system":
                block = _json.loads(k.get("event_data") or "{}")
                if block.get("subtype") == "turn_ended":
                    out.append(block)
        return out

    @pytest.mark.asyncio
    async def test_a_plain_error_is_persisted_as_the_error_ending(self, monkeypatch):
        saved: list = []
        sp, eq, pump = self._mk_pump("chat-y", "sess-y", saved, monkeypatch)
        await eq.put(sp.CommonEvent(type=sp.ERROR,
                                    data={"message": "Remote session timeout"}))
        await pump._run()
        [block] = self._ended_rows(saved)
        assert block["reason"] == "error" and block["detail"] == "Remote session timeout"
        assert [a for a, k in saved if len(a) >= 3 and a[1] == "assistant"] == []

    @pytest.mark.asyncio
    async def test_a_typed_ending_is_persisted_once_and_forwarded_typed(self, monkeypatch):
        """A turn the engine stopped (a decline, a usage limit) persists its
        card row (a reload shows it on both engines) and forwards the typed
        reason; the runner and the delegator read it off the pump."""
        from core.events import turn_ending
        saved: list = []
        sp, eq, pump = self._mk_pump("chat-z", "sess-z", saved, monkeypatch)
        frames: list = []

        async def forward(item):
            frames.append(item)

        monkeypatch.setattr(pump, "_forward", forward)
        ending = turn_ending.TurnEnding(reason=turn_ending.LIMIT,
                                        resets_at="2026-10-01T15:00:00+00:00")
        await eq.put(sp.CommonEvent(type=sp.ERROR, data={
            "message": ending.line(), "ending": ending.as_dict()}))
        await pump._run()
        [block] = self._ended_rows(saved)
        assert block["reason"] == "limit" and block["message"] == ending.line()
        err = [f for f in frames if f.get("pump_type") == sp.wire.PUMP_ERROR]
        assert err == [{"pump_type": sp.wire.PUMP_ERROR, "message": ending.line(),
                        "reason": "limit", "resets_at": "2026-10-01T15:00:00+00:00"}]
        assert pump.last_ending == ending


class TestWaitSessionReconnect:
    """A turn start waits for a session whose machine is in its reconnect
    grace instead of reading it as dead (the dead path would drop the live
    session's record and state); a session not held answers at once."""

    @staticmethod
    def _held(*, cli_dead: bool = False):
        from core.remote import remote_execution as re_mod
        from core.remote.satellite_connection import SatelliteConnectionManager
        cm = SatelliteConnectionManager()
        layer = re_mod.RemoteExecutionLayer(cm)
        info = re_mod.RemoteSessionInfo(
            session_id="s", machine_id="m", agent_name="agent-x",
            execution_path="claude-code-cli", event_queue=asyncio.Queue())
        info.cli_dead = cli_dead
        layer._sessions["s"] = info
        cm._connections["m"] = _fake_conn({"s": info.event_queue})
        return cm, layer

    @staticmethod
    def _reconnect_after(cm, delay: float):
        async def _back():
            await asyncio.sleep(delay)
            # What register() does for the held sessions.
            held = cm._grace_sessions.pop("m")
            cm._grace_timers.pop("m").cancel()
            cm._connections["m"] = _fake_conn({sid: q for sid, (q, _p) in held.items()})
        return asyncio.ensure_future(_back())

    @pytest.mark.asyncio
    async def test_a_machine_back_inside_the_wait_leaves_the_session_alive(self):
        cm, layer = self._held()
        await cm.deregister("m")
        assert not await layer.is_session_alive("s")
        self._reconnect_after(cm, 0.3)

        assert await layer.wait_session_reconnect("s", timeout=5) is True
        assert await layer.is_session_alive("s")

    @pytest.mark.asyncio
    async def test_a_machine_still_away_at_the_timeout_reads_dead(self):
        cm, layer = self._held()
        await cm.deregister("m")
        loop = asyncio.get_running_loop()
        t0 = loop.time()

        assert await layer.wait_session_reconnect("s", timeout=0.4) is False
        assert 0.35 <= loop.time() - t0 < 2
        cm._grace_timers["m"].cancel()

    @pytest.mark.asyncio
    async def test_a_session_not_held_answers_at_once(self):
        from core.remote import remote_execution as re_mod
        from core.remote.satellite_connection import SatelliteConnectionManager
        layer = re_mod.RemoteExecutionLayer(SatelliteConnectionManager())
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        # No record at all, and a machine gone past its grace.
        assert await layer.wait_session_reconnect("nobody", timeout=5) is False
        layer._sessions["s"] = re_mod.RemoteSessionInfo(
            session_id="s", machine_id="m", agent_name="agent-x",
            execution_path="claude-code-cli", event_queue=asyncio.Queue())
        assert await layer.wait_session_reconnect("s", timeout=5) is False
        assert loop.time() - t0 < 0.2

    @pytest.mark.asyncio
    async def test_a_process_that_died_stays_dead_after_the_reconnect(self):
        cm, layer = self._held(cli_dead=True)
        await cm.deregister("m")
        self._reconnect_after(cm, 0.1)

        assert await layer.wait_session_reconnect("s", timeout=5) is False

    @pytest.mark.asyncio
    async def test_a_local_layer_never_waits(self):
        from core.layers.cli.layer import CLIExecutionLayer
        assert await CLIExecutionLayer().wait_session_reconnect("s") is False


def test_the_session_records_on_one_machine():
    from core.remote import remote_execution as re_mod
    from core.remote.satellite_connection import SatelliteConnectionManager
    layer = re_mod.RemoteExecutionLayer(SatelliteConnectionManager())
    for sid, machine in (("a", "m-1"), ("b", "m-2"), ("c", "m-1")):
        layer._sessions[sid] = re_mod.RemoteSessionInfo(
            session_id=sid, machine_id=machine, agent_name="agent-x",
            execution_path="claude-code-cli", event_queue=asyncio.Queue())
    assert layer.session_ids_on("m-1") == ["a", "c"]
    assert layer.session_ids_on("m-3") == []

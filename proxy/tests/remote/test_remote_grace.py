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
    """A grace-expiry ERROR carrying `durable_marker` is
    PERSISTED by the pump (as a visible assistant block), not just forwarded
    live — so a refresh after a genuinely-lost turn shows the ⚠ instead of a
    silent truncation. The pump's `_run` BREAKS on ERROR, so the marker must be
    saved on that path (it relies on the `finally`'s _save_turn_blocks)."""

    @staticmethod
    def _mk_pump(chat_id, session_id, saved, monkeypatch):
        import asyncio as _aio
        from core.events import stream_pump as sp
        monkeypatch.setattr(sp.task_store, "add_chat_message",
                            lambda *a, **k: saved.append((a, k)))
        monkeypatch.setattr(sp.task_store, "get_last_chat_message_id",
                            lambda cid: len(saved))
        eq: _aio.Queue = _aio.Queue()

        async def _idle_producer():
            await _aio.sleep(3600)

        prod = _aio.create_task(_idle_producer())
        return sp, eq, sp.ChatStreamPump(chat_id, session_id, prod, eq, None)

    @pytest.mark.asyncio
    async def test_durable_marker_error_is_persisted(self, monkeypatch):
        saved: list = []
        sp, eq, pump = self._mk_pump("chat-x", "sess-x", saved, monkeypatch)
        marker = "⚠ stream interrupted — output may be incomplete"
        await eq.put(sp.CommonEvent(type=sp.ERROR,
                                    data={"message": marker, "durable_marker": True}))
        await pump._run()
        assistant = [a for a, k in saved if len(a) >= 3 and a[1] == "assistant"]
        assert any(marker in a[2] for a in assistant), saved

    @pytest.mark.asyncio
    async def test_plain_error_not_persisted(self, monkeypatch):
        """An ordinary transient ERROR (no durable_marker) is forwarded live
        only — NOT persisted as an assistant block (today's behaviour)."""
        saved: list = []
        sp, eq, pump = self._mk_pump("chat-y", "sess-y", saved, monkeypatch)
        await eq.put(sp.CommonEvent(type=sp.ERROR,
                                    data={"message": "Remote session timeout"}))
        await pump._run()
        assistant = [a for a, k in saved if len(a) >= 3 and a[1] == "assistant"]
        assert assistant == [], saved

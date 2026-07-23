"""Notify-drain staleness guard for ``chat_status`` forwards.

The per-user notify queue is only drained BETWEEN the viewed chat's turns, so
a turn-START ``streaming`` broadcast can sit queued for the whole turn and
land right after ``done`` — where the frontend's auto-attach reads it as a
fresh turn and pointlessly re-resumes the chat (a full-history reload flash
at every turn end). The drain must forward a ``streaming`` status only while
the chat still has a live turn (an active pump, or an interactive session
with an open turn); ``ready`` always forwards.
"""

import asyncio

import pytest

from core.events.stream_pump import ChatStreamPump, _active_pumps
from ws.dashboard_server_events import ServerNotificationController


class _Conn(ServerNotificationController):
    """Bare mixin host — the chat_status branch only touches ``self._send``."""

    def __init__(self):
        self.sent: list[dict] = []

    async def _send(self, data: dict):
        self.sent.append(data)


def _mk_pump(chat_id: str) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    return ChatStreamPump(
        chat_id=chat_id,
        session_id=f"sess-{chat_id}",
        producer=producer,
        event_queue=asyncio.Queue(),
        perm_queue=None,
    )


def _status(chat_id: str, status: str) -> dict:
    return {"type": "chat_status", "chat_id": chat_id, "status": status}


@pytest.mark.asyncio
async def test_stale_streaming_status_is_dropped(temp_db):
    conn = _Conn()
    await conn._handle_server_notification(_status("sc-none", "streaming"))
    assert conn.sent == []  # no pump, no interactive turn — stale, dropped


@pytest.mark.asyncio
async def test_live_pump_streaming_status_forwards(temp_db):
    temp_db.create_chat("sc-live", "user-admin", "a1")
    pump = _mk_pump("sc-live")
    _active_pumps["sc-live"] = pump
    try:
        conn = _Conn()
        await conn._handle_server_notification(_status("sc-live", "streaming"))
        assert conn.sent == [_status("sc-live", "streaming")]
    finally:
        _active_pumps.pop("sc-live", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_done_pump_streaming_status_is_dropped(temp_db):
    temp_db.create_chat("sc-done", "user-admin", "a1")
    pump = _mk_pump("sc-done")
    pump._done = True
    _active_pumps["sc-done"] = pump
    try:
        conn = _Conn()
        await conn._handle_server_notification(_status("sc-done", "streaming"))
        assert conn.sent == []
    finally:
        _active_pumps.pop("sc-done", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_interactive_open_turn_streaming_forwards(temp_db, monkeypatch):
    class _Isess:
        turn_open = True

    from core.session import interactive_session

    monkeypatch.setattr(
        interactive_session, "find_live_for_chat", lambda cid: _Isess()
    )
    conn = _Conn()
    await conn._handle_server_notification(_status("sc-pty", "streaming"))
    assert conn.sent == [_status("sc-pty", "streaming")]


@pytest.mark.asyncio
async def test_ready_status_always_forwards(temp_db):
    conn = _Conn()
    await conn._handle_server_notification(_status("sc-ready", "ready"))
    assert conn.sent == [_status("sc-ready", "ready")]

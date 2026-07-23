"""Mid-turn pump attach races.

Switching into a chat whose turn is ending raced the pump teardown ("the
story ended mid-sentence until I refreshed"). These tests lock the
pump-level invariants the fixes rely on:

- attach() is FAN-OUT: a second viewer (another device/tab, a teammate on a
  shared chat) gets its own queue and every frame — it never displaces the
  first viewer (the old single-slot steal made concurrent viewers' 3s
  _task_pump_poll fight over the stream, wholesale-reloading the chat every
  few seconds mid-generation).
- detach() removes only the CALLER's queue: a stale holder's close path can
  never stop another viewer's frames.
- DONE (turn boundary) KEEPS live_blocks accumulating: a consumer attaching
  between the turn's final save and the producer's exit still reconstructs
  the whole turn from live_state.
- DONE advances _db_msg_cutoff_id past the just-saved rows, so a resume's
  id-based truncation (keep messages with id <= cutoff_id) cannot hide a
  persisted turn.

The connection-level halves (promised-pump history re-send in
_enter_pump_loop; pump_ended always sending `done`) live inside the nested
ws_dashboard_handler closure — covered by live verification, not unit tests.
"""

import asyncio

import pytest

from core.events import stream_pump
from core.events.common_events import CommonEvent, DONE, TEXT
from core.events.stream_pump import ChatStreamPump


def _mk_pump(chat_id: str) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    return ChatStreamPump(
        chat_id=chat_id,
        session_id=f"sess-{chat_id}",
        producer=producer,
        event_queue=asyncio.Queue(),
        perm_queue=None,
    )


def _seed_live(chat_id: str, session_id: str) -> dict:
    # Mirrors the dict the pump builds at _run entry (stream_pump.py).
    live = {
        "streaming": True,
        "session_id": session_id,
        "started_at": 0.0,
        "live_blocks": [],
        "active_tools": [],
        "active_agents": [],
        "active_delegates": [],
        "pending_permission": None,
        "thinking_active": False,
        "thinking_text": "",
        "thinking_tokens": 0,
        "todos": [],
        "goal": None,
        "meeting_agent": None,
        "meeting_participants": [],
        "workflows": {},
    }
    stream_pump._chat_streaming_state[chat_id] = live
    return live


def _drain(q: asyncio.Queue) -> list:
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break
    return items


@pytest.mark.asyncio
async def test_attach_fans_out_to_all_viewers(temp_db):
    temp_db.create_chat("pc1", "user-admin", "a1")
    pump = _mk_pump("pc1")
    try:
        q1 = pump.attach()
        q2 = pump.attach()  # a second viewer joins — q1 keeps streaming
        assert _drain(q1) == []  # no steal sentinel

        await pump._forward({"pump_type": "ws_event", "event": {"type": "text"}})
        assert len(_drain(q1)) == 1  # both viewers get every frame
        assert len(_drain(q2)) == 1

        pump.detach(q1)  # one viewer leaves — the other keeps streaming
        await pump._forward({"pump_type": "ws_event", "event": {"type": "text"}})
        assert _drain(q1) == []
        assert len(_drain(q2)) == 1

        pump.detach(q1)  # double-detach of a gone queue is a no-op
        pump.detach(q2)
        await pump._forward({"pump_type": "ws_event", "event": {"type": "text"}})
        assert _drain(q2) == []
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_pump_ended_reaches_all_viewers(temp_db):
    temp_db.create_chat("pc1b", "user-admin", "a1")
    pump = _mk_pump("pc1b")
    try:
        q1 = pump.attach()
        q2 = pump.attach()
        for q in list(pump._ws_queues):
            q.put_nowait({"pump_type": "pump_ended"})  # the _run finally
        assert [i["pump_type"] for i in _drain(q1)] == ["pump_ended"]
        assert [i["pump_type"] for i in _drain(q2)] == ["pump_ended"]
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_push_ws_event_fans_out(temp_db):
    temp_db.create_chat("pc1c", "user-admin", "a1")
    pump = _mk_pump("pc1c")
    try:
        assert pump.push_ws_event({"type": "text"}) is False  # nobody attached
        q1 = pump.attach()
        q2 = pump.attach()
        assert pump.push_ws_event({"type": "text"}) is True
        assert [i["event"]["type"] for i in _drain(q1)] == ["text"]
        assert [i["event"]["type"] for i in _drain(q2)] == ["text"]
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_detach_own_queue_clears(temp_db):
    temp_db.create_chat("pc2", "user-admin", "a1")
    pump = _mk_pump("pc2")
    try:
        q = pump.attach()
        pump.detach(q)
        await pump._forward({"pump_type": "ws_event", "event": {"type": "text"}})
        assert _drain(q) == []
    finally:
        pump.producer.cancel()


def test_external_driven_sources_policy():
    """Dashboard view-only policy: opening a chat whose pump is
    driven OUT-OF-BAND (the phone pipeline plays its stream as TTS) must NOT
    attach — the dashboard must never inject itself into a live call's
    delivery path (and pre-fan-out, attach() stole the stream outright). Only externally-driven sources are view-only; 'task'/'meeting'
    pumps MUST stay attachable (the dashboard is their live viewer).
    """
    from ws.dashboard import _EXTERNAL_DRIVEN_SOURCES

    assert "phone" in _EXTERNAL_DRIVEN_SOURCES
    # Regression guard — adding any of these would silently break the dashboard's
    # live streaming of that source; removing 'phone' reintroduces the call-kill.
    assert "chat" not in _EXTERNAL_DRIVEN_SOURCES
    assert "task" not in _EXTERNAL_DRIVEN_SOURCES
    assert "meeting" not in _EXTERNAL_DRIVEN_SOURCES


@pytest.mark.asyncio
async def test_phone_pump_is_view_only_default_is_attachable(temp_db):
    """The guard keys on pump.source_type — a phone pump reports 'phone' (→ the
    dashboard views it read-only) while a default pump is 'chat' (→ attachable)."""
    from ws.dashboard import _EXTERNAL_DRIVEN_SOURCES

    temp_db.create_chat("pc-phone", "phone", "a1")
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    try:
        phone_pump = ChatStreamPump(
            chat_id="pc-phone", session_id="s-phone", producer=producer,
            event_queue=asyncio.Queue(), perm_queue=None,
            scope="agent", source_type="phone",
        )
        assert phone_pump.source_type in _EXTERNAL_DRIVEN_SOURCES

        default_pump = _mk_pump("pc-chat")
        assert default_pump.source_type == "chat"
        assert default_pump.source_type not in _EXTERNAL_DRIVEN_SOURCES
        default_pump.producer.cancel()
    finally:
        producer.cancel()


@pytest.mark.asyncio
async def test_done_keeps_live_blocks_and_advances_cutoff(temp_db):
    temp_db.create_chat("pc3", "user-admin", "a1")
    pump = _mk_pump("pc3")
    live = _seed_live("pc3", pump.session_id)
    try:
        q = pump.attach()
        await pump._process_event(CommonEvent(TEXT, {"content": "hello world"}))
        assert any(b.get("type") == "text" for b in live["live_blocks"])
        cutoff_before = pump._db_msg_cutoff_id

        await pump._process_event(CommonEvent(DONE, {}))

        # live_blocks survive the turn boundary — a mid-turn attach between
        # the final save and the producer's exit reconstructs the WHOLE turn
        # from live_state (DB rows past the cutoff are withheld meanwhile).
        assert any(b.get("type") == "text" for b in live["live_blocks"])
        # The saved row's id is now INSIDE the cutoff → a resume's id-based
        # truncation keeps it; the turn cannot be hidden from chat_history.
        assert pump._db_msg_cutoff_id > cutoff_before
        assert pump._db_msg_cutoff_id == temp_db.get_last_chat_message_id("pc3")
        assert temp_db.get_chat_message_count("pc3") == 1  # one row saved this turn
        # The attached consumer saw the boundary marker (and the text frame).
        types = [i.get("pump_type") for i in _drain(q)]
        assert "is_done" in types
    finally:
        stream_pump._chat_streaming_state.pop("pc3", None)
        pump.producer.cancel()

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
    from core.session.session_kind import EXTERNAL_DRIVEN_SOURCE_TYPES as _EXTERNAL_DRIVEN_SOURCES

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
    from core.session.session_kind import EXTERNAL_DRIVEN_SOURCE_TYPES as _EXTERNAL_DRIVEN_SOURCES

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
        cutoff_before = pump._db_msg_cutoff_id or 0

        await pump._process_event(CommonEvent(DONE, {}))

        # live_blocks survive the turn boundary — a mid-turn attach between
        # the final save and the producer's exit reconstructs the WHOLE turn
        # from live_state (DB rows past the cutoff are withheld meanwhile).
        assert any(b.get("type") == "text" for b in live["live_blocks"])
        # The save is an off-loop writer job: its rows, the cutoff advance
        # and the live_blocks trim land together when it does.
        from core.events import chat_writer
        assert await chat_writer.drain("pc3", timeout=5)
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


# ---------------------------------------------------------------------------
# Coalesced deltas, bounded dashboard queues
# ---------------------------------------------------------------------------

from core.events.common_events import PRODUCER_DONE, THINKING, TOOL_USE  # noqa: E402


def _events_of(items: list) -> list[dict]:
    return [i["event"] for i in items if i.get("pump_type") == "ws_event"]


@pytest.mark.asyncio
async def test_a_burst_of_text_deltas_arrives_as_fewer_frames_in_order(temp_db):
    pump = _mk_pump("co1")
    _seed_live("co1", pump.session_id)
    q = pump.attach()
    try:
        for i in range(20):
            await pump._process_event(CommonEvent(type=TEXT, data={"content": f"{i},"}))
        first = _drain(q)
        assert _events_of(first) == [{"type": "text", "content": "0,"}]  # no added latency
        await asyncio.sleep(stream_pump._DELTA_FLUSH_S * 2)
        rest = _events_of(_drain(q))
        assert len(rest) == 1
        assert rest[0]["content"] == "".join(f"{i}," for i in range(1, 20))
        live = stream_pump._chat_streaming_state["co1"]
        assert live["live_blocks"][-1]["content"] == "".join(f"{i}," for i in range(20))
    finally:
        stream_pump._chat_streaming_state.pop("co1", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_any_other_frame_flushes_the_pending_text_first(temp_db):
    pump = _mk_pump("co2")
    _seed_live("co2", pump.session_id)
    q = pump.attach()
    try:
        await pump._process_event(CommonEvent(type=TEXT, data={"content": "a"}))
        await pump._process_event(CommonEvent(type=TEXT, data={"content": "b"}))
        await pump._process_event(CommonEvent(type=TOOL_USE, data={"name": "Bash", "tool_id": "t1"}))
        pump.push_ws_event({"type": "system", "subtype": "note"})
        types = [(e["type"], e.get("content")) for e in _events_of(_drain(q))]
        assert types == [("text", "a"), ("text", "b"), ("tool_start", None), ("system", None)]
    finally:
        stream_pump._chat_streaming_state.pop("co2", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_thinking_deltas_merge_and_progress_keeps_the_latest(temp_db):
    pump = _mk_pump("co3")
    _seed_live("co3", pump.session_id)
    q = pump.attach()
    try:
        for data in ({"phase": "start"},
                     {"phase": "delta", "text": "x"}, {"phase": "delta", "text": "y"},
                     {"phase": "delta", "text": "z"},
                     {"phase": "progress", "estimated_tokens": 10},
                     {"phase": "progress", "estimated_tokens": 30},
                     {"phase": "end", "text": ""}):
            await pump._process_event(CommonEvent(type=THINKING, data=data))
        frames = _events_of(_drain(q))
        assert frames == [
            {"type": "thinking", "phase": "start"},
            {"type": "thinking", "phase": "delta", "text": "x"},
            {"type": "thinking", "phase": "delta", "text": "yz"},
            {"type": "thinking", "phase": "progress", "estimated_tokens": 30},
            {"type": "thinking", "phase": "end", "text": ""},
        ]
    finally:
        stream_pump._chat_streaming_state.pop("co3", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_attach_mid_burst_gives_no_duplicate_and_no_gap(temp_db):
    """The pending delta goes to the existing viewers BEFORE the new queue
    joins: the newcomer's live_state snapshot already holds it."""
    pump = _mk_pump("co4")
    live = _seed_live("co4", pump.session_id)
    qa = pump.attach()
    try:
        for c in "abc":
            await pump._process_event(CommonEvent(type=TEXT, data={"content": c}))
        qb = pump.attach(bounded=True)
        snapshot = live["live_blocks"][-1]["content"]
        await pump._process_event(CommonEvent(type=TEXT, data={"content": "d"}))
        await asyncio.sleep(stream_pump._DELTA_FLUSH_S * 2)
        a_text = "".join(e["content"] for e in _events_of(_drain(qa)))
        b_text = "".join(e["content"] for e in _events_of(_drain(qb)))
        assert a_text == "abcd"
        assert snapshot == "abc" and snapshot + b_text == "abcd"
    finally:
        stream_pump._chat_streaming_state.pop("co4", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_bounded_queue_past_its_cap_gets_one_resync_and_nothing_after(temp_db, monkeypatch):
    monkeypatch.setattr(stream_pump, "_VIEWER_QUEUE_MAX", 5)
    pump = _mk_pump("co5")
    _seed_live("co5", pump.session_id)
    slow = pump.attach(bounded=True)
    other = pump.attach()
    try:
        for i in range(12):
            pump.push_ws_event({"type": "system", "subtype": f"n{i}"})
        assert _drain(slow) == [{"pump_type": stream_pump.PUMP_RESYNC}]
        pump.push_ws_event({"type": "system", "subtype": "late"})
        assert _drain(slow) == []
        assert len(_drain(other)) == 13
    finally:
        stream_pump._chat_streaming_state.pop("co5", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_pending_text_goes_before_all_done_and_a_late_attach_gets_ended(temp_db):
    temp_db.create_chat("co6", "user-admin", "a1")
    eq: asyncio.Queue = asyncio.Queue()
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    pump = ChatStreamPump(chat_id="co6", session_id="sess-co6", producer=producer,
                          event_queue=eq, perm_queue=None)
    stream_pump._active_pumps["co6"] = pump
    q = pump.attach(bounded=True)
    for c in "abc":
        await eq.put(CommonEvent(type=TEXT, data={"content": c}))
    await eq.put(CommonEvent(type=PRODUCER_DONE, data={}))
    try:
        await pump._run()
        items = _drain(q)
        kinds = [i["pump_type"] for i in items]
        text = "".join(e["content"] for e in _events_of(items))
        assert text == "abc"
        assert kinds.index("all_done") > max(
            n for n, i in enumerate(items) if i["pump_type"] == "ws_event")
        assert kinds[-1] == "pump_ended"
        late = pump.attach(bounded=True)
        assert _drain(late) == [{"pump_type": "pump_ended"}]
    finally:
        stream_pump._active_pumps.pop("co6", None)
        stream_pump._chat_streaming_state.pop("co6", None)

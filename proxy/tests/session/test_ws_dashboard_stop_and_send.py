"""End-to-end stop-and-send (R7.2) through the REAL dashboard WS handler.

Drives ``ws_dashboard_handler`` + the real pump/producer machinery with a
scripted layer: a typed ``chat`` frame lands mid-turn, the handler queues it
and fires the graceful-only interrupt, the "CLI" closes the turn (the fake's
``on_interrupt_for_queued`` releases the turn generator's gate), and the
producer's queue drain delivers the message as the next turn — note on the
ENGINE prompt only, DB user rows raw. The refusal path (no live turn /
unsupported engine) degrades to today's deliver-at-turn-end with no note.

This is the live-verification twin of the browser test: everything except
the real CLI binary (whose control_request{interrupt} behavior the Stop
button and phone barge-in exercise daily) runs exactly as deployed.
"""

import asyncio
import json

import pytest  # noqa: F401  (temp_db / monkeypatch fixtures)

from core.events.common_events import CommonEvent, TEXT, DONE
from storage import database as task_store
from tests.fixtures.ws_dashboard_harness import (
    ANY,
    FakeExecutionLayer,
    dashboard_connection,
    drain_startup,
    make_test_agent,
    run_ws_scenario,
    session_cookie,
    set_username,
    stub_dashboard_seams,
    warm_new_chat,
)

import ws.dashboard  # noqa: F401  (resolves the dashboard↔dashboard_chat cycle)
from ws.dashboard_chat import _STOP_AND_SEND_NOTE


def _gated_turns(gate: asyncio.Event, calls: dict):
    """Turn 1 streams t1 then parks on ``gate`` (a long-running turn);
    later turns finish immediately."""

    def turn(sid, prompt):
        calls["n"] += 1
        first = calls["n"] == 1

        async def gen():
            yield CommonEvent(type=TEXT, data={"content": f"t{calls['n']}"})
            if first:
                await gate.wait()
            yield CommonEvent(type=DONE, data={})
        return gen()

    return turn


def _drain_prelude(ws, chat_id, sid, first_text):
    """The frames every turn-opening chat send produces before its text."""

    async def inner():
        await ws.expect({"type": "title_updated", "chat_id": chat_id,
                         "title": first_text})
        await ws.expect({"type": "chat_status", "chat_id": chat_id,
                         "status": "streaming"})
        await ws.expect({"type": "live_state", "chat_id": chat_id,
                         "streaming": True, "session_id": sid,
                         "started_at": ANY, "live_blocks": [],
                         "active_tools": [], "active_agents": [],
                         "active_delegates": [], "active_commands": [],
                         "pending_permission": None,
                         "thinking_active": False, "thinking_text": "",
                         "thinking_tokens": 0, "todos": [],
                         "goal": None, "meeting_agent": None,
                         "meeting_participants": [], "workflows": {}})
    return inner()


class TestStopAndSend:
    def test_typed_mid_turn_interrupts_and_delivers_with_note(
        self, temp_db, monkeypatch,
    ):
        gate = asyncio.Event()
        calls = {"n": 0}
        layer = FakeExecutionLayer()
        layer.turn_events = _gated_turns(gate, calls)
        # The "CLI" accepts the graceful interrupt and closes the turn.
        layer.interrupt_for_queued_accepts = True
        layer.on_interrupt_for_queued = lambda sid: gate.set()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                ws.client_send({"type": "chat", "text": "start",
                                "chat_id": chat_id})
                await _drain_prelude(ws, chat_id, sid, "start")
                await ws.expect({"type": "text", "content": "t1",
                                 "chat_id": chat_id})

                # Mid-turn typed message: queued + graceful interrupt fired
                # → the turn closes → the drain delivers it immediately.
                ws.client_send({"type": "chat", "text": "do this instead",
                                "chat_id": chat_id, "queue_id": "q-1"})
                await ws.expect({"type": "queued", "index": 0, "queue_id": "q-1",
                                 "text": "do this instead", "author_sub": "user-admin",
                                 "chat_id": chat_id})
                await ws.expect({"type": "queue_sent", "queue_ids": ["q-1"],
                                 "message_ids": ANY, "text": "do this instead",
                                 "chat_id": chat_id})
                await ws.expect({"type": "text", "content": "t2",
                                 "chat_id": chat_id})
                await ws.expect({"type": "done", "chat_id": chat_id})

                # The interrupt targeted the streaming session, exactly once.
                assert layer.queued_interrupts == [sid]
                # Steer-first ordering intact: the attempt was made and
                # rejected (real CLI keeps the unsupported default), so the
                # message took the queue+interrupt path, never both.
                assert layer.steered == [(sid, "do this instead")]

                # Engine prompts: turn 1 raw; the drained turn carries the
                # interruption note ON THE PROMPT ONLY.
                assert len(layer.messages) == 2
                assert layer.messages[0][1] == "start"
                assert layer.messages[1][1] == (
                    _STOP_AND_SEND_NOTE + "\n\ndo this instead"
                )

                # DB user rows stay raw — the note never persists.
                msgs = task_store.get_chat_messages(chat_id)
                users = [m["content"] for m in msgs if m["role"] == "user"]
                assert users == ["start", "do this instead"]
                assert all(_STOP_AND_SEND_NOTE not in u for u in users)
        run_ws_scenario(scenario)

    def test_engine_refusal_degrades_to_deliver_at_turn_end(
        self, temp_db, monkeypatch,
    ):
        """No live turn / unsupported engine → interrupt refused: the message
        waits for the natural turn end and drains WITHOUT the note."""
        gate = asyncio.Event()
        calls = {"n": 0}
        layer = FakeExecutionLayer()
        layer.turn_events = _gated_turns(gate, calls)
        layer.interrupt_for_queued_accepts = False  # refusal (default engine)
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                ws.client_send({"type": "chat", "text": "start",
                                "chat_id": chat_id})
                await _drain_prelude(ws, chat_id, sid, "start")
                await ws.expect({"type": "text", "content": "t1",
                                 "chat_id": chat_id})

                ws.client_send({"type": "chat", "text": "later please",
                                "chat_id": chat_id, "queue_id": "q-1"})
                await ws.expect({"type": "queued", "index": 0, "queue_id": "q-1",
                                 "text": "later please", "author_sub": "user-admin",
                                 "chat_id": chat_id})
                # Give the fire task time to run + be refused.
                await asyncio.sleep(0.05)
                assert layer.queued_interrupts == [sid]

                # The turn keeps running until ITS OWN end.
                gate.set()
                await ws.expect({"type": "queue_sent", "queue_ids": ["q-1"],
                                 "message_ids": ANY, "text": "later please",
                                 "chat_id": chat_id})
                await ws.expect({"type": "text", "content": "t2",
                                 "chat_id": chat_id})
                await ws.expect({"type": "done", "chat_id": chat_id})

                # No note on a turn that was never interrupted.
                assert layer.messages[1][1] == "later please"
        run_ws_scenario(scenario)


# ---------------------------------------------------------------------------
# The viewer loop is event-driven (no fixed receive wait per frame)
# ---------------------------------------------------------------------------

import collections  # noqa: E402
import time  # noqa: E402

from core.events import stream_pump  # noqa: E402
from core.events.common_events import PRODUCER_DONE  # noqa: E402
from tests.fixtures.ws_dashboard_harness import FakeDashboardWebSocket  # noqa: E402


class _TimedWebSocket(FakeDashboardWebSocket):
    def __init__(self):
        super().__init__(cookie=None)
        self.sent_at: list[tuple[float, dict]] = []

    async def send_json(self, data: dict) -> None:
        self.sent_at.append((time.monotonic(), data))
        await super().send_json(data)


def _viewer(ws, chat_id: str, sid: str):
    from services.notifications.notification_manager import LiveQueue
    from ws.dashboard import DashboardConnection
    conn = DashboardConnection.__new__(DashboardConnection)
    conn.websocket = ws
    conn.chat_id = chat_id
    conn.session_id = sid
    conn.streaming = False
    conn.live_queue = LiveQueue()
    conn._send_lock = asyncio.Lock()
    conn._client_pushback = collections.deque()
    conn._ended = asyncio.Event()
    conn._recheck_wake = asyncio.Event()
    conn.pending_control_requests = []
    # The session was checked just now: the throttled revalidation is not due.
    conn._last_authz_check = time.time()
    return conn


def _pump(chat_id: str, events, *, rate_hz: float = 0.0):
    eq: asyncio.Queue = asyncio.Queue()

    async def _produce():
        for ev in events:
            await eq.put(ev)
            await asyncio.sleep(1 / rate_hz if rate_hz else 0)
        await eq.put(CommonEvent(type=PRODUCER_DONE, data={}))
        await asyncio.sleep(3600)

    producer = asyncio.get_event_loop().create_task(_produce())
    pump = stream_pump.ChatStreamPump(chat_id=chat_id, session_id=f"sess-{chat_id}",
                                      producer=producer, event_queue=eq, perm_queue=None)
    stream_pump._active_pumps[chat_id] = pump
    return pump


@pytest.mark.asyncio
async def test_a_fast_stream_reaches_a_silent_viewer_in_time(temp_db):
    """200 deltas at 100/s: every character arrives while the model streams
    (a loop that sends at most ~20 frames/s lags seconds behind), and
    `done` follows the pump's all_done at once."""
    task_store.create_chat("vw1", "user-admin", "a1")
    words = [f"w{i} " for i in range(200)]
    pump = _pump("vw1", [CommonEvent(type=TEXT, data={"content": w}) for w in words],
                 rate_hz=100)
    all_done_at: list[float] = []
    real_forward = pump._forward

    async def _stamp(item):
        if item.get("pump_type") == "all_done":
            all_done_at.append(time.monotonic())
        await real_forward(item)
    pump._forward = _stamp
    ws = _TimedWebSocket()
    conn = _viewer(ws, "vw1", pump.session_id)
    try:
        started = time.monotonic()
        pump.start()
        result = await asyncio.wait_for(conn._stream_via_pump(pump), timeout=10)
        assert result["detached"] is False
        text = "".join(d.get("content", "") for _t, d in ws.sent_at if d.get("type") == "text")
        assert text == "".join(words)
        done_at = next(t for t, d in ws.sent_at if d.get("type") == "done")
        assert done_at - all_done_at[0] < 0.1
        assert done_at - started < 5.0
    finally:
        stream_pump._active_pumps.pop("vw1", None)
        stream_pump._chat_streaming_state.pop("vw1", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_client_message_is_read_while_a_backlog_is_pending(temp_db):
    pump = _pump("vw2", [])
    pump.producer.cancel()
    pump.producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    ws = _TimedWebSocket()
    conn = _viewer(ws, "vw2", pump.session_id)
    try:
        loop_task = asyncio.create_task(conn._stream_via_pump(pump))
        while not pump._ws_queues:
            await asyncio.sleep(0)
        for i in range(1000):
            pump.push_ws_event({"type": "system", "subtype": f"n{i}"})
        ws.client_send({"type": "resume_chat", "chat_id": "elsewhere"})
        result = await asyncio.wait_for(loop_task, timeout=5)
        assert result["detached"] is True
        assert result["resume_msg"]["type"] == "resume_chat"
        assert sum(1 for _t, d in ws.sent_at if d.get("type") == "system") < 1000
    finally:
        stream_pump._active_pumps.pop("vw2", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_pushed_documents_token_reaches_only_the_pushing_persons_sockets(temp_db):
    """One pump, two people viewing the chat: the live document_preview
    frame (and the live_state snapshot a mid-turn attach gets) carries the
    WOPI token to the connection of the person the turn runs as only."""
    from core.session.session_state import _chat_streaming_state
    doc = {"type": "document_preview", "wopi_url": "https://c/x", "access_token": "tok",
           "access_token_ttl": 123, "filename": "x.docx", "file_id": "fid",
           "download_url": "/v1/media/d"}
    pump = _pump("vw-doc", [])
    pump.producer.cancel()
    pump.producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    pump.wake_person = "user-a"
    _chat_streaming_state["vw-doc"] = {"streaming": True, "live_blocks": [dict(doc)]}
    viewers = {}
    for person in ("user-a", "user-b"):
        ws = _TimedWebSocket()
        conn = _viewer(ws, "vw-doc", pump.session_id)
        conn.user_sub = person
        viewers[person] = (ws, conn)
    try:
        loops = [asyncio.create_task(conn._stream_via_pump(pump)) for _ws, conn in viewers.values()]
        while len(pump._ws_queues) < 2:
            await asyncio.sleep(0)
        assert pump.push_ws_event(dict(doc))
        for ws, _conn in viewers.values():
            while not any(d.get("type") == "document_preview" for d in ws.sent):
                await asyncio.sleep(0)
            ws.client_send({"type": "resume_chat", "chat_id": "elsewhere"})
        await asyncio.wait_for(asyncio.gather(*loops), timeout=5)
        for person, (ws, _conn) in viewers.items():
            frames = ws.sent
            pushed = next(d for d in frames if d.get("type") == "document_preview")
            snapshot = next(d for d in frames if d.get("type") == "live_state")["live_blocks"][0]
            for frame in (pushed, snapshot):
                assert frame["file_id"] == "fid" and frame["wopi_url"] == "https://c/x"
                if person == "user-a":
                    assert (frame["access_token"], frame["access_token_ttl"]) == ("tok", 123)
                else:
                    assert "access_token" not in frame and "access_token_ttl" not in frame
        # The shared live state keeps the token for the pusher's next attach.
        assert _chat_streaming_state["vw-doc"]["live_blocks"][0]["access_token"] == "tok"
    finally:
        stream_pump._active_pumps.pop("vw-doc", None)
        _chat_streaming_state.pop("vw-doc", None)
        pump.producer.cancel()


def test_no_known_pusher_keeps_the_token_from_every_viewer():
    from core.events import artifact_events
    frame = {"type": "document_preview", "access_token": "tok", "access_token_ttl": 1}
    assert artifact_events.for_viewer(frame, "", "") == {"type": "document_preview"}
    assert artifact_events.for_viewer(frame, "user-a", "user-a") is frame
    plain = {"type": "text", "content": "hi"}
    assert artifact_events.for_viewer(plain, "", "user-b") is plain


@pytest.mark.asyncio
async def test_a_resync_marker_becomes_a_same_chat_resume(temp_db):
    pump = _pump("vw3", [])
    pump.producer.cancel()
    pump.producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    ws = _TimedWebSocket()
    conn = _viewer(ws, "vw3", pump.session_id)
    try:
        loop_task = asyncio.create_task(conn._stream_via_pump(pump))
        while not pump._ws_queues:
            await asyncio.sleep(0)
        pump._ws_queues[0].put_nowait({"pump_type": stream_pump.PUMP_RESYNC})
        result = await asyncio.wait_for(loop_task, timeout=5)
        assert result["detached"] is True
        assert result["resume_msg"] == {"type": "resume_chat", "chat_id": "vw3", "_resync": True}
        assert not pump._ws_queues          # detached
    finally:
        stream_pump._active_pumps.pop("vw3", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_viewer_attaching_to_an_ended_pump_gets_done(temp_db):
    pump = _pump("vw4", [])
    pump._ended = True
    ws = _TimedWebSocket()
    conn = _viewer(ws, "vw4", pump.session_id)
    try:
        result = await asyncio.wait_for(conn._stream_via_pump(pump), timeout=5)
        assert result["detached"] is True
        assert [d["type"] for _t, d in ws.sent_at][-1] == "done"
    finally:
        stream_pump._active_pumps.pop("vw4", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_non_object_client_message_gets_an_error_not_a_crash(temp_db):
    pump = _pump("vw5", [])
    pump.producer.cancel()
    pump.producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    ws = _TimedWebSocket()
    conn = _viewer(ws, "vw5", pump.session_id)
    try:
        loop_task = asyncio.create_task(conn._stream_via_pump(pump))
        ws.client_send_raw("[]")
        ws.client_send({"type": "resume_chat", "chat_id": "elsewhere"})
        result = await asyncio.wait_for(loop_task, timeout=5)
        assert result["resume_msg"]["chat_id"] == "elsewhere"
        assert {"type": "error", "message": "Invalid message"} in [d for _t, d in ws.sent_at]
    finally:
        stream_pump._active_pumps.pop("vw5", None)
        pump.producer.cancel()


class _ViaWebSocket(FakeDashboardWebSocket):
    """Records which send method carried each frame."""

    def __init__(self):
        super().__init__(cookie=None)
        self.via: list[tuple[str, str]] = []

    async def send_json(self, data: dict) -> None:
        self.via.append((data.get("type"), "json"))
        await super().send_json(data)

    async def send_text(self, text: str) -> None:
        self.via.append((json.loads(text).get("type"), "text"))
        await super().send_text(text)


@pytest.mark.asyncio
async def test_the_attach_snapshot_is_encoded_once_and_sent_as_text(temp_db, monkeypatch):
    """g13: the live_state of an attach is encoded to text once, at the
    snapshot (no await since the attach), and sent as that text: the
    string is the frozen copy, a later change to the live state is not in it."""
    from ws import dashboard_chat_stream as dcs
    pump = _pump("vw7", [])
    pump.producer.cancel()
    pump.producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    live = {"streaming": True, "session_id": pump.session_id,
            "live_blocks": [{"type": "text", "content": "so far"}]}
    stream_pump._chat_streaming_state["vw7"] = live
    decodes = []
    real_json = dcs.json

    class _CountingJson:
        dumps = staticmethod(real_json.dumps)
        JSONDecodeError = real_json.JSONDecodeError

        @staticmethod
        def loads(*args, **kwargs):
            decodes.append(args)
            return real_json.loads(*args, **kwargs)
    monkeypatch.setattr(dcs, "json", _CountingJson)
    ws = _ViaWebSocket()
    conn = _viewer(ws, "vw7", pump.session_id)
    try:
        loop_task = asyncio.create_task(conn._stream_via_pump(pump))
        while not any(t == "live_state" for t, _how in ws.via):
            await asyncio.sleep(0)
        live["live_blocks"].append({"type": "text", "content": "after the attach"})
        pump._ws_queues[0].put_nowait({"pump_type": "all_done"})
        await asyncio.wait_for(loop_task, timeout=5)
        assert ws.via == [("chat_status", "json"), ("live_state", "text"), ("done", "json")]
        assert decodes == []
        snapshot = next(f for f in ws.sent if f["type"] == "live_state")
        assert snapshot["live_blocks"] == [{"type": "text", "content": "so far"}]
        assert snapshot["chat_id"] == "vw7"
    finally:
        stream_pump._chat_streaming_state.pop("vw7", None)
        stream_pump._active_pumps.pop("vw7", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_received_message_left_unprocessed_is_handed_back(temp_db):
    """A client message that completed as a loop ended is never lost: it is
    handed back to the connection and read before the socket."""
    ws = _TimedWebSocket()
    conn = _viewer(ws, "vw6", "sess-vw6")

    async def _done():
        return '{"type": "chat_read", "chat_id": "vw6"}'

    recv = asyncio.create_task(_done())
    await asyncio.sleep(0)
    pending = asyncio.create_task(asyncio.sleep(3600))
    await conn._settle_stream_tasks(recv, pending, None)
    assert pending.cancelled()
    assert await conn._receive_client_text() == '{"type": "chat_read", "chat_id": "vw6"}'
    ws.client_send({"type": "ping"})
    assert await conn._receive_client_text() == '{"type": "ping"}'


@pytest.mark.asyncio
async def test_the_multiplex_keeps_a_result_that_lands_while_cancelling():
    from ws.dashboard import DashboardConnection
    gate = asyncio.Event()

    async def _slow_then_value():
        try:
            await gate.wait()
        except asyncio.CancelledError:
            return "landed"          # completes instead of cancelling
        return "never"

    first = asyncio.create_task(asyncio.sleep(0))
    late = asyncio.create_task(_slow_then_value())
    await asyncio.sleep(0.01)
    done = await DashboardConnection._collect_late_results({first}, {late})
    assert late in done and late.result() == "landed"

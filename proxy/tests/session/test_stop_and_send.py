"""Stop-and-send (R7.2): a message typed onto a busy Claude-CLI-headless
turn fires a graceful-ONLY interrupt at queue time, so the producer's queue
drain delivers it as the next turn within seconds instead of after the whole
turn. Pins the audited invariants:

- Ownership guard: only pumps built by ``_start_new_stream`` carry the
  ``stop_and_send`` flags dict; foreign producers (task runs, meetings,
  duplex, delegate-result echoes, recovery adopts) never drain the chat's
  queue, so the fire site must treat their ``None`` as
  queue-only — an interrupt there would destroy the turn AND strand the
  message.
- Pre-flight re-checks in the fire task: empty queue (producer claimed it /
  user cancelled) and superseded pump both skip the interrupt and un-latch
  the debounce; the interrupt targets the PUMP's session id, never the
  connection's.
- The interruption note rides the ENGINE prompt only — the QUEUE_TURN
  event / DB user row keep the raw combined text.
- A successful fire clears the pump's permission slot (the auto-deny
  released the waiters; without this the drain turn's next permission
  buffers invisibly and the turn hangs).
"""

from __future__ import annotations

import asyncio

import pytest

from core.events.stream_pump import (
    ChatStreamPump,
    _active_pumps,
    _pending_permissions,
)
from core.session.session_state import _chat_streaming_state

import ws.dashboard  # noqa: F401  (resolves the dashboard↔dashboard_chat cycle)
from ws.dashboard_chat import (
    _STOP_AND_SEND_NOTE,
    _queued_outgoing,
    ChatController,
)


class _FakeLayer:
    def __init__(self, accept: bool = True, raise_exc: bool = False):
        self.calls: list[str] = []
        self.accept = accept
        self.raise_exc = raise_exc

    async def interrupt_for_queued(self, session_id: str) -> bool:
        self.calls.append(session_id)
        if self.raise_exc:
            raise RuntimeError("boom")
        return self.accept


class _StubPump:
    """The attributes the fire site reads — mirrors a stream-owned pump. The
    typed message it fires for waits in the chat's queue."""

    def __init__(self, chat_id: str = "chat-1", session_id: str = "sess-1"):
        from core.events import input_queue
        from core.events.common_events import TurnInput
        self.chat_id = chat_id
        self.session_id = session_id
        self.message_queue: list = []
        self.stop_and_send: dict | None = {"fired": False, "note": False}
        self.perm_cleared = 0
        input_queue.get(chat_id).items[:] = [input_queue.QueuedInput(
            queue_id="q-1", chat_id=chat_id, author_sub="user-admin",
            item=TurnInput("queued text"))]

    def clear_permission_state(self):
        self.perm_cleared += 1


def _controller(layer) -> ChatController:
    ctl = ChatController.__new__(ChatController)
    ctl.layer = layer
    return ctl


@pytest.fixture(autouse=True)
def _clean_registry():
    from core.events import input_queue
    _active_pumps.clear()
    input_queue._registry.clear()
    yield
    _active_pumps.clear()
    input_queue._registry.clear()


async def _settle():
    # Let the create_task'd fire coroutine run to completion.
    for _ in range(4):
        await asyncio.sleep(0)


class TestFireSite:
    @pytest.mark.asyncio
    async def test_fires_once_on_owned_pump_and_clears_permissions(self):
        layer = _FakeLayer(accept=True)
        ctl = _controller(layer)
        pump = _StubPump()
        _active_pumps[pump.chat_id] = pump

        ctl._maybe_stop_and_send(pump)
        # Debounce: a second typed message during the same boundary must not
        # send a second interrupt frame.
        ctl._maybe_stop_and_send(pump)
        await _settle()

        assert layer.calls == [pump.session_id]  # pump's session, exactly once
        assert pump.stop_and_send == {"fired": True, "note": True}
        assert pump.perm_cleared == 1

    @pytest.mark.asyncio
    async def test_foreign_pump_is_queue_only(self):
        """stop_and_send=None (task/meeting/duplex/delegate-echo/recovery
        pumps) → never interrupt: nothing would drain the message."""
        layer = _FakeLayer()
        ctl = _controller(layer)
        pump = _StubPump()
        pump.stop_and_send = None
        _active_pumps[pump.chat_id] = pump

        ctl._maybe_stop_and_send(pump)
        await _settle()
        assert layer.calls == []

    @pytest.mark.asyncio
    async def test_preflight_empty_queue_skips_and_unlatches(self):
        layer = _FakeLayer()
        ctl = _controller(layer)
        pump = _StubPump()
        from core.events import input_queue
        input_queue.get(pump.chat_id).items.clear()  # the producer took it / a cancel
        _active_pumps[pump.chat_id] = pump

        ctl._maybe_stop_and_send(pump)
        await _settle()
        assert layer.calls == []
        assert pump.stop_and_send["fired"] is False  # a later message may fire

    @pytest.mark.asyncio
    async def test_preflight_superseded_pump_skips(self):
        layer = _FakeLayer()
        ctl = _controller(layer)
        pump = _StubPump()
        _active_pumps[pump.chat_id] = _StubPump(session_id="successor")

        ctl._maybe_stop_and_send(pump)
        await _settle()
        assert layer.calls == []
        assert pump.stop_and_send["fired"] is False

    @pytest.mark.asyncio
    async def test_layer_refusal_and_error_unlatch(self):
        # Refusal (no live turn): flags reset, no note, no permission clear.
        layer = _FakeLayer(accept=False)
        ctl = _controller(layer)
        pump = _StubPump()
        _active_pumps[pump.chat_id] = pump
        ctl._maybe_stop_and_send(pump)
        await _settle()
        assert layer.calls == [pump.session_id]
        assert pump.stop_and_send == {"fired": False, "note": False}
        assert pump.perm_cleared == 0

        # Exception: same un-latch, swallowed.
        layer2 = _FakeLayer(raise_exc=True)
        ctl2 = _controller(layer2)
        pump2 = _StubPump(chat_id="chat-2")
        _active_pumps[pump2.chat_id] = pump2
        ctl2._maybe_stop_and_send(pump2)
        await _settle()
        assert pump2.stop_and_send == {"fired": False, "note": False}

    @pytest.mark.asyncio
    async def test_no_layer_no_fire(self):
        ctl = _controller(None)
        pump = _StubPump()
        _active_pumps[pump.chat_id] = pump
        ctl._maybe_stop_and_send(pump)
        await _settle()
        assert pump.stop_and_send == {"fired": False, "note": False}


class TestQueuedOutgoing:
    def test_note_rides_engine_prompt_only_and_flags_reset(self):
        flags = {"fired": True, "note": True}
        combined = "do the other thing"
        out = _queued_outgoing(flags, combined)
        assert out == _STOP_AND_SEND_NOTE + "\n\n" + combined
        # The caller persisted `combined` raw via QUEUE_TURN before this.
        assert flags == {"fired": False, "note": False}

    def test_without_note_text_is_raw(self):
        flags = {"fired": True, "note": False}
        assert _queued_outgoing(flags, "hello") == "hello"
        assert flags == {"fired": False, "note": False}


class TestPumpState:
    def _mk_pump(self) -> ChatStreamPump:
        async def _noop():
            pass

        return ChatStreamPump(
            chat_id="chat-p",
            session_id="sess-p",
            producer=asyncio.get_event_loop().create_task(_noop()),
            event_queue=asyncio.Queue(),
            perm_queue=None,
        )

    @pytest.mark.asyncio
    async def test_default_pump_is_not_stream_owned(self):
        """F1 ownership guard: a bare pump (task/meeting/duplex builders use
        this constructor directly) must NOT carry stop-and-send flags."""
        pump = self._mk_pump()
        assert pump.stop_and_send is None

    @pytest.mark.asyncio
    async def test_clear_permission_state(self):
        pump = self._mk_pump()
        pump._permission_active = {"request_id": "r1"}
        pump._permission_buffer.append({"request_id": "r2"})
        _pending_permissions[pump.session_id] = {"request_id": "r1"}
        _chat_streaming_state[pump.chat_id] = {"pending_permission": {"r": 1}}
        try:
            pump.clear_permission_state()
            assert pump._permission_active is None
            assert pump._permission_buffer == []
            assert pump.session_id not in _pending_permissions
            assert _chat_streaming_state[pump.chat_id]["pending_permission"] is None
        finally:
            _pending_permissions.pop(pump.session_id, None)
            _chat_streaming_state.pop(pump.chat_id, None)

    @pytest.mark.asyncio
    async def test_a_bare_pump_refuses_typed_messages(self):
        """A pump whose producer never drains typed messages (task run,
        recovery, phone, delegate echo) starts with its queues closed."""
        from core.events.common_events import TurnInput
        pump = self._mk_pump()
        assert pump.queue_message(TurnInput("x")) == ChatStreamPump.QUEUE_CLOSED
        assert pump.queue_artifact({"token": "t"}) is False
        assert pump.message_queue == [] and pump.artifact_queue == []

    @pytest.mark.asyncio
    async def test_an_error_closes_the_queues_and_keeps_what_they_held(self, temp_db):
        """An error closes the pump's own queues: an utterance it never sent
        is kept in the chat as one undelivered card after the turn's rows,
        an interaction is left once for a viewer, and a system prompt is the
        chat's wake. A typed message never waits here (the chat's queue)."""
        import json
        from core.events import chat_writer
        from core.events.common_events import CommonEvent, ERROR, TurnInput
        from storage import database as task_store
        task_store.create_chat("chat-e", "user-admin", "a1")
        events: asyncio.Queue = asyncio.Queue()
        pump = ChatStreamPump(
            chat_id="chat-e", session_id="sess-e",
            producer=asyncio.get_event_loop().create_task(asyncio.sleep(3600)),
            event_queue=events, perm_queue=None,
        )
        # The queues as a draining producer shares them.
        pump.queue_closed = False
        pump.system_queue_consumer = True
        first = TurnInput("first")
        second = TurnInput("second", files=[{"path": "u/a/notes.txt", "name": "notes.txt"}])
        assert (pump.queue_message(first), pump.queue_message(second)) == (0, 1)
        art = {"token": "t1", "title": "", "payload": {}, "payload_json": "{}"}
        assert pump.queue_artifact(art) is True
        pump.system_queue.append("a nudge")

        _active_pumps["chat-e"] = pump
        pump.attach(bounded=True)
        await events.put(CommonEvent(type=ERROR, data={"message": "boom"}))
        pump.start()
        await asyncio.wait_for(pump._task, 5)

        assert pump.queue_message(TurnInput("late")) == ChatStreamPump.QUEUE_CLOSED
        assert pump.queue_artifact({"token": "t2"}) is False
        assert pump.system_queue_consumer is False
        assert pump.take_artifact_leftover() == [art]
        assert pump.take_artifact_leftover() == []
        await chat_writer.drain("chat-e", timeout=5)
        rows = task_store.get_chat_messages("chat-e")
        blocks = [json.loads(m["event_data"]) for m in rows if m.get("event_type") == "system"]
        assert [b["subtype"] for b in blocks] == ["turn_ended", "undelivered_input"]
        assert blocks[1]["message"] == "first\n\nsecond\n\nAttached: notes.txt"
        # A system prompt belongs to the chat: the next turn replays it.
        assert task_store.claim_pending_wake_records("chat-e") == [
            {"prompt": "a nudge", "person": "", "role": "", "by": ""}]

    @pytest.mark.asyncio
    async def test_a_system_prompt_left_at_close_is_stored_for_the_pump_person(self, temp_db):
        """A pump whose turns run as a person (``wake_person``) stores a
        system prompt it never sent as that person's wake, at their role on
        the chat's agent, so the redelivery sweep runs it as them."""
        from core.events import chat_writer
        from storage import database as task_store
        from storage.identity import db_users
        task_store.create_chat("chat-wp", "agent::a1", "a1")
        db_users.add_user_agent("user-viewer", "a1", "editor", "user-admin")
        pump = self._mk_pump()
        pump.chat_id = "chat-wp"
        pump.wake_person = "user-viewer"
        pump.system_queue.append("a delegated result")
        pump.close_queue()
        await chat_writer.drain("chat-wp", timeout=5)
        assert task_store.claim_pending_wake_records("chat-wp") == [
            {"prompt": "a delegated result", "person": "user-viewer", "role": "editor",
             "by": ""}]


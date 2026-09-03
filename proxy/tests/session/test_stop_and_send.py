"""Stop-and-send (R7.2): a message typed onto a busy Claude-CLI-headless
turn fires a graceful-ONLY interrupt at queue time, so the producer's queue
drain delivers it as the next turn within seconds instead of after the whole
turn. Pins the audited invariants:

- Ownership guard: only pumps built by ``_start_new_stream`` carry the
  ``stop_and_send`` flags dict; foreign producers (task runs, meetings,
  duplex, delegate-result echoes, recovery adopts) never drain
  ``message_queue``, so the fire site must treat their ``None`` as
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
    """The attributes the fire site reads — mirrors a stream-owned pump."""

    def __init__(self, chat_id: str = "chat-1", session_id: str = "sess-1"):
        self.chat_id = chat_id
        self.session_id = session_id
        self.message_queue: list[str] = ["queued text"]
        self.stop_and_send: dict | None = {"fired": False, "note": False}
        self.perm_cleared = 0

    def clear_permission_state(self):
        self.perm_cleared += 1


def _controller(layer) -> ChatController:
    ctl = ChatController.__new__(ChatController)
    ctl.layer = layer
    return ctl


@pytest.fixture(autouse=True)
def _clean_registry():
    _active_pumps.clear()
    yield
    _active_pumps.clear()


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
        pump.message_queue = []  # producer claimed it / cancel_queued
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

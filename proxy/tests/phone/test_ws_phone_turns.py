"""Turn protocol on /ws/phone: turn-id stamping, abort, and overlap draining.

The handler is driven directly with a duck-typed WebSocket and a fake
execution layer + pump, so these tests pin the PROTOCOL (frames echo the
chat's turn id; "abort" cancels the in-flight producer; a chat that arrives
while a previous turn is still streaming queues behind the session lock)
without exercising the real layers or DB persistence.
"""

import asyncio
import contextlib
import json

import pytest

from core.events.common_events import CommonEvent, TEXT, PRODUCER_DONE
import ws.phone as ws_phone


_DISCONNECT = object()


class FakeWebSocket:
    """Duck-typed FastAPI WebSocket: scripted client messages, captured sends."""

    def __init__(self):
        self.headers = {"authorization": "Bearer test-key"}
        self.sent: list[dict] = []
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._sent_event = asyncio.Event()

    async def accept(self):
        pass

    async def close(self, code=1000, reason=""):
        pass

    async def receive_text(self) -> str:
        item = await self._incoming.get()
        if item is _DISCONNECT:
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect(1000)
        return item

    async def send_json(self, payload: dict):
        self.sent.append(payload)
        self._sent_event.set()

    def push(self, msg: dict):
        self._incoming.put_nowait(json.dumps(msg))

    def disconnect(self):
        self._incoming.put_nowait(_DISCONNECT)

    async def wait_for_frame(self, predicate, timeout=5.0):
        """Wait until a sent frame matches ``predicate``; returns it."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            for frame in self.sent:
                if predicate(frame):
                    return frame
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise AssertionError(
                    f"no frame matched within {timeout}s; sent={self.sent}"
                )
            self._sent_event.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._sent_event.wait(), timeout=remaining)


class FakeLayer:
    """Execution layer stub: per-session lock + scripted/gated generation."""

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self.gate: asyncio.Event | None = None  # holds generation open when set
        self.cancelled: list[str] = []
        self.prompts: list[str] = []

    def session_lock(self, session_id: str):
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def is_session_alive(self, session_id: str) -> bool:
        return False

    async def start_session(self, session_id: str, config) -> None:
        pass

    async def close_session(self, session_id: str) -> None:
        pass

    async def send_message(self, session_id: str, prompt: str, **kwargs):
        self.prompts.append(prompt)
        try:
            yield CommonEvent(TEXT, {"content": f"reply-to:{prompt}"})
            if self.gate is not None:
                await self.gate.wait()
            yield CommonEvent(TEXT, {"content": " tail"})
        except asyncio.CancelledError:
            self.cancelled.append(prompt)
            raise


class FakePump:
    """Minimal ChatStreamPump: event_queue → attached ws queue."""

    def __init__(self, *, chat_id, session_id, producer, event_queue,
                 perm_queue, scope, source_type):
        self.event_queue = event_queue
        self._ws_queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None

    def attach(self) -> asyncio.Queue:
        self._ws_queue = asyncio.Queue()
        return self._ws_queue

    def start(self):
        async def _pump():
            while True:
                ev = await self.event_queue.get()
                if ev.type == PRODUCER_DONE:
                    self._ws_queue.put_nowait({"pump_type": "all_done"})
                    return
                if ev.type == "error":
                    self._ws_queue.put_nowait({
                        "pump_type": "error",
                        "message": ev.data.get("message", ""),
                    })
                    return
                self._ws_queue.put_nowait({
                    "pump_type": "ws_event",
                    "event": {"type": ev.type, **ev.data},
                })
        self._task = asyncio.create_task(_pump())
        return self._task


class _FakeAgentCfg:
    execution_path = "direct-llm"


@pytest.fixture
def phone_ws_env(monkeypatch):
    """Patch the handler's collaborators; returns (websocket, layer)."""
    layer = FakeLayer()

    monkeypatch.setattr(ws_phone.config, "is_master_key", lambda k: k == "test-key")
    monkeypatch.setattr(ws_phone.config, "get_cli_model", lambda name: "model-x")
    monkeypatch.setattr(ws_phone, "resolve_phone_execution_target", lambda name: None)
    monkeypatch.setattr(ws_phone, "get_execution_layer",
                        lambda name, execution_target=None: layer)

    async def _fake_build(**kwargs):
        return _FakeAgentCfg()
    monkeypatch.setattr(ws_phone, "build_phone_agent_config", _fake_build)

    import core.concurrency as concurrency
    async def _fake_acquire(session_id, target=None, execution_path=None):
        return True
    monkeypatch.setattr(concurrency, "acquire_chat_slot", _fake_acquire)
    monkeypatch.setattr(concurrency, "release_chat_slot", lambda sid: None)

    monkeypatch.setattr(ws_phone.task_store, "create_chat",
                        lambda *a, **kw: None)
    monkeypatch.setattr(ws_phone.task_store, "update_chat",
                        lambda *a, **kw: None)
    monkeypatch.setattr(ws_phone.task_store, "add_chat_message",
                        lambda *a, **kw: None)

    monkeypatch.setattr(ws_phone, "ChatStreamPump", FakePump)
    monkeypatch.setattr(ws_phone, "_active_pumps", {})

    return FakeWebSocket(), layer


async def _run_handler(ws):
    return asyncio.create_task(ws_phone.ws_phone_handler(ws))


async def _warmup(ws):
    ws.push({"type": "warmup", "model": "unified", "llm_mode": "direct",
             "phone_mode": True})
    frame = await ws.wait_for_frame(lambda f: f["type"] in ("warmup_ready", "error"))
    assert frame["type"] == "warmup_ready", frame
    return frame["data"]["session_id"]


def test_turn_frames_are_stamped_with_turn_id(phone_ws_env):
    ws, layer = phone_ws_env

    async def run():
        handler = await _run_handler(ws)
        await _warmup(ws)

        ws.push({"type": "chat", "prompt": "hello", "turn": 5})
        done = await ws.wait_for_frame(
            lambda f: f["type"] == "done" and f.get("turn") == 5)
        assert done["turn"] == 5

        texts = [f for f in ws.sent if f["type"] == "text"]
        assert texts and all(f["turn"] == 5 for f in texts)

        ws.disconnect()
        await handler

    asyncio.run(run())


def test_abort_cancels_inflight_turn(phone_ws_env):
    ws, layer = phone_ws_env

    async def run():
        handler = await _run_handler(ws)
        await _warmup(ws)

        layer.gate = asyncio.Event()  # hold generation open after first token
        ws.push({"type": "chat", "prompt": "long question", "turn": 1})
        await ws.wait_for_frame(
            lambda f: f["type"] == "text" and f.get("turn") == 1)

        ws.push({"type": "abort", "turn": 1})
        done = await ws.wait_for_frame(
            lambda f: f["type"] == "done" and f.get("turn") == 1)
        assert done is not None
        assert layer.cancelled == ["long question"]
        # the gated tail never streamed
        tail = [f for f in ws.sent
                if f["type"] == "text" and "tail" in f["data"]["content"]]
        assert not tail

        ws.disconnect()
        await handler

    asyncio.run(run())


def test_chat_during_active_turn_queues_behind_session_lock(phone_ws_env):
    """A follow-up chat while a turn streams (proxy-mode drain overlap) runs
    after the first completes; both keep their own turn ids."""
    ws, layer = phone_ws_env

    async def run():
        handler = await _run_handler(ws)
        await _warmup(ws)

        layer.gate = asyncio.Event()
        ws.push({"type": "chat", "prompt": "first", "turn": 1})
        await ws.wait_for_frame(
            lambda f: f["type"] == "text" and f.get("turn") == 1)

        # Client abandoned turn 1 (barge-in) and moved on — turn 2 must wait
        # for turn 1's generation to drain, not interleave into it.
        ws.push({"type": "chat", "prompt": "second", "turn": 2})
        await asyncio.sleep(0.05)
        assert layer.prompts == ["first"]  # second not started yet

        layer.gate.set()
        done2 = await ws.wait_for_frame(
            lambda f: f["type"] == "done" and f.get("turn") == 2)
        assert done2 is not None
        assert layer.prompts == ["first", "second"]

        done_turns = [f["turn"] for f in ws.sent if f["type"] == "done"]
        assert done_turns == [1, 2]

        ws.disconnect()
        await handler

    asyncio.run(run())

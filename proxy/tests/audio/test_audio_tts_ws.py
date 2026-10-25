"""WebSocket tests for the chat TTS endpoint (``/ws/audio/tts``) — the platform
sink for voice mode.

The handler runs a reader + a pump task concurrently (it pushes text while
draining audio), which Starlette's synchronous ``TestClient`` cannot interleave
(its WS ``receive`` blocks the loop → deadlock). So these drive the handler
coroutine directly against a controllable fake WebSocket in a real event loop —
the same interleaving uvicorn provides. Covers the init/token handshake (purpose
``audio_tts``), text→audio→done over one streaming context, barge-in cancel, the
``max_chars`` cap, and replay / bad-token / wrong-purpose rejection.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from services.media import audio_service, ws_audio_token
from ws.audio import ws_audio_tts_handler


class WsFakeTTS:
    """Streaming TTS fake: each pushed text chunk yields one PCM frame; the
    ``is_last`` flush (or ``cancel``) ends ``receive_audio``."""

    def __init__(self):
        self.voice_id = ""
        self.voices: dict[str, str] = {}
        self._queue: asyncio.Queue | None = None
        self._cancelled = False

    async def connect(self):
        pass

    async def close(self):
        pass

    def select_voice(self, language):
        return self.voice_id

    def start_streaming_context(self, *, output_sample_rate=None, language=None):
        self._queue = asyncio.Queue()
        self._cancelled = False

    async def send_text_chunk(self, text, is_last=False):
        if self._cancelled or self._queue is None:
            return
        if text:
            await self._queue.put(b"PCM:" + text.encode())
        if is_last:
            await self._queue.put(None)  # end-of-stream sentinel

    async def receive_audio(self):
        while True:
            item = await self._queue.get()
            if item is None or self._cancelled:
                break
            yield item

    def cancel(self):
        self._cancelled = True
        if self._queue is not None:
            self._queue.put_nowait(None)

    def billing_unit(self):
        return "char"

    def cost_per_unit(self):
        return 0.0


class FakeWS:
    """Minimal WebSocket double: scripted inbound frames + captured outbound
    frames over asyncio.Queues, so a real event loop interleaves the handler's
    reader + pump exactly as uvicorn would."""

    def __init__(self):
        self._in: asyncio.Queue = asyncio.Queue()
        self.out: asyncio.Queue = asyncio.Queue()
        self.closed: int | None = None
        self.accepted = False

    # ── server side (called by the handler) ──
    async def accept(self):
        self.accepted = True

    async def receive_json(self):
        msg = await self._in.get()
        return json.loads(msg["text"])

    async def receive(self):
        return await self._in.get()

    async def send_json(self, obj):
        await self.out.put(("json", obj))

    async def send_bytes(self, data):
        await self.out.put(("bytes", data))

    async def close(self, code=1000):
        self.closed = code
        self._in.put_nowait({"type": "websocket.disconnect"})  # unblock a pending receive

    # ── test side ──
    def send(self, obj):
        self._in.put_nowait({"type": "websocket.receive", "text": json.dumps(obj)})

    async def next(self):
        return await asyncio.wait_for(self.out.get(), 2.0)


@pytest.fixture(autouse=True)
def _patch_provider(monkeypatch):
    monkeypatch.setattr(
        audio_service, "build_chat_provider",
        lambda ptype, *, provider_id=None: (WsFakeTTS(), {"id": provider_id or 1, "provider_name": "fake"}),
    )
    monkeypatch.setattr(audio_service, "record_audio_usage", lambda *a, **k: None)


def _tts_token(sub="user-admin", *, max_chars=10000):
    return ws_audio_token.create_ws_audio_token(
        sub, purpose=ws_audio_token.PURPOSE_TTS, provider_id=1, max_chars=max_chars,
    )["ws_token"]


def _run(scenario):
    # Hard ceiling so a logic bug fails fast instead of hanging the suite.
    asyncio.run(asyncio.wait_for(scenario(), 5.0))


def test_handshake_text_audio_done():
    async def scenario():
        ws = FakeWS()
        task = asyncio.create_task(ws_audio_tts_handler(ws))
        ws.send({"type": "init", "token": _tts_token(), "language": "en"})
        assert await ws.next() == ("json", {"type": "ready"})
        ws.send({"type": "text", "text": "hello"})
        assert await ws.next() == ("bytes", b"PCM:hello")
        ws.send({"type": "text", "text": " world"})
        assert await ws.next() == ("bytes", b"PCM: world")
        ws.send({"type": "done"})
        assert await ws.next() == ("json", {"type": "ended"})
        await task
    _run(scenario)


def test_cancel_stops():
    async def scenario():
        ws = FakeWS()
        task = asyncio.create_task(ws_audio_tts_handler(ws))
        ws.send({"type": "init", "token": _tts_token()})
        assert await ws.next() == ("json", {"type": "ready"})
        ws.send({"type": "text", "text": "hi"})
        assert await ws.next() == ("bytes", b"PCM:hi")
        ws.send({"type": "cancel"})
        await task  # handler breaks + cleans up
    _run(scenario)


def test_bad_token_rejected():
    async def scenario():
        ws = FakeWS()
        task = asyncio.create_task(ws_audio_tts_handler(ws))
        ws.send({"type": "init", "token": "garbage"})
        await task
        assert ws.closed == 4401
    _run(scenario)


def test_stt_token_rejected_on_tts_ws():
    async def scenario():
        ws = FakeWS()
        task = asyncio.create_task(ws_audio_tts_handler(ws))
        # A token minted for STT must not open the TTS socket (purpose mismatch).
        stt_tok = ws_audio_token.create_ws_audio_token("user-admin", max_seconds=60, provider_id=1)["ws_token"]
        ws.send({"type": "init", "token": stt_tok})
        await task
        assert ws.closed == 4401
    _run(scenario)


def test_replayed_token_rejected():
    async def scenario():
        tok = _tts_token()
        ws1 = FakeWS()
        t1 = asyncio.create_task(ws_audio_tts_handler(ws1))
        ws1.send({"type": "init", "token": tok})
        assert await ws1.next() == ("json", {"type": "ready"})
        ws1.send({"type": "done"})
        assert await ws1.next() == ("json", {"type": "ended"})
        await t1
        # Same token again → jti already consumed → rejected.
        ws2 = FakeWS()
        t2 = asyncio.create_task(ws_audio_tts_handler(ws2))
        ws2.send({"type": "init", "token": tok})
        await t2
        assert ws2.closed == 4401
    _run(scenario)


def test_max_chars_cap():
    async def scenario():
        ws = FakeWS()
        task = asyncio.create_task(ws_audio_tts_handler(ws))
        ws.send({"type": "init", "token": _tts_token(max_chars=3)})
        assert await ws.next() == ("json", {"type": "ready"})
        ws.send({"type": "text", "text": "hello"})  # 5 > 3
        kind, payload = await ws.next()
        assert kind == "json" and payload["type"] == "error" and payload["code"] == "max_chars"
        await task
    _run(scenario)

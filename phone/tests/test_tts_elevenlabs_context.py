"""ElevenLabs streaming context — end-of-input close and the final frame.

The provider used to flush a context and keep it open; the server sent no
final frame, so every utterance ended through the 2 s tail stall guard while
the ambience bed stood down — 2 s of dead air after each sentence. Now the
context is closed right behind its flush (``advanced.close_at_flush``, on by
default), a final frame under either key ends the utterance at once, the
hygiene close and ``cancel()`` never double-send, an error drawn by the early
close is ignored, and a text-less utterance sends nothing and returns at
once. The socket is a fake; the poll and guard constants are shrunk.
"""

import asyncio
import base64
import json

import pytest
from audio.providers.tts import elevenlabs as el

_AUDIO = base64.b64encode(b"\x00" * 320).decode()


class FakeWS:
    """Records every frame sent; ``recv`` replays the scripted frames and then
    stays silent (the receive loop's poll timeout fires)."""

    def __init__(self, script=None):
        self.sent: list[dict] = []
        self.script = list(script or [])
        self.close_code = None

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self.script:
            await asyncio.sleep(10)
        return json.dumps(self.script.pop(0))

    async def close(self):
        self.close_code = 1000


def _tts(monkeypatch, ws, advanced=None):
    tts = el.ElevenLabsTTS(api_key="k", voice_id="v", advanced=advanced)
    tts._ws = ws
    tts._ws_bound = ("v", tts._model_id, 8000, None)
    tts._ws_gen = 1
    monkeypatch.setattr(el, "_RECV_POLL_S", 0.01)
    monkeypatch.setattr(el, "_POST_FLUSH_TAIL_STALL_S", 0.03)
    monkeypatch.setattr(el, "_POST_FLUSH_STALL_S", 0.05)
    return tts


def _closes(ws):
    return [f for f in ws.sent if f.get("close_context")]


async def _speak(tts, *chunks):
    tts.start_streaming_context()
    for text in chunks[:-1]:
        await tts.send_text_chunk(text)
    await tts.send_text_chunk(chunks[-1], is_last=True)
    return tts._ctx


async def _receive(tts):
    return [c async for c in tts.receive_audio()]


@pytest.mark.asyncio
async def test_one_shot_utterance_closes_on_the_first_audio_frame(monkeypatch):
    """The whole utterance flushed before any audio (an outbound opening):
    the close waits for the first audio frame — a close racing the flush on
    a fresh context made the server restart the utterance mid-word."""
    ws = FakeWS([{"audio": _AUDIO}, {"audio": _AUDIO}, {"isFinal": True, "contextId": None}])
    tts = _tts(monkeypatch, ws)
    ctx = await _speak(tts, "Hello ", "world")
    assert ws.sent[0]["text"] == " " and "generation_config" in ws.sent[0]   # init
    assert ws.sent[1] == {"text": "Hello ", "context_id": ctx.id}
    assert ws.sent[2] == {"text": "world ", "context_id": ctx.id}
    assert ws.sent[3] == {"context_id": ctx.id, "flush": True}
    assert len(ws.sent) == 4 and not ctx.close_sent          # no close yet
    chunks = await _receive(tts)
    assert len(chunks) == 2 and len(chunks[0]) == 320
    assert ws.sent[4] == {"context_id": ctx.id, "close_context": True}
    assert ctx.close_sent and ctx.audio_started
    await asyncio.sleep(0)               # a hygiene close would fire here
    assert len(_closes(ws)) == 1         # exactly one close, after the first frame


@pytest.mark.asyncio
async def test_streamed_utterance_closes_right_behind_the_flush(monkeypatch):
    """Audio already flowing when the input ends (a normal turn): the close
    rides with the flush."""
    ws = FakeWS([{"audio": _AUDIO}, {"isFinal": True}])
    tts = _tts(monkeypatch, ws)
    tts.start_streaming_context()
    await tts.send_text_chunk("Hello ")
    ctx = tts._ctx
    ctx.audio_started = True             # the server streamed the first batch
    await tts.send_text_chunk("world", is_last=True)
    assert ws.sent[-2] == {"context_id": ctx.id, "flush": True}
    assert ws.sent[-1] == {"context_id": ctx.id, "close_context": True}
    assert len(await _receive(tts)) == 1
    await asyncio.sleep(0)
    assert len(_closes(ws)) == 1


@pytest.mark.asyncio
async def test_snake_case_final_frame_is_accepted_too(monkeypatch):
    ws = FakeWS([{"audio": _AUDIO}, {"is_final": True}])
    tts = _tts(monkeypatch, ws)
    await _speak(tts, "hi")
    assert len(await _receive(tts)) == 1
    await asyncio.sleep(0)
    assert len(_closes(ws)) == 1


@pytest.mark.asyncio
async def test_without_a_final_frame_the_tail_guard_ends_it_and_no_second_close(monkeypatch):
    ws = FakeWS([{"audio": _AUDIO}])
    tts = _tts(monkeypatch, ws)
    await _speak(tts, "hi")
    chunks = await _receive(tts)
    assert len(chunks) == 1
    await asyncio.sleep(0)
    assert len(_closes(ws)) == 1         # the close sent at flush; none from the exit


@pytest.mark.asyncio
async def test_cancel_closes_only_when_no_close_was_sent(monkeypatch):
    ws = FakeWS()
    tts = _tts(monkeypatch, ws)
    tts.start_streaming_context()
    await tts.send_text_chunk("hi")      # no end of input yet
    tts.cancel()
    await asyncio.sleep(0)
    assert len(_closes(ws)) == 1         # barge-in close

    ws2 = FakeWS([{"audio": _AUDIO}])
    tts2 = _tts(monkeypatch, ws2)
    await _speak(tts2, "hi")
    await _receive(tts2)                 # the close went out on the first frame
    assert tts2._ctx is None or tts2._ctx.close_sent or len(_closes(ws2)) == 1
    tts2.cancel()
    await asyncio.sleep(0)
    assert len(_closes(ws2)) == 1        # cancel() did not send a second one


@pytest.mark.asyncio
async def test_error_frame_after_the_early_close_does_not_end_the_utterance(monkeypatch):
    ws = FakeWS([
        {"audio": _AUDIO},
        {"error": "context is closed", "code": 4000},
        {"audio": _AUDIO},
        {"isFinal": True},
    ])
    tts = _tts(monkeypatch, ws)
    await _speak(tts, "hi")
    assert len(await _receive(tts)) == 2


@pytest.mark.asyncio
async def test_close_at_flush_off_keeps_the_flush_only_flow(monkeypatch):
    ws = FakeWS([{"audio": _AUDIO}])
    tts = _tts(monkeypatch, ws, advanced={"close_at_flush": False})
    ctx = await _speak(tts, "hi")
    assert ws.sent[-1] == {"context_id": ctx.id, "flush": True}
    assert not ctx.close_sent
    assert len(await _receive(tts)) == 1          # tail guard
    assert not ctx.close_sent                     # no close on the first frame either
    await asyncio.sleep(0)
    assert len(_closes(ws)) == 1                  # the hygiene close at exit


@pytest.mark.asyncio
async def test_empty_utterance_sends_nothing_and_returns_at_once(monkeypatch):
    ws = FakeWS()
    tts = _tts(monkeypatch, ws)
    tts.start_streaming_context()
    await tts.send_text_chunk("", is_last=True)
    assert ws.sent == [] and tts._ctx.empty
    chunks = await asyncio.wait_for(_receive(tts), timeout=0.5)
    assert chunks == []
    await asyncio.sleep(0)
    assert _closes(ws) == []


def test_close_at_flush_setting_validation():
    ok = el.ElevenLabsTTS.validate_advanced
    assert ok({"close_at_flush": False}) == {}
    assert ok({"close_at_flush": "false"}) == {}
    assert "close_at_flush" in ok({"close_at_flush": "maybe"})
    assert el._as_bool("off", True) is False and el._as_bool(None, True) is True
    assert el.ElevenLabsTTS(api_key="k", voice_id="v",
                            advanced={"close_at_flush": "0"})._close_at_flush is False

"""TwilioMediaStreamConnection against a scripted fake Twilio client.

Twilio DIALS US, so unlike the FakeScribeWS pattern the fake here is a WS
CLIENT talking to a real aiohttp server hosting the transport — the same
socket shape production sees. Frames use the documented Media Streams JSON
(connected/start/media/mark/clear/stop)."""

import asyncio
import base64
import contextlib
import json

import pytest
import pytest_asyncio
from aiohttp import ClientSession, WSMsgType, web

from telephony.g711 import mulaw_to_pcm16, pcm16_to_mulaw
from telephony.twilio_media import (
    TwilioMediaError,
    TwilioMediaStreamConnection,
    read_stream_start,
)
from transport.base import (
    FRAME_AUDIO,
    FRAME_DTMF,
    FRAME_HANGUP,
    MediaTransport,
    TransportError,
)

STREAM_SID = "MZ0123456789abcdef0123456789abcdef"
CALL_SID = "CA0123456789abcdef0123456789abcdef"


def _start_msg(custom: dict | None = None, fmt: dict | None = None) -> dict:
    return {
        "event": "start",
        "sequenceNumber": "1",
        "streamSid": STREAM_SID,
        "start": {
            "accountSid": "ACxx",
            "streamSid": STREAM_SID,
            "callSid": CALL_SID,
            "tracks": ["inbound"],
            "mediaFormat": fmt or {
                "encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1,
            },
            "customParameters": custom or {},
        },
    }


def _media_msg(pcm: bytes, chunk: int = 1) -> dict:
    return {
        "event": "media",
        "sequenceNumber": str(chunk + 1),
        "streamSid": STREAM_SID,
        "media": {
            "track": "inbound",
            "chunk": str(chunk),
            "timestamp": str(chunk * 20),
            "payload": base64.b64encode(pcm16_to_mulaw(pcm)).decode(),
        },
    }


class TransportHarness:
    """Aiohttp server whose handler builds the transport after the start
    handshake, plus the fake Twilio client driving it."""

    def __init__(self):
        self.conn: TwilioMediaStreamConnection | None = None
        self._conn_ready = asyncio.Event()
        self._handler_done = asyncio.Event()
        self.runner = None
        self.port = 0
        self.client_ws = None
        self._session = None

    async def _handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        start = await read_stream_start(ws)
        self.conn = TwilioMediaStreamConnection(
            ws, stream_sid=start.get("streamSid", ""),
            call_sid=start.get("callSid", ""),
        )
        self._conn_ready.set()
        # Keep the handler alive until the transport closes (production
        # awaits run_call here).
        while not self.conn.is_closed:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # let close() finish with the socket
        self._handler_done.set()
        return ws

    async def start(self):
        app = web.Application()
        app.router.add_get("/media", self._handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self._session = ClientSession()
        self.client_ws = await self._session.ws_connect(
            f"ws://127.0.0.1:{self.port}/media")
        return self

    async def handshake(self, custom=None, fmt=None):
        await self.client_ws.send_str(json.dumps(
            {"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        await self.client_ws.send_str(json.dumps(_start_msg(custom, fmt)))
        await asyncio.wait_for(self._conn_ready.wait(), timeout=2.0)
        return self.conn

    async def send(self, msg: dict):
        await self.client_ws.send_str(json.dumps(msg))

    async def recv_json(self, timeout=2.0) -> dict:
        msg = await asyncio.wait_for(self.client_ws.receive(), timeout)
        assert msg.type == WSMsgType.TEXT, f"expected TEXT, got {msg.type}"
        return json.loads(msg.data)

    async def stop(self):
        if self.conn is not None:
            await self.conn.close()
        with contextlib.suppress(Exception):
            await self.client_ws.close()
        with contextlib.suppress(Exception):
            await self._session.close()
        await self.runner.cleanup()


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def harness():
    h = await TransportHarness().start()
    yield h
    await h.stop()


async def test_transport_implements_the_seam():
    assert issubclass(TwilioMediaStreamConnection, MediaTransport)
    assert issubclass(TwilioMediaError, TransportError)


async def test_handshake_and_media_in(harness):
    conn = await harness.handshake()
    assert conn.call_uuid == CALL_SID
    assert (conn.sample_rate_in, conn.sample_rate_out) == (8000, 8000)
    assert conn.frame_bytes_out == 320

    pcm = b"\x00\x10" * 160  # one 20 ms frame
    await harness.send(_media_msg(pcm))
    kind, payload = await asyncio.wait_for(conn.read_frame(), 2.0)
    assert kind == FRAME_AUDIO
    # mulaw is lossy — compare through the codec, not byte-for-byte.
    assert payload == mulaw_to_pcm16(pcm16_to_mulaw(pcm))


async def test_send_audio_is_documented_media_shape(harness):
    conn = await harness.handshake()
    pcm = b"\x01\x02" * 160
    conn.send_audio(pcm)
    await conn.drain()
    frame = await harness.recv_json()
    assert set(frame) == {"event", "streamSid", "media"}
    assert frame["event"] == "media"
    assert frame["streamSid"] == STREAM_SID
    assert set(frame["media"]) == {"payload"}
    assert base64.b64decode(frame["media"]["payload"]) == pcm16_to_mulaw(pcm)


async def test_flush_playback_drops_audio_and_sends_clear(harness):
    conn = await harness.handshake()
    # Stall the writer with a full-queue burst, then flush: queued audio
    # must die, the clear frame must arrive.
    for _ in range(20):
        conn.send_audio(b"\x00\x01" * 160)
    conn.flush_playback()
    await conn.drain()  # balanced task_done bookkeeping or this hangs
    seen = []
    with contextlib.suppress(asyncio.TimeoutError):
        while True:
            seen.append(await harness.recv_json(timeout=0.3))
    clears = [f for f in seen if f["event"] == "clear"]
    assert clears == [{"event": "clear", "streamSid": STREAM_SID}]
    # The flush ran synchronously right after the burst: at most the one
    # in-flight frame the writer had already dequeued may precede the clear.
    media = [f for f in seen if f["event"] == "media"]
    assert len(media) <= 1


async def test_hangup_waits_for_eos_mark_echo(harness):
    conn = await harness.handshake()
    conn.send_hangup()
    frame = await harness.recv_json()
    assert frame == {"event": "mark", "streamSid": STREAM_SID,
                     "mark": {"name": "eos"}}
    # close() blocks until the echo lands (never longer than its 3 s cap).
    close_task = asyncio.create_task(conn.close())
    await asyncio.sleep(0.1)
    assert not close_task.done()
    await harness.send({"event": "mark", "sequenceNumber": "9",
                        "streamSid": STREAM_SID, "mark": {"name": "eos"}})
    await asyncio.wait_for(close_task, 2.0)
    assert conn.is_closed


async def test_hangup_close_times_out_without_echo(harness):
    conn = await harness.handshake()
    conn.send_hangup()
    await harness.recv_json()  # the eos mark
    t0 = asyncio.get_running_loop().time()
    await asyncio.wait_for(conn.close(), 5.0)
    elapsed = asyncio.get_running_loop().time() - t0
    assert 2.5 <= elapsed <= 4.5  # the 3 s echo cap, not a hang
    assert conn.is_closed


async def test_stop_event_surfaces_hangup_frame(harness):
    conn = await harness.handshake()
    await harness.send({"event": "stop", "sequenceNumber": "5",
                        "streamSid": STREAM_SID,
                        "stop": {"accountSid": "ACxx", "callSid": CALL_SID}})
    kind, payload = await asyncio.wait_for(conn.read_frame(), 2.0)
    assert (kind, payload) == (FRAME_HANGUP, b"")
    with pytest.raises(TransportError):
        await asyncio.wait_for(conn.read_frame(), 2.0)


async def test_client_disconnect_raises_transport_error(harness):
    conn = await harness.handshake()
    await harness.client_ws.close()
    with pytest.raises(TransportError):
        await asyncio.wait_for(conn.read_frame(), 2.0)
    assert conn.is_closed


async def test_inbound_backlog_drops_oldest(harness):
    conn = await harness.handshake()
    for i in range(110):  # queue cap is 100
        await harness.send(_media_msg(bytes([i % 256, 0]) * 160, chunk=i))
    await asyncio.sleep(0.3)  # let the reader drain the socket
    first_kind, first_payload = await conn.read_frame()
    assert first_kind == FRAME_AUDIO
    # Frame 0 was dropped: the queue holds the NEWEST 100 frames.
    expected_oldest = mulaw_to_pcm16(pcm16_to_mulaw(bytes([10, 0]) * 160))
    assert first_payload == expected_oldest


async def test_dtmf_becomes_frame_and_unknown_events_are_ignored(harness, caplog):
    # dtmf events surface as FRAME_DTMF (the PIN gate consumes them);
    # the digit VALUE never reaches the logs — it is PIN material.
    conn = await harness.handshake()
    assert conn.has_dtmf_events
    with caplog.at_level("DEBUG"):
        await harness.send({"event": "dtmf", "streamSid": STREAM_SID,
                            "sequenceNumber": "5",
                            "dtmf": {"track": "inbound_track", "digit": "7"}})
        await harness.send({"event": "someday-new", "streamSid": STREAM_SID})
        pcm = b"\x00\x20" * 160
        await harness.send(_media_msg(pcm))
        kind, payload = await asyncio.wait_for(conn.read_frame(), 2.0)
        assert (kind, payload) == (FRAME_DTMF, b"7")
        kind, _ = await asyncio.wait_for(conn.read_frame(), 2.0)
        assert kind == FRAME_AUDIO  # stream unaffected
    assert "7" not in "".join(r.getMessage() for r in caplog.records
                              if "DTMF" in r.getMessage())


async def test_backlog_never_evicts_a_queued_digit(harness):
    """Under mic backlog the queue evicts the oldest AUDIO frame — a queued
    keypress must survive (and the put must not raise QueueFull into the
    read loop, which would kill the stream)."""
    conn = await harness.handshake()
    await harness.send({"event": "dtmf", "streamSid": STREAM_SID,
                        "dtmf": {"digit": "3"}})
    for i in range(120):  # queue cap is 100 — forces eviction
        await harness.send(_media_msg(bytes([i % 256, 0]) * 160, chunk=i))
    await asyncio.sleep(0.3)
    kind, payload = await asyncio.wait_for(conn.read_frame(), 2.0)
    assert (kind, payload) == (FRAME_DTMF, b"3")
    kind, _ = await asyncio.wait_for(conn.read_frame(), 2.0)
    assert kind == FRAME_AUDIO  # stream still healthy after eviction


async def test_read_stream_start_timeout():
    # A socket that never sends start must raise, not hang.
    class _NeverWS:
        async def receive(self):
            await asyncio.sleep(3600)

    with pytest.raises(TwilioMediaError):
        await read_stream_start(_NeverWS(), timeout_s=0.2)


async def test_pipeline_smoke_over_twilio_transport(harness, make_pipeline):
    """End-to-end 8 kHz sanity: a CallPipeline (fake providers) bound to the
    live transport plays its greeting as documented media frames and ends
    cleanly on Twilio's stop event."""
    from pipeline_fakes import FakeLLM

    conn = await harness.handshake()
    pipeline = make_pipeline(conn=conn, llm=FakeLLM())
    assert pipeline._frame_bytes_out == 320  # telephony math from the seam
    run = asyncio.create_task(pipeline.run())

    # Caller audio flows in while the greeting streams out.
    for i in range(5):
        await harness.send(_media_msg(b"\x00\x00" * 160, chunk=i))
    frame = await harness.recv_json(timeout=3.0)
    assert frame["event"] == "media"
    assert len(base64.b64decode(frame["media"]["payload"])) == 160  # 20 ms mulaw

    await harness.send({"event": "stop", "streamSid": STREAM_SID,
                        "stop": {"accountSid": "ACxx", "callSid": CALL_SID}})
    await asyncio.wait_for(run, timeout=5.0)
    assert conn.is_closed

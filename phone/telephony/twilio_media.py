"""Twilio Media Streams transport — the WSS connection Twilio opens to us.

Twilio dials a `wss://` URL from `<Connect><Stream>` TwiML and speaks JSON
text frames both ways: inbound `media` events carry base64 μ-law 8 kHz audio,
outbound audio is the exact 3-key `media` message ({event, streamSid,
media:{payload}}), `clear` empties Twilio's outbound playback buffer, and a
`mark` we send is echoed back once the audio queued before it has PLAYED
(immediately when nothing is buffered). Closing the socket ends the call —
our TwiML has nothing after `<Connect>`.

`TwilioMediaStreamConnection` mirrors `DuplexEngineConnection`'s shape (one
reader task routing inbound frames, one writer task serializing sends) over
an aiohttp server-side WebSocket, transcoding mulaw↔PCM16 at the boundary so
the pipeline sees plain 8 kHz telephony audio.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging

from aiohttp import WSMsgType

from telephony.g711 import mulaw_to_pcm16, pcm16_to_mulaw
from transport.base import (
    FRAME_AUDIO,
    FRAME_DTMF,
    FRAME_HANGUP,
    MediaTransport,
    TransportError,
)

logger = logging.getLogger("twilio_media")

# Protocol constants (Asterisk-compatible telephony format: 8 kHz PCM16 mono,
# 320-byte 20 ms frames — Twilio media is the same audio at 8-bit mulaw).
SAMPLE_RATE = 8000
FRAME_BYTES_OUT = 320

# Mic backlog cap: ~2 s at 20 ms media events. The pipeline consumes
# continuously; a burst beyond this means we're behind — drop OLDEST.
_AUDIO_QUEUE_MAX = 100

#: The mark name that gates end-of-call teardown on played-out audio.
_EOS_MARK = "eos"


class TwilioMediaError(TransportError):
    pass


async def read_stream_start(ws, timeout_s: float = 10.0) -> dict:
    """Consume the handshake (`connected` → `start`) and return the `start`
    block (streamSid, callSid, mediaFormat, customParameters). Raises
    `TwilioMediaError` on close/timeout/garbage before `start` arrives."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TwilioMediaError("no start event within handshake timeout")
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
        except asyncio.TimeoutError:
            raise TwilioMediaError("no start event within handshake timeout")
        if msg.type != WSMsgType.TEXT:
            raise TwilioMediaError(f"socket ended during handshake ({msg.type})")
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError:
            continue
        if data.get("event") == "start":
            return data.get("start") or {}
        # `connected` (and anything unexpected) is skipped, not an error.


class TwilioMediaStreamConnection(MediaTransport):
    """One live Twilio media stream, bound to a pipeline session."""

    has_dtmf_events = True
    transport_name = "twilio"

    def __init__(self, ws, *, stream_sid: str, call_sid: str):
        self._ws = ws
        self._stream_sid = stream_sid
        self.call_uuid = call_sid or None
        self.peer_addr = f"twilio:{(call_sid or stream_sid)[-8:]}"
        self._closed = False
        self._mic_drops = 0  # backlog evictions (counts only — no payloads)
        self._audio_q: asyncio.Queue = asyncio.Queue(maxsize=_AUDIO_QUEUE_MAX)
        # Outbound frames as (kind, json_text): kind "audio" is droppable by
        # flush_playback, control frames ("mark"/"clear") never are.
        self._out_q: asyncio.Queue = asyncio.Queue()
        self._eos_pending = False
        self._eos_echoed = asyncio.Event()
        self._reader = asyncio.create_task(self._read_loop())
        self._writer = asyncio.create_task(self._write_loop())

    # -- MediaTransport: audio format ----------------------------------------

    @property
    def sample_rate_in(self) -> int:
        return SAMPLE_RATE

    @property
    def sample_rate_out(self) -> int:
        return SAMPLE_RATE

    @property
    def frame_bytes_out(self) -> int:
        return FRAME_BYTES_OUT

    # -- socket loops ---------------------------------------------------------

    async def _read_loop(self) -> None:
        try:
            async for msg in self._ws:
                if msg.type != WSMsgType.TEXT:
                    break
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                event = data.get("event")
                if event == "media":
                    payload = (data.get("media") or {}).get("payload") or ""
                    try:
                        pcm = mulaw_to_pcm16(base64.b64decode(payload))
                    except (binascii.Error, ValueError):
                        continue
                    if not pcm:
                        continue
                    self._put_dropping_audio((FRAME_AUDIO, pcm))
                elif event == "stop":
                    # Call ended on Twilio's side — surface the telephony
                    # hangup frame, then end the stream.
                    with contextlib.suppress(asyncio.QueueFull):
                        self._audio_q.put_nowait((FRAME_HANGUP, b""))
                    break
                elif event == "mark":
                    if ((data.get("mark") or {}).get("name")) == _EOS_MARK:
                        self._eos_echoed.set()
                elif event == "dtmf":
                    # The digit value is PIN material — never log it.
                    digit = (data.get("dtmf") or {}).get("digit", "")
                    if digit:
                        self._put_dropping_audio((FRAME_DTMF, digit.encode()))
                        logger.info(f"[{self.peer_addr}] DTMF digit received")
                # connected / unknown events: ignore.
        except Exception:
            pass
        finally:
            self._mark_closed()

    def _put_dropping_audio(self, item: tuple[int, bytes]) -> None:
        """Enqueue, evicting the oldest AUDIO frame when the backlog cap is
        hit. Dropping 20 ms of stale mic audio is cheap; dropping a queued
        keypad digit (or hangup) is not — and a bare put_nowait would raise
        QueueFull into the read loop's broad except, killing the stream."""
        if self._audio_q.full():
            backlog = []
            with contextlib.suppress(asyncio.QueueEmpty):
                while True:
                    backlog.append(self._audio_q.get_nowait())
            drop = next(
                (i for i, f in enumerate(backlog) if f[0] == FRAME_AUDIO), 0)
            if backlog:
                backlog.pop(drop)
                # Eviction was SILENT — a backlogged daemon accumulated
                # seconds of caller latency invisibly. Counts only (never
                # payloads: DTMF digits are PIN material).
                self._mic_drops += 1
                if self._mic_drops % 50 == 1:
                    logger.warning(
                        f"[{self.peer_addr}] mic backlog: dropped oldest "
                        f"frame (total {self._mic_drops})"
                    )
            for f in backlog:
                self._audio_q.put_nowait(f)
        self._audio_q.put_nowait(item)

    async def _write_loop(self) -> None:
        try:
            while True:
                _kind, text = await self._out_q.get()
                try:
                    await self._ws.send_str(text)
                finally:
                    self._out_q.task_done()
        except asyncio.CancelledError:
            return
        except Exception:
            self._mark_closed()

    def _mark_closed(self) -> None:
        if self._closed:
            return
        self._closed = True
        # A hangup can no longer be confirmed played — never stall close().
        self._eos_echoed.set()
        # Wake a blocked read_frame with the end-of-stream sentinel.
        with contextlib.suppress(Exception):
            self._audio_q.put_nowait(None)
        # Release drain() joiners parked on frames the dead writer will
        # never send.
        while True:
            try:
                self._out_q.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._out_q.task_done()

    # -- MediaTransport: frame I/O -------------------------------------------

    async def read_frame(self) -> tuple[int, bytes]:
        if self._closed and self._audio_q.empty():
            raise TwilioMediaError("media stream closed")
        item = await self._audio_q.get()
        if item is None:
            raise TwilioMediaError("media stream closed")
        return item

    def send_audio(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        payload = base64.b64encode(pcm16_to_mulaw(pcm)).decode("ascii")
        self._out_q.put_nowait(("audio", json.dumps({
            "event": "media",
            "streamSid": self._stream_sid,
            "media": {"payload": payload},
        })))

    async def drain(self) -> None:
        await self._out_q.join()

    def flush_playback(self) -> None:
        """Drop queued-unsent audio, then `clear` Twilio's playback buffer.

        Every drained get pairs with task_done; kept control frames re-queue
        (a fresh unfinished item), so drain() bookkeeping stays balanced.
        """
        if self._closed:
            return
        kept: list[tuple[str, str]] = []
        while True:
            try:
                item = self._out_q.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._out_q.task_done()
            if item[0] != "audio":
                kept.append(item)
        for item in kept:
            self._out_q.put_nowait(item)
        self._out_q.put_nowait(("clear", json.dumps({
            "event": "clear",
            "streamSid": self._stream_sid,
        })))

    # -- MediaTransport: lifecycle -------------------------------------------

    @property
    def is_closed(self) -> bool:
        return self._closed

    def send_hangup(self) -> None:
        """Arm graceful end-of-call: the eos mark's echo tells us Twilio has
        PLAYED everything queued before it, so `close()` can end the call
        (WS close = hangup) without clipping the farewell."""
        if self._closed:
            return
        self._eos_pending = True
        self._out_q.put_nowait(("mark", json.dumps({
            "event": "mark",
            "streamSid": self._stream_sid,
            "mark": {"name": _EOS_MARK},
        })))

    async def close(self) -> None:
        if self._eos_pending and not self._closed:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._eos_echoed.wait(), timeout=3.0)
        self._mark_closed()
        self._reader.cancel()
        self._writer.cancel()
        with contextlib.suppress(Exception):
            await self._ws.close()

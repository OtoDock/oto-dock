"""The dial-back socket as a MediaTransport + turn-frame router.

One websocket carries a whole duplex session: binary mic PCM arrives from
the proxy (browser-relayed), binary TTS PCM goes back, and JSON frames run
in both directions — control (``barge_in``/``mute``/``unmute``/``end``) and
the LLM turn stream (``utterance`` out; ``text``/``tool_start``/``tool_end``/
``done``/``error`` in, per-turn-routed exactly like ``proxy/client.py``).

A single reader task routes inbound frames; a single writer task serializes
outbound sends (audio frames and JSON events must never interleave
mid-message). The MediaTransport surface makes this connection a drop-in
for the pipeline: mic frames come out of ``read_frame`` as
``(FRAME_AUDIO, pcm)``; stream end surfaces as ``TransportError``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from transport.base import FRAME_AUDIO, MediaTransport, TransportError

logger = logging.getLogger("duplex")

# Mic backlog cap: ~2s at 20ms frames. The pipeline consumes continuously;
# a burst beyond this means we're behind — drop OLDEST to keep latency.
_AUDIO_QUEUE_MAX = 100


class DuplexEngineConnection(MediaTransport):
    """One live duplex session's socket, shared by transport + LLM client."""

    transport_name = "duplex"

    def __init__(self, ws, duplex_id: str, *, rate_in: int, rate_out: int):
        self._ws = ws
        self.call_uuid = duplex_id
        self.peer_addr = f"duplex:{duplex_id[:8]}"
        self._rate_in = int(rate_in)
        self._rate_out = int(rate_out)
        self._closed = False
        self._muted = False
        self._mic_drops = 0  # backlog evictions (counts only — no payloads)

        self._audio_q: asyncio.Queue = asyncio.Queue(maxsize=_AUDIO_QUEUE_MAX)
        self._out_q: asyncio.Queue = asyncio.Queue()
        # Per-turn frame routing (mirrors ProxyClient): frames for the current
        # turn land in a fresh queue; abandoned turns' frames die silently.
        self._turn_q: asyncio.Queue | None = None
        self._current_turn = 0
        # Manual barge-in taps from the browser (relayed by the proxy).
        self.on_barge_in = None  # async callable | None
        # Click-to-edit (R3.8): hold = the composer takes ownership of the
        # pending utterance; typed_utterance = the edited text dispatched as
        # a spoken-mode turn.
        self.on_hold = None             # async callable | None
        self.on_typed_utterance = None  # async callable(text) | None
        # Muted-window STT survival (2026-08-12 dead-mic incident): while
        # muted/held, mic frames are DROPPED — Deepgram idle-kills its
        # websocket after ~12s without audio or keepalives (close 1011),
        # and every later utterance was silently discarded ("send skipped —
        # connection closed"). The session wires these so a keepalive rides
        # each dropped frame (provider-throttled) and the unmute edge can
        # health-check + reconnect the provider.
        self.on_muted_audio = None      # async callable | None
        self.on_unmute = None           # async callable | None
        self.on_release = None          # async callable(text) | None
        # Optional zero-arg callable the session wires so the hangup frame
        # can say WHY (the pipeline calls send_hangup from shared code):
        # "agent_complete" ([DUPLEX_COMPLETE] farewell — the browser skips
        # the exit note, the agent knows it ended the mode) vs "engine"
        # (idle timeout / limits — the note still applies).
        self.hangup_reason = None  # () -> str | None

        self._reader = asyncio.create_task(self._read_loop())
        self._writer = asyncio.create_task(self._write_loop())

    # -- MediaTransport: audio format ----------------------------------------

    @property
    def sample_rate_in(self) -> int:
        return self._rate_in

    @property
    def sample_rate_out(self) -> int:
        return self._rate_out

    @property
    def frame_bytes_out(self) -> int:
        # One 20 ms output frame: rate × 2 bytes × 0.02.
        return self._rate_out * 2 // 50

    # -- socket loops ---------------------------------------------------------

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, (bytes, bytearray)):
                    if self._muted:
                        # Frame dropped — the session keeps the STT provider's
                        # socket AND audio timeline alive (silence at the mic
                        # cadence; a keepalive alone leaves the streaming model
                        # stale after long mutes — audit F16). The frame length
                        # rides along so the silence matches the real cadence.
                        cb = self.on_muted_audio
                        if cb is not None:
                            with contextlib.suppress(Exception):
                                await cb(len(raw))
                        continue
                    if self._audio_q.full():
                        with contextlib.suppress(asyncio.QueueEmpty):
                            self._audio_q.get_nowait()
                        # Backlog eviction was SILENT — a stalled pipeline
                        # accumulated seconds of latency invisibly. Counts
                        # only, rate-limited.
                        self._mic_drops += 1
                        if self._mic_drops % 50 == 1:
                            logger.warning(
                                f"[{self.peer_addr}] mic backlog: dropped "
                                f"oldest frame (total {self._mic_drops})"
                            )
                    self._audio_q.put_nowait(bytes(raw))
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._route_json(msg)
        except Exception:
            pass
        finally:
            self._mark_closed()

    async def _route_json(self, msg: dict) -> None:
        mtype = msg.get("type", "")
        if mtype in ("text", "tool_start", "tool_end", "done", "error", "session"):
            turn = int(msg.get("turn") or 0)
            q = self._turn_q
            if q is not None and turn == self._current_turn:
                q.put_nowait(msg)
        elif mtype == "barge_in":
            cb = self.on_barge_in
            if cb is not None:
                with contextlib.suppress(Exception):
                    await cb()
        elif mtype == "mute":
            self._muted = True
        elif mtype == "unmute":
            self._muted = False
            # Mic is live again — let the session heal a provider socket
            # that died during the muted window (see on_unmute).
            cb = self.on_unmute
            if cb is not None:
                with contextlib.suppress(Exception):
                    await cb()
        elif mtype == "hold":
            # Click-to-edit: mic muted AND the session drops its pending
            # turn accumulation (the composer owns the text from here).
            self._muted = True
            cb = self.on_hold
            if cb is not None:
                with contextlib.suppress(Exception):
                    await cb()
        elif mtype == "release":
            # Mic unmuted while the composer held the conversation: live
            # listening resumes and an optional held draft rides along to
            # re-seed the pending turn (sticky-mute UX, 2026-08-24).
            self._muted = False
            cb = self.on_release
            if cb is not None:
                with contextlib.suppress(Exception):
                    await cb(str(msg.get("text") or ""))
        elif mtype == "typed_utterance":
            cb = self.on_typed_utterance
            if cb is not None:
                with contextlib.suppress(Exception):
                    await cb(str(msg.get("text") or ""))
        elif mtype == "end":
            self._mark_closed()

    async def _write_loop(self) -> None:
        try:
            while True:
                item = await self._out_q.get()
                try:
                    if isinstance(item, (bytes, bytearray)):
                        await self._ws.send(item)
                    else:
                        await self._ws.send(json.dumps(item))
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
        # Wake a blocked read_frame with the end-of-stream sentinel.
        with contextlib.suppress(Exception):
            self._audio_q.put_nowait(None)
        if self._turn_q is not None:
            self._turn_q.put_nowait({"type": "_closed"})

    # -- turn-frame routing (LLM client side) ---------------------------------

    def new_turn(self, turn: int) -> asyncio.Queue:
        """Fresh per-turn queue; frames from older turns die with theirs."""
        self._current_turn = turn
        self._turn_q = asyncio.Queue()
        return self._turn_q

    def send_event(self, payload: dict) -> None:
        """Queue one JSON frame to the proxy (state/captions/utterance)."""
        if not self._closed:
            self._out_q.put_nowait(payload)

    # -- MediaTransport: frame I/O -------------------------------------------

    async def read_frame(self) -> tuple[int, bytes]:
        if self._closed and self._audio_q.empty():
            raise TransportError("duplex stream closed")
        item = await self._audio_q.get()
        if item is None:
            raise TransportError("duplex stream closed")
        return FRAME_AUDIO, item

    def send_audio(self, pcm: bytes) -> None:
        if not self._closed:
            self._out_q.put_nowait(pcm)

    async def drain(self) -> None:
        await self._out_q.join()

    def flush_playback(self) -> None:
        """Barge-in: drop queued outbound AUDIO and tell the browser to cut
        its scheduled buffer (the client plays 250 ms–1 s ahead — without the
        flush frame it talks over the caller for that long). JSON events in
        the queue survive (state/caption frames must not be lost). Single
        event loop + sync method ⇒ at most the one already-dequeued frame
        (≤20 ms) escapes; the writer's FIFO keeps the flush frame ordered
        after any such frame and before all later audio."""
        if self._closed:
            return
        kept: list[dict] = []
        while True:
            try:
                item = self._out_q.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not isinstance(item, (bytes, bytearray)):
                kept.append(item)
            self._out_q.task_done()
        for item in kept:
            self._out_q.put_nowait(item)
        self._out_q.put_nowait({"type": "flush"})

    def pause_playback(self) -> None:
        """Barge-in pause: the browser player suspends in place, retaining
        its scheduled lead (250 ms–1 s) for a lossless resume. The paced
        sender drains the out-queue per frame, so plain FIFO ordering puts
        this ahead of any later audio."""
        self.send_event({"type": "pause"})

    def resume_playback(self) -> None:
        """Undo pause_playback: the player resumes from where it froze."""
        self.send_event({"type": "resume"})

    # -- MediaTransport: lifecycle -------------------------------------------

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def is_muted(self) -> bool:
        """Mic gate state (mute/hold frames). The session's caption channels
        check this so late STT output from audio already in flight can't
        pollute a held composer draft."""
        return self._muted

    def send_hangup(self) -> None:
        reason = "engine"
        if self.hangup_reason is not None:
            with contextlib.suppress(Exception):
                reason = str(self.hangup_reason() or "engine")
        self.send_event({"type": "end", "reason": reason})

    async def close(self) -> None:
        self._mark_closed()
        self._reader.cancel()
        self._writer.cancel()
        with contextlib.suppress(Exception):
            await self._ws.close()

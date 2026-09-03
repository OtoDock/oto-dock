"""The media-transport contract the conversation pipeline runs against.

This formalizes the seam `CallPipeline` has always used (the exact surface
`tests/pipeline_fakes.FakeConn` mirrors): frame I/O plus lifecycle, extended
with explicit sample-rate properties so one engine serves 8 kHz telephony and
16 kHz-in / 24 kHz-out duplex sessions. In-rate and out-rate genuinely differ
for duplex, so there is deliberately no single `frame_bytes` — the paced
sender's unit is `frame_bytes_out`, and input chunking derives from
`sample_rate_in` (the VAD's 32 ms window).

Audio format is 16-bit signed little-endian mono PCM at the declared rates on
both sides; `frame_bytes_out` is one 20 ms output frame.

`read_frame` yields `(kind, payload)`. The kind vocabulary embeds the
AudioSocket TLV type space (its wire values, so the telephony transport passes
frames through untouched) plus daemon-local kinds allocated OUTSIDE that space
(`FRAME_DTMF` — AudioSocket has no DTMF TLV); non-telephony transports emit
`FRAME_AUDIO` for media and `FRAME_HANGUP` for end-of-conversation and, when
`has_dtmf_events`, `FRAME_DTMF` for keypad digits. Stream errors raise
`TransportError` (or a transport-specific subclass).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

#: End of conversation (AudioSocket TLV 0x00).
FRAME_HANGUP = 0x00
#: One PCM media frame (AudioSocket TLV 0x10).
FRAME_AUDIO = 0x10
#: One keypad digit, payload = the ASCII digit (daemon-local kind — chosen
#: outside the live AudioSocket TLV space 0x00-0x12 so passthrough can never
#: collide).
FRAME_DTMF = 0x20


class TransportError(Exception):
    """The media stream failed or ended unexpectedly."""


class MediaTransport(ABC):
    """One live audio connection a pipeline session is bound to."""

    #: Human-readable peer label for logs ("host:port", chat id, …).
    peer_addr: str
    #: Correlation id of this media stream (AudioSocket UUID, duplex session id).
    call_uuid: str | None
    #: True when the transport delivers keypad digits as `FRAME_DTMF` frames
    #: (Twilio media streams). False means digits, if any, exist only in-band
    #: in the audio (AudioSocket) — consumers that need them run a detector.
    has_dtmf_events: bool = False
    #: Short transport label for call-log rows ("twilio" / "audiosocket" /
    #: "duplex").
    transport_name: str = "audiosocket"

    # -- audio format ---------------------------------------------------------

    @property
    @abstractmethod
    def sample_rate_in(self) -> int:
        """Hz of the PCM this transport delivers via `read_frame`."""

    @property
    @abstractmethod
    def sample_rate_out(self) -> int:
        """Hz of the PCM this transport expects in `send_audio`."""

    @property
    @abstractmethod
    def frame_bytes_out(self) -> int:
        """Bytes of one 20 ms output frame (rate_out × 2 bytes × 0.02)."""

    # -- frame I/O ------------------------------------------------------------

    @abstractmethod
    async def read_frame(self) -> tuple[int, bytes]:
        """Next inbound `(kind, payload)` frame; raises `TransportError`
        when the stream ends."""

    @abstractmethod
    def send_audio(self, pcm: bytes) -> None:
        """Queue outbound PCM (non-blocking; pair with `drain`)."""

    @abstractmethod
    async def drain(self) -> None:
        """Backpressure point after `send_audio`."""

    def flush_playback(self) -> None:
        """Discard transport-buffered outbound audio that has not played yet
        (barge-in hard cut). Default no-op: paced transports buffer at most
        one in-flight frame. A transport whose peer buffers ahead (Twilio
        media streams) drops its queued audio and cuts the peer's buffer."""

    def pause_playback(self) -> None:
        """Tell a lead-buffering peer to SUSPEND playback in place (barge-in
        pause — reversible, nothing discarded). Default no-op: paced
        transports have at most one frame in flight, so the daemon-side
        sender stopping IS the pause. The duplex connection forwards a
        control frame so the browser player suspends its scheduled lead."""

    def resume_playback(self) -> None:
        """Undo ``pause_playback``: the peer resumes from where it froze.
        Default no-op (see pause_playback)."""

    def poll_dtmf(self) -> tuple[str, int] | None:
        """Next side-channel keypad digit as ``(digit, duration_ms)``, or
        None. Digits that arrive OUTSIDE the media stream (AudioSocket: the
        AMI event listener) surface here rather than as ``FRAME_DTMF`` —
        the read path stays untouched and consumers (the PIN gate) drain
        this between frames. Transports with native digit events (Twilio)
        keep this default None."""
        return None

    # -- lifecycle ------------------------------------------------------------

    @property
    @abstractmethod
    def is_closed(self) -> bool: ...

    @abstractmethod
    def send_hangup(self) -> None:
        """Signal end-of-conversation to the peer (telephony hangup frame;
        duplex end frame). Best-effort — `close` is the hard stop."""

    @abstractmethod
    async def close(self) -> None: ...

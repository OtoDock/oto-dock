"""Asterisk AudioSocket TLV protocol implementation.

AudioSocket protocol (TCP):
  Each frame: [1 byte type][2 bytes length (big-endian)][payload]

Frame types:
  0x00 = Hangup (no payload)
  0x01 = UUID (16 bytes, sent once on connect)
  0x10 = Audio (PCM 8kHz 16-bit signed LE mono, typically 320 bytes = 20ms)
  0x11 = Silence notification (Asterisk >= 22, no payload)
  0x12 = Error (payload = error message)
"""

import asyncio
import collections
import logging
import struct
import uuid as uuid_mod

import config
from transport.base import FRAME_AUDIO, FRAME_HANGUP, MediaTransport, TransportError

logger = logging.getLogger("audio_socket")

# Frame types (0x00/0x10 are the shared transport vocabulary — see transport.base)
TYPE_HANGUP = FRAME_HANGUP
TYPE_UUID = 0x01
TYPE_AUDIO = FRAME_AUDIO
TYPE_SILENCE = 0x11
TYPE_ERROR = 0x12

# TLV header: 1 byte type + 2 bytes length
HEADER_SIZE = 3


class AudioSocketError(TransportError):
    pass


class AudioSocketConnection(MediaTransport):
    """Manages a single AudioSocket TCP connection from Asterisk."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        self.reader = reader
        self.writer = writer
        self.call_uuid: str | None = None
        self._closed = False
        # Side-channel digits from the AMI event listener (signalled DTMF —
        # never present in the TLV stream). Bounded drop-oldest: nothing
        # drains it outside the PIN gate, a PIN entry is ≤ 7 chars, and
        # anything older is stale by definition. Single asyncio loop — no
        # lock needed.
        self._dtmf_injected: collections.deque[tuple[str, int]] = (
            collections.deque(maxlen=32)
        )

        # Set TCP_NODELAY for low-latency audio
        sock = writer.get_extra_info("socket")
        if sock:
            import socket
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        peer = writer.get_extra_info("peername")
        self.peer_addr = f"{peer[0]}:{peer[1]}" if peer else "unknown"

    # AudioSocket is fixed-format telephony: 8 kHz both ways, 320-byte 20 ms
    # frames (protocol constants — see config.py).
    @property
    def sample_rate_in(self) -> int:
        return config.SAMPLE_RATE

    @property
    def sample_rate_out(self) -> int:
        return config.SAMPLE_RATE

    @property
    def frame_bytes_out(self) -> int:
        return config.FRAME_SIZE

    async def read_frame(self) -> tuple[int, bytes]:
        """Read one TLV frame. Returns (frame_type, payload).

        Raises AudioSocketError on connection issues.
        """
        try:
            header = await self.reader.readexactly(HEADER_SIZE)
        except asyncio.IncompleteReadError:
            raise AudioSocketError("Connection closed during header read")
        except ConnectionError as e:
            raise AudioSocketError(f"Connection error: {e}")

        frame_type = header[0]
        payload_len = struct.unpack(">H", header[1:3])[0]

        if payload_len == 0:
            return frame_type, b""

        try:
            payload = await self.reader.readexactly(payload_len)
        except asyncio.IncompleteReadError:
            raise AudioSocketError("Connection closed during payload read")

        return frame_type, payload

    async def read_uuid(self) -> str:
        """Read the initial UUID frame sent by Asterisk on connect."""
        frame_type, payload = await self.read_frame()
        if frame_type != TYPE_UUID:
            raise AudioSocketError(
                f"Expected UUID frame (0x01), got 0x{frame_type:02x}"
            )
        if len(payload) == 16:
            self.call_uuid = str(uuid_mod.UUID(bytes=payload))
        else:
            # Some Asterisk versions send UUID as ASCII string
            self.call_uuid = payload.decode("ascii", errors="replace").strip()
        logger.info(f"[{self.peer_addr}] Call UUID: {self.call_uuid}")
        return self.call_uuid

    def inject_dtmf(self, digit: str, duration_ms: int = 0) -> None:
        """Queue one AMI-signalled digit for ``poll_dtmf`` (the AMI event
        listener calls this from the same asyncio loop)."""
        if not self._closed:
            self._dtmf_injected.append((digit, duration_ms))

    def poll_dtmf(self) -> tuple[str, int] | None:
        if self._dtmf_injected:
            return self._dtmf_injected.popleft()
        return None

    def send_audio(self, pcm_data: bytes) -> None:
        """Send a single audio frame to Asterisk.

        Caller must ensure pcm_data is exactly FRAME_SIZE bytes.
        """
        if self._closed:
            return

        frame = struct.pack(">BH", TYPE_AUDIO, len(pcm_data)) + pcm_data
        try:
            self.writer.write(frame)
        except ConnectionError:
            self._closed = True

    async def drain(self) -> None:
        """Flush the write buffer."""
        if self._closed:
            return
        try:
            await self.writer.drain()
        except ConnectionError:
            self._closed = True

    def send_hangup(self) -> None:
        """Send a hangup frame to Asterisk."""
        if self._closed:
            return
        frame = struct.pack(">BH", TYPE_HANGUP, 0)
        try:
            self.writer.write(frame)
        except ConnectionError:
            self._closed = True

    async def close(self) -> None:
        """Close the connection."""
        if self._closed:
            return
        self._closed = True
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass

    @property
    def is_closed(self) -> bool:
        return self._closed

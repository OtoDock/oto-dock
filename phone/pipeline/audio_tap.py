"""The outgoing-audio tap: one call's frames, as handed to the transport,
saved as a WAV when the call ends.

Switched on by the ``debug_outgoing_audio`` setting (Infrastructure →
Logging). The hot path only appends ``(monotonic, frame)``; the file is
written off the loop at cleanup, with silence filled in for every gap
longer than one frame period between two sends, so it shows what the
transport was given and when (a frame the transport then dropped was still
given). Only the agent's side is saved; the caller's audio never is.
"""

import re
import time
import wave
from pathlib import Path

# The newest files kept in the directory; older ones go when a new one lands.
KEEP_FILES = 10

_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class OutgoingAudioTap:
    """One call's outgoing frames with their send times."""

    def __init__(self, sample_rate: int, frame_bytes: int) -> None:
        self.sample_rate = sample_rate
        self.frame_bytes = frame_bytes
        self._frames: list[tuple[float, bytes]] = []

    def add(self, frame: bytes) -> None:
        self._frames.append((time.monotonic(), frame))

    @property
    def frame_count(self) -> int:
        return len(self._frames)

    def pcm(self) -> bytes:
        """The frames on their timeline: back to back, with silence for a
        gap longer than one frame period between two sends (sleep jitter of
        a few ms per frame never shows; a stall does, at its length)."""
        frame_s = self.frame_bytes / (self.sample_rate * 2)
        out = bytearray()
        prev_t = None
        for t, frame in self._frames:
            if prev_t is not None:
                gap = t - prev_t - frame_s
                if gap > frame_s:
                    out.extend(b"\x00" * (int(gap * self.sample_rate) * 2))
            out.extend(frame)
            prev_t = t
        return bytes(out)

    def write(self, directory: Path, call_id: str, keep: int = KEEP_FILES) -> Path:
        """Write the WAV under ``directory`` and keep its ``keep`` newest files."""
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = directory / f"{stamp}-{_NAME_RE.sub('-', call_id) or 'call'}-out.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(self.pcm())
        # The name breaks a tie between files written in the same clock tick;
        # a file another call's retention removed meanwhile sorts last.
        def written(p: Path) -> int:
            try:
                return p.stat().st_mtime_ns
            except OSError:
                return 0
        files = sorted(directory.glob("*-out.wav"), key=lambda p: (written(p), p.name), reverse=True)
        for old in files[keep:]:
            old.unlink(missing_ok=True)
        return path

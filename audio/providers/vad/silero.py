"""Silero VAD Lite wrapper with IDLE/SPEAKING state machine.

Uses silero-vad-lite (ONNX, no torch dependency) for CPU-only inference.
Buffers AudioSocket frames (320 bytes = 160 samples) into 512-byte chunks
(256 samples) as required by Silero.

Supports two sensitivity modes:
  - Normal: standard threshold + short debounce (listening phase)
  - Barge-in: higher threshold + longer debounce (during TTS playback)
    to avoid false triggers from echo/noise
"""

from __future__ import annotations

import array
import collections
import logging
import struct
import time

from audio.constants import SAMPLE_RATE, SAMPLE_WIDTH
from audio.providers.vad.base import VadEvent, VadState

logger = logging.getLogger(__name__)

# Silero VAD expects 256 samples at 8kHz (512 bytes of 16-bit PCM)
SILERO_CHUNK_SAMPLES = 256
SILERO_CHUNK_BYTES = SILERO_CHUNK_SAMPLES * SAMPLE_WIDTH


class SileroVad:
    """Silero VAD with state machine and debounce logic."""

    def __init__(
        self,
        threshold: float,
        silence_duration_ms: int,
        speech_pad_ms: int,
        min_energy_rms: float,
        bargein_threshold: float,
        bargein_debounce_ms: int,
        bargein_chunk_ratio: float,
        bargein_silence_duration_ms: int,
    ):
        self.threshold = threshold
        self.silence_duration_ms = silence_duration_ms
        self.speech_pad_ms = speech_pad_ms
        self._min_energy_rms = min_energy_rms

        # Barge-in mode settings (stricter)
        self._bargein_silence_ms = bargein_silence_duration_ms
        self._bargein_threshold = bargein_threshold
        self._bargein_debounce_chunks = max(1, bargein_debounce_ms // 32)
        self._bargein_chunk_ratio = bargein_chunk_ratio
        self._bargein_mode = False

        self.state = VadState.IDLE
        self._buffer = bytearray()

        # Debounce counters
        self._speech_chunks = 0
        self._silence_start_ms: float | None = None
        # Sliding window for barge-in (tolerates natural speech volume dips)
        self._chunk_window: collections.deque[bool] = collections.deque()

        # Normal debounce: ~64ms (2 chunks) — stable detection for telephone audio
        self._normal_debounce_chunks = max(1, speech_pad_ms // 32)

        # Early speech detection during barge-in (fires SPEECH_PROBABLE)
        self._early_speech_count = 0
        self._probable_fired = False

        # Periodic Silero reset during barge-in to prevent LSTM drift
        self._bargein_chunks_since_reset = 0
        self._BARGEIN_RESET_INTERVAL = 30  # ~1s (30 chunks × 32ms)

        # Load Silero VAD ONNX model
        from silero_vad_lite import SileroVAD
        self._model = SileroVAD(SAMPLE_RATE)
        logger.info(
            f"Silero VAD loaded (threshold={threshold}, "
            f"bargein_threshold={self._bargein_threshold}, "
            f"silence={silence_duration_ms}ms, "
            f"bargein_silence={self._bargein_silence_ms}ms, "
            f"bargein_debounce={self._bargein_debounce_chunks} chunks, "
            f"bargein_ratio={self._bargein_chunk_ratio})"
        )

    def set_bargein_mode(self, enabled: bool) -> None:
        """Switch between normal and barge-in sensitivity.

        Call with True when TTS is playing, False when listening.
        Resets Silero on BOTH transitions to prevent LSTM state issues:
          - Entering: fresh start for barge-in detection
          - Leaving: clear echo-corrupted state for normal detection
        """
        if enabled != self._bargein_mode:
            self._bargein_mode = enabled
            # Reset debounce counter when switching modes
            self._speech_chunks = 0
            self._chunk_window.clear()
            self._early_speech_count = 0
            self._probable_fired = False
            self._bargein_chunks_since_reset = 0
            # Always reset Silero on mode transitions
            from silero_vad_lite import SileroVAD
            self._model = SileroVAD(SAMPLE_RATE)
            logger.debug(f"VAD: Silero state reset (bargein={'on' if enabled else 'off'})")

    def reset(self) -> None:
        """Reset state for a new call."""
        self.state = VadState.IDLE
        self._buffer.clear()
        self._speech_chunks = 0
        self._silence_start_ms = None
        self._chunk_window.clear()
        self._bargein_mode = False
        self._early_speech_count = 0
        self._probable_fired = False
        from silero_vad_lite import SileroVAD
        self._model = SileroVAD(SAMPLE_RATE)

    def process(self, audio_bytes: bytes) -> VadEvent:
        """Feed raw PCM bytes and return any state transition event."""
        self._buffer.extend(audio_bytes)

        event = VadEvent.NONE
        while len(self._buffer) >= SILERO_CHUNK_BYTES:
            chunk = bytes(self._buffer[:SILERO_CHUNK_BYTES])
            del self._buffer[:SILERO_CHUNK_BYTES]
            event = self._process_chunk(chunk)
            if event != VadEvent.NONE:
                return event

        return event

    def _process_chunk(self, chunk: bytes) -> VadEvent:
        """Run VAD on a single 256-sample chunk."""
        n_samples = len(chunk) // 2
        int16_samples = struct.unpack(f"<{n_samples}h", chunk)
        float32_audio = array.array("f", (s / 32768.0 for s in int16_samples))
        probability = self._model.process(float32_audio)

        # RMS energy gate: reject low-energy audio (background TV, distant voices).
        # Phone speech (close to mic) is typically RMS 500-5000+, background noise 50-300.
        rms = (sum(s * s for s in int16_samples) / n_samples) ** 0.5
        energy_ok = rms >= self._min_energy_rms

        # Use stricter settings during barge-in mode
        active_threshold = self._bargein_threshold if self._bargein_mode else self.threshold
        active_debounce = self._bargein_debounce_chunks if self._bargein_mode else self._normal_debounce_chunks

        is_speech = probability >= active_threshold and energy_ok
        now_ms = time.monotonic() * 1000

        # Diagnostic: log near-miss detections in IDLE mode
        if self.state == VadState.IDLE and not is_speech:
            if self._bargein_mode and (probability >= 0.3 or rms >= 400):
                logger.debug(
                    f"VAD: near-miss prob={probability:.2f} rms={rms:.0f} "
                    f"(thresh={active_threshold} min_rms={self._min_energy_rms})"
                )
            elif not self._bargein_mode and (probability >= 0.3 or rms >= 200):
                logger.debug(
                    f"VAD: near-miss prob={probability:.2f} rms={rms:.0f} "
                    f"(thresh={active_threshold} min_rms={self._min_energy_rms})"
                )

        if self.state == VadState.IDLE:
            if self._bargein_mode:
                # Periodic Silero model reset to prevent LSTM drift during
                # long TTS playback.  Without this, the model processes
                # silence/echo for seconds and its hidden state drifts so
                # far toward "not speech" that real speech can't trigger it.
                self._bargein_chunks_since_reset += 1
                if self._bargein_chunks_since_reset >= self._BARGEIN_RESET_INTERVAL:
                    self._bargein_chunks_since_reset = 0
                    from silero_vad_lite import SileroVAD
                    self._model = SileroVAD(SAMPLE_RATE)
                    # Re-run this chunk on the fresh model
                    probability = self._model.process(float32_audio)
                    is_speech = probability >= active_threshold and energy_ok
                    logger.debug("VAD: periodic Silero reset during bargein")

                # Track consecutive chunks at normal threshold for early STT unmute
                early_speech = probability >= self.threshold and energy_ok
                if early_speech:
                    self._early_speech_count += 1
                else:
                    self._early_speech_count = 0

                # Sliding window: require a ratio of speech chunks in the
                # debounce window.  Tolerates natural volume dips / micro-pauses
                # while still rejecting short impulse noises (bangs, TV effects).
                self._chunk_window.append(is_speech)
                while len(self._chunk_window) > active_debounce:
                    self._chunk_window.popleft()

                # Check barge-in confirmation first (higher priority)
                if len(self._chunk_window) >= active_debounce:
                    speech_ratio = sum(self._chunk_window) / len(self._chunk_window)
                    if speech_ratio >= self._bargein_chunk_ratio:
                        self.state = VadState.SPEAKING
                        self._chunk_window.clear()
                        self._speech_chunks = 0
                        self._silence_start_ms = None
                        self._early_speech_count = 0
                        self._probable_fired = False
                        logger.info(
                            f"VAD: SPEECH_START bargein "
                            f"(prob={probability:.2f}, ratio={speech_ratio:.2f})"
                        )
                        return VadEvent.SPEECH_START

                # Early detection: fire SPEECH_PROBABLE once after 1 chunk
                # at normal threshold (~32ms).  Pipeline uses this to unmute
                # STT early so Deepgram captures the beginning of speech.
                # Safe at 1 chunk: SPEECH_PROBABLE only unmutes STT, it does
                # NOT cancel TTS — the sliding window handles that.
                if self._early_speech_count >= 1 and not self._probable_fired:
                    self._probable_fired = True
                    logger.info(
                        f"VAD: SPEECH_PROBABLE "
                        f"(prob={probability:.2f}, rms={rms:.0f})"
                    )
                    return VadEvent.SPEECH_PROBABLE
            else:
                # Normal mode: strict consecutive chunks
                if is_speech:
                    self._speech_chunks += 1
                    if self._speech_chunks >= active_debounce:
                        self.state = VadState.SPEAKING
                        self._speech_chunks = 0
                        self._silence_start_ms = None
                        logger.debug(
                            f"VAD: SPEECH_START (prob={probability:.2f})"
                        )
                        return VadEvent.SPEECH_START
                else:
                    self._speech_chunks = 0

        elif self.state == VadState.SPEAKING:
            if is_speech:
                self._silence_start_ms = None
            else:
                if self._silence_start_ms is None:
                    self._silence_start_ms = now_ms
                else:
                    # Use longer silence threshold during barge-in to prevent
                    # premature SPEECH_END from natural mid-sentence pauses.
                    active_silence_ms = self._bargein_silence_ms if self._bargein_mode else self.silence_duration_ms
                    if now_ms - self._silence_start_ms >= active_silence_ms:
                        self.state = VadState.IDLE
                        self._silence_start_ms = None
                        self._speech_chunks = 0
                        # Reset Silero internal state so next speech detection
                        # starts fresh — prevents LSTM hidden state from getting
                        # stuck on "not speech" after processing silence.
                        from silero_vad_lite import SileroVAD
                        self._model = SileroVAD(SAMPLE_RATE)
                        logger.debug(f"VAD: SPEECH_END (silence={active_silence_ms}ms)")
                        return VadEvent.SPEECH_END

        return VadEvent.NONE

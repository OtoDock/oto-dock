"""Silero VAD wrapper with IDLE/SPEAKING state machine.

Runs the bundled Silero model on ONNX Runtime (``silero_model.py``; no torch)
for CPU-only inference. Buffers incoming PCM into Silero's 32 ms analysis
window (256 samples at 8 kHz, 512 at 16 kHz — the model derives it from the
sample rate).

Supports two sensitivity modes:
  - Normal: standard threshold + short debounce (listening phase)
  - Barge-in: higher threshold + longer debounce (during TTS playback)
    to avoid false triggers from echo/noise

All chunk-count debounce math is in 32 ms units, which holds at every
supported rate (the window is always 32 ms), so only the byte math is
rate-dependent.
"""

from __future__ import annotations

import array
import bisect
import collections
import logging
import struct
import threading
import time
from typing import NamedTuple

from audio.constants import SAMPLE_RATE, SAMPLE_WIDTH
from audio.providers.vad.base import VadEvent, VadState
from audio.providers.vad.silero_model import SileroModel

logger = logging.getLogger(__name__)

#: The windows whose scores a VAD keeps for a reader (``SileroVad.scored``):
#: twelve seconds of 32 ms windows.
_SCORES_KEPT = 375


class Scored(NamedTuple):
    """What a VAD scored over a time span. Numbers only."""

    windows: int        # the windows scored in the span
    over: int           # at or over the probability threshold in force
    speech: int         # of those, at or over the energy floor: what the state machine counts
    longest_run: int    # the longest run of counted windows in a row
    needed_run: int     # the run a speech start needs while listening
    peak: float         # the highest probability
    bargein: int        # the windows scored in barge-in mode


#: The edges of the loudness bands a VAD counts its windows in
#: (``SileroVad.by_loudness``), as RMS: under 75, 75 to 150, 150 to 300, 300
#: to 600, 600 to 1,200, above. Fixed, so two calls compare whatever the
#: energy floor was on each.
LOUDNESS_EDGES = (75, 150, 300, 600, 1200)


class Loudness(NamedTuple):
    """A VAD's windows by loudness. Per band of ``LOUDNESS_EDGES``: the
    windows scored, and those of them at or over the probability threshold in
    force. Numbers only."""

    listening: tuple[tuple[int, int], ...]
    bargein: tuple[tuple[int, int], ...]


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
        sample_rate: int = SAMPLE_RATE,
    ):
        self._sample_rate = sample_rate
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

        # The last windows' scores, for a reader that asks what the detector
        # saw (``scored``): (time, probability, over the threshold, counted as
        # speech, barge-in mode). The feeding thread writes, a reader copies.
        self._scores: collections.deque[tuple[float, float, bool, bool, bool]] = collections.deque(
            maxlen=_SCORES_KEPT)
        self._scores_lock = threading.Lock()
        # Every window of the call by loudness (``by_loudness``): per mode
        # (barge-in or not) and band, [windows, those over the threshold].
        self._bands: dict[bool, list[list[int]]] = {
            mode: [[0, 0] for _ in range(len(LOUDNESS_EDGES) + 1)] for mode in (False, True)}

        # One ONNX session per instance; the analysis window (32 ms) is derived
        # from the sample rate by the model itself. Every "fresh model" reset
        # below zeroes its recurrent state and its kept context, which are
        # the whole of its memory.
        self._model = SileroModel(self._sample_rate)
        self._chunk_bytes = self._model.window_size_samples * SAMPLE_WIDTH
        logger.info(
            "Silero VAD loaded (threshold=%s, "
            "bargein_threshold=%s, "
            "silence=%sms, "
            "bargein_silence=%sms, "
            "bargein_debounce=%s chunks, "
            "bargein_ratio=%s, "
            "min_energy_rms=%s, "
            "start_debounce=%s chunks, "
            "rate=%s)",
            threshold, self._bargein_threshold, silence_duration_ms, self._bargein_silence_ms,
            self._bargein_debounce_chunks, self._bargein_chunk_ratio, f"{min_energy_rms:g}",
            self._normal_debounce_chunks, self._sample_rate
        )

    def _reset_model(self) -> None:
        """Fresh Silero state at this instance's rate (LSTM state reset)."""
        self._model.reset()

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
            self._reset_model()
            logger.debug("VAD: Silero state reset (bargein=%s)", 'on' if enabled else 'off')

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
        self._reset_model()
        with self._scores_lock:
            self._scores.clear()
            for bands in self._bands.values():
                for counts in bands:
                    counts[:] = [0, 0]

    def _keep_score(self, now_ms: float, probability: float, threshold: float, is_speech: bool,
                    rms: float, *, again: bool = False) -> None:
        """One window's score into the ring and into its loudness band.
        ``again``: the same window, scored once more on a fresh model,
        replaces its first score and is counted once, with its second
        verdict."""
        passed = probability >= threshold
        entry = (now_ms / 1000.0, probability, passed, is_speech, self._bargein_mode)
        counts = self._bands[self._bargein_mode][bisect.bisect_right(LOUDNESS_EDGES, rms)]
        with self._scores_lock:
            if again and self._scores:
                counts[1] += passed - self._scores[-1][2]
                self._scores[-1] = entry
            else:
                counts[0] += 1
                counts[1] += passed
                self._scores.append(entry)

    def by_loudness(self) -> Loudness:
        """This VAD's windows by loudness since its last ``reset()``, while
        listening and in barge-in mode. A read: it changes nothing, and it
        may come from another thread than the one that feeds the VAD."""
        with self._scores_lock:
            return Loudness(*(tuple((windows, over) for windows, over in self._bands[mode])
                              for mode in (False, True)))

    def scored(self, since: float, until: float) -> Scored:
        """What this VAD scored between two ``time.monotonic()`` stamps, of
        the last ``_SCORES_KEPT`` windows. A read: it changes nothing, and it
        may come from another thread than the one that feeds the VAD."""
        with self._scores_lock:
            kept = list(self._scores)
        windows = over = speech = run = longest = bargein = 0
        peak = 0.0
        for at, probability, passed, counted, mode in kept:
            if not since <= at <= until:
                continue
            windows += 1
            over += passed
            speech += counted
            bargein += mode
            peak = max(peak, probability)
            run = run + 1 if counted else 0
            longest = max(longest, run)
        return Scored(windows, over, speech, longest, self._normal_debounce_chunks, peak, bargein)

    def process(self, audio_bytes: bytes) -> VadEvent:
        """Feed raw PCM bytes and return any state transition event."""
        self._buffer.extend(audio_bytes)

        event = VadEvent.NONE
        while len(self._buffer) >= self._chunk_bytes:
            chunk = bytes(self._buffer[:self._chunk_bytes])
            del self._buffer[:self._chunk_bytes]
            event = self._process_chunk(chunk)
            if event != VadEvent.NONE:
                return event

        return event

    def _process_chunk(self, chunk: bytes) -> VadEvent:
        """Run VAD on a single 32 ms window chunk."""
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
        self._keep_score(now_ms, probability, active_threshold, is_speech, rms)

        # Diagnostic: log near-miss detections in IDLE mode
        if self.state == VadState.IDLE and not is_speech:
            if self._bargein_mode and (probability >= 0.3 or rms >= 400):
                logger.debug(
                    "VAD: near-miss prob=%.2f rms=%.0f "
                    "(thresh=%s min_rms=%s)",
                    probability, rms, active_threshold, self._min_energy_rms
                )
            elif not self._bargein_mode and (probability >= 0.3 or rms >= 200):
                logger.debug(
                    "VAD: near-miss prob=%.2f rms=%.0f "
                    "(thresh=%s min_rms=%s)",
                    probability, rms, active_threshold, self._min_energy_rms
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
                    self._reset_model()
                    # Re-run this chunk on the fresh model
                    probability = self._model.process(float32_audio)
                    is_speech = probability >= active_threshold and energy_ok
                    self._keep_score(now_ms, probability, active_threshold, is_speech, rms, again=True)
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
                            "VAD: SPEECH_START bargein "
                            "(prob=%.2f, ratio=%.2f)",
                            probability, speech_ratio
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
                        "VAD: SPEECH_PROBABLE "
                        "(prob=%.2f, rms=%.0f)", probability, rms
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
                            "VAD: SPEECH_START (prob=%.2f)", probability
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
                        self._reset_model()
                        logger.debug("VAD: SPEECH_END (silence=%sms)", active_silence_ms)
                        return VadEvent.SPEECH_END

        return VadEvent.NONE

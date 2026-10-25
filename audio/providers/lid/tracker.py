"""The driver between a consumer's audio and the rule.

The consumer feeds the bytes that go to the ACTIVE STT stream live (so the
agent's own voice, fed as silence during playback, never reaches the
identifier) and says which frames are voiced (the VAD's SPEAKING and
PROBABLE frames on calls and live voice, the RMS floor on dictation). The
tracker keeps the last ``WINDOW_S`` of voiced audio at 16 kHz (an 8 kHz
line is upsampled by a half-band FIR), runs the identifier in a worker
thread once ``MIN_VOICED_S`` of voice is in and every ``STEP_VOICED_S``
after, stamps each run with the voiced seconds it covers, drops a result
that lands more than half a window behind the feed (a loaded worker never
applies a stale reading, a slow one still gets its readings in), and queues
the rule's events; the consumer polls them
between frames and applies them on the loop. ``take`` asks for one fresh
reading (bounded) and the rule's take verdict. Every applied reading is one
INFO line, numbers only: the top two languages, the model's time and how
far the reading trails the feed (a reading every half second of voice until
the final, at most ``MAX_VOICED_S`` of it).

One identifier worker per process (``_executor``): at this box's speed a
3 s window costs about 0.4 s, so one worker keeps up with about two calls
deciding at once; on AVX2 hardware several times that. A run that waited in
the worker's queue until it was stale is skipped before the model runs, so
a busy worker spends no time on readings nobody applies.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from audio.providers.lid.base import LanguageIdentifier
from audio.providers.lid.rule import (
    MIN_VOICED_S,
    MIN_WORD_S,
    STEP_VOICED_S,
    WINDOW_S,
    Event,
    LanguageRule,
)

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lang-id")

RATE = 16000
#: Audio seconds after the first voice at which the window closes with the
#: current language final: the replay bound, and the end of billing a
#: candidate stream for a person who never speaks clearly.
WINDOW_MAX_S = 20.0
#: How long ``take`` waits for a reading.
TAKE_WAIT_S = 0.3
#: A result that lands this much voice behind the feed is dropped: on a slow
#: CPU a run takes longer than a step, and the window still covers it, so
#: the bound is half a window, not a step.
STALE_S = WINDOW_S / 2
_FIR_TAPS = 63
#: The model labels two languages by their old codes: a route or an install
#: list that names the current one reads the old label.
_LABEL_ALIASES = {"he": "iw", "jv": "jw"}


def _half_band() -> np.ndarray:
    n = np.arange(_FIR_TAPS) - (_FIR_TAPS - 1) / 2
    return (np.sinc(n / 2.0) / 2.0 * np.blackman(_FIR_TAPS) * 2.0).astype(np.float32)


class Upsampler2x:
    """8 kHz to 16 kHz, streaming: the half-band FIR as two polyphase
    branches over the 8 kHz samples (the branch holding the centre tap
    passes the input through, the other interpolates between neighbours),
    with the filter's tail kept between calls so chunk edges are seamless.
    The output lags the input by 16 samples (2 ms)."""

    def __init__(self) -> None:
        h = _half_band()
        self._through = h[1::2]      # 31 taps, the centre tap among them
        self._between = h[0::2]      # 32 taps, the half-sample interpolator
        self._state = np.zeros(len(self._between) - 1, dtype=np.float32)

    def process(self, x8: np.ndarray) -> np.ndarray:
        if not len(x8):
            return x8
        x = np.concatenate([self._state, x8.astype(np.float32)])
        through = np.convolve(x, self._through, mode="valid")
        between = np.convolve(x, self._between, mode="valid")
        n = min(len(through), len(between), len(x8))
        out = np.empty(2 * n, dtype=np.float32)
        out[0::2] = through[:n]
        out[1::2] = between[:n]
        self._state = x[-(len(self._between) - 1):]
        return out


class LanguageTracker:
    """One consumer's identifier runs and their events."""

    def __init__(self, identifier: LanguageIdentifier, rule: LanguageRule, *, rate_in: int,
                 executor: ThreadPoolExecutor | None = None) -> None:
        if rate_in not in (8000, RATE):
            raise ValueError(f"the language tracker takes 8 or 16 kHz audio, not {rate_in} Hz")
        self._identifier = identifier
        self.rule = rule
        self._up = Upsampler2x() if rate_in == 8000 else None
        self._executor = executor or _executor
        # The last window of voiced audio as a ring: written per frame, read
        # in order once per run (a fresh array per 20 ms frame churned).
        self._window = int(WINDOW_S * RATE)
        self._ring = np.zeros(self._window, dtype=np.float32)
        self._ring_at = 0
        self._ring_filled = 0
        self.voiced_s = 0.0
        self._audio_since_voice_s: float | None = None
        self._next_run_at = MIN_VOICED_S
        self._last_read_at = -1.0
        self._task: asyncio.Task | None = None
        self._events: deque[Event] = deque()
        self.closed = False
        self.failed = False
        self.runs = 0
        self.dropped = 0
        self._index = {code: i for i, code in enumerate(identifier.labels)}
        for code, label in _LABEL_ALIASES.items():
            if label in self._index and code not in self._index:
                self._index[code] = self._index[label]
        missing = [lang for lang in rule.languages if lang not in self._index]
        if missing:
            raise ValueError(f"the identifier knows no {', '.join(missing)}")

    # ── Feeding ────────────────────────────────────────────────────

    def feed(self, pcm16: bytes, *, voiced: bool) -> None:
        if self.closed or self.rule.done or not pcm16:
            return
        x = np.frombuffer(pcm16[: len(pcm16) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
        if self._up is not None:
            x = self._up.process(x)
        if self._audio_since_voice_s is None:
            if not voiced:
                return
            self._audio_since_voice_s = 0.0
        self._audio_since_voice_s += len(x) / RATE
        if voiced:
            self._ring_write(x)
            self.voiced_s += len(x) / RATE
            if self.voiced_s >= self._next_run_at and (self._task is None or self._task.done()):
                self._schedule()
        if self._audio_since_voice_s >= WINDOW_MAX_S:
            self._events.extend(self.rule.expire())

    def _ring_write(self, x: np.ndarray) -> None:
        if len(x) >= self._window:
            self._ring[:] = x[-self._window:]
            self._ring_at = 0
            self._ring_filled = self._window
            return
        end = self._ring_at + len(x)
        if end <= self._window:
            self._ring[self._ring_at:end] = x
        else:
            first = self._window - self._ring_at
            self._ring[self._ring_at:] = x[:first]
            self._ring[: len(x) - first] = x[first:]
        self._ring_at = end % self._window
        self._ring_filled = min(self._window, self._ring_filled + len(x))

    def _snapshot(self) -> np.ndarray:
        """The window's voiced audio in order, oldest first, a copy."""
        if self._ring_filled < self._window:
            return self._ring[: self._ring_filled].copy()
        return np.concatenate([self._ring[self._ring_at:], self._ring[: self._ring_at]])

    def _schedule(self) -> None:
        if self.failed:
            return
        at = self.voiced_s
        self._next_run_at = at + STEP_VOICED_S
        self._task = asyncio.get_running_loop().create_task(self._run(self._snapshot(), at))

    def _stale(self, at: float) -> bool:
        return self.voiced_s - at > STALE_S

    def _job(self, snapshot: np.ndarray, at: float) -> np.ndarray | None:
        """The worker's side: a run that waited in the queue until it is
        stale (or until the tracker closed) is skipped before the model
        runs; ``voiced_s`` is one float read across threads."""
        if self.closed or self._stale(at):
            return None
        return self._identifier.scores(snapshot)

    async def _run(self, snapshot: np.ndarray, at: float) -> dict[str, float] | None:
        loop = asyncio.get_running_loop()
        t0 = time.monotonic()
        try:
            scores = await loop.run_in_executor(self._executor, self._job, snapshot, at)
        except Exception as e:  # noqa: BLE001  # whatever the model raises, the surface runs on without readings
            if not self.failed:
                logger.warning("language identification failed, no more readings: %s", e)
            self.failed = True
            return None
        if self.closed:
            return None
        took = time.monotonic() - t0
        if scores is None or self._stale(at):
            self.dropped += 1
            logger.debug("language reading dropped: %.1f s of voice behind (%.0f ms, %s)", self.voiced_s - at,
                         took * 1000, "skipped" if scores is None else "computed")
            return None
        self.runs += 1
        return self._apply(scores, at, took)

    def _apply(self, scores: np.ndarray, at: float, took: float = 0.0) -> dict[str, float]:
        by_lang = {lang: float(scores[self._index[lang]]) for lang in self.rule.languages}
        self._last_read_at = at
        read = not self.rule.done
        self._events.extend(self.rule.reading(by_lang, at))
        latest = self.rule.latest
        if read and latest:
            (first, p1), (second, p2) = (sorted(latest.items(), key=lambda kv: -kv[1]) + [("-", 0.0)])[:2]
            logger.info("language reading at %.1f s voiced: %s %.2f, %s %.2f (%.0f ms, %.1f s behind)",
                        at, first, p1, second, p2, took * 1000, self.voiced_s - at)
        return by_lang

    # ── The consumer's side ────────────────────────────────────────

    def poll(self) -> list[Event]:
        """The events since the last poll, in order; the consumer applies
        them on the loop."""
        events = list(self._events)
        self._events.clear()
        return events

    async def take(self) -> list[Event]:
        """A turn was taken: one fresh reading if voice arrived since the
        last one (bounded at ``TAKE_WAIT_S``), then the rule's take verdict,
        with every event queued meanwhile."""
        if self.closed or self.rule.done:
            return self.poll()
        if self._task is not None and not self._task.done():
            await self._await_reading()
        elif self.voiced_s >= MIN_WORD_S and self.voiced_s > self._last_read_at and not self.failed:
            self._next_run_at = self.voiced_s + STEP_VOICED_S
            self._task = asyncio.get_running_loop().create_task(self._run(self._snapshot(), self.voiced_s))
            await self._await_reading()
        if not self.closed:
            self._events.extend(self.rule.take(self.voiced_s, self.rule.latest))
        return self.poll()

    async def _await_reading(self) -> None:
        """The in-flight run, bounded at ``TAKE_WAIT_S``. The run's own end
        (its cancel at ``close``, its failure, the bound) is this tracker's
        business and the take goes on with what it has; a cancel aimed at
        the task that takes (the turn timer ended by new speech, the call's
        teardown) is not, and goes through."""
        try:
            await asyncio.wait_for(asyncio.shield(self._task), TAKE_WAIT_S)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
        except Exception:  # noqa: BLE001  # the bound, or a run that failed (recorded by _run)
            pass

    def freeze(self) -> None:
        self.rule.freeze()

    def close(self) -> None:
        self.closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

"""The second listen: the speech a live window's final left without words
goes through the vendor's batch endpoint. In dictation the window's text
waits for it (bounded) so the words land once, in the order spoken; on a
call the turn's take waits for it (bounded) and the words join the turn.

A live stream answers in windows of a few seconds. On continuous Greek,
Deepgram's nova-3 answers some windows EMPTY (interims and final alike) or
keeps only a window's tail; measured on the same audio: 7 of 15 windows of
fast speech, 3 of 32 of read speech, 1 of 7 of the operator's own voice,
none of English; at 8 kHz on a call, 7 of 30 finals of fast speech, 15 of
71 of read speech. The batch endpoint hears the same audio: one cut misses
spans another cut of a different length finds, so a gap's cuts go out one
at a time and the first with words in the gap wins.

The pieces, all owned by one live stream:

- ``AudioRing``: the PCM the stream's connection accepted, on that
  connection's clock (its first byte at 0), the last ``RELISTEN_KEEP_S``.
- ``uncovered``: the stretches of a window no committed word covers.
- ``SpeechCheck``: the bundled Silero model over a stretch; a stretch is a
  gap only with ``RELISTEN_MIN_SPEECH_S`` of speech in it (a loudness floor
  read train noise and breaths as speech on English, Silero none of them).
- ``Relisten``: every final passes its FIFO; a window with a gap is held
  until its re-listen answered or ``RELISTEN_HOLD_MAX_S`` passed, and
  windows after it wait behind it. Its held text (the words the person was
  shown in the gap included) is what the provider keeps on screen
  meanwhile; those shown words are the fallback when the re-listen finds
  nothing.
- ``TurnRelisten``: a call's, narrower. Only a final with no word at all
  over speech is listened to again, at once and in the background; the
  turn's take waits for what is still out (bounded) and gets the words
  spliced into the turn's text by time.

Recovered words never enter the provider's ``heard`` ledger: the language
decision reads the live words only.
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
import wave
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import numpy as np

from audio.providers.stt.base import TranscriptResult, Word

logger = logging.getLogger(__name__)

#: The audio a stream keeps for a re-listen, in seconds of its connection.
RELISTEN_KEEP_S = 30.0
#: A stretch is a gap with this much speech in it (Silero at
#: ``SPEECH_PROBABILITY`` per 32 ms window, with ``SPEECH_CONTEXT_S`` of the
#: audio before it as the model's context).
RELISTEN_MIN_SPEECH_S = 0.4
SPEECH_PROBABILITY = 0.5
SPEECH_CONTEXT_S = 0.3
#: Before the model runs, a stretch must be loud enough for that much speech
#: (50 ms blocks at this int16 RMS, the dictation relay's voiced floor): a
#: quiet edge of a window is released at once, on the loop.
LOUD_RMS = 300
#: Committed words cover a window up to this far past their edges, and a
#: stretch shorter than ``STRETCH_MIN_S`` is never a gap.
COVER_TOLERANCE_S = 0.15
STRETCH_MIN_S = 0.3
#: The cuts of a gap, as the audio taken before and after it, in order: the
#: next only when the last heard no word in the gap (each misses gaps
#: another finds; measured on the Greek captures).
RELISTEN_CUTS_S = ((0.5, 0.5), (1.5, 1.5), (0.3, 2.5))
#: A recovered word belongs to the gap when it ends past the gap's start and
#: starts before its end (this tolerance after it, never past the window).
#: The batch endpoint stretches a cut's first word back to the cut's start
#: and its last word on past the cut's end: those two are cut down to
#: ``EDGE_WORD_S`` from their reliable side, any other to ``WORD_MAX_S``. A
#: committed word of the same form this close is the same word (batch and
#: live timings drift by 0.1 to 0.2 s); a recovered word overlapping a word
#: the live stream committed is that word (timings of two batch cuts are
#: too loose to judge one against the other: only the form check applies).
EDGE_TOLERANCE_S = 0.25
EDGE_WORD_S = 0.5
WORD_MAX_S = 1.2
DUPLICATE_WITHIN_S = 0.35
#: Each cut's bound, the held window's bound from its arrival, the cuts one
#: stream runs at once (the others queue), and the finish's default wait
#: (the dictation relay hands it what is left of its ending's budget). The
#: batch endpoint answers a cut in 1 to 2 s, slower under load: four
#: dictations at once saw a cut pass 4 s.
RELISTEN_TIMEOUT_S = 6.0
RELISTEN_HOLD_MAX_S = 10.0
RELISTEN_MAX_ACTIVE = 3
RELISTEN_FINISH_WAIT_S = 5.0
#: The committed words kept for the duplicate guard, in seconds.
RECENT_KEEP_S = 10.0
#: A call's turn waits at most this long for its second listen (the take's
#: own waits count against it): a turn with no empty window waits nothing.
TURN_RELISTEN_WAIT_S = 2.0
#: Two windows this close are one seam: a recovered piece's head repeating
#: the piece before it is that piece's tail heard again.
SEAM_S = 0.3
#: A dictation's batch seconds may reach this share of its stream seconds so
#: far plus the extra: beyond it a window goes out as the live stream left
#: it. Measured on fast Greek with half its windows answered empty, the
#: second listen bought 1.03 times the stream's seconds and its spend peaked
#: at about 1.15 times the stream so far plus the extra; read speech and the
#: operator's own voice stayed near a fifth.
RELISTEN_BUDGET_RATIO = 1.25
RELISTEN_BUDGET_EXTRA_S = 10.0


class AudioRing:
    """The PCM a connection accepted, on that connection's clock."""

    def __init__(self, rate: int = 16000, keep_s: float = RELISTEN_KEEP_S) -> None:
        self._keep_s = keep_s
        self.reset(rate)

    def reset(self, rate: int) -> None:
        self.rate = int(rate)
        self._buf = bytearray()
        self._start_s = 0.0

    @property
    def end_s(self) -> float:
        return self._start_s + len(self._buf) / (self.rate * 2)

    def append(self, pcm: bytes) -> None:
        self._buf.extend(pcm)
        excess = len(self._buf) - int(self._keep_s * self.rate) * 2
        if excess > 0:
            excess += excess % 2
            del self._buf[:excess]
            self._start_s += excess / (self.rate * 2)

    def _bounds(self, a: float, b: float) -> tuple[int, int]:
        lo = max(0, int((a - self._start_s) * self.rate)) * 2
        hi = min(len(self._buf), max(0, int((b - self._start_s) * self.rate)) * 2)
        return lo, max(lo, hi)

    def samples(self, a: float, b: float) -> tuple[np.ndarray, float]:
        """A copy of the kept samples of ``[a, b)`` and where they start."""
        lo, hi = self._bounds(a, b)
        return np.frombuffer(bytes(self._buf[lo:hi]), dtype=np.int16).copy(), self._start_s + lo / (self.rate * 2)

    def wav(self, a: float, b: float) -> tuple[bytes, float, float]:
        """``[a, b)`` as a WAV file (the batch endpoint reads a container),
        where it starts and how long it is."""
        lo, hi = self._bounds(a, b)
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes(bytes(self._buf[lo:hi]))
        return out.getvalue(), self._start_s + lo / (self.rate * 2), (hi - lo) / (self.rate * 2)


def loud_seconds(samples: np.ndarray, rate: int) -> float:
    """The seconds of 50 ms blocks at ``LOUD_RMS`` or more."""
    block = rate // 20
    n = len(samples) // block
    if not n:
        return 0.0
    blocks = samples[:n * block].astype(np.float32).reshape(n, block)
    return float(np.sum(np.sqrt(np.mean(blocks * blocks, axis=1)) >= LOUD_RMS)) * 0.05


def uncovered(start: float, end: float, covered: list[Word]) -> list[tuple[float, float]]:
    """The stretches of ``[start, end)`` no word covers."""
    spans: list[tuple[float, float]] = []
    t = start
    for w in sorted(covered, key=lambda w: w.start):
        if w.end <= start or w.start >= end:
            continue
        if w.start - COVER_TOLERANCE_S > t:
            spans.append((t, min(w.start, end)))
        t = max(t, w.end)
    if t + COVER_TOLERANCE_S < end:
        spans.append((t, end))
    return [(a, b) for a, b in spans if b - a >= STRETCH_MIN_S]


# One Silero model per rate for the whole process, loaded on first use: every
# call resets its state and runs a whole stretch under the lock, so the
# streams share it. A rate whose load failed is remembered (one warning).
_MODELS: dict[int, object] = {}
_FAILED: set[int] = set()
_MODELS_LOCK = threading.Lock()


def _upsampled(samples: np.ndarray) -> np.ndarray:
    """8 kHz to 16 kHz by linear interpolation."""
    x = samples.astype(np.float32)
    out = np.empty(len(x) * 2, dtype=np.float32)
    out[0::2] = x
    out[1::2] = np.append((x[:-1] + x[1:]) / 2, x[-1:]) if len(x) else x
    return out


class SpeechCheck:
    """The seconds of speech in a stretch, by the bundled Silero model, in a
    worker thread (the model carries state between windows: the calls take
    turns). ``model_factory`` gives a check its own model (the tests).

    At 8 kHz the model runs on the audio upsampled to 16 kHz: fed one 32 ms
    window at a time with no context (the wrapper's feed, the phone VAD's),
    Silero's 8 kHz head heard 9 % of a speaker's clear speech, its 16 kHz
    head 90 % of the same audio upsampled, and silence 0 % either way."""

    def __init__(self, rate: int, model_factory: Callable[[int], object] | None = None) -> None:
        self.rate = rate
        self._model_rate = 16000 if rate == 8000 else rate
        self._factory = model_factory
        self._model = None
        self._lock = threading.Lock() if model_factory is not None else _MODELS_LOCK

    @property
    def usable(self) -> bool:
        return self.rate in (8000, 16000) and (self._factory is not None or self._model_rate not in _FAILED)

    def _load(self):
        if self._model is None:
            if self._factory is not None:
                self._model = self._factory(self._model_rate)
            else:
                if self._model_rate not in _MODELS:
                    from audio.providers.vad.silero_model import SileroModel
                    _MODELS[self._model_rate] = SileroModel(self._model_rate)
                self._model = _MODELS[self._model_rate]
        return self._model

    def seconds(self, context: np.ndarray, stretch: np.ndarray) -> float:
        if self._model_rate != self.rate:
            context, stretch = _upsampled(context), _upsampled(stretch)
        with self._lock:
            try:
                model = self._load()
            except Exception as e:  # noqa: BLE001  # onnxruntime or a missing model file: the second listen stands down
                if self._model_rate not in _FAILED:
                    _FAILED.add(self._model_rate)
                    logger.warning("second listen off: the speech model did not load: %s", e)
                return 0.0
            model.reset()
            size = model.window_size_samples
            audio = np.concatenate([context, stretch]).astype(np.float32) / 32768.0
            first = len(context)
            speech = 0
            for i in range(0, len(audio) - size + 1, size):
                if model.process(audio[i:i + size]) >= SPEECH_PROBABILITY and i + size > first:
                    speech += size
            return speech / self._model_rate


@dataclass
class _Held:
    """One window in the FIFO."""

    text: str
    words: list[Word]
    shown: list[Word]
    window_end: float
    arrived: float
    done: bool = False
    out_text: str = ""
    out_words: list[Word] = field(default_factory=list)
    #: The words each gap brought back so far (kept when the window runs
    #: out of time with another gap still out).
    recovered: list[Word] = field(default_factory=list)
    relistened: bool = False
    task: asyncio.Task | None = None
    timer: asyncio.TimerHandle | None = None


def _mid(w: Word) -> float:
    return (w.start + w.end) / 2


def _overlap(a: Word, b: Word) -> float:
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


class _SecondListen:
    """What both second listens share: the ring, the speech check, the
    budget and the cuts of one live stream."""

    #: The surface the log lines name.
    _surface = ""

    def __init__(self, *, transcribe: Callable[[bytes], Awaitable[TranscriptResult]],
                 norm: Callable[[str], str], rate: int = 16000,
                 speech: SpeechCheck | None = None, label: str = "",
                 stream_seconds: Callable[[], float] | None = None,
                 spent: Callable[[], float] | None = None) -> None:
        self._transcribe = transcribe
        self._norm = norm
        self.ring = AudioRing(rate)
        self._speech = speech or SpeechCheck(rate)
        self._sem: asyncio.Semaphore | None = None
        self.seconds = 0.0
        self.label = label
        self._stream_seconds = stream_seconds
        self._spent = spent
        self._over_budget = False
        #: The stream is closed: a final arriving now (the vendor's last
        #: results during the close) is released at once, never listened to.
        self.closed = False

    def _reset_audio(self, rate: int) -> None:
        self.closed = False
        self.ring.reset(rate)
        if self._speech.rate != rate:
            self._speech = SpeechCheck(rate)

    @property
    def keeps_audio(self) -> bool:
        """Whether the ring is worth filling (the speech check can run)."""
        return self._speech.usable and not self.closed

    def _budget_left(self) -> float:
        if self._stream_seconds is None:
            return float("inf")
        spent = self._spent() if self._spent is not None else self.seconds
        return self._stream_seconds() * RELISTEN_BUDGET_RATIO + RELISTEN_BUDGET_EXTRA_S - spent

    def _note_budget(self) -> None:
        if not self._over_budget:
            self._over_budget = True
            logger.info("%s second listen (%s): its budget is spent (%.1f s), later windows go as "
                        "the live stream left them", self._surface, self.label, self.seconds)

    def _gaps(self, heard: list[tuple[float, float, np.ndarray, np.ndarray]]) -> list[tuple[float, float, float]]:
        """The stretches with enough speech in them, and that speech (runs in
        a worker thread, on copies of the audio)."""
        out = []
        for a, b, context, stretch in heard:
            speech = self._speech.seconds(context, stretch)
            if speech >= RELISTEN_MIN_SPEECH_S:
                out.append((a, b, speech))
        return out

    async def _cut(self, a: float, b: float, cut: tuple[float, float]) -> list[Word]:
        """One cut of the gap ``[a, b)`` through the batch endpoint, its words
        on the connection's clock with the edge words trimmed."""
        if self._sem is None:
            self._sem = asyncio.Semaphore(RELISTEN_MAX_ACTIVE)
        async with self._sem:
            audio, at, seconds = self.ring.wav(a - cut[0], b + cut[1])
            if seconds <= 0 or self.closed:
                return []
            if seconds > self._budget_left():
                self._note_budget()
                return []
            self.seconds += seconds
            result = await asyncio.wait_for(self._transcribe(audio), RELISTEN_TIMEOUT_S)
        words = list(result.words or [])
        out = []
        for i, w in enumerate(words):
            start, end = at + w.start, at + w.end
            if i == 0:
                start = max(start, end - EDGE_WORD_S)
            if i == len(words) - 1:
                end = min(end, start + EDGE_WORD_S)
            end = min(end, start + WORD_MAX_S)
            out.append(Word(word=w.word, start=start, end=end, confidence=w.confidence))
        return out

    async def _cuts(self, a: float, b: float,
                    keep: Callable[[list[Word]], list[Word]]) -> tuple[list[Word], tuple[float, float]]:
        """The gap's cuts in order; the first with words ``keep`` keeps wins.
        One cut at a time, the next only when the last heard no word in the
        gap: the first finds most gaps, and sending the cuts together spent
        about as many batch seconds as the stream had, past the budget."""
        for cut in RELISTEN_CUTS_S:
            if self.closed:
                break
            try:
                words = await self._cut(a, b, cut)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001  # one cut failing leaves the next
                logger.info("%s re-listen cut %s failed: %s", self._surface, cut, type(e).__name__)
                continue
            kept = keep(words)
            if kept:
                return kept, cut
        return [], (0.0, 0.0)


class Relisten(_SecondListen):
    """The FIFO, the re-listens and their bounds for one live stream."""

    _surface = "dictation"

    def __init__(self, *, transcribe: Callable[[bytes], Awaitable[TranscriptResult]],
                 release: Callable[[str], None], norm: Callable[[str], str], rate: int = 16000,
                 speech: SpeechCheck | None = None, label: str = "",
                 stream_seconds: Callable[[], float] | None = None,
                 spent: Callable[[], float] | None = None) -> None:
        super().__init__(transcribe=transcribe, norm=norm, rate=rate, speech=speech, label=label,
                         stream_seconds=stream_seconds, spent=spent)
        self._on_release = release
        self._fifo: deque[_Held] = deque()
        self._recent: deque[Word] = deque()
        self._recent_live: deque[Word] = deque()
        self._last_word: Word | None = None
        self._last_relistened = False

    # ── Lifecycle ──────────────────────────────────────────────────

    def reset(self, rate: int) -> None:
        """A new connection: its clock starts at zero; what was held goes."""
        self.cancel()
        self._reset_audio(rate)
        self._recent.clear()
        self._recent_live.clear()
        self._last_word = None

    def cancel(self) -> None:
        """Every re-listen ends; the held windows are dropped."""
        for entry in self._fifo:
            self._stop(entry)
        self._fifo.clear()

    def close(self) -> None:
        """The stream is closing: what is held is dropped and nothing new is
        listened to again."""
        self.closed = True
        self.cancel()

    @staticmethod
    def _stop(entry: _Held) -> None:
        if entry.task is not None and not entry.task.done():
            entry.task.cancel()
        if entry.timer is not None:
            entry.timer.cancel()

    @property
    def holding(self) -> bool:
        return bool(self._fifo)

    # ── A window ───────────────────────────────────────────────────

    def window(self, *, start: float, end: float, text: str, words: list[Word], shown: list[Word],
               enabled: bool) -> None:
        """A final for ``[start, end)``: its committed text and timed words,
        and the words the person was shown for it. Held when ``enabled`` and
        a stretch no word covers may hold speech; released at once
        otherwise (behind any window still held)."""
        now = time.monotonic()
        stretches = []
        if enabled and not self.closed and self._budget_left() <= 0:
            self._note_budget()
            enabled = False
        if enabled and not self.closed and self._speech.usable:
            for a, b in uncovered(start, end, words):
                samples, _ = self.ring.samples(a, b)
                if loud_seconds(samples, self.ring.rate) >= RELISTEN_MIN_SPEECH_S:
                    stretches.append((a, b))
        lost = [w for w in shown if any(a - EDGE_TOLERANCE_S <= _mid(w) < b + EDGE_TOLERANCE_S for a, b in stretches)
                and not any(_overlap(w, c) > 0 for c in words)]
        entry = _Held(text=text, words=list(words), shown=lost, window_end=end, arrived=now)
        self._fifo.append(entry)
        if stretches:
            loop = asyncio.get_running_loop()
            entry.task = loop.create_task(self._work(entry, stretches))
            entry.timer = loop.call_later(RELISTEN_HOLD_MAX_S, self._expire, entry)
        else:
            self._complete(entry, [])
        self._release()

    def _expire(self, entry: _Held) -> None:
        if not entry.done:
            logger.info("dictation re-listen ran out of time after %.1f s, the window goes with %d shown word(s)",
                        RELISTEN_HOLD_MAX_S, len(entry.shown))
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
            self._complete(entry, entry.recovered)
            self._release()

    async def _work(self, entry: _Held, stretches: list[tuple[float, float]]) -> None:
        began = time.monotonic()
        try:
            # The audio is copied here, on the loop: the ring moves while a
            # worker thread would read it.
            heard = []
            for a, b in stretches:
                stretch, at = self.ring.samples(a, b)
                if len(stretch):
                    context, _ = self.ring.samples(max(0.0, at - SPEECH_CONTEXT_S), at)
                    heard.append((a, b, context, stretch))
            gaps = await asyncio.to_thread(self._gaps, heard)
            # A window's gaps run together (the semaphore bounds the cuts);
            # each one's words land on the entry as they come back.
            results = await asyncio.gather(*(self._gap(entry, a, b, speech, began) for a, b, speech in gaps),
                                           return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.warning("dictation re-listen failed: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  # a re-listen that failed releases the window as the live stream left it
            logger.warning("dictation re-listen failed: %s", e)
        finally:
            if entry.timer is not None:
                entry.timer.cancel()
        if not entry.done:
            self._complete(entry, entry.recovered)
            self._release()

    async def _gap(self, entry: _Held, a: float, b: float, speech: float, began: float) -> None:
        words, cut = await self._cuts(a, b, lambda found: self._keep(found, a, b, entry))
        entry.recovered.extend(words)
        logger.info("dictation re-listen (%s): %.1f s stretch, %.1f s of speech, %d word(s) back%s in %.1f s",
                    self.label, b - a, speech, len(words),
                    f" from the cut of {cut[0]:.1f} s before and {cut[1]:.1f} s after" if words else "",
                    time.monotonic() - began)

    def _committed(self, entry: _Held) -> tuple[list[Word], list[Word]]:
        """Every committed word the guards compare with, and those the live
        stream timed."""
        every, live = list(self._recent), list(self._recent_live)
        for held in [*self._fifo, *([entry] if entry not in self._fifo else [])]:
            every.extend(held.out_words if held.done else [*held.words, *held.recovered])
            live.extend(held.words)
        return every, live

    def _keep(self, words: list[Word], a: float, b: float, entry: _Held) -> list[Word]:
        every, live = self._committed(entry)
        kept = []
        for w in words:
            form = self._norm(w.word)
            if not form:
                continue
            if w.end <= a + 0.05 or w.start >= min(b + EDGE_TOLERANCE_S, entry.window_end):
                continue
            if any(_overlap(w, c) > 0.5 * min(w.end - w.start, c.end - c.start) for c in live):
                continue
            if any(self._norm(c.word) == form and abs(c.start - w.start) <= DUPLICATE_WITHIN_S
                   for c in [*every, *kept]):
                continue
            kept.append(w)
        return kept

    # ── The merge and the release ──────────────────────────────────

    def _merged(self, entry: _Held, extra: list[Word]) -> tuple[str, list[Word]]:
        if not extra:
            return entry.text, entry.words
        pairs = self._timed_tokens(entry)
        if pairs is not None:
            pairs += [(w.word, w) for w in extra]
        elif entry.words:
            first = entry.words[0].start
            before = [w for w in extra if _mid(w) < first]
            after = [w for w in extra if _mid(w) >= first]
            text = " ".join([*(w.word for w in before), entry.text, *(w.word for w in after)]).strip()
            return text, sorted([*entry.words, *extra], key=lambda w: w.start)
        else:
            pairs = [(w.word, w) for w in extra] + ([(entry.text, Word(entry.text, 0.0, 0.0))] if entry.text else [])
        pairs.sort(key=lambda p: p[1].start)
        return " ".join(t for t, _ in pairs).strip(), sorted([*entry.words, *extra], key=lambda w: w.start)

    def _timed_tokens(self, entry: _Held) -> list[tuple[str, Word]] | None:
        """The window's text tokens, each with the timed word it is (a
        token that is punctuation alone rides with the word before it), or
        None when they do not line up."""
        out: list[tuple[str, Word]] = []
        words = iter(entry.words)
        for token in entry.text.split():
            if self._norm(token):
                word = next(words, None)
                if word is None:
                    return None
                out.append((token, word))
            elif out:
                out[-1] = (f"{out[-1][0]} {token}", out[-1][1])
            else:
                return None
        return out if entry.words and next(words, None) is None else None

    def _complete(self, entry: _Held, recovered: list[Word]) -> None:
        # The recovered words, and every word the person was shown that the
        # second listen did not hear again (painted words never die).
        extra = list(recovered)
        for w in entry.shown:
            form = self._norm(w.word)
            if any(_overlap(w, r) > 0 for r in recovered):
                continue
            if any(self._norm(r.word) == form and abs(r.start - w.start) <= DUPLICATE_WITHIN_S for r in recovered):
                continue
            extra.append(w)
        extra.sort(key=lambda w: w.start)
        entry.relistened = bool(extra)
        entry.out_text, entry.out_words = self._merged(entry, extra)
        entry.done = True

    def _placeholder(self, entry: _Held) -> str:
        return entry.out_text if entry.done else self._merged(entry, entry.shown)[0]

    def held_text(self) -> str:
        """The held windows' text as the person sees it meanwhile."""
        return " ".join(t for t in (self._placeholder(e) for e in self._fifo) if t)

    def _release(self) -> None:
        while self._fifo and self._fifo[0].done:
            entry = self._fifo.popleft()
            self._stop(entry)
            text, words = entry.out_text, entry.out_words
            last = self._last_word
            if ((entry.relistened or self._last_relistened) and last is not None and words
                    and self._norm(words[0].word) == self._norm(last.word) and words[0].start < last.end):
                tokens = text.split()
                if tokens and self._norm(tokens[0]) == self._norm(words[0].word):
                    text = " ".join(tokens[1:])
                words = words[1:]
            if words:
                self._last_word = words[-1]
                self._recent.extend(words)
                self._recent_live.extend(entry.words)
                horizon = words[-1].end - RECENT_KEEP_S
                for recent in (self._recent, self._recent_live):
                    while recent and recent[0].end < horizon:
                        recent.popleft()
            self._last_relistened = entry.relistened
            if text:
                self._on_release(text)

    async def settle(self, timeout: float) -> None:
        """Wait (bounded) for the windows still held, then release them all
        (a window whose re-listen is still out goes with its shown words)."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            pending = [e.task for e in self._fifo if not e.done and e.task is not None and not e.task.done()]
            left = deadline - time.monotonic()
            if not pending or left <= 0:
                break
            await asyncio.wait(pending, timeout=left)
        late = [entry for entry in self._fifo if not entry.done]
        if late:
            logger.info("dictation re-listen: %d window(s) still out at the finish go with %d shown word(s)",
                        len(late), sum(len(e.shown) for e in late))
        for entry in late:
            self._stop(entry)
            self._complete(entry, entry.recovered)
        self._release()


@dataclass
class _Window:
    """One live final of a call's turn."""

    start: float
    end: float
    text: str
    words: list[Word]
    task: asyncio.Task | None = None
    recovered: list[Word] = field(default_factory=list)
    released: bool = False


class TurnRelisten(_SecondListen):
    """A call's second listen, the narrow form: a live final with no word at
    all over ``RELISTEN_MIN_SPEECH_S`` of speech is listened to again at once,
    in the background (nothing waits for it there); the turn's take waits for
    what is still out, at most its bound, and gets the recovered words
    spliced into the turn's text by time (``complete``)."""

    _surface = "call"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._turn: list[_Window] = []

    def reset(self, rate: int) -> None:
        """A new connection: its clock starts at zero; the turn's notes go."""
        self.take()
        self._reset_audio(rate)

    def close(self) -> None:
        self.closed = True
        self.take()

    def take(self) -> None:
        """The turn went out (dispatched or queued): a re-listen still out is
        cancelled, the next turn starts empty."""
        for window in self._turn:
            if window.task is not None and not window.task.done():
                window.task.cancel()
        self._turn = []

    def window(self, *, start: float, end: float, text: str, words: list[Word], enabled: bool) -> None:
        """A live final of ``[start, end)`` on the connection's clock."""
        window = _Window(start=start, end=end, text=text.strip(), words=list(words))
        self._turn.append(window)
        if window.text or not enabled or self.closed or not self._speech.usable:
            return
        if self._budget_left() <= 0:
            self._note_budget()
            return
        stretch, at = self.ring.samples(start, end)
        if loud_seconds(stretch, self.ring.rate) < RELISTEN_MIN_SPEECH_S:
            return
        context, _ = self.ring.samples(max(0.0, at - SPEECH_CONTEXT_S), at)
        window.task = asyncio.get_running_loop().create_task(self._work(window, context, stretch))

    async def _work(self, window: _Window, context: np.ndarray, stretch: np.ndarray) -> None:
        began = time.monotonic()
        try:
            gaps = await asyncio.to_thread(self._gaps, [(window.start, window.end, context, stretch)])
            if not gaps:
                return
            _a, _b, speech = gaps[0]
            words, cut = await self._cuts(window.start, window.end, lambda found: self._keep(found, window))
            window.recovered = words
            logger.info("call re-listen (%s): %.1f s window, %.1f s of speech, %d word(s) back%s in %.1f s",
                        self.label, window.end - window.start, speech, len(words),
                        f" from the cut of {cut[0]:.1f} s before and {cut[1]:.1f} s after" if words else "",
                        time.monotonic() - began)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  # a re-listen that failed leaves the turn as the live stream left it
            logger.warning("call re-listen failed: %s", e)

    def _keep(self, words: list[Word], window: _Window) -> list[Word]:
        live = [w for other in self._turn for w in other.words]
        kept: list[Word] = []
        for w in words:
            form = self._norm(w.word)
            if not form:
                continue
            if w.end <= window.start + 0.05 or w.start >= window.end + EDGE_TOLERANCE_S:
                continue
            if any(_overlap(w, c) > 0.5 * min(w.end - w.start, c.end - c.start) for c in live):
                continue
            if any(self._norm(c.word) == form and abs(c.start - w.start) <= DUPLICATE_WITHIN_S
                   for c in [*live, *kept]):
                continue
            kept.append(w)
        return kept

    def _outstanding(self) -> list[asyncio.Task]:
        return [w.task for w in self._turn if w.task is not None and not w.task.done()]

    @property
    def wordless(self) -> bool:
        """The turn so far has finals and not one word in them."""
        return bool(self._turn) and not any(w.text for w in self._turn)

    async def wait_wordless(self, timeout: float) -> str | None:
        """A turn with no committed word whose re-listen is still out (the
        caller's whole answer came back empty): wait for it, at most
        ``timeout``, and give its words once, as the turn's final."""
        if not self.wordless:
            return None
        pending = self._outstanding()
        if pending and timeout > 0:
            await asyncio.wait(pending, timeout=timeout)
        # What missed the bound is dropped here: finishing later, its words
        # would open the next turn.
        for task in pending:
            if not task.done():
                task.cancel()
        words = []
        for window in self._turn:
            if window.recovered and not window.released:
                window.released = True
                words.extend(window.recovered)
        return " ".join(w.word for w in sorted(words, key=lambda w: w.start)) or None

    async def complete(self, text: str, bound: float) -> str:
        """The turn's text with its windows' recovered words in, waiting at
        most ``bound`` for a re-listen still out (none: no wait at all)."""
        pending = self._outstanding()
        if pending and bound > 0:
            began = time.monotonic()
            await asyncio.wait(pending, timeout=bound)
            logger.info("call re-listen: the turn waited %.2f s for %d window(s)",
                        time.monotonic() - began, len(pending))
        windows = sorted(self._turn, key=lambda w: w.start)
        self._trim_seams(windows)
        gaps = [w for w in windows if w.recovered and not w.released]
        if not gaps:
            return text
        for gap in gaps:
            gap.released = True
        finals = [w for w in windows if w.text]

        def said(window: _Window) -> str:
            return " ".join(w.word for w in window.recovered)

        if self._same(text, " ".join(w.text for w in finals)):
            pieces = sorted([(w.start, w.text) for w in finals] + [(g.start, said(g)) for g in gaps])
            return " ".join(p for _, p in pieces)
        # The turn's text is not the finals' alone (a live partial went out
        # in a final's place, a segment was deduplicated): each window's
        # words go after the last final before it, found in the text.
        out, at = text, 0
        for gap in gaps:
            before = [w for w in finals if w.start < gap.start]
            add = said(gap)
            if not before:
                out = f"{add} {out}"
                at = len(add) + 1
                continue
            anchor = before[-1].text
            i = out.find(anchor, at)
            if i < 0:
                out = f"{out} {add}"
                at = len(out)
                continue
            j = i + len(anchor)
            out = f"{out[:j]} {add}{out[j:]}"
            at = j + 1 + len(add)
        return " ".join(out.split())

    def _trim_seams(self, windows: list[_Window]) -> None:
        """A recovered piece whose head repeats the tail of the piece just
        before it (a cut reaching back into the window before: "σχετικά
        σχετικά", two empty windows in a row) loses the repeat."""
        tail: list[str] = []
        end: float | None = None
        for window in windows:
            if window.recovered and not window.released:
                if end is not None and window.start - end <= SEAM_S:
                    forms = [self._norm(w.word) for w in window.recovered]
                    k = next((k for k in range(min(3, len(tail), len(forms)), 0, -1) if tail[-k:] == forms[:k]), 0)
                    window.recovered = window.recovered[k:]
                tail = [self._norm(w.word) for w in window.recovered] or tail
            elif window.text:
                tail = [f for f in map(self._norm, window.text.split()) if f]
            else:
                tail = []
            end = window.end

    def _same(self, a: str, b: str) -> bool:
        return [f for f in map(self._norm, a.split()) if f] == [f for f in map(self._norm, b.split()) if f]

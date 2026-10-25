"""One STT provider made of one stream per language in play.

``LanguageStreams`` is the plumbing under automatic language detection: the
pipeline and the dictation relay keep talking to ONE ``STTProvider``, this
one, which owns the default language's stream, the active stream once a
switch moved away from the default, and at most one more candidate (a new
lean closes the previous candidate), fans every write and lifecycle hook
out to all of them, keeps the audio it sent since the last turn was taken
as a candidate's replay, and answers every read (the drains, the interim
fallback, the guard's health) from the ACTIVE stream. It knows nothing about languages beyond their names: the
rule (``audio.providers.lid.rule``) decides, the consumer applies.

On a provider without word confidence a switch is pending until the
candidate is live and has answered its replay, bounded (``SWITCH_READY_S``);
when both streams' providers report a word confidence it is the WORDS that
decide (``audio.providers.lid.evidence`` over the two ``heard`` ledgers on a
shared clock): the switch resolves when the candidate's words beat the shown
stream's, is refused (``refused()``) when the shown stream's words win, and
is held while they are even (``SWITCH_READY_S`` after the latest of the
request, the candidate catching up and its first words; ``SWITCH_WAIT_MAX_S``
while it has not caught up; ``SWITCH_WORDLESS_MAX_S`` while it answered
without a word and the speech goes on, then ``SWITCH_READY_S`` from the settle
point).
While a resolved switch is not final the default is compared back against
the active stream, and a decisive win calls it back (``called_back()``).
The read surface moves at the resolve and the consumer learns of it through
``switched()``, together with
the new stream's text since the last take AS OF THAT CALL (the current
turn's words, for the rewrite of the turn and the ``replace`` frame; a
final drained between the resolve and the call is in it, so the text the
consumer shows again never lacks a final it already sent). A take moves every
stream's cursor, resets the replay, closes every candidate but the active
one and abandons a switch still pending to it (the next lean reopens one
with the next turn's audio). Settling closes every
stream but the active one. Evidence (``speech_evidence``,
``has_pending_finals``) looks at every stream: a barge-in paused over a
switch must not resume the reply over a caller who is speaking;
``has_drainable_finals`` answers for the active stream, the one the drains
read.

A candidate is behind its audio: the vendor transcribes the replay at about
real-time pace, not at once. An endpoint closes only what a stream has
transcribed, so a forced endpoint on a stream that is behind ends its window
in the middle of the replay (an empty final, a split word, the words after
it lost): ``force_endpoint`` sends it at once to a stream that keeps up and
holds it for one that is behind until that stream has caught up with the
moment it was asked for.

Every stream is built by the consumer's factory from the same provider row,
so the vendor bills each as a stream of its own: ``seconds_sent`` per
stream and ``billed_seconds`` over all of them feed the usage record and
the dictation cost cap.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from audio.capabilities import STTCapabilities
from audio.providers.credential_resolver import CredentialResolver
from audio.providers.lid.evidence import SHOWN, TARGET, Side, Verdict, compare
from audio.providers.stt.base import Heard, STTProvider

logger = logging.getLogger(__name__)

#: Below this, a verdict's side has no word (word counts are weighted floats).
_WORD = 0.5

#: How long a pending switch waits for the candidate to be live and to have
#: answered its replay before it is abandoned (the old stream stays). When
#: the words decide, it is the time they get after the later of the request
#: and the candidate catching up with its replay; a candidate not open yet,
#: or still behind, is abandoned ``SWITCH_WAIT_MAX_S`` after the request.
SWITCH_READY_S = 1.5
SWITCH_WAIT_MAX_S = 4.0
#: While the candidate has answered without a word over the compared span
#: (Deepgram's nova-3 answers some Greek windows empty), the words cannot
#: judge it: the switch stays pending this long after the request, until its
#: next window brings words. Nothing shown changes meanwhile; a reading that
#: moves elsewhere ends it sooner (the rule proposes another switch). Once
#: the speech has settled (a call's take, a dictation's stop) the candidate
#: gets ``SWITCH_READY_S`` from the settle point, never past the long bound.
SWITCH_WORDLESS_MAX_S = 8.0
#: The audio kept for a candidate's replay (since the last take).
REPLAY_MAX_S = 20.0
#: The replay goes out in pieces of this length.
REPLAY_PIECE_S = 0.2
#: A stream whose answers trail the audio it was offered by more than this
#: is behind (the vendor answers about once a second of audio, so a stream
#: in real time trails by up to a second between answers).
BEHIND_S = 1.2
#: A held endpoint goes out once the stream has answered up to this close
#: to the moment it was asked for (a call asks at the speech end, after the
#: VAD's silence padding), or this long after it was asked whatever the
#: stream did.
CAUGHT_UP_S = 0.3
ENDPOINT_HOLD_MAX_S = 4.0


class _Stream:
    """One language's stream and what it has produced."""

    def __init__(self, language: str, provider: STTProvider) -> None:
        self.language = language
        self.provider = provider
        self.live = False
        self.opening = False
        self.failed = False
        self.buffer = bytearray()
        self.history: list[str] = []
        self.cursor = 0
        self.seconds_sent = 0.0
        self.replay_sent_at = 0.0
        self.force_after_replay = False
        #: The audio the stream replays once connected (taken when it was
        #: registered), and the connect's progress.
        self.replay = b""
        self.connecting = False
        self.opened = asyncio.Event()
        self.discard_next_final = False
        self.discard_until = 0.0
        self.discard_takes = 0
        #: Where the stream's own audio clock starts on the shared clock (a
        #: candidate's is where its replay begins), and where its audio for
        #: the comparison begins (that, or the last take).
        self.offset = 0.0
        self.born_at = 0.0
        #: An endpoint held until the stream catches up with this point of
        #: its own clock (bounded in time), and where its replay ends on it.
        self.endpoint_at: float | None = None
        self.endpoint_by = 0.0
        self.replay_end = 0.0
        self.caught_up_since: float | None = None
        self.live_since = 0.0
        #: Registered beside the default (it replays the turn and may trail
        #: it); the default stream never trails: a pause is no lag.
        self.candidate = False

    def text_since_take(self) -> str:
        return " ".join(self.history[self.cursor:]).strip()

    @property
    def answered(self) -> bool:
        """A result (an interim or a final) landed after the replay went
        out: the stream has caught up with the audio."""
        return self.live and self.provider.last_result_monotonic > self.replay_sent_at

    @property
    def reports_confidence(self) -> bool:
        return bool(getattr(getattr(self.provider, "capabilities", None), "reports_confidence", False))

    @property
    def heard(self) -> tuple[Heard, ...]:
        return tuple(getattr(self.provider, "heard", ()))

    @property
    def offered_s(self) -> float:
        return float(getattr(self.provider, "audio_offered_s", 0.0))

    @property
    def answered_until(self) -> float:
        """How far the stream has answered, on its own clock."""
        return max((h.end for h in self.heard), default=0.0)

    @property
    def behind(self) -> bool:
        """A candidate still working through its replay, or trailing its
        audio by more than the vendor's answer cadence."""
        if not self.candidate:
            return False
        return self.answered_until < self.replay_end or self.offered_s - self.answered_until > BEHIND_S

    def hold_endpoint(self, at: float) -> None:
        self.endpoint_at = at
        self.endpoint_by = time.monotonic() + ENDPOINT_HOLD_MAX_S

    def side(self) -> Side:
        """The stream's ledger on the shared clock."""
        off = self.offset
        return Side(tuple(Heard(h.start + off, h.end + off, h.words, h.confidence, h.final,
                                tuple((a + off, b + off, c) for a, b, c in h.marks))
                          for h in self.heard), self.born_at)


def _enable_turn_relisten(provider: STTProvider) -> None:
    enable = getattr(provider, "enable_turn_relisten", None)
    if enable is not None:
        enable()


def _relisten_seconds(provider: STTProvider) -> float:
    return float(getattr(provider, "relisten_seconds", 0.0) or 0.0)


def _ledger_key(stream: _Stream):
    """What changes when the stream's ledger does (its version, or for a
    provider without one its size and newest entry)."""
    version = getattr(stream.provider, "heard_version", None)
    if version is not None:
        return version
    heard = stream.heard
    return len(heard), heard[-1:]


class LanguageStreams(STTProvider):
    """The default, the active and at most one more stream, behind one
    provider."""

    capabilities = STTCapabilities(supports_streaming=True, supports_endpointing=True, is_local=False)

    def __init__(self, build: Callable[[], STTProvider], default: str) -> None:
        self._build = build
        self.default = default
        self.active = default
        self._streams: dict[str, _Stream] = {}
        self._params: dict[str, Any] = {}
        self._rate = 16000
        self._replay = bytearray()
        self._replay_max = int(REPLAY_MAX_S * self._rate) * 2
        self._pending: str | None = None
        self._pending_since = 0.0
        self._switch_done = asyncio.Event()
        self._resolved: str | None = None
        self._dropped: list[str] = []
        self._closing: set[asyncio.Task] = set()
        # The seconds streams that are gone had been sent, per language (they
        # stay billed, and the default's stay the default's).
        self._closed_seconds: dict[str, float] = {}
        self._closed_relisten = 0.0
        self.settled = False
        self.closed = False
        self._cls: type[STTProvider] | None = None
        self._take_clock = 0.0
        # The speech ended and the streams were finalised (a call's take, a
        # dictation's stop): one word on a side is evidence.
        self._speech_settled = False
        self._refused: tuple[str, Verdict] | None = None
        self._called_back: str | None = None
        self._call_back_armed = False
        self._call_back_key: tuple | None = None
        self._logged: tuple[str, str] | None = None
        # The pending switch's verdict, recomputed only when a ledger
        # changed, and how many successive changes it has been a win.
        self._pending_key: tuple | None = None
        self._pending_verdict: Verdict | None = None
        self._target_streak = 0
        # When the pending candidate first had words over the compared span:
        # the time the words get to decide runs from there.
        self._pending_worded_at: float | None = None
        # When the last settle point came (``settle_point``).
        self._settled_at = 0.0
        self._words_note = False
        self._drained = ""
        self._tasks: set[asyncio.Task] = set()
        # A call's second listen, on every stream built (``enable_turn_relisten``).
        self._turn_relisten = False
        # The default stream's provider exists from construction (the VAD
        # reads its padding, the consumer asks it which languages stream); it
        # connects at ``start``.
        self._streams[default] = self._new_stream(default)

    @classmethod
    def from_row(cls, row: dict, resolver: CredentialResolver) -> STTProvider:
        raise NotImplementedError("LanguageStreams is built around a factory, not a row")

    # ── Construction and the default stream ────────────────────────

    def _new_stream(self, language: str) -> _Stream:
        provider = self._build()
        if self.dictation_mode:
            provider.enable_dictation_mode()
        if self._turn_relisten:
            _enable_turn_relisten(provider)
        stream = _Stream(language, provider)
        provider.on_partial_final = lambda text, s=stream: self._forward_partial(s, text)
        # Only the stream the person sees re-listens a window it answered
        # without words, and while a switch is pending only the one about to
        # be shown: a stream in the wrong language answers most windows empty
        # by design, and its second listen hears nothing either (English on
        # Greek speech: 0 words back, a call's budget spent in 20 s).
        provider.relisten_gate = lambda s=stream: s.language == (self._pending or self.active)
        # One budget for the dictation, whichever streams spend it.
        provider.dictation_seconds = lambda: self.billed_seconds
        provider.dictation_relistened = lambda: self.relisten_seconds
        self._cls = type(provider)
        return stream

    def _forward_partial(self, stream: _Stream, text: str) -> None:
        if stream.language == self.active and stream.live and not stream.discard_next_final:
            self._emit_partial_final(text)

    def enable_dictation_mode(self) -> None:
        self.dictation_mode = True
        for stream in self._streams.values():
            stream.provider.enable_dictation_mode()

    def enable_turn_relisten(self) -> None:
        self._turn_relisten = True
        for stream in self._streams.values():
            _enable_turn_relisten(stream.provider)

    async def complete_turn(self, text: str, bound: float) -> str:
        """The turn's text is the active stream's: its second listen
        completes it."""
        complete = getattr(self._active.provider, "complete_turn", None) if self._streams else None
        return await complete(text, bound) if complete is not None else text

    def turn_taken(self) -> None:
        for stream in self._streams.values():
            taken = getattr(stream.provider, "turn_taken", None)
            if taken is not None:
                taken()

    async def release_wordless_turn(self, timeout: float) -> bool:
        release = getattr(self._active.provider, "release_wordless_turn", None) if self._streams else None
        return bool(await release(timeout)) if release is not None else False

    async def start(
        self, language: str = "multi", sample_rate: int | None = None, interim_results: bool = False,
        endpointing_ms: int | None = None,
    ) -> None:
        """Open the default stream; the parameters are every later stream's."""
        self._params = dict(sample_rate=sample_rate, interim_results=interim_results, endpointing_ms=endpointing_ms)
        if sample_rate:
            self._rate = int(sample_rate)
            self._replay_max = int(REPLAY_MAX_S * self._rate) * 2
        stream = self._streams.get(self.default) or self._new_stream(self.default)
        self._streams[self.default] = stream
        await stream.provider.start(language=self.default, **self._params)
        stream.live = True
        stream.live_since = time.monotonic()
        self._is_open = True

    @property
    def _active(self) -> _Stream:
        return self._streams[self.active]

    @property
    def _clock(self) -> float:
        """The shared clock: the default stream's audio clock (every stream
        maps its own onto it through its offset)."""
        stream = self._streams.get(self.default) or self._streams.get(self.active)
        return stream.offset + stream.offered_s if stream else 0.0

    @property
    def words_compared(self) -> bool:
        """Every stream's provider reports a word confidence (they are one
        provider's, built by one factory)."""
        stream = self._streams.get(self.default) or next(iter(self._streams.values()), None)
        return stream is not None and stream.reports_confidence

    @property
    def _live(self) -> list[_Stream]:
        return [s for s in self._streams.values() if s.live]

    @property
    def languages(self) -> list[str]:
        return list(self._streams)

    def streams_language(self, language: str) -> bool:
        stream = self._streams.get(self.default) or next(iter(self._streams.values()), None)
        return stream.provider.streams_language(language) if stream else True

    # ── The candidate ──────────────────────────────────────────────

    def register(self, language: str) -> bool:
        """Register a candidate stream NOW, before its connect: the rule's
        ``open`` and ``switch`` for the same language often arrive in one
        batch (a short first answer, a lean at the take), and the switch
        must find the stream and go pending rather than fail. The replay
        snapshot (what the active stream heard since the last take) is taken
        here; a previous candidate goes first. False when refused (closed,
        settled)."""
        if self.closed or self.settled:
            return False
        if language in self._streams:
            return True
        for other in [s for lang, s in self._streams.items() if lang not in (self.default, self.active, language)]:
            self._drop(other, "replaced")
        stream = self._new_stream(language)
        stream.opening = True
        stream.candidate = True
        stream.replay = bytes(self._replay)
        # The replay is the shared clock's audio up to now: the candidate's
        # own clock starts where it begins (a trimmed or capped replay
        # starts later).
        stream.offset = self._clock - len(stream.replay) / (self._rate * 2)
        stream.born_at = max(stream.offset, self._take_clock)
        self._streams[language] = stream
        return True

    async def open(self, language: str) -> bool:
        """Connect a candidate stream (registered here if not yet): the
        replay snapshot, then the frames that arrived while it connected,
        then live. False when it could not open (the consumer reports the
        language as unavailable to the rule after a second failure)."""
        if not self.register(language):
            return False
        stream = self._streams[language]
        if stream.live:
            return True
        if stream.connecting:
            await stream.opened.wait()
            return self._streams.get(language) is stream and stream.live
        stream.connecting = True
        try:
            return await self._connect(stream)
        finally:
            stream.opened.set()

    async def _connect(self, stream: _Stream) -> bool:
        language = stream.language
        try:
            await stream.provider.start(language=language, **self._params)
        except Exception as e:  # noqa: BLE001  # a vendor's connect error of any kind: the candidate is dropped
            logger.warning("language stream %s failed to open: %s", language, e)
            if self._streams.get(language) is stream:
                self._streams.pop(language, None)
                stream.failed = True
                self._dropped.append(language)
                self._pending_gone(language)
            return False
        if self.closed or self.settled or self._streams.get(language) is not stream:
            self._finish_quietly(stream.provider)
            return False
        replay, stream.replay = stream.replay, b""
        piece = int(REPLAY_PIECE_S * self._rate) * 2
        for i in range(0, len(replay), piece):
            await stream.provider.send_audio(replay[i:i + piece])
        stream.seconds_sent += len(replay) / (self._rate * 2)
        stream.replay_sent_at = time.monotonic()
        stream.replay_end = len(replay) / (self._rate * 2)
        stream.live_since = stream.replay_sent_at
        while stream.buffer:
            chunk, stream.buffer = bytes(stream.buffer), bytearray()
            await stream.provider.send_audio(chunk)
            stream.seconds_sent += len(chunk) / (self._rate * 2)
        stream.opening = False
        stream.live = True
        if stream.force_after_replay:
            stream.force_after_replay = False
            with contextlib.suppress(Exception):
                await stream.provider.force_endpoint()
        logger.info("language stream %s open, %.1f s replayed", language, len(replay) / (self._rate * 2))
        return True

    def _pending_gone(self, language: str) -> None:
        """A pending switch whose stream is gone is over: whoever waits on it
        learns at once (``wait_switched``)."""
        if self._pending == language:
            self._pending = None
            self._switch_done.set()

    def switch(self, language: str) -> bool:
        """Move the read surface to ``language`` once its stream is ready;
        pending meanwhile. False when no such stream exists."""
        if language == self.active:
            self._pending = None
            self._switch_done.set()
            return True
        if language not in self._streams or self._streams[language].failed:
            return False
        self._pending = language
        self._pending_since = time.monotonic()
        self._logged = None
        self._pending_key = None
        self._target_streak = 0
        self._pending_worded_at = None
        self._switch_done.clear()
        self._resolve_pending()
        return True

    async def wait_switched(self, timeout: float) -> None:
        """Wait (bounded) for a pending switch to resolve or be abandoned.
        The candidate's answer is read off its stream, so the wait looks
        every 50 ms (no frame need flow meanwhile, as at a dictation's stop)
        and ends early when the switch is over."""
        deadline = time.monotonic() + timeout
        while self.pending is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._switch_done.wait(), min(0.05, remaining))

    def _resolve_pending(self) -> None:
        self._send_held_endpoints()
        if self._pending is None:
            self._watch_call_back()
            return
        stream = self._streams.get(self._pending)
        if stream is None or stream.failed:
            self._pending = None
            self._switch_done.set()
            return
        if not self.words_compared:
            if not stream.live:
                self._abandon_when_late("the stream did not answer in time")
                return
            if not self._words_note:
                self._words_note = True
                logger.info("language switch to %s: words not compared on this provider, "
                            "the acoustic rule decides", stream.language)
            if stream.language == self.default or stream.answered:
                self._resolve(stream)
            else:
                self._abandon_when_late("the stream did not answer in time")
            return
        now = time.monotonic()
        if not stream.live:
            self._abandon_when_late("the stream did not open in time", limit=SWITCH_WAIT_MAX_S)
            return
        behind = stream.behind
        if not behind and stream.caught_up_since is None:
            stream.caught_up_since = now
        key = (_ledger_key(stream), _ledger_key(self._active), self._speech_settled)
        if key != self._pending_key or self._pending_verdict is None:
            self._pending_key = key
            self._pending_verdict = compare(stream.side(), self._active.side(), settled=self._speech_settled)
            self._target_streak = self._target_streak + 1 if self._pending_verdict.outcome == TARGET else 0
        verdict = self._pending_verdict
        if verdict.target_words >= _WORD and self._pending_worded_at is None:
            self._pending_worded_at = now
        # A win shows the other language's words: it must hold on two
        # successive answers or rest on a final one (one early window can
        # flatter the wrong stream); a refusal only delays and needs neither.
        if verdict.outcome == TARGET and (verdict.target_final or self._target_streak >= 2):
            self._log_verdict(stream.language, "resolved", verdict)
            self._resolve(stream)
        elif verdict.outcome == SHOWN:
            self._log_verdict(stream.language, "refused, decisive" if verdict.decisive else "refused", verdict)
            self._pending = None
            self._refused = (stream.language, verdict)
            self._switch_done.set()
        else:
            self._log_verdict(stream.language, "held", verdict)
            if verdict.target_words < _WORD and stream.heard and self._speech_settled:
                # The take or the stop forced every stream's final: a
                # candidate still without a word has nothing coming for this
                # speech (a Greek candidate on accented English is often
                # wordless): it goes ``SWITCH_READY_S`` after the settle point,
                # or at the long bound when that comes first.
                self._abandon_when_late("the candidate heard no word in time",
                                        since=min(self._settled_at,
                                                  self._pending_since + SWITCH_WORDLESS_MAX_S - SWITCH_READY_S))
            elif verdict.target_words < _WORD and stream.heard:
                # The candidate answered, without a word, while speech goes
                # on: its next window decides (a candidate that never
                # answered is the cap's).
                self._abandon_when_late("the candidate heard no word in time", limit=SWITCH_WORDLESS_MAX_S)
            elif behind:
                # Still working through its replay: its words are on the
                # way, the time they get starts once it has caught up.
                self._abandon_when_late("the stream did not catch up in time", limit=SWITCH_WAIT_MAX_S)
            else:
                # From the request, the candidate catching up, and its first
                # words, whichever came last: an empty first window must not
                # spend the time its words then get.
                self._abandon_when_late("the words did not decide in time",
                                        since=max(self._pending_since, stream.caught_up_since or now,
                                                  self._pending_worded_at or 0.0))

    def _resolve(self, stream: _Stream) -> None:
        self._drain_all()
        self.active = stream.language
        self._pending = None
        self._resolved = stream.language
        self._call_back_armed = stream.language != self.default
        self._call_back_key = None
        self._switch_done.set()
        logger.info("language stream active: %s", stream.language)

    def _abandon_when_late(self, why: str, since: float | None = None, limit: float | None = None) -> None:
        limit = SWITCH_READY_S if limit is None else limit
        if time.monotonic() - (self._pending_since if since is None else since) >= limit:
            logger.info("language switch to %s abandoned: %s", self._pending, why)
            self._pending = None
            self._switch_done.set()

    def _log_verdict(self, language: str, outcome: str, verdict: Verdict) -> None:
        """Once per outcome change of a pending switch, numbers only."""
        if self._logged == (language, outcome):
            return
        self._logged = (language, outcome)
        logger.info("language switch to %s %s: %.0f words at %.2f against %.0f at %.2f over %.1f s",
                    language, outcome, verdict.target_words, verdict.target_confidence,
                    verdict.shown_words, verdict.shown_confidence, verdict.span[1] - verdict.span[0])

    def _watch_call_back(self) -> None:
        """A resolved switch the default's words decisively beat since: the
        consumer is told once (``called_back``) and switches back, judged
        like any switch."""
        if not self._call_back_armed or self.settled or self.active == self.default:
            return
        default = self._streams.get(self.default)
        if default is None or not default.live or not self.words_compared:
            return
        key = (_ledger_key(default), _ledger_key(self._active), self._speech_settled)
        if key == self._call_back_key:
            return
        self._call_back_key = key
        verdict = compare(default.side(), self._active.side(), settled=self._speech_settled)
        if verdict.outcome == TARGET and verdict.decisive:
            self._call_back_armed = False
            self._called_back = self.default
            self._logged = (self.default, "called back")
            logger.info("language switch to %s called back: %.0f words at %.2f against %.0f at %.2f over %.1f s",
                        self.default, verdict.target_words, verdict.target_confidence,
                        verdict.shown_words, verdict.shown_confidence, verdict.span[1] - verdict.span[0])

    @property
    def pending(self) -> str | None:
        self._resolve_pending()
        return self._pending

    def switched(self) -> tuple[str, str] | None:
        """A switch that resolved since the last call: the new active
        language and its text since the last take as of now, once."""
        self._resolve_pending()
        resolved, self._resolved = self._resolved, None
        if resolved is None:
            return None
        return resolved, self.text_since_take(resolved)

    def refused(self) -> tuple[str, Verdict] | None:
        """A switch the shown stream's words won since the last call: the
        language and the verdict, once."""
        self._resolve_pending()
        refused, self._refused = self._refused, None
        return refused

    def called_back(self) -> str | None:
        """The default's words decisively beat the active stream's since the
        last call: the default, once."""
        self._resolve_pending()
        called, self._called_back = self._called_back, None
        return called

    def settle_point(self, on: bool = True) -> None:
        """The speech ended and every stream was finalised (a call's take, a
        dictation's stop): until the next take or the finish, a comparison
        takes one word on a side as evidence."""
        self._speech_settled = on
        if on:
            self._settled_at = time.monotonic()
        self._call_back_key = None

    def drained_at_take(self) -> str:
        """The active stream's text the last take drained (finals that landed
        after the consumer's own drain), once."""
        drained, self._drained = self._drained, ""
        return drained

    def dropped(self) -> list[str]:
        """Candidates that failed to open or died since the last call."""
        dropped, self._dropped = self._dropped, []
        return dropped

    def trim_replay(self, keep_seconds: float) -> None:
        """Keep only the last ``keep_seconds`` of replay (the dictation relay
        trims the silence before the first word to a pre-roll, the phone
        pipeline at each turn's first voiced frame)."""
        keep = int(keep_seconds * self._rate) * 2
        if len(self._replay) > keep:
            del self._replay[: len(self._replay) - keep]

    def text_since_take(self, language: str | None = None) -> str:
        stream = self._streams.get(language or self.active)
        return stream.text_since_take() if stream else ""

    def take(self) -> list[str]:
        """A turn was taken: every stream's cursor moves to now, the replay
        starts over, every candidate but the active closes, a switch still
        pending to one of them abandoned (a caller that wants the switch
        waits for it first, ``wait_switched``; a candidate kept here would
        answer the words just taken as the next turn's). Returns the
        languages closed. The active stream's text it drained is kept for
        ``drained_at_take``."""
        self._drained = self._drain_all() or ""
        self._take_clock = self._clock
        self._speech_settled = False
        for stream in self._streams.values():
            stream.cursor = len(stream.history)
            stream.born_at = max(stream.born_at, self._take_clock)
            if stream.discard_next_final:
                # Kept through the take that follows its arming (the same
                # turn's), gone at the next.
                stream.discard_takes += 1
                if stream.discard_takes >= 2:
                    stream.discard_next_final = False
        self._replay.clear()
        closed = []
        for lang, stream in list(self._streams.items()):
            if lang not in (self.default, self.active):
                self._drop(stream, "taken")
                self._pending_gone(lang)
                closed.append(lang)
        return closed

    def discard_next_final(self, within_s: float = 1.0) -> None:
        """The active stream's next final belongs to the turn about to be
        taken (its words were in flight and went out from another stream):
        it is dropped when it lands instead of seeding the next turn. The
        mark lives ``within_s`` and through the take that follows it, so a
        final that never comes cannot take the next turn's first words."""
        stream = self._active
        stream.discard_next_final = True
        stream.discard_until = time.monotonic() + within_s
        stream.discard_takes = 0

    def settle(self) -> None:
        """The decision is final: every stream but the active one closes."""
        self.settled = True
        self._pending = None
        for lang, stream in list(self._streams.items()):
            if lang != self.active:
                self._drop(stream, "settled")

    def _drop(self, stream: _Stream, why: str) -> None:
        self._streams.pop(stream.language, None)
        stream.failed = True
        self._finish_quietly(stream.provider)
        logger.info("language stream %s closed (%s, %.1f s sent)", stream.language, why, stream.seconds_sent)
        self._closed_seconds[stream.language] = self._closed_seconds.get(stream.language, 0.0) + stream.seconds_sent
        self._closed_relisten += _relisten_seconds(stream.provider)

    def _finish_quietly(self, provider: STTProvider) -> None:
        async def _close() -> None:
            with contextlib.suppress(Exception):
                await provider.close()

        task = asyncio.get_running_loop().create_task(_close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    # ── Billing ────────────────────────────────────────────────────

    def billing_unit(self):  # type: ignore[override]
        return self._cls.billing_unit() if self._cls else "second"

    def cost_per_unit(self) -> float:  # type: ignore[override]
        return self._cls.cost_per_unit() if self._cls else 0.0

    @property
    def billed_seconds(self) -> float:
        return sum(s.seconds_sent for s in self._streams.values()) + sum(self._closed_seconds.values())

    @property
    def relisten_seconds(self) -> float:
        """The batch seconds every stream bought for its second listen, open
        or closed (billed on top of ``billed_seconds``)."""
        return sum(_relisten_seconds(s.provider) for s in self._streams.values()) + self._closed_relisten

    @property
    def extra_seconds(self) -> float:
        """What the candidate streams cost beyond the default's own stream,
        open or closed."""
        default = self._streams.get(self.default)
        default_seconds = default.seconds_sent if default else 0.0
        return self.billed_seconds - default_seconds - self._closed_seconds.get(self.default, 0.0)

    # ── Writes: fanned out ─────────────────────────────────────────

    def _extend_replay(self, audio_bytes: bytes) -> None:
        self._replay.extend(audio_bytes)
        if len(self._replay) > self._replay_max:
            del self._replay[: len(self._replay) - self._replay_max]

    async def send_audio(self, audio_bytes: bytes) -> None:
        self._extend_replay(audio_bytes)
        for stream in list(self._streams.values()):
            if stream.live:
                await stream.provider.send_audio(audio_bytes)
                stream.seconds_sent += len(audio_bytes) / (self._rate * 2)
            elif stream.opening:
                stream.buffer.extend(audio_bytes)
        self._drop_marked_finals()
        self._watch_candidates()
        self._resolve_pending()

    async def feed_during_tts(self, audio_bytes: bytes) -> None:
        silence = b"\x00" * len(audio_bytes)
        if self.words_compared:
            # The default's clock counts the silence fed during playback:
            # the replay keeps it too, so a candidate's audio stays where
            # the default's was (its offset is the clock minus the replay).
            self._extend_replay(silence)
        for stream in list(self._streams.values()):
            if stream.live:
                await stream.provider.feed_during_tts(audio_bytes)
                stream.seconds_sent += len(audio_bytes) / (self._rate * 2)
            elif stream.opening:
                stream.buffer.extend(silence)
        self._drop_marked_finals()
        self._watch_candidates()

    def _drop_marked_finals(self) -> None:
        """A marked stream's next final is decided when it ARRIVES (every
        frame looks): dropped while the mark lives, the mark gone at its
        deadline. A drain before that frame drops it too."""
        now = time.monotonic()
        for stream in list(self._streams.values()):
            if not stream.discard_next_final or not stream.live:
                continue
            if stream.provider.has_pending_finals:
                text = stream.provider.drain_transcript()
                stream.discard_next_final = False
                if text:
                    logger.info("language stream %s: the final of the turn taken dropped (%d chars)",
                                stream.language, len(text))
            elif now > stream.discard_until:
                stream.discard_next_final = False

    async def force_endpoint(self) -> None:
        """Every stream finalises what it heard up to now: a stream that
        keeps up at once, one that is behind (a candidate working through
        its replay) once it has caught up with this moment, so its endpoint
        never cuts the replay short. A provider without the ledger gets it
        at once, or right after the replay."""
        for stream in list(self._streams.values()):
            if stream.live:
                if stream.reports_confidence and stream.behind:
                    stream.hold_endpoint(stream.offered_s)
                else:
                    await stream.provider.force_endpoint()
            elif stream.opening:
                if stream.reports_confidence:
                    stream.hold_endpoint((len(stream.replay) + len(stream.buffer)) / (self._rate * 2))
                else:
                    stream.force_after_replay = True
        self._send_held_endpoints()

    def _send_held_endpoints(self) -> None:
        for stream in list(self._streams.values()):
            if stream.endpoint_at is None or not stream.live:
                continue
            # The vendor works at real-time pace at least: a stream live for
            # as long as the audio it had to answer has caught up, whatever
            # its answers say yet (it may hold its next one until it ends).
            caught_up_by = stream.live_since + stream.endpoint_at + CAUGHT_UP_S
            if (stream.answered_until >= stream.endpoint_at - CAUGHT_UP_S
                    or time.monotonic() >= min(stream.endpoint_by, caught_up_by)):
                stream.endpoint_at = None
                self._spawn(stream.provider.force_endpoint())

    @property
    def active_behind(self) -> bool:
        """The active stream is a candidate still catching up with its
        audio (its text so far lacks the newest words)."""
        stream = self._streams.get(self.active)
        return (self.active != self.default and stream is not None
                and stream.reports_confidence and stream.behind)

    @property
    def active_lag_s(self) -> float:
        """How far the active stream's answers trail the audio it was
        offered (0 for a provider without the ledger)."""
        stream = self._streams.get(self.active)
        if stream is None or not stream.reports_confidence:
            return 0.0
        return max(0.0, stream.offered_s - stream.answered_until)

    async def wait_answered(self, timeout: float) -> None:
        """Wait (bounded) until the active stream has answered all the audio
        it was offered up to now: a held endpoint sent and answered, no live
        partial left. Its finals stay queued for the caller's drain."""
        stream = self._streams.get(self.active)
        if stream is None:
            return
        target = stream.offered_s
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self._streams.get(self.active) is stream:
            self._send_held_endpoints()
            done = stream.endpoint_at is None and not stream.provider.latest_interim
            if done and (not stream.reports_confidence or stream.answered_until >= target - CAUGHT_UP_S):
                return
            await asyncio.sleep(0.05)

    def _spawn(self, coro) -> None:
        # The task runs the call itself: a wrapper cancelled before its first
        # step (a close right after a held endpoint went out) would leave the
        # call never awaited.
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._spawned_done)

    def _spawned_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()                 # a failed side call is quiet: the streams go on

    async def keepalive(self) -> None:
        for stream in self._live:
            await stream.provider.keepalive()

    async def feed_during_opening(self) -> None:
        for stream in self._live:
            await stream.provider.feed_during_opening()

    def on_tts_finished(self, was_interrupted: bool) -> None:
        for stream in self._live:
            stream.provider.on_tts_finished(was_interrupted)

    def clear_queue(self) -> None:
        for stream in self._live:
            stream.provider.clear_queue()

    def _watch_candidates(self) -> None:
        """A candidate whose socket died or raised a fatal error is dropped
        (the consumer may reopen one at the next lean)."""
        for lang, stream in list(self._streams.items()):
            if lang == self.active or not stream.live:
                continue
            fatal = stream.provider.pop_fatal_error()
            if fatal or not stream.provider.is_open:
                logger.warning("language stream %s died: %s", lang, fatal or "socket closed")
                self._drop(stream, "died")
                self._dropped.append(lang)
                self._pending_gone(lang)

    # ── Reads: the active stream ───────────────────────────────────

    def _drain_all(self) -> str | None:
        """Every stream's finals into its history; the active's new text
        back."""
        active_text = None
        for stream in list(self._streams.values()):
            if not stream.live:
                continue
            text = stream.provider.drain_transcript()
            if text and stream.discard_next_final:
                stream.discard_next_final = False
                logger.info("language stream %s: the final of the turn taken dropped (%d chars)", stream.language, len(text))
                continue
            if text:
                stream.history.append(text)
                if stream.language == self.active:
                    active_text = text
        return active_text

    def drain_transcript(self) -> str | None:
        text = self._drain_all()
        self._resolve_pending()
        return text

    async def wait_for_transcript(self, timeout: float = 1.0) -> str | None:
        deadline = time.monotonic() + timeout
        while True:
            text = self.drain_transcript()
            if text:
                return text
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(0.1, remaining))

    def pop_interim(self) -> str | None:
        """The dictation relay's live partial (duck-typed on the providers)."""
        self._resolve_pending()
        pop = getattr(self._active.provider, "pop_interim", None)
        return pop() if pop else None

    @property
    def latest_interim(self) -> str:
        return self._active.provider.latest_interim if self._streams else ""

    @property
    def display_interim(self) -> str:
        if not self._streams:
            return ""
        provider = self._active.provider
        return getattr(provider, "display_interim", None) or provider.latest_interim

    def bound_finish(self, seconds: float) -> None:
        # ``finish`` finishes the active stream; the others close.
        if self._streams:
            getattr(self._active.provider, "bound_finish", lambda _s: None)(seconds)

    @property
    def has_pending_finals(self) -> bool:
        return any(s.provider.has_pending_finals for s in self._live)

    @property
    def has_drainable_finals(self) -> bool:
        stream = self._streams.get(self.active)
        return stream is not None and stream.live and stream.provider.has_pending_finals

    @property
    def speech_evidence(self) -> bool:
        return any(bool(s.provider.latest_interim) or s.provider.has_pending_finals for s in self._live)

    @property
    def interim_evidence(self) -> str:
        return " | ".join(s.provider.latest_interim for s in self._live if s.provider.latest_interim)

    def pop_fatal_error(self) -> str | None:
        return self._active.provider.pop_fatal_error() if self._streams else None

    @property
    def last_result_monotonic(self) -> float:
        return self._active.provider.last_result_monotonic if self._streams else 0.0

    @property
    def is_open(self) -> bool:
        return bool(self._streams) and self._active.provider.is_open

    async def ensure_alive(self, language: str, *, clear_queue: bool = False) -> bool:
        """The ACTIVE stream's probe and reconnect, in its own language; a
        candidate's queue is cleared with it when asked."""
        if not self._streams:
            return False
        ok = await self._active.provider.ensure_alive(self.active, clear_queue=clear_queue)
        if clear_queue:
            for stream in self._live:
                if stream.language != self.active:
                    stream.provider.clear_queue()
        self._is_open = ok
        return ok

    async def finish(self) -> str | None:
        """The active stream's finish; the others close. A switch still
        pending is abandoned here, never resolved in silence: the consumer
        gave it its chance before (the relay's stop) and the stream the
        person saw is the one that finishes."""
        if self._pending is not None:
            logger.info("language switch to %s abandoned at the finish", self._pending)
            self._pending_gone(self._pending)
        self._speech_settled = False
        active = self._streams.get(self.active)
        for lang, stream in list(self._streams.items()):
            if lang != self.active:
                self._drop(stream, "finished")
        if active is None:
            return None
        text = await active.provider.finish()
        if text:
            active.history.append(text)
        return text

    async def close(self) -> None:
        self.closed = True
        self._is_open = False
        self._pending = None
        for task in list(self._tasks):
            task.cancel()
        streams, self._streams = list(self._streams.values()), {}
        for stream in streams:
            with contextlib.suppress(Exception):
                await stream.provider.close()
            self._closed_seconds[stream.language] = self._closed_seconds.get(stream.language, 0.0) + stream.seconds_sent
            self._closed_relisten += _relisten_seconds(stream.provider)
        for task in list(self._closing):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ── The providers' self-description, from the default stream ───

    def _describe(self, name: str, fallback):
        stream = self._streams.get(self.default) or next(iter(self._streams.values()), None)
        return getattr(stream.provider, name) if stream else fallback

    @property
    def endpointing_ms(self) -> int:
        return self._describe("endpointing_ms", 500)

    @property
    def needs_pre_connect(self) -> bool:
        return True

    @property
    def stay_open_between_turns(self) -> bool:
        return self._describe("stay_open_between_turns", True)

    @property
    def transcript_wait_timeout_s(self) -> float:
        return self._describe("transcript_wait_timeout_s", 1.0)

    @property
    def supports_early_unmute(self) -> bool:
        return self._describe("supports_early_unmute", True)

    @property
    def vad_silence_padding_ms(self) -> int:
        return self._describe("vad_silence_padding_ms", 550)

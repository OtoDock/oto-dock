"""The caller's language, read from the audio while they speak.

Mixin for :class:`pipeline.core.CallPipeline` and the duplex session. A
surface that detects builds its STT as :class:`LanguageStreams` (the
default language's stream, the active one after a switch and at most one
more candidate, one provider to the rest of the pipeline) and a :class:`LanguageTracker` that runs the local
identifier on the voiced audio the active stream receives. The tracker's
events are applied between frames, on the loop:

- ``open``: a candidate stream opens (after the stream can be opened in
  that language and the line has a voice for it) with the turn's audio
  replayed into it;
- ``switch``: the read surface moves once the candidate's words beat the
  shown stream's (held while they are even, then the old stream stays; on a
  provider that reports no word confidence: once it has answered,
  ``SWITCH_READY_S`` at most); the resolved switch rewrites the turn's
  STT segments with the new stream's words and, on a call, moves the locked
  language, the voice and the fillers (at SURE; a later SURE reading may
  move them again before the final); live voice moves the language it
  speaks and classifies in at the turn's take or at the final, since a lean
  may flip back;
- the words' outcomes: a switch the shown stream's words win is refused
  (the rule goes back, a decisive refusal finalises), and a resolved switch
  the default's words decisively beat since is called back;
- ``final``: the losing stream closes and the language stands for the call.

Every site that consumes a turn calls ``_note_turn_taken``: the streams'
cursors move, the replay starts over (and is trimmed to a pre-roll at the
turn's first voice, so a candidate replays the speech, not the listening
before it: the vendor works through a replay at about real-time pace), every
candidate but the active one closes and a switch still pending to it is
abandoned (a queued turn never waits). On a call the dispatch's take also
asks the rule (a queued turn never does): at the first one a leader at LEAN
wins once, so the caller's first words reach the agent in their language
(the dispatch waits, bounded, for the switch, the speech having ended and
every stream been finalised, and falls back to the default's text, then for
the new stream's final when the switch resolved inside the turn). Live
voice's dispatch takes the same way until the final, with no take verdict.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from audio.providers.lid.ecapa import (
    EcapaIdentifier,
    identifier_available,
    loaded_model,
)
from audio.providers.lid.rule import FINAL, OPEN, SURE, SWITCH, Event, LanguageRule
from audio.providers.lid.tracker import LanguageTracker
from audio.providers.stt.relisten import TURN_RELISTEN_WAIT_S
from audio.providers.stt.streams import SWITCH_WAIT_MAX_S, LanguageStreams
from audio.providers.vad.base import VadState

from background import spawn
from fillers import filler_cache

from .providers import filler_key_for

logger = logging.getLogger("pipeline")

#: How long a dispatch waits for the new stream's final after a switch
#: resolved inside the turn (on the candidate's first result, often a
#: partial): the agent gets the words once, from the stream that heard them.
#: A new stream still working through its replay gets the longer wait. Every
#: wait of a take shares ``TAKE_BUDGET_S`` (the switch, the catch-up, the
#: final): the caller hears nothing meanwhile.
TAKE_FINAL_WAIT_S = 1.0
TAKE_CATCH_UP_S = 2.0
TAKE_BUDGET_S = 3.0
#: The audio before a turn's first voice a candidate's replay keeps.
REPLAY_PREROLL_S = 0.5


class LanguageMixin:
    """One rule, one switch surface, one take fence."""

    #: The surface's parameters: a call switches at SURE and lets a lean win
    #: at the first take, and what is heard follows the switch; live voice
    #: switches at LEAN (and may switch again) and changes what is heard at
    #: the take or the final.
    _language_switch_at: float = SURE
    _language_lean_at_take: bool = True
    _heard_follows_switch: bool = True

    _language_tracker: LanguageTracker | None = None
    _language_streams: LanguageStreams | None = None
    _heard_language: str = ""
    _language_voiced_since_take: bool = False

    def _language_stt(self, build, default: str, rate_in: int):
        """The surface's STT when it detects the language: the streams and
        the tracker, or the plain provider when the identifier is off,
        unavailable, or has no candidate to choose from."""
        self._heard_language = default
        candidates = [lang for lang in self.cfg.language_candidates if lang != default]
        if not (self.cfg.language_identify_enabled and candidates and identifier_available()):
            if not candidates:
                logger.info("[%s] language detection off: no candidate beside %s", self.conn.peer_addr, default)
            elif not self.cfg.language_identify_enabled:
                logger.info("[%s] language detection is off (the admin switched it off)", self.conn.peer_addr)
            else:
                logger.info("[%s] language detection off: the identifier is unavailable", self.conn.peer_addr)
            return build()
        # The constructor runs on the loop: the model is the pre-warmed one
        # or the surface runs without detection (never a load here).
        model = loaded_model(num_threads=self.cfg.language_identify_threads)
        if model is None:
            logger.info("[%s] language detection off: the identifier is not loaded yet", self.conn.peer_addr)
            return build()
        try:
            identifier = EcapaIdentifier(model=model)
            streams = LanguageStreams(build, default)
            rule = LanguageRule(default, candidates, switch_at=self._language_switch_at,
                                lean_at_take=self._language_lean_at_take)
            tracker = LanguageTracker(identifier, rule, rate_in=rate_in)
        except Exception as e:  # noqa: BLE001  # onnxruntime loading or running a model raises anything
            # A language the model does not know (a route row written by
            # hand), a rate the tracker does not take: the plain provider.
            logger.warning("[%s] language detection off: %s", self.conn.peer_addr, e)
            return build()
        self._language_tracker = tracker
        self._language_streams = streams
        return streams

    # ── The live feed ──────────────────────────────────────────────

    async def _stt_feed(self, payload: bytes, *, voiced: bool | None = None) -> None:
        """The listen loop's live feed: the stream and the tracker hear the
        same bytes, so the agent's own voice (fed as silence during
        playback) never reaches the identifier."""
        tracker = self._language_tracker
        detecting = tracker is not None and not tracker.rule.done
        if detecting:
            if voiced is None:
                voiced = self.vad.state == VadState.SPEAKING
            if voiced and not self._language_voiced_since_take:
                # The turn's first voice: a candidate replays from a pre-roll
                # before it, not the whole listening since the last take.
                self._language_voiced_since_take = True
                self._language_streams.trim_replay(REPLAY_PREROLL_S)
        await self.stt.send_audio(payload)
        if detecting:
            if self.state._language_onset == 0.0 and voiced:
                self.state._language_onset = time.monotonic()
            tracker.feed(payload, voiced=voiced)

    def _language_poll(self) -> None:
        """Between frames: the tracker's events, a resolved switch, a dropped
        candidate, and the rule's view reconciled with the streams'."""
        tracker, streams = self._language_tracker, self._language_streams
        if tracker is None or streams is None:
            return
        self._language_resolved()
        for event in tracker.poll():
            self._language_event(event)
        self._language_words()
        self._language_resolved()
        if self.state._language_final_pending and streams.pending is None:
            self.state._language_final_pending = False
            self._language_final(streams.active)
        for lang in streams.dropped():
            self._language_dropped(lang)
        if tracker.rule.current != streams.active and streams.pending is None and tracker.rule.final is None:
            logger.info("[%s] language switch to %s did not happen, "
                        "%s stays",
                        self.conn.peer_addr, tracker.rule.current, streams.active)
            tracker.rule.revert(streams.active)

    def _language_resolved(self) -> None:
        resolved = self._language_streams.switched()
        if resolved is not None:
            self._language_switched(*resolved)

    def _language_words(self) -> None:
        """The words' outcomes: a refused switch goes back (a decisive
        refusal finalises, through the same hold as any final), a call-back
        switches to the default."""
        tracker, streams = self._language_tracker, self._language_streams
        refused = streams.refused()
        if refused is not None:
            language, verdict = refused
            events = tracker.rule.refused(language, decisive=verdict.decisive)
            final = any(event.kind == FINAL for event in events)
            logger.info("[%s] language switch to %s refused by the words"
                        "%s, %s stays",
                        self.conn.peer_addr, language, ', final' if final else '', streams.active)
            for event in events:
                self._language_event(event)
        called = streams.called_back()
        if called is not None:
            for event in tracker.rule.words_switch(called):
                self._language_event(event)

    def _language_event(self, event: Event) -> None:
        tracker, streams = self._language_tracker, self._language_streams
        if event.kind == OPEN:
            if not (streams.streams_language(event.language) and self.tts.has_voice_for(event.language)):
                logger.info("[%s] language %s: no stream or voice for it, kept out", self.conn.peer_addr, event.language)
                tracker.rule.unavailable(event.language)
                return
            # Registered now, connected by the task: a switch in the same
            # batch (a short first answer) finds the stream and goes pending.
            if not streams.register(event.language):
                return
            task = self.state._language_open_task
            if task is not None and not task.done():
                task.cancel()
            self.state._language_open_task = asyncio.create_task(self._language_open(event.language))
        elif event.kind == SWITCH:
            if not streams.switch(event.language):
                tracker.rule.revert(streams.active)
        elif event.kind == FINAL:
            # A switch still pending settles once its candidate answered (or
            # was abandoned): the final language is whatever is active then.
            if streams.pending is not None:
                self.state._language_final_pending = True
            else:
                self._language_final(streams.active)

    async def _language_open(self, language: str) -> None:
        streams = self._language_streams
        t0 = time.monotonic()
        ok = await streams.open(language)
        if ok:
            self.state._language_opened_at = self.state._language_opened_at or self._since_onset()
            logger.info("[%s] language candidate %s open "
                        "(%.0f ms, %.1f s after the first words)",
                        self.conn.peer_addr, language, (time.monotonic() - t0) * 1000,
                        self._since_onset())

    def _language_dropped(self, language: str) -> None:
        """A candidate failed to open or died: once it may come back at the
        next lean, twice it is out for the call."""
        tracker = self._language_tracker
        failures = self.state._language_failures
        failures[language] = failures.get(language, 0) + 1
        if failures[language] >= 2:
            tracker.rule.unavailable(language)
            logger.info("[%s] language %s failed twice, kept out", self.conn.peer_addr, language)
        else:
            tracker.rule.candidate_closed()

    def _since_onset(self) -> float:
        onset = self.state._language_onset
        return time.monotonic() - onset if onset else 0.0

    # ── A switch that resolved ─────────────────────────────────────

    def _language_switched(self, language: str, text: str) -> None:
        """The read surface moved: the turn's STT words are the new
        stream's, and on a call the locked language and the voice follow
        (live voice moves them at the take or the final)."""
        if self._heard_follows_switch:
            self._locked_language = language
        self.state._language_switched_at = self._since_onset()
        self.state._language_turn_switched = True
        self.state._turn_segments = list(self.state._turn_typed) + ([text] if text else [])
        logger.info("[%s] language switched to %s "
                    "%.1f s after the first words"
                    "%s",
                    self.conn.peer_addr, language, self._since_onset(),
                    '' if text else ', no words yet')
        if self._heard_follows_switch:
            self._language_apply_heard(language)
        self._on_language_replace(" ".join(self.state._turn_segments))

    def _on_language_replace(self, text: str) -> None:
        """The shown transcript changed under the person (the duplex session
        sends the ``replace`` frame)."""

    def _language_apply_heard(self, language: str) -> None:
        """What the person hears follows: the locked language (the voice's,
        the turn classifier's), the voice, the fillers."""
        self._locked_language = language
        if language == self._heard_language:
            return
        self._heard_language = language
        self._select_tts(language)
        self._filler_key = filler_key_for(
            self._route_settings.tts_provider, self.tts, language, self._rate_out)
        spawn(self._warm_fillers_for_language(), name="warm_fillers_for_language", owner="call")

    async def _warm_fillers_for_language(self) -> None:
        try:
            await filler_cache.ensure(
                self._filler_key, self.tts,
                backchannel_phrases=self.cfg.backchannel_phrases,
                thinking_phrases=self.cfg.thinking_phrases,
            )
        except Exception as e:  # noqa: BLE001  # fillers are optional, the call runs without them
            logger.warning("[%s] filler re-key after the language switch failed: %s", self.conn.peer_addr, e)

    def _language_final(self, language: str) -> None:
        streams, tracker = self._language_streams, self._language_tracker
        streams.settle()
        tracker.rule.final = language
        tracker.rule.current = language
        self.state._language_locked = True
        self._language_apply_heard(language)
        st = self.state
        logger.info(
            "[%s] language final: %s"
            "%s, "
            "candidate opened at %.1f s, switched at %.1f s, "
            "final at %.1f s after the first words, "
            "%.1f s of extra stream",
            self.conn.peer_addr, language, ' (the default)' if language == streams.default else '',
            st._language_opened_at, st._language_switched_at, self._since_onset(),
            streams.extra_seconds)

    # ── The take fence ─────────────────────────────────────────────

    def _note_turn_taken(self) -> str:
        """A turn's segments went out (dispatched or queued): every stream's
        cursor moves, the replay starts over, every candidate but the active
        one closes (a switch still pending to it abandoned); on live voice
        what is heard catches up with what is shown. Returns the active
        stream's text the take drained (a final that landed after the
        caller's own drain): only the dispatch's take adds it to the turn,
        every other site drops it with the turn it consumed."""
        self.state._turn_typed.clear()
        self.state._language_turn_switched = False
        self._language_voiced_since_take = False
        turn_taken = getattr(self.stt, "turn_taken", None)
        if turn_taken is not None:
            turn_taken()
        streams, tracker = self._language_streams, self._language_tracker
        if streams is None or tracker is None:
            return ""
        if streams.take():
            tracker.rule.candidate_closed()
        if not self._heard_follows_switch and tracker.rule.final is None:
            self._language_apply_heard(streams.active)
        return streams.drained_at_take()

    async def _language_take(self, joined: str) -> str:
        """The dispatch's take: on a call the first turn's lean leader wins
        once (the switch awaited, bounded, with the speech ended and every
        stream finalised, so one word on a side is evidence; the default's
        words go out when it never resolves), then the fence. A switch still
        pending while a final waits on it (``_language_final_pending``) gets
        the same wait, so the call settles on the language the words chose.
        The active stream's final that landed after the dispatch's own drain
        joins the turn; an active stream from an earlier turn's switch that
        still trails its audio is waited for first, final or not (its words
        of this turn would seed the next). Every wait shares
        ``TAKE_BUDGET_S``. Returns the text to dispatch."""
        tracker, streams = self._language_tracker, self._language_streams
        t0 = time.monotonic()
        deadline = t0 + TAKE_BUDGET_S

        def left(bound: float) -> float:
            return max(0.0, min(bound, deadline - time.monotonic()))

        waited = False
        if tracker is not None and streams is not None and (not tracker.rule.done or streams.pending is not None):
            waited = True
            streams.settle_point()
            try:
                for event in await tracker.take():
                    self._language_event(event)
                self._language_poll()
                await streams.wait_switched(left(SWITCH_WAIT_MAX_S))
                self._language_poll()
                if self.state._language_turn_switched:
                    await self._language_turn_final(left)
                elif streams.active_behind:
                    await self._language_catch_up(left)
            except asyncio.CancelledError:
                streams.settle_point(False)
                raise
            joined = " ".join(self.state._turn_segments) or joined
        elif streams is not None and streams.active_behind:
            waited = True
            await self._language_catch_up(left)
            joined = " ".join(self.state._turn_segments) or joined
        if waited:
            logger.info("[%s] language take: %.1f s waited, "
                        "%s dispatched",
                        self.conn.peer_addr, time.monotonic() - t0, streams.active)
        joined = await self._turn_relisten(joined, t0)
        drained = self._note_turn_taken()
        if drained and not self._is_interim_fallback_dup(drained):
            self._append_turn_segment(drained)
            joined = " ".join(self.state._turn_segments) or drained
        return joined

    async def _turn_relisten(self, joined: str, t0: float) -> str:
        """The turn's text with the words its second listen recovered from
        windows the live stream answered empty: what is still out is waited
        for at most ``TURN_RELISTEN_WAIT_S`` from the take's start (the
        language waits count against it); a turn without such a window
        waits nothing. The segments become the completed text."""
        complete = getattr(self.stt, "complete_turn", None)
        if complete is None or not joined:
            return joined
        bound = max(0.0, TURN_RELISTEN_WAIT_S - (time.monotonic() - t0))
        try:
            text = await complete(joined, bound)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  # the second listen never costs the turn its live words
            logger.warning("[%s] call re-listen: the turn goes as heard: %s", self.conn.peer_addr, e)
            return joined
        if text and text != joined:
            logger.info("[%s] call re-listen completed the turn: %s", self.conn.peer_addr, text)
            self.state._turn_segments = [text]
            return text
        return joined

    async def _language_turn_final(self, left: Callable[[float], float]) -> None:
        """The switch resolved inside this turn, on the new stream's first
        result, often a partial: its final for the turn is awaited (bounded,
        ``left`` gives what remains of the take's budget; a new stream still
        working through its replay is waited for until it has caught up) so
        the agent gets those words now and not again as the next turn's. The
        turn's segments become the new stream's text. A final still in flight
        at the bound is dropped when it lands."""
        streams = self._language_streams
        text = streams.text_since_take()
        if not text or streams.has_drainable_finals or streams.latest_interim or streams.active_behind:
            with contextlib.suppress(Exception):
                await streams.force_endpoint()
            if streams.active_behind:
                # Still working through its replay: its words of this turn
                # come as it catches up (its endpoint is held until then).
                await streams.wait_answered(left(TAKE_CATCH_UP_S))
                late = streams.drain_transcript()
            else:
                late = await streams.wait_for_transcript(left(TAKE_FINAL_WAIT_S))
            if late is None and (streams.latest_interim or streams.active_behind):
                streams.discard_next_final(TAKE_FINAL_WAIT_S)
            text = streams.text_since_take()
        self.state._turn_segments = list(self.state._turn_typed) + ([text] if text else [])

    async def _language_catch_up(self, left: Callable[[float], float]) -> None:
        """The active stream is a candidate an earlier turn's switch moved to
        and it still trails its audio: it is waited for (bounded) until it has
        answered what it was offered, so its words of this turn come out of
        the take's own drain and join the dispatch like any late final (with
        the interim fallback's check), never the next turn. The pipeline's
        segments stay as dispatched when nothing lands, and the stream's next
        final is then marked dropped. A switch that resolved meanwhile (the
        default's words calling the active stream back) is the new stream's
        turn: its final is waited for instead."""
        streams = self._language_streams
        active, lag, t0 = streams.active, streams.active_lag_s, time.monotonic()
        with contextlib.suppress(Exception):
            await streams.force_endpoint()
        await streams.wait_answered(left(TAKE_CATCH_UP_S))
        if streams.active != active:
            self._language_poll()
            if self.state._language_turn_switched:
                await self._language_turn_final(left)
            return
        if not streams.has_drainable_finals and (streams.latest_interim or streams.active_behind):
            streams.discard_next_final(TAKE_FINAL_WAIT_S)
        logger.info("[%s] language take: %s trailed its audio by %.1f s, "
                    "caught up in %.1f s%s",
                    self.conn.peer_addr, active, lag, time.monotonic() - t0,
                    '' if streams.has_drainable_finals else ', nothing more to add')

    # ── Teardown ───────────────────────────────────────────────────

    async def _language_cleanup(self) -> None:
        tracker = self._language_tracker
        if tracker is not None:
            tracker.close()
        task = self.state._language_open_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.state._language_open_task = None

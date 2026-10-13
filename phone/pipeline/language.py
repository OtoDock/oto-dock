"""The caller's language, identified once per call and locked.

Mixin for :class:`pipeline.core.CallPipeline`. A route with
``language_detect`` starts the call in the route's language (the greeting,
the fillers, the stream) and, on the first speech segment with enough voiced
audio, sends that audio to the STT provider's batch identification in its
own task. A confident answer in a language the stream can transcribe and
the line has a voice for locks the call to it: the voice switches, the
fillers re-key, the stream reopens in the new language (frames that arrive
while it reopens are kept and replayed) and, when the language changed, the
first turn's text is the identification's. Anything else keeps the route's
language; after three attempts the route's language locks. The lock is never
revisited: no mid-call flip-flop.
"""

import asyncio
import contextlib
import logging
import time

from audio.streaming.lang import base_lang, pcm16_to_wav
from fillers import filler_cache
from .providers import filler_key_for

logger = logging.getLogger("pipeline")

#: Voiced audio needed before an identification is attempted (one word is
#: no evidence); attempts before the route's language locks for good.
MIN_IDENTIFY_SECONDS = 1.2
MAX_ATTEMPTS = 3
#: Confidence to confirm the route's language, and to move away from it (a
#: move also needs two words). How long a dispatch waits for a running
#: identification before it goes ahead with the stream's text.
CONFIRM_CONFIDENCE = 0.6
MOVE_CONFIDENCE = 0.8
DISPATCH_WAIT_S = 2.5
#: Frames kept while the stream reopens (2 s of 20 ms frames at 8 kHz).
RESTART_BUFFER_MAX_BYTES = 2 * 8000 * 2


class LanguageMixin:
    """One identification, one lock."""

    def _language_detect_pending(self) -> bool:
        return (not self.state._language_locked
                and self.state._language_attempts < MAX_ATTEMPTS)

    def _consider_language_identification(self) -> None:
        """At a speech end: enough voiced audio and no identification
        running starts one on the buffered utterance."""
        if not self._language_detect_pending():
            return
        task = self.state._language_task
        if task is not None and not task.done():
            return
        audio = bytes(self.state._speech_audio_buf)
        seconds = len(audio) / float(self._rate_in * 2)
        if seconds < MIN_IDENTIFY_SECONDS:
            return
        self.state._language_identify_gen = self.state._language_turn_gen
        self.state._language_task = asyncio.create_task(self._identify_language(
            audio, covers_turn=not self.state._speech_audio_truncated))

    async def _identify_language(self, pcm: bytes, *, covers_turn: bool = True) -> None:
        route_lang = base_lang(self.route.language) or "en"
        self.state._language_attempts += 1
        attempt = self.state._language_attempts
        t0 = time.monotonic()
        try:
            guess = await self.stt.identify_language(pcm16_to_wav(pcm, self._rate_in))
        except Exception as e:
            logger.warning(f"[{self.conn.peer_addr}] language identification failed: {e}")
            guess = None
        took = (time.monotonic() - t0) * 1000
        if guess is None or not guess.language:
            logger.info(
                f"[{self.conn.peer_addr}] language: no answer (attempt {attempt}, {took:.0f}ms)")
            self._lock_route_language_if_exhausted(route_lang)
            return
        lang = base_lang(guess.language)
        words = len((guess.text or "").split())
        if lang == route_lang:
            if guess.confidence >= CONFIRM_CONFIDENCE:
                self.state._language_locked = True
                logger.info(
                    f"[{self.conn.peer_addr}] language kept: {lang} "
                    f"({guess.confidence:.2f}, {took:.0f}ms)")
            else:
                logger.info(
                    f"[{self.conn.peer_addr}] language unsure: {lang} "
                    f"({guess.confidence:.2f}), attempt {attempt}")
                self._lock_route_language_if_exhausted(route_lang)
            return
        speaks = self.stt.streams_language(lang) and self.tts.has_voice_for(lang)
        if guess.confidence >= MOVE_CONFIDENCE and words >= 2 and speaks:
            # The identification's text stands in for the turn's only when
            # it heard the whole turn (the buffer kept every frame) and that
            # turn has not been dispatched meanwhile.
            await self._apply_language_lock(lang, first_text=guess.text if covers_turn else "")
            logger.info(
                f"[{self.conn.peer_addr}] language locked: {lang} "
                f"({guess.confidence:.2f}, {words} words, {took:.0f}ms"
                f"{'' if covers_turn else ', the stream keeps the text'})")
            return
        reason = ("the line has no voice or model for it" if not speaks
                  else f"confidence {guess.confidence:.2f}, {words} words")
        logger.info(
            f"[{self.conn.peer_addr}] language kept: {route_lang} "
            f"(guess {lang}, {reason}), attempt {attempt}")
        self._lock_route_language_if_exhausted(route_lang)

    def _lock_route_language_if_exhausted(self, route_lang: str) -> None:
        if self.state._language_attempts >= MAX_ATTEMPTS:
            self.state._language_locked = True
            logger.info(f"[{self.conn.peer_addr}] language locked: {route_lang} (route, after {MAX_ATTEMPTS} attempts)")

    async def _apply_language_lock(self, lang: str, *, first_text: str) -> None:
        """Switch the call to ``lang``: the voice, the fillers, the stream."""
        self.state._language_locked = True
        self._locked_language = lang
        state = self.state
        state._language_first_text = (
            (first_text or "").strip()
            if state._language_identify_gen == state._language_turn_gen else "")
        self._select_tts(lang)
        self._filler_key = filler_key_for(
            self._route_settings.tts_provider, self.tts, lang, self._rate_out)
        asyncio.create_task(self._warm_fillers_for_lock())
        if self.state._stt_active:
            await self._restart_stt(lang)

    async def _warm_fillers_for_lock(self) -> None:
        try:
            await filler_cache.ensure(
                self._filler_key, self.tts,
                backchannel_phrases=self.cfg.backchannel_phrases,
                thinking_phrases=self.cfg.thinking_phrases,
            )
        except Exception as e:
            logger.warning(f"[{self.conn.peer_addr}] filler re-key after the language lock failed: {e}")

    async def _restart_stt(self, lang: str) -> None:
        """Reopen the stream in ``lang``; the caller's frames meanwhile are
        kept and replayed so a word spoken during the reopen is not lost."""
        self.state._stt_restarting = True
        self.state._stt_restart_buffer.clear()
        try:
            ok = await self.stt.restart(lang)
        except Exception as e:
            logger.warning(f"[{self.conn.peer_addr}] STT restart in {lang} failed: {e}")
            ok = False
        buffered = bytes(self.state._stt_restart_buffer)
        self.state._stt_restart_buffer.clear()
        self.state._stt_restarting = False
        if not ok:
            return
        if buffered:
            with contextlib.suppress(Exception):
                await self.stt.send_audio(buffered)
        self.state._stt_last_fed = time.monotonic()

    async def _stt_feed(self, payload: bytes) -> None:
        """The listen loop's live feed: buffered while the stream reopens."""
        if self.state._stt_restarting:
            buf = self.state._stt_restart_buffer
            buf.extend(payload)
            if len(buf) > RESTART_BUFFER_MAX_BYTES:
                del buf[:len(buf) - RESTART_BUFFER_MAX_BYTES]
            return
        await self.stt.send_audio(payload)

    async def _await_language_identification(self) -> None:
        """A dispatch waits (bounded) for a running identification: the
        first turn's text may be the identification's."""
        task = self.state._language_task
        if task is None or task.done():
            return
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=DISPATCH_WAIT_S)

    def _take_language_first_text(self) -> str:
        """The identification's text for the turn being dispatched, once; the
        dispatch moves the generation on, so an identification that lands
        later keeps nothing for the next turn."""
        state = self.state
        text = (state._language_first_text
                if state._language_identify_gen == state._language_turn_gen else "")
        self._note_turn_taken()
        return text

    def _note_turn_taken(self) -> None:
        """A turn's segments went out (dispatched or queued)."""
        self.state._language_first_text = ""
        self.state._language_turn_gen += 1

"""Turn accumulation and turn-completeness classification.

Mixin for :class:`pipeline.core.CallPipeline`; operates on ``self.state``
(CallState) plus the injected collaborators (conn/cfg/stt/vad/tts/llm).
"""

import asyncio
import logging
import time
import re
import numpy as np

from .markers import _CALL_COMPLETE_RE, _QUESTION_RE

logger = logging.getLogger("pipeline")


class TurnMixin:
    """Turn accumulation and turn-completeness classification."""

    def _speech_audio_16k(self, pcm_bytes: bytes) -> np.ndarray:
        """Speech buffer as 16 kHz float32 for Smart Turn (model rate).

        8 kHz telephony audio is upsampled 2x by linear interpolation (no
        scipy); a 16 kHz transport is already at model rate — plain convert.
        """
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        n = len(samples)
        if n == 0 or self._rate_in != 8000:
            return samples
        # 2x upsampling via linear interpolation
        indices = np.arange(2 * n) * 0.5
        orig_indices = np.arange(n, dtype=np.float32)
        samples_16k = np.interp(indices, orig_indices, samples)
        return samples_16k.astype(np.float32)

    def _build_classifier_context(self) -> str:
        """Build conversation context from voice-aware call transcript.

        Uses _call_transcript which tracks what was actually said/heard
        in the call, rather than full LLM responses from llm.messages.
        Responses that were never played via TTS are excluded, and
        interrupted responses are marked as such.
        """
        # Last 6 entries ≈ up to 3 complete turns
        recent = self.state._call_transcript[-6:]
        context_lines = []
        for entry in recent:
            role = entry["role"]
            content = entry["content"]
            if role == "user":
                context_lines.append(f"User: {content}")
            elif role == "assistant":
                if entry.get("interrupted"):
                    context_lines.append(f"Assistant: {content} (interrupted by caller)")
                else:
                    context_lines.append(f"Assistant: {content}")
        return "\n".join(context_lines)

    def _add_assistant_transcript(self) -> None:
        """Add assistant entry to voice-aware call transcript based on TTS state."""
        if not self.state._full_response:
            return
        # Clean response text (strip session markers and call markers)
        content = re.sub(r"\[//\]: # \(session:[a-f0-9-]+\)", "", self.state._full_response).strip()
        content = _CALL_COMPLETE_RE.sub("", content)
        content = _QUESTION_RE.sub("", content).strip()
        if not content:
            return
        # Truncate for classifier context
        if len(content) > 300:
            content = content[:300] + "..."
        if not self.state._tts_ever_played:
            # Response was never played — don't add to transcript
            logger.debug(
                f"[{self.conn.peer_addr}] Voice transcript: skipping assistant "
                f"(never played): {content[:60]}"
            )
            return
        entry = {
            "role": "assistant",
            "content": content,
            "interrupted": self.state._utterance_cancelled,
        }
        self.state._call_transcript.append(entry)
        logger.debug(
            f"[{self.conn.peer_addr}] Voice transcript += assistant "
            f"({'interrupted' if self.state._utterance_cancelled else 'full'}): "
            f"{content[:80]}"
        )

    async def _classify_turn(self, segments: list[str]) -> float:
        """Classify turn completeness using the appropriate backend.

        Routes to Smart Turn (audio-based) or Groq (text-based) depending
        on language, via TurnClassifierDispatcher.

        Returns the timeout to wait before dispatching:
          - TURN_CLASSIFIER_GRACE_S (0.2s) for complete turns
          - TURN_INCOMPLETE_TIMEOUT_S (4s) for incomplete turns
        """
        if self._turn_clf is not None:
            try:
                context = self._build_classifier_context()
                language = self._locked_language

                # Prepare audio if Smart Turn backend needs it
                audio_16k = None
                if self._turn_clf.needs_audio(language) and self.state._speech_audio_buf:
                    audio_16k = self._speech_audio_16k(bytes(self.state._speech_audio_buf))

                # Format segments as numbered list for text-based backends
                if len(segments) > 1:
                    speech_text = "\n".join(
                        f"[{i + 1}] {seg}" for i, seg in enumerate(segments)
                    )
                else:
                    speech_text = segments[0]

                is_complete = await self._turn_clf.classify(
                    text=speech_text,
                    context=context,
                    language=language,
                    audio_16k=audio_16k,
                )
                if is_complete is not None:
                    if is_complete:
                        return self.cfg.turn_classifier_grace_s
                    else:
                        return self.cfg.turn_incomplete_timeout_s
                # is_complete is None → fall through to heuristic
            except Exception as e:
                logger.warning(
                    f"[{self.conn.peer_addr}] Classifier failed, using heuristic: {e}"
                )
        # No classifier available — use complete timeout as safe default
        return self.cfg.turn_complete_timeout_s

    async def _wait_for_complete_continuation(self, combined: str) -> str:
        """After detecting continuation, wait until the combined turn is complete.

        Re-classifies the turn (Smart Turn for audio, Groq for text) and
        waits for more speech if incomplete.  Batches all fragments into
        a single combined text before returning, avoiding multiple LLM
        round-trips for fragmented speech.

        Returns the final combined transcript ready for LLM dispatch.
        """
        transcript = combined

        for attempt in range(5):  # safety cap
            # Wait for user silence
            if not self.state._user_silent.is_set():
                logger.info(
                    f"[{self.conn.peer_addr}] Continuation: "
                    f"waiting for user to finish speaking"
                )
                try:
                    await asyncio.wait_for(
                        self.state._user_silent.wait(), timeout=5.0,
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    break

            # Wait up to 1.0s for delayed queue retries to deliver
            # (matches _delayed_queue_retry's 1.0s timeout).  Break early
            # as soon as transcript arrives — no unnecessary waiting.
            for _poll in range(10):  # 10 x 0.1s = 1.0s max
                if self.state._queued_speech:
                    break
                await asyncio.sleep(0.1)

            # Gather any additional queued speech
            if self.state._queued_speech:
                extra = " ".join(self.state._queued_speech)
                self.state._queued_speech.clear()
                transcript = f"{transcript} {extra}"
                logger.debug(
                    f"[{self.conn.peer_addr}] Continuation batch: "
                    f"+'{extra}' → {transcript}"
                )

            # Drain late STT transcripts
            if self.state._stt_active:
                late = self.stt.drain_transcript()
                if late:
                    transcript = f"{transcript} {late}"
                    logger.debug(
                        f"[{self.conn.peer_addr}] Continuation drain: "
                        f"+'{late}' → {transcript}"
                    )

            # Classify the combined turn
            timeout = await self._classify_turn([transcript])
            label = "complete" if timeout <= self.cfg.turn_classifier_grace_s else "incomplete"
            logger.debug(
                f"[{self.conn.peer_addr}] Continuation re-classify: "
                f"{label} — {transcript[:80]}"
            )

            if timeout <= self.cfg.turn_classifier_grace_s:
                # Complete — but recheck: if user started speaking again
                # or new speech arrived during classification, don't
                # dispatch yet — loop again to gather it.
                if not self.state._user_silent.is_set() or self.state._queued_speech:
                    logger.info(
                        f"[{self.conn.peer_addr}] Continuation: classified "
                        f"complete but user speaking or new speech queued "
                        f"— continuing to gather"
                    )
                    continue
                break

            # Incomplete — wait for more speech
            try:
                await asyncio.sleep(timeout)
            except asyncio.CancelledError:
                break

            # After wait, check for new speech
            has_new = False
            if self.state._queued_speech:
                extra = " ".join(self.state._queued_speech)
                self.state._queued_speech.clear()
                transcript = f"{transcript} {extra}"
                has_new = True
            if self.state._stt_active:
                late = self.stt.drain_transcript()
                if late:
                    transcript = f"{transcript} {late}"
                    has_new = True

            if not has_new:
                # No new speech after timeout — send what we have
                logger.info(
                    f"[{self.conn.peer_addr}] Continuation: no new speech "
                    f"after timeout, dispatching"
                )
                break

        return transcript

    async def _is_queued_continuation(
        self, original: str, follow_up: str,
    ) -> bool:
        """Check if queued speech is a continuation of the just-dispatched turn.

        Uses Groq to classify. Returns True if the follow-up should be
        discarded (it's just adding detail to the original request which
        the AI is already handling).
        """
        if self._turn_clf is None:
            return False
        try:
            context = self._build_classifier_context()
            return await self._turn_clf.classify_continuation(
                original, follow_up, context,
            )
        except Exception as e:
            logger.warning(
                f"[{self.conn.peer_addr}] Continuation classifier failed: {e}"
            )
            return False  # fail-open: treat as new request

    def _turn_busy(self) -> bool:
        """A turn is being generated RIGHT NOW. Subclasses with extra turn
        lanes (the duplex typed turn) override to include them — every
        busy/dispatch check must see every lane, or two turn state machines
        end up sharing ``_tts_task``/``_queued_speech`` (audit F4,
        2026-08-24)."""
        return bool(self.state._utterance_task
                    and not self.state._utterance_task.done())

    def _is_interim_fallback_dup(self, text: str) -> bool:
        """True when ``text`` is the late-arriving REAL final of an
        utterance a dispatch already consumed via the INTERIM fallback
        (recorded at the flush sites). The fallback exists for LOST finals
        — but the live-hit final was merely ~1 s late, landed unconsumed,
        and re-dispatched the same word as its own turn (Paros
        double-send, 2026-08-25). Normalized exact match inside a tight
        window; one-shot so a deliberately repeated word later still
        dispatches."""
        recorded = self.state._interim_fallback_text
        if not recorded:
            return False
        if time.monotonic() - self.state._interim_fallback_at > 2.0:
            return False

        def norm(s: str) -> list[str]:
            return "".join(
                ch if ch.isalnum() or ch.isspace() else " "
                for ch in s.lower()).split()

        if norm(text) == norm(recorded):
            self.state._interim_fallback_text = ""
            return True
        return False

    async def _turn_timeout_handler(self, text: str) -> None:
        """Classify turn completeness, then wait appropriate timeout before dispatch."""
        try:
            # Use current segments (may have grown since timer was created)
            segments = list(self.state._turn_segments) if self.state._turn_segments else [text]
            t_verdict = time.monotonic()
            timeout = await self._classify_turn(segments)
            verdict_ms = (time.monotonic() - t_verdict) * 1000
            label = "complete" if timeout <= self.cfg.turn_classifier_grace_s else "incomplete"
            logger.info(
                f"[{self.conn.peer_addr}] Turn looks {label} "
                f"(verdict {verdict_ms:.0f}ms) — waiting {timeout}s"
            )

            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return

        # Guard: if user started speaking during the sleep, wait for
        # silence before dispatching.  SPEECH_START normally cancels the
        # turn timer, but if the timer was created by _delayed_drain_retry
        # after SPEECH_START, it can't be cancelled.
        if not self.state._user_silent.is_set():
            logger.info(
                f"[{self.conn.peer_addr}] Turn timer: user speaking — "
                f"waiting for silence before dispatch"
            )
            # Two-stage wait with a phantom short-circuit (live-hit
            # 2026-08-14: line noise held VAD in "speech" and this wait ate
            # its full 5 s before every dispatch): after 2 s of "speech"
            # with NO transcript evidence, dispatch — the "speech" is noise
            # as far as STT is concerned. With evidence, wait the full 5 s
            # as before.
            try:
                await asyncio.wait_for(
                    self.state._user_silent.wait(), timeout=2.0,
                )
            except asyncio.TimeoutError:
                evidence = (bool(self.stt.latest_interim)
                            or self.stt.has_pending_finals
                            or bool(self.state._queued_speech))
                if not evidence:
                    logger.info(
                        f"[{self.conn.peer_addr}] Turn timer: 2s of VAD "
                        f"speech with no transcript evidence — dispatching"
                    )
                else:
                    try:
                        await asyncio.wait_for(
                            self.state._user_silent.wait(), timeout=3.0,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"[{self.conn.peer_addr}] Turn timer: timed out "
                            f"waiting for user silence (5s)"
                        )
                    except asyncio.CancelledError:
                        return
            except asyncio.CancelledError:
                return

            # After user went silent, wait for any delayed STT delivery
            for _ in range(10):  # up to 1.0s in 0.1s steps
                if self.state._queued_speech or self.state._turn_segments:
                    break
                await asyncio.sleep(0.1)

        # Timer expired — drain any remaining STT transcripts.  Keep the
        # connection alive when the provider prefers it (stay_open_between_turns).
        # Interim fallback (audit F2): this is the last drain before dispatch —
        # a lost final degrades to the live partial instead of a dropped tail.
        if self.state._stt_active:
            final, source = self.stt.drain_transcript(), "final"
            if not final and self.stt.latest_interim:
                final, source = self.stt.latest_interim, "interim"
                # The live partial is being DISPATCHED. Its REAL final is
                # usually not lost, just ~1s late — record the text so the
                # late arrival is recognized as the same utterance instead
                # of dispatching again (Paros double-send, 2026-08-25).
                self.state._interim_fallback_text = final
                self.state._interim_fallback_at = time.monotonic()
            self.state._speech_end_time = time.monotonic()
            if final:
                self._append_turn_segment(final)
                logger.info(
                    f"[{self.conn.peer_addr}] STT final flush ({source}): {final}"
                )

        if self.state._turn_segments:
            joined = " ".join(self.state._turn_segments)
            count = len(self.state._turn_segments)
            self.state._turn_segments.clear()
            self.state._turn_timer = None

            logger.info(
                f"[{self.conn.peer_addr}] Turn dispatching "
                f"({count} segments): {joined}"
            )

            if self._turn_busy():
                logger.info(
                    f"[{self.conn.peer_addr}] Queuing turn (LLM busy): {joined}"
                )
                self.state._queued_speech.append(joined)
                self._interrupt_for_queued_speech()
            else:
                if self.state._tts_playing:
                    # Dispatch over a live/paused standalone playback (the
                    # greeting): commit — tear it down BEFORE
                    # _process_utterance reuses _tts_task/_audio_out_buf, so
                    # two senders never write one connection.
                    await self._cancel_tts()
                self.state._utterance_task = asyncio.create_task(
                    self._process_utterance(joined)
                )

    def _append_turn_segment(self, transcript: str) -> None:
        """Append transcript to turn segments, deduplicating interim→final overlaps.

        STT providers may return an interim as a segment and later return a
        final that starts with the same text.  Without dedup, the pipeline
        would dispatch duplicated text.  This method replaces the previous
        segment when the new one supersedes it.
        """
        if self.state._turn_segments:
            last = self.state._turn_segments[-1]
            if transcript.startswith(last) and len(transcript) > len(last):
                logger.info(
                    f"[{self.conn.peer_addr}] Replacing interim segment "
                    f"with final: \"{last}\" → \"{transcript}\""
                )
                self.state._turn_segments[-1] = transcript
                return
        self.state._turn_segments.append(transcript)

    def _cancel_turn_timer(self) -> None:
        """Cancel the turn continuation timer if running."""
        if self.state._turn_timer and not self.state._turn_timer.done():
            self.state._turn_timer.cancel()
            self.state._turn_timer = None

    def _interrupt_for_queued_speech(self, *, final_evidence: bool = True) -> None:
        """Speech was queued while a turn is in flight. Two duties:

        COMMIT a paused playback: the queued text IS the confirming
        transcript a barge-in pause was waiting for — cancel the paused TTS
        (the commit; _cancel_tts also fires the liveness-gated upstream
        abort) so the new speech takes the floor.

        ABORT a silent-phase generation (thinking / tool run, nothing
        playing): interrupt-style clients (abort keeps the turn in history
        with an interruption note — the duplex attach) abort the in-flight
        generation NOW so the new speech dispatches promptly instead of
        after the full tool run; the token loop's own abort-on-queued
        branch can't cover this — it needs tokens flowing and fires only
        before any audio played. Erase-style clients (Direct) keep
        run-to-completion: aborting there erases the exchange, unsafe once
        audio may have played. Safe to over-fire: the proxy drops aborts
        for turns that already finished.

        Both duties require FINAL transcript evidence (``final_evidence`` —
        the caller knows its source). An interim-fallback queue append
        (audit F2: a lost provider final degrades to the live partial)
        keeps the DISPATCH but must never kill anything: VAD/interim
        evidence aborting turns is the exact bug class the pause/confirm/
        commit design removed.

        Call this after every ``_queued_speech.append`` that can happen
        mid-turn. During audible UNPAUSED playback the queued speech is
        picked up at the stream-end poll instead (no mid-audio kill).
        """
        if not final_evidence:
            return
        if self.state._playback_paused:
            logger.info(
                f"[{self.conn.peer_addr}] Barge-in: confirmed transcript on "
                f"paused playback — committing"
            )
            asyncio.create_task(self._cancel_tts())
            # _cancel_tts owns the upstream abort (liveness-gated) — done.
            return
        if self.state._tts_playing:
            return
        if not getattr(self.llm, "supports_abort", False):
            return
        if getattr(self.llm, "abort_erases_turn", True):
            return
        logger.info(
            f"[{self.conn.peer_addr}] Queued speech during silent turn "
            f"phase — aborting in-flight generation"
        )

        async def _abort() -> None:
            try:
                await self.llm.abort_turn()
            except Exception as e:
                logger.warning(
                    f"[{self.conn.peer_addr}] Queued-speech abort failed: {e}"
                )

        asyncio.create_task(_abort())

    async def _delayed_drain_retry(self) -> None:
        """Wait for STT to deliver a finalized transcript.

        Called when SPEECH_END produced an empty transcript with no pending
        segments — the STT provider's final may not have arrived yet.
        Uses an event-based wait that returns the instant the transcript
        is ready (up to 1.0s timeout to cover network RTT jitter).
        """
        try:
            if not self.state._stt_active:
                return

            transcript = await self.stt.wait_for_transcript(timeout=self.stt.transcript_wait_timeout_s)
            elapsed_ms = (time.monotonic() - self.state._speech_end_time) * 1000
            source = "final"
            if not transcript and self.stt.latest_interim:
                # Forced finalize's is_final didn't arrive — use the live partial
                # instead of dropping the turn (interim_results streams it).
                transcript, source = self.stt.latest_interim, "interim"

            if transcript and self._is_interim_fallback_dup(transcript):
                logger.info(
                    f"[{self.conn.peer_addr}] Delayed drain: late final "
                    f"duplicates the interim-dispatched tail — dropped: "
                    f"{transcript}"
                )
                return
            if transcript:
                self._append_turn_segment(transcript)
                logger.info(
                    f"[{self.conn.peer_addr}] Delayed drain got ({source}, {elapsed_ms:.0f}ms): {transcript}"
                )
                joined = " ".join(self.state._turn_segments)
                self._cancel_turn_timer()
                self.state._turn_timer = asyncio.create_task(
                    self._turn_timeout_handler(joined)
                )

                # Backchannel: play after 2nd+ segment
                if (self._route_settings.backchannel_enabled
                        and len(self.state._turn_segments) > self.cfg.backchannel_min_segments):
                    if self.state._backchannel_task and not self.state._backchannel_task.done():
                        self.state._backchannel_task.cancel()
                    self.state._backchannel_task = asyncio.create_task(
                        self._play_backchannel()
                    )
            else:
                logger.info(
                    f"[{self.conn.peer_addr}] Delayed drain timed out ({self.stt.transcript_wait_timeout_s}s)"
                )
                # If there are pending segments, create a turn timer so they
                # don't get orphaned.  Without this, SPEECH_END cancels the
                # timer before scheduling this retry, and if the retry times
                # out the segments have no timer to dispatch them.
                if self.state._turn_segments and not (self.state._turn_timer and not self.state._turn_timer.done()):
                    joined = " ".join(self.state._turn_segments)
                    logger.info(
                        f"[{self.conn.peer_addr}] Delayed drain: creating "
                        f"timer for {len(self.state._turn_segments)} pending segments"
                    )
                    self.state._turn_timer = asyncio.create_task(
                        self._turn_timeout_handler(joined)
                    )
        except asyncio.CancelledError:
            return

    async def _delayed_queue_retry(self) -> None:
        """Wait for STT to deliver a transcript, then queue it.

        Same as _delayed_drain_retry but for the LLM-busy path — queues
        into _queued_speech instead of starting a turn timer.  This ensures
        speech during hold (or any LLM-busy period) is captured even when
        the STT final arrives after SPEECH_END.
        """
        try:
            if not self.state._stt_active:
                return

            transcript = await self.stt.wait_for_transcript(timeout=self.stt.transcript_wait_timeout_s)
            elapsed_ms = (time.monotonic() - self.state._speech_end_time) * 1000
            source = "final"
            if not transcript and self.stt.latest_interim:
                transcript, source = self.stt.latest_interim, "interim"

            if transcript and self._is_interim_fallback_dup(transcript):
                logger.info(
                    f"[{self.conn.peer_addr}] Delayed queue: late final "
                    f"duplicates the interim-dispatched tail — dropped: "
                    f"{transcript}"
                )
                return
            if transcript:
                logger.info(
                    f"[{self.conn.peer_addr}] Delayed queue got ({source}, {elapsed_ms:.0f}ms): {transcript}"
                )
                self.state._queued_speech.append(transcript)
                # Interim fallback (audit F2) keeps the DISPATCH; only a real
                # final may commit a paused playback or abort a generation.
                self._interrupt_for_queued_speech(
                    final_evidence=(source == "final"))
            else:
                logger.info(
                    f"[{self.conn.peer_addr}] Delayed queue timed out ({self.stt.transcript_wait_timeout_s}s)"
                )
        except asyncio.CancelledError:
            return

"""LLM streaming -> TTS, marker handling, and the parallel-LLM path.

Mixin for :class:`pipeline.core.CallPipeline`; operates on ``self.state``
(CallState) plus the injected collaborators (conn/cfg/stt/vad/tts/llm).
"""

import asyncio
import contextlib
import logging
import time
import re

from proxy.client import TOOL_USE_SIGNAL
from .markers import _CALL_COMPLETE_RE, _QUESTION_RE

logger = logging.getLogger("pipeline")


class LlmStreamingMixin:
    """LLM streaming -> TTS, marker handling, and the parallel-LLM path."""

    def _on_turn_dispatch(self, transcript: str) -> None:
        """One user turn is about to dispatch to the LLM — fires at the TOP of
        every ``_process_utterance`` loop iteration (the original utterance and
        every continuation/queued-speech re-dispatch), never for opening or
        injected prompts. Hook for transports that surface turn state to a UI:
        the duplex session sends its ``final`` + ``thinking`` frames here so
        the composer caption is consumed and the presence halo tracks EVERY
        real turn, not just the outer call. Base: no-op (telephony has no
        caption UI)."""

    async def _process_utterance(self, transcript: str, is_opening: bool = False) -> None:
        """Process one user turn: stream the LLM response through TTS to the caller.

        This is the turn state-machine — a ``while True`` loop where each
        iteration handles one turn. The phases, in order:

          1. SETUP — warmup wait, transcript tracking, select TTS voice, start
             the thinking filler + a fresh TTS streaming context.
          2. RESPONSE — either replay a pre-collected response (the parallel-LLM
             fast path, ``pending_response``) OR stream the LLM token-by-token
             into TTS, flushing on sentence/tool boundaries while protecting the
             [CALL_COMPLETE] / [QUESTION:] markers from being split mid-flush.
          3. COMPLETION — on [CALL_COMPLETE], wait for TTS to drain then hang up
             (inbound barge-in during the farewell resumes the call instead).
          4. QUEUED SPEECH — if the user spoke during playback, classify it as a
             continuation (re-batch into one turn) or a new query (run the next
             LLM in parallel with the current TTS, playing pre-tool filler in
             between for latency).
          5. LOOP OR EXIT — re-enter with the next transcript, or break.

        It is one long stateful method (rather than a request/response) because
        the full-duplex concerns above — barge-in cancellation, trailing-voice
        cooldown, queued speech, and parallel LLM — are driven by ``continue`` /
        ``break`` / ``return`` woven through the loop. Shared mutable state lives
        on ``self.state`` (CallState). For outbound calls it also records
        transcript entries and drives the [QUESTION:] hold/answer cycle.
        """
        # Wait for session warmup if still running
        if not self.state._warmup_done.is_set():
            logger.info(f"[{self.conn.peer_addr}] Waiting for session warmup...")
            await self.state._warmup_done.wait()

        # A previously aborted turn (Direct-layer barge-in) erased its user
        # message server-side — fold its transcript into this turn so the
        # agent still hears the full request.
        if self.state._aborted_turn_text and not is_opening:
            transcript = f"{self.state._aborted_turn_text} {transcript}"
            self.state._aborted_turn_text = None
            logger.info(
                f"[{self.conn.peer_addr}] Folding aborted turn into new "
                f"dispatch: {transcript[:80]}"
            )

        pending_response: str | None = None  # pre-collected LLM text from parallel processing

        while True:
            logger.debug(f"[{self.conn.peer_addr}] User: {transcript}")

            # Every loop iteration IS a dispatched turn — the original
            # utterance AND every continuation/queued-speech re-dispatch
            # (`continue` sites below). UI transports hook here so their
            # dispatch frames fire per REAL turn, not once per outer call
            # (live-hit 2026-08-12 04:07: a queued re-dispatch sent no
            # final/thinking frames — the dashboard composer kept showing
            # the utterance for the whole think phase and the presence halo
            # froze on the previous phase). Opening/injected prompts are
            # internal text, never a user caption.
            if not is_opening:
                self._on_turn_dispatch(transcript)

            # Track user entry in voice-aware transcript (skip opening prompts)
            if not is_opening:
                self.state._call_transcript.append({"role": "user", "content": transcript})
                logger.debug(
                    f"[{self.conn.peer_addr}] Voice transcript += user: "
                    f"{transcript[:80]}"
                )
            self.state._tts_ever_played = False
            self.state._full_response = ""
            self.state._tts_unsent_text = ""

            # Record user transcript for outbound calls (skip the opening prompt)
            if self._is_outbound and self._call_manager and not is_opening:
                self._call_manager.add_transcript_entry(
                    self._outbound_call_id, "human", transcript,
                )

            is_opening_turn = is_opening
            if is_opening:
                voice_prompt = transcript
                is_opening = False
            else:
                # Voice context is in the system prompt (injected by proxy).
                # Send raw transcript without wrapping.
                voice_prompt = transcript

            # Set TTS provider + voice based on locked language (driven by STT)
            self._select_tts(self._locked_language)
            logger.info(f"[{self.conn.peer_addr}] TTS: {self._locked_language} → {self.tts.__class__.__name__} voice={self.tts.voice_id[:16]}...")

            # Start thinking filler to mask LLM delay (skip on openings
            # and when we already have a pre-collected response). The
            # played-last-turn shift drives the repeat damper in
            # _play_thinking_filler.
            self.state._filler_played_last_turn = self.state._filler_played_this_turn
            self.state._filler_played_this_turn = False
            if self._route_settings.thinking_filler_enabled and not is_opening_turn and pending_response is None:
                self.state._thinking_filler_task = asyncio.create_task(
                    self._play_thinking_filler()
                )
            else:
                self.state._thinking_filler_done.set()

            # Start TTS streaming context
            self.tts.start_streaming_context(
            language=self._locked_language, output_sample_rate=self._rate_out)
            self.state._audio_out_buf.clear()
            # NOTE: _tts_playing and bargein_mode are set LATER in _stream_tts_audio
            # when the first audio chunk actually arrives, to avoid false barge-in
            # from the user's trailing voice before any audio is playing.

            # Launch TTS audio receiver as background task
            self.state._utterance_cancelled = False
            self.state._tts_task = asyncio.create_task(self._stream_tts_audio())

            _early_continuation = False
            _turn_aborted = False

            if pending_response is not None:
                # Fast path: we already have the full response from parallel LLM.
                # Send it all to TTS at once.
                logger.info(
                    f"[{self.conn.peer_addr}] Playing pre-collected response "
                    f"({len(pending_response)} chars)"
                )
                tts_text = pending_response
                self.state._full_response = pending_response
                _precoll_question = False
                # Check for [CALL_COMPLETE] in pre-collected response
                if _CALL_COMPLETE_RE.search(tts_text):
                    tts_text = _CALL_COMPLETE_RE.sub("", tts_text).strip()
                    self.state._call_complete = True
                    logger.info(f"[{self.conn.peer_addr}] [CALL_COMPLETE] detected in pre-collected response")
                elif self._is_outbound and not self.state._call_complete:
                    q_match = _QUESTION_RE.search(tts_text)
                    if q_match:
                        _precoll_question = True
                        question_text = q_match.group(1).strip()
                        tts_text = _QUESTION_RE.sub("", tts_text).strip()
                        logger.debug(
                            f"[{self.conn.peer_addr}] [QUESTION:] detected in "
                            f"pre-collected response: {question_text[:80]}"
                        )

                # Record assistant transcript (clean of markers)
                if self._is_outbound and self._call_manager and tts_text:
                    self._call_manager.add_transcript_entry(
                        self._outbound_call_id, "assistant", tts_text,
                    )

                if tts_text:
                    await self.tts.send_text_chunk(tts_text, is_last=True)
                else:
                    await self.tts.send_text_chunk("", is_last=True)
                pending_response = None

                # Handle question in pre-collected response
                if _precoll_question:
                    if self.state._tts_task and not self.state._tts_task.done():
                        with contextlib.suppress(asyncio.CancelledError):
                            await self.state._tts_task
                    self.state._tts_playing = False
                    self.vad.set_bargein_mode(False)
                    self.state._tts_task = None

                    answer = await self._handle_question(question_text)
                    if answer:
                        transcript = (
                            f"[ANSWER FROM MANAGER: {answer}]\n"
                            f"Continue the conversation using this information. "
                            f"Do NOT repeat the answer verbatim — integrate it naturally."
                        )
                    else:
                        transcript = (
                            "[ANSWER TIMEOUT: The manager did not respond in time.]\n"
                            "Tell the other person politely that you need to check "
                            "and will call back. Then end with [CALL_COMPLETE]."
                        )
                    is_opening = True
                    self.state._last_activity = time.monotonic()
                    continue
            else:
                # Normal path: stream LLM → buffer → TTS
                # When a tool_start signal arrives, flush pre-tool text as a
                # complete TTS segment so the caller hears it immediately while
                # tools execute.  A fresh TTS context is then created for the
                # post-tool response.
                text_buffer = ""
                full_response = []  # accumulate ALL text for [CALL_COMPLETE] detection
                sentence_end_re = re.compile(r"[.!?;]\s")
                _question_marker_seen = False  # stop TTS flushing once [QUESTION detected
                _stream_done = False   # stream ran to completion (no break)
                _abort_requeue = False  # abort + re-dispatch batched with queued speech

                self.state._t_dispatch = time.monotonic()
                self.state._t_text_first = 0.0
                self.state._t_audio_first = 0.0
                # Capture-and-clear the VAD stamp: the turn line covers only
                # dispatches that followed real speech (openings/typed → 0).
                self.state._t_turn_speech_end = self.state._t_speech_end
                self.state._t_speech_end = 0.0
                try:
                    async for token in self.llm.send_message(voice_prompt):
                        if self.state._utterance_cancelled:
                            break
                        if (not self.state._t_text_first
                                and token is not TOOL_USE_SIGNAL):
                            self.state._t_text_first = time.monotonic()

                        # Caller finished a new utterance while nothing has
                        # played yet (LLM still thinking): on the Direct
                        # layer, abort and re-dispatch original+new as one
                        # batched turn instead of answering a half-request.
                        # Nothing was heard, so the batch is always safe.
                        if (self.state._queued_speech
                                and not self.state._tts_ever_played
                                and not is_opening_turn
                                and not _question_marker_seen
                                and getattr(self.llm, "supports_abort", False)):
                            _abort_requeue = True
                            break

                        # --- Tool boundary: finalize current TTS segment ---
                        if token is TOOL_USE_SIGNAL:
                            if not _question_marker_seen and text_buffer.strip():
                                logger.info(
                                    f"[{self.conn.peer_addr}] Tool start: "
                                    f"flushing {len(text_buffer)} chars to TTS"
                                )
                                await self.tts.send_text_chunk(text_buffer, is_last=True)
                                text_buffer = ""

                                # Wait for this segment to finish playing.
                                # A PAUSED segment (barge-in pause) parks this
                                # await until the pause resolves — resume
                                # drains it, a commit cancels it (below).
                                if self.state._tts_task and not self.state._tts_task.done():
                                    try:
                                        await self.state._tts_task
                                    except asyncio.CancelledError:
                                        # Scoped absorb (audit blocker): only a
                                        # playback cancel from our own commit
                                        # path (_cancel_tts sets the flag BEFORE
                                        # cancelling) may be absorbed — a
                                        # cancellation of THIS task (teardown)
                                        # must propagate or _cleanup deadlocks
                                        # awaiting an uncancellable task.
                                        cur = asyncio.current_task()
                                        if cur is not None and cur.cancelling():
                                            raise
                                        if not self.state._utterance_cancelled:
                                            raise
                                        # Committed barge-in: do NOT open a
                                        # fresh streaming context (a zombie
                                        # sender would speak the dead turn's
                                        # post-tool text) — unwind to the
                                        # completion tail; the abort branch
                                        # below owns the server-side stop.
                                        break
                                    cur = asyncio.current_task()
                                    if cur is not None and cur.cancelling():
                                        # Our own cancellation rode the awaited
                                        # segment task and EVAPORATED when its
                                        # CancelledError handler returned
                                        # normally (asyncio cancels the awaited
                                        # future, not this frame) — honor it.
                                        raise asyncio.CancelledError()

                                # A commit that landed BEFORE this boundary
                                # (segment task already done/cancelled, so the
                                # await above was skipped or returned) must not
                                # open the fresh context either.
                                if self.state._utterance_cancelled:
                                    break

                                # Reset TTS state
                                self.state._tts_playing = False
                                self.vad.set_bargein_mode(False)
                                self.state._tts_task = None
                                self.state._audio_out_buf.clear()

                                # New TTS context for post-tool text
                                self.tts.start_streaming_context(
            language=self._locked_language, output_sample_rate=self._rate_out)
                                self.state._tts_task = asyncio.create_task(
                                    self._stream_tts_audio()
                                )
                            else:
                                # Buffer empty or whitespace-only — discard
                                text_buffer = ""
                            continue

                        # --- Normal text token ---
                        text_buffer += token
                        full_response.append(token)

                        # Once [QUESTION marker detected, stop all TTS flushing.
                        # Let tokens accumulate silently — post-stream detection
                        # will handle the complete marker.
                        if _question_marker_seen:
                            continue

                        # Check for [QUESTION marker arriving before flush threshold
                        if self._is_outbound and "[QUESTION" in text_buffer:
                            # Flush only text before the marker
                            bracket_idx = text_buffer.find("[QUESTION")
                            pre_marker = text_buffer[:bracket_idx].strip()
                            if pre_marker:
                                tts_chunk = _CALL_COMPLETE_RE.sub("", pre_marker)
                                if tts_chunk:
                                    await self.tts.send_text_chunk(tts_chunk)
                            text_buffer = text_buffer[bracket_idx:]  # keep marker in buffer
                            _question_marker_seen = True
                            logger.info(
                                f"[{self.conn.peer_addr}] [QUESTION marker "
                                f"detected mid-stream — stopping TTS flush"
                            )
                            continue

                        should_flush = (
                            len(text_buffer) >= self.cfg.tts_buffer_chars
                            and sentence_end_re.search(text_buffer)
                        ) or len(text_buffer) >= self.cfg.tts_buffer_chars * 3

                        if should_flush:
                            # Protect against splitting markers ([CALL_COMPLETE],
                            # [QUESTION:...]) across flush boundaries.
                            # If buffer ends with an unclosed '[', flush only
                            # the text before it and hold the rest.
                            if not _question_marker_seen:
                                last_bracket = text_buffer.rfind("[")
                                if last_bracket >= 0 and "]" not in text_buffer[last_bracket:]:
                                    flush_part = text_buffer[:last_bracket]
                                    text_buffer = text_buffer[last_bracket:]
                                    if flush_part.strip():
                                        flush_part = _CALL_COMPLETE_RE.sub("", flush_part).strip()
                                        if flush_part:
                                            await self.tts.send_text_chunk(flush_part)
                                    continue

                            # Strip [CALL_COMPLETE] before sending to TTS
                            tts_chunk = _CALL_COMPLETE_RE.sub("", text_buffer)
                            # Outbound only: detect [QUESTION] marker — flush
                            # pre-marker text then stop all further flushing
                            if self._is_outbound:
                                bracket_idx = tts_chunk.find("[QUESTION")
                                if bracket_idx >= 0:
                                    tts_chunk = tts_chunk[:bracket_idx].strip()
                                    _question_marker_seen = True
                                    logger.info(
                                        f"[{self.conn.peer_addr}] [QUESTION marker "
                                        f"detected mid-stream — stopping TTS flush"
                                    )
                            if tts_chunk:
                                await self.tts.send_text_chunk(tts_chunk)
                            text_buffer = ""
                    else:
                        _stream_done = True

                    # Caller kept talking before anything played — tear down
                    # this turn (TTS context + server-side generation) and
                    # re-dispatch original + queued speech as one turn.
                    if _abort_requeue:
                        await self._abort_streaming_turn()
                        queued = " ".join(self.state._queued_speech)
                        self.state._queued_speech.clear()
                        if getattr(self.llm, "abort_erases_turn", True):
                            # The aborted turn's user message is gone
                            # server-side — resend it batched with the new
                            # speech, and drop the local mirror entry.
                            if (self.state._call_transcript
                                    and self.state._call_transcript[-1]["role"] == "user"):
                                self.state._call_transcript.pop()
                            redispatch = f"{transcript} {queued}"
                        else:
                            # Interrupt-style abort (duplex attach): the turn
                            # STAYS in history with an interruption note —
                            # resending its words would duplicate them.
                            redispatch = queued
                        logger.info(
                            f"[{self.conn.peer_addr}] Turn aborted mid-stream "
                            f"(caller kept talking) — re-dispatching "
                            f"{'batched' if redispatch != queued else 'new speech only'}"
                        )
                        transcript = await self._wait_for_complete_continuation(
                            redispatch,
                        )
                        continue

                    # Barge-in stopped the stream mid-generation: abort the
                    # turn server-side (Direct) so the model isn't still
                    # answering it; its transcript is folded into the next
                    # dispatch (the erased turn "never happened" upstream).
                    if (not _stream_done and self.state._utterance_cancelled
                            and getattr(self.llm, "supports_abort", False)):
                        await self.llm.abort_turn()
                        if getattr(self.llm, "abort_erases_turn", True):
                            # Erase-style abort only: refold the transcript
                            # into the next dispatch. Interrupt-style keeps
                            # the turn in history — nothing to resend.
                            self.state._aborted_turn_text = transcript
                        _turn_aborted = True
                        logger.info(
                            f"[{self.conn.peer_addr}] Turn aborted on barge-in "
                            f"(mid-generation)"
                        )

                    # Store full response and unflushed text for barge-in annotation
                    self.state._full_response = "".join(full_response)
                    self.state._tts_unsent_text = text_buffer

                    # --- Detect [CALL_COMPLETE] in full accumulated response ---
                    _has_question = False
                    if not self.state._utterance_cancelled:
                        full_text = "".join(full_response)
                        if _CALL_COMPLETE_RE.search(full_text):
                            self.state._call_complete = True
                            logger.info(
                                f"[{self.conn.peer_addr}] [CALL_COMPLETE] detected"
                            )
                            # Strip marker from remaining buffer before TTS
                            text_buffer = _CALL_COMPLETE_RE.sub("", text_buffer).strip()

                        # --- Detect [QUESTION:] — outbound only ---
                        elif self._is_outbound and not self.state._call_complete:
                            q_match = _QUESTION_RE.search(full_text)
                            if q_match:
                                _has_question = True
                                question_text = q_match.group(1).strip()
                                logger.debug(
                                    f"[{self.conn.peer_addr}] [QUESTION:] detected: "
                                    f"{question_text[:80]}"
                                )
                                # Strip the marker from TTS buffer
                                text_buffer = _QUESTION_RE.sub("", text_buffer).strip()

                    # Record assistant transcript for outbound
                    if self._is_outbound and self._call_manager and full_response:
                        clean_text = "".join(full_response)
                        clean_text = re.sub(r"\[//\]: # \(session:[a-f0-9-]+\)", "", clean_text)
                        clean_text = _CALL_COMPLETE_RE.sub("", clean_text)
                        clean_text = _QUESTION_RE.sub("", clean_text).strip()
                        if clean_text:
                            self._call_manager.add_transcript_entry(
                                self._outbound_call_id, "assistant", clean_text,
                            )

                    # --- Early continuation check ---
                    # If speech was queued during LLM streaming and user is
                    # now silent, classify BEFORE sending final TTS text.
                    # This avoids 0.3-1.7s of wrong audio playing while
                    # Groq classifies in the normal queued-speech handler.
                    _is_answer_turn = transcript.startswith("[ANSWER FROM MANAGER:")
                    if (self.state._queued_speech
                            and self.state._user_silent.is_set()
                            and not self.state._utterance_cancelled
                            and not _question_marker_seen
                            and not _has_question
                            and not _is_answer_turn
                            and not (self._is_outbound and self.state._call_complete)):
                        _early_next = " ".join(self.state._queued_speech)
                        if await self._is_queued_continuation(
                            transcript, _early_next,
                        ):
                            _early_continuation = True
                            logger.info(
                                f"[{self.conn.peer_addr}] Early continuation "
                                f"detected — skipping final TTS text"
                            )

                    # Send remaining text as final chunk (strip markers before TTS)
                    if _early_continuation:
                        # Continuation: close TTS context without final text.
                        # Queued speech handler below will cancel TTS and resend.
                        await self.tts.send_text_chunk("", is_last=True)
                    elif _question_marker_seen:
                        # Question marker was seen — don't send any remaining text to TTS
                        await self.tts.send_text_chunk("", is_last=True)
                    elif text_buffer and not self.state._utterance_cancelled:
                        tts_final = _CALL_COMPLETE_RE.sub("", text_buffer).strip()
                        if tts_final:
                            await self.tts.send_text_chunk(tts_final, is_last=True)
                        else:
                            await self.tts.send_text_chunk("", is_last=True)
                    elif not self.state._utterance_cancelled:
                        await self.tts.send_text_chunk("", is_last=True)

                    # --- Handle question: wait for TTS, play hold, wait for answer ---
                    if _has_question:
                        # Wait for current TTS to finish (caller's pre-question speech)
                        if self.state._tts_task and not self.state._tts_task.done():
                            with contextlib.suppress(asyncio.CancelledError):
                                await self.state._tts_task
                        self.state._tts_playing = False
                        self.vad.set_bargein_mode(False)
                        self.state._tts_task = None

                        answer = await self._handle_question(question_text)
                        if answer:
                            transcript = (
                                f"[ANSWER FROM MANAGER: {answer}]\n"
                                f"Continue the conversation using this information. "
                                f"Do NOT repeat the answer verbatim — integrate it naturally."
                            )
                        else:
                            transcript = (
                                "[ANSWER TIMEOUT: The manager did not respond in time.]\n"
                                "Tell the other person politely that you need to check "
                                "and will call back. Then end with [CALL_COMPLETE]."
                            )
                        is_opening = True  # skip user transcript recording for injected answer
                        self.state._last_activity = time.monotonic()
                        continue

                except asyncio.CancelledError:
                    # Barge-in cancelled this task
                    logger.debug(f"[{self.conn.peer_addr}] Utterance cancelled (barge-in)")
                    return

            # --- LLM is done (or pre-collected text sent to TTS). ---

            # If call complete, wait for TTS to finish then hang up
            if self.state._call_complete:
                if self.state._tts_task and not self.state._tts_task.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await self.state._tts_task
                self.state._tts_playing = False
                self.vad.set_bargein_mode(False)
                self.state._tts_task = None
                self.state._last_activity = time.monotonic()

                # If the caller still has something to say — they barged in over
                # the farewell, or spoke (now queued) while it waited for them to
                # finish — resume the call instead of hanging up. The listen loop
                # keeps feeding VAD (inbound + outbound), so their speech becomes
                # the next turn and the agent re-emits [CALL_COMPLETE] when the call
                # is truly over. This is also what makes the farewell wait for the
                # caller rather than deadlocking on a forever-parked TTS.
                if self.state._utterance_cancelled or self.state._queued_speech:
                    self.state._call_complete = False
                    logger.info(
                        f"[{self.conn.peer_addr}] Caller still engaged during "
                        f"farewell — resuming instead of hanging up"
                    )
                    if not self.state._queued_speech:
                        return  # not queued yet; the next turn will pick it up
                    # else fall through to the queued-speech handler below
                else:
                    # Brief pause then hang up
                    await asyncio.sleep(0.5)
                    self.conn.send_hangup()
                    self.state._running = False
                    logger.info(f"[{self.conn.peer_addr}] Call complete — hanging up")
                    break

            # Wait for user to finish speaking before processing queued speech.
            # Without this, we'd grab a partial sentence from the queue while
            # the user is still mid-sentence (VAD split on a brief pause).
            # Applies to both barge-in AND normal queued-speech scenarios.
            if self.state._queued_speech and not self.state._user_silent.is_set():
                logger.info(
                    f"[{self.conn.peer_addr}] Queued speech found but user "
                    f"still speaking — waiting for silence"
                )
                while True:
                    await self.state._user_silent.wait()
                    try:
                        await asyncio.sleep(0.5)
                    except asyncio.CancelledError:
                        return
                    if self.state._user_silent.is_set():
                        break
                    logger.info(
                        f"[{self.conn.peer_addr}] User resumed — waiting again"
                    )

            # Check for queued speech: start next LLM in parallel with TTS playback.
            if self.state._queued_speech:
                next_transcript = " ".join(self.state._queued_speech)
                self.state._queued_speech.clear()

                # An aborted turn's user message is gone server-side — always
                # batch original + new (no classifier call needed). Otherwise
                # check if queued speech is just a continuation of the current
                # request (e.g., "check the cameras" → "the external ones").
                # Skip the classifier for answer-injection turns — user speech
                # during hold is always a new query.
                _aborted_text = self.state._aborted_turn_text
                if _aborted_text is not None:
                    self.state._aborted_turn_text = None
                    is_continuation = True
                    resend_base = f"{_aborted_text} {next_transcript}"
                else:
                    is_continuation = _early_continuation or (
                        not _is_answer_turn
                        and await self._is_queued_continuation(
                            transcript, next_transcript,
                        )
                    )
                    resend_base = next_transcript
                _early_continuation = False  # consumed
                if is_continuation:
                    # LLM completed (or the turn was aborted server-side) —
                    # don't pop from llm.messages (would desync). Send the
                    # continuation (batched with the aborted transcript when
                    # applicable) as a new turn.
                    resend_text = resend_base
                    logger.info(
                        f"[{self.conn.peer_addr}] Continuation (LLM done) "
                        f"— new turn: {resend_text}"
                    )
                    if self.state._call_transcript and self.state._call_transcript[-1]["role"] == "user":
                        self.state._call_transcript.pop()

                    # Cancel TTS if still playing
                    _was_tts_playing = self.state._tts_playing
                    if self.state._tts_task and not self.state._tts_task.done():
                        self.state._tts_playing = False
                        self.state._utterance_cancelled = True
                        self.vad.set_bargein_mode(False)
                        self.tts.cancel()
                        self.conn.flush_playback()
                        self.state._tts_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await self.state._tts_task
                        self.state._tts_task = None
                    else:
                        self.state._tts_playing = False
                        self.vad.set_bargein_mode(False)
                        self.state._tts_task = None
                        # The receiver already ended (natural drain closes the
                        # server context since 2026-08-24), but a context that
                        # was started and never consumed any frame still needs
                        # its close — cancel() is idempotent either way.
                        self.tts.cancel()

                    # Only notify STT if TTS was actually playing audio (echo
                    # cleanup).  When TTS never played (early continuation
                    # before any audio), the queue may have valid user speech.
                    if _was_tts_playing:
                        self.stt.on_tts_finished(was_interrupted=False)
                    self.state._stt_early_unmuted = False
                    self.state._last_activity = time.monotonic()

                    # Re-classify and wait for user to finish speaking.
                    # Batches all speech into one LLM call.
                    transcript = await self._wait_for_complete_continuation(
                        resend_text,
                    )
                    continue
                else:
                    logger.info(
                        f"[{self.conn.peer_addr}] Starting parallel LLM "
                        f"while TTS plays: {next_transcript}"
                    )

                    # Raw transcript, like every other turn — the spoken-style
                    # rules live in the SYSTEM prompt (phone context / duplex
                    # chat context). The old inline [VOICE: …] wrapper rode the
                    # utterance frame into the chat DB and showed inside the
                    # user's own bubble (operator screenshot 2026-08-11).
                    next_prompt = next_transcript

                    # Reset pre-tool text state for this parallel LLM run
                    self.state._parallel_pre_tool_text = None
                    self.state._parallel_pre_tool_ready.clear()

                    # Start LLM-2 in parallel with current TTS playback.
                    # Remember its transcript so a barge-in that cancels it
                    # (_cancel_tts aborts it server-side on the Direct layer)
                    # can fold these words into the next dispatch.
                    self.state._parallel_llm_text = next_transcript
                    self.state._parallel_llm_task = asyncio.create_task(
                        self._collect_llm_response(next_prompt)
                    )

                    # Wait for current TTS to finish (user hears full answer)
                    if self.state._tts_task and not self.state._tts_task.done():
                        with contextlib.suppress(asyncio.CancelledError):
                            await self.state._tts_task

                    self.state._tts_playing = False
                    self.vad.set_bargein_mode(False)
                    self.state._tts_task = None
                    self.state._last_activity = time.monotonic()
                    self.stt.on_tts_finished(was_interrupted=False)
                    self.state._stt_early_unmuted = False
                    # Add assistant entry to voice transcript (TTS finished)
                    self._add_assistant_transcript()

                    # Play pre-tool text as intermediate TTS (e.g. "let me check the camera")
                    # while the tool continues running in the background.
                    try:
                        await asyncio.wait_for(self.state._parallel_pre_tool_ready.wait(), timeout=30.0)
                    except asyncio.TimeoutError:
                        logger.warning(f"[{self.conn.peer_addr}] Pre-tool text wait timed out")

                    if self.state._parallel_pre_tool_text:
                        logger.info(
                            f"[{self.conn.peer_addr}] Playing intermediate pre-tool TTS: "
                            f"{self.state._parallel_pre_tool_text[:60]}"
                        )
                        await asyncio.sleep(self.cfg.tts_response_gap_s)

                        # Play pre-tool text via TTS
                        self.tts.start_streaming_context(
            language=self._locked_language, output_sample_rate=self._rate_out)
                        self.state._audio_out_buf.clear()
                        self.state._utterance_cancelled = False
                        self.state._tts_task = asyncio.create_task(self._stream_tts_audio())
                        await self.tts.send_text_chunk(
                            self.state._parallel_pre_tool_text, is_last=True,
                        )

                        # Wait for intermediate TTS to finish
                        if self.state._tts_task and not self.state._tts_task.done():
                            with contextlib.suppress(asyncio.CancelledError):
                                await self.state._tts_task
                        self.state._tts_playing = False
                        self.vad.set_bargein_mode(False)
                        self.state._tts_task = None
                        self.state._last_activity = time.monotonic()
                        self.stt.on_tts_finished(was_interrupted=False)
                        self.state._stt_early_unmuted = False

                    # Get the post-tool LLM response (may already be done)
                    if not self.state._parallel_llm_task:
                        break
                    try:
                        pending_response = await self.state._parallel_llm_task
                        self.state._parallel_llm_text = ""
                    except asyncio.CancelledError:
                        logger.debug(f"[{self.conn.peer_addr}] Parallel LLM cancelled")
                        return
                    finally:
                        self.state._parallel_llm_task = None

                    transcript = next_transcript

                    # Small natural gap between consecutive responses
                    await asyncio.sleep(self.cfg.tts_response_gap_s)
                    continue

            # No queued speech — wait for TTS to finish playing.
            # Poll periodically so we catch late-arriving queued speech.
            # Without this, speech queued AFTER the check above but DURING
            # TTS would only be handled after the full TTS finishes — the
            # user hears a wrong response before the real one.
            if self.state._tts_task and not self.state._tts_task.done():
                while self.state._tts_task and not self.state._tts_task.done():
                    done, _ = await asyncio.wait(
                        {self.state._tts_task}, timeout=0.3,
                    )
                    if done:
                        break
                    # Speech queued while TTS is playing/waiting — cancel
                    # TTS early so we can classify and handle it now.
                    if self.state._queued_speech and self.state._user_silent.is_set():
                        logger.info(
                            f"[{self.conn.peer_addr}] Speech queued during "
                            f"TTS — cancelling for reprocessing"
                        )
                        self.state._tts_playing = False
                        self.state._utterance_cancelled = True
                        self.vad.set_bargein_mode(False)
                        self.tts.cancel()
                        self.conn.flush_playback()
                        self.state._tts_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await self.state._tts_task
                        self.state._tts_task = None
                        break

            self.state._tts_playing = False
            self.vad.set_bargein_mode(False)
            self.state._tts_task = None
            self.state._last_activity = time.monotonic()
            self.stt.on_tts_finished(was_interrupted=self.state._utterance_cancelled)
            self.state._stt_early_unmuted = False

            # Annotate interrupted response for barge-in.
            # Done here (after TTS wait) because _utterance_cancelled is set
            # by _cancel_tts() during TTS playback, not during LLM streaming.
            # Works for both direct and proxy modes through ProxyClient.
            # Skipped for aborted turns: the exchange was erased server-side,
            # so there is no assistant message to annotate — the batched
            # re-dispatch is the recovery.
            if (self.state._utterance_cancelled
                    and not _turn_aborted
                    and hasattr(self.llm, 'annotate_interrupted_response')):
                spoken_chars = len(self.state._full_response) - len(self.state._tts_unsent_text)
                self.llm.mark_spoken(spoken_chars)
                self.llm.annotate_interrupted_response()

            # Add assistant entry to voice transcript
            self._add_assistant_transcript()

            # Late check: speech may have arrived during TTS playback
            if self.state._queued_speech:
                late_text = " ".join(self.state._queued_speech)
                self.state._queued_speech.clear()
                _aborted_text = self.state._aborted_turn_text
                if _aborted_text is not None:
                    # Aborted turn (erased server-side): always batch.
                    self.state._aborted_turn_text = None
                    if (self.state._call_transcript
                            and self.state._call_transcript[-1]["role"] == "user"):
                        self.state._call_transcript.pop()
                    logger.info(
                        f"[{self.conn.peer_addr}] Late speech after aborted "
                        f"turn — re-dispatching batched"
                    )
                    transcript = await self._wait_for_complete_continuation(
                        f"{_aborted_text} {late_text}",
                    )
                    continue
                if not _is_answer_turn and await self._is_queued_continuation(transcript, late_text):
                    # Late continuation: LLM already responded and TTS
                    # finished playing.  Session has the full exchange.
                    # Don't pop from llm.messages or call_transcript —
                    # they reflect what happened.
                    # Send only the continuation as a new turn.
                    logger.info(
                        f"[{self.conn.peer_addr}] Late continuation — "
                        f"new turn: {late_text}"
                    )
                    transcript = await self._wait_for_complete_continuation(
                        late_text,
                    )
                    continue
                else:
                    transcript = late_text
                    logger.debug(f"[{self.conn.peer_addr}] Processing queued speech: {transcript}")
                    continue
            else:
                break

    async def _abort_streaming_turn(self) -> None:
        """Tear down the current turn's TTS + server-side generation (Direct).

        Used when a turn is being re-dispatched (batched with new caller
        speech) before any audio played: kills the streaming TTS context so
        already-flushed text can't start playing, cancels the thinking
        filler, and aborts the turn server-side (the direct session pops the
        un-answered user message).
        """
        self.tts.cancel()
        if self.state._tts_task and not self.state._tts_task.done():
            self.state._tts_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.state._tts_task
        self.state._tts_task = None
        self.state._tts_playing = False
        self.vad.set_bargein_mode(False)

        if (self.state._thinking_filler_task
                and not self.state._thinking_filler_task.done()):
            self.state._thinking_filler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.state._thinking_filler_task
            self.state._thinking_filler_task = None
            self.state._thinking_filler_done.set()

        await self.llm.abort_turn()

    async def _collect_llm_response(self, voice_prompt: str) -> str:
        """Send to LLM and collect full response text (for parallel processing).

        Runs concurrently with TTS playback so the next response is ready
        as soon as the current one finishes playing. If cancelled (barge-in),
        cleans up LLM messages to avoid corruption.

        When a TOOL_USE_SIGNAL arrives, the text collected so far (pre-tool
        text like "let me check the camera") is stored in
        ``_parallel_pre_tool_text`` and ``_parallel_pre_tool_ready`` is set.
        The caller can play this as intermediate TTS while the tool runs.
        Only the first tool boundary triggers this; subsequent ones are
        ignored (the text just accumulates into post_tool).
        The method returns ONLY post-tool text when a tool was used.
        """
        msg_count_before = len(self.llm.messages)
        pre_tool: list[str] = []
        post_tool: list[str] = []
        pre_tool_signalled = False
        try:
            async for token in self.llm.send_message(voice_prompt):
                if token is TOOL_USE_SIGNAL:
                    if not pre_tool_signalled:
                        pre_tool_text = "".join(pre_tool).strip()
                        if pre_tool_text:
                            self.state._parallel_pre_tool_text = pre_tool_text
                            logger.info(
                                f"[{self.conn.peer_addr}] Parallel LLM pre-tool text "
                                f"({len(pre_tool_text)} chars): {pre_tool_text[:80]}"
                            )
                        else:
                            self.state._parallel_pre_tool_text = None
                        self.state._parallel_pre_tool_ready.set()
                        pre_tool_signalled = True
                    continue

                if pre_tool_signalled:
                    post_tool.append(token)
                else:
                    pre_tool.append(token)
        except asyncio.CancelledError:
            while len(self.llm.messages) > msg_count_before:
                self.llm.messages.pop()
            # Ensure event is set so caller isn't stuck waiting
            self.state._parallel_pre_tool_ready.set()
            raise
        except Exception as e:
            logger.error(f"[{self.conn.peer_addr}] Parallel LLM error: {e}")
            while len(self.llm.messages) > msg_count_before:
                self.llm.messages.pop()
            self.state._parallel_pre_tool_ready.set()
            return ""

        if not pre_tool_signalled:
            # No tool was called — signal ready with no pre-tool text
            self.state._parallel_pre_tool_ready.set()
            return "".join(pre_tool)

        # Tool was called — return only post-tool text
        return "".join(post_tool)

"""LLM streaming -> TTS, marker handling, and the parallel-LLM path.

Mixin for :class:`pipeline.core.CallPipeline`; operates on ``self.state``
(CallState) plus the injected collaborators (conn/cfg/stt/vad/tts/llm).
"""

import asyncio
import contextlib
import logging
import re
import time

from proxy.client import TOOL_USE_SIGNAL

from .markers import _CALL_COMPLETE_RE, _QUESTION_RE

logger = logging.getLogger("pipeline")

#: The engine's pause after which a complete sentence already with the TTS
#: is flushed, so it plays while the engine works (a tool call, a short
#: answer under the provider's first-generation threshold).
TTS_PAUSE_FLUSH_S = 0.6

#: A chunk ends a sentence: closing punctuation (the Greek question mark is
#: the semicolon), then closing quotes or brackets, then whitespace, all at
#: the END of the text. A 60-character cut mid-sentence never matches.
_SENTENCE_FINAL_RE = re.compile(r"[.!?;…][\"'”’»)\]]*\s*$")

# What the engine race hands back: a token, the call tools' wake, a pause
# with no token, the stream's end.
TOKEN, WAKE, PAUSE, END = "token", "wake", "pause", "end"


class _EngineRace:
    """The engine's token stream, read through ONE persistent next-token task
    and raced against the call tools' wake and a pause timeout.

    The token task is never cancelled on a wake or a timeout: cancelling the
    client's ``__anext__`` throws into its generators and ends the turn at
    the first pause. ``close()`` cancels a still-pending task once, at the
    loop's exit. A wake is handed back once (the event is cleared). A token
    that finished while the loop was busy elsewhere comes before a wake set
    in the same window: it arrived first. ``drain_ready`` hands over the
    tokens already in the engine's queue without waiting, so the words that
    reached the daemon before a control frame are spoken with the cut.
    """

    def __init__(self, stream, wake: asyncio.Event, pause_s: float | None = None):
        self._it = stream.__aiter__()
        self._wake = wake
        self._pause_s = pause_s
        self._token_task: asyncio.Future | None = None
        self._wake_task: asyncio.Future | None = None
        self._held = None       # a token drain_ready stopped at, read next
        self.ended = False

    def _take(self, task: asyncio.Future):
        self._token_task = None
        try:
            return TOKEN, task.result()
        except StopAsyncIteration:
            self.ended = True
            return END, None

    async def next(self):
        if self._held is not None:
            token, self._held = self._held, None
            return TOKEN, token
        if self.ended:
            return END, None
        if self._token_task is not None and self._token_task.done():
            return self._take(self._token_task)
        if self._wake.is_set():
            self._wake.clear()
            return WAKE, None
        if self._token_task is None:
            self._token_task = asyncio.ensure_future(self._it.__anext__())
        if self._wake_task is None or self._wake_task.done():
            self._wake_task = asyncio.ensure_future(self._wake.wait())
        done, _ = await asyncio.wait(
            {self._token_task, self._wake_task},
            timeout=self._pause_s, return_when=asyncio.FIRST_COMPLETED,
        )
        if self._token_task in done:
            return self._take(self._token_task)
        if self._wake_task in done:
            self._wake.clear()
            return WAKE, None
        return PAUSE, None

    async def drain_ready(self, *, stop=None) -> list:
        """The tokens the engine has already delivered (in hand, or queued
        at the client): each is read in one loop step, nothing waits. A
        token ``is stop`` ends the drain and is handed back by the next
        ``next()``, so a tool boundary keeps its own handling."""
        out: list = []
        while not self.ended and self._held is None:
            if self._token_task is None:
                self._token_task = asyncio.ensure_future(self._it.__anext__())
            await asyncio.sleep(0)
            if not self._token_task.done():
                break
            kind, token = self._take(self._token_task)
            if kind is END:
                break
            if stop is not None and token is stop:
                self._held = token
                break
            out.append(token)
        return out

    async def close(self) -> None:
        if self._wake_task is not None and not self._wake_task.done():
            self._wake_task.cancel()
        self._wake_task = None
        task, self._token_task = self._token_task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
            await asyncio.wait({task})
        if not task.cancelled():
            task.exception()           # retrieved: a stream end nobody read


def _split_open_bracket(text: str) -> tuple[str, str]:
    """``text`` as (speakable, held): a trailing unclosed ``[`` and what
    follows it are held back, a marker arriving in pieces is never spoken."""
    last = text.rfind("[")
    if last >= 0 and "]" not in text[last:]:
        return text[:last], text[last:]
    return text, ""


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
             fast path, ``pending_response``) OR stream the engine through
             ``_EngineRace`` (the next token raced against the call tools'
             acceptance and a ``TTS_PAUSE_FLUSH_S`` pause) into TTS: chunks on
             sentence and tool boundaries, a mid-utterance flush after a pause
             at a sentence end or at an empty tool boundary, the cut at an
             accepted end or transfer, the [CALL_COMPLETE] / [QUESTION:]
             markers protected from being split mid-flush.
          3. COMPLETION — on [CALL_COMPLETE] or an accepted end or transfer,
             wait for TTS to drain then hang up or transfer (a barge-in, or
             queued input that started after a call tool's acceptance, resumes
             the call instead).
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
            logger.info("[%s] Waiting for session warmup...", self.conn.peer_addr)
            await self.state._warmup_done.wait()

        # A previously aborted turn (Direct-layer barge-in) erased its user
        # message server-side — fold its transcript into this turn so the
        # agent still hears the full request.
        if self.state._aborted_turn_text and not is_opening:
            transcript = f"{self.state._aborted_turn_text} {transcript}"
            self.state._aborted_turn_text = None
            logger.info(
                "[%s] Folding aborted turn into new "
                "dispatch: %s", self.conn.peer_addr, transcript[:80]
            )

        pending_response: str | None = None  # pre-collected LLM text from parallel processing

        while True:
            logger.debug("[%s] User: %s", self.conn.peer_addr, transcript)

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

            # A caller who speaks after the agent's goodbye resumes the call:
            # an end a previous turn raised does not carry over (a transfer
            # does: it completes at this turn's end). A failed transfer's
            # note rides this dispatch.
            self._clear_call_control("new dispatch", keep_transfer=True)
            self.state._control_wake.clear()
            if self.state._control_note:
                transcript = f"{self.state._control_note}\n{transcript}"
                self.state._control_note = ""

            # Track user entry in voice-aware transcript (skip opening prompts)
            if not is_opening:
                self.state._call_transcript.append({"role": "user", "content": transcript})
                logger.debug(
                    "[%s] Voice transcript += user: "
                    "%s", self.conn.peer_addr, transcript[:80]
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
            logger.info("[%s] TTS: %s → %s voice=%s...", self.conn.peer_addr, self._locked_language, self.tts.__class__.__name__, self.tts.voice_id[:16])

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
            self.state._tts_flushed_open = False
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
                    "[%s] Playing pre-collected response "
                    "(%s chars)", self.conn.peer_addr, len(pending_response)
                )
                tts_text = pending_response
                self.state._full_response = pending_response
                _precoll_question = False
                transcript_text = None   # what the call transcript records (markers stripped)
                # Check for [CALL_COMPLETE] in pre-collected response: the
                # caller hears what precedes the marker and NOTHING after it
                # (a report the model appends behind the marker stays in the
                # transcript — live-hit 2026-09-07).
                _m = _CALL_COMPLETE_RE.search(tts_text)
                if _m:
                    transcript_text = _CALL_COMPLETE_RE.sub("", tts_text).strip()
                    tts_text = tts_text[:_m.start()].strip()
                    self.state._call_complete = True
                    logger.info("[%s] [CALL_COMPLETE] detected in pre-collected response", self.conn.peer_addr)
                elif self._control_pending_end():
                    # The parallel turn's end or transfer: the collector kept
                    # only the words before the tool (and one sentence when
                    # nothing came before it), so the text plays whole.
                    transcript_text = tts_text.strip()
                    self.state._call_complete = True
                    logger.info("[%s] call control: end pending in pre-collected response", self.conn.peer_addr)
                elif self._is_outbound and not self.state._call_complete:
                    q_match = _QUESTION_RE.search(tts_text)
                    if q_match:
                        _precoll_question = True
                        question_text = q_match.group(1).strip()
                        tts_text = _QUESTION_RE.sub("", tts_text).strip()
                        logger.debug(
                            "[%s] [QUESTION:] detected in "
                            "pre-collected response: %s",
                            self.conn.peer_addr, question_text[:80]
                        )

                # Record assistant transcript (clean of markers, incl. any
                # text the model put after the end-of-call marker)
                if self._is_outbound and self._call_manager and (transcript_text or tts_text):
                    self._call_manager.add_transcript_entry(
                        self._outbound_call_id, "assistant", transcript_text or tts_text,
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
                _complete_marker_seen = False  # stop TTS flushing once [CALL_COMPLETE] streamed
                _stream_done = False   # stream ran to completion (no break)
                _abort_requeue = False  # abort + re-dispatch batched with queued speech
                # The call tools' speech rule: once an end or a transfer is
                # pending, the text produced before the tool plays whole;
                # after the tool's boundary at most one sentence, and only
                # when nothing was spoken before it. _control_break leaves
                # the stream the moment the last speakable text is flushed:
                # the completion point is the TTS drain, never the stream's
                # end (whatever the engine still produces is transcript).
                _spoke_any = False        # text of this turn reached TTS
                _control_spoke_before = False
                _control_break = False
                _context_open = True      # a streaming context waits for is_last
                # The mid-utterance flush: characters handed to the TTS since
                # the last flush or end of input, and whether the last chunk
                # ended a sentence. A provider holds a short chunk under its
                # first-generation threshold; the flush at a tool boundary
                # or after a pause makes it play (never a close).
                sent_since_flush = 0
                last_sent_final = False

                async def _tts_ready(piece: str) -> None:
                    # The receiver ended under the open context (the
                    # provider's socket died across a long tool run, a
                    # vendor error): text still to come gets a fresh context
                    # and receiver; what was spoken is not repeated.
                    nonlocal sent_since_flush, _context_open
                    task = self.state._tts_task
                    if (not piece or not _context_open or self.state._utterance_cancelled
                            or (task is not None and not task.done())):
                        return
                    logger.warning(
                        "[%s] TTS receiver ended under an open "
                        "context: a fresh context for the rest of the reply", self.conn.peer_addr
                    )
                    self.tts.start_streaming_context(
                        language=self._locked_language, output_sample_rate=self._rate_out)
                    self.state._audio_out_buf.clear()
                    self.state._tts_flushed_open = False
                    sent_since_flush = 0
                    _context_open = True
                    self.state._tts_task = asyncio.create_task(self._stream_tts_audio())

                async def _speak(piece: str) -> None:
                    nonlocal _spoke_any, sent_since_flush, last_sent_final
                    await _tts_ready(piece)
                    await self.tts.send_text_chunk(piece)
                    self.state._tts_flushed_open = False
                    _spoke_any = True
                    sent_since_flush += len(piece)
                    last_sent_final = bool(_SENTENCE_FINAL_RE.search(piece))

                async def _end_input(piece: str = "") -> None:
                    # The context's end of input: the last text (markers
                    # stripped by the caller), or nothing more.
                    nonlocal _context_open, sent_since_flush
                    await _tts_ready(piece)
                    await self.tts.send_text_chunk(piece, is_last=True)
                    self.state._tts_flushed_open = False
                    _context_open = False
                    sent_since_flush = 0

                async def _flush(where: str) -> None:
                    nonlocal sent_since_flush
                    await self.tts.flush()
                    self.state._tts_flushed_open = True
                    sent_since_flush = 0
                    logger.info("[%s] TTS flushed %s", self.conn.peer_addr, where)

                self.state._t_dispatch = time.monotonic()
                self.state._t_text_first = 0.0
                self.state._t_audio_first = 0.0
                # Capture-and-clear the VAD stamp: the turn line covers only
                # dispatches that followed real speech (openings/typed → 0).
                self.state._t_turn_speech_end = self.state._t_speech_end
                self.state._t_speech_end = 0.0
                race = _EngineRace(self.llm.send_message(voice_prompt),
                                   self.state._control_wake, TTS_PAUSE_FLUSH_S)
                try:
                    while True:
                        kind, token = await race.next()
                        if kind is END:
                            _stream_done = True
                            break
                        if kind is WAKE:
                            # An end or transfer accepted mid-turn. With words
                            # spoken (with the TTS or in the buffer) the turn
                            # is cut here: the buffer goes out as the end of
                            # input and the engine's tail is transcript. With
                            # nothing spoken yet the one-sentence rule after
                            # the tool's boundary stands.
                            if (not self._control_pending_end() or _control_break
                                    or self.state._utterance_cancelled):
                                continue
                            if self.state._control_boundary_seen:
                                # The tool's boundary passed before the
                                # acceptance with nothing spoken: the words
                                # after it follow the one-sentence rule,
                                # token by token.
                                continue
                            # Frames the client queued before the control
                            # frame are the words before the tool: they go
                            # out whole with the cut. The tool's boundary, if
                            # queued, stays for its own handling.
                            for early in await race.drain_ready(stop=TOOL_USE_SIGNAL):
                                if not self.state._t_text_first:
                                    self.state._t_text_first = time.monotonic()
                                full_response.append(early)
                                if not (_complete_marker_seen or self.state._control_cut):
                                    text_buffer += early
                            held = _question_marker_seen or _complete_marker_seen
                            piece = "" if held else _CALL_COMPLETE_RE.sub(
                                "", _split_open_bracket(text_buffer)[0]).strip()
                            if not (_spoke_any or piece):
                                continue
                            if piece:
                                await _end_input(piece)
                                _spoke_any = True
                            elif _context_open:
                                await _end_input()
                            _context_open = False
                            sent_since_flush = 0
                            text_buffer = ""
                            self.state._control_cut = True
                            _control_break = True
                            logger.info(
                                "[%s] call control: turn cut at "
                                "the acceptance (%s)",
                                self.conn.peer_addr, self.state._control_action
                            )
                            break
                        if kind is PAUSE:
                            # The engine went quiet after a complete sentence:
                            # a sentence-final buffer goes to the TTS and what
                            # the TTS holds is flushed. Never mid-sentence,
                            # never with nothing sent, never once the turn's
                            # speech is held or closed.
                            if (self.state._utterance_cancelled or not _context_open
                                    or _question_marker_seen or _complete_marker_seen
                                    or (self._control_pending_end()
                                        and self.state._control_boundary_seen)):
                                continue
                            speakable, held_tail = _split_open_bracket(text_buffer)
                            if speakable.strip() and _SENTENCE_FINAL_RE.search(speakable):
                                piece = _CALL_COMPLETE_RE.sub("", speakable).strip()
                                if piece:
                                    await _speak(piece)
                                text_buffer = held_tail
                            if sent_since_flush > 0 and last_sent_final:
                                await _flush(f"after a {TTS_PAUSE_FLUSH_S:.1f}s pause "
                                             f"at a sentence end")
                            continue
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
                            if self._control_pending_end() and not self.state._control_boundary_seen:
                                # The action's own tool boundary (Codex sends it
                                # after the tool ran, with the pre-tool text
                                # still in the buffer: it plays whole below).
                                self.state._control_boundary_seen = True
                                _control_spoke_before = _spoke_any or bool(text_buffer.strip())
                            if not _question_marker_seen and text_buffer.strip():
                                logger.info(
                                    "[%s] Tool start: "
                                    "flushing %s chars to TTS",
                                    self.conn.peer_addr, len(text_buffer)
                                )
                                await _end_input(text_buffer)
                                _spoke_any = True
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

                                # The words before the end or transfer tool
                                # have played: the line closes now, whatever
                                # the engine says next.
                                if self._control_pending_end() and _control_spoke_before:
                                    self.state._control_cut = True
                                    _control_break = True
                                    break

                                # New TTS context for post-tool text
                                self.tts.start_streaming_context(
            language=self._locked_language, output_sample_rate=self._rate_out)
                                _context_open = True
                                self.state._tts_task = asyncio.create_task(
                                    self._stream_tts_audio()
                                )
                            else:
                                # Buffer empty or whitespace-only — discard
                                text_buffer = ""
                                if self._control_pending_end() and _control_spoke_before:
                                    # Spoken earlier in the turn; nothing new
                                    # before this boundary: the line closes.
                                    # The words already with TTS wait for the
                                    # end of input (a streaming provider may
                                    # hold them until then): close it so they
                                    # play, then leave the stream.
                                    if _context_open and not self.state._utterance_cancelled:
                                        await _end_input()
                                    sent_since_flush = 0
                                    self.state._control_cut = True
                                    _control_break = True
                                    break
                                if (_context_open and sent_since_flush > 0
                                        and not self.state._utterance_cancelled):
                                    # A sentence already with the TTS, under
                                    # its first-generation threshold: it plays
                                    # through the tool run, the context stays
                                    # open for the text after the tool.
                                    await _flush("at the tool boundary")
                            continue

                        # --- Normal text token ---
                        full_response.append(token)

                        # [CALL_COMPLETE] streamed: the caller hears what
                        # precedes the marker and NOTHING after it — a report
                        # the model appends behind the marker is for the
                        # transcript, not the line. The tokens still reach
                        # full_response (transcript + post-stream detection)
                        # but never the TTS buffer.
                        if _complete_marker_seen or self.state._control_cut:
                            continue
                        text_buffer += token

                        # After an end or transfer tool with no words before
                        # it: one sentence (or 200 characters) plays, then the
                        # line closes.
                        if self._control_pending_end() and self.state._control_boundary_seen:
                            _m = sentence_end_re.search(text_buffer)
                            if _m or len(text_buffer) >= 200:
                                piece = text_buffer[:_m.end()] if _m else text_buffer[:200]
                                await _end_input(piece.strip())
                                _spoke_any = True
                                text_buffer = ""
                                self.state._control_cut = True
                                _control_break = True
                                break
                            continue

                        # Once [QUESTION marker detected, stop all TTS flushing.
                        # Let tokens accumulate silently — post-stream detection
                        # will handle the complete marker.
                        if _question_marker_seen:
                            continue

                        _m = _CALL_COMPLETE_RE.search(text_buffer)
                        if _m:
                            pre_marker = text_buffer[:_m.start()].strip()
                            if pre_marker:
                                await _speak(pre_marker)
                            text_buffer = ""
                            _complete_marker_seen = True
                            logger.info(
                                "[%s] [CALL_COMPLETE] streamed — "
                                "TTS stops after the text before the marker", self.conn.peer_addr
                            )
                            continue

                        # Check for [QUESTION marker arriving before flush threshold
                        if self._is_outbound and "[QUESTION" in text_buffer:
                            # Flush only text before the marker
                            bracket_idx = text_buffer.find("[QUESTION")
                            pre_marker = text_buffer[:bracket_idx].strip()
                            if pre_marker:
                                tts_chunk = _CALL_COMPLETE_RE.sub("", pre_marker)
                                if tts_chunk:
                                    await _speak(tts_chunk)
                            text_buffer = text_buffer[bracket_idx:]  # keep marker in buffer
                            _question_marker_seen = True
                            logger.info(
                                "[%s] [QUESTION marker "
                                "detected mid-stream — stopping TTS flush", self.conn.peer_addr
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
                                            await _speak(flush_part)
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
                                        "[%s] [QUESTION marker "
                                        "detected mid-stream — stopping TTS flush",
                                        self.conn.peer_addr
                                    )
                            if tts_chunk:
                                await _speak(tts_chunk)
                            text_buffer = ""
                    await race.close()

                    if _control_break:
                        # The last speakable words are with TTS: stop reading
                        # the engine (the proxy keeps its transcript) and go to
                        # the completion point below.
                        discard = getattr(self.llm, "discard_turn", None)
                        if discard is not None:
                            discard()
                        logger.info(
                            "[%s] call control: stream left at "
                            "the last spoken words (%s)",
                            self.conn.peer_addr, self.state._control_action
                        )

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
                            "[%s] Turn aborted mid-stream "
                            "(caller kept talking) — re-dispatching "
                            "%s",
                            self.conn.peer_addr,
                            'batched' if redispatch != queued else 'new speech only'
                        )
                        transcript = await self._wait_for_complete_continuation(
                            redispatch,
                        )
                        continue

                    # Barge-in stopped the stream mid-generation: abort the
                    # turn server-side (Direct) so the model isn't still
                    # answering it; its transcript is folded into the next
                    # dispatch (the erased turn "never happened" upstream).
                    if (not _stream_done and not _control_break
                            and self.state._utterance_cancelled
                            and getattr(self.llm, "supports_abort", False)):
                        await self.llm.abort_turn()
                        if getattr(self.llm, "abort_erases_turn", True):
                            # Erase-style abort only: refold the transcript
                            # into the next dispatch. Interrupt-style keeps
                            # the turn in history — nothing to resend.
                            self.state._aborted_turn_text = transcript
                        _turn_aborted = True
                        logger.info(
                            "[%s] Turn aborted on barge-in "
                            "(mid-generation)", self.conn.peer_addr
                        )

                    # Store full response and unflushed text for barge-in annotation
                    self.state._full_response = "".join(full_response)
                    self.state._tts_unsent_text = "" if _complete_marker_seen else text_buffer

                    # --- Detect [CALL_COMPLETE] in full accumulated response ---
                    _has_question = False
                    if not self.state._utterance_cancelled:
                        full_text = "".join(full_response)
                        if _CALL_COMPLETE_RE.search(full_text) or self._control_pending_end():
                            # The marker, or an end / transfer tool (the first
                            # action wins; an end and a transfer beat a question).
                            self.state._call_complete = True
                            logger.info(
                                "[%s] "
                                "%s "
                                "detected",
                                self.conn.peer_addr,
                                'call control: ' + self.state._control_action if self._control_pending_end() else '[CALL_COMPLETE]'
                            )
                            # Strip marker from remaining buffer before TTS
                            text_buffer = _CALL_COMPLETE_RE.sub("", text_buffer).strip()
                            if self._control_pending_end() and not self.state._control_boundary_seen:
                                # No tool boundary came: everything the engine
                                # produced counts as the words before the tool.
                                pass
                            elif self._control_pending_end() and not self.state._control_cut:
                                # The stream ended inside the one-sentence
                                # allowance: its remainder plays (200 chars).
                                text_buffer = text_buffer[:200]

                        # --- Detect [QUESTION:] — outbound only ---
                        elif self._is_outbound and not self.state._call_complete:
                            if self.state._control_action == "ask":
                                # The ask tool's question; a marker beside it is
                                # dropped (one question per turn).
                                _has_question = True
                                question_text = str(self.state._control_args.get("question") or "").strip()
                                text_buffer = _QUESTION_RE.sub("", text_buffer).strip()
                                self._clear_call_control("question taken")
                            else:
                                q_match = _QUESTION_RE.search(full_text)
                                if q_match:
                                    _has_question = True
                                    question_text = q_match.group(1).strip()
                                    logger.debug(
                                        "[%s] [QUESTION:] detected: "
                                        "%s",
                                        self.conn.peer_addr, question_text[:80]
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
                                "[%s] Early continuation "
                                "detected — skipping final TTS text", self.conn.peer_addr
                            )

                    # Send remaining text as final chunk (strip markers before TTS)
                    if _control_break:
                        # The last speakable words were flushed with is_last
                        # at the cut; the context is closed already.
                        pass
                    elif _early_continuation:
                        # Continuation: close TTS context without final text.
                        # Queued speech handler below will cancel TTS and resend.
                        await _end_input()
                    elif _question_marker_seen or _complete_marker_seen:
                        # A marker was seen mid-stream — nothing after it goes
                        # to TTS (the pre-marker text was already sent).
                        await _end_input()
                    elif text_buffer and not self.state._utterance_cancelled:
                        await _end_input(_CALL_COMPLETE_RE.sub("", text_buffer).strip())
                    elif not self.state._utterance_cancelled:
                        await _end_input()
                    if (self._control_pending_end() and not _control_break
                            and not _spoke_any and not text_buffer.strip()
                            and not self.state._utterance_cancelled):
                        # A tool-first reply with no words at all: the line
                        # still says goodbye (or announces the hand-over).
                        if self.state._tts_task and not self.state._tts_task.done():
                            with contextlib.suppress(asyncio.CancelledError):
                                await self.state._tts_task
                        self.state._tts_task = None
                        with contextlib.suppress(Exception):
                            await self._play_phrase(
                                "transferring" if self.state._transfer_target else "goodbye")

                    # --- Handle question: wait for TTS, play hold, wait for answer ---
                    if _has_question and self._in_session:
                        # Nobody to ask on a call that runs inside the caller's
                        # chat: the marker was stripped from the speech, the
                        # call goes on.
                        logger.info(
                            "[%s] [QUESTION:] on an in-session call — "
                            "no hold, the question is dropped", self.conn.peer_addr)
                        _has_question = False
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
                    logger.debug("[%s] Utterance cancelled (barge-in)", self.conn.peer_addr)
                    return
                finally:
                    await race.close()

            # --- LLM is done (or pre-collected text sent to TTS). ---

            # The proxy went away during the turn (an in-session call's chat
            # attach with it): the apology, the hangup (nobody else may
            # answer this call).
            if self.state._proxy_lost or getattr(self.llm, "proxy_lost", False):
                await self._end_unreachable_call()
                break

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
                # caller rather than deadlocking on a forever-parked TTS. A
                # transfer the agent asked for goes through regardless: the
                # caller's "thanks" over the hand-over sentence must not
                # strand them with the agent. After a call tool's acceptance
                # only speech that STARTED after it counts: a short goodbye
                # plays while the context is still open, and the caller's
                # "thanks, bye" over that silence is the end of the call,
                # not a new turn.
                accepted = (self.state._control_accepted_at
                            if self._control_pending_end() else 0.0)
                last_input = max(self.state._user_silent.speech_started_at,
                                 self.state._typed_input_at)
                engaged = self.state._utterance_cancelled or bool(
                    self.state._queued_speech
                    and (not accepted or last_input >= accepted))
                if engaged and self.state._transfer_target is None:
                    self.state._call_complete = False
                    self._clear_call_control("farewell resumed")
                    logger.info(
                        "[%s] Caller still engaged during "
                        "farewell — resuming instead of hanging up", self.conn.peer_addr
                    )
                    if not self.state._queued_speech:
                        return  # not queued yet; the next turn will pick it up
                    # else fall through to the queued-speech handler below
                else:
                    # The completion point: the transfer when one is pending,
                    # else the brief pause and the hangup.
                    if await self._execute_pending_end():
                        break
                    # A failed transfer: the call resumes, the agent told.
                    transcript = self.state._control_note
                    self.state._control_note = ""
                    is_opening = True
                    self.state._last_activity = time.monotonic()
                    continue

            # Wait for user to finish speaking before processing queued speech.
            # Without this, we'd grab a partial sentence from the queue while
            # the user is still mid-sentence (VAD split on a brief pause).
            # Applies to both barge-in AND normal queued-speech scenarios.
            if self.state._queued_speech and not self.state._user_silent.is_set():
                logger.info(
                    "[%s] Queued speech found but user "
                    "still speaking — waiting for silence", self.conn.peer_addr
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
                        "[%s] User resumed — waiting again",
                        self.conn.peer_addr
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
                        "[%s] Continuation (LLM done) "
                        "— new turn: %s", self.conn.peer_addr, resend_text
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
                        "[%s] Starting parallel LLM "
                        "while TTS plays: %s", self.conn.peer_addr, next_transcript
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
                        logger.warning("[%s] Pre-tool text wait timed out", self.conn.peer_addr)

                    if self.state._parallel_pre_tool_text:
                        logger.info(
                            "[%s] Playing intermediate pre-tool TTS: "
                            "%s",
                            self.conn.peer_addr, self.state._parallel_pre_tool_text[:60]
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
                        logger.debug("[%s] Parallel LLM cancelled", self.conn.peer_addr)
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
                            "[%s] Speech queued during "
                            "TTS — cancelling for reprocessing", self.conn.peer_addr
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
                        "[%s] Late speech after aborted "
                        "turn — re-dispatching batched", self.conn.peer_addr
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
                        "[%s] Late continuation — "
                        "new turn: %s", self.conn.peer_addr, late_text
                    )
                    transcript = await self._wait_for_complete_continuation(
                        late_text,
                    )
                    continue
                else:
                    transcript = late_text
                    logger.debug("[%s] Processing queued speech: %s", self.conn.peer_addr, transcript)
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
        # The call tools' speech rule, as in the streaming path: after an
        # end or transfer tool's boundary, text is kept only when nothing
        # came before it, and then one sentence.
        control_cut = False
        control_spoke_before = False
        sentence_end_re = re.compile(r"[.!?;]\s")
        # The same race as the streaming path, without the pause rule (the
        # text is collected, not spoken yet): an acceptance with words
        # already collected cuts the collection and leaves the stream.
        race = _EngineRace(self.llm.send_message(voice_prompt), self.state._control_wake)
        try:
            while True:
                kind, token = await race.next()
                if kind is END:
                    break
                if kind is WAKE:
                    # The words queued before the control frame are the
                    # reply's own (the tool's boundary keeps its handling).
                    for early in await race.drain_ready(stop=TOOL_USE_SIGNAL):
                        (post_tool if pre_tool_signalled else pre_tool).append(early)
                    if (self._control_pending_end() and not control_cut
                            and "".join(pre_tool + post_tool).strip()):
                        control_cut = True
                        break
                    continue
                if control_cut:
                    break   # the rest is transcript: the proxy keeps the engine's text
                if token is TOOL_USE_SIGNAL:
                    if self._control_pending_end() and not self.state._control_boundary_seen:
                        self.state._control_boundary_seen = True
                        control_spoke_before = bool("".join(pre_tool + post_tool).strip())
                        if control_spoke_before:
                            control_cut = True
                    if not pre_tool_signalled:
                        pre_tool_text = "".join(pre_tool).strip()
                        if pre_tool_text:
                            self.state._parallel_pre_tool_text = pre_tool_text
                            logger.info(
                                "[%s] Parallel LLM pre-tool text "
                                "(%s chars): %s",
                                self.conn.peer_addr, len(pre_tool_text), pre_tool_text[:80]
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
                if self._control_pending_end() and self.state._control_boundary_seen:
                    tail = "".join(post_tool if pre_tool_signalled else pre_tool)
                    _m = sentence_end_re.search(tail)
                    if _m or len(tail) >= 200:
                        kept = tail[:_m.end()] if _m else tail[:200]
                        if pre_tool_signalled:
                            post_tool[:] = [kept]
                        else:
                            pre_tool[:] = [kept]
                        control_cut = True
            await race.close()
            if control_cut:
                # The last speakable words are collected: the engine's tail
                # is the proxy's transcript, the drain is the completion point.
                discard = getattr(self.llm, "discard_turn", None)
                if discard is not None:
                    discard()
                logger.info(
                    "[%s] call control: parallel stream left at "
                    "the last spoken words (%s)",
                    self.conn.peer_addr, self.state._control_action
                )
        except asyncio.CancelledError:
            while len(self.llm.messages) > msg_count_before:
                self.llm.messages.pop()
            # Ensure event is set so caller isn't stuck waiting
            self.state._parallel_pre_tool_ready.set()
            raise
        except Exception as e:  # noqa: BLE001  # the parallel answer's failure leaves the turn empty, never a dead call
            logger.error("[%s] Parallel LLM error: %s", self.conn.peer_addr, e)
            while len(self.llm.messages) > msg_count_before:
                self.llm.messages.pop()
            self.state._parallel_pre_tool_ready.set()
            return ""
        finally:
            await race.close()

        if not pre_tool_signalled:
            # No tool was called — signal ready with no pre-tool text
            self.state._parallel_pre_tool_ready.set()
            return "".join(pre_tool)

        # Tool was called — return only post-tool text
        return "".join(post_tool)

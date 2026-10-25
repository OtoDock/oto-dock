"""Outbound opening/warmup, finalization, and the [QUESTION:] hold/answer cycle.

Mixin for :class:`pipeline.core.CallPipeline`; operates on ``self.state``
(CallState) plus the injected collaborators (conn/cfg/stt/vad/tts/llm).
"""

import asyncio
import contextlib
import logging
import re
import time

from proxy.client import TOOL_USE_SIGNAL
from telephony.audio_socket import TYPE_AUDIO, TYPE_HANGUP, AudioSocketError

from .markers import _CALL_COMPLETE_RE, _QUESTION_RE

logger = logging.getLogger("pipeline")

#: How long a call that ends before adopting its pre-warmed client waits for
#: the opening still generating on it (the client exists only once it is).
UNADOPTED_OPENING_WAIT_S = 15.0
#: Why an outbound call ended at the PIN gate, as the record and the chat
#: read it.
PIN_REFUSED = "the person who answered did not enter the PIN"
PIN_COOLDOWN = "the number was still in PIN cooldown, nobody was asked"


class OutboundMixin:
    """Outbound opening/warmup, finalization, and the [QUESTION:] hold/answer cycle."""

    async def _settle_unadopted_outbound(self, *, adopt: bool) -> None:
        """An outbound call ending before it adopted its pre-warmed client
        (the PIN gate refused the person, a hangup during the gate, the max
        duration): the client, and the opening still generating on it, must
        not stay open on the proxy. ``adopt`` takes the client as the call's
        own (``self.llm``) so the finalize can tell the record and the chat
        why (a refused code, a hangup during the gate), and the cleanup
        closes it; any other exit closes it here, after cancelling a
        pre-generation still running."""
        if not self._call_manager or not self._outbound_call_id:
            return
        call = self._call_manager.get_call(self._outbound_call_id)
        if call is None:
            return
        ready = getattr(call, "_opening_ready", None)
        if ready is not None and not ready.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ready.wait(), timeout=UNADOPTED_OPENING_WAIT_S)
        client = getattr(call, "warmup_client", None)
        if adopt and client is not None:
            call.warmup_client = None
            self.llm = client
            return
        task = getattr(call, "pregen_task", None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        client = getattr(call, "warmup_client", None)
        if client is not None:
            call.warmup_client = None
            with contextlib.suppress(Exception):
                await client.close()

    async def _adopt_prewarmed_backend(self):
        """Take over the connection that pre-warmed this call's session during
        ringing — WAITING for the opening it may still be generating.

        Returns the client to use as ``self.llm`` (None → create a fresh
        backend). The pre-generation runs on the SAME proxy session the live
        call continues on; sending the opening prompt again while it is still
        in flight queues a duplicate prompt behind it (live-hit 2026-09-07: a
        local model needed ~20 s for the first turn, the old 15 s wait fell
        back, the model saw the task twice and hung up on its second answer).
        So the wait is bounded only by ``opening_pregen_timeout_s`` and a
        timeout CANCELS the pre-generation (its client closes; the fallback
        turn then runs on a fresh session) instead of racing it. The line
        stays SILENT while waiting — no thinking filler, no other
        pre-recorded audio: the first thing the callee hears is the agent's
        own opening (operator decision 2026-09-07 after a call that played
        four fillers in a row).
        """
        call = self._call_manager.get_call(self._outbound_call_id) if self._call_manager else None
        if call is None:
            return None
        if not call._opening_ready.is_set():
            timeout = self.cfg.opening_pregen_timeout_s
            t0 = time.monotonic()
            if await self._wait_for_opening(call._opening_ready, timeout):
                logger.info(
                    "[%s] Opening pre-gen finished after "
                    "%.1fs of waiting",
                    self.conn.peer_addr, time.monotonic() - t0
                )
            else:
                logger.warning(
                    "[%s] Opening pre-gen still running after "
                    "%.0fs — cancelling it, falling back to live LLM",
                    self.conn.peer_addr, timeout
                )
                task = getattr(call, "pregen_task", None)
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                call.opening_text = None
                call.warmup_client = None
                call._opening_ready.set()
        pw_client = getattr(call, "warmup_client", None)
        if pw_client is None:
            return None
        call.warmup_client = None  # pipeline takes ownership
        if (getattr(pw_client, "_ws_connected", False)
                and getattr(pw_client, "llm_mode", "proxy") == self.route.llm_mode):
            logger.info(
                "[%s] Reusing pre-warmed connection "
                "(session=%s)", self.conn.peer_addr, pw_client.session_id
            )
            return pw_client
        with contextlib.suppress(Exception):
            await pw_client.close()
        return None

    @staticmethod
    async def _wait_for_opening(event: asyncio.Event, timeout: float) -> bool:
        """Wait for ``event`` up to ``timeout`` seconds, silently (the callee
        hears nothing before the agent's own opening). Returns True when the
        event fired."""
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _send_outbound_opening(self) -> None:
        """Speak the opening line for an outbound call.

        If the opening was pre-generated during ringing (call.opening_text),
        skip LLM and go straight to TTS — eliminates the 5-6s LLM delay.
        Falls back to live LLM generation if pre-generation didn't finish.
        """
        call = self._call_manager.get_call(self._outbound_call_id) if self._call_manager else None
        if not call:
            logger.error("[%s] Outbound call not found: %s", self.conn.peer_addr, self._outbound_call_id)
            return

        if getattr(self.llm, "proxy_lost", False) or (
                self._in_session and not getattr(self.llm, "session_id", None)):
            # No session to speak from: the proxy's socket never opened, or
            # the chat refused or lost the attach. A short apology, then the
            # line closes; an in-session call's finalize tells the chat (when
            # the socket still stands).
            await self._end_unreachable_call()
            return

        # The pre-generation was awaited (or cancelled) when the backend was
        # adopted (_adopt_prewarmed_backend); this is only a guard for a
        # caller that skipped that step.
        try:
            await asyncio.wait_for(call._opening_ready.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            logger.warning("[%s] Opening pre-gen timed out — falling back to live LLM", self.conn.peer_addr)

        if call.opening_text:
            # Fast path: pre-generated text ready — straight to TTS
            logger.debug(
                "[%s] Using pre-generated opening "
                "(%s chars): %s...",
                self.conn.peer_addr, len(call.opening_text), call.opening_text[:80]
            )

            # Record transcript
            if self._call_manager:
                self._call_manager.add_transcript_entry(
                    self._outbound_call_id, "assistant", call.opening_text,
                )

            # Play via TTS while discarding incoming audio frames.
            # Audio frames accumulate in the TCP buffer from the moment
            # AudioSocket connects (the receiver's greeting etc.).
            # We read and discard them during TTS so the listen loop
            # starts with a clean, real-time audio stream.
            self._select_tts(self._locked_language)

            self.tts.start_streaming_context(
            language=self._locked_language, output_sample_rate=self._rate_out)
            self.state._audio_out_buf.clear()
            self.state._utterance_cancelled = False
            self.state._tts_task = asyncio.create_task(self._stream_tts_audio())

            # Discard incoming frames in parallel with TTS.
            # Audio accumulates in the TCP buffer from the callee's greeting
            # etc. — we drain it so the listen loop starts with real-time audio.
            discard_stop = asyncio.Event()
            discard_task = asyncio.create_task(
                self._discard_incoming_frames(discard_stop)
            )

            await self.tts.send_text_chunk(call.opening_text, is_last=True)

            # Wait for TTS to finish
            if self.state._tts_task and not self.state._tts_task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await self.state._tts_task

            # Stop discarding — listen loop will take over frame reading
            discard_stop.set()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await discard_task

            self.state._tts_playing = False
            self.vad.set_bargein_mode(False)
            self.state._tts_task = None
            self.state._last_activity = time.monotonic()

            # If the agent's opening itself ended the call ([CALL_COMPLETE] in
            # the pre-generated opening — a "say X then hang up" task), complete
            # and hang up instead of listening. Mirrors the utterance loop's
            # hangup so this fast path matches the live-LLM slow path.
            if call.opening_completes_call:
                self.state._call_complete = True
                await asyncio.sleep(0.5)  # let the farewell audio flush
                self.conn.send_hangup()
                self.state._running = False
                logger.info(
                    "[%s] [CALL_COMPLETE] in opening — hanging up",
                    self.conn.peer_addr
                )
                return

            # Let the STT provider check its own health and recover if needed.
            # Opening semantics: clear stale echo transcripts when healthy.
            if self.state._stt_active:
                self.state._stt_active = await self.stt.ensure_alive(
                    self._locked_language, clear_queue=True,
                )
                self.state._stt_last_fed = time.monotonic()
        else:
            # Slow path: pre-generation failed or empty — generate live via LLM
            task_desc = call.task_description
            instructions = call.instructions or ""
            extra = f" Instructions: {instructions}" if instructions else ""

            prompt = (
                f"Task: {task_desc}.{extra}\n"
                f"The person just answered the phone. "
                f"Introduce yourself briefly and begin working on the task."
            )

            logger.debug(
                "[%s] No pre-generated opening — "
                "falling back to live LLM for task: %s", self.conn.peer_addr, task_desc
            )

            # Discard incoming frames in parallel with LLM+TTS
            discard_stop = asyncio.Event()
            discard_task = asyncio.create_task(
                self._discard_incoming_frames(discard_stop)
            )

            try:
                await self._process_utterance(prompt, is_opening=True)
            finally:
                discard_stop.set()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await discard_task

            # Let the STT provider check its own health and recover if needed.
            # Opening semantics: clear stale echo transcripts when healthy.
            if self.state._stt_active:
                self.state._stt_active = await self.stt.ensure_alive(
                    self._locked_language, clear_queue=True,
                )
                self.state._stt_last_fed = time.monotonic()

    async def _discard_incoming_frames(
        self,
        stop: asyncio.Event,
    ) -> None:
        """Read and discard incoming audio during outbound opening TTS.

        Drains the AudioSocket TCP receive buffer so frames don't accumulate.
        The STT stream gets each frame as SILENCE via ``feed_during_tts``
        (same echo-management contract as playback mute) so its audio
        timeline stays continuous — live-hit 2026-08-14 (Call C): with only
        KeepAlives for the ~10 s opening, Deepgram's socket survived but the
        streaming session went stale and the first two real utterances after
        the opening produced no transcript at all. The periodic
        ``feed_during_opening`` keepalive stays as the belt for providers
        whose feed_during_tts is a no-op.
        """
        frames_read = 0
        try:
            while not stop.is_set():
                try:
                    frame_type, payload = await asyncio.wait_for(
                        self.conn.read_frame(), timeout=0.1,
                    )
                except asyncio.TimeoutError:
                    if self.state._stt_active:
                        await self.stt.feed_during_opening()
                    continue
                if frame_type == TYPE_HANGUP:
                    raise AudioSocketError("Hangup during opening")
                if frame_type != TYPE_AUDIO or not payload:
                    continue

                frames_read += 1
                if self.state._stt_active:
                    # Timeline continuity: zeros at the exact inbound cadence.
                    await self.stt.feed_during_tts(payload)
                    self.state._stt_last_fed = time.monotonic()
                    # Belt: periodic protocol keepalive (every ~20 frames ≈ 400ms)
                    if frames_read % 20 == 0:
                        await self.stt.feed_during_opening()

        except AudioSocketError:
            raise
        except Exception:  # noqa: BLE001  # the opening's frame drain runs beside the call, whatever it meets
            pass
        finally:
            if frames_read:
                logger.info(
                    "[%s] Discarded %s frames "
                    "during opening", self.conn.peer_addr, frames_read
                )

    def _on_proxy_lost(self) -> None:
        """The proxy's socket is gone (the proxy restarted or closed the
        call's socket; for an in-session call also when the chat was stopped
        or deleted, or its turn ended). The person is never left on a silent
        line: the call ends with the apology now, or right after the turn
        that is running."""
        if not self.state._running:
            return
        self.state._proxy_lost = True
        task = self.state._utterance_task
        if task is not None and not task.done():
            return   # the dispatch sees the flag at its stream's end
        asyncio.get_running_loop().create_task(self._end_unreachable_call())

    async def _end_unreachable_call(self) -> None:
        if self.state._unreachable_ending:
            return
        self.state._unreachable_ending = True
        logger.warning(
            "[%s] The proxy is not reachable for this call "
            "— ending it", self.conn.peer_addr)
        self.state.call_outcome = "error"
        with contextlib.suppress(Exception):
            await self._play_phrase("cannot_continue")
        await asyncio.sleep(0.5)
        self.conn.send_hangup()
        self.state._running = False

    def _in_session_ending(self) -> str:
        """How an in-session call ended, for the resume note and the record."""
        if self.state._transferred_to:
            return f"transferred to {self.state._transferred_to}"
        if self.state.call_outcome == "error":
            return "ended by an error"
        if self.state.call_outcome == "pin_cooldown":
            return f"ended: {PIN_COOLDOWN}"
        if self.state.call_outcome.startswith("pin_"):
            return f"ended: {PIN_REFUSED}"
        if self.state._call_complete or self._control_pending_end():
            return "completed: you ended it"
        if self.state.call_outcome == "hangup":
            return "the person hung up"
        return f"ended after {int(self.cfg.idle_timeout_s)} s of silence"

    async def _finalize_in_session_call(self, call) -> None:
        """The chat gets the resume note as the call's last turn (the reply is
        not awaited: the proxy's producer runs it after the socket closes),
        and the record its ending; no summary turn, the chat has the whole
        exchange as rows."""
        from calls.call_manager import CallStatus
        how = self._in_session_ending()
        note = (
            f"[The phone call has ended ({how}); the line is closed. You are back in the "
            "text chat: the phone rules no longer apply. Summarise the call's outcome for "
            "the user in one or two sentences, then continue your task.]")
        sent = False
        with contextlib.suppress(Exception):
            sent = await self.llm.send_system_note(note, resume=True)
        if self.state._transferred_to:
            call.transferred_to = self.state._transferred_to
        failed = self.state.call_outcome == "error" or not (
            self.state._transferred_to or self.state._call_complete
            or self._control_pending_end())
        if failed and self.state.call_outcome != "error":
            self.state._call_complete = False
        self._call_manager.update_status(
            self._outbound_call_id,
            CallStatus.FAILED if failed else CallStatus.COMPLETED,
            outcome=None if failed else f"Ran in the chat, {how}",
            error=f"ran in the chat, {how}" if failed else None,
        )
        logger.info(
            "[%s] In-session call finished: %s "
            "(resume note %s)",
            self.conn.peer_addr, how, 'sent' if sent else 'not sent')

    async def _finalize_outbound_call(self) -> None:
        """Finalize the outbound call: get outcome summary and update status."""
        if not self._call_manager or not self._outbound_call_id:
            return

        call = self._call_manager.get_call(self._outbound_call_id)
        if not call:
            return

        # A transfer in flight when the media leg closed: its outcome first.
        await self._settle_transfer()
        # The PIN gate's result rides the record into the terminal report
        # (set before the status update that fires it). An outbound record
        # carries the failures the windows counted, never the silent rounds.
        call.pin_attempts = self.state.pin_failures
        call.pin_outcome = (self.state.call_outcome
                            if self.state.call_outcome.startswith("pin_") else "")
        call.hung_up = self.state.call_outcome == "hangup"
        from calls.call_manager import CallStatus
        if self._in_session:
            await self._finalize_in_session_call(call)
            return
        if call.pin_outcome:
            self._call_manager.update_status(
                self._outbound_call_id, CallStatus.FAILED,
                error=PIN_COOLDOWN if call.pin_outcome == "pin_cooldown" else PIN_REFUSED)
            logger.warning(
                "[%s] Outbound call ended at the PIN gate "
                "(%s, %s wrong codes)",
                self.conn.peer_addr, call.pin_outcome, self.state.pin_failures)
            return
        if self.state._transferred_to:
            call.transferred_to = self.state._transferred_to
            self._call_manager.update_status(
                self._outbound_call_id, CallStatus.COMPLETED,
                outcome=f"Transferred to {self.state._transferred_to}",
            )
            logger.info("[%s] Outbound call transferred to %s", self.conn.peer_addr, self.state._transferred_to)
            return
        # An end the agent asked for counts, whoever hung up first.
        if self._control_pending_end():
            self.state._call_complete = True

        if self.state._call_complete:
            # Ask the LLM for a 1-sentence outcome summary
            try:
                summary_prompt = (
                    "The call has ended. Based on the conversation, provide a single "
                    "sentence summarizing the outcome (e.g., 'Reservation confirmed "
                    "for 8pm Saturday, 4 people'). Reply ONLY with the summary, "
                    "nothing else. Use the same language as the conversation."
                )
                summary_parts = []
                async for token in self.llm.send_message(summary_prompt):
                    if token is TOOL_USE_SIGNAL:
                        continue
                    summary_parts.append(token)
                outcome = "".join(summary_parts).strip()
                # Clean any session markers from summary
                outcome = re.sub(r"\[//\]: # \(session:[a-f0-9-]+\)", "", outcome).strip()
            except Exception as e:  # noqa: BLE001  # the outcome summary is best effort, the call already ended
                logger.error("[%s] Failed to get outcome summary: %s", self.conn.peer_addr, e)
                outcome = "Call completed (summary unavailable)"

            self._call_manager.update_status(
                self._outbound_call_id,
                CallStatus.COMPLETED,
                outcome=outcome,
            )
            logger.info("[%s] Outbound call completed: %s", self.conn.peer_addr, outcome)
        else:
            if self.state.call_outcome == "hangup":
                how = "the person hung up before the call was completed"
            elif self.state.call_outcome == "error":
                how = "ended by an error"
            else:
                how = f"ended after {int(self.cfg.idle_timeout_s)} s of silence"
            self._call_manager.update_status(
                self._outbound_call_id, CallStatus.FAILED, error=how)
            logger.warning("[%s] Outbound call ended without completion: %s", self.conn.peer_addr, how)

    async def _handle_question(self, question_text: str) -> str | None:
        """Handle a [QUESTION:] marker from the caller agent.

        Posts the question to call_manager, plays hold TTS, then waits
        for the calling agent to answer via HTTP.  While waiting, the
        caller agent can still respond to the callee (e.g. "Yes, I'm
        still checking") so the phone conversation stays natural.
        Returns the answer text, or None on timeout.
        """
        if not self._call_manager or not self._outbound_call_id:
            return None

        self._call_manager.post_question(self._outbound_call_id, question_text)

        # Play hold message while waiting for answer (per-language)
        hold_msg = self.cfg.voice_phrases.get(self._locked_language, {}).get(
            "hold_message", "One moment please.",
        )
        self._select_tts(self._locked_language)

        self.tts.start_streaming_context(
            language=self._locked_language, output_sample_rate=self._rate_out)
        self.state._audio_out_buf.clear()
        self.state._utterance_cancelled = False
        self.state._tts_task = asyncio.create_task(self._stream_tts_audio())
        await self.tts.send_text_chunk(hold_msg, is_last=True)

        # Wait for hold TTS to finish
        if self.state._tts_task and not self.state._tts_task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await self.state._tts_task
        self.state._tts_playing = False
        self.vad.set_bargein_mode(False)
        self.state._tts_task = None

        # Poll for answer while responding to callee speech.
        # Also send silence to AudioSocket to keep RTP alive —
        # carriers drop calls after 10-20s of no outbound audio.
        answer_task = asyncio.create_task(
            self._call_manager.wait_for_answer(
                self._outbound_call_id,
                timeout=self.cfg.question_answer_timeout_s,
            )
        )
        silence_task = asyncio.create_task(self._send_hold_silence())

        try:
            while not answer_task.done():
                # Wait up to 0.5s for the answer
                done, _ = await asyncio.wait({answer_task}, timeout=0.5)
                if done:
                    break

                # Callee spoke during the wait — respond briefly
                if self.state._queued_speech:
                    speech = " ".join(self.state._queued_speech)
                    self.state._queued_speech.clear()
                    await self._respond_during_hold(speech)

            answer = answer_task.result()
        except asyncio.CancelledError:
            answer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await answer_task
            raise
        finally:
            silence_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await silence_task

        logger.debug(
            "[%s] Question answer: "
            "%s",
            self.conn.peer_addr, answer[:80] if answer else 'TIMEOUT'
        )
        return answer

    async def _send_hold_silence(self) -> None:
        """Send silence frames to AudioSocket to keep the RTP stream alive.

        Runs in the background during _handle_question.  Without outbound
        audio, the SIP carrier may drop the call after 10-20s of inactivity.
        Sends one silence frame every 20ms (matching the AudioSocket frame rate).

        Skips sending when TTS is playing — the TTS audio itself keeps RTP
        alive, and interleaving silence with TTS would cause garbled audio.
        With an ambience bed active, its continuous frames already keep RTP
        alive, so this sender stands down entirely.
        """
        if self._ambience is not None:
            return
        silence = b"\x00" * self._frame_bytes_out
        try:
            while True:
                if not self.state._tts_playing:
                    try:
                        self._send_frame(silence)
                    except Exception:  # noqa: BLE001  # a frame the closing line refuses ends the hold silence
                        break
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            return

    async def _respond_during_hold(self, speech: str) -> None:
        """Respond briefly to callee while waiting for the manager's answer.

        Sends callee speech to the LLM with hold context, plays the
        short response via TTS, then returns so the answer poll continues.
        """
        logger.debug("[%s] Callee spoke during hold: %s", self.conn.peer_addr, speech)

        # Record callee speech in transcript
        if self._call_manager:
            self._call_manager.add_transcript_entry(
                self._outbound_call_id, "human", speech,
            )

        prompt = (
            "[HOLD: You asked a question to your manager and are waiting "
            "for the answer. The person on the phone just spoke. Respond "
            "VERY briefly (1 sentence max) — acknowledge them and let them "
            "know you're still checking. Do NOT make up an answer.]\n"
            f"{speech}"
        )

        self._select_tts(self._locked_language)
        self.tts.start_streaming_context(
            language=self._locked_language, output_sample_rate=self._rate_out)
        self.state._audio_out_buf.clear()
        self.state._utterance_cancelled = False
        self.state._tts_task = asyncio.create_task(self._stream_tts_audio())

        # Collect full LLM response (should be very short)
        response_text = ""
        try:
            async for token in self.llm.send_message(prompt):
                if self.state._utterance_cancelled:
                    break
                if token is TOOL_USE_SIGNAL:
                    continue
                response_text += token
        except Exception as e:  # noqa: BLE001  # the hold response is best effort, the hold goes on
            logger.error("[%s] Hold response LLM error: %s", self.conn.peer_addr, e)

        # Clean markers and send to TTS
        tts_text = response_text.strip()
        if tts_text:
            tts_text = _CALL_COMPLETE_RE.sub("", tts_text)
            tts_text = _QUESTION_RE.sub("", tts_text).strip()
        if tts_text:
            await self.tts.send_text_chunk(tts_text, is_last=True)
        else:
            await self.tts.send_text_chunk("", is_last=True)

        # Wait for TTS to finish
        if self.state._tts_task and not self.state._tts_task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await self.state._tts_task
        self.state._tts_playing = False
        self.vad.set_bargein_mode(False)
        self.state._tts_task = None

        # Record assistant response in transcript
        clean = re.sub(r"\[//\]: # \(session:[a-f0-9-]+\)", "", response_text)
        clean = _CALL_COMPLETE_RE.sub("", clean)
        clean = _QUESTION_RE.sub("", clean).strip()
        if self._call_manager and clean:
            self._call_manager.add_transcript_entry(
                self._outbound_call_id, "assistant", clean,
            )

        # Notify STT that TTS finished (echo cleanup)
        self.stt.on_tts_finished(was_interrupted=False)
        self.state._stt_early_unmuted = False

        logger.debug(
            "[%s] Hold response: "
            "%s",
            self.conn.peer_addr, clean[:80] if clean else '(empty)'
        )

    async def _warmup_session(self) -> None:
        """Pre-create persistent session during greeting (proxy mode only).

        Starts Claude process + MCP servers without sending a user message.
        No tokens wasted — just process and tool initialization.
        For direct mode, starts MCP servers on the proxy side.
        """
        t0 = time.monotonic()
        try:
            sid = await self.llm.warmup_session()
            logger.info(
                "[%s] Session warmup complete "
                "(session=%s, %.0fms)",
                self.conn.peer_addr, sid, (time.monotonic() - t0) * 1000
            )
        except Exception as e:  # noqa: BLE001  # the session warmup is best effort, the first turn warms it
            logger.warning("[%s] Session warmup failed: %s", self.conn.peer_addr, e)
        finally:
            self.state._warmup_done.set()

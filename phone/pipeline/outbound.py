"""Outbound opening/warmup, finalization, and the [QUESTION:] hold/answer cycle.

Mixin for :class:`pipeline.core.CallPipeline`; operates on ``self.state``
(CallState) plus the injected collaborators (conn/cfg/stt/vad/tts/llm).
"""

import asyncio
import contextlib
import logging
import time
import re

from telephony.audio_socket import AudioSocketError, TYPE_AUDIO, TYPE_HANGUP
from proxy.client import TOOL_USE_SIGNAL
from .markers import _CALL_COMPLETE_RE, _QUESTION_RE

logger = logging.getLogger("pipeline")


class OutboundMixin:
    """Outbound opening/warmup, finalization, and the [QUESTION:] hold/answer cycle."""

    async def _send_outbound_opening(self) -> None:
        """Speak the opening line for an outbound call.

        If the opening was pre-generated during ringing (call.opening_text),
        skip LLM and go straight to TTS — eliminates the 5-6s LLM delay.
        Falls back to live LLM generation if pre-generation didn't finish.
        """
        call = self._call_manager.get_call(self._outbound_call_id) if self._call_manager else None
        if not call:
            logger.error(f"[{self.conn.peer_addr}] Outbound call not found: {self._outbound_call_id}")
            return

        # Wait for pre-generation to finish (may still be running)
        try:
            await asyncio.wait_for(call._opening_ready.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            logger.warning(f"[{self.conn.peer_addr}] Opening pre-gen timed out — falling back to live LLM")

        if call.opening_text:
            # Fast path: pre-generated text ready — straight to TTS
            logger.debug(
                f"[{self.conn.peer_addr}] Using pre-generated opening "
                f"({len(call.opening_text)} chars): {call.opening_text[:80]}..."
            )

            # Inject the opening exchange into local LLM messages for session continuity.
            # Only needed for proxy (CLI) mode — the session marker lets --resume work.
            # For direct mode, the proxy already has the messages stored server-side.
            if not self._is_direct:
                if call.opening_prompt:
                    self.llm.messages.append({"role": "user", "content": call.opening_prompt})
                response_content = call.opening_text
                if getattr(self.llm, 'session_id', None):
                    response_content += f"\n\n[//]: # (session:{self.llm.session_id})"
                self.llm.messages.append({"role": "assistant", "content": response_content})

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
                    f"[{self.conn.peer_addr}] [CALL_COMPLETE] in opening — hanging up"
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
                f"[{self.conn.peer_addr}] No pre-generated opening — "
                f"falling back to live LLM for task: {task_desc}"
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
        except Exception:
            pass
        finally:
            if frames_read:
                logger.info(
                    f"[{self.conn.peer_addr}] Discarded {frames_read} frames "
                    f"during opening"
                )

    async def _finalize_outbound_call(self) -> None:
        """Finalize the outbound call: get outcome summary and update status."""
        if not self._call_manager or not self._outbound_call_id:
            return

        call = self._call_manager.get_call(self._outbound_call_id)
        if not call:
            return

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
            except Exception as e:
                logger.error(f"[{self.conn.peer_addr}] Failed to get outcome summary: {e}")
                outcome = "Call completed (summary unavailable)"

            from calls.call_manager import CallStatus
            self._call_manager.update_status(
                self._outbound_call_id,
                CallStatus.COMPLETED,
                outcome=outcome,
            )
            logger.info(f"[{self.conn.peer_addr}] Outbound call completed: {outcome}")
        else:
            from calls.call_manager import CallStatus
            self._call_manager.update_status(
                self._outbound_call_id,
                CallStatus.FAILED,
                error="call ended without [CALL_COMPLETE] marker",
            )
            logger.warning(f"[{self.conn.peer_addr}] Outbound call ended without completion")

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
            f"[{self.conn.peer_addr}] Question answer: "
            f"{answer[:80] if answer else 'TIMEOUT'}"
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
                        self.conn.send_audio(silence)
                    except Exception:
                        break
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            return

    async def _respond_during_hold(self, speech: str) -> None:
        """Respond briefly to callee while waiting for the manager's answer.

        Sends callee speech to the LLM with hold context, plays the
        short response via TTS, then returns so the answer poll continues.
        """
        logger.debug(f"[{self.conn.peer_addr}] Callee spoke during hold: {speech}")

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
        except Exception as e:
            logger.error(f"[{self.conn.peer_addr}] Hold response LLM error: {e}")

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
            f"[{self.conn.peer_addr}] Hold response: "
            f"{clean[:80] if clean else '(empty)'}"
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
                f"[{self.conn.peer_addr}] Session warmup complete "
                f"(session={sid}, {(time.monotonic() - t0) * 1000:.0f}ms)"
            )
        except Exception as e:
            logger.warning(f"[{self.conn.peer_addr}] Session warmup failed: {e}")
        finally:
            self.state._warmup_done.set()

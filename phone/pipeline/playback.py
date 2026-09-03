"""Paced TTS playback, fillers, and barge-in cancellation.

Mixin for :class:`pipeline.core.CallPipeline`; operates on ``self.state``
(CallState) plus the injected collaborators (conn/cfg/stt/vad/tts/llm).
"""

import asyncio
import contextlib
import logging
import time
import numpy as np

from audio.providers.vad.base import VadState
from audio.providers.tts.base import TTSProvider
from audio.streaming.lang import base_lang
from fillers import filler_cache

logger = logging.getLogger("pipeline")


class PlaybackMixin:
    """Paced TTS playback, fillers, and barge-in cancellation."""

    # Paced-sender catch-up window (seconds). 0 = telephony behavior: any
    # lateness re-anchors the schedule, so cadence never bursts (a PBX plays
    # frames as they arrive — catch-up bursts fast-forward audibly). The
    # duplex session sets ~1s: the browser buffers a jitter lead, so sending
    # late frames IMMEDIATELY refills that buffer instead of letting sleep
    # jitter erode it (see _paced_playback).
    _pace_catch_up_s = 0.0
    _pace_next_t = 0.0        # absolute-schedule anchor, carried across chunks
    _pace_stalls = 0          # re-anchors beyond the catch-up window (per segment)
    _pace_max_behind_s = 0.0  # worst lateness seen (per segment)
    # Stall attribution (per segment): was the frame late because conn.drain()
    # blocked (transport backpressure) or because the event loop lost the time
    # elsewhere (VAD inference, other tasks)? Logged when stalls > 0 — turns
    # the next live call into a definitive answer instead of a guess (P2.1).
    _pace_stall_drain = 0
    _pace_stall_loop = 0
    # Voice-sender nesting depth: >0 while a TTS segment or filler clip is
    # actively sending. The ambience bed must NOT inject frames during a
    # sender's stall — that decorates every >30 ms hiccup with an overlapping
    # bed frame, an audible tick (audit F12: segment-scoped, fillers
    # included). Inter-turn silence (depth 0) keeps the bed as before.
    _voice_senders = 0

    def _select_tts(self, lang: str) -> TTSProvider:
        """(Re)select the call TTS voice for ``lang``.

        The call has a single TTS (built for the route); this just sets the
        per-language voice on it (English fallback when the language has no
        configured voice — see TTSProvider.select_voice).
        """
        self.tts.select_voice(base_lang(lang))
        return self.tts

    def _out_frame(self, frame: bytes) -> bytes:
        """Prepare one outgoing voice frame: texture the voice (grain + early
        reflections, when the route has a bed), mix the ambience bed under
        it, and stamp the send time for the ambience idle sender."""
        self.state._last_voice_sent = time.monotonic()
        if self._texture is not None:
            frame = self._texture.process(frame)
        if self._ambience is not None:
            return self._ambience.mix_into(frame)
        return frame

    async def _ambience_loop(self) -> None:
        """Continuous ambience bed: fill any 20ms window no VOICE was sent.

        Runs for the whole call. Voice senders (_paced_playback /
        _play_filler_audio) stamp _last_voice_sent on every frame and mix the
        bed themselves, so this loop only emits when the line would otherwise
        go silent — at most one overlapping frame at a hand-off boundary,
        which the PBX jitter buffer absorbs. The guard compares against
        VOICE sends only — the bed's own sends must not suppress its next
        frame (that bug halved the bed's frame rate: every send tripped the
        next iteration's check, making it choppy and inaudible). The idle
        threshold is 1.5 frame periods so a voice sender lagging a few ms
        doesn't trigger the bed, while real silence picks up within ~30ms.
        Absolute schedule so sleep jitter doesn't open gaps in the bed.
        """
        frame_s = self._frame_bytes_out / self._byte_rate_out
        idle_threshold = frame_s * 1.5
        next_t = time.monotonic()
        try:
            while self.state._running and not self.conn.is_closed:
                if (self._voice_senders == 0
                        and time.monotonic() - self.state._last_voice_sent
                        >= idle_threshold):
                    try:
                        self.conn.send_audio(self._ambience.next_frame())
                        await self.conn.drain()
                    except Exception:
                        return
                next_t += frame_s
                await asyncio.sleep(max(0.0, next_t - time.monotonic()))
        except asyncio.CancelledError:
            return

    async def _paced_playback(self, pcm_data: bytes) -> None:
        """Send PCM audio to AudioSocket at real-time rate.

        Pacing rides an ABSOLUTE schedule (R5): a per-frame relative timer
        oversleeps by the event loop's scheduling jitter, and that error
        ACCUMULATES — each 20 ms frame runs 20+ε ms, so delivery drifts
        behind real time by ~ε per frame (~50-250 ms per second of audio
        on a busy loop). A PBX jitter buffer re-times and hides it; the
        duplex browser player buffers a fixed lead and every erosion past
        it clicks — the live [pcm] signature of repeated 10-99 ms
        underruns in continuous audio. Anchoring frame times to a schedule
        makes sleep jitter self-correct: lateness up to
        ``_pace_catch_up_s`` sends the next frames immediately (the
        receiver's buffer absorbs the burst and refills), anything worse
        counts as a stall and re-anchors — never a fast-forward blast
        after a real lag spike. Telephony keeps ``_pace_catch_up_s = 0``:
        every lateness re-anchors, i.e. the old never-catch-up cadence.
        The anchor carries across synthesis chunks (``_pace_next_t``), so
        chunk boundaries don't leak schedule either.

        Partial frames are carried over in _audio_out_buf and prepended
        to the next call, ensuring seamless audio across TTS chunks.
        The ambience bed (when enabled) is mixed under every frame.
        """
        if not pcm_data:
            return

        # Prepend any leftover bytes from the previous chunk
        if self.state._audio_out_buf:
            pcm_data = bytes(self.state._audio_out_buf) + pcm_data
            self.state._audio_out_buf.clear()

        # One 20 ms output frame (e.g. 320 bytes at 8 kHz, 960 at 24 kHz)
        frame_duration = self._frame_bytes_out / self._byte_rate_out
        offset = 0

        # Continue the segment's schedule, or re-anchor after a fresh start
        # / upstream starvation (synthesis gap longer than the catch-up
        # window — not a pacing stall, nothing to correct for).
        now = time.monotonic()
        next_t = self._pace_next_t
        if next_t <= 0 or now - next_t > self._pace_catch_up_s:
            next_t = now

        try:
            while offset + self._frame_bytes_out <= len(pcm_data):
                if self.conn.is_closed:
                    return

                if self.state._playback_paused:
                    # Barge-in pause: park until resume or commit. The TTS
                    # provider keeps synthesizing upstream (its receive queue
                    # buffers); a commit (_cancel_tts) clears _tts_playing
                    # BEFORE setting the resume event, so the wake below
                    # exits without a fade — nothing is audible mid-pause.
                    await self.state._playback_resume.wait()
                    if not self.state._tts_playing:
                        return
                    next_t = time.monotonic()  # re-anchor after the gap
                    continue

                if not self.state._tts_playing:
                    # Graceful fade-out over ~100ms (5 frames at 20ms each)
                    remaining = (len(pcm_data) - offset) // self._frame_bytes_out
                    fade_frames = min(5, remaining)
                    for i in range(fade_frames):
                        fade_start = time.monotonic()
                        chunk = pcm_data[offset : offset + self._frame_bytes_out]
                        # Linear gain: 1.0 → 0.0 (voice only — the bed persists)
                        gain = 1.0 - (i + 1) / fade_frames
                        samples = np.frombuffer(chunk, dtype=np.int16).copy()
                        samples = (samples * gain).astype(np.int16)
                        self.conn.send_audio(self._out_frame(samples.tobytes()))
                        await self.conn.drain()
                        offset += self._frame_bytes_out
                        elapsed = time.monotonic() - fade_start
                        sleep_time = frame_duration - elapsed
                        if sleep_time > 0:
                            await asyncio.sleep(sleep_time)
                    return

                chunk = pcm_data[offset : offset + self._frame_bytes_out]
                self.conn.send_audio(self._out_frame(chunk))
                drain_t0 = time.monotonic()
                await self.conn.drain()
                drain_s = time.monotonic() - drain_t0

                offset += self._frame_bytes_out

                next_t += frame_duration
                behind = time.monotonic() - next_t
                if behind > self._pace_max_behind_s:
                    self._pace_max_behind_s = behind
                if behind > self._pace_catch_up_s:
                    # Beyond the catch-up window — re-anchor, never burst.
                    # With the telephony 0-window this fires on every ε of
                    # sleep jitter (that IS the old cadence) — only count a
                    # stall when the lateness was a real stretch of lost
                    # time, so the summary stat stays meaningful.
                    if behind > 0.05:
                        self._pace_stalls += 1
                        # Attribution: >30 ms inside drain() = transport
                        # backpressure; else the loop lost the time elsewhere.
                        if drain_s > 0.03:
                            self._pace_stall_drain += 1
                        else:
                            self._pace_stall_loop += 1
                    next_t = time.monotonic()
                elif behind < 0:
                    await asyncio.sleep(-behind)
                # else: late but within catch-up — next frame goes now.
        finally:
            self._pace_next_t = next_t

        # Save any partial remainder for the next chunk
        if offset < len(pcm_data):
            self.state._audio_out_buf.extend(pcm_data[offset:])

    async def _play_filler_audio(self, pcm_data: bytes) -> None:
        """Play a short pre-synthesized filler clip at real-time rate.

        Unlike _paced_playback, this does NOT set _tts_playing (no barge-in),
        does NOT use _audio_out_buf (independent from streaming TTS), and has
        no fade-out logic. Just sends frames and returns.
        """
        if not pcm_data:
            return

        frame_duration = self._frame_bytes_out / self._byte_rate_out
        offset = 0

        self._voice_senders += 1
        try:
            while offset + self._frame_bytes_out <= len(pcm_data):
                if self.conn.is_closed:
                    return

                frame_start = time.monotonic()
                chunk = pcm_data[offset : offset + self._frame_bytes_out]
                self.conn.send_audio(self._out_frame(chunk))
                await self.conn.drain()
                offset += self._frame_bytes_out

                elapsed = time.monotonic() - frame_start
                sleep_time = frame_duration - elapsed
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
        finally:
            self._voice_senders -= 1

    async def _play_backchannel(self) -> None:
        """Play a backchannel sound after a brief silence gap.

        Started as a task after each speech segment (2nd+). Cancelled if
        the user resumes speaking or the turn is dispatched.
        """
        try:
            await asyncio.sleep(self.cfg.backchannel_min_gap_s)

            # Abort if user resumed speaking or turn already dispatched
            if not self.state._user_silent.is_set() or not self.state._turn_segments:
                return

            clip = filler_cache.get_backchannel(self._filler_key)
            if not clip:
                return

            logger.info(
                f"[{self.conn.peer_addr}] Backchannel: playing '{clip.text}' "
                f"({clip.duration_s:.2f}s)"
            )
            await self._play_filler_audio(clip.pcm_audio)
        except asyncio.CancelledError:
            return

    async def _play_thinking_filler(self) -> None:
        """Play a thinking filler sound after a delay to mask LLM processing.

        Latency-gated: if the LLM responds within the delay, the filler is
        cancelled in its sleep phase and nothing plays — actual slowness is
        the trigger, not turn position. A filler that played in the previous
        turn raises the bar (repeat delay) so a slow multi-turn stretch
        doesn't chirp on every single turn. Always sets _thinking_filler_done
        in the finally block so _stream_tts_audio can proceed.
        """
        try:
            self.state._thinking_filler_done.clear()
            delay = self.cfg.thinking_filler_delay_s
            if self.state._filler_played_last_turn:
                delay = max(delay, self.cfg.thinking_filler_repeat_delay_s)
            await asyncio.sleep(delay)

            clip = filler_cache.get_thinking(self._filler_key)
            if not clip:
                return

            logger.info(
                f"[{self.conn.peer_addr}] Thinking filler: playing '{clip.text}' "
                f"({clip.duration_s:.2f}s)"
            )
            self.state._filler_playing = True
            self.state._filler_played_this_turn = True
            await self._play_filler_audio(clip.pcm_audio)
        except asyncio.CancelledError:
            return
        finally:
            self.state._filler_playing = False
            self.state._thinking_filler_done.set()

    def _on_tts_playback_start(self) -> None:
        """First audio chunk of a TTS segment is about to play. Hook for
        transports that surface pipeline state to a UI (the duplex session
        sends its 'speaking' frame here — at REAL audio start, not at
        segment-task creation, so 'thinking' covers dispatch→first-audio).
        Base: no-op."""

    def _on_tts_playback_end(self) -> None:
        """A TTS segment finished playing (drained, cancelled, or errored)
        after audio actually started. From the user's perspective the mic
        is live again — including between segments of one turn (pre-tool
        speech → silent tool run). Hook for UI state ('listening').
        Base: no-op."""

    async def _stream_tts_audio(self) -> None:
        """Receive audio from TTS and play at real-time rate.

        A config-gated pre-buffer (``tts_prebuffer_ms``, default 120) delays
        the FIRST frame until that much audio is banked; the absolute pacing
        schedule then carries the offset forward, so every later chunk gets
        the same arrival slack — a jitter lead for the whole segment at a
        bounded one-off first-audio cost. Big-chunk providers (ElevenLabs
        ships seconds per chunk) satisfy it instantly — it exists for
        small-chunk streams and struggling environments.
        """
        total_bytes = 0
        chunk_count = 0
        segment_marked = False  # _voice_senders held from first chunk → finally
        seg_start = time.monotonic()   # per-segment stamp (turn stamps only
        #   reset at dispatch — post-tool/pre-tool segments log against this)
        t_gate_done = 0.0              # start gate passed → prebuffer wait ref
        prebuffer_wait_ms = -1.0
        # Fresh pacing schedule + stats per segment (summarized on done).
        self._pace_next_t = 0.0
        self._pace_stalls = 0
        self._pace_max_behind_s = 0.0
        self._pace_stall_drain = 0
        self._pace_stall_loop = 0
        prebuffer_bytes = int(
            self._byte_rate_out * self.cfg.tts_prebuffer_ms / 1000.0)
        prebuffering = prebuffer_bytes > 0
        pending: list[bytes] = []
        pending_bytes = 0
        try:
            async for audio_chunk in self.tts.receive_audio():
                if not self.state._tts_playing and chunk_count > 0:
                    # Cancelled after playback started
                    break

                # Enable barge-in only once first audio chunk arrives
                if chunk_count == 0:
                    # Turn-first discriminator BEFORE stamping (the turn
                    # stamps reset only at dispatch — later segments of the
                    # same turn must not re-log the turn breakdown).
                    turn_first = not self.state._t_audio_first
                    if turn_first:
                        self.state._t_audio_first = time.monotonic()
                    hold_s = filler_wait_s = breath_s = 0.0
                    # Wait for user to finish speaking before starting playback.
                    # TTS audio buffers in the async generator while we wait.
                    hold_capped = False
                    if not self.state._user_silent.is_set():
                        logger.info(
                            f"[{self.conn.peer_addr}] TTS ready but user speaking — waiting"
                        )
                        # Loop with settle period to catch mid-sentence pauses.
                        # VAD fires SPEECH_END after 350ms silence, but the user
                        # might just be pausing between sentences.
                        #
                        # Phantom-hold cap (live-hit 2026-08-14: line noise kept
                        # VAD in "speech" for 39 s with zero transcripts and the
                        # ready reply never played — the caller gave up): past
                        # ``tts_hold_max_s`` with NO transcript evidence (no
                        # interim, no queued segment) the "speech" is phantom as
                        # far as STT is concerned — start playback; a real talker
                        # still barges in. Evidence disables the cap entirely: a
                        # transcribing speaker deserves the full wait.
                        hold_start = time.monotonic()
                        while True:
                            cap_s = self.cfg.tts_hold_max_s
                            remaining = cap_s - (time.monotonic() - hold_start)
                            evidence = self._speech_evidence()
                            if remaining <= 0 and not evidence:
                                hold_capped = True
                                logger.warning(
                                    f"[{self.conn.peer_addr}] TTS hold cap "
                                    f"({cap_s:.1f}s) — no transcript evidence, "
                                    f"starting playback"
                                )
                                break
                            try:
                                if evidence:
                                    await self.state._user_silent.wait()
                                else:
                                    await asyncio.wait_for(
                                        self.state._user_silent.wait(),
                                        timeout=max(0.1, remaining),
                                    )
                            except asyncio.TimeoutError:
                                continue  # cap window elapsed — re-evaluate
                            except asyncio.CancelledError:
                                return
                            try:
                                await asyncio.sleep(0.5)
                            except asyncio.CancelledError:
                                return
                            if self.state._user_silent.is_set():
                                break
                            logger.info(
                                f"[{self.conn.peer_addr}] User resumed speaking — waiting again"
                            )
                        hold_s = time.monotonic() - hold_start
                        if not hold_capped:
                            # Reset cooldown so trailing energy doesn't trigger
                            # immediate false barge-in after we start playing
                            self.state._speech_end_time = time.monotonic()
                            logger.info(
                                f"[{self.conn.peer_addr}] User silent — starting TTS playback"
                            )

                    # Thinking filler: if still in sleep phase, cancel it
                    # (LLM responded faster than the delay — no filler needed).
                    # If already playing, wait for it to finish naturally.
                    t_filler = time.monotonic()
                    if not self.state._thinking_filler_done.is_set():
                        if (self.state._thinking_filler_task
                                and not self.state._thinking_filler_task.done()
                                and not self.state._filler_playing):
                            self.state._thinking_filler_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await self.state._thinking_filler_task
                            self.state._thinking_filler_task = None
                            logger.info(
                                f"[{self.conn.peer_addr}] Thinking filler cancelled "
                                f"(LLM responded fast)"
                            )
                        else:
                            # Filler is playing — wait for it to finish
                            try:
                                await asyncio.wait_for(
                                    self.state._thinking_filler_done.wait(), timeout=5.0
                                )
                            except asyncio.TimeoutError:
                                logger.warning(
                                    f"[{self.conn.peer_addr}] Thinking filler wait timed out"
                                )
                    filler_wait_s = time.monotonic() - t_filler

                    # Pre-response breath: a subtle inhale right before the
                    # voice starts, only after a real silence gap (a breath
                    # mid rapid exchange reads as uncanny). Uses the same
                    # voice-audio path as fillers, so it stamps
                    # _last_voice_sent and mixes over the ambience bed.
                    if (self._breath_pcm is not None
                            and time.monotonic() - self.state._last_voice_sent
                            >= self.cfg.breath_min_gap_s):
                        logger.info(
                            f"[{self.conn.peer_addr}] Breath: playing inhale"
                        )
                        t_breath = time.monotonic()
                        await self._play_filler_audio(self._breath_pcm)
                        breath_s = time.monotonic() - t_breath

                    self.state._tts_playing = True
                    self.state._tts_ever_played = True
                    # If user is already speaking when TTS starts, keep STT
                    # unmuted so the provider captures their speech.  Without
                    # this, STT gets muted the moment _tts_playing becomes True
                    # and short utterances like "ee yes" are lost entirely.
                    if self.vad.state == VadState.SPEAKING and self.stt.supports_early_unmute:
                        self.state._stt_early_unmuted = True
                        logger.info(
                            f"[{self.conn.peer_addr}] User already speaking when "
                            f"TTS starts — keeping STT unmuted"
                        )
                    self.vad.set_bargein_mode(True)
                    # Cap-triggered start with VAD still in SPEAKING: the
                    # "speech" has produced zero transcript evidence, so it is
                    # phantom as far as STT is concerned — no pause, no abort
                    # (this replaced the F10 abort timer that killed turns on
                    # VAD-only evidence). A real talker's finals still commit
                    # through the transcript paths.
                    self._on_tts_playback_start()
                    logger.debug(f"[{self.conn.peer_addr}] First TTS audio arrived, barge-in enabled")
                    # Segment active: the ambience bed must not decorate this
                    # sender's stalls with overlapping frames (audit F12).
                    self._voice_senders += 1
                    segment_marked = True
                    # First-audio latency breakdown (P4): which span is slow —
                    # the engine (dispatch→text), synthesis (text→audio), or
                    # the daemon's own start gate (audio→frame: hold + filler
                    # + breath)? One line per TURN — the turn stamps reset
                    # only at dispatch, so later segments (post-tool text)
                    # would log the tool run folded into audio→frame; they
                    # get the segment-relative breadcrumb instead.
                    now = time.monotonic()
                    if self.state._t_dispatch and turn_first:
                        t_text = self.state._t_text_first or now
                        t_audio = self.state._t_audio_first or now
                        logger.info(
                            f"[{self.conn.peer_addr}] First-audio latency: "
                            f"dispatch→text {(t_text - self.state._t_dispatch) * 1000:.0f}ms, "
                            f"text→audio {(t_audio - t_text) * 1000:.0f}ms, "
                            f"audio→frame {(now - t_audio) * 1000:.0f}ms "
                            f"(gate: hold {hold_s * 1000:.0f}, "
                            f"filler-wait {filler_wait_s * 1000:.0f}, "
                            f"breath {breath_s * 1000:.0f})"
                        )
                        if self.state._t_turn_speech_end:
                            logger.info(
                                f"[{self.conn.peer_addr}] Turn first-frame: "
                                f"speech_end→dispatch "
                                f"{(self.state._t_dispatch - self.state._t_turn_speech_end) * 1000:.0f}ms, "
                                f"dispatch→frame "
                                f"{(now - self.state._t_dispatch) * 1000:.0f}ms, "
                                f"total "
                                f"{(now - self.state._t_turn_speech_end) * 1000:.0f}ms"
                            )
                    elif self.state._t_dispatch:
                        logger.info(
                            f"[{self.conn.peer_addr}] Segment first frame: "
                            f"+{(now - seg_start) * 1000:.0f}ms after segment "
                            f"start (gate: hold {hold_s * 1000:.0f}, "
                            f"filler-wait {filler_wait_s * 1000:.0f})"
                        )
                    t_gate_done = time.monotonic()

                total_bytes += len(audio_chunk)
                chunk_count += 1
                if prebuffering:
                    pending.append(audio_chunk)
                    pending_bytes += len(audio_chunk)
                    if pending_bytes >= prebuffer_bytes:
                        prebuffering = False
                        if t_gate_done:
                            prebuffer_wait_ms = (
                                time.monotonic() - t_gate_done) * 1000
                        for c in pending:
                            if not self.state._tts_playing:
                                break
                            await self._paced_playback(c)
                        pending, pending_bytes = [], 0
                    continue
                await self._paced_playback(audio_chunk)
            # Stream ended while still pre-buffering (utterance shorter than
            # the floor) — flush what was banked, unless cancelled (the fade
            # path owns a cancelled exit).
            for c in pending:
                if not self.state._tts_playing and chunk_count > 0:
                    break
                await self._paced_playback(c)
        except asyncio.CancelledError:
            return
        except Exception as e:
            if self.state._tts_playing:
                logger.error(f"[{self.conn.peer_addr}] TTS stream error: {e}")
        finally:
            if segment_marked:
                self._voice_senders -= 1
            duration_s = total_bytes / self._byte_rate_out if total_bytes else 0
            logger.info(
                f"[{self.conn.peer_addr}] TTS playback done: "
                f"{chunk_count} chunks, {total_bytes} bytes, {duration_s:.1f}s audio"
                + (f", pacing max-behind {self._pace_max_behind_s * 1000:.0f}ms, "
                   f"{self._pace_stalls} stalls" if chunk_count else "")
                + (f" (drain {self._pace_stall_drain} / loop "
                   f"{self._pace_stall_loop})" if self._pace_stalls else "")
                + (f", prebuffer {prebuffer_wait_ms:.0f}ms"
                   if prebuffer_wait_ms >= 0 else "")
            )
            if chunk_count > 0:  # zero-chunk segments never left 'thinking'
                self._on_tts_playback_end()
            # Post-playback STT probe: playback mute is the longest send-
            # starvation window — probe (and reconnect if dead) right away
            # instead of waiting for the guard tick. ensure_alive returns
            # immediately when healthy and never clears the queue here
            # (audit F6: after barge-in it holds the interrupter's finals).
            if (chunk_count > 0 and self.state._running
                    and self.state._stt_active and not self.conn.is_closed):
                asyncio.create_task(self._stt_reconnect())

    # -- barge-in pause/confirm/commit ---------------------------------------
    #
    # VAD evidence alone never kills a turn (live-hit 2026-08-20/21: a stuck
    # "speech" state with zero transcripts aborted mid-tool generations and
    # left dead air). Speech-start PAUSES the sender — reversible, the engine
    # turn and TTS synthesis keep running; only a non-empty FINAL transcript
    # commits (cancels TTS + aborts the turn, via the transcript consumers'
    # choke points in turn.py). An episode that never produces words resumes
    # playback where it froze.

    def _speech_evidence(self) -> bool:
        """ANY sign the current "speech" is producing text: a live partial, a
        finalized-but-undrained provider queue entry (mid-speech is_finals
        CLEAR the partial — the queue is where a long utterance's proof
        lives; live-hit 2026-08-24: missing it resumed the reply over a
        mid-sentence caller), a queued dispatch, or accumulated segments."""
        return (
            bool(self.state._queued_speech)
            or bool(self.state._turn_segments)
            or bool(self.stt.latest_interim)
            or self.stt.has_pending_finals
        )

    def _pause_playback(self) -> None:
        """VAD speech-start during playback: suspend the sender in place.

        Reversible and immediate: the paced loop parks before its next
        frame, the peer suspends its lead buffer (duplex pause frame;
        telephony has ~1 frame in flight), STT routing flips to the live
        feed (core listen loop reads the flag) so the confirming final can
        form. The engine turn is untouched.
        """
        if not self.state._tts_playing or self.state._playback_paused:
            return
        self.state._playback_paused = True
        self.state._playback_resume.clear()
        self.state._pause_speech_start = time.monotonic()
        self.state._pause_long_episode = False
        self.conn.pause_playback()
        self._cancel_pause_timers()
        self.state._pause_confirm_timer = asyncio.create_task(
            self._pause_confirm_expire()
        )
        logger.info(
            f"[{self.conn.peer_addr}] Barge-in: playback paused (speech "
            f"start) — awaiting transcript"
        )

    def _handle_pause_speech_end(self) -> bool:
        """SPEECH_END while paused. Returns True when the episode was SHORT
        (< ``bargein_timer_s``) and was fully handled here: the same
        duration filter the pre-pause design applied — a short ack/noise
        over playback ("ok", a cough) is ignored ENTIRELY (queue cleared,
        never dispatched, never a commit) and playback resumes where it
        froze. False = the episode is long enough to be a real barge-in
        candidate: the resume-grace is armed and the caller falls through
        to normal transcript processing, where a non-empty final commits.
        """
        episode_s = time.monotonic() - self.state._pause_speech_start
        if episode_s < self.cfg.bargein_timer_s:
            if (self.state._stt_active and self.stt.has_pending_finals
                    and self.state._pause_long_episode):
                # The short blip is a trailing-energy RESTART right after a
                # real (long) episode whose final just landed — the restart
                # re-stamped the episode clock, but those words are the
                # commit evidence; discarding them here would resume the
                # reply over a confirmed sentence (audit B2, 2026-08-24).
                # Fall through to normal processing instead. A pause whose
                # ONLY episode was short keeps the ack rule: "ok"/"ναι"
                # over playback is ignored even when its final beat the
                # gate.
                logger.info(
                    f"[{self.conn.peer_addr}] Barge-in: short episode "
                    f"({episode_s:.2f}s) but a long episode's final is "
                    f"pending — processing instead of ignoring"
                )
            else:
                logger.info(
                    f"[{self.conn.peer_addr}] Barge-in: short episode "
                    f"({episode_s:.2f}s < {self.cfg.bargein_timer_s}s) — "
                    f"ignoring speech, resuming playback"
                )
                if self.state._stt_active:
                    self.stt.clear_queue()
                self.state._stt_early_unmuted = False
                self._resume_playback("short episode")
                return True
        if (self.state._pause_grace_task
                and not self.state._pause_grace_task.done()):
            self.state._pause_grace_task.cancel()
        self.state._pause_grace_task = asyncio.create_task(
            self._pause_resume_after_grace()
        )
        return False

    def _resume_playback(self, reason: str) -> None:
        """No confirming transcript — resume the paused playback in place."""
        if not self.state._playback_paused:
            return
        self.state._playback_paused = False
        self._cancel_pause_timers()
        self.conn.resume_playback()
        self.state._playback_resume.set()
        logger.info(
            f"[{self.conn.peer_addr}] Barge-in: playback resumed ({reason})"
        )

    def _cancel_pause_timers(self) -> None:
        if (self.state._pause_confirm_timer
                and not self.state._pause_confirm_timer.done()):
            self.state._pause_confirm_timer.cancel()
        self.state._pause_confirm_timer = None
        if (self.state._pause_grace_task
                and not self.state._pause_grace_task.done()):
            self.state._pause_grace_task.cancel()
        self.state._pause_grace_task = None

    async def _pause_confirm_expire(self) -> None:
        """Evidence-aware monitor for a pause that never resolves.

        Replaces the single-shot wall-clock backstop (live-hit 2026-08-24:
        it resumed the reply over a mid-sentence caller at expiry — a long
        utterance whose first phrase Deepgram had already finalized looked
        exactly like phantom VAD, because the old evidence tuple missed the
        provider QUEUE). Policy, sampled every 0.25 s:

        - NO speech evidence for ``bargein_confirm_timeout_s`` → phantom:
          heal ``_user_silent`` when VAD is wedged in SPEAKING (the
          observed 16 s stuck state) and resume where playback froze.
        - Evidence present and still EVOLVING (interim text growing, new
          finals landing) → a real speaker: never resume on wall clock.
          SPEECH_END resolution belongs to the resume-grace and the commit
          choke points.
        - Evidence static for ``2 × bargein_confirm_timeout_s`` (VAD
          wedged mid-"speech", or idle with the delayed retries spent):
          a pending FINAL runs the synthetic speech-end — the user said
          real words, so the commit wins over a resume; interim-only or
          consumer-less static evidence resumes with a warning (the
          bounded escape the old backstop existed for).

        Commit paths cancel this task via ``_cancel_pause_timers``; every
        resume nulls the timer ref first so the cancel can't hit the
        running monitor mid-resume.
        """
        pause_start = time.monotonic()
        snapshot: tuple | None = None
        last_delta = pause_start
        try:
            while True:
                await asyncio.sleep(0.25)
                if not self.state._playback_paused:
                    return
                now = time.monotonic()
                cur = (
                    self.stt.latest_interim,
                    self.stt.has_pending_finals,
                    len(self.state._queued_speech),
                    len(self.state._turn_segments),
                )
                evidence = bool(cur[0]) or cur[1] or cur[2] > 0 or cur[3] > 0
                if cur != snapshot:
                    snapshot = cur
                    last_delta = now
                    continue
                if not evidence:
                    if now - pause_start < self.cfg.bargein_confirm_timeout_s:
                        continue
                    if self.vad.state == VadState.SPEAKING:
                        # Phantom-speech wedge: zero transcript evidence —
                        # the "speech" is noise as far as STT is concerned.
                        # Heal the silence flag so later segment holds don't
                        # each pay the hold cap (the 4 s first-audio tax).
                        self.state._user_silent.set()
                        logger.warning(
                            f"[{self.conn.peer_addr}] Barge-in: confirm "
                            f"timeout ({self.cfg.bargein_confirm_timeout_s:.1f}s) "
                            f"with VAD stuck in speech and no transcript — "
                            f"treating as phantom"
                        )
                    self.state._pause_confirm_timer = None
                    self._resume_playback("confirm-timeout, no transcript")
                    return
                if now - last_delta < 2 * self.cfg.bargein_confirm_timeout_s:
                    continue
                if (self.stt.has_pending_finals
                        and now - self.state._pause_speech_start
                        >= 2 * self.cfg.bargein_confirm_timeout_s):
                    # Stuck episode with real words waiting (VAD never fired
                    # its end, or the retries died): run the SPEECH_END
                    # processing synthetically — commit beats resume. The
                    # recent-episode check keeps a fresh restart owning its
                    # own endpointing.
                    logger.warning(
                        f"[{self.conn.peer_addr}] Barge-in: stuck pause with "
                        f"a pending final — synthetic speech end"
                    )
                    self.state._pause_confirm_timer = None
                    await self._process_speech_end(synthetic=True)
                    return
                if not self.stt.has_pending_finals:
                    # Static interim/segments with nothing consuming them —
                    # bounded escape so dead air can't run unbounded.
                    self.state._user_silent.set()
                    self.state._pause_confirm_timer = None
                    self._resume_playback("stale evidence, no commit")
                    return
        except asyncio.CancelledError:
            return

    async def _pause_resume_after_grace(self) -> None:
        """Armed at SPEECH_END while paused: the speech episode ended — give
        the STT final ``bargein_resume_grace_s`` to arrive (the delayed
        retry paths deliver it and their consumers commit); resume when
        nothing materializes. A new speech episode re-arms via its own
        SPEECH_END; commit paths cancel this task via _cancel_tts."""
        try:
            await asyncio.sleep(self.cfg.bargein_resume_grace_s)
        except asyncio.CancelledError:
            return
        if not self.state._playback_paused:
            return
        if not self.state._user_silent.is_set():
            return  # new episode owns the pause now
        if self._speech_evidence():
            return  # commit imminent (or the confirm backstop resolves it)
        self.state._pause_grace_task = None
        self._resume_playback("speech ended, no transcript")

    async def _cancel_tts(self) -> None:
        """Cancel current TTS playback and parallel LLM (barge-in commit)."""
        logger.info(f"[{self.conn.peer_addr}] Barge-in: cancelling TTS")
        was_paused = self.state._playback_paused
        self.state._tts_playing = False
        self.state._utterance_cancelled = True
        # Clear a pause FIRST (flag + timers), then wake the parked sender —
        # it re-checks _tts_playing and exits without a fade (nothing is
        # audible mid-pause, a fade would blip).
        if was_paused:
            self.state._playback_paused = False
            self._cancel_pause_timers()
            self.state._playback_resume.set()
        self.vad.set_bargein_mode(False)
        self.tts.cancel()
        # Cut peer-buffered audio BEFORE the fade frames go out, so the fade
        # plays as the tail instead of after a stale backlog.
        self.conn.flush_playback()

        if not was_paused:
            # Give _paced_playback time to fade out (~100ms / 5 frames)
            # before cancelling the task — otherwise CancelledError
            # interrupts the fade and we get a hard cut anyway.
            await asyncio.sleep(0.10)

        if self.state._tts_task and not self.state._tts_task.done():
            self.state._tts_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.state._tts_task
            self.state._tts_task = None

        # Cancel thinking filler if still playing
        if self.state._thinking_filler_task and not self.state._thinking_filler_task.done():
            self.state._thinking_filler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.state._thinking_filler_task
            self.state._thinking_filler_task = None
            self.state._thinking_filler_done.set()

        # Cancel parallel LLM if running (cleans up LLM messages via handler)
        if self.state._parallel_llm_task and not self.state._parallel_llm_task.done():
            self.state._parallel_llm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.state._parallel_llm_task
            self.state._parallel_llm_task = None
            # Direct layer: cancel it server-side too and remember its
            # transcript — the caller's new speech gets batched with it
            # (the aborted turn's user message is popped by the proxy).
            if getattr(self.llm, "supports_abort", False):
                try:
                    await self.llm.abort_turn()
                except Exception as e:
                    logger.warning(
                        f"[{self.conn.peer_addr}] Parallel abort failed: {e}"
                    )
                if (self.state._parallel_llm_text
                        and getattr(self.llm, "abort_erases_turn", True)):
                    self.state._aborted_turn_text = self.state._parallel_llm_text
                self.state._parallel_llm_text = ""

        # Interrupt-style upstream abort (duplex chat attach + WS-proxy
        # CLI/Codex routes): the token loop may be parked on an empty frame
        # queue (silent tool run) and never notice _utterance_cancelled — the
        # abort unwinds the turn server-side so 'done' arrives and everything
        # drains. Erase-style clients (Direct) keep the llm-loop abort path,
        # which owns the transcript refold; a stale abort is a server no-op.
        # Gated on turn liveness: a commit that lands AFTER the engine turn
        # finished (audio was still playing/paused) must not fire a
        # session-scoped abort that could kill an unrelated queued turn.
        if (getattr(self.llm, "supports_abort", False)
                and not getattr(self.llm, "abort_erases_turn", True)
                and getattr(self.llm, "turn_in_flight", True)):
            with contextlib.suppress(Exception):
                await self.llm.abort_turn()

    async def _greeting_tts_cleanup(self) -> None:
        """Reset TTS state after greeting finishes naturally.

        Normally _process_utterance() handles TTS cleanup, but the greeting
        isn't an utterance — it's a standalone TTS playback. If barge-in
        happened, _cancel_tts() already handled everything.
        """
        try:
            if self.state._tts_task:
                await self.state._tts_task
        except asyncio.CancelledError:
            return  # Barge-in cancelled it — _cancel_tts() already cleaned up
        # Greeting finished naturally — reset TTS state
        self.state._tts_playing = False
        self.vad.set_bargein_mode(False)
        self.state._tts_task = None
        self.stt.on_tts_finished(was_interrupted=self.state._utterance_cancelled)
        self.state._stt_early_unmuted = False
        self.state._utterance_cancelled = False
        self.state._last_activity = time.monotonic()

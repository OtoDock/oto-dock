"""DuplexSession — the conversation pipeline on a dashboard duplex socket.

Reuses ``CallPipeline``'s machinery (listen loop, VAD dispatch, turn
accumulation + classification, TTS pacing, barge-in, thinking fillers)
over a ``DuplexEngineConnection`` instead of AudioSocket, with the
telephony-only pieces absent by construction: no AMI/outbound, no
greeting, no ambience/texture/breath (8 kHz assets), no ``[QUESTION:]``
hold machinery, no backchannels (v1). The LLM backend is the
``DuplexLlmClient`` — the proxy's attach layer owns the real chat session.

State/caption frames for the dashboard UI ride the same socket:
``interim`` (the STT provider's live partial — the caption OVERLAY, sampled
by a small pump because providers expose ``latest_interim`` as a polled
property), ``interim_final`` (a finalized piece of the still-accumulating
utterance, pushed by the provider's ``on_partial_final`` callback — the
browser ACCUMULATES these and overlays the partial, the same pair the
dictation composer uses), ``final`` (the dispatched utterance), ``state``
(``thinking`` = dispatch→first audio, ``speaking`` = audio actually
playing, ``listening`` = mic-live reality — including between TTS segments
and during silent tool phases).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import config
from audio.providers.vad.silero import SileroVad
from pipeline.vad_serial import SerializedVad
from audio.providers.turn.dispatcher import build_dispatcher
from audio.streaming.lang import base_lang
from config_manager import ConfigManager, PhoneRoute, RouteSettings
from fillers import filler_cache
from pipeline.core import CallPipeline
from pipeline.providers import build_stt, build_tts, filler_key_for
from pipeline.state import CallState
from transport.base import TransportError

from .llm_client import DuplexLlmClient

logger = logging.getLogger("duplex")


class _DuplexCfg:
    """ConfigManager view with duplex-appropriate session limits.

    A phone call hangs up after 30 s of dead air and 10 minutes total; a
    duplex session is an open mic on a screen the user may sit quietly at —
    silence is normal, and the whole-session budget is enforced PROXY-side
    (the token's ``max_seconds``). Everything else passes through.
    """

    def __init__(self, base: ConfigManager):
        self._base = base
        self.idle_timeout_s = 600.0
        self.call_max_duration_s = 24 * 3600.0
        # A complete-verdict dispatches after this grace instead of instantly
        # (phone runs 0.0 for snappiness): a mid-sentence pause that outlived
        # the VAD endpointing re-opens the turn when speech resumes, instead
        # of shipping a fragment ("Hey. Do you listen to" — live test).
        self.turn_classifier_grace_s = 0.4

    def __getattr__(self, name):
        return getattr(self._base, name)


class DuplexSession(CallPipeline):
    """A duplex conversation bound to one dial-back connection."""

    # Deliberately does NOT call CallPipeline.__init__: that constructor is
    # route-resolution (call defaults, ambience, outbound) — this one wires
    # the same attribute surface from the per-session duplex config instead.
    def __init__(self, conn, cfg: ConfigManager, session_cfg: dict):
        self.conn = conn
        self._rate_in = conn.sample_rate_in
        self._rate_out = conn.sample_rate_out
        self._frame_bytes_out = conn.frame_bytes_out
        self._byte_rate_out = self._rate_out * config.SAMPLE_WIDTH
        self.cfg = _DuplexCfg(cfg)

        lang = base_lang(str(session_cfg.get("language") or "") or "en")
        self._locked_language = lang
        # Synthetic route: the pipeline's route-scoped toggles with duplex
        # semantics (no bed, fillers on, backchannels off via settings below).
        self.route = PhoneRoute(
            id=f"duplex-{conn.call_uuid[:8]}", direction="inbound", agent="",
            llm_mode="proxy", greeting="", language=lang,
            phone_context_override="", enabled=True,
            ami_caller_id="", ami_outbound_context="", dial_prefix="",
        )
        self.agent_model = ""
        self._audiosocket_uuid = conn.call_uuid
        self._caller_info: dict = {}

        stt_provider = session_cfg.get("stt") or None
        tts_provider = session_cfg.get("tts") or None
        self._route_settings = RouteSettings(
            stt_provider=stt_provider,
            tts_provider=tts_provider,
            backchannel_enabled=False,     # listening sounds are telephony UX
            thinking_filler_enabled=True,  # CLI first-token latency cover
        )
        self.stt = build_stt(self.cfg, stt_provider)
        # Same serialized-VAD wrapper as the telephony pipeline (P2.2/F11).
        self.vad = SerializedVad(SileroVad(
            threshold=self.cfg.vad_threshold,
            silence_duration_ms=self.stt.vad_silence_padding_ms,
            speech_pad_ms=self.cfg.vad_speech_pad_ms,
            min_energy_rms=self.cfg.vad_min_energy_rms,
            bargein_threshold=self.cfg.bargein_threshold,
            bargein_debounce_ms=self.cfg.bargein_debounce_ms,
            bargein_chunk_ratio=self.cfg.bargein_chunk_ratio,
            bargein_silence_duration_ms=self.cfg.bargein_silence_duration_ms,
            sample_rate=self._rate_in,
        ))
        self.tts = build_tts(tts_provider)
        self.tts.select_voice(lang)
        self._filler_key = filler_key_for(
            tts_provider, self.tts, lang, self._rate_out)

        self.llm = DuplexLlmClient(conn)
        self._is_direct = False
        self._turn_clf = None
        # Telephony-flavoured extras stay off (8 kHz assets / phone-mic DSP).
        self._ambience = None
        self._texture = None
        self._breath_pcm = None
        self._call_manager = None
        self._outbound_call_id = None
        self._is_outbound = False
        self._speech_audio_max_bytes = int(
            self.cfg.smart_turn_audio_window_s * self._rate_in * config.SAMPLE_WIDTH
        )
        self.state = CallState()
        # Absolute-schedule pacing with catch-up (R5): the browser player
        # buffers a jitter lead, so late frames must burst-refill it — the
        # telephony default (0 = re-anchor on any lateness) let event-loop
        # sleep jitter accumulate into repeated audible micro-gaps.
        self._pace_catch_up_s = 1.0
        self._typed_turn: asyncio.Task | None = None
        # Manual barge-in tap from the browser = a confirmed barge-in.
        conn.on_barge_in = self._on_manual_barge_in
        # Click-to-edit (R3.8): focus holds, Enter/Send dispatches typed.
        conn.on_hold = self._on_hold
        conn.on_typed_utterance = self._on_typed_utterance
        # Muted-window STT survival (2026-08-12 dead-mic incident; timeline
        # continuity added 2026-08-14): a 17s hold starved Deepgram of audio
        # → it closed 1011 and NEVER came back. Each dropped frame now feeds
        # SILENCE at the mic cadence (keeps the streaming model's audio
        # timeline warm — a keepalive alone leaves it stale after long
        # mutes) plus the throttled protocol keepalive; the unmute edge
        # health-checks and reconnects a provider that died anyway, at the
        # ACTIVE sample rate.
        conn.on_muted_audio = self._on_muted_audio
        conn.on_unmute = self._on_unmute
        conn.on_release = self._on_release
        # A [DUPLEX_COMPLETE] farewell hangs up with reason "agent_complete"
        # so the browser can skip the exit note (the agent KNOWS it ended
        # the spoken mode); every other hangup stays "engine".
        conn.hangup_reason = (
            lambda: "agent_complete" if self.state._call_complete else "engine")
        # Mid-utterance provider finals push straight to the composer —
        # they sit in the provider's internal queue until dispatch, so the
        # sampled ``latest_interim`` overlay alone made the caption reset
        # to each newest phrase (R4.6).
        self.stt.on_partial_final = self._on_stt_partial_final

    async def _on_muted_audio(self, frame_len: int) -> None:
        """A mic frame arrived while muted/held and was dropped — keep the
        STT provider's socket AND audio timeline alive through the gap.
        ``feed_during_tts`` sends silence at the real mic cadence (a
        protocol keepalive alone leaves the streaming model stale after a
        long mute — the Call C failure class, audit F16); the throttled
        ``feed_during_opening`` keepalive stays as the belt for providers
        whose silence feed is a no-op."""
        with contextlib.suppress(Exception):
            await self.stt.feed_during_tts(b"\x00" * frame_len)
            self.state._stt_last_fed = time.monotonic()
            await self.stt.feed_during_opening()

    async def _on_unmute(self) -> None:
        """The mic is live again — health-check the provider and reconnect
        if its socket died during the muted window (the keepalive makes
        that rare; this is the backstop that ends the dead-mic failure
        mode for good). ``ensure_alive`` reconnects with the ACTIVE sample
        rate (audit F5: the old recover path reconnected at the 8 kHz
        default and decoded the 16 kHz mic as garbage); clearing stale
        queue state on a healthy connection is correct here (speech while
        muted is discarded by design)."""
        try:
            ok = await self.stt.ensure_alive(self._locked_language, clear_queue=True)
            if not ok:
                logger.warning(
                    f"[{self.conn.peer_addr}] STT reconnect on unmute failed "
                    f"— next utterances may be lost until re-open")
            else:
                self.state._stt_active = True
        except Exception as e:
            logger.warning(
                f"[{self.conn.peer_addr}] STT health check on unmute "
                f"failed: {e}")

    def _turn_busy(self) -> bool:
        # The duplex TYPED-turn lane must count as busy everywhere the VAD
        # lane checks (audit F4, 2026-08-24) — with it invisible, a timer
        # dispatch during a typed turn's reply created a SECOND
        # _process_utterance loop sharing _tts_task/_queued_speech.
        return (super()._turn_busy()
                or bool(self._typed_turn and not self._typed_turn.done()))

    async def _on_release(self, text: str) -> None:
        """Mic unmuted while the composer held the conversation: resume live
        listening and hand the held draft BACK to the engine (operator
        design 2026-08-24: unmute-after-hold means "send it when I finish
        talking"). Heal FIRST — same backstop ``_on_unmute`` runs; its
        clear_queue discards muted-window artifacts, never the seed — THEN
        seed. With a turn in flight the draft queues with CONTINUATION
        semantics (no interrupt — audit F3: an unmute must not behave like
        a barge-in commit); idle, it seeds the pending turn and arms the
        classifier timer, so new speech appends and the normal endpointing
        dispatches the whole thing (or the timer sends it alone after the
        grace if the user stays silent)."""
        await self._on_unmute()
        text = (text or "").strip()
        if not text:
            return
        self.state._last_activity = time.monotonic()
        if self._turn_busy():
            self.state._queued_speech.append(text)
            logger.info(
                f"[{self.conn.peer_addr}] Release: draft queued behind the "
                f"busy turn ({len(text)} chars)")
            return
        self._append_turn_segment(text)
        self._cancel_turn_timer()
        self.state._turn_timer = asyncio.create_task(
            self._turn_timeout_handler(text))
        # Echo the seed back through the ACCUMULATE caption channel so the
        # composer keeps showing the held words while the user talks on —
        # without it the draft vanished at unmute and only reappeared united
        # at dispatch (operator live-test 2026-08-25). Direct send_event on
        # purpose: _on_stt_partial_final's orphan sweep must NOT run for a
        # seed that is already in _turn_segments (it would double-queue it).
        self.conn.send_event({"type": "interim_final", "text": text})
        logger.info(
            f"[{self.conn.peer_addr}] Release: draft seeded as the pending "
            f"turn ({len(text)} chars) — listening resumed")

    async def _on_hold(self) -> None:
        """Click-to-edit: the composer took ownership of the pending
        utterance — drop the daemon-side accumulation so VAD endpointing
        can't auto-dispatch a partial turn behind the user's back while
        they edit. The provider queue is cleared too: its finals are the
        SAME text the composer now holds (they fed the caption), and a
        copy left behind resurfaces as a duplicate dispatch later. The
        mic is already muted (the hold frame gates it connection-side);
        `unmute` resumes normal listening."""
        if self.state._turn_timer and not self.state._turn_timer.done():
            self._cancel_turn_timer()
        self.state._turn_segments.clear()
        self.state._speech_audio_buf.clear()
        self.stt.clear_queue()
        logger.info(f"[{self.conn.peer_addr}] Hold: composer owns the turn")

    async def _on_typed_utterance(self, text: str) -> None:
        """The edited composer text dispatched as a SPOKEN-mode turn — the
        reply comes back as TTS, exactly like an STT dispatch (this is why
        the send rides the duplex socket instead of the typed-chat path).
        Queued like overlapping speech when a turn is already in flight —
        and that queue-append fires the interrupt-style abort, so a send
        DURING the agent's work stops it and dispatches promptly (the
        operator's stop-and-send contract). The Send tap can arrive with
        NO prior hold (tapping the arrow doesn't focus the textarea), so
        this path must take the same ownership hold does: drop the pending
        VAD accumulation and the provider queue — the typed text IS that
        utterance (it fed the composer via the caption), and any copy left
        daemon-side re-dispatches the same words a second time (live-hit
        2026-08-11 09:18: manual send + the queued copy both answered)."""
        text = (text or "").strip()
        if not text:
            return
        self.state._last_activity = time.monotonic()
        self._cancel_turn_timer()
        self.state._turn_segments.clear()
        self.state._speech_audio_buf.clear()
        self.stt.clear_queue()
        busy = (
            self.state._tts_playing
            or (self.state._utterance_task and not self.state._utterance_task.done())
            or (self._typed_turn and not self._typed_turn.done())
        )
        if busy:
            if text in self.state._queued_speech:
                logger.info(
                    f"[{self.conn.peer_addr}] Typed utterance already "
                    f"queued — skipping duplicate")
                return
            self.state._queued_speech.append(text)
            logger.info(
                f"[{self.conn.peer_addr}] Typed utterance queued (turn busy)")
            self._interrupt_for_queued_speech()
            return
        logger.info(
            f"[{self.conn.peer_addr}] Typed utterance dispatching "
            f"({len(text)} chars)")
        self._typed_turn = asyncio.create_task(self._process_utterance(text))

    async def _on_manual_barge_in(self) -> None:
        if self.state._tts_playing:
            await self._cancel_tts()
            self.state._stt_early_unmuted = False
        else:
            # Tap while THINKING: no TTS to cut yet — flag the in-flight
            # utterance so the token loop unwinds and the abort path fires.
            # Harmless while idle-listening: the flag is reset at the next
            # dispatch. The proactive abort below unblocks a loop parked on
            # an empty frame queue (tool running upstream).
            self.state._utterance_cancelled = True
            with contextlib.suppress(Exception):
                await self.llm.abort_turn()

    # NOTE: the upstream abort on barge-in (token loop parked on a silent
    # tool run never sees the cancel flag) moved into the base
    # _cancel_tts, gated interrupt-style (supports_abort and not
    # abort_erases_turn) — it now covers WS-proxy phone routes too.

    async def run(self) -> None:
        """Duplex lifecycle: no greeting, no outbound — straight to listening."""
        self.state._running = True
        logger.info(f"[{self.conn.peer_addr}] Duplex session started "
                    f"(lang={self._locked_language})")
        try:
            # 24 kHz prewarm: duplex streams _rate_out, not the 8 kHz default
            # — without the hint the first turn pays the rebind connect.
            await self.tts.connect(output_sample_rate=self._rate_out)
            self._select_tts(self._locked_language)
            try:
                await filler_cache.ensure(
                    self._filler_key, self.tts,
                    backchannel_phrases=self.cfg.backchannel_phrases,
                    thinking_phrases=self.cfg.thinking_phrases,
                )
            except Exception as e:
                logger.warning(f"[{self.conn.peer_addr}] Filler init failed: {e}")

            self._turn_clf = build_dispatcher(
                smart_turn_enabled=self.cfg.smart_turn_enabled,
                smart_turn_threshold=self.cfg.smart_turn_threshold,
                smart_turn_onnx_threads=self.cfg.smart_turn_onnx_threads,
                smart_turn_audio_window_s=self.cfg.smart_turn_audio_window_s,
                groq_api_key=self.cfg.groq_api_key,
                groq_base_url=self.cfg.groq_base_url,
                lang_map=self.cfg.lang_backend_map,
                default_backend=self.cfg.turn_classifier_default_backend,
            )
            if self.stt.needs_pre_connect:
                try:
                    await self.stt.start(
                        language=self._locked_language,
                        sample_rate=self._rate_in, interim_results=True,
                    )
                    self.state._stt_active = True
                except Exception as e:
                    logger.warning(
                        f"[{self.conn.peer_addr}] STT pre-connect failed: {e}")

            # STT liveness guard (same as the telephony run() — this override
            # must start it itself): keepalive through send starvation +
            # reconnect on mid-call death.
            self.state._stt_last_fed = time.monotonic()
            self.state._stt_guard_task = asyncio.create_task(self._stt_guard())

            # No proxy warmup here: the attach layer resolves the chat session
            # on the first utterance. Unblock the turn machinery immediately.
            self.state._warmup_done.set()
            self.conn.send_event({"type": "state", "state": "listening"})
            interim_task = asyncio.create_task(self._interim_pump())
            try:
                await self._listen_loop()
            finally:
                interim_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await interim_task
        except TransportError as e:
            logger.info(f"[{self.conn.peer_addr}] Duplex stream closed: {e}")
        except Exception as e:
            logger.error(
                f"[{self.conn.peer_addr}] Duplex session error: {e}",
                exc_info=True)
        finally:
            await self._cleanup()

    # -- dashboard state/caption frames ---------------------------------------

    async def _interim_pump(self) -> None:
        """Caption OVERLAY channel: push the STT provider's live partial,
        sampled (providers expose ``latest_interim`` as a polled property,
        not a callback) and sent only on change. Finalized pieces of the
        utterance ride ``interim_final`` frames from the provider's push
        callback instead — the browser accumulates those and overlays this
        partial, exactly like dictation. Suppressed while the mic is held
        (click-to-edit: the composer owns the text; late partials from
        in-flight audio must not pollute the draft)."""
        last = ""
        while True:
            await asyncio.sleep(0.15)
            if self.conn.is_closed:
                return
            if self.conn.is_muted:
                continue
            partial = (getattr(self.stt, "latest_interim", "") or "").strip()
            if partial != last:
                last = partial
                self.conn.send_event({"type": "interim", "text": partial})

    def _on_stt_partial_final(self, text: str) -> None:
        """A final landed in the provider's queue mid-utterance — forward
        it as an accumulate chunk. Skipped while held (composer owns the
        draft) and during the STT echo window (TTS playing without early
        unmute: the provider is being fed silence, so any final is echo
        or stale — the pipeline discards it, the composer must too)."""
        text = (text or "").strip()
        if not text or self.conn.is_closed or self.conn.is_muted:
            return
        if (self.state._tts_playing and not self.state._stt_early_unmuted
                and not self.state._playback_paused):
            # Echo window (TTS audibly playing, provider fed silence) — but a
            # PAUSED playback feeds the live mic and its finals are the
            # barge-in commit evidence, never echo.
            return
        self.conn.send_event({"type": "interim_final", "text": text})
        # Orphan sweep: a final can land with NO VAD speech cycle to drain
        # it — soft speech under Silero's threshold that Deepgram still
        # transcribed (live-hit 2026-08-11 09:17: the sentence tail showed
        # in the composer but sat in the provider queue until discarded as
        # echo), or a final arriving just after the turn dispatched. The
        # sweep gives every VAD-driven consumer time to take it; whatever
        # is still queued afterwards would otherwise never dispatch.
        asyncio.create_task(self._sweep_orphan_final())

    async def _sweep_orphan_final(self) -> None:
        """Dispatch provider finals no VAD speech cycle ever drained (see
        ``_on_stt_partial_final``). VAD-driven consumers are parked on the
        transcript-ready event and take finals within milliseconds, so a
        non-empty queue after the delay is genuinely orphaned. No-ops
        whenever the normal machinery owns the text: user audibly
        speaking, a turn timer pending, playback running, or held."""
        await asyncio.sleep(0.6)
        if self.conn.is_closed or self.conn.is_muted:
            return
        if not self.state._user_silent.is_set():
            return
        if self.state._turn_timer and not self.state._turn_timer.done():
            return
        if self.state._tts_playing and not self.state._playback_paused:
            return  # audible playback owns the floor; a PAUSE is eligible —
            # its orphaned final is exactly the commit evidence
        text = self.stt.drain_transcript()
        if not text:
            return
        if self._is_interim_fallback_dup(text):
            logger.info(
                f"[{self.conn.peer_addr}] Orphan final duplicates the "
                f"interim-dispatched tail — dropped: {text}")
            return
        busy = (
            (self.state._utterance_task and not self.state._utterance_task.done())
            or (self._typed_turn and not self._typed_turn.done())
        )
        if busy:
            if text in self.state._queued_speech:
                return
            logger.info(
                f"[{self.conn.peer_addr}] Orphan STT final while turn busy "
                f"— queuing: {text}")
            self.state._queued_speech.append(text)
            self._interrupt_for_queued_speech()
            return
        logger.info(
            f"[{self.conn.peer_addr}] Orphan STT final — dispatching "
            f"through the turn machinery: {text}")
        self._append_turn_segment(text)
        joined = " ".join(self.state._turn_segments)
        self._cancel_turn_timer()
        self.state._turn_timer = asyncio.create_task(
            self._turn_timeout_handler(joined)
        )

    def _on_turn_dispatch(self, text: str) -> None:
        """Fires per REAL dispatched turn — including continuation/queued
        re-dispatches inside the pipeline loop, which the old frames-around-
        super() shape missed entirely (live-hit 2026-08-12 04:07: a barge-in
        utterance re-dispatched from the queue left its caption stuck in the
        composer through the whole think phase and the halo frozen on the
        stale phase). ``final`` consumes the composer caption; ``thinking``
        drives the halo until the first reply audio."""
        if not self.conn.is_closed:
            self.conn.send_event({"type": "final", "text": text})
            self.conn.send_event({"type": "state", "state": "thinking"})

    async def _process_utterance(self, text: str) -> None:
        try:
            await super()._process_utterance(text)
        finally:
            if not self.conn.is_closed:
                self.conn.send_event({"type": "state", "state": "listening"})

    # State frames ride the REAL playback edges (R4.2): 'speaking' when the
    # first audio chunk actually plays (not at segment-task creation, which
    # made 'thinking' invisible), 'listening' the moment a segment drains or
    # is cut — including between segments of one turn (pre-tool speech →
    # silent tool run), where the mic is live long before the turn ends.

    def _on_tts_playback_start(self) -> None:
        self.conn.send_event({"type": "state", "state": "speaking"})

    def _on_tts_playback_end(self) -> None:
        if not self.conn.is_closed:
            self.conn.send_event({"type": "state", "state": "listening"})

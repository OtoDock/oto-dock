"""Per-call phone pipeline orchestrator.

Lifecycle: AudioSocket -> VAD -> STT -> LLM (proxy or direct API) -> TTS ->
AudioSocket.  CallPipeline is the spine (construction, the run() lifecycle,
the listen loop, inbound greeting, teardown); cohesive concerns live in the
playback / turn / outbound / llm mixins, all operating on the shared
CallState.
"""

import asyncio
import contextlib
import logging
import time

import config

from transport.base import FRAME_AUDIO, FRAME_HANGUP, MediaTransport, TransportError
from audio.providers.vad.base import VadEvent, VadState
from audio.providers.vad.silero import SileroVad
from audio.providers.tts.base import TTSProvider
from audio.providers.turn.dispatcher import build_dispatcher
from audio.streaming.lang import base_lang
from ambience import load_ambience
from breath import load_breath
from voice_texture import VoiceTexture
from proxy.llm_factory import create_llm_backend
from proxy.client import report_turn_classifier_usage
from config_manager import ConfigManager, PhoneRoute
from fillers import filler_cache

from .state import CallState
from .providers import build_stt, build_tts, filler_key_for
from .vad_serial import SerializedVad
from .playback import PlaybackMixin
from .turn import TurnMixin
from .outbound import OutboundMixin
from .llm import LlmStreamingMixin
from .pin_gate import PinGateMixin

logger = logging.getLogger("pipeline")


class CallPipeline(PinGateMixin, PlaybackMixin, TurnMixin, OutboundMixin, LlmStreamingMixin):
    """Orchestrates a single phone call through the voice AI pipeline."""

    def __init__(
        self,
        conn: MediaTransport,
        route: PhoneRoute | None = None,
        cfg: ConfigManager | None = None,
        call_manager=None,
        outbound_call_id: str | None = None,
        llm_backend=None,
        audiosocket_uuid: str = "",
        caller_info: dict | None = None,
    ):
        self.conn = conn
        # Audio format from the transport: telephony is 8 kHz both ways;
        # duplex is 16 kHz mic in / 24 kHz TTS out.
        self._rate_in = conn.sample_rate_in
        self._rate_out = conn.sample_rate_out
        self._frame_bytes_out = conn.frame_bytes_out
        self._byte_rate_out = self._rate_out * config.SAMPLE_WIDTH
        self.cfg = cfg or ConfigManager()
        self.route = route or PhoneRoute(
            id="", direction="inbound", agent="unified", llm_mode="proxy",
            greeting="", language="en", phone_context_override="", enabled=True,
            ami_caller_id="", ami_outbound_context="", dial_prefix="",
        )
        self.agent_model = self.route.agent
        # Pass-through to the proxy warmup so it can resolve the route's
        # bound trigger (if any) and build ``${trigger.*}`` for manifest
        # ``agent_context`` blocks. ``caller_info`` is populated from the
        # AMI dial event by the outbound caller (call_manager / http_api);
        # empty dict when no AMI / no event matched.
        self._audiosocket_uuid = audiosocket_uuid or (conn.call_uuid or "")
        self._caller_info: dict = caller_info or {}

        # A call is locked to one language (the route's). Normalise to a base
        # code (en/el/de) — the form the voices map, STT, and fillers all use.
        self._locked_language: str = base_lang(self.route.language)

        # Resolve the route's STT/TTS providers (route override → call default)
        # + the effective filler toggles, then build one of each for this call.
        self._route_settings = self.cfg.resolve_route_settings(self.route)
        self.stt = build_stt(self.cfg, self._route_settings.stt_provider)
        # Silero rides one worker thread: inference off the event loop (its
        # p99 spikes stalled the paced TTS sender — the crackle) and mode
        # swaps serialized against it (audit F11). See vad_serial.py.
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
        self.llm = llm_backend  # Set later in run() if None
        self._is_direct = False  # Set from actual backend type in run()
        self._turn_clf = None  # Turn-classifier dispatcher; built in run()

        # One TTS for the call (the route's resolved provider), with the voice
        # for the call's language selected (English fallback if that language has
        # no configured voice — see TTSProvider.select_voice).
        self.tts: TTSProvider = build_tts(self._route_settings.tts_provider)
        self.tts.select_voice(self._locked_language)
        # Fillers are cached per (provider, voice, language) so they always match
        # this route's TTS voice (see fillers.py); same key the pre-warm uses.
        self._filler_key = filler_key_for(
            self._route_settings.tts_provider, self.tts, self._locked_language,
            self._rate_out)

        # Ambience bed / voice texture / breath are telephony-flavoured and
        # their assets + DSP constants are 8 kHz — gate the whole trio on the
        # telephony out-rate so a duplex session (24 kHz out) never plays a
        # slow-motion clip. Re-mastering them for hi-fi is a deliberate
        # non-goal of duplex v1.
        telephony_out = self._rate_out == config.SAMPLE_RATE
        # Background ambience bed for this route (None = off / asset missing)
        self._ambience = (
            load_ambience(self.route.background_sound,
                          self.cfg.background_sound_gain)
            if telephony_out else None
        )
        # Voice texture (grain + early reflections) glues the voice into the
        # bed — only meaningful when the route actually has a bed.
        self._texture = (
            VoiceTexture(self.cfg.voice_texture)
            if self._ambience is not None and self.cfg.voice_texture > 0
            else None
        )
        # Pre-response inhale (None = disabled / asset missing)
        self._breath_pcm = (
            load_breath(self.cfg.breath_gain)
            if self.cfg.breath_enabled and telephony_out else None
        )

        # Outbound call support
        self._call_manager = call_manager
        self._outbound_call_id = outbound_call_id
        self._is_outbound = outbound_call_id is not None
        # Derived audio constant (config; read by the turn loop)
        self._speech_audio_max_bytes = int(
            self.cfg.smart_turn_audio_window_s * self._rate_in * config.SAMPLE_WIDTH
        )

        # All mutable per-call runtime state lives in one explicit object, so
        # the decomposed pipeline modules share state instead of reaching into
        # each other.  Collaborators (conn/cfg/route/stt/vad/tts/llm/providers)
        # stay as direct attributes above.
        self.state = CallState()

    async def run(self) -> None:
        """Run the full call pipeline until hangup or timeout."""
        self.state._running = True
        logger.info(
            f"[{self.conn.peer_addr}] Pipeline started "
            f"(agent={self.agent_model}, outbound={self._is_outbound})"
        )

        # Ambience runs for the whole call (greeting through hangup) so the
        # line never goes dead-quiet between exchanges.
        if self._ambience is not None:
            self.state._ambience_task = asyncio.create_task(self._ambience_loop())

        try:
            # Connect the call's TTS + (re)select the route-language voice.
            # Prewarm at the session's REAL output rate — a rate-bound socket
            # (ElevenLabs) otherwise pays a fresh rebind on the first turn.
            await self.tts.connect(output_sample_rate=self._rate_out)
            self._select_tts(self._locked_language)

            # Inbound PIN gate — deliberately BEFORE filler ensure, the turn
            # classifier, STT pre-connect, and the LLM/warmup: a call that
            # fails the PIN pays for none of them, and a cold filler cache
            # (seconds of REST synthesis) must not delay the prompt.
            if (not self._is_outbound and self.route.direction == "inbound"
                    and self.route.pin):
                if not await self._pin_gate():
                    return

            # Ensure this route's filler clips exist (per provider+voice+language).
            # Pre-warmed on each config push (see pipeline.providers.prewarm_fillers);
            # this is the self-heal if a call lands before pre-warm finished, and a
            # no-op once warm (content-keyed — see fillers.ensure).
            try:
                await filler_cache.ensure(
                    self._filler_key, self.tts,
                    backchannel_phrases=self.cfg.backchannel_phrases,
                    thinking_phrases=self.cfg.thinking_phrases,
                )
            except Exception as e:
                logger.warning(f"[{self.conn.peer_addr}] Filler cache init failed: {e}")

            # Load turn classifier from config (per-call, not module-level)
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

            # Pre-connect STT if the provider benefits from early connection
            # (e.g., cloud providers with expensive WebSocket setup).
            if self.stt.needs_pre_connect:
                try:
                    await self.stt.start(language=self._locked_language, sample_rate=self._rate_in, interim_results=True)
                    self.state._stt_active = True
                    logger.info(f"[{self.conn.peer_addr}] STT pre-connected")
                except Exception as e:
                    logger.warning(f"[{self.conn.peer_addr}] STT pre-connect failed: {e}")

            # STT liveness guard: keepalive through send starvation + reconnect
            # on mid-call death. Runs for the whole call regardless of how STT
            # was started (pre-connect here or lazily at SPEECH_START).
            self.state._stt_last_fed = time.monotonic()
            self.state._stt_guard_task = asyncio.create_task(self._stt_guard())

            # Outbound: reuse the pre-warmed connection — it holds the live proxy
            # session that already generated the opening + task context, so the
            # call continues with full context instead of a cold session. Falls
            # back to a fresh backend if pre-warm didn't run, its socket died, or
            # the LLM mode differs — so this only matches or improves the old path.
            if self._is_outbound and self.llm is None and self._call_manager:
                pw_call = self._call_manager.get_call(self._outbound_call_id)
                pw_client = getattr(pw_call, "warmup_client", None) if pw_call else None
                if pw_client is not None:
                    pw_call.warmup_client = None  # pipeline takes ownership
                    if (getattr(pw_client, "_ws_connected", False)
                            and getattr(pw_client, "llm_mode", "proxy") == self.route.llm_mode):
                        self.llm = pw_client
                        logger.info(
                            f"[{self.conn.peer_addr}] Reusing pre-warmed connection "
                            f"(session={pw_client.session_id})"
                        )
                    else:
                        with contextlib.suppress(Exception):
                            await pw_client.close()

            # Create LLM backend (all modes go through ProxyClient).
            # Thread audiosocket UUID + caller info into the proxy warmup
            # so manifest ``agent_context`` blocks (incl. ``builder``
            # lookups against CRMs) resolve ``${trigger.*}``.
            if self.llm is None:
                call_type = "outbound" if self._is_outbound else "inbound"
                self.llm = await create_llm_backend(
                    self.route, call_type=call_type,
                    audiosocket_uuid=self._audiosocket_uuid,
                    caller_phone=self._caller_info.get("phone", ""),
                    caller_did=self._caller_info.get("did", ""),
                    dial_event=self._caller_info.get("dial_event") or None,
                )
            self._is_direct = getattr(self.llm, 'llm_mode', 'proxy') == 'direct'
            logger.info(
                f"[{self.conn.peer_addr}] LLM backend: "
                f"{'direct' if self._is_direct else 'proxy'} "
                f"({self.llm.__class__.__name__})"
            )

            if self._is_outbound:
                call = self._call_manager.get_call(self._outbound_call_id) if self._call_manager else None

                if call and call.warmup_session_id:
                    # Pre-warmed session: inject session ID, then register with
                    # the WS handler via warmup message (proxy sets direct_session).
                    self.llm.set_session_id(call.warmup_session_id)
                    self.state._warmup_task = asyncio.create_task(self._warmup_session())
                    await self.state._warmup_done.wait()
                    logger.info(
                        f"[{self.conn.peer_addr}] Using pre-warmed session "
                        f"{call.warmup_session_id}"
                    )
                else:
                    # No pre-warmup: warmup now
                    self.state._warmup_task = asyncio.create_task(self._warmup_session())
                    await self.state._warmup_done.wait()

                await self._send_outbound_opening()
                await self._listen_loop()
                # The listen loop exits once the call is hung up (_running=False,
                # set after the farewell TTS plays — see the [CALL_COMPLETE] block
                # in llm.py). Await the utterance task in case it's still finishing
                # the farewell + hangup sequence.
                if self.state._utterance_task and not self.state._utterance_task.done():
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await self.state._utterance_task
                await self._finalize_outbound_call()
            else:
                # Inbound: warmup during greeting → greeting + listen
                # _play_greeting() now starts _listen_loop() internally
                # for barge-in support during greeting playback
                self.state._warmup_task = asyncio.create_task(self._warmup_session())
                await self._play_greeting()
        except TransportError as e:
            logger.info(f"[{self.conn.peer_addr}] AudioSocket closed: {e}")
            # Finalize outbound call on remote hangup (callee hung up)
            if self._is_outbound:
                try:
                    await self._finalize_outbound_call()
                except Exception as fin_err:
                    logger.error(
                        f"[{self.conn.peer_addr}] Finalize after hangup failed: {fin_err}"
                    )
        except Exception as e:
            logger.error(f"[{self.conn.peer_addr}] Pipeline error: {e}", exc_info=True)
            if self._is_outbound:
                try:
                    await self._finalize_outbound_call()
                except Exception as fin_err:
                    logger.debug("Finalize after pipeline error failed: %s", fin_err)
        finally:
            await self._cleanup()

    async def _play_greeting(self) -> None:
        """Play greeting via streaming TTS path (supports barge-in).

        Uses the same streaming TTS path as _process_utterance. The greeting
        is played via _stream_tts_audio() while _listen_loop() reads incoming
        frames. All existing barge-in logic works naturally:
        - SPEECH_PROBABLE → early STT unmute
        - SPEECH_START → playback pauses (_pause_playback)
        - Non-empty final → commit (_cancel_tts) → dispatch
        - No transcript → playback resumes where it froze
        """
        fallback = self.cfg.voice_phrases.get(self._locked_language, {}).get(
            "greeting_fallback", "Hello, how can I help you?",
        )
        greeting = self.route.greeting or fallback
        self._select_tts(self._locked_language)
        logger.info(f"[{self.conn.peer_addr}] Playing greeting (voice: {self._locked_language})")

        # Start greeting via streaming TTS (same path as _process_utterance)
        self.tts.start_streaming_context(
            language=self._locked_language, output_sample_rate=self._rate_out)
        self.state._audio_out_buf.clear()
        self.state._utterance_cancelled = False
        self.state._tts_task = asyncio.create_task(self._stream_tts_audio())
        await self.tts.send_text_chunk(greeting, is_last=True)

        # Start cleanup watcher (resets _tts_playing when greeting TTS finishes)
        greeting_cleanup = asyncio.create_task(self._greeting_tts_cleanup())

        # Enter listen loop immediately — handles barge-in, speech, everything
        await self._listen_loop()

        if not greeting_cleanup.done():
            greeting_cleanup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await greeting_cleanup

    async def _stt_guard(self) -> None:
        """STT liveness guard — a call must never go deaf (live-hit 2026-08-14:
        Deepgram idle-died mid-call after an inbound media gap, ElevenLabs died
        during a 16 s playback mute; with no reconnect, every later utterance
        hit a closed socket and both calls ended deaf).

        Every 2 s: (a) poll provider health (`is_open` + `pop_fatal_error` —
        a dead socket can't raise from send_audio/keepalive, both log-and-
        return) and reconnect on death; (b) if nothing has been fed for 3 s
        (playback mute, opening discard, inbound media gap, held playback —
        the last two also stall the listen loop itself, which is why this is
        a timer and not an in-loop check), send the provider keepalive.
        Cadence: 2 s guard + 3 s threshold leaves margin inside Deepgram's
        ~10 s net0001 idle window even with the provider-side KeepAlive rate
        limit (audit F8)."""
        try:
            while self.state._running and not self.conn.is_closed:
                await asyncio.sleep(2.0)
                await self._stt_guard_tick()
        except asyncio.CancelledError:
            return

    async def _stt_guard_tick(self) -> None:
        """One guard iteration (split out so tests drive it directly)."""
        if not self.state._stt_active or self.state._stt_reconnecting:
            return
        fatal = self.stt.pop_fatal_error()
        if fatal:
            logger.warning(f"[{self.conn.peer_addr}] STT fatal: {fatal}")
        if fatal or not self.stt.is_open:
            await self._stt_reconnect()
            return
        # Failure mode #3 — alive but MUTE (live-hit 2026-08-14): socket
        # open, sends flowing, VAD tracking real speech, zero results.
        # Neither the death poll above nor the starvation keepalive below
        # can see it; the accumulated unheard-speech counter can.
        last_result = self.stt.last_result_monotonic
        if last_result != self.state._stt_result_seen:
            self.state._stt_result_seen = last_result
            self.state._stt_unheard_speech_s = 0.0
        elif self.state._stt_unheard_speech_s >= self.cfg.stt_mute_reconnect_s:
            logger.warning(
                f"[{self.conn.peer_addr}] STT mute session: "
                f"{self.state._stt_unheard_speech_s:.1f}s of speech with "
                f"zero results — reconnecting"
            )
            self.state._stt_unheard_speech_s = 0.0
            await self._stt_reconnect()
            return
        if time.monotonic() - self.state._stt_last_fed >= 3.0:
            try:
                await self.stt.keepalive()
            except Exception as e:
                logger.warning(f"[{self.conn.peer_addr}] STT keepalive error: {e}")
            self.state._stt_last_fed = time.monotonic()

    async def _stt_reconnect(self) -> None:
        """One reconnect chain per death incident (latched), capped backoff.

        `ensure_alive` returns immediately when the probe finds the socket
        healthy, so this doubles as the cheap post-playback probe. Never
        clears the transcript queue (audit F6 — after a barge-in it holds
        the interrupting utterance's finals)."""
        if self.state._stt_reconnecting:
            return
        self.state._stt_reconnecting = True
        try:
            for delay in (0.0, 0.5, 1.0, 2.0):
                if delay:
                    await asyncio.sleep(delay)
                if not self.state._running:
                    return
                try:
                    ok = await self.stt.ensure_alive(
                        self._locked_language, clear_queue=False)
                except Exception as e:
                    logger.warning(f"[{self.conn.peer_addr}] STT ensure_alive error: {e}")
                    ok = False
                if ok:
                    self.state._stt_last_fed = time.monotonic()
                    return
            logger.error(
                f"[{self.conn.peer_addr}] STT reconnect failed after retries — "
                f"will retry on the next guard tick"
            )
        except asyncio.CancelledError:
            raise
        finally:
            self.state._stt_reconnecting = False

    async def _listen_loop(self) -> None:
        """Main loop: read frames, run VAD, dispatch STT/LLM/TTS.

        This loop runs continuously. During TTS playback, it still
        reads frames and feeds VAD so barge-in works.
        """
        while self.state._running:
            # The loop exits on hangup / idle / _running=False — NOT on
            # _call_complete. Both inbound and outbound keep feeding VAD through
            # the final [CALL_COMPLETE] TTS so the farewell waits for the caller to
            # finish (no deadlock) and a caller who keeps talking resumes the call
            # instead of being cut off. The hangup sets _running=False once the
            # farewell has played (see the [CALL_COMPLETE] block in llm.py).
            if time.monotonic() - self.state._last_activity > self.cfg.idle_timeout_s:
                logger.info(f"[{self.conn.peer_addr}] Idle timeout ({self.cfg.idle_timeout_s}s)")
                break

            try:
                frame_type, payload = await asyncio.wait_for(
                    self.conn.read_frame(),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                continue

            if frame_type == FRAME_HANGUP:
                logger.info(f"[{self.conn.peer_addr}] Hangup received")
                break

            if frame_type != FRAME_AUDIO:
                continue

            self.state._last_activity = time.monotonic()
            event = await self.vad.aprocess(payload)

            # Accumulate raw audio while speaking (for Smart Turn)
            if self.vad.state == VadState.SPEAKING:
                self.state._speech_audio_buf.extend(payload)
                # Cap at max window size
                if len(self.state._speech_audio_buf) > self._speech_audio_max_bytes:
                    self.state._speech_audio_buf = self.state._speech_audio_buf[-self._speech_audio_max_bytes:]
                # Mute-session evidence for the STT guard: speech time the
                # provider has heard but not answered (guard resets this the
                # moment any result arrives). A paused playback feeds the
                # provider REAL audio (live-feed routing below), so it counts.
                if self.state._stt_active and (
                        not self.state._tts_playing
                        or self.state._playback_paused):
                    self.state._stt_unheard_speech_s += (
                        len(payload) / (self._rate_in * config.SAMPLE_WIDTH))

            if event == VadEvent.SPEECH_START:
                # Cooldown: ignore speech that starts within 500ms of the
                # user finishing their own utterance — this is trailing voice
                # energy, not a new interruption.
                # Only apply while TTS is AUDIBLY playing (the echo/trailing
                # scenario the cooldown exists for). A PAUSED playback means
                # the speaker is silent and the user just spoke — a restart
                # here is the user CONTINUING, and swallowing the event blinds
                # `_user_silent`/the turn timer, so the continuation batcher
                # dispatches mid-sentence and the request splits into
                # several prompts (live-hit 2026-08-24, duplex 7a092c59).
                # During normal listening, always process speech immediately
                # so we don't lose short utterances like "ee yes".
                since_speech_end = time.monotonic() - self.state._speech_end_time
                if (self.state._tts_playing
                        and not self.state._playback_paused
                        and since_speech_end < 0.5):
                    logger.info(
                        f"[{self.conn.peer_addr}] Speech start ignored "
                        f"(cooldown {since_speech_end*1000:.0f}ms after speech end)"
                    )
                    continue

                logger.info(f"[{self.conn.peer_addr}] Speech start")
                self.state._user_silent.clear()  # user is speaking

                # Clear audio buffer if starting a fresh turn (no prior segments)
                if not self.state._turn_segments:
                    self.state._speech_audio_buf.clear()

                # Cancel turn continuation timer — user is still talking
                if self.state._turn_timer and not self.state._turn_timer.done():
                    self._cancel_turn_timer()
                    logger.info(
                        f"[{self.conn.peer_addr}] Turn timer cancelled — user continuing"
                    )

                # Cancel backchannel if playing — user resumed speaking
                if self.state._backchannel_task and not self.state._backchannel_task.done():
                    self.state._backchannel_task.cancel()
                    self.state._backchannel_task = None

                # Barge-in: PAUSE playback if playing (pause/confirm/commit —
                # VAD evidence alone never cancels TTS or aborts the turn).
                # Do NOT cancel the utterance task — a commit (_cancel_tts,
                # driven by the transcript consumers) sets
                # _utterance_cancelled which stops LLM streaming, and the
                # utterance task gracefully processes any queued speech.
                if self.state._tts_playing and not self.state._playback_paused:
                    # Clear stale STT transcripts from previous turns.
                    # Any transcripts in the queue are leftovers (late finals
                    # from the previous turn); the user's new speech hasn't
                    # had time to produce transcripts yet.
                    self.stt.clear_queue()
                    self._pause_playback()
                elif self.state._playback_paused:
                    # New speech episode while already paused (rapid double
                    # barge-in): the pending resume-grace belongs to the OLD
                    # episode — cancel it; this episode's own SPEECH_END
                    # re-arms. The confirm backstop keeps running. The queue
                    # is NOT cleared here: it may hold the first episode's
                    # final (the commit evidence). The episode clock restarts
                    # — the duration gate judges each episode on its own,
                    # EXCEPT that a restart after a long-enough episode marks
                    # the pause: a pending final then survives the gate's
                    # short path (the words are the long episode's — audit
                    # B2, 2026-08-24).
                    prev_episode_s = (self.state._speech_end_time
                                      - self.state._pause_speech_start)
                    if prev_episode_s >= self.cfg.bargein_timer_s:
                        self.state._pause_long_episode = True
                    self.state._pause_speech_start = time.monotonic()
                    if (self.state._pause_grace_task
                            and not self.state._pause_grace_task.done()):
                        self.state._pause_grace_task.cancel()
                        self.state._pause_grace_task = None
                # If utterance is running but no TTS (LLM processing / tool use),
                # just capture speech — it will be queued on SPEECH_END

                if not self.state._stt_active:
                    await self.stt.start(language=self._locked_language, sample_rate=self._rate_in, interim_results=True)
                    self.state._stt_active = True

                await self.stt.send_audio(payload)
                self.state._stt_last_fed = time.monotonic()

            elif event == VadEvent.SPEECH_END:
                logger.info(f"[{self.conn.peer_addr}] Speech end")
                await self._process_speech_end()

            elif event == VadEvent.SPEECH_PROBABLE:
                # Early speech hint during barge-in (~64ms after speech starts).
                # Unmute STT to capture the beginning of speech if the provider
                # benefits from it.  Do NOT cancel TTS yet — barge-in needs
                # full confirmation from the sliding window.
                logger.info(f"[{self.conn.peer_addr}] Speech probable (early STT unmute)")
                if not self.state._stt_active:
                    await self.stt.start(language=self._locked_language, sample_rate=self._rate_in, interim_results=True)
                    self.state._stt_active = True
                if self.stt.supports_early_unmute:
                    self.state._stt_early_unmuted = True
                await self.stt.send_audio(payload)
                self.state._stt_last_fed = time.monotonic()
                # Also capture audio for Smart Turn (onset prosody during barge-in)
                self.state._speech_audio_buf.extend(payload)

            elif event == VadEvent.NONE and self.state._stt_active:
                # Send audio to STT during SPEAKING and IDLE states,
                # but delegate to the provider during TTS playback (echo
                # management).  Exceptions: early-unmute (SPEECH_PROBABLE
                # fired — capture the beginning of barge-in speech), and a
                # PAUSED playback (the speaker is silent, no echo risk — and
                # the live feed is what lets the confirming final form at
                # all: Deepgram's feed_during_tts clears its queue per
                # frame, ElevenLabs' is a no-op).
                if (not self.state._tts_playing
                        or self.state._playback_paused
                        or self.state._stt_early_unmuted):
                    await self.stt.send_audio(payload)
                else:
                    await self.stt.feed_during_tts(payload)
                self.state._stt_last_fed = time.monotonic()

    async def _process_speech_end(self, *, synthetic: bool = False) -> None:
        """SPEECH_END processing — shared by the real VAD event and the
        stuck-pause SYNTHETIC path (the pause monitor in playback.py calls
        with ``synthetic=True`` when a wedged episode holds a pending
        final: the user said real words, so the commit choke points must
        run even though the end event never fired). The synthetic caller
        SKIPS the pause duration gate — the gate's short path clears the
        provider queue, which would discard the very final that justified
        the synthetic call (audit A3, 2026-08-24)."""
        self.state._speech_end_time = time.monotonic()
        # End-to-end stamp: written ONLY here (see state.py note) —
        # dispatch captures the latest one for the turn line.
        self.state._t_speech_end = self.state._speech_end_time

        # Barge-in pause: the speech episode over a paused playback
        # just ended. A SHORT episode (< bargein_timer_s — the same
        # duration filter the pre-pause design applied) is ignored
        # entirely: queue cleared, playback resumed, nothing ever
        # dispatched — this is what keeps acks ("ok", "ναι") and
        # coughs from interrupting the reply. A long-enough episode
        # arms the resume-grace and falls through to NORMAL
        # transcript processing: a non-empty final commits through
        # the queue/dispatch choke points; no words → the grace
        # task resumes playback where it froze.
        if self.state._playback_paused and not synthetic:
            if self._handle_pause_speech_end():
                self.state._user_silent.set()
                return

        if self.state._stt_active:
            # Force-flush any pending STT tokens before draining.
            await self.stt.force_endpoint()

            if self._turn_busy():
                # LLM busy — drain STT (keep alive) and queue speech
                transcript = self.stt.drain_transcript()
                if transcript and self._is_interim_fallback_dup(transcript):
                    logger.info(
                        f"[{self.conn.peer_addr}] Drained final duplicates "
                        f"the interim-dispatched tail — dropped: {transcript}"
                    )
                    transcript = None

                if transcript:
                    self._append_turn_segment(transcript)

                if self.state._turn_segments:
                    all_text = " ".join(self.state._turn_segments)
                    self.state._turn_segments.clear()
                    self._cancel_turn_timer()
                    logger.info(
                        f"[{self.conn.peer_addr}] Queuing speech "
                        f"(LLM busy): {all_text}"
                    )
                    self.state._queued_speech.append(all_text)
                    self._interrupt_for_queued_speech()
                elif transcript:
                    logger.info(
                        f"[{self.conn.peer_addr}] Queuing speech "
                        f"(LLM busy): {transcript}"
                    )
                    self.state._queued_speech.append(transcript)
                    self._interrupt_for_queued_speech()
                else:
                    # STT may not have finalized yet —
                    # schedule a delayed retry to queue it.
                    logger.info(
                        f"[{self.conn.peer_addr}] Empty transcript "
                        f"(LLM busy), scheduling delayed queue retry"
                    )
                    asyncio.create_task(
                        self._delayed_queue_retry()
                    )
            else:
                # LLM free — drain transcript (keep STT open
                # so connecting words aren't lost between segments)
                transcript = self.stt.drain_transcript()
                if transcript and self._is_interim_fallback_dup(transcript):
                    logger.info(
                        f"[{self.conn.peer_addr}] Drained final duplicates "
                        f"the interim-dispatched tail — dropped: {transcript}"
                    )
                    transcript = None

                if transcript:
                    self._append_turn_segment(transcript)
                    logger.info(
                        f"[{self.conn.peer_addr}] Turn segment "
                        f"{len(self.state._turn_segments)}: {transcript}"
                    )
                    # Got transcript — start/restart classification timer
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
                    # STT hasn't finalized yet — wait for it.
                    # Cancel any existing timer so we don't dispatch
                    # without this segment's transcript.
                    self._cancel_turn_timer()
                    logger.info(
                        f"[{self.conn.peer_addr}] Empty transcript, "
                        f"scheduling delayed drain retry "
                        f"(segments={len(self.state._turn_segments)})"
                    )
                    asyncio.create_task(
                        self._delayed_drain_retry()
                    )

        # Signal silence AFTER transcript is queued, so consumers
        # waiting on _user_silent see the complete queue.
        self.state._user_silent.set()

    async def _cleanup(self) -> None:
        """Clean up all resources for this call."""
        self.state._running = False
        logger.info(f"[{self.conn.peer_addr}] Pipeline cleanup")

        # Capture this call's turn-classifier (Groq) token spend BEFORE teardown, so it
        # can be reported for local per-agent cost tracking after resources are freed.
        # Drain is pure-sync; None-safe if run() threw before the dispatcher was built.
        clf_in = clf_out = 0
        clf_model = ""
        if self._turn_clf is not None:
            try:
                clf_in, clf_out = self._turn_clf.drain_usage()
                clf_model = self._turn_clf.classifier_model()
            except Exception as e:
                logger.warning(f"[{self.conn.peer_addr}] Turn-classifier drain failed: {e}")

        # Cancel warmup if still running
        if self.state._warmup_task and not self.state._warmup_task.done():
            self.state._warmup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.state._warmup_task
            self.state._warmup_task = None

        # Stop the VAD inference worker (no-op for bare fakes in tests)
        if hasattr(self.vad, "shutdown"):
            self.vad.shutdown()

        # Cancel the STT liveness guard
        if self.state._stt_guard_task and not self.state._stt_guard_task.done():
            self.state._stt_guard_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.state._stt_guard_task
            self.state._stt_guard_task = None

        # Cancel turn accumulation timer
        self._cancel_turn_timer()
        self.state._turn_segments.clear()
        self.state._speech_audio_buf.clear()

        # Cancel barge-in pause machinery (confirm backstop + resume grace).
        # A paused playback keeps _tts_playing True, so the _cancel_tts below
        # wakes the parked sender and cancels the TTS task too.
        self._cancel_pause_timers()

        # Stop the ambience bed
        if self.state._ambience_task and not self.state._ambience_task.done():
            self.state._ambience_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.state._ambience_task
            self.state._ambience_task = None

        # Cancel filler tasks
        if self.state._backchannel_task and not self.state._backchannel_task.done():
            self.state._backchannel_task.cancel()
            self.state._backchannel_task = None
        if self.state._thinking_filler_task and not self.state._thinking_filler_task.done():
            self.state._thinking_filler_task.cancel()
            self.state._thinking_filler_task = None
        self.state._thinking_filler_done.set()

        if self.state._tts_playing:
            await self._cancel_tts()

        # Cancel parallel LLM if still running (may not be caught by _cancel_tts
        # if TTS already finished but LLM is still processing)
        if self.state._parallel_llm_task and not self.state._parallel_llm_task.done():
            self.state._parallel_llm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.state._parallel_llm_task
            self.state._parallel_llm_task = None

        if self.state._utterance_task and not self.state._utterance_task.done():
            self.state._utterance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.state._utterance_task

        cleanup_tasks = [self.stt.close(), self.conn.close()]
        if self.llm:
            cleanup_tasks.append(self.llm.close())
        cleanup_tasks.append(self.tts.close())
        if self._turn_clf is not None:
            cleanup_tasks.append(self._turn_clf.close())
        results = await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(f"Cleanup error [{i}]: {result}")

        # Report the call's turn-classifier spend (one row per call). Awaited with a
        # tight timeout and all errors swallowed inside the helper — never blocks or
        # fails teardown beyond the timeout. Runs after the gather so it can't delay
        # freeing the audiosocket/STT/LLM.
        if clf_in or clf_out:
            session_id = getattr(self.llm, "session_id", "") or self._audiosocket_uuid or ""
            await report_turn_classifier_usage(
                agent=self.route.agent, model=clf_model,
                input_tokens=clf_in, output_tokens=clf_out, session_id=session_id,
            )

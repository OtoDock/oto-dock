"""Deepgram Nova-3 STT provider.

Streaming transcription via the Deepgram async live WebSocket (transcripts are
pushed into an ``asyncio.Queue`` for the pipeline), plus batch ``transcribe_file``
via the prerecorded REST API (word-level timings for SRT generation).
"""

from __future__ import annotations

import asyncio
import logging
import time

from deepgram import (
    DeepgramClient,
    LiveOptions,
    LiveTranscriptionEvents,
    PrerecordedOptions,
)

from audio.capabilities import STTCapabilities
from audio.providers.credential_resolver import CredentialResolver
from audio.providers.stt.base import STTProvider, TranscriptResult, Word

logger = logging.getLogger(__name__)

# Minimum interval between KeepAlive messages (seconds). Deepgram's docs suggest
# sending KeepAlive to prevent idle timeout; once every 5s is plenty.
_KEEPALIVE_INTERVAL_S = 5.0

# Deepgram prerecorded pricing: nova-3 ≈ $0.0043/min = $0.0000717/sec
# (verified against deepgram.com/pricing 2026-06-03). Admin may override per
# instance via advanced.rate_override_per_unit.
_COST_PER_SECOND = 0.0000717

# Default Deepgram model (streaming + prerecorded). nova-3 is the current
# flagship general model; admin-overridable per provider via ``advanced.model_id``
# (set a newer/specialised one — nova-3-medical, nova-2-phonecall, … — without a
# code change). Applies to both the live socket and file transcription.
_DEFAULT_MODEL = "nova-3"

# Deepgram uses base language codes, except for a few regional variants it accepts
# natively. The dictation dropdown / native recognizer speak BCP-47 (e.g. el-GR,
# de-DE), so we normalize before handing the code to Deepgram — Deepgram has no
# "el-GR" (Greek is "el"); English variants like en-US/en-GB it does accept. (The
# phone already passes base codes, so it is unaffected.) nova-3 streaming supports
# all of en / de / es / fr / it / el.
# Only regional codes Deepgram accepts natively. Our dropdown's es-ES / fr-FR /
# de-DE / it-IT / el-GR are NOT Deepgram codes → they strip to es / fr / de / it /
# el. English regionals (en-US, en-GB, …) and a few documented others are kept.
_DG_REGIONAL = {
    "en-US", "en-GB", "en-AU", "en-IN", "en-NZ",
    "es-419", "pt-BR", "pt-PT", "zh-CN", "zh-TW", "fr-CA", "nl-BE",
}


def _to_deepgram_lang(tag: str) -> str:
    """BCP-47 tag → Deepgram language code: keep the regional variants Deepgram
    supports, else strip to the base subtag. Empty → ``multi`` (auto-detect)."""
    if not tag or tag in _DG_REGIONAL or "-" not in tag:
        return tag or "multi"
    return tag.split("-", 1)[0]


class DeepgramSTT(STTProvider):
    """Streaming + prerecorded speech-to-text via Deepgram."""

    capabilities = STTCapabilities(
        supports_streaming=True,
        supports_transcribe_file=True,
        supports_endpointing=True,
        supports_word_timestamps=True,
        is_local=False,
    )

    def __init__(
        self,
        *,
        api_key: str,
        endpointing_ms: int = 200,
        sample_rate: int = 8000,
        channels: int = 1,
        vad_silence_offset_ms: int = 50,
        model: str = _DEFAULT_MODEL,
    ):
        self._api_key = api_key
        self._client = DeepgramClient(api_key=api_key)
        self._model = model or _DEFAULT_MODEL
        self._endpointing_ms = endpointing_ms
        self._sample_rate = sample_rate
        self._channels = channels
        self._vad_silence_offset_ms = vad_silence_offset_ms
        self._connection = None
        self._transcript_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._is_open = False
        self._connection_gen: int = 0  # generation counter to guard _on_close race
        self._last_keepalive: float = 0.0  # monotonic time of last keep_alive
        self._transcript_ready = asyncio.Event()  # signalled on each new is_final
        self._latest_interim = ""  # most recent non-final partial (chat live text)
        self._last_interim_sent = ""  # dedup guard for pop_interim()
        self._interim_results = False  # mode last requested in start() (kept on reconnect)
        self._endpointing_override: int | None = None  # per-connection override (kept on reconnect)

    # ── Factory / metadata ─────────────────────────────────────────

    @classmethod
    def from_row(cls, row: dict, resolver: CredentialResolver) -> "DeepgramSTT":
        advanced = row.get("advanced") or {}
        return cls(
            api_key=resolver(row.get("credential_key", "")),
            endpointing_ms=int(advanced.get("call_endpointing_ms", 200)),
            vad_silence_offset_ms=int(advanced.get("vad_silence_offset_ms", 50)),
            model=advanced.get("model_id") or _DEFAULT_MODEL,
        )

    @classmethod
    def cost_per_unit(cls) -> float:
        return _COST_PER_SECOND

    @classmethod
    def default_advanced_settings(cls) -> dict:
        # vad_silence_offset_ms is deliberately NOT here: the live knob is the
        # global Audio-tab setting; from_row still reads a per-row override.
        return {"call_endpointing_ms": 500, "chat_endpointing_ms": 1500}

    @classmethod
    def validate_advanced(cls, settings: dict) -> dict[str, str]:
        errors: dict[str, str] = {}
        for key in ("call_endpointing_ms", "chat_endpointing_ms", "vad_silence_offset_ms"):
            if key in settings:
                try:
                    if int(settings[key]) < 0:
                        errors[key] = "must be >= 0"
                except (TypeError, ValueError):
                    errors[key] = "must be an integer"
        return errors

    @property
    def endpointing_ms(self) -> int:
        return self._endpointing_ms

    # ── Batch / prerecorded transcription ──────────────────────────

    async def transcribe_file(self, audio: bytes, *, language: str | None = None) -> TranscriptResult:
        """Transcribe a complete audio file via Deepgram's prerecorded REST API.

        Returns word-level timings (for SRT) + the decoded duration (the billing
        multiplier). Word/duration parsing is defensive — Deepgram's response
        objects vary across content types.
        """
        opts_kwargs = dict(model=self._model, smart_format=True, punctuate=True)
        if language:
            opts_kwargs["language"] = _to_deepgram_lang(language)
        else:
            opts_kwargs["detect_language"] = True
        options = PrerecordedOptions(**opts_kwargs)

        resp = await self._client.listen.asyncrest.v("1").transcribe_file({"buffer": audio}, options)

        text = ""
        words: list[Word] = []
        lang = language or "en"
        duration = 0.0
        try:
            channel = resp.results.channels[0]
            alt = channel.alternatives[0]
            text = (alt.transcript or "").strip()
            for w in (alt.words or []):
                words.append(Word(
                    word=getattr(w, "punctuated_word", None) or w.word,
                    start=float(w.start),
                    end=float(w.end),
                ))
            detected = getattr(channel, "detected_language", None)
            if detected:
                lang = detected
            duration = float(getattr(resp.metadata, "duration", 0.0) or 0.0)
            # Surface Deepgram's metadata warnings. An unsupported model+language
            # combo (e.g. Greek on nova-3 *prerecorded*) doesn't error — Deepgram
            # warns + silently falls back to a default model, yielding an empty or
            # garbage transcript that looks identical to "no speech". Logging the
            # warning turns that into an actionable signal (switch model, e.g. to
            # nova-2 for broad batch-language coverage).
            for warn in (getattr(resp.metadata, "warnings", None) or []):
                logger.warning(
                    "Deepgram prerecorded warning (model=%s, lang=%s): %s",
                    self._model, _to_deepgram_lang(language) if language else "auto",
                    getattr(warn, "message", None) or warn,
                )
        except (AttributeError, IndexError, TypeError) as e:
            logger.warning(f"Deepgram prerecorded parse error: {e}")

        self._log_transcript("Deepgram file", text)
        return TranscriptResult(
            text=text, language=lang, audio_seconds=duration, words=words, provider_used="deepgram",
        )

    # ── Streaming surface ──────────────────────────────────────────

    async def start(
        self, language: str = "multi", sample_rate: int | None = None, interim_results: bool = False,
        endpointing_ms: int | None = None,
    ) -> None:
        """Open a streaming connection to Deepgram.

        ``sample_rate`` overrides the instance default for THIS connection (the
        chat mic streams 16 kHz; the call default is 8 kHz). Mismatching it
        makes Deepgram decode the PCM at the wrong rate → empty transcripts.

        ``interim_results`` streams live partial transcripts (so the chat mic
        shows text as you speak — the native/Gboard feel) and turns on smart
        formatting. Left off for the call pipeline, which only dispatches
        finalized utterances.

        ``endpointing_ms`` overrides the configured (call) endpointing for THIS
        connection — chat dictation passes ``advanced.chat_endpointing_ms`` so
        low-latency call tuning doesn't make dictation commit on every breath.
        """
        rate = sample_rate or self._sample_rate
        language = _to_deepgram_lang(language)
        self._latest_interim = ""
        self._last_interim_sent = ""
        self._interim_results = interim_results
        self._endpointing_override = endpointing_ms
        self._transcript_queue = asyncio.Queue()
        self._transcript_ready.clear()
        self._connection_gen += 1
        gen = self._connection_gen
        self._connection = self._client.listen.asyncwebsocket.v("1")

        self._connection.on(LiveTranscriptionEvents.Transcript, self._on_transcript)
        self._connection.on(LiveTranscriptionEvents.Error, self._on_error)
        # Bind the current generation so stale _on_close callbacks are ignored
        self._connection.on(
            LiveTranscriptionEvents.Close,
            lambda _conn, close, _gen=gen, **kw: self._on_close(_conn, close, _gen=_gen, **kw),
        )

        opts: dict = dict(
            model=self._model,
            language=language,
            encoding="linear16",
            sample_rate=rate,
            channels=self._channels,
            punctuate=True,
            endpointing=endpointing_ms if endpointing_ms is not None else self._endpointing_ms,
        )
        if interim_results:
            opts["interim_results"] = True
            opts["smart_format"] = True
        options = LiveOptions(**opts)

        started = await self._connection.start(options)
        if not started:
            raise RuntimeError("Failed to start Deepgram connection")
        self._is_open = True
        logger.info("Deepgram STT connection opened")

    async def send_audio(self, audio_bytes: bytes) -> None:
        """Send raw PCM audio to Deepgram for transcription."""
        if self._connection and self._is_open:
            try:
                await self._connection.send(audio_bytes)
            except Exception as e:
                logger.error(f"Deepgram send error: {e}")
        elif not self._is_open and self._connection:
            logger.warning("Deepgram send skipped — connection closed")

    async def send_keep_alive(self) -> None:
        """Send a KeepAlive message to maintain the WebSocket connection.

        Rate-limited to at most once per _KEEPALIVE_INTERVAL_S seconds.
        Use this instead of sending silence frames during TTS playback —
        silence frames train Deepgram's acoustic model to expect silence,
        causing the first real speech after TTS to produce empty transcripts.
        KeepAlive keeps the connection alive without affecting the model.
        """
        if not self._connection or not self._is_open:
            return
        now = time.monotonic()
        if now - self._last_keepalive < _KEEPALIVE_INTERVAL_S:
            return
        try:
            await self._connection.keep_alive()
            self._last_keepalive = now
            logger.debug("Deepgram KeepAlive sent")
        except Exception as e:
            logger.warning(f"Deepgram KeepAlive error: {e}")

    def clear_queue(self) -> None:
        """Discard all pending transcripts (e.g., after TTS echo)."""
        discarded = 0
        while not self._transcript_queue.empty():
            try:
                self._transcript_queue.get_nowait()
                discarded += 1
            except asyncio.QueueEmpty:
                break
        self._transcript_ready.clear()
        if discarded:
            logger.info(f"STT queue cleared ({discarded} items discarded)")

    def drain_transcript(self) -> str | None:
        """Return any transcripts available so far WITHOUT closing the connection.

        Non-blocking: grabs whatever is_final transcripts Deepgram has
        already delivered.  The connection stays open so more audio can
        be sent (for persistent per-turn STT).
        """
        parts = []
        while not self._transcript_queue.empty():
            try:
                text = self._transcript_queue.get_nowait()
                if text:
                    parts.append(text)
            except asyncio.QueueEmpty:
                break
        # Reset event after draining so the next wait catches fresh arrivals
        self._transcript_ready.clear()
        transcript = " ".join(parts).strip() if parts else None
        if transcript:
            self._log_transcript("STT drain", transcript)
        return transcript

    def pop_interim(self) -> str | None:
        """Return the latest live (non-final) partial if it changed since the
        last call. Used by the chat WS to stream text as the user speaks; the
        call pipeline never enables interims so this stays empty there."""
        txt = self._latest_interim
        if txt and txt != self._last_interim_sent:
            self._last_interim_sent = txt
            return txt
        return None

    async def wait_for_transcript(self, timeout: float = 1.0) -> str | None:
        """Wait up to `timeout` seconds for the next is_final transcript.

        Unlike drain_transcript (instant, non-blocking), this blocks until
        Deepgram actually delivers a finalized transcript — no fixed-delay
        guessing.  Returns the transcript text, or None on timeout.
        """
        self._transcript_ready.clear()
        try:
            await asyncio.wait_for(self._transcript_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return self.drain_transcript()

    async def force_endpoint(self) -> None:
        """Flush Deepgram's buffer into an is_final immediately via the ``Finalize``
        control message, instead of waiting out the server-side ``endpointing``
        silence. Called at VAD SPEECH_END so the final transcript arrives in ~1
        network RTT rather than ~``endpointing_ms`` later — the bulk of the
        post-speech turn latency. No-op if the socket isn't open."""
        if self._connection and self._is_open:
            try:
                await self._connection.finalize()
            except Exception as e:
                logger.warning(f"Deepgram finalize error: {e}")

    @property
    def latest_interim(self) -> str:
        """Most recent non-final partial (empty once an is_final commits, or when
        interims weren't requested). The pipeline uses it as a fallback transcript
        when a forced finalize's is_final fails to arrive — so a lost final
        degrades to the live partial instead of a dropped turn."""
        return self._latest_interim

    async def finish(self) -> str | None:
        """Signal end of audio and wait for final transcript.

        Returns the final transcript text, or None if nothing was recognized.
        """
        if self._connection and self._is_open:
            try:
                await self._connection.finish()
            except Exception as e:
                logger.error(f"Deepgram finish error: {e}")

        self._is_open = False

        # Collect all queued transcripts
        parts = []
        while not self._transcript_queue.empty():
            try:
                text = self._transcript_queue.get_nowait()
                if text:
                    parts.append(text)
            except asyncio.QueueEmpty:
                break

        transcript = " ".join(parts).strip() if parts else None
        if transcript:
            self._log_transcript("STT transcript", transcript)
        return transcript

    async def close(self) -> None:
        """Close the connection without waiting for final transcript."""
        self._is_open = False
        if self._connection:
            try:
                await self._connection.finish()
            except Exception:
                pass
            self._connection = None

    # ── Lifecycle hooks ──────────────────────────────────────────

    async def feed_during_tts(self, audio_bytes: bytes) -> None:
        """Send silence to keep Nova-3 model active, discard echo transcripts.

        Silence bytes maintain the acoustic model state during TTS playback
        without feeding it actual echo audio (which would produce garbled
        transcripts).  Stale transcripts (late is_finals from prior speech)
        are discarded to prevent duplicate processing.
        """
        await self.send_audio(b"\x00" * len(audio_bytes))
        self.clear_queue()

    def on_tts_finished(self, was_interrupted: bool) -> None:
        """Clear echo transcripts after TTS finishes.

        Only clears when TTS finished normally (was_interrupted=False).
        After barge-in (was_interrupted=True), the user is speaking and
        their speech may already be in the transcript queue.
        """
        if not was_interrupted:
            self.clear_queue()

    async def feed_during_opening(self) -> None:
        """Send KeepAlive during long opening TTS (10+ seconds).

        KeepAlive keeps the WebSocket alive without feeding audio.
        Silence bytes would train the model to expect silence, causing
        empty transcripts on first real speech after the opening.
        """
        await self.send_keep_alive()

    async def recover_after_opening(self, language: str) -> bool:
        """Check WebSocket health after opening TTS, reconnect if needed."""
        if self._is_open:
            self.clear_queue()
            logger.info("STT still alive after opening TTS")
            return True
        # Connection died during opening — reconnect
        try:
            await self.close()
        except Exception:
            pass
        try:
            await self.start(
                language=language, interim_results=self._interim_results,
                endpointing_ms=self._endpointing_override,
            )
            logger.info("STT reconnected after opening TTS")
            return True
        except Exception as e:
            logger.warning(f"STT reconnect after opening failed: {e}")
            return False

    @property
    def needs_pre_connect(self) -> bool:
        return True  # ~650ms WebSocket setup

    @property
    def stay_open_between_turns(self) -> bool:
        return True  # 650ms reconnect cost

    @property
    def transcript_wait_timeout_s(self) -> float:
        return 1.0  # network RTT + endpointing jitter

    @property
    def supports_early_unmute(self) -> bool:
        return True  # needs speech onset for accurate transcription

    @property
    def vad_silence_padding_ms(self) -> int:
        return self._endpointing_ms + self._vad_silence_offset_ms

    # ── Event handlers ────────────────────────────────────────────

    async def _on_transcript(self, _conn, result, **kwargs) -> None:
        """Handle transcript events from Deepgram."""
        try:
            transcript = result.channel.alternatives[0].transcript
            is_final = result.is_final

            if transcript and is_final:
                self._log_transcript("Deepgram final", transcript)
                self._latest_interim = ""  # utterance committed → clear live partial
                self._last_interim_sent = ""
                await self._transcript_queue.put(transcript)
                self._transcript_ready.set()
            elif transcript and not is_final:
                self._latest_interim = transcript
                logger.debug(f"Deepgram interim: \"{transcript}\"")
            elif is_final and not transcript:
                logger.debug("Deepgram final: (empty)")
        except (IndexError, AttributeError) as e:
            logger.warning(f"Failed to parse Deepgram result: {e}")

    async def _on_error(self, _conn, error, **kwargs) -> None:
        """Handle errors from Deepgram."""
        logger.error(f"Deepgram error: {error}")

    async def _on_close(self, _conn, close, _gen: int = 0, **kwargs) -> None:
        """Handle connection close.

        Only resets _is_open if the callback is from the current connection
        generation — prevents stale callbacks from old connections clobbering
        the state of a freshly opened connection.
        """
        if _gen == self._connection_gen:
            self._is_open = False
            logger.info("Deepgram connection closed (current gen)")
        else:
            logger.info(
                f"Deepgram connection closed (stale gen={_gen}, "
                f"current={self._connection_gen}) — ignoring"
            )

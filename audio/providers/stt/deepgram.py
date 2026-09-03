"""Deepgram Nova-3 STT provider.

Streaming transcription via the Deepgram async live WebSocket (transcripts are
pushed into an ``asyncio.Queue`` for the pipeline), plus batch ``transcribe_file``
via the prerecorded REST API (word-level timings for SRT generation).
"""

from __future__ import annotations

import asyncio
import contextlib
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


# Punctuation smart_format may add/alter between an interim and its final —
# includes the Greek question mark (;) and ano teleia (·).
_WORD_STRIP = ".,;:!?·»«\"'()[]{}"


def _norm_words(text: str) -> list[str]:
    """Casefolded, punctuation-stripped tokens, for prefix comparisons between
    interim and final variants of the same audio window (dictation mode)."""
    return [t for t in (w.strip(_WORD_STRIP).casefold() for w in text.split()) if t]


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
        self._fatal_error: str | None = None  # last Error event, surfaced once via pop_fatal_error
        # Rate actually negotiated by the last start() — reconnects MUST reuse
        # it: falling back to the constructor default decodes a 16 kHz stream
        # as 8 kHz → permanent garbage transcripts (live duplex bug, audit F5).
        self._active_rate = sample_rate
        self._send_skips = 0  # closed-connection sends since last open (one WARNING per incident)

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
        self._active_rate = rate
        self._send_skips = 0
        self._latest_interim = ""
        self._last_interim_sent = ""
        self._fatal_error = None
        self._interim_results = interim_results
        self._endpointing_override = endpointing_ms
        self._transcript_queue = asyncio.Queue()
        self._transcript_ready.clear()
        self._connection_gen += 1
        gen = self._connection_gen
        self._connection = self._client.listen.asyncwebsocket.v("1")

        self._connection.on(LiveTranscriptionEvents.Transcript, self._on_transcript)
        # Bind the current generation so stale callbacks are ignored — a
        # replaced socket's dying gasp (net0001 after a mid-call reconnect)
        # must not surface as a pipeline "STT fatal" for the LIVE connection.
        self._connection.on(
            LiveTranscriptionEvents.Error,
            lambda _conn, error, _gen=gen, **kw: self._on_error(_conn, error, _gen=_gen, **kw),
        )
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
            # One WARNING per death incident, not one per 20 ms frame (a dead
            # 35 s call tail used to log 1,700+ of these). The guard task owns
            # recovery; the counter is reported when the connection reopens.
            self._send_skips += 1
            if self._send_skips == 1:
                logger.warning("Deepgram send skipped — connection closed "
                               "(suppressing repeats until reconnect)")

    async def send_keep_alive(self) -> None:
        """Send a KeepAlive message to maintain the WebSocket connection.

        Rate-limited to at most once per _KEEPALIVE_INTERVAL_S seconds.
        KeepAlive keeps the SOCKET alive without feeding audio — but it does
        NOT keep the streaming model's audio timeline warm: a session that
        receives only KeepAlives for ~10 s goes stale and the first real
        utterances return empty transcripts (live-hit 2026-08-14, outbound
        opening). Pair it with a silence feed (``feed_during_tts``) whenever
        real audio is being withheld for long; KeepAlive alone is only right
        for short gaps.
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
        # A discarded utterance's live partial must go with it — a stale
        # interim that never finalizes reads as permanent speech "evidence"
        # to the barge-in pause monitor (audit A5, 2026-08-24).
        self._latest_interim = ""
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
        last call. Used by the chat WS to stream text as the user speaks.

        Dictation mode also suppresses a pure backtrack — an interim revision
        that is a strict prefix of what was already sent (Deepgram briefly
        retracting words it will re-confirm) would visibly truncate the
        composer; real rewordings still pass."""
        txt = self._latest_interim
        if not txt or txt == self._last_interim_sent:
            return None
        if self.dictation_mode and self._last_interim_sent:
            new, old = _norm_words(txt), _norm_words(self._last_interim_sent)
            if len(new) < len(old) and old[: len(new)] == new:
                return None
        self._last_interim_sent = txt
        return txt

    async def wait_for_transcript(self, timeout: float = 1.0) -> str | None:
        """Wait up to `timeout` seconds for the next is_final transcript.

        Unlike drain_transcript (instant, non-blocking), this blocks until
        Deepgram actually delivers a finalized transcript — no fixed-delay
        guessing.  Returns the transcript text, or None on timeout.
        """
        self._transcript_ready.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._transcript_ready.wait(), timeout=timeout)
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

    @property
    def has_pending_finals(self) -> bool:
        """Finalized transcript(s) sitting in the queue, not yet drained —
        barge-in speech evidence (each mid-speech is_final clears
        ``latest_interim``, so the queue is where the proof lives)."""
        return not self._transcript_queue.empty()

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

        if self.dictation_mode:
            # A tail the user saw but Deepgram never finalized (stop hit
            # mid-window) must still commit — the client's stop() waits
            # ≤1.8s for exactly this trailing final.
            leftover = self._shown_interim()
            if leftover:
                parts.append(leftover)
                self._latest_interim = ""
                self._last_interim_sent = ""

        transcript = " ".join(parts).strip() if parts else None
        if transcript:
            self._log_transcript("STT transcript", transcript)
        return transcript

    async def close(self) -> None:
        """Close the connection without waiting for final transcript."""
        self._is_open = False
        if self._connection:
            with contextlib.suppress(Exception):
                await self._connection.finish()
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

        Socket-level belt only. The opening loop ALSO feeds each discarded
        frame as silence via ``feed_during_tts`` — KeepAlive alone kept the
        socket alive but left the streaming model's audio timeline stale,
        and the first 1-2 real utterances after the opening returned empty
        transcripts (live-hit 2026-08-14; the silence feed fixed it,
        verified live the same night — an older theory here claimed silence
        would "train the model to expect silence"; the live test came out
        the other way).
        """
        await self.send_keep_alive()

    async def keepalive(self) -> None:
        """Guard-task keepalive: the protocol KeepAlive message (rate-limited
        internally via ``send_keep_alive``)."""
        await self.send_keep_alive()

    async def ensure_alive(self, language: str, *, clear_queue: bool = False) -> bool:
        """Health probe + reconnect with the ACTIVE params (language passed by
        the pipeline; rate/interims/endpointing stored from the last start()).

        ``clear_queue`` only applies to the healthy path — after an opening
        (echo artifacts) pass True; after a barged-in playback or from the
        guard task pass False (the queue may hold the interrupting
        utterance's finals — audit F6).
        """
        if self._is_open:
            if clear_queue:
                self.clear_queue()
            logger.debug("Deepgram STT healthy")
            return True
        # Connection died — reconnect with the same negotiated rate (F5).
        skipped = self._send_skips
        with contextlib.suppress(Exception):
            await self.close()
        try:
            await self.start(
                language=language, sample_rate=self._active_rate,
                interim_results=self._interim_results,
                endpointing_ms=self._endpointing_override,
            )
            logger.info(
                "Deepgram STT reconnected mid-call"
                + (f" ({skipped} frames dropped while dead)" if skipped else "")
            )
            return True
        except Exception as e:
            logger.warning(f"Deepgram STT reconnect failed: {e}")
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

    def _shown_interim(self) -> str:
        """The richer of the live interim and the last one actually sent to the
        client — dictation paints whichever was delivered last, and a not-yet-
        popped newer interim only ever shows MORE. Used by dictation mode to
        decide what "the user already saw" for the never-drop guarantees."""
        latest, sent = self._latest_interim, self._last_interim_sent
        if len(_norm_words(sent)) > len(_norm_words(latest)):
            return sent
        return latest

    async def _on_transcript(self, _conn, result, **kwargs) -> None:
        """Handle transcript events from Deepgram."""
        try:
            transcript = result.channel.alternatives[0].transcript
            is_final = result.is_final

            if transcript and is_final:
                self._last_result_at = time.monotonic()
                committed = transcript
                if self.dictation_mode:
                    # Never drop painted words: if the shown interim extends
                    # this final (Deepgram sometimes finalizes only the head of
                    # a window on non-English languages), commit exactly what
                    # the user saw — the window's audio is consumed with its
                    # final, so the tail would never be re-delivered.
                    shown = self._shown_interim()
                    fin, intr = _norm_words(transcript), _norm_words(shown)
                    if len(intr) > len(fin) and intr[: len(fin)] == fin:
                        committed = shown
                        logger.info(
                            "Deepgram short final — committing shown interim "
                            f"(+{len(intr) - len(fin)} word(s))"
                        )
                self._log_transcript("Deepgram final", committed)
                self._latest_interim = ""  # utterance committed → clear live partial
                self._last_interim_sent = ""
                await self._transcript_queue.put(committed)
                self._transcript_ready.set()
                self._emit_partial_final(transcript)
            elif transcript and not is_final:
                self._last_result_at = time.monotonic()
                self._latest_interim = transcript
                logger.debug(f"Deepgram interim: \"{transcript}\"")
            elif is_final and not transcript:
                shown = self._shown_interim() if self.dictation_mode else ""
                if shown:
                    # An empty final orphans the painted interim: the base the
                    # composer commits onto never advances, and the next
                    # window's interim REPLACES everything shown (the Greek
                    # vanishing-words bug, 2026-09-01). Promote what the user
                    # saw instead. Duplex/call pipelines never take this path
                    # (dictation_mode is chat-relay-only): a promoted noise
                    # interim would be false barge-in evidence there.
                    self._log_transcript(
                        "Deepgram empty final — committing shown interim", shown)
                    self._latest_interim = ""
                    self._last_interim_sent = ""
                    await self._transcript_queue.put(shown)
                    self._transcript_ready.set()
                else:
                    logger.debug("Deepgram final: (empty)")
        except (IndexError, AttributeError) as e:
            logger.warning(f"Failed to parse Deepgram result: {e}")

    async def _on_error(self, _conn, error, _gen: int = 0, **kwargs) -> None:
        """Handle errors from Deepgram (generation-guarded like _on_close)."""
        if _gen != self._connection_gen:
            logger.info(
                f"Deepgram error on stale gen={_gen} "
                f"(current={self._connection_gen}) — ignoring: {str(error)[:120]}"
            )
            return
        logger.error(f"Deepgram error: {error}")
        self._fatal_error = f"Deepgram speech-to-text error: {str(error)[:200]}"

    def pop_fatal_error(self) -> str | None:
        err, self._fatal_error = self._fatal_error, None
        return err

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

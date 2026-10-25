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
import unicodedata

from deepgram import (
    DeepgramClient,
    LiveOptions,
    LiveTranscriptionEvents,
    PrerecordedOptions,
)

from audio.capabilities import STTCapabilities
from audio.providers.credential_resolver import CredentialResolver
from audio.providers.model_succession import DEFAULT_MODELS
from audio.providers.stt.base import Heard, STTProvider, TranscriptResult, Word
from audio.providers.stt.relisten import RELISTEN_FINISH_WAIT_S, Relisten, TurnRelisten

logger = logging.getLogger(__name__)


class _SdkFinishNoise(logging.Filter):
    """The SDK's ``finish()`` gathers the listen task it has just cancelled
    and logs the ``CancelledError`` at ERROR on every close; the SDK swallows
    the error either way (a cancellation of the caller's own task lands in
    the same branch)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not str(record.msg).startswith("tasks cancelled error")


def _route_sdk_logger(connection) -> None:
    """The SDK builds each connection's logger outside the logging registry
    (so ``logging.getLogger(name)`` never reaches it) with its own bare
    stderr handler and the root as parent: every record printed twice. Its
    handlers go, so a record reaches the process's own handlers once, with
    the logger's name; the finish noise is dropped and every other record
    (a failed send, a refused close) stays."""
    sdk_logger = getattr(connection, "_logger", None)
    if not isinstance(sdk_logger, logging.Logger):
        return
    for handler in list(sdk_logger.handlers):
        sdk_logger.removeHandler(handler)
    if not any(isinstance(f, _SdkFinishNoise) for f in sdk_logger.filters):
        sdk_logger.addFilter(_SdkFinishNoise())

# Minimum interval between KeepAlive messages (seconds). Deepgram's docs suggest
# sending KeepAlive to prevent idle timeout; once every 5s is plenty.
_KEEPALIVE_INTERVAL_S = 5.0

# Deepgram prerecorded pricing: nova-3 ≈ $0.0043/min = $0.0000717/sec
# (verified against deepgram.com/pricing 2026-06-03). Admin may override per
# instance via advanced.rate_override_per_unit.
_COST_PER_SECOND = 0.0000717

# Default Deepgram model (streaming + prerecorded; ``audio.providers.model_succession``).
# nova-3 is the current flagship general model; admin-overridable per provider
# via ``advanced.model_id`` (set a newer/specialised one — nova-3-medical,
# nova-2-phonecall, … — without a code change). Applies to both the live
# socket and file transcription.
_DEFAULT_MODEL = DEFAULT_MODELS[("stt", "deepgram")]["model_id"]

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
    supports, else strip to the base subtag. Empty or ``auto`` → ``multi``
    (the code-switching stream: ten languages, Greek not among them; the
    dictation relay's fallback when the local identifier is unavailable)."""
    if not tag or tag == "auto":
        return "multi"
    if tag in _DG_REGIONAL or "-" not in tag:
        return tag
    return tag.split("-", 1)[0]


# The languages a nova-3 stream can be opened in (the vendor's documented
# set); asked when the language rule opens a candidate stream.
_STREAMABLE = frozenset({
    "bg", "ca", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr", "hi", "hu",
    "id", "it", "ja", "ko", "lt", "lv", "ms", "nl", "no", "pl", "pt", "ro", "ru",
    "sk", "sv", "th", "tr", "uk", "vi", "zh",
})


# Punctuation smart_format may add/alter between an interim and its final —
# includes the Greek question mark (;) and ano teleia (·).
_WORD_STRIP = ".,;:!?·»«\"'()[]{}"


def _norm_word(word: str) -> str:
    # An interim and its final can differ by an accent alone (the Greek
    # tonos lands on the final): the same word for the prefix rule.
    bare = unicodedata.normalize("NFD", word.strip(_WORD_STRIP).casefold())
    return "".join(c for c in bare if unicodedata.category(c) != "Mn")


def _norm_words(text: str) -> list[str]:
    """Casefolded, punctuation-stripped tokens, for prefix comparisons between
    interim and final variants of the same audio window (dictation mode)."""
    return [t for t in (_norm_word(w) for w in text.split()) if t]


def _result_words(alt) -> list[Word]:
    """Timed words of a live result (punctuated form, seconds from stream
    start). Empty when the result carries no usable timings — callers then
    fall back to text-only rules. Entries that normalize to nothing (bare
    punctuation) are dropped so the list aligns 1:1 with ``_norm_words`` of
    the transcript."""
    out: list[Word] = []
    for w in (getattr(alt, "words", None) or []):
        try:
            text = getattr(w, "punctuated_word", None) or w.word
            if _norm_word(text):
                confidence = getattr(w, "confidence", None)
                out.append(Word(word=text, start=float(w.start), end=float(w.end),
                                confidence=float(confidence) if confidence is not None else None))
        except (AttributeError, TypeError, ValueError):
            return []
    return out


def _evidence(alt, time_base: float) -> tuple[int, float, tuple[tuple[float, float, float], ...]]:
    """A live result's word count, mean word confidence (the result's own
    confidence when its words carry none; 0 for an empty result) and each
    timed word's ``(start, end, confidence)`` on the provider's clock."""
    confidences: list[float] = []
    marks: list[tuple[float, float, float]] = []
    for w in (getattr(alt, "words", None) or []):
        try:
            confidence = float(w.confidence)
        except (AttributeError, TypeError, ValueError):
            continue
        confidences.append(confidence)
        with contextlib.suppress(AttributeError, TypeError, ValueError):
            marks.append((time_base + float(w.start), time_base + float(w.end), confidence))
    if confidences:
        return len(confidences), sum(confidences) / len(confidences), tuple(marks) if len(marks) == len(confidences) else ()
    count = len((getattr(alt, "transcript", "") or "").split())
    if not count:
        return 0, 0.0, ()
    try:
        return count, float(getattr(alt, "confidence", 0.0) or 0.0), ()
    except (TypeError, ValueError):
        return count, 0.0, ()


class DeepgramSTT(STTProvider):
    """Streaming + prerecorded speech-to-text via Deepgram."""

    capabilities = STTCapabilities(
        supports_streaming=True,
        supports_transcribe_file=True,
        supports_endpointing=True,
        supports_word_timestamps=True,
        is_local=False,
        reports_confidence=True,
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
        # Timed words behind the two interims above (dictation mode reads the
        # timeline to tell consumed audio from audio the next window re-delivers).
        self._latest_interim_words: list[Word] = []
        self._last_interim_sent_words: list[Word] = []
        # A promoted word that straddled a final's boundary: (normalized word,
        # end). The next window may re-recognize it from its tail audio — that
        # copy is stripped when it leads the window's results.
        self._promoted_edge: tuple[str, float] | None = None
        self._interim_results = False  # mode last requested in start() (kept on reconnect)
        self._endpointing_override: int | None = None  # per-connection override (kept on reconnect)
        self._fatal_error: str | None = None  # last Error event, surfaced once via pop_fatal_error
        # Rate actually negotiated by the last start() — reconnects MUST reuse
        # it: falling back to the constructor default decodes a 16 kHz stream
        # as 8 kHz → permanent garbage transcripts (live duplex bug, audit F5).
        self._active_rate = sample_rate
        self._send_skips = 0  # closed-connection sends since last open (one WARNING per incident)
        # Every connection's result times start at zero: the audio clock at
        # the connection's start puts its results on the provider's clock.
        self._time_base = 0.0
        # Dictation's second listen (``relisten.py``): the audio this
        # connection accepted, the windows held for a re-listen, the words
        # the interims showed per window (by time), and what the last
        # display sent showed of the held windows.
        self._relisten: Relisten | None = None
        # A call's second listen (``relisten.TurnRelisten``): the audio this
        # connection accepted and the live finals of the turn.
        self._turn_relisten: TurnRelisten | None = None
        self._finish_wait = RELISTEN_FINISH_WAIT_S
        self._seen: list[Word] = []
        self._held_shown = ""
        self._last_display_sent = ""

    # ── Factory / metadata ─────────────────────────────────────────

    @classmethod
    def from_row(cls, row: dict, resolver: CredentialResolver) -> DeepgramSTT:
        advanced = row.get("advanced") or {}
        return cls(
            api_key=resolver(row.get("credential_key", "")),
            endpointing_ms=int(advanced.get("call_endpointing_ms", 200)),
            vad_silence_offset_ms=int(advanced.get("vad_silence_offset_ms", 50)),
            model=cls.live_model_id(advanced.get("model_id")) or _DEFAULT_MODEL,
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
    def default_model_ids(cls) -> dict[str, str]:
        return dict(DEFAULT_MODELS[("stt", "deepgram")])

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

    def enable_dictation_mode(self) -> None:
        super().enable_dictation_mode()
        if self._relisten is None:
            self._relisten = Relisten(transcribe=self._relisten_transcribe, release=self._release_final,
                                      norm=_norm_word, rate=self._active_rate,
                                      stream_seconds=self._budget_stream_seconds, spent=self._budget_spent)

    def enable_turn_relisten(self) -> None:
        if self._turn_relisten is None and not self.dictation_mode:
            self._turn_relisten = TurnRelisten(transcribe=self._relisten_transcribe, norm=_norm_word,
                                               rate=self._active_rate, stream_seconds=self._budget_stream_seconds,
                                               spent=self._budget_spent)

    async def complete_turn(self, text: str, bound: float) -> str:
        if self._turn_relisten is None:
            return text
        return await self._turn_relisten.complete(text, bound)

    def turn_taken(self) -> None:
        if self._turn_relisten is not None:
            self._turn_relisten.take()

    async def release_wordless_turn(self, timeout: float) -> bool:
        if self._turn_relisten is None or not self._turn_relisten.wordless:
            return False
        text = await self._turn_relisten.wait_wordless(timeout)
        if not text:
            return False
        self._log_transcript("Deepgram call re-listen final", text)
        self._release_final(text)
        return True

    def _budget_stream_seconds(self) -> float:
        return self.dictation_seconds() if self.dictation_seconds is not None else self.audio_offered_s

    def _budget_spent(self) -> float:
        return self.dictation_relistened() if self.dictation_relistened is not None else self.relisten_seconds

    async def _relisten_transcribe(self, audio: bytes) -> TranscriptResult:
        return await self.transcribe_file(audio, language=getattr(self, "_label", None))

    @property
    def relisten_seconds(self) -> float:
        dictation = self._relisten.seconds if self._relisten is not None else 0.0
        return dictation + (self._turn_relisten.seconds if self._turn_relisten is not None else 0.0)

    def bound_finish(self, seconds: float) -> None:
        self._finish_wait = max(0.0, min(RELISTEN_FINISH_WAIT_S, seconds))

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
        confidence = 0.0
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
                try:
                    confidence = float(getattr(channel, "language_confidence", None) or 0.0)
                except (TypeError, ValueError):
                    confidence = 0.0
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
            logger.warning("Deepgram prerecorded parse error: %s", e)

        self._log_transcript("Deepgram file", text)
        return TranscriptResult(
            text=text, language=lang, audio_seconds=duration, words=words, provider_used="deepgram",
            language_confidence=confidence,
        )

    def streams_language(self, language: str) -> bool:
        return (language or "").split("-", 1)[0].lower() in _STREAMABLE

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
        self._label = language
        self._active_rate = rate
        self._send_skips = 0
        self._latest_interim = ""
        self._last_interim_sent = ""
        self._latest_interim_words = []
        self._last_interim_sent_words = []
        self._promoted_edge = None
        self._fatal_error = None
        self._interim_results = interim_results
        self._endpointing_override = endpointing_ms
        self._transcript_queue = asyncio.Queue()
        self._seen = []
        self._held_shown = ""
        self._last_display_sent = ""
        self._finish_wait = RELISTEN_FINISH_WAIT_S
        if self._relisten is not None:
            self._relisten.reset(rate)
            self._relisten.label = language
        if self._turn_relisten is not None:
            self._turn_relisten.reset(rate)
            self._turn_relisten.label = language
        self._transcript_ready.clear()
        self._connection_gen += 1
        gen = self._connection_gen
        self._connection = self._client.listen.asyncwebsocket.v("1")
        _route_sdk_logger(self._connection)

        # Bind the current generation so stale callbacks are ignored — a
        # replaced socket's dying gasp (net0001 after a mid-call reconnect)
        # must not surface as a pipeline "STT fatal" for the LIVE connection,
        # and a late result of the old stream must not land in the new queue
        # (the reconnect in ``ensure_alive``).
        self._connection.on(
            LiveTranscriptionEvents.Transcript,
            lambda _conn, result, _gen=gen, **kw: self._on_transcript(_conn, result, _gen=_gen, **kw),
        )
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
        # The connection's time zero is the first byte it receives: the
        # frames offered while it connected were skipped, not sent.
        self._time_base = self.audio_offered_s
        self._is_open = True
        logger.info("Deepgram STT connection opened")

    async def send_audio(self, audio_bytes: bytes) -> None:
        """Send raw PCM audio to Deepgram for transcription."""
        if self._connection_gen:
            self._count_offered(len(audio_bytes), self._active_rate, self._channels)
        if self._connection and self._is_open:
            try:
                accepted = await self._connection.send(audio_bytes)
            except Exception as e:  # noqa: BLE001  # the Deepgram SDK raises whatever its socket does
                logger.error("Deepgram send error: %s", e)
            else:
                if accepted is True:
                    for second in (self._relisten, self._turn_relisten):
                        if second is not None and second.keeps_audio:
                            second.ring.append(audio_bytes)
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
        except Exception as e:  # noqa: BLE001  # the Deepgram SDK raises whatever its socket does
            logger.warning("Deepgram KeepAlive error: %s", e)

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
        if self._relisten is not None:
            self._relisten.cancel()
        if self._turn_relisten is not None:
            self._turn_relisten.take()
        # A discarded utterance's live partial must go with it — a stale
        # interim that never finalizes reads as permanent speech "evidence"
        # to the barge-in pause monitor (audit A5, 2026-08-24).
        self._latest_interim = ""
        self._latest_interim_words = []
        self._promoted_edge = None
        if discarded:
            logger.info("STT queue cleared (%s items discarded)", discarded)

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
        fresh = bool(txt) and txt != self._last_interim_sent
        if fresh and self.dictation_mode and self._last_interim_sent:
            new, old = _norm_words(txt), _norm_words(self._last_interim_sent)
            if len(new) < len(old) and old[: len(new)] == new:
                fresh = False
        if fresh:
            self._last_interim_sent = txt
            self._last_interim_sent_words = list(self._latest_interim_words)
        held = self._relisten.held_text() if self._relisten is not None else ""
        if not held and not self._held_shown:
            if fresh:
                self._last_display_sent = txt
                return txt
            return None
        # A window held for a re-listen stays on screen ahead of the live
        # partial (its words, and the words shown for its gap) until it is
        # released as a final; a release sends the display again.
        self._held_shown = held
        display = " ".join(p for p in (held, self._last_interim_sent) if p)
        if not display or display == self._last_display_sent:
            return None
        self._last_display_sent = display
        return display

    @property
    def display_interim(self) -> str:
        """The live partial as the composer shows it: the held windows'
        text ahead of it."""
        held = self._relisten.held_text() if self._relisten is not None else ""
        return " ".join(p for p in (held, self._latest_interim) if p)

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
            except Exception as e:  # noqa: BLE001  # the Deepgram SDK raises whatever its socket does
                logger.warning("Deepgram finalize error: %s", e)

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
        # The bound counts from here: the vendor's own finish takes part of it.
        deadline = time.monotonic() + self._finish_wait
        self._finish_wait = RELISTEN_FINISH_WAIT_S
        if self._connection and self._is_open:
            try:
                await self._connection.finish()
            except Exception as e:  # noqa: BLE001  # the Deepgram SDK raises whatever its socket does
                logger.error("Deepgram finish error: %s", e)

        self._is_open = False
        if self._relisten is not None:
            # The windows held for a re-listen go out first, in order (a
            # re-listen still out at the bound goes with its shown words).
            await self._relisten.settle(max(0.0, deadline - time.monotonic()))

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
            # for exactly this trailing final (10 s, more on a slow uplink).
            # The stream is over, so
            # nothing can re-deliver it: the whole shown text goes.
            leftover, _ = self._shown_interim()
            if leftover:
                parts.append(leftover)
                self._set_interim("", [])

        transcript = " ".join(parts).strip() if parts else None
        if transcript:
            self._log_transcript("STT transcript", transcript)
        return transcript

    async def close(self) -> None:
        """Close the connection without waiting for final transcript."""
        self._is_open = False
        if self._relisten is not None:
            # The vendor's last results arrive during its finish: they are
            # released as they are, never listened to again.
            self._relisten.close()
        if self._turn_relisten is not None:
            self._turn_relisten.close()
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
                "Deepgram STT reconnected mid-call%s",
                f" ({skipped} frames dropped while dead)" if skipped else "",
            )
            return True
        except Exception as e:  # noqa: BLE001  # the Deepgram SDK raises whatever its socket does
            logger.warning("Deepgram STT reconnect failed: %s", e)
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

    def _shown_interim(self) -> tuple[str, list[Word]]:
        """The richer of the live interim and the last one actually sent to the
        client (text + its timed words) — dictation paints whichever was
        delivered last, and a not-yet-popped newer interim only ever shows
        MORE. Used by dictation mode to decide what "the user already saw"
        for the never-drop guarantees."""
        latest, sent = self._latest_interim, self._last_interim_sent
        if len(_norm_words(sent)) > len(_norm_words(latest)):
            return sent, self._last_interim_sent_words
        return latest, self._latest_interim_words

    def _set_interim(self, text: str, words: list[Word]) -> None:
        """Replace the live interim AND the sent baseline, so pop_interim
        delivers ``text`` next (or nothing, when empty)."""
        self._latest_interim, self._latest_interim_words = text, words
        self._last_interim_sent, self._last_interim_sent_words = "", []

    @staticmethod
    def _split_shown(shown: str, words: list[Word], final_end: float) -> tuple[list[Word], list[Word]]:
        """Split the painted interim into the words whose audio a final ending
        at ``final_end`` consumed and the words the next window re-delivers.

        Deepgram finals partition the stream: a final covers
        ``[start, start + duration)`` and the next window starts exactly at
        that end, re-recognizing everything after it — so interim words past
        the boundary come back on their own and committing them now would
        print them twice. A word that BEGAN inside the range but ends past it
        is the one case Deepgram drops (the boundary cuts it; the next window
        sees only its tail): that one counts as consumed, and
        ``_promoted_edge`` strips the tail copy if the next window recognizes
        it anyway. Without usable timings (a result whose word list does not
        line up with its transcript) everything shown counts as consumed —
        the pre-timing behaviour, never taken on Deepgram results.
        """
        if not words or len(words) != len(_norm_words(shown)):
            return [Word(word=w, start=0.0, end=0.0) for w in shown.split()], []
        consumed = [w for w in words if w.start < final_end]
        return consumed, words[len(consumed):]

    def _note_promoted_edge(self, consumed: list[Word], final_end: float) -> None:
        last = consumed[-1] if consumed else None
        if last is not None and last.end > final_end:
            self._promoted_edge = (_norm_word(last.word), last.end)

    def _strip_promoted_edge(self, transcript: str, words: list[Word]) -> tuple[str, list[Word]]:
        """Drop a leading word that is the re-recognized tail of a word already
        committed across the previous final's boundary (same normalized word,
        starting before that word ended — a genuine repetition starts after)."""
        edge = self._promoted_edge
        if edge and words and _norm_word(words[0].word) == edge[0] and words[0].start < edge[1]:
            words = words[1:]
            transcript = " ".join(w.word for w in words)
        return transcript, words

    async def _commit(self, label: str, text: str, *, partial_final: str | None = None,
                      window: tuple[float, float] | None = None, words: list[Word] | None = None) -> None:
        self._log_transcript(label, text)
        if self._relisten is not None and window is not None:
            self._hold_window(window, text, words or [])
        else:
            await self._transcript_queue.put(text)
            self._transcript_ready.set()
        if partial_final:
            self._emit_partial_final(partial_final)

    def _release_final(self, text: str) -> None:
        self._transcript_queue.put_nowait(text)
        self._transcript_ready.set()

    def _hold_window(self, window: tuple[float, float], text: str, words: list[Word]) -> None:
        """A dictation final through the re-listen's FIFO (``relisten.py``):
        released at once unless a stretch of its window no committed word
        covers may hold speech (never for untimed words, the ``multi``
        stream, or a stream the gate keeps out)."""
        start, end = window
        shown = [w for w in self._seen if start - 0.25 <= w.start < end]
        gate = self.relisten_gate
        timed = (bool(words) or not text) and all(w.end > w.start for w in words)
        enabled = getattr(self, "_label", "") != "multi" and timed and (gate is None or gate())
        self._relisten.window(start=start, end=end, text=text, words=words, shown=shown, enabled=enabled)

    def _note_turn_window(self, start: float, end: float, text: str, words: list[Word]) -> None:
        """A call's live final for its turn's second listen (never the
        ``multi`` stream, never one the gate keeps out)."""
        if self._turn_relisten is None:
            return
        gate = self.relisten_gate
        enabled = getattr(self, "_label", "") != "multi" and (gate is None or gate())
        self._turn_relisten.window(start=start, end=end, text=text, words=words, enabled=enabled)

    def _note_seen(self, words: list[Word]) -> None:
        """An interim's timed words replace the words seen over the time they
        cover (a word by its middle: a re-timed word is the same word); what
        an earlier interim showed before them stays."""
        if not words:
            return
        lo, hi = words[0].start - 0.05, words[-1].end
        kept = [w for w in self._seen if not lo <= (w.start + w.end) / 2 < hi]
        self._seen = sorted([*kept, *words], key=lambda w: w.start)

    async def _on_transcript(self, _conn, result, _gen: int | None = None, **kwargs) -> None:
        """Handle transcript events from Deepgram."""
        if _gen is not None and _gen != self._connection_gen:
            logger.debug("Deepgram transcript on stale gen=%s (current=%s) — ignoring", _gen, self._connection_gen)
            return
        try:
            alt = result.channel.alternatives[0]
            transcript = alt.transcript
            is_final = result.is_final
            words = _result_words(alt) if self.dictation_mode or self._turn_relisten is not None else []
            result_start = float(getattr(result, "start", 0.0) or 0.0)
            final_end = result_start + float(getattr(result, "duration", 0.0) or 0.0)
            count, confidence, marks = _evidence(alt, self._time_base)
            heard = Heard(start=self._time_base + result_start, end=self._time_base + final_end,
                          words=count, confidence=confidence, final=bool(is_final), marks=marks)
            if is_final:
                self._heard_final(heard)
            else:
                self._heard_interim(heard)
            if self._promoted_edge:
                transcript, words = self._strip_promoted_edge(transcript, words)
                if is_final:
                    self._promoted_edge = None  # re-delivery only happens in the window right after

            if transcript and is_final:
                self._last_result_at = time.monotonic()
                committed = transcript
                if self.dictation_mode:
                    # Never drop painted words: of the shown interim, the
                    # words past this final's end stay live (the next window
                    # re-delivers them, and leaving them painted avoids a
                    # blink); the words its audio consumed are gone for good
                    # unless committed here — done when they extend the final
                    # (normalized word-prefix match), and for a reworded
                    # final, for the painted words after its own last word:
                    # the rewording covers only the head it transcribed, the
                    # window past that head never comes again.
                    shown, shown_words = self._shown_interim()
                    consumed, rest = self._split_shown(shown, shown_words, final_end)
                    fin = _norm_words(transcript)
                    seen = [_norm_word(w.word) for w in consumed]
                    timed = bool(shown_words) and len(shown_words) == len(_norm_words(shown))
                    extra: list[Word] = []
                    shape = "short"
                    if len(seen) > len(fin) and seen[: len(fin)] == fin:
                        extra = consumed[len(fin):]
                    elif timed and words:
                        covered = words[-1].end
                        extra = [w for w in consumed if w.start >= covered]
                        shape = "reworded short"
                    if extra:
                        committed = transcript + " " + " ".join(w.word for w in extra)
                        self._note_promoted_edge(extra, final_end)
                        logger.info(
                            "Deepgram %s final (%s) — %d shown word(s) committed with it, "
                            "%d left for the next window", shape, getattr(self, "_label", ""),
                            len(extra), len(rest),
                        )
                    self._set_interim(" ".join(w.word for w in rest), rest)
                else:
                    # The final covers the partial's audio: a drain that finds
                    # the queue empty falls back to ``latest_interim``, and a
                    # partial left here was appended once per drain (the
                    # repeated words of every call). Cleared before the
                    # commit, whose push starts the duplex sweep.
                    self._latest_interim, self._latest_interim_words = "", []
                if self.dictation_mode:
                    await self._commit("Deepgram final", committed, partial_final=transcript,
                                       window=(result_start, final_end), words=[*words, *extra])
                    self._seen = [w for w in self._seen if w.start >= final_end]
                else:
                    self._note_turn_window(result_start, final_end, committed, words)
                    await self._commit("Deepgram final", committed, partial_final=transcript)
            elif transcript and not is_final:
                self._last_result_at = time.monotonic()
                self._latest_interim, self._latest_interim_words = transcript, words
                if self.dictation_mode:
                    self._note_seen(words)
                logger.debug("Deepgram interim: \"%s\"", transcript)
            elif is_final and not transcript:
                shown, shown_words = self._shown_interim() if self.dictation_mode else ("", [])
                if not shown:
                    logger.debug("Deepgram final: (empty)")
                    self._note_turn_window(result_start, final_end, "", [])
                    if self._relisten is not None:
                        # A window answered empty with nothing shown: it may
                        # still hold speech the second listen recovers.
                        self._hold_window((result_start, final_end), "", [])
                        self._seen = [w for w in self._seen if w.start >= final_end]
                    return
                # An empty final orphans the painted interim: the base the
                # composer commits onto never advances, and the next window's
                # interim REPLACES everything shown (the Greek vanishing-words
                # bug, 2026-09-01). Promote the part whose audio this final
                # consumed; the rest stays live for the next window to
                # re-deliver. Duplex/call pipelines never take this path
                # (dictation_mode is chat-relay-only): a promoted noise
                # interim would be false barge-in evidence there.
                consumed, rest = self._split_shown(shown, shown_words, final_end)
                if consumed:
                    self._note_promoted_edge(consumed, final_end)
                    self._set_interim(" ".join(w.word for w in rest), rest)
                    await self._commit("Deepgram empty final — committing shown interim",
                                       " ".join(w.word for w in consumed),
                                       window=(result_start, final_end), words=consumed)
                else:
                    logger.debug("Deepgram final: (empty, shown interim all past its end)")
                    if self._relisten is not None:
                        self._hold_window((result_start, final_end), "", [])
                self._seen = [w for w in self._seen if w.start >= final_end]
        except (IndexError, AttributeError) as e:
            logger.warning("Failed to parse Deepgram result: %s", e)

    async def _on_error(self, _conn, error, _gen: int = 0, **kwargs) -> None:
        """Handle errors from Deepgram (generation-guarded like _on_close)."""
        if _gen != self._connection_gen:
            logger.info(
                "Deepgram error on stale gen=%s "
                "(current=%s) — ignoring: %s",
                _gen, self._connection_gen, str(error)[:120]
            )
            return
        logger.error("Deepgram error: %s", error)
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
                "Deepgram connection closed (stale gen=%s, "
                "current=%s) — ignoring", _gen, self._connection_gen
            )

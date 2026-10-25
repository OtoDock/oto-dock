"""Abstract base class + shared types for STT providers.

Providers implement the core streaming methods (start, send_audio, …) and the
lifecycle/echo hooks so the call pipeline stays generic. The streaming surface
serves the telephony call pipeline; ``transcribe_file`` is the batch/prerecorded
path (file → text with word timings), plus capability/billing metadata and the
``from_row`` factory used by the registry.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from audio.capabilities import BillingUnit, STTCapabilities
from audio.log_policy import log_transcript
from audio.providers.credential_resolver import CredentialResolver
from audio.providers.model_succession import successor_of

__all__ = ["HEARD_KEEP_S", "Heard", "STTProvider", "TranscriptResult", "Word"]

#: The finals the ``heard`` ledger keeps, in seconds of the provider's audio
#: clock behind the newest audio.
HEARD_KEEP_S = 30.0


@dataclass
class Word:
    """One word with start/end offsets in seconds (for SRT generation)."""

    word: str
    start: float
    end: float
    confidence: float | None = None


@dataclass(frozen=True)
class Heard:
    """One decoded result as evidence: numbers only, never the text.
    An empty result is an entry with ``words == 0``: it says the stream heard
    this span and found nothing, which is not the same as not having
    answered yet. ``start``, ``end`` and the marks are on the provider's
    audio clock (``STTProvider.audio_offered_s``). ``marks`` places each
    word, ``(start, end, confidence)``, when the result times them: a
    result's window grows over a whole utterance, so the words say where in
    it they were heard; without marks the words count as spread evenly over
    the entry."""

    start: float
    end: float
    words: int
    confidence: float   # the words' mean; the result's own when its words carry none; 0 when words == 0
    final: bool
    marks: tuple[tuple[float, float, float], ...] = ()


@dataclass
class TranscriptResult:
    """Result of a batch (prerecorded) file transcription."""

    text: str
    language: str
    audio_seconds: float            # decoded duration; the billing multiplier
    words: list[Word] = field(default_factory=list)
    provider_used: str = ""         # provider_name that served it (e.g. "deepgram")
    # The engine's confidence in ``language`` when it identified it (0.0 when
    # the language was given or the engine reports none).
    language_confidence: float = 0.0


class STTProvider(ABC):
    """Interface that all STT providers must implement.

    The pipeline interacts with STT exclusively through this interface. The
    lifecycle hooks (feed_during_tts, on_tts_finished, …) let each provider
    describe how it wants to be treated during TTS playback, opening sequences,
    and barge-in — without the pipeline needing provider-specific logic.
    """

    # Subclasses MUST override `capabilities`. Billing classmethods + `is_free_tier`
    # default to "free, unknown unit"; override where the provider actually bills.
    capabilities: ClassVar[STTCapabilities] = STTCapabilities()
    is_free_tier: ClassVar[bool] = False
    # True on not-yet-implemented placeholder classes (importable, __init__ raises).
    # The admin "Add provider" menu hides stubs; flip when the implementation lands.
    is_stub: ClassVar[bool] = False
    # Retired model ids and their successors (``audio.providers.model_succession``):
    # a stored id resolves through ``live_model_id`` at every build, and the
    # proxy's boot walk rewrites the rows.
    model_successors: ClassVar[dict[str, str]] = {}

    _is_open: bool = False

    @classmethod
    def live_model_id(cls, model_id: str | None) -> str:
        """A stored model id as it runs today: the end of its successor chain,
        else the id itself ("" when the row stores none)."""
        return successor_of(cls.model_successors, model_id or "")

    @classmethod
    def default_model_ids(cls) -> dict[str, str]:
        """The model ids that run when the row stores none (the admin pill's
        placeholders). Never written to a row: ``default_advanced_settings``
        carries no model id, so a later default moves every such row."""
        return {}

    # Optional push hook: the receive loop invokes this with each FINAL
    # transcript at the moment it is queued for ``drain_transcript``. Streaming
    # UIs that render the ACCUMULATING utterance live need the push — during
    # continuous speech, mid-utterance finals otherwise sit invisible in the
    # queue until dispatch, so a polled ``latest_interim`` overlay alone resets
    # the caption to each newest phrase. Consumers assign a plain sync callable
    # (the duplex session forwards it as a caption frame); providers call it
    # via ``_emit_partial_final`` so a raising callback can never kill the
    # receive loop. Providers that implement streaming SHOULD emit this
    # wherever they enqueue a final.
    on_partial_final: Callable[[str], None] | None = None

    def _emit_partial_final(self, text: str) -> None:
        """Invoke ``on_partial_final`` defensively (see attribute docs)."""
        cb = self.on_partial_final
        if cb is None or not text:
            return
        try:
            cb(text)
        except Exception:  # noqa: BLE001  # a consumer's partial-final callback must not break the stream
            logging.getLogger(type(self).__module__).debug(
                "on_partial_final callback failed", exc_info=True)

    # Dictation mode — set ONLY by the chat-input STT relay (ws/audio.py).
    # The composer paints interims as if committed, so a dictation provider
    # must never let painted words die with an empty or truncated final
    # (Deepgram does both on some languages) — it promotes the shown interim
    # into the transcript queue instead. The call/duplex pipelines drain the
    # SAME queue and use pending finals as barge-in speech evidence, so they
    # must NOT get promotions (a noise interim would become dispatched turn
    # text / false barge-in there) — hence opt-in per instance, which also
    # survives ensure_alive() reconnects.
    dictation_mode: bool = False

    def enable_dictation_mode(self) -> None:
        """Opt this instance into dictation semantics (see attribute docs)."""
        self.dictation_mode = True

    # Dictation's second listen (Deepgram's ``relisten.py``): a provider that
    # re-listens the speech a live window left without words asks this gate
    # at every final (None: always); the language streams let only the
    # stream the person sees, or the one about to be, re-listen. The batch
    # seconds it bought are billed audio on top of the stream's.
    relisten_gate: Callable[[], bool] | None = None
    # The second listen's budget counts the whole dictation: the language
    # streams point every stream at the dictation's stream seconds and at
    # the batch seconds all its streams bought (None: the stream's own).
    dictation_seconds: Callable[[], float] | None = None
    dictation_relistened: Callable[[], float] | None = None

    @property
    def relisten_seconds(self) -> float:
        return 0.0

    def bound_finish(self, seconds: float) -> None:
        """The next ``finish()`` waits at most ``seconds`` for work the
        provider still owes (a held window's re-listen). Default: nothing
        to bound."""
        return

    # A call's second listen (Deepgram's ``relisten.TurnRelisten``): the
    # phone pipeline turns it on for every call STT; a provider without one
    # keeps these no-ops.
    def enable_turn_relisten(self) -> None:
        """Listen again, in the background, to a live final that came back
        with no word over speech (calls and live voice). Default: never."""
        return

    async def complete_turn(self, text: str, bound: float) -> str:
        """The turn's ``text`` with the words its second listen recovered,
        waiting at most ``bound`` for a re-listen still out. Default: the
        text as it is."""
        return text

    def turn_taken(self) -> None:
        """The turn went out: what its second listen still owes is dropped."""
        return

    async def release_wordless_turn(self, timeout: float) -> bool:
        """A turn whose every live final came back with no word: wait at most
        ``timeout`` for its second listen and queue the words it recovered as
        the turn's final. True when something was queued. Default: False."""
        return False

    @property
    def display_interim(self) -> str:
        """The live partial as the dictation composer shows it (a provider
        that holds windows puts their text ahead of it)."""
        return self.latest_interim

    # ── Construction from an audio_providers row ───────────────────

    @classmethod
    @abstractmethod
    def from_row(cls, row: dict, resolver: CredentialResolver) -> STTProvider:
        """Build an instance from an ``audio_providers`` row + a credential resolver.

        Keeps the row→kwargs mapping next to the provider (each provider knows
        which ``advanced`` keys it reads). Still pure — receives data + a
        callable, never touches the DB itself.
        """

    # ── Capability / billing metadata ──────────────────────────────

    @classmethod
    def billing_unit(cls) -> BillingUnit:
        """Unit this provider bills on. STT is usually per-second."""
        return "second"

    @classmethod
    def cost_per_unit(cls) -> float:
        """USD per `billing_unit`. Class default; admin may override per-instance."""
        return 0.0

    @classmethod
    def default_advanced_settings(cls) -> dict[str, Any]:
        """The `advanced` JSONB defaults shown in the admin pill's Restore button."""
        return {}

    @classmethod
    def validate_advanced(cls, settings: dict) -> dict[str, str]:
        """Validate an `advanced` settings dict.

        Returns ``{}`` on success, or ``{field_name: error_message}`` on failure
        (surfaced as field-level errors by the admin API).
        """
        return {}

    # ── Batch / prerecorded transcription (file → text) ────────────

    async def transcribe_file(self, audio: bytes, *, language: str | None = None) -> TranscriptResult:
        """Transcribe a complete audio file. Override in providers that support it.

        Distinct from the streaming surface below — used by the transcribe
        endpoint / transcribe-mcp, with word-level timings for SRT. Default:
        unsupported (capabilities.supports_transcribe_file stays False).
        """
        raise NotImplementedError(f"{type(self).__name__} does not support file transcription")

    # ── The languages a stream can be opened in ────────────────────

    def streams_language(self, language: str) -> bool:
        """Whether a stream can be opened in ``language`` (a base code):
        asked when the language rule opens a candidate stream. Default:
        every language."""
        return True

    # ── Core streaming methods ─────────────────────────────────────

    @property
    @abstractmethod
    def endpointing_ms(self) -> int:
        """Return the provider's endpointing delay in milliseconds."""

    @abstractmethod
    async def start(
        self, language: str = "multi", sample_rate: int | None = None, interim_results: bool = False,
        endpointing_ms: int | None = None,
    ) -> None:
        """Open a streaming connection to the STT provider.

        ``sample_rate`` (Hz) overrides the provider's default input rate for this
        connection — the chat mic streams 16 kHz, while the call/telephony
        default is 8 kHz. ``None`` keeps the provider's configured default.

        ``interim_results`` requests live partial transcripts (the chat mic shows
        text as you speak). Providers that support it expose them via
        ``pop_interim()``; the call pipeline leaves it off.

        ``endpointing_ms`` overrides the provider's configured endpointing delay
        for this connection. Chat dictation tolerates longer pauses than calls,
        so the chat surface passes its own value (``advanced.chat_endpointing_ms``)
        and call-latency tuning never tightens dictation. ``None`` keeps the
        provider's configured (call) value.
        """

    @abstractmethod
    async def send_audio(self, audio_bytes: bytes) -> None:
        """Send raw PCM audio for transcription."""

    @abstractmethod
    def drain_transcript(self) -> str | None:
        """Return any available transcripts without blocking. Non-blocking."""

    @abstractmethod
    async def wait_for_transcript(self, timeout: float = 1.0) -> str | None:
        """Wait up to *timeout* seconds for a finalized transcript."""

    @abstractmethod
    def clear_queue(self) -> None:
        """Discard all pending transcripts."""

    @abstractmethod
    async def finish(self) -> str | None:
        """Signal end of audio and return any final transcript."""

    async def force_endpoint(self) -> None:
        """Force the provider to finalize any pending transcript immediately.

        Called at SPEECH_END so providers with slow server-side endpointing
        flush their buffered tokens without waiting for the full endpoint
        delay. On a provider whose capabilities say ``reports_confidence``
        it may also come while speech goes on (a language candidate that is
        behind gets one once it has caught up, or at the hold's bound,
        ``stt/streams.py``), so a final mid-speech must keep every word.
        Default: no-op.
        """
        return

    @property
    def latest_interim(self) -> str:
        """Most recent non-final partial transcript, or ``""`` when the provider
        doesn't stream interims / none is pending. The pipeline uses it as a
        fallback when a forced finalize fails to deliver an is_final. Default:
        ``""`` (provider streams no interims).
        """
        return ""

    @property
    def speech_evidence(self) -> bool:
        """Any sign the speech being heard is producing text: a live partial
        or a pending final. The barge-in pause and the turn timer's phantom
        check read this (never the text), so a provider made of several
        streams answers over all of them."""
        return bool(self.latest_interim) or self.has_pending_finals

    @property
    def interim_evidence(self) -> str:
        """The live partials as one string, for a monitor that watches
        whether they EVOLVE (an evolving partial is a speaker still
        talking); a provider of several streams joins theirs."""
        return self.latest_interim

    @property
    def has_pending_finals(self) -> bool:
        """Finalized-but-undrained transcript(s) waiting in the provider queue.

        Speech evidence the barge-in pause checks MUST include: providers
        finalize phrases DURING continuous speech (endpointing on a natural
        comma pause), which CLEARS ``latest_interim`` — a real speaker with an
        early-finalized phrase otherwise looks exactly like phantom VAD
        (live-hit 2026-08-24: the confirm backstop resumed the reply over a
        mid-sentence caller). Default: nothing pending.
        """
        return False

    @property
    def has_drainable_finals(self) -> bool:
        """Finals the next ``drain_transcript`` returns as text. The same as
        ``has_pending_finals`` for one stream; a provider of several
        streams answers for the one its drains read, so a consumer that
        commits on a drain never waits on another stream's final."""
        return self.has_pending_finals

    def pop_fatal_error(self) -> str | None:
        """Fatal stream failure (auth/quota/connection) to surface to the
        client, returned ONCE then cleared; ``None`` while healthy. Consumers
        (the chat STT WebSocket) poll this alongside ``drain_transcript`` —
        without it a provider that dies after connecting looks like a mic
        that hears nothing. Default: no fatal-error surface.
        """
        return None

    @property
    def last_result_monotonic(self) -> float:
        """``time.monotonic()`` of the last NON-EMPTY interim or final this
        provider delivered; 0.0 before the first. The pipeline's STT guard
        compares it against accumulated caller-speech time to catch the
        third liveness failure mode — an alive-but-MUTE session (socket
        open, sends flowing, zero results while the caller speaks; live-hit
        2026-08-14) — and force a reconnect. Streaming providers stamp
        ``self._last_result_at`` in their result handlers; the default keeps
        non-streaming providers exempt (0.0 with no speech accumulation ever
        reaching the guard's threshold requires the pipeline's counter,
        which only the call pipeline runs). ``LanguageStreams`` reads it too:
        a candidate stream has answered its replay once a result lands after
        it, so a stream that never stamps it never wins a switch.
        """
        return getattr(self, "_last_result_at", 0.0)

    # ── The evidence ledger and the audio clock ────────────────────
    #
    # Created lazily per instance: a provider whose ``__init__`` never calls
    # ``super().__init__`` must not share a class-level list with every other
    # stream.

    @property
    def heard(self) -> tuple[Heard, ...]:
        """The finals of the last ``HEARD_KEEP_S`` clock seconds and the
        latest interim, oldest first. Filled only by a provider whose
        capabilities say ``reports_confidence``; ``clear_queue`` and
        ``finish`` leave it alone."""
        finals = tuple(self.__dict__.get("_ledger_finals", ()))
        interim = self.__dict__.get("_ledger_interim")
        return finals + (interim,) if interim is not None else finals

    @property
    def heard_version(self) -> int:
        """Moves with every entry the ledger takes: a consumer compares only
        when it changed."""
        return self.__dict__.get("_ledger_version", 0)

    def _heard_interim(self, entry: Heard) -> None:
        """A live interim: it replaces the previous interim entry."""
        self.__dict__["_ledger_interim"] = entry
        self.__dict__["_ledger_version"] = self.heard_version + 1

    def _heard_final(self, entry: Heard) -> None:
        """A final: it replaces the interim it completes (one that began
        before the final's end), and finals older than ``HEARD_KEEP_S`` of
        the clock go."""
        finals = self.__dict__.setdefault("_ledger_finals", [])
        self.__dict__["_ledger_version"] = self.heard_version + 1
        interim = self.__dict__.get("_ledger_interim")
        if interim is not None and interim.start < entry.end:
            self.__dict__["_ledger_interim"] = None
        finals.append(entry)
        horizon = max(self.audio_offered_s, entry.end) - HEARD_KEEP_S
        while finals and finals[0].end < horizon:
            finals.pop(0)

    @property
    def audio_offered_s(self) -> float:
        """The provider's audio clock: the seconds of audio offered to
        ``send_audio`` since the first ``start`` (the bytes a dead socket
        skipped and the silence fed during playback included). ``Heard``
        entries are on this clock, whatever reconnects happened."""
        return self.__dict__.get("_audio_offered", 0.0)

    def _count_offered(self, nbytes: int, rate: int, channels: int = 1) -> None:
        if rate > 0 and nbytes > 0:
            self.__dict__["_audio_offered"] = self.audio_offered_s + nbytes / (rate * 2 * channels)

    @abstractmethod
    async def close(self) -> None:
        """Close the connection without waiting for final transcript."""

    # ── Echo management during mid-call TTS ────────────────────────

    async def feed_during_tts(self, audio_bytes: bytes) -> None:
        """Called for each audio frame while TTS is playing and STT is muted.

        The provider decides how to handle echo: send silence bytes to keep
        its model active, ignore entirely, run acoustic echo cancellation,
        etc.  Also handles any stale transcript cleanup internally.

        Args:
            audio_bytes: The raw PCM audio frame from the microphone.

        Default: no-op (suitable for providers unaffected by echo).
        """
        return

    def on_tts_finished(self, was_interrupted: bool) -> None:
        """Called when TTS playback ends.

        Provider handles echo cleanup (e.g., clearing transcript queue).

        Args:
            was_interrupted: True if TTS was cancelled by barge-in.
                When True, the user is actively speaking — the transcript
                queue may contain valid speech that should NOT be discarded.
                When False, TTS finished naturally — the queue likely
                contains echo artifacts that should be discarded.

        Default: no-op.
        """
        return

    # ── Echo management during long opening TTS ────────────────────

    async def feed_during_opening(self) -> None:
        """Called periodically during long opening TTS (10+ seconds).

        Provider manages its own keepalive / connection health.
        For cloud providers this may send a keepalive message;
        for local models this is typically a no-op.

        Default: no-op.
        """
        return

    # ── Liveness: keepalive, health, reconnect ─────────────────────

    @property
    def is_open(self) -> bool:
        """True while the streaming connection is believed healthy.

        The pipeline's STT guard polls this (a dead socket can't raise from
        ``send_audio``/``keepalive`` — both log-and-return) and triggers
        ``ensure_alive`` when it flips false mid-call.
        """
        return self._is_open

    async def keepalive(self) -> None:
        """Keep the streaming connection alive through send starvation.

        Called by the pipeline's guard task when no audio (real or silence)
        has reached the provider for a few seconds — playback mute, opening
        discard, inbound media gaps, held playback. Cloud providers send a
        protocol keepalive or a zeros chunk; local models no-op.

        Default: no-op.
        """
        return

    async def ensure_alive(self, language: str, *, clear_queue: bool = False) -> bool:
        """Health probe + reconnect from the provider's stored start() params.

        ``clear_queue`` discards pending transcripts when the probe finds the
        connection healthy — correct after an opening monologue (queue holds
        echo artifacts), WRONG after a barged-in playback (queue holds the
        interrupting utterance's finals) — so the caller must choose.
        Reconnects always start from a fresh queue regardless.

        Returns True if the provider is ready to receive audio.
        Default: returns True (assumes healthy — local models).
        """
        return True

    async def recover_after_opening(self, language: str) -> bool:
        """Post-opening health check — thin wrapper over ``ensure_alive`` with
        the opening semantics (clear stale echo transcripts when healthy)."""
        return await self.ensure_alive(language, clear_queue=True)

    # ── Connection lifecycle properties ────────────────────────────

    @property
    def needs_pre_connect(self) -> bool:
        """True if start() is expensive and should be called early
        (e.g., during greeting playback).

        Default: True (conservative — pre-connect is always safe).
        """
        return True

    @property
    def stay_open_between_turns(self) -> bool:
        """True if closing/reopening between turns is expensive.
        When True, the pipeline uses drain_transcript() between turns
        instead of finish() + start().

        Default: True (conservative).
        """
        return True

    # ── Delayed transcript delivery timing ─────────────────────────

    @property
    def transcript_wait_timeout_s(self) -> float:
        """Max seconds to wait for a transcript after SPEECH_END.

        Covers network RTT + provider endpointing jitter for cloud
        providers.  Local models can use much smaller values.

        Default: 1.0 seconds.
        """
        return 1.0

    # ── Early unmute during barge-in ───────────────────────────────

    @property
    def supports_early_unmute(self) -> bool:
        """True if provider benefits from receiving audio before full
        barge-in confirmation (captures speech onset for better accuracy).

        Default: True (conservative — real audio is always useful).
        """
        return True

    # ── VAD integration ────────────────────────────────────────────

    @property
    def vad_silence_padding_ms(self) -> int:
        """Silence duration (ms) for VAD, accounting for endpointing delay.

        The pipeline uses this as the VAD silence_duration_ms parameter.
        Providers with faster endpointing can return smaller values.

        Default: endpointing_ms + 50.
        """
        return self.endpointing_ms + 50

    # ── Logging helper (Rule #1: never bare-log transcripts) ───────

    def _log_transcript(self, label: str, text: str) -> None:
        """Log a transcript through the policy gate (INFO only when opted in)."""
        log_transcript(logging.getLogger(type(self).__module__), label, text)

    # ── Secret-safe repr ───────────────────────────────────────────

    def __repr__(self) -> str:
        return f"<{type(self).__name__} (api_key=***redacted***)>"

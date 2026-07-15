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
from dataclasses import dataclass, field
from typing import Any, ClassVar

from audio.capabilities import STTCapabilities, BillingUnit
from audio.log_policy import log_transcript
from audio.providers.credential_resolver import CredentialResolver

__all__ = ["STTProvider", "TranscriptResult", "Word"]


@dataclass
class Word:
    """One word with start/end offsets in seconds (for SRT generation)."""

    word: str
    start: float
    end: float


@dataclass
class TranscriptResult:
    """Result of a batch (prerecorded) file transcription."""

    text: str
    language: str
    audio_seconds: float            # decoded duration; the billing multiplier
    words: list[Word] = field(default_factory=list)
    provider_used: str = ""         # provider_name that served it (e.g. "deepgram")


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

    _is_open: bool = False

    # ── Construction from an audio_providers row ───────────────────

    @classmethod
    @abstractmethod
    def from_row(cls, row: dict, resolver: CredentialResolver) -> "STTProvider":
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
        delay.  Default: no-op.
        """

    @property
    def latest_interim(self) -> str:
        """Most recent non-final partial transcript, or ``""`` when the provider
        doesn't stream interims / none is pending. The pipeline uses it as a
        fallback when a forced finalize fails to deliver an is_final. Default:
        ``""`` (provider streams no interims).
        """
        return ""

    def pop_fatal_error(self) -> str | None:
        """Fatal stream failure (auth/quota/connection) to surface to the
        client, returned ONCE then cleared; ``None`` while healthy. Consumers
        (the chat STT WebSocket) poll this alongside ``drain_transcript`` —
        without it a provider that dies after connecting looks like a mic
        that hears nothing. Default: no fatal-error surface.
        """
        return None

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

    # ── Echo management during long opening TTS ────────────────────

    async def feed_during_opening(self) -> None:
        """Called periodically during long opening TTS (10+ seconds).

        Provider manages its own keepalive / connection health.
        For cloud providers this may send a keepalive message;
        for local models this is typically a no-op.

        Default: no-op.
        """

    async def recover_after_opening(self, language: str) -> bool:
        """Called after opening TTS ends.

        Provider checks its own health, reconnects if needed, and
        clears any stale state.

        Args:
            language: Language code for reconnection if needed.

        Returns:
            True if the provider is ready to receive audio.

        Default: returns True (assumes healthy).
        """
        return True

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

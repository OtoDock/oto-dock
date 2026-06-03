"""Abstract base class for TTS providers.

The streaming surface (connect / start_streaming_context / send_text_chunk /
receive_audio / cancel) is the original voice-server contract — a provider
streams text chunks from the LLM into an audio context that preserves prosody
across continuations. ``synthesize`` is the one-shot path (greetings).

Per-language voices live in a ``voices`` map (``{lang: voice_id}``); ``voice_id``
is the active voice, selectable via ``select_voice``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, ClassVar

from audio.capabilities import TTSCapabilities, BillingUnit
from audio.providers.credential_resolver import CredentialResolver

__all__ = ["TTSProvider", "VoiceInfo", "UnsupportedProviderOperation"]


class UnsupportedProviderOperation(Exception):
    """An optional provider operation (voice library search/add) the engine
    doesn't support. Callers map this to a clean client error, not a 5xx."""


@dataclass(frozen=True)
class VoiceInfo:
    """One catalog voice — the provider-agnostic shape the voice endpoints
    return. ``owner_id`` is only set for shared-library results (needed to add
    the voice to the vendor workspace before use)."""

    id: str
    name: str = ""
    languages: list[str] = field(default_factory=list)
    category: str = ""
    preview_url: str = ""
    description: str = ""
    owner_id: str = ""


class TTSProvider(ABC):
    """Interface all TTS providers must implement."""

    # Subclasses MUST override `capabilities`. Billing defaults to per-char, $0.
    capabilities: ClassVar[TTSCapabilities] = TTSCapabilities()
    is_free_tier: ClassVar[bool] = False
    # True on not-yet-implemented placeholder classes (importable, __init__ raises).
    # The admin "Add provider" menu hides stubs; flip when the implementation lands.
    is_stub: ClassVar[bool] = False

    voice_id: str = ""
    voices: dict[str, str]
    # Class-level fallback voices (language → voice id) consulted when the
    # admin configured none for a language. Providers ship known-good,
    # ACCOUNT-INDEPENDENT ids here (public-library / premade voices only) so
    # TTS works out of the box; the admin's ``voices`` map always wins.
    default_voices: ClassVar[dict[str, str]] = {}

    # ── Construction from an audio_providers row ───────────────────

    @classmethod
    @abstractmethod
    def from_row(cls, row: dict, resolver: CredentialResolver) -> "TTSProvider":
        """Build an instance from an ``audio_providers`` row + a credential resolver."""

    # ── Capability / billing metadata ──────────────────────────────

    @classmethod
    def billing_unit(cls) -> BillingUnit:
        """Unit this provider bills on. TTS is usually per-character."""
        return "char"

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
        """Return ``{}`` on success, ``{field_name: error_message}`` on failure."""
        return {}

    # ── Voice selection ────────────────────────────────────────────

    def select_voice(self, language: str) -> str:
        """Set the active ``voice_id`` from the per-language ``voices`` map.

        Resolution order: the admin's map for the language, the admin's English
        voice (a multilingual voice still pronounces the text), then the
        provider's built-in ``default_voices`` (same language-then-English
        order) — so a row with no voices configured still speaks every
        language. Falls back to the current ``voice_id`` if nothing resolves.
        Returns the resolved voice id.
        """
        chosen = (
            self.voices.get(language) or self.voices.get("en")
            or self.default_voices.get(language) or self.default_voices.get("en")
        )
        if chosen:
            self.voice_id = chosen
        return self.voice_id

    # ── Voice discovery (optional surface) ─────────────────────────

    async def list_voices(self) -> list[VoiceInfo]:
        """The provider's voice catalog. Default: the configured per-language
        map (id-deduped) — cloud providers with a voices API override this."""
        seen: dict[str, VoiceInfo] = {}
        for lang, vid in self.voices.items():
            if vid in seen:
                seen[vid].languages.append(lang)
            else:
                seen[vid] = VoiceInfo(id=vid, name=vid, languages=[lang], category="configured")
        return list(seen.values())

    async def search_voice_library(
        self, *, search: str | None = None, language: str | None = None,
        gender: str | None = None, age: str | None = None,
        category: str | None = None, page: int = 0, page_size: int = 20,
    ) -> list[VoiceInfo]:
        """Search the provider's shared voice library (where one exists)."""
        raise UnsupportedProviderOperation(
            f"{type(self).__name__} has no searchable voice library"
        )

    async def add_library_voice(
        self, public_owner_id: str, voice_id: str, name: str | None = None,
    ) -> str:
        """Add a shared-library voice to the vendor workspace so TTS calls can
        use it. Returns the usable voice id."""
        raise UnsupportedProviderOperation(
            f"{type(self).__name__} has no voice library to add from"
        )

    # ── Streaming surface ──────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> None:
        """Open the connection to the TTS provider."""

    @abstractmethod
    async def close(self) -> None:
        """Close the connection."""

    @abstractmethod
    async def synthesize(self, text: str, *, language: str | None = None) -> bytes:
        """Synthesize complete text to PCM audio (one-shot, e.g. greetings/fillers).

        ``language`` (base code, e.g. ``de``) tells the engine which language to
        pronounce — needed when a multilingual voice serves several languages.
        """

    @abstractmethod
    def start_streaming_context(
        self, *, output_sample_rate: int | None = None, language: str | None = None,
    ) -> None:
        """Start a new streaming context for the LLM→TTS bridge.

        Continuations preserve prosody across the text chunks pushed via
        ``send_text_chunk``. ``output_sample_rate`` overrides the provider's
        default output rate (telephony 8 kHz) for surfaces that want a higher rate
        (chat → 24 kHz). ``language`` (base code, e.g. ``de``/``el``) tells the
        engine which language to pronounce; ``None`` uses the voice's default.
        """

    @abstractmethod
    async def send_text_chunk(self, text: str, is_last: bool = False) -> None:
        """Send a text chunk to the current streaming context."""

    @abstractmethod
    async def receive_audio(self) -> AsyncGenerator[bytes, None]:
        """Yield PCM audio chunks from the current streaming context."""
        raise NotImplementedError
        yield  # pragma: no cover — marks this as an async generator

    @abstractmethod
    def cancel(self) -> None:
        """Cancel the current streaming context (for barge-in)."""

    # ── Secret-safe repr ───────────────────────────────────────────

    def __repr__(self) -> str:
        return f"<{type(self).__name__} voice_id={self.voice_id!r} (api_key=***redacted***)>"

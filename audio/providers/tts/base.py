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
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, ClassVar

from audio.capabilities import BillingUnit, TTSCapabilities
from audio.providers.credential_resolver import CredentialResolver
from audio.providers.model_succession import successor_of

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
    # Retired model ids and their successors (``audio.providers.model_succession``):
    # a stored id resolves through ``live_model_id`` at every build, and the
    # proxy's boot walk rewrites the rows.
    model_successors: ClassVar[dict[str, str]] = {}

    voice_id: str = ""
    voices: dict[str, str]
    # Class-level fallback voices (language → voice id) consulted when the
    # admin configured none for a language. Providers ship known-good,
    # ACCOUNT-INDEPENDENT ids here (public-library / premade voices only) so
    # TTS works out of the box; the admin's voice for a language always wins
    # over the shipped one for it.
    default_voices: ClassVar[dict[str, str]] = {}
    # Whether the shipped voices speak their language natively: then one
    # outranks the admin's English voice for that language; a shipped
    # voice with an English accent never does (``select_voice``).
    shipped_voices_native: ClassVar[bool] = True

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
    # A voice pinned for the whole session (a phone route's own voice): it
    # wins over both maps in ``select_voice``, for every language.
    voice_pin: str = ""

    def pin_voice(self, voice_id: str) -> None:
        """Pin ``voice_id`` for every language of this session ("" unpins)."""
        self.voice_pin = (voice_id or "").strip()
        if self.voice_pin:
            self.voice_id = self.voice_pin

    # ── Construction from an audio_providers row ───────────────────

    @classmethod
    @abstractmethod
    def from_row(cls, row: dict, resolver: CredentialResolver) -> TTSProvider:
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

        Resolution order: a pinned voice (``pin_voice``), the admin's voice
        for the language, the provider's shipped voice for the language when
        the provider's shipped voices are native speakers (a native voice
        beats an English one speaking it), the admin's English voice (a
        multilingual voice still pronounces the text), then the shipped
        voice for the language and the shipped English voice — so a row with
        no voices configured still speaks every language, and an admin's
        chosen English voice is never outranked by a shipped voice with an
        English accent (``shipped_voices_native`` False). Falls back to the
        current ``voice_id`` if nothing resolves. Returns the resolved voice id.
        """
        if self.voice_pin:
            self.voice_id = self.voice_pin
            return self.voice_id
        shipped = self.default_voices.get(language)
        chosen = (
            self.voices.get(language) or (shipped if self.shipped_voices_native else None)
            or self.voices.get("en") or shipped or self.default_voices.get("en")
        )
        if chosen:
            self.voice_id = chosen
        return self.voice_id

    def has_voice_for(self, language: str) -> bool:
        """A voice meant for ``language`` exists (the admin's map or the
        provider's defaults), as opposed to the English fallback
        ``select_voice`` takes. A call's language detection asks before it
        opens a candidate stream in a language the line has no voice for."""
        return bool(self.voices.get(language) or self.default_voices.get(language))

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
    async def connect(self, *, output_sample_rate: int | None = None) -> None:
        """Open the connection to the TTS provider.

        ``output_sample_rate`` hints the session's real output rate so
        providers with per-rate socket bindings (ElevenLabs) can prewarm the
        binding the first utterance will actually use; rate-agnostic
        providers ignore it. ``None`` → the 8 kHz telephony default.
        """

    @abstractmethod
    async def close(self) -> None:
        """Close the connection."""

    @abstractmethod
    async def synthesize(
        self, text: str, *, language: str | None = None,
        output_sample_rate: int | None = None,
    ) -> bytes:
        """Synthesize complete text to PCM audio (one-shot, e.g. greetings/fillers).

        ``language`` (base code, e.g. ``de``) tells the engine which language to
        pronounce — needed when a multilingual voice serves several languages.
        ``output_sample_rate`` overrides the 8 kHz telephony default (duplex
        fillers are synthesized at the transport's output rate).
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

    async def flush(self) -> None:  # noqa: B027 — a default no-op: an engine that generates as the text arrives has nothing to release
        """Synthesize what the context holds without ending the utterance.

        A streaming engine may hold a short chunk until more text arrives;
        the bridge calls this when the engine pauses after a complete
        sentence (a tool call, a short answer), so the sentence plays while
        the context stays open for more text. Never a close: ``is_last``
        still ends the utterance. Default: nothing, for an engine that
        generates as the text arrives.
        """

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

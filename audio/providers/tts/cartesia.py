"""Cartesia Sonic TTS provider (streaming WebSocket).

Cartesia SDK v3.x API:
  conn = await client.tts.websocket_connect().enter()   # long-lived connection
  ctx  = conn.context(model_id=, voice={"mode":"id","id":…}, output_format={…}, language=)
  await ctx.push(text)                                  # stream text (continuations)
  await ctx.no_more_inputs()                            # end of input for this context
  async for r in ctx.receive():                         # r.type=="chunk" → r.audio (decoded bytes)
      ...
  await conn.close()

``output_format`` is a plain dict (``RawOutputFormatParam``) — no private types import. A single
connection multiplexes many sequential contexts (one per utterance / one-shot clip).

The voice catalog and search walk ``client.voices.list(...)``, a plain method whose paginator
``async for`` walks page by page. A public voice speaks by its id as is: Cartesia has no add step.
"""

from __future__ import annotations

import contextlib
import logging
import math
import time
from typing import get_args, get_type_hints

from cartesia import AsyncCartesia
from cartesia.types.generation_config_param import GenerationConfigParam

from audio.capabilities import TTSCapabilities
from audio.constants import SAMPLE_RATE
from audio.log_policy import log_transcript
from audio.providers.credential_resolver import CredentialResolver
from audio.providers.model_succession import DEFAULT_MODELS, MODEL_SUCCESSORS
from audio.providers.tts.base import (
    TTSProvider,
    UnsupportedProviderOperation,
    VoiceInfo,
)

logger = logging.getLogger(__name__)


def _output_format(sample_rate: int) -> dict:
    """Raw 16-bit signed LE mono PCM at ``sample_rate`` (8 kHz matches AudioSocket)."""
    return {"container": "raw", "encoding": "pcm_s16le", "sample_rate": sample_rate}


# Default Cartesia model (``audio.providers.model_succession``, which also
# names the retired ids and their successor). Admin-overridable per provider
# via ``advanced.model_id``.
_MODEL_ID = DEFAULT_MODELS[("tts", "cartesia")]["model_id"]

# Cartesia Sonic bills per character (~$40 / 1M chars on the Pro tier, 2026-06-03).
# Varies by plan — verify and override per instance via advanced.rate_override_per_unit.
_COST_PER_CHAR = 0.00004

# The catalog is a first look: the account's own voices, then public ones, until
# the list holds this many. A style is found with the search, which reaches the
# whole library.
_CATALOG_SIZE = 50
# A walk of the library never reads more voices than this (ten of the API's
# largest pages), so a page far past the end, a language the API ignores, or
# a page cursor the API ignores stays a few requests.
_SEARCH_WALK = 1000
_PAGE_LIMIT = 100
_HTTP_TIMEOUT_S = 30.0

# The shared tools' gender words and Cartesia's.
_GENDERS = {"male": "masculine", "female": "feminine", "neutral": "gender_neutral"}
_GENDER_WORDS = {cartesia: word for word, cartesia in _GENDERS.items()}


# Sonic 3's delivery settings (``generation_config``), ranges as the pinned SDK
# documents them. The emotion words are read from the SDK, so a pin bump moves
# them; Cartesia applies an emotion to English speech only.
_SPEED_RANGE = (0.6, 1.5)
_VOLUME_RANGE = (0.5, 2.0)
_EMOTIONS = frozenset(
    word for part in get_args(get_type_hints(GenerationConfigParam)["emotion"])
    for word in get_args(part)
)


def _number(value) -> float | None:
    """A finite number from an ``advanced`` value (a number or its text), else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _generation_config(advanced: dict) -> dict | None:
    """``speed``, ``volume`` and ``emotion`` from the row's settings and the
    call's, as Sonic 3 reads them; None when none is set.

    A call's settings never pass ``validate_advanced`` (the admin save runs
    it), and Cartesia answers a refused setting with an error frame that ends
    the context: so a value is held to what the engine accepts, a number
    clamped into its range, a non-number or an unknown emotion dropped."""
    config: dict = {}
    for key, (lo, hi) in (("speed", _SPEED_RANGE), ("volume", _VOLUME_RANGE)):
        value = _number(advanced.get(key))
        if value is not None:
            config[key] = min(max(value, lo), hi)
        elif advanced.get(key) not in (None, ""):
            logger.warning("Cartesia TTS: %s %r is not a number, ignored", key, advanced[key])
    emotion = str(advanced.get("emotion") or "").strip().lower()
    if emotion in _EMOTIONS:
        config["emotion"] = emotion
    elif emotion:
        logger.warning("Cartesia TTS: unknown emotion %r ignored", emotion)
    return config or None


def _log_refusal(event) -> None:
    """A Cartesia ``error`` frame ends its context: say why (the vendor's
    title and message, never the text spoken)."""
    logger.warning(
        "Cartesia TTS refused (%s): %s %s", getattr(event, "status_code", None),
        getattr(event, "title", None) or getattr(event, "error_code", None) or "",
        getattr(event, "message", None) or "",
    )


def _base_language(tag: str | None) -> str:
    return (tag or "").split("-", 1)[0].strip().lower()


def _voice_info(voice) -> VoiceInfo:
    """A Cartesia voice in the shared catalog shape: no preview link (the
    vendor's preview URL answers only with the account's key) and no
    ``owner_id`` (nothing to add)."""
    gender = _GENDER_WORDS.get(voice.gender or "", "")
    description = (voice.description or "")[:200]
    return VoiceInfo(
        id=voice.id,
        name=voice.name or voice.id,
        languages=[voice.language] if voice.language else [],
        category="own" if voice.is_owner else "public",
        description=" ".join(part for part in (gender, description) if part),
    )


class CartesiaTTS(TTSProvider):
    """Streaming text-to-speech via Cartesia WebSocket."""

    capabilities = TTSCapabilities(supports_streaming=True, supports_endpointing=False, is_local=False)
    model_successors = MODEL_SUCCESSORS[("tts", "cartesia")]

    # Public-library voices (account-independent ids), one native voice per
    # shipped language, each checked by a live synthesis: Skylar, Despina,
    # Klara, Marta (Castilian), Lise, Elena. An admin's voice for a language
    # wins over the shipped one.
    default_voices = {
        "en": "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4",
        "el": "50849023-76e9-46c7-af52-9ec39888a165",
        "de": "2578354e-4b18-4d28-832c-5943344b7085",
        "es": "de38f545-c574-44e8-9b54-a7d6fec1c6b1",
        "fr": "045b0ec6-3bed-4bc8-b797-b90a7bb5b445",
        "it": "00e9ec78-2002-41dd-8d19-6b1d3b17a461",
    }

    def __init__(
        self,
        *,
        api_key: str = "",
        voice_id: str = "",
        voices: dict[str, str] | None = None,
        advanced: dict | None = None,
    ):
        self._api_key = api_key
        self._client = AsyncCartesia(api_key=api_key)
        self._conn = None       # AsyncTTSResourceConnection — long-lived, reused across contexts
        self._ctx = None        # AsyncWebSocketContext — the current streaming utterance
        self._cancelled = False
        self.voices: dict[str, str] = voices or {}
        self.voice_id: str = voice_id or ""
        self._advanced = advanced or {}
        self._model_id = self.live_model_id(self._advanced.get("model_id")) or _MODEL_ID
        self._generation_config = _generation_config(self._advanced)
        self._t_first_send = 0.0   # first-audio isolation stamps (per context)
        self._sent_chars = 0

    # ── Factory / metadata ─────────────────────────────────────────

    @classmethod
    def from_row(cls, row: dict, resolver: CredentialResolver) -> CartesiaTTS:
        return cls(
            api_key=resolver(row.get("credential_key", "")),
            voices=row.get("voices") or {},
            advanced=row.get("advanced") or {},
        )

    @classmethod
    def cost_per_unit(cls) -> float:
        return _COST_PER_CHAR

    @classmethod
    def default_model_ids(cls) -> dict[str, str]:
        return dict(DEFAULT_MODELS[("tts", "cartesia")])

    @classmethod
    def validate_advanced(cls, settings: dict) -> dict[str, str]:
        errors: dict[str, str] = {}
        if "model_id" in settings and not str(settings["model_id"]).startswith("sonic"):
            errors["model_id"] = "must be a Cartesia Sonic model id (sonic…)"
        for key, (lo, hi) in (("speed", _SPEED_RANGE), ("volume", _VOLUME_RANGE)):
            if key in settings:
                value = _number(settings[key])
                if value is None:
                    errors[key] = "must be a number"
                elif not lo <= value <= hi:
                    errors[key] = f"must be between {lo:g} and {hi:g}"
        if "emotion" in settings and str(settings["emotion"]).strip().lower() not in _EMOTIONS:
            errors["emotion"] = (
                "must be an emotion Cartesia knows, such as neutral, calm, content, sad, angry or scared"
            )
        return errors

    def _voice(self) -> dict:
        return {"mode": "id", "id": self.voice_id}

    # ── Connection lifecycle ───────────────────────────────────────

    async def connect(self, *, output_sample_rate: int | None = None) -> None:
        """Open the long-lived WebSocket connection to Cartesia.

        ``output_sample_rate`` is accepted for provider-interface parity and
        ignored: the socket is rate-agnostic — each streaming context carries
        its own ``output_format``, so there is no per-rate binding to prewarm.
        """
        self._conn = await self._client.tts.websocket_connect().enter()
        self._cancelled = False
        logger.info("Cartesia TTS WebSocket connected")

    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self._conn:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None
        self._ctx = None
        with contextlib.suppress(Exception):
            await self._client.close()

    # ── One-shot synthesis (greetings / fillers) ───────────────────

    async def synthesize(
        self, text: str, *, language: str | None = None,
        output_sample_rate: int | None = None,
    ) -> bytes:
        """Synthesize complete text to PCM audio (one-shot). Drives a dedicated
        context to completion on the shared connection. ``language`` (base code)
        tells the engine which language to pronounce (None → voice default)."""
        if not self._conn:
            await self.connect()

        ctx = self._conn.context(
            model_id=self._model_id, voice=self._voice(),
            output_format=_output_format(output_sample_rate or SAMPLE_RATE),
            language=language, generation_config=self._generation_config,
        )
        await ctx.push(text)
        await ctx.no_more_inputs()

        chunks = []
        async for r in ctx.receive():
            if r.type == "chunk" and r.audio:
                chunks.append(r.audio)
            elif r.type == "error":
                _log_refusal(r)

        result = b"".join(chunks)
        # The synthesized text is agent speech that can quote user-personal data —
        # route it through the transcript gate; keep only the byte count at INFO.
        log_transcript(logger, "TTS synthesize", text)
        logger.info("TTS synthesized %s bytes", len(result))
        return result

    # ── Streaming context (LLM → TTS bridge) ───────────────────────

    def start_streaming_context(
        self, *, output_sample_rate: int | None = None, language: str | None = None,
    ) -> None:
        """Start a new streaming context for the LLM-to-TTS bridge.

        Continuations (``push`` with the context's defaults) preserve prosody
        across multiple text chunks. ``output_sample_rate`` overrides the 8 kHz
        telephony default (chat streams 24 kHz); ``language`` (e.g. ``de``/``el``)
        tells Cartesia which language to pronounce (``None`` → voice default).
        """
        if not self._conn:
            raise RuntimeError("TTS WebSocket not connected")
        self._ctx = self._conn.context(
            model_id=self._model_id, voice=self._voice(),
            output_format=_output_format(output_sample_rate or SAMPLE_RATE),
            language=language, generation_config=self._generation_config,
        )
        self._cancelled = False
        # First-audio isolation stamps (one context at a time — provider-level)
        self._t_first_send = 0.0
        self._sent_chars = 0
        logger.debug("TTS streaming context started")

    async def send_text_chunk(self, text: str, is_last: bool = False) -> None:
        """Send a text chunk to the current streaming context.

        Args:
            text: Text to synthesize (empty is allowed — used to flush the end).
            is_last: If True, signals no more text will follow.
        """
        if not self._ctx or self._cancelled:
            return

        try:
            if text:
                await self._ctx.push(text)
                self._sent_chars += len(text)
                if not self._t_first_send:
                    self._t_first_send = time.monotonic()
            if is_last:
                await self._ctx.no_more_inputs()
        except Exception as e:  # noqa: BLE001  # the Cartesia SDK raises whatever its socket does
            if not self._cancelled:
                logger.error("TTS send error: %s", e)

    async def receive_audio(self):
        """Yield PCM audio chunks from the current streaming context."""
        if not self._ctx:
            return

        first = True
        try:
            async for r in self._ctx.receive():
                if self._cancelled:
                    break
                if r.type == "chunk" and r.audio:
                    if first:
                        first = False
                        if self._t_first_send:
                            logger.info(
                                "Cartesia TTS first audio: +%.0fms after "
                                "first text (%d chars sent)",
                                (time.monotonic() - self._t_first_send) * 1000,
                                self._sent_chars,
                            )
                    yield r.audio
                elif r.type == "error":
                    _log_refusal(r)
        except Exception as e:  # noqa: BLE001  # the Cartesia SDK raises whatever its socket does
            if not self._cancelled:
                logger.error("TTS receive error: %s", e)

    def cancel(self) -> None:
        """Cancel the current streaming context (for barge-in).

        Sets a flag that stops ``receive_audio`` iteration and abandons the
        context; a new one is created for the next turn.
        """
        self._cancelled = True
        self._ctx = None
        logger.debug("TTS streaming cancelled (barge-in)")

    # ── Voice discovery ────────────────────────────────────────────
    # Each call opens its own client: the voice endpoints build a provider
    # and never close it, so ``self._client`` would keep a pool open.

    async def list_voices(self) -> list[VoiceInfo]:
        """The account's own voices, then public ones, ``_CATALOG_SIZE`` in all."""
        out: list[VoiceInfo] = []
        seen: set[str] = set()
        walked = 0
        async with AsyncCartesia(api_key=self._api_key, timeout=_HTTP_TIMEOUT_S) as client:
            for owner_only in (True, False):
                if len(out) >= _CATALOG_SIZE or walked >= _SEARCH_WALK:
                    break
                kwargs: dict = {"limit": _PAGE_LIMIT}
                if owner_only:
                    kwargs["is_owner"] = True
                async for voice in client.voices.list(**kwargs):
                    walked += 1
                    if voice.id not in seen:
                        seen.add(voice.id)
                        out.append(_voice_info(voice))
                    if len(out) >= _CATALOG_SIZE or walked >= _SEARCH_WALK:
                        break
        return out

    async def search_voice_library(
        self, *, search: str | None = None, language: str | None = None,
        gender: str | None = None, age: str | None = None,
        category: str | None = None, page: int = 0, page_size: int = 20,
    ) -> list[VoiceInfo]:
        """Every voice the account can speak with, its own and the public
        library's, by text, language and gender. ``age`` and ``category`` are
        not Cartesia's: ignored. The language tag goes to the API whole (a
        locale such as ``en-GB`` narrows to that accent) and its base is
        checked again on each voice, so one in another language never shows;
        pages count the voices that match."""
        size = max(1, min(int(page_size), _PAGE_LIMIT))
        kwargs: dict = {"limit": _PAGE_LIMIT}
        if search and search.strip():
            kwargs["q"] = search.strip()
        if gender:
            if gender not in _GENDERS:
                raise UnsupportedProviderOperation(
                    f"Cartesia filters by gender male, female or neutral, not {gender!r}"
                )
            kwargs["gender"] = _GENDERS[gender]
        base = _base_language(language)
        if base:
            kwargs["extra_query"] = {"language": (language or "").strip()}
        skip = max(0, int(page)) * size
        out: list[VoiceInfo] = []
        walked = 0
        async with AsyncCartesia(api_key=self._api_key, timeout=_HTTP_TIMEOUT_S) as client:
            async for voice in client.voices.list(**kwargs):
                walked += 1
                if not (base and voice.language and _base_language(voice.language) != base):
                    if skip:
                        skip -= 1
                    else:
                        out.append(_voice_info(voice))
                if len(out) >= size or walked >= _SEARCH_WALK:
                    break
        return out

    async def add_library_voice(
        self, public_owner_id: str, voice_id: str, name: str | None = None,
    ) -> str:
        raise UnsupportedProviderOperation(
            "Cartesia voices need no adding: pass the voice id to generate_speech"
        )

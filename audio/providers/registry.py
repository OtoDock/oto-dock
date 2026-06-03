"""Provider discovery registry + caching factory.

``KNOWN_*`` map ``provider_name`` → ``"module:Class"`` strings, lazily imported
so a provider's import-time model load never fires on proxy startup.
``build_provider`` resolves the class and calls its ``from_row``.

``get_or_build_provider`` caches process-wide for **local model-loading**
providers (heavy ONNX/Whisper load — caching reuses the loaded model). Cloud /
streaming providers (``capabilities.is_local == False``, e.g. Deepgram WS,
Cartesia WS) hold per-connection state and are **never** cached — they always
build fresh.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
from typing import Any

from audio.providers.credential_resolver import CredentialResolver

logger = logging.getLogger(__name__)

# name → "module:Class". Lazy — resolved only when actually built.
KNOWN_STT_PROVIDERS: dict[str, str] = {
    "deepgram": "audio.providers.stt.deepgram:DeepgramSTT",
    "elevenlabs": "audio.providers.stt.elevenlabs:ElevenLabsSTT",
    "canary": "audio.providers.stt.canary:NvidiaCanarySTT",
}

KNOWN_TTS_PROVIDERS: dict[str, str] = {
    "cartesia": "audio.providers.tts.cartesia:CartesiaTTS",
    "elevenlabs": "audio.providers.tts.elevenlabs:ElevenLabsTTS",
    "chatterbox": "audio.providers.tts.chatterbox:ChatterboxTTS",
}


def _resolve(path: str) -> type:
    module_path, _, cls_name = path.partition(":")
    return getattr(importlib.import_module(module_path), cls_name)


def get_provider_class(provider_type: str, provider_name: str) -> type:
    """Resolve (lazily import) the provider class for a (type, name) pair."""
    table = KNOWN_STT_PROVIDERS if provider_type == "stt" else KNOWN_TTS_PROVIDERS
    if provider_name not in table:
        raise KeyError(f"Unknown {provider_type} provider: {provider_name!r}")
    return _resolve(table[provider_name])


def build_provider(row: dict, resolver: CredentialResolver) -> Any:
    """Build a fresh provider instance from an ``audio_providers`` row."""
    cls = get_provider_class(row["provider_type"], row["provider_name"])
    return cls.from_row(row, resolver)


_provider_cache: dict[tuple[str, str, str], Any] = {}


def get_or_build_provider(row: dict, resolver: CredentialResolver) -> Any:
    """Build a provider, caching only safe (local-model) providers.

    Cloud/streaming providers always build fresh (per-connection state). Local
    model providers are cached by (type, name, sha256(credential)[:16]) so the
    heavy model load happens once. Credential rotation needs a process restart
    in v1 (pub/sub eviction deferred — see ``clear_cache``).
    """
    cls = get_provider_class(row["provider_type"], row["provider_name"])
    if not getattr(cls.capabilities, "is_local", False):
        return cls.from_row(row, resolver)

    cred = resolver(row.get("credential_key", "")) or ""
    key = (
        row["provider_type"],
        row["provider_name"],
        hashlib.sha256(cred.encode()).hexdigest()[:16],
    )
    if key not in _provider_cache:
        _provider_cache[key] = cls.from_row(row, resolver)
    return _provider_cache[key]


def clear_cache() -> None:
    """Drop the local-provider cache (e.g. on credential rotation)."""
    _provider_cache.clear()

"""Chat audio service — capability resolution, provider building, cost
recording, and stream-cancellation for the ``/v1/audio/*`` endpoints.

Providers are built from ``audio_providers`` rows via the ``audio`` package
registry, with a proxy-side credential resolver (infra_credentials →
``API_KEY``). Cost is recorded with ``database.insert_usage_record`` directly
(the audio endpoints aren't MCP tool calls, so the cost engine never fires —
and that path also records $0/free-tier seconds).
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass

from audio.providers import registry
from storage import audio_provider_store
from storage import credential_store
from storage import database as task_store

# Browser playback rate — chat requests this rate from the TTS provider directly
# (Cartesia emits it natively), so no server-side resampling is needed.
CHAT_AUDIO_TARGET_RATE = 24000

_VALID_AVAIL = ("unavailable", "native_only", "platform", "either")


class AudioUnavailableError(Exception):
    """Raised when a requested provider can't be resolved/built."""


# ---------------------------------------------------------------------------
# Credential resolver + provider building
# ---------------------------------------------------------------------------

def credential_resolver(credential_key: str) -> str:
    """Resolve an audio provider's API key (uniform ``API_KEY`` inner key)."""
    if not credential_key:
        return ""
    creds = credential_store.get_infra_credentials(credential_key)
    return creds.get(audio_provider_store.CREDENTIAL_INNER_KEY, "")


def _provider_usable(row: dict | None) -> bool:
    """A provider is usable if it's local (no key) or has its credential set."""
    if not row:
        return False
    if not row.get("credential_key"):
        # No credential slot → only usable if it's a local engine.
        return True
    return bool(credential_resolver(row["credential_key"]))


def transcribe_capability_available() -> bool:
    """True when file transcription can run — i.e. a usable default STT provider
    exists (the transcribe endpoint + MCP use the chat-default STT). Gates the
    transcribe MCP out of the assignable set when no STT is configured."""
    return _provider_usable(audio_provider_store.get_default_provider("stt", "chat"))


def phone_calls_available() -> bool:
    """True when a phone call's audio pipeline could run — i.e. usable default
    *call* STT and TTS providers both exist. Gates the phone MCP out of the
    assignable set when telephony audio isn't configured."""
    return (
        _provider_usable(audio_provider_store.get_default_provider("stt", "calls"))
        and _provider_usable(audio_provider_store.get_default_provider("tts", "calls"))
    )


def _default_tts_row() -> dict | None:
    """Default TTS row for file generation: chat default, else calls default
    (phone-first installs configure only a calls provider — it still generates
    voice-over files fine)."""
    return (
        audio_provider_store.get_default_provider("tts", "chat")
        or audio_provider_store.get_default_provider("tts", "calls")
    )


def tts_capability_available() -> bool:
    """True when platform TTS generation can run — a usable default TTS
    provider in either context (mirrors :func:`build_generate_provider`'s
    resolution). Gates the tts MCP out of the assignable set."""
    return _provider_usable(_default_tts_row())


def build_generate_provider(*, provider_id: int | None = None,
                            advanced_overrides: dict | None = None):
    """Resolve + build the TTS provider for file generation (tts-mcp / the
    ``/v1/audio/tts/generate`` endpoint).

    Explicit id, else chat default, else calls default. ``advanced_overrides``
    (per-call model_id / voice settings) merge into a COPY of the row — cloud
    providers only: a cached local provider is shared state, so overrides on it
    are ignored rather than mutating a cross-request instance.

    Returns ``(instance, row)``. Raises ``AudioUnavailableError`` if no provider
    is configured or the engine isn't implemented.
    """
    if provider_id is not None:
        row = audio_provider_store.get_provider(provider_id)
        if not row or row["provider_type"] != "tts":
            raise AudioUnavailableError(f"No tts provider with id {provider_id}")
    else:
        row = _default_tts_row()
        if not row:
            raise AudioUnavailableError("No default TTS provider configured")
    try:
        cls = registry.get_provider_class("tts", row["provider_name"])
    except (KeyError, ImportError) as e:
        raise AudioUnavailableError(f"{row['provider_name']} unavailable: {e}")
    if advanced_overrides:
        if getattr(cls.capabilities, "is_local", False):
            logging.getLogger("claude-proxy").warning(
                "TTS generate: ignoring per-call overrides for local provider %s",
                row["provider_name"],
            )
        else:
            row = dict(row)
            row["advanced"] = {**(row.get("advanced") or {}), **advanced_overrides}
    try:
        inst = registry.get_or_build_provider(row, credential_resolver)
    except (KeyError, NotImplementedError, ImportError) as e:
        raise AudioUnavailableError(f"{row['provider_name']} unavailable: {e}")
    return inst, row


def build_chat_provider(provider_type: str, *, provider_id: int | None = None):
    """Resolve + build the STT/TTS provider for chat (explicit id or chat default).

    Returns ``(instance, row)``. Raises ``AudioUnavailableError`` if no provider
    is configured or the engine isn't implemented.
    """
    if provider_id is not None:
        row = audio_provider_store.get_provider(provider_id)
        if not row or row["provider_type"] != provider_type:
            raise AudioUnavailableError(f"No {provider_type} provider with id {provider_id}")
    else:
        row = audio_provider_store.get_default_provider(provider_type, "chat")
        if not row:
            raise AudioUnavailableError(f"No default chat {provider_type} provider configured")
    try:
        inst = registry.get_or_build_provider(row, credential_resolver)
    except (KeyError, NotImplementedError, ImportError) as e:
        raise AudioUnavailableError(f"{row['provider_name']} unavailable: {e}")
    return inst, row


# ---------------------------------------------------------------------------
# Capability resolution
# ---------------------------------------------------------------------------

@dataclass
class ChatAudioCapability:
    tts: str                    # one of _VALID_AVAIL
    stt: str
    tts_provider_id: int | None
    stt_provider_id: int | None
    reason: str
    icons_enabled: bool


def _resolve_modality(provider_type: str, policy: str, has_native: bool) -> tuple[str, int | None, str]:
    default = audio_provider_store.get_default_provider(provider_type, "chat")
    platform_ok = _provider_usable(default)
    pid = default["id"] if (default and platform_ok) else None

    if policy == "native_only":
        if has_native:
            return "native_only", None, "admin policy is native-only"
        return "unavailable", None, "native-only policy and no native support on this device"
    if policy == "native_preferred":
        if has_native:
            return "native_only", None, "native available (admin policy: native preferred)"
        if platform_ok:
            return "platform", pid, "no native support; using platform provider"
        return "unavailable", None, "no native support and no platform provider configured"
    # user_choice
    if has_native and platform_ok:
        return "either", pid, "user choice"
    if platform_ok:
        return "platform", pid, "no native support; using platform provider"
    if has_native:
        return "native_only", None, "no platform provider; using native"
    return "unavailable", None, "no native support and no platform provider configured"


def chat_audio_enabled() -> bool:
    """Admin kill-switch for chat audio (sound/mic icons). Absent ⇒ enabled;
    only an explicit "false" disables — availability is otherwise derived from
    the policy and the native/provider state per request."""
    return task_store.get_platform_setting("audio_chat_enabled") != "false"


def resolve_chat_audio_capability(*, has_native_tts: bool, has_native_stt: bool) -> ChatAudioCapability:
    """Resolve what the chat sound/mic icons can do for this request.

    Read on-demand (never cached) so a mid-session admin policy / kill-switch
    change is reflected on the next interaction. Gated by ``audio_chat_enabled``
    (default on).
    """
    if not chat_audio_enabled():
        return ChatAudioCapability(
            "unavailable", "unavailable", None, None,
            "chat audio is turned off by the administrator", False,
        )
    policy = task_store.get_platform_setting("audio_chat_user_policy") or "native_preferred"
    tts, tpid, treason = _resolve_modality("tts", policy, has_native_tts)
    stt, spid, sreason = _resolve_modality("stt", policy, has_native_stt)
    return ChatAudioCapability(tts, stt, tpid, spid, f"TTS: {treason}. STT: {sreason}.", True)


def is_native_only_policy() -> bool:
    """Server-side gate for the STT/TTS session mints: when the
    policy is native-only the platform audio WS must be refused even if hit
    directly (voice mode then uses the device's own native engine)."""
    return (task_store.get_platform_setting("audio_chat_user_policy") or "native_preferred") == "native_only"


# ---------------------------------------------------------------------------
# Cost recording
# ---------------------------------------------------------------------------

def record_audio_usage(
    user_sub: str, source_type: str, provider_name: str, provider, *,
    chars: int = 0, seconds: float = 0.0,
) -> None:
    """Compute cost from the provider's billing metadata and record one usage
    row (captures ``audio_seconds`` even when free/$0)."""
    unit = provider.billing_unit()
    rate = provider.cost_per_unit()
    if unit == "char":
        cost = chars * rate
    elif unit == "minute":
        cost = (seconds / 60.0) * rate
    else:  # "second"
        cost = seconds * rate
    task_store.insert_usage_record(
        user_sub, "", "user", source_type, None, round(cost, 6),
        provider=provider_name,
        audio_seconds=round(seconds, 2) if seconds else None,
        billing_unit=unit,
    )


# ---------------------------------------------------------------------------
# TTS rate limiting (per-user char budget)
# ---------------------------------------------------------------------------
# The login rate limiter is request-count/per-IP only; chat TTS needs a
# per-user character budget. Sliding 60s window, in-process (v1).
_tts_windows: dict[str, list[tuple[float, int]]] = {}


def check_tts_rate(user_sub: str, chars: int) -> tuple[bool, int]:
    """Returns (allowed, retry_after_s). Consumes the budget when allowed."""
    cap = int(task_store.get_platform_setting("audio_tts_rate_limit_chars_per_min") or 10000)
    now = time.time()
    window = [(ts, c) for ts, c in _tts_windows.get(user_sub, []) if ts > now - 60]
    if sum(c for _, c in window) + chars > cap:
        _tts_windows[user_sub] = window
        retry = int(60 - (now - window[0][0])) if window else 60
        return False, max(1, retry)
    window.append((now, chars))
    _tts_windows[user_sub] = window
    return True, 0


# ---------------------------------------------------------------------------
# Stream cancellation
# ---------------------------------------------------------------------------

async def stream_with_cancellation(chunks, request, *, on_close=None):
    """Yield chunks from ``chunks`` until the client disconnects, then run
    ``on_close`` (provider cleanup) in a finally — so a closed tab doesn't leave
    an upstream provider stream draining tokens."""
    try:
        async for chunk in chunks:
            if await request.is_disconnected():
                break
            yield chunk
    finally:
        if on_close is not None:
            with contextlib.suppress(Exception):
                await on_close()

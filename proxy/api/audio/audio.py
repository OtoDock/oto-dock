"""Audio API — STT / TTS providers, chat audio policy, shared audio settings,
and per-user audio preferences.

Providers (the ``audio_providers`` table) are the single source of truth for
which STT/TTS engines exist, their per-language voices, endpointing, and which
is the default for calls vs chat. Credentials live in ``infra_credentials``
keyed by the provider's ``credential_key`` (inner key ``API_KEY``), set through
the per-provider credential endpoint.

The phone-server / route / call-prompt admin surface lives in ``api/phone/phone.py``;
this router owns everything "audio".
"""

import asyncio
import io
import logging
import wave
from dataclasses import asdict

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile,
)
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from audio.providers import registry
from audio.providers.tts.base import UnsupportedProviderOperation
from audio.streaming.text_chunks import split_sentences
from audio.streaming import tts_stream
from audio.streaming import lang
from auth.providers import UserContext, get_current_user, mask_email, require_admin
from services.media import audio_service
from services.media import ws_audio_token
from storage import audio_provider_store
from storage import credential_store
from storage import database as task_store
from storage import user_audio_prefs_store
from storage.audio_provider_store import (
    CREDENTIAL_INNER_KEY as AUDIO_CREDENTIAL_KEY,
    ProviderDefaultDisabledError,
)
from services.phone.phone_config import notify_phone_config_changed, groq_classifier_configured

logger = logging.getLogger("claude-proxy")
router = APIRouter()


def _int_setting(key: str, default: int) -> int:
    try:
        return int(task_store.get_platform_setting(key) or default)
    except (TypeError, ValueError):
        return default

# Audio-domain platform settings exposed via /v1/admin/audio/settings (the
# ``audio_`` prefix is stripped on the wire). Shared by chat audio + telephony.
_AUDIO_SETTING_PREFIX = "audio_"

# Chat-audio policy keys (dedicated /policy endpoint; chat-only, no phone push).
_POLICY_KEYS = {
    "chat_enabled": "audio_chat_enabled",
    "chat_user_policy": "audio_chat_user_policy",
    "show_experimental": "audio_show_experimental",
}
_VALID_POLICIES = {"native_only", "native_preferred", "user_choice"}


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ProviderCreate(BaseModel):
    provider_type: str  # 'stt' | 'tts'
    provider_name: str
    label: str = ""
    credential_key: str | None = None
    enabled_for_calls: bool = True
    enabled_for_chat: bool = True
    voices: dict = {}
    advanced: dict = {}


class ProviderUpdate(BaseModel):
    label: str | None = None
    credential_key: str | None = None
    enabled_for_calls: bool | None = None
    enabled_for_chat: bool | None = None
    voices: dict | None = None
    advanced: dict | None = None


class CredentialSet(BaseModel):
    value: str


class PolicyUpdate(BaseModel):
    chat_enabled: bool | None = None
    chat_user_policy: str | None = None
    show_experimental: bool | None = None


class AudioPrefsUpdate(BaseModel):
    stt_mode: str | None = None
    tts_mode: str | None = None
    tts_voice_map: dict | None = None
    stt_language: str | None = None


# ---------------------------------------------------------------------------
# Provider helpers
# ---------------------------------------------------------------------------

def _credential_configured(credential_key: str | None) -> bool:
    if not credential_key:
        return False
    creds = credential_store.get_infra_credentials(credential_key)
    return bool(creds.get(AUDIO_CREDENTIAL_KEY, ""))


def _provider_class(provider_type: str, provider_name: str):
    """Resolve the engine class, or None for names the registry doesn't know."""
    try:
        return registry.get_provider_class(provider_type, provider_name)
    except KeyError:
        return None


def _decorate(provider: dict) -> dict:
    """Attach the credential-configured flag + the engine's advanced-settings
    defaults (single source of truth: the provider class) for the pill UI."""
    cls = _provider_class(provider.get("provider_type", ""), provider.get("provider_name", ""))
    return {
        **provider,
        "credential_configured": _credential_configured(provider.get("credential_key")),
        "advanced_defaults": cls.default_advanced_settings() if cls else {},
    }


# ---------------------------------------------------------------------------
# Providers CRUD
# ---------------------------------------------------------------------------

@router.get("/v1/admin/audio/providers")
async def list_providers(user: UserContext | None = Depends(get_current_user)):
    require_admin(user)
    providers = await asyncio.to_thread(audio_provider_store.get_all_providers)
    return {"providers": [_decorate(p) for p in providers]}


@router.get("/v1/admin/audio/known-providers")
async def known_providers(user: UserContext | None = Depends(get_current_user)):
    """The IMPLEMENTED provider engines — drives the "Add provider" dropdown
    (the UI offers the ones not already added). Stubs (`is_stub = True`) are
    hidden until a real implementation lands."""
    require_admin(user)

    def _implemented(ptype: str, names) -> list[str]:
        out = []
        for name in names:
            cls = _provider_class(ptype, name)
            if cls is not None and not cls.is_stub:
                out.append(name)
        return sorted(out)

    return {
        "stt": _implemented("stt", registry.KNOWN_STT_PROVIDERS),
        "tts": _implemented("tts", registry.KNOWN_TTS_PROVIDERS),
    }


@router.post("/v1/admin/audio/providers")
async def create_provider(
    req: ProviderCreate, user: UserContext | None = Depends(get_current_user),
):
    u = require_admin(user)
    if req.provider_type not in ("stt", "tts"):
        raise HTTPException(status_code=400, detail="provider_type must be 'stt' or 'tts'")
    data = req.model_dump()
    # Seed the engine's advanced defaults so a freshly added provider never runs
    # on hidden built-in fallbacks (e.g. Deepgram chat endpointing). An explicit
    # `advanced` from the client wins; unknown engines stay {}.
    if not data.get("advanced"):
        cls = _provider_class(req.provider_type, req.provider_name)
        data["advanced"] = cls.default_advanced_settings() if cls else {}
    try:
        provider = await asyncio.to_thread(audio_provider_store.create_provider, data)
    except Exception as e:  # unique (provider_type, provider_name) collision, etc.
        raise HTTPException(status_code=400, detail=f"Could not create provider: {e}")
    logger.info(f"Admin {mask_email(u.email)} created audio provider: {provider['provider_name']} ({provider['provider_type']})")
    await notify_phone_config_changed()
    return _decorate(provider)


@router.put("/v1/admin/audio/providers/{provider_id}")
async def update_provider(
    provider_id: int, req: ProviderUpdate,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_admin(user)
    provider = await asyncio.to_thread(
        audio_provider_store.update_provider, provider_id, req.model_dump(exclude_unset=True),
    )
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    logger.info(f"Admin {mask_email(u.email)} updated audio provider: {provider_id}")
    await notify_phone_config_changed()
    return _decorate(provider)


@router.delete("/v1/admin/audio/providers/{provider_id}")
async def delete_provider(
    provider_id: int, user: UserContext | None = Depends(get_current_user),
):
    u = require_admin(user)
    in_use = await asyncio.to_thread(audio_provider_store.routes_using_provider, provider_id)
    if in_use:
        raise HTTPException(
            status_code=409,
            detail=f"Provider is used by {len(in_use)} route(s): {', '.join(in_use)}. "
                   "Re-assign or delete those routes first.",
        )
    deleted = await asyncio.to_thread(audio_provider_store.delete_provider, provider_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Provider not found")
    logger.info(f"Admin {mask_email(u.email)} deleted audio provider: {provider_id}")
    await notify_phone_config_changed()
    return {"status": "deleted"}


@router.put("/v1/admin/audio/providers/{provider_id}/default")
async def set_provider_default(
    provider_id: int,
    context: str = Query(..., pattern="^(calls|chat)$"),
    user: UserContext | None = Depends(get_current_user),
):
    u = require_admin(user)
    try:
        provider = await asyncio.to_thread(audio_provider_store.set_default, provider_id, context)
    except ProviderDefaultDisabledError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    logger.info(f"Admin {mask_email(u.email)} set audio provider {provider_id} default for {context}")
    await notify_phone_config_changed()
    return _decorate(provider)


# ---------------------------------------------------------------------------
# Provider credentials (encrypted via infra_credentials[credential_key])
# ---------------------------------------------------------------------------

@router.put("/v1/admin/audio/providers/{provider_id}/credential")
async def set_provider_credential(
    provider_id: int, req: CredentialSet,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_admin(user)
    provider = await asyncio.to_thread(audio_provider_store.get_provider, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    cred_key = provider.get("credential_key")
    if not cred_key:
        raise HTTPException(status_code=400, detail="Provider has no credential slot (local provider)")
    await asyncio.to_thread(
        credential_store.set_infra_credentials, cred_key, {AUDIO_CREDENTIAL_KEY: req.value},
    )
    logger.info(f"Admin {mask_email(u.email)} set credential for audio provider {provider_id}")
    await notify_phone_config_changed()
    return {"status": "saved"}


@router.delete("/v1/admin/audio/providers/{provider_id}/credential")
async def delete_provider_credential(
    provider_id: int, user: UserContext | None = Depends(get_current_user),
):
    u = require_admin(user)
    provider = await asyncio.to_thread(audio_provider_store.get_provider, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    cred_key = provider.get("credential_key")
    if cred_key:
        await asyncio.to_thread(credential_store.delete_infra_credentials, cred_key)
    logger.info(f"Admin {mask_email(u.email)} deleted credential for audio provider {provider_id}")
    await notify_phone_config_changed()
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Groq turn classifier (call-only; read-only "active" indicator)
# ---------------------------------------------------------------------------

@router.get("/v1/admin/audio/turn-classifier")
async def get_turn_classifier(user: UserContext | None = Depends(get_current_user)):
    """Whether the Groq turn classifier is active — i.e. a Groq path is
    configured in the Direct LLM execution layer (its single source): a BYO key
    or a hosted relay sub. The model is fixed and there is no enable toggle;
    per-language opt-in lives in the Languages section, and an inactive Groq falls
    back to Smart Turn.
    """
    require_admin(user)
    configured = await asyncio.to_thread(groq_classifier_configured)
    return {"active": configured}


# ---------------------------------------------------------------------------
# Chat audio policy (chat-only; no phone push)
# ---------------------------------------------------------------------------

@router.get("/v1/admin/audio/policy")
async def get_audio_policy(user: UserContext | None = Depends(get_current_user)):
    require_admin(user)
    all_settings = await asyncio.to_thread(task_store.get_all_platform_settings)
    return {
        "chat_enabled": all_settings.get(_POLICY_KEYS["chat_enabled"], "true") != "false",
        "chat_user_policy": all_settings.get(_POLICY_KEYS["chat_user_policy"], "native_preferred"),
        "show_experimental": all_settings.get(_POLICY_KEYS["show_experimental"], "false") == "true",
    }


@router.put("/v1/admin/audio/policy")
async def update_audio_policy(
    req: PolicyUpdate, user: UserContext | None = Depends(get_current_user),
):
    u = require_admin(user)
    if req.chat_enabled is not None:
        await asyncio.to_thread(
            task_store.set_platform_setting,
            _POLICY_KEYS["chat_enabled"], "true" if req.chat_enabled else "false",
        )
    if req.chat_user_policy is not None:
        if req.chat_user_policy not in _VALID_POLICIES:
            raise HTTPException(status_code=400, detail=f"Invalid policy. Allowed: {sorted(_VALID_POLICIES)}")
        await asyncio.to_thread(
            task_store.set_platform_setting, _POLICY_KEYS["chat_user_policy"], req.chat_user_policy,
        )
    if req.show_experimental is not None:
        await asyncio.to_thread(
            task_store.set_platform_setting,
            _POLICY_KEYS["show_experimental"], "true" if req.show_experimental else "false",
        )
    logger.info(f"Admin {mask_email(u.email)} updated audio policy")
    return {"status": "updated"}


# ---------------------------------------------------------------------------
# Shared audio settings (audio_* keys; VAD / smart-turn — fed to phone server)
# ---------------------------------------------------------------------------

@router.get("/v1/admin/audio/settings")
async def get_audio_settings(user: UserContext | None = Depends(get_current_user)):
    """Audio settings with the ``audio_`` prefix stripped."""
    require_admin(user)
    all_settings = await asyncio.to_thread(task_store.get_all_platform_settings)
    return {
        k.removeprefix(_AUDIO_SETTING_PREFIX): v
        for k, v in all_settings.items()
        if k.startswith(_AUDIO_SETTING_PREFIX) and k not in _POLICY_KEYS.values()
    }


@router.put("/v1/admin/audio/settings")
async def update_audio_settings(
    req: dict, user: UserContext | None = Depends(get_current_user),
):
    """Partial update of audio_* settings. Keys arrive prefix-stripped."""
    u = require_admin(user)
    for key, value in req.items():
        if value is not None:
            await asyncio.to_thread(
                task_store.set_platform_setting, f"{_AUDIO_SETTING_PREFIX}{key}", str(value),
            )
    logger.info(f"Admin {mask_email(u.email)} updated audio settings: {list(req.keys())}")
    await notify_phone_config_changed()
    return {"status": "updated"}


# ---------------------------------------------------------------------------
# Per-user audio preferences
# ---------------------------------------------------------------------------

@router.get("/v1/users/me/audio-prefs")
async def get_my_audio_prefs(user: UserContext | None = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return await asyncio.to_thread(user_audio_prefs_store.get_prefs, user.sub)


@router.put("/v1/users/me/audio-prefs")
async def update_my_audio_prefs(
    req: AudioPrefsUpdate, user: UserContext | None = Depends(get_current_user),
):
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        prefs = await asyncio.to_thread(
            user_audio_prefs_store.upsert_prefs, user.sub, req.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return prefs


# ---------------------------------------------------------------------------
# Chat audio runtime endpoints (sound icon / mic icon / transcribe) — user-facing
# ---------------------------------------------------------------------------

class TTSRequest(BaseModel):
    text: str
    language: str | None = None
    voice_id: str | None = None
    provider_id: int | None = None


class STTSessionRequest(BaseModel):
    provider_id: int | None = None


class TTSSessionRequest(BaseModel):
    provider_id: int | None = None


@router.get("/v1/audio/capability")
async def get_audio_capability(
    has_native_tts: bool = False, has_native_stt: bool = False,
    user: UserContext | None = Depends(get_current_user),
):
    """What the chat sound/mic icons can do for this device. Read on-demand —
    never cached — so a mid-session policy/flag change is reflected next call."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    cap = await asyncio.to_thread(
        audio_service.resolve_chat_audio_capability,
        has_native_tts=has_native_tts, has_native_stt=has_native_stt,
    )
    return asdict(cap)


@router.post("/v1/audio/tts/synthesize")
async def tts_synthesize(
    req: TTSRequest, request: Request,
    user: UserContext | None = Depends(get_current_user),
):
    """Synthesize text → streamed raw PCM (24 kHz, ``audio/L16``) for the sound icon.

    Sentences are pushed to the provider's continuation context and audio is
    streamed back as it is produced (low time-to-first-audio); the browser plays
    it progressively via the Web Audio API, reading the sample rate from the
    ``X-Audio-Sample-Rate`` header. The guards below run BEFORE the first byte so
    they can still return a proper status code."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not audio_service.chat_audio_enabled():
        raise HTTPException(status_code=403, detail="Chat audio is disabled")
    if await asyncio.to_thread(audio_service.is_native_only_policy):
        raise HTTPException(status_code=403, detail="Admin policy: native speech only")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")
    max_chars = _int_setting("audio_tts_max_chars_per_request", 5000)
    if len(text) > max_chars:
        raise HTTPException(status_code=413, detail=f"Text exceeds {max_chars} characters")
    ok, retry = await asyncio.to_thread(audio_service.check_tts_rate, user.sub, len(text))
    if not ok:
        raise HTTPException(status_code=429, detail="TTS rate limit exceeded",
                            headers={"Retry-After": str(retry)})

    try:
        provider, row = await asyncio.to_thread(
            audio_service.build_chat_provider, "tts", provider_id=req.provider_id,
        )
    except audio_service.AudioUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e))

    tts_lang = lang.base_lang(req.language) if req.language else lang.detect_tts_language(text)
    if req.voice_id:
        provider.voice_id = req.voice_id
    else:
        provider.select_voice(tts_lang)

    try:
        await provider.connect()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TTS connect failed: {e}")

    chunks = split_sentences(text)

    async def _on_close():
        # Runs in the stream's finally (normal end OR client disconnect): close the
        # provider and record usage once (full chars — Cartesia bills the whole
        # transcript, submitted up front, regardless of early disconnect).
        try:
            await provider.close()
        finally:
            await asyncio.to_thread(
                audio_service.record_audio_usage,
                user.sub, "audio-tts-chat", row["provider_name"], provider, chars=len(text),
            )

    stream = tts_stream.stream_tts(
        provider, chunks, output_sample_rate=audio_service.CHAT_AUDIO_TARGET_RATE, language=tts_lang,
    )
    return StreamingResponse(
        audio_service.stream_with_cancellation(stream, request, on_close=_on_close),
        media_type="audio/L16",
        headers={
            "X-Audio-Sample-Rate": str(audio_service.CHAT_AUDIO_TARGET_RATE),
            "Cache-Control": "no-store",
        },
    )


@router.post("/v1/audio/stt/session")
async def mint_stt_session(
    req: STTSessionRequest, user: UserContext | None = Depends(get_current_user),
):
    """Mint a short-lived token for the STT WebSocket. Enforces the chat-audio
    policy server-side (native-only / disabled → 403) so the WS can't be reached
    by hitting it directly."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not audio_service.chat_audio_enabled():
        raise HTTPException(status_code=403, detail="Chat audio is disabled")
    if await asyncio.to_thread(audio_service.is_native_only_policy):
        raise HTTPException(status_code=403, detail="Admin policy: native speech only")

    provider_id = req.provider_id
    if provider_id is None:
        default = await asyncio.to_thread(
            audio_provider_store.get_default_provider, "stt", "chat",
        )
        if not default:
            raise HTTPException(status_code=503, detail="No chat STT provider configured")
        provider_id = default["id"]

    max_seconds = _int_setting("audio_chat_stt_max_seconds", 60)
    return ws_audio_token.create_ws_audio_token(user.sub, max_seconds=max_seconds, provider_id=provider_id)


@router.post("/v1/audio/tts/session")
async def mint_tts_session(
    req: TTSSessionRequest, user: UserContext | None = Depends(get_current_user),
):
    """Mint a short-lived token for the streaming TTS WebSocket (voice mode).

    Same gate as the STT mint: disabled flag / native-only policy → 403 (voice
    mode then speaks via the device's own native engine, which needs no token).
    Language + voice are chosen per-reply in the WS ``init`` frame, not here."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not audio_service.chat_audio_enabled():
        raise HTTPException(status_code=403, detail="Chat audio is disabled")
    if await asyncio.to_thread(audio_service.is_native_only_policy):
        raise HTTPException(status_code=403, detail="Admin policy: native speech only")

    provider_id = req.provider_id
    if provider_id is None:
        default = await asyncio.to_thread(
            audio_provider_store.get_default_provider, "tts", "chat",
        )
        if not default:
            raise HTTPException(status_code=503, detail="No chat TTS provider configured")
        provider_id = default["id"]

    max_chars = _int_setting("audio_tts_max_chars_per_session", 20000)
    return ws_audio_token.create_ws_audio_token(
        user.sub, purpose=ws_audio_token.PURPOSE_TTS, provider_id=provider_id, max_chars=max_chars,
    )


async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    """Read an upload in chunks, aborting (413) past ``max_bytes`` — never an
    unbounded ``.read()`` into memory."""
    out = bytearray()
    while True:
        chunk = await upload.read(1 << 20)  # 1 MiB
        if not chunk:
            break
        out.extend(chunk)
        if len(out) > max_bytes:
            raise HTTPException(status_code=413, detail="Uploaded file too large")
    return bytes(out)


@router.post("/v1/audio/transcribe")
async def transcribe_audio(
    file: UploadFile = File(...),
    language: str | None = Form(None),
    provider_id: int | None = Form(None),
    user: UserContext | None = Depends(get_current_user),
):
    """Transcribe an uploaded audio file → text + word timings (transcribe-mcp,
    meeting transcription). Cost is recorded here, not via a manifest costs
    block."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    max_mb = _int_setting("audio_transcribe_max_upload_mb", 100)
    data = await _read_capped(file, max_mb * 1024 * 1024)
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    try:
        provider, row = await asyncio.to_thread(
            audio_service.build_chat_provider, "stt", provider_id=provider_id,
        )
    except audio_service.AudioUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not provider.capabilities.supports_transcribe_file:
        raise HTTPException(status_code=400, detail=f"{row['provider_name']} cannot transcribe files")

    try:
        result = await provider.transcribe_file(data, language=language)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Transcription failed: {e}")

    max_dur_s = _int_setting("audio_transcribe_max_duration_min", 60) * 60
    if result.audio_seconds > max_dur_s:
        raise HTTPException(status_code=413, detail=f"Audio exceeds {max_dur_s // 60} minute limit")

    await asyncio.to_thread(
        audio_service.record_audio_usage,
        user.sub, "audio-transcribe", result.provider_used or row["provider_name"],
        provider, seconds=result.audio_seconds,
    )
    return {
        "text": result.text,
        "language": result.language,
        "audio_seconds": int(round(result.audio_seconds)),
        "provider_used": result.provider_used or row["provider_name"],
        "words": [{"word": w.word, "start": w.start, "end": w.end} for w in result.words],
    }


# ---------------------------------------------------------------------------
# Voice-over generation (tts-mcp) — file-oriented TTS + voice discovery.
# Session-token auth like /v1/audio/transcribe; deliberately NOT behind the
# chat-audio policy gate (that governs the browser sound/mic icons, not file
# generation). Remote machines reach these through the satellite HTTP tunnel —
# the paths are allowlisted on both sides.
# ---------------------------------------------------------------------------

# PCM rates the generate endpoint accepts (WAV output; matches what the cloud
# providers emit natively so no server-side resampling happens).
_GENERATE_RATES = (8000, 16000, 22050, 24000, 44100)


class TTSGenerateRequest(BaseModel):
    text: str = ""
    provider_id: int | None = None
    voice_id: str | None = None
    language: str | None = None
    model_id: str | None = None
    voice_settings: dict | None = None
    sample_rate: int = 24000


class TTSVoiceAddRequest(BaseModel):
    public_owner_id: str
    voice_id: str
    name: str | None = None
    provider_id: int | None = None


def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw s16le mono PCM in a WAV container (stdlib, no transcode)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


async def _build_generate_provider_or_503(req_provider_id, advanced_overrides):
    try:
        return await asyncio.to_thread(
            audio_service.build_generate_provider,
            provider_id=req_provider_id, advanced_overrides=advanced_overrides,
        )
    except audio_service.AudioUnavailableError as e:
        status = 404 if "with id" in str(e) else 503
        raise HTTPException(status_code=status, detail=str(e))


@router.post("/v1/audio/tts/generate")
async def tts_generate(
    req: TTSGenerateRequest, user: UserContext | None = Depends(get_current_user),
):
    """Synthesize text → a complete WAV (voice-over files for the tts MCP).

    Unlike ``tts_synthesize`` (the chat sound icon's policy-gated PCM stream),
    this returns a finished file and accepts per-call voice/model/setting
    overrides — mix-and-match voice discovery is the point. Cost is recorded
    here, not via a manifest costs block."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")
    max_chars = _int_setting("audio_tts_max_chars_per_request", 5000)
    if len(text) > max_chars:
        raise HTTPException(status_code=413, detail=f"Text exceeds {max_chars} characters")
    if req.sample_rate not in _GENERATE_RATES:
        raise HTTPException(status_code=400, detail=f"sample_rate must be one of {_GENERATE_RATES}")
    ok, retry = await asyncio.to_thread(audio_service.check_tts_rate, user.sub, len(text))
    if not ok:
        raise HTTPException(status_code=429, detail="TTS rate limit exceeded",
                            headers={"Retry-After": str(retry)})

    advanced_overrides: dict = {}
    if req.model_id:
        advanced_overrides["model_id"] = req.model_id
    if req.voice_settings:
        advanced_overrides.update(req.voice_settings)
    provider, row = await _build_generate_provider_or_503(
        req.provider_id, advanced_overrides or None,
    )

    tts_lang = lang.base_lang(req.language) if req.language else lang.detect_tts_language(text)
    is_local = bool(getattr(provider.capabilities, "is_local", False))
    if req.voice_id and not is_local:
        provider.voice_id = req.voice_id
    else:
        if req.voice_id and is_local:
            logger.warning("TTS generate: ignoring voice_id override for local provider")
        provider.select_voice(tts_lang)
    if not provider.voice_id and not is_local:
        raise HTTPException(
            status_code=400,
            detail="No voice configured for this provider — pass voice_id or set "
                   "the per-language voice map in the Audio tab",
        )

    try:
        await provider.connect()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TTS connect failed: {e}")

    pcm_parts: list[bytes] = []
    try:
        async for chunk in tts_stream.stream_tts(
            provider, split_sentences(text),
            output_sample_rate=req.sample_rate, language=tts_lang,
        ):
            pcm_parts.append(chunk)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TTS generation failed: {e}")
    finally:
        try:
            await provider.close()
        except Exception:
            pass

    pcm = b"".join(pcm_parts)
    if not pcm:
        raise HTTPException(
            status_code=502,
            detail="TTS produced no audio (check the provider credential, voice id "
                   "and model in the Audio tab)",
        )
    seconds = len(pcm) / (2 * req.sample_rate)
    await asyncio.to_thread(
        audio_service.record_audio_usage,
        user.sub, "audio-tts-generate", row["provider_name"], provider,
        chars=len(text), seconds=seconds,
    )
    return Response(
        content=_pcm_to_wav(pcm, req.sample_rate),
        media_type="audio/wav",
        headers={
            "X-Audio-Sample-Rate": str(req.sample_rate),
            "X-Audio-Seconds": f"{seconds:.2f}",
            "X-Provider-Used": row["provider_name"],
            "X-Voice-Used": provider.voice_id,
            "Cache-Control": "no-store",
        },
    )


@router.get("/v1/audio/tts/voices")
async def tts_voices(
    provider_id: int | None = Query(None),
    user: UserContext | None = Depends(get_current_user),
):
    """The TTS provider's voice catalog (workspace/premade voices for cloud
    engines with a voices API; the configured per-language map otherwise)."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    provider, row = await _build_generate_provider_or_503(provider_id, None)
    try:
        voices = await provider.list_voices()
    except UnsupportedProviderOperation as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Voice listing failed: {e}")
    return {
        "provider_id": row["id"],
        "provider_name": row["provider_name"],
        "configured": row.get("voices") or {},
        "voices": [asdict(v) for v in voices],
    }


@router.get("/v1/audio/tts/voices/search")
async def tts_voices_search(
    search: str | None = Query(None), language: str | None = Query(None),
    gender: str | None = Query(None), age: str | None = Query(None),
    category: str | None = Query(None), page: int = Query(0, ge=0),
    page_size: int = Query(20, ge=1, le=100),
    provider_id: int | None = Query(None),
    user: UserContext | None = Depends(get_current_user),
):
    """Search the provider's shared voice library (ElevenLabs today). Results
    carry ``owner_id`` — needed by the admin-only add endpoint before a shared
    voice becomes usable."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    provider, row = await _build_generate_provider_or_503(provider_id, None)
    try:
        voices = await provider.search_voice_library(
            search=search, language=language, gender=gender, age=age,
            category=category, page=page, page_size=page_size,
        )
    except UnsupportedProviderOperation as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Voice search failed: {e}")
    return {"provider_name": row["provider_name"], "voices": [asdict(v) for v in voices]}


@router.post("/v1/audio/tts/voices/add")
async def tts_voices_add(
    req: TTSVoiceAddRequest, user: UserContext | None = Depends(get_current_user),
):
    """Add a shared-library voice to the vendor workspace. Admin-only: this
    permanently consumes one of the account's limited voice slots."""
    require_admin(user)
    provider, row = await _build_generate_provider_or_503(req.provider_id, None)
    try:
        voice_id = await provider.add_library_voice(
            req.public_owner_id, req.voice_id, name=req.name,
        )
    except UnsupportedProviderOperation as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Voice add failed: {e}")
    return {"provider_name": row["provider_name"], "voice_id": voice_id}

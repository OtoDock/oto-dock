"""Short-lived bearer token for the chat audio WebSockets (``/ws/audio/stt`` and
``/ws/audio/tts``).

The dashboard POSTs to ``/v1/audio/{stt,tts}/session`` (cookie-authed), gets a
5-minute purpose-scoped JWT, then opens the WS and sends the token in the FIRST
JSON frame (never in the URL — query strings leak into access logs). The server
is the authority: the per-session cap (``max_seconds`` for
STT, ``max_chars`` for TTS) and ``provider_id`` are baked into the token, and
``jti`` is one-time-use (replay guard).

Mirrors ``auth.totp.create_2fa_session_token`` — same ``JWT_SECRET``, HS256, and
purpose-claim discipline. One mint/validate pair serves both
modalities via ``purpose`` (default STT, so the existing STT call sites — incl.
their tests — are unchanged).
"""

from __future__ import annotations

import time
import uuid

import jwt

import config

PURPOSE_STT = "audio_stt"
PURPOSE_TTS = "audio_tts"
PURPOSE_DUPLEX = "duplex_session"
_TTL = 300  # 5 minutes

# One-time-use guard: jti → expiry. In-process (single proxy worker in v1);
# a multi-worker deploy would move this to a shared TTL store.
_consumed_jtis: dict[str, float] = {}


def create_ws_audio_token(
    sub: str, *, provider_id: int | None,
    purpose: str = PURPOSE_STT, max_seconds: int = 0, max_chars: int = 0,
) -> dict:
    """Mint a token for one audio WS session. Returns the wire dict the client needs.

    ``purpose`` selects the modality (``PURPOSE_STT`` / ``PURPOSE_TTS``). The cap
    is per-modality: STT bakes ``max_seconds`` (cumulative audio), TTS bakes
    ``max_chars`` (cumulative synthesized text); the unused cap stays 0.
    """
    now = int(time.time())
    payload = {
        "sub": sub,
        "purpose": purpose,
        "jti": uuid.uuid4().hex,
        "max_seconds": int(max_seconds),
        "max_chars": int(max_chars),
        "provider_id": provider_id,
        "iat": now,
        "exp": now + _TTL,
    }
    token = jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")
    return {
        "ws_token": token,
        "expires_at": payload["exp"],
        "max_seconds": payload["max_seconds"],
        "max_chars": payload["max_chars"],
    }


def create_duplex_token(sub: str, *, chat_id: str, max_seconds: int) -> dict:
    """Mint a token for one full-duplex conversation session (``/ws/duplex``).

    Same discipline as the audio tokens (HS256, one-time ``jti``, token in the
    FIRST WS frame), plus the CHAT the session may attach to — the bridge
    enforces per-user chat access against these claims; the daemon's master
    key never grants user access. ``max_seconds`` is the whole-session budget
    (the bridge enforces it with a timer, not per-modality caps).
    """
    now = int(time.time())
    payload = {
        "sub": sub,
        "purpose": PURPOSE_DUPLEX,
        "jti": uuid.uuid4().hex,
        "chat_id": chat_id,
        "max_seconds": int(max_seconds),
        "iat": now,
        "exp": now + _TTL,
    }
    token = jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")
    return {
        "ws_token": token,
        "expires_at": payload["exp"],
        "max_seconds": payload["max_seconds"],
    }


def validate_ws_audio_token(token: str, *, purpose: str = PURPOSE_STT) -> dict | None:
    """Validate signature, ``purpose``, and expiry. Returns the claims, or None.

    Does NOT consume the jti — the WS handler calls ``consume_jti`` once it has
    accepted the connection, so a validation that never opens a socket doesn't
    burn the token.
    """
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None
    if payload.get("purpose") != purpose:
        return None
    return payload


def consume_jti(jti: str) -> bool:
    """Mark a token consumed. Returns False if already used (replay) or missing."""
    if not jti:
        return False
    now = time.time()
    # Prune expired entries opportunistically.
    if _consumed_jtis:
        for k in [k for k, exp in _consumed_jtis.items() if exp < now]:
            _consumed_jtis.pop(k, None)
    if jti in _consumed_jtis:
        return False
    _consumed_jtis[jti] = now + _TTL
    return True

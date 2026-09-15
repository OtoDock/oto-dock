"""Per-user audio preferences — chat sound (TTS) / mic (STT) icon behaviour.

One row per user (PK = ``user_sub``, FK → users ON DELETE CASCADE). All fields
have defaults, so ``get_prefs`` always returns a full dict even when the user
has never saved (no row yet). Consumed by the chat audio capability resolver;
the store + endpoint back the dashboard prefs page.

All functions are synchronous (called via asyncio.to_thread from async code).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from storage.pg import get_conn

# Defaults returned when a user has no saved row. ``auto`` = prefer native,
# fall back to platform (final policy is decided by the capability resolver).
DEFAULT_PREFS = {
    "stt_mode": "auto",
    "tts_mode": "auto",
    "tts_voice_map": {},
    "stt_language": None,
    "wake_word_enabled": False,
}

_MODES = ("native", "platform", "auto")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_field(value) -> dict:
    if isinstance(value, str):
        try:
            return json.loads(value) or {}
        except (ValueError, TypeError):
            return {}
    return value or {}


def _row_to_dict(row: dict) -> dict:
    d = dict(row)
    d["tts_voice_map"] = _json_field(d.get("tts_voice_map"))
    return d


def get_prefs(user_sub: str) -> dict:
    """Return the user's prefs, or the defaults (with ``user_sub``) if unset."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM user_audio_prefs WHERE user_sub = %s", (user_sub,)
        ).fetchone()
        if row:
            return _row_to_dict(row)
    return {"user_sub": user_sub, **DEFAULT_PREFS, "updated_at": None}


def upsert_prefs(user_sub: str, data: dict) -> dict:
    """Insert or update the user's prefs. Validates mode enums; partial updates
    merge onto whatever is already stored (or the defaults)."""
    current = get_prefs(user_sub)
    merged = {
        "stt_mode": data.get("stt_mode", current["stt_mode"]),
        "tts_mode": data.get("tts_mode", current["tts_mode"]),
        "tts_voice_map": data.get("tts_voice_map", current["tts_voice_map"]),
        "stt_language": data.get("stt_language", current["stt_language"]),
        "wake_word_enabled": bool(
            data.get("wake_word_enabled", current["wake_word_enabled"])
        ),
    }
    for mode_key in ("stt_mode", "tts_mode"):
        if merged[mode_key] not in _MODES:
            raise ValueError(f"{mode_key} must be one of {_MODES}")
    now = _now()
    with get_conn() as conn:
        row = conn.execute(
            """INSERT INTO user_audio_prefs
               (user_sub, stt_mode, tts_mode, tts_voice_map, stt_language,
                wake_word_enabled, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (user_sub) DO UPDATE SET
                   stt_mode = EXCLUDED.stt_mode,
                   tts_mode = EXCLUDED.tts_mode,
                   tts_voice_map = EXCLUDED.tts_voice_map,
                   stt_language = EXCLUDED.stt_language,
                   wake_word_enabled = EXCLUDED.wake_word_enabled,
                   updated_at = EXCLUDED.updated_at
               RETURNING *""",
            (
                user_sub,
                merged["stt_mode"],
                merged["tts_mode"],
                json.dumps(merged["tts_voice_map"] or {}),
                merged["stt_language"],
                merged["wake_word_enabled"],
                now,
            ),
        ).fetchone()
        conn.commit()
        return _row_to_dict(row)

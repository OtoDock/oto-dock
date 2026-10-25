"""The phone settings an admin may store (``PUT /v1/admin/phone/settings``).

Every ``phone_*`` key the seed writes and every key the daemon's
``ConfigManager`` reads, each with its value rule; the keys the config
assembler injects from other tables (the AMI coordinates, the endpointing
fallback) are not storable here. The daemon ignores a key it does not
read, so the list only tightens what the proxy stores: an unknown key or a
malformed value answers 400 with the key named, before anything is written.
Keys arrive prefix-stripped; a value is stored as the string the daemon
parses.
"""

from __future__ import annotations

import json
from collections.abc import Callable

Rule = Callable[[object], str]

#: The daemon's choices for the two words it reads.
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_BACKENDS = ("groq", "smart_turn")

_TRUE_WORDS = ("true", "1", "yes", "on")
_FALSE_WORDS = ("false", "0", "no", "off")


def _words(value: object, true_word: str, false_word: str) -> str:
    if isinstance(value, bool):
        return true_word if value else false_word
    word = str(value).strip().lower()
    if word in _TRUE_WORDS:
        return true_word
    if word in _FALSE_WORDS:
        return false_word
    raise ValueError(f"must be {true_word} or {false_word}")


def _yes_no(value: object) -> str:
    return _words(value, "true", "false")


def _on_off(value: object) -> str:
    return _words(value, "on", "off")


def _number(low: float, high: float, *, integer: bool = False) -> Rule:
    def rule(value: object) -> str:
        if isinstance(value, bool):
            raise ValueError(_range_words(low, high, integer)) from None
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(_range_words(low, high, integer)) from None
        if number != number or not low <= number <= high:
            raise ValueError(_range_words(low, high, integer)) from None
        if integer:
            if not number.is_integer():
                raise ValueError(_range_words(low, high, integer)) from None
            return str(int(number))
        return str(value).strip() if isinstance(value, str) else repr(number)
    return rule


def _range_words(low: float, high: float, integer: bool) -> str:
    kind = "a whole number" if integer else "a number"
    return f"must be {kind} between {low:g} and {high:g}"


def _choice(*words: str, fold: Callable[[str], str] = str) -> Rule:
    def rule(value: object) -> str:
        word = fold(str(value).strip())
        if word not in words:
            raise ValueError("must be one of " + ", ".join(words))
        return word
    return rule


def _text(max_chars: int) -> Rule:
    def rule(value: object) -> str:
        if isinstance(value, (list, dict, bool)):
            raise ValueError("must be text")
        text = str(value)
        if len(text) > max_chars:
            raise ValueError(f"must be at most {max_chars} characters")
        return text
    return rule


def _host(value: object) -> str:
    text = str(value).strip()
    if not text or len(text) > 253 or any(c.isspace() or c in "/\\" for c in text):
        raise ValueError("must be a host name or address")
    return text


def _per_language(shape: str) -> Rule:
    """A JSON object keyed by language: ``shape`` ``"texts"`` is a map of
    phrase keys to strings per language, ``"lists"`` a list of strings per
    language. A string value must parse to that; a mapping is stored as
    its JSON. The daemon's own reader is the judge of the keys inside."""
    def rule(value: object) -> str:
        if isinstance(value, str):
            text = value
            try:
                parsed = json.loads(text) if text.strip() else {}
            except json.JSONDecodeError:
                raise ValueError("must be JSON") from None
        elif isinstance(value, dict):
            parsed, text = value, json.dumps(value, ensure_ascii=False)
        else:
            raise ValueError("must be a JSON object keyed by language")
        if not isinstance(parsed, dict):
            raise ValueError("must be a JSON object keyed by language")
        for language, entry in parsed.items():
            if not isinstance(language, str):
                raise ValueError("must be a JSON object keyed by language")
            if shape == "texts":
                ok = isinstance(entry, dict) and all(
                    isinstance(k, str) and isinstance(v, str) for k, v in entry.items())
                if not ok:
                    raise ValueError(f"{language} must be an object of phrase keys to text")
            else:
                ok = isinstance(entry, list) and all(isinstance(v, str) for v in entry)
                if not ok:
                    raise ValueError(f"{language} must be a list of phrases")
        return text
    return rule


_port = _number(1, 65535, integer=True)
_count = _number(1, 100000, integer=True)
_seconds = _number(0, 3600)
_unit = _number(0, 1)

#: Key → rule. Grouped as the daemon's ``ConfigManager`` groups its reads.
PHONE_SETTINGS: dict[str, Rule] = {
    # Server
    "audiosocket_host": _host,
    "audiosocket_port": _port,
    "http_api_host": _host,
    "http_api_port": _port,
    "log_level": _choice(*_LOG_LEVELS, fold=str.upper),
    "debug_outgoing_audio": _on_off,
    # Limits
    "max_live_calls": _count,
    "max_outbound_calls": _count,
    "rate_limit_calls_per_min": _count,
    "rate_limit_register_per_min": _count,
    "rate_limit_twilio_per_min": _count,
    # Barge-in
    "bargein_threshold": _unit,
    "bargein_debounce_ms": _number(0, 10000, integer=True),
    "bargein_chunk_ratio": _unit,
    "bargein_silence_duration_ms": _number(0, 10000, integer=True),
    "bargein_timer_s": _number(0, 60),
    "bargein_confirm_timeout_s": _number(0, 60),
    "bargein_resume_grace_s": _number(0, 60),
    # Turn taking
    "turn_complete_timeout_s": _number(0, 60),
    "turn_incomplete_timeout_s": _number(0, 60),
    "turn_classifier_grace_s": _number(0, 60),
    "turn_classifier_default_backend": _choice(*_BACKENDS, fold=str.lower),
    "turn_classifier_lang_map": _text(2000),
    # Fillers, the bed, the breath
    "backchannel_min_segments": _number(0, 100, integer=True),
    "backchannel_min_gap_s": _number(0, 60),
    "thinking_filler_delay_s": _number(0, 60),
    "thinking_filler_repeat_delay_s": _number(0, 60),
    "background_sound_gain": _unit,
    "voice_texture": _unit,
    "breath_enabled": _yes_no,
    "breath_gain": _unit,
    "breath_min_gap_s": _number(0, 600),
    # TTS pacing
    "tts_buffer_chars": _number(1, 1000, integer=True),
    "tts_response_gap_s": _number(0, 10),
    "tts_prebuffer_ms": _number(0, 5000, integer=True),
    "tts_hold_max_s": _number(0, 60),
    # Timeouts
    "idle_timeout_s": _number(1, 3600, integer=True),
    "call_max_duration_s": _number(1, 86400, integer=True),
    "outbound_call_timeout_s": _number(1, 86400, integer=True),
    "question_answer_timeout_s": _number(1, 3600, integer=True),
    "opening_pregen_timeout_s": _seconds,
    "stt_mute_reconnect_s": _seconds,
    # The PIN gate and the AMI listeners
    "pin_entry_timeout_s": _number(1, 600),
    "pin_interdigit_timeout_s": _number(1, 120),
    "pin_max_attempts": _number(1, 10, integer=True),
    "ami_dtmf_listener": _on_off,
    # The call prompts and the per-language phrases
    "context_inbound": _text(50000),
    "context_outbound": _text(50000),
    "phrases": _per_language("texts"),
    "backchannel_phrases": _per_language("lists"),
    "thinking_phrases": _per_language("lists"),
}


class PhoneSettingError(ValueError):
    """An unknown key or a malformed value, with the key in the message."""


def check_phone_settings(changes: dict) -> dict[str, str]:
    """Every change checked before any is stored: the prefix-stripped keys
    and their values as the strings to write, or :class:`PhoneSettingError`
    naming the first offending key. ``None`` values are skipped (the
    dashboard's partial saves)."""
    out: dict[str, str] = {}
    for key, value in changes.items():
        if value is None:
            continue
        rule = PHONE_SETTINGS.get(str(key))
        if rule is None:
            raise PhoneSettingError(f"Unknown phone setting: {key}")
        try:
            out[str(key)] = rule(value)
        except ValueError as e:
            raise PhoneSettingError(f"{key}: {e}") from None
    return out

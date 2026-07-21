"""Unit tests for the chat-audio capability resolver.

Exercises the policy × native-support × provider-config matrix + the
``audio_chat_enabled`` kill-switch gate.
"""

from __future__ import annotations


from services.media import audio_service
from storage import audio_provider_store
from storage import credential_store
from storage import database as task_store


def _set(key, value):
    task_store.set_platform_setting(key, value)


def _usable_tts_default():
    p = audio_provider_store.create_provider({
        "provider_type": "tts", "provider_name": "cartesia", "credential_key": "audio-cartesia",
    })
    credential_store.set_infra_credentials("audio-cartesia", {"API_KEY": "sk-x"})
    audio_provider_store.set_default(p["id"], "chat")
    return p


def test_kill_switch_disabled_short_circuits(temp_db):
    _set("audio_chat_enabled", "false")
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=True, has_native_stt=True)
    assert cap.icons_enabled is False
    assert cap.tts == "unavailable" and cap.stt == "unavailable"


def test_enabled_by_default_when_key_absent(temp_db):
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=True, has_native_stt=True)
    assert cap.icons_enabled is True
    assert cap.tts == "native_only" and cap.stt == "native_only"


def test_native_only_uses_native_or_nothing(temp_db):
    _set("audio_chat_user_policy", "native_only")
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=True, has_native_stt=False)
    assert cap.tts == "native_only" and cap.tts_provider_id is None
    assert cap.stt == "unavailable"


def test_native_preferred_falls_back_to_platform(temp_db):
    _set("audio_chat_user_policy", "native_preferred")
    p = _usable_tts_default()
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=False, has_native_stt=False)
    assert cap.tts == "platform" and cap.tts_provider_id == p["id"]
    # native present → use native, ignore platform
    cap2 = audio_service.resolve_chat_audio_capability(has_native_tts=True, has_native_stt=False)
    assert cap2.tts == "native_only" and cap2.tts_provider_id is None


def test_user_choice_is_either_when_both_available(temp_db):
    _set("audio_chat_user_policy", "user_choice")
    p = _usable_tts_default()
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=True, has_native_stt=False)
    assert cap.tts == "either" and cap.tts_provider_id == p["id"]


def test_platform_unusable_without_credential(temp_db):
    _set("audio_chat_user_policy", "native_preferred")
    p = audio_provider_store.create_provider({
        "provider_type": "tts", "provider_name": "cartesia", "credential_key": "audio-cartesia",
    })
    audio_provider_store.set_default(p["id"], "chat")  # default but NO credential
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=False, has_native_stt=False)
    assert cap.tts == "unavailable"


def test_native_only_gate_helper(temp_db):
    _set("audio_chat_user_policy", "native_only")
    assert audio_service.is_native_only_policy() is True
    _set("audio_chat_user_policy", "user_choice")
    assert audio_service.is_native_only_policy() is False

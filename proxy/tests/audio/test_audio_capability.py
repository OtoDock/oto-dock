"""Unit tests for the chat-audio capability resolver.

Exercises the policy × native-support × provider-config matrix + the
``audio_chat_enabled`` kill-switch gate.
"""

from __future__ import annotations

import pytest

from services.media import audio_service
from storage import database as task_store
from storage.identity import credential_store
from storage.prefs import audio_provider_store


def _set(key, value):
    task_store.set_platform_setting(key, value)


def _usable_tts_default():
    p = audio_provider_store.create_provider({
        "provider_type": "tts", "provider_name": "cartesia", "credential_key": "audio-cartesia",
    })
    credential_store.set_infra_credentials("audio-cartesia", {"API_KEY": "sk-x"})
    audio_provider_store.set_default(p["id"], "chat")
    return p


def test_language_identify_reports_the_identifier_state(temp_db, monkeypatch):
    from audio.providers.lid import ecapa
    monkeypatch.setattr(ecapa, "identifier_available", lambda model_path=None: True)
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=True, has_native_stt=True)
    assert cap.language_identify is True
    _set("audio_language_identify_enabled", "false")
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=True, has_native_stt=True)
    assert cap.language_identify is False
    _set("audio_language_identify_enabled", "true")
    monkeypatch.setattr(ecapa, "identifier_available", lambda model_path=None: False)
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=True, has_native_stt=True)
    assert cap.language_identify is False


def test_the_proxy_prewarms_the_identifier_only_with_a_chat_stt_provider(temp_db, monkeypatch):
    from audio.providers.lid import ecapa

    from storage.identity import credential_store
    from storage.prefs import audio_provider_store
    monkeypatch.setattr(ecapa, "identifier_available", lambda model_path=None: True)
    assert audio_service.language_identify_prewarm_wanted() is False
    p = audio_provider_store.create_provider({
        "provider_type": "stt", "provider_name": "deepgram", "credential_key": "audio-deepgram",
    })
    credential_store.set_infra_credentials("audio-deepgram", {"API_KEY": "dg-x"})
    audio_provider_store.set_default(p["id"], "chat")
    assert audio_service.language_identify_prewarm_wanted() is True
    _set("audio_language_identify_enabled", "false")
    assert audio_service.language_identify_prewarm_wanted() is False


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


def _usable_stt_default():
    p = audio_provider_store.create_provider({
        "provider_type": "stt", "provider_name": "deepgram", "credential_key": "audio-deepgram",
    })
    credential_store.set_infra_credentials("audio-deepgram", {"API_KEY": "dg-x"})
    audio_provider_store.set_default(p["id"], "chat")
    return p


def test_apple_webkit_under_native_preferred_is_either_with_the_platform_by_default(temp_db):
    _set("audio_chat_user_policy", "native_preferred")
    p = _usable_stt_default()
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=False, has_native_stt=True, apple_webkit=True)
    assert cap.stt == "either" and cap.stt_provider_id == p["id"]
    assert "You pick who listens" in cap.reason
    # Chrome's recognizer under the same policy keeps the device answer.
    cap2 = audio_service.resolve_chat_audio_capability(has_native_tts=False, has_native_stt=True)
    assert cap2.stt == "native_only"
    # TTS never reads the flag.
    cap3 = audio_service.resolve_chat_audio_capability(has_native_tts=True, has_native_stt=True, apple_webkit=True)
    assert cap3.tts == "native_only" and cap3.tts_provider_id is None


def test_apple_webkit_without_a_provider_keeps_web_speech_and_says_so(temp_db):
    _set("audio_chat_user_policy", "native_preferred")
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=False, has_native_stt=True, apple_webkit=True)
    assert cap.stt == "native_only" and cap.stt_provider_id is None
    assert "Your device is used for dictation" in cap.reason
    assert cap.stt_automatic == "device"


def test_apple_webkit_under_native_only_keeps_web_speech(temp_db):
    _set("audio_chat_user_policy", "native_only")
    _usable_stt_default()
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=False, has_native_stt=True, apple_webkit=True)
    assert cap.stt == "native_only" and cap.stt_provider_id is None
    assert cap.stt_automatic == "device"


def test_the_automatic_pick_runs_on_the_platform_whenever_a_provider_exists(temp_db):
    p = _usable_stt_default()
    # Native preferred with a device recognizer: the answer is the device,
    # the Automatic pick the platform, the provider id along for its mint.
    _set("audio_chat_user_policy", "native_preferred")
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=False, has_native_stt=True)
    assert cap.stt == "native_only" and cap.stt_automatic == "platform" and cap.stt_provider_id == p["id"]
    _set("audio_chat_user_policy", "user_choice")
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=False, has_native_stt=True)
    assert cap.stt == "either" and cap.stt_automatic == "platform" and cap.stt_provider_id == p["id"]
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=False, has_native_stt=False)
    assert cap.stt == "platform" and cap.stt_automatic == "platform"


def test_the_automatic_pick_stays_on_the_device_without_a_provider_or_under_native_only(temp_db):
    _set("audio_chat_user_policy", "user_choice")
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=False, has_native_stt=True)
    assert cap.stt == "native_only" and cap.stt_automatic == "device" and cap.stt_provider_id is None
    _usable_stt_default()
    _set("audio_chat_user_policy", "native_only")
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=False, has_native_stt=True)
    assert cap.stt == "native_only" and cap.stt_automatic == "device" and cap.stt_provider_id is None



# The resolver's whole table, per modality: the policy, whether the device
# has the modality, whether a usable chat provider exists, and for dictation
# whether the device recognizer is Apple's WebKit. Every value applies per
# modality. Native preferred, Platform only and User's choice take the other
# side's engine where the chosen one is missing; Native only never does.
# (policy, device, provider, apple_webkit) -> (answer, provider rides, sentence key)
_TABLE = [
    ("native_only", True, True, False, "native_only", False, "device"),
    ("native_only", True, True, True, "native_only", False, "device"),
    ("native_only", True, False, False, "native_only", False, "device"),
    ("native_only", True, False, True, "native_only", False, "device"),
    ("native_only", False, True, False, "unavailable", False, "unavailable"),
    ("native_only", False, False, False, "unavailable", False, "unavailable"),
    ("native_preferred", True, True, True, "either", True, "either"),
    ("native_preferred", True, True, False, "native_only", False, "device"),
    ("native_preferred", True, False, True, "native_only", False, "device"),
    ("native_preferred", False, True, False, "platform", True, "platform"),
    ("native_preferred", False, False, False, "unavailable", False, "nothing"),
    ("platform_only", True, True, False, "platform", True, "platform_only"),
    ("platform_only", True, True, True, "platform", True, "platform_only"),
    ("platform_only", False, True, False, "platform", True, "platform_only"),
    ("platform_only", True, False, False, "native_only", False, "device"),
    ("platform_only", True, False, True, "native_only", False, "device"),
    ("platform_only", False, False, False, "unavailable", False, "nothing"),
    ("user_choice", True, True, False, "either", True, "either"),
    ("user_choice", True, True, True, "either", True, "either"),
    ("user_choice", False, True, False, "platform", True, "platform"),
    ("user_choice", True, False, False, "native_only", False, "device"),
    ("user_choice", True, False, True, "native_only", False, "device"),
    ("user_choice", False, False, False, "unavailable", False, "nothing"),
]


@pytest.mark.parametrize("policy,device,provider,apple_webkit,answer,rides,key", _TABLE)
def test_the_dictation_table(temp_db, policy, device, provider, apple_webkit, answer, rides, key):
    _set("audio_chat_user_policy", policy)
    p = _usable_stt_default() if provider else None
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=False, has_native_stt=device, apple_webkit=apple_webkit)
    assert cap.stt == answer
    # The provider id rides the answer where the platform listens, and the
    # Automatic pick's mint carries it on every answer but Native only's.
    automatic_rides = provider and policy != "native_only"
    assert cap.stt_provider_id == (p["id"] if (rides or automatic_rides) else None)
    assert cap.stt_automatic == ("platform" if automatic_rides else "device")
    assert cap.reason.endswith(audio_service._REASONS["stt"][key])


@pytest.mark.parametrize("policy,device,provider,apple_webkit,answer,rides,key", [row for row in _TABLE if not row[3]])
def test_the_voice_table_never_reads_the_webkit_flag(temp_db, policy, device, provider, apple_webkit, answer, rides, key):
    _set("audio_chat_user_policy", policy)
    p = _usable_tts_default() if provider else None
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=device, has_native_stt=False, apple_webkit=True)
    assert cap.tts == answer
    assert cap.tts_provider_id == (p["id"] if rides else None)
    assert cap.reason.startswith(audio_service._REASONS["tts"][key])


def test_platform_only_folds_the_pair_and_keeps_the_mints_open(temp_db):
    _set("audio_chat_user_policy", "platform_only")
    _usable_stt_default()
    _usable_tts_default()
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=True, has_native_stt=True)
    assert (cap.tts, cap.stt) == ("platform", "platform")
    assert cap.reason == "The platform's speech provider is used for voice and dictation."
    assert "TTS" not in cap.reason and "STT" not in cap.reason
    assert audio_service.is_native_only_policy() is False


def test_platform_only_with_one_provider_takes_the_device_for_the_other(temp_db):
    _set("audio_chat_user_policy", "platform_only")
    p = _usable_tts_default()
    cap = audio_service.resolve_chat_audio_capability(has_native_tts=True, has_native_stt=True)
    assert cap.tts == "platform" and cap.tts_provider_id == p["id"]
    assert cap.stt == "native_only" and cap.stt_provider_id is None and cap.stt_automatic == "device"
    assert cap.reason == "The platform's speech provider is used for voice. Your device is used for dictation."


def test_platform_provider_usable_reads_the_chat_default(temp_db):
    assert audio_service.platform_provider_usable("stt") is False
    _usable_stt_default()
    assert audio_service.platform_provider_usable("stt") is True
    assert audio_service.platform_provider_usable("tts") is False

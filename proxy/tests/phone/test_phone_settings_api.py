"""``PUT /v1/admin/phone/settings`` stores the allowlisted keys only, each
value checked, nothing written until every value passed."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user
from services.phone import phone_settings
from storage import database as task_store


@pytest.fixture
def client(temp_db, monkeypatch):
    from api.phone import phone as phone_router

    async def _quiet():
        return None
    monkeypatch.setattr(phone_router, "notify_phone_config_changed", _quiet)
    app = FastAPI()
    app.include_router(phone_router.router)

    async def _admin():
        return UserContext(sub="admin-sub", email="admin@test.com", name="Admin",
                           role="admin", agents=[], agent_roles={})

    app.dependency_overrides[get_current_user] = _admin
    return TestClient(app)


def _stored(key):
    return task_store.get_all_platform_settings().get(f"phone_{key}")


def test_known_keys_are_stored_as_the_daemon_reads_them(client):
    body = {
        "debug_outgoing_audio": "on", "breath_enabled": True, "tts_buffer_chars": "24",
        "bargein_threshold": "0.4", "log_level": "debug", "idle_timeout_s": 45,
        "turn_classifier_default_backend": "Groq", "ami_dtmf_listener": "false",
        "phrases": json.dumps({"en": {"goodbye": "Bye now."}}),
        "backchannel_phrases": {"el": ["ναι", "μμ"]},
    }
    assert client.put("/v1/admin/phone/settings", json=body).status_code == 200, body
    assert _stored("debug_outgoing_audio") == "on"
    assert _stored("breath_enabled") == "true"
    assert _stored("tts_buffer_chars") == "24"
    assert _stored("bargein_threshold") == "0.4"
    assert _stored("log_level") == "DEBUG"
    assert _stored("idle_timeout_s") == "45"
    assert _stored("turn_classifier_default_backend") == "groq"
    assert _stored("ami_dtmf_listener") == "off"
    assert json.loads(_stored("phrases")) == {"en": {"goodbye": "Bye now."}}
    assert json.loads(_stored("backchannel_phrases")) == {"el": ["ναι", "μμ"]}
    got = client.get("/v1/admin/phone/settings").json()
    assert got["debug_outgoing_audio"] == "on" and got["log_level"] == "DEBUG"


def test_an_unknown_key_answers_400_with_the_key_named(client):
    r = client.put("/v1/admin/phone/settings", json={"tts_buffer_chars": "30", "vad_threshold": "0.5"})
    assert r.status_code == 400 and "vad_threshold" in r.json()["detail"]
    assert _stored("tts_buffer_chars") is None          # nothing written before the refusal
    for key in ("ami_host", "deepgram_endpointing_ms", "phone_tts_buffer_chars"):
        assert client.put("/v1/admin/phone/settings", json={key: "x"}).status_code == 400


@pytest.mark.parametrize("key, value, words", [
    ("breath_enabled", "maybe", "true or false"),
    ("bargein_threshold", "1.5", "between 0 and 1"),
    ("tts_buffer_chars", "12.5", "whole number"),
    ("audiosocket_port", "70000", "between 1 and 65535"),
    ("log_level", "LOUD", "one of"),
    ("turn_classifier_default_backend", "llama", "one of"),
    ("phrases", "[1, 2]", "keyed by language"),
    ("phrases", json.dumps({"en": ["not", "an", "object"]}), "phrase keys"),
    ("backchannel_phrases", json.dumps({"en": "ναι"}), "list of phrases"),
    ("thinking_phrases", "not json", "JSON"),
    ("audiosocket_host", "", "host"),
    ("context_inbound", ["a", "list"], "text"),
    ("idle_timeout_s", True, "whole number"),
])
def test_a_malformed_value_answers_400_with_the_key_named(client, key, value, words):
    r = client.put("/v1/admin/phone/settings", json={key: value})
    assert r.status_code == 400, r.text
    assert r.json()["detail"].startswith(f"{key}:") and words in r.json()["detail"]
    assert _stored(key) is None


def test_a_bad_value_leaves_the_good_ones_unwritten(client):
    r = client.put("/v1/admin/phone/settings", json={"tts_buffer_chars": "30", "breath_gain": "7"})
    assert r.status_code == 400 and r.json()["detail"].startswith("breath_gain:")
    assert _stored("tts_buffer_chars") is None


def test_the_dashboard_payloads_pass():
    """What the dashboard writes today: the Advanced defaults, the prompts,
    the per-language phrases and the Logging switch."""
    advanced = {
        "bargein_threshold": "0.35", "bargein_debounce_ms": "300", "bargein_chunk_ratio": "0.5",
        "bargein_silence_duration_ms": "500", "bargein_timer_s": "0.6",
        "bargein_confirm_timeout_s": "3.0", "bargein_resume_grace_s": "1.0",
        "turn_complete_timeout_s": "1.0", "turn_incomplete_timeout_s": "2.0",
        "turn_classifier_grace_s": "0.0", "tts_buffer_chars": "20", "tts_response_gap_s": "0.4",
        "backchannel_min_segments": "1", "backchannel_min_gap_s": "0.4",
        "thinking_filler_delay_s": "0",
    }
    out = phone_settings.check_phone_settings(advanced)
    assert out == advanced
    out = phone_settings.check_phone_settings({
        "context_inbound": "You answer the phone.", "debug_outgoing_audio": "off",
        "thinking_phrases": json.dumps({"en": ["let me see", "one moment"]}),
        "audiosocket_port": "9092", "call_max_duration_s": "600", "ignored": None,
    })
    assert out["debug_outgoing_audio"] == "off" and "ignored" not in out
    with pytest.raises(phone_settings.PhoneSettingError, match="Unknown phone setting: nope"):
        phone_settings.check_phone_settings({"nope": "1"})

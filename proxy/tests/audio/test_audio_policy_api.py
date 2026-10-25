"""HTTP tests for audio policy, turn classifier, audio settings, and per-user
audio preferences.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user


@pytest.fixture
def client(temp_db):
    from api.audio import audio as audio_router

    app = FastAPI()
    app.include_router(audio_router.router)

    async def _admin():
        # ``user-admin`` is seeded by temp_db (needed for the prefs FK).
        return UserContext(sub="user-admin", email="admin@test.com", name="Admin",
                           role="admin", agents=[], agent_roles={})

    app.dependency_overrides[get_current_user] = _admin
    return TestClient(app)


# --- policy ---------------------------------------------------------------

def test_policy_default_and_update(client):
    p = client.get("/v1/admin/audio/policy").json()
    assert p["chat_enabled"] is True
    assert p["chat_user_policy"] == "native_preferred"
    assert p["show_experimental"] is False
    resp = client.put("/v1/admin/audio/policy", json={"chat_user_policy": "user_choice", "show_experimental": True})
    assert resp.status_code == 200
    p = client.get("/v1/admin/audio/policy").json()
    assert p["chat_user_policy"] == "user_choice" and p["show_experimental"] is True


def test_policy_chat_enabled_kill_switch(client):
    resp = client.put("/v1/admin/audio/policy", json={"chat_enabled": False})
    assert resp.status_code == 200
    assert client.get("/v1/admin/audio/policy").json()["chat_enabled"] is False
    resp = client.put("/v1/admin/audio/policy", json={"chat_enabled": True})
    assert resp.status_code == 200
    assert client.get("/v1/admin/audio/policy").json()["chat_enabled"] is True


def test_policy_rejects_invalid(client):
    r = client.put("/v1/admin/audio/policy", json={"chat_user_policy": "bogus"})
    assert r.status_code == 400


# --- turn classifier ------------------------------------------------------

def test_turn_classifier_active_from_direct_llm(client):
    # Active iff a Groq key exists in the Direct LLM execution layer (its single
    # source). No groq subscription yet → inactive; no enable/model fields.
    assert client.get("/v1/admin/audio/turn-classifier").json() == {"active": False}

    # Add a Groq key to the Direct LLM layer → the classifier becomes active.
    from storage.billing import subscription_store
    subscription_store.add_subscription(
        layer="direct-llm", provider="groq", auth_type="api_key",
        owner_sub="", contribute_platform=True,
        credential_data={"api_key": "gsk-test"},
    )
    assert client.get("/v1/admin/audio/turn-classifier").json()["active"] is True

    # The old enable/model + credential endpoints are gone.
    resp = client.put("/v1/admin/audio/turn-classifier", json={"enabled": False})
    assert resp.status_code == 405
    resp = client.put("/v1/admin/audio/turn-classifier/credential", json={"value": "x"})
    assert resp.status_code == 404


# --- audio settings -------------------------------------------------------

def test_audio_settings_roundtrip_and_policy_excluded(client):
    resp = client.put("/v1/admin/audio/settings", json={"vad_threshold": "0.55"})
    assert resp.status_code == 200
    s = client.get("/v1/admin/audio/settings").json()
    assert s["vad_threshold"] == "0.55"
    # Policy keys live behind /policy, not the settings bag.
    client.put("/v1/admin/audio/policy", json={"chat_user_policy": "native_only"})
    s = client.get("/v1/admin/audio/settings").json()
    assert "chat_user_policy" not in s and "show_experimental" not in s
    assert "chat_enabled" not in s


# --- user prefs -----------------------------------------------------------

def test_language_candidates_are_validated_to_the_supported_set(client):
    resp = client.put("/v1/admin/audio/settings", json={"language_candidates": "el-GR, xx, en, el"})
    assert resp.status_code == 200
    assert client.get("/v1/admin/audio/settings").json()["language_candidates"] == "el,en"
    client.put("/v1/admin/audio/settings", json={"language_candidates": "zz"})
    assert client.get("/v1/admin/audio/settings").json()["language_candidates"] == "en,el,de,es,fr,it"


def test_identifier_settings_are_validated(client):
    # The cap is fixed (8), never this host's CPU count: the daemon clamps
    # the value again to its own machine on read.
    for raw, want in (("0", "1"), ("2", "2"), ("9999", "8"), (" 1 ", "1")):
        assert client.put("/v1/admin/audio/settings", json={"language_identify_threads": raw}).status_code == 200
        assert client.get("/v1/admin/audio/settings").json()["language_identify_threads"] == want
    assert client.put("/v1/admin/audio/settings", json={"language_identify_threads": "many"}).status_code == 400
    for raw, want in (("yes", "true"), ("0", "false"), ("True", "true"), ("no", "false")):
        assert client.put("/v1/admin/audio/settings", json={"language_identify_enabled": raw}).status_code == 200
        assert client.get("/v1/admin/audio/settings").json()["language_identify_enabled"] == want
    assert client.put("/v1/admin/audio/settings", json={"language_identify_enabled": "maybe"}).status_code == 400


def test_user_audio_prefs_roundtrip(client):
    p = client.get("/v1/users/me/audio-prefs").json()
    assert p["stt_mode"] == "auto" and p["tts_voice_map"] == {}
    r = client.put("/v1/users/me/audio-prefs", json={"tts_mode": "platform", "stt_language": "en"})
    assert r.status_code == 200 and r.json()["tts_mode"] == "platform"
    assert client.get("/v1/users/me/audio-prefs").json()["stt_language"] == "en"


def test_user_audio_prefs_rejects_bad_mode(client):
    r = client.put("/v1/users/me/audio-prefs", json={"stt_mode": "bogus"})
    assert r.status_code == 400


def test_audio_settings_are_checked_before_any_is_written(client):
    # A bad value after a good one: nothing is written.
    r = client.put("/v1/admin/audio/settings", json={"vad_threshold": "0.61", "language_identify_threads": "many"})
    assert r.status_code == 400
    assert client.get("/v1/admin/audio/settings").json().get("vad_threshold") != "0.61"
    # A JSON array or object is refused, never stored as its text.
    for bad in (["a", "b"], {"k": "v"}):
        r = client.put("/v1/admin/audio/settings", json={"wake_word_threshold": bad})
        assert r.status_code == 400 and "wake_word_threshold" in r.json()["detail"]
    assert "wake_word_threshold" not in client.get("/v1/admin/audio/settings").json()

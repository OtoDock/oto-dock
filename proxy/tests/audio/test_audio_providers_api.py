"""HTTP tests for the audio provider admin API.

Covers provider CRUD, default uniqueness per (type, context), the
"default ⇒ enabled" invariant, credential status, and the FK-RESTRICT
409 when a phone route still references a provider.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user


@pytest.fixture
def client(temp_db):
    from api.audio import audio as audio_router
    from api.phone import phone as phone_router

    app = FastAPI()
    app.include_router(audio_router.router)
    app.include_router(phone_router.router)

    async def _admin():
        return UserContext(sub="admin-sub", email="admin@test.com", name="Admin",
                           role="admin", agents=[], agent_roles={})

    app.dependency_overrides[get_current_user] = _admin
    return TestClient(app)


def _create(client, **kw):
    body = {"provider_type": "stt", "provider_name": f"p-{uuid.uuid4().hex[:6]}", **kw}
    r = client.post("/v1/admin/audio/providers", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_create_list_update_delete(client):
    p = _create(client, provider_type="tts", provider_name="cartesia", voices={"en": "v1"})
    assert p["provider_type"] == "tts" and p["voices"]["en"] == "v1"
    assert p["credential_configured"] is False

    lst = client.get("/v1/admin/audio/providers").json()["providers"]
    assert any(x["id"] == p["id"] for x in lst)

    r = client.put(f"/v1/admin/audio/providers/{p['id']}", json={"label": "Cartesia TTS"})
    assert r.status_code == 200 and r.json()["label"] == "Cartesia TTS"

    r = client.delete(f"/v1/admin/audio/providers/{p['id']}")
    assert r.status_code == 200
    assert all(x["id"] != p["id"] for x in client.get("/v1/admin/audio/providers").json()["providers"])


def test_default_unique_per_type_and_context(client):
    a = _create(client, provider_type="stt", provider_name="deepgram")
    b = _create(client, provider_type="stt", provider_name="canary")
    resp = client.put(f"/v1/admin/audio/providers/{a['id']}/default?context=calls")
    assert resp.status_code == 200
    resp = client.put(f"/v1/admin/audio/providers/{b['id']}/default?context=calls")
    assert resp.status_code == 200
    providers = {x["id"]: x for x in client.get("/v1/admin/audio/providers").json()["providers"]}
    assert providers[a["id"]]["is_default_calls"] is False
    assert providers[b["id"]]["is_default_calls"] is True
    # calls/chat defaults are independent
    resp = client.put(f"/v1/admin/audio/providers/{a['id']}/default?context=chat")
    assert resp.status_code == 200
    providers = {x["id"]: x for x in client.get("/v1/admin/audio/providers").json()["providers"]}
    assert providers[a["id"]]["is_default_chat"] is True
    assert providers[b["id"]]["is_default_calls"] is True


def test_cannot_default_a_disabled_context(client):
    p = _create(client, enabled_for_calls=False)
    r = client.put(f"/v1/admin/audio/providers/{p['id']}/default?context=calls")
    assert r.status_code == 400
    assert "enabled" in r.json()["detail"].lower()


def test_disabling_a_context_demotes_its_default(client):
    p = _create(client)
    resp = client.put(f"/v1/admin/audio/providers/{p['id']}/default?context=calls")
    assert resp.status_code == 200
    r = client.put(f"/v1/admin/audio/providers/{p['id']}", json={"enabled_for_calls": False})
    assert r.status_code == 200
    assert r.json()["is_default_calls"] is False
    assert r.json()["enabled_for_calls"] is False


def test_credential_set_and_status(client):
    p = _create(client, credential_key="audio-deepgram")
    resp = client.put(f"/v1/admin/audio/providers/{p['id']}/credential", json={"value": "sk-test"})
    assert resp.status_code == 200
    fetched = next(x for x in client.get("/v1/admin/audio/providers").json()["providers"] if x["id"] == p["id"])
    assert fetched["credential_configured"] is True
    assert client.delete(f"/v1/admin/audio/providers/{p['id']}/credential").status_code == 200
    fetched = next(x for x in client.get("/v1/admin/audio/providers").json()["providers"] if x["id"] == p["id"])
    assert fetched["credential_configured"] is False


def test_credential_requires_slot(client):
    p = _create(client, credential_key=None)
    r = client.put(f"/v1/admin/audio/providers/{p['id']}/credential", json={"value": "x"})
    assert r.status_code == 400


def test_delete_blocked_when_route_uses_provider(client):
    stt = _create(client, provider_type="stt", provider_name="deepgram")
    srv = client.post("/v1/admin/phone-servers", json={"name": "pbx"}).json()
    # Routes provision against a bootstrap-verified server.
    client.post(f"/v1/admin/phone-servers/{srv['id']}/bootstrap/verify")
    route = client.post("/v1/admin/phone/routes", json={
        "direction": "inbound", "agent": "ag", "phone_server_id": srv["id"],
        "stt_provider_id": stt["id"],
    })
    assert route.status_code == 200, route.text
    r = client.delete(f"/v1/admin/audio/providers/{stt['id']}")
    assert r.status_code == 409
    assert "route" in r.json()["detail"].lower()


def test_create_seeds_engine_advanced_defaults(client):
    # No advanced sent -> seeded from the provider class (defaults-on-add);
    # the model ids are placeholders (model_defaults), never stored, so a
    # row follows a later default.
    p = _create(client, provider_type="stt", provider_name="deepgram")
    assert p["advanced"] == {"call_endpointing_ms": 500, "chat_endpointing_ms": 1500}
    assert p["advanced_defaults"] == {"call_endpointing_ms": 500, "chat_endpointing_ms": 1500}
    assert p["model_defaults"] == {"model_id": "nova-3"} and p["voice_defaults"] == {}
    e = _create(client, provider_type="tts", provider_name="elevenlabs")
    assert "model_id" not in e["advanced"] and e["model_defaults"] == {"model_id": "eleven_flash_v2_5"}
    assert set(e["voice_defaults"]) == {"en", "el", "de", "es", "fr", "it"}
    # Explicit advanced wins; unknown engines stay {}.
    q = _create(client, provider_type="tts", provider_name="cartesia",
                advanced={"model_id": "sonic-x"})
    assert q["advanced"] == {"model_id": "sonic-x"}
    assert q["model_defaults"] == {"model_id": "sonic-3.6"} and q["voice_defaults"]["de"]
    r = _create(client)  # random unknown engine name
    assert r["advanced"] == {} and r["advanced_defaults"] == {} and r["model_defaults"] == {}


def test_the_boot_walk_moves_retired_ids_and_clears_a_former_default_once(client, monkeypatch):
    from audio.providers import model_succession

    from services.phone import phone_config_seed
    from storage import database as task_store
    from storage.prefs import audio_provider_store

    row = _create(client, provider_type="tts", provider_name="cartesia",
                  advanced={"model_id": "sonic-2"})
    # An elevenlabs row holding a Cartesia id, an unknown provider holding a
    # Cartesia id: the walk keys on the row's own provider, so neither moves;
    # an admin's explicit pick of a served id (sonic-3.5) stays.
    other = _create(client, provider_type="tts", provider_name="elevenlabs",
                    advanced={"model_id": "sonic-3.5"})
    unknown = audio_provider_store.create_provider({
        "provider_type": "tts", "provider_name": f"cartesia-{uuid.uuid4().hex[:4]}",
        "advanced": {"model_id": "sonic-3.5"}})
    current = _create(client, provider_type="stt", provider_name="deepgram",
                      advanced={"model_id": "nova-3"})
    task_store.set_platform_setting(phone_config_seed.FORMER_DEFAULTS_WALKED, "[]")

    # A retired id whose successor is the live default: the row follows the
    # default unpinned, so the next default moves it too.
    assert phone_config_seed.remap_retired_audio_models() == {"sonic-2": 1}
    assert "model_id" not in audio_provider_store.get_provider(row["id"])["advanced"]
    assert audio_provider_store.get_provider(other["id"])["advanced"]["model_id"] == "sonic-3.5"
    assert audio_provider_store.get_provider(unknown["id"])["advanced"]["model_id"] == "sonic-3.5"
    assert audio_provider_store.get_provider(current["id"])["advanced"]["model_id"] == "nova-3"
    audio_provider_store.update_provider(row["id"], {"advanced": {"model_id": "sonic-3.5"}})
    assert phone_config_seed.remap_retired_audio_models() == {}
    assert audio_provider_store.get_provider(row["id"])["advanced"]["model_id"] == "sonic-3.5"

    # A retired id whose successor is not the default keeps the successor.
    monkeypatch.setitem(model_succession.MODEL_SUCCESSORS, ("tts", "cartesia"), {"sonic-2": "sonic-3.5"})
    audio_provider_store.update_provider(row["id"], {"advanced": {"model_id": "sonic-2"}})
    assert phone_config_seed.remap_retired_audio_models() == {"sonic-2": 1}
    assert audio_provider_store.get_provider(row["id"])["advanced"]["model_id"] == "sonic-3.5"

    # A former default (an id a release seeded; none shipped today) is
    # cleared once...
    monkeypatch.setitem(model_succession.FORMER_DEFAULTS, ("tts", "cartesia"), ("sonic-3.5",))
    task_store.set_platform_setting(phone_config_seed.FORMER_DEFAULTS_WALKED, "[]")
    assert phone_config_seed.remap_retired_audio_models() == {"sonic-3.5": 1}
    assert "model_id" not in audio_provider_store.get_provider(row["id"])["advanced"]
    # ...and an admin's later pick of the same id stays on the next boot.
    audio_provider_store.update_provider(row["id"], {"advanced": {"model_id": "sonic-3.5"}})
    assert phone_config_seed.remap_retired_audio_models() == {}
    assert audio_provider_store.get_provider(row["id"])["advanced"]["model_id"] == "sonic-3.5"


def test_known_providers_hides_stubs(client):
    # canary/chatterbox stay stubs (hidden); elevenlabs shipped for both types.
    known = client.get("/v1/admin/audio/known-providers").json()
    assert known == {"stt": ["deepgram", "elevenlabs"], "tts": ["cartesia", "elevenlabs"]}

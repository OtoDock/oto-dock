"""Wake word — per-user opt-in pref + the /v1/users/me/wake-keywords compiler.

Covers the three seams the browser listener depends on:
- user_audio_prefs.wake_word_enabled (typed column, default OFF — privacy),
- the keyword compiler (BPE encoding against the committed model assets:
  fidelity, the bare-"HEY" guard for non-Latin names, collisions, favorite
  fallback),
- the endpoint payload + the platform kill switch / threshold knob.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user
from services.media import wake_keywords
from storage import agent_store
from storage import database as task_store
from storage import user_audio_prefs_store


# ---------------------------------------------------------------------------
# Encoder unit tests (real committed bpe.model/tokens.txt — no DB needed)
# ---------------------------------------------------------------------------

class TestNormalizeEncode:
    def test_normalize_strips_to_english_phonetic(self):
        assert wake_keywords.normalize_phrase("Personal Assistant") == "PERSONAL ASSISTANT"
        assert wake_keywords.normalize_phrase("Chloé") == "CHLOE"
        assert wake_keywords.normalize_phrase("🚀 Rocket-2") == "ROCKET"
        assert wake_keywords.normalize_phrase("Νίκος") == ""
        assert wake_keywords.normalize_phrase("") == ""

    def test_encode_matches_official_text2token(self):
        # The model tarball's own keywords_raw.txt → keywords.txt mapping.
        assert wake_keywords.encode_phrase("HELLO WORLD") == "▁HE LL O ▁WORLD"
        assert wake_keywords.encode_phrase("HEY SIRI") == "▁HE Y ▁S I RI"

    def test_encode_rejects_out_of_vocab(self):
        assert wake_keywords.encode_phrase("Νίκος") is None
        assert wake_keywords.encode_phrase("") is None

    def test_platform_phrases_encode_to_pinned_chains(self):
        # Silent-drop guard: encode_phrase returning None makes a platform
        # line vanish with only an opaque line-count failure downstream.
        # These chains are the MEASURED 4/4-recall spellings (2026-08-15
        # TTS-through-real-engine experiment) — pin them exactly.
        expected = {
            "HEY OTO DOCK": "▁HE Y ▁O T O ▁DO CK",
            "HEY OTTO DOCK": "▁HE Y ▁O T T O ▁DO CK",
            "HEY AUTODOCK": "▁HE Y ▁A U T O D O CK",
            "HEY AUTO DOCK": "▁HE Y ▁A U T O ▁DO CK",
        }
        assert set(wake_keywords.PLATFORM_PHRASES) == set(expected)
        for phrase, chain in expected.items():
            assert wake_keywords.encode_phrase(phrase) == chain


# ---------------------------------------------------------------------------
# Store: the typed opt-in column
# ---------------------------------------------------------------------------

class TestWakeWordPref:
    def test_default_off(self, temp_db):
        prefs = user_audio_prefs_store.get_prefs("user-viewer")
        assert prefs["wake_word_enabled"] is False

    def test_toggle_roundtrip_preserves_other_fields(self, temp_db):
        user_audio_prefs_store.upsert_prefs("user-viewer", {"stt_language": "el"})
        out = user_audio_prefs_store.upsert_prefs(
            "user-viewer", {"wake_word_enabled": True}
        )
        assert out["wake_word_enabled"] is True
        assert out["stt_language"] == "el"  # partial merge kept it
        out = user_audio_prefs_store.upsert_prefs(
            "user-viewer", {"wake_word_enabled": False}
        )
        assert out["wake_word_enabled"] is False


# ---------------------------------------------------------------------------
# Endpoint + compiler composition
# ---------------------------------------------------------------------------

def _make_client(user: UserContext) -> TestClient:
    from api.audio import audio as audio_router

    app = FastAPI()
    app.include_router(audio_router.router)

    async def _user():
        return user

    app.dependency_overrides[get_current_user] = _user
    return TestClient(app)


def _member(agents: list[str], default: str = "") -> UserContext:
    return UserContext(sub="user-viewer", email="viewer@test.com", name="U",
                       role="member", agents=agents, default_agent=default,
                       agent_roles={})


@pytest.fixture
def three_agents(temp_db):
    agent_store.create_agent("alpha", "Alpha Helper", created_by="user-admin")
    agent_store.create_agent("beta", "Beta Buddy", created_by="user-admin")
    agent_store.create_agent("gamma", "Gamma Guide", created_by="user-admin")


class TestWakeKeywordsEndpoint:
    def test_prefs_endpoint_roundtrips_wake_toggle(self, temp_db):
        client = _make_client(_member([]))
        assert client.get("/v1/users/me/audio-prefs").json()["wake_word_enabled"] is False
        r = client.put("/v1/users/me/audio-prefs", json={"wake_word_enabled": True})
        assert r.status_code == 200 and r.json()["wake_word_enabled"] is True
        assert client.get("/v1/users/me/audio-prefs").json()["wake_word_enabled"] is True

    def test_member_gets_only_accessible_agents(self, three_agents):
        client = _make_client(_member(["alpha", "beta"], default="beta"))
        data = client.get("/v1/users/me/wake-keywords").json()
        assert data["enabled"] is True
        assert [a["slug"] for a in data["agents"]] == ["alpha", "beta"]
        assert all(a["wakeable"] for a in data["agents"])
        lines = data["keywords"].splitlines()
        # one line per wakeable agent + the platform-word VARIANTS
        # (letter-token chains — measured 2026-08-15: the acoustic model
        # emits the letters; word-phonetic respellings scored zero recall)
        assert len(lines) == 6
        assert lines[0].endswith(" @alpha") and lines[1].endswith(" @beta")
        # every line is "<bpe tokens> [modifiers] @slug"
        assert all(line.startswith("▁HE Y ") for line in lines)
        # agent lines carry NO per-line modifiers — globals apply to them
        assert all(":" not in line and "#" not in line for line in lines[:2])
        # every platform variant targets the favorite, tag last on the line
        platform_lines = lines[2:]
        assert len(platform_lines) == 4
        assert all(line.endswith(" @beta") for line in platform_lines)
        # the measured letter-token shapes; the dead word-phonetic family
        # ("▁OH ▁TO"-style) must be gone
        assert any("▁O T O ▁DO CK" in line for line in platform_lines)
        assert any("▁A U T O D O CK" in line for line in platform_lines)
        assert "▁OH ▁TO" not in data["keywords"]
        # per-line sensitivity riders on platform lines (defaults)
        assert all(" :2 " in line and " #0.2 " in line for line in platform_lines)
        assert data["platform_target"] == "beta"
        assert data["threshold"] == 0.30

    def test_favorite_falls_back_when_lost(self, three_agents):
        # default_agent points at an agent the user lost access to
        client = _make_client(_member(["alpha"], default="gamma"))
        data = client.get("/v1/users/me/wake-keywords").json()
        assert data["platform_target"] == "alpha"

    def test_admin_scoped_to_added_agents(self, three_agents):
        # Admins have blanket ACCESS but wake only compiles their ADDED
        # roster — a name added on another account must never voice-wake
        # here (live-hit 2026-08-15: "hey otodock" opened a foreign "Oto").
        admin = UserContext(sub="user-admin", email="admin@test.com", name="A",
                            role="admin", agents=["beta"], agent_roles={})
        data = _make_client(admin).get("/v1/users/me/wake-keywords").json()
        assert [a["slug"] for a in data["agents"]] == ["beta"]
        assert all(line.endswith(" @beta") for line in data["keywords"].splitlines())
        assert data["platform_target"] == "beta"

    def test_admin_with_nothing_added_is_strictly_disabled(self, three_agents):
        # Deliberate divergence from the Agents-page fallback-to-all: voice
        # waking needs an explicit attach, even for admins.
        admin = UserContext(sub="user-admin", email="admin@test.com", name="A",
                            role="admin", agents=[], agent_roles={})
        data = _make_client(admin).get("/v1/users/me/wake-keywords").json()
        assert data["enabled"] is False
        assert data["reason"] == "no wakeable agents"
        assert data["agents"] == []
        assert data["platform_target"] is None
        assert data["keywords"] == ""

    def test_non_latin_name_not_wakeable_and_no_bare_hey(self, temp_db):
        agent_store.create_agent("nikos", "Νίκος", created_by="user-admin")
        client = _make_client(_member(["nikos"], default="nikos"))
        data = client.get("/v1/users/me/wake-keywords").json()
        agents = {a["slug"]: a for a in data["agents"]}
        assert agents["nikos"]["wakeable"] is False
        # The trap: a stripped-empty name must not leave a keyword that is
        # just "HEY" (it would fire on every hey). Only the platform word
        # variants (longer than bare HEY) may remain. Strip the per-line
        # :boost/#threshold riders so the guard checks the tokens alone.
        for line in data["keywords"].splitlines():
            tokens = line.rsplit(" @", 1)[0]
            tokens = " ".join(
                p for p in tokens.split()
                if not p.startswith(":") and not p.startswith("#")
            )
            assert tokens != "▁HE Y"
        assert data["platform_target"] == "nikos"

    def test_display_name_collision_first_wins(self, temp_db):
        agent_store.create_agent("one", "Twin Name", created_by="user-admin")
        agent_store.create_agent("two", "Twin Name", created_by="user-admin")
        client = _make_client(_member(["one", "two"], default="one"))
        data = client.get("/v1/users/me/wake-keywords").json()
        agents = {a["slug"]: a for a in data["agents"]}
        assert agents["one"]["wakeable"] is True
        assert agents["two"]["wakeable"] is False  # duplicate keyword dropped

    def test_platform_kill_switch(self, three_agents):
        task_store.set_platform_setting("audio_wake_word_enabled", "false")
        client = _make_client(_member(["alpha"], default="alpha"))
        data = client.get("/v1/users/me/wake-keywords").json()
        assert data["enabled"] is False
        assert data["keywords"] == ""

    def test_threshold_setting_read(self, three_agents):
        task_store.set_platform_setting("audio_wake_word_threshold", "0.5")
        client = _make_client(_member(["alpha"], default="alpha"))
        assert client.get("/v1/users/me/wake-keywords").json()["threshold"] == 0.5

    def test_platform_line_tuning_settings(self, three_agents):
        task_store.set_platform_setting("audio_wake_word_platform_boost", "3.5")
        task_store.set_platform_setting(
            "audio_wake_word_platform_threshold", "0.15")
        client = _make_client(_member(["alpha"], default="alpha"))
        lines = client.get("/v1/users/me/wake-keywords").json()["keywords"].splitlines()
        platform_lines = [ln for ln in lines if ":" in ln]
        assert platform_lines
        assert all(" :3.5 " in ln and " #0.15 " in ln for ln in platform_lines)

    def test_platform_line_tuning_junk_falls_back(self, three_agents):
        # A junk or non-positive admin value must NEVER reach the emitted
        # line: the spotter runs an unguarded stof on it (engine abort for
        # every user), and 0 means "use global" downstream.
        task_store.set_platform_setting("audio_wake_word_platform_boost", "abc")
        task_store.set_platform_setting("audio_wake_word_platform_threshold", "0")
        client = _make_client(_member(["alpha"], default="alpha"))
        lines = client.get("/v1/users/me/wake-keywords").json()["keywords"].splitlines()
        platform_lines = [ln for ln in lines if ":" in ln]
        assert platform_lines
        assert all(" :2 " in ln and " #0.2 " in ln for ln in platform_lines)

    def test_no_agents_disabled(self, temp_db):
        client = _make_client(_member([]))
        data = client.get("/v1/users/me/wake-keywords").json()
        assert data["enabled"] is False
        assert data["reason"] == "no wakeable agents"

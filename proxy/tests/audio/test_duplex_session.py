"""Full-duplex chat voice — capability gate, access checks, token, mint.

The duplex capability is deliberately fail-CLOSED (the plan's inverse of the
feature-flags convention): every gate must positively pass — explicit
``audio_chat_duplex_enabled=true``, usable chat STT+TTS, a connected daemon
advertising duplex — or the button never renders and the mint refuses.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user
from services.media import audio_service, duplex_service, ws_audio_token
from services.phone import phone_config
from storage.prefs import audio_provider_store
from storage.identity import credential_store
from storage import database as task_store


def _set(key, value):
    task_store.set_platform_setting(key, value)


def _usable_chat_providers():
    for ptype, name, cred in (("stt", "deepgram", "audio-deepgram"),
                              ("tts", "cartesia", "audio-cartesia")):
        p = audio_provider_store.create_provider({
            "provider_type": ptype, "provider_name": name, "credential_key": cred,
        })
        credential_store.set_infra_credentials(cred, {"API_KEY": "sk-x"})
        audio_provider_store.set_default(p["id"], "chat")


class _FakeWs:
    pass


@pytest.fixture
def duplex_daemon():
    ws = _FakeWs()
    phone_config.set_management_capabilities(
        ws, {"duplex": {"supported": True, "rates": [16000], "version": 1}})
    yield ws
    phone_config.clear_management_capabilities(ws)


# --- capability gate -------------------------------------------------------

def test_duplex_unavailable_by_default(temp_db):
    # No flag, no providers, no daemon → every reason path is fail-closed.
    cap = audio_service.duplex_capability()
    assert cap["available"] is False


def test_duplex_requires_explicit_flag(temp_db, duplex_daemon):
    _usable_chat_providers()
    assert audio_service.duplex_capability()["available"] is False  # flag absent
    _set("audio_chat_duplex_enabled", "true")
    assert audio_service.duplex_capability()["available"] is True


def test_duplex_needs_connected_daemon(temp_db):
    _usable_chat_providers()
    _set("audio_chat_duplex_enabled", "true")
    cap = audio_service.duplex_capability()
    assert cap["available"] is False
    assert "engine" in cap["reason"]


def test_duplex_gated_by_native_only_policy(temp_db, duplex_daemon):
    _usable_chat_providers()
    _set("audio_chat_duplex_enabled", "true")
    _set("audio_chat_user_policy", "native_only")
    assert audio_service.duplex_capability()["available"] is False


def test_duplex_needs_usable_providers(temp_db, duplex_daemon):
    _set("audio_chat_duplex_enabled", "true")
    cap = audio_service.duplex_capability()
    assert cap["available"] is False
    assert "provider" in cap["reason"]


def test_pick_duplex_client_prefers_newest(temp_db):
    old, new = _FakeWs(), _FakeWs()
    phone_config.set_management_capabilities(old, {"duplex": {"supported": True}})
    phone_config.set_management_capabilities(new, {"duplex": {"supported": True}})
    try:
        assert phone_config.pick_duplex_client() is new
        phone_config.clear_management_capabilities(new)
        assert phone_config.pick_duplex_client() is old
    finally:
        phone_config.clear_management_capabilities(old)
        phone_config.clear_management_capabilities(new)


def test_pre_duplex_daemon_has_no_capability(temp_db):
    ws = _FakeWs()
    phone_config.set_management_capabilities(ws, {})  # daemon sent empty caps
    try:
        assert phone_config.duplex_engine_available() is False
    finally:
        phone_config.clear_management_capabilities(ws)


# --- token -----------------------------------------------------------------

def test_duplex_token_round_trip(temp_db):
    minted = ws_audio_token.create_duplex_token(
        "user-admin", chat_id="chat-1", max_seconds=1800)
    claims = ws_audio_token.validate_ws_audio_token(
        minted["ws_token"], purpose=ws_audio_token.PURPOSE_DUPLEX)
    assert claims and claims["chat_id"] == "chat-1" and claims["sub"] == "user-admin"
    assert claims["max_seconds"] == 1800
    # Wrong purpose never validates; jti is one-time.
    assert ws_audio_token.validate_ws_audio_token(minted["ws_token"]) is None
    assert ws_audio_token.consume_jti(claims["jti"]) is True
    assert ws_audio_token.consume_jti(claims["jti"]) is False


# --- access checks ---------------------------------------------------------

def _mk_chat(chat_id, user_sub, agent="agent-a", source_type="chat"):
    task_store.create_chat(chat_id, user_sub, agent, "auto", source_type=source_type)


def _ctx(sub, role="member", agent_roles=None):
    agent_roles = agent_roles or {}
    return UserContext(sub=sub, email="", name="", role=role,
                       agents=list(agent_roles), agent_roles=agent_roles)


def test_access_owner_and_admin_allowed(temp_db):
    _mk_chat("c-own", "user-admin")
    assert duplex_service.chat_access_denied_reason(
        "c-own", _ctx("user-admin", role="admin")) is None
    assert duplex_service.chat_access_denied_reason(
        "c-own", _ctx("someone-else", role="admin")) is None


def test_access_foreign_chat_denied(temp_db):
    _mk_chat("c-foreign", "user-admin")
    denial = duplex_service.chat_access_denied_reason(
        "c-foreign", _ctx("user-b", agent_roles={"agent-a": "editor"}))
    assert denial == "Access denied"


def test_access_missing_chat_and_unknown_user(temp_db):
    assert duplex_service.chat_access_denied_reason(
        "c-nope", _ctx("user-admin", role="admin")) == "Chat not found"
    _mk_chat("c-own2", "user-admin")
    assert duplex_service.chat_access_denied_reason("c-own2", None) == "Access denied"


def test_access_follows_the_chat_owner_not_the_agent_mode(temp_db):
    """The by-id rule resume_chat binds by: on a Shared-only agent an
    assigned editor drives the ``agent::`` pool, never a colleague's chat
    from before the switch, and a phone call only as a manager."""
    from core.session import session_kind
    from core.session.visibility import PHONE_CHAT_OWNER, shared_chat_owner
    from storage.agents import agent_store
    agent_store.create_agent("agent-so", "Shared only", collaborative=False,
                             default_scope="agent")
    _mk_chat("c-pre", "user-admin", agent="agent-so")
    _mk_chat("c-pool", shared_chat_owner("agent-so"), agent="agent-so")
    _mk_chat("c-call", PHONE_CHAT_OWNER, agent="agent-so",
             source_type=session_kind.PHONE.source_type)
    editor = _ctx("user-b", agent_roles={"agent-so": "editor"})
    manager = _ctx("user-m", agent_roles={"agent-so": "manager"})
    assert duplex_service.chat_access_denied_reason("c-pre", editor) == "Access denied"
    assert duplex_service.chat_access_denied_reason("c-pool", editor) is None
    assert duplex_service.chat_access_denied_reason("c-call", editor) == "Access denied"
    assert duplex_service.chat_access_denied_reason("c-call", manager) is None


def test_access_pooled_chat_takes_the_editor_tier(temp_db):
    """A Shared-only pool chat runs as the agent: below editor the mint and
    the attach refuse with the tier's own sentence, an editor passes (the case
    above), and a personal chat's owner is untouched."""
    from core.session.visibility import shared_chat_owner
    from storage.agents import agent_store
    agent_store.create_agent("agent-so2", "Shared only", collaborative=False,
                             default_scope="agent")
    _mk_chat("c-pool2", shared_chat_owner("agent-so2"), agent="agent-so2")
    contributor = _ctx("user-c", agent_roles={"agent-so2": "contributor"})
    viewer = _ctx("user-v", agent_roles={"agent-so2": "viewer"})
    editor = _ctx("user-e", agent_roles={"agent-so2": "editor"})
    denial = duplex_service.chat_access_denied_reason("c-pool2", contributor)
    assert denial and "editor role" in denial
    assert duplex_service.chat_access_denied_reason("c-pool2", viewer)
    assert duplex_service.chat_access_denied_reason("c-pool2", editor) is None
    _mk_chat("c-own3", "user-c", agent="agent-a")
    assert duplex_service.chat_access_denied_reason("c-own3", contributor) is None


@pytest.mark.asyncio
async def test_attach_resolve_refuses_a_contributor_on_a_pooled_chat(temp_db):
    """The first utterance re-checks the tier from the live role: a
    contributor's pooled chat gets the sentence, an editor's proceeds to the
    session lookup."""
    from types import SimpleNamespace
    from core.session.visibility import shared_chat_owner
    from storage.agents import agent_store
    from ws import duplex_attach
    agent_store.create_agent("agent-so3", "Shared only", collaborative=False,
                             default_scope="agent")
    task_store.upsert_user("user-c3", "c3@test.com", "C3", "member")
    task_store.add_user_agent("user-c3", "agent-so3", "contributor", "test")
    task_store.upsert_user("user-e3", "e3@test.com", "E3", "member")
    task_store.add_user_agent("user-e3", "agent-so3", "editor", "test")
    _mk_chat("c-pool3", shared_chat_owner("agent-so3"), agent="agent-so3")
    try:
        got = await duplex_attach._resolve(
            SimpleNamespace(duplex_id="dx-c3", sub="user-c3", chat_id="c-pool3"))
        assert isinstance(got, str) and "editor role" in got
        got = await duplex_attach._resolve(
            SimpleNamespace(duplex_id="dx-e3", sub="user-e3", chat_id="c-pool3"))
        assert got == "no_session"
    finally:
        duplex_attach._states.pop("dx-c3", None)
        duplex_attach._states.pop("dx-e3", None)


# --- mint endpoint ---------------------------------------------------------

@pytest.fixture
def client(temp_db):
    from api.duplex import duplex as duplex_router

    app = FastAPI()
    app.include_router(duplex_router.router)

    async def _admin():
        return UserContext(sub="user-admin", email="admin@test.com", name="Admin",
                           role="admin", agents=[], agent_roles={})

    app.dependency_overrides[get_current_user] = _admin
    return TestClient(app)


def test_mint_refuses_while_unavailable(client):
    resp = client.post("/v1/duplex/session", json={"chat_id": "c-1"})
    assert resp.status_code == 503


def test_mint_full_path(client, duplex_daemon):
    _usable_chat_providers()
    _set("audio_chat_duplex_enabled", "true")
    _mk_chat("c-mine", "user-admin")
    resp = client.post("/v1/duplex/session", json={"chat_id": "c-mine"})
    assert resp.status_code == 200
    body = resp.json()
    claims = ws_audio_token.validate_ws_audio_token(
        body["ws_token"], purpose=ws_audio_token.PURPOSE_DUPLEX)
    assert claims["chat_id"] == "c-mine"
    # Unknown chat → 404 (availability passed, access failed).
    assert client.post("/v1/duplex/session",
                       json={"chat_id": "c-nope"}).status_code == 404


# --- prewarm endpoint ------------------------------------------------------

def test_prewarm_endpoint_access_and_spawn(client, temp_db, monkeypatch):
    """Wake-detection pre-warm: admin (or added agent) fires the detached
    spawn; a member without the agent is refused; the spawn is dispatched
    with the caller's identity."""
    from storage import database as task_store

    task_store.upsert_user("user-admin", "admin@test.com", "Admin", "admin")

    calls = []

    async def _fake_spawn(**kw):
        calls.append(kw)
        return "sid-123"
    import ws.dashboard  # noqa: F401  (the pre-warm module needs it loaded first)
    import ws.dashboard_prewarm as dp
    monkeypatch.setattr(dp, "spawn_detached_prewarm", _fake_spawn)

    resp = client.post("/v1/duplex/prewarm", json={"agent": "alpha"})
    assert resp.status_code == 200
    assert resp.json()["session_id"] == "sid-123"
    assert calls and calls[0]["agent"] == "alpha"
    assert calls[0]["user_sub"] == "user-admin"

    assert client.post("/v1/duplex/prewarm",
                       json={"agent": ""}).status_code == 422


def test_prewarm_endpoint_refuses_unadded_agent(temp_db, monkeypatch):
    from api.duplex import duplex as duplex_router
    from storage import database as task_store

    app = FastAPI()
    app.include_router(duplex_router.router)

    async def _member():
        return UserContext(sub="user-m", email="m@test.com", name="M",
                           role="member", agents=["mine"], agent_roles={})
    app.dependency_overrides[get_current_user] = _member
    c = TestClient(app)
    task_store.upsert_user("user-m", "m@test.com", "M", "member")

    assert c.post("/v1/duplex/prewarm",
                  json={"agent": "not-mine"}).status_code == 403

    called = []

    async def _fake_spawn(**kw):
        called.append(kw)
        return None
    import ws.dashboard  # noqa: F401  (the pre-warm module needs it loaded first)
    import ws.dashboard_prewarm as dp
    monkeypatch.setattr(dp, "spawn_detached_prewarm", _fake_spawn)
    assert c.post("/v1/duplex/prewarm",
                  json={"agent": "mine"}).status_code == 200
    assert called


def test_prewarm_registry_claim_by_key(temp_db):
    """claim_by_key pops only a full-key match (model included — CLI spawns
    can't swap models) and reports the entry's age."""
    import asyncio
    from core.session import prewarm_session_registry as reg

    async def run():
        await reg.register("sid-a", agent="alpha", user_sub="u1",
                           role="manager", exec_path="claude-code-cli",
                           model="claude-opus-5")
        miss = await reg.claim_by_key(user_sub="u1", agent="alpha",
                                      model="claude-sonnet-5", role="manager",
                                      exec_path="claude-code-cli")
        assert miss is None
        hit = await reg.claim_by_key(user_sub="u1", agent="alpha",
                                     model="claude-opus-5", role="manager",
                                     exec_path="claude-code-cli")
        assert hit is not None
        sid, age = hit
        assert sid == "sid-a" and age >= 0
        # popped — a second claim finds nothing
        assert await reg.claim(sid) is False
    asyncio.run(run())


# --- session config assembly ----------------------------------------------

def test_session_config_overlays_user_voice_picks(temp_db):
    """The user's dashboard voice picks (``tts_voice_map``) must OVERLAY the
    provider row's voices map in the duplex session config — the spoken
    conversation uses the same voice as every other chat TTS surface
    (live-hit 2026-08-11: fillers + replies played the row/stock voice,
    not the chosen one). Native-source picks (device voices the engine
    can't synthesize with) and malformed entries are ignored; languages
    the user never picked keep the row's voice."""
    from storage.prefs import user_audio_prefs_store

    p = audio_provider_store.create_provider({
        "provider_type": "tts", "provider_name": "elevenlabs",
        "credential_key": "audio-elevenlabs",
        "voices": {"en": "row-en-voice", "el": "row-el-voice"},
    })
    credential_store.set_infra_credentials("audio-elevenlabs", {"API_KEY": "sk-x"})
    audio_provider_store.set_default(p["id"], "chat")

    task_store.upsert_user("user-1", "u1@example.com", "U One", "member")
    user_audio_prefs_store.upsert_prefs("user-1", {"tts_voice_map": {
        "en-US": {"source": "platform", "voiceId": "picked-en-voice"},
        "el": {"source": "native", "voiceId": "device-voice"},   # ignored
        "de": "garbage",                                         # ignored
    }})

    cfg = phone_config.assemble_duplex_session_config("user-1")
    assert cfg["tts"]["voices"] == {
        "en": "picked-en-voice",     # user's pick wins, BCP-47 key normalized
        "el": "row-el-voice",        # native pick ignored → row voice kept
    }

    # A user with no picks keeps the row map untouched.
    cfg2 = phone_config.assemble_duplex_session_config("user-2")
    assert cfg2["tts"]["voices"] == {"en": "row-en-voice", "el": "row-el-voice"}

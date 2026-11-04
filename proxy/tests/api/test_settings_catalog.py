"""The settings catalog (api/settings): the generated rows load, every card
binding names a registered route and method, every row for every person,
the card's draw wherever a person is at the keyboard (a shared chat and a
chat another person owns included) and never in a session with nobody
there, and the card's answer for its viewer: the server says who changes a
row, the dashboard never guesses."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import config
from api.settings import catalog
from api.settings.settings import NO_PERSON_SIGNED_IN
from app import app
from auth import roles
from auth.providers import create_session_jwt
from core.session import session_kind, session_state, visibility
from tests.api.test_anonymous_bodies import _routes
from tests.conftest import live_session_token

ADMIN_ROWS = [e for e in catalog.ENTRIES if roles.is_admin(e["reads"])]


def _cookie(sub: str, email: str, name: str, role: str) -> TestClient:
    return TestClient(app, cookies={"session": create_session_jwt(sub, email, name, role)})


def _bearer(token: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


# ── the rows ───────────────────────────────────────────────────────────────

def test_the_catalog_loads_with_its_version_and_sentences():
    assert catalog.VERSION >= 1
    assert len(catalog.ENTRIES) > 30
    ids = [e["id"] for e in catalog.ENTRIES]
    assert len(set(ids)) == len(ids)
    for e in catalog.ENTRIES:
        assert e["where"] and e["who"] and e["help"], e["id"]
        assert isinstance(e["card"], bool)
        assert "current" not in e and "value_now" not in e
        # The two words the admin-or-not check reads; nothing in between.
        assert e["reads"] in (roles.MEMBER, roles.ADMIN)
        assert e["writes"] in (None, roles.MEMBER, roles.ADMIN)
    assert catalog.find(ids[0])["id"] == ids[0]
    assert catalog.find("user.general.no-such-row") is None


def test_every_card_binding_names_a_registered_route_and_method():
    """A renamed route, or a write whose method the route lacks, fails
    here, never a card in a chat."""
    registered = {(m, r.path) for r in _routes(app.routes) for m in (r.methods or ())}
    missing = sorted(b for b in catalog.bindings() if b not in registered)
    assert not missing, missing
    assert ("GET", "/v1/users/me/ui-prefs") in catalog.bindings()
    assert ("PUT", "/v1/admin/platform-settings") in catalog.bindings()


def test_writes_reads_the_rows_floor():
    row = next(e for e in catalog.ENTRIES if e["writes"] == roles.MEMBER)
    assert catalog.writes(row, roles.MEMBER) and catalog.writes(row, roles.ADMIN)
    reading = next(e for e in catalog.ENTRIES if e["writes"] is None)
    assert not catalog.writes(reading, roles.ADMIN)
    if ADMIN_ROWS:
        assert not catalog.writes(ADMIN_ROWS[0], roles.MEMBER)
        assert catalog.writes(ADMIN_ROWS[0], roles.ADMIN)


# ── the route ──────────────────────────────────────────────────────────────

def test_a_member_and_an_admin_read_every_row(temp_db):
    """No row carries a value, and the platform skill's map lists them all:
    a member's settings tool names an admin's setting too, with who changes
    it, so its card can be drawn in a chat an admin also reads."""
    member = _cookie("user-viewer", "viewer@test.com", "Viewer User", roles.MEMBER)
    r = member.get("/v1/settings/catalog")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == catalog.VERSION
    assert [e["id"] for e in body["entries"]] == [e["id"] for e in catalog.ENTRIES]
    admin = _cookie("user-admin", "admin@test.com", "Admin User", roles.ADMIN)
    assert len(admin.get("/v1/settings/catalog").json()["entries"]) == len(catalog.ENTRIES)


def test_a_session_token_reads_as_its_person(temp_db):
    token = live_session_token("sid-settings-member", "pa", user_sub="user-viewer")
    r = _bearer(token).get("/v1/settings/catalog")
    assert r.status_code == 200, r.text
    assert len(r.json()["entries"]) == len(catalog.ENTRIES)


def test_a_bearer_naming_nobody_is_refused(temp_db, monkeypatch):
    token = live_session_token("sid-settings-nobody", "pa")
    r = _bearer(token).get("/v1/settings/catalog")
    assert r.status_code == 403 and r.json()["detail"] == NO_PERSON_SIGNED_IN
    monkeypatch.setattr(config, "API_KEY", "master-key-for-the-test")
    r = _bearer("master-key-for-the-test").get("/v1/settings/catalog")
    assert r.status_code in (401, 403), r.text
    assert TestClient(app).get("/v1/settings/catalog").status_code == 401


@pytest.mark.parametrize("entry", ADMIN_ROWS, ids=lambda e: e["id"])
def test_an_admin_row_names_an_admin(entry):
    """An admin's row is changed by an admin, or by nobody (a reading)."""
    if entry["writes"] is None:
        assert entry["who"] == "Nobody changes it. The page only shows it."
    else:
        assert entry["writes"] == roles.ADMIN
        assert entry["who"] == "A platform administrator."


# ── the card route ─────────────────────────────────────────────────────────

from unittest.mock import patch

from api.settings.settings import (
    ADMIN_CHANGES_IT,
    NO_CARD,
    NO_SUCH_SETTING,
)
from services.cards import status as st
from storage import database as task_store
from ws import wire_events as wire

CARD_ROW = next(e for e in catalog.ENTRIES if e["card"] and e["writes"] == roles.MEMBER)
NO_CARD_ROW = next(e for e in catalog.ENTRIES if not e["card"])


def _own_session(sid: str, sub: str, chat_id: str) -> str:
    """A live session of ``sub`` whose chat row ``sub`` owns."""
    task_store.create_chat(chat_id, sub, "pa")
    task_store.update_chat(chat_id, session_id=sid)
    return live_session_token(sid, "pa", user_sub=sub)


def _drain(sid: str) -> list[dict]:
    q = session_state.get_permission_queue(sid)
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def _card(token: str, sid: str, setting: str):
    return _bearer(token).post("/v1/settings/card", json={"session_id": sid, "setting": setting})


def test_the_card_is_queued_in_the_persons_own_chat(temp_db):
    token = _own_session("sid-card-own", "user-viewer", "chat-card-own")
    r = _card(token, "sid-card-own", CARD_ROW["id"])
    assert r.status_code == 200, r.text
    assert _drain("sid-card-own") == [{
        "event_type": wire.ITEM_SETTINGS_CARD, "setting": CARD_ROW["id"],
        "label": CARD_ROW["label"], "catalog_version": catalog.VERSION,
    }]


def test_the_card_refuses_a_bearer_naming_nobody_and_a_cookie(temp_db, monkeypatch):
    token = live_session_token("sid-card-nobody", "pa")
    r = _card(token, "sid-card-nobody", CARD_ROW["id"])
    assert r.status_code == 403 and r.json()["detail"] == st.NO_PERSON
    monkeypatch.setattr(config, "API_KEY", "master-key-for-the-test")
    r = _card("master-key-for-the-test", "sid-card-nobody", CARD_ROW["id"])
    assert r.status_code in (401, 403)
    # A person's own sign-in is not a session: the card is the tool's to ask.
    member = _cookie("user-viewer", "viewer@test.com", "Viewer User", roles.MEMBER)
    r = member.post("/v1/settings/card", json={"session_id": "sid-card-nobody", "setting": CARD_ROW["id"]})
    assert r.status_code == 403 and r.json()["detail"] == st.NO_PERSON
    assert _drain("sid-card-nobody") == []


def test_the_card_refuses_another_session_a_meeting_and_a_session_with_no_chat(temp_db):
    token = _own_session("sid-card-a", "user-viewer", "chat-card-a")
    # The token is bound to its own session id.
    r = _card(token, "sid-card-b", CARD_ROW["id"])
    assert r.status_code == 403 and "session" in r.json()["detail"]
    # A meeting participant's session draws nothing in the parent chat.
    with patch("api.settings.settings.routing.resolve_hook_route") as route:
        route.return_value.is_meeting = True
        r = _card(token, "sid-card-a", CARD_ROW["id"])
    assert r.status_code == 403 and r.json()["detail"] == st.NO_PERSON
    gone = live_session_token("sid-card-none", "pa", user_sub="user-viewer")
    r = _card(gone, "sid-card-none", CARD_ROW["id"])
    assert r.status_code == 403 and r.json()["detail"] == st.NO_PERSON
    assert _drain("sid-card-a") == []


def test_the_card_draws_in_a_shared_chat_and_in_a_chat_another_person_owns(temp_db):
    """Whose chat it is plays no part: the card is live for whoever opens
    it, each with their own sign-in."""
    task_store.create_chat("chat-settings-shared", visibility.shared_chat_owner("pa"), "pa")
    task_store.update_chat("chat-settings-shared", session_id="sid-settings-shared")
    shared = live_session_token("sid-settings-shared", "pa", user_sub="user-viewer")
    assert _card(shared, "sid-settings-shared", CARD_ROW["id"]).status_code == 200
    assert [i["setting"] for i in _drain("sid-settings-shared")] == [CARD_ROW["id"]]
    task_store.create_chat("chat-settings-of-admin", "user-admin", "pa")
    task_store.update_chat("chat-settings-of-admin", session_id="sid-settings-foreign")
    foreign = live_session_token("sid-settings-foreign", "pa", user_sub="user-viewer")
    assert _card(foreign, "sid-settings-foreign", CARD_ROW["id"]).status_code == 200
    assert [i["setting"] for i in _drain("sid-settings-foreign")] == [CARD_ROW["id"]]


def test_the_card_never_draws_where_nobody_is_at_the_keyboard(temp_db, monkeypatch):
    """An agent-scope task's chat, a call, and a scheduled run whose token
    names its creator in the creator's own chat: nobody would read it then.
    A task chat a person reopened in the dashboard does draw."""
    for chat_id, owner, source_type in [
        ("chat-card-task", visibility.task_chat_owner("pa"), session_kind.TASK.source_type),
        ("chat-card-phone", visibility.PHONE_CHAT_OWNER, session_kind.PHONE.source_type),
        ("chat-card-phone-mine", "user-viewer", session_kind.PHONE.source_type),
    ]:
        task_store.create_chat(chat_id, owner, "pa", source_type=source_type)
        task_store.update_chat(chat_id, session_id=f"sid-{chat_id}")
        token = live_session_token(f"sid-{chat_id}", "pa", user_sub="user-viewer")
        r = _card(token, f"sid-{chat_id}", CARD_ROW["id"])
        assert r.status_code == 403 and r.json()["detail"] == st.NO_PERSON, chat_id
        assert _drain(f"sid-{chat_id}") == []
    # The session's own kind decides over the chat's: a scheduled run in a
    # dashboard chat of its creator draws nothing, a person's dashboard
    # session in a task chat does.
    task_store.create_chat("chat-card-wake", "user-viewer", "pa")
    task_store.update_chat("chat-card-wake", session_id="sid-card-run")
    monkeypatch.setitem(session_state._sessions, "sid-card-run", {"client_type": session_kind.TASK.name})
    token = live_session_token("sid-card-run", "pa", user_sub="user-viewer")
    r = _card(token, "sid-card-run", CARD_ROW["id"])
    assert r.status_code == 403 and r.json()["detail"] == st.NO_PERSON
    task_store.create_chat("chat-card-reopened", "user-viewer", "pa", source_type=session_kind.TASK.source_type)
    task_store.update_chat("chat-card-reopened", session_id="sid-card-reopened")
    monkeypatch.setitem(session_state._sessions, "sid-card-reopened", {"client_type": session_kind.DASHBOARD.name})
    token = live_session_token("sid-card-reopened", "pa", user_sub="user-viewer")
    assert _card(token, "sid-card-reopened", CARD_ROW["id"]).status_code == 200
    assert [i["setting"] for i in _drain("sid-card-reopened")] == [CARD_ROW["id"]]


def test_an_api_key_is_no_principal_and_a_task_runs_token_names_its_creator(temp_db):
    """A person's ``otok_`` key never reaches these routes (401 before any
    route runs). A user-scope task run's token names the task's creator: it
    reads the rows, and draws no card in the run's own chat, where nobody
    is at the keyboard."""
    member = _cookie("user-viewer", "viewer@test.com", "Viewer User", roles.MEMBER)
    r = member.post("/v1/user-api-keys", json={"name": "walk", "permissions": ["triggers"]})
    assert r.status_code == 200, r.text
    key = r.json()["key"]
    assert key.startswith("otok_")
    assert _bearer(key).get("/v1/settings/catalog").status_code == 401
    assert _card(key, "sid-task-run", CARD_ROW["id"]).status_code == 401
    task_store.create_chat("chat-task-run", "user-viewer", "pa",
                           source_type=session_kind.TASK.source_type)
    task_store.update_chat("chat-task-run", session_id="sid-task-run")
    token = live_session_token("sid-task-run", "pa", user_sub="user-viewer")
    r = _bearer(token).get("/v1/settings/catalog")
    assert r.status_code == 200 and len(r.json()["entries"]) == len(catalog.ENTRIES)
    r = _card(token, "sid-task-run", CARD_ROW["id"])
    assert r.status_code == 403 and r.json()["detail"] == st.NO_PERSON
    assert _drain("sid-task-run") == []


def test_the_card_refuses_an_unknown_row_and_a_row_with_no_card_and_draws_an_admins_row(temp_db):
    token = _own_session("sid-card-rows", "user-viewer", "chat-card-rows")
    r = _card(token, "sid-card-rows", "user.general.no-such-row")
    assert r.status_code == 404 and r.json()["detail"] == NO_SUCH_SETTING
    r = _card(token, "sid-card-rows", NO_CARD_ROW["id"])
    assert r.status_code == 409 and r.json()["detail"] == NO_CARD
    assert _drain("sid-card-rows") == []
    # An admin's row draws for a member too: the card tells each viewer
    # what the server lets them do, and an admin in the chat changes it.
    r = _card(token, "sid-card-rows", ADMIN_CARD_ROW["id"])
    assert r.status_code == 200, r.text
    assert [i["setting"] for i in _drain("sid-card-rows")] == [ADMIN_CARD_ROW["id"]]


# ── the card's answer for its viewer ──────────────────────────────────────

ADMIN_CARD_ROW = next(e for e in ADMIN_ROWS if e["card"])


def _viewer_card(client: TestClient, setting: str):
    return client.get(f"/v1/settings/card/{setting}")


def test_the_viewer_learns_from_the_server_whether_they_change_the_row(temp_db):
    member = _cookie("user-viewer", "viewer@test.com", "Viewer User", roles.MEMBER)
    admin = _cookie("user-admin", "admin@test.com", "Admin User", roles.ADMIN)
    r = _viewer_card(member, CARD_ROW["id"])
    assert r.status_code == 200 and r.json() == {"setting": CARD_ROW["id"], "label": CARD_ROW["label"],
                                                 "changes": True}, r.text
    r = _viewer_card(member, ADMIN_CARD_ROW["id"])
    assert r.status_code == 403 and r.json()["detail"] == ADMIN_CHANGES_IT
    assert _viewer_card(admin, ADMIN_CARD_ROW["id"]).json()["changes"] is True
    assert _viewer_card(admin, CARD_ROW["id"]).status_code == 200


def test_the_viewers_answer_refuses_an_unknown_row_a_row_with_no_card_and_any_bearer(temp_db):
    member = _cookie("user-viewer", "viewer@test.com", "Viewer User", roles.MEMBER)
    r = _viewer_card(member, "user.general.no-such-row")
    assert r.status_code == 404 and r.json()["detail"] == NO_SUCH_SETTING
    r = _viewer_card(member, NO_CARD_ROW["id"])
    assert r.status_code == 409 and r.json()["detail"] == NO_CARD
    # The viewer's own sign-in only: never a session token, whoever it names.
    token = live_session_token("sid-card-viewer", "pa", user_sub="user-admin")
    assert _viewer_card(_bearer(token), CARD_ROW["id"]).status_code == 403
    assert TestClient(app).get(f"/v1/settings/card/{CARD_ROW['id']}").status_code == 401

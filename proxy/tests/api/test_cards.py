"""The cards routes and service (api/cards, services/cards): the draw's refusals, the
server-rendered words, the state a person reads, the click's refusals and
its audit row, the parameters the client never supplies, the say path,
the three first actions' role checks on the side each touches.
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import app
from auth import roles
from auth.providers import UserContext, create_session_jwt
from core.session import session_kind, session_state, visibility
from services.cards import actions, cards, lists, status as st
from services.community import department_set_installer as installer
from storage import database as task_store
from storage.agents import agent_store, db_departments
from storage.cards import db_cards
from tests._paths import REPO_ROOT
from tests.conftest import live_session_token
from ws import wire_events as wire

ADMIN = "user-admin"
CREATOR = "user-manager"
MEMBER = "user-viewer"
AGENT = "pa"

REGISTRY = {"agents": [
    {"slug": "yacht-crew", "display_name": "Yacht Crew", "version": "0.2.2", "platform_min_version": "1.0.0",
     "apps": [{"slug": "crew-board", "title": "Crew board", "visibility": "agent", "sig": "s", "app_json": {"title": "Crew board"}}]},
], "departments": [{
    "slug": "yacht-office", "name": "Yacht office", "version": "0.1.0", "levels": ["Head", "Team"],
    "members": [{"template": "yacht-crew", "level": "Team"}]}]}


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    monkeypatch.setattr("services.community.community_agents_catalog.fetch_registry",
                        AsyncMock(return_value=REGISTRY))


def _cookie(sub: str, email: str, role: str) -> TestClient:
    return TestClient(app, headers={"Cookie": f"session={create_session_jwt(sub, email, 'T User', role)}"})


def _bearer(token: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def _own_session(sid: str, sub: str, chat_id: str, agent: str = AGENT) -> str:
    task_store.create_chat(chat_id, sub, agent)
    task_store.update_chat(chat_id, session_id=sid)
    return live_session_token(sid, agent, user_sub=sub)


def _drain(sid: str) -> list[dict]:
    q = session_state.get_permission_queue(sid)
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def _dept(name="Eng"):
    return db_departments.create_department(name, ADMIN, level_names=["Head", "Team"])


def _move_buttons(agent: str, dept: dict) -> list[dict]:
    return [{"id": "go", "kind": st.BUTTON_DO, "action": "move_agent",
             "params": {"agent": agent, "department_id": dept["id"], "level_id": dept["levels"][1]["id"]}},
            {"id": "no", "kind": st.BUTTON_SAY, "label": "Not now", "message": "Not now, thanks."}]


def _draw(token: str, sid: str, buttons: list[dict], note: str = ""):
    return _bearer(token).post("/v1/cards", json={"session_id": sid, "buttons": buttons, "note": note})


# --- the draw ---------------------------------------------------------------------------

def test_the_draw_stores_the_card_renders_its_words_and_queues_the_block(temp_db):
    agent_store.create_agent("alpha", "Alpha", created_by=ADMIN)
    dept = _dept()
    token = _own_session("sid-card-1", ADMIN, "chat-card-1")
    r = _draw(token, "sid-card-1", _move_buttons("alpha", dept), note="I suggest the Team level.")
    assert r.status_code == 200, r.text
    card_id = r.json()["card_id"]
    card = db_cards.get_card(card_id)
    assert card["chat_id"] == "chat-card-1" and card["session_id"] == "sid-card-1" and card["person_sub"] == ADMIN
    assert card["title"] == 'Move Alpha to "Eng"' and card["note"] == "I suggest the Team level."
    assert card["buttons"][0]["params"]["department_id"] == dept["id"]
    (item,) = _drain("sid-card-1")
    assert item["event_type"] == wire.ITEM_ACTION_CARD and item["card_id"] == card_id
    assert item["buttons"] == [{"id": "go", "label": "Move", "kind": st.BUTTON_DO},
                               {"id": "no", "label": "Not now", "kind": st.BUTTON_SAY, "message": "Not now, thanks."}]
    assert "params" not in str(item)
    assert item["text"].startswith('Places Alpha at the Team level of "Eng".')


def test_the_draw_takes_a_shared_space_chat_a_person_runs_and_refuses_an_unattended_one(temp_db):
    agent_store.create_agent("alpha", "Alpha", created_by=ADMIN)
    dept = _dept()
    buttons = _move_buttons("alpha", dept)
    task_store.create_chat("chat-shared", visibility.shared_chat_owner(AGENT), AGENT)
    task_store.update_chat("chat-shared", session_id="sid-card-shared")
    r = _draw(live_session_token("sid-card-shared", AGENT, user_sub=ADMIN), "sid-card-shared", buttons)
    assert r.status_code == 200, r.text
    assert db_cards.get_card(r.json()["card_id"])["person_sub"] == ADMIN
    # Every unattended kind, whoever the chat belongs to: an agent-scope
    # task, the person's own scheduled task or task-surface worker (the
    # chat is theirs, the kind has nobody at the keyboard), a phone call.
    unattended = [
        ("chat-task", visibility.task_chat_owner(AGENT), session_kind.TASK.source_type),
        ("chat-task-mine", ADMIN, session_kind.TASK.source_type),
        (session_kind.task_chat_id("run-old"), ADMIN, "chat"),
        ("chat-phone", visibility.PHONE_CHAT_OWNER, session_kind.PHONE.source_type),
        ("chat-phone-mine", ADMIN, session_kind.PHONE.source_type),
    ]
    for chat_id, owner, source_type in unattended:
        task_store.create_chat(chat_id, owner, AGENT, source_type=source_type)
        task_store.update_chat(chat_id, session_id=f"sid-{chat_id}")
        r = _draw(live_session_token(f"sid-{chat_id}", AGENT, user_sub=ADMIN), f"sid-{chat_id}", buttons)
        assert r.status_code == 403 and r.json()["detail"] == st.NO_PERSON, chat_id
        assert _drain(f"sid-{chat_id}") == []


def test_the_draw_refuses_a_cookie_a_bearer_naming_nobody_another_session_and_a_meeting(temp_db):
    agent_store.create_agent("alpha", "Alpha", created_by=ADMIN)
    dept = _dept()
    buttons = _move_buttons("alpha", dept)
    admin = _cookie(ADMIN, "admin@test.com", roles.ADMIN)
    r = admin.post("/v1/cards", json={"session_id": "sid-x", "buttons": buttons})
    assert r.status_code == 403 and r.json()["detail"] == st.NO_PERSON
    nobody = live_session_token("sid-card-nobody", AGENT)
    task_store.create_chat("chat-nobody", ADMIN, AGENT)
    task_store.update_chat("chat-nobody", session_id="sid-card-nobody")
    r = _draw(nobody, "sid-card-nobody", buttons)
    assert r.status_code == 403 and r.json()["detail"] == st.NO_PERSON
    token = _own_session("sid-card-a", ADMIN, "chat-card-a")
    r = _draw(token, "sid-card-b", buttons)
    assert r.status_code == 403 and "session" in r.json()["detail"]
    with patch("api.cards.cards.routing.resolve_hook_route") as route:
        route.return_value.is_meeting = True
        r = _draw(token, "sid-card-a", buttons)
    assert r.status_code == 403 and r.json()["detail"] == st.NO_PERSON
    assert _drain("sid-card-a") == []
    # A chat another person owns (an admin driving a member's chat) draws:
    # whose chat it is plays no part, the card acts for whoever presses it.
    task_store.create_chat("chat-of-member", MEMBER, AGENT)
    task_store.update_chat("chat-of-member", session_id="sid-card-foreign")
    foreign = live_session_token("sid-card-foreign", AGENT, user_sub=ADMIN)
    assert _draw(foreign, "sid-card-foreign", buttons).status_code == 200
    assert [i["event_type"] for i in _drain("sid-card-foreign")] == [wire.ITEM_ACTION_CARD]


def test_a_member_presses_the_card_an_admin_drew_in_the_members_chat(temp_db, monkeypatch):
    """The admin drives the member's chat and the agent draws a card in
    that turn: the member reads it and presses it as themselves."""
    queued = _queue_spy(monkeypatch)
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    task_store.create_chat("chat-member-driven", MEMBER, AGENT)
    task_store.update_chat("chat-member-driven", session_id="sid-member-driven")
    admins = live_session_token("sid-member-driven", AGENT, user_sub=ADMIN)
    r = _draw(admins, "sid-member-driven", [{"id": "yes", "kind": st.BUTTON_SAY, "label": "Yes", "message": "Yes, go on."}])
    assert r.status_code == 200, r.text
    card_id = r.json()["card_id"]
    assert db_cards.get_card(card_id)["person_sub"] == ADMIN
    member = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    assert member.get(f"/v1/cards/{card_id}").status_code == 200
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "yes"})
    assert r.status_code == 200 and r.json()["queued"] is True, r.text
    (click,) = db_cards.clicks_of(card_id)
    assert click["person_sub"] == MEMBER and click["status"] == db_cards.CLICK_SAID
    assert queued == [("chat-member-driven", MEMBER, "Yes, go on.", {"card_id": card_id, "button": "yes"})]


def test_the_draw_refuses_bad_buttons_unknown_actions_and_parameters_off_the_schema(temp_db):
    token = _own_session("sid-card-bad", ADMIN, "chat-card-bad")
    cases = [
        ([], "one to three buttons"),
        ([{"id": "a", "kind": st.BUTTON_DO, "action": "x"}] * 4, "at most 3"),
        ([{"id": "a", "kind": "press"}], "kind is one of"),
        ([{"id": "a", "kind": st.BUTTON_DO, "action": "nope", "params": {}}], "no action named"),
        ([{"id": "a", "kind": st.BUTTON_DO, "action": "move_agent", "params": {"agent": "Bad Slug"}}], "do not fit"),
        ([{"id": "a", "kind": st.BUTTON_SAY, "label": "x"}], "say button needs"),
        ([{"id": "a", "kind": st.BUTTON_SAY, "label": "x", "message": "m"}, {"id": "a", "kind": st.BUTTON_SAY, "label": "y", "message": "m"}], "own short id"),
        ([{"id": "a", "kind": st.BUTTON_DO, "action": "move_agent", "params": {}},
          {"id": "b", "kind": st.BUTTON_DO, "action": "move_agent", "params": {}}], st.ONE_DO_BUTTON),
    ]
    for buttons, words in cases:
        r = _draw(token, "sid-card-bad", buttons)
        assert r.status_code == 400 and words in r.json()["detail"], (buttons, r.text)
        # A sentence the tool repeats to the agent, so it learns which parameter was wrong.
        assert r.json()["detail"].endswith(".")
    r = _draw(token, "sid-card-bad", [{"id": "a", "kind": st.BUTTON_DO, "action": "move_agent",
                                       "params": {"agent": "alpha", "department_id": "d", "level_id": "l", "library": "x"}}])
    assert r.status_code == 400 and "'library' was unexpected" in r.json()["detail"] and r.json()["detail"].endswith(".")
    assert _drain("sid-card-bad") == []


def test_a_say_only_card_needs_a_person_too(temp_db):
    # A card nobody could press would wait for an answer that never comes.
    task_store.create_chat("chat-say", ADMIN, AGENT)
    task_store.update_chat("chat-say", session_id="sid-say")
    nobody = live_session_token("sid-say", AGENT)
    r = _draw(nobody, "sid-say", [{"id": "yes", "kind": st.BUTTON_SAY, "label": "Yes", "message": "Yes, go on."}])
    assert r.status_code == 403 and r.json()["detail"] == st.NO_PERSON
    assert _drain("sid-say") == []


def test_a_say_only_card_wears_the_agents_words_and_is_the_persons_to_press(temp_db, monkeypatch):
    queued = _queue_spy(monkeypatch)
    token = _own_session("sid-say-mine", ADMIN, "chat-say-mine")
    r = _bearer(token).post("/v1/cards", json={
        "session_id": "sid-say-mine", "title": "Keep going?", "text": "The draft is ready for a second pass.",
        "buttons": [{"id": "yes", "kind": st.BUTTON_SAY, "label": "Yes", "message": "Yes, go on."},
                    {"id": "no", "kind": st.BUTTON_SAY, "label": "Not now", "message": "Not now."}]})
    assert r.status_code == 200, r.text
    card = db_cards.get_card(r.json()["card_id"])
    assert card["person_sub"] == ADMIN and card["title"] == "Keep going?"
    assert card["text"] == "The draft is ready for a second pass." and card["note"] == ""
    admin = _cookie(ADMIN, "admin@test.com", roles.ADMIN)
    r = admin.post(f"/v1/cards/{card['id']}/act", json={"button": "yes"})
    assert r.status_code == 200 and r.json()["kind"] == st.BUTTON_SAY and r.json()["queued"] is True
    # The message never rides the answer: the server sent it as the person.
    assert "message" not in r.json()
    assert r.json()["state"]["clicks"][0]["status"] == db_cards.CLICK_SAID
    assert r.json()["state"]["clicks"][0]["detail"] == "Yes, go on."
    assert queued == [("chat-say-mine", ADMIN, "Yes, go on.", {"card_id": card["id"], "button": "yes"})]
    # One click per card: the other say button is refused.
    r = admin.post(f"/v1/cards/{card['id']}/act", json={"button": "no"})
    assert r.status_code == 409 and r.json()["detail"] == st.ANSWERED_ALREADY
    assert len(queued) == 1
    r = _cookie(MEMBER, "viewer@test.com", roles.MEMBER).post(f"/v1/cards/{card['id']}/act", json={"button": "yes"})
    assert r.status_code == 403


def _queue_spy(monkeypatch) -> list:
    """What the say path queued: ``(chat_id, author, text, card)`` per
    message, with the delivery left alone (no socket drives a test)."""
    from core.events import input_queue
    out: list = []
    real_add = input_queue.ChatInputQueue.add

    async def add(self, queue_id, author_sub, item, **kw):
        out.append((self.chat_id, author_sub, item.text, item.card))
        return await real_add(self, queue_id, author_sub, item, **kw)

    monkeypatch.setattr(input_queue.ChatInputQueue, "add", add)
    monkeypatch.setattr(input_queue, "arm", lambda *_a, **_k: None)
    return out


def test_the_open_card_cap_counts_the_live_unpressed_cards_of_the_session(temp_db, monkeypatch):
    monkeypatch.setattr(db_cards, "MAX_OPEN_PER_CHAT", 2)
    token = _own_session("sid-cap", ADMIN, "chat-cap")
    say = [{"id": "yes", "kind": st.BUTTON_SAY, "label": "Yes", "message": "Yes."}]
    first = _draw(token, "sid-cap", say).json()["card_id"]
    assert _draw(token, "sid-cap", say).status_code == 200
    assert _draw(token, "sid-cap", say).status_code == 409
    # A pressed say card leaves the count; a card of an older session and
    # a card past its time never count.
    admin = _cookie(ADMIN, "admin@test.com", roles.ADMIN)
    assert admin.post(f"/v1/cards/{first}/act", json={"button": "yes"}).status_code == 200
    assert _draw(token, "sid-cap", say).status_code == 200
    assert _draw(token, "sid-cap", say).status_code == 409
    task_store.update_chat("chat-cap", session_id="sid-cap-2")
    token2 = live_session_token("sid-cap-2", AGENT, user_sub=ADMIN)
    assert _draw(token2, "sid-cap-2", say).status_code == 200
    assert _draw(token2, "sid-cap-2", say).status_code == 200
    assert _draw(token2, "sid-cap-2", say).status_code == 409
    monkeypatch.setattr(st, "CARD_TTL_S", 0)
    assert _draw(token2, "sid-cap-2", say).status_code == 200


def test_the_parameters_are_capped_in_bytes(temp_db, monkeypatch):
    agent_store.create_agent("alpha", "Alpha", created_by=ADMIN)
    dept = _dept()
    monkeypatch.setattr(db_cards, "MAX_PARAMS_BYTES", 40)
    token = _own_session("sid-bytes", ADMIN, "chat-bytes")
    r = _draw(token, "sid-bytes", _move_buttons("alpha", dept))
    assert r.status_code == 400 and "larger than" in r.json()["detail"]


def test_the_draw_refuses_what_the_person_could_never_do(temp_db):
    agent_store.create_agent("alpha", "Alpha", created_by=ADMIN)
    dept = _dept()
    # A member managing the agent: not a creator.
    task_store.set_user_agents(MEMBER, ["alpha", AGENT], ADMIN, agent_roles={"alpha": "manager", AGENT: "manager"})
    token = _own_session("sid-card-m", MEMBER, "chat-card-m")
    r = _draw(token, "sid-card-m", _move_buttons("alpha", dept))
    assert r.status_code == 409 and r.json()["detail"] == "Only admins and creators can change an agent's department."
    # A creator who does not manage the agent.
    task_store.set_user_agents(CREATOR, [AGENT], ADMIN, agent_roles={AGENT: "manager"})
    token = _own_session("sid-card-c", CREATOR, "chat-card-c")
    r = _draw(token, "sid-card-c", _move_buttons("alpha", dept))
    assert r.status_code == 409 and r.json()["detail"] == "Manager or admin access required for this agent."
    # A creator who manages it draws the card.
    task_store.set_user_agents(CREATOR, ["alpha", AGENT], ADMIN, agent_roles={"alpha": "manager", AGENT: "manager"})
    token = _own_session("sid-card-c2", CREATOR, "chat-card-c2")
    assert _draw(token, "sid-card-c2", _move_buttons("alpha", dept)).status_code == 200


# --- the state and the click --------------------------------------------------------

def _drawn_move(sub: str = ADMIN, sid: str = "sid-click", chat_id: str = "chat-click") -> tuple[str, dict]:
    agent_store.create_agent("alpha", "Alpha", created_by=ADMIN)
    dept = _dept()
    token = _own_session(sid, sub, chat_id)
    r = _draw(token, sid, _move_buttons("alpha", dept))
    assert r.status_code == 200, r.text
    return r.json()["card_id"], dept


def test_the_state_is_read_by_whoever_may_open_the_chat(temp_db):
    card_id, _dept_row = _drawn_move()
    admin = _cookie(ADMIN, "admin@test.com", roles.ADMIN)
    s = admin.get(f"/v1/cards/{card_id}").json()
    assert s["card_id"] == card_id and s["resolved"] is False and s["expired"] is None and s["clicks"] == []
    assert [b["kind"] for b in s["buttons"]] == [st.BUTTON_DO, st.BUTTON_SAY] and "params" not in str(s)
    other = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    r = other.get(f"/v1/cards/{card_id}")
    assert r.status_code == 403 and r.json()["detail"] == st.CARD_ELSEWHERE
    assert admin.get("/v1/cards/card-nope").status_code == 404
    # The card's own session token reads it too (the agent learns the outcome).
    token = live_session_token("sid-click", AGENT, user_sub=ADMIN)
    assert _bearer(token).get(f"/v1/cards/{card_id}").status_code == 200
    # A session of the same person on another agent does not.
    task_store.create_chat("chat-elsewhere", ADMIN, "alpha")
    task_store.update_chat("chat-elsewhere", session_id="sid-elsewhere")
    elsewhere = live_session_token("sid-elsewhere", "alpha", user_sub=ADMIN)
    assert _bearer(elsewhere).get(f"/v1/cards/{card_id}").status_code == 403


def test_a_do_click_runs_the_action_once_with_an_audit_row(temp_db):
    card_id, dept = _drawn_move()
    admin = _cookie(ADMIN, "admin@test.com", roles.ADMIN)
    r = admin.post(f"/v1/cards/{card_id}/act", json={"button": "go"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == st.BUTTON_DO and body["outcome"]["status"] == st.OUTCOME_DONE
    assert body["outcome"]["text"] == 'Alpha is now in "Eng" as Team.'
    assert agent_store.get_agent("alpha")["department_id"] == dept["id"]
    (click,) = db_cards.clicks_of(card_id)
    assert click["person_sub"] == ADMIN and click["status"] == db_cards.CLICK_DONE and click["button"] == "go"
    s = admin.get(f"/v1/cards/{card_id}").json()
    assert s["resolved"] is True and s["outcome"]["status"] == st.OUTCOME_DONE and len(s["clicks"]) == 1
    # A second press of the same button, and the other do button, are refused.
    r = admin.post(f"/v1/cards/{card_id}/act", json={"button": "go"})
    assert r.status_code == 409 and r.json()["detail"] == st.ANSWERED_ALREADY
    # One click per card: the say button is refused once the card resolved.
    r = admin.post(f"/v1/cards/{card_id}/act", json={"button": "no"})
    assert r.status_code == 409 and r.json()["detail"] == st.ANSWERED_ALREADY
    assert admin.post(f"/v1/cards/{card_id}/act", json={"button": "nope"}).status_code == 400


def test_the_click_refuses_a_person_who_cannot_open_the_chat_a_bearer_and_an_expired_card(temp_db):
    card_id, _dept_row = _drawn_move()
    # The admin's own chat: a member cannot open it, so cannot press.
    other = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    r = other.post(f"/v1/cards/{card_id}/act", json={"button": "go"})
    assert r.status_code == 403 and r.json()["detail"] == st.CHAT_NOT_OPEN
    token = live_session_token("sid-click", AGENT, user_sub=ADMIN)
    assert _bearer(token).post(f"/v1/cards/{card_id}/act", json={"button": "go"}).status_code == 403
    admin = _cookie(ADMIN, "admin@test.com", roles.ADMIN)
    # The chat's session moved on: the card expired.
    task_store.update_chat("chat-click", session_id="sid-click-2")
    assert admin.get(f"/v1/cards/{card_id}").json()["expired"] == st.EXPIRED
    r = admin.post(f"/v1/cards/{card_id}/act", json={"button": "go"})
    assert r.status_code == 409 and r.json()["detail"] == st.EXPIRED
    assert db_cards.clicks_of(card_id) == []
    task_store.delete_chat("chat-click")
    assert db_cards.get_card(card_id) is None


def test_an_answered_card_never_reads_expired(temp_db, monkeypatch):
    _queue_spy(monkeypatch)
    token = _own_session("sid-ans", ADMIN, "chat-ans")
    card_id = _draw(token, "sid-ans", [{"id": "yes", "kind": st.BUTTON_SAY, "label": "Yes", "message": "Yes."},
                                       {"id": "no", "kind": st.BUTTON_SAY, "label": "No", "message": "No."}]).json()["card_id"]
    admin = _cookie(ADMIN, "admin@test.com", roles.ADMIN)
    assert admin.post(f"/v1/cards/{card_id}/act", json={"button": "yes"}).status_code == 200
    # The chat's session moved on: the answered card still reads as answered.
    task_store.update_chat("chat-ans", session_id="sid-ans-2")
    s = admin.get(f"/v1/cards/{card_id}").json()
    assert s["expired"] is None and s["resolved"] is True and s["clicks"][0]["status"] == db_cards.CLICK_SAID
    # A press on it is still refused.
    r = admin.post(f"/v1/cards/{card_id}/act", json={"button": "no"})
    assert r.status_code == 409


def test_the_clients_parameters_never_reach_the_action(temp_db):
    card_id, dept = _drawn_move()
    other = _dept("Ops")
    admin = _cookie(ADMIN, "admin@test.com", roles.ADMIN)
    r = admin.post(f"/v1/cards/{card_id}/act", json={
        "button": "go", "options": {"department_id": other["id"], "level_id": other["levels"][0]["id"]},
        "params": {"department_id": other["id"]}})
    assert r.status_code == 200
    assert agent_store.get_agent("alpha")["department_id"] == dept["id"]


def test_the_role_is_checked_again_at_the_click(temp_db):
    agent_store.create_agent("alpha", "Alpha", created_by=ADMIN)
    dept = _dept()
    task_store.set_user_agents(CREATOR, ["alpha", AGENT], ADMIN, agent_roles={"alpha": "manager", AGENT: "manager"})
    token = _own_session("sid-role", CREATOR, "chat-role")
    card_id = _draw(token, "sid-role", _move_buttons("alpha", dept)).json()["card_id"]
    # The creator lost the manager tier between the draw and the click.
    task_store.set_user_agents(CREATOR, [AGENT], ADMIN, agent_roles={AGENT: "manager"})
    creator = _cookie(CREATOR, "manager@test.com", roles.CREATOR)
    r = creator.post(f"/v1/cards/{card_id}/act", json={"button": "go"})
    assert r.status_code == 200 and r.json()["outcome"]["status"] == st.OUTCOME_FAILED
    assert r.json()["outcome"]["text"] == "Manager or admin access required for this agent."
    assert agent_store.get_agent("alpha")["department_id"] == ""
    (click,) = db_cards.clicks_of(card_id)
    assert click["status"] == db_cards.CLICK_FAILED


def test_two_claims_on_one_card_run_the_action_once(temp_db):
    card_id, _dept_row = _drawn_move()
    first = db_cards.claim_click(card_id, "go", ADMIN, {}, resolves=True)
    second = db_cards.claim_click(card_id, "go", ADMIN, {}, resolves=True)
    assert first is not None and second is None
    assert db_cards.claim_click(card_id, "no", ADMIN, {}, resolves=False) is not None
    assert db_cards.claim_click(card_id, "no", ADMIN, {}, resolves=False) is None


# --- a shared chat: live for whoever presses -----------------------------------------

def _shared_move(sid: str = "sid-shared-move", chat_id: str = "chat-shared-move") -> tuple[str, dict]:
    """A move card the admin drew in the Shared-only chat of ``AGENT``."""
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    agent_store.create_agent("alpha", "Alpha", created_by=ADMIN)
    dept = _dept()
    task_store.create_chat(chat_id, visibility.shared_chat_owner(AGENT), AGENT)
    task_store.update_chat(chat_id, session_id=sid)
    r = _draw(live_session_token(sid, AGENT, user_sub=ADMIN), sid, _move_buttons("alpha", dept))
    assert r.status_code == 200, r.text
    _drain(sid)
    return r.json()["card_id"], dept


def test_a_shared_chats_card_acts_for_the_person_who_presses_it(temp_db):
    card_id, dept = _shared_move()
    _person(CREATOR, "manager", {AGENT: roles.EDITOR, "alpha": roles.MANAGER})
    creator = _cookie(CREATOR, "manager@test.com", roles.CREATOR)
    s = creator.get(f"/v1/cards/{card_id}").json()
    assert s["resolved"] is False and [b["id"] for b in s["buttons"]] == ["go", "no"]
    r = creator.post(f"/v1/cards/{card_id}/act", json={"button": "go"})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"]["status"] == st.OUTCOME_DONE
    assert agent_store.get_agent("alpha")["department_id"] == dept["id"]
    (click,) = db_cards.clicks_of(card_id)
    assert click["person_sub"] == CREATOR
    # The card keeps who asked; the click names who pressed, per reader.
    assert db_cards.get_card(card_id)["person_sub"] == ADMIN
    (mine,) = r.json()["state"]["clicks"]
    assert mine["mine"] is True and mine["by"] == "Manager User"
    admin = _cookie(ADMIN, "admin@test.com", roles.ADMIN)
    (theirs,) = admin.get(f"/v1/cards/{card_id}").json()["clicks"]
    assert theirs["mine"] is False and theirs["by"] == "Manager User"


def test_a_press_the_pressers_role_could_never_complete_spends_no_click(temp_db):
    card_id, _dept_row = _shared_move()
    # An editor of the agent reads the card but may not move Alpha.
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    member = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    assert member.get(f"/v1/cards/{card_id}").status_code == 200
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "go"})
    assert r.status_code == 403 and r.json()["detail"].endswith("."), r.text
    assert db_cards.clicks_of(card_id) == [] and db_cards.get_card(card_id)["resolved_at"] is None
    # The admin who asked still presses it.
    admin = _cookie(ADMIN, "admin@test.com", roles.ADMIN)
    assert admin.post(f"/v1/cards/{card_id}/act", json={"button": "go"}).json()["outcome"]["status"] == st.OUTCOME_DONE


def test_a_say_press_in_a_shared_chat_speaks_as_the_presser(temp_db, monkeypatch):
    queued = _queue_spy(monkeypatch)
    card_id, _dept_row = _shared_move()
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    member = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "no"})
    assert r.status_code == 200 and r.json()["queued"] is True, r.text
    assert queued == [("chat-shared-move", MEMBER, "Not now, thanks.", {"card_id": card_id, "button": "no"})]
    (click,) = r.json()["state"]["clicks"]
    assert click["status"] == db_cards.CLICK_SAID and click["mine"] is True


def test_a_say_press_where_the_pressers_session_may_not_run_is_refused(temp_db, monkeypatch):
    from services.remote import chat_move
    queued = _queue_spy(monkeypatch)
    card_id, _dept_row = _shared_move()
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    monkeypatch.setattr(chat_move, "placement_blocked", lambda *_a, **_k: True)
    member = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "no"})
    assert r.status_code == 403 and r.json()["detail"] == st.PLACE_NOT_YOURS
    assert db_cards.clicks_of(card_id) == [] and queued == []


def test_a_say_press_on_a_platform_machine_takes_the_tier_that_runs_there(temp_db, monkeypatch):
    """A shared chat placed on an admin-paired (platform) machine: a
    session below the owner tier never runs there, and a card cannot move
    the chat as a send does, so an editor's say press is refused and a
    manager's goes out. The real placement gate, nothing patched."""
    from storage import remote_store
    queued = _queue_spy(monkeypatch)
    card_id, _dept_row = _shared_move()
    remote_store.create_remote_machine("m-platform-card", "Platform box for cards", ADMIN, pairing_scope="admin")
    task_store.update_chat("chat-shared-move", execution_target="m-platform-card")
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    member = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "no"})
    assert r.status_code == 403 and r.json()["detail"] == st.PLACE_NOT_YOURS
    assert db_cards.clicks_of(card_id) == [] and queued == []
    _person(CREATOR, "manager", {AGENT: roles.MANAGER, "alpha": roles.MANAGER})
    creator = _cookie(CREATOR, "manager@test.com", roles.CREATOR)
    r = creator.post(f"/v1/cards/{card_id}/act", json={"button": "no"})
    assert r.status_code == 200 and r.json()["queued"] is True, r.text
    assert [author for _chat, author, _text, _card in queued] == [CREATOR]


def test_a_say_press_on_a_platform_machine_follows_its_every_role_switch(temp_db, monkeypatch):
    """The platform machine's admin let every role of its agents run there:
    an editor's say press in a chat placed on it goes out once it is the
    agent's own default, and stays refused on another agent's machine."""
    from storage import remote_store
    queued = _queue_spy(monkeypatch)
    card_id, _dept_row = _shared_move()
    remote_store.create_remote_machine("m-platform-card", "Platform box for cards", ADMIN, pairing_scope="admin")
    remote_store.set_all_roles_run_here("m-platform-card", True)
    task_store.update_chat("chat-shared-move", execution_target="m-platform-card")
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    member = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "no"})
    assert r.status_code == 403 and r.json()["detail"] == st.PLACE_NOT_YOURS
    assert db_cards.clicks_of(card_id) == [] and queued == []
    remote_store.set_agent_remote_target(AGENT, "m-platform-card", ADMIN)
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "no"})
    assert r.status_code == 200 and r.json()["queued"] is True, r.text
    assert [author for _chat, author, _text, _card in queued] == [MEMBER]


def test_a_chat_held_on_a_persons_own_computer_is_theirs_alone(temp_db, monkeypatch):
    from services.remote import chat_move
    card_id, _dept_row = _shared_move()
    held = "This chat is running on Pat's computer."
    monkeypatch.setattr(chat_move, "lock_sentence", lambda _chat, sub: "" if sub == ADMIN else held)
    _person(CREATOR, "manager", {AGENT: roles.EDITOR, "alpha": roles.MANAGER})
    creator = _cookie(CREATOR, "manager@test.com", roles.CREATOR)
    for button in ("go", "no"):
        r = creator.post(f"/v1/cards/{card_id}/act", json={"button": button})
        assert r.status_code == 403 and r.json()["detail"] == held
    assert db_cards.clicks_of(card_id) == []


def test_a_person_who_lost_the_agent_neither_reads_nor_presses_their_own_chats_card(temp_db, monkeypatch):
    _queue_spy(monkeypatch)
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    token = _own_session("sid-lost", MEMBER, "chat-lost")
    card_id = _draw(token, "sid-lost", [{"id": "yes", "kind": st.BUTTON_SAY, "label": "Yes",
                                         "message": "Yes."}]).json()["card_id"]
    task_store.set_user_agents(MEMBER, [], ADMIN, agent_roles={})
    member = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    r = member.get(f"/v1/cards/{card_id}")
    assert r.status_code == 403 and r.json()["detail"] == st.CARD_ELSEWHERE
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "yes"})
    assert r.status_code == 403 and r.json()["detail"] == st.CHAT_NOT_OPEN
    assert db_cards.clicks_of(card_id) == []


def test_a_session_of_the_cards_chat_reads_it_and_one_of_another_chat_does_not(temp_db):
    card_id, _dept_row = _shared_move()
    # The shared chat's session after a hand-over names another person.
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    same_chat = live_session_token("sid-shared-move", AGENT, user_sub=MEMBER)
    assert _bearer(same_chat).get(f"/v1/cards/{card_id}").status_code == 200
    task_store.create_chat("chat-other", MEMBER, AGENT)
    task_store.update_chat("chat-other", session_id="sid-other")
    other_chat = live_session_token("sid-other", AGENT, user_sub=MEMBER)
    r = _bearer(other_chat).get(f"/v1/cards/{card_id}")
    assert r.status_code == 403 and r.json()["detail"] == st.CARD_ELSEWHERE
    # An admin's token on another agent reads nothing here either.
    task_store.create_chat("chat-alpha", ADMIN, "alpha")
    task_store.update_chat("chat-alpha", session_id="sid-alpha")
    elsewhere = live_session_token("sid-alpha", "alpha", user_sub=ADMIN)
    assert _bearer(elsewhere).get(f"/v1/cards/{card_id}").status_code == 403


# --- the install actions --------------------------------------------------------------

def test_the_install_card_installs_through_the_catalog_and_ticks_the_edge(temp_db, monkeypatch):
    installed = {}

    async def fake_install(**kw):
        installed.update(kw)
        agent_store.create_agent(kw["target_slug"], "Yacht Crew", created_by=kw["installer_user_sub"],
                                 community_template=kw["template_slug"], community_template_version="0.2.2")
        return {"agent_slug": kw["target_slug"]}

    monkeypatch.setattr(actions.community_agent_installer, "install_from_catalog", fake_install)
    task_store.set_user_agents(ADMIN, [AGENT], ADMIN, agent_roles={AGENT: "manager"})
    token = _own_session("sid-inst", ADMIN, "chat-inst")
    r = _draw(token, "sid-inst", [{"id": "go", "kind": st.BUTTON_DO, "action": "install_agent",
                                   "params": {"template_slug": "yacht-crew", "display_name": "Crew of Lady G"}}])
    assert r.status_code == 200, r.text
    card = db_cards.get_card(r.json()["card_id"])
    assert card["title"] == "Install Yacht Crew"
    assert "Its apps wait for approval on their cards" in card["text"]
    assert card["options"] == [{"id": "delegation_edge", "label": "Let pa hand work to the new agent", "default": True}]
    assert card["buttons"][0]["params"]["_agent"] == AGENT
    admin = _cookie(ADMIN, "admin@test.com", roles.ADMIN)
    r = admin.post(f"/v1/cards/{card['id']}/act", json={"button": "go", "options": {"delegation_edge": True}})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"]["status"] == st.OUTCOME_DONE and r.json()["outcome"]["agent_slug"] == "yacht-crew"
    assert installed["installer_user_sub"] == ADMIN and installed["display_name"] == "Crew of Lady G"
    assert installed.get("app_consent") is None and installed.get("check_consent") is None
    assert "yacht-crew" in agent_store.get_delegation_targets(AGENT)
    assert "pa can hand work to it." in r.json()["outcome"]["text"]
    # Every viewer of the chat reads the outcome: it names the manager.
    assert "is installed as yacht-crew, managed by Admin User." in r.json()["outcome"]["text"]


def test_the_install_cards_role_is_checked_again_at_the_click(temp_db, monkeypatch):
    installed = {}

    async def fake_install(**kw):
        installed.update(kw)
        return {"agent_slug": kw["target_slug"]}

    monkeypatch.setattr(actions.community_agent_installer, "install_from_catalog", fake_install)
    task_store.set_user_agents(CREATOR, [AGENT], ADMIN, agent_roles={AGENT: "manager"})
    token = _own_session("sid-inst-role", CREATOR, "chat-inst-role")
    card_id = _draw(token, "sid-inst-role", [{"id": "go", "kind": st.BUTTON_DO, "action": "install_agent",
                                              "params": {"template_slug": "yacht-crew"}}]).json()["card_id"]
    # The creator became a member between the draw and the click (the
    # users row is the role's authority, the cookie only names the person).
    task_store.update_user_role(CREATOR, roles.MEMBER)
    member = _cookie(CREATOR, "manager@test.com", roles.MEMBER)
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "go"})
    assert r.status_code == 200 and r.json()["outcome"]["status"] == st.OUTCOME_FAILED
    assert r.json()["outcome"]["text"] == "Only admins and creators install agents."
    assert not installed
    (click,) = db_cards.clicks_of(card_id)
    assert click["status"] == db_cards.CLICK_FAILED


def test_the_install_card_refuses_a_member_and_an_unknown_template(temp_db):
    task_store.set_user_agents(MEMBER, [AGENT], ADMIN, agent_roles={AGENT: "manager"})
    token = _own_session("sid-inst-m", MEMBER, "chat-inst-m")
    r = _draw(token, "sid-inst-m", [{"id": "go", "kind": st.BUTTON_DO, "action": "install_agent",
                                     "params": {"template_slug": "yacht-crew"}}])
    assert r.status_code == 409 and r.json()["detail"] == "Only admins and creators install agents."
    token = _own_session("sid-inst-a", ADMIN, "chat-inst-a")
    r = _draw(token, "sid-inst-a", [{"id": "go", "kind": st.BUTTON_DO, "action": "install_agent",
                                     "params": {"template_slug": "nowhere"}}])
    assert r.status_code == 409 and "no template named nowhere" in r.json()["detail"]


def test_the_set_card_starts_the_job_and_the_state_follows_it(temp_db, monkeypatch):
    started = {}

    async def fake_start(dset, registry, name, u, *, consent, choices=None):
        started.update(name=name, sub=u.acting_sub, consent=consent)
        from storage.agents import db_set_installs
        return db_set_installs.create(dset.slug, dset.version, name, u.acting_sub, [])

    monkeypatch.setattr(actions.department_set_installer, "start", fake_start)
    token = _own_session("sid-set", ADMIN, "chat-set")
    r = _draw(token, "sid-set", [{"id": "go", "kind": st.BUTTON_DO, "action": "install_department_set",
                                  "params": {"set_slug": "yacht-office", "department_name": "Lady G"}}])
    assert r.status_code == 200, r.text
    card = db_cards.get_card(r.json()["card_id"])
    assert card["title"] == "Install the Yacht office department"
    assert card["text"].startswith('Creates the department "Lady G" with 1 agents: Yacht Crew (Team).')
    assert "joins it" not in card["text"]
    # A standalone agent from a member template is named: the card takes it.
    agent_store.create_agent("yacht-crew-2", "Crew Two", created_by=ADMIN, community_template="yacht-crew",
                             community_template_version="0.2.2")
    second = _draw(token, "sid-set", [{"id": "go", "kind": st.BUTTON_DO, "action": "install_department_set",
                                       "params": {"set_slug": "yacht-office", "department_name": "Lady H"}}])
    assert "Crew Two (yacht-crew-2) joins it: installed from these templates before and in no department." \
        in db_cards.get_card(second.json()["card_id"])["text"]
    admin = _cookie(ADMIN, "admin@test.com", roles.ADMIN)
    r = admin.post(f"/v1/cards/{card['id']}/act", json={"button": "go"})
    assert r.status_code == 200 and r.json()["outcome"]["status"] == st.OUTCOME_RUNNING
    assert started == {"name": "Lady G", "sub": ADMIN, "consent": False}
    job_id = r.json()["outcome"]["job_id"]
    s = admin.get(f"/v1/cards/{card['id']}").json()
    assert s["outcome"]["status"] == st.OUTCOME_RUNNING and s["outcome"]["job_id"] == job_id
    from storage.agents import db_set_installs
    db_set_installs.update(job_id, status=db_set_installs.DONE, lines=["The department Lady G holds Yacht Crew."])
    s = admin.get(f"/v1/cards/{card['id']}").json()
    assert s["outcome"]["status"] == st.OUTCOME_DONE and s["outcome"]["text"] == "The department Lady G holds Yacht Crew."
    assert db_cards.get_card(card["id"])["outcome"]["status"] == st.OUTCOME_DONE


def test_the_set_card_refuses_a_bad_name_and_a_missing_member(temp_db, monkeypatch):
    token = _own_session("sid-set-bad", ADMIN, "chat-set-bad")
    r = _draw(token, "sid-set-bad", [{"id": "go", "kind": st.BUTTON_DO, "action": "install_department_set",
                                      "params": {"set_slug": "yacht-office", "department_name": "a/b"}}])
    assert r.status_code == 409 and "may not contain" in r.json()["detail"]
    monkeypatch.setattr("services.community.community_agents_catalog.fetch_registry",
                        AsyncMock(return_value={"agents": [], "departments": REGISTRY["departments"]}))
    r = _draw(token, "sid-set-bad", [{"id": "go", "kind": st.BUTTON_DO, "action": "install_department_set",
                                      "params": {"set_slug": "yacht-office"}}])
    assert r.status_code == 409 and "not in the catalog: yacht-crew" in r.json()["detail"]


def test_the_catalog_validates_parameters_and_the_block_carries_no_params():
    problems = actions.validate_params(actions.MOVE_AGENT, {"agent": "x y", "department_id": "", "level_id": "l"})
    assert any("agent" in p for p in problems) and any("department_id" in p for p in problems)
    assert actions.validate_params(actions.MOVE_AGENT, "nope") == ["the parameters must be an object"]
    assert actions.get("move_agent") is actions.MOVE_AGENT and actions.get("x") is None
    block = cards.block_of({"id": "card-1", "title": "T", "text": "x", "note": "", "options": [],
                            "buttons": [{"id": "a", "label": "Go", "kind": st.BUTTON_DO, "action": "move_agent",
                                         "params": {"agent": "alpha"}}]})
    assert block["buttons"] == [{"id": "a", "label": "Go", "kind": st.BUTTON_DO}]
    nobody = UserContext(sub="", email="", name="", role=roles.ADMIN, agents=[], agent_roles={})
    assert asyncio.run(actions.INSTALL_AGENT.words({"template_slug": "yacht-crew", "target_slug": "crew-2"}, nobody))["text"] \
        .startswith("Installs the Yacht Crew template as the agent crew-2.")


def _ts_const_strings(text: str, name: str) -> list[str]:
    m = re.search(rf"export const {name}\b[^=]*=\s*(.+?)(?:\n\n|\nexport|\Z)", text, re.S)
    assert m, f"{name} not found in the mirror"
    return re.findall(r"'([^']*)'", m.group(1))


def test_the_dashboard_mirror_of_the_cards_words():
    text = (REPO_ROOT / "dashboard" / "src" / "lib" / "kinds" / "card.ts").read_text(encoding="utf-8")
    assert _ts_const_strings(text, "BUTTON_KIND") == list(st.BUTTON_KINDS)
    assert _ts_const_strings(text, "OUTCOME") == list(st.OUTCOME_STATES)
    assert _ts_const_strings(text, "CLICK_STATUS") == [db_cards.CLICK_RUNNING, db_cards.CLICK_DONE,
                                                      db_cards.CLICK_FAILED, db_cards.CLICK_SAID]
    assert _ts_const_strings(text, "SET_MEMBER") == [installer.MEMBER_PENDING, installer.MEMBER_INSTALLING,
                                                    installer.MEMBER_INSTALLED, installer.MEMBER_ADOPTED,
                                                    installer.MEMBER_FAILED]
    assert _ts_const_strings(text, "LIST_KIND") == list(st.LIST_KINDS)
    assert _ts_const_strings(text, "LIST_SCOPE") == list(lists.SCOPES)
    assert _ts_const_strings(text, "LIST_STATUS") == list(lists.STATUSES)
    assert f'managersOnly: "{lists.MANAGERS_SEE_MCPS}"' in text
    assert st.FROM_CARD in text


# --- the team and department actions ---------------------------------------------------

from api.cards import actions_departments, actions_team
from storage.pg import get_conn


def _person(sub: str, username: str, agents: dict[str, str]) -> None:
    """A person with a username (the file paths need one) and their roles."""
    task_store.set_user_agents(sub, list(agents), ADMIN, agent_roles=agents)
    with get_conn() as c:
        c.execute("UPDATE users SET username=%s WHERE sub=%s", (username, sub))
        c.commit()


def _do(action: str, params: dict, bid: str = "go") -> list[dict]:
    return [{"id": bid, "kind": st.BUTTON_DO, "action": action, "params": params}]


def test_the_catalog_holds_the_team_and_department_actions():
    for name in ("hand_files", "promote_memory", "share_app", "attach_library", "update_followed_app",
                 "send_release_to_source", "create_department", "set_department", "move_agent"):
        assert actions.get(name) is not None, name
    assert actions.sentence("the card was drawn") == "The card was drawn."
    # A name registers once; the same action again is harmless.
    actions.register(actions.MOVE_AGENT)
    clone = actions.Action(name="move_agent", schema={}, words=actions.MOVE_AGENT.words,
                           check=actions.MOVE_AGENT.check, run=actions.MOVE_AGENT.run)
    with pytest.raises(ValueError):
        actions.register(clone)
    assert actions.sentence("Done.") == "Done." and actions.sentence("") == ""


def test_hand_files_moves_the_persons_files_to_the_teams_workspace(temp_db):
    import config
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    _person(MEMBER, "viewer", {AGENT: roles.CONTRIBUTOR})
    root = config.get_agent_dir(AGENT)
    (root / "users" / "viewer" / "workspace" / "reports").mkdir(parents=True)
    (root / "users" / "viewer" / "workspace" / "reports" / "site.md").write_text("final")
    (root / "users" / "viewer" / "workspace" / "photo.jpg").write_bytes(b"jpg")
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    token = _own_session("sid-hand", MEMBER, "chat-hand")
    r = _draw(token, "sid-hand", _do("hand_files", {"paths": ["reports/site.md", "/users/viewer/workspace/photo.jpg"]}),
              note="The report reads final to me.")
    assert r.status_code == 200, r.text
    card = db_cards.get_card(r.json()["card_id"])
    assert card["title"] == "Hand 2 files to the team" and card["note"] == "The report reads final to me."
    assert card["text"].startswith('Moves "site.md" and "photo.jpg" from your space to the team\'s workspace')
    assert card["options"] == [{"id": "copy", "label": "Keep my copy", "default": False}]
    assert card["buttons"][0]["params"]["_agent"] == AGENT
    member = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    r = member.post(f"/v1/cards/{card['id']}/act", json={"button": "go"})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"]["status"] == st.OUTCOME_DONE
    assert r.json()["outcome"]["text"] == "2 files are in the team's workspace."
    assert (root / "workspace" / "site.md").read_text() == "final"
    assert (root / "workspace" / "photo.jpg").exists()
    assert not (root / "users" / "viewer" / "workspace" / "photo.jpg").exists()


def test_a_card_about_the_askers_own_files_is_theirs_alone_to_press(temp_db):
    """An admin in a member's chat presses the member's hand-files card: it
    is refused with one sentence before the claim, and the admin's own
    file of the same name stays where it is. The member still presses it."""
    import config
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    _person(MEMBER, "viewer", {AGENT: roles.CONTRIBUTOR})
    _person(ADMIN, "boss", {AGENT: roles.MANAGER})
    root = config.get_agent_dir(AGENT)
    for who in ("viewer", "boss"):
        (root / "users" / who / "workspace").mkdir(parents=True)
        (root / "users" / who / "workspace" / "photo.jpg").write_bytes(who.encode())
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    token = _own_session("sid-hand-own", MEMBER, "chat-hand-own")
    card_id = _draw(token, "sid-hand-own", _do("hand_files", {"paths": ["photo.jpg"]})).json()["card_id"]
    admin = _cookie(ADMIN, "admin@test.com", roles.ADMIN)
    assert admin.get(f"/v1/cards/{card_id}").status_code == 200
    r = admin.post(f"/v1/cards/{card_id}/act", json={"button": "go"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "This card is about Viewer User's own files. Ask the agent for a card of yours."
    assert db_cards.clicks_of(card_id) == [] and db_cards.get_card(card_id)["resolved_at"] is None
    assert (root / "users" / "boss" / "workspace" / "photo.jpg").read_bytes() == b"boss"
    assert not (root / "workspace" / "photo.jpg").exists()
    member = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "go"})
    assert r.status_code == 200 and r.json()["outcome"]["status"] == st.OUTCOME_DONE, r.text
    assert (root / "workspace" / "photo.jpg").read_bytes() == b"viewer"


def test_the_actions_that_reach_the_askers_own_space():
    """The cards only their asker presses: files and memory always, an app
    when either side of it is a personal space."""
    personal = {name: a.personal for name, a in actions.CATALOG.items()}
    assert personal["hand_files"]({"paths": ["a"]}) == "files"
    assert personal["promote_memory"]({"topic": "t.md"}) == "memory"
    assert personal["share_app"]({"app_slug": "a", "target_agent": "b"}) == ""
    assert personal["share_app"]({"app_slug": "a", "target_agent": "b", "source_visibility": "user"}) == "apps"
    assert personal["share_app"]({"app_slug": "a", "target_agent": "b", "visibility": "user"}) == "apps"
    for name in ("update_followed_app", "send_release_to_source"):
        assert personal[name]({"app_slug": "a", "visibility": "agent"}) == ""
        assert personal[name]({"app_slug": "a", "visibility": "user"}) == "apps"
    for name in ("attach_library", "create_department", "set_department", "move_agent", "install_agent",
                 "install_department_set"):
        assert personal[name]({}) == "", name


def test_hand_files_refuses_a_viewer_a_missing_file_and_a_path_outside_the_space(temp_db):
    import config
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    _person(MEMBER, "viewer", {AGENT: roles.VIEWER})
    root = config.get_agent_dir(AGENT)
    (root / "users" / "viewer" / "workspace").mkdir(parents=True)
    (root / "users" / "viewer" / "workspace" / "a.md").write_text("a")
    token = _own_session("sid-hand-v", MEMBER, "chat-hand-v")
    r = _draw(token, "sid-hand-v", _do("hand_files", {"paths": ["a.md"]}))
    assert r.status_code == 409 and r.json()["detail"] == actions_team.HAND_NEEDS_CONTRIBUTOR
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    r = _draw(token, "sid-hand-v", _do("hand_files", {"paths": ["nope.md"]}))
    assert r.status_code == 409 and r.json()["detail"] == 'There is no file named "nope.md" in your space.'
    r = _draw(token, "sid-hand-v", _do("hand_files", {"paths": ["../../workspace/x.md"]}))
    assert r.status_code == 409 and "is not inside your space" in r.json()["detail"]
    r = _draw(token, "sid-hand-v", _do("hand_files", {"paths": ["a.md"], "dest_dir": "knowledge"}))
    assert r.status_code == 409 and r.json()["detail"] == actions_team.BAD_DEST


def test_hand_files_names_every_file_marks_folders_and_refuses_twice_and_overlaps(temp_db):
    import config
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    root = config.get_agent_dir(AGENT)
    space = root / "users" / "viewer" / "workspace"
    (space / "photos").mkdir(parents=True)
    (space / "photos" / "crack.jpg").write_bytes(b"jpg")
    for name in ("a.md", "b.md", "c.md", "d.md"):
        (space / name).write_text(name)
    (root / "workspace" / "reports").mkdir(parents=True)
    token = _own_session("sid-hand-names", MEMBER, "chat-hand-names")
    r = _draw(token, "sid-hand-names", _do("hand_files", {"paths": ["a.md", "b.md", "c.md", "d.md", "photos"],
                                                          "dest_dir": "workspace/reports"}))
    assert r.status_code == 200, r.text
    card = db_cards.get_card(r.json()["card_id"])
    assert card["title"] == "Hand 4 files and a folder to the team"
    assert card["text"] == ('Moves "a.md", "b.md", "c.md", "d.md" and the folder "photos" from your space to '
                            'the team\'s "reports" folder, where everyone on the agent reads them.')
    r = _draw(token, "sid-hand-names", _do("hand_files", {"paths": ["a.md", "/users/viewer/workspace/a.md"]}))
    assert r.status_code == 409 and r.json()["detail"] == '"a.md" is named twice.'
    r = _draw(token, "sid-hand-names", _do("hand_files", {"paths": ["photos/crack.jpg", "photos"]}))
    assert r.status_code == 409 and r.json()["detail"] == '"photos/crack.jpg" is inside "photos". Name one of them.'
    # The outcome names the subfolder.
    card_id = _draw(token, "sid-hand-names", _do("hand_files", {"paths": ["a.md"], "dest_dir": "workspace/reports"})).json()["card_id"]
    r = _cookie(MEMBER, "viewer@test.com", roles.MEMBER).post(f"/v1/cards/{card_id}/act", json={"button": "go"})
    assert r.json()["outcome"]["text"] == '1 file is in the team\'s "reports" folder.'
    assert (root / "workspace" / "reports" / "a.md").exists()


def test_hand_files_needs_both_spaces(temp_db):
    import config
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN, collaborative=False, default_scope="user")
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    (config.get_agent_dir(AGENT) / "users" / "viewer" / "workspace").mkdir(parents=True)
    (config.get_agent_dir(AGENT) / "users" / "viewer" / "workspace" / "a.md").write_text("a")
    token = _own_session("sid-hand-po", MEMBER, "chat-hand-po")
    r = _draw(token, "sid-hand-po", _do("hand_files", {"paths": ["a.md"]}))
    assert r.status_code == 409 and r.json()["detail"] == "PA has no shared space to hand files to."


def test_hand_files_copies_when_the_person_keeps_a_copy_and_rechecks_the_role(temp_db):
    import config
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    root = config.get_agent_dir(AGENT)
    (root / "users" / "viewer" / "workspace").mkdir(parents=True)
    (root / "users" / "viewer" / "workspace" / "a.md").write_text("a")
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    token = _own_session("sid-hand-c", MEMBER, "chat-hand-c")
    card_id = _draw(token, "sid-hand-c", _do("hand_files", {"paths": ["a.md"]})).json()["card_id"]
    member = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "go", "options": {"copy": True}})
    assert r.status_code == 200 and r.json()["outcome"]["status"] == st.OUTCOME_DONE, r.text
    assert (root / "workspace" / "a.md").exists() and (root / "users" / "viewer" / "workspace" / "a.md").exists()
    # The role lost between the draw and the click: the click fails with the sentence.
    card_id = _draw(token, "sid-hand-c", _do("hand_files", {"paths": ["a.md"]})).json()["card_id"]
    _person(MEMBER, "viewer", {AGENT: roles.VIEWER})
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "go"})
    assert r.status_code == 200 and r.json()["outcome"]["status"] == st.OUTCOME_FAILED
    assert r.json()["outcome"]["text"] == actions_team.HAND_NEEDS_CONTRIBUTOR


def test_hand_files_refuses_a_link_on_either_side_at_the_draw_and_at_the_click(temp_db):
    import os

    import config
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    root = config.get_agent_dir(AGENT)
    space = root / "users" / "viewer" / "workspace"
    space.mkdir(parents=True)
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    private = root / "users" / "viewer" / "context" / "memory"
    private.mkdir(parents=True)
    (private / "health.md").write_text("private")
    (space / "note.md").write_text("note")
    token = _own_session("sid-hand-link", MEMBER, "chat-hand-link")
    # A destination that is a link into another person's private memory.
    os.symlink(private, root / "workspace" / "drop")
    r = _draw(token, "sid-hand-link", _do("hand_files", {"paths": ["note.md"], "dest_dir": "workspace/drop"}))
    assert r.status_code == 409 and r.json()["detail"] == actions_team.LINKED_PATH
    # A source that is a link to a private memory topic, and one behind a linked folder.
    os.symlink(private / "health.md", space / "report.md")
    r = _draw(token, "sid-hand-link", _do("hand_files", {"paths": ["report.md"]}))
    assert r.status_code == 409 and r.json()["detail"] == actions_team.LINKED_PATH
    os.symlink(private, space / "sub")
    r = _draw(token, "sid-hand-link", _do("hand_files", {"paths": ["sub/health.md"]}))
    assert r.status_code == 409 and r.json()["detail"] == actions_team.LINKED_PATH
    # A link that appears between the draw and the press: the click refuses.
    card_id = _draw(token, "sid-hand-link", _do("hand_files", {"paths": ["note.md"]})).json()["card_id"]
    (space / "note.md").unlink()
    os.symlink(private / "health.md", space / "note.md")
    member = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "go"})
    assert r.status_code == 200 and r.json()["outcome"]["status"] == st.OUTCOME_FAILED
    assert r.json()["outcome"]["text"] == actions_team.LINKED_PATH
    assert (private / "health.md").read_text() == "private" and not (root / "workspace" / "health.md").exists()


def test_promote_memory_calls_the_promotion_as_the_person_and_refuses_below_editor(temp_db, monkeypatch):
    import config
    from services.memory import memory_file
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    root = memory_file.scope_root(config.get_agent_dir(AGENT), visibility.SCOPE_USER, "viewer")
    root.mkdir(parents=True)
    (root / "suppliers.md").write_text("# Suppliers\n")
    calls = []

    async def fake_promote(agent, username, rel, *, role, user_sub, dst_rel, caps):
        calls.append((agent, username, rel, role, user_sub, dst_rel, caps is not None))
        return object()

    monkeypatch.setattr("services.memory.memory_promote.promote", fake_promote)
    token = _own_session("sid-prom", MEMBER, "chat-prom")
    r = _draw(token, "sid-prom", _do("promote_memory", {"topic": "/memories/user/suppliers.md", "new_name": "team-suppliers.md"}))
    assert r.status_code == 200, r.text
    card = db_cards.get_card(r.json()["card_id"])
    assert card["title"] == 'Share the memory topic "suppliers.md" with the team'
    assert 'Its shared name will be "team-suppliers.md".' in card["text"]
    member = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    r = member.post(f"/v1/cards/{card['id']}/act", json={"button": "go"})
    assert r.status_code == 200 and r.json()["outcome"]["status"] == st.OUTCOME_DONE, r.text
    assert r.json()["outcome"]["text"] == 'The topic "team-suppliers.md" is now shared with everyone on PA.'
    assert calls == [(AGENT, "viewer", "suppliers.md", roles.EDITOR, MEMBER, "team-suppliers.md", True)]
    # A contributor is refused at the draw with the route's own sentence; a
    # topic that is not there, with its own.
    _person(MEMBER, "viewer", {AGENT: roles.CONTRIBUTOR})
    r = _draw(token, "sid-prom", _do("promote_memory", {"topic": "suppliers.md"}))
    assert r.status_code == 409 and r.json()["detail"].startswith("Only editors and managers can share a memory")
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    r = _draw(token, "sid-prom", _do("promote_memory", {"topic": "nowhere.md"}))
    assert r.status_code == 409 and r.json()["detail"] == 'There is no memory topic named "nowhere.md" in your private memory.'
    r = _draw(token, "sid-prom", _do("promote_memory", {"topic": "/memories/agent/suppliers.md"}))
    assert r.status_code == 409 and r.json()["detail"] == "Only a topic under /memories/user/ can be shared."
    # promote()'s own checks at the draw: a text extension, a shared name taken, a scope turned off.
    r = _draw(token, "sid-prom", _do("promote_memory", {"topic": "suppliers"}))
    assert r.status_code == 409 and not r.json()["detail"].startswith("Error")
    shared = memory_file.scope_root(config.get_agent_dir(AGENT), visibility.SCOPE_AGENT)
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "suppliers.md").write_text("# Shared\n")
    r = _draw(token, "sid-prom", _do("promote_memory", {"topic": "suppliers.md"}))
    assert r.status_code == 409 and r.json()["detail"] == 'The shared memory already has a topic named "suppliers.md".'
    from storage.agents import memory_store
    memory_store.set_agent_toggle(AGENT, "agent_memory_enabled", False)
    r = _draw(token, "sid-prom", _do("promote_memory", {"topic": "suppliers.md", "new_name": "other.md"}))
    assert r.status_code == 409 and r.json()["detail"] == "PA keeps no shared memory, so nothing can be shared."
    from services.cards import actions as _actions
    assert _actions.plain("Error: a promotion names one topic file.") == "A promotion names one topic file."


def test_attach_library_takes_the_routes_rule_on_both_sides(temp_db, monkeypatch):
    from storage.knowledge import db_knowledge_libraries
    agent_store.create_agent("source", "Source", created_by=ADMIN)
    agent_store.create_agent("target", "Target", created_by=ADMIN)
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    db_knowledge_libraries.promote("source", created_by=ADMIN)
    # A creator who manages the target and reaches the source.
    _person(CREATOR, "manager", {AGENT: roles.MANAGER, "target": roles.MANAGER, "source": roles.VIEWER})
    token = _own_session("sid-att", CREATOR, "chat-att")
    r = _draw(token, "sid-att", _do("attach_library", {"source_agent": "source", "target_agent": "target"}))
    assert r.status_code == 200, r.text
    card = db_cards.get_card(r.json()["card_id"])
    assert card["title"] == "Attach the library of Source to Target"
    assert card["text"].startswith("Target reads the shared knowledge library of Source.")
    creator = _cookie(CREATOR, "manager@test.com", roles.CREATOR)
    r = creator.post(f"/v1/cards/{card['id']}/act", json={"button": "go"})
    assert r.status_code == 200 and r.json()["outcome"]["status"] == st.OUTCOME_DONE, r.text
    assert r.json()["outcome"]["text"] == "Target now reads the library of Source."
    assert db_knowledge_libraries.attachments_for_consumer("target")
    # A creator without manager on the target: the route's sentence, with a full stop.
    _person(CREATOR, "manager", {AGENT: roles.MANAGER, "target": roles.EDITOR, "source": roles.VIEWER})
    r = _draw(token, "sid-att", _do("attach_library", {"source_agent": "source", "target_agent": "target"}))
    assert r.status_code == 409 and r.json()["detail"] == "A creator wires a library only to an agent they manage."
    # A member is no creator; a writable attach needs editor on the source.
    _person(MEMBER, "viewer", {AGENT: roles.MANAGER, "target": roles.MANAGER, "source": roles.VIEWER})
    token_m = _own_session("sid-att-m", MEMBER, "chat-att-m")
    r = _draw(token_m, "sid-att-m", _do("attach_library", {"source_agent": "source", "target_agent": "target"}))
    assert r.status_code == 409 and r.json()["detail"] == actions_team.ATTACH_NEEDS_CREATOR
    _person(CREATOR, "manager", {AGENT: roles.MANAGER, "target": roles.MANAGER, "source": roles.VIEWER})
    r = _draw(token, "sid-att", _do("attach_library", {"source_agent": "source", "target_agent": "target", "writable": True}))
    assert r.status_code == 409 and r.json()["detail"].startswith("Writable attachment requires editor")
    agent_store.create_agent("plain", "Plain", created_by=ADMIN)
    _person(CREATOR, "manager", {AGENT: roles.MANAGER, "target": roles.MANAGER, "source": roles.VIEWER, "plain": roles.VIEWER})
    r = _draw(token, "sid-att", _do("attach_library", {"source_agent": "plain", "target_agent": "target"}))
    assert r.status_code == 409 and r.json()["detail"] == "Plain has no shared knowledge library there."


def test_attach_library_names_the_change_on_an_existing_attachment(temp_db):
    from storage.knowledge import db_knowledge_libraries
    agent_store.create_agent("source", "Source", created_by=ADMIN)
    agent_store.create_agent("target", "Target", created_by=ADMIN)
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    db_knowledge_libraries.promote("source", created_by=ADMIN)
    db_knowledge_libraries.attach("source", "target", writable=False, created_by=ADMIN)
    _person(CREATOR, "manager", {AGENT: roles.MANAGER, "target": roles.MANAGER, "source": roles.EDITOR})
    token = _own_session("sid-att-x", CREATOR, "chat-att-x")
    # A card that changes nothing (no flag, or the same flag) is refused.
    for params in ({"source_agent": "source", "target_agent": "target"},
                   {"source_agent": "source", "target_agent": "target", "writable": False}):
        r = _draw(token, "sid-att-x", _do("attach_library", params))
        assert r.status_code == 409 and r.json()["detail"] == "Target already reads the shared knowledge library of Source."
    r = _draw(token, "sid-att-x", _do("attach_library", {"source_agent": "source", "target_agent": "target", "writable": True}))
    assert r.status_code == 200, r.text
    card = db_cards.get_card(r.json()["card_id"])
    assert card["title"] == "Let Target write to the library of Source"
    assert card["text"] == "Target already reads the shared knowledge library of Source. This card lets it write there too."
    r = _cookie(CREATOR, "manager@test.com", roles.CREATOR).post(f"/v1/cards/{card['id']}/act", json={"button": "go"})
    assert r.json()["outcome"]["text"] == "Target can now write to the library of Source.", r.text
    (row,) = db_knowledge_libraries.attachments_for_consumer("target")
    assert row["writable"] is True


def test_update_and_send_cards_take_the_follow_rule_on_both_sides_and_say_the_release(temp_db, monkeypatch):
    import json

    from services.apps import app_follow, releases
    from storage import db_apps
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    agent_store.create_agent("source", "Source", created_by=ADMIN)
    _person(MEMBER, "viewer", {AGENT: roles.VIEWER, "source": roles.EDITOR})
    # The source's app and the copy that follows it: rows alone, since the
    # role checks and the no-release refusals need no files.
    src = db_apps.upsert_app("source", "", None, "board", title="Crew board", rel_path="workspace/apps/board",
                             kind="folder")
    copy = db_apps.upsert_app(AGENT, "", None, "board", title="Crew board", rel_path="workspace/apps/board",
                              kind="folder")
    lineage = json.dumps({"id": src["id"], "agent": "source", "username": "", "slug": "board",
                          "release": 1, "copy_release": 1})
    db_apps.set_follows(copy["id"], lineage)
    token = _own_session("sid-upd", MEMBER, "chat-upd")
    update = _do("update_followed_app", {"app_slug": "board", "mode": "update"})
    merge = _do("update_followed_app", {"app_slug": "board", "mode": "merge"})
    send = _do("send_release_to_source", {"app_slug": "board"})
    # A viewer of the copy; an editor of the copy alone; an editor on both sides with no release anywhere.
    r = _draw(token, "sid-upd", update)
    assert r.status_code == 409 and r.json()["detail"] == app_follow.COPY_NEEDS_EDITOR
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR, "source": roles.VIEWER})
    r = _draw(token, "sid-upd", send)
    assert r.status_code == 409 and r.json()["detail"] == "Only editors and managers of Source take its releases."
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR, "source": roles.EDITOR})
    r = _draw(token, "sid-upd", update)
    assert r.status_code == 409 and r.json()["detail"] == "Source's app has no newer release."
    r = _draw(token, "sid-upd", merge)
    assert r.status_code == 409 and r.json()["detail"] == "Source's app has no newer release."
    r = _draw(token, "sid-upd", send)
    assert r.status_code == 409 and r.json()["detail"] == app_follow.NOTHING_TO_SEND
    r = _draw(token, "sid-upd", _do("update_followed_app", {"app_slug": "nope", "mode": "update"}))
    assert r.status_code == 409 and r.json()["detail"] == 'There is no app named "nope" on PA.'
    db_apps.set_follows(copy["id"], "")
    r = _draw(token, "sid-upd", update)
    assert r.status_code == 409 and r.json()["detail"] == app_follow.NOT_FOLLOWING
    db_apps.set_follows(copy["id"], lineage)
    # The words and the click, with the module's reading and moves in place of the files.
    state = {"source": {"agent": "source", "agent_name": "Source", "slug": "board", "release": 1, "exists": True},
             "update": {"release": 3, "mode": "update"}}
    monkeypatch.setattr(app_follow, "refusal", lambda row, u, move: None)
    monkeypatch.setattr(app_follow, "state", lambda row: dict(state))
    monkeypatch.setattr(releases, "current_number", lambda row: 2)
    moves: list = []

    async def fake_take(row, u):
        moves.append(("take", row["id"], u.acting_sub))
        return {"release": 2, "source_release": 3, "text": "Release 2 of \"board\" waits for approval here."}

    async def fake_stage(row, u):
        moves.append(("merge", row["id"], u.acting_sub))
        return {"release": 3, "path": "/workspace/apps/board.source-r3", "text": "Release 3 is unpacked."}

    async def fake_send(row, u):
        moves.append(("send", row["id"], u.acting_sub))
        return {"release": 2, "text": "Release 2 of Source's app waits for approval there."}

    monkeypatch.setattr(app_follow, "take", fake_take)
    monkeypatch.setattr(app_follow, "stage_merge", fake_stage)
    monkeypatch.setattr(app_follow, "send_back", fake_send)
    member = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    r = _draw(token, "sid-upd", update)
    assert r.status_code == 200, r.text
    card = db_cards.get_card(r.json()["card_id"])
    assert card["title"] == 'Update the app "Crew board" to release 3'
    assert card["text"] == ('Replaces the files of "board" with release 3 of Source\'s app. The app\'s data stays. '
                            "The new release waits for approval here.")
    assert card["buttons"][0]["label"] == "Update"
    r = member.post(f"/v1/cards/{card['id']}/act", json={"button": "go"})
    assert r.status_code == 200 and r.json()["outcome"]["status"] == st.OUTCOME_DONE, r.text
    assert r.json()["outcome"]["text"] == 'Release 2 of "board" waits for approval here.'
    card = db_cards.get_card(_draw(token, "sid-upd", merge).json()["card_id"])
    assert card["title"] == 'Ask the agent to merge release 3 into "Crew board"'
    assert card["text"] == ("Unpacks release 3 of Source's app beside this app's files. The agent merges the two into "
                            "one release and deploys it as always.")
    assert card["buttons"][0]["label"] == "Merge"
    member.post(f"/v1/cards/{card['id']}/act", json={"button": "go"})
    card = db_cards.get_card(_draw(token, "sid-upd", send).json()["card_id"])
    assert card["title"] == 'Send release 2 of "Crew board" to Source'
    assert card["text"] == ('Puts this app\'s release 2 into Source\'s app "board" as a new release, which waits for '
                            "approval there. The data of neither app moves.")
    assert card["buttons"][0]["label"] == "Send"
    r = member.post(f"/v1/cards/{card['id']}/act", json={"button": "go"})
    assert r.json()["outcome"]["text"] == "Release 2 of Source's app waits for approval there."
    assert moves == [("take", copy["id"], MEMBER), ("merge", copy["id"], MEMBER), ("send", copy["id"], MEMBER)]


def test_the_follow_cards_check_both_sides_again_at_the_click(temp_db, monkeypatch):
    """A card drawn while the person had the rights on both sides refuses
    at the click once they lost them: the click runs the module's real
    rule, never the draw's verdict."""
    import json

    from services.apps import app_follow
    from storage import db_apps
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    agent_store.create_agent("source", "Source", created_by=ADMIN)
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR, "source": roles.EDITOR})
    src = db_apps.upsert_app("source", "", None, "board", title="Crew board", rel_path="workspace/apps/board",
                             kind="folder")
    copy = db_apps.upsert_app(AGENT, "", None, "board", title="Crew board", rel_path="workspace/apps/board",
                              kind="folder")
    db_apps.set_follows(copy["id"], json.dumps({"id": src["id"], "agent": "source", "username": "",
                                                "slug": "board", "release": 1, "copy_release": 1}))
    token = _own_session("sid-click", MEMBER, "chat-click")
    # Drawn with the module's reading in place of the files, so the draw passes.
    real_refusal = app_follow.refusal
    monkeypatch.setattr(app_follow, "refusal", lambda row, u, move: None)
    monkeypatch.setattr(app_follow, "state", lambda row: {
        "source": {"agent": "source", "agent_name": "Source", "slug": "board", "release": 1, "exists": True},
        "update": {"release": 3, "mode": "update"}})
    update = db_cards.get_card(_draw(token, "sid-click", _do(
        "update_followed_app", {"app_slug": "board", "mode": "update"})).json()["card_id"])
    send = db_cards.get_card(_draw(token, "sid-click", _do(
        "send_release_to_source", {"app_slug": "board"})).json()["card_id"])
    monkeypatch.setattr(app_follow, "refusal", real_refusal)
    member = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    # The source's role is gone by the click.
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR, "source": roles.VIEWER})
    r = member.post(f"/v1/cards/{update['id']}/act", json={"button": "go"})
    assert r.status_code == 200 and r.json()["outcome"]["status"] == st.OUTCOME_FAILED, r.text
    assert r.json()["outcome"]["text"] == "Only editors and managers of Source take its releases."
    # The copy's role is gone by the click.
    _person(MEMBER, "viewer", {AGENT: roles.VIEWER, "source": roles.EDITOR})
    r = member.post(f"/v1/cards/{send['id']}/act", json={"button": "go"})
    assert r.status_code == 200 and r.json()["outcome"]["status"] == st.OUTCOME_FAILED, r.text
    assert r.json()["outcome"]["text"] == app_follow.COPY_NEEDS_EDITOR


def test_create_department_runs_the_route_for_a_creator_and_refuses_a_taken_name(temp_db):
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    _person(CREATOR, "manager", {AGENT: roles.MANAGER})
    token = _own_session("sid-dept", CREATOR, "chat-dept")
    r = _draw(token, "sid-dept", _do("create_department", {"name": "Marketing", "levels": ["Head", "Team"],
                                                          "mode": "down_across", "reach": "subtree"}))
    assert r.status_code == 200, r.text
    card = db_cards.get_card(r.json()["card_id"])
    assert card["title"] == 'Create the department "Marketing"'
    assert card["text"] == ('Creates the department "Marketing" with the levels Head, Team. Delegation runs '
                            "downward and across each level and reaches every level below.")
    creator = _cookie(CREATOR, "manager@test.com", roles.CREATOR)
    r = creator.post(f"/v1/cards/{card['id']}/act", json={"button": "go"})
    assert r.status_code == 200 and r.json()["outcome"]["status"] == st.OUTCOME_DONE, r.text
    dept = next(d for d in db_departments.list_departments() if d["name"] == "Marketing")
    assert dept["mode"] == "down_across" and dept["reach"] == "subtree" and dept["created_by_sub"] == CREATOR
    assert r.json()["outcome"]["department_id"] == dept["id"]
    assert r.json()["outcome"]["text"] == ('The department "Marketing" exists. Place agents in it '
                                           "with a card, or from the Agents page.")
    r = _draw(token, "sid-dept", _do("create_department", {"name": "marketing"}))
    assert r.status_code == 409 and r.json()["detail"] == 'A department named "marketing" already exists.'
    _person(MEMBER, "viewer", {AGENT: roles.MANAGER})
    r = _draw(_own_session("sid-dept-m", MEMBER, "chat-dept-m"), "sid-dept-m", _do("create_department", {"name": "Ops"}))
    assert r.status_code == 409 and r.json()["detail"] == actions_departments.CREATORS_ONLY


def test_set_department_is_the_creators_or_an_admins(temp_db):
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    _person(CREATOR, "manager", {AGENT: roles.MANAGER})
    mine = db_departments.create_department("Mine", CREATOR, level_names=["Head", "Team"])
    theirs = db_departments.create_department("Theirs", ADMIN, level_names=["Head", "Team"])
    token = _own_session("sid-set-d", CREATOR, "chat-set-d")
    r = _draw(token, "sid-set-d", _do("set_department", {"department_id": theirs["id"], "mode": "both"}))
    assert r.status_code == 409 and r.json()["detail"] == actions_departments.CREATOR_OR_ADMIN
    r = _draw(token, "sid-set-d", _do("set_department", {"department_id": mine["id"]}))
    assert r.status_code == 409 and r.json()["detail"] == actions_departments.NOTHING_TO_SET
    r = _draw(token, "sid-set-d", _do("set_department", {"department_id": mine["id"], "mode": "both", "reach": "subtree"}))
    assert r.status_code == 200, r.text
    card = db_cards.get_card(r.json()["card_id"])
    assert card["title"] == 'Set the delegation of "Mine"'
    assert card["text"] == 'Delegation in "Mine" runs in every direction and reaches every level below.'
    creator = _cookie(CREATOR, "manager@test.com", roles.CREATOR)
    r = creator.post(f"/v1/cards/{card['id']}/act", json={"button": "go"})
    assert r.status_code == 200 and r.json()["outcome"]["status"] == st.OUTCOME_DONE, r.text
    assert db_departments.get_department(mine["id"])["mode"] == "both"
    assert r.json()["outcome"]["text"] == 'Delegation in "Mine" now runs in every direction and reaches every level below.'


# --- the say gates and the list cards --------------------------------------------------

def test_a_say_press_is_refused_before_the_claim_when_the_person_may_not_send(temp_db, monkeypatch):
    from core.events import input_queue
    queued = _queue_spy(monkeypatch)
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    say = [{"id": "yes", "kind": st.BUTTON_SAY, "label": "Yes", "message": "Yes."}]
    # The agent's shared space: the editor tier answers, a viewer does not.
    task_store.create_chat("chat-say-shared", visibility.shared_chat_owner(AGENT), AGENT)
    task_store.update_chat("chat-say-shared", session_id="sid-say-shared")
    token = live_session_token("sid-say-shared", AGENT, user_sub=MEMBER)
    card_id = _draw(token, "sid-say-shared", say).json()["card_id"]
    _person(MEMBER, "viewer", {AGENT: roles.VIEWER})
    member = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "yes"})
    assert r.status_code == 403 and r.json()["detail"] == st.SHARED_NEEDS_EDITOR
    assert db_cards.clicks_of(card_id) == [] and queued == []
    # A full queue refuses with the composer's sentence and spends no click.
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    monkeypatch.setattr(input_queue, "CAP", 0)
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "yes"})
    assert r.status_code == 409 and r.json()["detail"] == st.TOO_MANY_QUEUED
    assert db_cards.clicks_of(card_id) == [] and db_cards.get_card(card_id)["resolved_at"] is None
    monkeypatch.setattr(input_queue, "CAP", 64)
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "yes"})
    assert r.status_code == 200 and r.json()["queued"] is True and len(queued) == 1


def test_a_say_press_whose_queue_fills_meanwhile_gives_the_click_back(temp_db, monkeypatch):
    from core.events import input_queue
    token = _own_session("sid-say-full", ADMIN, "chat-say-full")
    card_id = _draw(token, "sid-say-full", [{"id": "yes", "kind": st.BUTTON_SAY, "label": "Yes", "message": "Yes."}]).json()["card_id"]

    async def full(self, queue_id, author_sub, item, **kw):
        return input_queue.FULL, None, -1

    monkeypatch.setattr(input_queue.ChatInputQueue, "add", full)
    admin = _cookie(ADMIN, "admin@test.com", roles.ADMIN)
    r = admin.post(f"/v1/cards/{card_id}/act", json={"button": "yes"})
    assert r.status_code == 409 and r.json()["detail"] == st.TOO_MANY_QUEUED
    assert db_cards.clicks_of(card_id) == [] and db_cards.get_card(card_id)["resolved_at"] is None


def test_a_say_press_whose_send_fails_gives_the_click_back(temp_db, monkeypatch):
    from core.events import input_queue
    token = _own_session("sid-say-boom", ADMIN, "chat-say-boom")
    card_id = _draw(token, "sid-say-boom", [{"id": "yes", "kind": st.BUTTON_SAY, "label": "Yes", "message": "Yes."}]).json()["card_id"]

    async def boom(self, queue_id, author_sub, item, **kw):
        raise RuntimeError("the queue's row was not written")

    monkeypatch.setattr(input_queue.ChatInputQueue, "add", boom)
    r = _cookie(ADMIN, "admin@test.com", roles.ADMIN).post(f"/v1/cards/{card_id}/act", json={"button": "yes"})
    assert r.status_code == 503 and r.json()["detail"] == st.NO_ANSWER
    assert db_cards.clicks_of(card_id) == [] and db_cards.get_card(card_id)["resolved_at"] is None


def test_an_interrupted_action_never_stays_running(temp_db, monkeypatch):
    card_id, _dept_row = _drawn_move()

    async def interrupted(params, options, u):
        raise asyncio.CancelledError()

    monkeypatch.setitem(actions.CATALOG, "move_agent", actions.Action(
        name="move_agent", schema=actions.MOVE_AGENT.schema, words=actions.MOVE_AGENT.words,
        check=actions.MOVE_AGENT.check, run=interrupted))
    card = db_cards.get_card(card_id)
    admin = UserContext(sub=ADMIN, email="admin@test.com", name="A", role=roles.ADMIN, agents=[], agent_roles={})
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cards.act(card, "go", None, admin))
    (click,) = db_cards.clicks_of(card_id)
    assert click["status"] == db_cards.CLICK_FAILED and click["detail"] == st.INTERRUPTED
    assert db_cards.get_card(card_id)["outcome"]["status"] == st.OUTCOME_FAILED


def _list(token: str, sid: str, kind: str, filters: dict | None = None):
    return _bearer(token).post("/v1/cards/list", json={"session_id": sid, "kind": kind, "filters": filters})


def test_a_list_card_is_a_block_for_the_person_with_the_kinds_filters(temp_db):
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    token = _own_session("sid-list", ADMIN, "chat-list")
    r = _list(token, "sid-list", "tasks", {"scope": "mine", "status": "paused"})
    assert r.status_code == 200 and r.json() == {"status": "ok", "kind": "tasks", "title": "Your scheduled tasks"}, r.text
    (item,) = _drain("sid-list")
    assert item == {"event_type": wire.ITEM_LIST_CARD, "kind": "tasks", "filters": {"scope": "mine", "status": "paused"},
                    "title": "Your scheduled tasks", "person": ADMIN}
    assert _list(token, "sid-list", "machines").json()["title"] == "Remote machines"
    assert _list(token, "sid-list", "triggers", {"scope": "agent"}).json()["title"] == "Triggers"
    r = _list(token, "sid-list", "inbox")
    assert r.status_code == 404 and r.json()["detail"] == st.NO_SUCH_LIST
    r = _list(token, "sid-list", "machines", {"scope": "mine"})
    assert r.status_code == 400 and r.json()["detail"] == "The machines list has no filter named scope."
    r = _list(token, "sid-list", "tasks", {"status": "done"})
    assert r.status_code == 400 and r.json()["detail"] == "The filter status takes one of active, paused."


def test_a_list_card_takes_the_pages_role_and_the_person_present_rule(temp_db):
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR})
    token = _own_session("sid-list-m", MEMBER, "chat-list-m")
    r = _list(token, "sid-list-m", "mcps")
    assert r.status_code == 409 and r.json()["detail"] == "Only managers and admins see the agent's MCP servers."
    _person(MEMBER, "viewer", {AGENT: roles.MANAGER})
    assert _list(token, "sid-list-m", "mcps").status_code == 200
    # Nobody at the keyboard: an agent-scope task's chat, a session naming nobody.
    task_store.create_chat("chat-list-task", visibility.task_chat_owner(AGENT), AGENT, source_type=session_kind.TASK.source_type)
    task_store.update_chat("chat-list-task", session_id="sid-list-task")
    r = _list(live_session_token("sid-list-task", AGENT, user_sub=ADMIN), "sid-list-task", "tasks")
    assert r.status_code == 403 and r.json()["detail"] == st.NO_PERSON
    r = _list(live_session_token("sid-list-m", AGENT), "sid-list-m", "tasks")
    assert r.status_code == 403 and r.json()["detail"] == st.NO_PERSON


def test_the_list_cards_count_toward_the_open_card_cap(temp_db, monkeypatch):
    monkeypatch.setattr(db_cards, "MAX_OPEN_PER_CHAT", 2)
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    token = _own_session("sid-list-cap", ADMIN, "chat-list-cap")
    for _ in range(2):
        block = _list(token, "sid-list-cap", "tasks").json()
        task_store.add_chat_message("chat-list-cap", "event", "", event_type=wire.LIST_CARD,
                                    event_data='{"type": "list_card", "kind": "tasks"}')
        assert block["status"] == "ok"
    r = _list(token, "sid-list-cap", "tasks")
    assert r.status_code == 409 and r.json()["detail"] == cards.TOO_MANY_OPEN


def test_share_app_copies_in_process_with_the_hooks_gates_and_refuses_a_taken_slug(temp_db, monkeypatch):
    from pathlib import Path as _Path

    from services.apps import app_blueprints
    agent_store.create_agent(AGENT, "PA", created_by=ADMIN)
    agent_store.create_agent("target", "Target", created_by=ADMIN)
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR, "target": roles.EDITOR})
    rows = {(AGENT, "", "board"): {"agent": AGENT, "username": "", "slug": "board", "title": "Crew board",
                                   "rel_path": "workspace/apps/board", "kind": "folder"}}
    monkeypatch.setattr(task_store, "get_app_by_slug", lambda agent, username, slug: rows.get((agent, username, slug)))
    monkeypatch.setattr(actions_team.db_apps, "app_kind_of",
                        lambda row: type("K", (), {"serves_tree": bool(row and row.get("kind") == "folder")})())
    seen: dict = {}

    def fake_export(row, **kw):
        seen["export"] = (row["slug"], kw.get("release_dir"))
        return b"tar", {"slug": row["slug"]}

    def fake_read(data):
        seen["read"] = data
        folder = _Path("/tmp/otodock-cards-test-bundle")
        folder.mkdir(exist_ok=True)
        return folder, {"slug": "board", "tasks": [{"slug": "t1"}], "from_template": "yacht-crew", "from_version": "0.2.2"}

    import config
    answers: list = []

    async def fake_import(agent, username, owner_sub, slug, folder, **kw):
        seen["import"] = (agent, username, owner_sub, slug, kw["template_slug"], kw["tasks"], kw["origin"])
        answer = answers.pop(0)
        if answer["status"] == "refused":
            # What a refused deploy leaves behind: the written folder and the seeded task.
            written = config.get_agent_dir(agent) / "workspace" / "apps" / slug / "client"
            written.mkdir(parents=True, exist_ok=True)
            (written / "index.html").write_text("<p>board</p>")
            task_store.create_dynamic_task(f"task-app-{slug}-t1", agent, "Board refresh", "p", "cli", "scheduled",
                                           "0 9 * * 1", None, None, 600, None, scope="agent",
                                           community_template=kw["template_slug"])
        return answer

    monkeypatch.setattr(app_blueprints, "export_bundle", fake_export)
    monkeypatch.setattr(app_blueprints, "read_bundle", fake_read)
    monkeypatch.setattr(app_blueprints, "import_folder", fake_import)
    from services.infra import file_bookkeeping
    pushed_deletes: list = []

    async def fake_push_delete(agent, rel, *a, **kw):
        pushed_deletes.append((agent, rel))
    monkeypatch.setattr(file_bookkeeping, "push_file_delete", fake_push_delete)
    token = _own_session("sid-share", MEMBER, "chat-share")
    member = _cookie(MEMBER, "viewer@test.com", roles.MEMBER)
    share = _do("share_app", {"app_slug": "board", "target_agent": "target"})
    # Pending: the release waits for approval on the target.
    r = _draw(token, "sid-share", share)
    assert r.status_code == 200, r.text
    card = db_cards.get_card(r.json()["card_id"])
    assert card["title"] == 'Share the app "Crew board" with Target'
    # An app with no manifest to approve and no secret: the draw promises no approval.
    assert card["text"] == ('Copies the app "board" from PA to the shared apps of Target. '
                            "It goes live there once its checks pass. The app's data stays here.")
    answers.append({"status": "pending approval", "app_id": "app-1", "release": 1})
    r = member.post(f"/v1/cards/{card['id']}/act", json={"button": "go"})
    assert r.status_code == 200 and r.json()["outcome"]["status"] == st.OUTCOME_DONE, r.text
    assert r.json()["outcome"]["text"] == 'The app "board" is on Target. Its first release waits for approval there.'
    # The working folder: the fake row has no live release.
    assert seen["export"] == ("board", None) and seen["read"] == b"tar"
    assert seen["import"] == ("target", "", None, "board", "bundle:board", [{"slug": "t1"}], ("yacht-crew", "0.2.2"))
    # Ok: the copy went live at once.
    answers.append({"status": "ok", "app_id": "app-1", "release": 1})
    card_id = _draw(token, "sid-share", share).json()["card_id"]
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "go"})
    assert r.json()["outcome"]["status"] == st.OUTCOME_DONE and r.json()["outcome"]["text"] == 'The app "board" is live on Target.'
    # Refused: the deploy's own reason, and what the copy left on the target is removed.
    answers.append({"status": "refused", "slug": "board", "problems": 1, "warnings": 0,
                    "findings": [{"rule": "client.x", "severity": "fail", "file": "client/app.js", "line": 3,
                                  "message": "the page calls a host the manifest does not allow"}]})
    card_id = _draw(token, "sid-share", share).json()["card_id"]
    r = member.post(f"/v1/cards/{card_id}/act", json={"button": "go"})
    assert r.json()["outcome"]["status"] == st.OUTCOME_FAILED
    assert r.json()["outcome"]["text"] == ('The app "board" was not shared. '
                                           "The page calls a host the manifest does not allow.")
    assert not (config.get_agent_dir("target") / "workspace" / "apps" / "board").exists()
    # Removed through the files' own delete, so a machine holding the copy drops it too.
    assert ("target", "workspace/apps/board") in pushed_deletes
    assert actions_team._bundle_tasks("target", "board") == {}
    # A source app with a manifest to approve: the draw says its first release waits.
    rows[(AGENT, "", "planner")] = {"agent": AGENT, "username": "", "slug": "planner", "title": "Planner",
                                    "rel_path": "workspace/apps/planner", "kind": "folder",
                                    "actions": '[{"id": "run", "label": "Run", "type": "fire_task"}]'}
    r = _draw(token, "sid-share", _do("share_app", {"app_slug": "planner", "target_agent": "target"}))
    assert "Its first release waits for approval there." in db_cards.get_card(r.json()["card_id"])["text"]
    # A taken slug on the target, a missing app, a viewer on the target, the same agent.
    rows[("target", "", "board")] = {"agent": "target", "username": "", "slug": "board", "kind": "folder", "rel_path": "x"}
    r = _draw(token, "sid-share", _do("share_app", {"app_slug": "board", "target_agent": "target"}))
    assert r.status_code == 409 and r.json()["detail"] == 'An app named "board" is already on Target.'
    r = _draw(token, "sid-share", _do("share_app", {"app_slug": "nope", "target_agent": "target"}))
    assert r.status_code == 409 and r.json()["detail"] == 'There is no app named "nope" on PA.'
    _person(MEMBER, "viewer", {AGENT: roles.EDITOR, "target": roles.VIEWER})
    r = _draw(token, "sid-share", _do("share_app", {"app_slug": "board", "target_agent": "target"}))
    assert r.status_code == 409 and r.json()["detail"] == "Only editors and managers add shared apps to Target."
    token_t = _own_session("sid-share-t", MEMBER, "chat-share-t", agent="target")
    r = _draw(token_t, "sid-share-t", _do("share_app", {"app_slug": "board", "target_agent": "target"}))
    assert r.status_code == 409 and r.json()["detail"] == actions_team.SAME_AGENT
    _person(MEMBER, "viewer", {AGENT: roles.VIEWER, "target": roles.EDITOR})
    r = _draw(token, "sid-share", _do("share_app", {"app_slug": "board", "target_agent": "target"}))
    assert r.status_code == 409 and r.json()["detail"] == "Only editors and managers share the team's apps of PA."

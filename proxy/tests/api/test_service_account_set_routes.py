"""The routes of an agent's own accounts of a tool as a set
(``api/mcp/service_account_set.py``): a manager adds one of their own
accounts, makes one the default, takes one out; every agent takes any
number, in every mode; another lender's label is refused in words; the
options route lists the set; a default that moves keeps every
subscription, and an account taken out takes its own; the one-account
route is gone; the Integrations page no longer takes a person's own account
for a Shared-only agent and names the account its chats use.

Real rows in the test database; the credentials router mounted on an app of
its own with the caller overridden; one plain per-person tool (Mail X)
registered for the test.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user
from services.mcp import mcp_manifest_parse, mcp_manifest_store, mcp_registry
from storage import database as task_store
from storage.agents import agent_store
from storage.identity import credential_store, service_account_set
from storage.pg import get_conn

MAIL = "svcset-mail"


def _person(name: str) -> str:
    sub = f"test-user-{uuid.uuid4().hex[:12]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, username, display_name, role, auth_provider, "
            "created_at, last_login) VALUES (%s, %s, %s, %s, %s, 'member', 'local', NOW()::text, "
            "NOW()::text)",
            (sub, f"{sub}@test.example", name, f"u{uuid.uuid4().hex[:8]}", name),
        )
        conn.commit()
    return sub


@pytest.fixture
def rig(monkeypatch, tmp_path):
    folder = tmp_path / MAIL
    folder.mkdir()
    (folder / "manifest.json").write_text(json.dumps({
        "name": MAIL, "version": "1.0.0", "description": "Mail tools.", "label": "Mail X",
        "server": {"runtime": "python", "transport": "stdio", "command": "venv/bin/python",
                   "args": ["server.py"]},
        "credentials": {"type": "per_user", "label": "Mail X", "service_account": True,
                        "fields": [{"key": "MAILX_PASS", "label": "Password"}]},
    }))
    manifest = mcp_manifest_parse._parse_manifest(folder / "manifest.json")
    monkeypatch.setitem(mcp_manifest_store._manifests, MAIL, manifest)
    monkeypatch.setattr(mcp_registry, "get_visible_mcps_for_agent", lambda agent: [manifest])
    monkeypatch.setattr(mcp_registry, "get_agent_mcps_all_placements", lambda agent, **kw: [manifest])
    pool, team = f"svcset-pool-{uuid.uuid4().hex[:6]}", f"svcset-team-{uuid.uuid4().hex[:6]}"
    agent_store.create_agent(pool, "Pool")
    agent_store.update_agent(pool, collaborative=False, default_scope="agent")
    agent_store.create_agent(team, "Team")
    ann, bob, eve = _person("Ann"), _person("Bob"), _person("Eve")
    for sub, role in ((ann, "manager"), (bob, "manager"), (eve, "editor")):
        for agent in (pool, team):
            task_store.add_user_agent(sub, agent, role, "test")
    for sub, labels in ((ann, ("work", "home")), (bob, ("bob@x.example", "work")), (eve, ("eve",))):
        for label in labels:
            credential_store.set_user_credentials(sub, MAIL, {"MAILX_PASS": label}, account_label=label)
            credential_store.set_account_display_email(sub, MAIL, label, f"{label}@mail.example"
                                                       if "@" not in label else label)
    caller = {"sub": ann}
    roles = {ann: "manager", bob: "manager", eve: "editor"}

    async def _stub():
        sub = caller["sub"]
        return UserContext(sub=sub, email=f"{sub}@test.example", name="T", role="member",
                           agents=[pool, team], agent_roles={pool: roles[sub], team: roles[sub]})

    from api.mcp import credentials as credentials_api
    app = FastAPI()
    app.include_router(credentials_api.router)
    app.dependency_overrides[get_current_user] = _stub
    yield SimpleNamespace(client=TestClient(app), pool=pool, team=team, ann=ann, bob=bob, eve=eve,
                          caller=caller)
    credential_store.delete_all_mcp_credentials(MAIL)
    with get_conn() as conn:
        for sub in (ann, bob, eve):
            conn.execute("DELETE FROM users WHERE sub=%s", (sub,))
        conn.commit()
    for agent in (pool, team):
        agent_store.delete_agent(agent)


def _base(agent: str) -> str:
    return f"/v1/agents/{agent}/mcps/{MAIL}"


def _add(rig, label, agent=None, **body):
    return rig.client.put(f"{_base(agent or rig.pool)}/service-accounts",
                          json={"account_label": label, **body})


def _set(rig, agent=None):
    return [(b.account_label, b.account_owner_sub, b.is_default) for b in
            service_account_set.list_service_agent_set(MAIL, agent or rig.pool, standing_only=False)]


def test_a_manager_sets_a_shared_only_agents_accounts_and_its_default(rig):
    resp = _add(rig, "work")
    assert resp.status_code == 200, resp.text
    assert _add(rig, "home").status_code == 200
    rig.caller["sub"] = rig.bob
    resp = _add(rig, "bob@x.example", make_default=True)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "set_allowed" not in body and "subscriptions_removed" not in body
    assert [(a["label"], a["owner_name"], a["is_default"], a["mine"], a["display_email"])
            for a in body["accounts"]] == [
        ("bob@x.example", "Bob", True, True, "bob@x.example"),
        ("work", "Ann", False, False, "work@mail.example"),
        ("home", "Ann", False, False, "home@mail.example")]
    resp = rig.client.put(f"{_base(rig.pool)}/service-accounts/default", json={"account_label": "home"})
    assert resp.status_code == 200 and _set(rig)[0] == ("home", rig.ann, True)
    options = rig.client.get(f"{_base(rig.pool)}/service-account-options").json()
    assert "current_binding" not in options
    assert [a["label"] for a in options["accounts"]] == ["home", "work", "bob@x.example"]
    assert options["accounts"][0] == {**options["accounts"][0], "installation_id": "", "repositories": [],
                                      "read_only": False, "problem": ""}
    assert all(a["stands"] for a in options["accounts"])


def test_what_the_set_cannot_hold_is_refused_in_words(rig):
    assert _add(rig, "work").status_code == 200
    rig.caller["sub"] = rig.bob
    resp = _add(rig, "work")
    assert resp.status_code == 409
    assert resp.json()["detail"] == ("This agent already has an account named work for Mail X. "
                                     "Connect yours under another name in Settings, Integrations, "
                                     "then add it.")
    assert _add(rig, "ghost").status_code == 404
    # An account the set holds keeps its reach: the add names none for it.
    rig.caller["sub"] = rig.ann
    resp = _add(rig, "work", read_only=True)
    assert (resp.status_code, resp.json()["detail"]) == (
        409, "This account is already one of this agent's. Change its repositories under it.")


def test_every_agent_with_a_space_of_its_own_takes_several_accounts(rig):
    """No mode caps the set: a collaborative agent takes two lenders'
    accounts as a Shared-only one does; a run with no person takes the
    default."""
    from services.oauth import credential_resolver
    rig.caller["sub"] = rig.ann
    assert _add(rig, "work", agent=rig.team).status_code == 200
    rig.caller["sub"] = rig.bob
    assert _add(rig, "bob@x.example", agent=rig.team, make_default=True).status_code == 200
    assert _set(rig, rig.team) == [("bob@x.example", rig.bob, True), ("work", rig.ann, False)]
    assert credential_resolver.pick_account(MAIL, rig.team).label == "bob@x.example"


PERSONAL_ONLY = "This agent is Personal only. It has no accounts of its own."


def test_a_personal_only_agent_has_no_accounts_of_its_own(rig):
    """Every read and write of the set is refused on a Personal-only agent,
    in one sentence, before anything is read or written."""
    agent_store.update_agent(rig.team, collaborative=False, default_scope="user")
    base = _base(rig.team)
    for resp in (
        rig.client.get(f"{base}/service-account-options"),
        _add(rig, "work", agent=rig.team),
        rig.client.put(f"{base}/service-accounts/reach", json={"account_label": "work", "installation_id": ""}),
        rig.client.put(f"{base}/service-accounts/default", json={"account_label": "work"}),
        rig.client.delete(f"{base}/service-accounts", params={"account_label": "work"}),
        rig.client.get(f"{base}/service-account-repositories", params={"account_label": "work"}),
    ):
        assert (resp.status_code, resp.json()["detail"]) == (409, PERSONAL_ONLY)
    assert _set(rig, rig.team) == []


def test_turning_personal_only_takes_the_accounts_out_through_the_remover(rig, monkeypatch):
    """The mode change empties the agent's own sets through the one remover:
    each account's subscriptions go first, through its lender, then the
    rows; a chat that chose one is told. Another agent's set is untouched."""
    from api.agents import agents as agents_api
    from services.webhooks import subscription_manager
    for label in ("work", "home"):
        assert _add(rig, label, agent=rig.team).status_code == 200
        assert _add(rig, label).status_code == 200
    cleaned: list = []

    async def _cleanup(**kw):
        cleaned.append((kw["agent"], kw["account_label"], kw["token_owner_sub"]))
        return 0

    monkeypatch.setattr(subscription_manager, "cleanup_account_subscriptions", _cleanup)
    row = agent_store.update_agent(rig.team, collaborative=False, default_scope="user")
    asyncio.run(agents_api._after_update(rig.team, {"collaborative": False, "default_scope": "user"}, row))
    assert _set(rig, rig.team) == []
    assert sorted(cleaned) == [(rig.team, "home", rig.ann), (rig.team, "work", rig.ann)]
    assert [label for label, _o, _d in _set(rig)] == ["work", "home"]


def test_the_boot_pass_empties_a_personal_only_agents_sets_once(rig, monkeypatch):
    """A released install that has a Personal-only agent with an account:
    the pass after the start takes it out, says so in the log, and finds
    nothing the second time; a collaborative agent keeps its own."""
    import logging

    from services.agents import offboarding_bindings
    from services.webhooks import subscription_manager

    async def _cleanup(**kw):
        return 0

    monkeypatch.setattr(subscription_manager, "cleanup_account_subscriptions", _cleanup)
    assert _add(rig, "work", agent=rig.team).status_code == 200
    assert _add(rig, "work").status_code == 200
    agent_store.update_agent(rig.team, collaborative=False, default_scope="user")
    records: list = []
    handler = logging.Handler()
    handler.emit = records.append
    logging.getLogger("otodock").addHandler(handler)
    try:
        assert asyncio.run(offboarding_bindings.clear_personal_only()) == 1
        assert asyncio.run(offboarding_bindings.clear_personal_only()) == 0
    finally:
        logging.getLogger("otodock").removeHandler(handler)
    assert _set(rig, rig.team) == [] and [label for label, _o, _d in _set(rig)] == ["work"]
    assert [r.getMessage() for r in records if "Personal only" in r.getMessage()] == [
        f"service accounts: {rig.team} is Personal only, its own {MAIL} accounts were taken out"]


def test_a_personal_only_agents_call_runs_with_no_account_of_its_own(rig, monkeypatch):
    """A row left from another mode serves nothing: a run with no person (a
    call on a caller route, delegated work) gets no account of the agent's,
    the tool is left out with a reason, and nothing raises."""
    from services.oauth import credential_resolver
    from storage.pg import get_conn
    manifest = mcp_registry.get_manifest(MAIL)
    monkeypatch.setattr(mcp_registry, "get_agent_mcps_all_placements", lambda agent: [manifest])
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO service_agent_bindings (mcp_name, agent_name, account_label, account_owner_sub, "
            "set_by, set_at, is_default) VALUES (%s, %s, 'work', %s, %s, 'now', TRUE)",
            (MAIL, rig.team, rig.ann, rig.ann))
        conn.commit()
    assert credential_resolver.pick_account(MAIL, rig.team) is not None
    agent_store.update_agent(rig.team, collaborative=False, default_scope="user")
    assert credential_resolver.pick_account(MAIL, rig.team) is None
    resolved = credential_resolver.resolve_credentials(rig.team, None, task_scope="agent")
    assert MAIL in resolved.excluded_mcps
    # The session is never told to have a manager bind an account: the
    # agent's own set refuses every write on a Personal-only agent.
    assert resolved.exclusion_reasons[MAIL] == f"Mail X needs an account. {PERSONAL_ONLY}"


def test_the_one_account_route_is_gone(rig):
    assert _add(rig, "work").status_code == 200
    assert rig.client.put(f"{_base(rig.pool)}/service-binding",
                          json={"account_label": "home"}).status_code in (404, 405)
    assert rig.client.delete(f"{_base(rig.pool)}/service-binding").status_code in (404, 405)
    assert _set(rig) == [("work", rig.ann, True)]


def test_only_a_manager_writes_the_set(rig):
    assert _add(rig, "work").status_code == 200
    rig.caller["sub"] = rig.eve
    assert _add(rig, "eve").status_code == 403
    assert rig.client.put(f"{_base(rig.pool)}/service-accounts/default",
                          json={"account_label": "work"}).status_code == 403
    assert rig.client.delete(f"{_base(rig.pool)}/service-accounts",
                             params={"account_label": "work"}).status_code == 403
    assert _set(rig) == [("work", rig.ann, True)]


def test_any_manager_takes_an_account_out_and_its_chats_are_closed(rig, monkeypatch):
    from core.session import visibility
    from services.remote import chat_reload
    for label in ("work", "home"):
        assert _add(rig, label).status_code == 200
    chat = str(uuid.uuid4())
    task_store.create_chat(chat, visibility.shared_chat_owner(rig.pool), rig.pool)
    assert service_account_set.set_chat_service_choice(chat, rig.pool, MAIL, "home")
    left: list = []

    async def _left(mcp_name, agent_name, leaving):
        left.append((mcp_name, list(leaving.chose + leaving.on_default)))
        return set()

    monkeypatch.setattr(chat_reload, "account_left", _left)
    rig.caller["sub"] = rig.bob
    resp = rig.client.delete(f"{_base(rig.pool)}/service-accounts", params={"account_label": "home"})
    assert resp.status_code == 200, resp.text
    assert left == [(MAIL, [chat])] and _set(rig) == [("work", rig.ann, True)]
    assert rig.client.delete(f"{_base(rig.pool)}/service-accounts",
                             params={"account_label": "home"}).status_code == 404
    # The default out: the oldest left takes its place.
    assert _add(rig, "bob@x.example").status_code == 200
    assert rig.client.delete(f"{_base(rig.pool)}/service-accounts",
                             params={"account_label": "work"}).status_code == 200
    assert _set(rig) == [("bob@x.example", rig.bob, True)]
    task_store.delete_chat(chat)


def test_a_default_that_moves_keeps_every_subscription_and_a_removal_takes_its_own(rig, monkeypatch):
    """Each account keeps its own subscriptions: moving the default drops
    none; taking an account out drops that account's alone, through its
    lender's own token, a second account's included."""
    from services.webhooks import subscription_manager
    for label in ("work", "home"):
        assert _add(rig, label).status_code == 200
    rig.caller["sub"] = rig.bob
    assert _add(rig, "bob@x.example").status_code == 200
    cleaned: list = []

    async def _cleanup(**kw):
        cleaned.append(kw)
        return 2

    monkeypatch.setattr(subscription_manager, "cleanup_account_subscriptions", _cleanup)
    for label in ("home", "work", "home"):
        resp = rig.client.put(f"{_base(rig.pool)}/service-accounts/default", json={"account_label": label})
        assert resp.status_code == 200 and "subscriptions_removed" not in resp.json()
    assert cleaned == []
    rig.caller["sub"] = rig.ann
    assert rig.client.delete(f"{_base(rig.pool)}/service-accounts",
                             params={"account_label": "bob@x.example"}).status_code == 200
    assert cleaned == [{"scope": "service", "owner": rig.bob, "mcp_name": MAIL,
                        "account_label": "bob@x.example", "agent": rig.pool, "token_owner_sub": rig.bob}]


def test_the_integrations_page_names_the_account_a_shared_only_agents_chats_use(rig):
    assert _add(rig, "work").status_code == 200
    rig.caller["sub"] = rig.eve
    [card] = [c for c in rig.client.get("/v1/users/me/integrations").json() if c["mcp_name"] == MAIL]
    assert rig.pool in card["agents_on_own_accounts"] and rig.team not in card["agents_on_own_accounts"]
    assert card["agent_accounts"] == {rig.pool: "work@mail.example"}
    resp = rig.client.put(f"/v1/users/me/integrations/{MAIL}/agent-accounts",
                          json={"agent_name": rig.pool, "account_label": "eve"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == ("This agent's chats use its own accounts. A manager sets them "
                                     "in Agent Settings, MCPs.")
    resp = rig.client.put(f"/v1/users/me/integrations/{MAIL}/agent-binding",
                          json={"agent_name": rig.pool, "account_label": "eve"})
    assert resp.status_code == 409
    # Another agent still takes the person's own account.
    resp = rig.client.put(f"/v1/users/me/integrations/{MAIL}/agent-accounts",
                          json={"agent_name": rig.team, "account_label": "eve"})
    assert resp.status_code == 200, resp.text
    # A row ticked for a Shared-only agent before stays clearable.
    assert credential_store.add_account_to_agent(rig.eve, MAIL, rig.pool, "eve")
    assert rig.client.delete(f"/v1/users/me/integrations/{MAIL}/agent-accounts/{rig.pool}",
                             params={"account_label": "eve"}).status_code == 200


def test_a_tool_that_runs_only_on_a_remote_machine_is_on_the_integrations_page(rig, monkeypatch, tmp_path):
    """An app connector loads only in a session on a remote machine whose owner
    granted it, yet a person sets their account for it here, with no session
    in view: the page reads the agent's tools at every placement. One with
    no account of its own (an executable path a person could override) gives
    a person nothing to set, so it has no card."""
    def _device(name, credentials):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "manifest.json").write_text(json.dumps({
            "name": name, "version": "1.0.0", "description": "A desktop app.", "label": name,
            "server": {"runtime": "node", "transport": "stdio", "command": "node", "args": ["index.js"]},
            "placement": "satellite_only", "requires_display": True, "device_capability": "app",
            "credentials": credentials,
            "config": [{"key": "APP_URL", "label": "Address", "input_type": "text",
                        "default": "ws://127.0.0.1:4455", "user_overridable": True}],
        }))
        manifest = mcp_manifest_parse._parse_manifest(folder / "manifest.json")
        monkeypatch.setitem(mcp_manifest_store._manifests, name, manifest)
        return manifest

    name = "svcset-app"
    app = _device(name, {"type": "per_user", "label": "App X", "fields": [
        {"key": "APPX_PASSWORD", "label": "Password", "input_type": "password"},
        {"key": "APP_URL", "label": "Address", "input_type": "text", "required": False}]})
    pathless = _device("svcset-pathless", {"type": "none"})
    monkeypatch.setattr(mcp_manifest_store, "_agent_base_manifests", lambda agent: [app, pathless])
    monkeypatch.setattr(mcp_registry, "get_agent_mcps_all_placements",
                        mcp_manifest_store.get_agent_mcps_all_placements)
    assert mcp_registry.get_agent_mcps(rig.team) == []

    cards = rig.client.get("/v1/users/me/integrations").json()
    assert "svcset-pathless" not in [c["mcp_name"] for c in cards]
    [card] = [c for c in cards if c["mcp_name"] == name]
    assert [f["key"] for f in card["fields"]] == ["APPX_PASSWORD", "APP_URL"]
    assert rig.team in card["candidate_agents"]
    resp = rig.client.put(f"/v1/users/me/integrations/{name}", json={"credentials": {"APPX_PASSWORD": "pw"}})
    assert resp.status_code == 200, resp.text
    resp = rig.client.put(f"/v1/users/me/integrations/{name}/agent-binding",
                          json={"agent_name": rig.team, "account_label": "default"})
    assert resp.status_code == 200, resp.text
    credential_store.delete_all_mcp_credentials(name)


def _notes(chat_id: str) -> list[str]:
    return [json.loads(m["event_data"]).get("message", "") for m in task_store.get_chat_messages(chat_id)
            if m.get("event_type") == "system"]


def _pool_chat(rig, *, session_id: str = "") -> str:
    from core.session import visibility
    chat = str(uuid.uuid4())
    task_store.create_chat(chat, visibility.shared_chat_owner(rig.pool), rig.pool)
    if session_id:
        task_store.update_chat(chat, session_id=session_id)
    return chat


def test_a_lender_who_deletes_a_plain_account_takes_it_out_of_the_set(rig):
    """The delete of a plain account is a removal like the OAuth disconnect:
    the row goes, the chat that chose it is told, and the next account takes
    the default, so the tool does not leave every chat while others stand."""
    for label in ("work", "home"):
        assert _add(rig, label).status_code == 200
    rig.caller["sub"] = rig.bob
    assert _add(rig, "bob@x.example").status_code == 200
    rig.caller["sub"] = rig.ann
    chat = _pool_chat(rig, session_id=str(uuid.uuid4()))
    assert service_account_set.set_chat_service_choice(chat, rig.pool, MAIL, "home")

    resp = rig.client.delete(f"/v1/users/me/integrations/{MAIL}", params={"account_label": "home"})
    assert resp.status_code == 200, resp.text
    assert [label for label, _o, _d in _set(rig)] == ["work", "bob@x.example"]
    assert service_account_set.get_chat_service_choices(chat) == {}
    assert [n for n in _notes(chat) if "was removed from the agent" in n]

    # The default too: the next account takes it, and it is the agent's now.
    resp = rig.client.delete(f"/v1/users/me/integrations/{MAIL}", params={"account_label": "work"})
    assert resp.status_code == 200, resp.text
    assert _set(rig) == [("bob@x.example", rig.bob, True)]
    assert credential_store.get_service_agent_binding(MAIL, rig.pool).account_label == "bob@x.example"
    # The account is gone from the person's list as well.
    assert not [a for a in credential_store.list_user_accounts(rig.ann, MAIL)
                if a["account_label"] in ("home", "work")]
    # Another lender's account of the same label is not theirs to take.
    assert _add(rig, "home", agent=rig.team).status_code == 404
    task_store.delete_chat(chat)


def test_a_person_who_deletes_an_account_no_agent_uses_touches_no_set(rig):
    assert _add(rig, "work").status_code == 200
    resp = rig.client.delete(f"/v1/users/me/integrations/{MAIL}", params={"account_label": "home"})
    assert resp.status_code == 200, resp.text
    assert _set(rig) == [("work", rig.ann, True)]


def test_making_another_account_the_default_tells_the_chats_on_the_old_one(rig):
    """The manager asked for it, the people in those chats did not: a chat
    with no choice is told, a chat with its own choice is not."""
    for label in ("work", "home"):
        assert _add(rig, label).status_code == 200
    on_default = _pool_chat(rig, session_id=str(uuid.uuid4()))
    chose = _pool_chat(rig, session_id=str(uuid.uuid4()))
    assert service_account_set.set_chat_service_choice(chose, rig.pool, MAIL, "work")
    assert rig.client.put(f"{_base(rig.pool)}/service-accounts/default",
                          json={"account_label": "work"}).status_code == 200
    assert _notes(on_default) == [] and _notes(chose) == []
    assert rig.client.put(f"{_base(rig.pool)}/service-accounts/default",
                          json={"account_label": "home"}).status_code == 200
    assert _notes(on_default) == [
        "The default account for Mail X is now home (home@mail.example). This chat moves to it "
        "at its next start."]
    assert _notes(chose) == []
    for chat in (on_default, chose):
        task_store.delete_chat(chat)


def test_removing_the_default_tells_the_chats_on_it(rig):
    """The default that goes is a removal: a chat on it with no live session
    is told the account it moves to, here another person's mailbox, never
    moved with no word."""
    assert _add(rig, "work").status_code == 200
    on_default = _pool_chat(rig, session_id=str(uuid.uuid4()))
    rig.caller["sub"] = rig.bob
    assert _add(rig, "bob@x.example").status_code == 200
    resp = rig.client.delete(f"{_base(rig.pool)}/service-accounts", params={"account_label": "work"})
    assert resp.status_code == 200, resp.text
    assert _notes(on_default) == [
        "The default account for Mail X is now bob@x.example. "
        "This chat moves to it at its next start."]
    task_store.delete_chat(on_default)


def test_an_account_whose_owner_no_longer_manages_the_agent_cannot_be_the_default(rig):
    """It would take the tool from every chat on the default while others
    stand. The page reads whether each row stands from the same place."""
    assert _add(rig, "work").status_code == 200
    rig.caller["sub"] = rig.bob
    assert _add(rig, "bob@x.example").status_code == 200
    rig.caller["sub"] = rig.ann
    task_store.set_user_agent_role(rig.bob, rig.pool, "editor")
    resp = rig.client.put(f"{_base(rig.pool)}/service-accounts/default",
                          json={"account_label": "bob@x.example"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == ("This account's owner no longer manages this agent, so it "
                                     "cannot be the default.")
    assert _set(rig)[0] == ("work", rig.ann, True)
    options = rig.client.get(f"{_base(rig.pool)}/service-account-options").json()
    assert [(a["label"], a["stands"], a["mine"]) for a in options["accounts"]] == [
        ("work", True, True), ("bob@x.example", False, False)]


def test_a_refusal_names_the_tool_as_a_person_knows_it(rig):
    resp = _add(rig, "ghost")
    assert (resp.status_code, resp.json()["detail"]) == (
        404, "Account 'ghost' not found among your connected Mail X accounts.")
    resp = rig.client.put(f"{_base(rig.pool)}/service-accounts/default", json={"account_label": "ghost"})
    assert (resp.status_code, resp.json()["detail"]) == (
        404, "Account 'ghost' is not one of this agent's Mail X accounts.")
    resp = rig.client.delete(f"{_base(rig.pool)}/service-accounts", params={"account_label": "ghost"})
    assert (resp.status_code, resp.json()["detail"]) == (
        404, "Account 'ghost' is not one of this agent's Mail X accounts.")

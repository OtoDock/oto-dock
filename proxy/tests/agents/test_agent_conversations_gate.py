"""The Conversations tab: outside people's conversations and the agent's own
line, open to the agent's editors and managers (S-F12), never to a viewer, a
session token or a key; a 1.9.1 call on a route tied to a person (the
``person`` line) stays the managers'.

Run: cd proxy && python -m pytest tests/agents/test_agent_conversations_gate.py -v
"""

import sys

import pytest
from fastapi import HTTPException

from tests._paths import PROXY_DIR

_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from api.agents import agents, conversations
from auth.providers import UserContext
from core.session import visibility


def _user(sub, agent, agent_role, platform_role="member"):
    return UserContext(
        sub=sub, email=f"{sub}@t.com", name=sub, role=platform_role,
        agents=[agent], agent_roles={agent: agent_role},
    )


def _fake_store(monkeypatch, rows=({"id": "c1"},)):
    from storage.chat import conversation_store
    asked = {}

    def _list(agent, **kw):
        asked.update(kw)
        return list(rows)
    monkeypatch.setattr(conversation_store, "list_for_agent", _list)
    monkeypatch.setattr(conversation_store, "count_for_agent", lambda agent, **kw: len(rows))
    return asked


@pytest.mark.asyncio
async def test_a_viewer_reads_no_conversation(monkeypatch):
    _fake_store(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await conversations.list_agent_conversations("acme", user=_user("u", "acme", "viewer"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_an_editor_reads_them_but_a_persons_old_calls(monkeypatch):
    from storage.chat import conversation_store
    asked = _fake_store(monkeypatch)
    res = await conversations.list_agent_conversations("acme", user=_user("u-ed", "acme", "editor"))
    assert res["total"] == 1
    assert tuple(asked["lines"]) == conversation_store.EDITOR_LINES
    assert conversation_store.LINE_PERSON not in asked["lines"]


@pytest.mark.asyncio
async def test_a_manager_and_an_admin_read_every_line(monkeypatch):
    from storage.chat import conversation_store
    asked = _fake_store(monkeypatch)
    await conversations.list_agent_conversations("acme", user=_user("u-mgr", "acme", "manager"))
    assert tuple(asked["lines"]) == conversation_store.LINES
    admin = UserContext(sub="a", email="a@t.com", name="a", role="admin")
    assert (await conversations.list_agent_conversations("acme", user=admin))["total"] == 1


@pytest.mark.asyncio
async def test_a_session_token_and_a_key_list_no_conversation(monkeypatch):
    """A task run's or a call's session token names no person, and a
    person's own session token or key reads no call either: the tab is a
    person's on the dashboard. The roster's manager check refuses the
    agent-scope session as it always did."""
    from api.agents import discovery
    from storage import database as task_store
    _fake_store(monkeypatch)
    monkeypatch.setattr(task_store, "get_agent_users_with_profile", lambda name: [])
    run = UserContext(sub="session:s-run", email="session@internal", name="Session Token",
                      role="agent", is_api_key=True, session_id="s-run", agent="acme")
    assert run.is_no_user_session and not run.can_manage_agent("acme")
    person_token = UserContext(sub="u-mgr", email="m@t.com", name="m", role="member",
                               is_api_key=True, session_id="s-chat", agent="acme",
                               agents=["acme"], agent_roles={"acme": "manager"})
    for u in (run, person_token):
        with pytest.raises(HTTPException) as exc:
            await conversations.list_agent_conversations("acme", user=u)
        assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await discovery.list_agent_users("acme", user=run)
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# GET /v1/chats/{id}/detail — the by-id rule the dashboard's resume shares
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_detail_follows_the_chat_owner_and_phone_calls_open_for_managers(temp_db):
    """On a Shared-only agent an assigned editor reads the ``agent::`` pool's
    detail, not a colleague's per-user chat from before the switch, and not
    a phone call; a manager reads the call. The agent's mode decides nothing
    by itself — the REST ``can_access_chat`` rule plus the phone clause."""
    import uuid

    from core.session import session_kind
    from core.session.visibility import PHONE_CHAT_OWNER, shared_chat_owner
    from storage import database as task_store
    from storage.agents import agent_store
    slug = "so-detail"
    agent_store.create_agent(slug, "SO", collaborative=False, default_scope="agent")
    call, pre_switch, pool = (str(uuid.uuid4()) for _ in range(3))
    task_store.create_chat(call, PHONE_CHAT_OWNER, slug, source_type=session_kind.PHONE.source_type)
    task_store.create_chat(pre_switch, "user-admin", slug)
    task_store.create_chat(pool, shared_chat_owner(slug), slug)
    editor = _user("u-ed", slug, "editor")
    manager = _user("u-mgr", slug, "manager")
    assert (await agents.get_chat_detail(pool, user=editor))["id"] == pool
    for cid in (call, pre_switch):
        with pytest.raises(HTTPException) as exc:
            await agents.get_chat_detail(cid, user=editor)
        assert exc.value.status_code == 403, cid
    assert (await agents.get_chat_detail(call, user=manager))["id"] == call
    with pytest.raises(HTTPException) as exc:
        await agents.get_chat_detail(pre_switch, user=manager)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_one_rule_opens_a_phone_call_to_the_agents_managers(temp_db):
    """``can_access_chat`` is the one by-id rule: the plain REST read and the
    apps audience open a phone call's conversation to the agent's managers
    (a long transcript pages for them), never to an editor, and never to a
    manager's session token or API key."""
    import uuid

    from api.agents import chats
    from core.session import session_kind
    from core.session.visibility import PHONE_CHAT_OWNER
    from services.apps import audience
    from storage import database as task_store
    from storage.agents import agent_store
    slug = "phone-read"
    agent_store.create_agent(slug, "Phone")
    call = str(uuid.uuid4())
    task_store.create_chat(call, PHONE_CHAT_OWNER, slug, source_type=session_kind.PHONE.source_type)
    chat = task_store.get_chat(call)
    manager = _user("u-mgr", slug, "manager")
    got = await chats.get_chat(call, before_id=None, limit=50, user=manager)
    assert (got.get("chat") or got)["id"] == call
    with pytest.raises(HTTPException) as exc:
        await chats.get_chat(call, before_id=None, limit=50, user=_user("u-ed", slug, "editor"))
    assert exc.value.status_code == 403
    bearer = UserContext(sub="u-mgr", email="m@t.com", name="m", role="member",
                         agents=[slug], agent_roles={slug: "manager"}, is_api_key=True)
    assert not chats.can_access_chat(bearer, chat)
    # The apps fan-out asks the same rule, with no principal.
    assert visibility.may_open_chat(chat, "u-mgr", "manager")
    assert not visibility.may_open_chat(chat, "u-ed", "editor")
    assert not hasattr(audience, "_may_open_chat")
    assert not hasattr(agents, "can_open_chat")


def _session(sub, agent, *, ext=""):
    return UserContext(sub=sub, email="", name="", role="member", is_api_key=True,
                       agent=agent, agents=[], agent_roles={}, external_claim=ext)


def test_one_rule_for_every_principal_kind(temp_db):
    """``may_open_chat`` behind ``can_access_chat`` for each kind of
    principal (ONE-DOOR #13): a no-user session opens its own agent's pool
    and agent-scope runs and never another agent's (audit B1); a person's
    session on its own agent with no row opens the pool's human chats as a
    viewer; an outside session opens none; a viewer never an agent-scope
    run; a user-scope run only its creator."""
    import uuid

    from storage import database as task_store
    from storage.agents import agent_store
    for slug in ("one-a", "one-b"):
        agent_store.create_agent(slug, slug, collaborative=False, default_scope="agent")
    pool = {"id": str(uuid.uuid4()), "user_sub": "agent::one-a", "agent": "one-a"}
    task_store.create_chat(pool["id"], pool["user_sub"], "one-a")
    worker = {"id": str(uuid.uuid4()), "user_sub": "agent::one-a", "agent": "one-a"}
    task_store.create_chat(worker["id"], worker["user_sub"], "one-a")
    task_store.create_run("run-w", "t", "one-a", "manual", None, "go")
    task_store.update_run("run-w", chat_id=worker["id"])
    mine = {"id": "task-run-u", "user_sub": "task::one-a", "agent": "one-a"}
    task_store.create_chat(mine["id"], mine["user_sub"], "one-a")
    task_store.create_run("run-u", "t", "one-a", "manual", None, "go", scope="user", created_by="u-view")
    task_store.update_run("run-u", chat_id=mine["id"])
    other_pool = {"id": str(uuid.uuid4()), "user_sub": "agent::one-b", "agent": "one-b"}
    task_store.create_chat(other_pool["id"], other_pool["user_sub"], "one-b")

    viewer = _user("u-view", "one-a", "viewer")
    editor = _user("u-ed", "one-a", "editor")
    service_a = _session("session:sa", "one-a")
    service_b = _session("session:sb", "one-b")
    person_no_row = _session("u-gone", "one-a")
    outside = _session("session:ext", "one-a", ext="phone:+302100000000")
    table = [
        (viewer, pool, True), (viewer, worker, False), (viewer, mine, True),
        (editor, pool, True), (editor, worker, True), (editor, mine, False),
        (service_a, pool, True), (service_a, worker, True), (service_a, mine, False),
        (service_b, pool, False), (service_b, worker, False), (service_b, other_pool, True),
        (person_no_row, pool, True), (person_no_row, worker, False),
        (outside, pool, False), (outside, worker, False), (outside, mine, False),
    ]
    for principal, chat, expected in table:
        assert visibility.can_access_chat(principal, task_store.get_chat(chat["id"])) is expected, \
            (principal.sub, chat["id"], expected)


@pytest.mark.asyncio
async def test_an_editor_opens_an_outside_conversation_not_a_persons_old_call(temp_db):
    """The by-id rule follows the record: an outside person's conversation
    and the agent's own line open to an editor, a 1.9.1 call on a route tied
    to a person and a conversation with no record stay the managers'; a call
    on a route tied to a person is that person's own chat."""
    import uuid

    from core.session import session_kind
    from core.session.visibility import PHONE_CHAT_OWNER
    from storage import database as task_store
    from storage.agents import agent_store
    from storage.chat import conversation_store
    slug = "conv-read"
    agent_store.create_agent(slug, "Conversations")
    phone = session_kind.PHONE.source_type
    outside, agent_line, old_person, bare, own = (str(uuid.uuid4()) for _ in range(5))
    for cid in (outside, agent_line, old_person, bare):
        task_store.create_chat(cid, PHONE_CHAT_OWNER, slug, source_type=phone)
    task_store.create_chat(own, "u-ed", slug, source_type=phone)
    conversation_store.create(outside, slug, line=conversation_store.LINE_CALLER, direction="inbound")
    conversation_store.create(agent_line, slug, line=conversation_store.LINE_AGENT, direction="inbound")
    conversation_store.create(old_person, slug, line=conversation_store.LINE_PERSON, direction="inbound")
    editor = _user("u-ed", slug, "editor")
    manager = _user("u-mgr", slug, "manager")
    for cid in (outside, agent_line, own):
        assert (await agents.get_chat_detail(cid, user=editor))["id"] == cid
    for cid in (old_person, bare):
        with pytest.raises(HTTPException) as exc:
            await agents.get_chat_detail(cid, user=editor)
        assert exc.value.status_code == 403, cid
        assert (await agents.get_chat_detail(cid, user=manager))["id"] == cid
    with pytest.raises(HTTPException):
        await agents.get_chat_detail(own, user=manager)

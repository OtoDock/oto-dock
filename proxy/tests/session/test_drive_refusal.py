"""``visibility.drive_refusal``: the one rule every door that drives a chat
asks (the socket, a key's turn, the voice doors, the move, a card's press,
the continue pass, the delegation's ``continue_id``, the engine switch).

The doors' own answers are pinned beside their code (``test_public_api``'s
doors-as-one test, the voice tests in ``tests/audio/test_duplex_session.py``);
here the rule itself, over the chats it decides, for each kind of principal.
"""

from __future__ import annotations

import asyncio

import pytest

from auth import roles
from auth.providers import SESSION_SUB_PREFIX, UserContext
from core.session import session_kind, visibility as vis
from services.scheduler import task_kinds
from storage import database as task_store

AGENT = "agent-dr"
MANAGER, EDITOR, VIEWER = "user-dr-manager", "user-dr-editor", "user-dr-viewer"


@pytest.fixture
def world(temp_db):
    from storage.agents import agent_store
    agent_store.create_agent(AGENT, "Drive", collaborative=True, default_scope="user")
    for sub, role in ((MANAGER, roles.MANAGER), (EDITOR, roles.EDITOR), (VIEWER, roles.VIEWER)):
        task_store.upsert_user(sub, f"{sub}@test.com", sub, "member")
        task_store.add_user_agent(sub, AGENT, role, "test")
    return AGENT


def _chat(cid: str, owner: str, *, source_type: str = "chat") -> dict:
    task_store.create_chat(cid, owner, AGENT, "auto", source_type=source_type)
    return task_store.get_chat(cid)


def _run(run_id: str, chat_id: str, *, kind: str = "delegate", by: str = MANAGER,
         scope: str = "agent") -> None:
    task_store.create_run(run_id, "dyn-dr", AGENT, kind, None, "go", kind, scope, by)
    task_store.update_run(run_id, status="completed", chat_id=chat_id)


def _person(sub: str, role: str) -> UserContext:
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="member",
                       agents=[AGENT], agent_roles={AGENT: role})


def _outside() -> UserContext:
    return UserContext(sub=f"{SESSION_SUB_PREFIX}sid-caller", email="session@internal",
                       name="Session Token", role=roles.SERVICE, agents=[], is_api_key=True,
                       session_id="sid-caller", agent=AGENT, external_claim="phone:+15550001",
                       external_channel="phone", external_id="+15550001")


def _no_person(session_id: str = "sid-task") -> UserContext:
    return UserContext(sub=f"{SESSION_SUB_PREFIX}{session_id}", email="session@internal",
                       name="Session Token", role=roles.SERVICE, agents=[], is_api_key=True,
                       session_id=session_id, agent=AGENT)


def _master_key() -> UserContext:
    return UserContext(sub="api-key", email="api@internal", name="API Key", role=roles.ADMIN,
                       agents=[], is_api_key=True)


def test_an_outside_principal_drives_no_chat(world):
    own = _chat("c-own", MANAGER)
    pool = _chat("c-pool", vis.shared_chat_owner(AGENT))
    for chat in (own, pool):
        assert vis.drive_refusal_for(_outside(), chat) == vis.OUTSIDE_DRIVE_REFUSED
        assert vis.drive_refusal(chat, "", roles.EXTERNAL) == vis.OUTSIDE_DRIVE_REFUSED


@pytest.fixture
def calls():
    """The chats a call writes in, empty around each test."""
    from services.phone import call_chats
    call_chats._held.clear()
    yield call_chats
    call_chats._held.clear()


def test_the_agents_conversation_is_the_calls_for_good(world, calls):
    """The agent's conversation is read-only during its call and after it,
    for an admin and the agent's manager too."""
    call = _chat("c-call", vis.PHONE_CHAT_OWNER, source_type=session_kind.PHONE.source_type)
    calls.hold("c-call", "sid-call")
    for _round in ("during the call", "after it"):
        assert vis.drive_refusal(call, MANAGER, roles.MANAGER) == vis.PHONE_CHAT_READ_ONLY, _round
        assert vis.drive_refusal(call, "user-admin", roles.ADMIN, admin=True) == vis.PHONE_CHAT_READ_ONLY
        calls.release("c-call", "sid-call")


def test_a_persons_own_call_is_the_calls_while_it_runs_then_theirs(world, calls):
    """A call on a route tied to a person writes in their own chat: nobody
    else writes there while it runs, an admin included. When it ends the
    chat is its owner's again, decided as for any chat of theirs."""
    mine = _chat("c-persons-call", MANAGER, source_type=session_kind.PHONE.source_type)
    calls.hold("c-persons-call", "sid-pc")
    assert vis.drive_refusal(mine, MANAGER, roles.MANAGER) == vis.CALL_IN_PROGRESS
    assert vis.drive_refusal(mine, "user-admin", roles.ADMIN, admin=True) == vis.CALL_IN_PROGRESS
    assert vis.drive_refusal_for(_master_key(), mine) == vis.CALL_IN_PROGRESS
    calls.release("c-persons-call", "sid-pc")
    assert vis.drive_refusal(mine, MANAGER, roles.MANAGER) == ""
    assert vis.drive_refusal(mine, "user-admin", roles.ADMIN, admin=True) == ""
    # Reading is not driving: another person of the agent, an outside
    # principal and a session with no person never write in it.
    assert vis.drive_refusal(mine, EDITOR, roles.EDITOR) == vis.TASK_CONTINUE_REFUSED
    assert vis.drive_refusal_for(_outside(), mine) == vis.OUTSIDE_DRIVE_REFUSED
    assert vis.drive_refusal_for(_no_person(), mine) == vis.TASK_CONTINUE_REFUSED


def test_a_shared_chat_a_call_wrote_takes_the_editor_floor_after_it(world, calls):
    """On a Shared-only agent the call writes in the agent's shared chats:
    after it every member reads it, and an editor or above writes in it, as
    in any shared chat."""
    pool = _chat("c-pool-call", vis.shared_chat_owner(AGENT), source_type=session_kind.PHONE.source_type)
    calls.hold("c-pool-call", "sid-pool")
    for sub, role in ((MANAGER, roles.MANAGER), (EDITOR, roles.EDITOR), (VIEWER, roles.VIEWER)):
        assert vis.drive_refusal(pool, sub, role) == vis.CALL_IN_PROGRESS, sub
    calls.release("c-pool-call", "sid-pool")
    assert vis.drive_refusal(pool, EDITOR, roles.EDITOR) == ""
    assert "editor role" in vis.drive_refusal(pool, VIEWER, roles.VIEWER)


def test_a_chat_runs_as_a_chat_unless_a_call_holds_it_or_it_is_the_agents_conversation(world, calls):
    """``runs_as_chat``: what a wake and the other sites build as a person's
    chat. A dashboard chat, and a chat a call wrote for a person once no call
    holds it. Never the agent's conversation, a task run's chat, or a chat a
    call holds."""
    phone = session_kind.PHONE.source_type
    mine = _chat("c-rac-mine", EDITOR, source_type=phone)
    pool = _chat("c-rac-pool", vis.shared_chat_owner(AGENT), source_type=phone)
    conversation = _chat("c-rac-conv", vis.PHONE_CHAT_OWNER, source_type=phone)
    plain = _chat("c-rac-plain", EDITOR)
    task = _chat(session_kind.task_chat_id("run-rac"), vis.task_chat_owner(AGENT), source_type="task")
    assert [vis.runs_as_chat(c) for c in (mine, pool, plain)] == [True, True, True]
    assert [vis.runs_as_chat(c) for c in (conversation, task)] == [False, False]
    calls.hold("c-rac-mine", "sid-rac")
    assert vis.runs_as_chat(mine) is False


def test_only_its_own_call_releases_a_chat(calls):
    calls.hold("c-held", "sid-later")
    calls.release("c-held", "sid-earlier")
    assert calls.holds("c-held")
    calls.release("c-held", "sid-later")
    assert not calls.holds("c-held")


def test_a_wrapups_record_is_nobodys_to_drive(world):
    cid = session_kind.task_chat_id("run-wrap1")
    record = _chat(cid, vis.task_chat_owner(AGENT))
    _run("run-wrap1", cid, kind=task_kinds.RUN_WRAPUP, by="")
    for sub, role, admin in ((MANAGER, roles.MANAGER, False), ("user-admin", roles.ADMIN, True)):
        assert vis.drive_refusal(record, sub, role, admin=admin) == vis.WRAPUP_RUN_REFUSED
    assert vis.drive_refusal_for(_master_key(), record) == vis.WRAPUP_RUN_REFUSED


def test_a_worker_whose_run_writes_knowledge_is_a_managers(world):
    worker = _chat("c-worker", vis.shared_chat_owner(AGENT))
    _run("run-kw", "c-worker")
    assert vis.drive_refusal(worker, EDITOR, roles.EDITOR) == vis.KNOWLEDGE_RUN_REFUSED
    assert vis.drive_refusal(worker, MANAGER, roles.MANAGER) == ""
    # Below the editor tier the shared space's floor answers first.
    assert "editor role" in vis.drive_refusal(worker, VIEWER, roles.VIEWER)


def test_a_session_with_no_person_takes_only_the_knowledge_rule(world, monkeypatch):
    """A task, a trigger or an agent-scope session continuing its own worker
    has no role on the pool: the floor and the owner rule are a person's, the
    knowledge rule is everyone's."""
    plain = _chat("c-plain", vis.shared_chat_owner(AGENT))
    _run("run-plain", "c-plain", by=EDITOR)
    writes = _chat("c-writes", vis.shared_chat_owner(AGENT))
    _run("run-writes", "c-writes")
    assert vis.drive_refusal_for(_no_person(), plain) == ""
    assert vis.drive_refusal_for(_no_person(), writes) == vis.KNOWLEDGE_RUN_REFUSED
    from core.config import task_config_builder
    monkeypatch.setattr(task_config_builder, "session_holds_knowledge_write",
                        lambda sid: sid == "sid-holds")
    assert vis.drive_refusal_for(_no_person("sid-holds"), writes) == ""


def test_the_platforms_own_caller_passes(world):
    writes = _chat("c-writes2", vis.shared_chat_owner(AGENT))
    _run("run-writes2", "c-writes2")
    assert vis.drive_refusal_for(_master_key(), writes) == ""


def test_a_persons_chat_is_theirs_or_an_admins(world):
    own = _chat("c-mine", EDITOR)
    assert vis.drive_refusal(own, EDITOR, roles.EDITOR) == ""
    assert vis.drive_refusal(own, MANAGER, roles.MANAGER) == vis.TASK_CONTINUE_REFUSED
    assert vis.drive_refusal(own, "user-admin", roles.ADMIN, admin=True) == ""


def test_the_doors_refuse_an_outside_principal(world):
    """The doors that take a request's principal: a key's turn, the voice
    door, a card's press and the delegation's rule; the move's asker."""
    from api.public import key_turns
    from services.cards import say
    from services.media import duplex_service
    from services.remote import chat_move
    pool = _chat("c-pool2", vis.shared_chat_owner(AGENT))
    u = _outside()
    assert key_turns.drive_refusal(u, pool) == vis.OUTSIDE_DRIVE_REFUSED
    assert duplex_service.chat_access_denied_reason("c-pool2", u)
    assert chat_move.may_drive(pool, chat_move.Asker("", roles.EXTERNAL, False)) is False
    with pytest.raises(say.SayRefused):
        asyncio.run(say.check(pool, u))


def test_a_task_chat_whose_run_is_gone_is_nobodys(world):
    """Nothing says who may continue it: a person, an admin, a session and
    the platform's own caller are all refused, at the rule and at the move."""
    from services.remote import chat_move
    gone = _chat(session_kind.task_chat_id("run-gone"), vis.task_chat_owner(AGENT), source_type="task")
    for sub, role, admin in ((MANAGER, roles.MANAGER, False), ("user-admin", roles.ADMIN, True)):
        assert vis.drive_refusal(gone, sub, role, admin=admin) == vis.TASK_RUN_NOT_FOUND
        assert chat_move.may_drive(gone, chat_move.Asker(sub, role, admin)) is False
    for principal in (_no_person(), _master_key()):
        assert vis.drive_refusal_for(principal, gone) == vis.TASK_RUN_NOT_FOUND


def test_an_empty_asker_never_drives(world):
    """No person and no session is nobody: refused on every chat."""
    from services.remote import chat_move
    cid = session_kind.task_chat_id("run-agent")
    task = _chat(cid, vis.task_chat_owner(AGENT), source_type="task")
    _run("run-agent", cid, kind="scheduled", by=EDITOR)
    chats = (_chat("c-own-e", EDITOR), _chat("c-pool-e", vis.shared_chat_owner(AGENT)), task)
    for chat in chats:
        assert vis.drive_refusal(chat, "", roles.MANAGER) == vis.TASK_CONTINUE_REFUSED, chat["id"]
        assert chat_move.may_drive(chat, chat_move.Asker("", roles.MANAGER, False)) is False, chat["id"]


def test_a_session_with_no_person_never_drives_a_persons_chat_or_run(world):
    """A task or a trigger acts as the agent: a person's own chat and a
    person's own run are theirs."""
    cid = session_kind.task_chat_id("run-user")
    users_run = _chat(cid, vis.task_chat_owner(AGENT), source_type="task")
    _run("run-user", cid, kind="scheduled", by=EDITOR, scope="user")
    for chat in (_chat("c-own-v", VIEWER), users_run):
        assert vis.drive_refusal_for(_no_person(), chat) == vis.TASK_CONTINUE_REFUSED, chat["id"]


def test_a_managers_run_moves_its_own_chat(world, monkeypatch):
    """A run with no person moves its own chat as its session: the knowledge
    write its session holds is what the rule asks for."""
    from core.config import task_config_builder
    from services.remote import chat_move
    monkeypatch.setattr(task_config_builder, "session_holds_knowledge_write",
                        lambda sid: sid == "sid-own-run")
    cid = session_kind.task_chat_id("run-own")
    chat = _chat(cid, vis.task_chat_owner(AGENT), source_type="task")
    _run("run-own", cid, kind="scheduled")
    assert chat_move.may_drive(chat, chat_move.Asker("", roles.MANAGER, False, session_id="sid-own-run"))
    assert not chat_move.may_drive(chat, chat_move.Asker("", roles.MANAGER, False, session_id="sid-other"))


def test_the_voice_is_a_persons(world):
    """A session or the master key speaks for nobody at the voice door, on
    any chat it might reach."""
    from services.media import duplex_service
    cid = session_kind.task_chat_id("run-v")
    _chat(cid, vis.task_chat_owner(AGENT), source_type="task")
    _run("run-v", cid, kind="scheduled", by=EDITOR)
    _chat("c-pool-v", vis.shared_chat_owner(AGENT))
    for chat_id in ("c-pool-v", cid):
        for principal in (_no_person(), _master_key()):
            assert duplex_service.chat_access_denied_reason(chat_id, principal) == "Access denied"
    assert duplex_service.chat_access_denied_reason("c-pool-v", _person(EDITOR, roles.EDITOR)) is None


# --- every door at once: the chats nobody drives, the principals nobody lets in ------

def _doors(u: UserContext, chat: dict) -> dict:
    """Each door's answer for one principal on one chat, the refusal as its
    sentence ("" or None when it lets the principal drive)."""
    from types import SimpleNamespace

    from api.public import key_turns
    from auth.providers import acting_role_of
    from services.cards import say
    from services.media import duplex_service
    from services.remote import chat_move
    from services.scheduler import continue_pass
    from tests.session.test_ws_dashboard_resume_controls import _bare_connection
    from ws import duplex_attach
    sub, agent = u.acting_sub or "", chat["agent"]
    role = acting_role_of(sub, agent) if sub else u.acting_role(agent)

    def _say():
        try:
            asyncio.run(say.check(chat, u))
        except say.SayRefused as e:
            return e.detail
        return ""

    def _judge():
        from datetime import datetime, timezone
        verdict = continue_pass.judge({"chat_id": chat["id"], "kind": session_kind.DASHBOARD.name,
                                       "person": sub, "run_id": "", "cause": "",
                                       "cut_at": datetime.now(timezone.utc).isoformat()})
        return "" if verdict.action == continue_pass.CONTINUE else verdict.reason
    answers = {
        "key": key_turns.drive_refusal(u, chat),
        "duplex": duplex_service.chat_access_denied_reason(chat["id"], u),
        "move": "" if chat_move.may_drive(chat, chat_move.Asker(
            sub, role, roles.is_admin(role), session_id=u.session_id or "")) else "refused",
        "say": _say(),
        "delegation": vis.drive_refusal_for(u, chat),
    }
    if sub:
        conn = _bare_connection(chat["id"], None, user_sub=sub, agent=agent)
        answers["socket"] = asyncio.run(conn._deny_task_continue(chat["id"], quiet=True))
        answers["duplex_attach"] = asyncio.run(duplex_attach._resolve(
            SimpleNamespace(duplex_id=f"dx-{sub}", chat_id=chat["id"], sub=sub)))
        answers["judge"] = _judge()
    return answers


def _nobodys(world) -> dict:
    cid = session_kind.task_chat_id
    task_owner = vis.task_chat_owner(AGENT)
    wrap = _chat(cid("run-dw"), task_owner, source_type="task")
    _run("run-dw", wrap["id"], kind=task_kinds.RUN_WRAPUP, by="")
    from storage.chat import conversation_store
    call = _chat("c-dcall", vis.PHONE_CHAT_OWNER, source_type=session_kind.PHONE.source_type)
    conversation_store.create("c-dcall", AGENT, line="caller", direction="inbound")
    from services.phone import call_chats
    call_chats.hold("c-dpcall", "sid-dpcall")
    return {"a call": call,
            "a person's own call while it runs": _chat("c-dpcall", EDITOR,
                                                       source_type=session_kind.PHONE.source_type),
            "a wrap-up's record": task_store.get_chat(wrap["id"]),
            "a task chat whose run is gone": _chat(cid("run-dgone"), task_owner, source_type="task")}


def test_every_door_refuses_the_chats_nobody_drives(world, monkeypatch, calls):
    """A call, a person's own call while it runs, a wrap-up's record and a
    task chat whose run is gone: every door refuses every person, an admin
    and the agent's manager included."""
    from ws import dashboard_chat_move

    async def _no_lock(conn, cid):
        return ""
    monkeypatch.setattr(dashboard_chat_move, "drive_refusal", _no_lock)
    task_store.upsert_user("user-dr-admin", "a@test.com", "a", "admin")
    admin = UserContext(sub="user-dr-admin", email="", name="a", role="admin", agents=[], agent_roles={})
    for name, chat in _nobodys(world).items():
        for u in (admin, _person(MANAGER, roles.MANAGER), _person(EDITOR, roles.EDITOR)):
            for door, answer in _doors(u, chat).items():
                assert answer not in ("", None, "no_session"), (name, u.sub, door)


def test_the_owner_writes_in_their_call_chat_at_every_door_once_it_ends(world, monkeypatch, calls):
    """The call over, every door that asks the rule lets its owner in, on
    the chat's own session the person's next message built: the socket, the
    voice, the move, a card's press, a key's turn and the delegation."""
    from core.session import session_state
    from ws import dashboard_chat_move

    async def _no_lock(conn, cid):
        return ""
    monkeypatch.setattr(dashboard_chat_move, "drive_refusal", _no_lock)
    _chat("c-dpcall-over", EDITOR, source_type=session_kind.PHONE.source_type)
    task_store.update_chat("c-dpcall-over", session_id="sid-over")
    calls.hold("c-dpcall-over", "sid-over")
    calls.release("c-dpcall-over", "sid-over")
    monkeypatch.setitem(session_state._sessions, "sid-over", {"client_type": session_kind.DASHBOARD.name})
    answers = _doors(_person(EDITOR, roles.EDITOR), task_store.get_chat("c-dpcall-over"))
    for door in ("socket", "duplex", "move", "say", "key", "delegation"):
        assert answers[door] in ("", None), (door, answers[door])
    # The voice's attach resolves the chat's session to speak into.
    assert not isinstance(answers["duplex_attach"], str), answers["duplex_attach"]


def test_every_door_refuses_an_outside_person_and_nobody(world, monkeypatch):
    """An outside person and a principal with neither a person nor a session
    get no door, on the agent's pool, a person's chat and a run's chat."""
    from services.remote import chat_move
    cid = session_kind.task_chat_id("run-dt")
    task = _chat(cid, vis.task_chat_owner(AGENT), source_type="task")
    _run("run-dt", cid, kind="scheduled", by=EDITOR)
    chats = (_chat("c-dpool", vis.shared_chat_owner(AGENT)), _chat("c-dmine", EDITOR), task)
    for chat in chats:
        for door, answer in _doors(_outside(), chat).items():
            assert answer not in ("", None), ("outside", chat["id"], door)
        assert chat_move.may_drive(chat, chat_move.Asker("", roles.MANAGER, False)) is False
        assert vis.drive_refusal(chat, "", roles.MANAGER), chat["id"]

"""Meeting creation authorization — a real-human VIEWER must not be able to
convene a meeting that makes a Shared-only agent (or any agent-scope meeting)
run with agent-scope MANAGER capability (shared ``/workspace/`` RW).

This mirrors ``_enforce_task_scope`` for tasks (api/tasks/tasks.py): agent-scope
participation requires editor+. A participant runs agent scope when the meeting
is agent-scoped OR the agent is Shared-only. User-scope participants run as the
caller's own per-agent role (self-limiting), so read access suffices.

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/meetings/test_meeting_authz.py -q
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from api.meetings.meetings import create_meeting, CreateMeetingRequest
from auth.providers import UserContext
from storage.agents import agent_store


def _user(role_map: dict[str, str], sub: str = "u-1") -> UserContext:
    return UserContext(
        sub=sub, email=f"{sub}@x.test", name="U", role="member",
        agents=list(role_map.keys()), agent_roles=dict(role_map),
    )


def _create(user, agents, scope="user", x_agent_name=None):
    req = CreateMeetingRequest(
        topic="t", agents=agents, scope=scope,
        parent_session_id="", parent_run_id="",
    )
    return asyncio.run(create_meeting(req, user=user, x_agent_name=x_agent_name))


def test_viewer_cannot_include_shared_only_agent(temp_db):
    agent_store.create_agent("ops", "Ops", collaborative=True)
    agent_store.create_agent("caller", "Caller", default_scope="agent", collaborative=False)
    # Manager on the moderator (ops), only VIEWER on the Shared-only caller.
    u = _user({"ops": "manager", "caller": "viewer"})
    with pytest.raises(HTTPException) as ei:
        _create(u, ["ops", "caller"], scope="user", x_agent_name="ops")
    assert ei.value.status_code == 403
    assert "caller" in ei.value.detail
    # Worded by what the participant runs as: the agent, Shared only.
    assert "set to Shared only" in ei.value.detail and "run as viewer" in ei.value.detail


def test_editor_can_include_shared_only_agent(temp_db):
    agent_store.create_agent("ops", "Ops", collaborative=True)
    agent_store.create_agent("caller", "Caller", default_scope="agent", collaborative=False)
    u = _user({"ops": "manager", "caller": "editor"})
    out = _create(u, ["ops", "caller"], scope="user", x_agent_name="ops")
    assert out["status"] == "pending"


def test_viewer_user_scope_collaborative_meeting_ok(temp_db):
    # A viewer CAN convene a user-scope meeting of collaborative agents — they
    # run as the viewer's own read-only per-agent role, so it is self-limiting.
    agent_store.create_agent("a1", "A1", collaborative=True)
    agent_store.create_agent("a2", "A2", collaborative=True)
    u = _user({"a1": "viewer", "a2": "viewer"})
    out = _create(u, ["a1", "a2"], scope="user", x_agent_name="a1")
    assert out["status"] == "pending"


def test_viewer_cannot_create_agent_scope_meeting(temp_db):
    # Agent-scope meeting → every participant runs agent-scope → editor+ required.
    agent_store.create_agent("a1", "A1", default_scope="agent", collaborative=True)
    agent_store.create_agent("a2", "A2", default_scope="agent", collaborative=True)
    u = _user({"a1": "viewer", "a2": "viewer"})
    with pytest.raises(HTTPException) as ei:
        _create(u, ["a1", "a2"], scope="agent", x_agent_name="a1")
    assert ei.value.status_code == 403


def _nouser(agent: str) -> UserContext:
    """No-user AGENT_SESSION JWT (agent-scope task / trigger / phone)."""
    return UserContext(
        sub="session:s-1", email="", name="", role="agent",
        is_api_key=True, session_id="s-1", agent=agent,
    )


def test_nouser_session_clamped_to_roster(temp_db):
    # C4 (2026-08-14): a no-user session used to skip ALL per-participant
    # checks — it could convene any agent that exists. Its reach is now its
    # agent's delegation roster ∪ itself (autonomy needs explicit wiring).
    agent_store.create_agent("a1", "A1", default_scope="agent", collaborative=True)
    agent_store.create_agent("a2", "A2", default_scope="agent", collaborative=True)
    agent_store.create_agent("a3", "A3", default_scope="agent", collaborative=True)
    agent_store.set_delegation_targets("a1", ["a2"])
    # Wired participant: allowed.
    out = _create(_nouser("a1"), ["a1", "a2"], scope="agent", x_agent_name="a1")
    assert out["status"] == "pending"
    # Unwired participant: 403, naming the agent.
    with pytest.raises(HTTPException) as ei:
        _create(_nouser("a1"), ["a1", "a3"], scope="agent", x_agent_name="a1")
    assert ei.value.status_code == 403
    assert "a3" in ei.value.detail


def test_nouser_clamp_ignores_client_header(temp_db):
    # The roster is read from the TOKEN's agent — a spoofed X-Agent-Name
    # naming a well-wired agent must not widen the reach.
    agent_store.create_agent("a1", "A1", default_scope="agent", collaborative=True)
    agent_store.create_agent("hub", "Hub", default_scope="agent", collaborative=True)
    agent_store.create_agent("a3", "A3", default_scope="agent", collaborative=True)
    agent_store.set_delegation_targets("hub", ["a3", "a1"])
    with pytest.raises(HTTPException) as ei:
        _create(_nouser("a1"), ["a1", "a3"], scope="agent", x_agent_name="hub")
    assert ei.value.status_code == 403


def test_master_key_unclamped(temp_db):
    agent_store.create_agent("a1", "A1", default_scope="agent", collaborative=True)
    agent_store.create_agent("a2", "A2", default_scope="agent", collaborative=True)
    svc = UserContext(sub="api-key", email="", name="", role="admin",
                      is_api_key=True)
    out = _create(svc, ["a1", "a2"], scope="agent", x_agent_name="a1")
    assert out["status"] == "pending"


# ---------------------------------------------------------------------------
# Reading a meeting: every participant, or nothing. Acting in one: the
# creator, an admin, or a session of the participant it is about.
# ---------------------------------------------------------------------------

from api.meetings.meetings import (  # noqa: E402
    end_meeting_endpoint, get_meeting_endpoint, get_transcript_endpoint,
    leave_meeting_endpoint, list_meetings_endpoint, propose_conclude_endpoint,
    start_meeting_endpoint,
)
from storage import database as task_store  # noqa: E402
from storage.chat import meeting_status  # noqa: E402


def _nouser_on(agent: str, sid: str) -> UserContext:
    return UserContext(sub=f"session:{sid}", email="", name="", role="agent",
                       is_api_key=True, session_id=sid, agent=agent)


def _office():
    for slug in ("head", "alpha", "beta"):
        agent_store.create_agent(slug, slug.title(), default_scope="agent", collaborative=True)
    agent_store.set_delegation_targets("head", ["alpha", "beta"])


def _turns(mid: str) -> None:
    import json
    task_store.add_meeting_turn(mid, 1, 0, "head", "assistant", "HEAD office margin",
                                "x" * 400, json.dumps([{"name": "Read", "input": "office/x.xlsx"}]),
                                "s-head", 0.0)
    task_store.add_meeting_turn(mid, 2, 0, "beta", "assistant", "BETA other project", "",
                                "[]", "s-beta", 0.0)


def _list(user, agent=None):
    return asyncio.run(list_meetings_endpoint(agent=agent, status=None, created_by=None,
                                              limit=50, offset=0, user=user))


def _status_of(call, *args, **kw) -> int:
    with pytest.raises(HTTPException) as ei:
        asyncio.run(call(*args, **kw))
    return ei.value.status_code


def test_pm_cannot_read_meeting_with_foreign_participants(temp_db):
    """A contributor on one project agent never lists, opens or transcribes
    an agent-scope meeting that includes agents they cannot read, nor
    another person's user-scope meeting by id; a reader of every
    participant does, and the page's total counts what it lists."""
    _office()
    mid = _create(_nouser_on("head", "s-h"), ["head", "alpha", "beta"], scope="agent",
                  x_agent_name="head")["meeting_id"]
    _turns(mid)
    pm = _user({"alpha": "contributor"}, sub="pm-1")
    rows = _list(pm)
    assert rows["meetings"] == [] and rows["total"] == 0
    assert _list(pm, agent="alpha")["meetings"] == []
    for call in (get_meeting_endpoint, get_transcript_endpoint):
        assert _status_of(call, mid, user=pm) == 404
    # Readers of every participant: the CEO, and the head's own run.
    ceo = _user({"head": "manager", "alpha": "manager", "beta": "manager"}, sub="ceo-1")
    assert {t["agent"] for t in asyncio.run(get_transcript_endpoint(mid, user=ceo))["turns"]} == {"head", "beta"}
    listed = _list(ceo)
    assert [m["id"] for m in listed["meetings"]] == [mid] and listed["total"] == 1
    assert asyncio.run(get_meeting_endpoint(mid, user=_nouser_on("head", "s-h2")))["id"] == mid
    # A no-user session on alpha reads alpha's own meetings only.
    assert _list(_nouser_on("alpha", "s-a"))["meetings"] == []
    assert _status_of(get_meeting_endpoint, mid, user=_nouser_on("alpha", "s-a")) == 404

    # A user-scope meeting is its creator's: not listed to a run on alpha,
    # not readable by id by the PM, nor by another manager of both agents.
    mid2 = _create(ceo, ["head", "alpha"], scope="user", x_agent_name="head")["meeting_id"]
    _turns(mid2)
    assert mid2 not in [m["id"] for m in _list(_nouser_on("alpha", "s-a"))["meetings"]]
    assert _status_of(get_transcript_endpoint, mid2, user=pm) == 404
    assert _status_of(get_meeting_endpoint, mid2, user=_nouser_on("alpha", "s-a")) == 404
    cfo = _user({"head": "manager", "alpha": "manager"}, sub="cfo-1")
    assert _status_of(get_meeting_endpoint, mid2, user=cfo) == 404
    assert asyncio.run(get_meeting_endpoint(mid2, user=ceo))["id"] == mid2
    assert [m["id"] for m in _list(ceo)["meetings"]] == [mid2, mid]


def test_the_verbs_take_the_creator_or_the_participants_own_session(temp_db, monkeypatch):
    """start and end: the creator, an admin or the moderator's own session;
    leave and propose-conclude: the creator, an admin or a session of the
    participant named. A header a cookie sends grants nothing; a session
    acts for its own agent whatever it sends; a caller who can neither read
    nor act gets 404."""
    from services.meetings import meeting_orchestrator

    async def _no_start(meeting_id):
        return None
    monkeypatch.setattr(meeting_orchestrator, "start_meeting", _no_start)
    _office()
    ceo = _user({"head": "manager", "alpha": "manager", "beta": "manager"}, sub="ceo-1")
    mid = _create(ceo, ["head", "alpha", "beta"], scope="agent", x_agent_name="head")["meeting_id"]
    pm = _user({"alpha": "contributor"}, sub="pm-1")
    reader = _user({"head": "manager", "alpha": "manager", "beta": "manager"}, sub="cfo-1")
    head, alpha, beta = (_nouser_on(a, f"s-{a}") for a in ("head", "alpha", "beta"))

    # start
    assert _status_of(start_meeting_endpoint, mid, user=pm, x_agent_name="head") == 404
    assert _status_of(start_meeting_endpoint, mid, user=reader, x_agent_name="head") == 403
    assert _status_of(start_meeting_endpoint, mid, user=alpha, x_agent_name="head") == 403
    assert asyncio.run(start_meeting_endpoint(mid, user=head, x_agent_name="beta"))["status"] == "starting"
    task_store.update_meeting(mid, status=meeting_status.ACTIVE)

    # propose-conclude: the session's own agent is the proposer
    assert _status_of(propose_conclude_endpoint, mid, user=pm, x_agent_name="alpha") == 404
    assert _status_of(propose_conclude_endpoint, mid, user=reader, x_agent_name="alpha") == 403
    out = asyncio.run(propose_conclude_endpoint(mid, user=alpha, x_agent_name="head"))
    assert out["status"] == "paused" and out["proposed_by"] == "alpha"
    task_store.update_meeting(mid, status=meeting_status.ACTIVE)

    # leave: beta's session leaves beta; the creator names the agent
    assert _status_of(leave_meeting_endpoint, mid, user=reader, x_agent_name="beta") == 403
    assert asyncio.run(leave_meeting_endpoint(mid, user=beta, x_agent_name="head"))["agent"] == "beta"
    assert asyncio.run(leave_meeting_endpoint(mid, user=ceo, x_agent_name="alpha"))["agent"] == "alpha"

    # end: a participant's session cannot, the moderator's can
    assert _status_of(end_meeting_endpoint, mid, user=pm, x_agent_name="head") == 404
    assert _status_of(end_meeting_endpoint, mid, user=reader, x_agent_name="head") == 403
    assert _status_of(end_meeting_endpoint, mid, user=alpha, x_agent_name="head") == 403
    assert asyncio.run(end_meeting_endpoint(mid, user=head, x_agent_name=None))["status"] == "concluding"


def test_a_paused_resume_never_overwrites_an_end(temp_db):
    """The orchestrator's read-then-write steps run on the DB executor now:
    the resume after a propose-conclude and the conclusion are conditional
    writes, and a leave takes the row's lock."""
    _office()
    mid = task_store.create_meeting("mtg-atomic", "t", '["head", "alpha", "beta"]', "head",
                                    "round_robin", 30, "chat-1", None, None, "agent", "ceo-1")["id"]
    task_store.update_meeting(mid, status=meeting_status.PAUSED)
    assert task_store.update_meeting_if(mid, meeting_status.ENDABLE,
                                        status=meeting_status.CONCLUDING) is True
    # The resume that lost the race writes nothing.
    assert task_store.update_meeting_if(mid, (meeting_status.PAUSED,),
                                        status=meeting_status.ACTIVE) is False
    assert task_store.get_meeting(mid)["status"] == meeting_status.CONCLUDING
    # The conclusion keeps a terminal row as it is.
    task_store.update_meeting(mid, status=meeting_status.FAILED)
    assert task_store.update_meeting_if(mid, meeting_status.STATUSES - meeting_status.TERMINAL,
                                        status=meeting_status.CONCLUDED) is False
    assert task_store.get_meeting(mid)["status"] == meeting_status.FAILED

    task_store.update_meeting(mid, status=meeting_status.ACTIVE)
    assert task_store.remove_active_participant(mid, "beta", meeting_status.LEAVABLE) == ["head", "alpha"]
    assert task_store.remove_active_participant(mid, "alpha", meeting_status.LEAVABLE) == ["head"]
    assert task_store.remove_active_participant(mid, "beta", meeting_status.LEAVABLE) is None
    task_store.update_meeting(mid, status=meeting_status.CONCLUDED)
    assert task_store.remove_active_participant(mid, "head", meeting_status.LEAVABLE) is None


# ---------------------------------------------------------------------------
# A leave through the route reaches the round loop: the loop re-reads the
# row's active list each round and writes its own departures through the
# row's lock.
# ---------------------------------------------------------------------------

def _leave_office(monkeypatch):
    import json
    from services.meetings import meeting_orchestrator as MO
    _office()
    agent_store.create_agent("gamma", "Gamma", default_scope="agent", collaborative=True)
    ceo = _user({a: "manager" for a in ("head", "alpha", "beta", "gamma")}, sub="ceo-1")
    mid = task_store.create_meeting(
        "mtg-leave", "t", json.dumps(["head", "alpha", "beta", "gamma"]), "head",
        "directed", 30, "chat-leave", None, None, "agent", "ceo-1")["id"]
    task_store.update_meeting(mid, status=meeting_status.ACTIVE)
    sessions = {a: f"s-{a}" for a in ("head", "alpha", "beta", "gamma")}
    closed: list[str] = []

    class _Layer:
        async def close_session(self, sid):
            closed.append(sid)
    for sid in sessions.values():
        monkeypatch.setitem(MO._meeting_session_layers, sid, _Layer())
    return ceo, mid, sessions, closed


def _run_rounds(monkeypatch, mid, sessions, turn):
    """meeting_produce on the real row, each turn scripted by ``turn(agent)``;
    returns the rounds run and the SYSTEM events emitted."""
    from types import SimpleNamespace
    from core.events.common_events import SYSTEM
    from services.meetings import meeting_orchestrator as MO
    rounds: list[list[str]] = []

    async def fake_live(agent, agent_sessions, meeting, transcript, pending, q, meeting_id):
        rounds.append([agent])
        return await turn(agent)

    async def fake_batch(ready, agent_sessions, meeting, transcript, pending, q, meeting_id):
        rounds.append(list(ready))
        return [await turn(a) for a in ready]
    monkeypatch.setattr(MO, "_run_live_turn", fake_live)
    monkeypatch.setattr(MO, "_run_parallel_batch", fake_batch)

    async def _go():
        q: asyncio.Queue = asyncio.Queue()
        await MO.meeting_produce(mid, sessions, q,
                                 SimpleNamespace(chat_id="chat-mtg", system_queue=[]))
        events = []
        while not q.empty():
            ev = q.get_nowait()
            if ev.type == SYSTEM:
                events.append(ev.data)
        return events
    return rounds, asyncio.run(_go())


def _said(agent, *, directed=(), called=()):
    from services.meetings import meeting_orchestrator as MO
    text = f"{agent} reports its findings in full. " * 3
    return MO.TurnResult(agent=agent, events=[], content=text, directed_to=list(directed),
                         tools_called=set(called), tail_text=len(text))


def test_a_rest_leave_during_a_round_keeps_the_agent_out(temp_db, monkeypatch):
    """The creator takes beta out while alpha's turn runs and alpha then
    addresses beta: beta gets no further turn, its session is closed and the
    dashboard is told it left."""
    import json
    from ws import wire_events as wire
    ceo, mid, sessions, closed = _leave_office(monkeypatch)
    spoken: list[str] = []

    async def turn(agent):
        spoken.append(agent)
        if agent == "head":
            return (_said("head", directed=["alpha"]) if spoken.count("head") == 1
                    else _said("head", called=("end_meeting",)))
        if agent == "alpha":
            await leave_meeting_endpoint(mid, user=ceo, x_agent_name="beta")
            return _said("alpha", directed=["beta", "gamma"])
        return _said(agent)

    rounds, events = _run_rounds(monkeypatch, mid, sessions, turn)
    # gamma answers alone, then the moderator wraps up.
    assert rounds == [["head"], ["alpha"], ["gamma"], ["head"]]
    assert closed == ["s-beta"]
    assert [e["agent"] for e in events
            if e.get("subtype") == wire.SUBTYPE_MEETING_AGENT_LEFT] == ["beta"]
    assert json.loads(task_store.get_meeting(mid)["active_participants"]) == ["head", "alpha", "gamma"]


def test_work_queued_for_a_removed_agent_is_dropped(temp_db, monkeypatch):
    """The moderator addresses only beta while the creator takes beta out:
    the work is dropped, nobody else is ready and the moderator spoke last,
    so the meeting concludes."""
    ceo, mid, sessions, closed = _leave_office(monkeypatch)

    async def turn(agent):
        await leave_meeting_endpoint(mid, user=ceo, x_agent_name="beta")
        return _said("head", directed=["beta"])

    rounds, events = _run_rounds(monkeypatch, mid, sessions, turn)
    assert rounds == [["head"]]
    assert closed == ["s-beta"]
    ends = [e for e in events if e.get("subtype") == "meeting_concluded"]
    assert ends and "error" not in ends[-1]


def test_a_rest_leave_of_the_moderator_ends_the_meeting(temp_db, monkeypatch):
    """The creator takes the moderator out while alpha's turn runs: the
    meeting ends after that round, as when the moderator fails, and beta,
    whom alpha addressed, never runs."""
    from ws import wire_events as wire
    ceo, mid, sessions, closed = _leave_office(monkeypatch)
    spoken: list[str] = []

    async def turn(agent):
        spoken.append(agent)
        if agent == "head":
            return _said("head", directed=["alpha"])
        await leave_meeting_endpoint(mid, user=ceo, x_agent_name="head")
        return _said("alpha", directed=["beta"])

    rounds, events = _run_rounds(monkeypatch, mid, sessions, turn)
    assert rounds == [["head"], ["alpha"]]
    assert closed == ["s-head"]
    assert [e["agent"] for e in events
            if e.get("subtype") == wire.SUBTYPE_MEETING_AGENT_LEFT] == ["head"]
    ends = [e for e in events if e.get("subtype") == "meeting_concluded"]
    assert ends and "error" not in ends[-1]


@pytest.mark.parametrize("gamma_ends", ["leaves", "fails"])
def test_the_round_loop_never_writes_a_removed_agent_back(temp_db, monkeypatch, gamma_ends):
    """The creator takes beta out while gamma's turn runs, and gamma then
    leaves (its own leave_meeting) or fails: the loop's own departure is
    written through the row's lock and beta stays out of the row."""
    import json
    ceo, mid, sessions, closed = _leave_office(monkeypatch)
    spoken: list[str] = []

    async def turn(agent):
        spoken.append(agent)
        if agent == "head":
            return (_said("head", directed=["gamma"]) if spoken.count("head") == 1
                    else _said("head", called=("end_meeting",)))
        if agent == "gamma":
            await leave_meeting_endpoint(mid, user=ceo, x_agent_name="beta")
            if gamma_ends == "leaves":
                await leave_meeting_endpoint(mid, user=_nouser_on("gamma", "s-gamma"))
                return _said("gamma", called=("leave_meeting",))
            return _said("gamma", called=("_failed",))
        return _said(agent)

    rounds, _ = _run_rounds(monkeypatch, mid, sessions, turn)
    assert "beta" not in spoken
    assert json.loads(task_store.get_meeting(mid)["active_participants"]) == ["head", "alpha"]
    # Every departure's session is closed when it leaves (the failed one by
    # the failure path, which closes it the same way).
    assert sorted(closed) == ["s-beta", "s-gamma"]


def test_thinking_goes_to_an_admin_or_the_creator_only(temp_db):
    """The operator's choice (2026-10-02): a turn's thinking is hidden from
    the other agents and from every reader but a person at the dashboard
    who is an admin or the meeting's creator."""
    _office()
    ceo = _user({"head": "manager", "alpha": "manager", "beta": "manager"}, sub="ceo-1")
    mid = _create(ceo, ["head", "alpha", "beta"], scope="agent", x_agent_name="head")["meeting_id"]
    _turns(mid)
    head_turn = lambda u: next(t for t in asyncio.run(  # noqa: E731
        get_transcript_endpoint(mid, user=u))["turns"] if t["agent"] == "head")
    assert head_turn(ceo)["thinking"] == "x" * 400                       # the creator
    admin = UserContext(sub="root-1", email="r@x", name="r", role="admin", agents=[])
    assert head_turn(admin)["thinking"] == "x" * 400                     # an admin
    reader = _user({"head": "manager", "alpha": "manager", "beta": "manager"}, sub="cfo-1")
    assert "thinking" not in head_turn(reader)                           # another reader
    token = UserContext(sub="ceo-1", email="c@x", name="c", role="admin", agents=[],
                        is_api_key=True, session_id="s9", agent="head")
    assert "thinking" not in head_turn(token)                            # never a token


def test_the_participants_prompt_carries_no_thinking(temp_db):
    import json
    from services.meetings import meeting_context
    _office()
    meeting = {"id": "m1", "topic": "margins", "participants": json.dumps(["head", "beta"]),
               "moderator": "head", "scope": "agent"}
    transcript = [{"agent": "head", "role": "assistant", "content": "HEAD says hi",
                   "thinking": "SECRET-REASONING", "tools": []}]
    prompt = meeting_context.build_turn_prompt(meeting, "beta", transcript)
    assert "HEAD says hi" in prompt and "SECRET-REASONING" not in prompt

"""The offboarding closer (``services/agents/offboarding_sessions.py``): a
person's live sessions close with their standing on an agent.

A person removed from an agent, demoted there or deleted loses their chat
sessions, terminals and pre-warms on it, and their user-scope runs on an
agent they can no longer reach. Other people's sessions, other agents,
agent-scope runs, meetings, phone calls and external callers are left
alone, and the standing is read again before anything closes.

Run: cd proxy && venv/bin/pytest tests/agents/test_offboarding_sessions.py -v
"""

from __future__ import annotations

import ast
import asyncio
from types import SimpleNamespace

import pytest

from services.agents import offboarding
from services.agents import offboarding_sessions as closer
from services.agents.offboarding import AgentLoss, OffboardEvent
from storage import database as db
from tests._paths import PROXY_DIR

A, B = "offb-a", "offb-b"


class _Layer:
    def __init__(self):
        self.sessions: set[str] = set()
        self.closed: list[str] = []

    def local_session_ids(self):
        return list(self.sessions)

    async def close_session(self, sid):
        self.closed.append(sid)
        self.sessions.discard(sid)


class _Pump:
    def __init__(self, sid):
        self.session_id = sid
        self.is_done = False
        self.calls: list[str] = []

    def cancel_all_queued(self):
        self.calls.append("cancel_all_queued")
        return ""

    def abort(self):
        self.calls.append("abort")


@pytest.fixture
def world(temp_db, monkeypatch):
    """Two people with usernames, fake registries, and a record of every
    close, pump stop and run stop."""
    from core.events import stream_pump
    from core.session import interactive_session, session_manager, session_state
    from core.session import prewarm_session_registry as prewarm
    from services.scheduler import scheduler, shared

    pat = db.create_local_user("pat@t.com", "Pat Person", "Pat", "member", "x")
    quinn = db.create_local_user("quinn@t.com", "Quinn Other", "Quinn", "member", "x")
    for sub in (pat, quinn):
        db.set_user_agents(sub, [A, B], "user-admin", agent_roles={A: "contributor", B: "contributor"})
    w = SimpleNamespace(pat=pat, quinn=quinn, pat_name=db.get_username_by_sub(pat),
                        quinn_name=db.get_username_by_sub(quinn), layer=_Layer(), ctx={},
                        client_type={}, interactive={}, closed_pty=[], dropped=[], pumps={},
                        runs={}, running={}, stopped=[], meetings={}, noted=[])
    monkeypatch.setattr(session_manager, "_iter_session_holders", lambda: [w.layer])
    monkeypatch.setattr(session_manager, "find_layer_for_session",
                        lambda sid: w.layer if sid in w.layer.sessions else None)
    monkeypatch.setattr(session_state, "get_session_security", w.ctx.get)
    monkeypatch.setattr(session_state, "get_session_client_type",
                        lambda sid: w.client_type.get(sid, "dashboard"))
    monkeypatch.setattr(session_state, "_meeting_session_info", w.meetings)
    monkeypatch.setattr(prewarm, "_entries", {})
    monkeypatch.setattr(interactive_session, "live_session_ids", lambda local_only=False: set(w.interactive))
    monkeypatch.setattr(interactive_session, "get", w.interactive.get)

    async def _close_pty(sid, *, reason="closed", drop_queued=False):
        w.closed_pty.append((sid, reason))
        if drop_queued:
            w.dropped.append(sid)
        return True

    monkeypatch.setattr(interactive_session, "close_session", _close_pty)
    monkeypatch.setattr(stream_pump, "_active_pumps", w.pumps)
    monkeypatch.setattr(shared, "_running_tasks", w.running)
    monkeypatch.setattr(db, "get_run", w.runs.get)
    monkeypatch.setattr(scheduler, "platform_cancel_run",
                        lambda rid, reason: w.stopped.append((rid, reason)) or True)
    from core.session import session_events
    monkeypatch.setattr(session_events, "note_user_message", w.noted.append)
    monkeypatch.setattr(closer, "RESWEEP_AFTER_S", ())
    return w


def _pool(w, sid, agent, username, role="contributor", *, external=False, client="dashboard"):
    w.layer.sessions.add(sid)
    w.ctx[sid] = SimpleNamespace(agent=agent, username=username, role=role, is_external=external)
    w.client_type[sid] = client


def _pty(w, sid, agent, sub, username, role="contributor", chat="chat-1"):
    w.interactive[sid] = SimpleNamespace(agent_name=agent, user_sub=sub, role=role,
                                         username=username, chat_id=chat, _prompt_queue=[{"text": "x"}])


def _prewarm(sid, agent, sub, role="contributor"):
    from core.session import prewarm_session_registry as prewarm
    prewarm._entries[sid] = prewarm._Entry(agent=agent, user_sub=sub, role=role, exec_path="", ts=0.0)


def _event(w, reason, *losses, sub=None, username=None):
    return OffboardEvent(sub=sub or w.pat, reason=reason, agents=tuple(losses),
                         actor_sub="user-admin",
                         username=w.pat_name if username is None else username)


async def _settle():
    await asyncio.sleep(0.05)


def _closed(w) -> set[str]:
    return set(w.layer.closed) | {sid for sid, _ in w.closed_pty}


@pytest.mark.asyncio
async def test_a_removed_person_loses_their_sessions_on_that_agent_only(world):
    w = world
    _pool(w, "pat-a", A, w.pat_name)
    _pool(w, "pat-b", B, w.pat_name)
    _pool(w, "quinn-a", A, w.quinn_name)
    _pool(w, "pat-task", A, w.pat_name, client="task")
    _pool(w, "pat-phone", A, w.pat_name, client="phone")
    _pool(w, "pat-meeting", A, w.pat_name)
    w.meetings["pat-meeting"] = {}
    _pool(w, "ext-a", A, w.pat_name, external=True)
    _pty(w, "pty-a", A, w.pat, w.pat_name)
    _prewarm("warm-a", A, w.pat)
    w.layer.sessions.add("warm-a")
    db.set_user_agents(w.pat, [B], "user-admin", agent_roles={B: "contributor"})
    await closer.sweep(_event(w, offboarding.REMOVED, AgentLoss(A, "contributor", "", "contributor", "")))
    await _settle()
    assert _closed(w) == {"pat-a", "pty-a", "warm-a"}
    assert ("pty-a", closer.REASON) in w.closed_pty
    # The terminal's queued server prompts are dropped by the close itself,
    # never handed back to the ladder under the departed person's role.
    assert w.dropped == ["pty-a"]


@pytest.mark.asyncio
async def test_a_person_added_back_keeps_their_sessions(world):
    w = world
    _pool(w, "pat-a", A, w.pat_name)
    await closer.sweep(_event(w, offboarding.REMOVED, AgentLoss(A, "contributor", "", "contributor", "")))
    await _settle()
    assert _closed(w) == set()


@pytest.mark.asyncio
async def test_a_demotion_closes_only_sessions_started_above_the_new_role(world):
    w = world
    _pool(w, "as-contributor", A, w.pat_name, role="contributor")
    _pool(w, "as-viewer", A, w.pat_name, role="viewer")
    db.set_user_agents(w.pat, [A, B], "user-admin", agent_roles={A: "viewer", B: "contributor"})
    await closer.sweep(_event(w, offboarding.DEMOTED,
                              AgentLoss(A, "contributor", "viewer", "contributor", "viewer")))
    await _settle()
    assert _closed(w) == {"as-contributor"}


@pytest.mark.asyncio
async def test_deleting_a_person_closes_every_agents_sessions(world):
    w = world
    _pool(w, "pat-a", A, w.pat_name)
    _pool(w, "pat-b", B, w.pat_name)
    _pool(w, "quinn-b", B, w.quinn_name)
    assert db.delete_user(w.pat)
    await closer.sweep(_event(w, offboarding.DELETED))
    await _settle()
    assert _closed(w) == {"pat-a", "pat-b"}


@pytest.mark.asyncio
async def test_a_live_turn_is_stopped_before_the_close(world):
    w = world
    _pool(w, "pat-a", A, w.pat_name)
    pump = w.pumps["chat-9"] = _Pump("pat-a")
    db.set_user_agents(w.pat, [B], "user-admin", agent_roles={B: "contributor"})
    await closer.sweep(_event(w, offboarding.REMOVED, AgentLoss(A, "contributor", "", "contributor", "")))
    assert pump.calls == ["cancel_all_queued", "abort"] and w.noted == ["pat-a"]
    await _settle()
    assert w.layer.closed == ["pat-a"]


@pytest.mark.asyncio
async def test_an_interactive_sessions_queued_prompts_are_not_handed_back(world):
    w = world
    _pty(w, "pty-a", A, w.pat, w.pat_name)
    db.set_user_agents(w.pat, [B], "user-admin", agent_roles={B: "contributor"})
    await closer.sweep(_event(w, offboarding.REMOVED, AgentLoss(A, "contributor", "", "contributor", "")))
    assert w.interactive["pty-a"]._prompt_queue == []


@pytest.mark.asyncio
async def test_user_scope_runs_on_a_lost_agent_stop_and_agent_scope_runs_do_not(world):
    w = world
    live = SimpleNamespace(done=lambda: False)
    for rid, agent, scope, by in (("r-user-a", A, "user", w.pat), ("r-agent-a", A, "agent", w.pat),
                                  ("r-user-b", B, "user", w.pat), ("r-quinn-a", A, "user", w.quinn)):
        w.running[rid] = live
        w.runs[rid] = {"id": rid, "agent": agent, "scope": scope, "created_by": by}
    db.set_user_agents(w.pat, [B], "user-admin", agent_roles={B: "contributor"})
    await closer.sweep(_event(w, offboarding.REMOVED, AgentLoss(A, "contributor", "", "contributor", "")))
    assert w.stopped == [("r-user-a", closer.REASON)]


@pytest.mark.asyncio
async def test_a_username_now_held_by_someone_else_is_not_matched(world):
    w = world
    _pool(w, "quinn-a", A, w.quinn_name)
    # A stale event naming Quinn's username for a person who is gone.
    assert db.delete_user(w.pat)
    await closer.sweep(_event(w, offboarding.DELETED, username=w.quinn_name))
    await _settle()
    assert _closed(w) == set()


@pytest.mark.asyncio
async def test_an_event_with_no_username_matches_no_pooled_session(world):
    w = world
    _pool(w, "blank", A, "")
    _pool(w, "pat-a", A, w.pat_name)
    assert db.delete_user(w.pat)
    await closer.sweep(_event(w, offboarding.DELETED, username=""))
    await _settle()
    assert _closed(w) == set()


@pytest.mark.asyncio
async def test_a_later_pass_closes_a_session_that_landed_after_the_first(world, monkeypatch):
    w = world
    monkeypatch.setattr(closer, "RESWEEP_AFTER_S", (0.01,))
    db.set_user_agents(w.pat, [B], "user-admin", agent_roles={B: "contributor"})
    await closer.on_offboard(_event(w, offboarding.REMOVED,
                                    AgentLoss(A, "contributor", "", "contributor", "")))
    _pool(w, "late", A, w.pat_name)
    await asyncio.sleep(0.1)
    assert _closed(w) == {"late"}


@pytest.mark.asyncio
async def test_the_later_passes_cover_both_events_of_one_change(world, monkeypatch):
    """One admin change sends two events (``removed`` on one agent, then
    ``demoted`` on another). The second must not drop the first's later
    passes: a warm-up landing late on either agent still closes."""
    w = world
    monkeypatch.setattr(closer, "RESWEEP_AFTER_S", (0.05,))
    db.set_user_agents(w.pat, [B], "user-admin", agent_roles={B: "viewer"})
    await closer.on_offboard(_event(w, offboarding.REMOVED,
                                    AgentLoss(A, "contributor", "", "contributor", "")))
    await closer.on_offboard(_event(w, offboarding.DEMOTED,
                                    AgentLoss(B, "contributor", "viewer", "contributor", "viewer")))
    _pool(w, "late-a", A, w.pat_name)
    _pool(w, "late-b", B, w.pat_name)
    _pool(w, "late-b-viewer", B, w.pat_name, role="viewer")
    await asyncio.sleep(0.3)
    assert _closed(w) == {"late-a", "late-b"}


@pytest.mark.asyncio
async def test_the_chain_returns_before_the_closes_finish(world, monkeypatch):
    w = world
    gate = asyncio.Event()

    async def slow_close(sid):
        await gate.wait()
        w.layer.closed.append(sid)

    monkeypatch.setattr(w.layer, "close_session", slow_close)
    _pool(w, "pat-a", A, w.pat_name)
    db.set_user_agents(w.pat, [B], "user-admin", agent_roles={B: "contributor"})
    await asyncio.wait_for(closer.sweep(_event(
        w, offboarding.REMOVED, AgentLoss(A, "contributor", "", "contributor", ""))), 1.0)
    assert w.layer.closed == []
    gate.set()
    await _settle()
    assert w.layer.closed == ["pat-a"]


@pytest.mark.asyncio
async def test_the_reconcile_closes_what_no_longer_qualifies(world):
    w = world
    _pool(w, "pat-a", A, w.pat_name, role="contributor")
    _pool(w, "pat-b", B, w.pat_name, role="contributor")
    _pty(w, "quinn-pty-a", A, w.quinn, w.quinn_name, role="contributor")
    db.set_user_agents(w.pat, [B], "user-admin", agent_roles={B: "contributor"})
    db.set_user_agents(w.quinn, [A], "user-admin", agent_roles={A: "viewer"})
    assert await closer.reconcile() == 2
    await _settle()
    assert _closed(w) == {"pat-a", "quinn-pty-a"}


@pytest.mark.asyncio
async def test_register_subscribes_first_and_the_lifespan_calls_it(world, monkeypatch):
    monkeypatch.setattr(closer, "BOOT_RECONCILE_AFTER_S", 3600)
    closer.register()
    try:
        priority, fn = offboarding._subscribers[closer.SUBSCRIBER]
        assert priority == 10 and fn is closer.on_offboard
    finally:
        offboarding.unsubscribe(closer.SUBSCRIBER)
        for task in list(closer._background):
            task.cancel()
    tree = ast.parse((PROXY_DIR / "startup.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "register"
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "offboarding_sessions"]
    assert calls, "startup.py must register the offboarding closer"

"""Where a meeting runs, who moderates it, and one start per meeting.

A meeting registers its pump on the host chat, writes its turns there and
inherits the permission mode of that chat's session, so ``create_meeting``
takes the host chat only from a caller who may open and drive it (a
session: its own chat) and reads the session off the chat's row, never the
body. A session moderates as its own agent, and the moderator is always a
participant. The orchestrator takes the meeting from pending to active in
one conditional write, so a second start, or a failure written while the
start awaited its checks, never runs it.

Run: cd proxy && venv/bin/pytest tests/meetings/test_meeting_host_and_start.py -v
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from api.meetings.meetings import CreateMeetingRequest, create_meeting
from auth.providers import UserContext
from core.events.common_events import PRODUCER_DONE
from services.meetings import meeting_orchestrator as MO
from storage import database as task_store
from storage.agents import agent_store
from storage.chat import meeting_status


@pytest.fixture
def agents(temp_db):
    agent_store.create_agent("a1", "A1", collaborative=True)
    agent_store.create_agent("a2", "A2", collaborative=True)
    agent_store.create_agent("a3", "A3", collaborative=True)
    agent_store.create_agent("pool", "Pool", default_scope="agent", collaborative=False)


def _person(sub: str = "u-1", roles: dict[str, str] | None = None) -> UserContext:
    roles = roles or {"a1": "editor", "a2": "editor", "a3": "editor"}
    return UserContext(sub=sub, email=f"{sub}@x.test", name="U", role="member",
                       agents=list(roles), agent_roles=dict(roles))


def _session(sid: str, agent: str = "a1", sub: str = "u-1") -> UserContext:
    roles = {"a1": "editor", "a2": "editor", "a3": "editor"}
    return UserContext(sub=sub, email=f"{sub}@x.test", name="U", role="member",
                       agents=list(roles), agent_roles=dict(roles),
                       is_api_key=True, session_id=sid, agent=agent)


def _chat(chat_id: str, owner: str, agent: str = "a1", session_id: str = "") -> None:
    task_store.create_chat(chat_id, owner, agent)
    if session_id:
        task_store.update_chat(chat_id, session_id=session_id)


def _create(user: UserContext, *, chat: str = "", body_session: str | None = None,
            x_agent_name: str | None = None, agents=("a1", "a2")) -> dict:
    req = CreateMeetingRequest(topic="t", agents=list(agents), scope="user",
                               parent_chat_id=chat, parent_session_id=body_session)
    return asyncio.run(create_meeting(req, user=user, x_agent_name=x_agent_name))


def _refused(fn) -> int:
    with pytest.raises(HTTPException) as ei:
        fn()
    return ei.value.status_code


# ── the host chat ─────────────────────────────────────────────────────────


def test_the_host_session_comes_from_the_chat_row(agents):
    _chat("c-own", "u-1", session_id="s-host")
    out = _create(_person(), chat="c-own", body_session="s-forged", x_agent_name="a1")
    row = task_store.get_meeting(out["meeting_id"])
    assert row["parent_chat_id"] == "c-own" and row["parent_session_id"] == "s-host"


def test_a_chat_the_caller_cannot_open_is_refused(agents):
    _chat("c-other", "u-2", session_id="s-other")
    assert _refused(lambda: _create(_person(), chat="c-other", x_agent_name="a1")) == 403
    assert task_store.get_active_meeting_for_chat("c-other") is None


def test_a_pool_chat_the_caller_may_open_but_not_drive_is_refused(agents):
    from core.session.visibility import shared_chat_owner
    _chat("c-pool", shared_chat_owner("pool"), agent="pool", session_id="s-pool")
    viewer = _person(roles={"a1": "editor", "a2": "editor", "pool": "viewer"})
    assert _refused(lambda: _create(viewer, chat="c-pool", x_agent_name="a1")) == 403
    assert task_store.get_active_meeting_for_chat("c-pool") is None


def test_an_unknown_chat_is_404(agents):
    assert _refused(lambda: _create(_person(), chat="c-missing", x_agent_name="a1")) == 404


def test_a_session_hosts_only_in_its_own_chat(agents):
    _chat("c-mine", "u-1", session_id="s-1")
    _chat("c-theirs", "u-1", session_id="s-2")
    out = _create(_session("s-1"), chat="c-mine", body_session="s-2")
    assert task_store.get_meeting(out["meeting_id"])["parent_session_id"] == "s-1"
    assert _refused(lambda: _create(_session("s-1"), chat="c-theirs")) == 403


def test_without_a_host_chat_a_session_keeps_its_own_session(agents):
    out = _create(_session("s-9"), body_session="s-forged")
    assert task_store.get_meeting(out["meeting_id"])["parent_session_id"] == "s-9"
    out = _create(_person(), body_session="s-forged", x_agent_name="a1",
                  agents=("a1", "a3"))
    assert task_store.get_meeting(out["meeting_id"])["parent_session_id"] is None


# ── the moderator ─────────────────────────────────────────────────────────


def test_a_session_moderates_as_its_own_agent(agents):
    out = _create(_session("s-3", agent="a1"), x_agent_name="a2")
    assert task_store.get_meeting(out["meeting_id"])["moderator"] == "a1"


def test_a_moderator_outside_the_participants_is_refused(agents):
    assert _refused(lambda: _create(_person(), x_agent_name="a3")) == 400
    assert _refused(lambda: _create(_session("s-4", agent="a3"))) == 400


def test_the_create_reads_no_store_on_the_loop(agents, loop_db_guard):
    agent_store.set_delegation_targets("a1", ["a2"])
    nouser = UserContext(sub="session:s-5", email="", name="", role="agent",
                         is_api_key=True, session_id="s-5", agent="a1")
    _chat("c-task", "u-1", session_id="s-5")

    async def scenario():
        req = CreateMeetingRequest(topic="t", agents=["a1", "a2"], scope="agent",
                                   parent_chat_id="c-task")
        with loop_db_guard.active():
            return await create_meeting(req, user=nouser, x_agent_name="a1")

    assert asyncio.run(scenario())["status"] == meeting_status.PENDING


# ── one start per meeting ─────────────────────────────────────────────────


def _pending_meeting(mid: str = "mtg-hs-1") -> str:
    _chat("c-start", "u-1", session_id="s-start")
    task_store.create_meeting(mid, "t", json.dumps(["a1", "a2"]), "a1", "round_robin", 10,
                              "c-start", "s-start", None, "user", "u-1")
    return mid


def _start_stubs(on_check=None):
    """The start's checks pass (``on_check`` runs inside the usage check),
    and the participant build records itself and stops the start."""
    from services.billing import pool_caps, usage_service
    built = AsyncMock(side_effect=RuntimeError("stop after the build"))

    def check(*_a, **_kw):
        if on_check:
            on_check()
        return {"allowed": True}

    return built, (
        patch.object(usage_service, "check_user_limit", side_effect=check),
        patch.object(pool_caps, "evaluate", return_value=SimpleNamespace(allowed=True)),
        patch.object(MO, "build_meeting_agent_config", new=built),
        patch.object(MO, "_notify_meeting_failed", new=AsyncMock()),
    )


def _run_with(patches, coro_factory):
    async def scenario():
        with patches[0], patches[1], patches[2], patches[3]:
            return await coro_factory()
    return asyncio.run(scenario())


def test_a_start_whose_meeting_failed_meanwhile_runs_nothing(agents):
    mid = _pending_meeting()
    built, patches = _start_stubs(
        on_check=lambda: task_store.update_meeting(mid, status=meeting_status.FAILED))
    _run_with(patches, lambda: MO.start_meeting(mid))
    assert built.await_count == 0
    assert task_store.get_meeting(mid)["status"] == meeting_status.FAILED


def test_two_starts_of_one_meeting_run_it_once(agents):
    mid = _pending_meeting()
    built, patches = _start_stubs()
    _run_with(patches, lambda: asyncio.gather(MO.start_meeting(mid), MO.start_meeting(mid)))
    assert built.await_count == 2      # the two participants of one start


# ── the producer ──────────────────────────────────────────────────────────


def test_a_moderator_outside_the_participants_still_ends_the_producer(agents):
    task_store.create_meeting("mtg-hs-2", "t", json.dumps(["a1", "a2"]), "a3", "round_robin",
                              10, "c-x", None, None, "user", "u-1")

    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()
        await MO.meeting_produce("mtg-hs-2", {}, queue,
                                 SimpleNamespace(message_queue=[], system_queue=None))
        return [queue.get_nowait() for _ in range(queue.qsize())]

    events = asyncio.run(asyncio.wait_for(scenario(), 5))
    assert events[-1].type == PRODUCER_DONE

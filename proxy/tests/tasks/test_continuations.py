"""Scheduled self-continuations: POST /v1/continuations + the fire path.

The endpoint targets the CALLING session's chat (token = authority); the fire
path delivers the wake through the session-delivery ladder with self-cancel
on missing chat / until bound / max_runs, and per-chat coalescing.
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user
from services.scheduler import scheduler
from services.scheduler import firing
from services.scheduler.scheduler import TaskDefinition
from storage.agents import agent_store
from storage import database as task_store


AGENT = "pa"
SESSION = "33333333-3333-3333-3333-333333333333"


def _session_user(sub="user-alice", agent=AGENT, sid=SESSION):
    return UserContext(
        sub=sub, email="alice@test.com", name="Alice", role="member",
        is_api_key=True, session_id=sid, agent=agent,
        agents=[agent], agent_roles={agent: "editor"},
    )


@pytest.fixture
def client(temp_db):
    from api.tasks import continuations as continuations_api

    agent_store.create_agent(AGENT, "PA", collaborative=True, default_scope="user")
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, "user-alice", AGENT)
    task_store.update_chat(chat_id, session_id=SESSION)

    app = FastAPI()
    app.include_router(continuations_api.router)
    app.state.user = _session_user()

    async def _current_user():
        return app.state.user

    app.dependency_overrides[get_current_user] = _current_user
    c = TestClient(app)
    c.app_ref = app
    c.chat_id = chat_id
    return c


class TestCreateContinuation:
    def test_one_shot_at(self, client):
        r = client.post("/v1/continuations", json={
            "prompt": "check the lanes", "at": "2099-01-01T10:00:00",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["chat_id"] == client.chat_id
        row = task_store.get_dynamic_task(data["task_id"])
        assert row["task_type"] == "continuation"
        assert row["target_chat_id"] == client.chat_id
        assert row["run_at"] == "2099-01-01T10:00:00"
        assert row["max_runs"] is None          # one-shot: bounds don't apply
        assert row["created_by"] == "user-alice"
        assert row["scope"] == "user"
        assert row["notification_mode"] == "none"

    def test_recurring_defaults_max_runs(self, client):
        r = client.post("/v1/continuations", json={
            "prompt": "poll the build", "repeat_interval_seconds": 600,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["max_runs"] == 5
        row = task_store.get_dynamic_task(data["task_id"])
        assert row["max_runs"] == 5
        assert row["interval_seconds"] == 600

    def test_recurring_until_needs_no_max_runs(self, client):
        until = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0)
        until_iso = until.replace(tzinfo=None).isoformat()
        r = client.post("/v1/continuations", json={
            "prompt": "poll", "repeat_cron": "*/10 * * * *", "until": until_iso,
        })
        assert r.status_code == 200
        row = task_store.get_dynamic_task(r.json()["task_id"])
        assert row["max_runs"] is None
        assert row["until_at"] == until_iso

    def test_a_recurring_until_past_the_ceiling_is_refused(self, client):
        """A recurring wake bounded only by its ``until`` must end within
        the longest interval a task may have (a year): a far ``until`` would
        wake the chat for as long as the process lives."""
        for until in ("2099-01-01T00:00:00", "2099-01-01T00:00:00+00:00"):
            r = client.post("/v1/continuations", json={
                "prompt": "poll", "repeat_interval_seconds": 600, "until": until,
            })
            assert r.status_code == 400 and "until" in r.json()["detail"]
        assert task_store.count_active_continuations(target_chat_id=client.chat_id) == 0
        # With max_runs as well the row is bounded by it: still refused, the
        # until is the caller's statement of when it ends.
        assert client.post("/v1/continuations", json={
            "prompt": "poll", "repeat_interval_seconds": 600, "max_runs": 3,
            "until": "2099-01-01T00:00:00"}).status_code == 400

    def test_timing_validation(self, client):
        assert client.post("/v1/continuations", json={
            "prompt": "p"}).status_code == 400                      # no timing
        assert client.post("/v1/continuations", json={
            "prompt": "p", "at": "2099-01-01T00:00:00",
            "in_seconds": 60}).status_code == 400                   # two timings
        assert client.post("/v1/continuations", json={
            "prompt": "p", "at": "not-a-date"}).status_code == 400
        assert client.post("/v1/continuations", json={
            "prompt": "p", "in_seconds": 5}).status_code == 400     # < 30s
        assert client.post("/v1/continuations", json={
            "prompt": "p", "repeat_interval_seconds": 10}).status_code == 400
        assert client.post("/v1/continuations", json={
            "prompt": "p", "at": "2099-01-01T00:00:00",
            "max_runs": 0}).status_code == 400

    def test_shared_chat_agent_scope(self, client):
        shared_chat = str(uuid.uuid4())
        task_store.create_chat(shared_chat, f"agent::{AGENT}", AGENT)
        sid = str(uuid.uuid4())
        task_store.update_chat(shared_chat, session_id=sid)
        client.app_ref.state.user = _session_user(sid=sid)
        r = client.post("/v1/continuations", json={
            "prompt": "p", "in_seconds": 60,
        })
        assert r.status_code == 200
        row = task_store.get_dynamic_task(r.json()["task_id"])
        assert row["scope"] == "agent"
        assert row["target_chat_id"] == shared_chat


def _cont_task(chat_id, **kw) -> TaskDefinition:
    return TaskDefinition(
        id=kw.pop("id", f"dyn-{uuid.uuid4().hex[:8]}"),
        name="wake", agent=AGENT, prompt=kw.pop("prompt", "wake up"),
        task_type="continuation", target_chat_id=chat_id,
        created_by="user-viewer2", scope="user",
        notification_mode="none", **kw,
    )


class TestContinuationCaps:
    """A chat holds at most CONTINUATION_MAX_ACTIVE_PER_CHAT pending
    self-continuations and an agent CONTINUATION_MAX_ACTIVE_PER_AGENT: a
    third schedule past a cap of two is 429, a paused row does not count
    and its resume is judged like a create."""

    def _schedule(self, client, i: int):
        return client.post("/v1/continuations", json={
            "prompt": f"poll {i}", "in_seconds": 3600 + i})

    def test_a_chat_holds_at_most_its_pending_continuations(self, client, monkeypatch):
        import config
        monkeypatch.setattr(config, "CONTINUATION_MAX_ACTIVE_PER_CHAT", 2)
        monkeypatch.setattr(config, "CONTINUATION_MAX_ACTIVE_PER_AGENT", 0)
        assert self._schedule(client, 1).status_code == 200
        assert self._schedule(client, 2).status_code == 200
        r = self._schedule(client, 3)
        assert r.status_code == 429
        assert "2 pending self-continuations" in r.json()["detail"]
        assert task_store.count_active_continuations(target_chat_id=client.chat_id) == 2

    def test_the_agent_cap_counts_every_chat(self, client, monkeypatch):
        import config
        monkeypatch.setattr(config, "CONTINUATION_MAX_ACTIVE_PER_CHAT", 0)
        monkeypatch.setattr(config, "CONTINUATION_MAX_ACTIVE_PER_AGENT", 2)
        other = str(uuid.uuid4())
        task_store.create_chat(other, "user-bob", AGENT)
        task_store.create_dynamic_task(
            "dyn-other", AGENT, "c", "p", "cli", "continuation", None,
            "2099-01-01T10:00:00", None, 600, "user-bob", scope="user", target_chat_id=other)
        assert self._schedule(client, 1).status_code == 200
        r = self._schedule(client, 2)
        assert r.status_code == 429 and "This agent already has 2" in r.json()["detail"]

    def test_a_resumed_continuation_counts(self, client, monkeypatch):
        import config
        from api.tasks import tasks as tasks_api
        monkeypatch.setattr(config, "CONTINUATION_MAX_ACTIVE_PER_CHAT", 2)
        monkeypatch.setattr(config, "CONTINUATION_MAX_ACTIVE_PER_AGENT", 0)
        first = self._schedule(client, 1).json()["task_id"]
        asyncio.run(scheduler.pause_dynamic_task(first))
        assert self._schedule(client, 2).status_code == 200
        assert self._schedule(client, 3).status_code == 200   # the paused row is free
        with pytest.raises(Exception) as ei:
            asyncio.run(tasks_api.resume_task(first, user=client.app_ref.state.user,
                                              x_agent_name=None))
        assert getattr(ei.value, "status_code", None) == 429
        assert task_store.get_dynamic_task(first)["enabled"] is False


class _Outcome:
    path = "pump"
    response = ""
    chat_id = ""
    session_id = ""


@pytest.fixture
def fire_env(temp_db, monkeypatch):
    """A chat + captured deliver_prompt for _fire_continuation tests. The
    tasks' creator (``_cont_task``) holds the agent: a wake fires only for a
    person who still does."""
    agent_store.create_agent(AGENT, "PA", collaborative=True, default_scope="user")
    task_store.add_user_agent("user-viewer2", AGENT, "editor", "test")
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, "user-alice", AGENT)

    calls: list[dict] = []

    async def _fake_deliver(cid, prompt, **kw):
        calls.append({"chat_id": cid, "prompt": prompt, **kw})
        # Simulate a non-WS rung persisting the wake event at enqueue.
        pe = kw.get("persist_event")
        if pe:
            pe(cid)
        return _Outcome()

    from core.session import session_delivery
    monkeypatch.setattr(session_delivery, "deliver_prompt", _fake_deliver)
    scheduler._continuation_cursors.clear()
    scheduler._continuation_skip_counts.clear()
    yield {"chat_id": chat_id, "calls": calls}
    scheduler._continuation_cursors.clear()
    scheduler._continuation_skip_counts.clear()


def _wake_events(chat_id):
    return [m for m in task_store.get_chat_messages(chat_id)
            if m.get("event_type") == "schedule_wake"]


class TestFireContinuation:
    def _persist(self, task):
        asyncio.run(scheduler.add_dynamic_task(task))

    def test_one_shot_delivers_and_self_deletes(self, fire_env):
        chat_id = fire_env["chat_id"]
        task = _cont_task(chat_id, delay_seconds=60)
        self._persist(task)
        asyncio.run(scheduler._fire_continuation(task))

        assert len(fire_env["calls"]) == 1
        call = fire_env["calls"][0]
        assert call["chat_id"] == chat_id
        assert call["source"] == "schedule_wake"
        assert call["notify_payload"]["type"] == "continuation_prompt"
        assert call["notify_payload"]["chat_id"] == chat_id
        evts = _wake_events(chat_id)
        assert len(evts) == 1
        assert json.loads(evts[0]["event_data"])["task_id"] == task.id
        assert task_store.get_dynamic_task(task.id) is None   # one-shot cleanup

    def test_an_agent_scope_wake_runs_with_its_creators_role(self, fire_env):
        """A shared chat's wake scheduled by a viewer is delivered as a
        viewer, never the manager role an agent-scope wake otherwise takes;
        a wake the agent itself scheduled keeps it."""
        chat_id = fire_env["chat_id"]
        task_store.add_user_agent("user-viewer", AGENT, "viewer", "test")
        by_viewer = _cont_task(chat_id, delay_seconds=60).model_copy(
            update={"scope": "agent", "created_by": "user-viewer"})
        by_agent = _cont_task(chat_id, delay_seconds=60).model_copy(
            update={"scope": "agent", "created_by": AGENT})
        for task in (by_viewer, by_agent):
            self._persist(task)
            asyncio.run(scheduler._fire_continuation(task))
            scheduler._continuation_cursors.clear()
        assert [(c["user_sub"], c["role"]) for c in fire_env["calls"]] == [
            (None, "viewer"), (None, "manager")]

    def test_missing_chat_self_cancels(self, fire_env):
        task = _cont_task("no-such-chat", delay_seconds=60)
        self._persist(task)
        asyncio.run(scheduler._fire_continuation(task))
        assert fire_env["calls"] == []
        assert task_store.get_dynamic_task(task.id) is None

    def test_past_until_self_cancels(self, fire_env):
        chat_id = fire_env["chat_id"]
        task = _cont_task(chat_id, schedule="*/10 * * * *",
                          until_at="2020-01-01T00:00:00+00:00")
        self._persist(task)
        asyncio.run(scheduler._fire_continuation(task))
        assert fire_env["calls"] == []
        assert task_store.get_dynamic_task(task.id) is None

    def test_coalesces_unprocessed_wake(self, fire_env):
        chat_id = fire_env["chat_id"]
        task = _cont_task(chat_id, schedule="*/10 * * * *", max_runs=10)
        self._persist(task)
        asyncio.run(scheduler._fire_continuation(task))
        assert len(fire_env["calls"]) == 1
        # Nothing advanced in the chat since the wake → second fire skips.
        asyncio.run(scheduler._fire_continuation(task))
        assert len(fire_env["calls"]) == 1
        # The chat moves (the wake's turn ran) → next fire delivers again.
        task_store.add_chat_message(chat_id, "assistant", "worked on it")
        asyncio.run(scheduler._fire_continuation(task))
        assert len(fire_env["calls"]) == 2

    def test_max_runs_bound(self, fire_env):
        chat_id = fire_env["chat_id"]
        task = _cont_task(chat_id, schedule="*/10 * * * *", max_runs=2)
        self._persist(task)
        asyncio.run(scheduler._fire_continuation(task))
        assert task_store.get_dynamic_task(task.id) is not None   # 1/2
        task_store.add_chat_message(chat_id, "assistant", "turn ran")
        asyncio.run(scheduler._fire_continuation(task))
        assert task_store.get_dynamic_task(task.id) is None       # 2/2 → gone

    def test_dead_chat_skips_age_toward_max_runs(self, fire_env):
        """A permanently-dead chat (wakes never processed) must not keep a
        max_runs-only continuation alive forever: from the 3rd consecutive
        coalesce-skip onward each skip consumes a run."""
        chat_id = fire_env["chat_id"]
        task = _cont_task(chat_id, schedule="*/10 * * * *", max_runs=4)
        self._persist(task)
        asyncio.run(scheduler._fire_continuation(task))            # delivers, 1/4
        assert len(fire_env["calls"]) == 1

        # Chat never processes the wake: skips 1-2 are free (grace)…
        asyncio.run(scheduler._fire_continuation(task))
        asyncio.run(scheduler._fire_continuation(task))
        assert task_store.get_dynamic_task(task.id)["run_count"] == 1
        # …skip 3 → 2/4, skip 4 → 3/4, skip 5 → 4/4 → self-cancel.
        asyncio.run(scheduler._fire_continuation(task))
        assert task_store.get_dynamic_task(task.id)["run_count"] == 2
        asyncio.run(scheduler._fire_continuation(task))
        assert task_store.get_dynamic_task(task.id)["run_count"] == 3
        asyncio.run(scheduler._fire_continuation(task))
        assert task_store.get_dynamic_task(task.id) is None
        assert len(fire_env["calls"]) == 1, "no extra deliveries while dead"

    def test_delivery_resets_skip_grace(self, fire_env):
        """A live-but-slow chat: two skips, then the chat moves and the wake
        delivers — the grace window restarts, so the next skip is free again."""
        chat_id = fire_env["chat_id"]
        task = _cont_task(chat_id, schedule="*/10 * * * *", max_runs=4)
        self._persist(task)
        asyncio.run(scheduler._fire_continuation(task))            # 1/4
        asyncio.run(scheduler._fire_continuation(task))            # skip ×1
        asyncio.run(scheduler._fire_continuation(task))            # skip ×2
        task_store.add_chat_message(chat_id, "assistant", "slow turn finished")
        asyncio.run(scheduler._fire_continuation(task))            # delivers, 2/4
        assert len(fire_env["calls"]) == 2
        asyncio.run(scheduler._fire_continuation(task))            # skip ×1 again
        assert task_store.get_dynamic_task(task.id)["run_count"] == 2, \
            "post-delivery skip is back inside the grace window"

    def test_execute_task_routes_continuations(self, fire_env, monkeypatch):
        seen = []

        async def _fake_fire(task):
            seen.append(task.id)
            return ""

        monkeypatch.setattr(firing, "_fire_continuation", _fake_fire)
        task = _cont_task(fire_env["chat_id"], delay_seconds=60, id="dyn-route1")
        asyncio.run(scheduler._execute_task(task))
        assert seen == ["dyn-route1"]


class TestChatDeleteCancelsContinuations:
    def test_delete_chat_removes_pending_rows(self, temp_db):
        from api.agents import chats as chats_api

        agent_store.create_agent(AGENT, "PA", collaborative=True, default_scope="user")
        chat_id = str(uuid.uuid4())
        task_store.create_chat(chat_id, "user-alice", AGENT)
        task = _cont_task(chat_id, schedule="*/10 * * * *", max_runs=5)
        asyncio.run(scheduler.add_dynamic_task(task))
        assert task_store.list_continuations_for_chat(chat_id)

        app = FastAPI()
        app.include_router(chats_api.router)

        async def _current_user():
            return _session_user()

        app.dependency_overrides[get_current_user] = _current_user
        c = TestClient(app)
        r = c.delete(f"/v1/chats/{chat_id}")
        assert r.status_code == 200
        assert task_store.list_continuations_for_chat(chat_id) == []
        assert task_store.get_dynamic_task(task.id) is None
        assert task_store.get_chat(chat_id) is None


class TestUndeliverableWake:
    """The twin of the delegate rung: a wake that every rung refused (a
    parked reservation that timed out, or gave way to a person) is kept on
    the chat for its next turn, unless the person who scheduled it no longer
    holds the agent."""

    class _None(_Outcome):
        path = "none"

    def _fire(self, monkeypatch, chat_id, task):
        from core.session import session_delivery

        async def _fake(cid, prompt, **kw):
            # The ladder runs the outcome hook on every path, "none" included.
            out = self._None()
            await kw["on_outcome"](out)
            return out
        monkeypatch.setattr(session_delivery, "deliver_prompt", _fake)
        asyncio.run(scheduler.add_dynamic_task(task))
        asyncio.run(scheduler._fire_continuation(task))

    def test_stored_for_the_chats_next_turn(self, fire_env, monkeypatch):
        chat_id = fire_env["chat_id"]
        task_store.add_user_agent("user-viewer", AGENT, "editor", "test")
        task = _cont_task(chat_id, delay_seconds=60, prompt="carry on").model_copy(
            update={"created_by": "user-viewer"})
        self._fire(monkeypatch, chat_id, task)
        assert task_store.claim_pending_delegate_wake(chat_id) == ["carry on"]

    def test_not_stored_for_a_person_without_the_agent(self, fire_env, monkeypatch):
        chat_id = fire_env["chat_id"]
        task = _cont_task(chat_id, delay_seconds=60, prompt="carry on").model_copy(
            update={"created_by": "user-alice"})
        self._fire(monkeypatch, chat_id, task)
        assert task_store.claim_pending_delegate_wake(chat_id) == []

    def test_not_delivered_for_a_person_without_the_agent(self, fire_env, monkeypatch):
        # The standing gate runs before the ladder, not only before the store:
        # no rung is tried for a person who no longer holds the agent.
        from core.session import session_delivery
        chat_id = fire_env["chat_id"]
        task = _cont_task(chat_id, delay_seconds=60, prompt="carry on").model_copy(
            update={"created_by": "user-alice"})
        delivered: list = []

        async def _record(cid, prompt, **kw):
            # (the fire swallows a ladder exception, so record instead of raising)
            delivered.append(cid)
            return self._None()
        monkeypatch.setattr(session_delivery, "deliver_prompt", _record)
        asyncio.run(scheduler.add_dynamic_task(task))
        assert asyncio.run(scheduler._fire_continuation(task)) == ""
        assert delivered == []
        assert task_store.claim_pending_delegate_wake(chat_id) == []
        # The refused fire still ends the row the way a delivered one does.
        assert task_store.get_dynamic_task(task.id) is None

    def test_a_refused_recurring_wake_still_counts_toward_max_runs(self, fire_env, monkeypatch):
        from core.session import session_delivery
        chat_id = fire_env["chat_id"]
        task = _cont_task(chat_id, schedule="*/10 * * * *", max_runs=2).model_copy(
            update={"created_by": "user-alice"})

        async def _record(cid, prompt, **kw):
            raise AssertionError("a wake was delivered for a person without the agent")
        monkeypatch.setattr(session_delivery, "deliver_prompt", _record)
        asyncio.run(scheduler.add_dynamic_task(task))
        asyncio.run(scheduler._fire_continuation(task))
        assert task_store.get_dynamic_task(task.id) is not None
        asyncio.run(scheduler._fire_continuation(task))
        assert task_store.get_dynamic_task(task.id) is None

    def test_a_delivered_wake_is_not_stored(self, fire_env):
        chat_id = fire_env["chat_id"]
        task_store.add_user_agent("user-viewer", AGENT, "editor", "test")
        task = _cont_task(chat_id, delay_seconds=60).model_copy(
            update={"created_by": "user-viewer"})
        asyncio.run(scheduler.add_dynamic_task(task))
        asyncio.run(scheduler._fire_continuation(task))
        assert task_store.claim_pending_delegate_wake(chat_id) == []


class TestSharedChatContinuationIdentity:
    """A continuation wake into a Shared-only chat runs as the
    person who scheduled it, at their current standing; one who lost the
    agent wakes nothing and the row ends; no person on record, a phone chat
    and a task chat keep today's identity."""

    def _chat(self, owner: str, agent: str = "so") -> str:
        chat_id = str(uuid.uuid4())
        task_store.create_chat(chat_id, owner, agent)
        return chat_id

    def _fire(self, chat_id: str, created_by: str, agent: str = "so") -> TaskDefinition:
        task = _cont_task(chat_id, delay_seconds=60).model_copy(
            update={"agent": agent, "scope": "agent", "created_by": created_by})
        asyncio.run(scheduler.add_dynamic_task(task))
        asyncio.run(scheduler._fire_continuation(task))
        scheduler._continuation_cursors.clear()
        return task

    def test_the_creator_at_their_standing(self, fire_env):
        agent_store.create_agent("so", "SO", collaborative=False, default_scope="agent")
        task_store.add_user_agent("user-viewer2", "so", "editor", "test")
        chat_id = self._chat("agent::so")
        self._fire(chat_id, "user-viewer2")
        assert [(c["user_sub"], c["role"]) for c in fire_env["calls"]] == [
            ("user-viewer2", "editor")]

    def test_a_creator_without_the_agent_wakes_nothing(self, fire_env):
        agent_store.create_agent("so", "SO", collaborative=False, default_scope="agent")
        chat_id = self._chat("agent::so")
        task = self._fire(chat_id, "user-gone")
        assert fire_env["calls"] == []
        assert task_store.get_dynamic_task(task.id) is None
        assert task_store.claim_pending_delegate_wake(chat_id) == []

    def test_below_the_editor_tier_counts_as_gone(self, fire_env):
        agent_store.create_agent("so", "SO", collaborative=False, default_scope="agent")
        task_store.add_user_agent("user-viewer2", "so", "contributor", "test")
        chat_id = self._chat("agent::so")
        task = self._fire(chat_id, "user-viewer2")
        assert fire_env["calls"] == []
        assert task_store.get_dynamic_task(task.id) is None

    def test_no_person_and_other_chats_keep_todays_identity(self, fire_env):
        agent_store.create_agent("so", "SO", collaborative=False, default_scope="agent")
        task_store.add_user_agent("user-viewer2", "so", "editor", "test")
        self._fire(self._chat("agent::so"), "so")
        self._fire(self._chat("phone"), "user-viewer2")
        self._fire(self._chat("task::so"), "user-viewer2")
        assert [(c["user_sub"], c["role"]) for c in fire_env["calls"]] == [
            (None, "manager"), (None, "editor"), (None, "editor")]

"""POST /v1/delegation/spawn — the atomic worker-spawn endpoint.

Authorization cells live in test_spawn_authz.py; this file covers the
endpoint's own work: worker-chat creation, callback registration BEFORE the
fire, continue_id resolution per surface, project stamping, and the
delegate_spawn badge fallback row.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user
from services.scheduler import scheduler
from storage.agents import agent_store
from storage.mcp import mcp_store
from storage import database as task_store


AGENT = "pa"
PARENT_SESSION = "11111111-1111-1111-1111-111111111111"


def _session_user(sub="user-alice", agent=AGENT, sid=PARENT_SESSION):
    return UserContext(
        sub=sub, email="alice@test.com", name="Alice", role="member",
        is_api_key=True, session_id=sid, agent=agent,
        agents=[agent], agent_roles={agent: "editor"},
    )


def _cookie_user(sub="user-alice"):
    return UserContext(
        sub=sub, email="alice@test.com", name="Alice", role="member",
        agents=[AGENT], agent_roles={AGENT: "editor"},
    )


@pytest.fixture
def client(temp_db, monkeypatch):
    """App with the delegation router, a parent chat bound to the caller's
    session, and the actual fire stubbed out."""
    from api.tasks import delegation as delegation_api

    agent_store.create_agent(AGENT, "PA", collaborative=True, default_scope="user")
    mcp_store.set_mcp_enabled("delegation-mcp", True)

    parent_chat_id = str(uuid.uuid4())
    task_store.create_chat(parent_chat_id, "user-alice", AGENT)
    task_store.update_chat(parent_chat_id, session_id=PARENT_SESSION)

    fired: list[dict] = []

    async def _fake_trigger(task, trigger_type="manual", trigger_source=None,
                            prompt_override=None, trigger_payload=None):
        fired.append({"task": task, "trigger_source": trigger_source})
        return "run-test1234"

    monkeypatch.setattr(scheduler, "trigger_task_now", _fake_trigger)

    app = FastAPI()
    app.include_router(delegation_api.router)
    app.state.user = _session_user()

    async def _current_user():
        return app.state.user

    app.dependency_overrides[get_current_user] = _current_user
    c = TestClient(app)
    c.app_ref = app
    c.parent_chat_id = parent_chat_id
    c.fired = fired
    return c


def _spawn(client, **overrides):
    body = {
        "name": "Lane 1", "prompt": "do the work", "surface": "task",
        "agent": AGENT, "source_agent": AGENT, "scope": "user",
    }
    body.update(overrides)
    return client.post("/v1/delegation/spawn", json=body)


class TestTaskSurface:
    def test_spawn_registers_callback_before_fire(self, client):
        r = _spawn(client)
        assert r.status_code == 200
        data = r.json()
        assert data["run_id"] == "run-test1234"
        assert data["chat_id"] is None
        row = task_store.get_dynamic_task(data["task_id"])
        assert row["use_persistent"] is True
        assert row["task_type"] == "delegate"   # explicit marker, not derived
        assert row["notification_mode"] == "none"
        assert row["on_complete_agent"] == AGENT
        assert row["on_complete_session_id"] == PARENT_SESSION
        assert row["on_complete_chat_id"] == client.parent_chat_id
        assert row["target_chat_id"] is None
        assert client.fired and client.fired[0]["task"].id == data["task_id"]

    def test_continue_unknown_task_404(self, client):
        r = _spawn(client, continue_id="dyn-gone")
        assert r.status_code == 404

    def test_continue_by_task_id_reuses_run_chat(self, client):
        # A finished task-surface run: chat task-run-old1 + a completed run
        # whose session links them (delegate task rows auto-clean, so the
        # task-id form resolves through the run's session to the chat).
        prior_sid = str(uuid.uuid4())
        task_store.create_run("run-old1", "dyn-old1", AGENT, "manual", None,
                              "old", task_type="delegate")
        task_store.update_run("run-old1", status="completed",
                              session_id=prior_sid, chat_id="task-run-old1")
        task_store.create_chat("task-run-old1", "user-alice", AGENT, "auto",
                               origin="delegated", title="old lane")
        task_store.update_chat("task-run-old1", session_id=prior_sid)
        r = _spawn(client, continue_id="dyn-old1")
        assert r.status_code == 200
        data = r.json()
        # The continuation appends to the SAME chat — no fresh task-run chat.
        assert data["chat_id"] == "task-run-old1"
        row = task_store.get_dynamic_task(data["task_id"])
        assert row["continue_session"] == prior_sid
        assert row["target_chat_id"] == "task-run-old1"

    def test_continue_by_task_run_chat_id(self, client):
        prior_sid = str(uuid.uuid4())
        task_store.create_chat("task-run-old2", "user-alice", AGENT, "auto",
                               origin="delegated", title="old lane")
        task_store.update_chat("task-run-old2", session_id=prior_sid)
        r = _spawn(client, continue_id="task-run-old2")
        assert r.status_code == 200
        data = r.json()
        assert data["chat_id"] == "task-run-old2"
        assert data["agent"] == AGENT
        row = task_store.get_dynamic_task(data["task_id"])
        assert row["continue_session"] == prior_sid
        assert row["target_chat_id"] == "task-run-old2"

    def test_continue_onto_a_working_lane_steers(self, client, monkeypatch):
        # The worker is mid-turn on THIS caller's delegate run: the follow-up
        # goes INTO that turn — no second task row, no run fired, the response
        # names the running run and the row wears the delegating badge.
        from core.events import stream_pump
        from core.session import session_delivery
        from services.scheduler import lane_steer

        prior_sid = str(uuid.uuid4())
        task_store.create_chat("task-run-live1", "user-alice", AGENT, "auto",
                               origin="delegated", title="live lane")
        task_store.update_chat("task-run-live1", session_id=prior_sid)
        task_store.create_dynamic_task(
            "dyn-live1", AGENT, "live lane", "do the work", "cli", "delegate",
            None, None, None, 3600, "user-alice",
            on_complete_agent=AGENT, on_complete_prompt="report",
            on_complete_session_id=PARENT_SESSION,
            on_complete_chat_id=client.parent_chat_id,
        )
        task_store.create_run("run-live1", "dyn-live1", AGENT, "manual", None,
                              "do the work", task_type="delegate")
        task_store.update_run("run-live1", status="running",
                              session_id=prior_sid, chat_id="task-run-live1")

        class _Pump:
            session_id = prior_sid
            is_done = False
            source_type = "task"

        class _Caps:
            class behaviour:
                supports_steer = True

        steers: list = []

        class _Layer:
            def capabilities_for(self, sid):
                return _Caps()

            async def steer(self, sid, text):
                steers.append((sid, text))
                return True

        async def _chat_layer(chat):
            return _Layer()
        monkeypatch.setattr(session_delivery, "chat_layer", _chat_layer)
        pushed: list = []
        monkeypatch.setattr(lane_steer, "_running_delegate_run",
                            lambda cid: task_store.get_run("run-live1"))
        from core.session import session_state
        monkeypatch.setattr(session_state, "push_pump_event",
                            lambda cid, ev: pushed.append((cid, ev)) or True)
        stream_pump._active_pumps["task-run-live1"] = _Pump()
        try:
            r = _spawn(client, continue_id="task-run-live1", prompt="also X")
        finally:
            stream_pump._active_pumps.pop("task-run-live1", None)
        assert r.status_code == 200
        data = r.json()
        assert data["steered"] is True
        assert data["run_id"] == "run-live1"
        assert data["task_id"] == "dyn-live1"
        assert data["chat_id"] == "task-run-live1"
        assert steers == [(prior_sid, "also X")]
        assert client.fired == []              # no second run
        assert task_store.list_dynamic_tasks(AGENT) == [
            task_store.get_dynamic_task("dyn-live1")]
        rows = task_store.get_chat_messages("task-run-live1")
        assert [(m["role"], m["content"]) for m in rows] == [("user", "also X")]
        import json as _json
        assert _json.loads(rows[0]["event_data"])["badge"] == "delegated by"
        assert pushed and pushed[0][1]["type"] == "steered"
        assert pushed[0][1]["text"] == "also X"

    def test_continue_onto_a_lane_working_for_another_caller_queues(
            self, client, monkeypatch):
        # The running run reports to ANOTHER chat: this caller's follow-up
        # would vanish into that report, so it queues a run as before. A
        # refused steer and a human's own turn on the worker take the same
        # path.
        from core.events import stream_pump
        from core.session import session_delivery

        prior_sid = str(uuid.uuid4())
        task_store.create_chat("task-run-live2", "user-alice", AGENT, "auto",
                               origin="delegated", title="live lane")
        task_store.update_chat("task-run-live2", session_id=prior_sid)
        task_store.create_dynamic_task(
            "dyn-live2", AGENT, "live lane", "do the work", "cli", "delegate",
            None, None, None, 3600, "user-alice",
            on_complete_agent=AGENT, on_complete_prompt="report",
            on_complete_session_id="other-session",
            on_complete_chat_id="other-chat",
        )
        task_store.create_run("run-live2", "dyn-live2", AGENT, "manual", None,
                              "do the work", task_type="delegate")
        task_store.update_run("run-live2", status="running",
                              session_id=prior_sid, chat_id="task-run-live2")
        steers: list = []

        class _Caps:
            class behaviour:
                supports_steer = True

        class _Layer:
            def capabilities_for(self, sid):
                return _Caps()

            async def steer(self, sid, text):
                steers.append(text)
                return True

        async def _chat_layer(chat):
            return _Layer()
        monkeypatch.setattr(session_delivery, "chat_layer", _chat_layer)

        class _Pump:
            session_id = prior_sid
            is_done = False
            source_type = "task"
        stream_pump._active_pumps["task-run-live2"] = _Pump()
        try:
            r = _spawn(client, continue_id="task-run-live2", prompt="also Y")
            assert r.status_code == 200
            assert "steered" not in r.json()
            assert steers == []
            assert len(client.fired) == 1

            # A person's own dashboard turn on the worker chat: queued too.
            _Pump.source_type = "chat"
            task_store.update_dynamic_task_on_complete(
                "dyn-live2", AGENT, "report", PARENT_SESSION, client.parent_chat_id)
            r = _spawn(client, continue_id="task-run-live2", prompt="also Z")
            assert r.status_code == 200
            assert "steered" not in r.json()
            assert steers == []
            assert len(client.fired) == 2
        finally:
            stream_pump._active_pumps.pop("task-run-live2", None)

    def test_continue_derives_agent_from_worker(self, client):
        # Continued worker lives on ANOTHER agent: the spawn runs there even
        # when the caller omits `agent` (the old behavior defaulted to the
        # caller's agent and --resume answered "No conversation found").
        agent_store.create_agent("sa", "SA", collaborative=True,
                                 default_scope="user")
        agent_store.set_delegation_targets(AGENT, ["sa"])
        caller = _session_user()
        caller.agents.append("sa")
        caller.agent_roles["sa"] = "editor"
        client.app_ref.state.user = caller
        prior_sid = str(uuid.uuid4())
        task_store.create_chat("task-run-sa1", "user-alice", "sa", "auto",
                               origin="delegated", title="probe")
        task_store.update_chat("task-run-sa1", session_id=prior_sid)
        r = _spawn(client, continue_id="task-run-sa1", agent="")
        assert r.status_code == 200
        data = r.json()
        assert data["agent"] == "sa"
        assert client.fired and client.fired[-1]["task"].agent == "sa"
        # An explicit mismatching agent is rejected, not silently rerouted.
        r2 = _spawn(client, continue_id="task-run-sa1", agent=AGENT)
        assert r2.status_code == 400

    def test_fresh_spawn_requires_agent(self, client):
        r = _spawn(client, agent="")
        assert r.status_code == 400

    def test_task_surface_lineage_rides_the_task(self, client):
        r = _spawn(client, project_id="slate")
        assert r.status_code == 200
        task = client.fired[-1]["task"]
        assert task.parent_chat_id == client.parent_chat_id
        assert task.project_id == "slate"


class TestChatSurface:
    def test_spawn_creates_worker_chat(self, client):
        r = _spawn(client, surface="chat", project_id="site-redesign")
        assert r.status_code == 200
        data = r.json()
        worker = task_store.get_chat(data["chat_id"])
        assert worker["user_sub"] == "user-alice"
        assert worker["origin"] == "delegated"
        assert worker["parent_chat_id"] == client.parent_chat_id
        assert worker["project_id"] == "site-redesign"
        assert worker["delegate_role"] == "worker"
        assert worker["title"] == "Lane 1"
        row = task_store.get_dynamic_task(data["task_id"])
        assert row["target_chat_id"] == data["chat_id"]
        # First project delegation stamps the parent as orchestrator.
        parent = task_store.get_chat(client.parent_chat_id)
        assert parent["project_id"] == "site-redesign"
        assert parent["delegate_role"] == "orchestrator"

    def test_plain_delegation_stamps_orchestrator_without_project(self, client):
        # Delegating at all makes the parent an orchestrator (the dock and the
        # sidebar accent key on the role); project_id only rides along when
        # one was passed.
        r = _spawn(client, surface="chat")
        assert r.status_code == 200
        parent = task_store.get_chat(client.parent_chat_id)
        assert parent["project_id"] == ""
        assert parent["delegate_role"] == "orchestrator"

    def test_continue_reuses_worker_chat(self, client):
        first = _spawn(client, surface="chat").json()
        worker_sid = str(uuid.uuid4())
        task_store.update_chat(first["chat_id"], session_id=worker_sid)
        second = _spawn(client, surface="chat", continue_id=first["chat_id"]).json()
        assert second["chat_id"] == first["chat_id"]
        row = task_store.get_dynamic_task(second["task_id"])
        assert row["target_chat_id"] == first["chat_id"]
        assert row["continue_session"] == worker_sid

    def test_continue_foreign_chat_403(self, client):
        foreign = str(uuid.uuid4())
        task_store.create_chat(foreign, "user-bob", AGENT)
        r = _spawn(client, surface="chat", continue_id=foreign)
        assert r.status_code == 403

    def test_a_caller_with_no_chat_cannot_continue_a_foreign_chat(self, client):
        # A cookie caller has no parent chat: its empty parent must not
        # match every chat that is nobody's worker.
        client.app_ref.state.user = _cookie_user()
        foreign = str(uuid.uuid4())
        task_store.create_chat(foreign, "user-bob", AGENT)
        r = _spawn(client, surface="chat", continue_id=foreign)
        assert r.status_code == 403
        own = str(uuid.uuid4())
        task_store.create_chat(own, "user-alice", AGENT)
        assert _spawn(client, surface="chat", continue_id=own).status_code == 200

    def test_continue_unknown_chat_404(self, client):
        r = _spawn(client, surface="chat", continue_id=str(uuid.uuid4()))
        assert r.status_code == 404

    def test_delegate_spawn_event_row_fallback(self, client):
        # No live pump in tests → the badge lands as a persisted event row
        # on the delegating chat, carrying the worker chat id.
        r = _spawn(client, surface="chat")
        msgs = task_store.get_chat_messages(client.parent_chat_id)
        events = [m for m in msgs if m.get("event_type") == "delegate_spawn"]
        assert len(events) == 1
        import json as _json
        data = _json.loads(events[0]["event_data"])
        assert data["chat_id"] == r.json()["chat_id"]
        assert data["surface"] == "chat"
        assert data["task_id"] == r.json()["task_id"]
        # Same shape as the pump-persisted block: the dashboard's history
        # reload keys blocks on event_data["type"], not the event_type column.
        assert data["type"] == "delegate_spawn"


class TestCallerVariants:
    def test_cookie_caller_gets_no_callback(self, client):
        client.app_ref.state.user = _cookie_user()
        r = _spawn(client)
        assert r.status_code == 200
        row = task_store.get_dynamic_task(r.json()["task_id"])
        assert row["on_complete_agent"] is None
        assert row["on_complete_session_id"] is None

    def test_invalid_project_id_400(self, client):
        r = _spawn(client, surface="chat", project_id="Not A Slug!")
        assert r.status_code == 400

    def test_kill_switch_denial_passes_through(self, client):
        mcp_store.set_mcp_enabled("delegation-mcp", False)
        r = _spawn(client)
        assert r.status_code == 403
        # And nothing was created or fired.
        assert not client.fired

    def test_add_dynamic_task_row_roundtrip(self, client):
        """Regression: positional args to create_dynamic_task shifted
        continue_session into on_complete_chat_id and use_persistent into
        continue_session (bool-as-string rows the _is_valid_session_uuid
        guard then had to paper over)."""
        import asyncio
        sid = str(uuid.uuid4())
        task = scheduler.TaskDefinition(
            id="dyn-roundtrip", name="t", agent=AGENT, prompt="p",
            continue_session=sid, use_persistent=True,
            on_complete_session_id="s-parent",
        )
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            scheduler.add_dynamic_task(task)
        )
        row = task_store.get_dynamic_task("dyn-roundtrip")
        assert row["continue_session"] == sid
        assert row["use_persistent"] is True
        assert row["on_complete_chat_id"] is None

    def test_scope_note_surfaces_clamp(self, client):
        agent_store.create_agent("shared-only", "SO", collaborative=False,
                                 default_scope="agent")
        agent_store.set_delegation_targets(AGENT, ["shared-only"])
        caller = _session_user()
        caller.agents.append("shared-only")
        caller.agent_roles["shared-only"] = "editor"
        client.app_ref.state.user = caller
        r = _spawn(client, agent="shared-only", scope="user")
        assert r.status_code == 200
        data = r.json()
        assert data["scope"] == "agent"
        assert "clamped" in (data["scope_note"] or "")


class TestSpawnOverrides:
    """Per-lane execution overrides (model/layer/mode): validated against the
    agent's envelope, carried in-memory on the TaskDefinition, pinned on the
    worker chat row, and IGNORED on continue (the worker keeps its config)."""

    def test_overrides_ride_the_task_and_chat_pins(self, client, monkeypatch):
        from storage.billing import subscription_store
        monkeypatch.setattr(subscription_store, "list_models",
                            lambda p: [{"model_id": "claude-opus-5", "enabled": True}])
        r = _spawn(client, surface="chat", model="claude-opus-5",
                   layer="claude-code-cli", mode="interactive")
        assert r.status_code == 200
        task = client.fired[0]["task"]
        assert task.override_model == "claude-opus-5"
        assert task.override_execution_path == "claude-code-cli"
        assert task.override_execution_mode == "interactive"
        chat = task_store.get_chat(r.json()["chat_id"])
        assert chat["model"] == "claude-opus-5"
        assert chat["execution_path"] == "claude-code-cli"
        assert chat["execution_mode"] == "interactive"

    def test_inherit_when_omitted(self, client):
        r = _spawn(client)
        assert r.status_code == 200
        task = client.fired[0]["task"]
        assert task.override_model is None
        assert task.override_execution_path is None
        assert task.override_execution_mode is None

    def test_layer_outside_agent_envelope_400(self, client):
        r = _spawn(client, layer="codex-cli")
        assert r.status_code == 400
        assert "not enabled" in r.json()["detail"]
        assert client.fired == []

    def test_model_foreign_to_layer_400(self, client, monkeypatch):
        from storage.billing import subscription_store
        monkeypatch.setattr(subscription_store, "list_models",
                            lambda p: [{"model_id": "other-model", "enabled": True}])
        r = _spawn(client, model="gpt-5.4")
        assert r.status_code == 400
        assert client.fired == []

    def test_interactive_with_task_surface_allowed(self, client):
        # An interactive-default agent's task-surface delegate is interactive
        # today via inheritance — the explicit override is coherent with that.
        r = _spawn(client, mode="interactive")
        assert r.status_code == 200
        assert client.fired[0]["task"].override_execution_mode == "interactive"

    def test_continue_ignores_overrides(self, client):
        prior_sid = str(uuid.uuid4())
        task_store.create_run("run-old2", "dyn-old2", AGENT, "manual", None,
                              "old", task_type="delegate")
        task_store.update_run("run-old2", status="completed",
                              session_id=prior_sid, chat_id="task-run-old2")
        task_store.create_chat("task-run-old2", "user-alice", AGENT, "auto",
                               origin="delegated", title="old lane")
        task_store.update_chat("task-run-old2", session_id=prior_sid)
        r = _spawn(client, continue_id="dyn-old2", model="whatever",
                   layer="codex-cli", mode="interactive")
        assert r.status_code == 200          # invalid layer never validated
        task = client.fired[0]["task"]
        # The unpinned continued chat yields no overrides either way.
        assert task.override_model is None
        assert task.override_execution_path is None
        assert task.override_execution_mode is None

    def test_chat_continue_repins_worker_config(self, client):
        worker = str(uuid.uuid4())
        task_store.create_chat(worker, "user-alice", AGENT,
                               origin="delegated",
                               parent_chat_id=client.parent_chat_id,
                               delegate_role="worker",
                               model="claude-opus-5",
                               execution_path="claude-code-cli",
                               execution_mode="interactive")
        task_store.update_chat(worker, session_id=str(uuid.uuid4()))
        r = _spawn(client, surface="chat", continue_id=worker)
        assert r.status_code == 200
        task = client.fired[0]["task"]
        assert task.override_model == "claude-opus-5"
        assert task.override_execution_path == "claude-code-cli"
        assert task.override_execution_mode == "interactive"


class TestAdoptProject:
    """POST /v1/delegation/adopt — takeover stamping for board/handoff pickups."""

    def _mk_project(self, project_id="proj-x", owner="user-alice"):
        lane = str(uuid.uuid4())
        task_store.create_chat(lane, owner, AGENT, "auto", origin="delegated",
                               project_id=project_id, delegate_role="worker",
                               title="old lane")
        return lane

    def test_adopt_stamps_caller_chat_and_broadcasts(self, client, monkeypatch):
        from core.session import session_state
        frames: list[tuple[str, dict]] = []
        monkeypatch.setattr(session_state, "broadcast_chat_frame",
                            lambda cid, frame: frames.append((cid, frame)))
        lane = self._mk_project()
        r = client.post("/v1/delegation/adopt", json={"project_id": "proj-x"})
        assert r.status_code == 200
        data = r.json()
        assert data["chat_id"] == client.parent_chat_id
        assert [row["id"] for row in data["lanes"]] == [lane]
        chat = task_store.get_chat(client.parent_chat_id)
        assert chat["delegate_role"] == "orchestrator"
        assert chat["project_id"] == "proj-x"
        assert frames == [(client.parent_chat_id,
                           {"type": "chat_meta",
                            "chat_id": client.parent_chat_id,
                            "delegate_role": "orchestrator",
                            "project_id": "proj-x"})]

    def test_adopt_is_idempotent(self, client):
        self._mk_project()
        assert client.post("/v1/delegation/adopt",
                           json={"project_id": "proj-x"}).status_code == 200
        assert client.post("/v1/delegation/adopt",
                           json={"project_id": "proj-x"}).status_code == 200

    def test_adopt_unknown_project_404(self, client):
        r = client.post("/v1/delegation/adopt", json={"project_id": "nope"})
        assert r.status_code == 404

    def test_adopt_foreign_project_404(self, client):
        # Same slug, different owner pool — invisible, so not adoptable.
        self._mk_project(owner="user-bob")
        r = client.post("/v1/delegation/adopt", json={"project_id": "proj-x"})
        assert r.status_code == 404

    def test_adopt_needs_session_bound_chat(self, client):
        self._mk_project()
        client.app_ref.state.user = _cookie_user()
        r = client.post("/v1/delegation/adopt", json={"project_id": "proj-x"})
        assert r.status_code == 400

    def test_adopt_rejects_bad_slug(self, client):
        r = client.post("/v1/delegation/adopt", json={"project_id": "Not A Slug!"})
        assert r.status_code == 400


class _LiveTerminal:
    """A live interactive session on a lane chat, as find_live_for_chat sees
    it, recording the prompts queued on it."""

    def __init__(self, chat_id: str, session_id: str):
        self.chat_id = chat_id
        self.session_id = session_id
        self.alive = True
        self.created_at = 1.0
        self.queued: list[dict] = []

    def queue_prompt(self, text, source, **context):
        item = {"text": text, "source": source, **context}
        self.queued.append(item)
        return item


class TestContinueOntoALiveTerminal:
    """No pump, a live interactive terminal on the lane: a follow-up onto a
    round working for THIS caller goes into the terminal (that round's
    report covers it, no second run); for another caller a run queues."""

    def _lane(self, client, chat_id: str, run_id: str, *, callback_chat: str):
        sid = str(uuid.uuid4())
        task_store.create_chat(chat_id, "user-alice", AGENT, "auto",
                               origin="delegated", title="terminal lane",
                               execution_mode="interactive")
        task_store.update_chat(chat_id, session_id=sid)
        task_store.create_dynamic_task(
            f"dyn-{run_id}", AGENT, "terminal lane", "do the work", "cli", "delegate",
            None, None, None, 3600, "user-alice",
            on_complete_agent=AGENT, on_complete_prompt="report",
            on_complete_session_id=PARENT_SESSION,
            on_complete_chat_id=callback_chat,
        )
        task_store.create_run(run_id, f"dyn-{run_id}", AGENT, "manual", None,
                              "do the work", task_type="delegate")
        task_store.update_run(run_id, status="running", session_id=sid, chat_id=chat_id)
        return _LiveTerminal(chat_id, sid)

    def test_a_working_round_for_this_caller_takes_the_prompt(self, client, monkeypatch):
        from core.session import interactive_session
        from services.scheduler import lane_steer
        term = self._lane(client, "task-run-term1", "run-term1",
                          callback_chat=client.parent_chat_id)
        monkeypatch.setitem(interactive_session._sessions, term.session_id, term)
        monkeypatch.setattr(lane_steer, "_running_delegate_run",
                            lambda cid: task_store.get_run("run-term1"))
        r = _spawn(client, continue_id="task-run-term1", prompt="also T")
        assert r.status_code == 200
        data = r.json()
        assert data["steered"] is True and data["run_id"] == "run-term1"
        assert client.fired == []                           # no second run
        assert [(q["text"], q["source"], q["steer"]) for q in term.queued] == [
            ("also T", "delegate_continue", True)]
        assert term.queued[0]["chat_id"] == "task-run-term1"
        # The transcript tailer owns the terminal's rows: nothing persisted here.
        assert task_store.get_chat_messages("task-run-term1") == []

    def test_a_round_for_another_caller_queues_a_run(self, client, monkeypatch):
        from core.session import interactive_session
        from services.scheduler import lane_steer
        term = self._lane(client, "task-run-term2", "run-term2", callback_chat="other-chat")
        monkeypatch.setitem(interactive_session._sessions, term.session_id, term)
        monkeypatch.setattr(lane_steer, "_running_delegate_run",
                            lambda cid: task_store.get_run("run-term2"))
        r = _spawn(client, continue_id="task-run-term2", prompt="also U")
        assert r.status_code == 200
        assert "steered" not in r.json()
        assert term.queued == [] and len(client.fired) == 1

    def test_a_dead_terminal_queues_a_run(self, client, monkeypatch):
        from core.session import interactive_session
        from services.scheduler import lane_steer
        term = self._lane(client, "task-run-term3", "run-term3",
                          callback_chat=client.parent_chat_id)
        term.alive = False
        monkeypatch.setitem(interactive_session._sessions, term.session_id, term)
        monkeypatch.setattr(lane_steer, "_running_delegate_run",
                            lambda cid: task_store.get_run("run-term3"))
        r = _spawn(client, continue_id="task-run-term3", prompt="also V")
        assert r.status_code == 200
        assert "steered" not in r.json()
        assert term.queued == [] and len(client.fired) == 1

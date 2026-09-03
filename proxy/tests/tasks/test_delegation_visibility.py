"""GET /v1/delegation/sessions + /peek and the lane_status derivation.

The visibility set is the caller's own history pool on its agent plus the
workers its chat spawned (any agent); status is derived fresh from the same
in-memory sources the dashboard reconnect path reads.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user
from services.delegation import lane_status
from storage import agent_store, mcp_store
from storage import database as task_store


AGENT = "pa"
PARENT_SESSION = "22222222-2222-2222-2222-222222222222"


def _session_user(sub="user-alice", agent=AGENT, sid=PARENT_SESSION):
    return UserContext(
        sub=sub, email="alice@test.com", name="Alice", role="member",
        is_api_key=True, session_id=sid, agent=agent,
        agents=[agent], agent_roles={agent: "editor"},
    )


@pytest.fixture
def client(temp_db):
    from api.tasks import delegation as delegation_api

    agent_store.create_agent(AGENT, "PA", collaborative=True, default_scope="user")
    agent_store.create_agent("other", "Other", collaborative=True, default_scope="user")
    mcp_store.set_mcp_enabled("delegation-mcp", True)

    parent_chat_id = str(uuid.uuid4())
    task_store.create_chat(parent_chat_id, "user-alice", AGENT)
    task_store.update_chat(parent_chat_id, session_id=PARENT_SESSION)

    app = FastAPI()
    app.include_router(delegation_api.router)
    app.state.user = _session_user()

    async def _current_user():
        return app.state.user

    app.dependency_overrides[get_current_user] = _current_user
    c = TestClient(app)
    c.app_ref = app
    c.parent_chat_id = parent_chat_id
    return c


class TestLaneStatus:
    def test_idle_by_default(self, temp_db):
        assert lane_status.chat_status("nothing-live") == "idle"

    def test_streaming_state_generating(self, temp_db):
        from core.session.session_state import _chat_streaming_state
        _chat_streaming_state["c-gen"] = {"streaming": True}
        try:
            assert lane_status.chat_status("c-gen") == "generating"
        finally:
            _chat_streaming_state.pop("c-gen", None)

    def test_pending_permission_awaits_user(self, temp_db):
        from core.session.session_state import _chat_streaming_state
        _chat_streaming_state["c-perm"] = {"streaming": True,
                                           "pending_permission": {"id": "p1"}}
        try:
            assert lane_status.chat_status("c-perm") == "awaiting_user"
        finally:
            _chat_streaming_state.pop("c-perm", None)

    def test_live_pump_generating(self, temp_db):
        from core.events.stream_pump import _active_pumps

        class _Pump:
            is_done = False

        _active_pumps["c-pump"] = _Pump()
        try:
            assert lane_status.chat_status("c-pump") == "generating"
        finally:
            _active_pumps.pop("c-pump", None)

    def test_interactive_turn_open_generating(self, temp_db):
        from core.session import interactive_session

        class _Live:
            session_id = "sid-int"
            chat_id = "c-int"
            alive = True
            target = None
            created_at = 0.0
            _turn_open = True

        interactive_session._sessions["sid-int"] = _Live()
        try:
            assert lane_status.chat_status("c-int") == "generating"
        finally:
            interactive_session._sessions.pop("sid-int", None)

    def test_interactive_permission_awaits_user(self, temp_db):
        from core.session import interactive_session
        from core.session.session_state import _session_permission_requests

        class _Live:
            session_id = "sid-int2"
            chat_id = "c-int2"
            alive = True
            target = None
            created_at = 0.0
            _turn_open = True

        interactive_session._sessions["sid-int2"] = _Live()
        _session_permission_requests["sid-int2"] = {"req-1"}
        try:
            assert lane_status.chat_status("c-int2") == "awaiting_user"
        finally:
            interactive_session._sessions.pop("sid-int2", None)
            _session_permission_requests.pop("sid-int2", None)

    def test_interactive_question_parked_awaits_user(self, temp_db):
        """A parked question (AskUserQuestion / request_user_input fold)
        CLOSES the turn — the lane must read awaiting_user like the chat
        row's "needs your input", not idle."""
        from core.session import interactive_session

        class _Live:
            session_id = "sid-int3"
            chat_id = "c-int3"
            alive = True
            target = None
            created_at = 0.0
            _turn_open = False
            question_parked = True

        interactive_session._sessions["sid-int3"] = _Live()
        try:
            assert lane_status.chat_status("c-int3") == "awaiting_user"
            # Answered → unparked, turn still closed → idle again.
            _Live.question_parked = False
            assert lane_status.chat_status("c-int3") == "idle"
        finally:
            interactive_session._sessions.pop("sid-int3", None)


class TestListSessions:
    def test_own_and_worker_chats_merged(self, client):
        own = str(uuid.uuid4())
        task_store.create_chat(own, "user-alice", AGENT)
        # Worker on ANOTHER agent — visible via lineage, not ownership.
        worker = str(uuid.uuid4())
        task_store.create_chat(worker, "user-alice", "other",
                               origin="delegated",
                               parent_chat_id=client.parent_chat_id,
                               delegate_role="worker", title="Lane 1")
        r = client.get("/v1/delegation/sessions")
        assert r.status_code == 200
        rows = {s["chat_id"]: s for s in r.json()["sessions"]}
        assert own in rows and worker in rows
        assert rows[worker]["agent"] == "other"
        assert rows[worker]["parent_chat_id"] == client.parent_chat_id
        assert rows[client.parent_chat_id]["is_current"] is True
        assert all(s["status"] == "idle" for s in rows.values())

    def test_foreign_chats_excluded(self, client):
        foreign = str(uuid.uuid4())
        task_store.create_chat(foreign, "user-bob", AGENT)
        r = client.get("/v1/delegation/sessions")
        assert foreign not in {s["chat_id"] for s in r.json()["sessions"]}

    def test_kill_switch_403(self, client):
        mcp_store.set_mcp_enabled("delegation-mcp", False)
        assert client.get("/v1/delegation/sessions").status_code == 403


class TestPeekSession:
    def _worker(self, client, agent="other") -> str:
        worker = str(uuid.uuid4())
        task_store.create_chat(worker, "user-alice", agent,
                               origin="delegated",
                               parent_chat_id=client.parent_chat_id,
                               delegate_role="worker")
        return worker

    def test_default_peek_last_user_and_assistant(self, client):
        w = self._worker(client)
        task_store.add_chat_message(w, "user", "prompt one")
        task_store.add_chat_message(w, "assistant", "answer one")
        task_store.add_chat_message(w, "user", "prompt two")
        task_store.add_chat_message(w, "assistant", "answer two")
        r = client.get(f"/v1/delegation/sessions/{w}/peek")
        assert r.status_code == 200
        data = r.json()
        assert [(m["role"], m["content"]) for m in data["messages"]] == [
            ("user", "prompt two"), ("assistant", "answer two"),
        ]
        assert data["truncated"] is True

    def test_depth_returns_last_n_rows(self, client):
        w = self._worker(client)
        for i in range(4):
            task_store.add_chat_message(w, "assistant", f"turn {i}")
        r = client.get(f"/v1/delegation/sessions/{w}/peek", params={"depth": 3})
        contents = [m["content"] for m in r.json()["messages"]]
        assert contents == ["turn 1", "turn 2", "turn 3"]

    class _FakePump:
        def __init__(self, cutoff: int):
            self.is_done = False
            self._db_msg_cutoff_id = cutoff

    def _arm_live(self, chat_id: str, cutoff: int, blocks: list[dict]):
        from core.events.stream_pump import _active_pumps
        from core.session.session_state import _chat_streaming_state
        pump = self._FakePump(cutoff)
        _active_pumps[chat_id] = pump
        _chat_streaming_state[chat_id] = {"live_blocks": blocks}
        return pump

    def _disarm_live(self, chat_id: str):
        from core.events.stream_pump import _active_pumps
        from core.session.session_state import _chat_streaming_state
        _active_pumps.pop(chat_id, None)
        _chat_streaming_state.pop(chat_id, None)

    def test_live_headless_turn_appends_in_progress(self, client):
        w = self._worker(client)
        task_store.add_chat_message(w, "user", "long job")
        cutoff = task_store.get_last_chat_message_id(w)
        self._arm_live(w, cutoff, [
            {"type": "text", "content": "working on it"},
            {"type": "tool", "name": "Bash", "summary": "Bash: make test",
             "active": True},
        ])
        try:
            r = client.get(f"/v1/delegation/sessions/{w}/peek")
            msgs = r.json()["messages"]
            assert msgs[0] == {"role": "user", "content": "long job"}
            assert msgs[-1]["in_progress"] is True
            assert "working on it" in msgs[-1]["content"]
            assert "[tool running: Bash: make test]" in msgs[-1]["content"]
        finally:
            self._disarm_live(w)

    def test_cutoff_fences_rows_persisted_after_snapshot(self, client):
        # DB rows ABOVE the pump's cutoff belong to the in-progress turn and
        # would duplicate the live copy — the DB view fences them out.
        w = self._worker(client)
        task_store.add_chat_message(w, "user", "long job")
        cutoff = task_store.get_last_chat_message_id(w)
        task_store.add_chat_message(w, "assistant", "persisted mid-request")
        self._arm_live(w, cutoff, [
            {"type": "text", "content": "persisted mid-request"},
        ])
        try:
            r = client.get(f"/v1/delegation/sessions/{w}/peek",
                           params={"depth": 10})
            msgs = r.json()["messages"]
            assert [(m["role"], m.get("in_progress", False)) for m in msgs] == [
                ("user", False), ("assistant", True),
            ]  # exactly one copy — the live one
        finally:
            self._disarm_live(w)

    def test_done_pump_leaves_peek_untouched(self, client):
        w = self._worker(client)
        task_store.add_chat_message(w, "user", "job")
        task_store.add_chat_message(w, "assistant", "finished answer")
        pump = self._arm_live(w, 0, [{"type": "text", "content": "stale"}])
        pump.is_done = True
        try:
            r = client.get(f"/v1/delegation/sessions/{w}/peek")
            msgs = r.json()["messages"]
            assert [(m["role"], m["content"]) for m in msgs] == [
                ("user", "job"), ("assistant", "finished answer"),
            ]
            assert not any(m.get("in_progress") for m in msgs)
        finally:
            self._disarm_live(w)

    def test_foreign_chat_403(self, client):
        foreign = str(uuid.uuid4())
        task_store.create_chat(foreign, "user-bob", AGENT)
        assert client.get(f"/v1/delegation/sessions/{foreign}/peek").status_code == 403

    def test_unknown_chat_404(self, client):
        assert client.get(
            f"/v1/delegation/sessions/{uuid.uuid4()}/peek").status_code == 404

    def test_own_task_chat_peekable_across_agents(self, client):
        # A task-surface worker chat (task-<run_id>, owner = the caller) on a
        # DIFFERENT agent — peekable by ownership, though never listed.
        task_chat = "task-run-peek1"
        task_store.create_chat(task_chat, "user-alice", "other")
        task_store.add_chat_message(task_chat, "assistant", "task output")
        r = client.get(f"/v1/delegation/sessions/{task_chat}/peek")
        assert r.status_code == 200
        assert r.json()["messages"][0]["content"] == "task output"
        listed = client.get("/v1/delegation/sessions").json()["sessions"]
        assert task_chat not in {s["chat_id"] for s in listed}


class TestCrossAgentVisibility:
    """C2: with ``?agent=<slug>|all``, reads follow the USER — the pools the
    acting user could open in the dashboard on that agent (their chats, a
    shared agent's shared pool, task-run chats via their RUN row). No-user
    sessions' cross-agent reach is their delegation roster instead
    (``TestNoUserEdgeReads``); without an edge, the 403 stands."""

    def _alice_multi(self):
        return UserContext(
            sub="user-alice", email="alice@test.com", name="Alice",
            role="member", is_api_key=True, session_id=PARENT_SESSION,
            agent=AGENT, agents=[AGENT, "other"],
            agent_roles={AGENT: "editor", "other": "viewer"},
        )

    def _seed_other(self, client):
        mine, bobs = str(uuid.uuid4()), str(uuid.uuid4())
        task_store.create_chat(mine, "user-alice", "other", title="Mine")
        task_store.create_chat(bobs, "user-bob", "other", title="Bobs")
        # Agent-scope task-run chat on "other" (+ its run row) — the dashboard
        # shows it to ANY accessor via Task History; entitlement lives on the
        # run, not the chat's synthetic ``task::`` owner.
        task_store.create_chat("task-run-x", "task::other", "other",
                               title="Nightly")
        # Bob's user-scope run chat — private to its creator.
        task_store.create_chat("task-run-y", "user-bob", "other",
                               title="Bob task")
        with task_store.get_conn() as conn:
            conn.execute(
                "INSERT INTO task_runs (id, task_id, agent, trigger_type, "
                "status, scope, chat_id) VALUES ('run-x', 't-x', 'other', "
                "'schedule', 'completed', 'agent', 'task-run-x')")
            conn.execute(
                "INSERT INTO task_runs (id, task_id, agent, trigger_type, "
                "status, scope, created_by, chat_id) VALUES ('run-y', 't-y', "
                "'other', 'schedule', 'completed', 'user', 'user-bob', "
                "'task-run-y')")
            conn.commit()
        return mine, bobs

    def test_agent_param_lists_user_derived_pools(self, client):
        client.app_ref.state.user = self._alice_multi()
        mine, bobs = self._seed_other(client)
        r = client.get("/v1/delegation/sessions", params={"agent": "other"})
        assert r.status_code == 200
        ids = {s["chat_id"] for s in r.json()["sessions"]}
        assert mine in ids
        assert "task-run-x" in ids          # agent-scope run: any accessor
        assert bobs not in ids              # another user's chat
        assert "task-run-y" not in ids      # another user's user-scope run

    def test_agent_all_spans_accessible_agents(self, client):
        client.app_ref.state.user = self._alice_multi()
        mine, _bobs = self._seed_other(client)
        own_pa = str(uuid.uuid4())
        task_store.create_chat(own_pa, "user-alice", AGENT)
        # An agent outside alice's reach contributes nothing — even her own
        # (stale) chats there.
        agent_store.create_agent("hidden", "Hidden")
        hidden_chat = str(uuid.uuid4())
        task_store.create_chat(hidden_chat, "user-alice", "hidden")
        r = client.get("/v1/delegation/sessions", params={"agent": "all"})
        ids = {s["chat_id"] for s in r.json()["sessions"]}
        assert {mine, own_pa} <= ids
        assert hidden_chat not in ids

    def test_shared_pool_listed_and_peekable(self, client):
        agent_store.create_agent("shared-o", "SharedO", collaborative=False,
                                 default_scope="agent")
        ctx = self._alice_multi()
        ctx.agents.append("shared-o")
        ctx.agent_roles["shared-o"] = "viewer"
        client.app_ref.state.user = ctx
        shared = str(uuid.uuid4())
        task_store.create_chat(shared, "agent::shared-o", "shared-o",
                               title="Shared room")
        task_store.add_chat_message(shared, "assistant", "shared history")
        r = client.get("/v1/delegation/sessions", params={"agent": "shared-o"})
        assert shared in {s["chat_id"] for s in r.json()["sessions"]}
        p = client.get(f"/v1/delegation/sessions/{shared}/peek")
        assert p.status_code == 200
        assert p.json()["messages"][0]["content"] == "shared history"

    def test_agent_param_403_for_inaccessible(self, client):
        agent_store.create_agent("hidden", "Hidden")
        assert client.get("/v1/delegation/sessions",
                          params={"agent": "hidden"}).status_code == 403

    def test_agent_param_403_for_no_user_session(self, client):
        # No delegation edge wired → denied (a wired edge IS the read
        # grant since the 2026-08-15 merge; none exists here).
        client.app_ref.state.user = UserContext(
            sub="session:svc-1", email="", name="", role="agent",
            is_api_key=True, session_id="svc-1", agent=AGENT)
        assert client.get("/v1/delegation/sessions",
                          params={"agent": "other"}).status_code == 403

    def test_peek_cross_agent_task_chat(self, client):
        client.app_ref.state.user = self._alice_multi()
        self._seed_other(client)
        task_store.add_chat_message("task-run-x", "assistant", "nightly done")
        r = client.get("/v1/delegation/sessions/task-run-x/peek")
        assert r.status_code == 200
        assert r.json()["messages"][0]["content"] == "nightly done"

    def test_peek_foreign_chats_still_403(self, client):
        client.app_ref.state.user = self._alice_multi()
        _mine, bobs = self._seed_other(client)
        # Another user's chat — and their user-scope run chat (run rule).
        assert client.get(
            f"/v1/delegation/sessions/{bobs}/peek").status_code == 403
        assert client.get(
            "/v1/delegation/sessions/task-run-y/peek").status_code == 403


class TestNoUserEdgeReads:
    """A delegation edge gives a NO-USER session agent-shape read reach
    into the wired target — its shared ``agent::`` pool + its AGENT-SCOPE
    task-run chats. Never per-user pools or user-scope runs (rule 1: there
    is no user to follow), and only where an edge exists."""

    def _nouser(self):
        return UserContext(
            sub="session:svc-1", email="", name="", role="agent",
            is_api_key=True, session_id="svc-1", agent=AGENT)

    def _wire(self):
        agent_store.set_delegation_targets(AGENT, ["other"])

    def _seed_other_pools(self):
        task_store.create_chat("obs-shared", "agent::other", "other",
                               title="Other autonomous")
        task_store.add_chat_message("obs-shared", "assistant", "other history")
        task_store.create_chat("obs-user", "user-bob", "other", title="Bob chat")
        task_store.create_chat("task-run-x", "task::other", "other",
                               title="Nightly")
        task_store.add_chat_message("task-run-x", "assistant", "nightly done")
        task_store.create_chat("task-run-y", "user-bob", "other",
                               title="Bob task")
        with task_store.get_conn() as conn:
            conn.execute(
                "INSERT INTO task_runs (id, task_id, agent, trigger_type, "
                "status, scope, chat_id) VALUES ('run-x', 't-x', 'other', "
                "'schedule', 'completed', 'agent', 'task-run-x')")
            conn.execute(
                "INSERT INTO task_runs (id, task_id, agent, trigger_type, "
                "status, scope, created_by, chat_id) VALUES ('run-y', 't-y', "
                "'other', 'schedule', 'completed', 'user', 'user-bob', "
                "'task-run-y')")
            conn.commit()

    def test_edge_target_lists_agent_shape_pools(self, client):
        self._wire()
        self._seed_other_pools()
        client.app_ref.state.user = self._nouser()
        r = client.get("/v1/delegation/sessions", params={"agent": "other"})
        assert r.status_code == 200
        ids = {s["chat_id"] for s in r.json()["sessions"]}
        assert "obs-shared" in ids          # target's shared pool
        assert "task-run-x" in ids          # target's agent-scope run chat
        assert "obs-user" not in ids        # per-user pool: no user to follow
        assert "task-run-y" not in ids      # user-scope run chat

    def test_all_spans_own_plus_targets(self, client):
        self._wire()
        self._seed_other_pools()
        # Own-agent shared pool row (a no-user session's own chats live there).
        task_store.create_chat("own-shared", f"agent::{AGENT}", AGENT,
                               title="Own autonomous")
        client.app_ref.state.user = self._nouser()
        r = client.get("/v1/delegation/sessions", params={"agent": "all"})
        assert r.status_code == 200
        ids = {s["chat_id"] for s in r.json()["sessions"]}
        assert {"own-shared", "obs-shared", "task-run-x"} <= ids
        assert "obs-user" not in ids

    def test_peek_edge_target_agent_shape_only(self, client):
        self._wire()
        self._seed_other_pools()
        client.app_ref.state.user = self._nouser()
        p = client.get("/v1/delegation/sessions/obs-shared/peek")
        assert p.status_code == 200
        assert p.json()["messages"][0]["content"] == "other history"
        p = client.get("/v1/delegation/sessions/task-run-x/peek")
        assert p.status_code == 200
        assert p.json()["messages"][0]["content"] == "nightly done"
        assert client.get(
            "/v1/delegation/sessions/obs-user/peek").status_code == 403
        assert client.get(
            "/v1/delegation/sessions/task-run-y/peek").status_code == 403

    def test_plain_roster_edge_grants_reads(self, client):
        # A wired edge alone opens the target's agent-shape pools — the
        # merged semantics (the old separate observe flag is retired).
        agent_store.set_delegation_targets(AGENT, ["other"])
        self._seed_other_pools()
        client.app_ref.state.user = self._nouser()
        r = client.get("/v1/delegation/sessions", params={"agent": "other"})
        assert r.status_code == 200
        ids = {s["chat_id"] for s in r.json()["sessions"]}
        assert "obs-shared" in ids and "obs-user" not in ids

    def test_unwired_agent_still_403(self, client):
        # An agent OFF the roster stays invisible to a no-user session.
        agent_store.set_delegation_targets(AGENT, [])
        client.app_ref.state.user = self._nouser()
        assert client.get("/v1/delegation/sessions",
                          params={"agent": "other"}).status_code == 403


class TestPresentationSplit:
    """Delegate runs leave the Tasks listing; task-run chats (delegated or
    not) live in the sidebar's Task history view, never the chat list."""

    def test_runs_listing_excludes_delegates_on_request(self, client):
        task_store.create_run("run-d1", "dyn-d", AGENT, "manual", None,
                              "p", "delegate")
        task_store.create_run("run-s1", "task-s", AGENT, "manual", None,
                              "p", "one-time")
        ids = [r["id"] for r in task_store.list_runs(
            exclude_task_type="delegate")]
        assert "run-s1" in ids and "run-d1" not in ids
        assert task_store.get_run_count(exclude_task_type="delegate") == 1
        assert task_store.get_run_count() == 2
        # A direct task_id query still resolves the delegate run (continue
        # flows read output by task id — the endpoint skips the exclusion).
        by_task = task_store.list_runs(task_id="dyn-d")
        assert [r["id"] for r in by_task] == ["run-d1"]

    def test_list_chats_excludes_all_task_chats(self, client):
        task_store.create_chat("task-run-a", "user-alice", AGENT,
                               origin="delegated", title="Lane A")
        task_store.create_chat("task-run-b", "user-alice", AGENT)
        task_store.create_chat(str(uuid.uuid4()), "user-alice", AGENT)
        ids = [c["id"] for c in task_store.list_chats("user-alice")]
        # Task-run chats live in the Task history view now — delegated ones
        # included (the old origin='delegated' carve-out double-listed them
        # against the Active-now strip).
        assert "task-run-a" not in ids
        assert "task-run-b" not in ids

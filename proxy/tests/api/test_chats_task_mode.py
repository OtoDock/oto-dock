"""Tasks in the chat space — the sidebar's task mode.

GET /v1/chats?kind=tasks + /v1/chats/search?kind=tasks list an agent's
task-run chats joined with their latest run, gated by the run rules
(agent-scoped → any user with agent access; user-scoped → creator only —
the /v1/tasks/runs user-view). Chat mode excludes task-% chats outright
(the old origin='delegated' carve-out is gone), and the chat_status fan-out
reaches agent users for the scheduler's synthetic task:: chat owner.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import app
from auth.providers import UserContext, get_current_user
from storage import database as task_store

client = TestClient(app)

AGENT = "agent-tasks"


def _user(sub="user-alice", role="member", agents=(AGENT,)):
    return UserContext(
        sub=sub, email=f"{sub}@test.com", name=sub, role=role,
        agents=list(agents), agent_roles={a: "editor" for a in agents},
    )


@pytest.fixture
def _as():
    def setup(user: UserContext):
        app.dependency_overrides[get_current_user] = lambda: user
    yield setup
    app.dependency_overrides.pop(get_current_user, None)


def _mk_task_chat(*, agent: str = AGENT, scope: str = "agent",
                  created_by: str | None = None, prompt: str = "check backups",
                  status: str = "completed", run_id: str | None = None,
                  task_id: str = "t-nightly") -> str:
    run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
    cid = f"task-{run_id}"
    owner = created_by if scope == "user" and created_by else f"task::{agent}"
    task_store.create_chat(cid, owner, agent)
    task_store.create_run(run_id, task_id, agent, "schedule", None, prompt,
                          scope=scope, created_by=created_by)
    task_store.update_run(run_id, status=status, chat_id=cid,
                          started_at="2026-07-12T00:00:00+00:00")
    task_store.add_chat_message(cid, "user", prompt)
    return cid


# ---------------------------------------------------------------------------
# Chat mode: task-% chats never list (delegated carve-out removed)
# ---------------------------------------------------------------------------

def test_chat_mode_excludes_all_task_chats(temp_db, _as):
    _as(_user())
    plain = str(uuid.uuid4())
    task_store.create_chat(plain, "user-alice", AGENT)
    task_store.create_chat("task-run-x1", "user-alice", AGENT)
    task_store.create_chat("task-run-x2", "user-alice", AGENT, origin="delegated")
    ids = [c["id"] for c in
           client.get(f"/v1/chats?agent={AGENT}").json()["chats"]]
    assert plain in ids
    assert "task-run-x1" not in ids
    assert "task-run-x2" not in ids  # delegate workers moved to task mode


def test_the_mint_writes_the_kind_and_both_lists_agree(temp_db, _as, monkeypatch):
    """core-seams phase 4: a row the scheduler mints carries source_type
    'task' on BOTH branches of _create_task_chat_row; chat mode excludes a
    task row by the resolved list as much as by the id, task mode lists it
    by either half; a row minted BEFORE the write (the default 'chat' with a
    task- id) sorts the same way; a chat-surface delegate worker (a uuid id,
    the default column) stays a chat."""
    import config
    from core.session import session_kind
    from services.scheduler import runner, shared
    monkeypatch.setattr(config, "get_cli_model", lambda *a, **k: "m")
    _as(_user())
    pre = _mk_task_chat(scope="user", created_by="user-alice")
    assert task_store.get_chat(pre)["source_type"] == "chat"
    assert session_kind.of_chat(task_store.get_chat(pre)) is session_kind.TASK
    plain = shared.TaskDefinition(id="dyn-m1", name="t", agent=AGENT, prompt="p", scope="user",
                                  created_by="user-alice")
    task_store.create_run("run-m1", "dyn-m1", AGENT, "scheduled", None, "p", task_type="scheduled",
                          scope="user", created_by="user-alice")
    runner._create_task_chat_row("task-run-m1", "run-m1", plain)
    worker = shared.TaskDefinition(id="dyn-m2", name="w", agent=AGENT, prompt="p", scope="user",
                                   created_by="user-alice", task_type="delegate")
    task_store.create_run("run-m2", "dyn-m2", AGENT, "manual", "orch", "p", task_type="delegate",
                          scope="user", created_by="user-alice")
    runner._create_task_chat_row("task-run-m2", "run-m2", worker)
    for cid in ("task-run-m1", "task-run-m2"):
        row = task_store.get_chat(cid)
        assert row["source_type"] == "task" and row["user_sub"] == "user-alice"
        assert session_kind.of_chat(row) is session_kind.TASK
    # The list clause alone (no task- id): a task-kind row with a uuid id.
    task_store.create_chat("by-column-only", "user-alice", AGENT, source_type="task")
    task_store.create_run("run-m3", "dyn-m3", AGENT, "scheduled", None, "p", task_type="scheduled",
                          scope="user", created_by="user-alice")
    task_store.update_run("run-m3", chat_id="by-column-only", started_at="2026-07-12T00:00:00+00:00")
    # A chat-surface delegate worker: a uuid id, the default column, a run row.
    task_store.create_chat("worker-uuid", "user-alice", AGENT, origin="delegated", delegate_role="worker")
    task_store.create_run("run-m4", "dyn-m4", AGENT, "manual", "orch", "p", task_type="delegate",
                          scope="user", created_by="user-alice")
    task_store.update_run("run-m4", chat_id="worker-uuid", started_at="2026-07-12T00:00:00+00:00")
    chat_ids = {c["id"] for c in client.get(f"/v1/chats?agent={AGENT}").json()["chats"]}
    task_ids = {c["id"] for c in client.get(f"/v1/chats?agent={AGENT}&kind=tasks").json()["chats"]}
    assert not ({pre, "task-run-m1", "task-run-m2", "by-column-only"} & chat_ids)
    assert "worker-uuid" in chat_ids
    assert {pre, "task-run-m1", "task-run-m2", "by-column-only"} <= task_ids
    assert "worker-uuid" not in task_ids


def test_the_conversations_tab_lists_the_external_kinds_only(temp_db):
    """The agent Conversations tab takes the resolved list of the externally
    driven kinds (phone today): a task row — minted before or after the
    column was written — never lists there, nor does a dashboard chat."""
    from core.session import session_kind
    task_store.create_chat("phone-1", "phone", AGENT, "auto", source_type="phone")
    task_store.create_chat(str(uuid.uuid4()), "user-alice", AGENT)
    _mk_task_chat()
    task_store.create_chat("task-run-new", f"task::{AGENT}", AGENT, "auto", source_type="task")
    kinds = session_kind.EXTERNAL_DRIVEN_SOURCE_TYPES
    assert [r["id"] for r in task_store.get_agent_conversations(AGENT, source_types=kinds)] == ["phone-1"]
    assert task_store.count_agent_conversations(AGENT, source_types=kinds) == 1
    assert [r["id"] for r in task_store.get_agent_conversations(
        AGENT, source_type="phone", source_types=kinds)] == ["phone-1"]
    assert task_store.get_agent_conversations(AGENT, source_type="task", source_types=kinds) == []


def test_unread_finished_backfill_excludes_delegated_task_chats(temp_db):
    task_store.create_chat("task-run-d9", "user-alice", AGENT, origin="delegated")
    task_store.update_chat("task-run-d9",
                           last_response_at="2026-07-12T00:00:00+00:00")
    rows = task_store.list_unread_finished_chats("2026-01-01T00:00:00+00:00")
    assert all(not r["id"].startswith("task-") for r in rows)


# ---------------------------------------------------------------------------
# Task mode listing
# ---------------------------------------------------------------------------

def test_task_mode_lists_with_latest_run_join(temp_db, _as):
    _as(_user())
    cid = _mk_task_chat(prompt="nightly backup sweep", status="completed")
    # A second, newer run on the SAME chat (multi-round continue) wins the join.
    run2 = f"run-{uuid.uuid4().hex[:12]}"
    task_store.create_run(run2, "t-nightly", AGENT, "manual", None, "round 2")
    task_store.update_run(run2, status="running", chat_id=cid,
                          started_at="2026-07-12T09:00:00+00:00")
    rows = client.get(f"/v1/chats?agent={AGENT}&kind=tasks").json()["chats"]
    assert [r["id"] for r in rows] == [cid]
    row = rows[0]
    assert row["run_id"] == run2
    assert row["run_status"] == "running"
    assert row["unread"] is False
    # No dynamic row (one-time cleanup) → NULL, so the client falls back to
    # the chat title instead of labeling the row with a raw task_id.
    assert row["task_name"] is None
    # Deterministic title stamped at first-message persistence.
    assert row["title"] == "nightly backup sweep"


def test_task_mode_task_name_prefers_dynamic_task_name(temp_db, _as):
    _as(_user())
    task_store.create_dynamic_task(
        "dyn-abc", AGENT, "Nightly report", "p", "cli", "scheduled",
        "0 9 * * *", None, None, 3600, "user-alice", scope="agent")
    cid = _mk_task_chat(task_id="dyn-abc")
    rows = client.get(f"/v1/chats?agent={AGENT}&kind=tasks").json()["chats"]
    assert rows[0]["id"] == cid
    assert rows[0]["task_name"] == "Nightly report"


def test_task_mode_user_scope_creator_only(temp_db, _as):
    own = _mk_task_chat(scope="user", created_by="user-alice")
    other = _mk_task_chat(scope="user", created_by="user-bob")
    shared = _mk_task_chat(scope="agent")
    _as(_user())
    ids = [r["id"] for r in
           client.get(f"/v1/chats?agent={AGENT}&kind=tasks").json()["chats"]]
    assert own in ids and shared in ids
    assert other not in ids
    # Admins get the user-view too (the admin History page is the audit surface).
    _as(_user(sub="user-admin", role="admin"))
    ids = [r["id"] for r in
           client.get(f"/v1/chats?agent={AGENT}&kind=tasks").json()["chats"]]
    assert other not in ids and shared in ids


def test_task_mode_requires_agent_param_and_access(temp_db, _as):
    _as(_user())
    assert client.get("/v1/chats?kind=tasks").status_code == 400
    assert client.get(
        "/v1/chats?agent=agent-not-mine&kind=tasks").status_code == 403


# ---------------------------------------------------------------------------
# Search follows the mode
# ---------------------------------------------------------------------------

def test_search_chat_mode_excludes_task_chats(temp_db, _as):
    _as(_user())
    plain = str(uuid.uuid4())
    task_store.create_chat(plain, "user-alice", AGENT)
    task_store.add_chat_message(plain, "user", "elephants love backups")
    # A user-scoped task chat carries the creator's sub — without the task-%
    # exclusion it would leak into chat-mode search.
    _mk_task_chat(scope="user", created_by="user-alice",
                  prompt="elephants love backups")
    rows = client.get(
        f"/v1/chats/search?agent={AGENT}&q=elephants").json()["chats"]
    assert [r["id"] for r in rows] == [plain]


def test_search_task_mode_matches_and_gates(temp_db, _as):
    mine = _mk_task_chat(scope="user", created_by="user-alice",
                         prompt="quarterly elephant census")
    theirs = _mk_task_chat(scope="user", created_by="user-bob",
                           prompt="quarterly elephant census")
    _as(_user())
    rows = client.get(
        f"/v1/chats/search?agent={AGENT}&q=elephant&kind=tasks").json()["chats"]
    ids = [r["id"] for r in rows]
    assert mine in ids and theirs not in ids
    assert rows[0]["unread"] is False


# ---------------------------------------------------------------------------
# chat_status fan-out for the scheduler's task:: owner
# ---------------------------------------------------------------------------

def test_chat_status_targets_task_owner_reaches_agent_users(temp_db, monkeypatch):
    from services.notifications import notification_manager as nm
    from storage.automation import notification_store
    monkeypatch.setattr(notification_store, "get_agent_user_subs",
                        lambda agent: ["user-alice", "user-bob"])
    assert nm.chat_status_targets(f"task::{AGENT}", AGENT) == [
        "user-alice", "user-bob"]
    # Owner-less / agent-less stays empty (no fan-out target).
    assert nm.chat_status_targets(f"task::{AGENT}", "") == []
    assert nm.chat_status_targets("user-alice", AGENT) == ["user-alice"]


# ---------------------------------------------------------------------------
# Task deep links open the chat page with task mode on
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delivery_push_task_chat_deep_link(temp_db, monkeypatch):
    import services.notifications.notification_manager as nm
    import services.notifications.push_sender as ps
    captured = {}

    async def fake_send_to_user(user_sub, payload):
        captured["payload"] = payload
    monkeypatch.setattr(ps, "send_to_user", fake_send_to_user)
    monkeypatch.setattr(nm, "get_active_connections", lambda u: [])
    monkeypatch.setattr(nm, "get_all_connections", lambda u: [])

    delivery = {
        "id": "d1", "notification_id": "n1", "title": "T", "body": "B",
        "severity": "info", "scope": "user", "source": "",
        "delivered_at": "2026-07-12T00:00:00Z",
        "agent_slug": AGENT, "chat_id": "task-run-77",
    }
    await nm._deliver_to_user("user1", delivery)
    assert captured["payload"]["click_url"] == f"/chat/{AGENT}/task-run-77?tasks=1"

    # Agent-less delivery falls back to the /runs resolver redirect.
    delivery["agent_slug"] = None
    await nm._deliver_to_user("user1", delivery)
    assert captured["payload"]["click_url"] == "/runs/run-77"


@pytest.mark.asyncio
async def test_ephemeral_push_task_chat_deep_link(temp_db, monkeypatch):
    import services.notifications.notification_manager as nm
    import services.notifications.push_sender as ps
    from storage.automation import notification_store as ns
    captured = {}

    async def fake_send_fcm(token, payload):
        captured["payload"] = payload
    monkeypatch.setattr(ps, "send_fcm", fake_send_fcm)
    monkeypatch.setattr(ns, "get_push_subscriptions",
                        lambda u: [{"platform": "android", "subscription_data": "t"}])
    monkeypatch.setattr(nm, "has_active_connection", lambda u: False)

    task_store.create_chat("task-run-88", f"task::{AGENT}", AGENT)
    await nm.fire_ephemeral("user1", "Done", "", chat_id="task-run-88")
    assert captured["payload"]["click_url"] == f"/chat/{AGENT}/task-run-88?tasks=1"

    # Unknown chat row → the /runs resolver fallback.
    await nm.fire_ephemeral("user1", "Done", "", chat_id="task-run-gone")
    assert captured["payload"]["click_url"] == "/runs/run-gone"


# ---------------------------------------------------------------------------
# Off the loop: the task-mode listing and search are one executor job each
# ---------------------------------------------------------------------------

def test_task_mode_listing_and_search_run_off_the_loop(temp_db, loop_db_guard):
    """The task-history list (and its search twin) never reads the
    store on the event loop thread. The handler coroutines are awaited
    directly on this thread with the guard armed; rows and flags are the
    ones the on-loop listing produced."""
    from api.agents import chats as api
    import asyncio
    task_store.create_dynamic_task(
        "dyn-off", AGENT, "Nightly report", "p", "cli", "scheduled",
        "0 9 * * *", None, None, 3600, "user-alice", scope="agent")
    cid = _mk_task_chat(task_id="dyn-off", prompt="elephant census off loop",
                        created_by="user-alice")
    u = _user()

    async def scenario():
        with loop_db_guard.active():
            listed = await api.list_chats(agent=AGENT, kind="tasks", limit=50, user=u)
            found = await api.search_chats(q="elephant", agent=AGENT, kind="tasks",
                                           limit=20, user=u)
        return listed["chats"], found["chats"]

    listed, found = asyncio.run(scenario())
    assert [r["id"] for r in listed] == [cid]
    assert listed[0]["task_name"] == "Nightly report"
    assert listed[0]["can_rename"] is True and listed[0]["can_delete"] is True
    assert [r["id"] for r in found] == [cid]


def test_task_mode_listing_answers_400_and_403_from_the_job(temp_db):
    from fastapi import HTTPException
    from api.agents import chats as api
    import asyncio

    async def scenario():
        with pytest.raises(HTTPException) as no_agent:
            await api.list_chats(agent=None, kind="tasks", limit=50, user=_user())
        with pytest.raises(HTTPException) as denied:
            await api.list_chats(agent="agent-not-mine", kind="tasks", limit=50,
                                 user=_user())
        return no_agent.value.status_code, denied.value.status_code

    assert asyncio.run(scenario()) == (400, 403)

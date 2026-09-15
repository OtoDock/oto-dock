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

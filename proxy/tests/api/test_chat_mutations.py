"""Chat/task-history rename + delete — permissions, sanitization, guards.

PATCH/DELETE /v1/chats/{id} gate on can_access_chat AND can_mutate_chat:
task-history chats (any chat with a task_runs row — ``task-`` ids and
uuid delegate workers alike) follow the task matrix (manager any / editor
own / viewer none; user-scope creator-only); shared-only ``agent::`` chats
are editor+; everything else owner-only. Deleting a chat with a live
(running|pending) run 409s — an EXISTS over ALL runs, so a pending run on a
multi-run worker chat can't hide behind a completed "latest". Titles are
sanitized (bidi/zero-width/control strip, 160-cp clip) and fan out via
broadcast_chat_title. Listings + search stamp can_rename/can_delete with
the same logic.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import app
from auth.providers import UserContext, get_current_user
from storage import agent_store
from storage import database as task_store

client = TestClient(app)

AGENT = "agent-mut"


def _user(sub="user-alice", role="member", agent_role="editor", agents=(AGENT,)):
    return UserContext(
        sub=sub, email=f"{sub}@test.com", name=sub, role=role,
        agents=list(agents),
        agent_roles={a: agent_role for a in agents},
    )


ADMIN = _user("user-root", role="admin")
MANAGER = _user("user-mgr", agent_role="manager")
EDITOR = _user("user-ed", agent_role="editor")
EDITOR2 = _user("user-ed2", agent_role="editor")
VIEWER = _user("user-view", agent_role="viewer")


@pytest.fixture
def _as():
    def setup(user: UserContext):
        app.dependency_overrides[get_current_user] = lambda: user
    yield setup
    app.dependency_overrides.pop(get_current_user, None)


def _mk_task_chat(*, scope="agent", created_by=None, status="completed",
                  chat_id=None, task_id=None, prompt="check backups"):
    """A task-run chat: ``task-{run}`` id by default, or an explicit uuid id
    (chat-surface delegate worker shape)."""
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    cid = chat_id or f"task-{run_id}"
    owner = created_by if scope == "user" and created_by else f"task::{AGENT}"
    task_store.create_chat(cid, owner, AGENT)
    task_store.create_run(run_id, task_id or f"t-{uuid.uuid4().hex[:8]}",
                          AGENT, "schedule", None, prompt,
                          scope=scope, created_by=created_by)
    task_store.update_run(run_id, status=status, chat_id=cid,
                          started_at="2026-07-12T00:00:00+00:00")
    task_store.add_chat_message(cid, "user", prompt)
    return cid, run_id


# ---------------------------------------------------------------------------
# View: the task-run branch of can_access_chat (REST parity with the listing)
# ---------------------------------------------------------------------------

def test_agent_scoped_run_chat_visible_to_any_agent_user(temp_db, _as):
    cid, _ = _mk_task_chat(created_by="user-ed")
    _as(VIEWER)
    assert client.get(f"/v1/chats/{cid}").status_code == 200


def test_user_scoped_run_chat_creator_only(temp_db, _as):
    cid, _ = _mk_task_chat(scope="user", created_by="user-ed")
    _as(EDITOR)
    assert client.get(f"/v1/chats/{cid}").status_code == 200
    _as(EDITOR2)
    assert client.get(f"/v1/chats/{cid}").status_code == 403


# ---------------------------------------------------------------------------
# Mutation matrix — task-history chats (rename PATCH as the probe)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("user,expected", [
    (ADMIN, 200), (MANAGER, 200), (EDITOR, 200), (EDITOR2, 403), (VIEWER, 403),
])
def test_agent_scope_matrix(temp_db, _as, user, expected):
    cid, _ = _mk_task_chat(created_by="user-ed")
    _as(user)
    r = client.patch(f"/v1/chats/{cid}", json={"title": "renamed"})
    assert r.status_code == expected, r.text


def test_user_scope_creator_only_even_for_manager(temp_db, _as):
    cid, _ = _mk_task_chat(scope="user", created_by="user-ed")
    _as(MANAGER)
    assert client.patch(f"/v1/chats/{cid}", json={"title": "x"}).status_code == 403
    _as(EDITOR)
    assert client.patch(f"/v1/chats/{cid}", json={"title": "x"}).status_code == 200


def test_uuid_delegate_worker_follows_matrix(temp_db, _as):
    # Chat-surface worker: plain-uuid chat id WITH a run row — the matrix must
    # key on run existence, never the id prefix.
    cid, _ = _mk_task_chat(chat_id=str(uuid.uuid4()), created_by="user-ed")
    _as(MANAGER)
    assert client.patch(f"/v1/chats/{cid}", json={"title": "mgr"}).status_code == 200
    _as(VIEWER)
    assert client.patch(f"/v1/chats/{cid}", json={"title": "v"}).status_code == 403


def test_plain_chat_owner_only(temp_db, _as):
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, "user-ed", AGENT)
    _as(EDITOR)
    assert client.patch(f"/v1/chats/{cid}", json={"title": "mine"}).status_code == 200
    _as(MANAGER)  # manager ≠ owner and no run row → owner-only rule
    assert client.patch(f"/v1/chats/{cid}", json={"title": "not-mine"}).status_code == 403


def test_shared_only_chat_editor_plus(temp_db, _as):
    agent_store.create_agent("so-mut", "SO", collaborative=False,
                             default_scope="agent")
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, "agent::so-mut", "so-mut")
    viewer = _user("user-v2", agent_role="viewer", agents=("so-mut",))
    editor = _user("user-e2", agent_role="editor", agents=("so-mut",))
    _as(viewer)
    # Viewers keep READ access to the shared history…
    assert client.get(f"/v1/chats/{cid}").status_code == 200
    # …but no longer mutate it (they previously could delete).
    assert client.patch(f"/v1/chats/{cid}", json={"title": "v"}).status_code == 403
    assert client.delete(f"/v1/chats/{cid}").status_code == 403
    _as(editor)
    assert client.patch(f"/v1/chats/{cid}", json={"title": "e"}).status_code == 200
    assert client.delete(f"/v1/chats/{cid}").status_code == 200


# ---------------------------------------------------------------------------
# Delete: the live-run 409 guard + history-only semantics
# ---------------------------------------------------------------------------

def test_delete_blocked_while_run_live(temp_db, _as):
    cid, run_id = _mk_task_chat(created_by="user-ed", status="running")
    _as(MANAGER)
    assert client.delete(f"/v1/chats/{cid}").status_code == 409
    task_store.update_run(run_id, status="completed")
    assert client.delete(f"/v1/chats/{cid}").status_code == 200
    # History-only: the run row survives for the admin audit pages.
    assert task_store.get_run(run_id) is not None
    assert task_store.get_chat(cid) is None


def test_delete_sees_pending_run_behind_completed_latest(temp_db, _as):
    # Multi-run worker chat: completed run (started) + PENDING run (no
    # started_at — sorts LAST on the latest-run ordering). The EXISTS guard
    # must still find it.
    cid, _ = _mk_task_chat(chat_id=str(uuid.uuid4()), created_by="user-ed")
    pending = f"run-{uuid.uuid4().hex[:12]}"
    task_store.create_run(pending, "t-next", AGENT, "delegate", None, "next round",
                          scope="agent", created_by="user-ed")
    task_store.update_run(pending, status="pending", chat_id=cid)
    _as(MANAGER)
    assert client.delete(f"/v1/chats/{cid}").status_code == 409


# ---------------------------------------------------------------------------
# Rename: sanitization + broadcast + echo
# ---------------------------------------------------------------------------

def test_title_sanitization(temp_db, _as, monkeypatch):
    from services.notifications import notification_manager
    calls = []
    monkeypatch.setattr(notification_manager, "broadcast_chat_title",
                        lambda *a, **k: calls.append((a, k)))
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, "user-ed", AGENT)
    _as(EDITOR)
    # Bidi override + zero-width stripped; inner newline collapses to a space.
    r = client.patch(f"/v1/chats/{cid}",
                     json={"title": "safe‮title​ one\ntwo"})
    assert r.status_code == 200
    assert r.json()["title"] == "safetitle one two"
    assert task_store.get_chat(cid)["title"] == "safetitle one two"
    assert len(calls) == 1  # fan-out fired with the sanitized title
    assert calls[0][0][2] == "safetitle one two"
    # Invisible-only → 400 (never an empty title row).
    assert client.patch(f"/v1/chats/{cid}",
                        json={"title": "​‮  "}).status_code == 400
    # 160-codepoint clip.
    r = client.patch(f"/v1/chats/{cid}", json={"title": "x" * 500})
    assert len(r.json()["title"]) == 160


def test_rename_blocks_llm_upgrade(temp_db, _as):
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, "user-ed", AGENT)
    _as(EDITOR)
    client.patch(f"/v1/chats/{cid}", json={"title": "Human title"})
    claimed, _ = task_store.claim_title_generation(cid)
    assert claimed is False  # title_generated stamped by the rename


# ---------------------------------------------------------------------------
# Listing + search flags — must judge exactly what the endpoints enforce
# ---------------------------------------------------------------------------

def _mk_named_task_chat(*, task_creator="user-ed", run_creator=None):
    task_id = f"t-{uuid.uuid4().hex[:8]}"
    task_store.create_dynamic_task(
        task_id, AGENT, "Nightly report", "do thing", "cli",
        "scheduled", "0 9 * * *", None, 1, 600,
        task_creator, None, None, None, None, False,
        scope="agent",
    )
    cid, run_id = _mk_task_chat(created_by=run_creator or task_creator,
                                task_id=task_id)
    return cid, task_id


def test_task_listing_flags_per_role(temp_db, _as):
    cid_own, _ = _mk_named_task_chat(task_creator="user-ed")
    cid_other, _ = _mk_named_task_chat(task_creator="user-ed2",
                                       run_creator="user-ed2")
    _as(EDITOR)
    rows = {c["id"]: c for c in
            client.get(f"/v1/chats?agent={AGENT}&kind=tasks").json()["chats"]}
    assert rows[cid_own]["can_rename"] is True
    assert rows[cid_own]["can_delete"] is True
    assert rows[cid_other]["can_rename"] is False
    assert rows[cid_other]["can_delete"] is False
    _as(MANAGER)
    rows = {c["id"]: c for c in
            client.get(f"/v1/chats?agent={AGENT}&kind=tasks").json()["chats"]}
    assert rows[cid_other]["can_rename"] is True
    assert rows[cid_other]["can_delete"] is True
    _as(VIEWER)
    rows = {c["id"]: c for c in
            client.get(f"/v1/chats?agent={AGENT}&kind=tasks").json()["chats"]}
    assert all(not r["can_rename"] and not r["can_delete"] for r in rows.values())


def test_search_carries_flags(temp_db, _as):
    _mk_named_task_chat(task_creator="user-ed")
    _as(EDITOR)
    rows = client.get(
        f"/v1/chats/search?q=backups&agent={AGENT}&kind=tasks").json()["chats"]
    assert rows and all("can_rename" in r and "can_delete" in r for r in rows)
    # Chat-mode search too (plain per-user history).
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, "user-ed", AGENT)
    task_store.add_chat_message(cid, "user", "findme in search")
    rows = client.get(
        f"/v1/chats/search?q=findme&agent={AGENT}").json()["chats"]
    assert rows and rows[0]["can_rename"] is True


def test_chat_listing_flags_owner(temp_db, _as):
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, "user-ed", AGENT)
    _as(EDITOR)
    rows = {c["id"]: c for c in
            client.get(f"/v1/chats?agent={AGENT}").json()["chats"]}
    assert rows[cid]["can_rename"] is True and rows[cid]["can_delete"] is True


# ---------------------------------------------------------------------------
# Definition rename → live run chats re-label everywhere
# ---------------------------------------------------------------------------

def test_definition_rename_broadcasts_live_run_chats(temp_db, _as, monkeypatch):
    from services.notifications import notification_manager
    calls = []
    monkeypatch.setattr(notification_manager, "broadcast_chat_title",
                        lambda *a, **k: calls.append(a))
    # The tasks API re-derives roles from the DB (unlike the chats API's
    # UserContext-only gate) — the acting editor needs real rows.
    task_store.upsert_user("user-ed", "user-ed@test.com", "user-ed", "member")
    task_store.add_user_agent("user-ed", AGENT, "editor", "user-root")
    cid, task_id = _mk_named_task_chat(task_creator="user-ed")
    # A live run of the task; the completed one from the fixture must NOT
    # broadcast (idle rows refresh on the next poll).
    live = f"run-{uuid.uuid4().hex[:12]}"
    live_cid = f"task-{live}"
    task_store.create_chat(live_cid, f"task::{AGENT}", AGENT)
    task_store.create_run(live, task_id, AGENT, "schedule", None, "p",
                          scope="agent", created_by="user-ed")
    task_store.update_run(live, status="running", chat_id=live_cid,
                          started_at="2026-07-12T01:00:00+00:00")
    _as(EDITOR)
    r = client.patch(f"/v1/tasks/{task_id}", json={"name": "Renamed nightly"})
    assert r.status_code == 200, r.text
    assert [c[1] for c in calls] == [live_cid]
    assert calls[0][2] == "Renamed nightly"

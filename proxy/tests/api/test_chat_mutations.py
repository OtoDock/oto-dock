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
from storage.agents import agent_store
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


def test_shared_only_pool_lists_and_searches_for_members_only(temp_db, _as):
    """The shared history's list and search answer the agent's members;
    anyone else gets 403, never the pool's rows or a content-search hit."""
    agent_store.create_agent("so-list", "SO", collaborative=False, default_scope="agent")
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, "agent::so-list", "so-list")
    task_store.add_chat_message(cid, "user", "zebrafinance numbers")
    outsider = _user("user-out", agent_role="editor", agents=(AGENT,))
    member = _user("user-in", agent_role="viewer", agents=("so-list",))
    _as(outsider)
    assert client.get("/v1/chats", params={"agent": "so-list"}).status_code == 403
    assert client.get("/v1/chats/search",
                      params={"agent": "so-list", "q": "zebrafinance"}).status_code == 403
    _as(member)
    listed = client.get("/v1/chats", params={"agent": "so-list"})
    assert listed.status_code == 200 and [c["id"] for c in listed.json()["chats"]] == [cid]
    found = client.get("/v1/chats/search", params={"agent": "so-list", "q": "zebrafinance"})
    assert found.status_code == 200 and found.json()


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


# ---------------------------------------------------------------------------
# Off the loop: listing, search, rename, delete, create, dismiss-preview
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402



def _record_title_broadcasts(monkeypatch) -> list:
    from services.notifications import notification_manager
    calls: list = []
    monkeypatch.setattr(notification_manager, "broadcast_chat_title",
                        lambda *a, **k: calls.append((a, k)))
    return calls


def test_chat_listing_and_search_run_off_the_loop(temp_db, loop_db_guard):
    """The sidebar list and search, flags included, are one executor
    job each; nothing reads the store on the loop thread."""
    from api.agents import chats as api
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, "user-ed", AGENT)
    task_store.add_chat_message(cid, "user", "findme off the loop")
    worker, _ = _mk_task_chat(chat_id=str(uuid.uuid4()), created_by="user-ed")

    async def scenario():
        with loop_db_guard.active():
            listed = await api.list_chats(agent=AGENT, kind="chats", limit=50, user=EDITOR)
            found = await api.search_chats(q="findme", agent=AGENT, kind="chats",
                                           limit=20, user=EDITOR)
        return listed["chats"], found["chats"]

    listed, found = asyncio.run(scenario())
    rows = {r["id"]: r for r in listed}
    assert rows[cid]["can_rename"] is True and rows[cid]["can_delete"] is True
    assert rows[cid]["can_share"] is True
    # Task chats never list in chat mode, delegate workers included.
    assert worker not in rows
    assert [r["id"] for r in found] == [cid] and found[0]["can_rename"] is True


def test_rename_lands_through_the_chat_writer_off_the_loop(temp_db, loop_db_guard, monkeypatch):
    """L2 item 6: the rename's checks run as one executor job and its row
    write (with the search-row rebuild) rides the chat's writer lane; the
    response waits for the row, so the sidebar search finds the new title."""
    from api.agents import chats as api
    from core.events import chat_writer
    calls = _record_title_broadcasts(monkeypatch)
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, "user-ed", AGENT)
    task_store.add_chat_message(cid, "user", "first message")

    async def scenario():
        with loop_db_guard.active():
            res = await api.update_chat(cid, req=api.UpdateChatRequest(title="Zebra plan"),
                                        user=EDITOR)
        assert await chat_writer.drain(cid, timeout=5)
        return res

    res = asyncio.run(scenario())
    assert res == {"status": "ok", "title": "Zebra plan"}
    row = task_store.get_chat(cid)
    assert row["title"] == "Zebra plan" and row["title_generated"] is True
    assert [r["id"] for r in task_store.search_chats("user-ed", AGENT, "zebra")] == [cid]
    assert calls and calls[0][0][2] == "Zebra plan"


def test_rename_refusals_come_from_the_job(temp_db, monkeypatch):
    from fastapi import HTTPException
    from api.agents import chats as api
    _record_title_broadcasts(monkeypatch)
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, "user-ed", AGENT)

    async def scenario():
        codes = []
        for who, target, title in ((EDITOR, "nope", "x"), (EDITOR2, cid, "x"),
                                   (EDITOR, cid, "​")):
            with pytest.raises(HTTPException) as exc:
                await api.update_chat(target, req=api.UpdateChatRequest(title=title), user=who)
            codes.append(exc.value.status_code)
        return codes

    assert asyncio.run(scenario()) == [404, 403, 400]
    assert task_store.get_chat(cid)["title"] in ("", None)


def test_create_delete_and_dismiss_run_off_the_loop(temp_db, loop_db_guard):
    """L1 item 8: the chats.py writes (INSERT, DELETE, the preview UPDATE)
    and the checks in front of them run on the executor."""
    from api.agents import chats as api

    async def scenario():
        with loop_db_guard.active():
            created = await api.create_chat(
                req=api.CreateChatRequest(agent=AGENT, permission_mode="default"),
                user=EDITOR)
            cid = created["chat"]["id"]
            dismissed = await api.dismiss_preview(cid, "file-1", snapshot_id=None,
                                                  message_id=None, user=EDITOR)
            deleted = await api.delete_chat(cid, user=EDITOR)
        return cid, created["chat"]["user_sub"], dismissed, deleted

    cid, owner, dismissed, deleted = asyncio.run(scenario())
    assert owner == "user-ed"
    assert dismissed == {"status": "ok", "dismissed": 0}
    assert deleted == {"status": "ok"}
    assert task_store.get_chat(cid) is None


def _pin_ledger(monkeypatch):
    """A deterministic admission ledger (the test_concurrency fixture's
    shape): plenty of RAM, no caps, a fresh condition for this loop."""
    import config
    import core.concurrency as C
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 0)
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 0)
    monkeypatch.setattr(C, "_live_available_mb", lambda: 100_000)
    for name in ("_sessions", "_session_est", "_session_added_at", "_session_owner", "_line"):
        getattr(C, name).clear()
    C._reserved_mb = 0
    C._parked_tasks = 0
    C._live_cache = None
    C._budget_mb = 50_000
    C._floor_mb = 300
    C._total_mb = 80_000
    return C


def test_delete_closes_the_chats_live_terminal(temp_db, monkeypatch):
    """The L1 walk finding: deleting a chat ends its interactive session at
    once. The fake session releases its ledger reservation the way
    ``InteractiveSession.close`` does, so the count a person is held to
    drops with the chat; its queued prompts are dropped, never handed back."""
    from api.agents import chats as api
    from core.session import interactive_session
    C = _pin_ledger(monkeypatch)
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, "user-ed", AGENT)
    sid = "sess-" + uuid.uuid4().hex[:8]
    task_store.update_chat(cid, session_id=sid)
    closed: list[str] = []

    class FakeTerminal:
        session_id = sid
        chat_id = cid
        alive = True
        target = None
        created_at = 1.0
        _prompt_queue = [{"text": "queued"}]

        async def close(self, *, reason="closed"):
            closed.append(reason)
            self.alive = False
            C.release_chat_slot(self.session_id)
            interactive_session._sessions.pop(self.session_id, None)

    fake = FakeTerminal()
    monkeypatch.setitem(interactive_session._sessions, sid, fake)

    async def scenario():
        C._cond = asyncio.Condition()
        assert await C.acquire_chat_slot(sid, user_sub="user-ed")
        assert sid in C._sessions
        res = await api.delete_chat(cid, user=EDITOR)
        return res, sid in C._sessions

    res, still_held = asyncio.run(scenario())
    assert res == {"status": "ok"}
    assert closed == ["chat_deleted"]
    assert fake._prompt_queue == []
    assert still_held is False
    assert sid not in interactive_session._sessions
    assert task_store.get_chat(cid) is None


def test_delete_still_closes_a_pooled_session_through_its_layer(temp_db, monkeypatch):
    from api.agents import chats as api
    from core.session import session_manager
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, "user-ed", AGENT)
    sid = "sess-" + uuid.uuid4().hex[:8]
    task_store.update_chat(cid, session_id=sid)
    closed: list[str] = []

    class FakeLayer:
        async def close_session(self, session_id):
            closed.append(session_id)

    layer = FakeLayer()
    monkeypatch.setattr(session_manager, "find_layer_for_session",
                        lambda s: layer if s == sid else None)
    assert asyncio.run(api.delete_chat(cid, user=EDITOR)) == {"status": "ok"}
    assert closed == [sid]
    assert task_store.get_chat(cid) is None


def test_delete_waits_for_a_chat_that_is_still_starting(temp_db, monkeypatch):
    """A warming session is registered nowhere a close can reach, so the
    delete refuses with its own sentence until the session exists; the row
    stays. The same for a one-shot wake in flight."""
    from fastapi import HTTPException
    from api.agents import chats as api
    from core.session import session_delivery, warmup_registry
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, "user-ed", AGENT)
    monkeypatch.setitem(warmup_registry._inflight, cid,
                        warmup_registry.InflightWarmup(chat_id=cid, user_sub="user-ed",
                                                       agent=AGENT))

    async def scenario():
        with pytest.raises(HTTPException) as exc:
            await api.delete_chat(cid, user=EDITOR)
        warmup_registry._inflight.pop(cid, None)
        monkeypatch.setitem(session_delivery._oneshot_inflight, cid, asyncio.Event())
        with pytest.raises(HTTPException) as exc2:
            await api.delete_chat(cid, user=EDITOR)
        session_delivery._oneshot_inflight.pop(cid, None)
        return exc.value, exc2.value

    first, second = asyncio.run(scenario())
    assert first.status_code == 409 and "still starting" in first.detail
    assert second.status_code == 409 and second.detail == first.detail
    assert task_store.get_chat(cid) is not None


def test_delete_lands_behind_the_chats_queued_writer_job(temp_db, caplog):
    """The row delete rides the chat's writer lane: a row job queued before
    it lands first, so nothing fails on the vanished row."""
    import logging
    import time
    from api.agents import chats as api
    from core.events import chat_writer
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, "user-ed", AGENT)

    def slow_row():
        time.sleep(0.2)
        return task_store.add_chat_message(cid, "assistant", "landed first")

    async def scenario():
        fut = chat_writer.submit(cid, slow_row, label="slow")
        res = await api.delete_chat(cid, user=EDITOR)
        return await fut, res

    with caplog.at_level(logging.WARNING, logger="claude-proxy.chat-writer"):
        row_id, res = asyncio.run(scenario())
    assert row_id > 0 and res == {"status": "ok"}
    assert task_store.get_chat(cid) is None
    assert "failed" not in caplog.text


def test_a_new_shared_only_chat_carries_no_pick_below_the_editor_tier(temp_db, _as):
    """On creation, a Shared-only chat runs as the
    agent, so a caller below the editor tier never leaves a permission mode
    of its own on the shared row; an editor's pick stays."""
    agent_store.create_agent("so-new", "SO", collaborative=False, default_scope="agent")
    contributor = _user("user-c3", agent_role="contributor", agents=("so-new",))
    editor = _user("user-e3", agent_role="editor", agents=("so-new",))
    _as(contributor)
    r = client.post("/v1/chats", json={"agent": "so-new", "permission_mode": "dontAsk"})
    assert r.status_code == 200, r.text
    assert task_store.get_chat(r.json()["chat"]["id"])["permission_mode"] == "default"
    _as(editor)
    r = client.post("/v1/chats", json={"agent": "so-new", "permission_mode": "acceptEdits"})
    assert task_store.get_chat(r.json()["chat"]["id"])["permission_mode"] == "acceptEdits"

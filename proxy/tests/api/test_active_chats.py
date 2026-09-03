"""GET /v1/chats/active — the cross-agent "Active now" widget seed.

Covers the visibility matrix (chat rows assignment-scoped, task rows gated on
the RUN — the same gates for the streaming set AND the warming backfill), the
pump+interactive union/dedupe, the warmup-registry warming rows, that the
literal path isn't shadowed by the /v1/chats/{chat_id} param route, and the
two deterministic-title broadcast sites the widget's title path rides on.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import app
from auth.providers import UserContext, get_current_user
from core.session import warmup_registry
from storage import database as task_store

client = TestClient(app)

AGENT = "agent-widget"
OTHER_AGENT = "agent-other"


@pytest.fixture(autouse=True)
def _clear_inflight_warmups():
    """The warmup registry is module-global state — a leaked entry would
    bleed a warming row into every later test's seed."""
    warmup_registry._inflight.clear()
    yield
    warmup_registry._inflight.clear()


def _user(sub="user-alice", role="member", agents=(AGENT,)):
    return UserContext(
        sub=sub, email=f"{sub}@test.com", name=sub, role=role,
        agents=list(agents), agent_roles={a: "editor" for a in agents},
    )


def _mk_chat(owner: str, *, agent: str = AGENT, title: str = "") -> str:
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, owner, agent)
    if title:
        task_store.update_chat(cid, title=title)
    return cid


def _mk_task_run_chat(owner: str, *, agent: str = AGENT, scope: str = "agent",
                      created_by: str | None = None, with_run: bool = True,
                      title: str = "", task_id: str = "t-nightly") -> str:
    """A scheduler task-run chat (id ``task-run-…``) plus its task_runs row,
    mirroring the scheduler's fire path (chat_id = f"task-{run_id}")."""
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    cid = f"task-{run_id}"
    task_store.create_chat(cid, owner, agent)
    if title:
        task_store.update_chat(cid, title=title)
    if with_run:
        task_store.create_run(run_id, task_id, agent, "schedule", None,
                              "check backups", scope=scope, created_by=created_by)
    return cid


@pytest.fixture
def _as(monkeypatch):
    """Authenticate as a given user + pin the two streaming registries."""
    def setup(user: UserContext, pump: list[str], interactive: list[str] = ()):
        app.dependency_overrides[get_current_user] = lambda: user
        import core.session.session_state as ss
        import core.session.interactive_session as isess
        monkeypatch.setattr(ss, "streaming_chat_ids", lambda: list(pump))
        monkeypatch.setattr(isess, "streaming_chat_ids", lambda: list(interactive))
    yield setup
    app.dependency_overrides.pop(get_current_user, None)


def test_own_streaming_chat_visible_with_metadata(temp_db, _as):
    cid = _mk_chat("user-alice", title="Refactor the login flow")
    _as(_user(), pump=[cid])
    rows = client.get("/v1/chats/active").json()["chats"]
    assert [r["id"] for r in rows] == [cid]
    r = rows[0]
    assert r["agent"] == AGENT
    assert r["title"] == "Refactor the login flow"
    assert r["status"] == "streaming"
    assert r["owner_is_shared"] is False


def test_other_users_personal_chat_hidden(temp_db, _as):
    cid = _mk_chat("user-bob")
    _as(_user(sub="user-alice"), pump=[cid])
    assert client.get("/v1/chats/active").json()["chats"] == []


def test_shared_only_chat_visible_to_assigned_user_only(temp_db, _as):
    # Shared-only agents collapse to the synthetic agent:: owner — any user
    # ASSIGNED to the agent sees its active chat; unassigned users don't.
    cid = _mk_chat(f"agent::{AGENT}", agent=AGENT)
    _as(_user(sub="user-alice", agents=(AGENT,)), pump=[cid])
    rows = client.get("/v1/chats/active").json()["chats"]
    assert [r["id"] for r in rows] == [cid]
    assert rows[0]["owner_is_shared"] is True

    _as(_user(sub="user-carol", agents=(OTHER_AGENT,)), pump=[cid])
    assert client.get("/v1/chats/active").json()["chats"] == []


def test_admin_widget_scoped_to_assigned_agents(temp_db, _as):
    # The widget is assignment-scoped even for admins: an admin CAN open any
    # chat (can_access_chat), but the widget must not advertise agents they
    # never added (live-observed 2026-07-11: the sample agents' unread chats
    # on the admin's widget). Own chats always show; other users' personal
    # chats never — the admin audit pages are the full-view surfaces.
    own = _mk_chat("user-admin")
    foreign = _mk_chat("user-bob")
    shared = _mk_chat(f"agent::{AGENT}")
    _as(_user(sub="user-admin", role="admin", agents=()),
        pump=[own, foreign], interactive=[shared])
    ids = {r["id"] for r in client.get("/v1/chats/active").json()["chats"]}
    assert ids == {own}


def test_admin_sees_shared_chats_of_added_agents(temp_db, _as):
    shared = _mk_chat(f"agent::{AGENT}")
    _as(_user(sub="user-admin", role="admin", agents=(AGENT,)),
        pump=[shared])
    ids = {r["id"] for r in client.get("/v1/chats/active").json()["chats"]}
    assert ids == {shared}


def test_pump_and_interactive_union_dedupe_and_unknown_skip(temp_db, _as):
    cid = _mk_chat("user-alice")
    # Same id in both registries + a cid with no chat row: one row, no crash.
    _as(_user(), pump=[cid, "no-such-chat"], interactive=[cid, ""])
    rows = client.get("/v1/chats/active").json()["chats"]
    assert [r["id"] for r in rows] == [cid]


def test_task_run_chats_report_source_type_task(temp_db, _as):
    """Task-run chats are created with the DEFAULT source_type ('chat') —
    the id prefix is the durable marker, and the widget needs the row typed
    as 'task' (purple identity + task-history click-through)."""
    cid = _mk_task_run_chat("user-alice", scope="user", created_by="user-alice",
                            title="Nightly digest")
    plain = _mk_chat("user-alice")
    _as(_user(), pump=[cid, plain])
    rows = {r["id"]: r for r in client.get("/v1/chats/active").json()["chats"]}
    assert rows[cid]["source_type"] == "task"
    assert rows[plain]["source_type"] == "chat"


def test_task_row_titled_by_task_name(temp_db, _as):
    """Task rows are labeled by the task's NAME (matching the sidebar's task
    mode); the chat title — the prompt's first line until the LLM upgrade —
    is only the fallback for runs whose dynamic task row is already gone
    (one-time tasks hard-delete after firing)."""
    task_store.create_dynamic_task(
        "dyn-digest", AGENT, "Nightly digest", "p", "cli", "scheduled",
        "0 9 * * *", None, None, 3600, "user-alice", scope="user")
    named = _mk_task_run_chat("user-alice", scope="user", created_by="user-alice",
                              task_id="dyn-digest", title="check the backups and")
    orphan = _mk_task_run_chat("user-alice", scope="user", created_by="user-alice",
                               title="one-off prompt line")
    _as(_user(), pump=[named, orphan])
    rows = {r["id"]: r for r in client.get("/v1/chats/active").json()["chats"]}
    assert rows[named]["title"] == "Nightly digest"
    assert rows[orphan]["title"] == "one-off prompt line"


def test_task_row_hidden_when_history_would_be_empty(temp_db, _as):
    """A task row routes to the per-agent Task History, which scopes user runs
    to their creator with NO admin bypass (the admin audit page is the full-view
    surface). Emitting the row to an admin whose History would be empty is the
    visibility-contract break: the row must follow the destination's rule, not
    can_access_chat's blanket admin access."""
    cid = _mk_task_run_chat("user-bob", scope="user", created_by="user-bob")
    _as(_user(sub="user-admin", role="admin", agents=()), pump=[cid])
    assert client.get("/v1/chats/active").json()["chats"] == []


def test_task_row_visible_to_run_creator(temp_db, _as):
    cid = _mk_task_run_chat("user-alice", scope="user", created_by="user-alice")
    _as(_user(sub="user-alice"), pump=[cid])
    assert [r["id"] for r in client.get("/v1/chats/active").json()["chats"]] == [cid]


def test_agent_scoped_task_row_follows_agent_access(temp_db, _as):
    # Scheduler agent-scope runs get the synthetic task:: owner. The run lists
    # in the Task History of every user with agent access — the active row
    # follows the same rule (and stays hidden without agent access).
    cid = _mk_task_run_chat(f"task::{AGENT}", scope="agent")
    _as(_user(sub="user-alice", agents=(AGENT,)), pump=[cid])
    assert [r["id"] for r in client.get("/v1/chats/active").json()["chats"]] == [cid]

    _as(_user(sub="user-carol", agents=(OTHER_AGENT,)), pump=[cid])
    assert client.get("/v1/chats/active").json()["chats"] == []


def test_task_row_without_run_row_hidden(temp_db, _as):
    # No task_runs row (deleted / never created) → the History page has
    # nothing to show, so the widget must not emit the row either.
    cid = _mk_task_run_chat("user-alice", with_run=False)
    _as(_user(), pump=[cid])
    assert client.get("/v1/chats/active").json()["chats"] == []


def test_empty_registries_return_empty_list(temp_db, _as):
    _as(_user(), pump=[])
    assert client.get("/v1/chats/active").json() == {"chats": []}


def test_literal_path_not_shadowed_by_chat_id_route(temp_db, _as):
    # /v1/chats/active must hit the widget endpoint, not resolve "active" as a
    # chat_id in /v1/chats/{chat_id} (which would 404).
    _as(_user(), pump=[])
    resp = client.get("/v1/chats/active")
    assert resp.status_code == 200 and "chats" in resp.json()


# ---------------------------------------------------------------------------
# Finished-unread backfill (status='finished' rows)
# ---------------------------------------------------------------------------

def _finish(cid: str, *, hours_ago: float = 1.0) -> None:
    """Stamp a finished response on a chat (last_response_at N hours ago)."""
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    task_store.update_chat(cid, last_response_at=ts)


def test_finished_unread_chat_backfills(temp_db, _as):
    cid = _mk_chat("user-alice", title="Done but unseen")
    _finish(cid)
    _as(_user(), pump=[])
    rows = client.get("/v1/chats/active").json()["chats"]
    assert [r["id"] for r in rows] == [cid]
    assert rows[0]["status"] == "finished"
    assert rows[0]["unread"] is True


def test_finished_read_chat_stays_out(temp_db, _as):
    cid = _mk_chat("user-alice")
    _finish(cid)
    task_store.mark_chat_read(cid, "user-alice")
    _as(_user(), pump=[])
    assert client.get("/v1/chats/active").json()["chats"] == []


def test_finished_older_than_window_ages_out(temp_db, _as):
    cid = _mk_chat("user-alice")
    _finish(cid, hours_ago=72)
    _as(_user(), pump=[])
    assert client.get("/v1/chats/active").json()["chats"] == []


def test_finished_unread_respects_chat_visibility(temp_db, _as):
    cid = _mk_chat("user-bob")  # someone else's personal chat
    _finish(cid)
    _as(_user(), pump=[])
    assert client.get("/v1/chats/active").json()["chats"] == []


def test_finished_unread_backfill_is_assignment_scoped_for_admins(temp_db, _as):
    # The live-observed bug: an admin's widget showed finished-unread chats of
    # sample agents they never added. Backfill honors the same assignment
    # scope as the streaming set.
    unassigned = _mk_chat(f"agent::{OTHER_AGENT}", agent=OTHER_AGENT)
    _finish(unassigned)
    assigned = _mk_chat(f"agent::{AGENT}", agent=AGENT)
    _finish(assigned)
    _as(_user(sub="user-admin", role="admin", agents=(AGENT,)), pump=[])
    rows = client.get("/v1/chats/active").json()["chats"]
    assert [r["id"] for r in rows] == [assigned]


def test_task_run_chats_never_backfill(temp_db, _as):
    # Task-run chats have no read markers — every run would read unread
    # forever. The widget keeps in-session task rows via the client store.
    cid = _mk_task_run_chat("user-alice")
    _finish(cid)
    _as(_user(), pump=[])
    assert client.get("/v1/chats/active").json()["chats"] == []


def test_streaming_row_wins_over_backfill_dupe(temp_db, _as):
    # A chat both streaming NOW and carrying an unread older response must
    # appear once, as streaming (the dedupe `seen` set).
    cid = _mk_chat("user-alice")
    _finish(cid)
    _as(_user(), pump=[cid])
    rows = client.get("/v1/chats/active").json()["chats"]
    assert [r["id"] for r in rows] == [cid]
    assert rows[0]["status"] == "streaming"


# ---------------------------------------------------------------------------
# Warming rows (in-flight warmup registry backfill)
# ---------------------------------------------------------------------------

def _warming(cid: str, *, user_sub: str = "user-alice", agent: str = AGENT) -> None:
    """Register an in-flight warmup exactly as _handle_warmup does (module-
    global dict entry) — sidesteps the async register() for sync tests."""
    warmup_registry._inflight[cid] = warmup_registry.InflightWarmup(
        chat_id=cid, user_sub=user_sub, agent=agent)


def test_own_warming_chat_included(temp_db, _as):
    # A warming chat has NO open turn yet, but its deterministic title is
    # already persisted at send-time — the widget seed must carry it so the
    # strip's row never sticks at "New chat" through the warmup window.
    cid = _mk_chat("user-alice", title="Deploy the staging fix")
    _warming(cid)
    _as(_user(), pump=[])
    rows = client.get("/v1/chats/active").json()["chats"]
    assert [r["id"] for r in rows] == [cid]
    assert rows[0]["status"] == "warming"
    assert rows[0]["title"] == "Deploy the staging fix"
    assert rows[0]["source_type"] == "chat"


def test_warming_row_of_other_users_personal_chat_hidden(temp_db, _as):
    cid = _mk_chat("user-bob")
    _warming(cid, user_sub="user-bob")
    _as(_user(sub="user-alice"), pump=[])
    assert client.get("/v1/chats/active").json()["chats"] == []


def test_warming_audience_is_the_chat_row_not_the_warmer(temp_db, _as):
    # The registry entry's user_sub is whoever STARTED the warmup — a shared
    # agent's chat warmed by user A must still reach user B (any assigned
    # user), and must NOT leak to users without the agent. Visibility is
    # re-derived from the chat row, same gates as the streaming set.
    cid = _mk_chat(f"agent::{AGENT}", agent=AGENT)
    _warming(cid, user_sub="user-alice")
    _as(_user(sub="user-bob", agents=(AGENT,)), pump=[])
    rows = client.get("/v1/chats/active").json()["chats"]
    assert [r["id"] for r in rows] == [cid]
    assert rows[0]["status"] == "warming"
    assert rows[0]["owner_is_shared"] is True

    _as(_user(sub="user-carol", agents=(OTHER_AGENT,)), pump=[])
    assert client.get("/v1/chats/active").json()["chats"] == []


def test_warming_task_rewarm_row_follows_run_gates(temp_db, _as):
    # A task re-warm (continue a finished run's chat) registers the task-run
    # chat as warming — the row keeps the task-row contract: RUN visibility
    # (creator only for user-scope, no admin bypass) + task-NAME titling.
    task_store.create_dynamic_task(
        "dyn-rewarm", AGENT, "Nightly digest", "p", "cli", "scheduled",
        "0 9 * * *", None, None, 3600, "user-alice", scope="user")
    cid = _mk_task_run_chat("user-alice", scope="user", created_by="user-alice",
                            task_id="dyn-rewarm", title="prompt first line")
    _warming(cid)
    _as(_user(sub="user-alice"), pump=[])
    rows = client.get("/v1/chats/active").json()["chats"]
    assert [r["id"] for r in rows] == [cid]
    assert rows[0]["status"] == "warming"
    assert rows[0]["title"] == "Nightly digest"
    assert rows[0]["source_type"] == "task"

    _as(_user(sub="user-admin", role="admin", agents=()), pump=[])
    assert client.get("/v1/chats/active").json()["chats"] == []


def test_warming_registry_entry_without_chat_row_skipped(temp_db, _as):
    _warming("no-such-chat")
    _as(_user(), pump=[])
    assert client.get("/v1/chats/active").json()["chats"] == []


def test_streaming_row_wins_over_warming_dupe(temp_db, _as):
    # Turn opened while the registry entry lingers (unregister precedes
    # kick/submit, but stay defensive): one row, streaming.
    cid = _mk_chat("user-alice")
    _warming(cid)
    _as(_user(), pump=[cid])
    rows = client.get("/v1/chats/active").json()["chats"]
    assert [r["id"] for r in rows] == [cid]
    assert rows[0]["status"] == "streaming"


# ---------------------------------------------------------------------------
# Deterministic-title broadcast sites — the widget's title path rides the
# `title_updated` broadcast, so both first-prompt persist sites must fan out.
# ---------------------------------------------------------------------------

def _title_broadcast_recorder(monkeypatch) -> list:
    calls: list = []
    from services.notifications import notification_manager as nm
    monkeypatch.setattr(nm, "broadcast_chat_title",
                        lambda *a, **k: calls.append((a, k)))
    return calls


def test_persist_first_prompt_broadcasts_inside_not_title_guard(temp_db, monkeypatch):
    from ws.dashboard_chat import ChatController

    calls = _title_broadcast_recorder(monkeypatch)
    ctrl = object.__new__(ChatController)
    ctrl.user_sub = "user-alice"
    ctrl.notify_connection_id = "conn-test"

    cid = _mk_chat("user-alice")
    ctrl._persist_first_prompt(cid, "deploy the fix")
    assert task_store.get_chat(cid)["title"] == "deploy the fix"
    assert calls == [(("user-alice", cid, "deploy the fix"), {"agent": AGENT})]

    # Cold re-send of an already-titled chat re-runs the persist — the
    # broadcast must stay INSIDE the not-title guard (no rename churn).
    ctrl._persist_first_prompt(cid, "a different prompt")
    assert task_store.get_chat(cid)["title"] == "deploy the fix"
    assert len(calls) == 1


def test_first_turn_title_branch_broadcasts_and_keeps_socket_send(temp_db, monkeypatch):
    # The _handle_chat titling branch must BOTH keep the per-socket
    # title_updated (the sending socket's notify queue drains only between
    # turns — the broadcast alone would leave that tab untitled for the whole
    # first turn) AND broadcast to every other viewer.
    from core.events.common_events import CommonEvent, TEXT, DONE
    from tests.fixtures.ws_dashboard_harness import (
        FakeExecutionLayer, dashboard_connection, drain_startup,
        make_test_agent, run_ws_scenario, session_cookie, set_username,
        stub_dashboard_seams, warm_new_chat,
    )

    layer = FakeExecutionLayer()
    layer.turn_events = [CommonEvent(type=TEXT, data={"content": "ok"}),
                         CommonEvent(type=DONE, data={})]
    stub_dashboard_seams(monkeypatch, layer)
    slug = make_test_agent()
    set_username("user-admin", "admin")
    calls = _title_broadcast_recorder(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await warm_new_chat(ws, layer, slug)
            ws.client_send({"type": "chat", "text": "deploy the fix",
                            "chat_id": chat_id})
            socket_titles = []
            while True:
                frame = await ws.next_frame()
                if frame.get("type") == "title_updated":
                    socket_titles.append(frame)
                if frame.get("type") == "turn_complete":
                    break
            assert socket_titles == [{"type": "title_updated",
                                      "chat_id": chat_id,
                                      "title": "deploy the fix"}]
            assert calls == [(("user-admin", chat_id, "deploy the fix"),
                              {"agent": slug})]
            ws.client_send({"type": "close"})
    run_ws_scenario(scenario)

"""Chat-history pagination + restore (newest-page + lazy scroll-back).

Covers the cursor (get_chat_messages before_id / get_chat_messages_page has_more),
the window-independent restore snapshot (get_last_todo_snapshot — native TodoWrite +
the synthesized panel_only block, robust to '' event_data; the cost gauge's word,
``restore.cost``), the id-based cutoff helper, and the GET /v1/chats/{id}?before_id=
older-page endpoint.
"""

import asyncio
import json

from fastapi.testclient import TestClient

from app import app
from auth.providers import UserContext, get_current_user
from core.events import tool_roles
from storage import database as db

client = TestClient(app)


def _mk_chat(cid="c-pag", n=5):
    db.create_chat(cid, "user-admin", "agent-x")
    ids = []
    for i in range(n):
        ids.append(db.add_chat_message(cid, "user", f"msg {i}"))
    return cid, ids


# --- cursor + has_more ---------------------------------------------------------

def test_page_newest_and_has_more(temp_db):
    cid, ids = _mk_chat(n=5)
    rows, has_more = db.get_chat_messages_page(cid, 2)
    assert [r["content"] for r in rows] == ["msg 3", "msg 4"]  # newest 2, chronological
    assert has_more is True


def test_page_before_id_walks_back(temp_db):
    cid, ids = _mk_chat(n=5)
    page1, more1 = db.get_chat_messages_page(cid, 2)             # msg 3, msg 4
    page2, more2 = db.get_chat_messages_page(cid, 2, before_id=page1[0]["id"])
    assert [r["content"] for r in page2] == ["msg 1", "msg 2"]
    assert more2 is True
    page3, more3 = db.get_chat_messages_page(cid, 2, before_id=page2[0]["id"])
    assert [r["content"] for r in page3] == ["msg 0"]            # only the oldest left
    assert more3 is False


def test_page_exact_fit_no_more(temp_db):
    cid, _ = _mk_chat(n=2)
    rows, has_more = db.get_chat_messages_page(cid, 2)
    assert len(rows) == 2 and has_more is False


def test_last_chat_message_id(temp_db):
    cid, ids = _mk_chat(n=3)
    assert db.get_last_chat_message_id(cid) == max(ids)
    assert db.get_last_chat_message_id("nope") == 0


# --- restore snapshot ----------------------------------------------------------

def _todo_event(cid, todos, *, panel_only=False):
    block = {"type": "tool", "name": "TodoWrite", "tool_input": {"todos": todos}}
    if panel_only:
        block["panel_only"] = True
    return db.add_chat_message(cid, "event", "", event_type="tool", event_data=json.dumps(block))


def test_last_todo_snapshot_native(temp_db):
    db.create_chat("c-todo", "user-admin", "agent-x")
    _todo_event("c-todo", [{"content": "a", "status": "completed"}])
    _todo_event("c-todo", [{"content": "a", "status": "completed"}, {"content": "b", "status": "pending"}])
    snap = db.get_last_todo_snapshot("c-todo", tool_roles.TODO_SNAPSHOT)
    assert [t["content"] for t in snap] == ["a", "b"]           # the LATEST snapshot


def test_last_todo_snapshot_finds_panel_only(temp_db):
    db.create_chat("c-task", "user-admin", "agent-x")
    _todo_event("c-task", [{"content": "x", "status": "in_progress"}], panel_only=True)
    snap = db.get_last_todo_snapshot("c-task", tool_roles.TODO_SNAPSHOT)
    assert [t["content"] for t in snap] == ["x"]


def test_last_todo_snapshot_ignores_empty_event_data(temp_db):
    # '' / non-JSON event_data on sibling rows must NOT break the LIKE+parse query.
    db.create_chat("c-mix", "user-admin", "agent-x")
    db.add_chat_message("c-mix", "user", "hi")                  # event_data ''
    db.add_chat_message("c-mix", "event", "", event_type="system", event_data="")
    _todo_event("c-mix", [{"content": "real", "status": "pending"}])
    db.add_chat_message("c-mix", "assistant", "ok")            # newer non-todo row
    assert [t["content"] for t in db.get_last_todo_snapshot("c-mix", tool_roles.TODO_SNAPSHOT)] == ["real"]


def test_last_todo_snapshot_none(temp_db):
    db.create_chat("c-empty", "user-admin", "agent-x")
    db.add_chat_message("c-empty", "user", "hi")
    assert db.get_last_todo_snapshot("c-empty", tool_roles.TODO_SNAPSHOT) == []


# --- the cost gauge's word (restore.cost) --------------------------------------

def _turn(cid, cost, **flag):
    return db.add_chat_message(cid, "event", "", event_type="metadata", event_data=json.dumps(
        {"type": "metadata", "cost_usd": cost, **flag}))


def _fee(cid, cost):
    return db.add_chat_message(cid, "event", "", event_type="mcp_cost", event_data=json.dumps(
        {"type": "mcp_cost", "cost_usd": cost, "cost_billed": True, "tool": "generate_image"}))


def _steps(cid, n):
    for i in range(n):
        db.add_chat_message(cid, "event", "", event_type="tool", event_data=json.dumps(
            {"type": "tool", "name": "Bash", "tool_input": {"command": f"step {i}"}}))


def test_restore_cost_reads_past_the_window(temp_db):
    from ws.dashboard import _CHAT_PAGE, _build_chat_restore
    db.create_chat("c-cost", "user-admin", "agent-x")
    _fee("c-cost", 0.12)
    _turn("c-cost", 93.53, cost_billed=False)
    _steps("c-cost", _CHAT_PAGE + 10)  # a long turn: its own steps fill the page
    rows, has_more = db.get_chat_messages_page("c-cost", _CHAT_PAGE)
    assert has_more and not any(r["event_type"] in ("metadata", "mcp_cost") for r in rows)
    assert _build_chat_restore("c-cost")["cost"] == {"billed": False, "fees": 0.12}


def test_restore_cost_follows_the_newest_turn(temp_db):
    from ws.dashboard import _build_chat_restore
    db.create_chat("c-kind", "user-admin", "agent-x")
    _turn("c-kind", 5.0, cost_billed=False)
    _fee("c-kind", 0.25)
    _turn("c-kind", 0.4, cost_billed=True)
    _fee("c-kind", 0.5)
    assert _build_chat_restore("c-kind")["cost"] == {"billed": True, "fees": 0.75}
    _turn("c-kind", 1.0, cost_billed=False)
    assert _build_chat_restore("c-kind")["cost"] == {"billed": False, "fees": 0.75}


def test_restore_cost_without_a_flagged_turn_is_shown(temp_db):
    from ws.dashboard import _build_chat_restore
    db.create_chat("c-none", "user-admin", "agent-x")
    db.add_chat_message("c-none", "user", "hi")
    assert _build_chat_restore("c-none")["cost"] == {"billed": True, "fees": 0}
    # A turn persisted before the flag existed keeps showing; an unreadable
    # row counts nothing and decides nothing.
    _turn("c-none", 0.3)
    db.add_chat_message("c-none", "event", "", event_type="metadata", event_data="{")
    db.add_chat_message("c-none", "event", "", event_type="metadata", event_data="[1]")
    db.add_chat_message("c-none", "event", "", event_type="mcp_cost", event_data="")
    db.add_chat_message("c-none", "event", "", event_type="mcp_cost", event_data="[1]")
    for junk in ("x", -1, None):
        db.add_chat_message("c-none", "event", "", event_type="mcp_cost",
                            event_data=json.dumps({"type": "mcp_cost", "cost_usd": junk}))
    assert _build_chat_restore("c-none")["cost"] == {"billed": True, "fees": 0}


def test_restore_cost_counts_the_sibling_runs(temp_db):
    from ws.dashboard import _build_chat_restore
    db.create_chat("task-r1", "user-admin", "agent-x")
    db.create_chat("task-r2", "user-admin", "agent-x")
    _fee("task-r1", 0.1)
    _turn("task-r1", 2.0, cost_billed=True)
    _fee("task-r2", 0.2)
    _turn("task-r2", 3.0, cost_billed=False)
    assert _build_chat_restore("task-r1")["cost"] == {"billed": True, "fees": 0.1}
    assert _build_chat_restore("task-r1", ["task-r2"])["cost"] == {"billed": False, "fees": 0.3}


def test_restore_cost_asks_a_live_turns_session(temp_db, monkeypatch):
    from core.events import stream_pump
    from services.engines import subscription_pool
    from ws.dashboard import _build_chat_restore

    class _Pump:
        def __init__(self, session_id, done=False):
            self.session_id, self.is_done = session_id, done

    db.create_chat("c-live", "user-admin", "agent-x")
    _turn("c-live", 0.4, cost_billed=True)
    asked = []
    monkeypatch.setattr(subscription_pool, "session_cost_billed",
                        lambda sid: asked.append(sid) or False)
    # The turn in flight is on a subscription: its account decides, the
    # newest finished turn does not.
    monkeypatch.setitem(stream_pump._active_pumps, "c-live", _Pump("sess-live"))
    assert _build_chat_restore("c-live")["cost"]["billed"] is False
    assert asked == ["sess-live"]
    # A pump past its turn's end is no turn in flight.
    monkeypatch.setitem(stream_pump._active_pumps, "c-live", _Pump("sess-live", done=True))
    assert _build_chat_restore("c-live")["cost"]["billed"] is True
    assert asked == ["sess-live"]


def test_a_paged_history_frame_carries_the_cost_word(temp_db, monkeypatch):
    import ws.dashboard  # noqa: F401  (assembles the mixins first)
    from tests.fixtures.ws_dashboard_harness import (
        FakeExecutionLayer,
        dashboard_connection,
        drain_startup,
        make_test_agent,
        run_ws_scenario,
        session_cookie,
        stub_dashboard_seams,
    )
    from ws.dashboard import _CHAT_PAGE
    stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
    slug = make_test_agent()
    db.create_chat("c-frame", "user-admin", slug, model="test-model", execution_path="claude-code-cli")
    db.update_chat("c-frame", total_cost=93.65)
    db.add_chat_message("c-frame", "user", "go", author_sub="user-admin")
    _turn("c-frame", 93.65, cost_billed=False)
    db.add_chat_message("c-frame", "user", "again", author_sub="user-admin")
    _steps("c-frame", _CHAT_PAGE + 5)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            ws.client_send({"type": "resume_chat", "chat_id": "c-frame"})
            for _ in range(12):
                frame = await ws.next_frame()
                if frame.get("type") == "chat_history":
                    break
            assert frame["has_more"] is True and frame["total_cost"] == 93.65
            assert not any(m["event_type"] == "metadata" for m in frame["messages"])
            assert frame["restore"]["cost"] == {"billed": False, "fees": 0}
            ws.client_send({"type": "close"})
    run_ws_scenario(scenario)


# --- REST older-page endpoint --------------------------------------------------

def _admin():
    async def _stub():
        return UserContext(sub="user-admin", email="a@t.com", name="A", role="admin")
    return _stub


def test_rest_before_id_returns_older_page(temp_db):
    cid, ids = _mk_chat("c-rest", n=5)
    app.dependency_overrides[get_current_user] = _admin()
    try:
        # default snapshot still returns the chat + messages (unchanged contract)
        full = client.get(f"/v1/chats/{cid}").json()
        assert "chat" in full and len(full["messages"]) == 5
        # older page: id < ids[2] (msg 2) → msg 0, msg 1
        page = client.get(f"/v1/chats/{cid}?before_id={ids[2]}&limit=2").json()
        assert [m["content"] for m in page["messages"]] == ["msg 0", "msg 1"]
        assert page["has_more"] is False and "chat" not in page
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _off_loop(fn):
    def guarded(*a, **kw):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return fn(*a, **kw)
        raise AssertionError(f"{fn.__name__} ran on the event loop")
    return guarded


def test_rest_reads_the_chat_and_its_page_off_the_loop(temp_db, monkeypatch):
    cid, _ids = _mk_chat("c-lane", n=3)
    monkeypatch.setattr(db, "get_chat", _off_loop(db.get_chat))
    monkeypatch.setattr(db, "get_chat_messages_page", _off_loop(db.get_chat_messages_page))
    app.dependency_overrides[get_current_user] = _admin()
    try:
        body = client.get(f"/v1/chats/{cid}").json()
        assert body["chat"]["id"] == cid and len(body["messages"]) == 3
    finally:
        app.dependency_overrides.pop(get_current_user, None)

"""Chat-history pagination + restore (newest-page + lazy scroll-back).

Covers the cursor (get_chat_messages before_id / get_chat_messages_page has_more),
the window-independent restore snapshot (get_last_todo_snapshot — native TodoWrite +
the synthesized panel_only block, robust to '' event_data), the id-based cutoff
helper, and the GET /v1/chats/{id}?before_id= older-page endpoint.
"""

import json

from fastapi.testclient import TestClient

from app import app
from auth.providers import UserContext, get_current_user
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
    snap = db.get_last_todo_snapshot("c-todo")
    assert [t["content"] for t in snap] == ["a", "b"]           # the LATEST snapshot


def test_last_todo_snapshot_finds_panel_only(temp_db):
    db.create_chat("c-task", "user-admin", "agent-x")
    _todo_event("c-task", [{"content": "x", "status": "in_progress"}], panel_only=True)
    snap = db.get_last_todo_snapshot("c-task")
    assert [t["content"] for t in snap] == ["x"]


def test_last_todo_snapshot_ignores_empty_event_data(temp_db):
    # '' / non-JSON event_data on sibling rows must NOT break the LIKE+parse query.
    db.create_chat("c-mix", "user-admin", "agent-x")
    db.add_chat_message("c-mix", "user", "hi")                  # event_data ''
    db.add_chat_message("c-mix", "event", "", event_type="system", event_data="")
    _todo_event("c-mix", [{"content": "real", "status": "pending"}])
    db.add_chat_message("c-mix", "assistant", "ok")            # newer non-todo row
    assert [t["content"] for t in db.get_last_todo_snapshot("c-mix")] == ["real"]


def test_last_todo_snapshot_none(temp_db):
    db.create_chat("c-empty", "user-admin", "agent-x")
    db.add_chat_message("c-empty", "user", "hi")
    assert db.get_last_todo_snapshot("c-empty") == []


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

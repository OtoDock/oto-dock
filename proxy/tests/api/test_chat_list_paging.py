"""GET /v1/chats pages: without a cursor the first page as before plus
``has_more``; with ``before_updated_at`` and ``before_chat_id`` (the last row
shown) the rows after it. Both kinds."""

import pytest
from fastapi.testclient import TestClient

from app import app
from auth.providers import UserContext, get_current_user
from storage import database as task_store
from storage.pg import get_conn

client = TestClient(app)

AGENT = "agent-pages"
SUB = "user-pages"


def _user():
    return UserContext(sub=SUB, email=f"{SUB}@test.com", name=SUB, role="member",
                       agents=[AGENT], agent_roles={AGENT: "editor"})


@pytest.fixture(autouse=True)
def _as_user():
    app.dependency_overrides[get_current_user] = _user
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _stamp(cid: str, at: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE chats SET updated_at=%s WHERE id=%s", (at, cid))
        conn.commit()


def _at(i: int) -> str:
    return f"2026-10-01T10:00:{i:02d}.250000+00:00"


def _chats(n: int) -> list[str]:
    ids = []
    for i in range(n):
        cid = f"chat-{i:02d}"
        task_store.create_chat(cid, SUB, AGENT)
        _stamp(cid, _at(i))
        ids.append(cid)
    return ids


def _task_chats(n: int) -> list[str]:
    ids = []
    for i in range(n):
        run_id = f"run-{i:02d}"
        cid = f"task-{run_id}"
        task_store.create_chat(cid, SUB, AGENT)
        task_store.create_run(run_id, "t-p", AGENT, "schedule", None, "p",
                              scope="user", created_by=SUB)
        task_store.update_run(run_id, status="completed", chat_id=cid,
                              started_at="2026-07-12T00:00:00+00:00")
        _stamp(cid, _at(i))
        ids.append(cid)
    return ids


def _get(**params) -> dict:
    r = client.get("/v1/chats", params={"agent": AGENT, **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_first_page_keeps_its_rows_and_says_whether_more_remain():
    ids = _chats(5)
    body = _get()
    assert [c["id"] for c in body["chats"]] == list(reversed(ids))
    assert body["has_more"] is False
    assert {"can_rename", "can_delete", "can_share"} <= set(body["chats"][0])
    small = _get(limit=3)
    assert [c["id"] for c in small["chats"]] == list(reversed(ids))[:3]
    assert small["has_more"] is True


@pytest.mark.parametrize("kind,seed", [("chats", _chats), ("tasks", _task_chats)])
def test_a_cursor_page_holds_the_rows_after_it(kind, seed):
    ids = list(reversed(seed(7)))
    extra = {"kind": kind} if kind == "tasks" else {}
    first = _get(limit=3, **extra)
    assert [c["id"] for c in first["chats"]] == ids[:3]
    last = first["chats"][-1]
    second = _get(limit=3, before_updated_at=last["updated_at"],
                  before_chat_id=last["id"], **extra)
    assert [c["id"] for c in second["chats"]] == ids[3:6]
    assert second["has_more"] is True
    last = second["chats"][-1]
    third = _get(limit=3, before_updated_at=last["updated_at"],
                 before_chat_id=last["id"], **extra)
    assert [c["id"] for c in third["chats"]] == ids[6:]
    assert third["has_more"] is False


def test_a_tie_across_the_page_boundary_survives_the_plus_in_the_cursor():
    tied = "2026-10-01T10:00:00+00:00"
    for i in range(4):
        task_store.create_chat(f"tie-{i}", SUB, AGENT)
        _stamp(f"tie-{i}", tied)
    first = _get(limit=2)
    assert [c["id"] for c in first["chats"]] == ["tie-3", "tie-2"]
    # The query string carries the `+` encoded; a raw one decodes to a space.
    r = client.get(f"/v1/chats?agent={AGENT}&limit=2"
                   f"&before_updated_at=2026-10-01T10%3A00%3A00%2B00%3A00&before_chat_id=tie-2")
    assert [c["id"] for c in r.json()["chats"]] == ["tie-1", "tie-0"]


def test_one_cursor_half_alone_is_refused():
    r = client.get("/v1/chats", params={"agent": AGENT, "before_chat_id": "chat-01"})
    assert r.status_code == 400
    assert "together" in r.json()["detail"]
    r = client.get("/v1/chats", params={"agent": AGENT, "before_updated_at": _at(1)})
    assert r.status_code == 400


@pytest.mark.parametrize("at,cid", [
    ("2026-10-01T10:00:00 00:00", "chat-01"),   # a raw `+` decoded to a space
    ("2026-10-01T10:00:00\x00", "chat-01"),
    (_at(1), "chat\x0001"),
    (_at(1), "chat 01"),
    ("yesterday", "chat-01"),
])
def test_a_malformed_cursor_is_refused_before_any_read(at, cid):
    r = client.get("/v1/chats", params={"agent": AGENT, "before_updated_at": at,
                                        "before_chat_id": cid})
    assert r.status_code == 422


def test_the_task_list_still_needs_its_agent_with_a_cursor():
    r = client.get("/v1/chats", params={"kind": "tasks", "before_updated_at": _at(1),
                                        "before_chat_id": "x"})
    assert r.status_code == 400   # the task list still needs its agent

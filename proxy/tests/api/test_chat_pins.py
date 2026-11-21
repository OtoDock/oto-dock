"""A person's pinned chats over the routes: who may pin (who may open the
chat, and only a chat of their own history), the same answer for a chat
that is not theirs as for one that does not exist, unpin with no access, the
rows' flags, and the history's pinned view."""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import app
from auth.providers import UserContext, get_current_user
from storage import database as task_store
from storage.agents import agent_store
from storage.chat import chat_pins
from storage.pg import get_conn

client = TestClient(app)

AGENT = "pin-routes"
POOL = "pin-pool"


def _person(sub: str, *, role: str = "member", agent_role: str = "editor",
            agents: tuple[str, ...] = (AGENT,), **kw) -> UserContext:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("INSERT INTO users (sub, email, name, role, created_at, last_login) "
                     "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                     (sub, f"{sub}@test.com", sub, role, now, now))
        conn.commit()
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role=role,
                       agents=list(agents), agent_roles={a: agent_role for a in agents}, **kw)


@pytest.fixture
def _as():
    def setup(user: UserContext):
        app.dependency_overrides[get_current_user] = lambda: user
    yield setup
    app.dependency_overrides.pop(get_current_user, None)


def _chat(owner: str, agent: str = AGENT, **kw) -> str:
    cid = kw.pop("chat_id", "") or str(uuid.uuid4())
    task_store.create_chat(cid, owner, agent, **kw)
    return cid


def _pins() -> list[tuple[str, str]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT user_sub, chat_id FROM pinned_chats ORDER BY 1, 2").fetchall()
    return [(r["user_sub"], r["chat_id"]) for r in rows]


def _pool() -> None:
    agent_store.create_agent(POOL, "Pool", collaborative=False, default_scope="agent")


# --- who may pin ------------------------------------------------------------


def test_a_person_pins_and_unpins_their_own_chat(_as):
    me = _person("u-me")
    cid = _chat(me.sub)
    _as(me)
    r = client.put(f"/v1/chats/{cid}/pinned")
    assert r.status_code == 200 and r.json() == {"pinned": True}
    assert client.put(f"/v1/chats/{cid}/pinned").status_code == 200
    assert _pins() == [(me.sub, cid)]
    r = client.delete(f"/v1/chats/{cid}/pinned")
    assert r.status_code == 200 and r.json() == {"pinned": False}
    assert _pins() == []


def test_a_person_s_own_call_pins(_as):
    """A call on a route tied to a person is a row of their history."""
    me = _person("u-caller")
    cid = _chat(me.sub, source_type="phone")
    _as(me)
    assert client.put(f"/v1/chats/{cid}/pinned").status_code == 200


def test_another_person_s_chat_answers_as_a_chat_that_does_not_exist(_as):
    me, other = _person("u-me"), _person("u-other")
    theirs = _chat(other.sub)
    _as(me)
    refused = client.put(f"/v1/chats/{theirs}/pinned")
    missing = client.put(f"/v1/chats/{uuid.uuid4()}/pinned")
    assert refused.status_code == missing.status_code == 404
    assert refused.json() == missing.json() == {"detail": "Chat not found"}
    assert _pins() == []


def test_an_admin_opens_every_chat_but_pins_only_their_own_history(_as):
    admin, other = _person("u-admin", role="admin"), _person("u-other")
    theirs = _chat(other.sub)
    _as(admin)
    r = client.put(f"/v1/chats/{theirs}/pinned")
    assert r.status_code == 400
    assert r.json() == {"detail": "Only a chat in your chat history can be pinned."}


def test_a_task_run_s_chat_cannot_be_pinned(_as):
    me = _person("u-me")
    cid = _chat(me.sub, chat_id=f"task-{uuid.uuid4().hex}")
    _as(me)
    assert client.put(f"/v1/chats/{cid}/pinned").status_code == 400


def test_a_pool_chat_pins_for_every_member_alone(_as):
    _pool()
    contributor = _person("u-contrib", agent_role="contributor", agents=(POOL,))
    editor = _person("u-editor", agent_role="editor", agents=(POOL,))
    outsider = _person("u-out", agents=(AGENT,))
    cid = _chat(f"agent::{POOL}", POOL)
    _as(contributor)
    assert client.put(f"/v1/chats/{cid}/pinned").status_code == 200
    _as(outsider)
    assert client.put(f"/v1/chats/{cid}/pinned").status_code == 404
    _as(editor)
    listed = client.get("/v1/chats", params={"agent": POOL}).json()["chats"]
    assert [(c["id"], c["pinned"], c["can_pin"]) for c in listed] == [(cid, False, True)]
    assert client.get("/v1/chats", params={"agent": POOL, "pinned": 1}).json()["chats"] == []
    assert _pins() == [(contributor.sub, cid)]


def test_a_hidden_worker_stays_hidden_below_the_editor_tier(_as):
    """A delegated worker in the pool with an agent-scope run is the editor
    tier's: a contributor neither pins it nor sees an editor's pin of it."""
    _pool()
    contributor = _person("u-contrib", agent_role="contributor", agents=(POOL,))
    editor = _person("u-editor", agent_role="editor", agents=(POOL,))
    worker = _chat(f"agent::{POOL}", POOL)
    task_store.create_run("run-pin-worker", "t-w", POOL, "delegate", None, "w", scope="agent")
    task_store.update_run("run-pin-worker", chat_id=worker)
    _as(contributor)
    assert client.put(f"/v1/chats/{worker}/pinned").status_code == 404
    _as(editor)
    assert client.put(f"/v1/chats/{worker}/pinned").status_code == 200
    rows = client.get("/v1/chats", params={"agent": POOL, "pinned": 1}).json()["chats"]
    assert [r["id"] for r in rows] == [worker]


def test_unpin_needs_no_access_and_touches_only_the_caller_s_row(_as):
    _pool()
    me = _person("u-me", agents=(POOL,))
    other = _person("u-other", agents=(POOL,))
    cid = _chat(f"agent::{POOL}", POOL)
    chat_pins.pin(me.sub, cid)
    chat_pins.pin(other.sub, cid)
    gone = _person("u-me", agents=())  # since removed from the agent
    _as(gone)
    assert client.put(f"/v1/chats/{cid}/pinned").status_code == 404
    r = client.delete(f"/v1/chats/{cid}/pinned")
    assert r.status_code == 200 and r.json() == {"pinned": False}
    assert client.delete(f"/v1/chats/{uuid.uuid4()}/pinned").status_code == 200
    assert _pins() == [(other.sub, cid)]


def test_an_api_key_has_no_pins(_as):
    me = _person("u-me")
    cid = _chat(me.sub)
    chat_pins.pin(me.sub, cid)
    key = _person("u-me", is_api_key=True, api_key_id="k1")
    _as(key)
    assert client.put(f"/v1/chats/{cid}/pinned").status_code == 403
    assert client.delete(f"/v1/chats/{cid}/pinned").status_code == 403
    assert _pins() == [(me.sub, cid)]


# --- the flags and the view -------------------------------------------------


def _stamp(cid: str, at: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE chats SET updated_at=%s WHERE id=%s", (at, cid))
        conn.commit()


def test_the_list_and_search_carry_the_person_s_own_flags(_as):
    me = _person("u-me")
    a, b = _chat(me.sub), _chat(me.sub)
    for cid in (a, b):
        task_store.add_chat_message(cid, "user", "the word quayside")
    chat_pins.pin(me.sub, a)
    _as(me)
    listed = {c["id"]: (c["pinned"], c["can_pin"])
              for c in client.get("/v1/chats", params={"agent": AGENT}).json()["chats"]}
    assert listed == {a: (True, True), b: (False, True)}
    found = {c["id"]: c["pinned"] for c in client.get(
        "/v1/chats/search", params={"agent": AGENT, "q": "quayside"}).json()["chats"]}
    assert found == {a: True, b: False}
    only = client.get("/v1/chats/search", params={"agent": AGENT, "q": "quayside", "pinned": 1})
    assert [c["id"] for c in only.json()["chats"]] == [a]


def test_task_rows_carry_no_pin(_as):
    me = _person("u-me")
    cid = _chat(me.sub, chat_id=f"task-run-{uuid.uuid4().hex[:8]}")
    task_store.create_run(cid[len("task-"):], "t-1", AGENT, "schedule", None, "p",
                          scope="user", created_by=me.sub)
    task_store.update_run(cid[len("task-"):], status="completed", chat_id=cid,
                          started_at="2026-07-12T00:00:00+00:00")
    _as(me)
    rows = client.get("/v1/chats", params={"agent": AGENT, "kind": "tasks"}).json()["chats"]
    assert [(r["id"], r["pinned"], r["can_pin"]) for r in rows] == [(cid, False, False)]


def test_the_pinned_view_pages_in_the_history_s_order(_as):
    me = _person("u-me")
    ids = []
    for i in range(5):
        cid = _chat(me.sub)
        _stamp(cid, f"2026-10-01T10:00:0{i}.000001+00:00")
        ids.append(cid)
        chat_pins.pin(me.sub, cid)
    _chat(me.sub)  # newest, not pinned
    _as(me)
    first = client.get("/v1/chats", params={"agent": AGENT, "pinned": 1, "limit": 3}).json()
    assert [c["id"] for c in first["chats"]] == ids[:1:-1] and first["has_more"] is True
    last = first["chats"][-1]
    rest = client.get("/v1/chats", params={
        "agent": AGENT, "pinned": 1, "limit": 3,
        "before_updated_at": last["updated_at"], "before_chat_id": last["id"]}).json()
    assert [c["id"] for c in rest["chats"]] == ids[1::-1] and rest["has_more"] is False
    assert all(c["pinned"] for c in first["chats"] + rest["chats"])


def test_one_person_s_pin_never_shows_for_another_on_a_shared_agent(_as):
    _pool()
    first = _person("u-first", agents=(POOL,))
    second = _person("u-second", agent_role="contributor", agents=(POOL,))
    cid = _chat(f"agent::{POOL}", POOL)
    _as(first)
    assert client.put(f"/v1/chats/{cid}/pinned").status_code == 200
    _as(second)
    rows = client.get("/v1/chats", params={"agent": POOL}).json()["chats"]
    assert [(r["id"], r["pinned"]) for r in rows] == [(cid, False)]
    assert client.get("/v1/chats", params={"agent": POOL, "pinned": 1}).json() == {
        "chats": [], "has_more": False}


def test_the_view_takes_an_agent_and_the_chat_kind(_as):
    _as(_person("u-me"))
    assert client.get("/v1/chats", params={"pinned": 1}).status_code == 400
    r = client.get("/v1/chats", params={"agent": AGENT, "kind": "tasks", "pinned": 1})
    assert r.status_code == 400 and r.json() == {"detail": "Task runs cannot be pinned."}
    r = client.get("/v1/chats/search", params={"agent": AGENT, "kind": "tasks", "pinned": 1, "q": "x"})
    assert r.status_code == 400

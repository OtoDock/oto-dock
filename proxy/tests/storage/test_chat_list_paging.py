"""The sidebar lists page by a cursor: the last shown row's ``(updated_at,
id)``, read with a row comparison in ``updated_at DESC, id DESC`` order, so a
page holds the rows after it, ties included, and a chat that moved to the top
is never read twice."""

import uuid

from storage import database as task_store
from storage.pg import get_conn

AGENT = "agent-paging"
OWNER = "user-pager"


def _stamp(cid: str, at: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE chats SET updated_at=%s WHERE id=%s", (at, cid))
        conn.commit()


def _chat(cid: str, at: str, *, owner: str = OWNER) -> str:
    task_store.create_chat(cid, owner, AGENT)
    _stamp(cid, at)
    return cid


def _task_chat(run_id: str, at: str, *, scope: str = "user",
               created_by: str = OWNER) -> str:
    cid = f"task-{run_id}"
    task_store.create_chat(cid, created_by if scope == "user" else f"task::{AGENT}", AGENT)
    task_store.create_run(run_id, "t-paging", AGENT, "schedule", None, "p",
                          scope=scope, created_by=created_by)
    task_store.update_run(run_id, status="completed", chat_id=cid,
                          started_at="2026-07-12T00:00:00+00:00")
    _stamp(cid, at)
    return cid


def _at(i: int) -> str:
    return f"2026-10-01T10:{i // 60:02d}:{i % 60:02d}.000001+00:00"


def _cursor(row: dict) -> tuple[str, str]:
    return row["updated_at"], row["id"]


def _walk(read, page: int) -> list[str]:
    seen: list[str] = []
    before = None
    while True:
        rows = read(page, before)
        seen += [r["id"] for r in rows]
        if len(rows) < page:
            return seen
        before = _cursor(rows[-1])


def test_the_pages_after_a_cursor_cover_the_list_once():
    ids = [_chat(f"c{i:03d}", _at(i)) for i in range(23)]
    seen = _walk(lambda n, b: task_store.list_chats(OWNER, agent=AGENT, limit=n, before=b), 5)
    assert seen == list(reversed(ids))


def test_rows_tied_on_their_timestamp_are_ordered_by_id_across_a_page_boundary():
    tied = "2026-10-01T10:00:00+00:00"
    ids = [_chat(f"tie-{i}", tied) for i in range(7)]
    _chat("newest", "2026-10-02T10:00:00+00:00")
    first = task_store.list_chats(OWNER, agent=AGENT, limit=4)
    assert [r["id"] for r in first] == ["newest", "tie-6", "tie-5", "tie-4"]
    rest = task_store.list_chats(OWNER, agent=AGENT, limit=10, before=_cursor(first[-1]))
    assert [r["id"] for r in rest] == ["tie-3", "tie-2", "tie-1", "tie-0"]
    assert sorted(r["id"] for r in first + rest) == sorted(ids + ["newest"])


def test_a_chat_that_moved_to_the_top_is_not_read_again_on_the_next_page():
    for i in range(10):
        _chat(f"m{i}", _at(i))
    first = task_store.list_chats(OWNER, agent=AGENT, limit=4)
    assert [r["id"] for r in first] == ["m9", "m8", "m7", "m6"]
    _stamp("m2", "2026-10-03T00:00:00+00:00")   # a reply landed in an old chat
    rest = task_store.list_chats(OWNER, agent=AGENT, limit=10, before=_cursor(first[-1]))
    assert [r["id"] for r in rest] == ["m5", "m4", "m3", "m1", "m0"]


def test_the_cursor_keeps_the_owner_and_agent_filters():
    for i in range(6):
        _chat(f"mine{i}", _at(i))
        _chat(f"theirs{i}", _at(i), owner="user-other")
    task_store.create_chat("elsewhere", OWNER, "agent-other")
    _stamp("elsewhere", _at(2))
    rows = task_store.list_chats(OWNER, agent=AGENT, limit=10, before=_cursor(
        {"updated_at": _at(4), "id": "mine4"}))
    assert [r["id"] for r in rows] == ["mine3", "mine2", "mine1", "mine0"]


def test_hide_agent_runs_holds_with_a_cursor():
    pool = f"agent::{AGENT}"
    for i in range(5):
        _chat(f"p{i}", _at(i), owner=pool)
    worker = _chat(str(uuid.uuid4()), _at(1), owner=pool)
    task_store.create_run("run-worker", "t-w", AGENT, "delegate", None, "w",
                          scope="agent", created_by="")
    task_store.update_run("run-worker", chat_id=worker)
    before = (_at(4), "p4")
    shown = task_store.list_chats(pool, agent=AGENT, limit=10, before=before)
    hidden = task_store.list_chats(pool, agent=AGENT, limit=10, before=before,
                                   hide_agent_runs=True)
    assert worker in [r["id"] for r in shown]
    assert worker not in [r["id"] for r in hidden]
    assert [r["id"] for r in hidden] == ["p3", "p2", "p1", "p0"]


def test_task_chats_page_by_the_same_cursor():
    ids = [_task_chat(f"r{i:02d}", _at(i)) for i in range(12)]
    seen = _walk(lambda n, b: task_store.list_task_chats(AGENT, OWNER, limit=n, before=b), 5)
    assert seen == list(reversed(ids))


def test_own_only_holds_with_a_cursor():
    mine = [_task_chat(f"own{i}", _at(i)) for i in range(4)]
    _task_chat("agentrun", _at(1), scope="agent", created_by="someone")
    _task_chat("otherrun", _at(2), created_by="user-other")
    rows = task_store.list_task_chats(AGENT, OWNER, limit=10, own_only=True,
                                      before=(_at(3), mine[3]))
    assert [r["id"] for r in rows] == [mine[2], mine[1], mine[0]]
    wide = task_store.list_task_chats(AGENT, OWNER, limit=10, before=(_at(3), mine[3]))
    assert "task-agentrun" in [r["id"] for r in wide]
    assert "task-otherrun" not in [r["id"] for r in wide]


def _pin(sub: str, *ids: str) -> None:
    from storage.chat import chat_pins
    for cid in ids:
        assert chat_pins.pin(sub, cid)


PINNER = "user-viewer"


def test_the_pinned_view_pages_the_person_s_pins_in_the_history_s_order():
    ids = [_chat(f"c{i:03d}", _at(i)) for i in range(12)]
    pinned = ids[1::2]
    _pin(PINNER, *pinned)
    _pin("user-viewer2", ids[0])
    seen = _walk(lambda n, b: task_store.list_chats(OWNER, agent=AGENT, limit=n, before=b,
                                                    pinned_by=PINNER), 2)
    assert seen == list(reversed(pinned))


def test_the_pinned_view_keeps_every_condition_of_the_history():
    own = _chat("c-own", _at(1))
    another = _chat("c-another", _at(2), owner="user-elsewhere")
    task_row = _task_chat("r-pinned", _at(3))
    _pin(PINNER, own, another, task_row)
    rows = task_store.list_chats(OWNER, agent=AGENT, pinned_by=PINNER)
    assert [r["id"] for r in rows] == [own]
    assert task_store.list_chats(OWNER, agent="agent-other", pinned_by=PINNER) == []


def test_hide_agent_runs_holds_in_the_pinned_view():
    worker = _chat(str(uuid.uuid4()), _at(1))
    task_store.create_run("r-worker-pin", "t-w", AGENT, "delegate", None, "p", scope="agent")
    task_store.update_run("r-worker-pin", chat_id=worker)
    plain = _chat("c-plain", _at(2))
    _pin(PINNER, worker, plain)
    rows = task_store.list_chats(OWNER, agent=AGENT, hide_agent_runs=True, pinned_by=PINNER)
    assert [r["id"] for r in rows] == [plain]


def test_search_keeps_to_the_pins_in_the_pinned_view():
    a, b = _chat("c-search-a", _at(1)), _chat("c-search-b", _at(2))
    for cid in (a, b):
        task_store.add_chat_message(cid, "user", "a shared word: lighthouse")
    _pin(PINNER, a)
    assert {r["id"] for r in task_store.search_chats(OWNER, AGENT, "lighthouse")} == {a, b}
    assert [r["id"] for r in task_store.search_chats(OWNER, AGENT, "lighthouse",
                                                     pinned_by=PINNER)] == [a]

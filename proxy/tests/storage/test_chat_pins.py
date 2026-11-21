"""A person's pinned chats (storage/chat/chat_pins.py): the rows, what removes
them and what never does, and the table's arrival on an existing database."""

import uuid
from datetime import datetime, timedelta, timezone

from core.session import visibility
from services.infra import retention
from services.infra.retention import LiveSnapshot
from storage import database as task_store, schema
from storage.chat import chat_pins
from storage.pg import get_conn

AGENT = "pin-agent"
ME = "user-viewer"
OTHER = "user-viewer2"


def _chat(owner: str = ME, agent: str = AGENT, chat_id: str = "", **kw) -> str:
    cid = chat_id or str(uuid.uuid4())
    task_store.create_chat(cid, owner, agent, **kw)
    return cid


def _rows() -> list[tuple[str, str]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT user_sub, chat_id FROM pinned_chats ORDER BY user_sub, chat_id").fetchall()
    return [(r["user_sub"], r["chat_id"]) for r in rows]


def test_a_pin_is_one_row_and_twice_keeps_the_first_time():
    cid = _chat()
    assert chat_pins.pin(ME, cid) is True
    with get_conn() as conn:
        first = conn.execute("SELECT pinned_at FROM pinned_chats").fetchone()["pinned_at"]
    assert chat_pins.pin(ME, cid) is True
    with get_conn() as conn:
        rows = conn.execute("SELECT pinned_at FROM pinned_chats").fetchall()
    assert [r["pinned_at"] for r in rows] == [first]


def test_unpin_removes_only_that_person_s_row():
    cid = _chat()
    chat_pins.pin(ME, cid)
    chat_pins.pin(OTHER, cid)
    chat_pins.unpin(ME, cid)
    chat_pins.unpin(ME, cid)  # a second unpin is no error
    assert _rows() == [(OTHER, cid)]


def test_pinned_ids_answers_the_person_s_pins_among_the_ids():
    a, b, c = _chat(), _chat(), _chat()
    chat_pins.pin(ME, a)
    chat_pins.pin(ME, c)
    chat_pins.pin(OTHER, b)
    assert chat_pins.pinned_ids(ME, [a, b]) == {a}
    assert chat_pins.pinned_ids(ME, []) == set()
    assert chat_pins.pinned_ids("", [a, b, c]) == set()


def test_a_pin_of_a_chat_or_a_person_that_is_gone_answers_false():
    assert chat_pins.pin(ME, "no-such-chat") is False
    assert chat_pins.pin("no-such-person", _chat()) is False
    assert _rows() == []


def test_a_deleted_chat_takes_its_pins():
    cid, kept = _chat(), _chat()
    chat_pins.pin(ME, cid)
    chat_pins.pin(OTHER, cid)
    chat_pins.pin(ME, kept)
    assert task_store.delete_chat_bound(cid)[0] is True
    assert _rows() == [(ME, kept)]


def test_the_agent_s_removal_takes_the_pins_of_its_chats():
    from storage.agents import agent_store
    agent_store.create_agent(AGENT, AGENT)
    cid, elsewhere = _chat(), _chat(agent="other-agent")
    chat_pins.pin(ME, cid)
    chat_pins.pin(ME, elsewhere)
    agent_store.delete_agent(AGENT)
    assert _rows() == [(ME, elsewhere)]


def test_the_person_s_deletion_takes_their_pins():
    cid = _chat()
    chat_pins.pin(ME, cid)
    chat_pins.pin(OTHER, cid)
    assert task_store.delete_user(ME) is True
    assert _rows() == [(OTHER, cid)]


def test_a_cleaned_session_keeps_the_pin(monkeypatch):
    """The retention's session pass removes an aged chat's session and flags
    the row: the chat stays, and its pin with it."""
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    cid = _chat(owner="user-admin")
    task_store.update_chat(cid, session_id=str(uuid.uuid4()))
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE chats SET updated_at=%s WHERE id=%s", (old, cid))
        conn.commit()
    chat_pins.pin(ME, cid)
    stats = retention._run_sweep_sync(30, True, LiveSnapshot(), False)
    assert stats["chats_flagged"] == 1
    assert task_store.get_chat(cid)["pending_history_seed"] == "retention"
    assert _rows() == [(ME, cid)]


def test_only_the_store_and_the_schema_name_the_table():
    """Nothing that closes, cleans or moves a session can touch a pin: the
    rows are written by the store alone, and leave by their keys' cascades."""
    from pathlib import Path
    proxy = Path(__file__).resolve().parents[2]
    named = sorted(
        str(f.relative_to(proxy)) for f in proxy.rglob("*.py")
        if "tests" not in f.parts and "venv" not in f.parts
        and "pinned_chats" in f.read_text(encoding="utf-8", errors="replace"))
    assert named == ["storage/chat/chat_pins.py", "storage/chat/schema.py"]


def _listed_ids(owner: str, agent: str) -> set[str]:
    return {c["id"] for c in task_store.list_chats(owner, agent=agent, limit=200)}


def test_in_chat_history_says_what_the_history_lists(monkeypatch):
    """The gate's predicate and the list's own SQL, on the same rows: a
    person's own chat and own call, a task run's chat, a conversation of the
    Conversations tab, another person's chat."""
    monkeypatch.setattr(visibility, "_read_agent_mode", lambda agent: (True, "user"))
    own = _chat()
    call = _chat(source_type="phone")
    conversation = _chat(owner=visibility.PHONE_CHAT_OWNER, source_type="phone")
    task_row = _chat(chat_id=f"task-{uuid.uuid4().hex}")
    worker_task = _chat(source_type="task")
    another = _chat(owner=OTHER)
    listed = _listed_ids(ME, AGENT)
    for cid in (own, call, conversation, task_row, worker_task, another):
        chat = task_store.get_chat(cid)
        assert visibility.in_chat_history(chat, ME) is (cid in listed), cid
    assert listed == {own, call}


def test_in_chat_history_on_a_shared_only_agent(monkeypatch):
    monkeypatch.setattr(visibility, "_read_agent_mode", lambda agent: (False, "agent"))
    pool = _chat(owner=visibility.shared_chat_owner(AGENT))
    before = _chat()  # a person's chat from before the agent turned Shared-only
    listed = _listed_ids(visibility.shared_chat_owner(AGENT), AGENT)
    assert listed == {pool}
    assert visibility.in_chat_history(task_store.get_chat(pool), ME) is True
    assert visibility.in_chat_history(task_store.get_chat(before), ME) is False


def test_the_table_arrives_on_an_existing_database():
    """A database from before 2.0 has no table: the boot's schema makes it,
    with both keys cascading, and an existing chat pins."""
    cid = _chat()
    with get_conn() as conn:
        conn.execute("DROP TABLE pinned_chats")
        conn.commit()
    with get_conn() as conn:
        schema.init_schema(conn)
        conn.commit()
    with get_conn() as conn:
        keys = conn.execute(
            """SELECT a.attname AS col, c.confdeltype AS on_delete, r.relname AS target
                 FROM pg_constraint c
                 JOIN pg_class t ON t.oid = c.conrelid
                 JOIN pg_class r ON r.oid = c.confrelid
                 JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY (c.conkey)
                WHERE t.relname = 'pinned_chats' AND c.contype = 'f'
                ORDER BY a.attname""").fetchall()
    assert [(k["col"], k["target"], k["on_delete"]) for k in keys] == [
        ("chat_id", "chats", "c"), ("user_sub", "users", "c")]
    assert chat_pins.pin(ME, cid) is True

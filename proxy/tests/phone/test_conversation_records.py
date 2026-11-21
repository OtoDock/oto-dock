"""The record of each conversation in an agent's Conversations tab
(``storage/chat/conversation_store.py``): the list the tab reads, the
first 2.0 boot's backfill of a 1.9.1 install's calls (fail closed, S-F12),
the wrap-up's claim and the notes from conversations."""

from __future__ import annotations

import asyncio
import uuid

from core.session import session_kind
from core.session.visibility import PHONE_CHAT_OWNER
from storage import database as task_store
from storage.agents import agent_store
from storage.chat import conversation_store as store
from storage.identity import external_identity_store
from storage.phone import phone_call_log_store

AGENT = "agent-conv"
PHONE = session_kind.PHONE.source_type


def _call(owner: str = PHONE_CHAT_OWNER, *, session_id: str = "", identity: str | None = None,
          external_identity_id: str = "", direction: str = "inbound") -> str:
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, owner, AGENT, "auto", source_type=PHONE)
    if session_id or external_identity_id:
        task_store.update_chat(cid, session_id=session_id or None,
                               external_identity_id=external_identity_id)
    if identity is not None:
        phone_call_log_store.insert_call({
            "agent": AGENT, "direction": direction, "outcome": "completed",
            "started_at": "2026-10-01T10:00:00+00:00", "ended_at": "2026-10-01T10:05:00+00:00",
            "session_id": session_id, "identity": identity})
    return cid


def test_the_tab_lists_the_agents_conversations_with_their_record(temp_db):
    outside = _call()
    store.create(outside, AGENT, line=store.LINE_CALLER, direction="inbound", identity_id="p-1")
    old = _call()
    store.create(old, AGENT, line=store.LINE_PERSON, direction="inbound")
    # A call on a person's route is their own chat; a task row and a
    # dashboard chat never list.
    _call("user-alice")
    task_store.create_chat(str(uuid.uuid4()), "user-alice", AGENT)
    task_store.create_chat("task-run-x1", f"task::{AGENT}", AGENT, "auto", source_type="task")

    rows = store.list_for_agent(AGENT, owner=PHONE_CHAT_OWNER)
    assert {r["id"] for r in rows} == {outside, old}
    assert store.count_for_agent(AGENT, owner=PHONE_CHAT_OWNER) == 2
    editor = store.list_for_agent(AGENT, owner=PHONE_CHAT_OWNER, lines=store.EDITOR_LINES)
    assert [r["id"] for r in editor] == [outside]
    (row,) = store.list_for_agent(AGENT, owner=PHONE_CHAT_OWNER, person="p-1")
    assert row["line"] == store.LINE_CALLER and row["person_id"] == "p-1"
    assert row["follow_ups"] == [] and row["wrote"] == []


def test_the_backfill_reads_the_line_from_the_call_log_and_fails_closed(temp_db):
    """A 1.9.1 call gets its record at the first 2.0 boot. Only a caller's
    label gives the caller line; a personal route's label, a pruned call-log
    row and an empty label give ``person``, which stays the managers'."""
    from storage.chat import schema as chat_schema
    from storage.pg import get_conn
    calls = {
        "caller": _call(session_id="s-1", identity="caller:+302101234567"),
        "caller-pin": _call(session_id="s-2", identity="caller-pin:+302101234568"),
        "ephemeral": _call(session_id="s-3", identity="ephemeral"),
        "shared": _call(session_id="s-4", identity="shared"),
        "user": _call(session_id="s-5", identity="user:alice"),
        "no-row": _call(session_id="s-6"),
        "empty": _call(session_id="s-7", identity=""),
        "known-person": _call(session_id="s-8", external_identity_id="p-9"),
        "outbound": _call(session_id="s-9", identity="caller:+302101234569", direction="outbound"),
    }
    persons_own = _call("user-alice", session_id="s-10", identity="user:alice")
    with get_conn() as conn:
        conn.execute("DELETE FROM conversation_records")
        conn.execute("DELETE FROM platform_settings WHERE key = 'conversation_records_backfilled'")
        chat_schema.backfill_conversation_records(conn)
        conn.commit()
    lines = {name: (store.get(cid) or {}).get("line") for name, cid in calls.items()}
    assert lines == {
        "caller": "caller", "caller-pin": "caller", "ephemeral": "caller", "shared": "shared",
        "user": "person", "no-row": "person", "empty": "person", "known-person": "caller",
        "outbound": "caller",
    }
    assert store.get(calls["outbound"])["direction"] == "outbound"
    assert store.get(calls["known-person"])["identity_id"] == "p-9"
    assert store.get(persons_own) is None
    # One-shot: a record removed after the boot is not made again.
    with get_conn() as conn:
        conn.execute("DELETE FROM conversation_records WHERE chat_id = %s", (calls["caller"],))
        chat_schema.backfill_conversation_records(conn)
        conn.commit()
    assert store.get(calls["caller"]) is None


def test_a_wrapup_is_claimed_once(temp_db):
    cid = _call()
    store.create(cid, AGENT, line=store.LINE_CALLER, direction="inbound")
    first = store.claim_wrapup(cid)
    assert first is not None and first["wrapup"] == store.WRAPUP_PENDING
    assert first["wrapup_attempts"] == 1 and first["ended_at"]
    assert store.claim_wrapup(cid) is None
    store.set_wrapup(cid, store.WRAPUP_RUNNING, run_id="run-w1")
    assert store.claim_wrapup(cid) is None
    assert store.by_run("run-w1")["chat_id"] == cid
    assert [r["chat_id"] for r in store.unfinished()] == [cid]
    store.record_outcome(cid, "Booked a visit.", ["Send the quote"])
    store.add_wrote(cid, {"kind": "memory", "name": "visits.md"})
    store.add_wrote(cid, {"kind": "memory", "name": "visits.md"})
    store.set_wrapup(cid, store.WRAPUP_DONE)
    row = store.get(cid)
    assert (row["outcome"], row["follow_ups"], row["wrote"]) == (
        "Booked a visit.", ["Send the quote"], [{"kind": "memory", "name": "visits.md"}])
    assert store.unfinished() == []
    assert store.claim_wrapup(str(uuid.uuid4())) is None


def test_the_notes_keep_the_newest_and_go_with_their_person_and_chat(temp_db):
    calls = [_call() for _ in range(store.NOTES_KEPT + 3)]
    for i, chat in enumerate(calls):
        store.add_note(AGENT, chat, "p-1" if i % 2 else "p-2", f"note {i}")
    notes = store.notes_for(AGENT)
    assert len(notes) == store.NOTES_KEPT
    assert notes[-1]["text"] == f"note {store.NOTES_KEPT + 2}" and notes[0]["text"] == "note 3"
    store.add_note(AGENT, calls[-1], "p-2", "x" * 500)
    assert len(store.notes_for(AGENT)[-1]["text"]) == store.NOTE_MAX_CHARS
    assert store.forget_notes(AGENT, "p-1") > 0
    assert store.notes_of_identity(AGENT, "p-1") == []
    assert store.notes_for(AGENT) == store.notes_of_identity(AGENT, "p-2")
    for chat in calls:
        task_store.delete_chat(chat)
    assert store.notes_for(AGENT) == []


def test_one_conversation_leaves_one_note(temp_db):
    """A misled wrap-up calling its record tool again and again replaces its
    own note and pushes out no other."""
    other, steered = _call(), _call()
    store.add_note(AGENT, other, "p-1", "the other caller's note")
    for i in range(store.NOTES_KEPT + 5):
        store.add_note(AGENT, steered, "p-2", f"steered {i}")
    assert [n["text"] for n in store.notes_for(AGENT)] == [
        "the other caller's note", f"steered {store.NOTES_KEPT + 4}"]


def _old_call(agent: str, *, session_id: str = "", thread: str = "") -> str:
    """A call as a 1.9.1 install left it: a phone-owned chat naming nobody,
    with the record the migration gave it."""
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, PHONE_CHAT_OWNER, agent, "auto", source_type=session_kind.PHONE.source_type)
    fields = {"session_id": session_id} if session_id else {}
    if thread:
        fields["codex_thread_id"] = thread
    if fields:
        task_store.update_chat(cid, **fields)
    store.create(cid, agent, line=store.LINE_PERSON, direction="inbound")
    return cid


def _labelled(agent: str, session_id: str, label: str) -> None:
    phone_call_log_store.insert_call({
        "agent": agent, "direction": "inbound", "outcome": "completed", "from_number": "",
        "started_at": "2026-10-01T10:00:00+00:00", "ended_at": "2026-10-01T10:03:00+00:00",
        "session_id": session_id, "identity": label})


def test_a_191_call_gets_the_person_it_was_with_once(temp_db, monkeypatch):
    """The first 2.0 boot names each old call's person so the people view
    shows it and forget reaches it: from the call log's label, else from
    whose tree holds its session. What neither names stays nobody's."""
    from services.phone import outside_upgrade
    agent = "agent-old-calls"
    agent_store.create_agent(agent, "Old calls")
    by_label = _old_call(agent, session_id="s-log")
    _labelled(agent, "s-log", "caller:+302101110000")
    by_pin = _old_call(agent, session_id="s-pin")
    _labelled(agent, "s-pin", "caller-pin:+302101110000")
    tree_gone = _old_call(agent, session_id="s-gone")
    _labelled(agent, "s-gone", "caller:+302102220000")
    by_tree = _old_call(agent, session_id="s-tree")
    by_thread = _old_call(agent, thread="t-tree")
    route_less = _old_call(agent, session_id="s-shared")
    _labelled(agent, "s-shared", "shared")
    nobody = _old_call(agent, session_id="s-aged-out")
    tree_person = external_identity_store.touch(agent, "phone", "+302103330000", "302103330000")
    log_person = external_identity_store.touch(agent, "phone", "+302101110000", "302101110000")
    monkeypatch.setattr(outside_upgrade, "_session_owners", lambda: {
        (agent, "s-tree"): ("phone", "302103330000"), (agent, "t-tree"): ("phone", "302103330000")})

    assert asyncio.run(outside_upgrade.name_old_calls()) == "5 earlier calls named their caller"

    for cid in (by_label, by_pin):
        assert task_store.get_chat(cid)["external_identity_id"] == log_person
        record = store.get(cid)
        assert (record["identity_id"], record["line"]) == (log_person, store.LINE_CALLER)
    for cid in (by_tree, by_thread):
        assert store.get(cid)["identity_id"] == tree_person
    made = external_identity_store.get(agent, "phone", "302102220000")
    assert made is not None and made["external_id"] == "+302102220000"
    assert store.get(tree_gone)["identity_id"] == made["id"]
    for cid in (route_less, nobody):
        assert task_store.get_chat(cid)["external_identity_id"] == ""
        assert store.get(cid)["line"] == store.LINE_PERSON
    assert {r["chat_id"] for r in store.for_identity(agent, log_person)} == {by_label, by_pin}
    assert asyncio.run(outside_upgrade.name_old_calls()) is None


def test_the_session_owners_are_read_from_each_engines_files_in_a_tree(temp_db, tmp_path, monkeypatch):
    from core.session import session_manager
    from services.infra import external_retention
    from services.phone import outside_upgrade

    class _Layer:
        def iter_session_files(self, home):
            for f in sorted(home.glob("*.jsonl")):
                yield f, f.stem
    home = tmp_path / "302103330000"
    home.mkdir()
    (home / "s-1.jsonl").write_text("{}\n")
    monkeypatch.setattr(session_manager, "get_all_layers", lambda: {"x": _Layer()})
    monkeypatch.setattr(external_retention, "iter_caller_homes",
                        lambda: iter([("a", "phone", "302103330000", home)]))
    assert outside_upgrade._session_owners() == {("a", "s-1"): ("phone", "302103330000")}

"""The turns the platform cut (``storage/chat/cut_records.py``), the facts
their open-turn row carries, and the rows of a waiting reload or move."""

from storage import database as task_store
from storage.chat import cut_records, open_turns, pending_rebuilds
from storage.pg import get_conn


def _open(chat_id: str) -> dict | None:
    return open_turns.get_open(chat_id)


def _chat(chat_id: str) -> None:
    task_store.create_chat(chat_id, "user-admin", "a1")


def test_the_open_turn_row_carries_the_facts_and_the_card(temp_db):
    _chat("cr-facts")
    open_turns.open_turn("cr-facts", "tok", "sid", person="user-admin", kind="dashboard",
                         mode="ask", machine_id="m-1", run_id="run-1", plan="plan.md")
    open_turns.set_card("cr-facts", "tok", '{"tool": "Bash"}')
    open_turns.set_card("cr-facts", "tok-other", '{"tool": "Write"}')
    row = _open("cr-facts")
    assert [row[name] for name in open_turns.FACTS] == [
        "user-admin", "dashboard", "ask", "m-1", "run-1", "plan.md"]
    assert row["card"] == '{"tool": "Bash"}'
    # The next turn starts with no card and its own facts.
    open_turns.open_turn("cr-facts", "tok-2", "sid", kind="task")
    row = _open("cr-facts")
    assert (row["card"], row["kind"], row["person"], row["plan"]) == ("", "task", "", "")


def test_a_local_row_is_moved_and_a_machine_row_is_copied(temp_db):
    _chat("cr-move")
    _chat("cr-copy")
    open_turns.open_turn("cr-move", "tok", "sid-1", person="user-admin", kind="dashboard", mode="ask")
    open_turns.set_card("cr-move", "tok", '{"tool": "Bash"}')
    moved = cut_records.move_open("cr-move", "tok", cause=cut_records.CAUSE_CRASH,
                                  state=cut_records.PENDING)
    assert _open("cr-move") is None
    assert (moved["person"], moved["kind"], moved["mode"], moved["card"], moved["cause"],
            moved["state"], moved["engine_open"]) == (
        "user-admin", "dashboard", "ask", '{"tool": "Bash"}', "crash", "pending", True)
    assert moved["started_at"] and moved["cut_at"] and moved["attempts"] == 0
    # A second move, or one after a new turn took the chat, writes nothing.
    assert cut_records.move_open("cr-move", "tok", cause="crash", state="pending") is None

    open_turns.open_turn("cr-copy", "tok", "sid-2", kind="dashboard", machine_id="m-9")
    copied = cut_records.copy_open(_open("cr-copy"), cause=cut_records.CAUSE_STOP,
                                   state=cut_records.WAITING)
    assert _open("cr-copy")["turn_token"] == "tok"
    assert (copied["machine_id"], copied["state"], copied["turn_token"]) == ("m-9", "waiting", "tok")
    assert [r["id"] for r in cut_records.in_state(cut_records.WAITING, machine_id="m-9")] == [
        copied["id"]]
    assert cut_records.in_state(cut_records.WAITING, machine_id="m-other") == []


def test_a_record_of_a_chat_that_is_gone_is_not_written(temp_db):
    assert cut_records.add("cr-nochat", cause="crash", state="pending", kind="task") is None


def test_a_record_is_claimed_once_released_and_settled_once(temp_db):
    _chat("cr-claim")
    rec = cut_records.add("cr-claim", cause=cut_records.CAUSE_STOP, state=cut_records.PENDING,
                          kind="task", run_id="run-9", session_id="sid")
    assert (rec["engine_open"], rec["run_id"]) == (False, "run-9")
    taken = cut_records.claim(rec["id"])
    assert (taken["state"], taken["attempts"]) == ("running", 1)
    assert cut_records.claim(rec["id"]) is None
    cut_records.release(rec["id"])
    assert cut_records.claim(rec["id"])["attempts"] == 2
    assert cut_records.settle(rec["id"], cut_records.CONTINUED) is True
    assert cut_records.settle(rec["id"], cut_records.DROPPED, "late") is False
    assert cut_records.get(rec["id"])["state"] == "continued"
    assert cut_records.claim(rec["id"]) is None


def test_a_waiting_record_becomes_pending_only_from_waiting(temp_db):
    _chat("cr-wait")
    rec = cut_records.add("cr-wait", cause="stop", state=cut_records.WAITING, machine_id="m-1")
    assert cut_records.unsettled_chats() == {"cr-wait"}
    assert cut_records.unsettled_of("cr-wait")["id"] == rec["id"]
    assert cut_records.make_pending(rec["id"]) is True
    assert cut_records.make_pending(rec["id"]) is False
    cut_records.settle(rec["id"], cut_records.LEFT, "its machine holds the session")
    assert cut_records.unsettled_chats() == set()
    assert cut_records.unsettled_of("cr-wait") is None


def test_the_boot_takes_back_what_was_held_and_keeps_the_newest_per_chat(temp_db):
    _chat("cr-tidy")
    _chat("cr-old")
    first = cut_records.add("cr-tidy", cause="crash", state=cut_records.PENDING)
    cut_records.claim(first["id"])                      # the last process held it
    second = cut_records.add("cr-tidy", cause="crash", state=cut_records.PENDING)
    old = cut_records.add("cr-old", cause="crash", state=cut_records.PENDING)
    cut_records.settle(old["id"], cut_records.DROPPED, "x")
    with get_conn() as conn:
        conn.execute("UPDATE chat_cut_turns SET settled_at='2000-01-01T00:00:00+00:00' WHERE id=%s",
                     (old["id"],))
        conn.commit()
    back, superseded, pruned = cut_records.tidy_at_boot()
    assert (back, superseded, pruned) == (1, 1, 1)
    assert cut_records.get(first["id"])["state"] == cut_records.CONTINUED
    assert cut_records.get(second["id"])["state"] == "pending"
    assert cut_records.get(old["id"]) is None


def test_a_boot_that_takes_nothing_up_marks_every_unsettled_record_and_no_other(temp_db):
    task_store.create_chat("c-mark", "user-1", "pa", "default")
    pending = cut_records.add("c-mark", cause=cut_records.CAUSE_STOP, state=cut_records.PENDING, kind="task")
    waiting = cut_records.add("c-mark", cause=cut_records.CAUSE_MACHINE, state=cut_records.WAITING, kind="task")
    settled = cut_records.add("c-mark", cause=cut_records.CAUSE_CRASH, state=cut_records.PENDING, kind="task")
    cut_records.settle(settled["id"], cut_records.DROPPED, "done")
    assert cut_records.mark_not_taken_up() == 2
    assert [cut_records.get(r["id"])["cause"] for r in (pending, waiting, settled)] == [
        cut_records.CAUSE_NOT_TAKEN_UP, cut_records.CAUSE_NOT_TAKEN_UP, cut_records.CAUSE_CRASH]


def test_the_cap_counts_continues_since_a_person_last_wrote(temp_db):
    _chat("cr-cap")
    for _ in range(2):
        rec = cut_records.add("cr-cap", cause="crash", state=cut_records.PENDING)
        cut_records.claim(rec["id"])
        cut_records.settle(rec["id"], cut_records.CONTINUED)
    dropped = cut_records.add("cr-cap", cause="crash", state=cut_records.PENDING)
    cut_records.settle(dropped["id"], cut_records.DROPPED, "x")
    assert cut_records.continued_in_a_row("cr-cap") == 2
    assert cut_records.continued_since("cr-cap", 24) == 2
    assert cut_records.person_wrote_since("cr-cap", "2000-01-01T00:00:00+00:00") is False
    task_store.add_chat_message("cr-cap", "user", "go on")
    assert cut_records.continued_in_a_row("cr-cap") == 0
    assert cut_records.continued_since("cr-cap", 24) == 2
    assert cut_records.person_wrote_since("cr-cap", "2000-01-01T00:00:00+00:00") is True


def test_a_deleted_chat_takes_its_records_and_its_waiting_rebuild(temp_db):
    _chat("cr-del")
    cut_records.add("cr-del", cause="crash", state=cut_records.PENDING)
    pending_rebuilds.save("cr-del", pending_rebuilds.RELOAD, person="user-admin", wait_s=60)
    task_store.delete_chat("cr-del")
    assert cut_records.in_state(*cut_records.UNSETTLED) == []
    assert pending_rebuilds.listed() == []


def test_a_waiting_rebuild_is_one_row_per_chat_taken_once(temp_db):
    _chat("pr-a")
    pending_rebuilds.save("pr-a", pending_rebuilds.MOVE, person="user-admin", wait_s=1800,
                          target="machine:m-1")
    pending_rebuilds.save("pr-a", pending_rebuilds.RELOAD, person="user-admin", wait_s=1800,
                          reason="a tool was enabled", choices={"mail": "work"})
    pending_rebuilds.save("pr-none", pending_rebuilds.RELOAD, person="user-admin", wait_s=1)
    rows = pending_rebuilds.listed()
    assert [(r["chat_id"], r["what"], r["choices"], r["reason"]) for r in rows] == [
        ("pr-a", "reload", {"mail": "work"}, "a tool was enabled")]
    assert 1700 < pending_rebuilds.seconds_left(rows[0]) <= 1800
    # A waiter of the other kind that ends does not take the row.
    pending_rebuilds.clear("pr-a", pending_rebuilds.MOVE)
    assert pending_rebuilds.take("pr-a")["what"] == "reload"
    assert pending_rebuilds.take("pr-a") is None
    pending_rebuilds.save("pr-a", pending_rebuilds.RELOAD, person="user-admin", wait_s=-5)
    assert pending_rebuilds.seconds_left(pending_rebuilds.listed()[0]) == 0.0
    pending_rebuilds.clear("pr-a", pending_rebuilds.RELOAD)
    assert pending_rebuilds.listed() == []


def test_an_older_releases_table_gains_the_facts_and_keeps_its_rows(temp_db):
    """The row of a release before the facts: the migration adds the columns
    empty, and an empty ``kind`` is how the boot tells such a row."""
    from storage.chat.schema import migrate_chat
    _chat("cr-old-shape")
    with get_conn() as conn:
        conn.execute("DROP TABLE chat_open_turns")
        conn.execute("""
            CREATE TABLE chat_open_turns (
                chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
                turn_token TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                engine_open BOOLEAN NOT NULL DEFAULT TRUE,
                early_ranges TEXT NOT NULL DEFAULT '[]'
            )
        """)
        conn.execute("INSERT INTO chat_open_turns (chat_id, turn_token, session_id, started_at) "
                     "VALUES ('cr-old-shape', 'tok', 'sid', '2026-01-01T00:00:00+00:00')")
        migrate_chat(conn)
        migrate_chat(conn)
        conn.commit()
    row = _open("cr-old-shape")
    assert (row["turn_token"], row["kind"], row["person"], row["card"]) == ("tok", "", "", "")


def test_a_later_cut_settles_a_claimed_record_as_a_start_and_any_other_as_dropped(temp_db):
    _chat("cr-later")
    claimed = cut_records.add("cr-later", cause=cut_records.CAUSE_CRASH, state=cut_records.PENDING,
                              session_id="s1", kind="dashboard")
    cut_records.claim(claimed["id"])
    cut_records.add("cr-later", cause=cut_records.CAUSE_CRASH, state=cut_records.PENDING,
                    session_id="s2", kind="dashboard")
    _chat("cr-never")
    never = cut_records.add("cr-never", cause=cut_records.CAUSE_CRASH, state=cut_records.PENDING,
                            session_id="s3", kind="dashboard")
    cut_records.add("cr-never", cause=cut_records.CAUSE_CRASH, state=cut_records.PENDING,
                    session_id="s4", kind="dashboard")
    assert cut_records.supersede_older() == 2
    assert cut_records.get(claimed["id"])["state"] == cut_records.CONTINUED
    assert cut_records.get(never["id"])["state"] == cut_records.DROPPED
    assert cut_records.continued_in_a_row("cr-later") == 1 and cut_records.continued_in_a_row("cr-never") == 0

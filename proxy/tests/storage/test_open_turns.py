"""The open-turn record (``storage/chat/open_turns.py``) and the batch
insert's id list, in-transaction hook and search switch it rides on."""

import json
from datetime import datetime, timezone

import psycopg
import pytest

from storage import database as task_store
from storage.chat import open_turns
from storage.pg import get_conn

_EARLIER = "2000-01-01T00:00:00+00:00"
_CARD = json.dumps({"type": "system", "subtype": "turn_ended", "reason": "restarted"})


def _record(chat_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chat_open_turns WHERE chat_id=%s", (chat_id,)).fetchone()
    return dict(row) if row else None


def _rows(chat_id: str) -> list[tuple[str, str, str]]:
    return [(r["role"], r["content"] or "", r["event_type"] or "")
            for r in task_store.get_chat_messages(chat_id)]


def _note(chat_id: str, token: str, after: int, upto: int) -> None:
    with get_conn() as conn:
        open_turns.note_range_in(conn, chat_id, token, after, upto)
        conn.commit()


def test_a_turn_start_replaces_the_chats_record(temp_db):
    task_store.create_chat("ot-a", "user-admin", "a1")
    open_turns.open_turn("ot-a", "tok-1", "sid-1")
    _note("ot-a", "tok-1", 1, 4)
    open_turns.engine_turn_done("ot-a", "tok-1")
    open_turns.open_turn("ot-a", "tok-2", "sid-2")
    rec = _record("ot-a")
    assert (rec["turn_token"], rec["session_id"], rec["engine_open"], rec["early_ranges"]) == (
        "tok-2", "sid-2", True, "[]")


def test_a_chat_with_no_row_gets_no_record(temp_db):
    open_turns.open_turn("ot-none", "tok", "sid")
    assert _record("ot-none") is None


def test_ranges_append_by_token_and_the_engine_turns_start_them_over(temp_db):
    task_store.create_chat("ot-r", "user-admin", "a1")
    open_turns.open_turn("ot-r", "tok", "sid")
    _note("ot-r", "tok", 0, 3)
    _note("ot-r", "tok", 4, 6)
    _note("ot-r", "tok-gone", 7, 9)
    assert json.loads(_record("ot-r")["early_ranges"]) == [[0, 3], [4, 6]]
    open_turns.engine_turn_done("ot-r", "tok")
    assert (_record("ot-r")["engine_open"], _record("ot-r")["early_ranges"]) == (False, "[]")
    _note("ot-r", "tok", 10, 12)
    open_turns.engine_turn_began("ot-r", "tok")
    assert (_record("ot-r")["engine_open"], _record("ot-r")["early_ranges"]) == (True, "[]")


def test_the_end_deletes_its_own_turn_only(temp_db):
    task_store.create_chat("ot-end", "user-admin", "a1")
    open_turns.open_turn("ot-end", "tok-new", "sid")
    assert open_turns.end_turn("ot-end", "tok-old") is False
    assert _record("ot-end") is not None
    assert open_turns.end_turn("ot-end", "tok-new") is True
    assert open_turns.list_open() == []


def test_a_deleted_chat_takes_its_record(temp_db):
    task_store.create_chat("ot-del", "user-admin", "a1")
    open_turns.open_turn("ot-del", "tok", "sid")
    task_store.delete_chat("ot-del")
    assert open_turns.list_open() == []


def test_the_batch_names_its_ids_and_its_hook_commits_with_the_rows(temp_db):
    task_store.create_chat("ot-batch", "user-admin", "a1")
    open_turns.open_turn("ot-batch", "tok", "sid")
    ids: list[int] = []

    def _hook(conn, written):
        open_turns.note_range_in(conn, "ot-batch", "tok", min(written) - 1, max(written))

    last = task_store.add_chat_messages_batch(
        "ot-batch", [("assistant", "one", "", ""), ("event", "", "tool", "{}")], ids=ids, in_txn=_hook)
    assert len(ids) == 2 and last == ids[-1] and ids[0] < ids[1]
    assert json.loads(_record("ot-batch")["early_ranges"]) == [[ids[0] - 1, ids[1]]]


def test_a_failing_hook_leaves_neither_the_rows_nor_the_note(temp_db):
    task_store.create_chat("ot-fail", "user-admin", "a1")

    def _boom(conn, written):
        raise psycopg.errors.QueryCanceled("cancelled")

    with pytest.raises(psycopg.errors.QueryCanceled):
        task_store.add_chat_messages_batch("ot-fail", [("assistant", "x", "", "")], in_txn=_boom)
    assert _rows("ot-fail") == []


def test_a_batch_may_leave_the_search_row_for_later(temp_db, monkeypatch):
    from storage.chat import db_chats
    rebuilt: list[str] = []
    monkeypatch.setattr(db_chats, "rebuild_chat_search", rebuilt.append)
    task_store.create_chat("ot-s", "user-admin", "a1")
    task_store.add_chat_messages_batch("ot-s", [("assistant", "words", "", "")], search=False)
    assert rebuilt == []
    task_store.add_chat_messages_batch("ot-s", [("assistant", "words", "", "")])
    assert rebuilt == ["ot-s"]


def test_the_closing_row_lands_once_with_the_abort_flags(temp_db):
    task_store.create_chat("ot-close", "user-admin", "a1")
    task_store.add_chat_message("ot-close", "user", "the prompt")
    open_turns.open_turn("ot-close", "tok", "sid")
    assert open_turns.close_cut("ot-close", "tok", "system", _CARD, abort_graceful=False,
                                since=datetime.now(timezone.utc).isoformat()) is True
    assert open_turns.close_cut("ot-close", "tok", "system", _CARD, abort_graceful=False,
                                since=_EARLIER) is False
    assert _rows("ot-close") == [("user", "the prompt", ""), ("event", "", "system")]
    chat = task_store.get_chat("ot-close")
    assert chat["last_turn_aborted"] is True and chat["last_abort_graceful"] is False


def test_a_newer_turn_gets_no_closing_row(temp_db):
    task_store.create_chat("ot-newer", "user-admin", "a1")
    open_turns.open_turn("ot-newer", "tok-new", "sid")
    assert open_turns.close_cut("ot-newer", "tok-cut", "system", _CARD, abort_graceful=False,
                                since=_EARLIER) is False
    assert _rows("ot-newer") == []
    assert task_store.get_chat("ot-newer")["last_turn_aborted"] is False


def test_no_closing_row_after_a_persons_new_message_or_after_a_card(temp_db):
    task_store.create_chat("ot-moved", "user-admin", "a1")
    open_turns.open_turn("ot-moved", "tok", "sid")
    task_store.add_chat_message("ot-moved", "user", "anything new?")
    assert open_turns.close_cut("ot-moved", "tok", "system", _CARD, abort_graceful=False,
                                since=_EARLIER) is False
    assert _record("ot-moved") is None and _rows("ot-moved") == [("user", "anything new?", "")]
    assert task_store.get_chat("ot-moved")["last_turn_aborted"] is False

    # The cut turn wrote its own typed card before its end could run.
    task_store.create_chat("ot-carded", "user-admin", "a1")
    open_turns.open_turn("ot-carded", "tok", "sid")
    task_store.add_chat_message("ot-carded", "event", "", event_type="system",
                                event_data=json.dumps({"type": "system", "subtype": "turn_ended",
                                                       "reason": "error"}))
    assert open_turns.close_cut("ot-carded", "tok", "system", _CARD, abort_graceful=False,
                                since=datetime.now(timezone.utc).isoformat()) is False
    assert len(_rows("ot-carded")) == 1


def test_an_earlier_turns_card_never_hides_a_later_cut(temp_db):
    """Turn 1 ended on a typed card; a prompt the engine took with no row
    of its own started turn 2, which the restart cut: turn 2 gets its
    closing row and its abort flags."""
    task_store.create_chat("ot-older", "user-admin", "a1")
    task_store.add_chat_message("ot-older", "user", "first")
    task_store.add_chat_message("ot-older", "event", "", event_type="system",
                                event_data=json.dumps({"type": "system", "subtype": "turn_ended",
                                                       "reason": "exited"}))
    open_turns.open_turn("ot-older", "tok-2", "sid")
    assert open_turns.close_cut("ot-older", "tok-2", "system", _CARD, abort_graceful=False,
                                since=datetime.now(timezone.utc).isoformat()) is True
    reasons = [json.loads(r["event_data"])["reason"] for r in task_store.get_chat_messages("ot-older")
               if r["role"] == "event"]
    assert reasons == ["exited", "restarted"]
    assert task_store.get_chat("ot-older")["last_turn_aborted"] is True


def test_the_replay_takes_the_record_and_its_ranges_keep_the_persons_rows(temp_db):
    task_store.create_chat("ot-replay", "user-admin", "a1")
    task_store.add_chat_message("ot-replay", "user", "the prompt")
    open_turns.open_turn("ot-replay", "tok", "sid")
    first = task_store.add_chat_messages_batch("ot-replay", [("assistant", "early", "", "")])
    steer = task_store.add_chat_message("ot-replay", "user", "a steer")
    second = task_store.add_chat_messages_batch("ot-replay", [("event", "", "tool", "{}")])
    _note("ot-replay", "tok", first - 1, first)
    _note("ot-replay", "tok", steer, second)
    assert open_turns.take_record("ot-replay", "tok-other") is None
    started = _record("ot-replay")["started_at"]
    taken = open_turns.take_record("ot-replay", "tok")
    assert taken == open_turns.Taken([(first - 1, first), (steer, second)], started)
    assert _record("ot-replay") is None
    assert task_store.delete_chat_message_ranges("ot-replay", taken.early) == 2
    assert _rows("ot-replay") == [("user", "the prompt", ""), ("user", "a steer", "")]


def test_a_taken_turn_whose_replay_never_began_gets_its_closing_row(temp_db):
    task_store.create_chat("ot-taken", "user-admin", "a1")
    assert open_turns.write_closing("ot-taken", "system", _CARD, abort_graceful=False,
                                    since=_EARLIER, turn_started=_EARLIER) is True
    assert _rows("ot-taken") == [("event", "", "system")]


def test_list_open_names_every_turn_left(temp_db):
    for cid in ("ot-l1", "ot-l2"):
        task_store.create_chat(cid, "user-admin", "a1")
        open_turns.open_turn(cid, f"tok-{cid}", f"sid-{cid}")
    open_turns.engine_turn_done("ot-l2", "tok-ot-l2")
    assert sorted((r["chat_id"], r["turn_token"], r["session_id"], r["engine_open"])
                  for r in open_turns.list_open()) == [
        ("ot-l1", "tok-ot-l1", "sid-ot-l1", True), ("ot-l2", "tok-ot-l2", "sid-ot-l2", False)]

"""A truncated replay never writes a row the chat already holds
(``core/events/replay_overlap.py``)."""

from __future__ import annotations

import json

import pytest

from core.events import tool_roles
from core.events.replay_overlap import ReplayOverlap, key_of
from core.events.stream_pump import _serialize_turn_rows
from ws import wire_events as wire

MARK = "\n⚠ stream interrupted — earlier output was truncated during a platform restart.\n"


def _text(words: str) -> dict:
    return {"type": wire.LIVE_TEXT, "content": words}


def _tool(tool_id: str, **more) -> dict:
    return {"type": wire.PERSISTED_TOOL, "name": "Bash", "tool_id": tool_id, "active": False, **more}


def _todo(todos: list) -> dict:
    return {"type": wire.PERSISTED_TOOL, "name": tool_roles.TODO_SNAPSHOT, "tool_input": {"todos": todos}}


def _early(blocks: list[dict], first_id: int = 100) -> list[dict]:
    """The rows a checkpoint wrote for ``blocks``."""
    return [{"id": first_id + i, "role": role, "content": content, "event_type": event_type,
             "event_data": event_data}
            for i, (role, content, event_type, event_data) in enumerate(_serialize_turn_rows(blocks))]


def _written(overlap: ReplayOverlap, blocks: list[dict], *, todo=None, early=False):
    kept, adopted = overlap.keep(blocks, _serialize_turn_rows, todo_block=todo, early=early)
    return kept, adopted


def _said(kept: list[dict]) -> list[str]:
    return [b.get("tool_id") or b["content"] for b in kept]


def test_the_incidents_counts_write_three_rows_and_no_mark():
    """The incident's counts in a made-up order, not its real rows: a replay
    of 45 blocks of which the chat held 42 (here fourteen times a tool, a
    tool and a text), then one new tool and two new texts."""
    turn: list[dict] = []
    for i in range(14):
        turn += [_tool(f"t-{i}-a", tool_result="output"), _tool(f"t-{i}-b", tool_result="output"),
                 _text(f"\n\nStep {i} is done.")]
    saved = _early(turn)
    assert len(saved) == 42
    # The replay: the same blocks with no results, its texts with the outer
    # blank lines off, then what the turn did after the last checkpoint.
    replay = [_tool(b["tool_id"]) if b["type"] == wire.PERSISTED_TOOL
              else _text(b["content"].strip()) for b in turn]
    new = [_tool("t-new"), _text("228 passed."), _text("Ruff is clean.")]
    overlap = ReplayOverlap(saved, MARK)
    kept, _ = _written(overlap, replay + new)
    assert kept == new
    assert overlap.dropped == 42 and overlap.marked is False


def test_a_held_tool_is_never_written_wherever_it_comes():
    overlap = ReplayOverlap(_early([_tool("t-1"), _text("one")]), MARK)
    kept, _ = _written(overlap, [_tool("t-1"), _tool("t-new"), _text("two"), _tool("t-1")])
    assert _said(kept) == ["t-new", "two"]


def test_a_replay_past_the_saved_rows_keeps_a_line_that_repeats_an_earlier_one_and_its_mark():
    """The review's third probe. The replay starts past the last saved row
    (a real gap), and its first new line equals a line said earlier in the
    turn. By membership it was dropped, and then no mark was written."""
    saved = _early([_text("Let me check."), _tool("t-1"), _text("The first half is done.")])
    overlap = ReplayOverlap(saved, MARK)
    kept, _ = _written(overlap, [_text("Let me check."), _tool("t-9"), _text("All done.")])
    assert _said(kept) == [MARK, "Let me check.", "t-9", "All done."]
    assert overlap.marked is True and overlap.dropped == 0


def test_the_head_is_dropped_only_when_it_runs_in_step_to_the_last_saved_row():
    saved = _early([_tool("t-1"), _text("Half."), _text("Almost.")])
    # In step up to the last saved row: the overlap, then the new line.
    kept, _ = _written(ReplayOverlap(saved, MARK), [_text("Half."), _text("Almost."), _text("Done.")])
    assert _said(kept) == ["Done."]
    kept, _ = _written(ReplayOverlap(saved, MARK), [_text("Almost."), _text("Done.")])
    assert _said(kept) == ["Done."]
    # A saved line that is not followed by the rest of the saved rows is new.
    gap = ReplayOverlap(saved, MARK)
    kept, _ = _written(gap, [_text("Half."), _text("Something else.")])
    assert _said(kept) == [MARK, "Half.", "Something else."] and gap.marked is True
    # Rows before the saved rows' last tool are not the head's to match:
    # that tool would come after them in the replay.
    saved = _early([_text("Start."), _tool("t-1"), _text("End.")])
    gap = ReplayOverlap(saved, MARK)
    kept, _ = _written(gap, [_text("Start."), _text("End."), _text("New.")])
    assert _said(kept) == [MARK, "Start.", "End.", "New."]


def test_a_new_line_that_repeats_an_old_one_is_kept_once_the_overlap_ended():
    """Only before the first tool the chat does not hold: after it the
    replay is new, and a narration line said twice is written twice."""
    overlap = ReplayOverlap(_early([_text("Let me check."), _tool("t-1"), _text("Done.")]), MARK)
    kept, _ = _written(overlap, [_tool("t-1"), _text("Done."), _tool("t-2"),
                                 _text("Let me check."), _text("Done.")])
    assert _said(kept) == ["t-2", "Let me check.", "Done."]


def test_texts_are_walked_in_step_after_the_last_saved_tool():
    """A line the chat holds earlier in the turn is not a line the replay
    repeats here: past the last saved tool a text must be the next saved row."""
    saved = _early([_text("Starting."), _tool("t-1"), _text("Half."), _text("Starting.")])
    overlap = ReplayOverlap(saved, MARK)
    kept, _ = _written(overlap, [_tool("t-1"), _text("Starting."), _text("Half.")])
    # "Starting." right after t-1 is not the next saved row ("Half." is).
    assert _said(kept) == ["Starting."]


def test_what_comes_before_a_saved_tool_was_saved_too():
    """Everything ahead of a saved block was saved: no comparison is needed,
    so a text the replay words differently is not written again."""
    saved = _early([_text("Starting."), _tool("t-1"), _text("Half the work is done."), _tool("t-2")])
    overlap = ReplayOverlap(saved, MARK)
    kept, _ = _written(overlap, [_text("ing."), _tool("t-1"), _text("Half the work"), _tool("t-2"),
                                 _text("The rest.")])
    assert _said(kept) == ["The rest."] and overlap.marked is False


def test_an_early_row_the_replay_does_not_produce_does_not_break_the_walk():
    saved = _early([_tool("t-1"), {"type": "permission_card", "request_id": "r-1"}, _text("after the card")])
    overlap = ReplayOverlap(saved, MARK)
    kept, _ = _written(overlap, [_tool("t-1"), _text("after the card"), _tool("t-2")])
    assert _said(kept) == ["t-2"]


class TestATextTheFirstRunSplit:
    """The first run split a text around a block a hook delivered (an
    artifact, a steer): two saved rows. The replay gets no such block and
    produces the text as one."""

    ARTIFACT = {"type": "permission_card", "request_id": "r-9"}

    def test_between_two_saved_tools_it_is_not_written_again(self):
        # Two saves of the first run: the split fell between them.
        first = _early([_tool("t-1"), _text("The first half, ")], first_id=100)
        second = _early([self.ARTIFACT, _text("and the second half."), _tool("t-2")], first_id=200)
        overlap = ReplayOverlap(first + second, MARK)
        kept, _ = _written(overlap, [_tool("t-1"), _text("The first half, and the second half."),
                                     _tool("t-2"), _text("New.")])
        assert _said(kept) == ["New."] and overlap.marked is False

    def test_after_the_last_saved_tool_its_pieces_are_matched_as_one(self):
        first = _early([_tool("t-1"), _text("The first half, ")], first_id=100)
        second = _early([self.ARTIFACT, _text("and the second half.")], first_id=200)
        overlap = ReplayOverlap(first + second, MARK)
        kept, _ = _written(overlap, [_tool("t-1"), _text("The first half, and the second half."),
                                     _text("New.")])
        assert _said(kept) == ["New."]

    def test_a_text_that_goes_on_past_the_last_saved_piece_is_written_from_there(self):
        """The cut fell between the two halves: only the first was saved."""
        saved = _early([_tool("t-1"), _text("The first half, "), self.ARTIFACT])
        overlap = ReplayOverlap(saved, MARK)
        whole = _text("The first half, and the second half.")
        kept, _ = _written(overlap, [_tool("t-1"), whole, _tool("t-2")])
        assert _said(kept) == ["and the second half.", "t-2"]
        # The pump's own block is left as it is: the row is a copy.
        assert whole["content"] == "The first half, and the second half."
        assert overlap.marked is False

    def test_in_the_head_too_when_enough_words_are_saved(self):
        saved = _early([_tool("t-1"), _text("The first half of a long line, "), self.ARTIFACT])
        overlap = ReplayOverlap(saved, MARK)
        kept, _ = _written(overlap, [_text("The first half of a long line, and the rest."), _tool("t-2")])
        assert _said(kept) == ["and the rest.", "t-2"]
        # A short saved line a new one only starts like proves nothing.
        gap = ReplayOverlap(_early([_tool("t-1"), _text("Done.")]), MARK)
        kept, _ = _written(gap, [_text("Done. Now the tests."), _tool("t-2")])
        assert _said(kept) == [MARK, "Done. Now the tests.", "t-2"]


def test_a_replay_that_starts_inside_a_text_drops_its_end_when_the_next_block_walks_on():
    saved = _early([_tool("t-1"), _text("A long line of narration."), _text("And another.")])
    overlap = ReplayOverlap(saved, MARK)
    kept, _ = _written(overlap, [_text("of narration."), _text("And another."), _text("New.")])
    assert _said(kept) == ["New."]
    # Alone, the end of a saved text proves nothing.
    gap = ReplayOverlap(_early([_tool("t-1"), _text("All of it is done.")]), MARK)
    kept, _ = _written(gap, [_text("done."), _tool("t-2")])
    assert _said(kept) == [MARK, "done.", "t-2"]


def test_a_replay_past_the_last_saved_row_keeps_everything_and_its_mark():
    overlap = ReplayOverlap(_early([_text("the start")]), MARK)
    kept, _ = _written(overlap, [_text("much later"), _tool("t-9"), _text("the end")])
    assert _said(kept) == [MARK, "much later", "t-9", "the end"]
    assert overlap.marked is True


def test_the_mark_is_decided_at_the_first_save_and_sits_before_its_lines():
    """A gap: the mark goes ahead of the first save's rows, never after new
    lines an earlier save wrote."""
    gap = ReplayOverlap(_early([_text("a")]), MARK)
    one, _ = _written(gap, [_text("new one")])
    two, _ = _written(gap, [_tool("t-5"), _text("more")])
    assert _said(one) == [MARK, "new one"] and _said(two) == ["t-5", "more"]
    # An overlap: no mark, in this save or a later one.
    overlap = ReplayOverlap(_early([_tool("t-1")]), MARK)
    first, _ = _written(overlap, [_tool("t-1")])
    later, _ = _written(overlap, [_tool("t-2"), _text("x")])
    assert first == [] and _said(later) == ["t-2", "x"] and overlap.marked is False
    # A save with no row of its own decides nothing.
    quiet = ReplayOverlap(_early([_tool("t-1")]), MARK)
    transient = {"type": "no-such-saved-kind"}
    rows_of = lambda blocks: [] if blocks == [transient] else _serialize_turn_rows(blocks)
    assert quiet.keep([transient], rows_of) == ([transient], 0)
    kept, _ = quiet.keep([_tool("t-1"), _text("after")], rows_of)
    assert _said(kept) == ["after"] and quiet.marked is False


def test_the_checklist_takes_the_row_saved_before_the_cut():
    saved = _early([_tool("t-1"), _todo([{"content": "a", "status": "pending"}])], first_id=500)
    overlap = ReplayOverlap(saved, MARK)
    todo = _todo([{"content": "a", "status": "completed"}])
    kept, adopted = _written(overlap, [_tool("t-1"), todo], todo=todo)
    assert kept == [] and adopted == 501
    # A checklist that is not the pump's own, or with no row saved, is written.
    other = _todo([{"content": "b", "status": "pending"}])
    kept, adopted = _written(ReplayOverlap(saved, MARK), [_tool("t-1"), other], todo=todo)
    assert kept == [other] and adopted == 0
    kept, adopted = _written(ReplayOverlap(_early([_tool("t-1")]), MARK), [_tool("t-1"), todo],
                             todo=todo)
    assert kept == [todo] and adopted == 0


def test_a_block_with_no_row_is_left_to_the_serialiser():
    overlap = ReplayOverlap(_early([_tool("t-1")]), MARK)
    transient = {"type": "no-such-saved-kind"}
    rows_of = lambda blocks: [] if blocks == [transient] else _serialize_turn_rows(blocks)
    kept, _ = overlap.keep([transient, _tool("t-1")], rows_of)
    assert kept == [transient]


def test_the_keys():
    assert key_of(("assistant", "\n\nHello.\n", "", "")) == ("text", "Hello.")
    assert key_of(("event", "", "tool", json.dumps({"tool_id": "t-1", "name": "Bash"}))) == ("tool", "t-1")
    assert key_of(("event", "", "tool", json.dumps(_todo([])))) == ("todo",)
    assert key_of(("event", "", "bg_command_spawn", json.dumps({"id": "c-1", "x": 1}))) == \
        ("event", "bg_command_spawn", "id", "c-1")
    # No id: the whole payload, in any key order.
    a = key_of(("event", "", "system", json.dumps({"subtype": "note", "message": "m"})))
    b = key_of(("event", "", "system", json.dumps({"message": "m", "subtype": "note"})))
    assert a == b and a[0] == "event"
    assert key_of(("event", "", "system", "not json"))[0] == "event"


class TestThePumpsSave:
    """``ChatStreamPump._save_turn_blocks`` with the walk: the rows are the
    blocks the chat does not hold, the turn's own bookkeeping reads them all."""

    @staticmethod
    def _pump(chat_id: str):
        import asyncio

        from core.events.stream_pump import ChatStreamPump
        producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
        return ChatStreamPump(chat_id=chat_id, session_id=f"sess-{chat_id}", producer=producer,
                              event_queue=asyncio.Queue(), perm_queue=None), producer

    @staticmethod
    def _rows(chat_id: str) -> list[dict]:
        from storage import database as task_store
        return [r for r in task_store.get_chat_messages(chat_id) if r["role"] != "user"]

    async def _saved_early(self, chat_id: str, blocks: list[dict]) -> list[dict]:
        from storage import database as task_store
        task_store.add_chat_messages_batch(chat_id, _serialize_turn_rows(blocks))
        return [{"id": r["id"], "role": r["role"], "content": r["content"],
                 "event_type": r.get("event_type") or "", "event_data": r.get("event_data") or ""}
                for r in self._rows(chat_id)]

    @pytest.mark.asyncio
    async def test_a_checkpoint_writes_the_new_blocks_and_the_checklist_keeps_its_row(self, temp_db):
        chat_id = "chat-overlap-1"
        temp_db.create_chat(chat_id, "user-1", "pa", "default")
        early = await self._saved_early(chat_id, [
            _tool("t-1", tool_result="kept"), _todo([{"content": "a", "status": "pending"}]),
            _text("Half way.")])
        todo_row = early[1]["id"]
        pump, producer = self._pump(chat_id)
        try:
            todo = _todo([{"content": "a", "status": "completed"}])
            pump._turn_blocks = [_tool("t-1"), todo, _text("Half way."), _tool("t-2"), _text("New.")]
            pump._todo_block = todo
            pump.replay_overlap = ReplayOverlap(early, MARK)
            await pump._save_turn_blocks(upto=5)
            from core.events import chat_writer
            await chat_writer.drain(chat_id, timeout=5.0)
            rows = self._rows(chat_id)
            assert [json.loads(r["event_data"]).get("tool_id") if r["role"] == "event" else r["content"]
                    for r in rows] == ["t-1", None, "Half way.", "t-2", "New."]
            # The held copy of the tool keeps its result, the checklist's row
            # is the early one, rewritten with the checklist as it is now.
            assert json.loads(rows[0]["event_data"])["tool_result"] == "kept"
            assert rows[1]["id"] == todo_row
            assert json.loads(rows[1]["event_data"])["tool_input"]["todos"][0]["status"] == "completed"
            assert pump._todo_ref is not None and pump._todo_ref.id == todo_row
            # Every saved block left the turn's accumulator, written or not.
            assert pump._turn_blocks == []
        finally:
            producer.cancel()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("save", [{"early": True}, {"upto": 1}], ids=["a_steers", "a_checkpoint"])
    async def test_an_early_save_inside_the_head_decides_nothing(self, temp_db, save):
        """The pump tells the walk which saves are early ones: a steer's
        save and a checkpoint. The fragment is written, no mark, and the
        turn's last save drops the held rows."""
        chat_id = f"chat-overlap-{len(save)}-{list(save)[0]}"
        temp_db.create_chat(chat_id, "user-1", "pa", "default")
        early = await self._saved_early(chat_id, [
            _tool("t-1"), _text("First I read the file. Then I ran it."), _text("Second line.")])
        pump, producer = self._pump(chat_id)
        try:
            from core.events import chat_writer
            pump.replay_overlap = ReplayOverlap(early, MARK)
            pump._turn_blocks = [_text("Then I ran it.")]
            pump._save_turn_blocks(**save)
            pump._turn_blocks = [_text("Second line."), _tool("t-new")]
            pump._save_turn_blocks()
            await chat_writer.drain(chat_id, timeout=5.0)
            said = [json.loads(r["event_data"]).get("tool_id") if r["role"] == "event" else r["content"]
                    for r in self._rows(chat_id)]
            assert said == ["t-1", "First I read the file. Then I ran it.", "Second line.",
                            "Then I ran it.", "t-new"]
            assert pump.replay_overlap.marked is False
        finally:
            producer.cancel()

    @pytest.mark.asyncio
    async def test_a_save_that_writes_nothing_still_takes_its_blocks(self, temp_db):
        chat_id = "chat-overlap-2"
        temp_db.create_chat(chat_id, "user-1", "pa", "default")
        early = await self._saved_early(chat_id, [_tool("t-1"), _text("Held.")])
        pump, producer = self._pump(chat_id)
        try:
            pump._turn_blocks = [_tool("t-1"), _text("Held.")]
            pump.replay_overlap = ReplayOverlap(early, MARK)
            fut = pump._save_turn_blocks(upto=2)
            assert fut is not None
            await fut
            assert len(self._rows(chat_id)) == 2 and pump._turn_blocks == []
        finally:
            producer.cancel()


class TestASaveBeforeTheReplayedHeadIsWhole:
    """A steer's save can land between the replay's events: its event goes
    on the pump's own queue the moment the engine took the message
    (``ChatStreamPump.record_steer``), while the replayed burst is still
    being read. Such a save holds a piece of the head. Decided there, the
    mark was written although nothing was lost, and every later block was
    written again."""

    def test_the_mark_waits_for_a_save_that_holds_a_tool(self):
        """The review's rows: the replay starts inside a saved text and the
        first save holds only that fragment."""
        saved = _early([_tool("t-1"), _text("First I read the file. Then I ran it."),
                        _text("Second line."), _text("Third line.")])
        overlap = ReplayOverlap(saved, MARK)
        first, _ = _written(overlap, [_text("Then I ran it.")], early=True)
        # The end of one line alone proves nothing: it is kept, and nothing
        # is decided yet.
        assert _said(first) == ["Then I ran it."] and overlap.marked is False
        second, _ = _written(overlap, [_text("Second line."), _text("Third line."), _tool("t-new")])
        assert _said(second) == ["t-new"]            # the held rows are not written again
        assert overlap.marked is False

    def test_a_real_gap_gets_its_mark_at_the_save_that_decides(self):
        saved = _early([_tool("t-1"), _text("Reading the file.")])
        overlap = ReplayOverlap(saved, MARK)
        first, _ = _written(overlap, [_text("Much later.")], early=True)
        assert _said(first) == ["Much later."] and overlap.marked is False
        second, _ = _written(overlap, [_text("Later still."), _tool("t-40")])
        assert _said(second) == [MARK, "Later still.", "t-40"] and overlap.marked is True
        third, _ = _written(overlap, [_text("Done.")])
        assert _said(third) == ["Done."]

    def test_an_early_save_that_holds_a_tool_decides(self):
        """A tool the chat does not hold ends the overlap: the head before
        it is whole."""
        saved = _early([_tool("t-1"), _text("Reading the file.")])
        overlap = ReplayOverlap(saved, MARK)
        kept, _ = _written(overlap, [_text("Much later."), _tool("t-40")], early=True)
        assert _said(kept) == [MARK, "Much later.", "t-40"] and overlap.marked is True

    def test_a_save_that_ends_the_turn_decides_with_no_tool(self):
        saved = _early([_tool("t-1"), _text("Reading the file.")])
        overlap = ReplayOverlap(saved, MARK)
        kept, _ = _written(overlap, [_text("Much later.")])
        assert _said(kept) == [MARK, "Much later."] and overlap.marked is True

    def test_an_early_save_whose_head_matches_needs_no_later_decision(self):
        saved = _early([_tool("t-1"), _text("Reading the file."), _text("It has three parts.")])
        overlap = ReplayOverlap(saved, MARK)
        first, _ = _written(overlap, [_text("Reading the file."), _text("It has three parts.")],
                            early=True)
        assert first == [] and overlap.marked is False
        second, _ = _written(overlap, [_text("The first is short."), _tool("t-2")])
        assert _said(second) == ["The first is short.", "t-2"] and overlap.marked is False


class TestWhatTheFilterCannotTell:
    """The two cases that lose a line: neither can be told from a true
    overlap of one row. Pinned so a change that moves them is seen."""

    def test_a_line_said_again_after_a_gap_is_taken_for_the_saved_one(self):
        saved = _early([_tool("t-1", tool_result="x"), _text("Running the next batch.")])
        overlap = ReplayOverlap(saved, MARK)
        kept, _ = _written(overlap, [_text("Running the next batch."), _tool("t-10"),
                                     _text("All green.")])
        assert _said(kept) == ["t-10", "All green."] and overlap.marked is False

    def test_a_new_text_that_starts_with_the_last_saved_one_loses_its_start(self):
        saved = _early([_tool("t-1"), _text("I will now run the whole suite again.")])
        overlap = ReplayOverlap(saved, MARK)
        kept, _ = _written(overlap, [
            _text("I will now run the whole suite again. It is green this time."), _tool("t-9")])
        assert _said(kept) == ["It is green this time.", "t-9"] and overlap.marked is False


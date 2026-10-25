"""Progress notes in the pump: a TEXT with ``note`` is text kept in a segment
of its own (its own assistant row, ``event_data {"note": true}``), a Claude
narration block re-sent as a note leaves no thinking row, a thinking
``end`` is read by its phase before its text, and the note mark (an empty
note TEXT) turns the plain text still pending into a note."""

import asyncio
import json

import pytest

from core.events import chat_writer, stream_pump
from core.events.common_events import (
    PRODUCER_DONE,
    TEXT,
    THINKING,
    TOOL_RESULT,
    TOOL_USE,
    CommonEvent,
    note_mark,
)
from core.events.stream_pump import ChatStreamPump, _delta_key, _serialize_turn_rows
from storage import database as task_store


def _scripted_pump(chat_id: str, events: list[CommonEvent]) -> ChatStreamPump:
    event_queue: asyncio.Queue = asyncio.Queue()

    async def _produce():
        for ev in events:
            await event_queue.put(ev)
            await asyncio.sleep(0)
        await event_queue.put(CommonEvent(type=PRODUCER_DONE, data={}))
        await asyncio.sleep(3600)

    producer = asyncio.get_event_loop().create_task(_produce())
    pump = ChatStreamPump(chat_id=chat_id, session_id=f"sess-{chat_id}", producer=producer,
                          event_queue=event_queue, perm_queue=None,
                          chat_owner="user-admin", chat_agent="a1")
    stream_pump._active_pumps[chat_id] = pump
    return pump


async def _rows(temp_db, chat_id: str, events: list[CommonEvent]) -> list[tuple]:
    temp_db.create_chat(chat_id, "user-admin", "a1")
    pump = _scripted_pump(chat_id, events)
    try:
        await pump.start()
    finally:
        stream_pump._chat_streaming_state.pop(chat_id, None)
        pump.producer.cancel()
    out = []
    for m in task_store.get_chat_messages(chat_id):
        ed = json.loads(m["event_data"]) if m.get("event_data") else {}
        out.append((m["role"], m.get("event_type") or "", m["content"] or ed.get("content", ""),
                    bool(ed.get("note"))))
    return out


def _text(content: str, note: bool = False) -> CommonEvent:
    return CommonEvent(type=TEXT, data={"content": content, **({"note": True} if note else {})})


@pytest.mark.asyncio
async def test_a_note_is_its_own_text_row_and_the_answer_another(temp_db):
    rows = await _rows(temp_db, "pn1", [
        _text("I'll list ", note=True), _text("it.", note=True),
        CommonEvent(type=TOOL_USE, data={"name": "Bash", "tool_id": "t1"}),
        CommonEvent(type=TOOL_RESULT, data={"name": "Bash", "tool_id": "t1"}),
        _text("Now the count.", note=True),
        _text("Done: 3 files."),
    ])
    texts = [(r[2], r[3]) for r in rows if r[0] == "assistant"]
    assert texts == [("I'll list it.", True), ("Now the count.", True), ("Done: 3 files.", False)]


@pytest.mark.asyncio
async def test_a_narration_block_resent_as_a_note_leaves_no_thinking_row(temp_db):
    rows = await _rows(temp_db, "pn2", [
        CommonEvent(type=THINKING, data={"phase": "start"}),
        CommonEvent(type=THINKING, data={"text": "Read it. Next the run."}),
        CommonEvent(type=THINKING, data={"phase": "end", "note": True}),
        _text("Read it. Next the run.", note=True),
        _text("All good."),
    ])
    assert [r for r in rows if r[1] == "thinking"] == []
    assert [(r[2], r[3]) for r in rows if r[0] == "assistant"] == [
        ("Read it. Next the run.", True), ("All good.", False)]


@pytest.mark.asyncio
async def test_an_end_that_carries_text_closes_its_block_and_a_second_start_keeps_the_first(temp_db):
    rows = await _rows(temp_db, "pn3", [
        CommonEvent(type=THINKING, data={"phase": "start"}),
        CommonEvent(type=THINKING, data={"phase": "end", "text": "all at the end"}),
        CommonEvent(type=THINKING, data={"phase": "start"}),
        CommonEvent(type=THINKING, data={"phase": "delta", "text": "left open"}),
        CommonEvent(type=THINKING, data={"phase": "start"}),
        CommonEvent(type=THINKING, data={"phase": "delta", "text": "closed"}),
        CommonEvent(type=THINKING, data={"phase": "end", "text": ""}),
    ])
    assert [r[2] for r in rows if r[1] == "thinking"] == ["all at the end", "left open", "closed"]


def test_a_note_row_keeps_the_meeting_speaker_beside_the_mark(monkeypatch):
    from storage.agents import agent_store
    monkeypatch.setattr(agent_store, "get_agent", lambda slug: {"display_name": "Writer", "color": "#123"})
    (row,) = _serialize_turn_rows([{"type": "text", "content": "Noted.", "note": True,
                                    "_meeting_agent": "writer"}])
    role, content, event_type, event_data = row
    assert (role, content, event_type) == ("assistant", "Noted.", "")
    assert json.loads(event_data) == {"note": True, "agent_slug": "writer", "agent_display_name": "Writer",
                                      "agent_color": "#123", "badge": "meeting"}
    (plain,) = _serialize_turn_rows([{"type": "text", "content": "Plain."}])
    assert plain[3] == ""


def test_notes_coalesce_only_with_notes():
    note = _delta_key(TEXT, {"content": "a", "note": True})
    plain = _delta_key(TEXT, {"content": "a"})
    assert note is not None and plain is not None and note != plain
    assert _delta_key(TEXT, {"content": "a", "other": 1}) is None


@pytest.mark.asyncio
async def test_the_live_view_never_puts_an_answer_inside_a_note():
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    pump = ChatStreamPump(chat_id="pn-live", session_id="s-live", producer=producer,
                          event_queue=asyncio.Queue(), perm_queue=None)
    try:
        live = {"live_blocks": []}
        pump._live_append_text(live, "Answer ", False)
        pump._live_append_text(live, "I'll check", True)
        pump._live_append_text(live, " it.", True)
        pump._live_append_text(live, "Checked.", False)
        pump._live_append_text(live, "\n\n")  # the turn's separator joins either kind
        assert live["live_blocks"] == [
            {"type": "text", "content": "Answer "},
            {"type": "text", "content": "I'll check it.", "note": True},
            {"type": "text", "content": "Checked.\n\n"},
        ]
    finally:
        producer.cancel()


# --- the note mark ------------------------------------------------------------

def _tool(tool_id: str) -> list[CommonEvent]:
    return [CommonEvent(type=TOOL_USE, data={"name": "Bash", "tool_id": tool_id}),
            CommonEvent(type=TOOL_RESULT, data={"name": "Bash", "tool_id": tool_id})]


@pytest.mark.asyncio
async def test_the_mark_turns_the_pending_text_into_a_note_row_and_the_answer_stays_text(temp_db):
    rows = await _rows(temp_db, "pm1", [
        _text("I'll list "), _text("the folder."), note_mark(), *_tool("t1"),
        _text("Three files."),
    ])
    assert [(r[2], r[3]) for r in rows if r[0] == "assistant"] == [
        ("I'll list the folder.", True), ("Three files.", False)]


@pytest.mark.asyncio
async def test_a_mark_with_nothing_pending_or_after_a_note_changes_nothing(temp_db):
    rows = await _rows(temp_db, "pm2", [
        _text("Before."), *_tool("t1"), note_mark(), *_tool("t2"),
        _text("A note.", note=True), note_mark(), *_tool("t3"),
        _text("Done."),
    ])
    assert [(r[2], r[3]) for r in rows if r[0] == "assistant"] == [
        ("Before.", False), ("A note.", True), ("Done.", False)]


def _live_pump(chat_id: str) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    pump = ChatStreamPump(chat_id=chat_id, session_id=f"s-{chat_id}", producer=producer,
                          event_queue=asyncio.Queue(), perm_queue=None)
    pump._init_live_state()
    return pump


def _frames(queue: asyncio.Queue) -> list[dict]:
    out = []
    while not queue.empty():
        item = queue.get_nowait()
        if item.get("pump_type") == "ws_event":
            out.append(item["event"])
    return out


@pytest.mark.asyncio
async def test_the_mark_reaches_the_page_after_the_text_and_before_the_tool_never_merged():
    pump = _live_pump("pm-frames")
    queue = pump.attach()
    try:
        for ev in (_text("I'll "), _text("look."), note_mark(), _tool("t1")[0], _text("A note.", note=True)):
            await pump._process_event(ev)
        pump._flush_deltas()
        frames = [(f["type"], f.get("content"), f.get("note", False)) for f in _frames(queue)]
        assert frames == [("text", "I'll ", False), ("text", "look.", False), ("text", "", True),
                          ("tool_start", None, False), ("text", "A note.", True)]
        live = stream_pump._chat_streaming_state["pm-frames"]["live_blocks"]
        assert [(b["type"], b.get("content"), b.get("note", False)) for b in live][:2] == [
            ("text", "I'll look.", True), ("tool", None, False)]
    finally:
        stream_pump._chat_streaming_state.pop("pm-frames", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_live_block_holding_earlier_words_is_split_at_the_mark():
    pump = _live_pump("pm-split")
    try:
        live = stream_pump._chat_streaming_state["pm-split"]
        live["live_blocks"].append({"type": "text", "content": "Earlier words. "})
        pump._pending_text.append("Now the preamble.")
        live["live_blocks"][-1]["content"] += "Now the preamble."
        await pump._note_open_text(live)
        assert live["live_blocks"] == [{"type": "text", "content": "Earlier words. "},
                                       {"type": "text", "content": "Now the preamble.", "note": True}]
        assert pump._pending_note is True
    finally:
        stream_pump._chat_streaming_state.pop("pm-split", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_long_preamble_past_a_due_checkpoint_is_saved_whole_as_a_note(temp_db):
    """A checkpoint never takes pending words: however long the preamble
    streams, the mark finds all of it, and the checkpoint after the tool
    starts saves it as one note row."""
    temp_db.create_chat("pm-ck", "user-admin", "a1")
    pump = _live_pump("pm-ck")
    stream_pump._active_pumps["pm-ck"] = pump
    try:
        for ev in _tool("t0"):
            await pump._process_event(ev)
        for n in range(40):
            await pump._process_event(_text(f"word{n} "))
            pump._last_save_at = float("-inf")
            await pump._maybe_checkpoint()
        await pump._process_event(note_mark())
        await pump._process_event(_tool("t1")[0])
        pump._last_save_at = float("-inf")
        await pump._maybe_checkpoint()
        await chat_writer.drain("pm-ck", timeout=5)
        notes = [(m["content"], json.loads(m["event_data"] or "{}").get("note"))
                 for m in task_store.get_chat_messages("pm-ck") if m["role"] == "assistant"]
        assert notes == [("".join(f"word{n} " for n in range(40)), True)]
    finally:
        stream_pump._active_pumps.pop("pm-ck", None)
        stream_pump._chat_streaming_state.pop("pm-ck", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_thinking_start_ends_the_segment_so_the_mark_folds_only_the_words_after_it(temp_db):
    """The real shape: words that end a message, the next message's empty
    thinking pair (the page draws a thinking block there), then a preamble
    and its mark. The rows split where the page splits; the live view splits
    the one text block both pieces landed in."""
    events = [
        _text("All three files are here."),
        CommonEvent(type=THINKING, data={"phase": "start"}),
        CommonEvent(type=THINKING, data={"phase": "end", "text": ""}),
        _text("\n\nNow the count."), note_mark(), *_tool("t1"),
        _text("Done: 3 files."),
    ]
    rows = await _rows(temp_db, "pm-think", events)
    assert [(r[2], r[3]) for r in rows if r[0] == "assistant"] == [
        ("All three files are here.", False), ("\n\nNow the count.", True), ("Done: 3 files.", False)]
    pump = _live_pump("pm-think-live")
    try:
        for ev in events[:5]:
            await pump._process_event(ev)
        live = stream_pump._chat_streaming_state["pm-think-live"]["live_blocks"]
        assert [(b["content"], b.get("note", False)) for b in live] == [
            ("All three files are here.", False), ("\n\nNow the count.", True)]
    finally:
        stream_pump._chat_streaming_state.pop("pm-think-live", None)
        pump.producer.cancel()

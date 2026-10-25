"""Turn checkpoints (``core/events/turn_checkpoint.py``): a long turn's
settled blocks saved while it runs, the checklist row rewritten in place, the
live state trimmed by identity, the open-turn record and its engine turns.

The core property: a turn that checkpoints after every step ends with the
same rows, in the same order, as the same turn with no checkpoint at all; and
a viewer reconnecting at any instant (the cutoff and the live blocks read
together, as ``ws/dashboard_chat_resume.py`` does) sees every block once.
"""

import asyncio
import copy
import json

import pytest

from core.events import (
    artifact_events,
    chat_writer,
    input_queue,
    stream_pump,
    turn_checkpoint,
    turn_ending,
)
from core.events.common_events import (
    BG_COMMAND_START,
    DONE,
    ERROR,
    PRODUCER_DONE,
    SUBAGENT_START,
    TEXT,
    THINKING,
    TOOL_INPUT,
    TOOL_RESULT,
    TOOL_USE,
    CommonEvent,
    TurnInput,
)
from core.events.stream_pump import ChatStreamPump, _active_pumps, _chat_streaming_state
from storage import database as task_store
from storage.chat import open_turns
from storage.pg import get_conn
from tests.fixtures.characterization.golden_master import load_json
from ws import wire_events as wire

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _quiet_media(monkeypatch):
    """The snapshot store's file work is not under test: its calls are
    recorded instead."""
    from services.media import preview_snapshots
    calls: list[str] = []
    monkeypatch.setattr(preview_snapshots, "delete_snapshot", lambda cid, snap: calls.append(f"delete:{snap}"))
    monkeypatch.setattr(preview_snapshots, "gc_chat", lambda cid: calls.append("gc"))
    return calls


@pytest.fixture
def quiet_end(monkeypatch):
    """A pump driven through its loop asks for a title and a library sweep
    at its end: neither is under test."""
    from services import title_generator
    from services.knowledge import library_projector

    async def _no_title(*a, **kw):
        return None
    monkeypatch.setattr(title_generator, "request_chat_title", _no_title)
    monkeypatch.setattr(library_projector, "schedule_reconcile_for_agent", lambda agent: None)


def _perm(name: str, **override) -> dict:
    item = copy.deepcopy(load_json("perm-items.json")[name])
    item.update(override)
    return item


def _injected(name: str) -> CommonEvent:
    ev = load_json("injected-events.json")[name]
    return CommonEvent(type=ev["type"], data=copy.deepcopy(ev["data"]))


def _ev(kind: str, **data) -> tuple:
    return ("event", CommonEvent(type=kind, data=data))


def _item(name: str, **override) -> tuple:
    return ("item", _perm(name, **override))


def _pump(chat_id: str, sid: str = "sid-ck") -> ChatStreamPump:
    producer = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
    pump = ChatStreamPump(chat_id=chat_id, session_id=sid, producer=producer,
                          event_queue=asyncio.Queue(), perm_queue=None)
    pump._init_live_state()
    return pump


def _cleanup(pump: ChatStreamPump) -> None:
    pump.producer.cancel()
    _active_pumps.pop(pump.chat_id, None)
    _chat_streaming_state.pop(pump.chat_id, None)
    stream_pump._pending_permissions.pop(pump.session_id, None)
    stream_pump._session_cumulative_cost.pop(pump.session_id, None)


async def _step(pump: ChatStreamPump, step: tuple) -> None:
    """One step: an engine event, a hook item, the card's answer (a plan
    review's action written after, as the socket writes it), or the
    turn's typed ending with its last save."""
    kind, payload = step
    if kind == "event":
        await pump._process_event(payload)
    elif kind == "item":
        await pump._handle_perm_event(payload)
    elif kind == "answer":
        await pump.resolve_active_permission()
        for block in pump._turn_blocks:
            if block.get("type") == wire.PLAN_REVIEW and block.get("request_id") == payload:
                block["action"] = "implement"
    else:
        await pump._on_error_event(payload)
        pump._save_turn_blocks()


async def _checkpoint(pump: ChatStreamPump) -> asyncio.Future | None:
    """A checkpoint now, whatever the clock says; its job, or None."""
    before = pump._checkpoint_fut
    pump._last_save_at = float("-inf")
    await pump._maybe_checkpoint()
    return pump._checkpoint_fut if pump._checkpoint_fut is not before else None


def _rows(chat_id: str) -> list[tuple]:
    out = []
    for r in task_store.get_chat_messages(chat_id):
        data = r.get("event_data") or ""
        out.append((r["role"], r.get("content") or "", r.get("event_type") or "",
                    json.loads(data) if data else None))
    return out


def _record(chat_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chat_open_turns WHERE chat_id=%s", (chat_id,)).fetchone()
    return dict(row) if row else None


def _long_turn() -> list[tuple]:
    """A long turn with every kind of block: text, a tool open while a
    picture arrives, a checklist updated after a checkpoint, a foreground
    subagent with its report, a transcode skeleton, a placeholder and its
    picture, a preview pushed twice, thinking, plan mode, a plan review
    answered and one that ran out, the engine's own permission prompt, a
    question, a background subagent, a background command, a delegation, a
    compaction, a system card, a check verdict, a meeting speaker's badge,
    the engine's cost, its result, then more work and a typed ending (a
    fresh copy each run)."""
    first = _injected("todo_update_cli")
    second = _injected("todo_update_cli")
    second.data["todos"] = [{"content": "alpha", "status": "completed"},
                            {"content": "beta", "status": "completed"}]
    return [
        _ev(TEXT, content="Looking at the repo. "),
        _ev(TOOL_USE, name="Bash", tool_id="t1"),
        _ev(TOOL_INPUT, name="Bash", summary="ls", tool_input={"command": "ls"}),
        _item("images"),
        _ev(TOOL_RESULT, name="Bash", tool_id="t1", result_content="a\nb"),
        ("event", first),
        _ev(TEXT, content="Two files. "),
        _ev(TOOL_USE, name="Write", tool_id="t2"),
        _ev(TOOL_RESULT, name="Write", tool_id="t2", result_content="written"),
        ("event", second),
        _ev(SUBAGENT_START, tool_use_id="a1", description="check", subagent_type="general",
            run_in_background=False, tool_input={"prompt": "check"}),
        _item("tool_result", tool_name="Agent", tool_use_id="a1", summary="report",
              result_content="all good"),
        _item("media_processing"),
        _item("image_generating"),
        _item("images"),
        _ev(TEXT, content="Writing a note. "),
        _ev(TOOL_USE, name="Write", tool_id="t3"),
        _item("document_preview"),
        _ev(TOOL_RESULT, name="Write", tool_id="t3", result_content="written"),
        _ev(TOOL_USE, name="Edit", tool_id="t4"),
        _item("document_preview_again"),
        _ev(TOOL_RESULT, name="Edit", tool_id="t4", result_content="edited"),
        _item("video_url"),
        _ev(THINKING, phase="start"),
        _ev(THINKING, text="Weighing the plan."),
        _ev(THINKING, phase="end", text=""),
        ("event", _injected("plan_mode_enter")),
        _item("plan_review_named"),
        _ev(TEXT, content="Waiting on the plan. "),
        ("answer", "req-plan-0001"),
        ("event", _injected("plan_mode_exit")),
        ("event", _injected("permission_request")),
        ("answer", "engine-req-0001"),
        _item("question"),
        _ev(SUBAGENT_START, tool_use_id="a2", description="watch", subagent_type="general",
            run_in_background=True, tool_input={"prompt": "watch"}),
        _ev(BG_COMMAND_START, tool_use_id="c1", command="sleep 60", description="a long wait"),
        ("event", _injected("delegate_spawn")),
        ("event", _injected("context_compact_completed")),
        ("event", _injected("system_other")),
        ("event", _injected("check_verdict")),
        ("event", _injected("meeting_turn_start")),
        _ev(TEXT, content="A word from the chair. "),
        _ev(TOOL_USE, name="Read", tool_id="t6"),
        _ev(TOOL_RESULT, name="Read", tool_id="t6", result_content="the minutes"),
        ("event", _injected("meeting_turn_end")),
        _item("plan_review_unnamed"),
        _item("prompt_retired", request_id="req-plan-0002"),
        _ev(TEXT, content="Done. "),
        ("event", _injected("metadata_delta")),
        _ev(DONE),
        _ev(TEXT, content="One more thing. "),
        _ev(TOOL_USE, name="Bash", tool_id="t5"),
        _ev(TOOL_RESULT, name="Bash", tool_id="t5", result_content="ok"),
        ("end", _injected("error_untyped")),
    ]


#: The blocks only history shows (a turn's own record, never a live block):
#: they appear once saved, earlier with checkpoints than without.
_HISTORY_ONLY = frozenset({wire.METADATA, wire.PLAN_REVIEW, wire.PERMISSION_PROMPT})
#: A live block's kind as its row names it.
_ROW_KIND = {wire.LIVE_AGENT: wire.TASK_SPAWN, wire.LIVE_COMMAND: wire.BG_COMMAND_SPAWN,
             wire.LIVE_DELEGATE: wire.DELEGATE_SPAWN}
#: The field that names one block of a kind built from two dicts.
_IDENTITY = {wire.PERSISTED_TOOL: "tool_id", wire.TASK_SPAWN: "tool_use_id",
             wire.BG_COMMAND_SPAWN: "tool_use_id", wire.DELEGATE_SPAWN: "task_id"}


def _key(block: dict) -> tuple | None:
    """A block's type and identity, the same for its row and its live block;
    None for a block only history shows."""
    kind = _ROW_KIND.get(block.get("type") or "", block.get("type") or "")
    if (kind in _HISTORY_ONLY or block.get("name") == "TodoWrite"
            or (kind == wire.SYSTEM and block.get("subtype") == wire.SUBTYPE_TURN_ENDED)):
        return None
    if kind in _IDENTITY:
        return (kind, block.get(_IDENTITY[kind]))
    return (kind, json.dumps({k: v for k, v in block.items() if not k.startswith("_")
                              and k not in artifact_events.LIVE_ONLY_KEYS}, sort_keys=True))


def _seen(pump: ChatStreamPump, chat_id: str) -> list[tuple]:
    """What a viewer reconnecting now sees: the rows up to the pump's cutoff,
    then the live blocks, as each block's type and identity in order, the
    text as runs (a row and a live block split one run differently; the
    paragraph break after a result is live only). A block that never gets a
    row (a transcode skeleton) is listed last, by presence: a checkpoint
    saves the blocks after it, so while it waits for its video it shows
    after them."""
    cutoff = pump._db_msg_cutoff_id or 0
    shown: list[dict] = []
    for r in task_store.get_chat_messages(chat_id):
        if r["id"] > cutoff or r["role"] == "user":
            continue
        shown.append({"type": wire.TEXT, "content": r["content"]} if r["role"] == "assistant"
                     else json.loads(r["event_data"] or "{}"))
    shown += (_chat_streaming_state.get(chat_id) or {}).get("live_blocks", [])
    out: list[tuple] = []
    skeletons: list[tuple] = []
    for block in shown:
        if not turn_checkpoint.rowed(block):
            skeletons.append((block["type"], json.dumps(block, sort_keys=True)))
        elif block.get("type") == wire.TEXT:
            words = block["content"].replace("\n\n", "")
            if out and out[-1][0] == wire.TEXT:
                out[-1] = (wire.TEXT, out[-1][1] + words)
            elif words:
                out.append((wire.TEXT, words))
        elif (key := _key(block)) is not None:
            out.append(key)
    return out + sorted(skeletons)


def _words(view: list[tuple]) -> str:
    return "".join(v[1] for v in view if v[0] == wire.TEXT)


@pytest.fixture
def quiet_prompts(monkeypatch):
    """The engine's prompt pings its person: not under test. The prompts it
    registers are withdrawn after the test."""
    from core.session import engine_prompts

    async def _no_ping(*a, **kw):
        return None
    monkeypatch.setattr(stream_pump.notification_manager, "fire_ephemeral", _no_ping)
    yield
    for chat_id in ("ck-plain", "ck-saved", "ck-seen", "ck-seen-plain"):
        for p in engine_prompts.pending_for_chat(chat_id):
            engine_prompts.withdraw(p.request_id)


async def test_a_turn_that_checkpoints_ends_with_the_rows_it_has_without(
        temp_db, _quiet_media, quiet_prompts):
    task_store.create_chat("ck-plain", "user-admin", "a1")
    task_store.create_chat("ck-saved", "user-admin", "a1")
    plain, saved = _pump("ck-plain"), _pump("ck-saved", sid="sid-ck-2")
    checkpoints = 0
    rewritten = False
    try:
        for step in _long_turn():
            await _step(plain, copy.deepcopy(step))
            await _step(saved, copy.deepcopy(step))
            if await _checkpoint(saved) is not None:
                checkpoints += 1
            rewritten = rewritten or saved._todo_ref is not None
            await chat_writer.drain("ck-saved", timeout=5)
        await chat_writer.drain("ck-plain", timeout=5)
        assert checkpoints >= 8 and rewritten
        assert _rows("ck-saved") == _rows("ck-plain")
        kinds = {r[2] or r[0] for r in _rows("ck-plain")}
        assert kinds >= {"assistant", wire.PERSISTED_TOOL, "images", "video",
                         wire.TASK_SPAWN, "document_preview", wire.THINKING, wire.PLAN_MODE,
                         wire.PLAN_REVIEW, wire.PERMISSION_PROMPT, wire.QUESTION,
                         wire.BG_COMMAND_SPAWN, wire.DELEGATE_SPAWN, wire.CONTEXT_COMPACT,
                         wire.SYSTEM, wire.CHECK_VERDICT, wire.METADATA}, kinds
        reviews = [r[3] for r in _rows("ck-plain") if r[2] == wire.PLAN_REVIEW]
        assert [rv.get("action") for rv in reviews] == ["implement", None]
        assert any((r[3] or {}).get("agent_slug") == "ceo" for r in _rows("ck-plain")
                   if r[0] == "assistant")
        assert _rows("ck-plain")[-1][3]["subtype"] == wire.SUBTYPE_TURN_ENDED
        # The preview pushed again replaced its first push in both chats.
        assert _quiet_media.count("delete:snap-1") == 2
    finally:
        _cleanup(plain)
        _cleanup(saved)


async def test_a_reconnect_sees_every_block_once_while_the_turn_checkpoints(temp_db, quiet_prompts):
    """At every step, a viewer reconnecting to the turn that checkpoints sees
    what one reconnecting to the same turn without checkpoints sees, block
    for block (the history-only kinds aside: a checkpoint shows them
    earlier), both while a checkpoint is on the lane and once it landed."""
    task_store.create_chat("ck-seen", "user-admin", "a1")
    task_store.create_chat("ck-seen-plain", "user-admin", "a1")
    pump, plain = _pump("ck-seen"), _pump("ck-seen-plain", sid="sid-ck-3")
    checkpoints = 0
    try:
        for step in _long_turn():
            await _step(plain, copy.deepcopy(step))
            await _step(pump, copy.deepcopy(step))
            for chat_id in ("ck-seen-plain", "ck-seen"):     # the step's own saves land
                await chat_writer.drain(chat_id, timeout=5)
            await asyncio.sleep(0)
            expected = _seen(plain, "ck-seen-plain")
            fut = await _checkpoint(pump)
            # Submitted, not landed: the rows are withheld by the cutoff.
            assert _seen(pump, "ck-seen") == expected, step
            if fut is not None:
                checkpoints += 1
                await fut
                await asyncio.sleep(0)
            # Landed: the rows are history, their live blocks gone.
            assert _seen(pump, "ck-seen") == expected, step
        assert checkpoints >= 8
        assert "A word from the chair. " in _words(expected)
    finally:
        _cleanup(pump)
        _cleanup(plain)


async def test_an_open_tool_holds_the_blocks_after_its_start(temp_db):
    task_store.create_chat("ck-open", "user-admin", "a1")
    pump = _pump("ck-open")
    try:
        for step in (_ev(TEXT, content="first "), _ev(TOOL_USE, name="Bash", tool_id="t1"),
                     _item("images")):
            await _step(pump, step)
        await (await _checkpoint(pump))
        assert [r[0] for r in _rows("ck-open")] == ["assistant"]
        assert [b["type"] for b in pump._turn_blocks] == ["images"]
        assert pump._active_tools["t1"]["_insert_idx"] == 0
        await _step(pump, _ev(TOOL_RESULT, name="Bash", tool_id="t1"))
        await (await _checkpoint(pump))
        assert [r[2] for r in _rows("ck-open")] == ["", wire.PERSISTED_TOOL, "images"]
    finally:
        _cleanup(pump)


async def test_a_block_with_no_row_stays_and_stops_nothing(temp_db):
    """A transcode skeleton never gets a row: the blocks after it are saved,
    it stays in front with its live block until its media replaces it."""
    task_store.create_chat("ck-skel", "user-admin", "a1")
    pump = _pump("ck-skel")
    try:
        await _step(pump, _item("media_processing"))
        await _step(pump, _ev(TOOL_USE, name="Bash", tool_id="t1"))
        await _step(pump, _ev(TOOL_RESULT, name="Bash", tool_id="t1"))
        await (await _checkpoint(pump))
        assert [r[2] for r in _rows("ck-skel")] == [wire.PERSISTED_TOOL]
        assert [b["type"] for b in pump._turn_blocks] == ["media_processing"]
        assert [b["type"] for b in _chat_streaming_state["ck-skel"]["live_blocks"]] == ["media_processing"]
        await _step(pump, _item("video_url"))
        assert [b["type"] for b in pump._turn_blocks] == ["video"]
    finally:
        _cleanup(pump)


async def test_words_after_a_checkpoint_start_a_live_block_of_their_own(temp_db):
    task_store.create_chat("ck-seal", "user-admin", "a1")
    pump = _pump("ck-seal")
    try:
        await _step(pump, _ev(TEXT, content="before "))
        await _step(pump, ("event", _injected("metadata_delta")))
        fut = await _checkpoint(pump)
        await _step(pump, _ev(TEXT, content="after "))
        await fut
        await asyncio.sleep(0)
        live = _chat_streaming_state["ck-seal"]["live_blocks"]
        assert [b["content"] for b in live] == ["after "]
        assert _rows("ck-seal")[0][1] == "before "
    finally:
        _cleanup(pump)


async def test_an_eviction_never_lets_words_into_a_block_being_trimmed(temp_db):
    """A failed generation removes its placeholder: the text block before it
    is last again, and a checkpoint in flight trims it."""
    task_store.create_chat("ck-evict", "user-admin", "a1")
    pump = _pump("ck-evict")
    try:
        await _step(pump, _ev(TEXT, content="a "))
        await _step(pump, _item("image_generating"))
        fut = await _checkpoint(pump)               # saves "a ", stops at the placeholder
        await _step(pump, _item("image_gen_failed"))
        await _step(pump, _ev(TEXT, content="b "))
        await fut
        await asyncio.sleep(0)
        assert _seen(pump, "ck-evict") == [(wire.TEXT, "a b ")]
    finally:
        _cleanup(pump)


async def test_the_stops_hold_while_they_should_and_then_release(temp_db, monkeypatch):
    """A plan review stops the checkpoint while its prompt is pending and a
    moment after its answer; a foreground subagent until its report, or its
    call's end; a placeholder until its picture; a held preview until the
    turn's end."""
    task_store.create_chat("ck-wait", "user-admin", "a1")
    cases = (
        (_item("plan_review_unnamed"), "answer"),
        (_ev(SUBAGENT_START, tool_use_id="a9", run_in_background=False),
         _item("tool_result", tool_name="Agent", tool_use_id="a9", summary="r", result_content="r")),
        (_ev(SUBAGENT_START, tool_use_id="a8", run_in_background=False),
         _ev(TOOL_RESULT, name="Agent", tool_id="a8")),
        (_item("image_generating"), _item("images")),
        (_item("document_preview"), None),
    )
    for held, settle in cases:
        pump = _pump("ck-wait")
        try:
            await _step(pump, _ev(TEXT, content="ahead "))
            await _step(pump, held)
            await _step(pump, _ev(TOOL_USE, name="Bash", tool_id="tw"))
            await _step(pump, _ev(TOOL_RESULT, name="Bash", tool_id="tw"))
            await (await _checkpoint(pump))
            assert _rows("ck-wait") == [("assistant", "ahead ", "", None)]
            if settle == "answer":
                await pump.resolve_active_permission()
                assert await _checkpoint(pump) is None      # the answer's moment
                monkeypatch.setattr(turn_checkpoint, "REVIEW_ANSWER_S", 0.0)
                settle = None
                fut = await _checkpoint(pump)
                assert fut is not None
                await fut
                assert pump._turn_blocks == []
            elif settle is not None:
                await _step(pump, settle)
                fut = await _checkpoint(pump)
                assert fut is not None
                await fut
                assert pump._turn_blocks == []
            else:
                assert await _checkpoint(pump) is None
        finally:
            _cleanup(pump)
            monkeypatch.setattr(turn_checkpoint, "REVIEW_ANSWER_S", 5.0)
        with get_conn() as conn:
            conn.execute("DELETE FROM chat_messages WHERE chat_id='ck-wait'")
            conn.commit()


async def test_a_checkpoint_waits_for_the_words_in_flight(temp_db):
    task_store.create_chat("ck-words", "user-admin", "a1")
    pump = _pump("ck-words")
    try:
        await _step(pump, _ev(TOOL_USE, name="Bash", tool_id="t1"))
        await _step(pump, _ev(TOOL_RESULT, name="Bash", tool_id="t1"))
        await _step(pump, _ev(TEXT, content="half a sent"))
        assert await _checkpoint(pump) is None
        await _step(pump, _ev(TOOL_USE, name="Bash", tool_id="t2"))
        assert await _checkpoint(pump) is not None
    finally:
        _cleanup(pump)


async def test_a_steer_after_a_checkpoint_keeps_the_order(temp_db):
    task_store.create_chat("ck-steer", "user-admin", "a1")
    pump = _pump("ck-steer")
    pump._recovery_eligible = False
    _active_pumps["ck-steer"] = pump
    try:
        await _step(pump, _ev(TEXT, content="one "))
        await _step(pump, _ev(TOOL_USE, name="Bash", tool_id="t1"))
        await _step(pump, _ev(TOOL_RESULT, name="Bash", tool_id="t1"))
        await _checkpoint(pump)                 # on the lane, not landed
        await _step(pump, _ev(TEXT, content="two "))
        qi = input_queue.QueuedInput(queue_id="", chat_id="ck-steer", author_sub="user-admin",
                                     item=TurnInput("a steer"))
        pump.accepts_in_flight += 1
        await pump._accept_steer({"item": qi, "extra_meta": None, "frame_extra": None})
        await chat_writer.drain("ck-steer", timeout=5)
        assert [(r[0], r[1] or r[2]) for r in _rows("ck-steer")] == [
            ("assistant", "one "), ("event", wire.PERSISTED_TOOL), ("assistant", "two "),
            ("user", "a steer")]
        assert _seen(pump, "ck-steer") == [(wire.TEXT, "one "), (wire.PERSISTED_TOOL, "t1"),
                                           (wire.TEXT, "two ")]
    finally:
        _cleanup(pump)


async def test_every_early_save_is_noted_with_its_rows_and_the_result_clears_them(temp_db):
    """The checkpoint's and the steer's ranges reach the open-turn record in
    their rows' transaction, for any turn; the engine's result makes the rows
    final (a replay covers the next engine turn only)."""
    task_store.create_chat("ck-note", "user-admin", "a1")
    pump = _pump("ck-note")
    pump._recovery_eligible = False
    pump._turn_token = "tok-note"
    open_turns.open_turn("ck-note", "tok-note", "sid-ck")
    try:
        await _step(pump, _ev(TEXT, content="saved early "))
        await _step(pump, _ev(TOOL_USE, name="Bash", tool_id="t1"))
        await _step(pump, _ev(TOOL_RESULT, name="Bash", tool_id="t1"))
        await (await _checkpoint(pump))
        ids = [r["id"] for r in task_store.get_chat_messages("ck-note")]
        assert json.loads(_record("ck-note")["early_ranges"]) == [[0, max(ids)]]
        assert pump._steer_saved_ranges == []        # the pump's own list is the steer's
        assert open_turns.take_record("ck-note", "tok-other") is None
        await _step(pump, _ev(DONE))
        await chat_writer.drain("ck-note", timeout=5)
        rec = _record("ck-note")
        assert (rec["engine_open"], rec["early_ranges"]) == (False, "[]")
    finally:
        _cleanup(pump)


async def test_a_save_keeps_the_blocks_it_trims_alive_until_it_lands(temp_db):
    """A full save in flight, the placeholder it carries evicted by its
    picture: no block appended before the landing can take the evicted
    dict's address, so the landing never trims a block it did not save."""
    task_store.create_chat("ck-pin", "user-admin", "a1")
    pump = _pump("ck-pin")
    try:
        await _step(pump, _ev(TEXT, content="a "))
        await _step(pump, _item("image_generating"))
        live = _chat_streaming_state["ck-pin"]["live_blocks"]
        evicted = id(live[-1])
        fut = pump._save_turn_blocks()          # on the lane, not landed
        await _step(pump, _item("images"))      # evicts the placeholder
        kept: list[dict] = []
        fresh = {"type": wire.TEXT, "content": "new words"}
        for _ in range(5000):                   # a recycled address, if one is free
            if id(fresh) == evicted:
                break
            kept.append(fresh)
            fresh = {"type": wire.TEXT, "content": "new words"}
        live.append(fresh)
        await fut
        await asyncio.sleep(0)
        assert any(b is fresh for b in _chat_streaming_state["ck-pin"]["live_blocks"])
    finally:
        _cleanup(pump)


async def test_a_turn_that_ends_with_nothing_new_still_indexes_its_checkpoints(temp_db):
    task_store.create_chat("ck-search", "user-admin", "a1")
    pump = _pump("ck-search")
    try:
        await _step(pump, _ev(TEXT, content="findable checkpointed words "))
        await _step(pump, _ev(TOOL_USE, name="Bash", tool_id="t1"))
        await _step(pump, _ev(TOOL_RESULT, name="Bash", tool_id="t1"))
        await (await _checkpoint(pump))
        assert pump._turn_blocks == [] and pump._search_owed

        def _indexed() -> str:
            with get_conn() as conn:
                row = conn.execute("SELECT content FROM chat_search WHERE chat_id='ck-search'").fetchone()
            return (row or {}).get("content") or ""
        assert "findable" not in _indexed()
        await _step(pump, _ev(DONE))            # the last save: nothing to write
        await chat_writer.drain("ck-search", timeout=5)
        assert "findable checkpointed words" in _indexed() and not pump._search_owed
    finally:
        _cleanup(pump)


async def test_a_compaction_opening_the_next_engine_turn_is_replayed_once(temp_db):
    """After the engine's result, a compaction is the next engine turn's
    first block: its checkpoint range survives the turn's first words, so a
    replay deletes it before writing it again."""
    task_store.create_chat("ck-compact", "user-admin", "a1")
    pump = _pump("ck-compact")
    pump._turn_token = "tok-compact"
    open_turns.open_turn("ck-compact", "tok-compact", "sid-ck")
    try:
        await _step(pump, _ev(TEXT, content="first turn "))
        await _step(pump, _ev(DONE))
        compaction = _injected("context_compact_completed")
        assert turn_checkpoint.opens_engine_turn(compaction)
        assert not turn_checkpoint.opens_engine_turn(_injected("context_compact_usage"))
        pump._engine_turn_begins()              # what the loop does for it
        await _step(pump, ("event", compaction))
        await (await _checkpoint(pump))
        await _step(pump, _ev(TEXT, content="second turn "))
        await chat_writer.drain("ck-compact", timeout=5)
        compact_row = next(r["id"] for r in task_store.get_chat_messages("ck-compact")
                           if r.get("event_type") == wire.CONTEXT_COMPACT)
        early = open_turns.take_record("ck-compact", "tok-compact").early
        assert any(after < compact_row <= upto for after, upto in early)
        task_store.delete_chat_message_ranges("ck-compact", early)
        assert [r[1] for r in _rows("ck-compact")] == ["first turn "]
    finally:
        _cleanup(pump)


async def _run(chat_id: str, events: list[CommonEvent], *, ending: bool = False):
    q: asyncio.Queue = asyncio.Queue()
    for ev in events:
        q.put_nowait(ev)
    producer = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
    pump = ChatStreamPump(chat_id=chat_id, session_id="sid-rec", producer=producer,
                          event_queue=q, perm_queue=None)
    _active_pumps[chat_id] = pump
    stream_pump._platform_ending = ending
    try:
        await pump.start()
        await chat_writer.drain(chat_id, timeout=5)
        return pump, open_turns.list_open()
    finally:
        stream_pump._platform_ending = False
        _cleanup(pump)


async def test_the_open_turn_record_lives_from_the_start_to_the_end(temp_db, quiet_end):
    task_store.create_chat("ck-rec", "user-admin", "a1")
    _, left = await _run("ck-rec", [CommonEvent(type=TEXT, data={"content": "hi"}),
                                    CommonEvent(type=DONE, data={}),
                                    CommonEvent(type=PRODUCER_DONE, data={})])
    assert left == []
    # Cut by a graceful restart before its result: the record stays.
    pump, left = await _run("ck-rec", [CommonEvent(type=TEXT, data={"content": "cut"}),
                                       CommonEvent(type=PRODUCER_DONE, data={})], ending=True)
    assert [(r["chat_id"], r["turn_token"], r["engine_open"]) for r in left] == [
        ("ck-rec", pump._turn_token, True)]
    # The next turn replaces it and, finishing, deletes its own.
    _, left = await _run("ck-rec", [CommonEvent(type=DONE, data={}),
                                    CommonEvent(type=PRODUCER_DONE, data={})], ending=True)
    assert left == []


async def test_a_prompt_after_the_result_is_a_new_engine_turn(temp_db, quiet_end):
    """A system prompt reaches the engine with no pump event: its first
    words reopen the engine turn, so a cut there keeps the record."""
    task_store.create_chat("ck-again", "user-admin", "a1")
    _, left = await _run("ck-again", [CommonEvent(type=TEXT, data={"content": "first"}),
                                      CommonEvent(type=DONE, data={}),
                                      CommonEvent(type=TEXT, data={"content": "second"}),
                                      CommonEvent(type=PRODUCER_DONE, data={})], ending=True)
    assert [r["engine_open"] for r in left] == [True]


async def test_a_pump_beside_the_chats_own_writes_no_record(temp_db, quiet_end):
    task_store.create_chat("ck-beside", "user-admin", "a1")
    owner = _pump("ck-beside", sid="sid-owner")
    _active_pumps["ck-beside"] = owner
    q: asyncio.Queue = asyncio.Queue()
    for ev in (CommonEvent(type=TEXT, data={"content": "wake"}), CommonEvent(type=DONE, data={}),
               CommonEvent(type=PRODUCER_DONE, data={})):
        q.put_nowait(ev)
    beside = ChatStreamPump(chat_id="ck-beside", session_id="sid-beside",
                            producer=asyncio.get_running_loop().create_task(asyncio.sleep(3600)),
                            event_queue=q, perm_queue=None)
    try:
        await beside.start()
        await chat_writer.drain("ck-beside", timeout=5)
        assert beside._turn_token == "" and open_turns.list_open() == []
    finally:
        beside.producer.cancel()
        _cleanup(owner)


async def test_a_shutdown_that_ends_the_engine_reads_as_the_restart(temp_db, quiet_end):
    task_store.create_chat("ck-exit", "user-admin", "a1")
    exited = turn_ending.TurnEnding(reason=turn_ending.EXITED, exit_code=143).as_dict()
    pump, left = await _run("ck-exit", [
        CommonEvent(type=TEXT, data={"content": "working"}),
        CommonEvent(type=ERROR, data={"message": "exited", "ending": exited})], ending=True)
    assert pump.last_ending.reason == turn_ending.RESTARTED
    cards = [r[3] for r in _rows("ck-exit") if r[2] == wire.SYSTEM]
    assert [c["reason"] for c in cards] == [turn_ending.RESTARTED]
    assert left == []


async def test_the_checkpoint_clock_is_thirty_seconds_of_the_loop(temp_db):
    task_store.create_chat("ck-clock", "user-admin", "a1")
    pump = _pump("ck-clock")
    try:
        await _step(pump, _ev(TEXT, content="x "))
        await _step(pump, _ev(TOOL_USE, name="Bash", tool_id="t1"))
        pump._last_save_at = asyncio.get_running_loop().time()
        await pump._maybe_checkpoint()
        assert pump._checkpoint_fut is None
        pump._last_save_at -= turn_checkpoint.CHECKPOINT_S
        await pump._maybe_checkpoint()
        assert pump._checkpoint_fut is not None
    finally:
        _cleanup(pump)


async def test_settled_prefix_never_splits_a_live_text_block():
    live = {"type": wire.LIVE_TEXT, "content": "a b"}
    first = {"type": wire.TEXT, "content": "a "}
    meta = {"type": wire.METADATA}
    review = {"type": wire.PLAN_REVIEW, "request_id": "r"}
    second = {"type": wire.TEXT, "content": "b"}
    twins = {id(first): (first, live), id(second): (second, live)}
    stops = turn_checkpoint.Stops(lambda rid: True, set(), {})
    assert turn_checkpoint.settled_prefix([first, meta, review, second], [], twins, stops) == 0
    assert turn_checkpoint.settled_prefix([first, meta, second], [], twins, stops) == 3

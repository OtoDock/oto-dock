"""The stream pump's persistence runs off the event loop, in turn order.

Regression fence for the 2026-09-04 hardening slice: every store call the
pump makes rides the per-chat writer lane (``core/events/chat_writer.py``),
the cutoff/live-state seam moves only when a save lands, a superseding
pump's cutoff observes its predecessor's rows, and the pump leaves
``_active_pumps`` only after its rows are committed.
"""

import asyncio
import threading

import pytest

from core.events import chat_writer, stream_pump
from core.events.common_events import (
    CommonEvent, DONE, PRODUCER_DONE, QUEUE_TURN, SYSTEM, TEXT, TOOL_RESULT, TOOL_USE,
)
from core.events.stream_pump import ChatStreamPump
from storage import database as task_store


def _scripted_pump(chat_id: str, events: list[CommonEvent], **kw) -> ChatStreamPump:
    """A pump whose producer replays ``events`` then PRODUCER_DONE, yielding
    between events like a real layer does."""
    event_queue: asyncio.Queue = asyncio.Queue()

    async def _produce():
        for ev in events:
            await event_queue.put(ev)
            await asyncio.sleep(0)
        await event_queue.put(CommonEvent(type=PRODUCER_DONE, data={}))
        await asyncio.sleep(3600)

    producer = asyncio.get_event_loop().create_task(_produce())
    pump = ChatStreamPump(
        chat_id=chat_id, session_id=f"sess-{chat_id}", producer=producer,
        event_queue=event_queue, perm_queue=None, **kw,
    )
    stream_pump._active_pumps[chat_id] = pump
    return pump


def _idle_pump(chat_id: str) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    return ChatStreamPump(
        chat_id=chat_id, session_id=f"sess-{chat_id}", producer=producer,
        event_queue=asyncio.Queue(), perm_queue=None,
    )


def _drain(q: asyncio.Queue) -> list[dict]:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return out


@pytest.fixture
def store_calls(monkeypatch):
    """Record (store function, thread ident, key arg) for the pump's writes."""
    calls: list[tuple[str, int, object]] = []
    real_add = task_store.add_chat_message
    real_upd = task_store.update_chat

    def rec_add(chat_id, role, *a, **k):
        calls.append(("add_chat_message", threading.get_ident(), role))
        return real_add(chat_id, role, *a, **k)

    def rec_upd(chat_id, **k):
        calls.append(("update_chat", threading.get_ident(), tuple(sorted(k))))
        return real_upd(chat_id, **k)

    real_batch = task_store.add_chat_messages_batch

    def rec_batch(chat_id, rows):
        for role, *_rest in rows:
            calls.append(("add_chat_message", threading.get_ident(), role))
        return real_batch(chat_id, rows)

    monkeypatch.setattr(stream_pump.task_store, "add_chat_message", rec_add)
    monkeypatch.setattr(stream_pump.task_store, "add_chat_messages_batch", rec_batch)
    monkeypatch.setattr(stream_pump.task_store, "update_chat", rec_upd)
    return calls


@pytest.mark.asyncio
async def test_full_turn_persists_off_loop_in_order(temp_db, loop_db_guard, store_calls):
    temp_db.create_chat("po1", "user-admin", "a1")
    task_store.add_chat_message("po1", "user", "hi")
    store_calls.clear()  # the seed row above is the test's own loop-side write
    loop_ident = threading.get_ident()
    pump = _scripted_pump(
        "po1", [CommonEvent(type=TEXT, data={"content": "Hello!"})],
        chat_owner="user-admin", chat_agent="a1",
    )
    q = pump.attach()
    try:
        with loop_db_guard.active():
            await pump.start()
        # The pump left the registry only after its rows landed: no drain
        # needed here, the DB already has them.
        assert "po1" not in stream_pump._active_pumps
        rows = [(m["role"], m["content"]) for m in task_store.get_chat_messages("po1")]
        assert rows == [("user", "hi"), ("assistant", "Hello!")]
        assert task_store.get_chat("po1")["last_response_at"]
        # Writer order: the assistant row before the turn-end stamp; every
        # write on a DB-executor thread, none on the loop.
        kinds = [(c[0], c[2]) for c in store_calls]
        assert ("add_chat_message", "assistant") in kinds
        assert kinds.index(("add_chat_message", "assistant")) < kinds.index(
            ("update_chat", ("last_response_at",)))
        assert all(c[1] != loop_ident for c in store_calls)
        # all_done reached the viewer AFTER the rows (it follows the drain),
        # then pump_ended.
        types = [f.get("pump_type") for f in _drain(q)]
        assert types.index("all_done") < types.index("pump_ended")
        assert pump._db_msg_cutoff_id == task_store.get_last_chat_message_id("po1")
    finally:
        stream_pump._chat_streaming_state.pop("po1", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_cutoff_is_none_until_the_start_job_lands(temp_db):
    temp_db.create_chat("po2", "user-admin", "a1")
    task_store.add_chat_message("po2", "user", "earlier")
    before = task_store.get_last_chat_message_id("po2")
    pump = _idle_pump("po2")
    try:
        assert pump._db_msg_cutoff_id is None  # withhold nothing meanwhile
        pump._submit_turn_start()
        assert await chat_writer.drain("po2", timeout=5)
        assert pump._db_msg_cutoff_id == before
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_superseding_pump_cutoff_observes_predecessor_rows(temp_db):
    temp_db.create_chat("po3", "user-admin", "a1")
    first = _idle_pump("po3")
    second = _idle_pump("po3")
    try:
        first._turn_blocks.append({"type": "text", "content": "turn one"})
        first._save_turn_blocks()          # queued on the lane...
        second._submit_turn_start()        # ...and the next pump's start job behind it
        assert await chat_writer.drain("po3", timeout=5)
        last = task_store.get_last_chat_message_id("po3")
        assert first._db_msg_cutoff_id == last
        assert second._db_msg_cutoff_id == last  # sees turn one's row
    finally:
        first.producer.cancel()
        second.producer.cancel()


@pytest.mark.asyncio
async def test_live_blocks_trim_only_when_the_save_lands(temp_db, monkeypatch):
    temp_db.create_chat("po4", "user-admin", "a1")
    gate = threading.Event()
    real_batch = task_store.add_chat_messages_batch

    def slow_batch(*a, **k):
        gate.wait(5)
        return real_batch(*a, **k)
    monkeypatch.setattr(stream_pump.task_store, "add_chat_messages_batch", slow_batch)

    pump = _idle_pump("po4")
    live = {"live_blocks": [], "session_id": pump.session_id}
    stream_pump._chat_streaming_state["po4"] = live
    try:
        blk_x = {"type": "text", "content": "X"}
        pump._turn_blocks.append(blk_x)
        live["live_blocks"].append(blk_x)
        fut = pump._save_turn_blocks()
        # Job in flight: a viewer reconnecting now still sees X from live_state.
        blk_y = {"type": "text", "content": "Y"}
        live["live_blocks"].append(blk_y)
        await asyncio.sleep(0.05)
        assert live["live_blocks"] == [blk_x, blk_y]
        gate.set()
        await fut
        await asyncio.sleep(0)  # the done-callback runs on the loop
        # Exactly the persisted block was trimmed; the later one survives.
        assert live["live_blocks"] == [blk_y]
        assert [m["content"] for m in task_store.get_chat_messages("po4")] == ["X"]
    finally:
        stream_pump._chat_streaming_state.pop("po4", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_meeting_rows_keep_chronological_order(temp_db, loop_db_guard):
    temp_db.create_chat("meeting-po5", "user-admin", "a1")
    pump = _scripted_pump(
        "meeting-po5",
        [
            CommonEvent(type=SYSTEM, data={"subtype": "meeting_started", "participants": []}),
            CommonEvent(type=QUEUE_TURN, data={"text": "first prompt"}),
            CommonEvent(type=TEXT, data={"content": "answer"}),
            CommonEvent(type=DONE, data={}),
        ],
        chat_owner="user-admin", chat_agent="a1",
    )
    pump.attach()
    try:
        with loop_db_guard.active():
            await pump.start()
        rows = [(m["role"], m.get("event_type") or "", m["content"])
                for m in task_store.get_chat_messages("meeting-po5")]
        assert rows == [
            ("event", "system", ""),
            ("user", "", "first prompt"),
            ("assistant", "", "answer"),
        ]
    finally:
        stream_pump._chat_streaming_state.pop("meeting-po5", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_recovery_suppress_skips_the_save(temp_db):
    temp_db.create_chat("po6", "user-admin", "a1")
    pump = _idle_pump("po6")
    try:
        stream_pump.suppress_recovery_flush("po6")
        pump._turn_blocks.append({"type": "text", "content": "lost turn"})
        assert pump._save_turn_blocks() is None
        assert pump._turn_blocks == []
        assert await chat_writer.drain("po6", timeout=5)
        assert task_store.get_chat_messages("po6") == []
    finally:
        stream_pump._recovery_suppress_flush.discard("po6")
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_turn_rebuilds_the_search_row_once(temp_db, monkeypatch):
    """A turn's text segments land in one batch with ONE
    rebuild of the chat's search row, not one per segment."""
    from storage.chat import db_chats
    rebuilds = []
    real = db_chats._rebuild_chat_search_row

    def counting(conn, chat_id):
        rebuilds.append(chat_id)
        return real(conn, chat_id)

    monkeypatch.setattr(db_chats, "_rebuild_chat_search_row", counting)
    temp_db.create_chat("po-seg", "user-admin", "a1")
    events = []
    for i in range(6):
        events += [
            CommonEvent(type=TEXT, data={"content": f"segment {i}"}),
            CommonEvent(type=TOOL_USE, data={"name": "Bash", "tool_id": f"t{i}"}),
            CommonEvent(type=TOOL_RESULT, data={"tool_id": f"t{i}", "content": "ok"}),
        ]
    pump = _scripted_pump("po-seg", events, chat_owner="user-admin", chat_agent="a1")
    try:
        await pump.start()
        texts = [m["content"] for m in task_store.get_chat_messages("po-seg")
                 if m["role"] == "assistant"]
        assert texts == [f"segment {i}" for i in range(6)]
        assert rebuilds == ["po-seg"]
        assert task_store.search_chats("user-admin", "a1", "segment")
    finally:
        stream_pump._chat_streaming_state.pop("po-seg", None)
        pump.producer.cancel()


def test_a_bad_row_in_a_batch_costs_only_itself(temp_db):
    """One row the database refuses (a NUL byte) no longer takes the rest of
    the turn with it: the batch falls back to row by row."""
    temp_db.create_chat("po-bad", "user-admin", "a1")
    last = task_store.add_chat_messages_batch("po-bad", [
        ("assistant", "before", "", ""),
        ("assistant", "bad\x00row", "", ""),
        ("event", "", "tool", "{}"),
        ("assistant", "after", "", ""),
    ])
    rows = task_store.get_chat_messages("po-bad")
    assert [r["content"] for r in rows if r["role"] == "assistant"] == ["before", "after"]
    assert [r["event_type"] for r in rows if r["role"] == "event"] == ["tool"]
    assert last == rows[-1]["id"]


def test_a_failed_search_rebuild_keeps_the_rows_and_warns(temp_db, monkeypatch, caplog):
    from storage.chat import db_chats

    def boom(conn, chat_id):
        raise RuntimeError("tsvector too long")

    monkeypatch.setattr(db_chats, "_rebuild_chat_search_row", boom)
    monkeypatch.setattr(db_chats, "_rebuild_warned_at", 0.0)
    temp_db.create_chat("po-warn", "user-admin", "a1")
    with caplog.at_level("WARNING", logger="db_chats"):
        task_store.add_chat_messages_batch("po-warn", [("assistant", "kept", "", "")])
        task_store.add_chat_message("po-warn", "user", "also kept")
    assert [m["content"] for m in task_store.get_chat_messages("po-warn")] == [
        "kept", "also kept"]
    warnings = [r for r in caplog.records if "chat_search rebuild failed" in r.getMessage()]
    assert len(warnings) == 1          # rate-limited


def _batch_fallbacks(monkeypatch) -> list:
    """Record the row-by-row fallback's writes (the batch calls the module's
    own ``add_chat_message``)."""
    from storage.chat import db_chats
    calls: list = []
    real = db_chats.add_chat_message

    def recording(*a, **kw):
        calls.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(db_chats, "add_chat_message", recording)
    return calls


@pytest.mark.parametrize("error", ["operational", "pool_timeout"])
def test_a_connection_failure_fails_the_batch_once(temp_db, monkeypatch, error):
    """A failure of the connection or the pool is not a row's fault: the
    batch raises once instead of retrying every row through the same broken
    path."""
    import psycopg
    import psycopg_pool
    from storage.chat import db_chats
    temp_db.create_chat("po-conn", "user-admin", "a1")
    fallbacks = _batch_fallbacks(monkeypatch)
    exc = (psycopg.OperationalError("server closed the connection unexpectedly")
           if error == "operational" else psycopg_pool.PoolTimeout("couldn't get a connection"))

    def broken(*a, **kw):
        raise exc

    monkeypatch.setattr(db_chats, "get_conn", broken)
    with pytest.raises(type(exc)):
        task_store.add_chat_messages_batch("po-conn", [("assistant", "a", "", ""),
                                                       ("assistant", "b", "", "")])
    assert fallbacks == []


def test_a_commit_with_an_unknown_outcome_is_not_written_again(temp_db, monkeypatch):
    """A commit that fails may have landed on the server: the rows are never
    written a second time row by row (a duplicated turn)."""
    import contextlib
    import psycopg
    from storage.chat import db_chats
    temp_db.create_chat("po-commit", "user-admin", "a1")
    fallbacks = _batch_fallbacks(monkeypatch)
    real = db_chats.get_conn

    @contextlib.contextmanager
    def failing_commit(*a, **kw):
        with real(*a, **kw) as conn:
            class _Conn:
                def execute(self, *ea, **ekw):
                    return conn.execute(*ea, **ekw)

                def transaction(self):
                    return conn.transaction()

                def commit(self):
                    raise psycopg.OperationalError("connection lost during COMMIT")

            yield _Conn()

    monkeypatch.setattr(db_chats, "get_conn", failing_commit)
    with pytest.raises(psycopg.OperationalError):
        task_store.add_chat_messages_batch("po-commit", [("assistant", "once", "", "")])
    assert fallbacks == []


def test_a_deleted_chat_fails_the_batch_once(temp_db, monkeypatch):
    """Every row of a turn whose chat is gone breaks the same foreign key: no
    row-by-row retry."""
    import psycopg
    fallbacks = _batch_fallbacks(monkeypatch)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        task_store.add_chat_messages_batch("po-gone", [("assistant", "a", "", ""),
                                                       ("assistant", "b", "", "")])
    assert fallbacks == []


def test_a_search_rebuild_that_cannot_connect_keeps_the_batch(temp_db, monkeypatch):
    """The rows landed: a pool timeout on the search rebuild after them is
    logged, never raised, so the writer lane counts the turn as saved."""
    import psycopg_pool
    from storage.chat import db_chats
    temp_db.create_chat("po-search", "user-admin", "a1")
    real = db_chats.get_conn
    calls = {"n": 0}

    def first_only(*a, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise psycopg_pool.PoolTimeout("couldn't get a connection")
        return real(*a, **kw)

    monkeypatch.setattr(db_chats, "get_conn", first_only)
    monkeypatch.setattr(db_chats, "_rebuild_warned_at", 0.0)
    last = task_store.add_chat_messages_batch("po-search", [("assistant", "landed", "", "")])
    monkeypatch.setattr(db_chats, "get_conn", real)
    rows = task_store.get_chat_messages("po-search")
    assert [r["content"] for r in rows] == ["landed"]
    assert last == rows[-1]["id"]


def test_a_lone_surrogate_costs_only_its_row(temp_db):
    """Text the client cannot encode (half a surrogate pair from a CLI's
    JSON) is a row's fault, like a NUL byte."""
    temp_db.create_chat("po-sur", "user-admin", "a1")
    task_store.add_chat_messages_batch("po-sur", [
        ("assistant", "before", "", ""),
        ("assistant", "half \ud800 pair", "", ""),
        ("assistant", "after", "", ""),
    ])
    assert [r["content"] for r in task_store.get_chat_messages("po-sur")] == ["before", "after"]


def test_a_pool_timeout_before_the_commit_is_tried_once_more(temp_db, monkeypatch):
    """Nothing was committed, so the whole batch is safe to send again once:
    a busy pool does not cost the turn."""
    import psycopg_pool
    from storage.chat import db_chats
    temp_db.create_chat("po-retry", "user-admin", "a1")
    fallbacks = _batch_fallbacks(monkeypatch)
    real = db_chats.get_conn
    calls = {"n": 0}

    def busy_once(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg_pool.PoolTimeout("couldn't get a connection")
        return real(*a, **kw)

    monkeypatch.setattr(db_chats, "get_conn", busy_once)
    last = task_store.add_chat_messages_batch("po-retry", [("assistant", "a", "", ""),
                                                           ("assistant", "b", "", "")])
    monkeypatch.setattr(db_chats, "get_conn", real)
    rows = task_store.get_chat_messages("po-retry")
    assert [r["content"] for r in rows] == ["a", "b"] and last == rows[-1]["id"]
    assert fallbacks == []


def test_a_cancel_before_the_commit_is_not_sent_again(temp_db, monkeypatch):
    """A statement timeout or an admin cancel is the server's answer, not a
    busy pool: the batch raises at once instead of holding the lane twice."""
    import psycopg
    from storage.chat import db_chats
    temp_db.create_chat("po-cancel", "user-admin", "a1")
    calls = {"n": 0}

    def cancelled(*a, **kw):
        calls["n"] += 1
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(db_chats, "get_conn", cancelled)
    with pytest.raises(psycopg.errors.QueryCanceled):
        task_store.add_chat_messages_batch("po-cancel", [("assistant", "a", "", "")])
    assert calls["n"] == 1

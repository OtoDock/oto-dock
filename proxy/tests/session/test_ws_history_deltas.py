"""A watched task chat's history as deltas (ws/dashboard_chat_resume.py).

A client that declared ``history_deltas`` and asks for one gets the rows
above the floors of the last history this connection sent for the chat,
the sibling runs included; a resume without the flag, a chat with no base,
or an older client is sent the full history, sorted by id. The pure parts:
``_cut_rows`` (a live pump's cutoff withholds its tail and pins the floor),
``_encode_history`` (the frame text with the rows spliced in) and the store
query behind the delta.
"""

import json

import pytest

from storage import database as task_store
from tests.fixtures.ws_dashboard_harness import (
    FakeExecutionLayer, dashboard_connection, drain_startup, make_test_agent,
    run_ws_scenario, session_cookie, stub_dashboard_seams,
)
import ws.dashboard  # noqa: F401  (assembles the mixins first)
from ws import dashboard_chat_resume as resume


async def _until(ws, ftype: str, *, limit: int = 12) -> dict:
    seen = []
    for _ in range(limit):
        frame = await ws.next_frame()
        if frame.get("type") == ftype:
            return frame
        seen.append(frame.get("type"))
    raise AssertionError(f"no {ftype!r} frame; saw {seen}")


def _task_chat(slug: str, run_id: str, session_id: str = "") -> str:
    cid = f"task-{run_id}"
    task_store.create_run(run_id, "t1", slug, "manual", None, "do it")
    task_store.update_run(run_id, chat_id=cid, **({"session_id": session_id} if session_id else {}))
    task_store.create_chat(cid, "user-admin", slug, "auto", model="test-model",
                           execution_path="claude-code-cli", source_type="task")
    return cid


class TestDeltas:
    def test_a_delta_carries_the_rows_above_the_floor_then_a_plain_resume_is_full(
            self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        cid = _task_chat(slug, "run-d1")
        r1 = task_store.add_chat_message(cid, "user", "first", author_sub="user-admin")
        r2 = task_store.add_chat_message(cid, "assistant", "one")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "client_info", "platform": "web", "history_deltas": True})
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                full = await _until(ws, "chat_history")
                assert [m["id"] for m in full["messages"]] == [r1, r2]
                r3 = task_store.add_chat_message(cid, "assistant", "two")
                ws.client_send({"type": "resume_chat", "chat_id": cid, "delta": True})
                delta = await _until(ws, "chat_history_delta")
                assert delta["chat_id"] == cid and delta["since_id"] == r2
                assert [m["id"] for m in delta["messages"]] == [r3]
                assert delta["mode"] == "auto" and "has_more" in delta and delta["restore"] is not None
                # Nothing new: an empty delta, the floor unchanged.
                ws.client_send({"type": "resume_chat", "chat_id": cid, "delta": True})
                again = await _until(ws, "chat_history_delta")
                assert again["messages"] == [] and again["since_id"] == r3
                # A resume without the flag is the full history (a navigation).
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                full2 = await _until(ws, "chat_history")
                assert [m["id"] for m in full2["messages"]] == [r1, r2, r3]
                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)

    def test_an_older_client_and_a_chat_with_no_base_get_the_full_history(
            self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        a = _task_chat(slug, "run-d2a")
        b = _task_chat(slug, "run-d2b")
        task_store.add_chat_message(a, "user", "a1", author_sub="user-admin")
        task_store.add_chat_message(b, "user", "b1", author_sub="user-admin")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                # No capability declared: a delta request is a full history.
                ws.client_send({"type": "resume_chat", "chat_id": a})
                await _until(ws, "chat_history")
                ws.client_send({"type": "resume_chat", "chat_id": a, "delta": True})
                assert (await _until(ws, "chat_history"))["chat_id"] == a
                ws.client_send({"type": "client_info", "platform": "web", "history_deltas": True})
                ws.client_send({"type": "resume_chat", "chat_id": a})
                await _until(ws, "chat_history")
                # Another chat has no base on this connection: full.
                ws.client_send({"type": "resume_chat", "chat_id": b, "delta": True})
                frame = await _until(ws, "chat_history")
                assert frame["chat_id"] == b
                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)

    def test_sibling_runs_ride_the_history_in_id_order_and_the_delta(self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        a = _task_chat(slug, "run-d3a", session_id="sess-d3")
        b = _task_chat(slug, "run-d3b", session_id="sess-d3")
        r1 = task_store.add_chat_message(a, "user", "a1", author_sub="user-admin")
        r2 = task_store.add_chat_message(b, "user", "b1", author_sub="user-admin")
        r3 = task_store.add_chat_message(a, "assistant", "a2")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "client_info", "platform": "web", "history_deltas": True})
                ws.client_send({"type": "resume_chat", "chat_id": a})
                full = await _until(ws, "chat_history")
                assert [m["id"] for m in full["messages"]] == [r1, r2, r3]
                r4 = task_store.add_chat_message(b, "assistant", "b2")
                r5 = task_store.add_chat_message(a, "user", "a3", author_sub="user-admin")
                ws.client_send({"type": "resume_chat", "chat_id": a, "delta": True})
                delta = await _until(ws, "chat_history_delta")
                assert [m["id"] for m in delta["messages"]] == [r4, r5]
                assert delta["since_id"] == min(r2, r3)
                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)


    @pytest.mark.parametrize("seen_by_read", [False, True], ids=["missed", "seen"])
    @pytest.mark.parametrize("as_delta", [False, True], ids=["full", "delta"])
    def test_a_sibling_save_during_the_history_read_is_in_the_frame_or_the_snapshot(
            self, temp_db, monkeypatch, as_delta, seen_by_read):
        """g8: a live sibling run's save that lands while the resume reads the
        rows (before or after the SELECT ran, the cut not yet taken) shows in
        the history frame or in the attach's live_state, once."""
        import asyncio
        import threading

        from core.events import stream_pump
        from core.events.common_events import CommonEvent, TEXT
        from core.session import session_kind
        from tests.session.test_pump_attach_races import _seed_live

        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        a = _task_chat(slug, "run-g8a", session_id="sess-g8")
        b = _task_chat(slug, "run-g8b", session_id="sess-g8")
        task_store.add_chat_message(a, "user", "a1", author_sub="user-admin")
        b1 = task_store.add_chat_message(b, "user", "b1", author_sub="user-admin")
        armed, read_ran, release = threading.Event(), threading.Event(), threading.Event()

        def _hold_after(real, names_b):
            def held(*args, **kwargs):
                hold = armed.is_set() and names_b(args) and not release.is_set()
                if hold and seen_by_read:
                    read_ran.set()
                    release.wait(5)
                rows = real(*args, **kwargs)
                if hold and not seen_by_read:
                    read_ran.set()
                    release.wait(5)
                return rows
            return held
        monkeypatch.setattr(task_store, "get_chat_messages", _hold_after(
            task_store.get_chat_messages, lambda args: args[0] == b))
        monkeypatch.setattr(task_store, "get_chat_messages_since", _hold_after(
            task_store.get_chat_messages_since, lambda args: b in args[0]))

        async def scenario():
            pump = stream_pump.ChatStreamPump(
                chat_id=b, session_id="sess-g8",
                producer=asyncio.get_running_loop().create_task(asyncio.sleep(3600)),
                event_queue=asyncio.Queue(), perm_queue=None,
                source_type=session_kind.TASK.source_type,
            )
            try:
                async with dashboard_connection(session_cookie()) as ws:
                    await drain_startup(ws)
                    live = _seed_live(b, "sess-g8")
                    pump._db_msg_cutoff_id = b1
                    stream_pump._active_pumps[b] = pump
                    if as_delta:
                        ws.client_send({"type": "client_info", "platform": "web",
                                        "history_deltas": True})
                        ws.client_send({"type": "resume_chat", "chat_id": a})
                        await _until(ws, "chat_history")
                        await _until(ws, "live_state", limit=20)
                    await pump._process_event(CommonEvent(TEXT, {"content": "landed mid-read"}))
                    assert live["live_blocks"]
                    armed.set()
                    ws.client_send({"type": "resume_chat", "chat_id": a, "delta": as_delta})
                    assert await asyncio.to_thread(read_ran.wait, 5)
                    pump._flush_pending_text()
                    await pump._save_turn_blocks()
                    await asyncio.sleep(0)  # the save's done-callback runs
                    assert not live["live_blocks"]  # persisted and trimmed
                    release.set()
                    history = await _until(
                        ws, "chat_history_delta" if as_delta else "chat_history", limit=20)
                    snapshot = await _until(ws, "live_state", limit=20)
                    in_history = [m for m in history["messages"]
                                  if m.get("content") == "landed mid-read"]
                    in_live = [blk for blk in snapshot.get("live_blocks", [])
                               if blk.get("content") == "landed mid-read"]
                    assert len(in_history) + len(in_live) == 1, (in_history, in_live)
                    ws.client_send({"type": "close"})
            finally:
                release.set()
                stream_pump._active_pumps.pop(b, None)
                stream_pump._chat_streaming_state.pop(b, None)
                pump.producer.cancel()
        run_ws_scenario(scenario)

def _row(i: int, chat: str, role: str = "assistant") -> dict:
    return {"id": i, "chat_id": chat, "role": role, "content": f"m{i}"}


def test_cut_rows_withholds_a_live_tail_keeps_user_rows_and_pins_the_floor():
    rows = [_row(1, "a", "user"), _row(2, "a"), _row(3, "a"), _row(4, "a", "user"), _row(5, "b")]
    kept, floors = resume._cut_rows(rows, {"a": 2}, {"a": 0, "b": 0})
    assert [r["id"] for r in kept] == [1, 2, 4, 5]
    # The user row above the cut neither is withheld nor raises the floor:
    # the next delta re-sends it with rows 3 and up, deduped by the client.
    assert floors == {"a": 2, "b": 5}
    # A cutoff of None (the start job pending) withholds nothing.
    kept, floors = resume._cut_rows(rows, {"a": None}, {"a": 1, "b": 0})
    assert len(kept) == 5 and floors == {"a": 4, "b": 5}
    # A floor only rises: a chat with no row keeps the floor it had.
    _, floors = resume._cut_rows([], {}, {"a": 7})
    assert floors == {"a": 7}


def test_encode_history_splices_the_rows_into_the_frame():
    frame = {"type": "chat_history", "chat_id": "c", "restore": {"todos": []}, "process_alive": False}
    rows = [_row(1, "c", "user"), {"id": 2, "chat_id": "c", "role": "assistant", "content": "é ☃"}]
    text = resume._encode_history(frame, rows)
    assert json.loads(text) == {**frame, "messages": rows}
    assert "\\u" not in text  # ensure_ascii off, as the socket's own encoder
    assert json.loads(resume._encode_history(frame, [])) == {**frame, "messages": []}


def test_get_chat_messages_since_applies_each_chats_floor(temp_db):
    slug = make_test_agent()
    task_store.create_chat("since-a", "user-admin", slug)
    task_store.create_chat("since-b", "user-admin", slug)
    a1 = task_store.add_chat_message("since-a", "user", "a1")
    b1 = task_store.add_chat_message("since-b", "user", "b1")
    a2 = task_store.add_chat_message("since-a", "assistant", "a2")
    b2 = task_store.add_chat_message("since-b", "assistant", "b2")
    rows = task_store.get_chat_messages_since({"since-a": a1, "since-b": b1})
    assert [r["id"] for r in rows] == [a2, b2]
    # b's own floor keeps b1 out even though a's lower floor lets the query see it.
    rows = task_store.get_chat_messages_since({"since-a": 0, "since-b": b1})
    assert [r["id"] for r in rows] == [a1, a2, b2]
    assert task_store.get_chat_messages_since({}) == []

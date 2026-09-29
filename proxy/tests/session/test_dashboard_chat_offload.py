"""Dashboard chat handlers keep their store calls off the event loop.

Regression fence for the 2026-09-04 hardening slice: the converted frames
(``chat_read``, ``mode_change``, ``model_change``, ``resume_chat``, an
``app_action`` between turns, a second ``chat`` on a warm chat) are driven
through the real handler with the loop guard armed — any store call that
still runs on the loop thread raises inside the handler and shows up as a
missing/changed frame. The golden-master suites pin the frame sequences;
this module pins WHERE the DB work happens.
"""

from storage.pg import run_db
from tests.fixtures.ws_dashboard_harness import (
    ANY,
    TEST_MODEL,
    FakeExecutionLayer,
    dashboard_connection,
    drain_startup,
    make_test_agent,
    run_ws_scenario,
    session_cookie,
    set_username,
    stub_dashboard_seams,
    sync_dispatch,
    warm_new_chat,
)


class TestBetweenTurnsFrames:
    def test_read_mode_and_model_frames_run_off_loop(self, temp_db, monkeypatch, loop_db_guard):
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, _sid = await warm_new_chat(ws, layer, slug)

                with loop_db_guard.active():
                    ws.client_send({"type": "chat_read", "chat_id": chat_id})
                    await ws.expect({"type": "chat_read", "chat_id": chat_id})

                    ws.client_send({"type": "mode_change", "mode": "acceptEdits",
                                    "chat_id": chat_id})
                    await ws.expect({"type": "mode_changed", "mode": "acceptEdits"})

                    ws.client_send({"type": "model_change", "model": TEST_MODEL,
                                    "chat_id": chat_id})
                    await ws.expect({"type": "model_changed", "model": TEST_MODEL})

                    # A model foreign to the chat's layer is refused and the
                    # selector re-synced — the read → validate → (no) write
                    # ran as one lane job.
                    ws.client_send({"type": "model_change", "model": "not-a-real-model",
                                    "chat_id": chat_id})
                    await ws.expect({"type": "model_changed", "model": TEST_MODEL,
                                     "chat_id": chat_id})
                    await sync_dispatch(ws)

                row = temp_db.get_chat(chat_id)
                assert row["permission_mode"] == "acceptEdits"
                assert row["model"] == TEST_MODEL
                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)

    def test_resume_chat_reads_history_off_loop(self, temp_db, monkeypatch, loop_db_guard):
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, _sid = await warm_new_chat(ws, layer, slug)
                temp_db.add_chat_message(chat_id, "user", "Say hello")
                with loop_db_guard.active():
                    ws.client_send({"type": "resume_chat", "chat_id": chat_id})
                    hist = await ws.expect({
                        "type": "chat_history", "chat_id": chat_id, "agent": slug,
                        "messages": ANY, "has_more": False, "restore": ANY,
                        "plans": [], "total_cost": 0, "context_used": 0,
                        "context_max": 0, "cache_read": 0, "cache_write": 0,
                        "output_tokens": 0, "execution_path": "claude-code-cli",
                        "execution_mode": ANY, "model": ANY, "mode": ANY,
                        "process_alive": ANY,
                    })
                assert [m["content"] for m in hist["messages"]] == ["Say hello"]
                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)


class TestSendPath:
    def test_first_prompt_title_and_second_send_run_off_loop(self, temp_db, monkeypatch, loop_db_guard):
        from core.events.common_events import CommonEvent, TEXT, DONE
        layer = FakeExecutionLayer()
        layer.turn_events = [
            CommonEvent(type=TEXT, data={"content": "Hello there!"}),
            CommonEvent(type=DONE, data={}),
        ]
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, _sid = await warm_new_chat(ws, layer, slug)

                async def turn_frames(text: str, *, titled: bool) -> None:
                    ws.client_send({"type": "chat", "text": text, "chat_id": chat_id})
                    if titled:
                        await ws.expect({"type": "title_updated", "chat_id": chat_id,
                                         "title": text})
                    await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                     "status": "streaming"})
                    await ws.expect({
                        "type": "live_state", "chat_id": chat_id,
                        "streaming": True, "session_id": ANY, "started_at": ANY,
                        "live_blocks": [], "active_tools": [], "active_agents": [],
                        "active_delegates": [], "active_commands": [],
                        "pending_permission": None, "thinking_active": False,
                        "thinking_text": "", "thinking_tokens": 0, "todos": [],
                        "goal": None, "meeting_agent": None,
                        "meeting_participants": [], "workflows": {},
                    })
                    await ws.expect({"type": "text", "content": "Hello there!",
                                     "chat_id": chat_id})
                    await ws.expect({"type": "done", "chat_id": chat_id})
                    await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                     "status": "ready"})
                    await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                     "title": "WS Dash Test finished",
                                     "body": "Response ready"})

                with loop_db_guard.active():
                    # First send: user row + conditional title, one lane job.
                    await turn_frames("Say hello", titled=True)
                    # done ⇒ persisted: rows readable with no barrier.
                    rows = [(m["role"], m["content"])
                            for m in await run_db(temp_db.get_chat_messages, chat_id)]
                    assert rows == [("user", "Say hello"), ("assistant", "Hello there!")]
                    # Second send on the warm chat: the title stays, the
                    # prev-author read + row ride the same lane job.
                    await turn_frames("And again", titled=False)
                rows = [(m["role"], m["content"]) for m in temp_db.get_chat_messages(chat_id)]
                assert rows == [("user", "Say hello"), ("assistant", "Hello there!"),
                                ("user", "And again"), ("assistant", "Hello there!")]
                assert temp_db.get_chat(chat_id)["title"] == "Say hello"
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)


def _task_chat_with_sibling(temp_db, slug: str) -> tuple[str, str]:
    """A task chat whose session also ran a later turn in its own chat (a
    continue_session task): the resume aggregates the sibling's rows."""
    import uuid
    first, second = (f"run-{uuid.uuid4().hex[:10]}" for _ in range(2))
    sid = str(uuid.uuid4())
    for rid in (first, second):
        temp_db.create_run(rid, "dyn-x", slug, "schedule", None, "go",
                           "recurring", "agent", "user-admin")
        temp_db.create_chat(f"task-{rid}", "user-admin", slug, source_type="task")
        temp_db.update_run(rid, session_id=sid, chat_id=f"task-{rid}",
                           started_at=f"2026-09-27T10:0{0 if rid == first else 1}:00+00:00")
    temp_db.add_chat_message(f"task-{first}", "user", "first turn")
    temp_db.add_chat_message(f"task-{second}", "user", "second turn")
    return f"task-{first}", f"task-{second}"


class TestTaskChatReads:
    def test_task_pump_poll_runs_off_loop(self, temp_db, monkeypatch, loop_db_guard):
        """The idle poll of a task chat looks up a related turn's pump
        without reading the database on the loop."""
        import asyncio
        from ws.dashboard import DashboardConnection
        slug = make_test_agent()
        cid, _ = _task_chat_with_sibling(temp_db, slug)
        conn = DashboardConnection.__new__(DashboardConnection)
        conn.chat_id = cid
        conn.streaming = False

        async def scenario():
            with loop_db_guard.active():
                return await conn._task_pump_poll()

        assert asyncio.run(scenario()) is False

    def test_resume_of_a_task_chat_reads_related_runs_off_loop(
            self, temp_db, monkeypatch, loop_db_guard):
        """The related-run reads of a task chat's resume run off
        the loop, and the sibling turn's rows still ride the history."""
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        cid, _ = _task_chat_with_sibling(temp_db, slug)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                with loop_db_guard.active():
                    ws.client_send({"type": "resume_chat", "chat_id": cid})
                    while True:
                        frame = await ws.next_frame()
                        if frame["type"] == "chat_history":
                            break
                assert [m["content"] for m in frame["messages"]] == [
                    "first turn", "second turn"]
                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)

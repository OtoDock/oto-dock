"""The per-chat interactive toggle's persist frame (``execution_mode_change``)
names its chat — a frame without ``chat_id`` persists nothing.

Found on T1 (engine-contract phase 4, 2026-09-20): the dashboard's new-chat
view sent the frame with no ``chat_id`` (a brand-new chat has no row; its
mode rides the first warmup) and the handler fell back to the connection's
bound ``chat_id`` — the LAST chat opened on that socket — so flipping the
toggle on a new chat rewrote the previous chat's ``chats.execution_mode``.
The live switch (``execution_mode_switch``) shares the rule.

Run: cd proxy && python -m pytest tests/session/test_ws_dashboard_execution_mode.py -v
"""

from tests.fixtures.ws_dashboard_harness import (
    FakeExecutionLayer,
    dashboard_connection,
    drain_startup,
    make_test_agent,
    run_ws_scenario,
    session_cookie,
    stub_dashboard_seams,
    sync_dispatch,
    warm_new_chat,
)


class TestExecutionModeChange:
    def test_a_frame_without_chat_id_touches_no_row(self, temp_db, monkeypatch):
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                # The warmup binds the connection to chat A — the situation
                # the new-chat view's toggle used to write into.
                chat_id, _sid = await warm_new_chat(ws, layer, slug)
                before = temp_db.get_chat(chat_id)["execution_mode"]

                ws.client_send({"type": "execution_mode_change",
                                "execution_mode": "interactive"})
                await ws.expect({"type": "execution_mode_changed",
                                 "execution_mode": "interactive"})
                await sync_dispatch(ws)
                assert temp_db.get_chat(chat_id)["execution_mode"] == before

                # The live switch with no chat falls through to the same rule.
                ws.client_send({"type": "execution_mode_switch",
                                "execution_mode": "interactive"})
                await ws.expect({"type": "execution_mode_changed",
                                 "execution_mode": "interactive"})
                await sync_dispatch(ws)
                assert temp_db.get_chat(chat_id)["execution_mode"] == before
        run_ws_scenario(scenario)

    def test_the_row_write_runs_off_the_loop_before_the_ack(self, temp_db, monkeypatch):
        import asyncio
        from storage import database as task_store
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        real = task_store.update_chat

        def guarded(*a, **kw):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return real(*a, **kw)
            raise AssertionError("update_chat ran on the event loop")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, _sid = await warm_new_chat(ws, layer, slug)
                # Armed after the warmup: the guard is about this frame's write.
                monkeypatch.setattr(task_store, "update_chat", guarded)
                ws.client_send({"type": "execution_mode_change",
                                "execution_mode": "-p", "chat_id": chat_id})
                await ws.expect({"type": "execution_mode_changed",
                                 "execution_mode": "-p", "chat_id": chat_id})
                # Awaited before the ack: the row already holds the mode.
                assert temp_db.get_chat(chat_id)["execution_mode"] == "-p"
        run_ws_scenario(scenario)

    def test_a_frame_naming_its_chat_is_persisted(self, temp_db, monkeypatch):
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, _sid = await warm_new_chat(ws, layer, slug)

                ws.client_send({"type": "execution_mode_change",
                                "execution_mode": "-p", "chat_id": chat_id})
                # The ack names its chat: the dashboard applies it to that chat only.
                await ws.expect({"type": "execution_mode_changed",
                                 "execution_mode": "-p", "chat_id": chat_id})
                await sync_dispatch(ws)
                assert temp_db.get_chat(chat_id)["execution_mode"] == "-p"

                ws.client_send({"type": "execution_mode_change",
                                "execution_mode": "interactive", "chat_id": chat_id})
                await ws.expect({"type": "execution_mode_changed",
                                 "execution_mode": "interactive", "chat_id": chat_id})
                await sync_dispatch(ws)
                assert temp_db.get_chat(chat_id)["execution_mode"] == "interactive"

                ws.client_send({"type": "execution_mode_change",
                                "execution_mode": "tui", "chat_id": chat_id})
                await ws.expect({"type": "error",
                                 "message": "Invalid execution_mode: tui"})
        run_ws_scenario(scenario)

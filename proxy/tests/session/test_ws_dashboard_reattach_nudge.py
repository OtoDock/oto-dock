"""Characterization suite for ``ws_dashboard_handler`` — part 4.

Pins the mid-turn re-attach (resume_chat while the chat streams → truncated
history + live_state replay), the bg-nudge server-driven review turn, and
the inline warmup reuse of an alive session. Completes the Step-1 minimum
flow list; same golden-master rules as parts 1-3.
"""

import asyncio
import uuid

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
    warm_new_chat,
)


class TestMidTurnReattach:
    def test_resume_chat_during_stream_reattaches(self, temp_db, monkeypatch):
        from core.events.common_events import DONE, TEXT, CommonEvent

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            hold = asyncio.Event()

            async def slow_turn():
                yield CommonEvent(type=TEXT, data={"content": "working…"})
                await hold.wait()
                yield CommonEvent(type=DONE, data={})

            layer.turn_events = lambda sid, prompt: slow_turn()

            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                ws.client_send({"type": "chat", "text": "long job",
                                "chat_id": chat_id})
                await ws.expect({"type": "title_updated", "chat_id": chat_id,
                                 "title": "long job"})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "streaming"})
                await ws.expect({
                    "type": "live_state", "chat_id": chat_id,
                    "streaming": True, "session_id": sid, "started_at": ANY,
                    "live_blocks": [], "active_tools": [],
                    "active_agents": [], "active_delegates": [],
                    "active_commands": [], "pending_permission": None,
                    "thinking_active": False, "thinking_text": "",
                    "thinking_tokens": 0, "todos": [], "goal": None, "meeting_agent": None,
                    "meeting_participants": [], "workflows": {},
                })
                await ws.expect({"type": "text", "content": "working…",
                                 "chat_id": chat_id})

                # reload mid-turn: history is TRUNCATED at the pump cutoff
                # (the in-flight tail rides live_state), then re-attach.
                ws.client_send({"type": "resume_chat", "chat_id": chat_id})
                history = await ws.expect({
                    "type": "chat_history", "chat_id": chat_id, "agent": ANY,
                    "messages": ANY, "has_more": False,
                    "restore": {"todos": [], "meeting": None, "goal": None},
                    "plans": [], "total_cost": 0, "context_used": 0,
                    "context_max": 0, "cache_read": 0, "cache_write": 0,
                    "output_tokens": 0,
                    "execution_path": "claude-code-cli",
                    "execution_mode": "", "model": TEST_MODEL, "mode": "autoReview",
                    "process_alive": True,  # active pump mid-turn
                    "live_pending": True,   # its live_state follows: one paint
                })
                assert [(m["role"], m["content"])
                        for m in history["messages"]] == [
                    ("user", "long job"),
                ]
                await ws.expect({
                    "type": "warmup_ready", "session_id": sid,
                    "chat_id": chat_id, "mode": "autoReview",
                    "model": TEST_MODEL,
                    "execution_path": "claude-code-cli",
                })
                await ws.expect({"type": "queue_snapshot",
                                 "chat_id": chat_id, "messages": []})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "streaming"})
                live = await ws.expect({
                    "type": "live_state", "chat_id": chat_id,
                    "streaming": True, "session_id": sid, "started_at": ANY,
                    "live_blocks": ANY, "active_tools": [],
                    "active_agents": [], "active_delegates": [],
                    "active_commands": [], "pending_permission": None,
                    "thinking_active": False, "thinking_text": "",
                    "thinking_tokens": 0, "todos": [], "goal": None, "meeting_agent": None,
                    "meeting_participants": [], "workflows": {},
                })
                assert live["live_blocks"] == [
                    {"type": "text", "content": "working…"},
                ]

                hold.set()
                await ws.expect({"type": "done", "chat_id": chat_id})
                # The queued turn-start "streaming" broadcast is dropped as stale at
                # drain time (the turn already ended) — only the "ready" flows.
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": "WS Dash Test finished",  # display name (harness agent), not slug
                                 "body": "Response ready"})
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)


class TestBgNudgeServerTurn:
    def test_bg_nudge_runs_review_turn_on_viewed_chat(self, temp_db,
                                                      monkeypatch):
        from core.events.common_events import DONE, TEXT, CommonEvent
        from core.session.session_state import _dashboard_notify_queues

        layer = FakeExecutionLayer()
        layer.turn_events = [
            CommonEvent(type=TEXT, data={"content": "reviewed"}),
            CommonEvent(type=DONE, data={}),
        ]
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        nudge_text = ("Your 2 background agent(s) have completed. "
                      "Please review the results and continue.")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                # the bg monitor's transport: the session's registered
                # dashboard notify queue
                _dashboard_notify_queues[sid].put_nowait({
                    "type": "bg_nudge", "count": 2,
                    "chat_id": chat_id, "session_id": sid,
                })

                await ws.expect({"type": "bg_agents_complete", "count": 2})
                await ws.expect({"type": "server_turn_start",
                                 "chat_id": chat_id, "cause": "bg_review"})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "streaming"})
                await ws.expect({
                    "type": "live_state", "chat_id": chat_id,
                    "streaming": True, "session_id": sid, "started_at": ANY,
                    "live_blocks": [], "active_tools": [],
                    "active_agents": [], "active_delegates": [],
                    "active_commands": [], "pending_permission": None,
                    "thinking_active": False, "thinking_text": "",
                    "thinking_tokens": 0, "todos": [], "goal": None, "meeting_agent": None,
                    "meeting_participants": [], "workflows": {},
                })
                await ws.expect({"type": "text", "content": "reviewed",
                                 "chat_id": chat_id})
                await ws.expect({"type": "done", "chat_id": chat_id})
                # The queued turn-start "streaming" broadcast is dropped as stale at
                # drain time (the turn already ended) — only the "ready" flows.
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                # NO turn_complete: this chat never recorded a turn origin
                # (no user send preceded the nudge), so the ephemeral ping
                # has no routing target.

                assert [p for _s, p, _k in layer.messages] == [nudge_text]
                msgs = temp_db.get_chat_messages(chat_id)
                assert [(m["role"], m["event_type"] or m["content"])
                        for m in msgs] == [
                    ("event", "bg_nudge"),
                    ("assistant", "reviewed"),
                ]
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)

    def test_bg_nudge_with_labels_names_the_agents(self, temp_db,
                                                   monkeypatch):
        """A labels-carrying payload (monitors ≥ this round) names WHICH
        agents finished in the composed review prompt and persists the
        labels in the event row."""
        import json as _json

        from core.events.common_events import DONE, TEXT, CommonEvent
        from core.session.session_state import _dashboard_notify_queues

        layer = FakeExecutionLayer()
        layer.turn_events = [
            CommonEvent(type=TEXT, data={"content": "reviewed"}),
            CommonEvent(type=DONE, data={}),
        ]
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        labels = ['"probe auth flow" [a7b954]', '"scan fs" [b1c2d3]']
        nudge_text = (
            'Your 2 background agent(s) have completed: '
            '"probe auth flow" [a7b954]; "scan fs" [b1c2d3]. '
            "Please review the results and continue."
        )

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                _dashboard_notify_queues[sid].put_nowait({
                    "type": "bg_nudge", "count": 2,
                    "chat_id": chat_id, "session_id": sid,
                    "labels": labels,
                })

                await ws.expect({"type": "bg_agents_complete", "count": 2})
                # Drain the rest of the turn without pinning every frame —
                # the un-labeled twin above covers the full sequence.
                while True:
                    frame = await ws.next_frame()
                    if frame.get("type") == "done":
                        break
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})

                assert [p for _s, p, _k in layer.messages] == [nudge_text]
                msgs = temp_db.get_chat_messages(chat_id)
                ev = next(m for m in msgs
                          if (m["event_type"] or "") == "bg_nudge")
                assert _json.loads(ev["event_data"])["labels"] == labels
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)


class TestExistingChatWarmupInline:
    def test_alive_session_warmup_reuses_inline(self, temp_db, monkeypatch):
        from core.session.session_state import set_session_mode
        from storage import database as task_store

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        sid = str(uuid.uuid4())
        layer.alive.add(sid)
        set_session_mode(sid, "default")
        cid = str(uuid.uuid4())
        task_store.create_chat(cid, "user-admin", slug, "default",
                               model=TEST_MODEL,
                               execution_path="claude-code-cli")
        task_store.update_chat(cid, session_id=sid)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "warmup", "agent": slug,
                                "chat_id": cid})
                # inline reuse: warmup_ready BEFORE the dispatch's count frame
                await ws.expect({
                    "type": "warmup_ready", "session_id": sid,
                    "chat_id": cid, "mode": "ask", "model": TEST_MODEL,
                    "execution_path": "claude-code-cli",
                    "interactive": False,
                })
                await ws.expect({"type": "notification_count", "count": 0})
                assert layer.started == []
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)


class TestMidTurnReattachKeepsSteeredMessage:
    def test_steered_user_row_survives_mid_turn_resume(self, temp_db,
                                                       monkeypatch):
        # Regression (2026-07-19): a STEERED message persists mid-turn with an
        # id ABOVE the pump's cutoff; the resume's id-based truncation withheld
        # it and the pump replay never re-sends user rows — so switching away
        # and back mid-turn made the just-steered message vanish. USER rows are
        # now exempt from the cutoff filter.
        from core.events.common_events import DONE, TEXT, CommonEvent
        from storage import database as task_store

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            hold = asyncio.Event()

            async def slow_turn():
                yield CommonEvent(type=TEXT, data={"content": "working…"})
                await hold.wait()
                yield CommonEvent(type=DONE, data={})

            layer.turn_events = lambda sid, prompt: slow_turn()

            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                ws.client_send({"type": "chat", "text": "long job",
                                "chat_id": chat_id})
                await ws.expect({"type": "title_updated", "chat_id": chat_id,
                                 "title": "long job"})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "streaming"})
                await ws.expect({
                    "type": "live_state", "chat_id": chat_id,
                    "streaming": True, "session_id": sid, "started_at": ANY,
                    "live_blocks": [], "active_tools": [],
                    "active_agents": [], "active_delegates": [],
                    "active_commands": [], "pending_permission": None,
                    "thinking_active": False, "thinking_text": "",
                    "thinking_tokens": 0, "todos": [], "goal": None,
                    "meeting_agent": None,
                    "meeting_participants": [], "workflows": {},
                })
                await ws.expect({"type": "text", "content": "working…",
                                 "chat_id": chat_id})

                # The steer branch persists the accepted message IMMEDIATELY
                # (dashboard_chat: steered → add_chat_message) — mid-turn, so
                # its id lands ABOVE the pump cutoff.
                task_store.add_chat_message(
                    chat_id, "user", "steer: go deeper",
                    author_sub="user-admin",
                )

                ws.client_send({"type": "resume_chat", "chat_id": chat_id})
                history = await ws.expect({
                    "type": "chat_history", "chat_id": chat_id, "agent": ANY,
                    "messages": ANY, "has_more": False,
                    "restore": {"todos": [], "meeting": None, "goal": None},
                    "plans": [], "total_cost": 0, "context_used": 0,
                    "context_max": 0, "cache_read": 0, "cache_write": 0,
                    "output_tokens": 0,
                    "execution_path": "claude-code-cli",
                    "execution_mode": "", "model": TEST_MODEL,
                    "mode": "autoReview",
                    "process_alive": True,  # active pump mid-turn
                    "live_pending": True,   # its live_state follows: one paint
                })
                # BOTH user rows survive the cutoff; the in-flight assistant
                # tail stays withheld (it rides live_state).
                assert [(m["role"], m["content"])
                        for m in history["messages"]] == [
                    ("user", "long job"),
                    ("user", "steer: go deeper"),
                ]
                await ws.expect({
                    "type": "warmup_ready", "session_id": sid,
                    "chat_id": chat_id, "mode": "autoReview",
                    "model": TEST_MODEL,
                    "execution_path": "claude-code-cli",
                })
                await ws.expect({"type": "queue_snapshot",
                                 "chat_id": chat_id, "messages": []})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "streaming"})
                await ws.expect({
                    "type": "live_state", "chat_id": chat_id,
                    "streaming": True, "session_id": sid, "started_at": ANY,
                    "live_blocks": ANY, "active_tools": [],
                    "active_agents": [], "active_delegates": [],
                    "active_commands": [], "pending_permission": None,
                    "thinking_active": False, "thinking_text": "",
                    "thinking_tokens": 0, "todos": [], "goal": None,
                    "meeting_agent": None,
                    "meeting_participants": [], "workflows": {},
                })

                hold.set()
                await ws.expect({"type": "done", "chat_id": chat_id})
                # The queued turn-start "streaming" broadcast is dropped as stale at
                # drain time (the turn already ended) — only the "ready" flows.
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": "WS Dash Test finished",  # display name (harness agent), not slug
                                 "body": "Response ready"})
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)


async def _history_of(ws, cid: str) -> dict:
    for _ in range(20):
        frame = await ws.next_frame()
        if frame.get("type") == "chat_history" and frame.get("chat_id") == cid:
            return frame
    raise AssertionError(f"no chat_history for {cid}")


class TestResumeAsTheTurnEnds:
    def test_a_turn_that_ends_during_the_history_read_is_in_the_frame(
            self, temp_db, monkeypatch):
        """Opening a chat as its turn ends: the history read is queued on the
        chat's lane while the turn still runs, the engine's last save queues
        behind it, and the pump reads done before the frame is cut. The frame
        then has no cut and no live state follows (the attach is skipped for
        a done pump, and no done reaches this socket), so it must carry the
        reply itself."""
        from core.events.common_events import DONE, TEXT, CommonEvent
        from core.events.stream_pump import _active_pumps
        from storage import database as task_store
        from ws import dashboard_chat_resume as resume

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        other = str(uuid.uuid4())
        task_store.create_chat(other, "user-admin", slug, "autoReview",
                               model=TEST_MODEL, execution_path="claude-code-cli")

        async def scenario():
            hold = asyncio.Event()

            async def slow_turn():
                yield CommonEvent(type=TEXT, data={"content": "working… "})
                await hold.wait()
                yield CommonEvent(type=TEXT, data={"content": "the reply"})
                yield CommonEvent(type=DONE, data={})

            layer.turn_events = lambda sid, prompt: slow_turn()
            watched: list[str] = []
            original = resume.ChatResumeMixin._resume_read

            async def read_the_turn_end_overtakes(self, cid, *args, **kwargs):
                read = asyncio.ensure_future(original(self, cid, *args, **kwargs))
                if cid not in watched or hold.is_set():
                    return await read
                # The read job is on the chat's lane before the turn ends.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                hold.set()
                for _ in range(300):
                    pump = _active_pumps.get(cid)
                    if pump is None or pump.is_done:
                        break
                    await asyncio.sleep(0.01)
                return await read

            monkeypatch.setattr(resume.ChatResumeMixin, "_resume_read",
                                read_the_turn_end_overtakes)

            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)
                ws.client_send({"type": "chat", "text": "long job", "chat_id": chat_id})
                await ws.expect({"type": "title_updated", "chat_id": chat_id,
                                 "title": "long job"})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "streaming"})
                await ws.next_frame()  # the turn's live_state
                await ws.expect({"type": "text", "content": "working… ",
                                 "chat_id": chat_id})

                # Another chat on show: the socket leaves the turn.
                ws.client_send({"type": "resume_chat", "chat_id": other})
                await _history_of(ws, other)

                # Back to the chat just as its turn ends.
                watched.append(chat_id)
                ws.client_send({"type": "resume_chat", "chat_id": chat_id})
                history = await _history_of(ws, chat_id)
                assert hold.is_set()
                assert not history.get("live_pending")
                assert [(m["role"], m["content"]) for m in history["messages"]] == [
                    ("user", "long job"),
                    ("assistant", "working… the reply"),
                ]
                # Nothing after it brings the turn to this socket: the page
                # shows what this frame carries until it opens the chat again.
                rest: list[dict] = []
                while True:
                    try:
                        rest.append(await ws.next_frame(timeout=0.5))
                    except TimeoutError:
                        break
                assert not [f for f in rest if f.get("chat_id") == chat_id and f.get("type") in (
                    "done", "live_state", "chat_history", "chat_history_delta", "text")]
                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)

    def test_a_pump_past_its_turn_end_lands_its_rest_before_the_read(
            self, temp_db, monkeypatch):
        """A pump that already reads done is neither held nor attached, and
        its tail can still queue the turn's rest (an error turn's text): the
        resume waits for the pump's task before it reads."""
        import functools

        from core.events import chat_writer
        from core.events.stream_pump import _active_pumps
        from storage import database as task_store

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        cid = str(uuid.uuid4())
        task_store.create_chat(cid, "user-admin", slug, "autoReview",
                               model=TEST_MODEL, execution_path="claude-code-cli")
        task_store.add_chat_message(cid, "user", "long job", author_sub="user-admin")

        class _EndingPump:
            is_done = True
            source_type = "dashboard"
            chat_id = cid
            session_id = "s-ending"
            _task: "asyncio.Future | None" = None

        async def scenario():
            pump = _EndingPump()
            release = asyncio.Event()

            async def tail():
                await release.wait()
                await chat_writer.submit(cid, functools.partial(
                    task_store.add_chat_message, cid, "assistant", "the rest"), label="tail")
                _active_pumps.pop(cid, None)

            pump._task = asyncio.ensure_future(tail())
            _active_pumps[cid] = pump
            asyncio.get_running_loop().call_later(0.2, release.set)
            try:
                async with dashboard_connection(session_cookie()) as ws:
                    await drain_startup(ws)
                    ws.client_send({"type": "resume_chat", "chat_id": cid})
                    history = await _history_of(ws, cid)
                    assert [(m["role"], m["content"]) for m in history["messages"]] == [
                        ("user", "long job"),
                        ("assistant", "the rest"),
                    ]
                    ws.client_send({"type": "close"})
            finally:
                _active_pumps.pop(cid, None)
                await asyncio.wait([pump._task], timeout=1.0)
        run_ws_scenario(scenario)

    def test_a_live_pump_not_yet_held_that_ends_during_the_read_is_in_the_frame(
            self, temp_db, monkeypatch):
        """A pump whose start job had not landed at the snapshot (no cutoff)
        is live but not held, a turn that fails at once while the page opens
        the chat: its error card, saved behind the read as the pump ends,
        is in the frame."""
        import functools

        from core.events import chat_writer
        from core.events.stream_pump import _active_pumps
        from storage import database as task_store
        from ws import dashboard_chat_resume as resume

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        cid = str(uuid.uuid4())
        task_store.create_chat(cid, "user-admin", slug, "autoReview",
                               model=TEST_MODEL, execution_path="claude-code-cli")
        task_store.add_chat_message(cid, "user", "long job", author_sub="user-admin")

        class _StartingPump:
            is_done = False
            source_type = "dashboard"
            chat_id = cid
            session_id = "s-starting"
            _db_msg_cutoff_id = None
            accepts_in_flight = 0
            _task: "asyncio.Future | None" = None

        async def scenario():
            pump = _StartingPump()
            pump.producer = asyncio.get_running_loop().create_future()  # still running: no reap
            original = resume.ChatResumeMixin._resume_read
            reads: list[int] = []

            async def tail():
                await chat_writer.submit(cid, functools.partial(
                    task_store.add_chat_message, cid, "event", "the error card"), label="tail")
                _active_pumps.pop(cid, None)

            async def read_the_pump_ends_during(self, rcid, *args, **kwargs):
                reads.append(1)
                read = asyncio.ensure_future(original(self, rcid, *args, **kwargs))
                if len(reads) == 1:
                    # The read job is on the chat's lane, then the pump ends.
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    pump.is_done = True
                    pump._task = asyncio.ensure_future(tail())
                return await read

            monkeypatch.setattr(resume.ChatResumeMixin, "_resume_read", read_the_pump_ends_during)
            _active_pumps[cid] = pump
            try:
                async with dashboard_connection(session_cookie()) as ws:
                    await drain_startup(ws)
                    ws.client_send({"type": "resume_chat", "chat_id": cid})
                    history = await _history_of(ws, cid)
                    assert [(m["role"], m["content"]) for m in history["messages"]] == [
                        ("user", "long job"),
                        ("event", "the error card"),
                    ]
                    assert len(reads) == 2
                    ws.client_send({"type": "close"})
            finally:
                _active_pumps.pop(cid, None)
                if pump._task is not None:
                    await asyncio.wait([pump._task], timeout=1.0)
        run_ws_scenario(scenario)

    def test_a_turn_end_that_never_lands_times_out_and_leaves_the_pump_running(
            self, temp_db, monkeypatch, caplog):
        """A pump that reads done but whose task never ends (a stuck tail):
        the wait gives up at its bound, the frame still goes out with what has
        landed, a warning names the wait, and the pump's task is never
        cancelled (its end still sends the chat's ready and the alert)."""
        import logging
        import time

        from core.events.stream_pump import _active_pumps
        from storage import database as task_store
        from ws import dashboard_chat_resume as resume

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        monkeypatch.setattr(resume, "_TURN_END_SETTLE_S", 0.1)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        cid = str(uuid.uuid4())
        task_store.create_chat(cid, "user-admin", slug, "autoReview",
                               model=TEST_MODEL, execution_path="claude-code-cli")
        task_store.add_chat_message(cid, "user", "long job", author_sub="user-admin")

        class _StuckPump:
            is_done = True
            source_type = "dashboard"
            chat_id = cid
            session_id = "s-stuck"
            _task: "asyncio.Future | None" = None

        async def scenario():
            pump = _StuckPump()
            never = asyncio.Event()
            pump._task = asyncio.ensure_future(never.wait())
            _active_pumps[cid] = pump
            try:
                async with dashboard_connection(session_cookie()) as ws:
                    await drain_startup(ws)
                    started = time.monotonic()
                    ws.client_send({"type": "resume_chat", "chat_id": cid})
                    history = await _history_of(ws, cid)
                    assert time.monotonic() - started < 2.0
                    assert [(m["role"], m["content"]) for m in history["messages"]] == [
                        ("user", "long job"),
                    ]
                    assert not pump._task.done() and not pump._task.cancelled()
                    ws.client_send({"type": "close"})
            finally:
                _active_pumps.pop(cid, None)
                never.set()
                await asyncio.wait([pump._task], timeout=1.0)

        with caplog.at_level(logging.WARNING, logger="otodock"):
            run_ws_scenario(scenario)
        assert any("still ending after" in r.getMessage() for r in caplog.records)

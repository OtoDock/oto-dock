"""End-to-end stop-and-send (R7.2) through the REAL dashboard WS handler.

Drives ``ws_dashboard_handler`` + the real pump/producer machinery with a
scripted layer: a typed ``chat`` frame lands mid-turn, the handler queues it
and fires the graceful-only interrupt, the "CLI" closes the turn (the fake's
``on_interrupt_for_queued`` releases the turn generator's gate), and the
producer's queue drain delivers the message as the next turn — note on the
ENGINE prompt only, DB user rows raw. The refusal path (no live turn /
unsupported engine) degrades to today's deliver-at-turn-end with no note.

This is the live-verification twin of the browser test: everything except
the real CLI binary (whose control_request{interrupt} behavior the Stop
button and phone barge-in exercise daily) runs exactly as deployed.
"""

import asyncio

import pytest  # noqa: F401  (temp_db / monkeypatch fixtures)

from core.events.common_events import CommonEvent, TEXT, DONE
from storage import database as task_store
from tests.fixtures.ws_dashboard_harness import (
    ANY,
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

import ws.dashboard  # noqa: F401  (resolves the dashboard↔dashboard_chat cycle)
from ws.dashboard_chat import _STOP_AND_SEND_NOTE


def _gated_turns(gate: asyncio.Event, calls: dict):
    """Turn 1 streams t1 then parks on ``gate`` (a long-running turn);
    later turns finish immediately."""

    def turn(sid, prompt):
        calls["n"] += 1
        first = calls["n"] == 1

        async def gen():
            yield CommonEvent(type=TEXT, data={"content": f"t{calls['n']}"})
            if first:
                await gate.wait()
            yield CommonEvent(type=DONE, data={})
        return gen()

    return turn


def _drain_prelude(ws, chat_id, sid, first_text):
    """The frames every turn-opening chat send produces before its text."""

    async def inner():
        await ws.expect({"type": "title_updated", "chat_id": chat_id,
                         "title": first_text})
        await ws.expect({"type": "chat_status", "chat_id": chat_id,
                         "status": "streaming"})
        await ws.expect({"type": "live_state", "chat_id": chat_id,
                         "streaming": True, "session_id": sid,
                         "started_at": ANY, "live_blocks": [],
                         "active_tools": [], "active_agents": [],
                         "active_delegates": [], "active_commands": [],
                         "pending_permission": None,
                         "thinking_active": False, "thinking_text": "",
                         "thinking_tokens": 0, "todos": [],
                         "goal": None, "meeting_agent": None,
                         "meeting_participants": [], "workflows": {}})
    return inner()


class TestStopAndSend:
    def test_typed_mid_turn_interrupts_and_delivers_with_note(
        self, temp_db, monkeypatch,
    ):
        gate = asyncio.Event()
        calls = {"n": 0}
        layer = FakeExecutionLayer()
        layer.turn_events = _gated_turns(gate, calls)
        # The "CLI" accepts the graceful interrupt and closes the turn.
        layer.interrupt_for_queued_accepts = True
        layer.on_interrupt_for_queued = lambda sid: gate.set()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                ws.client_send({"type": "chat", "text": "start",
                                "chat_id": chat_id})
                await _drain_prelude(ws, chat_id, sid, "start")
                await ws.expect({"type": "text", "content": "t1",
                                 "chat_id": chat_id})

                # Mid-turn typed message: queued + graceful interrupt fired
                # → the turn closes → the drain delivers it immediately.
                ws.client_send({"type": "chat", "text": "do this instead",
                                "chat_id": chat_id})
                await ws.expect({"type": "queued", "index": 0,
                                 "text": "do this instead",
                                 "chat_id": chat_id})
                await ws.expect({"type": "queue_sent",
                                 "text": "do this instead",
                                 "chat_id": chat_id})
                await ws.expect({"type": "text", "content": "t2",
                                 "chat_id": chat_id})
                await ws.expect({"type": "done", "chat_id": chat_id})

                # The interrupt targeted the streaming session, exactly once.
                assert layer.queued_interrupts == [sid]
                # Steer-first ordering intact: the attempt was made and
                # rejected (real CLI keeps the unsupported default), so the
                # message took the queue+interrupt path, never both.
                assert layer.steered == [(sid, "do this instead")]

                # Engine prompts: turn 1 raw; the drained turn carries the
                # interruption note ON THE PROMPT ONLY.
                assert len(layer.messages) == 2
                assert layer.messages[0][1] == "start"
                assert layer.messages[1][1] == (
                    _STOP_AND_SEND_NOTE + "\n\ndo this instead"
                )

                # DB user rows stay raw — the note never persists.
                msgs = task_store.get_chat_messages(chat_id)
                users = [m["content"] for m in msgs if m["role"] == "user"]
                assert users == ["start", "do this instead"]
                assert all(_STOP_AND_SEND_NOTE not in u for u in users)
        run_ws_scenario(scenario)

    def test_engine_refusal_degrades_to_deliver_at_turn_end(
        self, temp_db, monkeypatch,
    ):
        """No live turn / unsupported engine → interrupt refused: the message
        waits for the natural turn end and drains WITHOUT the note."""
        gate = asyncio.Event()
        calls = {"n": 0}
        layer = FakeExecutionLayer()
        layer.turn_events = _gated_turns(gate, calls)
        layer.interrupt_for_queued_accepts = False  # refusal (default engine)
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                ws.client_send({"type": "chat", "text": "start",
                                "chat_id": chat_id})
                await _drain_prelude(ws, chat_id, sid, "start")
                await ws.expect({"type": "text", "content": "t1",
                                 "chat_id": chat_id})

                ws.client_send({"type": "chat", "text": "later please",
                                "chat_id": chat_id})
                await ws.expect({"type": "queued", "index": 0,
                                 "text": "later please",
                                 "chat_id": chat_id})
                # Give the fire task time to run + be refused.
                await asyncio.sleep(0.05)
                assert layer.queued_interrupts == [sid]

                # The turn keeps running until ITS OWN end.
                gate.set()
                await ws.expect({"type": "queue_sent", "text": "later please",
                                 "chat_id": chat_id})
                await ws.expect({"type": "text", "content": "t2",
                                 "chat_id": chat_id})
                await ws.expect({"type": "done", "chat_id": chat_id})

                # No note on a turn that was never interrupted.
                assert layer.messages[1][1] == "later please"
        run_ws_scenario(scenario)

"""Remote Codex background sub-agents — the proxy-side router + supervisor.

The satellite is a dumb pipe: its persistent forwarder streams EVERY app-server
notification to the proxy as a session_event (including a background sub-agent's
events AFTER the main turn ends). All the demux + supervision lives on the proxy,
which consumes the WS-forwarded ``info.event_queue``.

Covered:
1. Router demuxes main-thread events (+ synthetic turn-control markers) to the
   active turn's default_consumer, and each sub-agent thread to its own buffer.
2. A background sub-agent active at main-turn end is registered + supervised, and
   the supervisor resolves it (registry mark_done) when its thread terminates —
   through the shared resolve_bg_subagent + SubagentRegistry, which the cohort
   nudge + the delegation wait read.
3. Supervision is always on: every satellite that can connect forwards
   bg-thread events past the main turn (that landed in 0.5.18, far below
   MIN_SATELLITE_VERSION), so the Codex remote adapter's router is the sole
   consumer of the session's event queue.

The router, the handoff and the teardown are the Codex engine's
(``core/layers/codex/remote.py`` — ``RemoteEngineAdapter``); the remote layer
reaches them through the adapter, never by engine id.
"""
import asyncio
import contextlib

import pytest

from core.layers.codex.events import CodexEvent
from core.layers.codex.layer import CodexEventTranslator
from core.layers.codex.remote import CodexRemoteState
from core.remote.remote_execution import RemoteExecutionLayer, RemoteSessionInfo
from core.remote.satellite_connection import (
    SatelliteConnection,
    SatelliteConnectionManager,
)
from core.session.session_manager import get_layer_by_path
from core.session.session_state import _subagent_registries, get_subagent_registry

MAIN = "thread-MAIN"
SUB = "thread-SUB-agent"


def _adapter():
    return get_layer_by_path("codex-cli").remote_adapter()


def _ev(method, tid, **item):
    """A forwarded codex notification {method, params} as it lands on event_queue."""
    params = {"threadId": tid}
    if item:
        params["item"] = item
    return {"method": method, "params": params}


def _make_layer_info(translator=None):
    cm = SatelliteConnectionManager()
    layer = RemoteExecutionLayer(cm)
    info = RemoteSessionInfo(
        session_id="s1", machine_id="m1", agent_name="a",
        execution_path="codex-cli", event_queue=asyncio.Queue(),
        resume_handle=MAIN,
    )
    # The engine's per-session record, as init_session would build it.
    info.engine_state = CodexRemoteState(
        translator=translator or CodexEventTranslator(model="m", supervised_bg=True),
    )
    layer._sessions["s1"] = info
    return layer, info


# ---------------------------------------------------------------------------
# 1. Router: demux by threadId
# ---------------------------------------------------------------------------

def test_router_demuxes_main_and_sub_threads():
    async def run():
        layer, info = _make_layer_info()
        info.engine_state.default_consumer = asyncio.Queue()
        router = asyncio.create_task(_adapter()._route_notifications(info))
        try:
            info.event_queue.put_nowait(_ev("item/started", MAIN, type="agentMessage"))
            info.event_queue.put_nowait(_ev("item/started", SUB, type="agentMessage"))
            # synthetic turn-control marker (no codex ``method``) → main turn
            info.event_queue.put_nowait({"type": "_turn_ended", "command_id": "c1"})
            await asyncio.sleep(0.05)
            # MAIN event + the marker went to the active turn's consumer.
            assert info.engine_state.default_consumer.qsize() == 2
            # The SUB event was siphoned into its own (lazily created) buffer.
            assert SUB in info.engine_state.thread_consumers
            assert info.engine_state.thread_consumers[SUB].qsize() == 1
        finally:
            router.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await router

    asyncio.run(run())


def test_router_captures_codex_thread_id_marker():
    # The thread-id marker is sent at session start (before any turn registers a
    # consumer), so the router must consume it to learn the demux key — not
    # forward it to a None consumer and drop it (which would disable demux).
    async def run():
        layer, info = _make_layer_info()
        info.resume_handle = ""         # fresh session — unknown until the marker
        info.engine_state.default_consumer = None    # no active turn yet
        router = asyncio.create_task(_adapter()._route_notifications(info))
        try:
            info.event_queue.put_nowait({"type": "_resume_handle", "handle": MAIN})
            await asyncio.sleep(0.05)
            assert info.resume_handle == MAIN
            # With the key learned, a sub-thread event now demuxes to its buffer.
            info.event_queue.put_nowait(_ev("item/started", SUB, type="agentMessage"))
            await asyncio.sleep(0.05)
            assert SUB in info.engine_state.thread_consumers
        finally:
            router.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await router

    asyncio.run(run())


def test_router_fans_out_session_ended_sentinel():
    async def run():
        layer, info = _make_layer_info()
        info.engine_state.default_consumer = asyncio.Queue()
        info.engine_state.thread_consumers[SUB] = asyncio.Queue()
        router = asyncio.create_task(_adapter()._route_notifications(info))
        info.event_queue.put_nowait(None)  # satellite session_ended sentinel
        await asyncio.wait_for(router, timeout=2)  # router returns after fan-out
        # Every waiter gets the sentinel so none hang on a terminal that never comes.
        default_sentinel = info.engine_state.default_consumer.get_nowait()
        assert default_sentinel is None
        thread_sentinel = info.engine_state.thread_consumers[SUB].get_nowait()
        assert thread_sentinel is None

    asyncio.run(run())


def test_a_sub_agents_end_or_error_never_ends_the_main_turn():
    """A sub-agent runs on its own thread and emits its own ``turn/completed``
    (and possibly a final ``error``) before the main agent's synthesis: the
    turn goes on to the main thread's own end, and nothing of the sub-agent's
    thread reaches the chat."""
    from core.events.common_events import ERROR, TEXT

    def _said(tid, text):
        return _ev("item/completed", tid, id=f"m-{text[:4]}", type="agentMessage", text=text)

    sequence = [
        _ev("turn/started", MAIN),
        _said(MAIN, "Spawning a sub-agent; waiting for its result."),
        _ev("item/completed", MAIN, id="c1", type="collabAgentToolCall",
            receiverThreadIds=[SUB], agentsStates={SUB: {"status": "pendingInit"}}),
        _ev("turn/started", SUB),
        _said(SUB, "pineapple"),
        {"method": "error", "params": {"threadId": SUB, "willRetry": False,
                                       "error": {"message": "sub boom"}}},
        {"method": "turn/completed", "params": {"threadId": SUB, "turn": {"status": "completed"}}},
        _ev("item/completed", MAIN, id="c1", type="collabAgentToolCall",
            agentsStates={SUB: {"status": "completed"}}),
        _said(MAIN, "The sub-agent returned: pineapple."),
        {"method": "turn/completed", "params": {"threadId": MAIN, "turn": {"status": "completed"}}},
        _said(MAIN, "LATE"),
        {"type": "_turn_ended", "command_id": ""},
    ]

    async def run():
        layer, info = _make_layer_info()
        router = asyncio.create_task(_adapter()._route_notifications(info))
        try:
            await _adapter().begin_turn(info, 0)
            for raw in sequence:
                info.event_queue.put_nowait(raw)
            return [ev async for ev in _adapter().stream_turn(info, layer._cm)]
        finally:
            router.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await router
            await _adapter().close_state(info)

    try:
        events = asyncio.run(run())
    finally:
        _subagent_registries.pop("s1", None)
        _bg_command_registries.pop("s1", None)
    text = "".join(e.data["content"] for e in events if e.type == TEXT)
    assert "The sub-agent returned: pineapple." in text
    assert "sub boom" not in text and "LATE" not in text
    assert text.count("pineapple") == 1
    assert not [e for e in events if e.type == ERROR] and events[-1].type == DONE


# ---------------------------------------------------------------------------
# 2. Handoff + supervisor + resolve
# ---------------------------------------------------------------------------

def test_remote_bg_subagent_registered_supervised_and_resolved():
    async def run():
        tr = CodexEventTranslator(model="gpt-5.4", supervised_bg=True)
        tr._main_thread_id = MAIN
        # The main thread spawned a bg sub (still running) — the translator tracks it.
        tr.translate(CodexEvent("item/completed", {"threadId": MAIN, "item": {
            "type": "collabAgentToolCall", "id": "c1", "prompt": "bg",
            "receiverThreadIds": [SUB], "agentsStates": {SUB: {"status": "running"}}}}))
        layer, info = _make_layer_info(tr)
        reg = get_subagent_registry("s1")
        reg.reset()
        try:
            # Main turn ended → hand the still-running bg sub to a supervisor.
            _adapter()._handoff_bg_subagents(info)
            assert SUB in reg.spawned
            sup = info.engine_state.bg_supervisors.get(SUB)
            assert sup is not None

            # Feed the sub's terminal to its buffer → the supervisor resolves it.
            info.engine_state.thread_consumers[SUB].put_nowait(
                {"method": "turn/completed", "params": {"threadId": SUB}}
            )
            await asyncio.wait_for(sup, timeout=5)
            assert SUB in reg.completed
            assert not reg.has_pending
            # The translator was tombstoned so a later collab snapshot can't reopen it.
            assert tr.subagent_end_event(SUB) == []
        finally:
            await _adapter().close_state(info)
            _subagent_registries.pop("s1", None)

    asyncio.run(run())


def test_remote_handoff_no_pending_arms_nothing():
    # Foreground-only turn (translator reports no pending bg sub) → no supervisor.
    tr = CodexEventTranslator(model="m", supervised_bg=True)
    tr._main_thread_id = MAIN
    layer, info = _make_layer_info(tr)
    reg = get_subagent_registry("s1")
    reg.reset()
    try:
        _adapter()._handoff_bg_subagents(info)
        assert info.engine_state.bg_supervisors == {}
        assert not reg.has_pending
        assert info.engine_state.default_consumer is None  # main-turn consumer cleared
    finally:
        _subagent_registries.pop("s1", None)


def test_remote_supervisor_resolves_on_session_ended():
    # A lost terminal: the session ends (None sentinel) while a bg sub buffer is
    # open → the supervisor still resolves (no hang).
    async def run():
        tr = CodexEventTranslator(model="m", supervised_bg=True)
        tr._main_thread_id = MAIN
        tr.translate(CodexEvent("item/completed", {"threadId": MAIN, "item": {
            "type": "collabAgentToolCall", "id": "c1", "prompt": "bg",
            "receiverThreadIds": [SUB], "agentsStates": {SUB: {"status": "running"}}}}))
        layer, info = _make_layer_info(tr)
        reg = get_subagent_registry("s1")
        reg.reset()
        try:
            _adapter()._handoff_bg_subagents(info)
            sup = info.engine_state.bg_supervisors[SUB]
            info.engine_state.thread_consumers[SUB].put_nowait(None)  # session ended
            await asyncio.wait_for(sup, timeout=5)
            assert SUB in reg.completed
        finally:
            await _adapter().close_state(info)
            _subagent_registries.pop("s1", None)

    asyncio.run(run())


def test_a_long_background_subagent_is_supervised_until_its_terminal():
    # A supervisor waits as long as the thread lives (no fixed bound may clear
    # a research agent's badge or run its review while it works); each event
    # stamps the agent alive for the chat monitor's silence rule.
    async def run():
        layer, info = _make_layer_info()
        reg = get_subagent_registry("s1")
        reg.reset()
        reg.register_spawn(SUB, SUB, label="bg task")
        reg.last_seen[SUB] = 0.0
        q: asyncio.Queue = asyncio.Queue()
        info.engine_state.thread_consumers[SUB] = q
        try:
            sup = asyncio.create_task(_adapter()._supervise_bg_subagent(info, SUB))
            for _ in range(3):
                q.put_nowait(_ev("item/completed", SUB))
            await asyncio.sleep(0.05)
            assert not sup.done() and reg.has_pending
            assert reg.last_seen[SUB] > 0.0
            q.put_nowait({"method": "turn/completed", "params": {"threadId": SUB}})
            await asyncio.wait_for(sup, timeout=5)
            assert SUB in reg.completed
        finally:
            await _adapter().close_state(info)
            _subagent_registries.pop("s1", None)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Plan mode: a completed plan-mode turn's `plan` item becomes a plan review,
# with no plan file on the machine.
# ---------------------------------------------------------------------------

from core.events.common_events import DONE, PLAN_MODE


def _drain_codex_turn(info, layer, raws):
    """Feed forwarded notifications to the turn's consumer (what the router
    does), run one engine turn stream, collect events."""
    async def run():
        info.engine_state.default_consumer = asyncio.Queue()
        for r in raws:
            info.engine_state.default_consumer.put_nowait(r)
        return [ev async for ev in _adapter().stream_turn(info, layer._cm)]
    return asyncio.run(run())


@pytest.fixture
def queued_reviews(monkeypatch):
    from core.layers.codex import remote as codex_remote
    queued: list = []

    async def _queue(session_id, plan):
        queued.append((session_id, plan))
    monkeypatch.setattr(codex_remote, "queue_plan_review", _queue)
    return queued


@pytest.mark.parametrize("items,plan", [
    ([("agentMessage", "Here is the plan."), ("plan", "1. Add --version")], "1. Add --version"),
    # A plan-mode turn answered in prose has no `plan` item: a plain turn,
    # never its last message drawn as a plan.
    ([("agentMessage", "- Add --version\n- Add a CLI test")], None),
])
def test_a_remote_plan_turn_queues_a_plan_review_only_for_a_plan_item(queued_reviews, items, plan):
    tr = CodexEventTranslator(model="gpt-5.5")
    layer, info = _make_layer_info(tr)
    info.mode = "plan"  # read-only plan mode
    events = _drain_codex_turn(info, layer, [
        *(_ev("item/completed", MAIN, type=kind, text=text) for kind, text in items),
        {"method": "turn/completed",
         "params": {"threadId": MAIN, "turn": {"status": "completed"}}},
        {"type": "_turn_ended", "command_id": ""},
    ])
    assert queued_reviews == ([(info.session_id, plan)] if plan else [])
    assert not [e for e in events if e.type == PLAN_MODE]


def test_remote_default_mode_queues_no_plan_review(queued_reviews):
    tr = CodexEventTranslator(model="gpt-5.5")
    layer, info = _make_layer_info(tr)
    info.mode = "ask"  # not a plan turn
    _drain_codex_turn(info, layer, [
        _ev("item/completed", MAIN, type="agentMessage", text="some answer"),
        {"type": "_turn_ended", "command_id": ""},
    ])
    assert queued_reviews == []


def test_a_plan_review_is_queued_under_the_chats_plan_name_and_answered_between_turns(monkeypatch):
    """The review goes on the session's queue as Claude's ExitPlanMode puts
    it, named after the chat's plan file (the same name across revisions) and
    marked as answered between turns."""
    from core.layers.codex.plan_review import queue_plan_review
    from core.session.session_state import get_permission_queue
    from storage import database as task_store
    monkeypatch.setattr(task_store, "get_chat_by_session", lambda sid: {"id": "abcdef12-0000"})

    async def run():
        await queue_plan_review("s-plan", "1. Add --version")
        await queue_plan_review("s-plan", "1. Add --version\n2. Add a test")
    asyncio.run(run())
    queue = get_permission_queue("s-plan")
    item = queue.get_nowait()
    assert item["event_type"] == "plan_review" and item["request_id"]
    assert item["plan"] == "1. Add --version"
    assert item["tool_input"] == {"plan": "1. Add --version", "planFilePath": "plan-abcdef12.md",
                                  "between_turns": True}
    revised = queue.get_nowait()
    assert revised["tool_input"]["planFilePath"] == "plan-abcdef12.md"
    assert revised["request_id"] != item["request_id"]


def test_a_turn_the_satellite_closed_before_completion_is_lost_and_resumes():
    """The satellite closed the turn before ``turn/completed`` (its daemon
    went): the ``lost`` ending, and the session reads dead so the next send
    re-warms it instead of meeting the gone process first."""
    from core.events import turn_ending
    from core.events.common_events import ERROR
    layer, info = _make_layer_info()
    events = _drain_codex_turn(info, layer, [
        _ev("item/completed", MAIN, type="agentMessage", text="half an answer"),
        {"type": "_turn_ended", "command_id": ""},
    ])
    err = next(e for e in events if e.type == ERROR)
    assert turn_ending.from_dict(err.data["ending"]).reason == turn_ending.LOST
    assert info.cli_dead


def test_a_turn_the_machine_ended_for_an_unanswered_interrupt_is_a_plain_done():
    """The daemon did not take the interrupt this side asked for (a person's
    Stop), so the machine dropped it and ended the turn. The turn was
    stopped: no ``lost`` card, and the session is not read dead, since the
    machine re-warms the daemon on the same thread at the next turn."""
    from core.events.common_events import ERROR
    layer, info = _make_layer_info()
    events = _drain_codex_turn(info, layer, [
        _ev("item/completed", MAIN, type="agentMessage", text="half an answer"),
        {"type": "_turn_ended", "command_id": "", "reason": "interrupted"},
    ])
    assert not [e for e in events if e.type == ERROR] and events[-1].type == DONE
    assert not info.cli_dead


def test_a_daemon_that_exits_in_a_turn_is_one_exited_ending():
    """The machine says the daemon exited inside the turn: one ending, typed
    ``exited`` with the exit code, where the stream used to carry the
    machine's own error and then a ``lost`` one."""
    from core.events import turn_ending
    from core.events.common_events import ERROR
    layer, info = _make_layer_info()
    events = _drain_codex_turn(info, layer, [
        _ev("item/completed", MAIN, type="agentMessage", text="half an answer"),
        {"type": "_turn_ended", "command_id": "", "reason": "exited", "exit_code": 101,
         "last_line": "thread 'main' panicked"},
    ])
    errors = [e for e in events if e.type == ERROR]
    assert len(errors) == 1 and events[-1].type == DONE
    ending = turn_ending.from_dict(errors[0].data["ending"])
    assert (ending.reason, ending.exit_code, ending.detail) == (
        turn_ending.EXITED, 101, "exit 101: thread 'main' panicked")
    assert info.cli_dead


def test_a_daemon_exit_after_the_platforms_own_kill_is_no_exit_ending():
    """The platform's own hard abort ended the daemon (a Stop, its
    watchdog): the machine's word that it exited is that kill, never an
    exit for the chat to read."""
    from core.events.common_events import ERROR
    layer, info = _make_layer_info()
    info.proxy_killed = True
    events = _drain_codex_turn(info, layer, [
        _ev("item/completed", MAIN, type="agentMessage", text="half an answer"),
        {"type": "_turn_ended", "command_id": "", "reason": "exited", "exit_code": -15},
    ])
    assert not [e for e in events if e.type == ERROR] and events[-1].type == DONE
    assert not info.cli_dead


def test_a_silent_remote_codex_turn_ends_typed_and_a_tool_item_keeps_it(monkeypatch):
    """No notification past the silence ceiling with nothing running: the
    satellite's codex turn is aborted and the turn ends ``silent`` (not
    graceful: the next turn gets the cancelled context). An open tool item
    keeps the turn past the ceiling, and a steer past it is refused."""
    import time

    import config
    from core.events import turn_ending
    from core.events.common_events import ERROR
    from core.layers.codex import remote as codex_remote
    monkeypatch.setattr(config, "TURN_SILENCE_S", 0.2)
    monkeypatch.setattr(codex_remote, "_SILENCE_SLICE_S", 0.1)
    layer, info = _make_layer_info()
    monkeypatch.setattr(layer._cm, "is_session_in_grace", lambda mid, sid: False)
    sent: list[dict] = []

    async def _send(mid, msg):
        sent.append(msg)
    monkeypatch.setattr(layer._cm, "send_fire_and_forget", _send)
    info.last_event_at = time.monotonic() - 1.0
    events = _drain_codex_turn(info, layer, [])
    assert [e.type for e in events][-2:] == [ERROR, DONE]
    ending = turn_ending.from_dict(events[-2].data["ending"])
    assert (ending.reason, ending.graceful) == (turn_ending.SILENT, False)
    assert sent == [{"type": "abort", "session_id": "s1"}]

    async def with_open_tool():
        info.engine_state.default_consumer = asyncio.Queue()
        info.engine_state.default_consumer.put_nowait(
            _ev("item/started", MAIN, id="call-1", type="mcpToolCall"))
        stream = _adapter().stream_turn(info, layer._cm)
        seen = [await stream.__anext__()]
        collect = asyncio.create_task(_collect(stream, seen))
        await asyncio.sleep(0.5)             # past the ceiling, the tool open
        assert not collect.done() and not [e for e in seen if e.type == ERROR]
        info.engine_state.default_consumer.put_nowait(
            {"method": "turn/completed",
             "params": {"threadId": MAIN, "turn": {"status": "completed"}}})
        info.engine_state.default_consumer.put_nowait({"type": "_turn_ended", "command_id": ""})
        await asyncio.wait_for(collect, 5.0)
        assert not [e for e in seen if e.type == ERROR] and seen[-1].type == DONE
        # A steer past the ceiling with nothing running is refused: the
        # caller queues the message for the turn that is about to end.
        info.turn_active = True
        info.engine_state.open_items.clear()
        info.last_event_at = time.monotonic() - 1.0
        assert await _adapter().steer(info, layer._cm, "x") is False
    asyncio.run(with_open_tool())


async def _collect(stream, into: list) -> None:
    async for ev in stream:
        into.append(ev)


def _short_silence(monkeypatch, layer) -> list[dict]:
    """The silence ceiling at 0.2 s, read every 0.1 s, on a machine that is
    not reconnecting. Returns the frames sent to the machine."""
    import config
    from core.layers.codex import remote as codex_remote
    monkeypatch.setattr(config, "TURN_SILENCE_S", 0.2)
    monkeypatch.setattr(config, "CLAUDE_TIMEOUT", 7200)
    monkeypatch.setattr(codex_remote, "_SILENCE_SLICE_S", 0.1)
    monkeypatch.setattr(layer._cm, "is_session_in_grace", lambda mid, sid: False)
    sent: list[dict] = []

    async def _send(mid, msg):
        sent.append(msg)
    monkeypatch.setattr(layer._cm, "send_fire_and_forget", _send)
    return sent


def test_an_open_message_item_does_not_keep_a_silent_remote_codex_turn(monkeypatch):
    """A stream that stalls in the middle of an answer is silence, not work:
    only a tool-like item takes the turn ceiling."""
    from core.events import turn_ending
    from core.events.common_events import ERROR
    layer, info = _make_layer_info()
    sent = _short_silence(monkeypatch, layer)
    events = _drain_codex_turn(info, layer, [
        _ev("item/started", MAIN, id="msg-1", type="agentMessage")])
    assert [e.type for e in events][-2:] == [ERROR, DONE]
    assert turn_ending.from_dict(events[-2].data["ending"]).reason == turn_ending.SILENT
    assert sent == [{"type": "abort", "session_id": "s1"}]


def test_a_task_runs_remote_codex_turn_keeps_its_silence_to_the_turn_ceiling(monkeypatch):
    """A task run's turn takes the turn ceiling with no tool open (a tool
    that never returns is the task watchdog's case)."""
    from core.events.common_events import ERROR
    layer, info = _make_layer_info()
    sent = _short_silence(monkeypatch, layer)
    info.task_turn = True

    async def run():
        info.engine_state.default_consumer = asyncio.Queue()
        seen: list = []
        collect = asyncio.create_task(_collect(_adapter().stream_turn(info, layer._cm), seen))
        await asyncio.sleep(0.6)             # past the silence ceiling, nothing open
        assert not collect.done()
        info.engine_state.default_consumer.put_nowait(
            {"method": "turn/completed",
             "params": {"threadId": MAIN, "turn": {"status": "completed"}}})
        info.engine_state.default_consumer.put_nowait({"type": "_turn_ended", "command_id": ""})
        await asyncio.wait_for(collect, 5.0)
        return seen
    seen = asyncio.run(run())
    assert not [e for e in seen if e.type == ERROR] and seen[-1].type == DONE
    assert sent == []


def test_a_codex_session_on_a_machine_counts_its_idle_seconds_from_its_last_event(monkeypatch):
    """The seconds since a session's last event, which the wedge check of a
    re-opened chat and the task watchdog read: every notification the router
    reads counts (a background sub-agent's included). A session in the
    reconnect grace is reconnecting, not idle, and has none; neither has a
    session the layer does not hold."""
    import time

    async def run():
        layer, info = _make_layer_info()
        info.last_activity = time.monotonic() - 42.0
        assert 41.0 < layer.session_idle_seconds("s1") < 50.0
        assert layer.session_idle_seconds("no-such") is None
        router = asyncio.create_task(_adapter()._route_notifications(info))
        try:
            info.event_queue.put_nowait(_ev("item/started", SUB, type="agentMessage"))
            for _ in range(50):
                await asyncio.sleep(0.01)
                if SUB in info.engine_state.thread_consumers:
                    break
            assert layer.session_idle_seconds("s1") < 5.0
        finally:
            router.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await router
        info.last_activity = time.monotonic() - 42.0
        monkeypatch.setattr(layer._cm, "is_session_in_grace",
                            lambda mid, sid: (mid, sid) == ("m1", "s1"))
        assert layer.session_idle_seconds("s1") is None

    asyncio.run(run())


def test_remote_interrupted_plan_turn_emits_no_card():
    tr = CodexEventTranslator(model="gpt-5.5")
    layer, info = _make_layer_info(tr)
    info.mode = "plan"
    events = _drain_codex_turn(info, layer, [
        _ev("item/completed", MAIN, type="agentMessage", text="- partial plan"),
        {"method": "turn/completed",
         "params": {"threadId": MAIN, "turn": {"status": "interrupted"}}},
        {"type": "_turn_ended", "command_id": ""},
    ])
    assert not [e for e in events if e.type == PLAN_MODE]


# ---------------------------------------------------------------------------
# Background terminals (unified_exec bg commands): live cross-turn completion.
# Same design as bg sub-agents — the satellite's persistent forwarder streams a
# terminal's late item/completed between turns; the router's OOB hook resolves
# it, _teardown_remote_bg sweeps survivors, and the drain's codex branch pulls
# the satellite's codex_bg_terminals RPC as the loss-window backstop.
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, MagicMock

from core.events.bg_command_state import (
    _bg_command_registries,
    get_bg_command_registry,
)


def _bg_cmd_tr(sid="s1"):
    tr = CodexEventTranslator(
        model="gpt-5.6-sol", supervised_bg=True,
        session_id=sid, supervised_bg_commands=True,
    )
    tr._main_thread_id = MAIN
    return tr


def _swept_bg_candidate(tr, *, pid=None):
    """One background terminal past its turn-end sweep (badge live, pending).
    ``pid`` optionally binds the unified_exec processId via terminalInteraction
    (the only place it reliably appears on this wire)."""
    tr.translate(CodexEvent("item/started", {"threadId": MAIN, "item": {
        "type": "commandExecution", "id": "i1", "command": "sleep 40",
        "processId": None}}))
    if pid:
        tr.translate(CodexEvent("item/commandExecution/terminalInteraction", {
            "threadId": MAIN, "itemId": "i1", "processId": pid, "stdin": ""}))
    tr.translate(CodexEvent("turn/completed",
                            {"threadId": MAIN, "turn": {"status": "completed"}}))


def test_router_oob_resolves_tracked_bg_command_completion():
    async def run():
        tr = _bg_cmd_tr()
        _swept_bg_candidate(tr)
        layer, info = _make_layer_info(tr)
        router = asyncio.create_task(_adapter()._route_notifications(info))
        try:
            assert get_bg_command_registry("s1").has_pending
            # Idle (no default_consumer): the exit's item/completed lands on
            # the router and must resolve via the OOB hook, not be dropped.
            info.event_queue.put_nowait(_ev(
                "item/completed", MAIN, type="commandExecution", id="i1",
                command="sleep 40", status="completed", exitCode=0,
                aggregatedOutput="done"))
            for _ in range(50):
                await asyncio.sleep(0.01)
                if not get_bg_command_registry("s1").has_pending:
                    break
            assert get_bg_command_registry("s1").completed == {"i1"}
            assert tr.pending_bg_commands() == []
            # The translated events were discarded, not routed anywhere.
            assert info.engine_state.thread_consumers == {}
            assert info.engine_state.default_consumer is None
            assert not router.done()  # a guarded apply never kills the router
        finally:
            router.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await router

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop("s1", None)


def test_router_oob_completion_clears_the_terminal_s_badge(monkeypatch):
    # A terminal's exit between turns clears its own badge through the
    # chat's viewers (no pump is attached).
    from core.session import session_state
    monkeypatch.setattr(session_state, "_push_pump_event_fn", lambda cid, ev: False)
    viewer: asyncio.Queue = asyncio.Queue()
    monkeypatch.setitem(session_state._dashboard_notify_queues, "s-viewer", viewer)

    async def run():
        tr = _bg_cmd_tr()
        _swept_bg_candidate(tr)
        get_bg_command_registry("s1").chat_id = "chat-remote"
        layer, info = _make_layer_info(tr)
        router = asyncio.create_task(_adapter()._route_notifications(info))
        try:
            info.event_queue.put_nowait(_ev(
                "item/completed", MAIN, type="commandExecution", id="i1",
                command="sleep 40", status="completed", exitCode=0,
                aggregatedOutput="done"))
            for _ in range(50):
                await asyncio.sleep(0.01)
                if not viewer.empty():
                    break
        finally:
            router.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await router

    try:
        asyncio.run(run())
        assert viewer.get_nowait() == {"type": "chat_ui_frame", "chat_id": "chat-remote", "frame": {
            "type": "bg_command_done", "tool_use_id": "i1", "status": "completed"}}
    finally:
        _bg_command_registries.pop("s1", None)


def test_router_untracked_item_completed_still_dropped():
    async def run():
        tr = _bg_cmd_tr()
        layer, info = _make_layer_info(tr)
        router = asyncio.create_task(_adapter()._route_notifications(info))
        try:
            # Between turns, a completion for an item the translator never
            # tracked is a plain main-thread straggler — dropped.
            info.event_queue.put_nowait(_ev(
                "item/completed", MAIN, type="commandExecution", id="ghost",
                status="completed", exitCode=0))
            await asyncio.sleep(0.05)
            assert get_bg_command_registry("s1").spawned == set()
            assert info.engine_state.thread_consumers == {}
            assert not router.done()
        finally:
            router.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await router

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop("s1", None)


def test_remote_teardown_sweeps_pending_terminals_as_killed():
    async def run():
        tr = _bg_cmd_tr()
        _swept_bg_candidate(tr)
        layer, info = _make_layer_info(tr)
        await _adapter().close_state(info)
        assert get_bg_command_registry("s1").completed == {"i1"}
        assert tr.pending_bg_commands() == []

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop("s1", None)


def test_codex_send_message_prunes_registry_preserving_pending():
    # The per-turn prune (reset_bg_command_registry): reset() PRESERVES
    # still-pending terminals but clears resolved + unsurfaced state, so the
    # cohort math can't fire spurious review turns.
    async def run():
        layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
        layer._cm = MagicMock()
        layer._cm.wait_abort_acked = AsyncMock(return_value=True)
        layer._cm.is_session_in_grace = MagicMock(return_value=False)
        info = RemoteSessionInfo(
            session_id="s1", machine_id="m1", agent_name="a",
            execution_path="codex-cli", event_queue=asyncio.Queue(),
            resume_handle=MAIN,
        )
        info.engine_state = CodexRemoteState(
            translator=CodexEventTranslator(model="m", supervised_bg=True),
        )
        layer._sessions = {"s1": info}
        # The router is the queue's sole consumer — the satellite's turn_ended
        # lands on event_queue and reaches the turn through it.
        router = asyncio.create_task(_adapter()._route_notifications(info))

        async def _send(machine_id, msg, **kw):
            if msg.get("type") == "send_message":
                info.event_queue.put_nowait(
                    {"type": "_turn_ended", "command_id": ""})
            return {}

        layer._cm.send_command = AsyncMock(side_effect=_send)
        reg = get_bg_command_registry("s1")
        reg.register_spawn("pending-i", "pending-i")
        reg.register_spawn("done-i", "done-i")
        reg.mark_done("done-i", surfaced=False)

        try:
            events = [e async for e in layer.send_message("s1", "hi")]
        finally:
            router.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await router
        assert events[-1].type == DONE
        assert reg.spawned == {"pending-i"}   # still-running preserved
        assert reg.completed == set()
        assert reg.unsurfaced_count == 0

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop("s1", None)


# ---------------------------------------------------------------------------
# Drain backstop (codex branch of RemoteExecutionLayer.drain_bg_commands)
# ---------------------------------------------------------------------------

def _drain_layer(sid, *, translator=None):
    cm = SatelliteConnectionManager()
    cm._connections["m1"] = SatelliteConnection(
        machine_id="m1", ws=None, satellite_version="0.5.171",
    )
    layer = RemoteExecutionLayer(cm)
    info = RemoteSessionInfo(
        session_id=sid, machine_id="m1", agent_name="a",
        execution_path="codex-cli", event_queue=asyncio.Queue(),
        resume_handle=MAIN,
    )
    info.engine_state = CodexRemoteState(translator=translator)
    layer._sessions[sid] = info
    return layer, info, cm


def _scripted_send(calls, ack):
    async def _send(machine_id, msg, *, timeout=30.0, command_id=None):
        calls.append(msg)
        if isinstance(ack, Exception):
            raise ack
        return ack
    return _send


def test_drain_codex_keeps_item_present_in_list():
    sid = "s-rbg-item"

    async def run():
        tr = _bg_cmd_tr(sid)
        _swept_bg_candidate(tr)
        layer, info, cm = _drain_layer(sid, translator=tr)
        calls: list = []
        cm.send_command = _scripted_send(calls, {"ok": True, "terminals": [
            {"itemId": "i1", "processId": "p-1", "command": "sleep 40"}]})
        assert await layer.drain_bg_commands(sid) is False
        assert calls == [{"type": "codex_bg_terminals", "session_id": sid}]
        assert get_bg_command_registry(sid).pending_count == 1
        assert tr.pending_bg_commands() != []

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop(sid, None)


def test_drain_codex_keeps_item_matched_by_process_id_only():
    # The list may name the CANONICAL itemId (ours is the synthesized
    # approval-path one) — the pid learned via terminalInteraction must still
    # match the row, keeping the entry pending.
    sid = "s-rbg-pid"

    async def run():
        tr = _bg_cmd_tr(sid)
        _swept_bg_candidate(tr, pid="p-1")
        layer, info, cm = _drain_layer(sid, translator=tr)
        cm.send_command = _scripted_send([], {"ok": True, "terminals": [
            {"itemId": "item-canonical-9", "processId": "p-1"}]})
        assert await layer.drain_bg_commands(sid) is False
        assert get_bg_command_registry(sid).pending_count == 1

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop(sid, None)


def test_drain_codex_resolves_item_missing_from_list():
    sid = "s-rbg-gone"

    async def run():
        tr = _bg_cmd_tr(sid)
        _swept_bg_candidate(tr)
        layer, info, cm = _drain_layer(sid, translator=tr)
        calls: list = []
        cm.send_command = _scripted_send(calls, {"ok": True, "terminals": []})
        # The router owns event_queue — the drain must never touch it.
        info.event_queue.put_nowait({"method": "x", "params": {}})
        assert await layer.drain_bg_commands(sid) is True
        assert get_bg_command_registry(sid).completed == {"i1"}
        assert tr.pending_bg_commands() == []
        assert info.event_queue.qsize() == 1
        assert not info.lock.locked()
        # Self-paced: an immediate re-poll (monitor retries every 0.3s) makes
        # no second RPC. (No pending would fast-path anyway — re-arm one.)
        get_bg_command_registry(sid).register_spawn("i9", "i9")
        assert await layer.drain_bg_commands(sid) is False
        assert len(calls) == 1

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop(sid, None)


def test_drain_codex_backs_off_while_a_turn_is_in_flight():
    # The turn's own translator resolves its completions: the drain asks the
    # machine for no list while the session's lock is held.
    sid = "s-rbg-lock"

    async def run():
        tr = _bg_cmd_tr(sid)
        _swept_bg_candidate(tr)
        layer, info, cm = _drain_layer(sid, translator=tr)
        calls: list = []
        cm.send_command = _scripted_send(calls, {"ok": True, "terminals": []})
        async with info.lock:
            assert await layer.drain_bg_commands(sid) is False
        assert calls == []
        assert get_bg_command_registry(sid).pending_count == 1

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop(sid, None)


def test_drain_codex_rpc_failure_is_no_progress():
    sid = "s-rbg-fail"

    async def run():
        tr = _bg_cmd_tr(sid)
        _swept_bg_candidate(tr)
        layer, info, cm = _drain_layer(sid, translator=tr)
        cm.send_command = _scripted_send([], RuntimeError("ack timeout"))
        assert await layer.drain_bg_commands(sid) is False
        # Never resolve-on-error: the entry stays pending for the next poll.
        assert get_bg_command_registry(sid).pending_count == 1
        assert tr.pending_bg_commands() != []
        assert not info.lock.locked()

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop(sid, None)


def test_drain_codex_satellite_refusal_is_no_progress():
    sid = "s-rbg-refuse"

    async def run():
        tr = _bg_cmd_tr(sid)
        _swept_bg_candidate(tr)
        layer, info, cm = _drain_layer(sid, translator=tr)
        cm.send_command = _scripted_send(
            [], {"ok": False, "terminals": None, "reason": "session not found"})
        assert await layer.drain_bg_commands(sid) is False
        assert get_bg_command_registry(sid).pending_count == 1

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop(sid, None)


# ---------------------------------------------------------------------------
# An idle Codex session a satellite kept across a proxy restart is taken back
# ---------------------------------------------------------------------------

def _adopting_layer(monkeypatch):
    """A remote layer on a connected machine, the credential and gateway
    restores stubbed, every control frame recorded."""
    cm = SatelliteConnectionManager()
    cm._connections["m1"] = SatelliteConnection(machine_id="m1", ws=None,
                                                satellite_version="0.5.138")
    layer = RemoteExecutionLayer(cm)
    monkeypatch.setattr(RemoteExecutionLayer, "_restore_adopted_credentials",
                        staticmethod(lambda *a, **k: None))

    async def _no_gateway(*a):
        return None
    monkeypatch.setattr(RemoteExecutionLayer, "_reprovision_gateway", staticmethod(_no_gateway))
    controls: list[tuple] = []

    async def _control(self, info, cm_, subtype, **kw):
        controls.append((info.session_id, subtype, kw))
        return True
    monkeypatch.setattr(type(_adapter()), "control_request", _control)
    return layer, controls


def test_an_idle_codex_session_is_adopted_on_its_thread(monkeypatch):
    from core.session import session_state

    async def run():
        layer, controls = _adopting_layer(monkeypatch)
        await layer.adopt_idle_session(
            machine_id="m1", session_id="s-ad1", agent_name="a",
            execution_path="codex-cli", mode="default", token_floor=0,
            resume_handle=MAIN, model="gpt-6.1-sol", used_mcps={"file-tools"},
            allow_full_fs=True,
        )
        info = layer._sessions["s-ad1"]
        try:
            assert info.resume_handle == MAIN and info.model == "gpt-6.1-sol"
            assert info.used_mcps == {"file-tools"} and info.allow_full_fs is True
            state = info.engine_state
            assert isinstance(state, CodexRemoteState) and state.router_task is not None
            # The sandbox follows the session's mode again, now (fire-and-forget).
            for _ in range(50):
                if controls:
                    break
                await asyncio.sleep(0.01)
            assert ("s-ad1", "set_permission_mode", {"mode": "default"}) in controls
            # A background sub-agent's events never reach a new turn's main consumer.
            state.default_consumer = asyncio.Queue()
            info.event_queue.put_nowait(_ev("item/started", SUB, type="agentMessage"))
            info.event_queue.put_nowait(_ev("item/started", MAIN, type="agentMessage"))
            for _ in range(50):
                if not state.default_consumer.empty():
                    break
                await asyncio.sleep(0.01)
            assert state.default_consumer.get_nowait()["params"]["threadId"] == MAIN
            assert state.thread_consumers[SUB].qsize() == 1
        finally:
            # No satellite answers here: the close frame's 15 s wait for one
            # is not this test's subject.
            async def _sent(*a, **k):
                return {}
            layer._cm.send_command = _sent
            await layer.close_session("s-ad1")

    try:
        asyncio.run(run())
    finally:
        session_state._sessions.pop("s-ad1", None)


def test_adoption_leaves_a_held_or_spawning_id_and_an_engine_that_cannot(monkeypatch):
    async def run():
        layer, _ = _adopting_layer(monkeypatch)
        held = object()
        layer._sessions["s-held"] = held
        layer._spawning.add("s-spawn")
        for sid, path in (("s-held", "codex-cli"), ("s-spawn", "codex-cli"),
                          ("s-direct", "direct-llm")):
            await layer.adopt_idle_session(
                machine_id="m1", session_id=sid, agent_name="a",
                execution_path=path, resume_handle=MAIN)
        assert layer._sessions == {"s-held": held}

    asyncio.run(run())


def test_a_new_thread_handle_is_written_on_the_next_turn():
    async def run():
        layer, info = _make_layer_info()
        translator = info.engine_state.translator
        translator._emitted_thread_id = True
        router = asyncio.create_task(_adapter()._route_notifications(info))
        try:
            info.event_queue.put_nowait({"type": "_resume_handle", "handle": MAIN})
            await asyncio.sleep(0.02)
            assert translator._emitted_thread_id is True     # the same thread
            info.event_queue.put_nowait({"type": "_resume_handle", "handle": "thread-NEW"})
            for _ in range(50):
                if info.resume_handle == "thread-NEW":
                    break
                await asyncio.sleep(0.01)
            assert info.resume_handle == "thread-NEW"
            assert translator._emitted_thread_id is False
        finally:
            router.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await router

    asyncio.run(run())


def test_a_spawn_over_an_adopted_record_ends_the_records_router(monkeypatch):
    async def run():
        layer, _ = _adopting_layer(monkeypatch)
        await layer.adopt_idle_session(
            machine_id="m1", session_id="s-ins", agent_name="a",
            execution_path="codex-cli", resume_handle=MAIN)
        old = layer._sessions["s-ins"]
        old_router = old.engine_state.router_task
        fresh = RemoteSessionInfo(session_id="s-ins", machine_id="m1", agent_name="a",
                                  execution_path="codex-cli", event_queue=asyncio.Queue())
        await layer._insert_session(fresh)
        assert layer._sessions["s-ins"] is fresh
        await asyncio.sleep(0)
        assert old_router.done()

    asyncio.run(run())

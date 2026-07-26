"""Remote Codex background sub-agents — the proxy-side router + supervisor.

The satellite is a dumb pipe: its persistent forwarder streams EVERY app-server
notification to the proxy as a session_event (including a background sub-agent's
events AFTER the main turn ends). All the demux + supervision lives on the proxy,
mirroring the LOCAL session (core/layers/codex/session.py) but consuming the
WS-forwarded ``info.event_queue`` instead of the daemon's notif_queue.

Covered:
1. Router demuxes main-thread events (+ synthetic turn-control markers) to the
   active turn's default_consumer, and each sub-agent thread to its own buffer.
2. A background sub-agent active at main-turn end is registered + supervised, and
   the supervisor resolves it (registry mark_done) when its thread terminates —
   reusing the shared resolve_bg_subagent + SubagentRegistry, so the cohort nudge
   + the delegation wait fire identically to local.
3. The version gate (satellite_supports_bg): only satellites >= 0.5.18 forward
   bg-thread events, so older ones leave supervision off (no spurious nudge).
"""
import asyncio
import contextlib

from core.layers.codex.layer import CodexEventTranslator
from core.layers.codex.session import CodexEvent
from core.remote.remote_execution import RemoteExecutionLayer, RemoteSessionInfo
from core.remote.satellite_connection import SatelliteConnection, SatelliteConnectionManager
from core.session.session_state import get_subagent_registry, _subagent_registries

MAIN = "thread-MAIN"
SUB = "thread-SUB-agent"


def _ev(method, tid, **item):
    """A forwarded codex notification {method, params} as it lands on event_queue."""
    params = {"threadId": tid}
    if item:
        params["item"] = item
    return {"method": method, "params": params}


def _make_layer_info(translator=None, *, bg=True):
    cm = SatelliteConnectionManager()
    layer = RemoteExecutionLayer(cm)
    info = RemoteSessionInfo(
        session_id="s1", machine_id="m1", agent_name="a",
        execution_path="codex-cli", event_queue=asyncio.Queue(),
        codex_translator=translator, codex_thread_id=MAIN, bg_supervised=bg,
    )
    layer._sessions["s1"] = info
    return layer, info


# ---------------------------------------------------------------------------
# 1. Router: demux by threadId
# ---------------------------------------------------------------------------

def test_router_demuxes_main_and_sub_threads():
    async def run():
        layer, info = _make_layer_info()
        info.default_consumer = asyncio.Queue()
        router = asyncio.create_task(layer._route_remote_notifications(info))
        try:
            info.event_queue.put_nowait(_ev("item/started", MAIN, type="agentMessage"))
            info.event_queue.put_nowait(_ev("item/started", SUB, type="agentMessage"))
            # synthetic turn-control marker (no codex ``method``) → main turn
            info.event_queue.put_nowait({"type": "_turn_ended", "command_id": "c1"})
            await asyncio.sleep(0.05)
            # MAIN event + the marker went to the active turn's consumer.
            assert info.default_consumer.qsize() == 2
            # The SUB event was siphoned into its own (lazily created) buffer.
            assert SUB in info.thread_consumers
            assert info.thread_consumers[SUB].qsize() == 1
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
        info.codex_thread_id = ""       # fresh session — unknown until the marker
        info.default_consumer = None    # no active turn yet
        router = asyncio.create_task(layer._route_remote_notifications(info))
        try:
            info.event_queue.put_nowait({"type": "_codex_thread_id", "thread_id": MAIN})
            await asyncio.sleep(0.05)
            assert info.codex_thread_id == MAIN
            # With the key learned, a sub-thread event now demuxes to its buffer.
            info.event_queue.put_nowait(_ev("item/started", SUB, type="agentMessage"))
            await asyncio.sleep(0.05)
            assert SUB in info.thread_consumers
        finally:
            router.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await router

    asyncio.run(run())


def test_router_fans_out_session_ended_sentinel():
    async def run():
        layer, info = _make_layer_info()
        info.default_consumer = asyncio.Queue()
        info.thread_consumers[SUB] = asyncio.Queue()
        router = asyncio.create_task(layer._route_remote_notifications(info))
        info.event_queue.put_nowait(None)  # satellite session_ended sentinel
        await asyncio.wait_for(router, timeout=2)  # router returns after fan-out
        # Every waiter gets the sentinel so none hang on a terminal that never comes.
        default_sentinel = info.default_consumer.get_nowait()
        assert default_sentinel is None
        thread_sentinel = info.thread_consumers[SUB].get_nowait()
        assert thread_sentinel is None

    asyncio.run(run())


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
            layer._handoff_remote_bg_subagents(info)
            assert SUB in reg.spawned
            sup = info.bg_supervisors.get(SUB)
            assert sup is not None

            # Feed the sub's terminal to its buffer → the supervisor resolves it.
            info.thread_consumers[SUB].put_nowait(
                {"method": "turn/completed", "params": {"threadId": SUB}}
            )
            await asyncio.wait_for(sup, timeout=5)
            assert SUB in reg.completed
            assert not reg.has_pending
            # The translator was tombstoned so a later collab snapshot can't reopen it.
            assert tr.subagent_end_event(SUB) == []
        finally:
            await layer._teardown_remote_bg(info)
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
        layer._handoff_remote_bg_subagents(info)
        assert info.bg_supervisors == {}
        assert not reg.has_pending
        assert info.default_consumer is None  # main-turn consumer cleared
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
            layer._handoff_remote_bg_subagents(info)
            sup = info.bg_supervisors[SUB]
            info.thread_consumers[SUB].put_nowait(None)  # session ended
            await asyncio.wait_for(sup, timeout=5)
            assert SUB in reg.completed
        finally:
            await layer._teardown_remote_bg(info)
            _subagent_registries.pop("s1", None)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. Version gate
# ---------------------------------------------------------------------------

def test_satellite_supports_bg_version_gate():
    cm = SatelliteConnectionManager()
    for mid, ver in [("new", "0.5.18"), ("old", "0.5.17"),
                     ("future", "0.6.0"), ("blank", "")]:
        cm._connections[mid] = SatelliteConnection(
            machine_id=mid, ws=None, satellite_version=ver,
        )
    assert cm.satellite_supports_bg("new") is True
    assert cm.satellite_supports_bg("future") is True
    assert cm.satellite_supports_bg("old") is False      # below the gate
    assert cm.satellite_supports_bg("blank") is False     # unknown version
    assert cm.satellite_supports_bg("absent") is False    # no connection


def test_satellite_supports_pty_inject_version_gate():
    cm = SatelliteConnectionManager()
    for mid, ver in [("new", "0.5.83"), ("old", "0.5.82"),
                     ("future", "0.6.0"), ("garbage", "abc")]:
        cm._connections[mid] = SatelliteConnection(
            machine_id=mid, ws=None, satellite_version=ver,
        )
    assert cm.satellite_supports_pty_inject("new") is True
    assert cm.satellite_supports_pty_inject("future") is True
    assert cm.satellite_supports_pty_inject("old") is False
    assert cm.satellite_supports_pty_inject("garbage") is False
    assert cm.satellite_supports_pty_inject("absent") is False


# ---------------------------------------------------------------------------
# Plan mode: the remote path synthesizes the SAME implement card as the local
# codex layer (codex delivers the plan as the turn's final agentMessage on the
# -p path, so a completed plan-mode turn emits a `plan_mode exit` before DONE).
# ---------------------------------------------------------------------------

from core.events.common_events import PLAN_MODE, DONE


def _drain_codex_turn(info, layer, raws):
    """Feed forwarded notifications, run one _stream_codex_turn, collect events."""
    async def run():
        for r in raws:
            info.event_queue.put_nowait(r)
        return [ev async for ev in layer._stream_codex_turn(info)]
    return asyncio.run(run())


def test_remote_plan_mode_synthesizes_implement_card():
    tr = CodexEventTranslator(model="gpt-5.5")
    layer, info = _make_layer_info(tr, bg=False)
    info.mode = "plan"  # read-only plan mode
    events = _drain_codex_turn(info, layer, [
        _ev("item/completed", MAIN, type="agentMessage",
            text="- Add --version\n- Add a CLI test"),
        {"type": "_turn_ended", "command_id": ""},
    ])
    plan = [e for e in events if e.type == PLAN_MODE]
    assert len(plan) == 1
    assert plan[0].data["action"] == "exit"
    assert plan[0].data["synthetic"] is True
    assert plan[0].data["tool_input"]["plan"].startswith("- Add --version")
    # The card is emitted BEFORE the terminal DONE (so it persists in the turn).
    assert events.index(plan[0]) < next(
        i for i, e in enumerate(events) if e.type == DONE
    )


def test_remote_default_mode_emits_no_plan_card():
    tr = CodexEventTranslator(model="gpt-5.5")
    layer, info = _make_layer_info(tr, bg=False)
    info.mode = "default"  # not a plan turn
    events = _drain_codex_turn(info, layer, [
        _ev("item/completed", MAIN, type="agentMessage", text="some answer"),
        {"type": "_turn_ended", "command_id": ""},
    ])
    assert not [e for e in events if e.type == PLAN_MODE]


def test_remote_interrupted_plan_turn_emits_no_card():
    tr = CodexEventTranslator(model="gpt-5.5")
    layer, info = _make_layer_info(tr, bg=False)
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
    _bg_command_registries, get_bg_command_registry,
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
        router = asyncio.create_task(layer._route_remote_notifications(info))
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
            assert info.thread_consumers == {}
            assert info.default_consumer is None
            assert not router.done()  # a guarded apply never kills the router
        finally:
            router.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await router

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop("s1", None)


def test_router_untracked_item_completed_still_dropped():
    async def run():
        tr = _bg_cmd_tr()
        layer, info = _make_layer_info(tr)
        router = asyncio.create_task(layer._route_remote_notifications(info))
        try:
            # Between turns, a completion for an item the translator never
            # tracked is a plain main-thread straggler — dropped.
            info.event_queue.put_nowait(_ev(
                "item/completed", MAIN, type="commandExecution", id="ghost",
                status="completed", exitCode=0))
            await asyncio.sleep(0.05)
            assert get_bg_command_registry("s1").spawned == set()
            assert info.thread_consumers == {}
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
        await layer._teardown_remote_bg(info)
        assert get_bg_command_registry("s1").completed == {"i1"}
        assert tr.pending_bg_commands() == []

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop("s1", None)


def test_codex_send_message_prunes_registry_preserving_pending():
    # The per-turn prune (reset_bg_command_registry) must run for remote codex
    # too: reset() PRESERVES still-pending terminals but clears resolved +
    # unsurfaced state, so the cohort math can't fire spurious review turns.
    async def run():
        layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
        layer._cm = MagicMock()
        layer._cm.wait_abort_acked = AsyncMock(return_value=True)
        layer._cm.is_session_in_grace = MagicMock(return_value=False)
        info = RemoteSessionInfo(
            session_id="s1", machine_id="m1", agent_name="a",
            execution_path="codex-cli", event_queue=asyncio.Queue(),
            codex_thread_id=MAIN, bg_supervised=False,
        )
        layer._sessions = {"s1": info}

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

        events = [e async for e in layer.send_message("s1", "hi")]
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

def _drain_layer(sid, *, version="0.5.105", translator=None):
    cm = SatelliteConnectionManager()
    cm._connections["m1"] = SatelliteConnection(
        machine_id="m1", ws=None, satellite_version=version,
    )
    layer = RemoteExecutionLayer(cm)
    info = RemoteSessionInfo(
        session_id=sid, machine_id="m1", agent_name="a",
        execution_path="codex-cli", event_queue=asyncio.Queue(),
        codex_translator=translator, codex_thread_id=MAIN, bg_supervised=True,
    )
    layer._sessions[sid] = info
    return layer, info, cm


def _scripted_send(calls, ack):
    async def _send(machine_id, msg, *, timeout=30.0, command_id=None):
        calls.append(msg)
        if isinstance(ack, Exception):
            raise ack
        return ack
    return _send


def test_drain_codex_version_gate_off_is_noop():
    sid = "s-rbg-gate"

    async def run():
        tr = _bg_cmd_tr(sid)
        _swept_bg_candidate(tr)
        layer, info, cm = _drain_layer(sid, version="0.5.104", translator=tr)
        calls: list = []
        cm.send_command = _scripted_send(calls, {"ok": True, "terminals": []})
        assert await layer.drain_bg_commands(sid) is False
        assert calls == []      # gated BEFORE sending — no burnt ack timeout
        assert get_bg_command_registry(sid).pending_count == 1

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop(sid, None)


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


def test_supports_codex_bg_terminals_version_gate():
    cm = SatelliteConnectionManager()
    for mid, ver in [("new", "0.5.105"), ("old", "0.5.104"),
                     ("future", "0.6.0"), ("blank", "")]:
        cm._connections[mid] = SatelliteConnection(
            machine_id=mid, ws=None, satellite_version=ver,
        )
    assert cm.supports_codex_bg_terminals("new") is True
    assert cm.supports_codex_bg_terminals("future") is True
    assert cm.supports_codex_bg_terminals("old") is False
    assert cm.supports_codex_bg_terminals("blank") is False
    assert cm.supports_codex_bg_terminals("absent") is False

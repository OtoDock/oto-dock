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

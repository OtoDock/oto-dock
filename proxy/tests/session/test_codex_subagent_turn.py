"""Codex background sub-agents: the per-session thread router, bg detection, and
the supervisor that feeds the SubagentRegistry.

Two guarantees are covered:

1. **No main-turn truncation.** A sub-agent runs on its own thread and emits its
   OWN ``turn/completed`` (and possibly ``error``). The router now siphons every
   sub-agent-thread notification into a per-thread buffer, so the main turn loop
   only ever sees MAIN-thread events — a sub-agent's ``turn/completed`` can no
   longer reach the loop and truncate the turn (dropping the main agent's
   post-sub-agent synthesis — the 0.5.17 bug, now structurally impossible).

2. **Background sub-agents are tracked + resolved.** A sub-agent still active at
   the main ``turn/completed`` is "background": the translator keeps it (no sweep)
   and reports it via ``pending_bg_subagents()``; the session registers it in the
   SubagentRegistry and arms a supervisor that drains its thread buffer to the
   terminal, then marks it done — so the shared _bg_agent_monitor can nudge.
"""
import asyncio


from core.events.common_events import SUBAGENT_START, SUBAGENT_END
from core.layers.codex.layer import CodexEventTranslator
from core.layers.codex.session import CodexAppServerSession, CodexEvent
from core.session.session_state import get_subagent_registry, _subagent_registries

MAIN = "thread-MAIN"
SUB = "thread-SUB-agent"      # in reality the agentId == the sub-agent thread id


def _evt(method, tid, text=None, item_type=None, states=None):
    item = {}
    if item_type:
        item = {"type": item_type, "id": "i1"}
        if text is not None:
            item["text"] = text
        if states is not None:
            item["agentsStates"] = states
    params = {"threadId": tid}
    if item:
        params["item"] = item
    return (method, params)


# The exact interleaving the daemon produces for "spawn a sub-agent, wait, reply":
# the sub-agent's turn/completed lands BEFORE the main agent's synthesis.
BUG_SEQUENCE = [
    _evt("turn/started", MAIN),
    _evt("item/completed", MAIN, text="Spawning a sub-agent; waiting for its result.",
         item_type="agentMessage"),
    _evt("item/completed", MAIN, item_type="collabAgentToolCall",
         states={"agent-1": {"status": "pendingInit"}}),
    # --- sub-agent thread runs entirely on its own thread ---
    _evt("turn/started", SUB),
    _evt("item/completed", SUB, text="pineapple", item_type="agentMessage"),
    _evt("turn/completed", SUB),                       # <-- used to truncate the turn
    # --- back on the main thread: wait resolves, THEN the synthesis ---
    _evt("item/completed", MAIN, item_type="collabAgentToolCall",
         states={"agent-1": {"status": "completed"}}),
    _evt("item/completed", MAIN, text="The sub-agent returned: pineapple.",
         item_type="agentMessage"),                    # <-- THE SYNTHESIS (was dropped)
    _evt("turn/completed", MAIN),                       # <-- the real turn end
]


class _FakeClient:
    """Minimal AppServerClient: a notif_queue + a request() that enqueues the
    turn's notifications when turn/start is called (mirrors the daemon)."""

    def __init__(self, sequence):
        self.notif_queue: asyncio.Queue = asyncio.Queue()
        self._sequence = sequence
        self.is_alive = True

    async def request(self, method, params=None, *, timeout=None):
        if method == "turn/start":
            for item in self._sequence:
                self.notif_queue.put_nowait(item)
            return {"turn": {"id": "turn-1"}}
        return {}


def _make_session(sequence, *, translator=None):
    s = CodexAppServerSession(
        session_id="s1", agent_name="a", model="gpt-5.4",
        sandbox_mode="workspace-write", working_dir="/tmp", config_dir="/tmp/.codex",
        thread_id=MAIN,
    )
    s._started = True
    s._client = _FakeClient(sequence)
    s.translator = translator
    return s


async def _drive(session):
    """Drive one turn with the router live (tests bypass start()); return the
    MAIN-thread events the session yielded. Tear-down is the caller's job."""
    session._router_task = asyncio.create_task(session._route_notifications())
    out = []
    async for ev in session.send_message("hi"):
        out.append((ev.type, ev.data))
    return out


def _texts(events):
    return [
        d.get("item", {}).get("text", "")
        for (m, d) in events
        if m == "item/completed" and d.get("item", {}).get("type") == "agentMessage"
    ]


# ---------------------------------------------------------------------------
# 1. Router: the main turn loop only sees MAIN-thread events (no truncation)
# ---------------------------------------------------------------------------

def test_subagent_turn_completed_does_not_truncate_main_turn():
    async def run():
        session = _make_session(BUG_SEQUENCE)
        try:
            events = await _drive(session)
        finally:
            await session._teardown_bg(reason="test")
        methods = [m for (m, _d) in events]

        # The MAIN synthesis (emitted AFTER the sub-agent's turn/completed) is yielded.
        assert "The sub-agent returned: pineapple." in _texts(events), (
            "main agent's post-sub-agent synthesis was dropped"
        )
        # The stream ENDS on the MAIN turn/completed (the last event).
        assert methods[-1] == "turn/completed"
        assert events[-1][1].get("threadId") == MAIN
        # The sub-agent's stream was routed AWAY: its turn/completed + its
        # "pineapple" message never reached the main turn loop.
        assert methods.count("turn/completed") == 1
        assert "pineapple" not in _texts(events)

    asyncio.run(run())


def test_main_turn_completed_still_ends_turn():
    async def run():
        seq = [
            _evt("turn/started", MAIN),
            _evt("item/completed", MAIN, text="hello", item_type="agentMessage"),
            _evt("turn/completed", MAIN),
            # anything after a MAIN turn/completed must NOT be yielded:
            _evt("item/completed", MAIN, text="LATE — should not appear", item_type="agentMessage"),
        ]
        session = _make_session(seq)
        try:
            return await _drive(session)
        finally:
            await session._teardown_bg(reason="test")

    events = asyncio.run(run())
    assert "LATE — should not appear" not in _texts(events)
    assert events[-1][0] == "turn/completed"


def test_subagent_error_does_not_truncate_main_turn():
    async def run():
        # A non-retryable error on a SUB thread is routed away — it must not end
        # the main turn (nor appear in the main stream).
        seq = [
            _evt("turn/started", MAIN),
            _evt("turn/started", SUB),
            ("error", {"threadId": SUB, "error": {"message": "sub boom", "willRetry": False}}),
            _evt("item/completed", MAIN, text="recovered: still here", item_type="agentMessage"),
            _evt("turn/completed", MAIN),
        ]
        session = _make_session(seq)
        try:
            return await _drive(session)
        finally:
            await session._teardown_bg(reason="test")

    events = asyncio.run(run())
    assert "recovered: still here" in _texts(events)
    assert events[-1][0] == "turn/completed"
    assert "error" not in [m for (m, _d) in events]


# ---------------------------------------------------------------------------
# 2. Translator: background sub-agent detection (no sweep at turn end)
# ---------------------------------------------------------------------------

def test_translator_does_not_sweep_active_subagent_at_turn_end():
    tr = CodexEventTranslator(model="m", supervised_bg=True)  # local path
    tr._main_thread_id = MAIN
    # spawn a bg sub-agent (status running) — SUBAGENT_START fires
    start = tr.translate(CodexEvent("item/completed", {"threadId": MAIN, "item": {
        "type": "collabAgentToolCall", "id": "c1", "prompt": "do bg work",
        "receiverThreadIds": [SUB], "agentsStates": {SUB: {"status": "running"}}}}))
    starts = [e for e in start if e.type == SUBAGENT_START and e.data["tool_use_id"] == SUB]
    assert starts, "no SUBAGENT_START for the spawned sub"
    # Codex subs are marked background so the dashboard keeps the badge spinning
    # past main-turn end (onDone only auto-clears non-background subs).
    assert starts[0].data["run_in_background"] is True
    # The spawn prompt rides as tool_input (CLI task_spawn parity) — the
    # dashboard pill expands to the full prompt.
    assert starts[0].data["tool_input"] == {"prompt": "do bg work"}

    # main turn completes while the sub is still active → must NOT be swept
    end = tr.translate(CodexEvent("turn/completed", {"threadId": MAIN,
                                                     "turn": {"status": "completed"}}))
    assert not any(e.type == SUBAGENT_END for e in end), "active bg sub swept at turn end"

    # it is reported as a pending background sub-agent for the session to supervise
    assert tr.pending_bg_subagents() == [{"agent_id": SUB, "description": "do bg work"}]

    # the supervisor later emits SUBAGENT_END exactly once (idempotent)
    fin = tr.subagent_end_event(SUB)
    assert len(fin) == 1 and fin[0].type == SUBAGENT_END and fin[0].data["tool_use_id"] == SUB
    assert tr.subagent_end_event(SUB) == []


def test_subagent_start_without_prompt_has_no_tool_input():
    # A collab snapshot can surface an agent with no spawn prompt (state-message
    # fallback) — the pill stays un-expandable rather than expanding to junk.
    tr = CodexEventTranslator(model="m", supervised_bg=True)
    tr._main_thread_id = MAIN
    evs = tr.translate(CodexEvent("item/completed", {"threadId": MAIN, "item": {
        "type": "collabAgentToolCall", "id": "c1",
        "agentsStates": {SUB: {"status": "running", "message": "working"}}}}))
    starts = [e for e in evs if e.type == SUBAGENT_START]
    assert len(starts) == 1
    assert "tool_input" not in starts[0].data
    assert starts[0].data["description"] == "working"


def test_translator_no_pending_for_foreground_subagent():
    tr = CodexEventTranslator(model="m", supervised_bg=True)
    tr._main_thread_id = MAIN
    tr.translate(CodexEvent("item/completed", {"threadId": MAIN, "item": {
        "type": "collabAgentToolCall", "id": "c1", "prompt": "fg",
        "receiverThreadIds": [SUB], "agentsStates": {SUB: {"status": "running"}}}}))
    # the sub reaches terminal BEFORE the main turn ends (foreground / waited)
    tr.translate(CodexEvent("item/completed", {"threadId": MAIN, "item": {
        "type": "collabAgentToolCall", "id": "c1",
        "agentsStates": {SUB: {"status": "completed"}}}}))
    tr.translate(CodexEvent("turn/completed", {"threadId": MAIN,
                                               "turn": {"status": "completed"}}))
    assert tr.pending_bg_subagents() == []  # nothing to supervise — it finished


def test_resolved_subagent_not_reopened_by_later_collab():
    # After a bg sub finishes, a LATER turn referencing it (e.g. the agent
    # "closes"/wait_agents it in the review-nudge turn) must NOT re-emit a
    # SUBAGENT_START — that produced a phantom second badge.
    tr = CodexEventTranslator(model="m", supervised_bg=True)
    tr._main_thread_id = MAIN
    s1 = tr.translate(CodexEvent("item/completed", {"threadId": MAIN, "item": {
        "type": "collabAgentToolCall", "id": "c1", "prompt": "bg",
        "receiverThreadIds": [SUB], "agentsStates": {SUB: {"status": "running"}}}}))
    assert any(e.type == SUBAGENT_START for e in s1)
    tr.translate(CodexEvent("turn/completed", {"threadId": MAIN,
                                               "turn": {"status": "completed"}}))
    # supervisor resolves it (tombstones the id)
    fin = tr.subagent_end_event(SUB)
    assert len(fin) == 1 and fin[0].type == SUBAGENT_END

    # later turn: the agent references the FINISHED sub again (its agentsStates
    # reappears in a fresh collab item) — must produce NO START and NO END.
    later = tr.translate(CodexEvent("item/completed", {"threadId": MAIN, "item": {
        "type": "collabAgentToolCall", "id": "c2", "prompt": "closing it",
        "receiverThreadIds": [SUB], "agentsStates": {SUB: {"status": "completed"}}}}))
    assert not any(e.type == SUBAGENT_START for e in later), "phantom re-spawn of finished sub"
    assert not any(e.type == SUBAGENT_END for e in later)
    # and it is NOT re-registered as pending (no phantom supervisor)
    assert tr.pending_bg_subagents() == []


def test_translator_remote_sweeps_active_subagent_at_turn_end():
    # Remote path (no bg supervisor): preserve the original behavior — a still-
    # active sub-agent IS swept to SUBAGENT_END at main-turn end so its badge can't
    # hang (no proxy-side supervisor will clear it).
    tr = CodexEventTranslator(model="m")  # supervised_bg defaults False (remote)
    tr._main_thread_id = MAIN
    tr.translate(CodexEvent("item/completed", {"threadId": MAIN, "item": {
        "type": "collabAgentToolCall", "id": "c1", "prompt": "bg",
        "receiverThreadIds": [SUB], "agentsStates": {SUB: {"status": "running"}}}}))
    end = tr.translate(CodexEvent("turn/completed", {"threadId": MAIN,
                                                     "turn": {"status": "completed"}}))
    assert any(e.type == SUBAGENT_END and e.data["tool_use_id"] == SUB for e in end)
    assert tr.pending_bg_subagents() == []  # swept + cleared


# ---------------------------------------------------------------------------
# 3. Session: background sub-agent registered, supervised, and resolved
# ---------------------------------------------------------------------------

def test_background_subagent_registered_supervised_and_resolved():
    async def run():
        seq = [
            _evt("turn/started", MAIN),
            _evt("item/completed", MAIN, item_type="collabAgentToolCall",
                 states={SUB: {"status": "running"}}),
            _evt("item/completed", MAIN, text="Spawned in the background; not waiting.",
                 item_type="agentMessage"),
            _evt("turn/completed", MAIN),                 # main turn ends — chat idle
            # the bg sub keeps running on its own thread, then terminates:
            _evt("item/completed", SUB, text="bg result", item_type="agentMessage"),
            _evt("turn/completed", SUB),
        ]
        tr = CodexEventTranslator(model="gpt-5.4", supervised_bg=True)
        tr._main_thread_id = MAIN
        # simulate the layer having fed the spawn collab item (sub is active)
        tr._subagents = {SUB: {"started": True, "desc": "bg task"}}
        session = _make_session(seq, translator=tr)

        reg = get_subagent_registry("s1")
        reg.reset()
        try:
            main_stream = await _drive(session)

            # The main turn ended (chat goes idle) — the bg sub did NOT keep it open,
            # and the sub's output was routed away from the main stream.
            assert main_stream[-1][0] == "turn/completed"
            assert main_stream[-1][1].get("threadId") == MAIN
            assert "bg result" not in _texts(main_stream)

            # The bg sub is registered + supervised after the hand-off.
            assert SUB in reg.spawned
            sup = session._bg_supervisors.get(SUB)
            assert sup is not None

            # The router fed the SUB terminal to the supervisor → it resolves the
            # registry (so the _bg_agent_monitor's all-done event can fire + nudge).
            await asyncio.wait_for(sup, timeout=5)
            assert SUB in reg.completed
            assert not reg.has_pending
        finally:
            await session._teardown_bg(reason="test")
            _subagent_registries.pop("s1", None)

    asyncio.run(run())


def test_registry_reset_preserves_pending_bg_subagent():
    # CLI parity: a follow-up turn's reset must NOT wipe a still-running bg agent
    # (else the monitor never sees it finish — the "lost nudge while bg runs" bug).
    reg = get_subagent_registry("reset-test")
    reg.reset()
    try:
        reg.register_spawn("bg-A", "tuid-A")     # background, still running
        reg.register_spawn("fg-B", "tuid-B")
        reg.mark_done("fg-B")                     # foreground, resolved this turn
        assert reg.has_pending and reg.pending_count == 1

        reg.reset()                               # next turn starts

        assert reg.has_pending, "still-pending bg agent was wiped by reset()"
        assert reg.spawned == {"bg-A"}
        assert reg.completed == set()
        assert reg.tuid_for("bg-A") == "tuid-A"   # spawning tool_use_id preserved
        assert reg.tuid_for("fg-B") == ""         # resolved entry dropped

        reg.mark_done("bg-A")
        assert not reg.has_pending
    finally:
        _subagent_registries.pop("reset-test", None)


# ---------------------------------------------------------------------------
# Multi-agent v2 subAgentActivity items (ultra / proactive orchestration, 0.144)
# ---------------------------------------------------------------------------


def _activity(kind, aid=SUB, path="root/fix-tests", tid=MAIN):
    return CodexEvent(type="item/completed", data={
        "threadId": tid,
        "item": {"type": "subAgentActivity", "id": "call-1", "kind": kind,
                 "agentThreadId": aid, "agentPath": path},
    })


def test_subagent_activity_started_maps_to_subagent_start():
    tr = CodexEventTranslator("gpt-5.6-sol")
    tr.translate(CodexEvent(type="turn/started", data={"threadId": MAIN}))
    evs = tr.translate(_activity("started"))
    assert len(evs) == 1 and evs[0].type == SUBAGENT_START
    d = evs[0].data
    assert d["tool_use_id"] == SUB
    assert d["description"] == "fix-tests"     # agentPath task segment
    assert d["run_in_background"] is True


def test_subagent_activity_start_converges_with_collab_end():
    # v2 spawn emits the activity item; the terminal state still arrives via a
    # wait_agent collab snapshot — one START, one END, shared per-agent record.
    tr = CodexEventTranslator("gpt-5.6-sol")
    tr.translate(CodexEvent(type="turn/started", data={"threadId": MAIN}))
    assert tr.translate(_activity("started"))[0].type == SUBAGENT_START
    assert tr.translate(_activity("started")) == []   # duplicate spawn item
    ends = tr.translate(CodexEvent(type="item/completed", data={
        "threadId": MAIN,
        "item": {"type": "collabAgentToolCall", "id": "c1",
                 "agentsStates": {SUB: {"status": "completed"}}},
    }))
    assert [e.type for e in ends] == [SUBAGENT_END]
    assert ends[0].data["tool_use_id"] == SUB
    # tombstoned: neither path may re-open the badge
    assert tr.translate(_activity("started")) == []


def test_subagent_activity_interrupted_ends_badge():
    tr = CodexEventTranslator("gpt-5.6-sol")
    tr.translate(CodexEvent(type="turn/started", data={"threadId": MAIN}))
    tr.translate(_activity("started"))
    evs = tr.translate(_activity("interrupted"))
    assert [e.type for e in evs] == [SUBAGENT_END]
    assert tr.translate(_activity("interrupted")) == []  # idempotent


def test_subagent_activity_interacted_is_lifecycle_noop():
    tr = CodexEventTranslator("gpt-5.6-sol")
    tr.translate(CodexEvent(type="turn/started", data={"threadId": MAIN}))
    tr.translate(_activity("started"))
    assert tr.translate(_activity("interacted")) == []

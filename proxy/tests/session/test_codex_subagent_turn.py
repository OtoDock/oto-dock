"""Codex background sub-agents in the translator: detection, the hand-off
list and the badge events.

A sub-agent still active at the main ``turn/completed`` is "background": the
translator keeps it (no sweep) and reports it via ``pending_bg_subagents()``,
so the session's holder registers it in the SubagentRegistry and supervises
its thread to the terminal (``tests/remote/test_remote_codex_bg.py``).
"""
from core.events.common_events import SUBAGENT_END, SUBAGENT_START
from core.layers.codex.events import CodexEvent
from core.layers.codex.layer import CodexEventTranslator
from core.session.session_state import _subagent_registries, get_subagent_registry

MAIN = "thread-MAIN"
SUB = "thread-SUB-agent"      # in reality the agentId == the sub-agent thread id


# ---------------------------------------------------------------------------
# Translator: background sub-agent detection (no sweep at turn end)
# ---------------------------------------------------------------------------

def test_translator_does_not_sweep_active_subagent_at_turn_end():
    tr = CodexEventTranslator(model="m", supervised_bg=True)
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


def test_an_unsupervised_translator_sweeps_an_active_subagent_at_turn_end():
    # No bg supervisor (the translator's default): a still-active sub-agent IS
    # swept to SUBAGENT_END at main-turn end so its badge can't hang (nothing
    # else will clear it).
    tr = CodexEventTranslator(model="m")
    tr._main_thread_id = MAIN
    tr.translate(CodexEvent("item/completed", {"threadId": MAIN, "item": {
        "type": "collabAgentToolCall", "id": "c1", "prompt": "bg",
        "receiverThreadIds": [SUB], "agentsStates": {SUB: {"status": "running"}}}}))
    end = tr.translate(CodexEvent("turn/completed", {"threadId": MAIN,
                                                     "turn": {"status": "completed"}}))
    assert any(e.type == SUBAGENT_END and e.data["tool_use_id"] == SUB for e in end)
    assert tr.pending_bg_subagents() == []  # swept + cleared


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

"""Extended CLI / Codex / Direct-LLM feature parity audit.

The satellite is a strict dumb pipe. The shared translators
(`ClaudeCLIEventTranslator`, `CodexEventTranslator`, `SettleController`)
guarantee that every event a local execution layer emits also reaches
the proxy via the satellite path with identical `CommonEvent` shape.

`test_remote_translator_parity.py` covers the canonical mixed turn.
This module extends coverage to:

  * Plan-mode events (CLI)
  * Context-compaction lifecycle (CLI)
  * Permission requests (CLI native control_request)
  * Codex translator parity (text, tool calls, update_plan, METADATA)
  * Codex effort xhigh+max collapse onto xhigh
  * Direct LLM refuses-remote by design (routing returns local layer
    regardless of execution_target)
"""

from __future__ import annotations

from unittest.mock import patch

from core.layers.cli.layer import cli_chunk_to_events
from core.layers.cli.translator import ClaudeCLIEventTranslator


def _run(events: list[dict]) -> list[tuple]:
    t = ClaudeCLIEventTranslator("sid")
    out = []
    for e in events:
        for c in t.feed(e):
            for ev in cli_chunk_to_events(c):
                out.append((ev.type, dict(ev.data or {})))
    return out


# ---------------------------------------------------------------------------
# CLI: plan mode round-trip
# ---------------------------------------------------------------------------


def test_plan_mode_round_trip():
    """ExitPlanMode tool call surfaces as PLAN_MODE event with the plan body."""
    events = [
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "tu1", "name": "ExitPlanMode"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta",
                      "partial_json": '{"plan":"# My plan\\n- step 1"}'}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
    ]
    stream = _run(events)
    types = [e[0] for e in stream]
    # PLAN_MODE is derived in cli_chunk_to_events for ExitPlanMode tool.
    assert "plan_mode" in types, f"got types: {types}"
    # Plan body must round-trip
    plan_event = next(e for e in stream if e[0] == "plan_mode")
    assert "step 1" in str(plan_event[1])


# ---------------------------------------------------------------------------
# CLI: context compaction events
# ---------------------------------------------------------------------------


def test_context_compact_started_and_completed():
    """The translator surfaces both phases of auto-compaction so the pump
    can render the right UX (started → spinner, completed → fresh gauge)."""
    events = [
        {"type": "system", "subtype": "compact_boundary",
         "compact_metadata": {"trigger": "auto",
                              "pre_tokens": 150000, "post_tokens": 80000}},
    ]
    stream = _run(events)
    types = [e[0] for e in stream]
    assert "context_compact" in types, f"got types: {types}"


# ---------------------------------------------------------------------------
# CLI: native permission request (control_request.can_use_tool)
# ---------------------------------------------------------------------------


def test_permission_request_round_trip():
    """control_request.can_use_tool → PERMISSION_REQUEST CommonEvent."""
    events = [
        {"type": "control_request", "request_id": "req-1", "request": {
            "subtype": "can_use_tool",
            "tool_name": "Bash",
            "input": {"command": "ls"},
        }},
    ]
    stream = _run(events)
    types = [e[0] for e in stream]
    assert "permission_request" in types, f"got types: {types}"


# ---------------------------------------------------------------------------
# Codex translator parity
# ---------------------------------------------------------------------------


def test_codex_translator_text_delta():
    """app-server `item/agentMessage/delta` notifications → incremental TEXT.

    Shapes verified live against codex 0.120.0: userMessage items are suppressed
    (we persist the user turn ourselves), text streams as incremental `delta`s.
    """
    from core.layers.codex import CodexEventTranslator, CodexEvent

    t = CodexEventTranslator(model="gpt-5.4")
    out = []
    for method, params in [
        ("turn/started", {}),
        ("item/started", {"item": {"id": "u0", "type": "userMessage", "content": []}}),
        ("item/started", {"item": {"id": "i1", "type": "agentMessage"}}),
        ("item/agentMessage/delta", {"itemId": "i1", "delta": "Hello"}),
        ("item/agentMessage/delta", {"itemId": "i1", "delta": " world"}),
        ("item/completed", {"item": {"id": "i1", "type": "agentMessage", "text": "Hello world"}}),
    ]:
        out.extend(t.translate(CodexEvent(type=method, data=params)))

    texts = [e.data.get("content") for e in out if e.type == "text"]
    assert "".join(texts) == "Hello world", f"got {texts}"
    # The userMessage item must NOT produce a tool/text event.
    assert all(e.type == "text" for e in out), [e.type for e in out]
    # codex_thread_id comes from the thread/start RESPONSE, emitted once.
    meta = t.thread_id_metadata("th-1")
    assert any(e.type == "metadata" and e.data.get("codex_thread_id") == "th-1" for e in meta)
    assert t.thread_id_metadata("th-1") == []  # only emitted once


def test_codex_translator_reasoning_to_thinking():
    """`reasoning` items frame THINKING; `item/reasoning/textDelta` streams it."""
    from core.layers.codex import CodexEventTranslator, CodexEvent

    t = CodexEventTranslator()
    out = []
    for method, params in [
        ("item/started", {"item": {"id": "r1", "type": "reasoning"}}),
        ("item/reasoning/textDelta", {"itemId": "r1", "delta": "thinking..."}),
        ("item/completed", {"item": {"id": "r1", "type": "reasoning"}}),
    ]:
        out.extend(t.translate(CodexEvent(type=method, data=params)))
    phases = [e.data.get("phase") for e in out if e.type == "thinking"]
    assert phases == ["start", "delta", "end"], phases


def test_codex_translator_command_execution_normalized_to_bash():
    """app-server `commandExecution` items → TOOL_USE name='Bash' (CLI parity)."""
    from core.layers.codex import CodexEventTranslator, CodexEvent

    t = CodexEventTranslator()
    out = t.translate(CodexEvent(type="item/started", data={
        "item": {"id": "c1", "type": "commandExecution", "command": "ls -la", "cwd": "/x"},
    }))
    tool_events = [e for e in out if e.type == "tool_use"]
    assert len(tool_events) == 1
    assert tool_events[0].data.get("name") == "Bash"


def test_codex_translator_mcp_tool_call_namespaced():
    """`mcpToolCall` items → TOOL_USE mcp__{server}__{tool} (server+tool split)."""
    from core.layers.codex import CodexEventTranslator, CodexEvent

    t = CodexEventTranslator()
    out = t.translate(CodexEvent(type="item/started", data={
        "item": {"id": "m1", "type": "mcpToolCall", "server": "display", "tool": "display_images"},
    }))
    tool = [e for e in out if e.type == "tool_use"][0]
    assert tool.data["name"] == "mcp__display__display_images"


def test_codex_translator_completed_command_carries_output():
    """`commandExecution` completion → TOOL_RESULT with the command output.

    Field names verified against a LIVE app-server probe (codex 0.142.5):
    ``aggregatedOutput`` (stdout+stderr interleaved), ``exitCode``, ``status``.
    Headless Codex chats previously dropped the body entirely — the dashboard
    pill had no Output section while interactive transcripts (rollout tailer)
    showed it."""
    from core.layers.codex import CodexEventTranslator, CodexEvent

    t = CodexEventTranslator()
    out = t.translate(CodexEvent(type="item/completed", data={
        "item": {"id": "c1", "type": "commandExecution",
                 "command": "printf 'alpha\\nbeta\\n'",
                 "aggregatedOutput": "alpha\nbeta\n",
                 "exitCode": 0, "status": "completed"},
    }))
    res = [e for e in out if e.type == "tool_result"][0]
    assert res.data["name"] == "Bash"
    assert res.data["result_content"] == "alpha\nbeta\n"
    assert res.data["is_error"] is False


def test_codex_translator_failed_command_flags_error():
    from core.layers.codex import CodexEventTranslator, CodexEvent

    t = CodexEventTranslator()
    out = t.translate(CodexEvent(type="item/completed", data={
        "item": {"id": "c2", "type": "commandExecution", "command": "boom",
                 "aggregatedOutput": "oops\n", "exitCode": 3, "status": "failed"},
    }))
    res = [e for e in out if e.type == "tool_result"][0]
    assert res.data["result_content"] == "oops\n"
    assert res.data["is_error"] is True
    # Failed with NO captured output still shows the exit code.
    out = t.translate(CodexEvent(type="item/completed", data={
        "item": {"id": "c3", "type": "commandExecution", "command": "boom",
                 "aggregatedOutput": "", "exitCode": 3, "status": "failed"},
    }))
    res = [e for e in out if e.type == "tool_result"][0]
    assert res.data["result_content"] == "(exit 3)"


def test_codex_translator_file_change_output_is_the_patch():
    """`fileChange` completion → TOOL_RESULT body = per-change kind+path+diff
    (probe-verified shape: changes[{path, kind.type, diff}])."""
    from core.layers.codex import CodexEventTranslator, CodexEvent

    t = CodexEventTranslator()
    out = t.translate(CodexEvent(type="item/completed", data={
        "item": {"id": "f1", "type": "fileChange", "status": "completed",
                 "changes": [{"path": "/x/probe.txt",
                              "kind": {"type": "add"}, "diff": "PROBE-OK\n"}]},
    }))
    res = [e for e in out if e.type == "tool_result"][0]
    assert res.data["name"] == "apply_patch"
    assert res.data["result_content"] == "add /x/probe.txt\nPROBE-OK\n"
    assert res.data["is_error"] is False


def test_codex_translator_mcp_result_and_error_bodies():
    """`mcpToolCall` completion: result.content text blocks join as the body;
    error.message rides as an error body (codex-rs v2 McpToolCallResult:
    content: Vec<content block>, error: {message})."""
    from core.layers.codex import CodexEventTranslator, CodexEvent

    t = CodexEventTranslator()
    out = t.translate(CodexEvent(type="item/completed", data={
        "item": {"id": "m1", "type": "mcpToolCall", "server": "s", "tool": "t",
                 "status": "completed",
                 "result": {"content": [
                     {"type": "text", "text": "hello"},
                     {"type": "image", "data": "…"},
                     {"type": "text", "text": "world"},
                 ]}},
    }))
    res = [e for e in out if e.type == "tool_result"][0]
    assert res.data["result_content"] == "hello\nworld"
    assert res.data["is_error"] is False

    out = t.translate(CodexEvent(type="item/completed", data={
        "item": {"id": "m2", "type": "mcpToolCall", "server": "s", "tool": "t",
                 "status": "failed", "error": {"message": "server exploded"}},
    }))
    res = [e for e in out if e.type == "tool_result"][0]
    assert res.data["result_content"] == "server exploded"
    assert res.data["is_error"] is True

    # No result/error → no body keys (a bare completion stays shape-stable).
    out = t.translate(CodexEvent(type="item/completed", data={
        "item": {"id": "m3", "type": "mcpToolCall", "server": "s", "tool": "t",
                 "status": "completed"},
    }))
    res = [e for e in out if e.type == "tool_result"][0]
    assert "result_content" not in res.data


def test_codex_translator_plan_updated_to_todo_update():
    """app-server `turn/plan/updated` → TODO_UPDATE (status mapped to platform)."""
    from core.layers.codex import CodexEventTranslator, CodexEvent

    t = CodexEventTranslator()
    out = t.translate(CodexEvent(type="turn/plan/updated", data={
        "explanation": None,
        "plan": [{"step": "do thing", "status": "inProgress"}],
    }))
    todos = [e for e in out if e.type == "todo_update"]
    assert len(todos) == 1
    assert todos[0].data["todos"][0] == {"content": "do thing", "status": "in_progress"}


def test_codex_translator_goal_updated_to_goal_update():
    """app-server `thread/goal/updated` → GOAL_UPDATE (snake_cased ThreadGoal).

    Wire shape verified LIVE vs 0.142.5 (thread/goal/set RPC probe): params
    {threadId, turnId, goal} with ThreadGoal {threadId, objective, status,
    tokenBudget, tokensUsed, timeUsedSeconds, createdAt, updatedAt}."""
    from core.layers.codex import CodexEventTranslator, CodexEvent

    t = CodexEventTranslator()
    out = t.translate(CodexEvent(type="thread/goal/updated", data={
        "threadId": "th-1", "turnId": "turn-1",
        "goal": {"threadId": "th-1", "objective": "Ship the release",
                 "status": "active", "tokenBudget": 500000,
                 "tokensUsed": 12345, "timeUsedSeconds": 61,
                 "createdAt": 1783433303, "updatedAt": 1783433311},
    }))
    assert [e.type for e in out] == ["goal_update"]
    assert out[0].data == {
        "objective": "Ship the release", "status": "active",
        "token_budget": 500000, "tokens_used": 12345,
        "time_used_seconds": 61, "cleared": False,
    }
    # A model "mark complete" arrives as an update with status "complete"
    # (verified live) — NOT as thread/goal/cleared. The panel hides on it.
    out = t.translate(CodexEvent(type="thread/goal/updated", data={
        "threadId": "th-1", "turnId": "turn-1",
        "goal": {"objective": "Ship the release", "status": "complete",
                 "tokenBudget": 500000, "tokensUsed": 20000,
                 "timeUsedSeconds": 90},
    }))
    assert out[0].data["status"] == "complete"
    # Null budget stays None (the panel hides its progress bar); flat params
    # (no `goal` wrapper) are tolerated; a missing status defaults to active.
    out = t.translate(CodexEvent(type="thread/goal/updated", data={
        "objective": "No budget", "tokenBudget": None, "tokensUsed": 1,
        "timeUsedSeconds": 2,
    }))
    assert out[0].data["token_budget"] is None
    assert out[0].data["objective"] == "No budget"
    assert out[0].data["status"] == "active"


def test_codex_translator_goal_cleared():
    """`thread/goal/cleared` → GOAL_UPDATE {cleared: True} (no goal fields)."""
    from core.layers.codex import CodexEventTranslator, CodexEvent

    t = CodexEventTranslator()
    out = t.translate(CodexEvent(type="thread/goal/cleared", data={"threadId": "th-1"}))
    assert [e.type for e in out] == ["goal_update"]
    assert out[0].data == {"cleared": True}


def test_codex_translator_goal_junk_shapes_ignored():
    """Malformed goal payloads are dropped without breaking the stream."""
    from core.layers.codex import CodexEventTranslator, CodexEvent

    t = CodexEventTranslator()
    for junk in [
        {},                                             # no goal at all
        {"goal": {"tokenBudget": 5}},                   # missing objective
        {"goal": {"objective": ""}},                    # empty objective
        {"goal": {"objective": "x", "tokensUsed": "a lot"}},  # non-numeric
        {"goal": "not-a-dict"},                         # wrong container type
    ]:
        assert t.translate(CodexEvent(type="thread/goal/updated", data=junk)) == []
    # The stream is unaffected: a valid update still translates afterwards.
    out = t.translate(CodexEvent(type="thread/goal/updated", data={
        "goal": {"objective": "recovered", "tokensUsed": 3, "timeUsedSeconds": 4},
    }))
    assert out[0].data["objective"] == "recovered"


def test_codex_translator_goal_subagent_thread_suppressed():
    """A sub-agent thread's goal notifications never reach the main stream."""
    from core.layers.codex import CodexEventTranslator, CodexEvent

    t = CodexEventTranslator()
    t.translate(CodexEvent(type="turn/started", data={"threadId": "main"}))
    assert t.translate(CodexEvent(type="thread/goal/updated", data={
        "threadId": "sub-1", "goal": {"objective": "sub goal"},
    })) == []
    out = t.translate(CodexEvent(type="thread/goal/updated", data={
        "threadId": "main", "goal": {"objective": "main goal"},
    }))
    assert out[0].data["objective"] == "main goal"


def test_codex_translator_turn_completed_metadata_and_done():
    """tokenUsage.last drives per-turn cost; turn/completed → METADATA + DONE.

    Numbers mirror the live-validated turn: per-turn `last` breakdown, non-cached
    input split for pricing, and the context gauge populated from inputTokens +
    modelContextWindow (a gain over exec mode, which hid it)."""
    from core.layers.codex import CodexEventTranslator, CodexEvent

    t = CodexEventTranslator(model="gpt-5.4")
    t.translate(CodexEvent(type="thread/tokenUsage/updated", data={
        "tokenUsage": {
            "last": {"totalTokens": 11566, "inputTokens": 11551,
                     "cachedInputTokens": 3456, "outputTokens": 15},
            "modelContextWindow": 258400,
        },
    }))
    out = t.translate(CodexEvent(type="turn/completed", data={
        "turn": {"status": "completed", "durationMs": 1234},
    }))
    types = [e.type for e in out]
    assert types == ["metadata", "done"], types
    md = out[0].data
    assert md["cost_is_delta"] is True
    assert md["input_tokens"] == 11551 - 3456  # non-cached
    assert md["cache_read"] == 3456
    assert md["output_tokens"] == 15
    assert md["context_used"] == 11551 and md["context_max"] == 258400
    assert md["duration_ms"] == 1234


def test_codex_translator_turn_failed_to_error():
    """turn/completed with status=failed → ERROR + DONE (no turn/failed method)."""
    from core.layers.codex import CodexEventTranslator, CodexEvent

    t = CodexEventTranslator()
    out = t.translate(CodexEvent(type="turn/completed", data={
        "turn": {"status": "failed", "error": {"message": "boom"}},
    }))
    assert [e.type for e in out] == ["error", "done"]
    assert out[0].data["message"] == "boom"


# ---------------------------------------------------------------------------
# Codex collab sub-agents (0.5) — collabAgentToolCall → SUBAGENT_START/END.
# Frames mirror a real gpt-5.4 multi_agent turn captured live.
# ---------------------------------------------------------------------------


def test_codex_translator_subagent_spawn_and_wait():
    """spawnAgent/completed → SUBAGENT_START keyed by the agent thread id
    (description from the spawn prompt); wait/completed → SUBAGENT_END for the
    agent whose agentsStates went terminal. Diffed from the authoritative
    agentsStates snapshot, so completions are per-agent (not coarse)."""
    from core.layers.codex import CodexEventTranslator, CodexEvent

    a1 = "019e8618-a5cb-7c13-bde2-97a5fec4caf8"
    a2 = "019e8618-a6a2-7681-b196-66f384f5a0ac"
    t = CodexEventTranslator(model="gpt-5.4")

    def completed(item):
        return t.translate(CodexEvent(type="item/completed", data={"item": item}))

    out = completed({
        "type": "collabAgentToolCall", "id": "c1", "tool": "spawnAgent",
        "status": "completed", "receiverThreadIds": [a1],
        "prompt": "Write exactly one 3-word phrase about the ocean. Return only the phrase.",
        "agentsStates": {a1: {"status": "pendingInit", "message": None}},
    })
    starts = [e for e in out if e.type == "subagent_start"]
    assert len(starts) == 1
    assert starts[0].data["tool_use_id"] == a1
    assert "ocean" in starts[0].data["description"]
    # Every Codex sub-agent is marked background: it runs on its own thread and
    # may outlive the main turn (clears on its own per-agent SUBAGENT_END).
    assert starts[0].data["run_in_background"] is True

    completed({
        "type": "collabAgentToolCall", "id": "c2", "tool": "spawnAgent",
        "status": "completed", "receiverThreadIds": [a2],
        "prompt": "Write exactly one 3-word phrase about mountains. Return only the phrase.",
        "agentsStates": {a2: {"status": "pendingInit", "message": None}},
    })

    out = completed({
        "type": "collabAgentToolCall", "id": "c3", "tool": "wait",
        "status": "completed", "receiverThreadIds": [a1],
        "agentsStates": {a1: {"status": "completed", "message": "Endless blue horizon"}},
    })
    assert [e.data["tool_use_id"] for e in out if e.type == "subagent_end"] == [a1]
    assert not [e for e in out if e.type == "subagent_start"]  # no duplicate START

    out = completed({
        "type": "collabAgentToolCall", "id": "c4", "tool": "wait",
        "status": "completed", "receiverThreadIds": [a2],
        "agentsStates": {a2: {"status": "completed", "message": "Silent alpine peaks"}},
    })
    assert [e.data["tool_use_id"] for e in out if e.type == "subagent_end"] == [a2]


def test_codex_translator_subagent_turn_end_sweep():
    """A sub-agent still active at turn end is swept to SUBAGENT_END (safety net),
    and the per-turn tracker resets so the next turn starts clean."""
    from core.layers.codex import CodexEventTranslator, CodexEvent

    a1 = "agent-thread-1"
    t = CodexEventTranslator(model="gpt-5.4")
    t.translate(CodexEvent(type="item/completed", data={"item": {
        "type": "collabAgentToolCall", "id": "c1", "tool": "spawnAgent",
        "status": "completed", "receiverThreadIds": [a1], "prompt": "do x",
        "agentsStates": {a1: {"status": "running", "message": None}},
    }}))
    out = t.translate(CodexEvent(type="turn/completed", data={"turn": {"status": "completed"}}))
    types = [e.type for e in out]
    assert types[0] == "subagent_end" and out[0].data["tool_use_id"] == a1
    assert types[-2:] == ["metadata", "done"]
    # Reset: a second turn doesn't re-end the (already-swept) agent.
    out2 = t.translate(CodexEvent(type="turn/completed", data={"turn": {"status": "completed"}}))
    assert not [e for e in out2 if e.type == "subagent_end"]


def test_codex_translator_subagent_terminal_without_prior_start():
    """Robustness: a first agentsStates sighting that is ALREADY terminal (a
    spawn we never saw active) emits START then END so the widget renders +
    clears rather than being dropped."""
    from core.layers.codex import CodexEventTranslator, CodexEvent

    a1 = "agent-thread-9"
    t = CodexEventTranslator()
    out = t.translate(CodexEvent(type="item/completed", data={"item": {
        "type": "collabAgentToolCall", "id": "c1", "tool": "wait",
        "status": "completed", "receiverThreadIds": [a1],
        "agentsStates": {a1: {"status": "completed", "message": "done"}},
    }}))
    assert [e.type for e in out] == ["subagent_start", "subagent_end"]
    assert out[0].data["tool_use_id"] == a1 and out[1].data["tool_use_id"] == a1


def test_codex_translator_suppresses_subagent_thread_notifications():
    """Multi-agent thread demux (live-validated): the daemon multiplexes each
    spawned sub-agent thread onto the one connection. Only the MAIN thread's
    stream surfaces — a sub-agent's own agentMessage / turn-completion must NOT
    leak as the main agent's text or emit a premature DONE/METADATA. The main
    thread id is captured from its first turn/started; everything tagged with a
    different threadId is dropped."""
    from core.layers.codex import CodexEventTranslator, CodexEvent

    MAIN, SUB = "main-thread", "sub-thread"
    t = CodexEventTranslator(model="gpt-5.4")

    # First turn/started captures the main thread id (its output is suppressed).
    assert t.translate(CodexEvent(type="turn/started", data={"threadId": MAIN, "turn": {}})) == []
    # Main-thread agent text → surfaces.
    out = t.translate(CodexEvent(type="item/agentMessage/delta",
                                 data={"threadId": MAIN, "itemId": "m1", "delta": "main text"}))
    assert [e.type for e in out] == ["text"] and out[0].data["content"] == "main text"
    # Sub-agent thread's own text → SUPPRESSED (no leak into the main chat).
    assert t.translate(CodexEvent(type="item/agentMessage/delta",
                                  data={"threadId": SUB, "itemId": "s1", "delta": "secret subagent text"})) == []
    assert t.translate(CodexEvent(type="item/completed", data={
        "item": {"type": "agentMessage", "id": "s1", "text": "secret subagent text"}, "threadId": SUB})) == []
    # Sub-agent thread's turn/completed → SUPPRESSED (no premature DONE/METADATA).
    assert t.translate(CodexEvent(type="turn/completed",
                                  data={"threadId": SUB, "turn": {"status": "completed"}})) == []
    # Main thread's turn/completed → the ONE METADATA + DONE.
    out = t.translate(CodexEvent(type="turn/completed",
                                 data={"threadId": MAIN, "turn": {"status": "completed"}}))
    assert [e.type for e in out] == ["metadata", "done"]


# ---------------------------------------------------------------------------
# Codex effort xhigh + max collapse
# ---------------------------------------------------------------------------


def test_codex_effort_collapses_xhigh_and_max():
    """Platform max collapses onto Codex 'xhigh' for pre-5.6 models (their
    scale tops there) and passes through as wire 'max' on the gpt-5.6 family
    — same mapping used by both local and remote paths."""
    from core.layers.codex.helpers import map_effort_to_codex

    assert map_effort_to_codex("low") == "low"
    assert map_effort_to_codex("medium") == "medium"
    assert map_effort_to_codex("high") == "high"
    assert map_effort_to_codex("xhigh") == "xhigh"
    assert map_effort_to_codex("max") == "xhigh"  # pre-5.6 collapse
    assert map_effort_to_codex("max", "gpt-5.6-sol") == "max"  # 5.6 unlock
    assert map_effort_to_codex("ultra", "gpt-5.6-sol") == "ultra"  # Sol/Terra/Astra
    assert map_effort_to_codex("ultra", "gpt-5.6-luna") == "max"  # Luna ceiling


# ---------------------------------------------------------------------------
# Direct LLM: refuses-remote by design
# ---------------------------------------------------------------------------


def test_direct_llm_always_returns_local_layer_even_with_remote_target():
    """Direct LLM has no satellite path. SessionManager must return the
    DirectLLMExecutionLayer regardless of execution_target — Direct LLM
    sessions can't be migrated to a satellite."""
    from core.session.session_manager import get_execution_layer, _direct_layer

    with patch("core.session.session_manager.agent_store") as mock_store:
        mock_store.get_agent.return_value = {
            "execution_path": "direct-llm",
            "execution_target": "some-remote-machine-id",  # ignored
        }
        layer = get_execution_layer("test-agent")
        assert layer is _direct_layer


def test_direct_llm_local_target_returns_local():
    """The expected local case for Direct LLM also returns the direct layer."""
    from core.session.session_manager import get_execution_layer, _direct_layer

    with patch("core.session.session_manager.agent_store") as mock_store:
        mock_store.get_agent.return_value = {
            "execution_path": "direct-llm",
            "execution_target": "local",
        }
        layer = get_execution_layer("test-agent")
        assert layer is _direct_layer


def test_codex_translator_rate_limits_recorded_not_emitted():
    """``account/rateLimits/updated`` feeds the pool's window samples on a side
    channel and never becomes a CommonEvent (a pool account's usage is not
    the chatting user's business)."""
    from unittest.mock import patch
    from core.layers.codex import CodexEventTranslator, CodexEvent

    snapshot = {"limitName": "codex", "planType": "plus",
                "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1789114918},
                "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1789447223}}
    with patch("core.layers.codex.usage.record_snapshot_async") as rec:
        t = CodexEventTranslator(session_id="sess-1")
        assert t.translate(CodexEvent(type="account/rateLimits/updated",
                                      data={"rateLimits": snapshot})) == []
        rec.assert_called_once_with("sess-1", snapshot)
        # Params that ARE the snapshot (no wrapper) work too.
        rec.reset_mock()
        assert t.translate(CodexEvent(type="account/rateLimits/updated", data=snapshot)) == []
        rec.assert_called_once_with("sess-1", snapshot)
        # No session id (back-compat construction): nothing to attribute to.
        rec.reset_mock()
        assert CodexEventTranslator().translate(
            CodexEvent(type="account/rateLimits/updated", data={"rateLimits": snapshot})) == []
        rec.assert_not_called()

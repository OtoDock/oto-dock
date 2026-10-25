"""Fixture-based tests for ClaudeCLIEventTranslator.

These tests verify the pure parsing logic: raw NDJSON → ClaudeStreamChunk.
No DB, no subprocess, no I/O — just a state-machine feed.

The test DB fixture (autouse) is still initialized but unused by these tests.
"""

from __future__ import annotations

from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.session.session_state import get_subagent_registry, reset_subagent_registry


def _summarize(chunks: list) -> list[tuple]:
    """Compact representation for assertions."""
    out = []
    for c in chunks:
        if c.text:
            out.append((c.event_type or "text", "text", c.text))
        elif c.event_type:
            out.append((c.event_type, "data", dict(c.event_data or {})))
        if c.is_done:
            out.append(("done", "flag", True))
        if c.is_error:
            out.append(("error", "flag", True))
    return out


def test_system_init_suppressed():
    t = ClaudeCLIEventTranslator("s1")
    out = t.feed({"type": "system", "subtype": "init", "mcp_servers": [{"name": "m1", "status": "connected"}]})
    assert out == []


def test_status_heartbeat_suppressed():
    """`status` is the only CLI system subtype that's pure heartbeat noise.
    `task_started`/`task_progress` are now handled (registry + workflow)."""
    t = ClaudeCLIEventTranslator("s1")
    assert t.feed({"type": "system", "subtype": "status"}) == []


def test_task_started_local_bash_not_gated():
    """Backgrounded Bash emits task_started + task_notification but NO
    SubagentStop — so it must NOT enter the completion gate (it would hang)."""
    reset_subagent_registry("s-bash")
    t = ClaudeCLIEventTranslator("s-bash")
    out = t.feed({"type": "system", "subtype": "task_started",
                  "task_id": "bash1", "tool_use_id": "tu-b",
                  "task_type": "local_bash"})
    # Backgrounded bash emits a bg_command_start tracking chunk (badge/inline),
    # but is NOT registered as a subagent → it never enters the completion gate.
    assert len(out) == 1 and out[0].event_type == "bg_command_start"
    reg = get_subagent_registry("s-bash")
    assert "bash1" not in reg.spawned


def test_task_started_local_agent_registers_spawn():
    reset_subagent_registry("s-ag")
    t = ClaudeCLIEventTranslator("s-ag")
    out = t.feed({"type": "system", "subtype": "task_started",
                  "task_id": "ag1", "tool_use_id": "tu-1",
                  "subagent_type": "general-purpose", "task_type": "local_agent"})
    assert out == []  # no dashboard event — SUBAGENT_START already covered the spawn
    reg = get_subagent_registry("s-ag")
    assert "ag1" in reg.spawned
    assert reg.tuid_for("ag1") == "tu-1"
    assert reg.has_pending is True


def test_task_notification_marks_agent_done_and_emits_subagent_end():
    reset_subagent_registry("s-tn")
    t = ClaudeCLIEventTranslator("s-tn")
    t.feed({"type": "system", "subtype": "task_started",
            "task_id": "ag1", "tool_use_id": "tu-1", "task_type": "local_agent"})
    out = t.feed({"type": "system", "subtype": "task_notification",
                  "task_id": "ag1", "tool_use_id": "tu-1", "status": "completed"})
    assert len(out) == 1
    assert out[0].event_type == "subagent_end"
    assert out[0].event_data["tool_use_id"] == "tu-1"
    reg = get_subagent_registry("s-tn")
    assert "ag1" in reg.completed
    assert reg.has_pending is False
    # Idempotent: a duplicate (e.g. after the SubagentStop hook already marked
    # it done) yields nothing.
    assert t.feed({"type": "system", "subtype": "task_notification",
                   "task_id": "ag1", "tool_use_id": "tu-1"}) == []


def test_task_notification_unknown_task_id_dropped():
    """A task_notification for an untracked id (local_bash, noise) is a no-op —
    no spurious subagent_end, no gate pollution."""
    reset_subagent_registry("s-unk")
    t = ClaudeCLIEventTranslator("s-unk")
    assert t.feed({"type": "system", "subtype": "task_notification",
                   "task_id": "ghost", "tool_use_id": "tu-x"}) == []
    reg = get_subagent_registry("s-unk")
    assert "ghost" not in reg.completed


def test_workflow_start_progress_end():
    reset_subagent_registry("s-wf")
    t = ClaudeCLIEventTranslator("s-wf")
    started = t.feed({"type": "system", "subtype": "task_started",
                      "tool_use_id": "tu-wf", "task_type": "local_workflow",
                      "workflow_name": "find-bugs"})
    assert len(started) == 1 and started[0].event_type == "workflow_started"
    assert started[0].event_data == {"tool_use_id": "tu-wf", "workflow_name": "find-bugs"}
    assert "tu-wf" in get_subagent_registry("s-wf").workflow_tuids

    long_preview = "x" * 900
    prog = t.feed({"type": "system", "subtype": "task_progress",
                   "tool_use_id": "tu-wf",
                   "workflow_progress": [{"workflow_agent": "a1", "resultPreview": long_preview}]})
    assert len(prog) == 1 and prog[0].event_type == "workflow_progress"
    # Preview truncated to the cap (+ ellipsis) — satellite WS size guard.
    assert len(prog[0].event_data["workflow_progress"][0]["resultPreview"]) <= 501

    ended = t.feed({"type": "system", "subtype": "task_notification", "tool_use_id": "tu-wf"})
    assert len(ended) == 1 and ended[0].event_type == "workflow_ended"
    assert "tu-wf" not in get_subagent_registry("s-wf").workflow_tuids


def test_task_progress_without_workflow_dropped():
    t = ClaudeCLIEventTranslator("s1")
    assert t.feed({"type": "system", "subtype": "task_progress",
                   "task_id": "x", "description": "heartbeat"}) == []


def test_compacting_and_compact_boundary_yield():
    t = ClaudeCLIEventTranslator("s1")
    out_a = t.feed({"type": "system", "subtype": "compacting"})
    assert len(out_a) == 1 and out_a[0].event_data["subtype"] == "compacting"
    out_b = t.feed({"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": "auto"}})
    assert len(out_b) == 1 and out_b[0].event_data["subtype"] == "compact_boundary"


def test_control_request_can_use_tool_yields_permission_prompt():
    t = ClaudeCLIEventTranslator("s1")
    out = t.feed({
        "type": "control_request",
        "request_id": "req-1",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": "Bash",
            "tool_use_id": "tu-1",
            "input": {"command": "ls"},
            "description": "run ls",
        },
    })
    assert len(out) == 1
    assert out[0].event_type == "permission_prompt"
    assert out[0].event_data["request_id"] == "req-1"
    assert out[0].event_data["tool_name"] == "Bash"
    assert out[0].event_data["tool_input"] == {"command": "ls"}


def test_unknown_control_request_dropped():
    t = ClaudeCLIEventTranslator("s1")
    assert t.feed({"type": "control_request", "request": {"subtype": "set_model", "model": "x"}}) == []


def test_control_response_and_assistant_dropped():
    t = ClaudeCLIEventTranslator("s1")
    assert t.feed({"type": "control_response", "response": {}}) == []
    assert t.feed({"type": "assistant", "message": {}}) == []


def test_text_streaming_with_separator_between_blocks():
    t = ClaudeCLIEventTranslator("s1")
    events = [
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "World"}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}},
    ]
    texts = []
    for e in events:
        for c in t.feed(e):
            if c.text:
                texts.append(c.text)
    assert texts == ["Hello", "\n\n", "World"]
    assert t.has_emitted_text is True


def test_thinking_phase_start_and_end():
    t = ClaudeCLIEventTranslator("s1")
    events = [
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
    ]
    results = []
    for e in events:
        for c in t.feed(e):
            results.append((c.event_type, c.event_data))
    assert results[0] == ("thinking", {"phase": "start"})
    assert results[1] == ("thinking", {"text": "hmm"})
    assert results[2] == ("thinking", {"phase": "end"})


def test_tool_use_emits_tool_start_tool_info_then_tool_end_on_next_block():
    t = ClaudeCLIEventTranslator("s1")
    events = [
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "tu1", "name": "Bash"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"command": "ls -la"}'}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 1,
            "content_block": {"type": "text"}}},
    ]
    out = []
    for e in events:
        for c in t.feed(e):
            out.append((c.event_type, dict(c.event_data or {})))
    # First tool_use produces tool_start then tool_info; the next content_block_start emits tool_end for the still-active tool.
    assert out[0] == ("tool_start", {"name": "Bash", "tool_id": "tu1"})
    assert out[1][0] == "tool_info"
    assert out[1][1]["name"] == "Bash"
    assert out[1][1]["tool_input"] == {"command": "ls -la"}
    assert out[2][0] == "tool_end"
    assert out[2][1] == {"tool_id": "tu1", "name": "Bash"}


def test_bash_tool_info_summary_prefers_description():
    # The collapsed pill title is the model-written description; the command
    # itself is the expanded detail (falls back to the command when absent).
    t = ClaudeCLIEventTranslator("s1")
    events = [
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "tu1", "name": "Bash"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta",
                      "partial_json": '{"command": "ls -la", "description": "List home dir"}'}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
    ]
    out = []
    for e in events:
        for c in t.feed(e):
            out.append((c.event_type, dict(c.event_data or {})))
    info = [e for e in out if e[0] == "tool_info"]
    assert len(info) == 1
    assert info[0][1]["summary"] == "List home dir"


def test_task_spawn_background_carries_tool_use_id():
    t = ClaudeCLIEventTranslator("s1")
    events = [
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "tu1", "name": "Task"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta",
                      "partial_json": '{"description":"work","subagent_type":"general","run_in_background":true}'}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
    ]
    out = []
    for e in events:
        for c in t.feed(e):
            out.append((c.event_type, dict(c.event_data or {})))
    spawn = [e for e in out if e[0] == "task_spawn"]
    assert len(spawn) == 1
    # tool_use_id is the dashboard correlation key; run_in_background drives the
    # fg/bg color. The registry gate is populated later, by task_started.
    assert spawn[0][1]["run_in_background"] is True
    assert spawn[0][1]["tool_use_id"] == "tu1"
    assert spawn[0][1]["subagent_type"] == "general"
    # Full tool input rides the event — the subagent pill expands to it.
    assert spawn[0][1]["tool_input"] == {
        "description": "work", "subagent_type": "general",
        "run_in_background": True,
    }
    assert t.agents_spawned == 1


def test_task_spawn_foreground_carries_tool_use_id():
    t = ClaudeCLIEventTranslator("s1")
    events = [
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "tu1", "name": "Agent"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta",
                      "partial_json": '{"description":"task","run_in_background":false}'}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
    ]
    out = []
    for e in events:
        for c in t.feed(e):
            out.append((c.event_type, dict(c.event_data or {})))
    spawn = [e for e in out if e[0] == "task_spawn"]
    assert len(spawn) == 1
    assert spawn[0][1]["run_in_background"] is False
    assert spawn[0][1]["tool_use_id"] == "tu1"
    assert t.agents_spawned == 1

    # message_start no longer infers foreground completion — it only captures
    # context tokens (completion comes from the SubagentStop hook).
    out2 = t.feed({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}})
    assert not any(
        c.event_type == "system" and c.event_data.get("subtype") == "fg_agents_complete"
        for c in out2
    )


def test_delegate_suppressed_in_translator():
    """The translator must NOT emit delegate_spawn or an inline tool card for
    the delegate tool. The badge is emitted by the PROXY when the worker is
    actually created (the delegation spawn path → inject_pump_event), keyed by
    a stable task_id, so a rejected delegation never strands a badge."""
    t = ClaudeCLIEventTranslator("s1")
    events = [
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "tu1", "name": "mcp__delegation-mcp__delegate"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta",
                      "partial_json": '{"name":"sub","agent":"system-admin","prompt":"do it"}'}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
    ]
    out = []
    for e in events:
        for c in t.feed(e):
            out.append(c.event_type)
    # No delegate badge from the translator, and no generic tool_info card.
    assert "delegate_spawn" not in out
    assert "tool_info" not in out


def test_enter_plan_mode_and_exit_plan_mode():
    t = ClaudeCLIEventTranslator("s1")
    events_enter = [
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "tu1", "name": "EnterPlanMode"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{}'}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
    ]
    events_exit = [
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 1,
            "content_block": {"type": "tool_use", "id": "tu2", "name": "ExitPlanMode"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"plan":"## Plan"}'}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}},
    ]
    out = []
    for e in events_enter + events_exit:
        for c in t.feed(e):
            out.append((c.event_type, dict(c.event_data or {})))
    plans = [e for e in out if e[0] == "plan_mode"]
    assert len(plans) == 2
    assert plans[0][1]["action"] == "enter"
    assert plans[1][1]["action"] == "exit"
    assert plans[1][1]["tool_input"] == {"plan": "## Plan"}


def test_skip_inline_tools_do_not_emit_tool_info():
    t = ClaudeCLIEventTranslator("s1")
    events = [
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "tu1", "name": "AskUserQuestion"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta",
                      "partial_json": '{"question":"y/n"}'}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
    ]
    out = []
    for e in events:
        for c in t.feed(e):
            out.append(c.event_type)
    assert "tool_info" not in out


def test_message_start_captures_context_tokens():
    t = ClaudeCLIEventTranslator("s1")
    t.feed({"type": "stream_event", "event": {"type": "message_start",
        "message": {"usage": {"input_tokens": 100, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 50}}}})
    assert t.last_turn_context == 1050


def test_result_emits_metadata_with_all_fields():
    t = ClaudeCLIEventTranslator("s1")
    # Simulate some input tokens first
    t.feed({"type": "stream_event", "event": {"type": "message_start",
        "message": {"usage": {"input_tokens": 100, "cache_read_input_tokens": 500}}}})
    out = t.feed({
        "type": "result",
        "total_cost_usd": 0.05,
        "duration_ms": 4200,
        "is_error": False,
        "usage": {
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 10,
            "input_tokens": 100,
            "output_tokens": 30,
        },
        "modelUsage": {"claude-opus-4-7": {"contextWindow": 200000}},
    })
    meta_chunks = [c for c in out if c.event_type == "metadata"]
    assert len(meta_chunks) == 1
    m = meta_chunks[0].event_data
    assert m["cost_usd"] == 0.05
    assert m["duration_ms"] == 4200
    assert m["context_used"] == 600
    assert m["context_max"] == 200000
    assert m["cache_read"] == 500
    assert m["cache_write"] == 10
    assert m["input_tokens"] == 100
    assert m["output_tokens"] == 30


def test_result_error_emits_error_flag():
    t = ClaudeCLIEventTranslator("s1")
    out = t.feed({
        "type": "result",
        "is_error": True,
        "result": "boom",
        "total_cost_usd": 0,
        "duration_ms": 0,
    })
    error_chunks = [c for c in out if c.is_error]
    assert len(error_chunks) == 1
    assert "boom" in error_chunks[0].text


def test_job_done_marker_is_passed_through_verbatim():
    """[JOB_DONE] detection was removed — the marker (if a model ever emits it)
    now streams through as plain text, not stripped or acted on."""
    t = ClaudeCLIEventTranslator("s1")
    t.feed({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
        "content_block": {"type": "text"}}})
    out = []
    for delta in ("Result ready [JOB", "_DONE]"):
        for c in t.feed({"type": "stream_event", "event": {"type": "content_block_delta",
                "index": 0, "delta": {"type": "text_delta", "text": delta}}}):
            if c.text:
                out.append(c.text)
    assert out == ["Result ready [JOB", "_DONE]"]


def test_session_id_tracking_from_events():
    t = ClaudeCLIEventTranslator("s1")
    t.feed({"type": "system", "subtype": "init", "session_id": "s2-actual"})
    assert t.actual_session_id == "s2-actual"


def test_reset_for_settle_clears_parsing_state_keeps_spawn_count():
    t = ClaudeCLIEventTranslator("s1")
    t.agents_spawned = 3
    t._tool_inputs = {0: ["abc"]}
    t.has_emitted_text = True
    t.reset_for_settle()
    assert t.agents_spawned == 3       # spawn count survives settle
    assert t._tool_inputs == {}         # parsing state cleared
    assert t.has_emitted_text is False


def test_context_window_drift_warns_once_per_session(caplog):
    """A CLI predating a model reports its default contextWindow for it; the
    translator logs ONE warning per session — it is rebuilt every turn, so a
    per-instance flag would fire once per turn instead."""
    import logging

    import config as app_config
    from core.layers.cli import translator as tr

    model_id = next(iter(app_config.MODEL_REGISTRY))
    registry_win = app_config.MODEL_REGISTRY[model_id]["context_window"]
    tr._ctx_window_warned.discard("s-drift")
    result = {
        "type": "result", "total_cost_usd": 0, "duration_ms": 0,
        "modelUsage": {model_id: {"contextWindow": registry_win + 7}},
    }
    with caplog.at_level(logging.WARNING, logger="cli-translator"):
        ClaudeCLIEventTranslator("s-drift").feed(dict(result))
        ClaudeCLIEventTranslator("s-drift").feed(dict(result))  # next turn
    warnings = [r for r in caplog.records if "context window" in r.message]
    assert len(warnings) == 1
    assert model_id in warnings[0].message


def test_context_window_match_and_unknown_model_stay_silent(caplog):
    import logging

    import config as app_config
    from core.layers.cli import translator as tr

    model_id = next(iter(app_config.MODEL_REGISTRY))
    registry_win = app_config.MODEL_REGISTRY[model_id]["context_window"]
    tr._ctx_window_warned.discard("s-nodrift")
    with caplog.at_level(logging.WARNING, logger="cli-translator"):
        ClaudeCLIEventTranslator("s-nodrift").feed({
            "type": "result", "total_cost_usd": 0, "duration_ms": 0,
            "modelUsage": {model_id: {"contextWindow": registry_win}},
        })
        ClaudeCLIEventTranslator("s-custom").feed({
            "type": "result", "total_cost_usd": 0, "duration_ms": 0,
            "modelUsage": {"some-custom-model": {"contextWindow": 12345}},
        })
    assert not [r for r in caplog.records if "context window" in r.message]


# ---------------------------------------------------------------------------
# rate_limit_event — the account's window state, for the pool, not the chat
# ---------------------------------------------------------------------------

_RATE_LIMIT_INFO = {
    "status": "allowed", "resetsAt": 1787796000, "rateLimitType": "five_hour",
    "overageStatus": "rejected", "overageDisabledReason": "out_of_credits",
    "isUsingOverage": False,
    "unifiedWindows": {"five_hour": {"utilization": 0.14, "resetsAt": 1787796000},
                       "seven_day": {"utilization": 0.03, "resetsAt": 1788328800}},
}


def test_rate_limit_event_becomes_a_rate_limit_chunk():
    from core.layers.cli.settle import chunk_is_content
    t = ClaudeCLIEventTranslator("s1")
    out = t.feed({"type": "rate_limit_event", "rate_limit_info": _RATE_LIMIT_INFO,
                  "uuid": "u1", "session_id": "cli-1"})
    assert len(out) == 1
    chunk = out[0]
    assert chunk.event_type == "rate_limit"
    assert chunk.event_data == _RATE_LIMIT_INFO
    assert chunk.text == "" and not chunk.is_done and not chunk.is_error
    # Not content: it must never prove a turn is streaming.
    assert not chunk_is_content(chunk)


def test_rate_limit_event_without_info_is_dropped():
    t = ClaudeCLIEventTranslator("s1")
    assert t.feed({"type": "rate_limit_event"}) == []
    assert t.feed({"type": "rate_limit_event", "rate_limit_info": "bad"}) == []


def test_ping_stream_event_yields_nothing():
    """Claude Code 2.1.285 sends a ``ping`` stream event every 30 s during a
    non-streaming fallback with partial messages on: liveness, not content."""
    t = ClaudeCLIEventTranslator("s1")
    assert t.feed({"type": "stream_event", "event": {"type": "ping"}, "session_id": "s1"}) == []


# --- narration: the server's summary of the prose between tool calls ---------

def _se(event: dict) -> dict:
    return {"type": "stream_event", "event": event}


def _thinking_frame(text: str, *, indexes=(0,), parent=None) -> dict:
    frame = {"type": "assistant", "parent_tool_use_id": parent,
             "message": {"content": [{"type": "thinking", "thinking": text, "signature": "s"}]}}
    if indexes is not None:
        frame["narration_block_indexes"] = list(indexes)
    return frame


def _narration_block(t, idx: int, text: str, **frame_kw) -> list:
    """The shape the CLI streams (2.1.296, partial messages): the block's
    start and delta, its own assistant frame, then its stop."""
    out = t.feed(_se({"type": "content_block_start", "index": idx,
                      "content_block": {"type": "thinking", "thinking": ""}}))
    out += t.feed(_se({"type": "content_block_delta", "index": idx,
                       "delta": {"type": "thinking_delta", "thinking": text}}))
    out += t.feed(_thinking_frame(text, **frame_kw))
    out += t.feed(_se({"type": "content_block_stop", "index": idx}))
    return out


def test_a_narration_block_ends_as_a_note_and_its_words_follow_as_note_text():
    t = ClaudeCLIEventTranslator("s-narr")
    t.feed(_se({"type": "message_start", "message": {}}))
    # The empty reasoning block first (index 0), then the narration (index 1):
    # the frame's own index is 0 for both.
    empty = t.feed(_se({"type": "content_block_start", "index": 0,
                        "content_block": {"type": "thinking", "thinking": ""}}))
    empty += t.feed(_thinking_frame("", indexes=None))
    empty += t.feed(_se({"type": "content_block_stop", "index": 0}))
    assert _summarize(empty)[-1] == ("thinking", "data", {"phase": "end"})
    out = _narration_block(t, 1, "Read the file. Next I'll run it.")
    assert _summarize(out) == [
        ("thinking", "data", {"phase": "start"}),
        ("thinking", "data", {"text": "Read the file. Next I'll run it."}),
        ("thinking", "data", {"phase": "end", "note": True}),
        ("text", "text", "Read the file. Next I'll run it."),
    ]
    assert out[-1].event_data == {"note": True}
    # A note is not the answer: the next text block gets no separator.
    assert t.has_emitted_text is False


def test_the_note_text_reaches_the_event_as_text_with_note():
    from core.events.common_events import TEXT, THINKING
    from core.layers.cli.layer import cli_chunk_to_events
    t = ClaudeCLIEventTranslator("s-narr2")
    events = [e for c in _narration_block(t, 0, "Checked it.") for e in cli_chunk_to_events(c)]
    assert [(e.type, e.data) for e in events][-2:] == [
        (THINKING, {"phase": "end", "note": True}),
        (TEXT, {"content": "Checked it.", "note": True}),
    ]


def test_an_empty_listed_block_a_sub_agent_frame_and_a_frame_without_the_field_stay_thinking():
    t = ClaudeCLIEventTranslator("s-narr3")
    for kw in ({"indexes": (0,), "parent": "toolu_sub"}, {"indexes": None}, {"indexes": (1,)}):
        out = _narration_block(t, 0, "words", **kw)
        assert _summarize(out)[-1] == ("thinking", "data", {"phase": "end"}), kw
    out = _narration_block(t, 0, "", indexes=(0,))
    assert _summarize(out)[-1] == ("thinking", "data", {"phase": "end"})


def test_a_narration_frame_after_the_stop_changes_nothing():
    t = ClaudeCLIEventTranslator("s-narr4")
    t.feed(_se({"type": "content_block_start", "index": 0,
                "content_block": {"type": "thinking", "thinking": ""}}))
    t.feed(_se({"type": "content_block_stop", "index": 0}))
    assert t.feed(_thinking_frame("late")) == []
    out = t.feed(_se({"type": "content_block_start", "index": 0,
                      "content_block": {"type": "text", "text": ""}}))
    out += t.feed(_se({"type": "content_block_stop", "index": 0}))
    assert all(not (c.event_data or {}).get("note") for c in out)


def test_a_reset_drops_an_open_mark():
    for reset in ("reset_for_new_turn", "reset_for_settle", "reset_for_foreign_skip"):
        t = ClaudeCLIEventTranslator("s-narr5")
        t.feed(_se({"type": "content_block_start", "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""}}))
        t.feed(_thinking_frame("marked"))
        getattr(t, reset)()
        out = t.feed(_se({"type": "content_block_start", "index": 0,
                          "content_block": {"type": "thinking", "thinking": ""}}))
        out += t.feed(_se({"type": "content_block_stop", "index": 0}))
        assert _summarize(out)[-1] == ("thinking", "data", {"phase": "end"}), reset


def test_narration_reads_a_paragraph_apart_for_every_reader_that_joins_text():
    """The meeting turn, a task's result, the task stream and voice join the
    TEXT events: a note keeps its paragraph break from the text on either
    side, while has_emitted_text stays the answer's."""
    from core.events.common_events import TEXT
    from core.layers.cli.layer import cli_chunk_to_events
    t = ClaudeCLIEventTranslator("s-narr6")
    out = t.feed(_se({"type": "message_start", "message": {}}))
    out += t.feed(_se({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}))
    out += t.feed(_se({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "text_delta", "text": "I'll check the config."}}))
    out += t.feed(_se({"type": "content_block_stop", "index": 0}))
    out += _narration_block(t, 1, "Reading the settings file.")
    out += t.feed(_se({"type": "content_block_start", "index": 2,
                       "content_block": {"type": "tool_use", "id": "tu1", "name": "Read"}}))
    out += t.feed(_se({"type": "content_block_stop", "index": 2}))
    out += t.feed(_se({"type": "message_start", "message": {}}))
    out += _narration_block(t, 0, "Found the key.")
    out += t.feed(_se({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}))
    out += t.feed(_se({"type": "content_block_delta", "index": 1,
                       "delta": {"type": "text_delta", "text": "Here is the answer."}}))
    joined = "".join(e.data["content"] for c in out for e in cli_chunk_to_events(c) if e.type == TEXT)
    assert joined == ("I'll check the config.\n\nReading the settings file.\n\nFound the key."
                      "\n\nHere is the answer.")
    # A note alone never counts as the answer's text.
    t2 = ClaudeCLIEventTranslator("s-narr7")
    _narration_block(t2, 0, "Only a note.")
    assert t2.has_emitted_text is False
    t2.reset_for_new_turn()
    first = t2.feed(_se({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}))
    assert first == []


# --- the note mark: the text right before a tool call in its message --------

def _text_block(t, idx: int, text: str) -> list:
    out = t.feed(_se({"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}}))
    if text:
        out += t.feed(_se({"type": "content_block_delta", "index": idx,
                           "delta": {"type": "text_delta", "text": text}}))
    return out + t.feed(_se({"type": "content_block_stop", "index": idx}))


def _tool_block(t, idx: int, tool_id: str) -> list:
    out = t.feed(_se({"type": "content_block_start", "index": idx,
                      "content_block": {"type": "tool_use", "id": tool_id, "name": "Bash"}}))
    out += t.feed(_se({"type": "content_block_delta", "index": idx,
                       "delta": {"type": "input_json_delta", "partial_json": '{"command": "ls"}'}}))
    return out + t.feed(_se({"type": "content_block_stop", "index": idx}))


def _marks(chunks: list) -> list[int]:
    """The positions of the note mark among the chunks' events."""
    from core.events.common_events import is_note_mark
    from core.layers.cli.layer import cli_chunk_to_events
    events = [e for c in chunks for e in cli_chunk_to_events(c)]
    return [i for i, e in enumerate(events) if e.type == "text" and is_note_mark(e.data)]


def test_the_text_right_before_a_tool_call_is_marked_a_note_at_the_tool_start():
    from core.events.common_events import NOTE_MARK, TEXT, TOOL_USE
    from core.layers.cli.layer import cli_chunk_to_events
    t = ClaudeCLIEventTranslator("s-mark1")
    out = t.feed(_se({"type": "message_start", "message": {}}))
    out += _text_block(t, 0, "I'll list the folder.")
    out += _tool_block(t, 1, "tu1")
    events = [(e.type, e.data) for c in out for e in cli_chunk_to_events(c)]
    assert events[:3] == [(TEXT, {"content": "I'll list the folder."}), (TEXT, NOTE_MARK),
                          (TOOL_USE, {"name": "Bash", "tool_id": "tu1"})]
    # The real 5.5 shape of a later message: an empty thinking block, the
    # text (with its separator), then the call.
    out = t.feed({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1"}]}})
    out += t.feed(_se({"type": "message_start", "message": {}}))
    out += t.feed(_se({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}))
    out += t.feed(_se({"type": "content_block_stop", "index": 0}))
    out += _text_block(t, 1, "Now the count.")
    out += _tool_block(t, 2, "tu2")
    events = [(e.type, e.data) for c in out for e in cli_chunk_to_events(c)]
    at = events.index((TOOL_USE, {"name": "Bash", "tool_id": "tu2"}))
    assert events[at - 1] == (TEXT, NOTE_MARK)
    assert [d["content"] for k, d in events[:at - 1] if k == TEXT] == ["\n\n", "Now the count."]


def test_text_that_ends_its_message_stays_text_even_when_the_next_message_opens_with_a_tool():
    t = ClaudeCLIEventTranslator("s-mark2")
    out = t.feed(_se({"type": "message_start", "message": {}}))
    out += _text_block(t, 0, "Done, unless a check asks for more.")
    out += t.feed(_se({"type": "message_start", "message": {}}))
    out += _tool_block(t, 0, "tu1")
    assert _marks(out) == []


def test_a_final_answer_an_empty_text_block_and_an_ending_after_text_get_no_mark():
    t = ClaudeCLIEventTranslator("s-mark3")
    out = t.feed(_se({"type": "message_start", "message": {}}))
    out += _text_block(t, 0, "Here is the answer.")
    assert _marks(out) == []
    t.reset_for_new_turn()
    out = t.feed(_se({"type": "message_start", "message": {}}))
    out += _text_block(t, 0, "")
    out += _tool_block(t, 1, "tu1")
    assert _marks(out) == []
    t.reset_for_new_turn()
    out = t.feed(_se({"type": "message_start", "message": {}}))
    out += _text_block(t, 0, "\n")
    out += _tool_block(t, 1, "tu2")
    assert _marks(out) == []  # whitespace is not words
    t.reset_for_new_turn()
    out = t.feed(_se({"type": "message_start", "message": {}}))
    out += _text_block(t, 0, "Let me look.")
    out += t.feed(_se({"type": "message_delta", "delta": {"stop_reason": "refusal",
                                                          "stop_details": {"category": "cyber"}}}))
    assert _marks(out) == []


def test_parallel_calls_and_a_narration_before_a_call_mark_only_the_text():
    t = ClaudeCLIEventTranslator("s-mark4")
    out = t.feed(_se({"type": "message_start", "message": {}}))
    out += _text_block(t, 0, "Two checks at once.")
    out += _tool_block(t, 1, "tu1")
    out += _tool_block(t, 2, "tu2")
    assert len(_marks(out)) == 1
    out = t.feed(_se({"type": "message_start", "message": {}}))
    out += _narration_block(t, 0, "Read both. Next the run.")
    out += _tool_block(t, 1, "tu3")
    assert _marks(out) == []  # the narration is a note already

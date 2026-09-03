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

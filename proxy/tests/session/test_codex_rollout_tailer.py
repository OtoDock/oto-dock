"""Tests for core.session.codex_rollout_tailer — interactive Codex rollout → chat_messages.

Uses synthetic rollout JSONL matching the real on-disk shapes (verified against live
``rollout-*.jsonl``): outer ``{type, payload}``; ``session_meta`` carries ``id`` /
``cwd`` / ``originator`` / ``thread_source``; a turn message is ``response_item`` with
``payload.type=="message"``, ``payload.role`` (user|assistant|developer) and
``payload.content[]`` blocks of ``{type: input_text|output_text, text}``. Monkeypatches
the DB so it's DB-free. Covers filtering, thread-id capture, append-only re-tail (offset
cursor), discovery, and the resume prefix-merge (no duplicate history).
"""
import json
import os

import pytest

from core.session import codex_rollout_tailer as C


def _line(obj: dict) -> str:
    return json.dumps(obj) + "\n"


def _meta(tid: str, cwd: str = "/users/alice", originator: str = "codex-tui",
          thread_source: str = "user") -> str:
    return _line({"type": "session_meta", "payload": {
        "id": tid, "cwd": cwd, "originator": originator, "thread_source": thread_source}})


def _msg(role: str, text: str) -> str:
    block = "output_text" if role == "assistant" else "input_text"
    return _line({"type": "response_item", "payload": {
        "type": "message", "role": role, "content": [{"type": block, "text": text}]}})


# A realistic developer perms message + the synthetic AGENTS.md user injection.
_DEV = _msg("developer", "<permissions instructions>\nFilesystem sandboxing...")
_AGENTS = _msg("user", "# AGENTS.md instructions for /users/alice\n<INSTRUCTIONS>\n...")


class _Captured(list):
    """A list of (role, content) rows that also carries the ``update_chat``
    kwargs list as ``.updates`` — one fixture value exposing both (a plain list
    can't take an attribute). Pump-shaped event rows land in ``.events`` as
    (event_type, parsed event_data); ``.order`` records every row's insert
    order for interleaving assertions."""
    updates: list
    events: list
    order: list


@pytest.fixture(autouse=True)
def _capture(monkeypatch):
    rows = _Captured()
    rows.events = []
    rows.order = []
    updates = []
    import storage.database as db

    def _add(chat_id, role, content="", event_type="", event_data=""):
        if role == "event":
            rows.events.append((event_type, json.loads(event_data)))
            rows.order.append(("event", event_type))
        else:
            rows.append((role, content))
            rows.order.append((role, content))

    monkeypatch.setattr(db, "add_chat_message", _add)
    monkeypatch.setattr(db, "update_chat", lambda cid, **kw: updates.append(kw))
    monkeypatch.setattr(db, "get_chat", lambda cid: {"user_sub": "u", "title": "", "codex_thread_id": ""})
    monkeypatch.setattr(db, "get_chat_messages", lambda cid, limit=500: [])
    C._offsets.clear()
    C._resolved.clear()
    C._thread_saved.clear()
    C._tool_events.clear()
    C._usage_models.clear()
    from core.session import transcript_tool_events as _TE
    _TE._sent_prompts.clear()
    rows.updates = updates  # expose both via one fixture value
    return rows


def _evt(ptype: str, **payload) -> str:
    return _line({"type": "event_msg", "payload": {"type": ptype, **payload}})


def _write(tmp_path, *lines: str) -> str:
    p = tmp_path / "rollout-2026-06-15T00-00-00-tid.jsonl"
    p.write_text("".join(lines))
    return str(p)


def test_persists_user_and_assistant_filters_synthetic(tmp_path, _capture):
    path = _write(
        tmp_path,
        _meta("019ec-tid"),
        _DEV,                       # developer perms — filtered
        _AGENTS,                    # synthetic AGENTS.md user — filtered
        _msg("user", "check my emails"),
        _msg("assistant", "On it."),
        _msg("assistant", "Found 3 unread."),
    )
    stats = C.tail_rollout("s1", "c1", path)
    assert stats["persisted"] == 3
    assert _capture == [
        ("user", "check my emails"),
        ("assistant", "On it."),
        ("assistant", "Found 3 unread."),
    ]
    # Thread id captured from session_meta.id.
    assert {"codex_thread_id": "019ec-tid"} in _capture.updates


def test_turn_complete_signal_from_task_complete(tmp_path, _capture):
    # A finished turn writes event_msg/task_complete with last_agent_message; the
    # tailer reports turn_complete (+ the final text) so interactive_session can
    # fire the interactive-task completion (gated bg-empty + min-time there).
    path = _write(
        tmp_path,
        _meta("tidtc"),
        _msg("user", "do it"),
        _msg("assistant", "All done."),
        _evt("task_complete", last_agent_message="All done."),
    )
    stats = C.tail_rollout("stc", "ctc", path)
    assert stats["turn_complete"] is True
    assert stats["last_message"] == "All done."
    # The final message is still persisted (task_complete is just the marker).
    assert _capture == [("user", "do it"), ("assistant", "All done.")]


def test_no_turn_complete_mid_turn(tmp_path, _capture):
    # task_started but no task_complete yet → not a turn boundary; the watcher
    # must keep waiting (no false completion during a long turn).
    path = _write(
        tmp_path,
        _meta("tidmid"),
        _evt("task_started"),
        _msg("user", "do it"),
        _msg("assistant", "Working on it…"),
    )
    stats = C.tail_rollout("smid", "cmid", path)
    assert stats["turn_complete"] is False
    assert stats["last_message"] == ""


def test_turn_aborted_closes_turn_without_completion(tmp_path, _capture):
    # ESC / dashboard Stop: codex ends the turn with event_msg/turn_aborted
    # (no task_complete) plus a synthetic <turn_aborted> user message. The
    # turn must CLOSE (last_signal end_turn) with NO completion signal, and
    # the synthetic message must stay out of chat history.
    path = _write(
        tmp_path,
        _meta("tidab"),
        _evt("task_started"),
        _msg("user", "run the sleeps"),
        _msg("assistant", "2 completed."),
        _msg("user", "<turn_aborted>\nThe user interrupted the previous turn "
                     "on purpose. Any running unified exec processes may "
                     "still be running in the background."),
        _evt("turn_aborted", turn_id="t-1", reason="interrupted"),
    )
    stats = C.tail_rollout("sab", "cab", path)
    assert stats["last_signal"] == "end_turn"
    assert stats["turn_complete"] is False
    assert _capture == [("user", "run the sleeps"), ("assistant", "2 completed.")]


def test_skips_non_message_items(tmp_path, _capture):
    # Items with nothing to persist: an empty (encrypted-only) reasoning item,
    # event_msg markers, and unknown response_item types.
    path = _write(
        tmp_path,
        _meta("tid2"),
        _line({"type": "response_item", "payload": {"type": "reasoning", "content": []}}),
        _line({"type": "event_msg", "payload": {"type": "task_complete"}}),
        _line({"type": "response_item", "payload": {"type": "ghost_item"}}),
        _msg("user", "hi"),
    )
    C.tail_rollout("s2", "c2", path)
    assert _capture == [("user", "hi")]
    assert _capture.events == []


def test_idempotent_append_only_retail(tmp_path, _capture):
    p = tmp_path / "r.jsonl"
    p.write_text(_meta("tid3") + _msg("user", "first"))
    assert C.tail_rollout("s3", "c3", str(p))["persisted"] == 1
    # Re-tail, no new lines → nothing.
    assert C.tail_rollout("s3", "c3", str(p))["persisted"] == 0
    with open(p, "a") as fh:
        fh.write(_msg("assistant", "second"))
    assert C.tail_rollout("s3", "c3", str(p))["persisted"] == 1
    assert _capture == [("user", "first"), ("assistant", "second")]


def test_resume_prefix_merge_no_duplicates(tmp_path, monkeypatch, _capture):
    # The rollout holds the full history; the DB already has it (resume). The first
    # tail of the new session must skip the persisted prefix and add nothing.
    path = _write(
        tmp_path,
        _meta("tid4"),
        _msg("user", "q1"), _msg("assistant", "a1"),
        _msg("user", "q2"), _msg("assistant", "a2"),
    )
    import storage.database as db
    persisted = [("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2")]
    # get_chat_messages returns newest-first.
    monkeypatch.setattr(
        db, "get_chat_messages",
        lambda cid, limit=500: [{"role": r, "content": c} for r, c in reversed(persisted)],
    )
    res = C.tail_rollout("s4", "c4", path)
    assert res["persisted"] == 0
    assert _capture == []
    # A brand-new turn appended after resume IS persisted.
    with open(path, "a") as fh:
        fh.write(_msg("user", "q3"))
    assert C.tail_rollout("s4", "c4", path)["persisted"] == 1
    assert _capture == [("user", "q3")]


def test_resolve_discovers_codex_tui_rollout(tmp_path, monkeypatch, _capture):
    # CODEX_HOME with three rollouts: an app-server one, a subagent one, and the
    # interactive (codex-tui) one — resolve must pick only the codex-tui rollout.
    home = tmp_path / ".codex"
    day = home / "sessions" / "2026" / "06" / "15"
    day.mkdir(parents=True)
    (day / "rollout-app-server.jsonl").write_text(
        _meta("appsrv", originator="otodock", thread_source=None) + _msg("user", "x"))
    (day / "rollout-subagent.jsonl").write_text(
        _meta("sub", originator="codex-tui", thread_source="subagent") + _msg("user", "y"))
    target = day / "rollout-2026-06-15T10-00-00-thetid.jsonl"
    target.write_text(_meta("thetid", cwd="/users/alice") + _msg("user", "real"))

    monkeypatch.setattr(
        "core.session.session_state.get_session_codex_dir",
        lambda sid: {"home": str(home), "cwd": "/users/alice", "started_at": 0.0},
    )
    resolved = C.resolve_rollout_path("s5", "c5")
    assert resolved == str(target)
    # Pinned: a second call returns the same path without re-scanning.
    assert C.resolve_rollout_path("s5", "c5") == str(target)


def test_resume_resolves_by_thread_id(tmp_path, monkeypatch, _capture):
    home = tmp_path / ".codex"
    day = home / "sessions" / "2026" / "06" / "15"
    day.mkdir(parents=True)
    # An unrelated newer codex-tui rollout + the resumed thread's own (older) rollout.
    newer = day / "rollout-2026-06-15T12-00-00-other.jsonl"
    newer.write_text(_meta("other", cwd="/users/alice") + _msg("user", "sibling"))
    owned = day / "rollout-2026-06-15T09-00-00-mythread.jsonl"
    owned.write_text(_meta("mythread", cwd="/users/alice") + _msg("user", "mine"))
    os.utime(newer, (2_000_000_000, 2_000_000_000))  # make the sibling strictly newer
    os.utime(owned, (1_000_000_000, 1_000_000_000))

    import storage.database as db
    monkeypatch.setattr(
        db, "get_chat",
        lambda cid: {"user_sub": "u", "title": "", "codex_thread_id": "mythread"},
    )
    monkeypatch.setattr(
        "core.session.session_state.get_session_codex_dir",
        lambda sid: {"home": str(home), "cwd": "/users/alice", "started_at": 0.0},
    )
    # Resume prefers the thread's own rollout by id even though another is newer.
    assert C.resolve_rollout_path("s6", "c6") == str(owned)


def test_last_signal_batch_boundary_user_after_task_complete(tmp_path, _capture):
    """[task_complete, user new-prompt] in one forwarded batch still reports
    turn_complete=True, but last_signal MUST be "user" — a new turn is opening,
    so turn-open consumers (prompt-injection gates) must not read it as idle."""
    path = _write(
        tmp_path,
        _meta("tidls1"),
        _msg("assistant", "Done."),
        _evt("task_complete", last_agent_message="Done."),
        _msg("user", "next thing please"),
    )
    stats = C.tail_rollout("sls1", "cls1", path)
    assert stats["turn_complete"] is True
    assert stats["last_signal"] == "user"


def test_last_signal_end_turn_and_mid_turn(tmp_path, _capture):
    path = _write(
        tmp_path,
        _meta("tidls2"),
        _msg("user", "go"),
        _msg("assistant", "Finished."),
        _evt("task_complete", last_agent_message="Finished."),
    )
    assert C.tail_rollout("sls2", "cls2", path)["last_signal"] == "end_turn"

    p2 = tmp_path / "r-mid.jsonl"
    p2.write_text(_meta("tidls3") + _msg("user", "go") + _msg("assistant", "Working…"))
    assert C.tail_rollout("sls3", "cls3", str(p2))["last_signal"] == "tool_use"


def test_last_signal_none_for_non_turn_batch(tmp_path, _capture):
    # session_meta / reasoning items aren't turn-relevant — last_signal stays None.
    path = _write(
        tmp_path,
        _meta("tidls4"),
        _line({"type": "response_item", "payload": {"type": "reasoning", "content": []}}),
    )
    assert C.tail_rollout("sls4", "cls4", path)["last_signal"] is None


def test_title_from_prompt_strips_injected_time_prelude():
    # Twin of transcript_tailer: injected stamps never become the title, and a
    # prelude-only prompt returns "" (chat stays untitled for the next prompt).
    stamp = "[Current time: Tuesday, July 07, 2026 19:31 (7:31 PM) Europe/Athens (UTC+03:00)]\n\n"
    assert C._title_from_prompt(stamp + "Fix the flaky login test") == "Fix the flaky login test"
    assert C._title_from_prompt(stamp + stamp + "Fix it") == "Fix it"
    assert C._title_from_prompt(stamp.strip()) == ""


# ---------------------------------------------------------------------------
# Tool-event persistence — pump-shaped rows (headless parity)
# ---------------------------------------------------------------------------

def _fn_call(name: str, call_id: str, args: dict) -> str:
    return _line({"type": "response_item", "payload": {
        "type": "function_call", "name": name, "call_id": call_id,
        "id": "fc_" + call_id, "arguments": json.dumps(args)}})


def _fn_out(call_id: str, output: str) -> str:
    return _line({"type": "response_item", "payload": {
        "type": "function_call_output", "call_id": call_id, "output": output}})


def test_exec_command_pair_persists_bash_row(tmp_path, _capture):
    path = _write(
        tmp_path,
        _meta("tt1"),
        _msg("user", "list the workspace"),
        _fn_call("exec_command", "call_1", {"cmd": "ls -la /users/alice", "workdir": "/users/alice"}),
        _fn_out("call_1", "total 8\ndrwxr-xr-x notes"),
        _msg("assistant", "Two entries."),
    )
    stats = C.tail_rollout("st1", "ct1", path)
    assert stats["persisted"] == 3
    assert _capture.order == [
        ("user", "list the workspace"),
        ("event", "tool"),
        ("assistant", "Two entries."),
    ]
    (etype, evt), = _capture.events
    assert evt == {
        "type": "tool", "name": "Bash", "tool_id": "call_1",
        "summary": "ls -la /users/alice", "active": False,
        "tool_input": {"cmd": "ls -la /users/alice", "workdir": "/users/alice"},
        "tool_result": "total 8\ndrwxr-xr-x notes", "result_summary": "2 lines",
        "is_error": False,
    }


def test_tool_pair_across_batches_and_dedupe(tmp_path, _capture):
    p = tmp_path / "r.jsonl"
    p.write_text(_meta("tt2") + _fn_call("get_goal", "call_g", {}))
    assert C.tail_rollout("st2", "ct2", str(p))["persisted"] == 0
    with open(p, "a") as fh:
        fh.write(_fn_out("call_g", '{"goal":null}'))
    assert C.tail_rollout("st2", "ct2", str(p))["persisted"] == 1
    assert C.tail_rollout("st2", "ct2", str(p))["persisted"] == 0
    assert [e for e, _ in _capture.events] == ["tool"]


def test_update_plan_persists_todowrite_snapshot(tmp_path, _capture):
    path = _write(
        tmp_path,
        _meta("tt3"),
        _fn_call("update_plan", "call_p", {"plan": [
            {"step": "Inspect workspace", "status": "in_progress"},
            {"step": "Create scaffold", "status": "inProgress"},
            {"step": "Verify", "status": "someday"},
        ]}),
        _fn_out("call_p", "Plan updated"),
    )
    C.tail_rollout("st3", "ct3", path)
    (etype, evt), = _capture.events  # the output is dropped — one row only
    assert (etype, evt["name"]) == ("tool", "TodoWrite")
    assert evt["tool_input"]["todos"] == [
        {"content": "Inspect workspace", "status": "in_progress"},
        {"content": "Create scaffold", "status": "in_progress"},
        {"content": "Verify", "status": "pending"},
    ]


def test_delegate_call_is_suppressed(tmp_path, _capture):
    # The delegation endpoint persists its own delegate_spawn row — the raw MCP
    # call must not double-render (headless _SKIP_TOOL_PERSIST parity).
    path = _write(
        tmp_path,
        _meta("tt4"),
        _fn_call("delegate", "call_d", {"name": "job", "prompt": "do it"}),
        _fn_out("call_d", "Delegated 'job'"),
    )
    C.tail_rollout("st4", "ct4", path)
    assert _capture.events == []


def test_custom_tool_call_apply_patch_summary(tmp_path, _capture):
    patch = ("*** Begin Patch\n*** Add File: workspace/notes/README.md\n+# Notes\n"
             "*** Update File: workspace/notes/index.md\n+x\n*** End Patch")
    path = _write(
        tmp_path,
        _meta("tt5"),
        _line({"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "apply_patch",
            "call_id": "call_ap", "id": "ctc_1", "input": patch}}),
        _line({"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "call_ap",
            "output": "patch applied"}}),
    )
    C.tail_rollout("st5", "ct5", path)
    (_, evt), = _capture.events
    assert evt["name"] == "apply_patch"
    assert evt["summary"] == "README.md, index.md"
    assert evt["tool_result"] == "patch applied"


def test_tool_result_truncation_mirrors_hook_policy(tmp_path, _capture):
    big = "\n".join(f"line {i}" for i in range(600))
    path = _write(
        tmp_path,
        _meta("tt6"),
        _fn_call("exec_command", "call_big", {"cmd": "cat big"}),
        _fn_out("call_big", big),
    )
    C.tail_rollout("st6", "ct6", path)
    result = _capture.events[0][1]["tool_result"]
    assert result.endswith("... (100 more lines)")
    assert "line 499" in result and "line 500" not in result


def test_reasoning_summary_persists_as_thinking(tmp_path, _capture):
    path = _write(
        tmp_path,
        _meta("tt7"),
        _line({"type": "response_item", "payload": {
            "type": "reasoning", "id": "rs_1",
            "summary": [{"type": "summary_text", "text": "I should check the file."}],
            "encrypted_content": "gAAAA..."}}),
        _line({"type": "response_item", "payload": {
            "type": "reasoning", "id": "rs_2", "summary": [],
            "encrypted_content": "gAAAB..."}}),  # nothing readable — skipped
        _msg("assistant", "Checked."),
    )
    stats = C.tail_rollout("st7", "ct7", path)
    assert stats["persisted"] == 2
    assert _capture.events == [
        ("thinking", {"type": "thinking", "content": "I should check the file."}),
    ]
    assert _capture.order[0] == ("event", "thinking")  # before the text row


def test_tool_search_and_web_search_rows(tmp_path, _capture):
    path = _write(
        tmp_path,
        _meta("tt8"),
        _line({"type": "response_item", "payload": {
            "type": "tool_search_call", "call_id": "call_ts", "id": "tsc_1",
            "arguments": {"query": "delegation tools", "limit": 5}}}),
        _line({"type": "response_item", "payload": {
            "type": "tool_search_output", "call_id": "call_ts",
            "tools": [{"type": "namespace", "name": "mcp__delegation_mcp"}]}}),
        _line({"type": "response_item", "payload": {
            "type": "web_search_call", "id": "ws_1",
            "action": {"type": "search", "query": "otodock docs"}}}),
    )
    C.tail_rollout("st8", "ct8", path)
    assert [(e, d["name"]) for e, d in _capture.events] == [
        ("tool", "ToolSearch"), ("tool", "web_search")]
    ts, ws = (d for _, d in _capture.events)
    assert ts["summary"] == "delegation tools"
    assert "mcp__delegation_mcp" in ts["tool_result"]
    assert ws["summary"] == "otodock docs"
    assert "tool_result" not in ws  # server-side tool — no output item exists


def test_first_tail_event_prefix_skips_persisted_rows(tmp_path, monkeypatch, _capture):
    # Post-restart re-read from line 0: text rows merge against the prefix, and
    # already-persisted tool/thinking rows are skipped by the event-key backstop.
    path = _write(
        tmp_path,
        _meta("tt9"),
        _msg("user", "q1"),
        _line({"type": "response_item", "payload": {
            "type": "reasoning", "id": "rs_9",
            "summary": [{"type": "summary_text", "text": "old thought"}]}}),
        _fn_call("exec_command", "call_old", {"cmd": "ls"}),
        _fn_out("call_old", "notes"),
        _msg("assistant", "a1"),
    )
    import storage.database as db
    db_rows = [  # chronological; get_chat_messages returns newest-first
        {"role": "user", "content": "q1"},
        {"role": "event", "event_type": "thinking",
         "event_data": json.dumps({"type": "thinking", "content": "old thought"})},
        {"role": "event", "event_type": "tool",
         "event_data": json.dumps({"type": "tool", "name": "Bash", "tool_id": "call_old"})},
        {"role": "assistant", "content": "a1"},
    ]
    monkeypatch.setattr(db, "get_chat_messages",
                        lambda cid, limit=500: list(reversed(db_rows)))
    res = C.tail_rollout("st9", "ct9", path)
    assert res["persisted"] == 0
    assert _capture == [] and _capture.events == []


def test_concurrent_tails_do_not_duplicate_rows(tmp_path, _capture, monkeypatch):
    # Twin of the transcript_tailer race test: the per-session tail lock keeps
    # overlapping worker-thread tails from persisting the same slice twice.
    import threading
    import storage.database as db

    entered = threading.Event()
    release = threading.Event()
    rows = []

    def _slow_add(chat_id, role, content="", event_type="", event_data=""):
        rows.append((role, content))
        entered.set()
        release.wait(timeout=5)

    monkeypatch.setattr(db, "add_chat_message", _slow_add)
    path = _write(tmp_path, _meta("ttrace"), _msg("user", "only once please"))

    t1 = threading.Thread(target=C.tail_rollout, args=("st-race", "ct-race", path))
    t1.start()
    entered_ok = entered.wait(timeout=5)
    assert entered_ok  # t1 is inside the locked persist
    t2 = threading.Thread(target=C.tail_rollout, args=("st-race", "ct-race", path))
    t2.start()
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert rows == [("user", "only once please")]


def test_orphaned_pending_call_drops_on_forget(tmp_path, _capture):
    path = _write(
        tmp_path,
        _meta("tt10"),
        _fn_call("exec_command", "call_hang", {"cmd": "sleep 999"}),
    )
    C.tail_rollout("st10", "ct10", path)
    C.forget("st10")
    assert _capture.events == []
    assert "st10" not in C._tool_events


# ---------------------------------------------------------------------------
# request_user_input — question row + turn park (Claude AskUserQuestion parity)
# ---------------------------------------------------------------------------

_Q_ARGS = {"questions": [{"header": "Preference", "id": "pref",
                          "question": "Which option?",
                          "options": [{"label": "Alpha", "description": "a"},
                                      {"label": "Beta", "description": "b"}]}]}


def test_question_pending_parks_turn(tmp_path, _capture):
    # An unanswered request_user_input at batch end folds to a turn close with
    # question_pending — the TUI is blocked on the picker.
    path = _write(
        tmp_path,
        _meta("tq1"),
        _msg("user", "set it up"),
        _fn_call("request_user_input", "call_q1", _Q_ARGS),
    )
    stats = C.tail_rollout("sq1", "cq1", path)
    assert stats["question_pending"] is True
    assert stats["last_signal"] == "end_turn"
    assert stats["turn_complete"] is False
    (etype, evt), = _capture.events
    assert etype == "question"
    assert evt == {"type": "question", "tool_name": "request_user_input",
                   "tool_input": _Q_ARGS, "tool_id": "call_q1"}


def test_question_answered_in_batch_no_fold(tmp_path, _capture):
    # Fire + answer + completion in one batch: the question row persists, the
    # answer output attaches nothing, and the turn completes normally.
    path = _write(
        tmp_path,
        _meta("tq2"),
        _msg("user", "set it up"),
        _fn_call("request_user_input", "call_q2", _Q_ARGS),
        _fn_out("call_q2", '{"answers":{"pref":{"answers":["Alpha"]}}}'),
        _msg("assistant", "Alpha it is."),
        _evt("task_complete", last_agent_message="Alpha it is."),
    )
    stats = C.tail_rollout("sq2", "cq2", path)
    assert stats["question_pending"] is False
    assert stats["turn_complete"] is True
    assert [e for e, _ in _capture.events] == ["question"]
    assert ("assistant", "Alpha it is.") in _capture


def test_question_then_turn_aborted_no_fold(tmp_path, _capture):
    # ESC on the picker: turn_aborted lands after the call — the fold must not
    # override the real close (no "needs your input" ping for a dismissal).
    path = _write(
        tmp_path,
        _meta("tq3"),
        _fn_call("request_user_input", "call_q3", _Q_ARGS),
        _evt("turn_aborted", reason="interrupted"),
    )
    stats = C.tail_rollout("sq3", "cq3", path)
    assert stats["question_pending"] is False
    assert stats["last_signal"] == "end_turn"
    assert stats["turn_complete"] is False


def test_question_answer_only_batch_keeps_none_signal(tmp_path, _capture):
    # Batch N parks; batch N+1 carries ONLY the answer output — it must not
    # re-park (question_pending False) nor fabricate a turn signal.
    p = tmp_path / "r.jsonl"
    p.write_text(_meta("tq4") + _fn_call("request_user_input", "call_q4", _Q_ARGS))
    stats = C.tail_rollout("sq4", "cq4", str(p))
    assert stats["question_pending"] is True
    with open(p, "a") as fh:
        fh.write(_fn_out("call_q4", '{"answers":{}}'))
    stats = C.tail_rollout("sq4", "cq4", str(p))
    assert stats["question_pending"] is False
    assert stats["last_signal"] is None
    assert [e for e, _ in _capture.events] == ["question"]


def test_question_row_dedupes_on_restart_reread(tmp_path, monkeypatch, _capture):
    # Post-restart re-read from line 0 with the question still unanswered: the
    # known_events backstop skips the persisted row, the park is restored.
    import storage.database as db
    path = _write(
        tmp_path,
        _meta("tq5"),
        _msg("user", "set it up"),
        _fn_call("request_user_input", "call_q5", _Q_ARGS),
    )
    monkeypatch.setattr(db, "get_chat_messages", lambda cid, limit=500: [
        {"role": "event", "event_type": "question", "event_data": json.dumps(
            {"type": "question", "tool_name": "request_user_input",
             "tool_input": _Q_ARGS, "tool_id": "call_q5"})},
        {"role": "user", "content": "set it up"},
    ])
    stats = C.tail_rollout("sq5", "cq5", path)
    assert stats["question_pending"] is True
    assert stats["persisted"] == 0
    assert _capture.events == []


# ---------------------------------------------------------------------------
# Usage accounting — token_count events → usage_records (the Codex twin of
# the Claude tailer's message.usage accounting; shared flush in
# transcript_tool_events.record_batch_usage). last_token_usage is the per-API-
# call breakdown (OpenAI semantics: input_tokens INCLUDES cached_input_tokens);
# total_token_usage is cumulative and only keys the per-event claim.
# ---------------------------------------------------------------------------


def _token_count(ts: str, total: int, inp: int, cached: int, out: int) -> str:
    return _line({"timestamp": ts, "type": "event_msg", "payload": {
        "type": "token_count", "info": {
            "total_token_usage": {"input_tokens": total - out, "cached_input_tokens": 0,
                                  "output_tokens": out, "reasoning_output_tokens": 0,
                                  "total_tokens": total},
            "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                 "output_tokens": out, "reasoning_output_tokens": 0,
                                 "total_tokens": inp + out},
            "model_context_window": 258400,
        }}})


def _turn_ctx(model: str) -> str:
    return _line({"type": "turn_context", "payload": {"model": model, "cwd": "/x"}})


@pytest.fixture
def _usage_env(monkeypatch):
    """Pin every external the usage path touches — captured rows instead of DB
    writes, a fixed pool binding, deterministic pricing, no agent-config reads."""
    rows: list[dict] = []
    from services.billing import usage_service
    from services.engines import subscription_pool
    from core.session import visibility
    import config

    def _record(batch):
        rows.extend(batch)
        return list(range(len(batch)))

    monkeypatch.setattr(usage_service, "record_turn_usage", _record)
    monkeypatch.setattr(subscription_pool, "get_session_subscription",
                        lambda sid: "sub-codex")
    monkeypatch.setattr(visibility, "is_shared_only", lambda agent: False)
    monkeypatch.setattr(config, "get_model_pricing",
                        lambda m, p="": (10.0, 50.0, 12.5, 1.0))
    monkeypatch.setattr(config, "get_model_provider", lambda m: "openai")
    return rows


def test_usage_sums_last_token_usage_with_cache_split(tmp_path, _capture, _usage_env):
    # Two API calls in one batch: input column = non-cached input, cache_read =
    # cached (headless translator parity), summed across events; model from
    # turn_context. The session was seeked at attach (offset present), so this
    # is a live tail, not the calibrate-only first re-read.
    path = _write(
        tmp_path,
        _turn_ctx("gpt-5.5"),
        _token_count("t1", 14910, 14704, 10112, 206),
        _token_count("t2", 30130, 15097, 14208, 123),
    )
    C._offsets["su1"] = 0
    stats = C.tail_rollout("su1", "cu1", path)
    assert stats["usage_rows"] == 1
    (row,) = _usage_env
    assert row["input_tokens"] == (14704 - 10112) + (15097 - 14208)
    assert row["cache_read"] == 10112 + 14208
    assert row["output_tokens"] == 206 + 123
    assert row["cache_write"] == 0
    assert row["model"] == "gpt-5.5" and row["provider"] == "openai"
    assert row["source_key"] == "sub-codex"
    # (5481*10 + 329*50 + 24320*1.0) / 1e6 with the pinned pricing
    assert row["cost_usd"] == pytest.approx(0.09558)


def test_usage_duplicate_event_claims_once(_capture, _usage_env):
    # A re-delivered token_count line (same timestamp + cumulative total) must
    # not double-count — satellite path modeled via tail_lines.
    tc = _token_count("t1", 14910, 14704, 10112, 206)
    s1 = C.tail_lines("su2", "cu2", [tc])
    s2 = C.tail_lines("su2", "cu2", [tc])
    assert s1["usage_rows"] == 1 and s2["usage_rows"] == 0
    assert len(_usage_env) == 1


def test_usage_first_tail_reread_calibrates_without_recording(tmp_path, _capture,
                                                              _usage_env):
    # Post-restart: no offset for the session → the first tail re-reads from
    # line 0. Those events were recorded pre-restart — recording the replay
    # would double-count the whole session. New events on the NEXT tail record
    # normally.
    p = tmp_path / "rollout-2026-06-15T00-00-00-tid.jsonl"
    p.write_text(_meta("t-cal") + _turn_ctx("gpt-5.5")
                 + _token_count("t1", 14910, 14704, 10112, 206))
    stats = C.tail_rollout("su3", "cu3", str(p))
    assert stats["usage_rows"] == 0 and _usage_env == []

    with open(p, "a") as fh:
        fh.write(_token_count("t2", 30130, 15097, 14208, 123))
    stats = C.tail_rollout("su3", "cu3", str(p))
    assert stats["usage_rows"] == 1
    (row,) = _usage_env
    assert row["input_tokens"] == 15097 - 14208  # only the NEW event
    assert row["model"] == "gpt-5.5"  # replayed turn_context still names it


def test_usage_model_falls_back_to_chat_row(_capture, _usage_env, monkeypatch):
    # No turn_context seen (e.g. remote session attached mid-turn after a proxy
    # restart): the chat row's model column names the row.
    import storage.database as db
    monkeypatch.setattr(db, "get_chat", lambda cid: {
        "user_sub": "u", "title": "t", "codex_thread_id": "",
        "agent": "dev", "model": "gpt-5-codex", "source_type": "chat"})
    C.tail_lines("su4", "cu4", [_token_count("t1", 100, 80, 0, 20)])
    (row,) = _usage_env
    assert row["model"] == "gpt-5-codex"


def test_usage_no_token_count_no_rows(tmp_path, _capture, _usage_env):
    path = _write(
        tmp_path,
        _meta("t-none"),
        _msg("user", "hi"),
        _msg("assistant", "hello"),
        _evt("task_complete", last_agent_message="hello"),
    )
    C._offsets["su5"] = 0
    stats = C.tail_rollout("su5", "cu5", path)
    assert stats["usage_rows"] == 0 and _usage_env == []


# ---------------------------------------------------------------------------
# Code-mode exec (Codex 0.144+) — commands arrive as JS scripts calling
# tools.exec_command({...}) under a custom tool named "exec". Shapes below
# are verbatim from a live 0.144.1 rollout.
# ---------------------------------------------------------------------------


_CODE_MODE_INPUT = ('const r = await tools.exec_command({"cmd":"echo smoke-5-6-ok",'
                    '"workdir":"/users/dev-admin","yield_time_ms":10000,'
                    '"max_output_tokens":1000});\ntext(r.output);\n')


def _custom_call(name: str, call_id: str, inp: str) -> str:
    return _line({"type": "response_item", "payload": {
        "type": "custom_tool_call", "name": name, "call_id": call_id,
        "input": inp}})


def _custom_out(call_id: str, output) -> str:
    return _line({"type": "response_item", "payload": {
        "type": "custom_tool_call_output", "call_id": call_id,
        "output": output}})


def test_code_mode_single_exec_renders_as_bash(tmp_path, _capture):
    path = _write(
        tmp_path,
        _meta("t-cm"),
        _custom_call("exec", "call_cm1", _CODE_MODE_INPUT),
        _custom_out("call_cm1", [
            {"type": "input_text", "text": "Script completed\nWall time 0.3 seconds\nOutput:\n"},
            {"type": "input_text", "text": "smoke-5-6-ok\n"}]),
    )
    C.tail_rollout("s-cm1", "c-cm1", path)
    (etype, evt), = _capture.events
    assert (etype, evt["name"]) == ("tool", "Bash")
    assert evt["summary"] == "echo smoke-5-6-ok"


def test_code_mode_multi_call_script_stays_generic_exec(tmp_path, _capture):
    # A script composing several tool calls is NOT one shell command — keep
    # the honest "exec" card with the script as input.
    script = ('const a = await tools.exec_command({"cmd":"ls"});\n'
              'const b = await tools.read_file({"path":"x"});\ntext(a.output);\n')
    path = _write(
        tmp_path,
        _meta("t-cm2"),
        _custom_call("exec", "call_cm2", script),
        _custom_out("call_cm2", "done"),
    )
    C.tail_rollout("s-cm2", "c-cm2", path)
    (etype, evt), = _capture.events
    assert evt["name"] == "exec"


def test_code_mode_unparseable_script_stays_generic(tmp_path, _capture):
    path = _write(
        tmp_path,
        _meta("t-cm3"),
        _custom_call("exec", "call_cm3", "await tools.exec_command(buildArgs());"),
        _custom_out("call_cm3", "done"),
    )
    C.tail_rollout("s-cm3", "c-cm3", path)
    (etype, evt), = _capture.events
    assert evt["name"] == "exec"


def test_multi_agent_spawn_pair_renders_task_card(tmp_path, _capture):
    # Ultra / multi-agent v2: spawn_agent keeps its wire name (it's Codex's
    # own orchestration, not the platform delegate) with the task as summary.
    path = _write(
        tmp_path,
        _meta("t-ma1"),
        _fn_call("spawn_agent", "call_ma1",
                 {"task_name": "fix-flaky-tests",
                  "message": "Find and fix the flaky login test."}),
        _fn_out("call_ma1", '{"task_name":"root/fix-flaky-tests"}'),
    )
    C.tail_rollout("s-ma1", "c-ma1", path)
    (etype, evt), = _capture.events
    assert (etype, evt["name"]) == ("tool", "spawn_agent")
    assert evt["summary"] == "fix-flaky-tests"


def test_multi_agent_send_message_summarizes_message(tmp_path, _capture):
    path = _write(
        tmp_path,
        _meta("t-ma2"),
        _fn_call("send_message", "call_ma2",
                 {"recipient": "root/fix-flaky-tests", "message": "status?"}),
        _fn_out("call_ma2", "delivered"),
    )
    C.tail_rollout("s-ma2", "c-ma2", path)
    (etype, evt), = _capture.events
    assert evt["name"] == "send_message"
    assert evt["summary"] == "status?"


def test_send_persisted_prompt_is_skipped_once_codex(tmp_path, _capture):
    # Codex twin of the Claude tailer's consume — the warmup's send-time row
    # must not be re-inserted when the rollout journals the same prompt.
    from core.session import transcript_tool_events as TE
    prompt = "[Current time: Friday, July 10, 2026 22:30 (10:30 PM) UTC]\n\nsmoke it"
    TE.note_sent_prompt("c-note-cx", prompt)
    path = _write(
        tmp_path,
        _meta("t-note"),
        _msg("user", prompt),
        _msg("assistant", "on it"),
        _evt("task_complete", last_agent_message="on it"),
    )
    stats = C.tail_rollout("s-note-cx", "c-note-cx", path)
    assert _capture == [("assistant", "on it")]
    assert stats["persisted"] == 1
    # One-shot: a later identical prompt persists normally.
    p2 = tmp_path / "r2.jsonl"
    p2.write_text(_meta("t-note2") + _msg("user", prompt))
    C.tail_rollout("s-note-cx2", "c-note-cx", str(p2))
    assert ("user", prompt) in _capture


# ─────────────────────────── compaction (compacted) ─────────────────────────


def test_compacted_item_flags_and_persists_separator(tmp_path, _capture):
    path = _write(
        tmp_path,
        _meta("t-cmp"),
        _line({"type": "compacted", "payload": {"message": "compacted"}}),
    )
    stats = C.tail_rollout("s-cmp-cx", "c-cmp-cx", path)
    assert stats["compacted"] is True
    assert stats["persisted"] == 1
    assert _capture.events[0][1]["subtype"] == "context_compressed"
    assert stats["last_signal"] is None


def test_no_compaction_flag_on_plain_batches_codex(tmp_path, _capture):
    path = _write(tmp_path, _meta("t-nc"), _msg("user", "hello"))
    assert C.tail_rollout("s-nc-cx", "c-nc-cx", path)["compacted"] is False


def test_manual_compact_closes_turn_via_task_complete(tmp_path, _capture):
    # Probed on codex 0.144.1 (2026-07-12): a manual /compact journals
    # task_started → compacted → context_compacted → task_complete. The
    # task_complete folds to end_turn, so a stale-open turn closes through
    # the normal signal path — codex needs no manual-trigger twin of the
    # Claude compact_boundary close.
    path = _write(
        tmp_path,
        _meta("t-mcp"),
        _line({"type": "event_msg", "payload": {"type": "task_started"}}),
        _line({"type": "compacted", "payload": {"message": "compacted"}}),
        _line({"type": "event_msg", "payload": {"type": "context_compacted"}}),
        _line({"type": "event_msg", "payload": {"type": "task_complete"}}),
    )
    stats = C.tail_rollout("s-mcp-cx", "c-mcp-cx", path)
    assert stats["compacted"] is True
    assert stats["last_signal"] == "end_turn"


# ---------------------------------------------------------------------------
# CLI-reported error events → subscription throttle hook
# ---------------------------------------------------------------------------


def test_error_event_triggers_limit_hook(tmp_path, _capture, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "services.engines.subscription_pool.throttle_from_cli_error",
        lambda sid, text: calls.append((sid, text)))
    path = _write(
        tmp_path, _meta("t-lim"),
        _evt("error", message="You've hit your usage limit."),
    )
    C.tail_rollout("s-lim-cx", "c-lim-cx", path)
    assert calls == [("s-lim-cx", "You've hit your usage limit.")]


def test_assistant_prose_never_triggers_hook_codex(tmp_path, _capture, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "services.engines.subscription_pool.throttle_from_cli_error",
        lambda sid, text: calls.append((sid, text)))
    path = _write(
        tmp_path, _meta("t-prose"),
        _msg("assistant", "your usage limit handling code has a bug"),
    )
    C.tail_rollout("s-prose-cx", "c-prose-cx", path)
    assert calls == []

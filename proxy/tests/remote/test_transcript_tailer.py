"""Tests for core.session.transcript_tailer — interactive transcript → chat_messages.

Uses a synthetic Claude-format JSONL (the shapes verified against real on-disk
transcripts: user.content as str, assistant.content as a block list, tool_use
with name="Task"/"Agent", tool_result inside a user message, thinking blocks,
isMeta/command-wrapper noise lines). Monkeypatches the DB write so it's DB-free.
Covers append-only re-tail (offset cursor), the registry spawn→done feed, the
pump-shaped tool/thinking/task_spawn event rows (pairing across batches, dedupe,
truncation, skip sets) and the slash-command noise filter.
"""
import json

import pytest

from core.session import transcript_tailer as T
from core.session.session_state import get_subagent_registry


def _line(obj: dict) -> str:
    return json.dumps(obj) + "\n"


def _user(text: str) -> str:
    return _line({"type": "user", "uuid": text[:8],
                  "message": {"role": "user", "content": text}})


def _assistant(blocks: list) -> str:
    return _line({"type": "assistant", "uuid": "a" + str(len(blocks)),
                  "message": {"role": "assistant", "content": blocks}})


def _assistant_sr(blocks: list, stop_reason: str) -> str:
    """An assistant message with an explicit stop_reason ('end_turn' = turn
    boundary, 'tool_use' = more tools coming)."""
    return _line({"type": "assistant", "uuid": "as",
                  "message": {"role": "assistant", "content": blocks,
                              "stop_reason": stop_reason}})


def _tool_result(tool_use_id: str, content="done", is_error: bool = False) -> str:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return _line({"type": "user", "uuid": "r" + tool_use_id,
                  "message": {"role": "user", "content": [block]}})


class _Captured(list):
    """(role, content) rows for user/assistant text; pump-shaped event rows land
    in ``.events`` as (event_type, parsed event_data) so text assertions stay
    stable while event tests inspect the exact persisted block. ``.order`` keeps
    every row's insert order — (role, content) or ("event", event_type) — for
    interleaving assertions."""
    events: list
    order: list


@pytest.fixture(autouse=True)
def _capture(monkeypatch):
    rows = _Captured()
    rows.events = []
    rows.order = []
    import storage.database as db

    def _add(chat_id, role, content="", event_type="", event_data=""):
        if role == "event":
            rows.events.append((event_type, json.loads(event_data)))
            rows.order.append(("event", event_type))
        else:
            rows.append((role, content))
            rows.order.append((role, content))

    monkeypatch.setattr(db, "add_chat_message", _add)
    # Title backfill reads the chat + may update it — mock both so the tests stay
    # DB-free. Default: an untitled chat (so the title path runs harmlessly);
    # the title test re-patches get_chat to assert the backfill.
    monkeypatch.setattr(db, "get_chat", lambda cid: {"user_sub": "u", "title": ""})
    monkeypatch.setattr(db, "update_chat", lambda cid, **kw: None)
    # Fresh cursor + pairing + replay-guard state each test.
    T._offsets.clear()
    T._tool_events.clear()
    T._attach_ts.clear()
    from core.session import transcript_tool_events as _TE
    _TE._sent_prompts.clear()
    return rows


def _write(tmp_path, *lines: str):
    p = tmp_path / "transcript.jsonl"
    p.write_text("".join(lines))
    return str(p)


def test_persists_user_and_assistant_text(tmp_path, _capture):
    path = _write(
        tmp_path,
        _user("hello there"),
        _assistant([{"type": "text", "text": "hi back"}]),
    )
    stats = T.tail_transcript("s1", "c1", path)
    assert stats["persisted"] == 2
    assert _capture == [("user", "hello there"), ("assistant", "hi back")]


def test_server_submitted_first_prompt_not_duplicated(tmp_path, _capture):
    # An interactive cold send persists the RAW prompt at send-time and the
    # server submits the STAMPED variant into the PTY; dashboard_warmup
    # re-notes the STAMPED bytes because the tailer's duplicate-skip matches
    # the journaled user line byte-for-byte (it does NOT strip the
    # [Current time:] prelude before consume) — without the re-note the first
    # user row would land twice.
    from core.session.transcript_tool_events import note_sent_prompt
    stamped = ("[Current time: Friday, July 17, 2026 02:00 (2:00 AM) "
               "UTC (UTC+00:00)]\n\ncold prompt")
    note_sent_prompt("c1", "cold prompt")  # _persist_first_prompt (raw)
    note_sent_prompt("c1", stamped)        # dashboard_warmup re-note (stamped)
    path = _write(
        tmp_path,
        _user(stamped),
        _assistant([{"type": "text", "text": "hi back"}]),
    )
    T.tail_transcript("s1", "c1", path)
    assert _capture == [("assistant", "hi back")]


def test_skips_tool_result_user_messages(tmp_path, _capture):
    # A user message that is only a tool_result must NOT be persisted as user
    # text — it completes the pending tool block into ONE event row instead.
    path = _write(
        tmp_path,
        _assistant([{"type": "tool_use", "id": "toolu_w", "name": "Write",
                     "input": {"file_path": "/tmp/x.py"}}]),
        _tool_result("toolu_w", "File created"),
    )
    T.tail_transcript("s2", "c2", path)
    assert _capture == []  # no text rows; Write isn't a Task so no registry entry either
    assert _capture.events == [("tool", {
        "type": "tool", "name": "Write", "tool_id": "toolu_w", "summary": "x.py",
        "active": False, "tool_input": {"file_path": "/tmp/x.py"},
        "tool_result": "File created", "result_summary": "ok", "is_error": False,
    })]


def test_idempotent_append_only_retail(tmp_path, _capture):
    path = tmp_path / "t.jsonl"
    path.write_text(_user("first"))
    assert T.tail_transcript("s3", "c3", str(path))["persisted"] == 1
    # Re-tail with no new lines → nothing.
    assert T.tail_transcript("s3", "c3", str(path))["persisted"] == 0
    # Append a new turn → only the new line is processed.
    with open(path, "a") as fh:
        fh.write(_assistant([{"type": "text", "text": "second"}]))
    assert T.tail_transcript("s3", "c3", str(path))["persisted"] == 1
    assert _capture == [("user", "first"), ("assistant", "second")]


def test_task_spawn_then_result_drives_registry(tmp_path, _capture):
    path = tmp_path / "t.jsonl"
    # Turn 1: a Task subagent is spawned (no result yet).
    path.write_text(_assistant([
        {"type": "tool_use", "id": "toolu_task1", "name": "Task",
         "input": {"description": "do work"}},
    ]))
    T.tail_transcript("s4", "c4", str(path))
    reg = get_subagent_registry("s4")
    assert "toolu_task1" in reg.spawned
    assert reg.has_pending  # outstanding subagent gates turn-end
    assert _capture.events == []  # spawn row waits for the result

    # Later: the subagent's tool_result lands on a subsequent tail → done,
    # and the pump-shaped task_spawn row persists with the fg report attached.
    with open(path, "a") as fh:
        fh.write(_tool_result("toolu_task1", "subagent output"))
    T.tail_transcript("s4", "c4", str(path))
    assert "toolu_task1" in reg.completed
    assert not reg.has_pending
    assert _capture.events == [("task_spawn", {
        "type": "task_spawn", "description": "do work", "subagent_type": "",
        "run_in_background": False, "tool_use_id": "toolu_task1",
        "tool_input": {"description": "do work"}, "tool_result": "subagent output",
    })]


def test_agent_named_spawn_drives_registry_and_persists(tmp_path, _capture):
    # Current CLIs name the subagent tool "Agent" — same registry feed + row.
    path = _write(
        tmp_path,
        _assistant([{"type": "tool_use", "id": "toolu_ag", "name": "Agent",
                     "input": {"description": "explore repo",
                               "subagent_type": "Explore"}}]),
        _tool_result("toolu_ag", "found it"),
    )
    T.tail_transcript("s4b", "c4b", path)
    reg = get_subagent_registry("s4b")
    assert "toolu_ag" in reg.spawned and "toolu_ag" in reg.completed
    assert _capture.events[0][0] == "task_spawn"
    assert _capture.events[0][1]["subagent_type"] == "Explore"
    assert _capture.events[0][1]["tool_result"] == "found it"


def test_background_task_spawn_skips_result_attach(tmp_path, _capture):
    # A bg spawn's tool_result is just the "launched" ack — the pump doesn't
    # attach it (the real report arrives via task_notification later).
    path = _write(
        tmp_path,
        _assistant([{"type": "tool_use", "id": "toolu_bg", "name": "Agent",
                     "input": {"description": "long job",
                               "run_in_background": True}}]),
        _tool_result("toolu_bg", "Async agent launched successfully."),
    )
    T.tail_transcript("s4c", "c4c", path)
    (etype, evt), = _capture.events
    assert etype == "task_spawn" and evt["run_in_background"] is True
    assert "tool_result" not in evt


def test_turn_complete_signal_from_end_turn(tmp_path, _capture):
    # The final assistant message of a turn carries stop_reason=="end_turn" (the
    # boundary); mid-turn messages are "tool_use". The tailer reports
    # turn_complete + the final text so the interactive-task watcher can complete
    # (gated bg-empty + min-time in interactive_session).
    path = _write(
        tmp_path,
        _user("summarize the repo"),
        _assistant_sr([{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}], "tool_use"),
        _tool_result("t1", "file contents"),
        _assistant_sr([{"type": "text", "text": "Here is the summary."}], "end_turn"),
    )
    stats = T.tail_transcript("se", "ce", path)
    assert stats["turn_complete"] is True
    assert stats["last_message"] == "Here is the summary."


def test_no_turn_complete_when_last_is_tool_use(tmp_path, _capture):
    # The last assistant message is "tool_use" (more tools coming) → NOT a turn
    # boundary; the watcher must keep waiting (no false completion mid-turn).
    path = _write(
        tmp_path,
        _user("do a thing"),
        _assistant_sr([{"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}], "tool_use"),
    )
    stats = T.tail_transcript("se2", "ce2", path)
    assert stats["turn_complete"] is False


def test_interrupt_marker_closes_turn_and_is_not_persisted(tmp_path, _capture):
    # ESC / dashboard Stop mid-turn: the CLI writes "[Request interrupted by
    # user for tool use]" (tool-phase; a single text block) instead of a result
    # event. It must close the turn (last_signal end_turn, NO turn_complete —
    # the user stopped it, no finished ping) and stay out of chat history.
    path = _write(
        tmp_path,
        _user("run the sleeps"),
        _assistant_sr([{"type": "tool_use", "id": "t9", "name": "Bash", "input": {}}], "tool_use"),
        _tool_result("t9", "The user doesn't want to proceed with this tool use.",
                     is_error=True),
        _line({"type": "user", "uuid": "int1",
               "message": {"role": "user", "content": [
                   {"type": "text", "text": "[Request interrupted by user for tool use]"}]}}),
    )
    stats = T.tail_transcript("si", "ci", path)
    assert stats["last_signal"] == "end_turn"
    assert stats["turn_complete"] is False
    assert ("user", "[Request interrupted by user for tool use]") not in _capture


def test_interrupt_marker_string_content_variant(tmp_path, _capture):
    # Text-phase interrupt: plain-string content "[Request interrupted by
    # user]" (no tool involved) — same close, same suppression.
    path = _write(
        tmp_path,
        _user("write me a poem"),
        _user("[Request interrupted by user]"),
    )
    stats = T.tail_transcript("si2", "ci2", path)
    assert stats["last_signal"] == "end_turn"
    assert _capture == [("user", "write me a poem")]


def test_interrupt_unpends_foreground_task_spawn(tmp_path, _capture):
    # An interrupt while a fg Task subagent is pending must un-pend it (the
    # turn died with it) so the bg-pending completion gates can't wedge.
    path = _write(
        tmp_path,
        _assistant([{"type": "tool_use", "id": "tk1", "name": "Task",
                     "input": {"description": "explore", "prompt": "look around"}}]),
        _line({"type": "user", "uuid": "int2",
               "message": {"role": "user", "content": [
                   {"type": "text", "text": "[Request interrupted by user]"}]}}),
    )
    stats = T.tail_transcript("si3", "ci3", path)
    assert stats["last_signal"] == "end_turn"
    reg = get_subagent_registry("si3")
    assert not reg.has_pending


def test_missing_file_is_graceful(_capture):
    assert T.tail_transcript("s5", "c5", "/no/such/file.jsonl")["persisted"] == 0
    assert T.tail_transcript("s5", "c5", "")["persisted"] == 0


def test_resolve_and_tail_discovers_transcript(tmp_path, _capture, monkeypatch):
    # The robust (no-Stop-hook) path: discover
    # <claude_dir>/projects/<hash>/<session_id>.jsonl via the session's
    # claude_dir, then tail it — the native TUI doesn't reliably fire Stop.
    sid = "sess-xyz"
    proj = tmp_path / ".claude" / "projects" / "-users-dave"
    proj.mkdir(parents=True)
    (proj / f"{sid}.jsonl").write_text(
        _user("from disk") + _assistant([{"type": "text", "text": "echoed"}])
    )
    monkeypatch.setattr(
        "core.session.session_state.get_session_claude_dir",
        lambda s: str(tmp_path / ".claude") if s == sid else None,
    )
    assert T.resolve_transcript_path(sid) == str(proj / f"{sid}.jsonl")
    stats = T.resolve_and_tail(sid, "c-disk")
    assert stats["persisted"] == 2
    assert _capture == [("user", "from disk"), ("assistant", "echoed")]


def test_resolve_and_tail_no_mapping_is_graceful(_capture, monkeypatch):
    monkeypatch.setattr("core.session.session_state.get_session_claude_dir", lambda s: None)
    assert T.resolve_and_tail("nope", "c")["persisted"] == 0
    assert T.resolve_transcript_path("nope") is None


def test_backfills_title_from_first_user_prompt(tmp_path, _capture, monkeypatch):
    # Interactive chats can't title at send-time, so the tailer backfills the
    # title from the first user message — only when the chat is still untitled.
    import storage.database as db
    titles = []
    monkeypatch.setattr(db, "get_chat", lambda cid: {"user_sub": "u", "title": ""})
    monkeypatch.setattr(db, "update_chat",
                        lambda cid, **kw: titles.append(kw.get("title")) if "title" in kw else None)
    path = _write(
        tmp_path,
        _user("Refactor the auth module to use JWT and drop the old session cookies please"),
        _assistant([{"type": "text", "text": "On it."}]),
    )
    out = T.tail_transcript("s-title", "c-title", path)
    assert out["title_set"] is True
    assert titles == ["Refactor the auth module to use…"]  # first 6 words + ellipsis


def test_does_not_overwrite_existing_title(tmp_path, _capture, monkeypatch):
    import storage.database as db
    titles = []
    monkeypatch.setattr(db, "get_chat", lambda cid: {"user_sub": "u", "title": "Existing Title"})
    monkeypatch.setattr(db, "update_chat",
                        lambda cid, **kw: titles.append(kw.get("title")) if "title" in kw else None)
    path = _write(tmp_path, _user("a new prompt"))
    out = T.tail_transcript("s-keep", "c-keep", path)
    assert out["title_set"] is False
    assert titles == []  # never overwrite an existing title


def test_title_strips_injected_time_prelude(tmp_path, _capture, monkeypatch):
    # The platform stamps `[Current time: ...]` onto interactive prompts and
    # the transcript persists it verbatim — the title must come from what the
    # user actually typed (stacked stamps from re-warms fold too).
    import storage.database as db
    titles = []
    monkeypatch.setattr(db, "get_chat", lambda cid: {"user_sub": "u", "title": ""})
    monkeypatch.setattr(db, "update_chat",
                        lambda cid, **kw: titles.append(kw.get("title")) if "title" in kw else None)
    stamp = "[Current time: Tuesday, July 07, 2026 19:31 (7:31 PM) Europe/Athens (UTC+03:00)]\n\n"
    path = _write(tmp_path, _user(stamp + stamp + "Fix the flaky login test"))
    out = T.tail_transcript("s-stamp", "c-stamp", path)
    assert out["title_set"] is True
    assert titles == ["Fix the flaky login test"]


def test_prelude_only_prompt_leaves_chat_untitled(tmp_path, _capture, monkeypatch):
    # A server housekeeping prompt that is ONLY the stamp sets no title, so the
    # next real prompt can still title the chat.
    import storage.database as db
    titles = []
    monkeypatch.setattr(db, "get_chat", lambda cid: {"user_sub": "u", "title": ""})
    monkeypatch.setattr(db, "update_chat",
                        lambda cid, **kw: titles.append(kw.get("title")) if "title" in kw else None)
    path = _write(tmp_path, _user(
        "[Current time: Tuesday, July 07, 2026 19:31 (7:31 PM) Europe/Athens (UTC+03:00)]"))
    out = T.tail_transcript("s-only", "c-only", path)
    assert out["title_set"] is False
    assert titles == []


# ---------------------------------------------------------------------------
# seek_past_existing — interactive attach must not re-persist prior history
# ---------------------------------------------------------------------------

def test_seek_past_existing_skips_prior_history(tmp_path, _capture, monkeypatch):
    """Toggling interactive ON for a chat with headless turns reuses the same
    transcript file — the attach-time seek must position the cursor past the
    pre-existing history so only lines appended DURING the stint persist."""
    path = tmp_path / "t.jsonl"
    path.write_text(
        _user("[Current time: Saturday, July 04, 2026 22:02 (10:02 PM)]\n\nDo you receive my message?")
        + _assistant([{"type": "text", "text": "Ναι, σε λαμβάνω κανονικά!"}])
        + _user("[Current time: Saturday, July 04, 2026 22:02 (10:02 PM)]\n\nNothing just checking")
        + _assistant([{"type": "text", "text": "All good then!"}])
    )
    monkeypatch.setattr(T, "resolve_transcript_path", lambda sid: str(path))

    assert T.seek_past_existing("s-seek", "c-seek") == 4

    # Nothing new yet → nothing persisted.
    assert T.tail_transcript("s-seek", "c-seek", str(path))["persisted"] == 0
    assert _capture == []

    # A turn typed DURING the interactive stint appends → only it persists.
    with open(path, "a") as fh:
        fh.write(_user("new interactive prompt"))
        fh.write(_assistant([{"type": "text", "text": "new reply"}]))
    assert T.tail_transcript("s-seek", "c-seek", str(path))["persisted"] == 2
    assert _capture == [("user", "new interactive prompt"), ("assistant", "new reply")]


def test_seek_without_transcript_starts_at_zero(tmp_path, _capture, monkeypatch):
    """A brand-new interactive chat has no transcript yet — the seek claims the
    cursor at 0 and the whole (all-new) conversation persists normally."""
    monkeypatch.setattr(T, "resolve_transcript_path", lambda sid: None)
    assert T.seek_past_existing("s-fresh", "c-fresh") == 0

    path = _write(tmp_path, _user("first ever prompt"))
    assert T.tail_transcript("s-fresh", "c-fresh", str(path))["persisted"] == 1
    assert _capture == [("user", "first ever prompt")]


def test_last_signal_batch_boundary_user_after_end_turn(tmp_path, _capture):
    """A batch straddling a turn boundary ([end_turn, user new-prompt] in one
    debounce window) still reports turn_complete=True (the notification/task
    signal), but last_signal MUST be "user" — a new turn is opening, so
    turn-open consumers (the prompt-injection gates) must not read it as idle."""
    path = _write(
        tmp_path,
        _assistant_sr([{"type": "text", "text": "Done."}], "end_turn"),
        _user("and now do this next"),
    )
    stats = T.tail_transcript("sb1", "cb1", path)
    assert stats["turn_complete"] is True
    assert stats["last_signal"] == "user"


def test_last_signal_end_turn_and_tool_use(tmp_path, _capture):
    path = _write(
        tmp_path,
        _user("go"),
        _assistant_sr([{"type": "text", "text": "Finished."}], "end_turn"),
    )
    assert T.tail_transcript("sb2", "cb2", path)["last_signal"] == "end_turn"

    path2 = tmp_path / "t2.jsonl"
    path2.write_text(
        _user("go")
        + _assistant_sr([{"type": "tool_use", "id": "tb", "name": "Bash", "input": {}}], "tool_use")
    )
    assert T.tail_transcript("sb3", "cb3", str(path2))["last_signal"] == "tool_use"


def test_last_signal_none_for_tool_result_only_batch(tmp_path, _capture):
    # tool_result user lines aren't turn-relevant input — a batch with nothing
    # else must leave last_signal None (turn state unchanged).
    path = _write(tmp_path, _tool_result("toolu_x", "output"))
    assert T.tail_transcript("sb4", "cb4", path)["last_signal"] is None


def _question(tuid: str) -> str:
    return _assistant_sr(
        [{"type": "tool_use", "id": tuid, "name": "AskUserQuestion",
          "input": {"questions": [{"question": "Which?"}]}}],
        "tool_use",
    )


def test_unanswered_question_folds_to_end_turn(tmp_path, _capture):
    """The harness blocks the turn on AskUserQuestion — an unanswered question
    at batch end closes the turn (headless parity) with question_pending set,
    beating the assistant line's stop_reason="tool_use". turn_complete stays
    False (the question ping is its own flavor, not a 'finished')."""
    path = _write(tmp_path, _user("go"), _question("t_q1"))
    stats = T.tail_transcript("sq1", "cq1", path)
    assert stats["last_signal"] == "end_turn"
    assert stats["question_pending"] is True
    assert stats["turn_complete"] is False
    assert ("event", "question") in _capture.order


def test_question_answered_in_batch_does_not_fold(tmp_path, _capture):
    # Answer landed in the same batch (fast user / overlapping tails): the
    # turn is NOT parked — the continuation's signals stand.
    path = _write(
        tmp_path,
        _user("go"),
        _question("t_q2"),
        _tool_result("t_q2", "user chose A"),
        _assistant_sr([{"type": "tool_use", "id": "t_b", "name": "Bash",
                        "input": {}}], "tool_use"),
    )
    stats = T.tail_transcript("sq2", "cq2", path)
    assert stats["last_signal"] == "tool_use"
    assert stats["question_pending"] is False


def test_question_then_interrupt_marker_is_plain_close(tmp_path, _capture):
    # ESC dismissed the question dialog: the interrupt marker's end_turn fold
    # wins and question_pending stays False — no "needs your input" ping for a
    # turn the user just killed.
    path = _write(
        tmp_path,
        _user("go"),
        _question("t_q3"),
        _user("[Request interrupted by user]"),
    )
    stats = T.tail_transcript("sq3", "cq3", path)
    assert stats["last_signal"] == "end_turn"
    assert stats["question_pending"] is False


def test_question_then_new_prompt_opens_turn(tmp_path, _capture):
    # Question dismissed and a fresh prompt typed within one batch: the new
    # turn wins — folding to end_turn here would read an opening turn as idle.
    path = _write(
        tmp_path,
        _question("t_q4"),
        _user("[Request interrupted by user]"),
        _user("different plan, do this instead"),
    )
    stats = T.tail_transcript("sq4", "cq4", path)
    assert stats["last_signal"] == "user"
    assert stats["question_pending"] is False


# ---------------------------------------------------------------------------
# Tool-event persistence — pump-shaped rows (headless parity)
# ---------------------------------------------------------------------------

def test_tool_rows_interleave_in_transcript_order(tmp_path, _capture):
    # Text persists at its line, a tool block at its RESULT's line — which the
    # protocol places before the turn's next assistant message, so DB order
    # matches the transcript: user → text → tool → text.
    path = _write(
        tmp_path,
        _user("check the file"),
        _assistant([{"type": "text", "text": "Looking."},
                    {"type": "tool_use", "id": "t_r", "name": "Read",
                     "input": {"file_path": "/a/b.txt"}}]),
        _tool_result("t_r", "line1\nline2"),
        _assistant_sr([{"type": "text", "text": "Two lines."}], "end_turn"),
    )
    stats = T.tail_transcript("so1", "co1", path)
    assert stats["persisted"] == 4  # user + 2 texts + 1 tool row
    assert _capture.order == [
        ("user", "check the file"),
        ("assistant", "Looking."),
        ("event", "tool"),
        ("assistant", "Two lines."),
    ]
    assert _capture.events[0][1]["summary"] == "b.txt"
    assert _capture.events[0][1]["result_summary"] == "2 lines"


def test_tool_pairing_across_batches_and_dedupe(tmp_path, _capture):
    # tool_use in one tail batch, its result in the next: exactly one row, at
    # result time. The pending buffer carries the pair across the cursor.
    path = tmp_path / "t.jsonl"
    path.write_text(_assistant([
        {"type": "tool_use", "id": "t_slow", "name": "Bash",
         "input": {"command": "sleep 99", "description": "wait a bit"}}]))
    assert T.tail_transcript("so2", "co2", str(path))["persisted"] == 0
    assert _capture.events == []

    with open(path, "a") as fh:
        fh.write(_tool_result("t_slow", "ok\n"))
    assert T.tail_transcript("so2", "co2", str(path))["persisted"] == 1
    (etype, evt), = _capture.events
    assert (etype, evt["name"], evt["summary"]) == ("tool", "Bash", "wait a bit")

    # A re-tail adds nothing (cursor), and even a replayed result can't
    # double-persist — the id was consumed.
    assert T.tail_transcript("so2", "co2", str(path))["persisted"] == 0
    assert len(_capture.events) == 1


def test_tool_result_truncation_mirrors_hook_policy(tmp_path, _capture):
    big = "\n".join(f"line {i}" for i in range(600))
    path = _write(
        tmp_path,
        _assistant([{"type": "tool_use", "id": "t_big", "name": "Bash",
                     "input": {"command": "cat big"}}]),
        _tool_result("t_big", big),
    )
    T.tail_transcript("so3", "co3", path)
    result = _capture.events[0][1]["tool_result"]
    assert result.endswith("... (100 more lines)")
    assert "line 499" in result and "line 500" not in result


def test_tool_result_content_blocks_and_error_flag(tmp_path, _capture):
    # tool_result content arrives as a block list for some tools; only text
    # blocks persist (never image payloads), and is_error rides the block.
    content = [{"type": "text", "text": "Error: no such host"},
               {"type": "image", "source": {"data": "AAAA"}}]
    path = _write(
        tmp_path,
        _assistant([{"type": "tool_use", "id": "t_err", "name": "WebFetch",
                     "input": {"url": "https://x.test"}}]),
        _tool_result("t_err", content, is_error=True),
    )
    T.tail_transcript("so4", "co4", path)
    (_, evt), = _capture.events
    assert evt["tool_result"] == "Error: no such host"
    assert evt["result_summary"].startswith("error:")
    assert evt["is_error"] is True
    assert "AAAA" not in json.dumps(evt)


def test_thinking_blocks_persist_nonempty_only(tmp_path, _capture):
    path = _write(
        tmp_path,
        _assistant([{"type": "thinking", "thinking": "let me reason", "signature": "sig"},
                    {"type": "thinking", "thinking": "", "signature": "sig2"},
                    {"type": "text", "text": "Answer."}]),
    )
    stats = T.tail_transcript("so5", "co5", path)
    assert stats["persisted"] == 2
    assert _capture.events == [("thinking", {"type": "thinking", "content": "let me reason"})]
    assert _capture.order[0] == ("event", "thinking")  # before the text row


def test_plan_mode_and_question_rows_persist_immediately(tmp_path, _capture):
    # Dedicated-event tools map like the pump: plan_mode + question rows at the
    # tool_use line; their tool_results are consumed silently (no extra row).
    path = _write(
        tmp_path,
        _assistant([{"type": "tool_use", "id": "t_ep", "name": "EnterPlanMode", "input": {}}]),
        _tool_result("t_ep", "ok"),
        _assistant([{"type": "tool_use", "id": "t_q", "name": "AskUserQuestion",
                     "input": {"questions": [{"question": "Which?"}]}}]),
        _tool_result("t_q", "user chose A"),
        _assistant([{"type": "tool_use", "id": "t_xp", "name": "ExitPlanMode",
                     "input": {"plan": "1. do it"}}]),
        _tool_result("t_xp", "approved"),
    )
    T.tail_transcript("so6", "co6", path)
    assert _capture.events == [
        ("plan_mode", {"type": "plan_mode", "action": "enter"}),
        ("question", {"type": "question", "tool_name": "AskUserQuestion",
                      "tool_input": {"questions": [{"question": "Which?"}]}}),
        ("plan_mode", {"type": "plan_mode", "action": "exit",
                       "tool_input": {"plan": "1. do it"}}),
    ]


def test_delegate_tool_is_suppressed(tmp_path, _capture):
    # The delegation endpoint persists its own delegate_spawn row — the raw MCP
    # tool call must not double-render (pump parity: _SKIP_TOOL_PERSIST).
    path = _write(
        tmp_path,
        _assistant([{"type": "tool_use", "id": "t_dg",
                     "name": "mcp__delegation-mcp__delegate",
                     "input": {"name": "job"}}]),
        _tool_result("t_dg", "Delegated 'job'"),
    )
    T.tail_transcript("so7", "co7", path)
    assert _capture.events == []


def test_skip_result_attach_set_keeps_input_only(tmp_path, _capture):
    # Tools in the hook's skip set get a card with the input summary but never
    # a result section — headless parity.
    path = _write(
        tmp_path,
        _assistant([{"type": "tool_use", "id": "t_tc", "name": "TaskCreate",
                     "input": {"subject": "new task"}}]),
        _tool_result("t_tc", '{"id": "task-1"}'),
    )
    T.tail_transcript("so8", "co8", path)
    (etype, evt), = _capture.events
    assert (etype, evt["name"]) == ("tool", "TaskCreate")
    assert "tool_result" not in evt and "result_summary" not in evt


def test_orphaned_pending_tool_drops_on_forget(tmp_path, _capture):
    # A tool whose result never arrives (abort/kill) is dropped with the
    # session state — the pump doesn't persist aborted calls either.
    path = _write(
        tmp_path,
        _assistant([{"type": "tool_use", "id": "t_orphan", "name": "Bash",
                     "input": {"command": "sleep 1000"}}]),
    )
    T.tail_transcript("so9", "co9", path)
    T.forget("so9")
    assert _capture.events == []
    assert "so9" not in T._tool_events


def test_thinking_row_claims_by_line_uuid(_capture):
    # Concurrent tails can double-process a slice (shared cursor, no lock) —
    # the line-uuid claim keeps thinking rows single-shot. tail_lines has no
    # cursor at all, so feeding the same line twice models the race directly.
    line = json.dumps({"type": "assistant", "uuid": "u-think",
                       "message": {"role": "assistant", "content": [
                           {"type": "thinking", "thinking": "deep thought"}]}}) + "\n"
    T.tail_lines("st-th", "ct-th", [line])
    T.tail_lines("st-th", "ct-th", [line])
    assert _capture.events == [("thinking", {"type": "thinking", "content": "deep thought"})]


def test_new_prompt_unpends_orphaned_fg_spawn(tmp_path, _capture):
    # A fg spawn interrupted without a result line (ESC race/kill) must not
    # wedge the bg_pending gates: the next REAL user prompt completes the
    # registry entry and drops the block (aborted spawns don't persist).
    path = _write(
        tmp_path,
        _assistant([{"type": "tool_use", "id": "toolu_lost", "name": "Agent",
                     "input": {"description": "doomed"}}]),
        _user("never mind, do something else"),
    )
    T.tail_transcript("s-orph", "c-orph", path)
    reg = get_subagent_registry("s-orph")
    assert not reg.has_pending
    assert _capture.events == []
    assert _capture == [("user", "never mind, do something else")]


def test_denied_spawn_persists_no_row(tmp_path, _capture):
    # Permission-rejected Agent: the result is the denial error. Headless
    # persists no row (task_started never fires) — parity, but the registry
    # entry still completes.
    path = _write(
        tmp_path,
        _assistant([{"type": "tool_use", "id": "toolu_deny", "name": "Agent",
                     "input": {"description": "blocked"}}]),
        _tool_result("toolu_deny", "The user doesn't want to proceed", is_error=True),
    )
    T.tail_transcript("s-deny", "c-deny", path)
    assert not get_subagent_registry("s-deny").has_pending
    assert _capture.events == []


def test_concurrent_tails_do_not_duplicate_rows(tmp_path, _capture, monkeypatch):
    # Overlapping tail triggers (debounce/sweep/close/Stop hook) run in worker
    # threads; without the per-session tail lock both read the cursor before
    # either advances it and the same slice persists twice (live-observed as
    # duplicate user rows). The first thread is held mid-persist; the second
    # must block on the lock and then find the cursor already advanced.
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
    path = _write(tmp_path, _user("only once please"))

    t1 = threading.Thread(target=T.tail_transcript, args=("s-race", "c-race", path))
    t1.start()
    assert entered.wait(timeout=5)  # t1 is inside the locked persist
    t2 = threading.Thread(target=T.tail_transcript, args=("s-race", "c-race", path))
    t2.start()
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert rows == [("user", "only once please")]


def test_sidechain_lines_are_ignored(tmp_path, _capture):
    line = json.dumps({"type": "assistant", "isSidechain": True,
                       "message": {"role": "assistant",
                                   "content": [{"type": "text", "text": "subagent inner"}]}}) + "\n"
    path = _write(tmp_path, line, _assistant([{"type": "text", "text": "main"}]))
    T.tail_transcript("so10", "co10", path)
    assert _capture == [("assistant", "main")]


# ---------------------------------------------------------------------------
# Slash-command noise filter — local output / meta lines never persist
# ---------------------------------------------------------------------------

def test_command_wrapper_lines_are_skipped(tmp_path, _capture):
    path = _write(
        tmp_path,
        _user("<command-name>/model</command-name>\n"
              "            <command-message>model</command-message>\n"
              "            <command-args>claude-sonnet-5</command-args>"),
        _user("<local-command-stdout>Set model to claude-sonnet-5</local-command-stdout>"),
        _user("a real prompt"),
    )
    stats = T.tail_transcript("sn1", "cn1", path)
    assert stats["persisted"] == 1
    assert _capture == [("user", "a real prompt")]


def test_meta_lines_are_skipped(tmp_path, _capture):
    meta = json.dumps({"type": "user", "isMeta": True,
                       "message": {"role": "user",
                                   "content": "<local-command-caveat>Caveat: local commands</local-command-caveat>"}}) + "\n"
    path = _write(tmp_path, meta, _user("hello"))
    T.tail_transcript("sn2", "cn2", path)
    assert _capture == [("user", "hello")]


def test_context_usage_report_is_skipped(tmp_path, _capture):
    report = ("## Context Usage\n**Model:** claude-sonnet-5\n"
              "**Tokens:** 45k/200k (23%)\n\nbreakdown...")
    path = _write(tmp_path, _user(report), _user("continue"))
    T.tail_transcript("sn3", "cn3", path)
    assert _capture == [("user", "continue")]


def test_command_wrapper_with_remainder_keeps_user_text(tmp_path, _capture):
    # Conservative matcher: only a message STARTING with a wrapper tag counts,
    # and any non-wrapped remainder is real user text.
    path = _write(
        tmp_path,
        _user("<command-name>/foo</command-name> and also do this"),
        _user("quoting <command-name>/bar</command-name> mid-message is fine"),
    )
    T.tail_transcript("sn4", "cn4", path)
    assert _capture == [
        ("user", "and also do this"),
        ("user", "quoting <command-name>/bar</command-name> mid-message is fine"),
    ]


def test_noise_never_titles_the_chat(tmp_path, _capture, monkeypatch):
    import storage.database as db
    titles = []
    monkeypatch.setattr(db, "get_chat", lambda cid: {"user_sub": "u", "title": ""})
    monkeypatch.setattr(db, "update_chat",
                        lambda cid, **kw: titles.append(kw.get("title")) if "title" in kw else None)
    path = _write(
        tmp_path,
        _user("<command-name>/context</command-name>"),
        _user("Fix the flaky login test"),
    )
    T.tail_transcript("sn5", "cn5", path)
    assert titles == ["Fix the flaky login test"]


# ---------------------------------------------------------------------------
# Harness-injected task-notification lines (bg bash / subagent completions)
# ---------------------------------------------------------------------------

_TASK_NOTIFICATION = (
    "<task-notification>\n<task-id>bhfa2mjno</task-id>\n"
    "<tool-use-id>toolu_01X</tool-use-id>\n"
    "<status>completed</status>\n"
    '<summary>Background command "build" completed (exit code 0)</summary>\n'
    "</task-notification>"
)


def test_task_notification_origin_lines_are_skipped(tmp_path, _capture):
    # origin.kind is the primary discriminator (real typed prompts carry
    # origin.kind == "human"; harness injections carry "task-notification").
    inj = _line({"type": "user", "uuid": "tn1",
                 "origin": {"kind": "task-notification"},
                 "message": {"role": "user", "content": _TASK_NOTIFICATION}})
    path = _write(tmp_path, inj, _user("hello"))
    stats = T.tail_transcript("sn6", "cn6", path)
    assert stats["persisted"] == 1
    assert _capture == [("user", "hello")]


def test_task_notification_content_skipped_without_origin(tmp_path, _capture):
    # Defense in depth: entrypoints that omit `origin` are still caught by
    # the whole-block content shape (start-anchored + closing tag).
    path = _write(tmp_path, _user(_TASK_NOTIFICATION), _user("hello"))
    T.tail_transcript("sn7", "cn7", path)
    assert _capture == [("user", "hello")]


def test_task_notification_fragment_pasted_by_user_is_kept(tmp_path, _capture):
    # A pasted fragment mid-discussion, or a message that starts with the tag
    # but never closes it, is REAL user text and must persist.
    path = _write(
        tmp_path,
        _user("look: <task-notification>x</task-notification> — weird?"),
        _user("<task-notification> is a tag the harness uses"),
    )
    T.tail_transcript("sn8", "cn8", path)
    assert _capture == [
        ("user", "look: <task-notification>x</task-notification> — weird?"),
        ("user", "<task-notification> is a tag the harness uses"),
    ]


def test_task_notification_never_titles_the_chat(tmp_path, _capture, monkeypatch):
    import storage.database as db
    titles = []
    monkeypatch.setattr(db, "get_chat", lambda cid: {"user_sub": "u", "title": ""})
    monkeypatch.setattr(db, "update_chat",
                        lambda cid, **kw: titles.append(kw.get("title")) if "title" in kw else None)
    path = _write(tmp_path, _user(_TASK_NOTIFICATION), _user("Fix the login test"))
    T.tail_transcript("sn9", "cn9", path)
    assert titles == ["Fix the login test"]


# ---------------------------------------------------------------------------
# Usage accounting — the tailer is the ONLY usage_records writer for
# interactive sessions (dashboard terminals, `otodock claude`, remote via the
# satellite's tail_lines). Assistant lines carry message.usage + message.model;
# one API message = one line PER CONTENT BLOCK with the SAME message.id and
# identical usage — so usage is claimed once per message id.
# ---------------------------------------------------------------------------

_USAGE = {"input_tokens": 1000, "output_tokens": 2000,
          "cache_read_input_tokens": 500, "cache_creation_input_tokens": 300}


def _assistant_usage(mid: str, block: dict, *, model: str = "claude-fable-5",
                     usage: dict | None = None, uuid: str = "") -> str:
    """One assistant transcript line the way the CLI writes it: a single
    content block, with the API message's id/model/usage repeated per line."""
    return _line({"type": "assistant", "uuid": uuid or f"u-{mid}-{block.get('type')}",
                  "message": {"role": "assistant", "id": mid, "model": model,
                              "content": [block],
                              "usage": dict(usage if usage is not None else _USAGE)}})


@pytest.fixture
def _usage_env(monkeypatch):
    """Pin every external the usage path touches: captured rows instead of DB
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
                        lambda sid: "sub-abc")
    monkeypatch.setattr(visibility, "is_shared_only",
                        lambda agent: agent == "shared-bot")
    monkeypatch.setattr(config, "get_model_pricing",
                        lambda m, p="": (10.0, 50.0, 12.5, 1.0))
    monkeypatch.setattr(config, "get_model_provider", lambda m: "anthropic")
    return rows


def test_usage_one_row_per_batch_deduped_by_message_id(tmp_path, _capture, _usage_env):
    # Three lines of ONE API message (thinking/text/tool_use blocks, identical
    # usage) + one line of a second message: exactly one row, tokens summed
    # over the two UNIQUE messages, cost from the pinned pricing.
    path = _write(
        tmp_path,
        _user("do the thing"),
        _assistant_usage("msg_1", {"type": "thinking", "thinking": "hm"}),
        _assistant_usage("msg_1", {"type": "text", "text": "Working."}),
        _assistant_usage("msg_1", {"type": "tool_use", "id": "t1", "name": "Bash",
                                   "input": {}}),
        _assistant_usage("msg_2", {"type": "text", "text": "Done."},
                         usage={"input_tokens": 10, "output_tokens": 20,
                                "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 0}),
    )
    stats = T.tail_transcript("su1", "cu1", path)
    assert stats["usage_rows"] == 1
    (row,) = _usage_env
    assert row["input_tokens"] == 1010 and row["output_tokens"] == 2020
    assert row["cache_read"] == 500 and row["cache_write"] == 300
    # (1010*10 + 2020*50 + 300*12.5 + 500*1.0) / 1e6
    assert row["cost_usd"] == pytest.approx(0.115350)
    assert row["source_key"] == "sub-abc"
    assert row["model"] == "claude-fable-5" and row["provider"] == "anthropic"
    assert row["scope"] == "user" and row["source_type"] == "interactive"
    assert row["source_id"] == "cu1" and row["user_sub"] == "u"
    assert row["message_count"] == 1  # keeps $0 rows through the filter


def test_usage_message_id_straddling_batches_counts_once(_capture, _usage_env):
    # Block 1 of a message tails in batch N, block 2 in batch N+1 (debounce
    # boundary): the message-id claim lives in the per-session buffer, so the
    # second batch adds NOTHING. tail_lines feeds batches directly (satellite
    # path — same fold).
    l1 = _assistant_usage("msg_x", {"type": "thinking", "thinking": "…"})
    l2 = _assistant_usage("msg_x", {"type": "text", "text": "done"})
    s1 = T.tail_lines("su2", "cu2", [l1])
    s2 = T.tail_lines("su2", "cu2", [l2])
    assert s1["usage_rows"] == 1 and s2["usage_rows"] == 0
    (row,) = _usage_env
    assert row["input_tokens"] == 1000 and row["output_tokens"] == 2000


def test_usage_zero_priced_model_row_still_written(tmp_path, _capture, _usage_env,
                                                   monkeypatch):
    # Unknown/local model resolving to $0 pricing: the row still lands (tokens
    # + message_count survive — pump parity), just with cost 0.
    import config
    monkeypatch.setattr(config, "get_model_pricing", lambda m, p="": (0, 0, 0, 0))
    path = _write(tmp_path, _assistant_usage(
        "msg_z", {"type": "text", "text": "hi"}, model="local-llama"))
    assert T.tail_transcript("su3", "cu3", path)["usage_rows"] == 1
    (row,) = _usage_env
    assert row["cost_usd"] == 0.0
    assert row["input_tokens"] == 1000 and row["message_count"] == 1


def test_usage_mixed_models_write_one_row_each(tmp_path, _capture, _usage_env):
    # /model mid-session: rows split per model so pricing/analytics stay honest.
    path = _write(
        tmp_path,
        _assistant_usage("msg_a", {"type": "text", "text": "a"},
                         model="claude-fable-5"),
        _assistant_usage("msg_b", {"type": "text", "text": "b"},
                         model="claude-haiku-4-5"),
    )
    assert T.tail_transcript("su4", "cu4", path)["usage_rows"] == 2
    assert {r["model"] for r in _usage_env} == {"claude-fable-5", "claude-haiku-4-5"}


def test_usage_absent_or_synthetic_lines_write_nothing(tmp_path, _capture, _usage_env):
    # Plain assistant lines without usage (the rest of this suite's fixtures)
    # and synthetic API-error lines (no message.id / all-zero usage) must not
    # produce rows.
    path = _write(
        tmp_path,
        _assistant([{"type": "text", "text": "no usage field"}]),
        _line({"type": "assistant", "uuid": "syn1",
               "message": {"role": "assistant", "model": "<synthetic>",
                           "content": [{"type": "text", "text": "API Error"}],
                           "usage": {"input_tokens": 0, "output_tokens": 0}}}),
        _assistant_usage("msg_z0", {"type": "text", "text": "zeroed"},
                         usage={"input_tokens": 0, "output_tokens": 0,
                                "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 0}),
    )
    assert T.tail_transcript("su5", "cu5", path)["usage_rows"] == 0
    assert _usage_env == []


def test_usage_shared_only_agent_bills_agent_scope(tmp_path, _capture, _usage_env,
                                                   monkeypatch):
    import storage.database as db
    monkeypatch.setattr(db, "get_chat", lambda cid: {
        "user_sub": "agent:shared-bot", "title": "t", "agent": "shared-bot",
        "source_type": "chat"})
    path = _write(tmp_path, _assistant_usage("msg_s", {"type": "text", "text": "x"}))
    T.tail_transcript("su6", "cu6", path)
    (row,) = _usage_env
    assert row["scope"] == "agent" and row["agent"] == "shared-bot"
    assert row["source_type"] == "chat"  # chat row's own source_type wins


def test_usage_unbound_session_attributes_default(tmp_path, _capture, _usage_env,
                                                  monkeypatch):
    from services.engines import subscription_pool
    monkeypatch.setattr(subscription_pool, "get_session_subscription",
                        lambda sid: None)
    path = _write(tmp_path, _assistant_usage("msg_d", {"type": "text", "text": "x"}))
    T.tail_transcript("su7", "cu7", path)
    (row,) = _usage_env
    assert row["source_key"] == "default"


def test_usage_failure_never_breaks_transcript_persistence(tmp_path, _capture,
                                                           _usage_env, monkeypatch):
    from services.billing import usage_service
    def _boom(rows):
        raise RuntimeError("db down")
    monkeypatch.setattr(usage_service, "record_turn_usage", _boom)
    path = _write(
        tmp_path,
        _user("hello"),
        _assistant_usage("msg_f", {"type": "text", "text": "world"}),
    )
    stats = T.tail_transcript("su8", "cu8", path)
    assert stats["persisted"] == 2  # text rows landed despite the usage failure
    assert stats["usage_rows"] == 0
    assert _capture == [("user", "hello"), ("assistant", "world")]


def test_usage_missing_chat_row_drops_batch_gracefully(tmp_path, _capture,
                                                       _usage_env, monkeypatch):
    import storage.database as db
    monkeypatch.setattr(db, "get_chat", lambda cid: None)
    path = _write(tmp_path, _assistant_usage("msg_n", {"type": "text", "text": "x"}))
    stats = T.tail_transcript("su9", "cu9", path)
    assert stats["usage_rows"] == 0 and _usage_env == []
    assert stats["persisted"] == 1  # the text itself still persisted


# ---------------------------------------------------------------------------
# Resume-replay guard — `claude --resume <old> --session-id <new>` creates a
# NEW transcript that REPLAYS the prior conversation as copied lines (original
# timestamps preserved). The attach-time seek saw no file (cursor 0), so
# without the timestamp bound the first tail re-persisted the whole history
# (duplicate text/tool rows — pre-existing bug — and a phantom usage row).
# ---------------------------------------------------------------------------


def _stamped(line: str, ts: str) -> str:
    obj = json.loads(line)
    obj["timestamp"] = ts
    return json.dumps(obj) + "\n"


def test_resume_rewarm_replay_lines_are_skipped(tmp_path, _capture, _usage_env,
                                                monkeypatch):
    from datetime import datetime, timezone
    # Attach: the resume's new transcript doesn't exist yet → cursor 0, and
    # the replay guard arms at NOW.
    monkeypatch.setattr(T, "resolve_transcript_path", lambda sid: None)
    assert T.seek_past_existing("s-replay", "c-replay") == 0

    old = "2026-07-10T10:00:00.000Z"  # the copied history's original stamps
    now = datetime.now(timezone.utc).isoformat()
    path = _write(
        tmp_path,
        # --- replayed history (copies; already persisted + usage-recorded) ---
        _stamped(_user("original prompt from hours ago"), old),
        _stamped(_assistant_usage("msg_old", {"type": "text",
                                              "text": "original reply"}), old),
        _stamped(_assistant([{"type": "tool_use", "id": "t_old", "name": "Bash",
                              "input": {"command": "echo hi"}}]), old),
        _stamped(_tool_result("t_old", "hi"), old),
        # --- the genuinely new turn ---
        _stamped(_user("the new prompt"), now),
        _stamped(_assistant_usage("msg_new", {"type": "text",
                                              "text": "the new reply"},
                                  usage={"input_tokens": 10, "output_tokens": 20,
                                         "cache_read_input_tokens": 0,
                                         "cache_creation_input_tokens": 0}), now),
    )
    stats = T.tail_transcript("s-replay", "c-replay", path)
    assert _capture == [("user", "the new prompt"),
                        ("assistant", "the new reply")]
    assert _capture.events == []  # replayed tool pair never persisted
    assert stats["persisted"] == 2
    # Usage counts ONLY the new message — no phantom history row.
    assert stats["usage_rows"] == 1
    (row,) = _usage_env
    assert row["input_tokens"] == 10 and row["output_tokens"] == 20


def test_untimestamped_lines_pass_the_replay_guard(tmp_path, _capture, monkeypatch):
    # Synthetic/meta records (and this suite's fixtures) carry no timestamp —
    # the guard must not drop them.
    monkeypatch.setattr(T, "resolve_transcript_path", lambda sid: None)
    T.seek_past_existing("s-nots", "c-nots")
    path = _write(tmp_path, _user("no timestamp on this line"))
    assert T.tail_transcript("s-nots", "c-nots", path)["persisted"] == 1


def test_tail_lines_is_not_bounded_by_attach_time(_capture):
    # Satellite path: the satellite owns replay exclusion (its own seek), and
    # its machine's clock must not be judged against proxy wall-time — an
    # old-stamped forwarded line still persists.
    line = _stamped(_user("forwarded from a skewed remote clock"),
                    "2026-07-10T10:00:00.000Z")
    T.tail_lines("s-sat", "c-sat", [line])
    assert _capture == [("user", "forwarded from a skewed remote clock")]


# ---------------------------------------------------------------------------
# Send-time-persisted prompt dedupe — the dashboard warmup persists the cold
# first prompt at send (_persist_first_prompt) and the CLI then journals the
# SAME text: without the note/consume pair the tailer re-inserted it (the
# live-observed duplicated first user row on fresh interactive chats).
# ---------------------------------------------------------------------------


def test_send_persisted_prompt_is_skipped_once(tmp_path, _capture):
    from core.session import transcript_tool_events as TE
    prompt = "[Current time: Friday, July 10, 2026 22:30 (10:30 PM) UTC]\n\ndo the thing"
    TE.note_sent_prompt("c-note", prompt)
    path = _write(
        tmp_path,
        _user(prompt),
        _assistant_sr([{"type": "text", "text": "done"}], "end_turn"),
    )
    stats = T.tail_transcript("s-note", "c-note", path)
    # The user row is NOT re-persisted; the turn signal still counts.
    assert _capture == [("assistant", "done")]
    assert stats["persisted"] == 1
    assert stats["last_signal"] == "end_turn"

    # The note is one-shot: the same text typed again later persists normally.
    p2 = tmp_path / "t2.jsonl"
    p2.write_text(_user(prompt))
    T._offsets.pop("s-note2", None)
    assert T.tail_transcript("s-note2", "c-note", str(p2))["persisted"] == 1
    assert ("user", prompt) in _capture


def test_non_matching_note_does_not_skip(tmp_path, _capture):
    from core.session import transcript_tool_events as TE
    TE.note_sent_prompt("c-note3", "a different prompt")
    path = _write(tmp_path, _user("the actual prompt"))
    assert T.tail_transcript("s-note3", "c-note3", path)["persisted"] == 1
    assert _capture == [("user", "the actual prompt")]


# ─────────────────────── compaction (compact_boundary) ──────────────────────


def test_compact_boundary_flags_and_persists_separator(tmp_path, _capture):
    boundary = _line({
        "type": "system", "subtype": "compact_boundary",
        "content": "Conversation compacted",
        "compactMetadata": {"trigger": "manual",
                            "preTokens": 500_000, "postTokens": 12_000},
    })
    path = _write(tmp_path, boundary)
    stats = T.tail_transcript("s-cmp", "c-cmp", path)
    assert stats["compacted"] is True
    assert stats["persisted"] == 1
    # The visible row is the SAME subtype the live headless compact appends.
    assert _capture.events[0][1]["subtype"] == "context_compressed"
    assert "488,000" in _capture.events[0][1]["message"]
    # A boundary is not a turn signal by itself.
    assert stats["last_signal"] is None
    # The trigger rides up so a MANUAL boundary can close a stale-open turn.
    assert stats["compact_trigger"] == "manual"


def test_compact_summary_user_line_is_skipped(tmp_path, _capture):
    # The post-compaction reseed ("This session is being continued…") is
    # synthetic: no row, no "user" turn signal — persisting it dumped the
    # whole summary into history and opened a phantom turn.
    summary = json.dumps({
        "type": "user", "isCompactSummary": True,
        "isVisibleInTranscriptOnly": True,
        "message": {"role": "user",
                    "content": "This session is being continued from a previous conversation…"},
    }) + "\n"
    path = _write(tmp_path, summary)
    stats = T.tail_transcript("s-cs", "c-cs", path)
    assert stats["persisted"] == 0
    assert stats["last_signal"] is None
    assert stats["compacted"] is False
    assert list(_capture) == []


def test_no_compaction_flag_on_plain_batches(tmp_path, _capture):
    path = _write(tmp_path, _user("hello"))
    assert T.tail_transcript("s-nc", "c-nc", path)["compacted"] is False


# ---------------------------------------------------------------------------
# CLI-reported API errors → subscription throttle hook
# ---------------------------------------------------------------------------


def test_api_error_row_triggers_limit_hook(tmp_path, _capture, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "services.engines.subscription_pool.throttle_from_cli_error",
        lambda sid, text: calls.append((sid, text)))
    path = _write(tmp_path, _line({
        "type": "assistant", "uuid": "err1", "isApiErrorMessage": True,
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "API Error: 429 rate limit exceeded"}]},
    }))
    T.tail_transcript("s-lim", "c-lim", path)
    assert calls == [("s-lim", "API Error: 429 rate limit exceeded")]
    # The error row still lands in chat history — the user must see it.
    assert ("assistant", "API Error: 429 rate limit exceeded") in list(_capture)


def test_prose_mentioning_limits_never_triggers_hook(tmp_path, _capture, monkeypatch):
    # Model PROSE discussing rate limits (a dev agent does this daily) must
    # not rest the account — only CLI-marked error rows classify.
    calls = []
    monkeypatch.setattr(
        "services.engines.subscription_pool.throttle_from_cli_error",
        lambda sid, text: calls.append((sid, text)))
    path = _write(tmp_path, _assistant(
        [{"type": "text", "text": "the 429 rate limit handling code looks fine"}]))
    T.tail_transcript("s-prose", "c-prose", path)
    assert calls == []

"""DB-history seed for fresh sessions (core/session/history_seed.py).

Covers the digest builder (rendering rules, caps, budget, trailing-user skip),
the atomic pending-seed claim, the consume flow (prepend + notice row + flag
clear), and the chat transitions that produce the flag: delete_remote_machine
(all pinned chats) and a move's reseed rebind (one chat,
``remote_store.rebind_chat_to_target``).
"""

import json


def _add(db, chat_id, role, content="", event_type="", event_data=""):
    return db.add_chat_message(chat_id, role, content,
                               event_type=event_type, event_data=event_data)


def _tool_event(name, summary="", tool_input=None):
    return json.dumps({
        "type": "tool", "name": name, "tool_id": "t1",
        "summary": summary, "active": False,
        "tool_input": tool_input or {},
    })


# ---------------------------------------------------------------------------
# build_history_seed — rendering
# ---------------------------------------------------------------------------

def test_seed_renders_user_assistant_in_order(temp_db):
    from core.session.history_seed import build_history_seed
    from storage import database as db
    db.create_chat("c1", "user-1", "agent-x")
    _add(db, "c1", "user", "first question")
    _add(db, "c1", "assistant", "first answer")
    _add(db, "c1", "user", "second question")
    _add(db, "c1", "assistant", "second answer")

    seed = build_history_seed("c1")
    assert seed.startswith("[Context restore:")
    assert seed.rstrip().endswith("The latest message follows.]")
    i_u1 = seed.index("User: first question")
    i_a1 = seed.index("Assistant: first answer")
    i_u2 = seed.index("User: second question")
    i_a2 = seed.index("Assistant: second answer")
    assert i_u1 < i_a1 < i_u2 < i_a2


def test_seed_skips_trailing_user_row(temp_db):
    """The current turn's prompt is persisted just before consumption — it
    must appear only as the live prompt, never duplicated in the digest."""
    from core.session.history_seed import build_history_seed
    from storage import database as db
    db.create_chat("c2", "user-1", "agent-x")
    _add(db, "c2", "user", "old question")
    _add(db, "c2", "assistant", "old answer")
    _add(db, "c2", "user", "the current prompt")

    seed = build_history_seed("c2")
    assert "old question" in seed
    assert "old answer" in seed
    assert "the current prompt" not in seed


def test_seed_per_message_truncation(temp_db):
    from core.session import history_seed
    from storage import database as db
    db.create_chat("c3", "user-1", "agent-x")
    _add(db, "c3", "user", "x" * (history_seed.SEED_PER_MESSAGE_CHARS + 500))
    _add(db, "c3", "assistant", "short reply")

    seed = history_seed.build_history_seed("c3")
    assert "…[truncated]" in seed
    # The full untruncated body must not appear.
    assert "x" * (history_seed.SEED_PER_MESSAGE_CHARS + 500) not in seed
    assert "Assistant: short reply" in seed


def test_seed_tool_lines(temp_db):
    from core.session.history_seed import build_history_seed
    from storage import database as db
    db.create_chat("c4", "user-1", "agent-x")
    _add(db, "c4", "user", "do things")
    _add(db, "c4", "event", event_type="tool",
         event_data=_tool_event("Edit", summary="proxy/api/hooks/hooks.py"))
    _add(db, "c4", "event", event_type="tool",
         event_data=_tool_event("Bash", tool_input={"command": "ls -la /tmp"}))
    _add(db, "c4", "event", event_type="tool",
         event_data=_tool_event("TodoWrite", summary="3 todos"))
    _add(db, "c4", "assistant", "done")

    seed = build_history_seed("c4")
    assert "[tool: Edit — proxy/api/hooks/hooks.py]" in seed
    assert "[tool: Bash" in seed and "ls -la /tmp" in seed
    assert "TodoWrite" not in seed


def test_seed_tool_line_truncation(temp_db):
    from core.session import history_seed
    from storage import database as db
    db.create_chat("c5", "user-1", "agent-x")
    _add(db, "c5", "event", event_type="tool",
         event_data=_tool_event("Bash", summary="y" * 500))
    _add(db, "c5", "assistant", "ok")

    seed = history_seed.build_history_seed("c5")
    tool_line = next(l for l in seed.splitlines() if l.startswith("[tool:"))
    assert len(tool_line) <= history_seed.SEED_TOOL_LINE_CHARS + len(" …[truncated]")


def test_seed_skips_non_tool_events(temp_db):
    from core.session.history_seed import build_history_seed
    from storage import database as db
    db.create_chat("c6", "user-1", "agent-x")
    _add(db, "c6", "user", "hello")
    _add(db, "c6", "event", event_type="thinking",
         event_data=json.dumps({"type": "thinking", "content": "SECRET-REASONING"}))
    _add(db, "c6", "event", event_type="permission_prompt",
         event_data=json.dumps({"type": "permission_prompt", "tool": "Bash"}))
    _add(db, "c6", "event", event_type="system",
         event_data=json.dumps({"type": "system", "subtype": "session_reseeded",
                                "message": "OLD-NOTICE"}))
    _add(db, "c6", "assistant", "hi")

    seed = build_history_seed("c6")
    assert "SECRET-REASONING" not in seed
    assert "permission" not in seed.lower()
    assert "OLD-NOTICE" not in seed
    assert "User: hello" in seed and "Assistant: hi" in seed


def test_seed_budget_drops_oldest_first(temp_db):
    from core.session.history_seed import build_history_seed
    from storage import database as db
    db.create_chat("c7", "user-1", "agent-x")
    for i in range(20):
        _add(db, "c7", "user", f"question number {i:02d} " + "pad " * 30)
        _add(db, "c7", "assistant", f"answer number {i:02d} " + "pad " * 30)

    seed = build_history_seed("c7", max_chars=600)
    assert "answer number 19" in seed       # newest survives
    assert "question number 00" not in seed  # oldest dropped
    assert len(seed) < 600 + 600  # content budget + wrapper overhead


def test_seed_empty_chat(temp_db):
    from core.session.history_seed import build_history_seed
    from storage import database as db
    db.create_chat("c8", "user-1", "agent-x")
    assert build_history_seed("c8") == ""
    # Only the current (trailing-user) prompt → still nothing to restore.
    _add(db, "c8", "user", "the very first message")
    assert build_history_seed("c8") == ""


# ---------------------------------------------------------------------------
# claim_pending_history_seed — atomic claim
# ---------------------------------------------------------------------------

def test_claim_pending_seed_returns_once(temp_db):
    from storage import database as db
    db.create_chat("c9", "user-1", "agent-x")
    assert db.get_chat("c9")["pending_history_seed"] == ""
    assert db.claim_pending_history_seed("c9") == ""  # nothing pending

    assert db.update_chat("c9", pending_history_seed="machine_removed:Laptop") is True
    assert db.claim_pending_history_seed("c9") == "machine_removed:Laptop"
    assert db.claim_pending_history_seed("c9") == ""  # already claimed
    assert db.get_chat("c9")["pending_history_seed"] == ""


def test_claim_pending_seed_missing_chat(temp_db):
    from storage import database as db
    assert db.claim_pending_history_seed("no-such-chat") == ""


# ---------------------------------------------------------------------------
# consume_pending_seed — full flow
# ---------------------------------------------------------------------------

def test_consume_noop_without_flag(temp_db):
    from core.session.history_seed import consume_pending_seed
    from storage import database as db
    db.create_chat("c10", "user-1", "agent-x")
    _add(db, "c10", "user", "history msg")
    _add(db, "c10", "assistant", "history reply")

    text, notice = consume_pending_seed("c10", "new prompt")
    assert text == "new prompt"
    assert notice == ""
    # No notice row persisted.
    assert all(m["event_type"] != "system" for m in db.get_chat_messages("c10"))


def test_consume_machine_removed_flow(temp_db):
    from core.session.history_seed import consume_pending_seed
    from storage import database as db
    db.create_chat("c11", "user-1", "agent-x")
    _add(db, "c11", "user", "earlier question")
    _add(db, "c11", "assistant", "earlier answer")
    _add(db, "c11", "user", "the new prompt")  # just persisted by _handle_chat
    db.update_chat("c11", pending_history_seed="machine_removed:Office-PC")

    text, notice = consume_pending_seed("c11", "the new prompt")
    # Digest prepended, live prompt last, current message not duplicated.
    assert text.startswith("[Context restore:")
    assert text.endswith("the new prompt")
    assert "earlier question" in text and "earlier answer" in text
    assert text.count("the new prompt") == 1
    # Notice references the machine and lands as a persisted system row.
    assert "Office-PC" in notice
    rows = [m for m in db.get_chat_messages("c11") if m["event_type"] == "system"]
    assert len(rows) == 1
    block = json.loads(rows[0]["event_data"])
    assert block["subtype"] == "session_reseeded"
    assert block["machine_name"] == "Office-PC"
    assert block["reason"] == "machine_removed"
    assert block["message"] == notice
    # Flag cleared — second consume is a no-op.
    text2, notice2 = consume_pending_seed("c11", "another prompt")
    assert text2 == "another prompt" and notice2 == ""


def test_consume_retention_reason(temp_db):
    from core.session.history_seed import consume_pending_seed
    from storage import database as db
    db.create_chat("c12", "user-1", "agent-x")
    _add(db, "c12", "user", "old msg")
    _add(db, "c12", "assistant", "old reply")
    db.update_chat("c12", pending_history_seed="retention")

    text, notice = consume_pending_seed("c12", "go on")
    assert "cleaned up" in notice
    assert text.startswith("[Context restore:") and text.endswith("go on")
    block = json.loads(next(
        m["event_data"] for m in db.get_chat_messages("c12")
        if m["event_type"] == "system"
    ))
    assert block["reason"] == "retention"
    assert block["machine_name"] == ""


def test_consume_resume_failed_reason(temp_db):
    """The warmup fresh branch flags 'resume_failed' when the resume gate
    refuses — dedicated wording (nothing was 'cleaned up'), digest restored."""
    from core.session.history_seed import consume_pending_seed
    from storage import database as db
    db.create_chat("c12b", "user-1", "agent-x")
    _add(db, "c12b", "user", "old msg")
    _add(db, "c12b", "assistant", "old reply")
    db.update_chat("c12b", pending_history_seed="resume_failed")

    text, notice = consume_pending_seed("c12b", "go on")
    assert "could not be resumed" in notice
    assert text.startswith("[Context restore:") and text.endswith("go on")
    block = json.loads(next(
        m["event_data"] for m in db.get_chat_messages("c12b")
        if m["event_type"] == "system"
    ))
    assert block["reason"] == "resume_failed"
    assert block["machine_name"] == ""


def test_consume_moved_reason(temp_db):
    """The explicit "Move this chat" action stamps 'moved:<label>' — the
    consumer's dedicated branch names the source label in the notice and the
    persisted card carries reason='moved' + machine_name (an unknown kind
    would fall to the wrong "files were cleaned up" copy)."""
    from core.session.history_seed import consume_pending_seed
    from storage import database as db
    db.create_chat("c12c", "user-1", "agent-x")
    _add(db, "c12c", "user", "old msg")
    _add(db, "c12c", "assistant", "old reply")
    db.update_chat("c12c", pending_history_seed="moved:Office-PC")

    text, notice = consume_pending_seed("c12c", "go on")
    assert "moved from Office-PC" in notice
    assert text.startswith("[Context restore:") and text.endswith("go on")
    block = json.loads(next(
        m["event_data"] for m in db.get_chat_messages("c12c")
        if m["event_type"] == "system"
    ))
    assert block["subtype"] == "session_reseeded"
    assert block["reason"] == "moved"
    assert block["machine_name"] == "Office-PC"
    assert block["message"] == notice


def test_consume_with_empty_history_still_notices(temp_db):
    """Flag set but nothing restorable: no digest, but the notice still
    persists and the flag clears (the fresh session is still a fact)."""
    from core.session.history_seed import consume_pending_seed
    from storage import database as db
    db.create_chat("c13", "user-1", "agent-x")
    _add(db, "c13", "user", "first ever prompt")  # current turn's row
    db.update_chat("c13", pending_history_seed="machine_removed:Box")

    text, notice = consume_pending_seed("c13", "first ever prompt")
    assert text == "first ever prompt"  # no digest prepended
    assert "Box" in notice
    assert db.get_chat("c13")["pending_history_seed"] == ""


# ---------------------------------------------------------------------------
# delete_remote_machine — the chat transition that sets the flag
# ---------------------------------------------------------------------------

def _insert_machine(machine_id, name):
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO remote_machines (id, name, registered_by, created_at) "
            "VALUES (%s, %s, 'admin-1', '2026-06-11T00:00:00+00:00')",
            (machine_id, name),
        )
        conn.commit()


def test_delete_machine_transitions_pinned_chats(temp_db):
    from storage import database as db, remote_store

    _insert_machine("m-dead", "Office-PC")
    _insert_machine("m-alive", "Laptop")

    db.create_chat("pinned", "user-1", "agent-x")
    db.update_chat("pinned", execution_target="m-dead", session_id="sess-1",
                   codex_thread_id="thread-1", last_turn_aborted=True,
                   context_used=120000)
    db.create_chat("other-machine", "user-1", "agent-x")
    db.update_chat("other-machine", execution_target="m-alive", session_id="sess-2")
    db.create_chat("local-chat", "user-1", "agent-x")
    db.update_chat("local-chat", session_id="sess-3")  # stays 'local'

    assert remote_store.delete_remote_machine("m-dead") is True

    pinned = db.get_chat("pinned")
    assert pinned["execution_target"] == ""
    assert pinned["session_id"] is None
    assert pinned["codex_thread_id"] is None
    assert pinned["last_turn_aborted"] is False
    assert pinned["context_used"] == 0
    assert pinned["pending_history_seed"] == "machine_removed:Office-PC"

    other = db.get_chat("other-machine")
    assert other["execution_target"] == "m-alive"
    assert other["session_id"] == "sess-2"
    assert other["pending_history_seed"] == ""

    local = db.get_chat("local-chat")
    assert local["execution_target"] == "local"
    assert local["session_id"] == "sess-3"
    assert local["pending_history_seed"] == ""


# ---------------------------------------------------------------------------
# The move's two rebinds (services/remote/chat_move.py)
# ---------------------------------------------------------------------------

def test_a_move_without_its_files_reseeds_one_chat_at_the_chosen_place(temp_db):
    """The reseed rebind pins the chat to the CHOSEN place, drops its ids and
    resets its flags, and seeds ``moved:<move id>``; a sibling on the same
    machine is untouched; a compare-and-set on the old place and ids."""
    from storage import database as db, remote_store

    _insert_machine("m-office", "Office-PC")
    db.create_chat("moving", "user-1", "agent-x")
    db.update_chat("moving", execution_target="m-office", session_id="sess-1",
                   codex_thread_id="thread-1", last_turn_aborted=True,
                   context_used=120000)
    db.create_chat("staying", "user-1", "agent-x")
    db.update_chat("staying", execution_target="m-office", session_id="sess-2")

    stale = dict(expect_target="local", expect_session_id="sess-1", expect_thread="thread-1")
    assert remote_store.rebind_chat_to_target("moving", "local", "mv-1", **stale) is False
    ok = dict(expect_target="m-office", expect_session_id="sess-1", expect_thread="thread-1")
    assert remote_store.rebind_chat_to_target("moving", "local", "mv-1", **ok) is True

    moved = db.get_chat("moving")
    assert moved["execution_target"] == "local"
    assert moved["session_id"] is None and moved["codex_thread_id"] is None
    assert moved["last_turn_aborted"] is False and moved["context_used"] == 0
    assert moved["pending_history_seed"] == "moved:mv-1"
    staying = db.get_chat("staying")
    assert staying["execution_target"] == "m-office" and staying["session_id"] == "sess-2"


def test_a_move_with_its_files_keeps_the_ids(temp_db):
    from storage import database as db, remote_store
    db.create_chat("m2", "user-1", "agent-x")
    db.update_chat("m2", execution_target="local", session_id="sess-9",
                   context_used=5000, pending_history_seed="resume_failed")
    assert remote_store.rebind_chat_with_files(
        "m2", "m-office", expect_target="local", expect_session_id="sess-9",
        expect_thread=None) is True
    row = db.get_chat("m2")
    assert row["execution_target"] == "m-office" and row["session_id"] == "sess-9"
    assert row["context_used"] == 5000 and not row["pending_history_seed"]


def test_a_rebind_of_an_unknown_chat_changes_nothing(temp_db):
    from storage import remote_store
    assert remote_store.rebind_chat_with_files(
        "no-such-chat", "local", expect_target="", expect_session_id=None,
        expect_thread=None) is False


def test_the_card_of_a_summarised_move_reads_its_row(temp_db):
    from core.session.history_seed import consume_pending_seed_digest
    from storage import database as db, move_schema, remote_store
    db.create_chat("m3", "user-1", "agent-x")
    _add(db, "m3", "user", "hello")
    remote_store.create_chat_move({
        "id": "0b8a1c3e-7f2d-4f6a-9a51-0c1d2e3f4a5b", "chat_id": "m3", "source": "m-1",
        "target": "local", "from_label": "Office-PC", "to_label": "OtoDock platform",
        "state": move_schema.SETTLED, "memory": move_schema.MEMORY_SUMMARY,
        "why": move_schema.WHY_OFFLINE, "created_at": "2026-10-19T00:00:00+00:00"})
    db.update_chat("m3", pending_history_seed="moved:0b8a1c3e-7f2d-4f6a-9a51-0c1d2e3f4a5b")
    _digest, notice = consume_pending_seed_digest("m3")
    assert notice == ("This chat moved from Office-PC to the OtoDock platform. Office-PC was "
                      "offline, so it continues from a summary of the conversation.")


def test_a_legacy_moved_seed_keeps_its_words(temp_db):
    from core.session.history_seed import consume_pending_seed_digest
    from storage import database as db
    db.create_chat("m4", "user-1", "agent-x")
    _add(db, "m4", "user", "hello")
    db.update_chat("m4", pending_history_seed="moved:Office-PC")
    _digest, notice = consume_pending_seed_digest("m4")
    assert notice.startswith("This chat moved from Office-PC")


# ---------------------------------------------------------------------------
# build_history_seed — delegate results
# ---------------------------------------------------------------------------

def _delegate_event(**over):
    data = {"task_id": "t1", "task_name": "review", "agent": "worker", "output_text": "the report",
            "status": "completed", **over}
    return json.dumps(data)


def test_seed_names_a_delegates_result(temp_db):
    """A re-warmed delegating session knows its lanes' results: one line per
    delegate_result, with how it ended and the files it attached."""
    from core.session.history_seed import build_history_seed
    from storage import database as db
    db.create_chat("dr1", "user-1", "agent-x")
    _add(db, "dr1", "user", "split the review")
    _add(db, "dr1", "event", event_type="delegate_result",
         event_data=_delegate_event(files=[{"path": "workspace/inbox/worker/r.md", "bytes": 9}]))
    _add(db, "dr1", "event", event_type="delegate_result",
         event_data=_delegate_event(task_name="gates", output_text="Now I will run",
                                    status="failed", report_missing=True))
    _add(db, "dr1", "assistant", "both lanes answered")

    seed = build_history_seed("dr1")
    assert ("[delegate result: worker · review · completed] the report"
            " · files: workspace/inbox/worker/r.md") in seed
    assert "[delegate result: worker · gates · ended without a report] Now I will run" in seed
    assert seed.index("User: split the review") < seed.index("· review ·") < seed.index("Assistant: both")


def test_seed_caps_a_delegate_line_whole_keeping_the_reports_end(temp_db):
    from core.session.history_seed import SEED_PER_MESSAGE_CHARS, build_history_seed
    from storage import database as db
    db.create_chat("dr2", "user-1", "agent-x")
    files = [{"path": f"workspace/inbox/w/file-{i:03d}.md", "bytes": 1} for i in range(400)]
    _add(db, "dr2", "event", event_type="delegate_result",
         event_data=_delegate_event(output_text="START " + "x" * 9000 + " THE-END", files=files))
    _add(db, "dr2", "assistant", "ok")

    seed = build_history_seed("dr2")
    line = next(ln for ln in seed.splitlines() if ln.startswith("[delegate result:"))
    assert len(line) <= SEED_PER_MESSAGE_CHARS
    assert "THE-END" in line and "START" not in line
    assert "file-000.md" in line and "file-399.md" not in line


def test_seed_caps_a_delegate_line_with_a_long_task_name(temp_db):
    from core.session.history_seed import SEED_PER_MESSAGE_CHARS, build_history_seed
    from storage import database as db
    db.create_chat("dr4", "user-1", "agent-x")
    _add(db, "dr4", "event", event_type="delegate_result",
         event_data=_delegate_event(task_name="t" * 3000, agent="a" * 3000,
                                    output_text="the report's END"))
    _add(db, "dr4", "assistant", "ok")

    line = next(ln for ln in build_history_seed("dr4").splitlines()
                if ln.startswith("[delegate result:"))
    assert len(line) <= SEED_PER_MESSAGE_CHARS
    assert line.endswith("the report's END")


def test_seed_skips_a_result_callback_stored_as_a_user_row(temp_db):
    """An interactive delegating chat's tailer stores the callback prompt as
    a user row: its delegate line carries it once."""
    from core.session.history_seed import build_history_seed
    from storage import database as db
    db.create_chat("dr3", "user-1", "agent-x")
    _add(db, "dr3", "user", "go")
    _add(db, "dr3", "event", event_type="delegate_result", event_data=_delegate_event())
    _add(db, "dr3", "user", "[BACKGROUND_TASK_RESULT]\nTask 'review' finished\n\nOutput:\nthe report")
    _add(db, "dr3", "user", "[Current time: Friday]\n\n[The worker ended without a report: x]\n\nmore")
    _add(db, "dr3", "user", "[DELEGATED_WORK] is a word I typed")
    _add(db, "dr3", "assistant", "done")

    seed = build_history_seed("dr3")
    assert seed.count("the report") == 1
    assert "BACKGROUND_TASK_RESULT" not in seed and "ended without a report: x" not in seed
    assert "User: [DELEGATED_WORK] is a word I typed" in seed


def test_a_refused_resume_reseeds_the_chat(temp_db):
    """The engine opened a new session instead of the one the chat names:
    the next turn restores the history with the resume_failed card."""
    from core.session.history_seed import consume_pending_seed, mark_resume_refused
    from storage import database as db
    db.create_chat("c-rr", "user-1", "agent-x")
    _add(db, "c-rr", "user", "what we said before")
    _add(db, "c-rr", "assistant", "the answer before")
    mark_resume_refused("c-rr")
    assert db.get_chat("c-rr")["pending_history_seed"] == "resume_failed"
    text, notice = consume_pending_seed("c-rr", "go on")
    assert "could not be resumed" in notice and "the answer before" in text


def test_a_revive_on_a_new_thread_reseeds_the_chat_bound_to_the_session(temp_db):
    import asyncio

    from core.layers.codex import remote as codex_remote
    from storage import database as db
    db.create_chat("c-rv", "user-1", "agent-x")
    db.update_chat("c-rv", session_id="sid-rv")
    asyncio.run(codex_remote._reseed_after_revive("sid-rv"))
    assert db.get_chat("c-rv")["pending_history_seed"] == "resume_failed"
    asyncio.run(codex_remote._reseed_after_revive("sid-nobody"))   # no chat: nothing

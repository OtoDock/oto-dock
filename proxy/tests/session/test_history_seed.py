"""DB-history seed for fresh sessions (core/session/history_seed.py).

Covers the digest builder (rendering rules, caps, budget, trailing-user skip),
the atomic pending-seed claim, the consume flow (prepend + notice row + flag
clear), and the chat transitions that produce the flag: delete_remote_machine
(all pinned chats) and rebind_chat_to_current_target (one chat — the
"Move this chat" escape hatch).
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
    from storage import database as db
    from core.session.history_seed import build_history_seed
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
    from storage import database as db
    from core.session.history_seed import build_history_seed
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
    from storage import database as db
    from core.session.history_seed import build_history_seed
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
    from storage import database as db
    from core.session.history_seed import build_history_seed
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
    from storage import database as db
    from core.session.history_seed import build_history_seed
    db.create_chat("c7", "user-1", "agent-x")
    for i in range(20):
        _add(db, "c7", "user", f"question number {i:02d} " + "pad " * 30)
        _add(db, "c7", "assistant", f"answer number {i:02d} " + "pad " * 30)

    seed = build_history_seed("c7", max_chars=600)
    assert "answer number 19" in seed       # newest survives
    assert "question number 00" not in seed  # oldest dropped
    assert len(seed) < 600 + 600  # content budget + wrapper overhead


def test_seed_empty_chat(temp_db):
    from storage import database as db
    from core.session.history_seed import build_history_seed
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
    from storage import database as db
    from core.session.history_seed import consume_pending_seed
    db.create_chat("c10", "user-1", "agent-x")
    _add(db, "c10", "user", "history msg")
    _add(db, "c10", "assistant", "history reply")

    text, notice = consume_pending_seed("c10", "new prompt")
    assert text == "new prompt"
    assert notice == ""
    # No notice row persisted.
    assert all(m["event_type"] != "system" for m in db.get_chat_messages("c10"))


def test_consume_machine_removed_flow(temp_db):
    from storage import database as db
    from core.session.history_seed import consume_pending_seed
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
    from storage import database as db
    from core.session.history_seed import consume_pending_seed
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
    from storage import database as db
    from core.session.history_seed import consume_pending_seed
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
    from storage import database as db
    from core.session.history_seed import consume_pending_seed
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
    from storage import database as db
    from core.session.history_seed import consume_pending_seed
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
    from storage import database as db
    from storage import remote_store

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
# rebind_chat_to_current_target — the per-chat "Move this chat" transition
# ---------------------------------------------------------------------------

def test_rebind_chat_transitions_one_chat(temp_db):
    """The user-facing move op applies the machine-removed shape to ONE chat:
    pin cleared (next warmup fresh-resolves + re-stamps), session ids dropped,
    abort flags + context meter reset, and 'moved:<from_label>' queued for the
    history-seed injection. Siblings on the same machine are untouched."""
    from storage import database as db
    from storage import remote_store

    _insert_machine("m-office", "Office-PC")

    db.create_chat("moving", "user-1", "agent-x")
    db.update_chat("moving", execution_target="m-office", session_id="sess-1",
                   codex_thread_id="thread-1", last_turn_aborted=True,
                   context_used=120000)
    db.create_chat("staying", "user-1", "agent-x")
    db.update_chat("staying", execution_target="m-office", session_id="sess-2")

    assert remote_store.rebind_chat_to_current_target(
        "moving", "Office-PC") is True

    moved = db.get_chat("moving")
    assert moved["execution_target"] == ""
    assert moved["session_id"] is None
    assert moved["codex_thread_id"] is None
    assert moved["last_turn_aborted"] is False
    assert moved["context_used"] == 0
    assert moved["pending_history_seed"] == "moved:Office-PC"

    staying = db.get_chat("staying")
    assert staying["execution_target"] == "m-office"
    assert staying["session_id"] == "sess-2"
    assert staying["pending_history_seed"] == ""


def test_rebind_unknown_chat_returns_false(temp_db):
    from storage import remote_store
    assert remote_store.rebind_chat_to_current_target(
        "no-such-chat", "Office-PC") is False

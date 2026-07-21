"""Phone session→chat linkage — the recovery mechanism the phone WS warmup
reuse path depends on.

A phone call pre-warms an LLM session, and a daemon reconnect mid-call sends a
reuse-warmup on a fresh WS connection. That connection recovers the call's
chat_id via ``get_chat_by_session``; without the session→chat link persisted at
warmup, recovery fails and every chat turn 404s ("No session — send warmup
first"). These tests pin the link + reverse lookup.
"""


from storage import database as db


def test_phone_chat_recoverable_by_session(temp_db):
    session_id = "sess-abc-123"
    chat_id = "chat-xyz-789"
    db.create_chat(chat_id, "phone", "caller", source_type="phone")
    # No link yet → not recoverable (reproduces the bug's precondition).
    assert db.get_chat_by_session(session_id) is None

    # Warmup persists the link → the reused connection can recover the chat.
    db.update_chat(chat_id, session_id=session_id)
    recovered = db.get_chat_by_session(session_id)
    assert recovered is not None
    assert recovered["id"] == chat_id
    assert recovered["source_type"] == "phone"


def test_get_chat_by_session_returns_most_recent(temp_db):
    sid = "sess-shared"
    db.create_chat("chat-old", "phone", "caller", source_type="phone")
    db.create_chat("chat-new", "phone", "caller", source_type="phone")
    db.update_chat("chat-old", session_id=sid)
    db.update_chat("chat-new", session_id=sid)
    # Reverse lookup takes the freshest row (update_chat bumps updated_at).
    assert db.get_chat_by_session(sid)["id"] == "chat-new"

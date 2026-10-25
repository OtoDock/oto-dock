"""Interactive-session turn-end stamps run as ONE ordered job on the DB
executor: ``last_response_at`` first, then the otodock-attached read mark —
never on the event loop, never reordered (storage/pg.py rule; the 09-04
plan's §3.6)."""

import asyncio
import threading
import time

import pytest


@pytest.mark.asyncio
async def test_turn_end_stamps_are_one_ordered_off_loop_job(loop_db_guard, monkeypatch):
    from core.session.interactive_session import InteractiveSession
    from services.notifications import notification_manager
    from storage import database as task_store

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(task_store, "update_chat",
                        lambda cid, **kw: calls.append(("update_chat", threading.get_ident())))
    monkeypatch.setattr(task_store, "mark_chat_read",
                        lambda cid, owner: calls.append(("mark_chat_read", threading.get_ident())))
    monkeypatch.setattr(notification_manager, "broadcast_chat_status", lambda *a, **k: None)
    monkeypatch.setattr(notification_manager, "broadcast_chat_read", lambda *a, **k: None)

    sess = InteractiveSession(
        session_id="s1", chat_id="c1", agent_name="alpha", user_sub="user-admin",
        chat_row={"title_generated": True, "user_sub": "user-admin"},
    )
    sess.otodock_attached = True
    assert sess._chat_owner() == "user-admin"   # seeded — no lazy DB read

    with loop_db_guard.active():
        sess._turn_end_effects()

    deadline = time.monotonic() + 3
    while len(calls) < 2 and time.monotonic() < deadline:  # noqa: ASYNC110  # the recorded calls list: the offloaded work has no event
        await asyncio.sleep(0.02)
    assert [c[0] for c in calls] == ["update_chat", "mark_chat_read"]
    assert all(c[1] != threading.get_ident() for c in calls)

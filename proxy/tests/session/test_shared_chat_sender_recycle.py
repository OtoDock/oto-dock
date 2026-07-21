"""Sender-pays session recycle on Shared-only chats.

A Shared-only chat's live session is bound to the subscription + identity of
whoever warmed it. When the chat changes hands BETWEEN turns, ``_handle_chat``
must close the live session and re-warm under the new sender — so one user's
turns never spend another user's account — and the first turn after the
change of hands carries a speaker-transition note. A send by the SAME payer
must keep the live session (no churn).

Run: env TEST_DATABASE_URL=... venv/bin/pytest tests/session/test_shared_chat_sender_recycle.py -q
"""

import uuid

from core.events.common_events import CommonEvent, TEXT, DONE

from tests.fixtures.ws_dashboard_harness import (
    FakeExecutionLayer,
    dashboard_connection,
    drain_startup,
    make_test_agent,
    run_ws_scenario,
    session_cookie,
    set_username,
    stub_dashboard_seams,
)


def _make_shared_chat(slug: str, *, session_id: str,
                      messages: tuple[tuple[str, str, str], ...] = ()) -> str:
    """Chat owned by the synthetic ``agent::{slug}``; messages carry the REAL
    author (the (role, content, author_sub) triples)."""
    from storage import database as task_store
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, f"agent::{slug}", slug, "default",
                           model="claude-test-model",
                           execution_path="claude-code-cli")
    task_store.update_chat(cid, session_id=session_id)
    for role, content, author in messages:
        task_store.add_chat_message(cid, role, content, author_sub=author)
    return cid


def _setup(monkeypatch):
    """Shared-only agent + a live session warmed (and paid) by user-admin."""
    from core.session.session_state import set_session_mode
    from services.engines import subscription_pool as sp
    from storage import database as task_store

    layer = FakeExecutionLayer()
    layer.turn_events = [
        CommonEvent(type=TEXT, data={"text": "ok"}),
        CommonEvent(type=DONE, data={}),
    ]
    stub_dashboard_seams(monkeypatch, layer)
    slug = make_test_agent(default_scope="agent", collaborative=False)
    set_username("user-admin", "admin")
    set_username("user-manager", "manager")
    task_store.set_user_agents("user-manager", [slug], "user-admin",
                               agent_roles={slug: "manager"})

    live_sid = str(uuid.uuid4())
    layer.alive.add(live_sid)
    set_session_mode(live_sid, "default")
    cid = _make_shared_chat(slug, session_id=live_sid,
                            messages=(("user", "hi", "user-admin"),
                                      ("assistant", "hello", "")))
    sp.bind_session(live_sid, "sub-a", layer="claude-code-cli",
                    user_sub="user-admin")
    return layer, slug, live_sid, cid


def _clear_pool_maps():
    from services.engines import subscription_pool as sp
    with sp._session_maps_lock:
        sp._session_subscriptions.clear()
        sp._session_binding_ctx.clear()
        sp._session_scope_keys.clear()


async def _drain_until(ws, frame_type: str, limit: int = 40) -> list[dict]:
    frames = []
    for _ in range(limit):
        frame = await ws.next_frame()
        frames.append(frame)
        if frame.get("type") == frame_type:
            return frames
    raise AssertionError(f"no {frame_type!r} frame within {limit} frames: "
                         f"{[f.get('type') for f in frames]}")


class TestSenderChangeRecycle:
    def test_new_sender_recycles_session_and_gets_transition_note(
            self, temp_db, monkeypatch):
        layer, slug, live_sid, cid = _setup(monkeypatch)

        async def scenario():
            cookie = session_cookie(sub="user-manager", email="manager@test.com",
                                    name="Manager User", role="creator")
            async with dashboard_connection(cookie) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                # history → warmup_ready (live re-attach) → queue_snapshot
                await _drain_until(ws, "queue_snapshot")

                ws.client_send({"type": "chat", "text": "hello from B"})
                await _drain_until(ws, "done")

        run_ws_scenario(scenario)
        _clear_pool_maps()

        # The A-paid session was recycled, a fresh one spawned for B …
        assert live_sid in layer.closed_sessions
        new_sids = [sid for sid, _ in layer.started if sid != live_sid]
        assert new_sids, f"no fresh session spawned (started={layer.started})"
        # … and B's turn ran on the NEW session, carrying the transition note.
        assert layer.messages, "turn was never sent to the layer"
        sent_sid, prompt = layer.messages[-1][0], layer.messages[-1][1]
        assert sent_sid == new_sids[-1]
        assert "Manager User" in prompt
        assert "hello from B" in prompt

    def test_same_payer_keeps_live_session(self, temp_db, monkeypatch):
        layer, slug, live_sid, cid = _setup(monkeypatch)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:  # user-admin
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")

                ws.client_send({"type": "chat", "text": "still me"})
                await _drain_until(ws, "done")

        run_ws_scenario(scenario)
        _clear_pool_maps()

        assert layer.closed_sessions == []
        assert layer.started == []  # the live session was reused, no re-warm
        sent_sid, prompt = layer.messages[-1][0], layer.messages[-1][1]
        assert sent_sid == live_sid
        # Same sender → no transition note.
        assert "message is from" not in prompt

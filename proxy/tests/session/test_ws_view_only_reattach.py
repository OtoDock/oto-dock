"""A view-only re-attach records no owner for the session.

A resume re-acquires the chat's concurrency slot, and a re-acquire that
names a person records them as the session's owner when none is recorded
(a background wake's reservation, one the reconciler dropped). A viewer who
may open a chat but not drive it (a Shared-only pool chat below the editor
tier) is never that owner: the session would count against their per-person
cap and become a candidate of their own-cap self-eviction.

Run: cd proxy && venv/bin/pytest tests/session/test_ws_view_only_reattach.py -v
"""

from __future__ import annotations

import asyncio
import uuid

from core.session.visibility import shared_chat_owner
from storage import database as task_store
from tests.fixtures.ws_dashboard_harness import (
    FakeExecutionLayer,
    dashboard_connection,
    drain_startup,
    make_test_agent,
    run_ws_scenario,
    session_cookie,
    stub_dashboard_seams,
)

import ws.dashboard  # noqa: F401  (resolves the dashboard and dashboard_chat cycle)


def _pool_chat_with_live_session(layer: FakeExecutionLayer, role: str) -> tuple[str, str]:
    slug = make_test_agent(default_scope="agent", collaborative=False)
    task_store.add_user_agent("user-viewer", slug, role, "test")
    sid = str(uuid.uuid4())
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, shared_chat_owner(slug), slug)
    task_store.update_chat(cid, session_id=sid)
    layer.alive.add(sid)
    return cid, sid


def _resume_owner(cookie: str, cid: str, sid: str, slots) -> list:
    async def scenario():
        async with dashboard_connection(cookie) as ws:
            await drain_startup(ws)
            ws.client_send({"type": "resume_chat", "chat_id": cid})
            for _ in range(100):
                if any(s == sid for s, _kw in slots.acquired):
                    break
                await asyncio.sleep(0.02)
    run_ws_scenario(scenario)
    return [kw.get("user_sub") for s, kw in slots.acquired if s == sid]


def test_a_view_only_resume_records_no_owner(temp_db, monkeypatch):
    layer = FakeExecutionLayer()
    slots = stub_dashboard_seams(monkeypatch, layer)
    cid, sid = _pool_chat_with_live_session(layer, "viewer")
    viewer = session_cookie(sub="user-viewer", email="viewer@test.com",
                            name="Viewer User", role="member")
    owners = _resume_owner(viewer, cid, sid, slots)
    assert owners and all(o is None for o in owners)


def test_a_driver_resume_still_records_the_owner(temp_db, monkeypatch):
    layer = FakeExecutionLayer()
    slots = stub_dashboard_seams(monkeypatch, layer)
    cid, sid = _pool_chat_with_live_session(layer, "editor")
    driver = session_cookie(sub="user-viewer", email="viewer@test.com",
                            name="Viewer User", role="member")
    owners = _resume_owner(driver, cid, sid, slots)
    assert owners and all(o == "user-viewer" for o in owners)

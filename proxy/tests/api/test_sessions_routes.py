"""The session-control routes (``api/sessions/sessions.py``): a session
token is judged on its holder (the person it was minted for must still
exist and the token must be current), and ``implement-plan`` no longer
exists (no first-party caller; the dashboard's WS frame builds the
implementation session through the session builders).

Run: cd proxy && venv/bin/pytest tests/api/test_sessions_routes.py -v
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.sessions import sessions
from app import app
from auth import token_holder
from auth.session_token import validate_session_token
from tests.conftest import live_session_token

SID = "44444444-4444-4444-4444-444444444444"


@pytest.fixture(autouse=True)
def _fresh_holder_answers():
    token_holder.reset_for_tests()
    yield
    token_holder.reset_for_tests()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _cookie() -> dict:
    from tests.fixtures.ws_dashboard_harness import session_cookie
    return {"Cookie": f"session={session_cookie('user-admin')}"}


def test_implement_plan_is_gone(temp_db):
    r = TestClient(app).post(f"/v1/sessions/{SID}/implement-plan",
                             json={"plan_path": "x.md", "mode": "acceptEdits"},
                             headers=_cookie())
    # 404, or 405 when another router's session pattern matches the path.
    assert r.status_code in (404, 405)


def test_the_session_control_routes_are_off_a_session_tokens_list(temp_db):
    """No tool calls the session control routes (pending, abort) with a
    session token: the list refuses them with its sentence before the
    route runs, for a person's token and a no-person one
    (``auth/external_endpoints.SESSION_ROUTES``)."""
    from auth.external_endpoints import SESSION_ROUTE_DETAIL
    for token in (live_session_token(SID, "pa", user_sub="user-admin"), live_session_token(SID, "pa")):
        for method, path in (("GET", f"/v1/sessions/{SID}/pending"), ("POST", f"/v1/sessions/{SID}/abort"),
                             ("GET", "/v1/models")):
            r = TestClient(app).request(method, path, headers=_bearer(token))
            assert (r.status_code, r.json()["detail"]) == (403, SESSION_ROUTE_DETAIL), (method, path)


@pytest.mark.asyncio
async def test_abort_and_close_end_a_direct_llm_session_and_find_no_other(temp_db, monkeypatch):
    """Both routes close a session Direct LLM's pool holds. A session a
    machine runs is in no pool of the platform's: the answer is
    ``not_found``, nothing is sent to its machine and its record stays (its
    Stop and its close go through its chat)."""
    from unittest.mock import AsyncMock, MagicMock

    from core.layers.direct import session as direct_session
    from core.remote.remote_execution import RemoteExecutionLayer
    from core.remote.remote_session_info import RemoteSessionInfo
    from core.session import session_manager
    token = f"Bearer {live_session_token(SID, 'pa', user_sub='user-admin')}"
    routes = ((sessions.abort_session_endpoint, "aborted"), (sessions.close_session_endpoint, "closed"))
    tools_closed = AsyncMock()
    monkeypatch.setattr(direct_session.mcp_pool, "close_session", tools_closed)
    for route, word in routes:
        monkeypatch.setitem(direct_session._direct_sessions, SID, object())
        assert await route(SID, authorization=token) == {"status": word, "session_id": SID}
        assert SID not in direct_session._direct_sessions
        assert await route(SID, authorization=token) == {"status": "not_found", "session_id": SID}
    assert [c.args for c in tools_closed.await_args_list] == [(SID,), (SID,)]

    cm = MagicMock()
    cm.send_command = AsyncMock()
    cm.send_fire_and_forget = AsyncMock()
    remote = RemoteExecutionLayer(cm)
    remote._sessions[SID] = RemoteSessionInfo(
        session_id=SID, machine_id="m-1", agent_name="pa", execution_path="claude-code-cli",
        event_queue=asyncio.Queue())
    monkeypatch.setattr(session_manager, "_remote_layer", remote)
    for route, _word in routes:
        assert await route(SID, authorization=token) == {"status": "not_found", "session_id": SID}
    assert SID in remote._sessions and remote._sessions[SID].alive
    cm.send_command.assert_not_awaited()
    cm.send_fire_and_forget.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_token_whose_person_is_gone_is_refused_by_the_session_routes(temp_db):
    """The routes' own holder judgment, asked as the route asks it (the
    middleware's list stands in front of the HTTP path)."""
    ghost = live_session_token(SID, "pa", user_sub="user-nobody")
    with pytest.raises(HTTPException) as ei:
        await sessions.get_session_pending(SID, authorization=f"Bearer {ghost}")
    assert ei.value.status_code == 401
    # The same route with a living person's token passes the auth and
    # answers about the session itself.
    alive = live_session_token(SID, "pa", user_sub="user-admin")
    with pytest.raises(HTTPException) as ei:
        await sessions.get_session_pending(SID, authorization=f"Bearer {alive}")
    assert ei.value.status_code == 404 and ei.value.detail == "No pending result"
    # A token with no person (an agent-scope run) is not judged here.
    service = live_session_token(SID, "pa")
    with pytest.raises(HTTPException) as ei:
        await sessions.get_session_pending(SID, authorization=f"Bearer {service}")
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_the_verifiers_refuse_a_gone_holder_and_keep_the_sid_check(temp_db):
    """Both verifiers judge the token's holder on the fast lane; the sid
    cross-check and the master key are unchanged."""
    ghost = live_session_token(SID, "pa", user_sub="user-nobody")
    with pytest.raises(HTTPException) as ei:
        await sessions.verify_session_match_async(f"Bearer {ghost}", SID)
    assert ei.value.status_code == 401
    with pytest.raises(HTTPException) as ei:
        await sessions.verify_api_key_async(f"Bearer {ghost}")
    assert ei.value.status_code == 401
    with pytest.raises(HTTPException) as ei:
        await sessions.verify_session_match_async(f"Bearer {ghost}", "another-sid")
    assert ei.value.status_code == 403
    import config
    await sessions.verify_session_match_async(f"Bearer {config.API_KEY}", "any-sid")
    alive = live_session_token(SID, "pa", user_sub="user-admin")
    await sessions.verify_session_match_async(f"Bearer {alive}", SID)
    await sessions.verify_api_key_async(f"Bearer {alive}")


@pytest.mark.asyncio
async def test_a_refusal_outlives_the_answers_bound(temp_db, monkeypatch):
    """A refused holder never becomes valid again for the same token: the
    refusal holds for the token's life and a full answer table drops the
    passes, never a refusal."""
    monkeypatch.setattr(token_holder, "ANSWERS_MAX", 8)
    monkeypatch.setattr(token_holder, "HOLDER_TTL_S", 0.0)
    ghost = live_session_token(SID, "pa", user_sub="user-nobody")
    with pytest.raises(HTTPException):
        await sessions.verify_session_match_async(f"Bearer {ghost}", SID)
    now = int(time.time())
    for i in range(12):
        assert await token_holder.holder_ok({"user_sub": "user-admin", "iat": now + i})
    assert len(token_holder._answers) <= 8
    key = token_holder._key(validate_session_token(ghost))
    assert key in token_holder._answers and token_holder._answers[key][0] is False
    with pytest.raises(HTTPException) as ei:
        await sessions.verify_session_match_async(f"Bearer {ghost}", SID)
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_forget_drops_a_persons_answers(temp_db):
    alive = live_session_token(SID, "pa", user_sub="user-admin")
    await sessions.verify_api_key_async(f"Bearer {alive}")
    assert any(k[0] == "user-admin" for k in token_holder._answers)
    token_holder.forget("user-admin")
    assert not any(k[0] == "user-admin" for k in token_holder._answers)


@pytest.mark.asyncio
async def test_a_non_ascii_session_id_is_refused_not_an_error(temp_db):
    token = live_session_token(SID, "pa", user_sub="user-admin")
    r = TestClient(app).get("/v1/sessions/%C3%A9/pending", headers=_bearer(token))
    assert r.status_code == 403
    with pytest.raises(HTTPException) as ei:
        await sessions.verify_session_match_async(f"Bearer {token}", "sessión")
    assert ei.value.status_code == 403


@pytest.mark.parametrize("path, body", [
    ("/v1/hooks/permission", {"tool_name": "Read", "tool_input": {"file_path": "/workspace/x"}}),
    ("/v1/hooks/resolve-path", {"path": "/workspace/x"}),
    ("/v1/hooks/url", {"url": "https://example.com", "title": "t"}),
    ("/v1/hooks/apps/list", {}),
])
def test_a_hook_route_refuses_a_token_whose_holder_is_gone(temp_db, monkeypatch, path, body):
    """The hook routes (permission, paths, artifacts, pins) judge the token's
    holder like the session routes: a person who no longer exists is refused
    before the route runs, a living person's token reaches the route."""
    from unittest.mock import AsyncMock

    from api.hooks import permission
    monkeypatch.setattr(permission, "decide_tool_permission", AsyncMock(return_value={"decision": "allow"}))
    ghost = live_session_token(SID, "pa", user_sub="user-nobody")
    r = TestClient(app).post(path, json={"session_id": SID, **body}, headers=_bearer(ghost))
    assert r.status_code == 401
    alive = live_session_token(SID, "pa", user_sub="user-admin")
    r = TestClient(app).post(path, json={"session_id": SID, **body}, headers=_bearer(alive))
    assert r.status_code != 401

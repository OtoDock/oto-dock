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
from auth.session_token import create_session_token

SID = "44444444-4444-4444-4444-444444444444"


@pytest.fixture(autouse=True)
def _fresh_holder_answers():
    sessions._holder_answers.clear()
    yield
    sessions._holder_answers.clear()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_implement_plan_is_gone(temp_db):
    token = create_session_token(SID, "pa", user_sub="user-admin")
    r = TestClient(app).post(f"/v1/sessions/{SID}/implement-plan",
                             json={"plan_path": "x.md", "mode": "acceptEdits"},
                             headers=_bearer(token))
    # 404, or 405 when another router's session pattern matches the path.
    assert r.status_code in (404, 405)


def test_a_token_whose_person_is_gone_is_refused_by_the_session_routes(temp_db):
    ghost = create_session_token(SID, "pa", user_sub="user-nobody")
    r = TestClient(app).get(f"/v1/sessions/{SID}/pending", headers=_bearer(ghost))
    assert r.status_code == 401
    # The same route with a living person's token passes the auth and
    # answers about the session itself.
    alive = create_session_token(SID, "pa", user_sub="user-admin")
    r = TestClient(app).get(f"/v1/sessions/{SID}/pending", headers=_bearer(alive))
    assert r.status_code == 404 and r.json()["detail"] == "No pending result"
    # A token with no person (an agent-scope run) is not judged here.
    service = create_session_token(SID, "pa")
    r = TestClient(app).get(f"/v1/sessions/{SID}/pending", headers=_bearer(service))
    assert r.status_code == 404


def test_the_synchronous_verifier_passes_a_miss_and_refuses_a_seen_holder(temp_db):
    """The synchronous shape never reads the store itself: it stands by the
    last answer a route or a check reached for the token's holder. Outside
    a running loop a miss starts no check and passes."""
    ghost = create_session_token(SID, "pa", user_sub="user-nobody")
    sessions.verify_session_match(f"Bearer {ghost}", SID)      # a miss passes
    sessions.verify_api_key(f"Bearer {ghost}")
    TestClient(app).get(f"/v1/sessions/{SID}/pending", headers=_bearer(ghost))
    with pytest.raises(HTTPException) as ei:
        sessions.verify_session_match(f"Bearer {ghost}", SID)
    assert ei.value.status_code == 401
    with pytest.raises(HTTPException) as ei:
        sessions.verify_api_key(f"Bearer {ghost}")
    assert ei.value.status_code == 401
    # The sid cross-check and the master key are unchanged.
    with pytest.raises(HTTPException) as ei:
        sessions.verify_session_match(f"Bearer {ghost}", "another-sid")
    assert ei.value.status_code == 403


async def _settled() -> None:
    for _ in range(250):
        if not getattr(sessions, "_holder_pending", None):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the holder check never finished")


@pytest.mark.asyncio
async def test_a_synchronous_miss_checks_the_holder_for_the_next_use(temp_db):
    """The hook routes still on the synchronous verifier (tool-result, stop,
    subagent, file-written, permission-response, location, images/temp,
    document-preview): a token whose person is gone is refused from its
    next use on, not for the rest of its day."""
    ghost = create_session_token(SID, "pa", user_sub="user-nobody")
    sessions.verify_session_match(f"Bearer {ghost}", SID)
    await _settled()
    with pytest.raises(HTTPException) as ei:
        sessions.verify_session_match(f"Bearer {ghost}", SID)
    assert ei.value.status_code == 401
    with pytest.raises(HTTPException) as ei:
        sessions.verify_api_key(f"Bearer {ghost}")
    assert ei.value.status_code == 401
    # A living person's token keeps passing.
    alive = create_session_token(SID, "pa", user_sub="user-admin")
    sessions.verify_session_match(f"Bearer {alive}", SID)
    await _settled()
    sessions.verify_session_match(f"Bearer {alive}", SID)


@pytest.mark.asyncio
async def test_a_refusal_outlives_the_answers_bound(temp_db, monkeypatch):
    """A refused holder never becomes valid again for the same token: the
    refusal holds for the token's life and a full answer table drops the
    passes, never a refusal."""
    monkeypatch.setattr(sessions, "_HOLDER_ANSWERS_MAX", 8)
    monkeypatch.setattr(sessions, "HOLDER_TTL_S", 0.0)
    ghost = create_session_token(SID, "pa", user_sub="user-nobody")
    with pytest.raises(HTTPException):
        await sessions.verify_session_match_async(f"Bearer {ghost}", SID)
    now = int(time.time())
    for i in range(12):
        assert await sessions._holder_ok({"user_sub": "user-admin", "iat": now + i})
    assert len(sessions._holder_answers) <= 8
    with pytest.raises(HTTPException) as ei:
        sessions.verify_session_match(f"Bearer {ghost}", SID)
    assert ei.value.status_code == 401


def test_a_non_ascii_session_id_is_refused_not_an_error(temp_db):
    token = create_session_token(SID, "pa", user_sub="user-admin")
    r = TestClient(app).get("/v1/sessions/%C3%A9/pending", headers=_bearer(token))
    assert r.status_code == 403
    with pytest.raises(HTTPException) as ei:
        sessions.verify_session_match(f"Bearer {token}", "sessión")
    assert ei.value.status_code == 403


def test_the_plan_routes_never_serve_the_service_accounts_own_plans(temp_db, tmp_path,
                                                                    monkeypatch):
    """A session with no plans folder of its own (none registered, or not
    made yet) lists nothing and reads nothing: the proxy account's own
    ``~/.claude/plans`` belong to no session, and neither does it answer
    the master key without one."""
    import config
    plans = tmp_path / "home" / ".claude" / "plans"
    plans.mkdir(parents=True)
    (plans / "operator.md").write_text("private")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "API_KEY", "master-test-key")
    client = TestClient(app)
    token = create_session_token(SID, "pa", user_sub="user-admin")
    r = client.get(f"/v1/plans?session_id={SID}", headers=_bearer(token))
    assert r.status_code == 200 and r.json() == {"plans": []}
    r = client.get(f"/v1/plans/operator.md?session_id={SID}", headers=_bearer(token))
    assert r.status_code == 404
    master = "Bearer master-test-key"
    assert asyncio.run(sessions.list_plans(master, None)) == {"plans": []}
    with pytest.raises(HTTPException) as ei:
        asyncio.run(sessions.get_plan_file("operator.md", master, None))
    assert ei.value.status_code == 404


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
    ghost = create_session_token(SID, "pa", user_sub="user-nobody")
    r = TestClient(app).post(path, json={"session_id": SID, **body}, headers=_bearer(ghost))
    assert r.status_code == 401
    alive = create_session_token(SID, "pa", user_sub="user-admin")
    r = TestClient(app).post(path, json={"session_id": SID, **body}, headers=_bearer(alive))
    assert r.status_code != 401

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


def test_implement_plan_is_gone(temp_db):
    token = live_session_token(SID, "pa", user_sub="user-admin")
    r = TestClient(app).post(f"/v1/sessions/{SID}/implement-plan",
                             json={"plan_path": "x.md", "mode": "acceptEdits"},
                             headers=_bearer(token))
    # 404, or 405 when another router's session pattern matches the path.
    assert r.status_code in (404, 405)


def test_a_token_whose_person_is_gone_is_refused_by_the_session_routes(temp_db):
    ghost = live_session_token(SID, "pa", user_sub="user-nobody")
    r = TestClient(app).get(f"/v1/sessions/{SID}/pending", headers=_bearer(ghost))
    assert r.status_code == 401
    # The same route with a living person's token passes the auth and
    # answers about the session itself.
    alive = live_session_token(SID, "pa", user_sub="user-admin")
    r = TestClient(app).get(f"/v1/sessions/{SID}/pending", headers=_bearer(alive))
    assert r.status_code == 404 and r.json()["detail"] == "No pending result"
    # A token with no person (an agent-scope run) is not judged here.
    service = live_session_token(SID, "pa")
    r = TestClient(app).get(f"/v1/sessions/{SID}/pending", headers=_bearer(service))
    assert r.status_code == 404


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
    token = live_session_token(SID, "pa", user_sub="user-admin")
    r = client.get(f"/v1/plans?session_id={SID}", headers=_bearer(token))
    assert r.status_code == 200 and r.json() == {"plans": []}
    r = client.get(f"/v1/plans/operator.md?session_id={SID}", headers=_bearer(token))
    assert r.status_code == 404
    master = "Bearer master-test-key"
    assert asyncio.run(sessions.list_plans(master, None)) == {"plans": []}
    with pytest.raises(HTTPException) as ei:
        asyncio.run(sessions.get_plan_file("operator.md", master, None))
    assert ei.value.status_code == 404


class _NoMachine:
    """A connection manager the plan routes must never reach."""

    def __getattr__(self, name):
        raise AssertionError(f"the plan routes asked the machine ({name})")


@pytest.mark.asyncio
async def test_a_remote_session_answers_its_plans_from_the_chat_rows(temp_db, monkeypatch,
                                                                     loop_db_guard):
    """A remote session's plans are the ones its chat reviewed (the rows the
    pump writes at plan review, remote chats included): listed and read from
    the database in a worker, never asked of the machine and never cached on
    the proxy's disk."""
    from types import SimpleNamespace
    import config
    from core.remote import satellite_connection
    from storage import database as task_store
    task_store.create_chat("chat-remote-plans", "user-admin", "pa")
    task_store.update_chat("chat-remote-plans", session_id=SID)
    task_store.add_chat_plan("chat-remote-plans", "first.md", "# first\n")
    task_store.add_chat_plan("chat-remote-plans", "second.md", "# second, édition\n")
    info = SimpleNamespace(machine_id="m1", agent_name="pa")
    monkeypatch.setattr(sessions, "_get_remote_session_info",
                        lambda sid: info if sid == SID else None)
    monkeypatch.setattr(satellite_connection, "get_connection_manager", _NoMachine)
    bearer = f"Bearer {live_session_token(SID, 'pa', user_sub='user-admin')}"
    with loop_db_guard.active():
        listed = (await sessions.list_plans(bearer, SID))["plans"]
        read = await sessions.get_plan_file("second.md", bearer, SID)
        with pytest.raises(HTTPException) as ei:
            await sessions.get_plan_file("other.md", bearer, SID)
    assert [p["filename"] for p in listed] == ["second.md", "first.md"]  # newest first
    assert listed[0]["size"] == len("# second, édition\n".encode("utf-8"))
    assert all(isinstance(p["modified"], float) and p["modified"] > 0 for p in listed)
    assert read == {"content": "# second, édition\n", "filename": "second.md"}
    assert ei.value.status_code == 404
    assert not (config.SESSIONS_DIR / "remote-plans" / SID).exists()
    # A remote session whose chat reviewed no plan lists nothing.
    other = "55555555-5555-5555-5555-555555555555"
    monkeypatch.setattr(sessions, "_get_remote_session_info", lambda sid: info)
    other_bearer = f"Bearer {live_session_token(other, 'pa', user_sub='user-admin')}"
    assert await sessions.list_plans(other_bearer, other) == {"plans": []}


def _loop_witness(monkeypatch, root) -> list[tuple[str, bool]]:
    """Record, for every filesystem call on a path under ``root``, whether it
    ran on an event loop's thread."""
    import builtins
    import io
    import os

    seen: list[tuple[str, bool]] = []

    def on_loop() -> bool:
        try:
            asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False

    for mod, name in ((os, "stat"), (os, "lstat"), (os, "scandir"), (os, "listdir"),
                      (os, "open"), (io, "open"), (builtins, "open")):
        real = getattr(mod, name)

        def wrapped(path, *a, _real=real, _name=name, **k):
            if isinstance(path, (str, os.PathLike)) and str(root) in os.fspath(path):
                seen.append((_name, on_loop()))
            return _real(path, *a, **k)
        monkeypatch.setattr(mod, name, wrapped)
    return seen


def test_the_local_plan_routes_read_off_the_loop_with_a_size_cap(temp_db, tmp_path, monkeypatch):
    """A local session's plans folder is listed and read in a worker thread,
    and a plan larger than the cap is refused instead of read whole."""
    from core.session import session_state
    claude = tmp_path / ".claude"
    plans = claude / "plans"
    plans.mkdir(parents=True)
    (plans / "a.md").write_text("# a")
    (plans / "big.md").write_bytes(b"x" * 65)
    (plans / "notes.txt").write_text("not a plan")
    (plans / "folder.md").mkdir()
    monkeypatch.setattr(sessions, "PLAN_MAX_BYTES", 64, raising=False)
    monkeypatch.setitem(session_state._session_claude_dirs, SID, str(claude))
    client = TestClient(app)
    token = live_session_token(SID, "pa", user_sub="user-admin")
    seen = _loop_witness(monkeypatch, tmp_path)
    r = client.get(f"/v1/plans?session_id={SID}", headers=_bearer(token))
    assert r.status_code == 200
    assert sorted(p["filename"] for p in r.json()["plans"]) == ["a.md", "big.md"]
    r = client.get(f"/v1/plans/a.md?session_id={SID}", headers=_bearer(token))
    assert r.status_code == 200 and r.json() == {"content": "# a", "filename": "a.md"}
    assert seen and not [name for name, on in seen if on], seen
    r = client.get(f"/v1/plans/big.md?session_id={SID}", headers=_bearer(token))
    assert r.status_code == 413
    r = client.get(f"/v1/plans/folder.md?session_id={SID}", headers=_bearer(token))
    assert r.status_code == 404


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

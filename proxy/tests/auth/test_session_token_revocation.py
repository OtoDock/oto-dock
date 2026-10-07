"""A session token is only as good as its person.

The session JWT minted into an agent's process resolves to nothing once its
user is deleted (never to the broader no-user agent principal), and a token
minted before the user's last password change is refused the way an old
dashboard cookie is. A token minted before tokens carried ``iat`` is
tolerated until it expires.

Run: cd proxy && venv/bin/pytest tests/auth/test_session_token_revocation.py -v
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient
from starlette.requests import Request

import config
from app import app
from auth.providers import _resolve_principal
from auth.session_token import create_session_token
from tests.conftest import live_session_token
from storage import database as db

client = TestClient(app)
AGENT = "project-alpha"


def _req(token: str) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/v1/tasks", "query_string": b"",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    })


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _legacy_token(sub: str) -> str:
    """A token as minted before 1.7.0: no ``iat``, its session live."""
    from core.session import session_state
    sid = str(uuid.uuid4())
    session_state.mark_starting(sid, 3600)
    return jwt.encode({"type": "session", "sid": sid, "agent": AGENT,
                       "user_sub": sub, "exp": int(time.time()) + 3600},
                      config.JWT_SECRET, algorithm="HS256")


def _stamp_password_change(sub: str, when: datetime) -> None:
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_changed_at=%s WHERE sub=%s",
                     (when.isoformat(), sub))
        conn.commit()


def test_a_minted_token_carries_an_integer_iat():
    payload = jwt.decode(create_session_token("s", AGENT, "user-viewer"),
                         config.JWT_SECRET, algorithms=["HS256"])
    assert isinstance(payload["iat"], int)
    assert abs(payload["iat"] - time.time()) < 5


def test_a_deleted_users_token_resolves_to_nobody():
    token = live_session_token(str(uuid.uuid4()), AGENT, "user-viewer")
    assert asyncio.run(_resolve_principal(_req(token))).sub == "user-viewer"
    assert db.delete_user("user-viewer")
    assert asyncio.run(_resolve_principal(_req(token))) is None
    assert client.get("/v1/tasks", headers=_bearer(token)).status_code == 401


def test_a_no_user_token_is_unchanged():
    token = live_session_token(str(uuid.uuid4()), AGENT, "")
    p = asyncio.run(_resolve_principal(_req(token)))
    assert p is not None and p.is_no_user_session and p.agent == AGENT


def test_a_password_change_ends_older_session_tokens():
    old = live_session_token(str(uuid.uuid4()), AGENT, "user-viewer")
    assert client.get("/v1/tasks", headers=_bearer(old)).status_code == 200
    _stamp_password_change("user-viewer", datetime.now(timezone.utc) + timedelta(seconds=60))
    assert client.get("/v1/tasks", headers=_bearer(old)).status_code == 401


def test_a_token_minted_after_the_change_works():
    _stamp_password_change("user-viewer", datetime.now(timezone.utc) - timedelta(seconds=60))
    fresh = live_session_token(str(uuid.uuid4()), AGENT, "user-viewer")
    assert client.get("/v1/tasks", headers=_bearer(fresh)).status_code == 200


def test_a_legacy_token_without_iat_is_tolerated_until_it_expires():
    _stamp_password_change("user-viewer", datetime.now(timezone.utc) + timedelta(seconds=60))
    assert client.get("/v1/tasks", headers=_bearer(_legacy_token("user-viewer"))).status_code == 200


def test_a_token_older_than_the_users_row_is_refused():
    # A person deleted and re-created under the same sub (an identity
    # provider's upsert) does not revive the tokens minted before.
    old = jwt.encode({"type": "session", "sid": "s-old", "agent": AGENT,
                      "user_sub": "user-viewer", "iat": int(time.time()) - 3600,
                      "exp": int(time.time()) + 3600}, config.JWT_SECRET, algorithm="HS256")
    assert client.get("/v1/tasks", headers=_bearer(old)).status_code == 401


def test_the_callback_bearer_carries_the_sessions_user():
    from auth.session_token import SESSION_JWT_SENTINEL_BEARER, swap_session_jwt_bearer
    sid = str(uuid.uuid4())
    create_session_token(sid, AGENT, "user-viewer")   # the session's own token
    swapped = swap_session_jwt_bearer(SESSION_JWT_SENTINEL_BEARER, sid, AGENT)
    payload = jwt.decode(swapped.split(" ", 1)[1], config.JWT_SECRET, algorithms=["HS256"])
    assert payload["user_sub"] == "user-viewer" and payload["sid"] == sid
    # A session with no person keeps a no-user callback bearer.
    other = str(uuid.uuid4())
    create_session_token(other, AGENT, "")
    payload = jwt.decode(swap_session_jwt_bearer(SESSION_JWT_SENTINEL_BEARER, other, AGENT)
                         .split(" ", 1)[1], config.JWT_SECRET, algorithms=["HS256"])
    assert payload["user_sub"] == ""


def test_the_holder_check_agrees_with_the_resolver():
    from auth.providers import session_token_holder_ok
    from auth.session_token import validate_session_token
    live = validate_session_token(create_session_token("s1", AGENT, "user-viewer"))
    nobody = validate_session_token(create_session_token("s2", AGENT, ""))
    assert session_token_holder_ok(live) and session_token_holder_ok(nobody)
    _stamp_password_change("user-viewer", datetime.now(timezone.utc) + timedelta(seconds=60))
    assert not session_token_holder_ok(live)
    assert db.delete_user("user-viewer")
    assert not session_token_holder_ok(live)


def test_every_password_change_closes_the_persons_chats(monkeypatch):
    """A changed password ends the person's warm chats, whichever route
    changed it: the reset link, the self-service change and the admin
    reset. The chats re-warm with fresh tokens on the next message."""
    from unittest.mock import AsyncMock

    from auth.password import hash_password
    from auth.providers import create_session_jwt
    from services.agents import offboarding_sessions

    closer = AsyncMock(return_value=0)
    monkeypatch.setattr(offboarding_sessions, "close_person_sessions", closer)
    first = "correct-horse-battery-77"
    sub = db.create_local_user("p@revoke.test", "P", "p-user", "member", hash_password(first))
    adm = db.create_local_user("a@revoke.test", "A", "a-user", "admin", hash_password(first))
    person = db.get_user(sub)
    # creation stamps a password change; the link must post-date it
    _stamp_password_change(sub, datetime.now(timezone.utc) - timedelta(minutes=5))

    def cookies(s: str) -> dict:
        u = db.get_user(s)
        return {"session": create_session_jwt(s, u["email"], u["name"], u["role"])}

    now = int(time.time())
    link = jwt.encode({"sub": sub, "purpose": "password_reset", "iat": now, "exp": now + 3600},
                      config.JWT_SECRET, algorithm="HS256")
    second = "another-horse-battery-88"
    third = "a-third-horse-battery-99"
    reset = client.post("/auth/reset-password", json={"token": link, "new_password": second})
    assert reset.status_code == 200, reset.text
    assert TestClient(app, cookies=cookies(sub)).put(
        "/v1/users/me/password",
        json={"current_password": second, "new_password": third},
    ).status_code == 200
    assert TestClient(app, cookies=cookies(adm)).post(
        f"/v1/admin/users/{sub}/reset-password").status_code == 200

    assert closer.await_count == 3
    for call in closer.await_args_list:
        assert call.args == (sub, person["username"], "password_changed")

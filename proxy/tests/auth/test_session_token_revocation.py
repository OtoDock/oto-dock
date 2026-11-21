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
from auth import tokens
from auth.providers import _resolve_principal
from auth.session_token import create_session_token
from storage import database as db
from tests.conftest import live_session_token

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
    payload = tokens.decode(create_session_token("s", AGENT, "user-viewer"), tokens.SESSION)
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
    from auth.session_token import CALLBACK_JWT_SENTINEL_BEARER, swap_session_jwt_bearer
    sid = str(uuid.uuid4())
    create_session_token(sid, AGENT, "user-viewer")   # the session's own token
    swapped = swap_session_jwt_bearer(CALLBACK_JWT_SENTINEL_BEARER, sid, AGENT)
    # The Docker tools' own kind, never a session token.
    payload = tokens.decode(swapped.split(" ", 1)[1], tokens.TOOL_CALLBACK)
    assert payload["user_sub"] == "user-viewer" and payload["sid"] == sid
    assert tokens.verify(swapped.split(" ", 1)[1], tokens.SESSION) is None
    # A session with no person keeps a no-user callback bearer.
    other = str(uuid.uuid4())
    create_session_token(other, AGENT, "")
    payload = tokens.decode(swap_session_jwt_bearer(CALLBACK_JWT_SENTINEL_BEARER, other, AGENT)
                            .split(" ", 1)[1], tokens.TOOL_CALLBACK)
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
    """A changed password ends the person's warm chats and forgets the
    holder cache's answers for them, whichever route changed it: the reset
    link, the self-service change and the admin reset. The chats re-warm
    with fresh tokens on the next message."""
    from unittest.mock import AsyncMock

    from auth import token_holder
    from auth.password import hash_password
    from auth.providers import create_session_jwt
    from services.agents import offboarding_sessions

    closer = AsyncMock(return_value=0)
    monkeypatch.setattr(offboarding_sessions, "close_person_sessions", closer)
    forgot: list[str] = []
    monkeypatch.setattr(token_holder, "forget", forgot.append)
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
    # The holder cache forgets the person at each change: a pass cached for
    # a token the change just ended does not live out its minute.
    assert forgot == [sub, sub, sub]


def _built_at(role: str, sub: str = "user-admin", username: str = "admin") -> str:
    """A live session token of ``sub`` whose session was built at ``role``."""
    from auth.path_policy import SecurityContext
    from core.session import session_state
    sid = str(uuid.uuid4())
    token = live_session_token(sid, AGENT, sub)
    session_state.set_session_security(sid, SecurityContext(
        role=role, username=username, agent=AGENT, is_admin_agent=False))
    return token


def test_a_session_built_below_its_person_acts_at_that_role_everywhere():
    from auth import roles
    p = asyncio.run(_resolve_principal(_req(_built_at(roles.MANAGER))))
    assert p.sub == "user-admin" and not p.is_admin and not p.is_owner
    assert p.role == roles.MEMBER
    assert p.effective_role(AGENT) == roles.MANAGER and p.can_manage_agent(AGENT)
    assert not p.can_access_agent("another-agent")


def test_a_session_at_its_persons_role_keeps_it():
    from auth import roles
    p = asyncio.run(_resolve_principal(_req(_built_at(roles.ADMIN))))
    assert p.is_admin and p.effective_role(AGENT) == roles.ADMIN
    # A token minted at its person's own role keeps working with no context:
    # a long-lived session across a platform restart.
    own = live_session_token(str(uuid.uuid4()), AGENT, "user-admin", role=roles.ADMIN)
    assert asyncio.run(_resolve_principal(_req(own))).is_admin


def test_a_token_carries_the_role_its_session_was_built_with():
    """The token's own role holds wherever the token is accepted."""
    from auth import roles
    from core.session import session_state
    for mark in ("starting", "closing"):
        sid = str(uuid.uuid4())
        token = create_session_token(sid, AGENT, "user-admin", role=roles.MANAGER)
        if mark == "starting":
            session_state.mark_starting(sid, 60)
        else:
            session_state.mark_closing(sid)
        p = asyncio.run(_resolve_principal(_req(token)))
        assert p is not None and not p.is_admin, mark
        assert p.effective_role(AGENT) == roles.MANAGER, mark


def test_a_builders_token_names_the_role_it_built():
    from auth import roles
    from auth.session_token import mint_session_env
    env: dict[str, str] = {}
    mint_session_env(env, "s-built", AGENT, "user-admin", external="", role=roles.MANAGER)
    assert tokens.decode(env["PROXY_API_KEY"], tokens.SESSION)["role"] == roles.MANAGER
    mint_session_env(env, "s-none", AGENT, "", external="", role=roles.MANAGER)
    assert "role" not in tokens.decode(env["PROXY_API_KEY"], tokens.SESSION)


def _creator_on_agent(role: str) -> tuple[str, str]:
    """A platform creator holding ``role`` on the agent: (sub, username)."""
    from auth.password import hash_password
    sub = db.create_local_user(f"c-{uuid.uuid4().hex[:8]}@cap.test", "C", "C", "creator",
                               hash_password("correct-horse-battery-77"))
    _stamp_password_change(sub, datetime.now(timezone.utc) - timedelta(minutes=5))
    db.add_user_agent(sub, AGENT, role, "test")
    return sub, db.get_user(sub)["username"]


def test_a_capped_creator_is_a_platform_member():
    from auth import roles
    sub, username = _creator_on_agent(roles.MANAGER)
    p = asyncio.run(_resolve_principal(_req(_built_at(roles.VIEWER, sub, username))))
    assert p.role == roles.MEMBER and not p.is_owner
    assert p.effective_role(AGENT) == roles.VIEWER


def _session_built_at(built: str, username: str):
    from auth.path_policy import SecurityContext
    from core.session import session_state
    sid = str(uuid.uuid4())
    ctx = SecurityContext(role=built, username=username, agent=AGENT, is_admin_agent=False)
    session_state.set_session_security(sid, ctx)
    return sid, ctx


def test_a_capped_session_lists_placed_apps_at_its_own_role():
    from api.hooks.pins import placement_identity
    from auth import roles
    sub, username = _creator_on_agent(roles.MANAGER)
    for built in (roles.VIEWER, roles.MANAGER):
        sid, ctx = _session_built_at(built, username)
        assert placement_identity(ctx, sid)[0] == built


def test_an_app_sees_a_capped_session_at_its_own_role():
    from api.apps import app_proxy
    from auth import roles
    sub, username = _creator_on_agent(roles.MANAGER)
    for built in (roles.VIEWER, roles.MANAGER):
        sid, _ctx = _session_built_at(built, username)
        payload = tokens.decode(live_session_token(sid, AGENT, sub, role=built), tokens.SESSION)
        ok, user, _name, _placed = app_proxy._agent_principal(
            payload, {"id": "app-1", "agent": AGENT}, False)
        assert ok and roles.row_role(user.agent_roles, AGENT) == built


def test_an_apps_call_for_a_capped_session_carries_its_own_role():
    """To another app of the same agent."""
    from api.apps import app_bindings
    from auth import roles
    from services.apps import app_tokens
    sub, username = _creator_on_agent(roles.MANAGER)
    for built in (roles.VIEWER, roles.MANAGER):
        sid, _ctx = _session_built_at(built, username)
        viewer = {"principal": app_tokens.PRINCIPAL_AGENT, "sub": sub, "role": built,
                  "agent": AGENT, "session": sid}
        _claim, role = app_bindings._claim_for_target(
            {"id": "app-1", "agent": AGENT, "slug": "one"}, {"id": "app-2", "agent": AGENT},
            viewer, None)
        assert role == built


def test_a_capped_admin_with_no_row_presents_no_more_than_an_uncapped_one():
    """An app and a placed app see the person's own row on the agent, never
    an admin's standing: a session built below that never raises it."""
    from api.apps import app_proxy
    from api.hooks.pins import placement_identity
    from auth import roles
    from auth.password import hash_password
    sub = db.create_local_user(f"a-{uuid.uuid4().hex[:8]}@cap.test", "A", "A", "admin",
                               hash_password("correct-horse-battery-77"))
    _stamp_password_change(sub, datetime.now(timezone.utc) - timedelta(minutes=5))
    username = db.get_user(sub)["username"]
    for built in (roles.MANAGER, roles.ADMIN):
        sid, ctx = _session_built_at(built, username)
        assert placement_identity(ctx, sid)[0] == roles.VIEWER, built
        payload = tokens.decode(live_session_token(sid, AGENT, sub, role=built), tokens.SESSION)
        caller = asyncio.run(app_proxy._agent_caller(payload, {"id": "app-1", "agent": AGENT}))
        assert caller.role == roles.VIEWER, built

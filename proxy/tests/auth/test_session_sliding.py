"""Sliding-session refresh middleware (``middleware.refresh_session_cookie``).

The session cookie is re-issued on activity once it is past the halfway point of
its lifetime, so an active session never expires — but logout must stay
authoritative (a logged-out session is never resurrected).
"""

import secrets
import time

import jwt
import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.providers import create_session_jwt, validate_session_jwt

client = TestClient(app)


def _session_set_cookies(resp) -> list[str]:
    """All Set-Cookie header values for the `session` cookie on a response."""
    return [
        v for k, v in resp.headers.multi_items()
        if k.lower() == "set-cookie" and v.startswith("session=")
    ]


def _cookie_value(set_cookie: str) -> str:
    """Extract the raw cookie value from a Set-Cookie header string."""
    return set_cookie.split("session=", 1)[1].split(";", 1)[0]


@pytest.fixture(autouse=True)
def _row_predates_the_stale_cookies():
    # The seeded row is created at test start and the stale cookies below
    # are hours old; a cookie older than its person's row is refused.
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET created_at=%s WHERE sub='user-admin'",
                     ("2020-01-01T00:00:00+00:00",))
        conn.commit()


def _stale_token(sub: str = "user-admin") -> str:
    """A valid session JWT that is past the halfway mark of its life."""
    now = int(time.time())
    return jwt.encode(
        {
            "purpose": "session",  # required by validate_session_jwt (2FA/reset bypass guard)
            "sub": sub, "email": "a@b.c", "name": "A", "role": "admin",
            "auth_provider": "local",
            "iat": now - 10 * 3600,  # 10h old
            "exp": now + 3600,       # 1h left → well past halfway
            "jti": secrets.token_urlsafe(8),  # its own sign-in
        },
        config.JWT_SECRET, algorithm="HS256",
    )


def test_fresh_cookie_not_reissued():
    """A cookie still in the first half of its life is left untouched."""
    token = create_session_jwt("user-admin", "a@b.c", "A", "admin")  # iat = now
    r = client.get("/auth/config", headers={"Cookie": f"session={token}"})
    assert r.status_code == 200
    assert _session_set_cookies(r) == []


def test_stale_cookie_refreshed():
    """A cookie past the halfway mark is re-issued with a later expiry."""
    r = client.get("/auth/config", headers={"Cookie": f"session={_stale_token()}"})
    assert r.status_code == 200
    cookies = _session_set_cookies(r)
    assert len(cookies) == 1
    payload = validate_session_jwt(_cookie_value(cookies[0]))
    assert payload is not None
    assert payload["sub"] == "user-admin"
    # exp reset to the full window (168h) — far beyond the old 1h-left token.
    assert payload["exp"] > int(time.time()) + 100 * 3600


def test_no_cookie_no_reissue():
    r = client.get("/auth/config")
    assert r.status_code == 200
    assert _session_set_cookies(r) == []


def test_bearer_request_not_refreshed():
    """API-key / bearer requests carry no session cookie → nothing to refresh."""
    r = client.get("/auth/config", headers={"Authorization": f"Bearer {config.API_KEY}"})
    assert _session_set_cookies(r) == []


def test_logout_not_resurrected():
    """Logout deletes the cookie; the middleware must not re-issue a fresh one."""
    r = client.post("/auth/logout", headers={"Cookie": f"session={_stale_token()}"})
    assert r.status_code == 200
    cookies = _session_set_cookies(r)
    assert len(cookies) == 1  # only the deletion, not a refresh
    val = _cookie_value(cookies[0])
    assert val in ("", '""')                 # a deletion, not a JWT
    assert validate_session_jwt(val) is None


# ── the refresh reads ──────────────────────────────────
# The re-mint reads nothing when the route already resolved this exact
# cookie, reads off the loop otherwise, and never costs the route its
# answer: any error there skips the re-mint.

import pytest  # noqa: E402

import middleware  # noqa: E402
from storage import pg  # noqa: E402


@pytest.fixture
def fast_lane_calls(monkeypatch):
    calls = []
    real = pg.run_db_fast

    async def spy(fn, *a, **kw):
        calls.append(getattr(fn, "__name__", str(fn)))
        return await real(fn, *a, **kw)

    monkeypatch.setattr(pg, "run_db_fast", spy)
    monkeypatch.setattr(middleware, "_expiry_cache", (0, 0.0))
    return calls


def test_a_cookie_the_route_resolved_is_reminted_without_a_user_read(fast_lane_calls):
    r = client.get("/v1/users/me/passkeys", headers={"Cookie": f"session={_stale_token()}"})
    assert r.status_code == 200
    assert _session_set_cookies(r)
    assert "get_user" not in fast_lane_calls


def test_a_cookie_no_route_resolved_is_read_first(fast_lane_calls):
    r = client.get("/auth/config", headers={"Cookie": f"session={_stale_token()}"})
    assert r.status_code == 200 and _session_set_cookies(r)
    assert "get_user" in fast_lane_calls


def test_a_database_error_in_the_refresh_keeps_the_routes_answer(monkeypatch):
    async def down(fn, *a, **kw):
        raise pg.DatabaseUnavailable("breaker open")

    monkeypatch.setattr(pg, "run_db_fast", down)
    monkeypatch.setattr(middleware, "_expiry_cache", (0, 0.0))
    r = client.get("/auth/config", headers={"Cookie": f"session={_stale_token()}"})
    assert r.status_code == 200
    assert _session_set_cookies(r) == []


def test_a_deleted_users_cookie_is_not_reminted():
    r = client.get("/auth/config", headers={"Cookie": f"session={_stale_token('local:gone')}"})
    assert r.status_code == 200
    assert _session_set_cookies(r) == []


@pytest.mark.asyncio
async def test_a_remint_is_dated_to_the_routes_check_so_a_later_epoch_refuses_it(monkeypatch):
    """The route judged the cookie current, then a "sign out everywhere"
    moved the person's epoch while the request ran: the refresh re-mints
    without a second read, dated to the route's check, so the new cookie
    predates the epoch and is refused like the old one."""
    from datetime import datetime, timezone
    from starlette.datastructures import MutableHeaders
    from auth.providers import session_cookie_current
    from storage import database as task_store
    from storage.pg import get_conn

    monkeypatch.setattr(middleware, "_expiry_cache", (0, 0.0))
    now = int(time.time())
    cookie = _stale_token()
    scope = {"type": "http", "path": "/v1/users/me/passkeys", "state": {
        "otodock_session_cookie": cookie, "otodock_session_cookie_checked_at": now - 20,
    }}
    with get_conn() as conn:
        conn.execute("UPDATE users SET token_epoch_at=%s WHERE sub='user-admin'",
                     (datetime.fromtimestamp(now - 10, timezone.utc).isoformat(),))
        conn.commit()
    headers = MutableHeaders()
    await middleware._refresh_session_cookie(scope, headers, cookie)
    minted = [v for v in headers.getlist("set-cookie") if v.startswith("session=")]
    assert len(minted) == 1
    payload = validate_session_jwt(_cookie_value(minted[0]))
    assert payload["iat"] == now - 20
    assert not session_cookie_current(task_store.get_user("user-admin"), payload)
    assert client.get("/auth/me", headers={"Cookie": f"session={_cookie_value(minted[0])}"}).status_code == 401

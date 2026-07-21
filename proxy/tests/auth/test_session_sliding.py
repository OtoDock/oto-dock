"""Sliding-session refresh middleware (``middleware.refresh_session_cookie``).

The session cookie is re-issued on activity once it is past the halfway point of
its lifetime, so an active session never expires — but logout must stay
authoritative (a logged-out session is never resurrected).
"""

import time

import jwt
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

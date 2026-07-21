"""Every login-shaped payload must carry feature_flags.

The dashboard stores the LOGIN response as its user object, and its
feature gates fail open on a missing key (`!== false`) — so a payload
without feature_flags un-hides staged features until the next /auth/me
refetch. Found on the 1.3.0 public cut: the Remote Machines tab
appeared right after a fresh login on builds that ship without the
satellite source. /auth/me always carried the flags; every path that
returns a user object must serve the same ones
(api.auth._common.build_feature_flags).
"""

import pytest
from fastapi.testclient import TestClient

from api.auth._common import _build_user_response, build_feature_flags
from app import app
from auth.password import hash_password
from auth.rate_limiter import clear_rate_limit
from storage import database as db

client = TestClient(app)

_PW = "correct-horse-battery-staple-77"
_EMAIL = "flags@t.com"

_FLAG_KEYS = {
    "allow_user_paired_machines",
    "remote_machines_available",
    "interactive_terminal_enabled",
}


@pytest.fixture(autouse=True)
def _fresh_buckets():
    clear_rate_limit("login", "testclient")
    yield
    clear_rate_limit("login", "testclient")


def test_local_login_response_carries_feature_flags():
    db.create_local_user(_EMAIL, "Flags", "Flags", "member", hash_password(_PW))
    resp = client.post("/auth/login/local", json={"email": _EMAIL, "password": _PW})
    assert resp.status_code == 200, resp.text
    flags = resp.json()["user"]["feature_flags"]
    assert _FLAG_KEYS <= set(flags)
    # Build-agnostic: the value must mirror whether the satellite source
    # ships with THIS build (True privately, False on the public cut).
    assert flags == build_feature_flags()


def test_build_user_response_always_includes_flags():
    """The shared builder feeds local login, 2FA, passkey, OAuth callback
    and admin-create — the flags must be present at the source."""
    row = {"sub": "u-flags-unit", "email": "u@t.com", "name": "U", "role": "member"}
    payload = _build_user_response(row, agents=[], agent_roles={})
    assert _FLAG_KEYS <= set(payload["feature_flags"])

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


# --- the apps' store review ask (two platform settings, read through the flags;
# no card and no field of the PUT since 2.0: an operator forces another value) ---

_REVIEW_KEYS = ("store_review_enabled", "store_review_days")


@pytest.fixture
def _review_settings():
    from auth.providers import UserContext, get_current_user

    async def _admin():
        return UserContext(sub="review-admin", email="ra@t.com", name="A", role="admin")
    app.dependency_overrides[get_current_user] = _admin
    for key in _REVIEW_KEYS:
        db.set_platform_setting(key, "")
    yield
    for key in _REVIEW_KEYS:
        db.set_platform_setting(key, "")
    app.dependency_overrides.pop(get_current_user, None)


def test_the_store_review_flags_default_on_and_ninety_days(_review_settings):
    flags = build_feature_flags()
    assert flags["store_review_enabled"] is True
    assert flags["store_review_days"] == 90


@pytest.mark.parametrize(("stored", "read"), [
    ("45", 45), ("30", 30), ("730", 730), ("5", 30), ("9999", 730), ("soon", 90), ("", 90),
])
def test_the_store_review_days_read_inside_the_range(_review_settings, stored, read):
    db.set_platform_setting("store_review_days", stored)
    assert build_feature_flags()["store_review_days"] == read


def test_a_value_stored_before_the_card_went_is_still_read(_review_settings):
    db.set_platform_setting("store_review_enabled", "0")
    db.set_platform_setting("store_review_days", "120")
    flags = build_feature_flags()
    assert flags["store_review_enabled"] is False and flags["store_review_days"] == 120
    got = client.get("/v1/admin/platform-settings").json()
    assert got["store_review_enabled"] is False and got["store_review_days"] == 120


@pytest.mark.parametrize("body", [
    {"store_review_enabled": True, "store_review_days": "45"},
    {"store_review_days": "29"},
    {"store_review_enabled": True},
])
def test_an_older_dashboards_put_is_answered_and_writes_neither_key(_review_settings, body):
    db.set_platform_setting("store_review_enabled", "0")
    db.set_platform_setting("store_review_days", "120")
    r = client.put("/v1/admin/platform-settings", json=body)
    assert r.status_code == 200, r.text
    assert db.get_platform_setting("store_review_enabled") == "0"
    assert db.get_platform_setting("store_review_days") == "120"


def test_an_operator_forces_the_store_review_off_and_spaced(_review_settings, monkeypatch):
    import config
    monkeypatch.setattr(config, "forced_settings",
                        lambda: {"store_review_enabled": "0", "store_review_days": "180"})
    flags = build_feature_flags()
    assert flags["store_review_enabled"] is False and flags["store_review_days"] == 180
    got = client.get("/v1/admin/platform-settings").json()
    assert got["store_review_enabled"] is False and got["store_review_days"] == 180
    assert set(_REVIEW_KEYS) <= set(got["forced_keys"])

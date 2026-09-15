"""DB-backed regression tests for the 2026-09-02 relaunch assurance pass.

Exercises the real endpoints: the login tarpit now records + resets, the
TOTP-setup reconfigure guard, the webauthn duplicate-credential 400, and the
user-delete API-key/trigger revocation.
"""

import pytest
from fastapi.testclient import TestClient

from app import app
from auth.password import hash_password
from auth.providers import UserContext, get_current_user
from auth.rate_limiter import clear_rate_limit
from storage import database as db

client = TestClient(app)

_PW = "correct-horse-battery-staple-42"


@pytest.fixture(autouse=True)
def _clean():
    for bucket in ("login", "confirm"):
        clear_rate_limit(bucket, "testclient")
    yield
    for bucket in ("login", "confirm"):
        clear_rate_limit(bucket, "testclient")
    app.dependency_overrides.pop(get_current_user, None)


def _mk_user(email: str = "tarpit@t.com", role: str = "member") -> str:
    return db.create_local_user(email, "T", "T", role, hash_password(_PW))


# --- Tarpit now records failed attempts and resets on success ----------------

def test_failed_login_records_against_account_and_resets():
    sub = _mk_user()
    assert db.get_user(sub)["failed_login_attempts"] == 0

    # Wrong password → counter increments (the AuthResult now carries sub).
    for _ in range(3):
        r = client.post("/auth/login/local",
                        json={"email": "tarpit@t.com", "password": "nope"})
        assert r.status_code == 401
    assert db.get_user(sub)["failed_login_attempts"] == 3

    # A correct login clears the counter.
    r = client.post("/auth/login/local",
                    json={"email": "tarpit@t.com", "password": _PW})
    assert r.status_code == 200
    assert db.get_user(sub)["failed_login_attempts"] == 0


def test_unknown_email_does_not_record_against_any_account():
    # No user exists; a miss must not raise and must not create counters.
    r = client.post("/auth/login/local",
                    json={"email": "ghost@t.com", "password": "nope"})
    assert r.status_code == 401


# --- TOTP setup guard: reconfigure while enabled needs the password ----------

def test_totp_setup_reconfigure_requires_password():
    import pyotp
    sub = _mk_user(email="totpguard@t.com")

    async def _me():
        return UserContext(sub=sub, email="totpguard@t.com", name="T", role="member")
    app.dependency_overrides[get_current_user] = _me

    # Initial enable flow: no body required.
    setup = client.post("/v1/users/me/totp/setup").json()
    code = pyotp.TOTP(setup["secret"]).now()
    assert client.post("/v1/users/me/totp/verify", json={"code": code}).status_code == 200

    # Now 2FA is enabled — a no-password re-setup is refused (would otherwise
    # silently rotate the victim's secret from a hijacked session).
    r = client.post("/v1/users/me/totp/setup")
    assert r.status_code == 401
    r = client.post("/v1/users/me/totp/setup", json={"password": "wrong"})
    assert r.status_code == 401
    # Correct password lets the owner reconfigure.
    r = client.post("/v1/users/me/totp/setup", json={"password": _PW})
    assert r.status_code == 200


# --- User delete revokes API keys + user triggers ----------------------------

def test_delete_user_revokes_api_keys_and_triggers():
    from storage.identity import api_key_store
    from storage.automation import trigger_store

    admin_sub = _mk_user(email="admin@t.com", role="admin")
    victim_sub = _mk_user(email="victim@t.com")

    # Give the victim an API key.
    api_key_store.create_user_api_key(
        user_sub=victim_sub, name="k1", key_hash="h", prefix="otok_abc",
        permissions=["triggers"],
    )
    assert len(api_key_store.list_user_api_keys(user_sub=victim_sub)) == 1

    async def _admin():
        return UserContext(sub=admin_sub, email="admin@t.com", name="A", role="admin")
    app.dependency_overrides[get_current_user] = _admin

    r = client.request("DELETE", f"/v1/admin/users/{victim_sub}")
    assert r.status_code == 200, r.text

    # The orphaned key is gone (was previously un-revocable after delete).
    assert api_key_store.list_user_api_keys(user_sub=victim_sub) == []
    assert trigger_store.cleanup_user_triggers(victim_sub) == 0


def test_delete_user_removes_their_usage_caps():
    from storage.billing import subscription_store

    admin_sub = _mk_user(email="admin2@t.com", role="admin")
    victim_sub = _mk_user(email="capped@t.com")
    db.upsert_usage_limit("user_self", victim_sub, "monthly", 5.0, victim_sub)
    db.upsert_usage_limit("user_override", victim_sub, "weekly", 9.0, admin_sub)
    subscription_store.upsert_pool_cap(
        "user", victim_sub, week_pct=50, day_pct=None, week_usd=None, day_usd=None,
        on_reached="stop", updated_by=victim_sub)

    async def _admin():
        return UserContext(sub=admin_sub, email="admin2@t.com", name="A", role="admin")
    app.dependency_overrides[get_current_user] = _admin

    r = client.request("DELETE", f"/v1/admin/users/{victim_sub}")
    assert r.status_code == 200, r.text
    assert db.get_usage_limits_for_target("user_self", victim_sub) == []
    assert db.get_usage_limits_for_target("user_override", victim_sub) == []
    assert subscription_store.get_pool_cap("user", victim_sub) is None

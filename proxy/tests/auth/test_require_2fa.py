"""Admin require-2FA policy: setting round-trip, forced-enrollment flag, and
the server-side gate.

The policy never locks anyone out: the ``must_enroll_2fa`` flag (login
response + /auth/me) routes the dashboard to the enrollment screen, and the
server holds the browser session to the enrollment and session routes until
then (the same for ``must_change_password``). OIDC accounts are exempt
(their IdP owns MFA), and the operator forced-settings overlay pins the knob
on managed installs.
"""

import pyotp
import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.password import hash_password
from auth.providers import UserContext, get_current_user
from auth.rate_limiter import clear_rate_limit
from storage import database as db

client = TestClient(app)

_PW = "correct-horse-battery-staple-77"
_EMAIL = "req2fa@t.com"


@pytest.fixture(autouse=True)
def _fresh():
    from auth import providers
    providers.clear_auth_gate_caches()
    for bucket in ("login", "2fa"):
        clear_rate_limit(bucket, "testclient")
    yield
    for bucket in ("login", "2fa"):
        clear_rate_limit(bucket, "testclient")
    app.dependency_overrides.pop(get_current_user, None)


def _mk_user(auth_provider: str = "local") -> str:
    sub = db.create_local_user(_EMAIL, "U", "U", "member", hash_password(_PW))
    if auth_provider != "local":
        db.update_user_auth_fields(sub, auth_provider=auth_provider)

    async def _me():
        return UserContext(sub=sub, email=_EMAIL, name="U", role="member",
                           auth_provider=auth_provider)

    app.dependency_overrides[get_current_user] = _me
    return sub


def _as_admin():
    async def _admin():
        return UserContext(sub="local:admin", email="a@t.com", name="A", role="admin")

    app.dependency_overrides[get_current_user] = _admin


def test_setting_roundtrip_defaults_off():
    _as_admin()
    assert client.get("/v1/admin/platform-settings").json()["require_2fa"] is False

    resp = client.put("/v1/admin/platform-settings", json={"require_2fa": True})
    assert resp.status_code == 200
    assert client.get("/v1/admin/platform-settings").json()["require_2fa"] is True

    client.put("/v1/admin/platform-settings", json={"require_2fa": False})
    assert client.get("/v1/admin/platform-settings").json()["require_2fa"] is False


def test_local_user_without_second_factor_must_enroll():
    _mk_user()
    db.set_platform_setting("require_2fa", "1")

    data = client.post("/auth/login/local", json={"email": _EMAIL, "password": _PW}).json()
    assert data["user"]["must_enroll_2fa"] is True
    # The session IS issued (no lockout) — /auth/me carries the flag too.
    assert client.get("/auth/me").json()["user"]["must_enroll_2fa"] is True


def test_flag_clears_after_totp_enrollment():
    _mk_user()
    db.set_platform_setting("require_2fa", "1")

    setup = client.post("/v1/users/me/totp/setup").json()
    code = pyotp.TOTP(setup["secret"]).now()
    assert client.post("/v1/users/me/totp/verify", json={"code": code}).status_code == 200
    assert client.get("/auth/me").json()["user"]["must_enroll_2fa"] is False


def test_policy_off_means_no_enrollment_flag():
    _mk_user()
    data = client.post("/auth/login/local", json={"email": _EMAIL, "password": _PW}).json()
    assert data["user"]["must_enroll_2fa"] is False
    assert client.get("/auth/me").json()["user"]["must_enroll_2fa"] is False


def test_a_passkey_counts_as_enrolment_only_while_passkeys_are_enabled(monkeypatch):
    """Without an https DASHBOARD_PUBLIC_URL the login never offers a
    passkey, so a registered one is no second factor and the person is held
    for enrolment like anyone without one."""
    from auth import providers
    from storage.identity import webauthn_store
    sub = _mk_user()
    webauthn_store.add_credential("gate-cred", sub, "pk", 0, "Mine", [])
    db.set_platform_setting("require_2fa", "1")

    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://dash.example.com")
    assert providers.auth_gate(db.get_user(sub)) == ""

    # The passkey answer is cached now; with passkeys off it is not read.
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "http://192.168.1.10:8400")
    assert providers.auth_gate(db.get_user(sub)) == providers.GATE_ENROLL_2FA
    providers.clear_auth_gate_caches()
    assert providers.auth_gate(db.get_user(sub)) == providers.GATE_ENROLL_2FA
    login = client.post("/auth/login/local", json={"email": _EMAIL, "password": _PW}).json()
    assert login["user"]["must_enroll_2fa"] is True


def test_oidc_user_exempt():
    _mk_user(auth_provider="oidc:authentik")
    db.set_platform_setting("require_2fa", "1")
    assert client.get("/auth/me").json()["user"]["must_enroll_2fa"] is False


def test_forced_settings_overlay_pins_the_knob(monkeypatch):
    monkeypatch.setattr(config, "_FORCED_SETTINGS", {"require_2fa": "1"})
    _as_admin()

    # Admin write is ignored; the read overlay keeps the forced value and the
    # key is surfaced as forced so the UI locks the control.
    client.put("/v1/admin/platform-settings", json={"require_2fa": False})
    data = client.get("/v1/admin/platform-settings").json()
    assert data["require_2fa"] is True
    assert "require_2fa" in data["forced_keys"]

    # Enforcement follows the forced value.
    _mk_user()
    login = client.post("/auth/login/local", json={"email": _EMAIL, "password": _PW}).json()
    assert login["user"]["must_enroll_2fa"] is True


# ── the server-side gate ─────────────────────────────
# While must_change_password or must_enroll_2fa holds, the browser session
# reaches only the change, enrolment and session routes: the rest answer
# 403 with X-Auth-Gate, never 401 (the dashboard reads 401 as a lost
# session). Session tokens and API keys are never gated.

_EXEMPT = [
    ("GET", "/auth/me"),
    ("GET", "/auth/config"),
    ("POST", "/v1/users/me/totp/setup"),
    ("GET", "/v1/users/me/passkeys"),
    ("POST", "/v1/users/me/passkeys/register/options"),
]


def _cookie_client(sub: str) -> TestClient:
    from auth.providers import create_session_jwt
    user = db.get_user(sub)
    return TestClient(app, cookies={"session": create_session_jwt(
        sub, user["email"], user["name"], user["role"])})


@pytest.fixture
def _gate_fresh():
    from auth import providers
    providers.clear_auth_gate_caches()
    yield
    providers.clear_auth_gate_caches()


def test_require_2fa_gates_an_unenrolled_browser_session(_gate_fresh):
    sub = db.create_local_user(_EMAIL, "U", "U", "member", hash_password(_PW))
    db.set_platform_setting("require_2fa", "1")
    c = _cookie_client(sub)
    r = c.get("/v1/tasks")
    assert r.status_code == 403 and r.headers["x-auth-gate"] == "must_enroll_2fa"
    for method, path in _EXEMPT:
        resp = c.request(method, path)
        assert resp.status_code != 403, (method, path, resp.status_code)
    assert c.get("/auth/me").json()["user"]["must_enroll_2fa"] is True


def test_enrolling_totp_lifts_the_gate(_gate_fresh):
    sub = db.create_local_user(_EMAIL, "U", "U", "member", hash_password(_PW))
    db.set_platform_setting("require_2fa", "1")
    c = _cookie_client(sub)
    assert c.get("/v1/tasks").status_code == 403
    secret = c.post("/v1/users/me/totp/setup").json()["secret"]
    assert c.post("/v1/users/me/totp/verify",
                  json={"code": pyotp.TOTP(secret).now(), "password": _PW}).status_code == 200
    assert c.get("/v1/tasks").status_code == 200


def test_an_oidc_account_is_never_gated(_gate_fresh):
    sub = db.create_local_user(_EMAIL, "U", "U", "member", hash_password(_PW))
    db.update_user_auth_fields(sub, auth_provider="oidc:authentik", must_change_password=True)
    db.set_platform_setting("require_2fa", "1")
    assert _cookie_client(sub).get("/v1/tasks").status_code == 200


def test_must_change_password_gates_until_the_change(_gate_fresh):
    sub = db.create_local_user(_EMAIL, "U", "U", "member", hash_password(_PW),
                               must_change_password=True)
    c = _cookie_client(sub)
    r = c.get("/v1/tasks")
    assert r.status_code == 403 and r.headers["x-auth-gate"] == "must_change_password"
    ok = c.put("/v1/users/me/password",
               json={"current_password": _PW, "new_password": "another-horse-battery-88"})
    assert ok.status_code == 200
    assert c.get("/v1/tasks").status_code == 200


def test_a_gated_persons_session_token_is_never_gated(_gate_fresh):
    from auth.session_token import create_session_token
    sub = db.create_local_user(_EMAIL, "U", "U", "member", hash_password(_PW),
                               must_change_password=True)
    db.set_platform_setting("require_2fa", "1")
    token = create_session_token("s-gate", "any-agent", sub)
    assert client.get("/v1/tasks", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_turning_the_policy_off_lifts_the_gate_at_once(_gate_fresh):
    sub = db.create_local_user(_EMAIL, "U", "U", "member", hash_password(_PW))
    db.set_platform_setting("require_2fa", "1")
    c = _cookie_client(sub)
    assert c.get("/v1/tasks").status_code == 403
    db.set_platform_setting("require_2fa", "0")
    assert c.get("/v1/tasks").status_code == 200


# ── one gate rule and the policy switch ───


def test_auth_me_follows_the_gate_for_an_sso_row(_gate_fresh):
    # The gate holds local accounts only: an SSO row that says
    # must_change_password is not held, and /auth/me says so.
    sub = _mk_user(auth_provider="oidc:authentik")
    db.update_user_auth_fields(sub, must_change_password=True)
    assert client.get("/auth/me").json()["user"]["must_change_password"] is False


def _admin_client(sub: str) -> TestClient:
    from auth.providers import create_session_jwt
    return TestClient(app, cookies={"session": create_session_jwt(sub, "adm@t.com", "A", "admin")})


def test_turning_the_policy_on_is_refused_for_an_unenrolled_local_admin(_gate_fresh):
    sub = db.create_local_user("adm@t.com", "A", "A", "admin", hash_password(_PW))
    c = _admin_client(sub)
    r = c.put("/v1/admin/platform-settings",
              json={"require_2fa": True, "company_name": "Not Written Ltd"})
    assert r.status_code == 409 and "two-step sign-in" in r.json()["detail"]
    settings = db.get_all_platform_settings()
    assert settings.get("require_2fa", "") != "1"
    assert settings.get("company_name", "") != "Not Written Ltd"


def test_an_enrolled_or_sso_or_rowless_admin_may_turn_it_on(_gate_fresh):
    sub = db.create_local_user("adm@t.com", "A", "A", "admin", hash_password(_PW))
    db.update_user_auth_fields(sub, totp_enabled=True)
    assert _admin_client(sub).put("/v1/admin/platform-settings",
                                  json={"require_2fa": True}).status_code == 200
    db.set_platform_setting("require_2fa", "0")
    sso = db.create_local_user("sso@t.com", "S", "S", "admin", hash_password(_PW))
    db.update_user_auth_fields(sso, auth_provider="oidc:authentik")
    from auth.providers import create_session_jwt
    c = TestClient(app, cookies={"session": create_session_jwt(sso, "sso@t.com", "S", "admin")})
    assert c.put("/v1/admin/platform-settings", json={"require_2fa": True}).status_code == 200
    db.set_platform_setting("require_2fa", "0")
    _as_admin()   # a principal with no users row
    assert client.put("/v1/admin/platform-settings", json={"require_2fa": True}).status_code == 200


def test_forwarding_warnings_reach_the_admin_settings_only(_gate_fresh):
    from auth import lan_check
    lan_check.reset_state()
    lan_check.stamp_scope({"type": "http", "client": ("10.200.0.1", 1), "server": ("x", 8400),
                           "headers": [(b"x-forwarded-for", b"203.0.113.7")]})
    _as_admin()
    rows = client.get("/v1/admin/platform-settings").json()["forwarding_warnings"]
    assert rows and rows[0]["peer"] == "10.200.0.1"
    _mk_user()
    assert "forwarding_warnings" not in client.get("/auth/config").json()
    assert client.get("/v1/admin/platform-settings").status_code == 403
    lan_check.reset_state()


@pytest.mark.asyncio
async def test_turning_the_requirement_on_counts_a_passkey_only_while_passkeys_are_enabled(monkeypatch):
    """The 409 that keeps an admin off the enrolment screen follows the same
    rule as the gate: a passkey the login never offers is no second factor."""
    from fastapi import HTTPException
    from api.auth import platform as platform_api
    from storage.identity import webauthn_store
    sub = _mk_user()
    webauthn_store.add_credential("gate-cred-2", sub, "pk", 0, "Mine", [])
    db.set_platform_setting("require_2fa", "0")
    u = UserContext(sub=sub, email=_EMAIL, name="U", role="admin")
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://dash.example.com")
    await platform_api._refuse_require_2fa_that_would_hold(u)
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "http://192.168.1.10:8400")
    with pytest.raises(HTTPException) as excinfo:
        await platform_api._refuse_require_2fa_that_would_hold(u)
    assert excinfo.value.status_code == 409

"""WebAuthn passkey endpoints: feature gate, ceremonies, management, policy.

The cryptographic verification itself belongs to py_webauthn; these tests
monkeypatch its two verify functions and cover OUR contract: the https
feature gate, single-use short-lived challenges, password-confirmed
management, credential storage/ownership, sign-count + last-used updates,
session issuance on login, and the require-2FA interplay (a registered
passkey satisfies the policy).
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse

import config
from api.auth import webauthn as wa
from app import app
from auth.password import hash_password
from auth.providers import UserContext, get_current_user
from auth.rate_limiter import clear_rate_limit
from storage import database as db
from storage import webauthn_store

client = TestClient(app)

_PW = "correct-horse-battery-staple-11"
_EMAIL = "pk@t.com"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://dash.example.com")
    clear_rate_limit("passkey", "testclient")
    wa._challenges.clear()
    wa._native_tokens.clear()
    yield
    clear_rate_limit("passkey", "testclient")
    wa._challenges.clear()
    wa._native_tokens.clear()
    app.dependency_overrides.pop(get_current_user, None)


def _mk_user(email=_EMAIL) -> str:
    sub = db.create_local_user(email, "PK", "PK", "member", hash_password(_PW))

    async def _me():
        return UserContext(sub=sub, email=email, name="PK", role="member")

    app.dependency_overrides[get_current_user] = _me
    return sub


def _register(monkeypatch, sub: str, name="My passkey", cred_id: bytes = b"credential-raw-id-1") -> str:
    """Drive the register ceremony with the library verify stubbed out."""
    opts = client.post("/v1/users/me/passkeys/register/options", json={"password": _PW})
    assert opts.status_code == 200, opts.text
    state = opts.json()["state"]

    monkeypatch.setattr(wa, "verify_registration_response", lambda **kw: SimpleNamespace(
        credential_id=cred_id,
        credential_public_key=b"cose-public-key",
        sign_count=0,
    ))
    resp = client.post("/v1/users/me/passkeys/register/verify", json={
        "state": state, "name": name,
        "credential": {"id": bytes_to_base64url(cred_id), "response": {"transports": ["internal"]}},
    })
    assert resp.status_code == 200, resp.text
    return bytes_to_base64url(cred_id)


def test_feature_gate_requires_https_public_url(monkeypatch):
    _mk_user()
    for bad in ("", "http://192.168.1.10:8400"):
        monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", bad)
        cfg = client.get("/auth/config").json()
        assert cfg["passkeys_enabled"] is False
        # No RP host while the feature is off — the login page must not gate
        # (or hint) on a host passkeys can't be used with anyway.
        assert cfg["passkey_rp_host"] == ""
        assert client.post("/auth/passkey/options").status_code == 400
        r = client.post("/v1/users/me/passkeys/register/options", json={"password": _PW})
        assert r.status_code == 400

    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://dash.example.com")
    cfg = client.get("/auth/config").json()
    assert cfg["passkeys_enabled"] is True
    # The login page compares location.hostname against this to hide the
    # passkey buttons on origins where the browser refuses the ceremony.
    assert cfg["passkey_rp_host"] == "dash.example.com"
    assert wa._rp_id() == "dash.example.com"
    assert wa._expected_origin() == "https://dash.example.com"
    assert client.get("/v1/users/me/passkeys").json()["rp_host"] == "dash.example.com"


def test_register_options_require_password():
    _mk_user()
    r = client.post("/v1/users/me/passkeys/register/options", json={"password": "wrong"})
    assert r.status_code == 401

    r = client.post("/v1/users/me/passkeys/register/options", json={"password": _PW})
    assert r.status_code == 200
    data = r.json()
    assert data["options"]["rp"]["id"] == "dash.example.com"
    assert data["options"]["authenticatorSelection"]["residentKey"] == "required"
    assert data["state"]


def test_register_stores_credential_and_challenge_is_single_use(monkeypatch):
    sub = _mk_user()
    cred_id = _register(monkeypatch, sub)

    stored = webauthn_store.get_credential(cred_id)
    assert stored["user_sub"] == sub
    assert stored["name"] == "My passkey"
    assert stored["public_key"] == bytes_to_base64url(b"cose-public-key")

    listed = client.get("/v1/users/me/passkeys").json()
    assert listed["enabled"] is True
    assert [p["name"] for p in listed["passkeys"]] == ["My passkey"]
    # The wire list carries no key material.
    assert "public_key" not in listed["passkeys"][0]

    # The registration state was consumed — replay is rejected.
    resp = client.post("/v1/users/me/passkeys/register/verify", json={
        "state": "spent-or-unknown", "name": "x",
        "credential": {"id": cred_id, "response": {}},
    })
    assert resp.status_code == 400


def test_register_verify_rejects_bad_attestation(monkeypatch):
    _mk_user()
    state = client.post("/v1/users/me/passkeys/register/options",
                        json={"password": _PW}).json()["state"]

    def _boom(**kw):
        raise InvalidRegistrationResponse("nope")

    monkeypatch.setattr(wa, "verify_registration_response", _boom)
    resp = client.post("/v1/users/me/passkeys/register/verify", json={
        "state": state, "credential": {"id": "x", "response": {}},
    })
    assert resp.status_code == 400
    assert client.get("/v1/users/me/passkeys").json()["passkeys"] == []


def test_passkey_login_issues_session_and_updates_usage(monkeypatch):
    sub = _mk_user()
    cred_id = _register(monkeypatch, sub)
    app.dependency_overrides.pop(get_current_user, None)  # login is public

    opts = client.post("/auth/passkey/options")
    assert opts.status_code == 200
    data = opts.json()
    assert data["options"]["rpId"] == "dash.example.com"
    # Discoverable flow: no allowCredentials constraint.
    assert not data["options"].get("allowCredentials")

    monkeypatch.setattr(wa, "verify_authentication_response",
                        lambda **kw: SimpleNamespace(new_sign_count=7))
    resp = client.post("/auth/passkey/verify", json={
        "state": data["state"], "credential": {"id": cred_id},
    })
    assert resp.status_code == 200
    assert resp.json()["user"]["email"] == _EMAIL
    assert "session" in resp.cookies

    stored = webauthn_store.get_credential(cred_id)
    assert stored["sign_count"] == 7
    assert stored["last_used"]

    # Challenge is single-use: same state again is rejected.
    resp = client.post("/auth/passkey/verify", json={
        "state": data["state"], "credential": {"id": cred_id},
    })
    assert resp.status_code == 401


def test_passkey_login_unknown_credential_or_bad_assertion(monkeypatch):
    sub = _mk_user()
    cred_id = _register(monkeypatch, sub)
    app.dependency_overrides.pop(get_current_user, None)

    state = client.post("/auth/passkey/options").json()["state"]
    resp = client.post("/auth/passkey/verify", json={
        "state": state, "credential": {"id": "unknown-credential"},
    })
    assert resp.status_code == 401

    def _boom(**kw):
        raise InvalidAuthenticationResponse("bad signature")

    monkeypatch.setattr(wa, "verify_authentication_response", _boom)
    state = client.post("/auth/passkey/options").json()["state"]
    resp = client.post("/auth/passkey/verify", json={
        "state": state, "credential": {"id": cred_id},
    })
    assert resp.status_code == 401
    assert "session" not in resp.cookies


def test_rename_and_delete_are_password_confirmed(monkeypatch):
    sub = _mk_user()
    cred_id = _register(monkeypatch, sub)

    r = client.put(f"/v1/users/me/passkeys/{cred_id}", json={"name": "New", "password": "wrong"})
    assert r.status_code == 401
    r = client.put(f"/v1/users/me/passkeys/{cred_id}", json={"name": "New", "password": _PW})
    assert r.status_code == 200
    assert webauthn_store.get_credential(cred_id)["name"] == "New"

    r = client.request("DELETE", f"/v1/users/me/passkeys/{cred_id}", json={"password": "wrong"})
    assert r.status_code == 401
    r = client.request("DELETE", f"/v1/users/me/passkeys/{cred_id}", json={"password": _PW})
    assert r.status_code == 200
    assert webauthn_store.get_credential(cred_id) is None


def test_cannot_touch_someone_elses_passkey(monkeypatch):
    other_sub = db.create_local_user("other@t.com", "O", "O", "member", hash_password(_PW))
    webauthn_store.add_credential("their-cred", other_sub, "pk", 0, "Theirs", [])

    _mk_user()
    r = client.put("/v1/users/me/passkeys/their-cred", json={"name": "Mine now", "password": _PW})
    assert r.status_code == 404
    r = client.request("DELETE", "/v1/users/me/passkeys/their-cred", json={"password": _PW})
    assert r.status_code == 404
    assert webauthn_store.get_credential("their-cred")["name"] == "Theirs"


def test_registered_passkey_satisfies_require_2fa(monkeypatch):
    sub = _mk_user()
    db.set_platform_setting("require_2fa", "1")
    clear_rate_limit("login", "testclient")

    login = client.post("/auth/login/local", json={"email": _EMAIL, "password": _PW}).json()
    assert login["user"]["must_enroll_2fa"] is True

    _register(monkeypatch, sub)
    login = client.post("/auth/login/local", json={"email": _EMAIL, "password": _PW}).json()
    assert "must_enroll_2fa" not in login["user"]
    assert client.get("/auth/me").json()["user"]["must_enroll_2fa"] is False


def test_credentials_cascade_on_user_delete(monkeypatch):
    sub = _mk_user()
    cred_id = _register(monkeypatch, sub)
    db.delete_user(sub)
    assert webauthn_store.get_credential(cred_id) is None


def test_native_handoff_token_exchange(monkeypatch):
    """Native-app flow: verify with native=true mints a one-time token (no
    cookie for the system browser); the webview exchanges it for a session."""
    sub = _mk_user()
    cred_id = _register(monkeypatch, sub)
    app.dependency_overrides.pop(get_current_user, None)

    state = client.post("/auth/passkey/options").json()["state"]
    monkeypatch.setattr(wa, "verify_authentication_response",
                        lambda **kw: SimpleNamespace(new_sign_count=1))
    resp = client.post("/auth/passkey/verify", json={
        "state": state, "credential": {"id": cred_id}, "native": True,
    })
    assert resp.status_code == 200
    token = resp.json()["native_token"]
    assert token
    assert "user" not in resp.json()
    assert "session" not in resp.cookies  # the system browser stays logged out

    resp = client.post("/auth/passkey/native/exchange", json={"token": token})
    assert resp.status_code == 200
    assert resp.json()["user"]["email"] == _EMAIL
    assert "session" in resp.cookies

    # Single-use: a replayed token is dead.
    resp = client.post("/auth/passkey/native/exchange", json={"token": token})
    assert resp.status_code == 401


def test_registration_and_login_options_require_uv():
    """UV required both sides: rejected at registration if the authenticator
    can't verify, enforced at login so passwordless is always ≥2 factors."""
    _mk_user()
    r = client.post("/v1/users/me/passkeys/register/options", json={"password": _PW})
    assert r.json()["options"]["authenticatorSelection"]["userVerification"] == "required"
    app.dependency_overrides.pop(get_current_user, None)
    assert client.post("/auth/passkey/options").json()["options"]["userVerification"] == "required"


def test_second_factor_mode_gates_passwordless_and_runs_step_flow(monkeypatch):
    sub = _mk_user()
    cred_id = _register(monkeypatch, sub)
    app.dependency_overrides.pop(get_current_user, None)
    db.set_platform_setting("passkey_login_mode", "second_factor")
    assert client.get("/auth/config").json()["passkey_login_mode"] == "second_factor"

    # Passwordless entry refused server-side — the mode is not cosmetic.
    assert client.post("/auth/passkey/options").status_code == 400
    state = client.post("/auth/passkey/options", json={}).status_code
    assert state == 400

    # Password login now demands step 2 with the passkey factor.
    clear_rate_limit("login", "testclient")
    login = client.post("/auth/login/local", json={"email": _EMAIL, "password": _PW}).json()
    assert login["requires_2fa"] is True
    assert login["second_factors"] == ["passkey"]
    step = login["totp_session_token"]

    # Step-scoped ceremony completes the login and consumes the token.
    opts = client.post("/auth/passkey/options", json={"totp_session_token": step})
    assert opts.status_code == 200
    data = opts.json()
    assert data["options"]["allowCredentials"]  # scoped to this user's creds
    monkeypatch.setattr(wa, "verify_authentication_response",
                        lambda **kw: SimpleNamespace(new_sign_count=2))
    resp = client.post("/auth/passkey/verify", json={
        "state": data["state"], "credential": {"id": cred_id},
        "totp_session_token": step,
    })
    assert resp.status_code == 200
    assert "session" in resp.cookies

    # Step token spent on success.
    assert client.post("/auth/passkey/options",
                       json={"totp_session_token": step}).status_code == 401


def test_step_token_binds_the_user(monkeypatch):
    """A step token for user A must not complete with user B's credential."""
    from auth.password import hash_password as _hp
    from auth.totp import create_2fa_session_token

    sub = _mk_user()
    cred_id = _register(monkeypatch, sub)
    other = db.create_local_user("other2@t.com", "O", "O", "member", _hp(_PW))
    app.dependency_overrides.pop(get_current_user, None)

    step_other = create_2fa_session_token(other)
    state = client.post("/auth/passkey/options",
                        json={"totp_session_token": step_other}).json()["state"]
    monkeypatch.setattr(wa, "verify_authentication_response",
                        lambda **kw: SimpleNamespace(new_sign_count=2))
    resp = client.post("/auth/passkey/verify", json={
        "state": state, "credential": {"id": cred_id},
        "totp_session_token": step_other,
    })
    assert resp.status_code == 401
    assert "session" not in resp.cookies


def test_passwordless_mode_offers_passkey_at_totp_step(monkeypatch):
    """Default mode: password login still works, but a TOTP-enrolled user with
    passkeys sees both factors offered at step 2."""
    import pyotp

    sub = _mk_user()
    cred_id = _register(monkeypatch, sub)
    # Enroll TOTP too (through the endpoints, as the user).
    setup = client.post("/v1/users/me/totp/setup").json()
    code = pyotp.TOTP(setup["secret"]).now()
    assert client.post("/v1/users/me/totp/verify", json={"code": code}).status_code == 200
    app.dependency_overrides.pop(get_current_user, None)

    clear_rate_limit("login", "testclient")
    login = client.post("/auth/login/local", json={"email": _EMAIL, "password": _PW}).json()
    assert login["requires_2fa"] is True
    assert login["second_factors"] == ["passkey", "totp"]

    # The passkey leg works against the TOTP-minted step token as well.
    step = login["totp_session_token"]
    data = client.post("/auth/passkey/options", json={"totp_session_token": step}).json()
    monkeypatch.setattr(wa, "verify_authentication_response",
                        lambda **kw: SimpleNamespace(new_sign_count=3))
    resp = client.post("/auth/passkey/verify", json={
        "state": data["state"], "credential": {"id": cred_id},
        "totp_session_token": step,
    })
    assert resp.status_code == 200


def test_native_exchange_rejects_expired_and_garbage(monkeypatch):
    sub = _mk_user()
    wa._native_tokens["stale-token"] = (sub, 0.0)  # already expired
    for tok in ("stale-token", "garbage"):
        resp = client.post("/auth/passkey/native/exchange", json={"token": tok})
        assert resp.status_code == 401
        assert "session" not in resp.cookies

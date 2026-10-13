"""TOTP 2FA lifecycle E2E + hardening regressions.

Covers the full self-service loop through the real endpoints — enroll →
login with 2FA → disable (password-confirmed) → re-enroll — plus the
hardening invariants: recovery codes carry ≥80 bits and are single-use,
and the 2FA step token is spent on successful use (replay must not mint
a second session).
"""

import time

import pyotp
import pytest
from fastapi.testclient import TestClient

from app import app
from auth import totp as totp_mod
from auth.password import hash_password
from auth.providers import UserContext, get_current_user
from auth.rate_limiter import clear_rate_limit
from storage import database as db

client = TestClient(app)

_PW = "correct-horse-battery-staple-42"
_EMAIL = "totp@t.com"


@pytest.fixture(autouse=True)
def _fresh_buckets():
    """The in-memory limiter + used-jti set persist across tests in a worker."""
    for bucket in ("login", "2fa"):
        clear_rate_limit(bucket, "testclient")
    totp_mod._used_jtis.clear()
    totp_mod._reserved_jtis.clear()
    yield
    for bucket in ("login", "2fa"):
        clear_rate_limit(bucket, "testclient")
    app.dependency_overrides.pop(get_current_user, None)


def _mk_user() -> str:
    sub = db.create_local_user(_EMAIL, "Totp", "Totp", "member", hash_password(_PW))

    async def _me():
        return UserContext(sub=sub, email=_EMAIL, name="Totp", role="member")

    app.dependency_overrides[get_current_user] = _me
    return sub


def _login() -> dict:
    resp = client.post("/auth/login/local", json={"email": _EMAIL, "password": _PW})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _next_code(secret: str) -> str:
    """The code of the next time step (inside the one-step skew window): the
    enrolment spent the current one."""
    return pyotp.TOTP(secret).at(time.time() + 30)


def _enroll() -> dict:
    setup = client.post("/v1/users/me/totp/setup").json()
    code = pyotp.TOTP(setup["secret"]).now()
    resp = client.post("/v1/users/me/totp/verify", json={"code": code})
    assert resp.status_code == 200, resp.text
    return setup


def test_full_lifecycle_enroll_login_disable_reenroll():
    _mk_user()

    # Before enrollment: plain login, no 2FA step.
    data = _login()
    assert "requires_2fa" not in data
    assert data["user"]["email"] == _EMAIL

    # Enroll. Codes are dash-grouped, 80 bits (20 hex chars) each.
    setup = _enroll()
    assert len(setup["recovery_codes"]) == 10
    for rc in setup["recovery_codes"]:
        assert len(rc.replace("-", "")) == 20
        assert rc == rc.upper()

    # Login now requires the 2FA step; a valid TOTP code completes it (the
    # next step's: the enrolment spent this one).
    data = _login()
    assert data["requires_2fa"] is True
    step_token = data["totp_session_token"]
    code = _next_code(setup["secret"])
    resp = client.post("/auth/login/2fa", json={"totp_session_token": step_token, "code": code})
    assert resp.status_code == 200
    assert resp.json()["user"]["email"] == _EMAIL
    assert "session" in resp.cookies

    # Disable: wrong password rejected, correct password disables.
    resp = client.request("DELETE", "/v1/users/me/totp", json={"password": "wrong"})
    assert resp.status_code == 401
    resp = client.request("DELETE", "/v1/users/me/totp", json={"password": _PW})
    assert resp.status_code == 200
    assert "requires_2fa" not in _login()

    # Re-enroll works from scratch.
    setup2 = _enroll()
    assert setup2["secret"] != setup["secret"]
    assert _login()["requires_2fa"] is True


def test_step_token_is_single_use():
    _mk_user()
    setup = _enroll()
    step_token = _login()["totp_session_token"]

    # A FAILED attempt must not consume the token — typos are retryable.
    resp = client.post("/auth/login/2fa", json={"totp_session_token": step_token, "code": "000000"})
    assert resp.status_code == 401
    code = _next_code(setup["secret"])
    resp = client.post("/auth/login/2fa", json={"totp_session_token": step_token, "code": code})
    assert resp.status_code == 200

    # Success spent it: replay with a valid code is rejected outright.
    resp = client.post("/auth/login/2fa", json={"totp_session_token": step_token, "code": code})
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()


def test_recovery_code_login_consumes_the_code():
    _mk_user()
    setup = _enroll()
    rc_dashed = setup["recovery_codes"][0]
    rc_plain = setup["recovery_codes"][1].replace("-", "").lower()

    # Dashed form works and is consumed.
    step = _login()["totp_session_token"]
    resp = client.post("/auth/login/2fa", json={"totp_session_token": step, "code": rc_dashed})
    assert resp.status_code == 200

    # The SAME code again: invalid (consumed).
    step = _login()["totp_session_token"]
    resp = client.post("/auth/login/2fa", json={"totp_session_token": step, "code": rc_dashed})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid 2FA code"

    # Canonicalization: undashed lowercase form of another code works too.
    step = _login()["totp_session_token"]
    resp = client.post("/auth/login/2fa", json={"totp_session_token": step, "code": rc_plain})
    assert resp.status_code == 200

    # Two codes spent → eight remain in the stored (encrypted) list.
    row = db.get_user_by_email(_EMAIL)
    remaining = totp_mod.decrypt_recovery_codes(row["totp_recovery_enc"])
    assert len(remaining) == 8


def test_recovery_code_entropy_and_canonical_hashing():
    codes = totp_mod.generate_recovery_codes()
    assert len(codes) == 10
    for c in codes:
        raw = c.replace("-", "")
        assert len(raw) == 20  # 80 bits
        int(raw, 16)  # hex

    hashed = totp_mod.hash_recovery_codes(codes)
    # All display forms of the same code hash identically.
    variants = [codes[0], codes[0].lower(), codes[0].replace("-", ""), codes[0].replace("-", " ")]
    for v in variants:
        matched, remaining = totp_mod.verify_recovery_code(v, hashed)
        assert matched and len(remaining) == 9


# ---------------------------------------------------------------------------
# One code is good once (RFC 6238 section 5.2), and one step token opens one
# session even when two requests carry it at once.
# ---------------------------------------------------------------------------


def test_the_step_window_is_one_step_either_way():
    secret = pyotp.random_base32()
    t = pyotp.TOTP(secret)
    now = 1_800_000_000.0
    base = int(now // 30)
    assert totp_mod.totp_step(secret, t.at(now), now=now) == base
    assert totp_mod.totp_step(secret, t.at(now - 30), now=now) == base - 1
    assert totp_mod.totp_step(secret, t.at(now + 30), now=now) == base + 1
    assert totp_mod.totp_step(secret, t.at(now - 60), now=now) is None
    assert totp_mod.totp_step(secret, "12345", now=now) is None
    assert totp_mod.totp_step(secret, "abcdef", now=now) is None
    # Digits another keyboard types are the same code; other characters
    # that call themselves digits are no code (and never an error).
    wide = "".join(chr(ord(d) + 0xFEE0) for d in t.at(now))
    assert totp_mod.totp_step(secret, wide, now=now) == base
    assert totp_mod.totp_step(secret, "\u00b2" * 6, now=now) is None


def test_the_step_claim_moves_forward_only():
    sub = _mk_user()
    assert db.get_user(sub)["totp_last_step"] == 0
    assert db.claim_totp_step(sub, 100) is True
    assert db.claim_totp_step(sub, 100) is False
    assert db.claim_totp_step(sub, 99) is False
    assert db.claim_totp_step(sub, 101) is True
    assert db.get_user(sub)["totp_last_step"] == 101


def test_a_code_sent_again_within_its_step_is_refused_also_after_a_restart():
    """A fresh step token with a code already accepted: refused with a
    reason, and the stored step outlives the process (the in-memory token
    sets are what a restart forgets)."""
    _mk_user()
    setup = _enroll()
    code = _next_code(setup["secret"])
    first = client.post("/auth/login/2fa", json={
        "totp_session_token": _login()["totp_session_token"], "code": code})
    assert first.status_code == 200
    totp_mod._used_jtis.clear()
    totp_mod._reserved_jtis.clear()
    again = client.post("/auth/login/2fa", json={
        "totp_session_token": _login()["totp_session_token"], "code": code})
    assert again.status_code == 401
    assert "already used" in again.json()["detail"]
    assert "session" not in again.cookies


def test_two_requests_with_one_step_token_open_one_session():
    import asyncio

    import httpx

    _mk_user()
    setup = _enroll()
    step_token = _login()["totp_session_token"]
    code = _next_code(setup["secret"])

    async def both():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            return await asyncio.gather(*[
                ac.post("/auth/login/2fa", json={"totp_session_token": step_token, "code": code})
                for _ in range(2)])
    answers = asyncio.run(both())
    assert sorted(r.status_code for r in answers) == [200, 409]
    assert sum("session" in r.cookies for r in answers) == 1
    assert not totp_mod._reserved_jtis


def test_a_refused_attempt_gives_the_step_token_back():
    _mk_user()
    setup = _enroll()
    step_token = _login()["totp_session_token"]
    for _ in range(2):
        resp = client.post("/auth/login/2fa", json={"totp_session_token": step_token,
                                                    "code": "000000"})
        assert resp.status_code == 401
        assert not totp_mod._reserved_jtis
    resp = client.post("/auth/login/2fa", json={"totp_session_token": step_token,
                                                "code": _next_code(setup["secret"])})
    assert resp.status_code == 200


def test_the_enrolment_code_counts_as_used():
    _mk_user()
    setup = client.post("/v1/users/me/totp/setup").json()
    code = pyotp.TOTP(setup["secret"]).now()
    assert client.post("/v1/users/me/totp/verify", json={"code": code}).status_code == 200
    again = client.post("/v1/users/me/totp/verify", json={"code": code})
    assert again.status_code == 400 and "already used" in again.json()["detail"]


def test_a_new_secret_starts_its_own_step_count():
    sub = _mk_user()
    _enroll()
    assert db.get_user(sub)["totp_last_step"] > 0
    resp = client.post("/v1/users/me/totp/setup", json={"password": _PW})
    assert resp.status_code == 200
    assert db.get_user(sub)["totp_last_step"] == 0


def test_the_admin_user_list_carries_no_totp_state():
    sub = _mk_user()
    _enroll()

    async def _admin():
        return UserContext(sub="user-admin", email="admin@test.com", name="A", role="admin")
    app.dependency_overrides[get_current_user] = _admin
    users = client.get("/v1/admin/users").json()["users"]
    row = next(u for u in users if u["sub"] == sub)
    assert not {"totp_secret_enc", "totp_recovery_enc", "totp_last_step"} & set(row)


def test_the_migration_adds_the_step_column_to_a_populated_table():
    from storage import schema as pg_schema
    from storage.pg import get_conn
    sub = _mk_user()
    with get_conn() as conn:
        conn.execute("ALTER TABLE users DROP COLUMN totp_last_step")
        assert not pg_schema._column_exists(conn, "users", "totp_last_step")
        pg_schema.run_migrations(conn)
        row = conn.execute("SELECT totp_last_step FROM users WHERE sub=%s", (sub,)).fetchone()
        assert row["totp_last_step"] == 0
        conn.rollback()

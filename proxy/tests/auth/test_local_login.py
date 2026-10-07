"""The local password login.

bcrypt and zxcvbn run in worker threads behind their semaphores, never on
the event loop; the login's store reads and writes go through the DB
executor; the per-address bucket bounds a burst; the account tarpit arms at
the fifth failure whatever the concurrency, never counts its own refusals,
and a browser that completed a full login (the trusted-device cookie) skips
it. Handlers are driven directly inside ``asyncio.run`` where the test is
about the loop (``TestClient`` runs the app on another thread).
"""

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone

import bcrypt
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import config
from api.auth import identity
from app import app
from auth import lan_check, password, rate_limiter
from auth.password import hash_password
from auth.providers import create_session_jwt, local_provider
from storage import database as db

_PW = "correct-horse-battery-staple-77"
_EMAIL = "login@t.com"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", False)
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [])
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    lan_check.reset_state()
    rate_limiter._attempts.clear()
    local_provider._tarpitted_until.clear()
    yield
    rate_limiter._attempts.clear()
    local_provider._tarpitted_until.clear()


def _user(email=_EMAIL, pw=_PW) -> str:
    return db.create_local_user(email, "L", "L", "member", hash_password(pw))


def _request(ip="198.51.100.10", cookie=""):
    headers = [(b"content-type", b"application/json")]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    return Request({"type": "http", "method": "POST", "path": "/auth/login/local",
                    "headers": headers, "client": (ip, 1234), "server": ("testserver", 80),
                    "scheme": "http", "query_string": b""})


async def _login(email, pw, ip="198.51.100.10", cookie=""):
    try:
        r = await identity.auth_login_local(
            identity.LocalLoginRequest(email=email, password=pw), _request(ip, cookie))
        return getattr(r, "status_code", 200)
    except identity.HTTPException as e:
        return e.status_code


def _client(ip="198.51.100.10") -> TestClient:
    return TestClient(app, client=(ip, 40000))


def _post_login(c, email=_EMAIL, pw=_PW):
    return c.post("/auth/login/local", json={"email": email, "password": pw})


# ── off the event loop ────────────────────────────────────────────────────


def test_bcrypt_runs_off_the_loop_on_the_miss_and_the_wrong_password(monkeypatch):
    _user()
    seen = []
    real_verify, real_dummy = password.verify_password, password.dummy_verify
    monkeypatch.setattr(password, "verify_password",
                        lambda p, h: seen.append(threading.get_ident()) or real_verify(p, h))
    monkeypatch.setattr(password, "dummy_verify",
                        lambda: seen.append(threading.get_ident()) or real_dummy())

    async def scenario():
        loop_thread = threading.get_ident()
        assert await _login("nobody@t.com", "x" * 12) == 401
        assert await _login(_EMAIL, "wrong-password-123") == 401
        return loop_thread

    loop_thread = asyncio.run(scenario())
    assert len(seen) == 2 and loop_thread not in seen


def test_the_login_and_2fa_handlers_make_no_store_call_on_the_loop(loop_db_guard):
    import pyotp

    from auth.totp import encrypt_totp_secret
    sub = _user()

    async def scenario():
        with loop_db_guard.active():
            assert await _login("nobody@t.com", "x" * 12, ip="198.51.100.11") == 401
            assert await _login(_EMAIL, "wrong-password-123", ip="198.51.100.12") == 401
            assert await _login(_EMAIL, _PW, ip="198.51.100.13") == 200
        secret = pyotp.random_base32()
        await asyncio.to_thread(db.update_user_auth_fields, sub,
                                totp_secret_enc=encrypt_totp_secret(secret), totp_enabled=True)
        with loop_db_guard.active():
            step = await identity.auth_login_local(
                identity.LocalLoginRequest(email=_EMAIL, password=_PW), _request("198.51.100.14"))
            r = await identity.auth_login_2fa(
                identity.TwoFactorRequest(totp_session_token=step["totp_session_token"],
                                          code=pyotp.TOTP(secret).now()),
                _request("198.51.100.14"))
        assert r.status_code == 200

    asyncio.run(scenario())


def test_the_strength_check_runs_off_the_loop(monkeypatch):
    seen = []
    real = password.zxcvbn
    monkeypatch.setattr(password, "zxcvbn", lambda p: seen.append(threading.get_ident()) or real(p))

    async def scenario():
        ok, _, _ = await password.check_password_strength_async("a-Long-and-Strong-pass-9713")
        return threading.get_ident(), ok

    loop_thread, ok = asyncio.run(scenario())
    assert ok and seen and loop_thread not in seen


# ── bursts ────────────────────────────────────────────────────────────────


def _slow_bcrypt(monkeypatch, seconds=0.15):
    calls = []

    def slow(*_a):
        calls.append(time.monotonic())
        time.sleep(seconds)
        return False

    monkeypatch.setattr(password, "verify_password", slow)
    monkeypatch.setattr(password, "dummy_verify", slow)
    return calls


def test_a_burst_from_one_address_gets_at_most_the_bucket_through(monkeypatch):
    calls = _slow_bcrypt(monkeypatch)
    cap = config.RATE_LIMIT_RULES["login"]["max"]

    async def scenario():
        burst = [_login(f"nobody{i}@t.com", "x" * 12, ip="203.0.113.50") for i in range(cap + 6)]
        started = time.monotonic()
        other = asyncio.create_task(_login("someone@t.com", "x" * 12, ip="203.0.113.51"))
        codes = await asyncio.gather(*burst)
        assert await other == 401
        return codes, time.monotonic() - started

    codes, _ = asyncio.run(scenario())
    assert codes.count(401) == cap and codes.count(429) == 6
    assert len(calls) == cap + 1


def test_the_hash_queue_answers_503_past_its_waiters(monkeypatch):
    _slow_bcrypt(monkeypatch, 0.3)
    monkeypatch.setattr(config, "AUTH_HASH_CONCURRENCY", 1)
    monkeypatch.setattr(config, "AUTH_HASH_MAX_WAITERS", 1)
    monkeypatch.setattr(password, "_gates", password._Gates())

    async def scenario():
        return await asyncio.gather(*[
            _login(f"nobody{i}@t.com", "x" * 12, ip=f"203.0.113.{60 + i}") for i in range(3)])

    codes = asyncio.run(scenario())
    assert sorted(codes) == [401, 401, 503]


def test_the_tarpit_arms_at_five_under_concurrency(monkeypatch):
    # A guard for the count-before-verify design: with the verify awaited, the
    # check and the count must not be one loop slice apart.
    _user()
    calls = _slow_bcrypt(monkeypatch)

    async def scenario():
        return await asyncio.gather(*[
            _login(_EMAIL, "wrong-password-123", ip=f"203.0.113.{70 + i}") for i in range(8)])

    codes = asyncio.run(scenario())
    assert len(calls) == 5 and codes.count(401) == 5 and codes.count(429) == 3


# ── a full login gives back its own attempt only ──────────────────────────


def test_a_full_login_leaves_the_addresss_other_failures_counted(monkeypatch):
    """A person's own sign-in never wipes the failures the address counted
    against other accounts: they stay counted until their window lapses."""
    monkeypatch.setattr(password, "dummy_verify", lambda: None)
    _user()
    cap = config.RATE_LIMIT_RULES["login"]["max"]
    ip = "198.51.100.60"

    async def scenario():
        misses = [await _login(f"nobody{i}@t.com", "x" * 12, ip=ip) for i in range(cap - 1)]
        assert misses == [401] * (cap - 1)
        assert await _login(_EMAIL, _PW, ip=ip) == 200
        return [await _login(f"later{i}@t.com", "x" * 12, ip=ip) for i in range(2)]

    assert asyncio.run(scenario()) == [401, 429]


def test_a_2fa_completion_gives_back_its_own_attempt_only():
    import pyotp

    from auth.totp import encrypt_totp_secret
    sub = _user()
    secret = pyotp.random_base32()
    db.update_user_auth_fields(sub, totp_secret_enc=encrypt_totp_secret(secret), totp_enabled=True)
    ip = "198.51.100.61"

    async def scenario():
        step = await identity.auth_login_local(
            identity.LocalLoginRequest(email=_EMAIL, password=_PW), _request(ip))

        def code(value):
            return identity.TwoFactorRequest(totp_session_token=step["totp_session_token"],
                                             code=value)

        for _ in range(3):
            with pytest.raises(identity.HTTPException) as e:
                await identity.auth_login_2fa(code("1234567"), _request(ip))
            assert e.value.status_code == 401
        r = await identity.auth_login_2fa(code(pyotp.TOTP(secret).now()), _request(ip))
        assert r.status_code == 200

    asyncio.run(scenario())
    assert rate_limiter._attempts[("2fa", ip)]["count"] == 3


# ── the tarpit ────────────────────────────────────────────────────


def _arm_tarpit(sub, attempts=5, ago_s=0):
    when = (datetime.now(timezone.utc) - timedelta(seconds=ago_s)).isoformat()
    db.update_user_auth_fields(sub, failed_login_attempts=attempts, last_failed_login=when)


def test_a_tarpit_refusal_does_not_count_against_the_address():
    sub = _user()
    _arm_tarpit(sub, attempts=9)
    c = _client("198.51.100.30")
    for _ in range(10):
        assert _post_login(c).status_code == 429
    _arm_tarpit(sub, ago_s=3600)
    local_provider._tarpitted_until.clear()
    assert _post_login(c).status_code == 200


def test_a_trusted_device_survives_the_tarpit_and_an_anonymous_client_does_not():
    sub = _user()
    victim = _client("198.51.100.40")
    assert _post_login(victim).status_code == 200
    assert victim.cookies.get("otodock_device")
    assert _post_login(_client("203.0.113.80"), pw="wrong-password-123").status_code == 401
    _arm_tarpit(sub, attempts=9)
    assert _post_login(_client("198.51.100.41")).status_code == 429
    assert _post_login(victim).status_code == 200


def test_a_trusted_device_failure_counts_against_the_device_not_the_account():
    sub = _user()
    c = _client("198.51.100.42")
    assert _post_login(c).status_code == 200
    for _ in range(3):
        assert _post_login(c, pw="wrong-password-123").status_code == 401
    assert (db.get_user(sub).get("failed_login_attempts") or 0) == 0
    claims = rate_limiter.device_token_claims(c.cookies.get("otodock_device"))
    assert rate_limiter._attempts[("login_device", claims["jti"])]["count"] == 3


def _device_cookie(ip: str) -> tuple[str, str]:
    """A trusted-device cookie from a full login, and its device key."""
    c = _client(ip)
    assert _post_login(c).status_code == 200
    token = c.cookies.get("otodock_device")
    return f"otodock_device={token}", rate_limiter.device_token_claims(token)["jti"]


def test_a_burst_carrying_one_device_cookie_meets_the_device_cap(monkeypatch):
    """The device attempt is counted before the hash is awaited: a burst of
    wrong passwords presenting one trusted-device cookie from many addresses
    gets the device's five through, then meets the account tarpit."""
    sub = _user()
    cookie, jti = _device_cookie("198.51.100.48")
    calls = _slow_bcrypt(monkeypatch)
    device_cap = config.RATE_LIMIT_RULES["login_device"]["max"]

    async def scenario():
        return await asyncio.gather(*[
            _login(_EMAIL, "wrong-password-123", ip=f"203.0.113.{100 + i}", cookie=cookie)
            for i in range(12)])

    codes = asyncio.run(scenario())
    # Five on the device, then five more the tarpit lets through before it arms.
    assert len(calls) == device_cap + 5
    assert codes.count(401) == device_cap + 5 and codes.count(429) == 12 - device_cap - 5
    assert (db.get_user(sub).get("failed_login_attempts") or 0) == 5
    assert rate_limiter._attempts[("login_device", jti)]["count"] == device_cap


def test_a_right_password_gives_the_device_attempt_back():
    _user()
    cookie, jti = _device_cookie("198.51.100.49")

    async def scenario():
        for i in range(8):
            assert await _login(_EMAIL, _PW, ip=f"203.0.113.{130 + i}", cookie=cookie) == 200

    asyncio.run(scenario())
    assert rate_limiter._attempts.get(("login_device", jti), {"count": 0})["count"] == 0


def _gate_full(monkeypatch):
    async def busy(*_a):
        raise password.HashBusy()

    monkeypatch.setattr(local_provider, "verify_password_async", busy)


def test_a_full_hash_gate_leaves_the_account_count_unchanged(monkeypatch):
    """Nothing was tested, so nothing is counted against the account."""
    sub = _user()

    async def attempt(ip):
        return await _login(_EMAIL, "wrong-password-123", ip=ip)

    _gate_full(monkeypatch)
    assert asyncio.run(attempt("203.0.113.140")) == 503
    row = db.get_user(sub)
    assert (row.get("failed_login_attempts") or 0) == 0 and row.get("last_failed_login") is None

    _arm_tarpit(sub, attempts=3, ago_s=60)
    assert asyncio.run(attempt("203.0.113.141")) == 503
    row = db.get_user(sub)
    assert row.get("failed_login_attempts") == 3


def test_the_failure_undo_is_the_users_stores_own_statement():
    """One less, the last failure kept while another remains, never below
    zero; it sits beside ``record_failed_login`` in the store."""
    sub = _user()
    db.record_failed_login(sub)
    db.record_failed_login(sub)
    db.undo_failed_login(sub)
    row = db.get_user(sub)
    assert row["failed_login_attempts"] == 1 and row["last_failed_login"] is not None
    db.undo_failed_login(sub)
    db.undo_failed_login(sub)
    row = db.get_user(sub)
    assert row["failed_login_attempts"] == 0 and row["last_failed_login"] is None


def test_a_full_hash_gate_gives_the_device_attempt_back(monkeypatch):
    _user()
    cookie, jti = _device_cookie("198.51.100.50")
    _gate_full(monkeypatch)

    async def scenario():
        return [await _login(_EMAIL, "wrong-password-123", ip=f"203.0.113.{150 + i}",
                             cookie=cookie) for i in range(3)]

    assert asyncio.run(scenario()) == [503, 503, 503]
    assert rate_limiter._attempts.get(("login_device", jti), {"count": 0})["count"] == 0


def test_a_password_change_voids_the_device_cookie():
    sub = _user()
    c = _client("198.51.100.43")
    assert _post_login(c).status_code == 200
    from storage.pg import get_conn
    later = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_changed_at=%s WHERE sub=%s", (later, sub))
        conn.commit()
    _arm_tarpit(sub, attempts=9)
    assert _post_login(c).status_code == 429


def test_the_device_cookie_attributes_and_its_replacement():
    _user()
    other = _user("other@t.com")
    c = _client("198.51.100.44")
    r = _post_login(c)
    (device,) = [h for h in r.headers.get_list("set-cookie") if h.startswith("otodock_device=")]
    assert "HttpOnly" in device and f"Max-Age={180 * 86400}" in device and "Secure" not in device
    first = rate_limiter.device_token_claims(c.cookies.get("otodock_device"))
    assert _post_login(c, email="other@t.com").status_code == 200
    second = rate_limiter.device_token_claims(c.cookies.get("otodock_device"))
    assert first["sub"] != second["sub"] == other


def test_a_device_token_is_no_session_and_a_session_token_no_device():
    sub = _user()
    device = rate_limiter.mint_device_token(sub)
    assert TestClient(app, cookies={"session": device}).get("/auth/me").status_code == 401
    session = create_session_jwt(sub, _EMAIL, "L", "member")
    assert rate_limiter.device_token_claims(session) is None
    _arm_tarpit(sub, attempts=9)
    c = TestClient(app, client=("198.51.100.45", 40000), cookies={"otodock_device": session})
    assert _post_login(c).status_code == 429


# ── the 72-byte cap ────────────────────────────────────────────────


_LONG = "Aa1!" * 18 + "x"   # 73 bytes


def test_a_password_over_72_bytes_is_refused_before_zxcvbn(monkeypatch):
    def never(_p):
        raise AssertionError("zxcvbn ran")

    monkeypatch.setattr(password, "zxcvbn", never)
    ok, msg, _ = password.check_password_strength(_LONG)
    assert not ok and "72 bytes" in msg
    assert not password.check_password_strength("é" * 37)[0]   # 74 bytes
    with pytest.raises(ValueError):
        password.hash_password(_LONG)


def test_the_change_and_reset_routes_refuse_a_long_password_fast(monkeypatch):
    import jwt as pyjwt
    sub = _user()

    def never(_p):
        raise AssertionError("zxcvbn ran")

    monkeypatch.setattr(password, "zxcvbn", never)
    c = TestClient(app, cookies={"session": create_session_jwt(sub, _EMAIL, "L", "member")})
    r = c.put("/v1/users/me/password", json={"current_password": _PW, "new_password": _LONG})
    assert r.status_code == 400 and "72 bytes" in r.json()["detail"]
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_changed_at=NULL WHERE sub=%s", (sub,))
        conn.commit()
    token = pyjwt.encode({"sub": sub, "purpose": "password_reset", "iat": int(time.time()),
                          "exp": int(time.time()) + 3600}, config.JWT_SECRET, algorithm="HS256")
    r = TestClient(app).post("/auth/reset-password", json={"token": token, "new_password": _LONG})
    assert r.status_code == 400 and "72 bytes" in r.json()["detail"]


def test_a_long_password_set_under_bcrypt_4_logs_in_again():
    long_pw = "Correct-Horse-Battery-Staple-" * 3   # 87 bytes
    legacy = bcrypt.hashpw(long_pw.encode()[:72], bcrypt.gensalt(rounds=4)).decode()
    db.create_local_user("legacy@t.com", "L", "L", "member", legacy)
    assert _post_login(_client("198.51.100.46"), email="legacy@t.com", pw=long_pw).status_code == 200


def test_the_password_change_keeps_the_changers_device_trusted():
    sub = _user()
    c = _client("198.51.100.47")
    assert _post_login(c).status_code == 200
    new_pw = "another-Strong-passphrase-4471"
    r = c.put("/v1/users/me/password", json={"current_password": _PW, "new_password": new_pw})
    assert r.status_code == 200, r.text
    claims = rate_limiter.device_token_claims(c.cookies.get("otodock_device"))
    assert rate_limiter.trusted_device_for(claims, db.get_user(sub)) == claims["jti"]


def test_the_password_confirm_answers_503_past_the_hash_gate(monkeypatch):
    """The confirm of an already-authed session (links, passkey management)
    shares the login's bounded bcrypt gate: a full gate is a 503 with
    Retry-After, never a thread queued without bound or a hash on the loop."""
    import types
    from auth import confirm

    _slow_bcrypt(monkeypatch, 0.3)
    monkeypatch.setattr(config, "AUTH_HASH_CONCURRENCY", 1)
    monkeypatch.setattr(config, "AUTH_HASH_MAX_WAITERS", 0)
    monkeypatch.setattr(password, "_gates", password._Gates())
    person = types.SimpleNamespace(sub=_user())

    async def scenario():
        hold = asyncio.create_task(password.verify_password_async("x", "y"))
        await asyncio.sleep(0.05)
        try:
            await confirm.confirm_password(person, _PW)
        except identity.HTTPException as e:
            outcome = (e.status_code, dict(e.headers or {}))
        else:
            outcome = (200, {})
        await hold
        return outcome

    code, headers = asyncio.run(scenario())
    assert code == 503 and headers.get("Retry-After")

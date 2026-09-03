"""Tokenized admin invite links: mint on create, accept-invite, single-use.

The invite is a signed JWT (purpose="invite", 48h) minted by
POST /v1/admin/users when no password is given; the account stays inert
(empty password_hash → login impossible) until POST /auth/accept-invite sets
one. Single-use is structural: accepting — or an admin password reset — gives
the account a password, which permanently invalidates every outstanding token.
"""

import time
from urllib.parse import parse_qs, urlparse

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.password import verify_password
from auth.providers import UserContext, get_current_user, validate_session_jwt
from auth.rate_limiter import clear_rate_limit
from storage import database as db

client = TestClient(app)

_STRONG_PW = "correct-horse-battery-staple-99"


@pytest.fixture(autouse=True)
def _clean_slate(monkeypatch):
    """Admin session, permissive seat limit, and a fresh invite rate bucket."""
    from api.auth import admin_users

    async def _admin():
        return UserContext(sub="local:admin", email="admin@t.com", name="Admin", role="admin")

    app.dependency_overrides[get_current_user] = _admin
    monkeypatch.setattr(admin_users, "check_seat_limit", lambda: (True, 1, 5))
    clear_rate_limit("invite", "testclient")
    yield
    app.dependency_overrides.pop(get_current_user, None)
    clear_rate_limit("invite", "testclient")


def _create_invited(email="invitee@t.com", **extra) -> dict:
    resp = client.post("/v1/admin/users", json={
        "email": email, "display_name": "Invitee", "role": "member", **extra,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def _token_of(invite_url: str) -> str:
    return parse_qs(urlparse(invite_url).query)["token"][0]


def test_create_without_password_returns_invite_link(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://dash.example.com")
    data = _create_invited()
    assert "temp_password" not in data
    assert data["invite_url"].startswith("https://dash.example.com/accept-invite?token=")

    payload = pyjwt.decode(_token_of(data["invite_url"]), config.JWT_SECRET, algorithms=["HS256"])
    assert payload["purpose"] == "invite"
    assert payload["exp"] - payload["iat"] == 48 * 3600

    # Account is inert: no password, and NOT flagged must-change (the user
    # picks their own via the invite, not a temp one).
    row = db.get_user_by_email("invitee@t.com")
    assert row["password_hash"] == ""
    assert not row["must_change_password"]


def test_invite_url_relative_without_public_url(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    data = _create_invited()
    assert data["invite_url"].startswith("/accept-invite?token=")


def test_accept_invite_sets_password_single_use(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    token = _token_of(_create_invited()["invite_url"])

    resp = client.post("/auth/accept-invite", json={"token": token, "new_password": _STRONG_PW})
    assert resp.status_code == 200
    assert resp.json()["email"] == "invitee@t.com"
    row = db.get_user_by_email("invitee@t.com")
    assert verify_password(_STRONG_PW, row["password_hash"])

    # Replay: the account now has a password → the same token is dead.
    resp = client.post("/auth/accept-invite", json={"token": token, "new_password": _STRONG_PW})
    assert resp.status_code == 400
    assert "already been used" in resp.json()["detail"]


def test_admin_password_reset_consumes_outstanding_invite(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    data = _create_invited()
    sub = data["user"]["sub"]

    assert client.post(f"/v1/admin/users/{sub}/reset-password").status_code == 200
    resp = client.post("/auth/accept-invite", json={
        "token": _token_of(data["invite_url"]), "new_password": _STRONG_PW,
    })
    assert resp.status_code == 400
    assert "already been used" in resp.json()["detail"]


def test_accept_invite_rejects_expired_and_foreign_tokens(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    data = _create_invited()
    sub = data["user"]["sub"]

    expired = pyjwt.encode(
        {"sub": sub, "purpose": "invite",
         "iat": int(time.time()) - 200_000, "exp": int(time.time()) - 100},
        config.JWT_SECRET, algorithm="HS256",
    )
    resp = client.post("/auth/accept-invite", json={"token": expired, "new_password": _STRONG_PW})
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"]

    # A password-reset token must not work as an invite (wrong purpose).
    reset_tok = pyjwt.encode(
        {"sub": sub, "purpose": "password_reset",
         "iat": int(time.time()), "exp": int(time.time()) + 3600},
        config.JWT_SECRET, algorithm="HS256",
    )
    resp = client.post("/auth/accept-invite", json={"token": reset_tok, "new_password": _STRONG_PW})
    assert resp.status_code == 400

    resp = client.post("/auth/accept-invite", json={"token": "garbage", "new_password": _STRONG_PW})
    assert resp.status_code == 400


def test_accept_invite_enforces_password_policy(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    token = _token_of(_create_invited()["invite_url"])
    resp = client.post("/auth/accept-invite", json={"token": token, "new_password": "password"})
    assert resp.status_code == 400
    # Account stays inert after the rejected attempt.
    assert db.get_user_by_email("invitee@t.com")["password_hash"] == ""


def test_invite_token_rejected_as_session_cookie(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    token = _token_of(_create_invited()["invite_url"])
    assert validate_session_jwt(token) is None


def test_send_invite_requires_smtp_and_public_url(monkeypatch):
    from services.notifications import smtp as smtp_mod

    monkeypatch.setattr(smtp_mod, "is_smtp_configured", lambda: False)
    resp = client.post("/v1/admin/users", json={
        "email": "a@t.com", "display_name": "A", "role": "member", "send_invite": True,
    })
    assert resp.status_code == 400
    assert "SMTP" in resp.json()["detail"]

    monkeypatch.setattr(smtp_mod, "is_smtp_configured", lambda: True)
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    resp = client.post("/v1/admin/users", json={
        "email": "a@t.com", "display_name": "A", "role": "member", "send_invite": True,
    })
    assert resp.status_code == 400
    assert "DASHBOARD_PUBLIC_URL" in resp.json()["detail"]

    # Neither failure may leave an inert account behind.
    assert db.get_user_by_email("a@t.com") is None


def test_send_invite_emails_the_tokenized_link(monkeypatch):
    from services.notifications import smtp as smtp_mod

    sent: dict = {}

    def _capture(to, invite_url, inviter_name=""):
        sent.update(to=to, url=invite_url, inviter=inviter_name)
        return True

    monkeypatch.setattr(smtp_mod, "is_smtp_configured", lambda: True)
    monkeypatch.setattr(smtp_mod, "send_invite_email", _capture)
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://dash.example.com")

    data = _create_invited(send_invite=True)
    assert data["invite_sent"] is True
    assert sent["to"] == "invitee@t.com"
    assert sent["url"] == data["invite_url"]
    payload = pyjwt.decode(_token_of(sent["url"]), config.JWT_SECRET, algorithms=["HS256"])
    assert payload["purpose"] == "invite"


def test_temp_password_mode_unchanged(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    data = _create_invited(password=_STRONG_PW)
    assert data["temp_password"] == _STRONG_PW
    assert "invite_url" not in data
    row = db.get_user_by_email("invitee@t.com")
    assert row["must_change_password"]


def test_email_links_available_needs_smtp_and_public_url(monkeypatch):
    from services.notifications import smtp as smtp_mod

    cases = [
        (False, "", False),
        (True, "", False),
        (False, "https://dash.example.com", False),
        (True, "https://dash.example.com", True),
    ]
    for smtp_on, url, expected in cases:
        monkeypatch.setattr(smtp_mod, "is_smtp_configured", lambda v=smtp_on: v)
        monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", url)
        # /auth/config keys smtp_configured on the smtp_host setting directly;
        # set it so both fields stay consistent for the case.
        db.set_platform_setting("smtp_host", "mail.example.com" if smtp_on else "")
        data = client.get("/auth/config").json()
        assert data["email_links_available"] is expected, (smtp_on, url)


def test_forgot_password_never_sends_broken_relative_links(monkeypatch):
    """SMTP on but no public URL → generic 200, and NO email goes out (an
    emailed relative link wouldn't resolve; header-derived bases are a
    reset-poisoning vector)."""
    from services.notifications import smtp as smtp_mod

    _create_invited(password=_STRONG_PW)
    clear_rate_limit("forgot", "testclient")
    clear_rate_limit("forgot", "email:invitee@t.com")

    sent: list = []
    monkeypatch.setattr(smtp_mod, "is_smtp_configured", lambda: True)
    monkeypatch.setattr(smtp_mod, "send_password_reset_email",
                        lambda to, url: sent.append((to, url)) or True)

    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    resp = client.post("/auth/forgot-password", json={"email": "invitee@t.com"})
    assert resp.status_code == 200
    assert sent == []

    clear_rate_limit("forgot", "testclient")
    clear_rate_limit("forgot", "email:invitee@t.com")
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://dash.example.com")
    resp = client.post("/auth/forgot-password", json={"email": "invitee@t.com"})
    assert resp.status_code == 200
    # The send is fire-and-forget (a background task, so the hit and miss
    # paths answer equally fast); under a loaded xdist run it can land after
    # the response — wait for it instead of asserting immediately.
    for _ in range(100):
        if sent:
            break
        time.sleep(0.02)
    assert len(sent) == 1
    assert sent[0][1].startswith("https://dash.example.com/reset-password?token=")


def test_admin_list_users_strips_secrets_and_flags_pending(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    _create_invited()
    _create_invited(email="active@t.com", password=_STRONG_PW)

    users = {u["email"]: u for u in client.get("/v1/admin/users").json()["users"]}
    for u in users.values():
        assert "password_hash" not in u
        assert "totp_secret_enc" not in u
        assert "totp_recovery_enc" not in u
    assert users["invitee@t.com"]["invite_pending"] is True
    assert users["active@t.com"]["invite_pending"] is False

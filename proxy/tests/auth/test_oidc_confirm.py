"""The identity-provider confirm (SHARING.md "The confirm"): an account
that signed in through the provider proves the person at the keyboard
with a round trip there. The start route mints the same login URL a
sign-in uses, carrying a confirm-purpose state (a new login is asked for
only under ``OIDC_CONFIRM_FRESH_LOGIN``); the callback's confirm branch
checks the account and the audience (and, strict, the freshness), issues
no session and answers a one-shot confirm token; ``confirm_human`` names
the method for such an account.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, quote, urlparse

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import config
from api.auth import identity
from app import app
from auth import confirm
from auth.providers import UserContext, _oauth_states, get_current_user
from auth.providers.base import AuthResult
from storage import database as task_store

client = TestClient(app)

SUB = "oidc-confirm-sub"
LOCAL = "local-confirm-sub"


def _ctx(sub: str = SUB, is_api_key: bool = False) -> UserContext:
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="member",
                       auth_provider="oidc:mock-sso", is_api_key=is_api_key)


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    """An enabled provider with explicit endpoints (no discovery), one
    account from it and one local account; the confirm bucket cleared."""
    monkeypatch.setattr(config, "OIDC_ENABLED", True)
    monkeypatch.setattr(config, "OIDC_DISCOVERY_URL", "")
    monkeypatch.setattr(config, "OIDC_AUTHORIZE_URL", "https://idp.example.com/authorize")
    monkeypatch.setattr(config, "OIDC_TOKEN_URL", "https://idp.example.com/token")
    monkeypatch.setattr(config, "OIDC_USERINFO_URL", "https://idp.example.com/userinfo")
    monkeypatch.setattr(config, "OIDC_CLIENT_ID", "test-client")
    monkeypatch.setattr(config, "OIDC_PROVIDER_NAME", "Mock SSO")
    monkeypatch.setattr(config, "OIDC_REDIRECT_URI", "https://dash.example.com/auth/callback")
    monkeypatch.setattr(config, "OIDC_CONFIRM_FRESH_LOGIN", False)
    monkeypatch.setattr(config, "OIDC_CONFIRM_REQUIRE_AUTH_TIME", False)
    task_store.upsert_user(SUB, f"{SUB}@test.com", "Oidc Person", "member")
    task_store.update_user_auth_fields(SUB, auth_provider="oidc:mock-sso")
    task_store.upsert_user(LOCAL, f"{LOCAL}@test.com", "Local Person", "member")
    task_store.update_user_auth_fields(LOCAL, auth_provider="local")
    from auth import rate_limiter
    rate_limiter._attempts.clear()
    client.cookies.clear()
    _as(_ctx())
    yield
    app.dependency_overrides.pop(get_current_user, None)
    client.cookies.clear()


def _run(coro):
    return asyncio.run(coro)


def _result(account: str = SUB, **claims) -> AuthResult:
    """What the provider says: ``account`` is the userinfo sub, ``claims``
    the ID token's (``sub`` there is the ID token's own)."""
    return AuthResult(success=True, sub=account, email=f"{account}@test.com", name=account,
                      role="member", auth_provider="oidc:mock-sso", id_claims=claims)


def _start(return_to: str = "/apps/app-1?share=1&tab=link", mobile: bool = False):
    q = f"return_to={quote(return_to, safe='')}" + ("&mobile=true" if mobile else "")
    return client.get(f"/auth/confirm/oidc-url?{q}")


def _state_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


# ── the method ─────────────────────────────────────────────────────────────


def test_confirm_human_names_the_provider_for_its_accounts():
    with pytest.raises(HTTPException) as e:
        _run(confirm.confirm_human(_ctx()))
    assert e.value.status_code == 428
    assert e.value.detail["method"] == "oidc"
    assert e.value.detail["provider"] == "Mock SSO"
    # A local account without a password or a passkey still has no method.
    with pytest.raises(HTTPException) as e:
        _run(confirm.confirm_human(_ctx(LOCAL)))
    assert e.value.detail["method"] == "none"


def test_confirm_human_needs_the_provider_enabled(monkeypatch):
    monkeypatch.setattr(config, "OIDC_ENABLED", False)
    with pytest.raises(HTTPException) as e:
        _run(confirm.confirm_human(_ctx()))
    assert e.value.detail["method"] == "none"


# ── the start route ────────────────────────────────────────────────────────


def test_start_is_the_login_request_with_a_confirm_state_and_binds_it():
    r = _start()
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    q = parse_qs(urlparse(url).query)
    # The same request a sign-in makes: a signed-in browser is answered at
    # once, a provider's default flows are never asked to re-login.
    assert "prompt" not in q and "max_age" not in q
    assert q["redirect_uri"] == ["https://dash.example.com/auth/callback"]
    meta = _oauth_states[q["state"][0]]
    assert meta["purpose"] == "confirm" and meta["sub"] == SUB
    assert meta["return_to"] == "/apps/app-1?share=1&tab=link"
    assert abs(meta["created_at"] - time.time()) < 5
    # The web flow binds the state to this browser like a login does.
    assert any("oidc" in c.lower() for c in client.cookies.keys())


def test_start_asks_for_a_new_login_only_when_configured(monkeypatch):
    monkeypatch.setattr(config, "OIDC_CONFIRM_FRESH_LOGIN", True)
    q = parse_qs(urlparse(_start().json()["url"]).query)
    assert q["prompt"] == ["login"] and q["max_age"] == ["0"]


def test_start_on_the_native_app_uses_the_custom_scheme_and_binds_the_webview():
    # The app's WebView fetches the URL itself: the binding cookie lands in
    # its jar like a browser's.
    r = _start(mobile=True)
    assert r.status_code == 200, r.text
    q = parse_qs(urlparse(r.json()["url"]).query)
    assert q["redirect_uri"] == ["otodock://auth/callback"]
    assert any("oidc" in c.lower() for c in client.cookies.keys())


# ── login states bound to the browser, the app's too ───────


def _login_state(c: TestClient, mobile: bool) -> str:
    app.dependency_overrides.pop(get_current_user, None)
    r = c.get("/auth/oidc-url", params={"mobile": "true" if mobile else "false"})
    assert r.status_code == 200, r.text
    return parse_qs(urlparse(r.json()["url"]).query)["state"][0]


@pytest.mark.parametrize("mobile", [True, False])
def test_a_login_state_needs_the_browser_that_started_it(monkeypatch, mobile):
    monkeypatch.setattr(identity._oidc_provider, "authenticate",
                        AsyncMock(return_value=_result(SUB)))
    starter = TestClient(app)
    state = _login_state(starter, mobile)
    other = TestClient(app)
    r = other.post("/auth/callback", json={"code": "c", "state": state})
    assert r.status_code == 400 and "does not match this browser" in r.json()["detail"]
    assert "session" not in other.cookies


def test_a_mobile_login_state_completes_in_the_webview_that_started_it(monkeypatch):
    monkeypatch.setattr(identity._oidc_provider, "authenticate",
                        AsyncMock(return_value=_result(SUB)))
    webview = TestClient(app)
    state = _login_state(webview, True)
    r = webview.post("/auth/callback", json={"code": "c", "state": state})
    assert r.status_code == 200, r.text
    assert "session" in r.cookies


def test_the_bypass_login_binds_its_state(monkeypatch):
    monkeypatch.setattr(config, "AUTH_PROVIDER_BYPASS", True)
    app.dependency_overrides.pop(get_current_user, None)
    c = TestClient(app)
    r = c.get("/auth/login")
    assert r.status_code == 200 and "url" in r.json()
    assert any("oidc" in k.lower() for k in c.cookies.keys())


def test_start_refuses_the_wrong_callers_and_paths(monkeypatch):
    _as(_ctx(is_api_key=True))
    assert _start().status_code == 403
    _as(_ctx(LOCAL))
    assert _start().status_code == 400
    _as(_ctx())
    for bad in ("//evil.example", "https://evil.example/x", "/a\\b", "/x?next=%2F%2Fevil",
                "/x%5Cy", "relative", "/" + "a" * 600):
        assert _start(bad).status_code == 400, bad
    monkeypatch.setattr(config, "OIDC_ENABLED", False)
    assert _start().status_code == 503


def test_start_sits_on_the_confirm_bucket():
    codes = [_start().status_code for _ in range(12)]
    assert 200 in codes and 429 in codes


# ── the callback's confirm branch ──────────────────────────────────────────


def test_callback_mints_a_one_shot_token_and_no_session(monkeypatch):
    started = _start()
    state = _state_of(started.json()["url"])
    monkeypatch.setattr(identity._oidc_provider, "authenticate", AsyncMock(
        return_value=_result(aud="test-client", sub=SUB, auth_time=time.time())))
    r = client.post("/auth/callback", json={"code": "c", "state": state})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["purpose"] == "confirm"
    assert body["return_to"] == "/apps/app-1?share=1&tab=link"
    assert "session" not in {c.lower() for c in r.cookies.keys()}
    assert "user" not in body
    token = body["confirm_token"]
    assert confirm.consume_confirm_token(token, SUB) is True
    assert confirm.consume_confirm_token(token, SUB) is False
    # The state was consumed with it.
    assert client.post("/auth/callback", json={"code": "c", "state": state}).status_code == 400


def test_callback_refuses_another_account_and_the_wrong_audience(monkeypatch):
    cases = [
        (_result(account="someone-else", aud="test-client", sub="someone-else",
                 auth_time=time.time()), "different account"),
        (_result(aud="other-client", sub=SUB, auth_time=time.time()), "not for this platform"),
        (_result(aud="test-client", sub="someone-else", auth_time=time.time()), "different account"),
    ]
    for result, words in cases:
        state = _state_of(_start().json()["url"])
        monkeypatch.setattr(identity._oidc_provider, "authenticate", AsyncMock(return_value=result))
        r = client.post("/auth/callback", json={"code": "c", "state": state})
        assert r.status_code == 403, (words, r.text)
        assert words in r.json()["detail"]


def test_callback_takes_an_old_login_unless_a_new_one_is_required(monkeypatch):
    # The provider let a signed-in browser through with the login of an hour
    # ago: the same account, so the confirm holds.
    old = _result(aud="test-client", sub=SUB, auth_time=time.time() - 3600)
    state = _state_of(_start().json()["url"])
    monkeypatch.setattr(identity._oidc_provider, "authenticate", AsyncMock(return_value=old))
    assert client.post("/auth/callback", json={"code": "c", "state": state}).status_code == 200
    # The strict form refuses it and says how old it was.
    monkeypatch.setattr(config, "OIDC_CONFIRM_FRESH_LOGIN", True)
    state = _state_of(_start().json()["url"])
    r = client.post("/auth/callback", json={"code": "c", "state": state})
    assert r.status_code == 403, r.text
    assert "not fresh" in r.json()["detail"] and "60 minute" in r.json()["detail"]


def test_callback_without_auth_time_passes_unless_required(monkeypatch):
    without = _result(aud="test-client", sub=SUB)
    monkeypatch.setattr(identity._oidc_provider, "authenticate", AsyncMock(return_value=without))
    state = _state_of(_start().json()["url"])
    assert client.post("/auth/callback", json={"code": "c", "state": state}).status_code == 200
    # Requiring the claim means nothing without the strict form.
    monkeypatch.setattr(config, "OIDC_CONFIRM_REQUIRE_AUTH_TIME", True)
    state = _state_of(_start().json()["url"])
    assert client.post("/auth/callback", json={"code": "c", "state": state}).status_code == 200
    monkeypatch.setattr(config, "OIDC_CONFIRM_FRESH_LOGIN", True)
    state = _state_of(_start().json()["url"])
    r = client.post("/auth/callback", json={"code": "c", "state": state})
    assert r.status_code == 403 and "when you logged in" in r.json()["detail"]


def test_callback_accepts_a_list_audience_and_no_id_token(monkeypatch):
    state = _state_of(_start().json()["url"])
    monkeypatch.setattr(identity._oidc_provider, "authenticate", AsyncMock(
        return_value=_result(aud=["test-client", "other"], sub=SUB, auth_time=time.time())))
    assert client.post("/auth/callback", json={"code": "c", "state": state}).status_code == 200
    # A provider that returns no ID token at all: the userinfo sub is the check.
    state = _state_of(_start().json()["url"])
    monkeypatch.setattr(identity._oidc_provider, "authenticate", AsyncMock(return_value=_result()))
    assert client.post("/auth/callback", json={"code": "c", "state": state}).status_code == 200


def test_callback_confirm_state_needs_the_browser_binding(monkeypatch):
    state = _state_of(_start().json()["url"])
    client.cookies.clear()
    monkeypatch.setattr(identity._oidc_provider, "authenticate", AsyncMock(
        return_value=_result(aud="test-client", sub=SUB, auth_time=time.time())))
    r = client.post("/auth/callback", json={"code": "c", "state": state})
    assert r.status_code == 400 and "does not match this browser" in r.json()["detail"]


def test_id_token_claims_are_read_defensively():
    from auth.providers.oidc_provider import _id_token_claims
    import base64
    import json
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "x", "auth_time": 5}).encode()).decode().rstrip("=")
    assert _id_token_claims(f"h.{payload}.s") == {"sub": "x", "auth_time": 5}
    assert _id_token_claims("not.a.jwt.at.all") == {}
    assert _id_token_claims("h.!!!.s") == {}
    assert _id_token_claims(None) == {}
    arr = base64.urlsafe_b64encode(b"[1]").decode().rstrip("=")
    assert _id_token_claims(f"h.{arr}.s") == {}

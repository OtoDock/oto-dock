"""OIDC lazy endpoint re-discovery tests.

Regression coverage for the boot-race brick: config.py fetches the
.well-known/openid-configuration document once at import time, and when the
IdP is unreachable at that moment (proxy and a co-hosted IdP cold-starting
together after a power cut) the endpoint URLs stayed empty for the process
lifetime — every SSO login returned 503 "OIDC not configured" until a manual
proxy restart. ensure_oidc_discovery() now re-attempts discovery at request
time, rate-limited, with explicit env vars always winning.

HTTP is mocked via httpx.AsyncClient (patched inside oidc_provider).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

import config
from app import app
from auth.providers import oidc_provider
from auth.providers.oidc_provider import OIDCAuthProvider, ensure_oidc_discovery

client = TestClient(app)

_DISCOVERY_DOC = {
    "authorization_endpoint": "https://idp.example.com/authorize",
    "token_endpoint": "https://idp.example.com/token",
    "userinfo_endpoint": "https://idp.example.com/userinfo",
    "end_session_endpoint": "https://idp.example.com/logout",
}


def _mock_get(json_payload: dict, status: int = 200):
    mock_response = MagicMock()
    mock_response.status_code = status
    mock_response.json = MagicMock(return_value=json_payload)
    mock_response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)
    return mock_client


def _mock_get_failing():
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    return mock_client


@pytest.fixture(autouse=True)
def _oidc_baseline(monkeypatch):
    """Empty-URL OIDC config with discovery enabled — the post-boot-failure
    state. monkeypatch restores the config attrs apply_oidc_discovery rebinds
    mid-test; the shared retry guard is reset on both sides of each test, and
    no test inherits the sign-in states another left live."""
    from auth import providers
    providers._oauth_states.clear()
    providers._client_states.clear()
    oidc_provider._discovery_guard["at"] = 0.0
    for attr in ("OIDC_AUTHORIZE_URL", "OIDC_TOKEN_URL",
                 "OIDC_USERINFO_URL", "OIDC_LOGOUT_URL"):
        monkeypatch.setattr(config, attr, "")
    monkeypatch.setattr(config, "OIDC_ENABLED", True)
    monkeypatch.setattr(config, "OIDC_CLIENT_ID", "test-client")
    monkeypatch.setattr(config, "OIDC_REDIRECT_URI", "https://dash.example.com/auth/callback")
    monkeypatch.setattr(
        config, "OIDC_DISCOVERY_URL",
        "https://idp.example.com/.well-known/openid-configuration",
    )
    yield
    oidc_provider._discovery_guard["at"] = 0.0


def test_login_url_none_without_discovery(monkeypatch):
    monkeypatch.setattr(config, "OIDC_DISCOVERY_URL", "")
    assert OIDCAuthProvider().get_login_url() is None


@pytest.mark.asyncio
async def test_ensure_noop_without_discovery_url(monkeypatch):
    monkeypatch.setattr(config, "OIDC_DISCOVERY_URL", "")
    mc = _mock_get(_DISCOVERY_DOC)
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        await ensure_oidc_discovery()
    assert mc.get.await_count == 0


@pytest.mark.asyncio
async def test_lazy_discovery_success():
    mc = _mock_get(_DISCOVERY_DOC)
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        await ensure_oidc_discovery()
    assert config.OIDC_AUTHORIZE_URL == "https://idp.example.com/authorize"
    assert config.OIDC_TOKEN_URL == "https://idp.example.com/token"
    assert config.OIDC_USERINFO_URL == "https://idp.example.com/userinfo"
    assert config.OIDC_LOGOUT_URL == "https://idp.example.com/logout"
    url = OIDCAuthProvider().get_login_url()
    assert url is not None
    assert url.startswith("https://idp.example.com/authorize?")
    assert "client_id=test-client" in url


@pytest.mark.asyncio
async def test_lazy_discovery_recovered_once(caplog):
    """Once recovered, further ensures are no-ops — no second fetch."""
    mc = _mock_get(_DISCOVERY_DOC)
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        await ensure_oidc_discovery()
        await ensure_oidc_discovery()
    assert mc.get.await_count == 1


@pytest.mark.asyncio
async def test_failure_rate_limited_then_retries(caplog):
    mc = _mock_get_failing()
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        with caplog.at_level("WARNING", logger="claude-proxy"):
            await ensure_oidc_discovery()
            await ensure_oidc_discovery()  # inside the retry window — skipped
        assert mc.get.await_count == 1
        failures = [r for r in caplog.records if "discovery retry failed" in r.message]
        assert len(failures) == 1

        # Past the retry window the next login click attempts again.
        oidc_provider._discovery_guard["at"] -= oidc_provider._DISCOVERY_RETRY_INTERVAL_S + 1
        await ensure_oidc_discovery()
        assert mc.get.await_count == 2


@pytest.mark.asyncio
async def test_explicit_env_var_wins(monkeypatch):
    monkeypatch.setattr(config, "OIDC_TOKEN_URL", "https://custom.example.com/token")
    mc = _mock_get(_DISCOVERY_DOC)
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        await ensure_oidc_discovery()
    assert config.OIDC_TOKEN_URL == "https://custom.example.com/token"
    assert config.OIDC_AUTHORIZE_URL == "https://idp.example.com/authorize"


@pytest.mark.asyncio
async def test_concurrent_ensures_single_fetch():
    mc = _mock_get(_DISCOVERY_DOC)
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        await asyncio.gather(ensure_oidc_discovery(), ensure_oidc_discovery())
    assert mc.get.await_count == 1
    assert config.OIDC_AUTHORIZE_URL == "https://idp.example.com/authorize"


def test_oidc_url_503_while_idp_down():
    mc = _mock_get_failing()
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        resp = client.get("/auth/oidc-url")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "OIDC not configured"


def test_oidc_url_recovers_without_restart():
    mc = _mock_get(_DISCOVERY_DOC)
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        resp = client.get("/auth/oidc-url")
    assert resp.status_code == 200
    assert resp.json()["url"].startswith("https://idp.example.com/authorize?")


def test_sso_starts_are_bounded_per_address(monkeypatch):
    from auth import rate_limiter
    monkeypatch.setitem(config.RATE_LIMIT_RULES, "sso_start",
                        {"max": 2, "window": 60, "base_block": 60, "max_block": 600})
    rate_limiter._attempts.clear()
    mc = _mock_get(_DISCOVERY_DOC)
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        # Each from a client without the state cookie: every start counts.
        codes = [TestClient(app).get("/auth/oidc-url").status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and codes[-1] == 429
    rate_limiter._attempts.clear()


def test_a_full_state_store_refuses_a_start_and_keeps_every_live_state(monkeypatch):
    """At the bound a new start is refused with the wait until the oldest
    state lapses; no sign-in in flight loses its state. A lapsed state
    makes room again."""
    import time as _t

    from auth import providers, rate_limiter
    monkeypatch.setattr(providers, "_STATE_MAX", 3)
    monkeypatch.setattr(providers, "_oauth_states", {})
    rate_limiter._attempts.clear()
    mc = _mock_get(_DISCOVERY_DOC)
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        assert [client.get("/auth/oidc-url").status_code for _ in range(3)] == [200] * 3
        live = list(providers._oauth_states)
        refused = client.get("/auth/oidc-url")
        assert refused.status_code == 503
        assert 1 <= int(refused.headers["Retry-After"]) <= providers._STATE_TTL
        assert list(providers._oauth_states) == live
        providers._oauth_states[live[0]]["expiry"] = _t.monotonic() - 1
        assert client.get("/auth/oidc-url").status_code == 200
        assert live[1] in providers._oauth_states and live[2] in providers._oauth_states
    rate_limiter._attempts.clear()


# --- the SSO start count: a browser already signing in is not counted again --

from urllib.parse import parse_qs as _parse_qs, urlparse as _urlparse  # noqa: E402

from api.auth import identity as _identity  # noqa: E402

_PATHS = ["/auth/oidc-url", "/auth/login"]


@pytest.fixture
def sso(monkeypatch):
    """Starts against a discovered provider, an empty state store and no
    counted start; ``/auth/login`` in bypass mode."""
    from auth import lan_check, providers, rate_limiter
    monkeypatch.setattr(providers, "_oauth_states", {})
    monkeypatch.setattr(providers, "_client_states", {})
    monkeypatch.setattr(config, "AUTH_PROVIDER_BYPASS", True)
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", False)
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [])
    lan_check.reset_state()
    rate_limiter._attempts.clear()
    with patch("auth.providers.oidc_provider.httpx.AsyncClient",
               return_value=_mock_get(_DISCOVERY_DOC)):
        yield
    rate_limiter._attempts.clear()


def _starts(c: TestClient, n: int, path: str = "/auth/oidc-url") -> list[str]:
    states = []
    for _ in range(n):
        r = c.get(path)
        assert r.status_code == 200, r.text
        states.append(_parse_qs(_urlparse(r.json()["url"]).query)["state"][0])
    return states


def _counted() -> int:
    from auth import rate_limiter
    entry = rate_limiter._attempts.get(("sso_start", "testclient"))
    return entry["count"] if entry else 0


def _ring(*states: str) -> TestClient:
    return TestClient(app, cookies={_identity._oidc_state_cookie_name(): ".".join(states)})


@pytest.mark.parametrize("path", _PATHS)
def test_a_reload_storm_from_one_browser_counts_once_until_the_cap(sso, path):
    """Each start mints its own state; while the browser's ring holds a live
    sign-in state and fewer than the ring's cap, a start is not counted."""
    c = TestClient(app)
    states = _starts(c, 4, path)
    assert len(set(states)) == 4 and _counted() == 1
    _starts(c, 1, path)                 # the ring holds the cap of live states
    assert _counted() == 2


def test_two_tabs_each_get_their_own_state_and_both_callbacks_succeed(sso, monkeypatch):
    from auth.providers.base import AuthResult
    result = AuthResult(success=True, sub="oidc-two-tabs", email="tabs@t.com", name="Tabs",
                        role="member", auth_provider="oidc:test", id_claims={},
                        email_verified=True)
    monkeypatch.setattr(_identity._oidc_provider, "authenticate", AsyncMock(return_value=result))
    c = TestClient(app)
    first, second = _starts(c, 2)
    assert first != second and _counted() == 1
    for state in (first, second):
        r = c.post("/auth/callback", json={"code": "c", "state": state})
        assert r.status_code == 200, r.text


@pytest.mark.parametrize("path", _PATHS)
def test_a_client_without_the_cookie_counts_every_start(sso, path):
    for _ in range(3):
        _starts(TestClient(app), 1, path)
    assert _counted() == 3


def test_a_ring_of_lapsed_states_counts(sso):
    import time as _t

    from auth import providers
    c = TestClient(app)
    (state,) = _starts(c, 1)
    providers._oauth_states[state]["expiry"] = _t.monotonic() - 1
    _starts(c, 1)
    assert _counted() == 2


def test_a_ring_of_a_confirm_state_counts(sso):
    from auth import providers
    _starts(_ring(providers.create_oauth_state(purpose="confirm", sub="someone")), 1)
    assert _counted() == 1


def _from(ip: str) -> TestClient:
    """A client at ``ip`` that keeps no state cookie: every start counts."""
    return TestClient(app, client=(ip, 40000))


def _start_code(ip: str) -> int:
    return _from(ip).get("/auth/oidc-url").status_code


def _roomy_bucket(monkeypatch, n: int = 100) -> None:
    monkeypatch.setitem(config.RATE_LIMIT_RULES, "sso_start",
                        {"max": n, "window": 300, "base_block": 300, "max_block": 3600})


def test_one_client_stops_at_eight_live_states_while_another_still_starts(sso, monkeypatch):
    _roomy_bucket(monkeypatch)
    assert [_start_code("203.0.113.5") for _ in range(8)] == [200] * 8
    refused = _from("203.0.113.5").get("/auth/oidc-url")
    assert refused.status_code == 429
    assert 1 <= int(refused.headers["Retry-After"]) <= 300
    assert _start_code("203.0.113.6") == 200


def test_two_addresses_of_one_slash_64_share_the_cap(sso, monkeypatch):
    _roomy_bucket(monkeypatch)
    codes = [_start_code(f"2001:db8:1:2::{i + 10:x}") for i in range(8)]
    assert codes == [200] * 8
    assert _start_code("2001:db8:1:2::ff") == 429
    assert _start_code("2001:db8:1:3::10") == 200


def test_two_addresses_of_one_slash_64_share_the_start_bucket(sso, monkeypatch):
    _roomy_bucket(monkeypatch, 3)
    codes = [_start_code(f"2001:db8:5:6::{i + 1:x}") for i in range(4)]
    assert codes[:3] == [200] * 3 and codes[3] == 429
    assert _start_code("2001:db8:5:7::1") == 200


def test_an_address_every_client_shares_meets_no_state_cap(sso, monkeypatch):
    """A trusted proxy that sends no X-Forwarded-For is everyone behind it:
    the start bucket and the store's global bound apply, not the cap."""
    _roomy_bucket(monkeypatch)
    monkeypatch.setattr(config, "TRUSTED_PROXIES", ["203.0.113.9"])
    assert [_start_code("203.0.113.9") for _ in range(10)] == [200] * 10


def test_a_lapsed_state_frees_a_clients_slot(sso, monkeypatch):
    import time as _t

    from auth import providers
    _roomy_bucket(monkeypatch)
    assert [_start_code("203.0.113.7") for _ in range(8)] == [200] * 8
    oldest = next(iter(providers._oauth_states))
    providers._oauth_states[oldest]["expiry"] = _t.monotonic() - 1
    assert _start_code("203.0.113.7") == 200


def test_a_browser_storm_stays_under_the_clients_cap_and_counts_as_decided(sso):
    """Its ring holds at most four live states: a state pushed out of the
    ring can no longer complete in that browser and is dropped."""
    from auth import providers
    browser = TestClient(app, client=("203.0.113.8", 40000))
    _starts(browser, 12)
    assert _counted_for("203.0.113.8") == 9
    assert len(providers._oauth_states) == 4


def _counted_for(key: str) -> int:
    from auth import rate_limiter
    entry = rate_limiter._attempts.get(("sso_start", key))
    return entry["count"] if entry else 0


@pytest.mark.parametrize("resend", ["first", "newest"])
def test_a_ring_a_client_builds_gets_no_more_free_starts_than_a_browser(sso, resend):
    """A client that sends back one live state (the first again and again,
    or only the newest) is counted once per four starts at most, as a
    browser's ring is."""
    sent = _starts(TestClient(app), 1)
    for _ in range(7):
        sent += _starts(_ring(sent[0] if resend == "first" else sent[-1]), 1)
    assert _counted() == (5 if resend == "first" else 2)


# --- F66: the ID token is verified before anything it says is trusted ---------

import json as _json  # noqa: E402
import time as _time  # noqa: E402

import jwt as _jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa as _rsa  # noqa: E402

_ISS = "https://idp.example.com/application/o/app/"
_KEY = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = _rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key, kid):
    jwk = _json.loads(_jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**jwk, "kid": kid, "alg": "RS256", "use": "sig"}


def _id_token(*, key=_KEY, kid="k1", alg="RS256", **over):
    now = int(_time.time())
    claims = {"iss": _ISS, "aud": "test-client", "sub": "user-1", "iat": now,
              "exp": now + 300, "nonce": "n-1", "email_verified": True}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return _jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


class _Idp:
    """A provider over an httpx MockTransport: discovery, JWKS, token and
    userinfo; ``seen`` keeps what the token endpoint received."""

    def __init__(self, *, id_token=None, userinfo=None, jwks_keys=None):
        self.id_token = id_token if id_token is not None else _id_token()
        self.userinfo = userinfo or {"sub": "user-1", "email": "u@x.com",
                                     "groups": ["members"], "email_verified": True}
        self.jwks_keys = jwks_keys or [_jwk(_KEY, "k1")]
        self.seen: dict = {}
        self.jwks_fetches = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": _ISS, "jwks_uri": "https://idp.example.com/jwks",
                "authorization_endpoint": "https://idp.example.com/authorize",
                "token_endpoint": "https://idp.example.com/token",
                "userinfo_endpoint": "https://idp.example.com/userinfo",
                "id_token_signing_alg_values_supported": ["RS256"]})
        if path == "/jwks":
            self.jwks_fetches += 1
            return httpx.Response(200, json={"keys": self.jwks_keys})
        if path == "/token":
            from urllib.parse import parse_qs
            self.seen = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            body = {"access_token": "at"}
            if self.id_token:
                body["id_token"] = self.id_token
            return httpx.Response(200, json=body)
        if path == "/userinfo":
            return httpx.Response(200, json=self.userinfo)
        return httpx.Response(404)


@pytest.fixture
def idp(monkeypatch):
    """An explicit-URL install (no discovery URL): the issuer and JWKS come
    from the token's own issuer on the token endpoint's origin."""
    monkeypatch.setattr(config, "OIDC_DISCOVERY_URL", "")
    monkeypatch.setattr(config, "OIDC_AUTHORIZE_URL", "https://idp.example.com/authorize")
    monkeypatch.setattr(config, "OIDC_TOKEN_URL", "https://idp.example.com/token")
    monkeypatch.setattr(config, "OIDC_USERINFO_URL", "https://idp.example.com/userinfo")
    monkeypatch.setattr(config, "OIDC_JWKS_URL", "")
    monkeypatch.setattr(config, "OIDC_ISSUER", "")
    monkeypatch.setattr(config, "OIDC_ID_TOKEN_ALGS", [])
    monkeypatch.setattr(config, "OIDC_CLIENT_SECRET", "s" * 40)
    monkeypatch.setattr(config, "OIDC_ROLE_GROUPS", {"members": "member"})
    monkeypatch.setattr(oidc_provider, "_jwks", {"url": "", "keys": [], "at": 0.0})
    monkeypatch.setattr(oidc_provider, "_derived", {"iss": "", "jwks": ""})
    fake = _Idp()
    real = httpx.AsyncClient
    monkeypatch.setattr(oidc_provider.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(fake.handler)))
    return fake


def _auth(nonce="n-1", verifier="v-1"):
    return asyncio.run(OIDCAuthProvider().authenticate(
        {"code": "c", "redirect_uri": None, "nonce": nonce, "code_verifier": verifier}))


def test_a_clean_sign_in_verifies_and_sends_the_pkce_verifier(idp):
    r = _auth()
    assert r.success, r.error
    assert r.sub == "user-1" and r.email_verified is True and r.id_claims["nonce"] == "n-1"
    assert idp.seen["code_verifier"] == "v-1"


@pytest.mark.parametrize("override, code", [
    ({"nonce": "other"}, "bad_nonce"),
    ({"aud": "someone-else"}, "wrong_audience"),
    ({"exp": int(_time.time()) - 3600}, "expired"),
    ({"key": _OTHER_KEY}, "bad_signature"),
    ({"kid": "unknown"}, "bad_signature"),
])
def test_each_check_refuses_with_its_reason(idp, override, code):
    idp.id_token = _id_token(**override)
    r = _auth()
    assert not r.success and r.error_code == code and r.error


def test_no_id_token_is_refused(idp):
    idp.id_token = ""
    r = _auth()
    assert not r.success and r.error_code == "no_id_token"


def test_an_unsigned_token_is_refused(idp):
    idp.id_token = _jwt.encode({"iss": _ISS, "aud": "test-client", "sub": "user-1",
                                "nonce": "n-1"}, None, algorithm="none")
    r = _auth()
    assert not r.success and r.error_code in ("bad_signature", "bad_id_token")


def test_userinfo_for_another_account_is_refused(idp):
    idp.userinfo = {"sub": "user-2", "email": "u@x.com", "groups": ["members"]}
    r = _auth()
    assert not r.success and r.error_code == "sub_mismatch"


def test_a_rotated_key_is_fetched_once(idp):
    assert _auth().success
    idp.jwks_keys = [_jwk(_OTHER_KEY, "k2")]
    idp.id_token = _id_token(key=_OTHER_KEY, kid="k2")
    oidc_provider._jwks["at"] -= oidc_provider._JWKS_REFETCH_S + 1
    assert _auth().success
    assert idp.jwks_fetches == 2


def test_an_issuer_off_the_token_endpoint_is_refused(idp):
    idp.id_token = _id_token(iss="https://elsewhere.example.com/")
    r = _auth()
    assert not r.success and r.error_code == "issuer_unknown"


def test_a_configured_issuer_on_another_origin_finds_its_keys(idp, monkeypatch):
    """A provider whose issuer is not the token endpoint's origin (Google's
    accounts host beside its API host): OIDC_ISSUER names it, and its own
    discovery document gives the JWKS."""
    other = "https://accounts.example.org"
    monkeypatch.setattr(config, "OIDC_ISSUER", other)
    real = idp.handler

    def handler(request):
        if request.url.host == "accounts.example.org":
            return httpx.Response(200, json={"issuer": other,
                                             "jwks_uri": "https://idp.example.com/jwks"})
        return real(request)
    idp.handler = handler
    idp.id_token = _id_token(iss=other)
    r = _auth()
    assert r.success, r.error


def test_hs256_is_verified_with_the_client_secret(idp):
    idp.id_token = _id_token(key="s" * 40, alg="HS256")
    assert _auth().success
    idp.id_token = _id_token(key="n" * 40, alg="HS256")
    assert _auth().error_code == "bad_signature"


def test_an_id_token_with_base64url_padding_signs_in(idp):
    # Some providers keep the base64url "=" padding on their segments and
    # sign the padded form. PyJWT 2.14.0 and 2.15.0 refused such a token; 2.15.1,
    # the floor in requirements.in, accepts it again.
    import base64
    def seg(obj):
        return base64.urlsafe_b64encode(_json.dumps(obj).encode()).decode()
    now = int(_time.time())
    signing_input = seg({"alg": "RS256", "kid": "k1", "typ": "JWT"}) + "." + seg(
        {"iss": _ISS, "aud": "test-client", "sub": "user-1", "iat": now,
         "exp": now + 300, "nonce": "n-1", "email_verified": True})
    rsa = _jwt.algorithms.RSAAlgorithm(_jwt.algorithms.RSAAlgorithm.SHA256)
    sig = base64.urlsafe_b64encode(rsa.sign(signing_input.encode(), _KEY)).decode()
    idp.id_token = signing_input + "." + sig
    assert idp.id_token.count("=") >= 2
    r = _auth()
    assert r.success, r.error


def test_an_omitted_email_verified_is_not_vouched_for(idp):
    idp.id_token = _id_token(email_verified=None)
    idp.userinfo = {"sub": "user-1", "email": "u@x.com", "groups": ["members"]}
    r = _auth()
    assert r.success and r.email_verified is False


def test_the_login_url_carries_a_nonce_and_an_s256_challenge(idp):
    from urllib.parse import parse_qs, urlparse
    from auth import providers
    url = OIDCAuthProvider().get_login_url()
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    meta = providers._oauth_states[q["state"]]
    assert q["nonce"] == meta["nonce"] and q["code_challenge_method"] == "S256"
    assert q["code_challenge"] == oidc_provider._pkce_challenge(meta["code_verifier"])
    assert "openid" in q["scope"].split()


def test_a_refused_code_exchange_says_what_the_provider_answered(idp, monkeypatch):
    original = idp.handler

    def handler(request):
        if request.url.path == "/token":
            return httpx.Response(400, json={"error": "invalid_grant",
                                             "error_description": "PKCE verification failed"})
        return original(request)

    monkeypatch.setattr(idp, "handler", handler)
    r = _auth()
    assert not r.success and "PKCE verification failed" in r.error

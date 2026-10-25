"""The GitHub App half of the ``github`` provider (auth/oauth_providers/
github_app.py): the PEM normalization, the app JWT and its cache, the
installation token mint with its pinned request body, the listings with the
owner standing, the app verification at save; the admin kinds leaf
(services/oauth/oauth_app_kinds.py); and the generic provider's token
endpoint asking for JSON and reading a form-encoded answer.

Every HTTP call goes to a fake GitHub (``httpx.MockTransport``); the RSA key
is generated here (a throwaway, never a real one).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from auth.oauth_providers import github_app
from auth.oauth_providers.base import OAuthTokenError
from auth.oauth_providers.generic import GenericOAuthProvider
from storage.identity import oauth_app_kinds as kinds

FIELDS = [
    {"key": "GITHUB_APP_ID", "label": "App ID", "input_type": "text"},
    {"key": "GITHUB_APP_CLIENT_ID", "label": "Client ID", "input_type": "text"},
    {"key": "GITHUB_APP_CLIENT_SECRET", "label": "Client secret", "input_type": "password"},
    {"key": "GITHUB_APP_PRIVATE_KEY", "label": "Private key (PEM)", "input_type": "multiline_secret"},
    {"key": "GITHUB_APP_WEBHOOK_SECRET", "label": "Webhook secret", "input_type": "password"},
]
OAUTH_FIELDS = [
    {"key": "GITHUB_CLIENT_ID", "label": "OAuth App Client ID", "input_type": "text"},
    {"key": "GITHUB_CLIENT_SECRET", "label": "OAuth App Client Secret", "input_type": "password"},
]
OAUTH_BLOCK = {
    "provider_id": "github",
    "app_credential": "github-oauth-app",
    "app_credential_fields": OAUTH_FIELDS,
    "app_kinds": {
        "key": "GITHUB_APP_KIND",
        "kinds": [
            {"kind": kinds.KIND_OAUTH_APP, "label": "OAuth App", "fields": OAUTH_FIELDS},
            {"kind": kinds.KIND_GITHUB_APP, "label": "GitHub App", "mcp_token": kinds.MCP_TOKEN_USER,
             "fields": FIELDS},
        ],
    },
}


@pytest.fixture(scope="module")
def key_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pkcs8 = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode()
    pkcs1 = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return pkcs8, pkcs1, public


@pytest.fixture
def app(key_pair):
    github_app.forget_jwts()
    return github_app.GitHubApp(
        app_id="4242", client_id="Iv1.cafe", client_secret="s", private_key_pem=key_pair[0],
        webhook_secret="w", slug="otodock-dev", name="OtoDock dev",
    )


class Fake:
    """A fake GitHub: ``(method, path)`` → an answer, a request log."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], object] = {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        route = self.routes.get(key)
        if route is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if callable(route):
            return route(request)
        status, body = route
        return httpx.Response(status, json=body)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), headers=github_app._API_HEADERS)


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(github_app, "_client", f.client)
    return f


# ---------------------------------------------------------------------------
# the PEM and the JWT
# ---------------------------------------------------------------------------

class TestPem:
    def test_a_pkcs8_and_a_pkcs1_key_pass(self, key_pair):
        pkcs8, pkcs1, _ = key_pair
        assert github_app.normalize_pem(pkcs8) == pkcs8
        assert github_app.normalize_pem(pkcs1) == pkcs1

    def test_a_single_line_paste_is_rewrapped(self, key_pair):
        pkcs8 = key_pair[0]
        one_line = " ".join(pkcs8.split("\n"))
        assert "\n" not in one_line.strip()
        assert github_app.normalize_pem(one_line) == pkcs8

    @pytest.mark.parametrize("text", ["", "ghp_not_a_key", "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"])
    def test_anything_else_is_refused(self, text):
        with pytest.raises(ValueError, match="not an RSA private key"):
            github_app.normalize_pem(text)


class TestJwt:
    def test_the_claims_and_the_signature(self, app, key_pair):
        token = github_app.app_jwt(app)
        claims = jwt.decode(token, key_pair[2], algorithms=["RS256"])
        assert claims["iss"] == "Iv1.cafe"
        assert claims["exp"] - claims["iat"] == 600
        assert claims["iat"] <= int(datetime.now(timezone.utc).timestamp()) - 59

    def test_the_app_id_is_the_issuer_without_a_client_id(self, app, key_pair):
        app.client_id = ""
        claims = jwt.decode(github_app.app_jwt(app), key_pair[2], algorithms=["RS256"])
        assert claims["iss"] == "4242"

    def test_the_cache_is_keyed_on_the_issuer_and_the_key(self, app, key_pair):
        first = github_app.app_jwt(app)
        assert github_app.app_jwt(app) == first and len(github_app._jwt_cache) == 1
        app.client_id = "Iv1.other"
        assert github_app.app_jwt(app) != first and len(github_app._jwt_cache) == 2
        app.client_id = "Iv1.cafe"
        # The same key in PKCS#1 form is another fingerprint: a fresh mint (the
        # token itself may equal the first one, RS256 being deterministic).
        app.private_key_pem = key_pair[1]
        github_app.app_jwt(app)
        assert len(github_app._jwt_cache) == 3
        github_app.forget_jwts()
        assert github_app._jwt_cache == {}

    def test_a_bad_key_is_a_value_error(self, app):
        app.private_key_pem = "nope"
        with pytest.raises(ValueError):
            github_app.app_jwt(app)


# ---------------------------------------------------------------------------
# the bundle
# ---------------------------------------------------------------------------

class TestBundle:
    def test_the_kinds_keys_never_shadow_each_other(self, key_pair):
        creds = {
            "GITHUB_CLIENT_ID": "oauth-cid", "GITHUB_CLIENT_SECRET": "oauth-secret",
            "GITHUB_APP_ID": "4242", "GITHUB_APP_CLIENT_ID": "Iv1.cafe",
            "GITHUB_APP_CLIENT_SECRET": "app-secret", "GITHUB_APP_PRIVATE_KEY": key_pair[0],
            "GITHUB_APP_WEBHOOK_SECRET": "w", "GITHUB_APP_SLUG": "slug", "GITHUB_APP_NAME": "Name",
        }
        app = github_app.app_from_bundle(creds, FIELDS)
        assert (app.app_id, app.client_id, app.client_secret) == ("4242", "Iv1.cafe", "app-secret")
        assert app.webhook_secret == "w" and app.slug == "slug" and app.name == "Name"
        assert app.private_key_pem == key_pair[0]

    def test_the_kinds_leaf(self):
        assert kinds.active_kind(OAUTH_BLOCK, {}) == kinds.KIND_OAUTH_APP
        assert kinds.active_kind(OAUTH_BLOCK, {"GITHUB_APP_KIND": kinds.KIND_GITHUB_APP}) == kinds.KIND_GITHUB_APP
        assert kinds.active_kind(OAUTH_BLOCK, {"GITHUB_APP_KIND": "slack_app"}) == kinds.KIND_OAUTH_APP
        assert kinds.active_kind({"app_credential_fields": OAUTH_FIELDS}, {"GITHUB_APP_KIND": kinds.KIND_GITHUB_APP}) == kinds.KIND_OAUTH_APP
        assert kinds.kind_fields(OAUTH_BLOCK, kinds.KIND_GITHUB_APP) == FIELDS
        assert kinds.kind_fields({"app_credential_fields": OAUTH_FIELDS}, kinds.KIND_OAUTH_APP) == OAUTH_FIELDS
        assert kinds.kind_fields({"app_credential_fields": OAUTH_FIELDS}, kinds.KIND_GITHUB_APP) == []
        assert kinds.kind_key(OAUTH_BLOCK) == "GITHUB_APP_KIND" and kinds.kind_key({}) == ""
        assert kinds.mcp_token_of(OAUTH_BLOCK, kinds.KIND_GITHUB_APP) == kinds.MCP_TOKEN_USER
        assert kinds.mcp_token_of({}, kinds.KIND_GITHUB_APP) == kinds.MCP_TOKEN_USER
        assert kinds.APP_TOKEN_DIR == "_app"


# ---------------------------------------------------------------------------
# the installation token
# ---------------------------------------------------------------------------

def _expires_at(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class TestMint:
    def test_the_request_and_the_token_set(self, app, fake):
        answer = {
            "token": "ghs_abc", "expires_at": _expires_at(3600),
            "permissions": {"contents": "write"}, "repository_selection": "selected",
            "repositories": [{"full_name": "octo/a"}, {"full_name": "octo/b"}],
        }
        fake.routes[("POST", "/app/installations/77/access_tokens")] = (201, answer)
        ts = github_app.mint_installation_token(app, 77)
        req = fake.requests[-1]
        assert req.headers["authorization"] == f"Bearer {github_app.app_jwt(app)}"
        assert req.headers["accept"] == "application/vnd.github+json"
        assert req.headers["x-github-api-version"] == "2022-11-28"
        assert req.content == b""   # the whole installation, nothing narrowed
        assert ts.access_token == "ghs_abc" and ts.refresh_token == ""
        assert 3500 < ts.expires_in <= 3600
        assert ts.raw["flow"] == kinds.INSTALLATION_FLOW
        assert ts.raw["installation_id"] == 77 and ts.raw["app_id"] == "4242"
        assert ts.raw["expires_at"] == answer["expires_at"]
        assert ts.raw["repositories"] == ["octo/a", "octo/b"]
        assert ts.raw["repository_selection"] == "selected"

    def test_the_narrowing_is_asked_for_in_the_body(self, app, fake):
        fake.routes[("POST", "/app/installations/77/access_tokens")] = (201, {"token": "ghs_x", "expires_at": _expires_at(3600)})
        github_app.mint_installation_token(app, 77, repositories=["a", "b"], permissions={"contents": "read"})
        body = json.loads(fake.requests[-1].content)
        assert body == {"repositories": ["a", "b"], "permissions": {"contents": "read"}}

    @pytest.mark.parametrize("status, body, code", [
        (401, {"message": "A JSON web token could not be decoded"}, kinds.APP_KEY_REJECTED),
        (404, {"message": "Not Found"}, kinds.INSTALLATION_GONE),
        (403, {"message": "This installation has been suspended"}, kinds.INSTALLATION_SUSPENDED),
        (403, {"message": "API rate limit exceeded"}, "http_403"),
        (500, {"message": "boom"}, "http_500"),
    ])
    def test_the_refusals(self, app, fake, status, body, code):
        fake.routes[("POST", "/app/installations/77/access_tokens")] = (status, body)
        with pytest.raises(OAuthTokenError) as exc:
            github_app.mint_installation_token(app, 77)
        assert exc.value.code == code and exc.value.status == status

    def test_an_unreachable_github(self, app, monkeypatch):
        def boom(request):
            raise httpx.ConnectError("down")
        monkeypatch.setattr(github_app, "_client", lambda: httpx.Client(transport=httpx.MockTransport(boom)))
        with pytest.raises(OAuthTokenError) as exc:
            github_app.mint_installation_token(app, 77)
        assert exc.value.code == "unreachable"


class TestAsTheApp:
    def test_get_installation_answers_none_when_gone(self, app, fake):
        assert github_app.get_installation(app, 5) is None
        fake.routes[("GET", "/app/installations/5")] = (200, {"id": 5, "suspended_at": None})
        assert github_app.get_installation(app, 5) == {"id": 5, "suspended_at": None}

    def test_verify_fills_the_slug_and_normalizes_the_pem(self, app, fake, key_pair):
        fake.routes[("GET", "/app")] = (200, {"id": 4242, "slug": "otodock-dev", "name": "OtoDock dev"})
        creds = {"GITHUB_APP_ID": "4242", "GITHUB_APP_CLIENT_ID": "Iv1.cafe",
                 "GITHUB_APP_PRIVATE_KEY": " ".join(key_pair[0].split("\n"))}
        out = github_app.verify_app_credentials(creds, FIELDS)
        assert out["GITHUB_APP_PRIVATE_KEY"] == key_pair[0]
        assert out[github_app.APP_SLUG_KEY] == "otodock-dev" and out[github_app.APP_NAME_KEY] == "OtoDock dev"
        assert out["GITHUB_APP_CLIENT_ID"] == "Iv1.cafe"

    def test_verify_refuses_another_apps_key(self, app, fake, key_pair):
        fake.routes[("GET", "/app")] = (200, {"id": 9, "slug": "other"})
        with pytest.raises(ValueError, match="another app"):
            github_app.verify_app_credentials({"GITHUB_APP_ID": "4242", "GITHUB_APP_PRIVATE_KEY": key_pair[0]}, FIELDS)

    def test_verify_names_a_refused_key(self, app, fake, key_pair):
        fake.routes[("GET", "/app")] = (401, {"message": "bad"})
        with pytest.raises(ValueError, match="refused the app's key"):
            github_app.verify_app_credentials({"GITHUB_APP_ID": "4242", "GITHUB_APP_PRIVATE_KEY": key_pair[0]}, FIELDS)

    def test_verify_needs_the_id_and_the_key(self):
        with pytest.raises(ValueError, match="App ID"):
            github_app.verify_app_credentials({"GITHUB_APP_ID": "4242"}, FIELDS)


# ---------------------------------------------------------------------------
# the listings
# ---------------------------------------------------------------------------

def _inst(iid: int, *, app_id: int = 4242, target: str = "User", login: str = "alice", account_id: int = 1,
          selection: str = "all") -> dict:
    return {"id": iid, "app_id": app_id, "target_type": target, "repository_selection": selection,
            "account": {"login": login, "id": account_id}, "permissions": {"contents": "write"},
            "suspended_at": None}


class TestUserInstallations:
    def test_the_persons_installations_of_this_app(self, fake):
        fake.routes[("GET", "/user")] = (200, {"id": 1, "login": "alice"})
        fake.routes[("GET", "/user/installations")] = (200, {"total_count": 3, "installations": [
            _inst(10),                                             # her own account
            _inst(11, target="Organization", login="octo", account_id=9, selection="selected"),
            _inst(12, app_id=1),                                   # another app
        ]})
        fake.routes[("GET", "/user/installations/11/repositories")] = (200, {
            "repository_selection": "selected", "repositories": [{"full_name": "octo/a"}],
        })
        fake.routes[("GET", "/user/memberships/orgs/octo")] = (200, {"role": "admin", "state": "active"})
        got = github_app.user_installations("ghu_t", app_id="4242")
        assert (got.login, got.user_id) == ("alice", 1)
        assert [i["id"] for i in got.installations] == [10, 11]
        own, org = got.installations
        assert own["admin"] is True and own["repositories"] == [] and own["repository_selection"] == "all"
        assert org["admin"] is True and org["repositories"] == ["octo/a"] and org["account"] == "octo"
        assert org["target_type"] == "Organization" and org["account_id"] == 9
        assert fake.requests[0].headers["authorization"] == "Bearer ghu_t"

    @pytest.mark.parametrize("membership, admin", [
        ((200, {"role": "member", "state": "active"}), False),
        ((200, {"role": "admin", "state": "pending"}), False),
        ((403, {"message": "Resource not accessible by integration"}), False),
        ((404, {"message": "Not Found"}), False),
    ])
    def test_reaching_an_organization_is_not_owning_it(self, fake, membership, admin):
        fake.routes[("GET", "/user")] = (200, {"id": 1, "login": "bob"})
        fake.routes[("GET", "/user/installations")] = (200, {"installations": [
            _inst(11, target="Organization", login="octo", account_id=9),
        ]})
        fake.routes[("GET", "/user/memberships/orgs/octo")] = membership
        got = github_app.user_installations("ghu_t", app_id="4242")
        assert got.installations[0]["admin"] is admin

    def test_another_persons_account_is_not_owned(self, fake):
        fake.routes[("GET", "/user")] = (200, {"id": 2, "login": "bob"})
        fake.routes[("GET", "/user/installations")] = (200, {"installations": [_inst(10, login="alice", account_id=1)]})
        got = github_app.user_installations("ghu_t", app_id="4242")
        assert got.installations[0]["admin"] is False

    def test_the_pages(self, fake):
        fake.routes[("GET", "/user")] = (200, {"id": 1, "login": "alice"})

        def page(request):
            n = int(request.url.params.get("page"))
            rows = [_inst(100 + i) for i in range(100)] if n == 1 else [_inst(900)]
            return httpx.Response(200, json={"installations": rows})
        fake.routes[("GET", "/user/installations")] = page
        got = github_app.user_installations("ghu_t", app_id="4242")
        assert len(got.installations) == 101
        assert [int(r.url.params.get("page")) for r in fake.requests if r.url.path == "/user/installations"] == [1, 2]

    def test_a_cut_repository_list_is_marked(self, fake):
        fake.routes[("GET", "/user")] = (200, {"id": 1, "login": "alice"})
        fake.routes[("GET", "/user/installations")] = (200, {"installations": [
            _inst(11, selection="selected"), _inst(12, selection="selected")]})

        def pages(full_pages: int):
            def route(request):
                n = int(request.url.params.get("page"))
                count = 100 if n <= full_pages else 40
                return httpx.Response(200, json={"repository_selection": "selected", "repositories": [
                    {"full_name": f"alice/r{n}-{i}"} for i in range(count)]})
            return route
        fake.routes[("GET", "/user/installations/11/repositories")] = pages(full_pages=2)
        fake.routes[("GET", "/user/installations/12/repositories")] = pages(full_pages=1)
        cut, whole = github_app.user_installations("ghu_t", app_id="4242").installations
        assert len(cut["repositories"]) == 200 and cut["repositories_truncated"] is True
        assert len(whole["repositories"]) == 140 and whole["repositories_truncated"] is False
        from services.oauth import github_apps
        assert github_apps.reach_label(cut) == "200+ repositories"
        assert github_apps.reach_label(whole) == "140 repositories"
        assert github_apps.reach_label({"repository_selection": "all"}) == "all repositories"
        assert github_apps.reach_label({"repositories": ["a/b"]}) == "1 repository"

    def test_a_dead_user_token(self, fake):
        fake.routes[("GET", "/user")] = (401, {"message": "Bad credentials"})
        with pytest.raises(OAuthTokenError) as exc:
            github_app.user_installations("ghu_t", app_id="4242")
        assert exc.value.code == "invalid_grant"


# ---------------------------------------------------------------------------
# the generic provider at GitHub's token endpoint
# ---------------------------------------------------------------------------

def _provider() -> GenericOAuthProvider:
    return GenericOAuthProvider(
        provider_id="github", authorization_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token", userinfo_url="https://api.github.com/user",
    )


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patched(handler):
    return patch(
        "auth.oauth_providers.generic.httpx.AsyncClient",
        side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw),
    )


class TestGenericAtGithub:
    @pytest.mark.asyncio
    async def test_the_exchange_asks_for_json_and_reads_a_form_answer(self):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                200, text="access_token=ghu_1&refresh_token=ghr_1&expires_in=28800&token_type=bearer&scope=",
                headers={"content-type": "application/x-www-form-urlencoded; charset=utf-8"},
            )
        with _patched(handler):
            ts = await _provider().exchange_code(code="c", redirect_uri="https://x/cb", client_id="i", client_secret="s")
        assert seen[0].headers["accept"] == "application/json"
        assert (ts.access_token, ts.refresh_token, ts.expires_in) == ("ghu_1", "ghr_1", 28800)

    @pytest.mark.asyncio
    async def test_a_json_answer_still_reads(self):
        def handler(request):
            return httpx.Response(200, json={"access_token": "ghu_2", "expires_in": 28800, "refresh_token": "ghr_2"})
        with _patched(handler):
            ts = await _provider().refresh(refresh_token="ghr_1", client_id="i", client_secret="s")
        assert (ts.access_token, ts.refresh_token) == ("ghu_2", "ghr_2")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("word, code", [
        ("bad_refresh_token", "invalid_grant"),
        ("incorrect_client_credentials", "invalid_client"),
        ("something_else", "something_else"),
    ])
    async def test_githubs_error_words_map_to_the_standard_codes(self, word, code):
        def handler(request):
            return httpx.Response(200, text=f"error={word}&error_description=no",
                                  headers={"content-type": "application/x-www-form-urlencoded"})
        with _patched(handler), pytest.raises(OAuthTokenError) as exc:
            await _provider().refresh(refresh_token="ghr_1", client_id="i", client_secret="s")
        assert exc.value.code == code and exc.value.description == "no"

    @pytest.mark.asyncio
    async def test_the_device_poll_reads_githubs_pending_answer(self):
        def pending(request):
            return httpx.Response(200, json={"error": "authorization_pending", "error_description": "wait"})
        with _patched(pending):
            assert await _provider().poll_device_code(device_code="d", client_id="i", client_secret="s") is None

        def expired(request):
            return httpx.Response(200, json={"error": "expired_token", "error_description": "too late"})
        with _patched(expired), pytest.raises(RuntimeError, match="too late"):
            await _provider().poll_device_code(device_code="d", client_id="i", client_secret="s")

    @pytest.mark.asyncio
    async def test_an_html_page_reports_the_status_alone(self):
        def handler(request):
            return httpx.Response(502, text="<html>bad gateway</html>", headers={"content-type": "text/html"})
        with _patched(handler), pytest.raises(OAuthTokenError) as exc:
            await _provider().exchange_code(code="c", redirect_uri="https://x/cb", client_id="i", client_secret="s")
        assert exc.value.code == "http_502"

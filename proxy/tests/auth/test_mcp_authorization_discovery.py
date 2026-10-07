"""Discovery of a hosted MCP server's authorization server
(``services/oauth/mcp_authorization.py``): the well-known order and the
challenge fallback, the resource check by document kind, the issuer check,
https only, endpoints on the issuer's origin, PKCE S256, the private-address
refusal, the override's limits, the cache and ``forget``, the token
requests' shapes, the identity and scope helpers.
"""

from __future__ import annotations

import json

import httpx
import pytest

from auth.oauth_providers.base import OAuthTokenError
from services.oauth import mcp_authorization as ma

RESOURCE = "https://mcp.example.com/mcp"
ISSUER = "https://mcp.example.com"
PRM_PATH = f"{ISSUER}/.well-known/oauth-protected-resource/mcp"
PRM_ROOT = f"{ISSUER}/.well-known/oauth-protected-resource"
AS_META = f"{ISSUER}/.well-known/oauth-authorization-server"


def _as_doc(**over):
    doc = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "registration_endpoint": f"{ISSUER}/register",
        "revocation_endpoint": f"{ISSUER}/token",
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
        "scopes_supported": ["read", "write"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    }
    doc.update(over)
    return doc


class Vendor:
    """A fake vendor: a map of URL to (status, body, headers), a request log."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = str(request.url).split("?")[0]
        route = self.routes.get((request.method, key)) or self.routes.get(key)
        if route is None:
            return httpx.Response(404, text="not here")
        if callable(route):
            return route(request)
        status, body, headers = route
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body, headers=headers or {})
        return httpx.Response(status, text=body or "", headers=headers or {})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler), follow_redirects=False,
            headers={"User-Agent": ma.user_agent()},
        )


@pytest.fixture
def vendor(monkeypatch):
    v = Vendor()
    monkeypatch.setattr(ma, "_client", v.client)
    monkeypatch.setattr(ma, "_resolve_addresses", lambda host: ["8.8.8.8"])
    ma.clear_caches()
    yield v
    ma.clear_caches()


def _standard(vendor, prm_kind="path"):
    if prm_kind == "path":
        vendor.routes[PRM_PATH] = (200, {"resource": RESOURCE, "authorization_servers": [ISSUER],
                                         "scopes_supported": ["read"]}, {})
    elif prm_kind == "root":
        vendor.routes[PRM_ROOT] = (200, {"resource": ISSUER, "authorization_servers": [ISSUER]}, {})
    vendor.routes[AS_META] = (200, _as_doc(), {})


class TestResourceMetadata:
    @pytest.mark.asyncio
    async def test_path_suffixed_document_first(self, vendor):
        _standard(vendor, "path")
        vendor.routes[PRM_ROOT] = (200, {"resource": "https://other.example", "authorization_servers": ["https://other.example"]}, {})
        server = await ma.discover(RESOURCE)
        assert server.issuer == ISSUER
        assert server.resource == RESOURCE
        assert server.scopes_supported == ["read"]
        assert [str(r.url) for r in vendor.requests][0] == PRM_PATH

    @pytest.mark.asyncio
    async def test_root_document_names_the_origin(self, vendor):
        _standard(vendor, "root")
        server = await ma.discover(RESOURCE)
        assert server.issuer == ISSUER and server.resource == ISSUER

    @pytest.mark.asyncio
    async def test_a_redirect_counts_as_absent(self, vendor):
        vendor.routes[PRM_PATH] = (302, "", {"location": "https://elsewhere.example/x"})
        _standard(vendor, "root")
        server = await ma.discover(RESOURCE)
        assert server.issuer == ISSUER
        assert not any(str(r.url).startswith("https://elsewhere") for r in vendor.requests)

    @pytest.mark.asyncio
    async def test_challenge_fallback_with_its_scopes(self, vendor):
        vendor.routes[("POST", RESOURCE)] = (401, {"error": "invalid_token"}, {
            "www-authenticate": f'Bearer realm="OAuth", resource_metadata="{PRM_PATH}", scope="read write"'})
        vendor.routes[("GET", PRM_PATH)] = (200, {"resource": RESOURCE, "authorization_servers": [ISSUER]}, {})
        vendor.routes[AS_META] = (200, _as_doc(), {})
        # the well-known probes find nothing (both GETs 404) until the challenge names the URL
        calls = {"n": 0}
        real = vendor.handler

        def gated(request):
            if request.method == "GET" and str(request.url) == PRM_PATH:
                calls["n"] += 1
                if calls["n"] == 1:
                    return httpx.Response(404)
            return real(request)
        vendor.handler = gated
        server = await ma.discover(RESOURCE)
        assert server.challenge_scopes == ["read", "write"]
        posted = [r for r in vendor.requests if r.method == "POST"]
        assert posted and posted[0].headers["accept"] == "application/json, text/event-stream"
        assert json.loads(posted[0].content)["method"] == "initialize"

    @pytest.mark.asyncio
    async def test_a_resource_with_a_trailing_slash_keeps_it(self, vendor):
        """GitHub's server lives at ``/mcp/``: the path-suffixed document is
        fetched with the slash and may name the resource without it."""
        res = "https://api.example.com/mcp/"
        vendor.routes["https://api.example.com/.well-known/oauth-protected-resource/mcp/"] = (
            200, {"resource": "https://api.example.com/mcp", "authorization_servers": ["https://auth.example.com/login/oauth"]}, {})
        vendor.routes["https://auth.example.com/.well-known/oauth-authorization-server/login/oauth"] = (
            200, _as_doc(issuer="https://auth.example.com/login/oauth",
                         authorization_endpoint="https://auth.example.com/login/oauth/authorize",
                         token_endpoint="https://auth.example.com/login/oauth/access_token",
                         registration_endpoint="", revocation_endpoint=""), {})
        server = await ma.discover(res)
        assert server.resource == "https://api.example.com/mcp"
        assert server.registration_endpoint == ""
        assert [str(r.url) for r in vendor.requests][0].endswith("/oauth-protected-resource/mcp/")

    @pytest.mark.asyncio
    async def test_a_challenge_metadata_url_off_the_resource_origin_is_not_followed(self, vendor):
        vendor.routes[("POST", RESOURCE)] = (401, {"error": "invalid_token"}, {
            "www-authenticate": 'Bearer resource_metadata="https://evil.example/.well-known/oauth-protected-resource"'})
        vendor.routes["https://evil.example/.well-known/oauth-protected-resource"] = (
            200, {"resource": RESOURCE, "authorization_servers": ["https://evil.example"]}, {})
        with pytest.raises(ma.AuthorizationError, match="names no authorization server"):
            await ma.discover(RESOURCE)
        assert not any(str(r.url).startswith("https://evil.example") for r in vendor.requests)

    @pytest.mark.asyncio
    async def test_resource_mismatch_is_refused(self, vendor):
        vendor.routes[PRM_PATH] = (200, {"resource": "https://mcp.example.com/other", "authorization_servers": [ISSUER]}, {})
        with pytest.raises(ma.AuthorizationError, match="names another resource"):
            await ma.discover(RESOURCE)

    @pytest.mark.asyncio
    async def test_no_metadata_anywhere(self, vendor):
        with pytest.raises(ma.AuthorizationError, match="names no authorization server"):
            await ma.discover(RESOURCE)

    @pytest.mark.asyncio
    async def test_http_resource_refused_before_any_call(self, vendor):
        with pytest.raises(ma.AuthorizationError, match="not an https URL"):
            await ma.discover("http://mcp.example.com/mcp")
        assert vendor.requests == []

    @pytest.mark.asyncio
    async def test_oversized_document_refused(self, vendor):
        vendor.routes[PRM_PATH] = (200, "x" * (ma.DOC_BYTES_CAP + 1), {"content-type": "application/json"})
        with pytest.raises(ma.AuthorizationError, match="larger than allowed"):
            await ma.discover(RESOURCE)


class TestServerMetadata:
    @pytest.mark.asyncio
    async def test_issuer_mismatch_refused(self, vendor):
        _standard(vendor)
        vendor.routes[AS_META] = (200, _as_doc(issuer="https://evil.example"), {})
        with pytest.raises(ma.AuthorizationError, match="names another issuer"):
            await ma.discover(RESOURCE)

    @pytest.mark.asyncio
    async def test_endpoint_on_another_origin_refused(self, vendor):
        _standard(vendor)
        vendor.routes[AS_META] = (200, _as_doc(token_endpoint="https://api.example.com/token"), {})
        with pytest.raises(ma.AuthorizationError, match="token_endpoint sits on another origin"):
            await ma.discover(RESOURCE)

    @pytest.mark.asyncio
    async def test_http_endpoint_refused(self, vendor):
        _standard(vendor)
        vendor.routes[AS_META] = (200, _as_doc(authorization_endpoint="http://mcp.example.com/authorize"), {})
        with pytest.raises(ma.AuthorizationError, match="not an https URL"):
            await ma.discover(RESOURCE)

    @pytest.mark.asyncio
    async def test_no_s256_refused(self, vendor):
        _standard(vendor)
        vendor.routes[AS_META] = (200, _as_doc(code_challenge_methods_supported=["plain"]), {})
        with pytest.raises(ma.AuthorizationError, match="PKCE S256"):
            await ma.discover(RESOURCE)

    @pytest.mark.asyncio
    async def test_path_insert_then_openid_variants(self, vendor):
        issuer = "https://auth.example.com/tenant"
        vendor.routes[PRM_PATH] = (200, {"resource": RESOURCE, "authorization_servers": [issuer]}, {})
        vendor.routes["https://auth.example.com/tenant/.well-known/openid-configuration"] = (
            200, _as_doc(issuer=issuer, authorization_endpoint=f"{issuer}/a", token_endpoint=f"{issuer}/t",
                         registration_endpoint="", revocation_endpoint=""), {})
        server = await ma.discover(RESOURCE)
        assert server.issuer == issuer and server.registration_endpoint == ""
        urls = [str(r.url) for r in vendor.requests if r.method == "GET"]
        assert urls[1:4] == [
            "https://auth.example.com/.well-known/oauth-authorization-server/tenant",
            "https://auth.example.com/.well-known/openid-configuration/tenant",
            "https://auth.example.com/tenant/.well-known/openid-configuration",
        ]

    @pytest.mark.asyncio
    async def test_no_metadata_at_the_issuer(self, vendor):
        _standard(vendor)
        vendor.routes.pop(AS_META)
        with pytest.raises(ma.AuthorizationError, match="publishes no authorization server metadata"):
            await ma.discover(RESOURCE)


class TestOverrideAndPrivate:
    @pytest.mark.asyncio
    async def test_override_must_be_listed(self, vendor):
        _standard(vendor)
        with pytest.raises(ma.AuthorizationError, match="not one mcp.example.com names"):
            await ma.discover(RESOURCE, issuer_override="https://evil.example")

    @pytest.mark.asyncio
    async def test_override_picks_a_listed_issuer(self, vendor):
        other = "https://auth.example.com"
        vendor.routes[PRM_PATH] = (200, {"resource": RESOURCE, "authorization_servers": [ISSUER, other]}, {})
        vendor.routes[f"{other}/.well-known/oauth-authorization-server"] = (
            200, _as_doc(issuer=other, authorization_endpoint=f"{other}/a", token_endpoint=f"{other}/t",
                         registration_endpoint=f"{other}/r", revocation_endpoint=""), {})
        server = await ma.discover(RESOURCE, issuer_override=other)
        assert server.issuer == other

    @pytest.mark.asyncio
    async def test_private_issuer_refused_for_a_public_mcp(self, vendor, monkeypatch):
        _standard(vendor)
        monkeypatch.setattr(ma, "_resolve_addresses",
                            lambda host: ["10.0.0.5"] if host == "mcp.example.com" else ["8.8.8.8"])
        # the MCP host is private here, so its issuer may be too
        await ma.discover(RESOURCE)
        ma.clear_caches()
        monkeypatch.setattr(ma, "_resolve_addresses",
                            lambda host: ["8.8.8.8"] if host == "mcp.example.com" else ["10.0.0.5"])
        vendor.routes[PRM_PATH] = (200, {"resource": RESOURCE, "authorization_servers": ["https://auth.internal"]}, {})
        with pytest.raises(ma.AuthorizationError, match="private address"):
            await ma.discover(RESOURCE)


class TestCache:
    @pytest.mark.asyncio
    async def test_cached_until_forgotten(self, vendor):
        _standard(vendor)
        await ma.discover(RESOURCE)
        n = len(vendor.requests)
        await ma.discover(RESOURCE)
        assert len(vendor.requests) == n
        ma.forget(RESOURCE)
        await ma.discover(RESOURCE)
        assert len(vendor.requests) > n

    @pytest.mark.asyncio
    async def test_ttl_expiry(self, vendor, monkeypatch):
        _standard(vendor)
        clock = {"now": 1000.0}
        monkeypatch.setattr(ma, "_monotonic", lambda: clock["now"])
        await ma.discover(RESOURCE)
        n = len(vendor.requests)
        clock["now"] += ma.DISCOVERY_TTL_S + 1
        await ma.discover(RESOURCE)
        assert len(vendor.requests) > n


class TestTokenRequests:
    @pytest.mark.asyncio
    async def test_public_exchange_sends_verifier_resource_and_no_secret(self, vendor):
        seen = {}

        def token(request):
            seen["form"] = dict(httpx.QueryParams(request.content.decode()))
            seen["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT",
                                             "expires_in": 3600, "workspace_id": "ws-1"})
        vendor.routes[("POST", f"{ISSUER}/token")] = token
        ts = await ma.exchange_code(
            "p", token_endpoint=f"{ISSUER}/token", code="c", redirect_uri="https://d/cb",
            code_verifier="v", resource=RESOURCE, method="none", client_id="cid", client_secret="",
        )
        assert seen["form"] == {"grant_type": "authorization_code", "code": "c",
                                "redirect_uri": "https://d/cb", "code_verifier": "v",
                                "resource": RESOURCE, "client_id": "cid"}
        assert seen["auth"] == ""
        assert ts.access_token == "AT" and ts.refresh_token == "RT"
        assert ts.raw == {"workspace_id": "ws-1"}

    @pytest.mark.asyncio
    async def test_confidential_methods(self, vendor):
        seen = []

        def token(request):
            seen.append((dict(httpx.QueryParams(request.content.decode())), request.headers.get("authorization", "")))
            return httpx.Response(200, json={"access_token": "AT"})
        vendor.routes[("POST", f"{ISSUER}/token")] = token
        await ma.refresh("p", token_endpoint=f"{ISSUER}/token", refresh_token="RT", resource=RESOURCE,
                         method="client_secret_post", client_id="cid", client_secret="sec")
        await ma.refresh("p", token_endpoint=f"{ISSUER}/token", refresh_token="RT", resource=RESOURCE,
                         method="client_secret_basic", client_id="cid", client_secret="sec")
        assert seen[0][0]["client_secret"] == "sec" and seen[0][1] == ""
        assert "client_secret" not in seen[1][0] and seen[1][1].startswith("Basic ")

    @pytest.mark.asyncio
    async def test_refresh_keeps_the_old_refresh_token_when_omitted(self, vendor):
        vendor.routes[("POST", f"{ISSUER}/token")] = (200, {"access_token": "AT2", "expires_in": 60}, {})
        ts = await ma.refresh("p", token_endpoint=f"{ISSUER}/token", refresh_token="RT", resource=RESOURCE,
                              method="none", client_id="cid", client_secret="")
        assert ts.refresh_token == "RT"

    @pytest.mark.asyncio
    async def test_error_code_is_typed(self, vendor):
        vendor.routes[("POST", f"{ISSUER}/token")] = (400, {"error": "invalid_grant", "error_description": "gone"}, {})
        with pytest.raises(OAuthTokenError) as info:
            await ma.refresh("p", token_endpoint=f"{ISSUER}/token", refresh_token="RT", resource=RESOURCE,
                             method="none", client_id="cid", client_secret="")
        assert info.value.code == "invalid_grant" and info.value.status == 400
        assert "invalid_grant" in str(info.value) and "gone" in str(info.value)

    @pytest.mark.asyncio
    async def test_html_404_and_unreachable_are_typed(self, vendor):
        vendor.routes[("POST", f"{ISSUER}/token")] = (404, "<html>moved</html>", {})
        with pytest.raises(OAuthTokenError) as info:
            await ma.refresh("p", token_endpoint=f"{ISSUER}/token", refresh_token="RT", resource=RESOURCE,
                             method="none", client_id="cid", client_secret="")
        assert info.value.code == "http_404"

        def boom(request):
            raise httpx.ConnectError("refused")
        vendor.routes[("POST", f"{ISSUER}/token")] = boom
        with pytest.raises(OAuthTokenError) as info:
            await ma.refresh("p", token_endpoint=f"{ISSUER}/token", refresh_token="RT", resource=RESOURCE,
                             method="none", client_id="cid", client_secret="")
        assert info.value.code == "unreachable"

    @pytest.mark.asyncio
    async def test_revocation_only_on_the_issuer_origin(self, vendor):
        vendor.routes[("POST", f"{ISSUER}/token")] = (200, "", {})
        assert await ma.revoke(f"{ISSUER}/token", issuer=ISSUER, token="RT", method="none",
                               client_id="cid", client_secret="") is True
        assert await ma.revoke("https://other.example/revoke", issuer=ISSUER, token="RT", method="none",
                               client_id="cid", client_secret="") is False
        assert all(str(r.url).startswith(ISSUER) for r in vendor.requests)

    def test_authorize_url_keeps_the_endpoints_query(self):
        url = ma.build_authorize_url(
            f"{ISSUER}/authorize?tenant=x", client_id="cid", redirect_uri="https://d/cb",
            scope="read write", state="st", code_challenge="ch", resource=RESOURCE,
        )
        q = dict(httpx.QueryParams(url.split("?", 1)[1]))
        assert q == {"tenant": "x", "response_type": "code", "client_id": "cid",
                     "redirect_uri": "https://d/cb", "state": "st", "code_challenge": "ch",
                     "code_challenge_method": "S256", "resource": RESOURCE, "scope": "read write"}


class TestHelpers:
    def test_private_addresses(self):
        for a in ("10.0.0.5", "127.0.0.1", "169.254.1.1", "0.0.0.0", "224.0.0.1", "::1", "::ffff:10.0.0.5", "fe80::1"):
            assert ma._is_private(a), a
        for a in ("8.8.8.8", "2606:4700:4700::1111", "not-an-ip"):
            assert not ma._is_private(a), a

    def test_id_token_never_lands_in_extra(self):
        ts = ma._token_set({"access_token": "a", "refresh_token": "r", "id_token": "jwt", "expires_in": 5,
                            "scope": "s", "token_type": "Bearer", "workspace_id": "w"})
        assert ts.raw == {"workspace_id": "w"}

    def test_canonical_resource(self):
        assert ma.canonical_resource("HTTPS://MCP.Example.com/mcp#frag") == "https://mcp.example.com/mcp"
        assert ma.canonical_resource("https://api.githubcopilot.com/mcp/") == "https://api.githubcopilot.com/mcp/"

    def test_identity_from_the_token_response(self):
        block = {"identity": {"label_field": "workspace_id", "display_field": "email_domain", "id_field": "user_id"}}
        label, info = ma.identity_from_token_response(
            {"workspace_id": "ws-1", "email_domain": "acme.com", "user_id": "u-1"},
            block, provider_id="notion-hosted", label_hint="",
        )
        assert (label, info.email, info.account_id) == ("ws-1", "acme.com", "u-1")
        label, info = ma.identity_from_token_response({}, {}, provider_id="p", label_hint=" Work ")
        assert (label, info.email, info.account_id) == ("Work", "Work", "Work")
        label, info = ma.identity_from_token_response({}, {}, provider_id="p", label_hint="")
        assert (label, info.email) == ("p", "p")

    def test_scope_order(self):
        server = ma.AuthorizationServer(issuer=ISSUER, authorization_endpoint="a", token_endpoint="t",
                                        resource=RESOURCE, scopes_supported=["default"],
                                        challenge_scopes=["read"])
        assert ma.scopes_for({}, service_scopes=["write", "write"], server=server) == "write"
        assert ma.scopes_for({"scopes": ["x"]}, service_scopes=[], server=server) == "x"
        assert ma.scopes_for({}, service_scopes=[], server=server) == "read"
        server.challenge_scopes = []
        assert ma.scopes_for({}, service_scopes=[], server=server) == "default"
        assert ma.registration_scope({"scopes": ["offline"]}, {"services": [
            {"key": "a", "scopes": ["read"]}, {"key": "b", "scopes": ["read", "write"]}]}) == "read write offline"

"""The install's client registration at a vendor's authorization server
(``mcp_authorization.ensure_registration``): one row for concurrent
connects, no endpoint refused, the vendor's refusal text, the echo check,
a secret stored encrypted and never logged, an expired or undecryptable
secret superseded, a passing failure as "try again later".
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from services.oauth import mcp_authorization as ma
from storage.identity import oauth_client_registrations as regs
from storage.pg import get_conn
from tests.auth.test_mcp_authorization_discovery import ISSUER, RESOURCE, Vendor

CB = "https://dash.example/v1/oauth/notion-hosted/callback"
REGISTER = f"{ISSUER}/register"


def _server(**over):
    kw = dict(issuer=ISSUER, authorization_endpoint=f"{ISSUER}/authorize",
              token_endpoint=f"{ISSUER}/token", registration_endpoint=REGISTER,
              resource=RESOURCE,
              token_endpoint_auth_methods=["client_secret_basic", "client_secret_post", "none"])
    kw.update(over)
    return ma.AuthorizationServer(**kw)


@pytest.fixture
def vendor(monkeypatch):
    v = Vendor()
    monkeypatch.setattr(ma, "_client", v.client)
    ma.clear_caches()
    yield v
    ma.clear_caches()


def _answer(request, **over):
    body = json.loads(request.content)
    answer = {"client_id": "cid-" + str(len(body.get("redirect_uris", []))),
              "redirect_uris": body.get("redirect_uris"), "grant_types": body.get("grant_types"),
              "token_endpoint_auth_method": body.get("token_endpoint_auth_method"),
              "client_name": body.get("client_name")}
    answer.update(over)
    return httpx.Response(201, json=answer)


class TestRegisterOnce:
    @pytest.mark.asyncio
    async def test_registers_then_reuses(self, vendor):
        posts = []
        vendor.routes[("POST", REGISTER)] = lambda r: (posts.append(json.loads(r.content)), _answer(r))[1]
        row = await ma.ensure_registration(_server(), redirect_uri=CB, block={}, scope="read write")
        again = await ma.ensure_registration(_server(), redirect_uri=CB, block={}, scope="read write")
        assert again["id"] == row["id"] and len(posts) == 1
        sent = posts[0]
        assert sent["redirect_uris"] == [CB]
        assert sent["token_endpoint_auth_method"] == "none"
        assert sent["grant_types"] == ["authorization_code", "refresh_token"]
        assert sent["application_type"] == "web" and sent["scope"] == "read write"
        assert sent["software_id"] == "otodock" and sent["client_uri"] == "https://otodock.io"
        assert sent["client_name"].startswith("OtoDock (")
        assert row["token_endpoint_auth_method"] == "none" and row["has_secret"] is False

    @pytest.mark.asyncio
    async def test_loopback_callback_registers_a_native_client(self, vendor):
        posts = []
        vendor.routes[("POST", REGISTER)] = lambda r: (posts.append(json.loads(r.content)), _answer(r))[1]
        await ma.ensure_registration(_server(), redirect_uri="http://localhost:8400/cb", block={}, scope="")
        assert posts[0]["application_type"] == "native" and "scope" not in posts[0]

    @pytest.mark.asyncio
    async def test_concurrent_connects_register_once(self, vendor):
        """Both coroutines are inside the vendor call's window: the lock
        serialises them and the second finds the first's row."""
        gate = asyncio.Event()
        posts = []

        def slow(request):
            posts.append(1)
            return _answer(request)
        vendor.routes[("POST", REGISTER)] = slow
        real_register = ma._register

        async def gated(*a, **k):
            await gate.wait()
            return await real_register(*a, **k)
        ma._register = gated
        try:
            t1 = asyncio.create_task(ma.ensure_registration(_server(), redirect_uri=CB, block={}, scope=""))
            t2 = asyncio.create_task(ma.ensure_registration(_server(), redirect_uri=CB, block={}, scope=""))
            await asyncio.sleep(0.05)
            assert not t1.done() and not t2.done()
            gate.set()
            r1, r2 = await asyncio.gather(t1, t2)
        finally:
            ma._register = real_register
        assert r1["id"] == r2["id"] and len(posts) == 1

    @pytest.mark.asyncio
    async def test_another_callback_registers_again(self, vendor):
        vendor.routes[("POST", REGISTER)] = _answer
        a = await ma.ensure_registration(_server(), redirect_uri=CB, block={}, scope="")
        b = await ma.ensure_registration(_server(), redirect_uri="https://new.example/cb", block={}, scope="")
        assert a["id"] != b["id"] and regs.get_live(ISSUER, CB)["id"] == a["id"]


class TestRefusals:
    @pytest.mark.asyncio
    async def test_no_registration_endpoint(self, vendor):
        with pytest.raises(ma.RegistrationUnavailable, match="does not register clients"):
            await ma.ensure_registration(_server(registration_endpoint=""), redirect_uri=CB, block={}, scope="")
        assert vendor.requests == []

    @pytest.mark.asyncio
    async def test_vendor_refusal_is_reported_and_remembered(self, vendor):
        vendor.routes[("POST", REGISTER)] = (400, {"error": "invalid_redirect_uri",
                                                   "error_description": "Redirect URI must use HTTPS"}, {})
        with pytest.raises(ma.RegistrationRefused) as info:
            await ma.ensure_registration(_server(), redirect_uri="http://10.0.0.1:8400/cb", block={}, scope="")
        assert "invalid_redirect_uri: Redirect URI must use HTTPS" in str(info.value)
        assert info.value.status == 400
        n = len(vendor.requests)
        with pytest.raises(ma.RegistrationRefused, match="Redirect URI must use HTTPS"):
            await ma.ensure_registration(_server(), redirect_uri="http://10.0.0.1:8400/cb", block={}, scope="")
        assert len(vendor.requests) == n

    @pytest.mark.asyncio
    async def test_passing_failures_are_try_again_later(self, vendor):
        vendor.routes[("POST", REGISTER)] = (503, "down", {})
        with pytest.raises(ma.TryAgainLater) as info:
            await ma.ensure_registration(_server(), redirect_uri=CB, block={}, scope="")
        assert info.value.status == 503
        vendor.routes[("POST", REGISTER)] = (429, {"error": "slow_down"}, {})
        with pytest.raises(ma.TryAgainLater):
            await ma.ensure_registration(_server(), redirect_uri=CB, block={}, scope="")

        def boom(request):
            raise httpx.ConnectError("refused")
        vendor.routes[("POST", REGISTER)] = boom
        with pytest.raises(ma.TryAgainLater, match="could not be reached"):
            await ma.ensure_registration(_server(), redirect_uri=CB, block={}, scope="")
        assert regs.get_live(ISSUER, CB) is None

    @pytest.mark.asyncio
    async def test_a_redirect_at_the_endpoint_is_try_again_later(self, vendor):
        vendor.routes[("POST", REGISTER)] = (302, "", {"location": "https://elsewhere.example/register"})
        with pytest.raises(ma.TryAgainLater):
            await ma.ensure_registration(_server(), redirect_uri=CB, block={}, scope="")
        assert regs.get_live(ISSUER, CB) is None and ISSUER not in ma._refusals

    @pytest.mark.asyncio
    async def test_answer_must_echo_the_callback(self, vendor):
        vendor.routes[("POST", REGISTER)] = lambda r: _answer(r, redirect_uris=["https://other/cb"])
        with pytest.raises(ma.AuthorizationError, match="registered another callback"):
            await ma.ensure_registration(_server(), redirect_uri=CB, block={}, scope="")

    @pytest.mark.asyncio
    async def test_answer_without_a_client_id(self, vendor):
        vendor.routes[("POST", REGISTER)] = (201, {"client_secret": "leak-me"}, {})
        with pytest.raises(ma.AuthorizationError) as info:
            await ma.ensure_registration(_server(), redirect_uri=CB, block={}, scope="")
        assert "leak-me" not in str(info.value)

    @pytest.mark.asyncio
    async def test_confidential_without_a_secret_refused(self, vendor):
        vendor.routes[("POST", REGISTER)] = _answer
        with pytest.raises(ma.AuthorizationError, match="without a secret"):
            await ma.ensure_registration(_server(), redirect_uri=CB, block={"confidential": True}, scope="")

    @pytest.mark.asyncio
    async def test_confidential_needs_an_offered_method(self, vendor):
        with pytest.raises(ma.AuthorizationError, match="no client-secret method"):
            await ma.ensure_registration(_server(token_endpoint_auth_methods=["none"]), redirect_uri=CB,
                                         block={"confidential": True}, scope="")


class TestSecrets:
    @pytest.mark.asyncio
    async def test_secret_stored_encrypted_and_never_logged(self, vendor, caplog):
        caplog.set_level(logging.DEBUG)
        caplog.set_level(logging.DEBUG, logger="httpx")  # restored at teardown
        vendor.routes[("POST", REGISTER)] = lambda r: _answer(
            r, client_secret="top-secret-value", client_secret_expires_at=0,
            registration_access_token="rat-value", registration_client_uri=f"{REGISTER}/cid")
        row = await ma.ensure_registration(_server(), redirect_uri=CB,
                                           block={"confidential": True}, scope="")
        assert row["token_endpoint_auth_method"] == "client_secret_post"
        assert row["has_secret"] is True
        assert "client_secret" not in row and "registration_access_token" not in row
        with get_conn() as conn:
            raw = conn.execute("SELECT client_secret_enc, registration_access_token_enc FROM "
                               "oauth_client_registrations WHERE id = %s", (row["id"],)).fetchone()
        assert "top-secret-value" not in raw["client_secret_enc"]
        assert "rat-value" not in raw["registration_access_token_enc"]
        assert regs.client_secret(row["id"]) == "top-secret-value"
        assert "top-secret-value" not in caplog.text and "rat-value" not in caplog.text
        assert json.dumps(regs.list_all()).count("top-secret-value") == 0

    @pytest.mark.asyncio
    async def test_a_public_request_answered_with_a_secret_keeps_it(self, vendor):
        vendor.routes[("POST", REGISTER)] = lambda r: _answer(
            r, client_secret="s", token_endpoint_auth_method="client_secret_basic")
        row = await ma.ensure_registration(_server(), redirect_uri=CB, block={}, scope="")
        assert row["token_endpoint_auth_method"] == "client_secret_basic" and row["has_secret"]

    @pytest.mark.asyncio
    async def test_expired_secret_is_superseded(self, vendor):
        vendor.routes[("POST", REGISTER)] = lambda r: _answer(r, client_id="cid-new")
        old = regs.insert(issuer=ISSUER, redirect_uri=CB, registration_endpoint=REGISTER,
                          client_id="cid-old", client_secret="s", client_secret_expires_at="1000")
        row = await ma.ensure_registration(_server(), redirect_uri=CB, block={}, scope="")
        assert row["client_id"] == "cid-new" and row["id"] != old["id"]
        assert regs.get(old["id"])["revoked_reason"] == "secret_expired"

    @pytest.mark.asyncio
    async def test_an_iso_expiry_is_read_too(self, vendor):
        vendor.routes[("POST", REGISTER)] = lambda r: _answer(r, client_id="cid-new")
        old = regs.insert(issuer=ISSUER, redirect_uri=CB, registration_endpoint=REGISTER,
                          client_id="cid-old", client_secret="s", client_secret_expires_at="2020-01-01T00:00:00Z")
        row = await ma.ensure_registration(_server(), redirect_uri=CB, block={}, scope="")
        assert row["client_id"] == "cid-new" and regs.get(old["id"])["revoked_reason"] == "secret_expired"

    @pytest.mark.asyncio
    async def test_a_changed_confidential_flag_registers_again(self, vendor):
        vendor.routes[("POST", REGISTER)] = lambda r: _answer(
            r, client_id="cid-conf", client_secret="s", token_endpoint_auth_method="client_secret_post")
        public = regs.insert(issuer=ISSUER, redirect_uri=CB, registration_endpoint=REGISTER, client_id="cid-pub")
        row = await ma.ensure_registration(_server(), redirect_uri=CB, block={"confidential": True}, scope="")
        assert row["client_id"] == "cid-conf" and regs.get(public["id"])["revoked_reason"] == "auth_method_changed"
        again = await ma.ensure_registration(_server(), redirect_uri=CB, block={"confidential": True}, scope="")
        assert again["id"] == row["id"]

    @pytest.mark.asyncio
    async def test_undecryptable_secret_is_superseded(self, vendor):
        vendor.routes[("POST", REGISTER)] = lambda r: _answer(r, client_id="cid-new")
        old = regs.insert(issuer=ISSUER, redirect_uri=CB, registration_endpoint=REGISTER,
                          client_id="cid-old", client_secret="s")
        with get_conn() as conn:
            conn.execute("UPDATE oauth_client_registrations SET client_secret_enc = 'garbage' WHERE id = %s",
                         (old["id"],))
            conn.commit()
        row = await ma.ensure_registration(_server(), redirect_uri=CB, block={}, scope="")
        assert row["client_id"] == "cid-new"
        assert regs.get(old["id"])["revoked_reason"] == "undecryptable"

    @pytest.mark.asyncio
    async def test_a_revoked_row_registers_afresh(self, vendor):
        vendor.routes[("POST", REGISTER)] = lambda r: _answer(r, client_id="cid-new")
        old = regs.insert(issuer=ISSUER, redirect_uri=CB, registration_endpoint=REGISTER, client_id="cid-old")
        regs.revoke(old["id"], "vendor_revoked")
        row = await ma.ensure_registration(_server(), redirect_uri=CB, block={}, scope="")
        assert row["client_id"] == "cid-new" and row["id"] != old["id"]

"""MicrosoftOAuthProvider tests.

Covers:
  * Provider registration as a stateless singleton (no per-tenant instances).
  * Tenant-aware URL construction (`/common/` default, override via extra).
  * PKCE-friendly authorize URL (challenge query params pass through extra).
  * id_token decode injects ``tid``/``oid``/``preferred_username`` into
    ``TokenSet.raw``, on both exchange_code and refresh.
  * ``fetch_userinfo`` falls back from ``mail`` to ``userPrincipalName``.
  * ``build_admin_consent_url`` shape (non-ABC Microsoft-only method).
  * Device-code start uses tenant-scoped URL; polling uses ``/common/``.
  * ``account_label`` consumer guidance: provider returns ``userPrincipalName``
    via ``userinfo_id_field``-free path (UserInfo.email is mail-or-upn).
  * Refresh re-persists rotated refresh token + re-decodes id_token.

HTTP is mocked via httpx.AsyncClient. JWTs are minted via PyJWT.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import jwt

from auth.oauth_providers import get_provider
from auth.oauth_providers.microsoft import MicrosoftOAuthProvider


@pytest.fixture
def provider() -> MicrosoftOAuthProvider:
    return MicrosoftOAuthProvider()


def _mint_id_token(claims: dict) -> str:
    """Mint an unsigned JWT — Microsoft provider decodes with
    verify_signature=False so HMAC secret doesn't matter."""
    return jwt.encode(claims, "anything", algorithm="HS256")


def _mock_post(json_payload: dict, status: int = 200):
    mock_response = MagicMock()
    mock_response.status_code = status
    mock_response.json = MagicMock(return_value=json_payload)
    mock_response.text = ""
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)
    return mock_client


def _mock_get(json_payload: dict, status: int = 200):
    mock_response = MagicMock()
    mock_response.status_code = status
    mock_response.json = MagicMock(return_value=json_payload)
    mock_response.text = ""
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)
    return mock_client


# ---------------------------------------------------------------------------
# Registration + class-level metadata
# ---------------------------------------------------------------------------


class TestMicrosoftRegistration:
    def test_microsoft_resolves_to_subclass(self):
        p = get_provider("microsoft")
        assert isinstance(p, MicrosoftOAuthProvider)
        assert p.provider_id == "microsoft"

    def test_singleton_has_no_per_tenant_state(self, provider):
        """Hardcoded provider is a stateless singleton — tenant_id is
        passed per-call via extra, not stored on the instance."""
        assert not hasattr(provider, "tenant_id")
        # Default `/common/` URLs are class-level (used by token endpoints
        # which are tenant-agnostic for Microsoft).
        assert "/common/" in provider.token_url
        assert provider.flow == "authorization_code_pkce"
        assert provider.userinfo_url == "https://graph.microsoft.com/v1.0/me"
        # No revoke endpoint at Microsoft.
        assert provider.revoke_url == ""


# ---------------------------------------------------------------------------
# Tenant-aware URL construction
# ---------------------------------------------------------------------------


class TestMicrosoftTenantUrls:
    def test_urls_for_common_default(self):
        # Empty / falsy tenant_id falls back to /common/.
        assert MicrosoftOAuthProvider._authorize_url_for("").endswith(
            "/common/oauth2/v2.0/authorize"
        )
        assert MicrosoftOAuthProvider._devicecode_url_for("").endswith(
            "/common/oauth2/v2.0/devicecode"
        )
        assert MicrosoftOAuthProvider._adminconsent_url_for("").endswith(
            "/common/v2.0/adminconsent"
        )

    def test_urls_for_single_tenant_uuid(self):
        assert "/acme-uuid-1234/" in (
            MicrosoftOAuthProvider._authorize_url_for("acme-uuid-1234")
        )
        assert "/acme-uuid-1234/" in (
            MicrosoftOAuthProvider._devicecode_url_for("acme-uuid-1234")
        )
        assert "/acme-uuid-1234/" in (
            MicrosoftOAuthProvider._adminconsent_url_for("acme-uuid-1234")
        )


# ---------------------------------------------------------------------------
# build_auth_url — reads tenant_id from extra, supports PKCE pass-through
# ---------------------------------------------------------------------------


class TestMicrosoftAuthUrl:
    @pytest.mark.asyncio
    async def test_no_tenant_defaults_to_common(self, provider):
        url = await provider.build_auth_url(
            state="st", scopes=["openid"], redirect_uri="https://x/cb",
            client_id="cid",
        )
        assert "/common/oauth2/v2.0/authorize?" in url
        assert "client_id=cid" in url
        assert "state=st" in url
        assert "scope=openid" in url

    @pytest.mark.asyncio
    async def test_tenant_id_extra_picks_url(self, provider):
        url = await provider.build_auth_url(
            state="s", scopes=["openid"], redirect_uri="https://x/cb",
            client_id="c", extra={"tenant_id": "acme-uuid"},
        )
        assert "/acme-uuid/oauth2/v2.0/authorize?" in url
        # tenant_id MUST NOT appear as a query param — it selects the URL.
        assert "tenant_id" not in url

    @pytest.mark.asyncio
    async def test_pkce_challenge_params_pass_through(self, provider):
        """Engine merges code_challenge/code_challenge_method into the
        URL extras; provider passes them straight through to query params."""
        url = await provider.build_auth_url(
            state="s", scopes=["openid"], redirect_uri="https://x/cb",
            client_id="c",
            extra={
                "tenant_id": "common",
                "code_challenge": "abc123",
                "code_challenge_method": "S256",
            },
        )
        assert "code_challenge=abc123" in url
        assert "code_challenge_method=S256" in url


# ---------------------------------------------------------------------------
# exchange_code + id_token decode
# ---------------------------------------------------------------------------


class TestMicrosoftExchange:
    @pytest.mark.asyncio
    async def test_exchange_decodes_id_token_claims_into_raw(self, provider):
        id_token = _mint_id_token({
            "tid": "tenant-aaa", "oid": "object-bbb",
            "preferred_username": "alice@contoso.com",
            "aud": "my-app", "iss": "https://login.microsoftonline.com/",
        })
        mc = _mock_post({
            "access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
            "scope": "openid", "token_type": "Bearer",
            "id_token": id_token,
        })
        with patch(
            "auth.oauth_providers.microsoft.httpx.AsyncClient",
            return_value=mc,
        ):
            ts = await provider.exchange_code(
                code="c", redirect_uri="r", client_id="ci",
                client_secret="cs", code_verifier="verifier",
            )
        assert ts.access_token == "AT"
        # id_token claims flow into raw — persist_oauth_account copies raw
        # (minus access_token) into the file's extra block, so these
        # become ${account.extra.tenant_id} etc. in agent prompts.
        assert ts.raw["tenant_id"] == "tenant-aaa"
        assert ts.raw["object_id"] == "object-bbb"
        assert ts.raw["preferred_username"] == "alice@contoso.com"

    @pytest.mark.asyncio
    async def test_exchange_without_id_token_skips_decode(self, provider):
        mc = _mock_post({
            "access_token": "AT", "refresh_token": "RT",
            "expires_in": 3600, "scope": "openid", "token_type": "Bearer",
        })
        with patch(
            "auth.oauth_providers.microsoft.httpx.AsyncClient",
            return_value=mc,
        ):
            ts = await provider.exchange_code(
                code="c", redirect_uri="r", client_id="ci",
                client_secret="cs",
            )
        # No id_token in response → no tenant_id/object_id injected.
        assert "tenant_id" not in ts.raw
        assert "object_id" not in ts.raw


# ---------------------------------------------------------------------------
# normalize_token_response — the hosted-relay contract
# ---------------------------------------------------------------------------


class TestMicrosoftNormalize:
    """The id_token decode lives in ``normalize_token_response`` so the
    hosted-relay path — which re-runs ONLY the normalizer over the relay's
    verbatim vendor response (subclass ``exchange_code`` never executes) —
    still captures tenant_id / object_id / preferred_username."""

    def test_normalize_decodes_id_token_from_relay_raw(self, provider):
        id_token = _mint_id_token({
            "tid": "tenant-r", "oid": "object-r",
            "preferred_username": "carol@contoso.com",
        })
        relay_raw = {
            "access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
            "scope": "openid", "token_type": "Bearer",
            "id_token": id_token,
            "via_relay": True,  # relay marker rides along inside raw
        }
        ts = provider.normalize_token_response(relay_raw)
        assert ts.raw["tenant_id"] == "tenant-r"
        assert ts.raw["object_id"] == "object-r"
        assert ts.raw["preferred_username"] == "carol@contoso.com"
        assert ts.raw["via_relay"] is True

    def test_normalize_is_idempotent(self, provider):
        """Some call paths normalize an already-normalized raw (engine
        re-run over relay raw that exchange already decoded) — claims must
        come out identical, never duplicated or dropped."""
        id_token = _mint_id_token({"tid": "T1", "oid": "O1"})
        raw = {"access_token": "AT", "id_token": id_token}
        first = provider.normalize_token_response(raw)
        second = provider.normalize_token_response(first.raw)
        assert second.access_token == "AT"
        assert second.raw["tenant_id"] == "T1"
        assert second.raw["object_id"] == "O1"

    def test_normalize_without_id_token_injects_nothing(self, provider):
        ts = provider.normalize_token_response({"access_token": "AT"})
        assert "tenant_id" not in ts.raw
        assert "object_id" not in ts.raw
        assert "preferred_username" not in ts.raw


# ---------------------------------------------------------------------------
# refresh — re-persists rotated refresh + re-decodes id_token
# ---------------------------------------------------------------------------


class TestMicrosoftRefresh:
    @pytest.mark.asyncio
    async def test_refresh_preserves_previous_refresh_when_omitted(self, provider):
        """If vendor omits refresh_token in response, keep the caller's."""
        mc = _mock_post({
            "access_token": "AT2", "expires_in": 3600,
            "scope": "openid", "token_type": "Bearer",
            # No refresh_token field.
        })
        with patch(
            "auth.oauth_providers.microsoft.httpx.AsyncClient",
            return_value=mc,
        ):
            ts = await provider.refresh(
                refresh_token="old-RT", client_id="ci", client_secret="cs",
            )
        assert ts.access_token == "AT2"
        assert ts.refresh_token == "old-RT"  # preserved

    @pytest.mark.asyncio
    async def test_refresh_decodes_fresh_id_token(self, provider):
        """Microsoft includes a new id_token in refresh responses — re-decode
        so the file's extra stays current across token rotations."""
        new_id_token = _mint_id_token({
            "tid": "tenant-aaa", "oid": "object-bbb",
            "preferred_username": "alice-new@contoso.com",
        })
        mc = _mock_post({
            "access_token": "AT2", "refresh_token": "RT2",
            "expires_in": 3600, "scope": "openid", "token_type": "Bearer",
            "id_token": new_id_token,
        })
        with patch(
            "auth.oauth_providers.microsoft.httpx.AsyncClient",
            return_value=mc,
        ):
            ts = await provider.refresh(
                refresh_token="RT1", client_id="c", client_secret="s",
            )
        assert ts.raw["preferred_username"] == "alice-new@contoso.com"
        assert ts.raw["tenant_id"] == "tenant-aaa"


# ---------------------------------------------------------------------------
# fetch_userinfo — mail/upn fallback
# ---------------------------------------------------------------------------


class TestMicrosoftUserinfo:
    @pytest.mark.asyncio
    async def test_email_prefers_mail_when_present(self, provider):
        mc = _mock_get({
            "id": "obj-1",
            "displayName": "Alice Doe",
            "userPrincipalName": "alice@contoso.onmicrosoft.com",
            "mail": "alice@contoso.com",
        })
        with patch(
            "auth.oauth_providers.microsoft.httpx.AsyncClient",
            return_value=mc,
        ):
            ui = await provider.fetch_userinfo(access_token="AT")
        assert ui.email == "alice@contoso.com"  # mail wins
        assert ui.name == "Alice Doe"
        assert ui.account_id == "obj-1"

    @pytest.mark.asyncio
    async def test_email_falls_back_to_upn_when_mail_null(self, provider):
        mc = _mock_get({
            "id": "obj-2",
            "displayName": "Bob",
            "userPrincipalName": "bob@contoso.onmicrosoft.com",
            "mail": None,  # Microsoft returns null for users without a mailbox.
        })
        with patch(
            "auth.oauth_providers.microsoft.httpx.AsyncClient",
            return_value=mc,
        ):
            ui = await provider.fetch_userinfo(access_token="AT")
        assert ui.email == "bob@contoso.onmicrosoft.com"  # upn fallback


# ---------------------------------------------------------------------------
# build_admin_consent_url — Microsoft-only non-ABC method
# ---------------------------------------------------------------------------


class TestMicrosoftAdminConsent:
    def test_admin_consent_url_targets_tenant_endpoint(self, provider):
        url = provider.build_admin_consent_url(
            tenant_id="acme-uuid-1234",
            state="state-token",
            redirect_uri="https://x/cb",
            client_id="cid",
        )
        # Distinct from /authorize — /adminconsent is the tenant-wide
        # grant endpoint.
        assert "/acme-uuid-1234/v2.0/adminconsent?" in url
        assert "client_id=cid" in url
        assert "state=state-token" in url
        assert "redirect_uri=https%3A%2F%2Fx%2Fcb" in url
        # No scope param — scopes derive from app registration.
        assert "scope=" not in url


# ---------------------------------------------------------------------------
# Device code — tenant-scoped start, /common/ poll
# ---------------------------------------------------------------------------


class TestMicrosoftDeviceCode:
    @pytest.mark.asyncio
    async def test_start_device_code_uses_tenant_url(self, provider):
        """Device-code start hits /{tenant}/devicecode like authorize URL."""
        mc = _mock_post({
            "device_code": "DC", "user_code": "USER",
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900, "interval": 5,
        })
        with patch(
            "auth.oauth_providers.microsoft.httpx.AsyncClient",
            return_value=mc,
        ):
            payload = await provider.start_device_code(
                scopes=["openid"], client_id="cid",
                extra={"tenant_id": "acme-uuid"},
            )
        assert payload["device_code"] == "DC"
        # Confirm the POST URL was tenant-scoped.
        call = mc.post.call_args
        assert "/acme-uuid/oauth2/v2.0/devicecode" in call.args[0]

    @pytest.mark.asyncio
    async def test_poll_device_code_uses_common_token_url(self, provider):
        """Polling uses the class-level /common/ token endpoint — works
        for any tenant's device codes per Microsoft semantics."""
        id_token = _mint_id_token({"tid": "T", "oid": "O"})
        mc = _mock_post({
            "access_token": "AT", "refresh_token": "RT",
            "expires_in": 3600, "scope": "openid", "token_type": "Bearer",
            "id_token": id_token,
        })
        with patch(
            "auth.oauth_providers.microsoft.httpx.AsyncClient",
            return_value=mc,
        ):
            ts = await provider.poll_device_code(
                device_code="DC", client_id="c", client_secret="s",
            )
        assert ts is not None
        call = mc.post.call_args
        assert "/common/oauth2/v2.0/token" in call.args[0]
        # id_token decode runs on device-code success too.
        assert ts.raw["tenant_id"] == "T"
        assert ts.raw["object_id"] == "O"

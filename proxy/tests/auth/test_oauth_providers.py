"""OAuth provider tests.

Covers:
  * Provider registry lookup (hardcoded + manifest-derived)
  * GoogleOAuthProvider URL building + token exchange + refresh + revoke
  * GenericOAuthProvider construction from manifest fields
  * Refresh-token rotation safety (preserve old refresh when
    vendor omits)

HTTP calls are mocked via ``httpx.AsyncClient`` so tests are hermetic.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.oauth_providers import get_provider, list_provider_ids
from auth.oauth_providers.base import TokenSet, UserInfo
from auth.oauth_providers.google import GoogleOAuthProvider
from auth.oauth_providers.generic import GenericOAuthProvider


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestProviderRegistry:
    def test_google_hardcoded(self):
        p = get_provider("google")
        assert isinstance(p, GoogleOAuthProvider)

    def test_unknown_provider_raises(self):
        with pytest.raises(KeyError):
            get_provider("nonexistent-xyz")

    def test_list_provider_ids_includes_google(self):
        assert "google" in list_provider_ids()


# ---------------------------------------------------------------------------
# GoogleOAuthProvider
# ---------------------------------------------------------------------------


class TestGoogleProvider:
    @pytest.fixture
    def provider(self):
        return GoogleOAuthProvider()

    @pytest.mark.asyncio
    async def test_build_auth_url_includes_required_params(self, provider):
        url = await provider.build_auth_url(
            state="state-123",
            scopes=["scope1", "scope2"],
            redirect_uri="https://x/cb",
            client_id="cid-1",
        )
        # Google forces offline access + consent so we always get a refresh_token.
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        assert "state=state-123" in url
        assert "client_id=cid-1" in url
        assert "scope=scope1+scope2" in url

    @pytest.mark.asyncio
    async def test_build_auth_url_accepts_extra_params(self, provider):
        url = await provider.build_auth_url(
            state="s", scopes=["x"], redirect_uri="https://x", client_id="c",
            extra={"include_granted_scopes": "true"},
        )
        assert "include_granted_scopes=true" in url

    @pytest.mark.asyncio
    async def test_exchange_code_returns_token_set(self, provider):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "expires_in": 3600,
            "scope": "x y",
            "token_type": "Bearer",
        })
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch(
            "auth.oauth_providers.google.httpx.AsyncClient",
            return_value=mock_client,
        ):
            ts = await provider.exchange_code(
                code="auth-code", redirect_uri="https://x", client_id="c",
                client_secret="s",
            )

        assert isinstance(ts, TokenSet)
        assert ts.access_token == "at-1"
        assert ts.refresh_token == "rt-1"
        assert ts.expires_in == 3600

    @pytest.mark.asyncio
    async def test_exchange_code_raises_on_error(self, provider):
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json = MagicMock(return_value={
            "error": "invalid_grant",
            "error_description": "code expired",
        })
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        with patch(
            "auth.oauth_providers.google.httpx.AsyncClient",
            return_value=mock_client,
        ):
            with pytest.raises(RuntimeError, match="code expired"):
                await provider.exchange_code(
                    code="x", redirect_uri="r", client_id="c",
                    client_secret="s",
                )

    @pytest.mark.asyncio
    async def test_refresh_preserves_refresh_token_when_omitted(self, provider):
        """When Google omits refresh_token in the response, we
        preserve the previous one so we never lose it."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "access_token": "at-2",
            # refresh_token omitted!
            "expires_in": 3600,
        })
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        with patch(
            "auth.oauth_providers.google.httpx.AsyncClient",
            return_value=mock_client,
        ):
            ts = await provider.refresh(
                refresh_token="prev-rt", client_id="c", client_secret="s",
            )
        assert ts.access_token == "at-2"
        assert ts.refresh_token == "prev-rt"  # preserved!

    @pytest.mark.asyncio
    async def test_fetch_userinfo_maps_fields(self, provider):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "email": "alice@example.com",
            "name": "Alice",
            "sub": "google-sub-123",
        })
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        with patch(
            "auth.oauth_providers.google.httpx.AsyncClient",
            return_value=mock_client,
        ):
            ui = await provider.fetch_userinfo(access_token="at")
        assert isinstance(ui, UserInfo)
        assert ui.email == "alice@example.com"
        assert ui.name == "Alice"
        assert ui.account_id == "google-sub-123"

    @pytest.mark.asyncio
    async def test_revoke_returns_true_on_200(self, provider):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        with patch(
            "auth.oauth_providers.google.httpx.AsyncClient",
            return_value=mock_client,
        ):
            ok = await provider.revoke(
                token="rt", client_id="c", client_secret="s",
            )
        assert ok is True

    @pytest.mark.asyncio
    async def test_revoke_returns_false_never_raises(self, provider):
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(side_effect=Exception("network"))
        with patch(
            "auth.oauth_providers.google.httpx.AsyncClient",
            return_value=mock_client,
        ):
            # Best-effort: never raises.
            ok = await provider.revoke(
                token="rt", client_id="c", client_secret="s",
            )
        assert ok is False


# ---------------------------------------------------------------------------
# GenericOAuthProvider
# ---------------------------------------------------------------------------


class TestGenericProvider:
    @pytest.fixture
    def provider(self):
        return GenericOAuthProvider(
            provider_id="linear",
            authorization_url="https://linear.app/oauth/authorize",
            token_url="https://api.linear.app/oauth/token",
            revoke_url="https://api.linear.app/oauth/revoke",
            userinfo_url="https://api.linear.app/me",
            userinfo_email_field="email",
            userinfo_name_field="display_name",
            userinfo_id_field="id",
        )

    @pytest.mark.asyncio
    async def test_build_auth_url_standard_oauth2(self, provider):
        url = await provider.build_auth_url(
            state="s", scopes=["read", "write"],
            redirect_uri="https://x/cb", client_id="cid",
        )
        # No Google-specific access_type/prompt — pure OAuth 2.0.
        assert "access_type" not in url
        assert "prompt" not in url
        assert "client_id=cid" in url
        assert "scope=read+write" in url

    @pytest.mark.asyncio
    async def test_exchange_code_uses_manifest_token_url(self, provider):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "access_token": "linear-at",
            "expires_in": 7200,
        })
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        with patch(
            "auth.oauth_providers.generic.httpx.AsyncClient",
            return_value=mock_client,
        ):
            ts = await provider.exchange_code(
                code="c", redirect_uri="r", client_id="ci",
                client_secret="cs",
            )
        # Verify the POST went to the manifest-declared token_url
        call = mock_client.post.call_args
        assert call.args[0] == "https://api.linear.app/oauth/token"
        assert ts.access_token == "linear-at"

    @pytest.mark.asyncio
    async def test_fetch_userinfo_uses_custom_field_names(self, provider):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "email": "user@x.com",
            "display_name": "U",  # custom field name
            "id": "lin-123",      # custom field name
        })
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        with patch(
            "auth.oauth_providers.generic.httpx.AsyncClient",
            return_value=mock_client,
        ):
            ui = await provider.fetch_userinfo(access_token="at")
        assert ui.email == "user@x.com"
        assert ui.name == "U"
        assert ui.account_id == "lin-123"

    @pytest.mark.asyncio
    async def test_revoke_noop_when_url_missing(self):
        """A provider with no revoke_url should return False without raising."""
        p = GenericOAuthProvider(
            provider_id="ghost",
            authorization_url="https://ghost.test/auth",
            token_url="https://ghost.test/token",
            revoke_url="",  # explicitly missing
        )
        ok = await p.revoke(token="x", client_id="c", client_secret="s")
        assert ok is False


# ---------------------------------------------------------------------------
# credential_locks — concurrent serialization
# ---------------------------------------------------------------------------


class TestCredentialLocks:
    def test_get_lock_returns_same_lock_per_key(self):
        from core.credentials import credential_locks
        l1 = credential_locks.get_lock("u", "m", "a")
        l2 = credential_locks.get_lock("u", "m", "a")
        assert l1 is l2

    def test_different_keys_get_different_locks(self):
        from core.credentials import credential_locks
        l1 = credential_locks.get_lock("u", "m", "a")
        l2 = credential_locks.get_lock("u", "m", "b")
        l3 = credential_locks.get_lock("u", "n", "a")
        l4 = credential_locks.get_lock("v", "m", "a")
        assert l1 is not l2
        assert l1 is not l3
        assert l1 is not l4

    def test_discard_lock_frees_registry(self):
        from core.credentials import credential_locks
        l1 = credential_locks.get_lock("u-temp", "m", "a")
        n_before = credential_locks.active_lock_count()
        credential_locks.discard_lock("u-temp", "m", "a")
        n_after = credential_locks.active_lock_count()
        assert n_after == n_before - 1
        # Re-acquiring after discard creates a fresh lock.
        l2 = credential_locks.get_lock("u-temp", "m", "a")
        assert l2 is not l1

    @pytest.mark.asyncio
    async def test_lock_actually_serializes_async_tasks(self):
        """Two coroutines competing for the same lock should serialize."""
        import asyncio
        from core.credentials import credential_locks

        order = []

        async def section(label: str):
            async with credential_locks.get_lock("u-serial", "m", "a"):
                order.append(f"{label}-enter")
                await asyncio.sleep(0.01)
                order.append(f"{label}-exit")

        await asyncio.gather(section("A"), section("B"))
        # Either A...B or B...A, but never interleaved.
        assert order in (
            ["A-enter", "A-exit", "B-enter", "B-exit"],
            ["B-enter", "B-exit", "A-enter", "A-exit"],
        )

"""OAuthProvider ABC — vendor-neutral OAuth 2.x flow contract.

A concrete provider (e.g. ``GoogleOAuthProvider``) implements vendor-specific
quirks (forced ``prompt=consent``, custom userinfo endpoints, S2S vs user
OAuth branching for Zoom, Slack v2 URLs, etc.). Most providers can use
``GenericOAuthProvider`` which is fully manifest-driven.

The provider registry (``oauth_providers/__init__.py``) returns the right
instance for a ``provider_id`` at runtime. Callers should never instantiate
concrete classes directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TokenSet:
    """Vendor-neutral token bundle returned by ``exchange_code`` / ``refresh``.

    ``raw`` carries the vendor's untouched response in case a future caller
    needs a quirk-field (e.g. Slack's ``team_id``). Persistence layers
    should only persist the canonical fields below.
    """
    access_token: str
    refresh_token: str = ""
    expires_in: int = 3600
    scope: str = ""
    token_type: str = "Bearer"
    raw: dict = field(default_factory=dict)


@dataclass
class UserInfo:
    """Vendor-neutral identity claims returned by ``fetch_userinfo``."""
    email: str
    name: str = ""
    account_id: str = ""  # provider-stable id (Google sub, Slack user_id, etc.)
    raw: dict = field(default_factory=dict)


class OAuthProvider(ABC):
    """Vendor-neutral OAuth 2.x provider contract.

    Concrete providers are stateless — provider state (refresh tokens, app
    credentials) lives in storage. Methods are async; HTTP calls use
    ``httpx.AsyncClient``.
    """

    # ------------------------------------------------------------------
    # Required class-level metadata
    # ------------------------------------------------------------------

    #: Manifest-level identifier (``"google"``, ``"slack"``, ``"linear"``).
    provider_id: str = ""

    #: OAuth flow type. ``authorization_code`` is the only flow the
    #: runtime currently exercises; ``authorization_code_pkce``,
    #: ``device_code``, ``client_credentials``, and ``service_account``
    #: are reserved names for future providers.
    flow: str = "authorization_code"

    #: Vendor authorization endpoint (consent screen URL).
    authorization_url: str = ""

    #: Vendor token exchange endpoint.
    token_url: str = ""

    #: Optional revoke endpoint. Empty string means revocation unsupported
    #: (some vendors require dashboard-level revocation only).
    revoke_url: str = ""

    #: Optional userinfo endpoint. Empty string means the provider returns
    #: identity inside the token response (rare).
    userinfo_url: str = ""

    #: JSON key in the userinfo response for the account's primary email.
    userinfo_email_field: str = "email"

    #: JSON key for the account's display name.
    userinfo_name_field: str = "name"

    #: JSON key for the account's stable provider-side id.
    userinfo_id_field: str = "sub"

    # ------------------------------------------------------------------
    # Abstract surface
    # ------------------------------------------------------------------

    @abstractmethod
    async def build_auth_url(
        self,
        *,
        state: str,
        scopes: list[str],
        redirect_uri: str,
        client_id: str,
        extra: dict[str, str] | None = None,
    ) -> str:
        """Build the URL the user is redirected to for consent.

        ``extra`` carries optional provider-specific params
        (Google's ``access_type``/``prompt``, Slack's ``user_scope``,
        Microsoft's ``prompt=admin_consent``). Generic providers default
        to standard OAuth 2.0 params.
        """

    @abstractmethod
    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str,
        code_verifier: str | None = None,
    ) -> TokenSet:
        """Exchange an authorization code for tokens.

        ``code_verifier`` is required when ``flow == authorization_code_pkce``
        and ignored otherwise.
        """

    @abstractmethod
    async def refresh(
        self,
        *,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> TokenSet:
        """Exchange a refresh token for a fresh access token.

        Implementations MUST re-persist the access AND refresh tokens
        even when the response omits or repeats the previous refresh —
        some vendors rotate refresh tokens on every refresh — losing
        the rotated value strands the connection.
        Callers are responsible for the atomic write.
        """

    @abstractmethod
    async def fetch_userinfo(self, *, access_token: str) -> UserInfo:
        """Fetch the user's vendor-side identity."""

    @abstractmethod
    async def revoke(self, *, token: str, client_id: str, client_secret: str) -> bool:
        """Revoke an access or refresh token at the vendor.

        Best-effort: returns ``True`` on 2xx, ``False`` on any failure;
        never raises (callers always proceed with local cleanup).
        """

    # ------------------------------------------------------------------
    # Device code flow (Microsoft) — RFC 8628
    # ------------------------------------------------------------------

    async def start_device_code(
        self,
        *,
        scopes: list[str],
        client_id: str,
        extra: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Start a device-code grant. Returns vendor's start response.

        Standard response shape (RFC 8628):
            ``{device_code, user_code, verification_uri, expires_in, interval}``

        The client shows ``user_code`` + ``verification_uri`` to the user, who
        visits the URL on another device and enters the code. Meanwhile the
        client polls ``poll_device_code`` until success or expiry.

        Only required for providers declaring ``flow: "device_code"`` (or
        ``flows: [..., "device_code", ...]``). Default raises NotImplementedError.
        """
        raise NotImplementedError(
            f"{self.provider_id}: device_code flow not implemented"
        )

    async def poll_device_code(
        self,
        *,
        device_code: str,
        client_id: str,
        client_secret: str,
    ) -> TokenSet | None:
        """Poll a device-code grant once. Returns:

        * ``TokenSet`` on success
        * ``None`` on ``authorization_pending`` / ``slow_down`` (caller waits + retries)

        Raises ``RuntimeError`` on terminal failures (``expired_token``,
        ``access_denied``, ``invalid_grant``).

        Default raises NotImplementedError.
        """
        raise NotImplementedError(
            f"{self.provider_id}: device_code flow not implemented"
        )

    # ------------------------------------------------------------------
    # Client credentials flow (Zoom S2S) — RFC 6749 §4.4
    # ------------------------------------------------------------------

    async def exchange_client_credentials(
        self,
        *,
        client_id: str,
        client_secret: str,
        scopes: list[str],
        extra: dict[str, str] | None = None,
    ) -> TokenSet:
        """Exchange client credentials for an access token (no user dance).

        ``extra`` carries provider-specific params (Zoom's ``account_id``
        for S2S, etc.). Only required for providers declaring
        ``flow: "client_credentials"``. Default raises NotImplementedError.
        """
        raise NotImplementedError(
            f"{self.provider_id}: client_credentials flow not implemented"
        )

    # ------------------------------------------------------------------
    # Personal Access Token flow (GitHub) — no OAuth dance
    # ------------------------------------------------------------------

    async def exchange_personal_access_token(
        self,
        *,
        token: str,
        scopes: list[str],
    ) -> tuple[TokenSet, "UserInfo"]:
        """Persist a user-pasted PAT as if it were an OAuth access token.

        No vendor round-trip required for the "exchange" — the PAT IS the
        access token. We do call ``fetch_userinfo`` to validate the token
        and capture the account's email/name. Returns ``(TokenSet, UserInfo)``.

        Default implementation: wraps the PAT as a zero-expiry token (the
        refresh worker skips zero-expiry files) and fetches userinfo with
        the PAT as the bearer. Providers can override if userinfo requires
        a different auth scheme.
        """
        ts = TokenSet(
            access_token=token,
            refresh_token="",
            expires_in=0,  # signals "never expires" to refresh worker
            scope=" ".join(scopes),
            token_type="Bearer",
            raw={"flow": "personal_access_token"},
        )
        userinfo = await self.fetch_userinfo(access_token=token)
        return ts, userinfo

    # ------------------------------------------------------------------
    # Hook for vendor-specific token response quirks
    # ------------------------------------------------------------------

    def normalize_token_response(self, raw: dict[str, Any]) -> TokenSet:
        """Convert a raw vendor token response into a ``TokenSet``.

        Default implementation reads the standard OAuth 2.0 keys; override
        for vendors that wrap tokens or use non-standard field names.
        """
        return TokenSet(
            access_token=str(raw.get("access_token", "")),
            refresh_token=str(raw.get("refresh_token", "")),
            # Absent expires_in = never expires (GitHub OAuth apps, Notion,
            # Slack without rotation); 0 is the never-expires sentinel.
            expires_in=int(raw.get("expires_in", 0) or 0),
            scope=str(raw.get("scope", "")),
            token_type=str(raw.get("token_type", "Bearer")),
            raw=dict(raw),
        )

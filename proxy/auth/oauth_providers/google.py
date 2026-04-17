"""GoogleOAuthProvider — Google Workspace OAuth 2.0 implementation.

Implements vendor-specific quirks:
  * ``access_type=offline`` + ``prompt=consent`` always set so we get a
    refresh token on EVERY consent (Google omits it on subsequent grants
    otherwise).
  * Userinfo via ``oauth2/v3/userinfo``.
  * Revoke via ``oauth2.googleapis.com/revoke`` with the refresh token.

Generic OAuth flow code lives in ``services/oauth_engine``; this module
only owns Google-specific endpoint + parameter assembly.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

import httpx

from auth.oauth_providers.base import OAuthProvider, TokenSet, UserInfo

logger = logging.getLogger("claude-proxy.oauth-providers.google")


class GoogleOAuthProvider(OAuthProvider):
    """Google's OAuth 2.0 authorization_code flow with offline access."""

    provider_id = "google"
    flow = "authorization_code"
    authorization_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    revoke_url = "https://oauth2.googleapis.com/revoke"
    userinfo_url = "https://www.googleapis.com/oauth2/v3/userinfo"
    userinfo_email_field = "email"
    userinfo_name_field = "name"
    userinfo_id_field = "sub"

    async def build_auth_url(
        self,
        *,
        state: str,
        scopes: list[str],
        redirect_uri: str,
        client_id: str,
        extra: dict[str, str] | None = None,
    ) -> str:
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            # Always offline + consent — guarantees a refresh_token even
            # when re-consenting. Google's docs are explicit:
            # https://developers.google.com/identity/protocols/oauth2/web-server#offline
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        if extra:
            # Allow callers to layer on `include_granted_scopes=true` for
            # incremental scope grants (re-consent for added services).
            params.update(extra)
        return f"{self.authorization_url}?{urlencode(params)}"

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str,
        code_verifier: str | None = None,
    ) -> TokenSet:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.token_url,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        data = resp.json()
        if resp.status_code != 200 or "error" in data:
            err = data.get("error_description") or data.get("error") or str(data)
            raise RuntimeError(f"Google token exchange failed: {err}")
        return self.normalize_token_response(data)

    async def refresh(
        self,
        *,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> TokenSet:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.token_url,
                data={
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                },
            )
        data = resp.json()
        if resp.status_code != 200 or "error" in data:
            err = data.get("error_description") or data.get("error") or str(data)
            raise RuntimeError(f"Google token refresh failed: {err}")
        # Google's refresh response omits refresh_token when it hasn't
        # rotated — preserve the caller's previous one so writeback never
        # nulls the credential.
        ts = self.normalize_token_response(data)
        if not ts.refresh_token:
            ts.refresh_token = refresh_token
        return ts

    async def fetch_userinfo(self, *, access_token: str) -> UserInfo:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                self.userinfo_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"Google userinfo failed: HTTP {resp.status_code}")
        data = resp.json()
        return UserInfo(
            email=str(data.get(self.userinfo_email_field, "")),
            name=str(data.get(self.userinfo_name_field, "")),
            account_id=str(data.get(self.userinfo_id_field, "")),
            raw=data,
        )

    async def revoke(self, *, token: str, client_id: str, client_secret: str) -> bool:
        # Google revoke ignores client_id/secret in v1 — token alone is enough.
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    self.revoke_url,
                    params={"token": token},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            if resp.status_code == 200:
                logger.info("Google token revoked successfully")
                return True
            logger.warning("Google token revocation returned %d", resp.status_code)
            return False
        except Exception as e:
            logger.warning("Google token revocation failed: %s", e)
            return False

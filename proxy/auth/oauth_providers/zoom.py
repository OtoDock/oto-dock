"""ZoomOAuthProvider — Zoom OAuth 2.0 with S2S branching.

Supports two flows:

* ``authorization_code`` — user-OAuth (per-user account, user-scope sessions).
  Standard OAuth dance against ``zoom.us/oauth/{authorize,token}``.
* ``client_credentials`` — Server-to-Server OAuth (admin-connected service
  account, agent-scope sessions). Zoom S2S is a non-standard variant of
  RFC 6749 §4.4: it requires an ``account_id`` query/body param on the
  token exchange. The framework's generic implementation already accepts
  arbitrary ``extra`` params (engine passes ``extra={"account_id": ...}``
  per the admin's S2S form input).

The S2S ``account_id`` is admin-paste — Zoom's token response does NOT
echo it back. It's persisted to the file's ``extra`` block at the
``s2s_exchange`` API route, NOT by overriding ``normalize_token_response``
here (so refresh-worker re-exchange doesn't accidentally drop it when
the re-exchange response also lacks ``account_id``).

Recording-scope tools are user-OAuth-only at the vendor —
``cloud_recording:read:list_recording_files`` is unavailable via S2S per
Zoom docs. Manifest marks those services ``requires_user_oauth: true``
so the ``has_service_credentials_only`` filter hides them when
the user has no user-OAuth account connected.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

import httpx

from auth.oauth_providers.base import OAuthProvider, TokenSet, UserInfo

logger = logging.getLogger("claude-proxy.oauth-providers.zoom")


class ZoomOAuthProvider(OAuthProvider):
    """Zoom OAuth 2.0 — authorization_code (user) + client_credentials (S2S)."""

    provider_id = "zoom"
    flow = "authorization_code"  # Default; client_credentials picked via flow param
    authorization_url = "https://zoom.us/oauth/authorize"
    token_url = "https://zoom.us/oauth/token"
    revoke_url = "https://zoom.us/oauth/revoke"
    userinfo_url = "https://api.zoom.us/v2/users/me"
    userinfo_email_field = "email"
    userinfo_name_field = "display_name"
    userinfo_id_field = "id"

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
            "state": state,
        }
        # NO scope param. Zoom "general apps" use GRANULAR scopes configured
        # on the marketplace app itself — consent grants exactly that set,
        # and Zoom's documented authorize URL carries no scope param. Sending
        # the manifest's classic-style names (meeting:read) against a
        # granular-scope app risks an invalid-scope refusal. The `scopes`
        # argument is intentionally unused (ABC contract).
        if extra:
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
        # Zoom uses HTTP Basic auth on the token endpoint (per OAuth 2.0
        # §2.3.1), not body params for client credentials.
        data = {
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.token_url,
                data=data,
                auth=(client_id, client_secret),
            )
        payload = resp.json()
        if resp.status_code != 200 or "error" in payload:
            err = payload.get("reason") or payload.get("error") or str(payload)
            raise RuntimeError(f"Zoom token exchange failed: {err}")
        return self.normalize_token_response(payload)

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
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                auth=(client_id, client_secret),
            )
        payload = resp.json()
        if resp.status_code != 200 or "error" in payload:
            err = payload.get("reason") or payload.get("error") or str(payload)
            raise RuntimeError(f"Zoom token refresh failed: {err}")
        ts = self.normalize_token_response(payload)
        # Zoom rotates refresh tokens — preserve previous if vendor omits.
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
            raise RuntimeError(f"Zoom userinfo failed: HTTP {resp.status_code}")
        data = resp.json()
        return UserInfo(
            email=str(data.get(self.userinfo_email_field, "")),
            name=str(data.get(self.userinfo_name_field, "")),
            account_id=str(data.get(self.userinfo_id_field, "")),
            raw=data,
        )

    async def revoke(self, *, token: str, client_id: str, client_secret: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    self.revoke_url,
                    data={"token": token},
                    auth=(client_id, client_secret),
                )
            if resp.status_code == 200 and resp.json().get("status") == "success":
                logger.info("Zoom token revoked successfully")
                return True
            logger.warning(
                "Zoom token revocation returned status=%d body=%s",
                resp.status_code, resp.text[:200],
            )
            return False
        except Exception as e:
            logger.warning("Zoom token revocation failed: %s", e)
            return False

    async def exchange_client_credentials(
        self,
        *,
        client_id: str,
        client_secret: str,
        scopes: list[str],
        extra: dict[str, str] | None = None,
    ) -> TokenSet:
        """Zoom Server-to-Server OAuth.

        Non-standard: requires ``account_id`` as a query/body param
        identifying the Zoom account whose admin generated the S2S app
        credentials. Caller (``api/auth/oauth.py::s2s_exchange``) passes it
        via ``extra={"account_id": "..."}``.

        Zoom S2S response does NOT echo ``account_id`` back. The persist
        site at the API route is responsible for injecting it into the
        token file's ``extra`` block — we don't do it here so the
        refresh-worker re-exchange (which calls this method) doesn't
        clobber the previously-persisted value with an empty one.
        """
        data: dict[str, str] = {
            "grant_type": "account_credentials",
        }
        if extra and extra.get("account_id"):
            data["account_id"] = extra["account_id"]
        # Zoom doesn't accept scope on S2S exchange — scopes are configured
        # on the marketplace app itself. Ignore the parameter.
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.token_url,
                data=data,
                auth=(client_id, client_secret),
            )
        payload = resp.json()
        if resp.status_code != 200 or "error" in payload:
            err = payload.get("reason") or payload.get("error") or str(payload)
            raise RuntimeError(
                f"Zoom S2S exchange failed: {err}"
            )
        return self.normalize_token_response(payload)

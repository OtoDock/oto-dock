"""FacebookOAuthProvider — Meta (Facebook Login for Business) OAuth 2.0.

One provider id serves facebook-mcp AND instagram-mcp: Instagram's Graph API
is served from graph.facebook.com under the same user access token, so both
MCPs share one grant + token file (the second-MCP-same-provider rule).

Vendor quirks this class owns:
  * Meta issues NO refresh_token. A long-lived user token (~60 days)
    re-exchanges ITSELF while still valid (``grant_type=fb_exchange_token``),
    so ``normalize_token_response`` stores the access token in the refresh
    slot. The refresh worker's standard arm then works unchanged — hosted and
    self-managed alike (the relay's facebook descriptor speaks the same
    grant), and every refresh ROTATES: the new token becomes the next
    refresh credential.
  * ``exchange_code`` upgrades the short-lived (~1 h) auth-code token to the
    long-lived form immediately. FAIL-OPEN: if the upgrade errors, the
    short-lived token ships and the refresh worker performs the identical
    upgrade within the hour.
  * Scopes are COMMA-joined on the consent URL (Meta documents commas only).
  * Errors come NESTED (``{"error": {"code": 190, …}}``); code 190 is Meta's
    invalid/expired-token class → raise with ``invalid_grant`` in the message
    so the refresh worker's permanent-failure check trips instead of
    retrying a dead grant forever.
  * Userinfo via Graph ``/me?fields=id,name,email`` — ``email`` requires the
    email permission and is often absent, so the profile NAME substitutes as
    the account label (the numeric Graph id would make an unreadable one).
  * Revoke = ``DELETE /me/permissions`` with the user token (de-authorizes
    the app for that user; Meta has no client-credential revoke endpoint).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

import httpx

from auth.oauth_providers.base import OAuthProvider, TokenSet, UserInfo

logger = logging.getLogger("claude-proxy.oauth-providers.facebook")

GRAPH_VERSION = "v26.0"


def _graph_error(data: dict[str, Any], action: str) -> RuntimeError:
    """Meta nests errors; surface a message the worker can classify. Code 190
    (OAuthException: invalid/expired token) maps to the canonical
    ``invalid_grant`` marker ``_is_permanent_refresh_error`` looks for."""
    err = data.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        detail = err.get("message") or err.get("type") or "error"
        if code == 190:
            return RuntimeError(
                f"Facebook {action} failed: invalid_grant ({detail})"
            )
        return RuntimeError(f"Facebook {action} failed: {detail} (code={code})")
    return RuntimeError(f"Facebook {action} failed: {err or data}")


class FacebookOAuthProvider(OAuthProvider):
    """Meta's OAuth 2.0 authorization_code flow with long-lived user tokens."""

    provider_id = "facebook"
    flow = "authorization_code"
    supports_incremental_auth = False
    authorization_url = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"
    token_url = f"https://graph.facebook.com/{GRAPH_VERSION}/oauth/access_token"
    revoke_url = f"https://graph.facebook.com/{GRAPH_VERSION}/me/permissions"
    userinfo_url = f"https://graph.facebook.com/{GRAPH_VERSION}/me"
    userinfo_email_field = "email"
    userinfo_name_field = "name"
    userinfo_id_field = "id"

    def normalize_token_response(self, raw: dict[str, Any]) -> TokenSet:
        """Standard mapping, plus: the access token doubles as the refresh
        credential (Meta issues no refresh_token — see the module docstring).
        Runs on BOTH lanes: self-managed exchanges here, and the hosted lane
        where the engine re-normalizes the relay's verbatim vendor response."""
        ts = super().normalize_token_response(raw)
        if not ts.refresh_token and ts.access_token:
            ts.refresh_token = ts.access_token
        return ts

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
            # Meta documents (and only reliably accepts) comma separation.
            "scope": ",".join(scopes),
            "state": state,
        }
        if extra:
            # Room for a Facebook-Login-for-Business `config_id` should the
            # dialog ever demand configurations instead of classic scopes.
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
            raise _graph_error(data, "token exchange")
        ts = self.normalize_token_response(data)
        # Upgrade short-lived (~1 h) → long-lived (~60 d) right away. The
        # upgrade is the same call refresh() makes; FAIL-OPEN on error (the
        # refresh worker repeats it within the hour — the grant stays valid).
        try:
            ts = await self.refresh(
                refresh_token=ts.access_token,
                client_id=client_id,
                client_secret=client_secret,
            )
        except Exception as exc:
            logger.warning(
                "Facebook long-lived upgrade failed (shipping the short-lived "
                "token; the refresh worker will retry): %s", exc,
            )
        return ts

    async def refresh(
        self,
        *,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> TokenSet:
        """Re-exchange the current long-lived token for a fresh one. The
        presented token must still be valid — which is why the facebook
        manifests raise the refresh worker's min_remaining window far above
        the 5-minute default."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.token_url,
                data={
                    "grant_type": "fb_exchange_token",
                    "fb_exchange_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
        data = resp.json()
        if resp.status_code != 200 or "error" in data:
            raise _graph_error(data, "token refresh")
        # normalize_token_response rotates: the NEW token becomes the next
        # refresh credential.
        return self.normalize_token_response(data)

    async def fetch_userinfo(self, *, access_token: str) -> UserInfo:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                self.userinfo_url,
                params={"fields": "id,name,email"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
        data = resp.json()
        if resp.status_code != 200 or "error" in data:
            raise _graph_error(data, "userinfo")
        email = str(data.get(self.userinfo_email_field) or "")
        name = str(data.get(self.userinfo_name_field) or "")
        # `email` needs the email permission and is often absent — fall back
        # to the profile NAME so the account label (and token filename) stays
        # human-readable instead of a numeric Graph id.
        return UserInfo(
            email=email or name,
            name=name,
            account_id=str(data.get(self.userinfo_id_field) or ""),
            raw=data,
        )

    async def revoke(self, *, token: str, client_id: str, client_secret: str) -> bool:
        # DELETE /me/permissions de-authorizes the app for this user — the
        # user token itself is the credential (no client secret involved).
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.delete(
                    self.revoke_url,
                    params={"access_token": token},
                )
            if resp.status_code == 200:
                logger.info("Facebook app authorization revoked")
                return True
            logger.warning("Facebook revoke returned %d", resp.status_code)
            return False
        except Exception as e:
            logger.warning("Facebook revoke failed: %s", e)
            return False

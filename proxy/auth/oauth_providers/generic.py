"""GenericOAuthProvider — pure manifest-driven OAuth 2.x.

For plain OAuth 2.0 providers (Linear, Notion, GitHub, anything without
custom request-shape quirks), the framework constructs a
``GenericOAuthProvider`` directly from the MCP's manifest. No Python
subclass needed.

This provider:
  * Reads ``authorization_url``, ``token_url``, ``userinfo_url``,
    ``revoke_url`` from the manifest.
  * Uses standard OAuth 2.0 request shapes (POST with form-encoded body).
  * Calls ``normalize_token_response`` to convert vendor responses.
  * Implements the standards-compliant versions of device_code (RFC 8628)
    and client_credentials (RFC 6749 §4.4) flows — providers can override
    via subclass for vendor quirks.

If a vendor's flow has a quirk (Slack's v2 endpoints, Microsoft's admin
consent prompt, Zoom's S2S branching), write a Python subclass in this
package alongside ``google.py``.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

import httpx

from auth.oauth_providers.base import OAuthProvider, TokenSet, UserInfo

logger = logging.getLogger("claude-proxy.oauth-providers.generic")


class GenericOAuthProvider(OAuthProvider):
    """Manifest-driven OAuth provider.

    Construct with the URLs + field names from the MCP's manifest.
    No vendor-specific overrides.
    """

    def __init__(
        self,
        *,
        provider_id: str,
        authorization_url: str,
        token_url: str,
        revoke_url: str = "",
        userinfo_url: str = "",
        userinfo_email_field: str = "email",
        userinfo_name_field: str = "name",
        userinfo_id_field: str = "sub",
        userinfo_headers: dict[str, str] | None = None,
        userinfo_method: str = "GET",
        userinfo_body: dict | None = None,
        flow: str = "authorization_code",
        device_authorization_url: str = "",
    ):
        self.provider_id = provider_id
        self.authorization_url = authorization_url
        self.token_url = token_url
        self.revoke_url = revoke_url
        self.userinfo_url = userinfo_url
        self.userinfo_email_field = userinfo_email_field
        self.userinfo_name_field = userinfo_name_field
        self.userinfo_id_field = userinfo_id_field
        # Manifest-declared static headers for the userinfo request (e.g.
        # Notion's mandatory `Notion-Version`). Authorization always ours.
        self.userinfo_headers = dict(userinfo_headers or {})
        # Identity-probe request shape. Vendors without a REST userinfo
        # endpoint (Linear is GraphQL-only) declare POST + a static JSON
        # body in the manifest; dotted userinfo_*_field paths walk the
        # nested response (`data.viewer.email`).
        self.userinfo_method = (userinfo_method or "GET").upper()
        self.userinfo_body = dict(userinfo_body or {})
        self.flow = flow
        self.device_authorization_url = device_authorization_url

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
            "state": state,
        }
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
        data = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(self.token_url, data=data)
        payload = resp.json()
        if resp.status_code != 200 or "error" in payload:
            err = payload.get("error_description") or payload.get("error") or str(payload)
            raise RuntimeError(f"{self.provider_id} token exchange failed: {err}")
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
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                },
            )
        payload = resp.json()
        if resp.status_code != 200 or "error" in payload:
            err = payload.get("error_description") or payload.get("error") or str(payload)
            raise RuntimeError(f"{self.provider_id} token refresh failed: {err}")
        ts = self.normalize_token_response(payload)
        # Refresh-token rotation safety: preserve previous refresh
        # if vendor's response omits it.
        if not ts.refresh_token:
            ts.refresh_token = refresh_token
        return ts

    async def fetch_userinfo(self, *, access_token: str) -> UserInfo:
        if not self.userinfo_url:
            raise RuntimeError(
                f"{self.provider_id}: userinfo_url not declared in manifest"
            )
        headers = {
            **self.userinfo_headers,
            "Authorization": f"Bearer {access_token}",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            if self.userinfo_method == "POST":
                resp = await client.post(
                    self.userinfo_url, headers=headers, json=self.userinfo_body,
                )
            else:
                resp = await client.get(self.userinfo_url, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(
                f"{self.provider_id} userinfo failed: HTTP {resp.status_code}"
            )
        data = resp.json()
        # JSON `null` -> Python None. `str(None)` would give the literal
        # "None" string (truthy!) which silently poisons account_label
        # and any downstream `if not email` fallback. Convert null to ""
        # so the not-empty checks fire correctly.
        def _field(key: str) -> str:
            v = data.get(key)
            if v is None and "." in key:
                # Dotted path → walk nested objects (e.g. Notion's
                # `bot.owner.user.person.email`). A literal flat key with
                # dots, if a vendor ever has one, wins above.
                cur: Any = data
                for part in key.split("."):
                    cur = cur.get(part) if isinstance(cur, dict) else None
                    if cur is None:
                        break
                v = cur
            if isinstance(v, (dict, list)):
                return ""
            return str(v) if v else ""
        return UserInfo(
            email=_field(self.userinfo_email_field),
            name=_field(self.userinfo_name_field),
            account_id=_field(self.userinfo_id_field),
            raw=data,
        )

    async def revoke(self, *, token: str, client_id: str, client_secret: str) -> bool:
        if not self.revoke_url:
            # Vendor doesn't expose a revoke endpoint — best-effort: treat
            # as a no-op and let local cleanup proceed.
            logger.info(
                "%s: revoke_url not declared; skipping vendor revoke",
                self.provider_id,
            )
            return False
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    self.revoke_url,
                    data={
                        "token": token,
                        "client_id": client_id,
                        "client_secret": client_secret,
                    },
                )
            if 200 <= resp.status_code < 300:
                logger.info("%s token revoked successfully", self.provider_id)
                return True
            logger.warning(
                "%s token revocation returned %d", self.provider_id, resp.status_code,
            )
            return False
        except Exception as e:
            logger.warning("%s token revocation failed: %s", self.provider_id, e)
            return False

    # ------------------------------------------------------------------
    # RFC 8628 — Device Authorization Grant
    # ------------------------------------------------------------------

    async def start_device_code(
        self,
        *,
        scopes: list[str],
        client_id: str,
        extra: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self.device_authorization_url:
            raise RuntimeError(
                f"{self.provider_id}: device_authorization_url not declared in manifest"
            )
        data: dict[str, str] = {
            "client_id": client_id,
            "scope": " ".join(scopes),
        }
        if extra:
            data.update(extra)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(self.device_authorization_url, data=data)
        payload = resp.json()
        if resp.status_code != 200 or "error" in payload:
            err = payload.get("error_description") or payload.get("error") or str(payload)
            raise RuntimeError(f"{self.provider_id} device code start failed: {err}")
        return payload

    async def poll_device_code(
        self,
        *,
        device_code: str,
        client_id: str,
        client_secret: str,
    ) -> TokenSet | None:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.token_url,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
        payload = resp.json()
        # Pending state — caller waits + retries.
        if resp.status_code != 200:
            err = (payload.get("error") or "").lower()
            if err in ("authorization_pending", "slow_down"):
                return None
            err_desc = payload.get("error_description") or err or str(payload)
            raise RuntimeError(f"{self.provider_id} device code poll failed: {err_desc}")
        return self.normalize_token_response(payload)

    # ------------------------------------------------------------------
    # RFC 6749 §4.4 — Client Credentials Grant
    # ------------------------------------------------------------------

    async def exchange_client_credentials(
        self,
        *,
        client_id: str,
        client_secret: str,
        scopes: list[str],
        extra: dict[str, str] | None = None,
    ) -> TokenSet:
        data: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if scopes:
            data["scope"] = " ".join(scopes)
        if extra:
            data.update(extra)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(self.token_url, data=data)
        payload = resp.json()
        if resp.status_code != 200 or "error" in payload:
            err = payload.get("error_description") or payload.get("error") or str(payload)
            raise RuntimeError(
                f"{self.provider_id} client credentials exchange failed: {err}"
            )
        return self.normalize_token_response(payload)

"""MicrosoftOAuthProvider — Microsoft Entra ID (Azure AD) OAuth 2.0 + PKCE.

Stateless singleton — `tenant_id` is passed by the caller via the ``extra``
dict on every per-tenant operation (``build_auth_url``, ``start_device_code``,
``build_admin_consent_url``). Token endpoints (``/token``, device-code poll)
use the tenant-agnostic ``/common/`` URL because Microsoft accepts tokens
from any tenant at ``/common/``; the tenant restriction is enforced at the
*authorize* endpoint where the user enters credentials.

Caller responsibility (``api/auth/oauth.py``): resolve ``tenant_id`` from the
admin's ``MS_TENANT_ID`` infra credential (defaults to ``"common"`` when
unset) and inject ``extra["tenant_id"]`` before each call. Multi-tenant
deploys leave it at ``"common"``; single-tenant deploys override.

Identity capture: the token response's ``id_token`` (JWT) is decoded inside
``normalize_token_response`` and its ``tid`` / ``oid`` /
``preferred_username`` claims are injected into ``TokenSet.raw``. Living in
the normalizer (not the exchange methods) means EVERY path captures them —
exchange / refresh / device-code-poll, and crucially the hosted-relay path,
where the engine re-runs only the normalizer over the relay's raw response
(the subclass ``exchange_code`` never executes). From there the claims flow
into the persisted token file's ``extra`` block via ``persist_oauth_account``
and become addressable as ``${account.extra.tenant_id}`` /
``${account.extra.preferred_username}`` in agent_context templates.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

import httpx
import jwt

from auth.oauth_providers.base import OAuthProvider, TokenSet, UserInfo

logger = logging.getLogger("claude-proxy.oauth-providers.microsoft")


class MicrosoftOAuthProvider(OAuthProvider):
    """Microsoft Entra ID OAuth 2.0 + PKCE provider."""

    provider_id = "microsoft"
    flow = "authorization_code_pkce"

    # Token endpoints are tenant-agnostic — `/common/` accepts tokens
    # issued for any tenant. Used for exchange_code, refresh,
    # poll_device_code.
    authorization_url = (
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    )
    token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    device_authorization_url = (
        "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
    )

    # Userinfo via Microsoft Graph — tenant-agnostic.
    userinfo_url = "https://graph.microsoft.com/v1.0/me"
    userinfo_email_field = "userPrincipalName"  # override in fetch_userinfo (mail fallback)
    userinfo_name_field = "displayName"
    userinfo_id_field = "id"  # Graph object id == JWT `oid` for home tenant

    # Microsoft has no standard token-revoke endpoint. Disconnect is
    # local-only — the token simply expires.
    revoke_url = ""

    # ------------------------------------------------------------------
    # Tenant-aware URL helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _authorize_url_for(tenant_id: str) -> str:
        return (
            f"https://login.microsoftonline.com/{tenant_id or 'common'}"
            "/oauth2/v2.0/authorize"
        )

    @staticmethod
    def _devicecode_url_for(tenant_id: str) -> str:
        return (
            f"https://login.microsoftonline.com/{tenant_id or 'common'}"
            "/oauth2/v2.0/devicecode"
        )

    @staticmethod
    def _adminconsent_url_for(tenant_id: str) -> str:
        return (
            f"https://login.microsoftonline.com/{tenant_id or 'common'}"
            "/v2.0/adminconsent"
        )

    # ------------------------------------------------------------------
    # ABC overrides
    # ------------------------------------------------------------------

    def normalize_token_response(self, raw: dict) -> TokenSet:
        # The id_token decode lives HERE (not in exchange_code/refresh)
        # because the hosted-relay path re-runs only the normalizer over
        # the relay's verbatim vendor response — subclass exchange methods
        # never execute there. Idempotent: re-decoding injects the same
        # claims.
        ts = super().normalize_token_response(raw)
        self._decode_id_token_into_raw(raw, ts.raw)
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
        # tenant_id resolved by caller from MS_TENANT_ID infra cred (defaults
        # to "common" when unset). Single-tenant deploys override.
        tenant_id = (extra or {}).get("tenant_id") or "common"
        params: dict[str, str] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
        }
        if extra:
            for k, v in extra.items():
                # tenant_id selects the URL, not a query param.
                if k == "tenant_id":
                    continue
                params[k] = v
        return f"{self._authorize_url_for(tenant_id)}?{urlencode(params)}"

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
            raise RuntimeError(f"Microsoft token exchange failed: {err}")
        # normalize_token_response decodes the id_token claims into raw.
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
            raise RuntimeError(f"Microsoft token refresh failed: {err}")
        # normalize_token_response re-decodes the refresh response's fresh
        # id_token, so claims stay current across token rotations.
        ts = self.normalize_token_response(payload)
        # Microsoft rotates refresh tokens — but if vendor omits, preserve
        # the previous one so writeback never nulls the credential.
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
            raise RuntimeError(
                f"Microsoft userinfo failed: HTTP {resp.status_code}"
            )
        data = resp.json()
        # `mail` is null for users without a mailbox (rare; some app-only
        # service principals). Fall back to userPrincipalName which is
        # always populated for human users.
        email = str(data.get("mail") or data.get("userPrincipalName") or "")
        return UserInfo(
            email=email,
            name=str(data.get(self.userinfo_name_field, "")),
            account_id=str(data.get(self.userinfo_id_field, "")),
            raw=data,
        )

    async def revoke(self, *, token: str, client_id: str, client_secret: str) -> bool:
        # Microsoft has no standard OAuth revoke endpoint. Disconnect
        # cleans up locally; the access token simply expires.
        logger.info("microsoft: no revoke endpoint — local cleanup only")
        return False

    async def start_device_code(
        self,
        *,
        scopes: list[str],
        client_id: str,
        extra: dict[str, str] | None = None,
    ) -> dict:
        # Device-code start is tenant-scoped (mirrors the authorize URL —
        # the user enters credentials on the verification_uri, and tenant
        # restriction is enforced there).
        tenant_id = (extra or {}).get("tenant_id") or "common"
        data: dict[str, str] = {
            "client_id": client_id,
            "scope": " ".join(scopes),
        }
        if extra:
            for k, v in extra.items():
                if k == "tenant_id":
                    continue
                data[k] = v
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self._devicecode_url_for(tenant_id), data=data,
            )
        payload = resp.json()
        if resp.status_code != 200 or "error" in payload:
            err = payload.get("error_description") or payload.get("error") or str(payload)
            raise RuntimeError(
                f"Microsoft device code start failed: {err}"
            )
        return payload

    async def poll_device_code(
        self,
        *,
        device_code: str,
        client_id: str,
        client_secret: str,
    ) -> TokenSet | None:
        # Polling against /common/ works for any tenant's device codes.
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
        if resp.status_code != 200:
            err = (payload.get("error") or "").lower()
            if err in ("authorization_pending", "slow_down"):
                return None
            err_desc = (
                payload.get("error_description") or err or str(payload)
            )
            raise RuntimeError(
                f"Microsoft device code poll failed: {err_desc}"
            )
        # normalize_token_response decodes the id_token claims into raw.
        return self.normalize_token_response(payload)

    # ------------------------------------------------------------------
    # Microsoft-specific — tenant-wide admin consent
    # ------------------------------------------------------------------

    def build_admin_consent_url(
        self,
        *,
        tenant_id: str,
        state: str,
        redirect_uri: str,
        client_id: str,
    ) -> str:
        """Build the tenant-admin consent URL.

        Distinct from the standard authorize endpoint with
        ``prompt=admin_consent`` — that path only forces the consent UX
        for the user's home tenant. The ``/{tenant}/v2.0/adminconsent``
        endpoint performs a tenant-wide grant for all scopes registered
        on the OAuth app. Callback shape is also different:
        ``?admin_consent=True&tenant=<guid>&state=<token>`` (no ``code``).
        """
        params = {
            "client_id": client_id,
            "state": state,
            "redirect_uri": redirect_uri,
        }
        return f"{self._adminconsent_url_for(tenant_id)}?{urlencode(params)}"

    # ------------------------------------------------------------------
    # id_token decode (helper)
    # ------------------------------------------------------------------

    def _decode_id_token_into_raw(
        self, payload: dict, raw: dict,
    ) -> None:
        """Decode the response's id_token and inject identity claims into raw.

        Signature verification is disabled (decision 21): the token comes
        from a TLS-verified endpoint (``login.microsoftonline.com``), so
        a JWKS round-trip is redundant defense-in-depth. We only read the
        identity claims; trust is anchored at the TLS layer.

        The injected keys (``tenant_id``, ``object_id``, ``preferred_username``)
        flow into the persisted token file's ``extra`` block via
        ``persist_oauth_account`` (which copies ``token_set.raw`` minus
        ``access_token`` into the file ``extra``). Available downstream as
        ``${account.extra.tenant_id}`` etc. in agent_context templates.
        """
        id_token = payload.get("id_token", "")
        if not id_token:
            return
        try:
            claims = jwt.decode(
                id_token,
                options={"verify_signature": False, "verify_aud": False},
            )
        except jwt.PyJWTError as e:
            logger.warning(
                "microsoft: failed to decode id_token: %s", e,
            )
            return
        if claims.get("tid"):
            raw["tenant_id"] = str(claims["tid"])
        if claims.get("oid"):
            raw["object_id"] = str(claims["oid"])
        if claims.get("preferred_username"):
            raw["preferred_username"] = str(claims["preferred_username"])

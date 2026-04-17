"""Generic OIDC authentication provider.

Replaces the Authentik-specific code with a generic OIDC implementation
that works with any OIDC provider (Authentik, Authelia, Keycloak, Okta, etc.).
"""

import logging

import httpx

import config
from auth.providers.base import AuthProvider, AuthResult

logger = logging.getLogger("claude-proxy")


class OIDCAuthProvider(AuthProvider):
    """OpenID Connect authentication provider."""

    async def authenticate(self, request_data: dict) -> AuthResult:
        """Authenticate via OIDC code exchange.

        request_data: {"code": str, "redirect_uri": str | None}
        """
        code = request_data.get("code", "")
        redirect_uri = request_data.get("redirect_uri")

        # Exchange code for tokens
        try:
            tokens = await self._exchange_code(code, redirect_uri)
        except httpx.HTTPStatusError as e:
            logger.error(f"OIDC token exchange failed: {e}")
            return AuthResult(success=False, error="Authentication failed",
                              error_code="token_exchange_failed")
        except Exception as e:
            logger.error(f"OIDC token exchange error: {e}")
            return AuthResult(success=False, error="Authentication failed",
                              error_code="token_exchange_failed")

        access_token = tokens.get("access_token")
        if not access_token:
            return AuthResult(success=False, error="No access token received",
                              error_code="no_access_token")

        # Fetch user info
        try:
            userinfo = await self._fetch_userinfo(access_token)
        except Exception as e:
            logger.error(f"OIDC userinfo fetch failed: {e}")
            return AuthResult(success=False, error="Failed to fetch user info",
                              error_code="userinfo_failed")

        sub = userinfo.get("sub", "")
        email = userinfo.get("email", "")
        name = userinfo.get("preferred_username") or userinfo.get("name", email)
        display_name = userinfo.get("name") or ""
        if not display_name:
            given = userinfo.get("given_name", "")
            family = userinfo.get("family_name", "")
            display_name = f"{given} {family}".strip()

        groups = userinfo.get("groups", [])
        role = self._extract_role(groups)
        if not role:
            return AuthResult(
                success=False,
                error=f"Access denied: not a member of any configured group. "
                      f"Expected one of: {', '.join(config.OIDC_ROLE_GROUPS.keys())}",
                error_code="no_group",
            )

        provider_slug = config.OIDC_PROVIDER_NAME.lower().replace(" ", "-")
        return AuthResult(
            success=True,
            sub=sub,
            email=email,
            name=name,
            display_name=display_name,
            role=role,
            auth_provider=f"oidc:{provider_slug}",
        )

    def get_login_url(self, *, redirect_uri: str | None = None,
                      mobile: bool = False) -> str | None:
        """Build OIDC authorization URL."""
        if not config.OIDC_AUTHORIZE_URL or not config.OIDC_CLIENT_ID:
            return None

        from urllib.parse import urlencode

        from auth.providers import create_oauth_state
        state = create_oauth_state(redirect_uri=redirect_uri)

        actual_redirect = redirect_uri or config.OIDC_REDIRECT_URI
        params = {
            "response_type": "code",
            "client_id": config.OIDC_CLIENT_ID,
            "redirect_uri": actual_redirect,
            "scope": config.OIDC_SCOPES,
            "state": state,
        }
        return f"{config.OIDC_AUTHORIZE_URL}?{urlencode(params)}"

    def get_logout_url(self, post_redirect: str | None = None) -> str | None:
        """Return OIDC provider logout URL."""
        url = config.OIDC_LOGOUT_URL
        if not url:
            return None
        if post_redirect:
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}post_logout_redirect_uri={post_redirect}"
        return url

    async def _exchange_code(self, code: str, redirect_uri: str | None = None) -> dict:
        """Exchange authorization code for tokens."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                config.OIDC_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri or config.OIDC_REDIRECT_URI,
                    "client_id": config.OIDC_CLIENT_ID,
                    "client_secret": config.OIDC_CLIENT_SECRET,
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def _fetch_userinfo(self, access_token: str) -> dict:
        """Fetch user profile from OIDC userinfo endpoint."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                config.OIDC_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _extract_role(groups: list[str]) -> str | None:
        """Map OIDC groups to role. Highest-priority wins."""
        best_role = None
        best_priority = 999
        for group in groups:
            role = config.OIDC_ROLE_GROUPS.get(group)
            if role and config.ROLE_PRIORITY.get(role, 999) < best_priority:
                best_role = role
                best_priority = config.ROLE_PRIORITY[role]
        return best_role

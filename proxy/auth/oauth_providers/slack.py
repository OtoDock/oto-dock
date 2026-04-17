"""SlackOAuthProvider — Slack ``v2_user`` OAuth (the mcp.slack.com flow).

mcp.slack.com only accepts bearers minted by Slack's USER-token flow
(verified against its RFC 8414 metadata — ``mcp.slack.com/.well-known/
oauth-authorization-server``), NOT classic ``oauth/v2/authorize`` bot
installs:

* Consent  : ``slack.com/oauth/v2_user/authorize`` — ONE standard ``scope``
  param of user scopes (no bot/``user_scope`` split). The app must still
  declare a bot user + ≥1 bot scope or this endpoint silently refuses
  ("doesn't have a bot user to install").
* Exchange : ``slack.com/api/oauth.v2.user.access`` — the user token
  (``xoxp-…``) is at the ROOT of the response; ``authed_user`` carries only
  ``{id, scope}``. Errors come back as HTTP 200 + ``ok: false``.
* Identity : ``auth.test`` returns handle + team but NO email — we enrich
  via ``users.info`` (needs ``users:read.email``), else synthesize
  ``handle@workspace-domain`` so account labels stay readable and unique.

``normalize_token_response`` also still flattens the classic nested v2 shape
(``authed_user.access_token`` → ``extra.user_token`` + ``preferred_bearer``)
so pre-existing token files and any classic-flow exchange keep working.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from auth.oauth_providers.base import OAuthProvider, TokenSet, UserInfo

logger = logging.getLogger("claude-proxy.oauth-providers.slack")


class SlackOAuthProvider(OAuthProvider):
    """Slack ``v2_user`` OAuth — user tokens accepted by mcp.slack.com."""

    provider_id = "slack"
    flow = "authorization_code"
    authorization_url = "https://slack.com/oauth/v2_user/authorize"
    token_url = "https://slack.com/api/oauth.v2.user.access"
    revoke_url = "https://slack.com/api/auth.revoke"
    userinfo_url = "https://slack.com/api/auth.test"
    userinfo_email_field = ""  # Slack doesn't return email at auth.test
    userinfo_name_field = "user"
    userinfo_id_field = "user_id"

    async def build_auth_url(
        self,
        *,
        state: str,
        scopes: list[str],
        redirect_uri: str,
        client_id: str,
        extra: dict[str, str] | None = None,
    ) -> str:
        params: dict[str, str] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
        # v2_user takes one standard `scope` param; omit when empty rather
        # than sending `scope=` (Slack rejects an empty value).
        if scopes:
            params["scope"] = " ".join(scopes)
        # Classic-flow compat: extra["user_scopes"] (string or list) becomes
        # the dual `user_scope` param. Nothing sets it on the v2_user path.
        if extra:
            user_scopes_raw = extra.get("user_scopes", "")
            if isinstance(user_scopes_raw, list):
                user_scope = " ".join(user_scopes_raw)
            else:
                user_scope = str(user_scopes_raw)
            if user_scope:
                params["user_scope"] = user_scope
            for k, v in extra.items():
                if k == "user_scopes":
                    continue
                params[k] = v
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
                },
            )
        payload = resp.json()
        if not payload.get("ok", False):
            err = payload.get("error", str(payload))
            raise RuntimeError(f"Slack token exchange failed: {err}")
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
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
        payload = resp.json()
        if not payload.get("ok", False):
            err = payload.get("error", str(payload))
            raise RuntimeError(f"Slack token refresh failed: {err}")
        ts = self.normalize_token_response(payload)
        if not ts.refresh_token:
            ts.refresh_token = refresh_token
        return ts

    async def fetch_userinfo(self, *, access_token: str) -> UserInfo:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                self.userinfo_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Slack auth.test failed: HTTP {resp.status_code}")
            data = resp.json()
            if not data.get("ok", False):
                raise RuntimeError(
                    f"Slack auth.test failed: {data.get('error', 'unknown')}"
                )

            # auth.test has no email. Best-effort users.info enrichment
            # (profile.email needs users:read.email); else synthesize
            # handle@workspace-domain — readable AND unique across the
            # workspaces one platform user may connect.
            email = ""
            user_id = str(data.get("user_id", ""))
            try:
                info = await client.post(
                    "https://slack.com/api/users.info",
                    headers={"Authorization": f"Bearer {access_token}"},
                    data={"user": user_id},
                )
                if info.status_code == 200:
                    payload = info.json()
                    if payload.get("ok"):
                        profile = (payload.get("user") or {}).get("profile") or {}
                        email = str(profile.get("email") or "")
            except Exception:
                pass
        if not email:
            # url is "https://<domain>.slack.com/"
            domain = urlparse(str(data.get("url", ""))).hostname or ""
            handle = str(data.get("user", ""))
            if handle and domain:
                email = f"{handle}@{domain}"

        return UserInfo(
            email=email,
            name=str(data.get("user", "")),
            account_id=str(data.get("user_id", "")),
            raw=data,
        )

    async def revoke(self, *, token: str, client_id: str, client_secret: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    self.revoke_url,
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code == 200 and resp.json().get("revoked", False):
                logger.info("Slack token revoked successfully")
                return True
            logger.warning(
                "Slack token revocation returned status=%d body=%s",
                resp.status_code, resp.text[:200],
            )
            return False
        except Exception as e:
            logger.warning("Slack token revocation failed: %s", e)
            return False

    def normalize_token_response(self, raw: dict[str, Any]) -> TokenSet:
        """Flatten Slack's response so persisted ``extra.*`` keys are
        addressable from ``agent_context`` templates (``${account.extra.team_id}``)
        and the bearer injector (``extra.preferred_bearer``).

        The engine writes every key of ``TokenSet.raw`` except ``access_token``
        into the file's ``extra`` block (oauth_account_store.persist_oauth_account
        line 264). So flattening here is the contract.

        Handles BOTH shapes: ``oauth.v2.user.access`` (the mcp.slack.com
        flow — user token at the ROOT, ``authed_user`` = ``{id, scope}``
        only) and classic ``oauth.v2.access`` (bot token at the root, full
        user grant nested under ``authed_user``).
        """
        enhanced = dict(raw)

        team = raw.get("team") or {}
        if isinstance(team, dict):
            if team.get("id"):
                enhanced["team_id"] = str(team["id"])
            if team.get("name"):
                enhanced["team_name"] = str(team["name"])

        enterprise = raw.get("enterprise") or {}
        if isinstance(enterprise, dict) and enterprise.get("id"):
            enhanced["enterprise_id"] = str(enterprise["id"])

        scope = str(raw.get("scope", ""))
        authed_user = raw.get("authed_user") or {}
        if isinstance(authed_user, dict):
            if authed_user.get("id"):
                enhanced["user_id"] = str(authed_user["id"])
            if not scope and authed_user.get("scope"):
                # v2_user reports the granted scopes under authed_user.
                scope = str(authed_user["scope"])
            if authed_user.get("access_token"):
                # Classic v2: root = bot token; the user grant is nested.
                # preferred_bearer routes the user token to mcp.slack.com.
                enhanced["user_token"] = str(authed_user["access_token"])
                enhanced["user_scope"] = str(authed_user.get("scope", ""))
                enhanced["preferred_bearer"] = "user_token"

        return TokenSet(
            access_token=str(raw.get("access_token", "")),
            refresh_token=str(raw.get("refresh_token", "")),
            # Slack returns `expires_in` only when token rotation is enabled
            # on the app. Treat 0 as "never expires" so the refresh worker
            # skips this file.
            expires_in=int(raw.get("expires_in", 0) or 0),
            scope=scope,
            token_type="Bearer",
            raw=enhanced,
        )

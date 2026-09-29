"""Anthropic OAuth glue for the Claude Code engine — the vendor half of the
subscription seam. What the platform does with a login (which account, when
to rotate, the lock, the backoff, the fan-out) is the pool's; what
Anthropic's shapes are — the PKCE flow the Claude Code CLI runs, the token
and profile endpoints, the credential file the CLI reads — is here.

The PKCE flow, as the CLI implements it:
1. Generate PKCE params (code_verifier, code_challenge)
2. Build the authorization URL → the user authenticates in a browser popup
3. Anthropic's callback page displays an authorization code
4. The user pastes the code back into the dashboard
5. The exchange trades the code for tokens (access + refresh)
6. The tokens are stored encrypted in the DB as a subscription

Token refresh is ``refresh`` below, called by the pool's rotation chokepoint
(``subscription_pool._refresh_oauth_token``) through
``CLIExecutionLayer.refresh_oauth``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import time
import urllib.parse

import httpx

from core.execution_layer import OAuthRefresh

logger = logging.getLogger(__name__)

# Anthropic OAuth endpoints (the same the Claude Code CLI uses, v2.1.97+).
AUTH_URL = "https://platform.claude.com/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
# The beta the CLI itself sends on every OAuth request.
OAUTH_BETA = "oauth-2025-04-20"

# All scopes from the CLI (union of console + claude.ai scopes)
SCOPES = "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
# The one scope inference needs. The authorization server DOWNGRADES the
# grant for an Anthropic Console (API) account: it answers the request above
# with `org:create_api_key user:file_upload user:profile` and
# `subscriptionType: api_individual` — a "create an API key" grant that
# Claude Code treats as not logged in.
INFERENCE_SCOPE = "user:inference"
_CONSOLE_SUBSCRIPTION_TYPES = frozenset({"api_individual"})

# Claude's 401 recovery polls the credentials file for a rotated token for up
# to this long before giving up (CLAUDE_CODE_OAUTH_401_WAIT_MS). Set on REMOTE
# sessions only: a satellite's file lands after a WS round-trip, so a request
# racing the push needs the poll window; local writes are synchronous.
REMOTE_401_WAIT_MS = 20_000


# ---------------------------------------------------------------------------
# The login flow (PKCE)
# ---------------------------------------------------------------------------

def grant_refusal(scopes: list[str], subscription_type: str) -> str:
    """Why a freshly exchanged login grant cannot serve Claude Code, or ``""``.

    Refuses a grant whose reported scopes lack :data:`INFERENCE_SCOPE`, and a
    grant that reports no scopes at all but a Console subscription type. A
    grant that reports neither is trusted — the exchange must keep working if
    the token endpoint ever stops sending the ``scope`` field. The reason is
    the message the dashboard shows under the code box, so it names what was
    granted and the two things that do work.
    """
    granted = [s for s in (scopes or []) if s]
    sub_type = (subscription_type or "").strip()
    if granted and INFERENCE_SCOPE in granted:
        return ""
    if not granted and sub_type not in _CONSOLE_SUBSCRIPTION_TYPES:
        return ""
    what = (
        f"scopes {', '.join(granted)}" if granted else "no inference scope"
    )
    if sub_type:
        what += f"; subscription type {sub_type}"
    return (
        "This Anthropic account granted Console (API) access only "
        f"({what}), not a Claude subscription — Claude Code would answer "
        "every turn with \"Not logged in\". Connect an account that has a "
        "Claude subscription, or add this account's API key as an API-key "
        "credential instead."
    )


def generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256).

    Returns (code_verifier, code_challenge).
    """
    verifier_bytes = os.urandom(32)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode()

    challenge_hash = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(challenge_hash).rstrip(b"=").decode()

    return code_verifier, code_challenge


def build_auth_url(code_challenge: str, state: str) -> str:
    """Build the Anthropic OAuth authorization URL."""
    params = {
        "code": "true",
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str, code_verifier: str, state: str = "") -> dict:
    """Exchange an authorization code for tokens.

    Uses JSON body matching the Claude Code CLI (not form-urlencoded).

    Returns the token response dict with:
    - access_token, refresh_token, expires_in
    - scope, subscriptionType, rateLimitTier

    Raises ValueError on failure.
    """
    import json as _json
    import re

    # Sanitize: strip whitespace, non-printable chars, and URL fragments.
    # The Anthropic callback page URL may include a '#' fragment (e.g.
    # ?code=REAL_CODE#fragment) — strip '#' and everything after it.
    code = code.strip()
    if '#' in code:
        code = code[:code.index('#')]
    code = re.sub(r'\s+', '', code)

    json_body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": code_verifier,
    }
    if state:
        json_body["state"] = state

    raw_body = _json.dumps(json_body, separators=(',', ':'))

    # Send compact JSON (no spaces), matching the Claude Code CLI's
    # JSON.stringify() output.
    resp = httpx.post(
        TOKEN_URL,
        content=raw_body.encode(),
        headers={
            "Content-Type": "application/json",
        },
        timeout=15,
    )

    if resp.status_code != 200:
        resp_body = resp.text[:500]
        logger.error(f"Claude OAuth token exchange failed: {resp.status_code} {resp_body}")
        try:
            err = resp.json()
            if isinstance(err.get("error"), dict):
                detail = err["error"].get("message", resp_body)
            else:
                detail = err.get("message") or err.get("error_description") or err.get("error") or resp_body
        except Exception:
            detail = resp_body
        raise ValueError(detail)

    return resp.json()


# ---------------------------------------------------------------------------
# The account profile (plan tier + identity)
# ---------------------------------------------------------------------------

def derive_subscription_fields(profile: dict) -> tuple[str, str]:
    """Map ``/api/oauth/profile`` JSON to the ``(subscriptionType,
    rateLimitTier)`` pair of ``.credentials.json``. The Claude Code TUI gates
    plan-included models (e.g. Fable 5 on Max) on ``subscriptionType`` — an
    empty value makes it classify the login as API/credits and refuse them."""
    account = profile.get("account") or {}
    org = profile.get("organization") or {}
    if account.get("has_claude_max"):
        sub_type = "max"
    elif account.get("has_claude_pro"):
        sub_type = "pro"
    else:
        # organization_type is "claude_<tier>" (claude_max, claude_enterprise, …)
        sub_type = str(org.get("organization_type") or "").removeprefix("claude_")
    return sub_type, str(org.get("rate_limit_tier") or "")


def fetch_profile(access_token: str) -> dict | None:
    """The ``/api/oauth/profile`` document for an Anthropic OAuth access token,
    or ``None`` when the endpoint did not answer 200 (a revoked token, an
    egress rule that passes the token host but not this one, a timeout).
    Synchronous: callers on the event loop run it in a thread. The document
    carries the account (``account.uuid``, ``account.email``, the plan flags)
    and the organization (``organization_type``, ``rate_limit_tier``)."""
    try:
        resp = httpx.get(
            PROFILE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "anthropic-beta": OAUTH_BETA,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            logger.info("Anthropic profile endpoint answered %s", resp.status_code)
            return None
        body = resp.json()
        return body if isinstance(body, dict) else None
    except Exception as e:
        logger.info("Anthropic profile endpoint unreachable: %s", e)
        return None


def fetch_subscription_fields(access_token: str) -> tuple[str, str]:
    """Best-effort plan-tier lookup for an Anthropic OAuth access token.

    The token endpoint does not echo the plan tier, so it is resolved from the
    profile endpoint the CLI itself uses. Returns ``("", "")`` on any failure —
    callers treat that as "keep whatever is stored"."""
    profile = fetch_profile(access_token)
    if profile is None:
        return "", ""
    return derive_subscription_fields(profile)


# ---------------------------------------------------------------------------
# Token refresh (the vendor call; the pool owns the policy around it)
# ---------------------------------------------------------------------------

def _json_or_none(resp) -> dict | None:
    try:
        body = resp.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def refresh(refresh_token: str, stored: dict) -> OAuthRefresh:
    """One refresh against Anthropic's token endpoint → the new ``oauth_token``
    record (the vendor-shaped part: token, refresh token, expiry, scopes, the
    plan tier), or the failure's status and body for the pool to classify.

    JSON body matching the Claude Code CLI (not form-urlencoded); the scope
    parameter is OMITTED — the CLI omits it for Claude.ai (inference) tokens,
    and scopes not in the original grant (e.g. org:create_api_key) cause
    'invalid_scope' errors. The refresh response does not echo the plan tier,
    so the stored values are preserved (overwriting them with "" strips the
    tier on every 8h rotation, and the TUI then gates Max-included models
    behind usage credits) and backfilled once from the profile endpoint when
    the stored values are empty too. Never raises.
    """
    try:
        resp = httpx.post(
            TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            },
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
    except Exception as e:
        return OAuthRefresh(error=str(e))
    if resp.status_code != 200:
        return OAuthRefresh(status=resp.status_code, body=_json_or_none(resp))
    data = _json_or_none(resp) or {}
    new_access = data.get("access_token")
    if not new_access:
        return OAuthRefresh(status=200, body=data, error="no access_token in the response")
    old = stored.get("oauth_token") if isinstance(stored.get("oauth_token"), dict) else {}
    sub_type = data.get("subscriptionType") or old.get("subscriptionType") or ""
    rate_tier = data.get("rateLimitTier") or old.get("rateLimitTier") or ""
    if not sub_type:
        sub_type, fetched_tier = fetch_subscription_fields(new_access)
        rate_tier = rate_tier or fetched_tier
    expires_in = data.get("expires_in", 28800)
    return OAuthRefresh(
        oauth_token={
            "accessToken": new_access,
            "refreshToken": data.get("refresh_token", refresh_token),
            "expiresAt": int((time.time() + expires_in) * 1000),
            "scopes": data.get("scope", "").split() if data.get("scope") else [],
            "subscriptionType": sub_type,
            "rateLimitTier": rate_tier,
        },
        refresh_token_expires_in=data.get("refresh_token_expires_in"),
    )


# ---------------------------------------------------------------------------
# The credential file the CLI reads
# ---------------------------------------------------------------------------

def credentials_file(access_token: str, expires_at_ms: int, stored: dict) -> dict:
    """The content of a session's ``.credentials.json`` — the CLI's
    ``claudeAiOauth`` schema — for ``access_token``, with the grant metadata
    from the stored ``oauth_token`` and the refresh token NEUTRALIZED (blank):
    the pool is the sole rotator — a CLI holding no refresh token physically
    cannot rotate, it can only use the fanned-out access token or 401-recover
    it from disk, which fails SAFE (an auth error repaired by the next
    fan-out) instead of cascading revocations. The Claude Code TUI gates
    plan-included models on ``subscriptionType``, so it rides along."""
    oauth = stored.get("oauth_token") if isinstance(stored.get("oauth_token"), dict) else {}
    blob = {
        "accessToken": access_token,
        "refreshToken": "",
        "expiresAt": int(expires_at_ms or 0),
        "scopes": oauth.get("scopes") or [],
        "subscriptionType": oauth.get("subscriptionType", ""),
        "rateLimitTier": oauth.get("rateLimitTier", ""),
    }
    # Grant-lifetime expiry rides along when known (CLI ≥2.1.222 stores and
    # warns on it; older CLIs ignore the extra key). Only when present — a
    # zero would read as an epoch-expired login.
    if oauth.get("refreshTokenExpiresAt"):
        blob["refreshTokenExpiresAt"] = int(oauth["refreshTokenExpiresAt"])
    return {"claudeAiOauth": blob}

"""Claude Code OAuth2 PKCE flow.

Implements the same OAuth flow as the Claude Code CLI:
1. Generate PKCE params (code_verifier, code_challenge)
2. Build authorization URL → user authenticates in browser popup
3. Anthropic's callback page displays an authorization code
4. User pastes the code back into our UI
5. Backend exchanges the code for tokens (access + refresh)
6. Tokens stored encrypted in DB as a subscription

Token refresh is handled by subscription_pool._refresh_oauth_token().
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import urllib.parse

logger = logging.getLogger(__name__)

# Anthropic OAuth endpoints (same as the Claude Code CLI)
AUTH_URL = "https://platform.claude.com/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"

# All scopes from the CLI (union of console + claude.ai scopes)
SCOPES = "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"


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
    import httpx
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

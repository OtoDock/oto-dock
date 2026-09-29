"""OpenAI / ChatGPT OAuth glue for the Codex engine — the vendor half of the
subscription seam.

The live login is a device-auth flow (``api/auth/openai_oauth.py`` shells out
to ``codex login --device-auth`` and stores the ``auth.json`` it writes as the
subscription's ``codex_auth_blob``); ``refresh`` below is the token refresh
the pool's rotation chokepoint calls through
``CodexCLIExecutionLayer.refresh_oauth``. The token endpoint and the public
client id come from the Codex CLI source (``codex-rs/login/src/server.rs``);
the client id is public, not a secret.
"""

from __future__ import annotations

import copy
import logging
import time
import urllib.parse
from datetime import datetime, timezone

import requests

from core.execution_layer import OAuthRefresh

logger = logging.getLogger(__name__)

TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def _json_or_none(resp) -> dict | None:
    try:
        body = resp.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def refresh(refresh_token: str, stored: dict) -> OAuthRefresh:
    """One refresh against OpenAI's token endpoint (form-encoded, as the CLI
    sends it) → the new ``oauth_token`` record plus the stored
    ``codex_auth_blob`` with its ``tokens`` brought in step (the blob is what
    every session's ``auth.json`` is built from, so its access and refresh
    tokens must follow the rotation), or the failure's status and body for
    the pool to classify. Never raises."""
    try:
        resp = requests.post(
            TOKEN_URL,
            data=urllib.parse.urlencode({
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
            }),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
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
    new_refresh = data.get("refresh_token", refresh_token)
    expires_in = data.get("expires_in", 28800)
    extra: dict = {}
    blob = stored.get("codex_auth_blob")
    if isinstance(blob, dict) and isinstance(blob.get("tokens"), dict):
        blob = copy.deepcopy(blob)
        blob["tokens"]["access_token"] = new_access
        if new_refresh:
            blob["tokens"]["refresh_token"] = new_refresh
        blob["last_refresh"] = datetime.now(timezone.utc).isoformat()
        extra["codex_auth_blob"] = blob
    return OAuthRefresh(
        oauth_token={
            "accessToken": new_access,
            "refreshToken": new_refresh,
            "expiresAt": int((time.time() + expires_in) * 1000),
        },
        extra=extra,
        refresh_token_expires_in=data.get("refresh_token_expires_in"),
    )

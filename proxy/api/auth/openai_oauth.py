"""OpenAI OAuth REST API — device code flow via `codex login --device-auth`.

Flow:
1. POST /v1/oauth/openai/start → spawns `codex login --device-auth` on server,
   returns {url, user_code, login_id}
2. User opens the verification URL on any device and enters the code
3. GET /v1/oauth/openai/status → polls until codex login writes ~/.codex/auth.json
4. POST /v1/oauth/openai/finish → reads auth.json, stores as encrypted subscription

Works from everywhere (Android, desktop, remote) — no localhost redirect needed.
For per-user subscriptions: same flow but stored with owner_type="user".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.providers import get_current_user, require_auth, UserContext
from services.engines import subscription_pool
from storage import subscription_store
import config as app_config

logger = logging.getLogger(__name__)
router = APIRouter()

# Active login sessions: login_id → {proc, user_sub, owner_type, layer, started_at, ...}
_active_logins: dict[str, dict] = {}

# Path to Codex auth.json (written by `codex login`)
_AUTH_JSON_PATH = Path.home() / ".codex" / "auth.json"

# Strip ANSI escape codes from codex output
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class OAuthStartRequest(BaseModel):
    layer: str = "codex-cli"
    owner_type: str = "platform"  # 'platform' (admin) or 'user'


class OAuthFinishRequest(BaseModel):
    login_id: str
    layer: str = "codex-cli"
    label: str = ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/v1/oauth/openai/start")
async def oauth_start(
    req: OAuthStartRequest,
    user: UserContext = Depends(get_current_user),
):
    """Start OpenAI device code auth. Returns verification URL + user code.

    Spawns `codex login --device-auth` on the server, parses the one-time
    code and verification URL from stdout.
    """
    user = require_auth(user)
    if req.owner_type == "platform" and user.role != "admin":
        raise HTTPException(403, "Admin required for platform subscriptions")

    # Clean up any stale login sessions
    _cleanup_stale_logins()

    # Kill any active login process
    for lid, meta in _active_logins.items():
        if meta["proc"].returncode is None:
            try:
                meta["proc"].terminate()
                await asyncio.sleep(0.5)
                if meta["proc"].returncode is None:
                    meta["proc"].kill()
            except Exception:
                pass
    _active_logins.clear()

    # Remove existing auth.json so we can detect when a new one is written
    if _AUTH_JSON_PATH.exists():
        _AUTH_JSON_PATH.unlink()

    # Spawn codex login --device-auth via node directly (the codex binary
    # is a Node.js script and systemd services have minimal PATH)
    codex_bin = getattr(app_config, "CODEX_BIN", "codex")
    codex_resolved = os.path.realpath(codex_bin)
    # Resolve node from PATH — its location varies by install (bare-metal apt
    # = /usr/bin/node, the container image = /usr/local/bin/node); keep the
    # apt path as the fallback for systemd's minimal PATH.
    node_bin = shutil.which("node") or "/usr/bin/node"
    logger.info(
        f"codex device-auth: binary={codex_bin}, resolved={codex_resolved}, "
        f"node={node_bin}"
    )
    spawn_env = {**os.environ, "BROWSER": "echo"}
    proc = await asyncio.create_subprocess_exec(
        node_bin, codex_resolved, "login", "--device-auth",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=spawn_env,
    )

    # Read stdout to extract verification URL and user code
    verification_url = ""
    user_code = ""
    all_output = []
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=3,
                )
            except asyncio.TimeoutError:
                # If we already have both values, stop reading
                if verification_url and user_code:
                    break
                continue
            if not line:
                break
            raw = line.decode("utf-8", errors="replace").strip()
            all_output.append(raw)
            # Strip ANSI codes for parsing
            clean = _ANSI_RE.sub("", raw)
            logger.info(f"codex device-auth: {clean[:200]}")

            # Extract verification URL (e.g., https://auth.openai.com/codex/device)
            if not verification_url:
                url_match = re.search(r"(https://\S+)", clean)
                if url_match:
                    verification_url = url_match.group(1)

            # Extract user code — alphanumeric with dash, on its own line
            # Format observed: KI02-SGF8G (4-5 chars, dash, 4-5 chars)
            if not user_code and verification_url:
                code_match = re.match(r"^\s*([A-Z0-9]{3,6}-[A-Z0-9]{3,6})\s*$", clean)
                if code_match:
                    user_code = code_match.group(1)

            if verification_url and user_code:
                break
    except Exception as e:
        logger.error(f"Error reading codex device-auth output: {e}")

    if not verification_url or not user_code:
        clean_output = [_ANSI_RE.sub("", l) for l in all_output]
        logger.error(
            f"codex device-auth: missing url={bool(verification_url)} "
            f"code={bool(user_code)}. Output: {clean_output[:10]}"
        )
        try:
            proc.terminate()
        except Exception:
            pass
        raise HTTPException(
            500,
            "Failed to start device code auth — could not extract code. "
            "Make sure device code login is enabled in your ChatGPT Security Settings.",
        )

    login_id = secrets.token_urlsafe(16)
    _active_logins[login_id] = {
        "proc": proc,
        "user_sub": user.sub,
        "owner_type": req.owner_type,
        "layer": req.layer,
        "started_at": time.monotonic(),
    }

    logger.info(f"OpenAI device-auth started (id={login_id[:8]}, code_received={bool(user_code)})")
    return {"url": verification_url, "user_code": user_code, "login_id": login_id}


@router.get("/v1/oauth/openai/status")
async def oauth_status(
    login_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Poll whether the codex login process has completed."""
    user = require_auth(user)
    meta = _active_logins.get(login_id)
    if not meta:
        raise HTTPException(404, "Login session not found")
    if meta["user_sub"] != user.sub:
        raise HTTPException(403, "Not your login session")

    proc = meta["proc"]
    if proc.returncode is not None:
        # Process exited — check if auth.json was written
        if _AUTH_JSON_PATH.exists():
            return {"status": "completed"}
        return {"status": "failed", "message": "Login process exited without writing credentials"}

    # Still running — check if auth.json appeared (process might still be cleaning up)
    if _AUTH_JSON_PATH.exists():
        return {"status": "completed"}

    # Device codes expire after 15 minutes
    if time.monotonic() - meta["started_at"] > 900:
        try:
            proc.terminate()
        except Exception:
            pass
        return {"status": "failed", "message": "Device code expired (15 minutes). Please try again."}

    return {"status": "pending"}


@router.post("/v1/oauth/openai/finish")
async def oauth_finish(
    req: OAuthFinishRequest,
    user: UserContext = Depends(get_current_user),
):
    """Read auth.json and store as subscription. Call after status=completed."""
    user = require_auth(user)
    meta = _active_logins.pop(req.login_id, None)
    if not meta:
        raise HTTPException(404, "Login session not found or already finished")
    if meta["user_sub"] != user.sub:
        _active_logins[req.login_id] = meta  # put it back
        raise HTTPException(403, "Not your login session")
    if meta["owner_type"] == "platform" and user.role != "admin":
        raise HTTPException(403, "Admin required for platform subscriptions")

    # Kill the process if still running
    proc = meta["proc"]
    if proc.returncode is None:
        try:
            proc.terminate()
            await asyncio.sleep(1)
            if proc.returncode is None:
                proc.kill()
        except Exception:
            pass

    # Read auth.json
    if not _AUTH_JSON_PATH.exists():
        raise HTTPException(400, "No credentials found — login may have failed")

    try:
        auth_data = json.loads(_AUTH_JSON_PATH.read_text())
    except Exception as e:
        raise HTTPException(400, f"Failed to read credentials: {e}")

    # Extract tokens — auth.json format may vary:
    # Could be {"access_token": "...", "refresh_token": "..."} directly
    # or nested under a "tokens" key
    access_token = (
        auth_data.get("access_token")
        or auth_data.get("tokens", {}).get("access_token")
        or ""
    )
    refresh_token = (
        auth_data.get("refresh_token")
        or auth_data.get("tokens", {}).get("refresh_token")
        or ""
    )

    if not access_token:
        raise HTTPException(400, "No access token found in credentials file")

    # Build credential data in our standard format
    credential_data = {
        "oauth_token": {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "expiresAt": int((time.time() + 28800) * 1000),  # 8 hour default
        },
        # Store full Codex auth.json structure for session auth.json reconstruction.
        # Codex CLI requires id_token, account_id, etc. alongside access_token.
        "codex_auth_blob": auth_data,
    }

    label = req.label or "ChatGPT (subscription)"
    # The connector owns the account (admin for 'platform', the user for 'user');
    # a 'platform' connect also contributes it to the agent pool. The admin gate
    # above ensures only an admin can request owner_type='platform'. Flags are set
    # on CREATE only — reconnect just refreshes tokens.
    is_platform = meta["owner_type"] == "platform"
    owner_sub = meta["user_sub"]
    # Admins' personal connects ALSO contribute to the shared agent pool by
    # default (so agent-scoped tasks work without the admin knowing to tick it).
    from storage import database as _db
    _connector_is_admin = (_db.get_user(owner_sub) or {}).get("role") == "admin"

    # Reconnecting the SAME account (matched by the auth blob's identity)
    # refreshes tokens on the existing row; a DIFFERENT account creates a
    # second subscription (users can pool several plans). Matching on mere
    # (owner, layer, provider) silently clobbered the first account's
    # credential when a second one was connected — see the Anthropic twin
    # in claude_oauth.py.
    identity = (
        auth_data.get("email")
        or (auth_data.get("tokens") or {}).get("account_id")
        or ""
    )
    existing = subscription_store.list_subscriptions(
        layer=meta["layer"],
        owner_sub=owner_sub,
    )
    existing_oauth = [s for s in existing if s["auth_type"] == "oauth" and s["provider"] == "openai"]

    if identity:
        # Only a row proven to be the same account is refreshed; legacy
        # rows (oauth_email == "") are never adopted by guesswork.
        match = next(
            (s for s in existing_oauth if s.get("oauth_email") == identity),
            None,
        )
    else:
        match = existing_oauth[0] if existing_oauth else None
        if match:
            logger.warning(
                "Codex auth blob carried no account identity — refreshing "
                "the first existing subscription %s", match["id"][:8],
            )

    if match:
        sub_id = match["id"]
        subscription_store.update_credential_data(sub_id, credential_data)
        subscription_store.update_subscription(
            sub_id, status="active", label=label, oauth_email=identity or None,
        )
        sub = subscription_store.get_subscription(sub_id)
        logger.info(f"Updated existing OpenAI OAuth subscription {sub_id[:8]} with fresh tokens")
        # The exchange rotated the grant OUTSIDE the rotation chokepoint —
        # push the fresh token into live bound sessions' auth.json files
        # (see the Anthropic twin in claude_oauth.py).
        await asyncio.to_thread(subscription_pool.fan_out_current_token, sub_id)
    else:
        sub = subscription_store.add_subscription(
            layer=meta["layer"],
            provider="openai",
            auth_type="oauth",
            owner_sub=owner_sub,
            use_personal=True,
            contribute_platform=is_platform or _connector_is_admin,
            label=label,
            credential_data=credential_data,
            oauth_email=identity,
        )
        logger.info(f"Created new OpenAI OAuth subscription {sub['id'][:8]}")

    # A freshly (re)connected account may be the replacement that sessions
    # stuck on a delisted/removed subscription are waiting for.
    subscription_pool.schedule_rebind("openai oauth connect")
    return {"subscription": sub}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cleanup_stale_logins():
    """Remove login sessions older than 20 minutes."""
    now = time.monotonic()
    stale = [lid for lid, m in _active_logins.items() if now - m["started_at"] > 1200]
    for lid in stale:
        meta = _active_logins.pop(lid, None)
        if meta and meta["proc"].returncode is None:
            try:
                meta["proc"].terminate()
            except Exception:
                pass

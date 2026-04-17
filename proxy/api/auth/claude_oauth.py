"""Claude OAuth REST API — PKCE flow for connecting Claude subscriptions.

Flow:
1. POST /v1/oauth/claude/start → returns {url, state} → open popup
2. User authenticates → Anthropic shows authorization code on callback page
3. POST /v1/oauth/claude/exchange → {code, state, layer, label} → stores subscription
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.providers import get_current_user, require_auth, UserContext
from auth import claude_oauth
from services.engines import subscription_pool
from storage import subscription_store

logger = logging.getLogger(__name__)
router = APIRouter()

# In-memory PKCE state store (state → {code_verifier, user_sub, owner_type, expiry})
_STATE_TTL = 300  # 5 minutes
_oauth_states: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class OAuthStartRequest(BaseModel):
    layer: str = "claude-code-cli"
    owner_type: str = "platform"  # 'platform' (admin) or 'user'


class OAuthExchangeRequest(BaseModel):
    code: str
    state: str
    layer: str = "claude-code-cli"
    label: str = ""


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def _create_state(user_sub: str, owner_type: str) -> str:
    """Generate and store PKCE state."""
    import secrets
    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = claude_oauth.generate_pkce()

    _oauth_states[state] = {
        "code_verifier": code_verifier,
        "code_challenge": code_challenge,
        "user_sub": user_sub,
        "owner_type": owner_type,
        "expiry": time.monotonic() + _STATE_TTL,
    }

    # Purge expired
    now = time.monotonic()
    expired = [k for k, v in _oauth_states.items() if v["expiry"] < now]
    for k in expired:
        _oauth_states.pop(k, None)

    return state


def _consume_state(state: str) -> dict | None:
    """Consume and return PKCE state (one-time use)."""
    meta = _oauth_states.pop(state, None)
    if meta is None:
        return None
    if time.monotonic() > meta["expiry"]:
        return None
    return meta


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/v1/oauth/claude/start")
async def oauth_start(
    req: OAuthStartRequest,
    user: UserContext = Depends(get_current_user),
):
    """Start the Claude OAuth PKCE flow. Returns auth URL for popup."""
    user = require_auth(user)
    # Admin required for platform subscriptions
    if req.owner_type == "platform" and user.role != "admin":
        raise HTTPException(403, "Admin required for platform subscriptions")

    state = _create_state(
        user_sub=user.sub,
        owner_type=req.owner_type,
    )
    meta = _oauth_states[state]
    url = claude_oauth.build_auth_url(meta["code_challenge"], state)

    return {"url": url, "state": state}


@router.post("/v1/oauth/claude/exchange")
async def oauth_exchange(
    req: OAuthExchangeRequest,
    user: UserContext = Depends(get_current_user),
):
    """Exchange authorization code for tokens and create subscription."""
    user = require_auth(user)
    code = req.code.strip()
    # Strip URL fragment if user copied from browser address bar
    if '#' in code:
        code = code[:code.index('#')]

    meta = _consume_state(req.state)
    if not meta:
        raise HTTPException(400, "Invalid or expired OAuth state")

    # Verify the request comes from the same user who started the flow
    if meta["user_sub"] != user.sub:
        raise HTTPException(403, "OAuth state mismatch")

    # Admin required for platform subscriptions
    if meta["owner_type"] == "platform" and user.role != "admin":
        raise HTTPException(403, "Admin required for platform subscriptions")

    # Exchange code for tokens (pass state to match CLI behavior). The
    # exchange is a synchronous HTTP POST (timeout 15s) — run it off the
    # event loop like every other sync auth/storage call.
    try:
        token_data = await asyncio.to_thread(
            claude_oauth.exchange_code, code, meta["code_verifier"], state=req.state,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    if not access_token:
        raise HTTPException(400, "No access token in response")

    expires_in = token_data.get("expires_in", 28800)
    scopes = token_data.get("scope", "").split() if token_data.get("scope") else []
    subscription_type = token_data.get("subscriptionType", "")
    rate_limit_tier = token_data.get("rateLimitTier", "")

    # Build credential data in the same format as .credentials.json
    credential_data = {
        "oauth_token": {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "expiresAt": int((time.time() + expires_in) * 1000),
            "scopes": scopes,
            "subscriptionType": subscription_type,
            "rateLimitTier": rate_limit_tier,
        }
    }

    # Build label from subscription type if not provided
    label = req.label
    if not label:
        type_labels = {
            "pro": "Claude Pro",
            "max": "Claude Max",
            "api": "Claude API",
        }
        label = type_labels.get(subscription_type, f"Claude ({subscription_type or 'subscription'})")

    # Store as subscription. Reconnecting the SAME account — matched by the
    # provider-reported account identity — refreshes tokens on the existing
    # row; a DIFFERENT account creates a second subscription (users can pool
    # several plans). Matching on mere (owner, layer, provider), as this did
    # before identities were stamped, silently clobbered the first account's
    # credential the moment a second one was connected.
    # The connector owns the account: the admin for a 'platform' connect, the user
    # for 'user'. use_personal lets them run their own chats on it; a 'platform'
    # connect also contributes it to the agent pool. (The admin gate above ensures
    # only an admin can request owner_type='platform', so a non-admin can never
    # contribute.) Flags are set on CREATE only — reconnect just refreshes tokens,
    # leaving whatever the owner later set via the scope checkboxes.
    is_platform = meta["owner_type"] == "platform"
    owner_sub = user.sub
    account = token_data.get("account") or {}
    identity = account.get("email_address") or account.get("uuid") or ""
    existing = subscription_store.list_subscriptions(
        layer=req.layer,
        owner_sub=owner_sub,
    )
    existing_oauth = [s for s in existing if s["auth_type"] == "oauth" and s["provider"] == "anthropic"]

    if identity:
        # Only a row PROVEN to be the same account is refreshed. Pre-identity
        # legacy rows (oauth_email == "") are never adopted — we can't tell
        # which account they hold, and guessing is exactly the clobber bug;
        # a same-account legacy reconnect just yields a fresh stamped row and
        # the stale pill is deleted by hand once.
        match = next(
            (s for s in existing_oauth if s.get("oauth_email") == identity),
            None,
        )
    else:
        # Provider returned no account identity (unexpected for Anthropic) —
        # fall back to the historic single-row refresh so a reconnect after
        # revocation still works.
        match = existing_oauth[0] if existing_oauth else None
        if match:
            logger.warning(
                "Claude OAuth exchange returned no account identity — "
                "refreshing the first existing subscription %s", match["id"][:8],
            )

    if match:
        # Update the same account's subscription with fresh tokens
        sub_id = match["id"]
        subscription_store.update_credential_data(sub_id, credential_data)
        subscription_store.update_subscription(
            sub_id, status="active", label=label, oauth_email=identity or None,
        )
        sub = subscription_store.get_subscription(sub_id)
        logger.info(f"Updated existing OAuth subscription {sub_id[:8]} with fresh tokens")
        # The exchange rotated the grant OUTSIDE the rotation chokepoint —
        # push the fresh token into live bound sessions' credential files.
        # Their pre-exchange token may be revoked by this rotation, and
        # 401-recovery re-reads the same stale file forever without this.
        await asyncio.to_thread(subscription_pool.fan_out_current_token, sub_id)
    else:
        sub = subscription_store.add_subscription(
            layer=req.layer,
            provider="anthropic",
            auth_type="oauth",
            owner_sub=owner_sub,
            use_personal=True,
            # Admins' personal connects ALSO contribute to the shared agent pool
            # by default (so agent-scoped tasks work without the admin knowing to
            # tick it). Non-admins can never contribute (the admin gate above).
            contribute_platform=is_platform or user.role == "admin",
            label=label,
            credential_data=credential_data,
            oauth_email=identity,
        )
        logger.info(f"Created new OAuth subscription {sub['id'][:8]}")

    # A freshly (re)connected account may be the replacement that sessions
    # stuck on a delisted/removed subscription are waiting for.
    subscription_pool.schedule_rebind("claude oauth connect")
    return {
        "subscription": sub,
        "subscription_type": subscription_type,
        "rate_limit_tier": rate_limit_tier,
    }

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

from auth.providers import get_current_user, require_human, UserContext, require_user
from api.auth.openai_oauth import limit_connect_start
from core.layers.cli import oauth as claude_oauth
from services.engines import subscription_pool
from storage.billing import subscription_status, subscription_store
from auth import roles

logger = logging.getLogger(__name__)
# No route here takes an anonymous caller (auth.providers.require_user).
router = APIRouter(dependencies=[Depends(require_user)])

#: The vendor this login route belongs to — the engine's ``identity.vendor_id``
#: and the provider its OAuth rows carry, named once.
_VENDOR = "anthropic"

# In-memory PKCE state store (state → {code_verifier, user_sub, owner_type, expiry})
_STATE_TTL = 300  # 5 minutes
_oauth_states: dict[str, dict] = {}

# Starts per person and window (``config.RATE_LIMIT_RULES``). Its own
# bucket: a Codex install that cannot spawn must not spend the Claude
# connect's budget.
_START_BUCKET = "oauth_start_claude"


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


def _require_claude_login_engine(layer: str) -> None:
    """The engine a Claude login is stored on must be one that takes an
    Anthropic OAuth login (``identity.vendor_id`` + ``oauth`` among its auth
    types) — a request naming another engine used to store an Anthropic
    OAuth row under it, which the pool then handed to the wrong CLI."""
    from core.session.session_manager import get_layer_capabilities
    caps = get_layer_capabilities(layer)
    if (caps is None or "oauth" not in caps.auth.auth_types
            or caps.identity.vendor_id != _VENDOR):
        raise HTTPException(400, f"{layer} does not take a Claude login")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/v1/oauth/claude/start")
async def oauth_start(
    req: OAuthStartRequest,
    user: UserContext = Depends(get_current_user),
):
    """Start the Claude OAuth PKCE flow. Returns auth URL for popup."""
    user = require_human(user)
    # Admin required for platform subscriptions
    if req.owner_type == "platform" and not roles.is_admin(user.role):
        raise HTTPException(403, "Admin required for platform subscriptions")
    limit_connect_start(_START_BUCKET, user.sub)

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
    user = require_human(user)
    _require_claude_login_engine(req.layer)
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
    if meta["owner_type"] == "platform" and not roles.is_admin(user.role):
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
    account = token_data.get("account") or {}
    token_email = str(account.get("email_address") or "").strip()
    token_uuid = str(account.get("uuid") or "").strip()

    # One profile read per exchange, off the event loop, only when the token
    # response left a gap: the plan tier (the Claude Code TUI gates
    # plan-included models on it) or the account identity (the match key
    # below, never guessed from the row list).
    profile: dict | None = None
    profile_read = False
    if not subscription_type or not (token_email or token_uuid):
        profile = await asyncio.to_thread(claude_oauth.fetch_profile, access_token)
        profile_read = True
    if not subscription_type and profile:
        fetched_type, fetched_tier = claude_oauth.derive_subscription_fields(profile)
        subscription_type = fetched_type
        rate_limit_tier = rate_limit_tier or fetched_tier

    # A grant Claude Code cannot run on is refused HERE, before any store
    # write: stored active it would be selected (and scope-stickily reused)
    # while every turn fails inside the CLI, and a reconnect with a
    # downgraded grant must never overwrite a good row's tokens. Public
    # issue #3. The 400 detail is what both connect forms display.
    refusal = claude_oauth.grant_refusal(scopes, subscription_type)
    if refusal:
        logger.warning(
            "Claude OAuth exchange refused for %s: scopes=%s subscriptionType=%s",
            token_email or token_uuid or "<no identity>",
            scopes, subscription_type or "-",
        )
        raise HTTPException(400, refusal)

    identity, account_uuid = _resolve_identity(
        token_email=token_email, token_uuid=token_uuid,
        profile=profile, profile_read=profile_read,
    )

    # Build credential data in the same format as .credentials.json
    oauth_token = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": int((time.time() + expires_in) * 1000),
        "scopes": scopes,
        "subscriptionType": subscription_type,
        "rateLimitTier": rate_limit_tier,
    }
    # The login GRANT's own lifetime (finite — ~28 days for Claude logins):
    # stored so the subscription_health sweep can warn before it lapses.
    rt_expires_in = token_data.get("refresh_token_expires_in")
    if rt_expires_in:
        oauth_token["refreshTokenExpiresAt"] = int((time.time() + rt_expires_in) * 1000)
    # The account uuid rides in the blob as a second match key, so an
    # exchange that yields only the uuid still finds the row.
    if account_uuid:
        oauth_token["accountUuid"] = account_uuid
    credential_data = {"oauth_token": oauth_token}

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

    def _match_or_create() -> tuple[dict | None, dict | None, str]:
        """``(match, None, "")`` for the row already holding this account,
        ``(None, row, "")`` for the row created for it, ``(None, None,
        identity)`` when a parallel connect won the insert."""
        # include_disabled: a reconnect on an admin-disabled row must MATCH it
        # (and keep it disabled, below) — excluding it here would fork a second
        # ACTIVE row for the same account, silently routing around the admin.
        existing = subscription_store.list_subscriptions(
            layer=req.layer,
            owner_sub=owner_sub,
            include_disabled=True,
        )
        existing_oauth = [s for s in existing if s["auth_type"] == "oauth" and s["provider"] == _VENDOR]
        match = _match_existing(existing_oauth, identity=identity, account_uuid=account_uuid)
        if match:
            return match, None, ""
        try:
            return None, subscription_store.add_subscription(
                layer=req.layer,
                provider=_VENDOR,
                auth_type="oauth",
                owner_sub=owner_sub,
                use_personal=True,
                # Admins' personal connects ALSO contribute to the shared agent pool
                # by default (so agent-scoped tasks work without the admin knowing to
                # tick it). Non-admins can never contribute (the admin gate above).
                contribute_platform=is_platform or roles.is_admin(user.role),
                label=label,
                credential_data=credential_data,
                oauth_email=identity,
            ), ""
        except subscription_store.SubscriptionExists:
            # A parallel connect of the same account won the insert: name the
            # row it holds instead of a bare 500.
            rows = subscription_store.list_subscriptions(
                layer=req.layer, owner_sub=owner_sub, include_disabled=True,
            )
            held = _match_existing(
                [s for s in rows if s["auth_type"] == "oauth" and s["provider"] == _VENDOR],
                identity=identity, account_uuid=account_uuid,
            )
            return None, None, (held or {}).get("oauth_email") or identity

    match, sub, conflict = await asyncio.to_thread(_match_or_create)
    if conflict:
        raise HTTPException(409, f"That account ({conflict}) is already connected.")
    created = match is None
    previous_status = match.get("status") if match else None
    if match:
        # Update the same account's subscription with fresh tokens. Under the
        # sub's refresh lock: an in-flight refresh of the OLD (possibly dead)
        # token must not interleave — its failure verdict would land on the
        # just-written fresh grant. Same lock discipline as every rotation.
        # An admin-DISABLED row keeps its status (credential refresh must not
        # override the admin decision); anything else revives to active.
        # The label and the stored identity spelling are kept: a renamed
        # pill keeps its name, and a case-variant identity must not trip the
        # case-sensitive unique index.
        sub_id = match["id"]
        new_status = (subscription_status.DISABLED if match.get("status") == subscription_status.DISABLED
                      else subscription_status.ACTIVE)

        def _apply_reconnect() -> None:
            with subscription_pool._refresh_lock(sub_id):
                subscription_store.update_credential_data(sub_id, credential_data)
                subscription_store.update_subscription(sub_id, status=new_status)
                subscription_pool.clear_refresh_backoff(sub_id)

        await asyncio.to_thread(_apply_reconnect)
        sub = await asyncio.to_thread(subscription_store.get_subscription, sub_id)
        logger.info(f"Updated existing OAuth subscription {sub_id[:8]} with fresh tokens")
        # The exchange rotated the grant OUTSIDE the rotation chokepoint —
        # push the fresh token into live bound sessions' credential files.
        # Their pre-exchange token may be revoked by this rotation, and
        # 401-recovery re-reads the same stale file forever without this.
        await asyncio.to_thread(subscription_pool.fan_out_current_token, sub_id)
    else:
        logger.info(f"Created new OAuth subscription {sub['id'][:8]}")

    # A freshly (re)connected account may be the replacement that sessions
    # stuck on a delisted/removed subscription are waiting for.
    subscription_pool.schedule_rebind("claude oauth connect")
    # The account's window bars show right after the connect.
    from services.engines import subscription_windows
    subscription_windows.schedule_poll(str(sub.get("id") or ""))
    return {
        "subscription": sub,
        "subscription_type": subscription_type,
        "rate_limit_tier": rate_limit_tier,
        "created": created,
        "previous_status": previous_status,
    }


def _resolve_identity(
    *, token_email: str, token_uuid: str, profile: dict | None, profile_read: bool,
) -> tuple[str, str]:
    """The account identity an exchange stores and matches on, plus the
    account uuid for the credential blob.

    Token ``email_address`` first, then the profile's ``email``, then the
    uuid; an email is lower-cased for storage. No identity is a refusal, not
    a guess: refreshing "the first row" put a second account's tokens under
    the first account's name. The two causes get distinct messages, since the
    profile host differs from the token host and an egress rule can pass one
    and not the other.
    """
    prof_account = (profile.get("account") or {}) if isinstance(profile, dict) else {}
    email = token_email or str(prof_account.get("email") or "").strip()
    uuid = token_uuid or str(prof_account.get("uuid") or "").strip()
    identity = email.lower() if email else uuid
    if identity:
        return identity, uuid
    if profile_read and profile is None:
        logger.warning(
            "Claude OAuth exchange: the token carried no account identity and "
            "the profile endpoint could not be reached to confirm it",
        )
        raise HTTPException(
            400,
            "Could not reach Claude to confirm which account this is. Try again "
            "in a moment; if it keeps failing, the install cannot reach "
            "api.anthropic.com.",
        )
    logger.warning(
        "Claude OAuth exchange: neither the token nor the profile returned an "
        "account identity",
    )
    raise HTTPException(
        400,
        "Claude did not return the account identity, so this login cannot be "
        "told apart from your other accounts. Try again.",
    )


def _match_existing(
    rows: list[dict], *, identity: str, account_uuid: str,
) -> dict | None:
    """The existing row that holds this account: the exact identity first,
    then a case-variant of it (the stored spelling is kept), then the uuid
    stored in the credential blob (an exchange that yielded only the uuid,
    or an install whose earlier connect stamped the email). Pre-identity
    rows (empty ``oauth_email``) are never adopted: they could hold any
    account, and guessing is the clobber bug."""
    for s in rows:
        if s.get("oauth_email") and s.get("oauth_email") == identity:
            return s
    folded = identity.lower()
    for s in rows:
        stored = s.get("oauth_email") or ""
        if stored and stored.lower() == folded:
            return s
    if account_uuid:
        for s in rows:
            if s.get("oauth_email") == account_uuid:
                return s
            blob = subscription_store.get_credential_data(s["id"])
            token = blob.get("oauth_token") if isinstance(blob, dict) else None
            if isinstance(token, dict) and token.get("accountUuid") == account_uuid:
                return s
    return None

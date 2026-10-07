"""Billing API — user credit balance for the OtoDock hosted relay.

When this install is relay-connected (:func:`relay_client.is_available`), the
balance is fetched live from the commercial relay (``api.otodock.io``); the credit
ledger (``user_credit_balance`` / ``credit_transactions`` / ``relay_call_log``)
lives in the commercial repo. Otherwise (air-gapped, offline_term, or relay
unset) this returns the canonical zero-balance stub. The response shape is stable
either way — the frontend derives ``has_balance = balance_usd > 0``.

Per-user: the relay debits the CALLING user at relay-call time (the relay is the
enforcement point), so the balance is keyed on ``user.sub``.
"""

import logging

from fastapi import APIRouter, Depends

from auth.providers import get_current_user, require_auth, UserContext, require_user

logger = logging.getLogger("claude-proxy.billing")
# No route here takes an anonymous caller (auth.providers.require_user).
router = APIRouter(dependencies=[Depends(require_user)])

_ZERO_BALANCE = {
    "balance_usd": 0.0,
    "balance_eur_approx": 0.0,
    "low_threshold": 0.0,
    "recent_transactions": [],
}


@router.get("/v1/user/credits")
async def get_user_credits(user: UserContext = Depends(get_current_user)):
    """Return the calling user's hosted-relay credit balance.

    Proxies to the relay when available; falls back to the zero-balance stub
    on an air-gapped/unconfigured install OR any relay error/outage (a transient
    relay issue must never break the dashboard's billing read). The per-MCP
    ``billing_setup_url`` comes from the manifest, not from here.
    """
    user = require_auth(user)
    from services.billing import relay_client

    # Only fetch a balance when this install is actually connected to an OtoDock
    # account (token present); otherwise there's no account to bill → zero stub.
    if relay_client.is_available() and relay_client.is_connected():
        try:
            return await relay_client.relay_credits(user.sub)
        except Exception:
            logger.warning("relay credits fetch failed; returning zero balance", exc_info=True)
    return dict(_ZERO_BALANCE)

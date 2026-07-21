"""Install-facing OtoDock account-connect endpoints (API/credit relay).

Binds THIS install to an OtoDock account so the hosted relay (image-gen MCP +
Direct-LLM for Anthropic/OpenAI/Groq) can spend the account's credits. The admin
starts the connect (gets a pairing code + a consent URL), approves on otodock.io,
and the one-time handle bounces back here to be redeemed for the per-install
``account_token``. Enable/disable flips the master 'hosted relay' toggle (which
gates the 'Hosted by OtoDock' system MCP instances + Direct-LLM availability);
disconnect fully revokes the link.

Mirrors api/auth/oauth.py (start → browser → callback/exchange). A single in-flight
connect state lives in a platform_setting (worker-safe — one admin connects at a
time). The mobile return reuses the already-handled ``otodock://oauth/...`` deep
link (zero Android changes).
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

import config
from auth.providers import UserContext, get_current_user
from services.billing import hosted_instances, relay_client
from storage import database as db

logger = logging.getLogger("claude-proxy.account-api")
router = APIRouter()

_STATE_KEY = "_otodock_connect_state"        # single in-flight connect CSRF state
_RELAY_TOGGLE_KEY = "otodock_api_relay_enabled"


class ConnectStartRequest(BaseModel):
    mobile: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_admin(user: UserContext) -> None:
    if user.is_service:
        return  # the trusted master key is admin-equivalent (service-to-service)
    if not user.is_admin:
        raise HTTPException(403, "Admin only")


def _require_relay_settable() -> None:
    """Connect/disconnect require an online, operator-unmanaged install."""
    if not relay_client.relay_offered():
        raise HTTPException(409, "This install is air-gapped — hosted relay is unavailable.")
    if _RELAY_TOGGLE_KEY in config.forced_settings():
        raise HTTPException(409, "Hosted relay is managed by OtoDock for this install.")


def _callback_uri() -> str:
    base = config.DASHBOARD_PUBLIC_URL.rstrip("/") if config.DASHBOARD_PUBLIC_URL else ""
    if not base:
        raise HTTPException(500, "DASHBOARD_PUBLIC_URL not configured")
    return f"{base}/v1/account/connect/callback"


def _set_state(state: str, mobile: bool) -> None:
    db.set_platform_setting(_STATE_KEY, f"{'m' if mobile else 'w'}:{state}")


def _check_and_clear_state(state: str) -> tuple[bool, bool]:
    """One-shot: validate the received state against the stored one and clear it.
    Returns (valid, mobile)."""
    stored = db.get_platform_setting(_STATE_KEY) or ""
    db.set_platform_setting(_STATE_KEY, "")
    flag, _, value = stored.partition(":")
    valid = bool(value) and bool(state) and secrets.compare_digest(value, state)
    return (valid, flag == "m")


def _enable_and_reconcile() -> None:
    """Turn the master relay toggle on + reconcile system instances (so the
    'Hosted by OtoDock' instances appear immediately). No-op on the toggle for a
    forced (cloud) install — set_platform_setting ignores forced keys."""
    db.set_platform_setting(_RELAY_TOGGLE_KEY, "1")
    try:
        hosted_instances.reconcile_otodock_system_instances()
    except Exception:
        logger.exception("post-connect instance reconcile failed (non-fatal)")


def _disable_and_reconcile() -> None:
    db.set_platform_setting(_RELAY_TOGGLE_KEY, "")
    try:
        hosted_instances.reconcile_otodock_system_instances()
    except Exception:
        logger.exception("post-disable instance reconcile failed (non-fatal)")


def _success_html() -> str:
    return """\
<!DOCTYPE html><html><head><title>Connected</title>
<style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;
justify-content:center;height:100vh;margin:0;background:#faf9f9}
.card{text-align:center;padding:2rem;border-radius:12px;background:#fff;
border:1px solid #e5e7eb;max-width:360px}.check{font-size:3rem;margin-bottom:.5rem}
h2{color:#333;margin:0 0 .5rem;font-size:1.1rem}p{color:#666;font-size:.9rem;margin:0}
</style></head><body><div class="card"><div class="check">&#10003;</div>
<h2>Connected to OtoDock</h2><p>Hosted relay is ready.</p>
<p id="hint" style="margin-top:1rem;color:#999;font-size:.8rem">This window will close automatically.</p>
</div><script>
if(window.opener){window.opener.postMessage({type:"otodock-connect-complete"},window.location.origin);
setTimeout(()=>window.close(),1500);}else{document.getElementById("hint").textContent=
"You can close this tab and return to OtoDock.";}
</script></body></html>"""


def _error_html(message: str) -> str:
    import html
    safe = html.escape(message)
    return f"""\
<!DOCTYPE html><html><head><title>Error</title>
<style>body{{font-family:system-ui,sans-serif;display:flex;align-items:center;
justify-content:center;height:100vh;margin:0;background:#faf9f9}}
.card{{text-align:center;padding:2rem;border-radius:12px;background:#fff;
border:1px solid #e5e7eb;max-width:360px}}.icon{{font-size:3rem;margin-bottom:.5rem}}
h2{{color:#333;margin:0 0 .5rem;font-size:1.1rem}}p{{color:#da3536;font-size:.9rem;margin:0}}
</style></head><body><div class="card"><div class="icon">&#10007;</div>
<h2>Connection Failed</h2><p>{safe}</p>
<p style="margin-top:1rem;color:#999;font-size:.8rem">You can close this window.</p>
</div></body></html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/v1/account/connect/start")
async def connect_start(body: ConnectStartRequest, user: UserContext = Depends(get_current_user)):
    """Begin the connect handshake (admin). Returns ``{url, pairing_code}`` — the
    dashboard DISPLAYS the pairing code and opens ``url`` in a browser."""
    _require_admin(user)
    _require_relay_settable()
    state = secrets.token_urlsafe(32)
    _set_state(state, body.mobile)
    try:
        data = await relay_client.account_connect_authorize_url(
            state=state, install_callback=_callback_uri(),
        )
    except relay_client.RelayNotConfigured as e:
        raise HTTPException(503, str(e))
    except relay_client.RelayError as e:
        raise HTTPException(403, relay_client.relay_error_message(e.code))
    return {"url": data.get("url", ""), "pairing_code": data.get("pairing_code", "")}


@router.get("/v1/account/connect/callback")
async def connect_callback(
    request: Request,
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
):
    """Browser lands here after consent: redeem the handle → store token → enable."""
    if error:
        return HTMLResponse(_error_html(f"OtoDock returned: {error}"))
    if not code or not state:
        return HTMLResponse(_error_html("Missing code or state parameter"))
    valid, mobile = _check_and_clear_state(state)
    if not valid:
        return HTMLResponse(_error_html("Invalid or expired connect request"))
    try:
        token = await relay_client.account_connect_exchange(code=code)
    except Exception as e:
        # Never reflect the exception text — it can carry relay response
        # bodies / internal URLs. Full detail goes to the server log only.
        logger.exception("account connect exchange failed")
        return HTMLResponse(_error_html(
            f"Connect exchange failed ({type(e).__name__}). "
            "Check the proxy logs for details."
        ))
    if not token:
        return HTMLResponse(_error_html("OtoDock returned no account token"))
    _enable_and_reconcile()
    if mobile:
        # Tag the source install so the multi-installation Android app routes the
        # callback back to the server that started the flow.
        from services.billing.relay_client import get_install_id
        return RedirectResponse(
            f"otodock://oauth/connect/complete?install={get_install_id()}"
        )
    return HTMLResponse(_success_html())


@router.post("/v1/account/relay/enable")
async def relay_enable(user: UserContext = Depends(get_current_user)):
    """Turn the hosted relay ON for an ALREADY-connected install (e.g. a paid
    install auto-linked at activation, or after a disable). Returns 409 if the
    install isn't connected yet — the dashboard then runs the connect handshake."""
    _require_admin(user)
    _require_relay_settable()
    if not relay_client.is_connected():
        raise HTTPException(409, "not_connected")
    _enable_and_reconcile()
    return {"status": "enabled"}


@router.post("/v1/account/relay/disable")
async def relay_disable(user: UserContext = Depends(get_current_user)):
    """Turn the hosted relay OFF but KEEP the connection (the account_token
    persists, so re-enabling is instant). Removes the system MCP instances."""
    _require_admin(user)
    _require_relay_settable()
    _disable_and_reconcile()
    return {"status": "disabled"}


@router.post("/v1/account/disconnect")
async def disconnect(user: UserContext = Depends(get_current_user)):
    """Fully disconnect: revoke the link at the relay (best-effort) + clear the
    local token + toggle off + remove the system instances. Use this to switch the
    install to a different OtoDock account."""
    _require_admin(user)
    if _RELAY_TOGGLE_KEY in config.forced_settings():
        raise HTTPException(409, "Hosted relay is managed by OtoDock for this install.")
    await relay_client.account_disconnect()   # best-effort revoke + always clears locally
    _disable_and_reconcile()
    return {"status": "disconnected"}

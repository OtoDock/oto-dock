"""Generic OAuth API endpoints — provider-neutral start / callback / exchange /
disconnect / accounts routes that work for any provider declared in any
MCP manifest.

Routes:
  POST   /v1/oauth/{provider}/start        — body: {mcp_name, services, account_label?, mobile?}
  GET    /v1/oauth/{provider}/callback     — web popup; returns HTML
  POST   /v1/oauth/{provider}/exchange     — mobile JSON path
  GET    /v1/oauth/{provider}/accounts     — list current user's accounts for (provider, mcp_name)
  POST   /v1/oauth/{provider}/disconnect   — body: {mcp_name, account_label}

Account routing:
  * Account-label resolution: state carries an optional
    ``account_label_hint``; on callback, if missing or empty, the engine
    auto-labels from ``userinfo.email``. Every account is persisted under
    the connecting user (there is no platform service-account tier).

Mobile flow:
  * Same start endpoint; pass ``mobile=true`` and the callback redirects
    to ``otodock://oauth/{provider}/complete?email=...`` instead of
    returning HTML.

This module owns no business logic — it's a thin wrapper around
``services/oauth_engine`` (state issuance + exchange orchestration) and
``services/oauth_account_store`` (persistence).
"""

from __future__ import annotations

import asyncio
import html
import logging
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import config
from storage import credential_store
from storage import database as task_store
from services.billing import relay_client
from services.mcp import mcp_registry
from services.oauth import oauth_engine, oauth_account_store
from auth.oauth_providers import canonical_provider_id, get_provider
from auth.providers import UserContext, get_current_user

logger = logging.getLogger("claude-proxy.oauth-api")
router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class OAuthStartRequest(BaseModel):
    mcp_name: str
    services: list[str]
    account_label: str = ""        # optional override; defaults to userinfo.email
    mobile: bool = False


class OAuthExchangeRequest(BaseModel):
    code: str
    state: str


class OAuthDisconnectRequest(BaseModel):
    mcp_name: str
    account_label: str


# ---------------------------------------------------------------------------
# HTML responses
# ---------------------------------------------------------------------------


def _popup_origin() -> str:
    """Origin the OAuth popup may postMessage back to — the dashboard itself.

    The web popup is always opened by the dashboard at ``DASHBOARD_PUBLIC_URL``;
    posting to that exact origin (instead of ``"*"``) keeps the result —
    provider + email — from leaking to any other window that grabbed a handle.
    Falls back to ``"*"`` only when the public URL isn't configured (dev).
    """
    base = (config.DASHBOARD_PUBLIC_URL or "").rstrip("/")
    if not base:
        return "*"
    # Reduce to scheme://host[:port] — strip any path component.
    parts = base.split("/")
    return "/".join(parts[:3]) if len(parts) >= 3 else base


def _success_html(provider: str, email: str) -> str:
    # Dynamic values reach the inline script only via HTML-escaped data
    # attributes (attribute-safe: html.escape encodes &<>"'), so the script
    # itself is fully static; `.dataset` reads decode back to the original.
    label = html.escape(provider.capitalize())
    return f"""\
<!DOCTYPE html>
<html><head><title>Connected</title>
<style>
  body {{ font-family: system-ui, sans-serif; display: flex; align-items: center;
         justify-content: center; height: 100vh; margin: 0; background: #faf9f9; }}
  .card {{ text-align: center; padding: 2rem; border-radius: 12px;
           background: white; border: 1px solid #e5e7eb; max-width: 360px; }}
  .check {{ font-size: 3rem; margin-bottom: 0.5rem; }}
  h2 {{ color: #333; margin: 0 0 0.5rem; font-size: 1.1rem; }}
  p {{ color: #666; font-size: 0.9rem; margin: 0; }}
</style></head>
<body><div class="card" id="result" data-provider="{html.escape(provider)}"
  data-email="{html.escape(email)}" data-origin="{html.escape(_popup_origin())}">
  <div class="check">&#10003;</div>
  <h2>{label} Account Connected</h2>
  <p>{html.escape(email)}</p>
  <p id="hint" style="margin-top:1rem;color:#999;font-size:0.8rem;">This window will close automatically.</p>
</div>
<script>
  var el = document.getElementById("result");
  if (window.opener) {{
    window.opener.postMessage({{type: "oauth-complete", provider: el.dataset.provider, email: el.dataset.email}}, el.dataset.origin);
    setTimeout(() => window.close(), 1500);
  }} else {{
    document.getElementById("hint").textContent = "You can close this tab and return to the app.";
  }}
</script>
</body></html>"""


def _error_html(provider: str, message: str) -> str:
    return f"""\
<!DOCTYPE html>
<html><head><title>Error</title>
<style>
  body {{ font-family: system-ui, sans-serif; display: flex; align-items: center;
         justify-content: center; height: 100vh; margin: 0; background: #faf9f9; }}
  .card {{ text-align: center; padding: 2rem; border-radius: 12px;
           background: white; border: 1px solid #e5e7eb; max-width: 360px; }}
  .icon {{ font-size: 3rem; margin-bottom: 0.5rem; }}
  h2 {{ color: #333; margin: 0 0 0.5rem; font-size: 1.1rem; }}
  p {{ color: #da3536; font-size: 0.9rem; margin: 0; }}
</style></head>
<body><div class="card" id="result" data-provider="{html.escape(provider)}"
  data-error="{html.escape(message)}" data-origin="{html.escape(_popup_origin())}">
  <div class="icon">&#10007;</div>
  <h2>Connection Failed</h2>
  <p>{html.escape(message)}</p>
  <p style="margin-top:1rem;color:#999;font-size:0.8rem;">You can close this window.</p>
</div>
<script>
  var el = document.getElementById("result");
  window.opener?.postMessage({{type: "oauth-error", provider: el.dataset.provider, error: el.dataset.error}}, el.dataset.origin);
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_dashboard_user(user: UserContext) -> None:
    if user.is_api_key:
        raise HTTPException(403, "Dashboard only")


def _require_admin(user: UserContext) -> None:
    if user.is_service:
        return  # the trusted master key is admin-equivalent (service-to-service)
    if not user.is_admin:
        raise HTTPException(403, "Admin only")


def _redirect_uri(provider: str) -> str:
    """Public callback URL for this provider, derived from DASHBOARD_PUBLIC_URL."""
    base = config.DASHBOARD_PUBLIC_URL.rstrip("/") if config.DASHBOARD_PUBLIC_URL else ""
    if not base:
        raise HTTPException(500, "DASHBOARD_PUBLIC_URL not configured")
    return f"{base}/v1/oauth/{provider}/callback"


def _validate_services_against_manifest(
    mcp_name: str, services: list[str],
) -> None:
    """Reject service keys that don't appear in the MCP's manifest."""
    manifest = mcp_registry.get_manifest(mcp_name)
    if manifest is None or not manifest.credentials.oauth:
        raise HTTPException(400, f"MCP '{mcp_name}' has no oauth credential block")
    valid_keys = {s["key"] for s in manifest.credentials.oauth.get("services", [])}
    if not services:
        # Some providers allow login-only flows with no service keys.
        if not valid_keys:
            return
        raise HTTPException(400, "At least one service must be selected")
    invalid = set(services) - valid_keys
    if invalid:
        raise HTTPException(
            400, f"Invalid services for '{mcp_name}': {', '.join(sorted(invalid))}",
        )


def _validate_provider_for_mcp(mcp_name: str, provider_id: str) -> None:
    """Confirm the (mcp_name, provider_id) pair matches the manifest."""
    manifest = mcp_registry.get_manifest(mcp_name)
    if manifest is None or not manifest.credentials.oauth:
        raise HTTPException(400, f"MCP '{mcp_name}' has no oauth credential block")
    declared = manifest.credentials.oauth.get("provider_id", "")
    if declared != provider_id:
        raise HTTPException(
            400,
            f"MCP '{mcp_name}' declares provider_id={declared!r}; "
            f"called with {provider_id!r}",
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/v1/oauth/{provider}/start")
async def oauth_start(
    provider: str,
    body: OAuthStartRequest,
    user: UserContext = Depends(get_current_user),
):
    """Initiate the OAuth flow. Returns ``{url}`` for the consent popup."""
    _require_dashboard_user(user)
    _validate_provider_for_mcp(body.mcp_name, provider)
    _validate_services_against_manifest(body.mcp_name, body.services)

    manifest = mcp_registry.get_manifest(body.mcp_name)
    oauth_block = manifest.credentials.oauth or {}
    redirect_uri = _redirect_uri(provider)

    # HOSTED: no install-side app credential. Mint state for the install's
    # CSRF, then ask the relay for the vendor consent URL. The relay holds
    # OtoDock's client_secret, is the redirect target, performs the exchange,
    # and bounces the user's tokens back to install_callback echoing state.
    # Stub raises RelayNotConfigured (→ 503) until the relay is built.
    if relay_client.hosted_oauth_active(body.mcp_name, manifest):
        state = oauth_engine.create_state(
            user_sub=user.sub,
            mcp_name=body.mcp_name,
            provider_id=provider,
            services=body.services,
            account_label_hint=body.account_label,
            mobile=body.mobile,
            redirect_uri=redirect_uri,
        )
        scopes = mcp_registry.build_oauth_scopes(body.mcp_name, body.services)
        try:
            url = await relay_client.oauth_authorize_url(
                provider_id=provider,
                scopes=scopes,
                state=state,
                install_callback=redirect_uri,
                user_sub=user.sub,
            )
        except relay_client.RelayNotConfigured as e:
            raise HTTPException(503, str(e))
        except relay_client.RelayError as e:
            # The relay rejected the connect (e.g. seat_exceeded — the install is
            # over its licensed user cap). Surface the mapped, user-facing message.
            raise HTTPException(403, relay_client.relay_error_message(e.code))
        return {"url": url}

    # SELF-MANAGED: confirm OAuth app credentials are configured (DB lookup).
    app_cred = oauth_block.get("app_credential", "")
    creds = credential_store.get_infra_credentials(app_cred) if app_cred else {}
    client_id, client_secret = oauth_engine._resolve_app_credentials(
        oauth_block, creds,
    )
    if not client_id or not client_secret:
        raise HTTPException(
            500,
            f"OAuth app credentials for {app_cred!r} are not configured. "
            f"Admin must set them in MCP Servers.",
        )

    provider_impl = get_provider(provider)

    # Mint state + build provider auth URL.
    state = oauth_engine.create_state(
        user_sub=user.sub,
        mcp_name=body.mcp_name,
        provider_id=provider,
        services=body.services,
        account_label_hint=body.account_label,
        mobile=body.mobile,
        redirect_uri=redirect_uri,
    )

    # Build the scope list from the manifest — manifest is the source
    # of truth for which scopes back which services.
    scopes = mcp_registry.build_oauth_scopes(body.mcp_name, body.services)

    # Incremental scope grant: if the user is reconnecting an account
    # they already have, send `include_granted_scopes=true` so the
    # consent screen only asks for the NEW scopes (not the ones already
    # granted). Detected by an existing account row for
    # (user_sub, mcp_name, account_label).
    extra: dict[str, str] = {}
    if body.account_label:
        existing_accounts = await asyncio.to_thread(
            credential_store.list_user_accounts, user.sub, body.mcp_name,
        )
        if any(a["account_label"] == body.account_label for a in existing_accounts):
            extra["include_granted_scopes"] = "true"

    # Admin-consent gate: when ANY requested service has
    # `requires_admin_consent: true` in its manifest entry, add
    # `prompt=admin_consent`. This forces the consent UX in the user's
    # home tenant — it does NOT grant tenant-wide consent. For tenant-wide
    # grant (e.g. OnlineMeetingTranscript.Read.All for the whole org),
    # admins use the separate `/v1/oauth/microsoft/admin-consent/start`
    # route which builds a `/{tenant}/v2.0/adminconsent` URL.
    declared_services = oauth_block.get("services", []) or []
    service_admin_map = {
        s.get("key", ""): bool(s.get("requires_admin_consent", False))
        for s in declared_services
    }
    if any(service_admin_map.get(k, False) for k in body.services):
        extra["prompt"] = "admin_consent"

    # Microsoft tenant_id: resolved from MS_TENANT_ID infra credential
    # (defaults to "common" / multi-tenant). The Microsoft provider's
    # build_auth_url reads extra["tenant_id"] to construct the correct
    # tenant-scoped authorize URL. Other providers ignore the key.
    if provider == "microsoft":
        extra["tenant_id"] = creds.get("MS_TENANT_ID") or "common"

    # Manifest-declared static authorize params (vendor URL quirks — e.g.
    # Notion's mandatory `owner=user`). setdefault: flow-computed keys win.
    for k, v in (oauth_block.get("authorize_params") or {}).items():
        extra.setdefault(str(k), str(v))

    # PKCE: when the manifest opts into authorization_code_pkce, the engine
    # injected code_challenge into state.extra. Merge into the URL extras
    # so the provider's build_auth_url adds them as URL query params.
    state_extra = oauth_engine.peek_state_extra(state)
    for k, v in state_extra.items():
        extra.setdefault(k, v)

    url = await provider_impl.build_auth_url(
        state=state,
        scopes=scopes,
        redirect_uri=redirect_uri,
        client_id=client_id,
        extra=extra or None,
    )
    return {"url": url}


@router.get("/v1/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
):
    """OAuth callback — web popup (HTML) or mobile deep-link (302)."""
    # Reject an unknown provider before reflecting it anywhere; the canonical
    # id comes from the registry's key set, not the request path (defense in
    # depth on top of the HTML escaping in the templates).
    try:
        provider = canonical_provider_id(provider)
    except KeyError:
        return HTMLResponse(_error_html("", "Unknown provider"), status_code=400)
    if error:
        return HTMLResponse(_error_html(provider, f"Provider returned: {error}"))
    if not code or not state:
        return HTMLResponse(_error_html(provider, "Missing code or state parameter"))

    # We can't pre-validate state without consuming it. Peek by validating;
    # validate_state is one-shot, so the rest of the flow must succeed.
    try:
        result = await oauth_engine.do_oauth_exchange(code=code, state_token=state)
    except Exception as e:
        # Never reflect the exception text — it can carry vendor response
        # bodies / internal URLs. Full detail goes to the server log only.
        logger.exception("OAuth callback exchange failed (provider=%s)", provider)
        return HTMLResponse(_error_html(
            provider,
            f"Token exchange failed ({type(e).__name__}). "
            "Check the proxy logs for details.",
        ))

    if result.state.mobile:
        # Tag the source install so the multi-installation Android app routes the
        # callback back to the server that started the flow.
        from services.billing.relay_client import get_install_id
        return RedirectResponse(
            f"otodock://oauth/{provider}/complete"
            f"?email={quote(result.email or '')}&install={quote(get_install_id())}"
        )
    return HTMLResponse(_success_html(provider, result.email))


@router.post("/v1/oauth/{provider}/exchange")
async def oauth_exchange(
    provider: str,
    body: OAuthExchangeRequest,
    user: UserContext = Depends(get_current_user),
):
    """Mobile deep-link JSON exchange path.

    The mobile app receives the auth code via the otodock:// deep link and
    POSTs it here. State carries user_sub and is verified to match.
    """
    _require_dashboard_user(user)

    # Peek at state's user_sub before consuming — but validate_state is
    # one-shot. We need to consume + check + (if mismatched) raise.
    # Acceptable: state is single-use anyway; mismatch means the legitimate
    # user must retry, which is the correct UX.
    try:
        result = await oauth_engine.do_oauth_exchange(
            code=body.code, state_token=body.state,
        )
    except Exception as e:
        logger.exception("OAuth exchange failed (provider=%s)", provider)
        raise HTTPException(400, str(e)) from e

    if result.state.user_sub != user.sub:
        raise HTTPException(403, "State was issued for a different user")
    return {"status": "ok", "email": result.email, "account_label": result.account_label}


@router.get("/v1/oauth/{provider}/accounts")
async def list_oauth_accounts(
    provider: str,
    mcp_name: str = Query(...),
    user: UserContext = Depends(get_current_user),
):
    """List the user's connected accounts for (provider, mcp_name).

    Returns ``{accounts, has_service_credentials_only}``. The latter is
    retained for response-shape compatibility and is always ``False`` —
    there is no platform service-account tier; every user connects their
    own account.
    """
    _require_dashboard_user(user)
    _validate_provider_for_mcp(mcp_name, provider)
    accounts = await asyncio.to_thread(
        credential_store.list_user_accounts, user.sub, mcp_name,
    )
    return {
        "accounts": accounts,
        "has_service_credentials_only": False,
    }


@router.post("/v1/oauth/{provider}/disconnect")
async def oauth_disconnect(
    provider: str,
    body: OAuthDisconnectRequest,
    user: UserContext = Depends(get_current_user),
):
    """Disconnect one account: revoke at provider, delete token file + DB rows.

    Best-effort revoke; local cleanup always proceeds even if the vendor
    revoke endpoint fails (network, vendor 5xx, token already revoked).

    Cleans up webhook subscriptions tied to the account FIRST (the caller's
    own user-scope subscriptions plus any agent-scope subscriptions whose
    binding points at this account), then deletes the token file + DB rows.
    """
    _require_dashboard_user(user)
    _validate_provider_for_mcp(body.mcp_name, provider)

    # Pick the right token dir for revoke (need the refresh token before
    # we delete the file). Token dir is per-provider (e.g.
    # sessions/google-tokens/ vs sessions/slack-tokens/).
    manifest = mcp_registry.get_manifest(body.mcp_name)
    provider_id = (
        (manifest.credentials.oauth or {}).get("provider_id", "")
        if manifest else ""
    )
    if not provider_id:
        raise HTTPException(
            500,
            f"MCP '{body.mcp_name}' has no provider_id in its manifest's "
            f"credentials.oauth block",
        )

    username = await asyncio.to_thread(
        task_store.get_username_by_sub, user.sub,
    )
    token_dir = (
        oauth_account_store.get_token_dir(username, provider_id=provider_id)
        if username else None
    )

    if token_dir is not None:
        token_data = oauth_account_store.read_account_token(
            token_dir, body.account_label,
        ) or {}

        refresh_token = (
            token_data.get("refresh_token", "") or token_data.get("token", "")
        )
        if refresh_token:
            # HOSTED accounts carry `extra.via_relay` (set at exchange time)
            # and have NO local client_secret — route revoke through the relay,
            # which holds the secret. Best-effort either way: a stub
            # RelayNotConfigured (relay unbuilt) is swallowed below and local
            # cleanup proceeds.
            via_relay = bool((token_data.get("extra") or {}).get("via_relay"))
            try:
                if via_relay:
                    await relay_client.oauth_revoke(
                        provider_id=provider, token=refresh_token,
                    )
                else:
                    oauth_block = (manifest.credentials.oauth or {}) if manifest else {}
                    app_cred = oauth_block.get("app_credential", "")
                    creds = await asyncio.to_thread(
                        credential_store.get_infra_credentials, app_cred,
                    ) if app_cred else {}
                    client_id, client_secret = oauth_engine._resolve_app_credentials(
                        oauth_block, creds,
                    )
                    provider_impl = get_provider(provider)
                    await provider_impl.revoke(
                        token=refresh_token,
                        client_id=client_id,
                        client_secret=client_secret,
                    )
            except Exception as e:
                logger.warning("Best-effort revoke failed for %s: %s", provider, e)

    # Best-effort vendor DELETE for webhook subscriptions tied
    # to the account being disconnected. Must run BEFORE we delete the
    # token file / DB credentials (the vendor delete API call needs the
    # access token).
    try:
        from services.webhooks import subscription_manager
        # (a) the caller's own user-scope subscriptions for this account.
        await subscription_manager.cleanup_account_subscriptions(
            scope="user", owner=user.sub,
            mcp_name=body.mcp_name, account_label=body.account_label,
        )
        # (b) agent-scope cascade: any agent whose service binding points at
        # THIS (owner, account) loses its service subscriptions, and the now-
        # dangling binding is removed.
        bindings = await asyncio.to_thread(
            credential_store.list_service_agent_bindings, body.mcp_name,
        )
        for b in bindings:
            if (b.get("account_owner_sub") == user.sub
                    and b.get("account_label") == body.account_label):
                await subscription_manager.cleanup_account_subscriptions(
                    scope="service", owner=user.sub,
                    mcp_name=body.mcp_name, account_label=body.account_label,
                    agent=b.get("agent_name"),
                )
                await asyncio.to_thread(
                    credential_store.remove_service_agent_binding,
                    body.mcp_name, b.get("agent_name"),
                )
    except Exception:
        logger.exception(
            "Subscription cleanup raised for %s/%s (continuing with disconnect)",
            body.mcp_name, body.account_label,
        )

    if token_dir is not None:
        oauth_account_store.delete_account_token(token_dir, body.account_label)

    # Delete DB rows (the caller's user account).
    await asyncio.to_thread(
        credential_store.delete_user_credentials,
        user.sub, body.mcp_name, body.account_label,
    )

    logger.info(
        "OAuth disconnected: provider=%s mcp=%s user=%s account=%s",
        provider, body.mcp_name, user.sub[:8], body.account_label,
    )
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Device-code flow (Microsoft) — RFC 8628
# ---------------------------------------------------------------------------


class DeviceCodeStartRequest(BaseModel):
    mcp_name: str
    services: list[str]
    account_label: str = ""


class DeviceCodePollRequest(BaseModel):
    mcp_name: str
    device_code: str
    services: list[str]
    account_label: str = ""


def _app_creds_for(provider: str, mcp_name: str, flow: str = "") -> tuple[str, str, dict]:
    """Helper: resolve (client_id, client_secret, oauth_block) for routes
    that need them — device-code start, S2S, PAT save. ``flow`` chooses
    the right variant when manifest declares ``app_credential_variants``.
    """
    manifest = mcp_registry.get_manifest(mcp_name)
    if manifest is None or not manifest.credentials.oauth:
        raise HTTPException(400, f"MCP '{mcp_name}' has no oauth credential block")
    oauth_block = manifest.credentials.oauth
    variants = oauth_block.get("app_credential_variants") or {}
    app_cred = variants.get(flow) if flow else None
    if not app_cred:
        app_cred = oauth_block.get("app_credential", "")
    creds = credential_store.get_infra_credentials(app_cred) if app_cred else {}
    client_id, client_secret = oauth_engine._resolve_app_credentials(oauth_block, creds)
    if not client_id or not client_secret:
        raise HTTPException(
            500,
            f"OAuth app credentials for {app_cred!r} are not configured. "
            f"Admin must set them in MCP Servers.",
        )
    return client_id, client_secret, oauth_block


def _microsoft_extra(mcp_name: str, flow: str = "") -> dict[str, str]:
    """Build the ``extra`` dict Microsoft URL-builders need.

    Resolves ``tenant_id`` from the admin's ``MS_TENANT_ID`` infra
    credential field, defaulting to ``"common"`` (multi-tenant). Used by
    every Microsoft entry point (oauth_start, device_code_start,
    admin_consent_start) so MS_TENANT_ID overrides take effect without
    constructor or per-instance state on the provider.
    """
    manifest = mcp_registry.get_manifest(mcp_name)
    if manifest is None or not manifest.credentials.oauth:
        return {"tenant_id": "common"}
    oauth_block = manifest.credentials.oauth
    variants = oauth_block.get("app_credential_variants") or {}
    app_cred = variants.get(flow) if flow else None
    if not app_cred:
        app_cred = oauth_block.get("app_credential", "")
    creds = credential_store.get_infra_credentials(app_cred) if app_cred else {}
    return {"tenant_id": creds.get("MS_TENANT_ID") or "common"}


@router.post("/v1/oauth/{provider}/device-code/start")
async def device_code_start(
    provider: str,
    body: DeviceCodeStartRequest,
    user: UserContext = Depends(get_current_user),
):
    """Start a device-code grant. Returns vendor's
    ``{device_code, user_code, verification_uri, expires_in, interval}``.

    The client shows ``user_code`` + ``verification_uri`` to the user,
    who visits the URL and enters the code. Meanwhile the client polls
    ``/device-code/poll`` until success or expiry.

    Stateless — no server-side state token. The polling endpoint trusts
    the caller's session for user/mcp/account context.
    """
    _require_dashboard_user(user)
    _validate_provider_for_mcp(body.mcp_name, provider)
    _validate_services_against_manifest(body.mcp_name, body.services)

    # Hosted (via OtoDock) mode brokers only the browser auth-code flow —
    # the relay never holds device-code state, and the install has no local
    # app credential to run the flow itself. Without this guard the user
    # would hit a raw "credentials not configured" 500 below.
    manifest = mcp_registry.get_manifest(body.mcp_name)
    if manifest is not None and relay_client.hosted_oauth_active(
        body.mcp_name, manifest, flow="device_code",
    ):
        raise HTTPException(
            400,
            "Device-code sign-in needs self-managed OAuth app credentials — "
            "this MCP is in hosted (via OtoDock) mode. Use the browser "
            "connect instead, or switch the MCP to self-managed and register "
            "your own app.",
        )

    client_id, _, _ = _app_creds_for(provider, body.mcp_name, flow="device_code")
    scopes = mcp_registry.build_oauth_scopes(body.mcp_name, body.services)
    provider_impl = get_provider(provider)
    # Microsoft device-code start is tenant-scoped (mirrors authorize URL).
    # Other providers ignore tenant_id in extra.
    extra: dict[str, str] = {}
    if provider == "microsoft":
        extra.update(_microsoft_extra(body.mcp_name, flow="device_code"))
    payload = await provider_impl.start_device_code(
        scopes=scopes,
        client_id=client_id,
        extra=extra or None,
    )
    return payload


@router.post("/v1/oauth/{provider}/device-code/poll")
async def device_code_poll(
    provider: str,
    body: DeviceCodePollRequest,
    user: UserContext = Depends(get_current_user),
):
    """Poll a device-code grant. Returns 202 while pending, 200 on success.

    On success runs the same persist path as the authorization-code flow
    (token file + DB rows) and returns ``{status, email, account_label}``.
    """
    _require_dashboard_user(user)
    _validate_provider_for_mcp(body.mcp_name, provider)
    _validate_services_against_manifest(body.mcp_name, body.services)

    client_id, client_secret, _ = _app_creds_for(provider, body.mcp_name, flow="device_code")
    provider_impl = get_provider(provider)
    try:
        token_set = await provider_impl.poll_device_code(
            device_code=body.device_code,
            client_id=client_id,
            client_secret=client_secret,
        )
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    if token_set is None:
        # Vendor said authorization_pending / slow_down.
        return JSONResponse({"status": "pending"}, status_code=202)

    userinfo = await provider_impl.fetch_userinfo(access_token=token_set.access_token)
    if not userinfo.email:
        # GitHub-style email privacy: fall back to stable id / name (mirrors PAT).
        userinfo.email = userinfo.account_id or userinfo.name
    if not userinfo.email:
        raise HTTPException(
            500,
            f"{provider}: userinfo returned no email/id/name after device-code success",
        )
    account_label = body.account_label.strip() or userinfo.email

    await asyncio.to_thread(
        oauth_account_store.persist_oauth_account,
        user_sub=user.sub,
        mcp_name=body.mcp_name,
        provider_id=provider,
        account_label=account_label,
        services=body.services,
        token_set=token_set,
        userinfo=userinfo,
        client_id=client_id,
        client_secret=client_secret,
        token_url=provider_impl.token_url,
    )
    return {
        "status": "ok",
        "email": userinfo.email,
        "account_label": account_label,
    }


# ---------------------------------------------------------------------------
# Client-credentials (S2S) — Zoom
# ---------------------------------------------------------------------------


class S2SExchangeRequest(BaseModel):
    mcp_name: str
    account_label: str = "s2s"
    extra: dict[str, str] = {}    # provider-specific (Zoom: {account_id})


@router.post("/v1/oauth/{provider}/s2s/exchange")
async def s2s_exchange(
    provider: str,
    body: S2SExchangeRequest,
    user: UserContext = Depends(get_current_user),
):
    """Server-to-Server (client_credentials) exchange. Admin-only.

    Uses the ``app_credential_variants[client_credentials]`` bundle from
    the manifest (Zoom: ``zoom-s2s-app``, distinct from the user-OAuth
    ``zoom-oauth-app``). Persists under the calling admin's own account as
    ``{account_label}`` — they can then bind it to an agent as its service
    identity.
    """
    _require_admin(user)
    _validate_provider_for_mcp(body.mcp_name, provider)

    client_id, client_secret, oauth_block = _app_creds_for(
        provider, body.mcp_name, flow="client_credentials",
    )
    # S2S has no per-service scope selection — uses whatever scopes the
    # vendor app was registered for. We pass an empty list.
    provider_impl = get_provider(provider)
    try:
        token_set = await provider_impl.exchange_client_credentials(
            client_id=client_id,
            client_secret=client_secret,
            scopes=[],
            extra=dict(body.extra) or None,
        )
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e

    # Inject S2S markers into TokenSet.raw BEFORE persist. The persist
    # path (oauth_account_store.persist_oauth_account) writes
    # `raw` minus `access_token` into the token file's `extra` block.
    # The refresh worker reads `extra.flow == "client_credentials"` to
    # dispatch S2S re-exchange (instead of standard refresh) and
    # `extra.account_id` to pass back into the re-exchange call (vendor
    # response doesn't echo account_id, so we must preserve it locally).
    token_set.raw["flow"] = "client_credentials"
    account_id = body.extra.get("account_id", "")
    if account_id:
        token_set.raw["account_id"] = account_id

    # S2S has no real user identity — synthesize a UserInfo. The synthesized
    # email distinguishes multi-S2S-account installs: two Zoom S2S accounts
    # for two different Zoom organizations end up with different sentinel
    # emails (acc1@zoom-s2s vs acc2@zoom-s2s) instead of colliding on a
    # shared "s2s@zoom" string. Falls back to account_label when account_id
    # is missing (e.g. providers other than Zoom that don't need it).
    from auth.oauth_providers.base import UserInfo
    s2s_id = account_id or body.account_label
    userinfo = UserInfo(
        email=f"{s2s_id}@{provider}-s2s",
        name=f"{provider.title()} Server-to-Server ({s2s_id})",
        account_id=account_id,
        raw=dict(body.extra),
    )

    await asyncio.to_thread(
        oauth_account_store.persist_oauth_account,
        user_sub=user.sub,
        mcp_name=body.mcp_name,
        provider_id=provider,
        account_label=body.account_label,
        services=[],
        token_set=token_set,
        userinfo=userinfo,
        client_id=client_id,
        client_secret=client_secret,
        token_url=provider_impl.token_url,
    )
    return {
        "status": "ok",
        "account_label": body.account_label,
    }


# ---------------------------------------------------------------------------
# Personal Access Token (GitHub) — no OAuth dance
# ---------------------------------------------------------------------------


class PatSaveRequest(BaseModel):
    mcp_name: str
    token: str
    services: list[str] = []
    account_label: str = ""


@router.post("/v1/oauth/{provider}/pat/save")
async def pat_save(
    provider: str,
    body: PatSaveRequest,
    user: UserContext = Depends(get_current_user),
):
    """Persist a user-pasted PAT as if it were an OAuth access token.

    Validates the token by calling ``fetch_userinfo`` (so a typo'd PAT
    fails loudly with the vendor's 401 instead of silently storing junk).
    The MCP manifest must declare ``personal_access_token`` in its
    ``flows`` list; otherwise we reject.
    """
    _require_dashboard_user(user)
    _validate_provider_for_mcp(body.mcp_name, provider)

    # Gate: manifest must declare personal_access_token as a valid flow.
    manifest = mcp_registry.get_manifest(body.mcp_name)
    oauth_block = manifest.credentials.oauth or {}
    flows = oauth_block.get("flows", [])
    if "personal_access_token" not in flows:
        raise HTTPException(
            400,
            f"MCP '{body.mcp_name}' does not declare 'personal_access_token' "
            f"in credentials.oauth.flows",
        )

    if not body.token.strip():
        raise HTTPException(400, "token must be non-empty")

    provider_impl = get_provider(provider)
    try:
        token_set, userinfo = await provider_impl.exchange_personal_access_token(
            token=body.token.strip(),
            scopes=body.services,
        )
    except Exception as e:
        # Most likely: token is invalid → userinfo 401.
        raise HTTPException(401, f"PAT validation failed: {e}") from e

    if not userinfo.email:
        # Some providers (GitHub) don't return email if user hides it.
        # Fall back to account_id / name; require something stable.
        userinfo.email = userinfo.account_id or userinfo.name
        if not userinfo.email:
            raise HTTPException(
                500,
                f"{provider}: could not derive account identity from PAT — "
                f"userinfo returned no email/id/name",
            )
    account_label = body.account_label.strip() or userinfo.email

    # PATs have no client_id/client_secret; persist empty strings so the
    # refresh worker's zero-expiry guard skips them safely.
    await asyncio.to_thread(
        oauth_account_store.persist_oauth_account,
        user_sub=user.sub,
        mcp_name=body.mcp_name,
        provider_id=provider,
        account_label=account_label,
        services=body.services,
        token_set=token_set,
        userinfo=userinfo,
        client_id="",
        client_secret="",
        token_url=provider_impl.token_url,
    )
    return {
        "status": "ok",
        "email": userinfo.email,
        "account_label": account_label,
    }


# ---------------------------------------------------------------------------
# Microsoft tenant-admin consent
# ---------------------------------------------------------------------------
#
# Distinct from the standard authorize-with-`prompt=admin_consent` flow:
# `prompt=admin_consent` only forces the consent UX for the user's home
# tenant. The `/{tenant}/v2.0/adminconsent` endpoint performs a tenant-wide
# grant for all scopes registered on the OAuth app. The callback shape
# differs too — `?admin_consent=True&tenant=<guid>&state=<token>`, no
# `code`. Therefore: separate routes, separate state namespace.


class AdminConsentStartRequest(BaseModel):
    mcp_name: str
    mobile: bool = False


@router.post("/v1/oauth/microsoft/admin-consent/start")
async def admin_consent_start(
    body: AdminConsentStartRequest,
    user: UserContext = Depends(get_current_user),
):
    """Build a Microsoft tenant-admin consent URL.

    Returns ``{url}`` for the dashboard to open in a popup (web) or
    Chrome Custom Tab (mobile). The URL points at the Microsoft
    ``/{tenant}/v2.0/adminconsent`` endpoint with a fresh state token;
    the callback (``/v1/oauth/microsoft/admin-consent/callback``) consumes
    the state and returns success HTML / mobile deep link.

    Microsoft enforces tenant-admin role at the consent screen — if the
    caller isn't a tenant admin, Microsoft returns AADSTS65004. We do
    NOT pre-gate by OtoDock role; the vendor error is clear enough and
    pre-gating would prevent OtoDock managers who happen to be Microsoft
    tenant admins from using the flow.
    """
    _require_dashboard_user(user)
    _validate_provider_for_mcp(body.mcp_name, "microsoft")

    client_id, _, _ = _app_creds_for("microsoft", body.mcp_name)
    tenant_id = _microsoft_extra(body.mcp_name).get("tenant_id") or "common"

    # The admin-consent endpoint refuses /common/ — it needs a concrete
    # tenant. If MS_TENANT_ID isn't configured, reject with a clear error
    # rather than letting Microsoft reject the URL with AADSTS90011.
    if tenant_id == "common":
        raise HTTPException(
            400,
            "Microsoft tenant-admin consent requires MS_TENANT_ID to be set "
            "on the OAuth app credentials. Paste your Azure AD tenant UUID "
            "in Admin → MCP Servers → m365-mcp → OAuth App Credentials.",
        )

    redirect_uri = (
        f"{_redirect_uri('microsoft').rsplit('/callback', 1)[0]}"
        "/admin-consent/callback"
    )
    state = oauth_engine.create_admin_consent_state(
        user_sub=user.sub,
        mcp_name=body.mcp_name,
        provider_id="microsoft",
        mobile=body.mobile,
    )
    provider_impl = get_provider("microsoft")
    # build_admin_consent_url is Microsoft-only — non-ABC method on the
    # subclass. The cast is safe because we just validated provider above.
    url = provider_impl.build_admin_consent_url(  # type: ignore[attr-defined]
        tenant_id=tenant_id,
        state=state,
        redirect_uri=redirect_uri,
        client_id=client_id,
    )
    return {"url": url}


@router.get("/v1/oauth/microsoft/admin-consent/callback")
async def admin_consent_callback(
    state: str = Query(None),
    admin_consent: str = Query(None),
    tenant: str = Query(None),
    error: str = Query(None),
    error_description: str = Query(None),
):
    """Receive Microsoft's tenant-admin consent callback.

    Shape: ``?admin_consent=True&tenant=<guid>&state=<token>``. There's no
    ``code`` — admin-consent grants scopes at the tenant level without
    minting a per-user access token. We just validate state, log the
    consent (audit), and return success HTML / mobile deep link.

    The user's subsequent OAuth flow against the same tenant will succeed
    for the admin-granted scopes without further consent prompts.
    """
    if error:
        return HTMLResponse(_error_html(
            "microsoft",
            f"Provider returned: {error_description or error}",
        ))
    if not state or admin_consent != "True":
        return HTMLResponse(_error_html(
            "microsoft",
            "Missing or invalid admin_consent / state parameter",
        ))

    ctx = oauth_engine.validate_admin_consent_state(state)
    if ctx is None:
        return HTMLResponse(_error_html(
            "microsoft", "Invalid or expired admin-consent state",
        ))

    # Microsoft reports the tenant as a GUID. Parse + re-serialize so a
    # forged callback can't smuggle arbitrary text into the deep link or
    # the success page; anything non-GUID renders as unknown.
    try:
        tenant = str(uuid.UUID(tenant)) if tenant else ""
    except ValueError:
        tenant = ""

    logger.info(
        "Microsoft tenant-admin consent granted: user_sub=%s mcp=%s tenant=%s",
        ctx.user_sub[:8], ctx.mcp_name, tenant,
    )

    if ctx.mobile:
        return RedirectResponse(
            f"otodock://oauth/microsoft/admin-consent/complete?tenant={tenant}"
        )
    return HTMLResponse(_admin_consent_success_html(tenant or ""))


def _admin_consent_success_html(tenant: str) -> str:
    """Success page for the tenant-admin consent callback."""
    safe_tenant = html.escape(tenant)
    return f"""<!doctype html>
<html><head><title>Tenant consent complete</title>
<style>body{{font-family:system-ui,-apple-system,sans-serif;
text-align:center;padding:4rem 1rem;color:#222;}}
.tenant{{color:#666;font-size:.9rem;font-family:monospace;margin-top:.5rem;}}
.muted{{color:#666;margin-top:1rem;}}
</style></head>
<body>
<h1>&#x2705; Tenant consent granted</h1>
<p>Microsoft accepted the tenant-admin consent for your organization.</p>
<p class="tenant">Tenant: {safe_tenant}</p>
<p class="muted">You can close this window and return to OtoDock. Users
in your tenant can now connect Microsoft 365 with the granted scopes.</p>
<script>setTimeout(function(){{window.close();}}, 3500);</script>
</body></html>"""

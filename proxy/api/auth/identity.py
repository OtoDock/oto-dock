"""Authentication + session + self-service identity endpoints.

Login (local + OIDC), 2FA, logout, the session/me view, profile + default
agent, password/email changes, TOTP management, and password recovery.
Attaches to the shared core-auth router."""

import asyncio
import hmac
import logging
import time
from urllib.parse import parse_qs, urlparse

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import config
from auth.lan_check import check_local_auth_allowed, get_client_ip
from auth.license import check_seat_limit
from auth.password import check_password_strength, hash_password, verify_password
from auth.providers import UserContext, apply_session_cookie, create_session_jwt, get_current_user, mask_email, require_auth, validate_oauth_state
from auth.providers.local_provider import LocalAuthProvider
from auth.providers.oidc_provider import OIDCAuthProvider
from auth.rate_limiter import check_ip_rate_limit, clear_rate_limit, hit as rate_limit_hit, record_failed_attempt, record_successful_login
from auth.totp import consume_2fa_session_token, create_2fa_session_token, decrypt_recovery_codes, decrypt_totp_secret, encrypt_recovery_codes, encrypt_totp_secret, generate_recovery_codes, generate_totp_secret, get_totp_uri, hash_recovery_codes, validate_2fa_session_token, verify_recovery_code, verify_totp
from storage import database as task_store

from api.auth._common import _build_user_response
from api.auth._router import router

logger = logging.getLogger("claude-proxy")


_local_provider = LocalAuthProvider()
_oidc_provider = OIDCAuthProvider()


# Serializes recovery-code consumption (read-verify-rewrite of the encrypted
# code list) so two concurrent logins can't both spend the SAME single-use
# recovery code. Recovery-code use is rare, so one global lock is plenty.
_recovery_consume_lock = asyncio.Lock()


# OIDC login-CSRF: bind the authorization ``state`` to a per-browser cookie set
# when the flow STARTS, and require it to match at the callback — so an attacker
# can't complete an OIDC login in the victim's browser with their own code/state
# (session fixation). The ``__Host-`` prefix (when HTTPS) pins the cookie to this
# exact host + path with Secure; on plain-HTTP self-hosts the prefix is dropped
# (browsers reject ``__Host-`` without Secure). The cookie holds the FEW most
# recent in-flight states (``.``-joined — state is URL-safe base64, no dots) so
# concurrent login tabs in one browser don't clobber each other, and a 30-min
# TTL covers a slow IdP page; the binding still blocks login-CSRF because an
# attacker-chosen state was never put in the victim's cookie.
_OIDC_STATE_COOKIE_MAX = 4
_OIDC_STATE_TTL = 1800  # 30 min — generous headroom for a slow IdP login


class OAuthCallbackRequest(BaseModel):
    code: str
    state: str


class UpdateDefaultAgentRequest(BaseModel):
    default_agent: str


class LocalLoginRequest(BaseModel):
    email: str
    password: str
    turnstile_token: str | None = None


class TwoFactorRequest(BaseModel):
    totp_session_token: str
    code: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ChangeEmailRequest(BaseModel):
    new_email: str
    password: str


class TotpSetupVerifyRequest(BaseModel):
    code: str


class TotpDisableRequest(BaseModel):
    password: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class AcceptInviteRequest(BaseModel):
    token: str
    new_password: str


class UpdateProfileRequest(BaseModel):
    display_name: str


def _issue_session_cookie(response: JSONResponse, sub: str, email: str,
                          name: str, role: str, auth_provider: str = "local"):
    """Set the HttpOnly session JWT cookie on a response."""
    token = create_session_jwt(sub, email, name, role, auth_provider=auth_provider)
    apply_session_cookie(response, token)


def _check_platform_configured(user_sub: str, role: str) -> bool:
    """Whether a user-scoped session would resolve to SOME credential on any layer.

    Delegates to subscription_pool.user_can_run (own usable sub, or a borrowable
    platform API sub when Platform Auth is on) so this gate and the resolver can
    never disagree — in particular it must NOT report "configured" when the only
    platform subscriptions are admin OAuth logins, which a user may not borrow.
    """
    from services.engines import subscription_pool

    return any(
        subscription_pool.user_can_run(layer, user_sub)
        for layer in ("claude-code-cli", "codex-cli", "direct-llm")
    )


def _oidc_state_cookie_name() -> str:
    return "__Host-oidc_state" if config.COOKIE_SECURE else "oidc_state"


def _must_enroll_2fa(db_user: dict | None) -> bool:
    """Whether the admin require-2FA policy forces enrollment for this user.

    Local-password accounts without a second factor (TOTP or a registered
    passkey) when ``require_2fa`` is on. OIDC users are exempt (their IdP owns
    MFA). Enforcement is a forced enrollment screen after login (mirrors
    must_change_password) — never a silent lockout. Sync — call via
    asyncio.to_thread."""
    if not db_user or not (db_user.get("auth_provider") or "local").startswith("local"):
        return False
    if db_user.get("totp_enabled"):
        return False
    if task_store.get_all_platform_settings().get("require_2fa", "") != "1":
        return False
    from storage import webauthn_store
    return webauthn_store.count_credentials(db_user["sub"]) == 0


@router.get("/auth/config")
async def auth_config():
    """Public endpoint: auth configuration for the login page.

    No authentication required — frontend needs this before login.
    """
    setup_required = await asyncio.to_thread(task_store.count_users) == 0
    settings = await asyncio.to_thread(task_store.get_all_platform_settings)
    smtp_configured = bool(settings.get("smtp_host", ""))
    # Cloudflare Turnstile: serve the (public) site key ONLY when verification is
    # actually enabled, so the rendered widget matches backend enforcement exactly.
    from services.infra import turnstile
    tcfg = turnstile.load_config(settings)
    from services.billing import relay_client
    from api.auth.webauthn import passkey_login_mode, passkeys_enabled
    return {
        "oidc_enabled": config.OIDC_ENABLED,
        "oidc_provider_name": config.OIDC_PROVIDER_NAME,
        "turnstile_site_key": tcfg.site_key if tcfg.enabled else "",
        "setup_required": setup_required,
        "auth_provider_bypass": config.AUTH_PROVIDER_BYPASS,
        "smtp_configured": smtp_configured,
        # Emailed links (password reset, invite) need BOTH SMTP and a public
        # dashboard URL to build an absolute URL — the UI hides those flows
        # otherwise instead of sending mails with broken relative links.
        "email_links_available": smtp_configured and bool(config.DASHBOARD_PUBLIC_URL),
        "password_min_score": int(settings.get("password_min_score", "3")),
        "password_min_length": int(settings.get("password_min_length", "8")),
        # Passkeys (WebAuthn): on only for https public-URL installs — the
        # login page shows "Sign in with a passkey" when true. The mode knob
        # decides whether that button exists at all (passwordless) or passkeys
        # appear only at the 2FA step after a correct password (second_factor).
        "passkeys_enabled": passkeys_enabled(),
        "passkey_login_mode": passkey_login_mode(),
        # OtoDock connectivity + deployment. `air_gapped` (effective — forced
        # false on cloud) = this install makes no outbound calls to OtoDock.
        # `relay_base` stays server-side; only these derived booleans are exposed.
        "air_gapped": not relay_client.relay_offered(),
        "relay_available": relay_client.is_available(),
        "cloud": config.OTODOCK_CLOUD,
    }


@router.get("/auth/login")
async def auth_login(mobile: bool = False):
    """Generate OIDC authorization URL or signal that a login page should be shown.

    If AUTH_PROVIDER_BYPASS is set and OIDC is enabled, returns the OIDC URL directly
    (backward-compatible behavior for single-SSO deployments).
    Otherwise returns login_page=true so the frontend shows the local login form.
    """
    if config.AUTH_PROVIDER_BYPASS and config.OIDC_ENABLED:
        # Bypass mode: go straight to OIDC (current Authentik behavior)
        url = _oidc_provider.get_login_url(
            redirect_uri="otodock://auth/callback" if mobile else None,
            mobile=mobile,
        )
        if url:
            return {"url": url}
        raise HTTPException(status_code=503, detail="OIDC not configured")

    # Normal mode: frontend shows login page
    return {"login_page": True}


@router.get("/auth/oidc-url")
async def auth_oidc_url(request: Request, mobile: bool = False):
    """Get OIDC authorization URL (called when user clicks 'Sign in with SSO')."""
    if not config.OIDC_ENABLED:
        raise HTTPException(status_code=503, detail="OIDC not configured")
    url = _oidc_provider.get_login_url(
        redirect_uri="otodock://auth/callback" if mobile else None,
        mobile=mobile,
    )
    if not url:
        raise HTTPException(status_code=503, detail="OIDC not configured")
    resp = JSONResponse({"url": url})
    # Web flow only — the native (mobile) app completes the callback without a
    # browser cookie, so its binding is the custom-scheme redirect instead.
    if not mobile:
        state_val = parse_qs(urlparse(url).query).get("state", [""])[0]
        if state_val:
            name = _oidc_state_cookie_name()
            prior = [s for s in (request.cookies.get(name) or "").split(".") if s]
            states = (prior + [state_val])[-_OIDC_STATE_COOKIE_MAX:]
            resp.set_cookie(
                name, ".".join(states),
                max_age=_OIDC_STATE_TTL, httponly=True, secure=config.COOKIE_SECURE,
                samesite="lax", path="/",
            )
    return resp


@router.post("/auth/login/local")
async def auth_login_local(req: LocalLoginRequest, request: Request):
    """Authenticate with email + password. Sets session cookie on success."""
    client_ip = get_client_ip(request)

    # IP rate limit
    ip_ok, retry_after = check_ip_rate_limit(client_ip)
    if not ip_ok:
        raise HTTPException(
            status_code=429,
            detail=f"Too many login attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )

    # Cloudflare Turnstile bot verification (if configured). Runs BEFORE the user
    # lookup so a 403 is identical for existing and non-existing emails (no enumeration).
    settings = await asyncio.to_thread(task_store.get_all_platform_settings)
    from services.infra import turnstile
    tcfg = turnstile.load_config(settings)
    if tcfg.enabled and not await turnstile.verify_token(tcfg, req.turnstile_token or "", client_ip):
        raise HTTPException(status_code=403, detail="Bot verification failed")

    # LAN restriction check (need to look up user first for local_only flag)
    user_row = await asyncio.to_thread(task_store.get_user_by_email, req.email.strip().lower())
    if user_row and not check_local_auth_allowed(request, user_row):
        raise HTTPException(status_code=403, detail="This account can only be accessed from the local network")

    # Authenticate
    result = await _local_provider.authenticate({"email": req.email, "password": req.password})

    if not result.success:
        record_failed_attempt(client_ip, result.sub or None)
        if result.error_code == "account_locked":
            raise HTTPException(status_code=429, detail=result.error)
        raise HTTPException(status_code=401, detail=result.error)

    # Second-factor assembly. TOTP is provider-flagged; passkeys join the 2FA
    # step whenever enrolled (nice-to-have in passwordless mode, MANDATORY gate
    # in second_factor mode — there a passkey-only user must still do step 2).
    from api.auth.webauthn import passkey_login_mode, passkeys_enabled
    from storage import webauthn_store
    pk_count = 0
    if passkeys_enabled():
        pk_count = await asyncio.to_thread(webauthn_store.count_credentials, result.sub)
    factors = (["passkey"] if pk_count else []) + (["totp"] if result.requires_2fa else [])

    if result.requires_2fa:
        return {"requires_2fa": True, "totp_session_token": result.totp_session_token,
                "second_factors": factors}
    if pk_count and await asyncio.to_thread(passkey_login_mode) == "second_factor":
        return {"requires_2fa": True,
                "totp_session_token": create_2fa_session_token(result.sub),
                "second_factors": factors}

    # Full success
    record_successful_login(client_ip, result.sub)
    # Update last_login
    user = await asyncio.to_thread(task_store.get_user, result.sub)
    user_data = _build_user_response(user) if user else {}
    if await asyncio.to_thread(_must_enroll_2fa, user):
        user_data["must_enroll_2fa"] = True

    response = JSONResponse(content={"user": user_data})
    _issue_session_cookie(response, result.sub, result.email, result.name,
                          result.role, auth_provider="local")
    logger.info(f"Local login: {mask_email(result.email)} role={result.role}")
    return response


@router.post("/auth/login/2fa")
async def auth_login_2fa(req: TwoFactorRequest, request: Request):
    """Verify TOTP code after successful email+password authentication."""
    # Brute-force guard: a 6-digit TOTP is only 1M combos, so this surface MUST
    # be rate-limited. Record-before-check (``hit``) at entry is burst-safe.
    client_ip = get_client_ip(request)
    ok, retry_after = rate_limit_hit("2fa", client_ip)
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=f"Too many 2FA attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )

    sub = validate_2fa_session_token(req.totp_session_token)
    if not sub:
        raise HTTPException(status_code=401, detail="2FA session expired. Please log in again.")

    user = await asyncio.to_thread(task_store.get_user, sub)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    # Decrypt TOTP secret
    totp_enc = user.get("totp_secret_enc")
    if not totp_enc:
        raise HTTPException(status_code=400, detail="2FA not configured for this account")

    secret = decrypt_totp_secret(totp_enc)
    code = req.code.strip()

    # Try TOTP first
    if len(code) == 6 and code.isdigit() and verify_totp(secret, code):
        pass  # TOTP verified
    else:
        # Try recovery code. Serialize + RE-READ the stored codes inside the
        # lock so a concurrent request can't match the same code against a stale
        # copy: the second caller sees the already-consumed list and is rejected.
        async with _recovery_consume_lock:
            fresh = await asyncio.to_thread(task_store.get_user, sub)
            recovery_enc = fresh.get("totp_recovery_enc") if fresh else None
            matched = False
            if recovery_enc:
                hashed_codes = decrypt_recovery_codes(recovery_enc)
                matched, remaining = verify_recovery_code(code, hashed_codes)
                if matched:
                    new_enc = encrypt_recovery_codes(remaining)
                    await asyncio.to_thread(
                        task_store.update_user_auth_fields, sub, totp_recovery_enc=new_enc
                    )
                    logger.info(f"2FA recovery code used for {mask_email(user['email'])} ({len(remaining)} remaining)")
            if not matched:
                raise HTTPException(status_code=401, detail="Invalid 2FA code")

    # 2FA verified — issue session. Spend the step token so a replay can't
    # mint a second session (failed attempts above did NOT consume it).
    consume_2fa_session_token(req.totp_session_token)
    record_successful_login(client_ip, sub)
    clear_rate_limit("2fa", client_ip)
    user_data = _build_user_response(user)

    response = JSONResponse(content={"user": user_data})
    _issue_session_cookie(response, sub, user["email"], user["name"],
                          user["role"], auth_provider="local")
    logger.info(f"2FA verified: {mask_email(user['email'])}")
    return response


@router.get("/auth/callback", include_in_schema=False)
async def auth_callback_page():
    """Serve the SPA for the OAuth2 callback (browser GET redirect from Authentik)."""
    if not config.DASHBOARD_ENABLED or not config.DASHBOARD_DIST.exists():
        raise HTTPException(status_code=404, detail="Dashboard not enabled")
    return FileResponse(str(config.DASHBOARD_DIST / "index.html"))


@router.post("/auth/callback")
async def auth_callback(req: OAuthCallbackRequest, request: Request):
    """Exchange OIDC code for session. Sets HttpOnly cookie."""
    state_meta = validate_oauth_state(req.state)
    if not state_meta:
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    # Login-CSRF: for the web flow, the callback must come from the same browser
    # that started it — req.state must be one of the recent states this browser
    # was issued (the binding cookie). The native app uses the ``otodock://``
    # redirect_uri instead and carries no browser cookie.
    is_mobile = (state_meta.get("redirect_uri") or "").startswith("otodock://")
    if not is_mobile:
        bound = [s for s in (request.cookies.get(_oidc_state_cookie_name()) or "").split(".") if s]
        if not any(hmac.compare_digest(s, req.state) for s in bound):
            raise HTTPException(
                status_code=400,
                detail="Login state does not match this browser. Please try signing in again.",
            )

    result = await _oidc_provider.authenticate({
        "code": req.code,
        "redirect_uri": state_meta.get("redirect_uri"),
    })

    if not result.success:
        if result.error_code == "no_group":
            raise HTTPException(status_code=403, detail=result.error)
        raise HTTPException(status_code=502, detail=result.error)

    # Seat-limit check for new OIDC users (deployment-aware; two-stage grace).
    existing = await asyncio.to_thread(task_store.get_user, result.sub)
    if not existing:
        allowed, current, max_users = await asyncio.to_thread(check_seat_limit)
        if not allowed:
            raise HTTPException(
                status_code=402,
                detail=f"User limit reached ({current}/{max_users}). Upgrade your license to add more users.",
            )

    # Upsert user in DB
    await asyncio.to_thread(
        task_store.upsert_user, result.sub, result.email, result.name,
        result.role, display_name=result.display_name,
    )
    # Update auth_provider for this user
    await asyncio.to_thread(
        task_store.update_user_auth_fields, result.sub,
        auth_provider=result.auth_provider,
    )

    # First-time OIDC login auto-attaches the user to every
    # default-for-new-users agent. Subsequent logins short-circuit on the
    # users.default_agents_assigned bool. The flag is set by
    # assign_default_agents itself so we don't need to gate the call here
    # — the function is internally idempotent — but the explicit check
    # saves a DB round trip on every login.
    if not await asyncio.to_thread(
        task_store.is_default_agents_assigned, result.sub,
    ):
        from services.community import default_agent_assigner
        await asyncio.to_thread(
            default_agent_assigner.assign_default_agents, result.sub,
        )

    user = await asyncio.to_thread(task_store.get_user, result.sub)
    user_data = _build_user_response(user) if user else {}

    response = JSONResponse(content={"user": user_data})
    _issue_session_cookie(response, result.sub, result.email, result.name,
                          result.role, auth_provider=result.auth_provider)
    # NB: we deliberately do NOT delete the state-binding cookie here — clearing
    # it would break a second login tab still in flight in the same browser. The
    # bound states are single-use (validate_oauth_state consumed this one) and
    # the cookie self-expires via its TTL.
    logger.info(f"OIDC login: {mask_email(result.email)} role={result.role} provider={result.auth_provider}")
    return response


@router.post("/auth/logout")
async def auth_logout(request: Request):
    """Clear session cookie and return provider logout URL (if OIDC)."""
    logout_url = ""
    # Check if user was authenticated via OIDC — read from cookie before clearing
    session_cookie = request.cookies.get("session")
    if session_cookie:
        from auth.providers import validate_session_jwt
        payload = validate_session_jwt(session_cookie)
        if payload:
            auth_prov = payload.get("auth_provider", "")
            if auth_prov.startswith("oidc:") and config.OIDC_LOGOUT_URL:
                logout_url = _oidc_provider.get_logout_url(
                    post_redirect=config.DASHBOARD_PUBLIC_URL
                ) or ""
    response = JSONResponse(content={"status": "logged_out", "logout_url": logout_url})
    response.delete_cookie(key="session", path="/")
    return response


@router.get("/auth/me")
async def auth_me(user: UserContext | None = Depends(get_current_user)):
    """Return current user info from session cookie."""
    if user is None or user.is_api_key:
        raise HTTPException(status_code=401, detail="Not authenticated")

    platform_configured = _check_platform_configured(user.sub, user.role)

    # Whether THIS user has connected their OWN AI engine (a personal Claude
    # Code or Codex subscription). Distinct from platform_configured (which is
    # true if they can merely BORROW a platform sub): drives the per-user
    # "connect an AI engine" banner — the user is nudged to add their own even
    # when borrowing works, because borrowing is for agent/phone work, not their
    # personal user-scoped chats. direct-llm (relay) is deliberately excluded —
    # it's the low-latency phone path, not a tool-capable chat engine.
    def _has_own_engine(sub: str) -> bool:
        from storage import subscription_store
        rows = subscription_store.list_personal(None, sub)
        return any(r.get("layer") in ("claude-code-cli", "codex-cli") for r in rows)

    has_own_engine = await asyncio.to_thread(_has_own_engine, user.sub)

    # Get fresh DB fields for totp/owner/must_change_password
    db_user = await asyncio.to_thread(task_store.get_user, user.sub)
    totp_enabled = bool(db_user.get("totp_enabled")) if db_user else False
    is_owner = bool(db_user.get("is_owner")) if db_user else False
    must_change_password = bool(db_user.get("must_change_password")) if db_user else False
    must_enroll_2fa = await asyncio.to_thread(_must_enroll_2fa, db_user)

    # Surface user-facing feature flags so the dashboard can hide
    # the Remote Machines section when the admin has disabled the feature
    # (or the build ships without it entirely).
    allow_user_paired = await asyncio.to_thread(
        task_store.get_platform_setting, "allow_user_paired_machines",
    )
    from ws.satellite import satellite_source_available
    from core import execution_mode
    interactive_enabled = await asyncio.to_thread(
        execution_mode.is_interactive_enabled,
    )
    feature_flags = {
        "allow_user_paired_machines": (allow_user_paired or "") != "0",
        "remote_machines_available": satellite_source_available(),
        # Mirrors the global interactive kill-switch so the dashboard hides
        # the interactive-terminal toggles when sessions always run headless.
        "interactive_terminal_enabled": interactive_enabled,
    }

    return {
        "user": {
            "sub": user.sub,
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "agents": user.agents,
            "default_agent": user.default_agent,
            "display_name": user.display_name,
            "agent_roles": user.agent_roles,
            "platform_configured": platform_configured,
            "has_own_engine": has_own_engine,
            "auth_provider": user.auth_provider,
            "totp_enabled": totp_enabled,
            "is_owner": is_owner,
            "must_change_password": must_change_password,
            "must_enroll_2fa": must_enroll_2fa,
            "feature_flags": feature_flags,
        }
    }


@router.put("/v1/users/me/profile")
async def update_my_profile(
    req: UpdateProfileRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Update current user's display name."""
    u = require_auth(user)
    if u.is_api_key:
        raise HTTPException(403, "Dashboard only")
    task_store.update_user_display_name(u.sub, req.display_name.strip())
    return {"status": "ok", "display_name": req.display_name.strip()}


@router.put("/v1/users/me/default-agent")
async def set_my_default_agent(
    req: UpdateDefaultAgentRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Set default agent for the current user (self-service)."""
    u = require_auth(user)
    agent = req.default_agent.strip()
    if agent:
        if not u.can_access_agent(agent):
            raise HTTPException(
                status_code=400,
                detail=f"Agent '{agent}' is not accessible to you",
            )
    task_store.set_user_default_agent(u.sub, agent)
    logger.info(f"User {mask_email(u.email)} set their default agent: {agent or '(none)'}")
    return {"status": "updated", "default_agent": agent}


@router.put("/v1/users/me/password")
async def change_my_password(
    req: ChangePasswordRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Change own password. Requires current password."""
    u = require_auth(user)
    if u.is_api_key:
        raise HTTPException(403, "Dashboard only")

    db_user = await asyncio.to_thread(task_store.get_user, u.sub)
    if not db_user or not db_user.get("password_hash"):
        raise HTTPException(400, "No password set for this account")

    if not verify_password(req.current_password, db_user["password_hash"]):
        raise HTTPException(401, "Current password is incorrect")

    ok, msg, _ = check_password_strength(req.new_password)
    if not ok:
        raise HTTPException(400, msg)

    pw_hash = hash_password(req.new_password)
    await asyncio.to_thread(task_store.set_user_password, u.sub, pw_hash)
    logger.info(f"User {mask_email(u.email)} changed their password")
    return {"status": "ok"}


@router.put("/v1/users/me/email")
async def change_my_email(
    req: ChangeEmailRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Change own email. Requires password confirmation."""
    u = require_auth(user)
    if u.is_api_key:
        raise HTTPException(403, "Dashboard only")

    db_user = await asyncio.to_thread(task_store.get_user, u.sub)
    if not db_user or not db_user.get("password_hash"):
        raise HTTPException(400, "Cannot change email for OIDC accounts")

    if not verify_password(req.password, db_user["password_hash"]):
        raise HTTPException(401, "Password is incorrect")

    try:
        await asyncio.to_thread(task_store.update_user_email, u.sub, req.new_email.strip().lower())
    except ValueError as e:
        raise HTTPException(409, str(e))

    logger.info(f"User {mask_email(u.email)} changed email to {mask_email(req.new_email)}")
    return {"status": "ok"}


@router.post("/v1/users/me/totp/setup")
async def totp_setup(user: UserContext | None = Depends(get_current_user)):
    """Generate TOTP secret and recovery codes. Does not enable 2FA yet."""
    u = require_auth(user)
    if u.is_api_key:
        raise HTTPException(403, "Dashboard only")

    secret = generate_totp_secret()
    qr_uri = get_totp_uri(secret, u.email)
    recovery_codes = generate_recovery_codes()

    # Store encrypted secret (but don't enable yet — user must verify first)
    enc_secret = encrypt_totp_secret(secret)
    enc_recovery = encrypt_recovery_codes(hash_recovery_codes(recovery_codes))
    await asyncio.to_thread(
        task_store.update_user_auth_fields, u.sub,
        totp_secret_enc=enc_secret, totp_recovery_enc=enc_recovery,
    )

    return {
        "secret": secret,
        "qr_uri": qr_uri,
        "recovery_codes": recovery_codes,
    }


@router.post("/v1/users/me/totp/verify")
async def totp_verify(
    req: TotpSetupVerifyRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Verify a TOTP code to complete 2FA setup."""
    u = require_auth(user)
    if u.is_api_key:
        raise HTTPException(403, "Dashboard only")

    db_user = await asyncio.to_thread(task_store.get_user, u.sub)
    if not db_user or not db_user.get("totp_secret_enc"):
        raise HTTPException(400, "Run /totp/setup first")

    secret = decrypt_totp_secret(db_user["totp_secret_enc"])
    if not verify_totp(secret, req.code.strip()):
        raise HTTPException(400, "Invalid code. Try again.")

    # Enable 2FA
    await asyncio.to_thread(
        task_store.update_user_auth_fields, u.sub, totp_enabled=True,
    )
    logger.info(f"User {mask_email(u.email)} enabled 2FA")
    return {"status": "enabled"}


@router.delete("/v1/users/me/totp")
async def totp_disable(
    req: TotpDisableRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Disable 2FA. Requires password confirmation."""
    u = require_auth(user)
    if u.is_api_key:
        raise HTTPException(403, "Dashboard only")

    db_user = await asyncio.to_thread(task_store.get_user, u.sub)
    if not db_user or not db_user.get("password_hash"):
        raise HTTPException(400, "Cannot disable 2FA without a password")

    if not verify_password(req.password, db_user["password_hash"]):
        raise HTTPException(401, "Password is incorrect")

    await asyncio.to_thread(
        task_store.update_user_auth_fields, u.sub,
        totp_secret_enc=None, totp_recovery_enc=None, totp_enabled=False,
    )
    logger.info(f"User {mask_email(u.email)} disabled 2FA")
    return {"status": "disabled"}


@router.post("/auth/forgot-password")
async def forgot_password(req: ForgotPasswordRequest, request: Request):
    """Request a password reset email. Always returns 200 (no user enumeration)."""
    from services.notifications.smtp import is_smtp_configured, send_password_reset_email
    import jwt as pyjwt

    # Throttle per source IP AND per target email so this can't be used to bomb a
    # victim's inbox or sweep emails. Both keys are independent of user existence,
    # so a 429 is not an enumeration oracle.
    email = req.email.strip().lower()
    client_ip = get_client_ip(request)
    for key in (client_ip, f"email:{email}"):
        ok, retry_after = rate_limit_hit("forgot", key)
        if not ok:
            raise HTTPException(
                status_code=429,
                detail=f"Too many reset requests. Try again in {retry_after} seconds.",
                headers={"Retry-After": str(retry_after)},
            )

    # A reset link must be absolute — without a public URL the email would
    # carry a broken relative link (and deriving the base from request headers
    # is a reset-poisoning vector), so skip sending entirely.
    if not await asyncio.to_thread(is_smtp_configured) or not config.DASHBOARD_PUBLIC_URL:
        return {"status": "ok", "message": "If your email is registered and SMTP is configured, you'll receive a reset link."}

    user = await asyncio.to_thread(task_store.get_user_by_email, email)

    if user and user.get("auth_provider", "").startswith("local") and user.get("password_hash"):
        # Generate reset token (JWT, 1hr expiry)
        token = pyjwt.encode(
            {"sub": user["sub"], "purpose": "password_reset",
             "iat": int(time.time()), "exp": int(time.time()) + 3600},
            config.JWT_SECRET, algorithm="HS256",
        )
        reset_url = f"{config.DASHBOARD_PUBLIC_URL}/reset-password?token={token}"
        await asyncio.to_thread(send_password_reset_email, email, reset_url)

    # Always return success (prevent enumeration)
    return {"status": "ok", "message": "If your email is registered, you'll receive a reset link."}


@router.post("/auth/reset-password")
async def reset_password(req: ResetPasswordRequest, request: Request):
    """Reset password using a token from the forgot-password email."""
    import jwt as pyjwt

    # Per-IP throttle so the reset endpoint can't be hammered (token guessing /
    # strength-check abuse). Burst-safe record-before-check.
    ok, retry_after = rate_limit_hit("reset", get_client_ip(request))
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=f"Too many reset attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        payload = pyjwt.decode(req.token, config.JWT_SECRET, algorithms=["HS256"])
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(400, "Reset link has expired. Please request a new one.")
    except pyjwt.InvalidTokenError:
        raise HTTPException(400, "Invalid reset link.")

    if payload.get("purpose") != "password_reset":
        raise HTTPException(400, "Invalid reset link.")

    sub = payload.get("sub", "")
    user = await asyncio.to_thread(task_store.get_user, sub)
    if not user:
        raise HTTPException(400, "Invalid reset link.")

    # Check single-use: if password was changed after token was issued
    changed_at = user.get("password_changed_at", "")
    if changed_at:
        try:
            from datetime import datetime
            changed_ts = datetime.fromisoformat(changed_at).timestamp()
            if changed_ts > payload.get("iat", 0):
                raise HTTPException(400, "This reset link has already been used.")
        except (ValueError, TypeError):
            pass

    ok, msg, _ = check_password_strength(req.new_password)
    if not ok:
        raise HTTPException(400, msg)

    pw_hash = hash_password(req.new_password)
    await asyncio.to_thread(task_store.set_user_password, sub, pw_hash)
    logger.info(f"Password reset completed for {mask_email(user['email'])}")
    return {"status": "ok"}


@router.post("/auth/accept-invite")
async def accept_invite(req: AcceptInviteRequest, request: Request):
    """Activate an invited account: set the initial password from an invite link.

    Public, token-authenticated (signed invite JWT minted by admin user
    creation). Single-use is structural: only valid while the account has no
    password, and accepting sets one — so a replayed token (or one raced by an
    admin password reset) is dead."""
    import jwt as pyjwt

    # Per-IP throttle, same posture as the reset endpoint (token guessing /
    # strength-check abuse). Burst-safe record-before-check.
    ok, retry_after = rate_limit_hit("invite", get_client_ip(request))
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        payload = pyjwt.decode(req.token, config.JWT_SECRET, algorithms=["HS256"])
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(400, "This invite link has expired. Ask your admin for a new one.")
    except pyjwt.InvalidTokenError:
        raise HTTPException(400, "Invalid invite link.")

    if payload.get("purpose") != "invite":
        raise HTTPException(400, "Invalid invite link.")

    user = await asyncio.to_thread(task_store.get_user, payload.get("sub", ""))
    if not user or not (user.get("auth_provider") or "").startswith("local"):
        raise HTTPException(400, "Invalid invite link.")

    if user.get("password_hash"):
        raise HTTPException(400, "This invite has already been used.")

    ok, msg, _ = check_password_strength(req.new_password)
    if not ok:
        raise HTTPException(400, msg)

    pw_hash = hash_password(req.new_password)
    await asyncio.to_thread(task_store.set_user_password, user["sub"], pw_hash)
    logger.info(f"Invite accepted for {mask_email(user['email'])}")
    return {"status": "ok", "email": user["email"]}

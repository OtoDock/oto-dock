"""Authentication + session + self-service identity endpoints.

Login (local + OIDC), 2FA, logout, the session/me view, profile + default
agent, password/email changes, TOTP management, and password recovery.
Attaches to the shared core-auth router."""

import asyncio
import hmac
import logging
import re
import time
from urllib.parse import parse_qs, urlparse

from fastapi import Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import config
from auth.lan_check import check_local_auth_allowed, get_client_ip
from auth.license import check_seat_limit
from auth.password import HashBusy, check_password_strength_async, hash_password_async, verify_password_async
from auth.providers import UserContext, apply_session_cookie, create_session_jwt, get_current_user, mask_email, require_auth, validate_oauth_state
from auth.providers.local_provider import LocalAuthProvider
from auth.providers.oidc_provider import OIDCAuthProvider, ensure_oidc_discovery
from auth import rate_limiter
from auth.rate_limiter import clear_rate_limit, hit as rate_limit_hit, record_successful_login
from auth.totp import consume_2fa_session_token, create_2fa_session_token, decrypt_recovery_codes, decrypt_totp_secret, encrypt_recovery_codes, encrypt_totp_secret, generate_recovery_codes, generate_totp_secret, get_totp_uri, hash_recovery_codes, validate_2fa_session_token, verify_recovery_code, verify_totp
from services.agents import offboarding_sessions
from storage import database as task_store
from storage.pg import run_db

from api.auth._common import build_feature_flags, user_payload
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


class TotpSetupRequest(BaseModel):
    # Only consulted when 2FA is ALREADY enabled (reconfiguration) — the
    # initial enable flow sends no body. Guards against a hijacked session
    # silently rotating a victim's live 2FA secret + recovery codes.
    password: str = ""


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
                          name: str, role: str, auth_provider: str = "local", *,
                          expiry_hours: int | None = None):
    """Set the HttpOnly session JWT cookie on a response. ``expiry_hours``
    (read off the loop with ``config.get_jwt_expiry_hours``) saves the two
    settings reads the JWT and the cookie would each make on the loop."""
    token = create_session_jwt(sub, email, name, role, auth_provider=auth_provider,
                               expiry_hours=expiry_hours)
    apply_session_cookie(response, token, expiry_hours=expiry_hours)


async def _login_response(user_row: dict, auth_provider: str) -> JSONResponse:
    """The answer to a full login: the user payload, the session cookie and
    (password and passkey logins) the trusted-device cookie; every store read
    on the DB executor."""
    def _job():
        return user_payload(user_row), config.get_jwt_expiry_hours()

    user_data, hours = await run_db(_job)
    response = JSONResponse(content={"user": user_data})
    _issue_session_cookie(response, user_row["sub"], user_row["email"], user_row["name"],
                          user_row["role"], auth_provider=auth_provider, expiry_hours=hours)
    if auth_provider == "local":
        _issue_device_cookie(response, user_row["sub"])
    return response


# The trusted-device cookie: set on every full password
# or passkey login; that browser's later password logins for the same account
# skip the account tarpit (``auth/rate_limiter.py``, "Trusted devices"). It
# grants nothing on its own, so logout keeps it; a password change voids it.
def _device_cookie_name() -> str:
    return "__Host-otodock_device" if config.COOKIE_SECURE else "otodock_device"


def _issue_device_cookie(response: JSONResponse, sub: str) -> None:
    response.set_cookie(
        _device_cookie_name(), rate_limiter.mint_device_token(sub),
        max_age=rate_limiter.DEVICE_TOKEN_DAYS * 86400, httponly=True,
        secure=config.COOKIE_SECURE, samesite="lax", path="/",
    )


def _hash_busy() -> HTTPException:
    return HTTPException(503, "Too many sign-ins at once. Try again in a few seconds.",
                         headers={"Retry-After": "5", "Connection": "close"})


async def _password_matches(plain: str, hashed: str) -> bool:
    try:
        return await verify_password_async(plain, hashed)
    except HashBusy:
        raise _hash_busy()


async def _new_password_hash(plain: str) -> str:
    """The strength check (400 with its reason), then the hash."""
    ok, msg, _ = await check_password_strength_async(plain)
    if not ok:
        raise HTTPException(400, msg)
    try:
        return await hash_password_async(plain)
    except HashBusy:
        raise _hash_busy()


def _check_platform_configured(user_sub: str, role: str) -> bool:
    """Whether a user-scoped session would resolve to SOME credential on any layer.

    Delegates to subscription_pool.user_can_run (own usable sub, or a borrowable
    platform API sub when Platform Auth is on) so this gate and the resolver can
    never disagree — in particular it must NOT report "configured" when the only
    platform subscriptions are admin OAuth logins, which a user may not borrow.
    """
    from core.session.session_manager import valid_execution_paths
    from services.engines import subscription_pool

    return any(
        subscription_pool.user_can_run(layer, user_sub)
        for layer in sorted(valid_execution_paths())
    )


def _oidc_state_cookie_name() -> str:
    return "__Host-oidc_state" if config.COOKIE_SECURE else "oidc_state"


@router.get("/auth/config")
async def auth_config():
    """Public endpoint: auth configuration for the login page.

    No authentication required: the frontend needs this before login. Every
    read (the users count, the settings, the license behind the relay
    answers) runs as ONE job on the DB executor, the settings read once.
    """
    return await run_db(_auth_config_payload)


def _auth_config_payload() -> dict:
    setup_required = task_store.count_users() == 0
    settings = task_store.get_all_platform_settings()
    smtp_configured = bool(settings.get("smtp_host", ""))
    # Cloudflare Turnstile: serve the (public) site key ONLY when verification is
    # actually enabled, so the rendered widget matches backend enforcement exactly.
    from services.infra import turnstile
    tcfg = turnstile.load_config(settings)
    from services.billing import relay_client
    from api.auth.webauthn import passkey_rp_host, passkeys_enabled
    mode = settings.get("passkey_login_mode", "")
    relay_offered = relay_client.relay_offered()
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
        "passkey_login_mode": mode if mode in ("passwordless", "second_factor") else "passwordless",
        # The RP hostname passkeys are bound to (the public dashboard URL's
        # host). A browser on any OTHER origin (localhost, LAN IP) cannot run
        # the ceremony, so the login page hides its passkey buttons and points
        # at this host instead of letting the browser fail with its own
        # security error.
        "passkey_rp_host": passkey_rp_host() if passkeys_enabled() else "",
        # OtoDock connectivity + deployment. `air_gapped` (effective — forced
        # false on cloud) = this install makes no outbound calls to OtoDock.
        # `relay_base` stays server-side; only these derived booleans are exposed.
        "air_gapped": not relay_offered,
        "relay_available": relay_client.is_available(),
        "cloud": config.OTODOCK_CLOUD,
    }


@router.get("/auth/login")
async def auth_login(request: Request, mobile: bool = False):
    """Generate OIDC authorization URL or signal that a login page should be shown.

    If AUTH_PROVIDER_BYPASS is set and OIDC is enabled, returns the OIDC URL directly
    (backward-compatible behavior for single-SSO deployments).
    Otherwise returns login_page=true so the frontend shows the local login form.
    """
    if config.AUTH_PROVIDER_BYPASS and config.OIDC_ENABLED:
        # Bypass mode: go straight to OIDC (current Authentik behavior)
        await ensure_oidc_discovery()
        url = _oidc_provider.get_login_url(
            redirect_uri="otodock://auth/callback" if mobile else None,
            mobile=mobile,
        )
        if url:
            resp = JSONResponse({"url": url})
            _bind_oidc_state(request, resp, url)
            return resp
        raise HTTPException(status_code=503, detail="OIDC not configured")

    # Normal mode: frontend shows login page
    return {"login_page": True}


def _bind_oidc_state(request: Request, resp: JSONResponse, url: str) -> None:
    """The login-CSRF binding: the state the URL carries joins the browser's
    ring of recent states (the note above the cookie name). Every state is
    bound, the native app's included: its WebView fetches the URL itself, so
    the cookie lands in the WebView's jar, and the deep link reloads that
    same WebView at the callback."""
    state_val = parse_qs(urlparse(url).query).get("state", [""])[0]
    if not state_val:
        return
    name = _oidc_state_cookie_name()
    prior = [s for s in (request.cookies.get(name) or "").split(".") if s]
    states = (prior + [state_val])[-_OIDC_STATE_COOKIE_MAX:]
    resp.set_cookie(
        name, ".".join(states),
        max_age=_OIDC_STATE_TTL, httponly=True, secure=config.COOKIE_SECURE,
        samesite="lax", path="/",
    )


@router.get("/auth/oidc-url")
async def auth_oidc_url(request: Request, mobile: bool = False):
    """Get OIDC authorization URL (called when user clicks 'Sign in with SSO')."""
    if not config.OIDC_ENABLED:
        raise HTTPException(status_code=503, detail="OIDC not configured")
    await ensure_oidc_discovery()
    url = _oidc_provider.get_login_url(
        redirect_uri="otodock://auth/callback" if mobile else None,
        mobile=mobile,
    )
    if not url:
        raise HTTPException(status_code=503, detail="OIDC not configured")
    resp = JSONResponse({"url": url})
    _bind_oidc_state(request, resp, url)
    return resp


# A same-origin path for the confirm flow to come back to: one leading
# slash, no scheme, no backslash, no whitespace, at most 512 chars. The
# encoded separators are refused below (an open redirect otherwise).
_RETURN_TO_RE = re.compile(r"^/(?!/)[^\\\s]{0,511}$")


def _safe_return_to(raw: str) -> str:
    value = (raw or "").strip()
    low = value.lower()
    if not _RETURN_TO_RE.match(value) or "://" in low or "%2f%2f" in low or "%5c" in low:
        raise HTTPException(status_code=400, detail="return_to must be a path on this site")
    return value


@router.get("/auth/confirm/oidc-url")
async def auth_confirm_oidc_url(request: Request, return_to: str = "", mobile: bool = False,
                                user: UserContext | None = Depends(get_current_user)):
    """The identity-provider confirm (SHARING.md "The confirm"): the same
    login URL a sign-in uses, with a confirm-purpose state, so the provider
    answers whether this browser is signed in there as the same account and
    the callback hands back a confirm, never a session. A new login is asked
    for only under ``OIDC_CONFIRM_FRESH_LOGIN``. A cookie principal whose
    account signed in through the provider; paced on the ``confirm`` bucket
    like the other confirm methods."""
    from auth.providers import require_human
    u = require_human(user)
    if not config.OIDC_ENABLED:
        raise HTTPException(status_code=503, detail="OIDC not configured")
    db_user = await asyncio.to_thread(task_store.get_user, u.sub)
    if not db_user or not str(db_user.get("auth_provider") or "").startswith("oidc:"):
        raise HTTPException(status_code=400,
                            detail="This account did not sign in through the identity provider")
    ok, retry_after = rate_limit_hit("confirm", u.sub)
    if not ok:
        raise HTTPException(429, f"Too many attempts. Try again in {retry_after} seconds.",
                            headers={"Retry-After": str(retry_after)})
    path = _safe_return_to(return_to) if return_to else "/"
    await ensure_oidc_discovery()
    url = _oidc_provider.get_login_url(
        redirect_uri="otodock://auth/callback" if mobile else None,
        mobile=mobile, prompt_login=config.OIDC_CONFIRM_FRESH_LOGIN,
        purpose="confirm", sub=u.sub, return_to=path,
    )
    if not url:
        raise HTTPException(status_code=503, detail="OIDC not configured")
    resp = JSONResponse({"url": url})
    _bind_oidc_state(request, resp, url)
    return resp


def _require_fresh_login(claims: dict, state_meta: dict, email: str) -> None:
    """The strict confirm (``OIDC_CONFIRM_FRESH_LOGIN``): the ID token's
    ``auth_time`` must be at or after the instant the confirm was started,
    with a minute of slack for clocks; the state's own TTL bounds the
    window. A provider that skips the login for a signed-in person answers
    with the old login time (Authentik's defaults, the VM pass 2026-09-18):
    the refusal says how old it was and what makes the next one fresh."""
    auth_time = claims.get("auth_time")
    started = float(state_meta.get("created_at") or 0)
    if auth_time is None:
        if config.OIDC_CONFIRM_REQUIRE_AUTH_TIME:
            logger.warning(f"OIDC confirm refused for {mask_email(email)}: no auth_time in the ID token")
            raise HTTPException(status_code=403,
                                detail="The identity provider did not say when you logged in")
        return
    try:
        at = float(auth_time)
        fresh = at >= started - 60
    except (TypeError, ValueError):
        at, fresh = 0.0, False
    if fresh:
        return
    ago = max(0, int((started - at) / 60)) if at else 0
    logger.warning(f"OIDC confirm refused for {mask_email(email)}: the login was not fresh "
                   f"(auth_time {at:.0f}, confirm started {started:.0f}, {ago} min earlier)")
    raise HTTPException(
        status_code=403,
        detail=(f"The login was not fresh: {config.OIDC_PROVIDER_NAME or 'the identity provider'} "
                f"let you through without a new login (your last one was {ago} minute(s) "
                f"earlier). Sign out of it, then try again."))


async def _finish_oidc_confirm(state_meta: dict, code: str) -> JSONResponse:
    """The confirm half of the callback: the same code exchange as a login,
    then the proof that the provider answered for the account the confirm
    was started for (and, under ``OIDC_CONFIRM_FRESH_LOGIN``, that the login
    is newer than the click). No session is issued and nothing is upserted;
    the answer is a one-shot confirm token the share routes consume
    (``auth/confirm.py``)."""
    from auth import confirm
    result = await _oidc_provider.authenticate({
        "code": code, "redirect_uri": state_meta.get("redirect_uri"),
    })
    if not result.success:
        raise HTTPException(status_code=403 if result.error_code == "no_group" else 502,
                            detail=result.error)
    expected = state_meta.get("sub") or ""
    if not expected or result.sub != expected:
        raise HTTPException(status_code=403, detail="Signed in as a different account")
    claims = result.id_claims or {}
    if claims:
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if config.OIDC_CLIENT_ID not in auds:
            raise HTTPException(status_code=403, detail="The login was not for this platform")
        if str(claims.get("sub") or "") != result.sub:
            raise HTTPException(status_code=403, detail="Signed in as a different account")
    if config.OIDC_CONFIRM_FRESH_LOGIN:
        _require_fresh_login(claims, state_meta, result.email)
    token = confirm.mint_confirm_token(result.sub)
    logger.info(f"OIDC confirm: {mask_email(result.email)} provider={result.auth_provider}")
    return JSONResponse(content={"purpose": "confirm", "confirm_token": token,
                                 "return_to": state_meta.get("return_to") or "/"})


@router.post("/auth/login/local")
async def auth_login_local(req: LocalLoginRequest, request: Request):
    """Authenticate with email + password. Sets session cookie on success."""
    client_ip = get_client_ip(request)

    # The address's attempt is counted before anything awaits, so a burst
    # from one address cannot all pass the check while the hashes run; one
    # that proves the password or is refused by the tarpit is given back.
    ip_ok, retry_after = rate_limiter.hit_ip_login(client_ip)
    if not ip_ok:
        raise HTTPException(
            status_code=429,
            detail=f"Too many login attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )

    # Cloudflare Turnstile bot verification (if configured). Runs BEFORE the user
    # lookup so a 403 is identical for existing and non-existing emails (no enumeration).
    settings = await run_db(task_store.get_all_platform_settings)
    from services.infra import turnstile
    tcfg = turnstile.load_config(settings)
    if tcfg.enabled and not await turnstile.verify_token(tcfg, req.turnstile_token or "", client_ip):
        raise HTTPException(status_code=403, detail="Bot verification failed")

    device = rate_limiter.device_token_claims(request.cookies.get(_device_cookie_name(), ""))
    try:
        result = await _local_provider.authenticate(
            {"email": req.email, "password": req.password}, device=device)
    except HashBusy:
        rate_limiter.release_ip_attempt(client_ip)
        raise _hash_busy()

    if not result.success:
        if result.error_code == "account_locked":
            # The tarpit refused it before any password was checked.
            rate_limiter.release_ip_attempt(client_ip)
            raise HTTPException(status_code=429, detail=result.error)
        raise HTTPException(status_code=401, detail=result.error)
    # The password was right: whatever follows, this attempt guessed nothing.
    rate_limiter.release_ip_attempt(client_ip)

    # LAN restriction — checked ONLY after the credentials verify, so the
    # distinctive "local network" 403 can no longer be used pre-auth as an
    # oracle for which emails are local_only accounts. The restriction itself
    # is unchanged: a valid-credential remote login to a local_only account is
    # still refused (before any session token / 2FA step is handed out).
    user_row = await run_db(task_store.get_user, result.sub)
    if user_row and not check_local_auth_allowed(request, user_row):
        raise HTTPException(status_code=403, detail="This account can only be accessed from the local network")

    # Second-factor assembly. TOTP is provider-flagged; passkeys join the 2FA
    # step whenever enrolled. A passkey-only account must still do step 2 in
    # second_factor mode, and in either mode while Require 2FA is on: there
    # the passkey is the account's second factor, so the password alone must
    # not open a session.
    from api.auth.webauthn import passkey_login_mode, passkeys_enabled
    from storage.identity import webauthn_store

    def _second_factor_job() -> tuple[int, str]:
        if not passkeys_enabled():
            return 0, ""
        return webauthn_store.count_credentials(result.sub), passkey_login_mode()

    pk_count, pk_mode = await run_db(_second_factor_job)
    factors = (["passkey"] if pk_count else []) + (["totp"] if result.requires_2fa else [])
    require_2fa_on = settings.get("require_2fa", "") == "1"

    if result.requires_2fa:
        return {"requires_2fa": True, "totp_session_token": result.totp_session_token,
                "second_factors": factors}
    if pk_count and (pk_mode == "second_factor" or require_2fa_on):
        return {"requires_2fa": True,
                "totp_session_token": create_2fa_session_token(result.sub),
                "second_factors": factors}

    # Full success
    await record_successful_login(client_ip, result.sub)
    user = await run_db(task_store.get_user, result.sub)
    response = await _login_response(user or user_row, "local")
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

    user = await run_db(task_store.get_user, sub)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    # Re-check the LAN restriction at step 2: step 1 verified it, but the step
    # token is portable for its 5-minute TTL — a local_only login must not be
    # completable from off-LAN with a token minted on-LAN.
    if not check_local_auth_allowed(request, user):
        raise HTTPException(status_code=403, detail="This account can only be accessed from the local network")

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
            fresh = await run_db(task_store.get_user, sub)
            recovery_enc = fresh.get("totp_recovery_enc") if fresh else None
            matched = False
            if recovery_enc:
                hashed_codes = decrypt_recovery_codes(recovery_enc)
                matched, remaining = verify_recovery_code(code, hashed_codes)
                if matched:
                    new_enc = encrypt_recovery_codes(remaining)
                    await run_db(
                        task_store.update_user_auth_fields, sub, totp_recovery_enc=new_enc
                    )
                    logger.info(f"2FA recovery code used for {mask_email(user['email'])} ({len(remaining)} remaining)")
            if not matched:
                raise HTTPException(status_code=401, detail="Invalid 2FA code")

    # 2FA verified — issue session. Spend the step token so a replay can't
    # mint a second session (failed attempts above did NOT consume it).
    consume_2fa_session_token(req.totp_session_token)
    await record_successful_login(client_ip, sub)
    clear_rate_limit("2fa", client_ip)
    response = await _login_response(user, "local")
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

    # Login-CSRF: the callback must come from the browser that started the
    # flow (the native app's WebView included): req.state must be one of the
    # recent states this browser was issued (the binding cookie).
    bound = [s for s in (request.cookies.get(_oidc_state_cookie_name()) or "").split(".") if s]
    if not any(hmac.compare_digest(s, req.state) for s in bound):
        raise HTTPException(
            status_code=400,
            detail="Login state does not match this browser. Please try signing in again.",
        )

    # A state serves the purpose it was minted for and no other: a confirm
    # never issues a session, a login never mints a confirm token.
    purpose = state_meta.get("purpose") or "login"
    if purpose == "confirm":
        return await _finish_oidc_confirm(state_meta, req.code)
    if purpose != "login":
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    result = await _oidc_provider.authenticate({
        "code": req.code,
        "redirect_uri": state_meta.get("redirect_uri"),
    })

    if not result.success:
        if result.error_code == "no_group":
            raise HTTPException(status_code=403, detail=result.error)
        raise HTTPException(status_code=502, detail=result.error)

    # Seat-limit check for new OIDC users (deployment-aware; two-stage grace).
    existing = await run_db(task_store.get_user, result.sub)
    if not existing:
        allowed, current, max_users = await asyncio.to_thread(check_seat_limit)
        if not allowed:
            raise HTTPException(
                status_code=402,
                detail=f"User limit reached ({current}/{max_users}). Upgrade your license to add more users.",
            )

    # Upsert user in DB. The identity provider may have changed the platform
    # role (its groups): a change gets the rule an admin's change does
    # (``apply_platform_role_change``), with no person as its actor.
    rows_before = await run_db(task_store.get_user_agent_roles, result.sub) if existing else {}
    prev_role = await run_db(
        task_store.upsert_user, result.sub, result.email, result.name,
        result.role, display_name=result.display_name,
    )
    if prev_role is not None and prev_role != result.role:
        from api.auth.admin_users import apply_platform_role_change
        try:
            await apply_platform_role_change(result.sub, existing, result.role, rows_before, "")
        except Exception:
            logger.exception("OIDC login: the role change of %s was not applied in full",
                             mask_email(result.email))
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

    user = await run_db(task_store.get_user, result.sub)
    response = await _login_response(user, result.auth_provider)
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

    if user.render_app:
        # The platform's own headless render (auth/render_principal.py): the
        # shell's guards must let the app page through (a configured
        # platform, nothing to change or enrol), and nothing personal is read.
        return {"user": {
            "sub": user.sub, "email": user.email, "name": user.name, "username": "",
            "role": user.role, "agents": user.agents, "default_agent": "",
            "display_name": user.display_name, "agent_roles": user.agent_roles,
            "platform_configured": True, "has_own_engine": False,
            "auth_provider": "render", "totp_enabled": False, "is_owner": False,
            "must_change_password": False, "must_enroll_2fa": False, "render": True,
            "feature_flags": await asyncio.to_thread(build_feature_flags),
        }}

    # Whether THIS user has connected their OWN AI engine (a personal
    # subscription on a CODING engine — Claude Code, Codex). Distinct from
    # platform_configured (which is true if they can merely BORROW a platform
    # sub): drives the per-user "connect an AI engine" banner — the user is
    # nudged to add their own even when borrowing works, because borrowing is
    # for agent/phone work, not their personal user-scoped chats. A supporting
    # engine (Direct LLM, the low-latency phone path) is deliberately not
    # counted — ``identity.role``; a row on an engine no longer registered is
    # skipped, as it always was.
    def _has_own_engine(sub: str) -> bool:
        from core.session.session_manager import get_layer_capabilities
        from storage.billing import subscription_store

        def _coding(layer: str) -> bool:
            caps = get_layer_capabilities(layer)
            return caps is not None and caps.identity.role == "coding"

        rows = subscription_store.list_personal(None, sub)
        return any(_coding(r.get("layer") or "") for r in rows)

    # Every read in ONE job on the DB executor. The forced password
    # change and 2FA enrolment come from the one gate rule the server's
    # refusals and the dashboard socket apply (``auth_gate``). The feature
    # flags are shared with every login payload (``_common.build_feature_flags``):
    # the flags must be identical on every path the dashboard stores as its
    # user object.
    def _me_job():
        from auth.providers import GATE_CHANGE_PASSWORD, GATE_ENROLL_2FA, auth_gate
        db_user = task_store.get_user(user.sub)
        gate = auth_gate(db_user)
        return (_check_platform_configured(user.sub, user.role), _has_own_engine(user.sub),
                db_user, gate == GATE_CHANGE_PASSWORD, gate == GATE_ENROLL_2FA,
                build_feature_flags())

    (platform_configured, has_own_engine, db_user, must_change_password,
     must_enroll_2fa, feature_flags) = await run_db(_me_job)
    totp_enabled = bool(db_user.get("totp_enabled")) if db_user else False
    is_owner = bool(db_user.get("is_owner")) if db_user else False

    return {
        "user": {
            "sub": user.sub,
            "email": user.email,
            "name": user.name,
            # Filesystem-safe slug backing `users/<username>/` in agent
            # workspaces — lets the dashboard pick the caller's OWN folder
            # from the file tree instead of assuming position.
            "username": (db_user.get("username") or "") if db_user else "",
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

    _confirm_ok, _retry = rate_limit_hit("confirm", u.sub)
    if not _confirm_ok:
        raise HTTPException(429, f"Too many attempts. Try again in {_retry} seconds.",
                            headers={"Retry-After": str(_retry)})

    db_user = await asyncio.to_thread(task_store.get_user, u.sub)
    if not db_user or not db_user.get("password_hash"):
        raise HTTPException(400, "No password set for this account")

    if not await _password_matches(req.current_password, db_user["password_hash"]):
        raise HTTPException(401, "Current password is incorrect")

    pw_hash = await _new_password_hash(req.new_password)
    await asyncio.to_thread(task_store.set_user_password, u.sub, pw_hash)
    logger.info(f"User {mask_email(u.email)} changed their password")
    # Their warm chats hold session tokens the change just invalidated:
    # close them so the next message re-warms with fresh ones.
    await offboarding_sessions.close_person_sessions(
        u.sub, db_user.get("username") or "", "password_changed")
    # The caller's CURRENT cookie now predates password_changed_at and would be
    # rejected on their next request (that rejection is the whole point — it
    # evicts any OTHER live session on the old credential). Re-issue a fresh
    # cookie on THIS response so the person who just changed it stays signed in.
    response = JSONResponse(content={"status": "ok"})
    hours = await run_db(config.get_jwt_expiry_hours)
    _issue_session_cookie(response, u.sub, u.email, u.name, u.role,
                          auth_provider=u.auth_provider or "local", expiry_hours=hours)
    # The change voided this browser's trusted-device token with the rest:
    # the person who made it keeps theirs.
    _issue_device_cookie(response, u.sub)
    return response


@router.put("/v1/users/me/email")
async def change_my_email(
    req: ChangeEmailRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Change own email. Requires password confirmation."""
    u = require_auth(user)
    if u.is_api_key:
        raise HTTPException(403, "Dashboard only")

    _confirm_ok, _retry = rate_limit_hit("confirm", u.sub)
    if not _confirm_ok:
        raise HTTPException(429, f"Too many attempts. Try again in {_retry} seconds.",
                            headers={"Retry-After": str(_retry)})

    db_user = await asyncio.to_thread(task_store.get_user, u.sub)
    if not db_user or not db_user.get("password_hash"):
        raise HTTPException(400, "Cannot change email for OIDC accounts")

    if not await _password_matches(req.password, db_user["password_hash"]):
        raise HTTPException(401, "Password is incorrect")

    try:
        await asyncio.to_thread(task_store.update_user_email, u.sub, req.new_email.strip().lower())
    except ValueError as e:
        raise HTTPException(409, str(e))

    logger.info(f"User {mask_email(u.email)} changed email to {mask_email(req.new_email)}")
    return {"status": "ok"}


@router.post("/v1/users/me/totp/setup")
async def totp_setup(
    req: TotpSetupRequest | None = Body(default=None),
    user: UserContext | None = Depends(get_current_user),
):
    """Generate TOTP secret and recovery codes. Does not enable 2FA yet."""
    u = require_auth(user)
    if u.is_api_key:
        raise HTTPException(403, "Dashboard only")

    # Reconfiguring while 2FA is already enabled overwrites the live secret +
    # recovery codes — require the password (like /totp DELETE does) so a
    # hijacked session can't silently swap the victim's second factor and
    # lock them out. The initial enable flow (2FA off) needs no password.
    db_user = await asyncio.to_thread(task_store.get_user, u.sub)
    if db_user and db_user.get("totp_enabled"):
        _confirm_ok, _retry = rate_limit_hit("confirm", u.sub)
        if not _confirm_ok:
            raise HTTPException(429, f"Too many attempts. Try again in {_retry} seconds.",
                                headers={"Retry-After": str(_retry)})
        if not db_user.get("password_hash"):
            raise HTTPException(400, "Cannot reconfigure 2FA without a password")
        if not req or not await _password_matches(req.password, db_user["password_hash"]):
            raise HTTPException(401, "Password confirmation required to reconfigure 2FA")

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

    _confirm_ok, _retry = rate_limit_hit("confirm", u.sub)
    if not _confirm_ok:
        raise HTTPException(429, f"Too many attempts. Try again in {_retry} seconds.",
                            headers={"Retry-After": str(_retry)})

    db_user = await asyncio.to_thread(task_store.get_user, u.sub)
    if not db_user or not db_user.get("password_hash"):
        raise HTTPException(400, "Cannot disable 2FA without a password")

    if not await _password_matches(req.password, db_user["password_hash"]):
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

        # Fire-and-forget: awaiting the SMTP send here would make the response
        # measurably slower ONLY for registered emails — a timing oracle that
        # contradicts this endpoint's no-enumeration contract. Send in the
        # background so both the hit and miss paths return immediately.
        async def _send_reset() -> None:
            try:
                await asyncio.to_thread(send_password_reset_email, email, reset_url)
            except Exception:
                logger.warning("Password-reset email send failed for %s",
                               mask_email(email))
        asyncio.create_task(_send_reset())

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

    pw_hash = await _new_password_hash(req.new_password)
    await asyncio.to_thread(task_store.set_user_password, sub, pw_hash)
    logger.info(f"Password reset completed for {mask_email(user['email'])}")
    await offboarding_sessions.close_person_sessions(
        sub, user.get("username") or "", "password_changed")
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

    pw_hash = await _new_password_hash(req.new_password)
    await asyncio.to_thread(task_store.set_user_password, user["sub"], pw_hash)
    logger.info(f"Invite accepted for {mask_email(user['email'])}")
    return {"status": "ok", "email": user["email"]}

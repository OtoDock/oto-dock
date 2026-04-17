"""WebAuthn passkey endpoints — registration + authentication ceremonies.

Feature gate: passkeys need a secure context and a stable domain, so they are
enabled ONLY when DASHBOARD_PUBLIC_URL is https — the RP ID is its hostname.
LAN-IP / plain-HTTP installs keep password+TOTP; the dashboard hides the
passkey UI via ``passkeys_enabled`` in /auth/config.

Login uses discoverable credentials (no allowCredentials — the browser offers
any passkey for this RP), and a verified passkey IS multi-factor (possession +
on-device user verification), so it bypasses the TOTP step and satisfies the
require-2FA policy. Password(+TOTP) always remains as fallback.

Attaches to the shared core-auth router."""

import asyncio
import json
import logging
import secrets
import time
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

import config
from auth.lan_check import check_local_auth_allowed, get_client_ip
from auth.password import verify_password
from auth.providers import UserContext, get_current_user, mask_email, require_auth
from auth.rate_limiter import hit as rate_limit_hit
from auth.totp import consume_2fa_session_token, validate_2fa_session_token
from storage import database as task_store
from storage import webauthn_store

from api.auth._common import _build_user_response
from api.auth._router import router

logger = logging.getLogger("claude-proxy")

_RP_NAME = "OtoDock"

# Server-side ceremony challenges, single-use, short-lived. In-memory (resets
# on restart; per-replica) — same posture as the rate limiter: a lost entry
# just means the user retries the ceremony.
_CHALLENGE_TTL = 300
_challenges: dict[str, tuple[bytes, str, float]] = {}  # state → (challenge, sub, expires)

# Native-app session handoff: the Android app can't run WebAuthn in its
# webview, so the ceremony runs in the SYSTEM browser (/native-passkey page)
# with ``native: true`` — the verify then mints a one-time token instead of a
# cookie, the page deep-links it back via the existing OIDC-callback rails
# (otodock://auth/callback?code=<token>&state=passkey-handoff), and the
# webview exchanges it for its own session cookie. Single-use, 60s.
NATIVE_HANDOFF_STATE = "passkey-handoff"
_NATIVE_TOKEN_TTL = 60
_native_tokens: dict[str, tuple[str, float]] = {}  # token → (sub, expires)


def passkeys_enabled() -> bool:
    """WebAuthn requires a secure context + stable RP domain — https only."""
    return (config.DASHBOARD_PUBLIC_URL or "").strip().lower().startswith("https://")


def passkey_login_mode() -> str:
    """Admin knob: ``passwordless`` (default — passkey is a primary sign-in;
    with UV required it is always ≥2 factors) or ``second_factor`` (no
    passwordless entry — passkeys are offered only at the 2FA step after a
    correct password, alongside TOTP)."""
    mode = task_store.get_all_platform_settings().get("passkey_login_mode", "")
    return mode if mode in ("passwordless", "second_factor") else "passwordless"


def _rp_id() -> str:
    return urlparse(config.DASHBOARD_PUBLIC_URL.strip()).hostname or ""


def _expected_origin() -> str:
    p = urlparse(config.DASHBOARD_PUBLIC_URL.strip())
    origin = f"{p.scheme}://{p.hostname}"
    if p.port:
        origin += f":{p.port}"
    return origin


def _require_enabled() -> None:
    if not passkeys_enabled():
        raise HTTPException(
            status_code=400,
            detail="Passkeys require an HTTPS public dashboard URL (DASHBOARD_PUBLIC_URL)",
        )


def _put_challenge(challenge: bytes, sub: str) -> str:
    now = time.time()
    for state in [s for s, (_, _, exp) in _challenges.items() if exp < now]:
        del _challenges[state]
    state = secrets.token_urlsafe(24)
    _challenges[state] = (challenge, sub, now + _CHALLENGE_TTL)
    return state


def _pop_challenge(state: str) -> tuple[bytes, str] | None:
    """Single-use: the challenge leaves the store on first retrieval."""
    entry = _challenges.pop(state, None)
    if not entry or entry[2] < time.time():
        return None
    return entry[0], entry[1]


def _mint_native_token(sub: str) -> str:
    now = time.time()
    for tok in [t for t, (_, exp) in _native_tokens.items() if exp < now]:
        del _native_tokens[tok]
    token = secrets.token_urlsafe(32)
    _native_tokens[token] = (sub, now + _NATIVE_TOKEN_TTL)
    return token


def _pop_native_token(token: str) -> str | None:
    """Single-use: returns the sub, or None (unknown/expired/replayed)."""
    entry = _native_tokens.pop(token, None)
    if not entry or entry[1] < time.time():
        return None
    return entry[0]


class PasskeyRegisterOptionsRequest(BaseModel):
    password: str


class PasskeyRegisterVerifyRequest(BaseModel):
    state: str
    credential: dict
    name: str = ""


class PasskeyRenameRequest(BaseModel):
    name: str
    password: str


class PasskeyDeleteRequest(BaseModel):
    password: str


class PasskeyLoginOptionsRequest(BaseModel):
    # Present at the 2FA step: the password-verified step token scopes the
    # ceremony to that user's credentials (and is REQUIRED in second_factor mode).
    totp_session_token: str | None = None


class PasskeyLoginVerifyRequest(BaseModel):
    state: str
    credential: dict
    # Native-app flow: mint a one-time handoff token instead of the cookie.
    native: bool = False
    # 2FA-step flow: consume this on success (single-use, same as the TOTP path).
    totp_session_token: str | None = None


class PasskeyNativeExchangeRequest(BaseModel):
    token: str


async def _confirm_password(u: UserContext, password: str) -> dict:
    """Password-confirm a passkey management action. Returns the DB user row."""
    db_user = await asyncio.to_thread(task_store.get_user, u.sub)
    if not db_user or not db_user.get("password_hash"):
        raise HTTPException(400, "Passkey management requires a password-backed local account")
    if not verify_password(password, db_user["password_hash"]):
        raise HTTPException(401, "Password is incorrect")
    return db_user


# --- Management (authed, password-confirmed mutations) ---


@router.get("/v1/users/me/passkeys")
async def list_passkeys(user: UserContext | None = Depends(get_current_user)):
    u = require_auth(user)
    if u.is_api_key:
        raise HTTPException(403, "Dashboard only")
    creds = await asyncio.to_thread(webauthn_store.list_credentials, u.sub)
    return {"passkeys": creds, "enabled": passkeys_enabled()}


@router.post("/v1/users/me/passkeys/register/options")
async def passkey_register_options(
    req: PasskeyRegisterOptionsRequest,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    if u.is_api_key:
        raise HTTPException(403, "Dashboard only")
    _require_enabled()
    db_user = await _confirm_password(u, req.password)

    existing = await asyncio.to_thread(webauthn_store.list_credentials, u.sub)
    options = generate_registration_options(
        rp_id=_rp_id(),
        rp_name=_RP_NAME,
        user_id=u.sub.encode(),
        user_name=u.email,
        user_display_name=db_user.get("display_name") or db_user.get("name") or u.email,
        # Discoverable credential so the login ceremony works without a
        # username prompt. UV REQUIRED at registration so a UV-incapable
        # authenticator is rejected here — not later at login, where UV is
        # also required (that's what makes passwordless login always ≥2
        # factors: possession + on-device biometric/PIN).
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["credential_id"]))
            for c in existing
        ],
    )
    state = _put_challenge(options.challenge, u.sub)
    return {"state": state, "options": json.loads(options_to_json(options))}


@router.post("/v1/users/me/passkeys/register/verify")
async def passkey_register_verify(
    req: PasskeyRegisterVerifyRequest,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    if u.is_api_key:
        raise HTTPException(403, "Dashboard only")
    _require_enabled()

    popped = _pop_challenge(req.state)
    if not popped or popped[1] != u.sub:
        raise HTTPException(400, "Registration challenge expired — try again")

    try:
        verification = verify_registration_response(
            credential=req.credential,
            expected_challenge=popped[0],
            expected_rp_id=_rp_id(),
            expected_origin=_expected_origin(),
        )
    except WebAuthnException as e:
        # Base class: parse errors (InvalidJSONStructure etc.) and verification
        # failures alike are a client-side 400, never a 500.
        logger.info(f"Passkey registration rejected for {mask_email(u.email)}: {e}")
        raise HTTPException(400, "Passkey registration could not be verified")

    count = await asyncio.to_thread(webauthn_store.count_credentials, u.sub)
    name = req.name.strip() or f"Passkey {count + 1}"
    transports = (req.credential.get("response") or {}).get("transports") or []
    await asyncio.to_thread(
        webauthn_store.add_credential,
        bytes_to_base64url(verification.credential_id),
        u.sub,
        bytes_to_base64url(verification.credential_public_key),
        verification.sign_count,
        name,
        [str(t) for t in transports],
    )
    logger.info(f"Passkey registered for {mask_email(u.email)} ({name})")
    return {"status": "registered", "name": name}


@router.put("/v1/users/me/passkeys/{credential_id}")
async def passkey_rename(
    credential_id: str,
    req: PasskeyRenameRequest,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    if u.is_api_key:
        raise HTTPException(403, "Dashboard only")
    await _confirm_password(u, req.password)
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    ok = await asyncio.to_thread(webauthn_store.rename_credential, u.sub, credential_id, name)
    if not ok:
        raise HTTPException(404, "Passkey not found")
    return {"status": "renamed", "name": name}


@router.delete("/v1/users/me/passkeys/{credential_id}")
async def passkey_delete(
    credential_id: str,
    req: PasskeyDeleteRequest,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    if u.is_api_key:
        raise HTTPException(403, "Dashboard only")
    await _confirm_password(u, req.password)
    ok = await asyncio.to_thread(webauthn_store.delete_credential, u.sub, credential_id)
    if not ok:
        raise HTTPException(404, "Passkey not found")
    logger.info(f"Passkey removed for {mask_email(u.email)}")
    return {"status": "deleted"}


# --- Authentication (public, rate-limited) ---


@router.post("/auth/passkey/options")
async def passkey_login_options(request: Request,
                                req: PasskeyLoginOptionsRequest | None = None):
    """Start a passkey login ceremony.

    Passwordless mode: discoverable — no username needed, no body. At the 2FA
    step (any mode) the password-verified step token scopes allowCredentials to
    that user; in ``second_factor`` mode the token is REQUIRED (no passwordless
    entry)."""
    ok, retry_after = rate_limit_hit("passkey", get_client_ip(request))
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )
    _require_enabled()

    step_sub = ""
    if req and req.totp_session_token:
        step_sub = validate_2fa_session_token(req.totp_session_token) or ""
        if not step_sub:
            raise HTTPException(401, "2FA session expired. Please log in again.")
    mode = await asyncio.to_thread(passkey_login_mode)
    if mode == "second_factor" and not step_sub:
        raise HTTPException(400, "Passkey sign-in requires your password first")

    allow = None
    if step_sub:
        creds = await asyncio.to_thread(webauthn_store.list_credentials, step_sub)
        allow = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["credential_id"]))
                 for c in creds]
    options = generate_authentication_options(
        rp_id=_rp_id(),
        user_verification=UserVerificationRequirement.REQUIRED,
        allow_credentials=allow,
    )
    state = _put_challenge(options.challenge, step_sub)
    return {"state": state, "options": json.loads(options_to_json(options))}


@router.post("/auth/passkey/verify")
async def passkey_login_verify(req: PasskeyLoginVerifyRequest, request: Request):
    """Finish a passkey login: verify the assertion, issue the session cookie."""
    ok, retry_after = rate_limit_hit("passkey", get_client_ip(request))
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )
    _require_enabled()

    # 2FA-step flow: validate (not yet consume) the step token; in
    # second_factor mode a bare passwordless verify is refused server-side —
    # the mode must not be merely cosmetic.
    step_sub = ""
    if req.totp_session_token:
        step_sub = validate_2fa_session_token(req.totp_session_token) or ""
        if not step_sub:
            raise HTTPException(401, "2FA session expired. Please log in again.")
    mode = await asyncio.to_thread(passkey_login_mode)
    if mode == "second_factor" and not step_sub:
        raise HTTPException(400, "Passkey sign-in requires your password first")

    popped = _pop_challenge(req.state)
    if not popped:
        raise HTTPException(401, "Sign-in challenge expired — try again")

    credential_id = req.credential.get("id") or ""
    cred = await asyncio.to_thread(webauthn_store.get_credential, credential_id)
    if not cred:
        raise HTTPException(401, "Unknown passkey")
    # Challenge- and token-bound identity: a step-scoped ceremony may only
    # complete with a credential of that same user.
    if popped[1] and cred["user_sub"] != popped[1]:
        raise HTTPException(401, "Unknown passkey")
    if step_sub and cred["user_sub"] != step_sub:
        raise HTTPException(401, "Unknown passkey")

    user_row = await asyncio.to_thread(task_store.get_user, cred["user_sub"])
    if not user_row:
        raise HTTPException(401, "Unknown passkey")
    if not check_local_auth_allowed(request, user_row):
        raise HTTPException(403, "This account can only be accessed from the local network")

    try:
        verification = verify_authentication_response(
            credential=req.credential,
            expected_challenge=popped[0],
            expected_rp_id=_rp_id(),
            expected_origin=_expected_origin(),
            credential_public_key=base64url_to_bytes(cred["public_key"]),
            credential_current_sign_count=cred["sign_count"],
            # Passwordless login must always be ≥2 factors: possession + the
            # on-device biometric/PIN. Enforced for the 2FA step too.
            require_user_verification=True,
        )
    except WebAuthnException as e:
        logger.info(f"Passkey login rejected for credential {credential_id[:12]}…: {e}")
        raise HTTPException(401, "Passkey could not be verified")

    await asyncio.to_thread(
        webauthn_store.record_use, credential_id, verification.new_sign_count,
    )
    if req.totp_session_token:
        # Spend the step token only on SUCCESS (typos stay retryable).
        consume_2fa_session_token(req.totp_session_token)

    # Native app: the SYSTEM browser ran this ceremony — don't log the browser
    # in; hand back a one-time token the app's webview exchanges for ITS cookie.
    if req.native:
        token = _mint_native_token(user_row["sub"])
        logger.info(f"Passkey native handoff minted for {mask_email(user_row['email'])}")
        return {"status": "ok", "native_token": token}

    # A verified passkey is multi-factor by construction — no TOTP step.
    user_data = _build_user_response(user_row)
    response = JSONResponse(content={"user": user_data})
    from api.auth.identity import _issue_session_cookie
    _issue_session_cookie(response, user_row["sub"], user_row["email"],
                          user_row["name"], user_row["role"], auth_provider="local")
    logger.info(f"Passkey login: {mask_email(user_row['email'])} role={user_row['role']}")
    return response


@router.post("/auth/passkey/native/exchange")
async def passkey_native_exchange(req: PasskeyNativeExchangeRequest, request: Request):
    """Trade a one-time native-handoff token for a session cookie (app webview)."""
    ok, retry_after = rate_limit_hit("passkey", get_client_ip(request))
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )

    sub = _pop_native_token(req.token)
    if not sub:
        raise HTTPException(401, "Sign-in expired — try again")

    user_row = await asyncio.to_thread(task_store.get_user, sub)
    if not user_row:
        raise HTTPException(401, "Sign-in expired — try again")
    if not check_local_auth_allowed(request, user_row):
        raise HTTPException(403, "This account can only be accessed from the local network")

    user_data = _build_user_response(user_row)
    response = JSONResponse(content={"user": user_data})
    from api.auth.identity import _issue_session_cookie
    _issue_session_cookie(response, user_row["sub"], user_row["email"],
                          user_row["name"], user_row["role"], auth_provider="local")
    logger.info(f"Passkey login (native): {mask_email(user_row['email'])} role={user_row['role']}")
    return response

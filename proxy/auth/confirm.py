"""A confirmation of the person at the keyboard (SHARING.md "The confirm").

The routes that open an exfiltration channel — an external link, its
Buttons switch — re-verify the account password on the already-authed
session (the ``confirm`` bucket bounds the oracle a stolen cookie would
be). An account without a local password confirms with a passkey
assertion instead (the ceremony in ``api/auth/webauthn.py``), and an
account that signed in through the identity provider with a round trip
there — the provider says the browser is signed in as the same account
(``GET /auth/confirm/oidc-url`` and the callback's confirm branch in
``api/auth/identity.py``); both mint a one-shot token this module consumes.
An account with none of the three cannot confirm, and the route says which
method it lacks.
"""

from __future__ import annotations

import asyncio
import secrets
import time

from fastapi import HTTPException

import config
from auth.password import HashBusy, verify_password_async
from auth.rate_limiter import hit as rate_limit_hit
from storage import database as task_store

CONFIRM_TOKEN_TTL_S = 300
# token → (sub, expires). In memory, single-use, like the passkey challenges.
_confirm_tokens: dict[str, tuple[str, float]] = {}


async def confirm_password(u, password: str) -> dict:
    """Password-confirm an action on an already-authed session; returns the
    users row. 429 past the bucket, 400 without a local password, 401 on a
    wrong one."""
    ok, retry_after = rate_limit_hit("confirm", u.sub)
    if not ok:
        raise HTTPException(429, f"Too many attempts. Try again in {retry_after} seconds.",
                            headers={"Retry-After": str(retry_after)})
    db_user = await asyncio.to_thread(task_store.get_user, u.sub)
    if not db_user or not db_user.get("password_hash"):
        raise HTTPException(400, "This action needs a password-backed local account")
    try:
        ok = await verify_password_async(password or "", db_user["password_hash"])
    except HashBusy:
        raise hash_busy()
    if not ok:
        raise HTTPException(401, "Password is incorrect")
    return db_user


def hash_busy() -> HTTPException:
    """The password gate is full (``AUTH_HASH_MAX_WAITERS``): the caller
    retries in a moment instead of queuing a bcrypt thread without bound."""
    return HTTPException(503, "Too many password checks at once. Try again in a few seconds.",
                         headers={"Retry-After": "5", "Connection": "close"})


def mint_confirm_token(sub: str) -> str:
    now = time.time()
    for tok in [t for t, (_, exp) in _confirm_tokens.items() if exp < now]:
        del _confirm_tokens[tok]
    token = secrets.token_urlsafe(32)
    _confirm_tokens[token] = (sub, now + CONFIRM_TOKEN_TTL_S)
    return token


def consume_confirm_token(token: str, sub: str) -> bool:
    """Single-use, bound to the user it was minted for."""
    entry = _confirm_tokens.pop(token or "", None)
    return bool(entry) and entry[0] == sub and entry[1] >= time.time()


async def confirm_human(u, *, password: str = "", confirm_token: str = "") -> None:
    """Raise unless the caller just proved they are at the keyboard: the
    password for a password-backed account, else a passkey confirm token.
    Without either, 428 with ``{"code": "confirm_required", "method":
    "password" | "passkey" | "oidc" | "none"}`` so the client knows what to
    ask (``oidc`` carries the provider's name for the button)."""
    db_user = await asyncio.to_thread(task_store.get_user, u.sub)
    if not db_user:
        raise HTTPException(401, "Unknown account")
    if db_user.get("password_hash"):
        if not password:
            raise HTTPException(428, {"code": "confirm_required", "method": "password",
                                      "message": "Enter your password to confirm"})
        await confirm_password(u, password)
        return
    if confirm_token:
        if consume_confirm_token(confirm_token, u.sub):
            return
        raise HTTPException(401, "The confirmation expired — try again")
    from storage.identity import webauthn_store
    creds = await asyncio.to_thread(webauthn_store.list_credentials, u.sub)
    if creds:
        raise HTTPException(428, {"code": "confirm_required", "method": "passkey",
                                  "message": "Confirm with your passkey"})
    if config.OIDC_ENABLED and str(db_user.get("auth_provider") or "").startswith("oidc:"):
        raise HTTPException(428, {"code": "confirm_required", "method": "oidc",
                                  "provider": config.OIDC_PROVIDER_NAME,
                                  "message": f"Confirm with {config.OIDC_PROVIDER_NAME}"})
    raise HTTPException(428, {"code": "confirm_required", "method": "none",
                              "message": "This action needs a password or a passkey on your account"})

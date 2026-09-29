"""Local email + password authentication provider."""

import asyncio
import logging
import time

from auth.password import HashBusy, dummy_verify_async, verify_password_async
from auth.providers.base import AuthProvider, AuthResult
from auth.rate_limiter import (
    check_account_tarpit,
    release_device_attempt,
    trusted_device_for,
    undo_failed_login,
)
from auth.totp import create_2fa_session_token
from storage import database as db
from storage.pg import run_db

logger = logging.getLogger("claude-proxy")

_INVALID = "Invalid email or password"


class _AccountLocks:
    """One ``asyncio.Lock`` per account around the tarpit check and the
    failure count, so concurrent guesses against one account each see the
    others' counts. Bound to the running loop (rebuilt when it changes: the
    test suite runs several loops); idle locks are dropped past the cap."""

    _MAX = 1024

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.locks: dict[str, asyncio.Lock] = {}

    def get(self, sub: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self.loop is not loop:
            self.loop, self.locks = loop, {}
        lock = self.locks.get(sub)
        if lock is None:
            if len(self.locks) >= self._MAX:
                for key in [k for k, v in self.locks.items() if not v.locked()]:
                    del self.locks[key]
            lock = self.locks[sub] = asyncio.Lock()
        return lock


_account_locks = _AccountLocks()
# An account the tarpit refused, until its delay lapses: a flood against it
# is answered from memory, not with a database read per request.
_tarpitted_until: dict[str, float] = {}


def _check_and_count(sub: str) -> tuple[bool, float]:
    """The tarpit check and, when it lets the attempt through, one failure
    counted BEFORE the password is checked (a right password clears it)."""
    ok, wait = check_account_tarpit(sub)
    if ok:
        db.record_failed_login(sub)
    return ok, wait


def _locked(wait_secs: float) -> AuthResult:
    return AuthResult(
        success=False,
        error=f"Too many failed attempts. Please wait {max(1, int(wait_secs))} seconds.",
        error_code="account_locked",
    )


class LocalAuthProvider(AuthProvider):
    """Email + password authentication with optional TOTP 2FA."""

    async def authenticate(self, request_data: dict, *, device: dict | None = None) -> AuthResult:
        """Authenticate a local user.

        request_data: {"email": str, "password": str}. ``device``: the claims
        of the browser's trusted-device token, when it carries one
        (``rate_limiter.device_token_claims``).
        """
        email = request_data.get("email", "").strip().lower()
        password = request_data.get("password", "")

        if not email or not password:
            return AuthResult(success=False, error="Email and password are required",
                              error_code="invalid_credentials")

        user = await run_db(db.get_user_by_email, email)
        # Each miss path runs a throwaway bcrypt compare (dummy_verify) so its
        # response time matches a real password check — otherwise the presence
        # of a local password hash is a remote timing oracle for account
        # existence / auth-provider.
        if (not user or not (user.get("auth_provider") or "").startswith("local")
                or not user.get("password_hash")):
            await dummy_verify_async()
            return AuthResult(success=False, error=_INVALID, error_code="invalid_credentials")

        sub = user["sub"]
        # Counts the attempt against the device when the cookie vouches.
        device_key = trusted_device_for(device, user)
        if not device_key:
            until = _tarpitted_until.get(sub, 0.0)
            now = time.monotonic()
            if until > now:
                return _locked(until - now)
            async with _account_locks.get(sub):
                ok, wait_secs = await run_db(_check_and_count, sub)
            if not ok:
                _tarpitted_until[sub] = time.monotonic() + wait_secs
                return _locked(wait_secs)
            _tarpitted_until.pop(sub, None)

        try:
            right = await verify_password_async(password, user["password_hash"])
        except HashBusy:
            # No password was checked: the attempt counted above is given back.
            if device_key:
                release_device_attempt(device_key)
            else:
                await run_db(undo_failed_login, sub)
            raise
        # The failure result carries ``sub``, and ONLY this branch does, so
        # the unknown-email / OIDC-account / passwordless branches above never
        # touch another auth-provider's row.
        if not right:
            return AuthResult(success=False, error=_INVALID,
                              error_code="invalid_credentials", sub=sub)
        # The attempt counted above had the right password.
        if device_key:
            release_device_attempt(device_key)
        else:
            await run_db(db.clear_failed_logins, sub)
        _tarpitted_until.pop(sub, None)

        # Password correct — check 2FA
        if user.get("totp_enabled"):
            token = create_2fa_session_token(sub)
            return AuthResult(
                success=True,
                requires_2fa=True,
                totp_session_token=token,
                sub=sub,
                auth_provider="local",
            )

        # Full success
        return AuthResult(
            success=True,
            sub=sub,
            email=user["email"],
            name=user["name"],
            display_name=user.get("display_name", ""),
            role=user["role"],
            auth_provider="local",
            must_change_password=bool(user.get("must_change_password")),
        )

    def get_login_url(self, **_) -> None:
        """Local auth uses a form, no redirect URL."""
        return None

    def get_logout_url(self, **_) -> None:
        """Local logout just clears the session cookie."""
        return None

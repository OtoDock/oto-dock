"""Local email + password authentication provider."""

import logging

from auth.password import verify_password
from auth.providers.base import AuthProvider, AuthResult
from auth.rate_limiter import check_account_tarpit
from auth.totp import create_2fa_session_token
from storage import database as db

logger = logging.getLogger("claude-proxy")


class LocalAuthProvider(AuthProvider):
    """Email + password authentication with optional TOTP 2FA."""

    async def authenticate(self, request_data: dict) -> AuthResult:
        """Authenticate a local user.

        request_data: {"email": str, "password": str}
        """
        email = request_data.get("email", "").strip().lower()
        password = request_data.get("password", "")

        if not email or not password:
            return AuthResult(success=False, error="Email and password are required",
                              error_code="invalid_credentials")

        # Look up user by email
        user = db.get_user_by_email(email)
        if not user:
            return AuthResult(success=False, error="Invalid email or password",
                              error_code="invalid_credentials")

        # Must be a local user with a password
        auth_provider = user.get("auth_provider", "")
        if not auth_provider.startswith("local"):
            return AuthResult(success=False, error="Invalid email or password",
                              error_code="invalid_credentials")

        if not user.get("password_hash"):
            return AuthResult(success=False, error="Invalid email or password",
                              error_code="invalid_credentials")

        # Check account tarpit (slow down after repeated failures)
        tarpit_ok, wait_secs = check_account_tarpit(user["sub"])
        if not tarpit_ok:
            return AuthResult(
                success=False,
                error=f"Too many failed attempts. Please wait {int(wait_secs)} seconds.",
                error_code="account_locked",
            )

        # Verify password
        if not verify_password(password, user["password_hash"]):
            return AuthResult(success=False, error="Invalid email or password",
                              error_code="invalid_credentials")

        # Password correct — check 2FA
        if user.get("totp_enabled"):
            token = create_2fa_session_token(user["sub"])
            return AuthResult(
                success=True,
                requires_2fa=True,
                totp_session_token=token,
                sub=user["sub"],
                auth_provider="local",
            )

        # Full success
        return AuthResult(
            success=True,
            sub=user["sub"],
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

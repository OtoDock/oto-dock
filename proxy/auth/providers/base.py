"""Auth provider abstraction — base class and result types."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class AuthResult:
    """Result of an authentication attempt."""

    success: bool
    sub: str = ""
    email: str = ""
    name: str = ""
    display_name: str = ""
    role: str = ""
    auth_provider: str = ""  # "local" | "oidc:authentik" | "oidc:authelia" etc.
    error: str = ""
    error_code: str = ""  # "invalid_credentials", "account_locked", "no_group", "lan_restricted"
    requires_2fa: bool = False
    totp_session_token: str = ""  # short-lived JWT for 2FA step
    must_change_password: bool = False


class AuthProvider(ABC):
    """Abstract base for authentication providers (local, OIDC, SAML, LDAP, etc.)."""

    @abstractmethod
    async def authenticate(self, request_data: dict) -> AuthResult:
        """Authenticate a user from request data.

        For local: request_data = {"email": ..., "password": ...}
        For OIDC: request_data = {"code": ..., "redirect_uri": ...}
        """
        ...

    @abstractmethod
    def get_login_url(self, *, redirect_uri: str | None = None,
                      mobile: bool = False) -> str | None:
        """Return URL to redirect to for login, or None for form-based auth."""
        ...

    @abstractmethod
    def get_logout_url(self, post_redirect: str | None = None) -> str | None:
        """Return provider logout URL, or None if not applicable."""
        ...

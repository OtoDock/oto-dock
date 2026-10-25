"""TOTP two-factor authentication utilities."""

import hashlib
import hmac
import json
import secrets
import time
import unicodedata
from dataclasses import dataclass

import pyotp

from auth import tokens
from storage.identity.credential_store import _decrypt, _encrypt


def generate_totp_secret() -> str:
    """Generate a new TOTP secret (base32)."""
    return pyotp.random_base32()


def get_totp_uri(secret: str, email: str, issuer: str = "OtoDock") -> str:
    """Get otpauth:// URI for QR code generation."""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name=issuer)


def totp_step(secret: str, code: str, now: float | None = None) -> int | None:
    """The time step a 6-digit ``code`` belongs to, one step either way of
    now for clock skew, or None. The caller accepts it only when the step is
    past the last one the account accepted (``claim_totp_step``): RFC 6238
    section 5.2, one code is good once."""
    # NFKC folds the full-width digits a wide keyboard types to ASCII, as
    # the authenticator's code is; the digits of other scripts and anything
    # else are no code (refused, never a 500).
    code = unicodedata.normalize("NFKC", code)
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    current = int((time.time() if now is None else now) // totp.interval)
    for step in (current + 1, current, current - 1):
        if hmac.compare_digest(code, totp.generate_otp(step)):
            return step
    return None


def generate_recovery_codes(count: int = 10) -> list[str]:
    """Generate recovery codes: 80 bits each (20 hex chars), dash-grouped for
    readability. Hashing canonicalizes, so users may type either form."""
    return [
        "-".join(raw[i:i + 5] for i in range(0, 20, 5))
        for raw in (secrets.token_hex(10).upper() for _ in range(count))
    ]


def _canonical_recovery_code(code: str) -> str:
    """Strip the display grouping (dashes/spaces) and uppercase."""
    return code.replace("-", "").replace(" ", "").upper()


def hash_recovery_codes(codes: list[str]) -> list[str]:
    """Hash recovery codes with SHA256 for storage."""
    return [hashlib.sha256(_canonical_recovery_code(c).encode()).hexdigest() for c in codes]


def verify_recovery_code(code: str, hashed_codes: list[str]) -> tuple[bool, list[str]]:
    """Verify a recovery code. Returns (matched, remaining_hashed_codes)."""
    code_hash = hashlib.sha256(_canonical_recovery_code(code).encode()).hexdigest()
    if code_hash in hashed_codes:
        remaining = [h for h in hashed_codes if h != code_hash]
        return True, remaining
    return False, hashed_codes


def encrypt_totp_secret(secret: str) -> str:
    """Encrypt TOTP secret for DB storage."""
    return _encrypt(secret)


def decrypt_totp_secret(encrypted: str) -> str:
    """Decrypt TOTP secret from DB."""
    return _decrypt(encrypted)


def encrypt_recovery_codes(hashed_codes: list[str]) -> str:
    """Encrypt hashed recovery codes (JSON) for DB storage."""
    return _encrypt(json.dumps(hashed_codes))


def decrypt_recovery_codes(encrypted: str) -> list[str]:
    """Decrypt recovery codes from DB."""
    return json.loads(_decrypt(encrypted))


# --- 2FA session tokens ---

_2FA_TOKEN_TTL = 300  # 5 minutes

# A step token is reserved by the request that presents it, before its first
# await, and spent once its factor is accepted: two requests carrying one
# token cannot both reach the session, and a wrong code gives the token back
# so a typo retries. jti → exp in this process's memory (one process serves
# the sign-in routes); a restart forgets them, and the five-minute token then
# still needs a code whose step is past the account's last one.
_used_jtis: dict[str, float] = {}
_reserved_jtis: dict[str, float] = {}


class StepTokenBusy(Exception):
    """Another request holds this step token right now."""


@dataclass(frozen=True)
class StepToken:
    sub: str
    jti: str
    exp: float


def _sweep_used_jtis(now: float) -> None:
    for table in (_used_jtis, _reserved_jtis):
        for jti in [j for j, exp in table.items() if exp < now]:
            del table[jti]


def create_2fa_session_token(sub: str) -> str:
    """Create a short-lived JWT for the 2FA verification step."""
    payload = {
        "sub": sub,
        "purpose": "2fa",
        "jti": secrets.token_hex(8),
        "iat": int(time.time()),
        "exp": int(time.time()) + _2FA_TOKEN_TTL,
    }
    return tokens.mint(tokens.TWO_FACTOR, payload)


def _step_payload(token: str) -> dict | None:
    payload = tokens.verify(token, tokens.TWO_FACTOR)
    if payload is None or not payload.get("jti") or not payload.get("sub"):
        return None
    return payload


def validate_2fa_session_token(token: str) -> str | None:
    """The sub of a live, unspent step token, or None. Reserves nothing: for
    a call that only reads it (the passkey ceremony's options)."""
    payload = _step_payload(token)
    if payload is None or payload["jti"] in _used_jtis:
        return None
    return payload["sub"]


def reserve_2fa_session_token(token: str) -> StepToken | None:
    """Take the step token for this request, with no await between the check
    and the take: None when it is invalid, expired or spent;
    ``StepTokenBusy`` when another request holds it. The caller spends it
    once the factor is accepted and releases it on every other exit."""
    payload = _step_payload(token)
    if payload is None:
        return None
    now = time.time()
    _sweep_used_jtis(now)
    jti = payload["jti"]
    if jti in _used_jtis:
        return None
    if jti in _reserved_jtis:
        raise StepTokenBusy(jti)
    exp = float(payload.get("exp") or now + _2FA_TOKEN_TTL)
    _reserved_jtis[jti] = exp
    return StepToken(sub=payload["sub"], jti=jti, exp=exp)


def release_2fa_session_token(step: StepToken) -> None:
    """Give a reserved step token back (a wrong code retries with it)."""
    _reserved_jtis.pop(step.jti, None)


def spend_2fa_session_token(step: StepToken) -> None:
    """The factor was accepted: the token never opens another session."""
    _reserved_jtis.pop(step.jti, None)
    _used_jtis[step.jti] = step.exp

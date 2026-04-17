"""TOTP two-factor authentication utilities."""

import hashlib
import json
import secrets
import time

import jwt
import pyotp

import config
from storage.credential_store import _encrypt, _decrypt


def generate_totp_secret() -> str:
    """Generate a new TOTP secret (base32)."""
    return pyotp.random_base32()


def get_totp_uri(secret: str, email: str, issuer: str = "OtoDock") -> str:
    """Get otpauth:// URI for QR code generation."""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    """Verify a 6-digit TOTP code (±1 window for clock skew)."""
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


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

# Successful use consumes the step token (single-use): a leaked/replayed token
# must not mint a second session. In-memory jti → exp, same posture as the
# rate limiter (resets on restart — the 5-min TTL bounds the exposure; each
# replica keeps its own set). Failed code attempts do NOT consume — the user
# retries a typo against the same token.
_used_jtis: dict[str, float] = {}


def _sweep_used_jtis(now: float) -> None:
    for jti in [j for j, exp in _used_jtis.items() if exp < now]:
        del _used_jtis[jti]


def create_2fa_session_token(sub: str) -> str:
    """Create a short-lived JWT for the 2FA verification step."""
    payload = {
        "sub": sub,
        "purpose": "2fa",
        "jti": secrets.token_hex(8),
        "iat": int(time.time()),
        "exp": int(time.time()) + _2FA_TOKEN_TTL,
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def validate_2fa_session_token(token: str) -> str | None:
    """Validate a 2FA session token. Returns sub, or None (invalid/expired/used)."""
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None
    if payload.get("purpose") != "2fa":
        return None
    if payload.get("jti", "") in _used_jtis:
        return None
    return payload.get("sub")


def consume_2fa_session_token(token: str) -> None:
    """Mark a step token as spent — call ONLY after the 2FA code verified."""
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return
    jti = payload.get("jti", "")
    if jti:
        now = time.time()
        _sweep_used_jtis(now)
        _used_jtis[jti] = payload.get("exp", now + _2FA_TOKEN_TTL)

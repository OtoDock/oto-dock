"""Password hashing and strength validation."""

import contextlib
import secrets

import bcrypt
from zxcvbn import zxcvbn

_DEFAULT_MIN_SCORE = 3
_DEFAULT_MIN_LENGTH = 8


def _get_min_score() -> int:
    """Get minimum password score from platform settings (DB), fallback to default."""
    try:
        from storage.database import get_platform_setting
        val = get_platform_setting("password_min_score")
        if val:
            score = int(val)
            if 0 <= score <= 4:
                return score
    except Exception:
        pass
    return _DEFAULT_MIN_SCORE


def _get_min_length() -> int:
    """Get minimum password length from platform settings (DB), fallback to default."""
    try:
        from storage.database import get_platform_setting
        val = get_platform_setting("password_min_length")
        if val:
            length = int(val)
            if length >= 4:
                return length
    except Exception:
        pass
    return _DEFAULT_MIN_LENGTH


def hash_password(plain: str) -> str:
    """Hash a password with bcrypt (cost factor 12)."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# A fixed valid bcrypt hash (of a random secret) used only to burn the same
# ~cost-12 CPU time on the "no such account" path as a real verify does, so
# login response time doesn't reveal whether an email maps to a local account.
_DUMMY_HASH = bcrypt.hashpw(secrets.token_bytes(16), bcrypt.gensalt(rounds=12))


def dummy_verify() -> None:
    """Constant-time-equalizer: run a throwaway bcrypt compare so the
    unknown-email / non-local / passwordless branches cost the same as a real
    password check (defeats a login timing oracle for account existence)."""
    with contextlib.suppress(Exception):
        bcrypt.checkpw(b"otodock-timing-equalizer", _DUMMY_HASH)


def check_password_strength(password: str) -> tuple[bool, str, int]:
    """Check password strength using zxcvbn entropy estimation.

    Returns (passes, feedback_message, score).
    Min score and min length read from platform_settings DB.
    """
    min_length = _get_min_length()
    min_score = _get_min_score()

    if len(password) < min_length:
        return False, f"Password must be at least {min_length} characters.", 0

    result = zxcvbn(password)
    score = result["score"]

    if score >= min_score:
        return True, "", score

    # Build feedback from zxcvbn
    feedback = result.get("feedback", {})
    warning = feedback.get("warning", "")
    suggestions = feedback.get("suggestions", [])
    msg = warning or "Password is too weak."
    if suggestions:
        msg += " " + " ".join(suggestions)
    return False, msg, score


def generate_temp_password() -> str:
    """Generate a random temporary password (URL-safe, 16 chars)."""
    return secrets.token_urlsafe(16)

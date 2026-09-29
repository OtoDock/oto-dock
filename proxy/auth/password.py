"""Password hashing and strength validation.

bcrypt (cost 12, about a core for 250 ms) and zxcvbn (pure Python, holding
the GIL) never run on the event loop: handlers call the ``*_async`` twins,
which run them in a worker thread behind their own semaphores. Passwords are
capped at 72 bytes, bcrypt's own limit, before either runs.
"""

import asyncio
import contextlib
import secrets

import bcrypt
from zxcvbn import zxcvbn

import config

_DEFAULT_MIN_SCORE = 3
_DEFAULT_MIN_LENGTH = 8

# bcrypt reads at most 72 bytes of a password. bcrypt 4 cut a longer one
# silently when it hashed; bcrypt 5 raises. A new password is refused past
# it; a verify compares the first 72 bytes, as the stored hash does.
MAX_PASSWORD_BYTES = 72
_TOO_LONG = ("Password must be at most 72 bytes (fewer characters when it uses "
             "accented letters or symbols).")


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
    raw = plain.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError(_TOO_LONG)
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:MAX_PASSWORD_BYTES], hashed.encode("utf-8"))
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
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return False, _TOO_LONG, 0

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


# --- off the event loop -----------------------------------------------------

class HashBusy(Exception):
    """Too many password hashes are already waiting their turn."""


class _Gates:
    """The semaphores, bound to the running loop and rebuilt when it changes
    (several loops share the process in the test suite; a semaphore with
    waiters is bound to the loop that created it)."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.hash: asyncio.Semaphore | None = None
        self.strength: asyncio.Semaphore | None = None
        self.waiting = 0

    def current(self) -> "_Gates":
        loop = asyncio.get_running_loop()
        if self.loop is not loop:
            self.loop = loop
            self.hash = asyncio.Semaphore(config.AUTH_HASH_CONCURRENCY)
            # zxcvbn holds the GIL: one at a time keeps the loop's share.
            self.strength = asyncio.Semaphore(1)
            self.waiting = 0
        return self


_gates = _Gates()


async def _hashing(fn, *args):
    g = _gates.current()
    if g.hash.locked() and g.waiting >= config.AUTH_HASH_MAX_WAITERS:
        raise HashBusy()
    g.waiting += 1
    try:
        await g.hash.acquire()
    finally:
        g.waiting -= 1
    try:
        return await asyncio.to_thread(fn, *args)
    finally:
        g.hash.release()


async def verify_password_async(plain: str, hashed: str) -> bool:
    return await _hashing(verify_password, plain, hashed)


async def dummy_verify_async() -> None:
    await _hashing(dummy_verify)


async def hash_password_async(plain: str) -> str:
    return await _hashing(hash_password, plain)


async def check_password_strength_async(password: str) -> tuple[bool, str, int]:
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return False, _TOO_LONG, 0
    g = _gates.current()
    async with g.strength:
        return await asyncio.to_thread(check_password_strength, password)

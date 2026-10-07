"""Brute-force protection: a generic keyed rate limiter + the account tarpit.

The keyed limiter is in-memory (resets on restart — fine for brute-force
defence; note the multi-replica caveat: each process keeps its own counters).
Buckets + thresholds are centralized in ``config.RATE_LIMIT_RULES`` so every
limit is one env var away. Each ``(bucket, key)`` tracks a sliding window with
exponential backoff once the window's attempt cap is exceeded.

Two entry points:

* ``hit(bucket, key)``: **check-then-record**, done synchronously with no
  ``await`` in between, so it is burst-safe: N concurrent requests each
  check and record before any of them yields, so they can't all slip under
  the cap; a refused attempt is recorded too.
  Use this at the TOP of a handler for surfaces with no legitimate high
  frequency (2FA, password reset, OAuth start, webhook fire).
* ``check_rate_limit`` / ``record_attempt``: the split check/record pair.
  The login counts every attempt with ``hit`` before its first await and
  gives back the ones that tested nothing or were right
  (``release_attempt``), so a burst from one address cannot all slip under
  the cap while the password hashes run.
"""

import hashlib
import logging
import secrets
import time
from collections import Counter
from datetime import datetime

import jwt

import config
from storage import database as db
from storage.pg import run_db

logger = logging.getLogger("claude-proxy")


class _Attempts(dict):
    """``(bucket, key) → {count, first_at, blocked_until, block_count}``,
    counting the keys of each bucket as entries come and go."""

    def __init__(self) -> None:
        super().__init__()
        self.per_bucket: Counter = Counter()
        # A bucket found full of blocked keys is not scanned again before
        # this time: a flood of fresh keys must not rescan it per request.
        self.full_until: dict[str, float] = {}

    def __setitem__(self, k, v) -> None:
        if k not in self:
            self.per_bucket[k[0]] += 1
        super().__setitem__(k, v)

    def __delitem__(self, k) -> None:
        super().__delitem__(k)
        self._dec(k[0])

    def pop(self, k, *default):
        if k in self:
            self._dec(k[0])
        return super().pop(k, *default)

    def clear(self) -> None:
        super().clear()
        self.per_bucket.clear()
        self.full_until.clear()

    def _dec(self, bucket: str) -> None:
        self.per_bucket[bucket] -= 1
        if self.per_bucket[bucket] <= 0:
            del self.per_bucket[bucket]


_attempts = _Attempts()
_last_cleanup = 0.0
_CLEANUP_EVERY = 300  # sweep stale entries at most every 5 min
# A bucket keeps at most this many keys: a flood of fresh keys (rotating
# addresses, invented emails) cannot grow it without bound. At the bound the
# bucket's expired entries go, then the oldest that were never blocked (a
# blocked key and its escalation stay); with nothing left to free, a new
# key goes untracked rather than refused (the account tarpit and the other
# buckets still apply).
_EVICT_FRACTION = 10
_FULL_RESCAN_S = 60.0
_full_logged: dict[str, float] = {}


def _rule(bucket: str) -> dict:
    """Resolve a bucket's thresholds, falling back to the login defaults so an
    unknown bucket name still gets *some* protection rather than none."""
    return config.RATE_LIMIT_RULES.get(bucket) or config.RATE_LIMIT_RULES["login"]


def _cleanup(now: float) -> None:
    global _last_cleanup
    if now - _last_cleanup < _CLEANUP_EVERY:
        return
    _last_cleanup = now
    stale = [
        k for k, d in _attempts.items()
        # keep while inside its window OR still serving a block
        if now - d["first_at"] > _rule(k[0])["max_block"] and d.get("blocked_until", 0) < now
    ]
    for k in stale:
        del _attempts[k]


def check_rate_limit(bucket: str, key: str) -> tuple[bool, int]:
    """Read-only: is ``(bucket, key)`` allowed right now? Returns
    ``(allowed, retry_after_seconds)``. May arm a block when the cap is met."""
    now = time.time()
    _cleanup(now)
    rule = _rule(bucket)
    entry = _attempts.get((bucket, key))
    if not entry:
        return True, 0

    blocked_until = entry.get("blocked_until", 0)
    if blocked_until > now:
        return False, int(blocked_until - now)

    if now - entry["first_at"] > rule["window"]:
        # Window lapsed and no active block → reset the COUNTING window but
        # KEEP block_count. Deleting the entry here (the old behaviour) wiped
        # the escalation history every window, so a patient attacker who
        # spaced attempts one window apart always faced the base block and
        # the documented exponential backoff never materialized. _cleanup
        # still forgets a genuinely idle key after max_block.
        entry["count"] = 0
        entry["first_at"] = now
        entry["blocked_until"] = 0
        return True, 0

    if entry["count"] < rule["max"]:
        return True, 0

    # Cap reached within the window → arm an exponential block.
    block_count = entry.get("block_count", 0)
    block_duration = min(rule["base_block"] * (2 ** block_count), rule["max_block"])
    entry["blocked_until"] = now + block_duration
    entry["block_count"] = block_count + 1
    return False, block_duration


def _make_room(bucket: str, now: float) -> bool:
    """Free keys in a full bucket; False when nothing may go."""
    rule = _rule(bucket)
    mine = [(k, d) for k, d in _attempts.items() if k[0] == bucket]
    stale = [k for k, d in mine
             if now - d["first_at"] > rule["max_block"] and d.get("blocked_until", 0) < now]
    for k in stale:
        del _attempts[k]
    cap = config.RATE_LIMIT_MAX_KEYS
    if _attempts.per_bucket[bucket] < cap:
        return True
    quiet = sorted((d["first_at"], k) for k, d in mine
                   if k in _attempts and not d.get("block_count") and d.get("blocked_until", 0) < now)
    for _, k in quiet[:max(1, cap // _EVICT_FRACTION)]:
        del _attempts[k]
    if _attempts.per_bucket[bucket] < cap:
        return True
    _attempts.full_until[bucket] = now + _FULL_RESCAN_S
    if now - _full_logged.get(bucket, 0.0) > 3600:
        _full_logged[bucket] = now
        logger.warning("Rate limit bucket %s is full (%d keys, all blocked): new keys go untracked",
                       bucket, cap)
    return False


def record_attempt(bucket: str, key: str) -> None:
    """Count one attempt against ``(bucket, key)`` (starts/rolls the window)."""
    now = time.time()
    entry = _attempts.get((bucket, key))
    if entry is None and _attempts.per_bucket[bucket] >= config.RATE_LIMIT_MAX_KEYS:
        if now < _attempts.full_until.get(bucket, 0.0) or not _make_room(bucket, now):
            return
    rule = _rule(bucket)
    # Never roll the window (which would zero blocked_until) while a block is
    # still being served — a single probe during the block would otherwise
    # lift it. While blocked, just keep counting on the existing entry.
    blocked = bool(entry) and entry.get("blocked_until", 0) > now
    if not entry or (now - entry["first_at"] > rule["window"] and not blocked):
        _attempts[(bucket, key)] = {
            "count": 1, "first_at": now, "blocked_until": 0,
            "block_count": entry.get("block_count", 0) if entry else 0,
        }
    else:
        entry["count"] += 1


def hit(bucket: str, key: str) -> tuple[bool, int]:
    """Burst-safe entry guard. Checks the attempts SO FAR, then records this
    one — both synchronously with NO ``await`` between, so the pair is atomic
    under the event loop and concurrent requests can't all slip under the cap
    (the bypass only exists when an ``await`` separates check from record).
    ``max`` is the count allowed within the window. Returns
    ``(allowed, retry_after_seconds)`` — call once at the top of a handler."""
    allowed, retry_after = check_rate_limit(bucket, key)
    record_attempt(bucket, key)
    return allowed, retry_after


def release_attempt(bucket: str, key: str) -> None:
    """Give back one attempt ``hit`` counted (never below zero)."""
    entry = _attempts.get((bucket, key))
    if entry and entry["count"] > 0:
        entry["count"] -= 1


def clear_rate_limit(bucket: str, key: str) -> None:
    """Drop tracking for ``(bucket, key)`` (e.g. a link unlocked by its
    password)."""
    _attempts.pop((bucket, key), None)


def forgot_email_key(email: str) -> str:
    """The forgot-password bucket's per-address key: a hash, so a stored key
    is a fixed size whatever the request carried."""
    digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()[:32]
    return f"email:{digest}"


# --- Login address limiter: thin wrappers over the "login" bucket ---------
# Keyed by ``lan_check.auth_bucket_key``. Every attempt is counted before the
# login's first await (``hit``); one that tested no password (the tarpit
# refused it) or had the right one is given back, so a correct password never
# blocks the legitimate user.

def hit_login(key: str) -> tuple[bool, int]:
    return hit("login", key)


def release_login_attempt(key: str) -> None:
    release_attempt("login", key)


# --- Account Tarpit (DB-backed) ---

_TARPIT_THRESHOLD = 5  # attempts before tarpit kicks in
_TARPIT_BASE_DELAY = 1.0  # seconds
_TARPIT_MAX_DELAY = 16.0  # cap


def check_account_tarpit(sub: str) -> tuple[bool, float]:
    """Check if an account is tarpitted.

    Returns (allowed, wait_seconds). No lockout — just delay.
    """
    user = db.get_user(sub)
    if not user:
        return True, 0

    attempts = user.get("failed_login_attempts", 0)
    if attempts < _TARPIT_THRESHOLD:
        return True, 0

    last_failed = user.get("last_failed_login")
    if not last_failed:
        return True, 0

    # Calculate delay: 2^(attempts - threshold) seconds, capped
    excess = attempts - _TARPIT_THRESHOLD
    delay = min(_TARPIT_BASE_DELAY * (2 ** excess), _TARPIT_MAX_DELAY)

    # Check if enough time has passed since last failure
    try:
        last_ts = datetime.fromisoformat(last_failed).timestamp()
        elapsed = time.time() - last_ts
        if elapsed >= delay:
            return True, 0
        return False, delay - elapsed
    except (ValueError, TypeError):
        return True, 0


def _clear_account_failures(sub: str) -> None:
    db.clear_failed_logins(sub)
    db.reset_login_attempts(sub)


async def record_successful_login(sub: str) -> None:
    """Clear the account's counters after a full login, through the DB
    executor. No address bucket is cleared: the route gave back the attempt
    that was right, and the address's other failures stay counted until
    their window lapses."""
    await run_db(_clear_account_failures, sub)


# --- Trusted devices (the OWASP device-cookie pattern) ---
#
# A browser that completed a full login for an account carries a signed
# device token for it: that browser's password logins skip the account
# tarpit (an attacker without the cookie still meets it) and count their
# failures against the device instead (the ``login_device`` bucket).

DEVICE_TOKEN_DAYS = 180
_DEVICE_PURPOSE = "trusted_device"


def mint_device_token(sub: str) -> str:
    now = int(time.time())
    return jwt.encode({
        "purpose": _DEVICE_PURPOSE, "sub": sub, "jti": secrets.token_urlsafe(16),
        "iat": now, "exp": now + DEVICE_TOKEN_DAYS * 86400,
    }, config.JWT_SECRET, algorithm="HS256")


def device_token_claims(token: str) -> dict | None:
    """The claims of a valid device token, or None (bad signature, expired,
    another token type)."""
    if not token:
        return None
    try:
        claims = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    if claims.get("purpose") != _DEVICE_PURPOSE or not claims.get("sub") or not claims.get("jti"):
        return None
    return claims


def trusted_device_for(claims: dict | None, user: dict) -> str:
    """The device key (the token's ``jti``) when ``claims`` vouch for this
    account in this browser: the same account, minted after its last password
    change, and the device's own bucket not blocked. "" otherwise.

    A vouched attempt is counted against the device at once, the check and
    the count with no await between, so a burst presenting one cookie cannot
    all pass the check while the hashes run; ``release_device_attempt``
    gives it back when the password was right or none was checked."""
    if not claims or claims.get("sub") != user.get("sub"):
        return ""
    changed = user.get("password_changed_at")
    if changed:
        try:
            if int(claims.get("iat") or 0) < datetime.fromisoformat(changed).timestamp() - 5:
                return ""
        except (ValueError, TypeError):
            return ""
    jti = str(claims["jti"])
    allowed, _ = check_rate_limit("login_device", jti)
    if not allowed:
        return ""
    record_attempt("login_device", jti)
    return jti


def release_device_attempt(device_key: str) -> None:
    release_attempt("login_device", device_key)

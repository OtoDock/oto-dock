"""Brute-force protection: a generic keyed rate limiter + the account tarpit.

The keyed limiter is in-memory (resets on restart — fine for brute-force
defence; note the multi-replica caveat: each process keeps its own counters).
Buckets + thresholds are centralized in ``config.RATE_LIMIT_RULES`` so every
limit is one env var away. Each ``(bucket, key)`` tracks a sliding window with
exponential backoff once the window's attempt cap is exceeded.

Two entry points:

* ``hit(bucket, key)`` — **record-then-check**, done synchronously with no
  ``await`` in between, so it is burst-safe: N concurrent requests each
  increment before any of them yields, so they can't all slip under the cap.
  Use this at the TOP of a handler for surfaces with no legitimate high
  frequency (2FA, password reset, OAuth start, webhook fire).
* ``check_rate_limit`` / ``record_attempt`` — the split check/record pair, kept
  for the login flow which counts only *failed* attempts.
"""

import time
from datetime import datetime

import config
from storage import database as db

# (bucket, key) → {count, first_at, blocked_until, block_count}
_attempts: dict[tuple[str, str], dict] = {}
_last_cleanup = 0.0
_CLEANUP_EVERY = 300  # sweep stale entries at most every 5 min


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
        del _attempts[(bucket, key)]
        return True, 0

    if entry["count"] < rule["max"]:
        return True, 0

    # Cap reached within the window → arm an exponential block.
    block_count = entry.get("block_count", 0)
    block_duration = min(rule["base_block"] * (2 ** block_count), rule["max_block"])
    entry["blocked_until"] = now + block_duration
    entry["block_count"] = block_count + 1
    return False, block_duration


def record_attempt(bucket: str, key: str) -> None:
    """Count one attempt against ``(bucket, key)`` (starts/rolls the window)."""
    now = time.time()
    entry = _attempts.get((bucket, key))
    rule = _rule(bucket)
    if not entry or now - entry["first_at"] > rule["window"]:
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


def clear_rate_limit(bucket: str, key: str) -> None:
    """Drop tracking for ``(bucket, key)`` (e.g. on a successful login)."""
    _attempts.pop((bucket, key), None)


# --- Login IP limiter: thin wrappers over the "login" bucket --------------
# Login counts only FAILED attempts (a correct password must never block the
# legitimate user), so it keeps the split check/record pair.

def check_ip_rate_limit(ip: str) -> tuple[bool, int]:
    return check_rate_limit("login", ip)


def record_ip_attempt(ip: str) -> None:
    record_attempt("login", ip)


def clear_ip_attempts(ip: str) -> None:
    clear_rate_limit("login", ip)


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


def record_failed_attempt(ip: str, sub: str | None):
    """Record a failed login attempt for both IP and account."""
    record_ip_attempt(ip)
    if sub:
        db.record_failed_login(sub)


def record_successful_login(ip: str, sub: str):
    """Clear rate limiting on successful login."""
    clear_ip_attempts(ip)
    db.reset_login_attempts(sub)

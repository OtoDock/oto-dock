"""Shared async lock manager for OAuth credential operations.

Three call sites must coordinate to avoid interleaved partial writes of
refreshed tokens:
  1. Lazy refresh inside ``OAuthProvider.refresh()`` (called from any
     session at any time when an access token is near expiry).
  2. Background refresh worker (``services/oauth_refresh_worker``) scanning
     and refreshing tokens with <5 min remaining lifetime.

Without serialization, the two can race and produce a token file that
mixes fields from different refreshes — particularly bad when a vendor
rotates refresh tokens on every refresh and one writer overwrites with
a stale refresh value.

Lock granularity: ``(user_sub, mcp_name, account_label)`` — the finest
key that matches what gets persisted (one token file per account). Locks
are kept in a process-local dict; this is fine for single-replica deploys
(everything OtoDock ships today). Multi-replica deploys (post-launch
hibernation work) need a Redis/DB advisory lock here.
"""

from __future__ import annotations

import asyncio

# Per-(user_sub, mcp_name, account_label) async lock.
_locks: dict[tuple[str, str, str], asyncio.Lock] = {}


def get_lock(user_sub: str, mcp_name: str, account_label: str) -> asyncio.Lock:
    """Return the asyncio.Lock for this credential resource.

    Locks are created lazily and never freed (a fresh asyncio.Lock costs
    ~200 bytes; the universe of (user, mcp, account) combinations is
    bounded by users × MCPs × accounts which is in the thousands at most).
    """
    key = (user_sub, mcp_name, account_label)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def discard_lock(user_sub: str, mcp_name: str, account_label: str) -> None:
    """Forget the lock for a freed resource (e.g. after account delete).

    Safe to call concurrently with ``get_lock`` because the next caller
    will see a freshly-created lock; only the previous holder retains
    a reference to the discarded one.
    """
    _locks.pop((user_sub, mcp_name, account_label), None)


def active_lock_count() -> int:
    """Diagnostic: how many lock objects are in the registry."""
    return len(_locks)

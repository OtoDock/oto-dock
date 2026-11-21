"""An account's rests: the account-wide and the per-model rest, their ends, the persisted rows and the
error texts that start one.
"""

from __future__ import annotations

import logging
import time

from services.engines import subscription_windows as _windows
from services.engines.pool_writer import (
    _queue_write,
)
from storage.billing import subscription_store

logger = logging.getLogger(__name__)


# Failover: subscriptions temporarily skipped after a provider error — sub_id → unix
# ts until which it's de-prioritised. The four rest structures here are all
# the readers read; each set and each clear also queues its row on the binding
# writer (subscription_rests), and the boot refills them (``load_rests``).
_throttled_until: dict[str, float] = {}


# The subset of _throttled_until resting due to a REAL account rate/usage limit
# (the full cooldown class) — the reactive scope-rebalance trigger. A transient
# overload nudge never lands here (moving whole scopes over a 529 blip would be
# pure churn). Entries clear with their _throttled_until expiry.
_throttled_hard: set[str] = set()


_THROTTLE_COOLDOWN_S = 900  # 15 min — a genuine account rate/usage/quota limit


# A usage-limit ending that names its reached window and a future reset rests
# the account until that reset (``rest_after_limit``), bounded by the window's
# declared length. An account-wide window rests in _throttled_until (the hard
# class) with its key here, sub_id → (window key, rested at); a model family's
# window rests only spawns of that scope, (sub_id, scope key) → (until,
# rested at). A later reading of the window with headroom ends either early
# (``clear_rests_with_headroom``, which compares the reading's instant with
# the rested-at one, so the row carries that instant exactly).
_window_rests: dict[str, tuple[str, float]] = {}


_scoped_rests: dict[tuple[str, str], tuple[float, float]] = {}


# A transient, server-side overload (Anthropic 529 "Overloaded") is NOT an account
# limit: the account is fine, the provider is momentarily busy, and the CLI already
# retries. So it gets a tiny cooldown — a one-turn failover nudge for multi-account
# installs — never a long lockout that would take a single-account install fully
# offline over a blip (the user can just retry immediately).
_OVERLOAD_COOLDOWN_S = 10


def account_resting(sub_id: str) -> bool:
    """Is the account resting as a whole right now (a usage limit or a
    throttle on the account, not a model family's own window)? The
    per-person engine resolution leaves such an account out while another
    engine resolves (``services/engines/engine_resolution.py``)."""
    return _is_throttled(sub_id)


def _is_throttled(sub_id: str) -> bool:
    until = _throttled_until.get(sub_id)
    if until is None:
        return False
    if time.time() >= until:
        _end_account_rest(sub_id)
        return False
    return True


def _scope_resting(sub_id: str, scope_key: str) -> bool:
    """Is the account resting for spawns of this model scope (a usage limit
    on that family's own window)? "" (no per-model window) never is."""
    if not scope_key:
        return False
    entry = _scoped_rests.get((sub_id, scope_key))
    if entry is None:
        return False
    if time.time() >= entry[0]:
        _end_scoped_rest(sub_id, scope_key)
        return False
    return True


def _end_account_rest(sub_id: str) -> None:
    """Drop the account-wide rest (its expiry, a reading with headroom) and
    its row."""
    _throttled_until.pop(sub_id, None)
    _throttled_hard.discard(sub_id)
    _window_rests.pop(sub_id, None)
    _queue_write("clearing a rest", subscription_store.clear_rest, sub_id, "")


def _end_scoped_rest(sub_id: str, scope_key: str) -> None:
    _scoped_rests.pop((sub_id, scope_key), None)
    _queue_write("clearing a rest", subscription_store.clear_rest, sub_id, scope_key)


def _rest_account(sub_id: str, until: float, *, window: str = "", hard: bool = False) -> bool:
    """Rest the account until ``until`` unless it already rests longer (a
    short cooldown never shortens a window's rest). ``window`` names the
    window the rest waits on, which a reading with headroom may end early;
    a rest that outlasts a window's drops the key (no reading ends it).
    ``hard`` marks a real account limit (``_throttled_hard``), on a rest
    already held too. The row is written as the structures then hold it.
    Returns whether the rest was set or extended."""
    extended = until > _throttled_until.get(sub_id, 0.0)
    if extended:
        _throttled_until[sub_id] = until
        if window:
            _window_rests[sub_id] = (window, time.time())
        else:
            _window_rests.pop(sub_id, None)
    marked = hard and sub_id not in _throttled_hard
    if marked:
        _throttled_hard.add(sub_id)
    if extended or marked:
        key, rested_at = _window_rests.get(sub_id, ("", time.time()))
        _queue_write("saving a rest", subscription_store.save_rest, sub_id, "",
                     _throttled_until.get(sub_id, until), rested_at, key,
                     sub_id in _throttled_hard)
    return extended


def clear_rests_with_headroom(sub_id: str, reading) -> None:
    """A window reading of the account (the poll or an in-band sample) that
    was observed after a window rest began and shows the rested window below
    its spill mark and not reached ends that rest early, account-wide or for
    one model scope. A plain cooldown waits on no window and is left alone.
    Safe from worker threads."""
    observed = reading.observed_at.timestamp()
    entry = _window_rests.get(sub_id)
    if entry is not None:
        window, rested_at = entry
        if observed > rested_at and _windows.has_headroom(reading, window):
            _end_account_rest(sub_id)
            logger.info("Pool: subscription %s has %s headroom again "
                        "(rest ended)", sub_id[:8], window)
    for (rested_sub, scope), (_until, rested_at) in list(_scoped_rests.items()):
        if (rested_sub == sub_id and observed > rested_at
                and _windows.has_headroom(reading, f"scoped:{scope}")):
            _end_scoped_rest(rested_sub, scope)
            logger.info("Pool: subscription %s has %s headroom again "
                        "(rest ended)", sub_id[:8], scope)


def load_rests() -> int:
    """Fill the rest structures from their rows (``subscription_rests``), at
    boot before the first selection. The store drops the rows that ended and
    those of accounts that are gone. Returns the rests loaded."""
    rows = subscription_store.load_rests(time.time())
    for row in rows:
        sub_id, scope = row["account_id"], row["scope"]
        if scope:
            _scoped_rests[(sub_id, scope)] = (row["until_at"], row["rested_at"])
            continue
        _throttled_until[sub_id] = row["until_at"]
        if row["window_key"]:
            _window_rests[sub_id] = (row["window_key"], row["rested_at"])
        if row["hard"]:
            _throttled_hard.add(sub_id)
    return len(rows)


# Provider errors that mean the ACCOUNT hit a real rate/usage/quota limit → rest it
# the full cooldown so the next turn fails over to a fresh account.
_LIMIT_ERROR_MARKERS = (
    "rate_limit", "rate limit", "ratelimit", "429", "too many requests",
    "usage limit", "usage credits", "out of usage", "quota", "insufficient_quota",
)


# Transient, server-side overload (Anthropic 529 "Overloaded") — NOT the account's
# fault. Kept separate so it gets the short nudge, not the 15-minute lockout.
_OVERLOAD_MARKERS = ("overloaded", "529")


def throttle_cooldown_for(message: str) -> int | None:
    """Cooldown (seconds) to rest the subscription bound to a failed turn, or ``None``
    to not throttle at all. Distinguishes a genuine account rate/usage limit (the full
    cooldown — fail over to another account) from a transient server overload (a brief
    nudge — the account is fine and the CLI retries). Conservative substring match — a
    false negative just skips failover; a false positive briefly rests one account.
    The vendor's brief throttle is the overload's class, whatever else its words hold
    ("not your usage limit", a 429)."""
    from core.events import turn_ending
    m = (message or "").lower()
    if turn_ending.is_brief_throttle_text(m):
        return _OVERLOAD_COOLDOWN_S
    if any(marker in m for marker in _LIMIT_ERROR_MARKERS):
        return _THROTTLE_COOLDOWN_S
    if any(marker in m for marker in _OVERLOAD_MARKERS):
        return _OVERLOAD_COOLDOWN_S
    return None


def looks_like_limit_error(message: str) -> bool:
    """True if a turn error warrants resting the account at all (a rate/usage limit OR
    a transient overload). Kept for boolean callers; use ``throttle_cooldown_for`` for
    the cooldown length."""
    return throttle_cooldown_for(message) is not None


def _rest_end(sub_id: str, scope_key: str) -> float | None:
    """When the account's rests for a spawn of this scope end (the later of
    an account-wide rest and the scope's own), or None when it rests for
    neither."""
    ends = []
    if _is_throttled(sub_id):
        ends.append(_throttled_until.get(sub_id, 0.0))
    if _scope_resting(sub_id, scope_key):
        entry = _scoped_rests.get((sub_id, scope_key))
        if entry is not None:
            ends.append(entry[0])
    return max(ends) if ends else None

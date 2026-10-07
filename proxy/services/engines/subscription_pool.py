"""Subscription pool manager — selects the right subscription for a session.

Resolution (single chokepoint — see acquire_subscription):
- USER-SCOPE (user_sub set): the user's own accounts (use_personal) first; then,
  only if Platform Auth is on, the admin pool restricted to BORROWABLE API
  credentials (api_key / relay / local_endpoint) — NEVER an admin OAuth
  subscription (those are strictly per-owner; see _USER_BORROWABLE_AUTH_TYPES).
- AGENT-SCOPE (user_sub None): the full platform pool, OAuth subscriptions included.
- None → caller surfaces a "no subscription" block.

Pool selection: scope-sticky first (sessions sharing a credential file stay on
one account — see credential_scope_key), then BYO before relay, then the
two-tier least-consumed headroom sort (5h recent burn, 7d weekly tiebreak),
with the store's least-active order breaking remaining ties.
Bindings are mirrored to the DB (subscription_session_bindings) so usage
attribution and stickiness survive proxy restarts.

Auth credentials reach a session through the ENGINE's adapter
(``ExecutionLayer.subscription_env`` — the pool looks the layer up by the
acquisition's execution path and hands it the ``SubscriptionHandle``):
- API key: the engine's key variable (``ANTHROPIC_API_KEY``, ``CODEX_API_KEY``, …)
- OAuth:   the engine's credential FILE payload, which its ``start_session``
  writes into the session's config dir (Claude ``.credentials.json``, Codex
  ``auth.json``). Never an env token: env is frozen at exec, so a live CLI
  could never pick up a rotation, and providers revoke older access tokens
  when the refresh token rotates. The CLIs re-read their credential file
  (Claude: mtime-watch + 401-recovery; Codex: guarded reload), so the pool
  rotates and FANS OUT — see ``ensure_fresh_and_fan_out`` and
  ``services/engines/token_fanout``.
- Local:   the engine's endpoint variable; a minted relay token arrives like
  a BYO key + endpoint

The pool is the SOLE rotator: session files carry a blank refresh token, so a
CLI physically cannot self-rotate (a cascade of CLI-side rotations is exactly
what revoked live sessions' tokens).

All functions are synchronous (call via asyncio.to_thread).
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import contextlib
import dataclasses
import logging
import threading
import time

from core.execution_layer import DEFAULT_EXECUTION_PATH, SubscriptionHandle
from core import placement
from storage.billing import subscription_status, subscription_store
from services.billing import pool_caps as _pool_caps
from services.engines import subscription_windows as _windows

logger = logging.getLogger(__name__)

# Track subscription_id → session_id for cleanup on session close.
# Guarded by _session_maps_lock: bind/release mutate it on the event loop while
# the rotation fan-out iterates it on a to_thread worker — an unguarded
# concurrent insert/pop during iteration raises "dictionary changed size".
_session_subscriptions: dict[str, str] = {}  # session_id → subscription_id
_session_maps_lock = threading.Lock()
# session_id → (layer, scope user_sub) — the ACQUISITION context this binding
# was resolved with: the exact (layer, user_sub) pair the config builder passed
# to acquire_subscription ("" = agent-scope/platform pool). Lets a selection
# change (scope checkboxes, delete, disable, Platform-Auth revoke) re-evaluate
# each live session against the same candidate lists and re-home it (see
# rebind_delisted_sessions). Only stamped bindings participate — a session
# bound without context is left alone (fail-soft).
_session_binding_ctx: dict[str, tuple[str, str]] = {}
# session_id → credential scope key (see credential_scope_key) — the live half
# of the scope-sticky lookup: a new spawn in a scope that already has a LIVE
# session bound to account X must reuse X (the scope shares ONE credential
# file; two accounts writing it is the last-write-wins / silent-401 hazard).
_session_scope_keys: dict[str, str] = {}
# scope_key → (subscription_id, acquired_at): closes the acquire→bind race —
# a second same-scope spawn between the first spawn's acquisition and its
# bind_session would otherwise miss the live map and pick by headroom.
# Consulted while fresh (see _SCOPE_RECENT_TTL_S), superseded by the live map.
_scope_recent: dict[str, tuple[str, float]] = {}
_SCOPE_RECENT_TTL_S = 180.0
# sub_id → expiresAt (epoch ms) of the access token most recently ISSUED to a
# spawn by resolve_subscription_env — may be earlier than the store's current
# token when the spawn-time refresh fail-softed to an aging stored token.
_issued_token_expiry: dict[str, int] = {}
# session_id → expiresAt (epoch ms) of the token in that session's credential
# FILE: snapshotted from the issued token by bind_session, then advanced by
# rotation fan-out per session whose file write actually landed (see
# session_token_expiry_ms). A session whose fan-out write failed keeps its old
# snapshot, so the turn-start guard retries the refresh for it.
_session_token_expiry: dict[str, int] = {}

# Backoff for failed OAuth refresh: sub_id → (fail_time, attempt_count)
# After a failed refresh, wait before retrying (prevents rate limit loops).
# Transient failures ONLY back off — they never expire the row: a provider
# outage must not lock an account out until a human reconnects it. The one
# single-shot terminal verdict is the provider's own `invalid_grant` (the
# login grant is dead), which expires the row immediately — see
# _refresh_error_terminal.
_refresh_backoff: dict[str, tuple[float, int]] = {}

# Sustained-auth-death escalation: sub_id → (first_fail_time, count) of
# 401-with-JSON-error refresh rejections. Not every provider says
# `invalid_grant` for a dead login — OpenAI answers a generic 401
# `invalid_request_error` forever — so a streak of provider auth rejections
# spanning BOTH thresholds below earns the same expired verdict. 401-only:
# RFC 6749 client-side request bugs (invalid_request / invalid_scope /
# unsupported_grant_type) surface as 400 and must stay transient forever.
# Other failures (400/429/5xx/non-JSON WAF pages/network errors) neither
# count nor reset the streak; only a successful refresh, a reconnect
# exchange, or the delivered verdict clears it. In-memory like
# _refresh_backoff: a restart just restarts the ~2 h clock.
_auth_fail_streaks: dict[str, tuple[float, int]] = {}
_AUTH_STREAK_MIN_ATTEMPTS = 8
_AUTH_STREAK_MIN_SPAN_S = 2 * 3600

# Selection cooldown after the TOKEN ENDPOINT itself rate-limits a refresh
# (not an account usage limit — just steer new work away briefly).
_REFRESH_RATELIMIT_COOLDOWN_S = 60

# Refresh at acquire only when the stored token has less runway than this — a
# new session otherwise REUSES the shared token generation (the official CLIs
# do the same: reuse until near expiry, never rotate per session). Rotating on
# ~every spawn was the 2026-07-06 outage mechanism: Anthropic REVOKES older
# outstanding access tokens on rotation, so each new chat killed every other
# live session's token. Rotation is safe for live sessions (each one gets the
# new token fanned out to its credential file), but stays rare — ~once per 6 h
# instead of per spawn — and every fresh spawn still gets ≥2 h of runway
# (the freshness worker + turn-start guard keep it above 45 min after that).
_SPAWN_REFRESH_RUNWAY_MS = 2 * 3600 * 1000
# A turn must never START on a token with less runway than a long turn can
# consume (mid-turn expiry = "Please run /login" inside a session that has no
# login; observed 2026-07-06 on 30-40 min working turns). The dashboard turn
# chokepoint refreshes + fans out below this; the freshness worker holds the
# same line for idle sessions between turns.
TURN_MIN_TOKEN_RUNWAY_MS = 45 * 60 * 1000
# Below this the stored token is too close to death to hand out at all.
_HARD_EXPIRY_BUFFER_MS = 300_000

# Single-flight refresh: providers rotate the refresh token on use, so a
# concurrent second refresh with the same (now-consumed) token fails and
# marches the backoff toward auto-expire. All refreshes for a subscription
# serialize on its lock and re-read the store before acting.
_refresh_locks_guard = threading.Lock()
_refresh_locks: dict[str, threading.Lock] = {}


def _refresh_lock(sub_id: str) -> threading.Lock:
    with _refresh_locks_guard:
        return _refresh_locks.setdefault(sub_id, threading.Lock())

# Headroom routing: consumption within these rolling windows approximates each
# account's remaining headroom, so a new chat lands on the least-consumed one.
# Two tiers, matched to how the providers actually reset (Claude consumer subs:
# a ~5h rolling window + weekly caps): the SHORT window is the primary key —
# right after everyone's reset all accounts tie at ~0 and the pool spreads by
# real recent burn — and the 7-day window breaks ties so weekly caps are still
# respected. A single long window remembers last week and keeps routing away
# from an account whose real headroom already reset (live-observed).
_CONSUMPTION_WINDOW_HOURS = 5
_CONSUMPTION_WINDOW_DAYS = 7

# Failover: subscriptions temporarily skipped after a provider error — sub_id → unix
# ts until which it's de-prioritised. In-memory (cleared on restart).
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
# (``clear_rests_with_headroom``). In-memory, like every rest.
_window_rests: dict[str, tuple[str, float]] = {}
_scoped_rests: dict[tuple[str, str], tuple[float, float]] = {}
# A transient, server-side overload (Anthropic 529 "Overloaded") is NOT an account
# limit: the account is fine, the provider is momentarily busy, and the CLI already
# retries. So it gets a tiny cooldown — a one-turn failover nudge for multi-account
# installs — never a long lockout that would take a single-account install fully
# offline over a blip (the user can just retry immediately).
_OVERLOAD_COOLDOWN_S = 10

# Scope rebalancing: stickiness pins every session of a credential scope to one
# account, and for a busy agent the pin never releases — so the pool must move
# the WHOLE scope when its account gets rate-limited (reactive) or drifts far
# above the pool's headroom (proactive). Drift knobs (operator-set 2026-07-11):
# ignore drift while the pinned account's 5h burn (est. API-equivalent USD) is
# under the floor — calibrated for Max-tier accounts, which burn several
# hundred $/window before capping (a Pro-tier account caps below the floor and
# is covered by the reactive trigger instead); move only when the burn is ≥
# RATIO× the cheapest eligible candidate's (roughly-even accounts stay put).
DRIFT_ABS_FLOOR_USD = 100.0
DRIFT_RATIO = 3.0
# After a scope moves (or a move is ATTEMPTED — a fan-out that keeps failing
# must not retry at full tick rate) it stays put for the cooldown, whatever
# the numbers say. The decaying 5h window supplies the rest of the hysteresis:
# right after A→B the old account's burn still reads high for hours, so the
# ratio can't flip straight back. In-memory: a restart just re-arms one move.
_SCOPE_REBALANCE_COOLDOWN_S = 45 * 60
_scope_rebalance_last: dict[str, float] = {}  # scope_key → monotonic ts


def _is_throttled(sub_id: str) -> bool:
    until = _throttled_until.get(sub_id)
    if until is None:
        return False
    if time.time() >= until:
        _throttled_until.pop(sub_id, None)
        _throttled_hard.discard(sub_id)
        _window_rests.pop(sub_id, None)
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
        _scoped_rests.pop((sub_id, scope_key), None)
        return False
    return True


def _rest_account(sub_id: str, until: float, *, window: str = "") -> bool:
    """Rest the account until ``until`` unless it already rests longer (a
    short cooldown never shortens a window's rest). ``window`` names the
    window the rest waits on, which a reading with headroom may end early;
    a rest that outlasts a window's drops the key (no reading ends it).
    Returns whether the rest was set or extended."""
    if until <= _throttled_until.get(sub_id, 0.0):
        return False
    _throttled_until[sub_id] = until
    if window:
        _window_rests[sub_id] = (window, time.time())
    else:
        _window_rests.pop(sub_id, None)
    return True


# Module import ≈ proxy boot. For the first minutes after a restart the live
# session registries are still warming (satellites reconnect and re-announce
# their surviving sessions lazily) — a persisted binding row must not be
# judged dead, let alone deleted, before its session had a chance to reappear.
_BOOT_MONOTONIC = time.monotonic()
_LIVENESS_BOOT_GRACE_S = 600.0


def within_boot_grace() -> bool:
    """True while the live-session registries are still warming after a proxy
    restart. Workers that act on a row *looking* unbound (e.g. the proactive
    unbound-row token refresh) must stand down during this window — a
    surviving satellite session's row is indistinguishable from a truly idle
    one until it re-announces."""
    return time.monotonic() - _BOOT_MONOTONIC < _LIVENESS_BOOT_GRACE_S


def _session_registered_live(session_id: str) -> bool | None:
    """Is this session live in ANY registry that can hold a bound session?
    Checks the pool's own live map, the interactive registry (local + remote
    PTYs, incl. re-adopted ones), then every execution layer through the
    registry (``find_layer_for_session``: the local engines and the remote
    layer, incl. post-restart adopted sessions — ``adopt_session``
    re-registers there without re-binding, which is exactly why the
    persisted rows exist; it never creates the remote layer). Returns None
    when a registry can't be consulted (import/attr failure) — the caller
    must fail SOFT and treat the session as live: wrongly deleting a live
    session's row breaks its usage attribution and scope pin."""
    try:
        if session_id in _session_subscriptions:
            return True
        from core.session import interactive_session
        if interactive_session.get(session_id) is not None:
            return True
        from core.session.session_manager import find_layer_for_session
        return find_layer_for_session(session_id) is not None
    except Exception:
        return None


def _consumption_window_starts() -> tuple[str, str]:
    """ISO start timestamps for the (short, long) consumption windows."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return (
        (now - timedelta(hours=_CONSUMPTION_WINDOW_HOURS)).isoformat(),
        (now - timedelta(days=_CONSUMPTION_WINDOW_DAYS)).isoformat(),
    )


def mark_subscription_throttled(session_id: str, *, cooldown_s: int = _THROTTLE_COOLDOWN_S) -> None:
    """Temporarily remove the subscription bound to ``session_id`` from selection
    after it hit a provider rate/usage limit, so the next chat/turn fails over to a
    fresh account. Best-effort + in-memory (cleared on restart).

    Read-through binding lookup (not just the live map): the interactive
    tailers report limits for sessions that may have outlived a proxy restart,
    where only the persisted row knows the account. A REAL limit (the full
    cooldown class) additionally fires the reactive scope rebalance — every
    scope pinned to this account gets re-homed onto a fresh one NOW, live
    sessions included, instead of erroring until the account's window resets.
    An overload nudge deliberately doesn't (see ``_throttled_hard``)."""
    sub_id = get_session_subscription(session_id)
    if not sub_id:
        return
    if _rest_account(sub_id, time.time() + cooldown_s):
        logger.info(f"Pool: throttled subscription {sub_id[:8]} for {cooldown_s}s (limit hit)")
    if cooldown_s >= _THROTTLE_COOLDOWN_S:
        _throttled_hard.add(sub_id)
        schedule_rebalance("provider limit")


def rest_after_limit(session_id: str, ending, err_msg: str = "") -> None:
    """Rest the subscription a failed turn ran on, by what ended it (the
    stream pump's ERROR branch):

    * a typed usage-limit ending that names its reached window and a reset
      still ahead: until that reset, bounded by the window's declared length.
      An account-wide window (the session or the weekly window) rests the
      account; a model family's window (``scoped:<scope key>``) rests it for
      spawns of that scope only (``usage_scope_key``), other models keep
      using it;
    * any other typed limit ending (no window, no reset, an unknown window):
      the usual cooldown (``_THROTTLE_COOLDOWN_S``);
    * no typed ending: the error text's class (``throttle_cooldown_for``: a
      limit the usual cooldown, a transient overload the brief nudge).

    Every eligible account resting still falls back to the one that frees
    first (``_select``), so a single-account install shows the vendor's own
    limit message rather than "no subscription"."""
    from core.events import turn_ending
    if ending is not None and ending.reason == turn_ending.LIMIT:
        if not _rest_until_reset(session_id, ending):
            mark_subscription_throttled(session_id)
        return
    cooldown = throttle_cooldown_for(err_msg)
    if cooldown:
        mark_subscription_throttled(session_id, cooldown_s=cooldown)


def _iso_epoch(value: str) -> float | None:
    from datetime import datetime, timezone
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _window_length_s(session_id: str, window: str) -> float | None:
    """The declared length of the window a limit ending names: the session's
    engine's declaration (its binding's acquisition context), else the
    longest any registered engine declares under that key. A model family's
    window settles by the quota window's length, as the readings do. None
    for a window nobody declares."""
    ctx = _session_binding_ctx.get(session_id)
    if ctx and ctx[0]:
        declared = [_windows.window_specs(ctx[0])]
    else:
        from core.session.session_manager import get_all_layers
        declared = [_windows.window_specs(name) for name in get_all_layers()]
    lengths: list[float] = []
    for specs in declared:
        if window.startswith("scoped:"):
            quota = _windows.quota_spec(specs)
            if quota is not None:
                lengths.append(float(quota.length_s))
        elif window in specs:
            lengths.append(float(specs[window].length_s))
    return max(lengths) if lengths else None


def _rest_until_reset(session_id: str, ending) -> bool:
    """The window rest of ``rest_after_limit``; False when the ending cannot
    carry one (no window, no future reset, a window nobody declares) or the
    session has no account."""
    window = getattr(ending, "window", "") or ""
    reset = _iso_epoch(getattr(ending, "resets_at", "") or "")
    now = time.time()
    if not window or reset is None or reset <= now:
        return False
    length = _window_length_s(session_id, window)
    if length is None:
        return False
    sub_id = get_session_subscription(session_id)
    if not sub_id:
        return False
    until = min(reset, now + length)
    if window.startswith("scoped:"):
        scope = window[len("scoped:"):]
        if not scope:
            return False
        held = _scoped_rests.get((sub_id, scope))
        if held is None or held[0] < until:
            _scoped_rests[(sub_id, scope)] = (until, now)
        logger.info(f"Pool: subscription {sub_id[:8]} rests for {scope} until its window "
                    f"resets ({int(until - now)}s, usage limit)")
    else:
        if _rest_account(sub_id, until, window=window):
            logger.info(f"Pool: subscription {sub_id[:8]} rests until its {window} window "
                        f"resets ({int(until - now)}s, usage limit)")
        _throttled_hard.add(sub_id)
    schedule_rebalance("provider limit")
    return True


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
            _throttled_until.pop(sub_id, None)
            _throttled_hard.discard(sub_id)
            _window_rests.pop(sub_id, None)
            logger.info(f"Pool: subscription {sub_id[:8]} has {window} headroom again "
                        f"(rest ended)")
    for (rested_sub, scope), (_until, rested_at) in list(_scoped_rests.items()):
        if (rested_sub == sub_id and observed > rested_at
                and _windows.has_headroom(reading, f"scoped:{scope}")):
            _scoped_rests.pop((rested_sub, scope), None)
            logger.info(f"Pool: subscription {sub_id[:8]} has {scope} headroom again "
                        f"(rest ended)")


def throttle_from_cli_error(session_id: str, error_text: str) -> None:
    """Classify a CLI-reported API-error line and rest the session's account if
    it names a provider limit/overload — the interactive-session counterpart of
    the stream pump's ERROR-event hook (before this, terminals never reported
    limits to the pool at all). Callers must pass ONLY text the CLI itself
    marked as an error (transcript ``isApiErrorMessage`` rows, codex ``error``
    events) — never model prose, which routinely DISCUSSES rate limits (a dev
    agent working on this very codebase would trip a substring match daily).
    Safe from tailer worker threads."""
    cooldown = throttle_cooldown_for(error_text)
    if cooldown:
        mark_subscription_throttled(session_id, cooldown_s=cooldown)


# Provider errors that mean the ACCOUNT hit a real rate/usage/quota limit → rest it
# the full cooldown so the next turn fails over to a fresh account.
_LIMIT_ERROR_MARKERS = (
    "rate_limit", "rate limit", "ratelimit", "429", "too many requests",
    "usage limit", "usage credits", "quota", "insufficient_quota",
)
# Transient, server-side overload (Anthropic 529 "Overloaded") — NOT the account's
# fault. Kept separate so it gets the short nudge, not the 15-minute lockout.
_OVERLOAD_MARKERS = ("overloaded", "529")


def throttle_cooldown_for(message: str) -> int | None:
    """Cooldown (seconds) to rest the subscription bound to a failed turn, or ``None``
    to not throttle at all. Distinguishes a genuine account rate/usage limit (the full
    cooldown — fail over to another account) from a transient server overload (a brief
    nudge — the account is fine and the CLI retries). Conservative substring match — a
    false negative just skips failover; a false positive briefly rests one account."""
    m = (message or "").lower()
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

# Auth types a NON-owner user may borrow from the admin pool. OAuth consumer
# subscriptions are deliberately excluded (they are strictly per-owner). To
# restore admin-subscription pooling for users, add 'oauth' here — nothing else
# in the resolver changes.
_USER_BORROWABLE_AUTH_TYPES = frozenset({"api_key", "relay", "local_endpoint"})

# Auth types whose per-turn cost is an ACTIVITY estimate, not money: a
# subscription's flat fee (oauth) or a local model (local_endpoint). The chat
# hides the cost line + gauge for these (usage is still recorded). Kept as a
# hide-list so an unknown future type keeps showing — today's behaviour.
_ACTIVITY_AUTH_TYPES = frozenset({"oauth", "local_endpoint"})


class NoSubscriptionError(Exception):
    """Raised when a spawn resolves to no usable credentials, so the dashboard
    can show an actionable message instead of a cryptic provider 401.
    ``reason`` ∈ {auth_off, admin_oauth_only, no_pool, none, throttled,
    own_sub_expired} for user-scoped work (see ``user_scope_block_reason``),
    or ``pool_cap`` for either scope when the pool's subscription cap refused
    the spawn (``message`` carries the cap's own wording)."""

    _MESSAGES = {
        "throttled": "Your subscription is briefly resting after the provider reported "
                     "a rate limit or overload — try again in a few seconds.",
        "own_sub_expired": "Your connected account's login has expired — reconnect it "
                           "in User Settings → AI Engines to keep using your "
                           "subscription.",
        "auth_off": "You don't have a subscription for this execution layer. "
                    "Connect your account in your User Settings.",
        "admin_oauth_only": "You don't have a subscription for this execution layer. "
                            "Connect your account in your User Settings.",
        "no_pool": "No subscription is configured for this execution layer. Connect your "
                   "account in your User Settings, or ask an administrator to add one.",
        "none": "No usable subscription for this execution layer. "
                "Connect your account in your User Settings.",
        "pool_cap": "The subscription cap on this pool is reached. It clears as the "
                    "accounts' windows reset; change the cap on the Usage page.",
    }

    def __init__(self, reason: str, message: str = ""):
        self.reason = reason
        super().__init__(message or self._MESSAGES.get(reason, self._MESSAGES["none"]))


def _window_readings(candidates: list[dict], now=None) -> dict:
    """The effective provider-window reading per OAuth candidate that has a
    sample (``services.engines.subscription_windows``); empty when the
    platform setting is off or nothing has been read yet. ``candidates`` are
    subscription ROWS — the row's ``layer`` names the windows its engine's
    vendor reports, which the reading is settled against."""
    if not _windows.is_enabled():
        return {}
    oauth = [c for c in candidates if c.get("auth_type") == "oauth" and c.get("id")]
    if not oauth:
        return {}
    try:
        readings = _windows.latest_readings(subscription_store, oauth, now)
    except Exception:
        logger.debug("Pool: window sample read failed", exc_info=True)
        return {}
    return readings if isinstance(readings, dict) else {}


def _scope_key(layer: str | None, model: str) -> str:
    """The per-model window a spawn of ``model`` counts against on ``layer``
    — the engine's ``usage_scope_key`` (Claude: the model family); "" for no
    model, no per-model windows, or an engine that is not registered."""
    if not model or not layer:
        return ""
    from core.session.session_manager import get_all_layers
    engine = get_all_layers().get(layer)
    return engine.usage_scope_key(model) if engine is not None else ""


def _window_exhausted_for(row: dict, model: str) -> bool:
    """Is the account out of window for a spawn of ``model`` — overall, or
    its per-model quota window (``""`` = overall only)? ``row`` is the
    subscription row (``id``, ``layer``, ``auth_type``); a non-OAuth row has
    no windows and is never exhausted."""
    reading = _window_readings([row]).get(row.get("id"))
    return reading is not None and _windows.exhausted(
        reading, _scope_key(row.get("layer"), model))


def _out_for(row: dict, model: str) -> bool:
    """Out of service for a spawn of ``model``: its windows exhausted for it,
    or resting for the model's scope after a usage limit on that family's
    own window (``rest_after_limit``)."""
    return (_window_exhausted_for(row, model)
            or _scope_resting(str(row.get("id") or ""), _scope_key(row.get("layer"), model)))


def _group_model(session_ids: list[str]) -> str:
    """The model the sessions of one credential scope run (the chat rows
    carry it; the binding context does not), so a scope is judged and
    re-homed against the right per-model window. The first row with a
    model wins — a scope's sessions share one agent, so one model."""
    from storage import database as task_store
    for sid in session_ids:
        try:
            row = task_store.get_chat_by_session(sid)
        except Exception:
            continue
        model = (row or {}).get("model") or ""
        if model:
            return model
    return ""


_FAR_FUTURE = float("inf")


def _epoch_or_inf(dt) -> float:
    return dt.timestamp() if dt is not None else _FAR_FUTURE


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


def _select(
    candidates: list[dict],
    *,
    allowed_auth: frozenset[str] | None,
    model: str = "",
) -> SubscriptionHandle | None:
    """Try candidates in priority order, claiming the first that yields usable
    credentials. ``candidates`` is already ordered (least-active first) by the
    store; here we additionally push hosted-relay subs last (a real
    BYO credential always wins over the relay's credit cost + latency hop).

    ``allowed_auth=None`` → no auth-type restriction (agent-scope / the owner's own
    accounts). When a restriction is given (a user borrowing the admin pool) it is
    enforced HERE, on the same list the usable-credential check reads, so an
    excluded auth type (notably ``oauth``) can never slip through a later branch.

    Ordering — drain first: a real BYO credential always beats the hosted
    relay (credit cost + latency); within that, the account whose WEEKLY
    window resets soonest comes first (quota left in a window when it closes
    is lost, so the account that closes first is used up first; the vendor's
    own reading, see ``subscription_windows``), accounts without a reading
    after every account with one (a subscription drains before a
    pay-per-token key), and equal reset instants fall back to the
    LEAST-CONSUMED key — recent burn (~5h) first, the 7-day total as tiebreak —
    so accounts that reset together still spread. The store's least-active
    order breaks remaining ties (stable sort). Subscriptions currently
    resting (a recent provider limit, account-wide or for this spawn's model
    scope: ``rest_after_limit``) or EXHAUSTED for this spawn's ``model`` (a
    window past its spill mark, the vendor's reached flag, or the model
    family's own weekly window) are skipped so the spawn fails over to an
    account with headroom.
    """
    auth_ok = [
        c for c in candidates
        if allowed_auth is None or c.get("auth_type") in allowed_auth
    ]
    # Read the samples for ONE candidate too: the sticky path selects from a
    # single-row list and the exhaustion check must still see it.
    readings = _window_readings(auth_ok)
    exhausted = {
        c["id"] for c in auth_ok
        if c["id"] in readings
        and _windows.exhausted(readings[c["id"]], _scope_key(c.get("layer"), model))
    }
    resting = {
        c["id"] for c in auth_ok
        if _is_throttled(c["id"])
        or _scope_resting(c["id"], _scope_key(c.get("layer"), model))
    }
    pool = [c for c in auth_ok if c["id"] not in resting and c["id"] not in exhausted]
    fallback = False
    if not pool:
        # Every eligible sub is briefly resting (recent provider limit/overload)
        # or has no window headroom. Both should DE-PRIORITISE, not eliminate:
        # rather than hard-block the user — fatal for a single-account install
        # over a transient 529 — fall back to the resting set, ordered by the
        # account that frees first. The provider's own retry/limit response
        # then governs (a transient overload usually clears on the immediate
        # retry; a real limit surfaces the real provider error instead of a
        # misleading "no subscription"). auth_ok already enforces allowed_auth,
        # so a borrowing user still never gets an OAuth sub here.
        pool = auth_ok
        fallback = True
        if exhausted:
            logger.warning(
                f"Pool: every candidate is out of window for {model or 'this spawn'} "
                f"({', '.join(sorted(s[:8] for s in exhausted))}) — using the one "
                f"that frees first; the provider may refuse until it resets"
            )
    if not pool:
        return None
    if len(pool) > 1:
        # Two-tier headroom key (see _CONSUMPTION_WINDOW_*): recent burn first
        # (~the provider's rolling window), the weekly-scale total as the
        # tiebreak. Two cheap SUMs per candidate — selection is rare (once per
        # CLI spawn/re-warm).
        since_short, since_long = _consumption_window_starts()
        consumption = {
            c["id"]: (
                subscription_store.get_subscription_consumption(c["id"], since_short),
                subscription_store.get_subscription_consumption(c["id"], since_long),
            )
            for c in pool
        }

        def _instant(s: dict) -> float:
            reading = readings.get(s["id"])
            if fallback:
                # When the account frees: the end of its rest and the reset
                # of the windows that exhaust it, whichever comes later.
                scope = _scope_key(s.get("layer"), model)
                known = [end for end in (_rest_end(s["id"], scope),) if end is not None]
                if reading is not None:
                    frees = _windows.frees_at(reading, scope)
                    if frees is not None:
                        known.append(frees.timestamp())
                return max(known) if known else _FAR_FUTURE
            if reading is None:
                return _FAR_FUTURE
            return _epoch_or_inf(_windows.quota_reset(reading))

        pool.sort(key=lambda s: (s.get("auth_type") == "relay",
                                 _instant(s),
                                 *consumption.get(s["id"], (0.0, 0.0))))
    else:
        pool.sort(key=lambda s: s.get("auth_type") == "relay")
    for chosen in pool:
        handle = _build_handle(chosen)
        # A relay sub carries no stored credential — its token is minted per
        # session/user at resolve time (resolve_subscription_env / change_model).
        if handle.api_key or handle.oauth_access_token or handle.endpoint_url or handle.auth_type == "relay":
            subscription_store.increment_active_sessions(chosen["id"])
            logger.info(
                f"Pool: acquired subscription {chosen['id'][:8]} for layer={chosen['layer']} "
                f"(provider={chosen['provider']}, auth={handle.auth_type}, "
                f"sessions={chosen['active_sessions'] + 1})"
            )
            return handle
        logger.warning(
            f"Pool: subscription {chosen['id'][:8]} has no usable credentials, trying next"
        )
    return None


def credential_scope_key(target: str, host_dir: str) -> str:
    """Identity of a credential-file-sharing domain. Sessions with the same key
    read/write the SAME ``.credentials.json`` / ``auth.json`` (the host config
    dir is per (agent, scope user, CLI flavor) — see
    ``ensure_persistent_agent_dir``), so they must all run on the same account:
    two accounts alternating over one file is the last-write-wins /
    silent-401-flip hazard. ``target`` keeps a satellite's mirrored dir from
    colliding with the local one in the key space; '' for no domain (e.g.
    direct-llm — env-injected keys, nothing shared on disk)."""
    if not host_dir:
        return ""
    return f"{target or placement.LOCAL}:{host_dir}"


def _sticky_subscription_id(scope_key: str) -> str | None:
    """The subscription the credential scope is already committed to, or None.

    Three sources, strongest first: a LIVE bound session's scope key (the
    in-memory maps), a just-acquired-not-yet-bound spawn (``_scope_recent`` —
    the acquire→bind race window), then the persisted bindings (post-restart:
    the maps are empty but a surviving session — remote satellite, re-adopted
    chat — may still hold the scope's file).

    Persisted rows are trusted only for LIVE sessions: a row whose session is
    in no registry is a ghost from an un-clean kill (proxy restart mid-turn,
    satellite death) and gets deleted on sight — before this check a single
    ghost pinned its scope to one account for the full startup-prune TTL (up
    to 7 days), which is how a busy agent's scope never re-consulted headroom
    (live-observed 2026-07-11). Within the post-boot grace window the verdict
    is skipped (registries still warming; behave like the pre-check code)."""
    with _session_maps_lock:
        for sid, sk in _session_scope_keys.items():
            if sk == scope_key:
                sub = _session_subscriptions.get(sid)
                if sub:
                    return sub
        recent = _scope_recent.get(scope_key)
        if recent:
            sub, ts = recent
            if time.time() - ts <= _SCOPE_RECENT_TTL_S:
                return sub
            _scope_recent.pop(scope_key, None)
    if time.monotonic() - _BOOT_MONOTONIC < _LIVENESS_BOOT_GRACE_S:
        try:
            sub = subscription_store.get_scope_binding(scope_key)
        except Exception:
            return None
        return sub if isinstance(sub, str) and sub else None
    try:
        rows = list(subscription_store.list_scope_bindings(scope_key) or [])
    except Exception:
        return None
    for row in rows:  # newest first
        sid = row.get("session_id") or ""
        if sid and _session_registered_live(sid) is False:
            _queue_write("dropping a ghost binding", subscription_store.delete_session_binding, sid)
            logger.info(
                f"Pool: dropped ghost binding of dead session {sid[:8]} "
                f"(scope no longer pinned by it)"
            )
            continue
        sub = row.get("subscription_id")
        if isinstance(sub, str) and sub:
            return sub
    return None


def _select_sticky(
    sticky_scope: str,
    candidates: list[dict],
    *,
    allowed_auth: frozenset[str] | None,
    model: str = "",
) -> SubscriptionHandle | None:
    """Reuse the scope's committed account IF it is still in this acquisition's
    candidate list (same eligibility the normal path enforces — a delisted or
    non-borrowable pin falls through to fresh selection; the rebind fan-out
    re-homes the scope's live sessions in that case). Throttling is
    deliberately NOT honored here: the shared-file constraint dominates a
    briefly resting account (``rebalance_scopes`` moves the whole scope).
    Window EXHAUSTION for the spawn's model is, and so is a rest for the
    model's scope after a usage limit on that family's window: a pin the
    vendor will refuse is no pin. When another candidate can serve the
    model, the spawn goes there and a rebalance pass is scheduled so the
    scope's other sessions follow onto the same account (the new spawn's
    credential file already points them there; the pass formalizes the
    bindings). Live-observed
    2026-09-11: sessions kept spawning onto an account at 96 % of its
    session window for a tick while the other account had headroom."""
    if not sticky_scope:
        return None
    pinned = _sticky_subscription_id(sticky_scope)
    if not pinned:
        return None
    match = [c for c in candidates if c["id"] == pinned]
    if not match:
        return None
    if _out_for(match[0], model):
        others = [
            c for c in candidates
            if c["id"] != pinned and (allowed_auth is None or c.get("auth_type") in allowed_auth)
            and not _out_for(c, model)
        ]
        if others:
            logger.info(
                f"Pool: scope-sticky account {pinned[:8]} is out of window for "
                f"{model or 'this spawn'} — spawning on the pool's pick and moving the scope"
            )
            schedule_rebalance("sticky account exhausted")
            return None
    handle = _select(match, allowed_auth=allowed_auth, model=model)
    if handle:
        logger.info(
            f"Pool: scope-sticky reuse of subscription {pinned[:8]} "
            f"(scope already holds its credential file)"
        )
    return handle


def _cap_status(layer: str, user_sub: str | None):
    """The pool cap this spawn is subject to (``services.billing.pool_caps``):
    the user's own pool for user scope, the platform pool for agent scope.
    None when the evaluation itself fails — a cap read must never take a
    spawn down with it."""
    try:
        if user_sub:
            return _pool_caps.evaluate("user", user_sub, layer)
        return _pool_caps.evaluate("platform", "", layer)
    except Exception:
        logger.warning("Pool: cap evaluation failed, spawn proceeds uncapped", exc_info=True)
        return None


def _rows_without_login(candidates: list[dict]) -> list[dict]:
    return [c for c in candidates if c.get("auth_type") != "oauth"]


def acquire_subscription(
    layer: str,
    user_sub: str | None,
    *,
    provider: str = "",
    sticky_scope: str = "",
    model: str = "",
    enforce_caps: bool = True,
) -> SubscriptionHandle | None:
    """Select and acquire a subscription for a new session. Off the loop
    (``asyncio.to_thread``): the queued seat releases are flushed first so
    the counts it reads are current.

    USER-SCOPE (``user_sub`` truthy): the user's own accounts (``use_personal``)
    first; then — only if Platform Auth is on — the admin pool restricted to
    BORROWABLE API credentials (api_key / relay / local_endpoint; never an admin
    OAuth subscription).  AGENT-SCOPE (``user_sub`` None/''): the full platform
    pool, OAuth subscriptions included.

    ``sticky_scope`` (CLI layers only — see ``credential_scope_key``): when the
    scope already has a live/just-acquired/persisted binding to account X and X
    is still an eligible candidate, X is reused instead of the headroom pick —
    the scope's sessions share ONE credential file, and re-selection happens at
    every spawn/re-warm, so without stickiness two same-scope sessions could
    fight over the file with different accounts.

    ``model`` (the spawn's model id, when known) lets the selection honour a
    per-model weekly window: an account whose "Fable" window is full is
    skipped for a Fable spawn and still serves a Sonnet one.

    ``enforce_caps``: the pool's subscription cap (``services.billing.pool_caps``)
    gates NEW spawns only. On a hit, ``stop`` raises ``NoSubscriptionError
    ("pool_cap")`` for BOTH scopes (an agent-scope spawn that acquired nothing
    would otherwise start on whatever credential file its scope dir still
    holds); ``continue`` drops the OAuth accounts from the candidates so an
    API key, the relay or a local endpoint takes the spawn, and raises the
    same when none is there. The re-homing of LIVE sessions (the delisting
    rebind and the scope rebalance) passes ``False``: a cap never moves or
    strands a running session.

    Returns None when nothing is available (caller surfaces the block; see
    ``user_scope_block_reason`` for the user-facing reason).
    """
    flush_binding_writes()
    user_sub = user_sub or None  # treat "" as agent-scope; never match owner_sub='' infra

    drop_oauth = False
    cap = _cap_status(layer, user_sub) if enforce_caps else None
    if cap is not None and not cap.allowed:
        scope_name = "user" if user_sub else "platform"
        if cap.on_reached != "continue":
            logger.info(
                f"Pool: {scope_name} pool cap reached on layer={layer} "
                f"({cap.hit_text()}) — spawn refused"
            )
            raise NoSubscriptionError("pool_cap", cap.blocked_message())
        logger.info(
            f"Pool: {scope_name} pool cap reached on layer={layer} "
            f"({cap.hit_text()}) — OAuth accounts excluded, continuing on a key"
        )
        drop_oauth = True

    handle: SubscriptionHandle | None = None
    if user_sub:
        # 1. The user's own usable accounts (any auth type, incl. their own OAuth)
        personal = subscription_store.list_personal(layer, user_sub, provider or None)
        if drop_oauth:
            personal = _rows_without_login(personal)
        handle = _select_sticky(sticky_scope, personal, allowed_auth=None, model=model) \
            or _select(personal, allowed_auth=None, model=model)
        if not handle:
            # 2. Platform fallback, gated by the per-user Platform Auth toggle
            if not subscription_store.get_user_allow_platform_auth(user_sub):
                logger.info(f"Pool: user {user_sub[:8]} has platform auth disabled, no subscription available")
            else:
                # 3. Borrow ONLY admin API-type credentials — never an admin OAuth sub
                platform = subscription_store.list_platform_pool(layer, provider or None)
                handle = _select_sticky(sticky_scope, platform,
                                        allowed_auth=_USER_BORROWABLE_AUTH_TYPES, model=model) \
                    or _select(platform, allowed_auth=_USER_BORROWABLE_AUTH_TYPES, model=model)
                if handle:
                    # Defense-in-depth: a user-scope handle must never be an OAuth subscription.
                    assert handle.auth_type in _USER_BORROWABLE_AUTH_TYPES, (
                        f"user-scope acquired non-borrowable auth_type={handle.auth_type}"
                    )
                else:
                    logger.warning(f"Pool: no borrowable platform credentials for user {user_sub[:8]}, layer={layer}")
    else:
        # AGENT-SCOPE: the full platform pool (OAuth subscriptions allowed)
        platform = subscription_store.list_platform_pool(layer, provider or None)
        if drop_oauth:
            platform = _rows_without_login(platform)
        handle = _select_sticky(sticky_scope, platform, allowed_auth=None, model=model) \
            or _select(platform, allowed_auth=None, model=model)

    if handle is None:
        if drop_oauth:
            # ``continue`` with nothing to continue on: say so, rather than
            # the generic "connect an account" classification.
            raise NoSubscriptionError("pool_cap", cap.blocked_message(no_key=True))
        return None

    if sticky_scope:
        # Stickiness can only bridge sessions whose candidate lists overlap. A
        # Shared-only agent's chats share ONE credential dir across ALL users
        # (sender-pays: each user's session runs on their own account), so two
        # users concurrently active on such an agent land different accounts on
        # the same file — the last-write-wins flap stickiness exists to prevent.
        # Surface it loudly; the full fix is per-payer credential delivery
        # for shared scopes. Under a cap's ``continue`` the switch to a key
        # is the point, so it is only noted.
        held = _sticky_subscription_id(sticky_scope)
        if held and held != handle.subscription_id:
            (logger.info if drop_oauth else logger.warning)(
                f"Pool: credential scope {sticky_scope!r} is live on subscription "
                f"{held[:8]} but this spawn selected {handle.subscription_id[:8]} "
                f"({'pool cap, continuing on a key' if drop_oauth else 'different payer'})"
                f" — concurrent sessions on this scope may contend over the "
                f"shared credential file"
            )
        # Commit the scope to this account for the acquire→bind window, so a
        # concurrent same-scope spawn can't pick a different one meanwhile.
        with _session_maps_lock:
            _scope_recent[sticky_scope] = (handle.subscription_id, time.time())
    return handle


def cap_continue_available(layer: str, user_sub: str | None, *, provider: str = "") -> bool:
    """Under a pool cap's ``continue``, is there a non-OAuth credential the
    spawn could take? User scope: an own key or endpoint, else a borrowable
    platform credential; agent scope: a non-OAuth row in the pool. No
    acquisition, no token mint (the friendly gates ask before recycling)."""
    if user_sub:
        own = subscription_store.list_personal(layer, user_sub, provider or None)
        if _rows_without_login(own):
            return True
        return borrowable_pool_available(layer, user_sub, provider=provider)
    return bool(_rows_without_login(subscription_store.list_platform_pool(layer, provider or None)))


def user_scope_block_reason(layer: str, user_sub: str, *, provider: str = "") -> str:
    """Classify why a user-scoped acquisition found no credentials, for the
    dashboard "no subscription" message. Cheap; called only on the terminal
    blocked path. Returns one of:
      'pool_cap'         — the user's own pool is at its subscription cap
                           (checked first: a capped user owns working accounts
                           and would otherwise read as auth_off / no_pool)
      'throttled'        — the user owns a sub for this layer but it's resting
                           (recent provider rate-limit/overload) — transient, retry
      'own_sub_expired'  — the user's own account(s) here are expired (dead
                           login grant) — reconnecting revives them in place
      'auth_off'         — Platform Auth disabled and the user has no own sub
      'admin_oauth_only' — pool exists but holds only OAuth subs (not borrowable)
      'no_pool'          — nothing in the platform pool for this layer at all
      'none'             — a borrowable sub exists but couldn't yield creds (glitch)

    Post-failure classification ONLY — never a pre-acquisition gate: a user
    whose own row expired but who can borrow an admin api_key row still gets a
    working session and never reaches this.
    """
    cap = _cap_status(layer, user_sub)
    if cap is not None and not cap.allowed:
        return "pool_cap"
    # The user DOES own a sub here, it's just resting — never tell them to "connect
    # an account". (With _select's throttled-fallback this rarely reaches a block,
    # but keep the classification honest for any caller.)
    own = subscription_store.list_personal(layer, user_sub, provider or None)
    if own and all(_is_throttled(s["id"]) for s in own):
        return "throttled"
    if not own:
        # No usable own account — but a dead one the user could revive beats
        # every "connect an account" message. Specifically `expired` rows
        # (grant death); an admin-DISABLED row must not say "reconnect".
        stale = subscription_store.list_personal(
            layer, user_sub, provider or None, any_status=True,
        )
        if any(s.get("status") == subscription_status.EXPIRED for s in stale):
            return "own_sub_expired"
    if not subscription_store.get_user_allow_platform_auth(user_sub):
        return "auth_off"
    pool = subscription_store.list_platform_pool(layer, provider or None)
    if not pool:
        return "no_pool"
    if any(s.get("auth_type") in _USER_BORROWABLE_AUTH_TYPES for s in pool):
        return "none"
    return "admin_oauth_only"


def borrowable_pool_available(layer: str, user_sub: str, *, provider: str = "") -> bool:
    """True if the user may borrow a platform API credential for this layer —
    Platform Auth on AND a borrowable admin sub exists (api_key/relay/local; never
    an admin OAuth subscription). No acquisition / no token mint."""
    if not subscription_store.get_user_allow_platform_auth(user_sub):
        return False
    pool = subscription_store.list_platform_pool(layer, provider or None)
    return any(s.get("auth_type") in _USER_BORROWABLE_AUTH_TYPES for s in pool)


def user_can_run(layer: str, user_sub: str, *, provider: str = "") -> bool:
    """True if a user-scoped request on ``layer`` would resolve to SOME credential:
    the user has an own usable account, OR a borrowable platform sub is available.
    Mirrors ``acquire_subscription``'s user-scope branch without acquiring/minting —
    the single predicate behind the SetupBanner and the per-layer availability flag."""
    if subscription_store.list_personal(layer, user_sub, provider or None):
        return True
    return borrowable_pool_available(layer, user_sub, provider=provider)


def layer_platform_configured(layer: str) -> bool:
    """True when the platform POOL has a subscription for ``layer`` — an
    active ``contribute_platform`` row owned by a current admin, or owner-less
    platform infra like the hosted relay (:func:`subscription_store.list_platform_pool`).

    This is the agent engine-enablement gate's predicate (operator decision
    2026-07-25, revised post-review): agent-scoped background work —
    scheduled tasks, phone conversations, triggers, anything with no driving
    user — can ONLY run on the pool, so an engine enabled on the strength of
    one user's personal connection would break the moment any non-user-scoped
    run touches it. Personal connections deliberately do NOT count here; they
    serve only that user's own chats, and per-user visibility is
    :func:`user_can_run`'s job at chat time. Still deliberately weaker than
    run-time credential resolution ("is enabling this sensible", not "will
    this specific run resolve a credential")."""
    return bool(subscription_store.list_platform_pool(layer))


def _auto_enable_candidates() -> list[str]:
    """The engines a fresh agent may be auto-enabled on, in priority order:
    the CODING engines (``identity.role``), in AI Engines page order
    (``identity.sort_order``). A supporting engine — Direct LLM — is never
    the auto-pick: it needs an explicit model + provider to be useful, and
    the agent-scope pool path can't borrow an admin OAuth sub the way the
    CLIs can; a creator can still enable it by hand in the agent's Config
    tab. The page order doubling as the priority is deliberate: an engine
    that should display second but never be auto-picked would need a field
    of its own, and no such engine exists."""
    from core.session.session_manager import get_all_layers
    coding = [
        (layer.capabilities.identity.sort_order, path)
        for path, layer in get_all_layers().items()
        if layer.capabilities.identity.role == "coding"
    ]
    return [path for _order, path in sorted(coding)]


def default_execution_layer_for_creator(user_sub: str) -> str:
    """Pick the execution layer (AI engine) to auto-enable for an agent that
    ``user_sub`` is creating or installing, so the agent runs zero-config.

    Returns the first coding engine (:func:`_auto_enable_candidates` — Claude
    Code, then Codex) that is connected on BOTH sides:

      - the PLATFORM — an admin has contributed an active subscription for it to
        the shared pool (:func:`subscription_store.list_platform_pool`), so
        agent-scope sessions (scheduled tasks, agent-scope chats) can run it; and
      - the CREATOR's own account — the creator holds an active personal
        subscription for it (:func:`subscription_store.list_personal`), so the
        creator's own user-scope chats run it too.

    Never returns a supporting engine. Falls back to the platform default
    engine when no candidate qualifies, so the new agent always has a
    sensible primary engine the creator can finish configuring.

    The agent's ``default_model`` stays empty (Auto → the engine's declared
    default) and ``default_effort`` stays empty (→ High), so the pair
    (engine, model, effort) is fully resolved with zero manual setup.

    Note on the BOTH rule: an admin who *contributes* their only subscription to
    the platform pool but leaves ``use_personal=False`` has no personal row, so
    that engine won't be auto-picked (we fall back to the default). That's an
    accepted edge — the pool engine still works for agent-scope runs, and the
    creator can enable it explicitly afterwards.
    """
    for layer in _auto_enable_candidates():
        platform_connected = bool(subscription_store.list_platform_pool(layer))
        creator_connected = bool(subscription_store.list_personal(layer, user_sub))
        if platform_connected and creator_connected:
            return layer
    return DEFAULT_EXECUTION_PATH


# --- the binding writer ------------------------------------------------------
# One worker carries every write of the persisted mirror and the seat
# counters, in order: a bind's upsert lands before its release's delete (the
# orphan row a deferred write once left behind cannot happen), a release's
# two statements leave the loop, and a reaper's burst is a queue, not a
# stall. A job is a pure store call: it never takes _session_maps_lock and
# never acquires. The spawn flows await a bind's future before they proceed,
# so the row is durable before the session runs; releases are fire-and-
# forget. After the shutdown drain every write runs inline (the pools close
# right after).
_binding_writer: ThreadPoolExecutor | None = None
_binding_writer_lock = threading.Lock()
_binding_writes_closed = False
# The shutdown drain's bound: a database stall must not hold the shutdown.
# What is still queued then is dropped; the next boot's counter reset
# (``reset_active_sessions``) and binding prune repair it.
_BINDING_DRAIN_TIMEOUT_S = 30.0


def _writer() -> ThreadPoolExecutor:
    global _binding_writer
    if _binding_writer is None:
        with _binding_writer_lock:
            if _binding_writer is None:
                _binding_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sub-bind")
    return _binding_writer


def _guarded(label: str, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.exception("Pool: %s failed (the in-memory state stands)", label)
        return None


def _queue_write(label: str, fn, *args) -> Future:
    """Queue one store write on the binding writer; its future resolves when
    the row landed (None when the write failed, logged)."""
    if _binding_writes_closed:
        done: Future = Future()
        done.set_result(_guarded(label, fn, *args))
        return done
    return _writer().submit(_guarded, label, fn, *args)


def flush_binding_writes(timeout: float | None = 30.0) -> None:
    """Wait for every queued write: a seat acquire before it reads the
    counts, a reconcile, the tests, the shutdown. Never on the loop thread
    (a running loop skips it: the counts may then read a write behind)."""
    if _binding_writer is None or _binding_writes_closed:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            _writer().submit(lambda: None).result(timeout)
        except TimeoutError:
            # A wedged writer (a database stall) must not fail the caller's
            # spawn: the counts may then read a write behind.
            logger.warning("Pool: binding writes not flushed within %ss; counts may read a write behind",
                           timeout)
    else:
        logger.debug("Pool: flush_binding_writes on the loop thread skipped")


def close_binding_writes(timeout: float | None = None) -> bool:
    """The shutdown, off the loop (the lifespan runs it in a worker thread):
    wait at most ``timeout`` (``_BINDING_DRAIN_TIMEOUT_S``, 30 s) for the
    queued writes, then every later write runs inline. Returns False when
    the writer did not drain in time (a database stall): it is then shut
    down without waiting, the writes still queued are dropped (the next
    boot's ``reset_active_sessions`` and binding prune repair what they
    would have written) and the one in progress finishes on its own thread."""
    global _binding_writes_closed
    limit = _BINDING_DRAIN_TIMEOUT_S if timeout is None else timeout
    writer = _binding_writer
    drained = True
    if writer is not None and not _binding_writes_closed:
        try:
            writer.submit(lambda: None).result(limit)
        except TimeoutError:
            drained = False
            logger.warning(
                "Pool: binding writes not drained within %ss at shutdown; "
                "the queued ones are dropped", limit,
            )
    _binding_writes_closed = True
    if writer is not None:
        writer.shutdown(wait=drained, cancel_futures=not drained)
    return drained


def _reopen_binding_writes() -> None:
    """Tests: a fresh writer after a close."""
    global _binding_writer, _binding_writes_closed
    _binding_writer = None
    _binding_writes_closed = False


def _release_rows(session_id: str, sub_id: str | None) -> None:
    # Each statement on its own: a failed delete must not keep the seat.
    _guarded("deleting a session binding", subscription_store.delete_session_binding, session_id)
    if sub_id:
        subscription_store.decrement_active_sessions(sub_id)


def release_subscription(session_id: str) -> None:
    """Release the subscription held by a session."""
    from services.engines import token_fanout
    token_fanout.unregister_session_target(session_id)
    with _session_maps_lock:
        _session_token_expiry.pop(session_id, None)
        _session_binding_ctx.pop(session_id, None)
        _session_scope_keys.pop(session_id, None)
        sub_id = _session_subscriptions.pop(session_id, None)
    # The persisted mirror and the seat: queued behind the bind's own write
    # (the startup TTL prune is the backstop for a write that fails).
    _queue_write("releasing a session binding", _release_rows, session_id, sub_id)
    if sub_id:
        logger.info(f"Pool: released subscription {sub_id[:8]} for session {session_id[:8]}")


def release_unbound_seat(subscription_id: str, sticky_scope: str = "") -> None:
    """Give back a seat ``resolve_subscription_env`` took that no session will
    ever bind — a spawn abandoned after its config was built (a skipped
    pre-warm, a slot denial, an offline target, a failed ``start_session``)
    or a task round that rode an already-warm session. ``release_subscription``
    cannot do it: it finds no binding and decrements nothing, which is how
    ``active_sessions`` drifted up one seat per such path (public issue #3's
    "stale counter"). The acquire-window claim the spawn left on its scope
    is dropped with the seat — nothing will bind from it, so it must not
    steer a concurrent same-scope spawn. Never touches bindings: the
    callers know whether a binding under their session id is a live
    session's (keep it) or a dead one's (``release_subscription``)."""
    if not subscription_id:
        return
    with _session_maps_lock:
        claim = _scope_recent.get(sticky_scope) if sticky_scope else None
        if claim and claim[0] == subscription_id:
            _scope_recent.pop(sticky_scope, None)
    _queue_write("releasing an unbound seat", subscription_store.decrement_active_sessions,
                 subscription_id)
    logger.info(
        f"Pool: released unbound seat on subscription {subscription_id[:8]} "
        f"(spawn abandoned before bind)"
    )


def session_bound(session_id: str) -> bool:
    """Whether the live map holds a binding for ``session_id``."""
    with _session_maps_lock:
        return session_id in _session_subscriptions


def live_session_count(sub_id: str) -> int:
    """Seats ``sub_id`` really holds right now: sessions bound to it in the
    live map plus fresh acquire-window claims (a spawn between its acquire
    and its bind). The honest counterpart of the store's ``active_sessions``
    counter, which only moves by increments and decrements and reads stale
    after any unbalanced path. Within the post-boot grace window the
    persisted bindings count too: a satellite session that has not
    re-announced yet is invisible to the maps but alive. Known slack: the
    claims exist only for CLI sticky spawns and one claim covers a scope,
    so a direct-llm, phone or meeting spawn is invisible between its
    acquire and its bind (seconds) — a delete in that window under-counts
    by one and the seat's release later floors at zero."""
    now = time.time()
    with _session_maps_lock:
        live_ids = {sid for sid, s in _session_subscriptions.items() if s == sub_id}
        claims = sum(
            1 for sub, ts in _scope_recent.values()
            if sub == sub_id and now - ts <= _SCOPE_RECENT_TTL_S
        )
    if within_boot_grace():
        try:
            live_ids |= set(subscription_store.list_binding_session_ids(sub_id))
        except Exception:
            logger.debug("live_session_count: persisted bindings unreadable", exc_info=True)
    return len(live_ids) + claims


def reconcile_active_sessions(sub_id: str) -> tuple[int, int]:
    """Lower the store's ``active_sessions`` for ``sub_id`` to
    :func:`live_session_count` when it reads HIGHER; never raise it (a seat
    the live map does not see — a satellite session not yet re-announced
    after a restart — is re-taken by ``restore_session_binding`` itself).
    Returns ``(stored, live)``. Used where a stale counter would otherwise
    refuse an admin action (the subscription delete)."""
    flush_binding_writes()   # the queued releases count before the stored value is read
    row = subscription_store.get_subscription(sub_id) or {}
    stored = int(row.get("active_sessions") or 0)
    live = live_session_count(sub_id)
    if stored > live and subscription_store.lower_active_sessions(sub_id, live):
        logger.info(
            f"Pool: reconciled active_sessions of subscription {sub_id[:8]} "
            f"{stored} → {live} (no live session backed the difference)"
        )
    return stored, live


def bind_session(
    session_id: str,
    subscription_id: str,
    *,
    layer: str = "",
    user_sub: str | None = None,
    scope_key: str = "",
) -> Future:
    """Track which subscription a session is using (for cleanup + fan-out).

    ``layer`` + ``user_sub`` record the ACQUISITION context — the arguments
    this binding's ``acquire_subscription`` call resolved with (``""`` =
    agent-scope) — so a later selection change can re-evaluate the session
    against the same candidate lists (``rebind_delisted_sessions``). The
    layers pass ``user_sub`` from ``AgentConfig.subscription_user_sub``;
    ``None`` means the spawn path didn't stamp it and the session is excluded
    from selection-change rebinds (fail-soft — never guess the scope: a
    user-scope session misjudged as agent-scope could be re-homed onto an
    admin OAuth subscription, which user scope must never borrow).

    Also snapshots the expiry of the OAuth access token this spawn wrote into
    the session's credential file: ``_issued_token_expiry`` was stamped when
    THIS spawn's credentials were resolved, and bind follows resolve within the
    same spawn flow. The snapshot deliberately captures the token actually
    issued — on a fail-soft refresh that is an aging stored token, not a
    full-runway one (exactly how the 2026-07-06 mid-turn 401s hid from every
    full-runway assumption). Rotation fan-out advances the snapshot per session
    once the session's file holds the new token.
    """
    exp = _issued_token_expiry.get(subscription_id)
    with _session_maps_lock:
        prior_sub = _session_subscriptions.get(session_id)
        _session_subscriptions[session_id] = subscription_id
        if layer and user_sub is not None:
            _session_binding_ctx[session_id] = (layer, user_sub)
        else:
            _session_binding_ctx.pop(session_id, None)
        if scope_key:
            _session_scope_keys[session_id] = scope_key
            # The live binding supersedes the acquire-window claim.
            _scope_recent.pop(scope_key, None)
        else:
            _session_scope_keys.pop(session_id, None)
        if exp:
            _session_token_expiry[session_id] = exp
        else:
            _session_token_expiry.pop(session_id, None)
    # Persisted mirror: survives restarts so usage attribution
    # (get_session_subscription read-through) and the scope-sticky lookup keep
    # working for sessions that outlive the proxy process. Queued on the
    # binding writer, which keeps it ahead of this session's release (the
    # orphan a deferred write once left behind cannot happen); the spawn
    # flows await the returned future so the row is durable before the
    # session runs. Best-effort: the in-memory binding stands either way.
    # Re-bind of a live session (start_session reusing a warm process): the
    # spawn flow acquired a FRESH seat for this round, so the replaced
    # binding's seat must be released — same-sub included (two increments,
    # one binding, one eventual release = +1 leak per warm continue round).
    # Historically masked because a close always released the old binding
    # before the next round's bind; a gracefully-aborted lane's session now
    # stays warm across rounds. Only the layer spawn flows re-bind live
    # sessions: restore_session_binding no-ops on a live binding and the
    # selection rebind moves seats itself without bind_session.
    def _persist() -> None:
        # Each statement on its own: a failed upsert must not keep the
        # replaced seat.
        _guarded("persisting a session binding", subscription_store.upsert_session_binding,
                 session_id, subscription_id,
                 layer=layer, user_sub=user_sub, scope_key=scope_key)
        if prior_sub:
            subscription_store.decrement_active_sessions(prior_sub)

    if prior_sub:
        logger.info(
            f"Pool: re-bound session {session_id[:8]} "
            f"{prior_sub[:8]} → {subscription_id[:8]} (replaced seat released)"
        )
    return _queue_write("persisting a session binding", _persist)


def get_session_subscription(session_id: str) -> str | None:
    """Get the subscription ID bound to a session.

    Read-through: the in-memory map first (every live spawn binds there), then
    the persisted bindings — a session that outlived a proxy restart (remote
    satellite, re-adopted chat) still attributes its usage to the right
    account instead of leaking to ``source_key='default'``. The store hit is
    NOT cached back: repopulating the map would make ``release_subscription``
    decrement a seat the post-restart counter reset never counted."""
    sub = _session_subscriptions.get(session_id)
    if sub:
        return sub
    try:
        row = subscription_store.get_session_binding(session_id)
    except Exception:
        return None
    if isinstance(row, dict):
        return row.get("subscription_id") or None
    return None


def session_cost_billed(session_id: str) -> bool:
    """Whether the credential serving this session costs real money — an API
    key or the hosted relay — as opposed to a subscription's or a local
    model's activity estimate (``_ACTIVITY_AUTH_TYPES``). Drives the
    ``cost_billed`` flag on the per-turn metadata event, i.e. whether the chat
    SHOWS the cost; recording is unaffected. Same read-through as
    ``get_session_subscription``. A session with no pool binding (pool-external
    credentials) or an unreadable subscription row answers True: the kind is
    unknowable there and showing is today's behaviour."""
    sub_id = get_session_subscription(session_id)
    if not sub_id:
        return True
    try:
        row = subscription_store.get_subscription(sub_id)
    except Exception:
        logger.debug("cost_billed: subscription lookup failed", exc_info=True)
        return True
    if not isinstance(row, dict):
        return True
    return row.get("auth_type") not in _ACTIVITY_AUTH_TYPES


def restore_session_binding(session_id: str) -> str | None:
    """Re-establish the IN-MEMORY binding for a session that survived a proxy
    restart (Mode C re-adopt). The persisted row keeps attribution working
    read-through, but rotation fan-out and the freshness worker enumerate the
    in-memory maps only — without this, a re-adopted session misses every
    future token rotation. Also re-takes the seat the boot-time
    ``reset_active_sessions()`` zeroed, so a later release's decrement stays
    balanced (decrement floors at 0 either way).

    Idempotent: an already-live binding is returned untouched. Returns the
    subscription id, or None when no persisted binding exists (api-key
    sessions, pool-external credentials)."""
    with _session_maps_lock:
        live = _session_subscriptions.get(session_id)
    if live:
        return live
    try:
        row = subscription_store.get_session_binding(session_id)
    except Exception:
        logger.exception("Pool: reading persisted binding failed on restore")
        return None
    if not isinstance(row, dict) or not row.get("subscription_id"):
        return None
    sub_id = row["subscription_id"]
    bind_session(
        session_id, sub_id,
        layer=row.get("layer") or "",
        user_sub=row.get("user_sub"),
        scope_key=row.get("scope_key") or "",
    )
    _queue_write("re-taking a seat on restore", subscription_store.increment_active_sessions, sub_id)
    logger.info(
        "Pool: restored binding for re-adopted session %s -> %s",
        session_id[:8], sub_id[:8],
    )
    return sub_id


def get_session_payer_sub(session_id: str) -> str:
    """The ``user_sub`` whose subscription this session acquired.

    ``""`` means platform/agent-pool paid (agent-scope acquisition) or
    unknown (no binding — e.g. credentials outside the pool). Same
    read-through as :func:`get_session_subscription`: the in-memory
    acquisition context first, then the persisted binding so usage
    attribution survives a proxy restart. Usage recording uses this to
    bill a Shared-only agent's human chats to the interacting user
    (whose subscription actually served the turn) instead of the agent
    bucket.
    """
    with _session_maps_lock:
        ctx = _session_binding_ctx.get(session_id)
    if ctx is not None:
        return ctx[1] or ""
    try:
        row = subscription_store.get_session_binding(session_id)
    except Exception:
        return ""
    if isinstance(row, dict):
        return row.get("user_sub") or ""
    return ""


def session_token_expiry_ms(session_id: str) -> int | None:
    """``expiresAt`` (epoch ms) of the OAuth access token in this session's
    credential file (spawn snapshot, advanced by fan-out) — None for sessions
    with no expiring credential (api_key / local_endpoint / relay) and for
    sessions spawned before the proxy last restarted (the map is in-memory;
    callers must treat None as "unknown", not "immortal")."""
    return _session_token_expiry.get(session_id)


def bound_oauth_subscription_ids() -> set[str]:
    """Subscription ids with at least one live bound session AND an expiring
    token snapshot — the freshness worker's work list. Keying on the expiry
    snapshots (only stamped for expiring OAuth credentials) skips api_key /
    local / relay sessions without a store read per tick."""
    with _session_maps_lock:
        return {
            _session_subscriptions[sid]
            for sid in list(_session_token_expiry)
            if sid in _session_subscriptions
        }


# ---------------------------------------------------------------------------
# Selection-change rebinding — live sessions follow the account checkboxes
# ---------------------------------------------------------------------------

# Serialize rebind passes (endpoint hook vs freshness tick): concurrent passes
# would double-acquire replacements for the same groups. Passes are quick, so
# the second caller just waits its turn.
_rebind_lock = threading.Lock()
# Keep fire-and-forget rebind tasks referenced until done (an unreferenced
# asyncio.Task can be garbage-collected mid-flight).
_rebind_tasks: set[asyncio.Task] = set()


def schedule_rebind(reason: str) -> None:
    """Fire-and-forget a ``rebind_delisted_sessions`` pass from an async
    context — called by the API endpoints right after a selection mutation
    (scope checkboxes, subscription add/delete, Platform-Auth toggle, role
    change, OAuth connect) so live sessions follow the change within moments
    instead of waiting for the next freshness tick. Never blocks the caller
    (a replacement acquisition can involve a network token refresh); the
    worker's per-tick pass is the retry loop, so a lost task only delays
    convergence by ≤5 min."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # sync caller (tests / scripts) — the worker pass converges
    task = loop.create_task(
        asyncio.to_thread(rebind_delisted_sessions, reason=reason)
    )
    _rebind_tasks.add(task)
    task.add_done_callback(_rebind_tasks.discard)


def schedule_rebalance(reason: str) -> None:
    """Fire-and-forget a ``rebalance_scopes`` pass — called by
    ``mark_subscription_throttled`` the moment a REAL provider limit lands, so
    every scope pinned to the limited account fails over within moments
    instead of erroring until the next freshness tick. Unlike
    ``schedule_rebind`` this must also work from WORKER THREADS: the
    interactive tailers (the main limit reporters) run via ``to_thread``, so
    with no running loop it hops to the loop token_fanout captured at startup.
    The worker's per-tick pass is the retry loop, so a lost task (no loop at
    all: tests/scripts) only delays convergence by ≤5 min."""
    def _spawn(loop: asyncio.AbstractEventLoop) -> None:
        task = loop.create_task(
            asyncio.to_thread(rebalance_scopes, reason=reason)
        )
        _rebind_tasks.add(task)
        task.add_done_callback(_rebind_tasks.discard)

    try:
        _spawn(asyncio.get_running_loop())
    except RuntimeError:
        from services.engines import token_fanout
        loop = token_fanout._loop
        if loop is None or loop.is_closed():
            return  # tests / scripts — the worker pass converges
        loop.call_soon_threadsafe(_spawn, loop)


def _selection_contains(sub_id: str, layer: str, scope_sub: str) -> bool:
    """True while ``sub_id`` is still part of the CURRENT selection for an
    acquisition context — membership in the same candidate lists
    ``acquire_subscription`` reads (``scope_sub=""`` = agent-scope). No
    provider filter: it only narrows the candidate set and a bound row always
    matches its own provider, so membership is filter-invariant. Throttling
    is ignored — a resting account is de-prioritised for NEW acquisitions,
    not delisted."""
    if scope_sub:
        if any(s["id"] == sub_id for s in subscription_store.list_personal(layer, scope_sub)):
            return True
        if not subscription_store.get_user_allow_platform_auth(scope_sub):
            return False
        return any(
            s["id"] == sub_id and s.get("auth_type") in _USER_BORROWABLE_AUTH_TYPES
            for s in subscription_store.list_platform_pool(layer)
        )
    return any(s["id"] == sub_id for s in subscription_store.list_platform_pool(layer))


def rebind_delisted_sessions(*, reason: str = "") -> int:
    """Re-home live sessions bound to a subscription that is no longer part of
    their scope's selection — the owner unticked ``use_personal``, an admin
    unticked ``contribute_platform`` or disabled the row, the row was deleted
    or auto-expired, or the user's Platform Auth was revoked. Without this,
    bindings made at spawn outlive the selection: the freshness worker keeps
    the OLD account's token fresh forever and long-lived sessions (interactive
    terminals survive their window closing) never see the newly selected
    account until a proxy restart.

    For each affected session with a registered credential FILE (the
    hot-swappable set: Claude ``.credentials.json`` / Codex ``auth.json``,
    local or satellite) a currently-eligible replacement is acquired via the
    normal ``acquire_subscription`` path and the file is rewritten in place
    over the rotation fan-out rails — the live CLI re-reads it, no respawn.
    The binding + expiry-snapshot swap is ack-gated per session
    (``on_written``): a failed write keeps the old binding so the next pass
    retries. ``active_sessions`` counters move per landed session.

    Fail-soft everywhere: with no eligible replacement — or a replacement
    whose credential can't reach a live process (env-injected API key /
    endpoint) — the session keeps its current credentials (they aren't
    revoked by deselection) and follows the new selection at its next spawn.
    Runs after every selection mutation (``schedule_rebind``) and at the top
    of every freshness tick (the retry/convergence loop). Returns the number
    of sessions whose rebind landed synchronously (satellite acks land
    later). Never raises (background primitive — the worker retries anyway).
    Sync — call via ``asyncio.to_thread``.
    """
    try:
        return _rebind_delisted_sessions(reason=reason)
    except Exception:
        logger.exception("Pool: selection rebind pass failed")
        return 0


def _move_seat(old_sub: str, new_sub: str, session_id: str = "") -> None:
    """Move one ``active_sessions`` seat between subscriptions (and swap the
    session's persisted binding row when ``session_id`` is given). The rebind's
    ``on_written`` runs on the EVENT LOOP for satellite acks (like the rotation
    fan-out's) and on a pool thread for local ones; either way the store
    writes are queued on the binding writer (``_queue_write``), in order with
    the session's bind and release, and never run on the caller's thread."""
    def _apply() -> None:
        subscription_store.increment_active_sessions(new_sub)
        subscription_store.decrement_active_sessions(old_sub)
        if session_id:
            try:
                subscription_store.update_session_binding_sub(session_id, new_sub)
            except Exception as e:
                # persisted mirror only; memory is authoritative live
                logger.debug("session binding rebind: %s", e)
    # The writer keeps it in order with the session's bind and release.
    _queue_write("moving a seat", _apply)


def _move_scope_group(
    old_sub: str,
    layer: str,
    scope_sub: str,
    sids: list[str],
    *,
    cause: str,
    reason: str,
    replacements: dict[tuple[str, str, str, str], SubscriptionHandle | None],
    moved: list[str],
    stuck_log,
    require_unthrottled: bool = False,
) -> None:
    """Move one (account, acquisition-context) group of live sessions onto a
    freshly acquired replacement over the rotation fan-out rails — the shared
    machinery behind the delisting rebind AND the scope rebalance. ``cause``
    names why for the logs ('delisted' / 'rate-limited' / 'headroom-drifted…').
    ``replacements`` caches one replacement per (layer, scope_sub, provider)
    for the caller's whole pass, so every group of one context lands on the
    SAME account (scope-shared credential files must not flap across sessions).
    ``require_unthrottled`` (rebalance): a replacement that is itself resting
    is discarded — failing over onto another limited account is pure churn
    (the delisting caller keeps ``_select``'s throttled-fallback semantics:
    deselection revokes eligibility, so SOME account must be chosen).
    Fail-soft everywhere; binding/seat swaps are ack-gated per session."""
    from services.engines import token_fanout

    swappable = [s for s in sids if token_fanout.session_target(s)]
    if not swappable:
        stuck_log(
            f"Pool: subscription {old_sub[:8]} {cause} but its {len(sids)} bound "
            f"session(s) carry env-injected credentials — they follow the new "
            f"selection at their next spawn"
        )
        return
    old_row = subscription_store.get_subscription(old_sub)
    # The sessions' model: a replacement must be able to serve it — moving
    # Fable sessions onto an account whose Fable window is full trades one
    # refusal for another (live-observed 2026-09-11).
    model = _group_model(sids)
    rkey = (layer, scope_sub, (old_row or {}).get("provider") or "", model)
    if rkey not in replacements:
        # Live sessions are re-homed, never capped: a cap gates new spawns only.
        handle = acquire_subscription(layer, scope_sub or None, provider=rkey[2],
                                      model=model, enforce_caps=False)
        if handle is not None:
            # Cancel acquire's built-in +1 either way: counters move per
            # session below, only for writes that actually land.
            subscription_store.decrement_active_sessions(handle.subscription_id)
            if handle.subscription_id == old_sub:
                # Delisting: shouldn't happen (acquire and the membership
                # check read the same store). Rebalance: the current account
                # IS the pool's best remaining pick (sole candidate, or
                # everything else rests) — leave the sessions alone rather
                # than "swap" onto themselves.
                handle = None
            elif require_unthrottled and (
                _is_throttled(handle.subscription_id)
                or _out_for(
                    {"id": handle.subscription_id, "layer": handle.layer,
                     "auth_type": handle.auth_type},
                    model,
                )
            ):
                # Hopping onto another limited or exhausted account is pure
                # churn: credential-file rewrites on live sessions every
                # cooldown, for nothing.
                handle = None
        replacements[rkey] = handle
    handle = replacements[rkey]
    if handle is None:
        stuck_log(
            f"Pool: subscription {old_sub[:8]} {cause} with no eligible "
            f"replacement for {len(sids)} bound session(s) (layer={layer}, "
            f"scope={'user' if scope_sub else 'agent'}"
            + (f", model={model}" if model else "") + ") — they keep their "
            f"current credentials until one is connected"
        )
        return

    # The replacement's credential FILE, from the token this handle actually
    # issued (the pair the snapshot below records): None for a key or an
    # endpoint (env-frozen, undeliverable into a live file session).
    payload = None
    if handle.oauth_access_token:
        from core.session.session_manager import get_layer_by_path
        payload = get_layer_by_path(layer).credential_file_payload(
            handle.oauth_access_token, handle.oauth_expires_at_ms, handle.credential,
        )
    deliverable = list(swappable) if payload is not None else []
    if not deliverable:
        stuck_log(
            f"Pool: replacement {handle.subscription_id[:8]} for {cause} "
            f"{old_sub[:8]} has no file-deliverable credential for "
            f"{len(swappable)} live session(s) — they keep their current "
            f"credentials until their next spawn"
        )
        return

    new_sub = handle.subscription_id
    new_expiry = handle.oauth_expires_at_ms

    def _on_written(
        sid: str, *, _old=old_sub, _new=new_sub, _exp=new_expiry,
    ) -> None:
        # Fires per session once its file write landed (sync for local
        # dirs, satellite-ack for remote) — swap the binding only then,
        # and only if the session wasn't released or re-homed meanwhile.
        with _session_maps_lock:
            if _session_subscriptions.get(sid) != _old:
                return
            _session_subscriptions[sid] = _new
            if _exp:
                _session_token_expiry[sid] = _exp
            else:
                _session_token_expiry.pop(sid, None)
        _move_seat(_old, _new, sid)
        moved.append(sid)
        logger.info(
            f"Pool: re-homed session {sid[:8]} onto subscription {_new[:8]} "
            f"(was {_old[:8]}, {cause}"
            + (f"; {reason}" if reason else "") + ")"
        )

    token_fanout.fan_out(
        deliverable, layer=layer, payload=payload,
        on_written=_on_written, expected_sub_id=old_sub,
    )


def _rebind_delisted_sessions(*, reason: str) -> int:
    with _rebind_lock:
        with _session_maps_lock:
            bindings = dict(_session_subscriptions)
            ctxs = dict(_session_binding_ctx)

        # Group by (old sub, acquisition context) so each delisted account is
        # evaluated once and every session of one context lands on the SAME
        # replacement (scope-shared credential files must not flap between
        # accounts across sessions).
        groups: dict[tuple[str, str, str], list[str]] = {}
        for sid, old_sub in bindings.items():
            ctx = ctxs.get(sid)
            if ctx is None:
                continue  # unstamped binding — excluded (fail-soft)
            layer, scope_sub = ctx
            groups.setdefault((old_sub, layer, scope_sub), []).append(sid)

        moved: list[str] = []
        replacements: dict[tuple[str, str, str], SubscriptionHandle | None] = {}
        # A user action (reason set) logs stuck sessions loudly once; the
        # 5-minute worker pass retries the same situation quietly.
        stuck_log = logger.info if reason else logger.debug
        for (old_sub, layer, scope_sub), sids in groups.items():
            if _selection_contains(old_sub, layer, scope_sub):
                continue
            _move_scope_group(
                old_sub, layer, scope_sub, sids,
                cause="delisted", reason=reason,
                replacements=replacements, moved=moved, stuck_log=stuck_log,
            )
        return len(moved)


# ---------------------------------------------------------------------------
# Scope rebalancing — whole-scope failover + headroom drift correction
# ---------------------------------------------------------------------------

def _eligible_candidates(layer: str, scope_sub: str, provider: str = "") -> list[dict]:
    """The candidate rows an acquisition with this context would consider —
    the exact lists ``acquire_subscription`` reads (user scope: own accounts,
    then the borrowable platform pool behind the Platform-Auth toggle; agent
    scope: the full platform pool). Used by the drift trigger to ask "is there
    somewhere meaningfully colder to move to?" before committing a move."""
    if scope_sub:
        cands = list(subscription_store.list_personal(layer, scope_sub, provider or None))
        if subscription_store.get_user_allow_platform_auth(scope_sub):
            cands += [
                c for c in subscription_store.list_platform_pool(layer, provider or None)
                if c.get("auth_type") in _USER_BORROWABLE_AUTH_TYPES
            ]
        return cands
    return subscription_store.list_platform_pool(layer, provider or None)


def rebalance_scopes(*, reason: str = "") -> int:
    """Re-home entire credential scopes whose pinned account should no longer
    carry them — the counterpart to ``rebind_delisted_sessions`` for accounts
    that are still SELECTED but shouldn't keep serving a scope:

      - REACTIVE: the account is resting on a real provider rate/usage limit
        (``_throttled_hard``, or a rest for the scope's model after a limit
        on that family's window), or the vendor's own window reading says it
        is exhausted (``subscription_windows``); without this, scope-stickiness
        deliberately keeps reusing the limited account (the shared-file
        constraint beats a resting account for NEW spawns) and every session
        in the scope errors until the provider window resets.
      - PROACTIVE (drift): the account's recent burn is far above the coldest
        eligible candidate (``DRIFT_ABS_FLOOR_USD`` + ``DRIFT_RATIO`` on the
        5h window) — the always-busy-agent case where the pin otherwise never
        re-consults headroom.

    Scopes move WHOLE (all live sessions of a scope share one credential
    file — the fan-out writes it once, so a scope can never split across
    accounts) and rarely (per-scope cooldown, stamped on attempt). Sessions
    without a scope key (direct-llm: env-frozen credentials) or without a
    stamped acquisition context never move. Serialized with the delisting
    rebind on ``_rebind_lock``. Returns sessions whose swap landed
    synchronously (satellite acks land later). Never raises. Sync — call via
    ``asyncio.to_thread``.
    """
    try:
        return _rebalance_scopes(reason=reason)
    except Exception:
        logger.exception("Pool: scope rebalance pass failed")
        return 0


def _rebalance_scopes(*, reason: str) -> int:
    with _rebind_lock:
        with _session_maps_lock:
            bindings = dict(_session_subscriptions)
            ctxs = dict(_session_binding_ctx)
            scope_keys = dict(_session_scope_keys)

        # Group by (account, acquisition context, credential scope): triggers
        # and cooldowns are per SCOPE, and a move must carry exactly the
        # sessions sharing that scope's credential file.
        groups: dict[tuple[str, str, str, str], list[str]] = {}
        for sid, old_sub in bindings.items():
            ctx = ctxs.get(sid)
            scope_key = scope_keys.get(sid) or ""
            if ctx is None or not scope_key:
                continue  # unstamped or file-less — excluded (fail-soft)
            layer, scope_sub = ctx
            groups.setdefault((old_sub, layer, scope_sub, scope_key), []).append(sid)

        moved: list[str] = []
        replacements: dict[tuple[str, str, str, str], SubscriptionHandle | None] = {}
        stuck_log = logger.info if reason else logger.debug
        now_mono = time.monotonic()
        since_short, _ = _consumption_window_starts()
        for (old_sub, layer, scope_sub, scope_key), sids in groups.items():
            last = _scope_rebalance_last.get(scope_key)
            if last is not None and now_mono - last < _SCOPE_REBALANCE_COOLDOWN_S:
                continue
            with _session_maps_lock:
                recent = _scope_recent.get(scope_key)
            if recent and recent[0] == old_sub and time.time() - recent[1] <= _SCOPE_RECENT_TTL_S:
                # A spawn just claimed this scope on THIS account
                # (acquire→bind window): moving the scope NOW would race the
                # spawn's own credential-file write. Skip WITHOUT stamping the
                # cooldown — the next tick retries once the spawn has bound. A
                # claim on ANOTHER account is a sticky pin that yielded to
                # exhaustion (``_select_sticky``): the rest of the scope must
                # follow it, and its file already carries that account.
                continue
            # The scope's model decides which windows count: a Fable scope on
            # an account whose Fable window is full moves even while the
            # overall windows have room.
            model = _group_model(sids)
            if old_sub in _throttled_hard and _is_throttled(old_sub):
                cause = "rate-limited"
            elif _scope_resting(old_sub, _scope_key(layer, model)):
                cause = f"rate-limited for {model}"
            elif _window_exhausted_for(
                {"id": old_sub, "layer": layer, "auth_type": "oauth"}, model,
            ):
                # The vendor's own reading says the account is out of window
                # for what these sessions run.
                cause = "window exhausted" + (f" for {model}" if model else "")
            else:
                burn = subscription_store.get_subscription_consumption(
                    old_sub, since_short)
                if burn < DRIFT_ABS_FLOOR_USD:
                    continue
                provider = (subscription_store.get_subscription(old_sub)
                            or {}).get("provider") or ""
                candidates = [
                    c for c in _eligible_candidates(layer, scope_sub, provider)
                    if c["id"] != old_sub and not _is_throttled(c["id"])
                    and not _out_for(c, model)
                ]
                if not candidates:
                    continue
                min_burn = min(
                    subscription_store.get_subscription_consumption(
                        c["id"], since_short)
                    for c in candidates
                )
                if burn <= DRIFT_RATIO * min_burn:
                    continue
                cause = f"headroom-drifted (${burn:.0f} vs ${min_burn:.0f} in 5h)"
            # Stamp on ATTEMPT: a move whose fan-out can't land (satellite
            # offline) must not retry at full tick rate.
            _scope_rebalance_last[scope_key] = now_mono
            logger.info(
                f"Pool: rebalancing scope of {len(sids)} session(s) off "
                f"subscription {old_sub[:8]} ({cause})"
            )
            _move_scope_group(
                old_sub, layer, scope_sub, sids,
                cause=cause, reason=reason,
                replacements=replacements, moved=moved, stuck_log=stuck_log,
                require_unthrottled=True,
            )
        return len(moved)


# ---------------------------------------------------------------------------
# High-level helper: resolve provider + acquire + build env vars
# ---------------------------------------------------------------------------

def relay_llm_credentials(layer: str, provider: str, user_sub: str | None) -> tuple[str, str] | None:
    """For a hosted (``auth_type='relay'``) subscription on ``layer``: mint a
    per-user relay token and build this provider's relay endpoint URL — the
    engine's ``providers[]`` entry declares the path (the provider SDK
    appends its own route suffix to base_url, so the install-side endpoint
    differs per vendor). Returns ``(api_key, endpoint_url)`` where
    ``api_key`` is the minted token, or ``None`` if the provider has no relay
    path or the relay is unavailable / refuses (out of credit, over seat,
    not configured) — the caller then surfaces a clean "no credentials"
    error."""
    import config as app_config
    from core.execution_layer import provider_entry
    from core.session.session_manager import get_layer_capabilities
    from services.billing import relay_client

    caps = get_layer_capabilities(layer)
    entry = provider_entry(caps, provider) if caps is not None else None
    path = (entry or {}).get("relay_path") or ""
    if not path or not app_config.OTODOCK_RELAY_BASE:
        return None
    try:
        token = relay_client.mint_session_token(user_sub or "")
    except relay_client.RelayNotConfigured as e:
        logger.warning(f"Hosted LLM unavailable (provider={provider}): {e}")
        return None
    base = app_config.OTODOCK_RELAY_BASE.rstrip("/")
    return token, f"{base}{path}"


def resolve_subscription_env(
    execution_path: str,
    user_sub: str | None,
    model: str = "",
    agent_info: dict | None = None,
    sticky_scope: str = "",
) -> tuple[str, dict[str, str]]:
    """Acquire a subscription and build the session's auth env.

    Provider resolution and acquisition are the pool's; the credential-to-env
    mapping is the ENGINE's (``ExecutionLayer.subscription_env`` — the layer
    is looked up by ``execution_path``, fail-closed on an id no engine
    claims). One call for every config builder (chat, task, meeting, phone).

    ``sticky_scope`` (the spawn's ``credential_scope_key``; the builders pass
    it) pins same-scope sessions to one account — see ``acquire_subscription``
    — and only means anything on an engine whose login is a credential FILE
    shared by the scope's sessions (``auth.credential_file``); an engine that
    injects its credential per session env has nothing on disk to share.

    Returns (subscription_id, env_vars_dict).  On failure returns ("", {}).
    """
    import config as app_config  # local import to avoid circular dependency
    from core.session.session_manager import get_layer_by_path

    layer = get_layer_by_path(execution_path)
    caps = layer.capabilities

    # 1. Resolve the provider from the model on a multi-provider engine (one
    # that declares MORE THAN ONE provider: Codex and Direct LLM carry
    # OpenAI-compatible LOCAL endpoints next to their vendor accounts, and
    # the model decides which one a session needs). A single-provider engine
    # has no filter: every row on it is its vendor, validated at insert, and
    # get_model_provider's prefix guess for an unknown id must never gate a
    # spawn there.
    resolved_provider = (
        app_config.get_model_provider(model, layer=execution_path)
        if len(caps.providers or ()) > 1 and model else ""
    )

    # 2. Acquire a subscription from the pool.
    file_delivery = caps.auth.credential_file is not None
    sub_handle = acquire_subscription(
        execution_path, user_sub, provider=resolved_provider,
        sticky_scope=sticky_scope if file_delivery else "",
        model=model,
    )
    if not sub_handle:
        return "", {}

    # Everything after the acquire runs under one guard: a failure here
    # (a DB read for the local-model catalog, a relay mint) used to leave
    # the seat taken with no handle for anyone to release — the builder
    # swallowed the exception and reported no subscription.
    try:
        # Stamp the expiry of the token THIS spawn will freeze into its env —
        # bind_session (called by the layer moments later in the same spawn flow)
        # snapshots it per session so the re-warm worker / turn-start guard track
        # the frozen token's real runway, not the store's latest.
        if sub_handle.oauth_expires_at_ms:
            _issued_token_expiry[sub_handle.subscription_id] = sub_handle.oauth_expires_at_ms
        else:
            _issued_token_expiry.pop(sub_handle.subscription_id, None)

        # 3. The hosted relay: mint a per-user token + this provider's relay
        # endpoint (the vendor key never reaches the install), and hand them
        # to the engine like any BYO key + endpoint. Fail-soft — if the relay
        # is unavailable / out of credit / over seat, surface no creds (a
        # clean "no LLM credentials" error) and give the pool slot back.
        if sub_handle.auth_type == "relay":
            creds = relay_llm_credentials(sub_handle.layer, sub_handle.provider, user_sub)
            if not creds:
                subscription_store.decrement_active_sessions(sub_handle.subscription_id)
                return "", {}
            sub_handle = dataclasses.replace(
                sub_handle, api_key=creds[0], endpoint_url=creds[1],
            )

        # 4. The engine maps the credential to its own env.
        env = layer.subscription_env(sub_handle)

    except Exception:
        release_unbound_seat(sub_handle.subscription_id, sticky_scope if file_delivery else "")
        raise
    return sub_handle.subscription_id, env


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

def _refresh_error_terminal(out) -> bool:
    """True only for a provider-confirmed dead grant: OAuth2 ``invalid_grant``
    in the error body. Everything else — 429/5xx, WAF challenge pages
    (non-JSON bodies), an ``invalid_scope``-class request bug on our side,
    client-id drift — must stay transient: a blanket status-code rule would
    mass-expire every healthy row over a single request-construction fault.
    Grants whose provider never confirms death this way are caught by the
    sustained-401 streak instead (``_sustained_auth_dead``)."""
    if out.status not in (400, 401) or not isinstance(out.body, dict):
        return False
    err = out.body.get("error")
    if isinstance(err, dict):
        err = err.get("type") or err.get("code") or err.get("error")
    return err == "invalid_grant"


def _log_refresh_failure(sub_id: str, vendor: str, out) -> None:
    detail = ""
    auth_shaped = False
    if isinstance(out.body, dict):
        err = out.body.get("error")
        if isinstance(err, dict):
            err = err.get("type") or err.get("code") or err.get("error")
        desc = out.body.get("error_description") or ""
        detail = f" ({err}{': ' + desc if desc else ''})"
        # A provider auth rejection (as opposed to a WAF/proxy page): the
        # token endpoint itself answered 401 with a structured error body.
        auth_shaped = out.status == 401 and err is not None
    elif out.error:
        detail = f" ({out.error})"
    logger.error(
        f"{vendor} OAuth refresh failed for {sub_id[:8]}: "
        f"{out.status or 'no response'}{detail}"
    )
    if auth_shaped:
        first, count = _auth_fail_streaks.get(sub_id) or (time.time(), 0)
        _auth_fail_streaks[sub_id] = (first, count + 1)
    if out.status == 429:
        _rest_account(sub_id, time.time() + _REFRESH_RATELIMIT_COOLDOWN_S)


def _sustained_auth_dead(sub_id: str) -> bool:
    """The streak verdict: enough consecutive provider auth rejections, for
    long enough, that the grant is dead even though the provider never said
    ``invalid_grant``. Both thresholds must hold — at the 600 s backoff cap
    the span condition dominates (~13 attempts over 2 h), so a brief vendor
    incident or WAF event can never reach it, while a genuinely dead login
    flips within ~2 h instead of retrying every 10 minutes forever
    (observed: OpenAI answering 401 invalid_request_error 118+ times)."""
    first, count = _auth_fail_streaks.get(sub_id) or (0.0, 0)
    return (
        count >= _AUTH_STREAK_MIN_ATTEMPTS
        and first
        and time.time() - first >= _AUTH_STREAK_MIN_SPAN_S
    )


def clear_refresh_backoff(sub_id: str) -> None:
    """Drop a subscription's refresh backoff — the reconnect exchange calls
    this after replacing the credential, so a just-revived row isn't left
    waiting out a dead token's backoff window (up to 10 min) or carrying the
    dead grant's auth-failure streak into its fresh credential."""
    _refresh_backoff.pop(sub_id, None)
    _auth_fail_streaks.pop(sub_id, None)


def _refresh_oauth_token(
    sub: dict, refresh_token: str, stored: dict | None = None,
) -> tuple[str | None, bool]:
    """Refresh an expired OAuth access token using the stored refresh token.

    The vendor call is the ENGINE's (``ExecutionLayer.refresh_oauth``, looked
    up by the row's layer); everything around it is the pool's: persisting
    the record with the platform keys carried across the rotation
    (``_carry_platform_keys``), the failure verdict, and the fan-out.
    Returns ``(new_access_token, terminal)`` — the token is None on failure,
    and ``terminal`` is True only when the provider confirmed the grant dead
    (``invalid_grant``; see ``_refresh_error_terminal``) or the sustained
    401 streak did.

    EVERY successful rotation fans the new token out to all live bound
    sessions' credential files before returning — providers revoke older
    outstanding access tokens on rotation, so a rotation whose fan-out is
    skipped strands every other live session on a revoked token. This wrapper
    is the single rotation chokepoint (both the spawn-time resolve and
    ``ensure_fresh_and_fan_out`` land here, already holding the sub's refresh
    lock).

    Fail-soft, deliberately: an engine that is no longer registered, one
    that takes no login (an OAuth row stored on it by an exchange that did
    not validate the layer — no migration removes such rows) or an adapter
    that raises all count as a TRANSIENT failure — logged, backed off, never
    terminal — the envelope the vendor refreshers always had.
    """
    sub_id = sub["id"]
    try:
        from core.session.session_manager import get_layer_by_path
        layer = get_layer_by_path(str(sub.get("layer") or ""))
        vendor = layer.capabilities.identity.vendor_label or layer.capabilities.name
        vendor_id = layer.capabilities.identity.vendor_id
        if vendor_id and (sub.get("provider") or vendor_id) != vendor_id:
            # A row stored on the wrong engine before the login routes
            # checked it: its refresh token must never reach another
            # vendor's token endpoint.
            raise ValueError(f"a {sub.get('provider')} login on a {vendor_id} engine")
        # ``stored`` is the credential the caller read under the refresh lock
        # (one read, not a second one racing the caller's).
        if stored is None:
            stored = subscription_store.get_credential_data(sub_id)
        out = layer.refresh_oauth(refresh_token, stored)
    except Exception as e:
        logger.error(f"OAuth refresh for {sub_id[:8]} could not run: {e}")
        return None, False
    if out.oauth_token is None:
        _log_refresh_failure(sub_id, vendor, out)
        return None, _refresh_error_terminal(out) or _sustained_auth_dead(sub_id)
    new_cred = dict(stored)
    new_cred["oauth_token"] = _carry_platform_keys(
        out.oauth_token, stored.get("oauth_token") or {}, out.refresh_token_expires_in,
    )
    new_cred.update(out.extra)
    subscription_store.update_credential_data(sub_id, new_cred)
    logger.info(f"{vendor} OAuth token refreshed for {sub_id[:8]}")
    _fan_out_rotated_token(sub)
    return out.oauth_token.get("accessToken"), False


def _carry_platform_keys(new_token: dict, old: dict, refresh_token_expires_in) -> dict:
    """The platform's own keys on the stored ``oauth_token`` record, carried
    across a rotation the vendor adapter knows nothing about.

    ``refreshTokenExpiresAt`` is the login GRANT's expiry (finite — ~28 days
    for Claude logins), not the rotated access token's: recomputed when the
    response reported ``refresh_token_expires_in``, else the stored value
    carries forward (erasing it on every 8h rotation would blind the
    pre-expiry warning). ``healthAlerts`` (the warning dedup stamps, see
    subscription_health) and ``accountUuid`` (the exchange's second match
    key) ride along for the same reason — a full reconnect rebuilds the blob
    without them, which is exactly the re-arm."""
    token = dict(new_token)
    if refresh_token_expires_in:
        token["refreshTokenExpiresAt"] = int((time.time() + refresh_token_expires_in) * 1000)
    elif old.get("refreshTokenExpiresAt"):
        token["refreshTokenExpiresAt"] = int(old["refreshTokenExpiresAt"])
    if old.get("healthAlerts"):
        token["healthAlerts"] = old["healthAlerts"]
    if old.get("accountUuid") and not token.get("accountUuid"):
        token["accountUuid"] = old["accountUuid"]
    return token


def _fan_out_rotated_token(sub: dict) -> None:
    """Rewrite every live bound session's credential file with the freshly
    rotated token (see ``token_fanout``) — the file payload is the engine's
    (``credential_file_payload``, from the row's layer), built from the
    stored token and its expiry, the pair the per-session snapshot records.
    Snapshots advance only for sessions whose file write landed — a failed
    write keeps the old snapshot so the turn-start guard retries. Best-effort:
    a fan-out error must never fail the refresh that triggered it (the
    backstop is each CLI's own on-401 file re-read)."""
    sub_id = sub["id"]
    with _session_maps_lock:
        sessions = [
            sid for sid, bound in _session_subscriptions.items() if bound == sub_id
        ]
    if not sessions:
        return
    try:
        from core.session.session_manager import get_layer_by_path
        layer = get_layer_by_path(str(sub.get("layer") or ""))
        cred = subscription_store.get_credential_data(sub_id)
        oauth = cred.get("oauth_token") or {}
        token = oauth.get("accessToken")
        new_expiry = int(oauth.get("expiresAt") or 0)
        payload = layer.credential_file_payload(token, new_expiry, cred) if token else None
        if payload is None:
            logger.info(
                f"Token fan-out for {sub_id[:8]}: no credential file to rewrite "
                f"({len(sessions)} bound session(s) keep their current file)"
            )
            return

        def _on_written(session_id: str) -> None:
            # Called from the fan-out (worker thread for local writes, event
            # loop for satellite pushes) — guard so a set can't race a
            # release_subscription pop and orphan the expiry entry.
            with _session_maps_lock:
                if new_expiry and session_id in _session_subscriptions \
                        and _session_subscriptions[session_id] == sub_id:
                    _session_token_expiry[session_id] = new_expiry

        from services.engines import token_fanout
        token_fanout.fan_out(
            sessions, layer=layer.capabilities.name, payload=payload,
            on_written=_on_written, expected_sub_id=sub_id,
        )
    except Exception:
        logger.exception(f"Token fan-out failed for {sub_id[:8]}")


def fan_out_current_token(sub_id: str) -> None:
    """Push the subscription's CURRENT stored token to every live bound
    session's credential file. For credential replacements that bypass the
    rotation chokepoint — the OAuth reconnect exchange writes fresh tokens
    straight to the store, and without a fan-out the bound sessions' files
    keep the pre-exchange token, which the provider may revoke on the grant
    rotation and which 401-recovery (a re-read of the same stale file) can
    never repair. Sync — call via ``asyncio.to_thread``."""
    sub = subscription_store.get_subscription(sub_id)
    if sub:
        _fan_out_rotated_token(sub)


def ensure_fresh_and_fan_out(
    sub_id: str, min_runway_ms: int = TURN_MIN_TOKEN_RUNWAY_MS,
) -> bool:
    """Ensure the subscription's stored token has at least ``min_runway_ms``
    of life, refreshing + fanning out to all live bound sessions if not.

    The single primitive behind every non-spawn freshness path: the dashboard
    turn-start guard (45 min) and the token-freshness worker both call it —
    one lock-guarded, single-flight implementation, so they can never race a
    double rotation. Returns True when the token now meets the runway, False
    on a fail-soft (refresh failed; sessions run out their current token and
    repair on a later attempt). Sync — call via ``asyncio.to_thread``.
    """
    sub = subscription_store.get_subscription(sub_id)
    if not sub:
        return False
    oauth = subscription_store.get_credential_data(sub_id).get("oauth_token")
    if not oauth:
        return True  # non-expiring credential — nothing to keep fresh
    token, expires_at = _resolve_oauth_access_token(
        sub, oauth, min_runway_ms=min_runway_ms,
    )
    if not token:
        return False
    return not expires_at or time.time() * 1000 < expires_at - min_runway_ms


def _resolve_oauth_access_token(
    sub: dict, oauth_data: dict, *, min_runway_ms: int = _SPAWN_REFRESH_RUNWAY_MS,
) -> tuple[str | None, int]:
    """(access token, its expiresAt epoch ms) for an OAuth subscription,
    refreshed when below ``min_runway_ms`` (spawn threshold by default; the
    turn-guard/worker path passes its own). expiry is 0 when the credential
    has no expiry info. A successful refresh fans the rotated token out to
    every live bound session (see ``_refresh_oauth_token``).

    A transient refresh failure never wastes a still-valid stored token: the
    failure is logged and backed off (exponential: 60s → 600s cap), and the
    stored token keeps sessions spawning while the admin gets signal —
    transient failures NEVER expire the row (a provider outage must not lock
    an account out until a human reconnects it), though a sustained streak of
    provider 401s earns the terminal verdict after ~2 h
    (``_sustained_auth_dead``). Only a provider-confirmed
    ``invalid_grant`` (the login grant itself is dead) expires the row, and it
    does so immediately. The returned expiry is the fail-soft's audit trail:
    it reports the runway of the token ACTUALLY handed out, so per-session
    tracking can re-warm a session that spawned on a short-runway stored
    token before it dies mid-turn.
    """
    sub_id = sub["id"]

    # A non-active row never hits the token endpoint again: its grant is
    # known-dead (terminal verdict) or an admin turned it off. Reconnect
    # resets status to active, which re-enables refresh. Bound sessions keep
    # running on the stored token while it lives.
    if (sub.get("status") or subscription_status.ACTIVE) != subscription_status.ACTIVE:
        expires_at = oauth_data.get("expiresAt", 0)
        usable = not expires_at or time.time() * 1000 < expires_at - _HARD_EXPIRY_BUFFER_MS
        return (oauth_data.get("accessToken"), expires_at) if usable else (None, 0)

    def _stored(data: dict) -> tuple[str | None, int, bool, bool]:
        """(accessToken, expiresAt, usable_now, wants_refresh) for a blob."""
        expires_at = data.get("expiresAt", 0)
        now_ms = int(time.time() * 1000)
        if not expires_at:
            return data.get("accessToken"), 0, True, False  # no expiry info — use as-is
        return (
            data.get("accessToken"),
            expires_at,
            now_ms < expires_at - _HARD_EXPIRY_BUFFER_MS,
            now_ms >= expires_at - min_runway_ms,
        )

    token, expires_at, usable, wants_refresh = _stored(oauth_data)
    if not wants_refresh:
        return token, expires_at

    with _refresh_lock(sub_id):
        # Re-read under the lock — a concurrent acquisition may have refreshed
        # while we waited, and its rotation consumed our refresh token.
        latest_cred = subscription_store.get_credential_data(sub_id)
        latest = latest_cred.get("oauth_token") or oauth_data
        token, expires_at, usable, wants_refresh = _stored(latest)
        if not wants_refresh:
            return token, expires_at

        now_s = time.time()
        backoff_entry = _refresh_backoff.get(sub_id)
        if backoff_entry:
            fail_time, attempts = backoff_entry
            wait = min(60 * (2 ** (attempts - 1)), 600)
            if now_s - fail_time < wait:
                logger.debug(
                    f"OAuth refresh skipped for {sub_id[:8]} (backoff {wait}s, attempt {attempts})"
                )
                return (token, expires_at) if usable else (None, 0)

        new_access = None
        terminal = False
        refresh_token = latest.get("refreshToken")
        if refresh_token:
            new_access, terminal = _refresh_oauth_token(sub, refresh_token, latest_cred)
        if new_access:
            _refresh_backoff.pop(sub_id, None)
            _auth_fail_streaks.pop(sub_id, None)
            # The refresher persisted the rotated credential; re-read for the
            # fresh token's real expiry (provider-reported expires_in).
            new_expires = 0
            with contextlib.suppress(Exception):
                new_expires = int(
                    (subscription_store.get_credential_data(sub_id).get("oauth_token") or {})
                    .get("expiresAt") or 0
                )
            return new_access, new_expires

        # A failure verdict is only actionable if the stored refresh token is
        # still the one the attempt used. Writers outside this lock (a
        # reconnect exchange landing through an older code path, an admin
        # credential replacement) may have swapped the credential mid-attempt
        # — their fresh grant must not inherit a dead token's verdict.
        if refresh_token:
            stored_now = (
                subscription_store.get_credential_data(sub_id).get("oauth_token") or {}
            )
            if stored_now.get("refreshToken") != refresh_token:
                logger.info(
                    f"OAuth refresh failure for {sub_id[:8]} discarded — "
                    f"credential was replaced mid-attempt"
                )
                # The replacement may not have gone through the reconnect
                # exchange (clear_refresh_backoff) — drop the dead grant's
                # streak here too, or the fresh grant's FIRST 401 would
                # inherit an exhausted streak and expire it instantly.
                _auth_fail_streaks.pop(sub_id, None)
                token, expires_at, usable, _ = _stored(stored_now)
                return (token, expires_at) if usable else (None, 0)

        if terminal:
            _auto_expire_subscription(
                sub_id,
                reason=(
                    "sustained auth-rejected refresh (401 streak ≥2h)"
                    if _sustained_auth_dead(sub_id)
                    else "invalid_grant (login grant dead)"
                ),
            )
            return (token, expires_at) if usable else (None, 0)

        attempts = (backoff_entry[1] + 1) if backoff_entry else 1
        _refresh_backoff[sub_id] = (now_s, attempts)
        logger.warning(
            f"OAuth refresh failed for {sub_id[:8]} (attempt {attempts}); "
            + (f"using stored token ({max(0, (expires_at - time.time() * 1000)) / 60000:.0f} min runway)"
               if usable else "no usable token")
        )
        return (token, expires_at) if usable else (None, 0)


def _auto_expire_subscription(sub_id: str, reason: str = "invalid_grant (login grant dead)") -> None:
    """Mark a subscription expired: the provider terminally rejected its
    refresh token — either a single ``invalid_grant`` verdict (e.g. the
    ~28-day Claude login lifetime lapsed) or a sustained 401 streak from a
    provider that never says so (see ``_sustained_auth_dead``).

    Persisted so the verdict survives restarts. Recovery is reconnecting the
    SAME account (User Settings → AI Engines for user rows; Setup → Execution
    Layers for admin pool rows) — the exchange matches on account identity
    and revives the row in place. The subscription_health sweep notifies the
    owner (dedup-stamped in the credential blob).
    """
    try:
        subscription_store.update_subscription(sub_id, status=subscription_status.EXPIRED)
        _refresh_backoff.pop(sub_id, None)
        _auth_fail_streaks.pop(sub_id, None)
        logger.error(
            f"OAuth subscription {sub_id[:8]} expired: {reason}. The owner "
            f"must reconnect the same account to revive it."
        )
    except Exception as e:
        logger.error(f"Failed to auto-expire subscription {sub_id[:8]}: {e}")


# ---------------------------------------------------------------------------
# Handle builder
# ---------------------------------------------------------------------------

def _build_handle(sub: dict) -> SubscriptionHandle:
    """Build a SubscriptionHandle from a subscription row, decrypting the
    credential. The vendor-shaped parts (a Codex auth blob, the Claude grant
    metadata) stay inside ``credential`` for the engine's adapter to read;
    the pool only resolves the OAuth access token with spawn runway."""
    cred_data = subscription_store.get_credential_data(sub["id"])

    oauth_access_token: str | None = None
    oauth_expires_at_ms = 0
    oauth_data = cred_data.get("oauth_token")
    if oauth_data:
        oauth_access_token, oauth_expires_at_ms = _resolve_oauth_access_token(sub, oauth_data)

    return SubscriptionHandle(
        subscription_id=sub["id"],
        layer=sub["layer"],
        provider=sub["provider"],
        auth_type=sub["auth_type"],
        api_key=cred_data.get("api_key"),
        oauth_access_token=oauth_access_token,
        endpoint_url=cred_data.get("endpoint_url"),
        oauth_expires_at_ms=oauth_expires_at_ms,
        credential=cred_data,
    )

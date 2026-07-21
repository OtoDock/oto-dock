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
with the store's is_primary / least-active order breaking remaining ties.
Bindings are mirrored to the DB (subscription_session_bindings) so usage
attribution and stickiness survive proxy restarts.

Auth credentials are returned as env-var-ready values:
- API key: SubscriptionHandle.api_key → ANTHROPIC_API_KEY
- OAuth:   a session-file blob — the layer writes it into the session's config
  dir (Claude: ``_CLAUDE_CREDS_BLOB`` → ``.credentials.json``; Codex:
  ``_CODEX_OAUTH_TOKEN``/``_CODEX_AUTH_BLOB`` → ``auth.json``). Never an env
  token: env is frozen at exec, so a live CLI could never pick up a rotation,
  and providers revoke older access tokens when the refresh token rotates.
  The CLIs re-read their credential file (Claude: mtime-watch + 401-recovery;
  Codex: guarded reload), so the pool rotates and FANS OUT — see
  ``ensure_fresh_and_fan_out`` and ``services/engines/token_fanout``.
- Local:   SubscriptionHandle.endpoint_url → provider-specific env var

The pool is the SOLE rotator: session files carry a blank refresh token, so a
CLI physically cannot self-rotate (a cascade of CLI-side rotations is exactly
what revoked live sessions' tokens).

All functions are synchronous (call via asyncio.to_thread).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass

import requests

from storage import subscription_store

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
# After a failed refresh, wait before retrying (prevents rate limit loops)
_refresh_backoff: dict[str, tuple[float, int]] = {}

# After this many consecutive refresh failures, mark the subscription as expired
# in DB so it won't be retried even across proxy restarts.
_MAX_REFRESH_ATTEMPTS = 5

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
        return False
    return True


# Module import ≈ proxy boot. For the first minutes after a restart the live
# session registries are still warming (satellites reconnect and re-announce
# their surviving sessions lazily) — a persisted binding row must not be
# judged dead, let alone deleted, before its session had a chance to reappear.
_BOOT_MONOTONIC = time.monotonic()
_LIVENESS_BOOT_GRACE_S = 600.0


def _session_registered_live(session_id: str) -> bool | None:
    """Is this session live in ANY registry that can hold a bound CLI session?
    Checks the pool's own live map, the interactive registry (local + remote
    PTYs, incl. re-adopted ones), both local headless layers, and the remote
    layer (incl. post-restart adopted sessions — ``adopt_session`` re-registers
    there without re-binding, which is exactly why the persisted rows exist).
    Returns None when a registry can't be consulted (import/attr failure) —
    the caller must fail SOFT and treat the session as live: wrongly deleting
    a live session's row breaks its usage attribution and scope pin."""
    try:
        if session_id in _session_subscriptions:
            return True
        from core.session import interactive_session
        if interactive_session.get(session_id) is not None:
            return True
        from core.layers.cli.session import _persistent_sessions
        if session_id in _persistent_sessions:
            return True
        from core.layers.codex.session import _codex_sessions
        if session_id in _codex_sessions:
            return True
        from core.session import session_manager
        remote_layer = session_manager._remote_layer  # never lazily create here
        if remote_layer is not None and session_id in remote_layer._sessions:
            return True
        return False
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
    _throttled_until[sub_id] = time.time() + cooldown_s
    logger.info(f"Pool: throttled subscription {sub_id[:8]} for {cooldown_s}s (limit hit)")
    if cooldown_s >= _THROTTLE_COOLDOWN_S:
        _throttled_hard.add(sub_id)
        schedule_rebalance("provider limit")


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
    "usage limit", "quota", "insufficient_quota",
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

# Anthropic OAuth token endpoint and client_id (from Claude Code CLI v2.1.97+)
_ANTHROPIC_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_ANTHROPIC_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"


def _derive_subscription_fields(profile: dict) -> tuple[str, str]:
    """Map ``/api/oauth/profile`` JSON to the ``(subscriptionType,
    rateLimitTier)`` pair of ``.credentials.json``. The Claude Code TUI gates
    plan-included models (e.g. Fable 5 on Max) on ``subscriptionType`` — an
    empty value makes it classify the login as API/credits and refuse them."""
    account = profile.get("account") or {}
    org = profile.get("organization") or {}
    if account.get("has_claude_max"):
        sub_type = "max"
    elif account.get("has_claude_pro"):
        sub_type = "pro"
    else:
        # organization_type is "claude_<tier>" (claude_max, claude_enterprise, …)
        sub_type = str(org.get("organization_type") or "").removeprefix("claude_")
    return sub_type, str(org.get("rate_limit_tier") or "")


def fetch_anthropic_subscription_fields(access_token: str) -> tuple[str, str]:
    """Best-effort plan-tier lookup for an Anthropic OAuth access token.

    The token endpoint does not echo the plan tier, so it is resolved from the
    profile endpoint the CLI itself uses. Returns ``("", "")`` on any failure —
    callers treat that as "keep whatever is stored"."""
    try:
        import httpx as _httpx
        resp = _httpx.get(
            _ANTHROPIC_PROFILE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "anthropic-beta": "oauth-2025-04-20",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return "", ""
        return _derive_subscription_fields(resp.json())
    except Exception:
        return "", ""

# Auth types a NON-owner user may borrow from the admin pool. OAuth consumer
# subscriptions are deliberately excluded (they are strictly per-owner). To
# restore admin-subscription pooling for users, add 'oauth' here — nothing else
# in the resolver changes.
_USER_BORROWABLE_AUTH_TYPES = frozenset({"api_key", "relay", "local_endpoint"})


class NoSubscriptionError(Exception):
    """Raised when USER-scoped work resolves to no usable credentials, so the
    dashboard can show an actionable message instead of a cryptic provider 401.
    ``reason`` ∈ {auth_off, admin_oauth_only, no_pool, none, throttled} (see
    ``user_scope_block_reason``)."""

    _MESSAGES = {
        "throttled": "Your subscription is briefly resting after the provider reported "
                     "a rate limit or overload — try again in a few seconds.",
        "auth_off": "You don't have a subscription for this execution layer. "
                    "Connect your account in your User Settings.",
        "admin_oauth_only": "You don't have a subscription for this execution layer. "
                            "Connect your account in your User Settings.",
        "no_pool": "No subscription is configured for this execution layer. Connect your "
                   "account in your User Settings, or ask an administrator to add one.",
        "none": "No usable subscription for this execution layer. "
                "Connect your account in your User Settings.",
    }

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(self._MESSAGES.get(reason, self._MESSAGES["none"]))


@dataclass
class SubscriptionHandle:
    """Result of acquiring a subscription for a session."""
    subscription_id: str
    layer: str
    provider: str
    auth_type: str                  # 'oauth' | 'api_key' | 'local_endpoint' | 'relay'
    api_key: str | None             # for api_key auth → ANTHROPIC_API_KEY
    oauth_access_token: str | None  # for OAuth → the session credential file
    endpoint_url: str | None        # for local models (Ollama, LM Studio)
    codex_auth_blob: dict | None = None  # full Codex auth.json for session reconstruction
    # expiresAt (epoch ms) of oauth_access_token — 0/absent for non-expiring
    # credentials. This is the expiry of the token ACTUALLY issued (the stored
    # one on a fail-soft refresh), the input to per-session runway tracking.
    oauth_expires_at_ms: int = 0
    # Ready-to-write ``claudeAiOauth`` payload for the session's
    # ``.credentials.json`` (Claude CLI file delivery) — refresh token
    # neutralized (pool = sole rotator). None for non-OAuth credentials.
    claude_creds_blob: dict | None = None


def _select(
    candidates: list[dict],
    *,
    allowed_auth: frozenset[str] | None,
) -> SubscriptionHandle | None:
    """Try candidates in priority order, claiming the first that yields usable
    credentials. ``candidates`` is already ordered (is_primary DESC, least-active
    first) by the store; here we additionally push hosted-relay subs last (a real
    BYO credential always wins over the relay's credit cost + latency hop).

    ``allowed_auth=None`` → no auth-type restriction (agent-scope / the owner's own
    accounts). When a restriction is given (a user borrowing the admin pool) it is
    enforced HERE, on the same list the usable-credential check reads, so an
    excluded auth type (notably ``oauth``) can never slip through a later branch.

    Ordering: a real BYO credential always beats the hosted relay (credit cost +
    latency); within that, route to the LEAST-CONSUMED account — recent burn
    (the ~5h window, tracking the provider's rolling reset) first, the 7-day
    total as tiebreak (weekly caps). The store's is_primary / least-active order
    breaks remaining ties (stable sort). Subscriptions currently throttled
    (recently hit a provider limit) are skipped so the next chat/turn fails over
    to a fresh account.
    """
    auth_ok = [
        c for c in candidates
        if allowed_auth is None or c.get("auth_type") in allowed_auth
    ]
    pool = [c for c in auth_ok if not _is_throttled(c["id"])]
    if not pool:
        # Every eligible sub is briefly resting (recent provider limit/overload).
        # Throttling should DE-PRIORITISE, not eliminate: rather than hard-block the
        # user — fatal for a single-account install over a transient 529 — fall back
        # to the resting set. The provider's own retry/limit response then governs
        # (a transient overload usually clears on the immediate retry; a real limit
        # surfaces the real provider error instead of a misleading "no subscription").
        # auth_ok already enforces allowed_auth, so a borrowing user still never gets
        # an OAuth sub here.
        pool = auth_ok
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
        pool.sort(key=lambda s: (s.get("auth_type") == "relay",
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
    return f"{target or 'local'}:{host_dir}"


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
            try:
                subscription_store.delete_session_binding(sid)
                logger.info(
                    f"Pool: dropped ghost binding of dead session {sid[:8]} "
                    f"(scope no longer pinned by it)"
                )
            except Exception:
                pass
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
) -> SubscriptionHandle | None:
    """Reuse the scope's committed account IF it is still in this acquisition's
    candidate list (same eligibility the normal path enforces — a delisted or
    non-borrowable pin falls through to fresh selection; the rebind fan-out
    re-homes the scope's live sessions in that case). Throttling is deliberately
    NOT honored here: the shared-file constraint dominates a resting account."""
    if not sticky_scope:
        return None
    pinned = _sticky_subscription_id(sticky_scope)
    if not pinned:
        return None
    match = [c for c in candidates if c["id"] == pinned]
    if not match:
        return None
    handle = _select(match, allowed_auth=allowed_auth)
    if handle:
        logger.info(
            f"Pool: scope-sticky reuse of subscription {pinned[:8]} "
            f"(scope already holds its credential file)"
        )
    return handle


def acquire_subscription(
    layer: str,
    user_sub: str | None,
    *,
    provider: str = "",
    sticky_scope: str = "",
) -> SubscriptionHandle | None:
    """Select and acquire a subscription for a new session.

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

    Returns None when nothing is available (caller surfaces the block; see
    ``user_scope_block_reason`` for the user-facing reason).
    """
    user_sub = user_sub or None  # treat "" as agent-scope; never match owner_sub='' infra

    handle: SubscriptionHandle | None = None
    if user_sub:
        # 1. The user's own usable accounts (any auth type, incl. their own OAuth)
        personal = subscription_store.list_personal(layer, user_sub, provider or None)
        handle = _select_sticky(sticky_scope, personal, allowed_auth=None) \
            or _select(personal, allowed_auth=None)
        if not handle:
            # 2. Platform fallback, gated by the per-user Platform Auth toggle
            if not subscription_store.get_user_allow_platform_auth(user_sub):
                logger.info(f"Pool: user {user_sub[:8]} has platform auth disabled, no subscription available")
                return None
            # 3. Borrow ONLY admin API-type credentials — never an admin OAuth sub
            platform = subscription_store.list_platform_pool(layer, provider or None)
            handle = _select_sticky(sticky_scope, platform,
                                    allowed_auth=_USER_BORROWABLE_AUTH_TYPES) \
                or _select(platform, allowed_auth=_USER_BORROWABLE_AUTH_TYPES)
            if handle:
                # Defense-in-depth: a user-scope handle must never be an OAuth subscription.
                assert handle.auth_type in _USER_BORROWABLE_AUTH_TYPES, (
                    f"user-scope acquired non-borrowable auth_type={handle.auth_type}"
                )
            else:
                logger.warning(f"Pool: no borrowable platform credentials for user {user_sub[:8]}, layer={layer}")
                return None
    else:
        # AGENT-SCOPE: the full platform pool (OAuth subscriptions allowed)
        platform = subscription_store.list_platform_pool(layer, provider or None)
        handle = _select_sticky(sticky_scope, platform, allowed_auth=None) \
            or _select(platform, allowed_auth=None)

    if handle and sticky_scope:
        # Stickiness can only bridge sessions whose candidate lists overlap. A
        # Shared-only agent's chats share ONE credential dir across ALL users
        # (sender-pays: each user's session runs on their own account), so two
        # users concurrently active on such an agent land different accounts on
        # the same file — the last-write-wins flap stickiness exists to prevent.
        # Surface it loudly; the full fix is per-payer credential delivery
        # for shared scopes.
        held = _sticky_subscription_id(sticky_scope)
        if held and held != handle.subscription_id:
            logger.warning(
                f"Pool: credential scope {sticky_scope!r} is live on subscription "
                f"{held[:8]} but this spawn selected {handle.subscription_id[:8]} "
                f"(different payer) — concurrent sessions on this scope may "
                f"contend over the shared credential file"
            )
        # Commit the scope to this account for the acquire→bind window, so a
        # concurrent same-scope spawn can't pick a different one meanwhile.
        with _session_maps_lock:
            _scope_recent[sticky_scope] = (handle.subscription_id, time.time())
    return handle


def user_scope_block_reason(layer: str, user_sub: str, *, provider: str = "") -> str:
    """Classify why a user-scoped acquisition found no credentials, for the
    dashboard "no subscription" message. Cheap; called only on the terminal
    blocked path. Returns one of:
      'throttled'        — the user owns a sub for this layer but it's resting
                           (recent provider rate-limit/overload) — transient, retry
      'auth_off'         — Platform Auth disabled and the user has no own sub
      'admin_oauth_only' — pool exists but holds only OAuth subs (not borrowable)
      'no_pool'          — nothing in the platform pool for this layer at all
      'none'             — a borrowable sub exists but couldn't yield creds (glitch)
    """
    # The user DOES own a sub here, it's just resting — never tell them to "connect
    # an account". (With _select's throttled-fallback this rarely reaches a block,
    # but keep the classification honest for any caller.)
    own = subscription_store.list_personal(layer, user_sub, provider or None)
    if own and all(_is_throttled(s["id"]) for s in own):
        return "throttled"
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


# Execution layers (AI engines) eligible for auto-enable on agent create/install,
# in priority order. ``direct-llm`` is intentionally excluded — it is never the
# auto-pick for a fresh agent (it needs an explicit model + provider to be useful,
# and the agent-scope pool path can't borrow an admin OAuth sub the way the CLIs
# can). A creator can still enable it by hand in the agent's Config tab.
AUTO_ENABLE_LAYER_ORDER: tuple[str, ...] = ("claude-code-cli", "codex-cli")


def default_execution_layer_for_creator(user_sub: str) -> str:
    """Pick the execution layer (AI engine) to auto-enable for an agent that
    ``user_sub`` is creating or installing, so the agent runs zero-config.

    Returns the first engine in :data:`AUTO_ENABLE_LAYER_ORDER` (claude-code-cli,
    then codex-cli) that is connected on BOTH sides:

      - the PLATFORM — an admin has contributed an active subscription for it to
        the shared pool (:func:`subscription_store.list_platform_pool`), so
        agent-scope sessions (scheduled tasks, agent-scope chats) can run it; and
      - the CREATOR's own account — the creator holds an active personal
        subscription for it (:func:`subscription_store.list_personal`), so the
        creator's own user-scope chats run it too.

    Never returns ``direct-llm``. Falls back to ``claude-code-cli`` when no
    candidate qualifies, so the new agent always has a sensible primary engine
    the creator can finish configuring.

    The agent's ``default_model`` stays empty (Auto → resolved to the best model
    of this primary engine) and ``default_effort`` stays empty (→ High), so the
    pair (engine, model, effort) is fully resolved with zero manual setup.

    Note on the BOTH rule: an admin who *contributes* their only subscription to
    the platform pool but leaves ``use_personal=False`` has no personal row, so
    that engine won't be auto-picked (we fall back to claude). That's an accepted
    edge — the pool engine still works for agent-scope runs, and the creator can
    enable it explicitly afterwards.
    """
    for layer in AUTO_ENABLE_LAYER_ORDER:
        platform_connected = bool(subscription_store.list_platform_pool(layer))
        creator_connected = bool(subscription_store.list_personal(layer, user_sub))
        if platform_connected and creator_connected:
            return layer
    return "claude-code-cli"


def release_subscription(session_id: str) -> None:
    """Release the subscription held by a session."""
    from services.engines import token_fanout
    token_fanout.unregister_session_target(session_id)
    with _session_maps_lock:
        _session_token_expiry.pop(session_id, None)
        _session_binding_ctx.pop(session_id, None)
        _session_scope_keys.pop(session_id, None)
        sub_id = _session_subscriptions.pop(session_id, None)
    try:
        subscription_store.delete_session_binding(session_id)
    except Exception as e:
        # persisted mirror only — the startup TTL prune is the backstop
        logger.debug("session binding delete: %s", e)
    if sub_id:
        subscription_store.decrement_active_sessions(sub_id)
        logger.info(f"Pool: released subscription {sub_id[:8]} for session {session_id[:8]}")


def bind_session(
    session_id: str,
    subscription_id: str,
    *,
    layer: str = "",
    user_sub: str | None = None,
    scope_key: str = "",
) -> None:
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
    # working for sessions that outlive the proxy process. Written INLINE (a
    # single upsert, same weight as release's decrement_active_sessions on the
    # same paths) — deferring it raced a quick bind→release, leaving an orphan
    # row that could pin the scope's sticky selection. Best-effort: the
    # in-memory binding stands either way.
    try:
        subscription_store.upsert_session_binding(
            session_id, subscription_id,
            layer=layer, user_sub=user_sub, scope_key=scope_key,
        )
    except Exception:
        logger.exception("Pool: persisting session binding failed (in-memory binding stands)")
    # Re-bind of a live session (start_session reusing a warm process): the
    # spawn flow acquired a FRESH seat for this round, so the replaced
    # binding's seat must be released — same-sub included (two increments,
    # one binding, one eventual release = +1 leak per warm continue round).
    # Historically masked because a close always released the old binding
    # before the next round's bind; a gracefully-aborted lane's session now
    # stays warm across rounds. Only the layer spawn flows re-bind live
    # sessions: restore_session_binding no-ops on a live binding and the
    # selection rebind moves seats itself without bind_session.
    if prior_sub:
        subscription_store.decrement_active_sessions(prior_sub)
        logger.info(
            f"Pool: re-bound session {session_id[:8]} "
            f"{prior_sub[:8]} → {subscription_id[:8]} (replaced seat released)"
        )


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
    try:
        subscription_store.increment_active_sessions(sub_id)
    except Exception:
        logger.exception("Pool: re-taking seat on restore failed (continuing)")
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
    fan-out's) — keep the loop non-blocking by pushing the DB updates to a
    thread there; local (pool-thread) acks run them inline."""
    def _apply() -> None:
        subscription_store.increment_active_sessions(new_sub)
        subscription_store.decrement_active_sessions(old_sub)
        if session_id:
            try:
                subscription_store.update_session_binding_sub(session_id, new_sub)
            except Exception as e:
                # persisted mirror only; memory is authoritative live
                logger.debug("session binding rebind: %s", e)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _apply()
        return
    task = loop.create_task(asyncio.to_thread(_apply))
    _rebind_tasks.add(task)
    task.add_done_callback(_rebind_tasks.discard)


def _move_scope_group(
    old_sub: str,
    layer: str,
    scope_sub: str,
    sids: list[str],
    *,
    cause: str,
    reason: str,
    replacements: dict[tuple[str, str, str], SubscriptionHandle | None],
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
    rkey = (layer, scope_sub, (old_row or {}).get("provider") or "")
    if rkey not in replacements:
        handle = acquire_subscription(layer, scope_sub or None, provider=rkey[2])
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
            elif require_unthrottled and _is_throttled(handle.subscription_id):
                handle = None
        replacements[rkey] = handle
    handle = replacements[rkey]
    if handle is None:
        stuck_log(
            f"Pool: subscription {old_sub[:8]} {cause} with no eligible "
            f"replacement for {len(sids)} bound session(s) (layer={layer}, "
            f"scope={'user' if scope_sub else 'agent'}) — they keep their "
            f"current credentials until one is connected"
        )
        return

    claude_blob = handle.claude_creds_blob
    codex_auth = None
    if handle.oauth_access_token and handle.codex_auth_blob:
        from core.layers.codex.helpers import build_auth_json
        codex_auth = build_auth_json(
            handle.oauth_access_token, auth_blob=handle.codex_auth_blob,
        )
    deliverable = []
    for sid in swappable:
        t = token_fanout.session_target(sid)
        if t and ((t.kind == "codex" and codex_auth) or
                  (t.kind != "codex" and claude_blob)):
            deliverable.append(sid)
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
        deliverable, claude_blob=claude_blob, codex_auth=codex_auth,
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
        (``_throttled_hard``) — without this, scope-stickiness deliberately
        keeps reusing the limited account (the shared-file constraint beats a
        resting account for NEW spawns) and every session in the scope errors
        until the provider window resets.
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
        replacements: dict[tuple[str, str, str], SubscriptionHandle | None] = {}
        stuck_log = logger.info if reason else logger.debug
        now_mono = time.monotonic()
        since_short, _ = _consumption_window_starts()
        for (old_sub, layer, scope_sub, scope_key), sids in groups.items():
            last = _scope_rebalance_last.get(scope_key)
            if last is not None and now_mono - last < _SCOPE_REBALANCE_COOLDOWN_S:
                continue
            with _session_maps_lock:
                recent = _scope_recent.get(scope_key)
            if recent and time.time() - recent[1] <= _SCOPE_RECENT_TTL_S:
                # A spawn just claimed this scope (acquire→bind window): moving
                # the scope NOW would race the spawn's own credential-file
                # write. Skip WITHOUT stamping the cooldown — the next tick
                # retries once the spawn has bound.
                continue
            if old_sub in _throttled_hard and _is_throttled(old_sub):
                cause = "rate-limited"
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

# Hosted Direct-LLM relay endpoint path per provider. The provider SDK appends
# its own route suffix to base_url, so the install-side endpoint differs per
# provider (the Anthropic SDK adds /v1/messages; the OpenAI-compatible SDKs add
# /chat/completions).
_RELAY_LLM_PATH = {
    "anthropic": "/v1/relay/anthropic",
    "openai": "/v1/relay/openai/v1",
    "groq": "/v1/relay/groq/v1",
}


def relay_llm_credentials(provider: str, user_sub: str | None) -> tuple[str, str] | None:
    """For a hosted (``auth_type='relay'``) direct-llm subscription: mint a per-user
    relay token and build this provider's relay endpoint URL. Returns
    ``(api_key, endpoint_url)`` where ``api_key`` is the minted token, or ``None``
    if the relay is unavailable / refuses (out of credit, over seat, not
    configured) — the caller then surfaces a clean "no credentials" error. Only
    anthropic / openai / groq are relay-backed (Ollama / LiteLLM are local)."""
    import config as app_config
    from services.billing import relay_client

    path = _RELAY_LLM_PATH.get(provider)
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
    """Acquire a subscription and build layer-specific auth env vars.

    Combines provider resolution, subscription acquisition, and credential-to-
    env-var mapping in a single call.  This replaces the duplicated if/else
    pattern that was previously copy-pasted across config_builder,
    task_config_builder, meeting_orchestrator, and phone_config_builder.py.

    ``sticky_scope`` (the spawn's ``credential_scope_key``; CLI builders pass
    it) pins same-scope sessions to one account — see ``acquire_subscription``.

    Returns (subscription_id, env_vars_dict).  On failure returns ("", {}).
    """
    import config as app_config  # local import to avoid circular dependency

    # 1. Resolve provider from execution path + model/agent config
    if execution_path == "direct-llm":
        resolved_provider = app_config.get_model_provider(model) if model else ""
    else:
        # CLI and future layers (Codex, etc.) use per-agent provider field
        resolved_provider = (agent_info or {}).get("codex_provider", "")

    # 2. Acquire subscription from pool (scope-sticky only meaningful for the
    # credential-FILE layers — direct-llm injects keys per env, nothing shared)
    sub_handle = acquire_subscription(
        execution_path, user_sub, provider=resolved_provider,
        sticky_scope=sticky_scope if execution_path in (
            "claude-code-cli", "codex-cli") else "",
    )
    if not sub_handle:
        return "", {}

    # Stamp the expiry of the token THIS spawn will freeze into its env —
    # bind_session (called by the layer moments later in the same spawn flow)
    # snapshots it per session so the re-warm worker / turn-start guard track
    # the frozen token's real runway, not the store's latest.
    if sub_handle.oauth_expires_at_ms:
        _issued_token_expiry[sub_handle.subscription_id] = sub_handle.oauth_expires_at_ms
    else:
        _issued_token_expiry.pop(sub_handle.subscription_id, None)

    # 3. Map credentials to layer-specific env vars
    env: dict[str, str] = {}
    if execution_path == "claude-code-cli":
        if sub_handle.api_key:
            env["ANTHROPIC_API_KEY"] = sub_handle.api_key
        # OAuth rides a session-file blob, never CLAUDE_CODE_OAUTH_TOKEN env:
        # env is frozen at exec (a rotation could never reach a live CLI) and
        # it outranks the credential file in the CLI's auth priority, which
        # would defeat the file-based fan-out. The layer pops this and writes
        # ``.credentials.json`` into the session's CLAUDE_CONFIG_DIR.
        if sub_handle.claude_creds_blob:
            import json as _json
            env["_CLAUDE_CREDS_BLOB"] = _json.dumps(sub_handle.claude_creds_blob)
    elif execution_path == "codex-cli":
        # Codex CLI uses CODEX_API_KEY for API key auth
        if sub_handle.api_key:
            env["CODEX_API_KEY"] = sub_handle.api_key
        # ChatGPT OAuth token — layer writes it to .codex/auth.json
        if sub_handle.oauth_access_token:
            env["_CODEX_OAUTH_TOKEN"] = sub_handle.oauth_access_token
        # Full Codex auth blob for auth.json reconstruction (has id_token, account_id, etc.)
        if sub_handle.codex_auth_blob:
            import json as _json
            env["_CODEX_AUTH_BLOB"] = _json.dumps(sub_handle.codex_auth_blob)
        if sub_handle.endpoint_url:
            env["_CODEX_ENDPOINT_URL"] = sub_handle.endpoint_url
    else:
        # Direct LLM and other layers use generic provider env vars.
        env["_PROVIDER"] = sub_handle.provider
        if sub_handle.auth_type == "relay":
            # Hosted relay: mint a per-user token + point the adapter at this
            # provider's relay endpoint (the vendor key never reaches the install).
            # Fail-soft — if the relay is unavailable / out of credit / over seat,
            # surface no creds (clean "no LLM credentials" error) and release the
            # pool slot we took.
            creds = relay_llm_credentials(sub_handle.provider, user_sub)
            if not creds:
                subscription_store.decrement_active_sessions(sub_handle.subscription_id)
                return "", {}
            env["_API_KEY"], env["_ENDPOINT_URL"] = creds
        else:
            if sub_handle.api_key:
                env["_API_KEY"] = sub_handle.api_key
            if sub_handle.endpoint_url:
                env["_ENDPOINT_URL"] = sub_handle.endpoint_url

    return sub_handle.subscription_id, env


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

def _refresh_oauth_token(sub_id: str, refresh_token: str, provider: str = "anthropic") -> str | None:
    """Refresh an expired OAuth access token using the stored refresh token.

    Supports both Anthropic and OpenAI OAuth providers.
    Returns the new access_token on success, or None on failure.
    Updates the subscription's credential_data in DB with new tokens.

    EVERY successful rotation fans the new token out to all live bound
    sessions' credential files before returning — providers revoke older
    outstanding access tokens on rotation, so a rotation whose fan-out is
    skipped strands every other live session on a revoked token. This wrapper
    is the single rotation chokepoint (both the spawn-time resolve and
    ``ensure_fresh_and_fan_out`` land here, already holding the sub's refresh
    lock).
    """
    if provider == "openai":
        new_access = _refresh_openai_oauth_token(sub_id, refresh_token)
    else:
        new_access = _refresh_anthropic_oauth_token(sub_id, refresh_token)
    if new_access:
        _fan_out_rotated_token(sub_id)
    return new_access


def _fan_out_rotated_token(sub_id: str) -> None:
    """Rewrite every live bound session's credential file with the freshly
    rotated token (see ``token_fanout``). Per-session expiry snapshots advance
    only for sessions whose file write landed — a failed write keeps the old
    snapshot so the turn-start guard retries. Best-effort: a fan-out error
    must never fail the refresh that triggered it (the backstop is each CLI's
    own on-401 file re-read)."""
    with _session_maps_lock:
        sessions = [
            sid for sid, bound in _session_subscriptions.items() if bound == sub_id
        ]
    if not sessions:
        return
    try:
        cred = subscription_store.get_credential_data(sub_id)
        oauth = cred.get("oauth_token") or {}
        new_expiry = int(oauth.get("expiresAt") or 0)
        claude_blob = _claude_file_blob(oauth) if oauth.get("accessToken") else None
        codex_auth = None
        blob = cred.get("codex_auth_blob")
        if blob and oauth.get("accessToken"):
            from core.layers.codex.helpers import build_auth_json
            codex_auth = build_auth_json(oauth["accessToken"], auth_blob=blob)

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
            sessions, claude_blob=claude_blob, codex_auth=codex_auth,
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
    _fan_out_rotated_token(sub_id)


def _claude_file_blob(oauth_data: dict) -> dict:
    """The ``claudeAiOauth`` payload for a session's ``.credentials.json``,
    from a stored oauth_token dict. The refresh token is NEUTRALIZED (blank):
    the pool is the sole rotator — a CLI holding no refresh token physically
    cannot rotate, it can only use the fanned-out access token or 401-recover
    it from disk, which fails SAFE (auth error repaired by the next fan-out)
    instead of cascading revocations."""
    return {
        "accessToken": oauth_data.get("accessToken", ""),
        "refreshToken": "",
        "expiresAt": int(oauth_data.get("expiresAt") or 0),
        "scopes": oauth_data.get("scopes") or [],
        "subscriptionType": oauth_data.get("subscriptionType", ""),
        "rateLimitTier": oauth_data.get("rateLimitTier", ""),
    }


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


def _refresh_anthropic_oauth_token(sub_id: str, refresh_token: str) -> str | None:
    """Refresh an Anthropic OAuth token.

    Uses JSON body matching the Claude Code CLI (not form-urlencoded).
    Omits scope parameter — the CLI omits it for Claude.ai (inference) tokens.
    Including scopes not in the original grant (e.g. org:create_api_key) causes
    'invalid_scope' errors from Anthropic.
    """
    try:
        import httpx as _httpx
        json_body = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _ANTHROPIC_CLIENT_ID,
        }
        resp = _httpx.post(
            _ANTHROPIC_TOKEN_URL,
            json=json_body,
            headers={
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            logger.error(f"Anthropic OAuth refresh failed for {sub_id[:8]}: {resp.status_code}")
            return None

        data = resp.json()
        new_access = data.get("access_token")
        new_refresh = data.get("refresh_token", refresh_token)
        expires_in = data.get("expires_in", 28800)

        # The refresh response does not echo the plan tier — preserve the
        # stored values (overwriting them with "" strips the tier on every
        # 8h rotation, and the TUI then gates Max-included models behind
        # usage credits). Backfill once from the profile endpoint when the
        # stored values are empty too.
        old = subscription_store.get_credential_data(sub_id).get("oauth_token") or {}
        sub_type = data.get("subscriptionType") or old.get("subscriptionType") or ""
        rate_tier = data.get("rateLimitTier") or old.get("rateLimitTier") or ""
        if not sub_type and new_access:
            sub_type, fetched_tier = fetch_anthropic_subscription_fields(new_access)
            rate_tier = rate_tier or fetched_tier

        new_cred = {
            "oauth_token": {
                "accessToken": new_access,
                "refreshToken": new_refresh,
                "expiresAt": int((time.time() + expires_in) * 1000),
                "scopes": data.get("scope", "").split() if data.get("scope") else [],
                "subscriptionType": sub_type,
                "rateLimitTier": rate_tier,
            }
        }
        subscription_store.update_credential_data(sub_id, new_cred)
        logger.info(f"Anthropic OAuth token refreshed for {sub_id[:8]}")
        return new_access
    except Exception as e:
        logger.error(f"Anthropic OAuth refresh error for {sub_id[:8]}: {e}")
        return None


def _refresh_openai_oauth_token(sub_id: str, refresh_token: str) -> str | None:
    """Refresh an OpenAI OAuth token."""
    try:
        from auth.openai_oauth import TOKEN_URL as _OPENAI_TOKEN_URL, CLIENT_ID as _OPENAI_CLIENT_ID
        import urllib.parse
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": _OPENAI_CLIENT_ID,
            "refresh_token": refresh_token,
        })
        resp = requests.post(
            _OPENAI_TOKEN_URL,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            logger.error(f"OpenAI OAuth refresh failed for {sub_id[:8]}: {resp.status_code}")
            return None

        data = resp.json()
        new_access = data.get("access_token")
        new_refresh = data.get("refresh_token", refresh_token)
        expires_in = data.get("expires_in", 28800)

        # Preserve existing credential_data (especially codex_auth_blob)
        existing_cred = subscription_store.get_credential_data(sub_id)
        existing_cred["oauth_token"] = {
            "accessToken": new_access,
            "refreshToken": new_refresh,
            "expiresAt": int((time.time() + expires_in) * 1000),
        }
        # Keep codex_auth_blob tokens in sync
        blob = existing_cred.get("codex_auth_blob")
        if blob and isinstance(blob.get("tokens"), dict):
            blob["tokens"]["access_token"] = new_access
            if new_refresh:
                blob["tokens"]["refresh_token"] = new_refresh
            from datetime import datetime, timezone
            blob["last_refresh"] = datetime.now(timezone.utc).isoformat()
        subscription_store.update_credential_data(sub_id, existing_cred)
        logger.info(f"OpenAI OAuth token refreshed for {sub_id[:8]}")
        return new_access
    except Exception as e:
        logger.error(f"OpenAI OAuth refresh error for {sub_id[:8]}: {e}")
        return None


def _resolve_oauth_access_token(
    sub: dict, oauth_data: dict, *, min_runway_ms: int = _SPAWN_REFRESH_RUNWAY_MS,
) -> tuple[str | None, int]:
    """(access token, its expiresAt epoch ms) for an OAuth subscription,
    refreshed when below ``min_runway_ms`` (spawn threshold by default; the
    turn-guard/worker path passes its own). expiry is 0 when the credential
    has no expiry info. A successful refresh fans the rotated token out to
    every live bound session (see ``_refresh_oauth_token``).

    A refresh failure never wastes a still-valid stored token: the failure is
    logged and backed off (exponential: 60s → 600s cap), and the stored token
    keeps sessions spawning while the admin gets signal. Repeated failures
    still auto-expire the subscription, but only once the stored token is
    genuinely dying — expiring an account whose sessions could still run for
    hours would turn a transient refresh outage into a full lockout. The
    returned expiry is the fail-soft's audit trail: it reports the runway of
    the token ACTUALLY handed out, so per-session tracking can re-warm a
    session that spawned on a short-runway stored token before it dies
    mid-turn.
    """
    sub_id = sub["id"]

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
        latest = subscription_store.get_credential_data(sub_id).get("oauth_token") or oauth_data
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
        refresh_token = latest.get("refreshToken")
        if refresh_token:
            new_access = _refresh_oauth_token(sub_id, refresh_token, sub.get("provider", "anthropic"))
        if new_access:
            _refresh_backoff.pop(sub_id, None)
            # The refresher persisted the rotated credential; re-read for the
            # fresh token's real expiry (provider-reported expires_in).
            new_expires = 0
            with contextlib.suppress(Exception):
                new_expires = int(
                    (subscription_store.get_credential_data(sub_id).get("oauth_token") or {})
                    .get("expiresAt") or 0
                )
            return new_access, new_expires

        attempts = (backoff_entry[1] + 1) if backoff_entry else 1
        _refresh_backoff[sub_id] = (now_s, attempts)
        logger.warning(
            f"OAuth refresh failed for {sub_id[:8]} (attempt {attempts}); "
            + (f"using stored token ({max(0, (expires_at - time.time() * 1000)) / 60000:.0f} min runway)"
               if usable else "no usable token")
        )
        if not usable and attempts >= _MAX_REFRESH_ATTEMPTS:
            _auto_expire_subscription(sub_id)
        return (token, expires_at) if usable else (None, 0)


def _auto_expire_subscription(sub_id: str) -> None:
    """Mark a subscription as expired after repeated refresh failures.

    This persists the decision to DB so it survives proxy restarts —
    the admin can reconnect OAuth when the rate limit clears.
    """
    try:
        subscription_store.update_subscription(sub_id, status="expired")
        _refresh_backoff.pop(sub_id, None)
        logger.error(
            f"OAuth subscription {sub_id[:8]} auto-expired after "
            f"{_MAX_REFRESH_ATTEMPTS} consecutive refresh failures. "
            f"Reconnect via admin Setup → Execution Layers."
        )
    except Exception as e:
        logger.error(f"Failed to auto-expire subscription {sub_id[:8]}: {e}")


# ---------------------------------------------------------------------------
# Handle builder
# ---------------------------------------------------------------------------

def _build_handle(sub: dict) -> SubscriptionHandle:
    """Build a SubscriptionHandle from a subscription row, decrypting credentials."""
    cred_data = subscription_store.get_credential_data(sub["id"])

    api_key = cred_data.get("api_key")
    oauth_access_token: str | None = None
    oauth_expires_at_ms = 0
    claude_creds_blob: dict | None = None
    endpoint_url = cred_data.get("endpoint_url")

    # For OAuth subscriptions, extract an access token with spawn runway
    oauth_data = cred_data.get("oauth_token")
    if oauth_data:
        oauth_access_token, oauth_expires_at_ms = _resolve_oauth_access_token(sub, oauth_data)
        if oauth_access_token:
            # Session-file payload for the Claude CLI. Built from the token
            # ACTUALLY issued (post-refresh or fail-soft stored) + the stored
            # grant metadata; refresh token neutralized.
            claude_creds_blob = _claude_file_blob({
                **oauth_data,
                "accessToken": oauth_access_token,
                "expiresAt": oauth_expires_at_ms,
            })

    # Codex auth blob: full auth.json structure for session reconstruction
    codex_blob = cred_data.get("codex_auth_blob")

    return SubscriptionHandle(
        subscription_id=sub["id"],
        layer=sub["layer"],
        provider=sub["provider"],
        auth_type=sub["auth_type"],
        api_key=api_key,
        oauth_access_token=oauth_access_token,
        endpoint_url=endpoint_url,
        codex_auth_blob=codex_blob,
        oauth_expires_at_ms=oauth_expires_at_ms,
        claude_creds_blob=claude_creds_blob,
    )

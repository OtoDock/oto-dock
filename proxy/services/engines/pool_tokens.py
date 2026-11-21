"""The OAuth token of a subscription: the refresh under its lock, the verdict on a failure, the rotation's
fan-out and the handle a spawn receives.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import cast

import psycopg

from core.execution_layer import SubscriptionHandle
from services.engines.pool_rests import (
    _rest_account,
)
from services.engines.pool_state import (
    _session_maps_lock,
    _session_subscriptions,
    _session_token_expiry,
)
from storage.billing import subscription_status, subscription_store

logger = logging.getLogger(__name__)


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
        "%s OAuth refresh failed for %s: "
        "%s%s",
        vendor, sub_id[:8], out.status or 'no response', detail
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
    # A recorded streak's first stamp is a time.time() value, never 0.0, so
    # the chain ends on a comparison.
    return cast("bool", (
        count >= _AUTH_STREAK_MIN_ATTEMPTS
        and first
        and time.time() - first >= _AUTH_STREAK_MIN_SPAN_S
    ))


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
    except Exception as e:  # noqa: BLE001  # an engine's OAuth refresh raises whatever its vendor client does
        logger.error("OAuth refresh for %s could not run: %s", sub_id[:8], e)
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
    logger.info("%s OAuth token refreshed for %s", vendor, sub_id[:8])
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


def _fan_out_rotated_token(sub: dict, only: list[str] | None = None) -> None:
    """Rewrite every live bound session's credential file (``only``: those
    of them alone) with the freshly
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
            sid for sid, bound in _session_subscriptions.items()
            if bound == sub_id and (only is None or sid in only)
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
                "Token fan-out for %s: no credential file to rewrite "
                "(%s bound session(s) keep their current file)",
                sub_id[:8], len(sessions)
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
        logger.exception("Token fan-out failed for %s", sub_id[:8])


def fan_out_current_token(sub_id: str, only: list[str] | None = None) -> None:
    """Push the subscription's CURRENT stored token to every live bound
    session's credential file (``only``: to those of them alone, the
    sessions a machine's sign-in is owed, ``token_fanout.machine_signed_in``).
    For credential replacements that bypass the
    rotation chokepoint — the OAuth reconnect exchange writes fresh tokens
    straight to the store, and without a fan-out the bound sessions' files
    keep the pre-exchange token, which the provider may revoke on the grant
    rotation and which 401-recovery (a re-read of the same stale file) can
    never repair. Sync — call via ``asyncio.to_thread``."""
    sub = subscription_store.get_subscription(sub_id)
    if sub:
        _fan_out_rotated_token(sub, only)


def ensure_fresh_and_fan_out(
    sub_id: str, min_runway_ms: int = TURN_MIN_TOKEN_RUNWAY_MS,
    owed: frozenset[str] = frozenset(),
) -> bool:
    """Ensure the subscription's stored token has at least ``min_runway_ms``
    of life, refreshing + fanning out to all live bound sessions if not.
    When the stored token is fresh, every bound session whose file holds an
    older one (it missed a renewal: a push that failed, a machine away, a
    start inside another's renewal) gets the stored token, and only those:
    with a folder per session no neighbour's renewal rewrites it, and the
    five-minute worker reaches every kind of session, a task's and a
    terminal's as a chat's. ``owed``: the sessions a push did not reach
    while their machine stayed signed in (the worker names them,
    ``token_fanout.owed_while_signed_in``). Their stamp does not show it, so
    they join the same list: one list a call, so each session is sent once.

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
    stored_exp = int(oauth.get("expiresAt") or 0)
    if stored_exp and time.time() * 1000 < stored_exp - min_runway_ms:
        with _session_maps_lock:
            lagging = [sid for sid, bound in _session_subscriptions.items()
                       if bound == sub_id and (
                           (_session_token_expiry.get(sid) or stored_exp) < stored_exp or sid in owed)]
        if lagging:
            _fan_out_rotated_token(sub, lagging)
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
                    "OAuth refresh skipped for %s (backoff %ss, attempt %s)",
                    sub_id[:8], wait, attempts
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
                    "OAuth refresh failure for %s discarded — "
                    "credential was replaced mid-attempt", sub_id[:8]
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
            "OAuth refresh failed for %s (attempt %s); %s", sub_id[:8], attempts,
            f"using stored token ({max(0, (expires_at - time.time() * 1000)) / 60000:.0f} min runway)"
            if usable else "no usable token",
        )
        return (token, expires_at) if usable else (None, 0)


#: How often one account renews after a refused sign-in: a renewal ends
#: every other session's token, so the sessions refused by the same revoked
#: token wait for the first one's renewal (it writes their files too).
SIGN_IN_RENEW_GAP_S = 1800
# A runway no token has: the resolver renews at once.
_RENEW_NOW_MS = 10 * 365 * 86_400_000
_sign_in_renewed_at: dict[str, float] = {}


def repair_sign_in(session_id: str) -> str:
    """A turn the vendor refused for the session's sign-in
    (``TurnEnding.sign_in``: the token was revoked, by a sign-out typed in
    another terminal of the account or by the vendor, or the session's file
    still holds a token it missed the renewal of). A session whose file
    holds an older token than the stored one gets the stored one; otherwise
    the account renews now, through the one renewal (``_resolve_oauth_access_token``),
    which writes the new token into every live session of the account, at
    most once per account in ``SIGN_IN_RENEW_GAP_S``, counted from a
    renewal that was made. A refused renewal ends as any failed renewal
    does. Sync; what it did, for the log."""
    with _session_maps_lock:
        sub_id = _session_subscriptions.get(session_id)
        held = _session_token_expiry.get(session_id)
    if not sub_id:
        return "unbound"
    sub = subscription_store.get_subscription(sub_id)
    oauth = (subscription_store.get_credential_data(sub_id) or {}).get("oauth_token") if sub else None
    if not sub or not oauth:
        return "no sign-in"
    stored_exp = int(oauth.get("expiresAt") or 0)
    if held and stored_exp and held < stored_exp:
        _fan_out_rotated_token(sub, [session_id])
        return "pushed"
    now = time.monotonic()
    with _session_maps_lock:
        last = _sign_in_renewed_at.get(sub_id)
        if last is not None and now - last < SIGN_IN_RENEW_GAP_S:
            return "recent"
        _sign_in_renewed_at[sub_id] = now
    token, _exp = _resolve_oauth_access_token(sub, oauth, min_runway_ms=_RENEW_NOW_MS)
    renewed = bool(token) and token != oauth.get("accessToken")
    if not renewed:
        # No renewal was made (the resolver's own back-off, a passing
        # failure): the gap starts at one that was. The resolver's back-off
        # bounds the calls to the vendor meanwhile.
        with _session_maps_lock:
            if _sign_in_renewed_at.get(sub_id) == now:
                _sign_in_renewed_at.pop(sub_id, None)
    logger.info("Pool: a sign-in of %s was refused; account %s %s", session_id[:8], sub_id[:8],
                "renewed" if renewed else "not renewed")
    return "renewed" if renewed else "failed"


def repair_sign_in_soon(session_id: str) -> None:
    """``repair_sign_in`` off the turn's path: the doors that end a turn
    (``pool_limits.follow_ending``, a terminal's ``_after_ending``) call this.
    A thread of its own, as the freshness worker's: a renewal may wait on the
    vendor, which the database's workers must never do."""
    import asyncio

    from core.background import spawn
    spawn(asyncio.to_thread(repair_sign_in, session_id), name="repair_sign_in", owner="pool_tokens")


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
            "OAuth subscription %s expired: %s. The owner "
            "must reconnect the same account to revive it.", sub_id[:8], reason
        )
    except psycopg.Error as e:
        logger.error("Failed to auto-expire subscription %s: %s", sub_id[:8], e)


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

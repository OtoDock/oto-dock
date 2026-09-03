"""OAuth subscription grant-health warnings (login-expiry + expired alerts).

Run as one pass of the ~60s registry sweep (``app.py::_registry_sweep_loop``),
self-throttled to 15 min — each pass decrypts every OAuth row's credential
blob, and grant lifetimes are measured in days, not minutes.

For every OAuth subscription row with an owner:

  * ``status=active`` with a known ``refreshTokenExpiresAt`` (the login
    GRANT's own expiry — ~28 days for Claude logins): fire a user-scoped
    **warning** to the owner at ≤72h and ≤24h remaining (most urgent
    threshold only). Rows without the field (grants connected before it was
    stored) stay silent — absence never means "expiring".
  * ``status=expired``: fire once — the refresh path already logged the
    terminal ``invalid_grant``; this is the owner-visible half.

Dedup stamps live INSIDE the encrypted credential blob
(``oauth_token.healthAlerts = {"72h": {"expiry": <ms>, "at": <ms>},
"24h": {...}, "expired": <firedAtMs>}``) — durable across restarts with no
schema change (legacy stamps are bare expiry ints, read as ``at=0``).
Threshold stamps record the grant expiry they fired FOR — but token
rotations recompute ``refreshTokenExpiresAt`` from wall-clock
(subscription_pool's rotation fields), so the "same" expiry drifts by
milliseconds-to-hours across refreshes; exact-match dedup re-fired on every
rotation (2026-09-01: 19 warnings in ~52h for one account). A fired
threshold therefore re-arms only when the grant expiry moved FORWARD by
``_REARM_DELTA_MS`` (a real renewal — a reconnect pushes ~28 days), with
``_MIN_RENOTIFY_MS`` as an absolute per-key floor backstopping any other
re-arm path. A reconnect rebuilds the blob without stamps (full re-arm).
Stamp writes merge one key into a freshly-read blob under the sub's refresh
lock so neither a concurrent rotation's write nor the sweep's own pre-lock
read can clobber anything.
"""

import asyncio
import logging
import time

from storage import subscription_store

logger = logging.getLogger(__name__)

_MIN_INTERVAL_S = 15 * 60
_last_run: float = 0.0

# Most urgent first — one notification per pass per row, the tighter window wins.
# (An account first seen at remaining≈25h gets the 72h warn, then the 24h warn
# ~1h later — still ≤2 per cycle; the floor below never blocks the normal
# 72h→24h pair, which is ~48h apart.)
_WARN_THRESHOLDS = (("24h", 24 * 3600 * 1000), ("72h", 72 * 3600 * 1000))

# Re-arm a fired threshold only when the grant expiry moved forward at least
# this much (rotation jitter is not a new grant)...
_REARM_DELTA_MS = 24 * 3600 * 1000
# ...and never repeat one key faster than this, whatever the stamps say.
_MIN_RENOTIFY_MS = 20 * 3600 * 1000

_LAYER_PRODUCT = {"claude-code-cli": "Claude", "codex-cli": "ChatGPT"}


async def check_subscription_health() -> None:
    """Sweep every OAuth row and fire due grant-health notifications. Called
    once per ~60s sweep, self-throttled to ``_MIN_INTERVAL_S``. Idempotent."""
    global _last_run
    now = time.monotonic()
    if _last_run and (now - _last_run) < _MIN_INTERVAL_S:
        return
    _last_run = now
    try:
        rows = await asyncio.to_thread(subscription_store.list_subscriptions)
    except Exception:
        logger.exception("subscription_health: failed to enumerate rows")
        return
    for sub in rows:
        if sub.get("auth_type") != "oauth" or not sub.get("owner_sub"):
            continue
        try:
            await _check_row(sub)
        except Exception:
            logger.exception("subscription_health: row %s failed", sub.get("id", "")[:8])


async def _check_row(sub: dict) -> None:
    status = sub.get("status")
    if status not in ("active", "expired"):
        return
    sub_id = sub["id"]
    cred = await asyncio.to_thread(subscription_store.get_credential_data, sub_id)
    oauth = cred.get("oauth_token") or {}
    alerts = dict(oauth.get("healthAlerts") or {})
    product = _LAYER_PRODUCT.get(sub.get("layer", ""), "AI engine")
    label = sub.get("label") or sub.get("oauth_email") or product

    if status == "expired":
        if alerts.get("expired"):
            return
        await _notify(
            sub,
            title=f"{product} login expired",
            body=(
                f"“{label}” can no longer refresh — its login grant expired. "
                f"Reconnect the same account from your AI Engines settings to "
                f"revive it in place."
            ),
        )
        await asyncio.to_thread(_stamp, sub_id, "expired", int(time.time() * 1000))
        return

    grant_expiry = int(oauth.get("refreshTokenExpiresAt") or 0)
    if not grant_expiry:
        return
    now_ms = int(time.time() * 1000)
    remaining_ms = grant_expiry - now_ms
    for key, threshold_ms in _WARN_THRESHOLDS:
        if remaining_ms > threshold_ms:
            continue
        stamped_expiry, stamped_at = _stamp_parts(alerts.get(key))
        if stamped_expiry and (
            grant_expiry - stamped_expiry < _REARM_DELTA_MS
            or now_ms - stamped_at < _MIN_RENOTIFY_MS
        ):
            return  # already warned for this grant generation
        hours = max(remaining_ms, 0) / 3_600_000
        when = (
            f"in about {hours / 24:.0f} day(s)" if hours >= 48
            else f"in about {hours:.0f} hour(s)" if hours >= 1
            else "imminently"
        )
        await _notify(
            sub,
            title=f"{product} login expires soon",
            body=(
                f"The login for “{label}” expires {when}. Reconnect the same "
                f"account from your AI Engines settings before then to avoid "
                f"an interruption."
            ),
        )
        await asyncio.to_thread(
            _stamp, sub_id, key, {"expiry": grant_expiry, "at": now_ms},
        )
        return


async def _notify(sub: dict, *, title: str, body: str) -> None:
    from services.notifications import notification_manager
    await notification_manager.fire_notification(
        title=title,
        body=body,
        severity="warning",
        scope="user",
        target=sub["owner_sub"],
        source="subscription_health",
        source_id=f"{sub['id']}:{title}",
    )


def _stamp_parts(stamp) -> tuple[int, int]:
    """(expiry, firedAt) from a threshold stamp — dict since 2026-09-01,
    bare expiry int before (read as firedAt=0 so only the delta guard
    applies to legacy stamps)."""
    if isinstance(stamp, dict):
        return int(stamp.get("expiry") or 0), int(stamp.get("at") or 0)
    if stamp:
        return int(stamp), 0
    return 0, 0


def _stamp(sub_id: str, key: str, value) -> None:
    """Merge ONE dedup stamp into the credential blob — fresh read-modify-write
    under the sub's refresh lock so neither a concurrent rotation's write nor
    the sweep's own stale pre-lock read gets clobbered."""
    from services.engines import subscription_pool
    with subscription_pool._refresh_lock(sub_id):
        cred = subscription_store.get_credential_data(sub_id)
        oauth = cred.get("oauth_token")
        if not oauth:
            return
        alerts = dict(oauth.get("healthAlerts") or {})
        alerts[key] = value
        oauth["healthAlerts"] = alerts
        subscription_store.update_credential_data(sub_id, cred)

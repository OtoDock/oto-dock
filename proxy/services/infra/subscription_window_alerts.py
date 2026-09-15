"""Owner notifications when an OAuth account's weekly window passes 90 % and
100 % (``services.engines.subscription_windows`` supplies the reading).

One pass of the ~60 s registry sweep (``startup.py::_registry_sweep_loop``),
self-throttled to 5 min. For every active OAuth row with an owner who has
not turned the alerts off (ui-prefs ``subscription_usage_alerts: false``),
each weekly window — the overall one and every per-model one — fires at
most twice per window instance: 90 % as ``info``, 100 % as ``warning``
(a full week locks the account for days). The 5-hour window never
notifies: it turns over several times a day. Dedup rows
(``subscription_window_alerts``) record the window instance an alert was
for; a reading whose reset instant lies within an hour of the stored one
is the same window (the poll's instant jitters across the minute boundary
between polls, the stream events carry epoch seconds), a later one is a
new window and re-arms both thresholds. A reading that jumps past both
marks fires the higher one and stamps both.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from services.engines import subscription_windows as sw
from storage.billing import subscription_store

logger = logging.getLogger(__name__)

_MIN_INTERVAL_S = 5 * 60
_last_run: float = 0.0

# Highest first: one notification per window per pass, the higher mark wins.
_THRESHOLDS = ((100, "warning"), (90, "info"))

_LAYER_PRODUCT = {"claude-code-cli": "Claude", "codex-cli": "ChatGPT"}

PREF_KEY = "subscription_usage_alerts"


async def check_window_alerts(*, now: datetime | None = None) -> int:
    """Sweep every owned OAuth row and fire the due window alerts. Called
    once per ~60 s sweep, self-throttled. Returns the number fired."""
    global _last_run
    mono = time.monotonic()
    if _last_run and (mono - _last_run) < _MIN_INTERVAL_S:
        return 0
    _last_run = mono
    if not sw.is_enabled():
        return 0
    try:
        rows = await asyncio.to_thread(subscription_store.list_subscriptions)
    except Exception:
        logger.exception("window alerts: failed to enumerate rows")
        return 0
    rows = [
        r for r in rows
        if r.get("auth_type") == "oauth" and r.get("status") == "active" and r.get("owner_sub")
    ]
    if not rows:
        return 0
    now = now or datetime.now(timezone.utc)
    try:
        readings = await asyncio.to_thread(sw.latest, [r["id"] for r in rows], now)
    except Exception:
        logger.exception("window alerts: failed to read the samples")
        return 0
    opted_out: dict[str, bool] = {}
    siblings: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r["owner_sub"], r.get("layer") or "")
        siblings[key] = siblings.get(key, 0) + 1
    fired = 0
    for sub in rows:
        reading = readings.get(sub["id"])
        if reading is None:
            continue
        owner = sub["owner_sub"]
        if owner not in opted_out:
            opted_out[owner] = await _opted_out(owner)
        if opted_out[owner]:
            continue
        try:
            fired += await _check_row(
                sub, reading, now,
                others=siblings.get((owner, sub.get("layer") or ""), 1) - 1,
            )
        except Exception:
            logger.exception("window alerts: row %s failed", sub.get("id", "")[:8])
    return fired


async def _opted_out(owner: str) -> bool:
    try:
        from storage.prefs import user_ui_prefs_store
        prefs = await asyncio.to_thread(user_ui_prefs_store.get_prefs, owner)
    except Exception:
        return False
    return prefs.get(PREF_KEY) is False


def _weekly_windows(reading: sw.Windows):
    """(window_key, scoped label or "", pct, resets_at) for every weekly
    window in the reading; windows without a reset instant are skipped (no
    instance to alert on)."""
    if reading.seven_day is not None and reading.seven_day.resets_at is not None:
        yield "seven_day", "", reading.seven_day.pct, reading.seven_day.resets_at
    for s in reading.scoped:
        if s.resets_at is not None:
            yield f"scoped:{s.key}", s.label, s.pct, s.resets_at


# Two reset instants this close are ONE window instance. The vendor's
# instant jitters by a few hundred milliseconds around the minute boundary
# from poll to poll (05:59:59.8 → 06:00:00.4 → 05:59:59.9, seen 2026-09-11),
# which a minute-precision key read as a new window each time and fired
# the same "limit reached" three times in an hour. A real new instance of
# a weekly window is a week away.
_SAME_INSTANCE_S = 3600.0


def _same_instance(stored: str | None, resets_at: datetime) -> bool:
    if not stored:
        return False
    try:
        then = datetime.fromisoformat(stored)
    except ValueError:
        return False
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return abs((resets_at - then).total_seconds()) <= _SAME_INSTANCE_S


async def _check_row(sub: dict, reading: sw.Windows, now: datetime, *, others: int) -> int:
    fired = 0
    for window_key, scoped_label, pct, resets_at in _weekly_windows(reading):
        instance = resets_at.replace(second=0, microsecond=0).isoformat()
        due = [(t, sev) for t, sev in _THRESHOLDS if pct >= t]
        if not due:
            continue
        top_threshold, severity = due[0]
        already = await asyncio.to_thread(
            subscription_store.get_window_alert, sub["id"], window_key, top_threshold)
        if already and _same_instance(already.get("resets_at"), resets_at):
            continue
        await _notify(sub, scoped_label=scoped_label, threshold=top_threshold,
                      severity=severity, pct=pct, resets_at=resets_at, others=others)
        fired += 1
        fired_at = now.isoformat()
        for threshold, _sev in due:
            await asyncio.to_thread(
                subscription_store.upsert_window_alert, sub["id"], window_key, threshold,
                resets_at=instance, fired_at=fired_at,
            )
    return fired


def _owner_when(owner: str, instant: datetime) -> str:
    """The reset instant in the owner's clock: their browser-reported zone,
    else the platform's, else UTC named."""
    tz_name = ""
    try:
        from core.session.session_state import get_user_tz
        tz_name = get_user_tz(owner) or ""
    except Exception:
        tz_name = ""
    if not tz_name:
        try:
            import config as app_config
            tz_name = app_config.get_platform_timezone() or ""
        except Exception:
            tz_name = ""
    try:
        zone = ZoneInfo(tz_name) if tz_name else timezone.utc
    except Exception:
        zone = timezone.utc
    local = instant.astimezone(zone)
    stamp = local.strftime("%a %H:%M")
    return stamp if zone is not timezone.utc else f"{stamp} UTC"


async def _notify(sub: dict, *, scoped_label: str, threshold: int, severity: str,
                  pct: float, resets_at: datetime, others: int) -> None:
    from services.notifications import notification_manager
    product = _LAYER_PRODUCT.get(sub.get("layer", ""), "AI engine")
    label = sub.get("label") or product
    email = sub.get("oauth_email") or ""
    account = f"{label} ({email})" if email and email not in label else label
    when = _owner_when(sub["owner_sub"], resets_at)
    window = f"{scoped_label} weekly limit" if scoped_label else "weekly limit"
    if threshold >= 100:
        title = f"{account}: {window} reached"
        if scoped_label:
            body = (f"{product} refuses {scoped_label} on this account until {when}. "
                    f"Its other models stay available.")
        else:
            body = f"{product} refuses new work on this account until {when}."
    else:
        title = f"{account}: {window} at {threshold}%"
        body = f"{pct:.0f}% used. Resets {when}."
    if others > 0:
        body += " New work goes to your other accounts first."
    await notification_manager.fire_notification(
        title=title,
        body=body,
        severity=severity,
        scope="user",
        target=sub["owner_sub"],
        source="subscription_windows",
        source_id=f"{sub['id']}:{window}:{threshold}",
    )

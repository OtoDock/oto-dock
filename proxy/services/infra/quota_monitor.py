"""Storage-quota measurement + threshold notifications (the soft-tier engine).

Run as one pass of the ~60s registry sweep (``app.py::_registry_sweep_loop``).
For every quota scope it measures usage and fires a 90/95/100 % **warning**
notification when a threshold is newly crossed:

  * usage source = the kernel quota report (O(1)) when the kernel tier is active,
    else a best-effort directory walk (``storage_quota.dir_usage``);
  * dedup + hysteresis via the ``storage_quota_alerts`` table — a threshold fires
    once and only re-arms after usage drops below ``threshold - 5 %`` (no flapping);
  * routing: a ``user`` scope alerts that user; a ``shared`` scope alerts the
    agent's managers + editors (the roles that can write it).

This engine runs whenever a limit is set, independent of the kernel tier — so the
warnings work on any filesystem/deployment, with or without hard enforcement.
"""

import asyncio
import logging
import time

from services.infra import storage_quota
from storage.pg import get_conn
from auth import roles

logger = logging.getLogger(__name__)

_THRESHOLDS = (90, 95, 100)
_REARM_MARGIN = 5  # re-arm threshold T only once usage drops below (T - 5)%
# Storage warnings don't need 60s freshness; throttle so the (potentially
# tree-walking) soft tier re-measures at most this often, not every sweep.
_MIN_INTERVAL_S = 300
_last_run: float = 0.0


# ---------------------------------------------------------------------------
# Entry point (called from the sweep loop)
# ---------------------------------------------------------------------------

def request_recheck() -> None:
    """Clear the throttle so the next ~60s sweep re-measures immediately.

    Called when an admin changes a quota limit, so the change is reflected on
    the next sweep instead of up to ``_MIN_INTERVAL_S`` later."""
    global _last_run
    _last_run = 0.0


async def check_quotas() -> None:
    """Measure every scope and fire/clear threshold warnings. Idempotent.

    Called once per ~60s sweep but self-throttled to ``_MIN_INTERVAL_S`` so the
    soft-tier directory walks don't run every minute."""
    global _last_run
    if not _anything_limited():
        return
    now = time.monotonic()
    if _last_run and (now - _last_run) < _MIN_INTERVAL_S:
        return
    _last_run = now
    try:
        scopes = await asyncio.to_thread(storage_quota.iter_scopes)
    except Exception:
        logger.exception("quota_monitor: failed to enumerate scopes")
        return
    for scope in scopes:
        try:
            await _check_scope(scope)
        except Exception:
            logger.exception("quota_monitor: scope %s failed", scope.scope_key)


def _anything_limited() -> bool:
    """True if any bucket type has a non-zero (enforced) limit — else there is
    nothing to warn about and we skip the (potentially expensive) tree walks."""
    sb, si = storage_quota.limits_for("shared")
    ub, ui = storage_quota.limits_for("user")
    return any(v > 0 for v in (sb, si, ub, ui))


# ---------------------------------------------------------------------------
# Per-scope evaluation
# ---------------------------------------------------------------------------

async def _check_scope(scope: "storage_quota.QuotaScope") -> None:
    limit_bytes, inode_limit = storage_quota.limits_for(scope.scope_type)
    if limit_bytes <= 0 and inode_limit <= 0:
        return  # this bucket type is unlimited
    used_bytes, used_inodes = await _measure(scope)
    if limit_bytes > 0:
        await _evaluate(scope, "bytes", used_bytes, limit_bytes)
    if inode_limit > 0:
        await _evaluate(scope, "inodes", used_inodes, inode_limit)


async def _measure(scope: "storage_quota.QuotaScope") -> tuple[int, int]:
    """``(used_bytes, used_inodes)`` — kernel report if available, else a walk."""
    if storage_quota.hard_enabled():
        pid = await asyncio.to_thread(storage_quota.get_project_id, scope.scope_key)
        if pid is not None:
            rep = await asyncio.to_thread(storage_quota.report_usage, pid)
            if rep is not None:
                return rep
    total_b = total_i = 0
    for d in scope.dirs:
        b, i = await asyncio.to_thread(storage_quota.dir_usage, d)
        total_b += b
        total_i += i
    if scope.scope_type == "user" and scope.owner_sub:
        # A chunked upload in flight is the user's disk too; it lives in the
        # staging area, outside the bucket's directories, until it completes.
        from api.media import uploads
        total_b += await asyncio.to_thread(uploads.staged_bytes_for, scope.owner_sub)
    return (total_b, total_i)


async def _evaluate(scope: "storage_quota.QuotaScope", metric: str,
                    used: int, limit: int) -> None:
    pct = (used / limit) * 100.0 if limit > 0 else 0.0

    # Re-arm (hysteresis): forget any fired threshold the usage has dropped
    # safely below, so it can fire again on a later climb.
    await asyncio.to_thread(_rearm, scope.scope_key, metric, pct)

    crossed = [t for t in _THRESHOLDS if pct >= t]
    if not crossed:
        return
    top = max(crossed)
    recorded = await asyncio.to_thread(_recorded_thresholds, scope.scope_key, metric)
    if top in recorded:
        return  # already warned at (or above) this severity
    await _fire(scope, metric, top, used, limit)
    # Record ALL currently-crossed thresholds so a lower one can't back-fire
    # later without an intervening drop+re-arm.
    await asyncio.to_thread(_record, scope.scope_key, metric, crossed)


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

async def _fire(scope: "storage_quota.QuotaScope", metric: str, threshold: int,
                used: int, limit: int) -> None:
    targets = await asyncio.to_thread(_targets_for, scope)
    if not targets:
        return
    title, body = _message(scope, metric, threshold, used, limit)
    from services.notifications import notification_manager
    for sub in targets:
        try:
            await notification_manager.fire_notification(
                title=title,
                body=body,
                severity="warning",
                scope="user",
                target=sub,
                source="storage_quota",
                source_id=f"{scope.scope_key}:{metric}:{threshold}",
                agent_slug=scope.agent_slug,
            )
        except Exception:
            logger.exception("quota_monitor: notify %s failed", sub)


def _targets_for(scope: "storage_quota.QuotaScope") -> list[str]:
    """Subs to notify: the owner for a user scope; the workspace tier (those
    who write the shared workspace) for shared."""
    if scope.scope_type == "user":
        return [scope.owner_sub] if scope.owner_sub else []
    from storage import database
    return [
        u["sub"]
        for u in database.get_agent_users_with_profile(scope.agent_slug)
        if roles.can_write_workspace(u.get("agent_role")) and u.get("sub")
    ]


def _message(scope: "storage_quota.QuotaScope", metric: str, threshold: int,
             used: int, limit: int) -> tuple[str, str]:
    if scope.scope_type == "shared":
        where = "shared workspace"
    elif scope.scope_type == "external":
        where = "callers' folders"
    else:
        where = f"personal folder ({scope.username})"
    if metric == "bytes":
        amount = f"{_fmt_bytes(used)} of {_fmt_bytes(limit)}"
        noun = "storage"
    else:
        amount = f"{used:,} of {limit:,} files"
        noun = "file-count"
    if threshold >= 100:
        title = f"Storage full — {scope.agent_slug}"
        body = (f"The {where} {noun} quota is full ({amount}). "
                f"New writes will fail until space is freed.")
    else:
        title = f"Storage {threshold}% full — {scope.agent_slug}"
        body = f"The {where} is at {threshold}% of its {noun} quota ({amount})."
    return title, body


def _fmt_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit in ("B", "KB") else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


# ---------------------------------------------------------------------------
# Dedup state (storage_quota_alerts)
# ---------------------------------------------------------------------------

def _recorded_thresholds(scope_key: str, metric: str) -> set[int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT threshold FROM storage_quota_alerts WHERE scope_key=%s AND metric=%s",
            (scope_key, metric),
        ).fetchall()
        return {int(r["threshold"]) for r in rows}


def _record(scope_key: str, metric: str, thresholds: list[int]) -> None:
    with get_conn() as conn:
        for t in thresholds:
            conn.execute(
                "INSERT INTO storage_quota_alerts (scope_key, metric, threshold) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (scope_key, metric, int(t)),
            )
        conn.commit()


def _rearm(scope_key: str, metric: str, pct: float) -> None:
    """Delete fired thresholds the usage has dropped at least ``_REARM_MARGIN``
    below, so they can fire again on a later climb (hysteresis)."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM storage_quota_alerts "
            "WHERE scope_key=%s AND metric=%s AND (threshold - %s) > %s",
            (scope_key, metric, _REARM_MARGIN, pct),
        )
        conn.commit()

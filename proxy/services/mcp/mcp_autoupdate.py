"""Weekly automatic MCP updates.

When ``mcp_auto_update_enabled`` is on (default), once a week in a low-traffic
window this applies every available **community-MCP** update:

  * stdio (npm/pypi) MCPs update immediately — running sessions keep their
    already-spawned subprocess, new sessions get the new version (graceful by
    construction), so there is nothing to defer.
  * docker MCPs share one long-lived container that a force-recreate would
    disrupt, so each is updated only when no live session holds it
    (``mcp_updater.mcp_in_use``); busy ones are deferred and re-checked, and if
    still busy after the overall timeout they are skipped this cycle (logged
    ``skipped_in_use``, retried next week — never a failure).

Every per-MCP result is logged (``storage/mcp/mcp_autoupdate_store``) for the admin
run-history card; admins get ONE notification only if something failed, plus
one notice per new source change (``_record_source_change``). On T3
(cloud) docker MCPs are managed centrally and excluded by
``mcp_updater.community_targets`` — the job is a clean no-op for them.

An MCP whose install dir carries ``mcp_updater.HOLD_MARKER`` (``.hold``) is
skipped entirely (logged + recorded ``held``). An install AHEAD of the catalog
(detection ``reason: ahead``) is left alone without a marker: the job never
moves an install backwards; the admin reverts one on purpose from the page.

Scheduling: ``maybe_run_weekly`` is polled every 60s by app.py's registry sweep
loop. It gates on a PERSISTED wall-clock last-run (not an in-memory monotonic
clock, which would re-fire on every restart) plus a weekday/hour window in the
platform timezone, with a catch-up for installs that are down during the window.
The actual run is launched as a background task so the long defer loop never
blocks the sweep loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

import config
from services.mcp import mcp_manifest_types as _mt
from services.mcp import mcp_updater
from storage import database as task_store
from storage.mcp import mcp_autoupdate_store as log_store

logger = logging.getLogger("claude-proxy.mcp-autoupdate")

# Scheduling (module-level so tests can monkeypatch).
LAST_RUN_KEY = "mcp_auto_update_last_run"
WEEKLY_INTERVAL_S = 7 * 86400
CATCHUP_AFTER_S = 8 * 86400        # if we missed the window, run at next tick
RUN_WINDOW_WEEKDAY = 6             # Sunday (Mon=0 .. Sun=6)
RUN_WINDOW_START_HOUR = 3          # 03:00 local
RUN_WINDOW_END_HOUR = 5            # ..04:59 local (exclusive)

# Defer loop for in-use docker MCPs.
DEFER_RECHECK_S = 300              # re-check busy MCPs every 5 min
DEFER_TIMEOUT_S = 4 * 3600         # give up after 4h → skipped_in_use

# Serializes a run against itself (a second weekly tick, or a manual trigger).
_run_lock = asyncio.Lock()

# The first tick after a start settles what a restart interrupted (a source
# switch left in ``switching``, an install backup left beside its folder),
# whatever the auto-update setting says.
_reconciled = False


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _last_run_at() -> datetime | None:
    raw = task_store.get_platform_setting(LAST_RUN_KEY)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _set_last_run(dt: datetime) -> None:
    try:
        task_store.set_platform_setting(LAST_RUN_KEY, dt.isoformat())
    except Exception:
        logger.exception("mcp autoupdate: failed to persist last-run")


def _in_window(now_utc: datetime) -> bool:
    """True if `now` (in the platform timezone) is inside the weekly window."""
    try:
        import zoneinfo
        local = now_utc.astimezone(zoneinfo.ZoneInfo(config.get_platform_timezone()))
    except Exception:
        local = now_utc
    return (
        local.weekday() == RUN_WINDOW_WEEKDAY
        and RUN_WINDOW_START_HOUR <= local.hour < RUN_WINDOW_END_HOUR
    )


def _is_due(now_utc: datetime) -> tuple[bool, bool]:
    """Return (due, overdue). `due` = a week has elapsed since the last run;
    `overdue` = long enough that we should run outside the window (catch-up)."""
    last = _last_run_at()
    if last is None:
        return True, True
    elapsed = (now_utc - last).total_seconds()
    return elapsed >= WEEKLY_INTERVAL_S, elapsed >= CATCHUP_AFTER_S


async def maybe_run_weekly() -> None:
    """Polled every 60s from app.py's registry sweep loop. Launches the weekly
    run as a background task when enabled, due, and inside the window (or
    overdue). Never blocks the caller — the two gate reads and the slot claim
    are store calls, so they run off the loop (run_db): this is a periodic
    drumbeat, exactly the class that froze the proxy on 2026-09-03."""
    from storage.pg import run_db
    global _reconciled
    if not _reconciled:
        from services.community import mcp_source_swap
        try:
            await mcp_source_swap.reconcile_interrupted()
            _reconciled = True
        except Exception:
            logger.exception("mcp autoupdate: the start reconcile failed; retried next tick")
    if _run_lock.locked():
        return
    if not await run_db(mcp_updater.auto_update_enabled):
        return
    now = _now_utc()
    due, overdue = await run_db(_is_due, now)
    if not due:
        return
    if not (overdue or _in_window(now)):
        return
    # Claim the slot up front so the next 60s tick sees "not due" even while the
    # run (incl. the multi-hour defer loop) is still in flight.
    await run_db(_set_last_run, now)
    asyncio.create_task(_run_guarded(trigger="auto"))


async def _run_guarded(trigger: str) -> None:
    try:
        await run_auto_update(trigger=trigger)
    except Exception:
        logger.exception("mcp autoupdate: run failed")


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

async def run_auto_update(trigger: str = "auto") -> dict:
    """Detect + apply available community-MCP updates. Returns a summary dict.

    Holds ``_run_lock`` so two runs never overlap. Each MCP is handled in its own
    try/except — a single failure (network, build) is logged and the run
    continues.
    """
    async with _run_lock:
        run_id = uuid.uuid4().hex
        logger.info("mcp autoupdate run %s starting (trigger=%s)", run_id, trigger)

        targets = {m.name: m for m in mcp_updater.community_targets()}
        try:
            detected = await mcp_updater.detect_available_updates()
        except Exception:
            logger.exception("mcp autoupdate: detection failed")
            detected = {"updates": {}}
        updates = {
            name: info for name, info in (detected.get("updates") or {}).items()
            if name in targets
        }

        counts = {
            log_store.STATUS_UPDATED: 0, log_store.STATUS_NO_CHANGE: 0,
            log_store.STATUS_SKIPPED_IN_USE: 0, log_store.STATUS_FAILED: 0,
            log_store.STATUS_HELD: 0, log_store.STATUS_NEEDS_APPROVAL: 0,
        }
        failed: list[tuple[str, str]] = []  # (mcp_name, error)

        # stdio (npm/pypi) MCPs — update immediately, no defer.
        pending_docker: list[str] = []
        for name, info in updates.items():
            reason = info.get("reason")
            if reason == "source":
                # The catalog source changed: the job never applies it. It is
                # recorded for the run history and the admins are told once
                # per pair; the switch is theirs (mcp_source_swap).
                await _record_source_change(run_id, name, info, targets[name], trigger)
                counts[log_store.STATUS_NEEDS_APPROVAL] += 1
                continue
            if reason == "switched":
                continue
            # A local hold marker excludes the MCP from the automatic converge
            # (out-of-band deploy running ahead of the catalog); recorded in the
            # run history so a forgotten hold stays visible week after week.
            if mcp_updater.is_held(targets[name]):
                logger.warning(
                    "mcp autoupdate: %s is HELD (%s present) — skipping "
                    "converge %s → %s", name, mcp_updater.HOLD_MARKER,
                    info.get("current", "?"), info.get("latest", "?"),
                )
                _record(run_id, name, targets[name].server.runtime,
                        info.get("current", ""), info.get("latest", ""),
                        log_store.STATUS_HELD, "", trigger)
                counts[log_store.STATUS_HELD] += 1
                continue
            if info.get("reason") == "ahead":
                logger.info(
                    "mcp autoupdate: %s installed %s is ahead of the catalog %s; "
                    "left alone", name, info.get("current", "?"),
                    info.get("latest", "?"),
                )
                continue
            if _mt.is_container(targets[name].server):
                pending_docker.append(name)
                continue
            status, err = await _update_and_record(run_id, name, info, targets[name], trigger)
            counts[status] = counts.get(status, 0) + 1
            if status == log_store.STATUS_FAILED:
                failed.append((name, err))

        # docker MCPs — update the free ones now, wait out the busy ones.
        if pending_docker:
            await _process_docker_with_defer(
                run_id, pending_docker, updates, targets, trigger, counts, failed,
            )

        if failed:
            await _notify_failures(run_id, failed)

        logger.info(
            "mcp autoupdate run %s done: %s updated, %s failed, %s skipped(in-use), "
            "%s held, %s needing approval, %s no-change", run_id,
            counts[log_store.STATUS_UPDATED], counts[log_store.STATUS_FAILED],
            counts[log_store.STATUS_SKIPPED_IN_USE], counts[log_store.STATUS_HELD],
            counts[log_store.STATUS_NEEDS_APPROVAL], counts[log_store.STATUS_NO_CHANGE],
        )
        return {"run_id": run_id, "counts": counts}


async def _record_source_change(run_id, name, info, manifest, trigger) -> None:
    """Record a pending source change as ``needs_approval`` and tell the
    admins once per pair: the row carries the mark, and a new pair clears it
    (``mcp_update_state_store.upsert_pending_source_change``). The row is
    taken from the store; when the detection result carries a change the
    store does not (a faked detection, a dismiss racing the run), the row is
    written from it first."""
    from storage.mcp import mcp_update_state_store as state_store
    change = info.get("source_change") or {}
    to_url = (change.get("to") or {}).get("url") or info.get("latest", "")
    _record(run_id, name, manifest.server.runtime, info.get("current", ""),
            to_url, log_store.STATUS_NEEDS_APPROVAL, "", trigger)
    try:
        row = await asyncio.to_thread(state_store.get_source_change, name)
        if row is None:
            src, dst = change.get("from") or {}, change.get("to") or {}
            row = await asyncio.to_thread(state_store.upsert_pending_source_change, name, {
                "from_kind": src.get("kind", ""), "from_identity": src.get("identity", ""),
                "from_url": src.get("url", ""), "from_runtime": src.get("runtime", ""),
                "to_kind": dst.get("kind", ""), "to_identity": dst.get("identity", ""),
                "to_url": dst.get("url", ""), "to_runtime": dst.get("runtime", ""),
                "to_version": dst.get("version", ""),
                "to_manifest_hash": change.get("to_manifest_hash", ""),
                "declared": bool(change.get("declared")), "plan": change.get("plan") or {},
            })
        if row.get("status") != state_store.STATUS_PENDING or row.get("notified_at"):
            return
        await _notify_source_change(name, row)
        await asyncio.to_thread(state_store.mark_source_change_notified, name)
    except Exception:
        logger.exception("mcp autoupdate: the source-change notification of %s failed", name)


async def _notify_source_change(name: str, row: dict) -> None:
    from services.notifications import notification_manager
    declared = bool(row.get("declared"))
    body = (
        f"The catalog source of **{name}** changed from `{row.get('from_url', '')}` "
        f"to `{row.get('to_url', '')}`. "
    )
    body += (
        "The catalog entry declares that it replaces the installed source. "
        if declared else
        "The catalog entry does not say it replaces the installed source: treat "
        "it as a possible compromise and verify the new source before switching. "
    )
    body += ("Automatic updates never apply a source change; review it and switch "
             "from Admin → MCP Servers.")
    await notification_manager.fire_notification(
        title="An MCP's catalog source changed",
        body=body,
        severity="info" if declared else "warning",
        scope="admin",
        source="mcp",
        source_id=f"source-change:{name}",
    )


async def _process_docker_with_defer(
    run_id, pending, updates, targets, trigger, counts, failed,
) -> None:
    """Update free docker MCPs immediately; defer + re-check in-use ones until
    free or the overall timeout, then mark the still-busy as skipped_in_use."""
    deadline = time.monotonic() + DEFER_TIMEOUT_S
    remaining = list(pending)
    while remaining:
        still_busy: list[str] = []
        for name in remaining:
            try:
                busy = await mcp_updater.mcp_in_use(name)
            except Exception:
                logger.exception("mcp autoupdate: in-use check failed for %s", name)
                busy = False  # fail-open: don't perma-defer on a probe error
            if busy:
                still_busy.append(name)
                continue
            status, err = await _update_and_record(
                run_id, name, updates[name], targets[name], trigger,
            )
            counts[status] = counts.get(status, 0) + 1
            if status == log_store.STATUS_FAILED:
                failed.append((name, err))

        if not still_busy:
            return
        if time.monotonic() >= deadline:
            for name in still_busy:
                info = updates[name]
                _record(
                    run_id, name, "docker", info.get("current", ""),
                    info.get("latest", ""), log_store.STATUS_SKIPPED_IN_USE, "", trigger,
                )
                counts[log_store.STATUS_SKIPPED_IN_USE] += 1
                logger.info("mcp autoupdate: %s still in use at timeout — skipped", name)
            return
        remaining = still_busy
        await asyncio.sleep(DEFER_RECHECK_S)


async def _update_and_record(run_id, name, info, manifest, trigger) -> tuple[str, str]:
    """Run one update, log the result, return (status, error)."""
    runtime = manifest.server.runtime
    old_version = info.get("current", "")
    # `reason` (package / manifest / both) is logged for diagnostics only — a
    # manifest-only converge records as UPDATED with old==new version.
    logger.info(
        "mcp autoupdate: updating %s (reason=%s)", name, info.get("reason", "package"),
    )
    try:
        result = await mcp_updater.update_one(name)
    except Exception as e:
        err = str(e)
        logger.warning("mcp autoupdate: %s failed: %s", name, err)
        _record(run_id, name, runtime, old_version, info.get("latest", ""),
                log_store.STATUS_FAILED, err, trigger)
        return log_store.STATUS_FAILED, err

    if result.get("status") == "updated":
        new_version = result.get("version", info.get("latest", ""))
        _record(run_id, name, runtime, result.get("old_version", old_version),
                new_version, log_store.STATUS_UPDATED, "", trigger)
        return log_store.STATUS_UPDATED, ""

    # already_latest / anything else — no change (e.g. version moved between
    # detection and update).
    _record(run_id, name, runtime, old_version, result.get("version", old_version),
            log_store.STATUS_NO_CHANGE, "", trigger)
    return log_store.STATUS_NO_CHANGE, ""


def _record(run_id, name, runtime, old_version, new_version, status, error, trigger) -> None:
    try:
        log_store.record_result(
            run_id, name, runtime=runtime, old_version=old_version,
            new_version=new_version, status=status, error=error, trigger=trigger,
        )
    except Exception:
        logger.exception("mcp autoupdate: failed to log result for %s", name)


async def _notify_failures(run_id: str, failed: list[tuple[str, str]]) -> None:
    """Fire ONE failure-only notification to admins (no success notification)."""
    from services.notifications import notification_manager

    names = ", ".join(f"**{n}**" for n, _ in failed)
    body = (
        f"Automatic update failed for {len(failed)} MCP(s): {names}. "
        "Their catalog/manifest was rolled back; you can retry from "
        "Admin → MCP Servers."
    )
    first_err = (failed[0][1] or "")[:300]
    if first_err:
        body += f"\n\n```\n{first_err}\n```"
    try:
        await notification_manager.fire_notification(
            title="Automatic MCP update failed",
            body=body,
            severity="warning",
            scope="admin",
            source="mcp",
            source_id=f"autoupdate:{run_id}",
        )
    except Exception:
        logger.exception("mcp autoupdate: failure notification failed")

"""Sync-delta telemetry: alert-only signal that a build tree escaped
``SYNC_IGNORE_RULES`` (1.5 sync-ignore design round).

Two sources feed it:
  * the RECONCILE merge (``remote_workspace_sync``) — planned pull-side NEW
    files (no base row) in one sync;
  * the PER-TURN applier (``satellite_file_transfer._apply_file_changed``) —
    satellite-authored writes applied inside a rolling window.

Crossing ``config.SYNC_DELTA_ALERT_FILES`` / ``_BYTES`` fires ONE admin
notification naming the largest first-level subtrees. It is strictly
observational: sync NEVER pauses or filters on it — when we don't know, we
sync, and this is how we learn which ecosystems the rule table misses.
Rate-limited in-process per (machine, agent) — a proxy restart may repeat
one alert (accepted). Alert text carries workspace-relative subtree names
only — same admin exposure class as machine offline/online alerts.
"""

import asyncio
import logging
import time

logger = logging.getLogger("claude-proxy.satellite")

_ALERT_INTERVAL_S = 6 * 3600

# (machine_id, agent_slug) → last alert monotonic-ish wall time.
_last_alert: dict[tuple[str, str], float] = {}

# (machine_id, agent_slug) → [window_start, files, bytes, {subtree: files}]
_TURN_WINDOW_S = 600.0
_turn_counters: dict[tuple[str, str], list] = {}


def _top_subtree(rel_path: str) -> str:
    parts = rel_path.split("/")
    return "/".join(parts[:2]) if len(parts) > 1 else (parts[0] or "?")


def record_turn_write(machine_id: str, agent_slug: str, rel_path: str,
                      size: int) -> "tuple[int, int, dict] | None":
    """Count one applied satellite-authored write. Returns the window's
    ``(files, bytes, subtree_counts)`` when a threshold is NEWLY crossed
    (caller then fires the alert), else None. Cheap and synchronous —
    called on the hot per-turn apply path."""
    import config as _cfg
    key = (machine_id, agent_slug)
    now = time.time()
    win = _turn_counters.get(key)
    if win is None or now - win[0] > _TURN_WINDOW_S:
        win = [now, 0, 0, {}]
        _turn_counters[key] = win
    win[1] += 1
    win[2] += max(0, int(size or 0))
    sub = _top_subtree(rel_path)
    win[3][sub] = win[3].get(sub, 0) + 1
    crossed_now = (win[1] == _cfg.SYNC_DELTA_ALERT_FILES
                   or (win[2] >= _cfg.SYNC_DELTA_ALERT_BYTES
                       and win[2] - max(0, int(size or 0))
                       < _cfg.SYNC_DELTA_ALERT_BYTES))
    if crossed_now:
        return win[1], win[2], dict(win[3])
    return None


async def maybe_alert(machine_id: str, agent_slug: str, *, source: str,
                      new_files: int, new_bytes: int,
                      subtree_counts: dict) -> None:
    """Fire the rate-limited admin notification if thresholds are met.
    Never raises; never affects the sync that reported the numbers."""
    try:
        import config as _cfg
        if (new_files < _cfg.SYNC_DELTA_ALERT_FILES
                and new_bytes < _cfg.SYNC_DELTA_ALERT_BYTES):
            return
        key = (machine_id, agent_slug)
        now = time.time()
        if now - _last_alert.get(key, 0.0) < _ALERT_INTERVAL_S:
            return
        _last_alert[key] = now

        from core.remote.satellite_connection import _list_admin_subs
        from services.notifications import notification_manager
        from storage import remote_store

        machine = await asyncio.to_thread(
            remote_store.get_remote_machine, machine_id)
        machine_name = (machine or {}).get("name") or machine_id[:8]
        top = sorted(subtree_counts.items(), key=lambda kv: -kv[1])[:3]
        top_txt = ", ".join(f"{name} ({count} files)" for name, count in top)
        if new_bytes >= 1024 * 1024:
            size_txt = f"~{new_bytes / (1024 * 1024):.0f}MB"
        else:
            size_txt = f"~{max(1, new_bytes // 1024)}KB"
        title = f"Large workspace sync: {agent_slug} on {machine_name}"
        body = (
            f"A {source} sync moved {new_files} new files "
            f"({size_txt}) from '{machine_name}' for "
            f"agent '{agent_slug}'. Largest subtrees: {top_txt or 'n/a'}. "
            f"If this is generated build output, it may belong in the "
            f"platform's sync-ignore rule table (SYNC_IGNORE_RULES). "
            f"Sync completed normally — this is informational."
        )
        admin_subs = await asyncio.to_thread(_list_admin_subs)
        for sub in admin_subs:
            try:
                await notification_manager.fire_notification(
                    title=title, body=body, severity="info",
                    scope="user", target=sub,
                    source="satellite", source_id=machine_id,
                )
            except Exception:
                logger.exception(
                    "Failed to fire sync-delta notification to %s", sub[:16])
        logger.info(
            "sync-delta alert: machine=%s agent=%s source=%s files=%d bytes=%d",
            machine_id[:8], agent_slug, source, new_files, new_bytes)
    except Exception:
        logger.debug("sync-delta alert failed", exc_info=True)

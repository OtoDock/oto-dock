"""The registry's periodic sweeps: the table of what runs each tick, the runner
that bounds them, and the loop the boot starts.
"""

import asyncio
import logging

import config
from storage import pg as pg_pool

logger = logging.getLogger("otodock")

_SWEEP_CONCURRENCY = 4
_sweeps_running: dict[str, asyncio.Task] = {}


async def run_sweep_items(items) -> list[str]:
    """One tick of the 60 s sweep: every item not still running from the
    last tick, ``_SWEEP_CONCURRENCY`` at a time, each failure logged on its
    own. Returns the names that ran once they all ended (the loop does not
    wait for that: a tick is its own task)."""
    slot = asyncio.Semaphore(_SWEEP_CONCURRENCY)
    ran: list[str] = []

    async def _one(name: str, fn) -> None:
        async with slot:
            try:
                await fn()
            except Exception:
                logger.exception("%s failed", name)

    tasks = []
    for name, fn in items:
        previous = _sweeps_running.get(name)
        if previous is not None and not previous.done():
            logger.warning("%s still running from the last tick; skipped", name)
            continue
        ran.append(name)
        task = asyncio.create_task(_one(name, fn))
        _sweeps_running[name] = task
        tasks.append(task)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return ran


def _purge_mcp_build_outputs() -> None:
    """The session MCP config build outputs are rebuilt per session; what an
    earlier release left there carried vendor bearers inline, so the two
    directories are emptied at every boot (the gateway descriptors beside
    them are kept: a re-adoption reads them)."""
    import shutil
    for name in ("user-mcp-configs", "sse-mcp-configs"):
        shutil.rmtree(config.SESSIONS_DIR / name, ignore_errors=True)


# The 60 s sweep's items, each isolated (one that raises is logged and
# the next runs) and run concurrently, _SWEEP_CONCURRENCY at a time: a
# slow one no longer pushes the rest back, and an item still running
# from the last tick is skipped (a hung sweep holds one slot, never
# four, and the fingerprint merge never runs twice). Every store touch
# goes through run_db (storage/pg.py's event-loop rule): a periodic
# DELETE + COMMIT on the loop is exactly what froze the proxy on
# 2026-09-03 when the disk went slow; the two file sweeps run in a
# worker thread for the same reason.

async def _sweep_media():
    from services.media import media_pipeline as _mp
    from storage import database as _db
    await asyncio.to_thread(_mp.sweep_host_media_cache)      # TTL satellite-host media
    expired = await pg_pool.run_db(_db.sweep_expired_media_tokens)  # reap expired workspace tokens
    await asyncio.to_thread(_mp.remove_cache_copies, expired)


async def _sweep_preview_snapshots():
    # Version-pinned preview snapshots: reap dirs of deleted chats
    # (internally throttled to hourly).
    from services.media import preview_snapshots as _psnap
    await asyncio.to_thread(_psnap.sweep_orphans)


async def _sweep_recover_bin():
    # Workspace Recover Bin: reap entries past their 7-day TTL
    # (DB rows + on-disk bytes). Quick indexed delete, usually 0.
    from storage.files import recover_bin_store as _rbstore
    await pg_pool.run_db(_rbstore.delete_expired)


async def _sweep_tombstones():
    # File-sync delete tombstones: reap past their 30-day TTL (an
    # offline satellite is assumed long-since caught up). Indexed delete.
    from storage.files import file_tombstones_store as _tstore
    await pg_pool.run_db(_tstore.delete_expired)


async def _sweep_idle_fingerprints():
    # Fingerprint-gated periodic idle sync — for each connected
    # satellite whose agent tree changed OUT-OF-TURN (no active session),
    # run the merge so it (and the dashboard) catches up without waiting
    # for the next session. No-op for quiet / pre-0.5.32 satellites.
    from core.session.session_manager import _get_remote_layer as _grl
    _layer = _grl()
    if _layer is not None:
        await _layer.run_idle_fingerprint_sweep()


async def _sweep_retention():
    # Session retention + disk cleanup: ages out LOCAL chats' on-disk
    # session files (admin knob; chats reseed from DB history via
    # #11), reaps orphaned session files + Codex telemetry junk, and
    # runs the MCP tarball-cache GC. Internally gated to once/24h.
    from services.infra import retention as _retention
    await _retention.maybe_run_daily()


async def _sweep_quotas():
    # Storage quotas: measure each agent's shared + per-user buckets
    # and fire the 90/95/100% warning notifications (with hysteresis).
    # Cheap when no limit is set (early-returns); uses the kernel quota
    # report when enforcing, else a throttled tree walk.
    from services.infra import quota_monitor as _qm
    await _qm.check_quotas()


async def _sweep_subscription_health():
    # OAuth grant health: warn owners before a login grant's
    # finite lifetime lapses (72h/24h out) and once when a row
    # flips to expired. Self-throttled to 15 min; dedup stamps
    # persist inside the credential blob.
    from services.infra import subscription_health as _sub_health
    await _sub_health.check_subscription_health()


async def _sweep_window_alerts():
    # Provider windows: tell an account's owner when a weekly
    # window passes 90 % and when it is reached. Self-throttled
    # to 5 min; dedup rows persist per window instance.
    from services.infra import subscription_window_alerts as _win_alerts
    await _win_alerts.check_window_alerts()


async def _sweep_mcp_autoupdate():
    # Automatic MCP updates: once a week in a low-traffic window,
    # apply available community-MCP updates (deferring in-use docker
    # MCPs). Persisted wall-clock gate; launches the run as its own
    # task so the multi-hour defer loop never blocks this sweep.
    from services.mcp import mcp_autoupdate as _mcp_autoupdate
    await _mcp_autoupdate.maybe_run_weekly()


async def _sweep_agent_updates():
    # Community agent templates: once a day, the managers of an
    # agent whose template has a newer catalog version hear it,
    # once per version (the update itself is theirs to press).
    from services.community import community_agent_updater as _agent_updater
    await _agent_updater.maybe_notify_updates()


async def _sweep_catalog_installs():
    from core.credentials import (
        catalog_install_registry as _catalog_install_registry,
    )
    await _catalog_install_registry.sweep_stale()


async def _sweep_tools_idle():
    # A Docker tool's container put to sleep after its quiet spell.
    from services.containers import idle
    await idle.sweep_once()


def _sweep_items(_warmup_registry, _install_registry, _transfer_registry) -> list:
    """The 60 s sweep's items: the registries' stale sweeps and the store and
    disk sweeps above, in the order they run."""
    sweep_items = [
        ("warmup_registry sweep", _warmup_registry.sweep_stale),
        ("install_registry sweep", _install_registry.sweep_stale),
        ("transfer_registry sweep", _transfer_registry.sweep_stale),
        ("catalog_install_registry sweep", _sweep_catalog_installs),
        ("media cache sweep", _sweep_media),
        ("preview snapshot sweep", _sweep_preview_snapshots),
        ("recover-bin reap", _sweep_recover_bin),
        ("tombstone reap", _sweep_tombstones),
        ("idle fingerprint sweep", _sweep_idle_fingerprints),
        ("retention sweep", _sweep_retention),
        ("quota monitor sweep", _sweep_quotas),
        ("subscription health sweep", _sweep_subscription_health),
        ("subscription window alert sweep", _sweep_window_alerts),
        ("mcp auto-update sweep", _sweep_mcp_autoupdate),
        ("agent template update sweep", _sweep_agent_updates),
        ("docker tools idle sweep", _sweep_tools_idle),
    ]
    return sweep_items


# The 60 s sweep's ticks in flight: a tick holds itself here until it ends.
_sweep_ticks: set[asyncio.Task] = set()


async def _registry_sweep_loop(sweep_items: list, ticks: set[asyncio.Task]) -> None:
    """One tick a minute, each its own task."""
    # A tick is started, not awaited: an item that hangs is skipped at
    # the next tick and holds nothing of it (each tick has its own
    # slots), instead of holding every later tick back.
    while True:
        await asyncio.sleep(60)
        tick = asyncio.create_task(run_sweep_items(sweep_items))
        ticks.add(tick)
        tick.add_done_callback(ticks.discard)

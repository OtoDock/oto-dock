"""Phone-server health + drift worker — background asyncio task.

Periodically probes every phone server's adapter for health (persisted to the
``last_health_*`` columns → the dashboard pill badge) and, for verified servers
whose adapter can enumerate its routes, reconciles the provider's provisioned
routes against the DB. A mismatch flips the server to ``bootstrap_status='drift'``
so the admin can re-verify.

Lifecycle mirrors ``oauth_refresh_worker``: started from ``app.py`` lifespan,
cancelled before session drain. Manual/stub adapters return ``None`` from
``list_provisioned_routes`` → no drift work; drift becomes meaningful with the
FreePBX adapter (P4b).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone

logger = logging.getLogger("claude-proxy.phone-health-worker")

_INTERVAL_SECONDS = 300
_MAX_CONCURRENCY = 5

# Module-level handle so app.py can cancel it cleanly.
_worker_task: asyncio.Task | None = None


def start_worker() -> asyncio.Task:
    """Spawn the background health/drift task. Idempotent."""
    global _worker_task
    if _worker_task and not _worker_task.done():
        return _worker_task
    _worker_task = asyncio.create_task(_health_loop(), name="phone-health-worker")
    logger.info("Phone health worker started (interval=%ds)", _INTERVAL_SECONDS)
    return _worker_task


async def stop_worker() -> None:
    """Cancel + await the worker. Idempotent; safe during shutdown."""
    global _worker_task
    if not _worker_task:
        return
    _worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await _worker_task
    _worker_task = None
    logger.info("Phone health worker stopped")


async def _health_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_INTERVAL_SECONDS)
            await run_health_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Phone health tick failed (continuing)")


async def run_health_tick() -> None:
    """One pass: health-check every server + drift-check the verified ones.

    Public (no sleep) so it can be driven directly from a test or an admin
    "refresh all" action.
    """
    from storage import phone_server_store

    servers = await asyncio.to_thread(phone_server_store.get_all_servers)
    if not servers:
        return
    sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def _one(server: dict) -> None:
        async with sem:
            try:
                await _reconcile_server(server)
            except Exception:
                logger.exception("Health/drift check failed for server %s", server.get("id"))

    await asyncio.gather(*(_one(s) for s in servers))


async def _reconcile_server(server: dict) -> None:
    """Health-probe one server and, if verified + enumerable, drift-check it."""
    from services.phone import phone_adapters
    from storage import phone_route_store, phone_server_store

    adapter = await asyncio.to_thread(phone_adapters.load_adapter, server)

    # 1. Health probe (all servers) → persist health columns.
    try:
        status = await adapter.health_check()
    except phone_adapters.PhoneAdapterError as e:
        status = phone_adapters.HealthStatus(healthy=False, detail=e.message)
    await asyncio.to_thread(
        phone_server_store.update_server, server["id"],
        {
            "last_health_check": datetime.now(timezone.utc).isoformat(),
            "last_health_status": "healthy" if status.healthy else "unhealthy",
            "last_health_detail": status.detail,
        },
    )

    # 2. Drift reconciliation — only for verified servers whose adapter can
    #    enumerate its routes (manual/stub return None → skip).
    if server.get("bootstrap_status") != "verified":
        return
    try:
        handles = await adapter.list_provisioned_routes()
    except phone_adapters.PhoneAdapterError as e:
        logger.warning("Drift list failed for server %s: %s", server["id"], e)
        return
    if handles is None:
        return

    all_routes = await asyncio.to_thread(phone_route_store.get_all_routes)
    db_dids = {
        r["did"] for r in all_routes
        if r.get("phone_server_id") == server["id"]
        and r.get("direction") == "inbound" and r.get("did")
    }
    pbx_dids = {h.did for h in handles if h.did}
    if db_dids == pbx_dids:
        return

    db_only = sorted(db_dids - pbx_dids)
    pbx_only = sorted(pbx_dids - db_dids)
    detail = (
        f"drift detected — missing on PBX: {db_only or '—'}; "
        f"orphan on PBX: {pbx_only or '—'}"
    )
    log = _append_log(server.get("bootstrap_log", ""), detail)
    await asyncio.to_thread(
        phone_server_store.update_server, server["id"],
        {"bootstrap_status": "drift", "bootstrap_log": log},
    )
    logger.warning("Phone server %s drift: %d missing / %d orphan on PBX",
                   server["id"], len(db_only), len(pbx_only))


def _append_log(existing: str, line: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    return (((existing or "") + f"\n[{stamp}] {line}").strip())[-4000:]

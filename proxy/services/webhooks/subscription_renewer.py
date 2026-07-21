"""Background worker that renews vendor webhook subscriptions before they expire.

Subscriptions for vendors like MS Graph and Zoom carry a server-side TTL
(MS Graph: 3 days max). Without renewal, the vendor stops sending events.
This worker periodically scans for subscriptions whose ``expires_at`` is
within a configurable lead time and calls the manifest's
``registration.renew`` template to extend them.

Lifecycle mirrors ``oauth_refresh_worker.py`` exactly:
  * Started from ``proxy/app.py`` lifespan AFTER ``mcp_registry.scan_manifests()``
  * Cancelled BEFORE ``_shutdown_sessions``
  * Module-global ``_worker_task`` handle; ``start_worker``/``stop_worker``
    are both idempotent.

Failure modes:
  * Vendor 401 (token revoked) → row → ``renew_failed``, last_error set.
    User must reconnect.
  * Vendor 404 (vendor lost the subscription) → row → ``renew_failed``.
    User must recreate via dashboard.
  * Network error → leave the row alone, retry on next tick.

Single-replica only for v1.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone

from services.webhooks import subscription_manager
from storage import webhook_subscription_store

logger = logging.getLogger("claude-proxy.subscription-renewer")

# Cadence of the loop. Sized to MS Graph's 3-day TTL with 24h lead time —
# 5 min cadence = 288 wake-ups per 24h, plenty of opportunities to renew.
_INTERVAL_SECONDS = 300

# Wake renewal when expires_at is within this many seconds of now.
_RENEW_LEAD_TIME = 24 * 60 * 60  # 24 hours

# Module-level handle so app.py can cancel cleanly on shutdown.
_worker_task: asyncio.Task | None = None


def start_worker() -> asyncio.Task:
    """Spawn the renewer task. Idempotent."""
    global _worker_task
    if _worker_task and not _worker_task.done():
        return _worker_task
    _worker_task = asyncio.create_task(_renew_loop(), name="subscription-renewer")
    logger.info(
        "subscription renewer started (interval=%ds, lead=%ds)",
        _INTERVAL_SECONDS, _RENEW_LEAD_TIME,
    )
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
    logger.info("subscription renewer stopped")


async def _renew_loop() -> None:
    """Main loop. Sleeps first so the proxy can finish boot before the
    first tick (matches oauth_refresh_worker.py)."""
    while True:
        try:
            await asyncio.sleep(_INTERVAL_SECONDS)
            await _renew_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("renew tick failed (continuing)")


async def _renew_tick() -> None:
    """Find subscriptions due for renewal and process each."""
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        rows = webhook_subscription_store.list_due_for_renewal(
            now_iso, _RENEW_LEAD_TIME,
        )
    except Exception:
        logger.exception("renewer query failed")
        return
    if not rows:
        return
    logger.info("renewing %d subscription(s) due before %s",
                len(rows), _humanize_lead())
    for row in rows:
        sid = row["id"]
        try:
            await subscription_manager.renew_subscription(sid)
            logger.info("renewed subscription %s (provider=%s mcp=%s target=%s)",
                        sid, row.get("provider_id"), row.get("mcp_name"),
                        row.get("vendor_target"))
        except subscription_manager.VendorAPIError as e:
            logger.warning(
                "renew failed for subscription %s (provider=%s): vendor %s — %s",
                sid, row.get("provider_id"), e.vendor_status,
                (e.vendor_body or "")[:200],
            )
        except Exception:
            logger.exception("renew raised for subscription %s", sid)


def _humanize_lead() -> str:
    hours = _RENEW_LEAD_TIME // 3600
    return f"{hours}h from now"

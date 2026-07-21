"""License liveness worker — background asyncio task that binds a
subscription/lifetime key to this install (activation) and then checks in with
the relay on a weekly cadence, caching the verdict for the offline state machine
in ``auth/license.get_current_license``.

Design (see the ``auth/license.py`` docstring):

* **Starts only when connected + self-hosted** — ``relay_client.is_available()``
  AND not ``OTODOCK_CLOUD``. While the relay is unbuilt (today) it never starts,
  so it stays dormant; cloud uses the control plane, not a customer key.
* **Cadence is elapsed-based, NOT ``sleep(7d)``** — the loop wakes hourly and
  acts only when ``now - license_last_check_at >= 7d`` (read from the DB), so it
  **survives restarts** (a fixed long sleep would reset every boot and never fire).
* **Activate-then-check** — a subscription/lifetime key with no valid receipt is
  activated first (retried every tick until it sticks); once bound, a
  subscription key checks in weekly; a bound lifetime key never re-checks
  (perpetual, no liveness).
* **Fail-open** — any relay/network error leaves the last-known-good facts in
  place (the client-side unreachable-grace in ``get_current_license`` decides
  when a prolonged outage finally lapses). The worker never blocks anything.
* **Shared lock** — the periodic tick and the manual ``/v1/admin/license/recheck``
  endpoint both run ``_do_check_under_lock`` behind one module ``asyncio.Lock``.

Stub era: ``activate_license`` / ``license_check`` raise ``RelayNotConfigured``
until the relay ships, so the worker doesn't start (``is_available()`` is False)
and a forced re-check is a clean no-op.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone

import config
from services.billing import relay_client

logger = logging.getLogger("claude-proxy.license-check-worker")

# How often the loop wakes to decide whether a check is due.
_INTERVAL_SECONDS = 3600
# Liveness cadence: re-check an activated subscription key this often.
_CHECK_EVERY_DAYS = 7

_worker_task: asyncio.Task | None = None
# Serializes the periodic tick against the manual /recheck endpoint.
_lock = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_worker() -> asyncio.Task | None:
    """Spawn the background worker, IFF connected + self-hosted. Idempotent.

    Returns None (and does not start) when ``OTODOCK_CLOUD`` is set or the relay
    isn't configured — there's nothing to activate/check against.
    """
    global _worker_task
    if _worker_task and not _worker_task.done():
        return _worker_task
    if config.OTODOCK_CLOUD or not relay_client.is_available():
        logger.info(
            "License check worker not started (cloud=%s, relay_available=%s)",
            config.OTODOCK_CLOUD, relay_client.is_available(),
        )
        return None
    _worker_task = asyncio.create_task(_loop(), name="license-check-worker")
    logger.info(
        "License check worker started (interval=%ds, check_every=%dd)",
        _INTERVAL_SECONDS, _CHECK_EVERY_DAYS,
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
    logger.info("License check worker stopped")


async def _loop() -> None:
    while True:
        try:
            await asyncio.sleep(_INTERVAL_SECONDS)
            await _do_check_under_lock()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("License check tick failed (continuing)")


async def _do_check_under_lock(force: bool = False) -> None:
    """Public entry — the periodic tick AND the manual /recheck both call this so
    a tick and a manual re-check can never interleave their state writes."""
    async with _lock:
        await _do_check(force)


async def _do_check(force: bool = False) -> None:
    import auth.license as L
    from storage import database as db

    # Dormant unless connected + self-hosted (defensive — also guards /recheck).
    if config.OTODOCK_CLOUD or not relay_client.is_available():
        return
    key = L.get_license_key()
    if not key:
        return
    lic = L.validate_license_key(key)
    if lic is None:
        return
    # Only subscription + lifetime bind/check; offline_term never phones home.
    if not (lic.lifetime or lic.license_mode == "subscription"):
        return

    L._advance_seen_clock()  # anti-rollback floor

    receipt = db.get_platform_setting("license_activation_receipt")
    if not L._receipt_valid(receipt, key):
        # Not bound yet → bind (retried every tick until it sticks — edge case 1).
        await _activate(key)
        return
    if lic.lifetime:
        return  # bound lifetime → perpetual, never re-check (no liveness)
    # subscription, activated → weekly liveness.
    if not force and not _check_due():
        return
    await _check(key)


def _check_due() -> bool:
    import auth.license as L
    from storage import database as db

    last = L._parse_iso(db.get_platform_setting("license_last_check_at"))
    if last is None:
        return True
    return (datetime.now(timezone.utc) - last).total_seconds() >= _CHECK_EVERY_DAYS * 86400


async def _activate(key: str) -> None:
    """Bind the key to this install. The relay returns a signed receipt that the
    offline state machine later verifies (must bind to this key + install_id)."""
    from storage import database as db

    db.set_platform_setting("license_last_check_at", _now_iso())
    try:
        receipt = await relay_client.activate_license(key)
    except relay_client.RelayError as e:
        # activation_limit_reached etc. → stays unactivated (community cap); the
        # admin sees the reason via the activate endpoint. Not fatal.
        logger.info("License activation rejected: %s", getattr(e, "code", e))
        return
    except relay_client.RelayNotConfigured:
        return  # relay not built / unreachable — retry next tick (fail-open)
    except Exception:
        logger.exception("License activation failed (will retry)")
        return
    if isinstance(receipt, str) and receipt:
        db.set_platform_setting("license_activation_receipt", receipt)
        _adopt_key_from_receipt(receipt, key)
    db.set_platform_setting("license_check_status", "active")
    db.set_platform_setting("license_last_ok_at", _now_iso())
    logger.info("License activated + bound to this install")


def _adopt_key_from_receipt(receipt: str, stored_key: str) -> None:
    """If the relay re-issued the key, the activation receipt is signed over the
    NEW key — adopt it so the stored key matches (and the receipt stays valid).
    No-op when unchanged or the receipt won't verify."""
    import auth.license as L

    payload = L.verify_license_token(receipt)
    new_key = (payload or {}).get("license_key", "")
    if new_key and new_key != stored_key:
        L.set_license_key(new_key)
        logger.info("Adopted re-issued license key (via activation receipt)")


async def _check(key: str) -> None:
    """Weekly liveness check for an activated subscription key. Fail-open."""
    from storage import database as db
    import auth.license as L

    db.set_platform_setting("license_last_check_at", _now_iso())
    try:
        result = await relay_client.license_check(key)
    except relay_client.RelayNotConfigured:
        return  # unreachable → unreachable-grace keeps running (fail-open)
    except Exception:
        logger.exception("License check failed (fail-open)")
        return
    result = result if isinstance(result, dict) else {}
    verdict = result.get("status", "")
    # Adopt a re-issued key (expiry refresh / plan change): the relay returns the
    # current key + a receipt signed over it, so storing both keeps the install
    # activated with no gap. No-op when unchanged.
    new_key = result.get("license", "")
    new_receipt = result.get("receipt", "")
    if new_key and new_key != key:
        L.set_license_key(new_key)
        if new_receipt:
            db.set_platform_setting("license_activation_receipt", new_receipt)
        logger.info("Adopted re-issued license key (via liveness check)")
    db.set_platform_setting("license_check_status", verdict)
    db.set_platform_setting("license_last_ok_at", _now_iso())
    logger.debug("License check OK: verdict=%s", verdict or "(none)")

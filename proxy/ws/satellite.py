"""Satellite WebSocket endpoint — accepts connections from satellite daemons.

Protocol:
  1. Satellite connects to /v1/satellite
  2. Sends auth message within 5s: {type: "auth", machine_id, machine_secret, capabilities}
  3. Proxy validates and sends: {type: "auth_result", status: "ok"|"rejected"}
  4. Satellite enters message loop (heartbeats, events, acks)
"""

import asyncio
import contextlib
import json
import logging
import re
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

import config as app_config
from core.remote.satellite_connection import get_connection_manager
from storage import remote_store

logger = logging.getLogger("claude-proxy.satellite-ws")

_AUTH_TIMEOUT_S = 5

# Minimum satellite protocol version the proxy accepts.
#
# Policy (introduced after the 0.4.1 fleet-outage incident):
# This floor is reserved for ACTUAL PROTOCOL BREAKS — new required message
# types the proxy depends on, removed handlers, breaking field changes in
# the wire format. Nice-to-have satellite improvements (e.g. the 0.4.1 uv
# discovery fix) MUST NOT bump this floor: an older satellite that simply
# lacks the improvement is still operationally fine on the wire. Surface
# such improvements via the admin "update available" badge instead.
#
# When a real bump IS warranted (and `auto_update_enabled=True` is set on
# the machine), the auth-time handler in this file pushes the new tarball
# over the existing WS — the satellite hot-swaps via systemd.
#
# History:
# 0.4.0: HTTP-over-WS tunneling. Subprocess hooks + Docker MCP HTTP ride
# the satellite WS. PROXY_URL points at the satellite's loopback — no public
# network path. (Genuine protocol break; bumping from prior was correct.)
# 0.4.1: satellite passes uv_bin to mcp_installer so Python-version-pinned
# MCPs install correctly. NOT a protocol break — bumping to 0.4.1 was a
# mistake that bricked the fleet. Rolled back to 0.4.0.
# 0.5.76: OAuth file delivery — remote Claude sessions authenticate from the
# start payload's ``credentials_json`` (no env token is sent anymore), and
# rotation fan-out arrives as ``credentials_update``. Genuine break: an older
# satellite would spawn Claude OAuth sessions with NO credential at all
# (silent auth failure beats nothing only if it never happens) — auto-update
# converges the fleet; a satellite with auto-update disabled gets the honest
# "update required" reject instead.
MIN_SATELLITE_VERSION = "0.5.76"


# ----------------------------------------------------------------------
# SATELLITE_VERSION_LATEST is derived from satellite/config.py at module
# load so there's ONE source of truth for the version that ships. The
# two-constant arrangement that lived here before drifted into an
# infinite auto-update loop when only one side was bumped: proxy pushes
# tarball → tarball's config.py still has the old version → satellite
# reconnects reporting the old version → proxy pushes again, forever.
#
# We parse the version with a tight regex instead of importing the
# satellite package — satellite/config.py imports psutil + sets signal
# handlers + adjusts process titles, none of which belongs in the proxy
# process. Reading a 30-character version string is sufficient.
#
# Bumping the satellite version is now a one-place edit:
# ``satellite/config.py::SATELLITE_VERSION``. Proxy restart picks it up.
# ----------------------------------------------------------------------

_SATELLITE_CONFIG_PY = (
    Path(__file__).resolve().parent.parent.parent / "satellite" / "config.py"
)
_VERSION_LINE_RE = re.compile(
    r'^\s*SATELLITE_VERSION\s*=\s*[\'"]([^\'"]+)[\'"]\s*$', re.MULTILINE,
)


def _read_satellite_version_from_source() -> str | None:
    """Parse ``SATELLITE_VERSION`` from ``satellite/config.py``.

    Returns ``None`` when the satellite source tree is not part of this
    build — the platform runs without the remote-machines feature
    (pairing and auto-update are disabled; everything else is unaffected).

    Raises ``RuntimeError`` when the tree IS present but the constant
    can't be read — proxy startup fails loudly rather than silently
    serving a wrong "latest" value (which would break auto-update).
    """
    if not _SATELLITE_CONFIG_PY.is_file():
        return None
    try:
        text = _SATELLITE_CONFIG_PY.read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError(
            f"Cannot read satellite source version from {_SATELLITE_CONFIG_PY}: {e}"
        ) from e
    match = _VERSION_LINE_RE.search(text)
    if not match:
        raise RuntimeError(
            f"SATELLITE_VERSION = '...' line not found in {_SATELLITE_CONFIG_PY}"
        )
    return match.group(1)


# The version we offer to satellites that are running anything older.
# Distinct from MIN_SATELLITE_VERSION: this is "what we'd push as an
# update", not "what we hard-reject". Bumping LATEST without bumping MIN
# is the routine path for nice-to-have satellite improvements — old
# satellites can auto-update but aren't bricked. Source of truth lives
# in ``satellite/config.py``. ``None`` = satellite source not in this
# build (see satellite_source_available()).
SATELLITE_VERSION_LATEST = _read_satellite_version_from_source()


def satellite_source_available() -> bool:
    """True when the satellite source tree ships with this build.

    False means the remote-machines feature is unavailable: pairing,
    bootstrap-installer download and satellite auto-update are refused
    with a clear error, and the dashboard hides the feature's UI
    (``remote_machines_available`` on the identity payload).
    """
    return SATELLITE_VERSION_LATEST is not None

# machine_id → target_version we just pushed via update_required. Read on the
# next reconnect to detect a rolled-back update (satellite came back BELOW the
# target) so we (a) notify dashboards via _broadcast_satellite_update_failed
# and (b) do NOT re-push (which would crash-loop the satellite). In-memory: a
# proxy restart loses it and we fall back to re-pushing, which is acceptable.
_pending_pushed_updates: dict[str, str] = {}


def _version_at_least(version: str, minimum: str) -> bool:
    """Compare dotted version strings (major.minor.patch). Missing = too old."""
    if not version:
        return False
    try:
        v_parts = [int(p) for p in version.split(".")[:3]]
        m_parts = [int(p) for p in minimum.split(".")[:3]]
    except (ValueError, AttributeError):
        return False
    while len(v_parts) < 3:
        v_parts.append(0)
    while len(m_parts) < 3:
        m_parts.append(0)
    return v_parts >= m_parts


async def ws_satellite_handler(websocket: WebSocket):
    """WebSocket handler for satellite daemon connections."""
    await websocket.accept()

    # --- Authentication (5s timeout) ---
    try:
        raw = await asyncio.wait_for(
            websocket.receive_text(), timeout=_AUTH_TIMEOUT_S
        )
        msg = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError) as e:
        logger.warning("Satellite auth timeout or invalid JSON: %s", e)
        with contextlib.suppress(Exception):
            await websocket.send_text(json.dumps({
                "type": "auth_result",
                "status": "rejected",
                "reason": "Auth timeout or invalid message",
            }))
        await websocket.close(code=4001, reason="Auth failed")
        return

    if msg.get("type") != "auth":
        await websocket.send_text(json.dumps({
            "type": "auth_result",
            "status": "rejected",
            "reason": "First message must be auth",
        }))
        await websocket.close(code=4001, reason="Expected auth message")
        return

    machine_id = msg.get("machine_id", "")
    machine_secret = msg.get("machine_secret", "")
    capabilities = msg.get("capabilities", {})
    sat_version = msg.get("satellite_version", "")

    if not machine_id or not machine_secret:
        await websocket.send_text(json.dumps({
            "type": "auth_result",
            "status": "rejected",
            "reason": "Missing machine_id or machine_secret",
        }))
        await websocket.close(code=4001, reason="Missing credentials")
        return

    # Machine DB row gone (admin/user deleted it from dashboard).
    # Tell the satellite to self-uninstall via close code 4006 so it
    # cleans up local files instead of looping forever in the reconnect
    # backoff trying with stale credentials.
    machine = remote_store.get_remote_machine(machine_id)
    if not machine:
        logger.info(
            "Satellite %s rejected: machine_id not found — instructing "
            "self-uninstall",
            machine_id[:8],
        )
        await websocket.send_text(json.dumps({
            "type": "auth_result",
            "status": "rejected",
            "reason": "machine_deleted",
            "action": "uninstall",
        }))
        await websocket.close(code=4006, reason="machine_deleted")
        return

    # Reject user-paired satellites trying to reconnect when the
    # admin has disabled the feature. Looking up the machine before
    # verifying the secret is fine — the pairing_scope read is read-only.
    if (machine.get("pairing_scope") or "") != "admin":
        from storage import database as _db
        if _db.get_platform_setting("allow_user_paired_machines") == "0":
            logger.info(
                "Satellite %s rejected: user-paired machines disabled",
                machine_id[:8],
            )
            await websocket.send_text(json.dumps({
                "type": "auth_result",
                "status": "rejected",
                "reason": "User-paired machines are disabled by admin policy.",
            }))
            await websocket.close(code=4005, reason="feature_disabled_by_admin")
            return

    # Verify machine secret
    if not remote_store.verify_machine_secret(machine_id, machine_secret):
        logger.warning("Satellite auth failed for machine %s", machine_id[:8])
        await websocket.send_text(json.dumps({
            "type": "auth_result",
            "status": "rejected",
            "reason": "Invalid credentials",
        }))
        await websocket.close(code=4001, reason="Auth rejected")
        return

    # --- version policy + auto-update push ---
    # After authentication so we never send the tarball to an unknown peer.
    # An older satellite with valid credentials either gets the new tarball
    # pushed (when auto_update_enabled OR pending_update is set) or gets
    # rejected with a "manual update required" reason. The
    # SATELLITE_VERSION_LATEST → SATELLITE_VERSION_LATEST flow handles
    # forward-compat as future versions bump.
    # No satellite source in this build → nothing to offer as an update.
    needs_update = SATELLITE_VERSION_LATEST is not None and not _version_at_least(
        sat_version, SATELLITE_VERSION_LATEST,
    )
    must_reject = not _version_at_least(sat_version, MIN_SATELLITE_VERSION)

    # Rollback detection: if we pushed an update last cycle and the satellite
    # reconnected STILL below that target, its boot guard crash-rolled-back.
    # Notify dashboards (red banner) and skip re-pushing — re-pushing would
    # just crash-loop it. It connects on the old (working) version instead.
    _pushed_target = _pending_pushed_updates.pop(machine_id, "")
    if _pushed_target and not _version_at_least(sat_version, _pushed_target):
        logger.warning(
            "Satellite %s: update → %s rolled back (reconnected on %s); "
            "not re-pushing", machine_id[:8], _pushed_target, sat_version or "unknown",
        )
        try:
            remote_store.record_update_result(
                machine_id,
                error=f"update to {_pushed_target} rolled back to {sat_version}",
            )
            await _broadcast_satellite_update_failed(
                machine_id, machine,
                error=f"Update to {_pushed_target} failed; rolled back.",
                rolled_back_to=sat_version,
            )
        except Exception:
            logger.exception("Failed to record/announce update rollback")
        needs_update = False  # connect on the old version; do not re-push

    if needs_update or must_reject:
        auto_update = bool(machine.get("auto_update_enabled", True))
        pending = bool(machine.get("pending_update", False))
        # A push needs the satellite source tree (the tarball is built from
        # it) — without it, a below-floor satellite falls through to the
        # manual-reject branch instead of a doomed tarball build.
        if (auto_update or pending) and satellite_source_available():
            # Push the new tarball over WS, then close. The satellite
            # extracts, restarts via systemd, and reconnects on the new
            # version. Dashboards are notified by _broadcast_satellite_updating.
            try:
                from api.remote.remote_machines import get_satellite_tarball_with_hash
                import base64 as _b64
                tarball_bytes, expected_sha256 = get_satellite_tarball_with_hash()
                payload = {
                    "type": "update_required",
                    "target_version": SATELLITE_VERSION_LATEST,
                    "tarball_b64": _b64.b64encode(tarball_bytes).decode(),
                    "expected_sha256": expected_sha256,
                    "previous_version": sat_version,
                }
                logger.info(
                    "Satellite %s: pushing update %s → %s (tarball %d bytes)",
                    machine_id[:8], sat_version, SATELLITE_VERSION_LATEST,
                    len(tarball_bytes),
                )
                await websocket.send_text(json.dumps(payload))
                # Broadcast to any attached dashboard WS users.
                await _broadcast_satellite_updating(
                    machine_id, machine, sat_version, SATELLITE_VERSION_LATEST,
                )
                # Remember the target so the next reconnect can detect a rollback.
                _pending_pushed_updates[machine_id] = SATELLITE_VERSION_LATEST
            except Exception as e:
                logger.exception(
                    "Satellite %s: failed to push update: %s",
                    machine_id[:8], e,
                )
                remote_store.record_update_result(
                    machine_id, error=f"push failed: {e}",
                )
            # Close so the satellite applies the update and reconnects.
            await websocket.close(code=4007, reason="updating")
            return
        elif must_reject:
            # Auto-update disabled and version is below the hard floor —
            # this satellite cannot connect until an admin clicks "Update
            # now" (which sets pending_update=True) or re-pairs.
            logger.warning(
                "Satellite %s rejected: version %r < required %s and "
                "auto_update_enabled=False",
                machine_id[:8], sat_version, MIN_SATELLITE_VERSION,
            )
            await websocket.send_text(json.dumps({
                "type": "auth_result",
                "status": "rejected",
                "reason": (
                    f"Satellite version {sat_version or 'unknown'} is older "
                    f"than the proxy's minimum {MIN_SATELLITE_VERSION}, and "
                    f"auto-update is disabled. Ask your admin to click "
                    f"'Update now' in the dashboard."
                ),
            }))
            await websocket.close(code=4001, reason="version too old")
            return
        # needs_update but not must_reject and not auto_update: connect
        # normally. Admin will see "Update available" in the dashboard.

    # Auth succeeded — record the version we just observed.
    try:
        remote_store.set_satellite_version(machine_id, sat_version)
    except Exception:
        logger.exception("Failed to record satellite_version")
    # Include the satellite-host policy in auth_result
    # so the satellite can re-validate file writes locally. Defense in
    # depth — a compromised proxy can't trick a home-only satellite into
    # writing /etc/sudoers because the satellite checks against its own
    # local copy of allow_full_fs before any satellite_host write.
    await websocket.send_text(json.dumps({
        "type": "auth_result",
        "status": "ok",
        "policy": {
            "allow_full_fs": bool(machine.get("allow_full_fs") or False),
            # Device-control consent set, so the
            # satellite can re-check capability grants at tool time.
            "device_grants": sorted(
                remote_store._parse_device_grants(machine.get("device_grants"))
            ),
        },
        # CLI version pins (VERSIONS.md) — the satellite reconciles its installed
        # claude/codex to these on auth, so the fleet runs the EXACT versions the
        # platform verified. Empty value → satellite skips that CLI (fail-open).
        "cli_pins": {
            "claude_code": app_config.PINNED_CLAUDE_CODE_VERSION,
            "codex": app_config.PINNED_CODEX_VERSION,
        },
    }))

    # Clear a deliberate-pause flag on reconnect: a fresh auth means the
    # machine is back (tray Resume, or a reboot), so the sustained-outage
    # evaluator should resume normal monitoring of it.
    if machine.get("paused"):
        try:
            remote_store.set_paused(machine_id, False)
        except Exception:
            logger.exception("Failed to clear paused flag on auth")

    # If the satellite just came back with the new version after an
    # update was in flight, clear the update bookkeeping + notify
    # dashboards. (record_update_result also clears pending_update.)
    prev_version = machine.get("satellite_version") or ""
    if prev_version and prev_version != sat_version:
        try:
            remote_store.record_update_result(
                machine_id, target_version=sat_version, error=None,
            )
            await _broadcast_satellite_updated(machine_id, machine, sat_version)
        except Exception:
            logger.exception("Failed to record/announce satellite_updated")

    # --- Register connection ---
    cm = get_connection_manager()
    conn = await cm.register(
        machine_id, websocket, capabilities, satellite_version=sat_version,
    )

    # --- Message loop ---
    try:
        async for raw in websocket.iter_text():
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await cm.handle_message(machine_id, msg)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("Satellite %s WS error: %s", machine_id[:8], e)
    finally:
        # Identity-guarded: only tear down if WE are still the registered
        # connection — a duplicate reconnect may have replaced us while this
        # socket was draining, and popping blindly would unregister the LIVE
        # connection (proxy shows the machine down, satellite thinks it's up).
        await cm.deregister(machine_id, expected=conn)


# ---------------------------------------------------------------------------
# Broadcast helpers — surface in-flight satellite updates on the
# dashboard WS so users see a banner while their satellite is restarting.
# ---------------------------------------------------------------------------


def _dashboard_recipients_for_machine(machine: dict) -> list[str]:
    """Return user_subs that should see updates for this machine: the
    machine's owner + every admin currently logged in. Agent-assigned
    users are intentionally NOT included for v1 to keep this cheap; they
    can still see the badge on next chat warmup if there's an active
    update on their target machine.
    """
    from storage import database as _db
    subs = set()
    owner = machine.get("registered_by", "")
    if owner:
        subs.add(owner)
    # Admins — best-effort; if listing fails we still notify the owner.
    try:
        for u in _db.list_users() or []:
            if (u.get("role") or "") == "admin":
                subs.add(u["sub"])
    except Exception:
        logger.exception("listing admin users failed")
    return sorted(subs)


def inflight_pushed_updates_for_user(user_sub: str) -> list[dict]:
    """Snapshot of satellite updates the proxy has pushed and is still awaiting a
    reconnect for, scoped to the machines ``user_sub`` may see.

    Used to RECONCILE a (re)connecting dashboard's update banners: the
    ``satellite_updating``/``satellite_updated`` events are transient per-connection
    pushes, so if the dashboard is briefly disconnected during the satellite's
    restart (e.g. a proxy restart drops both, the satellite reconnects on the new
    version while the dashboard is still reconnecting) the ``satellite_updated`` is
    dropped and a stale "updating" banner sticks until a page refresh. On connect
    the dashboard reconciles against this authoritative set — dismissing any
    ``updating`` banner whose machine is no longer mid-update, and surfacing any it
    missed. ``_pending_pushed_updates`` is popped the moment a satellite reconnects
    (success or rollback), so a machine drops out of here exactly when its update
    resolves."""
    out: list[dict] = []
    for mid, target in list(_pending_pushed_updates.items()):
        m = remote_store.get_remote_machine(mid)
        if not m or user_sub not in _dashboard_recipients_for_machine(m):
            continue
        out.append({
            "machine_id": mid,
            "machine_name": m.get("name", ""),
            "from_version": m.get("satellite_version", "") or "",
            "to_version": target,
        })
    return out


async def _push_machine_event(machine: dict, event: dict) -> None:
    """Push an event payload into each recipient's dashboard notify queue.
    The dashboard handler at proxy/ws/dashboard.py::_handle_server_notification
    forwards items with our event types straight to the frontend WS."""
    from services.notifications import notification_manager
    recipients = _dashboard_recipients_for_machine(machine)
    for sub in recipients:
        for conn in notification_manager.get_all_connections(sub):
            try:
                conn.queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "notify queue full for %s — dropping %s",
                    sub[:8], event.get("type"),
                )
            except Exception:
                logger.exception("queue put failed")


async def push_install_event(
    machine_id: str, event: dict, recipients: list[str],
) -> None:
    """Per-user delivery hook for ``core/install_registry``.

    Registered at startup via ``install_registry.set_broadcaster``. Fans an
    MCP-install lifecycle event (install_started / install_mcp_plan /
    install_progress / install_heartbeat / install_verifying / install_done /
    mcp_install_failed / install_failed) out to ``recipients`` — the install's
    participants (the user_subs that warmed this (machine, agent)) — through
    the same per-user notify channel satellite-update events use.

    Scoping to participants is what makes every combination correct:
      * personal (user-paired) machine → only the owning user sees it;
      * admin-shared machine as an agent's default target → the viewer /
        editor / member actually using that agent sees it (NOT every admin);
      * multiple users on one shared install → each participant sees it.

    This replaced the old per-(machine, agent) WS-listener attach, which
    raced and leaked on reconnect: a backgrounded ``pre_warmup`` task kept a
    dead ``_send`` attached for the whole install while the live tab never
    attached, so the install bar stayed invisible until a manual refresh.
    """
    from services.notifications import notification_manager
    for sub in recipients:
        if not sub:
            continue
        for conn in notification_manager.get_all_connections(sub):
            try:
                conn.queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "notify queue full for %s — dropping %s",
                    sub[:8], event.get("type"),
                )
            except Exception:
                logger.exception("queue put failed")


async def _broadcast_satellite_updating(
    machine_id: str, machine: dict, from_version: str, to_version: str,
) -> None:
    """Fired when the proxy is about to push a tarball to a satellite
    whose version is below LATEST. Dashboards render a transient banner
    "Updating <name> to X — reconnects in ~30s" via machineUpdateStore."""
    from datetime import datetime, timezone
    await _push_machine_event(machine, {
        "type": "satellite_updating",
        "machine_id": machine_id,
        "machine_name": machine.get("name", ""),
        "from_version": from_version or "unknown",
        "to_version": to_version,
        "started_at": datetime.now(timezone.utc).isoformat(),
    })


async def _broadcast_satellite_updated(
    machine_id: str, machine: dict, version: str,
) -> None:
    """Fired after a previously-updating satellite reconnects on the new
    version. Dashboards flash a green confirmation, then dismiss."""
    await _push_machine_event(machine, {
        "type": "satellite_updated",
        "machine_id": machine_id,
        "machine_name": machine.get("name", ""),
        "version": version,
    })


async def _broadcast_satellite_update_failed(
    machine_id: str, machine: dict, error: str, rolled_back_to: str,
) -> None:
    """Fired when an update was attempted but the satellite came back on
    the OLD version (rollback path). Dashboards render a sticky red
    banner until dismissed."""
    await _push_machine_event(machine, {
        "type": "satellite_update_failed",
        "machine_id": machine_id,
        "machine_name": machine.get("name", ""),
        "error": error,
        "rolled_back_to": rolled_back_to,
    })

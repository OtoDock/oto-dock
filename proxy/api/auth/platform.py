"""Platform-settings, licensing, concurrency, and SMTP-test endpoints.

Admin-only configuration surface plus the user-paired-disable cascade.
Attaches to the shared core-auth router."""

import asyncio
import logging

from fastapi import Depends, HTTPException
from pydantic import BaseModel

import config
from auth.license import get_current_license, get_license_key, set_license_key
from auth.providers import UserContext, get_current_user, mask_email, require_admin
from storage import database as task_store

from api.auth._router import router
import contextlib

logger = logging.getLogger("claude-proxy")


class PlatformSettingsRequest(BaseModel):
    company_name: str | None = None
    platform_instructions: str | None = None
    platform_timezone: str | None = None
    session_timeout: str | None = None
    session_idle_timeout: str | None = None
    jwt_expiry_hours: str | None = None
    allow_user_paired_machines: bool | None = None
    remote_fallback_user_override: bool | None = None
    remote_fallback_agent_default: bool | None = None
    # Global interactive kill-switch (core/execution_mode.py). Default ON
    # (unset = enabled since R1.5): interactive is opt-out per installation.
    interactive_cli_enabled: bool | None = None
    # Session retention (services/infra/retention.py)
    session_retention_enabled: bool | None = None
    session_retention_days: str | None = None
    # Automatic MCP updates (services/mcp/mcp_autoupdate.py)
    mcp_auto_update_enabled: bool | None = None
    # Storage quotas (services/infra/storage_quota.py) — MB / file-count; 0 = unlimited
    quota_shared_folder_mb: str | None = None
    quota_user_folder_mb: str | None = None
    quota_shared_folder_inodes: str | None = None
    quota_user_folder_inodes: str | None = None
    # SMTP
    smtp_host: str | None = None
    smtp_port: str | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_tls: str | None = None
    # Cloudflare Turnstile (login bot-protection)
    turnstile_site_key: str | None = None
    turnstile_secret_key: str | None = None
    # Password policy
    password_min_score: str | None = None
    password_min_length: str | None = None
    # Require a second factor (TOTP or passkey) for local-password accounts.
    # OIDC users are exempt — their IdP owns MFA.
    require_2fa: bool | None = None
    # Passkey sign-in mode: "passwordless" (primary sign-in button; UV-required
    # so always ≥2 factors) or "second_factor" (passkeys only at the 2FA step).
    passkey_login_mode: str | None = None


class LicenseKeyRequest(BaseModel):
    license_key: str = ""


class SmtpTestRequest(BaseModel):
    host: str
    port: int
    user: str
    password: str
    from_addr: str
    tls: bool = True
    test_email: str = ""


async def _enforce_user_paired_disabled() -> None:
    """Cascade for the ``allow_user_paired_machines`` ON→OFF toggle.

    When the admin disables user-paired machines, we have to
    (a) close every live user-paired satellite WS (so sessions stop
    routing there), and (b) delete every ``user_remote_targets`` row that
    points at a user-paired machine (online or offline). Admin-paired
    machines are untouched. Re-enabling the toggle does NOT auto-restore
    deleted targets — users re-select per-agent in the UI.
    """
    try:
        from core.remote.satellite_connection import get_connection_manager
        from storage import remote_store as _rs
        cm = get_connection_manager()
        # Live user-paired satellites. Drop their targets, close
        # the WS with a clear close code, deregister.
        for machine_id in list(cm.get_connected_machines()):
            machine = _rs.get_remote_machine(machine_id)
            if not machine:
                continue
            if (machine.get("pairing_scope") or "") == "admin":
                continue  # admin-paired stays connected
            try:
                _rs.clear_user_remote_targets_for_machine(machine_id)
            except Exception:
                logger.exception(
                    "failed to clear targets for %s", machine_id[:8],
                )
            conn = cm.get_connection(machine_id)
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.ws.close(
                        code=4005, reason="feature_disabled_by_admin",
                    )
                await cm.deregister(machine_id)
        # Offline user-paired machines — drop their targets too.
        for m in _rs.get_all_user_paired_machines():
            try:
                _rs.clear_user_remote_targets_for_machine(m["id"])
            except Exception:
                logger.exception(
                    "failed to clear targets for offline machine %s",
                    m["id"][:8],
                )
    except Exception:
        logger.exception("user-paired-disabled cascade failed")


def _license_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@router.get("/v1/admin/platform-settings")
async def get_platform_settings(user: UserContext | None = Depends(get_current_user)):
    """Get platform settings. Admin only."""
    require_admin(user)
    from core import execution_mode
    from services.billing import relay_client
    from services.infra import storage_quota, turnstile
    settings = await asyncio.to_thread(task_store.get_all_platform_settings)
    license_info = await asyncio.to_thread(get_current_license)
    user_count = await asyncio.to_thread(task_store.count_users)
    tcfg = turnstile.load_config(settings)
    return {
        "company_name": settings.get("company_name", ""),
        "platform_instructions": settings.get("platform_instructions", ""),
        "platform_timezone": settings.get("platform_timezone", "") or config.SCHEDULER_TIMEZONE,
        "session_timeout": settings.get("session_timeout", "") or str(config.CLAUDE_TIMEOUT),
        "session_idle_timeout": settings.get("session_idle_timeout", "") or str(config.PERSISTENT_SESSION_TIMEOUT),
        "jwt_expiry_hours": settings.get("jwt_expiry_hours", "") or str(config.JWT_EXPIRY_HOURS),
        # Concurrency is fully automatic (live-RAM admission, no admin limit knobs).
        # The live gauge is at GET /v1/admin/concurrency-stats.
        # Kill-switch for user-paired remote machines. Default ON
        # (empty/unset = "1" semantics, mirrors remote_fallback_user_override).
        "allow_user_paired_machines": settings.get("allow_user_paired_machines", "") != "0",
        # Remote-agent fallback policy (default: fall back to local on user-override
        # offline, hard-fail on agent-default offline)
        "remote_fallback_user_override": settings.get("remote_fallback_user_override", "") != "0",
        "remote_fallback_agent_default": settings.get("remote_fallback_agent_default", "") == "1",
        # Global interactive kill-switch (core/execution_mode.py). Default ON
        # (unset = enabled since R1.5) — effective_enabled keeps this view and
        # the resolver from drifting.
        "interactive_cli_enabled": execution_mode.effective_enabled(
            settings.get(execution_mode.KILL_SWITCH_KEY, ""),
        ),
        # Session retention (services/infra/retention.py). Default ON @ 180 days
        # (unset = "1" semantics); the days knob governs the aged-chats pass
        # only — junk/orphan cleanup always runs.
        "session_retention_enabled": settings.get("session_retention_enabled", "") != "0",
        "session_retention_days": settings.get("session_retention_days", "") or "180",
        # Automatic MCP updates (services/mcp/mcp_autoupdate.py). Default ON
        # (unset = "1" semantics); a weekly job applies available community-MCP
        # updates in a low-traffic window, deferring docker MCPs that are in use.
        # Failure-only admin notification + an inline run-history log.
        "mcp_auto_update_enabled": settings.get("mcp_auto_update_enabled", "") != "0",
        # Storage quotas (services/infra/storage_quota.py). MB; the value shown is the
        # effective limit (built-in default when unset); "0" = unlimited. Inode
        # caps ship but default off (0). `storage_quotas_enforced` reflects the
        # kernel tier — when false the limits are measurement + warnings only.
        "quota_shared_folder_mb": settings.get("quota_shared_folder_mb", "") or str(config.QUOTA_SHARED_FOLDER_MB_DEFAULT),
        "quota_user_folder_mb": settings.get("quota_user_folder_mb", "") or str(config.QUOTA_USER_FOLDER_MB_DEFAULT),
        "quota_shared_folder_inodes": settings.get("quota_shared_folder_inodes", "") or str(config.QUOTA_SHARED_FOLDER_INODES_DEFAULT),
        "quota_user_folder_inodes": settings.get("quota_user_folder_inodes", "") or str(config.QUOTA_USER_FOLDER_INODES_DEFAULT),
        "storage_quotas_enforced": storage_quota.hard_enabled(),
        # SMTP
        "smtp_host": settings.get("smtp_host", ""),
        "smtp_port": settings.get("smtp_port", "587"),
        "smtp_user": settings.get("smtp_user", ""),
        "smtp_from": settings.get("smtp_from", ""),
        "smtp_tls": settings.get("smtp_tls", "true"),
        "smtp_password_set": bool(settings.get("smtp_password_enc", "")),
        # Cloudflare Turnstile (login bot-protection). On OtoDock-managed installs the
        # keys come from env — never surface them; the UI shows only a "managed" badge.
        # turnstile_secret_key_set is keyed on the DECRYPTED secret so a decrypt failure
        # honestly reports "not set" instead of a false "configured".
        "turnstile_site_key": "" if tcfg.managed else tcfg.site_key,
        "turnstile_secret_key_set": False if tcfg.managed else bool(tcfg.secret_key),
        "turnstile_managed": tcfg.managed,
        # License — never return the raw key (it's stored
        # encrypted); the dashboard only needs to know one is on file.
        "has_license_key": bool(get_license_key()),
        "license_tier": license_info.tier,
        "license_max_users": license_info.max_users,
        "license_users_count": user_count,
        "license_status": license_info.status,            # valid|grace|expired|lifetime|unactivated|grace_unreachable|lapsed
        "license_valid_until": license_info.valid_until,  # ISO expiry, or ""
        "license_days_since_expiry": license_info.days_since_expiry,
        "license_lifetime": license_info.lifetime,
        "license_company_name": license_info.company_name,
        "license_mode": license_info.license_mode,                  # subscription|offline_term
        "license_activation_state": license_info.activation_state,  # none|activated
        "license_check_status": license_info.check_status,
        "license_last_check_at": settings.get("license_last_check_at", ""),
        "air_gapped": not relay_client.relay_offered(),
        # Deployment axis — the UI adapts (cloud shows plan + usage; self-host
        # shows the license-key entry).
        "cloud": config.OTODOCK_CLOUD,
        # Operator-forced settings (managed installs) — keys the admin can't
        # change. Their values above already reflect the forced overlay; the UI
        # locks/hides these. Empty on a normal self-host.
        "forced_keys": sorted(config.forced_settings().keys()),
        # OtoDock account connection (API/credit relay) — the "OtoDock Connection"
        # admin card binds to this. connected = an account_token is stored (paid
        # auto-link OR browser handshake); enabled = the master toggle; active =
        # the effective state (relay usable AND enabled AND connected); forced =
        # operator-managed (cloud).
        "otodock_connection": {
            "connected": relay_client.is_connected(),
            "enabled": relay_client.api_relay_enabled(),
            "active": relay_client.system_relay_active(),
            "relay_available": relay_client.is_available(),
            "air_gapped": not relay_client.relay_offered(),
            "forced": "otodock_api_relay_enabled" in config.forced_settings(),
        },
        # Password policy
        "password_min_score": settings.get("password_min_score", "3"),
        "password_min_length": settings.get("password_min_length", "8"),
        # Require-2FA policy for local accounts (default OFF). Enforced as
        # forced enrollment after login — never a silent lockout.
        "require_2fa": settings.get("require_2fa", "") == "1",
        "passkey_login_mode": settings.get("passkey_login_mode", "") or "passwordless",
    }


@router.put("/v1/admin/platform-settings")
async def set_platform_settings(
    req: PlatformSettingsRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Update platform settings. Admin only."""
    u = require_admin(user)
    if req.company_name is not None:
        await asyncio.to_thread(task_store.set_platform_setting, "company_name", req.company_name)
    if req.platform_instructions is not None:
        await asyncio.to_thread(task_store.set_platform_setting, "platform_instructions", req.platform_instructions)
    if req.platform_timezone is not None:
        await asyncio.to_thread(task_store.set_platform_setting, "platform_timezone", req.platform_timezone)
        # Invalidate the cached timezone, then re-register all recurring jobs so
        # the new TZ takes effect immediately (event-driven — no polling job).
        config._tz_cache["tz"] = None
        from services.scheduler import scheduler
        await asyncio.to_thread(scheduler.apply_platform_timezone_change)
    if req.session_timeout is not None:
        await asyncio.to_thread(task_store.set_platform_setting, "session_timeout", req.session_timeout)
    if req.session_idle_timeout is not None:
        await asyncio.to_thread(task_store.set_platform_setting, "session_idle_timeout", req.session_idle_timeout)
    if req.jwt_expiry_hours is not None:
        await asyncio.to_thread(task_store.set_platform_setting, "jwt_expiry_hours", req.jwt_expiry_hours)
    # Concurrency is fully automatic (live-RAM admission in core/concurrency.py) —
    # there are no admin-tunable session/task limits to write here.
    if req.allow_user_paired_machines is not None:
        # Detect ON→OFF flip and fire the force-disconnect cascade.
        old = await asyncio.to_thread(
            task_store.get_platform_setting, "allow_user_paired_machines",
        )
        old_on = (old or "") != "0"  # default ON
        new_on = bool(req.allow_user_paired_machines)
        await asyncio.to_thread(
            task_store.set_platform_setting,
            "allow_user_paired_machines", "1" if new_on else "0",
        )
        if old_on and not new_on:
            # Schedule the cascade so we return the API call immediately.
            asyncio.create_task(_enforce_user_paired_disabled())
    if req.interactive_cli_enabled is not None:
        from core import execution_mode
        await asyncio.to_thread(
            task_store.set_platform_setting,
            execution_mode.KILL_SWITCH_KEY,
            "1" if req.interactive_cli_enabled else "0",
        )
    if req.remote_fallback_user_override is not None:
        await asyncio.to_thread(
            task_store.set_platform_setting,
            "remote_fallback_user_override",
            "1" if req.remote_fallback_user_override else "0",
        )
    if req.remote_fallback_agent_default is not None:
        await asyncio.to_thread(
            task_store.set_platform_setting,
            "remote_fallback_agent_default",
            "1" if req.remote_fallback_agent_default else "0",
        )
    if req.session_retention_enabled is not None:
        await asyncio.to_thread(
            task_store.set_platform_setting,
            "session_retention_enabled",
            "1" if req.session_retention_enabled else "0",
        )
    if req.mcp_auto_update_enabled is not None:
        await asyncio.to_thread(
            task_store.set_platform_setting,
            "mcp_auto_update_enabled",
            "1" if req.mcp_auto_update_enabled else "0",
        )
    if req.session_retention_days is not None:
        # Clamp to the sweep's floor; garbage falls back to the default.
        from services.infra import retention as _retention
        try:
            days = max(_retention.MIN_DAYS, int(req.session_retention_days))
        except (TypeError, ValueError):
            days = _retention.DEFAULT_DAYS
        await asyncio.to_thread(
            task_store.set_platform_setting,
            "session_retention_days", str(days),
        )
    # Storage quotas — non-negative integers; 0 = unlimited. On any change,
    # re-apply the limits to live XFS projects so a new cap takes effect at once
    # (kernel tier); the soft-tier monitor picks it up on its next sweep anyway.
    quota_changed = False
    for _val, _key in (
        (req.quota_shared_folder_mb, "quota_shared_folder_mb"),
        (req.quota_user_folder_mb, "quota_user_folder_mb"),
        (req.quota_shared_folder_inodes, "quota_shared_folder_inodes"),
        (req.quota_user_folder_inodes, "quota_user_folder_inodes"),
    ):
        if _val is not None:
            try:
                _clean = max(0, int(_val))
            except (TypeError, ValueError):
                _clean = 0
            await asyncio.to_thread(task_store.set_platform_setting, _key, str(_clean))
            quota_changed = True
    if quota_changed:
        from services.infra import quota_monitor, storage_quota
        asyncio.create_task(asyncio.to_thread(storage_quota.reapply_all_limits))
        # Re-evaluate thresholds on the next sweep (~60s) instead of waiting out
        # the monitor's throttle, so a limit change surfaces warnings promptly.
        quota_monitor.request_recheck()
    # SMTP settings
    from services.notifications.smtp import save_smtp_config as _save_smtp
    smtp_fields = {
        "smtp_host": req.smtp_host, "smtp_port": req.smtp_port,
        "smtp_user": req.smtp_user, "smtp_from": req.smtp_from,
        "smtp_tls": req.smtp_tls,
    }
    if any(v is not None for v in smtp_fields.values()) or req.smtp_password is not None:
        await asyncio.to_thread(
            _save_smtp,
            req.smtp_host or "", req.smtp_port or "587", req.smtp_user or "",
            req.smtp_password, req.smtp_from or "", req.smtp_tls or "true",
        )
    # Cloudflare Turnstile (secret stored encrypted; ignored when env-managed)
    if req.turnstile_site_key is not None or req.turnstile_secret_key is not None:
        from services.infra import turnstile
        await asyncio.to_thread(turnstile.save_keys, req.turnstile_site_key, req.turnstile_secret_key)
    # (License is set via POST /v1/admin/license — not here.)
    # Password policy
    if req.password_min_score is not None:
        await asyncio.to_thread(task_store.set_platform_setting, "password_min_score", req.password_min_score)
    if req.password_min_length is not None:
        await asyncio.to_thread(task_store.set_platform_setting, "password_min_length", req.password_min_length)
    if req.require_2fa is not None:
        await asyncio.to_thread(
            task_store.set_platform_setting,
            "require_2fa", "1" if req.require_2fa else "0",
        )
    if req.passkey_login_mode is not None:
        if req.passkey_login_mode not in ("passwordless", "second_factor"):
            raise HTTPException(status_code=400, detail="Invalid passkey_login_mode")
        await asyncio.to_thread(
            task_store.set_platform_setting,
            "passkey_login_mode", req.passkey_login_mode,
        )
    logger.info(f"Admin {mask_email(u.email)} updated platform settings")
    return {"status": "updated"}


@router.post("/v1/admin/license")
async def set_license(
    req: LicenseKeyRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Set (or clear) the platform license key + activate it. Admin only.

    Persists the key, then — for a ``subscription``/``lifetime`` key on a
    connected install — binds it to this install via the relay. A key
    change/clear releases the previous binding first. Returns the recomputed
    status + a message (e.g. activation_limit_reached guidance). The license is
    set ONLY here, not via the platform-settings PUT.
    """
    u = require_admin(user)
    from auth import license as L
    from services.billing import relay_client

    new_key = (req.license_key or "").strip()
    old_key = await asyncio.to_thread(get_license_key)

    if old_key and old_key != new_key:
        # Key changed/cleared → free the old binding + drop cached activation.
        try:
            await relay_client.deactivate_license(old_key)
        except Exception as exc:
            # Best-effort — relay may be unbuilt/unreachable; binding is cleared locally.
            logger.debug(f"License deactivation for the old key failed: {exc}")
        for k in ("license_activation_receipt", "license_check_status",
                  "license_last_ok_at", "license_last_check_at"):
            await asyncio.to_thread(task_store.set_platform_setting, k, "")

    await asyncio.to_thread(set_license_key, new_key)

    message = ""
    if new_key:
        lic = await asyncio.to_thread(L.validate_license_key, new_key)
        if lic is None:
            message = "Invalid or unverifiable license key."
        elif (lic.lifetime or lic.license_mode == "subscription") and relay_client.is_available():
            await asyncio.to_thread(task_store.set_platform_setting, "license_last_check_at", _license_now_iso())
            try:
                receipt = await relay_client.activate_license(new_key)
                if isinstance(receipt, str) and receipt:
                    await asyncio.to_thread(task_store.set_platform_setting, "license_activation_receipt", receipt)
                await asyncio.to_thread(task_store.set_platform_setting, "license_check_status", "active")
                await asyncio.to_thread(task_store.set_platform_setting, "license_last_ok_at", _license_now_iso())
            except relay_client.RelayError as e:
                message = relay_client.relay_error_message(e.code)
            except relay_client.RelayNotConfigured:
                message = "Saved — activation completes once the OtoDock relay is available."
            except Exception:
                logger.exception("License activation failed")
                message = "Saved, but activation failed; it will retry automatically."

    lic_now = await asyncio.to_thread(get_current_license)
    logger.info(f"Admin {mask_email(u.email)} updated the platform license")
    return {"message": message, "status": lic_now.status,
            "activation_state": lic_now.activation_state}


@router.post("/v1/admin/license/deactivate")
async def deactivate_license_binding(
    user: UserContext | None = Depends(get_current_user),
):
    """Release this install's license binding ("Move license"). Admin only.

    Best-effort relay call + clears the cached activation; the key itself stays
    so the admin can re-activate here or move it to another install.
    """
    u = require_admin(user)
    from services.billing import relay_client
    key = await asyncio.to_thread(get_license_key)
    if key:
        try:
            await relay_client.deactivate_license(key)
        except Exception as exc:
            # Best-effort — the cached activation is cleared regardless.
            logger.debug(f"License deactivation failed: {exc}")
    for k in ("license_activation_receipt", "license_check_status",
              "license_last_ok_at", "license_last_check_at"):
        await asyncio.to_thread(task_store.set_platform_setting, k, "")
    lic_now = await asyncio.to_thread(get_current_license)
    logger.info(f"Admin {mask_email(u.email)} deactivated the platform license binding")
    return {"status": lic_now.status, "activation_state": lic_now.activation_state}


@router.post("/v1/admin/license/recheck")
async def recheck_license(
    user: UserContext | None = Depends(get_current_user),
):
    """Force an immediate liveness re-check (same path + lock as the worker)."""
    require_admin(user)
    from services.billing import license_check_worker
    try:
        await license_check_worker._do_check_under_lock(force=True)
    except Exception:
        logger.exception("Manual license re-check failed (fail-open)")
    lic_now = await asyncio.to_thread(get_current_license)
    return {"status": lic_now.status, "activation_state": lic_now.activation_state}


@router.get("/v1/admin/concurrency-stats")
async def get_concurrency_stats(user: UserContext | None = Depends(get_current_user)):
    """Get live session concurrency counters. Admin only."""
    require_admin(user)
    from core.concurrency import get_stats
    return get_stats()


@router.post("/v1/admin/smtp/test")
async def admin_test_smtp(
    req: SmtpTestRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Test SMTP connection. Admin only."""
    require_admin(user)
    from services.notifications.smtp import test_smtp_connection
    success, message = await asyncio.to_thread(
        test_smtp_connection, req.host, req.port, req.user,
        req.password, req.tls, req.test_email,
    )
    return {"success": success, "message": message}

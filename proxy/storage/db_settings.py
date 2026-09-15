"""Platform-settings queries.

Part of the ``storage.database`` facade; import names from
``storage.database`` rather than this module directly. All functions are
synchronous (called via ``asyncio.to_thread`` from async code).
"""

from storage.pg import get_conn


def get_platform_setting(key: str) -> str:
    # Operator-forced settings (managed/cloud installs) win over the DB so every
    # consumer honors them uniformly. See config.forced_settings().
    import config as _cfg
    forced = _cfg.forced_settings()
    if key in forced:
        return forced[key]
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM platform_settings WHERE key=%s", (key,)).fetchone()
        return row["value"] if row else ""


def set_platform_setting(key: str, value: str) -> None:
    # Writes to operator-forced keys are ignored — the operator owns them and the
    # read overlay returns the forced value regardless. Single write-guard for
    # both the admin API and any internal caller.
    import config as _cfg
    if key in _cfg.forced_settings():
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO platform_settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            (key, value),
        )
        conn.commit()


def seed_platform_timezone_if_unset() -> None:
    """One-shot on FIRST install: default ``platform_timezone`` to the server's
    local zone (e.g. ``Europe/Athens``) so task scheduling / cron / datetime
    injection match the operator's wall clock instead of UTC out of the box.

    Guarded on the setting being ABSENT — it runs once on a fresh DB and NEVER
    again, so an admin's later change persists and platform updates never
    overwrite it. In a container the detected zone is the CONTAINER's (UTC unless
    the operator sets ``TZ`` / mounts ``/etc/localtime``), so Docker deploys
    localise via the proxy service's ``TZ`` env. Best-effort: any failure leaves
    the UTC fallback (``config.SCHEDULER_TIMEZONE``) untouched.
    """
    import logging
    log = logging.getLogger("claude-proxy.database")
    try:
        if get_platform_setting("platform_timezone"):
            return  # already set (prior install or admin choice) — never touch
        # Only a GENUINELY fresh install (no users yet) — never retroactively
        # change an EXISTING install whose tz setting merely defaulted to UTC
        # (e.g. the live box on its next restart). "On installation" = no users.
        from storage.identity.db_users import count_users
        if count_users() > 0:
            return
        import tzlocal
        tz = (tzlocal.get_localzone_name() or "").strip()
        if tz:
            set_platform_setting("platform_timezone", tz)
            log.info("Seeded platform_timezone from server local time: %s", tz)
    except Exception as e:
        log.warning("Could not seed platform_timezone (UTC fallback stays): %s", e)


def get_all_platform_settings() -> dict[str, str]:
    import config as _cfg
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM platform_settings").fetchall()
        out = {r["key"]: r["value"] for r in rows}
    out.update(_cfg.forced_settings())  # forced values win over the DB
    return out

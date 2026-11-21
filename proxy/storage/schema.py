"""PostgreSQL schema entry points and the DDL of the flat storage domains.

``init_schema`` (called once at startup, then by the test conftest) runs one
``init_*`` function per domain in foreign-key order. The functions live next
to the stores of their tables: ``storage/<domain>/schema.py`` for the
sub-packages, this module for the flat domains (remote machines, storage
quotas). ``run_migrations`` runs the forward migrations for pre-existing
installs, a domain's own ``migrate_*`` or inline, and ``check_schema_drift`` is
the boot guard for a column that never got one. Every CREATE TABLE / CREATE INDEX uses IF NOT
EXISTS so re-runs are safe (the startup path is idempotent).
"""

import contextlib
import json
import logging
import re

from auth import roles
from storage.agents.schema import (
    init_agents,
    init_departments,
    init_memory,
    init_outside,
    migrate_agents,
)
from storage.apps.schema import (
    init_app_handler_deliveries,
    init_app_secrets,
    init_app_state,
    init_pinned_app_user_hides,
    init_pinned_apps,
    migrate_apps,
)
from storage.automation.schema import (
    init_notifications,
    init_push,
    init_tasks,
    init_triggers,
    init_webhooks,
    migrate_automation,
)
from storage.billing.schema import init_execution_layers, init_usage
from storage.cards.schema import init_cards
from storage.chat.schema import (
    backfill_conversation_records,
    init_chats,
    init_meetings,
    init_reply_reports,
    migrate_chat,
)
from storage.checks.schema import init_checks
from storage.files.schema import (
    init_file_sync,
    init_file_transfers,
    init_pinned_files,
    init_recover_bin,
)
from storage.identity.schema import init_identity, migrate_identity
from storage.knowledge.schema import init_knowledge_libraries
from storage.mcp.schema import (
    init_community_requests,
    init_mcp,
    init_mcp_autoupdate,
    init_mcp_tool_listings,
    migrate_mcp,
)
from storage.move_schema import init_chat_moves, init_engine_switch_leftovers
from storage.notebook.schema import init_notebook
from storage.phone.schema import init_audio_telephony, init_phone_call_log
from storage.schema_base import (  # noqa: F401
    _check_constraint_words,
    _column_exists,
    _index_exists,
)
from storage.sharing.schema import _SHARE_CHECKS, init_shares, migrate_sharing

logger = logging.getLogger(__name__)

# The per-agent role CHECKs ``run_migrations`` keeps current on an existing
# install: (table, constraint name, column, the words). The CREATEs spell the
# same words and ``tests/auth/test_roles.py`` pins both to ``auth/roles``.
_ROLE_CHECKS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("user_agents", "user_agents_agent_role_check", "agent_role", roles.AGENT_ROLES),
    ("agents", "agents_default_for_new_users_role_check", "default_for_new_users_role",
     ("",) + tuple(reversed(roles.AGENT_ROLES))),
    ("agents", "agents_team_account_floor_check", "team_account_floor", tuple(reversed(roles.AGENT_ROLES))),
    ("shares", "shares_role_cap_check", "role_cap", roles.AGENT_ROLES),
    ("dynamic_tasks", "dynamic_tasks_notify_audience_check", "notify_audience", roles.AGENT_ROLES),
    ("triggers", "triggers_notify_audience_check", "notify_audience", roles.AGENT_ROLES),
    ("notifications", "notifications_audience_check", "audience", roles.AGENT_ROLES),
)


# ---------------------------------------------------------------------------
# Table creation for the flat domains (the sub-packages hold the rest;
# init_schema calls every domain in FK order)
# ---------------------------------------------------------------------------


def init_storage_quotas(conn) -> None:
    """XFS project-quota registry and threshold-alert dedup."""
    # --- Storage quotas (services/infra/storage_quota.py + services/infra/quota_monitor.py) ---
    # Kernel-tier project-ID registry: a stable scope_key -> XFS project_id map so
    # disk usage charged to an inode can be traced back to the agent/user bucket
    # that owns it across restarts. Limits are NOT stored here — they come from
    # the quota_* platform settings (one value per bucket TYPE) and are re-applied
    # each sweep, so there is a single source of truth and the table can't drift.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS storage_quota_projects (
            scope_key  TEXT PRIMARY KEY,   -- "{agent}:shared" | "user:{agent}:{username}"
            agent_slug TEXT NOT NULL,
            scope_type TEXT NOT NULL,      -- 'shared' | 'user'
            username   TEXT,               -- NULL for the shared bucket
            project_id BIGINT NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_storage_quota_projects_agent "
        "ON storage_quota_projects (agent_slug)"
    )

    # Threshold-alert dedup with hysteresis: one row per (scope, threshold) that
    # has fired. The monitor re-arms a threshold (deletes the row) only after
    # usage drops below threshold-5%, so the 90/95/100% WARNING notifications
    # don't flap on every 60s sweep.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS storage_quota_alerts (
            scope_key TEXT NOT NULL,
            metric    TEXT NOT NULL,       -- 'bytes' | 'inodes'
            threshold INT  NOT NULL,       -- 90 | 95 | 100
            fired_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (scope_key, metric, threshold)
        )
    """)


def init_remote_machines(conn) -> None:
    """Satellite machines and their per-agent / per-user targets."""
    # --- Remote machines (satellite daemon connections) ---
    # No IP column — the satellite dials the platform's public `wss://` endpoint
    # outbound and reports its local tunnel port via
    # `capabilities.local_tunnel_port` at auth time.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS remote_machines (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'offline',
            last_seen TEXT,
            -- NULL only on a compute machine, which nobody owns
            -- (remote_schema.OWNER_CHECK, added by its migration).
            registered_by TEXT,
            -- 'admin' = paired via /v1/admin/remote-machines/pair (platform
            -- infrastructure, can be agent-scope default for any agent).
            -- 'user'  = paired via /v1/users/me/remote-machines/pair
            -- (personal, only for the owner's user-scope chats/tasks).
            -- 'compute' = registered via /v1/admin/remote-machines/compute
            -- (the platform's own machine, where the sessions of the
            -- platform's place run).
            pairing_scope TEXT NOT NULL DEFAULT 'admin',
            pairing_token_hash TEXT,
            pairing_token_created_at TEXT,
            machine_secret_hash TEXT,
            capabilities TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            auto_update_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            satellite_version TEXT,
            last_update_at TEXT,
            last_update_error TEXT,
            pending_update BOOLEAN NOT NULL DEFAULT FALSE,
            -- Auto-update rollback budget, PER TARGET VERSION: how many
            -- pushed updates to update_rollback_target came back rolled
            -- back (the satellite reconnected below the target). At
            -- ws/satellite.py::MAX_AUTO_UPDATE_ROLLBACKS automatic pushes of
            -- that target stop; "Update now" still pushes; a successful
            -- reconnect on the target (or a new target) resets the count.
            update_rollback_count INTEGER NOT NULL DEFAULT 0,
            update_rollback_target TEXT,
            -- Per-machine filesystem-access policy. When TRUE,
            -- agents running on this satellite can read/write any path the
            -- OS user can reach. When FALSE, agents are limited to the
            -- agent tree + the OS user's home directory.
            --   Admin-pairing flow defaults to TRUE (operational machines).
            --   User-pairing flow defaults to FALSE (personal laptops).
            allow_full_fs BOOLEAN NOT NULL DEFAULT FALSE,
            -- Per-machine device-control consent.
            -- JSON array of granted capability keys, e.g. ["computer","browser"].
            -- Empty (the default) blocks ALL device-local MCPs on this machine.
            -- Device control is strictly more powerful than allow_full_fs (it
            -- can drive sudo prompts, a browser with saved cards), so it
            -- defaults closed for BOTH admin- and user-paired machines and is
            -- granted only by an explicit per-capability toggle.
            device_grants TEXT NOT NULL DEFAULT '[]',
            -- Which browser the browser-control MCP drives on this machine:
            -- 'dedicated' (the per-agent profile, default) or 'own' (the OS
            -- user's signed-in Chrome/Edge/Brave through the Playwright
            -- Extension). Meaningful only while 'browser' is granted —
            -- revoking that grant resets it.
            browser_mode TEXT NOT NULL DEFAULT 'dedicated',
            -- Playwright Extension token for 'own' mode (Fernet, credential-
            -- store key). NULL = every session asks the user to click Allow.
            -- Never leaves the store: listed in remote_store._SECRET_COLUMNS.
            browser_extension_token_enc TEXT,
            -- Whether an unattended session (a task, a call, a meeting) gets
            -- that token too; off, only a session a person drives does.
            browser_unattended BOOLEAN NOT NULL DEFAULT FALSE,
            -- Whether admins currently hold an outstanding "offline" alert
            -- for this machine. Set TRUE when the sustained-outage evaluator
            -- (core/remote/satellite_connection.py) fires the offline notification;
            -- cleared when the machine reconnects (firing a "back online"
            -- notice only if it had been set). Persisting this makes the
            -- alert edge-triggered and restart-safe: a proxy restart or
            -- satellite auto-update no longer produces offline/online spam.
            offline_alerted BOOLEAN NOT NULL DEFAULT FALSE,
            -- Deliberate pause (tray Pause). When TRUE the machine is
            -- offline by intent, so the sustained-outage evaluator skips it
            -- (no false admin alert). Cleared on the next successful auth
            -- (tray Resume / reboot reconnects). Persisted so the
            -- suppression survives a proxy restart.
            paused BOOLEAN NOT NULL DEFAULT FALSE,
            -- optional admin override of the satellite's own physical session
            -- ceiling; NULL = use its reported recommended_max_sessions.
            max_sessions INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_remote_targets (
            agent_slug TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            added_by TEXT NOT NULL,
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (agent_slug, machine_id),
            FOREIGN KEY (agent_slug) REFERENCES agents(slug) ON DELETE CASCADE,
            FOREIGN KEY (machine_id) REFERENCES remote_machines(id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_remote_targets_machine ON agent_remote_targets(machine_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_remote_targets (
            user_sub TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            agent_slug TEXT NOT NULL DEFAULT '',
            added_at TEXT NOT NULL,
            PRIMARY KEY (user_sub, agent_slug),
            FOREIGN KEY (user_sub) REFERENCES users(sub) ON DELETE CASCADE,
            FOREIGN KEY (machine_id) REFERENCES remote_machines(id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_remote_targets_machine ON user_remote_targets(machine_id)")


# ---------------------------------------------------------------------------
# Schema entry points
# ---------------------------------------------------------------------------

def init_schema(conn) -> None:
    """Create every table and index if absent (idempotent; safe each boot)."""
    init_tasks(conn)
    init_identity(conn)
    init_chats(conn)
    init_cards(conn)
    init_usage(conn)
    init_mcp(conn)
    init_agents(conn)
    init_outside(conn)
    init_departments(conn)
    init_storage_quotas(conn)
    init_community_requests(conn)
    init_audio_telephony(conn)
    init_phone_call_log(conn)
    init_remote_machines(conn)
    init_chat_moves(conn)
    init_engine_switch_leftovers(conn)
    init_meetings(conn)
    init_execution_layers(conn)
    init_notifications(conn)
    init_mcp_autoupdate(conn)
    init_mcp_tool_listings(conn)
    init_push(conn)
    init_webhooks(conn)
    init_triggers(conn)
    init_memory(conn)
    init_recover_bin(conn)
    init_pinned_apps(conn)
    init_pinned_app_user_hides(conn)
    init_app_state(conn)
    init_app_handler_deliveries(conn)
    init_app_secrets(conn)
    init_shares(conn)
    init_pinned_files(conn)
    init_file_sync(conn)
    init_file_transfers(conn)
    init_knowledge_libraries(conn)
    init_checks(conn)
    init_reply_reports(conn)
    init_notebook(conn)
    init_schema_meta(conn)
    logger.info("PostgreSQL schema initialized (all tables created)")


# ---------------------------------------------------------------------------
# The release that migrated this database (schema_meta)
# ---------------------------------------------------------------------------
# One row. Its own table, not a platform_settings key: that bag is written by
# the admin settings route and overlaid by OTODOCK_FORCED_SETTINGS, and a fact
# that gates the boot must be neither; restore.sh recreates the public schema
# before it feeds a dump, so a dump that does not carry the table leaves none
# behind (features/BACKUP-RESTORE.md).

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(rc(\d+)|dev))?$")


def parse_version(text: str | None) -> tuple[int, int, int, tuple[int, int]] | None:
    """The one version rule, shared with the release check and the scripts
    (BACKUP-RESTORE.md "One version rule"): ``X.Y.Z`` is a release,
    ``X.Y.Z-rcN`` is older than it, ``X.Y.Z-dev`` older than every rc, a
    leading ``v`` is dropped. Anything else is unknown (None): no refusal, no
    notice, no row write."""
    m = _VERSION_RE.match((text or "").strip())
    if not m:
        return None
    suffix = m.group(4)
    if suffix is None:
        pre = (0, 0)
    elif suffix == "dev":
        pre = (-2, 0)
    else:
        pre = (-1, int(m.group(5)))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), pre)


def is_dev_version(text: str | None) -> bool:
    """A developer's working tree (``-dev``): never refused, never recorded."""
    return (text or "").strip().endswith("-dev")


def init_schema_meta(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_meta (
            id SMALLINT PRIMARY KEY CHECK (id = 1),
            created_by TEXT NOT NULL DEFAULT '',
            last_migrated_by TEXT NOT NULL,
            last_migrated_at TIMESTAMPTZ NOT NULL,
            upgraded_from TEXT NOT NULL DEFAULT ''
        )
    """)


def read_platform_version(conn) -> dict | None:
    """The ``schema_meta`` row, or None when the table or the row is absent (a
    database from before 1.8.0, or one no release has migrated yet)."""
    present = conn.execute("SELECT to_regclass('schema_meta') AS t").fetchone()["t"]
    if present is None:
        return None
    row = conn.execute(
        "SELECT created_by, last_migrated_by, last_migrated_at, upgraded_from "
        "FROM schema_meta WHERE id = 1"
    ).fetchone()
    return dict(row) if row else None


def newer_database_refusal(db_version: str, running: str) -> str:
    return (
        f"this database was last migrated by OtoDock {db_version} and this server "
        f"runs {running}. Migrations only run forward, so an older release cannot "
        f"serve it: upgrade this server to {db_version} or later, or restore a "
        f"backup taken on {running} (scripts/restore.sh). To start anyway set "
        f"OTODOCK_ALLOW_NEWER_DATABASE=true: the server then runs against a schema "
        f"it does not know, and data the newer release wrote may be misread or lost."
    )


def platform_version_preflight(conn, running: str, *, allow_newer: bool = False) -> str:
    """Before ``init_schema``: the refusal text when a NEWER release migrated
    this database and nothing lets the boot go on, else ''. A ``-dev`` build
    and ``OTODOCK_ALLOW_NEWER_DATABASE`` turn the refusal into a WARNING; an
    unknown version on either side skips the check with a WARNING."""
    meta = read_platform_version(conn)
    if meta is None:
        return ""
    db_version = meta["last_migrated_by"]
    db_v, run_v = parse_version(db_version), parse_version(running)
    if run_v is None:
        logger.warning(
            "the platform version %r is not a release number: the database's "
            "version (%s) is not checked", running, db_version)
        return ""
    if db_v is None:
        logger.warning("schema_meta names %r, not a release number: not checked", db_version)
        return ""
    if db_v <= run_v:
        return ""
    text = newer_database_refusal(db_version, running)
    if is_dev_version(running):
        logger.warning("%s (a -dev build: the boot goes on)", text)
        return ""
    if allow_newer:
        logger.warning("%s (OTODOCK_ALLOW_NEWER_DATABASE is set: the boot goes on)", text)
        return ""
    return text


def record_platform_version(conn, running: str) -> None:
    """After ``run_migrations``, in the same transaction: the row says which
    release migrated the database last. The version only rises; a fresh
    database (no users yet) remembers the release that created it; a database
    from before the row (a 1.7.1 install meeting 1.8.0) gets ``created_by ''``
    and this boot's time as the upgrade. Unknown and ``-dev`` versions write
    nothing. Two processes booting together serialize on the row lock, and
    the insert tolerates the loser."""
    run_v = parse_version(running)
    if run_v is None or is_dev_version(running):
        return
    row = conn.execute(
        "SELECT last_migrated_by FROM schema_meta WHERE id = 1 FOR UPDATE"
    ).fetchone()
    if row is None:
        fresh = conn.execute("SELECT NOT EXISTS (SELECT 1 FROM users) AS fresh").fetchone()["fresh"]
        conn.execute(
            "INSERT INTO schema_meta (id, created_by, last_migrated_by, last_migrated_at, upgraded_from) "
            "VALUES (1, %s, %s, now(), '') ON CONFLICT (id) DO NOTHING",
            (running if fresh else "", running),
        )
        return
    old = parse_version(row["last_migrated_by"])
    if old is None:
        # A row this rule cannot read (a later release's suffix): never lowered.
        logger.warning("schema_meta names %r, not a release number: the row is left as it is",
                       row["last_migrated_by"])
        return
    if old < run_v:
        conn.execute(
            "UPDATE schema_meta SET last_migrated_by = %s, upgraded_from = %s, "
            "last_migrated_at = now() WHERE id = 1",
            (running, row["last_migrated_by"]),
        )


def migrate_database(conn, running: str, *, allow_newer: bool = False, engines=()) -> None:
    """The one guarded entry that migrates the database (the proxy's startup;
    the scheduler goes through the proxy's API): the version preflight, the
    schema, the migrations, the row; ``engines`` is ``migrate_agents``'s."""
    refusal = platform_version_preflight(conn, running, allow_newer=allow_newer)
    if refusal:
        logger.critical(refusal)
        raise RuntimeError(refusal)
    init_schema(conn)
    run_migrations(conn, engines=engines)
    record_platform_version(conn, running)


# ---------------------------------------------------------------------------
# Migrations (idempotent column additions)
# ---------------------------------------------------------------------------

# The first day the Windows hash bug (satellite 0.5.130–0.5.143) could run:
# an idle-merge delete recorded since then for an agent a Windows machine
# syncs may have been read off a manifest that left the file out.
_WINDOWS_HASH_BUG_SINCE = "2026-10-01"


def _windows_or_unknown(capabilities) -> bool:
    """Whether a machine's stored capabilities say Windows, or cannot be
    read at all (counted in: a hold only delays a delete). A machine that
    never connected stores ``{}`` and never ran the bug."""
    from core import host_os
    try:
        caps = json.loads(capabilities) if isinstance(capabilities, str) else capabilities
    except ValueError:
        return True
    if not isinstance(caps, dict):
        return True
    if not caps.get("os"):
        return False
    row = host_os.of(caps.get("os"))
    return row is not None and row.name == host_os.WINDOWS


def _hold_windows_hash_deletes(conn) -> None:
    """One-shot (1.8.0): turn the Windows hash bug's idle-merge deletes into
    holds (``file_tombstones_store.HOLD_ORIGIN``). Deleting those tombstones
    would let an idle machine's older bytes reach the platform first and win
    over the Windows machine's newer copy; a hold never deletes, and the
    fixed Windows satellite brings its copy back. Scope: tombstones with
    origin ``satellite-idle-delete`` since the bug began, of an agent tied
    to a Windows machine (or one whose capabilities cannot be read, or a
    machine since removed) by its sync state or a remote target naming it.
    Flag-guarded and inside a SAVEPOINT: a failure retries at the next
    start."""
    flag = "windows_hash_holds"
    if conn.execute("SELECT 1 FROM platform_settings WHERE key = %s", (flag,)).fetchone():
        return
    from storage.files.file_tombstones_store import HOLD_ORIGIN
    conn.execute("SAVEPOINT windows_hash_holds")
    try:
        candidates = {r["agent_slug"] for r in conn.execute(
            "SELECT DISTINCT agent_slug FROM file_tombstones "
            "WHERE origin = 'satellite-idle-delete' AND deleted_at >= %s",
            (_WINDOWS_HASH_BUG_SINCE,),
        ).fetchall()}
        held = 0
        if candidates:
            machines = {r["id"]: _windows_or_unknown(r["capabilities"]) for r in conn.execute(
                "SELECT id, capabilities FROM remote_machines").fetchall()}
            tied: set[str] = set()
            for r in conn.execute("SELECT DISTINCT machine_id, agent_slug FROM sync_state").fetchall():
                if machines.get(r["machine_id"], True):
                    tied.add(r["agent_slug"])
            for r in conn.execute(
                    "SELECT machine_id, agent_slug FROM agent_remote_targets "
                    "UNION SELECT machine_id, agent_slug FROM user_remote_targets "
                    "WHERE agent_slug <> ''").fetchall():
                if machines.get(r["machine_id"], True):
                    tied.add(r["agent_slug"])
            for agent in sorted(candidates & tied):
                held += conn.execute(
                    "UPDATE file_tombstones SET origin = %s "
                    "WHERE agent_slug = %s AND origin = 'satellite-idle-delete' "
                    "AND deleted_at >= %s",
                    (HOLD_ORIGIN, agent, _WINDOWS_HASH_BUG_SINCE),
                ).rowcount
        conn.execute(
            "INSERT INTO platform_settings (key, value) VALUES (%s, 'done') "
            "ON CONFLICT (key) DO NOTHING", (flag,),
        )
        conn.execute("RELEASE SAVEPOINT windows_hash_holds")
        if held:
            logger.warning(
                "Windows hash bug: %d delete record(s) since %s held until the "
                "Windows machine restores or the record expires",
                held, _WINDOWS_HASH_BUG_SINCE)
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT windows_hash_holds")
        logger.exception("Windows hash hold migration failed — will retry next startup")


def apscheduler_dow_to_standard(field: str) -> str:
    """Rewrite a day-of-week field from APScheduler numbering (0 = Monday)
    back to standard cron (0 or 7 = Sunday), preserving the fired days.

    The one-shot startup migration uses this on pre-existing rows: their
    numeric weekdays were INTERPRETED as APScheduler days, so rewriting them
    one day forward keeps every schedule firing exactly when it always did
    (the failure mode to avoid is silently shifting fire days). Numbers map
    0-6 → 1-7 so ranges never wrap; a lone Sunday emits the conventional 0;
    ``*/k`` becomes ``1-7/k`` (standard anchors steps at Sunday, APScheduler
    at Monday). Names and unrecognized tokens pass through untouched.
    """
    out: list[str] = []
    for token in field.split(","):
        token = token.strip()
        expr, sep, step_s = token.partition("/")
        suffix = f"{sep}{step_s}" if sep else ""
        if expr == "*":
            out.append(f"1-7{suffix}" if step_s.isdigit() and int(step_s) > 1 else token)
        elif "-" in expr:
            lo_s, _, hi_s = expr.partition("-")
            if lo_s.isdigit() and hi_s.isdigit() and 0 <= int(lo_s) <= int(hi_s) <= 6:
                out.append(f"{int(lo_s) + 1}-{int(hi_s) + 1}{suffix}")
            else:
                out.append(token)
        elif expr.isdigit() and 0 <= int(expr) <= 6:
            std = int(expr) + 1
            out.append(f"{0 if std == 7 else std}{suffix}")
        else:
            out.append(token)
    return ",".join(out)


def _widen_identity_mode_check(conn) -> None:
    """``phone_routes.identity_mode`` takes 'agent': the CHECK an older
    install's CREATE made knows three words. Replaced in a SAVEPOINT so a
    surprise leaves the old one and the next boot tries again; read from the
    catalog, so a boot after the first does nothing."""
    row = conn.execute(
        "SELECT pg_get_constraintdef(c.oid) AS def FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE t.relname = 'phone_routes' AND n.nspname = current_schema() "
        "AND c.conname = 'phone_routes_identity_mode_check'"
    ).fetchone()
    if row is not None and "'agent'" in (row["def"] or ""):
        return
    conn.execute("SAVEPOINT widen_identity_mode")
    try:
        conn.execute("ALTER TABLE phone_routes DROP CONSTRAINT IF EXISTS phone_routes_identity_mode_check")
        conn.execute(
            "ALTER TABLE phone_routes ADD CONSTRAINT phone_routes_identity_mode_check "
            "CHECK (identity_mode IN ('caller', 'shared', 'user', 'agent'))")
        conn.execute("RELEASE SAVEPOINT widen_identity_mode")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT widen_identity_mode")
        logger.exception("phone_routes: the identity CHECK was not widened; the next boot retries")


def run_migrations(conn, *, engines=()) -> None:
    """Post-launch schema migration hook.

    ``init_schema()`` is the single source of truth for the schema and all
    indexes. Additive migrations for pre-existing installs land here (the
    startup call sites are ``proxy/app.py`` /
    ``scheduler/standalone_scheduler.py`` / ``conftest.py``).

    Conventions when this becomes non-empty after launch:

    - Check ``information_schema`` / ``pg_indexes`` first so migrations are
      idempotent (safe to run on every startup).
    - One-shot data transformations should set a "done" flag in
      ``platform_settings`` so subsequent startups skip the work cheaply.
    - The caller's transaction has ``autocommit=False``. For best-effort blocks
      where partial failures must not abort the outer transaction, wrap in
      ``SAVEPOINT name`` / ``ROLLBACK TO SAVEPOINT name`` / ``RELEASE SAVEPOINT
      name`` — never rely on ``try/except: pass`` alone.

    The pre-launch transitional migrations that once lived here were removed
    once every install converged (pre-launch DBs are hand-converged).
    """
    migrate_apps(conn)
    migrate_chat(conn)
    # 2026-09-12: a notification row may carry its own dashboard path (a
    # shared app or chat); empty on every pre-existing row = the old deep link.
    conn.execute(
        "ALTER TABLE notification_deliveries "
        "ADD COLUMN IF NOT EXISTS href TEXT NOT NULL DEFAULT ''"
    )
    conn.execute("ALTER TABLE dynamic_tasks ADD COLUMN IF NOT EXISTS checks TEXT NOT NULL DEFAULT ''")
    # 2026-09-13: an app handler as a task row (`task_type='app'`) and as a
    # trigger's action. NULL on every pre-existing row. The partial unique
    # index lives here because a pre-existing install's CREATE ran before
    # the columns existed (the scope-index precedent below).
    conn.execute("ALTER TABLE dynamic_tasks ADD COLUMN IF NOT EXISTS app_id TEXT")
    conn.execute("ALTER TABLE dynamic_tasks ADD COLUMN IF NOT EXISTS app_handler TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_dyn_tasks_app_handler "
        "ON dynamic_tasks (app_id, app_handler) WHERE app_id IS NOT NULL"
    )
    conn.execute("ALTER TABLE triggers ADD COLUMN IF NOT EXISTS app_id TEXT")
    conn.execute("ALTER TABLE triggers ADD COLUMN IF NOT EXISTS handler TEXT")
    # 2026-09-16: a vendor subscription remembers its target kind (GitHub:
    # repository or organization) so delete and renew render the same
    # vendor URL create did. '' on every pre-existing row = the default kind.
    conn.execute(
        "ALTER TABLE webhook_subscriptions "
        "ADD COLUMN IF NOT EXISTS target_kind TEXT NOT NULL DEFAULT ''"
    )
    # 2026-10-15: GitHub Apps. An account's vendor meta (the installations a
    # GitHub-App account can reach) and an agent's installation identity;
    # '' on every pre-existing row = an OAuth-App or PAT account, the account
    # itself as the service identity. An agent's GitHub reach, on a service
    # binding and on a person's own account choice: '[]' and false = every
    # repository, writes allowed.
    conn.execute(
        "ALTER TABLE user_credential_accounts "
        "ADD COLUMN IF NOT EXISTS meta TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        "ALTER TABLE service_agent_bindings "
        "ADD COLUMN IF NOT EXISTS installation_id TEXT NOT NULL DEFAULT '', "
        "ADD COLUMN IF NOT EXISTS repositories TEXT NOT NULL DEFAULT '[]', "
        "ADD COLUMN IF NOT EXISTS read_only BOOLEAN NOT NULL DEFAULT FALSE"
    )
    conn.execute("ALTER TABLE agent_account_bindings ADD COLUMN IF NOT EXISTS repositories TEXT NOT NULL "
                 "DEFAULT '[]', ADD COLUMN IF NOT EXISTS read_only BOOLEAN NOT NULL DEFAULT FALSE")
    # 2026-07-17: skills adoption — approval requests carry which catalog
    # they target ('mcp' | 'skill'); every pre-existing row is an MCP request.
    conn.execute(
        "ALTER TABLE mcp_assignment_requests "
        "ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'mcp'"
    )
    # 2026-08-29: satellite auto-update rollback budget (per target version).
    conn.execute(
        "ALTER TABLE remote_machines ADD COLUMN IF NOT EXISTS "
        "update_rollback_count INTEGER NOT NULL DEFAULT 0"
    )
    conn.execute(
        "ALTER TABLE remote_machines ADD COLUMN IF NOT EXISTS "
        "update_rollback_target TEXT"
    )
    # 2026-09-04: browser-control own-browser mode — per-machine opt-in plus
    # the encrypted Playwright Extension token.
    conn.execute(
        "ALTER TABLE remote_machines ADD COLUMN IF NOT EXISTS "
        "browser_mode TEXT NOT NULL DEFAULT 'dedicated'"
    )
    conn.execute(
        "ALTER TABLE remote_machines ADD COLUMN IF NOT EXISTS "
        "browser_extension_token_enc TEXT"
    )
    # Unattended runs and a saved own-browser token: added once, and set
    # where a token is already stored, since unattended runs used it until
    # the flag existed (a later choice of the owner is never reset).
    if not _column_exists(conn, "remote_machines", "browser_unattended"):
        conn.execute(
            "ALTER TABLE remote_machines ADD COLUMN IF NOT EXISTS "
            "browser_unattended BOOLEAN NOT NULL DEFAULT FALSE"
        )
        conn.execute(
            "UPDATE remote_machines SET browser_unattended = TRUE "
            "WHERE browser_extension_token_enc IS NOT NULL"
        )
    # 2026-08-12: departments (agents map). Every pre-existing delegation edge
    # is a manual one; the CHECK stays in-CREATE only per convention.
    conn.execute(
        "ALTER TABLE agent_delegation_targets "
        "ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual'"
    )
    # 2026-08-14: D1 observe edge — no-user read grant, off for every
    # pre-existing edge. 2026-08-15: superseded — the edge itself is the
    # grant; the column stays reserved-unused (see the table doc above).
    conn.execute(
        "ALTER TABLE agent_delegation_targets "
        "ADD COLUMN IF NOT EXISTS observe BOOLEAN NOT NULL DEFAULT FALSE"
    )
    conn.execute(
        "ALTER TABLE agents "
        "ADD COLUMN IF NOT EXISTS department_id TEXT NOT NULL DEFAULT ''"
    )
    # 2026-09-25: a department's delegation mode folds the 1.6.x
    # auto_delegation toggle and the never-released direction column into one
    # word. 'both' on a pre-existing row is exactly the symmetric wiring it
    # compiled to before either column existed; a direction the install had
    # carries over; a department switched off stays off (applied last, so it
    # wins over its direction). The two old columns are dropped after the
    # fold, so the live table matches the CREATE (the drift probe compares
    # them) and every step is a no-op once it has run, whichever of the two
    # an install had. The CHECK stays in-CREATE only; the store validates.
    conn.execute(
        "ALTER TABLE departments "
        "ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'both'"
    )
    if _column_exists(conn, "departments", "direction"):
        conn.execute("UPDATE departments SET mode = direction")
        conn.execute("ALTER TABLE departments DROP COLUMN direction")
        logger.info("departments.direction folded into mode")
    if _column_exists(conn, "departments", "auto_delegation"):
        conn.execute(
            "UPDATE departments SET mode = 'off' WHERE NOT auto_delegation"
        )
        conn.execute("ALTER TABLE departments DROP COLUMN auto_delegation")
        logger.info("departments.auto_delegation folded into mode")
    # 2026-09-24: the contributor role widens the two per-agent role CHECKs.
    # CREATE TABLE IF NOT EXISTS never touches an existing table's CHECK and
    # a stale one refuses the first contributor row, so this is the one
    # place an existing install's constraint is rewritten: guarded by the
    # catalog (a no-op once the words are in), DROP+ADD inside a SAVEPOINT,
    # at startup before the pool serves a request — the "never ALTER ADD
    # CONSTRAINT" note on init_schema is about DDL on a busy pool.
    migrate_automation(conn)
    migrate_agents(conn, engines=engines)
    migrate_identity(conn)
    migrate_mcp(conn)
    from storage.remote_schema import migrate_remote
    migrate_remote(conn)
    for table, name, column, words in _ROLE_CHECKS:
        # A column a later block adds (shares.role_cap, with its CHECK inline)
        # is rewritten from its next boot on.
        if not _column_exists(conn, table, column):
            continue
        present = _check_constraint_words(conn, table, name)
        if present is not None and present >= set(words):
            continue
        wanted = ", ".join(f"'{w}'" for w in words)
        conn.execute(f"SAVEPOINT {name}")
        try:
            conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
            conn.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({column} IN ({wanted}))")
            conn.execute(f"RELEASE SAVEPOINT {name}")
            logger.info("role CHECK %s.%s now allows %s", table, name, wanted)
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            logger.exception("role CHECK %s.%s could not be rewritten — will retry next startup",
                             table, name)
    # 2026-08-14: wake word — per-user opt-in (privacy: default OFF for
    # every pre-existing user; enabling is a deliberate act).
    conn.execute(
        "ALTER TABLE user_audio_prefs "
        "ADD COLUMN IF NOT EXISTS wake_word_enabled BOOLEAN NOT NULL DEFAULT FALSE"
    )
    conn.execute(
        "ALTER TABLE agents "
        "ADD COLUMN IF NOT EXISTS department_level_id TEXT NOT NULL DEFAULT ''"
    )
    # 2026-08-16: knowledge libraries carry a display name. Empty on every
    # pre-existing row; the UI falls back to the source agent's name, so an
    # install that upgrades mid-flight needs no data migration.
    conn.execute(
        "ALTER TABLE knowledge_libraries "
        "ADD COLUMN IF NOT EXISTS name TEXT NOT NULL DEFAULT ''"
    )
    # Who started a chat's move (1.9.0): the agent's chain of its own moves
    # is counted by it. A table a pre-release build created lacks it.
    conn.execute("ALTER TABLE chat_moves ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT ''")
    # Capability tier + "good at" line per model row. NULL tier = untiered
    # (admin-added models until an admin tags them); builtins are stamped
    # from the registry by sync_builtin_models at boot and on the admin
    # execution-layer pages (the catalog GET only reads).
    conn.execute(
        "ALTER TABLE execution_layer_models "
        "ADD COLUMN IF NOT EXISTS tier INTEGER"
    )
    conn.execute(
        "ALTER TABLE execution_layer_models "
        "ADD COLUMN IF NOT EXISTS good_at TEXT NOT NULL DEFAULT ''"
    )
    # How many background commands and subagents a task run left running
    # when it completed (0 on every pre-existing row).
    conn.execute(
        "ALTER TABLE task_runs "
        "ADD COLUMN IF NOT EXISTS background_pending INTEGER NOT NULL DEFAULT 0"
    )
    # 2026-09-06: external routes — route identity / role / remember-callers
    # and the call-log audit columns. The defaults match the CREATE (every
    # pre-existing route becomes a per-caller external route, exactly like a
    # new one); the CHECKs stay in-CREATE only per the convention above.
    conn.execute(
        "ALTER TABLE phone_routes "
        "ADD COLUMN IF NOT EXISTS identity_mode TEXT NOT NULL DEFAULT 'caller'"
    )
    conn.execute(
        "ALTER TABLE phone_routes ADD COLUMN IF NOT EXISTS identity_user_sub "
        "TEXT REFERENCES users(sub) ON DELETE SET NULL"
    )
    conn.execute(
        "ALTER TABLE phone_routes "
        "ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'viewer'"
    )
    conn.execute(
        "ALTER TABLE phone_routes "
        "ADD COLUMN IF NOT EXISTS remember_callers BOOLEAN NOT NULL DEFAULT TRUE"
    )
    # 2.0: the agent's own line ('agent', the call runs as the agent itself)
    # and the management tools its route ticks. The identity CHECK an
    # install's CREATE made is widened to the new word, once.
    conn.execute(
        "ALTER TABLE phone_routes ADD COLUMN IF NOT EXISTS line_tools JSONB NOT NULL DEFAULT '[]'"
    )
    _widen_identity_mode_check(conn)
    # 2026-10-10: the call tools' transfer targets and the per-call language
    # identification. Existing routes keep today's behaviour (no targets,
    # detection off); the route form defaults detection on for new routes.
    conn.execute(
        "ALTER TABLE phone_routes "
        "ADD COLUMN IF NOT EXISTS transfer_targets JSONB NOT NULL DEFAULT '[]'"
    )
    conn.execute(
        "ALTER TABLE phone_routes "
        "ADD COLUMN IF NOT EXISTS language_detect BOOLEAN NOT NULL DEFAULT FALSE"
    )
    # 2026-10-12: a voice for the route alone; '' keeps the provider's voice
    # for the call's language, so existing routes change nothing.
    conn.execute(
        "ALTER TABLE phone_routes "
        "ADD COLUMN IF NOT EXISTS voice_id TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        "ALTER TABLE phone_call_log "
        "ADD COLUMN IF NOT EXISTS session_id TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        "ALTER TABLE phone_call_log "
        "ADD COLUMN IF NOT EXISTS identity TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        "ALTER TABLE phone_call_log "
        "ADD COLUMN IF NOT EXISTS tools_run JSONB NOT NULL DEFAULT '[]'"
    )
    # 2.0: the record of every phone-owned conversation an install holds,
    # its line read from the call log's labels above.
    backfill_conversation_records(conn)
    # 2026-07-13: cron day-of-week convention fix. Stored 5-field schedules
    # used to reach APScheduler verbatim, whose numeric day-of-week is
    # 0=Monday; the platform now stores/accepts STANDARD cron (0=Sunday) and
    # remaps at trigger build (services/scheduler/scheduler_triggers.py).
    # Rewrite pre-existing rows one day forward so every schedule keeps
    # firing on the SAME weekdays it always fired on — the display strings
    # change, the fire behavior must not. One-shot (platform_settings flag);
    # SAVEPOINT so a surprise row can't abort schema init — without the flag
    # the rewrite simply retries next startup.
    _MIGRATION_FLAG = "cron_dow_standardized"
    done = conn.execute(
        "SELECT 1 FROM platform_settings WHERE key = %s", (_MIGRATION_FLAG,)
    ).fetchone()
    if not done:
        conn.execute("SAVEPOINT cron_dow_std")
        try:
            for table in ("dynamic_tasks", "notifications"):
                rows = conn.execute(
                    f"SELECT id, schedule FROM {table} "
                    "WHERE schedule IS NOT NULL AND schedule != ''"
                ).fetchall()
                for r in rows:
                    fields = (r["schedule"] or "").split()
                    if len(fields) != 5:
                        continue
                    remapped = apscheduler_dow_to_standard(fields[4])
                    if remapped == fields[4]:
                        continue
                    fields[4] = remapped
                    conn.execute(
                        f"UPDATE {table} SET schedule = %s WHERE id = %s",
                        (" ".join(fields), r["id"]),
                    )
                    logger.info(
                        "cron dow migration: %s %s day-of-week rewritten to "
                        "standard convention", table, r["id"],
                    )
            conn.execute(
                "INSERT INTO platform_settings (key, value) VALUES (%s, 'done') "
                "ON CONFLICT (key) DO NOTHING", (_MIGRATION_FLAG,),
            )
            conn.execute("RELEASE SAVEPOINT cron_dow_std")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT cron_dow_std")
            logger.exception("cron dow migration failed — will retry next startup")

    _hold_windows_hash_deletes(conn)

    # Tasks seeded from an agent template or an app bundle were pinned to the
    # literal 'UTC' until 2026-09-19; the seeders now leave an agent-scope
    # row NULL so it follows the platform timezone. There is deliberately NO
    # migration for the rows already written: no published template or
    # bundle ever carried a task item (the public catalog ships
    # notifications only), so such rows exist only on installs that authored
    # their own templates — the internal install's three were converted by
    # hand (operator decision 2026-09-19, the knowledge-libraries precedent
    # below).

    # Per-folder knowledge libraries (2026-08-24) ship with composite
    # (source_agent, subdir[, consumer_agent]) keys from birth: the
    # single-key v1 shape never reached ANY released install (public
    # latest at the time was 1.4.0, which predates libraries entirely),
    # so there is deliberately NO migration — the dev installs that
    # carried v1 rows were reset by hand (operator decision 2026-08-24:
    # drop the two tables before upgrading, recreate shares in the UI).

    # The offboarding transfer record (services/agents/offboarding_transfer.py):
    # empty on every pre-existing row, which is exactly a row that never
    # changed hands.
    for table in ("dynamic_tasks", "triggers", "notifications"):
        conn.execute(
            f"ALTER TABLE {table} "
            "ADD COLUMN IF NOT EXISTS transferred_from TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            f"ALTER TABLE {table} "
            "ADD COLUMN IF NOT EXISTS transferred_at TEXT NOT NULL DEFAULT ''"
        )

    # The per-person token epoch (1.7.1, auth/providers.py): NULL on every
    # pre-existing row = never bumped, so nothing signed in before the
    # upgrade is refused.
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS token_epoch_at TEXT")

    # The TOTP step accepted last (1.8.0, auth/totp.py): 0 on every
    # pre-existing row, so the first code after the upgrade passes. A constant
    # default adds the column without rewriting the table.
    conn.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_last_step BIGINT NOT NULL DEFAULT 0"
    )
    migrate_sharing(conn)


# ---------------------------------------------------------------------------
# Schema-drift guard (log-only)
# ---------------------------------------------------------------------------

_DRIFT_PROBE_SCHEMA = "otodock_drift_probe"


def check_schema_drift(conn) -> list[tuple[str, str]]:
    """Log-only boot guard: live tables missing columns the code expects.

    ``init_schema``'s CREATE TABLE IF NOT EXISTS silently no-ops on existing
    tables, so a column added to a CREATE TABLE without a matching
    ``run_migrations`` ALTER never reaches a pre-existing install — its
    endpoints then 500 at query time with UndefinedColumn and nothing points
    at the cause. This builds the expected schema in a scratch pg schema
    inside a rolled-back transaction and diffs information_schema against the
    live one. Never raises; returns the (table, column) drift list after
    logging it loudly.
    """
    drift: list[tuple[str, str]] = []
    stale: list[tuple[str, str]] = []
    try:
        live_schema = conn.execute(
            "SELECT current_schema() AS cs"
        ).fetchone()["cs"]
        conn.execute(f"DROP SCHEMA IF EXISTS {_DRIFT_PROBE_SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {_DRIFT_PROBE_SCHEMA}")
        # SET LOCAL: scoped to this (never-committed) transaction, so the
        # probe DDL below lands in the scratch schema and every trace of it
        # vanishes on the rollback in `finally`.
        conn.execute(f"SET LOCAL search_path TO {_DRIFT_PROBE_SCHEMA}")
        init_schema(conn)
        rows = conn.execute(
            """
            SELECT c1.table_name AS t, c1.column_name AS c
            FROM information_schema.columns c1
            WHERE c1.table_schema = %s
              AND EXISTS (SELECT 1 FROM information_schema.tables tb
                          WHERE tb.table_schema = %s
                            AND tb.table_name = c1.table_name)
              AND NOT EXISTS (SELECT 1 FROM information_schema.columns c2
                              WHERE c2.table_schema = %s
                                AND c2.table_name = c1.table_name
                                AND c2.column_name = c1.column_name)
            ORDER BY c1.table_name, c1.column_name
            """,
            (_DRIFT_PROBE_SCHEMA, live_schema, live_schema),
        ).fetchall()
        drift = [(r["t"], r["c"]) for r in rows]
        # The role CHECKs run_migrations rewrites: the probe's words are the
        # code's, the live words the install's.
        checks = [(table, name) for table, name, _c, _w in _ROLE_CHECKS]
        checks += [("shares", name) for name, _d, _w in _SHARE_CHECKS]
        for table, name in checks:
            expected = _check_constraint_words(conn, table, name)
            live = _check_constraint_words(conn, table, name, live_schema)
            if expected is not None and live != expected:
                stale.append((table, f"CHECK {name}"))
    except Exception as e:  # noqa: BLE001  # the boot's drift probe must never take the boot down with it
        logger.debug("schema drift probe skipped: %s", e)
    finally:
        with contextlib.suppress(Exception):
            conn.rollback()  # discard the probe schema + reset search_path
    if drift:
        logger.error(
            "SCHEMA DRIFT: the live database is missing %d column(s) the "
            "code expects: %s. This DB predates those columns and no forward "
            "migration covers them (pre-release installs were hand-converged, "
            "not migrated). Add the matching ALTER TABLE ... ADD COLUMN, or "
            "file it in run_migrations(). Affected endpoints will return "
            "HTTP 500 (UndefinedColumn) until fixed.",
            len(drift),
            ", ".join(f"{t}.{c}" for t, c in drift),
        )
    if stale:
        logger.error(
            "SCHEMA DRIFT: %d CHECK constraint(s) on the live database "
            "differ from the code's: %s. run_migrations() rewrites them at "
            "startup; a row with a newer word is refused until it has.",
            len(stale),
            ", ".join(f"{t}.{c}" for t, c in stale),
        )
    return drift + stale

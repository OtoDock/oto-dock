"""PostgreSQL schema entry points and the DDL of the flat storage domains.

``init_schema`` (called once at startup, then by the test conftest) runs one
``init_*`` function per domain in foreign-key order. The functions live next
to the stores of their tables: ``storage/<domain>/schema.py`` for the ten
sub-packages, this module for the flat domains (remote machines, pinned
apps, storage quotas). ``run_migrations`` carries the forward migrations for
pre-existing installs and ``check_schema_drift`` is the boot guard for a
column that never got one. Every CREATE TABLE / CREATE INDEX uses IF NOT
EXISTS so re-runs are safe (the startup path is idempotent).
"""

import contextlib
import logging
import re

from auth import roles
from storage.agents.schema import init_agents, init_departments, init_memory
from storage.automation.schema import (
    init_notifications,
    init_push,
    init_tasks,
    init_triggers,
    init_webhooks,
)
from storage.billing.schema import init_execution_layers, init_usage
from storage.chat.schema import init_chats, init_meetings
from storage.checks.schema import init_checks
from storage.files.schema import (
    init_file_sync,
    init_file_transfers,
    init_pinned_files,
    init_recover_bin,
)
from storage.identity.schema import init_identity
from storage.knowledge.schema import init_knowledge_libraries
from storage.mcp.schema import init_community_requests, init_mcp, init_mcp_autoupdate
from storage.phone.schema import init_audio_telephony, init_phone_call_log
from storage.schema_base import _drop_invalid_indexes
from storage.schema_base import _index_exists  # noqa: F401  (offered to migrations, DATABASE-SCHEMA.md)
from storage.sharing.schema import init_shares

logger = logging.getLogger(__name__)

# The per-agent role CHECKs ``run_migrations`` keeps current on an existing
# install: (table, constraint name, column, the words). The CREATEs spell the
# same words and ``tests/auth/test_roles.py`` pins both to ``auth/roles``.
_ROLE_CHECKS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("user_agents", "user_agents_agent_role_check", "agent_role", roles.AGENT_ROLES),
    ("agents", "agents_default_for_new_users_role_check", "default_for_new_users_role",
     ("",) + tuple(reversed(roles.AGENT_ROLES))),
)


def _check_constraint_words(conn, table: str, name: str, schema: str | None = None) -> set[str] | None:
    """The quoted words of a CHECK constraint as the catalog renders it
    (``current_schema()`` unless ``schema`` names another); None when the
    constraint does not exist."""
    row = conn.execute(
        "SELECT pg_get_constraintdef(c.oid) AS d FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE c.conname = %s AND t.relname = %s "
        "AND n.nspname = COALESCE(%s, current_schema())",
        (name, table, schema),
    ).fetchone()
    return set(re.findall(r"'([^']*)'", row["d"])) if row else None


def _column_exists(conn, table: str, column: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s "
        "AND column_name = %s",
        (table, column),
    ).fetchone() is not None


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
            registered_by TEXT NOT NULL,
            -- 'admin' = paired via /v1/admin/remote-machines/pair (platform
            -- infrastructure, can be agent-scope default for any agent).
            -- 'user'  = paired via /v1/users/me/remote-machines/pair
            -- (personal, only for the owner's user-scope chats/tasks).
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


def init_pinned_apps(conn) -> None:
    """Pinned apps registry (standing agent-authored dashboards)."""
    # One row per pinned app (``storage/db_apps.py``). The HTML content is a
    # workspace file at ``rel_path`` (agent-dir-relative) — the row is the
    # registry: identity, tab order, and the user-approved declared-actions
    # manifest. ``owner_sub`` is NULL for shared rows — a deliberate deviation
    # from the ``''`` convention (media_tokens): the users-FK CASCADE that
    # cleans personal rows on user delete needs NULL, not ''. ``username`` is
    # the filesystem slug ('' = shared) used for path anchoring and the
    # per-scope UNIQUE. ``actions_approved_sig`` holds sha256(canonical
    # manifest JSON) at approval time — any manifest change breaks it, so
    # actions stay dead until re-approved. ``hidden`` is the dashboard-side
    # SOFT unpin: the row (manifest + approval) survives so an agent re-pin
    # of the same slug restores the app without re-approval; the agent-side
    # unpin_app hook is the hard delete. ``scope_chat_id`` /
    # ``scope_project_id`` (at most one set — the Dock feature): a scoped row
    # is a per-chat / per-project pinned dashboard shown on the chat's Dock
    # overlay instead of the standing apps strip; exactly one per scope
    # (partial unique indexes — created in ``run_migrations`` because a
    # pre-existing install runs this CREATE-IF-NOT-EXISTS before the columns
    # exist). Inline constraints only (NEVER ``ALTER ADD CONSTRAINT`` —
    # conftest pool deadlock), so pre-existing installs get the scope XOR
    # from the store layer, not the CHECK.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pinned_apps (
            id            TEXT PRIMARY KEY,
            agent         TEXT NOT NULL,
            owner_sub     TEXT REFERENCES users(sub) ON DELETE CASCADE,
            username      TEXT NOT NULL DEFAULT '',
            slug          TEXT NOT NULL,
            title         TEXT NOT NULL DEFAULT '',
            rel_path      TEXT NOT NULL,
            actions       TEXT NOT NULL DEFAULT '[]',
            actions_approved_sig TEXT NOT NULL DEFAULT '',
            approved_by   TEXT NOT NULL DEFAULT '',
            position      INTEGER NOT NULL DEFAULT 0,
            hidden        BOOLEAN NOT NULL DEFAULT FALSE,
            scope_chat_id    TEXT,
            scope_project_id TEXT,
            -- The release copy viewers are served (agent-dir-relative path
            -- under app-releases/ and its sha256); empty = not deployed
            -- yet, the working file serves (the upgrade path).
            release_path  TEXT NOT NULL DEFAULT '',
            release_sha256 TEXT NOT NULL DEFAULT '',
            -- Folder apps (APPS.md): 'file' (one html) or 'folder' (a
            -- release directory, a server); the deploy state is idle or
            -- pending ("deploying" is the in-process lock, never stored);
            -- the per-app approval switch and the release waiting for it;
            -- the two manifest blocks the runtime signs with the actions.
            kind          TEXT NOT NULL DEFAULT 'file',
            deploy_state  TEXT NOT NULL DEFAULT 'idle',
            deploy_requires_approval BOOLEAN NOT NULL DEFAULT FALSE,
            pending_release INTEGER NOT NULL DEFAULT 0,
            files         TEXT NOT NULL DEFAULT '',
            egress        TEXT NOT NULL DEFAULT '',
            -- APPS.md "Handlers" / "Bindings": three more signed blocks,
            -- and `requires` (what the app needs to work), which grants
            -- nothing and is therefore not part of the signature.
            handlers      TEXT NOT NULL DEFAULT '',
            exports       TEXT NOT NULL DEFAULT '',
            bindings      TEXT NOT NULL DEFAULT '',
            requires      TEXT NOT NULL DEFAULT '',
            -- APPS.md "Steps": the scripts handlers run instead of the
            -- server route, with their content hashes; signed.
            steps         TEXT NOT NULL DEFAULT '',
            -- APPS.md "Secrets": the declared secret names, whether each
            -- is required and where the platform sends it; signed. The
            -- values live in app_secrets, never here.
            secrets       TEXT NOT NULL DEFAULT '',
            -- APPS.md "Inbound hooks": the public routes a vendor may call,
            -- the scheme and the secret each is verified with, the handler
            -- each wakes; signed.
            inbound       TEXT NOT NULL DEFAULT '',
            -- APPS.md "External links": what a link may do — the hosts the
            -- host page may open, the paths that need a challenge, the
            -- session cookie's lifetime; signed.
            external      TEXT NOT NULL DEFAULT '',
            -- COMMUNITY-AGENTS-REGISTRY.md "Per-user template apps": the
            -- template and app slug a seeded row came from ('' = the
            -- member's own app, never touched by a seed or an update), and
            -- why a seeded row is hidden ('opted_out' = the owner purged or
            -- unpinned it, never re-seeded; 'removed' = a membership
            -- removal, restored on re-attach).
            template_ref  TEXT NOT NULL DEFAULT '',
            template_state TEXT NOT NULL DEFAULT '',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            UNIQUE (agent, username, slug),
            CHECK (scope_chat_id IS NULL OR scope_project_id IS NULL)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pinned_apps_agent ON pinned_apps (agent)"
    )


def init_pinned_app_user_hides(conn) -> None:
    """Per-user hides of SHARED pinned apps (S2 of the shared-dashboards
    unit): a viewer removing a team dashboard from THEIR OWN strip must not
    hide it for the team — ``pinned_apps.hidden`` is the global soft-unpin
    (editor+ only), this table is the personal one (any role). One row =
    "this user hid this shared app for themselves"; restore = delete the
    row. Personal pins never get rows here (they already belong to one
    user). Both FKs CASCADE so app hard-deletes and user deletes reap
    hides for free. Whole-table CREATE in init (new-table pattern —
    converges existing installs on startup, no migration needed)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pinned_app_user_hides (
            app_id     TEXT NOT NULL REFERENCES pinned_apps(id) ON DELETE CASCADE,
            user_sub   TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (app_id, user_sub)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_app_user_hides_user "
        "ON pinned_app_user_hides (user_sub)"
    )


def init_app_state(conn) -> None:
    """The per-app state document (APPS.md "Live apps"): one JSON
    object per pinned app that the agent writes and every viewer's page
    reads, so an app's data changes without a re-pin of its HTML. ``doc``
    is the canonical JSON text (TEXT like ``pinned_apps.actions``; nothing
    queries inside it), ``rev`` grows by one per write so a page can drop
    a stale delivery. The FK CASCADE ties it to the row: a hard unpin, an
    agent delete or a user delete reaps it; a soft unpin keeps the row and
    therefore the state. Whole-table CREATE in init (new-table pattern)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_state (
            app_id     TEXT PRIMARY KEY REFERENCES pinned_apps(id) ON DELETE CASCADE,
            doc        TEXT NOT NULL DEFAULT '{}',
            rev        INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT ''
        )
    """)


def init_app_handler_deliveries(conn) -> None:
    """A handler fire that must not be lost (APPS.md "Handlers"): one row
    per wake of an app's server, written before anything wakes, claimed by
    the drain with a lease (`inflight`, `next_at` = the lease end), retried
    on 5xx and timeouts, `dead` on 4xx or after the last attempt. The FK
    CASCADE ties it to the app row like `app_state`. `event_id` carries a
    caller's idempotency key (`X-OtoDock-Event-Id`, or the emitter's
    sequence for a cross-app event); the partial unique index makes a
    replay a no-op. Whole-table CREATE in init (new-table pattern)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_handler_deliveries (
            id          TEXT PRIMARY KEY,
            app_id      TEXT NOT NULL REFERENCES pinned_apps(id) ON DELETE CASCADE,
            handler     TEXT NOT NULL,
            event       TEXT NOT NULL,
            payload     JSONB NOT NULL DEFAULT '{}',
            event_id    TEXT NOT NULL DEFAULT '',
            trigger_id  TEXT,
            task_run_id TEXT,
            attempts    INTEGER NOT NULL DEFAULT 0,
            status      TEXT NOT NULL DEFAULT 'pending',
            next_at     TEXT NOT NULL,
            claimed_at  TEXT,
            done_at     TEXT,
            last_error  TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL,
            -- APPS.md "Steps" (2026-09-17): a step's verdict — the exit
            -- code, the first 32 KB of its output, where it ran (`local`
            -- or a machine id) and when it started.
            exit_code   INTEGER,
            output      TEXT NOT NULL DEFAULT '',
            ran_on      TEXT NOT NULL DEFAULT '',
            started_at  TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_app_deliveries_due "
        "ON app_handler_deliveries (status, next_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_app_deliveries_app "
        "ON app_handler_deliveries (app_id, status)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_app_deliveries_event "
        "ON app_handler_deliveries (app_id, handler, event_id) WHERE event_id <> ''"
    )


def init_app_secrets(conn) -> None:
    """The values behind an app's declared secrets (APPS.md "Secrets"): one
    row per (app, name), the value Fernet-encrypted with the credential
    store's key, who set it and when — never the value in clear, never in a
    manifest, a release or a synced tree. The FK CASCADE ties it to the app
    row: a hard unpin or a purge drops the values with the row, a soft unpin
    keeps them. Whole-table CREATE in init (new-table pattern)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_secrets (
            app_id      TEXT NOT NULL REFERENCES pinned_apps(id) ON DELETE CASCADE,
            name        TEXT NOT NULL,
            value_enc   TEXT NOT NULL,
            set_by      TEXT NOT NULL DEFAULT '',
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (app_id, name)
        )
    """)


# ---------------------------------------------------------------------------
# Schema entry points
# ---------------------------------------------------------------------------

def init_schema(conn) -> None:
    """Create every table and index if absent (idempotent; safe each boot)."""
    init_tasks(conn)
    init_identity(conn)
    init_chats(conn)
    init_usage(conn)
    init_mcp(conn)
    init_agents(conn)
    init_departments(conn)
    init_storage_quotas(conn)
    init_community_requests(conn)
    init_audio_telephony(conn)
    init_phone_call_log(conn)
    init_remote_machines(conn)
    init_meetings(conn)
    init_execution_layers(conn)
    init_notifications(conn)
    init_mcp_autoupdate(conn)
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
    logger.info("PostgreSQL schema initialized (all tables created)")


# ---------------------------------------------------------------------------
# Migrations (idempotent column additions)
# ---------------------------------------------------------------------------

def run_migrations(conn) -> None:
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

    The pre-launch transitional migrations that used to live here (ssh-server
    → ssh-hosts rename, users invite-column drops, phone ambience column,
    agent_remote_targets backfill) were removed once every existing install
    converged — pre-launch DBs are hand-converged, not migrated forever.
    """
    # 2026-07-10: dashboard soft-unpin for pinned apps (ADD COLUMN
    # IF NOT EXISTS is idempotent — no information_schema probe needed).
    conn.execute(
        "ALTER TABLE pinned_apps "
        "ADD COLUMN IF NOT EXISTS hidden BOOLEAN NOT NULL DEFAULT FALSE"
    )
    # 2026-09-12: a notification row may carry its own dashboard path (a
    # shared app or chat); empty on every pre-existing row = the old deep link.
    conn.execute(
        "ALTER TABLE notification_deliveries "
        "ADD COLUMN IF NOT EXISTS href TEXT NOT NULL DEFAULT ''"
    )
    # 2026-09-12: app releases. Empty on every pre-existing row = the
    # working file keeps serving until the app is pinned again.
    conn.execute(
        "ALTER TABLE pinned_apps ADD COLUMN IF NOT EXISTS release_path TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        "ALTER TABLE pinned_apps ADD COLUMN IF NOT EXISTS release_sha256 TEXT NOT NULL DEFAULT ''"
    )
    # 2026-09-13: folder apps (APPS.md). Every pre-existing row is a
    # single-file app with nothing pending and no extra manifest blocks.
    for ddl in (
        "kind TEXT NOT NULL DEFAULT 'file'",
        "deploy_state TEXT NOT NULL DEFAULT 'idle'",
        "deploy_requires_approval BOOLEAN NOT NULL DEFAULT FALSE",
        "pending_release INTEGER NOT NULL DEFAULT 0",
        "files TEXT NOT NULL DEFAULT ''",
        "egress TEXT NOT NULL DEFAULT ''",
    ):
        conn.execute(f"ALTER TABLE pinned_apps ADD COLUMN IF NOT EXISTS {ddl}")
    # 2026-09-13: handlers, exports, bindings and requires (APPS.md
    # "Handlers", "Bindings"). Empty on every pre-existing row, so no
    # approval changes: the signature covers a block only once it holds
    # something.
    for ddl in (
        "handlers TEXT NOT NULL DEFAULT ''",
        "exports TEXT NOT NULL DEFAULT ''",
        "bindings TEXT NOT NULL DEFAULT ''",
        "requires TEXT NOT NULL DEFAULT ''",
    ):
        conn.execute(f"ALTER TABLE pinned_apps ADD COLUMN IF NOT EXISTS {ddl}")
    # 2026-09-17: app steps (APPS.md "Steps"), one more signed block; empty
    # on every pre-existing row, so no approval changes. The delivery table
    # gains the step's verdict columns the same day.
    conn.execute("ALTER TABLE pinned_apps ADD COLUMN IF NOT EXISTS steps TEXT NOT NULL DEFAULT ''")
    for ddl in (
        "exit_code INTEGER",
        "output TEXT NOT NULL DEFAULT ''",
        "ran_on TEXT NOT NULL DEFAULT ''",
        "started_at TEXT",
    ):
        conn.execute(f"ALTER TABLE app_handler_deliveries ADD COLUMN IF NOT EXISTS {ddl}")
    # 2026-09-18: app secrets (APPS.md "Secrets"), one more signed block;
    # empty on every pre-existing row, so no approval changes. The values
    # table is created by init_app_secrets.
    conn.execute("ALTER TABLE pinned_apps ADD COLUMN IF NOT EXISTS secrets TEXT NOT NULL DEFAULT ''")
    # 2026-09-18: inbound hooks (APPS.md "Inbound hooks"), one more signed
    # block; empty on every pre-existing row.
    conn.execute("ALTER TABLE pinned_apps ADD COLUMN IF NOT EXISTS inbound TEXT NOT NULL DEFAULT ''")
    # 2026-09-18: what a link may do (APPS.md "External links"), one more
    # signed block; empty on every pre-existing row.
    conn.execute("ALTER TABLE pinned_apps ADD COLUMN IF NOT EXISTS external TEXT NOT NULL DEFAULT ''")
    # 2026-09-19: template-seeded apps (COMMUNITY-AGENTS-REGISTRY.md). Empty
    # on every pre-existing row = the member's own app.
    conn.execute("ALTER TABLE pinned_apps ADD COLUMN IF NOT EXISTS template_ref TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE pinned_apps ADD COLUMN IF NOT EXISTS template_state TEXT NOT NULL DEFAULT ''")
    # 2026-09-17: checks (CHECKS.md). The checks attached to a chat (a JSON
    # list of refs, '' = none) and to a task; empty on every pre-existing
    # row. The three checks tables are new tables (init_checks).
    conn.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS checks TEXT NOT NULL DEFAULT ''")
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
    # 2026-07-11: chat/project-scoped pins (the Dock). The one-per-scope
    # partial unique indexes live HERE, not in init_pinned_apps: on a
    # pre-existing install the CREATE-IF-NOT-EXISTS no-ops before these
    # columns exist, so an index in init would reference a missing column.
    # The scope XOR CHECK stays in-CREATE only (never ALTER ADD CONSTRAINT).
    conn.execute(
        "ALTER TABLE pinned_apps ADD COLUMN IF NOT EXISTS scope_chat_id TEXT"
    )
    conn.execute(
        "ALTER TABLE pinned_apps ADD COLUMN IF NOT EXISTS scope_project_id TEXT"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pinned_apps_scope_chat "
        "ON pinned_apps (scope_chat_id) WHERE scope_chat_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pinned_apps_scope_project "
        "ON pinned_apps (scope_project_id) WHERE scope_project_id IS NOT NULL"
    )
    # 2026-07-18: durable delegate-wake replay — undeliverable delegate
    # results are stored on the parent chat and injected at its next
    # warmup/turn.
    conn.execute(
        "ALTER TABLE chats "
        "ADD COLUMN IF NOT EXISTS pending_delegate_wake TEXT NOT NULL DEFAULT ''"
    )
    # The chats with a parked wake (startup and every satellite reconnect);
    # here, not in init_chats: the column itself arrives by this migration.
    _drop_invalid_indexes(conn, "idx_chats_pending_wake")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chats_pending_wake ON chats (id) "
        "WHERE pending_delegate_wake <> ''"
    )
    # The engine-reported running cost total at the last recorded turn and
    # the engine session it belongs to. Claude Code ≥ 2.1.277 resumes
    # a session from its saved total, and the pump's per-turn delta needs the
    # baseline across a proxy restart: the in-memory map made durable, keyed
    # by session id exactly like it (a fresh session on the chat starts at 0).
    # 0 / '' on every pre-existing row.
    conn.execute(
        "ALTER TABLE chats "
        "ADD COLUMN IF NOT EXISTS engine_cost_total DOUBLE PRECISION NOT NULL DEFAULT 0"
    )
    conn.execute(
        "ALTER TABLE chats "
        "ADD COLUMN IF NOT EXISTS engine_cost_session_id TEXT NOT NULL DEFAULT ''"
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
    for table, name, column, words in _ROLE_CHECKS:
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
    # 2026-08-16: per-task model + engine override. Empty on every
    # pre-existing row = inherit the agent's default, which is exactly what
    # those tasks did before the column existed.
    conn.execute(
        "ALTER TABLE dynamic_tasks "
        "ADD COLUMN IF NOT EXISTS override_model TEXT DEFAULT ''"
    )
    conn.execute(
        "ALTER TABLE dynamic_tasks "
        "ADD COLUMN IF NOT EXISTS override_execution_path TEXT DEFAULT ''"
    )
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
        from services.scheduler.scheduler_triggers import apscheduler_dow_to_standard
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
        for table, name, _column, _words in _ROLE_CHECKS:
            expected = _check_constraint_words(conn, table, name)
            live = _check_constraint_words(conn, table, name, live_schema)
            if expected is not None and live != expected:
                stale.append((table, f"CHECK {name}"))
    except Exception as e:  # the guard must never take the boot down with it
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
            "SCHEMA DRIFT: %d role CHECK constraint(s) on the live database "
            "differ from the code's: %s. run_migrations() rewrites them at "
            "startup; a row with the newer role word is refused until it has.",
            len(stale),
            ", ".join(f"{t}.{c}" for t, c in stale),
        )
    return drift + stale

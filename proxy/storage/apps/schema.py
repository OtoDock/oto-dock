"""Schema for the apps domain: the pinned apps registry, the per-person hides,
the state document, handler deliveries and secrets; their forward migrations in
``migrate_apps``.

Called by ``storage.schema`` (``init_schema`` in foreign-key order, ``migrate_apps``
from ``run_migrations``); every statement is idempotent.
"""


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
            from_template TEXT NOT NULL DEFAULT '',
            from_version  TEXT NOT NULL DEFAULT '',
            follows       TEXT NOT NULL DEFAULT '',
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


def migrate_apps(conn) -> None:
    """The apps domain's forward migrations on an existing install: the columns
    each app feature added to ``pinned_apps`` and ``app_handler_deliveries``,
    and the one-per-scope indexes on two of them."""
    # 2026-07-10: dashboard soft-unpin for pinned apps (ADD COLUMN
    # IF NOT EXISTS is idempotent — no information_schema probe needed).
    conn.execute(
        "ALTER TABLE pinned_apps "
        "ADD COLUMN IF NOT EXISTS hidden BOOLEAN NOT NULL DEFAULT FALSE"
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
    # Template-seeded apps, a bundle's origin and (1.9.1) the source a copy follows; empty on old rows.
    for column in ("template_ref", "template_state", "from_template", "from_version", "follows"):
        conn.execute(f"ALTER TABLE pinned_apps ADD COLUMN IF NOT EXISTS {column} TEXT NOT NULL DEFAULT ''")
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

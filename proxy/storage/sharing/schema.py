"""Schema for the sharing domain: the ``shares`` table and the placement
hides (SHARING.md).

Called by ``storage.schema.init_schema`` after the pinned apps (an app share
references its row) and the chats; every statement is idempotent. See
``docs/architecture/DATABASE-SCHEMA.md``. The table shipped in 1.7.0 with
one grantee kind: the columns, CHECKs and indexes of the three-kind model
reach an existing install through ``storage.schema.run_migrations``, which
is also where every index on a migrated column is created (the CREATE below
no-ops on an existing table, so an index here would name a missing column).
"""

# The named CHECKs ``run_migrations`` keeps current on an existing install
# (the 1.7.0 scope CHECK was auto-named; these are spelled here and found by
# name in the catalog).
GRANTEE_CHECK = (
    "(scope = 'external' AND grantee_sub IS NULL AND grantee_agent IS NULL"
    " AND grantee_department IS NULL)"
    " OR (scope = 'internal' AND grantee_kind = 'person' AND grantee_sub IS NOT NULL"
    " AND grantee_agent IS NULL AND grantee_department IS NULL)"
    " OR (scope = 'internal' AND grantee_kind = 'agent' AND target_kind = 'app'"
    " AND grantee_agent IS NOT NULL AND grantee_agent <> ''"
    " AND grantee_sub IS NULL AND grantee_department IS NULL)"
    " OR (scope = 'internal' AND grantee_kind = 'department' AND target_kind = 'app'"
    " AND grantee_department IS NOT NULL AND grantee_department <> ''"
    " AND grantee_sub IS NULL AND grantee_agent IS NULL)"
)
GRANTEE_KIND_CHECK = "grantee_kind IN ('', 'person', 'agent', 'department')"
DECISION_CHECK = "decision IN ('', 'pending', 'accepted', 'declined')"
# The frozen spelling of the per-agent roles (``auth/roles.AGENT_ROLES`` is
# built from the CHECKs, never the other way round; ``tests/auth/test_roles.py``).
ROLE_CAP_CHECK = "role_cap IN ('manager', 'editor', 'contributor', 'viewer')"


def init_shares(conn) -> None:
    """Shares of apps and chats: internal shares to a person, an agent or a
    department, and external links; the per-agent placement hides."""
    # One row per share (``storage/sharing/share_store.py``). The target is
    # one of two nullable FK columns — Postgres cannot point one column at
    # two tables — and the CHECK ties the column to ``target_kind``; the
    # three delete sites (hard unpin, chat delete, user delete) cascade the
    # rows through the FKs. Internal: ``grantee_kind`` says which of
    # ``grantee_sub`` (a person), ``grantee_agent`` (an agent's slug, no FK
    # like ``pinned_apps.agent``) or ``grantee_department`` (an id, no FK)
    # names the recipient; ``role_cap`` is the role the sharer allows;
    # ``decision`` is the recipient's answer (``pending`` until a person
    # accepts or declines an app; a chat, an agent share and a department
    # share are ``accepted`` when made), with ``decided_by`` and
    # ``decided_at``; ``placed_agent`` is the agent a person accepted an app
    # into; ``hidden_by_grantee`` is a person's own "hide for me". External:
    # ``token_hash`` is sha256 of the link token, ``password_hash`` is empty
    # for a public link, ``allow_actions`` is the single Buttons switch,
    # ``actions_today``/``actions_day`` count the per-share daily cap; the
    # kind and the decision are empty. ``state`` is ``suspended`` while the
    # app is soft-unpinned and nothing else. The defaults ``person`` and
    # ``pending`` let a 1.7.0 INSERT, which names neither, pass after a
    # downgrade. Chat snapshots keep their directory in ``snapshot_ref``.
    # Timestamps are ISO text like ``pinned_apps``. Inline constraints only
    # (never ``ALTER ADD CONSTRAINT`` outside run_migrations).
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS shares (
            id                 TEXT PRIMARY KEY,
            target_kind        TEXT NOT NULL CHECK (target_kind IN ('app', 'chat')),
            app_id             TEXT REFERENCES pinned_apps(id) ON DELETE CASCADE,
            chat_id            TEXT REFERENCES chats(id) ON DELETE CASCADE,
            scope              TEXT NOT NULL CHECK (scope IN ('internal', 'external')),
            grantee_kind       TEXT NOT NULL DEFAULT 'person',
            grantee_sub        TEXT REFERENCES users(sub) ON DELETE CASCADE,
            grantee_agent      TEXT,
            grantee_department TEXT,
            role_cap           TEXT NOT NULL DEFAULT 'viewer',
            decision           TEXT NOT NULL DEFAULT 'pending',
            decided_by         TEXT,
            decided_at         TEXT,
            placed_agent       TEXT,
            hidden_by_grantee  BOOLEAN NOT NULL DEFAULT FALSE,
            token_hash         TEXT UNIQUE,
            password_hash      TEXT NOT NULL DEFAULT '',
            public             BOOLEAN NOT NULL DEFAULT FALSE,
            allow_actions      BOOLEAN NOT NULL DEFAULT FALSE,
            include_tools      BOOLEAN NOT NULL DEFAULT FALSE,
            state              TEXT NOT NULL DEFAULT 'active'
                               CHECK (state IN ('active', 'suspended')),
            expires_at         TEXT,
            revoked_at         TEXT,
            created_by         TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
            created_at         TEXT NOT NULL,
            last_access_at     TEXT,
            access_count       INTEGER NOT NULL DEFAULT 0,
            actions_today      INTEGER NOT NULL DEFAULT 0,
            actions_day        TEXT NOT NULL DEFAULT '',
            snapshot_ref       TEXT NOT NULL DEFAULT '',
            CHECK ((target_kind = 'app' AND app_id IS NOT NULL AND chat_id IS NULL)
                OR (target_kind = 'chat' AND chat_id IS NOT NULL AND app_id IS NULL)),
            CONSTRAINT shares_grantee_check CHECK ({GRANTEE_CHECK}),
            CONSTRAINT shares_grantee_kind_check CHECK ({GRANTEE_KIND_CHECK}),
            CONSTRAINT shares_decision_check CHECK ({DECISION_CHECK}),
            CONSTRAINT shares_role_cap_check CHECK ({ROLE_CAP_CHECK})
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_app ON shares (app_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_chat ON shares (chat_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_grantee ON shares (grantee_sub)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_created_by ON shares (created_by)")
    # A member's own hide of a placed app in ONE agent, whichever share
    # placed it there (an agent share and a department share of the same
    # app hide once; a department placement hidden here stays visible in
    # the department's other agents). ``agent`` has no FK, like
    # ``pinned_apps.agent``; ``delete_agent`` removes the rows.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS share_placement_hides (
            app_id     TEXT NOT NULL REFERENCES pinned_apps(id) ON DELETE CASCADE,
            agent      TEXT NOT NULL,
            user_sub   TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (app_id, agent, user_sub)
        )
    """)

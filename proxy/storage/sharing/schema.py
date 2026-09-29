"""Schema for the sharing domain: the ``shares`` table (SHARING.md).

Called by ``storage.schema.init_schema`` after the pinned apps (an app share
references its row) and the chats; every statement is idempotent. See
``docs/architecture/DATABASE-SCHEMA.md``.
"""


def init_shares(conn) -> None:
    """Shares of apps and chats: internal grants to a platform user and
    external links."""
    # One row per share (``storage/sharing/share_store.py``). The target is
    # one of two nullable FK columns — Postgres cannot point one column at
    # two tables — and the CHECK ties the column to ``target_kind``; the
    # three delete sites (hard unpin, chat delete, user delete) cascade the
    # rows through the FKs. Internal: ``grantee_sub`` names the user,
    # ``hidden_by_grantee`` is their own "hide for me". External:
    # ``token_hash`` is sha256 of the link token, ``password_hash`` is empty
    # for a public link, ``allow_actions`` is the single Buttons switch,
    # ``actions_today``/``actions_day`` count the per-share daily cap.
    # ``state`` is ``suspended`` while the app is soft-unpinned. Chat
    # snapshots keep their directory in ``snapshot_ref``. Timestamps are ISO
    # text like ``pinned_apps``. Inline constraints only (never ``ALTER ADD
    # CONSTRAINT``).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shares (
            id                TEXT PRIMARY KEY,
            target_kind       TEXT NOT NULL CHECK (target_kind IN ('app', 'chat')),
            app_id            TEXT REFERENCES pinned_apps(id) ON DELETE CASCADE,
            chat_id           TEXT REFERENCES chats(id) ON DELETE CASCADE,
            scope             TEXT NOT NULL CHECK (scope IN ('internal', 'external')),
            grantee_sub       TEXT REFERENCES users(sub) ON DELETE CASCADE,
            hidden_by_grantee BOOLEAN NOT NULL DEFAULT FALSE,
            token_hash        TEXT UNIQUE,
            password_hash     TEXT NOT NULL DEFAULT '',
            public            BOOLEAN NOT NULL DEFAULT FALSE,
            allow_actions     BOOLEAN NOT NULL DEFAULT FALSE,
            include_tools     BOOLEAN NOT NULL DEFAULT FALSE,
            state             TEXT NOT NULL DEFAULT 'active'
                              CHECK (state IN ('active', 'suspended')),
            expires_at        TEXT,
            revoked_at        TEXT,
            created_by        TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
            created_at        TEXT NOT NULL,
            last_access_at    TEXT,
            access_count      INTEGER NOT NULL DEFAULT 0,
            actions_today     INTEGER NOT NULL DEFAULT 0,
            actions_day       TEXT NOT NULL DEFAULT '',
            snapshot_ref      TEXT NOT NULL DEFAULT '',
            CHECK ((target_kind = 'app' AND app_id IS NOT NULL AND chat_id IS NULL)
                OR (target_kind = 'chat' AND chat_id IS NOT NULL AND app_id IS NULL)),
            CHECK ((scope = 'internal' AND grantee_sub IS NOT NULL)
                OR (scope = 'external' AND grantee_sub IS NULL))
        )
    """)
    # One live internal grant per (target, user); a revoked one may be
    # re-created.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_shares_internal_grant "
        "ON shares (target_kind, COALESCE(app_id, chat_id), grantee_sub) "
        "WHERE scope = 'internal' AND revoked_at IS NULL"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_app ON shares (app_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_chat ON shares (chat_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_grantee ON shares (grantee_sub)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_created_by ON shares (created_by)")

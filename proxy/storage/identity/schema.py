"""Schema for the identity domain: users, their agent grants, passkeys, UI
preferences, credentials and the bearer allowlist.

Called by ``storage.schema.init_schema`` in foreign-key order. Every
statement is idempotent (``CREATE ... IF NOT EXISTS``); see
``docs/architecture/DATABASE-SCHEMA.md``.
"""

import logging

logger = logging.getLogger(__name__)


def init_identity(conn) -> None:
    """Users, per-agent membership / RBAC, and MCP credential stores."""
    # --- User / RBAC tables ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            sub TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'creator', 'member')),
            created_at TEXT NOT NULL,
            last_login TEXT NOT NULL,
            default_agent TEXT DEFAULT '',
            username TEXT DEFAULT '',
            display_name TEXT DEFAULT '',
            -- New users default to NOT borrowing the shared platform pool: each
            -- user connects their OWN AI engine (Claude Code / Codex) for their
            -- user-scoped chats. Admins still contribute to the pool for
            -- agent-scoped tasks. An admin can flip this per-user. (Existing
            -- installs get the same new-user default via run_migrations' ALTER.)
            allow_platform_auth BOOLEAN NOT NULL DEFAULT FALSE,
            password_hash TEXT,
            auth_provider TEXT DEFAULT 'local',
            totp_secret_enc TEXT,
            totp_enabled BOOLEAN DEFAULT FALSE,
            totp_recovery_enc TEXT,
            failed_login_attempts INTEGER DEFAULT 0,
            last_failed_login TEXT,
            locked_until TEXT,
            local_only BOOLEAN DEFAULT FALSE,
            must_change_password BOOLEAN DEFAULT FALSE,
            is_owner BOOLEAN DEFAULT FALSE,
            password_changed_at TEXT,
            default_agents_assigned BOOLEAN NOT NULL DEFAULT FALSE,
            -- The person's token epoch: every session cookie and agent
            -- session token minted before it is refused. Stamped by a
            -- password set and by an admin "sign out everywhere"; NULL =
            -- never bumped (auth/providers.py).
            token_epoch_at TEXT,
            -- The TOTP time step the account accepted last: a code is good
            -- only for a later step (auth/totp.py, RFC 6238 replay).
            totp_last_step BIGINT NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    # The sign-in ids a logout revoked (auth/session_revocation.py), kept
    # until the cookie they revoked could no longer be presented.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS revoked_session_ids (
            id TEXT PRIMARY KEY,
            user_sub TEXT NOT NULL,
            expires_at DOUBLE PRECISION NOT NULL,
            revoked_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_revoked_session_ids_expires "
        "ON revoked_session_ids(expires_at)"
    )
    # Single-owner invariant, enforced at the DB so the setup wizard can't race
    # two concurrent fresh-install POSTs into two ``is_owner`` admins. The
    # partial unique index allows at most one row with is_owner=TRUE; the second
    # concurrent INSERT fails the unique constraint → the endpoint returns 409.
    # Pre-check the row count first: init_schema runs in ONE autocommit=False
    # transaction, so letting CREATE UNIQUE INDEX fail on a pre-existing >1-owner
    # DB would abort + roll back the whole schema init (bricking boot). Skip with
    # a loud warning instead — the invariant isn't enforced on that already-
    # inconsistent DB until the duplicate owners are reconciled.
    _owner_row = conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE is_owner = TRUE"
    ).fetchone()
    _owner_count = (_owner_row["c"] if _owner_row else 0)
    if _owner_count <= 1:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_single_owner "
            "ON users(is_owner) WHERE is_owner = TRUE"
        )
    else:
        logger.warning(
            "Skipping uq_users_single_owner: %d users already have is_owner=TRUE. "
            "Reconcile duplicate owners; the single-owner constraint is NOT enforced "
            "until then.", _owner_count,
        )
    # Username slugs are minted by a SELECT-then-INSERT dedup
    # (db_users._make_username_slug), so two concurrent first-logins with the
    # same display name can both see a slug as free and mint identical
    # usernames — merging the two users' filesystem/RBAC identity. The partial
    # unique index makes the DB the arbiter (upsert_user retries the loser
    # with a fresh slug); rows still awaiting a slug keep the '' default.
    # Same don't-brick-boot guard as the owner index above: skip loudly if a
    # pre-existing DB already carries duplicates.
    _dup_row = conn.execute(
        "SELECT COUNT(*) AS c FROM ("
        "  SELECT username FROM users WHERE username <> ''"
        "  GROUP BY username HAVING COUNT(*) > 1"
        ") dups"
    ).fetchone()
    _dup_count = (_dup_row["c"] if _dup_row else 0)
    if _dup_count == 0:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username "
            "ON users(username) WHERE username <> ''"
        )
    else:
        logger.warning(
            "Skipping uq_users_username: %d username slugs are duplicated. "
            "Reconcile the duplicate usernames; slug uniqueness is NOT enforced "
            "until then.", _dup_count,
        )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_agents (
            sub TEXT NOT NULL,
            agent TEXT NOT NULL,
            assigned_at TEXT NOT NULL,
            assigned_by TEXT NOT NULL,
            agent_role TEXT DEFAULT 'viewer'
                CHECK (agent_role IN ('manager', 'editor', 'contributor', 'viewer')),
            PRIMARY KEY (sub, agent),
            FOREIGN KEY (sub) REFERENCES users(sub) ON DELETE CASCADE
        )
    """)
    # A deleted person's username never returns to the slug pool: their
    # users/<username>/ trees are archived out of the agent folders
    # (services/agents/offboarding_transfer.py) and a later person with the
    # same display name must not inherit them. Written by db_users.delete_user
    # in the deleting transaction; archived_at is stamped once no agent
    # folder holds the tree. A username here that a live users row holds
    # again means an older dump was restored: the row is dropped, not acted on.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS retired_usernames (
            username TEXT PRIMARY KEY,
            sub TEXT NOT NULL,
            retired_at TEXT NOT NULL,
            archived_at TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_retired_usernames_sub ON retired_usernames(sub)"
    )
    # WebAuthn passkeys (storage/identity/webauthn_store.py; api/auth/webauthn.py).
    # credential_id / public_key are base64url. sign_count backs the cloned-
    # authenticator check; transports (JSON list) improve later allowCredentials
    # hints. Feature-gated at the API on an https public dashboard URL.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS webauthn_credentials (
            credential_id TEXT PRIMARY KEY,
            user_sub TEXT NOT NULL,
            public_key TEXT NOT NULL,
            sign_count BIGINT NOT NULL DEFAULT 0,
            name TEXT NOT NULL DEFAULT '',
            transports TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            last_used TEXT,
            FOREIGN KEY (user_sub) REFERENCES users(sub) ON DELETE CASCADE
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_webauthn_user "
        "ON webauthn_credentials(user_sub)"
    )
    # --- Per-user roaming UI preferences (dashboard sticky prefs) ---
    # Free-form JSON bag (storage/prefs/user_ui_prefs_store.py); first tenant is
    # ``last_execution_mode`` — the per-agent interactive/headless pick.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_ui_prefs (
            user_sub TEXT PRIMARY KEY REFERENCES users(sub) ON DELETE CASCADE,
            data JSONB NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        )
    """)
    # --- Credential tables ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_credentials (
            id SERIAL PRIMARY KEY,
            user_sub TEXT NOT NULL,
            mcp_name TEXT NOT NULL,
            account_label TEXT NOT NULL DEFAULT 'default',
            credential_key TEXT NOT NULL,
            credential_value_enc TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_sub, mcp_name, account_label, credential_key),
            FOREIGN KEY (user_sub) REFERENCES users(sub) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ucred_user ON user_credentials(user_sub)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ucred_mcp ON user_credentials(mcp_name)")

    # Multi-account support — one row per labeled account a user has
    # connected for a per-user MCP. The ``is_default=TRUE`` row is the
    # catch-all account used by any agent without an explicit binding.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_credential_accounts (
            id SERIAL PRIMARY KEY,
            user_sub TEXT NOT NULL,
            mcp_name TEXT NOT NULL,
            account_label TEXT NOT NULL,
            display_email TEXT NOT NULL DEFAULT '',
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TEXT NOT NULL,
            -- What the vendor knows about the account beyond its identity
            -- (JSON; '' = nothing): a GitHub-App account keeps the person's
            -- GitHub id and the installations they can reach
            -- (storage/identity/oauth_app_kinds.py).
            meta TEXT NOT NULL DEFAULT '',
            UNIQUE(user_sub, mcp_name, account_label),
            FOREIGN KEY (user_sub) REFERENCES users(sub) ON DELETE CASCADE
        )
    """)
    # Partial unique index: at most ONE default account per (user_sub, mcp).
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_uca_default
        ON user_credential_accounts(user_sub, mcp_name)
        WHERE is_default = TRUE
    """)

    # Per agent, the set of a person's accounts the agent may use (the
    # catch-all default applies to an empty set), one row per account. The
    # key is named here and in ``migrate_identity`` alike, so a fresh table
    # and a migrated one are the same; the one-default index lives in
    # ``migrate_identity``, where ``is_default`` exists on both.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_account_bindings (
            id SERIAL PRIMARY KEY,
            user_sub TEXT NOT NULL,
            mcp_name TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            account_label TEXT NOT NULL,
            set_at TEXT NOT NULL,
            -- The person's own GitHub reach for this agent, the service
            -- binding's shape: a JSON list of owner/name ('[]' = every
            -- repository the account reaches) and whether it may only read.
            repositories TEXT NOT NULL DEFAULT '[]',
            read_only BOOLEAN NOT NULL DEFAULT FALSE,
            -- The set's account when a chat chose none: one per set.
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            CONSTRAINT agent_account_bindings_set_key
                UNIQUE (user_sub, mcp_name, agent_name, account_label),
            FOREIGN KEY (user_sub) REFERENCES users(sub) ON DELETE CASCADE
        )
    """)

    # OAuth bearer-token allowlist — controls which (provider_id, host)
    # pairs may receive a user's OAuth token via HTTP Authorization
    # injection. Hybrid model: MCP manifests declare INTENT
    # (``credentials.oauth.bearer_required`` + ``proposed_hosts``); this
    # table is the platform-controlled REALITY, with no predefined row;
    # admins can extend via /v1/admin/oauth-bearer-allowlist.
    # ``source``: ``admin`` (hand-added, or seeded by an older version),
    # else the MCP whose manifest put the row there (an older install's
    # table takes it in ``bearer_allowlist.migrate``). The ledgers beside it
    # record the hosts each MCP's provenance approved, kept while the MCP is
    # disabled, and where each MCP was installed from.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_bearer_allowlist (
            id SERIAL PRIMARY KEY,
            provider_id TEXT NOT NULL,
            host_pattern TEXT NOT NULL,
            added_by TEXT NOT NULL DEFAULT 'system',
            added_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'admin',
            CONSTRAINT oauth_bearer_allowlist_source_key UNIQUE (provider_id, host_pattern, source)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_bearer_allowlist_mcp_approved (
            mcp_name TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            host_pattern TEXT NOT NULL,
            via TEXT NOT NULL,
            approved_by TEXT NOT NULL DEFAULT '',
            approved_at TEXT NOT NULL,
            PRIMARY KEY (mcp_name, provider_id, host_pattern)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_bearer_allowlist_mcp_origin (
            mcp_name TEXT PRIMARY KEY,
            via TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )
    """)
    # The old boot seed's ledger: nothing writes or reads it, and a released
    # table is never dropped.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_bearer_allowlist_seeded (
            provider_id TEXT NOT NULL,
            host_pattern TEXT NOT NULL,
            seeded_at TEXT NOT NULL,
            PRIMARY KEY (provider_id, host_pattern)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS infra_credentials (
            id SERIAL PRIMARY KEY,
            mcp_name TEXT NOT NULL,
            credential_key TEXT NOT NULL,
            credential_value_enc TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(mcp_name, credential_key)
        )
    """)

    # Per-agent service accounts (user-owned accounts ONLY). A manager/admin
    # connects an account in their OWN user and lends it to the agent as one
    # of its own; the agent-scope (service) session reads that user's tokens
    # directly. ``account_owner_sub`` is always the owning user's sub — there
    # is no platform "service account" tier (removed before the public
    # release). The key is named here and in ``migrate_identity`` alike; the
    # one-default index lives in ``_migrate_service_sets``.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS service_agent_bindings (
            id SERIAL PRIMARY KEY,
            mcp_name TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            account_label TEXT NOT NULL,
            account_owner_sub TEXT NOT NULL DEFAULT '',
            set_by TEXT NOT NULL DEFAULT '',
            set_at TEXT NOT NULL,
            -- A GitHub App installation of the bound account as the agent's
            -- identity ('' = the account itself): the agent then gets the
            -- installation's own token, which reaches its repositories only.
            installation_id TEXT NOT NULL DEFAULT '',
            -- The agent's GitHub reach: the repositories it may reach (a JSON
            -- list of owner/name, '[]' = every one the identity reaches) and
            -- whether it may only read.
            repositories TEXT NOT NULL DEFAULT '[]',
            read_only BOOLEAN NOT NULL DEFAULT FALSE,
            -- The set's account when a chat chose none: one per set.
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            -- An agent's own accounts of a tool are a set, one account per
            -- label (the name a chat's choice and the prompt carry).
            CONSTRAINT service_agent_bindings_set_key
                UNIQUE (mcp_name, agent_name, account_label)
        )
    """)

    # The OAuth clients this install registered at vendors' authorization
    # servers (RFC 7591; storage/identity/oauth_client_registrations.py):
    # one live row per issuer and callback URL, secrets Fernet-encrypted
    # with the credential-store key, revoked rows kept because token files
    # point at them by id.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_client_registrations (
            id SERIAL PRIMARY KEY,
            issuer TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            registration_endpoint TEXT NOT NULL,
            client_id TEXT NOT NULL,
            client_secret_enc TEXT NOT NULL DEFAULT '',
            client_secret_expires_at TEXT NOT NULL DEFAULT '',
            token_endpoint_auth_method TEXT NOT NULL DEFAULT 'none',
            registration_client_uri TEXT NOT NULL DEFAULT '',
            registration_access_token_enc TEXT NOT NULL DEFAULT '',
            client_name TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL DEFAULT '',
            revoked_at TEXT NOT NULL DEFAULT '',
            revoked_reason TEXT NOT NULL DEFAULT '',
            requested_auth_method TEXT NOT NULL DEFAULT '',
            resources TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_oauth_client_registrations_live
        ON oauth_client_registrations(issuer, redirect_uri)
        WHERE revoked_at = ''
    """)
    # The outside people an agent has spoken with, one row per channel and
    # id (storage/identity/external_identity_store.py): a caller on the
    # agent's phone line today, a link visitor or a channel's member later.
    # Never a member's row: an outside person is not a platform user. The
    # row goes with the person's tree (the caller-data sweep, Forget all).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS external_identities (
            id TEXT PRIMARY KEY,
            agent TEXT NOT NULL,
            channel TEXT NOT NULL,
            external_id TEXT NOT NULL,
            slug TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            verified_at TEXT NOT NULL DEFAULT '',
            conversations INTEGER NOT NULL DEFAULT 0,
            UNIQUE (agent, channel, slug)
        )
    """)


def migrate_identity(conn) -> None:
    """The identity domain's forward migrations on an existing install
    (``run_migrations`` calls it; every step is idempotent)."""
    # 1.9.1: the bearer allowlist's rows carry their source.
    from storage.identity import bearer_allowlist
    bearer_allowlist.migrate(conn)
    # 2.0: the preferred engine of 1.9.0, read by 1.9.1 only as the seed of
    # the engine order, is gone with its column.
    conn.execute("ALTER TABLE users DROP COLUMN IF EXISTS preferred_execution_path")
    _migrate_account_sets(conn)
    _migrate_service_sets(conn)


# The four-column key of an agent's set (``init_identity`` names the same).
_SET_KEY = "agent_account_bindings_set_key"
# The three-column key of an agent's own accounts (``init_identity`` names the same).
_SERVICE_SET_KEY = "service_agent_bindings_set_key"


def _unique_keys_on(conn, table: str, columns: list[str]) -> list[str]:
    """The names of every UNIQUE constraint on ``table`` over exactly
    ``columns`` (a 1.9.1 key was never named, so it is found by them)."""
    rows = conn.execute(
        "SELECT c.conname FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE t.relname = %s AND n.nspname = current_schema() "
        "AND c.contype = 'u' AND ("
        "  SELECT array_agg(a.attname::text ORDER BY a.attname) FROM pg_attribute a "
        "  WHERE a.attrelid = t.oid AND a.attnum = ANY(c.conkey)"
        ") = %s::text[]",
        (table, sorted(columns)),
    ).fetchall()
    return [r["conname"] for r in rows]


def _one_account_keys(conn) -> list[str]:
    """The names of the 1.9.1 UNIQUE (user_sub, mcp_name, agent_name) on
    ``agent_account_bindings``."""
    return _unique_keys_on(conn, "agent_account_bindings", ["user_sub", "mcp_name", "agent_name"])


def _constraint_exists(conn, table: str, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE t.relname = %s AND n.nspname = current_schema() "
        "AND c.conname = %s",
        (table, name),
    ).fetchone() is not None


def _set_key_exists(conn) -> bool:
    return _constraint_exists(conn, "agent_account_bindings", _SET_KEY)


def _migrate_service_sets(conn) -> None:
    """An agent's own account of a tool becomes a set of accounts with one
    default, the shape of a person's set (``_migrate_account_sets``). A 1.9.1
    row becomes the default of a set of one, so every reader resolves what it
    did until a manager adds a second account. Guarded by the catalog, so a
    migrated or fresh table gets no DDL but the ``IF NOT EXISTS`` steps. A
    step that fails stops the boot: every chat's start reads the choice table
    this ends with."""
    conn.execute(
        "ALTER TABLE service_agent_bindings "
        "ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE"
    )
    old = _unique_keys_on(conn, "service_agent_bindings", ["mcp_name", "agent_name"])
    if old:
        conn.execute("UPDATE service_agent_bindings SET is_default = TRUE")
    for name in old:
        quoted = '"' + name.replace('"', '""') + '"'
        conn.execute(f"ALTER TABLE service_agent_bindings DROP CONSTRAINT IF EXISTS {quoted}")
    if not _constraint_exists(conn, "service_agent_bindings", _SERVICE_SET_KEY):
        conn.execute(
            f"ALTER TABLE service_agent_bindings ADD CONSTRAINT {_SERVICE_SET_KEY} "
            "UNIQUE (mcp_name, agent_name, account_label)"
        )
    if old:
        logger.info("service_agent_bindings: each agent's account is the default of its set")
    # A set with rows and none marked: its oldest row is the default; a set
    # with several marked keeps the oldest marked one. Every boot, before the
    # index that allows one default a set, so no data stops the boot.
    conn.execute(
        "UPDATE service_agent_bindings b SET is_default = TRUE "
        "WHERE b.id = (SELECT o.id FROM service_agent_bindings o "
        "              WHERE o.mcp_name = b.mcp_name AND o.agent_name = b.agent_name "
        "              ORDER BY o.set_at, o.id LIMIT 1) "
        "  AND NOT EXISTS (SELECT 1 FROM service_agent_bindings d "
        "                  WHERE d.mcp_name = b.mcp_name AND d.agent_name = b.agent_name "
        "                    AND d.is_default)"
    )
    conn.execute(
        "UPDATE service_agent_bindings b SET is_default = FALSE "
        "WHERE b.is_default "
        "  AND b.id <> (SELECT o.id FROM service_agent_bindings o "
        "               WHERE o.mcp_name = b.mcp_name AND o.agent_name = b.agent_name "
        "                 AND o.is_default ORDER BY o.set_at, o.id LIMIT 1)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sab_default "
        "ON service_agent_bindings(mcp_name, agent_name) WHERE is_default"
    )
    # A Shared-only chat's own choice among the agent's accounts per tool,
    # whoever made it: it goes with its set row on every delete path (a label
    # used again never revives it) and with its chat.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_service_account_choices (
            chat_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            mcp_name TEXT NOT NULL,
            account_label TEXT NOT NULL,
            set_by TEXT NOT NULL DEFAULT '',
            set_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, mcp_name),
            FOREIGN KEY (mcp_name, agent_name, account_label)
                REFERENCES service_agent_bindings(mcp_name, agent_name, account_label)
                ON DELETE CASCADE,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_csac_binding "
        "ON chat_service_account_choices(mcp_name, agent_name, account_label)"
    )


def _migrate_account_sets(conn) -> None:
    """An agent's choice of a person's account becomes a set of
    accounts with one default. A 1.9.1 row becomes the default of a set of
    one. Guarded by the catalog, so a migrated or fresh table gets no DDL
    but the ``IF NOT EXISTS`` steps. A step that fails stops the boot: every
    chat's start reads the table this ends with. The default index and the
    chat's choice come after the column and the key, which they need."""
    conn.execute(
        "ALTER TABLE agent_account_bindings "
        "ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE"
    )
    old = _one_account_keys(conn)
    if old:
        conn.execute("UPDATE agent_account_bindings SET is_default = TRUE")
    for name in old:
        quoted = '"' + name.replace('"', '""') + '"'
        conn.execute(f"ALTER TABLE agent_account_bindings DROP CONSTRAINT IF EXISTS {quoted}")
    if not _set_key_exists(conn):
        conn.execute(
            f"ALTER TABLE agent_account_bindings ADD CONSTRAINT {_SET_KEY} "
            "UNIQUE (user_sub, mcp_name, agent_name, account_label)"
        )
    if old:
        logger.info("agent_account_bindings: each agent's account is the default of its set")
    # A set with rows and none marked (a row written past the store, a
    # restore of a half-written state): its oldest row is the default, on
    # every boot, before the index that allows one default a set.
    conn.execute(
        "UPDATE agent_account_bindings b SET is_default = TRUE "
        "WHERE b.id = (SELECT o.id FROM agent_account_bindings o "
        "              WHERE o.user_sub = b.user_sub AND o.mcp_name = b.mcp_name "
        "                AND o.agent_name = b.agent_name ORDER BY o.set_at, o.id LIMIT 1) "
        "  AND NOT EXISTS (SELECT 1 FROM agent_account_bindings d "
        "                  WHERE d.user_sub = b.user_sub AND d.mcp_name = b.mcp_name "
        "                    AND d.agent_name = b.agent_name AND d.is_default)"
    )
    # A set with more than one marked (rows written past the store before the
    # index was there): the oldest marked row stays the default, so the index
    # below can always be built and no data stops the boot.
    conn.execute(
        "UPDATE agent_account_bindings b SET is_default = FALSE "
        "WHERE b.is_default "
        "  AND b.id <> (SELECT o.id FROM agent_account_bindings o "
        "               WHERE o.user_sub = b.user_sub AND o.mcp_name = b.mcp_name "
        "                 AND o.agent_name = b.agent_name AND o.is_default "
        "               ORDER BY o.set_at, o.id LIMIT 1)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_aab_default "
        "ON agent_account_bindings(user_sub, mcp_name, agent_name) WHERE is_default"
    )
    # A chat's own choice of account per tool, for the person who made it,
    # among the agent's set: it goes with its set row on every delete path
    # (a label used again never revives it) and with its chat.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_account_choices (
            chat_id TEXT NOT NULL,
            user_sub TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            mcp_name TEXT NOT NULL,
            account_label TEXT NOT NULL,
            set_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, user_sub, mcp_name),
            FOREIGN KEY (user_sub, mcp_name, agent_name, account_label)
                REFERENCES agent_account_bindings(user_sub, mcp_name, agent_name, account_label)
                ON DELETE CASCADE,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        )
    """)
    # A set row's delete finds its choices by the foreign key's columns.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cac_binding "
        "ON chat_account_choices(user_sub, mcp_name, agent_name, account_label)"
    )

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
            default_agents_assigned BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
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
                CHECK (agent_role IN ('manager', 'editor', 'viewer')),
            PRIMARY KEY (sub, agent),
            FOREIGN KEY (sub) REFERENCES users(sub) ON DELETE CASCADE
        )
    """)
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

    # Per-agent explicit override: which account to use for a specific
    # agent (catch-all default applies otherwise). UNIQUE on
    # (user, mcp, agent) so one agent binds to at most one account.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_account_bindings (
            id SERIAL PRIMARY KEY,
            user_sub TEXT NOT NULL,
            mcp_name TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            account_label TEXT NOT NULL,
            set_at TEXT NOT NULL,
            UNIQUE(user_sub, mcp_name, agent_name),
            FOREIGN KEY (user_sub) REFERENCES users(sub) ON DELETE CASCADE
        )
    """)

    # OAuth bearer-token allowlist — controls which (provider_id, host)
    # pairs may receive a user's OAuth token via HTTP Authorization
    # injection. Hybrid model: MCP manifests declare INTENT
    # (``credentials.oauth.bearer_required`` + ``proposed_hosts``); this
    # table is the platform-controlled REALITY. Seeded with known vendor
    # hosts below; admins can extend via /v1/admin/oauth-bearer-allowlist.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_bearer_allowlist (
            id SERIAL PRIMARY KEY,
            provider_id TEXT NOT NULL,
            host_pattern TEXT NOT NULL,
            added_by TEXT NOT NULL DEFAULT 'system',
            added_at TEXT NOT NULL,
            UNIQUE(provider_id, host_pattern)
        )
    """)

    # Seed vendor-official hosts so the framework works out of the box.
    # Idempotent (ON CONFLICT DO NOTHING) — keeps admin-added entries intact;
    # admins re-add any deleted defaults via the "Restore defaults" action
    # (Admin Setup → Security). DEFAULT_ALLOWLIST is the single source of truth.
    from storage.identity import bearer_allowlist
    bearer_allowlist.seed_defaults(conn)

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

    # Per-agent service binding (user-owned accounts ONLY). A manager/admin
    # connects an account in their OWN user and designates it as the agent's
    # service identity; the agent-scope (service) session reads that user's
    # tokens directly. ``account_owner_sub`` is always the owning user's sub —
    # there is no platform "service account" tier (removed for open source).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS service_agent_bindings (
            id SERIAL PRIMARY KEY,
            mcp_name TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            account_label TEXT NOT NULL,
            account_owner_sub TEXT NOT NULL DEFAULT '',
            set_by TEXT NOT NULL DEFAULT '',
            set_at TEXT NOT NULL,
            UNIQUE(mcp_name, agent_name)
        )
    """)

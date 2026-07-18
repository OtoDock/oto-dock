"""PostgreSQL schema — all table definitions, indexes, and seeds.

Called once at startup via :func:`init_schema`, which delegates to one
``init_*`` helper per domain (created in dependency order so foreign keys
resolve). ``run_migrations`` is a no-op hook today; ``init_schema`` is the
single source of truth for the schema. Every CREATE TABLE / CREATE INDEX uses
IF NOT EXISTS so re-runs are safe (the startup path is idempotent).
"""

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _index_exists(conn, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname = %s",
        (index_name,),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Per-domain table creation (called in FK order by init_schema)
# ---------------------------------------------------------------------------

def init_tasks(conn) -> None:
    """Task-run history and scheduled / dynamic task definitions."""
    # --- Task tables ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_runs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            agent TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            trigger_source TEXT,
            status TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            duration_ms INTEGER,
            prompt_preview TEXT,
            output_text TEXT,
            error_message TEXT,
            session_id TEXT,
            prompt_text TEXT,
            task_type TEXT,
            cost_usd REAL DEFAULT 0,
            chat_id TEXT,
            scope TEXT DEFAULT 'agent',
            created_by TEXT,
            execution_target TEXT NOT NULL DEFAULT 'local'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_agent ON task_runs(agent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_status ON task_runs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_task_id ON task_runs(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_started_at ON task_runs(started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_scope_created ON task_runs(scope, created_by)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS dynamic_tasks (
            id TEXT PRIMARY KEY,
            agent TEXT NOT NULL,
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            llm_mode TEXT DEFAULT 'cli',
            task_type TEXT NOT NULL,
            schedule TEXT,
            run_at TEXT,
            delay_seconds INTEGER,
            interval_seconds INTEGER,
            timeout_seconds INTEGER DEFAULT 600,
            enabled BOOLEAN DEFAULT TRUE,
            created_at TEXT NOT NULL,
            created_by TEXT,
            fired BOOLEAN DEFAULT FALSE,
            on_complete_agent TEXT,
            on_complete_prompt TEXT,
            on_complete_session_id TEXT,
            on_complete_chat_id TEXT,
            continue_session TEXT,
            use_persistent BOOLEAN DEFAULT FALSE,
            notification_mode TEXT NOT NULL DEFAULT 'manual'
                CHECK (notification_mode IN ('auto', 'manual', 'none')),
            notify_severity TEXT DEFAULT 'info',
            scope TEXT DEFAULT 'user',
            user_tz TEXT,
            community_template TEXT,
            community_template_item_slug TEXT,
            -- chat-surface delegation + continuations: run the task's turn
            -- INSIDE this existing chat instead of a fresh task-<run_id> chat.
            -- Existing DBs: one-time manual ALTER (tui_theme precedent).
            target_chat_id TEXT,
            -- recurring-continuation guardrails: hard bound on fires and/or a
            -- stop time — a chat must never wake itself forever.
            max_runs INTEGER,
            run_count INTEGER NOT NULL DEFAULT 0,
            until_at TEXT
        )
    """)
    # Template-idempotency partial indexes — one row per
    # (agent, template_item) for agent-scope; one per (agent, item, user)
    # for user-scope. Prevents duplicate seeding when the same community
    # template re-installs.
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_dyn_tasks_tpl_agent
        ON dynamic_tasks (agent, community_template_item_slug)
        WHERE community_template_item_slug IS NOT NULL AND scope = 'agent'
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_dyn_tasks_tpl_user
        ON dynamic_tasks (agent, community_template_item_slug, created_by)
        WHERE community_template_item_slug IS NOT NULL AND scope = 'user'
    """)

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
    # WebAuthn passkeys (storage/webauthn_store.py; api/auth/webauthn.py).
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
    from storage import bearer_allowlist
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

def init_chats(conn) -> None:
    """Chats, messages, media tokens, plans, and full-text search."""
    # --- Chat tables ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id TEXT PRIMARY KEY,
            user_sub TEXT NOT NULL,
            agent TEXT NOT NULL,
            title TEXT DEFAULT '',
            session_id TEXT,
            permission_mode TEXT DEFAULT 'default',
            model TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            total_cost REAL DEFAULT 0,
            context_used INTEGER DEFAULT 0,
            context_max INTEGER DEFAULT 0,
            cache_read INTEGER DEFAULT 0,
            cache_write INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            last_turn_aborted BOOLEAN DEFAULT FALSE,
            -- TRUE when the last abort closed the turn gracefully (the engine's
            -- own history kept the partial turn — Claude control_request
            -- interrupt / Codex turn/interrupt), so the next turn skips the
            -- cancelled-context injection. last_turn_aborted stays TRUE either
            -- way (scheduler/delegate user_interrupted keys on it). Existing
            -- DBs: one-time manual ALTER (test-pool deadlock — tui_theme
            -- precedent).
            last_abort_graceful BOOLEAN NOT NULL DEFAULT FALSE,
            source_type TEXT NOT NULL DEFAULT 'chat',
            codex_thread_id TEXT DEFAULT NULL,
            -- codex per-thread long-running goal, JSON {objective, token_budget,
            -- tokens_used, time_used_seconds}; NULL = no goal. Written by the
            -- pump on GOAL_UPDATE, shipped as restore.goal. Existing DBs: one-time
            -- manual ALTER (an ALTER here would deadlock the test pool's
            -- concurrent init — tui_theme precedent).
            thread_goal TEXT DEFAULT NULL,
            execution_path TEXT NOT NULL DEFAULT '',
            execution_target TEXT NOT NULL DEFAULT 'local',
            -- non-empty = the on-disk session is gone; the next turn injects a
            -- DB-history digest. Payload: 'machine_removed:<name>' | 'retention'.
            pending_history_seed TEXT NOT NULL DEFAULT '',
            -- per-chat execution-mode override: '' (resolver default → -p) |
            -- 'interactive' | '-p'; persisted so a resume re-spawns the same mode.
            execution_mode TEXT NOT NULL DEFAULT '',
            -- the interactive session's baked TUI theme ('light' | 'dark'; '' =
            -- never spawned interactive). Server-side re-warms carry no dashboard
            -- theme snapshot and re-seed from this, so a light terminal never
            -- flips dark across a respawn. Existing DBs: one-time manual ALTER
            -- (an ALTER here would deadlock the test pool's concurrent init).
            tui_theme TEXT NOT NULL DEFAULT '',
            -- TRUE once the one-time LLM chat-title upgrade is claimed (fires once).
            title_generated BOOLEAN NOT NULL DEFAULT FALSE,
            -- how the chat started: 'dashboard' | 'otodock' (CLI session on the
            -- remote machine itself) | 'delegated' (worker chat spawned by the
            -- delegate tool); a chat-list badge, does not change source_type.
            origin TEXT NOT NULL DEFAULT 'dashboard',
            -- absolute satellite-host working dir for an otodock session (outside
            -- agent_dir) so a dashboard resume re-spawns there; '' for normal chats.
            work_cwd TEXT NOT NULL DEFAULT '',
            -- when the last assistant response landed (pump end / interactive
            -- turn-complete); compared against chat_reads.last_read_at for the
            -- sidebar unread indicator. Existing DBs: one-time manual ALTER
            -- (an ALTER here would deadlock the test pool's concurrent init).
            last_response_at TEXT DEFAULT NULL,
            -- delegation (Projects): the chat that spawned this worker via
            -- delegate(surface="chat"); '' for normal chats. Existing DBs:
            -- one-time manual ALTER (tui_theme precedent).
            parent_chat_id TEXT NOT NULL DEFAULT '',
            -- opt-in project slug linking an orchestrator chat and its lanes.
            project_id TEXT NOT NULL DEFAULT '',
            -- '' | 'orchestrator' | 'worker' — stamped by the delegation spawn
            -- path; drives the chat-list linkage accents (session H).
            delegate_role TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_user ON chats(user_sub)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_agent ON chats(agent)")

    # Per-(chat, owner-identity) read markers for the sidebar unread indicator.
    # user_sub stores the chat-history OWNER identity (visibility.py's
    # chat_history_owner): the real sub for user-owned chats, the synthetic
    # "agent::<slug>" for shared-only agents — so a shared chat reads as unread
    # until ANY user opens it, then clears for everyone. Upserted on view
    # (open + focused); rows follow the chat's lifecycle via delete_chat.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_reads (
            chat_id TEXT NOT NULL,
            user_sub TEXT NOT NULL,
            last_read_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, user_sub)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id SERIAL PRIMARY KEY,
            chat_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT DEFAULT '',
            event_type TEXT DEFAULT '',
            event_data TEXT DEFAULT '',
            -- the REAL sender's user_sub (visibility-modes): for shared-only
            -- agents the chat row is owned by a synthetic agent::{slug}, so
            -- per-message attribution lives here. '' for single-owner chats.
            author_sub TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_chat ON chat_messages(chat_id)")
    # Composite (chat_id, id) backs the lazy-load scroll-back pages — the
    # `WHERE chat_id=%s AND id<%s ORDER BY id DESC LIMIT N` cursor query.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_id ON chat_messages(chat_id, id)")

    # Capability tokens for serving audio/video to <video>/<audio> elements
    # with HTTP Range support. The token in the URL IS the auth (these tags
    # can't send Authorization headers). Durable (unlike the in-memory file
    # download tokens) so chat history replays after a restart. `cache_owned`
    # marks proxy-cache copies (satellite pulls / transcode outputs) that are
    # safe to unlink on chat delete; in-place agent-tree files are not.
    # `chat_id` NULL = a workspace-minted token (no chat); reaped by TTL sweep.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS media_tokens (
            token TEXT PRIMARY KEY,
            abs_path TEXT NOT NULL,
            mime TEXT NOT NULL DEFAULT '',
            media_kind TEXT NOT NULL DEFAULT '',
            chat_id TEXT,
            session_id TEXT NOT NULL DEFAULT '',
            machine_id TEXT,
            -- satellite-host abs path (Desktop/Downloads) so a clip can be
            -- re-pulled from the laptop on replay instead of being retained.
            origin_path TEXT NOT NULL DEFAULT '',
            cache_owned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL DEFAULT '',
            -- Serve-time access stamps (cookie-gated routes): chat-bound rows
            -- derive access from the chats table; chatless rows fall back to
            -- agent-access via `agent`. '' = pre-stamp row → coarse fallback
            -- (any authenticated user). `owner_sub` is recorded where a real
            -- user sub exists (workspace mints) for the sharing-era rule.
            owner_sub TEXT NOT NULL DEFAULT '',
            agent TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_tokens_chat ON media_tokens(chat_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_plans (
            id SERIAL PRIMARY KEY,
            chat_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_plans_chat ON chat_plans(chat_id)")

    # --- FTS: chat_search with tsvector (replaces FTS5) ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_search (
            chat_id TEXT PRIMARY KEY,
            user_sub TEXT NOT NULL,
            agent TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            search_vector TSVECTOR GENERATED ALWAYS AS (
                setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(content, '')), 'B')
            ) STORED
        )
    """)
    if not _index_exists(conn, "idx_chat_search_vector"):
        conn.execute(
            "CREATE INDEX idx_chat_search_vector ON chat_search USING GIN(search_vector)"
        )

def init_usage(conn) -> None:
    """Per-call usage records and spend limits."""
    # --- Usage tracking tables ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_records (
            id SERIAL PRIMARY KEY,
            user_sub TEXT,
            agent TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'user',
            source_type TEXT NOT NULL,
            source_id TEXT,
            cost_usd REAL NOT NULL DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read INTEGER DEFAULT 0,
            cache_write INTEGER DEFAULT 0,
            message_count INTEGER DEFAULT 0,
            provider TEXT DEFAULT 'anthropic',
            source_key TEXT DEFAULT 'default',
            model TEXT DEFAULT '',
            -- billed audio duration (chat TTS/STT, transcribe-mcp); legacy rows
            -- are NULL, so aggregations must COALESCE(audio_seconds, 0).
            audio_seconds DECIMAL(10,2),
            billing_unit TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_user_date ON usage_records(user_sub, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_agent_scope_date ON usage_records(agent, scope, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_source ON usage_records(source_type, source_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_limits (
            id SERIAL PRIMARY KEY,
            limit_type TEXT NOT NULL,
            target TEXT NOT NULL,
            period TEXT NOT NULL,
            cost_limit_usd REAL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE(limit_type, target, period)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_limits_lookup ON usage_limits(limit_type, target)")

def init_mcp(conn) -> None:
    """MCP framework state, per-agent assignments, and instances."""
    # --- MCP Framework tables ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_state (
            name TEXT PRIMARY KEY,
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TEXT NOT NULL,
            -- Generic per-MCP tool filter. When the
            -- manifest declares `tool_filter.arg_name` AND admin sets a
            -- regex here, the framework appends `<arg_name> '<regex>'` to
            -- the MCP's runtime CLI args (or its Docker env, depending on
            -- the MCP's launch mechanism). Empty string = no filter (full
            -- tool surface exposed).
            tool_filter_regex TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_mcps (
            agent_name TEXT NOT NULL,
            mcp_name TEXT NOT NULL,
            PRIMARY KEY (agent_name, mcp_name)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_skills (
            agent_name TEXT NOT NULL,
            skill_id TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            exclude_from TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (agent_name, skill_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_config_values (
            mcp_name TEXT NOT NULL,
            config_key TEXT NOT NULL,
            config_value TEXT NOT NULL,
            PRIMARY KEY (mcp_name, config_key)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ssh_hosts (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            host TEXT NOT NULL,
            port INTEGER NOT NULL DEFAULT 22,
            username TEXT NOT NULL,
            key_name TEXT NOT NULL DEFAULT '',
            agents TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_instances (
            id SERIAL PRIMARY KEY,
            mcp_name TEXT NOT NULL,
            instance_name TEXT NOT NULL,
            field_values_enc TEXT NOT NULL DEFAULT '{}',
            agents TEXT NOT NULL DEFAULT '[]',
            assigned_to_all BOOLEAN NOT NULL DEFAULT FALSE,
            -- Hosted-relay state.
            -- hosted_mode: 'self_managed' (inject field_values) | 'hosted'
            --   (route through the OtoDock relay — skips field_values).
            -- managed_by:  'admin' (admin-created) | 'system' (platform
            --   auto-created by the startup pass; admin may rename/scope/
            --   delete but not flip to self_managed).
            hosted_mode TEXT NOT NULL DEFAULT 'self_managed',
            managed_by TEXT NOT NULL DEFAULT 'admin',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(mcp_name, instance_name)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mcp_instances_mcp
        ON mcp_instances(mcp_name)
    """)

def init_agents(conn) -> None:
    """Agent definitions, delegation / browser scoping, platform settings."""
    # --- Agent configuration ---
    # ``default_scope`` (memory-v3): drives the default scope for every
    # scope-aware MCP (memory, tasks, notifications, triggers, meetings) when
    # the agent talks to a user. Threaded into the agent's session env as
    # ``OTO_DEFAULT_SCOPE``; per-MCP scope args still resolve to ``user`` /
    # ``agent`` through the API role gate. Forced to ``"agent"`` at runtime
    # for sessions without a user (phone/task/trigger).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            slug TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            admin_only BOOLEAN NOT NULL DEFAULT FALSE,
            execution_path TEXT NOT NULL DEFAULT 'claude-code-cli',
            default_model TEXT NOT NULL DEFAULT '',
            default_effort TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            color TEXT DEFAULT '',
            description TEXT DEFAULT '',
            execution_paths TEXT NOT NULL DEFAULT '',
            execution_target TEXT NOT NULL DEFAULT 'local',
            community_template TEXT,
            community_template_version TEXT,
            setup_completed_at TEXT,
            default_scope TEXT NOT NULL DEFAULT 'user'
                CHECK (default_scope IN ('user', 'agent')),
            community_template_data JSONB NOT NULL DEFAULT '{}'::jsonb,
            default_for_new_users_role TEXT NOT NULL DEFAULT ''
                CHECK (default_for_new_users_role IN ('', 'viewer', 'editor', 'manager')),
            -- visibility-modes 2×2 (with default_scope): TRUE+user=Personal+shared,
            -- TRUE+agent=Shared+personal, FALSE+user=Personal-only, FALSE+agent=Shared-only.
            collaborative BOOLEAN NOT NULL DEFAULT TRUE,
            default_execution_mode TEXT NOT NULL DEFAULT ''
                CHECK (default_execution_mode IN ('', 'interactive', '-p'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_agents_community_template
        ON agents(community_template)
        WHERE community_template IS NOT NULL
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_delegation_targets (
            agent_name TEXT NOT NULL,
            target_agent TEXT NOT NULL,
            PRIMARY KEY (agent_name, target_agent)
        )
    """)

    # Per-agent allow-list of web origins the browser-control MCP may visit.
    # Empty (the default) means no allow-list — any
    # origin not caught by the manifest's blocked-origins default is reachable.
    # Injected into the browser MCP as PLAYWRIGHT_MCP_ALLOWED_ORIGINS. NOTE: this
    # is a network-request scope, NOT a hard security boundary (the dedicated
    # profile + per-machine grant are the real boundary).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_browser_origins (
            agent_name TEXT NOT NULL,
            origin TEXT NOT NULL,
            PRIMARY KEY (agent_name, origin)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS platform_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
    """)

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

def init_community_requests(conn) -> None:
    """Manager-to-admin MCP assignment-request queue."""
    # --- Community MCP assignment requests ---
    # Managers request an MCP for one of their agents; admins approve / reject.
    # Approval triggers install (if needed) + auto-enable for the requesting
    # agent. The partial unique index keeps a manager from queueing duplicate
    # open requests for the same (mcp, agent) while still allowing a re-request
    # after a prior one is rejected, cancelled, or fulfilled.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_assignment_requests (
            id SERIAL PRIMARY KEY,
            mcp_name TEXT NOT NULL,
            agent_slug TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            admin_note TEXT NOT NULL DEFAULT '',
            install_log TEXT NOT NULL DEFAULT '',
            batch_id TEXT,
            -- 'mcp' | 'skill' — which catalog/installer the request targets
            -- (mcp_name then holds a skill-package slug). Pre-existing
            -- installs get the column via run_migrations.
            kind TEXT NOT NULL DEFAULT 'mcp',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT,
            resolved_by TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_requests_open
        ON mcp_assignment_requests (mcp_name, agent_slug)
        WHERE status IN ('pending', 'approved', 'installing', 'install_failed')
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mcp_requests_status
        ON mcp_assignment_requests (status, created_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mcp_requests_agent
        ON mcp_assignment_requests (agent_slug)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mcp_requests_batch
        ON mcp_assignment_requests (batch_id)
        WHERE batch_id IS NOT NULL
    """)

def init_audio_telephony(conn) -> None:
    """Audio (STT / TTS) providers, phone servers / routes, audio prefs."""
    # --- Audio providers (STT / TTS) — shared by chat audio + telephony ---
    # Provider rows drive the admin pills and per-route / per-chat selection.
    # ``credential_key`` references ``infra_credentials.mcp_name`` (e.g.
    # ``audio-deepgram``); NULL for local / self-hosted providers. The partial
    # unique indexes enforce one default STT and one default TTS per context.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audio_providers (
            id SERIAL PRIMARY KEY,
            provider_type TEXT NOT NULL CHECK (provider_type IN ('stt', 'tts')),
            provider_name TEXT NOT NULL,
            label TEXT NOT NULL,
            credential_key TEXT,
            enabled_for_calls BOOLEAN NOT NULL DEFAULT TRUE,
            enabled_for_chat BOOLEAN NOT NULL DEFAULT TRUE,
            is_default_calls BOOLEAN NOT NULL DEFAULT FALSE,
            is_default_chat BOOLEAN NOT NULL DEFAULT FALSE,
            voices JSONB NOT NULL DEFAULT '{}',
            advanced JSONB NOT NULL DEFAULT '{}',
            last_health_check TEXT,
            last_health_status TEXT NOT NULL DEFAULT 'unknown',
            last_health_detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (provider_type, provider_name)
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_audio_default_calls "
        "ON audio_providers(provider_type) WHERE is_default_calls = TRUE"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_audio_default_chat "
        "ON audio_providers(provider_type) WHERE is_default_chat = TRUE"
    )

    # --- Phone servers (telephony adapters: Asterisk/FreePBX, Twilio, 3CX) ---
    # ``credentials`` + ``config`` are adapter-specific JSONB blobs. Bootstrap
    # tracks the one-time dialplan-snippet handshake. The control-plane adapters
    # live proxy-side in ``services/phone/phone_adapters/`` (the proxy talks to the PBX
    # directly); the phone server stays a pure media daemon. ``asterisk_manual``
    # is the no-automation reference adapter (snippet + admin-confirmed verify).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS phone_servers (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            adapter_type TEXT NOT NULL
                CHECK (adapter_type IN ('asterisk_manual', 'asterisk_freepbx', 'twilio', 'three_cx')),
            host TEXT NOT NULL DEFAULT '',
            credentials JSONB NOT NULL DEFAULT '{}',
            config JSONB NOT NULL DEFAULT '{}',
            bootstrap_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (bootstrap_status IN ('pending', 'snippet_provided', 'verified', 'failed', 'drift')),
            bootstrap_log TEXT NOT NULL DEFAULT '',
            last_health_check TEXT,
            last_health_status TEXT NOT NULL DEFAULT 'unknown',
            last_health_detail TEXT NOT NULL DEFAULT '',
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_phone_servers_default "
        "ON phone_servers(is_default) WHERE is_default = TRUE"
    )

    # --- Phone routes ---
    # ``id`` stays TEXT (UUID): it is a cross-process string key round-tripped
    # to the phone server in the warmup message (``phone_route_id``) and keyed
    # in the dashboard outbound-route dropdown — never coerce it to an int.
    # ``stt_provider_id`` / ``tts_provider_id`` NULL = use the context default;
    # ``phone_server_id`` is required (provisions the route on the adapter).
    # ``trigger_slug`` logically references ``triggers.slug`` with
    # ``scope='agent'`` and matching ``agent``. When a phone call lands on a
    # route, the warmup handler resolves the trigger row, builds a normalised
    # ``trigger_payload`` ({source: "phone", route, phone, did, body}) and
    # threads it into the agent session so manifest ``agent_context`` blocks
    # can resolve ``${trigger.*}`` tokens. NULL = no enrichment.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS phone_routes (
            id TEXT PRIMARY KEY,
            direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
            name TEXT NOT NULL DEFAULT '',
            agent TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'en',
            llm_mode TEXT NOT NULL DEFAULT 'proxy',
            phone_server_id INTEGER NOT NULL REFERENCES phone_servers(id) ON DELETE RESTRICT,
            stt_provider_id INTEGER REFERENCES audio_providers(id) ON DELETE RESTRICT,
            tts_provider_id INTEGER REFERENCES audio_providers(id) ON DELETE RESTRICT,
            greeting TEXT NOT NULL DEFAULT '',
            phone_context_override TEXT NOT NULL DEFAULT '',
            backchannel_mode TEXT NOT NULL DEFAULT 'on'
                CHECK (backchannel_mode IN ('on', 'off')),
            thinking_filler_mode TEXT NOT NULL DEFAULT 'on'
                CHECK (thinking_filler_mode IN ('on', 'off')),
            background_sound TEXT NOT NULL DEFAULT 'off',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            audiosocket_uuid TEXT UNIQUE,
            did TEXT,
            ami_caller_id TEXT NOT NULL DEFAULT '',
            ami_outbound_context TEXT NOT NULL DEFAULT '',
            dial_prefix TEXT NOT NULL DEFAULT '',
            adapter_data JSONB NOT NULL DEFAULT '{}',
            trigger_slug TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_phone_routes_direction ON phone_routes(direction)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_phone_routes_agent ON phone_routes(agent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_phone_routes_server ON phone_routes(phone_server_id)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_phone_routes_did "
        "ON phone_routes(phone_server_id, did) WHERE direction = 'inbound'"
    )
    # NOTE: ``dial_prefix`` (outbound SIP-line selection) is in the CREATE TABLE
    # above for fresh installs. Existing DBs get it via a one-time manual
    # migration — an ALTER here deadlocks the test pool's concurrent init_schema.

    # --- Per-user audio preferences (chat sound / mic icons) ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_audio_prefs (
            user_sub TEXT PRIMARY KEY REFERENCES users(sub) ON DELETE CASCADE,
            stt_mode TEXT NOT NULL DEFAULT 'auto' CHECK (stt_mode IN ('native', 'platform', 'auto')),
            tts_mode TEXT NOT NULL DEFAULT 'auto' CHECK (tts_mode IN ('native', 'platform', 'auto')),
            tts_voice_map JSONB NOT NULL DEFAULT '{}',
            stt_language TEXT,
            updated_at TEXT NOT NULL
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

def init_meetings(conn) -> None:
    """Multi-agent meetings and their turn log."""
    # --- Meetings ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meetings (
            id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            participants TEXT NOT NULL DEFAULT '[]',
            active_participants TEXT NOT NULL DEFAULT '[]',
            moderator TEXT NOT NULL,
            strategy TEXT NOT NULL DEFAULT 'round_robin',
            max_turns INTEGER NOT NULL DEFAULT 30,
            current_round INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            parent_chat_id TEXT NOT NULL,
            parent_session_id TEXT,
            parent_run_id TEXT,
            scope TEXT NOT NULL DEFAULT 'user',
            created_by TEXT,
            summary TEXT DEFAULT '',
            cost_usd REAL DEFAULT 0,
            created_at TEXT NOT NULL,
            concluded_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_meetings_status ON meetings(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_meetings_parent_chat ON meetings(parent_chat_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS meeting_turns (
            id SERIAL PRIMARY KEY,
            meeting_id TEXT NOT NULL,
            round_number INTEGER NOT NULL,
            turn_order INTEGER NOT NULL,
            agent TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'assistant',
            content TEXT DEFAULT '',
            thinking TEXT DEFAULT '',
            tool_summary TEXT DEFAULT '[]',
            session_id TEXT,
            cost_usd REAL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_meeting_turns_meeting ON meeting_turns(meeting_id)")

def init_execution_layers(conn) -> None:
    """Execution-layer subscriptions and model catalog."""
    # --- Execution layer subscriptions ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS execution_layer_subscriptions (
            id TEXT PRIMARY KEY,
            layer TEXT NOT NULL,
            provider TEXT NOT NULL,
            auth_type TEXT NOT NULL,
            owner_sub TEXT NOT NULL DEFAULT '',
            use_personal BOOLEAN NOT NULL DEFAULT TRUE,
            contribute_platform BOOLEAN NOT NULL DEFAULT FALSE,
            label TEXT NOT NULL DEFAULT '',
            is_primary BOOLEAN NOT NULL DEFAULT FALSE,
            credential_data_enc TEXT NOT NULL DEFAULT '',
            oauth_email TEXT NOT NULL DEFAULT '',
            active_sessions INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(layer, provider, auth_type, owner_sub, oauth_email)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_els_layer ON execution_layer_subscriptions(layer)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_els_contribute ON execution_layer_subscriptions(contribute_platform, owner_sub)")

    # --- Session → subscription bindings (persisted mirror of the pool's
    # in-memory map). Survives proxy restarts so usage attribution
    # (usage_records.source_key) and the scope-sticky selection keep working
    # for sessions that outlive a restart (remote satellites, re-adopted
    # chats). Written by subscription_pool.bind_session, deleted on release,
    # pruned by TTL at startup (crash leftovers). ``user_sub`` is NULL when
    # the spawn path didn't stamp the acquisition context (same fail-soft rule
    # as the in-memory ctx map); '' means agent-scope. ``scope_key`` is the
    # credential-file-sharing domain (see subscription_pool.credential_scope_key).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscription_session_bindings (
            session_id TEXT PRIMARY KEY,
            subscription_id TEXT NOT NULL,
            layer TEXT NOT NULL DEFAULT '',
            user_sub TEXT,
            scope_key TEXT NOT NULL DEFAULT '',
            bound_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ssb_scope ON subscription_session_bindings(scope_key)")

    # --- Execution layer models ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS execution_layer_models (
            id SERIAL PRIMARY KEY,
            layer TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT '',
            model_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            is_builtin BOOLEAN NOT NULL DEFAULT TRUE,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            context_window INTEGER NOT NULL DEFAULT 0,
            pricing_input REAL NOT NULL DEFAULT 0,
            pricing_output REAL NOT NULL DEFAULT 0,
            pricing_cache_write REAL NOT NULL DEFAULT 0,
            pricing_cache_read REAL NOT NULL DEFAULT 0,
            supports_reasoning BOOLEAN NOT NULL DEFAULT FALSE,
            supports_xhigh BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(layer, model_id)
        )
    """)

def init_notifications(conn) -> None:
    """Notification definitions and per-user deliveries."""
    # --- Notification tables ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            scope TEXT NOT NULL DEFAULT 'user',
            target TEXT,
            source TEXT NOT NULL,
            source_id TEXT,
            notification_type TEXT NOT NULL,
            schedule TEXT,
            run_at TEXT,
            interval_seconds INTEGER,
            created_by TEXT,
            created_at TEXT NOT NULL,
            enabled BOOLEAN DEFAULT TRUE,
            fired_count INTEGER DEFAULT 0,
            last_fired_at TEXT,
            agent_slug TEXT,
            chat_id TEXT,
            user_tz TEXT,
            community_template TEXT,
            community_template_item_slug TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_notifs_tpl_agent
        ON notifications (agent_slug, community_template_item_slug)
        WHERE community_template_item_slug IS NOT NULL AND scope = 'agent'
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_notifs_tpl_user
        ON notifications (agent_slug, community_template_item_slug, target)
        WHERE community_template_item_slug IS NOT NULL AND scope = 'user'
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS notification_deliveries (
            id TEXT PRIMARY KEY,
            notification_id TEXT,
            user_sub TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            severity TEXT NOT NULL,
            scope TEXT NOT NULL,
            source TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            read BOOLEAN DEFAULT FALSE,
            dismissed BOOLEAN DEFAULT FALSE,
            read_at TEXT,
            dismissed_at TEXT,
            agent_slug TEXT,
            chat_id TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_user ON notification_deliveries(user_sub)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_unread ON notification_deliveries(user_sub, read, dismissed)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_delivered ON notification_deliveries(delivered_at)")

def init_mcp_autoupdate(conn) -> None:
    """Weekly MCP auto-update run log."""
    # --- Automatic MCP updates run log (services/mcp/mcp_autoupdate.py) ---
    # One row per MCP touched in a weekly run. `status`:
    #   updated | no_change | skipped_in_use | failed.
    # `run_id` groups a single run; `trigger` is 'auto' (weekly) — reserved for
    # future manual full-runs. Pruned by the daily retention sweep.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_auto_update_log (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            mcp_name TEXT NOT NULL,
            runtime TEXT NOT NULL DEFAULT '',
            old_version TEXT NOT NULL DEFAULT '',
            new_version TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            trigger TEXT NOT NULL DEFAULT 'auto',
            ts TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mcp_autoupdate_ts ON mcp_auto_update_log(ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mcp_autoupdate_run ON mcp_auto_update_log(run_id)")

def init_push(conn) -> None:
    """Web-push subscriptions for notification delivery."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id TEXT PRIMARY KEY,
            user_sub TEXT NOT NULL,
            platform TEXT NOT NULL,
            subscription_data TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions(user_sub)")
    if not _index_exists(conn, "idx_push_unique"):
        conn.execute(
            "CREATE UNIQUE INDEX idx_push_unique ON push_subscriptions(user_sub, subscription_data)"
        )

def init_webhooks(conn) -> None:
    """Vendor webhook-subscription state."""
    # --- Webhook subscription table ---
    # Vendor-side subscription state. One row per (provider, account, vendor_target)
    # that we've registered with a vendor's webhook API. Triggers reference these
    # via subscription_id to fan out on incoming events.
    #
    # Subscriptions are scope-aware: scope='user' rows belong to a user (owner =
    # user_sub) and fire user-scope triggers; scope='service' rows are agent-owned
    # (owner = '', agent set) and fire agent-scope triggers on that agent.
    # Cross-scope firing is not supported — the binding feature on
    # service_agent_bindings affects only credential RESOLUTION at session time,
    # not webhook fan-out.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS webhook_subscriptions (
            id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            owner TEXT NOT NULL,
            agent TEXT,
            mcp_name TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            account_label TEXT NOT NULL,
            vendor_target TEXT NOT NULL,
            vendor_subscription_id TEXT,
            selected_events JSONB NOT NULL DEFAULT '[]',
            selected_subevents JSONB NOT NULL DEFAULT '{}',
            signing_secret_enc TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'creating',
            last_error TEXT,
            last_event_at TEXT,
            event_count INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            -- event-relay delivery: 'vendor' = the vendor calls this install's
            -- webhook URL directly; 'relay' = events arrive via the OtoDock
            -- relay's forwarded ingest (/v1/webhooks/relay/{provider}).
            delivery_mode TEXT NOT NULL DEFAULT 'vendor'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_owner ON webhook_subscriptions(scope, owner)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_agent ON webhook_subscriptions(agent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_mcp ON webhook_subscriptions(mcp_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_provider ON webhook_subscriptions(provider_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_status ON webhook_subscriptions(status)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_subs_expires "
        "ON webhook_subscriptions(expires_at) WHERE expires_at IS NOT NULL"
    )
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_subs_unique_target
        ON webhook_subscriptions(scope, owner, agent, provider_id, account_label, vendor_target)
        WHERE status IN ('active', 'creating', 'renew_failed')
    """)

def init_triggers(conn) -> None:
    """Event triggers plus the agent / user API keys that fire them."""
    # --- Trigger tables ---
    # Webhook triggers (mirror tasks/notifications scope model). Two scopes:
    # 'user' (per-user automations) and 'agent' (manager-managed business
    # events). The slug uniqueness is partial — see indexes below.
    #
    # Vendor-subscribed triggers carry subscription_id (FK to
    # webhook_subscriptions) and event_filter (equality dict). Generic
    # webhook-URL triggers leave both NULL/'{}'.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS triggers (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            scope TEXT NOT NULL,
            agent TEXT NOT NULL,
            created_by TEXT NOT NULL,
            task_id TEXT,
            notify_enabled BOOLEAN DEFAULT FALSE,
            notify_severity TEXT DEFAULT 'info',
            notify_title TEXT,
            notify_body TEXT,
            notify_target_scope TEXT,
            notify_target TEXT,
            debounce_seconds INTEGER DEFAULT 0,
            enabled BOOLEAN DEFAULT TRUE,
            fired_count INTEGER DEFAULT 0,
            last_fired_at TEXT,
            last_error TEXT,
            subscription_id TEXT REFERENCES webhook_subscriptions(id) ON DELETE SET NULL,
            event_filter JSONB NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            community_template TEXT,
            community_template_item_slug TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_triggers_agent ON triggers(agent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_triggers_created_by ON triggers(created_by)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_triggers_subscription "
        "ON triggers(subscription_id) WHERE subscription_id IS NOT NULL"
    )
    # Partial unique indexes: same slug allowed across scopes / different owners.
    if not _index_exists(conn, "idx_triggers_agent_slug"):
        conn.execute(
            "CREATE UNIQUE INDEX idx_triggers_agent_slug "
            "ON triggers (agent, slug) WHERE scope = 'agent'"
        )
    if not _index_exists(conn, "idx_triggers_user_slug"):
        conn.execute(
            "CREATE UNIQUE INDEX idx_triggers_user_slug "
            "ON triggers (created_by, slug) WHERE scope = 'user'"
        )
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_triggers_tpl_agent
        ON triggers (agent, community_template_item_slug)
        WHERE community_template_item_slug IS NOT NULL AND scope = 'agent'
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_triggers_tpl_user
        ON triggers (agent, community_template_item_slug, created_by)
        WHERE community_template_item_slug IS NOT NULL AND scope = 'user'
    """)

    # Per-agent API keys (manager-managed). Authenticate webhook fires for
    # agent-scoped triggers. Master PROXY_API_KEY is rejected on the webhook
    # endpoint — only these scoped keys (or user keys) work.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_api_keys (
            id TEXT PRIMARY KEY,
            agent TEXT NOT NULL,
            name TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            prefix TEXT NOT NULL,
            permissions JSONB NOT NULL DEFAULT '["triggers"]',
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            revoked_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_api_keys_agent ON agent_api_keys(agent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_api_keys_prefix ON agent_api_keys(prefix)")

    # Per-user API keys (per-user). Permission scopes (JSONB list) restrict
    # what the key can do. v1 supports 'triggers'; future: chat / tasks /
    # notifications (alternative input layers like WhatsApp/Telegram/Slack).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_api_keys (
            id TEXT PRIMARY KEY,
            user_sub TEXT NOT NULL,
            name TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            prefix TEXT NOT NULL,
            permissions JSONB NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            revoked_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_api_keys_user ON user_api_keys(user_sub)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_api_keys_prefix ON user_api_keys(prefix)")

def init_memory(conn) -> None:
    """File-based memory toggles (platform + per-agent overrides)."""
    # --- Memory tables (file-based Memory) ---
    # Memory content lives in markdown topic files + a server-generated
    # ``MEMORY.md`` index per scope (``knowledge/memory/`` shared,
    # ``users/{u}/context/memory/`` per-user — see ``services/memory_file``).
    # DB only tracks platform toggles + per-agent overrides + the
    # prompt-injection inline budget + the turn-counter nudge knob.

    # Platform-wide memory feature toggles. Singleton row (id=1). Admin
    # master switches gate the entire feature; per-agent rows in
    # ``agent_memory_settings`` override the two toggles.
    #
    # ``inline_budget_bytes`` — per scope: topic files inject in FULL into
    # the system prompt while their total stays under this; past it the
    # prompt carries only the generated index (+ the `memory` tool's view).
    # ``nudge_turns`` — assistant turns without a memory-tool call before a
    # one-line capture reminder rides the next user message (0 = off).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_settings (
            id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            user_memory_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            agent_memory_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            inline_budget_bytes INTEGER NOT NULL DEFAULT 8192,
            nudge_turns INTEGER NOT NULL DEFAULT 10
        )
    """)
    conn.execute(
        "INSERT INTO memory_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING"
    )

    # Per-agent toggle overrides. Cascade-deleted with agents.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_memory_settings (
            agent TEXT PRIMARY KEY REFERENCES agents(slug) ON DELETE CASCADE,
            user_memory_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            agent_memory_enabled BOOLEAN NOT NULL DEFAULT TRUE
        )
    """)

def init_recover_bin(conn) -> None:
    """Workspace recover-bin (soft-deleted / conflict captures)."""
    # Workspace Recover Bin (``storage/recover_bin_store.py``) — the one passive
    # trash-can. One row per ``deleted`` file (dashboard / tombstone / live delete)
    # or ``conflict`` (the losing side of a genuine cross-user concurrent edit on a
    # shared file). Captured bytes live on disk under
    # ``RECOVER_BIN_DIR/<agent_slug>/<entry_id>``; this row holds metadata + the
    # per-user/shared recovery scope so a restore is authorization-gated. Reaped
    # after ``expires_at`` (7 days) by ``app.py``. Inline CHECK only (NEVER
    # ``ALTER ADD CONSTRAINT`` — conftest pool deadlock; see role-refactor note).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recover_bin (
            entry_id       TEXT PRIMARY KEY,
            agent_slug     TEXT NOT NULL,
            rel_path       TEXT NOT NULL,
            original_name  TEXT NOT NULL,
            reason         TEXT NOT NULL
                CHECK (reason IN ('deleted', 'conflict')),
            scope          TEXT NOT NULL CHECK (scope IN ('user', 'shared')),
            owner_sub      TEXT NOT NULL DEFAULT '',
            binned_at      TEXT NOT NULL,
            file_hash      TEXT NOT NULL,
            size           BIGINT NOT NULL,
            expires_at     TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recover_bin_agent_expires "
        "ON recover_bin (agent_slug, expires_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recover_bin_owner "
        "ON recover_bin (owner_sub)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recover_bin_expires "
        "ON recover_bin (expires_at)"
    )

def init_pinned_apps(conn) -> None:
    """Pinned mini-apps registry (standing agent-authored dashboards)."""
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
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            UNIQUE (agent, username, slug),
            CHECK (scope_chat_id IS NULL OR scope_project_id IS NULL)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pinned_apps_agent ON pinned_apps (agent)"
    )


def init_pinned_files(conn) -> None:
    """Dock file pins (read-only file rows on the chat/project Dock)."""
    # One row per pinned FILE (``storage/db_file_pins.py``): the Dock renders
    # the workspace file at ``rel_path`` (agent-dir-relative, served through
    # the files API — path policy + viewer role enforced there, never here)
    # as a collapsed row → expand → markdown. Unlike pinned_apps there is no
    # standing scope: EXACTLY one of ``scope_chat_id``/``scope_project_id``
    # is set (inline CHECK — new table, so it applies everywhere). Multiple
    # pins per scope, unique per (scope, rel_path) via partial indexes (safe
    # in init: table + columns are created together). Content lives on disk;
    # deleting a pin never touches the file.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pinned_files (
            id               TEXT PRIMARY KEY,
            agent            TEXT NOT NULL,
            rel_path         TEXT NOT NULL,
            title            TEXT NOT NULL DEFAULT '',
            scope_chat_id    TEXT,
            scope_project_id TEXT,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            CHECK ((scope_chat_id IS NULL) <> (scope_project_id IS NULL))
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pinned_files_chat "
        "ON pinned_files (scope_chat_id, rel_path) "
        "WHERE scope_chat_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pinned_files_project "
        "ON pinned_files (scope_project_id, rel_path) "
        "WHERE scope_project_id IS NOT NULL"
    )


def init_file_sync(conn) -> None:
    """Versioned file-sync state, tombstones, and authorship."""
    # --- Versioned file sync (last-write-wins 3-way merge) ---------------
    #
    # Three tables back ``core/remote/file_sync.py::diff_manifests``. Per file the
    # proxy resolves platform {hash,mtime} vs satellite {hash,mtime} vs a
    # remembered ``base`` — the hash last CONVERGED with that machine — so it
    # knows WHO changed since the last sync (newest-version-wins, not
    # proxy-always-wins).

    # sync_state (storage/sync_state_store.py): the per-(machine,agent,file)
    # merge base. A CACHE + change-attribution hint, never a delete-authority —
    # reconciled from every manifest, so a stale/missing row degrades to "first
    # sync", never to data loss. ``base_mtime`` is the PLATFORM file's mtime
    # (platform clock) — a re-hash-cache hint, NOT a merge input. No TTL: a row
    # lives while the file stays converged; cleared when deleted on both sides.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            machine_id   TEXT NOT NULL,
            agent_slug   TEXT NOT NULL,
            rel_path     TEXT NOT NULL,
            base_hash    TEXT NOT NULL,
            base_mtime   DOUBLE PRECISION NOT NULL DEFAULT 0,
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (machine_id, agent_slug, rel_path)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_state_machine_agent "
        "ON sync_state (machine_id, agent_slug)"
    )

    # file_tombstones (storage/file_tombstones_store.py): an EXPLICIT, timestamped
    # record that the platform deleted a file. Deletes are NEVER inferred from
    # absence — a platform-absent / satellite-present file is PULLED (healed)
    # unless a tombstone authorizes deleting the satellite's copy. This is what
    # stops a wiped/divergent satellite from mass-deleting platform data, and what
    # lets an offline satellite apply a missed delete on reconnect.
    # ``deleted_at_mtime`` (epoch seconds) orders a tombstone against a satellite
    # re-create. Reaped after ``FILE_TOMBSTONE_TTL_DAYS`` (30d) by ``app.py``.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_tombstones (
            agent_slug       TEXT NOT NULL,
            rel_path         TEXT NOT NULL,
            deleted_at_mtime DOUBLE PRECISION NOT NULL,
            deleted_at       TEXT NOT NULL,
            origin           TEXT NOT NULL DEFAULT '',
            expires_at       TEXT NOT NULL,
            PRIMARY KEY (agent_slug, rel_path)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_tombstones_expires "
        "ON file_tombstones (expires_at)"
    )

    # file_author (storage/file_author_store.py): the persisted platform
    # last-writer per file — the durable form of the in-memory ``_last_writer``.
    # Read at merge time to tell a CROSS-USER concurrent divergence (capture the
    # loser + notify) from a same-user one (newest-wins, no capture). Stores the
    # username SLUG (what both the live path and the merge hold natively — a sub
    # is resolved only when actually notifying). Best-effort: an unknown author
    # degrades to a silent safety capture, never data loss. Updated on every
    # platform-side write; cleared on delete.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_author (
            agent_slug   TEXT NOT NULL,
            rel_path     TEXT NOT NULL,
            last_writer  TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (agent_slug, rel_path)
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
    init_storage_quotas(conn)
    init_community_requests(conn)
    init_audio_telephony(conn)
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
    init_pinned_files(conn)
    init_file_sync(conn)
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
    # 2026-07-10: dashboard soft-unpin for pinned mini-apps (ADD COLUMN
    # IF NOT EXISTS is idempotent — no information_schema probe needed).
    conn.execute(
        "ALTER TABLE pinned_apps "
        "ADD COLUMN IF NOT EXISTS hidden BOOLEAN NOT NULL DEFAULT FALSE"
    )
    # 2026-07-17: skills adoption — approval requests carry which catalog
    # they target ('mcp' | 'skill'); every pre-existing row is an MCP request.
    conn.execute(
        "ALTER TABLE mcp_assignment_requests "
        "ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'mcp'"
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
    except Exception as e:  # the guard must never take the boot down with it
        logger.debug("schema drift probe skipped: %s", e)
    finally:
        try:
            conn.rollback()  # discard the probe schema + reset search_path
        except Exception:
            pass
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
    return drift

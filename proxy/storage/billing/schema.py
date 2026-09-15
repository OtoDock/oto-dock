"""Schema for the billing domain: usage records and limits, execution-layer
subscriptions, session bindings and models.

Called by ``storage.schema.init_schema`` in foreign-key order. Every
statement is idempotent (``CREATE ... IF NOT EXISTS``); see
``docs/architecture/DATABASE-SCHEMA.md``.
"""


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
    # Per-subscription spend over a rolling window: the pool's headroom
    # routing and the pool caps sum by source_key.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_source_key_date ON usage_records(source_key, created_at)")

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
    # ``is_primary`` is retired (it was only a stable-sort tie-break behind the
    # consumption routing and read as a "primary provider" it never was). The
    # column stays for released installs; rows are zeroed so an older client
    # bundle that still renders the badge shows nothing.
    conn.execute("UPDATE execution_layer_subscriptions SET is_primary = FALSE WHERE is_primary")

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

    # --- Provider windows: the vendor's own 5-hour / weekly window state per
    # OAuth subscription, one append-only row per observation (a poll or a
    # CLI stream event; services/engines/subscription_windows.py). The pool
    # reads the latest sample per account to route new work, the caps read
    # the 24-hour deltas, the alerts read the weekly windows. ``data`` holds
    # the normalized extras only (scoped per-model windows, the reached flag,
    # the plan name). Pruned past 8 days by the poll tick.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscription_window_samples (
            id SERIAL PRIMARY KEY,
            subscription_id TEXT NOT NULL
                REFERENCES execution_layer_subscriptions(id) ON DELETE CASCADE,
            observed_at TEXT NOT NULL,
            source TEXT NOT NULL,
            five_hour_pct REAL,
            five_hour_resets_at TEXT,
            seven_day_pct REAL,
            seven_day_resets_at TEXT,
            data JSONB NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sws_sub_time ON subscription_window_samples(subscription_id, observed_at DESC)")
    # One row per (account, weekly window, threshold) — the dedup for the
    # owner's 90 % / 100 % notifications. ``resets_at`` names the window
    # instance the alert was for (minute precision); a new reset instant
    # re-arms the threshold.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscription_window_alerts (
            subscription_id TEXT NOT NULL
                REFERENCES execution_layer_subscriptions(id) ON DELETE CASCADE,
            window_key TEXT NOT NULL,
            threshold INTEGER NOT NULL,
            resets_at TEXT NOT NULL,
            fired_at TEXT NOT NULL,
            PRIMARY KEY (subscription_id, window_key, threshold)
        )
    """)

    # --- Pool caps: one row per pool, never per account. ``scope`` is
    # 'platform' (``target`` '') or 'user' (``target`` the owner's sub); the
    # percentages are of the accounts' weekly windows, the dollars are the
    # attributed spend on those accounts; NULL = not set. ``on_reached`` is
    # 'stop' or 'continue' (on API keys). Evaluated per engine by
    # services/billing/pool_caps.py.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscription_pool_caps (
            scope TEXT NOT NULL,
            target TEXT NOT NULL,
            week_pct REAL,
            day_pct REAL,
            week_usd REAL,
            day_usd REAL,
            on_reached TEXT NOT NULL DEFAULT 'stop',
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (scope, target)
        )
    """)

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

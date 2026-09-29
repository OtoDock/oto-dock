"""Schema for the automation domain: tasks and runs, notifications and
deliveries, push subscriptions, webhook subscriptions, triggers and the
API keys that fire them.

Called by ``storage.schema.init_schema`` in foreign-key order. Every
statement is idempotent (``CREATE ... IF NOT EXISTS``); see
``docs/architecture/DATABASE-SCHEMA.md``.
"""

from storage.schema_base import _drop_invalid_indexes, _index_exists


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
            execution_target TEXT NOT NULL DEFAULT 'local',
            background_pending INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_status ON task_runs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_task_id ON task_runs(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_started_at ON task_runs(started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_scope_created ON task_runs(scope, created_by)")
    # A chat's latest run (the task-history list, the sidebar's run lookup,
    # the task-chat pump), a session's runs (the task-pump poll) and one
    # agent's run history newest first (the Tasks page poll) each read an
    # index: a sequential scan there holds the event loop. The COALESCE expression
    # must stay textually identical to the chat queries' ORDER BY, and the
    # agent index must say NULLS LAST like the run listing's ORDER BY (a
    # DESC key is NULLS FIRST otherwise, and every run of the agent gets
    # sorted); test_schema_indexes.py holds both. The agent index covers a
    # single-column agent lookup as its prefix, so idx_runs_agent is dropped.
    _drop_invalid_indexes(conn, "idx_runs_chat_started", "idx_runs_session",
                          "idx_runs_agent_started")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_chat_started "
        "ON task_runs (chat_id, (COALESCE(started_at, '')) DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_session ON task_runs (session_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_agent_started "
        "ON task_runs (agent, started_at DESC NULLS LAST)"
    )
    conn.execute("DROP INDEX IF EXISTS idx_runs_agent")

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
            until_at TEXT,
            -- Per-task execution overrides. Empty = inherit the agent's
            -- default at fire time; the names match the TaskDefinition fields
            -- the delegate path already carried in memory, and
            -- core/config/task_config_builder.py consumes both. Validated
            -- against the agent's envelope on WRITE
            -- (spawn_authz.validate_spawn_overrides), never at fire time.
            override_model TEXT DEFAULT '',
            override_execution_path TEXT DEFAULT '',
            -- An app handler on a schedule (task_type='app', APPS.md
            -- "Handlers"): the app row and the handler name; the prompt
            -- stays empty and the runner never opens a session for it.
            -- Existing DBs: run_migrations (with the partial unique index).
            app_id TEXT,
            app_handler TEXT,
            -- The offboarding transfer (services/agents/offboarding_transfer.py):
            -- an agent-scope row whose creator lost the editor tier moves to
            -- the admin who made the change. transferred_from keeps the FIRST
            -- creator across hops (the prompt's author, whose provenance the
            -- knowledge-write grant follows), transferred_at the last hop.
            -- Empty on a row that never moved. Existing DBs: run_migrations.
            transferred_from TEXT NOT NULL DEFAULT '',
            transferred_at TEXT NOT NULL DEFAULT ''
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
            community_template_item_slug TEXT,
            -- The offboarding transfer record, as on dynamic_tasks.
            transferred_from TEXT NOT NULL DEFAULT '',
            transferred_at TEXT NOT NULL DEFAULT ''
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
            chat_id TEXT,
            -- A dashboard path the row opens when set (a shared app, a
            -- shared chat); empty = the agent/chat deep link as before.
            href TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_user ON notification_deliveries(user_sub)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_unread ON notification_deliveries(user_sub, read, dismissed)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_delivered ON notification_deliveries(delivered_at)")


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
            delivery_mode TEXT NOT NULL DEFAULT 'vendor',
            -- the manifest's vendor_target_spec.target_kinds key the row
            -- registered as; '' = the default kind (a repository, for GitHub).
            target_kind TEXT NOT NULL DEFAULT ''
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
            community_template_item_slug TEXT,
            -- The app action (APPS.md "Handlers"): a fire wakes this app's
            -- handler instead of a task; never set together with task_id
            -- (service-layer rule, no CHECK). Existing DBs: run_migrations.
            app_id TEXT,
            handler TEXT,
            -- The offboarding transfer record, as on dynamic_tasks.
            transferred_from TEXT NOT NULL DEFAULT '',
            transferred_at TEXT NOT NULL DEFAULT ''
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

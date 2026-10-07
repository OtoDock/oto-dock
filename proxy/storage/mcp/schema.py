"""Schema for the MCP domain: MCP state and config, agent MCPs and skills,
ssh hosts, instances, assignment requests and the auto-update log.

Called by ``storage.schema.init_schema`` in foreign-key order. Every
statement is idempotent (``CREATE ... IF NOT EXISTS``); see
``docs/architecture/DATABASE-SCHEMA.md``.
"""


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


def init_mcp_autoupdate(conn) -> None:
    """Weekly MCP auto-update run log."""
    # --- Automatic MCP updates run log (services/mcp/mcp_autoupdate.py) ---
    # One row per MCP touched in a weekly run. `status`:
    #   updated | no_change | skipped_in_use | failed | held | needs_approval
    # (the last two: a `.hold` marker, a catalog source change left to the
    # admin). `run_id` groups a single run; `trigger` is 'auto' (weekly) or
    # 'manual'. Pruned by the daily retention sweep.
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

    # --- The last update check and the pending source changes ---
    # (services/mcp/mcp_updater.py, services/community/mcp_source_swap.py;
    # storage/mcp/mcp_update_state_store.py). One row per MCP the last check
    # found an update for (`info` is the detection entry as JSON), replaced
    # whole by every check; the check time is the platform setting
    # `mcp_update_last_checked_at`.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_update_checks (
            mcp_name TEXT PRIMARY KEY,
            info TEXT NOT NULL,
            checked_at TEXT NOT NULL
        )
    """)
    # One row per MCP whose catalog source differs from the installed one:
    # `status` pending (shown, awaiting the admin's Switch) | switching (the
    # accept route holds the install lock) | switched (the result shown until
    # dismissed). `from_*` is the installed identity, `to_*` the catalog's at
    # check time, `to_manifest_hash` the catalog manifest the card described,
    # `plan` the credential carry/rename/reconnect lists (JSON), `result` what
    # a switch did or why it failed (JSON). `notified_at` marks the weekly
    # job's one notification per pair.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_source_changes (
            mcp_name TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            from_kind TEXT NOT NULL,
            from_identity TEXT NOT NULL,
            from_url TEXT NOT NULL,
            from_runtime TEXT NOT NULL DEFAULT '',
            to_kind TEXT NOT NULL,
            to_identity TEXT NOT NULL,
            to_url TEXT NOT NULL,
            to_runtime TEXT NOT NULL DEFAULT '',
            to_version TEXT NOT NULL DEFAULT '',
            to_manifest_hash TEXT NOT NULL DEFAULT '',
            declared BOOLEAN NOT NULL DEFAULT FALSE,
            plan TEXT NOT NULL DEFAULT '{}',
            detected_at TEXT NOT NULL,
            notified_at TEXT,
            accepted_at TEXT,
            accepted_by TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        )
    """)

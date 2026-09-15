"""Schema for the agents domain: agents, delegation edges, browser origins,
platform settings, departments and the memory settings.

Called by ``storage.schema.init_schema`` in foreign-key order. Every
statement is idempotent (``CREATE ... IF NOT EXISTS``); see
``docs/architecture/DATABASE-SCHEMA.md``.
"""


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
                CHECK (default_execution_mode IN ('', 'interactive', '-p')),
            -- Departments (agents-map): ONE department per agent; '' =
            -- unassigned. Both set together or both '' — enforced at the API
            -- (PATCH /v1/agents) and self-healed by the edge compiler (a
            -- dangling id simply compiles to no edges). No FK by design,
            -- matching agent_delegation_targets.
            department_id TEXT NOT NULL DEFAULT '',
            department_level_id TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_agents_community_template
        ON agents(community_template)
        WHERE community_template IS NOT NULL
    """)

    # Delegation edges are DIRECTIONAL (A→B and B→A are two rows) and carry a
    # ``source``: 'manual' rows are owned by the agent-config checkbox UI /
    # PUT delegation-targets; 'department' rows are owned exclusively by the
    # department edge compiler (services/departments/edge_compiler.py) and are
    # retracted/recreated wholesale on every recompile. One row per edge — a
    # manual row wins a compiled INSERT (ON CONFLICT DO NOTHING), so an edge a
    # user explicitly checked survives leaving a department.
    # ``observe`` is RESERVED-UNUSED (2026-08-15 merge — same precedent as
    # ``seen_at``): the edge ITSELF now grants agent_name's NO-USER sessions
    # read visibility into target_agent's agent-scope activity, so the
    # separate per-edge flag was retired without schema churn. Nothing reads
    # or writes the column; INSERTs leave it at its DEFAULT.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_delegation_targets (
            agent_name TEXT NOT NULL,
            target_agent TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual'
                CHECK (source IN ('manual', 'department')),
            observe BOOLEAN NOT NULL DEFAULT FALSE,
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


def init_departments(conn) -> None:
    """Company departments for the agents map (installation-wide, shared).

    Departments are METADATA that compiles down to agent_delegation_targets
    rows tagged source='department' — the map is a view, not new distributed
    state. ``created_by_sub`` stores the creating user's ``acting_sub`` and is
    the FIRST created_by-ownership gate on a platform-level object: creators
    may edit/delete only departments they created; admins may edit/delete all
    (proxy/api/departments/). Assignment lives ON the agent row
    (agents.department_id / department_level_id), one department per agent.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS departments (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_by_sub TEXT NOT NULL DEFAULT '',
            auto_delegation BOOLEAN NOT NULL DEFAULT TRUE,
            reach TEXT NOT NULL DEFAULT 'adjacent'
                CHECK (reach IN ('adjacent', 'subtree')),
            -- Free-form map placement hint (JSON), owned by the map UI.
            position_hint TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # Levels are admin-named tiers (rank 0 = top, e.g. Head/Senior/Junior).
    # Cap of 8 and rank uniqueness are enforced in db_departments (the only
    # writer replaces a department's levels transactionally), not by
    # constraint — a UNIQUE(department_id, rank) would make in-place
    # reorders conflict mid-statement.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS department_levels (
            id TEXT PRIMARY KEY,
            department_id TEXT NOT NULL REFERENCES departments(id)
                ON DELETE CASCADE,
            rank INTEGER NOT NULL,
            name TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_department_levels_dept
        ON department_levels(department_id)
    """)


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

"""Schema for the agents domain: agents, delegation edges, browser origins,
platform settings, departments and the memory settings.

Called by ``storage.schema.init_schema`` in foreign-key order. Every
statement is idempotent (``CREATE ... IF NOT EXISTS``); see
``docs/architecture/DATABASE-SCHEMA.md``.
"""


#: The engine an agent runs on when nothing says otherwise — a missing
#: ``agents.execution_path``, a chat row from before the column existed, a
#: caller with no opinion. ONE constant: it used to be typed inline at a dozen
#: sites, so "the platform's default engine" could be neither read nor changed
#: in one place. Lives beside the ``agents`` column whose DDL default spells
#: the same id (a test pins the two equal), in a module with no imports, so
#: storage and every config builder above it read it without an import cycle.
DEFAULT_EXECUTION_PATH = "claude-code-cli"


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
                CHECK (default_for_new_users_role IN ('', 'viewer', 'contributor', 'editor', 'manager')),
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
            department_level_id TEXT NOT NULL DEFAULT '',
            -- The engines of ``execution_paths`` on which this agent runs
            -- local models only (a JSON list of engine ids; '' = none).
            -- ``execution_path`` and ``execution_paths`` together are the
            -- engines the agent MAY run on: each person's sessions take
            -- their own engine among them (services/engines/engine_resolution.py).
            local_only_engines TEXT NOT NULL DEFAULT '',
            -- The team account (services/mcp/account_context.py): on a
            -- collaborative agent, every session with a person behind it
            -- runs the tools on the agent's own accounts, from the floor
            -- role up; below it a person gets no account for those tools.
            team_account BOOLEAN NOT NULL DEFAULT FALSE,
            team_account_floor TEXT NOT NULL DEFAULT 'editor'
                CHECK (team_account_floor IN ('viewer', 'contributor', 'editor', 'manager'))
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


def init_outside(conn) -> None:
    """What an agent's managers picked for outside people (a caller on the
    agent's phone line, a visitor who is not a member): the tools, each Read
    only, Read and write or a custom list of function names; the person's
    own files, the web, the knowledge folders and the apps. Nothing is
    picked by default. ``services/mcp/outside_picks.py`` reads the tables. After a
    conversation: whether the agent writes down what it learned
    (``wrapup_enabled``, on), whether that keeps a note for the team
    (``shared_notes``, off) and the model it runs on (``wrapup_model``, ''
    = the agent's own)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_outside (
            agent_name TEXT PRIMARY KEY,
            files_access TEXT NOT NULL DEFAULT 'read_only'
                CHECK (files_access IN ('read_only', 'read_write')),
            web BOOLEAN NOT NULL DEFAULT FALSE,
            knowledge TEXT NOT NULL DEFAULT '[]',
            shared_notes BOOLEAN NOT NULL DEFAULT FALSE,
            wrapup_model TEXT NOT NULL DEFAULT '',
            wrapup_enabled BOOLEAN NOT NULL DEFAULT TRUE
        )
    """)
    # ``granted`` keeps the read-only functions the labels gave when a Read
    # only pick was made; a later listing that changes the set stamps
    # ``changed_at`` and tells the managers (services/mcp/outside_picks.py).
    # ``governs`` says which sessions a row is for: outside people (a pick,
    # which opens a tool to them), the agent's own runs or people's chats (a
    # limit, which narrows a tool the agent has: services/mcp/tool_limits.py).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_outside_tools (
            agent_name TEXT NOT NULL,
            mcp_name TEXT NOT NULL,
            access TEXT NOT NULL
                CHECK (access IN ('read_only', 'read_write', 'custom')),
            functions TEXT NOT NULL DEFAULT '[]',
            granted TEXT NOT NULL DEFAULT '[]',
            changed_at TEXT NOT NULL DEFAULT '',
            governs TEXT NOT NULL DEFAULT 'outside'
                CHECK (governs IN ('outside', 'runs', 'chats')),
            PRIMARY KEY (agent_name, mcp_name, governs)
        )
    """)
    # The apps a manager opened to outside people, by the app row's id (a
    # purged slug never moves a pick to a new app). ``methods`` keeps the
    # methods the app opened to outside people when it was picked; a later
    # release that changes them stamps ``changed_at`` and tells the
    # managers (services/mcp/outside_picks.py). 2.0's own table.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_outside_apps (
            agent_name TEXT NOT NULL,
            app_id TEXT NOT NULL,
            methods TEXT NOT NULL DEFAULT '[]',
            changed_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (agent_name, app_id)
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
            reach TEXT NOT NULL DEFAULT 'adjacent'
                CHECK (reach IN ('adjacent', 'subtree')),
            -- What the compiled edges wire (db_departments.MODE_WIRING):
            -- 'off' nothing; 'down' a strict hierarchy (below only);
            -- 'down_across' the hierarchy plus the same level mutual; 'both'
            -- every wired pair both ways. The column default serves the
            -- forward migration only (a department from before the column
            -- keeps the symmetric wiring it had); every INSERT writes the
            -- value explicitly and a NEW department defaults to 'down'.
            mode TEXT NOT NULL DEFAULT 'both'
                CHECK (mode IN ('off', 'down', 'down_across', 'both')),
            -- Free-form map placement hint (JSON), owned by the map UI.
            position_hint TEXT NOT NULL DEFAULT '',
            -- The catalog set the department was installed from and its
            -- version (department_set_installer); '' = made by hand.
            community_set TEXT NOT NULL DEFAULT '',
            community_set_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # The record of a set's install: one row per job, the members' states
    # and the lines as JSON (db_set_installs). Whole-table CREATE: a new
    # table converges existing installs at startup.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS set_installs (
            id TEXT PRIMARY KEY,
            set_slug TEXT NOT NULL,
            set_version TEXT NOT NULL DEFAULT '',
            department_name TEXT NOT NULL,
            department_id TEXT NOT NULL DEFAULT '',
            started_by TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('running', 'done', 'failed', 'interrupted')),
            members JSONB NOT NULL DEFAULT '[]'::jsonb,
            lines JSONB NOT NULL DEFAULT '[]'::jsonb,
            started_at TEXT NOT NULL,
            ended_at TEXT
        )
    """)
    # The places in ``lines`` of the lines that say something was not done
    # (the dialog never folds them). An install that made the table before
    # gains it here, every earlier job with none.
    conn.execute("ALTER TABLE set_installs ADD COLUMN IF NOT EXISTS "
                 "not_done JSONB NOT NULL DEFAULT '[]'::jsonb")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_set_installs_set ON set_installs (set_slug, status)")
    # One running job per set: the insert is the claim, so two starts
    # close together never both run (the second answers 409).
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_set_installs_one_running "
                 "ON set_installs (set_slug) WHERE status = 'running'")
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
    # The agent a set install placed for each member template of its set
    # (``agent`` '' when it placed none): what a binding that names that
    # template reaches from inside the department (services/apps/
    # binding_target.py). Written by a set install's placement
    # (db_departments.place_members) and by a hand placement into an empty
    # place (db_departments.fill_place); an agent's delete empties its rows.
    # Whole-table CREATE: a new table converges existing installs at startup,
    # and a department made before it records nothing.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS department_set_members (
            department_id TEXT NOT NULL REFERENCES departments(id)
                ON DELETE CASCADE,
            template TEXT NOT NULL,
            agent TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (department_id, template)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_department_set_members_agent
        ON department_set_members(agent)
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
    # newest topics that fit load whole and the rest are listed with their
    # index entries (+ the `memory` tool's view).
    # ``nudge_turns`` — user turns without a memory write that landed before
    # a one-line capture reminder rides the next user message (0 = off).
    # ``topic_grow_cap_bytes`` — a topic over this size no longer grows (a
    # write that shrinks it always lands); ``entry_cap_bytes`` — the most
    # one edit may add to a topic.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_settings (
            id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            user_memory_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            agent_memory_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            inline_budget_bytes INTEGER NOT NULL DEFAULT 8192,
            nudge_turns INTEGER NOT NULL DEFAULT 10,
            topic_grow_cap_bytes INTEGER NOT NULL DEFAULT 32768,
            entry_cap_bytes INTEGER NOT NULL DEFAULT 2048
        )
    """)
    # A database that made the table before the two caps existed (1.9.1):
    # the defaults apply to its row.
    conn.execute(
        "ALTER TABLE memory_settings ADD COLUMN IF NOT EXISTS "
        "topic_grow_cap_bytes INTEGER NOT NULL DEFAULT 32768"
    )
    conn.execute(
        "ALTER TABLE memory_settings ADD COLUMN IF NOT EXISTS "
        "entry_cap_bytes INTEGER NOT NULL DEFAULT 2048"
    )
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


#: The one-shot flag of the allowlist widening (``platform_settings``).
ENGINE_ALLOWLIST_WIDENED_FLAG = "engine_allowlist_widened"


def _primary_key(conn, table: str) -> tuple[str, list[str]] | None:
    """``(name, columns in key order)`` of ``table``'s primary key, None
    when it has none."""
    row = conn.execute(
        "SELECT c.conname AS name, ("
        "  SELECT array_agg(a.attname::text ORDER BY k.ord) FROM unnest(c.conkey) WITH ORDINALITY k(attnum, ord) "
        "  JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum"
        ") AS cols FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE t.relname = %s AND n.nspname = current_schema() AND c.contype = 'p'",
        (table,),
    ).fetchone()
    if row is None:
        return None
    return row["name"], list(row["cols"] or [])


def _widen_outside_tools_key(conn) -> None:
    """The primary key of ``agent_outside_tools`` takes ``governs``: the
    two-column key of an install that made the table before the column is
    dropped by its name and the three-column one added. Idempotent."""
    key = _primary_key(conn, "agent_outside_tools")
    if key is None or key[1] == ["agent_name", "mcp_name", "governs"]:
        return
    name = '"' + key[0].replace('"', '""') + '"'
    conn.execute(f"ALTER TABLE agent_outside_tools DROP CONSTRAINT {name}")
    conn.execute("ALTER TABLE agent_outside_tools ADD PRIMARY KEY (agent_name, mcp_name, governs)")
    import logging
    logging.getLogger(__name__).info("agent_outside_tools: the key now names which sessions a row governs")


def migrate_agents(conn, *, engines=()) -> None:
    """The agents domain's forward migrations on an existing install
    (``run_migrations`` calls it once; every step is idempotent; the next
    migration of the domain goes here). The set a department came from
    (1.9.1): empty on every pre-existing row.

    1.9.1: the engine switches of an agent became an allowlist ("this agent
    may run on this engine"; each person's sessions take their own engine
    among them) where they used to be a selection of what the manager held.
    So an upgraded install loses no engine anyone used and gains the ones it
    did not list: every agent row's ``execution_paths`` keeps its stored
    extras first and appends the registered engines it lacks, in
    ``engines``' order (the registry's), the primary excluded. One shot (a
    ``platform_settings`` flag), in a SAVEPOINT so a surprise row never
    aborts the boot (it retries next start). A boot that passes no engines
    (the test fixture, the standalone scheduler) widens nothing and leaves
    the flag unset, so the next proxy boot widens.
    """
    # 2.0: after a conversation, the note for the team, the wrap-up's model
    # and whether it runs at all (the table is 2.0's own; an install that
    # made it before these columns gains them here, every agent's wrap-up
    # on as it was).
    conn.execute("ALTER TABLE agent_outside ADD COLUMN IF NOT EXISTS "
                 "shared_notes BOOLEAN NOT NULL DEFAULT FALSE")
    conn.execute("ALTER TABLE agent_outside ADD COLUMN IF NOT EXISTS "
                 "wrapup_model TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE agent_outside ADD COLUMN IF NOT EXISTS "
                 "wrapup_enabled BOOLEAN NOT NULL DEFAULT TRUE")
    for column in ("community_set", "community_set_version"):
        conn.execute(f"ALTER TABLE departments ADD COLUMN IF NOT EXISTS {column} TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "ALTER TABLE agents "
        "ADD COLUMN IF NOT EXISTS local_only_engines TEXT NOT NULL DEFAULT ''"
    )
    # Days an agent keeps its history (services/infra/history_retention.py), 0 is off.
    conn.execute(
        "ALTER TABLE agents "
        "ADD COLUMN IF NOT EXISTS history_retention_days INTEGER NOT NULL DEFAULT 0"
    )
    # 2.0: the team account and its role floor (services/mcp/account_context.py).
    conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS team_account BOOLEAN NOT NULL DEFAULT FALSE")
    conn.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS team_account_floor TEXT NOT NULL DEFAULT 'editor' "
        "CHECK (team_account_floor IN ('viewer', 'contributor', 'editor', 'manager'))"
    )
    # 2.0: a tool pick row says which sessions it governs. An install that
    # made the table before the column keeps every outside pick exactly as
    # it was, under 'outside', and its key widens to the three columns. No
    # savepoint: a half-widened key would refuse every later write.
    conn.execute(
        "ALTER TABLE agent_outside_tools ADD COLUMN IF NOT EXISTS governs TEXT NOT NULL DEFAULT 'outside' "
        "CHECK (governs IN ('outside', 'runs', 'chats'))"
    )
    _widen_outside_tools_key(conn)
    engines = [e for e in engines if e]
    if not engines:
        return
    if conn.execute("SELECT 1 FROM platform_settings WHERE key = %s",
                    (ENGINE_ALLOWLIST_WIDENED_FLAG,)).fetchone():
        return
    import json
    import logging
    conn.execute("SAVEPOINT engine_allowlist")
    try:
        rows = conn.execute(
            "SELECT slug, execution_path, execution_paths FROM agents"
        ).fetchall()
        widened = 0
        for r in rows:
            primary = r["execution_path"] or ""
            try:
                extras = json.loads(r["execution_paths"] or "[]")
            except (json.JSONDecodeError, TypeError):
                extras = []
            if not isinstance(extras, list):
                extras = []
            kept = [e for e in extras if isinstance(e, str) and e and e != primary]
            wanted = kept + [e for e in engines if e != primary and e not in kept]
            if wanted == kept:
                continue
            conn.execute(
                "UPDATE agents SET execution_paths = %s WHERE slug = %s",
                (json.dumps(wanted), r["slug"]),
            )
            widened += 1
        conn.execute("INSERT INTO platform_settings (key, value) VALUES (%s, 'done') "
                     "ON CONFLICT (key) DO NOTHING", (ENGINE_ALLOWLIST_WIDENED_FLAG,))
        conn.execute("RELEASE SAVEPOINT engine_allowlist")
        if widened:
            logging.getLogger(__name__).info(
                "engine allowlist widened on %d agent(s): every registered engine is allowed",
                widened)
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT engine_allowlist")
        logging.getLogger(__name__).exception(
            "engine allowlist widening failed: will retry next startup")

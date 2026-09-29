"""Schema for the chat domain: chats, messages, media tokens, plans, search
and meetings.

Called by ``storage.schema.init_schema`` in foreign-key order. Every
statement is idempotent (``CREATE ... IF NOT EXISTS``); see
``docs/architecture/DATABASE-SCHEMA.md``.
"""

from storage.schema_base import _drop_invalid_indexes, _index_exists


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
            -- non-empty = delegate-result wakes that could not be delivered
            -- (every ladder rung failed — e.g. dead interactive parent whose
            -- resume also failed). JSON array of rendered wake prompts,
            -- claimed atomically at the chat's next warmup/turn and injected
            -- so the orchestrator continues.
            pending_delegate_wake TEXT NOT NULL DEFAULT '',
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
            delegate_role TEXT NOT NULL DEFAULT '',
            -- the checks attached to this chat (CHECKS.md): a JSON list of
            -- refs ('agent:<name>' | 'user:<name>'), '' = none. Attached by
            -- the checks tool for a chat, copied from the task for a run.
            -- Existing DBs: run_migrations adds it.
            checks TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_user ON chats(user_sub)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_agent ON chats(agent)")
    # The newest chats of an agent (the chat and task lists stop at their
    # limit instead of sorting every chat), a session's chat (every hook with
    # chat side effects), the project and worker lookups polled every 10 s,
    # and the finished-unread backfill (partial: the query repeats the
    # predicate, so generic plans use it too).
    _drop_invalid_indexes(conn, "idx_chats_agent_updated", "idx_chats_session",
                          "idx_chats_project", "idx_chats_parent", "idx_chats_last_response")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_agent_updated ON chats (agent, updated_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_session ON chats (session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_project ON chats (project_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_parent ON chats (parent_chat_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chats_last_response ON chats (last_response_at) "
        "WHERE last_response_at IS NOT NULL"
    )

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
            -- user sub exists (workspace mints) and is not read by the rule.
            -- Share routes never read this table: a chat share serves copies
            -- from its snapshot (SHARING.md).
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

"""Schema for the chat domain: chats, messages, media tokens, plans, search
and meetings.

Called by ``storage.schema.init_schema`` in foreign-key order. Every
statement is idempotent (``CREATE ... IF NOT EXISTS``); see
``docs/architecture/DATABASE-SCHEMA.md``.
"""

import json
import logging

from core.permission_modes import ASK_MODE, AUTO_REVIEW_MODE
from storage.schema_base import _drop_invalid_indexes, _index_exists

logger = logging.getLogger("otodock")

#: The mode words a release before 1.9 stored in ``chats.permission_mode``,
#: and the word each one is now. Only the migration below reads them.
_MODES_BEFORE_1_9 = {"default": ASK_MODE, "acceptEdits": AUTO_REVIEW_MODE}


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
            permission_mode TEXT DEFAULT 'ask',
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
            -- resume also failed). JSON array of {"prompt", "person", "role",
            -- "by"} (the person and role the delivery ran as, "" for no
            -- person; "by" the creator whose role a no-person wake runs at),
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
            checks TEXT NOT NULL DEFAULT '',
            -- the outside person this conversation is with (an
            -- external_identities id), '' for every other chat
            external_identity_id TEXT NOT NULL DEFAULT '',
            -- the compute machine on another host the chat's engine session
            -- last started on, '' for every other chat
            compute_machine TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_user ON chats(user_sub)")
    # No single-column agent index: idx_chats_agent_updated below serves the
    # agent lookups by its prefix, and one index fewer keeps a chats UPDATE
    # cheaper (run_migrations drops the old one on an existing database).
    # The newest chats of an agent (the chat and task lists stop at their
    # limit instead of sorting every chat), a session's chat (every hook with
    # chat side effects), the project and worker lookups polled every 10 s,
    # and the finished-unread backfill (partial: the query repeats the
    # predicate, so generic plans use it too), and a person's own chats per
    # engine, which order the engines they see (partial: storage/chat/engine_use).
    _drop_invalid_indexes(conn, "idx_chats_agent_updated", "idx_chats_session",
                          "idx_chats_project", "idx_chats_parent", "idx_chats_last_response",
                          "idx_chats_own_engine_use")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_agent_updated ON chats (agent, updated_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_session ON chats (session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_project ON chats (project_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_parent ON chats (parent_chat_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chats_last_response ON chats (last_response_at) "
        "WHERE last_response_at IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chats_own_engine_use ON chats (user_sub, created_at) "
        "WHERE source_type = 'chat' AND origin = 'dashboard' AND execution_path <> ''"
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

    # A person's pinned chats (``storage/chat/chat_pins.py``): one row per
    # person and chat, the real person's sub even on a Shared-only agent's
    # pool chat. Both keys cascade, so a chat's delete and a person's delete
    # take their pins. Whole-table CREATE (the new-table pattern: an existing
    # install converges at its first boot).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pinned_chats (
            user_sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
            chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            pinned_at TEXT NOT NULL,
            PRIMARY KEY (user_sub, chat_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pinned_chats_chat ON pinned_chats (chat_id)")

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

    # A message typed while the chat's turn runs, waiting for the next turn:
    # one row per message while it waits, deleted when the turn accepts it
    # (its chat_messages row then carries the queue_id). Keyed by the chat
    # and the client-minted queue_id so a re-send after a reconnect is acked
    # once. The chat's drop cascades. Created at every boot (IF NOT EXISTS),
    # nothing to migrate: an older proxy never reads it.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_input_queue (
            id SERIAL PRIMARY KEY,
            chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            queue_id TEXT NOT NULL,
            author_sub TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            cli_text TEXT NOT NULL DEFAULT '',
            event_data TEXT NOT NULL DEFAULT '',
            images TEXT NOT NULL DEFAULT '',
            origin_conn TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE (chat_id, queue_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_input_queue_chat ON chat_input_queue (chat_id, id)"
    )

    # The turn a chat has in flight, written by its pump at the turn's start
    # and deleted at its end (``storage/chat/open_turns.py``): a row the boot
    # finds is a turn the last process never finished. ``engine_open`` says
    # an engine turn runs (its result has not arrived); ``early_ranges`` (a
    # JSON list of ``[after, upto]``) names the rows that engine turn saved
    # before its end, committed with those rows (a replay deletes them).
    # The facts a continue of the turn needs sit on the row from its start,
    # since a killed process writes nothing: who the session runs as
    # (``person``, a sub, '' for the agent itself), the kind of session, its
    # mode, the machine it runs on, the task run that drives it, the plan it
    # implements, and ``card``, the permission card that waits on a person
    # (JSON, '' when none). ``kind`` is '' only on a row an older release left.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_open_turns (
            chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
            turn_token TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            engine_open BOOLEAN NOT NULL DEFAULT TRUE,
            early_ranges TEXT NOT NULL DEFAULT '[]',
            person TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT '',
            machine_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            plan TEXT NOT NULL DEFAULT '',
            card TEXT NOT NULL DEFAULT ''
        )
    """)

    # The turns the platform itself cut (``storage/chat/cut_records.py``): a
    # stop, a crash, a machine that lost the session. The boot turns every
    # open-turn row it finds into one, and the continue pass
    # (``services/scheduler/continue_pass.py``) works from them. ``state``:
    # ``pending`` (the pass takes it), ``waiting`` (its machine has not said
    # whether it still holds the session), ``running`` (the pass holds it),
    # then ``continued``, ``gave_way``, ``dropped`` or ``left``. No role is
    # stored: the pass reads the person's standing when it starts a session.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_cut_turns (
            id BIGSERIAL PRIMARY KEY,
            chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            turn_token TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            person TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT '',
            machine_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            plan TEXT NOT NULL DEFAULT '',
            card TEXT NOT NULL DEFAULT '',
            engine_open BOOLEAN NOT NULL DEFAULT TRUE,
            started_at TEXT NOT NULL DEFAULT '',
            cause TEXT NOT NULL,
            cut_at TEXT NOT NULL,
            state TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            settled_at TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_cut_turns_state ON chat_cut_turns (state)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_cut_turns_chat ON chat_cut_turns (chat_id, id)")

    # A reload or a move the agent asked for that waits for its turn's end
    # (``storage/chat/pending_rebuilds.py``): the waiter is a task in memory,
    # the row is what a restart finds. One per chat, gone when the waiter ends.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_pending_rebuilds (
            chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
            what TEXT NOT NULL,
            person TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            target TEXT NOT NULL DEFAULT '',
            choices TEXT NOT NULL DEFAULT '{}',
            deadline TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

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
    # The record of each conversation in an agent's Conversations tab
    # (storage/chat/conversation_store.py): the line it came on, its
    # direction, the outside person it was with, and what the wrap-up after
    # it recorded. One row per phone-owned conversation; it goes with its chat.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_records (
            chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
            agent TEXT NOT NULL,
            line TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'inbound',
            identity_id TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL DEFAULT '',
            follow_ups TEXT NOT NULL DEFAULT '[]',
            wrapup TEXT NOT NULL DEFAULT '',
            wrapup_run_id TEXT NOT NULL DEFAULT '',
            wrapup_attempts INTEGER NOT NULL DEFAULT 0,
            wrote TEXT NOT NULL DEFAULT '[]',
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversation_records_identity "
                 "ON conversation_records (agent, identity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversation_records_wrapup "
                 "ON conversation_records (wrapup) WHERE wrapup IN ('pending', 'running')")
    # A wrap-up's one-line note for the team, when the agent's switch is on:
    # rendered into members' and the agent's own prompts, never a file, so no
    # model tool writes it and forgetting a person removes their lines.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS outside_notes (
            id SERIAL PRIMARY KEY,
            agent TEXT NOT NULL,
            identity_id TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            day TEXT NOT NULL,
            text TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outside_notes_agent ON outside_notes (agent, id)")


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


def init_reply_reports(conn) -> None:
    """A person's report of an agent's reply, for the install's admins
    (the ``report`` notification source): who, when, the chat and its
    reply's first row, the agent, the reason, the note, the reply's text as
    the server stored it, the chat's title when it was reported, and when
    the admins were told (``notified_at``: the notice gap runs from a
    person's last notified report). One per person and reply. A report
    outlives its chat (``chat_id`` set NULL, the text and the title kept)
    and goes a year after it was filed (``db_reply_reports.KEEP_DAYS``, the
    retention sweep); a message row can go alone (a turn replayed after a
    restart writes its rows again), so the message id carries no FK. An
    admin marks a report handled (``handled_at``, ``handled_by``) or
    dismisses it (``dismissed_at``, ``dismissed_by``), at most one of the
    two, restores it, or deletes it. Whole-table CREATE in init (new-table
    pattern), with the later columns added after for a database that made
    the table before them; the foreign key of a table made with ON DELETE
    CASCADE is rewritten once by ``migrate_chat``."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reply_reports (
            id            SERIAL PRIMARY KEY,
            reporter_sub  TEXT NOT NULL,
            chat_id       TEXT REFERENCES chats(id) ON DELETE SET NULL,
            message_id    INTEGER NOT NULL,
            agent         TEXT NOT NULL DEFAULT '',
            reason        TEXT NOT NULL,
            note          TEXT NOT NULL DEFAULT '',
            reply_text    TEXT NOT NULL DEFAULT '',
            chat_title    TEXT NOT NULL DEFAULT '',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            notified_at   TIMESTAMPTZ,
            dismissed_at  TIMESTAMPTZ,
            dismissed_by  TEXT NOT NULL DEFAULT '',
            handled_at    TIMESTAMPTZ,
            handled_by    TEXT NOT NULL DEFAULT '',
            UNIQUE (reporter_sub, message_id)
        )
    """)
    # A database that made the table before an admin could dismiss, mark
    # handled or outlive a chat: every existing report stays open, its
    # title filled in by migrate_chat.
    conn.execute("ALTER TABLE reply_reports ADD COLUMN IF NOT EXISTS dismissed_at TIMESTAMPTZ")
    conn.execute(
        "ALTER TABLE reply_reports ADD COLUMN IF NOT EXISTS dismissed_by TEXT NOT NULL DEFAULT ''"
    )
    conn.execute("ALTER TABLE reply_reports ADD COLUMN IF NOT EXISTS handled_at TIMESTAMPTZ")
    conn.execute(
        "ALTER TABLE reply_reports ADD COLUMN IF NOT EXISTS handled_by TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        "ALTER TABLE reply_reports ADD COLUMN IF NOT EXISTS chat_title TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reply_reports_reporter "
        "ON reply_reports (reporter_sub, created_at)"
    )
    # A chat's deletion sets its reports' chat_id: without it every delete
    # scans the table.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reply_reports_chat ON reply_reports (chat_id)"
    )


def _current_mode_words(conn) -> None:
    """2.0: a chat that stored a mode word from before 1.9 holds the word it
    reads as, and the column's default is Ask. A boot after the first finds
    no such row, and the default is written only while the catalog names
    another; in the boot's transaction, so a failure stops the boot."""
    for old, new in _MODES_BEFORE_1_9.items():
        conn.execute("UPDATE chats SET permission_mode = %s WHERE permission_mode = %s", (new, old))
    row = conn.execute(
        "SELECT column_default FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'chats' AND column_name = 'permission_mode'").fetchone()
    if row and not (row["column_default"] or "").startswith(f"'{ASK_MODE}'"):
        conn.execute(f"ALTER TABLE chats ALTER COLUMN permission_mode SET DEFAULT '{ASK_MODE}'")


def migrate_chat(conn) -> None:
    """The chat domain's forward migrations on an existing install, at
    startup before the pool serves a request (``run_migrations`` calls it): the
    columns on ``chats``, the stored wakes' shape, the index it no longer needs,
    the waiting messages of deleted people, the mode words from before 1.9
    (all before the reports' step, which returns early once it is done).
    Reports outlive their chat (1.9.1): a
    ``reply_reports`` made with ``chat_id NOT NULL ... ON DELETE CASCADE`` gets ``ON DELETE SET NULL``
    and a nullable column, and every report's ``chat_title`` from its chat
    (all of them still have one). Read from the catalog, so a boot after the
    first does nothing, and it never runs on a table the CREATE above made:
    not on a fresh install, the boot's drift probe nor a test database, so
    the constraint DDL meets no pool (the reason the other constraints live
    in ``run_migrations``). Inside a SAVEPOINT: a failure leaves the reports
    going with their chat and the next boot tries again."""
    # 2026-09-17: checks (CHECKS.md). The checks attached to a chat (a JSON
    # list of refs, '' = none) and to a task; empty on every pre-existing
    # row. The three checks tables are new tables (init_checks).
    conn.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS checks TEXT NOT NULL DEFAULT ''")
    # 2.0: the outside person a phone conversation is with.
    conn.execute(
        "ALTER TABLE chats ADD COLUMN IF NOT EXISTS external_identity_id TEXT NOT NULL DEFAULT ''"
    )
    # 2.0: the compute machine on another host the chat's engine session last
    # started on (its own copy holds the engine's record): where the chat
    # stays. Only such a machine's id is written; '' everywhere else.
    conn.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS compute_machine TEXT NOT NULL DEFAULT ''")
    # What a continue of a cut turn needs, on the turn's own row.
    for column in ("person", "kind", "mode", "machine_id", "run_id", "plan", "card"):
        conn.execute(f"ALTER TABLE chat_open_turns ADD COLUMN IF NOT EXISTS {column} "
                     "TEXT NOT NULL DEFAULT ''")
    # 2026-07-18: durable delegate-wake replay — undeliverable delegate
    # results are stored on the parent chat and injected at its next
    # warmup/turn.
    conn.execute(
        "ALTER TABLE chats "
        "ADD COLUMN IF NOT EXISTS pending_delegate_wake TEXT NOT NULL DEFAULT ''"
    )
    # The chats with a parked wake (startup and every satellite reconnect);
    # here, not in init_chats: the column itself arrives by this migration.
    _drop_invalid_indexes(conn, "idx_chats_pending_wake")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chats_pending_wake ON chats (id) "
        "WHERE pending_delegate_wake <> ''"
    )
    # A stored wake names the person it runs as since 1.7.1 (a JSON array of
    # {"prompt", "person", "role", "by"}); 1.7.0 stored plain strings. Each
    # such value is rewritten with no person; a converted value starts with
    # '[{', so the pass is idempotent and needs no flag. A value that is not
    # JSON is cleared (the reader would drop it anyway).
    conn.execute("SAVEPOINT pending_wake_records")
    try:
        from storage.chat.db_chats import wake_records
        rows = conn.execute(
            "SELECT id, pending_delegate_wake FROM chats "
            "WHERE pending_delegate_wake <> '' AND pending_delegate_wake NOT LIKE '[{%%'"
        ).fetchall()
        for r in rows:
            wakes = wake_records(r["pending_delegate_wake"])
            if not wakes and r["pending_delegate_wake"].strip() != "[]":
                logger.warning("pending wake migration: chat %s held no readable wake; cleared",
                               r["id"])
            conn.execute("UPDATE chats SET pending_delegate_wake = %s WHERE id = %s",
                         (json.dumps(wakes) if wakes else "", r["id"]))
        conn.execute("RELEASE SAVEPOINT pending_wake_records")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT pending_wake_records")
        logger.exception("pending wake migration failed; it retries at the next startup")
    # The engine-reported running cost total at the last recorded turn and
    # the engine session it belongs to. Claude Code ≥ 2.1.277 resumes
    # a session from its saved total, and the pump's per-turn delta needs the
    # baseline across a proxy restart: the in-memory map made durable, keyed
    # by session id exactly like it (a fresh session on the chat starts at 0).
    # 0 / '' on every pre-existing row.
    conn.execute(
        "ALTER TABLE chats "
        "ADD COLUMN IF NOT EXISTS engine_cost_total DOUBLE PRECISION NOT NULL DEFAULT 0"
    )
    conn.execute(
        "ALTER TABLE chats "
        "ADD COLUMN IF NOT EXISTS engine_cost_session_id TEXT NOT NULL DEFAULT ''"
    )

    # The single-column agent index on chats (1.7.1): idx_chats_agent_updated
    # covers it by its prefix, and every chats UPDATE rewrote it for nothing.
    # Guarded by the catalog so a database that never had it, or already
    # lost it, does nothing here.
    if _index_exists(conn, "idx_chats_agent"):
        conn.execute("SAVEPOINT drop_idx_chats_agent")
        try:
            conn.execute("DROP INDEX IF EXISTS idx_chats_agent")
            conn.execute("RELEASE SAVEPOINT drop_idx_chats_agent")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT drop_idx_chats_agent")
            logger.exception("dropping idx_chats_agent failed; the next boot retries")
    # 1.8.0: a deleted person's waiting messages go with them; rows a 1.7.x
    # deletion left behind could never be sent. One-shot.
    _QUEUE_FLAG = "chat_input_queue_orphans_swept"
    if not conn.execute("SELECT 1 FROM platform_settings WHERE key = %s",
                        (_QUEUE_FLAG,)).fetchone():
        gone = conn.execute(
            "DELETE FROM chat_input_queue q WHERE q.author_sub <> '' AND NOT EXISTS "
            "(SELECT 1 FROM users u WHERE u.sub = q.author_sub)").rowcount
        if gone:
            logger.info("chat_input_queue: %d waiting message(s) of deleted people removed", gone)
        conn.execute("INSERT INTO platform_settings (key, value) VALUES (%s, 'done') "
                     "ON CONFLICT (key) DO NOTHING", (_QUEUE_FLAG,))
    _current_mode_words(conn)
    fk = conn.execute(
        "SELECT c.conname FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY (c.conkey) "
        "WHERE t.relname = 'reply_reports' AND n.nspname = current_schema() "
        "AND c.contype = 'f' AND c.confdeltype = 'c' AND a.attname = 'chat_id'"
    ).fetchone()
    col = conn.execute(
        "SELECT a.attnotnull FROM pg_attribute a "
        "JOIN pg_class t ON t.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE t.relname = 'reply_reports' AND n.nspname = current_schema() "
        "AND a.attname = 'chat_id' AND NOT a.attisdropped"
    ).fetchone()
    not_null = bool(col and col["attnotnull"])
    if not fk and not not_null:
        return
    conn.execute("SAVEPOINT reply_reports_outlive_chats")
    try:
        conn.execute(
            "UPDATE reply_reports r SET chat_title = COALESCE(c.title, '') "
            "FROM chats c WHERE c.id = r.chat_id AND r.chat_title = ''"
        )
        if fk:
            quoted = '"' + fk["conname"].replace('"', '""') + '"'
            conn.execute(f"ALTER TABLE reply_reports DROP CONSTRAINT {quoted}")
            conn.execute(
                "ALTER TABLE reply_reports ADD CONSTRAINT reply_reports_chat_id_fkey "
                "FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE SET NULL"
            )
        if not_null:
            conn.execute("ALTER TABLE reply_reports ALTER COLUMN chat_id DROP NOT NULL")
        conn.execute("RELEASE SAVEPOINT reply_reports_outlive_chats")
        logger.info("reply_reports now outlive their chat")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT reply_reports_outlive_chats")
        logger.exception("reply_reports could not be made to outlive their chat; the next boot retries")


# 2.0: one record per phone-owned conversation an existing install holds.
_RECORDS_FLAG = "conversation_records_backfilled"


def backfill_conversation_records(conn) -> None:
    """The record of every phone-owned conversation made before the record
    existed, its line from the call log's label for its session. Only a
    caller's label (``caller:``, ``caller-pin:``, ``ephemeral``) or a
    recorded outside person gives the caller line and ``shared`` the
    route-less one; anything else, a pruned call-log row or an empty label
    included, gives ``person``, which stays a manager's as in 1.9.1: the
    label is younger than some calls and its rows age out. One-shot, called
    by ``run_migrations`` after the call log's columns are there."""
    if conn.execute("SELECT 1 FROM platform_settings WHERE key = %s",
                    (_RECORDS_FLAG,)).fetchone():
        return
    made = conn.execute(
        """INSERT INTO conversation_records
               (chat_id, agent, line, direction, identity_id, started_at)
           SELECT c.id, c.agent,
                  CASE WHEN c.external_identity_id <> '' THEN 'caller'
                       WHEN l.identity LIKE 'caller:%%' OR l.identity LIKE 'caller-pin:%%'
                            OR l.identity = 'ephemeral' THEN 'caller'
                       WHEN l.identity = 'shared' THEN 'shared'
                       ELSE 'person' END,
                  COALESCE(l.direction, 'inbound'), c.external_identity_id, c.created_at
           FROM chats c
           LEFT JOIN LATERAL (
               SELECT p.identity, p.direction FROM phone_call_log p
               WHERE c.session_id IS NOT NULL AND c.session_id <> ''
                 AND p.session_id = c.session_id
               ORDER BY p.started_at DESC LIMIT 1) l ON TRUE
           WHERE c.source_type = 'phone' AND c.user_sub = 'phone'
           ON CONFLICT (chat_id) DO NOTHING""").rowcount
    if made:
        logger.info("conversation_records: %d existing conversation(s) recorded", made)
    conn.execute("INSERT INTO platform_settings (key, value) VALUES (%s, 'done') "
                 "ON CONFLICT (key) DO NOTHING", (_RECORDS_FLAG,))

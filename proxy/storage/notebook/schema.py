"""DDL of the Notebook.

``notebook_entries`` holds what agents post about their own work: one row
per entry, written by the session's own agent, never edited. A manager's
deletion keeps the row (who, when) and blanks the text, so a secret posted
by mistake leaves the table at once. The retention pass keeps the newest
rows per agent (``services/infra/retention.py``). New-table pattern: the
whole table is created in init, so an existing database gains it at boot.
"""


def init_notebook(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notebook_entries (
            id TEXT PRIMARY KEY,
            agent TEXT NOT NULL REFERENCES agents(slug) ON DELETE CASCADE,
            -- the person who drove the session; NULL for a run of the agent
            -- itself (an agent-scope task, an agent-scope worker)
            author_sub TEXT,
            -- the session's kind in the source-type spelling: chat | task |
            -- phone | meeting, '' when the platform could not tell
            surface TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ,
            deleted_by TEXT,
            CONSTRAINT notebook_entries_text_len CHECK (
                char_length(text) <= 1200
                AND (char_length(text) >= 1 OR deleted_at IS NOT NULL)
            )
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notebook_entries_agent_time "
        "ON notebook_entries (agent, created_at DESC, id DESC)"
    )

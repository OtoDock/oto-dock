"""DDL of the checks domain (CHECKS.md, DATABASE-SCHEMA.md).

``agent_checks`` mirrors the check documents on disk (``config/checks/<name>/
check.json`` for an agent's, ``users/<u>/checks/<name>/`` for a user's) —
the files are the source, the row the cache keyed by the file's sha256, so
a hand edit is picked up at the next evaluation. ``check_verdicts`` is the
verdict store: one row per evaluated section per turn. ``check_settings``
holds the per-agent daily cap on judge spend (NULL = the platform setting,
itself empty = no cap). New-table pattern: whole-table CREATE in init.
"""


def init_checks(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_checks (
            agent TEXT NOT NULL REFERENCES agents(slug) ON DELETE CASCADE,
            -- '' for the agent's own checks (config/checks), else the
            -- username whose tree holds the check (users/<u>/checks).
            owner TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            -- the normalized document, as validated
            doc TEXT NOT NULL,
            doc_sha256 TEXT NOT NULL,
            -- the script beside it, '' when the check has none
            script_sha256 TEXT NOT NULL DEFAULT '',
            mandatory BOOLEAN NOT NULL DEFAULT FALSE,
            -- JSON list: chats | tasks | delegations
            applies TEXT NOT NULL DEFAULT '[]',
            -- who last wrote it: a user sub, or 'file' for a hand edit
            updated_by TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (agent, owner, name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS check_verdicts (
            id TEXT PRIMARY KEY,
            agent TEXT NOT NULL,
            owner TEXT NOT NULL DEFAULT '',
            check_name TEXT NOT NULL,
            -- schema | script | handler | judge — the section that decided
            section TEXT NOT NULL DEFAULT '',
            -- pass | fail | error | skipped
            status TEXT NOT NULL,
            pass BOOLEAN NOT NULL DEFAULT FALSE,
            score REAL,
            findings TEXT NOT NULL DEFAULT '[]',
            summary TEXT NOT NULL DEFAULT '',
            -- why an error or a skip (the platform's words)
            reason TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL DEFAULT '',
            -- the judged task run, '' for a chat
            run_id TEXT NOT NULL DEFAULT '',
            -- the judge's own task run, '' for the other kinds
            judge_run_id TEXT NOT NULL DEFAULT '',
            user_sub TEXT NOT NULL DEFAULT '',
            round INTEGER NOT NULL DEFAULT 0,
            -- 'local' or the machine id the section ran on
            ran_on TEXT NOT NULL DEFAULT '',
            engine TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            cost_usd REAL NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            -- the script that ran, for the trace
            script_sha256 TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_check_verdicts_agent_time "
        "ON check_verdicts (agent, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_check_verdicts_chat ON check_verdicts (chat_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_check_verdicts_check "
        "ON check_verdicts (agent, check_name)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS check_settings (
            agent TEXT PRIMARY KEY REFERENCES agents(slug) ON DELETE CASCADE,
            -- NULL = the platform default (checks_daily_cap_usd; empty = none)
            daily_cap_usd REAL,
            updated_by TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
    """)

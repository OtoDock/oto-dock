"""Schema for the files domain: the recover bin, file pins, sync state,
tombstones, authorship and transfers.

Called by ``storage.schema.init_schema`` in foreign-key order. Every
statement is idempotent (``CREATE ... IF NOT EXISTS``); see
``docs/architecture/DATABASE-SCHEMA.md``.
"""


def init_recover_bin(conn) -> None:
    """Workspace recover-bin (soft-deleted / conflict captures)."""
    # Workspace Recover Bin (``storage/files/recover_bin_store.py``) — the one passive
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


def init_pinned_files(conn) -> None:
    """Dock file pins (read-only file rows on the chat/project Dock)."""
    # One row per pinned FILE (``storage/files/db_file_pins.py``): the Dock renders
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

    # sync_state (storage/files/sync_state_store.py): the per-(machine,agent,file)
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

    # file_tombstones (storage/files/file_tombstones_store.py): an EXPLICIT, timestamped
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

    # file_author (storage/files/file_author_store.py): the persisted platform
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


def init_file_transfers(conn) -> None:
    """Cross-agent file transfers (delegation-mcp ``send_files``).

    One row per transfer call — the audit record + the per-creator quota
    counter. ``seen_at`` (and its index) are RESERVED, unused since the
    v1 session-start notice was removed 2026-08-14 — kept so installs
    that created the table before the removal stay identical to fresh
    ones, and a future "since last run" digest can rebuild on it."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_file_transfers (
            id            TEXT PRIMARY KEY,
            source_agent  TEXT NOT NULL,
            target_agent  TEXT NOT NULL,
            scope         TEXT NOT NULL,
            owner_sub     TEXT NOT NULL DEFAULT '',
            dest_dir      TEXT NOT NULL DEFAULT '',
            file_count    INTEGER NOT NULL,
            total_bytes   BIGINT NOT NULL,
            note          TEXT NOT NULL DEFAULT '',
            created_by    TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            seen_at       TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_file_transfers_unseen
        ON agent_file_transfers (target_agent, seen_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_file_transfers_creator
        ON agent_file_transfers (created_by, created_at)
    """)

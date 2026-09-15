"""Schema for the knowledge domain: libraries, attachments and the mirror state.

Called by ``storage.schema.init_schema`` in foreign-key order. Every
statement is idempotent (``CREATE ... IF NOT EXISTS``); see
``docs/architecture/DATABASE-SCHEMA.md``.
"""


def init_knowledge_libraries(conn) -> None:
    """Shared knowledge libraries (promote-in-place, mirrored to consumers).

    ``knowledge_libraries``: one row per shared SUBTREE of a source agent's
    ``knowledge/`` folder — ``subdir = ''`` is the whole-folder share, a
    non-empty ``subdir`` shares just that subtree. Subtrees of one agent are
    kept disjoint at promote time (API validation), so any knowledge path
    belongs to at most one library.
    ``knowledge_library_attachments``: one row per (library, consumer);
    ``writable`` gates mirror→source propagation and the satellite
    write-back subtree rule. Mirrors are real files at
    ``agents/<consumer>/knowledge/shared/<source>/<subdir>/`` (the slug
    segment the write gates key on does not move) maintained by
    ``services/knowledge/library_projector.py`` — these tables are the
    single source of truth for which mirrors must exist.

    Born with composite keys: the single-key v1 shape never reached a
    released install (public latest at the time predates libraries), so
    there is no migration — the two dev installs carrying v1 rows were
    reset by hand (drop both tables; init recreates; re-share in the UI)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_libraries (
            source_agent  TEXT NOT NULL,
            -- Library subtree relative to the source's knowledge/ root;
            -- '' = the whole folder. Validated at promote (relative, no
            -- dot/dotdot segments, first segment never memory/shared/
            -- .credentials, disjoint from the agent's other libraries).
            subdir        TEXT NOT NULL DEFAULT '',
            created_by    TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            -- Human label for the library, set when it is shared. DISPLAY
            -- ONLY for paths under knowledge/shared/ (the mirror folder is
            -- keyed on ``source_agent``/``subdir``) — but it DOES drive the
            -- library's bulletin filename (``bulletin/<name>.md`` inside
            -- the subtree), so promote validates it as a filename too.
            -- Deliberately not unique — every surface shows the owning
            -- agent beside it. Empty = fall back to the agent's name.
            name          TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (source_agent, subdir)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_library_attachments (
            source_agent    TEXT NOT NULL,
            subdir          TEXT NOT NULL DEFAULT '',
            consumer_agent  TEXT NOT NULL,
            writable        BOOLEAN NOT NULL DEFAULT FALSE,
            created_by      TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            PRIMARY KEY (source_agent, subdir, consumer_agent)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_kl_attach_consumer
        ON knowledge_library_attachments (consumer_agent)
    """)
    # library_mirror_state (storage/knowledge/db_library_mirror_state.py): the
    # projector's per-(consumer, source, file) merge base — the content hash
    # last CONVERGED between that consumer's mirror and the source (the
    # ``sync_state`` idea applied to shared libraries, 2026-09-03). It is what
    # lets a reconcile tell "the mirror never had this file" (project it) from
    # "the mirror had it and deleted it" (a deliberate delete → propagate) and
    # "the source deleted it" (heal the mirror, never re-adopt). A CACHE +
    # attribution hint, never a delete authority on its own: an absent row
    # degrades to first-projection behaviour (heal), never to data loss.
    # ``base_size``/``base_mtime`` are the rsync-style quick-check inputs
    # (equal size + mtime ⇒ unchanged, no re-hash). No TTL: rows live while
    # the pair converges; dropped when a file is gone on both sides or the
    # attachment is torn down.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS library_mirror_state (
            consumer_agent  TEXT NOT NULL,
            source_agent    TEXT NOT NULL,
            sub_rel         TEXT NOT NULL,
            base_hash       TEXT NOT NULL,
            base_size       BIGINT NOT NULL DEFAULT 0,
            base_mtime      DOUBLE PRECISION NOT NULL DEFAULT 0,
            updated_at      TEXT NOT NULL,
            PRIMARY KEY (consumer_agent, source_agent, sub_rel)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_lib_mirror_state_source
        ON library_mirror_state (source_agent, sub_rel)
    """)

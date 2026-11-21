"""Schema for the sharing domain: the ``shares`` table and the placement
hides (SHARING.md).

Called by ``storage.schema.init_schema`` after the pinned apps (an app share
references its row) and the chats; every statement is idempotent. See
``docs/architecture/DATABASE-SCHEMA.md``. The table shipped in 1.7.0 with
one grantee kind: the columns, CHECKs and indexes of the three-kind model
reach an existing install through ``migrate_sharing``, which is also where every
index on a migrated column is created (the CREATE below no-ops on an existing
table, so an index here would name a missing column).
"""

import logging
import re

from auth import roles
from storage.schema_base import _check_constraint_words, _index_exists

logger = logging.getLogger(__name__)

# The named CHECKs ``run_migrations`` keeps current on an existing install
# (the 1.7.0 scope CHECK was auto-named; these are spelled here and found by
# name in the catalog).
GRANTEE_CHECK = (
    "(scope = 'external' AND grantee_sub IS NULL AND grantee_agent IS NULL"
    " AND grantee_department IS NULL)"
    " OR (scope = 'internal' AND grantee_kind = 'person' AND grantee_sub IS NOT NULL"
    " AND grantee_agent IS NULL AND grantee_department IS NULL)"
    " OR (scope = 'internal' AND grantee_kind = 'agent' AND target_kind = 'app'"
    " AND grantee_agent IS NOT NULL AND grantee_agent <> ''"
    " AND grantee_sub IS NULL AND grantee_department IS NULL)"
    " OR (scope = 'internal' AND grantee_kind = 'department' AND target_kind = 'app'"
    " AND grantee_department IS NOT NULL AND grantee_department <> ''"
    " AND grantee_sub IS NULL AND grantee_agent IS NULL)"
)
GRANTEE_KIND_CHECK = "grantee_kind IN ('', 'person', 'agent', 'department')"
DECISION_CHECK = "decision IN ('', 'pending', 'accepted', 'declined')"
# The frozen spelling of the per-agent roles (``auth/roles.AGENT_ROLES`` is
# built from the CHECKs, never the other way round; ``tests/auth/test_roles.py``).
ROLE_CAP_CHECK = "role_cap IN ('manager', 'editor', 'contributor', 'viewer')"


def init_shares(conn) -> None:
    """Shares of apps and chats: internal shares to a person, an agent or a
    department, and external links; the per-agent placement hides."""
    # One row per share (``storage/sharing/share_store.py``). The target is
    # one of two nullable FK columns — Postgres cannot point one column at
    # two tables — and the CHECK ties the column to ``target_kind``; the
    # three delete sites (hard unpin, chat delete, user delete) cascade the
    # rows through the FKs. Internal: ``grantee_kind`` says which of
    # ``grantee_sub`` (a person), ``grantee_agent`` (an agent's slug, no FK
    # like ``pinned_apps.agent``) or ``grantee_department`` (an id, no FK)
    # names the recipient; ``role_cap`` is the role the sharer allows;
    # ``decision`` is the recipient's answer (``pending`` until a person
    # accepts or declines an app; a chat, an agent share and a department
    # share are ``accepted`` when made), with ``decided_by`` and
    # ``decided_at``; ``placed_agent`` is the agent a person accepted an app
    # into; ``hidden_by_grantee`` is a person's own "hide for me". External:
    # ``token_hash`` is sha256 of the link token, ``password_hash`` is empty
    # for a public link, ``allow_actions`` is the single Buttons switch,
    # ``actions_today``/``actions_day`` count the per-share daily cap; the
    # kind and the decision are empty. ``state`` is ``suspended`` while the
    # app is soft-unpinned and nothing else. The defaults ``person`` and
    # ``pending`` let a 1.7.0 INSERT, which names neither, pass after a
    # downgrade. Chat snapshots keep their directory in ``snapshot_ref``.
    # Timestamps are ISO text like ``pinned_apps``. Inline constraints only
    # (never ``ALTER ADD CONSTRAINT`` outside run_migrations).
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS shares (
            id                 TEXT PRIMARY KEY,
            target_kind        TEXT NOT NULL CHECK (target_kind IN ('app', 'chat')),
            app_id             TEXT REFERENCES pinned_apps(id) ON DELETE CASCADE,
            chat_id            TEXT REFERENCES chats(id) ON DELETE CASCADE,
            scope              TEXT NOT NULL CHECK (scope IN ('internal', 'external')),
            grantee_kind       TEXT NOT NULL DEFAULT 'person',
            grantee_sub        TEXT REFERENCES users(sub) ON DELETE CASCADE,
            grantee_agent      TEXT,
            grantee_department TEXT,
            role_cap           TEXT NOT NULL DEFAULT 'viewer',
            decision           TEXT NOT NULL DEFAULT 'pending',
            decided_by         TEXT,
            decided_at         TEXT,
            placed_agent       TEXT,
            hidden_by_grantee  BOOLEAN NOT NULL DEFAULT FALSE,
            token_hash         TEXT UNIQUE,
            password_hash      TEXT NOT NULL DEFAULT '',
            public             BOOLEAN NOT NULL DEFAULT FALSE,
            allow_actions      BOOLEAN NOT NULL DEFAULT FALSE,
            include_tools      BOOLEAN NOT NULL DEFAULT FALSE,
            state              TEXT NOT NULL DEFAULT 'active'
                               CHECK (state IN ('active', 'suspended')),
            expires_at         TEXT,
            revoked_at         TEXT,
            created_by         TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
            created_at         TEXT NOT NULL,
            last_access_at     TEXT,
            access_count       INTEGER NOT NULL DEFAULT 0,
            actions_today      INTEGER NOT NULL DEFAULT 0,
            actions_day        TEXT NOT NULL DEFAULT '',
            snapshot_ref       TEXT NOT NULL DEFAULT '',
            CHECK ((target_kind = 'app' AND app_id IS NOT NULL AND chat_id IS NULL)
                OR (target_kind = 'chat' AND chat_id IS NOT NULL AND app_id IS NULL)),
            CONSTRAINT shares_grantee_check CHECK ({GRANTEE_CHECK}),
            CONSTRAINT shares_grantee_kind_check CHECK ({GRANTEE_KIND_CHECK}),
            CONSTRAINT shares_decision_check CHECK ({DECISION_CHECK}),
            CONSTRAINT shares_role_cap_check CHECK ({ROLE_CAP_CHECK})
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_app ON shares (app_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_chat ON shares (chat_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_grantee ON shares (grantee_sub)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_created_by ON shares (created_by)")
    # A member's own hide of a placed app in ONE agent, whichever share
    # placed it there (an agent share and a department share of the same
    # app hide once; a department placement hidden here stays visible in
    # the department's other agents). ``agent`` has no FK, like
    # ``pinned_apps.agent``; ``delete_agent`` removes the rows.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS share_placement_hides (
            app_id     TEXT NOT NULL REFERENCES pinned_apps(id) ON DELETE CASCADE,
            agent      TEXT NOT NULL,
            user_sub   TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (app_id, agent, user_sub)
        )
    """)

# The named CHECKs of ``shares`` (SHARING.md) ``run_migrations`` keeps current
# the same way: (name, definition, the quoted words the catalog renders). The
# 1.7.0 table carried an auto-named scope CHECK over ``grantee_sub`` alone;
# the grantee rewrite drops it, found by its definition.
_SHARE_CHECKS: tuple[tuple[str, str, frozenset[str]], ...] = tuple(
    (name, definition, frozenset(re.findall(r"'([^']*)'", definition)))
    for name, definition in (
        ("shares_grantee_check", GRANTEE_CHECK),
        ("shares_grantee_kind_check", GRANTEE_KIND_CHECK),
        ("shares_decision_check", DECISION_CHECK),
    )
)


def _rewrite_share_checks(conn) -> None:
    """Keep the named ``shares`` CHECKs at the code's words on an existing
    install: a constraint that is absent or lacks a word is dropped and added
    again inside a SAVEPOINT (a no-op once the words are in, logged and
    retried next boot on a failure). The grantee rewrite also drops the 1.7.0
    scope CHECK, auto-named by Postgres, found by its definition: it reads
    ``grantee_sub`` and not ``grantee_kind``."""
    for name, definition, words in _SHARE_CHECKS:
        present = _check_constraint_words(conn, "shares", name)
        if present is not None and present >= words:
            continue
        conn.execute(f"SAVEPOINT {name}")
        try:
            if name == "shares_grantee_check":
                old = conn.execute(
                    "SELECT c.conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "JOIN pg_namespace n ON n.oid = t.relnamespace "
                    "WHERE t.relname = 'shares' AND n.nspname = current_schema() "
                    "AND c.contype = 'c' "
                    "AND pg_get_constraintdef(c.oid) LIKE '%%grantee_sub%%' "
                    "AND pg_get_constraintdef(c.oid) NOT LIKE '%%grantee_kind%%'"
                ).fetchall()
                for row in old:
                    quoted = '"' + row["conname"].replace('"', '""') + '"'
                    conn.execute(f"ALTER TABLE shares DROP CONSTRAINT IF EXISTS {quoted}")
            conn.execute(f"ALTER TABLE shares DROP CONSTRAINT IF EXISTS {name}")
            conn.execute(f"ALTER TABLE shares ADD CONSTRAINT {name} CHECK ({definition})")
            conn.execute(f"RELEASE SAVEPOINT {name}")
            logger.info("shares CHECK %s rewritten", name)
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            logger.exception("shares CHECK %s could not be rewritten; the next boot retries", name)


def migrate_sharing(conn) -> None:
    """The sharing domain's forward migrations on an existing install: the
    three-kind columns of ``shares``, their backfill, the named CHECKs, and the
    indexes on the migrated columns. ``run_migrations`` calls it after the role
    CHECK loop."""

    # 1.7.1: shares to an agent or a department, a role cap, a decision and a
    # placement (SHARING.md). The 1.7.0 table knew one grantee kind, so the
    # columns reach an existing install here; their defaults are what a
    # 1.7.0 INSERT leaves after a downgrade (``person``, ``pending``,
    # ``viewer``), which every CHECK below accepts. The role-cap CHECK rides
    # the column add, so the role loop above finds it from the next boot.
    conn.execute(
        "ALTER TABLE shares ADD COLUMN IF NOT EXISTS grantee_kind TEXT NOT NULL DEFAULT 'person'"
    )
    conn.execute("ALTER TABLE shares ADD COLUMN IF NOT EXISTS grantee_agent TEXT")
    conn.execute("ALTER TABLE shares ADD COLUMN IF NOT EXISTS grantee_department TEXT")
    conn.execute(
        "ALTER TABLE shares ADD COLUMN IF NOT EXISTS role_cap TEXT NOT NULL DEFAULT 'viewer' "
        "CONSTRAINT shares_role_cap_check CHECK (role_cap IN ("
        + ", ".join(f"'{w}'" for w in roles.AGENT_ROLES) + "))"
    )
    conn.execute(
        "ALTER TABLE shares ADD COLUMN IF NOT EXISTS decision TEXT NOT NULL DEFAULT 'pending'"
    )
    conn.execute("ALTER TABLE shares ADD COLUMN IF NOT EXISTS decided_by TEXT")
    conn.execute("ALTER TABLE shares ADD COLUMN IF NOT EXISTS decided_at TEXT")
    conn.execute("ALTER TABLE shares ADD COLUMN IF NOT EXISTS placed_agent TEXT")
    # The backfill runs at every boot and never writes ``pending``, so a
    # decision is never flipped back and a row 1.7.0 wrote with the defaults
    # after a downgrade is repaired: a link has no kind and no decision; a
    # chat share needs no decision.
    conn.execute(
        "UPDATE shares SET grantee_kind = '', decision = '' "
        "WHERE scope = 'external' AND (grantee_kind <> '' OR decision <> '')"
    )
    conn.execute(
        "UPDATE shares SET decision = 'accepted' "
        "WHERE scope = 'internal' AND target_kind = 'chat' AND decision = 'pending'"
    )
    _rewrite_share_checks(conn)
    # The 1.7.0 unique index keyed every unrevoked internal row and would
    # refuse a re-share after a decline; the three per-kind indexes replace
    # it. Here, never in init_shares: the CREATE no-ops on an existing table
    # and an index on a column this block adds would fail the boot.
    if _index_exists(conn, "idx_shares_internal_grant"):
        conn.execute("SAVEPOINT drop_idx_shares_internal_grant")
        try:
            conn.execute("DROP INDEX IF EXISTS idx_shares_internal_grant")
            conn.execute("RELEASE SAVEPOINT drop_idx_shares_internal_grant")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT drop_idx_shares_internal_grant")
            logger.exception("dropping idx_shares_internal_grant failed; the next boot retries")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_shares_person_grant "
        "ON shares (target_kind, COALESCE(app_id, chat_id), grantee_sub) "
        "WHERE scope = 'internal' AND grantee_kind = 'person' AND revoked_at IS NULL "
        "AND decision <> 'declined'"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_shares_agent_grant "
        "ON shares (app_id, grantee_agent) "
        "WHERE grantee_kind = 'agent' AND revoked_at IS NULL AND decision <> 'declined'"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_shares_department_grant "
        "ON shares (app_id, grantee_department) "
        "WHERE grantee_kind = 'department' AND revoked_at IS NULL AND decision <> 'declined'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shares_grantee_agent ON shares (grantee_agent)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shares_grantee_department ON shares (grantee_department)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shares_placed_agent ON shares (placed_agent)"
    )

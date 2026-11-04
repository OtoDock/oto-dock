"""OAuth bearer-token allowlist storage.

The table says which (provider_id, host_pattern) pairs may receive a
person's OAuth token via HTTP ``Authorization: Bearer`` injection. A row's
``source`` is ``admin`` (a row an admin typed, or one an older version
seeded) or the name of the MCP whose manifest put it there: an MCP
installed from the catalog, or from a ZIP whose hosts an admin allowed,
owns the rows of its ``proposed_hosts`` (``services/community/manifest_hosts.py``
keeps them in step with its install, update, disable and removal). Two
ledger tables of this module's own record what each MCP's provenance
approved and where each MCP came from (the catalog or a ZIP).

No row is predefined. An MCP's row takes over the admin row of exactly the
same pair when the MCP makes it live, so the address follows the MCP from
then on; an admin row no installed MCP declares is never touched here.
"Remove custom addresses" deletes every admin row and keeps the MCPs'
rows. Admins extend via /v1/admin/oauth-bearer-allowlist.

The gateway (``core/credentials/mcp_gateway.py``) judges every forward
against this table and memoises the answer briefly; ``generation`` moves at
every change so a removed row refuses at the next request.

Matcher supports two wildcard patterns:
  * Exact host: ``mcp.slack.com``
  * Subdomain wildcard: ``*.linear.app`` (matches ``mcp.linear.app``,
    ``api.linear.app``, etc. — NOT ``linear.app`` itself).
"""

from __future__ import annotations

import fnmatch
import logging
from datetime import datetime, timezone

from storage.pg import get_conn

logger = logging.getLogger("otodock.bearer-allowlist")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


Pair = tuple[str, str]

# Moves at every change of the table in this process; the gateway's memo
# compares it so a removed row refuses at the next request.
_generation = 0


def generation() -> int:
    return _generation


def _bump() -> None:
    global _generation
    _generation += 1


ADMIN_SOURCE = "admin"

# The key a migrated table and a fresh one share (``CREATE TABLE`` in
# ``storage/identity/schema.py`` names the same columns inline).
_SOURCE_KEY = "oauth_bearer_allowlist_source_key"


def migrate(conn) -> None:
    """An older install's table takes ``source`` (every row an admin's) and
    the three-column key (``migrate_identity`` calls it). Guarded by the
    catalog, so a migrated or fresh table gets no DDL; in a SAVEPOINT, so a
    failure never aborts the boot (it retries next start)."""
    old = conn.execute(
        "SELECT c.conname FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE t.relname = 'oauth_bearer_allowlist' AND n.nspname = current_schema() "
        "AND c.contype = 'u' AND ("
        "  SELECT array_agg(a.attname::text ORDER BY a.attname) FROM pg_attribute a "
        "  WHERE a.attrelid = t.oid AND a.attnum = ANY(c.conkey)"
        ") = ARRAY['host_pattern', 'provider_id']"
    ).fetchall()
    has_source = conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'oauth_bearer_allowlist' AND column_name = 'source'"
    ).fetchone() is not None
    if has_source and not old:
        return
    conn.execute("SAVEPOINT bearer_allowlist_source")
    try:
        conn.execute(
            "ALTER TABLE oauth_bearer_allowlist "
            "ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'admin'"
        )
        for row in old:
            quoted = '"' + row["conname"].replace('"', '""') + '"'
            conn.execute(f"ALTER TABLE oauth_bearer_allowlist DROP CONSTRAINT IF EXISTS {quoted}")
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_SOURCE_KEY} "
            "ON oauth_bearer_allowlist (provider_id, host_pattern, source)"
        )
        conn.execute("RELEASE SAVEPOINT bearer_allowlist_source")
        logger.info("oauth_bearer_allowlist: rows marked admin, keyed by source")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT bearer_allowlist_source")
        logger.exception("oauth_bearer_allowlist source migration failed: will retry next startup")


def remove_admin_rows() -> list[dict]:
    """Every admin row goes and every row an MCP put there stays, since
    disabling or removing the MCP is how its hosts go. Returns the full
    list."""
    with get_conn() as conn:
        conn.execute("DELETE FROM oauth_bearer_allowlist WHERE source = %s", (ADMIN_SOURCE,))
        conn.commit()
    _bump()
    return list_allowed()


def list_allowed() -> list[dict]:
    """Return [{id, provider_id, host_pattern, added_by, added_at, source}, ...]."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, provider_id, host_pattern, added_by, added_at, source "
            "FROM oauth_bearer_allowlist "
            "ORDER BY provider_id, host_pattern, source"
        ).fetchall()
        return [dict(r) for r in rows]


def get_allowed(row_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, provider_id, host_pattern, added_by, added_at, source "
            "FROM oauth_bearer_allowlist WHERE id = %s", (row_id,),
        ).fetchone()
        return dict(row) if row else None


def add_allowed(provider_id: str, host_pattern: str, added_by: str = "admin") -> int:
    """Insert an admin's allowlist entry. Returns the new row id (or the
    existing one on conflict)."""
    with get_conn() as conn:
        row = conn.execute(
            """INSERT INTO oauth_bearer_allowlist
               (provider_id, host_pattern, added_by, added_at, source)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (provider_id, host_pattern, source)
               DO UPDATE SET added_at = oauth_bearer_allowlist.added_at
               RETURNING id""",
            (provider_id, host_pattern, added_by, _now(), ADMIN_SOURCE),
        ).fetchone()
        conn.commit()
    _bump()
    return row["id"]


# ---------------------------------------------------------------------------
# An MCP's own rows, what its provenance approved and where it came from
# ---------------------------------------------------------------------------

def _mcp_source(mcp_name: str) -> str:
    if not mcp_name or mcp_name == ADMIN_SOURCE:
        raise ValueError(f"an MCP named {mcp_name!r} cannot own allowlist rows")
    return mcp_name


def approved_hosts(mcp_name: str) -> set[Pair]:
    """The (provider_id, host) pairs the MCP's provenance approved."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT provider_id, host_pattern FROM oauth_bearer_allowlist_mcp_approved "
            "WHERE mcp_name = %s", (_mcp_source(mcp_name),),
        ).fetchall()
    return {(r["provider_id"], r["host_pattern"]) for r in rows}


def live_hosts(mcp_name: str) -> set[Pair]:
    """The (provider_id, host) rows the MCP owns on the allowlist now."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT provider_id, host_pattern FROM oauth_bearer_allowlist WHERE source = %s",
            (_mcp_source(mcp_name),),
        ).fetchall()
    return {(r["provider_id"], r["host_pattern"]) for r in rows}


def has_origin(mcp_name: str) -> bool:
    """Whether an install or update recorded where the MCP came from: an
    MCP without it was installed before the record existed."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM oauth_bearer_allowlist_mcp_origin WHERE mcp_name = %s",
            (_mcp_source(mcp_name),),
        ).fetchone() is not None


def admin_pairs(provider_id: str) -> set[Pair]:
    """The exact pairs an admin row holds for the provider, the host
    lowercased as the manifests declare it."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT provider_id, lower(host_pattern) AS host FROM oauth_bearer_allowlist "
            "WHERE provider_id = %s AND source = %s", (provider_id, ADMIN_SOURCE),
        ).fetchall()
    return {(r["provider_id"], r["host"]) for r in rows}


def set_mcp_hosts(mcp_name: str, *, approved: dict[Pair, str], live: set[Pair],
                  take_over: set[Pair] | None = None, handover: set[Pair] | None = None,
                  origin: str = "", by: str) -> bool:
    """The MCP's ledger becomes ``approved`` (each pair with how it was
    approved; a pair already approved keeps its own) and its rows ``live``
    (a subset of it), in one transaction. The admin row of exactly a pair of
    ``take_over`` that goes live is deleted in the same transaction: the
    MCP's row replaces it. A pair of ``handover`` stands only on an exact
    admin row this transaction holds (locked, so an admin's removal waits
    for the commit); one whose row is already gone is neither approved nor
    live. ``origin`` (where an install came from) is recorded when given.
    Whether a row went (a paired machine holding a token is told); a row
    taken over is not one, its pair stays allowed."""
    source = _mcp_source(mcp_name)
    approved = dict(approved)
    removed = False
    with get_conn() as conn:
        if handover:
            held = {
                (r["provider_id"], r["host"])
                for r in conn.execute(
                    "SELECT provider_id, lower(host_pattern) AS host FROM oauth_bearer_allowlist "
                    "WHERE source = %s AND (provider_id, lower(host_pattern)) "
                    "IN (SELECT * FROM unnest(%s::text[], %s::text[])) ORDER BY id FOR UPDATE",
                    (ADMIN_SOURCE, [p for p, _ in handover], [h for _, h in handover]),
                ).fetchall()
            }
            for pair in handover - held:
                approved.pop(pair, None)
        live = live & set(approved)
        take_over = (take_over or set()) & live
        conn.execute(
            "DELETE FROM oauth_bearer_allowlist_mcp_approved WHERE mcp_name = %s "
            "AND (provider_id, host_pattern) NOT IN (SELECT * FROM unnest(%s::text[], %s::text[]))",
            (source, [p for p, _ in approved], [h for _, h in approved]),
        )
        for (provider_id, host), via in sorted(approved.items()):
            conn.execute(
                "INSERT INTO oauth_bearer_allowlist_mcp_approved "
                "(mcp_name, provider_id, host_pattern, via, approved_by, approved_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (source, provider_id, host, via, by, _now()),
            )
        if origin:
            conn.execute(
                "INSERT INTO oauth_bearer_allowlist_mcp_origin (mcp_name, via, recorded_at) "
                "VALUES (%s, %s, %s) ON CONFLICT (mcp_name) "
                "DO UPDATE SET via = EXCLUDED.via, recorded_at = EXCLUDED.recorded_at",
                (source, origin, _now()),
            )
        current = {
            (r["provider_id"], r["host_pattern"]): r["id"]
            for r in conn.execute(
                "SELECT id, provider_id, host_pattern FROM oauth_bearer_allowlist WHERE source = %s",
                (source,),
            ).fetchall()
        }
        gone = [row_id for pair, row_id in current.items() if pair not in live]
        if gone:
            conn.execute("DELETE FROM oauth_bearer_allowlist WHERE id = ANY(%s)", (gone,))
            removed = True
        for provider_id, host in sorted(live - set(current)):
            conn.execute(
                "INSERT INTO oauth_bearer_allowlist "
                "(provider_id, host_pattern, added_by, added_at, source) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (provider_id, host, by, _now(), source),
            )
        if take_over:
            # One statement after the MCP's rows exist: no request sees the
            # pair missing, and every takeover locks the rows in id order.
            conn.execute(
                "DELETE FROM oauth_bearer_allowlist WHERE id IN (SELECT id FROM oauth_bearer_allowlist "
                "WHERE source = %s AND (provider_id, lower(host_pattern)) "
                "IN (SELECT * FROM unnest(%s::text[], %s::text[])) ORDER BY id FOR UPDATE)",
                (ADMIN_SOURCE, [p for p, _ in take_over], [h for _, h in take_over]),
            )
        conn.commit()
    _bump()
    return removed


def forget_mcp(mcp_name: str) -> bool:
    """The MCP's rows, its ledger and its origin go (its removal, an orphan
    row's Remove). Whether a row went."""
    source = _mcp_source(mcp_name)
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM oauth_bearer_allowlist WHERE source = %s", (source,))
        conn.execute("DELETE FROM oauth_bearer_allowlist_mcp_approved WHERE mcp_name = %s", (source,))
        conn.execute("DELETE FROM oauth_bearer_allowlist_mcp_origin WHERE mcp_name = %s", (source,))
        conn.commit()
    _bump()
    return (cur.rowcount or 0) > 0


def delete_allowed(row_id: int) -> bool:
    """Remove an allowlist entry by id. Returns True if deleted."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM oauth_bearer_allowlist WHERE id = %s", (row_id,),
        )
        conn.commit()
    _bump()
    return (cur.rowcount or 0) > 0


def _matches(patterns: list[str], host: str) -> bool:
    host_lc = host.lower()
    return any(p and fnmatch.fnmatchcase(host_lc, p.lower()) for p in patterns)


def is_host_allowed(provider_id: str, host: str) -> bool:
    """True iff (provider_id, host) is on the allowlist.

    Matches against ``host_pattern`` with fnmatch semantics — exact host
    or ``*.domain.com`` style wildcards. Case-insensitive on host.
    """
    if not provider_id or not host:
        return False
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT host_pattern FROM oauth_bearer_allowlist "
            "WHERE provider_id = %s",
            (provider_id,),
        ).fetchall()
    return _matches([r["host_pattern"] or "" for r in rows], host)


def mcp_row_allows(provider_id: str, host: str) -> bool:
    """True iff a row an MCP owns allows (provider_id, host): what an
    admin's row allows does not count."""
    if not provider_id or not host:
        return False
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT host_pattern FROM oauth_bearer_allowlist WHERE provider_id = %s AND source <> %s",
            (provider_id, ADMIN_SOURCE),
        ).fetchall()
    return _matches([r["host_pattern"] or "" for r in rows], host)
